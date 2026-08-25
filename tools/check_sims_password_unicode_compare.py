"""DB 없이 SIMS 평문 비밀번호 UTF-8 constant-time 비교 계약을 검증한다."""

from __future__ import annotations

import ast
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.ssai_auth_service import verify_sims_plain_password  # noqa: E402


def _assert_route_contract() -> None:
    login_source = (ROOT / "app" / "ui" / "ssai_login.py").read_text(encoding="utf-8")
    service_source = (ROOT / "app" / "services" / "ssai_auth_service.py").read_text(
        encoding="utf-8"
    )
    ast.parse(login_source)
    ast.parse(service_source)
    assert "verify_sims_plain_password(" in login_source
    assert "authenticate_wholesale_sims_password(" in login_source
    assert "return authenticate_wholesale_user(login_id, sims_password)" in service_source


def main() -> None:
    cases = [
        ("ascii_match", "Pass123!", "Pass123!", True),
        ("ascii_mismatch", "Pass123!", "Pass124!", False),
        ("korean_match", "한글비밀번호", "한글비밀번호", True),
        ("korean_mismatch", "한글비밀번호", "한글비밀번화", False),
        ("mixed_match", "SIMS한글12!@", "SIMS한글12!@", True),
        ("mixed_mismatch", "SIMS한글12!@", "SIMS한글12!#", False),
        ("blank", "", "", False),
        ("none_input", None, "한글비밀번호", False),
        ("none_stored", "한글비밀번호", None, False),
        ("trimmed_match", "  한글비밀번호  ", "한글비밀번호", True),
    ]
    for name, input_password, sims_password, expected in cases:
        actual = verify_sims_plain_password(input_password, sims_password)
        assert actual is expected, name

    _assert_route_contract()
    print("PASS: SIMS Unicode password compare and shared login/re-auth routes")


if __name__ == "__main__":
    main()
