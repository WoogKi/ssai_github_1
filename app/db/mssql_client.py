# app/db/mssql_client.py
# === mssql_client.py 안정판 ===

VERSION = "chat_middleware/2025-11-01T-v1"

import os, logging, re, time
from contextlib import contextmanager
from urllib.parse import quote_plus
import pyodbc
import pandas as pd
from contextvars import ContextVar
from sqlalchemy import create_engine,text,inspect
from sqlalchemy.engine import Engine
from typing import Any, Optional, Sequence, Dict


class DashboardQueryMeasurement:
    """Collect per-query metadata only while a Dashboard request is active."""

    def __init__(self, *, request_id: str, company_id: Any = "") -> None:
        self.request_id = str(request_id or "")
        self.company_id = str(company_id or "")
        self.records: list[dict[str, Any]] = []
        self.phase_records: list[dict[str, Any]] = []

    def add(self, record: Dict[str, Any]) -> None:
        self.records.append(dict(record))

    def add_phase(
        self,
        *,
        phase: str,
        source_name: str,
        source_mode: str = "",
        input_rows: int = 0,
        result_rows: int = 0,
        elapsed_ms: int = 0,
        cache_used: bool = False,
        started_at_perf: float | None = None,
        finished_at_perf: float | None = None,
        input_cols: int = 0,
        result_cols: int = 0,
        physical_query_count_before: int | None = None,
        physical_query_count_after: int | None = None,
        copy_occurred: bool = False,
        query_name: str = "",
        table_names: str = "",
    ) -> None:
        started = float(started_at_perf) if started_at_perf is not None else None
        finished = float(finished_at_perf) if finished_at_perf is not None else None
        if started is not None and finished is not None:
            elapsed_ms = int(max(0.0, finished - started) * 1000)
        before = len(self.records) if physical_query_count_before is None else max(0, int(physical_query_count_before))
        after = len(self.records) if physical_query_count_after is None else max(before, int(physical_query_count_after))
        self.phase_records.append(
            {
                "phase": str(phase or "unknown"),
                "source_name": str(source_name or "unknown"),
                "source_mode": str(source_mode or ""),
                "input_rows": max(0, int(input_rows or 0)),
                "result_rows": max(0, int(result_rows or 0)),
                "input_cols": max(0, int(input_cols or 0)),
                "result_cols": max(0, int(result_cols or 0)),
                "elapsed_ms": max(0, int(elapsed_ms or 0)),
                "physical_query_count_before": before,
                "physical_query_count_after": after,
                "physical_query_count_delta": after - before,
                "cache_used": bool(cache_used),
                "copy_occurred": bool(copy_occurred),
                "query_name": str(query_name or ""),
                "table_names": str(table_names or ""),
                "company_id": self.company_id,
            }
        )

    def summary(self) -> Dict[str, Any]:
        by_source: Dict[str, int] = {}
        for record in self.records:
            source = str(record.get("source") or "unknown")
            by_source[source] = int(by_source.get(source) or 0) + 1
        by_phase: Dict[str, int] = {}
        for record in self.records:
            phase = str(record.get("phase") or "unknown")
            by_phase[phase] = int(by_phase.get(phase) or 0) + 1
        return {
            "logical_source_count": 3,
            "physical_query_count": len(self.records),
            "physical_query_count_total": len(self.records),
            "physical_query_count_by_source": by_source,
            "physical_query_count_by_phase": by_phase,
            "physical_queries": [dict(record) for record in self.records],
            "phase_metrics": [dict(record) for record in self.phase_records],
        }


_DASHBOARD_QUERY_MEASUREMENT: ContextVar[Optional[DashboardQueryMeasurement]] = ContextVar(
    "dashboard_query_measurement", default=None
)
_DASHBOARD_QUERY_PHASE: ContextVar[Dict[str, str]] = ContextVar("dashboard_query_phase", default={})


def get_active_dashboard_query_measurement() -> Optional[DashboardQueryMeasurement]:
    return _DASHBOARD_QUERY_MEASUREMENT.get()


def _dashboard_table_names(sql: str) -> str:
    sql_text = str(sql or "")
    cte_names = {
        name
        for match in re.finditer(r"(?:\bWITH|,)\s*([A-Za-z0-9_]+)\s+AS\s*\(", sql_text, flags=re.IGNORECASE)
        for name in (match.group(1),)
    }
    names = re.findall(r"\b(?:FROM|JOIN)\s+(?:dbo\.)?([A-Za-z0-9_]+)", sql_text, flags=re.IGNORECASE)
    return ",".join(name for name in dict.fromkeys(names) if name not in cte_names)


@contextmanager
def dashboard_query_measurement(
    measurement: DashboardQueryMeasurement,
    *,
    source: str,
    phase: str,
    source_mode: str = "",
) -> Any:
    measurement_token = _DASHBOARD_QUERY_MEASUREMENT.set(measurement)
    previous_phase = dict(_DASHBOARD_QUERY_PHASE.get() or {})
    phase_token = _DASHBOARD_QUERY_PHASE.set(
        {
            "source": str(source or previous_phase.get("source") or "unknown"),
            "phase": str(phase or previous_phase.get("phase") or "query"),
            "source_mode": str(source_mode or previous_phase.get("source_mode") or ""),
        }
    )
    try:
        yield measurement
    finally:
        _DASHBOARD_QUERY_PHASE.reset(phase_token)
        _DASHBOARD_QUERY_MEASUREMENT.reset(measurement_token)


@contextmanager
def dashboard_measurement_phase(
    measurement: DashboardQueryMeasurement,
    *,
    phase: str,
    source: str,
    source_mode: str = "",
    input_rows: int = 0,
    input_cols: int = 0,
    cache_used: bool = False,
) -> Any:
    """Record one exclusive Dashboard stage and its physical-query delta."""
    started = time.perf_counter()
    before = len(measurement.records)
    state: Dict[str, Any] = {
        "result_rows": 0,
        "result_cols": 0,
        "copy_occurred": False,
        "query_name": "",
        "table_names": "",
    }
    with dashboard_query_measurement(
        measurement,
        source=source,
        phase=phase,
        source_mode=source_mode,
    ):
        try:
            yield state
        finally:
            measurement.add_phase(
                phase=phase,
                source_name=source,
                source_mode=source_mode,
                input_rows=input_rows,
                result_rows=state.get("result_rows") or 0,
                input_cols=input_cols,
                result_cols=state.get("result_cols") or 0,
                cache_used=cache_used,
                copy_occurred=bool(state.get("copy_occurred")),
                query_name=str(state.get("query_name") or ""),
                table_names=str(state.get("table_names") or ""),
                started_at_perf=started,
                finished_at_perf=time.perf_counter(),
                physical_query_count_before=before,
                physical_query_count_after=len(measurement.records),
            )

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
    measurement = _DASHBOARD_QUERY_MEASUREMENT.get()
    if measurement is not None:
        phase = dict(_DASHBOARD_QUERY_PHASE.get() or {})
        record = {
            "physical_query_index": len(measurement.records) + 1,
            "source": str(phase.get("source") or "unknown"),
            "phase": str(phase.get("phase") or "query"),
            "source_mode": str(phase.get("source_mode") or ""),
            "query_name": str(phase.get("phase") or "query"),
            "table_names": _dashboard_table_names(sql),
            "result_rows": int(len(df)),
            "elapsed_ms": ms,
            "cache_used": False,
        }
        measurement.add(record)
        try:
            _sims.info(
                "[dashboard.physical_query] request_id=%s source=%s phase=%s source_mode=%s query=%s tables=%s rows=%s elapsed_ms=%s physical_query_index=%s cache_used=False",
                measurement.request_id,
                record["source"],
                record["phase"],
                record["source_mode"],
                record["query_name"],
                record["table_names"],
                record["result_rows"],
                record["elapsed_ms"],
                record["physical_query_index"],
            )
        except Exception:
            pass
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
        
