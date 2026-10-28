"""Explicitly non-operational, posthoc DB3 backtest dataset preparation."""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping

from app.services.saved_io_scope import resolve_saved_io_scope


def _days(start: date, end: date) -> list[str]:
    return [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]


def create_research_dataset(
    source: Path, target_root: Path, *, saved_io_codes: list[str],
    profile_fingerprint: str,
) -> Path:
    """Treat missing days as zero only within a visibly marked research assumption."""
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    source_bytes = (source / "fixture.json").read_bytes()
    if (manifest.get("company_id") != 3 or manifest.get("source_db_alias") != "DB3"
            or manifest.get("sha256_by_file", {}).get("fixture.json") != hashlib.sha256(source_bytes).hexdigest()
            or manifest.get("source_extraction_window", {}).get("sample_from") != "2026-04-01"
            or manifest.get("source_extraction_window", {}).get("sample_to") != "2026-08-30"):
        raise ValueError("DB3 source checksum or scope mismatch")
    fixture = json.loads(source_bytes)
    if fixture.get("company_id") != 3 or len(fixture.get("outbound_rows", [])) > 500:
        raise ValueError("DB3 source sample is not bounded")
    scope = resolve_saved_io_scope(saved_io_codes, confirmed_trade_roles=True)
    if not scope.outbound_codes or not profile_fingerprint:
        raise ValueError("saved Dashboard I/O scope is unverified")
    version = str(manifest["dataset_version"]) + "-research-v1"
    target = target_root / "datasets" / version
    if target.exists():
        raise FileExistsError("research dataset already exists")
    fixture["dataset_version"] = version
    fixture["saved_io_codes"] = list(scope.all_codes)
    fixture["complete_dates"] = _days(date(2026, 4, 1), date(2026, 8, 30))
    fixture["timestamp_policy_confirmed"] = True
    fixture["calendar_asof_required"] = False
    derived = dict(manifest)
    derived.update({
        "dataset_version": version, "dataset_status": "RESEARCH_POSTHOC",
        "evaluation_grade": "research_posthoc",
        "research_assumption": "no R120 rows for a selected product/day means zero within this cloned source; completeness not independently certified",
        "source_fixture_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "saved_io_profile_fingerprint": profile_fingerprint,
        "saved_io_count": len(scope.all_codes),
        "calendar_asof_evidence": "posthoc_only",
        "last_complete_date": None,
    })
    target.mkdir(parents=True, exist_ok=False)
    payload = (json.dumps(fixture, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    derived["sha256_by_file"] = {"fixture.json": hashlib.sha256(payload).hexdigest()}
    (target / "fixture.json").write_bytes(payload)
    (target / "manifest.json").write_text(json.dumps(derived, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return target
