"""
소방설비 적법 배치 제안 v2 — 생성기(삼각 격자) + 검증기(래스터 거리장) + 수리기(greedy).

v1(방별 정방 격자·방중심 보행거리) 대비:
  · 헤드: 삼각(지그재그) 격자 = 원 커버링 최적 배열 → 이후 100mm 셀 단위로
    전 보행가능 지점 커버를 '검증'하고, 미커버 최원점에 greedy 추가(수리) → 커버 보증
  · 창문 0.6m 헤드(NFPC 608 §7, 체크 15798) 반영: 외벽 창 구간마다 0.6m 안쪽 배치,
    창 전체가 헤드 수평거리에 들도록 5.06m(=2√(2.6²−0.6²)) 간격 분할
  · 보행거리: 래스터 거리장(문=통행, 벽·기둥=차단) Dijkstra — 규정 문구 그대로
    '각 부분으로부터' 셀 단위 계산 + 히트맵. 세대 내 미라벨 통로도 자동 포함
  · 피난 동선: 거리장 경사 하강 = 실제 개구부를 지나는 경로
  · 소화기: 배치 후 거리장으로 20m 커버를 셀 단위 재검증

사용법: python fire_layout.py [기준층]   (numpy·matplotlib venv — fran_consist venv)
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

import build_bot as BB
import fire_field as FF

def load_legal_ttl(path):
    """온톨로지 법령 레이어(_legal.ttl) → 실명별 판정 dict (bindings JSON 과 동형).

    annotate_legal.py 가 만든 fran:verdict 트리플을 되읽는다. 동명 실(코어 대칭)의
    판정은 실명 단위 매칭 결과라 동일하므로 실명당 하나로 접는다.
    """
    from rdflib import Graph, Namespace
    from rdflib.namespace import RDF, RDFS
    BOTNS = Namespace("https://w3id.org/bot#")
    FR = Namespace("https://example.org/fran#")
    g = Graph()
    g.parse(path, format="turtle")
    out = {}
    for sp in g.subjects(RDF.type, BOTNS.Space):
        name = next((str(o) for o in g.objects(sp, RDFS.label)), None)
        v = next(iter(g.objects(sp, FR.verdict)), None)
        if not name or v is None or name in out:
            continue
        basis = []
        for bn in g.objects(v, FR.basis):
            order = next((int(o) for o in g.objects(bn, FR.order)), 0)
            ru = next(iter(g.objects(bn, FR.rule)), None)
            src = next((str(o) for o in g.objects(ru if ru is not None else bn,
                                                  RDFS.label)), "")
            rid = None
            if ru is not None:
                tail = str(ru).rsplit("Rule_", 1)
                rid = int(tail[1]) if len(tail) == 2 and tail[1].isdigit() else None
            basis.append((order, {"출처": src, "rule_id": rid}))
        def _s(p):
            return next((str(o) for o in g.objects(v, p)), "")
        out[name] = {
            "결정규칙": sorted(
                ({"출처": next((str(l) for l in g.objects(d, RDFS.label)), ""),
                  "rule_id": int(str(d).rsplit("Rule_", 1)[1])}
                 for d in g.objects(v, FR.appliedRule) if "Rule_" in str(d)),
                key=lambda x: x["rule_id"]),
            "기본동작": _s(FR.action), "출처": _s(FR.source),
            "confidence": _s(FR.confidence), "노드": _s(FR.legalNode),
            "이유": _s(FR.reason),
            "확인필요": _s(FR.needsReview) == "true",
            "근거": [b for _, b in sorted(basis, key=lambda t: t[0])],
            "생략가능": sorted(
                [{"출처": next((str(l) for l in g.objects(ru, RDFS.label)), ""),
                  "rule_id": int(str(ru).rsplit("Rule_", 1)[1])}
                 for ru in g.objects(v, FR.omittableRule)],
                key=lambda d: d["rule_id"]) +
                sorted(str(o) for o in g.objects(v, FR.omittableBasis)),
        }
    return out


# 법정 수치는 head_params.json(법령DB 유래)에서 온다 — 아래는 그 파일이 없을
# 때만 쓰는 폴백이고, 화면 파라미터 패널에 '하드코딩' 으로 표시된다.
# main() 이 같은 이름의 지역변수로 덮어쓴다.
FALLBACK = {
    "r_unit": 2600.0,      # 세대 내 헤드 수평거리 (NFPC 608 §7)
    "r_common": 2300.0,    # 공용부(내화구조) 헤드 수평거리 (NFPC 103 §10)
    "window_band": 600.0,  # 창문 이격 (NFPC 608 §7)
    "ext_limit": 20000.0,  # 소화기 보행거리 (NFPC 101 §4)
    "outlet_r": 5000.0,    # 방수구·비상콘센트 계단 출입구 이격 (NFPC 608 §17·18)
}

# 법적 근거 표. 조문 출처·원문은 **법령DB에서 온다** — head_params.json 의
# 파라미터마다 rule_id·근거·원문이 붙어 있으므로 그걸 읽는다. 코드에 남는 건
# (제목, 파라미터 key, 이 엔진이 무엇을 했는가) 뿐이다. 적용·검증 서술은 법이
# 아니라 우리 구현에 대한 설명이라 코드에 있는 게 맞다.
#
# LAW_SPEC: (제목, head_params key 또는 None, 적용·검증)
# key 가 None 인 항목은 DB 수집 범위(NFPC/NFTC 103·608) 밖의 법령이거나
# 엔진 서술이라 아래 LAW_FALLBACK 의 (출처, 조문)을 쓰고 출처를 명시한다.
LAW_SPEC = [
    ("헤드 수평거리 — 세대 내 2.6m", "r_unit",
     "주거실로 확인되는 실에만 2.6m 적용. 배치 직후 100mm 셀 전수 검증(수평거리+가시선)으로 "
     "미커버 0을 확인."),
    ("헤드 — 외벽 창문 0.6m 이내", "window_band",
     "창 구간을 5.06m(=2√(2.6²−0.6²)) 이하로 분할해 각 구간 중앙 0.6m 안쪽에 배치. "
     "세대 실 전용 규정 — 창이 없는 층은 0개."),
    ("헤드 수평거리 — 공용부 2.3m (내화구조)", "r_common",
     "본 건물 내화구조 → 2.3m 적용. 주거실로 확인되지 않는 모든 실(부대시설·무명실 포함)에 "
     "보수적으로 적용."),
    ("살수장애물 이격 — 반경 0.6m / 폭 3배", "clear_head",
     "구조도 정합 보·거더 축선 0.6m 대역을 설치 후보에서 제외. 기하적으로 회피 불가한 "
     "헤드만 ⚠ 표시 — 반사판 하향/차폐판 검토 대상."),
]

# DB 범위 밖 법령 + 엔진 서술 (제목, 출처, 조문, 적용·검증, 출처구분)
LAW_EXTRA = [
    ("소화기 (소형)",
     "소화기구 및 자동소화장치의 화재안전성능기준(NFPC 101) 제4조 · NFTC 101 — 체크 15704·16144",
     "특정소방대상물의 각 부분으로부터 1개의 소화기까지의 보행거리가 소형소화기의 경우에는 "
     "20미터 이내가 되도록 배치할 것.",
     "배치 후 래스터 보행 거리장(문=통행·벽=차단)으로 전 지점 20m 커버를 재검증."),
    ("방수구 · 비상콘센트",
     "공동주택의 화재안전성능기준(NFPC 608) 제17조·제18조 — 체크 15810·15811·15785",
     "계단의 출입구로부터 5미터 이내에 방수구(제17조)·비상콘센트(제18조)를 설치하되, 해당 "
     "층의 각 부분까지의 수평거리가 50미터를 초과하는 경우 추가 설치.",
     "계단 출입구 = BOT 위상(계단-홀 경계 중점)에서 산출, 점선원 = 5m 허용 범위."),
    ("피난 동선 · 보행거리",
     "건축법 시행령 제34조 — 체크 11388 (16층 이상 공동주택 40m: 체크 11390)",
     "거실의 각 부분으로부터 직통계단에 이르는 보행거리가 30미터 이하.",
     "100mm 셀 보행 거리장(8방향 다익스트라)으로 전수 계산. 동선 = 거리장 경사 하강 경로."),
    ("한계 · 미반영",
     "NFTC 103 2.7.7(반사판-보 수직거리 표) 외",
     "반자·천장고 조건, 덕트·선반 등 설비 장애물, 경사천장 보정은 해당 데이터가 도면에 "
     "없어 검증 범위 밖.",
     "보 춤(900mm)은 확보 — 천장고 데이터 추가 시 반사판-보 하단 수직거리 표 검증으로 확장 "
     "예정. 발코니 분합창은 통행 가능으로 근사, 지오메트리는 100mm 래스터 근사."),
]

# LAW_SPEC 의 key 를 DB 에서 못 찾았을 때 쓰는 조문(출처, 원문). 코드에 남은
# 마지막 리터럴이라, 이게 쓰이면 화면에 '하드코딩' 으로 표시된다.
LAW_FALLBACK = {
    "r_unit": ("공동주택의 화재안전성능기준(NFPC 608) 제7조 · NFTC 608 2.3.1.4",
               "아파트등의 세대 내 스프링클러헤드를 설치하는 경우 각 부분으로부터 하나의 "
               "스프링클러헤드까지의 수평거리는 2.6미터 이하로 할 것."),
    "window_band": ("공동주택의 화재안전성능기준(NFPC 608) 제7조",
                    "외벽에 설치된 창문에서 0.6미터 이내에 스프링클러헤드를 배치할 것."),
    "r_common": ("스프링클러설비의 화재안전성능기준(NFPC 103) 제10조",
                 "스프링클러헤드까지의 수평거리는 2.1미터 이하로 해야 한다. 내화구조로 "
                 "된 특정소방대상물의 경우에는 2.3미터 이하."),
    "clear_head": ("공동주택의 화재안전성능기준(NFPC 608) 제7조",
                   "헤드와 장애물 사이에 60센티미터 반경을 확보하거나 장애물 폭의 3배를 "
                   "확보할 것."),
}


def build_legal(head_params):
    """법적 근거 표를 만든다 → [(제목, 출처, 조문, 적용·검증, 출처구분)].

    조문·출처는 법령DB(head_params) 우선. 못 찾으면 LAW_FALLBACK 리터럴을 쓰되
    출처구분을 '하드코딩' 으로 남겨 화면에서 구분되게 한다.
    """
    byk = {p.get("key"): p for p in (head_params or {}).get("파라미터", [])}
    rows = []
    for title, key, applied in LAW_SPEC:
        p = byk.get(key)
        if p and p.get("근거") and p.get("원문"):
            rows.append((title, p["근거"], p["원문"], applied, "법령DB"))
        else:
            cite, law = LAW_FALLBACK[key]
            rows.append((title, cite, law, applied, "하드코딩"))
    for title, cite, law, applied in LAW_EXTRA:
        # 소화기·방수구·피난동선은 엄연히 법령이다 — 다만 규칙 추출 대상
        # 문서(NFPC/NFTC 103·608) 밖이라 DB 에 없다. '엔진' 으로 표시하면 거짓말.
        kind = "엔진" if title.startswith("한계") else "법령(DB 범위 밖)"
        rows.append((title, cite, law, applied, kind))
    return rows


def main():
    argv = list(sys.argv[1:])
    heads_only = "--heads" in argv

    # 헤드 수평거리 조절 (성능형/확대살수형 헤드 대응): --r-unit 3.2 --r-common 2.6
    # 값은 m(예: 2.6) 또는 mm(예: 2600) 모두 허용. 기본 = 법정 기준값.
    def _optf(name, default):
        if name in argv:
            i = argv.index(name)
            try:
                v = float(argv[i + 1])
            except (IndexError, ValueError):
                return default
            del argv[i:i + 2]
            return v * 1000.0 if v < 100 else v
        return default

    # 배치 파라미터는 derive_head_params.py 가 법령DB(legal_rule)에서 뽑아 둔
    # output/head_params.json 을 우선한다. 건물 설정(내화 여부·용도)이 값을
    # 정한다 — 내화면 2.3, 비내화면 2.1, 무대부면 1.7. 파일이 없으면 예전
    # 하드코딩 기본값으로 물러나되, 화면 파라미터 패널에 '하드코딩' 으로 찍힌다.
    _fo = os.path.dirname(os.path.abspath(__file__))
    _pp = os.path.join(_fo, "output", "head_params.json")
    HEAD_PARAMS = None
    if os.path.exists(_pp):
        try:
            HEAD_PARAMS = json.load(open(_pp, encoding="utf-8"))
        except Exception as e:
            print(f"head_params.json 읽기 실패({e}) — 하드코딩 값으로 진행")

    # 법적 근거 표 — 조문·출처는 법령DB(head_params)에서 온다
    LEGAL = build_legal(HEAD_PARAMS)
    _lit = [t for t, _, _, _, k in LEGAL if k == "하드코딩"]
    if _lit:
        print(f"조문을 법령DB에서 못 찾아 코드 리터럴 사용: {', '.join(_lit)}")

    _fallback_used = []

    def _law_mm(key, default=None):
        if default is None:
            default = FALLBACK[key]
        if HEAD_PARAMS:
            for prm in HEAD_PARAMS.get("파라미터", []):
                if prm.get("key") == key and prm.get("값_mm"):
                    return float(prm["값_mm"])
        if key not in _fallback_used:
            _fallback_used.append(key)
        return default

    R_UNIT = _optf("--r-unit", _law_mm("r_unit"))
    R_COMMON = _optf("--r-common", _law_mm("r_common"))
    R_REPAIR = R_COMMON                     # 안전망은 공용 반경으로 보수적
    WIN_OFF = _law_mm("window_band")
    EXT_LIMIT = _law_mm("ext_limit")
    OUTLET_R = _law_mm("outlet_r")
    r_custom = (R_UNIT != _law_mm("r_unit")) or (R_COMMON != _law_mm("r_common"))
    if _fallback_used:
        print(f"법령DB에 없어 폴백 상수 사용: {', '.join(_fallback_used)} "
              f"(화면에 '하드코딩' 표시)")
    if HEAD_PARAMS:
        _prof = HEAD_PARAMS.get("프로필", {})
        print(f"법령 파라미터 적용: {_prof.get('이름','')} · "
              f"{_prof.get('용도','')} · {_prof.get('구조','')} → "
              f"세대 {R_UNIT/1000:.1f}m / 공용 {R_COMMON/1000:.1f}m")
    if r_custom:
        print(f"수평거리 사용자 지정: 세대 {R_UNIT/1000:.2f}m · 공용 {R_COMMON/1000:.2f}m "
              f"(법정 기준 2.6/2.3 — 상회 시 성능 인정 헤드 사용 전제)")

    base = next((a for a in argv if not a.startswith("--")), "기준층")
    FO = os.path.dirname(os.path.abspath(__file__))
    rooms_data = json.load(open(os.path.join(FO, "output", f"{base}_rooms_rect.json"),
                                encoding="utf-8"))["rooms"]
    cats = json.load(open(os.path.join(FO, "output",
                                       f"{base}_layer_classification.json"),
                          encoding="utf-8"))["categories"]
    ents = json.load(open(os.path.join(FO, "data", f"{base}.json"),
                          encoding="utf-8"))["Entities"]

    # 실 분류는 온톨로지 법령 레이어(_legal.ttl — annotate_legal.py)가 정본이다.
    # 규칙×실 매칭(match_rules_rooms.py) 판정이 fran:verdict 로 실 노드에 달려
    # 있고, 세대/공용/제외가 법령 근거와 함께 온다. TTL 이 없으면 예전 바인딩
    # JSON, 그마저 없는 실만 옛 키워드(is_resi)로 물러나고 '하드코딩' 표시.
    _lt = os.path.join(FO, "output", f"{base}_legal.ttl")
    _bp = os.path.join(FO, "output", f"{base}_room_bindings.json")
    BINDINGS = {}
    if os.path.exists(_lt):
        try:
            BINDINGS = load_legal_ttl(_lt)
            _bsrc = "온톨로지 법령 레이어"
        except ImportError:   # rdflib 없는 환경 — 판정 자체는 JSON 으로 살린다
            print("rdflib 없음 — 법령 TTL 대신 바인딩 JSON 사용 (pip install rdflib)")
    if not BINDINGS and os.path.exists(_bp):
        BINDINGS = json.load(open(_bp, encoding="utf-8")).get("바인딩", {})
        _bsrc = "바인딩 JSON"
    if BINDINGS:
        print(f"실 바인딩 적용({_bsrc}): {len(BINDINGS)}개 실명 "
              f"(확인필요 {sum(1 for b in BINDINGS.values() if b.get('확인필요'))})")

    # ── 사람 확정 — ⚠확인필요를 리포트의 "확정" 카드에서 사람이 제외/설치로
    # 결정한 것(output/<base>_room_decisions.json, fire_server /decide 가 씀).
    # LLM 판정 위에 덮는 마지막 층이다: 확정된 실은 ⚠ 가 꺼지고 출처가
    # '사람'(초록 배지)이 된다. 바인딩·TTL 은 건드리지 않는다 — LLM 판정과
    # 사람 결정을 한 파일에 섞으면 재매칭 때 사람 결정이 지워진다.
    _dp = os.path.join(FO, "output", f"{base}_room_decisions.json")
    DECISIONS = {}
    if os.path.exists(_dp):
        DECISIONS = {k: v for k, v in
                     json.load(open(_dp, encoding="utf-8")).items()
                     if v in ("제외", "설치")}
    # 확정 카드에 실을 목록: 지금 ⚠확인필요인 실 + 이미 결정된 실(번복 가능하게)
    PENDING = sorted({n for n, b in BINDINGS.items() if b.get("확인필요")}
                     | set(DECISIONS))
    for _n, _act in DECISIONS.items():
        _b = BINDINGS.setdefault(_n, {})
        if _act == "제외":
            _b["기본동작"] = "제외"
            _b["반경_mm"] = None
        elif _b.get("기본동작") in (None, "", "제외"):
            _b["기본동작"] = "일반(공용 반경)"
        _b["확인필요"] = False
        _b["출처"] = "사람"
    if DECISIONS:
        print(f"사람 확정 적용: {len(DECISIONS)}건 "
              f"(제외 {sum(1 for v in DECISIONS.values() if v == '제외')}"
              f" · 설치 {sum(1 for v in DECISIONS.values() if v == '설치')})")

    def room_class(name):
        """(세대여부, 제외여부, 출처) — 바인딩 우선, 없으면 키워드 폴백."""
        b = BINDINGS.get(name)
        if b:
            act = b.get("기본동작", "")
            return (act == "세대 반경 2.6m", act == "제외",
                    b.get("출처", "LLM"))
        # 폴백 — 바인딩 없는 실. 세대 반경(2.6m)은 세대가 있는 층에서만 준다.
        # 실명 키워드만 믿으면 지하 부대시설층의 '대피공간' 이 주거실로 잡혀
        # 세대 기준이 켜진다(층에 세대가 없는데도).
        floor_has_unit = (HEAD_PARAMS or {}).get("사실", {}).get("세대있음")
        unit = BB.is_resi(name) and floor_has_unit is not False
        return (unit, any(k in name.upper() for k in ("PIT", "피트")), "하드코딩")

    rooms = [{"id": i, "name": r["room"], "rect": r["rect"], "poly": r.get("poly"),
              "common": not room_class(r["room"])[0]}
             for i, r in enumerate(rooms_data)]

    def cen(r):
        x0, y0, x1, y1 = r["rect"]
        return ((x0 + x1) / 2, (y0 + y1) / 2)

    # ---- 세그먼트 수집 ----
    # 비출력(Plot=false) 레이어는 물리 요소가 아님(A-MC 몰딩 보조선 등) — 차단 벽에서
    # 제외. 현재 층 파일에 Plot 정보가 없으면 1층.json 의 레이어 표준을 폴백으로 사용.
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

    # 문 개구부 천공 선분: 스윙호의 경첩→양 끝점 현(닫힘 위치 문짝 = 출입구 그 자체).
    # 이 도면은 문 위치에서 벽선이 끊기지 않아 래스터 통행로를 직접 뚫어야 한다.
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

    # ---- 격자: 통행 차단 = 벽·기둥 (창은 분합창 근사로 통행 허용,
    #      외부 밀폐 판정은 벽+창+문 모두 포함) ----
    # 천공 = 스윙호 현 + 문 레이어 선분 전체(방화문·문틀은 스윙호 없이 선분만으로
    # 그려짐 — 문 도형은 개구부 위에만 있으므로 벽 천공에 안전)
    grid = FF.build_grid(wall_walk, win_segs + door_segs, bounds,
                         carve_segs=carve + door_segs)
    # 실 내부 마스크 — '각 부분' 평가는 실 내부 셀 기준 (벽 공동·창호선 틈새 등
    # 도면상 가짜 공간을 통계·헤드 수리에서 배제).
    # 헤드 제외는 **법령 판정(바인딩)이 정한다**. 코드가 실명을 보고 최종 결정을
    # 내리던 자리였는데(대피공간), 그러면 근거가 그래프에서 사라지고 "대피공간2"·
    # "피난대기공간" 같은 표기 차이에 판정이 흔들린다.
    #
    # 바인딩 없는 실의 폴백은 **보수적으로** 설치 쪽이다. 법령 제외장소 원문에
    # 실명이 나온다고 빼면 안 된다 — 2.12.1 은 "설치하지 않을 수 있다"(임의)이고,
    # 실제로 뺄지는 정책(비출입 구획만)이 정하기 때문이다. 원문 매칭으로 빼 봤더니
    # 계단·창고까지 제외돼 헤드가 74→68로 줄었다. 정책 판단 없이 뺄 수 있는 건
    # 도면 관용 표기상 사람이 들어가지 않는 구획(PIT·피트)뿐이다.
    PIT_KW = ("PIT", "피트")

    def eval_room(r):
        b = BINDINGS.get(r["name"])
        if b:
            return b.get("기본동작") != "제외"
        return not any(k in r["name"].upper() for k in PIT_KW)

    # 실 마스크: poly(실제 형상) 우선 — rect(외접 사각형)는 ㄷ자·코너 실에서 벽 너머
    # bbox 초과분까지 '커버해야 할 셀'로 요구해 과잉 설치를 만든다. poly 는 래스터
    # 팽창 탓에 벽보다 ~200mm 안쪽이므로, '보행가능 셀 한정' 팽창(측지) 2회로
    # 폴리곤-벽 사이 띠만 흡수 — 벽(비보행)을 넘어 옆 공간으로 새지 않는다.
    from matplotlib.path import Path as _MPath

    def room_mask_of(r):
        # 래스터 창: 폴리곤이 있으면 '폴리곤 bbox' 기준 — 병합으로 부착된 poly 는
        # rect(광선 캐스팅 박스)보다 클 수 있어, rect 창으로 자르면 바깥 부분이
        # 마스크에서 누락된다(대피공간 제외 누수 버그의 원인이었음).
        if r.get("poly"):
            x0 = min(p[0] for p in r["poly"])
            y0 = min(p[1] for p in r["poly"])
            x1 = max(p[0] for p in r["poly"])
            y1 = max(p[1] for p in r["poly"])
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

    room_mask = np.zeros_like(grid["walkable"])
    for r in rooms:
        if eval_room(r):
            room_mask |= room_mask_of(r)
    print(f"격자 {grid['W']}×{grid['H']} · 보행가능 {int(grid['walkable'].sum()):,}셀 "
          f"(실 내부 {int((grid['walkable'] & room_mask).sum()):,})")

    # ---- 계단 출입구 (BOT 위상: 계단-홀 경계 중점) ----
    vseg, hseg, og, arcs = BB.build_index(ents, cats)
    adjs = []
    for i in range(len(rooms)):
        for j in range(i + 1, len(rooms)):
            bd = BB.boundary(rooms[i]["rect"], rooms[j]["rect"])
            if bd:
                adjs.append((i, j, bd))
    stairs = [r["id"] for r in rooms if "계단" in r["name"]]
    halls = [r["id"] for r in rooms if "ELEV" in r["name"].upper() or "홀" in r["name"]]
    stair_door = []
    for s in stairs:
        for a, b, bd in adjs:
            if s not in (a, b):
                continue
            o = b if a == s else a
            if o not in halls:
                continue
            if bd["axis"] == "V":
                p = ((bd["c0"] + bd["c1"]) / 2, (bd["lo"] + bd["hi"]) / 2)
            else:
                p = ((bd["lo"] + bd["hi"]) / 2, (bd["c0"] + bd["c1"]) / 2)
            stair_door.append(p)
            break

    # ---- 보행 거리장 + 통계 ----
    walk_f = FF.distance_field(grid, stair_door)
    fin = np.isfinite(walk_f) & grid["walkable"]
    ev = grid["walkable"] & room_mask          # '각 부분' 평가 셀 = 실 내부
    evf = ev & fin
    n_unreach = int(ev.sum() - evf.sum())
    wmax = float(walk_f[evf].max()) if evf.any() else 0.0
    wj, wi = np.unravel_index(np.where(evf, walk_f, -1).argmax(), walk_f.shape)
    worst_xy = FF.to_xy(grid, wj, wi)
    print(f"보행거리(실 내부 셀): 최악 {wmax/1000:.1f}m · 실 내 미도달 {n_unreach:,}셀")

    # ---- 보(구조도 정합) — 살수장애 회피 제약 (체크 15806·15807) ----
    # 보 축선·거더 폴리곤 + 0.6m 팽창 대역 = 헤드 '설치 위치' 금지 (커버 대상은 그대로
    # — 보는 천장 장애물일 뿐 그 아래 바닥도 방호 대상).
    beams = None
    beam_avoid = None
    bp = os.path.join(FO, "output", f"{base}_beams.json")
    if os.path.exists(bp):
        beams = json.load(open(bp, encoding="utf-8"))
        bl = np.zeros_like(grid["walkable"])
        FF._raster([tuple(s) for s in beams["segs"]],
                   grid["x0"], grid["y0"], grid["W"], grid["H"], bl)
        for poly in beams["polys"]:    # 거더는 내부 전체가 보 아래
            pxs_ = [p[0] for p in poly]
            pys_ = [p[1] for p in poly]
            j0, i0 = FF.to_cell(grid, min(pxs_), min(pys_))
            j1, i1 = FF.to_cell(grid, max(pxs_), max(pys_))
            j0, i0 = max(j0, 0), max(i0, 0)
            j1, i1 = min(j1, grid["H"] - 1), min(i1, grid["W"] - 1)
            if j1 < j0 or i1 < i0:
                continue
            jj, ii = np.mgrid[j0:j1 + 1, i0:i1 + 1]
            pts = np.column_stack([grid["x0"] + (ii.ravel() + 0.5) * FF.CELL,
                                   grid["y0"] + (jj.ravel() + 0.5) * FF.CELL])
            bl[j0:j1 + 1, i0:i1 + 1] |= \
                _MPath(poly).contains_points(pts).reshape(jj.shape)
        # 0.6m 이격 대역 — 원형(유클리드) 팽창. 반복 _dilate(정사각)는 대각 방향에서
        # 0.6m 미만이 새어나가(예: 564mm) 원형 오프셋 합집합으로 보장한다.
        _R = 6
        _H, _W = grid["H"], grid["W"]
        out = np.zeros_like(bl)
        for dj in range(-_R, _R + 1):
            for di in range(-_R, _R + 1):
                if dj * dj + di * di > _R * _R:
                    continue
                out[max(dj, 0):_H + min(dj, 0), max(di, 0):_W + min(di, 0)] |= \
                    bl[max(-dj, 0):_H + min(-dj, 0), max(-di, 0):_W + min(-di, 0)]
        beam_avoid = out
        print(f"보 제약: 축선·거더 + 0.6m 대역 = 설치금지 {int(beam_avoid.sum()):,}셀 "
              f"(춤 {beams.get('depth_mm') or '?'}mm)")

    # ---- 헤드 v3: 창문 특칙 → 구역 분할 → 구역별 greedy 최대커버 → 프루닝 → 검수 ----
    # (격자+수리 2단계 폐기 — 격자는 방 형상을 모른 채 겹치게 깔리고 수리가 덧대는
    #  구조라, 빈 상태에서 구역별로 최적 위치를 고르는 단일 로직으로 재구성)
    heads, r_of, kind = [], [], []
    # (a) 창문 헤드: 세대 실 가장자리의 창 구간(≥600) → 외측 확인 → 0.6m 안쪽 (법정 특칙)
    span_step = 2 * math.sqrt(R_UNIT ** 2 - WIN_OFF ** 2)   # ≈5060

    def outside_pt(x, y):
        return not any(rm["rect"][0] <= x <= rm["rect"][2]
                       and rm["rect"][1] <= y <= rm["rect"][3] for rm in rooms)

    n_win = 0
    for r in rooms:
        if r["common"]:
            continue
        x0, y0, x1, y1 = r["rect"]
        edges = [("V", x0, +1), ("V", x1, -1), ("H", y0, +1), ("H", y1, -1)]
        for axis, c, inward in edges:
            src = vseg["window"] if axis == "V" else hseg["window"]
            lo_r, hi_r = (y0, y1) if axis == "V" else (x0, x1)
            for wc, s0, s1 in src:
                if abs(wc - c) > 250:
                    continue
                a, b = max(s0, lo_r), min(s1, hi_r)
                if b - a < 600:
                    continue
                probe = (wc - inward * 400, (a + b) / 2) if axis == "V" \
                    else ((a + b) / 2, wc - inward * 400)
                if not outside_pt(*probe):
                    continue    # 외벽 창만
                nseg = max(1, math.ceil((b - a) / span_step))
                for k in range(nseg):
                    m = a + (b - a) * (k + 0.5) / nseg
                    p = (wc + inward * WIN_OFF, m) if axis == "V" \
                        else (m, wc + inward * WIN_OFF)
                    heads.append(p)
                    r_of.append(R_UNIT)
                    kind.append("win")
                    n_win += 1
    def _sh(m, dj, di):
        out = np.zeros_like(m)
        Hh, Ww = m.shape
        out[max(dj, 0):Hh + min(dj, 0), max(di, 0):Ww + min(di, 0)] = \
            m[max(-dj, 0):Hh + min(-dj, 0), max(-di, 0):Ww + min(-di, 0)]
        return out

    def _open(m, it=2):
        e = m.copy()
        for _ in range(it):
            e = e & _sh(e, 1, 0) & _sh(e, -1, 0) & _sh(e, 0, 1) & _sh(e, 0, -1)
        d = e
        for _ in range(it):
            d = d | _sh(d, 1, 0) | _sh(d, -1, 0) | _sh(d, 0, 1) | _sh(d, 0, -1)
        return d & m

    # 창문 헤드 벽 스냅 — 팽창된 벽 셀 위에 얹히면 LoS 전면 차단. 스냅 불가면 제거.
    snapped, r2, k2 = [], [], []
    for (hx, hy), rr, kd in zip(heads, r_of, kind):
        c = FF.snap(grid, hx, hy, reach=4)
        if c:
            x, y = FF.to_xy(grid, *c)
            snapped.append((x, y))
            r2.append(rr)
            k2.append(kd)
    heads, r_of, kind = snapped, r2, k2
    n_win = len(heads)

    # (b0) 무명실 승격: 어떤 실에도 속하지 않는 밀폐 보행영역(≥2㎡)을 별개 방으로
    # 간주해 커버 대상에 포함 — 도면에 이름만 안 적힌 방(무명 창고·부속실 등).
    # 2㎡ 미만(PD·소형 샤프트)은 파이프덕트류로 보고 제외(체크 16254 취지).
    # PIT 등 평가 제외 실 내부는 승격 대상에서도 제외.
    skip_mask = np.zeros_like(grid["walkable"])
    pit_mask = np.zeros_like(grid["walkable"])
    for r in rooms:
        if not eval_room(r):
            m0 = room_mask_of(r)
            skip_mask |= m0
            # 피트 마스크는 **파이프덕트·피트류에만**. 예전에는 '제외로 판정된
            # 모든 실'을 담아서, 대피공간처럼 피트가 아닌 제외실 옆의 무명실까지
            # '피트 부속'으로 삼켜 버렸다.
            _b = BINDINGS.get(r["name"])
            _node = (_b or {}).get("노드", "") + " " + (_b or {}).get("이유", "")
            if any(k in r["name"].upper() for k in PIT_KW) \
                    or "파이프덕트" in _node or "덕트피트" in _node:
                pit_mask |= m0
    unnamed, pit_annex = [], []
    unl = _open(grid["walkable"] & ~room_mask & ~skip_mask, 2)
    lab0 = np.zeros(unl.shape, dtype=np.int32)
    from collections import deque as _dq
    ucomp = 0
    for sj, si in np.argwhere(unl):
        if lab0[sj, si]:
            continue
        ucomp += 1
        q = _dq([(sj, si)])
        lab0[sj, si] = ucomp
        comp = [(sj, si)]
        while q:
            j, i = q.popleft()
            for dj, di in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nj, ni = j + dj, i + di
                if 0 <= nj < grid["H"] and 0 <= ni < grid["W"] \
                        and unl[nj, ni] and not lab0[nj, ni]:
                    lab0[nj, ni] = ucomp
                    q.append((nj, ni))
                    comp.append((nj, ni))
        if len(comp) >= 200:            # 2㎡ 이상만 방으로 승격
            m = np.zeros_like(grid["walkable"])
            for j, i in comp:
                m[j, i] = True
            # PIT 와 개방/문으로 이어진 공간은 피트 부속(점검 통로 등)으로 보고
            # 승격하지 않음 — 벽 너머 단순 인접은 걸리지 않는다(1셀 팽창은 벽
            # 래스터 밴드(≥2셀)를 못 넘고, 문을 뚫은 통로로 맞닿은 경우만 참).
            # (대피공간 등 다른 제외실 옆방은 피트가 아니므로 PIT 마스크만 사용)
            if (FF._dilate(m) & pit_mask).any():
                pit_annex.append(m)
                continue
            unnamed.append(m)
            room_mask |= m
    if unnamed:
        print(f"무명실 승격: {len(unnamed)}곳 "
              f"({', '.join(f'{int(m.sum()) / 100:.1f}㎡' for m in unnamed)})")
    if pit_annex:
        print(f"PIT 연결 부속공간 제외: {len(pit_annex)}곳 "
              f"({', '.join(f'{int(m.sum()) / 100:.1f}㎡' for m in pit_annex)})")

    # (b) 커버 대상 + 구역 분할 — 각 셀은 정확히 한 구역에만 속한다.
    # 실 폴리곤(flood, 정확) → rect 실은 작은 것부터(겹친 bbox 는 작은 실이 선점)
    # → 무명실 → 남는 셀(통로·주차장 등 개방영역)은 연결성분마다 하나의 구역.
    cover_mask = _open(grid["walkable"] & (room_mask | fin), 2)
    cover_idx = np.argwhere(cover_mask)
    zone_of = np.full(cover_mask.shape, -1, dtype=np.int32)
    zones = []                    # (cells_idx, r, kind, name)

    def _claim(mask, rr, kd, name):
        m = mask & cover_mask & (zone_of < 0)
        if m.any():
            zone_of[m] = len(zones)
            zones.append((np.argwhere(m), rr, kd, name))

    ev_rooms = [r for r in rooms if eval_room(r)]
    for r in sorted(ev_rooms, key=lambda r: (
            0 if r.get("poly") else 1,
            (r["rect"][2] - r["rect"][0]) * (r["rect"][3] - r["rect"][1]))):
        _claim(room_mask_of(r), R_COMMON if r["common"] else R_UNIT,
               "common" if r["common"] else "unit", r["name"])
    for k, m in enumerate(unnamed, 1):
        _claim(m, R_COMMON, "common", f"무명실{k}")
    # 잔여 연결성분 (0.3㎡ 미만 파편은 커버 대상이지만 자체 구역은 안 만듦 —
    # 이웃 구역 헤드가 개방 연결로 덮고, 못 덮으면 검수 안전망이 잡는다)
    left = cover_mask & (zone_of < 0)
    lab = np.zeros(left.shape, dtype=np.int32)
    from collections import deque as _dq
    comp_id = 0
    for sj, si in np.argwhere(left):
        if lab[sj, si]:
            continue
        comp_id += 1
        q = _dq([(sj, si)])
        lab[sj, si] = comp_id
        comp = [(sj, si)]
        while q:
            j, i = q.popleft()
            for dj, di in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nj, ni = j + dj, i + di
                if 0 <= nj < grid["H"] and 0 <= ni < grid["W"] \
                        and left[nj, ni] and not lab[nj, ni]:
                    lab[nj, ni] = comp_id
                    q.append((nj, ni))
                    comp.append((nj, ni))
        if len(comp) >= 30:
            m = np.zeros_like(cover_mask)
            for j, i in comp:
                m[j, i] = True
            _claim(m, R_COMMON, "common", "잔여공간")

    # (c) 구역별 greedy 최대커버 — 창문 헤드가 덮는 셀은 기 커버로 반영
    zstat = []
    for zi, (cells, rr, kd, name) in enumerate(zones):
        pre = []
        for (wx, wy) in heads[:n_win]:
            cj, ci = FF.to_cell(grid, wx, wy)
            if 0 <= cj < grid["H"] and 0 <= ci < grid["W"] and zone_of[cj, ci] == zi:
                pre.append((wx, wy, R_UNIT))
        zh = FF.zone_cover(grid, cells, rr, pre=pre, avoid=beam_avoid)
        zstat.append((name, len(cells), len(zh)))
        for p in zh:
            heads.append(p)
            r_of.append(rr)
            kind.append(kd)
    dense = sorted(zstat, key=lambda t: -t[2])[:8]
    print("구역별 헤드(상위):", " · ".join(
        f"{nm} {nh}개/{nc / 100:.0f}㎡" for nm, nc, nh in dense))

    # (d) 전역 프루닝 — 구역 경계·개방 연결에서 완전 중복된 헤드 제거 (창문은 유지)
    heads, r_of, kind, n_pruned = FF.prune_heads(
        grid, cover_idx, heads, r_of, kind, keep={"win"})

    # (e) 전셀 검수 + 안전망 (v3 에선 통상 0 — 구역 분할이 놓친 파편 셀 몫)
    added, slack, triggers = FF.repair_cover(grid, cover_idx, heads, r_of, R_REPAIR,
                                             avoid_mask=beam_avoid)
    kind += ["repair"] * len(added)
    n_zone = sum(1 for k in kind if k in ("unit", "common"))
    print(f"헤드: 구역 {len(zones)}곳 최적배치 {n_zone} + 창문 {n_win} "
          f"+ 안전망 {len(added)} = {len(heads)} (프루닝 -{n_pruned}, "
          f"커버 여유 {-slack/1000:.2f}m {'✓전셀커버' if slack <= 0 else '⚠미커버'})")

    # ---- 보 이격 사후 검사 (15806) — 좁은 실 등 회피 불가 잔여분만 ⚠ ----
    beam_warn = {}
    if beams is not None:
        edges = [tuple(s) for s in beams["segs"]]
        for p in beams["polys"]:
            edges += [(p[i][0], p[i][1], p[i + 1][0], p[i + 1][1])
                      for i in range(len(p) - 1)]

        def _dseg(px, py, x1, y1, x2, y2):
            vx, vy = x2 - x1, y2 - y1
            L2 = vx * vx + vy * vy
            if L2 == 0:
                return math.hypot(px - x1, py - y1)
            t = max(0.0, min(1.0, ((px - x1) * vx + (py - y1) * vy) / L2))
            return math.hypot(px - (x1 + t * vx), py - (y1 + t * vy))

        for hi, (hx, hy) in enumerate(heads):
            dmin = min((_dseg(hx, hy, *e) for e in edges), default=1e9)
            if dmin < 600:
                beam_warn[hi] = dmin
        print(f"보 이격 검사(0.6m, 체크 15806): 미달 {len(beam_warn)}개"
              + (f" — {sorted(round(v) for v in beam_warn.values())}mm" if beam_warn else ""))

    # ---- 추가 법규 지표: 헤드 간 근접(차폐판 스크린 15755) · 헤드-벽 간격(16228) ----
    hh_min, hh_pairs = None, 0
    for i in range(len(heads)):
        for j in range(i + 1, len(heads)):
            d = math.hypot(heads[i][0] - heads[j][0], heads[i][1] - heads[j][1])
            if hh_min is None or d < hh_min:
                hh_min = d
            if d < 1800.0:              # NFPA 관행(6ft) 기준의 근접 스크린
                hh_pairs += 1
    wall_min = None
    _blkw = grid["blk_wall"]
    for hx, hy in heads:
        j0, i0 = FF.to_cell(grid, hx, hy)
        for dj in range(-6, 7):
            for di in range(-6, 7):
                j, i = j0 + dj, i0 + di
                if 0 <= j < grid["H"] and 0 <= i < grid["W"] and _blkw[j, i]:
                    d = math.hypot(dj, di) * FF.CELL
                    if wall_min is None or d < wall_min:
                        wall_min = d
    print(f"헤드 간 최소 {hh_min/1000:.2f}m (1.8m 미만 근접쌍 {hh_pairs}) · "
          f"헤드-벽 최소 ≈{wall_min:.0f}mm(래스터)" if hh_min else "")

    # ---- 소화기 + 20m 재검증 (헤드 전용 모드에선 생략) ----
    exts = [] if heads_only else \
        [(*cen(r), "세대용(현관)") for r in rooms if "현관" in r["name"]]
    if not heads_only:
        exts += [(p[0] + 400, p[1], "공용(계단 출입구)") for p in stair_door]
    def ext_check():
        f_ = FF.distance_field(grid, [(x, y) for x, y, _ in exts])
        fi = np.isfinite(f_) & ev
        mx = float(f_[fi].max()) if fi.any() else 0.0
        k = np.unravel_index(np.where(fi, f_, -1).argmax(), f_.shape)
        return f_, mx, FF.to_xy(grid, *k)

    n_ext_add = 0
    emax, ext_ok = 0.0, True
    paths = []
    if not heads_only:
        ext_f, emax, eworst = ext_check()
        while emax > EXT_LIMIT and n_ext_add < 4:   # 20m 초과 최원점 보완(greedy)
            exts.append((*eworst, "보완(20m 커버)"))
            n_ext_add += 1
            ext_f, emax, eworst = ext_check()
        ext_ok = emax <= EXT_LIMIT
        print(f"소화기 보행거리 재검증: 최악 {emax/1000:.1f}m "
              f"{'≤20m ✓' if ext_ok else '>20m ⚠'} (보완 배치 +{n_ext_add})")
        # ---- 피난 동선(경사 하강) ----
        paths = [FF.descend_path(grid, walk_f, *cen(r))
                 for r in rooms if "현관" in r["name"]]
        paths = [p for p in paths if p]

    # ---- 히트맵 PNG (헤드 전용 모드에선 생략) ----
    b64 = ""
    if not heads_only:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        hm = np.where(fin, walk_f / 1000.0, np.nan)
        cm = plt.get_cmap("RdYlGn_r").copy()
        rgba = cm(np.clip(hm / 30.0, 0, 1))
        rgba[..., 3] = np.where(np.isnan(hm), 0.0, 0.5)
        buf = io.BytesIO()
        plt.imsave(buf, rgba[::-1], format="png")   # 행 뒤집기: CAD y↑ → 이미지 y↓
        b64 = base64.b64encode(buf.getvalue()).decode()

    # ---- SVG ----
    minx, miny, maxx, maxy = bounds

    def fy(y):
        return round(maxy - y)

    G = {k: [] for k in ("walls", "beam", "rooms", "labels", "heat", "cov", "hd-unit",
                         "hd-common", "hd-win", "hd-repair", "bwarn", "esc",
                         "outlet", "ext")}
    # 구조도 정합 보(align_beams.py 산출물, 위에서 로드) — 있으면 오버레이
    if beams is not None:
        G["beam"].append('<path d="' + "".join(
            f"M{a} {fy(b)}L{c} {fy(d)}" for a, b, c, d in beams["segs"]) + '"/>')
        for poly in beams["polys"]:
            pts = " ".join(f"{p[0]},{fy(p[1])}" for p in poly)
            G["beam"].append(f'<polygon points="{pts}"/>')
    # 보 이격 미달 헤드 ⚠ 링 (좁은 실 등 회피 불가 — 15806 단서 검토 대상)
    for hi, dmin in beam_warn.items():
        hx, hy = heads[hi]
        G["bwarn"].append(
            f'<circle cx="{round(hx)}" cy="{fy(hy)}" r="360">'
            f'<title>#{hi + 1} 보 이격 {dmin:.0f}mm &lt; 600mm — '
            f'이격 조정 불가 시 반사판 하향/차폐 검토 (체크 15806·15807)</title></circle>')
    gx0, gy0 = grid["x0"], grid["y0"]
    gw, gh = grid["W"] * FF.CELL, grid["H"] * FF.CELL
    if b64:
        G["heat"].append(f'<image x="{round(gx0)}" y="{fy(gy0 + gh)}" '
                         f'width="{round(gw)}" height="{round(gh)}" '
                         f'preserveAspectRatio="none" '
                         f'href="data:image/png;base64,{b64}"/>')
    G["walls"].append('<path d="' + "".join(
        f"M{round(a)} {fy(b)}L{round(c)} {fy(d)}" for a, b, c, d in wall_walk) + '"/>')
    for r in rooms:
        if r.get("poly"):
            pts = " ".join(f"{p[0]},{fy(p[1])}" for p in r["poly"])
            G["rooms"].append(f'<polygon points="{pts}"/>')
        else:
            x0, y0, x1, y1 = r["rect"]
            G["rooms"].append(f'<rect x="{x0}" y="{fy(y1)}" '
                              f'width="{x1-x0}" height="{y1-y0}"/>')
        from plan_label import label_spot
        cx, cy = label_spot(r)           # 오목한 실에서도 트인 자리에 (plan_label)
        _also = rooms_data[r["id"]].get("also") if r["id"] < len(rooms_data) else None
        _tt = (f'<title>개방 병합(벽 없음): {html.escape(", ".join(_also))}</title>'
               if _also else "")
        G["labels"].append(f'<text x="{round(cx)}" y="{fy(cy)}">'
                           f'{html.escape(r["name"])}{_tt}</text>')
    for k, m in enumerate(unnamed, 1):
        jj, ii = np.nonzero(m)
        ux, uy = FF.to_xy(grid, float(jj.mean()), float(ii.mean()))
        G["labels"].append(f'<text x="{round(ux)}" y="{fy(uy)}" '
                           f'style="fill:#b26a00">무명실{k}</text>')
    KD_KO = {"unit": "세대(구역 최적)", "common": "공용(구역 최적)",
             "win": "창문 0.6m", "repair": "안전망(검수 보완)"}
    for hi, ((hx, hy), rr, kd) in enumerate(zip(heads, r_of, kind)):
        G["cov"].append(f'<circle class="{kd}" data-ci="{hi}" cx="{round(hx)}" '
                        f'cy="{fy(hy)}" r="{round(rr)}"/>')
        G[f"hd-{kd if kd in ('unit','common','win','repair') else 'unit'}"].append(
            f'<circle data-hi="{hi}" data-k="{kd}" data-r="{rr/1000:.1f}" '
            f'cx="{round(hx)}" cy="{fy(hy)}" r="90">'
            f'<title>#{hi+1} {KD_KO.get(kd, kd)} · r={rr/1000:.1f}m</title></circle>')
    # 검수 시각화: 수리를 촉발한 미커버 최원점(✗) — 기본 숨김, 리플레이에서 표시
    G["fix"] = []
    for ri, (tx, ty) in enumerate(triggers):
        xx, yy = round(tx), fy(ty)
        G["fix"].append(
            f'<g class="fx" data-ri="{ri}">'
            f'<line x1="{xx-160}" y1="{yy-160}" x2="{xx+160}" y2="{yy+160}"/>'
            f'<line x1="{xx-160}" y1="{yy+160}" x2="{xx+160}" y2="{yy-160}"/></g>')
    for p in paths:
        d = " ".join(f"{round(x)},{fy(y)}" for x, y in p)
        G["esc"].append(f'<polyline points="{d}"/>')
    if not heads_only:   # 방수구·비상콘센트 마커는 전체 모드 전용(헤드 모드 범례에 없음)
        for x, y in stair_door:
            G["outlet"].append(f'<circle class="or5" cx="{round(x)}" cy="{fy(y)}" '
                               f'r="{round(OUTLET_R)}"/>')
            G["outlet"].append(f'<rect class="sq" x="{round(x)-220}" y="{fy(y)-220}" '
                               f'width="440" height="440"/>')
    for x, y, lab in exts:
        G["ext"].append(f'<g><title>{html.escape(lab)}</title>'
                        f'<path d="M{round(x)} {fy(y)-260}L{round(x)-230} {fy(y)+200}'
                        f'L{round(x)+230} {fy(y)+200}Z"/></g>')
    if not heads_only:   # 최악 보행거리 지점 마커(피난 동선 레이어 소속)
        G["esc"].append(f'<circle class="worst" cx="{round(worst_xy[0])}" '
                        f'cy="{fy(worst_xy[1])}" r="200"/>')

    groups_svg = "\n".join(f'<g id="g-{k}">{"".join(v)}</g>' for k, v in G.items())
    pad = 3000
    vb = f"{round(minx)-pad} {-pad} {round(maxx-minx)+2*pad} {round(maxy-miny)+2*pad}"
    cnt = {k: kind.count(k) for k in ("unit", "common", "win", "repair")}

    # ---- 범례(카테고리 그룹) + 헤드 현황 표 + 상단 KPI 칩 ----
    def _cb(gid, col, lab):
        return (f'<label><input type="checkbox" data-g="{gid}" checked/>'
                f'<span style="color:{col}">{html.escape(lab)}</span></label>')

    _ru = f"{R_UNIT / 1000:.1f}".rstrip("0").rstrip(".")
    _rc = f"{R_COMMON / 1000:.1f}".rstrip("0").rstrip(".")
    lg_secs = []
    lg_secs.append(("헤드", [
        _cb("g-hd-unit", "#1e88e5", f"세대 r{_ru} ({cnt['unit']})"),
        _cb("g-hd-common", "#8e24aa", f"공용 r{_rc} ({cnt['common']})"),
        _cb("g-hd-win", "#00acc1", f"창문 0.6m ({cnt['win']})"),
        _cb("g-hd-repair", "#f6b900", f"안전망 ({cnt['repair']})"),
        _cb("g-cov", "#607d8b", "커버리지 원"),
    ]))
    if beams:
        _bd = beams.get("depth_mm")
        lg_secs.append(("구조 검증", [
            _cb("g-beam", "#8d6e63",
                f"보·거더{f' 춤{_bd:.0f}' if _bd else ''} "
                f"({len(beams['segs'])}선+{len(beams['polys'])}PC)"),
            _cb("g-bwarn", "#e65100", f"⚠ 보 이격 미달 ({len(beam_warn)})"),
        ]))
    if not heads_only:
        lg_secs.append(("설비 · 동선", [
            _cb("g-heat", "#d84315", "보행거리 히트맵"),
            _cb("g-ext", "#e53935", f"소화기 ({len(exts)}, 보완+{n_ext_add})"),
            _cb("g-outlet", "#fb8c00", f"방수구·비상콘센트 ({len(stair_door)})"),
            _cb("g-esc", "#2e7d32", f"피난 동선 ({len(paths)})"),
        ]))
    lg_secs.append(("도면", [
        _cb("g-labels", "#5a6578", "실 이름"),
        _cb("g-rooms", "#94a3b8", "방 경계"),
        _cb("g-walls", "#9aa5b5", "벽 선형"),
    ]))
    legend_html = "".join(
        f'<div class="lg-sec"><div class="lg-t">{t}</div>'
        f'<div class="lg-grid">{"".join(items)}</div></div>'
        for t, items in lg_secs)

    KIND_META = [
        ("세대", "#1e88e5", f"{R_UNIT / 1000:.1f}m", cnt["unit"]),
        ("공용", "#8e24aa", f"{R_COMMON / 1000:.1f}m", cnt["common"]),
        ("창문 0.6m", "#00acc1", f"{R_UNIT / 1000:.1f}m", cnt["win"]),
        ("안전망", "#f6b900", f"{R_REPAIR / 1000:.1f}m", cnt["repair"]),
    ]
    head_tbl = (
        '<table class="tbl"><thead><tr><th>구분</th><th>수평거리</th><th>개수</th></tr>'
        '</thead><tbody>'
        + "".join(f'<tr><td><span class="dot" style="background:{c}"></span>'
                  f'{html.escape(nm)}</td><td>{r_}</td><td class="num">{n}</td></tr>'
                  for nm, c, r_, n in KIND_META)
        + f'<tr class="sum"><td>합계</td><td></td><td class="num">{len(heads)}</td></tr>'
          '</tbody></table>'
        + f'<div class="meta">구역 {len(zones)}곳 · 중복 제거 -{n_pruned}'
        + (f' · 보 이격 ⚠{len(beam_warn)}' if beams else '') + '</div>'
        + (f'<div class="meta warn-r">⚠ 수평거리 사용자 지정 — 세대 {R_UNIT / 1000:.2f}m · '
           f'공용 {R_COMMON / 1000:.2f}m (법정 기준 2.6/2.3m, 상회분은 성능 인정 '
           f'헤드 사용 전제)</div>' if r_custom else
           f'<div class="meta">적용 수평거리 — 세대 2.6m · 공용 2.3m (법정 기준값)</div>')
        + '<div class="meta hint">반경 변경은 좌측 "재계산" 카드에서 (로컬 서버 실행 시)</div>')
    zrows = "".join(
        f'<tr><td>{html.escape(nm)}</td><td class="num">{nc / 100:.0f}</td>'
        f'<td class="num">{nh}</td></tr>'
        for nm, nc, nh in sorted(zstat, key=lambda t: -t[2]) if nh > 0)
    room_tbl = (
        '<details class="plain"><summary>실별 헤드 수 펼치기</summary>'
        '<div class="scrolltbl"><table class="tbl">'
        '<thead><tr><th>실</th><th>㎡</th><th>헤드</th></tr></thead>'
        f'<tbody>{zrows}</tbody></table></div></details>')

    ok_cov = slack <= 0

    def _chip(txt, cls=""):
        return f'<span class="chip {cls}">{txt}</span>'

    chips_html = (_chip(f'헤드 <b>{len(heads)}</b>')
                  + _chip(f'구역 <b>{len(zones)}</b>')
                  + _chip(f"전셀 커버 {'✓' if ok_cov else '⚠'} {-slack / 1000:+.2f}m",
                          "ok" if ok_cov else "warn"))
    if r_custom:
        chips_html += _chip(f"반경조정 {R_UNIT / 1000:.1f}/{R_COMMON / 1000:.1f}m", "warn")
    if beams is not None:
        chips_html += _chip("보 이격 " + (f"⚠ {len(beam_warn)}개" if beam_warn else "✓"),
                            "warn" if beam_warn else "ok")
    if not heads_only:
        chips_html += (_chip(f"보행 최악 {wmax / 1000:.1f}m",
                             "ok" if wmax <= 30000 else "warn")
                       + _chip(f"소화기 최악 {emax / 1000:.1f}m",
                               "ok" if ext_ok else "warn"))

    # ── 건물 사실(설계 가정) 버튼 + 패널 — 매칭·배치가 전제한 값을 숨기지
    # 않는다. 이 가정이 규칙×실 매칭 문맥에 실리므로(내화구조 → 2.3m 확신),
    # 사람이 보고 틀렸으면 파일을 고쳐 재매칭할 수 있어야 한다.
    _bprof = {}
    try:
        _bprof = json.load(open(os.path.join(FO, "data", "building_profile.json"),
                                encoding="utf-8"))
    except Exception:
        pass
    facts_html = ""
    if _bprof:
        _frow = [(k, str(v)) for k, v in _bprof.items()
                 if isinstance(v, (str, int)) and not isinstance(v, bool)]
        _frow += [(f"층 · {k.replace('_', ' ')}", "예" if v else "아니오")
                  for k, v in _bprof.get("층", {}).items()
                  if isinstance(v, bool)]
        facts_html = (
            '<div id="bf-pop" hidden><div id="bf-box">'
            '<h4>🏢 건물 사실 — 설계 가정</h4>'
            '<div class="meta" style="margin-bottom:8px">아래 값은 확인된 사실이 '
            '아니라 <b>가정</b>이며, 규칙×실 매칭과 배치 판정의 전제로 쓰였습니다. '
            '<code>data/building_profile.json</code> 을 고친 뒤 재매칭하면 '
            '반영됩니다.</div><table class="tbl">'
            + "".join(f'<tr><td style="color:var(--mut);white-space:nowrap">'
                      f'{html.escape(str(k))}</td>'
                      f'<td><b>{html.escape(str(v))}</b></td></tr>'
                      for k, v in _frow)
            + '</table><button id="bf-x">닫기</button></div></div>')
        chips_html += ('<button id="bf-btn" class="chip" type="button">'
                      '🏢 건물 사실</button>')

    # 배치 로직 패널 — 짧고 읽기 쉬운 단계 설명 (줄바꿈은 pre-wrap 으로 유지)
    LOGIC = [
        ("반경 결정",
         "· 주거실(거실·침실·주방·욕실·현관 등) → 세대 기준 r={:.1f}m\n"
         "· 그 외 모든 실(부대시설·무명실) → 공용 기준 r={:.1f}m\n"
         "· 애매한 실은 작은 반경(보수적)으로 처리{}".format(
             R_UNIT / 1000, R_COMMON / 1000,
             "\n⚠ 사용자 지정 반경 — 법정 기준(2.6/2.3m) 상회분은 성능 인정 "
             "헤드(확대살수형 등) 사용이 전제" if r_custom else
             " (법정 기준값 2.6/2.3m)")),
        ("창문 헤드 — {}개".format(cnt['win']),
         "외벽 창 0.6m 특칙 (세대 실 전용).\n"
         "창을 5.06m 이하 구간으로 나눠 각 구간 중앙 0.6m 안쪽에 배치.\n"
         "법정 위치라 중복 제거 대상에서 제외."),
        ("구역 분할 — {}곳".format(len(zones)),
         "모든 커버 대상 셀을 정확히 한 구역에만 배속.\n"
         "· 실 폴리곤(실제 형상) 우선, 남는 통로는 연결영역별로\n"
         "· 라벨 없는 밀폐 공간(2㎡ 이상)은 '무명실'로 승격\n"
         "· PIT·피트 부속공간은 제외 (체크 16254)"),
        ("최대커버 배치 — {}개".format(n_zone),
         "구역마다 '안 덮인 곳을 가장 많이 덮는 위치'를 반복 선택.\n"
         "· 커버 판정 = 수평거리 ≤ r + 가시선(벽에 안 가림)\n"
         "· 배치 후 간격 균등화 (헤드를 담당 영역 중심으로 이동)"),
        ("중복 제거 — {}개 삭제".format(n_pruned),
         "이웃 헤드들로도 전부 커버되는 완전 중복 헤드 삭제.\n"
         "구역 경계·개방 연결(LDK) 부근에서 주로 발생."),
        ("전수 검수 — 여유 {:+.2f}m".format(-slack / 1000),
         "{:,}개 셀 전부를 최종 재검증 (거리+가시선).\n"
         "미커버가 남으면 안전망 헤드 추가 — 이번 {}개.\n"
         "여유 ≥ 0 = 모든 지점 커버가 보증된 상태.".format(len(cover_idx), cnt['repair'])),
    ]
    if beams is not None:
        _bd = beams.get("depth_mm")
        LOGIC.insert(3, (
            "보 살수장애 회피 — 잔여 ⚠{}개".format(len(beam_warn)),
            "보·거더(춤 {}mm) 좌우 0.6m 안에는 헤드를 놓지 않음.\n"
            "· 보 아래 바닥은 계속 커버 대상 (옆 헤드가 커버, 검수로 보증)\n"
            "· 회피가 불가능한 좁은 곳만 ⚠ 표시 → 반사판 하향/차폐판 검토\n"
            "근거: 체크 15806(반경 0.6m) · 15807(폭 3배)".format(
                f"{_bd:.0f}" if _bd else "?")))
    # ── 적용 파라미터 카드 — 값마다 출처를 배지로 단다.
    # 법령DB(초록) = legal_rule 에서 조건 매칭으로 나온 값. 규칙 번호·원문 첨부.
    # 엔진(회색) = 알고리즘 상수. 법 아님을 명시.
    # 하드코딩(주황) = DB에서 못 찾아 코드 기본값을 쓴 것 — 눈에 띄어야 고친다.
    _plist = (HEAD_PARAMS.get("파라미터") if HEAD_PARAMS else
              [{"이름": "헤드 수평거리(세대)", "값_mm": R_UNIT, "출처": "하드코딩"},
               {"이름": "헤드 수평거리(공용)", "값_mm": R_COMMON, "출처": "하드코딩"}])
    _prof = (HEAD_PARAMS or {}).get("프로필", {})
    _pf_html = ""
    if _prof:
        fl = _prof.get("층", {})
        _pf_html = (f'<div class="quote" style="margin-bottom:6px">'
                    f'{html.escape(_prof.get("이름", ""))} · '
                    f'{html.escape(_prof.get("지역", ""))} · '
                    f'{html.escape(_prof.get("용도", ""))} '
                    f'{_prof.get("층수_지상", "?")}층 {_prof.get("동수", "?")}개동 · '
                    f'{html.escape(_prof.get("구조", ""))} · '
                    f'층={html.escape(fl.get("이름", ""))}'
                    f'{" (세대 없음)" if not fl.get("세대있음") else ""}</div>')
    _rows = []
    for prm in _plist:
        src = prm.get("출처", "?")
        cls = {"법령DB": "law", "엔진": "eng", "하드코딩": "hard"}.get(src, "eng")
        v = prm.get("값_mm")
        vtxt = (f"{v/1000:g} m" if v and v >= 1000 else
                f"{v:g} mm" if v else "—")
        tip = html.escape(" · ".join(
            x for x in (prm.get("근거"), prm.get("조건") or prm.get("설명"))
            if x))
        law_txt = html.escape(prm.get("원문") or "")
        if law_txt:
            tip = (tip + "<br><i>" + law_txt + "</i>") if tip else f"<i>{law_txt}</i>"
        _rows.append(
            f'<details class="prm"><summary>'
            f'<span class="src {cls}">{html.escape(src)}</span>'
            f'<span class="pn">{html.escape(prm.get("이름", ""))}</span>'
            f'<b>{vtxt}</b></summary>'
            f'<div class="quote">{tip}</div></details>')
    _beam = (HEAD_PARAMS or {}).get("보표_2_7_8", [])
    if _beam:
        _rows.append('<details class="prm"><summary>'
                     '<span class="src law">법령DB</span>'
                     '<span class="pn">보표 2.7.8 (전사)</span>'
                     f'<b>{len(_beam)}구간</b></summary><div class="quote">'
                     + "<br>".join(f'{html.escape(b["수평거리"])} → '
                                   f'{html.escape(b["수직거리한계"])}'
                                   for b in _beam) + "</div></details>")
    params_html = _pf_html + "".join(_rows)

    # ── 실 분류 카드 — 실명이 어느 법정 노드로 묶였고 그래서 뭘 했는지.
    # 출처 배지: 별칭(결정적)=초록 · LLM=파랑 · 사람=초록 · 하드코딩=주황.
    # ⚠확인필요(LLM confidence 낮음)는 사람이 캐시 파일에서 확정할 것.
    _rc_rows = []
    _seen_names = set()
    for r in sorted(rooms, key=lambda x: x["name"]):
        if r["name"] in _seen_names:      # 같은 실명(코어 대칭 등)은 한 줄로
            continue
        _seen_names.add(r["name"])
        unit, excl, src = room_class(r["name"])
        b = BINDINGS.get(r["name"], {})
        node = b.get("노드", "—")
        act = ("제외" if excl else "세대 2.6m" if unit else "공용")
        chk = b.get("확인필요", src == "하드코딩")
        cls = {"별칭": "law", "사람": "law", "LLM": "llm",
               "하드코딩": "hard"}.get(src, "llm")
        basis = " · ".join(g.get("출처", "") for g in b.get("근거", [])[:1])
        tip = html.escape((b.get("이유") or "") + (" | " + basis if basis else ""))
        _rc_rows.append(
            f'<details class="prm"><summary>'
            f'<span class="src {cls}">{html.escape(src)}</span>'
            f'<span class="pn">{html.escape(r["name"])}'
            f'{" ⚠" if chk else ""}</span>'
            f'<b>{html.escape(act)}</b></summary>'
            f'<div class="quote">노드: {html.escape(node)}'
            f'{"<br>" + tip if tip else ""}</div></details>')
    roomclass_html = "".join(_rc_rows)

    logic_html = "".join(
        f'<details{" open" if i == 0 else ""}><summary><span class="stepno">{i + 1}</span>'
        f'{html.escape(t)}</summary>'
        f'<div class="quote">{html.escape(q)}</div></details>'
        for i, (t, q) in enumerate(LOGIC))

    # ---- 리플레이(배치 과정 재현) — 헤드 전용 모드 ----
    n_lat = cnt["unit"] + cnt["common"]
    replay_html = replay_js = ""
    if heads_only:
        replay_html = """
<div id="replay">
 <button id="rp-start">▶ 시작</button>
 <button id="rp-reset">↺ 처음부터</button>
 <select id="rp-speed"><option value="1">×1</option><option value="2">×2</option>
 <option value="4" selected>×4</option></select>
 <ol id="rp-st">
  <li>법률 내용 검토</li><li>도면 검토</li><li>구역 분할</li>
  <li>배치 진행(창문·구역 최적화)</li><li>중복 제거(프루닝)</li><li>검수·안전망</li><li>완료</li>
 </ol>
 <div id="rp-msg">▶ 시작 = 배치 과정을 단계별 재현 · 헤드 클릭 = 개별 정보</div>
</div>"""
        replay_js = ("const NWIN=%d,NZONE=%d,NREP=%d,NPRUNE=%d,NZDIV=%d,SLACK='%+.2f',"
                     "RU='%.1f',RC='%.1f';"
                     % (n_win, n_zone, cnt["repair"], n_pruned, len(zones),
                        -slack / 1000, R_UNIT / 1000, R_COMMON / 1000)) + r"""
(function(){
var HD=Array.from(document.querySelectorAll('[data-hi]')).sort(function(a,b){return a.dataset.hi-b.dataset.hi;});
var CV=Array.from(document.querySelectorAll('[data-ci]')).sort(function(a,b){return a.dataset.ci-b.dataset.ci;});
var FX=Array.from(document.querySelectorAll('.fx')).sort(function(a,b){return a.dataset.ri-b.dataset.ri;});
var KO={unit:'세대(구역 최적)',common:'공용(구역 최적)',win:'창문 0.6m',repair:'안전망(검수 보완)'};
var msg=document.getElementById('rp-msg');
/* ---- 헤드 클릭 = 자기 커버리지 원 선택 토글 ---- */
var selected=new Set();
var covG=document.getElementById('g-cov');
var covCb=document.querySelector('#legend input[data-g="g-cov"]');
function applyCovSel(){
 if(selected.size){
  covG.style.display='';
  CV.forEach(function(c){var on=selected.has(c.dataset.ci);
   c.style.display=on?'':'none'; c.classList.toggle('sel',on);});
 }else{
  covG.style.display=(covCb&&!covCb.checked)?'none':'';
  CV.forEach(function(c){c.style.display=''; c.classList.remove('sel');});
 }
}
if(covCb)covCb.addEventListener('change',applyCovSel);
HD.forEach(function(h){h.style.cursor='pointer';
 h.addEventListener('click',function(){var i=+h.dataset.hi, key=String(i);
  if(selected.has(key)){selected.delete(key);}else{selected.add(key);}
  applyCovSel();
  msg.textContent='#'+(i+1)+' '+KO[h.dataset.k]+' 헤드 · 수평거리 반경 '+h.dataset.r+'m · '+
   (h.dataset.k==='repair'?'검수에서 발견된 잔여 미커버 셀을 보완하는 안전망 헤드':
    h.dataset.k==='win'?'외벽 창문 0.6m 규정 배치(NFPC 608 §7)':
    '구역 최대커버 배치(거리·가시선 동시 검증)')+
   (selected.has(key)?' — 커버리지 원 표시 중 ('+selected.size+'개 선택)':' — 원 해제');});});
var speed=4, running=false, cancel=false;
document.getElementById('rp-speed').onchange=function(e){speed=+e.target.value;};
function sl(ms){return new Promise(function(r){setTimeout(r,ms/speed);});}
function st(k){document.querySelectorAll('#rp-st li').forEach(function(e,i){
 e.className=i<k?'done':(i===k?'now':'');});}
function chk(){if(cancel){running=false;return true;}return false;}
function hideHeads(){HD.concat(CV).forEach(function(e){e.style.display='none';});
 FX.forEach(function(e){e.style.display='none';});}
async function run(){
 if(running)return; running=true; cancel=false;
 hideHeads(); st(0);
 var ds=document.querySelectorAll('#legal details');
 for(var i=0;i<Math.min(3,ds.length);i++){ if(chk())return;
  ds[i].open=true; ds[i].classList.add('hot');
  msg.textContent='법률 검토: '+ds[i].querySelector('summary').textContent;
  await sl(1000); ds[i].classList.remove('hot'); ds[i].open=false;}
 st(1); if(chk())return;
 var rms=document.getElementById('g-rooms'); rms.classList.add('hot');
 msg.textContent='도면 검토: 벽 선형·실 경계·통행 격자 인식 (비출력 보조선 A-MC 제외)';
 await sl(1600); rms.classList.remove('hot'); if(chk())return;
 st(2);
 msg.textContent='구역 분할: 실 폴리곤/사각형 → 셀별 단일 구역 '+NZDIV+'곳 · r='+RU+'m(세대)/'+RC+'m(공용) · LoS 검증 준비';
 await sl(1700); if(chk())return;
 st(3);
 for(var i=0;i<NWIN+NZONE;i++){ if(chk())return;
  HD[i].style.display=''; CV[i].style.display='';
  msg.textContent='배치 진행: '+(i<NWIN?'창문 0.6m':'구역 최대커버(거리+가시선)')+' 헤드 '+(i+1)+'/'+(NWIN+NZONE);
  await sl(60);}
 st(4);
 msg.textContent='중복 제거: 담당 셀이 전부 이웃 헤드로도 커버되는 완전 중복 헤드 '+NPRUNE+'개 삭제 (창문 헤드는 법정 위치라 유지)';
 await sl(1400); if(chk())return;
 st(5);
 for(var j=0;j<FX.length;j++){ if(chk())return;
  FX[j].style.display='';
  msg.textContent='검수: 100mm 셀 전수 — 수평거리·가시선 검증, 잔여 미커버 지점 '+(j+1)+'곳 발견';
  await sl(90);}
 await sl(500);
 for(var k2=0;k2<NREP;k2++){ if(chk())return;
  var gi=NWIN+NZONE+k2;
  if(HD[gi]){HD[gi].style.display='';} if(CV[gi]){CV[gi].style.display='';}
  if(FX[k2]){FX[k2].style.display='none';}
  msg.textContent='안전망 배치: 잔여 미커버 셀 보완 헤드 '+(k2+1)+'/'+NREP;
  await sl(130);}
 if(NREP===0){msg.textContent='검수: 잔여 미커버 0곳 — 안전망 불필요';await sl(600);}
 st(6); msg.textContent='완료: 헤드 '+HD.length+'개 · 전 셀 커버 보증 ✓ (여유 '+SLACK+'m)';
 running=false;
}
document.getElementById('rp-start').onclick=run;
document.getElementById('rp-reset').onclick=function(){cancel=true; running=false;
 selected.clear();
 HD.concat(CV).forEach(function(e){e.style.display='none'; e.classList.remove('sel');});
 FX.forEach(function(e){e.style.display='none';}); st(-1);
 msg.textContent='빈 도면으로 초기화 — ▶ 시작을 누르면 배치 과정이 재현됩니다.';};
})();
"""
    # ---- 법적 검토(헤드 모드): 항목별 '적용 결과 vs 법정 기준' 판정 ----
    # 판정: 적합 / 기준초과·부적합(빨강) / 확인필요(주황) / 해당없음·미검증(회색)
    has_unit = any(kd == "unit" for _c, _r, kd, _n in zones)
    EV = []          # {ids, v(판정), t, src, law, res}

    def _ev(ids, v, t, src, law, res):
        EV.append({"ids": ids, "v": v, "t": t, "src": src, "law": law, "res": res})

    def _law(key):
        """법적 근거 표에서 (출처, 조문) — 순서가 아니라 key 로 찾는다.
        예전에는 LEGAL[2][1] 처럼 인덱스로 집어서, 표에 한 줄만 끼워 넣어도
        엉뚱한 조문이 조용히 붙었다."""
        i = next(i for i, (_, k, _) in enumerate(LAW_SPEC) if k == key)
        return LEGAL[i][1], LEGAL[i][2]

    if heads_only:
        cov_ok = slack <= 0
        # 1) 세대 수평거리
        if not has_unit:
            _ev([15797, 16089], "해당없음", "세대 내 수평거리 ≤ 2.6m",
                *_law("r_unit"), "이 층에 세대(주거) 실이 없음 — 적용 대상 아님.")
        else:
            _v = "적합" if (R_UNIT <= 2600 and cov_ok) else \
                 ("기준초과" if R_UNIT > 2600 else "부적합")
            _ev([15797, 16089], _v, "세대 내 수평거리 ≤ 2.6m", *_law("r_unit"),
                f"적용 반경 {R_UNIT/1000:.2f}m vs 법정 2.6m · 전셀 검증 여유 {-slack/1000:+.2f}m"
                + ("\n⚠ 법정 기준 초과 — 확대살수형 등 성능 인정 헤드의 형식승인·설계 근거 "
                   "없이는 위반입니다." if R_UNIT > 2600 else ""))
        # 2) 공용부 수평거리
        _v = "적합" if (R_COMMON <= 2300 and cov_ok) else \
             ("기준초과" if R_COMMON > 2300 else "부적합")
        _ev([15742, 15739], _v, "공용부 수평거리 ≤ 2.3m (내화구조)", *_law("r_common"),
            f"적용 반경 {R_COMMON/1000:.2f}m vs 법정 2.3m(내화) · 전셀 검증 여유 {-slack/1000:+.2f}m"
            + ("\n⚠ 법정 기준 초과 — 성능 인정 헤드 근거 없이는 위반입니다."
               if R_COMMON > 2300 else ""))
        # 3) 외벽 창문 0.6m
        _ev([15798], ("적합" if n_win > 0 else "해당없음"), "외벽 창문 0.6m 이내 배치",
            *_law("window_band"),
            f"창문 헤드 {n_win}개" + ("" if n_win else " — 세대 외벽 창 없음(지하 부대시설)."))
        # 4) 살수장애물 이격
        if beams is None:
            _ev([15806, 15807], "미검증", "살수장애물(보) 이격 0.6m/폭 3배",
                *_law("clear_head"), "구조도 미연계 — align_beams.py 로 보 위치 연계 필요.")
        else:
            _ev([15806, 15807], ("적합" if not beam_warn else "확인필요"),
                "살수장애물(보) 이격 0.6m/폭 3배", *_law("clear_head"),
                f"보(춤 {beams.get('depth_mm') or '?'}mm) 0.6m 대역 회피 배치 · 미달 {len(beam_warn)}개"
                + (f" — 최소 {min(beam_warn.values()):.0f}mm. 소규모 공간 단서 적용 검토"
                   f"(반사판 하향/차폐판)." if beam_warn else ""))
        # 5) 설치 제외 장소
        _ev([16254], "적합", "헤드 설치 제외 장소 (파이프덕트·덕트피트 등)",
            "스프링클러설비 화재안전기술기준(NFTC 103) — 체크 16254",
            "계단실·경사로·승강로·파이프덕트 및 덕트피트·목욕실·화장실 등에는 헤드를 "
            "설치하지 않을 수 있다.",
            "PIT·피트 부속공간 제외 적용. 계단·승강로·화장실은 임의 제외 가능하나 "
            "본 배치는 설치(강화 적용) — 적법.")
        # 5-1) 대피공간 헤드 제외 — 실명 검색이 아니라 **실제 판정**에서 만든다.
        # 예전에는 이름만 보고 "제외 적용"이라 적었는데, 제외 여부는 아래
        # eval_room(바인딩+폴백)이 정하므로 둘이 어긋나면 리포트가 거짓말을 한다.
        _daepi = [r for r in rooms if "대피공간" in r["name"]]
        _dae_off = [r["name"] for r in _daepi if not eval_room(r)]
        _dae_on = [r["name"] for r in _daepi if eval_room(r)]
        _dae_law = next((x for x in (HEAD_PARAMS or {}).get("제외장소", [])
                         if "대피공간" in x.get("원문", "")), None)
        _ev([15805], ("적합" if _daepi else "해당없음"),
            "대피공간 헤드 설치 예외",
            (f"{_dae_law['doc']} {_dae_law['item']} — 규칙 #{_dae_law['rule_id']}"
             if _dae_law else "공동주택의 화재안전성능기준(NFPC 608) — 체크 15805"),
            (_dae_law["원문"] if _dae_law else
             "「건축법 시행령」 제46조제4항에 따라 설치된 대피공간에는 헤드를 설치하지 "
             "않을 수 있다."),
            ("이 층에 대피공간 없음." if not _daepi else
             (f"대피공간 {len(_daepi)}곳 · 제외 {len(_dae_off)}곳"
              + (f", 설치 {len(_dae_on)}곳({', '.join(_dae_on)}) — 임의 규정이라 "
                 f"설치도 적법(강화 적용)." if _dae_on else " — 임의 규정 적용."))))
        # 6) 벽-헤드 공간 10cm
        _ev([16228], ("적합" if (wall_min is None or wall_min >= 100) else "확인필요"),
            "벽과 헤드 간 공간 10cm 이상",
            "스프링클러설비 화재안전기술기준(NFTC 103) — 체크 16228",
            "벽과 스프링클러헤드 간의 공간은 10cm 이상으로 한다.",
            f"헤드-벽 최소 간격 ≈{wall_min:.0f}mm (100mm 격자 근사, 실제 벽면 기준 "
            f"+100mm 내외)" if wall_min is not None else "측정 불가")
        # 7) 근접 헤드 차폐판
        _ev([15755], ("적합" if hh_pairs == 0 else "확인필요"),
            "근접 헤드 상호 방출수 영향 (차폐판)",
            "스프링클러설비 화재안전성능기준(NFPC 103) — 체크 15755",
            "상부에 설치된 헤드의 방출수에 따라 감열부가 영향을 받을 우려가 있는 헤드에는 "
            "차폐판을 설치할 것.",
            f"헤드 간 최소 {hh_min/1000:.2f}m · 1.8m 미만 근접쌍 {hh_pairs}개"
            + (" — 동일 평면 헤드라 통상 무관하나 낙차 있는 배치 시 차폐판 검토."
               if hh_pairs else " — 영향 우려 없음."))
        # 8) 반사판-보 수직거리 (데이터 대기)
        _ev([], "미검증", "반사판과 보 하단 수직거리 표 (NFTC 103 2.7.7)",
            "스프링클러설비 화재안전기술기준(NFTC 103) 2.7.7",
            "보와 가까운 헤드는 보 하단과 반사판 높이 관계를 표 기준에 따라 설치할 것.",
            "보 춤 900mm 확보 — 천장고 데이터가 추가되면 자동 검증 예정.")
        # 9) 측벽형
        _ev([15753, 15754, 16236, 16237, 16238], "해당없음", "측벽형 헤드 간격·배열",
            "NFPC/NFTC 103 — 체크 15753·15754·16236~16238",
            "폭 9m 이하 실은 측벽형 설치 가능, 간격 3.6m 이내, 폭 4.5~9m 는 양측 나란히꼴.",
            "본 배치는 천장형(하향식) 전제 — 측벽형 미사용.")
        # 10) 천장·반자 사이
        _ev([16255, 16256, 16257], "미검증", "천장-반자 사이 헤드 제외 조건",
            "NFTC 103 — 체크 16255~16257",
            "천장·반자 재질과 사이 거리(0.5/1/2m)에 따라 반자 속 헤드 생략 가능.",
            "반자·천장고 데이터 없음 — 단면 연계 시 검증 예정.")

        VCLS = {"적합": "ok", "기준초과": "bad", "부적합": "bad",
                "확인필요": "chk", "해당없음": "na", "미검증": "na", "참고": "na"}
        legal_html = "".join(
            f'<details{" open" if e["v"] in ("기준초과", "부적합") else ""}>'
            f'<summary><span class="vd {VCLS[e["v"]]}">{e["v"]}</span>'
            f'{html.escape(e["t"])}</summary>'
            f'<div class="cite">{html.escape(e["src"])}</div>'
            f'<div class="law"><span class="lb">조문</span>{html.escape(e["law"])}</div>'
            f'<div class="applied"><span class="lb aplb">결과 비교</span>'
            f'{html.escape(e["res"])}</div></details>'
            for e in EV)

        # DB 전체 목록(스프링클러/헤드 관련) — 자동 판정된 항목은 배지, 나머지는 참고
        covered = {}
        for e in EV:
            for cid in e["ids"]:
                covered[cid] = e["v"]
        legal_extra = ""
        dbp = os.path.join(FO, "output", "head_law_checks.json")
        if os.path.exists(dbp):
            dbl = json.load(open(dbp, encoding="utf-8"))["checks"]
            rows_db = "".join(
                f'<tr class="dbrow" data-i="{k}"><td class="num">{c["id"]}</td>'
                f'<td>{html.escape(c["title"])}</td>'
                f'<td><span class="vd {VCLS.get(covered.get(c["id"], "참고"), "na")}">'
                f'{covered.get(c["id"], "참고")}</span></td></tr>'
                f'<tr class="exrow" id="ex{k}"><td colspan="3">'
                f'<div class="law"><span class="lb">'
                f'{html.escape(c.get("source") or c.get("law") or "출처 미상")}</span>'
                f'{html.escape(c["excerpt"] or "(원문 미수록)")}</div></td></tr>'
                for k, c in enumerate(dbl))
            n_auto = len(set(covered) & {c["id"] for c in dbl})
            legal_extra = (
                f'<section class="card"><h3>관련 법령 전체 — DB {len(dbl)}건</h3>'
                f'<div class="meta">스프링클러/헤드 언급 체크 전수 · 자동 판정 {n_auto}건, '
                f'나머지는 참고(수동 검토 대상). <b>행을 클릭하면 조문이 펼쳐집니다.</b></div>'
                f'<div class="scrolltbl tall"><table class="tbl">'
                f'<thead><tr><th>체크</th><th>제목</th><th>판정</th></tr></thead>'
                f'<tbody>{rows_db}</tbody></table></div></section>')

        n_bad = sum(1 for e in EV if e["v"] in ("기준초과", "부적합"))
        n_chk = sum(1 for e in EV if e["v"] == "확인필요")
        n_ok = sum(1 for e in EV if e["v"] == "적합")
        chips_html += _chip(
            f"법적검토 적합 {n_ok}" + (f" · 확인 {n_chk}" if n_chk else "")
            + (f" · 위반 {n_bad}" if n_bad else ""),
            "warn" if (n_bad or n_chk) else "ok")
    else:
        legal_extra = ""
        legal_html = "".join(
            f'<details><summary>{html.escape(t)}</summary>'
            f'<div class="cite"><span class="src '
            f'{ {"법령DB": "law", "하드코딩": "hard"}.get(_k, "eng") }">'
            f'{html.escape(_k)}</span> {html.escape(c)}</div>'
            f'<div class="law"><span class="lb">조문</span>{html.escape(law)}</div>'
            f'<div class="applied"><span class="lb aplb">적용·검증</span>{html.escape(ap_)}</div>'
            f'</details>'
            for t, c, law, ap_, _k in LEGAL)

    # ---- AI 어시스턴트 컨텍스트(결과 요약 JSON) + 챗 스크립트 ----
    rpt = {"도면": base,
           "헤드": {"세대": cnt["unit"], "공용": cnt["common"], "창문": cnt["win"],
                   "안전망": cnt["repair"], "합계": len(heads)},
           "구역수": len(zones), "중복제거": n_pruned,
           "커버여유_m": round(-slack / 1000, 2), "검증셀수": int(len(cover_idx)),
           "실별헤드_상위": [{"실": nm, "면적m2": round(nc / 100), "헤드": nh}
                          for nm, nc, nh in sorted(zstat, key=lambda t: -t[2])
                          if nh > 0][:20],
           "보검증": ({"춤mm": beams.get("depth_mm"),
                      "이격미달": [{"헤드": hi + 1, "거리mm": round(d)}
                                  for hi, d in beam_warn.items()]}
                     if beams is not None else "구조도 미연계"),
           "적용수평거리": {"세대_m": round(R_UNIT / 1000, 2),
                       "공용_m": round(R_COMMON / 1000, 2),
                       "법정기준": "세대 2.6m / 공용 2.3m(내화)",
                       "사용자지정여부": r_custom,
                       "비고": ("법정 기준 상회분은 성능 인정 헤드(확대살수형 등) "
                              "사용 전제" if r_custom else "법정 기준값 그대로")},
           "적용기준": ["NFPC608 §7 세대 2.6m", "NFPC103 §10 공용 2.3m(내화)",
                      "살수장애 0.6m/폭3배(체크 15806·15807)",
                      "PIT·덕트피트 헤드 제외(체크 16254)"],
           "적용파라미터": (HEAD_PARAMS.get("파라미터") if HEAD_PARAMS else
                        [{"key": "r_unit", "이름": "헤드 수평거리(세대)",
                          "값_mm": R_UNIT, "출처": "하드코딩"},
                         {"key": "r_common", "이름": "헤드 수평거리(공용)",
                          "값_mm": R_COMMON, "출처": "하드코딩"}]),
           "보표_법령": (HEAD_PARAMS or {}).get("보표_2_7_8", []),
           "건물설정": (HEAD_PARAMS or {}).get("프로필", {}),
           "배치로직": [{"단계": f"{i + 1}. {t}", "내용": q}
                      for i, (t, q) in enumerate(LOGIC)],
           "법적검토": [{"항목": e["t"], "판정": e["v"], "출처": e["src"],
                       "조문": e["law"], "결과비교": e["res"]} for e in EV]}
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
  if(v){localStorage.setItem('openai_key',v);syncKey();
   add('sys','API 키가 이 브라우저(localStorage)에 저장됐습니다. 이제 질문하세요.');}};
 document.getElementById('okeyreset').onclick=function(e){e.stopPropagation();
  localStorage.removeItem('openai_key');syncKey();};
 syncKey();
 var hist=[];
 function esch(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
 function md(s){
  s=esch(s);
  s=s.replace(/```[a-z]*\n?([\s\S]*?)```/g,function(m,c){
   return '<pre>'+c.replace(/\n$/,'')+'</pre>';});
  s=s.replace(/`([^`\n]+)`/g,'<code>$1</code>');
  s=s.replace(/\*\*([^*\n]+)\*\*/g,'<b>$1</b>');
  s=s.replace(/(^|\n)#{1,4} +(.+)/g,'$1<b class="h">$2</b>');
  s=s.replace(/(^|\n)[ \t]*[-*] +/g,'$1• ');
  s=s.replace(/\n{3,}/g,'\n\n');
  return s;}
 function add(role,txt){var d=document.createElement('div');d.className='cm '+role;
  if(role==='ai'){d.innerHTML=md(txt);}else{d.textContent=txt;}
  msgs.appendChild(d);msgs.scrollTop=msgs.scrollHeight;return d;}
 add('sys','배치 결과에 대해 질문하세요. 예: "키즈짐에 헤드가 20개인 이유는?", "보 이격 미달 1개는 어떻게 처리해야 해?"');
 var SYS='너는 공동주택 소방설비(스프링클러 헤드) 배치 검토를 돕는 전문가다. '
  +'아래 JSON에는 자동 배치 엔진의 결과 요약과 함께, 실제 사용된 배치로직(단계별) '
  +'및 법적근거(조문 원문 + 본 시스템의 적용·검증 방식)가 들어 있다. '
  +'사용자의 질문에는 반드시 이 배치로직 단계와 법적근거(출처·체크번호)를 인용해 '
  +'논리적으로 설명하라 — 예: "3단계 최대커버 배치에 따라 …이고, 근거는 NFPC 608 §7(체크 15797)". '
  +'데이터에 없는 값은 추정하지 말고 없다고 말하라. 간결한 한국어로 답하고, '
  +'서식은 굵게(**)와 불릿(-) 정도만 쓰며 마크다운 표는 쓰지 마라.\n'
  +'검토데이터: '+JSON.stringify(RPT);
 async function send(){
  var q=cin.value.trim(); if(!q)return;
  if(!getKey()){add('sys','먼저 OpenAI API 키를 입력하고 저장하세요.');return;}
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
  }catch(err){wait.textContent='오류: '+err.message+' — API 키/네트워크를 확인하세요.';
   wait.className='cm sys';}
 }
 document.getElementById('csend').onclick=send;
 cin.addEventListener('keydown',function(e){
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}});
})();
""".replace("__RPT__", rpt_json).replace("__KEY__", json.dumps(api_key))

    # '재실행' 버튼 — 로컬 서버(fire_server.py)의 /rerun 으로 재계산 요청 후 새로고침.
    # file:// 로 열었거나 서버가 없으면 실행 안내를 표시한다.
    rerun_js = r"""
(function(){
 var B=__BASE__;
 // 재계산 버튼은 어디서나 보인다(예전 그대로). 서버가 없으면 누를 때
 // "터미널에서 실행하라"는 안내로 물러난다 — res.ok 를 먼저 봐서
 // 404 HTML 을 JSON 으로 읽다 새던 파싱 에러는 안 나온다.
 window.__IS_LOCAL=/^(localhost|127\.)/.test(location.hostname);
 window.__SRV_HINT=function(){
  return ('서버 미연결: 터미널에서 <code>python fire_server.py '+B+'</code> 를 '
          +'실행하면 브라우저가 자동으로 열리고, 이 버튼이 동작합니다.');
 };
 var go=document.getElementById('rr-go'),m=document.getElementById('rr-msg');
 if(!go)return;
 go.onclick=async function(){
  if(!window.__IS_LOCAL){
   // 공개 데모: 서버 재계산 대신 배치 과정 재생(헤드가 놓인 순서대로)
   var rp=document.getElementById('rp-start');
   if(rp){m.textContent='배치 과정을 재생합니다 — 헤드가 놓인 순서대로.';rp.click();}
   else{m.innerHTML=window.__SRV_HINT();}
   return;
  }
  var ru=parseFloat(document.getElementById('rr-ru').value)||2.6;
  var rc=parseFloat(document.getElementById('rr-rc').value)||2.3;
  go.disabled=true; go.textContent='계산 중… (30초~1분)';
  m.textContent='배치 엔진 실행 중 — 완료되면 자동으로 새로고침됩니다.';
  try{
   var res=await fetch('/rerun',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({base:B,r_unit:ru,r_common:rc})});
   if(!res.ok)throw new Error('서버 응답 '+res.status);
   var j=await res.json();
   if(j.ok){location.reload();}
   else{throw new Error(j.error||'배치 엔진 실행 실패');}
  }catch(e){
   go.disabled=false; go.textContent='⟳ 재실행 — 헤드 재배치';
   m.innerHTML=window.__SRV_HINT()
    +' <span style="color:#94a3b8">('+e.message+')</span>';
  }
 };
})();
""".replace("__BASE__", json.dumps(base))

    # ── '확인필요 확정' 카드 — ⚠ 실을 판단 성격별로 묶어 사람이 확정한다.
    # 재계산 카드와 함께 **로컬 서버에서만** 보인다(공개 데모에서는 JS 가
    # 숨김 — 서버 없는 정적 페이지에서 동작 안 하는 UI 는 소음이다).
    def _dc_grp(b):
        if b.get("기본동작") == "제외":
            return 0
        return 1 if b.get("생략가능") else 2
    _GRP_T = ["현재 제외 — 맞는지 확인", "면제 걸림 · 정책상 설치 — 뺄 수 있음",
              "그 외 확인필요"]
    _dc_html, _n_decided = [], 0
    for _gi, _gt in enumerate(_GRP_T):
        _rows = []
        for _n in PENDING:
            _b = BINDINGS.get(_n, {})
            if _dc_grp(_b) != _gi:
                continue
            _cur = _b.get("기본동작", "—")
            _d = DECISIONS.get(_n, "")
            _n_decided += bool(_d)
            _short = (_cur.replace("반경 ", "").replace("세대 반경 ", "세대 ")
                          .replace("일반(공용 반경)", "설치"))
            _chip = (f'<span class="dc-cur ex">제외</span>' if _cur == "제외" else
                     f'<span class="dc-cur in">{html.escape(_short)}</span>')
            _tip = html.escape((_b.get("이유") or "판정 이유 없음")[:400])
            _seg = "".join(
                f'<button type="button" data-v="{v}"'
                f'{" class=on" if _d == v else ""}>{t}</button>'
                for v, t in [("", "판정대로"), ("제외", "제외"), ("설치", "설치")])
            _rows.append(
                f'<div class="dc-row" data-room="{html.escape(_n)}"'
                f' data-init="{html.escape(_d)}" title="{_tip}">'
                f'<span class="dc-name">{html.escape(_n)}</span>{_chip}'
                f'<span class="seg">{_seg}</span></div>')
        if _rows:
            _dc_html.append(f'<div class="dc-grp">{_gt} · {len(_rows)}</div>'
                            + "".join(_rows))
    decide_card = ""
    if _dc_html:
        decide_card = (
            '<section class="card" id="dc-card"><h3>⚠ 확인필요 — 사람 확정</h3>'
            '<div class="meta">판정 확신이 낮은 실입니다. 실명에 마우스를 올리면 '
            'LLM의 판정 이유가 보입니다. 확정하면 출처가 \'사람\'(초록 배지)이 '
            f'되고 ⚠가 꺼집니다. — 확정 {_n_decided} · 대기 '
            f'{len(PENDING) - _n_decided}</div>'
            '<div id="dc-list">' + "".join(_dc_html) + '</div>'
            '<button id="dc-go" disabled>변경 없음</button>'
            '<div id="dc-msg" class="meta"></div></section>')

    decide_js = r"""
(function(){
 var B=__BASE__;
 // 건물 사실(설계 가정) 패널 토글 — 어디서든(공개 데모 포함) 동작한다.
 var bfB=document.getElementById('bf-btn'),bfP=document.getElementById('bf-pop');
 if(bfB&&bfP){
  bfB.onclick=function(){bfP.hidden=false;};
  document.getElementById('bf-x').onclick=function(){bfP.hidden=true;};
  bfP.onclick=function(e){if(e.target===bfP)bfP.hidden=true;};
 }
 // 확정 카드는 로컬 서버에서만 — 공개 데모(정적)에서는 숨긴다.
 if(window.__IS_LOCAL===false){
  var dc=document.getElementById('dc-card');
  if(dc)dc.style.display='none';
  return;
 }
 var go=document.getElementById('dc-go'),m=document.getElementById('dc-msg');
 if(!go)return;
 var rows=Array.prototype.slice.call(document.querySelectorAll('.dc-row'));
 function val(r){var b=r.querySelector('.seg button.on');
                 return b?b.getAttribute('data-v'):'';}
 function refresh(){
  var n=0;
  rows.forEach(function(r){
   var chg=val(r)!==r.getAttribute('data-init');
   r.classList.toggle('chg',chg); if(chg)n++;
  });
  go.disabled=(n===0);
  go.textContent=n?('✓ 변경 '+n+'건 저장 후 재실행'):'변경 없음';
 }
 rows.forEach(function(r){
  Array.prototype.forEach.call(r.querySelectorAll('.seg button'),function(b){
   b.onclick=function(){
    Array.prototype.forEach.call(r.querySelectorAll('.seg button'),
      function(x){x.classList.remove('on');});
    b.classList.add('on'); refresh();
   };
  });
 });
 refresh();
 go.onclick=async function(){
  var d={};
  rows.forEach(function(r){ d[r.dataset.room]=val(r); }); // ''=판정대로(철회)
  var ru=parseFloat((document.getElementById('rr-ru')||{}).value)||2.6;
  var rc=parseFloat((document.getElementById('rr-rc')||{}).value)||2.3;
  go.disabled=true; go.textContent='저장 후 재계산 중… (30초~1분)';
  m.textContent='결정 저장 → 배치 엔진 재실행 — 완료되면 자동 새로고침됩니다.';
  try{
   var res=await fetch('/decide',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({base:B,decisions:d,r_unit:ru,r_common:rc})});
   if(!res.ok)throw new Error('서버 응답 '+res.status);
   var j=await res.json();
   if(j.ok){location.reload();}
   else{throw new Error(j.error||'저장 실패');}
  }catch(e){
   go.disabled=false; refresh();
   m.innerHTML=(window.__SRV_HINT?window.__SRV_HINT():'서버 미연결')
    +' <span style="color:#94a3b8">('+e.message+')</span>';
  }
 };
})();
""".replace("__BASE__", json.dumps(base))

    replay_card = (f'<section class="card">{replay_html}</section>'
                   if replay_html else "")
    # 헤더 표시명: 파일 베이스에서 층 이름만 추출 (예: 510_지하1층_pit → 지하1층)
    _m = re.search(r"(지하\s*\d+층|기준층|옥탑|\d+층)", base)
    disp = _m.group(1) if _m else base
    page = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"/>
<title>{'스프링클러 헤드 배치 검토' if heads_only else '소방설비 배치 검토'} · {html.escape(disp)}</title><style>
:root{{--bg:#eef1f5;--panel:#fff;--ink:#1e2937;--mut:#64748b;--line:#e2e8f0;
--acc:#2563eb;--ok:#16803c;--warn:#c2410c}}
*{{box-sizing:border-box}}
html,body{{margin:0;height:100%;font-family:Pretendard,"Malgun Gothic",system-ui,sans-serif;
color:var(--ink)}}
#hd{{position:fixed;top:0;left:0;right:0;height:52px;z-index:20;display:flex;
align-items:center;gap:10px;padding:0 18px;background:#0f1d33;color:#fff;
box-shadow:0 2px 10px rgba(0,0,0,.25)}}
#hd .brand{{font-weight:800;font-size:15px;letter-spacing:.3px;white-space:nowrap}}
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
.tbl tr.sum td{{border-top:2px solid var(--line);border-bottom:none;font-weight:700}}
.dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;
vertical-align:-1px}}
.meta{{color:var(--mut);font-size:11.5px;margin-top:6px}}
.meta.warn-r{{color:#9a3412;background:#fff7ed;border:1px solid #fed7aa;
border-radius:6px;padding:5px 8px}}
.meta.hint code{{background:#eef2f7;border-radius:4px;padding:0 4px;
font-family:Consolas,monospace;font-size:11px}}
.rr-row{{display:flex;align-items:center;gap:7px;margin:5px 0;font-size:12.5px}}
.rr-row input{{width:70px;border:1px solid var(--line);border-radius:7px;
padding:5px 8px;font-size:12.5px;text-align:right}}
.rr-row .std{{color:var(--mut);font-size:11px;margin-left:auto}}
#dc-list{{max-height:250px;overflow-y:auto;margin:4px 0 8px;scrollbar-width:thin}}
.dc-grp{{font-size:10px;font-weight:800;color:var(--mut);letter-spacing:.08em;
margin:8px 0 3px;display:flex;align-items:center;gap:6px;white-space:nowrap}}
.dc-grp::after{{content:"";flex:1;height:1px;background:var(--line)}}
.dc-row{{display:flex;align-items:center;gap:6px;padding:4px 6px;margin:1px 0;
border-radius:8px;font-size:12px}}
.dc-row:hover{{background:#f8fafc}}
.dc-row.chg{{background:#eff6ff;box-shadow:inset 2px 0 0 var(--acc)}}
.dc-name{{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap;font-weight:600;cursor:help}}
.dc-cur{{font-size:9.5px;font-weight:800;padding:1px 6px;border-radius:99px;
white-space:nowrap}}
.dc-cur.ex{{background:#ffedd5;color:#c2410c}}
.dc-cur.in{{background:#dbeafe;color:#1d4ed8}}
.seg{{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;
flex-shrink:0}}
.seg button{{border:none;background:#fff;font-size:10px;padding:4px 6px;
cursor:pointer;color:var(--mut);border-left:1px solid var(--line);line-height:1.2}}
.seg button:first-child{{border-left:none}}
.seg button.on{{color:#fff;font-weight:700}}
.seg button.on[data-v=""]{{background:#64748b}}
.seg button.on[data-v="제외"]{{background:#c2410c}}
.seg button.on[data-v="설치"]{{background:#16803c}}
#dc-go{{width:100%;border:none;background:var(--acc);color:#fff;border-radius:9px;
padding:8px 0;font-size:12.5px;font-weight:700;cursor:pointer}}
#dc-go:disabled{{background:#cbd5e1;cursor:default}}
#bf-btn{{cursor:pointer}}
#bf-pop{{position:fixed;inset:0;background:rgba(15,23,42,.45);z-index:99;
display:flex;align-items:flex-start;justify-content:center;padding-top:70px}}
#bf-pop[hidden]{{display:none}}
#bf-box{{background:var(--panel);border-radius:14px;padding:16px 18px;width:370px;
max-height:72vh;overflow-y:auto;box-shadow:0 20px 50px rgba(15,29,51,.35);
font-size:12.5px;color:var(--ink)}}
#bf-box h4{{margin:0 0 8px;font-size:13px}}
#bf-x{{margin-top:10px;width:100%;border:1px solid var(--line);background:#f8fafc;
border-radius:8px;padding:6px 0;cursor:pointer;font-size:12px}}
#rr-go{{width:100%;margin-top:7px;border:none;background:var(--acc);color:#fff;
border-radius:9px;padding:8px 0;font-size:13px;font-weight:700;cursor:pointer}}
#rr-go:disabled{{background:#94a3b8;cursor:wait}}
.scrolltbl{{max-height:210px;overflow-y:auto;margin-top:6px;scrollbar-width:thin}}
details.plain{{border:none;background:none;padding:2px 0;margin:6px 0 0}}
details.plain summary{{color:var(--acc);font-size:12px;font-weight:600}}
#legend .lg-sec{{margin:2px 0 8px}}
#legend .lg-t{{font-size:10.5px;color:var(--mut);letter-spacing:.1em;font-weight:800;
margin:4px 0 3px}}
#legend .lg-grid{{display:grid;grid-template-columns:1fr 1fr;gap:2px 8px}}
.prm summary{{display:flex;gap:7px;align-items:baseline;cursor:pointer;
padding:3px 0;list-style:none}}
.prm summary::-webkit-details-marker{{display:none}}
.prm .pn{{flex:1;min-width:0}}
.prm b{{font-variant-numeric:tabular-nums}}
.src{{font-size:10px;font-weight:800;padding:1px 7px;border-radius:99px;
letter-spacing:.04em;flex:0 0 auto}}
.src.law{{background:#dcfce7;color:#15803d}}
.src.eng{{background:#e2e8f0;color:#475569}}
.src.hard{{background:#ffedd5;color:#c2410c}}
.src.llm{{background:#dbeafe;color:#1d4ed8}}
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
padding:1px 7px;font-size:10.5px;font-weight:800;margin-right:7px;vertical-align:1px}}
.vd.ok{{background:#dcfce7;color:#15803d}}
.vd.bad{{background:#fee2e2;color:#b91c1c}}
.vd.chk{{background:#ffedd5;color:#c2410c}}
.vd.na{{background:#f1f5f9;color:#94a3b8}}
.scrolltbl.tall{{max-height:300px}}
.dbrow{{cursor:pointer}}
.dbrow:hover td{{background:#f4f7fb}}
.exrow{{display:none}}
.exrow.on{{display:table-row}}
.exrow td{{background:#fbfcfe;padding:6px 8px}}
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
.cm pre{{background:#0f1d33;color:#dbe6f5;border-radius:8px;padding:8px 10px;
overflow-x:auto;font-size:11.5px;margin:6px 0;white-space:pre}}
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
#g-beam path{{stroke:#8d6e63;fill:none;stroke-width:2;stroke-dasharray:14 7;
vector-effect:non-scaling-stroke}}
#g-beam polygon{{stroke:#8d6e63;fill:#8d6e63;fill-opacity:.10;stroke-width:1.4;
vector-effect:non-scaling-stroke}}
#g-bwarn circle{{fill:none;stroke:#e65100;stroke-width:2.6;stroke-dasharray:5 4;
vector-effect:non-scaling-stroke}}
#g-rooms polygon,#g-rooms rect{{fill:none;stroke:#94a3b8;stroke-width:1;
vector-effect:non-scaling-stroke}}
#g-labels text{{font-size:520px;font-weight:600;fill:#5a6578;text-anchor:middle;
dominant-baseline:middle;paint-order:stroke;stroke:#fff;stroke-width:70px}}
#g-cov circle{{fill-opacity:.055;stroke-width:.9;vector-effect:non-scaling-stroke;
stroke-opacity:.55}}
#g-cov .unit{{fill:#1e88e5}} #g-cov .common{{fill:#8e24aa}}
#g-cov .win{{fill:#00acc1}} #g-cov .repair{{fill:#f6b900}}
#g-cov circle.sel{{stroke-width:2.6;stroke-opacity:1;fill-opacity:.16}}
#g-cov .unit{{stroke:#1e88e5}} #g-cov .common{{stroke:#8e24aa}}
#g-cov .win{{stroke:#00acc1}} #g-cov .repair{{stroke:#f6b900}}
#g-hd-unit circle{{fill:#1e88e5}} #g-hd-common circle{{fill:#8e24aa}}
#g-hd-win circle{{fill:#00acc1}} #g-hd-repair circle{{fill:#f6b900}}
#g-esc polyline{{fill:none;stroke:#2e7d32;stroke-width:2.4;stroke-dasharray:10 6;
vector-effect:non-scaling-stroke}}
#g-esc .worst{{fill:none;stroke:#d84315;stroke-width:3;vector-effect:non-scaling-stroke}}
#g-ext path{{fill:#e53935}}
#g-outlet .sq{{fill:#fb8c00}}
#g-outlet .or5{{fill:#fb8c00;fill-opacity:.05;stroke:#fb8c00;stroke-dasharray:8 6;
stroke-width:1;vector-effect:non-scaling-stroke}}
#g-fix line{{stroke:#d32f2f;stroke-width:3;vector-effect:non-scaling-stroke}}
.fx{{display:none}}
#replay{{background:none;border:none;padding:0;margin:0}}
#replay button{{border:1px solid #bcd2f0;background:#fff;border-radius:8px;
padding:5px 12px;cursor:pointer;font-size:12.5px;margin-right:4px}}
#replay button#rp-start{{background:var(--acc);color:#fff;border-color:var(--acc);
font-weight:700}}
#replay select{{border:1px solid #bcd2f0;border-radius:8px;padding:4px 6px;font-size:12px}}
#rp-st{{margin:7px 0 5px 18px;padding:0;font-size:12px;color:#98a0ad;line-height:1.7}}
#rp-st li.now{{color:#1e5cb3;font-weight:700}}
#rp-st li.done{{color:#1b8a3f}}
#rp-st li.done::after{{content:' ✓'}}
#rp-msg{{font-size:12px;color:#333;background:#fff;border-radius:6px;
padding:5px 8px;min-height:28px}}
details.hot{{outline:2px solid #f6b900}}
#g-rooms.hot polygon,#g-rooms.hot rect{{stroke:#1e5cb3;stroke-width:2}}
</style></head><body>
<header id="hd">
 <span class="brand">🔥 FRAN 소방배치 검토<small>{'스프링클러 헤드' if heads_only else '소방설비 종합'} · {html.escape(disp)}</small></span>
 <span class="sp"></span>
 {chips_html}
</header>
{facts_html}
<div id="stage"><svg id="svg" viewBox="{vb}" xmlns="http://www.w3.org/2000/svg">
{groups_svg}
</svg></div>
<aside id="left">
 {replay_card}
 <section class="card" id="rr-card"><h3>재계산 — 헤드 수평거리</h3>
  <div class="rr-row">세대 실 r
   <input id="rr-ru" type="number" step="0.1" min="1.5" max="5.0"
    value="{R_UNIT / 1000:.1f}"/> m <span class="std">기준 2.6</span></div>
  <div class="rr-row">공용부 r
   <input id="rr-rc" type="number" step="0.1" min="1.5" max="5.0"
    value="{R_COMMON / 1000:.1f}"/> m <span class="std">기준 2.3</span></div>
  <button id="rr-go">⟳ 재실행 — 헤드 재배치</button>
  <div id="rr-msg" class="meta">기준(2.6/2.3m) 상회분은 성능 인정 헤드(확대살수형 등)
   사용 전제. 변경 후 재실행하면 배치·검증·표가 전부 갱신됩니다.</div>
 </section>
 {decide_card}
 <section class="card"><h3>헤드 현황</h3>{head_tbl}{room_tbl}</section>
 <section class="card"><h3>표시 항목</h3><div id="legend">{legend_html}</div></section>
</aside>
<aside id="right">
 <section class="card"><h3>적용 파라미터 — 값의 출처</h3>
  <div id="params">{params_html}</div></section>
 <section class="card"><h3>실 분류 — 법정 노드 바인딩</h3>
  <div id="roomclass">{roomclass_html}</div></section>
 <section class="card"><h3>배치 로직</h3><div id="logic">{logic_html}</div></section>
 <section class="card"><h3>{'법적 검토 — 결과 대조' if heads_only else '법적 근거'}</h3>
  <div id="legal">{legal_html}</div></section>
 {legal_extra}
</aside>
<div id="chatdock" class="closed">
 <div id="chathead">💬 AI 검토 어시스턴트 <small>배치 결과에 대해 질문·논의</small>
  <span class="sp"></span>
  <button id="okeyreset" title="저장된 API 키 삭제">키 재설정</button>
  <button id="chattoggle">열기 ▴</button></div>
 <div id="chatbody">
  <div id="chatmsgs"></div>
  <div id="chatkey"><span>OpenAI API 키</span>
   <input id="okey" type="password" placeholder="sk-..."/>
   <button id="okeysave">저장</button></div>
  <div id="chatin"><textarea id="cin" rows="2"
   placeholder="예: 제연휀룸에 헤드 5개가 최소인 이유는?"></textarea>
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
document.querySelectorAll('.dbrow').forEach(function(r){{
r.onclick=function(){{var ex=document.getElementById('ex'+r.getAttribute('data-i'));
if(ex)ex.classList.toggle('on');}};}});
}})();
{replay_js}
{chat_js}
{rerun_js}
{decide_js}
</script></body></html>"""

    name = f"{base}_head_layout.html" if heads_only else f"{base}_fire_layout.html"
    op = os.path.join(FO, "output", name)
    open(op, "w", encoding="utf-8").write(page)
    print(f"출력: {op}")


if __name__ == "__main__":
    main()
