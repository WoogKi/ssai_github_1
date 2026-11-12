"""DB-free checks for the opt-in saved-I/O calculation contract."""

from __future__ import annotations

import inspect
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import analytics_sales_trend_service as sales  # noqa: E402
from app.services import dashboard_lite_facts as dashboard  # noqa: E402
from app.services.dashboard_inbound_facts_service import _sql as inbound_sql  # noqa: E402
from app.services.dashboard_inventory_frequency_snapshot import (  # noqa: E402
    build_product_statistics_relational_snapshot_from_aggregates,
)
from app.services.dashboard_inventory_frequency_snapshot_service import (  # noqa: E402
    _aggregate_product_statistics_event_chunks, _python_product_statistics_projection_chunks,
    build_frequency_snapshot_plan, frequency_snapshot_key, frequency_snapshot_read_keys, product_statistics_raw_event_stream_sql,
    selected_io_snapshot_key,
)
from app.services.monthly_frequency_aggregate import product_lifecycle_sql  # noqa: E402
from app.services.order_calculation_service import build_demand_params  # noqa: E402
from app.services.saved_io_scope import resolve_saved_io_scope  # noqa: E402
from tools.check_saved_io_scope_shadow_20261010 import (  # noqa: E402
    COMPANY13_CODES, _expected_saved_io_read_keys,
)


def _bind_values(binds: dict, prefix: str) -> set[str]:
    return {value for key, value in binds.items() if key.startswith(prefix)}


def test_company_scopes_and_snapshot_key() -> None:
    company13 = resolve_saved_io_scope(COMPANY13_CODES, confirmed_trade_roles=True)
    assert len(company13.all_codes) == 45
    assert tuple(map(len, (company13.inbound_codes, company13.inbound_return_codes,
                           company13.outbound_codes, company13.outbound_return_codes))) == (8, 5, 24, 8)
    assert company13.normal_inbound_codes == company13.inbound_codes
    assert company13.price_codes == company13.inbound_codes
    assert not company13.price_candidate_codes
    assert not company13.unresolved_inbound_codes
    assert {"580", "590"} <= set(company13.outbound_codes)
    other = resolve_saved_io_scope(("001", "051", "501", "601"), confirmed_trade_roles=True)
    assert other.all_codes == ("001", "051", "501", "601")
    assert other.outbound_codes == ("501",) and other.price_codes == ("001", "051")
    assert other.fingerprint("profile") != company13.fingerprint("profile")
    plan = build_frequency_snapshot_plan(company_id=13, evaluation_month="202610",
                                         stock_codes=("00001", "00012"), io_gu_codes=COMPANY13_CODES)
    assert selected_io_snapshot_key(plan, company13) != frequency_snapshot_key(plan, product_statistics=True)
    assert selected_io_snapshot_key(plan, company13) != selected_io_snapshot_key(
        plan, resolve_saved_io_scope(COMPANY13_CODES, include_pda_candidate=True)
    )
    company3 = build_frequency_snapshot_plan(company_id=3, evaluation_month="202610",
                                            stock_codes=("00001",), io_gu_codes=COMPANY13_CODES)
    for company_plan in (plan, company3):
        keys = frequency_snapshot_read_keys(company_plan)
        assert keys == _expected_saved_io_read_keys(company_plan, company13)
        assert keys[0] == selected_io_snapshot_key(company_plan, company13)
        assert keys[3] == frequency_snapshot_key(company_plan, product_statistics=True)
        assert len(set(keys)) == 6
        assert all(key.company_id == str(company_plan.company_id) for key in keys)
    # Isolate company identity while keeping every other scope dimension equal.
    same_scope_company3 = replace(plan, company_id=3)
    company3_keys = frequency_snapshot_read_keys(same_scope_company3)
    assert company3_keys == _expected_saved_io_read_keys(same_scope_company3, company13)
    assert set(company3_keys).isdisjoint(frequency_snapshot_read_keys(plan))
    assert all(key.company_id == "3" for key in company3_keys)


def test_grade_source_sql() -> None:
    scope = resolve_saved_io_scope(COMPANY13_CODES, confirmed_trade_roles=True)
    plan = build_frequency_snapshot_plan(company_id=13, evaluation_month="202610",
                                         stock_codes=("00001", "00012"), io_gu_codes=COMPANY13_CODES)
    lifecycle, binds = product_lifecycle_sql(
        stock_codes=plan.stock_codes, cutoff_date="20261010", basis_from=plan.basis_from,
        basis_to=plan.basis_to, io_scope=scope, stock_mode="real",
    )
    assert _bind_values(binds, "lifecycle_inbound_io") == set(scope.inbound_codes)
    assert _bind_values(binds, "lifecycle_price_io") == set(scope.inbound_codes)
    assert "PurchasePriceMonthly" in lifecycle and "MonthlyStock" in lifecycle
    assert "NULLIF(RC.cost_apply_code, '') IS NOT NULL" in lifecycle
    assert "V.Rd03_Cost_Apply_Cd" in lifecycle
    assert "ROW_NUMBER() OVER (" in lifecycle
    assert "I.Rd11_Io_Gu_Gcode = '0012'" in lifecycle
    raw, raw_binds = product_statistics_raw_event_stream_sql(plan, io_scope=scope)
    assert _bind_values(raw_binds, "statistics_io") == set(scope.outbound_codes + scope.outbound_return_codes)
    assert "O.Rd12_Io_Gu_Gcode='0012'" in raw
    assert raw.rstrip().endswith("O.Rd12_Io_Gu")
    assert "O.Rd12_Stock_Cd IN" in raw and raw_binds["basis_from"] == "20260701"
    legacy, _ = product_statistics_raw_event_stream_sql(plan)
    assert not legacy.rstrip().endswith("O.Rd12_Io_Gu")


def test_distinct_online_events_and_grades() -> None:
    scope = resolve_saved_io_scope(COMPANY13_CODES, confirmed_trade_roles=True)
    rows = [
        ("580", Decimal("5"), Decimal("100")),
        ("580", Decimal("5"), Decimal("100")),
        ("590", Decimal("5"), Decimal("100")),
        ("690", Decimal("-2"), Decimal("-40")),
    ]
    raw = pd.DataFrame([
        {"outbound_date": "20260801", "vendor_code": "V1", "outbound_seq": "1",
         "product_code": "00001", "stock_code": "00001", "io_gcode": "0012", "io_tcode": code,
         "outbound_quantity": qty, "paid_quantity": qty, "supply_amount": amount}
        for code, qty, amount in rows
    ])
    projection = pd.DataFrame.from_records([
        record for chunk in _python_product_statistics_projection_chunks(
            (raw.iloc[:2], raw.iloc[2:]), basis_from="20260701", io_scope=scope,
        ) for record in chunk.to_dict("records")
    ])
    events = projection.loc[projection["row_kind"].eq("event")]
    assert len(events) == 2 and set(events["mapping_count"]) == {1}
    assert sum(int(value) for value in events["exact_duplicate_row_count"]) == 1
    assert sum(Decimal(str(value)) for value in events["outbound_quantity"]) == Decimal(10)
    returns = projection.loc[projection["row_kind"].eq("return_stats")]
    assert len(returns) == 1 and returns.iloc[0]["return_quantity"] == Decimal(2)
    assert returns.iloc[0]["return_supply_amount"] == Decimal(40)
    monthly, diagnostics, days, customers, paid, return_stats, prices = _aggregate_product_statistics_event_chunks(
        (projection,)
    )
    assert sum(int(row["occurrence_count"]) for row in monthly) == 2
    assert sum(int(row["outbound_quantity"]) for row in monthly) == 10
    assert diagnostics["distinct_normal_event_count"] == 2
    assert paid["00001"] == 10 and return_stats["00001"]["quantity"] == Decimal(2)
    snapshot = build_product_statistics_relational_snapshot_from_aggregates(
        company_id=13, evaluation_month="202610", monthly_rows=monthly,
        product_codes=("00001",), product_day_counts=days, product_customer_counts=customers,
        first_normal_inbound_months={"00001": "202501"}, outbound_paid_quantities=paid,
        return_statistics=return_stats,
        purchase_prices={"00001": {"unit_price": "10", "basis_month": "202608", "status": "ready"}},
        sales_prices={code: {**value, "status": "ready"} for code, value in prices.items()},
        stock_codes=("00001",), source_diagnostics=diagnostics,
    )
    product = snapshot.frequency_products[0]
    assert product["outbound_qty_3m"] == 10
    assert product["profit_grade"] != "unavailable" and product["contribution_grade"] != "unavailable", product
    assert product["frequency_grade"] in "ABCDEFX"


def test_order_and_dashboard_scope() -> None:
    order_scope = SimpleNamespace(io_gu_codes=COMPANY13_CODES)
    demand = build_demand_params({}, order_scope)
    assert set(demand["io_gu_list"]) == set(resolve_saved_io_scope(COMPANY13_CODES).outbound_codes)
    assert {"580", "590"} <= set(demand["io_gu_list"])
    assert not (set(demand["io_gu_list"]) & {"008", "090", "052", "690"})
    scope = resolve_saved_io_scope(COMPANY13_CODES, confirmed_trade_roles=True)
    sql, binds = inbound_sql({"stock_cd_list": ["00001", "00012"]},
                             start_date="20251001", cutoff_date="20261010", io_scope=scope)
    assert _bind_values(binds, "inbound_io") == set(scope.normal_inbound_codes + scope.inbound_return_codes)
    assert "I.Rd11_Stock_Cd IN" in sql and "0018" in sql
    assert "io_scope=saved_inbound_scope" in inspect.getsource(dashboard.build_dashboard_lite_facts)
    stock_params, removed = sales._copy_current_stock_params_without_io_scope({
        "stock_cd_list": ["00001"], "stock_mode": "real", "io_gu_list": list(COMPANY13_CODES),
    })
    assert removed == 45 and stock_params["stock_cd_list"] == ["00001"]
    assert stock_params["stock_mode"] == "real" and "io_gu_list" not in stock_params


def test_original_return_sign_contract() -> None:
    source = inspect.getsource(sales)
    assert "THEN -1 * COALESCE(M.{p}_Out_Quantity" not in source
    assert "THEN -1 * COALESCE(Out_Put.Rd12_Quantity" not in source
    assert "SUM(COALESCE(M.{p}_Out_Quantity, 0)) AS [출고수량]" in source
    assert "SUM(COALESCE(Out_Put.Rd12_Quantity, 0)) AS 출고수량" in source
    assert Decimal(388 + 5 + 769 - 5) == Decimal(1157)


if __name__ == "__main__":
    tests = (
        test_company_scopes_and_snapshot_key, test_grade_source_sql,
        test_distinct_online_events_and_grades, test_order_and_dashboard_scope,
        test_original_return_sign_contract,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"saved_io_full_integration: {len(tests)}/{len(tests)} PASS (offline)")
