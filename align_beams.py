# -*- coding: utf-8 -*-
"""구조 평면도 ↔ 건축 평면도 정합 → 보(beam) 위치 이식.

정합은 원본 프로젝트 compare_drawings.py 의 estimate_translation() 방식 이식:
  · 축 분리 투표 — 수직선의 x끼리, 수평선의 y끼리 (건축−구조) 차이를 5mm 빈에
    투표해 최빈값을 취한다. 두 도면이 같은 원도(주차장 배치)에서 나왔으므로
    공유 기하(기둥면·벽면)가 정확히 같은 차이값을 반복 생성 → 최빈값이 압도.
    (내가 처음 쓴 '기둥 중심 군집 매칭'은 8m 격자 주기 별칭에 취약했음)
  · 점선/은선/중심선(HID·CEN 등)은 타층 투영·가상선 → 앵커에서 제외
  · 검증 — ① 1mm 정확 일치 선분 수 ② 주동표시(S07) bbox IoU
    ③ 변환된 보 끝점이 건축 기둥 1m 내 안착하는 수

보 지오메트리(4_보 Line[HID 포함 — 보 자체가 은선으로 작도됨] + !BTS I거더/T형보
폴리라인)를 오프셋 변환, 건축 방 목록 주변으로 클립 → output/<건축base>_beams.json

사용법: python align_beams.py data/510_지하1층_구조.json data/510_지하1층_pit.json
"""
import argparse
import collections
import json
import math
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SP = r"D:/Python_test/fran_consist_cad_json"
sys.path.insert(0, SP)
import facility_geom as fg

BEAM_LINE_LAYERS = ("4_보",)
BEAM_POLY_LAYERS = ("!BTS 배치 - I거더", "!BTS 배치 - T형 보 (3대 주차)")
STRUCT_ANCHOR = ("COL-H",)          # 구조도 앵커(실선 기둥)
VOTE_BIN = 5.0                      # compare_drawings 관례

# 점선/은선/중심선 판별 — compare_drawings.is_dashed 이식
_SOLID = {"continuous", "bylayer", "byblock", "", "solid"}
_DASH_KEY = ("HID", "HIDDEN", "HD", "DASH", "CEN", "CENTER", "PHANTOM", "ACAD_ISO", "DOT")


def is_dashed(linetype):
    if linetype is None:
        return False
    s = str(linetype).strip()
    if s.lower() in _SOLID:
        return False
    return any(s.upper().startswith(k) for k in _DASH_KEY)


def load(path):
    return json.load(open(path, encoding="utf-8"))


def segs_of(ents, layers, solid_only=False, contains=False):
    def hit(ly):
        ly = ly or ""
        return any((k in ly) if contains else (ly == k) for k in layers)

    out = []
    for e in ents:
        if not hit(e.get("Layer")):
            continue
        if solid_only and is_dashed(e.get("Linetype")):
            continue
        t = e.get("Type")
        if t == "Line":
            a, b = e["Start"], e["End"]
            out.append((a[0], a[1], b[0], b[1]))
        elif t == "Polyline":
            v = e["Verts"] + ([e["Verts"][0]] if e.get("Closed") else [])
            for i in range(len(v) - 1):
                out.append((v[i][0], v[i][1], v[i + 1][0], v[i + 1][1]))
    return out


def estimate_translation(s_segs, a_segs):
    """compare_drawings 방식: 수직선 x끼리·수평선 y끼리 차(건축−구조) 최빈값."""
    def axis_vals(segs):
        vx, vy = [], []
        for x1, y1, x2, y2 in segs:
            if abs(x2 - x1) < 1 and abs(y2 - y1) >= 100:
                vx.append(round(x1))
            elif abs(y2 - y1) < 1 and abs(x2 - x1) >= 100:
                vy.append(round(y1))
        return vx, vy

    sx, sy = axis_vals(s_segs)
    ax, ay = axis_vals(a_segs)

    def vote(sv, av, label):
        votes = collections.Counter()
        for a in sv:
            for b in av:
                votes[round((b - a) / VOTE_BIN) * VOTE_BIN] += 1
        top = votes.most_common(3)
        print(f"  {label} 투표 상위: " + " · ".join(f"{v:+.0f}({n})" for v, n in top))
        return float(top[0][0]) if top else 0.0

    return vote(sx, ax, "dx"), vote(sy, ay, "dy")


def exact_match(s_segs, a_segs, dx, dy):
    """1mm 정확 일치 선분 수 (compare_drawings.alignment_check 관례)."""
    def keyset(segs, ox=0.0, oy=0.0):
        out = set()
        for x1, y1, x2, y2 in segs:
            a = (round(x1 + ox), round(y1 + oy))
            b = (round(x2 + ox), round(y2 + oy))
            out.add((a, b) if a <= b else (b, a))
        return out
    S = keyset(s_segs, dx, dy)
    A = keyset(a_segs)
    inter = len(S & A)
    return inter, inter / max(1, min(len(S), len(A)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("struct_json")
    ap.add_argument("arch_json")
    ap.add_argument("--anchor", default=",".join(STRUCT_ANCHOR),
                    help="구조 앵커 레이어(부분일치, 콤마 구분)")
    ap.add_argument("--beam-lines", default=",".join(BEAM_LINE_LAYERS),
                    help="보 선분 레이어(부분일치)")
    ap.add_argument("--beam-polys", default=",".join(BEAM_POLY_LAYERS),
                    help="PC거더 폴리곤 레이어(부분일치)")
    ap.add_argument("--clip-margin", type=float, default=5000.0,
                    help="보 클립 여유(mm) — 건축 방 영역에서 이 이상 떨어진 보 제거")
    ap.add_argument("--depth", type=float, default=None,
                    help="보 춤(mm) — 도면 미기재 값을 수동 지정(예: 900)")
    args = ap.parse_args()
    anchor_ly = tuple(s.strip() for s in args.anchor.split(",") if s.strip())
    beam_line_ly = tuple(s.strip() for s in args.beam_lines.split(",") if s.strip())
    beam_poly_ly = tuple(s.strip() for s in args.beam_polys.split(",") if s.strip())

    st = load(args.struct_json)["Entities"]
    ar = load(args.arch_json)["Entities"]
    base = os.path.splitext(os.path.basename(args.arch_json))[0]

    cats = load(os.path.join("output", f"{base}_layer_classification.json"))["categories"]
    a_anchor_ly = {ly for ly, c in cats.items() if c in ("wall_struct", "column_struct")}

    # ---- 앵커 선분 ----
    # 구조도(S2)는 슬래브 아래 부재라 전체가 은선(HID)으로 작도 — 실선 필터를 걸면
    # 전멸하므로 구조 쪽은 라인타입 무관, 건축 쪽만 실선(타층 투영 제외).
    s_anchor = segs_of(st, anchor_ly, contains=True)
    a_anchor = segs_of(ar, a_anchor_ly, solid_only=True)
    print(f"앵커: 구조 {len(s_anchor)}선 ({args.anchor}) · 건축 {len(a_anchor)}선 (구조벽+기둥 실선)")

    # ---- 평행이동 투표 (축 분리, 5mm 빈) ----
    dx, dy = estimate_translation(s_anchor, a_anchor)
    n1, ratio = exact_match(s_anchor, a_anchor, dx, dy)
    print(f"정합 오프셋: dx={dx:+.0f} dy={dy:+.0f} · 1mm 정확일치 {n1}개 ({ratio:.1%})")

    # ---- 검증 ②③: 주동표시 IoU + 보 끝점 기둥 안착 ----
    rooms = load(os.path.join("output", f"{base}_rooms_rect.json"))["rooms"]
    arb = (min(r["rect"][0] for r in rooms), min(r["rect"][1] for r in rooms),
           max(r["rect"][2] for r in rooms), max(r["rect"][3] for r in rooms))
    fps = []
    for e in st:
        if e.get("Layer") == "S07-주동 표시" and e.get("Type") == "Polyline":
            v = e["Verts"]
            fps.append((min(p[0] for p in v), min(p[1] for p in v),
                        max(p[0] for p in v), max(p[1] for p in v)))

    def iou(b1, b2):
        ox = min(b1[2], b2[2]) - max(b1[0], b2[0])
        oy = min(b1[3], b2[3]) - max(b1[1], b2[1])
        if ox <= 0 or oy <= 0:
            return 0.0
        inter = ox * oy
        return inter / ((b1[2] - b1[0]) * (b1[3] - b1[1])
                        + (b2[2] - b2[0]) * (b2[3] - b2[1]) - inter)

    iou_v = max((iou((f[0] + dx, f[1] + dy, f[2] + dx, f[3] + dy), arb)
                 for f in fps), default=0.0)

    RM = 20000.0
    rb = (arb[0] - RM, arb[1] - RM, arb[2] + RM, arb[3] + RM)
    a_col_ly = {ly for ly, c in cats.items() if c == "column_struct"}
    a_cols = [(c[0], c[1]) for c in fg.cluster_seeds(segs_of(ar, a_col_ly), tol=600)
              if rb[0] <= c[0] <= rb[2] and rb[1] <= c[1] <= rb[3]]
    beam_lines = segs_of(st, beam_line_ly, contains=True)
    n_seat = n_end = 0
    for x1, y1, x2, y2 in beam_lines:
        for px, py in ((x1 + dx, y1 + dy), (x2 + dx, y2 + dy)):
            if rb[0] <= px <= rb[2] and rb[1] <= py <= rb[3]:
                n_end += 1
                if any(math.hypot(px - cx, py - cy) < 1000 for cx, cy in a_cols):
                    n_seat += 1
    print(f"검증: 주동표시 IoU {iou_v:.2f} · 보 끝점 기둥 1m 안착 {n_seat}/{n_end}")

    # ---- 보 변환 + 클립 — 건축 방 영역 + 여유(기본 5m)로 좁게.
    # (정합 검증 창(rb, 20m)과 별개 — 도면에서 멀리 떨어진 주차장 보는 표시 안 함)
    CM = args.clip_margin
    rc = (arb[0] - CM, arb[1] - CM, arb[2] + CM, arb[3] + CM)

    def keep(x, y):
        return rc[0] <= x <= rc[2] and rc[1] <= y <= rc[3]

    beam_segs = []
    for x1, y1, x2, y2 in beam_lines:
        X1, Y1, X2, Y2 = x1 + dx, y1 + dy, x2 + dx, y2 + dy
        if keep(X1, Y1) or keep(X2, Y2):
            beam_segs.append([round(X1), round(Y1), round(X2), round(Y2)])
    beam_polys = []
    for e in st:
        if e.get("Type") != "Polyline":
            continue
        if not any(k in (e.get("Layer") or "") for k in beam_poly_ly):
            continue
        v = [[p[0] + dx, p[1] + dy] for p in e["Verts"]]
        if any(keep(x, y) for x, y in v):
            if e.get("Closed"):
                v.append(v[0])
            beam_polys.append([[round(x), round(y)] for x, y in v])
    print(f"보: 선분 {len(beam_segs)} · PC거더 폴리 {len(beam_polys)} "
          f"(건축 방영역+{CM/1000:.0f}m 클립"
          + (f" · 춤 {args.depth:.0f}mm" if args.depth else "") + ")")

    out = {"source_struct": os.path.basename(args.struct_json),
           "offset": [round(dx, 1), round(dy, 1)],
           "depth_mm": args.depth,
           "match": {"exact_1mm": n1, "exact_ratio": round(ratio, 4),
                     "footprint_iou": round(iou_v, 3),
                     "beam_end_on_col": f"{n_seat}/{n_end}"},
           "segs": beam_segs, "polys": beam_polys}
    op = os.path.join("output", f"{base}_beams.json")
    json.dump(out, open(op, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"출력: {op}")


if __name__ == "__main__":
    main()
