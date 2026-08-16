from __future__ import annotations

import calendar
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
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
