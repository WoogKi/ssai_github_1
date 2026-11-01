"""DB-free gate for month-specific approved product-universe reuse."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.sql_server_monthly_io_repository import MonthlyIoMaterialization  # noqa: E402
from app.services.dashboard_inventory_frequency_snapshot_service import build_frequency_snapshot_plan  # noqa: E402
from app.services.monthly_io_components import monthly_product_universe_sql  # noqa: E402
from tools import build_monthly_io_company as build  # noqa: E402
from tools.build_monthly_io_company import _verify_approved_universe_reuse  # noqa: E402


def manifest(month: str, codes: tuple[str, ...], verified_at: str | None) -> dict[str, object]:
    item = MonthlyIoMaterialization(6, month, "a" * 64, "b" * 64, codes, ("S",), ())
    return {"product_count": len(codes),
            "product_universe_fingerprint": item.product_universe_fingerprint,
            "source_verified_at": verified_at}


def verify(rows: list[tuple[str, str]], months: dict[str, tuple[str, ...]],
           source: dict[str, dict[tuple[str, str], str | None]] | None = None,
           verified_at: dict[str, str | None] | None = None) -> dict[str, object]:
    verification = verified_at or {}
    evidence = source if source is not None else {"R110": {}, "R120": {}}
    return _verify_approved_universe_reuse(
        company_id=6,
        current_rows=pd.DataFrame(rows, columns=("product_code", "product_add_date")),
        existing={month: manifest(month, codes, verification.get(month, "2026-10-25 12:57:48"))
                  for month, codes in months.items()},
        profile_fingerprint="a" * 64, scope_fingerprint="b" * 64,
        stock_codes=("S",),
        source_first_dates=lambda codes, first_day, cutoff: {
            kind: {(code, month): day for (code, month), day in rows.items()
                   if code in codes and day is not None and first_day <= day < cutoff}
            for kind, rows in evidence.items()
        },
    )


def blocked(action) -> None:
    try:
        action()
    except ValueError:
        return
    raise AssertionError("unsafe universe reuse was allowed")


def main() -> None:
    plan = build_frequency_snapshot_plan(
        company_id=6, evaluation_month="202611", stock_codes=("S",),
        product_group_codes=("0013:9999",), product_di_codes=("0004:1",),
        product_class_codes=("0031:01",), io_gu_codes=("501", "601"),
    )
    sql, _ = monthly_product_universe_sql(plan, include_add_date=True)
    for fragment in ("Rd04_Physic_Cd <> ''", "Rd04_Physic_Group_Gcode",
                     "Rd04_Physic_Di_Gcode", "Rd04_Physic_Tax_Gcode",
                     "ManagementStd.Rd046_Physic_Cd", "Rd04_Add_Date"):
        assert fragment in sql
    old = {"202603": ("A",), "202609": ("A",)}
    rows = [("A", "20200101"), ("B", "20261029")]
    proof = verify(rows, old)
    assert proof["changed_months"] == ["202603", "202609"]
    assert proof["new_product_count"] == 1
    assert verify([("A", "20200101")], old)["changed_months"] == []
    blocked(lambda: verify(rows, old, {"R110": {("B", "202609"): "20260930"}, "R120": {}}))
    blocked(lambda: verify(rows, old, {"R110": {}, "R120": {("B", "202603"): "20260301"}}))
    blocked(lambda: verify([("B", "20261029")], old))
    blocked(lambda: verify([("A", "20200101"), ("B", "20261029")],
                           {"202603": ("A", "C")}))
    blocked(lambda: verify([("A", "20200101"), ("B", "")], old))
    blocked(lambda: verify([("A", "20200101"), ("B", "20261029"),
                            ("B", "20261028")], old))
    mixed = verify([("A", "20200101"), ("B", "20260915")],
                   {"202608": ("A",), "202609": ("A", "B")},
                   {"R110": {("B", "202609"): "20260916"}, "R120": {}},
                   {"202608": "2026-09-10 12:00:00", "202609": "2026-10-25 12:00:00"})
    assert mixed["changed_months"] == ["202608"]
    mixed_with_trade = verify(
        [("A", "20200101"), ("B", "20260915"), ("C", "20261015")],
        {"202608": ("A",), "202609": ("A", "B")},
        {"R110": {("B", "202609"): "20260916"}, "R120": {}},
        {"202608": "2026-09-10 12:00:00", "202609": "2026-10-10 12:00:00"},
    )
    assert mixed_with_trade["changed_months"] == ["202608", "202609"]
    assert mixed_with_trade["new_product_count"] == 2
    added_in_approved_month = {"202609": ("A",)}
    added_rows = [("A", "20200101"), ("B", "20260915")]
    early_verified = {"202609": "2026-09-10 12:00:00"}
    assert verify(added_rows, added_in_approved_month, verified_at=early_verified)["new_product_count"] == 1
    blocked(lambda: verify(
        added_rows, added_in_approved_month,
        {"R110": {("B", "202609"): "20260916"}, "R120": {}}, early_verified,
    ))
    blocked(lambda: verify(
        added_rows, added_in_approved_month,
        {"R110": {}, "R120": {("B", "202609"): "20260916"}}, early_verified,
    ))
    blocked(lambda: verify(
        [("A", "20200101"), ("B", "20260930")], added_in_approved_month,
        {"R110": {("B", "202609"): "20260930"}, "R120": {}}, early_verified,
    ))
    assert verify(
        [("A", "20200101"), ("B", "20260930")], added_in_approved_month,
        {"R110": {}, "R120": {("B", "202610"): "20261001"}}, early_verified,
    )["new_product_count"] == 1
    prior_registered = verify(
        [("A", "20200101"), ("B", "20260410"), ("C", "20261029")],
        {f"2026{month:02d}": ("A", "B") for month in range(3, 10)},
    )
    assert prior_registered["new_product_count"] == 1
    assert len(prior_registered["month_proofs"]) == 7
    assert all(item["post_verification_candidate_count"] == 1 and item["fingerprint_match"]
               for item in prior_registered["month_proofs"].values())
    blocked(lambda: verify(rows, old, verified_at={"202603": "2026-10-29 01:00:00"}))
    blocked(lambda: verify(rows, old, verified_at={"202603": "2026-10-28 16:00:00"}))
    blocked(lambda: verify(rows, old, verified_at={"202603": None}))
    queries = []
    original_query = build._query_decimal

    def fake_query(company_id, sql, binds, timeout):
        queries.append((company_id, sql, binds, timeout))
        if "Rddbc110" in sql:
            return pd.DataFrame([("B", "202609", "20260916")],
                                columns=("product_code", "basis_month", "first_date"))
        return pd.DataFrame([("B", "202609", "20260917")],
                            columns=("product_code", "basis_month", "first_date"))

    build._query_decimal = fake_query
    try:
        dates, selects = build._new_product_source_first_dates(
            6, ("B",), "20260901", "20261001", 120,
        )
    finally:
        build._query_decimal = original_query
    assert dates == {"R110": {("B", "202609"): "20260916"},
                     "R120": {("B", "202609"): "20260917"}} and selects == 2
    blocked(lambda: verify(added_rows, added_in_approved_month, dates, early_verified))
    assert all(company == 6 and timeout == 120 and binds["first_day"] == "20260901"
               and binds["cutoff"] == "20261001" and ">= :first_day" in sql
               and "< :cutoff" in sql and "LEFT(" in sql
               and "SELECT" in sql and "INSERT" not in sql
               for company, sql, binds, timeout in queries)
    print("monthly_universe_reuse_db_free: PASS")


if __name__ == "__main__":
    main()
