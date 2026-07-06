# tools/ssai_test_company_connection.py
# SS_AI DB 접속
#  → SSAI_COMPANIES에서 company_id 조회
#  → db_password_encrypted 복호화
#  → 해당 ERP DB 접속
#  → Rddbc060 존재 확인
#  → Rddbc060 row count 출력
# 사용법:
# 1) .env 파일에 SSAI_DB_SERVER, SSAI_DB_NAME, SSAI_DB_USER, SSAI_DB_PASSWORD, SSAI_DB_DRIVER(선택) 설정
# 2) SSAI_SECRET_KEY 설정 (암호화된 비밀번호 복호화에 필요)
# 3) python ssai_test_company_connection.py --company-id <회사ID>  
# 참고: SSAI_COMPANIES 테이블의 db_password_encrypted 컬럼은 암호화된 비밀번호를 저장합니다.
# Crate 2026-06-22




from __future__ import annotations

import argparse
import os
from pathlib import Path

import pyodbc
from cryptography.fernet import Fernet


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
    port: int | None = None,
) -> str:
    server_value = server
    if port:
        server_value = f"{server},{port}"

    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={server_value};"
        f"DATABASE={database};"
        f"UID={user};"
        f"PWD={password};"
        "Encrypt=yes;"
        "TrustServerCertificate=yes;"
    )


def connect_admin_db(env: dict[str, str]) -> pyodbc.Connection:
    server = pick_env(env, ["SSAI_DB_SERVER", "DB_SERVER", "MSSQL_SERVER", "SERVER"])
    database = pick_env(env, ["SSAI_DB_NAME", "DB_NAME", "MSSQL_DATABASE", "DATABASE"], "SS_AI")
    user = pick_env(env, ["SSAI_DB_USER", "DB_USER", "MSSQL_USER", "UID"])
    password = pick_env(env, ["SSAI_DB_PASSWORD", "DB_PASSWORD", "MSSQL_PASSWORD", "PWD"])
    driver = pick_env(env, ["SSAI_DB_DRIVER", "DB_DRIVER", "MSSQL_DRIVER"], "ODBC Driver 18 for SQL Server")

    missing = []
    if not server:
        missing.append("SSAI_DB_SERVER 또는 DB_SERVER")
    if not database:
        missing.append("SSAI_DB_NAME 또는 DB_NAME")
    if not user:
        missing.append("SSAI_DB_USER 또는 DB_USER")
    if not password:
        missing.append("SSAI_DB_PASSWORD 또는 DB_PASSWORD")

    if missing:
        raise RuntimeError(".env에 DB 접속정보가 부족합니다: " + ", ".join(missing))

    conn_str = build_conn_str(
        driver=driver,
        server=server,
        database=database,
        user=user,
        password=password,
    )

    return pyodbc.connect(conn_str, timeout=10)


def get_company(admin_conn: pyodbc.Connection, company_id: int) -> dict[str, object]:
    sql = """
    SELECT
        company_id,
        company_code,
        company_name,
        company_type,
        db_server,
        db_port,
        db_name,
        db_user,
        db_password_encrypted,
        db_driver,
        is_active
    FROM dbo.SSAI_COMPANIES
    WHERE company_id = ?
    """

    cur = admin_conn.cursor()
    row = cur.execute(sql, company_id).fetchone()

    if not row:
        raise RuntimeError(f"SSAI_COMPANIES에 company_id={company_id} 자료가 없습니다.")

    columns = [col[0] for col in cur.description]
    return dict(zip(columns, row))


def decrypt_password(secret_key: str, encrypted_password: str) -> str:
    fernet = Fernet(secret_key.encode("utf-8"))
    return fernet.decrypt(encrypted_password.encode("utf-8")).decode("utf-8")


def test_company_db(company: dict[str, object], plain_password: str) -> None:
    company_id = company["company_id"]
    company_code = company["company_code"]
    company_name = company["company_name"]
    db_server = str(company["db_server"])
    db_port = company["db_port"]
    db_name = str(company["db_name"])
    db_user = str(company["db_user"])
    db_driver = str(company["db_driver"] or "ODBC Driver 18 for SQL Server")

    port_value = int(db_port) if db_port is not None else None

    conn_str = build_conn_str(
        driver=db_driver,
        server=db_server,
        port=port_value,
        database=db_name,
        user=db_user,
        password=plain_password,
    )

    print(f"[INFO] 회사 DB 접속 테스트: company_id={company_id}, code={company_code}, name={company_name}")
    print(f"[INFO] DB: server={db_server}, database={db_name}, user={db_user}, driver={db_driver}")

    with pyodbc.connect(conn_str, timeout=10) as conn:
        cur = conn.cursor()

        table_check = cur.execute(
            """
            SELECT COUNT(*)
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_NAME = 'Rddbc060'
            """
        ).fetchone()[0]

        if table_check == 0:
            raise RuntimeError("접속은 성공했지만 Rddbc060 테이블을 찾지 못했습니다.")

        row_count = cur.execute("SELECT COUNT(*) FROM dbo.Rddbc060").fetchone()[0]

    print(f"[OK] ERP DB 접속 성공")
    print(f"[OK] Rddbc060 확인 성공")
    print(f"[OK] Rddbc060 rows = {row_count}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company-id", type=int, required=True)
    args = parser.parse_args()

    env = load_dotenv(".env")

    secret_key = pick_env(env, ["SSAI_SECRET_KEY"])
    if not secret_key:
        raise RuntimeError(".env에 SSAI_SECRET_KEY가 없습니다.")

    with connect_admin_db(env) as admin_conn:
        company = get_company(admin_conn, args.company_id)

    if not company.get("is_active"):
        raise RuntimeError(f"company_id={args.company_id} 회사가 is_active=0 상태입니다.")

    encrypted_password = str(company["db_password_encrypted"])
    if encrypted_password == "PENDING_ENCRYPTION":
        raise RuntimeError(f"company_id={args.company_id} DB 비밀번호가 아직 암호화되지 않았습니다.")

    plain_password = decrypt_password(secret_key, encrypted_password)
    test_company_db(company, plain_password)


if __name__ == "__main__":
    main()