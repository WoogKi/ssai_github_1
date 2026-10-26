"""DB-free contracts for exact price buckets and optional Snapshot reuse."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot_service import (  # noqa: E402
    _python_product_statistics_projection_chunks, build_frequency_snapshot_plan,
    product_statistics_compact_event_stream_sql,
)
from app.services.monthly_frequency_aggregate import product_lifecycle_sql  # noqa: E402
from app.services.monthly_io_components import MonthlyIoFact, reconstruct_purchase_prices, whole_checksum  # noqa: E402
from app.services.sql_server_monthly_io_repository import MonthlyIoMaterialization  # noqa: E402
from app.services.saved_io_scope import resolve_saved_io_scope  # noqa: E402
from tools.build_monthly_io_company import _assert_exact_r120, _save_or_resume_monthly_draft  # noqa: E402
from tools.build_monthly_io_company import _require_closed_source_months  # noqa: E402
from datetime import date


def bucket(product: str, price: str, count: int, amount: str, quantity: str, month: str = "202609") -> MonthlyIoFact:
    return MonthlyIoFact(
        month, "R110", "PRICE_BUCKET", product, "", "000", Decimal(price), "",
        eligible_price_row_count=count, eligible_paid_quantity=Decimal(quantity),
        eligible_supply_amount=Decimal(amount),
    )


def main() -> None:
    _require_closed_source_months(("202609",), today=date(2026, 10, 25))
    try:
        _require_closed_source_months(("202610",), today=date(2026, 10, 25))
    except ValueError:
        pass
    else:
        raise AssertionError("current source month was allowed for draft saving")
    class FakeStored:
        manifest_id = 7
        generation_no = 2
        materialization = None

    class FakeRepository:
        def __init__(self) -> None:
            self.pending = None
            self.save_count = 0

        def read_pending_exact(self, item):
            return self.pending

        def save_draft(self, item, *, created_by):
            self.save_count += 1
            self.pending = FakeStored()
            self.pending.materialization = item
            return self.pending

        def read_generation(self, item, generation_no):
            assert generation_no == 2
            return self.pending

    empty_month = MonthlyIoMaterialization(
        3, "202609", "a" * 64, "b" * 64, ("00001",), ("00001",), (),
    )
    empty_month.validate()
    fake = FakeRepository()
    first = _save_or_resume_monthly_draft(fake, empty_month, created_by="offline")
    second = _save_or_resume_monthly_draft(fake, empty_month, created_by="offline")
    assert first["resumed_draft"] is False and second["resumed_draft"] is True
    assert fake.save_count == 1 and second["status"] == "draft_pending_manual_approval"
    facts = (
        bucket("00001", "100", 4, "400", "4"),
        bucket("00001", "1000", 1, "1000", "1"),
        bucket("00002", "1", 4, "4", "4"),
        bucket("00002", "100", 1, "100", "1"),
        bucket("00003", "14103.666666", 1, "42311", "3"),
        MonthlyIoFact(
            "202603", "R110", "PRICE_TAIL", "00004", "", "000", Decimal("-1.000000"), "00004",
            eligible_price_row_count=1, eligible_paid_quantity=Decimal("3"),
            eligible_supply_amount=Decimal("42311"), last_purchase_date="20250315",
            last_purchase_seq=1, last_vendor_code="V", last_tie_count=1,
        ),
    )
    result = reconstruct_purchase_prices(
        facts, three_month_from="202607", six_month_from="202604",
        basis_month="202609", product_codes=("00001", "00002", "00003", "00004"),
    )
    assert result["00001"]["unit_price"] == Decimal("100.0000000000")
    assert result["00001"]["excluded_trade_count"] == 1
    assert result["00002"]["excluded_trade_count"] == 0
    assert result["00003"]["unit_price"] == Decimal("14103.6666660000")
    assert result["00004"]["basis_kind"] == "historical"
    assert result["00004"]["unit_price"] == Decimal("14103.6666660000")
    assert len(whole_checksum(facts)) == 64

    empty_return = MonthlyIoFact(
        "202607", "R120", "BASE", "00001", "00001", "601", Decimal(-1), "", source_row_count=1,
    )
    assert _assert_exact_r120((empty_return,), ([], None, None, None, None, {}, None)) == {
        "normal_monthly_mismatch_count": 0, "return_product_mismatch_count": 0,
        "normal_monthly_group_count": 0,
    }
    scoped_normal = MonthlyIoFact(
        "202607", "R120", "BASE", "00001", "00001", "501", Decimal(-1), "",
        source_row_count=1, normal_event_count=1, normal_event_quantity=Decimal(2),
    )
    source_projection = (
        [
            {"month": "202607", "product_code": "00001", "stock_code": "00001",
             "occurrence_count": 1, "outbound_quantity": Decimal(2)},
            {"month": "202607", "product_code": "outside", "stock_code": "00001",
             "occurrence_count": 1, "outbound_quantity": Decimal(3)},
        ], None, None, None, None,
        {"outside": {"event_count": 1, "quantity": Decimal(1), "supply_amount": Decimal(10)}}, None,
    )
    assert _assert_exact_r120((scoped_normal,), source_projection, product_codes={"00001"}) == {
        "normal_monthly_mismatch_count": 0, "return_product_mismatch_count": 0,
        "normal_monthly_group_count": 1,
    }

    plan = build_frequency_snapshot_plan(company_id=3, evaluation_month="202610", stock_codes=("00001",),
                                         io_gu_codes=("001", "501", "601"))
    io_scope = resolve_saved_io_scope(plan.io_gu_codes, confirmed_trade_roles=True)
    options = dict(
        stock_codes=plan.stock_codes, cutoff_date=plan.basis_to,
        basis_from=plan.basis_from, basis_to=plan.basis_to, io_scope=io_scope,
        average_purchase_price=True,
    )
    direct_sql, direct_binds = product_lifecycle_sql(**options)
    cached_sql, cached_binds = product_lifecycle_sql(
        **options, cached_purchase_manifest_ids=(17, 18), cached_purchase_company_id=3,
    )
    assert "CachedPurchaseProduct" not in direct_sql and "cached_purchase_manifest_0" not in direct_binds
    assert "SSAI_ANALYTICS.snapshot.monthly_io_fact" in cached_sql
    assert cached_sql.count("NOT EXISTS (SELECT 1 FROM CachedPurchaseProduct") == 3
    assert cached_binds["cached_purchase_company_id"] == "3"
    assert cached_binds["cached_purchase_manifest_0"] == 17
    assert cached_binds["cached_purchase_manifest_1"] == 18
    precomputed_sql, _ = product_lifecycle_sql(**options, precomputed_purchase_prices=True)
    assert "PurchaseCandidates AS" not in precomputed_sql
    assert "PurchaseAverage AS" not in precomputed_sql
    assert "AS L0" not in precomputed_sql and "AS L1" not in precomputed_sql
    assert "CAST(NULL AS decimal(38,10)) AS avg_purchase_unit_cost" in precomputed_sql
    assert "VendorAggregate AS" in precomputed_sql
    compact_sql, compact_binds = product_statistics_compact_event_stream_sql(plan, io_scope=io_scope)
    assert "HistoricalExact AS" in compact_sql and "HistoricalMonthly AS" in compact_sql
    assert "stream_row_kind" in compact_sql and "historical_normal_io_0" in compact_binds

    columns = ("outbound_date", "vendor_code", "outbound_seq", "product_code", "stock_code",
               "io_gcode", "io_tcode", "cost_apply_code", "outbound_quantity", "paid_quantity",
               "supply_amount")
    def row(date: str, seq: str, paid: str, amount: str) -> dict[str, object]:
        return dict(zip(columns, (date, "V", seq, "00001", "00001", "0012", "501", "",
                                  Decimal("2"), Decimal(paid), Decimal(amount))))
    raw_rows = [row("20260601", "1", "2", "20"), row("20260601", "1", "2", "20"),
                row("20260602", "2", "3", "30"), row("20260701", "3", "4", "40")]
    compressed = {key: raw_rows[0][key] for key in columns}
    compressed.update(outbound_date="20260601", vendor_code="", outbound_seq="", stock_code="",
                      io_tcode="000", outbound_quantity=Decimal(0), paid_quantity=Decimal(5),
                      supply_amount=Decimal(50), stream_row_kind="historical_price")
    recent = dict(raw_rows[-1], stream_row_kind="raw")
    raw_projection = pd.concat(_python_product_statistics_projection_chunks(
        (pd.DataFrame(raw_rows),), basis_from="20260701", io_scope=io_scope,
    ), ignore_index=True)
    compact_projection = pd.concat(_python_product_statistics_projection_chunks(
        (pd.DataFrame((compressed, recent)),), basis_from="20260701", io_scope=io_scope,
    ), ignore_index=True)
    assert raw_projection.equals(compact_projection)
    print("PASS monthly IO price, R120, checksum and optional Source1 cache gates")


if __name__ == "__main__":
    main()
