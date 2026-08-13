# -*- coding: utf-8 -*-
"""규칙 → 실(방) 직접 매칭. 손으로 쓴 장소 목록 없이.

흐름 (사용자 설계):
  1. 장소가 나오는 규칙 선별 — LLM 이 규칙 문장을 보고 "특정 장소에 관한
     규칙인가" 를 판정. 도면과 무관하므로 **규칙 단위로 영구 캐시**.
  2. 규칙마다 도면의 실 목록을 주고 "이 규칙의 장소에 해당하는 실은?" 을
     질의. **원문을 통째로** 준다 — 괄호 정의("파이프·덕트를 통과시키기 위한
     구획된 구멍에 한한다")와 단서가 판단 재료다. 낱말 목록만 줬을 때
     EPS/TPS 를 놓친 게 그 교훈. (rule_id, 실명) 단위 캐시라 다음 도면에서
     같은 실명은 재질의 없음.
  3. 결과(rule_id × 실)를 컴파일 — 효과는 규칙 자신의 구조에서 나온다:
     · permission "설치하지 않을 수 있다" → 제외 후보
       (실제 제외는 정책: 사람이 출입하지 않는 구획만. data/placement_policy.json)
     · obligation + distance 사양       → 그 실의 반경
     출력은 fire_layout 이 이미 읽는 <base>_room_bindings.json 모양.

실행: cons_law venv
  D:/.../cons_law/.venv/Scripts/python.exe match_rules_rooms.py 지하1층_pit
"""
import io
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"D:\Python_test\cons_law\src")

from cons_law.config import connect          # noqa: E402
from cons_law.llm import chat_json, usage_line   # noqa: E402

FO = os.path.dirname(os.path.abspath(__file__))
TRIAGE = os.path.join(FO, "data", "rule_place_triage.json")   # 1단계 캐시 (영구)
MATCH = os.path.join(FO, "data", "rule_room_cache.json")      # 2단계 캐시 (실명 단위)
MATCH_PROMPT_V = "match-2026-08-09a"   # 질의 형식(context 포함)이 바뀌면 올린다
POLICY = os.path.join(FO, "data", "placement_policy.json")
ROOMTYPE = os.path.join(FO, "data", "room_type_cache.json")   # 실/샤프트 (전역)
DOCS = "NFPC 103|NFTC 103|NFPC 608|NFTC 608"


def jload(p, d):
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else d


def jsave(p, obj):
    """임시 파일에 쓰고 교체한다. 캐시를 쓰는 도중 죽으면 잘린 JSON 이 남아
    다음 실행이 JSONDecodeError 로 죽는다 — 증분 저장의 취지가 무너진다."""
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def load_rules():
    with connect() as cn, cn.cursor() as cu:
        cu.execute(f"""
          SELECT r.id, d.title, COALESCE(r.item, r.article_no), r.deontic,
                 r.statement, COALESCE(r.raw_text, r.statement),
                 (SELECT q.value FROM rule_requirement q
                  WHERE q.rule_id=r.id AND q.measure='distance'
                    AND q.value ~ '^[0-9.]+$'
                    -- 값의 척도가 수평거리라고 1-B 가 판정한 것만. raw_text 로
                    -- 넓히면 창문 0.6m 배치 특칙("…헤드의 수평거리 이내에 창문이
                    -- 포함되도록")의 0.6 이 실 반경으로 오염된다 — 그 규칙의
                    -- measure_raw 는 None, 보 규칙은 '수직거리'다.
                    AND q.measure_raw LIKE '%수평거리%' LIMIT 1),
                 COALESCE(n.name, r.subject_raw, ''), r.context
          FROM legal_rule r JOIN documents d ON d.id=r.document_id
          LEFT JOIN nodes n ON n.id = r.subject_node_id
          WHERE d.title ~ '{DOCS}'""")
        return [{"id": i, "doc": doc, "wh": wh, "deontic": de, "stmt": st,
                 "raw": raw, "context": ctx_,
                 # 반경 사양은 규칙의 **대상이 헤드**일 때만 뜻이 있다.
                 # 송수구·방수구 5m 같은 distance 가 실 반경을 오염시켰다.
                 "dist": dist if "헤드" in subj else None,
                 "subj": subj}
                for i, doc, wh, de, st, raw, dist, subj, ctx_ in cu.fetchall()]


# ── 1단계: 장소 규칙 선별 (규칙 단위 영구 캐시) ──────────────────────────
TRIAGE_SYS = """소방 규칙 문장들을 봅니다. 각 규칙이 **특정 장소·실(방)의 종류에
따라** 적용이 갈리는 규칙인지 판정하십시오.
· 장소규칙 = 화장실·계단실·무대부·세대·보일러실처럼 "어떤 방이냐" 가 적용을
  가르는 것 (제외 장소, 장소별 다른 수치 포함)
· 아님 = 펌프·배관·제어반 등 설비 사양, 모든 곳에 똑같이 적용되는 기준
JSON: {"결과":[{"id":123,"장소규칙":true}]}"""


def triage(rules):
    cache = jload(TRIAGE, {})
    todo = [r for r in rules if str(r["id"]) not in cache]
    if todo:
        print(f"1단계 선별: 규칙 {len(todo)}건 질의 (캐시 {len(rules)-len(todo)})",
              flush=True)
        batches = [todo[i:i + 40] for i in range(0, len(todo), 40)]
        lock = threading.Lock()

        def ask(batch):
            body = "\n".join(f'{r["id"]}: [{r["doc"][-9:]} {r["wh"]}] {r["stmt"][:90]}'
                             for r in batch)
            return chat_json(TRIAGE_SYS, body)

        done = 0
        os.makedirs(os.path.dirname(TRIAGE), exist_ok=True)
        with ThreadPoolExecutor(max_workers=8) as pool:
            for fut in as_completed({pool.submit(ask, b): b for b in batches}):
                try:
                    got = fut.result()
                    hits = [(str(x.get("id")), bool(x.get("장소규칙")))
                            for x in got.get("결과", [])]
                except Exception as e:   # 배치 1건 실패로 전체를 버리지 않는다
                    print(f"  ✗ 선별 배치 실패: {e}", flush=True)
                    continue
                with lock:
                    cache.update(dict(hits))
                    done += 1
                    # 결과가 나오는 대로 저장 — 죽어도 한 만큼은 남는다
                    jsave(TRIAGE, cache)
                print(f"  선별 {done}/{len(batches)} 배치", flush=True)
    else:
        print(f"1단계 선별: 캐시 {len(rules)}건 — 질의 0")
    # permission(생략·면제) 규칙은 선별 결과와 무관하게 **전수 포함**한다.
    # 실을 비우는 결정을 하는 규칙이라, 배치 선별의 확률적 실수(실제로
    # 2.12.1.1 이 두 번 연속 떨어졌다)에 안전을 맡길 수 없다.
    return [r for r in rules
            if cache.get(str(r["id"])) or r["deontic"] == "permission"]


# ── 2단계: 규칙 × 실 매칭 ((rule_id, 실명) 단위 캐시) ─────────────────────
MATCH_SYS = """소방 규칙 **원문**과 CAD 도면의 실명 목록을 봅니다.
**모든 실명에 대해 빠짐없이** 이 규칙의 장소에 해당하는지 판정하십시오 —
고르는 게 아니라 실명마다 해당/비해당을 명시합니다. 하나라도 빼먹으면 안 됩니다.
· 원문의 괄호 정의·단서가 판단 근거입니다 (예: "파이프덕트 및
  덕트피트(파이프·덕트를 통과시키기 위한 구획된 구멍에 한한다)" 면 PS·EPS·TPS
  같은 배관·전기·통신 샤프트가 그 정의에 드는지를 원문 기준으로).
· 해당인 실만 자세히(occupied·confidence·이유), 비해당은 이름만 나열.
  두 목록을 합치면 실명 전체가 되어야 합니다.
· 표기만으로 모르겠으면 confidence 를 낮추십시오. 지어내지 마십시오.
JSON: {"해당":[{"실명":"...","occupied":false,
"confidence":"high|medium|low","이유":"한줄"}],
"비해당":["실명","실명"]}"""


def match(place_rules, names, ctx):
    cache = jload(MATCH, {})
    # 프롬프트 판이 다른 캐시는 쓰지 않는다 — 옛 판정과 새 판정이 섞이면
    # "왜 이 실만 판단 기준이 다르냐" 를 아무도 설명할 수 없게 된다.
    if cache.get("_prompt") != MATCH_PROMPT_V:
        if cache:
            jsave(MATCH + ".prev", cache)
            print(f"매칭 캐시 프롬프트 판 불일치 → 새로 시작 "
                  f"(이전분은 {MATCH}.prev)", flush=True)
        cache = {"_prompt": MATCH_PROMPT_V}
    todo = []
    for r in place_rules:
        missing = [n for n in names
                   if n not in cache.get(str(r["id"]), {}).get("실명들", {})]
        if missing:
            todo.append((r, missing))
    print(f"2단계 매칭: 질의 {len(todo)}건 / 장소규칙 {len(place_rules)}개 "
          f"(나머지 캐시)", flush=True)
    if not todo:
        return cache
    lock = threading.Lock()

    def ask(r, missing):
        # context = 상위 조항 원문(조 제목·지배문) — legal_rule.context 칼럼.
        # 번호 접두사에서 결정적으로 조립된 법령 원문이지 LLM 요약이 아니다.
        # 호 조각(raw)만 주면 "설치하지 않을 수 있다" 는 취지를 모델이 모른다.
        head = (r["context"] + "\n") if r.get("context") else ""
        return chat_json(MATCH_SYS,
                         f"규칙 [{r['doc']} {r['wh']}] ({r['deontic']}):\n"
                         f"{head}{r['raw'][:700]}"
                         f"\n\n맥락: {ctx}\n실명 목록:\n" + "\n".join(missing))

    done = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(ask, r, m): (r, m) for r, m in todo}
        for fut in as_completed(futs):
            r, missing = futs[fut]
            try:   # 파싱까지 감싼다 — 모델이 형식을 어겨도 이 규칙만 건너뛴다
                got = fut.result()
                hits = {h["실명"]: h for h in got.get("해당", [])
                        if isinstance(h, dict) and h.get("실명")}
                noes = {x for x in got.get("비해당", []) if isinstance(x, str)}
            except Exception as e:
                print(f"  ✗ #{r['id']}: {e}", flush=True)
                continue
            with lock:
                d = cache.setdefault(str(r["id"]), {"실명들": {}})
                for n in missing:
                    if n in hits:
                        v = hits[n]
                        d["실명들"][n] = {"해당": True,
                                        **{k: v[k] for k in
                                           ("occupied", "confidence", "이유")
                                           if k in v}}
                    elif n in noes:
                        d["실명들"][n] = {"해당": False}
                    # 두 목록 어디에도 없으면 캐시에 안 넣는다 — 다음 실행이 다시 묻는다
                done += 1
                if done % 10 == 0 or done == len(todo):
                    jsave(MATCH, cache)
                    print(f"  매칭 {done}/{len(todo)}", flush=True)
    jsave(MATCH, cache)
    return cache


# ── 2b단계: 실/샤프트 물리 판정 (실명당 1회, 전역 캐시) ───────────────────
# "상주(occupied)" 는 화장실에서 애매해 실행마다 뒤집혔다. 물리 이분법이 안정적:
#   실    = 사람이 들어가 쓰는 공간 (화장실·홀·창고 포함)
#   샤프트 = 배관·덕트·케이블·승강기만 지나는 폐쇄 구획 (PS·EPS·DA·PIT류)
ROOMTYPE_SYS = """CAD 실명을 보고 물리 유형을 판정합니다.
"실" = 사람이 들어가 쓰는 공간(화장실·홀·창고·기계실 포함).
"샤프트" = 배관·덕트·전기·승강기만 지나는 폐쇄 수직구획(사람 공간 아님).
모르겠으면 "모름". JSON: {"유형":[{"실명":"...","유형":"실|샤프트|모름",
"confidence":"high|medium|low"}]}"""


def room_types(names, ctx):
    cache = jload(ROOMTYPE, {})
    missing = [n for n in names if n not in cache]
    if missing:
        got = chat_json(ROOMTYPE_SYS,
                        f"맥락: {ctx}\n실명:\n" + "\n".join(missing))
        for v in got.get("유형", []):
            if v.get("실명"):
                cache[v["실명"]] = {"유형": v.get("유형", "모름"),
                                  "confidence": v.get("confidence", "low")}
        jsave(ROOMTYPE, cache)
        print(f"실/샤프트 판정: 질의 {len(missing)}개", flush=True)
    else:
        print("실/샤프트 판정: 전부 캐시", flush=True)
    return cache


# ── 3단계: 컴파일 — 효과는 규칙 구조에서 ─────────────────────────────────
def compile_bindings(place_rules, cache, names, policy, rtypes):
    by_rule = {str(r["id"]): r for r in place_rules}
    rooms: dict[str, dict] = {n: {"적용규칙": [], "확인필요": False} for n in names}
    for rid, d in cache.items():
        r = by_rule.get(rid)
        if not r:
            continue
        for n, h in d.get("실명들", {}).items():
            if n not in rooms or not h.get("해당"):
                continue
            rooms[n]["적용규칙"].append({
                "rule_id": r["id"], "근거": f"{r['doc']} {r['wh']}",
                "deontic": r["deontic"], "dist": r["dist"],
                "occupied": h.get("occupied"),
                "confidence": h.get("confidence", "low"),
                "이유": h.get("이유", "")})

    bindings = {}
    for n, info in rooms.items():
        # rule_id 로 정렬한다. 원래 순서는 스레드 완료 순서라 같은 입력을
        # 다시 돌리면 근거 목록과 대표 노드가 바뀐다(그게 그대로 RDF 에 새겨진다).
        applied = sorted(info["적용규칙"], key=lambda a: a["rule_id"])
        # 제외 — permission 규칙에 해당 + 정책(비출입 구획만 실제 제외)
        exempt = [a for a in applied if a["deontic"] == "permission"
                  and a["dist"] is None]
        # 헤드를 잘못 빼는 쪽이 잘못 더 놓는 쪽보다 위험하다. 제외 조건 셋:
        # 생략가능 규칙에 확신 있게 매칭 + 이 실이 물리적으로 샤프트.
        rt = rtypes.get(n, {})
        is_shaft = rt.get("유형") == "샤프트" and rt.get("confidence") == "high"
        shaft_maybe = rt.get("유형") == "샤프트" and rt.get("confidence") != "high"
        # 제외를 정당화하는 규칙은 여럿일 수 있다 — 하나만 골라 적으면
        # 나머지 근거가 사라지고, 고르는 순서에 판정 설명이 좌우된다.
        excl_all = ([a for a in exempt
                     if is_shaft and a["confidence"] == "high"]
                    if policy["생략가능_실제제외"] == "비출입구획만" else [])
        excl = excl_all[0] if excl_all else None
        excl_pending = ([a for a in exempt if shaft_maybe]
                        + ([a for a in exempt if is_shaft
                            and a["confidence"] != "high"] if not excl else []))
        # 반경 — distance 사양이 붙은 의무 규칙 중 이 실에 해당하는 것.
        # 여러 개면 규칙 구조상 더 구체(장소 조건이 붙은 쪽)가 override 라
        # 그 값을 쓴다. 같은 급이면 작은 값(보수적).
        dist_rules = sorted((float(a["dist"]), a) for a in applied
                            if a["deontic"] == "obligation" and a["dist"])
        dists = [d for d, _ in dist_rules]
        low = []
        if excl and excl["confidence"] != "high":
            low.append(excl)                       # 제외를 결정한 판정이 흔들림
        low += [a for a in applied
                if a["deontic"] == "obligation" and a["dist"]
                and a["confidence"] != "high"]      # 반경을 결정한 판정이 흔들림
        # 판정을 실제로 결정한 규칙. 이게 없으면 온톨로지의 근거 목록은
        # '이 실에 걸린 규칙들'일 뿐 '왜 이렇게 판정했는가'를 답하지 못한다.
        deciders = ([(a, "제외") for a in excl_all] if excl_all
                    else [(dist_rules[0][1], "반경")] if dist_rules else [])
        bindings[n] = {
            "노드": excl["근거"] if excl else (applied[0]["근거"] if applied else "—"),
            "기본동작": ("제외" if excl else
                        "세대 반경 2.6m" if dists and dists[0] == 2.6 else
                        f"반경 {dists[0]:g}m" if dists else "일반(공용 반경)"),
            "반경_mm": (None if excl else
                       int(dists[0] * 1000) if dists else None),
            "결정규칙": [{"출처": a["근거"], "rule_id": a["rule_id"], "역할": role}
                      for a, role in deciders],
            "정책적용": (policy.get("생략가능_실제제외") if exempt else None),
            "출처": "규칙매칭",
            "confidence": ("high" if applied and not low
                           else "low" if low else "high"),
            "확인필요": bool(low) or bool(excl_pending and not excl) or
                        bool(exempt and not excl and
                             any(a["occupied"] is None for a in exempt)),
            "이유": "; ".join(f"#{a['rule_id']} {a['이유']}" for a in applied[:2]),
            # 자르지 않는다 — 잘린 근거는 "그 조항은 검토 안 했다"로 읽힌다.
            "근거": [{"출처": a["근거"], "rule_id": a["rule_id"]}
                     for a in applied],
            # 같은 조문 라벨의 규칙이 여럿이라 rule_id 를 함께 남긴다
            # (온톨로지 레이어에서 규칙별 URI 로 구분하기 위함).
            "생략가능": [{"출처": a["근거"], "rule_id": a["rule_id"]}
                     for a in exempt if not excl],
        }
    return bindings


def run(base):
    rooms_p = os.path.join(FO, "output", f"{base}_rooms_rect.json")
    names = sorted({r["room"] for r in jload(rooms_p, {"rooms": []})["rooms"]})
    if not names:
        sys.exit(f"실 목록 없음: {rooms_p}")
    profile = jload(os.path.join(FO, "data", "building_profile.json"), {})
    floor = profile.get("층", {})
    ctx = (f"{profile.get('용도','건물')} {floor.get('이름','')} "
           + ("(세대 있는 층)" if floor.get("세대있음") else "(부대시설 층, 세대 없음)"))
    policy = jload(POLICY, None)
    if policy is None:
        # 법이 안 정해 주는 것 — "생략 가능" 을 실제로 생략할지는 설계 정책이다.
        # 숨기지 않고 파일로 꺼내 둔다. 화면에 '정책' 으로 표시된다.
        policy = {"생략가능_실제제외": "비출입구획만",
                  "설명": "2.12 생략 가능 장소 중 사람이 출입하지 않는 구획"
                          "(샤프트류)만 실제 제외. 화장실·계단 등은 강화 적용(설치)."}
        jsave(POLICY, policy)
    # 지원하지 않는 정책 값이면 조용히 '제외 없음' 으로 흐르지 않게 여기서 멈춘다.
    SUPPORTED = {"비출입구획만"}
    if policy.get("생략가능_실제제외") not in SUPPORTED:
        sys.exit(f"지원하지 않는 정책 값: 생략가능_실제제외="
                 f"{policy.get('생략가능_실제제외')!r} (가능: {sorted(SUPPORTED)})\n"
                 f"  {POLICY} 를 고치십시오.")

    rules = load_rules()
    place_rules = triage(rules)
    print(f"장소 규칙 {len(place_rules)} / 전체 {len(rules)}")
    cache = match(place_rules, names, ctx)
    rtypes = room_types(names, ctx)
    bindings = compile_bindings(place_rules, cache, names, policy, rtypes)

    outp = os.path.join(FO, "output", f"{base}_room_bindings.json")
    jsave(outp, {"층맥락": ctx, "정책": policy, "바인딩": bindings})
    from collections import Counter
    print(f"\n실 {len(names)}개:")
    for n in names:
        b = bindings[n]
        mark = " ⚠확인필요" if b["확인필요"] else ""
        extra = f" (생략가능: {len(b['생략가능'])}건 — 정책상 설치)" if b["생략가능"] else ""
        print(f"  {n:12} → {b['기본동작']:14}{extra}{mark}")
    print(f"\n동작 분포: {dict(Counter(b['기본동작'] for b in bindings.values()))}")
    print("→", outp)
    print(usage_line())


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "기준층")
