"""Offline contract gate for the final Dashboard sales candidate E2E probe."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.analytics_sales_trend_service import build_dashboard_narrow_sales_purchase_bundle
from app.services.dashboard_lite_facts import _dashboard_internal_source_params, normalize_dashboard_lite_params
from app.services.dashboard_narrow_sales_candidate_service import can_use_dashboard_narrow_sales_candidate
from tools.check_dashboard_sales_purchase_candidate_e2e_gate import (
    assemble_candidate_bundle,
    build_candidate_queries,
    run_candidate_consumers,
)
from tools.check_dashboard_sales_purchase_db_multigrain_equality import dashboard_probe_contract
from tools.check_dashboard_sales_purchase_narrow_bundle import _projection_frames, _purchase_fixture, _sales_fixture


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

    params, _contract = dashboard_probe_contract()
    queries, plan = build_candidate_queries(params)
    assert list(queries) == ["product_month_sales", "product_identity", "manufacturer_vendor_relation", "purchase_facts"]
    assert plan["logical_source_call_count"] == 1
    assert plan["physical_query_count"] == 4
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
    candidate = assemble_candidate_bundle(product_month, identity, relation, purchase_facts)

    for name in (
        "manufacturer_month_df", "sales_month_total_df", "purchase_month_total_df",
    ):
        pd.testing.assert_frame_equal(getattr(legacy, name), getattr(candidate, name), check_dtype=True)
    pd.testing.assert_frame_equal(legacy.product_identity_df, candidate.product_identity_df, check_dtype=True)
    pd.testing.assert_frame_equal(legacy.product_month_sales_df, candidate.product_month_sales_df, check_dtype=True)
    assert legacy.purchase_diagnostics == candidate.purchase_diagnostics

    fixture_params = dict(params)
    fixture_params.update({"month_from": "202501", "month_to": "202607", "evaluation_month": "202607", "dashboard_lite_history_month_from": "202501"})
    timings, facts = run_candidate_consumers(candidate, fixture_params)
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
