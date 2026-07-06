#  tools/ssai_test_managed_users.py
#
# SS AI Phase 3
# 사용자 가입 신청 / 승인 / 회사 연결 / 역할 연결 서비스 테스트 2
# create 2026/06/23

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.ssai_auth_service import authenticate_user  # noqa: E402
from app.services.ssai_user_admin_service import (  # noqa: E402
    get_manageable_companies,
    list_managed_company_users,
)


def print_json(title: str, value) -> None:
    print()
    print(f"========== {title} ==========")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--login-id", required=True)
    parser.add_argument("--company-id", type=int, default=None)
    args = parser.parse_args()

    password = getpass.getpass("비밀번호 입력: ")

    result = authenticate_user(args.login_id, password)

    if not result.success or not result.user:
        print(f"[FAIL] 로그인 실패: {result.fail_reason}")
        return

    user = result.user
    permissions = set(result.permissions or [])

    allow_all_companies = bool(
        user.user_type in ("SSART_ADMIN", "SSART_USER")
        and (
            "USER_MANAGE_ALL" in permissions
            or "USER_APPROVE" in permissions
        )
    )

    can_manage_company = bool(
        allow_all_companies
        or "USER_MANAGE_COMPANY" in permissions
    )

    print("[OK] 로그인 성공")
    print(f"[INFO] login_id={user.login_id}")
    print(f"[INFO] user_id={user.user_id}")
    print(f"[INFO] user_type={user.user_type}")
    print(f"[INFO] user_grade={user.user_grade}")
    print(f"[INFO] default_company_id={user.default_company_id}")
    print(f"[INFO] permissions={sorted(permissions)}")
    print(f"[INFO] allow_all_companies={allow_all_companies}")
    print(f"[INFO] can_manage_company={can_manage_company}")

    if not can_manage_company:
        print("[FAIL] 사용자 관리 권한이 없습니다.")
        return

    companies = get_manageable_companies(
        manager_user_id=user.user_id,
        allow_all_companies=allow_all_companies,
    )

    print_json("관리 가능 회사", companies)

    users = list_managed_company_users(
        manager_user_id=user.user_id,
        allow_all_companies=allow_all_companies,
        company_id=args.company_id,
        include_inactive=True,
        top=500,
    )

    print_json("관리 가능 사용자", users)


if __name__ == "__main__":
    main()