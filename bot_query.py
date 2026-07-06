"""
BOT 그래프(.ttl) 검증 + 예시 SPARQL 질의.

  python bot_query.py [도면베이스명]   # 기본 1층

- 파싱 성공(문법 오류 없음) 확인
- 스모크 테스트: 공간 수 / 세대별 방 수 / adjacentZone 대칭 / Interface 무결성
- 예시 질의 몇 개 출력
"""

import argparse
import os
import sys

from rdflib import Graph, Namespace
from rdflib.namespace import RDF

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BOT = Namespace("https://w3id.org/bot#")
FRAN = Namespace("https://example.org/fran#")


def q(g, sparql):
    return list(g.query(sparql, initNs={"bot": BOT, "fran": FRAN,
                                         "rdfs": "http://www.w3.org/2000/01/rdf-schema#"}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base", nargs="?", default="1층")
    ap.add_argument("--out-dir", default="output")
    args = ap.parse_args()
    ttl = os.path.join(args.out_dir, f"{args.base}.ttl")

    g = Graph()
    g.parse(ttl, format="turtle")
    print(f"✔ 파싱 성공: {ttl}  ({len(g)} 트리플)\n")

    # --- 층 스택(다중 층 building.ttl 일 때) ---
    storeys = q(g, """SELECT ?s ?lb ?lvl ?el ?h WHERE {
        ?s a bot:Storey . OPTIONAL{?s rdfs:label ?lb} OPTIONAL{?s fran:levelIndex ?lvl}
        OPTIONAL{?s fran:elevationMm ?el} OPTIONAL{?s fran:heightMm ?h}
        } ORDER BY DESC(?lvl)""")
    if len(storeys) > 1:
        print("== 층 스택(위→아래) ==")
        for r in storeys:
            print(f"  {str(r[1]):14} level={r[2]} elev={r[3]}mm h={r[4]}mm")
        chain = int(q(g, """SELECT (COUNT(*) AS ?n) WHERE {
            ?u fran:aboveStorey ?l }""")[0][0])
        print(f"  aboveStorey 연결: {chain} (층수-1 이어야 정상)\n")

    # --- 개체 수 ---
    print("== 클래스별 개체 수 ==")
    for r in q(g, """SELECT ?c (COUNT(?s) AS ?n) WHERE {
        ?s a ?c . FILTER(?c IN (bot:Site,bot:Building,bot:Storey,fran:DwellingUnit,
            bot:Space,bot:Interface,fran:Wall,fran:Door,fran:Window,
            fran:Elevator,fran:Stair)) } GROUP BY ?c ORDER BY DESC(?n)"""):
        print(f"  {str(r[0]).split('#')[-1]:14} {int(r[1])}")

    # --- 세대별 방 수 ---
    print("\n== 세대(DwellingUnit)별 방 수 ==")
    rows = q(g, """SELECT ?u (COUNT(?s) AS ?n) WHERE {
        ?u a fran:DwellingUnit ; bot:hasSpace ?s . ?s a bot:Space .
        } GROUP BY ?u ORDER BY ?u""")
    for r in rows:
        print(f"  {str(r[0]).split('#')[-1]:10} 방 {int(r[1])}개")
    print(f"  → 세대 {len(rows)}개, 세대 소속 방 합계 {sum(int(r[1]) for r in rows)}")

    # --- 스모크 테스트 ---
    print("\n== 스모크 테스트 ==")
    n_space = int(q(g, "SELECT (COUNT(?s) AS ?n) WHERE { ?s a bot:Space }")[0][0])
    print(f"  bot:Space 총 {n_space}개")

    # adjacentZone 대칭성
    asym = q(g, """SELECT (COUNT(*) AS ?n) WHERE {
        ?a bot:adjacentZone ?b . FILTER NOT EXISTS { ?b bot:adjacentZone ?a } }""")
    print(f"  adjacentZone 비대칭 위반: {int(asym[0][0])}  (0이어야 정상)")

    # Interface 는 정확히 2개 공간과 연결
    bad_if = q(g, """SELECT ?i (COUNT(?s) AS ?n) WHERE {
        ?i a bot:Interface ; bot:interfaceOf ?s } GROUP BY ?i HAVING (?n != 2)""")
    print(f"  interfaceOf!=2 인 Interface: {len(bad_if)}  (0이어야 정상)")

    # 모든 방이 어떤 세대나 층에 속함
    orphan = q(g, """SELECT (COUNT(?s) AS ?n) WHERE {
        ?s a bot:Space . FILTER NOT EXISTS { ?z bot:hasSpace ?s } }""")
    print(f"  어느 zone 에도 안 속한 방: {int(orphan[0][0])}  (0이어야 정상)")

    # --- 예시 질의 ---
    print("\n== 예시 질의 1: 구조벽 vs 비구조벽 수 ==")
    for r in q(g, """SELECT ?st (COUNT(?w) AS ?n) WHERE {
        ?w a fran:Wall ; fran:structural ?st } GROUP BY ?st"""):
        print(f"  structural={r[0]} : {int(r[1])}개")

    print("\n== 예시 질의 2: '거실'에 인접한 방들 (첫 세대) ==")
    rows = q(g, """SELECT ?ln WHERE {
        ?lr rdfs:label "거실"@ko ; bot:adjacentZone ?nb . ?nb rdfs:label ?ln .
        } LIMIT 12""")
    print("  ", ", ".join(sorted({str(r[0]) for r in rows})) or "(없음)")

    print("\n== 예시 질의 3: 문/창 개구부(Interface)가 잇는 두 방 (샘플 8) ==")
    rows = q(g, """SELECT ?la ?lb ?et WHERE {
        ?i a bot:Interface ; bot:interfaceOf ?a, ?b ; bot:hasElement ?e .
        ?a rdfs:label ?la . ?b rdfs:label ?lb . ?e a ?et .
        FILTER(STR(?a) < STR(?b) && STRSTARTS(STR(?et),STR(fran:))) } LIMIT 8""")
    for r in rows:
        print(f"  {str(r[0]):8} ─[{str(r[2]).split('#')[-1]}]─ {str(r[1])}")

    print("\n✔ 검증 완료")


if __name__ == "__main__":
    main()
