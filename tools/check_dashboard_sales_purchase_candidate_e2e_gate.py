"""Prepare or execute the final read-only Dashboard sales candidate gate.

The production ``get_dashboard_sales_source_bundle`` route is intentionally not
used or changed here.  This tool composes the already-validated narrow stages:
numeric product-month sales, product identity, the minimal manufacturer/vendor
relation, and compact purchase month/diagnostic facts.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import event

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import mssql_client
from app.services.analytics_manufacturer_sales_trend_service import get_manufacturer_sales_trend_summary
from app.services.analytics_sales_trend_service import (
    DashboardNarrowSalesPurchaseBundle,
    adapt_dashboard_narrow_bundle_for_forecast,
    adapt_dashboard_narrow_bundle_for_manufacturer,
    adapt_dashboard_narrow_bundle_for_visual,
    build_dashboard_narrow_bundle_from_projections,
    get_sales_forecast_df,
    query_to_df,
)
from app.services.dashboard_lite_facts import (
    _build_demand_surge_history_by_product,
    _build_visual_phase2_summary,
)
from tools.check_dashboard_sales_purchase_db_multigrain_equality import dashboard_probe_contract
from tools.check_dashboard_sales_purchase_db_narrow_perf_probe import build_narrow_projection_sql
from tools.check_dashboard_sales_purchase_manufacturer_vendor_relation_probe import (
    build_manufacturer_month_from_narrow_projections,
    build_manufacturer_vendor_relation_sql,
)
from tools.check_dashboard_sales_purchase_sales_projection_stage_profiler import (
    assemble_sales_facts_from_stages,
    build_sales_projection_stage_sql,
)


def _write(path: Path | None, value: dict[str, Any]) -> None:
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _frame_metrics(frame: pd.DataFrame) -> dict[str, int]:
    return {
        "row_count": int(len(frame)),
        "column_count": int(len(frame.columns)),
        "pandas_deep_memory_bytes": int(frame.memory_usage(index=True, deep=True).sum()),
    }


def build_candidate_queries(params: dict[str, Any]) -> tuple[dict[str, tuple[str, dict[str, Any]]], dict[str, Any]]:
    """Return only the final candidate's compact projections.

    One logical Dashboard sales/purchase source remains one source contract.  It
    is deliberately four physical SELECTs because the split avoids the former
    wide GROUPING SETS aggregate and avoids row-level purchase/vendor payloads.
    """
    stages, stage_plan = build_sales_projection_stage_sql(params)
    relation_sql, relation_bind, relation_plan = build_manufacturer_vendor_relation_sql(params)
    narrow, narrow_plan = build_narrow_projection_sql(params)
    queries = {
        "product_month_sales": stages["product_month_sales"],
        "product_identity": stages["product_identity"],
        "manufacturer_vendor_relation": (relation_sql, relation_bind),
        "purchase_facts": narrow["purchase_facts"],
    }
    plan = {
        "logical_source_call_count": 1,
        "physical_query_count": len(queries),
        "projection_order": list(queries),
        "removed_from_previous_802434_row_candidate": [
            "wide_sales_facts_grouping_sets",
            "db_manufacturer_month_count_distinct",
            "purchase_product_month_row_level",
            "purchase_product_vendor_row_level",
            "manufacturer_vendor_row_level_return",
            "legacy_raw_sales_purchase_return",
        ],
        "stage_plan": stage_plan,
        "relation_plan": relation_plan,
        "purchase_plan": narrow_plan,
    }
    return queries, plan


def assemble_candidate_bundle(
    product_month_sales: pd.DataFrame,
    product_identity: pd.DataFrame,
    manufacturer_vendor_relation: pd.DataFrame,
    purchase_facts: pd.DataFrame,
) -> DashboardNarrowSalesPurchaseBundle:
    """Build the existing typed bundle from the final compact projections."""
    manufacturer_month = build_manufacturer_month_from_narrow_projections(
        product_month_sales, product_identity, manufacturer_vendor_relation
    )
    sales_facts = assemble_sales_facts_from_stages(
        product_month_sales, product_identity, manufacturer_month
    )
    return build_dashboard_narrow_bundle_from_projections(
        product_identity, sales_facts, purchase_facts
    )


def run_candidate_consumers(
    bundle: DashboardNarrowSalesPurchaseBundle,
    params: dict[str, Any],
) -> tuple[dict[str, int], dict[str, Any]]:
    """Run existing Python consumers only; no new business calculation exists here."""
    evaluation_month = str(params["evaluation_month"])
    history_from = str(params.get("dashboard_lite_history_month_from") or params["month_from"])
    timings: dict[str, int] = {}

    started = time.perf_counter()
    forecast = get_sales_forecast_df(params, raw_df=adapt_dashboard_narrow_bundle_for_forecast(bundle))
    timings["forecast_adapter_consumer_elapsed_ms"] = int((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    demand_history = _build_demand_surge_history_by_product(
        bundle.product_month_sales_df,
        evaluation_month=evaluation_month,
        history_month_from=history_from,
    )
    timings["demand_surge_adapter_consumer_elapsed_ms"] = int((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    manufacturer = get_manufacturer_sales_trend_summary(
        params, raw_df=adapt_dashboard_narrow_bundle_for_manufacturer(bundle)
    )
    timings["manufacturer_reconstruction_consumer_elapsed_ms"] = int((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    visual, trend = _build_visual_phase2_summary(
        [{"stock_cover_status": "ready", "주요매입처상태": "assigned"}],
        adapt_dashboard_narrow_bundle_for_visual(bundle),
        evaluation_remaining_days=1,
    )
    timings["purchase_consumer_elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    facts = {
        "forecast_rows": int(len(forecast)),
        "demand_surge_product_count": int(len(demand_history.get("by_product") or {})),
        "manufacturer_summary_rows": int(len(manufacturer)),
        "purchase_trend_points": int(visual.get("purchase_trend_points") or len(trend or [])),
        "purchase_diagnostics": dict(bundle.purchase_diagnostics),
        "sales_month_total_rows": int(len(bundle.sales_month_total_df)),
        "purchase_month_total_rows": int(len(bundle.purchase_month_total_df)),
    }
    return timings, facts


def main(output: Path | None, *, execute: bool, timeout_seconds: int) -> int:
    params, contract = dashboard_probe_contract()
    result: dict[str, Any] = {
        "status": "PREPARED",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "company_id": 8,
        "retry_count": 0,
        "statement_timeout_seconds": int(timeout_seconds),
        "source_call_count_contract": 3,
        "dashboard_contract": contract,
        "equality_boundary": {
            "same_source_manufacturer_reconstruction": "PASS_REFERENCED_GATE",
            "purchase_compact": "PASS_REFERENCED_GATE",
            "inbound_authority": "UNCHANGED_NOT_IN_CANDIDATE",
            "cross_time_production_comparison": "DRIFT_OR_UNPROVABLE_UNLESS_SAME_SOURCE",
        },
    }
    _write(output, result)
    try:
        queries, plan = build_candidate_queries(params)
        result["plan"] = plan
        if not execute:
            result.update({"actual_one_shot_ready": True, "finished_at": datetime.now().isoformat(timespec="seconds")})
            _write(output, result)
            return 0

        engine = mssql_client.get_engine()

        def _set_timeout(_conn: Any, cursor: Any, _statement: str, _parameters: Any, _context: Any, _executemany: bool) -> None:
            cursor.timeout = int(timeout_seconds)

        event.listen(engine, "before_cursor_execute", _set_timeout)
        try:
            mssql_client.set_current_company_id(8)
            source_started = time.perf_counter()
            frames: dict[str, pd.DataFrame] = {}
            details: dict[str, dict[str, int]] = {}
            for name, (sql, bind) in queries.items():
                started = time.perf_counter()
                frame = query_to_df(sql, bind)
                frame = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()
                frames[name] = frame
                details[name] = {"elapsed_ms": int((time.perf_counter() - started) * 1000), **_frame_metrics(frame)}
                result["projection_results"] = details
                _write(output, result)

            started = time.perf_counter()
            bundle = assemble_candidate_bundle(
                frames["product_month_sales"], frames["product_identity"],
                frames["manufacturer_vendor_relation"], frames["purchase_facts"],
            )
            assembly_ms = int((time.perf_counter() - started) * 1000)
            consumer_timings, facts = run_candidate_consumers(bundle, params)
            result.update({
                "status": "EXECUTED",
                "physical_query_count": len(queries),
                "projection_results": details,
                "python_bundle_assembly_elapsed_ms": assembly_ms,
                "consumer_elapsed_ms": consumer_timings,
                "sales_logical_source_total_elapsed_ms": int((time.perf_counter() - source_started) * 1000),
                "total_return_rows": int(sum(item["row_count"] for item in details.values())),
                "total_pandas_deep_memory_bytes": int(sum(item["pandas_deep_memory_bytes"] for item in details.values())),
                "bundle_rows": {
                    "product_identity": int(len(bundle.product_identity_df)),
                    "product_month_sales": int(len(bundle.product_month_sales_df)),
                    "manufacturer_month": int(len(bundle.manufacturer_month_df)),
                    "sales_month_total": int(len(bundle.sales_month_total_df)),
                    "purchase_month_total": int(len(bundle.purchase_month_total_df)),
                },
                "consumer_facts": facts,
            })
            return 0
        finally:
            event.remove(engine, "before_cursor_execute", _set_timeout)
            mssql_client.set_current_company_id(None)
    except Exception as exc:
        result.update({"status": "FAIL", "exception_type": type(exc).__name__, "exception": str(exc), "traceback": traceback.format_exc(limit=4)})
        return 1
    finally:
        result["finished_at"] = datetime.now().isoformat(timespec="seconds")
        _write(output, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prepare or execute one final read-only Dashboard sales candidate gate.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true", help="Run the four compact read-only projections exactly once each.")
    parser.add_argument("--query-timeout-seconds", type=int, default=120)
    args = parser.parse_args()
    if int(args.query_timeout_seconds) <= 0:
        raise SystemExit("query timeout must be positive")
    raise SystemExit(main(args.output, execute=bool(args.execute), timeout_seconds=int(args.query_timeout_seconds)))
