# app/services/ssai_auth_service.py
#
# Create 2026/06/22

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyodbc
from cryptography.fernet import Fernet

from app.utils.env_config import ENV_PATH


ALGORITHM = "pbkdf2_sha256"


@dataclass
class AuthUser:
    user_id: int
    login_id: str
    user_name: str
    nickname: str | None
    phone: str | None
    user_type: str
    user_grade: str
    default_company_id: int | None
    sims_user_id: str | None
    approval_status: str
    is_active: bool


@dataclass
class AuthResult:
    success: bool
    user: AuthUser | None = None
    permissions: list[str] | None = None
    fail_reason: str | None = None


@dataclass
class CompanyDbConfig:
    company_id: int
    company_code: str
    company_name: str
    company_type: str
    db_server: str
    db_port: int | None
    db_name: str
    db_user: str
    db_password: str
    db_driver: str


def load_dotenv(path: str = ".env") -> dict[str, str]:
    env: dict[str, str] = {}
    p = Path(path)
    if not p.is_absolute() and str(path).strip() == ".env":
        p = ENV_PATH

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
    return env.get(name) or os.environ.get(name) or default


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


def connect_ssai_db() -> pyodbc.Connection:
    env = load_dotenv(".env")

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


def _row_to_dict(cur: pyodbc.Cursor, row: Any) -> dict[str, Any]:
    columns = [col[0] for col in cur.description]
    return dict(zip(columns, row))


def get_user_by_login_id(conn: pyodbc.Connection, login_id: str) -> dict[str, Any] | None:
    sql = """
    SELECT
        user_id,
        login_id,
        password_hash,
        user_name,
        nickname,
        phone,
        user_type,
        user_grade,
        default_company_id,
        sims_user_id,
        approval_status,
        is_active
    FROM dbo.SSAI_USERS
    WHERE login_id = ?
    """

    cur = conn.cursor()
    row = cur.execute(sql, login_id).fetchone()

    if not row:
        return None

    return _row_to_dict(cur, row)


def make_auth_user(row: dict[str, Any]) -> AuthUser:
    return AuthUser(
        user_id=int(row["user_id"]),
        login_id=str(row["login_id"]),
        user_name=str(row["user_name"]),
        nickname=row.get("nickname"),
        phone=row.get("phone"),
        user_type=str(row["user_type"]),
        user_grade=str(row["user_grade"]),
        default_company_id=row.get("default_company_id"),
        sims_user_id=row.get("sims_user_id"),
        approval_status=str(row["approval_status"]),
        is_active=bool(row["is_active"]),
    )


def get_user_permissions(
    conn: pyodbc.Connection,
    *,
    user_id: int,
    company_id: int | None = None,
) -> list[str]:
    sql = """
    SELECT DISTINCT
        p.permission_code
    FROM dbo.SSAI_USER_ROLES ur
    JOIN dbo.SSAI_ROLES r
        ON r.role_id = ur.role_id
    JOIN dbo.SSAI_ROLE_PERMISSIONS rp
        ON rp.role_id = r.role_id
    JOIN dbo.SSAI_PERMISSIONS p
        ON p.permission_id = rp.permission_id
    WHERE ur.user_id = ?
      AND ur.is_active = 1
      AND r.is_active = 1
      AND p.is_active = 1
      AND rp.is_allowed = 1
      AND (
            ur.company_id IS NULL
            OR ur.company_id = ?
          )
    ORDER BY p.permission_code
    """

    cur = conn.cursor()
    rows = cur.execute(sql, user_id, company_id).fetchall()
    return [str(row[0]) for row in rows]


def log_login(
    conn: pyodbc.Connection,
    *,
    login_id: str,
    user_id: int | None,
    company_id: int | None,
    success: bool,
    fail_reason: str | None = None,
) -> None:
    sql = """
    INSERT INTO dbo.SSAI_LOGIN_LOG (
        user_id,
        company_id,
        login_id,
        login_result,
        fail_reason,
        login_at
    )
    VALUES (?, ?, ?, ?, ?, SYSDATETIME())
    """

    conn.cursor().execute(
        sql,
        user_id,
        company_id,
        login_id,
        "SUCCESS" if success else "FAIL",
        fail_reason,
    )
    conn.commit()

    # 감사 로그에도 로그인 성공/실패를 남긴다.
    # 주의:
    # - ssai_audit_service가 ssai_auth_service를 참조할 수 있으므로
    #   순환 import 방지를 위해 함수 내부에서 지연 import한다.
    # - 감사 로그 실패가 로그인 자체를 막으면 안 되므로 예외는 삼킨다.
    try:
        from app.services.ssai_audit_service import safe_log_audit_event

        safe_log_audit_event(
            event_type="LOGIN_SUCCESS" if success else "LOGIN_FAIL",
            action_result="SUCCESS" if success else "FAILURE",
            actor_user_id=int(user_id) if user_id is not None else None,
            actor_login_id=str(login_id or "").strip() or None,
            company_id=int(company_id) if company_id is not None else None,
            target_user_id=int(user_id) if user_id is not None else None,
            target_login_id=str(login_id or "").strip() or None,
            target_company_id=int(company_id) if company_id is not None else None,
            message="로그인 성공" if success else "로그인 실패",
            details={
                "login_id": str(login_id or "").strip(),
                "success": bool(success),
                "fail_reason": fail_reason,
                "source": "SSAI_LOGIN_LOG",
            },
        )
    except Exception:
        pass

def update_last_login(conn: pyodbc.Connection, user_id: int) -> None:
    sql = """
    UPDATE dbo.SSAI_USERS
    SET last_login_at = SYSDATETIME(),
        updated_at = SYSDATETIME()
    WHERE user_id = ?
    """
    conn.cursor().execute(sql, user_id)
    conn.commit()


def authenticate_ssart_user(login_id: str, password: str) -> AuthResult:
    with connect_ssai_db() as conn:
        row = get_user_by_login_id(conn, login_id)

        if not row:
            log_login(
                conn,
                login_id=login_id,
                user_id=None,
                company_id=None,
                success=False,
                fail_reason="USER_NOT_FOUND",
            )
            return AuthResult(success=False, fail_reason="USER_NOT_FOUND")

        user = make_auth_user(row)

        if not user.is_active:
            log_login(
                conn,
                login_id=login_id,
                user_id=user.user_id,
                company_id=user.default_company_id,
                success=False,
                fail_reason="INACTIVE_USER",
            )
            return AuthResult(success=False, user=user, fail_reason="INACTIVE_USER")

        if user.approval_status != "APPROVED":
            log_login(
                conn,
                login_id=login_id,
                user_id=user.user_id,
                company_id=user.default_company_id,
                success=False,
                fail_reason="NOT_APPROVED",
            )
            return AuthResult(success=False, user=user, fail_reason="NOT_APPROVED")

        if user.user_type not in ("SSART_ADMIN", "SSART_USER"):
            log_login(
                conn,
                login_id=login_id,
                user_id=user.user_id,
                company_id=user.default_company_id,
                success=False,
                fail_reason="NOT_SSART_USER",
            )
            return AuthResult(success=False, user=user, fail_reason="NOT_SSART_USER")

        password_hash = row.get("password_hash")
        if not password_hash:
            log_login(
                conn,
                login_id=login_id,
                user_id=user.user_id,
                company_id=user.default_company_id,
                success=False,
                fail_reason="NO_PASSWORD_HASH",
            )
            return AuthResult(success=False, user=user, fail_reason="NO_PASSWORD_HASH")

        if not verify_password(password, str(password_hash)):
            log_login(
                conn,
                login_id=login_id,
                user_id=user.user_id,
                company_id=user.default_company_id,
                success=False,
                fail_reason="INVALID_PASSWORD",
            )
            return AuthResult(success=False, user=user, fail_reason="INVALID_PASSWORD")

        permissions = get_user_permissions(
            conn,
            user_id=user.user_id,
            company_id=user.default_company_id,
        )

        update_last_login(conn, user.user_id)

        log_login(
            conn,
            login_id=login_id,
            user_id=user.user_id,
            company_id=user.default_company_id,
            success=True,
        )

        return AuthResult(
            success=True,
            user=user,
            permissions=permissions,
        )

def authenticate_ssai_password(login_id: str, password: str) -> AuthResult:
    """
    SS AI 1단계 로그인 인증.

    정책:
    - 모든 사용자는 먼저 SSAI_USERS.password_hash로 SS AI 비밀번호를 검증한다.
    - SSART 사용자는 1단계 인증 성공 시 최종 로그인 완료.
    - WHOLESALE 사용자는 1단계 인증 성공 후 SIMS 비밀번호 2단계로 넘어간다.
    """
    login_id = str(login_id or "").strip()
    password = str(password or "")

    if not login_id:
        return AuthResult(success=False, fail_reason="EMPTY_LOGIN_ID")

    if not password:
        return AuthResult(success=False, fail_reason="EMPTY_PASSWORD")

    with connect_ssai_db() as conn:
        row = get_user_by_login_id(conn, login_id)

        if not row:
            log_login(
                conn,
                login_id=login_id,
                user_id=None,
                company_id=None,
                success=False,
                fail_reason="USER_NOT_FOUND",
            )
            return AuthResult(success=False, fail_reason="USER_NOT_FOUND")

        user = make_auth_user(row)
        user_type = str(user.user_type or "").strip().upper()

        # 신성아트컴 사용자는 기존 인증 함수를 그대로 사용한다.
        # 여기서 성공하면 최종 로그인 완료까지 처리된다.
        if user_type in ("SSART_ADMIN", "SSART_USER"):
            return authenticate_ssart_user(login_id, password)

        if user_type not in ("WHOLESALE_ADMIN", "WHOLESALE_USER"):
            log_login(
                conn,
                login_id=login_id,
                user_id=user.user_id,
                company_id=user.default_company_id,
                success=False,
                fail_reason="UNKNOWN_USER_TYPE",
            )
            return AuthResult(success=False, user=user, fail_reason="UNKNOWN_USER_TYPE")

        if not user.is_active:
            log_login(
                conn,
                login_id=login_id,
                user_id=user.user_id,
                company_id=user.default_company_id,
                success=False,
                fail_reason="INACTIVE_USER",
            )
            return AuthResult(success=False, user=user, fail_reason="INACTIVE_USER")

        if user.approval_status != "APPROVED":
            log_login(
                conn,
                login_id=login_id,
                user_id=user.user_id,
                company_id=user.default_company_id,
                success=False,
                fail_reason="NOT_APPROVED",
            )
            return AuthResult(success=False, user=user, fail_reason="NOT_APPROVED")

        password_hash = row.get("password_hash")

        if not password_hash:
            log_login(
                conn,
                login_id=login_id,
                user_id=user.user_id,
                company_id=user.default_company_id,
                success=False,
                fail_reason="NO_PASSWORD_HASH",
            )
            return AuthResult(success=False, user=user, fail_reason="NO_PASSWORD_HASH")

        if not verify_password(password, str(password_hash)):
            log_login(
                conn,
                login_id=login_id,
                user_id=user.user_id,
                company_id=user.default_company_id,
                success=False,
                fail_reason="INVALID_SSAI_PASSWORD",
            )
            return AuthResult(success=False, user=user, fail_reason="INVALID_SSAI_PASSWORD")

        # 도매 사용자는 여기서 최종 로그인 완료가 아니다.
        # SIMS Password 2단계로 넘긴다.
        return AuthResult(
            success=True,
            user=user,
            permissions=[],
        )


def authenticate_wholesale_sims_password(login_id: str, sims_password: str) -> AuthResult:
    """
    도매 사용자 2단계 SIMS 비밀번호 인증.

    SS AI Password 1단계 인증은 UI에서 먼저 통과한 상태로 들어온다.
    실제 최종 로그인 성공 로그와 last_login_at 갱신은 authenticate_wholesale_user()에서 처리한다.
    """
    return authenticate_wholesale_user(login_id, sims_password)

def get_active_companies_for_user(user: AuthUser) -> list[dict[str, Any]]:
    """
    로그인 사용자가 선택 가능한 활성 ERP DB 목록을 반환한다.

    정책:
    1. SIMS_DB_ACCESS_ALL 권한이 있으면 모든 활성 ERP DB를 반환한다.
       - SYSTEM_ADMIN
       - SSART_MANAGER
       - 향후 SSART_SUPPORT_ALL 등

    2. SIMS_DB_ACCESS_ALL 권한이 없으면
       SSAI_USER_COMPANIES에 연결된 활성 ERP DB만 반환한다.

    3. 기존 SSART_ADMIN 전체 접근은 호환성 차원에서 유지한다.
    """
    with connect_ssai_db() as conn:
        permissions = get_user_permissions(
            conn,
            user_id=user.user_id,
            company_id=user.default_company_id,
        )

        can_access_all_db = (
            "SIMS_DB_ACCESS_ALL" in permissions
            or user.user_type == "SSART_ADMIN"
        )

        cur = conn.cursor()

        if can_access_all_db:
            sql = """
            SELECT
                company_id,
                company_code,
                customer_code,
                customer_name,
                company_name,
                company_type,
                db_usage_type,
                erp_company_name,
                db_server,
                db_port,
                db_name,
                is_test_company,
                is_active
            FROM dbo.SSAI_COMPANIES
            WHERE is_active = 1
            ORDER BY company_id
            """
            rows = cur.execute(sql).fetchall()

        else:
            sql = """
            SELECT
                c.company_id,
                c.company_code,
                c.customer_code,
                c.customer_name,
                c.company_name,
                c.company_type,
                c.db_usage_type,
                c.erp_company_name,
                c.db_server,
                c.db_port,
                c.db_name,
                c.is_test_company,
                c.is_active
            FROM dbo.SSAI_USER_COMPANIES uc
            JOIN dbo.SSAI_COMPANIES c
                ON c.company_id = uc.company_id
            WHERE uc.user_id = ?
              AND uc.is_active = 1
              AND c.is_active = 1
            ORDER BY
                uc.is_default DESC,
                c.company_id
            """
            rows = cur.execute(sql, user.user_id).fetchall()

        columns = [col[0] for col in cur.description]
        return [dict(zip(columns, row)) for row in rows]
    
def _get_ssai_secret_key() -> str:
    env = load_dotenv(".env")
    secret_key = pick_env(env, ["SSAI_SECRET_KEY"])

    if not secret_key:
        raise RuntimeError(".env에 SSAI_SECRET_KEY가 없습니다.")

    return secret_key


def decrypt_ssai_value(encrypted_value: str) -> str:
    secret_key = _get_ssai_secret_key()
    fernet = Fernet(secret_key.encode("utf-8"))
    return fernet.decrypt(str(encrypted_value).encode("utf-8")).decode("utf-8")


def get_company_db_config(company_id: int) -> CompanyDbConfig:
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

    with connect_ssai_db() as conn:
        cur = conn.cursor()
        row = cur.execute(sql, int(company_id)).fetchone()

        if not row:
            raise RuntimeError(f"SSAI_COMPANIES에 company_id={company_id} 회사가 없습니다.")

        columns = [col[0] for col in cur.description]
        data = dict(zip(columns, row))

    if not bool(data.get("is_active")):
        raise RuntimeError(f"company_id={company_id} 회사가 비활성 상태입니다.")

    encrypted_password = str(data.get("db_password_encrypted") or "")
    if not encrypted_password or encrypted_password == "PENDING_ENCRYPTION":
        raise RuntimeError(f"company_id={company_id} DB 비밀번호가 암호화되지 않았습니다.")

    plain_password = decrypt_ssai_value(encrypted_password)

    return CompanyDbConfig(
        company_id=int(data["company_id"]),
        company_code=str(data["company_code"]),
        company_name=str(data["company_name"]),
        company_type=str(data["company_type"]),
        db_server=str(data["db_server"]),
        db_port=int(data["db_port"]) if data.get("db_port") is not None else None,
        db_name=str(data["db_name"]),
        db_user=str(data["db_user"]),
        db_password=plain_password,
        db_driver=str(data.get("db_driver") or "ODBC Driver 18 for SQL Server"),
    )


def build_company_conn_str(cfg: CompanyDbConfig) -> str:
    server_value = cfg.db_server
    if cfg.db_port:
        server_value = f"{cfg.db_server},{cfg.db_port}"

    return (
        f"DRIVER={{{cfg.db_driver}}};"
        f"SERVER={server_value};"
        f"DATABASE={cfg.db_name};"
        f"UID={cfg.db_user};"
        f"PWD={cfg.db_password};"
        "Encrypt=yes;"
        "TrustServerCertificate=yes;"
    )


def connect_company_db(company_id: int) -> pyodbc.Connection:
    cfg = get_company_db_config(company_id)
    conn_str = build_company_conn_str(cfg)
    return pyodbc.connect(conn_str, timeout=10)

def _get_default_company_id_for_user(
    conn: pyodbc.Connection,
    *,
    user_id: int,
    default_company_id: int | None,
) -> int | None:
    """
    도매 사용자의 기본 회사 ID를 가져온다.

    우선순위:
    1. SSAI_USERS.default_company_id
    2. SSAI_USER_COMPANIES.is_default = 1
    3. 첫 번째 활성 연결 회사
    """
    if default_company_id:
        return int(default_company_id)

    sql = """
    SELECT TOP 1
        uc.company_id
    FROM dbo.SSAI_USER_COMPANIES uc
    JOIN dbo.SSAI_COMPANIES c
        ON c.company_id = uc.company_id
    WHERE uc.user_id = ?
      AND uc.is_active = 1
      AND c.is_active = 1
    ORDER BY
        uc.is_default DESC,
        uc.company_id
    """

    row = conn.cursor().execute(sql, int(user_id)).fetchone()
    if not row:
        return None

    return int(row[0])


def get_sims_user_for_login(
    *,
    company_id: int,
    sims_user_id: str,
) -> dict[str, Any] | None:
    """
    선택 회사 SIMS DB의 Rddbc060에서 사용자 정보를 조회한다.

    sims_user_id는 Rd06_User_ID 또는 Rd06_User_Cd와 비교한다.
    """
    sql = """
    SELECT TOP 1
        RTRIM(LTRIM(Rd06_User_Cd)) AS sims_user_cd,
        RTRIM(LTRIM(Rd06_User_ID)) AS sims_user_id,
        RTRIM(LTRIM(Rd06_User_Nm)) AS sims_user_name,
        RTRIM(LTRIM(Rd06_Password)) AS sims_password,
        RTRIM(LTRIM(Rd06_Del_Flag)) AS sims_del_flag
    FROM dbo.Rddbc060
    WHERE RTRIM(LTRIM(Rd06_User_ID)) = ?
       OR RTRIM(LTRIM(Rd06_User_Cd)) = ?
    """

    with connect_company_db(int(company_id)) as conn:
        cur = conn.cursor()
        row = cur.execute(sql, sims_user_id, sims_user_id).fetchone()

        if not row:
            return None

        columns = [col[0] for col in cur.description]
        return dict(zip(columns, row))


def verify_sims_plain_password(input_password: str, sims_password: str) -> bool:
    """
    SIMS Rddbc060.Rd06_Password 평문 비교.

    기존 SIMS 비밀번호는 varchar(16) 기반이므로 양쪽 공백을 제거한 뒤 비교한다.
    """
    left = str(input_password or "").strip()
    right = str(sims_password or "").strip()

    if not left:
        return False

    return hmac.compare_digest(left, right)


def authenticate_wholesale_user(login_id: str, password: str) -> AuthResult:
    """
    도매 사용자 로그인.

    원칙:
    - SSAI_USERS.password_hash는 사용하지 않는다.
    - SSAI_USERS.sims_user_id로 회사 SIMS DB의 Rddbc060 사용자를 찾는다.
    - 입력 비밀번호는 Rddbc060.Rd06_Password와 비교한다.
    """
    with connect_ssai_db() as conn:
        row = get_user_by_login_id(conn, login_id)

        if not row:
            log_login(
                conn,
                login_id=login_id,
                user_id=None,
                company_id=None,
                success=False,
                fail_reason="USER_NOT_FOUND",
            )
            return AuthResult(success=False, fail_reason="USER_NOT_FOUND")

        user = make_auth_user(row)

        company_id = _get_default_company_id_for_user(
            conn,
            user_id=user.user_id,
            default_company_id=user.default_company_id,
        )

        if not company_id:
            log_login(
                conn,
                login_id=login_id,
                user_id=user.user_id,
                company_id=None,
                success=False,
                fail_reason="NO_COMPANY",
            )
            return AuthResult(success=False, user=user, fail_reason="NO_COMPANY")

        if not user.is_active:
            log_login(
                conn,
                login_id=login_id,
                user_id=user.user_id,
                company_id=company_id,
                success=False,
                fail_reason="INACTIVE_USER",
            )
            return AuthResult(success=False, user=user, fail_reason="INACTIVE_USER")

        if user.approval_status != "APPROVED":
            log_login(
                conn,
                login_id=login_id,
                user_id=user.user_id,
                company_id=company_id,
                success=False,
                fail_reason="NOT_APPROVED",
            )
            return AuthResult(success=False, user=user, fail_reason="NOT_APPROVED")

        if user.user_type not in ("WHOLESALE_ADMIN", "WHOLESALE_USER"):
            log_login(
                conn,
                login_id=login_id,
                user_id=user.user_id,
                company_id=company_id,
                success=False,
                fail_reason="NOT_WHOLESALE_USER",
            )
            return AuthResult(success=False, user=user, fail_reason="NOT_WHOLESALE_USER")

        sims_user_id = str(user.sims_user_id or "").strip()
        if not sims_user_id:
            log_login(
                conn,
                login_id=login_id,
                user_id=user.user_id,
                company_id=company_id,
                success=False,
                fail_reason="NO_SIMS_USER_ID",
            )
            return AuthResult(success=False, user=user, fail_reason="NO_SIMS_USER_ID")

        sims_user = get_sims_user_for_login(
            company_id=company_id,
            sims_user_id=sims_user_id,
        )

        if not sims_user:
            log_login(
                conn,
                login_id=login_id,
                user_id=user.user_id,
                company_id=company_id,
                success=False,
                fail_reason="SIMS_USER_NOT_FOUND",
            )
            return AuthResult(success=False, user=user, fail_reason="SIMS_USER_NOT_FOUND")

        sims_del_flag = str(sims_user.get("sims_del_flag") or "").strip().upper()
        if sims_del_flag == "E":
            log_login(
                conn,
                login_id=login_id,
                user_id=user.user_id,
                company_id=company_id,
                success=False,
                fail_reason="SIMS_USER_DISABLED",
            )
            return AuthResult(success=False, user=user, fail_reason="SIMS_USER_DISABLED")

        sims_password = str(sims_user.get("sims_password") or "")

        if not verify_sims_plain_password(password, sims_password):
            log_login(
                conn,
                login_id=login_id,
                user_id=user.user_id,
                company_id=company_id,
                success=False,
                fail_reason="INVALID_SIMS_PASSWORD",
            )
            return AuthResult(success=False, user=user, fail_reason="INVALID_SIMS_PASSWORD")

        permissions = get_user_permissions(
            conn,
            user_id=user.user_id,
            company_id=company_id,
        )

        update_last_login(conn, user.user_id)

        log_login(
            conn,
            login_id=login_id,
            user_id=user.user_id,
            company_id=company_id,
            success=True,
        )

        # 도매 사용자는 default_company_id가 실제 로그인 회사로 확정되어야 한다.
        user.default_company_id = company_id

        return AuthResult(
            success=True,
            user=user,
            permissions=permissions,
        )


def authenticate_user(login_id: str, password: str) -> AuthResult:
    """
    사용자 종류별 통합 로그인 함수.

    - SSART_ADMIN / SSART_USER: SSAI_USERS.password_hash 검증
    - WHOLESALE_ADMIN / WHOLESALE_USER: 회사 SIMS DB Rddbc060.Rd06_Password 검증
    """
    with connect_ssai_db() as conn:
        row = get_user_by_login_id(conn, login_id)

    if not row:
        # 기존 실패 로그 처리를 위해 SSART 함수로 위임하지 않고 직접 기록
        with connect_ssai_db() as conn:
            log_login(
                conn,
                login_id=login_id,
                user_id=None,
                company_id=None,
                success=False,
                fail_reason="USER_NOT_FOUND",
            )
        return AuthResult(success=False, fail_reason="USER_NOT_FOUND")

    user_type = str(row.get("user_type") or "").strip()

    if user_type in ("SSART_ADMIN", "SSART_USER"):
        return authenticate_ssart_user(login_id, password)

    if user_type in ("WHOLESALE_ADMIN", "WHOLESALE_USER"):
        return authenticate_wholesale_user(login_id, password)

    with connect_ssai_db() as conn:
        log_login(
            conn,
            login_id=login_id,
            user_id=row.get("user_id"),
            company_id=row.get("default_company_id"),
            success=False,
            fail_reason="UNKNOWN_USER_TYPE",
        )

    return AuthResult(success=False, fail_reason="UNKNOWN_USER_TYPE")
