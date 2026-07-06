# tools/ssai_verify_admin_password.py
# SSAI_USERS 테이블에서 관리자 계정의 password_hash를 가져와서 입력한 비밀번호가 일치하는지 검증하는 스크립트입니다.
# .env 파일에서 DB 접속정보를 읽어와서 SSAI_USERS 테이블에 접속합니다.
# 사용법:
#   python ssai_verify_admin_password.py --login-id admin
# .env 파일 예시:
#   SSAI_DB_SERVER=your_db_server
#   SSAI_DB_NAME=your_db_name 
#   SSAI_DB_USER=your_db_user
#   SSAI_DB_PASSWORD=your_db_password
# SSAI_DB_DRIVER=ODBC Driver 18 for SQL Server
# SSAI_USERS 테이블의 password_hash는 pbkdf2_sha256 알고리즘으로 해시된 값입니다.
# 이 스크립트는 관리자 계정의 비밀번호가 올바른지 검증하는 용도로 사용됩니다.
# Create 2026/06/22



from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import os
from pathlib import Path

import pyodbc


ALGORITHM = "pbkdf2_sha256"


def load_dotenv(path: str = ".env") -> dict[str, str]:
    env: dict[str, str] = {}

    p = Path(path)
    if not p.exists():
        return env

    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")

    return env


def get_env(name: str, env: dict[str, str], default: str | None = None) -> str | None:
    return os.environ.get(name) or env.get(name) or default


def pick_env(env: dict[str, str], names: list[str], default: str | None = None) -> str | None:
    for name in names:
        value = get_env(name, env)
        if value:
            return value
    return default


def build_conn_str(
    *,
    driver: str,
    server: str,
    database: str,
    user: str,
    password: str,
) -> str:
    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"UID={user};"
        f"PWD={password};"
        "Encrypt=yes;"
        "TrustServerCertificate=yes;"
    )


def connect_ssai_db(env: dict[str, str]) -> pyodbc.Connection:
    server = pick_env(env, ["SSAI_DB_SERVER", "DB_SERVER", "MSSQL_SERVER", "SERVER"])
    database = pick_env(env, ["SSAI_DB_NAME", "DB_NAME", "MSSQL_DATABASE", "DATABASE"], "SS_AI")
    user = pick_env(env, ["SSAI_DB_USER", "DB_USER", "MSSQL_USER", "UID"])
    password = pick_env(env, ["SSAI_DB_PASSWORD", "DB_PASSWORD", "MSSQL_PASSWORD", "PWD"])
    driver = pick_env(env, ["SSAI_DB_DRIVER", "DB_DRIVER", "MSSQL_DRIVER"], "ODBC Driver 18 for SQL Server")

    missing = []
    if not server:
        missing.append("SSAI_DB_SERVER")
    if not database:
        missing.append("SSAI_DB_NAME")
    if not user:
        missing.append("SSAI_DB_USER")
    if not password:
        missing.append("SSAI_DB_PASSWORD")

    if missing:
        raise RuntimeError(".env에 SS AI DB 접속정보가 부족합니다: " + ", ".join(missing))

    conn_str = build_conn_str(
        driver=driver,
        server=server,
        database=database,
        user=user,
        password=password,
    )

    return pyodbc.connect(conn_str, timeout=10)


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


def get_user_password_hash(conn: pyodbc.Connection, login_id: str) -> dict[str, object]:
    sql = """
    SELECT
        user_id,
        login_id,
        user_name,
        user_type,
        user_grade,
        password_hash,
        approval_status,
        is_active
    FROM dbo.SSAI_USERS
    WHERE login_id = ?
    """

    cur = conn.cursor()
    row = cur.execute(sql, login_id).fetchone()

    if not row:
        raise RuntimeError(f"SSAI_USERS에 login_id={login_id!r} 사용자가 없습니다.")

    columns = [col[0] for col in cur.description]
    return dict(zip(columns, row))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--login-id", default="admin")
    args = parser.parse_args()

    env = load_dotenv(".env")

    with connect_ssai_db(env) as conn:
        user = get_user_password_hash(conn, args.login_id)

    print(f"[INFO] login_id={user['login_id']}")
    print(f"[INFO] user_name={user['user_name']}")
    print(f"[INFO] user_type={user['user_type']}")
    print(f"[INFO] user_grade={user['user_grade']}")
    print(f"[INFO] approval_status={user['approval_status']}")
    print(f"[INFO] is_active={user['is_active']}")

    if not user["is_active"]:
        raise RuntimeError("비활성 사용자입니다.")

    if user["approval_status"] != "APPROVED":
        raise RuntimeError("승인되지 않은 사용자입니다.")

    password_hash = user["password_hash"]
    if not password_hash:
        raise RuntimeError("password_hash가 없습니다.")

    password = getpass.getpass("관리자 비밀번호 입력: ")

    if verify_password(password, str(password_hash)):
        print("[OK] 관리자 비밀번호 검증 성공")
    else:
        print("[FAIL] 관리자 비밀번호 검증 실패")


if __name__ == "__main__":
    main()