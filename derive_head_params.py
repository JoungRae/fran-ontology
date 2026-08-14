# -*- coding: utf-8 -*-
"""건물 설정 → 스프링클러 배치 파라미터. cons_law 의 legal_rule 에서 끌어온다.

옛 fetch_head_checks.py 를 대체한다. 그쪽은 얼려 둔 checks(파편 규칙)를 통째로
넘겨서 판정 문구를 손으로 썼는데, 이제 legal_rule 은 조건(rule_condition)·수치
(rule_requirement)·단서(rule_override)가 구조화돼 있어 **건물 설정과 기계로
맞춰진다** — "내화구조면 2.1 대신 2.3" 을 코드가 아니라 데이터가 정한다.

정직함이 규칙이다:
  · 법령DB 에서 온 값에는 rule id 와 원문을 단다.
  · DB 에서 못 찾으면 하드코딩 기본값으로 물러나되 출처를 '하드코딩' 으로 표시한다.
  · 엔진 내부 상수(격자 크기 등)는 법이 아니므로 처음부터 '엔진' 으로 표시한다.
  화면은 이 출처 표시를 그대로 보여 준다 — 어떤 값이 법이고 어떤 값이 가정인지.

실행: cons_law venv 로 (psycopg 필요)
  D:/Python_test/cons_law/.venv/Scripts/python.exe derive_head_params.py
  → output/head_params.json
"""
import io
import json
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"D:\Python_test\cons_law\src")

from cons_law.config import connect  # noqa: E402

FO = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(FO, "data", "building_profile.json")
OUT = os.path.join(FO, "output", "head_params.json")

# 스프링클러를 다루는 문서들. 103 은 일반 기준, 608 은 공동주택 강화 기준.
DOCS = ("%NFPC 103%", "%NFTC 103%", "%NFPC 608%", "%NFTC 608%")


# ── 사실(facts) — 건물 설정 + 실 바인딩을 조건 매칭이 먹을 수 있는 꼴로 ──────
def load_bindings(base: str) -> dict | None:
    p = os.path.join(FO, "output", f"{base}_room_bindings.json")
    if base and os.path.exists(p):
        return json.load(open(p, encoding="utf-8")).get("바인딩", {})
    return None


def build_facts(profile: dict, cur, bindings: dict | None = None) -> dict:
    use = profile.get("용도", "")
    # 용도의 조상까지 사실로 넣는다. 조문이 "공동주택" 이라고만 써도
    # 아파트에 걸려야 한다 — use_taxonomy 가 그 계보를 안다.
    names = {use}
    cur.execute("""SELECT group_name FROM use_taxonomy WHERE item_name = %s""", (use,))
    names |= {r[0] for r in cur.fetchall() if r[0]}
    floor = profile.get("층", {})
    # 층 깃발(무대부·랙크식창고 등)의 출처는 두 갈래다.
    #   · 도면 — 실명에 그 말이 있으면 확실히 '있다'(True)
    #   · 설정 — 도면에서 못 찾았다고 '없다'로 단정하면 안 된다. 무대부를
    #            '스테이지'라 적은 도면에서 1.7m 규칙이 조용히 빠진다.
    #            그래서 못 찾으면 None(모름)으로 두고 profile 값으로 물러난다.
    manual = {"무대부": floor.get("무대부"),
              "특수가연물": floor.get("특수가연물_저장취급"),
              "랙크식": floor.get("랙크식창고"),
              "전기차": floor.get("전기차충전구역")}
    if bindings:
        found = {k: (True if any(k in name for name in bindings) else None)
                 for k in manual}
        flags = {k: (found[k] if found[k] is not None else manual[k])
                 for k in manual}
        hit = [k for k, v in found.items() if v]
        unit_found = (any(b.get("기본동작") == "세대 반경 2.6m"
                          for b in bindings.values())
                      or floor.get("세대있음"))
        flag_src = (f"실명 {len(bindings)}개에서 확인"
                    + (f": {', '.join(hit)}" if hit else " (해당 없음)")
                    + " · 나머지는 building_profile.json")
    else:
        flags = manual
        unit_found = floor.get("세대있음")
        flag_src = "building_profile.json 수동 입력"
    return {
        "use": names,
        "structure": profile.get("구조", ""),
        "storeys": profile.get("층수_지상"),
        "flags": flags,
        "세대있음": unit_found,
        "깃발출처": flag_src,
    }


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def eval_condition(c: dict, facts: dict):
    """조건 한 줄 → True / False / None(모름)."""
    measure, op, value = c["measure"], (c["op"] or "").strip(), str(c["value"] or "")
    got = None
    if measure == "use":
        toks = set(re.split(r"[·,/\s]+", value)) - {""}
        got = bool(toks & facts["use"]) if toks else None
        # 조문이 든 용도 중 아무것도 우리 용도가 아니면 False 로 본다
        if got is False and toks:
            got = False
    elif measure in ("structure", "fire_resistance"):
        s = facts["structure"]
        got = (s in value or value in s) if s and value else None
    elif measure == "storeys":
        n, v = facts["storeys"], _num(value)
        if n is not None and v is not None:
            got = {"<": n < v, "<=": n <= v, ">": n > v, ">=": n >= v,
                   "=": n == v}.get(op)
    else:
        # 장소 성격 낱말 — 설정에 있는 깃발과 맞춰 본다
        for key, flag in facts["flags"].items():
            if key in value or key in (c.get("raw_text") or ""):
                got = flag        # True/False/None 그대로
                break
    if got is not None and c.get("negated"):
        got = not got
    return got


def eval_rule(conds: list, facts: dict) -> str:
    """규칙 판정 — 적용 / 비적용 / 미확인. 같은 group=AND, 다른 group=OR."""
    if not conds:
        return "적용"                       # 무조건 규칙
    groups: dict[int, list] = {}
    for c in conds:
        groups.setdefault(c["group_no"], []).append(eval_condition(c, facts))
    results = []
    for g in groups.values():
        if any(x is False for x in g):
            results.append(False)
        elif any(x is None for x in g):
            results.append(None)
        else:
            results.append(True)
    if any(r is True for r in results):
        return "적용"
    if any(r is None for r in results):
        return "미확인"
    return "비적용"


# ── DB 에서 스프링클러 규칙 전부 ─────────────────────────────────────────
def load_rules(cur) -> list[dict]:
    cur.execute(f"""
        SELECT r.id, d.title, r.article_no, r.item, r.local_key, r.deontic,
               r.strength, COALESCE(n.name, r.subject_raw), r.statement, r.raw_text
        FROM legal_rule r
        JOIN documents d ON d.id = r.document_id
        LEFT JOIN nodes n ON n.id = r.subject_node_id
        WHERE {" OR ".join("d.title LIKE %s" for _ in DOCS)}
        ORDER BY d.title, r.article_no, r.local_key""", DOCS)
    rules = {row[0]: {"id": row[0], "doc": row[1], "article": row[2],
                      "item": row[3], "key": row[4], "deontic": row[5],
                      "strength": row[6], "subject": row[7], "statement": row[8],
                      "raw": row[9], "conds": [], "reqs": [], "overrides": []}
             for row in cur.fetchall()}
    ids = list(rules)
    cur.execute("""SELECT rule_id, group_no, node_raw, measure, op, value, unit,
                          formula, value_ref, negated, raw_text
                   FROM rule_condition WHERE rule_id = ANY(%s)""", (ids,))
    for r in cur.fetchall():
        rules[r[0]]["conds"].append(dict(zip(
            ("rule_id", "group_no", "node", "measure", "op", "value", "unit",
             "formula", "value_ref", "negated", "raw_text"), r)))
    cur.execute("""SELECT rule_id, option_group, node_raw, measure, op, value,
                          unit, formula, value_ref, raw_text
                   FROM rule_requirement WHERE rule_id = ANY(%s)""", (ids,))
    for r in cur.fetchall():
        rules[r[0]]["reqs"].append(dict(zip(
            ("rule_id", "option", "node", "measure", "op", "value", "unit",
             "formula", "value_ref", "raw_text"), r)))
    cur.execute("""SELECT over_rule_id, under_rule_id, effect FROM rule_override
                   WHERE over_rule_id = ANY(%s)""", (ids,))
    for a, b, eff in cur.fetchall():
        if a in rules:
            rules[a]["overrides"].append({"under": b, "effect": eff})
    return list(rules.values())


def cite(rule: dict) -> str:
    where = rule["item"] or rule["article"]
    return f"{rule['doc']} {where} — 규칙 #{rule['id']}({rule['key']})"


# ── 평면도 법률 검토 조문 묶음 (plan_law_report 가 소비) ─────────────────
def derive_plan_review(cur) -> dict:
    """방화구획(§14①1.)·지하층 비상탈출구(§25①1.)·직통계단 2개소(§34②)를
    주소 닻으로 수확한다. 규칙·조건·요건·면제 연결을 그대로 나르고, 어느
    값이 적용되는지는 소비자(plan_law_report)가 도면 측정값·건물 사실로
    정한다 — 수치는 전부 DB 에서 온다(1,000/3,000㎡ · 50㎡ · 200㎡ · 2개소)."""
    ANCHORS = [
        ("방화구획", "%피난%방화구조%", "제14조", "①", "1."),
        ("비상탈출구", "%피난%방화구조%", "제25조", "①", "1."),
        ("직통계단수", "건축법 시행령", "제34조", "②", None),
    ]
    out = {}
    for key, pat, art, para, item in ANCHORS:
        cur.execute("""
          SELECT r.id, d.title, r.para, r.item, r.local_key, r.deontic,
                 left(COALESCE(r.raw_text, r.statement), 320)
          FROM legal_rule r JOIN documents d ON d.id=r.document_id
          WHERE d.title LIKE %s AND r.article_no=%s AND r.para=%s
            AND (%s::text IS NULL OR r.item=%s)
          ORDER BY r.id""", (pat, art, para, item, item))
        rules = {}
        doc_title = ""
        for rid, dt, p, it, k, de, raw in cur.fetchall():
            doc_title = dt
            rules[rid] = {"id": rid, "para": p, "item": it, "key": k,
                          "deontic": de, "원문": raw,
                          "조건": [], "요건": [], "끄는의무": []}
        ids = list(rules)
        if not ids:
            out[key] = {}
            continue
        cur.execute("""SELECT rule_id, group_no, node_raw, measure, op, value,
                              unit, left(raw_text, 120)
                       FROM rule_condition WHERE rule_id = ANY(%s)""", (ids,))
        for rid, g, node, me, op, v, u, raw in cur.fetchall():
            rules[rid]["조건"].append({"group": g, "node": node, "measure": me,
                                     "op": op, "value": v, "unit": u, "원문": raw})
        cur.execute("""SELECT rule_id, node_raw, measure, op, value, unit,
                              left(raw_text, 120)
                       FROM rule_requirement WHERE rule_id = ANY(%s)""", (ids,))
        for rid, node, me, op, v, u, raw in cur.fetchall():
            rules[rid]["요건"].append({"node": node, "measure": me, "op": op,
                                     "value": v, "unit": u, "원문": raw})
        cur.execute("""SELECT over_rule_id, under_rule_id, effect
                       FROM rule_override WHERE over_rule_id = ANY(%s)
                         AND under_rule_id IS NOT NULL""", (ids,))
        for over, under, eff in cur.fetchall():
            rules[over]["끄는의무"].append({"under": under, "effect": eff})
        out[key] = {"조문": f"{doc_title} {art}{para}"
                          + (f" {item}호" if item else ""),
                    "규칙": list(rules.values())}
    return out


# ── 피난 보행거리 한도 — 건축법 시행령 §34① (evac_report 가 소비) ──────────
def derive_evac_limits(cur, profile: dict) -> dict:
    """원칙(의무)과 완화(단서) 한도를 주소 닻으로 수확하고, 건물 사실로
    어느 완화가 적용되는지까지 고른다. 수치는 전부 DB 에서 온다 —
    evac_report 의 30/50 상수는 이 결과가 없을 때의 폴백일 뿐이다.

    적용 선택은 단서의 문면 조건을 사실과 대조한다(공장/내화·불연/16층).
    낱말 대조라 거친 편이지만, 선택 '메뉴' 전체(rule_id·원문)를 함께 실어
    화면이 근거를 보여 주고 사람이 검증할 수 있게 한다.
    """
    cur.execute("""
      SELECT r.id, r.local_key, r.deontic, COALESCE(r.raw_text, r.statement),
             q.value, q.unit, q.raw_text
      FROM legal_rule r
      JOIN documents d ON d.id = r.document_id
      JOIN rule_requirement q ON q.rule_id = r.id AND q.measure = 'distance'
      WHERE d.title = '건축법 시행령' AND r.article_no = '제34조'
        AND r.para = '①' AND q.value ~ '^[0-9.]+$'
      ORDER BY r.id, q.value::numeric""")
    principle, menu = None, []
    for rid, key, de, raw, val, unit, qraw in cur.fetchall():
        m = mm(val, unit)
        if m is None:
            continue
        row = {"rule_id": rid, "key": key, "한도_m": m / 1000,
               "조건원문": (qraw or "")[:160], "규칙원문": (raw or "")[:260]}
        if de == "obligation":
            if principle is None or row["한도_m"] < principle["한도_m"]:
                principle = row
        else:
            menu.append(row)
    if principle is None:
        return {}

    # 건물 사실로 적용 완화를 고른다
    use = profile.get("용도") or ""
    fire = any(w in (profile.get("구조") or "") for w in ("내화", "불연"))
    apt16 = (any(w in use for w in ("아파트", "공동주택"))
             and (profile.get("층수_지상") or 0) >= 16)
    fl_name = (profile.get("층") or {}).get("이름") or ""
    _m = re.search(r"(\d+)\s*층", fl_name)
    fl_16up = ("지하" not in fl_name) and _m is not None and int(_m.group(1)) >= 16

    picked, why = None, []
    for row in menu:
        t = row["조건원문"] + row["규칙원문"]
        if "공장" in t:
            continue                      # 용도가 공장일 때만 — 아파트면 비적용
        if "내화" in t or "불연" in t or "16층" in t:
            if not fire:
                continue
            if "16층" in row["조건원문"] and not (apt16 and fl_16up):
                continue                  # 40m 는 16층 이상 '층'에만
            if picked is None or row["한도_m"] < picked["한도_m"]:
                picked = row              # 적용 가능한 것 중 엄격한 값
    if picked:
        why.append(f"주요구조부 {profile.get('구조', '')}(건물 사실·설계 가정)")
        if "16층" in picked["조건원문"]:
            why.append(f"{use} 지상 {profile.get('층수_지상')}층 · 현재 층 16층 이상")
        elif apt16:
            why.append(f"현재 층({fl_name})은 16층 이상 층이 아니라 40m 대신 50m")
    return {"조문": "건축법 시행령 제34조 제1항",   # 수확 닻 그대로 — 표시용
            "원칙": principle, "완화메뉴": menu,
            "적용": picked, "적용사유": " · ".join(why)}


# ── 파라미터 뽑기 — 닻(doc·조·수치 모양)으로 찾고, 없으면 하드코딩 표시 ────
def mm(value, unit) -> float | None:
    v = _num(value)
    if v is None:
        return None
    u = (unit or "").strip()
    if u in ("m", "미터"):
        return v * 1000
    if u in ("㎝", "cm", "센티미터"):
        return v * 10
    if u in ("㎜", "mm", "밀리미터"):
        return v
    # 단위가 없으면 짐작하지 않는다. 2.7.7.1 의 '60'(㎝)을 60,000mm 로 읽는
    # 100배 오류가 조용히 배치에 반영되는 것보다, 값을 안 쓰는 편이 낫다.
    return None


def find(rules, doc, item=None, article=None, req_val=None, req_measure=None):
    """닻으로 규칙 하나를 찾는다. local_key 는 재추출마다 흔들려서 닻이 아니다."""
    out = []
    for r in rules:
        if doc not in r["doc"]:
            continue
        if item and not (r["item"] or "").startswith(item):
            continue
        if article and r["article"] != article:
            continue
        for q in r["reqs"]:
            if req_measure and q["measure"] != req_measure:
                continue
            if req_val is not None and _num(q["value"]) != req_val:
                continue
            out.append((r, q))
            break
    return out


def parse_table_html(html_str: str) -> list[list[str]]:
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html_str, re.S | re.I)
    out = []
    for tr in rows:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)
        out.append([re.sub(r"<[^>]+>", "", c).strip() for c in cells])
    return out


def derive_beam_table(rules):
    """표 2.7.8 을 전사본에서 읽는다. 근거 규칙(표에 따르라는 의무)도 함께."""
    anchor = next((r for r in rules if "NFTC 103" in r["doc"]
                   and "2.7.8" in (r["statement"] or "")), None)
    with connect() as cn, cn.cursor() as cu:
        cu.execute("""SELECT si.fl_seq, si.content
                      FROM clause_images ci
                      JOIN current_clauses c ON c.clause_id = ci.clause_id
                      JOIN source_images si ON si.fl_seq = ci.fl_seq
                      WHERE c.doc_title LIKE '%NFTC 103%'
                        AND c.article_no LIKE '2.7.8%'
                        AND si.kind = 'table' LIMIT 1""")
        row = cu.fetchone()
    if not row:
        return [], anchor
    fl_seq, content = row
    table = parse_table_html(content)
    out = []
    for cells in table[1:]:                  # 첫 행은 머리
        if len(cells) >= 2:
            out.append({"수평거리": cells[0], "수직거리한계": cells[1],
                        "출처": f"표 2.7.8 전사 (이미지 {fl_seq})"})
    return out, anchor


def derive(rules, facts, profile):
    params, notes = [], []

    def add(key, label, value_mm, source, rule=None, req=None, cond_note="",
            scope=""):
        p = {"key": key, "이름": label, "값_mm": value_mm, "출처": source,
             "적용범위": scope}
        if rule:
            # 원문 = 조문 전체(근거 표시용), 요건원문 = 그 안의 수치 조각.
            # 예전에는 조각만 남겨서 "2.3 m 이하" 처럼 적용 범위가 사라졌다 —
            # 화면의 근거란에 그대로 쓰면 "아파트등의 세대 내" 같은 한정이 증발한다.
            p |= {"rule_id": rule["id"], "근거": cite(rule),
                  "원문": rule["raw"] or rule["statement"],
                  "요건원문": (req or {}).get("raw_text"),
                  "조건": cond_note}
        params.append(p)

    # 1) 공용부 수평거리 — 2.1 기본, 조건이 갈라 놓은 1.7(무대부)·2.3(내화) 중
    #    조건이 '적용' 으로 판정된 가장 구체적인 규칙을 고른다.
    # 후보 선별은 **구조적 닻**으로 한다. 예전에는 값이 (2.1, 1.7, 2.3, 2.6) 중
    # 하나인지로 걸렀는데, 그러면 (a) 법이 다른 값을 주면 조용히 버려지고
    # (b) 수평거리가 아닌 거리들(부착면 55㎝·개구부 15㎝·30㎝)이 값만 다를 뿐
    # 같은 자격으로 후보에 남는다. 지금은 조문 번호로 가른다 —
    # 2.7.3 이 헤드 수평거리 항이고, 항 번호 없는 성능기준(NFPC) 조문도 받는다.
    # 항 번호가 없는 규칙(NFPC 성능기준 등)은 번호로 못 가리므로 요건 문구가
    # '수평거리' 인지까지 본다 — 같은 2.7 안에 '부착 면과의 거리 55㎝'(#11158)
    # 처럼 헤드 간격이 아닌 거리가 섞여 있다.
    def is_spacing(r, q):
        item = r["item"] or ""
        if item.startswith("2.7.3"):
            return True
        if item:
            return False
        return "수평거리" in ((q.get("raw_text") or "") + (q.get("measure_raw") or ""))

    cands, dropped = [], []
    for r, q in find(rules, "NFTC 103", article="2.7", req_measure="distance"):
        v_mm = mm(q["value"], q["unit"])
        why = None
        if not is_spacing(r, q):
            why = (f"헤드 수평거리 규정 아님 — {r['item'] or '항번호 없음'}"
                   f" / {(q.get('raw_text') or '')[:28]}")
        elif v_mm is None:
            why = f"값·단위를 읽을 수 없음 — value={q['value']!r} unit={q['unit']!r}"
        if why:
            dropped.append(f"#{r['id']} {v_mm or q['value']}: {why}")
            continue
        cands.append((r, q, v_mm, eval_rule(r["conds"], facts), len(r["conds"])))
    # 결정적 정렬: 조건 많은(구체적인) 순 → 값 작은(보수적인) 순 → id 순.
    # 예전에는 동률이면 DB 반환 순서가 결정했다.
    cands.sort(key=lambda x: (-x[4], x[2], x[0]["id"]))
    chosen = None
    for r, q, v_mm, verdict, nc in cands:
        if verdict == "적용" and nc > 0:
            chosen = (r, q, v_mm, "조건 충족: " +
                      "; ".join(c["raw_text"] for c in r["conds"])[:80])
            break
    if not chosen:
        base = [(r, q, v_mm) for r, q, v_mm, verdict, nc in cands
                if nc == 0 and verdict == "적용"]
        if base:
            r, q, v_mm = base[0]     # 위 정렬로 값이 가장 작은 무조건 규칙
            chosen = (r, q, v_mm, "무조건(기본값)")
    for d in dropped:
        notes.append(f"공용 수평거리 후보 제외 — {d}")
    if chosen:
        r, q, v_mm, note = chosen
        add("r_common", "헤드 수평거리(공용·일반)", v_mm, "법령DB", r, q, note,
            "세대 아닌 모든 실")
    else:
        add("r_common", "헤드 수평거리(공용·일반)", 2300, "하드코딩",
            scope="세대 아닌 모든 실")
        notes.append("수평거리 규칙을 DB에서 못 찾아 하드코딩 값 사용")

    # 2) 세대 내 수평거리 — 공동주택(608) 전용 강화 기준
    got = find(rules, "NFTC 608", req_measure="distance", req_val=2.6)
    if got and profile.get("용도") in facts["use"]:
        r, q = got[0]
        verdict = eval_rule(r["conds"], facts)
        add("r_unit", "헤드 수평거리(세대 내)", mm(q["value"], q["unit"]),
            "법령DB", r, q,
            f"용도={profile.get('용도')} → 공동주택 기준 적용 ({verdict})",
            "세대(주거) 실" + ("" if facts.get("세대있음")
                             else " — 이 층에는 없음"))
    else:
        add("r_unit", "헤드 수평거리(세대 내)", 2600, "하드코딩", scope="세대 실")

    # 3) 외벽 창문 0.6m (608)
    got = find(rules, "NFTC 608", req_val=0.6)
    if got:
        r, q = got[0]
        exs = [x for x in rules if any(o["under"] == r["id"]
                                       for o in x["overrides"])]
        add("window_band", "외벽 창문 헤드 배치 폭", mm(q["value"], q["unit"]),
            "법령DB", r, q,
            "예외: " + " / ".join(e["statement"][:40] for e in exs) if exs else "",
            "세대 외벽 창")
    else:
        add("window_band", "외벽 창문 헤드 배치 폭", 600, "하드코딩",
            scope="세대 외벽 창")

    # 4) 살수장애 반경 60cm (2.7.7.1) · 벽 이격 10cm · 부착면 30cm
    for item, key, label, val, unit_scope in (
            ("2.7.7.1", "clear_head", "헤드 주위 확보 공간", 60, "모든 헤드"),
            ("2.7.7.2", "deflector_gap", "헤드↔부착면 거리(≤)", 30, "모든 헤드")):
        got = find(rules, "NFTC 103", item=item, req_val=val)
        if got:
            r, q = got[0]
            add(key, label, mm(q["value"], q["unit"]), "법령DB", r, q, "",
                unit_scope)
        else:
            add(key, label, val * 10, "하드코딩", scope=unit_scope)
    got = find(rules, "NFTC 103", item="2.7.7.1", req_val=10)
    if got:
        r, q = got[0]
        add("wall_gap", "벽↔헤드 공간(≥)", mm(q["value"], q["unit"]),
            "법령DB", r, q, "", "벽 곁 헤드")
    else:
        add("wall_gap", "벽↔헤드 공간(≥)", 100, "하드코딩", scope="벽 곁 헤드")

    # 5) 장애물 폭 3배 식 (2.7.7.3 단서)
    got = [(r, c) for r in rules for c in r["conds"]
           if "NFTC 103" in r["doc"] and (r["item"] or "").startswith("2.7.7.3")
           and c.get("formula")]
    if got:
        r, c = got[0]
        add("obstacle_formula", "장애물 이격(식)", None, "법령DB", r,
            {"raw_text": c["raw_text"]}, f"식: {c['formula']}", "보·배관·조명 곁")
    else:
        add("obstacle_formula", "장애물 이격(식)", None, "하드코딩",
            scope="장애물 폭 × 3")

    # 6) 보 곁 헤드 — 표 2.7.8
    # 표 값은 LLM 추출이 아니라 **전사본에서 직접** 읽는다. 추출은 실행마다
    # 표를 규칙 4개로 펼치기도, "표에 따라" 한 줄로 두기도 한다(비결정).
    # 전사본(source_images)은 사람이 검산한 고정 데이터라 닻으로 더 낫다.
    beam_rows, beam_anchor = derive_beam_table(rules)

    if beam_rows:
        params.append({"key": "beam_table", "이름": "보 곁 헤드 (표 2.7.8)",
                       "값_mm": None, "출처": "법령DB",
                       "rule_id": beam_anchor["id"] if beam_anchor else None,
                       "근거": cite(beam_anchor) if beam_anchor else "표 전사",
                       "조건": f"{len(beam_rows)}구간 표 — 아래 보표 참조",
                       "적용범위": "보와 가장 가까운 헤드"})
    else:
        params.append({"key": "beam_table", "이름": "보 곁 헤드 (표 2.7.8)",
                       "값_mm": None, "출처": "하드코딩",
                       "설명": "표 전사를 못 찾음 — 엔진 회피 폭만 적용"})

    # 7) 헤드 제외 장소 — "설치하지 않을 수 있다" 는 규칙 전부.
    # 예전에는 NFTC 103 의 2.12.1 만 훑었는데, 그러면 공동주택 전용 제외 조항이
    # 통째로 빠진다 — 특히 NFTC 608 2.3.1.8(건축법 시행령 §46④ 대피공간).
    # 그 결과 대피공간 제외가 법령이 아니라 코드의 실명 검사로 처리되고 있었다.
    # 2.12.1.x 는 장소 나열이라 "않을 수 있" 이 그 줄에 없다(상위 2.12.1 문장에
    # 있다) — 조 번호로 잡는다. 608 쪽은 번호 규칙이 다르므로 문면으로 잡는다.
    exclusions = []
    for r in rules:
        item = r["item"] or ""
        txt = r["raw"] or r["statement"] or ""
        hit = (("NFTC 103" in r["doc"] and item.startswith("2.12.1"))
               or ("NFTC 608" in r["doc"] and r["deontic"] == "permission"
                   and "헤드" in txt and "설치하지 않을 수 있" in txt))
        if hit:
            exclusions.append({"rule_id": r["id"], "item": item or r["article"],
                               "doc": r["doc"], "원문": txt[:200]})

    # 8) 엔진 내부 상수 — 법이 아니다. 처음부터 그렇게 표시한다.
    for key, label, val, why in (
            ("grid", "검증 격자 크기", 100,
             "커버 전수검증의 해상도. 법 아님 — 작을수록 정밀·느림"),
            ("window_split", "창 구간 분할 간격", 5060,
             "유도값 2√(r²−0.6²), r=2.6m. 창 전체가 헤드 반경에 들도록"),
            ("beam_avoid", "보 좌우 회피 폭", 600,
             "2.7.7.1 의 60cm 를 보 회피에 적용한 엔진 해석")):
        params.append({"key": key, "이름": label, "값_mm": val, "출처": "엔진",
                       "설명": why})

    # 9) 판정 목록 — 스프링클러 규칙 전부에 프로필 대조 결과
    verdicts = []
    for r in rules:
        v = eval_rule(r["conds"], facts)
        verdicts.append({
            "rule_id": r["id"], "근거": cite(r), "deontic": r["deontic"],
            "대상": r["subject"], "내용": r["statement"],
            "판정": v,
            "조건": [c["raw_text"] for c in r["conds"]][:4]})

    return params, beam_rows, exclusions, verdicts, notes


def main():
    base = next((a for a in sys.argv[1:] if not a.startswith("--")), "")
    profile = json.load(open(PROFILE, encoding="utf-8"))
    bindings = load_bindings(base)
    with connect() as cn, cn.cursor() as cur:
        facts = build_facts(profile, cur, bindings)
        rules = load_rules(cur)
        evac = derive_evac_limits(cur, profile)
        plan = derive_plan_review(cur)
    print(f"층 깃발 출처: {facts['깃발출처']} → {facts['flags']}"
          f" · 세대 {'있음' if facts['세대있음'] else '없음'}")
    params, beams, excl, verdicts, notes = derive(rules, facts, profile)

    from collections import Counter
    vc = Counter(v["판정"] for v in verdicts)
    out = {"프로필": profile,
           "사실": {"용도(조상포함)": sorted(facts["use"]), "깃발출처": facts["깃발출처"],
                    "세대있음": facts.get("세대있음"),
                    "구조": facts["structure"], "지상층수": facts["storeys"]},
           "파라미터": params, "보표_2_7_8": beams, "제외장소": excl,
           "피난한도": evac, "평면검토": plan,
           "판정요약": dict(vc), "적용규칙": verdicts, "비고": notes}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT + ".tmp", "w", encoding="utf-8") as f:   # 원자적 교체
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(OUT + ".tmp", OUT)

    print(f"규칙 {len(rules)}건 대조 → {dict(vc)}")
    print(f"\n파라미터 {len(params)}개:")
    for p in params:
        src = p["출처"]
        v = f"{p['값_mm']:.0f}mm" if p.get("값_mm") else "-"
        extra = p.get("조건") or p.get("설명") or ""
        print(f"  [{src:4}] {p['이름']:20} {v:8} {extra[:52]}")
    print(f"\n보표 2.7.8: {len(beams)}행 · 제외장소 {len(excl)}곳")
    print("평면검토 수확: " + " · ".join(
        f"{k} {len(v.get('규칙', []))}규칙" for k, v in plan.items()))
    if evac:
        _ap = evac.get("적용")
        print(f"피난한도(§34①): 원칙 {evac['원칙']['한도_m']:.0f}m"
              + (f" → 완화 {_ap['한도_m']:.0f}m 적용 (#{_ap['rule_id']}, "
                 f"{evac['적용사유']})" if _ap else " (완화 비적용)"))
    print(f"→ {OUT}")


if __name__ == "__main__":
    main()
