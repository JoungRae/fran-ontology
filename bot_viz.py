"""
BOT 그래프(.ttl)를 평면도 위에 시각화하는 자체 완결형 HTML(SVG) 생성기.

  방(bot:Space)   = 세대 색으로 칠한 사각형 + 방이름
  인접(adjacentZone) = 방 중심을 잇는 회색 선
  개구부(Interface)  = 문(초록 사각)·창(파랑 원) 마커
줌/팬 지원. 외부 의존성: rdflib 만.

  python bot_viz.py [도면베이스명]   # 기본 1층
"""

import argparse
import html
import json
import os
import sys

from rdflib import Graph, Namespace

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BOT = Namespace("https://w3id.org/bot#")
FRAN = Namespace("https://example.org/fran#")
INST = Namespace("https://example.org/fran/inst#")

UNIT_COLORS = ["#1e88e5", "#e53935", "#43a047", "#8e24aa",
               "#fb8c00", "#00acc1", "#c0ca33", "#6d4c41"]


def rid(uri, g=None):
    """Space URI → 도면 내 방 순번. URI 는 안정 이름이라 번호를 담지 않으므로
    그래프의 fran:roomIndex 를 읽는다(build_bot 이 실어 보낸다)."""
    if g is not None:
        for o in g.objects(uri, FRAN.roomIndex):
            return int(o)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base", nargs="?", default="1층")
    ap.add_argument("--out-dir", default="output")
    args = ap.parse_args()
    base = args.base
    rects = {i: r["rect"] for i, r in enumerate(
        json.load(open(os.path.join(args.out_dir, f"{base}_rooms_rect.json"),
                       encoding="utf-8"))["rooms"])}
    names = {i: r["room"] for i, r in enumerate(
        json.load(open(os.path.join(args.out_dir, f"{base}_rooms_rect.json"),
                       encoding="utf-8"))["rooms"])}

    g = Graph()
    g.parse(os.path.join(args.out_dir, f"{base}.ttl"), format="turtle")
    ns = {"bot": BOT, "fran": FRAN}

    # 방 → 세대 색인
    unit_of = {}
    for k, (u,) in enumerate(g.query(
            "SELECT ?u WHERE { ?u a fran:DwellingUnit } ORDER BY ?u", initNs=ns)):
        for (s,) in g.query(
                "SELECT ?s WHERE { ?u bot:hasSpace ?s . ?s a bot:Space }",
                initNs=ns, initBindings={"u": u}):
            unit_of[rid(s, g)] = k

    adj = set()
    for a, b in g.query(
            "SELECT ?a ?b WHERE { ?a bot:adjacentZone ?b FILTER(STR(?a)<STR(?b)) }",
            initNs=ns):
        adj.add((rid(a, g), rid(b, g)))

    ifaces = []   # (x, y, kind)
    for e, wkt in g.query(
            # 개구부 요소는 interfaceOf 의 끝점 중 Door/Window 인 것
            # (bot:hasElement 는 domain 이 Zone 이라 Interface 에 못 건다)
            """SELECT ?e ?w WHERE { ?i a bot:Interface ; bot:interfaceOf ?e .
               ?e a ?t . FILTER(?t IN (fran:Door, fran:Window))
               ?e geo:hasGeometry/geo:asWKT ?w }""",
            initNs={**ns, "geo": Namespace("http://www.opengis.net/ont/geosparql#")}):
        s = str(wkt)
        kind = "door" if "Door" in str(e) else "window"
        xy = s[s.find("(") + 1:s.find(")")].split()
        ifaces.append((float(xy[0]), float(xy[1]), kind))

    # 좌표 경계 + Y 반전
    xs = [c for r in rects.values() for c in (r[0], r[2])]
    ys = [c for r in rects.values() for c in (r[1], r[3])]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    pad = max(maxx - minx, maxy - miny) * 0.03

    def fy(y):
        return maxy - y

    def cen(i):
        r = rects[i]
        return (r[0] + r[2]) / 2, fy((r[1] + r[3]) / 2)

    svg = []
    # 인접 선(맨 아래)
    for i, j in adj:
        x1, y1 = cen(i)
        x2, y2 = cen(j)
        svg.append(f'<line class="adj" x1="{x1:.0f}" y1="{y1:.0f}" '
                   f'x2="{x2:.0f}" y2="{y2:.0f}"/>')
    # 방 사각형 + 라벨
    for i, r in rects.items():
        col = UNIT_COLORS[unit_of.get(i, 0) % len(UNIT_COLORS)]
        x, y, w, h = r[0], fy(r[3]), r[2] - r[0], r[3] - r[1]
        svg.append(f'<rect class="room" x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" '
                   f'height="{h:.0f}" fill="{col}" fill-opacity="0.18" stroke="{col}"/>')
        cx, cy = cen(i)
        svg.append(f'<text class="lbl" x="{cx:.0f}" y="{cy:.0f}">'
                   f'{html.escape(names[i])}</text>')
    # 개구부 마커(맨 위)
    for x, y, kind in ifaces:
        sy = fy(y)
        if kind == "door":
            svg.append(f'<rect class="door" x="{x-140:.0f}" y="{sy-140:.0f}" '
                       f'width="280" height="280"/>')
        else:
            svg.append(f'<circle class="win" cx="{x:.0f}" cy="{sy:.0f}" r="150"/>')

    vb = f"{minx-pad:.0f} {fy(maxy)-pad:.0f} {maxx-minx+2*pad:.0f} {maxy-miny+2*pad:.0f}"
    n_units = len(set(unit_of.values()))
    legend = " ".join(
        f'<span style="color:{UNIT_COLORS[k%len(UNIT_COLORS)]}">■ 세대{k+1}</span>'
        for k in range(n_units))

    page = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"/>
<title>BOT 위상 · {html.escape(base)}</title><style>
html,body{{margin:0;height:100%;font-family:system-ui,sans-serif}}
#bar{{position:fixed;top:8px;left:8px;z-index:10;background:rgba(255,255,255,.94);
border:1px solid #ccc;border-radius:6px;padding:7px 11px;font-size:13px}}
#bar b{{font-size:14px}} #bar .k{{color:#555;margin-left:8px}}
#stage{{width:100vw;height:100vh;background:#fff;cursor:grab}}
svg{{width:100%;height:100%;display:block}}
.adj{{stroke:#bbb;stroke-width:1.2;vector-effect:non-scaling-stroke}}
.room{{stroke-width:2;vector-effect:non-scaling-stroke}}
.lbl{{font-size:340px;fill:#111;text-anchor:middle;dominant-baseline:middle;
paint-order:stroke;stroke:#fff;stroke-width:60px}}
.door{{fill:#2e7d32;fill-opacity:.85}} .win{{fill:#1565c0;fill-opacity:.9}}
</style></head><body>
<div id="bar"><b>BOT 위상 뷰</b> · {html.escape(base)}
<span class="k">방 {len(rects)} · 세대 {n_units} · 인접 {len(adj)} · 개구부 {len(ifaces)}
(<span style="color:#2e7d32">■문</span> <span style="color:#1565c0">●창</span>)</span>
<div class="k">{legend}</div></div>
<div id="stage"><svg id="svg" viewBox="{vb}" xmlns="http://www.w3.org/2000/svg">
{chr(10).join(svg)}
</svg></div>
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
window.addEventListener('mouseup',function(){{d=false}});}})();
</script></body></html>"""

    out = os.path.join(args.out_dir, f"{base}_bot.html")
    open(out, "w", encoding="utf-8").write(page)
    print(f"출력: {out}  (방 {len(rects)} · 세대 {n_units} · 인접 {len(adj)} · 개구부 {len(ifaces)})")


if __name__ == "__main__":
    main()
