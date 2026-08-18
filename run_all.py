# -*- coding: utf-8 -*-
"""도면 하나 → 전 리포트. 원클릭 파이프라인.

  python run_all.py 지하1층_pit                # data/지하1층_pit.json 부터 끝까지
  python run_all.py C:/어딘가/새도면.json       # data/ 로 복사 후 진행
  python run_all.py 지하1층_pit --dry-run       # 뭘 돌릴지만 보여줌
  python run_all.py 지하1층_pit --force heads   # 그 단계부터 강제 재실행
  python run_all.py 지하1층_pit --until bot     # 거기까지만
  python run_all.py --list                     # 단계 목록

단계 (산출물이 입력보다 새로우면 건너뜀 — 증분):

  classify  레이어 분류(GPT)        → <b>_layer_classification.json   💰 LLM
  rect      방 인식(사각+스냅)      → <b>_rooms_rect.json
  flood     밀폐 폴리곤 + rect 병합 → <b>_rooms_flood.json
  bot       BOT 온톨로지            → <b>.ttl (+room_ids)
  match     규칙×실 매칭(LLM+캐시)  → <b>_room_bindings.json          💰 LLM
  params    법령값 수확(DB)         → head_params.json
  legal     법령 레이어 TTL         → <b>_legal.ttl
  heads     스프링클러 헤드 배치    → <b>_head_layout.html
  evac      피난 보행거리           → <b>_evac_layout.html (+summary)
  law       평면도 법률 검토 4항목  → <b>_law_review.html

왜 이 파일이 필요한가: 단계마다 파이썬이 다르다(기하 스택은 소스 프로젝트
venv, DB 단계는 cons_law venv) 그리고 .env 도 다르다. 그 배선 지식이 사람
머릿속에만 있으면 파이프라인은 "되지만 아무나 못 돌리는" 상태다 — 여기에
전부 적어서 명령 하나로 만든다.

보(살수장애) 오버레이는 선택 입력이다: output/<b>_beams.json 이 있으면
heads 가 자동으로 쓴다(구조도 정합은 align_beams.py 로 별도 1회).

이 스크립트 자체는 표준 라이브러리만 쓴다 — 아무 파이썬으로 실행해도 된다.
"""
import argparse
import os
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FO = os.path.dirname(os.path.abspath(__file__))
SRCP = r"D:\Python_test\fran_consist_cad_json"      # 기하 스택(numpy·contourpy·rdflib)
LAWP = r"D:\Python_test\cons_law"                   # 법령 DB 스택(psycopg)
SRC_PY = os.environ.get("FRAN_SRC_PY",
                        os.path.join(SRCP, ".venv", "Scripts", "python.exe"))
LAW_PY = os.environ.get("FRAN_LAW_PY",
                        os.path.join(LAWP, ".venv", "Scripts", "python.exe"))


def read_env(path):
    """KEY=VALUE 꼴 .env 를 읽는다(따옴표 벗김). dotenv 의존을 피한다 —
    이 스크립트는 어느 파이썬으로도 돌아야 하므로."""
    env = {}
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def stages(base):
    b = base
    D = lambda *p: os.path.join(FO, *p)
    data = D("data", f"{b}.json")
    cls = D("output", f"{b}_layer_classification.json")
    rect = D("output", f"{b}_rooms_rect.json")
    flood = D("output", f"{b}_rooms_flood.json")
    ttl = D("output", f"{b}.ttl")
    hp = D("output", "head_params.json")
    bind = D("output", f"{b}_room_bindings.json")
    legal = D("output", f"{b}_legal.ttl")
    heads = D("output", f"{b}_head_layout.html")
    evac = D("output", f"{b}_evac_layout.html")
    esum = D("output", f"{b}_evac_summary.json")
    law = D("output", f"{b}_law_review.html")
    profile = D("data", "building_profile.json")

    env_src = {**os.environ, **read_env(os.path.join(SRCP, ".env"))}
    env_law = {**os.environ, **read_env(os.path.join(LAWP, ".env"))}

    def law_cmd(script, *args):
        # cons_law 스크립트류는 .env 를 스스로 안 읽는다(세션에서 배운 것) —
        # 환경으로 주입하고 runpy 로 부른다.
        code = ("import runpy,sys;"
                f"sys.argv={[os.path.basename(script)] + list(args)!r};"
                f"runpy.run_path({script!r},run_name='__main__')")
        return [LAW_PY, "-c", code]

    return [
        # (이름, 설명, cmd, env, 입력들, 산출물들, LLM 여부)
        ("classify", "레이어 분류 (GPT)",
         [SRC_PY, D("layer_classify.py"), data], env_src,
         [data], [cls], True),
        ("rect", "방 인식 (사각+벽면 스냅)",
         [SRC_PY, D("plan_rooms_rect.py"), data], env_src,
         [data, cls], [rect], False),
        ("flood", "밀폐 폴리곤 인식 + rect 병합",
         [SRC_PY, D("plan_rooms_flood.py"), data, "--merge-into-rect"], env_src,
         [data, cls], [flood], False),
        ("bot", "BOT 온톨로지 (기하 그래프)",
         [SRC_PY, D("build_bot.py"), b], env_src,
         [rect, flood], [ttl], False),
        ("match", "규칙×실 매칭 (LLM, (규칙,실명) 캐시)",
         law_cmd(D("match_rules_rooms.py"), b), env_law,
         [rect, profile], [bind], True),
        ("params", "법령값 수확 (규칙 DB → head_params)",
         law_cmd(D("derive_head_params.py"), b), env_law,
         [profile, bind], [hp], False),
        ("legal", "법령 레이어 TTL (판정 → RDF)",
         [SRC_PY, D("annotate_legal.py"), b], env_src,
         [ttl, bind], [legal], False),
        ("heads", "스프링클러 헤드 배치",
         [SRC_PY, D("fire_layout.py"), b, "--heads"], env_src,
         [legal, hp, rect], [heads], False),
        ("evac", "피난 보행거리 검토",
         [SRC_PY, D("evac_report.py"), b], env_src,
         [rect, cls, hp], [evac, esum], False),
        ("law", "평면도 법률 검토 (4항목 종합)",
         [SRC_PY, D("plan_law_report.py"), b], env_src,
         [esum, hp, rect], [law], False),
    ]


def need(ins, outs):
    """산출물이 없거나 입력보다 오래됐으면 True."""
    ots = [os.path.getmtime(o) for o in outs if os.path.exists(o)]
    if len(ots) < len(outs):
        return True
    its = [os.path.getmtime(i) for i in ins if os.path.exists(i)]
    return bool(its) and min(ots) < max(its)


def main():
    ap = argparse.ArgumentParser(description="도면 → 전 리포트 원클릭")
    ap.add_argument("base", nargs="?", help="도면 이름 또는 json 경로")
    ap.add_argument("--force", default="",
                    help="쉼표로 단계 지정(그 단계와 하류를 강제), all=전부")
    ap.add_argument("--until", default="", help="이 단계까지만")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true", help="단계 목록만")
    a = ap.parse_args()

    if a.list or not a.base:
        for name, desc, *_ in stages("<도면>"):
            print(f"  {name:9} {desc}")
        return

    base = a.base
    if base.lower().endswith(".json"):     # 경로로 받으면 data/ 로 들인다
        src = os.path.abspath(base)
        base = os.path.splitext(os.path.basename(src))[0]
        dst = os.path.join(FO, "data", f"{base}.json")
        if os.path.abspath(src) != os.path.abspath(dst):
            import shutil
            shutil.copy2(src, dst)
            print(f"입력 복사: {src} → data/{base}.json")

    if not os.path.exists(os.path.join(FO, "data", f"{base}.json")):
        sys.exit(f"도면 없음: data/{base}.json")

    # 판정 전제(건물 사실)가 이 도면과 맞는지 — 층 이름으로만 거칠게 경고
    try:
        import json as _j
        prof = _j.load(open(os.path.join(FO, "data", "building_profile.json"),
                            encoding="utf-8"))
        fl = (prof.get("층") or {}).get("이름", "")
        if fl and fl not in base:
            print(f"⚠ building_profile 의 층 '{fl}' 이 도면 이름과 다릅니다 — "
                  f"프로필이 다른 층 것이면 판정 전제가 어긋납니다.")
    except Exception:
        pass

    plan = stages(base)
    forced = {s.strip() for s in a.force.split(",") if s.strip()}
    force_all = "all" in forced
    names = [s[0] for s in plan]
    force_from = min((names.index(f) for f in forced if f in names),
                     default=len(names)) if not force_all else 0

    t0 = time.time()
    ran = skipped = 0
    for idx, (name, desc, cmd, env, ins, outs, llm) in enumerate(plan):
        # forced 는 "그 단계부터 하류 전부" 강제 — mtime 이 못 잡는 코드
        # 수정 뒤에 쓴다. 평소에는 산출물/입력 mtime 비교(need)만.
        run_it = (force_all or (bool(forced) and idx >= force_from)
                  or need(ins, outs))
        if not run_it:
            print(f"[{name:9}] 건너뜀 (산출물 최신)")
            skipped += 1
        else:
            tag = " 💰 LLM 비용 발생" if llm else ""
            print(f"[{name:9}] 실행 — {desc}{tag}")
            if a.dry_run:
                print("           $", " ".join(
                    c if len(c) < 100 else c[:97] + "…" for c in cmd))
            else:
                t = time.time()
                r = subprocess.run(cmd, cwd=FO, env=env)
                if r.returncode != 0:
                    sys.exit(f"✗ {name} 실패 (종료 {r.returncode}) — 여기서 멈춥니다. "
                             f"위 로그를 보고 고친 뒤 다시 실행하면 이 단계부터 이어집니다.")
                print(f"           ✓ {time.time()-t:.0f}초")
                ran += 1
        if a.until and name == a.until:
            break

    print(f"\n완료 — 실행 {ran} · 건너뜀 {skipped} · {time.time()-t0:.0f}초")
    if not a.dry_run and not a.until:
        print("리포트:")
        for f in (f"{base}_head_layout.html", f"{base}_evac_layout.html",
                  f"{base}_law_review.html"):
            p = os.path.join(FO, "output", f)
            if os.path.exists(p):
                print(f"  output/{f}")
        print(f"보기: python fire_server.py {base}  →  http://localhost:8765/")


if __name__ == "__main__":
    main()
