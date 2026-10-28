"""DB-free contract checks for the bounded company-3 extraction path."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from app.ml.dataset_contract import completeness_by_day, validate_db3_scope, write_probe_dataset
from app.ml.db3_probe import run_db3_probe
from tools.check_ml_phase1_foundation import outbound
from tools.extract_ml_db3_probe import execute_probe, validate_destination
from tools.run_ml_phase1_offline import run_fixture


def fake_query(sql, params, cap):
    assert sql.lstrip().upper().startswith("SELECT")
    assert params and cap <= 2200
    if "GROUP BY Rd12_Out_YyMmDd" in sql:
        return [{"out_date": "20260630", "io_family": "5", "row_count": 1,
                 "quantity": "2", "bonus_quantity": "1", "negative_rows": 0,
                 "missing_add_time": 0, "missing_mod_time": 0}]
    if "GROUP BY Rd13_Trans_YyMmDd" in sql:
        return [{"out_date": "20260630", "row_count": 1, "supply_amount": "100", "tax_amount": "10"}]
    if "COUNT_BIG(*) AS normal_rows" in sql:
        return [{"product_code": "0001", "normal_rows": 5}]
    if "TOP (501) Rd12_Out_YyMmDd" in sql:
        return [outbound(day=date(2026, 6, 30), add="20260630120000", mod="20260630120000")]
    if "MAX(H.Rd13_Supply_Price)" in sql:
        return [{"Rd13_Trans_Di": "3", "Rd13_Trans_YyMmDd": "20260630",
                 "Rd13_Ven_Cd": "V1", "Rd13_Trans_Seq": "10",
                 "Rd13_Supply_Price": "100", "Rd13_Tax_Price": "10", "header_count": 1}]
    if "FROM dbo.Rddbc030" in sql:
        return [{"Rd03_Ven_Cd": "V1", "Rd03_Ven_Num": "B1", "Rd03_Other1": "",
                 "Rd03_Supply_Cd_Gcode": "0012", "Rd03_Supply_Cd": "01"}]
    if "FROM dbo.Rddbc040" in sql:
        return [{"Rd04_Physic_Cd": "0001", "Rd04_Add_Date": "20260101"}]
    if "FROM dbo.Rddbc046" in sql:
        return []
    if "FROM dbo.Rddbc010" in sql:
        return [{"Rd01_Gcode": "0012", "Rd01_Tcode": "01", "Rd01_Hnm": "약국"}]
    raise AssertionError("unexpected SQL")


class Db3Gate(unittest.TestCase):
    def test_only_company3_and_bounded_dates(self):
        for company, end in [(4, date(2026, 10, 1)), (3, date(2026, 10, 2))]:
            with self.assertRaises(ValueError):
                validate_db3_scope(company_id=company, date_from=date(2026, 4, 1), date_to=end, alias="DB3")
        with self.assertRaises(ValueError):
            validate_destination(Path(__file__).resolve().parents[1] / "data")

    def test_activity_is_not_completeness(self):
        states = completeness_by_day([{"out_date": "20260923"}],
                                     date_from=date(2026, 9, 23), date_to=date(2026, 9, 24))
        self.assertEqual(states["20260923"], "observed_unverified")
        self.assertEqual(states["20260924"], "partial_unconfirmed")
        self.assertNotIn("confirmed_complete", states.values())

    def test_bounded_extraction_offline_withheld_and_checksum(self):
        result = run_db3_probe(fake_query)
        self.assertEqual(result["query_count"], 9)
        self.assertEqual(result["quality"]["source_quantity"], "2")
        self.assertEqual(result["quality"]["document_match_counts"], {"matched": 1})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "dataset"
            manifest = write_probe_dataset(
                root, fixture=result["fixture"], daily_r120=result["daily_r120"],
                daily_r130=result["daily_r130"], approval_reference="fixture-approval",
                query_durations_ms=result["query_durations_ms"], probe_quality=result["quality"],
            )
            self.assertEqual(manifest["table_totals"]["r120_period_rows_all_io"], 1)
            self.assertEqual(manifest["dataset_status"], "REVIEW_REQUIRED")
            self.assertIsNone(manifest["last_complete_date"])
            report = run_fixture(root / "fixture.json", Path(tmp) / "runs", run_id="db3-fixture")
            self.assertEqual(report["source_row_count"], 1)
            for model in report["models"].values():
                self.assertEqual(model["evaluated_count"], 0)
            predictions = json.loads((Path(tmp) / "runs" / "db3-fixture" / "predictions.json").read_text(encoding="utf-8"))
            self.assertEqual(len(predictions), 4)
            self.assertTrue(all(item["predicted_quantity"] is None for item in predictions))
            run_fixture(root / "fixture.json", Path(tmp) / "runs", run_id="db3-fixture-repeat")
            repeated = json.loads((Path(tmp) / "runs" / "db3-fixture-repeat" / "predictions.json").read_text(encoding="utf-8"))
            self.assertEqual(predictions, repeated)
            (root / "fixture.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                run_fixture(root / "fixture.json", Path(tmp) / "runs", run_id="tampered")

    def test_duplicate_r130_header_stops(self):
        def duplicate(sql, params, cap):
            rows = fake_query(sql, params, cap)
            if "MAX(H.Rd13_Supply_Price)" in sql:
                rows[0]["header_count"] = 2
            return rows
        with self.assertRaisesRegex(ValueError, "duplicate R130"):
            run_db3_probe(duplicate)

    def test_company4_rejected_before_registry_or_erp_connection(self):
        with patch("tools.extract_ml_db3_probe.get_company_db_config") as registry, patch(
            "tools.extract_ml_db3_probe.pyodbc.connect"
        ) as connect:
            with self.assertRaises(ValueError):
                execute_probe(company_id=4, output_root=Path("outside"), approval_reference="fixture")
            registry.assert_not_called()
            connect.assert_not_called()

    def test_truncated_sample_stops_before_dataset_creation(self):
        def truncated(sql, params, cap):
            if "TOP (501) Rd12_Out_YyMmDd" in sql:
                return [outbound(day=date(2026, 6, 30), seq=str(i)) for i in range(501)]
            return fake_query(sql, params, cap)
        with self.assertRaisesRegex(ValueError, "truncated"):
            run_db3_probe(truncated)


if __name__ == "__main__":
    unittest.main()
