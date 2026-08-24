from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import pyodbc

from app.services.ssai_auth_service import CompanyDbConfig, build_conn_str, get_company_db_config


AnalyticsRole = Literal["reader", "writer", "migration"]
ANALYTICS_DATABASE_NAME = "SSAI_ANALYTICS"


class AnalyticsTargetResolutionError(RuntimeError):
    """Raised when the CompanyDbConfig-derived analytics target is unsafe."""


@dataclass(frozen=True)
class AnalyticsTarget:
    target_id: str
    erp_server_identity: str
    analytics_server: str
    database: str


def normalize_sql_server_identity(server: Any, port: Any = None) -> str:
    """Return the stable configured endpoint identity without DNS resolution."""
    host = str(server or "").strip().lower()
    if not host:
        raise AnalyticsTargetResolutionError("company ERP server is missing")
    suffix = str(port or "").strip()
    return f"{host},{suffix}" if suffix else host


def _server_value(config: CompanyDbConfig) -> str:
    return f"{config.db_server},{config.db_port}" if config.db_port else str(config.db_server)


def _target_from_company_config(config: CompanyDbConfig, role: AnalyticsRole) -> AnalyticsTarget:
    """Use the selected company's ERP endpoint as the only analytics endpoint.

    The database name is the only connection-string change. This makes a SQL
    Server instance the shared analytics target for every company on it, with
    no parallel registry, credential store, or central fallback.
    """
    if role not in {"reader", "writer", "migration"}:
        raise AnalyticsTargetResolutionError(f"unsupported analytics role: {role!r}")
    identity = normalize_sql_server_identity(config.db_server, config.db_port)
    return AnalyticsTarget(
        target_id=f"company-db-server:{identity}",
        erp_server_identity=identity,
        analytics_server=_server_value(config),
        database=ANALYTICS_DATABASE_NAME,
    )


def resolve_analytics_target(company_id: int, role: AnalyticsRole) -> AnalyticsTarget:
    return _target_from_company_config(get_company_db_config(int(company_id)), role)


def connect_company_analytics_db(
    company_id: int,
    role: AnalyticsRole,
    *,
    autocommit: bool = False,
    timeout: int = 10,
) -> pyodbc.Connection:
    """Connect to the same CompanyDbConfig SQL Server, changing only database."""
    if role not in {"reader", "writer", "migration"}:
        raise AnalyticsTargetResolutionError(f"unsupported analytics role: {role!r}")
    config = get_company_db_config(int(company_id))
    target = _target_from_company_config(config, role)
    conn = pyodbc.connect(
        build_conn_str(
            driver=str(config.db_driver),
            server=target.analytics_server,
            database=ANALYTICS_DATABASE_NAME,
            user=str(config.db_user),
            password=str(config.db_password),
        ),
        timeout=max(1, int(timeout)),
        autocommit=bool(autocommit),
    )
    conn.timeout = max(1, int(timeout))
    cursor = conn.cursor()
    try:
        row = cursor.execute(
            "SELECT DB_NAME(), "
            "CAST(SERVERPROPERTY('MachineName') AS nvarchar(128)), "
            "CAST(SERVERPROPERTY('ServerName') AS nvarchar(128))"
        ).fetchone()
        if not row or str(row[0] or "") != ANALYTICS_DATABASE_NAME:
            raise AnalyticsTargetResolutionError("analytics connection database mismatch")
        # SQL Server may report its canonical host while CompanyDbConfig uses
        # an IP address or alias. The endpoint is already identical by
        # construction: this connection changes only the database name.
    except Exception:
        # Preserve the failed target verification. Closing the connection first
        # makes pyodbc cursor.close() mask it with a ProgrammingError.
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()
        raise
    else:
        cursor.close()
    return conn
