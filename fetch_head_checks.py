# -*- coding: utf-8 -*-
"""cons_law DB 에서 '스프링클러 헤드' 관련 체크 전체를 추출 → output/head_law_checks.json.

fire_layout 리포트의 '법적 검토' 패널이 이 파일을 읽어 결과와 비교 판정한다.
(fire_layout 은 소스 venv[psycopg 없음]로 돌므로 DB 접근을 이 스크립트로 분리)

실행: cons_law venv 로 → D:/Python_test/cons_law/.venv/Scripts/python.exe fetch_head_checks.py
"""
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"D:\Python_test\cons_law\src")
from dotenv import load_dotenv

load_dotenv(r"D:\Python_test\cons_law\.env")
import psycopg

FO = os.path.dirname(os.path.abspath(__file__))

SQL = """
SELECT c.id, c.title, c.domain, LEFT(c.source_excerpt, 400) AS excerpt,
       COALESCE(d.title, '')          AS law_title,
       COALESCE(cl.article_no, '')    AS article_no,
       COALESCE(cl.title, '')         AS article_title,
       COALESCE(cl.paragraph_no, '')  AS paragraph_no,
       COALESCE(cl.item_no, '')       AS item_no,
       COALESCE(att.label, att.title, '') AS attachment
FROM checks c
LEFT JOIN clauses cl            ON cl.id = c.source_clause_id
LEFT JOIN attachments att       ON att.id = c.source_attachment_id
LEFT JOIN document_versions dv  ON dv.id = c.source_version_id
LEFT JOIN documents d           ON d.id = dv.document_id
WHERE c.deprecated_at IS NULL
  AND (c.title LIKE %s OR c.title LIKE %s
       OR c.source_excerpt LIKE %s OR c.source_excerpt LIKE %s)
ORDER BY c.id
"""


def source_label(law, art_no, art_title, para, item, att):
    """법령명 제n조(제목) 제n항 제n호 / 법령명 별표 …"""
    parts = [law] if law else []
    if att:
        parts.append(att if "별표" in att else f"별표 {att}")
    elif art_no:
        s = art_no + (f"({art_title})" if art_title else "")
        if para and para != "None":
            s += f" {para}"
        if item and item != "None":
            s += f" {item}"
        parts.append(s)
    return " ".join(parts)


def main():
    con = psycopg.connect(os.environ.get("PG_DSN") or os.environ.get("DATABASE_URL"))
    cur = con.cursor()
    cur.execute(SQL, ("%스프링클러%", "%헤드%", "%스프링클러헤드%", "%스프링클러 헤드%"))
    rows = cur.fetchall()
    con.close()

    # 드렌처·간이·화재조기진압용 등도 '헤드'로 걸리지만 참고용으로 함께 보존
    out = [{"id": r[0], "title": r[1], "domain": r[2],
            "excerpt": (r[3] or "").strip(),
            "source": source_label(r[4], r[5], r[6], r[7], r[8], r[9])}
           for r in rows]
    op = os.path.join(FO, "output", "head_law_checks.json")
    json.dump({"n": len(out), "checks": out},
              open(op, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"스프링클러/헤드 관련 체크 {len(out)}건 → {op}")


if __name__ == "__main__":
    main()
