"""Bounded DB3 sample contract; completeness is never inferred from activity."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from app.ml.phase1 import METRIC_POLICY_VERSION, prepare_sources


KST = timezone(timedelta(hours=9))
SAMPLE_MAX_ROWS = 500
DAILY_MAX_ROWS = 2200
BASE_FROM = date(2026, 4, 1)
BASE_TO = date(2026, 10, 1)
PARTIAL_FROM = date(2026, 9, 24)


def validate_db3_scope(*, company_id: int, date_from: date, date_to: date, alias: str) -> None:
    if company_id != 3 or alias != "DB3" or date_from < BASE_FROM or date_to > BASE_TO or date_to < date_from:
        raise ValueError("DB3 company or date scope is not approved")


def completeness_by_day(
    daily_rows: list[Mapping[str, Any]], *, date_from: date, date_to: date,
    approved_complete_dates: frozenset[date] = frozenset(),
) -> dict[str, str]:
    if any(day < date_from or day > date_to for day in approved_complete_dates):
        raise ValueError("complete date outside extraction scope")
    observed = {str(row["out_date"]).strip() for row in daily_rows}
    result: dict[str, str] = {}
    day = date_from
    while day <= date_to:
        key = day.strftime("%Y%m%d")
        if day in approved_complete_dates:
            result[key] = "confirmed_complete"
        elif day >= PARTIAL_FROM:
            result[key] = "partial_unconfirmed"
        else:
            result[key] = "observed_unverified" if key in observed else "no_rows_unverified"
        day += timedelta(days=1)
    return result


def _encode(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def write_probe_dataset(
    root: Path, *, fixture: dict[str, Any], daily_r120: list[Mapping[str, Any]],
    daily_r130: list[Mapping[str, Any]], approval_reference: str, query_durations_ms: list[int],
    probe_quality: Mapping[str, Any],
) -> dict[str, Any]:
    """Write a sample dataset as REVIEW_REQUIRED, never as verified full history."""
    company_id = int(fixture["company_id"])
    date_from = date.fromisoformat(fixture["date_from"])
    date_to = date.fromisoformat(fixture["date_to"])
    validate_db3_scope(company_id=company_id, date_from=date_from, date_to=date_to, alias="DB3")
    if len(fixture["outbound_rows"]) > SAMPLE_MAX_ROWS or len(daily_r120) > DAILY_MAX_ROWS:
        raise ValueError("sample or daily aggregation exceeded cap")
    if fixture.get("complete_dates"):
        raise ValueError("probe cannot certify complete dates")
    prepared = prepare_sources(
        company_id=company_id, outbound_rows=fixture["outbound_rows"],
        document_rows=fixture["document_rows"], vendor_rows=fixture["vendor_rows"],
        product_rows=fixture["product_rows"], standard_rows=fixture["standard_rows"],
        code_rows=fixture["code_rows"], saved_io_codes=fixture["saved_io_codes"],
    )
    root.mkdir(parents=True, exist_ok=False)
    fixture_bytes = (json.dumps(fixture, ensure_ascii=False, sort_keys=True, default=_encode, separators=(",", ":")) + "\n").encode("utf-8")
    digest = hashlib.sha256(fixture_bytes).hexdigest()
    (root / "fixture.json").write_bytes(fixture_bytes)
    states_r120 = completeness_by_day(daily_r120, date_from=BASE_FROM, date_to=BASE_TO)
    states_r130 = completeness_by_day(daily_r130, date_from=BASE_FROM, date_to=BASE_TO)
    r120_total_rows = sum(int(row["row_count"]) for row in daily_r120)
    r130_total_rows = sum(int(row["row_count"]) for row in daily_r130)
    r120_total_quantity = sum((Decimal(str(row["quantity"])) for row in daily_r120), Decimal(0))
    r120_total_bonus = sum((Decimal(str(row["bonus_quantity"])) for row in daily_r120), Decimal(0))
    manifest = {
        "schema_version": "ml_db3_probe_v1", "dataset_version": fixture["dataset_version"],
        "source_db_alias": "DB3", "replica_origin": "DB4_documented_unverified",
        "company_id": company_id, "approval_reference": approval_reference,
        "source_extraction_window": {
            "daily_from": BASE_FROM.isoformat(), "daily_to": BASE_TO.isoformat(),
            "sample_from": fixture["date_from"], "sample_to": fixture["date_to"],
        },
        "extracted_at_kst": datetime.now(KST).isoformat(),
        "source_max_observed_date": max((str(row["out_date"]) for row in daily_r120), default=None),
        "last_complete_date": None, "table_counts": {
            "r120_daily_groups": len(daily_r120), "r130_daily_groups": len(daily_r130),
            "r120_sample_rows": prepared.source_row_count, "r130_sample_headers": len(fixture["document_rows"]),
        },
        "table_totals": {
            "r120_period_rows_all_io": r120_total_rows,
            "r120_period_quantity_all_io": str(r120_total_quantity),
            "r120_period_bonus_all_io": str(r120_total_bonus),
            "r120_period_negative_rows_all_io": sum(int(row["negative_rows"]) for row in daily_r120),
            "r130_period_headers_direction3": r130_total_rows,
            "r120_sample_quantity": str(prepared.source_quantity),
            "r120_sample_bonus_quantity": str(prepared.source_bonus_quantity),
        },
        "completeness_status": {"Rddbc120": states_r120, "Rddbc130": states_r130},
        "sha256_by_file": {"fixture.json": digest}, "classification_map_version": "unverified",
        "calendar_version": "unavailable", "metric_policy_version": METRIC_POLICY_VERSION,
        "asof_evidence_grade": "unavailable", "dataset_status": "REVIEW_REQUIRED",
        "query_durations_ms": query_durations_ms, "probe_quality": dict(probe_quality),
        "limitations": ["bounded_product_sample", "complete_dates_not_certified", "calendar_asof_not_certified"],
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8",
    )
    return manifest
