"""Offline gate for the narrowly scoped admin company-switch bypass."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import sys
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.ui import ssai_login as login


class _Rerun(Exception):
    pass


class _FakeStreamlit:
    def __init__(self, *, password: str = "") -> None:
        self.session_state: dict[str, object] = {}
        self.password = password
        self.password_inputs = 0
        self.errors: list[str] = []
        self.options: list[str] = []
        self.submit_count = 0
        self.rerun_count = 0

    def title(self, *_args, **_kwargs):
        return None

    def caption(self, *_args, **_kwargs):
        return None

    def info(self, *_args, **_kwargs):
        return None

    def warning(self, *_args, **_kwargs):
        return None

    def success(self, *_args, **_kwargs):
        return None

    def error(self, message, **_kwargs):
        self.errors.append(str(message))

    def selectbox(self, _label, options, index=0, **_kwargs):
        self.options = list(options)
        return self.options[-1]

    def form(self, *_args, **_kwargs):
        return nullcontext()

    def text_input(self, *_args, **_kwargs):
        self.password_inputs += 1
        return self.password

    def columns(self, spec, **_kwargs):
        return [nullcontext() for _ in spec]

    def form_submit_button(self, *_args, **_kwargs):
        self.submit_count += 1
        return self.submit_count == 1

    def rerun(self):
        self.rerun_count += 1
        raise _Rerun()


def _user(login_id: str, user_type: str, sims_user_id: str = "SIMS01"):
    return SimpleNamespace(
        user_id=1,
        login_id=login_id,
        user_type=user_type,
        user_grade="MANAGER",
        sims_user_id=sims_user_id,
        default_company_id=1,
        nickname=login_id,
    )


COMPANIES = [
    {"company_id": 1, "company_name": "허용회사1", "company_code": "C1", "db_name": "ERP1"},
    {"company_id": 2, "company_name": "허용회사2", "company_code": "C2", "db_name": "ERP2"},
]


def _run(
    user,
    *,
    password: str = "",
    sims_password: str = "stored",
    sims_del_flag: str = "",
    sims_user_exists: bool = True,
) -> dict[str, object]:
    fake_st = _FakeStreamlit(password=password)
    selected: list[int] = []
    lookups: list[tuple[int, str]] = []
    verifies: list[tuple[str, str]] = []

    def _lookup(*, company_id: int, sims_user_id: str):
        lookups.append((company_id, sims_user_id))
        if not sims_user_exists:
            return None
        return {"sims_password": sims_password, "sims_del_flag": sims_del_flag}

    def _verify(value: str, stored: str) -> bool:
        verifies.append((value, stored))
        return value == stored

    def _selected(company, **_kwargs):
        selected.append(int(company["company_id"]))

    with (
        patch.object(login, "st", fake_st),
        patch.object(login, "get_current_user", return_value=user),
        patch.object(login, "get_active_companies_for_user", return_value=list(COMPANIES)),
        patch.object(login, "get_selected_company", return_value=COMPANIES[0]),
        patch.object(login, "get_sims_user_for_login", side_effect=_lookup),
        patch.object(login, "verify_sims_plain_password", side_effect=_verify),
        patch.object(login, "_clear_company_dependent_state"),
        patch.object(login, "_after_company_selected", side_effect=_selected),
        patch.object(login, "_log_form_lifecycle"),
    ):
        try:
            login.render_company_selector()
        except _Rerun:
            pass
    return {
        "st": fake_st,
        "selected": selected,
        "lookups": lookups,
        "verifies": verifies,
    }


def main() -> int:
    assert login._can_bypass_company_sims_password(_user("admin", "SSART_ADMIN")) is True
    for user in (
        _user("other-admin", "SSART_ADMIN"),
        _user("admin", "SSART_USER"),
        _user("admin", "WHOLESALE_ADMIN"),
        _user("ADMIN", "SSART_ADMIN"),
    ):
        assert login._can_bypass_company_sims_password(user) is False

    admin = _run(_user("admin", "SSART_ADMIN", sims_user_id=""))
    assert admin["selected"] == [2]
    assert admin["lookups"] == [(2, "admin")]
    assert admin["verifies"] == []
    assert admin["st"].password_inputs == 0

    inactive_admin = _run(
        _user("admin", "SSART_ADMIN", sims_user_id="admin"),
        sims_del_flag="E",
    )
    assert inactive_admin["selected"] == []
    assert inactive_admin["lookups"] == [(2, "admin")]
    assert inactive_admin["verifies"] == []
    assert inactive_admin["st"].errors == [
        "선택한 회원사 ERP DB의 SIMS 사용자가 삭제/비활성 상태입니다."
    ]

    missing_admin = _run(
        _user("admin", "SSART_ADMIN", sims_user_id="admin"),
        sims_user_exists=False,
    )
    assert missing_admin["selected"] == []
    assert missing_admin["lookups"] == [(2, "admin")]
    assert missing_admin["verifies"] == []

    other_admin = _run(_user("other-admin", "SSART_ADMIN"), password="")
    assert other_admin["selected"] == []
    assert other_admin["st"].password_inputs == 1
    assert other_admin["st"].errors == ["회원사/ERP DB 변경을 위해 SIMS Password를 입력하세요."]

    regular = _run(_user("regular", "SSART_USER"), password="stored")
    assert regular["selected"] == [2]
    assert regular["lookups"] == [(2, "SIMS01")]
    assert regular["verifies"] == [("stored", "stored")]
    assert regular["st"].password_inputs == 1

    expected_options = [login._company_selector_label(company) for company in COMPANIES]
    assert admin["st"].options == expected_options
    assert regular["st"].options == expected_options

    print("admin company switch SIMS password bypass: PASS")
    print("admin SIMS user existence/active-state checks: PASS")
    print("other SSART_ADMIN password required: PASS")
    print("regular user password contract: PASS")
    print("accessible company options unchanged: PASS")
    print("DB connection/write: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
