"""
평면도 방 영역 flood-fill 폴리곤 추출 — compare_facility.pit_regions() 이식.

직사각형(plan_rooms_rect) 대비:
  - 경계 = 분류 카테고리 {벽(구조/비구조)·기둥·창호·문} → **문이 방을 닫는다**
  - 100mm 격자 래스터화 + 1셀 팽창(작은 틈 봉합) → 씨앗 BFS flood-fill
  - 계단: 라벨 없어도 geometry 군집(cluster_seeds)으로 자동 씨앗
  - 엘리베이터: flood 대신 A-ELEV bbox 로 고정 + 셀 선점(문틈 유출·이웃 침범 차단)
  - 그리드 끝에 닿으면 열린 공간 → 제외. contourpy 로 실제 방 모양 폴리곤 추출.

출력: output/<base>_rooms_rect.json (기존 스키마 호환: room/seed/rect/w_mm/h_mm
      + 확장: poly(폴리곤 꼭짓점), area_m2(폴리곤 실면적))

사용법(numpy·contourpy 필요 → fran_consist_cad_json venv 로 실행):
  python plan_rooms_flood.py "data/<평면도>.json" [--cls <layer_classification.json>]
"""

import argparse
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SP = r"D:/Python_test/fran_consist_cad_json"
sys.path.insert(0, SP)
import facility_geom as fg   # cluster_seeds 재사용 (순수 기하, IO 없음)

BOUNDARY_CATS = {"wall_struct", "wall_nonstruct", "column_struct", "window", "door"}
CELL = 100.0
MARGIN = 12000.0
ELEV_MIN = 800.0     # 실제 샤프트 판정 최소 변(슬리버 제외)
# 씨앗 = 룸 이름 글씨 크기대의 '모든' 라벨 (원본 pit_regions 관례 — 키워드 필터 없음).
# 사우나·키즈짐·저수조 등 예측 못한 실명도 잡힌다. 표제란·주차 라벨 등 쓰레기 씨앗은
# flood 가 자체 필터(열린공간=그리드 끝 도달→제외, 벽 위→skip, 기존 방 안→also 병합).
ROOM_MIN_H, ROOM_MAX_H = 200.0, 600.0   # 샤워실·EPS 등=200, 동번호 1440↑ 제외
LABEL_SKIP = {"UP", "DN"}               # 계단 방향 표시 — 방 이름 아님


def norm(t):
    return ''.join(str(t).split())


def load_segs(ents, layers):
    out = []
    for e in ents:
        if e.get("Layer") not in layers:
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


def poly_area_m2(poly):
    s = 0.0
    for i in range(len(poly) - 1):
        s += poly[i][0] * poly[i + 1][1] - poly[i + 1][0] * poly[i][1]
    return abs(s) / 2 / 1e6


def flood_rooms(ents, cats):
    import numpy as np
    import contourpy
    from collections import deque

    lay = lambda want: {ly for ly, c in cats.items() if c in want}
    bound = load_segs(ents, lay(BOUNDARY_CATS))
    elev_cl = fg.cluster_seeds(load_segs(ents, lay({"elevator"})))
    stair = load_segs(ents, lay({"stair"}))

    # 씨앗: 룸 이름 크기대(200~600)의 모든 텍스트 라벨 + 라벨 없는 계단 군집
    seeds = []
    for e in ents:
        if e.get("Type") not in ("DBText", "MText"):
            continue
        t = str(e.get("Text", "")).strip()
        h = e.get("Height", 0) or 0
        if not t or not (ROOM_MIN_H <= h <= ROOM_MAX_H) or t.upper() in LABEL_SKIP:
            continue
        p = e.get("Pos") or e.get("InsertionPoint") or [0, 0]
        seeds.append((norm(t), p[0], p[1], h))
    seeds.sort(key=lambda s: -s[3])                 # 큰 글씨 우선(룸이름 먼저 명명)
    seeds = [(t, x, y) for t, x, y, _ in seeds]
    seeds += [("계단", cx, cy) for cx, cy, *_ in fg.cluster_seeds(stair)]
    if not seeds and not elev_cl:
        return []

    xs = [s[1] for s in seeds] + [c[0] for c in elev_cl]
    ys = [s[2] for s in seeds] + [c[1] for c in elev_cl]
    x0, y0 = min(xs) - MARGIN, min(ys) - MARGIN
    x1, y1 = max(xs) + MARGIN, max(ys) + MARGIN
    W = int((x1 - x0) / CELL) + 2
    H = int((y1 - y0) / CELL) + 2

    blk = np.zeros((H, W), dtype=bool)
    for a, b, c, d in bound:
        if max(a, c) < x0 or min(a, c) > x1 or max(b, d) < y0 or min(b, d) > y1:
            continue
        steps = int(max(abs(c - a), abs(d - b)) / CELL) + 1
        for k in range(steps + 1):
            t = k / steps
            gx = int((a + (c - a) * t - x0) / CELL)
            gy = int((b + (d - b) * t - y0) / CELL)
            if 0 <= gx < W and 0 <= gy < H:
                blk[gy, gx] = True
    dil = blk.copy()
    dil[1:, :] |= blk[:-1, :]; dil[:-1, :] |= blk[1:, :]
    dil[:, 1:] |= blk[:, :-1]; dil[:, :-1] |= blk[:, 1:]
    blk = dil

    gx_axis = x0 + (np.arange(W) + 0.5) * CELL
    gy_axis = y0 + (np.arange(H) + 0.5) * CELL
    state = np.zeros((H, W), dtype=np.int8)
    gen = np.zeros((H, W), dtype=np.int32)
    rooms = []

    def emit(name, lx, ly, poly):
        px = [p[0] for p in poly]; py = [p[1] for p in poly]
        rooms.append({
            "room": name, "seed": [round(lx), round(ly)],
            "rect": [round(min(px)), round(min(py)), round(max(px)), round(max(py))],
            "w_mm": round(max(px) - min(px)), "h_mm": round(max(py) - min(py)),
            "poly": [[round(p[0]), round(p[1])] for p in poly],
            "area_m2": round(poly_area_m2(poly), 2),
        })

    # 엘리베이터: bbox 고정 + 셀 선점(이웃 방 flood 차단, 문틈 유출 방지)
    for cx, cy, ax0, ay0, ax1, ay1 in elev_cl:
        if min(ax1 - ax0, ay1 - ay0) < ELEV_MIN:
            continue
        gi0 = max(0, int((ax0 - x0) / CELL)); gi1 = min(W - 1, int((ax1 - x0) / CELL))
        gj0 = max(0, int((ay0 - y0) / CELL)); gj1 = min(H - 1, int((ay1 - y0) / CELL))
        state[gj0:gj1 + 1, gi0:gi1 + 1] = 1
        emit("ELEV.", cx, cy, [[ax0, ay0], [ax1, ay0], [ax1, ay1], [ax0, ay1], [ax0, ay0]])

    n_open = 0
    absorbed = []            # 이미 찬 영역에 떨어진 씨앗 → 라벨 병합 대상
    for gi, (name, cx, cy) in enumerate(seeds, 1):
        si = int((cx - x0) / CELL); sj = int((cy - y0) / CELL)
        if not (0 <= si < W and 0 <= sj < H) or blk[sj, si] or state[sj, si] != 0:
            absorbed.append((name, cx, cy))
            continue
        dq = deque([(sj, si)]); gen[sj, si] = gi
        cells = []; edge = False
        while dq:
            j, i = dq.popleft(); cells.append((j, i))
            if j == 0 or j == H - 1 or i == 0 or i == W - 1:
                edge = True
            for dj, di in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nj, ni = j + dj, i + di
                if 0 <= nj < H and 0 <= ni < W and gen[nj, ni] != gi \
                        and not blk[nj, ni] and state[nj, ni] == 0:
                    gen[nj, ni] = gi; dq.append((nj, ni))
        if edge:
            for j, i in cells:
                state[j, i] = -1
            n_open += 1
            continue
        mask = np.zeros((H, W))
        for j, i in cells:
            mask[j, i] = 1.0; state[j, i] = 1
        lines = contourpy.contour_generator(gx_axis, gy_axis, mask).lines(0.5)
        if not lines:
            continue
        poly = [[float(p[0]), float(p[1])] for p in max(lines, key=len)]
        emit(name, cx, cy, poly)

    # 흡수된 씨앗(문 없이 이어진 개방 공간: LDK 등) → 해당 영역에 라벨 병합
    n_merged = 0
    for name, cx, cy in absorbed:
        for r in rooms:
            x0r, y0r, x1r, y1r = r["rect"]
            if x0r <= cx <= x1r and y0r <= cy <= y1r \
                    and fg.point_in_poly(cx, cy, r["poly"]):
                if name != r["room"] and name not in r.setdefault("also", []):
                    r["also"].append(name)
                    n_merged += 1
                break
    print(f"열린 공간 제외 {n_open} · 흡수 씨앗 {len(absorbed)}(라벨 병합 {n_merged})")
    return rooms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--cls", default="")
    ap.add_argument("-o", "--output", default="")
    ap.add_argument("--merge-into-rect", action="store_true",
                    help="rect 파일에 flood 전용 공간(계단·ELEV. 등)을 병합")
    args = ap.parse_args()

    base = os.path.splitext(os.path.basename(args.input))[0]
    cls_path = args.cls or os.path.join("output", f"{base}_layer_classification.json")
    cats = json.load(open(cls_path, encoding="utf-8"))["categories"]
    ents = json.load(open(args.input, encoding="utf-8"))["Entities"]

    rooms = flood_rooms(ents, cats)

    import collections
    print(f"방 {len(rooms)}개:", dict(collections.Counter(r["room"] for r in rooms)))
    for n in ("거실", "침실1", "주방/식당", "ELEV.홀", "로비", "계단"):
        rs = [r for r in rooms if r["room"] == n]
        if rs:
            print(f"  {n:8} area={[r['area_m2'] for r in rs[:4]]}㎡")

    op = args.output or os.path.join("output", f"{base}_rooms_flood.json")
    os.makedirs(os.path.dirname(op), exist_ok=True)
    json.dump({"source": os.path.basename(args.input), "method": "floodfill",
               "rooms": rooms}, open(op, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"출력: {op}")

    if args.merge_into_rect:
        rp = os.path.join("output", f"{base}_rooms_rect.json")
        rect = json.load(open(rp, encoding="utf-8"))

        # ① 동명 실에 flood 폴리곤 부착 — rect 는 외접 사각형이라 ㄷ자 복도·홀의
        #    실제 형상을 모른다. 하류(헤드 배치 구역 분할 등)가 poly 를 우선 쓴다.
        def _ovl(a, b):
            ox = min(a[2], b[2]) - max(a[0], b[0])
            oy = min(a[3], b[3]) - max(a[1], b[1])
            if ox <= 0 or oy <= 0:
                return 0.0
            amin = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
            return ox * oy / max(amin, 1.0)

        used, n_poly = set(), 0
        for rm in rect["rooms"]:
            if rm.get("poly"):
                continue
            best = None
            for fi, fr in enumerate(rooms):
                if fi in used or fr["room"] != rm["room"] or not fr.get("poly"):
                    continue
                ov = _ovl(rm["rect"], fr["rect"])
                if ov > 0.3 and (best is None or ov > best[0]):
                    best = (ov, fi)
            if best:
                fr = rooms[best[1]]
                used.add(best[1])
                rm["poly"] = fr["poly"]
                rm["area_m2"] = fr["area_m2"]
                if fr.get("also"):
                    rm["also"] = fr["also"]
                n_poly += 1

        # ② flood 전용 공간(계단·ELEV. 등) 추가
        have = {r["room"] for r in rect["rooms"]}
        added = [r for r in rooms if r["room"] not in have]
        rect["rooms"] += added
        json.dump(rect, open(rp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"rect 병합: 폴리곤 부착 {n_poly}개 · 신규 +{len(added)}개 "
              f"({sorted({r['room'] for r in added})}) → {rp}")


if __name__ == "__main__":
    main()
