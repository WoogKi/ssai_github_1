# tools/ssai_hash_password.py
# 관리자 비밀번호를 안전하게 해시하여 SQL에 삽입할 수 있도록 도와주는 스크립트입니다.
# 사용 방법:
# 1. 이 스크립트를 실행합니다.
# 2. 관리자 비밀번호를 입력하고 확인을 위해 다시 입력합니다.   
#    - 비밀번호는 최소 8자 이상이어야 합니다.
# 3. 입력한 비밀번호가 일치하면, 해시된 비밀번호가 출력됩니다.
# 4. 출력된 해시된 비밀번호를 SQL에 붙여 넣어 관리자 계정을 생성하거나 업데이트할 때 사용하세요.
# Created  2026/06/22




from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import secrets


ITERATIONS = 260_000
ALGORITHM = "pbkdf2_sha256"


def hash_password(password: str) -> str:
    salt = secrets.token_urlsafe(24)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        ITERATIONS,
    )
    digest_b64 = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
    return f"{ALGORITHM}${ITERATIONS}${salt}${digest_b64}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt, expected_digest = password_hash.split("$", 3)
        if algorithm != ALGORITHM:
            return False

        iterations = int(iterations_text)
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            iterations,
        )
        digest_b64 = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")

        return hmac.compare_digest(digest_b64, expected_digest)
    except Exception:
        return False


def main() -> None:
    password1 = getpass.getpass("관리자 비밀번호 입력: ")
    password2 = getpass.getpass("관리자 비밀번호 재입력: ")

    if password1 != password2:
        raise RuntimeError("비밀번호가 서로 다릅니다.")

    if len(password1) < 8:
        raise RuntimeError("비밀번호는 최소 8자 이상으로 입력하세요.")

    password_hash = hash_password(password1)

    print("\n아래 password_hash 값을 SQL에 붙여 넣으세요.\n")
    print(password_hash)


if __name__ == "__main__":
    main()