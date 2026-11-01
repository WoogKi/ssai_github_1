"""DB-free gate for bounded, resumable DB3 bulk research datasets."""

from __future__ import annotations

import json
import hashlib
import io
import re
import tempfile
import time
import tracemalloc
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from decimal import Decimal
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from app.ml.bulk_dataset import (Batch, BatchLimitError, _split_batch, _write_rows,
                                 build_bulk_dataset, materialize_research_cohort, verify_bulk_dataset)
from tools.extract_ml_bulk_db3 import (Db3Source, ProgressReporter, _fixed_cohort, _select_fixed_products,
                                      main as extract_main, run as extract_run)
from tools.check_ml_phase1_foundation import outbound
from tools.run_ml_phase1_offline import run_fixture


PRODUCTS = ("0001", "0002", "A003", "Z9")
MASTER = tuple({"company_id": 3, "Rd04_Physic_Cd": code, "Rd04_Use_Gu": "0" if code != "Z9" else "1",
                "Rd04_Add_Date": "20200101"} for code in PRODUCTS)


class FakeSource:
    def __init__(self):
        self.rows = [outbound(day=date(2026, month, 10), product=code,
                              qty="-2" if code == "0001" and month == 5 else "5",
                              seq=f"{month}-{code}", add=f"2026{month:02d}10120000",
                              mod=f"2026{month:02d}10120000")
                     for month in (5, 6) for code in PRODUCTS[:3]]
        self.failed = None
        self.calls = []

    def selected(self, batch):
        return [row for row in self.rows if row["Rd12_Physic_Cd"] in batch.product_codes
                and batch.date_from <= row["Rd12_Out_YyMmDd"] < batch.date_to_exclusive]

    def totals(self, batch):
        self.calls.append(batch.batch_id)
        if batch.batch_id == self.failed:
            raise RuntimeError("synthetic source failure")
        rows = self.selected(batch)
        return {"row_count": len(rows),
                "quantity": sum((Decimal(row["Rd12_Quantity"]) for row in rows), Decimal(0)),
                "bonus_quantity": sum((Decimal(row["Rd12_Oquantity"]) for row in rows), Decimal(0))}

    def outbound_rows(self, batch):
        yield from self.selected(batch)

    def document_rows(self, batch):
        return iter(())


class DenseSource(FakeSource):
    def __init__(self, rows, documents=()):
        super().__init__()
        self.rows = rows
        self.documents = documents

    def document_rows(self, batch):
        keys = {(row["Rd12_Trans_Di"], row["Rd12_Trans_YyMmDd"], row["Rd12_Ven_Cd"],
                 row["Rd12_Trans_Seq"]) for row in self.selected(batch)}
        return (row for row in self.documents if (
            row["Rd13_Trans_Di"], row["Rd13_Trans_YyMmDd"], row["Rd13_Ven_Cd"],
            row["Rd13_Trans_Seq"]) in keys)


def dense_rows(counts):
    result = []
    for code, day, count in counts:
        for _ in range(count):
            result.append(outbound(day=day, product=code, seq=str(len(result)), qty="1"))
    return result


class OfflineDb3SqlSource(Db3Source):
    def __init__(self, rows, *, latency_s=0.0):
        self.data = rows
        self.latency_s = latency_s
        self.query_count = 0
        self.queries = []
        self.document = {"Rd13_Trans_Di": "3", "Rd13_Trans_YyMmDd": "20260510",
                         "Rd13_Ven_Cd": "V1", "Rd13_Trans_Seq": "10",
                         "Rd13_Supply_Price": "1", "Rd13_Tax_Price": "0", "header_count": 1}

    def rows(self, sql, params):
        self.query_count += 1
        self.queries.append((sql, params))
        if self.latency_s:
            time.sleep(self.latency_s)
        start, stop, *codes = params
        selected = [row for row in self.data if start <= row["Rd12_Out_YyMmDd"] < stop
                    and row["Rd12_Physic_Cd"] in codes]
        if "GROUP BY Rd12_Physic_Cd" in sql:
            for code in codes:
                matched = [row for row in selected if row["Rd12_Physic_Cd"] == code]
                if matched:
                    yield {"product_code": code, "row_count": len(matched),
                           "quantity": sum((Decimal(row["Rd12_Quantity"]) for row in matched), Decimal(0)),
                           "bonus_quantity": sum((Decimal(row["Rd12_Oquantity"]) for row in matched), Decimal(0))}
        elif "FROM dbo.Rddbc130 AS H" in sql:
            if selected:
                yield self.document
        elif "COUNT_BIG(*) AS row_count" in sql:
            yield {"row_count": len(selected),
                   "quantity": sum((Decimal(row["Rd12_Quantity"]) for row in selected), Decimal(0)),
                   "bonus_quantity": sum((Decimal(row["Rd12_Oquantity"]) for row in selected), Decimal(0))}
        else:
            limit = int(re.search(r"TOP \((\d+)\)", sql).group(1))
            yield from selected[:limit]


COALESCED_MASTER = tuple({"company_id": 3, "Rd04_Physic_Cd": f"{index:05d}",
                          "Rd04_Use_Gu": "0", "Rd04_Add_Date": "20200101"}
                         for index in range(300))


def run_coalesced(root, source, *, resume=False, max_batches=None, coalesce_groups=1):
    return build_bulk_dataset(
        root, dataset_version="db3-bulk-coalesced-fixture", source=source,
        master_rows=COALESCED_MASTER, saved_io_codes=("501", "601"),
        profile_fingerprint="fixture", calendar_version="fixture",
        date_from=date(2026, 5, 1), date_to=date(2026, 5, 31),
        group_size=100, resume=resume, max_batches=max_batches,
        active_only=True, source_identity_sha256="a" * 64,
        coalesce_groups=coalesce_groups,
    )


def run(root: Path, source: FakeSource, *, resume=False, max_batches=None,
        start=date(2026, 5, 1), end=date(2026, 6, 30), progress=None):
    return build_bulk_dataset(
        root, dataset_version="db3-bulk-fixture", source=source, master_rows=MASTER,
        saved_io_codes=("501", "601"), profile_fingerprint="fixture", calendar_version="fixture",
        date_from=start, date_to=end, group_size=2,
        resume=resume, max_batches=max_batches, progress=progress,
    )


class BulkDatasetGate(unittest.TestCase):
    def test_coalesced_300_exact_query_count_and_memory(self):
        rows = dense_rows([(f"{index:05d}", date(2026, 5, 10), 1) for index in range(300)])
        with tempfile.TemporaryDirectory() as old, tempfile.TemporaryDirectory() as new:
            baseline = OfflineDb3SqlSource(rows, latency_s=0.002)
            tracemalloc.start()
            started = time.perf_counter()
            old_result = run_coalesced(Path(old), baseline)
            old_elapsed = time.perf_counter() - started
            grouped = OfflineDb3SqlSource(rows, latency_s=0.002)
            started = time.perf_counter()
            new_result = run_coalesced(Path(new), grouped, coalesce_groups=3)
            new_elapsed = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertEqual((baseline.query_count, grouped.query_count), (9, 3))
            self.assertEqual((old_result["source_rows"], new_result["source_rows"]), (300, 300))
            self.assertLess(peak, 128 * 1024 * 1024)
            self.assertEqual(
                (Path(old) / "datasets" / "db3-bulk-coalesced-fixture" / "plan.json").read_bytes(),
                (Path(new) / "datasets" / "db3-bulk-coalesced-fixture" / "plan.json").read_bytes(),
            )
            for group in range(3):
                batch_id = f"202605-g{group:04d}"
                old_folder = Path(old) / "datasets" / "db3-bulk-coalesced-fixture" / "batches" / batch_id
                new_folder = Path(new) / "datasets" / "db3-bulk-coalesced-fixture" / "batches" / batch_id
                for name in ("r120.jsonl", "r130.jsonl"):
                    self.assertEqual(hashlib.sha256((old_folder / name).read_bytes()).hexdigest(),
                                     hashlib.sha256((new_folder / name).read_bytes()).hexdigest())
                    self.assertLessEqual((new_folder / name).stat().st_size, 32 * 1024 * 1024)
                for name in ("r120_rows", "r120_quantity", "r120_bonus", "r130_headers",
                             "role_totals", "document_match_counts"):
                    self.assertEqual(json.loads((old_folder / "manifest.json").read_text())[name],
                                     json.loads((new_folder / "manifest.json").read_text())[name])
            print(json.dumps({"coalesced_db_free": "normal", "old_selects": baseline.query_count,
                              "new_selects": grouped.query_count, "old_wall_ms": round(old_elapsed * 1000),
                              "new_wall_ms": round(new_elapsed * 1000), "peak_tracemalloc_bytes": peak}))

    def test_coalesced_round_trip_latency_model(self):
        rows = dense_rows([(f"{index:05d}", date(2026, 5, 10), 1) for index in range(300)])
        with tempfile.TemporaryDirectory() as old, tempfile.TemporaryDirectory() as new:
            baseline = OfflineDb3SqlSource(rows, latency_s=0.025)
            started = time.perf_counter()
            run_coalesced(Path(old), baseline)
            old_elapsed = time.perf_counter() - started
            grouped = OfflineDb3SqlSource(rows, latency_s=0.025)
            started = time.perf_counter()
            run_coalesced(Path(new), grouped, coalesce_groups=3)
            new_elapsed = time.perf_counter() - started
            self.assertEqual((baseline.query_count, grouped.query_count), (9, 3))
            self.assertLess(new_elapsed, old_elapsed)
            print(json.dumps({"coalesced_db_free": "simulated_25ms_per_select",
                              "old_wall_ms": round(old_elapsed * 1000),
                              "new_wall_ms": round(new_elapsed * 1000)}))

    def test_coalesced_resume_skips_verified_without_source_select(self):
        rows = dense_rows([(f"{index:05d}", date(2026, 5, 10), 1) for index in range(300)])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_coalesced(root, OfflineDb3SqlSource(rows), max_batches=1)
            first = root / "datasets" / "db3-bulk-coalesced-fixture" / "batches" / "202605-g0000"
            original = {item.name: hashlib.sha256(item.read_bytes()).hexdigest()
                        for item in first.iterdir() if item.is_file()}
            source = OfflineDb3SqlSource(rows)
            result = run_coalesced(root, source, resume=True, coalesce_groups=3)
            self.assertEqual((result["reused_batches"], result["new_batches"]), (1, 2))
            self.assertEqual(source.query_count, 3)
            self.assertEqual(original, {item.name: hashlib.sha256(item.read_bytes()).hexdigest()
                                        for item in first.iterdir() if item.is_file()})
            self.assertTrue(all("00000" not in params[2:] for _, params in source.queries))

    def test_coalesced_overflow_uses_existing_split_and_keeps_100_plan(self):
        rows = dense_rows([("00000", date(2026, 5, 10), 1),
                           ("00100", date(2026, 5, 10), 6000),
                           ("00101", date(2026, 5, 10), 4001),
                           ("00200", date(2026, 5, 10), 1)])
        with tempfile.TemporaryDirectory() as old, tempfile.TemporaryDirectory() as new:
            baseline = OfflineDb3SqlSource(rows)
            tracemalloc.start()
            started = time.perf_counter()
            old_result = run_coalesced(Path(old), baseline)
            old_elapsed = time.perf_counter() - started
            grouped = OfflineDb3SqlSource(rows)
            started = time.perf_counter()
            new_result = run_coalesced(Path(new), grouped, coalesce_groups=3)
            new_elapsed = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertEqual((old_result["source_rows"], new_result["source_rows"]), (10003, 10003))
            self.assertEqual((baseline.query_count, grouped.query_count), (23, 7))
            self.assertLess(peak, 256 * 1024 * 1024)
            target = Path(new) / "datasets" / "db3-bulk-coalesced-fixture"
            plan = json.loads((target / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual((plan["group_size"], new_result["planned_batches"]), (100, 3))
            parent = json.loads((target / "batches" / "202605-g0001" / "manifest.json").read_text())
            self.assertEqual((parent["split_axis"], parent["r120_rows"]), ("product", 10001))
            self.assertTrue(all(path.stat().st_size <= 32 * 1024 * 1024
                                for path in (target / "batches").rglob("*.jsonl")))
            self.assertEqual(verify_bulk_dataset(target)["source_rows"], 10003)
            print(json.dumps({"coalesced_db_free": "overflow", "old_selects": baseline.query_count,
                              "new_selects": grouped.query_count, "old_wall_ms": round(old_elapsed * 1000),
                              "new_wall_ms": round(new_elapsed * 1000), "peak_tracemalloc_bytes": peak}))

    def test_coalesced_product_and_r130_scope_mismatch_fail_closed(self):
        class ChangedProduct(OfflineDb3SqlSource):
            def rows(self, sql, params):
                for row in super().rows(sql, params):
                    if "SELECT TOP" in sql and "FROM dbo.Rddbc120" in sql and row["Rd12_Physic_Cd"] == "00000":
                        yield {**row, "Rd12_Physic_Cd": "00001"}
                    else:
                        yield row

        class WrongDocument(OfflineDb3SqlSource):
            def rows(self, sql, params):
                for row in super().rows(sql, params):
                    if "FROM dbo.Rddbc130 AS H" in sql:
                        yield {**row, "Rd13_Trans_Seq": "outside"}
                    else:
                        yield row

        rows = dense_rows([("00000", date(2026, 5, 10), 1),
                           ("00001", date(2026, 5, 10), 1),
                           ("00100", date(2026, 5, 10), 1)])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "product differs"):
                run_coalesced(Path(tmp), ChangedProduct(rows), coalesce_groups=3)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "outside R120 scope"):
                run_coalesced(root, WrongDocument(rows), coalesce_groups=3)
            target = root / "datasets" / "db3-bulk-coalesced-fixture"
            self.assertFalse((target / "batches").exists())
            resumed = run_coalesced(root, OfflineDb3SqlSource(rows), resume=True, coalesce_groups=3)
            self.assertEqual((resumed["new_batches"], resumed["source_rows"]), (3, 3))

    def test_r120_product_split_resume_and_research_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = DenseSource(dense_rows([
                ("0001", date(2026, 6, 10), 6001), ("0002", date(2026, 6, 10), 4000),
            ]))
            result = run(root, source, start=date(2026, 4, 1), end=date(2026, 8, 30))
            self.assertEqual((result["source_rows"], result["source_bonus"]), (10001, "10001"))
            target = root / "datasets" / "db3-bulk-fixture"
            parent = target / "batches" / "202606-g0000"
            meta = json.loads((parent / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["split_axis"], "product")
            self.assertEqual(meta["r120_rows"], 10001)
            self.assertEqual(meta["source_query_count"], 7)
            self.assertEqual(len(meta["child_ids"]), 2)
            old_hashes = {path.relative_to(target): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in target.rglob("*") if path.is_file() and "failed" not in path.parts}
            source.calls.clear()
            again = run(root, source, resume=True, start=date(2026, 4, 1), end=date(2026, 8, 30))
            self.assertEqual((again["new_batches"], again["reused_batches"]), (0, 10))
            self.assertEqual(source.calls, [])
            self.assertEqual(old_hashes, {
                path.relative_to(target): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in target.rglob("*") if path.is_file() and "failed" not in path.parts})
            calendar = {"status": "ready", "start_date": "2026-04-01", "end_date": "2026-08-30",
                        "holiday_dates": [], "known_as_of": "2026-09-09", "version": "fixture"}
            research = materialize_research_cohort(
                target, root, dataset_version="db3-research-split-fixture",
                product_codes=("0001",), as_of_dates=("2026-06-30", "2026-07-31"),
                calendar=calendar)
            fixture = json.loads((research / "fixture.json").read_text(encoding="utf-8"))
            self.assertEqual(len(fixture["outbound_rows"]), 6001)
            child_file = parent / "p0" / "r120.jsonl"
            with child_file.open("a", encoding="utf-8") as stream:
                stream.write("{}\n")
            with self.assertRaisesRegex(ValueError, "checksum"):
                verify_bulk_dataset(target)

    def test_r120_date_split_and_indivisible_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = dense_rows([
                ("0001", date(2026, 5, 10), 5000), ("0001", date(2026, 5, 20), 5001),
            ])
            result = build_bulk_dataset(
                root, dataset_version="db3-bulk-date-fixture", source=DenseSource(rows),
                master_rows=MASTER[:1], saved_io_codes=("501", "601"),
                profile_fingerprint="fixture", calendar_version="fixture",
                date_from=date(2026, 5, 1), date_to=date(2026, 5, 31), group_size=1,
            )
            self.assertEqual(result["source_rows"], 10001)
            target = root / "datasets" / "db3-bulk-date-fixture"
            parent = json.loads((target / "batches" / "202605-g0000" / "manifest.json").read_text())
            self.assertEqual(parent["split_axis"], "date")
            self.assertEqual(verify_bulk_dataset(target)["source_rows"], 10001)
            with self.assertRaisesRegex(BatchLimitError, "single-product single-day"):
                _split_batch(Batch("one-day", "20260510", "20260511", ("0001",)))

    def test_r130_file_cap_splits_and_preserves_header_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = dense_rows([
                ("0001", date(2026, 5, 10), 1), ("0002", date(2026, 5, 10), 1),
            ])
            docs = []
            for index, row in enumerate(rows):
                row["Rd12_Trans_Seq"] = f"T{index}"
                docs.append({"company_id": 3, "Rd13_Trans_Di": "3",
                             "Rd13_Trans_YyMmDd": row["Rd12_Trans_YyMmDd"],
                             "Rd13_Ven_Cd": row["Rd12_Ven_Cd"],
                             "Rd13_Trans_Seq": row["Rd12_Trans_Seq"],
                             "Rd13_Supply_Price": "1", "Rd13_Tax_Price": "0",
                             "header_count": 1, "padding": "x" * 1800})
            with patch("app.ml.bulk_dataset.MAX_BATCH_BYTES", 2500):
                result = run(root, DenseSource(rows, docs), start=date(2026, 5, 1), end=date(2026, 5, 31))
                self.assertEqual(result["source_rows"], 2)
                target = root / "datasets" / "db3-bulk-fixture"
                parent = json.loads((target / "batches" / "202605-g0000" / "manifest.json").read_text())
                self.assertEqual((parent["split_axis"], parent["r130_headers"]), ("product", 2))
                self.assertEqual(parent["source_query_count"], 9)
                self.assertEqual(verify_bulk_dataset(target)["source_rows"], 2)

    def test_split_child_failure_resume_preserves_prior_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = DenseSource(dense_rows([
                ("0001", date(2026, 6, 10), 6001), ("0002", date(2026, 6, 10), 4000),
            ]))
            source.failed = "202606-g0000/p1"
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                run(root, source)
            target = root / "datasets" / "db3-bulk-fixture"
            prior = target / "batches" / "202605-g0000" / "manifest.json"
            prior_sha = hashlib.sha256(prior.read_bytes()).hexdigest()
            self.assertTrue((target / "batches" / "202606-g0000" / "error.json").exists())
            source.failed = None
            source.calls.clear()
            result = run(root, source, resume=True)
            self.assertEqual(result["source_rows"], 10001)
            self.assertTrue((target / "failed" / "202606-g0000" / "error.json").exists())
            self.assertEqual(hashlib.sha256(prior.read_bytes()).hexdigest(), prior_sha)
            self.assertNotIn("202605-g0000", source.calls)
            source.calls.clear()
            again = run(root, source, resume=True)
            self.assertEqual(again["new_batches"], 0)
            self.assertEqual(source.calls, [])

    def test_split_parent_exact_and_cross_child_pk_fail_closed(self):
        class WrongParentSource(DenseSource):
            def totals(self, batch):
                result = super().totals(batch)
                if batch.batch_id == "202605-g0000":
                    result["row_count"] += 1
                return result

        rows = dense_rows([
            ("0001", date(2026, 5, 10), 6001), ("0002", date(2026, 5, 10), 4000),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "independent R120 source totals"):
                run(Path(tmp), WrongParentSource(rows), start=date(2026, 5, 1),
                    end=date(2026, 5, 31))
            parent = Path(tmp) / "datasets" / "db3-bulk-fixture" / "batches" / "202605-g0000"
            self.assertTrue((parent / "error.json").exists())
        rows[6001]["Rd12_Out_Seq"] = rows[0]["Rd12_Out_Seq"]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "duplicate R120 source PK across split"):
                run(Path(tmp), DenseSource(rows), start=date(2026, 5, 1),
                    end=date(2026, 5, 31))

    def test_r130_row_cap_remains_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(BatchLimitError, "row cap"):
                _write_rows(Path(tmp) / "r130.jsonl",
                            ({"Rd13_Trans_Di": "3"} for _ in range(10001)), company_id=3)

    def test_live_progress_and_resume_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_output = io.StringIO()
            with ProgressReporter(stream=first_output) as reporter:
                first = run(root, FakeSource(), max_batches=1, progress=reporter.update)
            self.assertEqual(first["verified_batches"], 1)
            self.assertIn("PLAN", first_output.getvalue())
            self.assertIn("BATCH_DONE", first_output.getvalue())
            self.assertIn("fetched_rows=2", first_output.getvalue())
            self.assertIn("verified_rows=2", first_output.getvalue())
            self.assertIn("PARTIAL", first_output.getvalue())
            resumed_output = io.StringIO()
            with ProgressReporter(stream=resumed_output) as reporter:
                resumed = run(root, FakeSource(), resume=True, progress=reporter.update)
            self.assertEqual(resumed["verified_batches"], 4)
            self.assertIn("existing_candidates=1", resumed_output.getvalue())
            self.assertIn("reused_verified=1", resumed_output.getvalue())
            self.assertIn("verified_rows=6", resumed_output.getvalue())
            self.assertIn("DONE", resumed_output.getvalue())

    def test_row_threshold_below_and_above_ten_thousand(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            with ProgressReporter(stream=output) as reporter:
                progress = lambda count: reporter.update("rows", {"count": count})
                _write_rows(Path(tmp) / "first.jsonl",
                            ({"Rd12_Quantity": "1"} for _ in range(6000)),
                            company_id=3, row_progress=progress)
                self.assertNotIn("FETCH_10000", output.getvalue())
                _write_rows(Path(tmp) / "second.jsonl",
                            ({"Rd12_Quantity": "1"} for _ in range(4500)),
                            company_id=3, row_progress=progress)
            self.assertEqual(output.getvalue().count("FETCH_10000"), 1)
            self.assertIn("boundary_rows=10000", output.getvalue())
            self.assertEqual(reporter.fetched, 10500)

    def test_heartbeat_during_source_wait_and_stdout_json(self):
        output = io.StringIO()
        class SlowSource(FakeSource):
            def totals(self, batch):
                Event().wait(0.07)
                return super().totals(batch)

        with tempfile.TemporaryDirectory() as tmp:
            with ProgressReporter(interval=0.02, stream=output) as reporter:
                run(Path(tmp), SlowSource(), max_batches=1, progress=reporter.update)
        heartbeats = [line for line in output.getvalue().splitlines() if "HEARTBEAT" in line]
        self.assertTrue(heartbeats)
        for line in heartbeats:
            self.assertRegex(line, r"^\[DB3\] HEARTBEAT RUNNING elapsed_s=\d+\.\d last_confirmed_rows=\d+$")
        self.assertTrue(any(line.endswith("last_confirmed_rows=0") for line in heartbeats))
        self.assertFalse(reporter.thread.is_alive())
        stdout, stderr = io.StringIO(), io.StringIO()
        with (patch("sys.argv", ["extract_ml_bulk_db3.py", "--live-read-only", "--company-id", "3"]),
              patch("tools.extract_ml_bulk_db3.run", return_value={"dataset_status": "BUILDING"}),
              redirect_stdout(stdout), redirect_stderr(stderr)):
            extract_main()
        self.assertEqual(json.loads(stdout.getvalue()), {"dataset_status": "BUILDING"})
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        self.assertIn("[DB3] RUNNING", stderr.getvalue())

    def test_progress_failure_stops_without_final_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            source = FakeSource()
            source.failed = "202605-g0000"
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                with ProgressReporter(stream=output) as reporter:
                    run(Path(tmp), source, progress=reporter.update)
            self.assertIn("FAILED", output.getvalue())
            self.assertIn("stage=source_totals", output.getvalue())
            self.assertFalse(reporter.thread.is_alive())
            self.assertTrue((Path(tmp) / "datasets" / "db3-bulk-fixture" / "batches" /
                             "202605-g0000" / "error.json").exists())

    def test_fixed_pilot_cohort_preserves_exact_product_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            version = "db3-bulk-fixed-fixture"
            source = root / "datasets" / version
            source.mkdir(parents=True)
            rows = [
                {"company_id": 3, "Rd04_Physic_Cd": "0001", "Rd04_Use_Gu": "0"},
                {"company_id": 3, "Rd04_Physic_Cd": "A003", "Rd04_Use_Gu": "1"},
            ]
            master = source / "products.jsonl"
            master.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            manifest = {"schema_version": "ssai_ml_bulk_supply_v0.1", "dataset_version": version,
                        "company_id": 3, "source_db_alias": "DB3", "dataset_status": "COMPLETE",
                        "evaluation_grade": "RESEARCH_POSTHOC", "product_count": 2,
                        "master_sha256": hashlib.sha256(master.read_bytes()).hexdigest()}
            manifest_path = source / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            trusted = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            codes, _ = _fixed_cohort(root, version, trusted)
            self.assertEqual(codes, ("0001", "A003"))
            self.assertEqual([row["Rd04_Physic_Cd"] for row in _select_fixed_products(
                [rows[1], rows[0], {"Rd04_Physic_Cd": "Z999"}], codes)], ["0001", "A003"])
            with self.assertRaisesRegex(ValueError, "missing"):
                _select_fixed_products([rows[0]], codes)
            with self.assertRaisesRegex(ValueError, "checksum"):
                _fixed_cohort(root, version, "0" * 64)
            master.write_text(master.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "checksum"):
                _fixed_cohort(root, version, trusted)

    def test_fixed_cohort_profile_change_blocks_before_erp_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            version = "db3-bulk-fixed-fixture"
            source = root / "datasets" / version
            source.mkdir(parents=True)
            master = source / "products.jsonl"
            master.write_text(json.dumps({"company_id": 3, "Rd04_Physic_Cd": "0001"}) + "\n",
                              encoding="utf-8")
            manifest_path = source / "manifest.json"
            manifest_path.write_text(json.dumps({
                "schema_version": "ssai_ml_bulk_supply_v0.1", "dataset_version": version,
                "company_id": 3, "source_db_alias": "DB3", "dataset_status": "COMPLETE",
                "evaluation_grade": "RESEARCH_POSTHOC", "product_count": 1,
                "master_sha256": hashlib.sha256(master.read_bytes()).hexdigest(),
                "profile_fingerprint": "old-profile",
            }), encoding="utf-8")
            trusted = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            with (patch("tools.extract_ml_bulk_db3.ml_storage_root", return_value=root),
                  patch("tools.extract_ml_bulk_db3.load_dashboard_profile_checked",
                        return_value=SimpleNamespace(status="ready", profile={"io_gu_list": ["501"]})),
                  patch("tools.extract_ml_bulk_db3.get_company_db_config",
                        side_effect=AssertionError("ERP connection must not be requested"))):
                with self.assertRaisesRegex(ValueError, "profile differs"):
                    extract_run(dataset_version="db3-bulk-new", date_from=date(2018, 1, 1),
                                date_to=date(2026, 8, 30), group_size=50, product_limit=None,
                                include_dataset_version=None, resume=False, max_batches=1,
                                fixed_cohort_version=version, fixed_cohort_manifest_sha256=trusted)

    def test_verified_shards_feed_unchanged_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run(root, FakeSource(), start=date(2026, 4, 1), end=date(2026, 8, 30))
            calendar = {"status": "ready", "start_date": "2026-04-01", "end_date": "2026-08-30",
                        "holiday_dates": [], "known_as_of": "2026-09-09", "version": "fixture"}
            target = materialize_research_cohort(
                root / "datasets" / "db3-bulk-fixture", root,
                dataset_version="db3-research-bulk-fixture", product_codes=("0001", "0002"),
                as_of_dates=("2026-06-30", "2026-07-31"), calendar=calendar,
            )
            payload = json.loads((target / "fixture.json").read_text(encoding="utf-8"))
            self.assertEqual(len(payload["outbound_rows"]), 4)
            self.assertEqual(run_fixture(target / "fixture.json", root / "runs", run_id="bulk-baseline")
                             ["source_row_count"], 4)

    def test_batch_exact_resume_and_no_sale_product(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = FakeSource()
            partial = run(root, source, max_batches=1)
            self.assertEqual((partial["new_batches"], partial["verified_batches"], partial["planned_batches"]),
                             (1, 1, 4))
            self.assertFalse((root / "datasets" / "db3-bulk-fixture" / "manifest.json").exists())
            second = run(root, source, resume=True, max_batches=1)
            self.assertEqual((second["new_batches"], second["verified_batches"]), (1, 2))
            complete = run(root, source, resume=True)
            self.assertEqual((complete["new_batches"], complete["reused_batches"]), (2, 2))
            self.assertEqual((complete["source_rows"], Decimal(complete["source_quantity"])), (6, Decimal(23)))
            self.assertEqual((complete["master_products"], complete["active_products"]), (4, 3))
            self.assertEqual(len(source.calls), 4)
            target = root / "datasets" / "db3-bulk-fixture"
            self.assertEqual(verify_bulk_dataset(target)["source_rows"], 6)
            manifest_path = target / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("whole_sha256")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            again = run(root, source, resume=True)
            self.assertEqual((again["new_batches"], again["reused_batches"]), (0, 4))
            self.assertEqual(len(source.calls), 4)
            role = json.loads((root / "datasets" / "db3-bulk-fixture" / "batches" /
                               "202605-g0000" / "manifest.json").read_text(encoding="utf-8"))["role_totals"]
            self.assertEqual((role["normal_rows"], role["normal_quantity"], role["return_rows"]),
                             (2, "3", 0))

    def test_failed_batch_explicit_resume_preserves_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = FakeSource()
            source.failed = "202605-g0001"
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                run(root, source)
            target = root / "datasets" / "db3-bulk-fixture"
            self.assertFalse((target / "manifest.json").exists())
            error = json.loads((target / "batches" / "202605-g0001" / "error.json").read_text())
            self.assertEqual((error["failed_stage"], error["error_type"]),
                             ("source_totals", "RuntimeError"))
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                run(root, source, resume=True)
            source.failed = None
            result = run(root, source, resume=True)
            self.assertEqual((result["reused_batches"], result["new_batches"]), (1, 3))
            self.assertTrue((target / "failed" / "202605-g0001" / "error.json").exists())
            self.assertTrue((target / "failed" / "202605-g0001-attempt2" / "error.json").exists())

    def test_interrupted_batch_without_manifest_is_quarantined_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = FakeSource()
            run(root, source, max_batches=1)
            target = root / "datasets" / "db3-bulk-fixture"
            interrupted = target / "batches" / "202605-g0001"
            interrupted.mkdir()
            (interrupted / "r120.jsonl").write_text("incomplete", encoding="utf-8")
            result = run(root, source, resume=True)
            self.assertEqual(result["verified_batches"], 4)
            self.assertTrue((target / "failed" / "202605-g0001" / "r120.jsonl").exists())

    def test_corruption_scope_and_source_totals_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = FakeSource()
            run(root, source)
            target = root / "datasets" / "db3-bulk-fixture"
            part = target / "batches" / "202605-g0000" / "r120.jsonl"
            with part.open("a", encoding="utf-8") as stream:
                stream.write("{}\n")
            with self.assertRaisesRegex(ValueError, "checksum"):
                verify_bulk_dataset(target)
            with self.assertRaisesRegex(ValueError, "resume plan"):
                build_bulk_dataset(root, dataset_version="db3-bulk-fixture", source=source,
                                   master_rows=MASTER, saved_io_codes=("501", "601"),
                                   profile_fingerprint="changed", calendar_version="fixture",
                                   date_from=date(2026, 5, 1), date_to=date(2026, 6, 30),
                                   group_size=2, resume=True)


if __name__ == "__main__":
    unittest.main()
