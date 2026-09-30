"""Offline regression for inactive company user-management visibility."""

from __future__ import annotations

import sys
import re
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import ssai_user_admin_service as service  # noqa: E402


def _normalized(sql: str) -> str:
    return " ".join(sql.split())


def _has_company_active_filter(sql: str) -> bool:
    return bool(re.search(r"(?<![A-Za-z0-9_])c\.is_active = 1", sql))


def _company_fetch(_conn, sql: str, params=()):
    normalized = _normalized(sql)
    include_inactive = not _has_company_active_filter(normalized) and "WHERE is_active = 1" not in normalized
    if "FROM dbo.SSAI_COMPANIES" in normalized:
        return ([{"company_id": 1, "is_active": 1}, {"company_id": 2, "is_active": 0}]
                if include_inactive else [{"company_id": 1, "is_active": 1}])
    assert params == (91,)
    return ([{"company_id": 1, "is_active": 1}, {"company_id": 2, "is_active": 0}]
            if include_inactive else [{"company_id": 1, "is_active": 1}])


def _user_fetch(_conn, sql: str, params=()):
    normalized = _normalized(sql)
    if "SELECT uc.company_id" in normalized:
        assert params == (91,)
        return ([{"company_id": 1}, {"company_id": 2}]
                if not _has_company_active_filter(normalized) else [{"company_id": 1}])

    assert "FROM dbo.SSAI_USER_COMPANIES uc" in normalized
    is_inactive_view = "u.is_active = 1" not in normalized
    if is_inactive_view:
        assert "uc.is_active = 1" not in normalized
        assert not _has_company_active_filter(normalized)
        return [
            {"login_id": "active_company_active_user", "company_id": 1},
            {"login_id": "inactive_company_active_user", "company_id": 2},
            {"login_id": "active_company_inactive_relation", "company_id": 1},
            {"login_id": "inactive_user", "company_id": 1},
        ]
    assert "uc.is_active = 1" in normalized
    assert _has_company_active_filter(normalized)
    return [{"login_id": "active_company_active_user", "company_id": 1}]


def main() -> None:
    connection = object()
    with patch.object(service, "connect_ssai_db", return_value=nullcontext(connection)):
        with patch.object(service, "_fetch_all_dicts", side_effect=_company_fetch):
            # Full administrators see all companies only when the switch is enabled.
            assert [row["company_id"] for row in service.get_manageable_companies(
                manager_user_id=91, allow_all_companies=True, include_inactive=False
            )] == [1]
            assert [row["company_id"] for row in service.get_manageable_companies(
                manager_user_id=91, allow_all_companies=True, include_inactive=True
            )] == [1, 2]

            # Member administrators never gain a company outside their active assignment.
            assert [row["company_id"] for row in service.get_manageable_companies(
                manager_user_id=91, allow_all_companies=False, include_inactive=False
            )] == [1]
            assert [row["company_id"] for row in service.get_manageable_companies(
                manager_user_id=91, allow_all_companies=False, include_inactive=True
            )] == [1, 2]

        with patch.object(service, "_fetch_all_dicts", side_effect=_user_fetch):
            off_rows = service.list_managed_company_users(
                manager_user_id=91, allow_all_companies=False, include_inactive=False
            )
            on_rows = service.list_managed_company_users(
                manager_user_id=91, allow_all_companies=False, include_inactive=True
            )
            try:
                service.list_managed_company_users(
                    manager_user_id=91,
                    allow_all_companies=False,
                    company_id=3,
                    include_inactive=True,
                )
            except PermissionError:
                pass
            else:
                raise AssertionError("inactive view widened the member-admin company scope")

    assert [row["login_id"] for row in off_rows] == ["active_company_active_user"]
    assert [row["login_id"] for row in on_rows] == [
        "active_company_active_user",
        "inactive_company_active_user",
        "active_company_inactive_relation",
        "inactive_user",
    ]

    ui_source = (ROOT / "app" / "ui" / "ssai_admin.py").read_text(encoding="utf-8")
    assert ui_source.index('"비활성 회원사 ERP DB 연결 포함"') < ui_source.index("get_manageable_companies(")
    assert "include_inactive=include_inactive" in ui_source
    print("RESULT OK tests=7 db_connection_attempts=0")


if __name__ == "__main__":
    main()
