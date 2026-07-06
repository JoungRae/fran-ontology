# CAD 평면도 → BOT 온톨로지 파이프라인

CAD 도면 JSON을 경량화·시각화하고, **BOT(Building Topology Ontology)** RDF 그래프로 변환한다.

## 파이프라인

```
data/<도면>.json              # 원본 CAD 추출 JSON (Entities=dict 리스트, 헤더에 Reference_*_Layer)
  │
  ├─ flatten_json.py  ──────▶ data/<도면>_flat.json      # 경량화(컬럼형, ~20%)
  │     └─ render.py  ──────▶ output/<도면>.html         # 인터랙티브 도면 뷰어
  │
  ├─ plan_rooms_rect.py ────▶ output/<도면>_rooms_rect.json   # 방 경계 사각형 (bot:Space)
  ├─ layer_classify.py  ────▶ output/<도면>_layer_classification.json  # 레이어 카테고리 (bot:Element) [GPT]
  │
  └─ build_bot.py  ─────────▶ output/<도면>.ttl          # BOT 그래프 (Turtle, 단일 층)
        ├─ bot_query.py ────▶ 콘솔               # SPARQL 검증 + 예시 질의
        └─ bot_viz.py  ─────▶ output/<도면>_bot.html      # 위상 시각화(도면 위 방/인접/개구부)

  build_building.py ────────▶ output/building.ttl        # 여러 층을 한 Building에 수직 스택
        └─ stack_viz.py ────▶ output/building_stack.html  # 표고 스케일 건물 단면

  compliance_report.py ─────▶ output/compliance_report.html  # 법령 검토 리포트(실무자 UI/UX)
```

## 법령 검토 (compliance_report.py)

`D:\Python_test\cons_law`(한국 건축법령 컴플라이언스 파이프라인)의 **평가 엔진
`evaluator.py`(순수 파이썬, DB 불필요)** 를 importlib 로 재사용하고, 룰 카탈로그
`rules/checks_demo.json`(cons_law 샘플: 실제 법령 근거 12개 + 일부 데모)을 로드한다.

흐름: BOT 그래프에서 `project`(용도·층수·세대수·높이; 층수·높이는 단면도 유래)와
`drawing`(방 면적 등, 직사각형 근사) 값을 도출 → `evaluate_project()` 로
`적합/부적합/검토필요/해당없음` 판정 → 상태색·분야필터·검색·근거조문·필요데이터·인쇄를
지원하는 자체완결형 HTML 렌더.

```bash
"$PY" compliance_report.py                       # building.ttl 기준
"$PY" compliance_report.py --ttl output/1층.ttl   # 단일 층 기준
```

정량 규정(면적·폭)은 근사 지오메트리라 최종 실측 대조가 필요하고, 대지·지역·주차 등
도면 밖 정보는 검토필요/해당없음으로 분류된다. 실제 운영 시 `rules/`를 cons_law DB의
전체 checks 로 교체하면 검토 범위가 확장된다.

## 전수 자동검토 (derive_terms.py + evaluate_full.py)

cons_law DB(PostgreSQL)의 **전체 체크를 전수 평가**하는 확장 파이프라인.

1. `derive_terms.py` — BOT/CAD 데이터에서 **표준 온톨로지 용어(ontology_terms) 값 19개** 도출
   → `output/derived_terms.json`. 핵심: 층 footprint(방 사각형 합집합)로 연면적·건축면적,
   ELEV.홀 짧은 변=복도 유효폭, 문 스윙호 반경=문폭, 세대수(층당 현관 수×층 반복).
2. `evaluate_full.py [--db]` — term_aliases 로 룰 필드명까지 확장 후 active 체크 전수 평가
   → `output/compliance_report_db.html` (KPI·분야×판정 매트릭스·검색/필터·근거조문).
   `--db` 시 결과를 cons_law `projects/analyses/findings` 테이블에 정식 적재.
   리포트는 **스플릿 워크스페이스 UI**: 좌측 검토 항목(탭: 항목/분야요약/입력값, 검색·필터,
   컴팩트 카드 클릭=펼침+실 강조) / 우측 도면 상시 고정(층 탭, 줌·팬, 실은 관련 판정
   상태색으로 표시 — 부적합=빨강·검토필요=주황·적합=초록). **양방향 연동**: 항목 클릭→실
   강조·층 자동전환, **실 클릭→관련 항목만 필터**. HTML 골격은 `report_template.html`
   (`__TOKEN__` 치환 방식 — 중괄호 이스케이프 불필요)에 분리.

**후처리(postprocess)** 가 LLM 추출 룰의 구조적 거짓 fail 을 재분류한다:
분기 필드 부족→검토필요, 문자열 기준값(existing_* 참조)→검토필요,
용도 분류 불일치(value_in on use)→해당없음, 정의·분류성 조항 의심은 '확인要' 태그.
말단 필드명 자동 확장은 하지 않는다(height_m 충돌로 거짓 판정 유발 — aliases만 신뢰).

2026-07-02 결과(체크 8,278 active 기준): pass 13 · fail 35 · 검토필요 3,078 · 해당없음 5,152.
대표 실질 발견: **복도 유효너비 1.77m < 1.8m** (공동주택 양옆거실 복도 기준, 근사값 — 실측 확인要).

## 다중 층(수직 스택)

`build_building.py` 의 `STACK` 리스트에 (base, 라벨, levelIndex, 층고mm)를 정의하면 각 층을
`build_bot.build_storey()` 로 만들어 하나의 `bot:Building` 아래 쌓는다. 층은 `fran:levelIndex`·
`fran:elevationMm`(1층 바닥=0 기준 누적)·`fran:heightMm` 로 표고를 갖고 `fran:aboveStorey` 로
상하 연결된다. 개체 URI는 층 접두사(`<base>_Room_..`)로 층 간 충돌을 막는다.

```bash
"$PY" build_building.py        # 기본 스택: 지하1층·1층·기준층(2~15층)
"$PY" bot_query.py building     # 층 스택 + 전체 검증
"$PY" stack_viz.py              # 건물 단면 HTML
```

## 실행 (예: 1층)

```bash
PY="D:/Python_test/fran_consist_cad_json/.venv/Scripts/python.exe"   # openai·dotenv·matplotlib·rdflib 설치된 venv

# 1) 경량화 + 도면 뷰어
"$PY" flatten_json.py "data/1층.json" -o "data/1층_flat.json"
"$PY" render.py "data/1층_flat.json" -o "output/1층.html"

# 2) 시맨틱 추출 (원본 1층.json 대상; layer_classify 는 OPENAI_API_KEY 필요)
"$PY" plan_rooms_rect.py "D:/절대경로/data/1층.json"     # cwd=fran_consist_cad_json 에서 실행 권장(로컬 모듈 의존)
"$PY" layer_classify.py  "D:/절대경로/data/1층.json"

# 3) BOT 그래프 생성 + 검증 + 시각화
"$PY" build_bot.py 1층
"$PY" bot_query.py 1층
"$PY" bot_viz.py 1층
```

> `plan_rooms_rect.py` / `layer_classify.py` 는 원본 프로젝트(`fran_consist_cad_json`)의
> 로컬 모듈(`compare_drawings` 등)과 `.env`(OPENAI_API_KEY)에 의존하므로, 그 디렉터리를 cwd로
> 두고 입력만 절대경로로 지정해 실행한 뒤 `output/*.json` 두 개를 이 프로젝트 `output/`으로 복사한다.

## 온톨로지 매핑 (CAD → BOT)

| BOT/도메인 | 소스 | 도출 |
|---|---|---|
| `bot:Site`/`bot:Building`/`bot:Storey` | Title/파일 | 상수 |
| `fran:DwellingUnit ⊑ bot:Space` | 내부 문 연결요소 | 문(Door)으로 이어진 방 블록 = 한 세대(미러쌍은 `fran:dwellingCount`로 표기) |
| `bot:Space` | `plan_rooms_rect` 방 사각형 | 방이름 시드→벽 확장 |
| `fran:Wall`(+`fran:structural`)`/Door/Window/Column/Stair/Elevator ⊑ bot:Element` | `layer_classification` 카테고리 | 참조레이어/GPT |
| `bot:adjacentZone` | 방 rect가 벽 두께(<400mm)만큼 마주봄 | 결정적 기하 |
| `bot:Interface`(+`interfaceOf`,`hasElement`) | 두 방 경계 존에 문/창 형상 | 개구부 점 격자 |
| 경량 지오메트리 | rect/선분/점 | `geo:asWKT` (CAD mm 좌표) |

## 알려진 한계

- 방은 **직사각형 근사** — L자/비정형 방은 오차. 정밀 면적엔 폴리곤화 필요.
- 세대 경계는 **문 연결 기반 휴리스틱** — 미러쌍(세대벽 공유+문 근접)이 한 블록으로 묶일 수 있어
  블록당 실제 주호 수는 `fran:dwellingCount`(=블록 내 현관 수)로 별도 기록. 정확한 개별 세대
  분할은 세대벽 폴리곤(P3)이 필요.
- `bot:Element` 는 좌표만 보유(재료/방화등급 없음) — 필요 시 OPM/PROPS 확장.
