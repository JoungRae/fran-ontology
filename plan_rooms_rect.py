"""
평면도 방 영역(직사각형) 추출 — 실명 씨앗 → 상하좌우 벽까지 광선 캐스팅. (개선판)

원본(fran_consist_cad_json/plan_rooms_rect.py) 대비 개선:
  1. 벽 경계 = GPT 레이어 분류(wall_struct/wall_nonstruct/window/elevator/stair)
     — 이름 패턴만으론 A-ELEV(승강기 샤프트) 벽을 못 봐 ELEV.홀 인식이 실패했음
  2. 중복 씨앗 병합 (같은 텍스트가 수십 mm 간격으로 2회 찍히는 CAD 관행)
  3. ROOM_KW 에 '로비' 추가
  4. 실패 씨앗은 문(door) 레이어까지 경계로 포함해 재시도 (로비 정면=유리문)
  5. (--mirror 옵트인) 미러 세대 방 보완 — 단, 이 도면(5BL 55A/55AS)은 검증 결과
     모든 세대가 코어 중심으로 완전 라벨돼 있어 불필요 (방 배치 덤프로 확인:
     거실 104,037↔117,557 등 전 실이 코어 x=110,797 대칭). '현관 8개'는
     세대당 라벨 2개 중복이었음 → 씨앗 병합 반경 1500mm 로 해결. 세대는 층당 4개.

사용법: python plan_rooms_rect.py "data/<평면도>.json" [--cls <layer_classification.json>]
        (--cls 생략 시 output/<입력명>_layer_classification.json 자동 탐색)
"""

import argparse
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOM_KW = ['거실', '주방', '식당', '침실', '욕실', '화장실', '발코니', '현관',
           '드레스', '팬트리', '복도', '계단', '홀', '다용도', '알파', '파우더',
           '부부', '대피', '실외기', '로비',
           # 지하층 시설 실 (B동 지하 등)
           '통신', '제연휀', '창고', '전기실', '기계실', '펌프실', '방재실', 'PIT']
# 미러 보완에서 제외할 공용/순환 공간
COMMON_KW = ('홀', '로비', '계단', '복도', 'ELEV', 'EPS', 'TPS', 'PIT')
WALL_HINT = ('ST-CONC', 'WALL', 'A-ST', 'A-CON', 'WIN')       # 분류 없을 때 폴백
CLS_WALL_CATS = ("wall_struct", "wall_nonstruct", "window", "elevator", "stair")
CLS_RETRY_CATS = CLS_WALL_CATS + ("door",)   # 실패 씨앗 재시도용(로비 유리문 등)

CAP = 9000.0          # 광선 최대 사거리(mm)
EPS = 50.0
AX = 2.0              # 축정렬 판정 허용오차(mm)
SEED_DEDUP = 1500.0   # 같은 텍스트 중복 병합 거리(mm) — 현관·실외기실 이중 라벨 관행
MIN_OVL = 200.0       # 벽이 면과 이만큼 겹쳐야 경계로 인정(문틈 무시)
MIRROR_PAIR_MAX = 6000.0    # 현관 쌍 최대 간격 → 대칭축
MIRROR_RANGE = 12000.0      # 방이 축에서 이 이내여야 미러 대상
MIRROR_OVL = 0.4            # 기존 방과 이 이상 겹치면 이미 존재로 간주


def norm(t):
    return ''.join(str(t).split())


def load_wall_layers(cls_path, cats):
    c = json.load(open(cls_path, encoding="utf-8"))["categories"]
    return {ly for ly, cat in c.items() if cat in cats}


def _wall_edges(ents, wall_layers=None):
    """벽 경계 레이어의 축정렬 선분 → 수직(x,y0,y1)·수평(y,x0,x1) 목록."""
    def iswall(ly):
        if wall_layers is not None:
            return ly in wall_layers
        u = str(ly).upper()
        return any(h in u for h in WALL_HINT)

    vseg, hseg = [], []

    def add(ax, ay, bx, by):
        if abs(bx - ax) < AX and abs(by - ay) > 1:
            vseg.append((ax, min(ay, by), max(ay, by)))
        elif abs(by - ay) < AX and abs(bx - ax) > 1:
            hseg.append((ay, min(ax, bx), max(ax, bx)))
    for e in ents:
        t = e.get("Type")
        if t == "Line" and iswall(e.get("Layer")):
            a, b = e["Start"], e["End"]
            add(a[0], a[1], b[0], b[1])
        elif t == "Polyline" and iswall(e.get("Layer")):
            v = e["Verts"]
            for i in range(len(v) - 1):
                add(v[i][0], v[i][1], v[i + 1][0], v[i + 1][1])
    return vseg, hseg


def grow_rect(sx, sy, vlines, hlines):
    """글자 중심에서 사방으로 키우다 벽을 만나면 멈춤(최대 직사각형)."""
    def pt():
        U = min((y for y, x0, x1 in hlines if y > sy + EPS and x0 - 1 <= sx <= x1 + 1 and y - sy < CAP), default=None)
        D = max((y for y, x0, x1 in hlines if y < sy - EPS and x0 - 1 <= sx <= x1 + 1 and sy - y < CAP), default=None)
        R = min((x for x, y0, y1 in vlines if x > sx + EPS and y0 - 1 <= sy <= y1 + 1 and x - sx < CAP), default=None)
        L = max((x for x, y0, y1 in vlines if x < sx - EPS and y0 - 1 <= sy <= y1 + 1 and sx - x < CAP), default=None)
        return L, R, D, U
    L, R, D, U = pt()
    if None in (L, R, D, U):
        return None
    for _ in range(6):
        U2 = min((y for y, x0, x1 in hlines if y > sy + EPS and y - sy < CAP
                  and min(x1, R) - max(x0, L) > MIN_OVL), default=U)
        D2 = max((y for y, x0, x1 in hlines if y < sy - EPS and sy - y < CAP
                  and min(x1, R) - max(x0, L) > MIN_OVL), default=D)
        R2 = min((x for x, y0, y1 in vlines if x > sx + EPS and x - sx < CAP
                  and min(y1, U2) - max(y0, D2) > MIN_OVL), default=R)
        L2 = max((x for x, y0, y1 in vlines if x < sx - EPS and sx - x < CAP
                  and min(y1, U2) - max(y0, D2) > MIN_OVL), default=L)
        if (L2, R2, D2, U2) == (L, R, D, U):
            break
        L, R, D, U = L2, R2, D2, U2
    return [L, R, D, U]


def clip_neighbors(rects):
    """겹침 제거: 한 사각형이 이웃 씨앗을 삼키면 두 씨앗 중점에서 경계 클립."""
    for i, (_, sx, sy, r) in enumerate(rects):
        for j, (_, ox, oy, _) in enumerate(rects):
            if i == j:
                continue
            if r[2] < oy < r[3]:
                if sx < ox < r[1]:
                    r[1] = min(r[1], (sx + ox) / 2)
                if r[0] < ox < sx:
                    r[0] = max(r[0], (sx + ox) / 2)
            if r[0] < ox < r[1]:
                if sy < oy < r[3]:
                    r[3] = min(r[3], (sy + oy) / 2)
                if r[2] < oy < sy:
                    r[2] = max(r[2], (sy + oy) / 2)
    return rects


def resolve_overlaps(rects, passes=4):
    """남은 겹침: 겹치는 쌍을 침투 작은 축에서 두 씨앗 중점으로 분리."""
    for _ in range(passes):
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                _, sxi, syi, ri = rects[i]
                _, sxj, syj, rj = rects[j]
                ox = min(ri[1], rj[1]) - max(ri[0], rj[0])
                oy = min(ri[3], rj[3]) - max(ri[2], rj[2])
                if ox <= 0 or oy <= 0:
                    continue
                if ox <= oy:
                    m = (sxi + sxj) / 2 if sxi != sxj else \
                        (max(ri[0], rj[0]) + min(ri[1], rj[1])) / 2
                    if sxi < sxj:
                        ri[1] = min(ri[1], m); rj[0] = max(rj[0], m)
                    else:
                        rj[1] = min(rj[1], m); ri[0] = max(ri[0], m)
                else:
                    m = (syi + syj) / 2 if syi != syj else \
                        (max(ri[2], rj[2]) + min(ri[3], rj[3])) / 2
                    if syi < syj:
                        ri[3] = min(ri[3], m); rj[2] = max(rj[2], m)
                    else:
                        rj[3] = min(rj[3], m); ri[2] = max(ri[2], m)
    return rects


SNAP_REACH = 1500.0   # 변에서 벽면 군집 탐색 거리(클립 중점→실제 벽 회복)
SNAP_CLUSTER = 260.0  # 벽 양면을 한 군집으로 묶는 간격 (section_analyze.measure_widths 관례)
SNAP_IN = 60.0        # 변 안쪽 허용(이미 벽면에 있으면 유지)


def snap_edges(rects, vlines, hlines):
    """rect 변을 실제 벽면 군집에 스냅 — 단면 정합성(measure_widths)의 면군집 관례 이식.

    이웃 씨앗 중점 클립으로 생긴 가짜 경계를, 변 바깥 SNAP_REACH 이내의
    벽면 군집(≤260mm=벽 하나)의 '방 쪽 면'으로 되돌린다(안목치수).
    군집이 없으면(개방 공간·LDK) 클립 경계 유지. 반환: 스냅된 변 수.
    """
    def faces(lines, lo, hi, span_lo, span_hi):
        """[lo,hi] 구간의 면 좌표들(변 스팬과 충분히 겹치는 것만)."""
        need = min(500.0, 0.5 * (span_hi - span_lo))
        return sorted(c for c, s0, s1 in lines
                      if lo <= c <= hi and min(s1, span_hi) - max(s0, span_lo) > need)

    def cluster_near(vals, from_low):
        """면 좌표들을 260mm 군집으로 → 변에서 가장 가까운 군집 (min,max)."""
        if not vals:
            return None
        groups = [[vals[0], vals[0]]]
        for v in vals[1:]:
            if v - groups[-1][1] <= SNAP_CLUSTER:
                groups[-1][1] = v
            else:
                groups.append([v, v])
        return groups[0] if from_low else groups[-1]

    n = 0
    for t, sx, sy, r in rects:
        L, R, D, U = r
        # 오른쪽 변: 바깥 [R-SNAP_IN, R+REACH] 최근접 군집의 안쪽 면(min)
        g = cluster_near(faces(vlines, R - SNAP_IN, R + SNAP_REACH, D, U), True)
        if g and abs(g[0] - R) > 1:
            r[1] = g[0]; n += 1
        # 왼쪽 변
        g = cluster_near(faces(vlines, L - SNAP_REACH, L + SNAP_IN, D, U), False)
        if g and abs(g[1] - L) > 1:
            r[0] = g[1]; n += 1
        # 위 변
        g = cluster_near(faces(hlines, U - SNAP_IN, U + SNAP_REACH, r[0], r[1]), True)
        if g and abs(g[0] - U) > 1:
            r[3] = g[0]; n += 1
        # 아래 변
        g = cluster_near(faces(hlines, D - SNAP_REACH, D + SNAP_IN, r[0], r[1]), False)
        if g and abs(g[1] - D) > 1:
            r[2] = g[1]; n += 1
    return n


def collect_seeds(ents):
    """실명 텍스트 씨앗 수집 + 중복 병합."""
    raw = []
    for e in ents:
        if e.get("Type") == "DBText":
            t = norm(e.get("Text", ""))
            if any(k in t for k in ROOM_KW) and len(t) <= 8 \
                    and not t.endswith("층"):   # '로비층' 등 층 라벨 제외
                p = e.get("Pos")
                raw.append((t, p[0], p[1]))
    merged = []
    dropped = 0
    for t, x, y in raw:
        dup = next((m for m in merged if m[0] == t
                    and abs(m[1] - x) < SEED_DEDUP and abs(m[2] - y) < SEED_DEDUP), None)
        if dup:
            dropped += 1
            continue
        merged.append((t, x, y))
    return merged, dropped


def _ovl_ratio(a, b):
    """rect [L,R,D,U] 두 개의 겹침 / 작은쪽 면적."""
    ox = min(a[1], b[1]) - max(a[0], b[0])
    oy = min(a[3], b[3]) - max(a[2], b[2])
    if ox <= 0 or oy <= 0:
        return 0.0
    amin = min((a[1] - a[0]) * (a[3] - a[2]), (b[1] - b[0]) * (b[3] - b[2]))
    return (ox * oy) / max(amin, 1.0)


def mirror_complete(rects):
    """미러 세대 보완: 현관 쌍의 중점 = 대칭축 → 라벨 있는 실을 반사 복제.

    - 대상: 세대 내부 실만 (공용부 COMMON_KW 제외)
    - 반사 위치에 이미 방이 있으면(겹침 > MIRROR_OVL) 생략
    """
    # 대칭축 = 현관 '사각형 중심' 쌍의 중점. (텍스트 위치는 미러 세대에서도
    # 읽기 방향으로 찍혀 좌우 대칭이 아니므로 축이 비뚤어진다 — 반드시 rect 기준)
    ent_x = sorted((r[0] + r[1]) / 2 for t, sx, sy, r in rects if '현관' in t)
    axes = []
    i = 0
    while i + 1 < len(ent_x):
        if ent_x[i + 1] - ent_x[i] < MIRROR_PAIR_MAX:
            axes.append((ent_x[i] + ent_x[i + 1]) / 2)
            i += 2
        else:
            i += 1
    if not axes:
        return rects, 0

    added = []
    for t, sx, sy, r in list(rects):
        if any(k in t for k in COMMON_KW):
            continue
        ax = min(axes, key=lambda a: abs(a - sx))
        if abs(ax - sx) > MIRROR_RANGE:
            continue
        mr = [2 * ax - r[1], 2 * ax - r[0], r[2], r[3]]
        msx = 2 * ax - sx
        if any(_ovl_ratio(mr, o[3]) > MIRROR_OVL for o in rects + added):
            continue
        added.append([t, msx, sy, mr])
    return rects + added, len(added)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--cls", default="", help="layer_classification.json (벽 경계 인식)")
    ap.add_argument("--mirror", action="store_true",
                    help="미러 세대 보완(라벨 생략 도면용 — 이 도면엔 불필요, 옵트인)")
    ap.add_argument("--no-clip", action="store_true", help="겹침 제거 생략")
    args = ap.parse_args()

    base = os.path.splitext(os.path.basename(args.input))[0]
    cls_path = args.cls or os.path.join("output", f"{base}_layer_classification.json")
    d = json.load(open(args.input, encoding="utf-8"))
    ents = d["Entities"]

    if os.path.exists(cls_path):
        wl = load_wall_layers(cls_path, CLS_WALL_CATS)
        wl_retry = load_wall_layers(cls_path, CLS_RETRY_CATS)
        print(f"벽 경계 = 분류 기반 {len(wl)}개 레이어 (재시도용 +door {len(wl_retry)})")
    else:
        wl = wl_retry = None
        print("분류 파일 없음 → 이름 패턴 폴백")

    vlines, hlines = _wall_edges(ents, wl)
    print(f"벽선 수직 {len(vlines)} 수평 {len(hlines)}")

    seeds, ndup = collect_seeds(ents)
    print(f"방 씨앗 {len(seeds)}개 (중복 {ndup}개 병합)")

    rects = []
    failed = []
    for t, sx, sy in seeds:
        r = grow_rect(sx, sy, vlines, hlines)
        if r:
            rects.append([t, sx, sy, r])
        else:
            failed.append((t, sx, sy))
    # 실패 씨앗: 문 레이어까지 경계에 포함해 재시도 (로비 정면 유리문 등)
    n_retry = 0
    if failed and wl_retry is not None:
        v2, h2 = _wall_edges(ents, wl_retry)
        for t, sx, sy in failed:
            r = grow_rect(sx, sy, v2, h2)
            if r:
                rects.append([t, sx, sy, r])
                n_retry += 1
    print(f"영역 생성 {len(rects)}/{len(seeds)} (문 포함 재시도 성공 {n_retry})")

    if not args.no_clip:
        rects = clip_neighbors(rects)
        rects = resolve_overlaps(rects)
        # 클립 경계를 실제 벽면으로 회복 (단면 정합성 face-cluster 관례)
        n_snap = snap_edges(rects, vlines, hlines)
        rects = resolve_overlaps(rects)
        print(f"벽면 스냅 {n_snap}개 변 보정")

    if args.mirror:
        rects, n_mirror = mirror_complete(rects)
        if n_mirror:
            rects = resolve_overlaps(rects)
        print(f"미러 세대 보완 +{n_mirror}개")

    out = [{"room": t, "seed": [round(sx), round(sy)],
            "rect": [round(r[0]), round(r[2]), round(r[1]), round(r[3])],
            "w_mm": round(r[1] - r[0]), "h_mm": round(r[3] - r[2])}
           for t, sx, sy, r in rects]
    os.makedirs("output", exist_ok=True)
    op = os.path.join("output", f"{base}_rooms_rect.json")
    json.dump({"source": os.path.basename(args.input), "rooms": out},
              open(op, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    import collections
    print("실명 분포:", dict(collections.Counter(r["room"] for r in out)))
    print(f"출력: {op}")


if __name__ == "__main__":
    main()
