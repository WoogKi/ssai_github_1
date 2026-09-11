"""Install Korean public-holiday authority tables in the shared SSAI DB.

Dry-run is the default.  Run with --apply only after reviewing the target
shared management database.  The application runtime never creates tables.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.ssai_auth_service import connect_ssai_db  # noqa: E402
from app.services.ssai_business_calendar_repository import HOLIDAY_LOAD_TABLE, HOLIDAY_TABLE  # noqa: E402


def _table_exists(conn: Any, table_name: str) -> bool:
    row = conn.cursor().execute("SELECT OBJECT_ID(?, N'U')", f"dbo.{table_name}").fetchone()
    return bool(row and row[0])


def _columns(conn: Any, table_name: str) -> list[str]:
    rows = conn.cursor().execute(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = N'dbo' AND TABLE_NAME = ?
        ORDER BY ORDINAL_POSITION
        """,
        table_name,
    ).fetchall()
    return [str(row[0]) for row in rows]


def run(*, apply: bool) -> dict[str, object]:
    with connect_ssai_db() as conn:
        holiday_exists = _table_exists(conn, HOLIDAY_TABLE)
        load_exists = _table_exists(conn, HOLIDAY_LOAD_TABLE)
        result: dict[str, object] = {
            "applied": bool(apply),
            "target": "shared_ssai_management_db",
            "holiday_table_exists": holiday_exists,
            "load_table_exists": load_exists,
        }
        if holiday_exists:
            result["holiday_columns"] = _columns(conn, HOLIDAY_TABLE)
        if load_exists:
            result["load_columns"] = _columns(conn, HOLIDAY_LOAD_TABLE)
        if not apply:
            return result
        cur = conn.cursor()
        try:
            if not holiday_exists:
                cur.execute(
                    f"""
                    CREATE TABLE dbo.{HOLIDAY_TABLE} (
                        holiday_date CHAR(8) NOT NULL,
                        source_sequence NVARCHAR(50) NOT NULL,
                        holiday_name NVARCHAR(200) NOT NULL,
                        holiday_type NVARCHAR(100) NOT NULL,
                        is_holiday BIT NOT NULL,
                        source NVARCHAR(100) NOT NULL,
                        source_version NVARCHAR(200) NOT NULL,
                        updated_at DATETIME2 NOT NULL,
                        CONSTRAINT PK_{HOLIDAY_TABLE} PRIMARY KEY (source, holiday_date, source_sequence)
                    )
                    """
                )
                result["holiday_table_created"] = True
            if not load_exists:
                cur.execute(
                    f"""
                    CREATE TABLE dbo.{HOLIDAY_LOAD_TABLE} (
                        source NVARCHAR(100) NOT NULL,
                        calendar_year INT NOT NULL,
                        source_version NVARCHAR(200) NOT NULL,
                        holiday_count INT NOT NULL,
                        payload_checksum CHAR(64) NOT NULL,
                        load_status NVARCHAR(20) NOT NULL,
                        loaded_at DATETIME2 NOT NULL,
                        updated_at DATETIME2 NOT NULL,
                        CONSTRAINT PK_{HOLIDAY_LOAD_TABLE} PRIMARY KEY (source, calendar_year)
                    )
                    """
                )
                result["load_table_created"] = True
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="install shared-DB schema")
    args = parser.parse_args()
    try:
        payload = run(apply=bool(args.apply))
        payload["ok"] = True
    except Exception as exc:
        payload = {"applied": bool(args.apply), "ok": False, "error_type": type(exc).__name__}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
