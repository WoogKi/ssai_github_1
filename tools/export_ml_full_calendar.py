"""Export the existing shared Calendar as an immutable offline ML input."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from app.ml.integration import ml_storage_root, official_calendar_snapshot


START = date(2018, 1, 1)
END = date(2026, 8, 30)


def calendar_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    if (snapshot.get("status") != "ready"
            or snapshot.get("start_date") != START.isoformat()
            or snapshot.get("end_date") != END.isoformat()
            or not snapshot.get("version")
            or not snapshot.get("known_as_of")):
        raise ValueError("shared Calendar coverage or persisted load evidence is incomplete")
    known = date.fromisoformat(snapshot["known_as_of"])
    if known < START:
        raise ValueError("shared Calendar load date is outside the source period")
    holidays = snapshot.get("holiday_dates")
    if not isinstance(holidays, list) or holidays != sorted(set(holidays)):
        raise ValueError("shared Calendar dates are missing, duplicated, or unsorted")
    for value in holidays:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{8}", value):
            raise ValueError("shared Calendar has an invalid holiday date")
        parsed = date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:]}")
        if not START <= parsed <= END:
            raise ValueError("shared Calendar holiday is outside the requested range")
    return {key: snapshot[key] for key in (
        "start_date", "end_date", "holiday_dates", "known_as_of", "version")}


def export(*, dataset_name: str) -> dict[str, Any]:
    if not re.fullmatch(r"calendar-db3-full-[A-Za-z0-9_-]{1,40}", dataset_name):
        raise ValueError("invalid Calendar dataset name")
    root = ml_storage_root()
    target_dir = root / "datasets" / dataset_name
    if target_dir.exists() or target_dir.is_symlink():
        raise FileExistsError("Calendar dataset already exists")
    snapshot = official_calendar_snapshot(start_date=START, end_date=END)
    payload = calendar_payload(snapshot)
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    target_dir.mkdir(parents=True, exist_ok=False)
    path = target_dir / "calendar.json"
    with path.open("xb") as stream:
        stream.write(raw)
    digest = hashlib.sha256(raw).hexdigest()
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError("offline Calendar readback SHA256 mismatch")
    return {"calendar_path": str(path), "sha256": digest,
            "start_date": payload["start_date"], "end_date": payload["end_date"],
            "known_as_of": payload["known_as_of"], "holiday_count": len(payload["holiday_dates"]),
            "validation_grade": "RESEARCH_POSTHOC"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-name", required=True)
    args = parser.parse_args()
    result = export(dataset_name=args.dataset_name)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
