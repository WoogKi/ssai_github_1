"""Read-only independent checks of one DB3 research dataset and its predictions."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.ml.phase1 import _day, _decimal, _window_summary, build_daily, prepare_sources


def check(dataset: Path, run: Path) -> dict:
    manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
    raw = (dataset / "fixture.json").read_bytes()
    if (manifest.get("company_id") != 3 or manifest.get("dataset_status") != "RESEARCH_POSTHOC"
            or manifest.get("sha256_by_file", {}).get("fixture.json") != hashlib.sha256(raw).hexdigest()):
        raise ValueError("research dataset identity or checksum mismatch")
    fixture = json.loads(raw)
    if (fixture.get("schema_version") != "ml_phase1_fixture_v1"
            or fixture.get("company_id") != 3
            or fixture.get("dataset_version") != manifest.get("dataset_version")
            or fixture.get("calendar_asof_required") is not False
            or manifest.get("evaluation_grade") != "research_posthoc"):
        raise ValueError("research fixture contract mismatch")
    predictions = json.loads((run / "predictions.json").read_text(encoding="utf-8"))
    rows = fixture["outbound_rows"]
    prepared = prepare_sources(
        company_id=3, outbound_rows=rows, document_rows=fixture["document_rows"],
        vendor_rows=fixture["vendor_rows"], product_rows=fixture["product_rows"],
        standard_rows=fixture["standard_rows"], code_rows=fixture["code_rows"],
        saved_io_codes=fixture["saved_io_codes"],
    )
    if (len(rows) != int(manifest["metrics"]["source_rows"])
            or sum((Decimal(str(row["Rd12_Quantity"])) for row in rows), Decimal(0))
            != Decimal(manifest["metrics"]["source_quantity"])):
        raise ValueError("R120 source rows or quantity changed")
    product_codes = set(prepared.product_codes)
    if len(product_codes) != int(manifest["metrics"]["selected_products"]):
        raise ValueError("product count changed")
    daily = build_daily(
        prepared, company_id=3, date_from=date.fromisoformat(fixture["date_from"]),
        date_to=date.fromisoformat(fixture["date_to"]),
        complete_dates=(date.fromisoformat(day) for day in fixture["complete_dates"]),
    )
    checked = withheld = unevaluated = 0
    for item in predictions:
        if (item.get("company_id") != 3 or item["product_code"] not in product_codes
                or item["as_of"] not in fixture["as_of_dates"]):
            raise ValueError("prediction company, product or as-of differs from fixture")
        if item["evaluation_grade"] != "research_posthoc" or int(item["horizon_days"]) != 30:
            raise ValueError("prediction grade or horizon mismatch")
        as_of = date.fromisoformat(item["as_of"])
        end = as_of + timedelta(days=30)
        cutoff = datetime.combine(as_of, datetime.max.time())
        history_start = as_of - timedelta(days=89)
        product = item["product_code"]
        history = [row.source for row in prepared.rows if row.source.product_code == product
                   and row.source.io_code in prepared.normal_codes
                   and history_start <= row.source.out_date <= as_of]
        if item["status"] != "ready":
            withheld += 1
            continue
        history_summary = _window_summary(daily, 3, product, history_start, as_of)
        if (fixture.get("timestamp_policy_confirmed") is not True or history_summary is None
                or any(row.add_time is None or row.mod_time is None or row.add_time > cutoff
                       or row.mod_time > cutoff or row.quantity < 0 for row in history)):
            raise ValueError("ready prediction has unavailable or negative as-of history")
        label_summary = _window_summary(daily, 3, product, as_of + timedelta(days=1), end)
        if label_summary is None or label_summary[1]:
            if item["actual_quantity"] is not None:
                raise ValueError("incomplete or negative label must remain unevaluated")
            unevaluated += 1
            continue
        if item["actual_quantity"] is None:
            raise ValueError("missing actual quantity has no incomplete or negative-label evidence")
        # Keep an independent source sum; the shared daily contract only decides availability.
        actual = sum((_decimal(row["Rd12_Quantity"]) for row in rows
                      if str(row["Rd12_Physic_Cd"]).strip() == product
                      and str(row["Rd12_Io_Gu"]).strip() in prepared.normal_codes
                      and as_of < _day(row["Rd12_Out_YyMmDd"]) <= end), Decimal(0))
        if actual != _decimal(item["actual_quantity"]):
            raise ValueError("30-calendar-day label differs from raw R120")
        checked += 1
    return {"company_id": 3, "source_rows": len(rows), "products": len(product_codes),
            "checked_ready_model_rows": checked, "withheld_model_rows": withheld,
            "ready_without_label_model_rows": unevaluated,
            "evaluation_grade": "research_posthoc"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(check(args.dataset, args.run), ensure_ascii=False))


if __name__ == "__main__":
    main()
