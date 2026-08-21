"""Offline regression for the Knowledge management permission seed tool."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.seed_knowledge_manage_permissions import (  # noqa: E402
    EXPECTED_GRANTS,
    PERMISSIONS,
    REQUIRED_ROLE_CODES,
    SeedConflict,
    SeedState,
    _safe_error_message,
    build_plan,
    execute_seed,
    grant_matrix,
)


PERMISSION_COLUMNS = frozenset({"permission_id", "permission_code", "permission_name", "is_active"})
GRANT_COLUMNS = frozenset({"role_id", "permission_id", "is_allowed", "is_active"})


def _state(*, permission_mode: str = "missing", grants: list[dict] | None = None, role_override: dict | None = None) -> SeedState:
    roles = [
        {"role_id": index, "role_code": code, "is_active": 1}
        for index, code in enumerate(REQUIRED_ROLE_CODES, start=1)
    ]
    if role_override:
        roles = [role_override if row["role_code"] == role_override["role_code"] else row for row in roles]
    permissions = []
    if permission_mode != "missing":
        permissions = [
            {"permission_id": index, "permission_code": code, "is_active": 1}
            for index, code in enumerate(PERMISSIONS, start=101)
        ]
    if permission_mode == "inactive":
        permissions[0]["is_active"] = 0
    if permission_mode == "duplicate":
        permissions.append(dict(permissions[0], permission_id=999))
    return SeedState(
        roles=tuple(roles),
        permissions=tuple(permissions),
        grants=tuple(grants or []),
        permission_columns=PERMISSION_COLUMNS,
        role_permission_columns=GRANT_COLUMNS,
        select_count=4,
    )


def _exact_grants() -> list[dict]:
    return [
        {"permission_code": permission, "role_code": role, "is_allowed": 1, "is_active": 1}
        for permission, roles in EXPECTED_GRANTS.items()
        for role in roles
    ]


class _Cursor:
    rowcount = 1

    def execute(self, *_args, **_kwargs):
        return self


class _Connection:
    autocommit = False

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.timeout = 0

    def cursor(self):
        return _Cursor()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_missing_plan_and_exact_matrix() -> None:
    plan = build_plan(_state())
    assert set(plan.permission_inserts) == set(PERMISSIONS)
    assert set(plan.grant_inserts) == {
        (permission, role) for permission, roles in EXPECTED_GRANTS.items() for role in roles
    }
    assert not plan.conflicts
    assert EXPECTED_GRANTS["KNOWLEDGE_ERP_DB_READ"] == ("SYSTEM_ADMIN", "SSART_MANAGER")
    assert EXPECTED_GRANTS["KNOWLEDGE_PROJECT_SOURCE_READ"] == ("SYSTEM_ADMIN", "SSART_MANAGER")
    assert "SSART_STAFF" not in EXPECTED_GRANTS["KNOWLEDGE_PROJECT_SOURCE_READ"]
    assert "WHOLESALE_MANAGER" not in EXPECTED_GRANTS["KNOWLEDGE_PROJECT_SOURCE_READ"]


def test_existing_manage_permissions_plan_only_read_permission() -> None:
    state = _state(permission_mode="active", grants=_exact_grants())
    existing_permissions = tuple(
        row for row in state.permissions if row["permission_code"] != "KNOWLEDGE_ERP_DB_READ"
    )
    existing_grants = tuple(
        row for row in state.grants if row["permission_code"] != "KNOWLEDGE_ERP_DB_READ"
    )
    plan = build_plan(
        SeedState(
            roles=state.roles,
            permissions=existing_permissions,
            grants=existing_grants,
            permission_columns=state.permission_columns,
            role_permission_columns=state.role_permission_columns,
            select_count=state.select_count,
        )
    )
    assert plan.permission_inserts == ("KNOWLEDGE_ERP_DB_READ",)
    assert set(plan.grant_inserts) == {
        ("KNOWLEDGE_ERP_DB_READ", "SYSTEM_ADMIN"),
        ("KNOWLEDGE_ERP_DB_READ", "SSART_MANAGER"),
    }
    assert not plan.conflicts


def test_idempotent_no_op() -> None:
    state = _state(permission_mode="active", grants=_exact_grants())
    plan = build_plan(state)
    assert not plan.permission_inserts and not plan.grant_inserts and not plan.conflicts
    assert all(
        grant_matrix(state)[permission][role] == "allowed"
        for permission, roles in EXPECTED_GRANTS.items()
        for role in roles
    )


def test_conflicts_fail_closed() -> None:
    forbidden = _exact_grants() + [
        {"permission_code": "KNOWLEDGE_GLOBAL_MANAGE", "role_code": "SSART_STAFF", "is_allowed": 1, "is_active": 1}
    ]
    denied = _exact_grants()
    denied[0] = dict(denied[0], is_allowed=0)
    duplicate = _exact_grants() + [dict(_exact_grants()[0])]
    cases = [
        build_plan(_state(permission_mode="inactive", grants=[])).conflicts,
        build_plan(_state(permission_mode="duplicate", grants=[])).conflicts,
        build_plan(_state(permission_mode="active", grants=forbidden)).conflicts,
        build_plan(_state(permission_mode="active", grants=denied)).conflicts,
        build_plan(_state(permission_mode="active", grants=duplicate)).conflicts,
        build_plan(_state(role_override={"role_id": 1, "role_code": "SYSTEM_ADMIN", "is_active": 0})).conflicts,
    ]
    assert all(case for case in cases)


def test_dry_run_never_applies() -> None:
    conn = _Connection()
    called = []
    result = execute_seed(
        conn,
        apply=False,
        inspect_fn=lambda _conn: _state(),
        apply_fn=lambda *_args: called.append(True),
    )
    assert result["status"] == "ready_to_apply" and result["dml_executed"] is False
    assert not called and conn.commits == 0 and conn.rollbacks == 1
    assert conn.timeout == 10


def test_apply_verifies_and_commits_once() -> None:
    conn = _Connection()
    states = iter([_state(), _state(permission_mode="active", grants=_exact_grants())])
    applied = []
    result = execute_seed(
        conn,
        apply=True,
        inspect_fn=lambda _conn: next(states),
        apply_fn=lambda *_args: applied.append(True),
    )
    assert result["status"] == "applied" and result["dml_executed"] is True
    assert applied == [True] and conn.commits == 1 and conn.rollbacks == 0
    assert result["after_grant_matrix"] == result["planned_after_grant_matrix"]


def test_apply_failure_rolls_back() -> None:
    conn = _Connection()

    def fail(*_args):
        raise SeedConflict(["synthetic_failure"])

    try:
        execute_seed(conn, apply=True, inspect_fn=lambda _conn: _state(), apply_fn=fail)
    except SeedConflict as exc:
        assert exc.reason_codes == ("synthetic_failure",)
    else:
        raise AssertionError("apply failure must propagate")
    assert conn.commits == 0 and conn.rollbacks == 1


def test_attribute_error_message_is_sanitized() -> None:
    assert (
        _safe_error_message(AttributeError("'pyodbc.Cursor' object has no attribute 'timeout'"))
        == "pyodbc.Cursor object has no attribute 'timeout'"
    )
    assert _safe_error_message(AttributeError("connection detail: secret")) == "attribute_access_failed"
    assert _safe_error_message(RuntimeError("secret")) == ""


def main() -> None:
    tests = [
        test_missing_plan_and_exact_matrix,
        test_existing_manage_permissions_plan_only_read_permission,
        test_idempotent_no_op,
        test_conflicts_fail_closed,
        test_dry_run_never_applies,
        test_apply_verifies_and_commits_once,
        test_apply_failure_rolls_back,
        test_attribute_error_message_is_sanitized,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"RESULT OK tests={len(tests)}")


if __name__ == "__main__":
    main()
