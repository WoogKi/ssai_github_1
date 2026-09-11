"""Fetch KASI special-day data and reconcile it into the shared SSAI DB.

This explicit administrative tool is intentionally separate from runtime
queries.  It performs no write unless --apply is passed and never exposes the
configured service key in its output.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.business_calendar_service import kst_today  # noqa: E402
from app.services.ssai_business_calendar_repository import initial_calendar_years, upsert_official_holiday_years  # noqa: E402
from app.services.ssai_korean_holiday_openapi import (  # noqa: E402
    KASI_SPECIAL_DAY_SOURCE,
    fetch_official_holidays_for_year,
    holiday_api_settings,
)


def run(*, apply: bool, years: tuple[int, ...] | None = None) -> dict[str, object]:
    target_years = tuple(sorted(set(years or initial_calendar_years(base_date=kst_today()))))
    settings = holiday_api_settings()
    result: dict[str, object] = {
        "applied": bool(apply),
        "years": target_years,
        "source": KASI_SPECIAL_DAY_SOURCE,
        "service_key_configured": bool(settings.service_key),
    }
    if not settings.service_key:
        result["status"] = "not_run"
        result["reason_code"] = "holiday_api_service_key_missing"
        return result
    records_by_year = {year: fetch_official_holidays_for_year(year=year, settings=settings) for year in target_years}
    result["holiday_counts"] = {str(year): len(records_by_year[year]) for year in target_years}
    if not apply:
        result["status"] = "preview"
        return result
    source_version = f"kasi-fetch:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    synced = upsert_official_holiday_years(records_by_year=records_by_year, source_version=source_version)
    result.update({
        "status": "synced",
        "inserted": synced.inserted,
        "updated": synced.updated,
        "deleted": synced.deleted,
        "source_version": synced.source_version,
    })
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write reconciled data to the shared SSAI DB")
    parser.add_argument("--year", action="append", type=int, dest="years")
    args = parser.parse_args()
    try:
        payload = run(apply=bool(args.apply), years=tuple(args.years or ()))
        payload["ok"] = True
    except Exception as exc:
        payload = {"applied": bool(args.apply), "ok": False, "error_type": type(exc).__name__}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
