# app/db/mssql_client.py
# === mssql_client.py 안정판 ===

VERSION = "chat_middleware/2025-11-01T-v1"

import os, logging, time
from contextlib import contextmanager
from urllib.parse import quote_plus
import pyodbc
import pandas as pd
from contextvars import ContextVar
from sqlalchemy import create_engine,text,inspect
from sqlalchemy.engine import Engine
from typing import Any, Optional, Sequence, Dict

# -----------------------------------------------------------------------------
# .env 키 (예시)
# -----------------------------------------------------------------------------
# MSSQL_DRIVER=ODBC Driver 18 for SQL Server
# MSSQL_SERVER=host,1433            # 또는 host\INSTANCE (고정 포트 권장)
# MSSQL_DATABASE=YourDb
# MSSQL_UID=sa                      # SQL 인증 시
# MSSQL_PWD=YourStrongPwd!          # SQL 인증 시
# MSSQL_ENCRYPT=yes                 # 초기 yes 권장
# MSSQL_TRUST=yes                   # 초기 yes 권장 (운영 인증서 배포 후 no)
# MSSQL_TIMEOUT=15                  # 접속 타임아웃(초)
# MSSQL_MARS=yes                    # 다중결과셋 사용
#
# ---- 문자 인코딩 토글(레거시 대응) ----
# MSSQL_CHARSET=cp949               # VARCHAR/CHAR 디코딩 (기본 cp949; 유니코드면 utf-8)
# MSSQL_WCHARSET=utf-16le           # NVARCHAR/NCHAR 디코딩 (기본 utf-16le)
# MSSQL_CLIENT_ENCODING=utf-8       # 파이썬→ODBC 바인딩 기본
#
# ---- 로깅/디버그 ----
# SQL_ECHO=no                       # yes면 SQLAlchemy echo on
# -----------------------------------------------------------------------------

# .env load: project-root only, no cwd-based discovery.
try:
    from app.utils.env_config import load_project_env
    load_project_env(override=False)
except Exception:
    pass

# ODBC 값 안전 인코딩(세미콜론/중괄호/공백 포함 시 브레이싱)
def _odbc_quote(v: str) -> str:
    if v is None:
        return ""
    s = str(v)
    if any(ch in s for ch in (";", "{", "}", " ")):
        s = s.replace("}", "}}")
        return "{" + s + "}"
    return s

def _build_conn_str() -> str:
    driver  = (os.getenv("MSSQL_DRIVER") or "ODBC Driver 18 for SQL Server").strip()
    server  = (os.getenv("MSSQL_SERVER") or "").strip()
    db      = (os.getenv("MSSQL_DATABASE") or "").strip()
    uid     = (os.getenv("MSSQL_UID") or "").strip()
    pwd     = (os.getenv("MSSQL_PWD") or "")

    if not server or not db:
        raise RuntimeError("MSSQL_SERVER / MSSQL_DATABASE 환경변수가 필요합니다.")

    encrypt = "Yes" if (os.getenv("MSSQL_ENCRYPT", "yes").lower() == "yes") else "No"
    trust   = "Yes" if (os.getenv("MSSQL_TRUST", "yes").lower() == "yes") else "No"

    parts = [
        f"Driver={{{driver}}};",
        f"Server={_odbc_quote(server)};",
        f"Database={_odbc_quote(db)};",
        f"Encrypt={encrypt};",
        f"TrustServerCertificate={trust};",
    ]
    if uid:
        parts += [
            f"Uid={_odbc_quote(uid)};",
            f"Pwd={_odbc_quote(pwd)};",
            "Authentication=SqlPassword;",
        ]
    else:
        parts += ["Trusted_Connection=Yes;"]  # (필요시)
    return "".join(parts)

def set_current_company_id(company_id: Optional[int]) -> None:
    """
    CLI 테스트나 배치 작업에서 사용할 회사 DB 지정 함수.

    Streamlit 화면에서는 st.session_state['ssai_selected_company']를 우선 사용하므로
    일반 화면 코드에서는 직접 호출할 필요가 없다.
    """
    if company_id is None:
        _CURRENT_COMPANY_ID.set(None)
        return

    _CURRENT_COMPANY_ID.set(int(company_id))


def get_current_company_id() -> Optional[int]:
    """
    현재 사용할 회사 ID를 가져온다.

    우선순위:
    1. set_current_company_id()로 지정한 값
    2. Streamlit session_state['ssai_selected_company']['company_id']
    3. 없음
    """
    try:
        override_id = _CURRENT_COMPANY_ID.get()
        if override_id:
            return int(override_id)
    except Exception:
        pass

    try:
        import streamlit as st  # type: ignore

        company = st.session_state.get("ssai_selected_company")
        if isinstance(company, dict):
            company_id = company.get("company_id")
            if company_id:
                return int(company_id)
    except Exception:
        pass

    return None


def _build_company_conn_str(company_id: int) -> str:
    """
    SSAI_COMPANIES에 저장된 회사 DB 접속정보를 복호화해서
    SQLAlchemy/pyodbc용 ODBC connection string을 만든다.
    """
    from app.services.ssai_auth_service import get_company_db_config

    cfg = get_company_db_config(int(company_id))

    server_value = cfg.db_server
    if cfg.db_port:
        server_value = f"{cfg.db_server},{cfg.db_port}"

    return (
        f"Driver={{{cfg.db_driver}}};"
        f"Server={_odbc_quote(server_value)};"
        f"Database={_odbc_quote(cfg.db_name)};"
        f"Uid={_odbc_quote(cfg.db_user)};"
        f"Pwd={_odbc_quote(cfg.db_password)};"
        "Encrypt=Yes;"
        "TrustServerCertificate=Yes;"
        "Authentication=SqlPassword;"
    )


def _get_company_engine(company_id: int) -> Engine:
    """
    회사별 ERP DB 엔진 캐시.
    company_id별로 별도 Engine을 유지한다.
    """
    company_id = int(company_id)

    if company_id in _COMPANY_ENGINES:
        return _COMPANY_ENGINES[company_id]

    t0 = time.perf_counter()
    raw = _build_company_conn_str(company_id)

    params = quote_plus(raw)
    echo = os.getenv("SQL_ECHO", "no").lower() == "yes"

    engine = create_engine(
        f"mssql+pyodbc:///?odbc_connect={params}",
        pool_pre_ping=True,
        pool_recycle=300,
        pool_size=5,
        max_overflow=10,
        echo=echo,
        connect_args={"timeout": 5},
    )

    _COMPANY_ENGINES[company_id] = engine
    try:
        logging.getLogger("ssai.sims.sql").info(
            "[db.connection] company_id=%s connection_configured=%s connection_result=%s elapsed_ms=%s",
            company_id,
            bool(raw),
            "engine_created",
            int((time.perf_counter() - t0) * 1000),
        )
    except Exception:
        pass
    return engine


_ENGINE: Optional[Engine] = None

_COMPANY_ENGINES: dict[int, Engine] = {}
_CURRENT_COMPANY_ID: ContextVar[Optional[int]] = ContextVar(
    "ssai_current_company_id",
    default=None,
)

def _get_engine() -> Engine:
    global _ENGINE

    # Phase 3:
    # 로그인 후 선택된 회사가 있으면 기존 .env DB가 아니라
    # SSAI_COMPANIES의 회사별 ERP DB 엔진을 우선 사용한다.
    company_id = get_current_company_id()
    if company_id:
        return _get_company_engine(company_id)

    # 선택 회사가 없으면 기존 .env 방식으로 fallback.
    # CLI 회귀테스트, 초기 부팅, 과거 호환용이다.
    if _ENGINE is not None:
        return _ENGINE

    t0 = time.perf_counter()
    raw = _build_conn_str()

    params = quote_plus(raw)
    echo = os.getenv("SQL_ECHO", "no").lower() == "yes"
    engine = create_engine(
        f"mssql+pyodbc:///?odbc_connect={params}",
        pool_pre_ping=True,
        pool_recycle=300,
        pool_size=5, max_overflow=10,
        echo=echo,
        connect_args={"timeout": 5}
    )
    _ENGINE = engine
    try:
        logging.getLogger("ssai.sims.sql").info(
            "[db.connection] company_id=%s connection_configured=%s connection_result=%s elapsed_ms=%s",
            "",
            bool(raw),
            "engine_created",
            int((time.perf_counter() - t0) * 1000),
        )
    except Exception:
        pass
    return engine

def get_engine() -> Engine:
    return _get_engine()

@contextmanager
def get_conn():
    conn = _get_engine().connect()
    try:
        yield conn
    finally:
        try: conn.close()
        except Exception: pass

# ssai.sims.sql 로거
_sims = logging.getLogger("ssai.sims.sql")
_sims.propagate = True
try:
    _sims.setLevel(getattr(logging, os.getenv("SIMS_LOG_LEVEL", "INFO").upper(), logging.INFO))
except Exception:
    _sims.setLevel(logging.INFO)

def log_sql(name: str, sql: str, params: Optional[Sequence[Any] | Dict[str, Any]] = None, level: Optional[int] = None) -> None:
    try:
        if level is None:
            level = logging.ERROR if name.endswith(".ERROR") else logging.DEBUG
        if isinstance(params, (list, tuple)):
            _sims.log(level, "%s\nSQL:\n%s\nparams: %s", name, sql, tuple(params))
        else:
            _sims.log(level, "%s\nSQL:\n%s\nparams: %s", name, sql, params)
    except Exception:
        pass

# 기본 유틸
def read_df(sql: str, params: Sequence[Any] | Dict[str, Any] = ()) -> pd.DataFrame:
    import time
    t0 = time.perf_counter()
    with get_conn() as conn:
        df = pd.read_sql(sql, con=conn, params=params)
    ms = int((time.perf_counter() - t0) * 1000)
    try:
        _sims.debug("[db.read_df] rows=%s, %s ms", len(df), ms)
    except Exception:
        pass
    return df

# 하위호환 DataFrame alias
def query_to_df(sql: str, params: Sequence[Any] | Dict[str, Any] = ()) -> pd.DataFrame:
    return read_df(sql, params)

def fetch_dataframe(sql: str, params: Sequence[Any] | Dict[str, Any] = ()) -> pd.DataFrame:
    return read_df(sql, params)

def execute_query_df(sql: str, params: Sequence[Any] | Dict[str, Any] = ()) -> pd.DataFrame:
    return read_df(sql, params)

def read_sql_df(sql: str, params: Sequence[Any] | Dict[str, Any] = ()) -> pd.DataFrame:
    return read_df(sql, params)

def run_query_df(sql: str, params: Sequence[Any] | Dict[str, Any] = ()) -> pd.DataFrame:
    return read_df(sql, params)

def query_df(sql: str, params: Sequence[Any] | Dict[str, Any] = ()) -> pd.DataFrame:
    return read_df(sql, params)

def query(sql: str, params: Sequence[Any] | Dict[str, Any] = ()) -> pd.DataFrame:
    return read_df(sql, params)
def fetch_one(sql: str, params: Sequence[Any] | Dict[str, Any] = ()) -> Optional[Dict[str, Any]]:
    df = read_df(sql, params)
    return None if df.empty else df.iloc[0].to_dict()

# ===== 하위호환 유틸 =====
def fetch_all(sql: str, params: Sequence[Any] | Dict[str, Any] = ()):
    """
    과거 인터페이스 호환용: 쿼리 결과를 list[dict]로 반환.
    """
    df = read_df(sql, params)
    return df.to_dict("records")

def health_check() -> dict:
    """
    SIMS Debug 패널에서 사용하는 간단 DB 헬스체크.
    - SELECT 1 한 번만 날려 보고 True / False 만 리턴.
    """
    try:
        eng = get_engine()  # ← 여기서 engine을 직접 쓰지 말고, get_engine() 통해 가져온다
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        _sims.warning("[DB] health_check failed")
        return False


@contextmanager
def get_connection():
    """Backward-compat alias for get_conn()."""
    with get_conn() as c:
        yield c

# (옵션) LIMIT 헬퍼 – 지금은 TOP로 운용 권장
def build_limit_sql(top: int, order_by_sql: str) -> tuple[str, str]:
    style = (os.getenv("SIMS_LIMIT_STYLE") or "top").lower()
    top = int(top)
    if style == "offset":
        return "", f"{order_by_sql} OFFSET 0 ROWS FETCH NEXT {top} ROWS ONLY"
    return f"TOP {top}", order_by_sql  # 기본 TOP

def list_tables(limit: int = 50) -> "pd.DataFrame":
    """현재 데이터베이스의 테이블 목록을 반환한다.

    - Debug 모드 > Health Check 에서 사용
    - limit 개수까지만 잘라서 반환
    """
    engine = get_engine()
    if engine is None:
        raise RuntimeError("MSSQL 엔진이 초기화되지 않았습니다. (.env 설정을 확인하세요.)")

    with engine.connect() as conn:
        try:
            # 기본: SQLAlchemy Inspector 사용
            insp = inspect(engine)
            tables = insp.get_table_names()
            rows = [{"schema": None, "table": name} for name in tables]
        except Exception:
            # Fallback: INFORMATION_SCHEMA 사용
            res = conn.execute(text("""
                SELECT TABLE_SCHEMA, TABLE_NAME
                FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_TYPE = 'BASE TABLE'
                ORDER BY TABLE_SCHEMA, TABLE_NAME
            """))
            rows = [
                {"schema": r[0], "table": r[1]}
                for r in res.fetchall()
            ]

    df = pd.DataFrame(rows)
    if limit and len(df) > limit:
        df = df.head(limit)
    return df


def search_columns(keyword: str, limit: int = 100) -> "pd.DataFrame":
    """컬럼명에 keyword 가 포함된 컬럼들을 검색한다.

    반환 컬럼:
      - schema
      - table
      - column
      - data_type
    """
    engine = get_engine()
    if engine is None:
        raise RuntimeError("MSSQL 엔진이 초기화되지 않았습니다. (.env 설정을 확인하세요.)")

    kw = f"%{keyword}%"
    sql = text("""
        SELECT TOP (:limit)
            TABLE_SCHEMA,
            TABLE_NAME,
            COLUMN_NAME,
            DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE COLUMN_NAME LIKE :kw
        ORDER BY TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME
    """)
    with engine.connect() as conn:
        res = conn.execute(sql, {"limit": limit, "kw": kw})
        rows = [
            {
                "schema": r[0],
                "table": r[1],
                "column": r[2],
                "data_type": r[3],
            }
            for r in res.fetchall()
        ]
    return pd.DataFrame(rows)
        
