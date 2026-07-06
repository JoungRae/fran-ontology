"""
building.ttl 의 층 스택을 건물 단면(표고 스케일)으로 그리는 자체 완결형 HTML.

  python stack_viz.py   # output/building.ttl → output/building_stack.html
"""

import argparse
import collections
import html
import os
import sys

from rdflib import Graph, Namespace

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BOT = Namespace("https://w3id.org/bot#")
FRAN = Namespace("https://example.org/fran#")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ttl", default=os.path.join("output", "building.ttl"))
    ap.add_argument("--out", default=os.path.join("output", "building_stack.html"))
    args = ap.parse_args()
    g = Graph()
    g.parse(args.ttl, format="turtle")
    ns = {"bot": BOT, "fran": FRAN,
          "rdfs": "http://www.w3.org/2000/01/rdf-schema#"}

    storeys = []
    for s, lb, lvl, el, h in g.query("""SELECT ?s ?lb ?lvl ?el ?h WHERE {
            ?s a bot:Storey ; rdfs:label ?lb ; fran:levelIndex ?lvl ;
               fran:elevationMm ?el ; fran:heightMm ?h }""", initNs=ns):
        base = str(s).rsplit("Storey_", 1)[1]
        storeys.append({"base": base, "label": str(lb), "lvl": int(lvl),
                        "el": int(el), "h": int(h)})

    # base 접두사로 개체 수 집계
    cnt = collections.defaultdict(lambda: collections.Counter())
    for s in set(g.subjects()):
        u = str(s)
        if "/inst#" not in u:
            continue
        loc = u.rsplit("#", 1)[1]
        for st in storeys:
            p = st["base"] + "_"
            if loc.startswith(p):
                kind = loc[len(p):].split("_")[0]
                cnt[st["base"]][kind] += 1
                break

    storeys.sort(key=lambda s: s["el"])
    minel = min(s["el"] for s in storeys)
    maxtop = max(s["el"] + s["h"] for s in storeys)
    total = maxtop - minel
    # 스케일: 전체 높이를 900px 에 맞춤
    SC = 900.0 / total
    W = 520
    X = 120

    def fy(el):
        return (maxtop - el) * SC   # 화면 y (위가 높은 표고)

    colors = ["#8e24aa", "#1e88e5", "#43a047", "#fb8c00", "#00acc1"]
    svg = []
    for k, st in enumerate(storeys):
        y = fy(st["el"] + st["h"])
        hpx = st["h"] * SC
        c = colors[k % len(colors)]
        svg.append(f'<rect x="{X}" y="{y:.1f}" width="{W}" height="{hpx:.1f}" '
                   f'fill="{c}" fill-opacity="0.14" stroke="{c}" stroke-width="2"/>')
        cc = cnt[st["base"]]
        info = (f"방 {cc.get('Room',0)} · 세대 {cc.get('Unit',0)} · "
                f"벽 {cc.get('Wall',0)} · 개구부 {cc.get('Interface',0)}")
        svg.append(f'<text x="{X+12}" y="{y+hpx/2-4:.1f}" class="ttl">'
                   f'{html.escape(st["label"])}</text>')
        svg.append(f'<text x="{X+12}" y="{y+hpx/2+16:.1f}" class="sub">{info}</text>')
        # 표고 눈금
        yb = fy(st["el"])
        svg.append(f'<line x1="{X-8}" y1="{yb:.1f}" x2="{X+W}" y2="{yb:.1f}" '
                   f'class="lvl"/>')
        svg.append(f'<text x="{X-14}" y="{yb+4:.1f}" class="elev">'
                   f'FL{st["el"]:+d}</text>')
        svg.append(f'<text x="{X+W+10}" y="{y+hpx/2+4:.1f}" class="hmm">'
                   f'{st["h"]}mm</text>')
    # 최상단 표고
    svg.append(f'<line x1="{X-8}" y1="{fy(maxtop):.1f}" x2="{X+W}" y2="{fy(maxtop):.1f}" class="lvl"/>')
    svg.append(f'<text x="{X-14}" y="{fy(maxtop)+4:.1f}" class="elev">FL{maxtop-0:+d}</text>')

    page = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"/>
<title>BOT 건물 스택</title><style>
html,body{{margin:0;font-family:system-ui,sans-serif;background:#fff}}
#h{{padding:12px 16px;border-bottom:1px solid #eee}}
#h b{{font-size:16px}} #h span{{color:#666;font-size:13px;margin-left:8px}}
svg{{display:block;margin:10px auto}}
.ttl{{font-size:17px;font-weight:700;fill:#111}}
.sub{{font-size:12px;fill:#444}}
.elev{{font-size:11px;fill:#c62828;text-anchor:end}}
.hmm{{font-size:11px;fill:#555}}
.lvl{{stroke:#e0a0a0;stroke-width:1;stroke-dasharray:4 3}}
</style></head><body>
<div id="h"><b>주동 A (5BL) · BOT 수직 스택</b>
<span>층 {len(storeys)}개 · 표고 스케일 단면 · 1층 바닥 FL±0 기준</span></div>
<svg width="820" height="960" viewBox="0 0 820 960" xmlns="http://www.w3.org/2000/svg">
{chr(10).join(svg)}
</svg></body></html>"""
    open(args.out, "w", encoding="utf-8").write(page)
    print(f"출력: {args.out}  (층 {len(storeys)})")


if __name__ == "__main__":
    main()
