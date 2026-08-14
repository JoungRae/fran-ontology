# -*- coding: utf-8 -*-
"""평면도 법률 검토 — 한 도면, 네 개의 법적 질문, 추적 가능한 판정.

  ① 피난 보행거리   건축법 시행령 §34①      (evac_summary 재사용)
  ② 방화구획        피난·방화규칙 §14① 1호   (층 면적 합산)
  ③ 지하층 비상탈출구 피난·방화규칙 §25① 1호   (거실 면적 + 계단 수 → 면제)
  ④ 직통계단 2개소   건축법 시행령 §34② 5호   (지하층 거실 200㎡ → 2개소)

법령 수치(30/50m·1,000/3,000㎡·50㎡·200㎡·2개소)는 전부 head_params.json
(derive_head_params 가 DB 에서 수확)에서 오고, 도면 측정값(면적·계단 수·
보행거리)과 건물 사실(내화구조 — 설계 가정)로 판정한다. 값마다 출처 배지:
법령DB(초록) · 도면 측정(파랑) · 가정(주황).

사용자 결정: 스프링클러 설치는 '가정'으로 표기한다(헤드 배치 결과를 근거로
쓰지 않는다). 재생(▶ 전체 검토)은 정적 페이지에서도 동작한다.

실행: 소스 venv (rdflib 불필요, numpy 불필요)
  python plan_law_report.py 지하1층_pit  →  output/<base>_law_review.html
선행:  derive_head_params.py(법령값) · evac_report.py(피난 요약)
"""
import html
import json
import math
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
FO = os.path.dirname(os.path.abspath(__file__))


def jload(p):
    return json.load(open(p, encoding="utf-8"))


def num(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else "지하1층_pit"
    rooms_data = jload(os.path.join(FO, "output", f"{base}_rooms_rect.json"))["rooms"]
    cats = jload(os.path.join(FO, "output",
                              f"{base}_layer_classification.json"))["categories"]
    ents = jload(os.path.join(FO, "data", f"{base}.json"))["Entities"]
    hp = jload(os.path.join(FO, "output", "head_params.json"))
    ev = jload(os.path.join(FO, "output", f"{base}_evac_summary.json"))
    profile = jload(os.path.join(FO, "data", "building_profile.json"))
    rtypes = {}
    try:
        rtypes = jload(os.path.join(FO, "data", "room_type_cache.json"))
    except Exception:
        pass

    PLAN = hp.get("평면검토") or {}

    def rules_of(key):
        return (PLAN.get(key) or {}).get("규칙", [])

    def cite_of(key):
        return (PLAN.get(key) or {}).get("조문", "")

    # ── 도면 측정 ─────────────────────────────────────────────────────────
    def area(r):
        if r.get("area_m2"):
            return float(r["area_m2"])
        x0, y0, x1, y1 = r["rect"]
        return (x1 - x0) * (y1 - y0) / 1e6

    def is_shaft(n):
        return (rtypes.get(n) or {}).get("유형") == "샤프트"

    def is_living(n):
        u = n.upper()
        return not is_shaft(n) and "계단" not in n and "PIT" not in u and "피트" not in n

    total_area = sum(area(r) for r in rooms_data)
    living_area = sum(area(r) for r in rooms_data if is_living(r["room"]))
    stair_rooms = [r for r in rooms_data if "계단" in r["room"]]
    n_stairs = len(stair_rooms)
    floor_name = (profile.get("층") or {}).get("이름", "")
    is_basement = "지하" in floor_name
    fire_struct = any(w in (profile.get("구조") or "") for w in ("내화", "불연"))

    # ── 법령값 꺼내기 (전부 DB 수확분 — 없으면 그 항목은 '자료 없음') ─────
    # ② 방화구획: floor_area 요건 두 값 — 작은 것이 원칙, 큰 것이 완화(소화설비)
    comp_rule = next((r for r in rules_of("방화구획")
                      if r["deontic"] == "obligation"), None)
    comp_areas = sorted({num(q["value"]) for q in (comp_rule or {}).get("요건", [])
                         if q["measure"] == "floor_area" and num(q["value"])})
    comp_p, comp_r = (comp_areas[0], comp_areas[-1]) if comp_areas else (None, None)

    # ③ 비상탈출구: 의무(거실 면적 문턱) + 면제(계단 수)
    esc_duty = next((r for r in rules_of("비상탈출구")
                     if r["deontic"] == "obligation"), None)
    esc_ex = next((r for r in rules_of("비상탈출구")
                   if r["deontic"] == "permission"), None)
    esc_thr = next((num(c["value"]) for c in (esc_duty or {}).get("조건", [])
                    if c["measure"] == "floor_area"), None)
    esc_nst = next((num(c["value"]) for c in (esc_ex or {}).get("조건", [])
                    if c["measure"] == "count"), None)
    esc_link = next((l for l in (esc_ex or {}).get("끄는의무", [])
                     if l["effect"] == "exempt"), None)

    # ④ 직통계단 2개소: §34② 중 지하층 호 — 조건에 position=지하층 이 있는 규칙
    st_rule = next((r for r in rules_of("직통계단수")
                    if any(c.get("value") == "지하층" for c in r["조건"])), None)
    st_thr = next((num(c["value"]) for c in (st_rule or {}).get("조건", [])
                   if c["measure"] == "floor_area"), None)
    st_need = next((num(q["value"]) for q in (st_rule or {}).get("요건", [])
                    if q["measure"] == "count"), None)
    if st_need is None and st_rule:          # 요건에 수가 없으면 원문에서 유도
        m = re.search(r"(\d+)\s*개소", st_rule["원문"])
        st_need = float(m.group(1)) if m else None

    # ── 판정 ─────────────────────────────────────────────────────────────
    B = {"적합": "ok", "적합(완화)": "ok", "적합(완화·가정)": "ok",
         "불요(면제)": "exm", "부적합": "bad", "자료 없음": "na"}

    def badge(v):
        return f'<span class="vd {B.get(v, "na")}">{html.escape(v)}</span>'

    def src(kind, txt):
        cls = {"법": "law", "측": "meas", "가": "asm"}[kind]
        lab = {"법": "법령DB", "측": "도면 측정", "가": "가정"}[kind]
        return (f'<span class="src {cls}">{lab}</span> {txt}')

    checks = []

    # ① 피난 보행거리 — rule_id 는 화면에 안 쓴다(툴팁으로만). 출처는 법령명.
    wmax, lim, pri = ev["최악_m"], ev["한도_m"], ev["원칙_m"]
    cite1 = (hp.get("피난한도") or {}).get("조문") or "건축법 시행령 제34조 제1항"
    worst = max(ev["동선"], key=lambda p: p["m"])
    v1 = ("적합" if wmax <= pri else
          "적합(완화)" if ev["완화적용"] and wmax <= lim else
          "부적합" if wmax > lim else "적합(완화)")
    checks.append({
        "id": "evac", "no": "①", "title": "피난 보행거리", "v": v1,
        "one": f"최악 {wmax}m ≤ 완화 한도 {lim:.0f}m (원칙 {pri:.0f}m)",
        # 조항 원문 그대로, 같은 항은 카드 하나로 (본문+단서 이어 붙임)
        "law": [{"src": cite1,
                 "rid": f"{ev['원칙규칙']}·{ev['완화규칙']}",
                 "txt": (((hp.get("피난한도") or {}).get("원칙") or {})
                         .get("규칙원문", "") + " "
                         + (((hp.get("피난한도") or {}).get("적용") or {})
                            .get("규칙원문", ""))).strip()}],
        "meas": [f"실 {len(ev['실'])}곳의 가장 먼 지점에서 계단까지 전수 측정 — "
                 f"최악 {wmax}m, {worst['실']} (도면의 굵은 빨간 동선)",
                 f"계단 출입구 {len(ev['출입구'])}개 자동 인식"],
        "steps": [
            {"k": "기준", "law": "보행거리 한도 (원칙)", "src": cite1,
             "need": f"≤ {pri:.0f}m", "got": f"{wmax}m",
             "res": "✓ 충족" if wmax <= pri else "✗ 초과",
             "cls": "ok" if wmax <= pri else "bad"},
            {"pre": "↓ 단서 적용 — 주요구조부 내화구조 (가정)",
             "k": "완화", "law": "내화구조·불연재료 건물의 완화 한도",
             "src": f"{cite1} 단서",
             "need": f"≤ {lim:.0f}m", "got": f"{wmax}m",
             "res": "✓ 충족" if wmax <= lim else "✗ 초과",
             "cls": "ok" if wmax <= lim else "bad"},
        ],
        "asm": ["주요구조부 내화구조 — 건물 사실(설계 가정)"],
        "link": f"{base}_evac_layout.html|피난 경로 상세 리포트",
    })

    # ② 방화구획
    if comp_p:
        v2 = ("적합" if total_area <= comp_p else
              "적합(완화·가정)" if total_area <= comp_r else "부적합")
        one2 = (f"층 면적 {total_area:.0f}㎡ ≤ 원칙 {comp_p:.0f}㎡ — 층 전체 한 구획 가능"
                if total_area <= comp_p else
                f"층 면적 {total_area:.0f}㎡ ≤ {comp_r:.0f}㎡ (스프링클러 가정 시 완화)")
        cite2 = cite_of("방화구획") or "건축물의 피난ㆍ방화구조 등의 기준에 관한 규칙 제14조"
        checks.append({
            "id": "comp", "no": "②", "title": "방화구획 면적", "v": v2, "one": one2,
            "law": [{"src": cite2, "rid": comp_rule["id"],
                     "txt": comp_rule["원문"]}],
            "meas": [f"인식된 실 {len(rooms_data)}개 면적 합계 {total_area:.0f}㎡ "
                     "(라벨 없는 통로 제외 — 하한값, 도면의 초록 채움)"],
            "steps": [
                {"k": "조건", "law": "층수", "src": cite2,
                 "need": "10층 이하의 층", "got": floor_name,
                 "res": "✓ 해당", "cls": "ok"},
                {"k": "기준", "law": "구획 단위 면적 (원칙)", "src": cite2,
                 "need": f"≤ {comp_p:.0f}㎡", "got": f"{total_area:.0f}㎡",
                 "res": "✓ 층 전체 한 구획 가능" if total_area <= comp_p else "✗ 초과",
                 "cls": "ok" if total_area <= comp_p else "bad"},
                {"pre": "↓ 참고 — 스프링클러 설치 시 (가정)",
                 "k": "완화", "law": "자동식 소화설비 설치 시 완화",
                 "src": cite2,
                 "need": f"≤ {comp_r:.0f}㎡", "got": f"{total_area:.0f}㎡",
                 "res": "✓ 여유" if total_area <= comp_r else "✗",
                 "cls": "ok" if total_area <= comp_r else "bad"},
            ],
            "asm": ["스프링클러 등 자동식 소화설비 설치 — 가정",
                    "면적은 인식된 실 합계(하한) — 실제 바닥면적은 이보다 큼"],
            "link": f"{base}_head_layout.html|스프링클러 헤드 배치 리포트",
        })

    # ③ 지하층 비상탈출구
    if esc_thr and esc_nst and esc_duty and esc_ex:
        duty_on = is_basement and living_area >= esc_thr
        exempt_on = n_stairs >= esc_nst
        v3 = ("불요(면제)" if duty_on and exempt_on else
              "적합" if not duty_on else
              "부적합" if not exempt_on else "자료 없음")
        cite3 = cite_of("비상탈출구") or "건축물의 피난ㆍ방화구조 등의 기준에 관한 규칙 제25조"
        checks.append({
            "id": "esc", "no": "③", "title": "지하층 비상탈출구", "v": v3,
            "one": (f"의무 발동(거실 {living_area:.0f}㎡ ≥ {esc_thr:.0f}㎡) → "
                    f"직통계단 {n_stairs}개소로 면제"),
            "law": [{"src": cite3, "rid": f"{esc_duty['id']}·{esc_ex['id']}",
                     "txt": (esc_duty["원문"] + " " + esc_ex["원문"]).strip()}],
            "meas": [f"거실 면적 합계 {living_area:.0f}㎡ — 샤프트·계단·PIT 제외 "
                     "(도면의 주황 채움)",
                     f"직통계단 {n_stairs}개소 자동 인식 (파란 점선 테두리)"],
            "steps": [
                {"k": "의무", "law": "지하층 거실 면적이 문턱 이상이면 "
                                    "비상탈출구 설치", "src": cite3,
                 "need": f"≥ {esc_thr:.0f}㎡", "got": f"{living_area:.0f}㎡",
                 "res": "⚡ 의무 발동", "cls": "warn"},
                {"pre": "↓ 다만 — 면제 조항 (면제 연결 그래프)",
                 "k": "면제", "law": "기준에 적합한 직통계단 수",
                 "src": f"{cite3} 단서",
                 "need": f"≥ {esc_nst:.0f}개소", "got": f"{n_stairs}개소",
                 "res": "✓ 면제 성립 → 의무 꺼짐" if exempt_on else "✗ 부족",
                 "cls": "exm" if exempt_on else "bad"},
            ],
            "asm": ["직통계단이 규칙 제8조② 구조 기준에 적합 — 가정"
                    " (계단 구조 상세는 단면·상세도 소관)"],
            "link": "",
        })

    # ④ 직통계단 2개소
    if st_rule and st_thr and st_need:
        duty4 = is_basement and living_area >= st_thr
        v4 = ("적합" if (not duty4 or n_stairs >= st_need) else "부적합")
        cite4 = cite_of("직통계단수") or "건축법 시행령 제34조 제2항"
        checks.append({
            "id": "stairs", "no": "④", "title": "직통계단 2개소", "v": v4,
            "one": (f"지하층 거실 {living_area:.0f}㎡ ≥ {st_thr:.0f}㎡ → "
                    f"{st_need:.0f}개소 의무 → {n_stairs}개소 확인"),
            "law": [{"src": cite4, "rid": st_rule["id"],
                     "txt": st_rule["원문"]}],
            "meas": [f"거실 면적 합계 {living_area:.0f}㎡ (도면의 주황 채움)",
                     f"직통계단 {n_stairs}개소 — "
                     f"{', '.join(sorted({r['room'] for r in stair_rooms}))} "
                     "(파란 점선 테두리)"],
            "steps": [
                {"k": "조건", "law": "층 구분", "src": cite4,
                 "need": "지하층", "got": floor_name, "res": "✓ 해당", "cls": "ok"},
                {"k": "의무", "law": "거실 바닥면적 합계", "src": cite4,
                 "need": f"≥ {st_thr:.0f}㎡ → {st_need:.0f}개소 의무",
                 "got": f"{living_area:.0f}㎡",
                 "res": "⚡ 의무 발동" if duty4 else "— 미발동",
                 "cls": "warn" if duty4 else "ok"},
                {"k": "기준", "law": "직통계단 개수", "src": cite4,
                 "need": f"≥ {st_need:.0f}개소", "got": f"{n_stairs}개소",
                 "res": "✓ 충족" if n_stairs >= st_need else "✗ 부족",
                 "cls": "ok" if n_stairs >= st_need else "bad"},
            ],
            "asm": [],
            "link": "",
        })

    # ── SVG ──────────────────────────────────────────────────────────────
    no_plot = set()
    for srcp in (os.path.join(FO, "data", f"{base}.json"),
                 os.path.join(FO, "data", "1층.json")):
        try:
            for L in jload(srcp).get("Layers", []):
                if L.get("Plot") is False:
                    no_plot.add(L["Name"])
        except Exception:
            pass

    def segs_of(want):
        out = []
        lys = {ly for ly, c in cats.items() if c in want and ly not in no_plot}
        for e in ents:
            if e.get("Layer") not in lys:
                continue
            t = e.get("Type")
            if t == "Line":
                a, b = e["Start"], e["End"]
                out.append((a[0], a[1], b[0], b[1]))
            elif t == "Polyline":
                v = e["Verts"] + ([e["Verts"][0]] if e.get("Closed") else [])
                for k in range(len(v) - 1):
                    out.append((v[k][0], v[k][1], v[k + 1][0], v[k + 1][1]))
        return out

    walls = segs_of({"wall_struct", "wall_nonstruct", "column_struct"})
    minx, miny, maxx, maxy = ev["bounds"]

    def fy(y):
        return round(maxy - y)

    from plan_label import label_spot as cen   # 내접 최대점 라벨 (공용 모듈)

    G = {k: [] for k in ("walls", "rooms", "comp", "living", "labels", "esc",
                         "exit", "stairs")}
    G["walls"].append('<path d="' + "".join(
        f"M{round(a)} {fy(b)}L{round(c)} {fy(d)}" for a, b, c, d in walls) + '"/>')
    for r in rooms_data:
        shape = (f'<polygon points="'
                 + " ".join(f"{round(p[0])},{fy(p[1])}" for p in r["poly"]) + '"/>'
                 if r.get("poly") else
                 f'<rect x="{r["rect"][0]}" y="{fy(r["rect"][3])}" '
                 f'width="{r["rect"][2]-r["rect"][0]}" '
                 f'height="{r["rect"][3]-r["rect"][1]}"/>')
        G["rooms"].append(shape)
        G["comp"].append(shape)          # 방화구획 하이라이트용 복제(채움)
        if is_living(r["room"]):
            G["living"].append(shape)    # ③④의 거실 면적 대상 하이라이트
        cx, cy = cen(r)
        st = ' class="stair"' if "계단" in r["room"] else ""
        G["labels"].append(f'<text{st} x="{round(cx)}" y="{fy(cy)}">'
                           f'{html.escape(r["room"])}</text>')
        if "계단" in r["room"]:
            x0, y0, x1, y1 = r["rect"]
            _sn = sum(1 for g in G["stairs"] if g.startswith("<rect")) + 1
            G["stairs"].append(f'<rect x="{x0-200}" y="{fy(y1)-200}" '
                               f'width="{x1-x0+400}" height="{y1-y0+400}"/>')
            G["stairs"].append(f'<text class="sno" x="{round(cx)}" '
                               f'y="{fy(cy)-650}">계단 {_sn}</text>')
    worst_nm = max(ev["동선"], key=lambda p: p["m"])["실"] if ev["동선"] else None
    for p in ev["동선"]:
        d = " ".join(f"{x},{fy(y)}" for x, y in p["pts"])
        w = ' class="worst"' if p["실"] == worst_nm else ""
        G["esc"].append(f'<polyline{w} points="{d}" data-nm="{html.escape(p["실"])}" '
                        f'data-m="{p["m"]}"/>')
        if p["실"] == worst_nm and p["pts"]:
            wx, wy = p["pts"][0]         # 동선의 시작점 = 최원점
            G["esc"].append(f'<circle class="wpt" cx="{wx}" cy="{fy(wy)}" r="420"/>')
            G["esc"].append(f'<text class="wlb" x="{wx}" y="{fy(wy)-700}">'
                            f'{html.escape(p["실"])} · 최악 {p["m"]}m</text>')
    for x, y in ev["출입구"]:
        G["exit"].append(f'<rect x="{x-260}" y="{fy(y)-260}" width="520" '
                         f'height="520"/>')
    cx0, cy0 = (minx + maxx) / 2, fy((miny + maxy) / 2)
    G["comp"].append(f'<text id="comp-lb" x="{round(cx0)}" y="{cy0}">'
                     f'1구획 · {total_area:.0f}㎡</text>')
    groups_svg = "\n".join(f'<g id="g-{k}">{"".join(v)}</g>' for k, v in G.items())
    pad = 3000
    vb = f"{round(minx)-pad} {-pad} {round(maxx-minx)+2*pad} {round(maxy-miny)+2*pad}"

    # ── 카드·상세 HTML ───────────────────────────────────────────────────
    from collections import Counter
    vc = Counter(c["v"] for c in checks)
    chips = "".join(f'<span class="chip {"ok" if B[v] in ("ok","exm") else "warn"}">'
                    f'{html.escape(v)} <b>{n}</b></span>' for v, n in vc.items())

    cards, details = [], []
    for i, c in enumerate(checks):
        cards.append(
            f'<div class="ck" data-i="{i}" data-id="{c["id"]}">'
            f'<div class="ck-h"><span class="ck-no">{c["no"]}</span>'
            f'<span class="ck-t">{html.escape(c["title"])}</span>{badge(c["v"])}</div>'
            f'<div class="ck-one">{html.escape(c["one"])}</div></div>')
        # 관련 조문 — 법령명을 앞세우고, rule_id 는 화면엔 없이 툴팁으로만.
        law_h = "".join(
            f'<div class="law" title="규칙 #{l["rid"]} — DB 추적용">'
            f'<span class="law-src">{html.escape(l["src"])}</span>'
            f'{html.escape(l["txt"])}</div>' for l in c["law"])
        meas_h = "".join(f'<div class="law meas">{html.escape(t)}</div>'
                         for t in c["meas"])
        # 판정 과정 — 기준→(단서)→결과의 단계 흐름. 표+사슬을 하나로 합쳤다.
        st_h = ""
        for s in c["steps"]:
            if s.get("pre"):
                st_h += f'<div class="st-a">{html.escape(s["pre"])}</div>'
            st_h += (
                f'<div class="st"><span class="st-k k-{s["k"]}">{s["k"]}</span>'
                f'<div class="st-b"><div class="st-law">{html.escape(s["law"])}'
                f'<small>{html.escape(s["src"])}</small></div>'
                f'<div class="st-cmp"><b class="need">{html.escape(s["need"])}</b>'
                f'<span class="vs">실측</span><b class="got">{html.escape(s["got"])}</b>'
                f'<span class="res {s["cls"]}">{html.escape(s["res"])}</span>'
                f'</div></div></div>')
        st_h += (f'<div class="st-fin">판정 {badge(c["v"])} '
                 f'<span class="meta">{html.escape(c["one"])}</span></div>')
        asm_h = "".join(f'<div class="law asm">{html.escape(t)}</div>'
                        for t in c["asm"]) or '<div class="meta">없음</div>'
        link_h = ""
        if c["link"]:
            href, lab = c["link"].split("|")
            # 공개 데모(Pages)의 파일명은 로컬과 다르다 — data-demo 에 실어 두면
            # JS 가 호스트를 보고 바꿔 단다. 데모 별칭이 없으면 링크를 숨긴다.
            DEMO_ALIAS = {f"{base}_evac_layout.html": "evac_layout_pit.html",
                          f"{base}_head_layout.html": "fire_head_layout_pit.html"}
            demo = DEMO_ALIAS.get(href, "")
            link_h = (f'<div style="margin-top:8px"><a class="xlink" '
                      f'href="{href}" data-demo="{demo}" '
                      f'target="_blank">↗ {html.escape(lab)}</a></div>')
        details.append(
            f'<div class="dt" id="dt-{i}" hidden>'
            f'<h4>{c["no"]} {html.escape(c["title"])} {badge(c["v"])}</h4>'
            f'<div class="sec">1. 관련 조문</div>{law_h}'
            f'<div class="sec">2. 도면 측정값</div>{meas_h}'
            f'<div class="sec">3. 판정 과정</div>{st_h}'
            f'<div class="sec">4. 전제 (가정)</div>{asm_h}{link_h}</div>')

    # 건물 사실 모달 (fire_layout 과 같은 무늬)
    _frow = [(k, str(v)) for k, v in profile.items()
             if isinstance(v, (str, int)) and not isinstance(v, bool)]
    _frow += [(f"층 · {k.replace('_',' ')}", "예" if v else "아니오")
              for k, v in (profile.get("층") or {}).items() if isinstance(v, bool)]
    facts_html = (
        '<div id="bf-pop" hidden><div id="bf-box"><h4>🏢 건물 사실 — 설계 가정</h4>'
        '<div class="meta" style="margin-bottom:8px">아래 값은 확인된 사실이 아니라 '
        '<b>가정</b>이며, 판정의 전제로 쓰였습니다.</div><table class="tbl">'
        + "".join(f'<tr><td style="color:var(--mut)">{html.escape(str(k))}</td>'
                  f'<td><b>{html.escape(str(v))}</b></td></tr>' for k, v in _frow)
        + '</table><button id="bf-x">닫기</button></div></div>')

    ck_json = json.dumps([{"id": c["id"], "v": c["v"]} for c in checks],
                         ensure_ascii=False)

    page = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"/>
<title>평면도 법률 검토 · {html.escape(floor_name or base)}</title><style>
:root{{--bg:#eef1f5;--panel:#fff;--ink:#1e2937;--mut:#64748b;--line:#e2e8f0;
--acc:#2563eb;--ok:#16803c;--warn:#c2410c;--exm:#1d4ed8}}
*{{box-sizing:border-box}}
html,body{{margin:0;height:100%;font-family:Pretendard,"Malgun Gothic",system-ui,sans-serif;
color:var(--ink)}}
#hd{{position:fixed;top:0;left:0;right:0;height:52px;z-index:20;display:flex;
align-items:center;gap:10px;padding:0 18px;background:#0f1d33;color:#fff;
box-shadow:0 2px 10px rgba(0,0,0,.25)}}
#hd .brand{{font-weight:800;font-size:15px;white-space:nowrap}}
#hd .brand small{{color:#8fb3e8;font-weight:600;margin-left:8px;font-size:12px}}
#hd .sp{{flex:1}}
.chip{{display:inline-block;background:rgba(255,255,255,.10);
border:1px solid rgba(255,255,255,.16);border-radius:999px;padding:3px 11px;
font-size:12px;margin-left:6px;color:#dbe6f5;white-space:nowrap;cursor:default}}
.chip b{{color:#fff}}
.chip.ok{{background:rgba(34,197,94,.16);border-color:rgba(34,197,94,.4);color:#bbf7d0}}
.chip.warn{{background:rgba(249,115,22,.18);border-color:rgba(249,115,22,.5);color:#fed7aa}}
#bf-btn{{cursor:pointer}}
aside{{position:fixed;top:62px;bottom:12px;z-index:10;width:335px;overflow-y:auto;
display:flex;flex-direction:column;gap:10px;scrollbar-width:thin}}
#left{{left:12px}} #right{{right:12px;width:390px}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;
padding:12px 14px;box-shadow:0 4px 18px rgba(15,29,51,.08);font-size:12.5px;
line-height:1.55;flex-shrink:0}}
.card h3{{margin:0 0 8px;font-size:11px;color:var(--mut);letter-spacing:.14em;
font-weight:800}}
#rp-go{{width:100%;border:none;background:var(--acc);color:#fff;border-radius:9px;
padding:9px 0;font-size:13px;font-weight:700;cursor:pointer}}
#rp-go:disabled{{background:#94a3b8;cursor:wait}}
.ck{{border:1px solid var(--line);border-radius:10px;padding:9px 11px;margin:7px 0;
cursor:pointer;transition:box-shadow .12s}}
.ck:hover{{box-shadow:0 3px 10px rgba(15,29,51,.10)}}
.ck.on{{border-color:var(--acc);box-shadow:inset 3px 0 0 var(--acc)}}
.ck-h{{display:flex;align-items:center;gap:7px}}
.ck-no{{font-weight:800;color:var(--acc)}}
.ck-t{{font-weight:700;flex:1}}
.ck-one{{color:var(--mut);font-size:11.5px;margin-top:3px}}
.vd{{display:inline-block;min-width:44px;text-align:center;border-radius:6px;
padding:1px 7px;font-size:10.5px;font-weight:800}}
.vd.ok{{background:#dcfce7;color:#15803d}}
.vd.exm{{background:#dbeafe;color:#1d4ed8}}
.vd.bad{{background:#fee2e2;color:#b91c1c}}
.vd.na{{background:#f1f5f9;color:#64748b}}
.src{{font-size:10px;font-weight:800;padding:1px 7px;border-radius:99px;
margin-right:6px;white-space:nowrap}}
.src.law{{background:#dcfce7;color:#15803d}}
.src.meas{{background:#dbeafe;color:#1d4ed8}}
.src.asm{{background:#ffedd5;color:#c2410c}}
.dt h4{{margin:0 0 6px;font-size:14.5px}}
.sec{{font-size:13px;font-weight:800;color:var(--ink);margin:14px 0 6px;
padding-bottom:3px;border-bottom:2px solid var(--line)}}
.law{{color:#3d4a5c;font-size:12.5px;background:#f3f6fa;border-left:3px solid #16803c;
padding:7px 10px;border-radius:4px;margin-bottom:5px;line-height:1.65}}
.law-src{{display:block;font-weight:800;color:var(--ink);font-size:12.5px;
margin-bottom:2px}}
.law.meas{{border-left-color:#1d4ed8}}
.law.asm{{border-left-color:#c2410c;background:#fff7ed}}
.tbl{{width:100%;border-collapse:collapse;font-size:12px}}
.tbl td{{padding:4px;border-bottom:1px solid #f1f5f9}}
.meta{{color:var(--mut);font-size:11.5px}}
.st{{display:flex;gap:8px;margin:7px 0;align-items:flex-start}}
.st-k{{flex-shrink:0;font-size:10.5px;font-weight:800;border-radius:99px;
padding:2px 9px;margin-top:2px}}
.k-기준,.k-조건{{background:#e2e8f0;color:#334155}}
.k-완화,.k-면제{{background:#dbeafe;color:#1d4ed8}}
.k-의무{{background:#fef9c3;color:#a16207}}
.st-b{{flex:1;min-width:0}}
.st-law{{font-size:12.5px;line-height:1.5}}
.st-law small{{display:block;color:var(--mut);font-size:10.5px;margin-top:1px}}
.st-cmp{{display:flex;align-items:baseline;gap:8px;margin-top:4px;
background:#f8fafc;border:1px solid var(--line);border-radius:9px;padding:6px 10px}}
.st-cmp .need{{font-size:14px}}
.st-cmp .vs{{color:var(--mut);font-size:10.5px}}
.st-cmp .got{{font-size:15.5px}}
.res{{margin-left:auto;font-size:11px;font-weight:800;border-radius:6px;
padding:1px 8px;white-space:nowrap}}
.res.ok{{background:#dcfce7;color:#15803d}}
.res.bad{{background:#fee2e2;color:#b91c1c}}
.res.exm{{background:#dbeafe;color:#1d4ed8}}
.res.warn{{background:#fef9c3;color:#a16207}}
.st-a{{color:var(--acc);font-weight:800;font-size:11.5px;margin:4px 0 4px 8px}}
.st-fin{{margin-top:10px;padding:8px 11px;border:1px dashed #94a3b8;
border-radius:10px;font-weight:700;font-size:13px;display:flex;
align-items:center;gap:8px;flex-wrap:wrap}}
#stage{{position:fixed;inset:52px 0 0 0;background:#fff;cursor:grab}}
svg{{width:100%;height:100%;display:block}}
#g-walls path{{stroke:#c9cfda;fill:none;stroke-width:1;vector-effect:non-scaling-stroke}}
#g-rooms polygon,#g-rooms rect{{fill:none;stroke:#94a3b8;stroke-width:1;
vector-effect:non-scaling-stroke}}
#g-comp{{display:none}}
#g-comp polygon,#g-comp rect{{fill:#22c55e;fill-opacity:.13;stroke:#16803c;
stroke-width:2;vector-effect:non-scaling-stroke}}
#g-comp text{{font-size:1400px;font-weight:800;fill:#15803d;text-anchor:middle;
paint-order:stroke;stroke:#fff;stroke-width:260px}}
#g-labels text{{font-size:520px;font-weight:600;fill:#475569;text-anchor:middle;
dominant-baseline:middle;paint-order:stroke;stroke:#fff;stroke-width:110px}}
#g-labels text.stair{{fill:#fff;font-size:600px;font-weight:800;
stroke:#1e5cb3;stroke-width:180px}}
#g-esc{{display:none}}
#g-esc polyline{{fill:none;stroke:#2e7d32;stroke-width:2.4;stroke-dasharray:10 6;
vector-effect:non-scaling-stroke}}
#g-esc polyline.worst{{stroke:#d84315;stroke-width:4.4}}
#g-esc .wpt{{fill:none;stroke:#d84315;stroke-width:3.4;
vector-effect:non-scaling-stroke}}
#g-esc .wlb{{font-size:750px;font-weight:800;fill:#d84315;text-anchor:middle;
paint-order:stroke;stroke:#fff;stroke-width:160px}}
#g-living{{display:none}}
#g-living polygon,#g-living rect{{fill:#f59e0b;fill-opacity:.24;stroke:#d97706;
stroke-width:2.6;vector-effect:non-scaling-stroke}}
#g-exit rect{{fill:#1e5cb3}}
#g-stairs{{display:none}}
#g-stairs rect{{fill:#1e5cb3;fill-opacity:.28;stroke:#1e5cb3;stroke-width:4;
vector-effect:non-scaling-stroke;stroke-dasharray:14 8}}
#g-stairs .sno{{font-size:750px;font-weight:800;fill:#1e5cb3;text-anchor:middle;
paint-order:stroke;stroke:#fff;stroke-width:160px}}
/* 포커스 모드 — 항목 선택 시 관련 요소만 도드라지고 바탕은 흐려진다 */
svg.dim #g-walls,svg.dim #g-rooms{{opacity:.3}}
svg.dim #g-labels{{opacity:.22}}
svg.dim #g-labels text.stair{{opacity:1}}
#g-exit.pulse rect,#g-stairs.pulse rect{{animation:pp .6s ease-in-out 4}}
@keyframes pp{{50%{{fill:#22c55e;stroke:#22c55e}}}}
#bf-pop{{position:fixed;inset:0;background:rgba(15,23,42,.45);z-index:99;
display:flex;align-items:flex-start;justify-content:center;padding-top:70px}}
#bf-pop[hidden]{{display:none}}
#bf-box{{background:var(--panel);border-radius:14px;padding:16px 18px;width:370px;
max-height:72vh;overflow-y:auto;box-shadow:0 20px 50px rgba(15,29,51,.35);
font-size:12.5px;color:var(--ink)}}
#bf-box h4{{margin:0 0 8px;font-size:13px}}
#bf-x{{margin-top:10px;width:100%;border:1px solid var(--line);background:#f8fafc;
border-radius:8px;padding:6px 0;cursor:pointer;font-size:12px}}
a{{color:var(--acc)}}
</style></head><body>
<header id="hd">
 <span class="brand">🏛️ FRAN 평면도 법률 검토<small>{html.escape(floor_name or base)}
 · 검토 {len(checks)}건</small></span>
 <span class="sp"></span>
 {chips}
 <button id="bf-btn" class="chip" type="button">🏢 건물 사실</button>
</header>
{facts_html}
<div id="stage"><svg id="svg" viewBox="{vb}" xmlns="http://www.w3.org/2000/svg">
{groups_svg}
</svg></div>
<aside id="left">
 <section class="card"><h3>전체 검토 재생</h3>
  <button id="rp-go">▶ 검토 시작 — 4항목 차례로</button>
  <div id="rp-st" class="meta" style="margin-top:7px;min-height:17px">항목을
  클릭하면 관련 도면 요소가 표시되고, 오른쪽에 판정 근거가 열립니다.</div>
 </section>
 <section class="card"><h3>검토 항목</h3>{''.join(cards)}</section>
</aside>
<aside id="right">
 <section class="card"><h3>판정 근거 — 조문 → 측정 → 대조 → 사슬 → 전제</h3>
  <div id="dt-none" class="meta">왼쪽에서 검토 항목을 선택하세요.</div>
  {''.join(details)}
 </section>
</aside>
<script>
(function(){{var s=document.getElementById('svg'),st=document.getElementById('stage');
var p=s.getAttribute('viewBox').split(' ').map(Number),vb={{x:p[0],y:p[1],w:p[2],h:p[3]}};
function ap(){{s.setAttribute('viewBox',vb.x+' '+vb.y+' '+vb.w+' '+vb.h)}}
st.addEventListener('wheel',function(e){{e.preventDefault();var r=s.getBoundingClientRect();
var px=vb.x+(e.clientX-r.left)/r.width*vb.w,py=vb.y+(e.clientY-r.top)/r.height*vb.h;
var f=e.deltaY>0?1.15:1/1.15;vb.w*=f;vb.h*=f;
vb.x=px-(e.clientX-r.left)/r.width*vb.w;vb.y=py-(e.clientY-r.top)/r.height*vb.h;ap()}},{{passive:false}});
var d=false,lx,ly;st.addEventListener('mousedown',function(e){{d=true;lx=e.clientX;ly=e.clientY}});
window.addEventListener('mousemove',function(e){{if(!d)return;var r=s.getBoundingClientRect();
vb.x-=(e.clientX-lx)/r.width*vb.w;vb.y-=(e.clientY-ly)/r.height*vb.h;lx=e.clientX;ly=e.clientY;ap()}});
window.addEventListener('mouseup',function(){{d=false}});
// 상세 링크 — 로컬 서버가 아니면 공개 데모 파일명(data-demo)으로 바꿔 단다
if(!/^(localhost|127[.])/.test(location.hostname)){{
 document.querySelectorAll('a.xlink').forEach(function(a){{
  var d=a.getAttribute('data-demo');
  if(d)a.setAttribute('href',d);
  else a.parentNode.style.display='none';
 }});
}}
var bfB=document.getElementById('bf-btn'),bfP=document.getElementById('bf-pop');
if(bfB&&bfP){{bfB.onclick=function(){{bfP.hidden=false}};
document.getElementById('bf-x').onclick=function(){{bfP.hidden=true}};
bfP.onclick=function(e){{if(e.target===bfP)bfP.hidden=true}};}}
}})();
{{ANIM}}
</script></body></html>"""

    anim_js = r"""
(function(){
 var CK=__CK__;
 var cards=Array.prototype.slice.call(document.querySelectorAll('.ck'));
 var go=document.getElementById('rp-go'),st=document.getElementById('rp-st');
 var G=function(id){return document.getElementById(id);};
 // 항목별로 켤 도면 레이어
 var SHOW={evac:['g-esc'],comp:['g-comp'],
           esc:['g-stairs','g-living'],stairs:['g-stairs','g-living']};
 var ALL=['g-esc','g-comp','g-stairs','g-living'];
 function select(i,quiet){
  svg.classList.toggle('dim', i>=0);   // 포커스: 바탕 흐림, 관련 레이어만 선명
  cards.forEach(function(c,k){c.classList.toggle('on',k===i);});
  document.querySelectorAll('.dt').forEach(function(d){d.hidden=true;});
  var none=G('dt-none'); if(none)none.hidden=(i>=0);
  if(i>=0){var d=G('dt-'+i); if(d)d.hidden=false;}
  ALL.forEach(function(g){var el=G(g);if(el)el.style.display='none';});
  if(i>=0){(SHOW[CK[i].id]||[]).forEach(function(g){
   // CSS 기본이 display:none 이라 '' 로 지우면 도로 숨는다 — 'inline' 로 켠다
   var el=G(g);if(el)el.style.display='inline';});}
  if(i>=0&&!quiet&&(CK[i].id==='esc'||CK[i].id==='stairs')){
   var sg=G('g-stairs');if(sg){sg.classList.add('pulse');
    setTimeout(function(){sg.classList.remove('pulse');},2600);}
  }
 }
 cards.forEach(function(c,i){c.onclick=function(){select(i);};});

 // ── 재생: ①동선 점선 드로잉 → ②구획 채움 → ③④계단 펄스, 항목마다 판정 표시
 var svg=document.getElementById('svg');
 var lines=Array.prototype.slice.call(document.querySelectorAll('#g-esc polyline'));
 lines.sort(function(a,b){return parseFloat(a.dataset.m)-parseFloat(b.dataset.m);});
 var defs=svg.querySelector('defs');
 if(!defs){defs=document.createElementNS('http://www.w3.org/2000/svg','defs');
  svg.insertBefore(defs,svg.firstChild);}
 var idc=0;
 function maskFor(l){
  var len=l.getTotalLength(),bb=l.getBBox(),pad=2500;
  var mk=document.createElementNS('http://www.w3.org/2000/svg','mask');
  mk.setAttribute('id','pm'+(++idc));
  mk.setAttribute('maskUnits','userSpaceOnUse');
  mk.setAttribute('x',bb.x-pad);mk.setAttribute('y',bb.y-pad);
  mk.setAttribute('width',bb.width+2*pad);mk.setAttribute('height',bb.height+2*pad);
  var c=document.createElementNS('http://www.w3.org/2000/svg','polyline');
  c.setAttribute('points',l.getAttribute('points'));
  c.setAttribute('fill','none');c.setAttribute('stroke','#fff');
  c.setAttribute('stroke-width','2600');c.setAttribute('stroke-linecap','round');
  c.style.strokeDasharray=len+' '+len;c.style.strokeDashoffset=len;
  mk.appendChild(c);defs.appendChild(mk);
  l.setAttribute('mask','url(#'+mk.id+')');
  return {mk:mk,c:c,len:len,l:l};
 }
 function drawOne(m,speed,done){
  var dur=Math.max(220,m.len/speed*1000),t0=null;
  function step(ts){
   if(!t0)t0=ts;
   var k=Math.min(1,(ts-t0)/dur);
   m.c.style.strokeDashoffset=m.len*(1-k);
   st.textContent='① '+m.l.dataset.nm+' — '
     +(m.len*k/1000).toFixed(1)+' / '+m.l.dataset.m+' m';
   if(k<1)requestAnimationFrame(step);
   else{m.l.removeAttribute('mask');defs.removeChild(m.mk);done();}
  }
  requestAnimationFrame(step);
 }
 var running=false;
 document.getElementById('rp-go').onclick=function(){
  if(running)return; running=true;
  go.disabled=true; go.textContent='재생 중…';
  // ① 피난거리
  select(0,true);
  var ms=lines.map(maskFor), i=0;
  (function next(){
   if(i<ms.length){drawOne(ms[i++],34000,function(){setTimeout(next,60);});return;}
   st.textContent='① 피난 보행거리 — '+CK[0].v;
   // ② 방화구획
   setTimeout(function(){
    select(1,true);
    var gc=G('g-comp'); gc.style.opacity=0; gc.style.display='inline';
    var t0=null;
    function fade(ts){
     if(!t0)t0=ts; var k=Math.min(1,(ts-t0)/900);
     gc.style.opacity=k;
     st.textContent='② 방화구획 — 층 면적 합산·구획 확인';
     if(k<1)requestAnimationFrame(fade);
     else{
      st.textContent='② 방화구획 면적 — '+CK[1].v;
      // ③ 비상탈출구 → ④ 직통계단
      setTimeout(function(){
       select(2);
       st.textContent='③ 지하층 비상탈출구 — 계단 '+'2개소 → 면제 확인';
       setTimeout(function(){
        select(3);
        st.textContent='④ 직통계단 2개소 — '+CK[3].v;
        setTimeout(function(){
         select(-1);
         st.textContent='완료 — 검토 '+CK.length+'건: '
           +CK.map(function(c){return c.v;}).join(' · ');
         go.disabled=false; go.textContent='↺ 다시 보기'; running=false;
        },2200);
       },2600);
      },1400);
     }
    }
    requestAnimationFrame(fade);
   },900);
  })();
 };
})();
"""
    page = page.replace("{ANIM}", anim_js.replace("__CK__", ck_json))

    op = os.path.join(FO, "output", f"{base}_law_review.html")
    open(op, "w", encoding="utf-8").write(page)
    print(f"검토 {len(checks)}건: " + " · ".join(f"{c['no']}{c['v']}" for c in checks))
    print(f"층 면적 {total_area:.0f}㎡ · 거실 {living_area:.0f}㎡ · 계단 {n_stairs}개소")
    print(f"출력: {op}")


if __name__ == "__main__":
    main()
