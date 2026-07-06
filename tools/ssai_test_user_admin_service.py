# tools/ssai_test_user_admin_service.py
#
# SS AI Phase 3
# 사용자 가입 신청 / 승인 / 회사 연결 / 역할 연결 서비스 테스트 욫
# create 2026/06/23
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.ssai_user_admin_service import (  # noqa: E402
    approve_user,
    create_signup_request,
    get_user_company_roles,
    list_pending_users,
)


def print_json(title: str, value) -> None:
    print()
    print(f"========== {title} ==========")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--login-id", default="wh_signup_test")
    parser.add_argument("--user-name", default="도매 가입 테스트")
    parser.add_argument("--requested-company-name", default="SS AI ETC 표준 ERP DB 01")
    parser.add_argument("--company-code", default="TEST_SIMS01")
    parser.add_argument("--sims-user-id", default="admin")
    parser.add_argument("--approve", action="store_true")
    args = parser.parse_args()

    result = create_signup_request(
        login_id=args.login_id,
        user_name=args.user_name,
        requested_company_name=args.requested_company_name,
        sims_user_id=args.sims_user_id,
        phone="",
        nickname=args.user_name,
    )
    print_json("가입 신청 생성/갱신", result)

    pending = list_pending_users(top=20)
    print_json("승인 대기 목록", pending)

    if args.approve:
        approved = approve_user(
            login_id=args.login_id,
            company_code=args.company_code,
            role_code="WHOLESALE_STAFF",
            user_grade="STAFF",
        )
        print_json("승인 처리", approved)

    final_state = get_user_company_roles(args.login_id)
    print_json("최종 사용자/회사/역할 상태", final_state)


if __name__ == "__main__":
    main()