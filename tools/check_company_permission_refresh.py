"""Regression for the common selected-company permission refresh boundary."""

from __future__ import annotations

import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.ssai_auth_service import AuthUser  # noqa: E402
from app.ui import ssai_login as login  # noqa: E402


def _user() -> AuthUser:
    return AuthUser(
        user_id=41,
        login_id="fixture",
        user_name="fixture",
        nickname=None,
        phone=None,
        user_type="WHOLESALE_ADMIN",
        user_grade="MANAGER",
        default_company_id=4,
        sims_user_id="fixture",
        approval_status="APPROVED",
        is_active=True,
    )


def _select(state: dict, company_id: int, permissions) -> None:
    company = {"company_id": company_id, "company_name": "fixture"}
    connection = object()
    with (
        patch.object(login.st, "session_state", state),
        patch.object(login, "connect_ssai_db", return_value=nullcontext(connection)),
        patch.object(login, "get_user_permissions", return_value=permissions) as get_permissions,
        patch.object(login, "ensure_user_storage_dirs"),
        patch.object(login, "build_login_profile", return_value={"company_id": company_id}),
    ):
        login._after_company_selected(company)
        get_permissions.assert_called_once_with(
            connection, user_id=41, company_id=company_id
        )


def main() -> None:
    # Company-specific wholesale permissions must replace, not merge with, the
    # permission set that was calculated at login for company 4.
    state = {
        login.SESSION_AUTH_USER: _user(),
        login.SESSION_AUTH_PERMISSIONS: ["KPI_READ", "RAG_USE", "USER_MANAGE_COMPANY"],
    }
    _select(state, 6, ["IO_READ", "MASTER_READ"])
    assert state[login.SESSION_AUTH_PERMISSIONS] == ["IO_READ", "MASTER_READ"]

    # Verify has/require against the exact session dictionary passed to the
    # production functions.
    with patch.object(login.st, "session_state", state):
        assert login.has_permission("IO_READ") is True
        assert login.require_permission("RAG_USE", show_error=False) is False
        assert login.require_permission("USER_MANAGE_COMPANY", show_error=False) is False

    # A global role's permissions are still refreshed to the selected company,
    # but retain the same result when the auth service returns them for both.
    global_state = {login.SESSION_AUTH_USER: _user(), login.SESSION_AUTH_PERMISSIONS: []}
    _select(global_state, 6, ["KPI_READ", "RAG_USE", "UPLOAD_FILE"])
    assert global_state[login.SESSION_AUTH_PERMISSIONS] == ["KPI_READ", "RAG_USE", "UPLOAD_FILE"]

    # WHOLESALE_READONLY's current policy does not receive RAG_USE.
    readonly_state = {login.SESSION_AUTH_USER: _user(), login.SESSION_AUTH_PERMISSIONS: ["RAG_USE"]}
    _select(readonly_state, 6, ["IO_READ", "MASTER_READ"])
    assert "RAG_USE" not in readonly_state[login.SESSION_AUTH_PERMISSIONS]

    # Refresh failures fail closed instead of retaining company 4 permissions.
    failed_state = {login.SESSION_AUTH_USER: _user(), login.SESSION_AUTH_PERMISSIONS: ["RAG_USE", "UPLOAD_FILE"]}
    with (
        patch.object(login.st, "session_state", failed_state),
        patch.object(login, "connect_ssai_db", return_value=nullcontext(object())),
        patch.object(login, "get_user_permissions", side_effect=RuntimeError("fixture")),
        patch.object(login, "ensure_user_storage_dirs"),
        patch.object(login, "build_login_profile", return_value={"company_id": 6}),
    ):
        login._after_company_selected({"company_id": 6, "company_name": "fixture"})
        assert failed_state[login.SESSION_AUTH_PERMISSIONS] == []
        assert login.require_permission("RAG_USE", show_error=False) is False

    print("RESULT OK tests=4")


if __name__ == "__main__":
    main()
