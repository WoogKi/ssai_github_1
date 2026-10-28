"""Migrate a verified DB3 probe and attach persisted Calendar evidence."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from app.ml.integration import add_calendar_dataset, migrate_probe_dataset, ml_storage_root, official_calendar_snapshot
from tools.run_ml_phase1_offline import run_fixture


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    try:
        root = ml_storage_root()
        migrated = migrate_probe_dataset(args.source.resolve(), root)
        calendar = official_calendar_snapshot(start_date=date(2026, 4, 1), end_date=date(2026, 8, 30))
        selected = add_calendar_dataset(migrated, root, calendar) if calendar["status"] == "ready" else migrated
        result = run_fixture(selected / "fixture.json", root / "runs", run_id=selected.name)
        print(json.dumps({
            "migrated_dataset": migrated.name, "selected_dataset": selected.name,
            "calendar_status": calendar["status"], "calendar_known_as_of": calendar.get("known_as_of"),
            "forecast_evaluated": {key: value["evaluated_count"] for key, value in result["models"].items()},
            "dataset_root": str(root),
        }, ensure_ascii=False))
    except Exception as exc:
        parser.exit(1, f"ML DB3 integration stopped: {type(exc).__name__}\n")


if __name__ == "__main__":
    main()
