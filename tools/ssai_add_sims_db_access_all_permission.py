# tools/ssai_add_sims_db_access_all_permission.py
#
# SS AI Phase 3
# SIMS_DB_ACCESS_ALL 권한 추가/부여 도구
#
# 의미:
# - 이 권한을 가진 사용자는 모든 활성 ERP DB를 선택할 수 있다.
#
# 기본 부여 대상:
# - SYSTEM_ADMIN
# - SSART_MANAGER
# - SSART_SUPPORT_ALL 이 이미 있으면 같이 부여
#
# 주의:
# - 기존 WHOLESALE_MANAGER / WHOLESALE_STAFF 에는 부여하지 않는다.
# - SSAI_ROLE_PERMISSIONS는 is_allowed 컬럼을 사용한다.

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.ssai_auth_service import connect_ssai_db  # noqa: E402


PERMISSION_CODE = "SIMS_DB_ACCESS_ALL"
PERMISSION_NAME = "모든 ERP DB 접근"
PERMISSION_DESC = "모든 활성 ERP/SIMS DB를 선택할 수 있는 권한"

GRANT_ROLE_CODES = [
    "SYSTEM_ADMIN",
    "SSART_MANAGER",
    "SSART_SUPPORT_ALL",
]


def _fetch_one_dict(conn, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    cur = conn.cursor()
    row = cur.execute(sql, *params).fetchone()

    if not row:
        return None

    cols = [c[0] for c in cur.description]
    return dict(zip(cols, row))


def _fetch_all_dicts(conn, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cur = conn.cursor()
    rows = cur.execute(sql, *params).fetchall()
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def _columns(conn, table_name: str) -> set[str]:
    rows = _fetch_all_dicts(
        conn,
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = N'dbo'
          AND TABLE_NAME = ?
        """,
        (table_name,),
    )
    return {str(r["COLUMN_NAME"]) for r in rows}


def _insert_permission_if_missing(conn) -> dict[str, Any]:
    existing = _fetch_one_dict(
        conn,
        """
        SELECT TOP 1 *
        FROM dbo.SSAI_PERMISSIONS
        WHERE permission_code = ?
        """,
        (PERMISSION_CODE,),
    )

    if existing:
        return {
            "action": "permission_exists",
            "permission": existing,
        }

    cols = _columns(conn, "SSAI_PERMISSIONS")

    insert_cols: list[str] = []
    value_sql: list[str] = []
    params: list[Any] = []

    def add(col: str, value: Any) -> None:
        if col in cols:
            insert_cols.append(col)
            value_sql.append("?")
            params.append(value)

    add("permission_code", PERMISSION_CODE)
    add("permission_name", PERMISSION_NAME)
    add("permission_group", "SYSTEM")
    add("category", "SYSTEM")
    add("description", PERMISSION_DESC)
    add("is_active", 1)

    if "created_at" in cols:
        insert_cols.append("created_at")
        value_sql.append("SYSDATETIME()")

    if "updated_at" in cols:
        insert_cols.append("updated_at")
        value_sql.append("SYSDATETIME()")

    if "permission_code" not in insert_cols:
        raise RuntimeError("SSAI_PERMISSIONS.permission_code 컬럼이 없습니다.")

    cur = conn.cursor()
    cur.execute(
        f"""
        INSERT INTO dbo.SSAI_PERMISSIONS (
            {", ".join(insert_cols)}
        )
        VALUES (
            {", ".join(value_sql)}
        )
        """,
        *params,
    )

    permission = _fetch_one_dict(
        conn,
        """
        SELECT TOP 1 *
        FROM dbo.SSAI_PERMISSIONS
        WHERE permission_code = ?
        """,
        (PERMISSION_CODE,),
    )

    return {
        "action": "permission_inserted",
        "permission": permission,
    }


def _grant_permission_to_roles(conn) -> list[dict[str, Any]]:
    permission = _fetch_one_dict(
        conn,
        """
        SELECT TOP 1 permission_id, permission_code
        FROM dbo.SSAI_PERMISSIONS
        WHERE permission_code = ?
        """,
        (PERMISSION_CODE,),
    )

    if not permission:
        raise RuntimeError(f"권한을 찾지 못했습니다: {PERMISSION_CODE}")

    permission_id = int(permission["permission_id"])

    rp_cols = _columns(conn, "SSAI_ROLE_PERMISSIONS")

    results: list[dict[str, Any]] = []

    for role_code in GRANT_ROLE_CODES:
        role = _fetch_one_dict(
            conn,
            """
            SELECT TOP 1 role_id, role_code, role_name
            FROM dbo.SSAI_ROLES
            WHERE role_code = ?
            """,
            (role_code,),
        )

        if not role:
            results.append(
                {
                    "role_code": role_code,
                    "action": "role_not_found_skip",
                }
            )
            continue

        role_id = int(role["role_id"])

        existing = _fetch_one_dict(
            conn,
            """
            SELECT TOP 1 *
            FROM dbo.SSAI_ROLE_PERMISSIONS
            WHERE role_id = ?
              AND permission_id = ?
            """,
            (role_id, permission_id),
        )

        if existing:
            sets: list[str] = []
            params: list[Any] = []

            if "is_allowed" in rp_cols:
                sets.append("is_allowed = ?")
                params.append(1)

            if "is_active" in rp_cols:
                sets.append("is_active = ?")
                params.append(1)

            if "updated_at" in rp_cols:
                sets.append("updated_at = SYSDATETIME()")

            if sets:
                params.extend([role_id, permission_id])
                conn.cursor().execute(
                    f"""
                    UPDATE dbo.SSAI_ROLE_PERMISSIONS
                    SET {", ".join(sets)}
                    WHERE role_id = ?
                      AND permission_id = ?
                    """,
                    *params,
                )

            results.append(
                {
                    "role_code": role_code,
                    "role_id": role_id,
                    "action": "role_permission_updated",
                }
            )
            continue

        insert_cols: list[str] = []
        value_sql: list[str] = []
        params = []

        def add(col: str, value: Any) -> None:
            if col in rp_cols:
                insert_cols.append(col)
                value_sql.append("?")
                params.append(value)

        add("role_id", role_id)
        add("permission_id", permission_id)
        add("is_allowed", 1)
        add("is_active", 1)

        if "created_at" in rp_cols:
            insert_cols.append("created_at")
            value_sql.append("SYSDATETIME()")

        if "updated_at" in rp_cols:
            insert_cols.append("updated_at")
            value_sql.append("SYSDATETIME()")

        conn.cursor().execute(
            f"""
            INSERT INTO dbo.SSAI_ROLE_PERMISSIONS (
                {", ".join(insert_cols)}
            )
            VALUES (
                {", ".join(value_sql)}
            )
            """,
            *params,
        )

        results.append(
            {
                "role_code": role_code,
                "role_id": role_id,
                "action": "role_permission_inserted",
            }
        )

    return results


def _verify(conn) -> dict[str, Any]:
    permission_rows = _fetch_all_dicts(
        conn,
        """
        SELECT
            p.permission_id,
            p.permission_code,
            p.permission_name
        FROM dbo.SSAI_PERMISSIONS p
        WHERE p.permission_code = ?
        """,
        (PERMISSION_CODE,),
    )

    role_rows = _fetch_all_dicts(
        conn,
        """
        SELECT
            r.role_code,
            r.role_name,
            p.permission_code,
            rp.is_allowed
        FROM dbo.SSAI_ROLE_PERMISSIONS rp
        INNER JOIN dbo.SSAI_ROLES r
            ON rp.role_id = r.role_id
        INNER JOIN dbo.SSAI_PERMISSIONS p
            ON rp.permission_id = p.permission_id
        WHERE p.permission_code = ?
        ORDER BY r.role_code
        """,
        (PERMISSION_CODE,),
    )

    return {
        "permission": permission_rows,
        "role_permissions": role_rows,
    }


def main() -> None:
    with connect_ssai_db() as conn:
        permission_result = _insert_permission_if_missing(conn)
        role_results = _grant_permission_to_roles(conn)
        conn.commit()

        verify = _verify(conn)

    result = {
        "ok": True,
        "permission_result": permission_result,
        "role_results": role_results,
        "verify": verify,
    }

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()


    