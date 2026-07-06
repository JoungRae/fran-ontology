"""
레이어 기반 분류 파이프라인 (레이어를 잘 쓰는 도면용 1차 경로).

1. JSON에서 레이어별 통계 추출 (lines/arcs/circles/median_len/텍스트 샘플)
2. GPT-5 텍스트 1콜: 레이어 전체를 일괄 분류
   - 카테고리: wall/column/stair/door/window/elevator/other/needs_review
   - 기준벽 레이어를 프롬프트에 명시 (그 레이어는 무조건 wall)
   - confidence 낮음 or needs_review -> 3단계로
3. 불확실 레이어: 해당 레이어 빨강 + 나머지 연회색 이미지 생성(레이어당 1장)
   -> GPT-5 이미지 판정 (mixed 감지 포함)
4. mixed 레이어: 기하 폴백 (레이어 내 평행쌍 -> wall, 나머지 other)
5. 카테고리 범례 HTML + 검증 PNG

사용법: python layer_classify.py [입력.json] [--dry-run]
"""

import argparse
import base64
import collections
import concurrent.futures
import json
import math
import os
import statistics
import sys

from dotenv import load_dotenv

# Windows cp949 콘솔에서 GPT 응답의 특수문자(논브레이킹 하이픈 등) 출력 깨짐 방지
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CATEGORIES = ["wall_struct", "wall_nonstruct", "column_struct", "column_nonstruct",
              "stair", "door", "window", "elevator", "other"]
CAT_KO = {"wall_struct": "구조벽체", "wall_nonstruct": "비구조벽체",
          "column_struct": "구조기둥", "column_nonstruct": "비구조기둥",
          "stair": "계단", "door": "문", "window": "창호",
          "elevator": "엘리베이터", "other": "그외",
          "mixed": "혼합", "needs_review": "재검토",
          "dangling": "허공에 뜬 선분", "dashed": "점선(타층/가상)",
          "extended": "구조선 연장"}
CAT_COLOR = {"wall_struct": "#0d47a1", "wall_nonstruct": "#42a5f5",
             "column_struct": "#4a148c", "column_nonstruct": "#ce93d8",
             "stair": "#8d6e63", "door": "#2e7d32", "window": "#00b8d4",
             "elevator": "#f9a825", "mixed": "#e53935", "other": "#bbbbbb",
             "dangling": "#ff1744", "dashed": "#ff8c00", "extended": "#e00000"}

# 점선/은선/중심선 = 절단면 위·아래(타층) 또는 가상선 → 이 층 구조체 아님
_SOLID_LT = {"continuous", "bylayer", "byblock", "", "solid"}
_DASH_LT = ("HID", "HIDDEN", "HD", "DASH", "CEN", "CENTER", "PHANTOM", "ACAD_ISO", "DOT")


def is_dashed_lt(linetype):
    if linetype is None:
        return False
    s = str(linetype).strip()
    return s.lower() not in _SOLID_LT and any(s.upper().startswith(k) for k in _DASH_LT)

# 도면 종류별 컨텍스트 (파일명 접두사 A=건축, S=구조 자동 감지 + --dwg-type 오버라이드)
DWG_CONTEXT = {
    "arch": (
        "This is an ARCHITECTURAL floor plan (건축도면). It contains BOTH structural "
        "elements (reinforced-concrete bearing walls/columns) AND non-structural "
        "elements (drywall 건식벽, masonry 조적벽, light partitions, furred walls). "
        "For consistency comparison you MUST separate them: concrete/bearing -> "
        "*_struct, dry/light/masonry partitions -> *_nonstruct."),
    "struct": (
        "This is a STRUCTURAL drawing (구조도면). Most elements are structural "
        "(RC walls/columns). Non-structural partitions may still appear (e.g. "
        "A-WALL-DRY) and must be classified *_nonstruct."),
}


def seg_len(e):
    if e["Type"] == "Line":
        return math.hypot(e["End"][0] - e["Start"][0], e["End"][1] - e["Start"][1])
    if e["Type"] == "Arc":
        a0, a1 = e.get("StartAngle", 0), e.get("EndAngle", 0)
        return e.get("Radius", 0) * ((a1 - a0) % (2 * math.pi))
    return 0.0


def layer_stats(entities):
    st = collections.defaultdict(lambda: {
        "lines": 0, "arcs": 0, "circles": 0, "polylines": 0,
        "texts": 0, "dims": 0, "line_lens": [], "text_samples": []})
    for e in entities:
        ly = e.get("Layer") or "(no-layer)"
        s = st[ly]
        t = e["Type"]
        if t == "Line":
            s["lines"] += 1
            s["line_lens"].append(seg_len(e))
        elif t == "Arc":
            s["arcs"] += 1
        elif t == "Circle":
            s["circles"] += 1
        elif t == "Polyline":
            s["polylines"] += 1
        elif t == "DBText":
            s["texts"] += 1
            txt = str(e.get("Text", "")).strip()
            if txt and txt not in s["text_samples"] and len(s["text_samples"]) < 5:
                s["text_samples"].append(txt[:20])
        elif "Dimension" in t:
            s["dims"] += 1
    out = {}
    for ly, s in st.items():
        out[ly] = {
            "lines": s["lines"], "arcs": s["arcs"], "circles": s["circles"],
            "polylines": s["polylines"], "texts": s["texts"], "dims": s["dims"],
            "median_len": round(statistics.median(s["line_lens"])) if s["line_lens"] else 0,
            "text_samples": s["text_samples"],
        }
    return out


STAGE1_PROMPT = """You are classifying CAD layers of a Korean apartment residential-building
floor plan (아파트 주동평면도).

{dwg_context}

PURPOSE: the result is used for structural consistency comparison between drawings.
Therefore ONLY layers that draw PHYSICALLY BUILT elements (actual geometry of the
element itself) belong to a building category. Any abstract, virtual, or helper
linework is NOT a building element.

ALWAYS classify as "other" (no exceptions):
- wall/column CENTER LINES (벽 중심선, axis lines, grid lines) — virtual lines, not built
- dimension lines, extension lines, tick marks, leaders (치수·보조선)
- text, labels, numbering, symbols, hatches
- INSULATION / condensation-prevention / finish / waterproofing DETAIL (결로방지재, 단열재,
  마감선, 방수). These trace a thin detail strip INSIDE/ON a wall, NOT the structural element
  itself. Name/desc cues: 결로, 단열, INSUL/INSL, 마감, FIN, 방수, WATERPROOF.
  (예: "A-벽체결로" 폭 13mm 띠 = 결로방지재 -> other, 기둥/벽 아님)
- any guide/reference/hidden/virtual line that does not trace the element's real outline

Categories (use exactly these English keys):
- wall_struct: actual face/outline of STRUCTURAL load-bearing walls (RC/concrete, 내력벽)
- wall_nonstruct: actual face/outline of NON-structural walls (drywall 건식벽, masonry
  조적, light partitions)
- column_struct: structural column outlines
- column_nonstruct: non-structural/decorative columns
- stair: actual stair geometry
- door: doors (leafs, frames, swing arcs)
- window: windows
- elevator: elevators
- other: everything else, INCLUDING all center/axis/dimension/virtual lines above
- needs_review: you cannot decide from name + stats alone

IMPORTANT: the user selected a reference WALL segment, and it belongs to layer
"{ref_layer}". That layer is a structural concrete wall -> wall_struct.

Layer name hints: Korean CAD conventions like A-WALL, A-ST-CONC(구조 콘크리트), COL(기둥),
WIN(창), DOOR(문), CEN(중심선), DIM(치수), ELEV/ELE(엘리베이터/입면), STL(steel), 계단(stair),
결로/단열/INSUL=결로방지·단열재(->other), 마감/FIN=마감선(->other), 방수/WP=방수(->other).
Names may be abbreviated or in Korean.

Each layer line may include a Korean DESCRIPTION in [설명: ...] — TRUST it: it states the
layer's real purpose (e.g. [설명: 결로방지재] = insulation -> other; [설명: 마감선] = finish ->
other). Also: a very small median_len (~10-20mm) on a wall/기둥-sounding layer usually means a
thin detail strip (insulation/finish), not a real wall/column.

Layers (name, optional [설명], entity counts, median line length mm, sample texts):
{table}

Respond ONLY with JSON, no markdown:
{{"layers": [{{"layer": "<name>", "category": "<key>", "confidence": "high|medium|low", "reason": "<short>"}}]}}"""


STAGE3_PROMPT = """The attached image is an apartment floor plan. Colors:
- RED   = the layer "{layer}" you must classify
- YELLOW = the user-picked REFERENCE WALL layer "{ref_wall}" (a structural concrete wall) — use as a visual anchor for what a WALL looks like
- BLUE  = the user-picked REFERENCE WINDOW layer "{ref_win}" — use as a visual anchor for what a WINDOW looks like
- light gray = all other layers (context)

Use YELLOW/BLUE only as POSITION anchors (where real wall faces / window openings are),
NOT as a thickness template — a wall can be much thinner than the YELLOW concrete wall.
- If RED draws the two parallel FACES (outlines) bounding a solid wall — sitting ON wall
  lines like YELLOW marks, whether thick or thin -> it is a WALL. Then decide:
    wall_struct if it is a thick concrete/bearing wall (like the YELLOW reference),
    wall_nonstruct if it is a thinner drywall / masonry / light partition.
- If RED sits in the openings between walls like the BLUE reference -> window
- If RED is a SINGLE line running down the CENTER of a wall (not on the faces), or is a
  dimension / extension / leader / grid line / text / symbol / hatch -> other

{dwg_context}

Layer stats: {stats}

PURPOSE: structural consistency comparison. Only PHYSICALLY BUILT geometry counts —
the category must describe the element's REAL outline/face geometry.

ALWAYS answer "other" (no exceptions) if the red lines are:
- wall/column CENTER LINES or axis/grid lines (single lines running along the middle
  of walls, or long lines crossing the whole plan) — virtual, not built
- dimension lines, extension lines, tick marks, leaders
- text, labels, symbols, hatches, guide/hidden/virtual lines

Which single category does the RED layer represent?
- wall_struct (RC/bearing wall faces) / wall_nonstruct (drywall·masonry·partition faces)
- column_struct / column_nonstruct
- stair / door / window / elevator / other
- If the red elements clearly contain MULTIPLE different kinds (e.g. walls AND furniture), answer "mixed".

Respond ONLY with JSON: {{"category": "<key>", "confidence": "high|medium|low", "reason": "<short>"}}"""


def render_layer_png(entities, target_layer, out_png, ref_wall=None, ref_win=None):
    """판정 대상 레이어=빨강. 기준 벽 레이어=노랑, 기준 창호 레이어=파랑(시각 앵커).
    대상이 기준 레이어와 겹치면 빨강 우선(대상 강조)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    def coords(e):
        t = e["Type"]
        if t == "Line":
            return [[e["Start"][0], e["End"][0]], [e["Start"][1], e["End"][1]]]
        if t == "Polyline":
            v = e["Verts"] + ([e["Verts"][0]] if e.get("Closed") else [])
            return [[p[0] for p in v], [p[1] for p in v]]
        if t == "Arc":
            cx, cy = e["Center"][:2]
            r = e["Radius"]; a0 = e.get("StartAngle", 0); a1 = e.get("EndAngle", 0)
            sw = (a1 - a0) % (2 * math.pi)
            return [[cx + r * math.cos(a0 + sw * k / 12) for k in range(13)],
                    [cy + r * math.sin(a0 + sw * k / 12) for k in range(13)]]
        if t == "Circle":
            cx, cy = e["Center"][:2]
            r = e["Radius"]
            return [[cx + r * math.cos(2 * math.pi * k / 24) for k in range(25)],
                    [cy + r * math.sin(2 * math.pi * k / 24) for k in range(25)]]
        return None

    fig, ax = plt.subplots(figsize=(14, 9), dpi=100)
    buckets = {"gray": ([], []), "yellow": ([], []), "blue": ([], []), "red": ([], [])}
    for e in entities:
        c = coords(e)
        if not c:
            continue
        ly = e.get("Layer") or "(no-layer)"
        if ly == target_layer:
            key = "red"                       # 대상 강조(겹치면 우선)
        elif ref_wall and ly == ref_wall:
            key = "yellow"                    # 기준 벽
        elif ref_win and ly == ref_win:
            key = "blue"                      # 기준 창호
        else:
            key = "gray"
        buckets[key][0].extend(c[0] + [None])
        buckets[key][1].extend(c[1] + [None])
    # 회색 → 노랑 → 파랑 → 빨강 순으로 그려 대상이 위에 오게
    ax.plot(buckets["gray"][0], buckets["gray"][1], color="#cccccc", lw=0.5)
    ax.plot(buckets["yellow"][0], buckets["yellow"][1], color="#f5c000", lw=1.3)
    ax.plot(buckets["blue"][0], buckets["blue"][1], color="#1565ff", lw=1.3)
    ax.plot(buckets["red"][0], buckets["red"][1], color="#e00000", lw=1.6)
    ax.set_aspect("equal"); ax.axis("off")
    plt.tight_layout()
    plt.savefig(out_png, facecolor="white")
    plt.close(fig)


def gpt_json(client, model, content, max_tokens=8000, retries=3):
    """GPT 호출 + JSON 파싱. 깨진 JSON(특수문자 레이어명 등)은 재시도."""
    last_err = None
    for attempt in range(retries):
        try:
            return _gpt_json_once(client, model, content, max_tokens)
        except (json.JSONDecodeError, RuntimeError) as e:
            last_err = e
            print(f"    [재시도 {attempt+1}/{retries}] 응답 파싱 실패: {e}")
    raise last_err


def _gpt_json_once(client, model, content, max_tokens):
    resp = client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": content}],
        max_completion_tokens=max_tokens)
    u = resp.usage
    rt = getattr(getattr(u, "completion_tokens_details", None), "reasoning_tokens", "?")
    print(f"    [usage] input {u.prompt_tokens:,} · output {u.completion_tokens:,} "
          f"(그중 reasoning {rt}) · finish={resp.choices[0].finish_reason}")
    raw = (resp.choices[0].message.content or "").strip()
    if not raw:
        raise RuntimeError(
            f"빈 응답 (finish_reason={resp.choices[0].finish_reason}) — "
            f"max_completion_tokens 부족 가능성")
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    # JSON 본문만 추출 (앞뒤 잡텍스트 방어)
    s, e = raw.find("{"), raw.rfind("}")
    if s >= 0 and e > s:
        raw = raw[s:e + 1]
    return json.loads(raw)


def window_opening_score(layer_segs, wall_segs):
    """기하 검증기: 레이어 선분이 구조벽 줄기의 개구부(틈) 안에 놓인 비율(0~1).

    창선은 벽 개구부 안에 위치한다 — LLM 표가 갈리는 window↔other 진동을
    비결정적 재투표 대신 결정적 기하로 판별한다.
    """
    from compare_drawings import normalize_runs  # 줄기 병합 재사용
    runs = normalize_runs(wall_segs)
    hits = n = 0
    nb = int(180 / 0.5)
    for x1, y1, x2, y2 in layer_segs:
        th = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180.0
        b = int(round(th / 0.5)) % nb
        rad = math.radians(b * 0.5)
        dx, dy = math.cos(rad), math.sin(rad)
        c = x1 * -dy + y1 * dx
        t1, t2 = sorted((x1 * dx + y1 * dy, x2 * dx + y2 * dy))
        n += 1
        found = False
        for c_w, ivs in runs.get(b, []):
            if abs(c_w - c) > 300:  # 벽 두께 대역 안 (창은 벽면 사이/근처)
                continue
            for k in range(len(ivs) - 1):
                g1, g2 = ivs[k][1], ivs[k + 1][0]
                # 완전 포함 요구: 창선은 개구부 '안에' 들어가고,
                # 중심선·축선은 개구부를 '뚫고 지나가' 탈락한다
                if (200 <= g2 - g1 <= 3500
                        and t1 >= g1 - 50 and t2 <= g2 + 50):
                    found = True
                    break
            if found:
                break
        if found:
            hits += 1
    return hits / max(1, n)


def door_swing_score(layer_segs, entities):
    """기하 검증기: 레이어 선분이 문 스윙 호(반경 300~1200mm) 근처에 있는 비율(0~1).

    문짝·문틀은 스윙 호와 붙어 다닌다 — door↔other 모호 판정을 결정적으로 판별.
    도면에 스윙 호가 아예 없으면 0 (문을 호로 그리지 않는 도면에선 door 확정 불가
    -> 보수적으로 비-문 처리. 이름이 명확한 문 레이어는 1차 high라 검증기 미적용).
    """
    swings = [(e["Center"][0], e["Center"][1], e["Radius"])
              for e in entities
              if e.get("Type") == "Arc" and 300.0 <= e.get("Radius", 0) <= 1200.0]
    if not swings or not layer_segs:
        return 0.0
    hits = 0
    for x1, y1, x2, y2 in layer_segs:
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        if any((mx - cx) ** 2 + (my - cy) ** 2 <= (r + 300.0) ** 2
               for cx, cy, r in swings):
            hits += 1
    return hits / len(layer_segs)


def _pt_seg_d2(px, py, ax, ay, bx, by):
    """점-선분 거리 제곱."""
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    cx, cy = ax + t * dx, ay + t * dy
    return (px - cx) ** 2 + (py - cy) ** 2


def prune_open_structures(entities, cat, tol=15.0, max_iter=10):
    """벽·기둥 다각형 강제: 끝이 허공에 뜬(dangling) 벽/기둥 선분을 반복 제거.

    - 실체 구조(벽·기둥)는 닫힌 외곽을 이뤄야 한다. 양 끝점이 다른 시공 요소
      (벽·기둥·창·문·계단·승강기)의 몸체에 tol 이내로 닿지 않으면 비구조 선
      (입면 띠선·보조선 잔재)으로 보고 제거한다.
    - 닫힌 폴리라인·원은 자체로 다각형이므로 제거 대상이 아니며 지지대 역할만 한다.
    - 제거가 다른 선의 지지를 무너뜨릴 수 있으므로 안정될 때까지 반복.
    반환: (제거된 엔티티 인덱스 set, 레이어별 제거 수 Counter)
    """
    TARGET = {"wall_struct", "wall_nonstruct", "column_struct", "column_nonstruct"}
    BUILT = TARGET | {"window", "door", "stair", "elevator"}
    elems = []
    for i, e in enumerate(entities):
        ly = e.get("Layer") or "(no-layer)"
        c = cat.get(ly, "other")
        if c not in BUILT:
            continue
        t = e["Type"]
        if t == "Line":
            pts = [(e["Start"][0], e["Start"][1]), (e["End"][0], e["End"][1])]
            closed = False
        elif t == "Polyline":
            pts = [(p[0], p[1]) for p in e["Verts"]]
            closed = bool(e.get("Closed"))
        elif t == "Arc":
            cx, cy = e["Center"][:2]
            r = e["Radius"]; a0 = e.get("StartAngle", 0); a1 = e.get("EndAngle", 0)
            sw = (a1 - a0) % (2 * math.pi)
            pts = [(cx + r * math.cos(a0 + sw * k / 8),
                    cy + r * math.sin(a0 + sw * k / 8)) for k in range(9)]
            closed = False
        elif t == "Circle":
            cx, cy = e["Center"][:2]
            r = e["Radius"]
            pts = [(cx + r * math.cos(2 * math.pi * k / 16),
                    cy + r * math.sin(2 * math.pi * k / 16)) for k in range(17)]
            closed = True
        else:
            continue
        if len(pts) < 2:
            continue
        length = sum(math.hypot(pts[j + 1][0] - pts[j][0], pts[j + 1][1] - pts[j][1])
                     for j in range(len(pts) - 1))
        # 50mm 미만 미세조각은 지지대 자격 없음 (A-DOOR 잔재 등 가짜 지지 차단)
        elems.append({"i": i, "pts": pts, "closed": closed,
                      "target": c in TARGET, "layer": ly,
                      "support": length >= 50.0})

    kept = set(range(len(elems)))
    cell = 500.0

    def build_grid():
        grid = collections.defaultdict(list)
        for k in kept:
            if not elems[k]["support"]:
                continue
            pts = elems[k]["pts"]
            for j in range(len(pts) - 1):
                (ax, ay), (bx, by) = pts[j], pts[j + 1]
                for gx in range(int(min(ax, bx) // cell), int(max(ax, bx) // cell) + 1):
                    for gy in range(int(min(ay, by) // cell), int(max(ay, by) // cell) + 1):
                        grid[(gx, gy)].append((k, j))
        return grid

    def supported(pt, self_k, grid):
        px, py = pt
        g0, g1 = int(px // cell), int(py // cell)
        for gx in (g0 - 1, g0, g0 + 1):
            for gy in (g1 - 1, g1, g1 + 1):
                for k, j in grid.get((gx, gy), ()):
                    if k == self_k:
                        continue
                    pts = elems[k]["pts"]
                    (ax, ay), (bx, by) = pts[j], pts[j + 1]
                    if _pt_seg_d2(px, py, ax, ay, bx, by) <= tol * tol:
                        return True
        return False

    pruned = []
    for _ in range(max_iter):
        grid = build_grid()
        rm = [k for k in kept
              if elems[k]["target"] and not elems[k]["closed"]
              and (not supported(elems[k]["pts"][0], k, grid)
                   or not supported(elems[k]["pts"][-1], k, grid))]
        if not rm:
            break
        for k in rm:
            kept.discard(k)
        pruned += rm
    by_layer = collections.Counter(elems[k]["layer"] for k in pruned)
    return {elems[k]["i"] for k in pruned}, by_layer


def close_open_structures(entities, cat, tol=15.0, extend_max=600.0):
    """구조 벽/기둥 선의 끝이 떠 있으면(다른 시공요소에 tol 이내로 안 닿으면) 그 선
    방향으로 연장해 가장 가까운 다른 구조선에 맞닿게 한다. (제거하지 않고 연장)

    - 대상: wall_struct·column_struct 의 Line.
    - 연장 끝점은 선 방향 ray 와 다른 BUILT 선분의 교차점(extend_max 이내, 가장 가까운 것).
      동일선상(평행) 선은 교차 없음 → 센터라인 위가 아니라 직교하는 실제 벽/기둥에 닿는다.
    반환: (extensions[{i,end,from,to}], 레이어별 수 Counter)
    """
    STRUCT = {"wall_struct", "column_struct"}
    BUILT = STRUCT | {"wall_nonstruct", "column_nonstruct",
                      "window", "door", "stair", "elevator"}
    segs = []   # (owner_i, ax, ay, bx, by)
    for i, e in enumerate(entities):
        if cat.get(e.get("Layer") or "(no-layer)", "other") not in BUILT:
            continue
        t = e["Type"]
        if t == "Line":
            segs.append((i, e["Start"][0], e["Start"][1], e["End"][0], e["End"][1]))
        elif t == "Polyline":
            v = e["Verts"] + ([e["Verts"][0]] if e.get("Closed") else [])
            for j in range(len(v) - 1):
                segs.append((i, v[j][0], v[j][1], v[j + 1][0], v[j + 1][1]))
    cell = 500.0
    grid = collections.defaultdict(list)
    for idx, (oi, ax, ay, bx, by) in enumerate(segs):
        for gx in range(int(min(ax, bx) // cell), int(max(ax, bx) // cell) + 1):
            for gy in range(int(min(ay, by) // cell), int(max(ay, by) // cell) + 1):
                grid[(gx, gy)].append(idx)

    def nearby(px, py, reach):
        out = set()
        r = int(reach // cell) + 1
        g0, g1 = int(px // cell), int(py // cell)
        for gx in range(g0 - r, g0 + r + 1):
            for gy in range(g1 - r, g1 + r + 1):
                out.update(grid.get((gx, gy), ()))
        return out

    def supported(px, py, self_i):
        for idx in nearby(px, py, tol + 1):
            oi, ax, ay, bx, by = segs[idx]
            if oi != self_i and _pt_seg_d2(px, py, ax, ay, bx, by) <= tol * tol:
                return True
        return False

    def nearest_hit(px, py, dx, dy, self_i):
        best = None
        for idx in nearby(px + dx * extend_max / 2, py + dy * extend_max / 2, extend_max):
            oi, ax, ay, bx, by = segs[idx]
            if oi == self_i:
                continue
            ex, ey = bx - ax, by - ay
            det = -dx * ey + dy * ex
            if abs(det) < 1e-9:           # 평행(동일선상 센터라인 등) → 건너뜀
                continue
            qx, qy = ax - px, ay - py
            s = (-qx * ey + ex * qy) / det
            u = (dx * qy - qx * dy) / det
            if 1.0 < s <= extend_max and -0.01 <= u <= 1.01 and (best is None or s < best):
                best = s
        return best

    extensions = []
    for i, e in enumerate(entities):
        if e["Type"] != "Line":
            continue
        if cat.get(e.get("Layer") or "(no-layer)", "other") not in STRUCT:
            continue
        P = [(e["Start"][0], e["Start"][1]), (e["End"][0], e["End"][1])]
        if math.hypot(P[1][0] - P[0][0], P[1][1] - P[0][1]) < 1:
            continue
        for end in (0, 1):
            px, py = P[end]
            if supported(px, py, i):
                continue
            ox, oy = P[end][0] - P[1 - end][0], P[end][1] - P[1 - end][1]
            dn = math.hypot(ox, oy)
            if dn < 1:
                continue
            dx, dy = ox / dn, oy / dn
            s = nearest_hit(px, py, dx, dy, i)
            if s is not None:
                extensions.append({"i": i, "end": end,
                                   "from": [round(px, 1), round(py, 1)],
                                   "to": [round(px + dx * s, 1), round(py + dy * s, 1)]})
    by_layer = collections.Counter(
        (entities[x["i"]].get("Layer") or "(no-layer)") for x in extensions)
    return extensions, by_layer


def apply_extensions(entities, extensions):
    """연장 결과를 엔티티 기하에 반영 (Start/End 끝점 이동)."""
    for x in extensions:
        e = entities[x["i"]]
        key = "Start" if x["end"] == 0 else "End"
        old = e.get(key) or [0, 0]
        e[key] = list(x["to"]) + (list(old[2:]) if len(old) > 2 else [])


def geometry_fallback_wall_ids(entities, layer):
    """mixed 레이어 폴백: 레이어 내 Line 평행쌍(50~250mm, 겹침>=300) -> wall."""
    segs = []
    for i, e in enumerate(entities):
        if (e.get("Layer") or "(no-layer)") != layer or e["Type"] != "Line":
            continue
        x1, y1 = e["Start"][:2]; x2, y2 = e["End"][:2]
        L = math.hypot(x2 - x1, y2 - y1)
        if L < 100:
            continue
        th = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180
        b = round(th / 0.5)
        rad = math.radians(b * 0.5)
        dx, dy = math.cos(rad), math.sin(rad)
        c = x1 * -dy + y1 * dx
        t1, t2 = sorted((x1 * dx + y1 * dy, x2 * dx + y2 * dy))
        segs.append({"i": i, "b": b, "c": c, "t1": t1, "t2": t2})
    wall_idx = set()
    by_b = collections.defaultdict(list)
    for s in segs:
        by_b[s["b"]].append(s)
    for b, g in by_b.items():
        g.sort(key=lambda s: s["c"])
        for i in range(len(g)):
            for j in range(i + 1, len(g)):
                dc = g[j]["c"] - g[i]["c"]
                if dc > 250:
                    break
                if dc < 50:
                    continue
                ov = min(g[i]["t2"], g[j]["t2"]) - max(g[i]["t1"], g[j]["t1"])
                if ov >= 300:
                    wall_idx.add(g[i]["i"]); wall_idx.add(g[j]["i"])
    return wall_idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", nargs="?",
                    default="data/S1-201~268 주동평면도-지상층(5BL)_20260611_134225.json")
    ap.add_argument("-o", "--output", default="",
                    help="결과 HTML 경로 (기본: output/<입력파일명>_layer.html)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--render-only", action="store_true",
                    help="기존 분류 JSON 으로 HTML(레이어 보기)만 재생성 — GPT 미호출")
    ap.add_argument("--dwg-type", choices=["arch", "struct"], default="",
                    help="도면 종류 (생략 시 파일명 접두사 A=건축/S=구조 자동 감지)")
    ap.add_argument("--keep-dangling", action="store_true",
                    help="지하 부분도면: 끝이 허공에 뜬 벽/기둥 선분을 other로 빼지 않고 구조로 유지")
    args = ap.parse_args()

    base = os.path.splitext(os.path.basename(args.input))[0]
    dwg_type = args.dwg_type or ("struct" if base[:1].upper() == "S" else "arch")
    print(f"도면 종류: {'건축(arch)' if dwg_type=='arch' else '구조(struct)'}"
          f"{' (파일명 자동감지)' if not args.dwg_type else ''}")
    dwg_context = DWG_CONTEXT[dwg_type]
    if not args.output:
        args.output = os.path.join("output", f"{base}_layer.html")
    json_out = os.path.join("output", f"{base}_layer_classification.json")

    load_dotenv()
    data = json.load(open(args.input, encoding="utf-8"))
    entities = data["Entities"]
    # 기준 요소 3포맷 지원:
    #  v3: Reference_Wall_Layer / Reference_Window_Layer (레이어명 직접 — 권장)
    #  v2: Reference_Wall / Reference_Window (선분 픽 -> Layer 필드)
    #  v1: Reference (선분 픽 단수)
    ref = data.get("Reference_Wall") or data.get("Reference", {})
    ref_layer = data.get("Reference_Wall_Layer") or ref.get("Layer", "?")
    win_layer = (data.get("Reference_Window_Layer")
                 or (data.get("Reference_Window") or {}).get("Layer"))
    if win_layer:
        print(f"창호 픽: 레이어 {win_layer} (검증 후 적용)")

    # 재생성 모드: 기존 분류 JSON 으로 HTML(레이어 보기)만 다시 만든다 (GPT 미호출)
    if args.render_only:
        if not os.path.exists(json_out):
            print(f"분류 JSON 없음: {json_out}", file=sys.stderr); sys.exit(1)
        prev = json.load(open(json_out, encoding="utf-8"))
        cat = prev.get("categories", {})
        mixed_layers = prev.get("mixed_layers", [])
        extensions = prev.get("extended", {}).get("items", [])
        pruned_idx = set(prev.get("pruned", {}).get("entity_idx", []))
        mixed_wall_idx = set()
        for ly in mixed_layers:
            mixed_wall_idx |= geometry_fallback_wall_ids(entities, ly)
        counts = build_html(entities, cat, mixed_layers, mixed_wall_idx, pruned_idx,
                            ref, ref_layer, args.output, extensions)
        print(f"\n[재생성] 카테고리별 형상 수: {dict(counts)}")
        print(f"출력: {args.output}")
        return

    st = layer_stats(entities)
    desc_map = {L["Name"]: (L.get("Description") or "").strip()
                for L in data.get("Layers", [])}
    table = "\n".join(
        f'- "{ly}"' + (f' [설명: {desc_map[ly]}]' if desc_map.get(ly) else '')
        + f': lines {s["lines"]}, arcs {s["arcs"]}, circles {s["circles"]}, '
        f'polylines {s["polylines"]}, texts {s["texts"]}, dims {s["dims"]}, '
        f'median_len {s["median_len"]}mm'
        + (f', sample texts {s["text_samples"]}' if s["text_samples"] else "")
        for ly, s in sorted(st.items(), key=lambda x: -x[1]["lines"]))
    print(f"레이어 {len(st)}개, 기준벽 레이어: {ref_layer}")

    if args.dry_run:
        print(table)
        return

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("OPENAI_API_KEY 없음", file=sys.stderr); sys.exit(1)
    model = os.environ.get("OPENAI_MODEL", "gpt-5")
    from openai import OpenAI
    client = OpenAI(api_key=api_key)

    # ---------- 2단계: 명칭+통계 일괄 분류 ----------
    print(f"\n[1차] 레이어 일괄 분류 (model={model}) ...")
    v1 = gpt_json(client, model,
                  STAGE1_PROMPT.format(ref_layer=ref_layer, table=table,
                                       dwg_context=dwg_context))
    cat = {}
    uncertain = []
    judgments = {}  # 레이어별 판정 근거 기록 (결과 JSON에 저장)
    for item in v1.get("layers", []):
        ly = item["layer"]
        c = item.get("category", "needs_review")
        conf = item.get("confidence", "low")
        cat[ly] = c
        judgments[ly] = {"stage": 1, "category": c, "confidence": conf,
                         "reason": item.get("reason", "")}
        flag = ""
        if c == "needs_review" or conf in ("low", "medium"):
            uncertain.append(ly)
            flag = "  -> 이미지 판정"
        print(f'  {ly:24} {c:13} ({conf}) {item.get("reason","")[:46]}{flag}')

    # 기준벽 레이어 강제 + 검증 신호 (기준벽 = 구조 콘크리트 벽)
    if cat.get(ref_layer) != "wall_struct":
        print(f"  ⚠ 1차가 기준벽 레이어({ref_layer})를 wall_struct로 안 봄 -> 강제 + 판정 신뢰도 주의")
    cat[ref_layer] = "wall_struct"

    # 콘크리트 벽은 '두께'와 무관하게 구조벽이다. 기준벽이 콘크리트 계열일 때, GPT가 벽으로
    # 본 같은 계열 레이어(예: 기준 A-CON(주차장) ↔ A-CON(주동))를 '얇아서 비구조'로 강등하지
    # 않고 wall_struct 로 되돌린다. (250mm 주동 내력벽이 400mm 주차장벽보다 얇다고 비구조로
    # 오판되던 것 보정 — 두께-상대 판단의 한계.)
    # 승격 3조건(AND): ① GPT가 벽(wall_nonstruct)으로 봄  ② base 이름이 기준벽과 동일(예 A-CON)
    #                ③ 레이어 대표색이 기준벽과 동일 (같은 펜=같은 부재 종류)
    _lay_color = collections.defaultdict(collections.Counter)
    for _e in entities:
        _lay_color[_e.get("Layer") or "(no-layer)"][_e.get("Color")] += 1

    def _color_of(ly):
        cc = _lay_color.get(ly)
        return cc.most_common(1)[0][0] if cc else None
    ref_base = ref_layer.split("|")[-1].split("(")[0].strip()      # 예: 'A-CON'
    ref_color = _color_of(ref_layer)
    if "CON" in ref_base.upper():
        promoted = []
        for _ly, _c in list(cat.items()):
            if (_c == "wall_nonstruct"
                    and _ly.split("|")[-1].split("(")[0].strip() == ref_base
                    and _color_of(_ly) == ref_color):
                cat[_ly] = "wall_struct"; promoted.append(_ly.split("|")[-1])
        if promoted:
            print(f"  콘크리트 동일계열·동일색({ref_color}) 벽 두께무관 구조벽 승격: {promoted}")

    # 창호 픽: 잠정 적용 + 이미지 판정 생략 (최종 수용 여부는 3.5단계 기하 검증)
    win_pick_prev = None
    if win_layer and win_layer in cat:
        win_pick_prev = cat[win_layer]  # 검증 실패 시 되돌릴 LLM 판정
        cat[win_layer] = "window"
        uncertain = [u for u in uncertain if u != win_layer]

    # ---------- 3단계: 불확실 레이어 이미지 판정 (PNG 순차 렌더 → GPT 비전 병렬) ----------
    mixed_layers = []
    os.makedirs("output", exist_ok=True)
    CONF_RANK = {"high": 2, "medium": 1, "low": 0}

    # (1) matplotlib 렌더는 thread-unsafe 라 PNG 는 순차로 먼저 만든다(빠름).
    todo = []   # [(ly, b64, stats_line)]
    for ly in uncertain:
        if ly == ref_layer:
            continue
        safe = "".join(ch if ch.isalnum() else "_" for ch in ly)
        png = os.path.join("output", f"layer_check_{safe}.png")
        render_layer_png(entities, ly, png, ref_wall=ref_layer, ref_win=win_layer)
        b64 = base64.b64encode(open(png, "rb").read()).decode()
        todo.append((ly, b64, json.dumps(st[ly], ensure_ascii=False)))

    # (2) GPT 비전 호출(레이어 × 3표)은 동시 실행 — 느린 부분을 병렬화.
    def _vote(task):
        ly, b64, stats_line = task
        try:
            v3 = gpt_json(client, model, [
                {"type": "text", "text": STAGE3_PROMPT.format(
                    layer=ly, stats=stats_line, dwg_context=dwg_context,
                    ref_wall=ref_layer or "(none)", ref_win=win_layer or "(none)")},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"}},
            ])
            return ly, (v3.get("category", "other"),
                        v3.get("confidence", "low"), v3.get("reason", ""))
        except Exception as ex:   # 한 표 실패가 전체 분류를 깨지 않도록 저확신 other 로 강등
            return ly, ("other", "low", f"gpt 실패: {ex}")

    votes_by_layer = collections.defaultdict(list)
    if todo:
        tasks = [t for t in todo for _ in range(3)]   # 레이어마다 3표
        print(f"\n[2차] 이미지 판정 {len(todo)}개 레이어 × 3표 = {len(tasks)}콜 동시 실행")
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as exr:
            for ly, vote in exr.map(_vote, tasks):
                votes_by_layer[ly].append(vote)

    # (3) 레이어별 다수결 — 판정 로직은 기존과 동일.
    for ly, _b64, _stats in todo:
        votes = votes_by_layer[ly]
        tally = collections.Counter(vc for vc, _, _ in votes)
        top, n = tally.most_common(1)[0]
        if n >= 2:
            c = top
        else:  # 3표 모두 다름 -> confidence 가장 높은 표 채택
            c = max(votes, key=lambda t: CONF_RANK.get(t[1], 0))[0]
        print(f"  {ly}: {dict(tally)} => {c}"
              + (f" ({n}/3)" if n >= 2 else " (표 분산, 최고확신)"))
        judgments[ly] = {"stage": 2, "category": c, "majority": f"{n}/3",
                         "votes": [{"category": vc, "confidence": cf, "reason": rs}
                                   for vc, cf, rs in votes]}
        if c == "mixed":
            mixed_layers.append(ly)
            cat[ly] = "mixed"
        elif c in CATEGORIES:
            cat[ly] = c
        else:
            cat[ly] = "other"

    # ---------- 3.5단계: 기하 검증기 — window↔other 진동 자동 해소 ----------
    # 이미지 판정을 거친 레이어 중 other/window 로 끝난 것은 LLM 표가 갈리는
    # 경계 케이스일 수 있다. 결정적 지표(구조벽 개구부 점유율)로 최종 판정.
    wall_lines = [(e["Start"][0], e["Start"][1], e["End"][0], e["End"][1])
                  for e in entities
                  if e["Type"] == "Line"
                  and cat.get(e.get("Layer") or "(no-layer)") == "wall_struct"]

    # 창호 픽 검증: 픽도 틀릴 수 있다 (겹친 선 중 중심선을 클릭하는 등).
    # 픽된 레이어의 개구부 점유율이 낮으면 픽을 기각하고 LLM 판정으로 복귀.
    if win_layer and win_layer in cat:
        segs_w = [(e["Start"][0], e["Start"][1], e["End"][0], e["End"][1])
                  for e in entities
                  if e["Type"] == "Line" and (e.get("Layer") or "") == win_layer
                  and math.hypot(e["End"][0] - e["Start"][0],
                                 e["End"][1] - e["Start"][1]) >= 100]
        sc = window_opening_score(segs_w, wall_lines) if segs_w else 0.0
        if sc >= 0.5:
            print(f"[창호픽 검증] {win_layer}: 개구부 점유율 {sc:.0%} -> 픽 수용 (window)")
            judgments[win_layer] = {"stage": 0, "category": "window",
                                    "confidence": "user-pick",
                                    "reason": f"유저 픽 + 기하검증 통과(점유율 {sc:.0%})"}
        else:
            print(f"⚠ [창호픽 검증] {win_layer}: 개구부 점유율 {sc:.0%} — 창호가 아닌 듯"
                  f" (겹친 선 오클릭 의심). 픽 기각, LLM 판정({win_pick_prev}) 복귀")
            cat[win_layer] = win_pick_prev or "other"
            judgments[win_layer] = {"stage": 0, "category": cat[win_layer],
                                    "confidence": "pick-rejected",
                                    "reason": f"창호 픽 기각: 개구부 점유율 {sc:.0%}"}

    for ly in uncertain:
        if cat.get(ly) not in ("other", "window", "door"):
            continue
        segs_ly = [(e["Start"][0], e["Start"][1], e["End"][0], e["End"][1])
                   for e in entities
                   if e["Type"] == "Line" and (e.get("Layer") or "(no-layer)") == ly
                   and math.hypot(e["End"][0] - e["Start"][0],
                                  e["End"][1] - e["Start"][1]) >= 100]
        if len(segs_ly) < 3:
            continue
        before = cat[ly]
        # 승격은 LLM 표가 갈렸을 때만 (만장일치 high 를 기하가 뒤집지 않음),
        # 강등은 항상 (시그니처 부재는 강한 음성 증거)
        unanimous = judgments.get(ly, {}).get("majority", "") == "3/3"
        w_score = window_opening_score(segs_ly, wall_lines)
        d_score = door_swing_score(segs_ly, entities)
        if w_score >= 0.5 and not (unanimous and before != "window"):
            cat[ly] = "window"
        elif d_score >= 0.5 and not (unanimous and before != "door"):
            cat[ly] = "door"
        elif before == "window" and w_score < 0.2:
            cat[ly] = "other"
        elif before == "door" and d_score < 0.2:
            cat[ly] = "other"  # 스윙 호 근접 없음 -> 문 아님 (인방·철골 등)
        if ly in judgments:
            judgments[ly]["geo_validator"] = {
                "opening_score": round(w_score, 3),
                "door_swing_score": round(d_score, 3),
                "before": before, "after": cat[ly]}
        mark = f" -> {cat[ly]}" if cat[ly] != before else " (유지)"
        print(f"[기하검증] {ly}: 개구부 {w_score:.0%} · 스윙호 {d_score:.0%}{mark}")

    # ---------- 4단계: mixed 기하 폴백 ----------
    mixed_wall_idx = set()
    for ly in mixed_layers:
        w = geometry_fallback_wall_ids(entities, ly)
        mixed_wall_idx |= w
        print(f"[폴백] mixed 레이어 {ly}: 평행쌍 기하로 {len(w)}개 선분 wall 판정")

    # ---------- 구조선 연장: 끝이 뜬 벽/기둥 선을 가장 가까운 구조선에 맞닿게 ----------
    pruned_idx = set()    # (구) dangling 제거 대신 연장 — 호환 위해 빈 값 유지
    if args.keep_dangling:
        # 지하 부분도면: 끝이 뜬 선분도 실제 구조로 보고 그대로 둔다(연장도 생략)
        extensions, ext_by_layer = [], collections.Counter()
        print("\n[지하 부분도면] --keep-dangling: 구조선 연장 생략 (원본 유지)")
    else:
        extensions, ext_by_layer = close_open_structures(entities, cat)
        if extensions:
            print(f"\n[구조선 연장] 끝이 뜬 구조 벽/기둥 {len(extensions)}개를 가까운 구조선에 연장")
            for ly, n in ext_by_layer.most_common():
                print(f"   {ly}: {n}개")
            # 원본 선은 그대로 두고, 연장 부분만 레이어 보기에 빨간색으로 따로 그린다

    # ---------- 결과 JSON ----------
    result = {"source": os.path.basename(args.input),
              "dwg_type": dwg_type, "keep_dangling": bool(args.keep_dangling),
              "ref_layer": ref_layer, "ref_window_layer": win_layer,
              "categories": cat,
              "mixed_layers": mixed_layers, "judgments": judgments,
              "extended": {"count": len(extensions),
                           "by_layer": dict(ext_by_layer), "items": extensions},
              "pruned": {"count": 0, "by_layer": {}, "entity_idx": []}}
    with open(json_out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)

    counts = build_html(entities, cat, mixed_layers, mixed_wall_idx, pruned_idx,
                        ref, ref_layer, args.output, extensions)
    print(f"\n카테고리별 형상 수: {dict(counts)}")
    print(f"출력: {args.output}")
    print(f"출력: {json_out}")


def build_html(entities, cat, mixed_layers, mixed_wall_idx, pruned_idx,
               ref, ref_layer, output, extensions=None):
    """분류 결과(cat 등)로 범례+토글 시각화 HTML 생성. GPT 불필요 —
    재생성(레이어 보기) 시 이 함수만 호출하면 됨. 반환: 카테고리별 형상 수.
    extensions: close_open_structures 의 연장 조각([{from,to}]) — 연장한 부분만
    빨간색 'extended' 그룹으로 따로 그려, 구조선을 연장했음을 범례에서 구분한다."""
    # ---------- 5단계: 범례 HTML ----------
    def ent_cat(i, e):
        if i in pruned_idx:
            return "dangling"  # 다각형 강제로 제거된 벽/기둥 선분 — 별도 범례
        # 점선/은선/중심선 = 타층 투영·가상선 → 구조체로 안 봄 (실선만 이 층 실물)
        if e.get("Type") == "Line" and is_dashed_lt(e.get("Linetype")):
            return "dashed"
        ly = e.get("Layer") or "(no-layer)"
        c = cat.get(ly, "other")
        if c == "mixed":
            # 기하 폴백은 구조/비구조를 구분 못하므로 보수적으로 비구조 벽으로
            return "wall_nonstruct" if i in mixed_wall_idx else "other"
        return c

    ys = []
    for e in entities:
        if e["Type"] == "Line":
            ys += [e["Start"][1], e["End"][1]]
        elif e["Type"] == "Polyline":
            ys += [p[1] for p in e["Verts"]]
    max_y = max(ys)

    def fy(y):
        return max_y - y

    paths = collections.defaultdict(list)
    for i, e in enumerate(entities):
        c = ent_cat(i, e)
        t = e["Type"]
        if t == "Line":
            paths[c].append(f'M {e["Start"][0]:.1f} {fy(e["Start"][1]):.1f} '
                            f'L {e["End"][0]:.1f} {fy(e["End"][1]):.1f} ')
        elif t == "Polyline":
            v = e["Verts"] + ([e["Verts"][0]] if e.get("Closed") else [])
            d = "M " + " L ".join(f'{p[0]:.1f} {fy(p[1]):.1f}' for p in v)
            paths[c].append(d + " ")
        elif t == "Arc":
            cx, cy = e["Center"][:2]
            r = e["Radius"]; a0 = e.get("StartAngle", 0); a1 = e.get("EndAngle", 0)
            sx, sy = cx + r * math.cos(a0), cy + r * math.sin(a0)
            ex, ey = cx + r * math.cos(a1), cy + r * math.sin(a1)
            large = 1 if (a1 - a0) % (2 * math.pi) > math.pi else 0
            paths[c].append(f'M {sx:.1f} {fy(sy):.1f} A {r:.1f} {r:.1f} 0 {large} 0 '
                            f'{ex:.1f} {fy(ey):.1f} ')
        elif t == "Circle":
            cx, cy = e["Center"][:2]
            r = e["Radius"]
            paths[c].append(f'M {cx-r:.1f} {fy(cy):.1f} A {r:.1f} {r:.1f} 0 1 0 '
                            f'{cx+r:.1f} {fy(cy):.1f} A {r:.1f} {r:.1f} 0 1 0 '
                            f'{cx-r:.1f} {fy(cy):.1f} ')

    # 연장 조각: 원래 끝점(from)→연장 끝점(to) 을 빨간 'extended' 그룹으로 (원본 위에 덧그림)
    for x in (extensions or []):
        fr, to = x["from"], x["to"]
        paths["extended"].append(
            f'M {fr[0]:.1f} {fy(fr[1]):.1f} L {to[0]:.1f} {fy(to[1]):.1f} ')

    counts = collections.Counter(ent_cat(i, e) for i, e in enumerate(entities)
                                 if e["Type"] in ("Line", "Polyline", "Arc", "Circle"))
    counts["extended"] = len(extensions or [])
    order = ["dashed", "other", "stair", "elevator", "door", "window",
             "column_nonstruct", "column_struct", "wall_nonstruct", "wall_struct",
             "dangling", "extended"]   # dashed=점선 바닥에, extended=연장 맨 위(빨강)
    groups_svg = "\n".join(
        f'<g id="g-{c}"><path d="{"".join(paths[c])}"/></g>' for c in order if paths[c])
    css = "\n".join(
        f'#g-{c} path{{stroke:{CAT_COLOR[c]};'
        f'stroke-width:{2.8 if c == "extended" else 2.5 if c == "dangling" else 2 if c.startswith("wall") else 1.2};'
        f'fill:none;vector-effect:non-scaling-stroke;'
        + ('stroke-dasharray:6 4;' if c == "dashed" else '')
        + '}' for c in CAT_COLOR)
    legend = "".join(
        f'<label><input type="checkbox" data-g="g-{c}" checked/>'
        f'<span style="color:{CAT_COLOR[c]};"> {CAT_KO[c]}({counts.get(c,0)})</span></label> '
        for c in order[::-1] if paths.get(c))

    # 글자(DBText/MText) — PIT·제연휀룸·동통신실 처럼 '텍스트로 공간을 찾는' 도면에 필수.
    # (분류뷰는 기하만 그렸지만 라벨이 있어야 어디가 무슨 방인지 보인다.)
    import html as _html
    texts_svg = []
    for e in entities:
        if e.get("Type") not in ("DBText", "MText"):
            continue
        tx = str(e.get("Text", "")).strip()
        if not tx:
            continue
        p = e.get("Pos") or e.get("InsertionPoint") or [0, 0]
        size = e.get("Height", 200) or 200
        deg = math.degrees(e.get("Rotation", 0.0) or 0.0)
        tr = (f' transform="rotate({-deg:.1f} {p[0]:.1f} {fy(p[1]):.1f})"'
              if abs(deg) > 1e-9 else "")
        texts_svg.append(f'<text x="{p[0]:.1f}" y="{fy(p[1]):.1f}" '
                         f'font-size="{size:.0f}"{tr}>{_html.escape(tx)}</text>')
    texts_group = f'<g id="g-texts">{"".join(texts_svg)}</g>' if texts_svg else ""
    if texts_svg:
        legend += ('<label><input type="checkbox" data-g="g-texts" checked/>'
                   f'<span style="color:#222;"> 글자({len(texts_svg)})</span></label> ')

    rx1, ry1 = ref.get("Start", [0, 0])[:2]
    rx2, ry2 = ref.get("End", [0, 0])[:2]
    xs = []
    for e in entities:
        if e["Type"] == "Line":
            xs += [e["Start"][0], e["End"][0]]
    vb = f"{min(xs):.0f} {fy(max(ys)):.0f} {max(xs)-min(xs):.0f} {max(ys)-min(ys):.0f}"

    page = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"/>
<title>레이어 기반 분류</title>
<style>
html,body{{margin:0;height:100%;font-family:system-ui,sans-serif;}}
#bar{{position:fixed;top:8px;left:8px;z-index:10;background:rgba(255,255,255,.93);
border:1px solid #ccc;border-radius:6px;padding:6px 10px;font-size:13px;max-width:90vw;}}
#bar label{{margin-right:8px;}}
#stage{{width:100vw;height:100vh;background:#fff;cursor:grab;}}
svg{{width:100%;height:100%;display:block;}}
{css}
#g-ref line{{stroke:#e91e8c;stroke-width:4;vector-effect:non-scaling-stroke;}}
#g-texts text{{fill:#111;font-family:"Malgun Gothic",sans-serif;
  paint-order:stroke;stroke:#fff;stroke-width:0.25;}}
</style></head><body>
<div id="bar">{legend}
<div style="color:#555;margin-top:3px;">기준벽 레이어 {ref_layer} · mixed {len(mixed_layers)}개 레이어 기하폴백</div></div>
<div id="stage"><svg id="svg" viewBox="{vb}" xmlns="http://www.w3.org/2000/svg">
{groups_svg}
{texts_group}
<g id="g-ref"><line x1="{rx1:.1f}" y1="{fy(ry1):.1f}" x2="{rx2:.1f}" y2="{fy(ry2):.1f}"/></g>
</svg></div>
<script>
(function(){{
var svg=document.getElementById('svg'),stage=document.getElementById('stage');
var p=svg.getAttribute('viewBox').split(' ').map(Number);
var vb={{x:p[0],y:p[1],w:p[2],h:p[3]}};
function ap(){{svg.setAttribute('viewBox',vb.x+' '+vb.y+' '+vb.w+' '+vb.h);}}
stage.addEventListener('wheel',function(e){{e.preventDefault();
var r=svg.getBoundingClientRect();
var px=vb.x+(e.clientX-r.left)/r.width*vb.w,py=vb.y+(e.clientY-r.top)/r.height*vb.h;
var f=e.deltaY>0?1.15:1/1.15;vb.w*=f;vb.h*=f;
vb.x=px-(e.clientX-r.left)/r.width*vb.w;vb.y=py-(e.clientY-r.top)/r.height*vb.h;ap();}},{{passive:false}});
var drag=false,lx,ly;
stage.addEventListener('mousedown',function(e){{drag=true;lx=e.clientX;ly=e.clientY;}});
window.addEventListener('mousemove',function(e){{if(!drag)return;var r=svg.getBoundingClientRect();
vb.x-=(e.clientX-lx)/r.width*vb.w;vb.y-=(e.clientY-ly)/r.height*vb.h;lx=e.clientX;ly=e.clientY;ap();}});
window.addEventListener('mouseup',function(){{drag=false;}});
document.querySelectorAll('#bar input').forEach(function(cb){{
cb.onchange=function(){{var g=document.getElementById(cb.dataset.g);
if(g)g.style.display=cb.checked?'':'none';}};}});
}})();
</script></body></html>"""

    with open(output, "w", encoding="utf-8") as f:
        f.write(page)
    return counts


if __name__ == "__main__":
    main()
