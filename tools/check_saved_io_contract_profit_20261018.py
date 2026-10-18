"""Offline average-purchase future-profit gate for Saved I/O snapshots."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sys
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot import (
    SAVED_IO_AVG_PURCHASE_ALGORITHM_VERSION,
    SAVED_IO_FUTURE_PROFIT_ALGORITHM_VERSION,
    SAVED_IO_FULL_SHADOW_ALGORITHM_VERSION,
    validate_relational_frequency_snapshot,
)
from app.services.dashboard_inventory_frequency_snapshot_service import (
    build_frequency_snapshot_plan,
    calculate_saved_io_three_grades_shadow,
    frequency_snapshot_read_keys,
)
from app.services.monthly_frequency_aggregate import product_lifecycle_sql
from app.services.saved_io_scope import resolve_saved_io_scope
from app.services.product_classification_contract import ClassificationStatus
from app.services.ssai_product_classification_repository import ProductClassificationAuthorityRead


def _authority(**_kwargs: Any) -> ProductClassificationAuthorityRead:
    return ProductClassificationAuthorityRead(status=ClassificationStatus.READY)


def test_average_purchase_profit_and_missing_price() -> None:
    plan = build_frequency_snapshot_plan(
        company_id=4, evaluation_month="202610", stock_codes=("00001",),
        io_gu_codes=("001", "008", "090", "580", "590", "690"),
    )
    lifecycle = pd.DataFrame([
        {
            "product_code": "P1", "first_normal_inbound_month": "202608",
            "current_stock_present": 1, "basis_inbound_present": 1,
            "avg_purchase_unit_cost": 100, "purchase_price_basis_month": "202609",
            "representative_vendor_code": "BUY1", "purchase_cost_apply_code": "PURCHASE1",
            "purchase_price_basis_kind": "three_month", "excluded_purchase_trade_count": 0,
        },
        {
            "product_code": "P2", "first_normal_inbound_month": "202601",
            "current_stock_present": 1, "basis_inbound_present": 0,
            "avg_purchase_unit_cost": None, "purchase_price_basis_month": None,
            "representative_vendor_code": "BUY2", "purchase_cost_apply_code": "PURCHASE2",
            "purchase_price_basis_kind": None, "excluded_purchase_trade_count": 0,
        },
        {
            "product_code": "P3", "first_normal_inbound_month": None,
            "current_stock_present": 1, "basis_inbound_present": 0,
            "avg_purchase_unit_cost": 20, "purchase_price_basis_month": "202609",
            "representative_vendor_code": "BUY3", "purchase_cost_apply_code": "PURCHASE3",
            "purchase_price_basis_kind": "three_month", "excluded_purchase_trade_count": 0,
        },
        {
            "product_code": "P4", "first_normal_inbound_month": "202608",
            "current_stock_present": 1, "basis_inbound_present": 1,
            "avg_purchase_unit_cost": 1, "purchase_price_basis_month": "202609",
            "representative_vendor_code": "BUY4", "purchase_cost_apply_code": "BID4",
            "purchase_price_basis_kind": "three_month", "excluded_purchase_trade_count": 0,
        },
        {
            "product_code": "P5", "first_normal_inbound_month": "202501",
            "current_stock_present": 1, "basis_inbound_present": 0,
            "avg_purchase_unit_cost": 50, "purchase_price_basis_month": "202602",
            "representative_vendor_code": "BUY5", "purchase_cost_apply_code": "OLD5",
            "purchase_price_basis_kind": "historical", "excluded_purchase_trade_count": 0,
        },
    ])
    raw = pd.DataFrame([
        {"outbound_date": "20260901", "vendor_code": "V1", "outbound_seq": str(index),
         "product_code": product, "stock_code": "00001", "io_gcode": "0012",
         "io_tcode": code, "cost_apply_code": contract,
         "outbound_quantity": quantity, "paid_quantity": quantity,
         "supply_amount": amount}
        for index, (product, code, contract, quantity, amount) in enumerate((
            ("P1", "580", "SALE1", 2, 200), ("P1", "590", "SALE2", 3, 600),
            ("P1", "690", "SALE2", -1, -30), ("P2", "580", "SALE9", 1, 50),
            ("P4", "580", "SALE4", 2, 100), ("P5", "590", "SALE5", 1, 100),
        ), start=1)
    ])
    calls: list[str] = []

    def query(company_id: int, sql: str, _binds: dict[str, Any], timeout: int) -> pd.DataFrame:
        assert company_id == 4 and timeout == 120
        calls.append(sql)
        return lifecycle.copy() if "FirstInbound" in sql else raw.copy()

    result = calculate_saved_io_three_grades_shadow(
        plan=plan, created_by="offline-contract", query_executor=query,
        classification_authority_loader=_authority,
    )
    snapshot = result["relational_snapshot"]
    validate_relational_frequency_snapshot(snapshot)
    rows = {row["product_code"]: row for row in snapshot.frequency_products}
    assert len(calls) == 2 and len(rows) == 5
    assert "RepresentativeContract" in calls[0] and "V.Rd03_Cost_Apply_Cd" not in calls[0]
    assert "vendor_quantity DESC" in calls[0] and "vendor_supply_price DESC" in calls[0]
    assert "P.master_order_vendor_code" in calls[0]
    assert "PurchaseCandidates" in calls[0] and "PurchaseDistribution" in calls[0]
    assert "A.product_code IS NULL" in calls[0]
    assert "I.Rd11_Quantity > 0" in calls[0]
    assert "FOR XML PATH('price')" not in calls[0]
    assert "COALESCE(I.Rd11_Fin_Supply_Price, I.Rd11_Supply_Price)" in calls[0]
    assert "COALESCE(O.Rd12_Fin_Supply_Price,O.Rd12_Supply_Price,0)" in calls[1]
    assert "/ 1.1" not in calls[0] + calls[1]
    assert "O.Rd12_Cost_Apply_Cd" in calls[1]
    assert snapshot.key.algorithm_version == SAVED_IO_AVG_PURCHASE_ALGORITHM_VERSION
    assert len(SAVED_IO_AVG_PURCHASE_ALGORITHM_VERSION) <= 64
    assert snapshot.source_contract["fingerprint_mode"] == "average_purchase_statistics_v1"
    assert len(snapshot.source_contract["fingerprint_mode"]) <= 32
    assert frequency_snapshot_read_keys(plan)[1].algorithm_version == SAVED_IO_FUTURE_PROFIT_ALGORITHM_VERSION
    assert frequency_snapshot_read_keys(plan)[2].algorithm_version == SAVED_IO_FULL_SHADOW_ALGORITHM_VERSION
    assert rows["P1"]["avg_purchase_unit_cost"] == Decimal("100.0000000000")
    assert rows["P1"]["avg_sales_unit_price"] == Decimal("160.0000000000")
    assert rows["P1"]["estimated_contribution_amount"] == Decimal("300.000000")
    assert rows["P1"]["profit_grade"] == "A" and rows["P1"]["contribution_grade"] == "A"
    assert rows["P1"]["return_qty_3m"] == 1
    assert rows["P2"]["profitability_status"] == "unavailable"
    assert rows["P3"]["profitability_status"] == "unavailable"
    assert rows["P3"]["purchase_price_status"] == "ready"
    assert rows["P4"]["avg_purchase_unit_cost"] == Decimal("1")
    assert rows["P4"]["profitability_status"] == "ready"
    assert rows["P4"]["estimated_contribution_amount"] == Decimal("98")
    assert rows["P5"]["purchase_price_status"] == "ready"
    assert rows["P5"]["estimated_contribution_amount"] == Decimal("50")
    assert rows["P5"]["profit_grade"] == "A"
    assert result["source_metrics"]["average_purchase_price_coverage"] == {
        "three_month": 3, "six_month": 0, "historical": 1, "unavailable": 1,
    }
    assert result["source_metrics"]["excluded_purchase_trades"]["trade_count"] == 0


def test_average_purchase_sql_contract() -> None:
    io_scope = resolve_saved_io_scope(("001", "008", "090", "101", "580", "690"), confirmed_trade_roles=True)
    sql, binds = product_lifecycle_sql(
        stock_codes=("00001",), cutoff_date="20261018", basis_from="20260701", basis_to="20260930",
        io_scope=io_scope, snapshot_projection_only=True, average_purchase_price=True,
        force_hash_join=True,
    )
    assert binds["purchase_three_month_from"] == "20260701"
    assert binds["purchase_six_month_from"] == "20260401"
    assert "PurchaseCandidates" in sql and "SelectedPurchase" in sql and "PurchaseAverage" in sql
    assert "D.trade_count >= 4" in sql and "D.center_count >= 3" in sql
    assert "D.center_count * 4 >= D.trade_count * 3" in sql
    assert "D.center_quantity * 5 >= D.total_quantity * 4" in sql
    assert "D.center_price > 2" in sql and "R.unit_price > 2" in sql
    assert "D.extreme_count * 4 <= D.trade_count" in sql
    assert "D.extreme_quantity * 5 <= D.total_quantity" in sql
    assert "I.Rd11_Cost_Apply_Cd" not in sql
    assert "I.Rd11_Oquantity AS" not in sql
    assert "I.Rd11_In_YyMmDd < :purchase_six_month_from" in sql
    assert "OPTION (HASH JOIN)" not in sql


if __name__ == "__main__":
    test_average_purchase_profit_and_missing_price()
    test_average_purchase_sql_contract()
    print("saved_io_average_purchase: 2/2 PASS (offline)")
