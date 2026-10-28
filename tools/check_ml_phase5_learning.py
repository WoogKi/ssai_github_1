"""DB-free, synthetic contract gate for the isolated learning worker."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from joblib import load

from app.ml.learning import TrainingCancelled, train_research
from tools.check_ml_phase1_foundation import outbound
from tools.monitor_ml_run import read_status


def dataset(root: Path, *, as_of_dates: list[str] | None = None) -> Path:
    source = root / "datasets" / "synthetic"
    source.mkdir(parents=True)
    start, end = date(2026, 4, 1), date(2026, 8, 30)
    rows = []
    for product, amount in (("0001", ("2", "5", "8")), ("A2", ("1", "2", "1"))):
        for index, day in enumerate((date(2026, 6, 15), date(2026, 7, 15), date(2026, 8, 15))):
            stamp = day.strftime("%Y%m%d") + "120000"
            rows.append(outbound(day=day, seq=f"{product}-{index}", product=product,
                                 add=stamp, mod=stamp, qty=amount[index]))
    payload = {
        "schema_version": "ml_phase1_fixture_v1", "company_id": 3, "dataset_version": "synthetic",
        "date_from": "2026-04-01", "date_to": "2026-08-30",
        "as_of_dates": as_of_dates or ["2026-06-30", "2026-07-31"],
        "complete_dates": [(start + timedelta(days=index)).isoformat() for index in range((end - start).days + 1)],
        "calendar_asof_required": False, "timestamp_policy_confirmed": True,
        "calendar": {"start_date": "2026-04-01", "end_date": "2026-08-30",
                     "holiday_dates": [], "known_as_of": "2026-09-09", "version": "synthetic"},
        "saved_io_codes": ["501", "601"], "outbound_rows": rows, "document_rows": [],
        "vendor_rows": [], "standard_rows": [], "code_rows": [],
        "product_rows": [{"company_id": 3, "Rd04_Physic_Cd": code, "Rd04_Add_Date": "20260101"}
                         for code in ("0001", "A2")],
    }
    content = (json.dumps(payload, sort_keys=True) + "\n").encode()
    (source / "fixture.json").write_bytes(content)
    (source / "manifest.json").write_text(json.dumps({
        "company_id": 3, "dataset_version": "synthetic", "dataset_status": "RESEARCH_POSTHOC",
        "evaluation_grade": "research_posthoc",
        "sha256_by_file": {"fixture.json": hashlib.sha256(content).hexdigest()},
    }), encoding="utf-8")
    return source / "fixture.json"


class LearningGate(unittest.TestCase):
    def test_training_holdout_baselines_reload_and_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = dataset(root)
            result = train_research(source, root, run_id="first", estimators=20)
            self.assertEqual((result["train_samples"], result["evaluation_samples"]), (2, 2))
            self.assertEqual(result["evaluation_grade"], "research_posthoc")
            self.assertEqual(result["train_dates"], ["2026-06-30"])
            self.assertEqual(result["evaluation_date"], "2026-07-31")
            self.assertEqual(set(result["metrics"]), {"random_forest", "moving_average", "ewma"})
            self.assertEqual(result["metrics"]["random_forest"]["evaluated_count"], 2)
            events = [json.loads(line) for line in (root / "runs" / "first" / "events.jsonl").read_text().splitlines()]
            self.assertTrue(any(row["stage"] == "train_random_forest" and row["event"] == "PROGRESS"
                                and row["completed_count"] == 10 and row["total_count"] == 20
                                for row in events))
            self.assertTrue(any(row["stage"] == "prepare" and row["completed_count"] == 2
                                for row in events))
            with patch("tools.monitor_ml_run.ml_storage_root", return_value=root):
                self.assertEqual(read_status("first")["stage"], "complete")
            artifact = load(root / "models" / "first" / "random_forest.joblib")
            self.assertIn("standard_status=unknown", artifact["vectorizer"].feature_names_)
            second = train_research(source, root, run_id="second", estimators=20)
            self.assertEqual(result["model_sha256"], second["model_sha256"])
            self.assertEqual((root / "runs" / "first" / "predictions.json").read_bytes(),
                             (root / "runs" / "second" / "predictions.json").read_bytes())

    def test_overlapping_labels_and_checksum_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = dataset(root, as_of_dates=["2026-06-30", "2026-07-15"])
            with self.assertRaisesRegex(ValueError, "non-overlapping"):
                train_research(source, root, run_id="overlap", estimators=10)
            source.write_text(source.read_text(encoding="utf-8") + " ", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "checksum"):
                train_research(source, root, run_id="tampered", estimators=10)

    def test_cancel_records_actual_count_and_does_not_save_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = dataset(root)
            calls = 0

            def stop_after_first_tree_group():
                nonlocal calls
                calls += 1
                return calls >= 6

            with self.assertRaises(TrainingCancelled):
                train_research(source, root, run_id="cancelled", estimators=20,
                               cancel_check=stop_after_first_tree_group)
            record = json.loads((root / "runs" / "cancelled" / "status.json").read_text())
            self.assertEqual((record["event"], record["completed_count"], record["total_count"]),
                             ("CANCEL", 10, 20))
            self.assertFalse((root / "models" / "cancelled").exists())
            self.assertFalse((root / "runs" / "training.lock").exists())

    def test_failure_and_worker_lock_are_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = dataset(root)
            lock = root / "runs" / "training.lock"
            lock.parent.mkdir()
            lock.write_text("occupied")
            with self.assertRaises(FileExistsError):
                train_research(source, root, run_id="blocked", estimators=10)
            lock.unlink()
            payload = json.loads(source.read_text())
            payload["complete_dates"].append("2026-09-01")
            content = (json.dumps(payload, sort_keys=True) + "\n").encode()
            source.write_bytes(content)
            manifest = json.loads((source.parent / "manifest.json").read_text())
            manifest["sha256_by_file"]["fixture.json"] = hashlib.sha256(content).hexdigest()
            (source.parent / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "outside dataset"):
                train_research(source, root, run_id="error", estimators=10)
            record = json.loads((root / "runs" / "error" / "status.json").read_text())
            self.assertEqual(record["event"], "ERROR")


if __name__ == "__main__":
    unittest.main()
