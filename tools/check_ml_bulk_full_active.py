"""DB-free gate for the full active R040 extraction scope and bounded shards."""

from __future__ import annotations

import hashlib
import io
import json
import signal
import sys
import tempfile
import time
import tracemalloc
import unittest
from datetime import date
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.ml.bulk_dataset import _month_batches, _role_totals, build_bulk_dataset, verify_bulk_dataset
from tools.extract_ml_bulk_db3 import (ProgressReporter, _product_drift, _source_identity_sha256,
                                       main as extract_main, run as extract_run)


IDENTITY = "a" * 64


def master(count: int) -> list[dict[str, str | int]]:
    return [{"company_id": 3, "Rd04_Physic_Cd": f"{number:06d}",
             "Rd04_Use_Gu": "0", "Rd04_Add_Date": "20180101"}
            for number in range(count)]


class EmptySource:
    def __init__(self) -> None:
        self.totals_calls = 0

    def totals(self, _batch: object) -> dict[str, object]:
        self.totals_calls += 1
        return {"row_count": 0, "quantity": Decimal(0), "bonus_quantity": Decimal(0)}

    def outbound_rows(self, _batch: object) -> object:
        raise AssertionError("zero-row batch must not query R120 detail")

    def document_rows(self, _batch: object) -> object:
        raise AssertionError("zero-row batch must not query R130")


def build(root: Path, source: EmptySource, products: list[dict[str, str | int]], **options: object) -> dict:
    return build_bulk_dataset(
        root, dataset_version="db3-bulk-active-fixture", source=source, master_rows=products,
        saved_io_codes=("501", "601"), profile_fingerprint="fixture-profile",
        calendar_version="fixture-calendar", date_from=date(2018, 1, 1),
        date_to=date(2026, 8, 30), group_size=300, active_only=True, **options,
        source_identity_sha256=IDENTITY,
    )


class FullActiveGate(unittest.TestCase):
    def test_full_25500_plan_checksum_zero_rows_and_resume(self) -> None:
        products = master(25_500)
        tracemalloc.start()
        batches = _month_batches(tuple(row["Rd04_Physic_Cd"] for row in products),
                                 date(2018, 1, 1), date(2026, 8, 30), 300)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertEqual(len(batches), 8_840)
        self.assertEqual((batches[0].batch_id, batches[-1].batch_id),
                         ("201801-g0000", "202608-g0084"))
        self.assertLess(peak, 80 * 1024 * 1024)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = EmptySource()
            first = build(root, source, products, max_batches=1)
            target = root / "datasets" / "db3-bulk-active-fixture"
            self.assertEqual((first["planned_batches"], first["verified_batches"], source.totals_calls),
                             (8_840, 1, 1))
            self.assertEqual(first["dataset_status"], "BUILDING")
            self.assertEqual(first["product_master_sha256"], hashlib.sha256(
                (target / "products.jsonl").read_bytes()).hexdigest())
            self.assertEqual(json.loads((target / "plan.json").read_text())
                             ["product_selection"], "r040_use_gu_0")
            self.assertEqual(json.loads((target / "plan.json").read_text())
                             ["product_master_sha256"], first["product_master_sha256"])
            batch_dir = target / "batches" / "201801-g0000"
            self.assertEqual(json.loads((batch_dir / "manifest.json").read_text())
                             ["source_query_count"], 1)
            self.assertEqual((batch_dir / "r120.jsonl").stat().st_size, 0)
            self.assertEqual((batch_dir / "r130.jsonl").stat().st_size, 0)
            self.assertEqual(verify_bulk_dataset(target, require_complete=False)["verified_batches"], 1)
            second = build(root, source, products, resume=True, max_batches=1)
            self.assertEqual((second["reused_batches"], second["new_batches"], source.totals_calls),
                             (1, 1, 2))
            changed = list(products)
            changed[0] = {**changed[0], "Rd04_Use_Gu": "1"}
            with self.assertRaisesRegex(ValueError, "active-only"):
                build(root, source, changed, resume=True, max_batches=1)
            with self.assertRaisesRegex(ValueError, "product master checksum"):
                (target / "products.sha256").write_text("0" * 64 + "\n", encoding="ascii")
                build(root, source, products, resume=True, max_batches=1)

    def test_frozen_master_plan_sha_detects_matching_sidecar_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, EmptySource(), master(2), max_batches=1)
            target = root / "datasets" / "db3-bulk-active-fixture"
            products_file = target / "products.jsonl"
            products_file.write_bytes(products_file.read_bytes().replace(b"20180101", b"20190101"))
            (target / "products.sha256").write_text(
                hashlib.sha256(products_file.read_bytes()).hexdigest() + "\n", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "frozen extraction plan"):
                verify_bulk_dataset(target, require_complete=False)

    def test_r040_date_type_normalization_does_not_report_false_drift(self) -> None:
        frozen = ({"company_id": 3, "Rd04_Physic_Cd": "000001",
                   "Rd04_Use_Gu": "0", "Rd04_Add_Date": "2018-01-01"},)
        current = [{**frozen[0], "Rd04_Add_Date": date(2018, 1, 1)}]
        self.assertEqual(_product_drift(frozen, current)["master_changed"], 0)

    def test_group_boundary_and_legacy_contract(self) -> None:
        products = master(301)
        batches = _month_batches(tuple(row["Rd04_Physic_Cd"] for row in products),
                                 date(2026, 8, 1), date(2026, 8, 30), 300)
        self.assertEqual([len(batch.product_codes) for batch in batches], [300, 1])
        with tempfile.TemporaryDirectory() as tmp:
            active = build_bulk_dataset(
                Path(tmp), dataset_version="db3-bulk-active-complete", source=EmptySource(),
                master_rows=products, saved_io_codes=("501", "601"),
                profile_fingerprint="fixture", calendar_version="fixture",
                date_from=date(2026, 8, 1), date_to=date(2026, 8, 30),
                group_size=300, active_only=True,
                source_identity_sha256=IDENTITY,
            )
            self.assertEqual((active["planned_batches"], active["verified_batches"]), (2, 2))
            self.assertEqual(verify_bulk_dataset(
                Path(tmp) / "datasets" / "db3-bulk-active-complete")["active_products"], 301)
            legacy = build_bulk_dataset(
                Path(tmp), dataset_version="db3-bulk-legacy-fixture", source=EmptySource(),
                master_rows=products, saved_io_codes=("501", "601"),
                profile_fingerprint="fixture", calendar_version="fixture",
                date_from=date(2026, 8, 1), date_to=date(2026, 8, 30),
                group_size=100, max_batches=1,
            )
            plan = json.loads((Path(tmp) / "datasets" / "db3-bulk-legacy-fixture" / "plan.json").read_text())
            self.assertNotIn("product_selection", plan)
            self.assertEqual(legacy["planned_batches"], 4)

    def test_row_cap_stops_and_failure_resumes_only_that_batch(self) -> None:
        class OverflowSource(EmptySource):
            def totals(self, _batch: object) -> dict[str, object]:
                self.totals_calls += 1
                return {"row_count": 10_001, "quantity": Decimal(10_001), "bonus_quantity": Decimal(0)}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = OverflowSource()
            with self.assertRaisesRegex(ValueError, "row cap"):
                build_bulk_dataset(
                    root, dataset_version="db3-bulk-active-fixture", source=source,
                    master_rows=master(301), saved_io_codes=("501", "601"),
                    profile_fingerprint="fixture", calendar_version="fixture",
                    date_from=date(2026, 8, 1), date_to=date(2026, 8, 30),
                    group_size=300, active_only=True,
                    source_identity_sha256=IDENTITY,
                )
            target = root / "datasets" / "db3-bulk-active-fixture"
            self.assertTrue((target / "batches" / "202608-g0000" / "error.json").exists())
            self.assertFalse((target / "manifest.json").exists())
            with self.assertRaisesRegex(ValueError, "resume plan differs"):
                build_bulk_dataset(
                    root, dataset_version="db3-bulk-active-fixture", source=source,
                    master_rows=master(301), saved_io_codes=("501", "601"),
                    profile_fingerprint="fixture", calendar_version="fixture",
                    date_from=date(2026, 8, 1), date_to=date(2026, 8, 30),
                    group_size=100, active_only=True, resume=True,
                    source_identity_sha256=IDENTITY,
                )

    def test_stop_after_batch_and_resume_without_reextracting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = EmptySource()
            completed = 0
            def progress(event: str, _details: dict) -> None:
                nonlocal completed
                if event == "batch_complete":
                    completed += 1
            options = dict(dataset_version="db3-bulk-stop-fixture", source=source,
                           master_rows=master(601), saved_io_codes=("501", "601"),
                           profile_fingerprint="fixture", calendar_version="fixture",
                           date_from=date(2026, 8, 1), date_to=date(2026, 8, 30),
                           group_size=300, active_only=True, source_identity_sha256=IDENTITY)
            first = build_bulk_dataset(root, **options, progress=progress,
                                       stop_requested=lambda: completed >= 1)
            self.assertEqual((first["verified_batches"], first["dataset_status"], source.totals_calls),
                             (1, "BUILDING", 1))
            second = build_bulk_dataset(root, **options, resume=True)
            self.assertEqual((second["reused_batches"], second["new_batches"], source.totals_calls),
                             (1, 2, 3))
            self.assertEqual(verify_bulk_dataset(root / "datasets" / "db3-bulk-stop-fixture")
                             ["verified_batches"], 3)

    def test_source_identity_and_scope_change_stop_before_batch_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = EmptySource()
            options = dict(dataset_version="db3-bulk-identity-fixture", source=source,
                           master_rows=master(2), saved_io_codes=("501", "601"),
                           profile_fingerprint="fixture", calendar_version="fixture",
                           date_from=date(2026, 8, 1), date_to=date(2026, 8, 30),
                           group_size=300, active_only=True)
            build_bulk_dataset(root, **options, source_identity_sha256=IDENTITY)
            with self.assertRaisesRegex(ValueError, "resume plan differs"):
                build_bulk_dataset(root, **options, source_identity_sha256="b" * 64, resume=True)
            with self.assertRaisesRegex(ValueError, "resume plan differs"):
                build_bulk_dataset(root, **{**options, "date_from": date(2026, 7, 1)},
                                   source_identity_sha256=IDENTITY, resume=True)
            self.assertEqual(source.totals_calls, 1)

    def test_live_r040_change_resumes_frozen_cohort(self) -> None:
        products = master(301)
        changed = [{**products[0], "Rd04_Use_Gu": "1"}, *products[1:],
                   {**products[0], "Rd04_Physic_Cd": "NEW", "Rd04_Use_Gu": "0"}]
        class Connection:
            timeout = 0
            def __enter__(self) -> Connection:
                return self
            def __exit__(self, *_: object) -> None:
                pass
        source_calls = 0
        def totals(_source: object, _batch: object) -> dict[str, object]:
            nonlocal source_calls
            source_calls += 1
            return {"row_count": 0, "quantity": Decimal(0), "bonus_quantity": Decimal(0)}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = {"io_gu_list": ["501", "601"]}
            config = SimpleNamespace(company_id=3, db_server="fixture", db_port=1433, db_name="DB3")
            with (patch("tools.extract_ml_bulk_db3.ml_storage_root", return_value=root),
                  patch("tools.extract_ml_bulk_db3.load_dashboard_profile_checked",
                        return_value=SimpleNamespace(status="ready", profile=profile)),
                  patch("tools.extract_ml_bulk_db3.get_company_db_config", return_value=config),
                  patch("tools.extract_ml_bulk_db3.build_company_conn_str", return_value="fixture"),
                  patch("tools.extract_ml_bulk_db3.pyodbc.connect", return_value=Connection()),
                  patch("tools.extract_ml_bulk_db3._source_identity_sha256", return_value=IDENTITY),
                  patch("tools.extract_ml_bulk_db3.Db3Source.product_rows", side_effect=[iter(products), iter(changed)]),
                  patch("tools.extract_ml_bulk_db3.Db3Source.totals", new=totals)):
                options = dict(dataset_version="db3-bulk-r040-drift", date_from=date(2026, 8, 1),
                               date_to=date(2026, 8, 30), group_size=300, product_limit=None,
                               include_dataset_version=None, active_only=True)
                first = extract_run(**options, resume=False, max_batches=1)
                events: list[tuple[str, dict]] = []
                second = extract_run(**options, resume=True, max_batches=None,
                                     progress=lambda event, details: events.append((event, details)))
            self.assertEqual((first["verified_batches"], second["verified_batches"], source_calls), (1, 2, 2))
            self.assertEqual((second["reused_batches"], second["new_batches"], second["master_products"]),
                             (1, 1, 301))
            drift = next(details for event, details in events if event == "product_drift")
            self.assertEqual((drift["new_active"], drift["no_longer_active"]), (1, 1))
            frozen = (root / "datasets" / "db3-bulk-r040-drift" / "products.jsonl").read_text()
            self.assertNotIn("NEW", frozen)

    def test_source_identity_read_only_guid_and_permission_failure(self) -> None:
        cfg = SimpleNamespace(company_id=3, db_server="test-server", db_port=1433, db_name="DB3")
        class Source:
            def __init__(self, guid: str | None) -> None:
                self.guid = guid
            def rows(self, sql: str, params: tuple) -> object:
                self.sql = sql
                self.params = params
                return iter([{"database_name": "DB3", "server_name": "test-server",
                              "database_guid": self.guid, "recovery_fork_guid": "fork-a"}])
        source = Source("guid-a")
        first = _source_identity_sha256(source, cfg)
        self.assertEqual(source.params, ())
        self.assertTrue(source.sql.startswith("SELECT"))
        self.assertNotEqual(first, _source_identity_sha256(Source("guid-b"), cfg))
        forked = Source("guid-a")
        original_rows = forked.rows
        def changed_fork(sql: str, params: tuple) -> object:
            row = next(original_rows(sql, params))
            return iter([{**row, "recovery_fork_guid": "fork-b"}])
        forked.rows = changed_fork
        self.assertNotEqual(first, _source_identity_sha256(forked, cfg))
        with self.assertRaisesRegex(ValueError, "identity is unavailable"):
            _source_identity_sha256(Source(None), cfg)

    def test_concise_progress_heartbeat_and_immediate_failure(self) -> None:
        stream = io.StringIO()
        reporter = ProgressReporter(interval=0.02, stream=stream, concise=True)
        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            with reporter:
                reporter.update("plan", {"planned_batches": 3, "existing_candidates": 1,
                                         "product_master_sha256": IDENTITY})
                reporter.update("batch_reused", {"batch_id": "old", "rows": 7})
                reporter.update("batch_start", {"batch_id": "new"})
                reporter.update("rows", {"count": 10001})
                reporter.update("batch_complete", {"rows": 10001, "elapsed_ms": 50})
                time.sleep(0.05)
                raise RuntimeError("fixture failure")
        output = stream.getvalue()
        self.assertIn("PROGRESS_10M", output)
        self.assertIn("eta_s=", output)
        self.assertIn("FAILED", output)
        self.assertNotIn("BATCH_START", output)
        self.assertNotIn("BATCH_DONE", output)
        self.assertNotIn("FETCH_10000", output)

    def test_role_totals_preserve_negative_return_and_other(self) -> None:
        rows = ({"Rd12_Io_Gu": code, "Rd12_Quantity": quantity} for code, quantity in
                (("501", "-5"), ("501", "10"), ("601", "-2"), ("599", "3")))
        self.assertEqual(_role_totals(rows, ("501", "601")), {
            "normal_rows": 2, "normal_quantity": "5", "negative_normal_rows": 1,
            "return_rows": 1, "return_quantity": "-2", "other_rows": 1, "other_quantity": "3",
        })

    def test_extractor_selects_only_active_without_erp_connection(self) -> None:
        source_rows = [*master(2), {**master(1)[0], "Rd04_Physic_Cd": "ZSTOP", "Rd04_Use_Gu": "1"}]
        class Connection:
            timeout = 0
            def __enter__(self) -> Connection:
                return self
            def __exit__(self, *_: object) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmp:
            with (patch("tools.extract_ml_bulk_db3.ml_storage_root", return_value=Path(tmp)),
                  patch("tools.extract_ml_bulk_db3.load_dashboard_profile_checked",
                        return_value=SimpleNamespace(status="ready", profile={"io_gu_list": ["501", "601"]})),
                  patch("tools.extract_ml_bulk_db3.get_company_db_config",
                        return_value=SimpleNamespace(company_id=3)),
                  patch("tools.extract_ml_bulk_db3.build_company_conn_str", return_value="fixture"),
                  patch("tools.extract_ml_bulk_db3.pyodbc.connect", return_value=Connection()) as connect,
                  patch("tools.extract_ml_bulk_db3._source_identity_sha256", return_value=IDENTITY),
                  patch("tools.extract_ml_bulk_db3.Db3Source.product_rows", return_value=iter(source_rows)),
                  patch("tools.extract_ml_bulk_db3.build_bulk_dataset",
                        return_value={"dataset_status": "BUILDING"}) as build_mock):
                extract_run(dataset_version="db3-bulk-active-selection", date_from=date(2018, 1, 1),
                            date_to=date(2026, 8, 30), group_size=300, product_limit=None,
                            include_dataset_version=None, resume=False, max_batches=1,
                            active_only=True)
        self.assertEqual(connect.call_count, 1)
        self.assertEqual([row["Rd04_Physic_Cd"] for row in build_mock.call_args.kwargs["master_rows"]],
                         ["000000", "000001"])
        self.assertTrue(build_mock.call_args.kwargs["active_only"])

    def test_active_cli_defaults_to_300_without_touching_db(self) -> None:
        with (patch.object(sys, "argv", ["extract_ml_bulk_db3.py", "--live-read-only",
                                        "--company-id", "3", "--active-only"]),
              patch("tools.extract_ml_bulk_db3.run", return_value={"dataset_status": "BUILDING"}) as mock_run,
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            extract_main()
        self.assertEqual(mock_run.call_args.kwargs["group_size"], 300)
        self.assertTrue(mock_run.call_args.kwargs["active_only"])

    def test_active_cli_first_interrupt_requests_batch_boundary_stop(self) -> None:
        before = signal.getsignal(signal.SIGINT)
        def simulated_run(**kwargs: object) -> dict[str, str]:
            signal.raise_signal(signal.SIGINT)
            self.assertTrue(kwargs["stop_requested"]())
            return {"dataset_status": "BUILDING"}
        stderr = io.StringIO()
        with (patch.object(sys, "argv", ["extract_ml_bulk_db3.py", "--live-read-only",
                                      "--company-id", "3", "--active-only"]),
              patch("tools.extract_ml_bulk_db3.run", side_effect=simulated_run),
              redirect_stdout(io.StringIO()), redirect_stderr(stderr)):
            extract_main()
        self.assertIs(signal.getsignal(signal.SIGINT), before)
        self.assertIn("STOP_REQUESTED", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
