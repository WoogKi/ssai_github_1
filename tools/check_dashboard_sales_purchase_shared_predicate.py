"""Static contract checks for the shared Dashboard monthly predicate boundary."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.analytics_sales_trend_service import (
    _build_dashboard_purchase_vendor_where,
    _build_monthly_fast_where,
    _monthly_spec,
)


def _params() -> dict:
    return {
        "month_from": "202507",
        "month_to": "202608",
        "physic_cd": "P-001",
        "stock_cd_list": ["00001", "00008"],
        "buy_cd": "V-001",
        "io_gu_list": ["501", "601"],
        "product_supplier_scope_mode": "manufacturer",
        "manufacturer_codes": ["M-001"],
    }


def _assert_contains(text: str, expected: str) -> None:
    assert expected in text, f"missing predicate: {expected}"


def main() -> int:
    spec = _monthly_spec("monthly_book")
    prefix = spec["prefix"]
    sales_where, sales_bind = _build_monthly_fast_where(_params(), spec)
    purchase_where, purchase_bind = _build_dashboard_purchase_vendor_where(_params(), spec)

    common_fragments = (
        f"M.{prefix}_Stock_YyMm >= %(month_from)s",
        f"M.{prefix}_Stock_YyMm <= %(month_to)s",
        f"M.{prefix}_Physic_Cd = %(physic_cd)s",
        f"M.{prefix}_Stock_Cd IN (%(fast_stock_cd_0)s,%(fast_stock_cd_1)s)",
        f"M.{prefix}_Ven_Cd = %(buy_cd)s",
    )
    for fragment in common_fragments:
        _assert_contains(sales_where, fragment)
    for fragment in (
        f"M.{prefix}_Stock_YyMm >= %(month_from)s",
        f"M.{prefix}_Stock_YyMm <= %(month_to)s",
        f"M.{prefix}_Physic_Cd = %(physic_cd)s",
        f"M.{prefix}_Stock_Cd IN (%(dashboard_purchase_stock_cd_0)s,%(dashboard_purchase_stock_cd_1)s)",
        f"M.{prefix}_Ven_Cd = %(buy_cd)s",
    ):
        _assert_contains(purchase_where, fragment)

    _assert_contains(sales_where, "fast_supplier_vendor_0")
    _assert_contains(purchase_where, "purchase_supplier_vendor_0")
    _assert_contains(sales_where, f"M.{prefix}_Io_Gu_Gcode = %(sales_io_gu_gcode)s")
    _assert_contains(sales_where, f"M.{prefix}_Io_Gu IN (%(sales_io_gu_0)s, %(sales_io_gu_1)s)")
    _assert_contains(sales_where, f"LEFT(M.{prefix}_Io_Gu, 1) IN ({spec['out_prefixes']})")
    assert "Io_Gu" not in purchase_where, "purchase branch must not inherit sales IO/outbound predicate"

    assert sales_bind["fast_stock_cd_0"] == "00001"
    assert sales_bind["fast_stock_cd_1"] == "00008"
    assert purchase_bind["dashboard_purchase_stock_cd_0"] == "00001"
    assert purchase_bind["dashboard_purchase_stock_cd_1"] == "00008"
    assert sales_bind["fast_supplier_vendor_0"] == "M-001"
    assert purchase_bind["purchase_supplier_vendor_0"] == "M-001"
    assert sales_bind["_sales_io_filter_mode"] == "exact_selected"
    assert "_sales_io_filter_mode" not in purchase_bind
    print("PASS: dashboard sales/purchase shared predicate contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
