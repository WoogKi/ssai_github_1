"""DB-free checks for ML storage, Calendar provenance and date diagnostics."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.ml.dataset_contract import write_probe_dataset
from app.ml.db3_probe import run_db3_probe
from app.ml.integration import add_calendar_dataset, migrate_probe_dataset, ml_storage_root, official_calendar_snapshot
from app.services.ssai_business_calendar_repository import CalendarAuthorityRead
from tools.check_ml_phase2_db3 import fake_query
from tools.diagnose_ml_db3_dates import summarize
from tools.run_ml_phase1_offline import run_fixture


class FakeConnection:
    timeout = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def cursor(self):
        return self

    def execute(self, sql, *_params):
        assert sql.lstrip().upper().startswith("SELECT")
        return self

    def fetchall(self):
        return [(2026, "fixture-v1", "abcdef123456", datetime(2026, 9, 9, 14, 0))]


class IntegrationGate(unittest.TestCase):
    def test_environment_storage_root_and_repo_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("app.ml.integration.PROJECT_ROOT", Path(tmp) / "other_repo"), patch(
                "app.ml.integration.read_project_env_file", return_value={"SSAI_STORAGE_ROOT": tmp}
            ):
                self.assertEqual(ml_storage_root(), Path(tmp).resolve() / "ml")
            with patch("app.ml.integration.read_project_env_file", return_value={"SSAI_STORAGE_ROOT": str(Path.cwd())}):
                with self.assertRaises(ValueError):
                    ml_storage_root()

    def test_calendar_uses_existing_authority_and_load_metadata(self):
        authority = CalendarAuthorityRead(status="ready", holiday_dates=frozenset({"20260815"}))
        result = official_calendar_snapshot(
            start_date=date(2026, 4, 1), end_date=date(2026, 8, 30),
            authority_loader=lambda **_kwargs: authority, connection_factory=FakeConnection,
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["known_as_of"], "2026-09-09")
        self.assertEqual(result["holiday_dates"], ["20260815"])
        absent = official_calendar_snapshot(
            start_date=date(2026, 4, 1), end_date=date(2026, 8, 30),
            authority_loader=lambda **_kwargs: CalendarAuthorityRead(status="unavailable", reason_code="missing"),
            connection_factory=lambda: self.fail("metadata DB must not be called"),
        )
        self.assertEqual(absent["status"], "unavailable")

    def test_migrate_preserves_source_and_strict_asof_withheld(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_db3_probe(fake_query)
            source = Path(tmp) / "legacy"
            write_probe_dataset(
                source, fixture=result["fixture"], daily_r120=result["daily_r120"],
                daily_r130=result["daily_r130"], approval_reference="fixture",
                query_durations_ms=result["query_durations_ms"], probe_quality=result["quality"],
            )
            root = Path(tmp) / "storage" / "ml"
            migrated = migrate_probe_dataset(source, root)
            self.assertEqual((migrated / "fixture.json").read_bytes(), (source / "fixture.json").read_bytes())
            self.assertTrue(all((root / name).is_dir() for name in ("datasets", "models", "runs", "logs", "diagnostics")))
            with self.assertRaises(FileExistsError):
                migrate_probe_dataset(source, root)
            calendar = {"status": "ready", "start_date": "2026-04-01", "end_date": "2026-08-30",
                        "holiday_dates": ["20260815"], "known_as_of": "2026-09-09", "version": "fixture-v1"}
            selected = add_calendar_dataset(migrated, root, calendar)
            manifest = json.loads((selected / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["calendar_loaded_asof"], "2026-09-09")
            self.assertEqual(manifest["dataset_status"], "REVIEW_REQUIRED")
            report = run_fixture(selected / "fixture.json", root / "runs", run_id=selected.name)
            self.assertEqual(report["models"]["moving_average"]["evaluated_count"], 0)
            predictions = json.loads((root / "runs" / selected.name / "predictions.json").read_text(encoding="utf-8"))
            self.assertTrue(all(item["predicted_quantity"] is None for item in predictions))
            self.assertEqual((source / "fixture.json").read_bytes(), (migrated / "fixture.json").read_bytes())

    def test_negative_match_is_only_candidate_and_missing_date_unverified(self):
        result = summarize(
            [{"out_date": "20260920", "io_family": "5", "row_count": 2,
              "quantity": -5, "negative_rows": 1, "first_add_time": None,
              "last_add_time": None, "last_mod_time": None}],
            [{"out_date": "20260920", "header_count": 1}],
            [{"candidate_count": 1, "negative_date": "20260920", "first_candidate_date": "20260919",
              "last_candidate_date": "20260919", "negative_trans_di": "3", "negative_trans_date": "20260920",
              "negative_trans_seq": "1", "candidate_trans_di": "3", "candidate_trans_date": "20260920",
              "candidate_trans_seq": "1"}],
        )
        self.assertEqual(result["dates"]["20260920"]["r120_5xx_quantity"], "-5")
        self.assertEqual(result["dates"]["20260924"]["r120_all_rows"], 0)
        self.assertEqual(result["dates"]["20260924"]["completeness"], "unverified")
        self.assertEqual(result["negative_5xx_sample"]["single_candidate_not_confirmed"], 1)
        self.assertEqual(result["negative_5xx_sample"]["confirmed_pairs"], 0)


if __name__ == "__main__":
    unittest.main()
