"""ML storage and existing official Calendar integration."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date
from pathlib import Path
from typing import Any, Callable

from app.services.business_calendar_service import load_official_holidays
from app.services.ssai_auth_service import connect_ssai_db
from app.services.ssai_korean_holiday_openapi import KASI_SPECIAL_DAY_SOURCE
from app.utils.env_config import PROJECT_ROOT, config_path, read_project_env_file


ML_SUBDIRS = ("datasets", "models", "runs", "logs", "diagnostics")


def ml_storage_root() -> Path:
    root = (config_path("SSAI_STORAGE_ROOT", environ=read_project_env_file()) / "ml").resolve()
    if root == PROJECT_ROOT or PROJECT_ROOT in root.parents:
        raise ValueError("ML customer data storage must be outside the Git checkout")
    return root


def migrate_probe_dataset(source: Path, target_root: Path) -> Path:
    """Copy a verified probe without touching the original or an existing target."""
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    name = str(manifest.get("dataset_version") or "")
    if not name.startswith("db3-probe-") or manifest.get("company_id") != 3:
        raise ValueError("not an approved DB3 probe dataset")
    fixture = (source / "fixture.json").read_bytes()
    digest = hashlib.sha256(fixture).hexdigest()
    if manifest.get("sha256_by_file", {}).get("fixture.json") != digest:
        raise ValueError("source dataset checksum mismatch")
    target = target_root / "datasets" / name
    if target.exists():
        raise FileExistsError("target dataset already exists")
    for subdir in ML_SUBDIRS:
        (target_root / subdir).mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=False)
    shutil.copy2(source / "manifest.json", target / "manifest.json")
    shutil.copy2(source / "fixture.json", target / "fixture.json")
    if hashlib.sha256((target / "fixture.json").read_bytes()).hexdigest() != digest:
        raise ValueError("copied dataset checksum mismatch")
    return target


def add_calendar_dataset(source: Path, target_root: Path, calendar: dict[str, Any]) -> Path:
    """Create a new version with official calendar evidence; keep the probe immutable."""
    if calendar.get("status") != "ready":
        raise ValueError("official Calendar is not ready")
    base_manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    source_hash = hashlib.sha256((source / "fixture.json").read_bytes()).hexdigest()
    if base_manifest.get("sha256_by_file", {}).get("fixture.json") != source_hash:
        raise ValueError("source dataset checksum mismatch")
    dataset_version = str(base_manifest["dataset_version"]) + "-calendar-v1"
    target = target_root / "datasets" / dataset_version
    if target.exists():
        raise FileExistsError("calendar dataset already exists")
    fixture = json.loads((source / "fixture.json").read_text(encoding="utf-8"))
    fixture["dataset_version"] = dataset_version
    fixture["calendar"] = {key: calendar[key] for key in (
        "start_date", "end_date", "holiday_dates", "known_as_of", "version"
    )}
    manifest = dict(base_manifest)
    manifest["dataset_version"] = dataset_version
    manifest["calendar_version"] = calendar["version"]
    manifest["calendar_loaded_asof"] = calendar["known_as_of"]
    manifest["source_fixture_sha256"] = source_hash
    manifest["dataset_status"] = "REVIEW_REQUIRED"
    target.mkdir(parents=True, exist_ok=False)
    fixture_bytes = (json.dumps(fixture, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    manifest["sha256_by_file"] = {"fixture.json": hashlib.sha256(fixture_bytes).hexdigest()}
    (target / "fixture.json").write_bytes(fixture_bytes)
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return target


def official_calendar_snapshot(
    *, start_date: date, end_date: date,
    authority_loader: Callable[..., Any] = load_official_holidays,
    connection_factory: Callable[[], Any] = connect_ssai_db,
) -> dict[str, Any]:
    """Read the common authority and its persisted load time; never call the API."""
    authority = authority_loader(start_date=start_date, end_date=end_date)
    if authority.status != "ready":
        return {"status": "unavailable", "reason": authority.reason_code}
    years = tuple(range(start_date.year, end_date.year + 1))
    with connection_factory() as connection:
        connection.timeout = 120
        cursor = connection.cursor()
        rows = cursor.execute(
            "SELECT calendar_year, source_version, payload_checksum, loaded_at "
            "FROM dbo.SSAI_KOREAN_HOLIDAY_LOADS WHERE source = ? AND load_status = 'ready' "
            f"AND calendar_year IN ({', '.join('?' for _ in years)})",
            KASI_SPECIAL_DAY_SOURCE, *years,
        ).fetchall()
    if len(rows) != len(years) or {int(row[0]) for row in rows} != set(years):
        return {"status": "unavailable", "reason": "calendar_load_metadata_incomplete"}
    latest_load = max(row[3] for row in rows)
    if latest_load is None:
        return {"status": "unavailable", "reason": "calendar_load_time_missing"}
    version = "+".join(f"{row[0]}:{row[1]}:{str(row[2])[:12]}" for row in sorted(rows, key=lambda value: value[0]))
    return {
        "status": "ready", "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
        "holiday_dates": sorted(authority.holiday_dates), "known_as_of": latest_load.date().isoformat(),
        "version": version, "source": KASI_SPECIAL_DAY_SOURCE,
    }
