# FRAN Ontology — CAD 도면 → 온톨로지 → AI 법규 검토·소방 배치 자동화

건축 CAD 평면도(JSON 내보내기)를 입력받아 **BOT(W3C Building Topology Ontology)** 기반
그래프로 변환하고, 이를 근거로 **스프링클러 헤드 자동 배치**와 **피난 경로 검토**를
법령 조항과 대조해 판정까지 내주는 파이프라인입니다.

> 🔗 **라이브 데모**: https://joungrae.github.io/fran-ontology/

## 이게 뭔가요

```
CAD 도면(JSON) ──① AI 레이어 분류──▶ 벽·창·문·승강기·계단
              ──② 방 인식(flood-fill)──▶ 실(폴리곤)·무명실·PIT
              ──③ BOT 온톨로지 그래프──▶ 층·세대·인접·문·보·보행거리 (RDF)
                        │            ▲
                        │            └─③' 법령 레이어: 실별 판정·근거 조문 (RDF)
                        │                 ← 법령 DB 규칙 × 실명 매칭
                        ├─④ 소방 헤드 자동 배치 + 법적 판정 리포트
                        └─⑤ 피난 경로(보행거리) 자동 검토 리포트
```

CAD는 "선의 좌표 목록"일 뿐이고, 법령은 자연어 문장일 뿐입니다. 이 둘을 사람 없이
잇기 위해 **온톨로지(위상 그래프)** 를 다리로 놓고, 그 위에서 결정적 알고리즘으로
배치·검증을 수행합니다.

## 핵심 기능

- **AI 레이어 분류**: GPT가 레이어를 벽/창/문/승강기/계단으로 분류(3회 투표 + 기하 검증)
- **방 자동 인식**: 실명 텍스트 + flood-fill로 실제 방 형상(폴리곤) 인식, 라벨 없는
  밀폐 공간도 "무명실"로 자동 승격
- **BOT 온톨로지 그래프**: 인접·개구부(Interface)·세대 그룹핑·계단 보행거리·층간 수직
  연결을 기하 추론으로 자동 도출 (rdflib/Turtle, GeoSPARQL WKT)
- **법령 레이어**: 법령 DB의 장소 규칙을 실명과 매칭해 실 노드에 판정(제외/공용/세대)과
  근거 조문 URI를 부착 — "이 실은 왜 헤드가 없나"가 그래프에서 추적된다
  ([README_LEGAL.md](README_LEGAL.md))
- **스프링클러 헤드 자동 배치** (`fire_layout.py`)
  - 구역별 최대커버(Greedy Set Cover 근사) + 가시선(LoS) 판정
  - 구조도 정합 보(beam) 회피(0.6m 이격), Lloyd 재정렬로 간격 균등화
  - 100mm 셀 단위 전수 검수로 커버리지 수학적 보증
  - 법규 판정 패널: 적용값 vs 법정 기준 자동 대조(적합/기준초과/확인필요)
- **피난 경로 검토** (`evac_report.py`): 계단 출입구 자동 추출 → 보행 거리장 →
  실별 30m/50m(내화 완화) 판정
- **리포트 내장 AI 어시스턴트**: 배치 로직·법조문을 컨텍스트로 받아 결과를 해설
  (판정에는 관여하지 않는 읽기 전용 역할)

## AI를 이렇게 썼습니다

| 역할 | AI가 하는 일 | AI가 안 하는 일 (의도적 설계) |
|---|---|---|
| 도면 이해 | 레이어 분류(3-투표 + 기하 검증) | 좌표 계산 — 기하는 전부 결정적 코드 |
| 법령 구조화 | 조항 → 기계평가 가능한 룰 추출 | 최종 판정 |
| 결과 해설 | 배치 결과·조문을 근거로 대화형 설명 | 판정 변경 (읽기 전용) |

> **설계 철학: "판정은 코드가, 이해와 해설은 AI가."**
> 법적 판정을 LLM에 맡기면 환각·비재현성 문제가 생기므로, 판정은 재현 가능한
> 결정적 규칙으로 남기고 AI는 입력 구조화와 출력 해설만 담당합니다.

## 사용해보기

리포트(`output/*_head_layout.html`, `output/*_evac_layout.html`)는 **완전히 자립적인
단일 HTML 파일**입니다 — 그냥 브라우저로 열면 됩니다. 다운로드하거나 `docs/` 폴더의
데모를 열어보세요.

리포트 하단의 **AI 검토 어시스턴트**를 쓰려면 본인의 OpenAI API 키를 입력창에 넣으세요.
**키는 브라우저의 localStorage 에만 저장되고, 어떤 서버로도 전송되지 않습니다**
(OpenAI API에 직접 요청). 이 저장소에는 어떤 API 키도 포함돼 있지 않습니다.

## 직접 돌려보기 (파이프라인 재실행)

```bash
pip install -r requirements.txt

# 1) 방 인식
python plan_rooms_rect.py "data/지하1층_pit.json"
python plan_rooms_flood.py "data/지하1층_pit.json" --merge-into-rect

# 2) (선택) 구조도 정합 — 보 위치 이식
python align_beams.py "data/지하1층_구조_MLINE버전.json" "data/지하1층_pit.json" \
    --anchor "S-CON-HID" --beam-lines "S-BEEM(부대)" --beam-polys "S-BTS(거더)" --depth 900

# 3) 헤드 배치 리포트 생성
python fire_layout.py "지하1층_pit" --heads
#   반경 조절(성능 인정 헤드 등): --r-unit 3.2 --r-common 2.6

# 4) 피난 경로 리포트 생성
python evac_report.py "지하1층_pit"
#   내화구조 완화(50m) 적용: --fire-resist

# 5) 브라우저에서 반경을 바꿔가며 재계산하고 싶다면 로컬 서버 실행
python fire_server.py 지하1층_pit
```

`layer_classify.py`(GPT 레이어 분류), `fetch_head_checks.py`(법령 DB 조회)는 각각
OpenAI API 키, PostgreSQL 법령 DB 접속이 필요합니다 — `.env.example` 참고.

## 저장소 구성

```
plan_rooms_rect.py / plan_rooms_flood.py   방 인식(광선 캐스팅 + flood-fill)
layer_classify.py                          GPT 레이어 분류
build_bot.py / build_building.py           BOT 온톨로지 그래프 생성(단층/다층)
match_rules_rooms.py                       법령 장소 규칙 × 실명 매칭(LLM + 캐시)
annotate_legal.py                          매칭 판정 → 온톨로지 법령 레이어 TTL
derive_head_params.py                      법령 수치 규칙 → 헤드 배치 파라미터
derive_terms.py                            법령 평가용 표준 term 도출
align_beams.py                             구조↔건축 도면 정합, 보 이식
fire_field.py                              소방 배치 기하 엔진(거리장·greedy·재정렬)
fire_layout.py                             헤드 배치 리포트(메인)
evac_report.py                             피난 경로 검토 리포트
fire_server.py                             리포트 재계산용 로컬 서버
evaluate_full.py / compliance_report.py    법령 전수 평가·종합 검토 리포트
data/  output/                             예시 도면 1개(지하1층 부대시설)·산출물
```

## 한계 (정직하게)

- 장소 판정은 LLM 매칭 + 캐시 — 재현은 되지만 첫 판정은 비결정적. ⚠ 표시된 실은
  사람이 확정해야 하며, 제외(헤드 미설치)는 이중 확신일 때만 적용된다
- 천장고·반자 등 단면 데이터 미연계 (반사판-보 수직거리 표 등은 보류)
- 100mm 래스터·직사각형 근사, 도면 라벨 품질에 의존
- 검토 초안 도구이며 시공 설계·최종 승인을 대체하지 않습니다

## 라이선스

MIT — [LICENSE](LICENSE) 참고
