# 법령 → 실 → 온톨로지 → 배치

법령 DB(`cons_law`)의 규칙을 도면의 실(室)에 붙이고, 그 결과를 온톨로지에 기록해
스프링클러 배치 알고리즘이 소비하게 하는 경로를 설명한다. 기하 파이프라인은
[README_BOT.md](README_BOT.md) 참조.

## 왜 이렇게 나눴나

법령 규칙은 성격이 셋이고 소비 경로도 셋이다. 하나의 파이프로 억지로 합치지 않는다.

| 규칙 성격 | 예 | 소비 경로 |
|---|---|---|
| **장소** 규칙 | "계단실·화장실에는 헤드를 설치하지 않을 수 있다" | 실×규칙 매칭 → **온톨로지 노드 주석** |
| **수치** 규칙 | "수평거리 2.6m 이하", "보에서 0.6m 이격" | → `head_params.json` → **배치 알고리즘** |
| **속성** 규칙 | "내화구조인 경우", "16층 이상 공동주택" | → 건물 프로필 → **적용 조문 선별** |

"보와 가장 가까운 헤드" 같은 규칙은 실 노드와 만나지 않는다 — 기하·부재 규칙이라
파라미터로 흘러야 맞다. 온톨로지는 세 갈래를 모두 통과시키는 파이프가 아니라,
장소 판정의 **정본**이자 나머지 결과의 **기록처**다.

## 흐름

```
cons_law DB (legal_rule · rule_condition · rule_requirement)
  │
  ├─ derive_head_params.py ─▶ output/head_params.json        수치 규칙 → 배치 파라미터
  │      (건물 프로필 data/building_profile.json 이 조건 분기를 결정: 내화→2.3m)
  │
  └─ match_rules_rooms.py ──▶ output/<도면>_room_bindings.json  장소 규칙 × 실명 판정
         │                     (+ data/rule_room_cache.json 등 캐시)
         ▼
      annotate_legal.py ────▶ output/<도면>_legal.ttl        판정 → RDF 법령 레이어
         │                     (기하 output/<도면>.ttl 과 같은 실 URI 를 공유)
         ▼
      fire_layout.py ───────▶ output/<도면>_fire_layout.html  배치 + 판정 리포트
```

기하와 법령을 **별도 TTL 파일**로 두고 파싱 시점에 병합한다. 판정을 사람이 고치거나
매칭을 다시 돌려도 기하 그래프는 재생성할 필요가 없다.

## 실행

```bash
# 법령 DB 를 쓰는 단계 — cons_law venv + .env(PG_DSN·OPENAI_API_KEY)
CL="D:/Python_test/cons_law/.venv/Scripts/python.exe"
"$CL" derive_head_params.py
"$CL" match_rules_rooms.py 지하1층_pit

# 그래프·배치 단계 — rdflib·numpy·matplotlib 있는 venv
PY="D:/Python_test/fran_consist_cad_json/.venv/Scripts/python.exe"
"$PY" build_bot.py 지하1층_pit         # 기하 TTL (+ _beams.json 있으면 fran:Beam 편입)
"$PY" annotate_legal.py 지하1층_pit    # 법령 TTL
"$PY" fire_layout.py 지하1층_pit       # 배치 리포트
```

`match_rules_rooms.py` 는 캐시가 채워져 있으면 LLM 을 호출하지 않는다(재컴파일 $0).
프롬프트를 고치면 `MATCH_PROMPT_V` 스탬프가 달라져 캐시가 자동 무효화된다.

## 장소 규칙 × 실 매칭 (match_rules_rooms.py)

실명을 동의어 사전이나 키워드 목록에 대보지 않는다. 규칙 원문을 그대로 주고
LLM 에게 "이 실들 중 어디가 해당하는가"를 묻는다. 사전을 만들면 그게 곧 하드코딩이고,
법 개정 때마다 사전이 무효가 되기 때문이다.

1. **선별(triage)** — 장소가 등장하는 규칙만 남긴다. 단, `permission`(면제) 규칙은
   진술문에 장소가 안 보여도 항상 통과시킨다. "설치하지 않을 수 있다" 를 놓치면
   헤드를 잘못 빼는 게 아니라 잘못 더 놓는 쪽으로 기울어 안전하지만, 판정 근거가
   사라져 설명이 불가능해진다. 확률이 아니라 구조로 보장한다.
2. **질의** — 규칙마다 `context`(조 제목 + 지배 문장 = 상위 문맥)와 원문을 함께 주고,
   실 명단 **전원**에 대해 해당/비해당 판정을 받는다(선택적으로 몇 개만 답하면
   누락이 조용히 발생한다). 결과는 `(rule_id, 실명)` 캐시에 증분 저장.
3. **실 유형 판정** — 실명이 물리적으로 '실'인지 '샤프트'인지 별도로 묻는다.
   규칙 해석과 분리해야 안정적이다.
4. **컴파일** — deontic(obligation/permission)과 requirement 로 동작을 정한다.

**안전 원칙**: 제외(헤드 미설치)는 이중 확신을 요구한다 — 면제 규칙 매칭이 high
**이고** 샤프트 판정이 high 일 때만. 하나라도 낮으면 설치 + ⚠ 표시. 헤드를 잘못
빼는 쪽이 잘못 더 놓는 쪽보다 위험하다.

**정책 분리**: "생략 가능한 장소를 실제로 생략할지"는 법이 정해주지 않는 설계 판단이다.
코드에 숨기지 않고 `data/placement_policy.json` 으로 꺼내 두고 화면에 표시한다.
현재 값은 `"생략가능_실제제외": "비출입구획만"` — 샤프트류만 실제 제외하고
화장실·계단 등은 강화 적용(설치)한다.

## 법령 레이어 어휘 (annotate_legal.py)

```turtle
inst:Room_17 a bot:Space ;
    rdfs:label "/TPS"@ko ;
    fran:verdict inst:Verdict_SprinklerHead_Room_17 .

inst:Verdict_SprinklerHead_Room_17 a fran:RuleVerdict ;
    fran:aboutSystem fran:SprinklerHead ;   # 대상 설비 — 피난·경보 판정과 안 섞이게
    fran:action "제외"@ko ;                 # 제외 / 세대 반경 2.6m / 일반(공용 반경)
    fran:appliedRule inst:Rule_11382 ;      # ★ 이 판정을 실제로 결정한 규칙
    fran:appliedPolicy inst:Policy_지하1층_pit ;   # ★ 그때 적용된 설계 정책
    fran:source "규칙매칭"@ko ;
    fran:confidence "high" ;
    fran:needsReview false ;
    fran:reason "#11286 …"@ko ;
    fran:basis inst:Basis_SprinklerHead_Room_17_0 ;  # 검토한 규칙 전체(순서 보존)
    fran:omittableRule inst:Rule_11265 .    # 생략 가능하나 적용하지 않은 후보
```

| 항목 | 뜻 |
|---|---|
| `fran:RuleVerdict` | 실 하나 × 설비 하나에 대한 법령 판정 |
| `fran:LegalRule` | cons_law `legal_rule` 행 (URI = `inst:Rule_<rule_id>`) |
| **`fran:appliedRule`** | **판정을 실제로 결정한 규칙** — 제외를 정당화한 규칙이 여럿이면 전부 |
| **`fran:appliedPolicy`** | 그 판정에 설계 정책이 개입했음 (법이 아닌 판단이 섞인 지점) |
| `fran:basis` → `fran:rule` | 이 실에 적용된다고 판정된 규칙 **전체**. `fran:order` 로 순서 보존 |
| `fran:omittableRule` | 생략 가능 근거이나 정책상 적용하지 않은 규칙(후보) |
| `fran:radiusMM` | 규칙이 정한 이 실의 헤드 수평거리 |
| `fran:action` / `source` / `confidence` / `needsReview` | 동작·출처·확신도·사람 확인 필요 |
| `fran:placementPolicy` → `fran:policyEntry` | 정책 노드와 키·값 항목 |
| `fran:Beam` (+`depthMM`, `bot:intersectingElement`) | 구조도 정합 보 — 기하 TTL 쪽. 지나가는 실에 연결됨 |

**`basis` 와 `appliedRule` 은 다르다.** basis 는 "이 실에 걸리는 규칙들"(감사 추적),
appliedRule 은 "그래서 이렇게 판정했다"의 근거다. 처음엔 basis 만 있었는데,
제외된 실의 basis 에는 제외와 무관한 조문만 들어 있었다 — 그럴듯한 오답이었다.

**주의 — 트리플은 집합이다.** 근거를 라벨 문자열로만 적으면 같은 조문 라벨의 규칙
여럿이 하나로 합쳐져 조용히 사라진다(초기 구현에서 25건 → 22건). 그래서 근거는
반드시 `rule_id` 기반 URI 로 적는다. 왕복(JSON → TTL → JSON) 무손실을 검증한다.

**블랭크 노드를 쓰지 않는다.** 근거 노드를 익명(`[ ... ]`)으로 두면 직렬화 순서가
실행마다 달라져 그래프가 동형이어도 파일이 매번 바뀌고(git 잡음), 파일을 나눠
쓸 때 같은 근거로 병합되지도 않는다. `inst:Basis_<설비>_<실>_<순번>` 으로 이름을 준다.
같은 이유로 생성 시각도 `now()` 가 아니라 **입력 파일의 수정 시각**을 적는다 —
입력이 그대로면 출력도 바이트 단위로 같아야 한다.

**개체 URI 에는 도면 접두사가 붙는다** — `inst:지하1층_pit_Room_5`. 방 번호는
도면별 리스트 순번이라, 접두사가 없으면 다른 도면의 같은 번호 방이 한 개체로
합쳐진다(실측 121개 충돌). 단층 TTL 과 다층 `building.ttl` 이 같은 규칙을 써서
두 그래프의 방 URI 가 일치한다. 단지(`Site_…`)만 의도적으로 공유된다.

**IRI 로컬명은 반드시 sanitize 한다.** 도면 Title 이 `55A/55AS(2~3층)` 처럼 슬래시나
공백을 담고 있으면 rdflib 이 직렬화 단계에서 통째로 실패한다. 사람이 편집하는
정책 파일의 키를 술어(IRI)로 쓰는 것도 같은 이유로 금지 — 키는 값으로 둔다
(`fran:policyEntry` → `fran:key`/`fran:value`).

## 질의 예

```sparql
# 헤드를 제외한 실과 그 결정 근거
SELECT ?name ?rule WHERE {
  ?s a bot:Space ; rdfs:label ?name ; fran:verdict ?v .
  ?v fran:action "제외"@ko ; fran:appliedRule ?r .
  ?r rdfs:label ?rule .
}
```

```sparql
# 특정 실을 제약하는 보
SELECT ?name (COUNT(?b) AS ?beams) WHERE {
  ?s a bot:Space ; rdfs:label ?name ; bot:intersectingElement ?b .
  ?b a fran:Beam .
} GROUP BY ?name
```

```sparql
# 사람 확정이 필요한 실
SELECT ?name ?reason WHERE {
  ?s rdfs:label ?name ; fran:verdict ?v .
  ?v fran:needsReview true ; fran:reason ?reason .
}
```

## 소비 (fire_layout.py)

`load_legal_ttl()` 이 법령 TTL 을 읽어 실명별 판정 dict 로 복원한다. 우선순위는
`_legal.ttl` → `_room_bindings.json` → 키워드 폴백이고, 폴백으로 내려간 실은 리포트
'실 분류' 카드에 **하드코딩** 배지로 표시된다. 화면에서 안 보이는 하드코딩은 없다.

배지: `법령`(초록) · `LLM`(파랑) · `하드코딩`(주황). ⚠ 는 사람 확정 대기.

## 현재 상태 (지하1층_pit, 2026-08-10)

- 기하 TTL 1,373 트리플 (Space 30 · Beam 68 · adjacentZone 25쌍 · 세대 0 · 공간군 2)
- 법령 TTL 5,746 트리플 (실명 25 → Space 30 에 판정 부착)
- 판정: 제외 6(샤프트류 — `/TPS`·`ELEV.`·`EPS/TPS`·`PS`·`급기DA`·`배기DA`) · 일반 19
- 사람 확정 대기 3: `PIT`(헤드 30개가 걸림) · `실외기실PD` · `AV`
- 배치 결과: 헤드 94 · 전셀 커버 · 보 이격 미달 0

## 한계

- **정의부 단어집의 커버리지**: 스프링클러 기준이 실제로 참조하는 장소(계단실·화장실·
  목욕실)는 어느 법에도 정의가 없다 — 일반어라서. 정의 조항만으로 장소 어휘를 만들면
  제외 장소류가 통째로 빠진다. 게다가 "거실"은 건축법·에너지기준·NFPC 608 이 서로
  다르게 정의한다.
- **규칙 쪽 정규화**: 실→규칙 방향은 매칭으로 풀렸지만, 규칙의 장소 표현은 정규화된
  노드가 아니라 구절이다. 노드 조인만으로 규칙을 연결하려면 규칙 쪽도 같은 어휘로
  정규화해야 한다.
- **LLM 비결정성**: 같은 입력에 항상 같은 답이 아니다. 캐시로 재현성을 확보하고,
  구조적으로 보장할 수 있는 것(면제 규칙 항상 포함)은 확률에 맡기지 않는다.
- 판정은 검토 초안이다. ⚠ 항목은 사람이 확정해야 한다.
