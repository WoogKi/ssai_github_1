# tools/check_customer_sales_source_live.py
# -*- coding: utf-8 -*-
"""
매출처별 매출 예상 자료원 운영 DB 비교 도구.

읽기 전용 SELECT 집계만 수행한다. 접속 문자열과 비밀번호는 출력하지 않는다.
앱에서 검증된 회사별 DB 연결 경로(app.db.mssql_client)를 그대로 재사용한다.
"""
from __future__ import annotations

import argparse
import calendar
import contextlib
import io
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.mssql_client import read_df, set_current_company_id  # noqa: E402
from app.services.rddbc_io_common import query_to_df  # noqa: E402
from app.services.ssai_auth_service import get_company_db_config  # noqa: E402


logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(message)s")
logging.getLogger("ssai").setLevel(logging.WARNING)
logging.getLogger("app.db.mssql_client").setLevel(logging.WARNING)
logging.getLogger("ssai.sims.sql").disabled = True


RDD13_TRANS_DI_OUT = "3"
R12_FIN_SUPPLY_EXPR = "COALESCE(O.Rd12_Fin_Supply_Price, O.Rd12_Supply_Price, 0)"
R12_FIN_TAX_EXPR = "COALESCE(O.Rd12_Fin_Tax_Price, O.Rd12_Tax_Price, 0)"


@dataclass
class SourceResult:
    name: str
    df: pd.DataFrame
    elapsed: float


@dataclass
class DiagnosticsResult:
    ok: bool
    company_name: str = ""
    db_name: str = ""
    driver: str = ""
    server_alias: str = ""


def _safe_query_to_df(sql: str, params: dict[str, Any] | None = None) -> pd.DataFrame:
    sql_logger = logging.getLogger("ssai.sims.sql")
    old_disabled = sql_logger.disabled
    old_level = sql_logger.level
    sql_logger.disabled = True
    sql_logger.setLevel(logging.CRITICAL + 1)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return query_to_df(sql, params or {})
    except Exception as exc:
        raise RuntimeError(_safe_error_message("DB SELECT 실행 실패", exc)) from None
    finally:
        sql_logger.disabled = old_disabled
        sql_logger.setLevel(old_level)


def _safe_read_df(sql: str, params: Any = ()) -> pd.DataFrame:
    sql_logger = logging.getLogger("ssai.sims.sql")
    old_disabled = sql_logger.disabled
    old_level = sql_logger.level
    sql_logger.disabled = True
    sql_logger.setLevel(logging.CRITICAL + 1)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return read_df(sql, params)
    except Exception as exc:
        raise RuntimeError(_safe_error_message("DB 진단 SELECT 실패", exc)) from None
    finally:
        sql_logger.disabled = old_disabled
        sql_logger.setLevel(old_level)


def _safe_error_message(prefix: str, exc: BaseException) -> str:
    text = str(exc)
    for key in ("Pwd", "PWD", "Uid", "UID", "Server", "SERVER", "Database", "DATABASE"):
        text = re.sub(fr"{key}=\{{?.*?\}}?;", f"{key}=***;", text, flags=re.IGNORECASE)
    text = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}(?:,\d+)?\b", "[server]", text)
    text = text.replace("\n", " ").strip()
    sqlstates = sorted(set(re.findall(r"\[([0-9A-Z]{5})\]|'([0-9A-Z]{5})'", text)))
    sqlstate_values = sorted({part for pair in sqlstates for part in pair if part})
    numbers = sorted(set(re.findall(r"\((-?\d{3,})\)", text)))
    pieces = [f"{prefix}: {type(exc).__name__}"]
    if sqlstate_values:
        pieces.append(f"SQLSTATE={','.join(sqlstate_values)}")
    if numbers:
        pieces.append(f"ODBC오류번호={','.join(numbers[:5])}")
    if text:
        pieces.append(f"메시지={text[:600]}")
    return " / ".join(pieces)


def _mask_server(server: str, port: int | None) -> str:
    value = str(server or "").strip()
    if not value:
        masked = "(empty)"
    elif re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", value):
        parts = value.split(".")
        masked = f"{parts[0]}.*.*.{parts[-1]}"
    elif len(value) <= 4:
        masked = "***"
    else:
        masked = f"{value[:2]}***{value[-2:]}"
    return f"{masked},{port}" if port else masked


def _yyyymm(value: str) -> str:
    return str(value or "").replace("-", "")[:6]


def _yyyymmdd(value: str) -> str:
    return str(value or "").replace("-", "")[:8]


def _month_start(yyyymm: str) -> str:
    return f"{_yyyymm(yyyymm)}01"


def _month_end(yyyymm: str) -> str:
    month = _yyyymm(yyyymm)
    year = int(month[:4])
    mon = int(month[4:6])
    return f"{month}{calendar.monthrange(year, mon)[1]:02d}"


def _prev_month(yyyymm: str) -> str:
    month = _yyyymm(yyyymm)
    year = int(month[:4])
    mon = int(month[4:6])
    if mon == 1:
        return f"{year - 1}12"
    return f"{year}{mon - 1:02d}"


def _fmt_int(value: Any) -> str:
    try:
        return f"{float(value):,.0f}"
    except Exception:
        return str(value)


def _fmt_float(value: Any) -> str:
    try:
        return f"{float(value):,.4f}"
    except Exception:
        return str(value)


def _empty_grouped() -> pd.DataFrame:
    return pd.DataFrame(columns=["month", "customer_cd", "supply", "tax", "total", "raw_rows"])


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return _empty_grouped()
    out = df.copy()
    out["month"] = out["month"].astype(str).str.strip()
    out["customer_cd"] = out["customer_cd"].astype(str).str.strip()
    for col in ["supply", "tax", "total", "raw_rows"]:
        out[col] = pd.to_numeric(out.get(col, 0), errors="coerce").fillna(0)
    return out[["month", "customer_cd", "supply", "tax", "total", "raw_rows"]]


def _timed_query(name: str, sql: str, params: dict[str, Any] | None = None) -> SourceResult:
    t0 = time.perf_counter()
    df = _safe_query_to_df(sql, params or {})
    return SourceResult(name, _normalize_df(df), time.perf_counter() - t0)


def _timed_df(sql: str, params: dict[str, Any] | None = None) -> tuple[pd.DataFrame, float]:
    t0 = time.perf_counter()
    df = _safe_query_to_df(sql, params or {})
    return df, time.perf_counter() - t0


def _query_rddbc260(month_from: str, month_to: str) -> SourceResult:
    sql = """
SELECT
    LTRIM(RTRIM(M.Rd26_Io_YyMm)) AS month,
    LTRIM(RTRIM(M.Rd26_Ven_Cd)) AS customer_cd,
    SUM(CAST(COALESCE(M.Rd26_Supply_Price, 0) AS float)) AS supply,
    SUM(CAST(COALESCE(M.Rd26_Tax_Price, 0) AS float)) AS tax,
    SUM(CAST(COALESCE(M.Rd26_Supply_Price, 0) + COALESCE(M.Rd26_Tax_Price, 0) AS float)) AS total,
    COUNT_BIG(*) AS raw_rows
FROM dbo.Rddbc260 AS M WITH (NOLOCK)
WHERE M.Rd26_Io_YyMm >= %(month_from)s
  AND M.Rd26_Io_YyMm <= %(month_to)s
  AND M.Rd26_Receive_Di_Gcode = '0023'
  AND M.Rd26_Receive_Di = '00'
  AND M.Rd26_Ven_Cd >= '50000'
  AND M.Rd26_Ven_Cd <= '8ZZZZ'
GROUP BY LTRIM(RTRIM(M.Rd26_Io_YyMm)), LTRIM(RTRIM(M.Rd26_Ven_Cd))
"""
    return _timed_query("Rddbc260", sql, {"month_from": month_from, "month_to": month_to})


def _query_rddbc130_trans(date_from: str, date_to: str) -> SourceResult:
    sql = """
SELECT
    LEFT(LTRIM(RTRIM(T.Rd13_Trans_YyMmDd)), 6) AS month,
    LTRIM(RTRIM(T.Rd13_Ven_Cd)) AS customer_cd,
    SUM(CAST(COALESCE(T.Rd13_Supply_Price, 0) AS float)) AS supply,
    SUM(CAST(COALESCE(T.Rd13_Tax_Price, 0) AS float)) AS tax,
    SUM(CAST(COALESCE(T.Rd13_Supply_Price, 0) + COALESCE(T.Rd13_Tax_Price, 0) AS float)) AS total,
    COUNT_BIG(*) AS raw_rows
FROM dbo.Rddbc130 AS T WITH (NOLOCK)
WHERE T.Rd13_Trans_YyMmDd >= %(date_from)s
  AND T.Rd13_Trans_YyMmDd <= %(date_to)s
  AND T.Rd13_Trans_Di = '3'
  AND T.Rd13_Ven_Cd >= '50000'
  AND T.Rd13_Ven_Cd <= '8ZZZZ'
GROUP BY LEFT(LTRIM(RTRIM(T.Rd13_Trans_YyMmDd)), 6), LTRIM(RTRIM(T.Rd13_Ven_Cd))
"""
    return _timed_query("Rddbc130(Trans_Di=3)", sql, {"date_from": date_from, "date_to": date_to})


def _query_rddbc120_linked_trans(date_from: str, date_to: str) -> SourceResult:
    sql = f"""
SELECT
    LEFT(LTRIM(RTRIM(H.Rd13_Trans_YyMmDd)), 6) AS month,
    LTRIM(RTRIM(H.Rd13_Ven_Cd)) AS customer_cd,
    SUM(CAST({R12_FIN_SUPPLY_EXPR} AS float)) AS supply,
    SUM(CAST({R12_FIN_TAX_EXPR} AS float)) AS tax,
    SUM(CAST({R12_FIN_SUPPLY_EXPR} + {R12_FIN_TAX_EXPR} AS float)) AS total,
    COUNT_BIG(*) AS raw_rows
FROM dbo.Rddbc130 AS H WITH (NOLOCK)
JOIN dbo.Rddbc120 AS O WITH (NOLOCK)
  ON O.Rd12_Trans_Di = H.Rd13_Trans_Di
 AND O.Rd12_Trans_YyMmDd = H.Rd13_Trans_YyMmDd
 AND O.Rd12_Ven_Cd = H.Rd13_Ven_Cd
 AND O.Rd12_Trans_Seq = H.Rd13_Trans_Seq
WHERE H.Rd13_Trans_YyMmDd >= %(date_from)s
  AND H.Rd13_Trans_YyMmDd <= %(date_to)s
  AND H.Rd13_Trans_Di = '3'
  AND H.Rd13_Ven_Cd >= '50000'
  AND H.Rd13_Ven_Cd <= '8ZZZZ'
GROUP BY LEFT(LTRIM(RTRIM(H.Rd13_Trans_YyMmDd)), 6), LTRIM(RTRIM(H.Rd13_Ven_Cd))
"""
    return _timed_query("Rddbc120(거래명세서연결)", sql, {"date_from": date_from, "date_to": date_to})


def _query_rddbc120_out_date(date_from: str, date_to: str, prefixes: tuple[str, ...], name: str) -> SourceResult:
    prefix_sql = ", ".join(f"'{p}'" for p in prefixes)
    sql = f"""
SELECT
    LEFT(LTRIM(RTRIM(O.Rd12_Out_YyMmDd)), 6) AS month,
    LTRIM(RTRIM(O.Rd12_Ven_Cd)) AS customer_cd,
    SUM(CASE WHEN LEFT(LTRIM(RTRIM(O.Rd12_Io_Gu)), 1) = '6'
             THEN -1 * CAST({R12_FIN_SUPPLY_EXPR} AS float)
             ELSE CAST({R12_FIN_SUPPLY_EXPR} AS float)
        END) AS supply,
    SUM(CASE WHEN LEFT(LTRIM(RTRIM(O.Rd12_Io_Gu)), 1) = '6'
             THEN -1 * CAST({R12_FIN_TAX_EXPR} AS float)
             ELSE CAST({R12_FIN_TAX_EXPR} AS float)
        END) AS tax,
    SUM(CASE WHEN LEFT(LTRIM(RTRIM(O.Rd12_Io_Gu)), 1) = '6'
             THEN -1 * CAST({R12_FIN_SUPPLY_EXPR} + {R12_FIN_TAX_EXPR} AS float)
             ELSE CAST({R12_FIN_SUPPLY_EXPR} + {R12_FIN_TAX_EXPR} AS float)
        END) AS total,
    COUNT_BIG(*) AS raw_rows
FROM dbo.Rddbc120 AS O WITH (NOLOCK)
WHERE O.Rd12_Out_YyMmDd >= %(date_from)s
  AND O.Rd12_Out_YyMmDd <= %(date_to)s
  AND LEFT(LTRIM(RTRIM(O.Rd12_Io_Gu)), 1) IN ({prefix_sql})
  AND O.Rd12_Ven_Cd >= '50000'
  AND O.Rd12_Ven_Cd <= '8ZZZZ'
GROUP BY LEFT(LTRIM(RTRIM(O.Rd12_Out_YyMmDd)), 6), LTRIM(RTRIM(O.Rd12_Ven_Cd))
"""
    return _timed_query(name, sql, {"date_from": date_from, "date_to": date_to})


def _query_io_prefix_impact(date_from: str, date_to: str) -> tuple[pd.DataFrame, float]:
    sql = f"""
SELECT
    LEFT(LTRIM(RTRIM(O.Rd12_Io_Gu)), 1) AS io_prefix,
    SUM(CASE WHEN LEFT(LTRIM(RTRIM(O.Rd12_Io_Gu)), 1) = '6'
             THEN -1 * CAST({R12_FIN_SUPPLY_EXPR} AS float)
             ELSE CAST({R12_FIN_SUPPLY_EXPR} AS float)
        END) AS supply,
    SUM(CASE WHEN LEFT(LTRIM(RTRIM(O.Rd12_Io_Gu)), 1) = '6'
             THEN -1 * CAST({R12_FIN_TAX_EXPR} AS float)
             ELSE CAST({R12_FIN_TAX_EXPR} AS float)
        END) AS tax,
    SUM(CASE WHEN LEFT(LTRIM(RTRIM(O.Rd12_Io_Gu)), 1) = '6'
             THEN -1 * CAST({R12_FIN_SUPPLY_EXPR} + {R12_FIN_TAX_EXPR} AS float)
             ELSE CAST({R12_FIN_SUPPLY_EXPR} + {R12_FIN_TAX_EXPR} AS float)
        END) AS total,
    COUNT_BIG(*) AS raw_rows
FROM dbo.Rddbc120 AS O WITH (NOLOCK)
WHERE O.Rd12_Out_YyMmDd >= %(date_from)s
  AND O.Rd12_Out_YyMmDd <= %(date_to)s
  AND LEFT(LTRIM(RTRIM(O.Rd12_Io_Gu)), 1) IN ('7', '8', '9')
  AND O.Rd12_Ven_Cd >= '50000'
  AND O.Rd12_Ven_Cd <= '8ZZZZ'
GROUP BY LEFT(LTRIM(RTRIM(O.Rd12_Io_Gu)), 1)
"""
    return _timed_df(sql, {"date_from": date_from, "date_to": date_to})


def _query_unlinked_docs(date_from: str, date_to: str) -> tuple[pd.DataFrame, float]:
    sql = """
SELECT TOP (30)
    H.Rd13_Trans_YyMmDd AS trans_date,
    LTRIM(RTRIM(H.Rd13_Ven_Cd)) AS customer_cd,
    H.Rd13_Trans_Seq AS trans_seq,
    CAST(COALESCE(H.Rd13_Supply_Price, 0) AS float) AS header_supply,
    CAST(COALESCE(H.Rd13_Tax_Price, 0) AS float) AS header_tax,
    CAST(COALESCE(H.Rd13_Supply_Price, 0) + COALESCE(H.Rd13_Tax_Price, 0) AS float) AS header_total
FROM dbo.Rddbc130 AS H WITH (NOLOCK)
WHERE H.Rd13_Trans_YyMmDd >= %(date_from)s
  AND H.Rd13_Trans_YyMmDd <= %(date_to)s
  AND H.Rd13_Trans_Di = '3'
  AND H.Rd13_Ven_Cd >= '50000'
  AND H.Rd13_Ven_Cd <= '8ZZZZ'
  AND NOT EXISTS (
      SELECT 1
      FROM dbo.Rddbc120 AS O WITH (NOLOCK)
      WHERE O.Rd12_Trans_Di = H.Rd13_Trans_Di
        AND O.Rd12_Trans_YyMmDd = H.Rd13_Trans_YyMmDd
        AND O.Rd12_Ven_Cd = H.Rd13_Ven_Cd
        AND O.Rd12_Trans_Seq = H.Rd13_Trans_Seq
  )
ORDER BY ABS(COALESCE(H.Rd13_Supply_Price, 0) + COALESCE(H.Rd13_Tax_Price, 0)) DESC
"""
    return _timed_df(sql, {"date_from": date_from, "date_to": date_to})


def _query_doc_mismatch(date_from: str, date_to: str) -> tuple[pd.DataFrame, float]:
    sql = f"""
WITH detail_sum AS (
    SELECT
        O.Rd12_Trans_Di AS trans_di,
        O.Rd12_Trans_YyMmDd AS trans_date,
        O.Rd12_Ven_Cd AS customer_cd,
        O.Rd12_Trans_Seq AS trans_seq,
        SUM(CAST({R12_FIN_SUPPLY_EXPR} AS float)) AS detail_supply,
        SUM(CAST({R12_FIN_TAX_EXPR} AS float)) AS detail_tax,
        SUM(CAST({R12_FIN_SUPPLY_EXPR} + {R12_FIN_TAX_EXPR} AS float)) AS detail_total,
        COUNT_BIG(*) AS detail_rows
    FROM dbo.Rddbc120 AS O WITH (NOLOCK)
    WHERE NULLIF(LTRIM(RTRIM(O.Rd12_Trans_Seq)), '') IS NOT NULL
    GROUP BY O.Rd12_Trans_Di, O.Rd12_Trans_YyMmDd, O.Rd12_Ven_Cd, O.Rd12_Trans_Seq
)
SELECT TOP (30)
    H.Rd13_Trans_YyMmDd AS trans_date,
    LTRIM(RTRIM(H.Rd13_Ven_Cd)) AS customer_cd,
    H.Rd13_Trans_Seq AS trans_seq,
    CAST(COALESCE(H.Rd13_Supply_Price, 0) AS float) AS header_supply,
    CAST(COALESCE(H.Rd13_Tax_Price, 0) AS float) AS header_tax,
    CAST(COALESCE(H.Rd13_Supply_Price, 0) + COALESCE(H.Rd13_Tax_Price, 0) AS float) AS header_total,
    CAST(COALESCE(D.detail_supply, 0) AS float) AS detail_supply,
    CAST(COALESCE(D.detail_tax, 0) AS float) AS detail_tax,
    CAST(COALESCE(D.detail_total, 0) AS float) AS detail_total,
    CAST(COALESCE(H.Rd13_Supply_Price, 0) - COALESCE(D.detail_supply, 0) AS float) AS diff_supply,
    CAST(COALESCE(H.Rd13_Tax_Price, 0) - COALESCE(D.detail_tax, 0) AS float) AS diff_tax,
    CAST(COALESCE(H.Rd13_Supply_Price, 0) + COALESCE(H.Rd13_Tax_Price, 0) - COALESCE(D.detail_total, 0) AS float) AS diff_total,
    ABS(CAST(COALESCE(H.Rd13_Supply_Price, 0) + COALESCE(H.Rd13_Tax_Price, 0) - COALESCE(D.detail_total, 0) AS float)) AS abs_diff_total,
    COALESCE(D.detail_rows, 0) AS detail_rows
FROM dbo.Rddbc130 AS H WITH (NOLOCK)
LEFT JOIN detail_sum AS D
  ON D.trans_di = H.Rd13_Trans_Di
 AND D.trans_date = H.Rd13_Trans_YyMmDd
 AND D.customer_cd = H.Rd13_Ven_Cd
 AND D.trans_seq = H.Rd13_Trans_Seq
WHERE H.Rd13_Trans_YyMmDd >= %(date_from)s
  AND H.Rd13_Trans_YyMmDd <= %(date_to)s
  AND H.Rd13_Trans_Di = '3'
  AND H.Rd13_Ven_Cd >= '50000'
  AND H.Rd13_Ven_Cd <= '8ZZZZ'
  AND (
      D.trans_seq IS NULL
      OR COALESCE(H.Rd13_Supply_Price, 0) <> COALESCE(D.detail_supply, 0)
      OR COALESCE(H.Rd13_Tax_Price, 0) <> COALESCE(D.detail_tax, 0)
  )
ORDER BY abs_diff_total DESC
"""
    return _timed_df(sql, {"date_from": date_from, "date_to": date_to})


def _query_link_summary(date_from: str, date_to: str) -> tuple[pd.DataFrame, float]:
    sql = """
WITH headers AS (
    SELECT
        H.Rd13_Trans_Di,
        H.Rd13_Trans_YyMmDd,
        H.Rd13_Ven_Cd,
        H.Rd13_Trans_Seq
    FROM dbo.Rddbc130 AS H WITH (NOLOCK)
    WHERE H.Rd13_Trans_YyMmDd >= %(date_from)s
      AND H.Rd13_Trans_YyMmDd <= %(date_to)s
      AND H.Rd13_Trans_Di = '3'
      AND H.Rd13_Ven_Cd >= '50000'
      AND H.Rd13_Ven_Cd <= '8ZZZZ'
),
linked AS (
    SELECT DISTINCT
        H.Rd13_Trans_Di,
        H.Rd13_Trans_YyMmDd,
        H.Rd13_Ven_Cd,
        H.Rd13_Trans_Seq
    FROM headers AS H
    JOIN dbo.Rddbc120 AS O WITH (NOLOCK)
      ON O.Rd12_Trans_Di = H.Rd13_Trans_Di
     AND O.Rd12_Trans_YyMmDd = H.Rd13_Trans_YyMmDd
     AND O.Rd12_Ven_Cd = H.Rd13_Ven_Cd
     AND O.Rd12_Trans_Seq = H.Rd13_Trans_Seq
)
SELECT
    (SELECT COUNT_BIG(*) FROM headers) AS header_docs,
    (SELECT COUNT_BIG(*) FROM linked) AS linked_docs,
    (SELECT COUNT_BIG(*) FROM headers) - (SELECT COUNT_BIG(*) FROM linked) AS unlinked_docs
"""
    return _timed_df(sql, {"date_from": date_from, "date_to": date_to})


def _concat_sources(parts: list[SourceResult], name: str) -> SourceResult:
    if not parts:
        return SourceResult(name, _empty_grouped(), 0.0)
    frames = [p.df for p in parts if isinstance(p.df, pd.DataFrame) and not p.df.empty]
    if not frames:
        return SourceResult(name, _empty_grouped(), sum(p.elapsed for p in parts))
    df = pd.concat(frames, ignore_index=True)
    grouped = (
        df.groupby(["month", "customer_cd"], as_index=False)
        .agg(supply=("supply", "sum"), tax=("tax", "sum"), total=("total", "sum"), raw_rows=("raw_rows", "sum"))
    )
    return SourceResult(name, grouped, sum(p.elapsed for p in parts))


def _overall(result: SourceResult) -> dict[str, Any]:
    df = result.df
    return {
        "source": result.name,
        "supply": float(df["supply"].sum()) if not df.empty else 0.0,
        "tax": float(df["tax"].sum()) if not df.empty else 0.0,
        "total": float(df["total"].sum()) if not df.empty else 0.0,
        "customer_count": int(df["customer_cd"].nunique()) if not df.empty else 0,
        "raw_rows": int(df["raw_rows"].sum()) if not df.empty else 0,
        "grouped_rows": int(len(df)),
        "elapsed": result.elapsed,
    }


def _compare(left: SourceResult, right: SourceResult, label: str) -> dict[str, Any]:
    l = left.df.rename(columns={"supply": "supply_l", "tax": "tax_l", "total": "total_l", "raw_rows": "raw_rows_l"})
    r = right.df.rename(columns={"supply": "supply_r", "tax": "tax_r", "total": "total_r", "raw_rows": "raw_rows_r"})
    merged = l.merge(r, on=["month", "customer_cd"], how="outer")
    for col in ["supply_l", "tax_l", "total_l", "raw_rows_l", "supply_r", "tax_r", "total_r", "raw_rows_r"]:
        merged[col] = pd.to_numeric(merged[col], errors="coerce").fillna(0)
    merged["customer_cd"] = merged["customer_cd"].fillna("")
    merged["month"] = merged["month"].fillna("")
    merged["diff_supply"] = merged["supply_l"] - merged["supply_r"]
    merged["diff_tax"] = merged["tax_l"] - merged["tax_r"]
    merged["diff_total"] = merged["total_l"] - merged["total_r"]
    merged["abs_diff_total"] = merged["diff_total"].abs()
    left_total = float(merged["total_l"].sum())
    right_total = float(merged["total_r"].sum())
    diff_total = left_total - right_total
    diff_ratio = diff_total / right_total * 100 if abs(right_total) >= 1e-12 else 0.0
    month = (
        merged.groupby("month", as_index=False)
        .agg(left_total=("total_l", "sum"), right_total=("total_r", "sum"), diff_total=("diff_total", "sum"))
        .sort_values("month")
    )
    month["diff_ratio_pct"] = month.apply(
        lambda row: (row["diff_total"] / row["right_total"] * 100) if abs(row["right_total"]) >= 1e-12 else 0.0,
        axis=1,
    )
    left_customers = set(left.df["customer_cd"].astype(str).tolist())
    right_customers = set(right.df["customer_cd"].astype(str).tolist())
    top = merged.sort_values("abs_diff_total", ascending=False).head(30)
    return {
        "label": label,
        "left": left.name,
        "right": right.name,
        "left_total": left_total,
        "right_total": right_total,
        "diff_total": diff_total,
        "diff_ratio_pct": diff_ratio,
        "month": month,
        "top": top,
        "left_only_customers": sorted(left_customers - right_customers),
        "right_only_customers": sorted(right_customers - left_customers),
    }


def _print_source_table(title: str, sources: list[SourceResult]) -> None:
    print(f"\n## {title} - 자료원 전체 금액")
    print("|자료원|공급가액|세액|합계금액|매출처수|원천행 수|집계행 수|조회시간(s)|")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    for src in sources:
        row = _overall(src)
        print(
            f"|{row['source']}|{_fmt_int(row['supply'])}|{_fmt_int(row['tax'])}|{_fmt_int(row['total'])}|"
            f"{row['customer_count']:,}|{row['raw_rows']:,}|{row['grouped_rows']:,}|{row['elapsed']:.3f}|"
        )


def _print_compare(comp: dict[str, Any]) -> None:
    print(f"\n### {comp['label']} 비교: {comp['left']} - {comp['right']}")
    print(f"- 전체 차이액: {_fmt_int(comp['diff_total'])} / 전체 차이율: {_fmt_float(comp['diff_ratio_pct'])}%")
    print("\n월별 비교")
    print("|월|left 합계|right 합계|차이액|차이율|")
    print("|---|---:|---:|---:|---:|")
    for _, row in comp["month"].iterrows():
        print(
            f"|{row['month']}|{_fmt_int(row['left_total'])}|{_fmt_int(row['right_total'])}|"
            f"{_fmt_int(row['diff_total'])}|{_fmt_float(row['diff_ratio_pct'])}%|"
        )

    print("\n매출처별 불일치 TOP 30")
    print("|월|매출처코드|left 합계|right 합계|차이액|절대차이|")
    print("|---|---|---:|---:|---:|---:|")
    top = comp["top"]
    if top.empty:
        print("|-|없음|0|0|0|0|")
    else:
        for _, row in top.iterrows():
            print(
                f"|{row['month']}|{row['customer_cd']}|{_fmt_int(row['total_l'])}|{_fmt_int(row['total_r'])}|"
                f"{_fmt_int(row['diff_total'])}|{_fmt_int(row['abs_diff_total'])}|"
            )

    left_only = comp["left_only_customers"][:30]
    right_only = comp["right_only_customers"][:30]
    print("\n누락 매출처")
    print(
        f"- {comp['left']}에만 존재: {len(comp['left_only_customers']):,}건"
        + (f" / 예: {', '.join(left_only)}" if left_only else "")
    )
    print(
        f"- {comp['right']}에만 존재: {len(comp['right_only_customers']):,}건"
        + (f" / 예: {', '.join(right_only)}" if right_only else "")
    )


def _print_df(title: str, df: pd.DataFrame, columns: list[str] | None = None) -> None:
    print(f"\n## {title}")
    if df.empty:
        print("- 없음")
        return
    out = df.copy()
    if columns:
        out = out[[c for c in columns if c in out.columns]]
    out = out.head(30)
    headers = [str(c) for c in out.columns]
    print("|" + "|".join(headers) + "|")
    print("|" + "|".join("---" for _ in headers) + "|")
    for _, row in out.iterrows():
        values: list[str] = []
        for col in out.columns:
            value = row[col]
            if pd.isna(value):
                values.append("")
            elif isinstance(value, (int, float)) or str(type(value)).find("numpy") >= 0:
                try:
                    values.append(_fmt_int(value))
                except Exception:
                    values.append(str(value))
            else:
                values.append(str(value))
        print("|" + "|".join(v.replace("|", "/") for v in values) + "|")


def _print_io_impact(title: str, df: pd.DataFrame, elapsed: float) -> None:
    print(f"\n## {title} - 7/8/9 계열 영향 (출고일 기준, 조회시간 {elapsed:.3f}s)")
    if df.empty:
        print("- 7/8/9 계열 금액 없음")
        return
    print("|Io_Gu 첫자리|공급가액|세액|합계금액|원천행 수|")
    print("|---|---:|---:|---:|---:|")
    for _, row in df.sort_values("io_prefix").iterrows():
        print(
            f"|{row['io_prefix']}|{_fmt_int(row['supply'])}|{_fmt_int(row['tax'])}|"
            f"{_fmt_int(row['total'])}|{int(row['raw_rows']):,}|"
        )


def _table_exists(table_name: str) -> bool:
    df = _safe_read_df(
        "SELECT CASE WHEN OBJECT_ID(?, 'U') IS NULL THEN 0 ELSE 1 END AS exists_yn",
        (f"dbo.{table_name}",),
    )
    if df.empty:
        return False
    return bool(int(df.iloc[0]["exists_yn"]))


def _run_diagnostics(company_id: int) -> DiagnosticsResult:
    print("## 연결 진단")
    print(f"- company_id: {company_id}")
    try:
        cfg = get_company_db_config(int(company_id))
        server_alias = _mask_server(cfg.db_server, cfg.db_port)
        print(f"- A. 회사 등록정보 조회: OK / 회사명={cfg.company_name}")
        print("- B. 접속정보 복호화: OK")
        print(f"- DB명: {cfg.db_name}")
        print(f"- 서버: {server_alias}")
        print(f"- ODBC Driver: {cfg.db_driver}")
        print("- Encrypt: Yes")
        print("- TrustServerCertificate: Yes")
        print("- Authentication: SqlPassword")
    except Exception as exc:
        print("- 실패 단계: 회사 등록정보 조회 또는 복호화")
        print(f"- {_safe_error_message('회사 DB 설정 로드 실패', exc)}")
        return DiagnosticsResult(ok=False)

    try:
        set_current_company_id(int(company_id))
        select_one = _safe_read_df("SELECT 1 AS ok")
        ok_value = int(select_one.iloc[0]["ok"]) if not select_one.empty else 0
        print(f"- C/D. ODBC 연결 및 SELECT 1: OK / result={ok_value}")

        db_df = _safe_read_df("SELECT DB_NAME() AS db_name")
        db_name = str(db_df.iloc[0]["db_name"]) if not db_df.empty else ""
        print(f"- E. SELECT DB_NAME(): OK / db_name={db_name}")

        for table in ("Rddbc260", "Rddbc120", "Rddbc130"):
            print(f"- {'FGH'[('Rddbc260','Rddbc120','Rddbc130').index(table)]}. {table} 존재 확인: {'OK' if _table_exists(table) else '없음'}")

        print("- 사용 Rddbc130 금액 필드: Rd13_Supply_Price, Rd13_Tax_Price, Rd13_Tot_Amt")
        print("- 사용 Rddbc120 확정금액 필드: COALESCE(Rd12_Fin_Supply_Price, Rd12_Supply_Price), COALESCE(Rd12_Fin_Tax_Price, Rd12_Tax_Price)")

        return DiagnosticsResult(
            ok=True,
            company_name=cfg.company_name,
            db_name=db_name,
            driver=cfg.db_driver,
            server_alias=server_alias,
        )
    except Exception as exc:
        print("- 실패 단계: ODBC 연결 또는 진단 SELECT")
        print(f"- {_safe_error_message('회사 DB 연결 진단 실패', exc)}")
        return DiagnosticsResult(ok=False, company_name=cfg.company_name, db_name=cfg.db_name, driver=cfg.db_driver, server_alias=server_alias)


def _run_period(title: str, date_from: str, date_to: str, month_from: str | None, month_to: str | None) -> dict[str, Any]:
    sources: list[SourceResult] = []
    if month_from and month_to:
        sources.append(_query_rddbc260(month_from, month_to))
    r130 = _query_rddbc130_trans(date_from, date_to)
    r120_linked = _query_rddbc120_linked_trans(date_from, date_to)
    r120_out_book = _query_rddbc120_out_date(date_from, date_to, ("5", "6", "7", "9"), "Rddbc120(출고일 장부 5/6/7/9)")
    r120_out_real = _query_rddbc120_out_date(date_from, date_to, ("5", "6", "8", "9"), "Rddbc120(출고일 실 5/6/8/9)")
    sources.extend([r130, r120_linked, r120_out_book, r120_out_real])

    _print_source_table(title, sources)
    if month_from and month_to:
        _print_compare(_compare(sources[0], r130, f"{title} Rddbc260 vs Rddbc130"))
    _print_compare(_compare(r130, r120_linked, f"{title} Rddbc130 vs 연결 Rddbc120"))
    _print_compare(_compare(r120_out_book, r130, f"{title} 출고일 장부 기준 vs 거래명세서일 기준"))

    link_df, link_elapsed = _query_link_summary(date_from, date_to)
    _print_df(f"{title} Rddbc120↔Rddbc130 연결 문서 수 (조회시간 {link_elapsed:.3f}s)", link_df)
    unlinked_df, unlinked_elapsed = _query_unlinked_docs(date_from, date_to)
    _print_df(f"{title} Rddbc130과 Rddbc120이 연결되지 않는 문서 TOP 30 (조회시간 {unlinked_elapsed:.3f}s)", unlinked_df)
    mismatch_df, mismatch_elapsed = _query_doc_mismatch(date_from, date_to)
    _print_df(f"{title} 거래명세서 문서별 금액 불일치 TOP 30 (조회시간 {mismatch_elapsed:.3f}s)", mismatch_df)
    impact_df, impact_elapsed = _query_io_prefix_impact(date_from, date_to)
    _print_io_impact(title, impact_df, impact_elapsed)

    return {
        "r130": r130,
        "r120_linked": r120_linked,
        "r120_out_book": r120_out_book,
        "link_df": link_df,
        "link_elapsed": link_elapsed,
        "unlinked_df": unlinked_df,
        "mismatch_df": mismatch_df,
        "impact_df": impact_df,
    }


def run(company_id: int, completed_month: str, current_date_to: str, historical_date_to: str) -> None:
    completed_month = _yyyymm(completed_month)
    current_date_to = _yyyymmdd(current_date_to)
    historical_date_to = _yyyymmdd(historical_date_to)
    current_month = current_date_to[:6]
    historical_month = historical_date_to[:6]
    historical_completed_to = _prev_month(historical_month)
    historical_start_month = "202601"
    historical_start_date = f"{historical_start_month}01"

    print("# 매출처별 매출 예상 자료원 운영 DB 비교")
    print("- 접속정보/비밀번호/전체 connection string은 출력하지 않습니다.")
    print("- 앱 공식 연결 경로: set_current_company_id(company_id) -> app.db.mssql_client.read_df/query_to_df")
    print("- Rddbc130 출고 거래명세서 기준: Rd13_Trans_Di='3'")

    diag = _run_diagnostics(company_id)
    if not diag.ok:
        raise RuntimeError("연결 진단 실패로 비교 SELECT를 실행하지 않았습니다.")

    print("\n## 비교 조건")
    print(f"- 완료월: {completed_month} ({_month_start(completed_month)}~{_month_end(completed_month)})")
    print(f"- 현재월: {current_month} ({_month_start(current_month)}~{current_date_to})")
    print(f"- 과거 월중: {historical_start_date}~{historical_date_to}")

    completed = _run_period(
        f"A. 완료월 {completed_month}",
        _month_start(completed_month),
        _month_end(completed_month),
        completed_month,
        completed_month,
    )
    current = _run_period(
        f"B. 현재월 {_month_start(current_month)}~{current_date_to}",
        _month_start(current_month),
        current_date_to,
        current_month,
        current_month,
    )

    c260_completed = _query_rddbc260(historical_start_month, historical_completed_to)
    c130_eval = _query_rddbc130_trans(_month_start(historical_month), historical_date_to)
    c_hybrid = _concat_sources([c260_completed, c130_eval], "Rddbc260완료월+Rddbc130평가월")
    c120_linked_eval = _query_rddbc120_linked_trans(_month_start(historical_month), historical_date_to)
    c120_out_book = _query_rddbc120_out_date(historical_start_date, historical_date_to, ("5", "6", "7", "9"), "Rddbc120(출고일 장부 5/6/7/9)")
    _print_source_table(f"C. 과거 월중 {historical_start_date}~{historical_date_to}", [c_hybrid, c120_out_book, c130_eval, c120_linked_eval])
    _print_compare(_compare(c_hybrid, c120_out_book, "C 과거 월중 hybrid vs 출고일 장부 기준"))
    _print_compare(_compare(c130_eval, c120_linked_eval, "C 평가월 Rddbc130 vs 연결 Rddbc120"))

    print("\n## 과거 월중 평가월 중복 확인")
    print(f"- 완료월 Rddbc260 범위: {historical_start_month}~{historical_completed_to}")
    print(f"- 평가월 Rddbc130 범위: {_month_start(historical_month)}~{historical_date_to}")
    print("- 평가월 Rddbc260은 hybrid에 합산하지 않았습니다.")

    print("\n## 차이가 발생하는 가능한 업무 원인")
    print("- Rddbc260은 월 장부매출 집계 기준이고 Rddbc130은 출고 거래명세서 헤더 기준이라 월마감/문서 확정 시점 차이가 있을 수 있습니다.")
    print("- Rddbc130과 연결 Rddbc120의 차이는 거래명세서 헤더 금액과 상세 확정금액 합계의 불일치 또는 미연결 문서에서 발생할 수 있습니다.")
    print("- 출고일 기준 Rddbc120은 Rd12_Out_YyMmDd 기준이고 공식 장부매출 비교는 Rd13_Trans_YyMmDd 기준이므로 월 경계에서 차이가 날 수 있습니다.")
    print("- 7계열 장부출고, 8계열 미결출고, 9계열 기타출고 포함 여부가 월집계와 출고상세 비교 차이를 만들 수 있습니다.")

    comp_260_130 = _compare(_query_rddbc260(completed_month, completed_month), completed["r130"], "완료월 Rddbc260 vs Rddbc130")
    comp_130_120 = _compare(completed["r130"], completed["r120_linked"], "완료월 Rddbc130 vs 연결 Rddbc120")
    print("\n## Rddbc260 운영 자료원 채택 권고")
    print(f"- 완료월 Rddbc260 vs Rddbc130 차이: {_fmt_int(comp_260_130['diff_total'])} / {_fmt_float(comp_260_130['diff_ratio_pct'])}%")
    print(f"- 완료월 Rddbc130 vs 연결 Rddbc120 차이: {_fmt_int(comp_130_120['diff_total'])} / {_fmt_float(comp_130_120['diff_ratio_pct'])}%")
    if abs(comp_260_130["diff_total"]) <= max(abs(comp_260_130["right_total"]) * 0.001, 1):
        print("- Rddbc260과 Rddbc130 차이가 0.1% 이내입니다. 월 장부매출 고속 자료원으로 Rddbc260 채택을 검토할 수 있습니다.")
    else:
        print("- Rddbc260과 Rddbc130 차이가 0.1%를 초과합니다. 문서별 불일치와 월 경계/입출고구분 영향을 확인한 뒤 채택 여부를 결정해야 합니다.")

    _ = current  # 현재월 결과는 운영 데이터 변동 가능성이 있으므로 출력 비교만 사용한다.


def main() -> int:
    parser = argparse.ArgumentParser(description="매출처별 매출 예상 자료원 후보 운영 DB 비교")
    parser.add_argument("--company-id", required=True, type=int, help="SSAI_COMPANIES.company_id")
    parser.add_argument("--completed-month", default="202606", help="완료월 비교 YYYYMM")
    parser.add_argument("--current-date-to", default=date.today().strftime("%Y%m%d"), help="현재월 비교 종료일 YYYYMMDD")
    parser.add_argument("--historical-date-to", default="20260702", help="과거 월중 비교 종료일 YYYYMMDD")
    args = parser.parse_args()
    try:
        run(
            company_id=args.company_id,
            completed_month=args.completed_month,
            current_date_to=args.current_date_to,
            historical_date_to=args.historical_date_to,
        )
        return 0
    except RuntimeError as exc:
        print("\n## 실행 실패")
        print(f"- {exc}")
        print("- 비밀번호와 전체 connection string은 출력하지 않았습니다.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
