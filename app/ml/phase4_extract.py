"""Bounded DB3 product-batch extraction for research-only 30-day backtests."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from app.ml.db3_probe import SAMPLE_R120, SAMPLE_R130, _select_codes
from app.ml.phase1 import METRIC_POLICY_VERSION, prepare_sources
from app.services.saved_io_scope import resolve_saved_io_scope


Query = Callable[[str, tuple[Any, ...], int], list[dict[str, Any]]]
KST = timezone(timedelta(hours=9))
START, END_EXCLUSIVE = "20260401", "20260831"
PER_BATCH_CAP = 10000
TOTAL_CAP = 30000

CANDIDATES = """
SELECT TOP (200) LTRIM(RTRIM(Rd12_Physic_Cd)) AS product_code,
       COUNT_BIG(*) AS row_count, COUNT(DISTINCT Rd12_Out_YyMmDd) AS active_days,
       MIN(Rd12_Quantity) AS min_quantity, MAX(Rd12_Quantity) AS max_quantity,
       SUM(CASE WHEN Rd12_Quantity < 0 THEN 1 ELSE 0 END) AS negative_rows
FROM dbo.Rddbc120
WHERE Rd12_Out_YyMmDd >= ? AND Rd12_Out_YyMmDd < ? AND Rd12_Io_Gu IN ({io_marks})
GROUP BY Rd12_Physic_Cd
HAVING COUNT_BIG(*) BETWEEN ? AND ?
ORDER BY COUNT_BIG(*) {order_direction}, product_code
"""

SOURCE_TOTALS = """
SELECT LTRIM(RTRIM(Rd12_Physic_Cd)) AS product_code, COUNT_BIG(*) AS row_count,
       SUM(CAST(ISNULL(Rd12_Quantity, 0) AS decimal(38, 4))) AS quantity,
       SUM(CAST(ISNULL(Rd12_Oquantity, 0) AS decimal(38, 4))) AS bonus_quantity
FROM dbo.Rddbc120
WHERE Rd12_Out_YyMmDd >= ? AND Rd12_Out_YyMmDd < ?
  AND Rd12_Physic_Cd IN ({product_marks})
  AND (Rd12_Io_Gu LIKE '5%' OR Rd12_Io_Gu LIKE '6%')
GROUP BY Rd12_Physic_Cd
"""


def select_products(high: list[dict], low: list[dict], standard_codes: set[str]) -> tuple[tuple[str, ...], dict[str, list[str]]]:
    candidates = {str(row["product_code"]).strip(): row for row in (*high, *low)}
    if not high or not low:
        raise ValueError("high and intermittent candidate groups are required")
    picks: dict[str, list[str]] = {}

    def add(category: str, rows: list[dict], count: int = 2) -> None:
        names = []
        for row in rows:
            code = str(row["product_code"]).strip()
            if code and code not in selected:
                selected.add(code)
                names.append(code)
            if len(names) == count:
                break
        picks[category] = names

    selected: set[str] = set()
    add("high_frequency", high[:30])
    add("ordinary", high[50:150] or high[2:])
    add("intermittent", low)
    volatile = sorted(candidates.values(), key=lambda row: (
        abs(Decimal(str(row["max_quantity"])) - Decimal(str(row["min_quantity"]))),
        int(row["active_days"]), str(row["product_code"])), reverse=True)
    add("high_variation", volatile)
    add("no_standard_code", [row for row in (*high, *low) if str(row["product_code"]).strip() not in standard_codes])
    add("with_standard_code", [row for row in (*high, *low) if str(row["product_code"]).strip() in standard_codes])
    if len(selected) < 6 or len(selected) > 12:
        raise ValueError("representative product selection is insufficient")
    return tuple(sorted(selected)), picks


def extract_research_fixture(
    query: Query, *, company_id: int, saved_io_codes: list[str], calendar: dict[str, Any],
    profile_fingerprint: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if company_id != 3 or calendar.get("status") != "ready" or not profile_fingerprint:
        raise ValueError("DB3, saved profile and common Calendar are required")
    scope = resolve_saved_io_scope(saved_io_codes, confirmed_trade_roles=True)
    if not scope.outbound_codes:
        raise ValueError("no saved normal-outbound codes")
    calls = 0
    durations = []

    def limited(sql: str, params: tuple[Any, ...], cap: int) -> list[dict[str, Any]]:
        nonlocal calls
        if not sql.lstrip().upper().startswith("SELECT") or calls >= 40:
            raise ValueError("DB3 SELECT count exceeded")
        started = perf_counter()
        rows = query(sql, params, cap)
        durations.append(int((perf_counter() - started) * 1000))
        calls += 1
        if len(rows) > cap:
            raise ValueError("DB3 result exceeded cap")
        return rows

    io_marks = ", ".join("?" for _ in scope.outbound_codes)
    high = limited(CANDIDATES.format(io_marks=io_marks, order_direction="DESC"),
                   (START, END_EXCLUSIVE, *scope.outbound_codes, 30, 800), 200)
    low = limited(CANDIDATES.format(io_marks=io_marks, order_direction="ASC"),
                  (START, END_EXCLUSIVE, *scope.outbound_codes, 3, 29), 200)
    candidate_codes = tuple(sorted({str(row["product_code"]).strip() for row in (*high, *low)}))
    candidate_marks = ", ".join("?" for _ in candidate_codes)
    standard_rows = limited(
        f"SELECT Rd046_Physic_Cd, Rd046_Standard_Cd FROM dbo.Rddbc046 WHERE Rd046_Physic_Cd IN ({candidate_marks})",
        candidate_codes, 2000,
    )
    standard_codes = {str(row["Rd046_Physic_Cd"]).strip() for row in standard_rows if row.get("Rd046_Standard_Cd")}
    products, categories = select_products(high, low, standard_codes)
    marks = ", ".join("?" for _ in products)
    totals = limited(SOURCE_TOTALS.format(product_marks=marks), (START, END_EXCLUSIVE, *products), len(products))
    source_count = sum(int(row["row_count"]) for row in totals)
    source_quantity = sum((Decimal(str(row["quantity"])) for row in totals), Decimal(0))
    source_bonus = sum((Decimal(str(row["bonus_quantity"])) for row in totals), Decimal(0))
    if source_count > TOTAL_CAP:
        raise ValueError("selected product source exceeds memory budget")
    outbound: list[dict] = []
    documents: dict[tuple[str, str, str, str], dict] = {}
    for offset in range(0, len(products), 3):
        batch = products[offset:offset + 3]
        batch_marks = ", ".join("?" for _ in batch)
        rows = limited(SAMPLE_R120.format(product_marks=batch_marks, row_limit=PER_BATCH_CAP + 1),
                       (START, END_EXCLUSIVE, *batch), PER_BATCH_CAP + 1)
        if len(rows) > PER_BATCH_CAP:
            raise ValueError("R120 product batch is truncated")
        outbound.extend(rows)
        headers = limited(SAMPLE_R130.format(product_marks=batch_marks, row_limit=PER_BATCH_CAP + 1),
                          (START, END_EXCLUSIVE, *batch), PER_BATCH_CAP + 1)
        if len(headers) > PER_BATCH_CAP:
            raise ValueError("R130 product batch is truncated")
        for row in headers:
            if int(row["header_count"]) != 1:
                raise ValueError("R130 duplicate document key")
            key = tuple(str(row[field]).strip() for field in (
                "Rd13_Trans_Di", "Rd13_Trans_YyMmDd", "Rd13_Ven_Cd", "Rd13_Trans_Seq"
            ))
            old = documents.setdefault(key, row)
            if old != row:
                raise ValueError("R130 header changed across product batches")
    if len(outbound) != source_count or sum((Decimal(str(row["Rd12_Quantity"])) for row in outbound), Decimal(0)) != source_quantity:
        raise ValueError("R120 row count or quantity differs from independent SQL aggregate")
    vendors = []
    vendor_codes = tuple(sorted({str(row["Rd12_Ven_Cd"]).strip() for row in outbound}))
    for offset in range(0, len(vendor_codes), 100):
        batch = vendor_codes[offset:offset + 100]
        vendors.extend(limited(_select_codes(
            "Rddbc030", "Rd03_Ven_Cd, Rd03_Ven_Num, Rd03_Other1, Rd03_Supply_Cd_Gcode, Rd03_Supply_Cd",
            "Rd03_Ven_Cd", batch), batch, 501))
    product_rows = limited(_select_codes("Rddbc040", "Rd04_Physic_Cd, Rd04_Add_Date",
                                         "Rd04_Physic_Cd", products), products, 501)
    selected_standard_rows = [row for row in standard_rows if str(row["Rd046_Physic_Cd"]).strip() in products]
    group_codes = tuple(sorted({"0012", *(str(row.get("Rd03_Supply_Cd_Gcode") or "").strip() for row in vendors)} - {""}))
    code_rows = limited(
        f"SELECT TOP (501) Rd01_Gcode, Rd01_Tcode, Rd01_Hnm FROM dbo.Rddbc010 WHERE Rd01_Gcode IN ({', '.join('?' for _ in group_codes)})",
        group_codes, 501,
    )
    for collection in (outbound, list(documents.values()), vendors, product_rows, selected_standard_rows, code_rows):
        for row in collection:
            row["company_id"] = company_id
    prepared = prepare_sources(
        company_id=company_id, outbound_rows=outbound, document_rows=documents.values(),
        vendor_rows=vendors, product_rows=product_rows, standard_rows=selected_standard_rows,
        code_rows=code_rows, saved_io_codes=scope.all_codes,
    )
    if prepared.source_row_count != source_count or prepared.source_quantity != source_quantity or prepared.source_bonus_quantity != source_bonus:
        raise ValueError("prepared source differs from SQL aggregate")
    version = "db3-research-" + datetime.now(KST).strftime("%Y%m%d%H%M%S")
    dates = [(date(2026, 4, 1) + timedelta(days=offset)).isoformat() for offset in range(152)]
    fixture = {
        "schema_version": "ml_phase1_fixture_v1", "dataset_version": version, "company_id": 3,
        "date_from": "2026-04-01", "date_to": "2026-08-30", "outbound_rows": outbound,
        "document_rows": list(documents.values()), "vendor_rows": vendors,
        "product_rows": product_rows, "standard_rows": selected_standard_rows, "code_rows": code_rows,
        "saved_io_codes": list(scope.all_codes), "complete_dates": dates,
        "as_of_dates": ["2026-06-30", "2026-07-31"], "timestamp_policy_confirmed": True,
        "calendar_asof_required": False,
        "calendar": {key: calendar[key] for key in ("start_date", "end_date", "holiday_dates", "known_as_of", "version")},
    }
    metrics = {
        "dataset_version": version, "source_rows": source_count, "source_quantity": str(source_quantity),
        "source_bonus_quantity": str(source_bonus), "selected_products": len(products),
        "category_counts": {category: len(values) for category, values in categories.items()},
        "query_count": calls, "query_durations_ms": durations,
        "document_match_counts": dict(prepared.document_match_counts),
        "negative_normal_rows": sum(row.source.quantity < 0 and row.source.io_code in prepared.normal_codes for row in prepared.rows),
        "calendar_known_as_of": calendar["known_as_of"], "profile_fingerprint": profile_fingerprint,
    }
    return fixture, metrics


def save_research_dataset(root: Path, fixture: dict[str, Any], metrics: dict[str, Any]) -> Path:
    target = root / "datasets" / fixture["dataset_version"]
    target.mkdir(parents=True, exist_ok=False)
    payload = (json.dumps(fixture, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")) + "\n").encode("utf-8")
    (target / "fixture.json").write_bytes(payload)
    manifest = {
        "schema_version": "ml_db3_research_v1", "dataset_version": fixture["dataset_version"],
        "company_id": 3, "source_db_alias": "DB3", "dataset_status": "RESEARCH_POSTHOC",
        "evaluation_grade": "research_posthoc", "source_window": [START, "20260830"],
        "research_assumption": "selected-product missing dates are zero in the clone; independent completeness not certified",
        "last_complete_date": None, "sha256_by_file": {"fixture.json": hashlib.sha256(payload).hexdigest()},
        "metrics": metrics,
    }
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return target
