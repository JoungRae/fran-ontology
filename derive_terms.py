"""
BOT/CAD 데이터 → cons_law 표준 온톨로지 용어(ontology_terms) 값 도출.

갭 분석에서 확인된 최대 병목을 채운다:
  - 면적 산정: 층 footprint(방 사각형 합집합) → 층면적·건축면적·연면적
  - 폭 근사: 공용홀(ELEV.홀) 짧은 변 → 홀/복도 유효폭, 문 스윙호 반경 → 문폭
  - 기존 값: 층수·높이·세대수·방면적·대피공간(발코니)

출력: output/derived_terms.json
  { "terms": {term_id: value}, "provenance": {term_id: 설명},
    "floors": {층: 면적}, ... }

주의: 전부 직사각형 근사·모델 확장 기반 — 리포트에 출처 명시됨.
"""

import collections
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FO = os.path.dirname(os.path.abspath(__file__))
SECTION_JSON = (r"D:/Python_test/fran_consist_cad_json/output/"
                r"A1-131~144 단위세대 주단면도(A5BL)_20260613_111427_section.json")

# 층 구성: (base, 반복 층수) — 기준층 도면은 2~15층 14개층을 대표
STACK = [("지하1층", 1), ("1층", 1), ("기준층", 14)]
ABOVE_REPEAT = {"1층": 1, "기준층": 14}   # 지상층만 (건축면적·용적률용)


def union_area_mm2(rects):
    """축정렬 사각형들의 합집합 면적 (좌표압축 스윕)."""
    if not rects:
        return 0
    xs = sorted({r[0] for r in rects} | {r[2] for r in rects})
    total = 0
    for i in range(len(xs) - 1):
        x0, x1 = xs[i], xs[i + 1]
        w = x1 - x0
        if w <= 0:
            continue
        ivs = sorted((r[1], r[3]) for r in rects if r[0] <= x0 and r[2] >= x1)
        cov = 0
        cur_lo = cur_hi = None
        for lo, hi in ivs:
            if cur_hi is None:
                cur_lo, cur_hi = lo, hi
            elif lo <= cur_hi:
                cur_hi = max(cur_hi, hi)
            else:
                cov += cur_hi - cur_lo
                cur_lo, cur_hi = lo, hi
        if cur_hi is not None:
            cov += cur_hi - cur_lo
        total += w * cov
    return total


def load_rooms(base):
    p = os.path.join(FO, "output", f"{base}_rooms_rect.json")
    return json.load(open(p, encoding="utf-8"))["rooms"]


def door_widths(base, near=None):
    """도어 레이어 스윙호 반경(=문짝 폭, mm). near=(rect리스트)면 그 안의 호만."""
    d = json.load(open(os.path.join(FO, "data", f"{base}.json"), encoding="utf-8"))
    cls = json.load(open(os.path.join(FO, "output", f"{base}_layer_classification.json"),
                         encoding="utf-8"))["categories"]
    door_layers = {ly for ly, c in cls.items() if c == "door"}
    out = []
    for e in d["Entities"]:
        if e.get("Type") != "Arc" or e.get("Layer") not in door_layers:
            continue
        r = e.get("Radius", 0)
        if not (300 <= r <= 1500):
            continue
        if near is not None:
            cx, cy = e["Center"][:2]
            pad = 300
            if not any(x0 - pad <= cx <= x1 + pad and y0 - pad <= cy <= y1 + pad
                       for x0, y0, x1, y1 in near):
                continue
        out.append(round(r))
    return out


def main():
    terms = {}
    prov = {}

    # ---------- 층 footprint 면적 ----------
    floor_area = {}
    for base, _rep in STACK:
        rooms = load_rooms(base)
        rects = [r["rect"] for r in rooms]
        a = union_area_mm2(rects) / 1e6  # m2
        floor_area[base] = round(a, 1)
    gfa = sum(floor_area[b] * rep for b, rep in STACK)
    gfa_above = sum(floor_area[b] * rep for b, rep in ABOVE_REPEAT.items())
    terms["building.gross_floor_area_m2"] = round(gfa, 1)
    prov["building.gross_floor_area_m2"] = (
        f"층 footprint 합산(지하1층 {floor_area['지하1층']} + 1층 {floor_area['1층']} "
        f"+ 기준층 {floor_area['기준층']}×14) — 방 사각형 합집합 근사(코어·벽 제외분 과소)")
    terms["building.ground_floors_gross_area_m2"] = round(gfa_above, 1)
    prov["building.ground_floors_gross_area_m2"] = "지상층 연면적(용적률 산정용 근사)"
    terms["building.building_area_m2"] = floor_area["1층"]
    prov["building.building_area_m2"] = "1층 footprint(방 사각형 합집합) — 건축면적 근사"
    terms["floor.area_m2"] = max(floor_area.values())
    prov["floor.area_m2"] = f"최대 층 바닥면적(층별: {floor_area})"

    # ---------- 층수·높이 (단면도) ----------
    if os.path.exists(SECTION_JSON):
        floors = json.load(open(SECTION_JSON, encoding="utf-8")).get("floors", [])
        above = below = 0
        hsum = 0
        heights = []
        for fl in floors:
            name = str(fl.get("name", ""))
            h = fl.get("floor_height_mm") or 0
            m = re.search(r"(\d+)\s*~\s*(\d+)\s*층", name)
            n = (int(m.group(2)) - int(m.group(1)) + 1) if m else (
                1 if re.search(r"\d+\s*층", name) and "지하" not in name else 0)
            if "지하" in name:
                below += 1
            elif n:
                above += n
                hsum += h * n
                heights.append(h)
        terms["building.floors_above_count"] = above
        terms["building.floors_below_count"] = below
        terms["building.height_m"] = round(hsum / 1000, 1)
        terms["floor.height_m"] = round(min(heights) / 1000, 2) if heights else None
        prov["building.floors_above_count"] = "단면도 층 밴드"
        prov["building.floors_below_count"] = "단면도 층 밴드"
        prov["building.height_m"] = "단면도 층고 합산"
        prov["floor.height_m"] = "단면도 최소 층고"

    # ---------- 세대수 (모델 확장: 층당 현관 수 × 반복) ----------
    hh = 0
    for base, rep in STACK:
        ents = sum(1 for r in load_rooms(base) if "현관" in r["room"])
        hh += ents * rep
    terms["project.household_count"] = hh
    prov["project.household_count"] = "층당 현관 수 × 층 반복(1층 8 + 기준층 8×14)"
    terms["project.use"] = "공동주택"
    prov["project.use"] = "주동평면도·세대타입(55A/55AS)"

    # ---------- 방 면적 (거실·침실 최소 / 욕실 / 대피공간=발코니) ----------
    def min_area(bases, kw):
        vals = []
        for b in bases:
            for r in load_rooms(b):
                if kw in r["room"]:
                    vals.append(r["w_mm"] * r["h_mm"] / 1e6)
        return round(min(vals), 2) if vals else None

    resi = ("1층", "기준층")
    habit = []
    for b in resi:
        for r in load_rooms(b):
            if any(k in r["room"] for k in ("침실", "거실")):
                habit.append(r["w_mm"] * r["h_mm"] / 1e6)
    terms["space.room.floor_area_m2"] = round(min(habit), 2) if habit else None
    prov["space.room.floor_area_m2"] = "거실·침실 최소 면적(직사각형 근사)"
    terms["space.room.bathroom_area_m2"] = min_area(resi, "욕실")
    prov["space.room.bathroom_area_m2"] = "욕실 최소 면적(근사)"
    terms["space.room.living_dining_area_m2"] = min_area(resi, "거실")
    prov["space.room.living_dining_area_m2"] = "거실 최소 면적(근사)"
    ev = min_area(resi, "발코니")
    terms["space.evacuation.area_m2"] = ev
    terms["space.evacuation.provided"] = bool(ev)
    prov["space.evacuation.area_m2"] = "발코니 최소 면적(대피공간 추정, 근사)"
    prov["space.evacuation.provided"] = "발코니 존재 여부"

    # ---------- 세대당 전용면적 (주택법 §2 국민주택규모 등 정의의 기준값) ----------
    # 전용면적 = 공용부(홀·계단·복도 등)·서비스면적 제외한 세대 내 실 합.
    # 발코니(노대)는 건축법 시행령 §119①3나: 접한 외벽(긴 변)×1.5m 까지만 제외,
    #   초과분은 바닥면적 산입 → max(0, 면적 − 긴변×1.5m) 를 전용면적에 더한다.
    # 실명이 세대당 1회씩 나타나므로 '실명별 평균 면적의 합' ≈ 1세대 전용면적.
    EXCL_KW = ("실외기", "홀", "계단", "복도", "로비", "ELEV", "EPS", "TPS", "PIT")
    BALCONY_FREE_DEPTH_MM = 1500.0
    per_floor = []
    balc_note = ""
    for b in resi:
        by_name = {}
        balc_excess = []
        for r in load_rooms(b):
            name = r["room"]
            if "발코니" in name:
                w, h = r["w_mm"], r["h_mm"]
                ex = max(0.0, w * h - max(w, h) * BALCONY_FREE_DEPTH_MM) / 1e6
                balc_excess.append(ex)
                continue
            if any(k in name for k in EXCL_KW):
                continue
            by_name.setdefault(name, []).append(r["w_mm"] * r["h_mm"] / 1e6)
        if by_name:
            total = sum(sum(v) / len(v) for v in by_name.values())
            if balc_excess:
                mean_ex = sum(balc_excess) / len(balc_excess)
                total += mean_ex
                balc_note = (f" + 발코니 1.5m 초과 산입분 {mean_ex:.2f}㎡"
                             f"(영 §119①3나, 깊이 1.5m 초과 시)")
            per_floor.append(total)
    if per_floor:
        excl = round(min(per_floor), 2)
        terms["space.exclusive_area_per_household_m2"] = excl
        prov["space.exclusive_area_per_household_m2"] = (
            "실명별 평균 면적 합(공용부·서비스면적 제외, 직사각형 근사)"
            + balc_note + " — 세대당 전용면적")
        terms["building.exclusive_area_sum_m2"] = round(excl * hh, 1)
        prov["building.exclusive_area_sum_m2"] = f"세대당 전용면적 × 세대수({hh})"

    # ---------- 폭: 공용홀(ELEV.홀) 짧은 변 ----------
    hall_w = []
    for b in ("지하1층", "기준층"):
        for r in load_rooms(b):
            if "홀" in r["room"]:
                hall_w.append(min(r["w_mm"], r["h_mm"]))
    if hall_w:
        terms["space.corridor.clear_width_m"] = round(min(hall_w) / 1000, 2)
        prov["space.corridor.clear_width_m"] = (
            f"공용홀(ELEV.홀) 최소 짧은 변 {min(hall_w)}mm — 복도 유효폭 근사(전용 복도 없음)")

    # ---------- 문폭: 스윙호 반경 ----------
    all_w = door_widths("1층") + door_widths("기준층")
    if all_w:
        terms["element.door.clear_width_m"] = round(min(all_w) / 1000, 2)
        prov["element.door.clear_width_m"] = (
            f"문 스윙호 반경 최소 {min(all_w)}mm (호 {len(all_w)}개, 욕실문 포함)")
    ent_rects = [tuple(r["rect"]) for r in load_rooms("1층") if "현관" in r["room"]]
    ent_w = door_widths("1층", near=ent_rects)
    if ent_w:
        terms["element.door.entrance_door_width_m"] = round(max(ent_w) / 1000, 2)
        prov["element.door.entrance_door_width_m"] = (
            f"현관 영역 스윙호 반경 {sorted(set(ent_w))}mm 중 최대(세대 출입문)")

    # ---------- 존재/개수 ----------
    terms["element.elevator.provided"] = True
    prov["element.elevator.provided"] = "레이어 분류 elevator + ELEV.홀"
    # 승강기 대수: flood-fill이 인식한 샤프트(ELEV. bbox 영역) 수 우선,
    # 없으면 ELEV.홀 수(코어당 1대 가정) 폴백
    k_rooms = load_rooms("기준층")
    shafts = sum(1 for r in k_rooms if r["room"] == "ELEV.")
    halls = sum(1 for r in k_rooms if "ELEV" in r["room"].upper() and r["room"] != "ELEV.")
    n_elev = shafts or halls
    if n_elev:
        terms["element.elevator.count"] = n_elev
        prov["element.elevator.count"] = (
            f"승강기 샤프트(A-ELEV bbox) {shafts}개" if shafts else
            f"ELEV.홀 {halls}개 → 코어당 1대 가정") + \
            " — 비상용/화물용 구분은 도면만으로 불가"
    terms["element.door.count"] = len(all_w)
    prov["element.door.count"] = "문 스윙호 수(1층+기준층)"

    # ---------- 위상 사실(topology_facts, build_building 산출) 유래 ----------
    tf = os.path.join(FO, "output", "topology_facts.json")
    if os.path.exists(tf):
        facts = json.load(open(tf, encoding="utf-8"))
        tv = facts.get("stair_travel_max_m")
        if tv:
            terms["space.evacuation.travel_distance_to_direct_stair_m"] = tv
            w = facts.get("stair_travel_worst") or {}
            prov["space.evacuation.travel_distance_to_direct_stair_m"] = (
                f"BOT 문/개방 연결 그래프 최단경로 최악값({w.get('floor','')} "
                f"{w.get('room','')}) — 실 중심 경유+대각/2 보정, 근사")
        ew = facts.get("entrance_door_min_width_m")
        if ew:
            terms["element.door.entrance_door_width_m"] = ew
            prov["element.door.entrance_door_width_m"] = (
                "현관에 접한 문(Interface) 최근접 스윙호 반경 최소값 — 세대 출입문 폭")

    out = {"terms": {k: v for k, v in terms.items() if v is not None},
           "provenance": prov,
           "floor_area_by_storey": floor_area}
    op = os.path.join(FO, "output", "derived_terms.json")
    json.dump(out, open(op, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"도출 term {len(out['terms'])}개 → {op}")
    for k, v in out["terms"].items():
        print(f"  {k:44} = {v}")
    print("층별 footprint:", floor_area)


if __name__ == "__main__":
    main()
