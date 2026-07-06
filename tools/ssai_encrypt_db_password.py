#  tools/ssai_encrypt_db_password.py
# 단계 1. cryptography 설치 확인
# 단계 2. Fernet Key 생성
# 단계 3. 암호화 도구 파일 생성
# 단계 4. .env 파일에 SSAI_SECRET_KEY 추가
# 단계 5. 암호화 도구 실행하여 암호화된 비밀번호 생성
# 단계 6. 생성된 SQL을 SSMS에서 실행하여 DB에 암호화된 비밀번호 저장
# 단계 7. SSAI 서비스에서 암호화된 비밀번호를 복호화하여 DB 연결에 사용
# 이 도구는 SSAI 서비스에서 DB 비밀번호를 안전하게 암호화하여 저장하기 위한 것입니다.
# SSAI_SECRET_KEY는 Fernet 대칭 키로, 암호화와 복호화에 사용됩니다. 이 키는 절대 노출되어서는 안 되며, 안전하게 보관해야 합니다.
# 암호화된 비밀번호는 SQL Server Management Studio(SSMS)를 통해 SSAI_COMPANIES 테이블에 저장됩니다. 
# 이 테이블의 db_password_encrypted 컬럼에 암호화된 비밀번호가 저장되며, is_active 플래그가 1로 설정되어 활성화됩니다.
# SSAI 서비스는 이 암호화된 비밀번호를 복호화하여 DB 연결에 사용하므로, SSAI_SECRET_KEY가 변경되면 기존에 저장된 암호화된 비밀번호는 더 이상 유효하지 않게 됩니다. 
# 따라서 SSAI_SECRET_KEY를 변경할 때는 기존 암호화된 비밀번호를 모두 재암호화해야 합니다.
# 이 도구는 Python 3.6 이상에서 실행되어야 하며, cryptography 라이브러리가 설치되어 있어야 합니다.
# 사용 방법:
# 1. Python 3.6 이상이 설치되어 있는지 확인합니다.
# 2. pip install cryptography 명령어로 cryptography 라이브러리를 설치합니다.
# 3. Fernet Key를 생성하려면 Python 인터프리터에서 다음 코드를 실행합니다:
#    from cryptography.fernet import Fernet
#    key = Fernet.generate_key()
#    print(key.decode())
# Create 2026-06-22 by ChatGPT

from __future__ import annotations

import getpass
import os
from pathlib import Path

from cryptography.fernet import Fernet


def load_env_value(key: str, env_path: str = ".env") -> str | None:
    path = Path(env_path)
    if not path.exists():
        return os.environ.get(key)

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        k, v = line.split("=", 1)
        if k.strip() == key:
            return v.strip().strip('"').strip("'")

    return os.environ.get(key)


def sql_escape(value: str) -> str:
    return value.replace("'", "''")


def main() -> None:
    secret_key = load_env_value("SSAI_SECRET_KEY")

    if not secret_key:
        raise RuntimeError(".env에 SSAI_SECRET_KEY가 없습니다.")

    fernet = Fernet(secret_key.encode("utf-8"))

    company_id = input("company_id 입력: ").strip()
    if not company_id.isdigit():
        raise ValueError("company_id는 숫자여야 합니다.")
    
# ========================================================
#    getpass.getpass()는 일부 환경에서 입력이 보이지 않을 수 있으므로, input()으로 대체
    plain_password = getpass.getpass("DB 비밀번호 입력: ")
#    plain_password = input("DB 비밀번호 입력(화면에 보임): ")
    encrypted = fernet.encrypt(plain_password.encode("utf-8")).decode("utf-8")

    print("\n아래 SQL을 SSMS에서 실행하세요.\n")
    print("USE [SS_AI];")
    print("GO")
    print("")
    print("UPDATE dbo.SSAI_COMPANIES")
    print(f"SET db_password_encrypted = N'{sql_escape(encrypted)}',")
    print("    is_active = 1,")
    print("    updated_at = SYSDATETIME()")
    print(f"WHERE company_id = {company_id};")
    print("GO")


if __name__ == "__main__":
    main()