"""
여러 층 평면도를 하나의 Building 아래 수직 스택으로 결합한 BOT 그래프 생성.

각 층은 build_bot.build_storey() 로 만들고, 층은 층고(단면도 유래)로 표고를 매겨
levelIndex/elevation/height 와 상하 관계(fran:aboveStorey)로 연결한다.

  python build_building.py            # 기본 스택(지하1층·1층·기준층)
  python build_building.py --out output/building.ttl

입력(각 층): data/<base>.json, output/<base>_rooms_rect.json,
            output/<base>_layer_classification.json
"""

import argparse
import json
import os
import sys

from rdflib import Literal, URIRef
from rdflib.namespace import RDF, RDFS, XSD

from build_bot import (BOT, FRAN, INST, new_graph, build_storey, add_beams,
                       uri_name)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 층 정의: (base, 표시라벨, levelIndex, 층고mm[단면도 유래])
#   levelIndex: 지하 음수, 지상 1부터. elevation 은 1층 바닥=0 기준 누적.
STACK = [
    ("지하1층", "지하1층",       -1, 4050),
    ("1층",    "1층",            1, 3250),
    ("기준층",  "기준층(2~15층)",  2, 2800),
]


def elevations(stack):
    """1층 바닥(levelIndex 1)=0 기준, 각 층 바닥 표고(mm) 계산."""
    order = sorted(stack, key=lambda s: s[2])
    base_i = next(k for k, s in enumerate(order) if s[2] == 1)
    elev = {}
    # 1층 위로 누적(+), 아래로 누적(-)
    e = 0
    for k in range(base_i, len(order)):
        elev[order[k][0]] = e
        e += order[k][3]
    e = 0
    for k in range(base_i - 1, -1, -1):
        e -= order[k][3]
        elev[order[k][0]] = e
    return elev


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join("output", "building.ttl"))
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out-dir", default="output")
    args = ap.parse_args()

    elev = elevations(STACK)
    g = new_graph()

    # 단지 이름은 설정에서 (build_bot.main 과 같은 규칙 — URI 가 일치해야 한다)
    site_name = "5BL 단지"
    _prof = os.path.join(args.data_dir, "building_profile.json")
    if os.path.exists(_prof):
        site_name = json.load(open(_prof, encoding="utf-8")).get("이름", site_name)
    site = INST[f"Site_{uri_name(site_name)}"]
    building = INST["Building_5BL_A"]
    g.add((site, RDF.type, BOT.Site))
    g.add((site, RDFS.label, Literal(site_name, lang="ko")))
    g.add((building, RDF.type, BOT.Building))
    g.add((building, RDFS.label, Literal("주동 A (5BL)", lang="ko")))
    g.add((site, BOT.hasBuilding, building))

    storey_uri = {}
    stats = {}
    for base, label, level, height in STACK:
        src = os.path.join(args.data_dir, f"{base}.json")
        rp = os.path.join(args.out_dir, f"{base}_rooms_rect.json")
        cp = os.path.join(args.out_dir, f"{base}_layer_classification.json")
        for p in (src, rp, cp):
            if not os.path.exists(p):
                print(f"입력 없음: {p}", file=sys.stderr)
                sys.exit(1)
        data = json.load(open(src, encoding="utf-8"))
        rooms_data = json.load(open(rp, encoding="utf-8"))["rooms"]
        cats = json.load(open(cp, encoding="utf-8"))["categories"]

        storey = INST[f"Storey_{uri_name(base)}"]
        storey_uri[base] = storey
        g.add((storey, RDF.type, BOT.Storey))
        g.add((storey, RDFS.label, Literal(label, lang="ko")))
        g.add((storey, FRAN.levelIndex, Literal(level, datatype=XSD.integer)))
        g.add((storey, FRAN.elevationMm, Literal(elev[base], datatype=XSD.integer)))
        g.add((storey, FRAN.heightMm, Literal(height, datatype=XSD.integer)))
        g.add((building, BOT.hasStorey, storey))

        pfx = f"{uri_name(base)}_"
        stats[base] = build_storey(
            g, storey, data["Entities"], rooms_data, cats, pfx=pfx,
            ids_path=os.path.join(args.out_dir, f"{base}_room_ids.json"))
        # 보도 층 접두사와 함께 — 안 그러면 층마다 inst:Beam_1 이 충돌한다
        bp = os.path.join(args.out_dir, f"{base}_beams.json")
        if os.path.exists(bp):
            add_beams(g, storey, json.load(open(bp, encoding="utf-8")),
                      rooms_rect=stats[base]["rooms_rect"],
                      room_uri=stats[base]["room_uri"], pfx=pfx)
        print(f"[{label:12}] level={level:+d} elev={elev[base]:+6d}mm h={height}mm "
              f"→ 방 {stats[base]['rooms']} · 세대 {stats[base]['units']} · "
              f"인접 {stats[base]['adj']} · 개구부 {stats[base]['iface']}")

    # 층 상하 관계: levelIndex 순으로 aboveStorey 연결
    order = sorted(STACK, key=lambda s: s[2])
    for lower, upper in zip(order, order[1:]):
        g.add((storey_uri[upper[0]], FRAN.aboveStorey, storey_uri[lower[0]]))

    g.serialize(destination=args.out, format="turtle")

    # 수직 연속(fran:verticalContinuation): 인접 층의 계단·ELEV 코어를 bbox 겹침으로 연결.
    # 층 도면들이 같은 시트에 나란히 그려져 원점이 다르다(기준층 = 1층 +84m 등) →
    # 같은 이름 방들의 중심 오프셋 중앙값으로 층간 평행이동을 추정해 보정한다.
    import statistics

    def _cens(base):
        rr = json.load(open(os.path.join(args.out_dir, f"{base}_rooms_rect.json"),
                            encoding="utf-8"))["rooms"]
        d = {}
        for r in rr:
            cx = (r["rect"][0] + r["rect"][2]) / 2
            cy = (r["rect"][1] + r["rect"][3]) / 2
            d.setdefault(r["room"], []).append((cx, cy))
        return d

    def _offset(lo_base, up_base):
        """upper 좌표 - lower 좌표 (같은 이름 방 정렬쌍의 중앙값). 매칭 부족 시 None."""
        a, b = _cens(lo_base), _cens(up_base)
        dxs, dys = [], []
        for nm in set(a) & set(b):
            for pa, pb in zip(sorted(a[nm]), sorted(b[nm])):
                dxs.append(pb[0] - pa[0])
                dys.append(pb[1] - pa[1])
        if len(dxs) < 2:
            return None
        return statistics.median(dxs), statistics.median(dys)

    def _ovl(a, b):
        ox = min(a[2], b[2]) - max(a[0], b[0])
        oy = min(a[3], b[3]) - max(a[1], b[1])
        if ox <= 0 or oy <= 0:
            return 0.0
        amin = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
        return ox * oy / max(amin, 1.0)

    def _kind(name):
        return "계단" if "계단" in name else "ELEV"

    n_vert = 0
    vert_pairs = []
    offsets = {}
    for lo, up in zip(order, order[1:]):
        off = _offset(lo[0], up[0])
        offsets[f"{lo[0]}→{up[0]}"] = ([round(off[0]), round(off[1])] if off else None)
        if off is None:
            continue
        dx, dy = off
        for cu in stats[up[0]].get("core_rooms", []):
            cur = [cu["rect"][0] - dx, cu["rect"][1] - dy,
                   cu["rect"][2] - dx, cu["rect"][3] - dy]
            for cl in stats[lo[0]].get("core_rooms", []):
                if _kind(cu["name"]) == _kind(cl["name"]) \
                        and _ovl(cur, cl["rect"]) > 0.3:
                    g.add((URIRef(cu["uri"]), FRAN.verticalContinuation,
                           URIRef(cl["uri"])))
                    n_vert += 1
                    vert_pairs.append(f"{up[0]}:{cu['name']}→{lo[0]}:{cl['name']}")
    if n_vert:
        g.serialize(destination=args.out, format="turtle")   # 수직 링크 반영 재직렬화

    # 위상 사실(topology facts): 리포트 평가가 근거로 쓸 수 있게 JSON 으로도 출력
    corridors = [dict(c, floor=base) for base, label, *_ in STACK
                 for c in stats[base].get("corridors", [])]
    travel_all = [dict(t, floor=base) for base, label, *_ in STACK
                  for t in stats[base].get("stair_travel", [])]
    facts = {
        "double_loaded_corridor_exists": any(c["double_loaded"] for c in corridors),
        "corridors": corridors,
        "stair_travel_max_m": (max(t["m"] for t in travel_all) if travel_all else None),
        "stair_travel_worst": (max(travel_all, key=lambda t: t["m"])
                               if travel_all else None),
        "stair_unreachable_rooms": sum(s.get("stair_unreachable", 0)
                                       for s in stats.values()),
        "vertical_links": n_vert, "vertical_pairs": vert_pairs,
        "storey_offsets_mm": offsets,
        # 문 폭: 현관에 접한 문(세대 출입문)의 최소 폭 + 전체 문 폭 통계
        "entrance_door_min_width_m": (min((d["width_m"] for base, *_ in STACK
                                           for d in stats[base].get("door_widths", [])
                                           if d["width_m"] and any("현관" in r for r in d["rooms"])),
                                          default=None)),
        "door_min_width_m": (min((d["width_m"] for base, *_ in STACK
                                  for d in stats[base].get("door_widths", [])
                                  if d["width_m"]), default=None)),
        "fixtures": {base: stats[base].get("fixtures", {}) for base, *_ in STACK},
        "accessible_rooms": sorted({r for base, *_ in STACK
                                    for r in stats[base].get("accessible_rooms", [])}),
        "basis": "BOT adjacentZone 위상 — 순환공간(복도·홀·로비) 양측 거주실 인접 판정 · "
                 "문/개방 연결 그래프 최단경로 보행거리 · 코어 bbox 층간 매칭",
    }
    fp = os.path.join(args.out_dir, "topology_facts.json")
    json.dump(facts, open(fp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n층 {len(STACK)}개 · 트리플 {len(g)}")
    print(f"복도 위상: 순환공간 {len(corridors)}개 · 양옆거실 중복도 "
          f"{'있음' if facts['double_loaded_corridor_exists'] else '없음'}")
    if facts["stair_travel_max_m"]:
        w = facts["stair_travel_worst"]
        print(f"직통계단 보행거리(근사): 최대 {facts['stair_travel_max_m']}m "
              f"({w['floor']} {w['room']}) · 미도달 실 {facts['stair_unreachable_rooms']}")
    print(f"수직 연결(계단·ELEV 코어): {n_vert}쌍")
    print(f"출력: {args.out} · {fp}")


if __name__ == "__main__":
    main()
