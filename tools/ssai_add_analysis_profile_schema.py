"""Install Dashboard profile storage in the shared SSAI management DB.

Dry-run is the default. Run with --apply on HO1 only after reviewing its
reported columns and targets. This tool is never invoked by the application.
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

PERMISSION_CODE = "ANALYSIS_PROFILE_MANAGE"
PROFILE_TABLE = "SSAI_ANALYSIS_PROFILES"


def _columns(conn: Any, table_name: str) -> set[str]:
    rows = conn.cursor().execute(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = N'dbo' AND TABLE_NAME = ?
        """,
        table_name,
    ).fetchall()
    return {str(row[0]) for row in rows}


def _table_exists(conn: Any, table_name: str) -> bool:
    row = conn.cursor().execute("SELECT OBJECT_ID(?, N'U')", f"dbo.{table_name}").fetchone()
    return bool(row and row[0])


def _profile_unique_indexes(conn: Any) -> list[dict[str, Any]]:
    rows = conn.cursor().execute(
        """
        SELECT i.name, i.is_unique_constraint, c.name, ic.key_ordinal
        FROM sys.indexes i
        INNER JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
        INNER JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
        WHERE i.object_id = OBJECT_ID(N'dbo.SSAI_ANALYSIS_PROFILES')
          AND i.is_unique = 1
          AND i.is_primary_key = 0
        ORDER BY i.name, ic.key_ordinal
        """
    ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row[0])
        item = grouped.setdefault(name, {"name": name, "is_constraint": bool(row[1]), "columns": []})
        item["columns"].append(str(row[2] or ""))
    return [
        {"name": item["name"], "is_constraint": item["is_constraint"], "columns": ",".join(item["columns"])}
        for item in grouped.values()
    ]


def _profile_duplicate_companies(conn: Any) -> int:
    row = conn.cursor().execute(
        """
        SELECT COUNT(*)
        FROM (
            SELECT company_id
            FROM dbo.SSAI_ANALYSIS_PROFILES
            GROUP BY company_id
            HAVING COUNT(*) > 1
        ) d
        """
    ).fetchone()
    return int(row[0] or 0) if row else 0


def _permission_exists(conn: Any) -> bool:
    row = conn.cursor().execute(
        "SELECT TOP 1 permission_id FROM dbo.SSAI_PERMISSIONS WHERE permission_code = ?",
        PERMISSION_CODE,
    ).fetchone()
    return bool(row)


def _permission_insert_sql(permission_columns: set[str]) -> tuple[str, list[Any]]:
    values: dict[str, Any] = {
        "permission_code": PERMISSION_CODE,
        "permission_name": "Dashboard profile save",
        "permission_group": "SYSTEM",
        "category": "SYSTEM",
        "description": "Save Dashboard conditions by user and company",
        "is_active": 1,
    }
    insert_columns: list[str] = []
    placeholders: list[str] = []
    params: list[Any] = []
    for column, value in values.items():
        if column in permission_columns:
            insert_columns.append(column)
            placeholders.append("?")
            params.append(value)
    for column in ("created_at", "updated_at"):
        if column in permission_columns:
            insert_columns.append(column)
            placeholders.append("SYSDATETIME()")
    if "permission_code" not in insert_columns:
        raise RuntimeError("SSAI_PERMISSIONS.permission_code is required")
    return (
        f"INSERT INTO dbo.SSAI_PERMISSIONS ({', '.join(insert_columns)}) VALUES ({', '.join(placeholders)})",
        params,
    )


def _grant_permission(conn: Any, role_permission_columns: set[str]) -> int:
    columns = ["role_id", "permission_id"]
    values = ["r.role_id", "p.permission_id"]
    if "is_allowed" in role_permission_columns:
        columns.append("is_allowed")
        values.append("1")
    if "is_active" in role_permission_columns:
        columns.append("is_active")
        values.append("1")
    for column in ("created_at", "updated_at"):
        if column in role_permission_columns:
            columns.append(column)
            values.append("SYSDATETIME()")
    sql = f"""
    INSERT INTO dbo.SSAI_ROLE_PERMISSIONS ({', '.join(columns)})
    SELECT {', '.join(values)}
    FROM dbo.SSAI_ROLES r CROSS JOIN dbo.SSAI_PERMISSIONS p
    WHERE p.permission_code = ?
      AND (r.role_code = N'SSART_MANAGER' OR EXISTS (
          SELECT 1 FROM dbo.SSAI_ROLE_PERMISSIONS rp
          INNER JOIN dbo.SSAI_PERMISSIONS existing_p ON existing_p.permission_id = rp.permission_id
          WHERE rp.role_id = r.role_id AND existing_p.permission_code = N'USER_MANAGE_COMPANY'
          {"AND rp.is_allowed = 1" if "is_allowed" in role_permission_columns else ""}
      ))
      AND NOT EXISTS (
          SELECT 1 FROM dbo.SSAI_ROLE_PERMISSIONS rp
          WHERE rp.role_id = r.role_id AND rp.permission_id = p.permission_id
      )
    """
    return int(conn.cursor().execute(sql, PERMISSION_CODE).rowcount or 0)


def run(*, apply: bool) -> dict[str, object]:
    with connect_ssai_db() as conn:
        permission_columns = _columns(conn, "SSAI_PERMISSIONS")
        role_permission_columns = _columns(conn, "SSAI_ROLE_PERMISSIONS")
        profile_exists = _table_exists(conn, PROFILE_TABLE)
        permission_exists = _permission_exists(conn)
        result: dict[str, object] = {
            "applied": bool(apply),
            "profile_table_exists": profile_exists,
            "permission_exists": permission_exists,
            "permission_columns": sorted(permission_columns),
            "role_permission_columns": sorted(role_permission_columns),
            "target_roles": "SSART_MANAGER plus roles with USER_MANAGE_COMPANY",
        }
        if profile_exists:
            result["profile_unique_indexes"] = _profile_unique_indexes(conn)
            result["duplicate_company_count"] = _profile_duplicate_companies(conn)
            result["target_unique_key"] = "company_id"
        if not apply:
            return result
        cur = conn.cursor()
        if not profile_exists:
            cur.execute(
                """
                CREATE TABLE dbo.SSAI_ANALYSIS_PROFILES (
                    profile_id BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    company_id BIGINT NOT NULL,
                    profile_json NVARCHAR(MAX) NOT NULL,
                    created_at DATETIME2 NOT NULL,
                    updated_at DATETIME2 NOT NULL,
                    CONSTRAINT UQ_SSAI_ANALYSIS_PROFILES_COMPANY UNIQUE (company_id)
                )
                """
            )
            result["profile_table_created"] = True
        else:
            duplicates = int(result.get("duplicate_company_count") or 0)
            if duplicates:
                raise RuntimeError("duplicate_company_profiles_require_manual_resolution")
            existing_indexes = _profile_unique_indexes(conn)
            for index in existing_indexes:
                if index.get("columns") != "company_id":
                    if index.get("is_constraint"):
                        cur.execute(f"ALTER TABLE dbo.SSAI_ANALYSIS_PROFILES DROP CONSTRAINT [{index['name']}]")
                    else:
                        cur.execute(f"DROP INDEX [{index['name']}] ON dbo.SSAI_ANALYSIS_PROFILES")
            if not any(index.get("columns") == "company_id" for index in existing_indexes):
                cur.execute("ALTER TABLE dbo.SSAI_ANALYSIS_PROFILES ADD CONSTRAINT UQ_SSAI_ANALYSIS_PROFILES_COMPANY UNIQUE (company_id)")
            result["profile_unique_key_migrated"] = True
        if not permission_exists:
            sql, params = _permission_insert_sql(permission_columns)
            cur.execute(sql, *params)
            result["permission_inserted"] = True
        result["role_grants_inserted"] = _grant_permission(conn, role_permission_columns)
        conn.commit()
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = run(apply=bool(args.apply))
    except Exception as exc:
        # Never print a connection string, endpoint, or driver exception detail.
        result = {"applied": bool(args.apply), "ok": False, "error_type": type(exc).__name__}
    else:
        result["ok"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))
