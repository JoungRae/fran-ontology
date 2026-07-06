# -*- coding: utf-8 -*-
"""구조 도면 단독 뷰어 — 레이어별 색상·토글, 텍스트 표시.

분류 파일이 필요 없는 원시 레이어 뷰 (정합 검토용).
사용법: python struct_view.py "data/510_지하1층_구조.json"
"""
import argparse
import collections
import json
import math
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PALETTE = ["#c62828", "#1565c0", "#2e7d32", "#6a1b9a", "#ef6c00", "#00838f",
           "#ad1457", "#4527a0", "#00695c", "#f9a825", "#5d4037", "#37474f",
           "#d81b60", "#7cb342", "#0288d1", "#8d6e63"]
# 눈에 띄어야 하는 레이어는 고정 색
FIXED = {"COL-H": "#6a1b9a", "4_보": "#1565c0",
         "!BTS 배치 - I거더": "#00897b", "!BTS 배치 - T형 보 (3대 주차)": "#2e7d32",
         "S07-주동 표시": "#e53935", "A-ST": "#00838f", "A-ELEV": "#d81b60"}


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def ent_path(e, fy):
    t = e.get("Type")
    if t == "Line":
        a, b = e["Start"], e["End"]
        return f'M {a[0]:.0f} {fy(a[1]):.0f} L {b[0]:.0f} {fy(b[1]):.0f} '
    if t == "Polyline":
        v = e.get("Verts") or []
        if len(v) < 2:
            return None
        d = f'M {v[0][0]:.0f} {fy(v[0][1]):.0f} ' + "".join(
            f'L {p[0]:.0f} {fy(p[1]):.0f} ' for p in v[1:])
        if e.get("Closed"):
            d += "Z "
        return d
    if t == "Arc":
        cx, cy = e["Center"][:2]
        r = e.get("Radius", 0)
        a0, a1 = e.get("StartAngle", 0), e.get("EndAngle", 0)
        sx, sy = cx + r * math.cos(a0), cy + r * math.sin(a0)
        ex, ey = cx + r * math.cos(a1), cy + r * math.sin(a1)
        large = 1 if (a1 - a0) % (2 * math.pi) > math.pi else 0
        return (f'M {sx:.0f} {fy(sy):.0f} A {r:.0f} {r:.0f} 0 {large} 0 '
                f'{ex:.0f} {fy(ey):.0f} ')
    if t == "Circle":
        cx, cy = e["Center"][:2]
        r = e.get("Radius", 0)
        return (f'M {cx - r:.0f} {fy(cy):.0f} A {r:.0f} {r:.0f} 0 1 0 '
                f'{cx + r:.0f} {fy(cy):.0f} A {r:.0f} {r:.0f} 0 1 0 '
                f'{cx - r:.0f} {fy(cy):.0f} ')
    if t == "Ellipse":
        cx, cy = (e.get("Center") or [0, 0])[:2]
        return (f'M {cx - 200:.0f} {fy(cy):.0f} l 400 0 m -200 -200 l 0 400 ')
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    args = ap.parse_args()
    base = os.path.splitext(os.path.basename(args.input))[0]
    d = json.load(open(args.input, encoding="utf-8"))
    ents = d["Entities"]

    xs, ys = [], []
    for e in ents:
        t = e.get("Type")
        if t == "Line":
            xs += [e["Start"][0], e["End"][0]]
            ys += [e["Start"][1], e["End"][1]]
        elif t == "Polyline":
            for p in e.get("Verts", []):
                xs.append(p[0]); ys.append(p[1])
    max_y = max(ys)

    def fy(y):
        return max_y - y

    _DASH_KEY = ("HID", "HIDDEN", "HD", "DASH", "CEN", "CENTER", "PHANTOM",
                 "ACAD_ISO", "DOT")

    def dashed(e):
        s = str(e.get("Linetype") or "").strip().upper()
        return any(s.startswith(k) for k in _DASH_KEY)

    by_layer = collections.defaultdict(list)       # 실선
    by_layer_d = collections.defaultdict(list)     # 점선(HID 등) — CAD 표현 재현
    texts = collections.defaultdict(list)
    n_geom = collections.Counter()
    for e in ents:
        ly = e.get("Layer") or "(없음)"
        if e.get("Type") in ("DBText", "MText"):
            t = str(e.get("Text", "")).strip()
            p = e.get("Pos") or e.get("InsertionPoint") or [0, 0]
            h = e.get("Height", 300) or 300
            if t:
                texts[ly].append((t, p[0], p[1], h))
            continue
        p = ent_path(e, fy)
        if p:
            (by_layer_d if dashed(e) else by_layer)[ly].append(p)
            n_geom[ly] += 1

    order = sorted(set(by_layer) | set(by_layer_d), key=lambda l: -n_geom[l])
    colors = {}
    pi = 0
    for ly in order + list(texts):
        if ly in colors:
            continue
        if ly in FIXED:
            colors[ly] = FIXED[ly]
        else:
            colors[ly] = PALETTE[pi % len(PALETTE)]
            pi += 1

    def layer_bbox(ly):
        bx = []
        by = []
        for e in ents:
            if (e.get("Layer") or "(없음)") != ly:
                continue
            t = e.get("Type")
            if t == "Line":
                bx += [e["Start"][0], e["End"][0]]
                by += [e["Start"][1], e["End"][1]]
            elif t == "Polyline":
                for p in e.get("Verts", []):
                    bx.append(p[0])
                    by.append(p[1])
            elif t in ("DBText", "MText"):
                p = e.get("Pos") or e.get("InsertionPoint") or [0, 0]
                bx.append(p[0])
                by.append(p[1])
        if not bx:
            return None
        return min(bx), min(by), max(bx), max(by)

    groups, legend = [], []
    for gi, ly in enumerate(order):
        col = colors[ly]
        txt_svg = "".join(
            f'<text x="{x:.0f}" y="{fy(y):.0f}" font-size="{h * 1.2:.0f}">{esc(t)}</text>'
            for t, x, y, h in texts.get(ly, []))
        big = ly in ("4_보", "!BTS 배치 - I거더", "!BTS 배치 - T형 보 (3대 주차)",
                     "S07-주동 표시", "COL-H")
        w = 2.0 if big else 0.9
        fill = ('fill:#00897b;fill-opacity:.15;' if ly.startswith("!BTS") else 'fill:none;')
        dash_svg = (f'<path d="{"".join(by_layer_d[ly])}" '
                    f'style="vector-effect:non-scaling-stroke;stroke-dasharray:10 6"/>'
                    if by_layer_d.get(ly) else "")
        solid_svg = (f'<path d="{"".join(by_layer[ly])}" '
                     f'style="vector-effect:non-scaling-stroke"/>'
                     if by_layer.get(ly) else "")
        groups.append(
            f'<g id="ly{gi}" style="stroke:{col};stroke-width:{w};{fill}">'
            f'{solid_svg}{dash_svg}'
            f'<g style="stroke:none;fill:{col}">{txt_svg}</g></g>')
        n_t = len(texts.get(ly, []))
        bb = layer_bbox(ly)
        zb = (f' data-bb="{bb[0]:.0f},{fy(bb[3]):.0f},{bb[2] - bb[0]:.0f},'
              f'{bb[3] - bb[1]:.0f}"' if bb else "")
        legend.append(
            f'<label><input type="checkbox" data-g="ly{gi}" checked/>'
            f'<span class="zoomto"{zb} style="color:{col};font-weight:600" '
            f'title="클릭=해당 레이어로 이동"> {esc(ly)} '
            f'({n_geom[ly]}{"+" + str(n_t) + "t" if n_t else ""})</span></label>')
    # 지오메트리 없는 텍스트 전용 레이어 (보 번호 등)
    for ly in texts:
        if ly in by_layer:
            continue
        gi = len(groups)
        col = colors[ly]
        txt_svg = "".join(
            f'<text x="{x:.0f}" y="{fy(y):.0f}" font-size="{h * 1.2:.0f}">{esc(t)}</text>'
            for t, x, y, h in texts[ly])
        groups.append(f'<g id="ly{gi}" style="fill:{col}">{txt_svg}</g>')
        legend.append(
            f'<label><input type="checkbox" data-g="ly{gi}" checked/>'
            f'<span style="color:{col};font-weight:600"> {esc(ly)} ({len(texts[ly])}t)</span></label>')

    pad = 5000
    vb = f"{min(xs)-pad:.0f} {fy(max(ys))-pad:.0f} {max(xs)-min(xs)+2*pad:.0f} {max(ys)-min(ys)+2*pad:.0f}"
    title = f"{base} 구조도 뷰어"
    page = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"/>
<title>{esc(title)}</title>
<style>
html,body{{margin:0;height:100%;font-family:'Malgun Gothic',system-ui,sans-serif}}
#bar{{position:fixed;top:8px;left:8px;z-index:10;background:rgba(255,255,255,.95);
border:1px solid #ccc;border-radius:8px;padding:8px 12px;font-size:12.5px;
max-width:330px;max-height:92vh;overflow-y:auto}}
#bar label{{display:block;margin:1px 0;cursor:pointer;white-space:nowrap}}
#stage{{width:100vw;height:100vh;background:#fafafa;cursor:grab}}
svg{{width:100%;height:100%;display:block}}
svg text{{dominant-baseline:middle}}
</style></head><body>
<div id="bar"><b>{esc(title)}</b><br/>{''.join(legend)}
<div style="color:#777;margin-top:4px">휠 확대 · 드래그 이동</div></div>
<div id="stage"><svg id="svg" viewBox="{vb}" xmlns="http://www.w3.org/2000/svg">
{''.join(groups)}
</svg></div>
<script>
(function(){{
var svg=document.getElementById('svg'),stage=document.getElementById('stage');
var p=svg.getAttribute('viewBox').split(' ').map(Number);
var vb={{x:p[0],y:p[1],w:p[2],h:p[3]}};
function ap(){{svg.setAttribute('viewBox',vb.x+' '+vb.y+' '+vb.w+' '+vb.h);}}
stage.addEventListener('wheel',function(e){{e.preventDefault();
var r=svg.getBoundingClientRect();
var px=vb.x+(e.clientX-r.left)/r.width*vb.w, py=vb.y+(e.clientY-r.top)/r.height*vb.h;
var f=e.deltaY>0?1.15:1/1.15; vb.w*=f; vb.h*=f;
vb.x=px-(e.clientX-r.left)/r.width*vb.w; vb.y=py-(e.clientY-r.top)/r.height*vb.h; ap();
}},{{passive:false}});
var drag=false,lx,ly;
stage.addEventListener('mousedown',function(e){{drag=true;lx=e.clientX;ly=e.clientY;}});
window.addEventListener('mousemove',function(e){{if(!drag)return;var r=svg.getBoundingClientRect();
vb.x-=(e.clientX-lx)/r.width*vb.w; vb.y-=(e.clientY-ly)/r.height*vb.h; lx=e.clientX;ly=e.clientY;ap();}});
window.addEventListener('mouseup',function(){{drag=false;}});
document.querySelectorAll('#bar input[data-g]').forEach(function(c){{
c.onchange=function(){{var g=document.getElementById(c.getAttribute('data-g'));
if(g) g.style.display=c.checked?'':'none';}};}});
document.querySelectorAll('#bar .zoomto[data-bb]').forEach(function(sp){{
sp.style.cursor='zoom-in';
sp.onclick=function(){{var b=sp.getAttribute('data-bb').split(',').map(Number);
var pad=Math.max(b[2],b[3])*0.15+2000;
vb.x=b[0]-pad; vb.y=b[1]-pad; vb.w=b[2]+2*pad; vb.h=b[3]+2*pad; ap();}};}});
}})();
</script></body></html>"""

    op = os.path.join("output", f"{base}_view.html")
    os.makedirs("output", exist_ok=True)
    open(op, "w", encoding="utf-8").write(page)
    print("레이어:", ", ".join(f"{ly}({n_geom[ly]})" for ly in order[:12]))
    print(f"출력: {op}")


if __name__ == "__main__":
    main()
