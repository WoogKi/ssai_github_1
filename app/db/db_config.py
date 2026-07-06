# app/db/db_config.py
from dataclasses import dataclass
from typing import Optional
import os
import streamlit as st


def _read_secret_or_env(key: str, default: Optional[str] = None) -> Optional[str]:
    """
    공통 설정 로더
    1) st.secrets[key]
    2) os.environ[key]
    3) default
    """
    try:
        v = st.secrets.get(key)
        if v not in (None, ""):
            return str(v)
    except Exception:
        # secrets 설정이 없거나, 키가 없을 수도 있으므로 조용히 패스
        pass

    v = os.environ.get(key)
    return v if v not in (None, "") else default


@dataclass(frozen=True)
class MSSQLConfig:
    host: str
    port: int
    database: str
    user: str
    password: str
    encrypt: str = "no"
    trust_cert: str = "yes"
    timeout: int = 5
    # 🔹 Debug/접속 테스트에서 같이 쓰기 편하도록 드라이버 필드 추가
    odbc_driver: str = "ODBC Driver 18 for SQL Server"


def load_mssql_config() -> MSSQLConfig:
    """
    MSSQL 설정을 한 번에 로드하는 단일 진실 소스.
    - 실제 DB 접속 코드
    - Debug 패널
    양쪽 모두 이 함수를 기준으로 사용해야 일관성이 유지된다.
    """
    return MSSQLConfig(
        host=_read_secret_or_env("MSSQL_HOST", "127.0.0.1"),
        port=int(_read_secret_or_env("MSSQL_PORT", "1433")),
        database=_read_secret_or_env("MSSQL_DB", "master"),
        user=_read_secret_or_env("MSSQL_USER", "sa"),
        password=_read_secret_or_env("MSSQL_PASSWORD", ""),
        encrypt=_read_secret_or_env("MSSQL_ENCRYPT", "no"),
        trust_cert=_read_secret_or_env("MSSQL_TRUST_CERT", "yes"),
        timeout=int(_read_secret_or_env("MSSQL_TIMEOUT", "5")),
        odbc_driver=_read_secret_or_env(
            "MSSQL_ODBC_DRIVER", "ODBC Driver 18 for SQL Server"
        ),
    )
