from __future__ import annotations

import calendar
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import Any, Iterable, Mapping, Sequence

from app.services.ssai_snapshot_repository import (
    SNAPSHOT_STATUS_CORRUPT,
    SNAPSHOT_STATUS_MISSING,
    SNAPSHOT_STATUS_READY,
    SNAPSHOT_STATUS_STALE,
    SNAPSHOT_STATUS_VERSION_MISMATCH,
    SnapshotKey,
    SnapshotReadResult,
)
from app.sims.meta.rddbc_io_meta import normalize_io_gu


SNAPSHOT_TYPE = "dashboard_inventory_outbound_frequency"
SCHEMA_VERSION = "1.0"
ALGORITHM_VERSION = "outbound_frequency_v1"
EXTENDED_SCHEMA_VERSION = "2.0"
EXTENDED_ALGORITHM_VERSION = "outbound_frequency_v2"
PRODUCT_STATISTICS_SCHEMA_VERSION = "2.1"
PRODUCT_STATISTICS_ALGORITHM_VERSION = "outbound_frequency_product_statistics_v1"
PRODUCT_STATISTICS_DECIMAL_STORAGE = {
    "return_supply_amount_3m": (38, 6),
    "avg_purchase_unit_cost": (38, 10),
    "avg_sales_unit_price": (38, 10),
    "estimated_unit_profit": (38, 10),
    "estimated_profit_rate": (38, 12),
    "estimated_contribution_amount": (38, 6),
}
FREQUENCY_INSUFFICIENT_GRADE = "빈도자료 부족"
FREQUENCY_GRADES = ("A", "B", "C", "D", "E")

_EVENT_KEY_FIELDS = ("outbound_date", "vendor_code", "outbound_seq")
_NON_EXCLUSION_FLAG_FIELDS = (
    "fixed_flag",
    "record_flag",
    "reform_flag",
    "validation",
)


class SnapshotContractError(ValueError):
    pass


FREQUENCY_PROJECTION_GRADES = (*FREQUENCY_GRADES, "X")
EXTENDED_FREQUENCY_PROJECTION_GRADES = ("F", *FREQUENCY_PROJECTION_GRADES)
RELATIONAL_FREQUENCY_REPRESENTATION = "relational_frequency_v1"
EXTENDED_RELATIONAL_FREQUENCY_REPRESENTATION = "relational_frequency_v2"
LEGACY_JSON_REPRESENTATION = "legacy_json_v1"
RELATIONAL_DIAGNOSTIC_FIELDS = (
    "source_row_count",
    "normal_positive_accepted_row_count",
    "normal_positive_duplicate_row_count",
    "normal_positive_conflicting_row_count",
    "normal_positive_missing_key_row_count",
    "normal_positive_nonintegral_row_count",
    "normal_nonpositive_row_count",
    "return_positive_row_count",
    "return_nonpositive_row_count",
    "other_tcode_row_count",
    "normal_positive_row_count",
    "distinct_normal_event_count",
    "conflicting_event_count",
    "ignored_product_event_count",
)
RELATIONAL_PARTITION_FIELDS = RELATIONAL_DIAGNOSTIC_FIELDS[1:10]


@dataclass(frozen=True)
class FrequencyProjectionReadResult:
    """Read-model result for approved product-frequency lookups.

    This is deliberately separate from SnapshotReadResult: projection rows are
    derived read data, while the full payload remains the approval authority.
    """

    status: str
    rows: tuple[Mapping[str, Any], ...] = ()
    reason: str = ""
    manifest_id: int | None = None
    generation_no: int | None = None
    checksum: str = ""
    authority_status: str = ""
    resolution_status: str = ""
    contract_version: str = ""

    @property
    def usable(self) -> bool:
        return self.status == SNAPSHOT_STATUS_READY


@dataclass(frozen=True)
class RelationalFrequencySnapshot:
    """Typed authority data for a payload-less frequency generation."""

    key: SnapshotKey
    basis_from: str
    basis_to: str
    scope_mode: str
    stock_codes: tuple[str, ...]
    source_watermark: str | None
    source_watermark_status: str
    source_fingerprint: str
    source_contract: Mapping[str, Any]
    source_diagnostics: Mapping[str, Any]
    monthly_activity: tuple[Mapping[str, Any], ...]
    frequency_products: tuple[Mapping[str, Any], ...]
    checksum: str

    @property
    def item_count(self) -> int:
        return len(self.frequency_products)


@dataclass(frozen=True)
class CompletedMonthBasis:
    evaluation_month: str
    months: tuple[str, str, str]
    basis_from: str
    basis_to: str


def _parse_month(value: Any) -> tuple[int, int]:
    text = str(value or "").strip()
    if len(text) != 6 or not text.isdecimal():
        raise SnapshotContractError(f"invalid evaluation_month: {text!r}")
    year, month = int(text[:4]), int(text[4:])
    if year < 1 or not 1 <= month <= 12:
        raise SnapshotContractError(f"invalid evaluation_month: {text!r}")
    return year, month


def _shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    ordinal = year * 12 + month - 1 + offset
    shifted_year, shifted_zero_month = divmod(ordinal, 12)
    return shifted_year, shifted_zero_month + 1


def completed_month_basis(evaluation_month: Any) -> CompletedMonthBasis:
    year, month = _parse_month(evaluation_month)
    completed = tuple(
        f"{shifted_year:04d}{shifted_month:02d}"
        for shifted_year, shifted_month in (
            _shift_month(year, month, offset) for offset in (-3, -2, -1)
        )
    )
    last_year, last_month = _parse_month(completed[-1])
    last_day = calendar.monthrange(last_year, last_month)[1]
    return CompletedMonthBasis(
        evaluation_month=f"{year:04d}{month:02d}",
        months=completed,
        basis_from=f"{completed[0]}01",
        basis_to=f"{completed[-1]}{last_day:02d}",
    )


def is_normal_outbound_tcode(io_gu_gcode: Any, io_tcode: Any) -> bool:
    normalized_group = str(io_gu_gcode or "").strip().zfill(4)
    normalized_tcode = normalize_io_gu(io_tcode)
    return (
        normalized_group == "0012"
        and len(normalized_tcode) == 3
        and normalized_tcode.isdecimal()
        and 500 <= int(normalized_tcode) <= 599
    )


def _decimal(value: Any, *, field: str) -> Decimal:
    if value in (None, ""):
        return Decimal(0)
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise SnapshotContractError(f"invalid {field}: {value!r}") from exc


def _integer_quantity(value: Decimal) -> int:
    integral = value.to_integral_value()
    if value != integral:
        raise SnapshotContractError(f"non-integral outbound quantity: {value}")
    return int(integral)


def _normalize_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    text = "".join(ch for ch in str(value or "").strip() if ch.isdecimal())
    if len(text) != 8:
        raise SnapshotContractError(f"invalid outbound_date: {value!r}")
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError as exc:
        raise SnapshotContractError(f"invalid outbound_date: {value!r}") from exc
    return text


def _normalize_sequence(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise SnapshotContractError("outbound_seq is required")
    if text.isdecimal():
        return str(int(text))
    return text


def _normalize_code(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SnapshotContractError(f"{field} is required")
    return text


def _normalized_scope(stock_codes: Iterable[Any] | None) -> tuple[str, ...]:
    return tuple(sorted({str(code).strip() for code in stock_codes or () if str(code).strip()}))


def scope_fingerprint(stock_codes: Iterable[Any] | None) -> str:
    normalized = _normalized_scope(stock_codes)
    body = {"mode": "selected", "stock_codes": list(normalized)} if normalized else {"mode": "all"}
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def dashboard_profile_fingerprint(
    *,
    stock_codes: Iterable[Any] | None,
    product_group_codes: Iterable[Any] | None = None,
    product_di_codes: Iterable[Any] | None = None,
    product_class_codes: Iterable[Any] | None = None,
    stock_mode: Any = "real",
) -> str:
    """Return the deterministic identity of the Dashboard product universe policy."""
    mode = str(stock_mode or "real").strip().lower()
    if mode not in {"real", "book"}:
        raise SnapshotContractError("stock_mode must be real or book")

    def normalized(values: Iterable[Any] | None) -> list[str]:
        return sorted({str(value).strip() for value in values or () if str(value).strip()})

    body = {
        "stock_cd_list": normalized(stock_codes),
        "product_group_list": normalized(product_group_codes),
        "product_di_list": normalized(product_di_codes),
        "product_class_list": normalized(product_class_codes),
        "stock_mode": mode,
    }
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def assign_frequency_grades(
    occurrence_counts: Mapping[str, int],
    product_codes: Iterable[Any],
) -> list[dict[str, Any]]:
    universe = tuple(sorted({_normalize_code(code, field="product_code") for code in product_codes}))
    counts = {code: max(int(occurrence_counts.get(code, 0) or 0), 0) for code in universe}
    positive_groups: dict[int, list[str]] = defaultdict(list)
    for code, count in counts.items():
        if count > 0:
            positive_groups[count].append(code)

    total_occurrences = sum(counts.values())
    cumulative_before = 0
    grades: dict[str, str] = {}
    for count in sorted(positive_groups, reverse=True):
        grade_index = min((cumulative_before * 5) // total_occurrences, 4)
        for code in positive_groups[count]:
            grades[code] = FREQUENCY_GRADES[grade_index]
        cumulative_before += count * len(positive_groups[count])

    return [
        {
            "product_code": code,
            "occurrence_count_3m": counts[code],
            "frequency_grade": grades.get(code, "X"),
            "data_status": SNAPSHOT_STATUS_READY,
        }
        for code in universe
    ]


def calculate_payload_checksum(payload: Mapping[str, Any]) -> str:
    canonical = {key: value for key, value in payload.items() if key != "checksum"}
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical_scalar(value: Any) -> bytes:
    if value is None:
        text = "null"
    elif value is True:
        text = "true"
    elif value is False:
        text = "false"
    else:
        text = str(value)
    encoded = text.encode("utf-8")
    return len(encoded).to_bytes(8, "big") + encoded


def _canonical_section(name: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> bytes:
    body = bytearray()
    body.extend(_canonical_scalar(name))
    body.extend(len(columns).to_bytes(4, "big"))
    for column in columns:
        body.extend(_canonical_scalar(column))
    normalized_rows = [tuple(row) for row in rows]
    body.extend(len(normalized_rows).to_bytes(8, "big"))
    for row in normalized_rows:
        if len(row) != len(columns):
            raise SnapshotContractError(f"canonical section {name} column count mismatch")
        for value in row:
            body.extend(_canonical_scalar(value))
    return bytes(body)


def _canonical_relational_bytes(
    *,
    key: SnapshotKey,
    basis_from: str,
    basis_to: str,
    scope_mode: str,
    stock_codes: Sequence[str],
    source_watermark: str | None,
    source_watermark_status: str,
    source_fingerprint: str,
    source_contract: Mapping[str, Any],
    source_diagnostics: Mapping[str, Any],
    monthly_activity: Sequence[Mapping[str, Any]],
    frequency_products: Sequence[Mapping[str, Any]],
) -> bytes:
    canonical_products = (
        [canonicalize_frequency_product_storage_row(key, row) for row in frequency_products]
        if _is_product_statistics_key(key)
        else list(frequency_products)
    )
    contract_columns = (
        "table", "io_gu_gcode", "normal_tcode_from", "normal_tcode_to", "event_key_fields",
        "positive_quantity_expression", "return_tcode_from", "return_tcode_to", "returns_are_netted",
        "flag_exclusion_fields", "non_exclusion_flag_fields", "universe_mode", "dashboard_product_filters",
        "include_rd04_del_flag_e", "fingerprint_contract_version", "fingerprint_mode",
    )
    manifest_columns = (
        "company_id", "snapshot_type", "evaluation_month", "scope_fingerprint",
        "schema_version", "algorithm_version", "basis_from", "basis_to", "scope_mode",
        "source_watermark", "source_watermark_status", "source_fingerprint",
    )
    manifest_values = (
        key.company_id, key.snapshot_type, key.evaluation_month, key.scope_fingerprint,
        key.schema_version, key.algorithm_version, basis_from, basis_to, scope_mode,
        source_watermark, source_watermark_status, source_fingerprint,
    )
    if key.profile_fingerprint:
        manifest_columns = (*manifest_columns, "profile_fingerprint")
        manifest_values = (*manifest_values, key.profile_fingerprint)
    parts = [
        _canonical_section(
            "manifest",
            manifest_columns,
            [manifest_values],
        ),
        _canonical_section("scope_stock", ("stock_code",), [(code,) for code in sorted(stock_codes)]),
        _canonical_section("source_contract", contract_columns, [tuple("|".join(map(str, source_contract.get(column, ()))) if isinstance(source_contract.get(column), (list, tuple)) else source_contract.get(column) for column in contract_columns)]),
        _canonical_section("source_diagnostics", RELATIONAL_DIAGNOSTIC_FIELDS + ("diagnostic_contract_version",), [tuple(source_diagnostics.get(column, 0) for column in RELATIONAL_DIAGNOSTIC_FIELDS) + (source_diagnostics.get("diagnostic_contract_version", 0),)]),
        _canonical_section(
            "monthly_activity",
            ("month", "product_code", "stock_code", "occurrence_count", "outbound_quantity", "outbound_day_count"),
            [
                (row.get("month"), row.get("product_code"), row.get("stock_code"), row.get("occurrence_count"), row.get("outbound_quantity"), row.get("outbound_day_count"))
                for row in sorted(monthly_activity, key=lambda row: (str(row.get("month") or ""), str(row.get("product_code") or ""), str(row.get("stock_code") or "")))
            ],
        ),
        _canonical_section(
            "frequency_product",
            _frequency_product_columns(key),
            [
                tuple(row.get(column) for column in _frequency_product_columns(key))
                for row in sorted(canonical_products, key=lambda row: str(row.get("product_code") or ""))
            ],
        ),
    ]
    prefix = b"RELATIONAL_FREQUENCY_V2\x00" if _is_extended_key(key) else b"RELATIONAL_FREQUENCY_V1\x00"
    return prefix + b"".join(parts)


def _is_extended_key(key: SnapshotKey) -> bool:
    return _is_lifecycle_key(key) or _is_product_statistics_key(key)


def _is_lifecycle_key(key: SnapshotKey) -> bool:
    return key.schema_version == EXTENDED_SCHEMA_VERSION and key.algorithm_version == EXTENDED_ALGORITHM_VERSION


def _is_product_statistics_key(key: SnapshotKey) -> bool:
    return (
        key.schema_version == PRODUCT_STATISTICS_SCHEMA_VERSION
        and key.algorithm_version == PRODUCT_STATISTICS_ALGORITHM_VERSION
    )


def _frequency_product_columns(key: SnapshotKey) -> tuple[str, ...]:
    base = ("product_code", "occurrence_count_3m", "frequency_grade", "data_status")
    if not _is_extended_key(key):
        return base
    extended = (
        "product_code",
        "occurrence_count_3m",
        "outbound_day_count_3m",
        "outbound_customer_count_3m",
        "frequency_grade",
        "legacy_frequency_grade",
        "lifecycle_status",
        "lifecycle_reason",
        "first_normal_inbound_month",
        "row_status",
        "data_status",
    )
    if not _is_product_statistics_key(key):
        return extended
    return (
        *extended[:-1],
        "outbound_qty_3m",
        "outbound_paid_qty_3m",
        "return_event_count_3m",
        "return_qty_3m",
        "return_supply_amount_3m",
        "avg_purchase_unit_cost",
        "purchase_price_basis_month",
        "purchase_price_status",
        "avg_sales_unit_price",
        "sales_price_basis_month",
        "sales_price_status",
        "estimated_unit_profit",
        "estimated_profit_rate",
        "profit_grade",
        "estimated_contribution_amount",
        "contribution_grade",
        "profitability_status",
        "data_status",
    )


def is_extended_frequency_key(key: SnapshotKey) -> bool:
    return _is_extended_key(key)


def is_product_statistics_key(key: SnapshotKey) -> bool:
    return _is_product_statistics_key(key)


def frequency_product_columns(key: SnapshotKey) -> tuple[str, ...]:
    return _frequency_product_columns(key)


def frequency_storage_representation(key: SnapshotKey) -> str:
    return (
        EXTENDED_RELATIONAL_FREQUENCY_REPRESENTATION
        if _is_extended_key(key)
        else RELATIONAL_FREQUENCY_REPRESENTATION
    )


def calculate_relational_frequency_checksum(**kwargs: Any) -> str:
    """Hash typed rows using a length-prefixed canonical encoder, never JSON."""
    return hashlib.sha256(_canonical_relational_bytes(**kwargs)).hexdigest()


def _relational_row_checksum(section: str, columns: Sequence[str], row: Sequence[Any]) -> str:
    return hashlib.sha256(_canonical_section(section, columns, [row])).hexdigest()


def relational_row_checksum(section: str, columns: Sequence[str], row: Sequence[Any]) -> str:
    return _relational_row_checksum(section, columns, row)


def _relational_projection_digest(rows: Iterable[Mapping[str, Any]], *, key: SnapshotKey | None = None) -> str:
    columns = _frequency_product_columns(key) if key is not None else (
        "product_code", "occurrence_count_3m", "frequency_grade", "data_status"
    )
    canonical = [
        tuple(canonicalize_frequency_product_storage_row(key, row).get(column) for column in columns)
        if key is not None and _is_product_statistics_key(key)
        else tuple(row.get(column) for column in columns)
        for row in rows
    ]
    return hashlib.sha256(_canonical_section("frequency_projection", columns, sorted(canonical))).hexdigest()


def build_relational_frequency_snapshot_from_aggregates(
    *,
    company_id: Any,
    evaluation_month: Any,
    monthly_rows: Iterable[Mapping[str, Any]],
    product_codes: Iterable[Any],
    stock_codes: Iterable[Any] | None = None,
    source_watermark: Any = None,
    source_watermark_status: str = "unverified",
    source_diagnostics: Mapping[str, Any] | None = None,
) -> RelationalFrequencySnapshot:
    company = _normalize_code(company_id, field="company_id")
    basis = completed_month_basis(evaluation_month)
    universe = tuple(sorted({_normalize_code(code, field="product_code") for code in product_codes}))
    scope_codes = _normalized_scope(stock_codes)
    normalized_monthly: list[dict[str, Any]] = []
    counts: dict[str, int] = defaultdict(int)
    seen: set[tuple[str, str, str]] = set()
    for source in monthly_rows:
        row = {
            "month": str(source.get("month") or ""),
            "product_code": _normalize_code(source.get("product_code"), field="product_code"),
            "stock_code": _normalize_code(source.get("stock_code"), field="stock_code"),
            "occurrence_count": _required_nonnegative_int(source.get("occurrence_count"), field="occurrence_count"),
            "outbound_quantity": _required_nonnegative_int(source.get("outbound_quantity"), field="outbound_quantity"),
            "outbound_day_count": _required_nonnegative_int(source.get("outbound_day_count"), field="outbound_day_count"),
        }
        key = (row["month"], row["product_code"], row["stock_code"])
        if key in seen or row["month"] not in basis.months or row["product_code"] not in universe:
            raise SnapshotContractError("relational monthly activity is invalid")
        if scope_codes and row["stock_code"] not in scope_codes:
            raise SnapshotContractError("relational monthly activity is outside scope")
        if row["occurrence_count"] <= 0 or row["outbound_quantity"] <= 0 or not 0 < row["outbound_day_count"] <= row["occurrence_count"]:
            raise SnapshotContractError("relational monthly activity aggregate is invalid")
        seen.add(key)
        counts[row["product_code"]] += row["occurrence_count"]
        normalized_monthly.append(row)
    frequency_products = assign_frequency_grades(counts, universe)
    diagnostics = {field: _required_nonnegative_int((source_diagnostics or {}).get(field, 0), field=field) for field in RELATIONAL_DIAGNOSTIC_FIELDS}
    diagnostics["diagnostic_contract_version"] = int((source_diagnostics or {}).get("diagnostic_contract_version") or 0)
    if diagnostics["diagnostic_contract_version"] >= 2 and sum(diagnostics[field] for field in RELATIONAL_PARTITION_FIELDS) != diagnostics["source_row_count"]:
        raise SnapshotContractError("relational source diagnostics do not reconcile")
    source_contract = {
        "table": "Rddbc120", "io_gu_gcode": "0012", "normal_tcode_from": "500", "normal_tcode_to": "599",
        "event_key_fields": _EVENT_KEY_FIELDS, "positive_quantity_expression": "quantity + oquantity > 0",
        "return_tcode_from": "600", "return_tcode_to": "699", "returns_are_netted": False,
        "flag_exclusion_fields": (), "non_exclusion_flag_fields": _NON_EXCLUSION_FLAG_FIELDS,
        "universe_mode": "all_rddbc040_baseline", "dashboard_product_filters": "post_grade",
        "include_rd04_del_flag_e": True, "fingerprint_contract_version": 2,
        "fingerprint_mode": "monthly_aggregate_v1",
    }
    key = SnapshotKey(company, SNAPSHOT_TYPE, basis.evaluation_month, scope_fingerprint(scope_codes), SCHEMA_VERSION, ALGORITHM_VERSION)
    source_fingerprint = hashlib.sha256(_canonical_section("source_monthly", ("month", "product_code", "stock_code", "occurrence_count", "outbound_quantity", "outbound_day_count"), [(row["month"], row["product_code"], row["stock_code"], row["occurrence_count"], row["outbound_quantity"], row["outbound_day_count"]) for row in sorted(normalized_monthly, key=lambda row: (row["month"], row["product_code"], row["stock_code"]))]) + _canonical_section("source_diagnostics", RELATIONAL_DIAGNOSTIC_FIELDS, [tuple(diagnostics[field] for field in RELATIONAL_DIAGNOSTIC_FIELDS)])).hexdigest()
    checksum = calculate_relational_frequency_checksum(
        key=key, basis_from=basis.basis_from, basis_to=basis.basis_to, scope_mode="selected" if scope_codes else "all", stock_codes=scope_codes,
        source_watermark=None if source_watermark is None else str(source_watermark), source_watermark_status=str(source_watermark_status or "unverified"), source_fingerprint=source_fingerprint,
        source_contract=source_contract, source_diagnostics=diagnostics, monthly_activity=normalized_monthly, frequency_products=frequency_products,
    )
    return RelationalFrequencySnapshot(key, basis.basis_from, basis.basis_to, "selected" if scope_codes else "all", scope_codes, None if source_watermark is None else str(source_watermark), str(source_watermark_status or "unverified"), source_fingerprint, source_contract, diagnostics, tuple(normalized_monthly), tuple(frequency_products), checksum)


def build_extended_relational_frequency_snapshot_from_aggregates(
    *, company_id: Any, evaluation_month: Any, monthly_rows: Iterable[Mapping[str, Any]],
    product_codes: Iterable[Any], product_day_counts: Mapping[str, Any],
    product_customer_counts: Mapping[str, Any], first_normal_inbound_months: Mapping[str, Any],
    stock_codes: Iterable[Any] | None = None, source_watermark: Any = None,
    source_watermark_status: str = "unverified",
    source_diagnostics: Mapping[str, Any] | None = None,
    profile_fingerprint: str = "",
) -> RelationalFrequencySnapshot:
    """Build v2 with a pre-F grade calculated on the current active universe."""
    legacy = build_relational_frequency_snapshot_from_aggregates(
        company_id=company_id, evaluation_month=evaluation_month, monthly_rows=monthly_rows,
        product_codes=product_codes, stock_codes=stock_codes, source_watermark=source_watermark,
        source_watermark_status=source_watermark_status, source_diagnostics=source_diagnostics,
    )
    evaluation_year, evaluation_month_number = _parse_month(legacy.key.evaluation_month)
    evaluation_index = evaluation_year * 12 + evaluation_month_number - 1
    products: list[dict[str, Any]] = []
    for source in legacy.frequency_products:
        product_code = str(source["product_code"])
        occurrence_count = int(source["occurrence_count_3m"])
        day_count = _required_nonnegative_int(product_day_counts.get(product_code, 0), field="outbound_day_count_3m")
        customer_count = _required_nonnegative_int(product_customer_counts.get(product_code, 0), field="outbound_customer_count_3m")
        if day_count > occurrence_count or customer_count > occurrence_count:
            raise SnapshotContractError("extended product distinct count exceeds occurrence count")
        legacy_grade = str(source["frequency_grade"])
        first_value = first_normal_inbound_months.get(product_code)
        first_month: str | None = None
        lifecycle_status = "unknown_lifecycle"
        lifecycle_reason = "first_normal_inbound_missing"
        final_grade = legacy_grade
        first_text = str(first_value).strip() if first_value is not None else ""
        if first_text.lower() not in {"", "nan", "nat", "<na>", "none"}:
            try:
                first_year, first_month_number = _parse_month(first_text)
                first_month = f"{first_year:04d}{first_month_number:02d}"
            except SnapshotContractError:
                lifecycle_status = "invalid_lifecycle"
                lifecycle_reason = "first_normal_inbound_invalid"
            else:
                age_months = evaluation_index - (first_year * 12 + first_month_number - 1)
                if age_months < 0:
                    lifecycle_status = "invalid_lifecycle"
                    lifecycle_reason = "first_normal_inbound_after_evaluation"
                elif age_months <= 2:
                    lifecycle_status = "new_product"
                    lifecycle_reason = "m0_to_m2"
                    final_grade = "F"
                else:
                    lifecycle_status = "established_product"
                    lifecycle_reason = "m3_or_later"
        products.append({
            "product_code": product_code,
            "occurrence_count_3m": occurrence_count,
            "outbound_day_count_3m": day_count,
            "outbound_customer_count_3m": customer_count,
            "frequency_grade": final_grade,
            "legacy_frequency_grade": legacy_grade,
            "lifecycle_status": lifecycle_status,
            "lifecycle_reason": lifecycle_reason,
            "first_normal_inbound_month": first_month,
            "row_status": "no_normal_outbound" if occurrence_count == 0 else "ready",
            "data_status": SNAPSHOT_STATUS_READY,
        })
    key = SnapshotKey(
        legacy.key.company_id, legacy.key.snapshot_type, legacy.key.evaluation_month,
        legacy.key.scope_fingerprint, EXTENDED_SCHEMA_VERSION, EXTENDED_ALGORITHM_VERSION,
        str(profile_fingerprint or "").strip().lower(),
    )
    source_contract = dict(legacy.source_contract)
    source_contract.update({
        "fingerprint_contract_version": 4 if key.profile_fingerprint else 3,
        "fingerprint_mode": "event_product_projection_v2",
        "universe_mode": "dashboard_profile_active_products",
        "dashboard_product_filters": "pre_grade",
        "product_activity_contract": "current_stock_or_basis_normal_inbound_or_basis_normal_outbound",
    })
    source_fingerprint = hashlib.sha256(
        legacy.source_fingerprint.encode("ascii") + _canonical_section(
            "extended_source_projection",
            ("product_code", "outbound_day_count_3m", "outbound_customer_count_3m", "first_normal_inbound_month"),
            [(row["product_code"], row["outbound_day_count_3m"], row["outbound_customer_count_3m"], row["first_normal_inbound_month"])
             for row in sorted(products, key=lambda item: item["product_code"])],
        )
    ).hexdigest()
    checksum = calculate_relational_frequency_checksum(
        key=key, basis_from=legacy.basis_from, basis_to=legacy.basis_to,
        scope_mode=legacy.scope_mode, stock_codes=legacy.stock_codes,
        source_watermark=legacy.source_watermark, source_watermark_status=legacy.source_watermark_status,
        source_fingerprint=source_fingerprint, source_contract=source_contract,
        source_diagnostics=legacy.source_diagnostics, monthly_activity=legacy.monthly_activity,
        frequency_products=products,
    )
    return RelationalFrequencySnapshot(
        key, legacy.basis_from, legacy.basis_to, legacy.scope_mode, legacy.stock_codes,
        legacy.source_watermark, legacy.source_watermark_status, source_fingerprint,
        source_contract, legacy.source_diagnostics, legacy.monthly_activity, tuple(products), checksum,
    )


def _optional_decimal(value: Any, *, field: str) -> Decimal | None:
    if value is None or str(value).strip().lower() in {"", "none", "nan", "nat", "<na>"}:
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise SnapshotContractError(f"{field} must be numeric or unavailable") from exc
    if not number.is_finite():
        raise SnapshotContractError(f"{field} must be finite")
    return number


def canonicalize_product_statistics_decimal(column: str, value: Any) -> Decimal | None:
    """Return the exact value SQL Server stores for one schema 2.1 DECIMAL column."""
    if column not in PRODUCT_STATISTICS_DECIMAL_STORAGE:
        raise SnapshotContractError(f"unsupported product statistics decimal column: {column}")
    number = _optional_decimal(value, field=column)
    if number is None:
        return None
    precision, scale = PRODUCT_STATISTICS_DECIMAL_STORAGE[column]
    quantum = Decimal(1).scaleb(-scale)
    try:
        with localcontext() as context:
            context.prec = precision + scale + 2
            context.rounding = ROUND_HALF_UP
            canonical = number.quantize(quantum)
            # SQL Server DECIMAL does not retain a negative sign on zero. Keep
            # the column scale while making checksum and INSERT values match
            # the value returned by pyodbc after storage.
            if canonical.is_zero():
                canonical = Decimal(0).quantize(quantum)
    except InvalidOperation as exc:
        raise SnapshotContractError(
            f"{column} exceeds DECIMAL({precision},{scale})"
        ) from exc
    if canonical and canonical.adjusted() >= precision - scale:
        raise SnapshotContractError(f"{column} exceeds DECIMAL({precision},{scale})")
    return canonical


def canonicalize_frequency_product_storage_row(
    key: SnapshotKey,
    row: Mapping[str, Any],
) -> dict[str, Any]:
    """Canonicalize only values whose schema 2.1 SQL storage can change."""
    canonical = dict(row)
    if _is_product_statistics_key(key):
        for column in PRODUCT_STATISTICS_DECIMAL_STORAGE:
            canonical[column] = canonicalize_product_statistics_decimal(column, canonical.get(column))
    return canonical


def _relative_grades(values: Mapping[str, Decimal]) -> dict[str, str]:
    """Assign deterministic relative quintiles while keeping equal values together."""
    groups: dict[Decimal, list[str]] = defaultdict(list)
    for code, value in values.items():
        groups[value].append(code)
    total = sum(len(codes) for codes in groups.values())
    before = 0
    grades: dict[str, str] = {}
    for value in sorted(groups, reverse=True):
        grade = FREQUENCY_GRADES[min((before * 5) // max(total, 1), 4)]
        for code in groups[value]:
            grades[code] = grade
        before += len(groups[value])
    return grades


def build_product_statistics_relational_snapshot_from_aggregates(
    *,
    company_id: Any,
    evaluation_month: Any,
    monthly_rows: Iterable[Mapping[str, Any]],
    product_codes: Iterable[Any],
    product_day_counts: Mapping[str, Any],
    product_customer_counts: Mapping[str, Any],
    first_normal_inbound_months: Mapping[str, Any],
    outbound_paid_quantities: Mapping[str, Any],
    return_statistics: Mapping[str, Mapping[str, Any]],
    purchase_prices: Mapping[str, Mapping[str, Any]],
    sales_prices: Mapping[str, Mapping[str, Any]],
    adjustment_only_products: Iterable[Any] = (),
    profitability_unavailable_products: Iterable[Any] = (),
    stock_codes: Iterable[Any] | None = None,
    source_watermark: Any = None,
    source_watermark_status: str = "unverified",
    source_diagnostics: Mapping[str, Any] | None = None,
    profile_fingerprint: str = "",
) -> RelationalFrequencySnapshot:
    """Build the versioned v2 product-statistics authority without changing frequency grades."""
    lifecycle = build_extended_relational_frequency_snapshot_from_aggregates(
        company_id=company_id,
        evaluation_month=evaluation_month,
        monthly_rows=monthly_rows,
        product_codes=product_codes,
        product_day_counts=product_day_counts,
        product_customer_counts=product_customer_counts,
        first_normal_inbound_months=first_normal_inbound_months,
        stock_codes=stock_codes,
        source_watermark=source_watermark,
        source_watermark_status=source_watermark_status,
        source_diagnostics=source_diagnostics,
        profile_fingerprint=profile_fingerprint,
    )
    adjustment_codes = {_normalize_code(code, field="adjustment_only_product") for code in adjustment_only_products}
    unavailable_codes = {_normalize_code(code, field="profitability_unavailable_product") for code in profitability_unavailable_products}
    outbound_quantities: dict[str, int] = defaultdict(int)
    for row in lifecycle.monthly_activity:
        outbound_quantities[str(row["product_code"])] += _required_nonnegative_int(
            row.get("outbound_quantity"), field="outbound_qty_3m"
        )

    rows: list[dict[str, Any]] = []
    profit_scores: dict[str, Decimal] = {}
    contribution_scores: dict[str, Decimal] = {}
    for source in lifecycle.frequency_products:
        code = str(source["product_code"])
        purchase = dict(purchase_prices.get(code) or {})
        sales = dict(sales_prices.get(code) or {})
        returns = dict(return_statistics.get(code) or {})
        purchase_cost = _optional_decimal(purchase.get("unit_price"), field="avg_purchase_unit_cost")
        sales_price = _optional_decimal(sales.get("unit_price"), field="avg_sales_unit_price")
        purchase_status = str(purchase.get("status") or "unavailable")
        sales_status = str(sales.get("status") or "unavailable")
        if purchase_status not in {"ready", "stale", "unavailable"} or sales_status not in {"ready", "stale", "unavailable"}:
            raise SnapshotContractError("price status is invalid")
        unit_profit = sales_price - purchase_cost if purchase_cost is not None and sales_price is not None else None
        profit_rate = (
            unit_profit / sales_price
            if unit_profit is not None and sales_price is not None and sales_price != 0
            else None
        )
        contribution = unit_profit * outbound_quantities.get(code, 0) if unit_profit is not None else None
        if code in adjustment_codes:
            profitability_status = "excluded_adjustment_only"
        elif code in unavailable_codes:
            profitability_status = "unavailable"
        elif purchase_status == "unavailable" or sales_status == "unavailable" or profit_rate is None:
            profitability_status = "unavailable"
        elif purchase_status == "stale" or sales_status == "stale":
            profitability_status = "stale"
        else:
            profitability_status = "ready"
            profit_scores[code] = profit_rate
            contribution_scores[code] = contribution or Decimal(0)
        rows.append({
            **source,
            "outbound_qty_3m": outbound_quantities.get(code, 0),
            "outbound_paid_qty_3m": _required_nonnegative_int(outbound_paid_quantities.get(code, 0), field="outbound_paid_qty_3m"),
            "return_event_count_3m": _required_nonnegative_int(returns.get("event_count", 0), field="return_event_count_3m"),
            "return_qty_3m": _required_nonnegative_int(returns.get("quantity", 0), field="return_qty_3m"),
            "return_supply_amount_3m": _optional_decimal(returns.get("supply_amount", 0), field="return_supply_amount_3m") or Decimal(0),
            "avg_purchase_unit_cost": purchase_cost,
            "purchase_price_basis_month": purchase.get("basis_month") or None,
            "purchase_price_status": purchase_status,
            "avg_sales_unit_price": sales_price,
            "sales_price_basis_month": sales.get("basis_month") or None,
            "sales_price_status": sales_status,
            "estimated_unit_profit": unit_profit,
            "estimated_profit_rate": profit_rate,
            "profit_grade": "unavailable",
            "estimated_contribution_amount": contribution,
            "contribution_grade": "unavailable",
            "profitability_status": profitability_status,
        })
    profit_grades = _relative_grades(profit_scores)
    contribution_grades = _relative_grades(contribution_scores)
    for row in rows:
        code = str(row["product_code"])
        if row["profitability_status"] == "ready":
            row["profit_grade"] = profit_grades[code]
            row["contribution_grade"] = contribution_grades[code]

    key = SnapshotKey(
        lifecycle.key.company_id,
        lifecycle.key.snapshot_type,
        lifecycle.key.evaluation_month,
        lifecycle.key.scope_fingerprint,
        PRODUCT_STATISTICS_SCHEMA_VERSION,
        PRODUCT_STATISTICS_ALGORITHM_VERSION,
        lifecycle.key.profile_fingerprint,
    )
    rows = [canonicalize_frequency_product_storage_row(key, row) for row in rows]
    source_contract = dict(lifecycle.source_contract)
    source_contract.update({
        "fingerprint_contract_version": 5,
        "fingerprint_mode": "event_product_statistics_v1",
    })
    source_fingerprint = hashlib.sha256(
        lifecycle.source_fingerprint.encode("ascii")
        + _canonical_section(
            "product_statistics_source",
            _frequency_product_columns(key),
            [tuple(row.get(column) for column in _frequency_product_columns(key)) for row in rows],
        )
    ).hexdigest()
    checksum = calculate_relational_frequency_checksum(
        key=key,
        basis_from=lifecycle.basis_from,
        basis_to=lifecycle.basis_to,
        scope_mode=lifecycle.scope_mode,
        stock_codes=lifecycle.stock_codes,
        source_watermark=lifecycle.source_watermark,
        source_watermark_status=lifecycle.source_watermark_status,
        source_fingerprint=source_fingerprint,
        source_contract=source_contract,
        source_diagnostics=lifecycle.source_diagnostics,
        monthly_activity=lifecycle.monthly_activity,
        frequency_products=rows,
    )
    return RelationalFrequencySnapshot(
        key, lifecycle.basis_from, lifecycle.basis_to, lifecycle.scope_mode, lifecycle.stock_codes,
        lifecycle.source_watermark, lifecycle.source_watermark_status, source_fingerprint,
        source_contract, lifecycle.source_diagnostics, lifecycle.monthly_activity, tuple(rows), checksum,
    )


def build_relational_frequency_projection(snapshot: RelationalFrequencySnapshot) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    columns = _frequency_product_columns(snapshot.key)
    for source in snapshot.frequency_products:
        row = _canonical_relational_projection_row(source, key=snapshot.key)
        row["row_checksum"] = _relational_row_checksum("frequency_product", columns, tuple(row[column] for column in columns))
        rows.append(row)
    grades = EXTENDED_FREQUENCY_PROJECTION_GRADES if _is_extended_key(snapshot.key) else FREQUENCY_PROJECTION_GRADES
    headers = [
        {
            "frequency_grade": grade,
            "expected_product_count": len([row for row in rows if row["frequency_grade"] == grade]),
            "projection_checksum": _relational_projection_digest(
                (row for row in rows if row["frequency_grade"] == grade), key=snapshot.key
            ),
        }
        for grade in grades
    ]
    return rows, headers


def validate_relational_frequency_projection(
    *,
    rows: Iterable[Mapping[str, Any]],
    headers: Iterable[Mapping[str, Any]],
    required_grade: str = "",
    require_complete: bool = False,
    key: SnapshotKey | None = None,
) -> tuple[dict[str, Any], ...]:
    product_columns = _frequency_product_columns(key) if key is not None else (
        "product_code", "occurrence_count_3m", "frequency_grade", "data_status"
    )
    grades = EXTENDED_FREQUENCY_PROJECTION_GRADES if key is not None and _is_extended_key(key) else FREQUENCY_PROJECTION_GRADES
    normalized_headers = {
        str(row.get("frequency_grade") or ""): {
            "expected_product_count": _required_nonnegative_int(row.get("expected_product_count"), field="expected_product_count"),
            "projection_checksum": str(row.get("projection_checksum") or ""),
        }
        for row in headers
        if isinstance(row, Mapping)
    }
    if set(normalized_headers) != set(grades):
        raise SnapshotContractError("relational projection headers are incomplete")
    normalized_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in rows:
        row = _canonical_relational_projection_row(source, key=key)
        if row["frequency_grade"] not in grades or row["data_status"] != SNAPSHOT_STATUS_READY or row["product_code"] in seen:
            raise SnapshotContractError("relational projection row is invalid")
        if str(source.get("row_checksum") or "") != _relational_row_checksum("frequency_product", product_columns, tuple(row[column] for column in product_columns)):
            raise SnapshotContractError("relational projection row checksum does not match")
        seen.add(row["product_code"])
        row["row_checksum"] = str(source.get("row_checksum"))
        normalized_rows.append(row)
    if required_grade:
        grade_rows = [row for row in normalized_rows if row["frequency_grade"] == required_grade]
        header = normalized_headers.get(required_grade)
        if header is None or len(grade_rows) != header["expected_product_count"] or _relational_projection_digest(grade_rows, key=key) != header["projection_checksum"]:
            raise SnapshotContractError("relational projection grade checksum does not match")
    elif require_complete:
        if sum(row["expected_product_count"] for row in normalized_headers.values()) != len(normalized_rows):
            raise SnapshotContractError("relational projection product count does not match")
        for grade, header in normalized_headers.items():
            grade_rows = [row for row in normalized_rows if row["frequency_grade"] == grade]
            if len(grade_rows) != header["expected_product_count"] or _relational_projection_digest(grade_rows, key=key) != header["projection_checksum"]:
                raise SnapshotContractError("relational projection checksum does not match")
    return tuple(normalized_rows)


def validate_relational_frequency_snapshot(snapshot: RelationalFrequencySnapshot) -> None:
    """Validate the complete native authority set before draft approval."""
    if snapshot.scope_mode not in {"all", "selected"}:
        raise SnapshotContractError("relational scope mode is invalid")
    if snapshot.scope_mode == "all" and snapshot.stock_codes:
        raise SnapshotContractError("relational all scope contains stock rows")
    if snapshot.scope_mode == "selected" and not snapshot.stock_codes:
        raise SnapshotContractError("relational selected scope is empty")
    basis = completed_month_basis(snapshot.key.evaluation_month)
    if snapshot.basis_from != basis.basis_from or snapshot.basis_to != basis.basis_to:
        raise SnapshotContractError("relational basis range does not match")
    if snapshot.key.scope_fingerprint != scope_fingerprint(snapshot.stock_codes):
        raise SnapshotContractError("relational scope fingerprint does not match")
    if snapshot.source_watermark_status not in {"verified", "unverified"}:
        raise SnapshotContractError("relational source watermark status is invalid")
    if len(snapshot.source_fingerprint) != 64:
        raise SnapshotContractError("relational source fingerprint is invalid")
    diagnostics = snapshot.source_diagnostics
    if int(diagnostics.get("diagnostic_contract_version") or 0) >= 2:
        expected_mode = (
            "event_product_statistics_v1"
            if _is_product_statistics_key(snapshot.key)
            else "event_product_projection_v2"
            if _is_extended_key(snapshot.key)
            else "monthly_aggregate_v1"
        )
        expected_contract_version = (
            5 if _is_product_statistics_key(snapshot.key) else
            4 if _is_extended_key(snapshot.key) and snapshot.key.profile_fingerprint else
            3 if _is_extended_key(snapshot.key) else 2
        )
        if int(snapshot.source_contract.get("fingerprint_contract_version") or 0) != expected_contract_version:
            raise SnapshotContractError("relational source contract version is invalid")
        if str(snapshot.source_contract.get("fingerprint_mode") or "") != expected_mode:
            raise SnapshotContractError("relational source fingerprint mode is invalid")
        if snapshot.key.profile_fingerprint and (
            len(snapshot.key.profile_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in snapshot.key.profile_fingerprint)
        ):
            raise SnapshotContractError("relational profile fingerprint is invalid")
        if sum(_required_nonnegative_int(diagnostics.get(field), field=field) for field in RELATIONAL_PARTITION_FIELDS) != _required_nonnegative_int(diagnostics.get("source_row_count"), field="source_row_count"):
            raise SnapshotContractError("relational source diagnostics do not reconcile")
    product_counts: dict[str, int] = {}
    for row in snapshot.frequency_products:
        code = _normalize_code(row.get("product_code"), field="product_code")
        if code in product_counts:
            raise SnapshotContractError("relational product code is duplicated")
        product_counts[code] = _required_nonnegative_int(row.get("occurrence_count_3m"), field="occurrence_count_3m")
        allowed_grades = EXTENDED_FREQUENCY_PROJECTION_GRADES if _is_extended_key(snapshot.key) else FREQUENCY_PROJECTION_GRADES
        if str(row.get("frequency_grade") or "") not in allowed_grades or str(row.get("data_status") or "") != SNAPSHOT_STATUS_READY:
            raise SnapshotContractError("relational product grade/status is invalid")
        if _is_extended_key(snapshot.key):
            day_count = _required_nonnegative_int(row.get("outbound_day_count_3m"), field="outbound_day_count_3m")
            customer_count = _required_nonnegative_int(row.get("outbound_customer_count_3m"), field="outbound_customer_count_3m")
            if day_count > product_counts[code] or customer_count > product_counts[code]:
                raise SnapshotContractError("relational product distinct count is invalid")
            if str(row.get("legacy_frequency_grade") or "") not in FREQUENCY_PROJECTION_GRADES:
                raise SnapshotContractError("relational legacy grade is invalid")
            if str(row.get("lifecycle_status") or "") not in {
                "new_product", "established_product", "unknown_lifecycle", "invalid_lifecycle"
            }:
                raise SnapshotContractError("relational lifecycle status is invalid")
            if str(row.get("row_status") or "") not in {"ready", "no_normal_outbound"}:
                raise SnapshotContractError("relational row status is invalid")
            if (product_counts[code] == 0) != (str(row.get("row_status")) == "no_normal_outbound"):
                raise SnapshotContractError("relational row status does not match occurrence count")
            if _is_product_statistics_key(snapshot.key):
                outbound_qty = _required_nonnegative_int(row.get("outbound_qty_3m"), field="outbound_qty_3m")
                paid_qty = _required_nonnegative_int(row.get("outbound_paid_qty_3m"), field="outbound_paid_qty_3m")
                _required_nonnegative_int(row.get("return_event_count_3m"), field="return_event_count_3m")
                _required_nonnegative_int(row.get("return_qty_3m"), field="return_qty_3m")
                return_amount = _optional_decimal(row.get("return_supply_amount_3m"), field="return_supply_amount_3m")
                if return_amount is None or return_amount < 0:
                    raise SnapshotContractError("relational product quantity/return statistics are invalid")
                for field in ("purchase_price_basis_month", "sales_price_basis_month"):
                    month_value = row.get(field)
                    if month_value not in (None, ""):
                        _parse_month(month_value)
                purchase_status = str(row.get("purchase_price_status") or "")
                sales_status = str(row.get("sales_price_status") or "")
                profitability_status = str(row.get("profitability_status") or "")
                if purchase_status not in {"ready", "stale", "unavailable"} or sales_status not in {"ready", "stale", "unavailable"}:
                    raise SnapshotContractError("relational product price status is invalid")
                if profitability_status not in {"ready", "stale", "unavailable", "excluded_adjustment_only"}:
                    raise SnapshotContractError("relational product profitability status is invalid")
                profit_grade = str(row.get("profit_grade") or "")
                contribution_grade = str(row.get("contribution_grade") or "")
                if profitability_status == "ready":
                    if profit_grade not in FREQUENCY_GRADES or contribution_grade not in FREQUENCY_GRADES:
                        raise SnapshotContractError("relational product profitability grade is invalid")
                elif profit_grade != "unavailable" or contribution_grade != "unavailable":
                    raise SnapshotContractError("unavailable profitability must not expose a grade")
    monthly_counts: dict[str, int] = defaultdict(int)
    seen: set[tuple[str, str, str]] = set()
    for row in snapshot.monthly_activity:
        month = str(row.get("month") or "")
        product = _normalize_code(row.get("product_code"), field="product_code")
        stock = _normalize_code(row.get("stock_code"), field="stock_code")
        row_key = (month, product, stock)
        if row_key in seen or month not in basis.months or product not in product_counts:
            raise SnapshotContractError("relational monthly row is invalid")
        seen.add(row_key)
        occurrence = _required_nonnegative_int(row.get("occurrence_count"), field="occurrence_count")
        quantity = _required_nonnegative_int(row.get("outbound_quantity"), field="outbound_quantity")
        day_count = _required_nonnegative_int(row.get("outbound_day_count"), field="outbound_day_count")
        if occurrence <= 0 or quantity <= 0 or not 0 < day_count <= occurrence:
            raise SnapshotContractError("relational monthly aggregate is invalid")
        monthly_counts[product] += occurrence
    if product_counts != {code: monthly_counts.get(code, 0) for code in sorted(product_counts)}:
        raise SnapshotContractError("relational monthly totals do not match product totals")
    expected_grades = {row["product_code"]: row["frequency_grade"] for row in assign_frequency_grades(product_counts, product_counts)}
    grade_field = "legacy_frequency_grade" if _is_extended_key(snapshot.key) else "frequency_grade"
    if expected_grades != {str(row.get("product_code")): str(row.get(grade_field)) for row in snapshot.frequency_products}:
        raise SnapshotContractError("relational legacy grades do not match occurrence bands")
    if _is_extended_key(snapshot.key):
        for row in snapshot.frequency_products:
            lifecycle = str(row.get("lifecycle_status") or "")
            final_grade = str(row.get("frequency_grade") or "")
            legacy_grade = str(row.get("legacy_frequency_grade") or "")
            if (lifecycle == "new_product" and final_grade != "F") or (
                lifecycle != "new_product" and final_grade != legacy_grade
            ):
                raise SnapshotContractError("relational lifecycle grade precedence is invalid")
    expected_checksum = calculate_relational_frequency_checksum(
        key=snapshot.key, basis_from=snapshot.basis_from, basis_to=snapshot.basis_to, scope_mode=snapshot.scope_mode, stock_codes=snapshot.stock_codes,
        source_watermark=snapshot.source_watermark, source_watermark_status=snapshot.source_watermark_status, source_fingerprint=snapshot.source_fingerprint,
        source_contract=snapshot.source_contract, source_diagnostics=snapshot.source_diagnostics, monthly_activity=snapshot.monthly_activity, frequency_products=snapshot.frequency_products,
    )
    if expected_checksum != snapshot.checksum:
        raise SnapshotContractError("relational snapshot checksum does not match")


def _canonical_projection_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "product_code": _normalize_code(row.get("product_code"), field="product_code"),
        "occurrence_count_3m": _required_nonnegative_int(
            row.get("occurrence_count_3m"), field="occurrence_count_3m"
        ),
        "frequency_grade": str(row.get("frequency_grade") or ""),
        "data_status": str(row.get("data_status") or ""),
    }


def _canonical_relational_projection_row(
    row: Mapping[str, Any], *, key: SnapshotKey | None
) -> dict[str, Any]:
    base = _canonical_projection_row(row)
    if key is None or not _is_extended_key(key):
        return base
    first_month = row.get("first_normal_inbound_month")
    lifecycle = {
        "product_code": base["product_code"],
        "occurrence_count_3m": base["occurrence_count_3m"],
        "outbound_day_count_3m": _required_nonnegative_int(row.get("outbound_day_count_3m"), field="outbound_day_count_3m"),
        "outbound_customer_count_3m": _required_nonnegative_int(row.get("outbound_customer_count_3m"), field="outbound_customer_count_3m"),
        "frequency_grade": base["frequency_grade"],
        "legacy_frequency_grade": str(row.get("legacy_frequency_grade") or ""),
        "lifecycle_status": str(row.get("lifecycle_status") or ""),
        "lifecycle_reason": str(row.get("lifecycle_reason") or ""),
        "first_normal_inbound_month": None if first_month in (None, "") else str(first_month),
        "row_status": str(row.get("row_status") or ""),
        "data_status": base["data_status"],
    }
    if not _is_product_statistics_key(key):
        return lifecycle
    return {
        **{field: lifecycle[field] for field in _frequency_product_columns(key) if field in lifecycle and field != "data_status"},
        "outbound_qty_3m": _required_nonnegative_int(row.get("outbound_qty_3m"), field="outbound_qty_3m"),
        "outbound_paid_qty_3m": _required_nonnegative_int(row.get("outbound_paid_qty_3m"), field="outbound_paid_qty_3m"),
        "return_event_count_3m": _required_nonnegative_int(row.get("return_event_count_3m"), field="return_event_count_3m"),
        "return_qty_3m": _required_nonnegative_int(row.get("return_qty_3m"), field="return_qty_3m"),
        "return_supply_amount_3m": canonicalize_product_statistics_decimal("return_supply_amount_3m", row.get("return_supply_amount_3m")),
        "avg_purchase_unit_cost": canonicalize_product_statistics_decimal("avg_purchase_unit_cost", row.get("avg_purchase_unit_cost")),
        "purchase_price_basis_month": None if row.get("purchase_price_basis_month") in (None, "") else str(row.get("purchase_price_basis_month")),
        "purchase_price_status": str(row.get("purchase_price_status") or ""),
        "avg_sales_unit_price": canonicalize_product_statistics_decimal("avg_sales_unit_price", row.get("avg_sales_unit_price")),
        "sales_price_basis_month": None if row.get("sales_price_basis_month") in (None, "") else str(row.get("sales_price_basis_month")),
        "sales_price_status": str(row.get("sales_price_status") or ""),
        "estimated_unit_profit": canonicalize_product_statistics_decimal("estimated_unit_profit", row.get("estimated_unit_profit")),
        "estimated_profit_rate": canonicalize_product_statistics_decimal("estimated_profit_rate", row.get("estimated_profit_rate")),
        "profit_grade": str(row.get("profit_grade") or ""),
        "estimated_contribution_amount": canonicalize_product_statistics_decimal("estimated_contribution_amount", row.get("estimated_contribution_amount")),
        "contribution_grade": str(row.get("contribution_grade") or ""),
        "profitability_status": str(row.get("profitability_status") or ""),
        "data_status": lifecycle["data_status"],
    }


def _projection_digest(rows: Iterable[Mapping[str, Any]]) -> str:
    canonical = [_canonical_projection_row(row) for row in rows]
    canonical.sort(key=lambda row: row["product_code"])
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _projection_row_checksum(*, manifest_checksum: str, row: Mapping[str, Any]) -> str:
    canonical = {"manifest_checksum": manifest_checksum, "row": _canonical_projection_row(row)}
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_frequency_projection(payload: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build the non-authoritative product-frequency projection from a valid payload."""
    manifest_checksum = str(payload.get("checksum") or "")
    if len(manifest_checksum) != 64:
        raise SnapshotContractError("projection requires the payload checksum")
    source_rows = payload.get("product_frequency")
    if not isinstance(source_rows, list):
        raise SnapshotContractError("projection source rows are invalid")

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in source_rows:
        if not isinstance(source, Mapping):
            raise SnapshotContractError("projection source row is invalid")
        row = _canonical_projection_row(source)
        if row["product_code"] in seen:
            raise SnapshotContractError("projection source contains duplicate product_code")
        if row["frequency_grade"] not in FREQUENCY_PROJECTION_GRADES or row["data_status"] != SNAPSHOT_STATUS_READY:
            raise SnapshotContractError("projection source grade/status is invalid")
        seen.add(row["product_code"])
        row["row_checksum"] = _projection_row_checksum(manifest_checksum=manifest_checksum, row=row)
        rows.append(row)

    headers: list[dict[str, Any]] = []
    for grade in FREQUENCY_PROJECTION_GRADES:
        grade_rows = [row for row in rows if row["frequency_grade"] == grade]
        headers.append(
            {
                "frequency_grade": grade,
                "expected_product_count": len(grade_rows),
                "projection_checksum": _projection_digest(grade_rows),
            }
        )
    return rows, headers


def validate_frequency_projection(
    *,
    manifest_checksum: str,
    rows: Iterable[Mapping[str, Any]],
    headers: Iterable[Mapping[str, Any]],
    required_grade: str = "",
    require_complete: bool = False,
) -> tuple[dict[str, Any], ...]:
    """Validate a derived projection without making it an approval authority."""
    checksum = str(manifest_checksum or "")
    if len(checksum) != 64:
        raise SnapshotContractError("projection manifest checksum is invalid")
    normalized_headers: dict[str, dict[str, Any]] = {}
    for source in headers:
        if not isinstance(source, Mapping):
            raise SnapshotContractError("projection header is invalid")
        grade = str(source.get("frequency_grade") or "")
        count = _required_nonnegative_int(source.get("expected_product_count"), field="expected_product_count")
        digest = str(source.get("projection_checksum") or "")
        if grade not in FREQUENCY_PROJECTION_GRADES or len(digest) != 64 or grade in normalized_headers:
            raise SnapshotContractError("projection header is invalid")
        normalized_headers[grade] = {
            "frequency_grade": grade,
            "expected_product_count": count,
            "projection_checksum": digest,
        }
    if set(normalized_headers) != set(FREQUENCY_PROJECTION_GRADES):
        raise SnapshotContractError("projection headers are incomplete")

    normalized_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in rows:
        if not isinstance(source, Mapping):
            raise SnapshotContractError("projection row is invalid")
        row = _canonical_projection_row(source)
        if row["frequency_grade"] not in FREQUENCY_PROJECTION_GRADES or row["data_status"] != SNAPSHOT_STATUS_READY:
            raise SnapshotContractError("projection row grade/status is invalid")
        if row["product_code"] in seen:
            raise SnapshotContractError("projection contains duplicate product_code")
        if str(source.get("row_checksum") or "") != _projection_row_checksum(manifest_checksum=checksum, row=row):
            raise SnapshotContractError("projection row checksum does not match")
        seen.add(row["product_code"])
        row["row_checksum"] = str(source.get("row_checksum"))
        normalized_rows.append(row)

    if required_grade:
        if required_grade not in FREQUENCY_PROJECTION_GRADES:
            raise SnapshotContractError("projection grade is invalid")
        grade_rows = [row for row in normalized_rows if row["frequency_grade"] == required_grade]
        header = normalized_headers[required_grade]
        if len(grade_rows) != header["expected_product_count"] or _projection_digest(grade_rows) != header["projection_checksum"]:
            raise SnapshotContractError("projection grade checksum does not match")
    elif require_complete:
        if sum(header["expected_product_count"] for header in normalized_headers.values()) != len(normalized_rows):
            raise SnapshotContractError("projection product count does not match")
        for grade, header in normalized_headers.items():
            grade_rows = [row for row in normalized_rows if row["frequency_grade"] == grade]
            if len(grade_rows) != header["expected_product_count"] or _projection_digest(grade_rows) != header["projection_checksum"]:
                raise SnapshotContractError("projection checksum does not match")
    return tuple(normalized_rows)


def _source_fingerprint(
    events: Mapping[tuple[str, str, str], tuple[str, str, int]],
) -> str:
    canonical = [
        {
            "event_key": list(event_key),
            "product_code": event_value[0],
            "stock_code": event_value[1],
            "outbound_quantity": event_value[2],
        }
        for event_key, event_value in sorted(events.items())
    ]
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def aggregate_source_fingerprint(
    monthly_rows: Iterable[Mapping[str, Any]],
    diagnostics: Mapping[str, Any] | None = None,
) -> str:
    canonical = {
        "monthly_activity": [dict(row) for row in monthly_rows],
        "diagnostics": dict(diagnostics or {}),
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _assemble_frequency_snapshot_payload(
    *,
    company: str,
    basis: CompletedMonthBasis,
    universe: Sequence[str],
    scope_codes: Sequence[str],
    monthly_rows: Sequence[Mapping[str, Any]],
    ignored_product_events: int,
    source_fingerprint: str,
    fingerprint_mode: str,
    source_watermark: Any,
    source_watermark_status: str,
    source_diagnostics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    product_counts: dict[str, int] = defaultdict(int)
    normalized_monthly_rows: list[dict[str, Any]] = []
    for row in monthly_rows:
        normalized = {
            "month": str(row.get("month") or ""),
            "product_code": str(row.get("product_code") or ""),
            "stock_code": str(row.get("stock_code") or ""),
            "occurrence_count": int(row.get("occurrence_count") or 0),
            "outbound_quantity": int(row.get("outbound_quantity") or 0),
            "outbound_day_count": int(row.get("outbound_day_count") or 0),
        }
        normalized_monthly_rows.append(normalized)
        product_counts[normalized["product_code"]] += normalized["occurrence_count"]

    frequency_rows = assign_frequency_grades(product_counts, universe)
    grade_counts = {grade: 0 for grade in (*FREQUENCY_GRADES, "X")}
    for row in frequency_rows:
        grade_counts[row["frequency_grade"]] += 1

    scope_payload = {
        "mode": "selected" if scope_codes else "all",
        "stock_codes": list(scope_codes),
        "fingerprint": scope_fingerprint(scope_codes),
    }
    payload: dict[str, Any] = {
        "snapshot_type": SNAPSHOT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "company_id": company,
        "evaluation_month": basis.evaluation_month,
        "basis_from": basis.basis_from,
        "basis_to": basis.basis_to,
        "basis_months": list(basis.months),
        "scope": scope_payload,
        "source_watermark": None if source_watermark is None else str(source_watermark),
        "source_watermark_status": str(source_watermark_status or "unverified"),
        "source_fingerprint": source_fingerprint,
        "source_contract": {
            "table": "Rddbc120",
            "io_gu_gcode": "0012",
            "normal_tcode_from": "500",
            "normal_tcode_to": "599",
            "event_key_fields": list(_EVENT_KEY_FIELDS),
            "positive_quantity_expression": "quantity + oquantity > 0",
            "return_tcode_from": "600",
            "return_tcode_to": "699",
            "returns_are_netted": False,
            "flag_exclusion_fields": [],
            "non_exclusion_flag_fields": list(_NON_EXCLUSION_FLAG_FIELDS),
            # Dashboard filters are applied after this stable, company/scope baseline is graded.
            "universe_mode": "all_rddbc040_baseline",
            "dashboard_product_filters": "post_grade",
            "include_rd04_del_flag_e": True,
            "fingerprint_contract_version": 2,
            "fingerprint_mode": fingerprint_mode,
        },
        "monthly_activity": normalized_monthly_rows,
        "product_frequency": frequency_rows,
        "summary": {
            "product_count": len(universe),
            "normal_event_count": sum(item["occurrence_count"] for item in normalized_monthly_rows),
            "ignored_product_event_count": int(ignored_product_events),
            "grade_counts": grade_counts,
        },
    }
    if source_diagnostics is not None:
        payload["source_diagnostics"] = dict(source_diagnostics)
    payload["checksum"] = calculate_payload_checksum(payload)
    return payload


def build_frequency_snapshot_payload(
    *,
    company_id: Any,
    evaluation_month: Any,
    rows: Iterable[Mapping[str, Any]],
    product_codes: Iterable[Any],
    stock_codes: Iterable[Any] | None = None,
    source_watermark: Any = None,
    source_watermark_status: str = "unverified",
) -> dict[str, Any]:
    company = _normalize_code(company_id, field="company_id")
    basis = completed_month_basis(evaluation_month)
    universe = tuple(sorted({_normalize_code(code, field="product_code") for code in product_codes}))
    universe_set = set(universe)
    scope_codes = _normalized_scope(stock_codes)
    scope_set = set(scope_codes)

    events: dict[tuple[str, str, str], tuple[str, str, int]] = {}
    ignored_product_events = 0
    for row in rows:
        if not is_normal_outbound_tcode(row.get("io_gu_gcode"), row.get("io_tcode")):
            continue
        quantity = _decimal(row.get("quantity"), field="quantity") + _decimal(
            row.get("oquantity"), field="oquantity"
        )
        if quantity <= 0:
            continue
        outbound_date = _normalize_date(row.get("outbound_date"))
        if outbound_date[:6] not in basis.months:
            continue
        product_code = _normalize_code(row.get("product_code"), field="product_code")
        stock_code = _normalize_code(row.get("stock_code"), field="stock_code")
        if scope_set and stock_code not in scope_set:
            continue
        event_key = (
            outbound_date,
            _normalize_code(row.get("vendor_code"), field="vendor_code"),
            _normalize_sequence(row.get("outbound_seq")),
        )
        event_value = (product_code, stock_code, _integer_quantity(quantity))
        previous = events.get(event_key)
        if previous is not None and previous != event_value:
            raise SnapshotContractError(f"conflicting outbound event: {event_key!r}")
        events[event_key] = event_value

    monthly: dict[tuple[str, str, str], dict[str, int]] = defaultdict(
        lambda: {"occurrence_count": 0, "outbound_quantity": 0, "outbound_day_count": 0}
    )
    occurrence_days: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for event_key, (product_code, stock_code, quantity) in events.items():
        if product_code not in universe_set:
            ignored_product_events += 1
            continue
        month = event_key[0][:6]
        key = (month, product_code, stock_code)
        monthly[key]["occurrence_count"] += 1
        monthly[key]["outbound_quantity"] += quantity
        occurrence_days[key].add(event_key[0])

    monthly_rows: list[dict[str, Any]] = []
    for (month, product_code, stock_code), values in sorted(monthly.items()):
        day_count = len(occurrence_days[(month, product_code, stock_code)])
        monthly_rows.append(
            {
                "month": month,
                "product_code": product_code,
                "stock_code": stock_code,
                "occurrence_count": values["occurrence_count"],
                "outbound_quantity": values["outbound_quantity"],
                "outbound_day_count": day_count,
            }
        )

    return _assemble_frequency_snapshot_payload(
        company=company,
        basis=basis,
        universe=universe,
        scope_codes=scope_codes,
        monthly_rows=monthly_rows,
        ignored_product_events=ignored_product_events,
        source_fingerprint=_source_fingerprint(events),
        fingerprint_mode="raw_event_v1",
        source_watermark=source_watermark,
        source_watermark_status=source_watermark_status,
    )


def build_frequency_snapshot_payload_from_aggregates(
    *,
    company_id: Any,
    evaluation_month: Any,
    monthly_rows: Iterable[Mapping[str, Any]],
    product_codes: Iterable[Any],
    stock_codes: Iterable[Any] | None = None,
    source_watermark: Any = None,
    source_watermark_status: str = "unverified",
    source_diagnostics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    company = _normalize_code(company_id, field="company_id")
    basis = completed_month_basis(evaluation_month)
    universe = tuple(sorted({_normalize_code(code, field="product_code") for code in product_codes}))
    universe_set = set(universe)
    scope_codes = _normalized_scope(stock_codes)
    scope_set = set(scope_codes)
    accepted: list[dict[str, Any]] = []
    ignored_product_events = 0
    seen: set[tuple[str, str, str]] = set()
    for index, row in enumerate(monthly_rows):
        month = str(row.get("month") or "").strip()
        product_code = _normalize_code(row.get("product_code"), field="product_code")
        stock_code = _normalize_code(row.get("stock_code"), field="stock_code")
        key = (month, product_code, stock_code)
        if key in seen:
            raise SnapshotContractError(f"duplicate aggregate row: {key!r}")
        seen.add(key)
        if month not in basis.months:
            raise SnapshotContractError(f"aggregate row outside basis months: {key!r}")
        if scope_set and stock_code not in scope_set:
            raise SnapshotContractError(f"aggregate row outside stock scope: {key!r}")
        occurrence_count = _required_nonnegative_int(
            row.get("occurrence_count"), field=f"monthly_rows[{index}].occurrence_count"
        )
        outbound_quantity = _required_nonnegative_int(
            row.get("outbound_quantity"), field=f"monthly_rows[{index}].outbound_quantity"
        )
        outbound_day_count = _required_nonnegative_int(
            row.get("outbound_day_count"), field=f"monthly_rows[{index}].outbound_day_count"
        )
        if occurrence_count <= 0 or outbound_quantity <= 0:
            raise SnapshotContractError(f"aggregate row must be positive: {key!r}")
        if outbound_day_count <= 0 or outbound_day_count > occurrence_count:
            raise SnapshotContractError(f"aggregate day count is inconsistent: {key!r}")
        if product_code not in universe_set:
            ignored_product_events += occurrence_count
            continue
        accepted.append(
            {
                "month": month,
                "product_code": product_code,
                "stock_code": stock_code,
                "occurrence_count": occurrence_count,
                "outbound_quantity": outbound_quantity,
                "outbound_day_count": outbound_day_count,
            }
        )
    accepted.sort(key=lambda row: (row["month"], row["product_code"], row["stock_code"]))
    diagnostics = dict(source_diagnostics or {})
    fingerprint = aggregate_source_fingerprint(accepted, diagnostics)
    return _assemble_frequency_snapshot_payload(
        company=company,
        basis=basis,
        universe=universe,
        scope_codes=scope_codes,
        monthly_rows=accepted,
        ignored_product_events=ignored_product_events,
        source_fingerprint=fingerprint,
        fingerprint_mode="monthly_aggregate_v1",
        source_watermark=source_watermark,
        source_watermark_status=source_watermark_status,
        source_diagnostics=diagnostics,
    )


def snapshot_key_from_payload(payload: Mapping[str, Any]) -> SnapshotKey:
    scope = payload.get("scope")
    if not isinstance(scope, Mapping):
        raise SnapshotContractError("snapshot scope is missing")
    return SnapshotKey(
        company_id=str(payload.get("company_id") or ""),
        snapshot_type=str(payload.get("snapshot_type") or ""),
        evaluation_month=str(payload.get("evaluation_month") or ""),
        scope_fingerprint=str(scope.get("fingerprint") or ""),
        schema_version=str(payload.get("schema_version") or ""),
        algorithm_version=str(payload.get("algorithm_version") or ""),
    )


def _required_nonnegative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise SnapshotContractError(f"invalid {field}: boolean")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise SnapshotContractError(f"invalid {field}: {value!r}") from exc
    if normalized < 0 or normalized != value:
        raise SnapshotContractError(f"invalid {field}: {value!r}")
    return normalized


def _validate_frequency_payload_structure(
    payload: Mapping[str, Any],
    *,
    expected_key: SnapshotKey,
) -> None:
    if payload.get("checksum") != calculate_payload_checksum(payload):
        raise SnapshotContractError("snapshot checksum does not match")
    actual_key = snapshot_key_from_payload(payload)
    if actual_key != expected_key:
        raise SnapshotContractError("snapshot key does not match")

    basis = completed_month_basis(payload.get("evaluation_month"))
    if (
        payload.get("basis_months") != list(basis.months)
        or payload.get("basis_from") != basis.basis_from
        or payload.get("basis_to") != basis.basis_to
    ):
        raise SnapshotContractError("snapshot basis range does not match evaluation month")

    source_fingerprint = str(payload.get("source_fingerprint") or "")
    if len(source_fingerprint) != 64 or any(ch not in "0123456789abcdef" for ch in source_fingerprint):
        raise SnapshotContractError("source fingerprint is invalid")
    if payload.get("source_watermark_status") not in {"verified", "unverified"}:
        raise SnapshotContractError("source watermark status is invalid")

    source_contract = payload.get("source_contract")
    if not isinstance(source_contract, Mapping):
        raise SnapshotContractError("snapshot source contract is invalid")
    diagnostic_contract_version = 0
    source_diagnostics = payload.get("source_diagnostics")
    if isinstance(source_diagnostics, Mapping):
        diagnostic_contract_version = int(source_diagnostics.get("diagnostic_contract_version") or 0)
    if diagnostic_contract_version >= 2:
        if source_contract.get("fingerprint_contract_version") != 2:
            raise SnapshotContractError("snapshot fingerprint contract version is invalid")
        if source_contract.get("fingerprint_mode") not in {"raw_event_v1", "monthly_aggregate_v1"}:
            raise SnapshotContractError("snapshot fingerprint mode is invalid")
        partition_fields = (
            "normal_positive_accepted_row_count",
            "normal_positive_duplicate_row_count",
            "normal_positive_conflicting_row_count",
            "normal_positive_missing_key_row_count",
            "normal_positive_nonintegral_row_count",
            "normal_nonpositive_row_count",
            "return_positive_row_count",
            "return_nonpositive_row_count",
            "other_tcode_row_count",
        )
        if not isinstance(source_diagnostics, Mapping):
            raise SnapshotContractError("snapshot diagnostics are invalid")
        source_count = _required_nonnegative_int(
            source_diagnostics.get("source_row_count"), field="source_diagnostics.source_row_count"
        )
        partition_total = sum(
            _required_nonnegative_int(source_diagnostics.get(field), field=f"source_diagnostics.{field}")
            for field in partition_fields
        )
        if partition_total != source_count:
            raise SnapshotContractError("snapshot source diagnostics do not reconcile")

    scope = payload.get("scope")
    if not isinstance(scope, Mapping):
        raise SnapshotContractError("snapshot scope is missing")
    stock_codes = scope.get("stock_codes")
    if not isinstance(stock_codes, list) or stock_codes != list(_normalized_scope(stock_codes)):
        raise SnapshotContractError("snapshot stock scope is not canonical")
    expected_mode = "selected" if stock_codes else "all"
    if scope.get("mode") != expected_mode or scope.get("fingerprint") != scope_fingerprint(stock_codes):
        raise SnapshotContractError("snapshot stock scope fingerprint does not match")

    frequency_rows = payload.get("product_frequency")
    monthly_rows = payload.get("monthly_activity")
    summary = payload.get("summary")
    if not isinstance(frequency_rows, list) or not isinstance(monthly_rows, list):
        raise SnapshotContractError("snapshot rows are invalid")
    if not isinstance(summary, Mapping):
        raise SnapshotContractError("snapshot summary is invalid")

    product_counts: dict[str, int] = {}
    frequency_by_product: dict[str, Mapping[str, Any]] = {}
    for index, row in enumerate(frequency_rows):
        if not isinstance(row, Mapping):
            raise SnapshotContractError(f"invalid product_frequency[{index}]")
        product_code = _normalize_code(row.get("product_code"), field="product_code")
        if product_code in frequency_by_product:
            raise SnapshotContractError(f"duplicate product_frequency product: {product_code}")
        count = _required_nonnegative_int(
            row.get("occurrence_count_3m"), field=f"product_frequency[{index}].occurrence_count_3m"
        )
        grade = str(row.get("frequency_grade") or "")
        if grade not in {*FREQUENCY_GRADES, "X"} or row.get("data_status") != SNAPSHOT_STATUS_READY:
            raise SnapshotContractError(f"invalid product frequency grade/status: {product_code}")
        product_counts[product_code] = count
        frequency_by_product[product_code] = row

    monthly_keys: set[tuple[str, str, str]] = set()
    monthly_product_counts: dict[str, int] = defaultdict(int)
    normal_event_count = 0
    for index, row in enumerate(monthly_rows):
        if not isinstance(row, Mapping):
            raise SnapshotContractError(f"invalid monthly_activity[{index}]")
        month = str(row.get("month") or "")
        product_code = _normalize_code(row.get("product_code"), field="product_code")
        stock_code = _normalize_code(row.get("stock_code"), field="stock_code")
        key = (month, product_code, stock_code)
        if key in monthly_keys:
            raise SnapshotContractError(f"duplicate monthly activity: {key!r}")
        monthly_keys.add(key)
        if month not in basis.months or product_code not in frequency_by_product:
            raise SnapshotContractError(f"monthly activity outside snapshot universe: {key!r}")
        if stock_codes and stock_code not in stock_codes:
            raise SnapshotContractError(f"monthly activity outside stock scope: {key!r}")
        occurrence_count = _required_nonnegative_int(
            row.get("occurrence_count"), field=f"monthly_activity[{index}].occurrence_count"
        )
        outbound_quantity = _required_nonnegative_int(
            row.get("outbound_quantity"), field=f"monthly_activity[{index}].outbound_quantity"
        )
        outbound_day_count = _required_nonnegative_int(
            row.get("outbound_day_count"), field=f"monthly_activity[{index}].outbound_day_count"
        )
        if occurrence_count <= 0 or outbound_quantity <= 0:
            raise SnapshotContractError(f"monthly activity must contain positive aggregate: {key!r}")
        if outbound_day_count <= 0 or outbound_day_count > occurrence_count:
            raise SnapshotContractError(f"monthly outbound day count is inconsistent: {key!r}")
        monthly_product_counts[product_code] += occurrence_count
        normal_event_count += occurrence_count

    if product_counts != {
        code: monthly_product_counts.get(code, 0) for code in sorted(frequency_by_product)
    }:
        raise SnapshotContractError("product occurrence totals do not match monthly activity")
    expected_grades = {
        row["product_code"]: row["frequency_grade"]
        for row in assign_frequency_grades(product_counts, frequency_by_product)
    }
    actual_grades = {
        code: str(row.get("frequency_grade") or "") for code, row in frequency_by_product.items()
    }
    if actual_grades != expected_grades:
        raise SnapshotContractError("frequency grades do not match occurrence bands")

    expected_grade_counts = {grade: 0 for grade in (*FREQUENCY_GRADES, "X")}
    for grade in actual_grades.values():
        expected_grade_counts[grade] += 1
    if _required_nonnegative_int(summary.get("product_count"), field="summary.product_count") != len(
        frequency_rows
    ):
        raise SnapshotContractError("summary product_count does not match")
    if _required_nonnegative_int(
        summary.get("normal_event_count"), field="summary.normal_event_count"
    ) != normal_event_count:
        raise SnapshotContractError("summary normal_event_count does not match")
    _required_nonnegative_int(
        summary.get("ignored_product_event_count"), field="summary.ignored_product_event_count"
    )
    if summary.get("grade_counts") != expected_grade_counts:
        raise SnapshotContractError("summary grade_counts does not match")


def validate_frequency_snapshot_payload(
    payload: Mapping[str, Any] | None,
    *,
    expected_key: SnapshotKey,
) -> SnapshotReadResult:
    if not payload:
        return SnapshotReadResult(status=SNAPSHOT_STATUS_MISSING, reason="snapshot payload is missing")
    if (
        payload.get("schema_version") != expected_key.schema_version
        or payload.get("algorithm_version") != expected_key.algorithm_version
    ):
        return SnapshotReadResult(
            status=SNAPSHOT_STATUS_VERSION_MISMATCH,
            reason="snapshot version does not match",
        )
    try:
        _validate_frequency_payload_structure(payload, expected_key=expected_key)
    except SnapshotContractError as exc:
        if str(exc) == "snapshot key does not match":
            return SnapshotReadResult(status=SNAPSHOT_STATUS_STALE, reason=str(exc))
        return SnapshotReadResult(status=SNAPSHOT_STATUS_CORRUPT, reason=str(exc))
    return SnapshotReadResult(status=SNAPSHOT_STATUS_READY, payload=payload)


def evaluate_frequency_snapshot(
    payload: Mapping[str, Any] | None,
    *,
    expected_key: SnapshotKey,
    repository_status: str = SNAPSHOT_STATUS_READY,
) -> SnapshotReadResult:
    if repository_status != SNAPSHOT_STATUS_READY:
        return SnapshotReadResult(status=repository_status, reason="repository status is not ready")
    return validate_frequency_snapshot_payload(payload, expected_key=expected_key)


def frequency_rows_for_universe(
    result: SnapshotReadResult,
    product_codes: Sequence[Any],
) -> list[dict[str, Any]]:
    universe = tuple(sorted({_normalize_code(code, field="product_code") for code in product_codes}))
    if result.usable:
        payload_rows = result.payload.get("product_frequency", []) if result.payload else []
        by_product = {
            str(row.get("product_code")): dict(row)
            for row in payload_rows
            if isinstance(row, Mapping) and row.get("product_code")
        }
        if set(by_product) != set(universe):
            status = SNAPSHOT_STATUS_STALE
        else:
            return [by_product[code] for code in universe]
    else:
        status = result.status
    return [
        {
            "product_code": code,
            "occurrence_count_3m": None,
            "frequency_grade": FREQUENCY_INSUFFICIENT_GRADE,
            "data_status": status,
        }
        for code in universe
    ]


def frequency_rows_for_product_subset(
    result: SnapshotReadResult,
    product_codes: Sequence[Any],
) -> list[dict[str, Any]]:
    """Return approved snapshot frequencies for a displayed product subset.

    The repository has already validated the immutable full-universe payload.
    Consumers that merely filter that universe must not re-grade their subset.
    """
    requested = tuple(sorted({_normalize_code(code, field="product_code") for code in product_codes}))
    if result.usable:
        payload_rows = result.payload.get("product_frequency", []) if result.payload else []
        by_product = {
            str(row.get("product_code")): dict(row)
            for row in payload_rows
            if isinstance(row, Mapping) and row.get("product_code")
        }
        return [
            by_product.get(
                code,
                {
                    "product_code": code,
                    "occurrence_count_3m": None,
                    "frequency_grade": FREQUENCY_INSUFFICIENT_GRADE,
                    "data_status": SNAPSHOT_STATUS_STALE,
                },
            )
            for code in requested
        ]
    return [
        {
            "product_code": code,
            "occurrence_count_3m": None,
            "frequency_grade": FREQUENCY_INSUFFICIENT_GRADE,
            "data_status": result.status,
        }
        for code in requested
    ]
