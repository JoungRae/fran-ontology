"""
건축 법령 검토 리포트 생성기.

  BOT 그래프(building.ttl) + cons_law 법령 룰(checks) → 실무자용 검토 리포트(HTML)

파이프라인:
  1. cons_law 의 평가 엔진(evaluator.py)을 그대로 재사용 (importlib 로 직접 로드, DB 불필요)
  2. cons_law 의 룰 카탈로그(checks_demo.json) 로드 — 실제 법령 근거 + 일부 데모 규칙
  3. BOT 그래프에서 project(용도·층수·세대수·높이)·drawing(방 면적 등) 값 도출 (출처 명시)
  4. evaluate_project() 로 pass/fail/needs_review/not_applicable 판정
  5. 상태색·필터·검색·근거조문·필요데이터·인쇄를 지원하는 자체완결형 HTML 렌더

사용법:
  python compliance_report.py [--ttl output/building.ttl] [--rules rules/checks_demo.json]
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

from rdflib import Graph, Namespace

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BOT = Namespace("https://w3id.org/bot#")
FRAN = Namespace("https://example.org/fran#")

CONS_LAW_EVALUATOR = r"D:/Python_test/cons_law/src/cons_law/evaluator.py"
SECTION_JSON = (r"D:/Python_test/fran_consist_cad_json/output/"
                r"A1-131~144 단위세대 주단면도(A5BL)_20260613_111427_section.json")


# --- cons_law 평가 엔진 로드 (패키지 __init__ 우회, DB 의존성 없음) ----------
def load_evaluator(path):
    spec = importlib.util.spec_from_file_location("cons_evaluator", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cons_evaluator"] = mod          # dataclass 해석 위해 등록
    spec.loader.exec_module(mod)
    return mod


# --- 단면도에서 지상/지하 층수·건물높이 도출 (실측 근거) ---------------------
def building_metrics_from_section(path):
    """단면도 층 밴드에서 지상층수·지하층수·건물높이(m) 추정. 실패 시 None."""
    if not os.path.exists(path):
        return None
    try:
        floors = json.load(open(path, encoding="utf-8")).get("floors", [])
    except Exception:
        return None
    above = below = 0
    height_mm = 0
    for fl in floors:
        name = str(fl.get("name", ""))
        h = fl.get("floor_height_mm") or 0
        m = re.search(r"(\d+)\s*~\s*(\d+)\s*층", name)
        if m:
            n = int(m.group(2)) - int(m.group(1)) + 1
        elif re.search(r"\d+\s*층", name) and "지하" not in name:
            n = 1
        else:
            n = 0
        if "지하" in name:
            below += 1
        elif "지붕" in name or "옥탑" in name:
            pass
        else:
            above += n
            height_mm += h * max(n, 1)
    return {"floors_above": above, "floors_below": below,
            "building_height_m": round(height_mm / 1000, 1)}


# --- BOT 그래프에서 측정값 도출 -------------------------------------------
def derive_from_graph(ttl):
    g = Graph()
    g.parse(ttl, format="turtle")
    ns = {"bot": BOT, "fran": FRAN,
          "rdfs": "http://www.w3.org/2000/01/rdf-schema#"}

    # 방 이름별 면적(㎡) 목록
    areas = collections.defaultdict(list)
    for lb, ar in g.query("""SELECT ?lb ?ar WHERE {
            ?s a bot:Space ; rdfs:label ?lb ; fran:areaM2 ?ar }""", initNs=ns):
        areas[str(lb)].append(float(ar))

    def min_area(keyword):
        vals = [a for name, lst in areas.items() if keyword in name
                for a in lst if a > 0]
        return round(min(vals), 2) if vals else None

    n_units = int(list(g.query("SELECT (COUNT(?u) AS ?n) WHERE { ?u a fran:DwellingUnit }",
                               initNs=ns))[0][0])
    dwell = 0
    for (dc,) in g.query("SELECT ?dc WHERE { ?u fran:dwellingCount ?dc }", initNs=ns):
        dwell += int(dc)
    # dwellingCount 없는 세대는 1주호로 계산
    units_no_dc = int(list(g.query("""SELECT (COUNT(?u) AS ?n) WHERE {
        ?u a fran:DwellingUnit FILTER NOT EXISTS { ?u fran:dwellingCount ?x } }""",
                                   initNs=ns))[0][0])
    households_model = dwell + units_no_dc

    balcony = min_area("발코니")
    return {
        "areas": areas,
        "bathroom_area_m2": min_area("욕실"),
        "living_dining_area_m2": min_area("거실"),
        "balcony_area_m2": balcony,
        "has_evacuation_space": bool(balcony),   # 대피공간=발코니 기준 추정
        "evacuation_area_m2": balcony,
        "units_model": n_units,
        "households_model": households_model,
    }


# --- project / drawing 조립 (+ 출처 provenance) -----------------------------
def build_inputs(ttl):
    m = derive_from_graph(ttl)
    sec = building_metrics_from_section(SECTION_JSON) or {}

    prov = {}   # 필드 → (값, 출처)
    project = {"use_type": "공동주택"}
    prov["용도(use_type)"] = ("공동주택", "주동평면도·세대타입(55A/55AS)")

    if sec:
        project["floors_above"] = sec["floors_above"]
        project["floors_below"] = sec["floors_below"]
        project["building_height_m"] = sec["building_height_m"]
        project["height_m"] = sec["building_height_m"]
        prov["지상 층수"] = (sec["floors_above"], "단면도 층 밴드")
        prov["지하 층수"] = (sec["floors_below"], "단면도 층 밴드")
        prov["건물 높이(m)"] = (sec["building_height_m"], "단면도 층고 합산")
    project["household_count"] = m["households_model"]
    prov["세대수(모델)"] = (m["households_model"], "BOT 그래프 DwellingUnit(모델링 층 기준)")
    # 대지·지역 정보는 도면만으로 알 수 없음 → 미입력 (관련 항목 해당없음/검토필요)
    prov["대지·지역(zone/sido)"] = ("미입력", "평면도에 없음 — 배치도·지적 필요")

    drawing = {}
    for k in ("bathroom_area_m2", "living_dining_area_m2",
              "evacuation_area_m2", "has_evacuation_space"):
        if m[k] is not None:
            drawing[k] = m[k]
    prov["욕실 면적(㎡)"] = (m["bathroom_area_m2"], "BOT 그래프(직사각형 근사)")
    prov["거실 면적(㎡)"] = (m["living_dining_area_m2"], "BOT 그래프(직사각형 근사)")
    prov["대피공간(발코니 추정, ㎡)"] = (m["evacuation_area_m2"],
                                  "BOT 그래프 발코니 면적(근사)")
    return project, drawing, prov, m


# --- 리포트 렌더 ----------------------------------------------------------
STATUS_KO = {"pass": "적합", "fail": "부적합",
             "needs_review": "검토필요", "not_applicable": "해당없음"}
STATUS_COLOR = {"pass": "#1b8a3f", "fail": "#c62828",
                "needs_review": "#e08600", "not_applicable": "#8a8f98"}
STATUS_ORDER = {"fail": 0, "needs_review": 1, "pass": 2, "not_applicable": 3}
DOMAIN_KO = {"site": "대지·배치", "massing": "형태·높이", "egress": "피난·방화",
             "mep": "설비", "parking": "주차", "landscape": "조경",
             "energy": "에너지", "fire": "소방", "structure": "구조",
             "accessibility": "장애인편의", "housing": "주택·세대",
             "documentation": "도서", "other": "기타"}


def fmt_val(v):
    if isinstance(v, bool):
        return "있음" if v else "없음"
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".")
    if isinstance(v, (list, dict)):
        return html.escape(json.dumps(v, ensure_ascii=False))
    return html.escape(str(v))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ttl", default=os.path.join("output", "building.ttl"))
    ap.add_argument("--rules", default=os.path.join("rules", "checks_demo.json"))
    ap.add_argument("--out", default=os.path.join("output", "compliance_report.html"))
    ap.add_argument("--project-name", default="주동 A (5BL) · 55A/55AS")
    args = ap.parse_args()

    ev = load_evaluator(CONS_LAW_EVALUATOR)
    checks = json.load(open(args.rules, encoding="utf-8"))["checks"]
    project, drawing, prov, m = build_inputs(args.ttl)

    results = ev.evaluate_project(checks, project, drawing)

    findings = []
    for c, r in results:
        src = c.get("source", {})
        law = src.get("law_title", "")
        art = src.get("article") or ""
        findings.append({
            "code": c.get("code", ""),
            "title": c.get("title", ""),
            "domain": c.get("domain", "other"),
            "domain_ko": DOMAIN_KO.get(c.get("domain", "other"), "기타"),
            "severity": c.get("severity", "mandatory"),
            "status": r.status,
            "status_ko": STATUS_KO.get(r.status, r.status),
            "actual": fmt_val(r.actual) if r.actual is not None else "",
            "required": fmt_val(r.required) if r.required is not None else "",
            "message": r.message or "",
            "law": law, "article": art,
            "law_ref": (law + (" " + art if art else "")).strip(),
            "excerpt": c.get("source_excerpt") or src.get("excerpt", ""),
            "fields": c.get("required_drawing_fields", []),
            "is_demo": src.get("kind") == "example",
        })
    findings.sort(key=lambda f: (STATUS_ORDER.get(f["status"], 9), f["code"]))

    counts = collections.Counter(f["status"] for f in findings)
    today = datetime.date.today().isoformat()

    prov_rows = "".join(
        f'<tr><td>{html.escape(k)}</td><td>{fmt_val(v)}</td>'
        f'<td class="src">{html.escape(s)}</td></tr>'
        for k, (v, s) in prov.items())

    page = _TEMPLATE.format(
        project=html.escape(args.project_name),
        today=today,
        n_total=len(findings),
        n_pass=counts.get("pass", 0), n_fail=counts.get("fail", 0),
        n_review=counts.get("needs_review", 0), n_na=counts.get("not_applicable", 0),
        prov_rows=prov_rows,
        findings_json=json.dumps(findings, ensure_ascii=False),
        ttl=html.escape(os.path.basename(args.ttl)),
        rules=html.escape(os.path.basename(args.rules)),
    )
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    open(args.out, "w", encoding="utf-8").write(page)

    print(f"룰: {args.rules} ({len(checks)}개)  ·  그래프: {args.ttl}")
    print(f"project: use={project['use_type']} 층수(지상)={project.get('floors_above')} "
          f"높이={project.get('building_height_m')}m 세대(모델)={project['household_count']}")
    print(f"drawing: {drawing}")
    print(f"판정: 적합 {counts.get('pass',0)} · 부적합 {counts.get('fail',0)} · "
          f"검토필요 {counts.get('needs_review',0)} · 해당없음 {counts.get('not_applicable',0)}")
    print(f"출력: {args.out}")


_TEMPLATE = r"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>건축 법령 검토 리포트 · {project}</title>
<style>
:root{{
  --pass:#1b8a3f; --fail:#c62828; --review:#e08600; --na:#8a8f98;
  --bg:#f4f5f7; --card:#fff; --line:#e3e6ea; --ink:#1c2430; --muted:#5b6472;
}}
*{{box-sizing:border-box}}
body{{margin:0;font-family:"Malgun Gothic",system-ui,sans-serif;background:var(--bg);
  color:var(--ink);font-size:14px;line-height:1.5}}
header{{background:linear-gradient(120deg,#1f2a44,#2f4064);color:#fff;padding:20px 26px}}
header h1{{margin:0 0 4px;font-size:20px;letter-spacing:-.3px}}
header .meta{{font-size:13px;opacity:.85}}
.wrap{{max-width:1120px;margin:0 auto;padding:18px 20px 60px}}
.grid{{display:grid;gap:14px}}
.cards{{grid-template-columns:repeat(4,1fr);margin:16px 0}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;
  position:relative;overflow:hidden;cursor:pointer;transition:.15s;user-select:none}}
.kpi:hover{{transform:translateY(-2px);box-shadow:0 6px 18px rgba(0,0,0,.08)}}
.kpi.active{{outline:2.5px solid currentColor;outline-offset:-1px}}
.kpi .n{{font-size:30px;font-weight:800;line-height:1}}
.kpi .l{{font-size:13px;color:var(--muted);margin-top:4px}}
.kpi .bar{{position:absolute;left:0;top:0;bottom:0;width:5px}}
.kpi.p{{color:var(--pass)}} .kpi.f{{color:var(--fail)}}
.kpi.r{{color:var(--review)}} .kpi.n{{color:var(--na)}}
.panel{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px}}
.panel h2{{margin:0 0 10px;font-size:15px}}
table.prov{{width:100%;border-collapse:collapse;font-size:13px}}
table.prov td{{padding:6px 8px;border-bottom:1px solid var(--line)}}
table.prov td:first-child{{color:var(--muted);width:210px}}
table.prov td.src{{color:#8a93a0;font-size:12px}}
.toolbar{{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:16px 0 10px}}
.toolbar input[type=search]{{flex:1;min-width:200px;padding:9px 12px;border:1px solid var(--line);
  border-radius:9px;font-size:14px}}
.chips{{display:flex;gap:6px;flex-wrap:wrap}}
.chip{{padding:6px 11px;border:1px solid var(--line);border-radius:999px;background:#fff;
  cursor:pointer;font-size:12.5px;user-select:none}}
.chip.on{{background:#1f2a44;color:#fff;border-color:#1f2a44}}
.finding{{background:var(--card);border:1px solid var(--line);border-left-width:5px;
  border-radius:10px;padding:13px 15px;margin-bottom:10px}}
.finding .row1{{display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
.badge{{font-size:12px;font-weight:700;color:#fff;padding:3px 10px;border-radius:999px;white-space:nowrap}}
.finding .title{{font-weight:700;font-size:15px}}
.finding .code{{font-family:ui-monospace,monospace;font-size:11.5px;color:var(--muted);
  background:#eef1f4;padding:2px 7px;border-radius:6px}}
.tag{{font-size:11.5px;color:var(--muted);border:1px solid var(--line);border-radius:6px;padding:2px 7px}}
.tag.sev-mandatory{{color:#b23b3b;border-color:#f0c9c9;background:#fdf1f1}}
.tag.sev-recommended{{color:#8a6d00;border-color:#eadfb0;background:#fbf7e6}}
.tag.demo{{color:#7a4bd0;border-color:#e0d3f5;background:#f6f0fe}}
.finding .verdict{{margin-top:8px;font-size:13.5px}}
.finding .verdict b{{font-weight:700}}
.finding .law{{margin-top:6px;font-size:12.5px;color:var(--muted)}}
.finding .need{{margin-top:6px;font-size:12.5px}}
.finding .need span{{background:#fff4e2;border:1px solid #f3d9a8;color:#8a5a00;
  padding:2px 8px;border-radius:6px;margin-right:5px;display:inline-block;margin-top:3px}}
.finding details{{margin-top:8px}}
.finding summary{{cursor:pointer;font-size:12.5px;color:#3a6ea5}}
.finding .excerpt{{margin-top:6px;padding:9px 11px;background:#f7f8fa;border-left:3px solid #c7ccd4;
  border-radius:5px;font-size:12.5px;color:#3a4250;white-space:pre-wrap}}
.empty{{text-align:center;color:var(--muted);padding:40px}}
.actions{{display:flex;gap:8px;margin-left:auto}}
.btn{{padding:8px 13px;border:1px solid var(--line);border-radius:9px;background:#fff;
  cursor:pointer;font-size:13px}}
footer{{color:#8a93a0;font-size:12px;margin-top:24px;line-height:1.6}}
@media print{{
  header{{background:#1f2a44 !important;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
  .toolbar,.actions,.kpi{{cursor:default}} .kpi:hover{{transform:none;box-shadow:none}}
  .finding{{break-inside:avoid}} details{{display:none}}
}}
</style></head><body>
<header>
  <h1>건축 법령 검토 리포트</h1>
  <div class="meta">{project} &nbsp;·&nbsp; 검토일 {today} &nbsp;·&nbsp;
    모델 {ttl} &nbsp;·&nbsp; 법령룰 {rules}</div>
</header>
<div class="wrap">

  <div class="grid cards">
    <div class="kpi p" data-f="pass"><span class="bar" style="background:var(--pass)"></span>
      <div class="n">{n_pass}</div><div class="l">적합 · Pass</div></div>
    <div class="kpi f" data-f="fail"><span class="bar" style="background:var(--fail)"></span>
      <div class="n">{n_fail}</div><div class="l">부적합 · Fail</div></div>
    <div class="kpi r" data-f="needs_review"><span class="bar" style="background:var(--review)"></span>
      <div class="n">{n_review}</div><div class="l">검토필요 · Review</div></div>
    <div class="kpi n" data-f="not_applicable"><span class="bar" style="background:var(--na)"></span>
      <div class="n">{n_na}</div><div class="l">해당없음 · N/A</div></div>
  </div>

  <div class="panel">
    <h2>프로젝트 개요 &amp; 입력값 출처</h2>
    <table class="prov"><tbody>{prov_rows}</tbody></table>
  </div>

  <div class="toolbar">
    <input type="search" id="q" placeholder="🔍 항목·법령·코드 검색 (예: 대피, 주차, 복도)"/>
    <div class="chips" id="domchips"></div>
    <div class="actions">
      <button class="btn" onclick="window.print()">🖨 인쇄/PDF</button>
    </div>
  </div>
  <div id="hint" style="font-size:12.5px;color:var(--muted);margin-bottom:8px"></div>

  <div id="list" class="grid"></div>

  <footer>
    본 리포트는 BOT(Building Topology Ontology) 그래프에서 도출한 값을 cons_law 법령 룰
    엔진으로 자동 판정한 결과입니다. 면적·치수는 <b>직사각형 근사 지오메트리</b> 기반으로,
    정량 규정의 최종 확인은 실측 도면 대조가 필요합니다.
    <span class="tag demo">데모</span> 표시 항목은 법령 매핑 없이 시연용으로 포함된 규칙입니다.
    대지·지역·주차 등 도면에 없는 정보는 <b>검토필요/해당없음</b>으로 분류됩니다.
    <br/>자동 검토는 참고용이며 최종 법적 판단은 담당 건축사·인허가권자의 확인을 따릅니다.
  </footer>
</div>

<script>
const F = {findings_json};
const SC = {{pass:'#1b8a3f',fail:'#c62828',needs_review:'#e08600',not_applicable:'#8a8f98'}};
const SK = {{pass:'적합',fail:'부적합',needs_review:'검토필요',not_applicable:'해당없음'}};
let statusFilter = null, domFilter = null, query = '';

const doms = [...new Set(F.map(f=>f.domain))];
const domchips = document.getElementById('domchips');
doms.forEach(d=>{{
  const c = document.createElement('span'); c.className='chip'; c.textContent = F.find(f=>f.domain===d).domain_ko;
  c.onclick=()=>{{domFilter = (domFilter===d)?null:d; syncChips(); render();}};
  c.dataset.dom=d; domchips.appendChild(c);
}});
function syncChips(){{
  document.querySelectorAll('#domchips .chip').forEach(c=>c.classList.toggle('on',c.dataset.dom===domFilter));
  document.querySelectorAll('.kpi').forEach(k=>k.classList.toggle('active',k.dataset.f===statusFilter));
}}
document.querySelectorAll('.kpi').forEach(k=>k.onclick=()=>{{
  statusFilter=(statusFilter===k.dataset.f)?null:k.dataset.f; syncChips(); render();
}});
document.getElementById('q').oninput = e=>{{query=e.target.value.trim().toLowerCase(); render();}};

function esc(s){{return (s||'').replace(/[&<>]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c]));}}
function render(){{
  const list = document.getElementById('list'); list.innerHTML='';
  let shown = F.filter(f=>{{
    if(statusFilter && f.status!==statusFilter) return false;
    if(domFilter && f.domain!==domFilter) return false;
    if(query){{
      const hay=(f.title+' '+f.code+' '+f.law_ref+' '+f.domain_ko+' '+f.message).toLowerCase();
      if(!hay.includes(query)) return false;
    }}
    return true;
  }});
  document.getElementById('hint').textContent =
    `${{shown.length}} / ${{F.length}} 항목 표시` +
    (statusFilter?` · 상태:${{SK[statusFilter]}}`:'') + (domFilter?` · 분야필터`:'') + (query?` · "${{query}}"`:'');
  if(!shown.length){{ list.innerHTML='<div class="empty">조건에 맞는 항목이 없습니다.</div>'; return; }}
  shown.forEach(f=>{{
    const col = SC[f.status];
    const el = document.createElement('div'); el.className='finding'; el.style.borderLeftColor=col;
    let verdict='';
    if(f.status==='pass') verdict = `<b style="color:${{col}}">적합</b> — 실측 ${{f.actual}} · 기준 ${{f.required}}`;
    else if(f.status==='fail') verdict = `<b style="color:${{col}}">부적합</b> — 실측 ${{f.actual}} · 기준 ${{f.required}} ${{f.message?('· '+esc(f.message)):''}}`;
    else if(f.status==='needs_review') verdict = `<b style="color:${{col}}">검토필요</b> — ${{esc(f.message)||'추가 데이터·정성 판단 필요'}}`;
    else verdict = `<b style="color:${{col}}">해당없음</b> — ${{esc(f.message)||'적용 대상 아님'}}`;
    const need = (f.status==='needs_review'||f.status==='fail') && f.fields.length
      ? `<div class="need">필요 데이터: ${{f.fields.map(x=>'<span>'+esc(x)+'</span>').join('')}}</div>` : '';
    el.innerHTML = `
      <div class="row1">
        <span class="badge" style="background:${{col}}">${{SK[f.status]}}</span>
        <span class="title">${{esc(f.title)}}</span>
        <span class="code">${{esc(f.code)}}</span>
        <span class="tag">${{esc(f.domain_ko)}}</span>
        <span class="tag sev-${{f.severity}}">${{f.severity==='mandatory'?'의무':'권장'}}</span>
        ${{f.is_demo?'<span class="tag demo">데모</span>':''}}
      </div>
      <div class="verdict">${{verdict}}</div>
      ${{f.law_ref?`<div class="law">📖 근거: ${{esc(f.law_ref)}}</div>`:''}}
      ${{need}}
      ${{f.excerpt?`<details><summary>원문 발췌 보기</summary><div class="excerpt">${{esc(f.excerpt)}}</div></details>`:''}}
    `;
    list.appendChild(el);
  }});
}}
render();
</script>
</body></html>"""


if __name__ == "__main__":
    main()
