"""
CAD JSON -> 인터랙티브 HTML 뷰어 생성기.

data/ 폴더의 JSON(캐드 도면 좌표 추출본)을 읽어,
브라우저에서 줌·팬이 되는 자체 완결형 HTML(SVG) 뷰어를 생성한다.

사용법:
    python render.py [입력.json] [-o 출력.html]

외부 의존성 없음 (표준 라이브러리만 사용).
"""

import argparse
import glob
import html
import json
import math
import os
import sys


# ---------------------------------------------------------------------------
# 좌표 정규화
# ---------------------------------------------------------------------------
def normalize_coord(v):
    """좌표 정규화.

    CAD 익스포트 과정의 부동소수점 노이즈(예: 269553.9999999999)를 정리한다.
    지금은 단순 반올림(3자리)이지만, 향후 정합성 비교 시에는
    '가장 가까운 mm로 snap' 또는 'epsilon 양자화'로 교체해 재사용할 지점이다.
    """
    return round(v, 3)


def fmt(v):
    """SVG에 박을 숫자 문자열. 정수면 소수점 제거."""
    v = normalize_coord(v)
    if v == int(v):
        return str(int(v))
    return repr(v)


# ---------------------------------------------------------------------------
# Y축 반전: CAD는 Y가 위로 증가, SVG는 아래로 증가.
# 화면좌표 sy = (max_y - cad_y) 로 변환해 텍스트가 바로 서도록 한다.
# ---------------------------------------------------------------------------
class Transform:
    def __init__(self, max_y):
        self.max_y = max_y

    def y(self, cad_y):
        return self.max_y - cad_y


# ---------------------------------------------------------------------------
# 엔티티 로딩: 원본/compact(list) 와 평탄화(flat-v1, dict) 두 포맷 모두 지원
# ---------------------------------------------------------------------------
def load_entities(data):
    ents = data.get("Entities")
    if isinstance(ents, list):
        return ents  # 원본 / compact 포맷 (이미 dict 리스트)

    # flat-v1: {Type: [rows...]} + Schema + Layers(인덱스 해석용)
    schema = data.get("Schema", {})
    names = [ly.get("Name") for ly in data.get("Layers", [])]

    def lname(i):
        return names[i] if isinstance(i, int) and 0 <= i < len(names) else ""

    out = []
    for t, rows in ents.items():
        cols = schema.get(t, [])
        for row in rows:
            d = dict(zip(cols, row))
            e = {"Type": t, "Layer": lname(d.get("L", -1)), "Color": d.get("Color")}
            if t == "Line":
                e["Start"] = [d["x1"], d["y1"]]
                e["End"] = [d["x2"], d["y2"]]
            elif t == "Polyline":
                v = d["Verts"]
                e["Verts"] = [[v[i], v[i + 1]] for i in range(0, len(v), 2)]
                e["Closed"] = bool(d["Closed"])
            elif t == "Circle":
                e["Center"] = [d["cx"], d["cy"]]
                e["Radius"] = d["r"]
            elif t == "Arc":
                e["Center"] = [d["cx"], d["cy"]]
                e["Radius"] = d["r"]
                e["StartAngle"] = d["a0"]
                e["EndAngle"] = d["a1"]
            elif t == "DBText":
                e["Pos"] = [d["x"], d["y"]]
                e["Height"] = d["Height"]
                e["Rotation"] = d["Rotation"]
                e["Text"] = d["Text"]
            elif t == "BlockReference":
                e["Pos"] = [d["x"], d["y"]]
                e["Rotation"] = d["Rotation"]
                e["Scale"] = [d["sx"], d["sy"], d["sz"]]
                e["BlockName"] = d["BlockName"]
            elif t in ("RotatedDimension", "LineAngularDimension2"):
                e["Pos"] = [d["x"], d["y"]]
                e["Measurement"] = d["Measurement"]
                e["DimText"] = d["DimText"]
            out.append(e)
    return out


# ---------------------------------------------------------------------------
# 경계(bounding box) 계산
# ---------------------------------------------------------------------------
def compute_bounds(entities):
    min_x = min_y = math.inf
    max_x = max_y = -math.inf

    def acc(x, y):
        nonlocal min_x, min_y, max_x, max_y
        if x < min_x:
            min_x = x
        if y < min_y:
            min_y = y
        if x > max_x:
            max_x = x
        if y > max_y:
            max_y = y

    for e in entities:
        t = e.get("Type")
        if t == "Line":
            acc(*e["Start"][:2])
            acc(*e["End"][:2])
        elif t == "Polyline":
            for v in e["Verts"]:
                acc(v[0], v[1])
        elif t in ("Circle", "Arc"):
            cx, cy = e["Center"][:2]
            r = e["Radius"]
            acc(cx - r, cy - r)
            acc(cx + r, cy + r)
        elif t in ("DBText", "BlockReference", "RotatedDimension",
                   "LineAngularDimension2"):
            pos = e.get("Pos")
            if pos:
                acc(pos[0], pos[1])

    if min_x is math.inf:
        # 그릴 게 하나도 없을 때의 안전장치
        return 0.0, 0.0, 100.0, 100.0
    return min_x, min_y, max_x, max_y


# ---------------------------------------------------------------------------
# SVG 요소 생성
# ---------------------------------------------------------------------------
def esc_attr(s):
    return html.escape(str(s), quote=True)


def marker_cross(x, sy, size, extra=""):
    """삽입점/위치점을 나타내는 작은 십자(+) 마커. 선은 non-scaling-stroke."""
    h = size / 2
    return (
        f'<line x1="{fmt(x - h)}" y1="{fmt(sy)}" x2="{fmt(x + h)}" y2="{fmt(sy)}"{extra}/>'
        f'<line x1="{fmt(x)}" y1="{fmt(sy - h)}" x2="{fmt(x)}" y2="{fmt(sy + h)}"{extra}/>'
    )


def build_svg_elements(entities, tf):
    """엔티티를 그룹별로 분리해 반환.

    반환: {"geom": [...], "texts": [...], "blocks": [...], "dims": [...]}
      - geom   : Line/Polyline/Circle (검정 실선)
      - texts  : DBText (검정)
      - blocks : BlockReference 삽입점 마커 + 블록명 (파랑)
      - dims   : 치수 위치점 마커 + 치수값 (빨강)
    """
    geom = []
    texts = []
    blocks = []
    dims = []

    # 마커/라벨 크기 (도면 단위 mm). 도면 전체가 ~100만 단위라 수백 단위면 적당.
    mk = 400
    label_size = 300

    for e in entities:
        t = e.get("Type")
        layer = esc_attr(e.get("Layer", ""))

        if t == "Line":
            x1, y1 = e["Start"][:2]
            x2, y2 = e["End"][:2]
            geom.append(
                f'<line x1="{fmt(x1)}" y1="{fmt(tf.y(y1))}" '
                f'x2="{fmt(x2)}" y2="{fmt(tf.y(y2))}" data-layer="{layer}"/>'
            )

        elif t == "Polyline":
            pts = " ".join(
                f"{fmt(v[0])},{fmt(tf.y(v[1]))}" for v in e["Verts"]
            )
            tag = "polygon" if e.get("Closed") else "polyline"
            geom.append(f'<{tag} points="{pts}" data-layer="{layer}"/>')

        elif t == "Circle":
            cx, cy = e["Center"][:2]
            geom.append(
                f'<circle cx="{fmt(cx)}" cy="{fmt(tf.y(cy))}" '
                f'r="{fmt(e["Radius"])}" data-layer="{layer}"/>'
            )

        elif t == "Arc":
            cx, cy = e["Center"][:2]
            r = e["Radius"]
            a0 = e.get("StartAngle", 0.0)
            a1 = e.get("EndAngle", 0.0)
            sx = cx + r * math.cos(a0)
            sy = tf.y(cy + r * math.sin(a0))
            ex = cx + r * math.cos(a1)
            ey = tf.y(cy + r * math.sin(a1))
            # CAD 호는 CCW(a0->a1). span>π 이면 large-arc.
            span = (a1 - a0) % (2 * math.pi)
            large = 1 if span > math.pi else 0
            # Y 반전 좌표계에서 CAD CCW 는 sweep-flag 0
            geom.append(
                f'<path d="M {fmt(sx)} {fmt(sy)} '
                f'A {fmt(r)} {fmt(r)} 0 {large} 0 {fmt(ex)} {fmt(ey)}" '
                f'data-layer="{layer}"/>'
            )

        elif t == "DBText":
            x, y = e["Pos"][:2]
            sy = tf.y(y)
            size = normalize_coord(e.get("Height", 100)) or 100
            # CAD 회전(라디안, CCW). Y 반전 화면에서는 부호를 뒤집어야 같은 방향.
            deg = math.degrees(e.get("Rotation", 0.0))
            transform = ""
            if abs(deg) > 1e-9:
                transform = f' transform="rotate({fmt(-deg)} {fmt(x)} {fmt(sy)})"'
            txt = html.escape(str(e.get("Text", "")))
            texts.append(
                f'<text x="{fmt(x)}" y="{fmt(sy)}" font-size="{fmt(size)}"'
                f'{transform} data-layer="{layer}">{txt}</text>'
            )

        elif t == "BlockReference":
            pos = e.get("Pos")
            if not pos:
                continue
            x, sy = pos[0], tf.y(pos[1])
            name = html.escape(str(e.get("BlockName", "")))
            blocks.append(
                f'<g data-layer="{layer}">'
                f'<title>{name}</title>'
                f'{marker_cross(x, sy, mk)}'
                f'<text x="{fmt(x + mk)}" y="{fmt(sy)}" font-size="{fmt(label_size)}">{name}</text>'
                f"</g>"
            )

        elif t in ("RotatedDimension", "LineAngularDimension2"):
            pos = e.get("Pos")
            if not pos:
                continue
            x, sy = pos[0], tf.y(pos[1])
            dim_text = e.get("DimText") or ""
            if not dim_text:
                m = e.get("Measurement")
                if m is not None:
                    # 각도 치수(라디안)와 길이 치수 구분 없이 측정값을 표시
                    dim_text = f"{normalize_coord(m)}"
            dim_text = html.escape(str(dim_text))
            dims.append(
                f'<g data-layer="{layer}">'
                f'{marker_cross(x, sy, mk)}'
                f'<text x="{fmt(x + mk)}" y="{fmt(sy)}" font-size="{fmt(label_size)}">{dim_text}</text>'
                f"</g>"
            )

    return {"geom": geom, "texts": texts, "blocks": blocks, "dims": dims}


def build_highlight(entities, tf, box, min_len=0):
    """기준 벽체 영역(box: xlo,ylo,xhi,yhi, CAD좌표) 안의 벽을 색칠 오버레이로 생성.

    Reference 블록엔 벽 형상 좌표가 없으므로, 그 유닛 영역 안의 형상을 칠한다.
    벽 ≈ 긴 직선이라, min_len>0 이면 그 길이 이상의 Line 만 칠해(가구·치수·미세조각 제외)
    벽에 가깝게 만든다. min_len=0 이면 영역 내 모든 형상을 칠한다.
    """
    if not box:
        return []
    xlo, ylo, xhi, yhi = box

    def inside(x, y):
        return xlo <= x <= xhi and ylo <= y <= yhi

    walls_only = min_len > 0
    out = []
    for e in entities:
        t = e.get("Type")
        if t == "Line":
            x1, y1 = e["Start"][:2]
            x2, y2 = e["End"][:2]
            if inside(x1, y1) and inside(x2, y2):
                if walls_only and math.hypot(x2 - x1, y2 - y1) < min_len:
                    continue
                out.append(
                    f'<line x1="{fmt(x1)}" y1="{fmt(tf.y(y1))}" '
                    f'x2="{fmt(x2)}" y2="{fmt(tf.y(y2))}"/>'
                )
        elif t == "Polyline" and not walls_only:
            vs = e["Verts"]
            if all(inside(v[0], v[1]) for v in vs):
                pts = " ".join(f"{fmt(v[0])},{fmt(tf.y(v[1]))}" for v in vs)
                tag = "polygon" if e.get("Closed") else "polyline"
                out.append(f'<{tag} points="{pts}"/>')
        elif t in ("Circle", "Arc") and not walls_only:
            cx, cy = e["Center"][:2]
            if inside(cx, cy):
                r = e["Radius"]
                if t == "Circle":
                    out.append(f'<circle cx="{fmt(cx)}" cy="{fmt(tf.y(cy))}" r="{fmt(r)}"/>')
                else:
                    a0 = e.get("StartAngle", 0.0)
                    a1 = e.get("EndAngle", 0.0)
                    sx = cx + r * math.cos(a0)
                    sy = tf.y(cy + r * math.sin(a0))
                    ex = cx + r * math.cos(a1)
                    ey = tf.y(cy + r * math.sin(a1))
                    large = 1 if (a1 - a0) % (2 * math.pi) > math.pi else 0
                    out.append(
                        f'<path d="M {fmt(sx)} {fmt(sy)} '
                        f'A {fmt(r)} {fmt(r)} 0 {large} 0 {fmt(ex)} {fmt(ey)}"/>'
                    )
    return out


# ---------------------------------------------------------------------------
# HTML 생성
# ---------------------------------------------------------------------------
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8"/>
<title>{title}</title>
<style>
  html, body {{ margin: 0; height: 100%; font-family: system-ui, sans-serif; }}
  #toolbar {{
    position: fixed; top: 10px; left: 10px; z-index: 10;
    background: rgba(255,255,255,0.92); border: 1px solid #ccc; border-radius: 6px;
    padding: 8px 10px; font-size: 13px; box-shadow: 0 1px 4px rgba(0,0,0,0.15);
    user-select: none;
  }}
  #toolbar button {{ font-size: 13px; padding: 2px 8px; cursor: pointer; }}
  #toolbar .info {{ color: #555; margin-top: 6px; line-height: 1.4; }}
  #stage {{ width: 100vw; height: 100vh; background: #fff; cursor: grab; }}
  #stage.panning {{ cursor: grabbing; }}
  svg {{ display: block; width: 100%; height: 100%; }}
  #geom line, #geom polyline, #geom polygon, #geom circle, #geom path {{
    stroke: #111; fill: none; stroke-width: 1; vector-effect: non-scaling-stroke;
  }}
  #text-layer text {{ fill: #111; }}
  /* 오버레이: 블록=파랑, 치수=빨강 */
  #blocks line {{ stroke: #1565c0; stroke-width: 1; vector-effect: non-scaling-stroke; }}
  #blocks text {{ fill: #1565c0; }}
  #dims line {{ stroke: #c62828; stroke-width: 1; vector-effect: non-scaling-stroke; }}
  #dims text {{ fill: #c62828; }}
  /* 기준 벽체 영역 색칠 오버레이 */
  #highlight line, #highlight polyline, #highlight polygon, #highlight circle, #highlight path {{
    stroke: #ff6d00; stroke-width: 2.5; fill: rgba(255,109,0,0.10); vector-effect: non-scaling-stroke;
  }}
  #refbox {{ stroke: #ff6d00; stroke-width: 1.5; stroke-dasharray: 8 6; fill: none; vector-effect: non-scaling-stroke; }}
  /* 유저가 뽑은 기준 벽체(Reference) 강조 */
  #reference line {{ stroke: #e91e8c; stroke-width: 3; vector-effect: non-scaling-stroke; }}
  #reference circle {{ stroke: #e91e8c; stroke-width: 2; fill: rgba(233,30,140,0.12); vector-effect: non-scaling-stroke; }}
  #reference text {{ fill: #e91e8c; font-weight: bold; }}
</style>
</head>
<body>
<div id="toolbar">
  <button id="zin">＋</button>
  <button id="zout">－</button>
  <button id="reset">전체보기</button>
  <span style="margin-left:8px;">
    <label><input type="checkbox" id="toggleText" checked/> 텍스트</label>
    <label style="color:#c62828;"><input type="checkbox" id="toggleDims" checked/> 치수</label>
    <label style="color:#c79a00;"><input type="checkbox" id="toggleWalls" checked/> 벽체</label>
    <label style="color:#1565ff;"><input type="checkbox" id="toggleWindows" checked/> 창호</label>
  </span>
  <div class="info">{filename}<br/>{counts}</div>
</div>
<div id="stage">
<svg id="svg" xmlns="http://www.w3.org/2000/svg"
     viewBox="{vb_x} {vb_y} {vb_w} {vb_h}"
     preserveAspectRatio="xMidYMid meet">
  <g id="geom">
{geom}
  </g>
  <g id="blocks">
{blocks}
  </g>
  <g id="dims">
{dims}
  </g>
  <g id="text-layer">
{texts}
  </g>
  <g id="highlight">
{highlight}
  </g>
  <g id="reference">
{reference}
  </g>
</svg>
</div>
<script>
(function() {{
  const svg = document.getElementById('svg');
  const stage = document.getElementById('stage');

  // 전체보기 기준: 실제 형상(geom) 영역에 맞춤.
  // 블록 삽입점 일부가 도면 외곽 멀리 있어 전체 extent로 잡으면 도면이
  // 작게 보이므로, 선/폴리라인/원 영역을 기준으로 여백 5%를 둔다.
  function homeBox() {{
    let b;
    try {{ b = document.getElementById('geom').getBBox(); }} catch (e) {{ b = null; }}
    if (!b || b.width === 0 || b.height === 0) {{
      return {{ x: {vb_x}, y: {vb_y}, w: {vb_w}, h: {vb_h} }};
    }}
    const pad = Math.max(b.width, b.height) * 0.05;
    return {{ x: b.x - pad, y: b.y - pad, w: b.width + pad * 2, h: b.height + pad * 2 }};
  }}
  const HOME = homeBox();
  let vb = Object.assign({{}}, HOME);
  svg.setAttribute('viewBox', vb.x + ' ' + vb.y + ' ' + vb.w + ' ' + vb.h);

  function apply() {{
    svg.setAttribute('viewBox', vb.x + ' ' + vb.y + ' ' + vb.w + ' ' + vb.h);
  }}

  // 화면(px) -> SVG 좌표
  function toSvg(px, py) {{
    const r = svg.getBoundingClientRect();
    return {{
      x: vb.x + (px - r.left) / r.width * vb.w,
      y: vb.y + (py - r.top) / r.height * vb.h
    }};
  }}

  function zoomAt(px, py, factor) {{
    const p = toSvg(px, py);
    vb.w *= factor; vb.h *= factor;
    // 커서 아래 지점이 고정되도록 원점 이동
    vb.x = p.x - (px - svg.getBoundingClientRect().left) / svg.getBoundingClientRect().width * vb.w;
    vb.y = p.y - (py - svg.getBoundingClientRect().top) / svg.getBoundingClientRect().height * vb.h;
    apply();
  }}

  // 휠 줌
  stage.addEventListener('wheel', function(ev) {{
    ev.preventDefault();
    const factor = ev.deltaY > 0 ? 1.15 : 1 / 1.15;
    zoomAt(ev.clientX, ev.clientY, factor);
  }}, {{ passive: false }});

  // 드래그 팬
  let dragging = false, lastX = 0, lastY = 0;
  stage.addEventListener('mousedown', function(ev) {{
    dragging = true; lastX = ev.clientX; lastY = ev.clientY;
    stage.classList.add('panning');
  }});
  window.addEventListener('mousemove', function(ev) {{
    if (!dragging) return;
    const r = svg.getBoundingClientRect();
    vb.x -= (ev.clientX - lastX) / r.width * vb.w;
    vb.y -= (ev.clientY - lastY) / r.height * vb.h;
    lastX = ev.clientX; lastY = ev.clientY;
    apply();
  }});
  window.addEventListener('mouseup', function() {{
    dragging = false; stage.classList.remove('panning');
  }});

  // 버튼
  document.getElementById('zin').onclick = function() {{
    zoomAt(window.innerWidth/2, window.innerHeight/2, 1/1.3);
  }};
  document.getElementById('zout').onclick = function() {{
    zoomAt(window.innerWidth/2, window.innerHeight/2, 1.3);
  }};
  document.getElementById('reset').onclick = function() {{
    vb = Object.assign({{}}, HOME); apply();
  }};
  function bindToggle(checkboxId, groupId) {{
    document.getElementById(checkboxId).onchange = function(ev) {{
      document.getElementById(groupId).style.display = ev.target.checked ? '' : 'none';
    }};
  }}
  bindToggle('toggleText', 'text-layer');
  bindToggle('toggleDims', 'dims');
  // 벽체/창호: 레이어 분류(layer_classification) 결과로 정확히 인식. 분류 없으면 이름패턴 폴백.
  var WALL_LAYERS = {wall_layers};
  var WIN_LAYERS = {win_layers};
  function bindLayerSet(checkboxId, layers, hints) {{
    document.getElementById(checkboxId).onchange = function(ev) {{
      var show = ev.target.checked;
      document.querySelectorAll('#geom [data-layer]').forEach(function(el) {{
        var ly = el.getAttribute('data-layer') || '';
        var match = layers ? layers.indexOf(ly) >= 0
                           : hints.some(function(h) {{ return ly.toUpperCase().indexOf(h) >= 0; }});
        if (match) el.style.display = show ? '' : 'none';
      }});
    }};
  }}
  bindLayerSet('toggleWalls', WALL_LAYERS, ['WALL','ST-CONC','A-CON','A-ST','CON']);
  bindLayerSet('toggleWindows', WIN_LAYERS, ['WIN','WID']);
}})();
</script>
</body>
</html>
"""


def render(input_path, output_path, ref_box=None, ref_min_len=0, cls_path=None):
    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)

    # 벽체/창호 레이어 = 분류 결과로 인식(이름패턴 아님). 두 포맷 지원:
    #   layer_classify.py -> {"categories": {layer: wall_struct/wall_nonstruct/window/...}}
    #   section_analyze.py -> {"layer_classification": {layer: wall/window/other}}
    wall_layers = win_layers = None
    if cls_path and os.path.exists(cls_path):
        try:
            cj = json.load(open(cls_path, encoding="utf-8"))
            cats = cj.get("categories") or cj.get("layer_classification") or {}
            wall_layers = [ly for ly, c in cats.items()
                           if c in ("wall_struct", "wall_nonstruct", "wall")]
            win_layers = [ly for ly, c in cats.items() if c == "window"]
        except Exception:
            wall_layers = win_layers = None

    entities = load_entities(data)
    min_x, min_y, max_x, max_y = compute_bounds(entities)
    tf = Transform(max_y)

    groups = build_svg_elements(entities, tf)

    # 기준 벽체 영역 색칠 오버레이 (+ 영역 박스)
    highlight_svg = build_highlight(entities, tf, ref_box, min_len=ref_min_len)
    if ref_box:
        xlo, ylo, xhi, yhi = ref_box
        bx, by = xlo, tf.y(yhi)  # 화면 좌상단
        highlight_svg.append(
            f'<rect id="refbox" x="{fmt(bx)}" y="{fmt(by)}" '
            f'width="{fmt(xhi - xlo)}" height="{fmt(yhi - ylo)}"/>'
        )

    # viewBox: 화면좌표(Y 반전 후) 기준. 여백 2% 추가.
    w = max_x - min_x
    h = max_y - min_y
    pad = max(w, h) * 0.02 or 10
    vb_x = min_x - pad
    vb_y = (max_y - max_y) - pad  # = -pad (반전 후 상단)
    vb_w = w + pad * 2
    vb_h = h + pad * 2

    type_counts = {}
    for e in entities:
        t = e.get("Type", "?")
        type_counts[t] = type_counts.get(t, 0) + 1

    n_dim = type_counts.get("RotatedDimension", 0) + type_counts.get(
        "LineAngularDimension2", 0
    )
    n_hatch = type_counts.get("Hatch", 0)
    counts = (
        f"Line {type_counts.get('Line', 0)}, "
        f"Arc {type_counts.get('Arc', 0)}, "
        f"Polyline {type_counts.get('Polyline', 0)}, "
        f"Circle {type_counts.get('Circle', 0)}, "
        f"Text {type_counts.get('DBText', 0)}, "
        f"Block {type_counts.get('BlockReference', 0)}, "
        f"Dim {n_dim}"
    )
    # 형상 좌표가 없어 그릴 수 없는 타입 안내 (flat 포맷은 Dropped 에 기록됨)
    no_geom = dict(data.get("Dropped", {}))
    for t in ("Hatch", "Ellipse", "Spline"):
        if type_counts.get(t):
            no_geom[t] = no_geom.get(t, 0) + type_counts[t]
    if no_geom:
        note = ", ".join(f"{k} {v}" for k, v in no_geom.items())
        counts += f" · 좌표없음·미표시: {note}"

    filename = os.path.basename(data.get("File", input_path))

    # 유저가 뽑은 기준 벽체(Reference) 강조
    ref_svg = ""
    ref = data.get("Reference")
    if isinstance(ref, dict):
        diag = max(max_x - min_x, max_y - min_y)
        if ref.get("Type") == "Line" and ref.get("Start") and ref.get("End"):
            # 기준이 실제 벽 선분 -> 그 선을 그대로 굵게 강조
            x1, y1 = ref["Start"][:2]
            x2, y2 = ref["End"][:2]
            sx1, sy1, sx2, sy2 = x1, tf.y(y1), x2, tf.y(y2)
            mx, my = (sx1 + sx2) / 2, (sy1 + sy2) / 2
            rr = diag * 0.015
            rlbl = "★ 기준벽체"
            if ref.get("Layer"):
                rlbl += f" [{html.escape(str(ref['Layer']))}]"
            ref_svg = (
                f'<line x1="{fmt(sx1)}" y1="{fmt(sy1)}" x2="{fmt(sx2)}" y2="{fmt(sy2)}"/>'
                f'<circle cx="{fmt(mx)}" cy="{fmt(my)}" r="{fmt(rr)}"/>'
                f'<text x="{fmt(mx + rr)}" y="{fmt(my)}" font-size="{fmt(rr)}">{rlbl}</text>'
            )
            counts += f" · 기준벽체: 선분(Line){' '+str(ref.get('Layer')) if ref.get('Layer') else ''}"
        elif ref.get("Pos"):
            # 기준이 블록 삽입점
            rx, ry = ref["Pos"][:2]
            rsy = tf.y(ry)
            rname = html.escape(str(ref.get("BlockName", "Reference")))
            rr = diag * 0.01
            ref_svg = (
                f'<circle cx="{fmt(rx)}" cy="{fmt(rsy)}" r="{fmt(rr)}"/>'
                f'<text x="{fmt(rx + rr)}" y="{fmt(rsy)}" font-size="{fmt(rr)}">★ 기준벽체 {rname}</text>'
            )
            counts += f" · 기준벽체: {ref.get('BlockName', '?')}"

    page = HTML_TEMPLATE.format(
        title=esc_attr(filename),
        filename=esc_attr(filename),
        counts=esc_attr(counts),
        vb_x=fmt(vb_x),
        vb_y=fmt(vb_y),
        vb_w=fmt(vb_w),
        vb_h=fmt(vb_h),
        geom="\n".join(groups["geom"]),
        texts="\n".join(groups["texts"]),
        blocks="\n".join(groups["blocks"]),
        dims="\n".join(groups["dims"]),
        reference=ref_svg,
        highlight="\n".join(highlight_svg),
        wall_layers=json.dumps(wall_layers, ensure_ascii=False) if wall_layers is not None else "null",
        win_layers=json.dumps(win_layers, ensure_ascii=False) if win_layers is not None else "null",
    )

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(page)

    print(f"입력: {input_path}")
    print(f"엔티티 {len(entities)}개")
    print(
        f"  형상 {len(groups['geom'])} · 텍스트 {len(groups['texts'])} · "
        f"블록 {len(groups['blocks'])} · 치수 {len(groups['dims'])}"
    )
    if no_geom:
        print("  좌표없음·미표시:", ", ".join(f"{k} {v}" for k, v in no_geom.items()))
    print(f"경계: X[{min_x:.1f}, {max_x:.1f}]  Y[{min_y:.1f}, {max_y:.1f}]")
    print(f"출력: {output_path}")


def main():
    ap = argparse.ArgumentParser(description="CAD JSON -> 인터랙티브 HTML 뷰어")
    ap.add_argument("input", nargs="?", help="입력 JSON 경로 (생략 시 data/ 폴더의 첫 JSON)")
    ap.add_argument("-o", "--output", default=os.path.join("output", "viewer.html"))
    ap.add_argument("--ref-box", default="",
                    help="기준 벽체 색칠 영역 'xlo,ylo,xhi,yhi' (CAD좌표). 생략 시 색칠 안 함")
    ap.add_argument("--ref-min-len", type=float, default=0,
                    help="영역 내 이 길이(mm) 이상의 Line만 색칠(벽 근사). 0이면 모든 형상")
    ap.add_argument("--cls", default="",
                    help="layer_classification.json 경로 — 벽체/창호 토글을 분류 결과로 인식")
    args = ap.parse_args()

    input_path = args.input
    if not input_path:
        candidates = sorted(glob.glob(os.path.join("data", "*.json")))
        if not candidates:
            print("data/ 폴더에서 JSON을 찾지 못했습니다.", file=sys.stderr)
            sys.exit(1)
        input_path = candidates[0]

    ref_box = None
    if args.ref_box:
        ref_box = [float(v) for v in args.ref_box.split(",")]

    render(input_path, args.output, ref_box=ref_box, ref_min_len=args.ref_min_len,
           cls_path=args.cls or None)


if __name__ == "__main__":
    main()
