"""Focused regression for the read-only admin Knowledge permission readback."""

from __future__ import annotations

import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import ssai_user_admin_service as service  # noqa: E402


class _FixtureState:
    company_id = 4
    role_code = "WHOLESALE_MANAGER"
    permission_codes = {"RAG_USE"}
    permission_reads = 0


def _roles_for_state(*_args, **_kwargs):
    return [
        {
            "role_code": _FixtureState.role_code,
            "role_name": "fixture role",
        }
    ]


def _assert_manager_scope(_conn, *, manager_user_id, company_id, allow_all_companies=False):
    assert manager_user_id == 1
    assert allow_all_companies is False
    if company_id != _FixtureState.company_id:
        raise PermissionError("fixture manager scope denied")


def _target_user(_conn, *, target_login_id):
    assert target_login_id == "target"
    return {"user_id": 22, "login_id": "target"}


def _assert_target_company(_conn, *, user_id, company_id):
    assert user_id == 22
    if company_id != _FixtureState.company_id:
        raise PermissionError("fixture target company denied")


def _effective_permissions(_conn, *, user_id, company_id):
    assert user_id == 22
    assert company_id == _FixtureState.company_id
    _FixtureState.permission_reads += 1
    return sorted(_FixtureState.permission_codes)


def _readback(company_id: int) -> dict:
    return service.get_managed_user_knowledge_permissions(
        manager_user_id=1,
        target_login_id="target",
        company_id=company_id,
        allow_all_companies=False,
    )


def _allowed_codes(result: dict) -> set[str]:
    return {
        str(row["permission_code"])
        for row in result["effective_permissions"]
        if row["allowed"]
    }


def main() -> None:
    connection = object()
    with (
        patch.object(service, "connect_ssai_db", return_value=nullcontext(connection)),
        patch.object(service, "_assert_manager_can_manage_company", side_effect=_assert_manager_scope),
        patch.object(service, "_get_user_by_id_or_login", side_effect=_target_user),
        patch.object(service, "_assert_user_belongs_to_company", side_effect=_assert_target_company),
        patch.object(service, "_fetch_all_dicts", side_effect=_roles_for_state),
        patch.object(service, "get_user_permissions", side_effect=_effective_permissions),
    ):
        # Selected user/company readback exposes only the five fixed Knowledge permissions.
        _FixtureState.company_id = 4
        _FixtureState.role_code = "WHOLESALE_MANAGER"
        _FixtureState.permission_codes = {"RAG_USE", "KNOWLEDGE_COMPANY_MANAGE"}
        before = _readback(4)
        assert before["target_user_id"] == 22
        assert before["roles"] == [{"role_code": "WHOLESALE_MANAGER", "role_name": "fixture role"}]
        assert _allowed_codes(before) == {"RAG_USE", "KNOWLEDGE_COMPANY_MANAGE"}
        assert [row["permission_code"] for row in before["effective_permissions"]] == list(
            service.KNOWLEDGE_EFFECTIVE_PERMISSION_CODES
        )

        # A role change is reflected by a fresh readback; no UI-side permission cache exists.
        _FixtureState.role_code = "SYSTEM_ADMIN"
        _FixtureState.permission_codes = set(service.KNOWLEDGE_EFFECTIVE_PERMISSION_CODES)
        after = _readback(4)
        assert after["roles"][0]["role_code"] == "SYSTEM_ADMIN"
        assert _allowed_codes(after) == set(service.KNOWLEDGE_EFFECTIVE_PERMISSION_CODES)

        # Company selection is part of the lookup and cannot reuse company 4 results.
        _FixtureState.company_id = 6
        _FixtureState.role_code = "WHOLESALE_READONLY"
        _FixtureState.permission_codes = set()
        switched = _readback(6)
        assert switched["company_id"] == 6
        assert switched["roles"][0]["role_code"] == "WHOLESALE_READONLY"
        assert _allowed_codes(switched) == set()

        # A non-manager is denied before effective permissions are read.
        reads_before_denial = _FixtureState.permission_reads
        try:
            _readback(4)
        except PermissionError:
            pass
        else:
            raise AssertionError("out-of-scope company was not fail-closed")
        assert _FixtureState.permission_reads == reads_before_denial

    # A calculation failure propagates to the UI instead of rendering a false empty result.
    with (
        patch.object(service, "connect_ssai_db", return_value=nullcontext(connection)),
        patch.object(service, "_assert_manager_can_manage_company"),
        patch.object(service, "_get_user_by_id_or_login", return_value={"user_id": 22, "login_id": "target"}),
        patch.object(service, "_assert_user_belongs_to_company"),
        patch.object(service, "_fetch_all_dicts", return_value=[]),
        patch.object(service, "get_user_permissions", side_effect=RuntimeError("fixture read failure")),
    ):
        try:
            _readback(4)
        except RuntimeError:
            pass
        else:
            raise AssertionError("permission read failure did not fail closed")

    source = (ROOT / "app" / "ui" / "ssai_admin.py").read_text(encoding="utf-8")
    assert "get_managed_user_knowledge_permissions" in source
    assert "개별 권한은 이 화면에서 변경하지 않습니다" in source
    print("RESULT OK tests=5")


if __name__ == "__main__":
    main()
