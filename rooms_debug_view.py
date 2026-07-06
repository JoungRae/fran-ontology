# -*- coding: utf-8 -*-
"""
방 구획 디버그 뷰어 — 방 사각형/flood 폴리곤 위에 레이어 분류(구조벽·비구조벽·
문·창호·엘리베이터·계단·기둥)를 색상별로 겹쳐 그리는 HTML 생성.

입력: data/<도면>.json + output/<도면>_layer_classification.json
      + output/<도면>_rooms_rect.json [+ output/<도면>_rooms_flood.json]
출력: output/<도면>_rooms_debug.html (범례 체크박스로 항목별 켜고 끄기,
      선 위에 마우스를 올리면 레이어명 표시)

사용법: python rooms_debug_view.py "data/지하1층_b동.json"
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

CAT_STYLE = {  # (한글 라벨, 색, 굵기)
    "wall_struct":     ("구조벽체",     "#c62828", 2.2),
    "wall_nonstruct":  ("비구조벽체",   "#ef6c00", 1.8),
    "column_struct":   ("기둥(구조)",   "#6a1b9a", 2.0),
    "column_nonstruct": ("기둥(비구조)", "#ab47bc", 1.6),
    "window":          ("창호",         "#0288d1", 1.8),
    "door":            ("문",           "#2e7d32", 1.8),
    "elevator":        ("엘리베이터",   "#d81b60", 2.0),
    "stair":           ("계단",         "#00897b", 1.6),
    "mixed":           ("혼합",         "#8d6e63", 1.2),
    "other":           ("기타",         "#d5d5d5", 0.8),
}
DEFAULT_OFF = {"other", "mixed"}   # 기본 꺼짐


def ent_path(e, fy):
    """엔티티 1개 -> SVG path d 문자열 (없으면 None)."""
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
        return (f'M {sx:.0f} {fy(sy):.0f} '
                f'A {r:.0f} {r:.0f} 0 {large} 0 {ex:.0f} {fy(ey):.0f} ')
    if t == "Circle":
        cx, cy = e["Center"][:2]
        r = e.get("Radius", 0)
        return (f'M {cx - r:.0f} {fy(cy):.0f} '
                f'A {r:.0f} {r:.0f} 0 1 0 {cx + r:.0f} {fy(cy):.0f} '
                f'A {r:.0f} {r:.0f} 0 1 0 {cx - r:.0f} {fy(cy):.0f} ')
    return None


def ent_bbox(e):
    t = e.get("Type")
    if t == "Line":
        xs = (e["Start"][0], e["End"][0]); ys = (e["Start"][1], e["End"][1])
    elif t == "Polyline":
        v = e.get("Verts") or []
        if not v:
            return None
        xs = [p[0] for p in v]; ys = [p[1] for p in v]
    elif t in ("Arc", "Circle"):
        cx, cy = e["Center"][:2]; r = e.get("Radius", 0)
        xs = (cx - r, cx + r); ys = (cy - r, cy + r)
    else:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    args = ap.parse_args()
    base = os.path.splitext(os.path.basename(args.input))[0]

    d = json.load(open(args.input, encoding="utf-8"))
    ents = d["Entities"]
    cats = json.load(open(os.path.join("output", f"{base}_layer_classification.json"),
                          encoding="utf-8"))["categories"]
    rect = json.load(open(os.path.join("output", f"{base}_rooms_rect.json"),
                          encoding="utf-8"))["rooms"]
    flood_path = os.path.join("output", f"{base}_rooms_flood.json")
    flood = (json.load(open(flood_path, encoding="utf-8"))["rooms"]
             if os.path.exists(flood_path) else [])

    noplot = {L["Name"] for L in d.get("Layers", []) if not L.get("Plot", True)}

    # ---- Y 뒤집기 기준: 분류된(기타 제외) 지오메트리 + 방 bbox ----
    xs, ys = [], []
    for e in ents:
        if cats.get(e.get("Layer"), "other") in ("other", "mixed"):
            continue
        bb = ent_bbox(e)
        if bb:
            xs += [bb[0], bb[2]]; ys += [bb[1], bb[3]]
    for r in rect:
        xs += [r["rect"][0], r["rect"][2]]; ys += [r["rect"][1], r["rect"][3]]
    if not xs:
        for e in ents:
            bb = ent_bbox(e)
            if bb:
                xs += [bb[0], bb[2]]; ys += [bb[1], bb[3]]
    max_y = max(ys)

    def fy(y):
        return max_y - y

    # ---- 카테고리별 path (레이어 단위로 묶어 <title>=레이어명) ----
    by_cat_layer = collections.defaultdict(lambda: collections.defaultdict(list))
    np_paths = collections.defaultdict(list)   # Plot=false 레이어
    n_cat = collections.Counter()
    for e in ents:
        ly = e.get("Layer")
        p = ent_path(e, fy)
        if not p:
            continue
        if ly in noplot:
            np_paths[ly].append(p)
            continue
        cat = cats.get(ly, "other")
        by_cat_layer[cat][ly].append(p)
        n_cat[cat] += 1

    cat_groups = []
    for cat, (label, color, w) in CAT_STYLE.items():
        layers = by_cat_layer.get(cat)
        if not layers:
            continue
        paths = "".join(
            f'<path d="{"".join(ps)}"><title>{esc(ly)} ({label})</title></path>'
            for ly, ps in sorted(layers.items()))
        disp = ' style="display:none"' if cat in DEFAULT_OFF else ""
        cat_groups.append(
            f'<g id="cat-{cat}" stroke="{color}" stroke-width="{w}" fill="none"{disp}>{paths}</g>')

    np_svg = "".join(
        f'<path d="{"".join(ps)}"><title>{esc(ly)} (Plot=false)</title></path>'
        for ly, ps in sorted(np_paths.items()))

    # ---- 방 사각형 + 라벨 + 씨앗 ----
    rect_svg, label_svg, seed_svg = [], [], []
    for i, r in enumerate(rect):
        x0, y0, x1, y1 = r["rect"]
        rect_svg.append(
            f'<rect x="{x0}" y="{fy(y1):.0f}" width="{x1 - x0}" height="{y1 - y0}">'
            f'<title>{esc(r["room"])} {r["w_mm"]}x{r["h_mm"]}mm</title></rect>')
        cxm, cym = (x0 + x1) / 2, (y0 + y1) / 2
        label_svg.append(
            f'<text x="{cxm:.0f}" y="{fy(cym):.0f}">{esc(r["room"])}</text>')
        sx, sy = r["seed"]
        seed_svg.append(f'<circle cx="{sx}" cy="{fy(sy):.0f}" r="90"/>')

    # ---- flood 폴리곤 ----
    flood_svg = []
    for r in flood:
        poly = r.get("poly")
        if not poly:
            continue
        pts = " ".join(f'{p[0]:.0f},{fy(p[1]):.0f}' for p in poly)
        also = ("+" + "/".join(r["also"])) if r.get("also") else ""
        flood_svg.append(
            f'<polygon points="{pts}"><title>{esc(r["room"])}{esc(also)} '
            f'{r.get("area_m2", "?")}m2</title></polygon>')

    # ---- 뷰박스: 방 영역 중심으로 초기 화면 ----
    pad = 3000
    vx0, vy0 = min(xs) - pad, fy(max(ys)) - pad
    vw, vh = (max(xs) - min(xs)) + 2 * pad, (max(ys) - min(ys)) + 2 * pad
    vb = f"{vx0:.0f} {vy0:.0f} {vw:.0f} {vh:.0f}"

    # ---- 범례 ----
    def cb(gid, color, text, on=True):
        chk = "checked" if on else ""
        return (f'<label><input type="checkbox" data-g="{gid}" {chk}/>'
                f'<span style="color:{color};font-weight:600"> {text}</span></label>')

    legend = []
    for cat, (label, color, w) in CAT_STYLE.items():
        if cat in by_cat_layer:
            nly = len(by_cat_layer[cat])
            legend.append(cb(f"cat-{cat}", color,
                             f"{label} ({n_cat[cat]}·{nly}레이어)", cat not in DEFAULT_OFF))
    legend.append(cb("g-noplot", "#9e9e9e", f"비출력 Plot=false ({sum(len(v) for v in np_paths.values())})", False))
    legend.append(cb("g-rect", "#1565c0", f"방 사각형 ({len(rect)})"))
    legend.append(cb("g-label", "#1565c0", "방 이름"))
    legend.append(cb("g-seed", "#e91e63", f"씨앗 ({len(rect)})"))
    if flood_svg:
        legend.append(cb("g-flood", "#7cb342", f"flood 폴리곤 ({len(flood_svg)})"))

    title = f"{base} 방 구획 디버그"
    page = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"/>
<title>{esc(title)}</title>
<style>
  html,body{{margin:0;height:100%;font-family:'Malgun Gothic',system-ui,sans-serif;}}
  #bar{{position:fixed;top:8px;left:8px;z-index:10;background:rgba(255,255,255,.95);
    border:1px solid #ccc;border-radius:8px;padding:8px 12px;font-size:13px;max-width:340px;}}
  #bar label{{margin-right:10px;white-space:nowrap;display:inline-block;margin-bottom:3px;cursor:pointer;}}
  #stage{{width:100vw;height:100vh;background:#fafafa;cursor:grab;}}
  svg{{width:100%;height:100%;display:block;}}
  svg path{{vector-effect:non-scaling-stroke;}}
  #g-noplot path{{stroke:#9e9e9e;stroke-width:1;stroke-dasharray:6 4;fill:none;vector-effect:non-scaling-stroke;}}
  #g-rect rect{{stroke:#1565c0;stroke-width:1.6;fill:#1565c0;fill-opacity:.05;vector-effect:non-scaling-stroke;}}
  #g-label text{{fill:#0d47a1;font-size:700px;text-anchor:middle;dominant-baseline:middle;
    paint-order:stroke;stroke:#ffffff;stroke-width:160px;}}
  #g-seed circle{{fill:#e91e63;}}
  #g-flood polygon{{stroke:#558b2f;stroke-width:1.2;fill:#8bc34a;fill-opacity:.18;vector-effect:non-scaling-stroke;}}
  #hint{{color:#777;margin-top:4px;}}
</style></head><body>
<div id="bar">
  <b>{esc(title)}</b><br/>
  {''.join(legend)}
  <div id="hint">선 위에 마우스 → 레이어명 · 휠 확대 · 드래그 이동</div>
</div>
<div id="stage"><svg id="svg" viewBox="{vb}" xmlns="http://www.w3.org/2000/svg">
  <g id="g-noplot" style="display:none">{np_svg}</g>
  {''.join(cat_groups)}
  <g id="g-flood">{''.join(flood_svg)}</g>
  <g id="g-rect">{''.join(rect_svg)}</g>
  <g id="g-seed">{''.join(seed_svg)}</g>
  <g id="g-label">{''.join(label_svg)}</g>
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
    c.onchange=function(){{
      var g=document.getElementById(c.getAttribute('data-g'));
      if(g) g.style.display=c.checked?'':'none';
    }};
  }});
}})();
</script></body></html>"""

    os.makedirs("output", exist_ok=True)
    out = os.path.join("output", f"{base}_rooms_debug.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"카테고리: " + ", ".join(f"{CAT_STYLE[c][0]} {n_cat[c]}" for c in n_cat))
    print(f"방 {len(rect)}개 · flood {len(flood_svg)}개 · Plot=false 레이어 {len(np_paths)}개")
    print(f"출력: {out}")


if __name__ == "__main__":
    main()
