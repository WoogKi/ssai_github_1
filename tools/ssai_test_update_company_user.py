# tools/ssai_test_update_company_user.py
#
# SS AI Phase 3
# 사용자 가입 신청 / 승인 / 회사 연결 / 역할 연결 서비스 데스트
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
    change_company_user_role,
    get_user_company_roles,
    set_company_user_access_active,
)


def print_json(title: str, value) -> None:
    print()
    print(f"========== {title} ==========")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manager-login-id", required=True)
    parser.add_argument("--target-login-id", required=True)
    parser.add_argument("--company-id", type=int, required=True)
    parser.add_argument("--role-code", default="")
    parser.add_argument("--disable", action="store_true")
    parser.add_argument("--enable", action="store_true")
    args = parser.parse_args()

    password = getpass.getpass("관리자 비밀번호 입력: ")

    result = authenticate_user(args.manager_login_id, password)

    if not result.success or not result.user:
        print(f"[FAIL] 관리자 로그인 실패: {result.fail_reason}")
        return

    manager = result.user
    permissions = set(result.permissions or [])

    allow_all_companies = bool(
        manager.user_type in ("SSART_ADMIN", "SSART_USER")
        and (
            "USER_MANAGE_ALL" in permissions
            or "USER_APPROVE" in permissions
        )
    )

    can_manage_company = bool(
        allow_all_companies
        or "USER_MANAGE_COMPANY" in permissions
    )

    print("[OK] 관리자 로그인 성공")
    print(f"[INFO] manager={manager.login_id}")
    print(f"[INFO] manager_user_id={manager.user_id}")
    print(f"[INFO] allow_all_companies={allow_all_companies}")
    print(f"[INFO] can_manage_company={can_manage_company}")

    if not can_manage_company:
        print("[FAIL] 사용자 관리 권한이 없습니다.")
        return

    before = get_user_company_roles(args.target_login_id)
    print_json("변경 전", before)

    if args.role_code:
        changed = change_company_user_role(
            manager_user_id=manager.user_id,
            target_login_id=args.target_login_id,
            company_id=args.company_id,
            role_code=args.role_code,
            allow_all_companies=allow_all_companies,
        )
        print_json("역할 변경 결과", changed)

    if args.disable:
        disabled = set_company_user_access_active(
            manager_user_id=manager.user_id,
            target_login_id=args.target_login_id,
            company_id=args.company_id,
            is_active=False,
            allow_all_companies=allow_all_companies,
        )
        print_json("사용 중지 결과", disabled)

    if args.enable:
        enabled = set_company_user_access_active(
            manager_user_id=manager.user_id,
            target_login_id=args.target_login_id,
            company_id=args.company_id,
            is_active=True,
            allow_all_companies=allow_all_companies,
        )
        print_json("재사용 결과", enabled)

    after = get_user_company_roles(args.target_login_id)
    print_json("변경 후", after)


if __name__ == "__main__":
    main()