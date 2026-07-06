"""
CAD 평면도 → BOT(Building Topology Ontology) 그래프 빌더.

입력(모두 원본 좌표 mm 기준):
  data/<도면>.json               원본 엔티티 + 헤더(Title, Reference_*_Layer)
  output/<도면>_rooms_rect.json  방 경계 사각형 (plan_rooms_rect.py 산출)
  output/<도면>_layer_classification.json  레이어별 카테고리 (layer_classify.py 산출)

출력:
  output/<도면>.ttl              Turtle 직렬화 BOT 그래프

핵심 아이디어: CAD 에는 '좌표'만 있고 '관계'가 없다. 방 사각형의 경계(edge)를
서로 비교해 위상을 '추론'한다.
  - 두 방 사각형이 한 축에서 벽 두께(<=ADJ_GAP)만큼 떨어져 마주보면 → bot:adjacentZone
  - 마주보는 경계 영역(개구부 존)에 door/window 형상이 있으면 → bot:Interface
  - 방을 감싸는 경계 = 벽 → fran:Wall (구조/비구조는 레이어 카테고리로)
  - 순환공간(홀·복도·계단…)을 제외한 인접 방들의 연결요소 = 한 세대(fran:DwellingUnit)

사용법:
  python build_bot.py [도면베이스명]   # 기본: 1층
"""

import argparse
import collections
import json
import math
import os
import sys

from rdflib import Graph, Namespace, Literal, URIRef, BNode
from rdflib.namespace import RDF, RDFS, OWL, XSD

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# --- 네임스페이스 ----------------------------------------------------------
BOT = Namespace("https://w3id.org/bot#")
GEO = Namespace("http://www.opengis.net/ont/geosparql#")
FRAN = Namespace("https://example.org/fran#")          # 도메인 확장(스키마)
INST = Namespace("https://example.org/fran/inst#")     # 개체(individual)

# --- 위상 추론 파라미터(mm) ------------------------------------------------
ADJ_GAP = 700.0     # 마주보는 두 방 경계의 최대 간격(=벽 두께+근사 여유). 이내면 '인접'
                    # (분류 기반 벽으로 방 rect 가 정밀해지며 간격이 커져 400→700 상향)
TOUCH = 60.0        # 맞닿음 허용(간격이 살짝 음수여도 인접 인정)
MIN_OVL = 300.0     # 공유 경계로 인정할 최소 겹침 길이
EDGE_TOL = 90.0     # 경계선에 형상이 '붙어있다'고 볼 좌표 허용오차
AX = 3.0            # 축정렬 판정 허용오차
CELL = 500.0        # 개구부 형상 점 인덱스 격자 크기

# 카테고리 → (fran 클래스, 구조여부)
CAT_CLASS = {
    "wall_struct": ("Wall", True),
    "wall_nonstruct": ("Wall", False),
    "column_struct": ("Column", True),
    "column_nonstruct": ("Column", False),
    "stair": ("Stair", None),
    "door": ("Door", None),
    "window": ("Window", None),
    "elevator": ("Elevator", None),
}
WALL_CATS = {"wall_struct", "wall_nonstruct"}

# 순환/공용 공간(세대에 속하지 않음) 판별 키워드
COMMON_KW = ["홀", "복도", "계단", "로비", "EPS", "TPS", "PS", "PIT",
             "피트", "파이프", "샤프트", "ELEV", "승강", "AD", "AV",
             # 지하층 시설 실 — 세대 아님 + 헤드 반경도 공용(2.3m) 기준
             "통신", "제연", "창고", "전기실", "기계실", "펌프실", "방재실", "주차"]


def norm(t):
    return "".join(str(t).split())


def is_common(name):
    u = norm(name).upper()
    return any(k.upper() in u for k in COMMON_KW)


# 세대 내 실(주거실) 화이트리스트 — 헤드 수평거리 판정용.
# NFPC 608 §7 의 2.6m 는 '아파트등의 세대 내'에만 적용되므로, 이름이 주거실로
# 확인되는 실만 세대 기준을 쓰고 나머지(사우나·키즈짐 등 부대시설 포함 미지의 실)는
# 보수적으로 공용 2.3m(NFPC 103 §10 내화구조) 기본. 공용 키워드가 섞이면 공용 우선.
RESI_KW = ["거실", "침실", "주방", "식당", "욕실", "현관", "발코니",
           "드레스", "팬트리", "다용도", "알파", "파우더", "부부", "대피"]


def is_resi(name):
    if is_common(name):
        return False
    n = norm(name)
    return any(k in n for k in RESI_KW)


# --- 엔티티 → 축정렬 세그먼트 / 점 -----------------------------------------
def entity_segments(e):
    """엔티티를 (축정렬 세그먼트 목록, 대표점 목록)으로. 세그먼트=('V',x,ylo,yhi) 또는 ('H',y,xlo,xhi)."""
    t = e.get("Type")
    pts = []
    segs = []

    def addseg(ax, ay, bx, by):
        if abs(bx - ax) < AX and abs(by - ay) > 1:
            segs.append(("V", (ax + bx) / 2, min(ay, by), max(ay, by)))
        elif abs(by - ay) < AX and abs(bx - ax) > 1:
            segs.append(("H", (ay + by) / 2, min(ax, bx), max(ax, bx)))

    if t == "Line":
        a, b = e["Start"], e["End"]
        pts += [(a[0], a[1]), (b[0], b[1])]
        addseg(a[0], a[1], b[0], b[1])
    elif t == "Polyline":
        v = e["Verts"]
        pts += [(p[0], p[1]) for p in v]
        for i in range(len(v) - 1):
            addseg(v[i][0], v[i][1], v[i + 1][0], v[i + 1][1])
    elif t == "Arc":
        cx, cy = e["Center"][:2]
        r = e["Radius"]
        a0 = e.get("StartAngle", 0.0)
        a1 = e.get("EndAngle", 0.0)
        sw = (a1 - a0) % (2 * math.pi)
        for k in range(5):
            pts.append((cx + r * math.cos(a0 + sw * k / 4),
                        cy + r * math.sin(a0 + sw * k / 4)))
    elif t == "Circle":
        cx, cy = e["Center"][:2]
        pts.append((cx, cy))
    return segs, pts


def build_index(entities, cats):
    """카테고리별 축정렬 세그먼트 + 개구부(door/window) 점 격자 + 문 스윙호 목록."""
    vseg = collections.defaultdict(list)   # cat -> [(x,ylo,yhi)]
    hseg = collections.defaultdict(list)   # cat -> [(y,xlo,xhi)]
    open_grid = collections.defaultdict(set)  # (gx,gy) -> {'door','window'}
    door_arcs = []                          # (cx, cy, r) — 스윙호 반경 = 문짝 폭
    for e in entities:
        cat = cats.get(e.get("Layer") or "(no-layer)", "other")
        cl = CAT_CLASS.get(cat)
        if not cl:
            continue
        if cat == "door" and e.get("Type") == "Arc" \
                and 300 <= e.get("Radius", 0) <= 1500:
            cx, cy = e["Center"][:2]
            door_arcs.append((cx, cy, e["Radius"]))
        segs, pts = entity_segments(e)
        for s in segs:
            if s[0] == "V":
                vseg[cat].append((s[1], s[2], s[3]))
            else:
                hseg[cat].append((s[1], s[2], s[3]))
        if cat in ("door", "window"):
            for x, y in pts:
                open_grid[(int(x // CELL), int(y // CELL))].add(cat)
    return vseg, hseg, open_grid, door_arcs


def opening_in_zone(open_grid, x0, y0, x1, y1):
    """존 사각형 안에 door/window 점이 있으면 우선순위(door>window)로 반환, 없으면 None."""
    found = set()
    for gx in range(int(x0 // CELL), int(x1 // CELL) + 1):
        for gy in range(int(y0 // CELL), int(y1 // CELL) + 1):
            found |= open_grid.get((gx, gy), set())
    if "door" in found:
        return "door"
    if "window" in found:
        return "window"
    return None


def open_gap(vseg, hseg, axis, c0, c1, lo, hi, min_gap=700.0):
    """경계 벽대역에 사람이 지날 연속 개구부(벽 없는 구간 ≥min_gap)가 있는가.

    문 없는 개방 통로(현관→주방 등) 판정용: 벽(구조+비구조) 구간을 병합한 뒤
    [lo,hi] 안의 최대 빈 구간을 잰다. 문틀 위치도 벽이 없으므로 갭으로 잡힌다.
    """
    segs = []
    src = vseg if axis == "V" else hseg
    for cat in ("wall_struct", "wall_nonstruct"):
        for c, s0, s1 in src[cat]:
            if c0 - EDGE_TOL <= c <= c1 + EDGE_TOL:
                a, b = max(s0, lo), min(s1, hi)
                if b > a:
                    segs.append((a, b))
    if not segs:
        return (hi - lo) >= min_gap
    segs.sort()
    merged = [list(segs[0])]
    for a, b in segs[1:]:
        if a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    gaps = [merged[0][0] - lo] + \
           [merged[k + 1][0] - merged[k][1] for k in range(len(merged) - 1)] + \
           [hi - merged[-1][1]]
    return max(gaps) >= min_gap


def wall_type_on(vseg, hseg, axis, c0, c1, lo, hi):
    """경계 존을 덮는 벽 카테고리 판정 → True(구조)/False(비구조)/None(불명)."""
    def covers(segs, clo, chi):
        for c, s_lo, s_hi in segs:
            if clo - EDGE_TOL <= c <= chi + EDGE_TOL:
                if min(s_hi, hi) - max(s_lo, lo) > MIN_OVL * 0.4:
                    return True
        return False
    segs_struct = vseg["wall_struct"] if axis == "V" else hseg["wall_struct"]
    segs_non = vseg["wall_nonstruct"] if axis == "V" else hseg["wall_nonstruct"]
    if covers(segs_struct, c0, c1):
        return True
    if covers(segs_non, c0, c1):
        return False
    return None


# --- 방 인접 판정 ----------------------------------------------------------
def boundary(A, B):
    """두 방 rect 사이의 공유 경계 반환 또는 None.
    반환: {'axis','c0','c1','lo','hi'}  (c0<c1: 경계 좌표대, [lo,hi]: 경계를 따르는 겹침 구간)"""
    ax0, ay0, ax1, ay1 = A
    bx0, by0, bx1, by1 = B
    # 좌우 인접(V): x축에서 떨어져 있고 y가 겹침
    if ax1 <= bx0:
        xgap, c0, c1 = bx0 - ax1, ax1, bx0
    elif bx1 <= ax0:
        xgap, c0, c1 = ax0 - bx1, bx1, ax0
    else:
        xgap = None
    if xgap is not None and -TOUCH <= xgap <= ADJ_GAP:
        lo, hi = max(ay0, by0), min(ay1, by1)
        if hi - lo > MIN_OVL:
            return {"axis": "V", "c0": c0, "c1": c1, "lo": lo, "hi": hi}
    # 상하 인접(H): y축에서 떨어져 있고 x가 겹침
    if ay1 <= by0:
        ygap, c0, c1 = by0 - ay1, ay1, by0
    elif by1 <= ay0:
        ygap, c0, c1 = ay0 - by1, by1, ay0
    else:
        ygap = None
    if ygap is not None and -TOUCH <= ygap <= ADJ_GAP:
        lo, hi = max(ax0, bx0), min(ax1, bx1)
        if hi - lo > MIN_OVL:
            return {"axis": "H", "c0": c0, "c1": c1, "lo": lo, "hi": hi}
    return None


# --- WKT 헬퍼 --------------------------------------------------------------
def poly_wkt(r):
    x0, y0, x1, y1 = r
    return (f"POLYGON(({x0} {y0}, {x1} {y0}, {x1} {y1}, "
            f"{x0} {y1}, {x0} {y0}))")


def line_wkt(axis, c, lo, hi):
    if axis == "V":
        return f"LINESTRING({c} {lo}, {c} {hi})"
    return f"LINESTRING({lo} {c}, {hi} {c})"


def point_wkt(x, y):
    return f"POINT({x} {y})"


def add_geom(g, subj, wkt):
    geom = BNode()
    g.add((subj, GEO.hasGeometry, geom))
    g.add((geom, RDF.type, GEO.Geometry))
    g.add((geom, GEO.asWKT, Literal(wkt, datatype=GEO.wktLiteral)))


# --- 온톨로지 스키마(경량 TBox) -------------------------------------------
def add_schema(g):
    g.add((FRAN.DwellingUnit, RDF.type, OWL.Class))
    g.add((FRAN.DwellingUnit, RDFS.subClassOf, BOT.Space))
    g.add((FRAN.DwellingUnit, RDFS.label, Literal("세대(단위주호)", lang="ko")))
    for cls in ("Wall", "Door", "Window", "Column", "Stair", "Elevator"):
        u = FRAN[cls]
        g.add((u, RDF.type, OWL.Class))
        g.add((u, RDFS.subClassOf, BOT.Element))
    g.add((FRAN.structural, RDF.type, OWL.DatatypeProperty))
    g.add((FRAN.structural, RDFS.domain, FRAN.Wall))
    g.add((FRAN.structural, RDFS.range, XSD.boolean))
    g.add((FRAN.areaM2, RDF.type, OWL.DatatypeProperty))
    g.add((FRAN.Fixture, RDF.type, OWL.Class))
    g.add((FRAN.Fixture, RDFS.subClassOf, BOT.Element))
    g.add((FRAN.Fixture, RDFS.label, Literal("위생·주방·설비 기구", lang="ko")))
    for p_ in ("clearWidthM", "fixtureType", "accessibleDesign",
               "stairTravelDistanceM", "doubleLoadedCorridor"):
        g.add((FRAN[p_], RDF.type, OWL.DatatypeProperty))
    g.add((FRAN.verticalContinuation, RDF.type, OWL.ObjectProperty))


def main():
    ap = argparse.ArgumentParser(description="CAD 평면도 → BOT 그래프")
    ap.add_argument("base", nargs="?", default="1층", help="도면 베이스명 (기본 1층)")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out-dir", default="output")
    args = ap.parse_args()
    base = args.base

    src = os.path.join(args.data_dir, f"{base}.json")
    rooms_path = os.path.join(args.out_dir, f"{base}_rooms_rect.json")
    cls_path = os.path.join(args.out_dir, f"{base}_layer_classification.json")
    ttl_out = os.path.join(args.out_dir, f"{base}.ttl")

    for p in (src, rooms_path, cls_path):
        if not os.path.exists(p):
            print(f"입력 없음: {p}", file=sys.stderr)
            sys.exit(1)

    data = json.load(open(src, encoding="utf-8"))
    title = data.get("Title") or base
    rooms_data = json.load(open(rooms_path, encoding="utf-8"))["rooms"]
    cats = json.load(open(cls_path, encoding="utf-8"))["categories"]

    g = new_graph()
    site = INST["Site_5BL"]
    building = INST[f"Building_{title}"]
    storey = INST[f"Storey_{base}"]
    g.add((site, RDF.type, BOT.Site))
    g.add((site, RDFS.label, Literal("5BL 단지", lang="ko")))
    g.add((building, RDF.type, BOT.Building))
    g.add((building, RDFS.label, Literal(f"주동 {title}", lang="ko")))
    g.add((storey, RDF.type, BOT.Storey))
    g.add((storey, RDFS.label, Literal(base, lang="ko")))
    g.add((site, BOT.hasBuilding, building))
    g.add((building, BOT.hasStorey, storey))

    st = build_storey(g, storey, data["Entities"], rooms_data, cats)

    g.serialize(destination=ttl_out, format="turtle")
    print(f"입력 도면: {src}  (Title={title})")
    print(f"방(Space): {st['rooms']} · 세대(DwellingUnit): {st['units']} "
          f"· 공용/순환방: {st['common']}")
    print(f"인접(adjacentZone): {st['adj']}쌍")
    print(f"벽(Wall): 분리벽 {st['wall']} + 외벽 {st['ext']}")
    print(f"개구부(Interface): {st['iface']} · 파사드 창: {st['facwin']}")
    print(f"트리플 수: {len(g)}")
    print(f"출력: {ttl_out}")


def new_graph():
    """프리픽스 바인딩 + 스키마(TBox)가 채워진 빈 그래프."""
    g = Graph()
    g.bind("bot", BOT)
    g.bind("fran", FRAN)
    g.bind("inst", INST)
    g.bind("geo", GEO)
    g.bind("owl", OWL)
    add_schema(g)
    return g


def build_storey(g, storey, entities, rooms_data, cats, pfx=""):
    """한 층의 방/세대/요소/위상을 그래프 g 에 추가(storey 아래). 반환: 통계 dict.

    pfx: 개체 URI 접두사(층 구분용, 다중 층에서 충돌 방지). 예: '기준층_'.
    """
    def U(name):
        return INST[f"{pfx}{name}"]

    rooms = []
    for i, r in enumerate(rooms_data):
        x0, y0, x1, y1 = r["rect"]
        rooms.append({
            "id": i, "name": r["room"], "rect": [x0, y0, x1, y1],
            "area": round((x1 - x0) * (y1 - y0) / 1e6, 2),
            "common": is_common(r["room"]),
            "shared_sides": set(),
        })

    vseg, hseg, open_grid, door_arcs = build_index(entities, cats)

    adjacencies = []
    for i in range(len(rooms)):
        for j in range(i + 1, len(rooms)):
            bd = boundary(rooms[i]["rect"], rooms[j]["rect"])
            if bd:
                adjacencies.append((i, j, bd))
                if bd["axis"] == "V":
                    lo_i = "R" if rooms[i]["rect"][2] <= rooms[j]["rect"][0] else "L"
                    rooms[i]["shared_sides"].add(lo_i)
                    rooms[j]["shared_sides"].add("L" if lo_i == "R" else "R")
                else:
                    lo_i = "T" if rooms[i]["rect"][3] <= rooms[j]["rect"][1] else "B"
                    rooms[i]["shared_sides"].add(lo_i)
                    rooms[j]["shared_sides"].add("B" if lo_i == "T" else "T")

    for i, j, bd in adjacencies:
        axis, c0, c1, lo, hi = bd["axis"], bd["c0"], bd["c1"], bd["lo"], bd["hi"]
        if axis == "V":
            bd["open"] = opening_in_zone(open_grid, c0 - EDGE_TOL, lo, c1 + EDGE_TOL, hi)
        else:
            bd["open"] = opening_in_zone(open_grid, lo, c0 - EDGE_TOL, hi, c1 + EDGE_TOL)
        bd["struct"] = wall_type_on(vseg, hseg, axis, c0, c1, lo, hi)

    # 방(Space) 개체
    room_uri = {}
    for r in rooms:
        u = U(f"Room_{r['id']}")
        room_uri[r["id"]] = u
        g.add((u, RDF.type, BOT.Space))
        g.add((u, RDFS.label, Literal(r["name"], lang="ko")))
        g.add((u, FRAN.areaM2, Literal(r["area"], datatype=XSD.decimal)))
        add_geom(g, u, poly_wkt(r["rect"]))

    def cen(r):
        x0, y0, x1, y1 = r["rect"]
        return ((x0 + x1) / 2, (y0 + y1) / 2)

    # 하이브리드 세대 분리: 문-연결 블록(연결요소) → 세대
    door_adj = collections.defaultdict(set)
    for i, j, bd in adjacencies:
        if bd.get("open") == "door":
            door_adj[i].add(j)
            door_adj[j].add(i)
    seen, blocks = set(), []
    for r in rooms:
        s = r["id"]
        if s in seen:
            continue
        stack, comp = [s], []
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            comp.append(x)
            stack.extend(door_adj.get(x, ()))
        blocks.append(comp)

    # 파편 블록(주거실 5개 미만)은 최근접 큰 블록에 흡수. 크기는 '주거실 수' 기준 —
    # 현관문이 코어(ELEV.홀·계단·로비)와 이어져 {현관+공용부} 혼합 블록이 5개를
    # 넘어도 세대가 아니다 (공용부는 어차피 층 직속).
    MIN_UNIT_ROOMS = 5
    def n_resi(b):
        return sum(1 for m in b if not rooms[m]["common"])
    big = [b for b in blocks if n_resi(b) >= MIN_UNIT_ROOMS]
    if big:
        def bcen(b):
            cs = [cen(rooms[m]) for m in b]
            return (sum(c[0] for c in cs) / len(cs), sum(c[1] for c in cs) / len(cs))
        bigcen = [(b, bcen(b)) for b in big]
        for b in blocks:
            if n_resi(b) < MIN_UNIT_ROOMS:
                for m in b:
                    near, _ = min(bigcen, key=lambda t: math.dist(cen(rooms[m]), t[1]))
                    near.append(m)
        blocks = big

    unit_count = 0
    for comp in sorted(blocks, key=lambda b: cen(rooms[b[0]])):
        n_ent = sum(1 for m in comp if "현관" in rooms[m]["name"])
        members_resi = [m for m in comp if not rooms[m]["common"]]
        # 공용부(코어: ELEV·계단·홀·로비)만의 블록이나 현관 없는 단독방은 세대가 아님
        if not members_resi or (len(comp) == 1 and n_ent == 0):
            for m in comp:
                g.add((storey, BOT.hasSpace, room_uri[m]))
            continue
        unit_count += 1
        uu = U(f"Unit_{unit_count}")
        g.add((uu, RDF.type, FRAN.DwellingUnit))
        g.add((uu, RDFS.label, Literal(f"세대 {unit_count}", lang="ko")))
        g.add((storey, BOT.hasSpace, uu))
        if n_ent > 1:
            g.add((uu, FRAN.dwellingCount, Literal(n_ent, datatype=XSD.integer)))
        for m in comp:
            if rooms[m]["common"]:
                g.add((storey, BOT.hasSpace, room_uri[m]))
            else:
                g.add((uu, BOT.hasSpace, room_uri[m]))

    # 인접 + 분리벽 + 개구부(Interface)
    n_adj = n_wall = n_iface = 0
    wall_id = door_id = win_id = iface_id = 0
    door_widths = []   # 문별 폭 + 연결 실 (facts·현관문 폭 도출용)
    for i, j, bd in adjacencies:
        ui, uj = room_uri[i], room_uri[j]
        g.add((ui, BOT.adjacentZone, uj))
        g.add((uj, BOT.adjacentZone, ui))
        n_adj += 1
        axis, c0, c1, lo, hi = bd["axis"], bd["c0"], bd["c1"], bd["lo"], bd["hi"]
        cc = (c0 + c1) / 2
        wall_id += 1
        w = U(f"Wall_{wall_id}")
        g.add((w, RDF.type, FRAN.Wall))
        if bd["struct"] is not None:
            g.add((w, FRAN.structural, Literal(bd["struct"], datatype=XSD.boolean)))
        add_geom(g, w, line_wkt(axis, cc, lo, hi))
        g.add((ui, BOT.adjacentElement, w))
        g.add((uj, BOT.adjacentElement, w))
        n_wall += 1
        op = bd["open"]
        ox, oy = (cc, (lo + hi) / 2) if axis == "V" else ((lo + hi) / 2, cc)
        if op:
            iface_id += 1
            iface = U(f"Interface_{iface_id}")
            g.add((iface, RDF.type, BOT.Interface))
            g.add((iface, BOT.interfaceOf, ui))
            g.add((iface, BOT.interfaceOf, uj))
            if op == "door":
                door_id += 1
                el = U(f"Door_{door_id}")
                g.add((el, RDF.type, FRAN.Door))
                # 문폭 = 개구부 지점 최근접 스윙호 반경 (경첩~문틀 = 문짝 폭)
                best = min(door_arcs, default=None,
                           key=lambda a_: (a_[0] - ox) ** 2 + (a_[1] - oy) ** 2)
                wm = None
                if best and math.hypot(best[0] - ox, best[1] - oy) <= 1500:
                    wm = round(best[2] / 1000, 2)
                    g.add((el, FRAN.clearWidthM,
                           Literal(wm, datatype=XSD.decimal)))
                door_widths.append({"rooms": [rooms[i]["name"], rooms[j]["name"]],
                                    "width_m": wm})
            else:
                win_id += 1
                el = U(f"Window_{win_id}")
                g.add((el, RDF.type, FRAN.Window))
            add_geom(g, el, point_wkt(round(ox), round(oy)))
            g.add((iface, BOT.hasElement, el))
            g.add((ui, BOT.adjacentElement, el))
            g.add((uj, BOT.adjacentElement, el))
            n_iface += 1

    # 외곽 변 → 외벽 + 파사드 창
    SIDE = {"L": ("V", 0, 1, 3), "R": ("V", 2, 1, 3),
            "B": ("H", 1, 0, 2), "T": ("H", 3, 0, 2)}
    n_ext = n_facwin = 0
    for r in rooms:
        for side, (axis, ci, li, hii) in SIDE.items():
            if side in r["shared_sides"]:
                continue
            c = r["rect"][ci]
            lo, hi = r["rect"][li], r["rect"][hii]
            struct = wall_type_on(vseg, hseg, axis, c, c, lo, hi)
            if struct is None:
                continue
            wall_id += 1
            w = U(f"Wall_{wall_id}")
            g.add((w, RDF.type, FRAN.Wall))
            g.add((w, FRAN.structural, Literal(struct, datatype=XSD.boolean)))
            g.add((w, FRAN.exterior, Literal(True, datatype=XSD.boolean)))
            add_geom(g, w, line_wkt(axis, c, lo, hi))
            g.add((room_uri[r["id"]], BOT.adjacentElement, w))
            n_ext += 1
            if axis == "V":
                op = opening_in_zone(open_grid, c - EDGE_TOL, lo, c + EDGE_TOL, hi)
                ox, oy = c, (lo + hi) / 2
            else:
                op = opening_in_zone(open_grid, lo, c - EDGE_TOL, hi, c + EDGE_TOL)
                ox, oy = (lo + hi) / 2, c
            if op == "window":
                win_id += 1
                el = U(f"Window_{win_id}")
                g.add((el, RDF.type, FRAN.Window))
                add_geom(g, el, point_wkt(round(ox), round(oy)))
                g.add((room_uri[r["id"]], BOT.adjacentElement, el))
                g.add((room_uri[r["id"]], BOT.containsElement, el))
                n_facwin += 1

    # 층 전체 특수 요소(승강기/계단)
    for cat, cname in (("elevator", "Elevator"), ("stair", "Stair")):
        if vseg[cat] or hseg[cat]:
            el = U(f"{cname}_1")
            g.add((el, RDF.type, FRAN[cname]))
            g.add((storey, BOT.containsElement, el))

    # ---- 복도 유형 위상 판별 (피난·방화규칙 §15의2 '양옆에 거실이 있는 복도') ----
    # 순환공간(복도·홀·로비)의 인접 방 중 법적 거실(거주·집무실)이 서로 마주보는
    # 양측에 있으면 중복도(double-loaded). adjacentZone 위상 + 경계 방향으로 판정.
    CIRC_KW = ("복도", "홀", "로비")
    HABITABLE_KW = ("거실", "침실", "주방", "식당", "알파")
    corridors = []
    for i, r in enumerate(rooms):
        if not any(k in r["name"] for k in CIRC_KW):
            continue
        sides = set()
        for a, b, bd in adjacencies:
            if i not in (a, b):
                continue
            j = b if a == i else a
            if not any(k in rooms[j]["name"] for k in HABITABLE_KW):
                continue
            if bd["axis"] == "V":
                sides.add("R" if rooms[i]["rect"][2] <= rooms[j]["rect"][0] else "L")
            else:
                sides.add("T" if rooms[i]["rect"][3] <= rooms[j]["rect"][1] else "B")
        dl = ({"L", "R"} <= sides) or ({"B", "T"} <= sides)
        g.add((room_uri[i], FRAN.doubleLoadedCorridor,
               Literal(dl, datatype=XSD.boolean)))
        x0, y0, x1, y1 = r["rect"]
        corridors.append({"name": r["name"], "double_loaded": dl,
                          "habitable_sides": sorted(sides),
                          "width_m": round(min(x1 - x0, y1 - y0) / 1000, 2)})

    # ---- 직통계단 보행거리(근사) — 문/개방 연결 그래프 최단경로 -------------
    # 통행 가능 = 문 개구부 or 벽 근거 없는 개방 경계(LDK 등). 거리 = 실 중심점
    # 경유 합 + 출발 실 대각선/2(실 '각 부분' 보정). 건축법령 §34 보행거리용.
    import heapq
    stair_ids = [r["id"] for r in rooms if "계단" in r["name"]]
    travel = []
    n_unreach = 0
    if stair_ids:
        walk = collections.defaultdict(list)
        for a, b, bd in adjacencies:
            passable = (bd.get("open") == "door"
                        or open_gap(vseg, hseg, bd["axis"], bd["c0"], bd["c1"],
                                    bd["lo"], bd["hi"]))
            if passable:
                w = math.dist(cen(rooms[a]), cen(rooms[b]))
                walk[a].append((b, w))
                walk[b].append((a, w))
        dist = {s: 0.0 for s in stair_ids}
        pq = [(0.0, s) for s in stair_ids]
        heapq.heapify(pq)
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, float("inf")):
                continue
            for v, w in walk[u]:
                nd = d + w
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        # 세대 내부 실: 세대 내 복도가 라벨 없는 공간이라 그래프가 현관에서 끊긴다.
        # 세대 내부는 물리적으로 연결돼 있으므로 '실→자기 세대 현관 직선 + 현관→계단
        # 경로'로 근사한다 (거실 각 부분 보정 = 실 대각/2).
        blk_of = {m: bi for bi, comp in enumerate(blocks) for m in comp}
        via_unit = 0
        for r in rooms:
            if r["common"]:
                continue
            rid = r["id"]
            if rid not in dist and rid in blk_of:
                ents_ = [m for m in blocks[blk_of[rid]]
                         if "현관" in rooms[m]["name"] and m in dist]
                if ents_:
                    e = min(ents_, key=lambda m: dist[m]
                            + math.dist(cen(r), cen(rooms[m])))
                    dist[rid] = dist[e] + math.dist(cen(r), cen(rooms[e]))
                    via_unit += 1
            if rid not in dist:
                n_unreach += 1
                continue
            x0, y0, x1, y1 = r["rect"]
            dm = round((dist[rid] + math.hypot(x1 - x0, y1 - y0) / 2) / 1000, 1)
            g.add((room_uri[rid], FRAN.stairTravelDistanceM,
                   Literal(dm, datatype=XSD.decimal)))
            travel.append({"room": r["name"], "m": dm})

    # ---- 설비 블록(BlockReference) → fran:Fixture + 주거약자형 설계 표시 ----
    FIXTURE_KINDS = [("세면대", "washbasin"), ("변기", "toilet"), ("싱크", "sink"),
                     ("가스렌지", "stove"), ("세탁", "laundry"), ("건조기", "laundry"),
                     ("분전반", "dist_panel"), ("통신단자", "comm_box"),
                     ("배수", "drain"), ("실외기", "outdoor_unit")]

    def host_room(x, y):
        return next((r for r in rooms
                     if r["rect"][0] <= x <= r["rect"][2]
                     and r["rect"][1] <= y <= r["rect"][3]), None)

    fixtures = collections.Counter()
    acc_rooms = set()
    fx_id = 0
    for e in entities:
        if e.get("Type") != "BlockReference":
            continue
        bn = str(e.get("BlockName", ""))
        pos = e.get("Pos")
        if not pos:
            continue
        if "주거약자" in bn:   # 주거약자형 욕실 등 — 실에 접근성 설계 표시
            h = host_room(pos[0], pos[1])
            if h:
                g.add((room_uri[h["id"]], FRAN.accessibleDesign,
                       Literal(True, datatype=XSD.boolean)))
                acc_rooms.add(h["name"])
        kind = next((k for kw, k in FIXTURE_KINDS if kw in bn), None)
        if not kind:
            continue
        fx_id += 1
        el = U(f"Fixture_{fx_id}")
        g.add((el, RDF.type, FRAN.Fixture))
        g.add((el, FRAN.fixtureType, Literal(kind)))
        add_geom(g, el, point_wkt(round(pos[0]), round(pos[1])))
        h = host_room(pos[0], pos[1])
        if h:
            g.add((room_uri[h["id"]], BOT.containsElement, el))
        fixtures[kind] += 1

    # 수직 연결용 코어 공간 목록(층간 bbox 매칭은 build_building 에서)
    core_rooms = [{"id": r["id"], "name": r["name"], "rect": r["rect"]}
                  for r in rooms if "계단" in r["name"] or "ELEV" in r["name"].upper()]

    return {"corridors": corridors, "stair_travel": travel,
            "stair_unreachable": n_unreach, "core_rooms": core_rooms,
            "door_widths": door_widths, "fixtures": dict(fixtures),
            "accessible_rooms": sorted(acc_rooms),
            "rooms": len(rooms), "units": unit_count,
            "common": sum(1 for r in rooms if r["common"]),
            "adj": n_adj, "wall": n_wall, "ext": n_ext,
            "iface": n_iface, "facwin": n_facwin}


if __name__ == "__main__":
    main()
