# app/services/ssai_company_admin_service.py
#
# SS AI Phase 3
# 회원사 ERP DB 등록 / 수정 / 접속 테스트 서비스
#
# 최종 정리본
# - SSAI_COMPANIES.db_port 반영
# - trust_server_certificate 컬럼이 없어도 동작
# - db_password_encrypted 컬럼 자동 탐색
# - DB 비밀번호는 Fernet 암호화 저장
# - 저장 전/저장 후 Rddbc060 접속 테스트 지원

from __future__ import annotations

import os
from typing import Any

import pyodbc
from cryptography.fernet import Fernet
from dotenv import load_dotenv

from app.services.ssai_auth_service import connect_ssai_db

load_dotenv()


VALID_COMPANY_TYPES = {"SSART", "WHOLESALE", "TEST"}
VALID_DB_USAGE_TYPES = {"MAIN", "TEST", "BACKUP", "ANALYSIS", "TRAINING", "ETC"}


def _normalize_company_type(company_type: str) -> str:
    value = str(company_type or "TEST").strip().upper()

    if value not in VALID_COMPANY_TYPES:
        raise ValueError("company_type은 SSART, WHOLESALE, TEST 중 하나여야 합니다.")

    return value


def _normalize_db_usage_type(db_usage_type: str) -> str:
    value = str(db_usage_type or "MAIN").strip().upper()

    if value not in VALID_DB_USAGE_TYPES:
        raise ValueError(
            "db_usage_type은 MAIN, TEST, BACKUP, ANALYSIS, TRAINING, ETC 중 하나여야 합니다."
        )

    return value


def _derive_customer_code(*, customer_code: str, company_code: str) -> str:
    value = str(customer_code or "").strip()
    return value if value else str(company_code or "").strip()


def _derive_customer_name(
    *,
    customer_name: str,
    erp_company_name: str,
    company_name: str,
) -> str:
    value = str(customer_name or "").strip()
    if value:
        return value

    value = str(erp_company_name or "").strip()
    if value:
        return value

    return str(company_name or "").strip()


# =========================================================
# 공통 DB helper
# =========================================================
def _row_to_dict(cursor: pyodbc.Cursor, row) -> dict[str, Any] | None:
    if not row:
        return None

    columns = [col[0] for col in cursor.description]
    return dict(zip(columns, row))


def _fetch_one_dict(
    conn: pyodbc.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> dict[str, Any] | None:
    cur = conn.cursor()
    row = cur.execute(sql, *params).fetchone()
    return _row_to_dict(cur, row)


def _fetch_all_dicts(
    conn: pyodbc.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    cur = conn.cursor()
    rows = cur.execute(sql, *params).fetchall()
    columns = [col[0] for col in cur.description]
    return [dict(zip(columns, row)) for row in rows]


def _get_table_columns(conn: pyodbc.Connection, table_name: str) -> set[str]:
    rows = _fetch_all_dicts(
        conn,
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = N'dbo'
          AND TABLE_NAME = ?
        """,
        (table_name,),
    )

    return {str(r["COLUMN_NAME"]) for r in rows}


def _get_company_password_column(columns: set[str]) -> str:
    """
    SSAI_COMPANIES의 DB 비밀번호 암호화 컬럼 탐색.
    현재 설계 기준은 db_password_encrypted.
    """
    candidates = [
        "db_password_encrypted",
        "db_password_enc",
        "db_password_cipher",
        "erp_db_password_encrypted",
    ]

    for col in candidates:
        if col in columns:
            return col

    raise RuntimeError(
        "SSAI_COMPANIES에서 DB 비밀번호 암호화 컬럼을 찾지 못했습니다. "
        "예상 컬럼: db_password_encrypted"
    )


def _bool_to_bit(value: bool) -> int:
    return 1 if bool(value) else 0


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return bool(value)

    text = str(value).strip().lower()
    if text in ("1", "true", "y", "yes", "on"):
        return True

    if text in ("0", "false", "n", "no", "off"):
        return False

    return default


def _normalize_db_port(db_port: str | int | None) -> int | None:
    if db_port is None:
        return None

    text = str(db_port).strip()

    if not text:
        return None

    if not text.isdigit():
        raise ValueError(f"db_port는 숫자여야 합니다. db_port={db_port}")

    return int(text)


def _sanitize_company(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """
    화면/로그 반환 시 password 포함 컬럼 제거.
    """
    if not row:
        return None

    copied = dict(row)

    for key in list(copied.keys()):
        if "password" in key.lower():
            copied.pop(key, None)

    return copied


# =========================================================
# 암호화 helper
# =========================================================
def _get_secret_key() -> str:
    key = os.getenv("SSAI_SECRET_KEY", "").strip()

    if not key:
        raise RuntimeError("환경변수 SSAI_SECRET_KEY가 없습니다.")

    return key


def encrypt_value(plain_text: str) -> str:
    plain_text = str(plain_text or "")

    if not plain_text:
        return ""

    f = Fernet(_get_secret_key().encode("utf-8"))
    return f.encrypt(plain_text.encode("utf-8")).decode("utf-8")


def decrypt_value(cipher_text: str) -> str:
    cipher_text = str(cipher_text or "")

    if not cipher_text:
        return ""

    f = Fernet(_get_secret_key().encode("utf-8"))
    return f.decrypt(cipher_text.encode("utf-8")).decode("utf-8")


# =========================================================
# ERP DB 접속 문자열 / 접속 테스트
# =========================================================
def _normalize_db_server_for_odbc(
    db_server: str,
    db_port: str | int | None = None,
) -> str:
    """
    SQL Server ODBC용 SERVER 값 보정.

    우선순위:
    1. db_server가 이미 tcp: 로 시작하면 그대로 사용
    2. db_server에 이미 콤마 포트가 있으면 tcp:만 붙임
    3. db_server가 host:port 형식이면 host,port로 변환
    4. db_port 컬럼값이 있으면 db_server,db_port 사용
    5. 포트 정보가 전혀 없으면 tcp:db_server 사용

    주의:
    - SQL Server ODBC 포트 구분자는 ':'가 아니라 ',' 이다.
    - 여기서는 1433을 강제로 붙이지 않는다.
      테이블의 db_port 또는 db_server에 저장된 포트를 우선 사용한다.
    """
    server = str(db_server or "").strip()
    port = str(db_port or "").strip()

    if not server:
        return server

    if server.lower().startswith("tcp:"):
        return server

    # host:port 로 잘못 들어온 경우 host,port 로 보정
    if "," not in server and ":" in server:
        host, maybe_port = server.rsplit(":", 1)
        if maybe_port.isdigit():
            server = f"{host},{maybe_port}"

    # server에 포트가 없고 instance명도 아니며 db_port가 있으면 붙인다.
    if "," not in server and "\\" not in server and port:
        server = f"{server},{port}"

    return f"tcp:{server}"


def build_company_connection_string(
    *,
    db_server: str,
    db_name: str,
    db_user: str,
    db_password: str,
    db_driver: str = "ODBC Driver 18 for SQL Server",
    db_port: str | int | None = None,
    trust_server_certificate: bool = True,
) -> str:
    trust_value = "yes" if trust_server_certificate else "no"

    server_for_odbc = _normalize_db_server_for_odbc(
        db_server=db_server,
        db_port=db_port,
    )

    return (
        f"DRIVER={{{db_driver}}};"
        f"SERVER={server_for_odbc};"
        f"DATABASE={db_name};"
        f"UID={db_user};"
        f"PWD={db_password};"
        f"Encrypt=yes;"
        f"TrustServerCertificate={trust_value};"
    )

def _make_erp_display_name(ven_nm: str | None) -> str:
    name = str(ven_nm or "").strip()

    if not name:
        return ""

    if name.upper().endswith("ERP DB"):
        return name

    return f"{name} ERP DB"


def _fetch_erp_company_name(conn) -> dict[str, Any]:
    """
    ERP DB 내부 회사명 조회.
    실패해도 접속 테스트 자체는 실패시키지 않는다.
    """
    sql = """
    SELECT TOP 1
           LTRIM(RTRIM(RD03_VEN_PRT)) AS VEN_NM
    FROM RDDBC030 WITH (NOLOCK)
    WHERE RD03_VEN_CD IN (
        SELECT Rd0A_Value
        FROM RDDBC0A0 WITH (NOLOCK)
        WHERE RD0A_GCODE = '0003'
          AND RD0A_TCODE = '000001'
    )
      AND ISNULL(LTRIM(RTRIM(RD03_VEN_PRT)), '') <> ''
    """

    try:
        cur = conn.cursor()
        row = cur.execute(sql).fetchone()

        if not row:
            return {
                "ok": False,
                "erp_company_name": "",
                "suggested_company_name": "",
                "message": "ERP DB 회사명 조회 결과 없음",
            }

        ven_nm = str(row[0] or "").strip()
        suggested_name = _make_erp_display_name(ven_nm)

        return {
            "ok": True,
            "erp_company_name": ven_nm,
            "suggested_company_name": suggested_name,
            "message": "ERP DB 회사명 조회 성공",
        }

    except Exception as e:
        return {
            "ok": False,
            "erp_company_name": "",
            "suggested_company_name": "",
            "message": f"ERP DB 회사명 조회 실패: {type(e).__name__}: {e}",
        }

def _fetch_sims_user_for_login(conn, sims_user_id: str) -> dict[str, Any] | None:
    """
    ERP DB Rddbc060에서 SIMS 사용자 정보를 조회한다.

    sims_user_id는 Rd06_User_ID 또는 Rd06_User_Cd와 비교한다.
    """
    sims_user_id = str(sims_user_id or "").strip()

    if not sims_user_id:
        return None

    sql = """
    SELECT TOP 1
        RTRIM(LTRIM(Rd06_User_Cd)) AS sims_user_cd,
        RTRIM(LTRIM(Rd06_User_ID)) AS sims_user_id,
        RTRIM(LTRIM(Rd06_User_Nm)) AS sims_user_name,
        RTRIM(LTRIM(Rd06_Password)) AS sims_password,
        RTRIM(LTRIM(Rd06_Del_Flag)) AS sims_del_flag
    FROM dbo.Rddbc060 WITH (NOLOCK)
    WHERE RTRIM(LTRIM(Rd06_User_ID)) = ?
       OR RTRIM(LTRIM(Rd06_User_Cd)) = ?
    ORDER BY Rd06_User_ID
    """

    try:
        return _fetch_one_dict(conn, sql, (sims_user_id, sims_user_id))
    except Exception:
        return None


def _verify_sims_plain_password(input_password: str, sims_password: str) -> bool:
    return str(input_password or "").strip() == str(sims_password or "").strip()


def test_sims_admin_auth(
    *,
    db_server: str,
    db_name: str,
    db_user: str,
    db_password: str,
    sims_admin_password: str,
    db_driver: str = "ODBC Driver 18 for SQL Server",
    db_port: str | int | None = None,
    trust_server_certificate: bool = True,
    timeout: int = 5,
) -> dict[str, Any]:
    """
    입력받은 ERP DB 접속정보로 SIMS admin 인증을 확인한다.

    원칙:
    - SIMS ID는 admin으로 고정한다.
    - SIMS admin 비밀번호는 저장하지 않는다.
    - 성공 시 ERP 내부 회사명과 admin 사용자명을 반환한다.
    """
    sims_admin_password = str(sims_admin_password or "")

    if not sims_admin_password:
        return {
            "ok": False,
            "message": "SIMS admin 비밀번호가 필요합니다.",
        }

    conn_str = build_company_connection_string(
        db_server=db_server,
        db_name=db_name,
        db_user=db_user,
        db_password=db_password,
        db_driver=db_driver,
        db_port=db_port,
        trust_server_certificate=trust_server_certificate,
    )

    server_for_odbc = _normalize_db_server_for_odbc(
        db_server=db_server,
        db_port=db_port,
    )

    try:
        conn = pyodbc.connect(conn_str, timeout=int(timeout), autocommit=True)

        try:
            db_row = _fetch_one_dict(conn, "SELECT DB_NAME() AS current_db")
            current_db = db_row.get("current_db") if db_row else None

            sims_user = _fetch_sims_user_for_login(conn, "admin")

            if not sims_user:
                return {
                    "ok": False,
                    "server_for_odbc": server_for_odbc,
                    "current_db": current_db,
                    "message": "Rddbc060에서 SIMS admin 사용자를 찾지 못했습니다.",
                }

            sims_del_flag = str(sims_user.get("sims_del_flag") or "").strip().upper()
            if sims_del_flag == "E":
                return {
                    "ok": False,
                    "server_for_odbc": server_for_odbc,
                    "current_db": current_db,
                    "sims_admin_user_name": sims_user.get("sims_user_name"),
                    "message": "SIMS admin 사용자가 삭제/비활성 상태입니다.",
                }

            if not _verify_sims_plain_password(
                sims_admin_password,
                str(sims_user.get("sims_password") or ""),
            ):
                return {
                    "ok": False,
                    "server_for_odbc": server_for_odbc,
                    "current_db": current_db,
                    "sims_admin_user_name": sims_user.get("sims_user_name"),
                    "message": "SIMS admin 비밀번호가 일치하지 않습니다.",
                }

            company_name_info = _fetch_erp_company_name(conn)

            return {
                "ok": True,
                "server_for_odbc": server_for_odbc,
                "current_db": current_db,
                "sims_admin_user_cd": sims_user.get("sims_user_cd"),
                "sims_admin_user_id": sims_user.get("sims_user_id"),
                "sims_admin_user_name": sims_user.get("sims_user_name"),
                "erp_company_name": company_name_info.get("erp_company_name", ""),
                "suggested_company_name": company_name_info.get("suggested_company_name", ""),
                "erp_company_name_message": company_name_info.get("message", ""),
                "message": "SIMS admin 인증 성공",
            }

        finally:
            conn.close()

    except Exception as e:
        return {
            "ok": False,
            "server_for_odbc": server_for_odbc,
            "error_type": type(e).__name__,
            "error": str(e),
            "message": "SIMS admin 인증 중 DB 접속 실패",
        }


def test_company_connection(
    *,
    db_server: str,
    db_name: str,
    db_user: str,
    db_password: str,
    db_driver: str = "ODBC Driver 18 for SQL Server",
    db_port: str | int | None = None,
    trust_server_certificate: bool = True,
    timeout: int = 5,
) -> dict[str, Any]:
    """
    입력받은 ERP DB 접속정보로 연결 테스트.

    확인 항목:
    - SQL Server 접속
    - 실제 DB_NAME()
    - dbo.Rddbc060 존재 여부
    - Rddbc060 건수
    """
    conn_str = build_company_connection_string(
        db_server=db_server,
        db_name=db_name,
        db_user=db_user,
        db_password=db_password,
        db_driver=db_driver,
        db_port=db_port,
        trust_server_certificate=trust_server_certificate,
    )

    server_for_odbc = _normalize_db_server_for_odbc(
        db_server=db_server,
        db_port=db_port,
    )

    try:
        conn = pyodbc.connect(conn_str, timeout=int(timeout), autocommit=True)

        try:
            db_row = _fetch_one_dict(conn, "SELECT DB_NAME() AS current_db")
            current_db = db_row.get("current_db") if db_row else None

            exists_row = _fetch_one_dict(
                conn,
                """
                SELECT
                    CASE
                        WHEN OBJECT_ID(N'dbo.Rddbc060', N'U') IS NULL THEN 0
                        ELSE 1
                    END AS exists_rddbc060
                """,
            )

            exists_rddbc060 = bool(
                exists_row and int(exists_row["exists_rddbc060"]) == 1
            )

            rddbc060_count = None

            if exists_rddbc060:
                count_row = _fetch_one_dict(
                    conn,
                    "SELECT COUNT(*) AS row_count FROM dbo.Rddbc060",
                )
                rddbc060_count = int(count_row["row_count"] or 0) if count_row else 0

            company_name_info = _fetch_erp_company_name(conn)

            return {
                "ok": True,
                "server_for_odbc": server_for_odbc,
                "current_db": current_db,
                "exists_rddbc060": exists_rddbc060,
                "rddbc060_count": rddbc060_count,
                "erp_company_name": company_name_info.get("erp_company_name", ""),
                "suggested_company_name": company_name_info.get("suggested_company_name", ""),
                "erp_company_name_message": company_name_info.get("message", ""),
                "message": "ERP DB 접속 테스트 성공",
            }

        finally:
            conn.close()

    except Exception as e:
        return {
            "ok": False,
            "server_for_odbc": server_for_odbc,
            "error_type": type(e).__name__,
            "error": str(e),
            "message": "ERP DB 접속 테스트 실패",
        }


# =========================================================
# 회사 목록 / 조회
# =========================================================
def list_companies(
    *,
    include_inactive: bool = True,
    top: int = 500,
) -> list[dict[str, Any]]:
    top = max(1, min(int(top or 500), 2000))

    with connect_ssai_db() as conn:
        columns = _get_table_columns(conn, "SSAI_COMPANIES")

        select_cols = [
            "company_id",
            "company_code",
            "customer_code",
            "customer_name",
            "company_name",
            "company_type",
            "db_usage_type",
            "erp_company_name",
            "db_server",
            "db_port",
            "db_name",
            "db_user",
            "db_driver",
            "trust_server_certificate",
            "is_test_company",
            "is_active",
            "created_at",
            "updated_at",
        ]

        existing_select_cols = [c for c in select_cols if c in columns]

        where_sql = ""
        if not include_inactive and "is_active" in columns:
            where_sql = "WHERE is_active = 1"

        order_col = "company_id" if "company_id" in columns else "company_code"

        sql = f"""
        SELECT TOP {top}
            {", ".join(existing_select_cols)}
        FROM dbo.SSAI_COMPANIES
        {where_sql}
        ORDER BY {order_col}
        """

        rows = _fetch_all_dicts(conn, sql)

    return [_sanitize_company(r) for r in rows]


def get_company_by_code(
    company_code: str,
    *,
    include_secret: bool = False,
) -> dict[str, Any] | None:
    company_code = str(company_code or "").strip()

    if not company_code:
        return None

    with connect_ssai_db() as conn:
        columns = _get_table_columns(conn, "SSAI_COMPANIES")

        select_cols = [
            "company_id",
            "company_code",
            "customer_code",
            "customer_name",
            "company_name",
            "company_type",
            "db_usage_type",
            "erp_company_name",
            "db_server",
            "db_port",
            "db_name",
            "db_user",
            "db_driver",
            "trust_server_certificate",
            "is_test_company",
            "is_active",
            "created_at",
            "updated_at",
        ]

        if include_secret:
            password_col = _get_company_password_column(columns)
            select_cols.append(f"{password_col} AS db_password_encrypted")

        existing_select_cols = []

        for col in select_cols:
            # alias가 있는 password 컬럼은 그대로 둔다.
            if " AS " in col:
                existing_select_cols.append(col)
            elif col in columns:
                existing_select_cols.append(col)

        row = _fetch_one_dict(
            conn,
            f"""
            SELECT TOP 1
                {", ".join(existing_select_cols)}
            FROM dbo.SSAI_COMPANIES
            WHERE company_code = ?
            """,
            (company_code,),
        )

    if include_secret:
        return row

    return _sanitize_company(row)


# =========================================================
# 저장된 회사 DB 접속 테스트
# =========================================================
def test_saved_company_connection(company_code: str) -> dict[str, Any]:
    """
    SSAI_COMPANIES에 저장된 회사 DB 접속정보로 재접속 테스트.
    """
    company_code = str(company_code or "").strip()

    with connect_ssai_db() as conn:
        columns = _get_table_columns(conn, "SSAI_COMPANIES")
        password_col = _get_company_password_column(columns)

        select_cols = [
            "company_code",
            "customer_code",
            "customer_name",
            "company_name",
            "company_type",
            "db_usage_type",
            "erp_company_name",
            "db_server",
            "db_port",
            "db_name",
            "db_user",
            f"{password_col} AS db_password_encrypted",
            "db_driver",
            "trust_server_certificate",
            "is_active",
        ]

        existing_select_cols = []

        for col in select_cols:
            if " AS " in col:
                existing_select_cols.append(col)
            elif col in columns:
                existing_select_cols.append(col)

        row = _fetch_one_dict(
            conn,
            f"""
            SELECT TOP 1
                {", ".join(existing_select_cols)}
            FROM dbo.SSAI_COMPANIES
            WHERE company_code = ?
            """,
            (company_code,),
        )

    if not row:
        return {
            "ok": False,
            "message": f"등록된 회원사 ERP DB를 찾지 못했습니다. company_code={company_code}",
        }

    db_password = decrypt_value(str(row.get("db_password_encrypted") or ""))

    trust_server_certificate = True
    if "trust_server_certificate" in row:
        trust_server_certificate = _to_bool(row.get("trust_server_certificate"), True)

    result = test_company_connection(
        db_server=str(row.get("db_server") or ""),
        db_port=row.get("db_port"),
        db_name=str(row.get("db_name") or ""),
        db_user=str(row.get("db_user") or ""),
        db_password=db_password,
        db_driver=str(row.get("db_driver") or "ODBC Driver 18 for SQL Server"),
        trust_server_certificate=trust_server_certificate,
    )

    result["company_code"] = row.get("company_code")
    result["customer_code"] = row.get("customer_code")
    result["customer_name"] = row.get("customer_name")
    result["company_name"] = row.get("company_name")
    result["company_type"] = row.get("company_type")
    result["db_usage_type"] = row.get("db_usage_type")
    result["saved_erp_company_name"] = row.get("erp_company_name")
    result["db_name"] = row.get("db_name")
    result["db_server"] = row.get("db_server")
    result["db_port"] = row.get("db_port")

    return result


# =========================================================
# 회원사 ERP DB 등록 / 수정
# =========================================================
def upsert_company(
    *,
    company_code: str,
    company_name: str,
    company_type: str,
    db_server: str,
    customer_code: str = "",
    customer_name: str = "",
    db_usage_type: str = "MAIN",
    erp_company_name: str = "",
    db_name: str,
    db_user: str,
    db_password: str = "",
    db_driver: str = "ODBC Driver 18 for SQL Server",
    db_port: str | int | None = None,
    trust_server_certificate: bool = True,
    is_test_company: bool = False,
    is_active: bool = True,
    require_connection_test: bool = True,
) -> dict[str, Any]:
    """
    회원사 ERP DB 등록/수정.

    원칙:
    - db_password가 입력되면 암호화 저장
    - db_password가 비어 있고 기존 회원사 ERP DB가 있으면 기존 암호 유지
    - 신규 회사는 db_password 필수
    - 저장 전 접속 테스트 가능
    """
    company_code = str(company_code or "").strip()
    company_name = str(company_name or "").strip()
    company_type = _normalize_company_type(company_type)
    db_usage_type = _normalize_db_usage_type(db_usage_type)
    db_server = str(db_server or "").strip()
    db_name = str(db_name or "").strip()
    db_user = str(db_user or "").strip()
    db_password = str(db_password or "")
    db_driver = str(db_driver or "ODBC Driver 18 for SQL Server").strip()
    normalized_db_port = _normalize_db_port(db_port)

    if not company_code:
        raise ValueError("ERP DB 코드(company_code)가 필요합니다.")

    if not db_server:
        raise ValueError("db_server가 필요합니다.")

    if not db_name:
        raise ValueError("db_name이 필요합니다.")

    if not db_user:
        raise ValueError("db_user가 필요합니다.")

    with connect_ssai_db() as conn:
        columns = _get_table_columns(conn, "SSAI_COMPANIES")
        password_col = _get_company_password_column(columns)

        existing = _fetch_one_dict(
            conn,
            """
            SELECT TOP 1
                company_id,
                company_code
            FROM dbo.SSAI_COMPANIES
            WHERE company_code = ?
            """,
            (company_code,),
        )

        if require_connection_test:
            if db_password:
                db_password_for_test = db_password
            else:
                if not existing:
                    raise ValueError("신규 회원사 ERP DB 등록 시 DB 비밀번호가 필요합니다.")

                existing_secret = get_company_by_code(company_code, include_secret=True)
                if not existing_secret:
                    raise ValueError("기존 회원사 ERP DB의 암호화 비밀번호를 찾지 못했습니다.")

                db_password_for_test = decrypt_value(
                    str(existing_secret.get("db_password_encrypted") or "")
                )

            test_result = test_company_connection(
                db_server=db_server,
                db_port=normalized_db_port,
                db_name=db_name,
                db_user=db_user,
                db_password=db_password_for_test,
                db_driver=db_driver,
                trust_server_certificate=trust_server_certificate,
            )

            if not test_result.get("ok"):
                raise ConnectionError(
                    "ERP DB 접속 테스트 실패: "
                    f"{test_result.get('error_type')}: {test_result.get('error')}"
                )

            if not erp_company_name:
                erp_company_name = str(test_result.get("erp_company_name") or "").strip()

            if not company_name:
                company_name = str(test_result.get("suggested_company_name") or "").strip()

        customer_code = _derive_customer_code(
            customer_code=customer_code,
            company_code=company_code,
        )
        customer_name = _derive_customer_name(
            customer_name=customer_name,
            erp_company_name=erp_company_name,
            company_name=company_name,
        )

        if not company_name:
            company_name = customer_name

        if not customer_code:
            raise ValueError("회원사코드(customer_code)가 필요합니다.")

        if not customer_name:
            raise ValueError("회원사명(customer_name)이 필요합니다.")

        if not company_name:
            raise ValueError("ERP DB 표시명(company_name)이 필요합니다.")

        encrypted_password = encrypt_value(db_password) if db_password else ""

        cur = conn.cursor()

        if existing:
            sets: list[str] = []
            params: list[Any] = []

            def add_set(col: str, value: Any) -> None:
                if col in columns:
                    sets.append(f"{col} = ?")
                    params.append(value)

            add_set("customer_code", customer_code)
            add_set("customer_name", customer_name)
            add_set("company_name", company_name)
            add_set("company_type", company_type)
            add_set("db_usage_type", db_usage_type)
            add_set("erp_company_name", erp_company_name)
            add_set("db_server", db_server)
            add_set("db_port", normalized_db_port)
            add_set("db_name", db_name)
            add_set("db_user", db_user)
            add_set("db_driver", db_driver)
            add_set("trust_server_certificate", _bool_to_bit(trust_server_certificate))
            add_set("is_test_company", _bool_to_bit(is_test_company))
            add_set("is_active", _bool_to_bit(is_active))

            if encrypted_password:
                add_set(password_col, encrypted_password)

            if "updated_at" in columns:
                sets.append("updated_at = SYSDATETIME()")

            if not sets:
                raise RuntimeError("수정 가능한 SSAI_COMPANIES 컬럼이 없습니다.")

            params.append(company_code)

            cur.execute(
                f"""
                UPDATE dbo.SSAI_COMPANIES
                SET {", ".join(sets)}
                WHERE company_code = ?
                """,
                *params,
            )

            action = "updated"

        else:
            if not encrypted_password:
                raise ValueError("신규 회원사 ERP DB 등록 시 DB 비밀번호가 필요합니다.")

            insert_cols: list[str] = []
            insert_values_sql: list[str] = []
            params = []

            def add_insert(col: str, value: Any) -> None:
                if col in columns:
                    insert_cols.append(col)
                    insert_values_sql.append("?")
                    params.append(value)

            add_insert("company_code", company_code)
            add_insert("customer_code", customer_code)
            add_insert("customer_name", customer_name)
            add_insert("company_name", company_name)
            add_insert("company_type", company_type)
            add_insert("db_usage_type", db_usage_type)
            add_insert("erp_company_name", erp_company_name)
            add_insert("db_server", db_server)
            add_insert("db_port", normalized_db_port)
            add_insert("db_name", db_name)
            add_insert("db_user", db_user)
            add_insert(password_col, encrypted_password)
            add_insert("db_driver", db_driver)
            add_insert("trust_server_certificate", _bool_to_bit(trust_server_certificate))
            add_insert("is_test_company", _bool_to_bit(is_test_company))
            add_insert("is_active", _bool_to_bit(is_active))

            if "created_at" in columns:
                insert_cols.append("created_at")
                insert_values_sql.append("SYSDATETIME()")

            if "updated_at" in columns:
                insert_cols.append("updated_at")
                insert_values_sql.append("SYSDATETIME()")

            cur.execute(
                f"""
                INSERT INTO dbo.SSAI_COMPANIES (
                    {", ".join(insert_cols)}
                )
                VALUES (
                    {", ".join(insert_values_sql)}
                )
                """,
                *params,
            )

            action = "inserted"

        conn.commit()

    return {
        "ok": True,
        "action": action,
        "company": get_company_by_code(company_code),
        "connection_test": test_saved_company_connection(company_code),
    }



def list_customer_candidates(
    keyword: str,
    *,
    top: int = 20,
) -> list[dict[str, Any]]:
    """
    회원사명/ERP 내부 회사명 기준 기존 회원사 후보를 조회한다.

    사업자등록번호는 보관하지 않는 설계이므로 자동 확정하지 않고
    화면에서 후보만 보여준 뒤 관리자가 선택/수정한다.
    """
    keyword = str(keyword or "").strip()
    top = max(1, min(int(top or 20), 100))

    if not keyword:
        return []

    like = f"%{keyword}%"

    with connect_ssai_db() as conn:
        columns = _get_table_columns(conn, "SSAI_COMPANIES")

        if "customer_code" not in columns or "customer_name" not in columns:
            return []

        rows = _fetch_all_dicts(
            conn,
            f"""
            SELECT TOP {top}
                customer_code,
                customer_name,
                COUNT(*) AS db_count,
                MAX(updated_at) AS last_updated_at
            FROM dbo.SSAI_COMPANIES
            WHERE is_active = 1
              AND (
                    customer_name LIKE ?
                 OR company_name LIKE ?
                 OR erp_company_name LIKE ?
              )
            GROUP BY
                customer_code,
                customer_name
            ORDER BY
                CASE WHEN customer_name = ? THEN 0 ELSE 1 END,
                customer_name
            """,
            (like, like, like, keyword),
        )

    return rows

def set_company_active(
    *,
    company_code: str,
    is_active: bool,
) -> dict[str, Any]:
    company_code = str(company_code or "").strip()

    if not company_code:
        raise ValueError("ERP DB 코드(company_code)가 필요합니다.")

    with connect_ssai_db() as conn:
        columns = _get_table_columns(conn, "SSAI_COMPANIES")

        if "is_active" not in columns:
            raise RuntimeError("SSAI_COMPANIES.is_active 컬럼이 없습니다.")

        sql = """
        UPDATE dbo.SSAI_COMPANIES
        SET is_active = ?
        """

        params: list[Any] = [_bool_to_bit(is_active)]

        if "updated_at" in columns:
            sql += ", updated_at = SYSDATETIME()"

        sql += " WHERE company_code = ?"
        params.append(company_code)

        cur = conn.cursor()
        cur.execute(sql, *params)

        if cur.rowcount <= 0:
            raise ValueError(f"회원사 ERP DB를 찾지 못했습니다. company_code={company_code}")

        conn.commit()

    return {
        "ok": True,
        "action": "company_active_updated",
        "company": get_company_by_code(company_code),
    }