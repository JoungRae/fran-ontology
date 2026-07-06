# -*- coding: utf-8 -*-
"""피난 경로 검토 리포트 — 계단 출입구 자동 추출 + 보행 거리장 + 실별 30m 판정.

헤드 리포트(fire_layout --heads)와 같은 UI/UX 로 별도 산출:
  · 계단 출입구 = 계단 실 폴리곤과 바깥 보행격자가 문(천공)으로 맞닿는 접점 군집
    (기존 rect 인접 방식은 flood 폴리곤 실에서 실패 — 폴리곤 접점 방식으로 대체)
  · 보행 거리장 = 문=통행·벽=차단 100mm 격자, 8방향 다익스트라
  · 실별 최원점 보행거리 → 건축법 시행령 §34(체크 11388) 30m 판정
  · 최원점 → 계단 경사하강 동선, 거리 히트맵

사용법: python evac_report.py 510_지하1층_pit
출력:   output/<도면>_evac_layout.html
"""
import base64
import html
import io
import json
import math
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np

import fire_field as FF

LIMIT_M = 30.0          # 건축법 시행령 §34 원칙 30m
LIMIT_FIRE_M = 50.0     # 주요구조부 내화·불연 완화 50m (공동주택 16층↑ 40m)
EVAL_SKIP = ("PIT", "피트", "대피공간")   # 사람 비상주/피난 목적지 공간
ORIGIN_SKIP = ("계단",)                  # 출발점(자기 자신) 제외
# 문 없는 설비 샤프트 — 거실이 아니므로 피난 평가 대상 아님
SHAFT_SKIP = ("ELEV.", "DA", "AV", "PS", "PD", "EPS", "TPS", "실외기")


def main():
    argv = list(sys.argv[1:])
    # --fire-resist: 주요구조부 내화구조·불연재료 확인됨 → 완화 기준 50m 로 판정
    # (16층 이상 공동주택 지상부는 --limit 40 으로 별도 지정 가능)
    fire_resist = "--fire-resist" in argv
    limit_eff = LIMIT_FIRE_M if fire_resist else LIMIT_M
    if "--limit" in argv:
        i = argv.index("--limit")
        limit_eff = float(argv[i + 1])
        del argv[i:i + 2]
        fire_resist = True
    base = next((a for a in argv if not a.startswith("--")), "지하1층_pit")
    FO = os.path.dirname(os.path.abspath(__file__))
    rooms_data = json.load(open(os.path.join(FO, "output", f"{base}_rooms_rect.json"),
                                encoding="utf-8"))["rooms"]
    cats = json.load(open(os.path.join(FO, "output",
                                       f"{base}_layer_classification.json"),
                          encoding="utf-8"))["categories"]
    ents = json.load(open(os.path.join(FO, "data", f"{base}.json"),
                          encoding="utf-8"))["Entities"]

    rooms = [{"id": i, "name": r["room"], "rect": r["rect"], "poly": r.get("poly")}
             for i, r in enumerate(rooms_data)]

    def cen(r):
        x0, y0, x1, y1 = r["rect"]
        return ((x0 + x1) / 2, (y0 + y1) / 2)

    # ---- 격자 (fire_layout 과 동일 규약) ----
    no_plot = set()
    for src in (os.path.join(FO, "data", f"{base}.json"),
                os.path.join(FO, "data", "1층.json")):
        try:
            for L in json.load(open(src, encoding="utf-8")).get("Layers", []):
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

    wall_walk = segs_of({"wall_struct", "wall_nonstruct", "column_struct"})
    win_segs = segs_of({"window"})
    door_segs = segs_of({"door"})
    door_lys = {ly for ly, c in cats.items() if c == "door"}
    carve = []
    for e in ents:
        if e.get("Type") != "Arc" or e.get("Layer") not in door_lys:
            continue
        rr = e.get("Radius", 0)
        if not (300 <= rr <= 1500):
            continue
        cx, cy = e["Center"][:2]
        for ang in (e.get("StartAngle", 0.0), e.get("EndAngle", 0.0)):
            carve.append((cx, cy, cx + rr * math.cos(ang), cy + rr * math.sin(ang)))

    xs = [c for s in wall_walk for c in (s[0], s[2])] + \
         [c for r in rooms for c in (r["rect"][0], r["rect"][2])]
    ys = [c for s in wall_walk for c in (s[1], s[3])] + \
         [c for r in rooms for c in (r["rect"][1], r["rect"][3])]
    bounds = (min(xs), min(ys), max(xs), max(ys))
    grid = FF.build_grid(wall_walk, win_segs + door_segs, bounds,
                         carve_segs=carve + door_segs)
    from matplotlib.path import Path as _MPath

    def room_mask_of(r):
        if r.get("poly"):
            x0 = min(p[0] for p in r["poly"]); y0 = min(p[1] for p in r["poly"])
            x1 = max(p[0] for p in r["poly"]); y1 = max(p[1] for p in r["poly"])
        else:
            x0, y0, x1, y1 = r["rect"]
        j0, i0 = FF.to_cell(grid, x0, y0)
        j1, i1 = FF.to_cell(grid, x1, y1)
        j0, i0 = max(j0, 0), max(i0, 0)
        j1, i1 = min(j1, grid["H"] - 1), min(i1, grid["W"] - 1)
        m = np.zeros_like(grid["walkable"])
        if j1 < j0 or i1 < i0:
            return m
        if r.get("poly"):
            jj, ii = np.mgrid[j0:j1 + 1, i0:i1 + 1]
            pts = np.column_stack([grid["x0"] + (ii.ravel() + 0.5) * FF.CELL,
                                   grid["y0"] + (jj.ravel() + 0.5) * FF.CELL])
            m[j0:j1 + 1, i0:i1 + 1] = \
                _MPath(r["poly"]).contains_points(pts).reshape(jj.shape)
            for _ in range(2):
                m = FF._dilate(m) & grid["walkable"]
        else:
            m[j0:j1 + 1, i0:i1 + 1] = True
        return m

    # ---- 계단 출입구: 계단 실 마스크와 바깥 보행셀의 접점(문 천공부) 군집 ----
    stair_rooms = [r for r in rooms if "계단" in r["name"]]
    stair_mask = np.zeros_like(grid["walkable"])
    for r in stair_rooms:
        stair_mask |= room_mask_of(r)
    portal = FF._dilate(stair_mask) & grid["walkable"] & ~stair_mask
    # 접점 셀 연결성분 → 출입구 점
    from collections import deque
    lab = np.zeros_like(portal, dtype=np.int32)
    exits = []
    for sj, si in np.argwhere(portal):
        if lab[sj, si]:
            continue
        cid = len(exits) + 1
        q = deque([(sj, si)])
        lab[sj, si] = cid
        cells = [(sj, si)]
        while q:
            j, i = q.popleft()
            for dj, di in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1),
                           (-1, 1), (-1, -1)):
                nj, ni = j + dj, i + di
                if 0 <= nj < grid["H"] and 0 <= ni < grid["W"] \
                        and portal[nj, ni] and not lab[nj, ni]:
                    lab[nj, ni] = cid
                    q.append((nj, ni))
                    cells.append((nj, ni))
        if len(cells) < 3:              # 래스터 파편 제외
            continue
        cj = sum(c[0] for c in cells) / len(cells)
        ci = sum(c[1] for c in cells) / len(cells)
        exits.append(FF.to_xy(grid, cj, ci))
    print(f"계단 {len(stair_rooms)}실 → 출입구 {len(exits)}개 추출")

    # ---- 보행 거리장 ----
    dist = FF.distance_field(grid, exits)
    fin = np.isfinite(dist) & grid["walkable"]

    # ---- 실별 최원거리 + 판정 ----
    def is_eval(r):
        u = r["name"].upper()
        return not any(k in u for k in EVAL_SKIP + ORIGIN_SKIP + SHAFT_SKIP)

    rows = []          # (name, area_m2, rmax_m, unreach, verdict, worst_xy)
    paths = []
    wmax, worst_xy = 0.0, None
    for r in rooms:
        if not is_eval(r):
            continue
        m = room_mask_of(r) & grid["walkable"]
        n_all = int(m.sum())
        if n_all == 0:
            continue
        mf = m & fin
        unreach = n_all - int(mf.sum())
        if mf.any():
            d_ = np.where(mf, dist, -1)
            k = int(d_.argmax())
            j, i = np.unravel_index(k, d_.shape)
            rmax = float(dist[j, i])
            wxy = FF.to_xy(grid, j, i)
            if rmax > wmax:
                wmax, worst_xy = rmax, wxy
            p = FF.descend_path(grid, dist, *wxy)
            if p:
                paths.append((r["name"], rmax, p))
            if rmax <= LIMIT_M * 1000:
                v = "적합"
            elif rmax <= limit_eff * 1000:
                v = "적합(완화)" if fire_resist else "확인필요"
            elif rmax <= LIMIT_FIRE_M * 1000 and not fire_resist:
                v = "확인필요"
            else:
                v = "부적합"
        else:
            rmax, wxy, v = None, None, "미도달"
        ar = rooms_data[r["id"]].get("area_m2") or \
            (r["rect"][2] - r["rect"][0]) * (r["rect"][3] - r["rect"][1]) / 1e6
        rows.append((r["name"], ar, rmax, unreach, v, wxy))
    n_unreach_rooms = sum(1 for x in rows if x[4] == "미도달")
    print(f"실 {len(rows)}곳 평가 · 최악 보행거리 {wmax/1000:.1f}m · 미도달 실 {n_unreach_rooms}"
          + (f" — {[x[0] for x in rows if x[4] == '미도달']}" if n_unreach_rooms else ""))

    # ---- 히트맵 ----
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    hm = np.where(fin, dist / 1000.0, np.nan)
    cm = plt.get_cmap("RdYlGn_r").copy()
    rgba = cm(np.clip(hm / limit_eff, 0, 1))
    rgba[..., 3] = np.where(np.isnan(hm), 0.0, 0.55)
    buf = io.BytesIO()
    plt.imsave(buf, rgba[::-1], format="png")
    b64 = base64.b64encode(buf.getvalue()).decode()

    # ---- SVG ----
    minx, miny, maxx, maxy = bounds

    def fy(y):
        return round(maxy - y)

    G = {k: [] for k in ("heat", "walls", "rooms", "labels", "esc", "exit", "worst")}
    gx0, gy0 = grid["x0"], grid["y0"]
    gw, gh = grid["W"] * FF.CELL, grid["H"] * FF.CELL
    G["heat"].append(f'<image x="{round(gx0)}" y="{fy(gy0 + gh)}" width="{round(gw)}" '
                     f'height="{round(gh)}" preserveAspectRatio="none" '
                     f'href="data:image/png;base64,{b64}"/>')
    G["walls"].append('<path d="' + "".join(
        f"M{round(a)} {fy(b)}L{round(c)} {fy(d)}" for a, b, c, d in wall_walk) + '"/>')
    for r in rooms:
        if r.get("poly"):
            pts = " ".join(f"{p[0]},{fy(p[1])}" for p in r["poly"])
            G["rooms"].append(f'<polygon points="{pts}"/>')
        else:
            x0, y0, x1, y1 = r["rect"]
            G["rooms"].append(f'<rect x="{x0}" y="{fy(y1)}" width="{x1-x0}" '
                              f'height="{y1-y0}"/>')
        cx, cy = cen(r)
        G["labels"].append(f'<text x="{round(cx)}" y="{fy(cy)}">'
                           f'{html.escape(r["name"])}</text>')
    for nm, rmax, p in paths:
        d = " ".join(f"{round(x)},{fy(y)}" for x, y in p)
        G["esc"].append(f'<polyline points="{d}"><title>{html.escape(nm)} 최원점 → '
                        f'계단 {rmax/1000:.1f}m</title></polyline>')
    for x, y in exits:
        G["exit"].append(f'<rect x="{round(x)-260}" y="{fy(y)-260}" width="520" '
                         f'height="520"><title>계단 출입구</title></rect>')
    for nm, ar, rmax, unreach, v, wxy in rows:
        if wxy and v in ("확인필요", "부적합"):
            G["worst"].append(f'<circle cx="{round(wxy[0])}" cy="{fy(wxy[1])}" r="300">'
                              f'<title>{html.escape(nm)} 최원점 {rmax/1000:.1f}m</title>'
                              f'</circle>')
    if worst_xy:
        G["worst"].append(f'<circle class="gmax" cx="{round(worst_xy[0])}" '
                          f'cy="{fy(worst_xy[1])}" r="380">'
                          f'<title>전체 최악점 {wmax/1000:.1f}m</title></circle>')
    groups_svg = "\n".join(f'<g id="g-{k}">{"".join(v)}</g>' for k, v in G.items())
    pad = 3000
    vb = f"{round(minx)-pad} {-pad} {round(maxx-minx)+2*pad} {round(maxy-miny)+2*pad}"

    # ---- UI 데이터 ----
    ok_all = wmax <= limit_eff * 1000 and n_unreach_rooms == 0

    def _chip(txt, cls=""):
        return f'<span class="chip {cls}">{txt}</span>'

    chips = (_chip(f'계단 출입구 <b>{len(exits)}</b>')
             + _chip(f'평가 실 <b>{len(rows)}</b>')
             + _chip(f"최악 보행거리 {wmax/1000:.1f}m",
                     "ok" if wmax <= limit_eff * 1000 else "warn")
             + _chip(f"{limit_eff:.0f}m 기준 {'✓' if wmax <= limit_eff*1000 else '⚠'}",
                     "ok" if wmax <= limit_eff * 1000 else "warn"))
    if fire_resist:
        chips += _chip(f"내화구조 완화 적용({limit_eff:.0f}m)")
    if n_unreach_rooms:
        chips += _chip(f"미도달 실 ⚠ {n_unreach_rooms}", "warn")

    VCLS = {"적합": "ok", "적합(완화)": "ok", "부적합": "bad", "확인필요": "chk",
            "미도달": "bad", "해당없음": "na", "미검증": "na"}
    rows_s = sorted(rows, key=lambda x: -(x[2] or 9e9))
    tbl = ('<table class="tbl"><thead><tr><th>실</th><th>㎡</th><th>보행거리</th>'
           '<th>판정</th></tr></thead><tbody>'
           + "".join(
               f'<tr><td>{html.escape(nm)}</td><td class="num">{ar:.0f}</td>'
               f'<td class="num">{f"{rmax/1000:.1f}m" if rmax else "—"}</td>'
               f'<td><span class="vd {VCLS[v]}">{v}</span></td></tr>'
               for nm, ar, rmax, unreach, v, _ in rows_s)
           + '</tbody></table>')

    LOGIC = [
        ("계단 출입구 추출 — {}개".format(len(exits)),
         "계단 실 폴리곤을 1셀 팽창해 바깥 보행격자와 맞닿는 접점(=문 천공부)을\n"
         "군집화 → 각 군집 중심이 출입구. 도면에 문 기호가 없어도 통행 가능\n"
         "개구부가 있으면 자동 인식."),
        ("보행 거리장 — 100mm 격자",
         "문 = 통행, 벽·기둥 = 차단으로 래스터화.\n"
         "모든 출입구에서 8방향 다익스트라 — '각 부분으로부터'를 셀 단위로 계산.\n"
         "라벨 없는 통로·개방 공간도 자동 포함."),
        ("실별 최원점 판정 — {}실".format(len(rows)),
         "각 실에서 계단까지 가장 먼 지점의 보행거리로 판정:\n"
         "· ≤ 30m → 적합 (건축법 시행령 §34 원칙)\n"
         + ("· 30~{:.0f}m → 적합(완화) — 내화구조 확인됨(사용자 지정)\n".format(limit_eff)
            if fire_resist else
            "· 30~50m → 확인필요 (주요구조부 내화·불연 완화 검토)\n")
         + "· 도달 불가 → 미도달(피난 불능 — 최우선 확인)"),
        ("피난 동선 — 경사 하강",
         "각 실 최원점에서 거리장이 줄어드는 방향으로 내려가면\n"
         "실제 개구부(문)를 지나는 최단 경로가 그려진다.\n"
         "히트맵: 초록(가까움) → 빨강(30m)."),
    ]
    logic_html = "".join(
        f'<details{" open" if i == 0 else ""}><summary><span class="stepno">{i+1}</span>'
        f'{html.escape(t)}</summary><div class="quote">{html.escape(q)}</div></details>'
        for i, (t, q) in enumerate(LOGIC))

    n_over = sum(1 for x in rows if x[4] in ("부적합",))
    n_chk = sum(1 for x in rows if x[4] == "확인필요")
    EV = [
        {"v": ("적합" if wmax <= LIMIT_M * 1000 else
               ("적합(완화)" if (fire_resist and wmax <= limit_eff * 1000) else
                ("확인필요" if wmax <= LIMIT_FIRE_M * 1000 else "부적합"))),
         "t": f"직통계단 보행거리 ≤ {limit_eff:.0f}m"
              + (" (내화 완화)" if fire_resist else ""),
         "src": "건축법 시행령 제34조 — 체크 11388",
         "law": "거실의 각 부분으로부터 계단(직통계단)에 이르는 보행거리가 30미터 이하가 "
                "되도록 설치할 것. 주요구조부가 내화구조 또는 불연재료인 건축물은 50미터"
                "(16층 이상 공동주택은 40미터) 이하.",
         "res": f"최악 보행거리 {wmax/1000:.1f}m (전 실 셀 단위 전수) · 30m 초과 실 "
                f"{n_over + n_chk + sum(1 for x in rows if x[4] == '적합(완화)')}곳"
                + (f"\n내화구조 완화 적용(사용자 지정) → 기준 {limit_eff:.0f}m 로 판정."
                   if fire_resist else
                   ("" if wmax <= LIMIT_M * 1000 else
                    "\n내화구조 완화(50m) 적용 가능 여부 확인 필요 — 확인되면 "
                    "--fire-resist 옵션으로 재판정."))},
        {"v": ("적합" if n_unreach_rooms == 0 else "부적합"),
         "t": "전 실 피난 도달성",
         "src": "피난 경로 성립 전제 (보행거리 판정의 선행 조건)",
         "law": "모든 거실에서 직통계단까지 통행 가능한 경로가 존재해야 한다.",
         "res": (f"평가 {len(rows)}실 전부 계단 도달 가능." if n_unreach_rooms == 0 else
                 f"⚠ 미도달 실 {n_unreach_rooms}곳 — 문 인식 누락 또는 실제 피난 불능. "
                 f"도면·문 기호 확인 필요.")},
        {"v": "미검증", "t": "복도 유효너비·피난계단 구조",
         "src": "건축법 시행령 §48, 피난·방화규칙 등",
         "law": "복도 유효너비, 피난계단·특별피난계단의 구조 요건 등.",
         "res": "복도 폭은 법령 검토 리포트(compliance)에서 별도 판정 — 본 리포트 범위 밖."},
    ]
    VB = {"적합": "ok", "적합(완화)": "ok", "부적합": "bad", "확인필요": "chk",
          "미검증": "na", "해당없음": "na"}
    legal_html = "".join(
        f'<details{" open" if e["v"] in ("부적합",) else ""}>'
        f'<summary><span class="vd {VB[e["v"]]}">{e["v"]}</span>{html.escape(e["t"])}'
        f'</summary><div class="cite">{html.escape(e["src"])}</div>'
        f'<div class="law"><span class="lb">조문</span>{html.escape(e["law"])}</div>'
        f'<div class="applied"><span class="lb aplb">결과 비교</span>'
        f'{html.escape(e["res"])}</div></details>'
        for e in EV)

    # ---- AI 챗 ----
    rpt = {"도면": base, "종류": "피난 경로 검토",
           "계단출입구": len(exits), "최악보행거리_m": round(wmax / 1000, 1),
           "기준": "30m(원칙)/50m(내화 완화, 16층↑ 공동주택 40m)",
           "적용판정기준_m": limit_eff, "내화완화적용": fire_resist,
           "미도달실": n_unreach_rooms,
           "실별": [{"실": nm, "면적m2": round(ar), "보행거리_m":
                    (round(rmax / 1000, 1) if rmax else None), "판정": v}
                   for nm, ar, rmax, unreach, v, _ in rows_s],
           "판정로직": [t for t, _ in LOGIC],
           "법적검토": [{"항목": e["t"], "판정": e["v"], "출처": e["src"],
                       "결과비교": e["res"]} for e in EV]}
    rpt_json = json.dumps(rpt, ensure_ascii=False).replace("</", "<\\/")
    # OpenAI 키는 절대 서버/파일에서 읽어와 내장하지 않는다 — 공개 배포 리포트이므로
    # 사용자가 브라우저에서 직접 입력해 localStorage 에만 저장하게 한다.
    api_key = ""
    chat_js = r"""
(function(){
 var RPT=__RPT__;
 var dock=document.getElementById('chatdock');
 var msgs=document.getElementById('chatmsgs'),cin=document.getElementById('cin');
 var tg=document.getElementById('chattoggle');
 function setOpen(o){dock.classList.toggle('closed',!o);tg.textContent=o?'접기 ▾':'열기 ▴';
  if(o)cin.focus();}
 tg.onclick=function(e){e.stopPropagation();setOpen(dock.classList.contains('closed'));};
 document.getElementById('chathead').onclick=function(){setOpen(dock.classList.contains('closed'));};
 var keyBox=document.getElementById('chatkey');
 var EMB=__KEY__;
 function getKey(){return localStorage.getItem('openai_key')||EMB;}
 function syncKey(){keyBox.style.display=getKey()?'none':'flex';}
 document.getElementById('okeysave').onclick=function(){
  var v=document.getElementById('okey').value.trim();
  if(v){localStorage.setItem('openai_key',v);syncKey();}};
 syncKey();
 var hist=[];
 function esch(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
 function md(s){s=esch(s);
  s=s.replace(/`([^`\n]+)`/g,'<code>$1</code>');
  s=s.replace(/\*\*([^*\n]+)\*\*/g,'<b>$1</b>');
  s=s.replace(/(^|\n)#{1,4} +(.+)/g,'$1<b class="h">$2</b>');
  s=s.replace(/(^|\n)[ \t]*[-*] +/g,'$1• ');
  return s;}
 function add(role,txt){var d=document.createElement('div');d.className='cm '+role;
  if(role==='ai'){d.innerHTML=md(txt);}else{d.textContent=txt;}
  msgs.appendChild(d);msgs.scrollTop=msgs.scrollHeight;return d;}
 add('sys','피난 경로 결과에 대해 질문하세요. 예: "가장 먼 방은 어디야?", "미도달 실이 생기는 원인은?"');
 var SYS='너는 건축물 피난 경로 검토 전문가다. 아래 JSON은 보행거리 자동 검토 결과다. '
  +'질문에는 판정로직과 법적검토(조문·체크번호)를 인용해 간결한 한국어로 답하라. '
  +'데이터에 없는 값은 추정하지 말라. 굵게와 불릿만 쓰고 표는 쓰지 마라.\n결과: '
  +JSON.stringify(RPT);
 async function send(){
  var q=cin.value.trim(); if(!q)return;
  if(!getKey()){add('sys','먼저 OpenAI API 키를 입력·저장하세요.');return;}
  cin.value=''; add('user',q); hist.push({role:'user',content:q});
  var wait=add('ai','생각 중…');
  try{
   var res=await fetch('https://api.openai.com/v1/chat/completions',{method:'POST',
    headers:{'Content-Type':'application/json','Authorization':'Bearer '+getKey()},
    body:JSON.stringify({model:'gpt-4o-mini',temperature:0.3,
     messages:[{role:'system',content:SYS}].concat(hist.slice(-12))})});
   if(!res.ok){var t=await res.text();throw new Error(res.status+' '+t.slice(0,140));}
   var j=await res.json();
   var a=(j.choices&&j.choices[0]&&j.choices[0].message.content)||'(응답 없음)';
   wait.innerHTML=md(a); hist.push({role:'assistant',content:a});
   msgs.scrollTop=msgs.scrollHeight;
  }catch(err){wait.textContent='오류: '+err.message;wait.className='cm sys';}
 }
 document.getElementById('csend').onclick=send;
 cin.addEventListener('keydown',function(e){
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}});
})();
""".replace("__RPT__", rpt_json).replace("__KEY__", json.dumps(api_key))

    _m = re.search(r"(지하\s*\d+층|기준층|옥탑|\d+층)", base)
    disp = _m.group(1) if _m else base
    legend = (
        '<div class="lg-sec"><div class="lg-t">피난</div><div class="lg-grid">'
        + "".join(f'<label><input type="checkbox" data-g="{g}" checked/>'
                  f'<span style="color:{c}">{t}</span></label>'
                  for g, c, t in [
                      ("g-heat", "#d84315", f"거리 히트맵(0→{limit_eff:.0f}m)"),
                      ("g-esc", "#2e7d32", f"피난 동선 ({len(paths)})"),
                      ("g-exit", "#1e5cb3", f"계단 출입구 ({len(exits)})"),
                      ("g-worst", "#d84315", "초과·최악점")])
        + '</div></div><div class="lg-sec"><div class="lg-t">도면</div><div class="lg-grid">'
        + "".join(f'<label><input type="checkbox" data-g="{g}" checked/>'
                  f'<span style="color:{c}">{t}</span></label>'
                  for g, c, t in [("g-labels", "#5a6578", "실 이름"),
                                  ("g-rooms", "#94a3b8", "방 경계"),
                                  ("g-walls", "#9aa5b5", "벽 선형")])
        + '</div></div>')

    page = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"/>
<title>피난 경로 검토 · {html.escape(disp)}</title><style>
:root{{--bg:#eef1f5;--panel:#fff;--ink:#1e2937;--mut:#64748b;--line:#e2e8f0;
--acc:#2563eb;--ok:#16803c;--warn:#c2410c}}
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
font-size:12px;margin-left:6px;color:#dbe6f5;white-space:nowrap}}
.chip b{{color:#fff}}
.chip.ok{{background:rgba(34,197,94,.16);border-color:rgba(34,197,94,.4);color:#bbf7d0}}
.chip.warn{{background:rgba(249,115,22,.18);border-color:rgba(249,115,22,.5);color:#fed7aa}}
aside{{position:fixed;top:62px;bottom:58px;z-index:10;width:335px;overflow-y:auto;
display:flex;flex-direction:column;gap:10px;scrollbar-width:thin}}
#left{{left:12px}} #right{{right:12px;width:375px}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;
padding:12px 14px;box-shadow:0 4px 18px rgba(15,29,51,.08);font-size:12.5px;
line-height:1.55;flex-shrink:0}}
.card h3{{margin:0 0 8px;font-size:11px;color:var(--mut);letter-spacing:.14em;
font-weight:800}}
.tbl{{width:100%;border-collapse:collapse;font-size:12.5px}}
.tbl th{{text-align:left;color:var(--mut);font-weight:600;font-size:11px;
border-bottom:1px solid var(--line);padding:3px 4px}}
.tbl td{{padding:4px;border-bottom:1px solid #f1f5f9}}
.tbl td.num{{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}}
#legend .lg-sec{{margin:2px 0 8px}}
#legend .lg-t{{font-size:10.5px;color:var(--mut);letter-spacing:.1em;font-weight:800;
margin:4px 0 3px}}
#legend .lg-grid{{display:grid;grid-template-columns:1fr 1fr;gap:2px 8px}}
#legend label{{cursor:pointer;user-select:none;white-space:nowrap;font-size:12px}}
#legend input{{vertical-align:-2px;margin-right:4px;accent-color:var(--acc)}}
details{{margin:6px 0;border:1px solid var(--line);border-radius:9px;padding:6px 10px;
background:#fbfcfe}}
summary{{cursor:pointer;font-weight:650;font-size:12.5px}}
.stepno{{display:inline-flex;width:17px;height:17px;border-radius:50%;
background:var(--acc);color:#fff;font-size:10.5px;align-items:center;
justify-content:center;margin-right:7px}}
.cite{{color:#2456a6;font-size:11px;margin:6px 0 4px}}
.quote{{color:#3d4a5c;font-size:12px;background:#f3f6fa;border-left:3px solid #c3ced9;
padding:7px 10px;border-radius:4px;white-space:pre-wrap;line-height:1.7}}
.law{{color:#3d4a5c;font-size:12px;background:#f3f6fa;border-left:3px solid #94a3b8;
padding:7px 10px;border-radius:4px;margin-bottom:5px;line-height:1.65}}
.applied{{color:#14532d;font-size:12px;background:#f0faf3;border-left:3px solid #22c55e;
padding:7px 10px;border-radius:4px;line-height:1.65}}
.lb{{display:block;font-size:10px;font-weight:800;letter-spacing:.1em;color:#64748b;
margin-bottom:2px}}
.lb.aplb{{color:#16803c}}
.vd{{display:inline-block;min-width:44px;text-align:center;border-radius:6px;
padding:1px 7px;font-size:10.5px;font-weight:800;margin-right:7px}}
.vd.ok{{background:#dcfce7;color:#15803d}}
.vd.bad{{background:#fee2e2;color:#b91c1c}}
.vd.chk{{background:#ffedd5;color:#c2410c}}
.vd.na{{background:#f1f5f9;color:#94a3b8}}
#chatdock{{position:fixed;left:50%;transform:translateX(-50%);bottom:0;
width:min(760px,92vw);z-index:30;background:#fff;border:1px solid var(--line);
border-bottom:none;border-radius:14px 14px 0 0;
box-shadow:0 -6px 30px rgba(15,29,51,.18);display:flex;flex-direction:column;
height:380px;transition:height .22s}}
#chatdock.closed{{height:44px;overflow:hidden}}
#chathead{{display:flex;align-items:center;gap:8px;padding:10px 14px;cursor:pointer;
font-weight:700;font-size:13px;flex-shrink:0}}
#chathead small{{color:var(--mut);font-weight:500}}
#chathead .sp{{flex:1}}
#chathead button{{border:1px solid var(--line);background:#f8fafc;border-radius:7px;
padding:3px 10px;font-size:11.5px;cursor:pointer}}
#chatbody{{display:flex;flex-direction:column;flex:1;min-height:0;
border-top:1px solid var(--line)}}
#chatmsgs{{flex:1;overflow-y:auto;padding:10px 14px;display:flex;
flex-direction:column;gap:8px}}
.cm{{max-width:84%;padding:7px 11px;border-radius:12px;font-size:12.5px;
line-height:1.6;white-space:pre-wrap}}
.cm.user{{align-self:flex-end;background:var(--acc);color:#fff;
border-bottom-right-radius:4px}}
.cm.ai{{align-self:flex-start;background:#f1f5f9;border-bottom-left-radius:4px}}
.cm.sys{{align-self:center;background:#fff7ed;color:#9a3412;
border:1px solid #fed7aa;font-size:11.5px}}
.cm code{{background:#e2e8f0;border-radius:4px;padding:0 4px;
font-family:Consolas,monospace;font-size:11.5px}}
.cm b.h{{display:block;margin:6px 0 2px;font-size:13px}}
#chatkey{{display:flex;gap:6px;padding:8px 14px;border-top:1px solid var(--line);
align-items:center;font-size:12px;color:var(--mut)}}
#chatkey input{{flex:1;border:1px solid var(--line);border-radius:7px;
padding:5px 9px;font-size:12px}}
#chatin{{display:flex;gap:8px;padding:10px 14px;border-top:1px solid var(--line)}}
#chatin textarea{{flex:1;resize:none;border:1px solid var(--line);border-radius:9px;
padding:8px 11px;font-size:12.5px;font-family:inherit}}
#chatin button,#chatkey button{{border:none;background:var(--acc);color:#fff;
border-radius:9px;padding:0 16px;font-size:12.5px;cursor:pointer;font-weight:600}}
#stage{{position:fixed;inset:52px 0 0 0;background:#fff;cursor:grab}}
svg{{width:100%;height:100%;display:block}}
#g-walls path{{stroke:#c9cfda;fill:none;stroke-width:1;vector-effect:non-scaling-stroke}}
#g-rooms polygon,#g-rooms rect{{fill:none;stroke:#94a3b8;stroke-width:1;
vector-effect:non-scaling-stroke}}
#g-labels text{{font-size:380px;fill:#5a6578;text-anchor:middle;
dominant-baseline:middle;paint-order:stroke;stroke:#fff;stroke-width:70px}}
#g-esc polyline{{fill:none;stroke:#2e7d32;stroke-width:2.4;stroke-dasharray:10 6;
vector-effect:non-scaling-stroke}}
#g-exit rect{{fill:#1e5cb3}}
#g-worst circle{{fill:none;stroke:#d84315;stroke-width:2.6;
vector-effect:non-scaling-stroke}}
#g-worst .gmax{{stroke-width:4}}
</style></head><body>
<header id="hd">
 <span class="brand">🚪 FRAN 피난경로 검토<small>보행거리 · {html.escape(disp)}</small></span>
 <span class="sp"></span>
 {chips}
</header>
<div id="stage"><svg id="svg" viewBox="{vb}" xmlns="http://www.w3.org/2000/svg">
{groups_svg}
</svg></div>
<aside id="left">
 <section class="card"><h3>실별 보행거리 — 계단까지</h3>{tbl}
  <div class="meta" style="color:var(--mut);font-size:11.5px;margin-top:6px">
  {'판정: ≤30m 적합 · 30~' + f'{limit_eff:.0f}' + 'm 적합(완화·내화구조) · 도달불가 미도달'
   if fire_resist else
   '판정: ≤30m 적합 · 30~50m 확인필요(내화 완화) · 도달불가 미도달'}</div></section>
 <section class="card"><h3>표시 항목</h3><div id="legend">{legend}</div></section>
</aside>
<aside id="right">
 <section class="card"><h3>판정 로직</h3><div id="logic">{logic_html}</div></section>
 <section class="card"><h3>법적 검토 — 결과 대조</h3><div id="legal">{legal_html}</div></section>
</aside>
<div id="chatdock" class="closed">
 <div id="chathead">💬 AI 검토 어시스턴트 <small>피난 결과에 대해 질문·논의</small>
  <span class="sp"></span><button id="chattoggle">열기 ▴</button></div>
 <div id="chatbody">
  <div id="chatmsgs"></div>
  <div id="chatkey"><span>OpenAI API 키</span>
   <input id="okey" type="password" placeholder="sk-..."/>
   <button id="okeysave">저장</button></div>
  <div id="chatin"><textarea id="cin" rows="2"
   placeholder="예: 가장 먼 방은 어디고 몇 m야?"></textarea>
   <button id="csend">전송</button></div>
 </div>
</div>
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
document.querySelectorAll('#legend input').forEach(function(cb){{
cb.onchange=function(){{var g=document.getElementById(cb.dataset.g);
if(g)g.style.display=cb.checked?'':'none';}};}});
}})();
{chat_js}
</script></body></html>"""

    op = os.path.join(FO, "output", f"{base}_evac_layout.html")
    open(op, "w", encoding="utf-8").write(page)
    print(f"출력: {op}")


if __name__ == "__main__":
    main()
