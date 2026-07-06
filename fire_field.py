"""
소방 배치 v2 기하 엔진 — 래스터 보행 거리장 + 커버리지 검증/수리 (순수 계산, IO 없음).

  · build_grid()      벽/창(통행 불가)·문(통행 가능) 래스터 + 건물 내부 판정
                      (외부 판정은 문까지 막고 플러드 — flood-fill 과 같은 의미론)
  · distance_field()  출발점(계단 출입구 등)에서 8방향 Dijkstra 보행 거리장(mm)
  · tri_lattice()     삼각(지그재그) 격자 — 원 커버링 최적 배열 (정방 대비 ~13% 절감)
  · repair_cover()    미커버 셀 최원점에 헤드 추가(greedy) → 전 셀 커버 '보증'
  · descend_path()    거리장 경사 하강 = 실제 개구부를 지나는 피난 경로
"""

import heapq
import math

import numpy as np

CELL = 100.0


def _raster(segs, x0, y0, W, H, blk):
    for a, b, c, d in segs:
        steps = int(max(abs(c - a), abs(d - b)) / CELL) + 1
        for k in range(steps + 1):
            t = k / steps
            gx = int((a + (c - a) * t - x0) / CELL)
            gy = int((b + (d - b) * t - y0) / CELL)
            if 0 <= gx < W and 0 <= gy < H:
                blk[gy, gx] = True


def _dilate(m):
    d = m.copy()
    d[1:, :] |= m[:-1, :]; d[:-1, :] |= m[1:, :]
    d[:, 1:] |= m[:, :-1]; d[:, :-1] |= m[:, 1:]
    return d


def build_grid(wall_segs, door_segs, bounds, margin=6000.0, carve_segs=None):
    """반환: dict(x0,y0,W,H, blk_wall(통행불가), walkable(내부∧통행가능)).

    carve_segs: 문 개구부 천공 선분(스윙호의 경첩→끝점 현 = 닫힘 위치 문짝 선).
    도면이 문 위치에서 벽선을 끊지 않는 경우 통행로를 외과적으로 뚫는다.
    (외부 밀폐 판정 blk_all 에는 영향 없음 — 문은 밀폐엔 유효)
    """
    minx, miny, maxx, maxy = bounds
    x0, y0 = minx - margin, miny - margin
    W = int((maxx - minx + 2 * margin) / CELL) + 2
    H = int((maxy - miny + 2 * margin) / CELL) + 2

    blk_wall = np.zeros((H, W), dtype=bool)
    _raster(wall_segs, x0, y0, W, H, blk_wall)
    blk_wall = _dilate(blk_wall)
    if carve_segs:
        carve = np.zeros((H, W), dtype=bool)
        _raster(carve_segs, x0, y0, W, H, carve)
        carve = _dilate(_dilate(carve))     # 팽창된 벽(±1셀)까지 뚫도록 2회 팽창
        blk_wall &= ~carve

    blk_all = blk_wall.copy()          # 외부 판정용: 문까지 막는다 (밀폐 의미론)
    _raster(door_segs, x0, y0, W, H, blk_all)
    blk_all = _dilate(blk_all)

    # 외부 = 격자 가장자리에서 blk_all 아닌 셀로 플러드
    outside = np.zeros((H, W), dtype=bool)
    from collections import deque
    dq = deque()
    for i in range(W):
        for j in (0, H - 1):
            if not blk_all[j, i]:
                outside[j, i] = True
                dq.append((j, i))
    for j in range(H):
        for i in (0, W - 1):
            if not blk_all[j, i] and not outside[j, i]:
                outside[j, i] = True
                dq.append((j, i))
    while dq:
        j, i = dq.popleft()
        for dj, di in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nj, ni = j + dj, i + di
            if 0 <= nj < H and 0 <= ni < W and not outside[nj, ni] \
                    and not blk_all[nj, ni]:
                outside[nj, ni] = True
                dq.append((nj, ni))

    walkable = (~blk_wall) & (~outside)
    return {"x0": x0, "y0": y0, "W": W, "H": H,
            "blk_wall": blk_wall, "walkable": walkable}


def to_cell(g, x, y):
    return int((y - g["y0"]) / CELL), int((x - g["x0"]) / CELL)


def to_xy(g, j, i):
    return g["x0"] + (i + 0.5) * CELL, g["y0"] + (j + 0.5) * CELL


def snap(g, x, y, reach=15):
    """(x,y) 최근접 walkable 셀."""
    j0, i0 = to_cell(g, x, y)
    best = None
    for dj in range(-reach, reach + 1):
        for di in range(-reach, reach + 1):
            j, i = j0 + dj, i0 + di
            if 0 <= j < g["H"] and 0 <= i < g["W"] and g["walkable"][j, i]:
                d = dj * dj + di * di
                if best is None or d < best[0]:
                    best = (d, j, i)
    return (best[1], best[2]) if best else None


_NB = [(1, 0, CELL), (-1, 0, CELL), (0, 1, CELL), (0, -1, CELL),
       (1, 1, CELL * 2 ** .5), (1, -1, CELL * 2 ** .5),
       (-1, 1, CELL * 2 ** .5), (-1, -1, CELL * 2 ** .5)]


def distance_field(g, sources_xy):
    """출발점들에서 walkable 셀 전체까지 8방향 Dijkstra 보행 거리(mm). inf=미도달."""
    dist = np.full((g["H"], g["W"]), np.inf)
    pq = []
    for x, y in sources_xy:
        c = snap(g, x, y)
        if c:
            dist[c] = 0.0
            heapq.heappush(pq, (0.0, c[0], c[1]))
    wk = g["walkable"]
    while pq:
        d, j, i = heapq.heappop(pq)
        if d > dist[j, i]:
            continue
        for dj, di, w in _NB:
            nj, ni = j + dj, i + di
            if 0 <= nj < g["H"] and 0 <= ni < g["W"] and wk[nj, ni]:
                nd = d + w
                if nd < dist[nj, ni]:
                    dist[nj, ni] = nd
                    heapq.heappush(pq, (nd, nj, ni))
    return dist


def descend_path(g, dist, x, y, step_keep=4):
    """(x,y)에서 거리장 경사 하강 → 실제 개구부를 지나는 경로 좌표열."""
    c = snap(g, x, y)
    if not c or not np.isfinite(dist[c]):
        return []
    path = [to_xy(g, *c)]
    j, i = c
    k = 0
    while dist[j, i] > 0:
        best = None
        for dj, di, _ in _NB:
            nj, ni = j + dj, i + di
            if 0 <= nj < g["H"] and 0 <= ni < g["W"] \
                    and dist[nj, ni] < (best[0] if best else dist[j, i]):
                best = (dist[nj, ni], nj, ni)
        if not best:
            break
        j, i = best[1], best[2]
        k += 1
        if k % step_keep == 0:
            path.append(to_xy(g, j, i))
    path.append(to_xy(g, j, i))
    return path


def tri_lattice(x0, y0, x1, y1, r):
    """방 중심 균등 분할 + 홀수행 반주기 지그재그 — 헤드가 벽에 붙지 않게 셀 중앙 배치.

    행/열 수 = ceil(치수 / 이론 간격(a=r√3, dy=1.5r)). 엄밀 커버는 repair 가 보증.
    """
    a = r * math.sqrt(3) * 0.97
    dy = a * math.sqrt(3) / 2
    w, h = x1 - x0, y1 - y0
    ny = max(1, math.ceil(h / dy))
    pts = []
    for iy in range(ny):
        cy = y0 + h * (iy + 0.5) / ny
        nx = max(1, math.ceil(w / a))
        for ix in range(nx):
            fx = (ix + 0.5 + (0.25 if (iy % 2 and nx > 1) else 0.0)) % nx
            pts.append((x0 + w * fx / nx, cy))
    return pts


def los_free(g, hx, hy, cxs, cys, samples=24):
    """헤드(hx,hy)→셀들 직선 가시선: 벽(blk_wall)을 안 지나면 True.

    양 끝 6%/3% 는 제외(헤드·셀 자신이 팽창 벽에 물려 있어도 무효화되지 않게).
    """
    ts = np.linspace(0.06, 0.97, samples)[:, None]
    px = hx + (cxs[None, :] - hx) * ts
    py = hy + (cys[None, :] - hy) * ts
    jj = ((py - g["y0"]) / CELL).astype(int).clip(0, g["H"] - 1)
    ii = ((px - g["x0"]) / CELL).astype(int).clip(0, g["W"] - 1)
    return ~g["blk_wall"][jj, ii].any(axis=0)


def zone_cover(g, zone_idx, r, pre=None, stride=3, max_heads=300, avoid=None):
    """구역(셀 집합) 전체를 greedy 최대커버로 덮는 헤드 배치 — v3 핵심.

    격자+수리 2단계의 문제(격자가 방 형상을 모르고 겹치게 깔림 → 수리가 덧댐)를
    없애기 위해, 빈 상태에서 시작해 '미커버 셀을 가장 많이 덮는 위치'를 반복
    선택한다. 커버 = 수평거리 ≤ r ∧ 가시선(LoS). 후보 = 구역 내 셀의
    stride(기본 300mm) 서브샘플 — 헤드는 자기 구역 천장에만 놓인다.
    pre = [(x,y,r), ...] 기 배치 헤드(창문 특칙 등)가 덮는 셀은 미리 제외.
    동률 후보는 미커버 무게중심에 가까운 쪽(구석 방지). 반환: [(x,y), ...]
    """
    N = len(zone_idx)
    if N == 0:
        return []
    cx = g["x0"] + (zone_idx[:, 1] + 0.5) * CELL
    cy = g["y0"] + (zone_idx[:, 0] + 0.5) * CELL
    covered = np.zeros(N, dtype=bool)
    for px, py, pr in (pre or []):
        d = np.hypot(cx - px, cy - py) <= pr
        if d.any():
            ok = los_free(g, px, py, cx[d], cy[d])
            covered[np.where(d)[0][ok]] = True

    if N > 20000:                      # 대공간(주차장 등)은 후보를 성기게
        stride = max(stride, 5)
    # avoid = 헤드 설치 금지 대역(보 살수장애 0.6m 등) — 커버 '대상'은 그대로,
    # 헤드 '위치' 후보에서만 제외한다.
    av = (avoid[zone_idx[:, 0], zone_idx[:, 1]] if avoid is not None
          else np.zeros(N, dtype=bool))
    sub = (zone_idx[:, 0] % stride == 0) & (zone_idx[:, 1] % stride == 0) & ~av
    if sub.any():
        cand = np.where(sub)[0]
    elif (~av).any():
        cand = np.where(~av)[0]
    else:
        cand = np.arange(N)            # 구역 전체가 금지 대역 — 15806 예외로 플래그됨
    covsets = []
    for k in cand:
        d = np.hypot(cx - cx[k], cy - cy[k]) <= r
        idx = np.where(d)[0]
        ok = los_free(g, cx[k], cy[k], cx[idx], cy[idx])
        covsets.append(idx[ok])

    heads = []
    while not covered.all() and len(heads) < max_heads:
        gains = np.fromiter((np.count_nonzero(~covered[s]) for s in covsets),
                            dtype=np.int64, count=len(covsets))
        b = int(gains.argmax())
        if gains[b] == 0:
            # 서브샘플 후보가 못 닿는 포켓 → 그 포켓을 덮을 수 있는 비금지 셀 우선,
            # 없으면 미커버 셀 자신을 후보로 승격
            k = int(np.argmax(~covered))
            d_k = np.hypot(cx - cx[k], cy - cy[k])
            opts = np.where((d_k <= r) & ~av)[0]
            if len(opts):
                ok0 = los_free(g, cx[k], cy[k], cx[opts], cy[opts])
                opts = opts[ok0]
            k2 = int(opts[np.argmin(d_k[opts])]) if len(opts) else k
            d = np.hypot(cx - cx[k2], cy - cy[k2]) <= r
            idx = np.where(d)[0]
            ok = los_free(g, cx[k2], cy[k2], cx[idx], cy[idx])
            heads.append((float(cx[k2]), float(cy[k2])))
            covered[idx[ok]] = True
            covered[k] = True          # 자기 셀은 항상 커버 → 수렴 보장
            continue
        ties = np.where(gains == gains[b])[0]
        if len(ties) > 1:
            def _compact(t):
                s = covsets[t]
                u = s[~covered[s]]
                return math.hypot(float(cx[u].mean()) - cx[cand[t]],
                                  float(cy[u].mean()) - cy[cand[t]])
            b = int(min(ties, key=_compact))
        k = cand[b]
        heads.append((float(cx[k]), float(cy[k])))
        covered[covsets[b]] = True
    return _recenter(g, cx, cy, heads, r, pre, av=av)


def _recenter(g, cx, cy, heads, r, pre, iters=4, av=None):
    """배치 마감(Lloyd 재정렬): 셀을 최근접 커버 헤드에 분담시키고, 각 헤드를
    자기 분담 영역의 1-중심(최대거리 최소 지점)으로 이동.

    greedy 가 고른 위치는 커버 수만 최적이라 간격이 들쭉날쭉하다(1.5/2.4/1.8m…).
    겹침 구간까지 최근접 헤드에 배분한 뒤 1-중심으로 옮기면 간격이 균등해지고
    코너·ㄷ자 복도에서도 중심선에 앉는다. 각 헤드는 이동 후에도 자기 분담 셀
    전체를 (거리+LoS 정밀 검증으로) 덮는 후보만 채택 — 분담의 합집합 = 전 셀
    이므로 커버 불변 보장.
    """
    for _ in range(iters):
        moved = False
        # 커버 집합 + 최근접 분담 (pre 헤드도 소유자 후보 — 창문 헤드 근처 셀은
        # 창문 헤드 몫이라 구역 헤드를 끌어당기지 않는다)
        owner = np.full(len(cx), -1, dtype=np.int64)
        bestd = np.full(len(cx), np.inf)
        sets = []
        for hi, (hx, hy) in enumerate(heads):
            d = np.hypot(cx - hx, cy - hy)
            idx = np.where(d <= r)[0]
            ok = los_free(g, hx, hy, cx[idx], cy[idx])
            s = idx[ok]
            sets.append(s)
            upd = d[s] < bestd[s]
            owner[s[upd]] = hi
            bestd[s[upd]] = d[s][upd]
        for px, py, pr in (pre or []):
            d = np.hypot(cx - px, cy - py)
            idx = np.where(d <= pr)[0]
            ok = los_free(g, px, py, cx[idx], cy[idx])
            s = idx[ok]
            upd = d[s] < bestd[s]
            owner[s[upd]] = -2          # pre 소유 — 구역 헤드 분담에서 제외
            bestd[s[upd]] = d[s][upd]
        for hi, (hx, hy) in enumerate(heads):
            u = np.where(owner == hi)[0]        # 이 헤드의 분담 셀
            if len(u) == 0:
                continue
            ux, uy = cx[u], cy[u]
            cur = float(np.hypot(ux - hx, uy - hy).max())
            ccx, ccy = float(ux.mean()), float(uy.mean())
            near = np.hypot(cx - ccx, cy - ccy) <= r
            if av is not None:
                near &= ~av            # 금지 대역(보 0.6m 등)으로는 이동하지 않음
            cand = np.where(near)[0]
            if len(cand) == 0:
                continue
            dmax = np.hypot(ux[None, :] - cx[cand][:, None],
                            uy[None, :] - cy[cand][:, None]).max(axis=1)
            keep = dmax <= r
            cand, dmax = cand[keep], dmax[keep]
            for t in np.argsort(dmax)[:6]:        # 1-중심 후보 상위만 정밀 검증
                if dmax[t] >= cur - 1.0:           # 개선 없으면 유지
                    break
                qx, qy = float(cx[cand[t]]), float(cy[cand[t]])
                if los_free(g, qx, qy, ux, uy).all():
                    heads[hi] = (qx, qy)
                    moved = True
                    break
        if not moved:
            break
    return heads


def prune_heads(g, cell_idx, heads, r_of, kind, keep=frozenset()):
    """전역 중복 제거: 담당 셀이 전부 다른 헤드로도 커버되는 헤드를 삭제.

    구역 경계·개방 연결(LDK)에서 이웃 구역 헤드와 커버가 완전히 겹치는 헤드가
    대상. keep 종류(창문 특칙 등 법정 위치)는 제거하지 않는다. 기여 작은
    헤드부터 시도해 제거 수를 최대화. 반환: (heads, r_of, kind, 제거 수)
    """
    if not heads:
        return heads, r_of, kind, 0
    cx = g["x0"] + (cell_idx[:, 1] + 0.5) * CELL
    cy = g["y0"] + (cell_idx[:, 0] + 0.5) * CELL
    sets = []
    for (hx, hy), rr in zip(heads, r_of):
        d = np.hypot(cx - hx, cy - hy) <= rr
        idx = np.where(d)[0]
        ok = los_free(g, hx, hy, cx[idx], cy[idx])
        sets.append(idx[ok])
    count = np.zeros(len(cx), dtype=np.int32)
    for s in sets:
        count[s] += 1
    alive = [True] * len(heads)
    order = sorted(range(len(heads)), key=lambda i: len(sets[i]))
    changed = True
    while changed:
        changed = False
        for i in order:
            if not alive[i] or kind[i] in keep:
                continue
            s = sets[i]
            if len(s) == 0 or (count[s] >= 2).all():
                alive[i] = False
                count[s] -= 1
                changed = True
    n_rm = alive.count(False)
    heads = [h for h, a in zip(heads, alive) if a]
    r_of = [r for r, a in zip(r_of, alive) if a]
    kind = [k for k, a in zip(kind, alive) if a]
    return heads, r_of, kind, n_rm


def repair_cover(g, cell_idx, heads_xy, r_of, add_r, max_add=120, avoid_mask=None):
    """미커버 셀의 최원점에 헤드를 추가(greedy)해 전 셀 커버를 보증.

    커버 조건 = 수평거리 ≤ r **그리고 가시선에 벽 없음(LoS)** — 옆방 헤드가
    벽 너머를 방호하는 오판을 막는다. 개방 연결(LDK 등)은 자연히 상호 커버.
    반환: (추가된 헤드 목록, 커버 후 최악 여유 mm)
    """
    if len(cell_idx) == 0:
        return [], 0.0
    cx = g["x0"] + (cell_idx[:, 1] + 0.5) * CELL
    cy = g["y0"] + (cell_idx[:, 0] + 0.5) * CELL

    def contrib(hx, hy, rr):
        d = np.hypot(cx - hx, cy - hy) - rr
        inside = d <= 0
        if inside.any():
            ok = los_free(g, hx, hy, cx[inside], cy[inside])
            t = d[inside]
            t[~ok] = np.inf          # 반경 안이라도 벽에 가리면 그 헤드론 커버 불가
            d[inside] = t
        return d

    best = np.full(len(cx), np.inf)
    for (hx, hy), rr in zip(heads_xy, r_of):
        np.minimum(best, contrib(hx, hy, rr), out=best)

    # 그리드 참조(후보 열거용)
    wk = g["walkable"]
    Hh, Ww = wk.shape

    def pick_position(kx, ky):
        """최원 미커버점 w=(kx,ky)를 덮을 수 있는 후보들 중 '미커버 셀을 가장
        많이 덮는' 위치 선택 (greedy max-coverage).

        w를 덮으려면 헤드는 반드시 [w 반경 add_r 내 ∧ w와 가시선 ∧ 보행가능]
        집합 안에 있어야 한다 — 그 집합을 300mm 간격으로 훑어 점수화한다.
        """
        # 후보 열거: w 주변 bbox의 walkable 셀, 3셀(300mm) 서브샘플
        rr_c = int(add_r / CELL) + 1
        j0, i0 = to_cell(g, kx, ky)
        js = np.arange(max(0, j0 - rr_c), min(Hh, j0 + rr_c + 1), 3)
        iis = np.arange(max(0, i0 - rr_c), min(Ww, i0 + rr_c + 1), 3)
        jj, ii = np.meshgrid(js, iis, indexing="ij")
        m = wk[jj, ii]
        if avoid_mask is not None:     # 금지 대역(보 0.6m 등) 후보 제외
            m &= ~avoid_mask[jj, ii]
        pxs = g["x0"] + (ii[m] + 0.5) * CELL
        pys = g["y0"] + (jj[m] + 0.5) * CELL
        near = np.hypot(pxs - kx, pys - ky) <= add_r * 0.95
        pxs, pys = pxs[near], pys[near]
        if len(pxs):
            # 주의: 정밀 검증(contrib, samples=24)과 반드시 같은 샘플수 —
            # 더 성기게 보면 '덮는 줄 알았던' 위치가 검증에서 탈락해 무한 반복
            vis = los_free(g, kx, ky, pxs, pys, samples=24)
            pxs, pys = pxs[vis], pys[vis]
        if len(pxs) == 0:
            return (kx, ky)          # 후보 없음 → 최원점 자체
        # 점수화 대상: 지역 미커버 셀(2×add_r 이내), 2셀 서브샘플
        loc = (best > 0) & (np.hypot(cx - kx, cy - ky) <= 2 * add_r)
        lx, ly = cx[loc][::2], cy[loc][::2]
        if len(lx) == 0:
            return (float(pxs[0]), float(pys[0]))
        best_score, best_p = -1, (kx, ky)
        for qx, qy in zip(pxs, pys):
            d_ok = np.hypot(lx - qx, ly - qy) <= add_r
            if not d_ok.any():
                continue
            score = int(los_free(g, qx, qy, lx[d_ok], ly[d_ok], samples=10).sum())
            if score > best_score:
                best_score, best_p = score, (float(qx), float(qy))
        return best_p

    added = []
    triggers = []          # 수리를 촉발한 미커버 최원점(검수 단계 시각화용)
    while best.max() > 0 and len(added) < max_add:
        k = int(np.argmax(best))
        kx, ky = float(cx[k]), float(cy[k])
        triggers.append((kx, ky))
        p = pick_position(kx, ky)
        heads_xy.append(p)
        r_of.append(add_r)
        added.append(p)
        np.minimum(best, contrib(p[0], p[1], add_r), out=best)
        if best[k] > 0:
            # 무진전 가드: 선택 위치가 정밀 검증에서 w 를 못 덮으면(경계 케이스)
            # w 자체에 폴백 배치 — 자기 셀 커버는 항상 성립 → 수렴 보장
            heads_xy.append((kx, ky))
            r_of.append(add_r)
            added.append((kx, ky))
            np.minimum(best, contrib(kx, ky, add_r), out=best)
    return added, float(best.max()), triggers
