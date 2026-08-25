"""Offline contract gate for the final Dashboard sales candidate E2E probe."""
from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.analytics_manufacturer_sales_trend_service import get_manufacturer_sales_trend_summary
from app.services.analytics_sales_trend_service import (
    DashboardNarrowSalesPurchaseBundle,
    adapt_dashboard_narrow_bundle_for_forecast,
    adapt_dashboard_narrow_bundle_for_manufacturer,
    adapt_dashboard_narrow_bundle_for_visual,
    build_dashboard_narrow_bundle_from_projections,
    build_dashboard_narrow_sales_purchase_bundle,
    get_sales_forecast_df,
)
from app.services.dashboard_lite_facts import (
    _build_demand_surge_history_by_product,
    _build_visual_phase2_summary,
    _dashboard_internal_source_params,
    normalize_dashboard_lite_params,
)
from app.services.dashboard_narrow_sales_candidate_service import (
    _manufacturer_month,
    _queries,
    _sales_facts,
    can_use_dashboard_narrow_sales_candidate,
)
from tools.check_dashboard_sales_purchase_narrow_bundle import _projection_frames, _purchase_fixture, _sales_fixture


def _assemble_candidate_bundle(
    product_month_sales: pd.DataFrame,
    product_identity: pd.DataFrame,
    manufacturer_vendor_relation: pd.DataFrame,
    purchase_facts: pd.DataFrame,
) -> DashboardNarrowSalesPurchaseBundle:
    """Use the production narrow assembly path without importing any probe."""
    manufacturer_month = _manufacturer_month(
        product_month_sales.copy(), product_identity, manufacturer_vendor_relation
    )
    sales_facts = _sales_facts(product_month_sales.copy(), product_identity, manufacturer_month)
    return build_dashboard_narrow_bundle_from_projections(product_identity, sales_facts, purchase_facts)


def _run_candidate_consumers(
    bundle: DashboardNarrowSalesPurchaseBundle,
    params: dict[str, object],
) -> tuple[dict[str, int], dict[str, int]]:
    """Exercise the existing consumers only; no probe SQL or DB work occurs."""
    timings: dict[str, int] = {}
    evaluation_month = str(params["evaluation_month"])
    history_month_from = str(params.get("dashboard_lite_history_month_from") or params["month_from"])

    started = time.perf_counter()
    forecast = get_sales_forecast_df(params, raw_df=adapt_dashboard_narrow_bundle_for_forecast(bundle))
    timings["forecast_adapter_consumer_elapsed_ms"] = int((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    demand_history = _build_demand_surge_history_by_product(
        bundle.product_month_sales_df,
        evaluation_month=evaluation_month,
        history_month_from=history_month_from,
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
    return timings, {
        "forecast_rows": int(len(forecast)),
        "demand_surge_product_count": int(len(demand_history.get("by_product") or {})),
        "manufacturer_summary_rows": int(len(manufacturer)),
        "purchase_month_total_rows": int(len(bundle.purchase_month_total_df)),
        "purchase_trend_points": int(visual.get("purchase_trend_points") or len(trend or [])),
    }


def main() -> int:
    smoke_params = normalize_dashboard_lite_params(
        {
            "company_id": "8",
            "month_from": "202602",
            "month_to": "202607",
            "evaluation_month": "202608",
            "date_from": "20260201",
            "date_to": "20260731",
            "policy_date": "20260825",
            "source_mode": "monthly_book",
            "stock_mode": "real",
            "stock_cd_list": ["00001", "00008", "00013"],
        },
        today=date(2026, 8, 25),
    )
    smoke_source_params = _dashboard_internal_source_params(smoke_params, today=date(2026, 8, 25))
    assert can_use_dashboard_narrow_sales_candidate(smoke_source_params) == (True, "monthly_only_contract")
    rejected_params = dict(smoke_source_params)
    rejected_params["product_di_list"] = ["0004:D1"]
    assert can_use_dashboard_narrow_sales_candidate(rejected_params) == (False, "product_dimension_filter_contract")
    rejected_class_params = dict(smoke_source_params)
    rejected_class_params["product_class_list"] = ["0031:C1"]
    assert can_use_dashboard_narrow_sales_candidate(rejected_class_params) == (False, "product_dimension_filter_contract")

    params = dict(smoke_source_params)
    queries, _meta = _queries(params)
    assert list(queries) == ["product_month_sales", "product_identity", "manufacturer_vendor_relation", "purchase_facts"]
    assert len(queries) == 4
    assert "GROUPING SETS" not in queries["product_month_sales"][0].upper()
    assert "GROUPING SETS" not in queries["manufacturer_vendor_relation"][0].upper()
    assert "purchase_product_vendor" not in queries["purchase_facts"][0]

    sales = _sales_fixture()
    # The live product master join is product-code keyed.  Keep the positive
    # case aligned with that normal invariant; the split-stage gate separately
    # proves that a mixed master identity fails closed.
    sales.loc[sales["제품코드"].eq("P2"), ["제조사코드", "제조사명"]] = ["M1", "제조사1"]
    purchase = _purchase_fixture()
    legacy = build_dashboard_narrow_sales_purchase_bundle(
        sales, purchase, evaluation_month="202607", history_month_from="202501"
    )
    identity, _sales_facts, purchase_facts = _projection_frames(legacy)
    product_month = legacy.product_month_sales_df.drop(columns=["__dashboard_product_identity_id"])
    relation = sales.loc[:, ["기준월", "제품코드", "매입처코드"]].copy()
    candidate = _assemble_candidate_bundle(product_month, identity, relation, purchase_facts)

    for name in (
        "manufacturer_month_df", "sales_month_total_df", "purchase_month_total_df",
    ):
        pd.testing.assert_frame_equal(getattr(legacy, name), getattr(candidate, name), check_dtype=True)
    pd.testing.assert_frame_equal(legacy.product_identity_df, candidate.product_identity_df, check_dtype=True)
    pd.testing.assert_frame_equal(legacy.product_month_sales_df, candidate.product_month_sales_df, check_dtype=True)
    assert legacy.purchase_diagnostics == candidate.purchase_diagnostics

    fixture_params = dict(smoke_source_params)
    fixture_params.update({"month_from": "202501", "month_to": "202607", "evaluation_month": "202607", "dashboard_lite_history_month_from": "202501"})
    timings, facts = _run_candidate_consumers(candidate, fixture_params)
    assert set(timings) == {
        "forecast_adapter_consumer_elapsed_ms", "demand_surge_adapter_consumer_elapsed_ms",
        "manufacturer_reconstruction_consumer_elapsed_ms", "purchase_consumer_elapsed_ms",
    }
    assert facts["forecast_rows"] > 0
    assert facts["manufacturer_summary_rows"] > 0
    assert facts["purchase_month_total_rows"] > 0
    print("PASS: dashboard final sales candidate E2E static gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
