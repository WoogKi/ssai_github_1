"""Create a bounded legacy research fixture from verified DB3 bulk shards, without DB access."""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

from app.ml.bulk_dataset import MAX_BATCH_BYTES, _load_master, _month_batches, materialize_research_cohort
from app.ml.integration import ml_storage_root
from app.ml.learning import _load_fixture


def _input_file(path: Path, root: Path, limit: int) -> Path:
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("dataset links are not allowed")
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("dataset input is absent or outside storage root")
    if path.stat().st_size > limit:
        raise ValueError("bounded adapter input exceeds byte cap")
    return path


def _iso_day(value: str) -> date:
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError("dates must use YYYY-MM-DD")
    return result


def run(root: Path, *, bulk_version: str, cohort_version: str, output_version: str,
        date_from: str, date_to: str, as_of_dates: tuple[str, ...]) -> Path:
    """Validate the small cohort before the common, full offline bulk verification."""
    for version in (bulk_version, cohort_version, output_version):
        if not version or not version.isascii() or not version.replace("-", "").isalnum():
            raise ValueError("dataset versions must be simple ASCII names")
    if not output_version.startswith("db3-research-"):
        raise ValueError("invalid bounded research version")
    target = root / "datasets" / output_version
    if target.exists() or target.is_symlink():
        raise FileExistsError("output dataset already exists")
    cohort = root / "datasets" / cohort_version
    _input_file(cohort / "manifest.json", root, 1024 * 1024)
    fixture, manifest = _load_fixture(_input_file(cohort / "fixture.json", root, MAX_BATCH_BYTES))
    if manifest["company_id"] != 3 or manifest["dataset_version"] != cohort_version:
        raise ValueError("prior DB3 cohort identity differs")
    masters = _load_master(fixture["product_rows"])
    if not 1 <= len(masters) <= 12:
        raise ValueError("bounded training adapter accepts 1..12 unique products")
    codes = tuple(row["Rd04_Physic_Cd"].strip() for row in masters)
    start, end = _iso_day(date_from), _iso_day(date_to)
    cutoffs = tuple(_iso_day(value) for value in as_of_dates)
    if (not cutoffs or len(set(cutoffs)) != len(cutoffs) or start > end
            or any(day - timedelta(days=89) < start or day + timedelta(days=30) > end for day in cutoffs)):
        raise ValueError("unique as-of dates require 90-day history and 30-day labels")
    calendar = fixture.get("calendar")
    if not isinstance(calendar, dict) or calendar.get("status", "ready") != "ready":
        raise ValueError("cohort Calendar is not ready")
    cal_start, cal_end = _iso_day(calendar["start_date"]), _iso_day(calendar["end_date"])
    _iso_day(calendar["known_as_of"])
    if (cal_start > start or cal_end < end or not isinstance(calendar["version"], str)
            or not calendar["version"].strip() or not isinstance(calendar["holiday_dates"], list)
            or any(not cal_start <= _iso_day(day) <= cal_end for day in calendar["holiday_dates"])):
        raise ValueError("cohort Calendar does not cover the requested source period")
    bulk = root / "datasets" / bulk_version
    plan = json.loads(_input_file(bulk / "plan.json", root, 1024 * 1024).read_text(encoding="utf-8"))
    summary = json.loads(_input_file(bulk / "manifest.json", root, 1024 * 1024).read_text(encoding="utf-8"))
    if (summary.get("schema_version") != "ml_db3_bulk_v1"
            or summary.get("dataset_status") != "RESEARCH_POSTHOC"
            or summary.get("evaluation_grade") != "research_posthoc"
            or any(item.get("company_id") != 3 or item.get("dataset_version") != bulk_version
                   for item in (plan, summary))
            or start < _iso_day(plan["date_from"]) or end > _iso_day(plan["date_to"])
            or set(fixture["saved_io_codes"]) != set(plan["saved_io_codes"])):
        raise ValueError("bulk identity, source window or Saved IO contract differs")
    profile = manifest.get("metrics", {}).get("profile_fingerprint")
    if profile is not None and profile != plan["profile_fingerprint"]:
        raise ValueError("cohort and bulk Saved IO profile differ")
    # The producer may explicitly have no extraction-time Calendar. Do not invent one.
    if plan["calendar_version"] not in ("UNAVAILABLE_ASOF_EXTRACTION", calendar["version"]):
        raise ValueError("bulk and cohort Calendar versions differ")
    _month_batches(codes, start, end, int(plan["group_size"]))
    _input_file(bulk / "products.jsonl", root, MAX_BATCH_BYTES)
    del fixture, manifest, masters
    return materialize_research_cohort(
        bulk, root, dataset_version=output_version, product_codes=codes,
        as_of_dates=as_of_dates, calendar={**calendar, "status": "ready"}, date_from=start, date_to=end,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bulk-version", required=True)
    parser.add_argument("--cohort-version", required=True)
    parser.add_argument("--output-version", required=True)
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--as-of-date", action="append", required=True)
    args = parser.parse_args()
    target = run(
        ml_storage_root(), bulk_version=args.bulk_version, cohort_version=args.cohort_version,
        output_version=args.output_version, date_from=args.date_from, date_to=args.date_to,
        as_of_dates=tuple(args.as_of_date),
    )
    result = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    fixture, _ = _load_fixture(target / "fixture.json")
    print(json.dumps({"dataset_version": result["dataset_version"],
                      "source_rows": result["source_rows"], "product_count": len(fixture["product_rows"]),
                      "evaluation_grade": result["evaluation_grade"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
