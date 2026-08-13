"""
실×규칙 매칭 결과 → 온톨로지 법령 주석 레이어(TTL).

입력:
  output/<도면>.ttl                    기하 그래프 (build_bot.py) — 실 URI 를 찾는 기준
  output/<도면>_room_bindings.json     실별 판정 (match_rules_rooms.py)

출력:
  output/<도면>_legal.ttl              법령 레이어 — 기하 TTL 과 같은 실 URI 에
                                       fran:verdict(판정·근거규칙·확신도)를 단다.
                                       두 파일을 함께 파싱하면 하나의 그래프.

설계: 기하와 법령을 파일로 분리한다. 사람이 판정을 확정하거나 매칭을 다시 돌려도
기하 TTL 은 재생성할 필요가 없다. 실명(rdfs:label)은 이 파일에도 다시 적어
(같은 트리플이라 병합 시 중복 없음) 법령 레이어 단독으로도 소비 가능하게 한다.

사용법: python annotate_legal.py [도면베이스명]
"""

import argparse
import json
import os
import re
import sys

from datetime import datetime

from rdflib import Graph, Namespace, Literal, URIRef
from rdflib.namespace import RDF, RDFS, OWL, XSD, DCTERMS

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BOT = Namespace("https://w3id.org/bot#")
FRAN = Namespace("https://example.org/fran#")
INST = Namespace("https://example.org/fran/inst#")


def uri_name(s):
    """도면 베이스명 → IRI 로컬명 (공백·슬래시가 있으면 직렬화가 실패한다)."""
    return re.sub(r"[^\w.-]", "_", str(s), flags=re.UNICODE).strip("_") or "unnamed"


def add_schema(g):
    g.add((FRAN.RuleVerdict, RDF.type, OWL.Class))
    g.add((FRAN.RuleVerdict, RDFS.label,
           Literal("실별 법령 판정(규칙×실 매칭 결과)", lang="ko")))
    g.add((FRAN.LegalRule, RDF.type, OWL.Class))
    g.add((FRAN.LegalRule, RDFS.label,
           Literal("법령 규칙(cons_law legal_rule 행)", lang="ko")))
    g.add((FRAN.System, RDF.type, OWL.Class))
    g.add((FRAN.System, RDFS.label, Literal("판정 대상 설비", lang="ko")))
    g.add((FRAN.SprinklerHead, RDF.type, FRAN.System))
    g.add((FRAN.SprinklerHead, RDFS.label, Literal("스프링클러헤드", lang="ko")))
    g.add((FRAN.aboutSystem, RDF.type, OWL.ObjectProperty))
    g.add((FRAN.aboutSystem, RDFS.domain, FRAN.RuleVerdict))
    g.add((FRAN.aboutSystem, RDFS.range, FRAN.System))
    g.add((FRAN.Basis, RDF.type, OWL.Class))
    g.add((FRAN.Basis, RDFS.label, Literal("판정 근거(순서 있는 규칙 참조)", lang="ko")))
    g.add((FRAN.PlacementPolicy, RDF.type, OWL.Class))
    g.add((FRAN.PlacementPolicy, RDFS.label,
           Literal("배치 정책(법령 외 설계 판단 — 화면 공개 대상)", lang="ko")))
    g.add((FRAN.PolicyEntry, RDF.type, OWL.Class))
    g.add((FRAN.PolicyEntry, RDFS.label, Literal("정책 항목(키·값)", lang="ko")))
    for p in ("action", "source", "confidence", "reason", "legalNode",
              "omittableBasis", "floorContext", "key", "value"):
        g.add((FRAN[p], RDF.type, OWL.DatatypeProperty))
        g.add((FRAN[p], RDFS.range, XSD.string))
    for p, dom in (("action", FRAN.RuleVerdict), ("source", FRAN.RuleVerdict),
                   ("confidence", FRAN.RuleVerdict), ("reason", FRAN.RuleVerdict),
                   ("legalNode", FRAN.RuleVerdict), ("key", FRAN.PolicyEntry),
                   ("value", FRAN.PolicyEntry), ("floorContext", BOT.Storey)):
        g.add((FRAN[p], RDFS.domain, dom))
    g.add((FRAN.needsReview, RDF.type, OWL.DatatypeProperty))
    g.add((FRAN.needsReview, RDFS.domain, FRAN.RuleVerdict))
    g.add((FRAN.needsReview, RDFS.range, XSD.boolean))
    g.add((FRAN.radiusMM, RDF.type, OWL.DatatypeProperty))
    g.add((FRAN.radiusMM, RDFS.domain, FRAN.RuleVerdict))
    g.add((FRAN.radiusMM, RDFS.range, XSD.decimal))
    g.add((FRAN.radiusMM, RDFS.label,
           Literal("이 실에 적용되는 헤드 수평거리(mm)", lang="ko")))
    for p, dom, rng in (("verdict", BOT.Space, FRAN.RuleVerdict),
                        ("basis", FRAN.RuleVerdict, FRAN.Basis),
                        ("rule", FRAN.Basis, FRAN.LegalRule),
                        ("appliedRule", FRAN.RuleVerdict, FRAN.LegalRule),
                        ("omittableRule", FRAN.RuleVerdict, FRAN.LegalRule),
                        ("appliedPolicy", FRAN.RuleVerdict, FRAN.PlacementPolicy),
                        ("policyEntry", FRAN.PlacementPolicy, FRAN.PolicyEntry),
                        ("placementPolicy", BOT.Storey, FRAN.PlacementPolicy)):
        g.add((FRAN[p], RDF.type, OWL.ObjectProperty))
        g.add((FRAN[p], RDFS.domain, dom))
        g.add((FRAN[p], RDFS.range, rng))
    g.add((FRAN.appliedRule, RDFS.label,
           Literal("이 판정을 실제로 결정한 규칙", lang="ko")))
    g.add((FRAN.omittableRule, RDFS.label,
           Literal("생략 가능 근거이나 적용하지 않은 규칙(후보)", lang="ko")))
    g.add((FRAN.order, RDF.type, OWL.DatatypeProperty))
    g.add((FRAN.order, RDFS.range, XSD.integer))


def main():
    ap = argparse.ArgumentParser(description="실 바인딩 → 법령 레이어 TTL")
    ap.add_argument("base", nargs="?", default="지하1층_pit", help="도면 베이스명")
    ap.add_argument("--out-dir", default="output")
    args = ap.parse_args()
    base = args.base

    geo_path = os.path.join(args.out_dir, f"{base}.ttl")
    bind_path = os.path.join(args.out_dir, f"{base}_room_bindings.json")
    ttl_out = os.path.join(args.out_dir, f"{base}_legal.ttl")
    for p in (geo_path, bind_path):
        if not os.path.exists(p):
            print(f"입력 없음: {p}", file=sys.stderr)
            sys.exit(1)

    geo = Graph()
    geo.parse(geo_path, format="turtle")
    # 실명(rdfs:label) → 해당 bot:Space URI 목록 (동명 실은 전부)
    by_label = {}
    for s in geo.subjects(RDF.type, BOT.Space):
        for lb in geo.objects(s, RDFS.label):
            by_label.setdefault(str(lb), []).append(s)
    storeys = list(geo.subjects(RDF.type, BOT.Storey))

    data = json.load(open(bind_path, encoding="utf-8"))
    bindings = data.get("바인딩", {})

    g = Graph()
    g.bind("bot", BOT)
    g.bind("fran", FRAN)
    g.bind("inst", INST)
    g.bind("owl", OWL)
    g.bind("dcterms", DCTERMS)
    add_schema(g)
    # 온톨로지 헤더 — 이 판정이 어느 도면·언제 것인지 파일만 보고 알 수 있어야 한다
    onto = URIRef(f"https://example.org/fran/legal/{uri_name(base)}")
    g.add((onto, RDF.type, OWL.Ontology))
    g.add((onto, OWL.imports, URIRef("https://w3id.org/bot#")))
    g.add((onto, DCTERMS.source, Literal(bind_path)))
    # 생성 시각은 '지금' 이 아니라 입력(판정 JSON)의 수정 시각. now() 를 쓰면
    # 입력이 그대로여도 파일이 매번 바뀌어 재실행 동일성이 깨진다.
    g.add((onto, DCTERMS.modified,
           Literal(datetime.fromtimestamp(os.path.getmtime(bind_path))
                   .astimezone().isoformat(timespec="seconds"),
                   datatype=XSD.dateTime)))
    g.add((onto, RDFS.comment,
           Literal(f"{base} 실별 법령 판정 (annotate_legal.py)", lang="ko")))

    # 층 맥락 + 배치 정책 (매칭 시 쓰인 그대로 — 키를 열거하지 않고 통째로 옮긴다)
    for st in storeys:
        if data.get("층맥락"):
            g.add((st, FRAN.floorContext, Literal(data["층맥락"], lang="ko")))
        pol = data.get("정책")
        if pol:
            pnode = INST[f"Policy_{uri_name(base)}"]
            g.add((st, FRAN.placementPolicy, pnode))
            g.add((pnode, RDF.type, FRAN.PlacementPolicy))
            # 정책 키를 술어(IRI)로 쓰지 않는다. 사람이 편집하는 설정 파일이라
            # 키에 공백이 하나만 들어가도 직렬화가 통째로 실패하고, 새 키마다
            # 어휘가 조용히 늘어난다. 키는 값으로 둔다.
            for i, (k, v) in enumerate(sorted(pol.items())):
                e = INST[f"Policy_{uri_name(base)}_{i}"]
                g.add((pnode, FRAN.policyEntry, e))
                g.add((e, RDF.type, FRAN.PolicyEntry))
                g.add((e, FRAN.key, Literal(str(k), lang="ko")))
                g.add((e, FRAN.value, Literal(str(v), lang="ko")))

    n_room = n_verdict = 0
    missing = []
    for name, b in bindings.items():
        spaces = by_label.get(name)
        if not spaces:
            missing.append(name)
            continue
        for sp in spaces:
            n_room += 1
            local = uri_name(str(sp).rsplit("#", 1)[-1].rsplit("/", 1)[-1])
            # 판정 URI 에 대상 설비를 넣는다 — 피난·경보 판정이 나중에
            # 같은 실에 붙어도 한 노드로 뭉개지지 않는다.
            v = INST[f"Verdict_SprinklerHead_{local}"]
            g.add((sp, RDFS.label, Literal(name, lang="ko")))   # 자체 완결용 재기재
            g.add((sp, RDF.type, BOT.Space))
            g.add((sp, FRAN.verdict, v))
            g.add((v, RDF.type, FRAN.RuleVerdict))
            g.add((v, FRAN.aboutSystem, FRAN.SprinklerHead))
            g.add((v, FRAN.action, Literal(b.get("기본동작", ""), lang="ko")))
            g.add((v, FRAN.source, Literal(b.get("출처", ""), lang="ko")))
            if b.get("confidence"):
                g.add((v, FRAN.confidence, Literal(b["confidence"])))
            g.add((v, FRAN.needsReview,
                   Literal(bool(b.get("확인필요")), datatype=XSD.boolean)))
            if b.get("노드"):
                g.add((v, FRAN.legalNode, Literal(b["노드"], lang="ko")))
            if b.get("이유"):
                g.add((v, FRAN.reason, Literal(b["이유"], lang="ko")))
            # 근거 노드는 블랭크 노드가 아니라 이름 있는 URI 로 만든다. 블랭크
            # 노드는 직렬화 순서가 실행마다 달라져(그래프는 동형이어도) 파일이
            # 매번 바뀌고, 파일 간 병합에서도 같은 근거로 합쳐지지 않는다.
            # 판정을 실제로 결정한 규칙 + 그때 적용된 정책. 이 둘이 없으면
            # "왜 제외인가" 에 관련 없는 조문이 답으로 나온다.
            decs = b.get("결정규칙") or []
            if isinstance(decs, dict):      # 구버전 바인딩(단일 dict) 호환
                decs = [decs]
            for dec in decs:
                if not dec.get("rule_id"):
                    continue
                ru = INST[f"Rule_{dec['rule_id']}"]
                g.add((v, FRAN.appliedRule, ru))
                g.add((ru, RDF.type, FRAN.LegalRule))
                if dec.get("출처"):
                    g.add((ru, RDFS.label, Literal(dec["출처"], lang="ko")))
            if b.get("정책적용") and pol:
                g.add((v, FRAN.appliedPolicy, INST[f"Policy_{uri_name(base)}"]))
            if b.get("반경_mm"):
                g.add((v, FRAN.radiusMM,
                       Literal(b["반경_mm"], datatype=XSD.decimal)))
            for i, ev in enumerate(b.get("근거", [])):
                bn = INST[f"Basis_SprinklerHead_{local}_{i}"]
                g.add((bn, RDF.type, FRAN.Basis))
                g.add((v, FRAN.basis, bn))
                g.add((bn, FRAN.order, Literal(i, datatype=XSD.integer)))
                rid = ev.get("rule_id")
                if rid:
                    ru = INST[f"Rule_{rid}"]
                    g.add((bn, FRAN.rule, ru))
                    g.add((ru, RDF.type, FRAN.LegalRule))
                    if ev.get("출처"):
                        g.add((ru, RDFS.label, Literal(ev["출처"], lang="ko")))
                elif ev.get("출처"):
                    g.add((bn, RDFS.label, Literal(ev["출처"], lang="ko")))
            for s_ in b.get("생략가능", []):
                if isinstance(s_, dict) and s_.get("rule_id"):
                    ru = INST[f"Rule_{s_['rule_id']}"]
                    g.add((v, FRAN.omittableRule, ru))
                    g.add((ru, RDF.type, FRAN.LegalRule))
                    if s_.get("출처"):
                        g.add((ru, RDFS.label, Literal(s_["출처"], lang="ko")))
                else:   # 구버전 바인딩(문자열만) 호환
                    g.add((v, FRAN.omittableBasis,
                           Literal(s_ if isinstance(s_, str)
                                   else s_.get("출처", ""), lang="ko")))
        n_verdict += 1

    g.serialize(destination=ttl_out, format="turtle")
    print(f"기하 그래프: {geo_path} (Space {sum(len(v) for v in by_label.values())})")
    print(f"판정 부착: 실명 {n_verdict}/{len(bindings)} → Space {n_room}개")
    if missing:
        print(f"경고 — 기하 TTL 에 없는 실명 {len(missing)}: {', '.join(missing)}")
    unbound = sorted(nm for nm in by_label if nm not in bindings)
    if unbound:
        print(f"바인딩 없는 실명 {len(unbound)}: {', '.join(unbound[:12])}"
              f"{' …' if len(unbound) > 12 else ''}")
    print(f"트리플 {len(g)} → {ttl_out}")
    if missing:
        # 부분 산출물을 정상 종료로 남기지 않는다 — 소비자(fire_layout)는 이
        # 파일을 '정본'으로 믿고, 빠진 실은 조용히 키워드 폴백으로 처리된다.
        sys.exit(f"실패: 판정이 붙지 않은 실명 {len(missing)}개 "
                 f"— 기하 TTL 을 다시 만들었는지 확인하십시오.")


if __name__ == "__main__":
    main()
