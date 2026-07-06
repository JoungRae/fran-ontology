"""
cons_law DB의 전체 체크(6,810) × BOT 도출값 → 전수 자동검토 + 리포트 v2 + findings 적재.

  1. derived_terms.json(표준 term 값) 로드 → term_aliases 로 룰 필드명까지 확장
  2. DB checks 전체 로드(+ v_clauses 조인으로 법령명·조문)
  3. cons_law evaluator 로 전수 평가
  4. output/compliance_report_db.html — 실무자용 리포트 v2
     (KPI·도메인×상태 매트릭스·검색/필터·근거조문·입력값 출처)
  5. --db 옵션 시 projects/analyses/findings 테이블에 결과 적재

사용법: python evaluate_full.py [--db] [--out output/compliance_report_db.html]
"""

import argparse
import collections
import datetime
import html
import importlib.util
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FO = os.path.dirname(os.path.abspath(__file__))
CONS_LAW = r"D:/Python_test/cons_law"


def load_evaluator():
    spec = importlib.util.spec_from_file_location(
        "cons_evaluator", os.path.join(CONS_LAW, "src", "cons_law", "evaluator.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cons_evaluator"] = mod
    spec.loader.exec_module(mod)
    return mod


def get_conn():
    from dotenv import load_dotenv
    import psycopg
    load_dotenv(os.path.join(CONS_LAW, ".env"))
    return psycopg.connect(os.environ["PG_DSN"], connect_timeout=10)


# 도면 연동: 방 이름 키워드 → 그 방이 값을 제공한 term (도면 하이라이트용)
KIND_TERMS = [
    ("홀", ["space.corridor.clear_width_m"]),
    ("발코니", ["space.evacuation.area_m2", "space.evacuation.provided"]),
    ("욕실", ["space.room.bathroom_area_m2"]),
    ("거실", ["space.room.living_dining_area_m2", "space.room.floor_area_m2"]),
    ("침실", ["space.room.floor_area_m2"]),
    ("현관", ["project.household_count", "element.door.entrance_door_width_m"]),
    # 승강기: 대수의 출처는 flood-fill이 인식한 샤프트(ELEV.)와 승강기홀
    ("ELEV", ["element.elevator.count", "element.elevator.provided"]),
]


def build_drawing(alias_map):
    """층별 도면 데이터(벽 선형 path + 방 사각형 + 연동 term) 생성 → dict.

    alias_map: term_id -> [룰 필드명 별칭들] — 하이라이트 매칭이 별칭으로도 되도록.
    """
    floors = {}
    for base in ("1층", "기준층", "지하1층"):
        rp = os.path.join(FO, "output", f"{base}_rooms_rect.json")
        dp = os.path.join(FO, "data", f"{base}.json")
        cp = os.path.join(FO, "output", f"{base}_layer_classification.json")
        if not all(os.path.exists(p) for p in (rp, dp, cp)):
            continue
        rooms_data = json.load(open(rp, encoding="utf-8"))["rooms"]
        cats = json.load(open(cp, encoding="utf-8"))["categories"]
        wall_layers = {ly for ly, c in cats.items()
                       if c in ("wall_struct", "wall_nonstruct")}
        data = json.load(open(dp, encoding="utf-8"))

        segs = []
        for e in data["Entities"]:
            if e.get("Layer") not in wall_layers:
                continue
            t = e.get("Type")
            if t == "Line":
                a, b = e["Start"], e["End"]
                segs.append((a[0], a[1], b[0], b[1]))
            elif t == "Polyline":
                v = e["Verts"] + ([e["Verts"][0]] if e.get("Closed") else [])
                for i in range(len(v) - 1):
                    segs.append((v[i][0], v[i][1], v[i + 1][0], v[i + 1][1]))
        if not segs and not rooms_data:
            continue

        xs = [c for s in segs for c in (s[0], s[2])] + \
             [c for r in rooms_data for c in (r["rect"][0], r["rect"][2])]
        ys = [c for s in segs for c in (s[1], s[3])] + \
             [c for r in rooms_data for c in (r["rect"][1], r["rect"][3])]
        minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)

        def fy(y):
            return round(maxy - y)

        path = "".join(f"M{round(x1)} {fy(y1)}L{round(x2)} {fy(y2)}"
                       for x1, y1, x2, y2 in segs)
        rooms = []
        for r in rooms_data:
            x0, y0, x1, y1 = r["rect"]
            terms = []
            for kw, tids in KIND_TERMS:
                if kw in r["room"]:
                    for tid in tids:
                        terms.append(tid)
                        terms.extend(alias_map.get(tid, []))
            rooms.append({"n": r["room"], "x": round(x0), "y": fy(y1),
                          "w": round(x1 - x0), "h": round(y1 - y0),
                          "t": sorted(set(terms))})
        pad = round(max(maxx - minx, maxy - miny) * 0.03)
        floors[base] = {
            "vb": [round(minx) - pad, -pad,
                   round(maxx - minx) + 2 * pad, round(maxy - miny) + 2 * pad],
            "walls": path, "rooms": rooms,
        }
    return floors


def rule_fields(rule):
    """룰이 참조하는 도면 필드명 집합."""
    out = set()
    if not isinstance(rule, dict):
        return out
    for k in ("numerator_field", "denominator_field", "field", "require",
              "credit_to_field"):
        v = rule.get(k)
        if isinstance(v, str):
            out.add(v)
    for f in rule.get("fields", []) or []:
        out.add(f)
    for br in rule.get("branches", []) or []:
        req = br.get("require", {})
        if isinstance(req, dict):
            out.update(req.keys())
    then = rule.get("then")
    if isinstance(then, dict) and isinstance(then.get("require"), str):
        out.add(then["require"])
    return out


STATUS_KO = {"pass": "적합", "fail": "부적합",
             "needs_review": "검토필요", "not_applicable": "해당없음"}

# 별칭 주입 차단: 별칭 이름에 '측정 조건'이 인코딩돼 있어 우리 일반 측정값을
# 넣으면 의미가 어긋나는 필드. 예: 양옆거실 중복도 유효폭 ← 승강기홀 짧은 변(홀형
# 건물)을 주입하면 무의미한 부적합 발생. 미공급 → 검토필요(필드 부족)가 정직한 상태.
ALIAS_BLOCK = {
    "corridor_effective_width_both_sides_m",   # 양옆 거실 복도 — 홀형 건물엔 해당 구조 없음
}
# 양옆거실 중복도 전용 필드 — 위상 사실(topology_facts)이 '중복도 없음'을 증명하면
# 이 필드를 쓰는 체크는 해당없음 처리
BOTH_SIDES_FIELDS = {"corridor_effective_width_both_sides_m"}

# 용도 분류형 필드 — value_in/value_equals 불일치는 위반이 아니라 '적용 대상 아님'
USE_FIELDS = {"use", "use_type", "building_use", "project_use", "facility_use"}
# 정의·분류성 조항 의심 마커 (상태는 유지, UI에 '확인要' 태그만).
# 다른 용도·주택유형·구조유형의 정의 조항이 applicability 없이 추출된 경우가 대부분.
SUSPECT_PAT = re.compile(
    r"정의|대상|특례|포함|신고|가설|허가|규모|경미한|변경|증축|개축|위원|면제|간주"
    r"|다중주택|다가구|다세대|연립|근린생활|생활주택|조적|목구조|집회|판매|업무시설"
    r"|REPORT|TEMPORARY|DEFINITION|EXEMPT|PERMIT")   # 영문 code 마커


def postprocess(check, result):
    """LLM 추출 룰의 구조적 한계로 생기는 거짓 fail 을 정직하게 재분류.

    1) 분기 요구 필드가 없어 actual=None 인 fail → needs_review
    2) min/max 기준값이 문자열 필드참조(예: 'existing_gross_floor_area_m2')
       → 기준값 데이터 부족이므로 needs_review (문자열 속 숫자 오파싱 방지)
    3) 용도 분류(value_in/equals on use) 불일치 → not_applicable
    반환: (result, suspect(bool))
    """
    rule = check.get("rule") or {}
    rt = rule.get("type")
    if result.status in ("pass", "fail"):
        thr = rule.get("min") if rt == "min_value" else (
            rule.get("max") if rt == "max_value" else None)
        if isinstance(thr, str):
            result.status = "needs_review"
            result.message = f"기준값이 타 필드 참조({thr}) — 해당 데이터 없음"
            return result, False
    if result.status == "fail":
        if result.actual is None:
            result.status = "needs_review"
            result.message = "분기 조건은 충족하나 요구 필드 데이터 없음"
            return result, False
        if rt in ("value_in", "value_equals") and rule.get("field") in USE_FIELDS:
            result.status = "not_applicable"
            result.message = "용도 분류 불일치(정의성 조항) — 본 건물 적용 대상 아님"
            return result, False
        title = f"{check.get('title', '')} {check.get('code', '')}"
        if SUSPECT_PAT.search(title):
            # 정의·분류·특례성 조항의 '불만족'은 위반이 아니라 '해당 안 됨'일 가능성이
            # 높다(예: 국민주택규모 85㎡ = 분류 기준). 부적합 목록 오염 방지를 위해
            # 검토필요로 강등하되 의심 태그는 유지해 사람이 확인하게 한다.
            result.status = "needs_review"
            result.message = ((result.message + " · ") if result.message else "") + \
                "정의·분류성 조항 추정 — 불만족은 위반이 아닐 가능성, 원문 확인要"
            return result, True
        return result, False
    return result, False
DOMAIN_KO = {"site": "대지·배치", "massing": "형태·높이", "egress": "피난·방화",
             "mep": "설비", "parking": "주차", "landscape": "조경",
             "energy": "에너지", "fire": "소방", "structure": "구조",
             "accessibility": "장애인편의", "housing": "주택·세대",
             "documentation": "설계도서", "other": "기타"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join("output", "compliance_report_db.html"))
    ap.add_argument("--db", action="store_true", help="findings 를 DB에 적재")
    ap.add_argument("--project-name", default="주동 A (5BL) · 55A/55AS")
    args = ap.parse_args()

    ev = load_evaluator()
    derived = json.load(open(os.path.join(FO, "output", "derived_terms.json"),
                             encoding="utf-8"))
    terms = derived["terms"]
    prov = derived["provenance"]

    conn = get_conn()
    cur = conn.cursor()

    # --- 별칭 확장: term_id 값 → 룰 필드명(alias)들에도 복제 -------------------
    cur.execute("select alias, term_id from term_aliases where term_id = any(%s)",
                (list(terms.keys()),))
    drawing = dict(terms)
    alias_map = {}
    n_alias = 0
    for alias, tid in cur.fetchall():
        if alias in ALIAS_BLOCK:
            continue
        alias_map.setdefault(tid, []).append(alias)
        if alias not in drawing:
            drawing[alias] = terms[tid]
            n_alias += 1
    # 주의: term_id 말단 세그먼트 자동 확장은 하지 않는다 — building.height_m 과
    # floor.height_m 이 'height_m' 으로 충돌해 거짓 판정(예: 피난안전구역 높이에
    # 건물 높이 주입)을 만든다. 큐레이션된 term_aliases 만 신뢰.

    project = {
        "use_type": "공동주택",
        "floors_above": terms.get("building.floors_above_count"),
        "floors_below": terms.get("building.floors_below_count"),
        "height_m": terms.get("building.height_m"),
        "building_height_m": terms.get("building.height_m"),
        "household_count": terms.get("project.household_count"),
    }

    # --- 위상 사실(topology_facts) → 조건부 필드 공급/차단 ---------------------
    # 양옆거실 중복도: 위상이 '있음'이면 그 복도의 실제 폭을 해당 필드에 공급(정식 판정),
    # '없음'이 증명되면 그 필드를 쓰는 체크를 해당없음 처리. (일반 홀 폭의 무차별
    # 주입은 ALIAS_BLOCK 이 계속 차단)
    facts_path = os.path.join(FO, "output", "topology_facts.json")
    facts = (json.load(open(facts_path, encoding="utf-8"))
             if os.path.exists(facts_path) else {})
    no_dl_corridor = bool(facts) and not facts.get("double_loaded_corridor_exists", True)
    if facts.get("double_loaded_corridor_exists"):
        dl_ws = [c["width_m"] for c in facts.get("corridors", [])
                 if c.get("double_loaded") and c.get("width_m")]
        if dl_ws:
            drawing["corridor_effective_width_both_sides_m"] = min(dl_ws)
            print(f"위상: 양옆거실 중복도 있음 → 유효폭 {min(dl_ws)}m 공급 "
                  f"(BOT adjacentZone 근거)")
    elif no_dl_corridor:
        print("위상: 양옆거실 중복도 없음 증명 → 해당 체크 not_applicable 처리")

    # --- 체크 전체 로드 (+ 법령명·조문) ---------------------------------------
    cur.execute("""
      select c.id, c.code, c.title, c.domain, c.applicability, c.rule,
             c.required_drawing_fields, c.severity, c.source_excerpt,
             v.doc_title, v.article_no
      from checks c
      left join lateral (
        select doc_title, article_no from v_clauses v
        where v.clause_id = c.source_clause_id
        order by is_latest desc nulls last limit 1
      ) v on true
      where c.deprecated_at is null
      order by c.id""")
    rows = cur.fetchall()
    checks = []
    for (cid, code, title, domain, appl, rule, rdf, sev, excerpt,
         doc_title, article) in rows:
        checks.append({
            "id": cid, "code": code, "title": title, "domain": domain or "other",
            "applicability": appl or {}, "rule": rule or {},
            "required_drawing_fields": rdf or [], "severity": sev or "mandatory",
            "source_excerpt": excerpt or "",
            "law": doc_title or "", "article": article or "",
        })
    print(f"체크 로드: {len(checks)}개 · 도면값 {len(terms)} term (+별칭 {n_alias})")

    # --- 전수 평가 -----------------------------------------------------------
    results = ev.evaluate_project(checks, project, drawing)

    supplied = set(drawing.keys())
    findings = []
    for c, r in results:
        # BOT 위상 근거: 양옆거실 복도가 없음이 증명되면 해당 체크는 적용 대상 아님
        if no_dl_corridor and r.status != "not_applicable" \
                and rule_fields(c["rule"]) & BOTH_SIDES_FIELDS:
            r.status = "not_applicable"
            r.message = ("BOT 위상 검증: 양옆에 거실이 있는 복도 없음 "
                         "(순환공간 인접 분석 — topology_facts.json)")
        r, suspect = postprocess(c, r)
        rf = rule_fields(c["rule"])
        contributed = sorted(rf & supplied)
        findings.append({
            "suspect": suspect,
            "id": c["id"], "code": c["code"], "title": c["title"],
            "domain": c["domain"],
            "domain_ko": DOMAIN_KO.get(c["domain"], "기타"),
            "severity": c["severity"], "status": r.status,
            "actual": r.actual, "required": r.required,
            "message": r.message or "",
            "law": c["law"], "article": c["article"],
            "excerpt": c["source_excerpt"],
            "fields": c["required_drawing_fields"],
            "contributed": contributed,
        })

    counts = collections.Counter(f["status"] for f in findings)
    matrix = collections.defaultdict(collections.Counter)
    for f in findings:
        matrix[f["domain"]][f["status"]] += 1

    n_data = sum(1 for f in findings
                 if f["status"] in ("pass", "fail")
                 or (f["status"] == "needs_review" and f["contributed"]))
    print(f"판정: pass {counts['pass']} · fail {counts['fail']} · "
          f"needs_review {counts['needs_review']} · n/a {counts['not_applicable']}")
    print(f"BOT 데이터가 기여한 체크: {n_data}개")

    # --- 상세 표시 대상 선별 (리포트 용량 관리) --------------------------------
    order = {"fail": 0, "pass": 1, "needs_review": 2}
    detail = [f for f in findings if f["status"] in ("pass", "fail")]
    partial = [f for f in findings
               if f["status"] == "needs_review" and f["contributed"]]
    partial.sort(key=lambda f: (-len(f["contributed"]), f["id"]))
    CAP = 250
    detail += partial[:CAP]
    dropped_partial = max(0, len(partial) - CAP)
    detail.sort(key=lambda f: (order.get(f["status"], 9), f["id"]))

    def fmt(v):
        if isinstance(v, bool):
            return "있음" if v else "없음"
        if isinstance(v, float):
            s = f"{v:,.2f}".rstrip("0").rstrip(".")
            return s
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False)
        return "" if v is None else str(v)

    detail_json = json.dumps([{
        **{k: f[k] for k in ("code", "title", "domain", "domain_ko", "severity",
                             "status", "message", "law", "article", "excerpt",
                             "fields", "contributed", "suspect")},
        "actual": fmt(f["actual"]), "required": fmt(f["required"]),
    } for f in detail], ensure_ascii=False)

    # --- 매트릭스/입력값 테이블 HTML ------------------------------------------
    dom_order = sorted(matrix, key=lambda d: -(matrix[d]["fail"] * 10000
                                               + matrix[d]["pass"] * 100
                                               + sum(matrix[d].values())))
    mrows = ""
    for d in dom_order:
        m = matrix[d]
        tot = sum(m.values())
        mrows += (f'<tr><td>{html.escape(DOMAIN_KO.get(d, d))}</td>'
                  f'<td class="c p">{m["pass"] or ""}</td>'
                  f'<td class="c f">{m["fail"] or ""}</td>'
                  f'<td class="c r">{m["needs_review"] or ""}</td>'
                  f'<td class="c n">{m["not_applicable"] or ""}</td>'
                  f'<td class="c t">{tot}</td></tr>')
    trows = "".join(
        f'<tr><td>{html.escape(t)}</td><td>{fmt(v)}</td>'
        f'<td class="src">{html.escape(prov.get(t, ""))}</td></tr>'
        for t, v in terms.items())

    drawing_data = build_drawing(alias_map)
    print(f"도면 패널: {', '.join(drawing_data)} "
          f"(벽 path {sum(len(d['walls']) for d in drawing_data.values())//1024}KB)")

    # 템플릿 파일(report_template.html)의 __TOKEN__ 치환 — 중괄호 이스케이프 불필요
    tpl_path = os.path.join(FO, "report_template.html")
    page = open(tpl_path, encoding="utf-8").read()
    for token, val in {
        "__PROJECT__": html.escape(args.project_name),
        "__TODAY__": datetime.date.today().isoformat(),
        "__NTOTAL__": f"{len(findings):,}",
        "__NPASS__": f"{counts['pass']:,}",
        "__NFAIL__": f"{counts['fail']:,}",
        "__NREVIEW__": f"{counts['needs_review']:,}",
        "__NNA__": f"{counts['not_applicable']:,}",
        "__NDETAIL__": f"{len(detail):,}",
        "__NTERMS__": str(len(terms)),
        "__NDATA__": f"{n_data:,}",
        "__DROPNOTE__": (f" (부분기여 검토필요 {dropped_partial}건은 지면상 생략)"
                         if dropped_partial else ""),
        "__MATRIX_ROWS__": mrows,
        "__TERM_ROWS__": trows,
        "__DETAIL_JSON__": detail_json,
        "__DRAWING_JSON__": json.dumps(drawing_data, ensure_ascii=False),
    }.items():
        page = page.replace(token, val)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    open(args.out, "w", encoding="utf-8").write(page)
    print(f"리포트: {args.out} (상세 {len(detail)}건 수록)")

    # --- DB 적재 --------------------------------------------------------------
    if args.db:
        cur.execute("""insert into projects (id, name, sido, sigungu, use_type, parameters)
                       values (gen_random_uuid(), %s, %s, %s, %s, %s) returning id""",
                    (args.project_name + " — BOT 자동검토",
                     "미상(도면 미기재)", "미상(도면 미기재)", "공동주택",
                     json.dumps({"project": project, "terms": terms},
                                ensure_ascii=False)))
        pid = cur.fetchone()[0]
        cur.execute("""insert into analyses (id, project_id, status,
                         dataset_snapshot_at, applied_check_ids, model_name, summary)
                       values (gen_random_uuid(), %s, 'complete', now(), %s, %s, %s)
                       returning id""",
                    (pid, [f["id"] for f in findings], "bot-pipeline/claude-fable-5",
                     f"pass {counts['pass']} fail {counts['fail']} "
                     f"review {counts['needs_review']} n/a {counts['not_applicable']}"))
        aid = cur.fetchone()[0]
        ins = [(aid, f["id"],
                json.dumps({k: drawing[k] for k in f["contributed"]},
                           ensure_ascii=False, default=str),
                f["status"],
                json.dumps(f["actual"], ensure_ascii=False, default=str),
                json.dumps(f["required"], ensure_ascii=False, default=str),
                f["message"][:500])
               for f in findings if f["status"] != "not_applicable"]
        cur.executemany("""insert into findings
              (analysis_id, check_id, extracted_values, status, actual, required, message)
              values (%s,%s,%s,%s,%s,%s,%s)""", ins)
        conn.commit()
        print(f"DB 적재: project={pid} analysis={aid} findings={len(ins)}건")
    conn.close()


if __name__ == "__main__":
    main()
