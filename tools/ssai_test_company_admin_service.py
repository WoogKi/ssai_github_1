# tools/ssai_test_company_admin_service.py
#
# SS AI Phase 3
# ERP 회사 DB 등록/수정/접속 테스트 도구
#
# 최종본
# - --db-port 지원
# - 저장 회사 접속 테스트
# - 입력값 접속 테스트
# - 회사 등록/수정
# - 회사 활성/비활성

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.ssai_company_admin_service import (  # noqa: E402
    list_companies,
    set_company_active,
    test_company_connection,
    test_saved_company_connection,
    upsert_company,
)


def print_json(title: str, value) -> None:
    print()
    print(f"========== {title} ==========")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def to_bool(text: str, default: bool = False) -> bool:
    value = str(text or "").strip().lower()

    if not value:
        return default

    return value in ("1", "true", "y", "yes", "on")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SS AI ERP 회사 DB 등록/수정/접속 테스트 도구"
    )

    parser.add_argument("--list", action="store_true", help="회사 목록 조회")
    parser.add_argument("--test-saved", default="", help="저장된 회사 코드로 접속 테스트")
    parser.add_argument("--test-input", action="store_true", help="입력값으로 ERP DB 접속 테스트")
    parser.add_argument("--save", action="store_true", help="ERP 회사 DB 등록/수정")
    parser.add_argument("--activate", default="", help="회사 활성화 company_code")
    parser.add_argument("--deactivate", default="", help="회사 비활성화 company_code")

    parser.add_argument("--company-code", default="")
    parser.add_argument("--company-name", default="")
    parser.add_argument("--company-type", default="TEST", help="SSART / WHOLESALE / TEST")

    parser.add_argument("--db-server", default="")
    parser.add_argument("--db-port", default="")
    parser.add_argument("--db-name", default="")
    parser.add_argument("--db-user", default="")
    parser.add_argument("--db-driver", default="ODBC Driver 18 for SQL Server")

    parser.add_argument(
        "--trust-server-certificate",
        default="yes",
        help="yes/no, 기본 yes",
    )

    parser.add_argument("--test-company", action="store_true")
    parser.add_argument("--inactive", action="store_true")
    parser.add_argument("--skip-connection-test", action="store_true")

    args = parser.parse_args()

    if args.list:
        print_json("회사 목록", list_companies(include_inactive=True))
        return

    if args.activate:
        print_json(
            "회사 활성화",
            set_company_active(company_code=args.activate, is_active=True),
        )
        return

    if args.deactivate:
        print_json(
            "회사 비활성화",
            set_company_active(company_code=args.deactivate, is_active=False),
        )
        return

    if args.test_saved:
        print_json(
            "등록된 회사 DB 접속 테스트",
            test_saved_company_connection(args.test_saved),
        )
        return

    if args.test_input or args.save:
        if not args.db_server:
            raise SystemExit("--db-server가 필요합니다.")

        if not args.db_name:
            raise SystemExit("--db-name이 필요합니다.")

        if not args.db_user:
            raise SystemExit("--db-user가 필요합니다.")

        db_password = getpass.getpass("ERP DB 비밀번호 입력: ")

        trust = to_bool(args.trust_server_certificate, default=True)

        if args.test_input:
            result = test_company_connection(
                db_server=args.db_server,
                db_port=args.db_port,
                db_name=args.db_name,
                db_user=args.db_user,
                db_password=db_password,
                db_driver=args.db_driver,
                trust_server_certificate=trust,
            )
            print_json("입력값 ERP DB 접속 테스트", result)

            if not args.save:
                return

        if args.save:
            if not args.company_code:
                raise SystemExit("--company-code가 필요합니다.")

            if not args.company_name:
                raise SystemExit("--company-name이 필요합니다.")

            result = upsert_company(
                company_code=args.company_code,
                company_name=args.company_name,
                company_type=args.company_type,
                db_server=args.db_server,
                db_port=args.db_port,
                db_name=args.db_name,
                db_user=args.db_user,
                db_password=db_password,
                db_driver=args.db_driver,
                trust_server_certificate=trust,
                is_test_company=args.test_company,
                is_active=not args.inactive,
                require_connection_test=not args.skip_connection_test,
            )

            print_json("ERP 회사 DB 등록/수정", result)
            return

    parser.print_help()


if __name__ == "__main__":
    main()