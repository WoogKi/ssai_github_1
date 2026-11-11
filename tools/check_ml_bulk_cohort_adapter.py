"""DB-free gate for the bounded bulk-to-legacy research adapter."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app.ml import bulk_dataset as bulk
from app.ml.learning import _load_fixture
from tools import materialize_ml_bulk_cohort as adapter
from tools.check_ml_bulk_dataset import DenseSource, FakeSource, run as build_fixture


CALENDAR = {"status": "ready", "start_date": "2026-04-01", "end_date": "2026-08-30",
            "holiday_dates": [], "known_as_of": "2026-09-01", "version": "fixture"}


class BulkCohortAdapterGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        rows = FakeSource().rows
        documents = []
        for index, row in enumerate(rows):
            row["Rd12_Trans_Seq"] = str(index)
            documents.append({"company_id": 3, "Rd13_Trans_Di": "3",
                              "Rd13_Trans_YyMmDd": row["Rd12_Trans_YyMmDd"],
                              "Rd13_Ven_Cd": row["Rd12_Ven_Cd"], "Rd13_Trans_Seq": str(index),
                              "Rd13_Supply_Price": "10", "Rd13_Tax_Price": "1", "header_count": 1})
        self.rows = rows
        build_fixture(self.root, DenseSource(rows, documents), start=date(2026, 4, 1), end=date(2026, 8, 30))
        self.source = self.root / "datasets" / "db3-bulk-fixture"
        self.prior = bulk.materialize_research_cohort(
            self.source, self.root, dataset_version="db3-research-prior", product_codes=("0001",),
            as_of_dates=("2026-06-30", "2026-07-31"), calendar=CALENDAR)

    def run_adapter(self, **changes):
        args = dict(bulk_version="db3-bulk-fixture", cohort_version="db3-research-prior",
                    output_version="db3-research-new", date_from="2026-04-01", date_to="2026-08-30",
                    as_of_dates=("2026-06-30", "2026-07-31"))
        args.update(changes)
        return adapter.run(self.root, **args)

    def rewrite_prior(self, change):
        fixture, manifest = _load_fixture(self.prior / "fixture.json")
        change(fixture)
        raw = json.dumps(fixture).encode("utf-8")
        (self.prior / "fixture.json").write_bytes(raw)
        manifest["sha256_by_file"]["fixture.json"] = hashlib.sha256(raw).hexdigest()
        (self.prior / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_exact_source_and_linked_headers_legacy_consumer(self):
        before = {str(path): bulk._sha(path) for path in self.root.rglob("*") if path.is_file()}
        target = self.run_adapter()
        fixture, manifest = _load_fixture(target / "fixture.json")
        selected = [row for row in self.rows if row["Rd12_Physic_Cd"] == "0001"]
        self.assertEqual(fixture["outbound_rows"], selected)
        self.assertEqual(len(fixture["document_rows"]), 2)
        self.assertEqual(manifest["source_rows"], 2)
        self.assertEqual(Decimal(manifest["source_quantity"]), Decimal(3))
        self.assertEqual(sum(Decimal(row["Rd12_Oquantity"]) for row in fixture["outbound_rows"]), 2)
        self.assertEqual(fixture["calendar"]["known_as_of"], "2026-09-01")
        self.assertFalse(fixture["calendar_asof_required"])
        self.assertEqual(manifest["evaluation_grade"], "research_posthoc")
        self.assertEqual(manifest["parent_bulk_manifest_sha256"], bulk._sha(self.source / "manifest.json"))
        self.assertEqual(before, {name: bulk._sha(Path(name)) for name in before})

    def test_fixture_checksum_corruption_is_blocked(self):
        path = self.prior / "fixture.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.run_adapter()

    def test_company_schema_version_and_product_boundaries(self):
        original = (self.prior / "fixture.json").read_bytes()
        manifest = (self.prior / "manifest.json").read_bytes()
        changes = [lambda f: f.update(company_id=4), lambda f: f.update(schema_version="other"),
                   lambda f: f.update(dataset_version="other"),
                   lambda f: f["product_rows"][0].update(company_id=4),
                   lambda f: f["product_rows"].append(dict(f["product_rows"][0])),
                   lambda f: f["product_rows"][0].update(Rd04_Physic_Cd=1),
                   lambda f: f["product_rows"][0].update(Rd04_Physic_Cd="MISSING")]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                (self.prior / "fixture.json").write_bytes(original)
                (self.prior / "manifest.json").write_bytes(manifest)
                self.rewrite_prior(change)
                with self.assertRaises(ValueError):
                    self.run_adapter()

    def test_calendar_invalid_not_promoted_to_ready(self):
        original = (self.prior / "fixture.json").read_bytes()
        manifest = (self.prior / "manifest.json").read_bytes()
        for fields in ({"status": "unavailable"}, {"start_date": "2026-05-01"},
                       {"holiday_dates": ["not-a-day"]}, {"known_as_of": "unknown"},
                       {"version": "different"}, {"version": ""}):
            with self.subTest(fields=fields):
                (self.prior / "fixture.json").write_bytes(original)
                (self.prior / "manifest.json").write_bytes(manifest)
                self.rewrite_prior(lambda f: f["calendar"].update(fields))
                with patch.object(adapter, "materialize_research_cohort") as materialize:
                    with self.assertRaises(ValueError):
                        self.run_adapter()
                    materialize.assert_not_called()

    def test_saved_io_mismatch_is_blocked(self):
        self.rewrite_prior(lambda f: f.update(saved_io_codes=["501"]))
        with self.assertRaisesRegex(ValueError, "Saved IO"):
            self.run_adapter()

    def test_bulk_manifest_identity_and_grade_are_required(self):
        path = self.source / "manifest.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        for fields in ({"company_id": 4}, {"dataset_version": "other"},
                       {"schema_version": "other"}, {"dataset_status": "BUILDING"},
                       {"evaluation_grade": "operational"}):
            with self.subTest(fields=fields), patch.object(adapter, "materialize_research_cohort") as materialize:
                path.write_text(json.dumps({**original, **fields}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.run_adapter()
                materialize.assert_not_called()

    def test_extraction_calendar_unavailable_allows_explicit_cohort_calendar(self):
        path = self.source / "plan.json"
        plan = json.loads(path.read_text(encoding="utf-8"))
        plan["calendar_version"] = "UNAVAILABLE_ASOF_EXTRACTION"
        path.write_text(json.dumps(plan), encoding="utf-8")
        with patch.object(adapter, "materialize_research_cohort", return_value=self.prior) as materialize:
            self.assertEqual(self.run_adapter(), self.prior)
            self.assertEqual(materialize.call_args.kwargs["calendar"]["version"], "fixture")
            self.assertEqual(materialize.call_args.kwargs["calendar"]["known_as_of"], "2026-09-01")

    def test_asof_and_whole_month_boundaries_fail_before_verification(self):
        for changes in (dict(as_of_dates=()), dict(as_of_dates=("2026-06-30",) * 2),
                        dict(as_of_dates=("2026-06-28",)), dict(as_of_dates=("2026-08-01",)),
                        dict(date_from="2026-04-02"), dict(date_from="20260401")):
            with self.subTest(changes=changes), patch.object(bulk, "verify_bulk_dataset") as verify:
                with self.assertRaises(ValueError):
                    self.run_adapter(**changes)
                verify.assert_not_called()

    def test_no_overwrite_before_bulk_verification(self):
        before = bulk._sha(self.prior / "fixture.json")
        with patch.object(bulk, "verify_bulk_dataset") as verify:
            with self.assertRaises(FileExistsError):
                self.run_adapter(output_version="db3-research-prior")
            verify.assert_not_called()
        self.assertEqual(before, bulk._sha(self.prior / "fixture.json"))

    def test_prior_size_cap_before_load(self):
        with patch.object(adapter, "MAX_BATCH_BYTES", 1), patch.object(adapter, "_load_fixture") as load:
            with self.assertRaisesRegex(ValueError, "byte cap"):
                self.run_adapter()
            load.assert_not_called()

    def test_common_retained_bytes_bound_before_output(self):
        with patch.object(bulk, "MAX_BATCH_BYTES", 1), patch.object(bulk, "verify_bulk_dataset"):
            with self.assertRaisesRegex(ValueError, "retained byte cap"):
                self.run_adapter()
        self.assertFalse((self.root / "datasets" / "db3-research-new").exists())

    def test_final_serialized_fixture_cap_before_output(self):
        self.rewrite_prior(lambda f: f["product_rows"][0].update(Rd04_Physic_Cd="Z9"))
        with patch.object(bulk, "MAX_BATCH_BYTES", 1), patch.object(bulk, "verify_bulk_dataset"):
            with self.assertRaisesRegex(ValueError, "fixture exceeds byte cap"):
                self.run_adapter()
        self.assertFalse((self.root / "datasets" / "db3-research-new").exists())

    def test_legacy_row_cap_still_enforced(self):
        original = bulk._iter_rows
        def synthetic_rows(path):
            if path.name == "r120.jsonl" and path.parent.name == "202604-g0000":
                for index in range(30001):
                    yield {**self.rows[0], "Rd12_Out_Seq": str(index)}
            else:
                yield from original(path)
        with patch.object(bulk, "_iter_rows", side_effect=synthetic_rows), patch.object(bulk, "verify_bulk_dataset"):
            with self.assertRaisesRegex(ValueError, "legacy row cap"):
                self.run_adapter()
        self.assertFalse((self.root / "datasets" / "db3-research-new").exists())

    def test_small_product_cap_before_verification(self):
        self.rewrite_prior(lambda f: f.update(product_rows=[
            {"company_id": 3, "Rd04_Physic_Cd": str(index)} for index in range(13)]))
        with patch.object(bulk, "verify_bulk_dataset") as verify:
            with self.assertRaisesRegex(ValueError, "1..12"):
                self.run_adapter()
            verify.assert_not_called()

    def test_shared_document_not_duplicated_and_other_io_preserved(self):
        rows = [dict(row) for row in self.rows]
        for row in rows:
            row["Rd12_Trans_Seq"] = "10"
        rows[0]["Rd12_Io_Gu"] = "601"
        rows[3]["Rd12_Io_Gu"] = "599"
        rows[3]["Rd12_Mod_Time"] = "20260801120000"
        documents = [{"company_id": 3, "Rd13_Trans_Di": "3", "Rd13_Trans_YyMmDd": day,
                      "Rd13_Ven_Cd": "V1", "Rd13_Trans_Seq": "10", "header_count": 1,
                      "Rd13_Supply_Price": "10", "Rd13_Tax_Price": "1"}
                     for day in ("20260510", "20260610")]
        source_root = self.root / "other"
        build_fixture(source_root, DenseSource(rows, documents), start=date(2026, 4, 1), end=date(2026, 8, 30))
        target = bulk.materialize_research_cohort(
            source_root / "datasets" / "db3-bulk-fixture", self.root,
            dataset_version="db3-research-new", product_codes=("0001", "0002"),
            as_of_dates=("2026-06-30",), calendar=CALENDAR)
        fixture, _ = _load_fixture(target / "fixture.json")
        self.assertEqual(len(fixture["document_rows"]), 2)
        self.assertEqual(fixture["outbound_rows"], [row for row in rows if row["Rd12_Physic_Cd"] in ("0001", "0002")])
        self.assertEqual(fixture["saved_io_codes"], ["501", "601"])

    def test_bulk_shard_tampering_is_blocked(self):
        path = self.source / "batches" / "202605-g0000" / "r120.jsonl"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaises(ValueError):
            self.run_adapter()
        self.assertFalse((self.root / "datasets" / "db3-research-new").exists())

    def test_version_traversal_and_link_rejected(self):
        with self.assertRaises(ValueError):
            self.run_adapter(output_version="../escape")
        with patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaisesRegex(ValueError, "links"):
                adapter._input_file(self.prior / "fixture.json", self.root, bulk.MAX_BATCH_BYTES)

    def test_no_sale_product_preserved_not_missing(self):
        self.rewrite_prior(lambda f: f["product_rows"][0].update(Rd04_Physic_Cd="Z9"))
        fixture, manifest = _load_fixture(self.run_adapter() / "fixture.json")
        self.assertEqual(manifest["source_rows"], 0)
        self.assertEqual(fixture["outbound_rows"], [])
        self.assertEqual(fixture["document_rows"], [])
        self.assertEqual(fixture["product_rows"][0]["Rd04_Physic_Cd"], "Z9")

    def test_cli_preserves_summary_contract(self):
        args = ["materialize_ml_bulk_cohort", "--bulk-version", "db3-bulk-fixture",
                "--cohort-version", "db3-research-prior", "--output-version", "db3-research-new",
                "--date-from", "2026-04-01", "--date-to", "2026-08-30", "--as-of-date", "2026-06-30"]
        output = io.StringIO()
        with patch("sys.argv", args), patch.object(adapter, "ml_storage_root", return_value=self.root), redirect_stdout(output):
            adapter.main()
        self.assertEqual(json.loads(output.getvalue()), {"dataset_version": "db3-research-new",
                         "source_rows": 2, "product_count": 1, "evaluation_grade": "research_posthoc"})


if __name__ == "__main__":
    unittest.main()
