from __future__ import annotations

import copy
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
    _relational_projection_digest,
    _relational_projection_digest_from_canonical_rows,
    _cumulative_amount_grades,
    _profit_rate_grade,
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
    _PRODUCT_STATISTICS_STREAM_COLUMNS,
    _aggregate_product_statistics_event_chunks,
    _python_product_statistics_projection_chunks,
    _price_status,
    build_frequency_snapshot_plan,
    product_statistics_event_stream_sql,
    product_statistics_raw_event_stream_sql,
)
from app.services.ssai_analytics_snapshot_migration import MIGRATION_008_SQL, MIGRATION_009_SQL, MIGRATIONS  # noqa: E402
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
    _assert(first["profit_grade"] == "A" and first["contribution_grade"] == "A", "independent grade policies")
    second = rows["00002"]
    _assert(second["frequency_grade"] == "F" and second["outbound_qty_3m"] == 5, "F raw statistics")
    _assert(second["profitability_status"] == "excluded_adjustment_only", "adjustment exclusion")
    _assert(
        second["profit_grade"] == second["contribution_grade"] == "unavailable",
        "adjustment grade exclusion",
    )
    stale = rows["00003"]
    _assert(stale["profitability_status"] == "stale", "stale profitability")
    _assert(
        stale["profit_grade"] == stale["contribution_grade"] == "unavailable",
        "stale profitability must not expose X",
    )


def test_price_status_boundaries() -> None:
    _assert(_price_status("202609", "202608") == "ready", "latest completed month")
    _assert(_price_status("202609", "202603") == "ready", "six-month price")
    _assert(_price_status("202609", "202602") == "stale", "seven-month price")
    _assert(_price_status("202609", "202509") == "stale", "twelve-month price")
    _assert(_price_status("202609", "202508") == "unavailable", "lookback exceeded")


def test_cumulative_amount_grade_boundaries_and_nonpositive() -> None:
    grades = _cumulative_amount_grades({
        "a": Decimal(80), "b": Decimal(10), "c": Decimal(5),
        "d": Decimal(4), "e": Decimal(1), "zero": Decimal(0), "loss": Decimal(-2),
    })
    _assert([grades[key] for key in ("a", "b", "c", "d", "e")] == list("ABCDE"), "amount boundary grades")
    _assert(grades["zero"] == grades["loss"] == "E", "nonpositive management grade")
    crossing = _cumulative_amount_grades({"large": Decimal(82), "next": Decimal(10), "last": Decimal(8)})
    _assert(crossing == {"large": "A", "next": "B", "last": "C"}, "crossing item belongs to starting band")
    tied = _cumulative_amount_grades({"first": Decimal(50), "tie1": Decimal(25), "tie2": Decimal(25)})
    _assert(tied["tie1"] == tied["tie2"], "equal amounts must not split")
    _assert(_cumulative_amount_grades({"zero": Decimal(0), "loss": Decimal(-1)}) == {"zero": "E", "loss": "E"}, "no positive denominator")
    _assert(_cumulative_amount_grades({"positive": Decimal(1), "zero": Decimal(0)}, nonpositive_grade="X") == {"positive": "A", "zero": "X"}, "new contribution X")
    _assert([_profit_rate_grade(Decimal(value)) for value in ("0.30", "0.20", "0.10", "0.05", "0.001", "0", "-0.01")] == list("ABCDEXX"), "absolute profit rate boundaries")


def test_profit_and_contribution_use_distinct_quantity_sources() -> None:
    snapshot = build_product_statistics_relational_snapshot_from_aggregates(
        company_id="07", evaluation_month="202609",
        monthly_rows=(
            {"month": "202608", "product_code": "00001", "stock_code": "00001", "occurrence_count": 1, "outbound_quantity": 100, "outbound_day_count": 1},
            {"month": "202608", "product_code": "00002", "stock_code": "00001", "occurrence_count": 1, "outbound_quantity": 1, "outbound_day_count": 1},
        ),
        product_codes=("00001", "00002"), product_day_counts={}, product_customer_counts={},
        first_normal_inbound_months={}, outbound_paid_quantities={"00001": 1, "00002": 1},
        return_statistics={},
        purchase_prices={code: {"unit_price": "10", "status": "ready"} for code in ("00001", "00002")},
        sales_prices={"00001": {"unit_price": "11", "status": "ready"}, "00002": {"unit_price": "20", "status": "ready"}},
        stock_codes=("00001",),
    )
    rows = {row["product_code"]: row for row in snapshot.frequency_products}
    _assert(rows["00001"]["profit_grade"] == "D" and rows["00001"]["contribution_grade"] == "A", "profit rate must ignore quantity")
    _assert(rows["00002"]["profit_grade"] == "A" and rows["00002"]["contribution_grade"] == "C", "contribution must include quantity")


def test_nonpositive_and_return_only_x() -> None:
    codes = ("P1", "P2", "P3", "P4")
    snapshot = build_product_statistics_relational_snapshot_from_aggregates(
        company_id="07", evaluation_month="202609",
        monthly_rows=tuple(
            {"month": "202608", "product_code": code, "stock_code": "00001", "occurrence_count": 1, "outbound_quantity": 1, "outbound_day_count": 1}
            for code in codes[:3]
        ),
        product_codes=codes, product_day_counts={}, product_customer_counts={},
        first_normal_inbound_months={}, outbound_paid_quantities={},
        return_statistics={"P4": {"event_count": 1, "quantity": 2, "supply_amount": "10"}},
        purchase_prices={code: {"unit_price": cost, "status": "ready"} for code, cost in zip(codes, ("5", "10", "12", "5"))},
        sales_prices={code: {"unit_price": "10", "status": "ready"} for code in codes},
        stock_codes=("00001",),
    )
    validate_relational_frequency_snapshot(snapshot)
    rows = {row["product_code"]: row for row in snapshot.frequency_products}
    positive = rows["P1"]
    _assert(
        positive["estimated_profit_rate"] > 0
        and positive["outbound_qty_3m"] > 0
        and positive["profit_grade"] != "X",
        "positive ready non-return product must not receive profit X",
    )
    _assert(positive["profit_grade"] == positive["contribution_grade"] == "A", "positive ready grade")
    _assert(all(rows[code][field] == "X" for code in ("P2", "P3", "P4") for field in ("profit_grade", "contribution_grade")), "zero, loss, and return-only X")
    return_only = rows["P4"]
    _assert(
        return_only["profitability_status"] == "ready"
        and return_only["estimated_profit_rate"] > 0
        and return_only["outbound_qty_3m"] == 0
        and return_only["return_qty_3m"] == 2,
        "return-only X exception authority",
    )
    _assert(
        return_only["estimated_contribution_amount"] == 0
        and return_only["profit_grade"] == return_only["contribution_grade"] == "X",
        "return-only must receive profit and contribution X",
    )

    unavailable = build_product_statistics_relational_snapshot_from_aggregates(
        company_id="07", evaluation_month="202609", monthly_rows=(),
        product_codes=("P5",), product_day_counts={}, product_customer_counts={},
        first_normal_inbound_months={}, outbound_paid_quantities={}, return_statistics={},
        purchase_prices={}, sales_prices={}, stock_codes=("00001",),
    ).frequency_products[0]
    _assert(
        unavailable["profitability_status"] == "unavailable"
        and unavailable["profit_grade"] == unavailable["contribution_grade"] == "unavailable",
        "unavailable profitability must not expose X",
    )


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


def test_validation_reuses_canonical_rows_without_changing_integrity() -> None:
    snapshot = _snapshot()
    key = snapshot.key
    columns = frequency_product_columns(key)
    rows, headers = build_relational_frequency_projection(snapshot)
    raw_rows = copy.deepcopy(rows)
    raw_rows[0].update({
        "return_supply_amount_3m": Decimal("-0.0000004"),
        "avg_purchase_unit_cost": Decimal("100.0000000000000"),
        "avg_sales_unit_price": Decimal("-12.5000000000000"),
        "estimated_unit_profit": None,
        "estimated_profit_rate": Decimal("-0.0000000000004"),
        "estimated_contribution_amount": None,
    })
    for row in raw_rows:
        canonical = canonicalize_frequency_product_storage_row(key, row)
        row["row_checksum"] = relational_row_checksum(
            "frequency_product", columns, tuple(canonical[column] for column in columns)
        )
    rebuilt_headers = [
        {
            "frequency_grade": header["frequency_grade"],
            "expected_product_count": header["expected_product_count"],
            "projection_checksum": _relational_projection_digest(
                (row for row in raw_rows if row["frequency_grade"] == header["frequency_grade"]),
                key=key,
            ),
        }
        for header in headers
    ]

    validated = validate_relational_frequency_projection(
        rows=raw_rows, headers=rebuilt_headers, require_complete=True, key=key
    )
    _assert(
        [row["product_code"] for row in validated] == [row["product_code"] for row in raw_rows],
        "validation must retain canonical row sequence",
    )
    for header in rebuilt_headers:
        grade = header["frequency_grade"]
        raw_grade = [row for row in raw_rows if row["frequency_grade"] == grade]
        canonical_grade = [row for row in validated if row["frequency_grade"] == grade]
        old_digest = _relational_projection_digest(raw_grade, key=key)
        new_digest = _relational_projection_digest_from_canonical_rows(
            canonical_grade, columns=columns
        )
        _assert(old_digest == new_digest == header["projection_checksum"], f"{grade} digest exact equality")
    _assert(
        validated[0]["return_supply_amount_3m"] == Decimal("0.000000")
        and not validated[0]["return_supply_amount_3m"].is_signed(),
        "signed zero must remain canonical",
    )
    _assert(validated[0]["estimated_unit_profit"] is None, "null decimal must remain canonical")
    _assert(validated[0]["avg_sales_unit_price"] == Decimal("-12.5000000000"), "negative decimal sign and scale")

    checksum_corrupt = copy.deepcopy(raw_rows)
    checksum_corrupt[0]["row_checksum"] = "0" * 64
    try:
        validate_relational_frequency_projection(
            rows=checksum_corrupt, headers=rebuilt_headers, require_complete=True, key=key
        )
    except SnapshotContractError:
        pass
    else:
        raise AssertionError("row checksum mismatch must fail closed")

    digest_corrupt = copy.deepcopy(rebuilt_headers)
    digest_corrupt[0]["projection_checksum"] = "0" * 64
    try:
        validate_relational_frequency_projection(
            rows=raw_rows, headers=digest_corrupt, require_complete=True, key=key
        )
    except SnapshotContractError:
        pass
    else:
        raise AssertionError("projection digest mismatch must fail closed")

    duplicate = copy.deepcopy(raw_rows)
    duplicate.append(copy.deepcopy(raw_rows[0]))
    try:
        validate_relational_frequency_projection(
            rows=duplicate, headers=rebuilt_headers, require_complete=True, key=key
        )
    except SnapshotContractError:
        pass
    else:
        raise AssertionError("duplicate product code must fail closed")


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
    split = _aggregate_product_statistics_event_chunks((rows.iloc[:1], rows.iloc[1:3], rows.iloc[3:]))
    _assert(split == (monthly, diagnostics, days, customers, paid, returns, prices), "chunk boundaries must not change product statistics")


def test_python_source2_projection_fixture() -> None:
    raw = pd.DataFrame([
        {"outbound_date": "20250901", "vendor_code": "V0", "outbound_seq": "1", "product_code": "P1", "stock_code": "S1", "io_gcode": "0012", "io_tcode": "500", "quantity": Decimal("2"), "oquantity": 0, "supply_price": 200, "final_supply_price": None},
        {"outbound_date": "20260801", "vendor_code": "V1", "outbound_seq": "1", "product_code": "P1", "stock_code": "S1", "io_gcode": "0012", "io_tcode": "500", "quantity": Decimal("8"), "oquantity": 2, "supply_price": 1100, "final_supply_price": 1200},
        {"outbound_date": "20260801", "vendor_code": "V1", "outbound_seq": "1", "product_code": "P1", "stock_code": "S1", "io_gcode": "0012", "io_tcode": "500", "quantity": Decimal("8"), "oquantity": 2, "supply_price": 1100, "final_supply_price": 1200},
        {"outbound_date": "20260802", "vendor_code": "V2", "outbound_seq": "2", "product_code": "P2", "stock_code": "S1", "io_gcode": "0012", "io_tcode": "501", "quantity": 1, "oquantity": 0, "supply_price": 10, "final_supply_price": None},
        {"outbound_date": "20260802", "vendor_code": "V2", "outbound_seq": "2", "product_code": "P3", "stock_code": "S1", "io_gcode": "0012", "io_tcode": "501", "quantity": 1, "oquantity": 0, "supply_price": 20, "final_supply_price": None},
        {"outbound_date": "20260803", "vendor_code": "", "outbound_seq": "3", "product_code": "P1", "stock_code": "S1", "io_gcode": "0012", "io_tcode": "599", "quantity": 1, "oquantity": 0, "supply_price": 10, "final_supply_price": None},
        {"outbound_date": "20260804", "vendor_code": "V4", "outbound_seq": "4", "product_code": "P1", "stock_code": "S1", "io_gcode": "0012", "io_tcode": "599", "quantity": Decimal("1.5"), "oquantity": 0, "supply_price": 10, "final_supply_price": None},
        {"outbound_date": "20260805", "vendor_code": "V5", "outbound_seq": "5", "product_code": "P1", "stock_code": "S1", "io_gcode": "0012", "io_tcode": "500", "quantity": 0, "oquantity": 0, "supply_price": 0, "final_supply_price": None},
        {"outbound_date": "20260806", "vendor_code": "V6", "outbound_seq": "6", "product_code": "P1", "stock_code": "S1", "io_gcode": "0012", "io_tcode": "600", "quantity": -2, "oquantity": 0, "supply_price": -300, "final_supply_price": None},
        {"outbound_date": "20260807", "vendor_code": "V7", "outbound_seq": "7", "product_code": "P1", "stock_code": "S1", "io_gcode": "0012", "io_tcode": "600", "quantity": 1, "oquantity": 0, "supply_price": 100, "final_supply_price": None},
        {"outbound_date": "20260808", "vendor_code": "V8", "outbound_seq": "8", "product_code": "P1", "stock_code": "S1", "io_gcode": "0012", "io_tcode": "A50", "quantity": 1, "oquantity": 0, "supply_price": 100, "final_supply_price": None},
    ])
    raw["outbound_quantity"] = raw.apply(lambda row: Decimal(str(row["quantity"])) + Decimal(str(row["oquantity"])), axis=1)
    raw["paid_quantity"] = raw["quantity"].map(lambda value: Decimal(str(value)))
    raw["supply_amount"] = raw.apply(
        lambda row: Decimal(str(row["final_supply_price"] if pd.notna(row["final_supply_price"]) else row["supply_price"])),
        axis=1,
    )
    raw = raw.drop(columns=["quantity", "oquantity", "supply_price", "final_supply_price"])
    projected_records = []
    for chunk in _python_product_statistics_projection_chunks(
        (raw.iloc[:3], raw.iloc[3:]), basis_from="20260601", output_chunk_rows=1
    ):
        projected_records.extend(chunk.to_dict("records"))
    projected = pd.DataFrame.from_records(projected_records, columns=_PRODUCT_STATISTICS_STREAM_COLUMNS)
    _assert(tuple(projected.columns) == _PRODUCT_STATISTICS_STREAM_COLUMNS, "Python Source-2 output column contract")
    events = projected.loc[projected["row_kind"].eq("event")].sort_values("outbound_date")
    _assert(len(events) == 2, "Python Source-2 event grain")
    first = events.iloc[0]
    _assert(first["product_code"] == "P1" and first["mapping_count"] == 1 and first["exact_duplicate_row_count"] == 1, "Python Source-2 exact duplicate")
    conflict = events.iloc[1]
    _assert(conflict["product_code"] == "P3" and conflict["mapping_count"] == 2, "Python Source-2 mapping conflict")
    prices = projected.loc[projected["row_kind"].eq("sales_price")]
    _assert(len(prices) == 1 and prices.iloc[0]["basis_month"] == "202608" and prices.iloc[0]["unit_price"] == Decimal("150.0000000000"), "Python Source-2 latest sales price")
    returns = projected.loc[projected["row_kind"].eq("return_stats")]
    _assert(len(returns) == 1 and returns.iloc[0]["return_quantity"] == Decimal("2.000000") and returns.iloc[0]["return_supply_amount"] == Decimal("300.000000"), "Python Source-2 return")
    diagnostic = projected.loc[projected["row_kind"].eq("diagnostics")].iloc[0]
    expected = {
        "source_row_count": 10, "normal_positive_row_count": 6,
        "normal_positive_missing_key_row_count": 1, "normal_positive_nonintegral_row_count": 1,
        "normal_nonpositive_row_count": 1, "return_positive_row_count": 1,
        "return_nonpositive_row_count": 1, "other_tcode_row_count": 1,
    }
    _assert(all(int(diagnostic[key]) == value for key, value in expected.items()), "Python Source-2 diagnostics")
    sql, binds = product_statistics_raw_event_stream_sql(
        build_frequency_snapshot_plan(company_id=7, evaluation_month="202609", stock_codes=("00001",))
    )
    _assert(sql.count("Rddbc120") == 1 and "GROUP BY" not in sql and "UNION ALL" not in sql, "raw Source-2 remains one simple extraction")
    _assert(binds["basis_from"] == "20260601" and binds["price_lookback_from"] == "20250901", "raw Source-2 bounds")


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
    _assert(MIGRATIONS[-1].migration_id == "009_frequency_product_x_grade", "migration order")
    _assert("DROP CONSTRAINT CK_snapshot_frequency_product_profit_grade" in MIGRATION_009_SQL and "'X'" in MIGRATION_009_SQL, "versioned X grade migration")


def test_io_classification_candidate_a_contract() -> None:
    values = ("500", "501", "599", "600", "601", "699", "499", "700", "050", "5A0", "A50", "", "   ", "500   ", " 500", "5 0")
    for io_gcode in ("0012", "0013"):
        for raw_value in values:
            value = raw_value.strip()
            safe_integer = int(value) if len(value) == 3 and value.isascii() and value.isdigit() else None
            old_normal = io_gcode == "0012" and safe_integer is not None and 500 <= safe_integer <= 599
            old_return = io_gcode == "0012" and safe_integer is not None and 600 <= safe_integer <= 699
            candidate_common = io_gcode == "0012" and len(value) == 3 and value.isascii() and value.isdigit()
            candidate_normal = candidate_common and value[0] == "5"
            candidate_return = candidate_common and value[0] == "6"
            _assert(old_normal == candidate_normal, f"normal IO contract changed: {io_gcode!r}, {raw_value!r}")
            _assert(old_return == candidate_return, f"return IO contract changed: {io_gcode!r}, {raw_value!r}")
    plan = build_frequency_snapshot_plan(company_id=7, evaluation_month="202609", stock_codes=("00001",))
    sql, _ = product_statistics_event_stream_sql(plan)
    _assert("LEFT(io_tcode,1)='5'" in sql and "LEFT(io_tcode,1)='6'" in sql, "candidate A SQL missing")
    _assert("sql_safe_int(\"io_tcode\")" not in sql, "safe integer range conversion remained in Source-2")


def main() -> int:
    tests = (
        test_product_statistics_and_profitability,
        test_price_status_boundaries,
        test_cumulative_amount_grade_boundaries_and_nonpositive,
        test_profit_and_contribution_use_distinct_quantity_sources,
        test_nonpositive_and_return_only_x,
        test_statistics_checksum_covers_additive_fields,
        test_validation_reuses_canonical_rows_without_changing_integrity,
        test_sql_decimal_storage_round_trip_checksum,
        test_sql_decimal_storage_collapse_matrix,
        test_repository_inserts_the_checksummed_storage_values,
        test_multi_projection_event_stream,
        test_python_source2_projection_fixture,
        test_sql_and_migration_contract,
        test_io_classification_candidate_a_contract,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS snapshot product statistics extension {len(tests)}/{len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
