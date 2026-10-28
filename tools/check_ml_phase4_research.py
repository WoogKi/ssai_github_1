"""DB-free research-grade forecast and bounded extraction guards."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from app.ml.dataset_contract import write_probe_dataset
from app.ml.db3_probe import run_db3_probe
from app.ml.phase4_extract import extract_research_fixture, select_products
from app.ml.research import create_research_dataset
from tools.check_ml_phase2_db3 import fake_query
from tools.check_ml_phase1_foundation import outbound
from tools.run_ml_phase1_offline import run_fixture


class ResearchGate(unittest.TestCase):
    def test_research_grade_is_explicit_and_strict_input_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_db3_probe(fake_query)
            source = Path(tmp) / "source"
            write_probe_dataset(
                source, fixture=result["fixture"], daily_r120=result["daily_r120"],
                daily_r130=result["daily_r130"], approval_reference="fixture",
                query_durations_ms=result["query_durations_ms"], probe_quality=result["quality"],
            )
            raw = json.loads((source / "fixture.json").read_text(encoding="utf-8"))
            raw["calendar"] = {"start_date": "2026-04-01", "end_date": "2026-08-30",
                               "holiday_dates": [], "known_as_of": "2026-09-09", "version": "official-fixture"}
            calendar_source = Path(tmp) / "calendar-source"
            calendar_source.mkdir()
            raw_bytes = (json.dumps(raw, sort_keys=True) + "\n").encode()
            (calendar_source / "fixture.json").write_bytes(raw_bytes)
            import hashlib
            base_manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
            base_manifest["sha256_by_file"] = {"fixture.json": hashlib.sha256(raw_bytes).hexdigest()}
            (calendar_source / "manifest.json").write_text(json.dumps(base_manifest), encoding="utf-8")
            root = Path(tmp) / "storage"
            research = create_research_dataset(calendar_source, root, saved_io_codes=["501", "601"],
                                               profile_fingerprint="fixture-profile")
            report = run_fixture(research / "fixture.json", root / "runs", run_id="research")
            self.assertEqual(report["evaluation_grade"], "research_posthoc")
            self.assertEqual(report["models"]["moving_average"]["evaluated_count"], 2)
            predictions = json.loads((root / "runs" / "research" / "predictions.json").read_text(encoding="utf-8"))
            self.assertTrue(all(item["evaluation_grade"] == "research_posthoc" for item in predictions))
            self.assertTrue(all(item["predicted_quantity"] is not None for item in predictions))
            strict = run_fixture(calendar_source / "fixture.json", root / "runs", run_id="strict")
            self.assertEqual(strict["models"]["moving_average"]["evaluated_count"], 0)

    def test_posthoc_without_research_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "fixture.json"
            source.write_text(json.dumps({"schema_version": "ml_phase1_fixture_v1", "calendar_asof_required": False}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "research-only manifest"):
                run_fixture(source, Path(tmp) / "runs", run_id="bad")

    def test_product_selection_has_multiple_categories_and_bounded_size(self):
        def row(index, count):
            return {"product_code": f"P{index:03d}", "row_count": count,
                    "active_days": max(1, count // 3), "min_quantity": 1,
                    "max_quantity": count, "negative_rows": 0}
        high = [row(index, 300 - index) for index in range(100)]
        low = [row(index + 100, index + 3) for index in range(30)]
        products, categories = select_products(high, low, {f"P{index:03d}" for index in range(0, 130, 2)})
        self.assertGreaterEqual(len(products), 6)
        self.assertLessEqual(len(products), 12)
        self.assertTrue(categories["high_frequency"])
        self.assertTrue(categories["intermittent"])
        self.assertTrue(categories["no_standard_code"])

    def test_batch_extract_preserves_independent_count_quantity_and_docs(self):
        def candidate(code, count):
            return {"product_code": code, "row_count": count, "active_days": count,
                    "min_quantity": 1, "max_quantity": count, "negative_rows": 0}

        high = [candidate(f"P{i}", 60 - i) for i in range(4)]
        low = [candidate(f"P{i}", i + 3) for i in range(4, 8)]

        def query(sql, params, cap):
            self.assertTrue(sql.lstrip().upper().startswith("SELECT"))
            if "AS active_days" in sql:
                return high if "DESC" in sql else low
            if "FROM dbo.Rddbc046" in sql:
                return []
            if "AS bonus_quantity" in sql and "GROUP BY Rd12_Physic_Cd" in sql:
                return [{"product_code": code, "row_count": 1, "quantity": "2", "bonus_quantity": "1"}
                        for code in params[2:]]
            if "TOP (10001) Rd12_Out_YyMmDd" in sql:
                return [outbound(day=date(2026, 6, 30), product=code, seq=str(int(code[1:]) + 1),
                                 add="20260630120000", mod="20260630120000") for code in params[2:]]
            if "MAX(H.Rd13_Supply_Price)" in sql:
                return [{"Rd13_Trans_Di": "3", "Rd13_Trans_YyMmDd": "20260630", "Rd13_Ven_Cd": "V1",
                         "Rd13_Trans_Seq": "10", "Rd13_Supply_Price": "100", "Rd13_Tax_Price": "10",
                         "header_count": 1}]
            if "FROM dbo.Rddbc030" in sql:
                return [{"Rd03_Ven_Cd": "V1", "Rd03_Ven_Num": "B1", "Rd03_Other1": "",
                         "Rd03_Supply_Cd_Gcode": "0012", "Rd03_Supply_Cd": "01"}]
            if "FROM dbo.Rddbc040" in sql:
                return [{"Rd04_Physic_Cd": code, "Rd04_Add_Date": "20260101"} for code in params]
            if "FROM dbo.Rddbc010" in sql:
                return [{"Rd01_Gcode": "0012", "Rd01_Tcode": "501", "Rd01_Hnm": "정상출고"}]
            raise AssertionError("unexpected bounded SQL")

        calendar = {"status": "ready", "start_date": "2026-04-01", "end_date": "2026-08-30",
                    "holiday_dates": [], "known_as_of": "2026-09-09", "version": "fixture"}
        fixture, metrics = extract_research_fixture(query, company_id=3, saved_io_codes=["501", "601"],
                                                    calendar=calendar, profile_fingerprint="fixture")
        self.assertEqual(metrics["source_rows"], 8)
        self.assertEqual(metrics["source_quantity"], "16")
        self.assertEqual(metrics["document_match_counts"], {"matched": 8})
        self.assertEqual(len(fixture["document_rows"]), 1)
        self.assertEqual(len(fixture["complete_dates"]), 152)


if __name__ == "__main__":
    unittest.main()
