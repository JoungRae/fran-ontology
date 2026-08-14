# -*- coding: utf-8 -*-
"""헤드 배치 리포트 로컬 서버 — 리포트 HTML 의 '재실행' 버튼 지원.

output/ 를 정적으로 서빙하고, POST /rerun 으로 fire_layout.py 를 재실행한다.
반드시 배치 엔진과 같은 venv 파이썬으로 실행할 것(numpy·matplotlib 필요).

사용법: python fire_server.py [도면베이스] [--port 8765]
  예:   python fire_server.py 510_지하1층_pit
        → http://localhost:8765/510_지하1층_pit_head_layout.html 자동 오픈
"""
import http.server
import json
import os
import re
import subprocess
import sys
import webbrowser

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FO = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(FO, "output")
PY = sys.executable


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=OUT, **kw)

    def do_POST(self):
        if self.path not in ("/rerun", "/decide"):
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
            base = str(req.get("base", "")).strip()
            if not re.fullmatch(r"[\w가-힣()\- .]+", base) or ".." in base:
                raise ValueError("잘못된 도면 이름")
            ru = float(req.get("r_unit", 2.6))
            rc = float(req.get("r_common", 2.3))
            if not (1.0 <= ru <= 6.0 and 1.0 <= rc <= 6.0):
                raise ValueError("반경은 1.0~6.0m 범위여야 합니다")
            if self.path == "/decide":
                # ⚠확인필요 실의 제외/설치 사람 확정 저장. 실명은 파일 경로에
                # 쓰이지 않고 JSON 열쇠로만 쓰인다. '' = 판정대로(결정 철회).
                dec_in = req.get("decisions") or {}
                if not isinstance(dec_in, dict):
                    raise ValueError("decisions 는 {실명: 제외|설치|''} 여야 합니다")
                dp = os.path.join(OUT, f"{base}_room_decisions.json")
                cur = {}
                if os.path.exists(dp):
                    cur = json.load(open(dp, encoding="utf-8"))
                for name, v in dec_in.items():
                    name = str(name)[:80]
                    if v == "":
                        cur.pop(name, None)
                    elif v in ("제외", "설치"):
                        cur[name] = v
                    else:
                        raise ValueError(f"알 수 없는 결정 값: {v!r}")
                tmp = dp + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(cur, f, ensure_ascii=False, indent=1)
                os.replace(tmp, dp)
                print(f"[확정] {base} 결정 {len(cur)}건 저장 → 재배치")
            cmd = [PY, os.path.join(FO, "fire_layout.py"), base, "--heads",
                   "--r-unit", f"{ru}", "--r-common", f"{rc}"]
            print(f"[재실행] {base} r_unit={ru} r_common={rc}")
            p = subprocess.run(cmd, cwd=FO, capture_output=True, text=True,
                               timeout=900, encoding="utf-8", errors="replace")
            print(p.stdout)
            if p.returncode != 0:
                print(p.stderr)
            body = {"ok": p.returncode == 0,
                    "log": (p.stdout or "")[-2000:],
                    "error": None if p.returncode == 0 else (p.stderr or "")[-500:]}
        except Exception as ex:
            body = {"ok": False, "error": str(ex)}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass  # 정적 요청 로그는 조용히


def main():
    argv = list(sys.argv[1:])
    port = 8765
    if "--port" in argv:
        i = argv.index("--port")
        port = int(argv[i + 1])
        del argv[i:i + 2]
    base = next((a for a in argv if not a.startswith("--")), None)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}/" + (f"{base}_head_layout.html" if base else "")
    print(f"리포트 서버 실행: {url}")
    print("(이 창을 닫으면 '재실행' 버튼이 동작하지 않습니다 — Ctrl+C 로 종료)")
    if base:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
