from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import event


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import mssql_client  # noqa: E402
from app.services import product_inventory_service  # noqa: E402


def _params(args: argparse.Namespace) -> dict[str, Any]:
    month = str(args.evaluation_month)
    return {
        "company_id": int(args.company_id),
        "evaluation_month": month,
        "frequency_grade": "A",
        "date_from": f"{month}01",
        "date_to": product_inventory_service._month_last(f"{month}01"),
        "month_from": month,
        "month_to": month,
        "stock_cds": list(args.stock_code),
    }


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    perf = dict((result.get("meta") or {}).get("product_inventory_perf") or {})
    prefilter = dict(perf.get("frequency_snapshot_prefilter") or {})
    display = result.get("df_display")
    return {
        "result_status": str((result.get("meta") or {}).get("result_status") or ""),
        "message": str(result.get("message") or ""),
        "result_rows": int(len(display)) if isinstance(display, pd.DataFrame) else 0,
        "columns": list(result.get("columns") or []),
        "source_elapsed_ms": perf.get("source_elapsed_ms"),
        "service_total_ms": perf.get("service_total_ms"),
        "sql": perf.get("sql") or {},
        "frequency_rows_after_filter": (perf.get("display") or {}).get("frequency_rows_after_filter"),
        "frequency_prefilter": {
            "applied": bool(prefilter.get("applied")),
            "grade": str(prefilter.get("frequency_grade") or ""),
            "product_code_count": int(prefilter.get("product_code_count") or 0),
            "safe_limit": int(prefilter.get("safe_limit") or 0),
            "snapshot_status": str(prefilter.get("snapshot_status") or ""),
            "fallback_reason": str(prefilter.get("fallback_reason") or ""),
        },
    }


def _assert_equal(full_scan: dict[str, Any], prefiltered: dict[str, Any]) -> None:
    for key in ("df", "df_display"):
        left = full_scan.get(key)
        right = prefiltered.get(key)
        if not isinstance(left, pd.DataFrame) or not isinstance(right, pd.DataFrame):
            raise AssertionError(f"{key} dataframe missing")
        pd.testing.assert_frame_equal(left, right, check_dtype=True, check_like=False)
    if full_scan.get("columns") != prefiltered.get("columns"):
        raise AssertionError("column order differs")


def _assert_source_calls(summary: dict[str, Any]) -> None:
    sql = dict(summary.get("sql") or {})
    if set(sql) != {"month_carry", "month_period", "last_cost"}:
        raise AssertionError(f"monthly source stages changed: {sorted(sql)}")
    for stage in ("month_carry", "month_period"):
        if int((sql.get(stage) or {}).get("rows") or 0) < 0:
            raise AssertionError(f"invalid {stage} row count")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only full-scan versus Snapshot-A product-inventory comparison.")
    parser.add_argument("--company-id", type=int, required=True)
    parser.add_argument("--evaluation-month", required=True)
    parser.add_argument("--stock-code", action="append", required=True)
    parser.add_argument("--query-timeout-seconds", type=int, default=120)
    args = parser.parse_args()

    month = str(args.evaluation_month)
    if len(month) != 6 or not month.isdigit():
        raise SystemExit("evaluation_month must be YYYYMM")
    if int(args.query_timeout_seconds) <= 0:
        raise SystemExit("query_timeout_seconds must be positive")

    mssql_client.set_current_company_id(int(args.company_id))
    engine = mssql_client.get_engine()

    def _set_timeout(_conn: Any, cursor: Any, _statement: str, _parameters: Any, _context: Any, _executemany: bool) -> None:
        cursor.timeout = int(args.query_timeout_seconds)

    original_prefilter = product_inventory_service._apply_product_inventory_frequency_snapshot_scope
    event.listen(engine, "before_cursor_execute", _set_timeout)
    try:
        def _disabled_prefilter(_params_in: dict[str, Any], _cfg: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            return {
                "requested": True,
                "applied": False,
                "frequency_grade": "A",
                "product_code_count": 0,
                "safe_limit": 0,
                "snapshot_status": "",
                "snapshot_reason": "",
                "fallback_reason": "live_full_scan_baseline",
            }

        product_inventory_service._apply_product_inventory_frequency_snapshot_scope = _disabled_prefilter
        baseline_started = time.perf_counter()
        full_scan = product_inventory_service.get_product_inventory_result(_params(args))
        baseline_elapsed_ms = round((time.perf_counter() - baseline_started) * 1000, 1)

        product_inventory_service._apply_product_inventory_frequency_snapshot_scope = original_prefilter
        prefilter_started = time.perf_counter()
        prefiltered = product_inventory_service.get_product_inventory_result(_params(args))
        prefilter_elapsed_ms = round((time.perf_counter() - prefilter_started) * 1000, 1)
    finally:
        product_inventory_service._apply_product_inventory_frequency_snapshot_scope = original_prefilter
        event.remove(engine, "before_cursor_execute", _set_timeout)
        mssql_client.set_current_company_id(None)

    baseline = _summary(full_scan)
    optimized = _summary(prefiltered)
    if baseline["result_status"] != "success" or optimized["result_status"] != "success":
        raise AssertionError(json.dumps({"full_scan": baseline, "prefilter": optimized}, ensure_ascii=False, default=str))
    _assert_equal(full_scan, prefiltered)
    _assert_source_calls(baseline)
    _assert_source_calls(optimized)
    if not optimized["frequency_prefilter"]["applied"]:
        raise AssertionError(f"Snapshot A prefilter was not applied: {optimized['frequency_prefilter']}")
    if optimized["frequency_prefilter"]["grade"] != "A":
        raise AssertionError("prefilter grade is not A")

    print(json.dumps({
        "ok": True,
        "company_id": int(args.company_id),
        "evaluation_month": month,
        "stock_code_count": len(args.stock_code),
        "query_timeout_seconds": int(args.query_timeout_seconds),
        "full_scan_elapsed_ms": baseline_elapsed_ms,
        "prefilter_elapsed_ms": prefilter_elapsed_ms,
        "full_scan": baseline,
        "prefilter": optimized,
    }, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
