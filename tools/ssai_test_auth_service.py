# tools/ssai_test_auth_service.py
# admin 로그인 검증
#  → 사용자 정보 조회
#  → 역할/권한 조회
#  → 접근 가능 회사 목록 조회
# Create 2026/06/22



from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.ssai_auth_service import (  # noqa: E402
    authenticate_ssart_user,
    get_active_companies_for_user,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--login-id", default="admin")
    args = parser.parse_args()

    password = getpass.getpass("비밀번호 입력: ")

    result = authenticate_ssart_user(args.login_id, password)

    if not result.success:
        print(f"[FAIL] 로그인 실패: {result.fail_reason}")
        return

    assert result.user is not None

    print("[OK] 로그인 성공")
    print(f"[INFO] user_id={result.user.user_id}")
    print(f"[INFO] login_id={result.user.login_id}")
    print(f"[INFO] user_name={result.user.user_name}")
    print(f"[INFO] user_type={result.user.user_type}")
    print(f"[INFO] user_grade={result.user.user_grade}")

    permissions = result.permissions or []
    print(f"[OK] permissions count = {len(permissions)}")
    print("[INFO] permissions:")
    for code in permissions:
        print(f"  - {code}")

    companies = get_active_companies_for_user(result.user)
    print(f"[OK] accessible companies count = {len(companies)}")
    for c in companies:
        print(
            f"  - company_id={c['company_id']}, "
            f"code={c['company_code']}, "
            f"name={c['company_name']}, "
            f"db={c['db_name']}"
        )


if __name__ == "__main__":
    main()