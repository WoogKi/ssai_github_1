from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.mssql_client import get_current_company_id, get_engine, set_current_company_id  # noqa: E402
from app.db.sql_utils import sql_safe_int  # noqa: E402
from app.services.dashboard_inventory_frequency_snapshot_service import (  # noqa: E402
    _aggregate_event_grain_chunks,
    build_frequency_snapshot_plan,
    outbound_base_rows_sql,
    outbound_event_grain_stream_sql,
    outbound_monthly_aggregate_sql,
)


def _classified_ctes(base_sql: str) -> str:
    safe_tcode = sql_safe_int("io_tcode")
    return f"""WITH BaseRows AS (
    {base_sql}
), Classified AS (
    SELECT *,
        CASE WHEN io_gcode = '0012' AND LEN(io_tcode) = 3
                  AND io_tcode NOT LIKE '%[^0-9]%'
                  AND {safe_tcode} BETWEEN 500 AND 599 THEN 1 ELSE 0 END AS is_normal,
        CASE WHEN io_gcode = '0012' AND LEN(io_tcode) = 3
                  AND io_tcode NOT LIKE '%[^0-9]%'
                  AND {safe_tcode} BETWEEN 600 AND 699 THEN 1 ELSE 0 END AS is_return
    FROM BaseRows
)"""


def _event_ctes(base_sql: str) -> str:
    return _classified_ctes(base_sql) + """,
NormalPositive AS (
    SELECT * FROM Classified WHERE is_normal = 1 AND outbound_quantity > 0
), ExactRows AS (
    SELECT outbound_date, vendor_code, outbound_seq, product_code, stock_code, outbound_quantity,
           COUNT_BIG(*) AS exact_duplicate_count
    FROM NormalPositive
    WHERE NULLIF(outbound_date, '') IS NOT NULL AND NULLIF(vendor_code, '') IS NOT NULL
      AND NULLIF(outbound_seq, '') IS NOT NULL AND NULLIF(product_code, '') IS NOT NULL
      AND NULLIF(stock_code, '') IS NOT NULL
      AND outbound_quantity = FLOOR(outbound_quantity)
    GROUP BY outbound_date, vendor_code, outbound_seq, product_code, stock_code, outbound_quantity
), EventGrain AS (
    SELECT outbound_date, vendor_code, outbound_seq,
           COUNT_BIG(*) AS mapping_count,
           MAX(product_code) AS product_code,
           MAX(stock_code) AS stock_code,
           MAX(outbound_quantity) AS outbound_quantity,
           SUM(exact_duplicate_count - 1) AS exact_duplicate_row_count
    FROM ExactRows
    GROUP BY outbound_date, vendor_code, outbound_seq
)"""


def _stage_queries(base_sql: str) -> tuple[tuple[str, str], ...]:
    classified = _classified_ctes(base_sql)
    events = _event_ctes(base_sql)
    exact = events.rsplit(", EventGrain AS", 1)[0]
    return (
        ("base_rows", f"WITH BaseRows AS ({base_sql}) SELECT COUNT_BIG(*) AS row_count FROM BaseRows"),
        ("classified", classified + """
SELECT COUNT_BIG(*) AS row_count,
       COALESCE(SUM(CASE WHEN is_normal = 1 THEN 1 ELSE 0 END), 0) AS normal_row_count,
       COALESCE(SUM(CASE WHEN is_return = 1 THEN 1 ELSE 0 END), 0) AS return_row_count
FROM Classified"""),
        ("normal_positive_validation", classified + """
SELECT COUNT_BIG(*) AS normal_positive_row_count,
       COALESCE(SUM(CASE WHEN NULLIF(outbound_date, '') IS NOT NULL AND NULLIF(vendor_code, '') IS NOT NULL
           AND NULLIF(outbound_seq, '') IS NOT NULL AND NULLIF(product_code, '') IS NOT NULL
           AND NULLIF(stock_code, '') IS NOT NULL AND outbound_quantity = FLOOR(outbound_quantity)
           THEN 1 ELSE 0 END), 0) AS valid_row_count
FROM Classified
WHERE is_normal = 1 AND outbound_quantity > 0"""),
        ("exact_row_group", exact + """
SELECT COUNT_BIG(*) AS exact_row_count,
       COALESCE(SUM(exact_duplicate_count - 1), 0) AS duplicate_row_count
FROM ExactRows"""),
        ("event_grain", events + """
SELECT COUNT_BIG(*) AS event_key_count,
       COALESCE(SUM(CASE WHEN mapping_count = 1 THEN 1 ELSE 0 END), 0) AS accepted_row_count,
       COALESCE(SUM(CASE WHEN mapping_count > 1 THEN 1 ELSE 0 END), 0) AS conflicting_event_count
FROM EventGrain"""),
        ("monthly_day_rollup", events + """,
AcceptedEvents AS (
    SELECT outbound_date, product_code, stock_code, outbound_quantity
    FROM EventGrain
    WHERE mapping_count = 1
), MonthlyDays AS (
    SELECT LEFT(outbound_date, 6) AS [month], product_code, stock_code, outbound_date,
           COUNT_BIG(*) AS occurrence_count, SUM(outbound_quantity) AS outbound_quantity
    FROM AcceptedEvents
    GROUP BY LEFT(outbound_date, 6), product_code, stock_code, outbound_date
), Monthly AS (
    SELECT [month], product_code, stock_code, SUM(occurrence_count) AS occurrence_count,
           SUM(outbound_quantity) AS outbound_quantity, COUNT_BIG(*) AS outbound_day_count
    FROM MonthlyDays
    GROUP BY [month], product_code, stock_code
)
SELECT COUNT_BIG(*) AS monthly_row_count, COALESCE(SUM(occurrence_count), 0) AS accepted_row_count,
       COALESCE(SUM(outbound_quantity), 0) AS outbound_quantity_sum
FROM Monthly"""),
    )

def _as_json_row(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {}
    return {str(key): _json_value(value) for key, value in frame.iloc[0].to_dict().items()}


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "item"):
        return value.item()
    return str(value)


def profile(*, company_id: int, evaluation_month: str, stock_codes: list[str], timeout_seconds: int, candidate_only: bool = False, event_stream_only: bool = False) -> dict[str, Any]:
    plan = build_frequency_snapshot_plan(
        company_id=company_id,
        evaluation_month=evaluation_month,
        stock_codes=stock_codes,
    )
    base_sql, binds = outbound_base_rows_sql(plan)
    result: dict[str, Any] = {
        "company_id": plan.company_id,
        "evaluation_month": plan.evaluation_month,
        "stock_codes": list(plan.stock_codes),
        "basis_from": plan.basis_from,
        "basis_to": plan.basis_to,
        "timeout_seconds_each": timeout_seconds,
        "retry_count": 0,
        "mode": "read_only_event_stream" if event_stream_only else ("read_only_candidate_aggregate" if candidate_only else "read_only_stage_profile"),
        "stages": [],
    }
    previous_company_id = get_current_company_id()
    set_current_company_id(plan.company_id)
    started = time.perf_counter()
    try:
        with get_engine().connect() as conn:
            raw = getattr(conn.connection, "driver_connection", conn.connection)
            if hasattr(raw, "timeout"):
                raw.timeout = max(1, int(timeout_seconds))
            result["connection_status"] = "ok"
            if event_stream_only:
                stream_sql, stream_binds = outbound_event_grain_stream_sql(plan)
                stage_started = time.perf_counter()
                entry: dict[str, Any] = {"stage": "event_grain_stream"}
                try:
                    chunks = pd.read_sql_query(text(stream_sql), conn, params=stream_binds, chunksize=50000)
                    monthly_rows, diagnostics = _aggregate_event_grain_chunks(chunks)
                    entry.update(
                        status="ok",
                        elapsed_seconds=round(time.perf_counter() - stage_started, 3),
                        metrics={"monthly_row_count": len(monthly_rows), **diagnostics},
                    )
                except Exception as exc:
                    entry.update(status="failed", elapsed_seconds=round(time.perf_counter() - stage_started, 3), error_type=type(exc).__name__)
                result["stages"].append(entry)
                return result
            stage_queries = [] if candidate_only else list(_stage_queries(base_sql))
            aggregate_sql, _aggregate_binds = outbound_monthly_aggregate_sql(plan)
            stage_queries.append(("candidate_aggregate", aggregate_sql))
            for name, sql in stage_queries:
                stage_started = time.perf_counter()
                entry: dict[str, Any] = {"stage": name}
                try:
                    frame = pd.read_sql_query(text(sql), conn, params=binds)
                    entry.update(
                        status="ok",
                        elapsed_seconds=round(time.perf_counter() - stage_started, 3),
                        metrics=_as_json_row(frame),
                    )
                except Exception as exc:
                    entry.update(
                        status="failed",
                        elapsed_seconds=round(time.perf_counter() - stage_started, 3),
                        error_type=type(exc).__name__,
                    )
                    result["stages"].append(entry)
                    break
                result["stages"].append(entry)
    except BaseException as exc:
        result["connection_status"] = "failed"
        result["connection_error_type"] = type(exc).__name__
    finally:
        set_current_company_id(previous_company_id)
        result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Rddbc120 snapshot aggregate stage profiler")
    parser.add_argument("--company-id", required=True, type=int)
    parser.add_argument("--evaluation-month", required=True)
    parser.add_argument("--stock-code", action="append", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate-only", action="store_true", help="run only the final candidate aggregate once")
    parser.add_argument("--event-stream-only", action="store_true", help="run the bounded event-stream candidate once")
    args = parser.parse_args()
    if args.candidate_only and args.event_stream_only:
        parser.error("--candidate-only and --event-stream-only are mutually exclusive")
    result = profile(
        company_id=args.company_id,
        evaluation_month=args.evaluation_month,
        stock_codes=list(args.stock_code),
        timeout_seconds=max(1, int(args.timeout_seconds)),
        candidate_only=bool(args.candidate_only),
        event_stream_only=bool(args.event_stream_only),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for item in result["stages"]:
        print("stage={stage} status={status} elapsed_seconds={elapsed_seconds}".format(**item))
    print("total_elapsed_seconds={:.3f}".format(result["elapsed_seconds"]))
    return 0 if result.get("connection_status") == "ok" and result["stages"] and all(item["status"] == "ok" for item in result["stages"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
