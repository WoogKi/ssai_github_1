# tools/ssai_test_company_db_service.py
#
# 선택 회사 DB 접속 공통 함수 테스트용
#
# Create 2026/06/22



from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.ssai_auth_service import (  # noqa: E402
    connect_company_db,
    get_company_db_config,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company-id", type=int, required=True)
    args = parser.parse_args()

    cfg = get_company_db_config(args.company_id)

    print("[INFO] company db config")
    print(f"  company_id   = {cfg.company_id}")
    print(f"  company_code = {cfg.company_code}")
    print(f"  company_name = {cfg.company_name}")
    print(f"  db_server    = {cfg.db_server}")
    print(f"  db_name      = {cfg.db_name}")
    print(f"  db_user      = {cfg.db_user}")
    print(f"  db_driver    = {cfg.db_driver}")

    with connect_company_db(args.company_id) as conn:
        cur = conn.cursor()

        db_name = cur.execute("SELECT DB_NAME()").fetchone()[0]
        user_name = cur.execute("SELECT SYSTEM_USER").fetchone()[0]

        table_exists = cur.execute(
            """
            SELECT COUNT(*)
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_NAME = 'Rddbc060'
            """
        ).fetchone()[0]

        if int(table_exists) == 0:
            raise RuntimeError("Rddbc060 테이블을 찾지 못했습니다.")

        row_count = cur.execute("SELECT COUNT(*) FROM dbo.Rddbc060").fetchone()[0]

    print("[OK] 회사 DB 접속 성공")
    print(f"[OK] current_db = {db_name}")
    print(f"[OK] system_user = {user_name}")
    print(f"[OK] Rddbc060 rows = {row_count}")


if __name__ == "__main__":
    main()