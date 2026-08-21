"""Dry-run-first seed tool for Knowledge permissions.

This tool is never imported or invoked by application startup. Run it without
arguments to inspect and plan; use ``--apply`` only after reviewing the plan.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.ssai_auth_service import connect_ssai_db  # noqa: E402


QUERY_TIMEOUT_SECONDS = 10

PERMISSIONS: dict[str, dict[str, str]] = {
    "KNOWLEDGE_GLOBAL_MANAGE": {
        "permission_name": "Global Knowledge management",
        "description": "Manage approved global Knowledge documents",
    },
    "KNOWLEDGE_COMPANY_MANAGE": {
        "permission_name": "Company Knowledge management",
        "description": "Manage approved Knowledge documents for the selected company",
    },
    "KNOWLEDGE_ERP_DB_READ": {
        "permission_name": "ERP DB internal Knowledge read",
        "description": "Read approved ERP DB internal Knowledge documents",
    },
    "KNOWLEDGE_PROJECT_SOURCE_READ": {
        "permission_name": "Project Source Knowledge read",
        "description": "Read approved Project Source Knowledge in explicit technical detail mode",
    },
}

EXPECTED_GRANTS: dict[str, tuple[str, ...]] = {
    "KNOWLEDGE_GLOBAL_MANAGE": ("SYSTEM_ADMIN",),
    "KNOWLEDGE_COMPANY_MANAGE": (
        "SYSTEM_ADMIN",
        "SSART_MANAGER",
        "WHOLESALE_MANAGER",
    ),
    "KNOWLEDGE_ERP_DB_READ": (
        "SYSTEM_ADMIN",
        "SSART_MANAGER",
    ),
    "KNOWLEDGE_PROJECT_SOURCE_READ": (
        "SYSTEM_ADMIN",
        "SSART_MANAGER",
    ),
}

REQUIRED_ROLE_CODES = (
    "SYSTEM_ADMIN",
    "SSART_MANAGER",
    "SSART_STAFF",
    "WHOLESALE_MANAGER",
    "WHOLESALE_STAFF",
    "WHOLESALE_READONLY",
)


class SeedConflict(RuntimeError):
    def __init__(self, reason_codes: Iterable[str]) -> None:
        self.reason_codes = tuple(sorted(set(reason_codes)))
        super().__init__("knowledge_permission_seed_conflict")


@dataclass(frozen=True)
class SeedState:
    roles: tuple[dict[str, Any], ...]
    permissions: tuple[dict[str, Any], ...]
    grants: tuple[dict[str, Any], ...]
    permission_columns: frozenset[str]
    role_permission_columns: frozenset[str]
    select_count: int


@dataclass(frozen=True)
class SeedPlan:
    permission_inserts: tuple[str, ...]
    grant_inserts: tuple[tuple[str, str], ...]
    conflicts: tuple[str, ...]


def _fetch_dicts(cursor: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    rows = cursor.execute(sql, *params).fetchall()
    columns = [str(item[0]) for item in cursor.description]
    return [dict(zip(columns, row)) for row in rows]


def _placeholders(values: Iterable[object]) -> str:
    return ", ".join("?" for _ in values)


def inspect_state(conn: Any) -> SeedState:
    """Read the complete seed boundary in four bounded SELECT statements."""
    permission_codes = tuple(PERMISSIONS)
    role_codes = tuple(REQUIRED_ROLE_CODES)
    cursor = conn.cursor()

    column_rows = _fetch_dicts(
        cursor,
        """
        SELECT TABLE_NAME, COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = N'dbo'
          AND TABLE_NAME IN (N'SSAI_PERMISSIONS', N'SSAI_ROLE_PERMISSIONS')
        """,
    )
    columns: dict[str, set[str]] = defaultdict(set)
    for row in column_rows:
        columns[str(row["TABLE_NAME"])].add(str(row["COLUMN_NAME"]))
    permission_columns = frozenset(columns["SSAI_PERMISSIONS"])
    role_permission_columns = frozenset(columns["SSAI_ROLE_PERMISSIONS"])
    required_permission_columns = {"permission_id", "permission_code"}
    required_grant_columns = {"role_id", "permission_id", "is_allowed"}
    if not required_permission_columns.issubset(permission_columns):
        raise SeedConflict(["permission_schema_mismatch"])
    if not required_grant_columns.issubset(role_permission_columns):
        raise SeedConflict(["role_permission_schema_mismatch"])

    role_rows = _fetch_dicts(
        cursor,
        f"""
        SELECT role_id, role_code, is_active
        FROM dbo.SSAI_ROLES
        WHERE role_code IN ({_placeholders(role_codes)})
        ORDER BY role_code, role_id
        """,
        role_codes,
    )
    permission_rows = _fetch_dicts(
        cursor,
        f"""
        SELECT permission_id, permission_code,
               {('is_active' if 'is_active' in permission_columns else 'CAST(1 AS bit)')} AS is_active
        FROM dbo.SSAI_PERMISSIONS
        WHERE permission_code IN ({_placeholders(permission_codes)})
        ORDER BY permission_code, permission_id
        """,
        permission_codes,
    )
    grant_active_sql = "rp.is_active" if "is_active" in role_permission_columns else "CAST(1 AS bit)"
    grant_rows = _fetch_dicts(
        cursor,
        f"""
        SELECT p.permission_code, r.role_code, rp.is_allowed,
               {grant_active_sql} AS is_active
        FROM dbo.SSAI_ROLE_PERMISSIONS rp
        INNER JOIN dbo.SSAI_PERMISSIONS p ON p.permission_id = rp.permission_id
        LEFT JOIN dbo.SSAI_ROLES r ON r.role_id = rp.role_id
        WHERE p.permission_code IN ({_placeholders(permission_codes)})
        ORDER BY p.permission_code, r.role_code, rp.role_id
        """,
        permission_codes,
    )
    return SeedState(
        roles=tuple(role_rows),
        permissions=tuple(permission_rows),
        grants=tuple(grant_rows),
        permission_columns=permission_columns,
        role_permission_columns=role_permission_columns,
        select_count=4,
    )


def build_plan(state: SeedState) -> SeedPlan:
    conflicts: list[str] = []
    permission_inserts: list[str] = []
    grant_inserts: list[tuple[str, str]] = []

    roles_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in state.roles:
        roles_by_code[str(row.get("role_code") or "")].append(row)
    for role_code in REQUIRED_ROLE_CODES:
        rows = roles_by_code[role_code]
        if not rows:
            conflicts.append(f"role_missing:{role_code}")
        elif len(rows) != 1:
            conflicts.append(f"role_duplicate:{role_code}")
        elif int(rows[0].get("is_active") or 0) != 1:
            conflicts.append(f"role_inactive:{role_code}")

    permissions_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in state.permissions:
        permissions_by_code[str(row.get("permission_code") or "")].append(row)
    for permission_code in PERMISSIONS:
        rows = permissions_by_code[permission_code]
        if not rows:
            permission_inserts.append(permission_code)
        elif len(rows) != 1:
            conflicts.append(f"permission_duplicate:{permission_code}")
        elif int(rows[0].get("is_active") or 0) != 1:
            conflicts.append(f"permission_inactive:{permission_code}")

    grants_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in state.grants:
        permission_code = str(row.get("permission_code") or "")
        role_code = str(row.get("role_code") or "<orphan>")
        grants_by_key[(permission_code, role_code)].append(row)

    for (permission_code, role_code), rows in grants_by_key.items():
        if role_code not in EXPECTED_GRANTS.get(permission_code, ()):
            conflicts.append(f"unexpected_grant:{permission_code}:{role_code}")
            continue
        if len(rows) != 1:
            conflicts.append(f"grant_duplicate:{permission_code}:{role_code}")
            continue
        row = rows[0]
        if int(row.get("is_allowed") or 0) != 1:
            conflicts.append(f"grant_denied:{permission_code}:{role_code}")
        if int(row.get("is_active") or 0) != 1:
            conflicts.append(f"grant_inactive:{permission_code}:{role_code}")

    for permission_code, role_codes in EXPECTED_GRANTS.items():
        for role_code in role_codes:
            if not grants_by_key.get((permission_code, role_code)):
                grant_inserts.append((permission_code, role_code))

    return SeedPlan(
        permission_inserts=tuple(permission_inserts),
        grant_inserts=tuple(grant_inserts),
        conflicts=tuple(sorted(set(conflicts))),
    )


def grant_matrix(state: SeedState) -> dict[str, dict[str, str]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in state.grants:
        grouped[(str(row.get("permission_code") or ""), str(row.get("role_code") or "<orphan>"))].append(row)
    matrix: dict[str, dict[str, str]] = {}
    for permission_code in PERMISSIONS:
        role_states: dict[str, str] = {}
        for role_code in REQUIRED_ROLE_CODES:
            rows = grouped.get((permission_code, role_code), [])
            if not rows:
                value = "missing"
            elif len(rows) != 1:
                value = "duplicate"
            elif int(rows[0].get("is_allowed") or 0) != 1:
                value = "denied"
            elif int(rows[0].get("is_active") or 0) != 1:
                value = "inactive"
            else:
                value = "allowed"
            role_states[role_code] = value
        matrix[permission_code] = role_states
    return matrix


def planned_matrix() -> dict[str, dict[str, str]]:
    return {
        permission_code: {
            role_code: "allowed" if role_code in EXPECTED_GRANTS[permission_code] else "missing"
            for role_code in REQUIRED_ROLE_CODES
        }
        for permission_code in PERMISSIONS
    }


def _insert_permission(conn: Any, permission_code: str, columns: frozenset[str]) -> None:
    metadata = PERMISSIONS[permission_code]
    values: dict[str, Any] = {
        "permission_code": permission_code,
        "permission_name": metadata["permission_name"],
        "permission_group": "KNOWLEDGE",
        "category": "KNOWLEDGE",
        "description": metadata["description"],
        "is_active": 1,
    }
    insert_columns: list[str] = []
    value_sql: list[str] = []
    params: list[Any] = []
    for column, value in values.items():
        if column in columns:
            insert_columns.append(column)
            value_sql.append("?")
            params.append(value)
    for column in ("created_at", "updated_at"):
        if column in columns:
            insert_columns.append(column)
            value_sql.append("SYSDATETIME()")
    if "permission_code" not in insert_columns:
        raise SeedConflict(["permission_schema_mismatch"])
    conn.cursor().execute(
        f"INSERT INTO dbo.SSAI_PERMISSIONS ({', '.join(insert_columns)}) VALUES ({', '.join(value_sql)})",
        *params,
    )


def _insert_grant(
    conn: Any,
    permission_code: str,
    role_code: str,
    role_permission_columns: frozenset[str],
    permission_columns: frozenset[str],
) -> None:
    insert_columns = ["role_id", "permission_id", "is_allowed"]
    select_values = ["r.role_id", "p.permission_id", "1"]
    if "is_active" in role_permission_columns:
        insert_columns.append("is_active")
        select_values.append("1")
    for column in ("created_at", "updated_at"):
        if column in role_permission_columns:
            insert_columns.append(column)
            select_values.append("SYSDATETIME()")
    conn.cursor().execute(
        f"""
        INSERT INTO dbo.SSAI_ROLE_PERMISSIONS ({', '.join(insert_columns)})
        SELECT {', '.join(select_values)}
        FROM dbo.SSAI_ROLES r
        CROSS JOIN dbo.SSAI_PERMISSIONS p
        WHERE r.role_code = ? AND r.is_active = 1
          AND p.permission_code = ?
          {('AND p.is_active = 1' if 'is_active' in permission_columns else '')}
          AND NOT EXISTS (
              SELECT 1 FROM dbo.SSAI_ROLE_PERMISSIONS rp
              WHERE rp.role_id = r.role_id AND rp.permission_id = p.permission_id
          )
        """,
        role_code,
        permission_code,
    )


def apply_plan(conn: Any, state: SeedState, plan: SeedPlan) -> None:
    for permission_code in plan.permission_inserts:
        _insert_permission(conn, permission_code, state.permission_columns)
    for permission_code, role_code in plan.grant_inserts:
        _insert_grant(
            conn,
            permission_code,
            role_code,
            state.role_permission_columns,
            state.permission_columns,
        )


def execute_seed(
    conn: Any,
    *,
    apply: bool,
    inspect_fn: Callable[[Any], SeedState] = inspect_state,
    apply_fn: Callable[[Any, SeedState, SeedPlan], None] = apply_plan,
) -> dict[str, Any]:
    if bool(getattr(conn, "autocommit", False)):
        conn.autocommit = False
    try:
        conn.timeout = QUERY_TIMEOUT_SECONDS
        conn.cursor().execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        before = inspect_fn(conn)
        plan = build_plan(before)
        result: dict[str, Any] = {
            "ok": not plan.conflicts,
            "mode": "apply" if apply else "dry-run",
            "query_timeout_seconds": QUERY_TIMEOUT_SECONDS,
            "before_grant_matrix": grant_matrix(before),
            "planned_after_grant_matrix": planned_matrix(),
            "permission_inserts": list(plan.permission_inserts),
            "grant_inserts": [
                {"permission_code": permission_code, "role_code": role_code}
                for permission_code, role_code in plan.grant_inserts
            ],
            "conflicts": list(plan.conflicts),
            "select_count": before.select_count,
            "retry_count": 0,
            "dml_executed": False,
        }
        if plan.conflicts:
            conn.rollback()
            result["status"] = "conflict"
            return result
        if not apply:
            conn.rollback()
            result["status"] = "ready_to_apply" if (plan.permission_inserts or plan.grant_inserts) else "no_op"
            return result

        apply_fn(conn, before, plan)
        after = inspect_fn(conn)
        after_plan = build_plan(after)
        if after_plan.conflicts or after_plan.permission_inserts or after_plan.grant_inserts:
            raise SeedConflict(after_plan.conflicts or ["post_apply_exact_matrix_mismatch"])
        result["select_count"] += after.select_count
        result["after_grant_matrix"] = grant_matrix(after)
        result["dml_executed"] = bool(plan.permission_inserts or plan.grant_inserts)
        result["status"] = "applied" if result["dml_executed"] else "no_op"
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def _safe_sqlstate(exc: Exception) -> str:
    for value in getattr(exc, "args", ()):
        text = str(value).strip()
        if len(text) == 5 and text.isalnum():
            return text
    return ""


def _safe_error_message(exc: Exception) -> str:
    if not isinstance(exc, AttributeError):
        return ""
    match = re.fullmatch(
        r"'([A-Za-z0-9_.]+)' object has no attribute '([A-Za-z0-9_]+)'",
        str(exc).strip(),
    )
    if not match:
        return "attribute_access_failed"
    object_type, attribute_name = match.groups()
    return f"{object_type} object has no attribute '{attribute_name}'"


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed Knowledge management permissions (default: dry-run).")
    parser.add_argument("--apply", action="store_true", help="Apply the reviewed inserts in one transaction.")
    args = parser.parse_args()
    try:
        with connect_ssai_db() as conn:
            result = execute_seed(conn, apply=bool(args.apply))
    except SeedConflict as exc:
        result = {
            "ok": False,
            "mode": "apply" if args.apply else "dry-run",
            "status": "conflict",
            "conflicts": list(exc.reason_codes),
            "retry_count": 0,
        }
    except Exception as exc:
        result = {
            "ok": False,
            "mode": "apply" if args.apply else "dry-run",
            "status": "error",
            "error_type": type(exc).__name__,
            "sqlstate": _safe_sqlstate(exc),
            "retry_count": 0,
        }
        safe_message = _safe_error_message(exc)
        if safe_message:
            result["error_message"] = safe_message
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
