from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pyodbc

from app.services.ssai_auth_service import build_conn_str, load_dotenv, pick_env


ANALYTICS_DATABASE_NAME = "SSAI_ANALYTICS"
AnalyticsDbRole = Literal["reader", "writer", "migration"]


class AnalyticsDbConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AnalyticsDbSettings:
    server: str
    database: str
    user: str
    password: str
    driver: str
    role: AnalyticsDbRole


def load_analytics_db_settings(role: AnalyticsDbRole) -> AnalyticsDbSettings:
    if role not in {"reader", "writer", "migration"}:
        raise AnalyticsDbConfigurationError(f"unsupported analytics DB role: {role!r}")
    env = load_dotenv(".env")
    role_prefix = f"SSAI_ANALYTICS_DB_{role.upper()}"
    server = pick_env(env, ["SSAI_ANALYTICS_DB_SERVER", "SSAI_DB_SERVER"])
    database = pick_env(env, ["SSAI_ANALYTICS_DB_NAME"], ANALYTICS_DATABASE_NAME)
    user = pick_env(
        env,
        [f"{role_prefix}_USER", "SSAI_ANALYTICS_DB_USER", "SSAI_DB_USER"],
    )
    password = pick_env(
        env,
        [f"{role_prefix}_PASSWORD", "SSAI_ANALYTICS_DB_PASSWORD", "SSAI_DB_PASSWORD"],
    )
    driver = pick_env(
        env,
        ["SSAI_ANALYTICS_DB_DRIVER", "SSAI_DB_DRIVER"],
        "ODBC Driver 18 for SQL Server",
    )
    missing = [
        name
        for name, value in (
            ("SSAI_ANALYTICS_DB_SERVER or SSAI_DB_SERVER", server),
            (f"{role_prefix}_USER or SSAI_DB_USER", user),
            (f"{role_prefix}_PASSWORD or SSAI_DB_PASSWORD", password),
        )
        if not value
    ]
    if missing:
        raise AnalyticsDbConfigurationError(
            "SSAI analytics DB configuration is incomplete: " + ", ".join(missing)
        )
    if str(database).strip() != ANALYTICS_DATABASE_NAME:
        raise AnalyticsDbConfigurationError(
            f"SSAI_ANALYTICS_DB_NAME must be {ANALYTICS_DATABASE_NAME}"
        )
    return AnalyticsDbSettings(
        server=str(server),
        database=ANALYTICS_DATABASE_NAME,
        user=str(user),
        password=str(password),
        driver=str(driver),
        role=role,
    )


def connect_analytics_db(
    role: AnalyticsDbRole,
    *,
    autocommit: bool = False,
    timeout: int = 10,
) -> pyodbc.Connection:
    settings = load_analytics_db_settings(role)
    conn_str = build_conn_str(
        driver=settings.driver,
        server=settings.server,
        database=ANALYTICS_DATABASE_NAME,
        user=settings.user,
        password=settings.password,
    )
    return pyodbc.connect(conn_str, timeout=int(timeout), autocommit=bool(autocommit))
