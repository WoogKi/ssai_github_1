from __future__ import annotations

import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot import (  # noqa: E402
    PRODUCT_STATISTICS_ALGORITHM_VERSION,
    PRODUCT_STATISTICS_DECIMAL_STORAGE,
    PRODUCT_STATISTICS_SCHEMA_VERSION,
    SnapshotContractError,
    build_product_statistics_relational_snapshot_from_aggregates,
    build_relational_frequency_projection,
    calculate_relational_frequency_checksum,
    canonicalize_frequency_product_storage_row,
    canonicalize_product_statistics_decimal,
    frequency_product_columns,
    relational_row_checksum,
    validate_relational_frequency_projection,
    validate_relational_frequency_snapshot,
)
from app.services.dashboard_inventory_frequency_snapshot_service import (  # noqa: E402
    _aggregate_product_statistics_event_chunks,
    _price_status,
    build_frequency_snapshot_plan,
    product_statistics_event_stream_sql,
)
from app.services.ssai_analytics_snapshot_migration import MIGRATION_008_SQL, MIGRATIONS  # noqa: E402
from app.services.sql_server_snapshot_repository import SqlServerSnapshotRepository  # noqa: E402


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _sql_server_decimal_read(value: Decimal | None, scale: int) -> Decimal | None:
    """Model the numeric representation returned by pyodbc for DECIMAL(p, s)."""
    if value is None:
        return None
    quantum = Decimal(1).scaleb(-scale)
    if value.is_zero():
        return Decimal(0).quantize(quantum)
    return Decimal(str(value)).quantize(quantum)


def _snapshot():
    return build_product_statistics_relational_snapshot_from_aggregates(
        company_id="07",
        evaluation_month="202609",
        monthly_rows=(
            {"month": "202606", "product_code": "00001", "stock_code": "00001", "occurrence_count": 2, "outbound_quantity": 10, "outbound_day_count": 1},
            {"month": "202608", "product_code": "00002", "stock_code": "00001", "occurrence_count": 1, "outbound_quantity": 5, "outbound_day_count": 1},
        ),
        product_codes=("00001", "00002", "00003"),
        product_day_counts={"00001": 1, "00002": 1},
        product_customer_counts={"00001": 1, "00002": 1},
        first_normal_inbound_months={"00001": "202501", "00002": "202607", "00003": "202401"},
        outbound_paid_quantities={"00001": 8, "00002": 4},
        return_statistics={"00001": {"event_count": 1, "quantity": 2, "supply_amount": "300"}},
        purchase_prices={
            "00001": {"unit_price": "100", "basis_month": "202608", "status": "ready"},
            "00002": {"unit_price": "90", "basis_month": "202608", "status": "ready"},
            "00003": {"unit_price": "80", "basis_month": "202602", "status": "stale"},
        },
        sales_prices={
            "00001": {"unit_price": "150", "basis_month": "202608", "status": "ready"},
            "00002": {"unit_price": "120", "basis_month": "202608", "status": "ready"},
            "00003": {"unit_price": "100", "basis_month": "202602", "status": "stale"},
        },
        adjustment_only_products=("00002",),
        stock_codes=("00001",),
        source_diagnostics={"diagnostic_contract_version": 1},
        profile_fingerprint="a" * 64,
    )


def test_product_statistics_and_profitability() -> None:
    snapshot = _snapshot()
    validate_relational_frequency_snapshot(snapshot)
    _assert(snapshot.key.schema_version == PRODUCT_STATISTICS_SCHEMA_VERSION, "statistics schema version")
    _assert(snapshot.key.algorithm_version == PRODUCT_STATISTICS_ALGORITHM_VERSION, "statistics algorithm version")
    rows = {row["product_code"]: row for row in snapshot.frequency_products}
    first = rows["00001"]
    _assert(first["outbound_qty_3m"] == 10 and first["outbound_paid_qty_3m"] == 8, "outbound quantities")
    _assert(first["return_event_count_3m"] == 1 and first["return_qty_3m"] == 2, "gross return statistics")
    _assert(first["estimated_unit_profit"] == Decimal("50"), "unit profit")
    _assert(first["estimated_profit_rate"] == Decimal("0.333333333333"), "profit rate")
    _assert(first["estimated_contribution_amount"] == Decimal("500.000000"), "contribution")
    second = rows["00002"]
    _assert(second["frequency_grade"] == "F" and second["outbound_qty_3m"] == 5, "F raw statistics")
    _assert(second["profitability_status"] == "excluded_adjustment_only", "adjustment exclusion")
    _assert(second["profit_grade"] == "unavailable", "adjustment grade exclusion")
    _assert(rows["00003"]["profitability_status"] == "stale", "stale profitability")


def test_price_status_boundaries() -> None:
    _assert(_price_status("202609", "202608") == "ready", "latest completed month")
    _assert(_price_status("202609", "202603") == "ready", "six-month price")
    _assert(_price_status("202609", "202602") == "stale", "seven-month price")
    _assert(_price_status("202609", "202509") == "stale", "twelve-month price")
    _assert(_price_status("202609", "202508") == "unavailable", "lookback exceeded")


def test_statistics_checksum_covers_additive_fields() -> None:
    snapshot = _snapshot()
    rows, headers = build_relational_frequency_projection(snapshot)
    validate_relational_frequency_projection(rows=rows, headers=headers, require_complete=True, key=snapshot.key)
    rows[0]["return_supply_amount_3m"] = Decimal("999")
    try:
        validate_relational_frequency_projection(rows=rows, headers=headers, require_complete=True, key=snapshot.key)
    except SnapshotContractError:
        pass
    else:
        raise AssertionError("statistics tampering must invalidate row checksum")


def test_sql_decimal_storage_round_trip_checksum() -> None:
    snapshot = _snapshot()
    key = snapshot.key
    columns = frequency_product_columns(key)
    source = dict(snapshot.frequency_products[0])
    source.update({
        "return_supply_amount_3m": Decimal("1.2345675"),
        "avg_purchase_unit_cost": Decimal("2063.637500000049"),
        "avg_sales_unit_price": Decimal("2311.702702000051"),
        "estimated_unit_profit": Decimal("-248.06520200005"),
        "estimated_profit_rate": Decimal("-0.1073084362385"),
        "estimated_contribution_amount": Decimal("33736.8674715"),
    })
    stored = canonicalize_frequency_product_storage_row(key, source)
    _assert(stored["return_supply_amount_3m"] == Decimal("1.234568"), "positive half-up round")
    _assert(stored["estimated_profit_rate"] == Decimal("-0.107308436239"), "negative half-up round")
    _assert(stored["estimated_contribution_amount"] == Decimal("33736.867472"), "incident contribution scale")
    _assert(
        stored["avg_purchase_unit_cost"] == Decimal("2063.6375000000")
        and stored["avg_sales_unit_price"] == Decimal("2311.7027020001"),
        "unit price scales",
    )

    pre_insert = relational_row_checksum(
        "frequency_product", columns, tuple(stored[column] for column in columns)
    )
    sql_round_trip = dict(stored)
    for column, (_precision, scale) in PRODUCT_STATISTICS_DECIMAL_STORAGE.items():
        value = stored[column]
        sql_round_trip[column] = None if value is None else Decimal(f"{value:.{scale}f}")
    post_read = relational_row_checksum(
        "frequency_product", columns, tuple(sql_round_trip[column] for column in columns)
    )
    _assert(pre_insert == post_read, "SQL DECIMAL round-trip row checksum")

    zero_null = dict(source)
    zero_null.update({
        "return_supply_amount_3m": Decimal("0"),
        "avg_purchase_unit_cost": None,
        "avg_sales_unit_price": Decimal("0.00000000000"),
        "estimated_unit_profit": None,
        "estimated_profit_rate": None,
        "estimated_contribution_amount": Decimal("0.0000000"),
    })
    canonical_zero_null = canonicalize_frequency_product_storage_row(key, zero_null)
    _assert(canonical_zero_null["return_supply_amount_3m"] == Decimal("0.000000"), "zero scale")
    _assert(canonical_zero_null["avg_purchase_unit_cost"] is None, "NULL preserved")
    _assert(canonical_zero_null["estimated_unit_profit"] is None, "profit NULL preserved")
    _assert(
        canonicalize_product_statistics_decimal("estimated_profit_rate", "1.2300000000000")
        == Decimal("1.230000000000"),
        "trailing zero canonicalization",
    )


def test_sql_decimal_storage_collapse_matrix() -> None:
    snapshot = _snapshot()
    key = snapshot.key
    columns = frequency_product_columns(key)

    for column, (precision, scale) in PRODUCT_STATISTICS_DECIMAL_STORAGE.items():
        quantum = Decimal(1).scaleb(-scale)
        tiny = Decimal(4).scaleb(-(scale + 1))
        midpoint = Decimal(5).scaleb(-(scale + 1))
        cases = (
            None,
            Decimal(0),
            Decimal("-0"),
            Decimal(0).scaleb(-scale),
            Decimal("-0").scaleb(-scale),
            tiny,
            -tiny,
            midpoint,
            -midpoint,
            Decimal("1"),
            Decimal("1.0"),
            Decimal("1.000000"),
            Decimal("1.230000000000000000"),
            Decimal("1.234567890123456789"),
            Decimal("-1.234567890123456789"),
            quantum,
            -quantum,
        )
        for source in cases:
            canonical = canonicalize_product_statistics_decimal(column, source)
            round_trip = _sql_server_decimal_read(canonical, scale)
            _assert(canonical == round_trip, f"{column} SQL numeric round-trip: {source!r}")
            if canonical is not None and canonical.is_zero():
                _assert(not canonical.is_signed(), f"{column} signed zero must collapse")
                _assert(canonical.as_tuple().exponent == -scale, f"{column} zero scale")

        _assert(
            canonicalize_product_statistics_decimal(column, midpoint) == quantum
            and canonicalize_product_statistics_decimal(column, -midpoint) == -quantum,
            f"{column} midpoint ROUND_HALF_UP",
        )
        maximum = Decimal(f"{'9' * (precision - scale)}.{'9' * scale}")
        _assert(canonicalize_product_statistics_decimal(column, maximum) == maximum, f"{column} precision maximum")
        try:
            canonicalize_product_statistics_decimal(column, Decimal(1).scaleb(precision - scale))
        except SnapshotContractError:
            pass
        else:
            raise AssertionError(f"{column} precision overflow must fail closed")

    incident = dict(snapshot.frequency_products[0])
    incident["estimated_unit_profit"] = Decimal("-248.0652020000")
    incident["outbound_qty_3m"] = 0
    incident["estimated_contribution_amount"] = (
        incident["estimated_unit_profit"] * incident["outbound_qty_3m"]
    )
    canonical = canonicalize_frequency_product_storage_row(key, incident)
    _assert(
        canonical["estimated_contribution_amount"] == Decimal("0.000000")
        and not canonical["estimated_contribution_amount"].is_signed(),
        "negative unit profit times zero must use SQL positive zero",
    )
    pre_insert = relational_row_checksum(
        "frequency_product", columns, tuple(canonical[column] for column in columns)
    )
    post_read_row = dict(canonical)
    for column, (_precision, scale) in PRODUCT_STATISTICS_DECIMAL_STORAGE.items():
        post_read_row[column] = _sql_server_decimal_read(canonical[column], scale)
    post_read = relational_row_checksum(
        "frequency_product", columns, tuple(post_read_row[column] for column in columns)
    )
    _assert(pre_insert == post_read, "incident pre-insert/post-read checksum equality")


def test_repository_inserts_the_checksummed_storage_values() -> None:
    snapshot = _snapshot()
    raw_first = dict(snapshot.frequency_products[0])
    raw_first["estimated_profit_rate"] = Decimal("0.3333333333334")
    raw_first["estimated_unit_profit"] = Decimal("-248.0652020000")
    raw_first["outbound_qty_3m"] = 0
    raw_first["estimated_contribution_amount"] = Decimal("-248.0652020000") * 0
    raw_rows = (raw_first, *snapshot.frequency_products[1:])
    raw_checksum = calculate_relational_frequency_checksum(
        key=snapshot.key,
        basis_from=snapshot.basis_from,
        basis_to=snapshot.basis_to,
        scope_mode=snapshot.scope_mode,
        stock_codes=snapshot.stock_codes,
        source_watermark=snapshot.source_watermark,
        source_watermark_status=snapshot.source_watermark_status,
        source_fingerprint=snapshot.source_fingerprint,
        source_contract=snapshot.source_contract,
        source_diagnostics=snapshot.source_diagnostics,
        monthly_activity=snapshot.monthly_activity,
        frequency_products=raw_rows,
    )
    raw_snapshot = replace(
        snapshot,
        frequency_products=raw_rows,
        checksum=raw_checksum,
    )

    class Cursor:
        def __init__(self) -> None:
            self.row = None
            self.batches: list[tuple[str, list[tuple[object, ...]]]] = []

        def execute(self, sql: str, *_params: object):
            self.row = (77,) if "snapshot.publish.relational.manifest" in sql else None
            return self

        def executemany(self, sql: str, params: list[tuple[object, ...]]):
            self.batches.append((sql, params))
            return self

        def fetchone(self):
            return self.row

    class Connection:
        def __init__(self) -> None:
            self.writer = Cursor()

        def cursor(self):
            return self.writer

        def commit(self) -> None:
            pass

        def rollback(self) -> None:
            pass

        def close(self) -> None:
            pass

    connection = Connection()
    repository = SqlServerSnapshotRepository(
        reader_connection_factory=lambda: connection,
        writer_connection_factory=lambda: connection,
    )
    repository.publish_relational(raw_snapshot, created_by="fixture")
    sql, params = next(
        batch for batch in connection.writer.batches
        if "snapshot.frequency_product" in batch[0]
    )
    _assert("estimated_profit_rate" in sql and "estimated_contribution_amount" in sql, "statistics insert columns")
    columns = frequency_product_columns(snapshot.key)
    inserted = params[0]
    values = inserted[1:-1]
    _assert(values[columns.index("estimated_profit_rate")] == Decimal("0.333333333333"), "insert profit rate canonical")
    inserted_contribution = values[columns.index("estimated_contribution_amount")]
    _assert(
        inserted_contribution == Decimal("0.000000") and not inserted_contribution.is_signed(),
        "insert contribution uses SQL positive zero",
    )
    _assert(
        inserted[-1] == relational_row_checksum("frequency_product", columns, values),
        "insert checksum uses identical canonical values",
    )
    product_params = next(
        params for sql, params in connection.writer.batches
        if "snapshot.frequency_product" in sql
    )
    round_trip_rows = []
    for stored in product_params:
        stored_row = dict(zip(columns, stored[1:-1]))
        for column, (_precision, scale) in PRODUCT_STATISTICS_DECIMAL_STORAGE.items():
            stored_row[column] = _sql_server_decimal_read(stored_row[column], scale)
        round_trip_rows.append({**stored_row, "row_checksum": stored[-1]})
    projection_params = next(
        params for sql, params in connection.writer.batches
        if "snapshot.frequency_projection" in sql
    )
    round_trip_headers = [
        {
            "frequency_grade": row[1],
            "expected_product_count": row[2],
            "projection_checksum": row[3],
        }
        for row in projection_params
    ]
    validate_relational_frequency_projection(
        rows=round_trip_rows,
        headers=round_trip_headers,
        require_complete=True,
        key=snapshot.key,
    )


def test_multi_projection_event_stream() -> None:
    rows = pd.DataFrame([
        {"row_kind": "event", "outbound_date": "20260801", "vendor_code": "000001", "product_code": "00001", "stock_code": "00001", "outbound_quantity": 10, "paid_quantity": 8, "mapping_count": 1, "exact_duplicate_row_count": 0},
        {"row_kind": "sales_price", "product_code": "00001", "basis_month": "202608", "unit_price": "150"},
        {"row_kind": "return_stats", "product_code": "00001", "return_event_count": 1, "return_quantity": 2, "return_supply_amount": "300"},
        {"row_kind": "diagnostics", "source_row_count": 2, "normal_positive_row_count": 1, "normal_positive_missing_key_row_count": 0, "normal_positive_nonintegral_row_count": 0, "normal_nonpositive_row_count": 0, "return_positive_row_count": 0, "return_nonpositive_row_count": 1, "other_tcode_row_count": 0},
    ])
    monthly, diagnostics, days, customers, paid, returns, prices = _aggregate_product_statistics_event_chunks((rows,))
    _assert(monthly[0]["occurrence_count"] == 1 and diagnostics["source_row_count"] == 2, "frequency projection")
    _assert(days == {"00001": 1} and customers == {"00001": 1}, "distinct projections")
    _assert(paid == {"00001": 8}, "paid quantity projection")
    _assert(returns["00001"]["supply_amount"] == "300", "return projection")
    _assert(prices["00001"]["basis_month"] == "202608", "sales price projection")


def test_sql_and_migration_contract() -> None:
    plan = build_frequency_snapshot_plan(company_id=7, evaluation_month="202609", stock_codes=("00001",))
    sql, binds = product_statistics_event_stream_sql(plan)
    _assert("Rddbc120" in sql and "ReturnProduct" in sql and "LatestSalesPrice" in sql, "R120 projections")
    _assert(binds["price_lookback_from"] == "20250901", "12-month lookback")
    required = (
        "outbound_qty_3m", "outbound_paid_qty_3m", "return_event_count_3m", "return_qty_3m",
        "return_supply_amount_3m", "avg_purchase_unit_cost", "avg_sales_unit_price",
        "estimated_unit_profit", "estimated_profit_rate", "profit_grade",
        "estimated_contribution_amount", "contribution_grade", "profitability_status",
    )
    _assert(all(column in MIGRATION_008_SQL for column in required), "migration columns")
    _assert(MIGRATIONS[-1].migration_id == "008_frequency_product_statistics_extension", "migration order")


def main() -> int:
    tests = (
        test_product_statistics_and_profitability,
        test_price_status_boundaries,
        test_statistics_checksum_covers_additive_fields,
        test_sql_decimal_storage_round_trip_checksum,
        test_sql_decimal_storage_collapse_matrix,
        test_repository_inserts_the_checksummed_storage_values,
        test_multi_projection_event_stream,
        test_sql_and_migration_contract,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS snapshot product statistics extension {len(tests)}/{len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
