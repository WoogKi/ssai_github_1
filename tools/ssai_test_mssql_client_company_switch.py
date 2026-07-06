# tools/ssai_test_mssql_client_company_switch.py
#
#  회사 선택 테스트용
#
# Create 2026/06/22

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.mssql_client import read_df, set_current_company_id  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company-id", type=int, required=True)
    args = parser.parse_args()

    set_current_company_id(args.company_id)

    df_db = read_df("SELECT DB_NAME() AS current_db, SYSTEM_USER AS [system_user]")
    df_count = read_df("SELECT COUNT(*) AS row_count FROM dbo.Rddbc060")

    print(f"[OK] company_id = {args.company_id}")
    print(f"[OK] current_db = {df_db.iloc[0]['current_db']}")
    print(f"[OK] system_user = {df_db.iloc[0]['system_user']}")
    print(f"[OK] Rddbc060 rows = {df_count.iloc[0]['row_count']}")


if __name__ == "__main__":
    main()