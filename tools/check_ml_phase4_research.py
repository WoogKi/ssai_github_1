"""DB-free research-grade forecast and bounded extraction guards."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from app.ml.dataset_contract import write_probe_dataset
from app.ml.db3_probe import run_db3_probe
from app.ml.phase4_extract import extract_research_fixture, save_research_dataset, select_products
from app.ml.research import create_research_dataset
from tools.check_ml_phase4_actual_dataset import check as check_actual_dataset
from tools.check_ml_phase2_db3 import fake_query
from tools.check_ml_phase1_foundation import outbound
from tools.run_ml_phase1_offline import run_fixture


def audit_fixture(rows, actual, *, fixture_changes=None, prediction_changes=None, tamper=False):
    first, last = date(2026, 1, 1), date(2026, 8, 30)
    fixture = {
        "schema_version": "ml_phase1_fixture_v1", "dataset_version": "synthetic-audit",
        "company_id": 3, "date_from": first.isoformat(), "date_to": last.isoformat(),
        "outbound_rows": rows, "document_rows": [], "vendor_rows": [],
        "product_rows": [{"company_id": 3, "Rd04_Physic_Cd": "0001", "Rd04_Add_Date": "20200101"}],
        "standard_rows": [], "code_rows": [], "saved_io_codes": ["501", "601"],
        "complete_dates": [(first + timedelta(days=i)).isoformat() for i in range((last-first).days+1)],
        "as_of_dates": ["2026-06-30"], "timestamp_policy_confirmed": True,
        "calendar_asof_required": False,
    }
    fixture.update(fixture_changes or {})
    prediction = {"company_id": 3, "evaluation_grade": "research_posthoc", "horizon_days": 30,
                  "as_of": "2026-06-30", "product_code": "0001", "status": "ready",
                  "actual_quantity": actual}
    prediction.update(prediction_changes or {})
    with tempfile.TemporaryDirectory() as tmp:
        dataset, run = Path(tmp)/"dataset", Path(tmp)/"run"
        dataset.mkdir()
        run.mkdir()
        raw = (json.dumps(fixture) + "\n").encode()
        (dataset/"fixture.json").write_bytes(raw + (b" " if tamper else b""))
        manifest = {
            "schema_version": "ml_db3_research_v1", "dataset_version": fixture["dataset_version"],
            "company_id": 3, "dataset_status": "RESEARCH_POSTHOC", "evaluation_grade": "research_posthoc",
            "sha256_by_file": {"fixture.json": hashlib.sha256(raw).hexdigest()},
            "metrics": {"source_rows": len(rows), "selected_products": len(fixture["product_rows"]),
                        "source_quantity": str(sum((Decimal(r["Rd12_Quantity"]) for r in rows), Decimal(0)))},
        }
        (dataset/"manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (run/"predictions.json").write_text(json.dumps([prediction]), encoding="utf-8")
        return check_actual_dataset(dataset, run)


class ResearchGate(unittest.TestCase):
    def test_negative_label_is_unevaluated_not_zero_or_error(self):
        result = audit_fixture([outbound(day=date(2026, 7, 10), qty="-1")], None)
        self.assertEqual(result["ready_without_label_model_rows"], 1)

    def test_audit_accepts_same_day_compact_and_iso_timestamps(self):
        for stamp in ("20260630235959", "2026-06-30 23:59:59"):
            with self.subTest(stamp=stamp):
                result = audit_fixture([outbound(day=date(2026, 6, 30), add=stamp, mod=stamp)], "0")
                self.assertEqual(result["checked_ready_model_rows"], 1)

    def test_audit_rejects_late_missing_and_negative_history(self):
        for change in ({"mod": "20260701000000"}, {"add": ""}, {"qty": "-1"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                row = outbound(day=date(2026, 6, 30), add="20260630120000", mod="20260630120000")
                row.update({{"mod": "Rd12_Mod_Time", "add": "Rd12_Add_Time", "qty": "Rd12_Quantity"}[k]: v
                            for k, v in change.items()})
                audit_fixture([row], "0")

    def test_audit_ignores_late_history_before_90_day_window(self):
        result = audit_fixture([outbound(day=date(2026, 4, 1), add="20260701120000")], "0")
        self.assertEqual(result["checked_ready_model_rows"], 1)

    def test_audit_rejects_numeric_negative_label_and_unexplained_missing_label(self):
        for rows, actual in (([outbound(day=date(2026, 7, 10), qty="-1")], "-1"),
                             ([outbound(day=date(2026, 7, 10), qty="1")], None)):
            with self.subTest(actual=actual), self.assertRaises(ValueError):
                audit_fixture(rows, actual)

    def test_audit_distinguishes_incomplete_label_from_zero(self):
        dates = [(date(2026, 1, 1)+timedelta(days=i)).isoformat() for i in range(242)]
        dates.remove("2026-07-01")
        result = audit_fixture([], None, fixture_changes={"complete_dates": dates})
        self.assertEqual(result["ready_without_label_model_rows"], 1)
        with self.assertRaises(ValueError):
            audit_fixture([], "0", fixture_changes={"complete_dates": dates})

    def test_audit_zero_trade_product_and_exact_label_boundaries(self):
        self.assertEqual(audit_fixture([], "0")["products"], 1)
        rows = [outbound(day=day, qty=str(qty), seq=str(i), add="20260101120000", mod="20260101120000")
                for i, (day, qty) in enumerate(((date(2026,6,30), 20), (date(2026,7,1), 3),
                                                (date(2026,7,30), 4), (date(2026,7,31), 50)))]
        rows.append(outbound(day=date(2026,7,15), io="601", qty="9", seq="9"))
        self.assertEqual(audit_fixture(rows, "7")["checked_ready_model_rows"], 1)

    def test_audit_rejects_wrong_identity_period_and_checksum(self):
        for change in ({"company_id":4}, {"product_code":"other"}, {"as_of":"2026-07-31"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                audit_fixture([outbound(day=date(2026,7,10))], "2" if "company_id" in change else "0",
                              prediction_changes=change)
        with self.assertRaises(ValueError):
            audit_fixture([], "0", tamper=True)

    def test_extract_rejects_invalid_scope_before_query(self):
        calendar = {"status":"ready", "start_date":"2026-04-01", "end_date":"2026-08-30"}
        def no_query(*args):
            self.fail("invalid scope reached query")
        cases = ({"as_of_dates":()}, {"as_of_dates":("2026-06-28",)},
                 {"as_of_dates":("2026-08-01",)}, {"date_from":"2026041"},
                 {"product_codes":(" ", "P1", "P2", "P3", "P4", "P5")},
                 {"product_codes":("P0", "P1", "P2", "P3", "P4", "P5", "P5")})
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                extract_research_fixture(no_query, company_id=3, saved_io_codes=["501","601"],
                                         calendar=calendar, profile_fingerprint="fixture", **kwargs)

    def test_save_rejects_inconsistent_window_without_partial_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = {"company_id":3, "dataset_version":"bad-window", "date_from":"2026-04-01", "date_to":"2026-08-30"}
            with self.assertRaises(ValueError):
                save_research_dataset(Path(tmp), fixture, {"source_window":["20250101","20260830"]})
            self.assertFalse((Path(tmp)/"datasets").exists())

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
        self.assertEqual(fixture["as_of_dates"], ["2026-06-30", "2026-07-31"])
        self.assertEqual(metrics["source_window"], ["20260401", "20260830"])
        with tempfile.TemporaryDirectory() as tmp:
            from app.ml.learning import _load_fixture
            path = save_research_dataset(Path(tmp), fixture, metrics)
            loaded, manifest = _load_fixture(path / "fixture.json")
            self.assertEqual(loaded, fixture)
            self.assertEqual(manifest["source_window"], ["20260401", "20260830"])
            with self.assertRaises(FileExistsError):
                save_research_dataset(Path(tmp), fixture, metrics)
        calendar["start_date"] = "2025-01-01"
        extended, extended_metrics = extract_research_fixture(
            query, company_id=3, saved_io_codes=["501", "601"], calendar=calendar,
            profile_fingerprint="fixture", date_from="20250101", date_to_exclusive="20260831",
            product_codes=tuple(f"P{i}" for i in range(8)),
            as_of_dates=("2025-03-31", "2026-07-31"),
        )
        self.assertEqual(extended_metrics["source_window"], ["20250101", "20260830"])
        self.assertEqual(extended["as_of_dates"], ["2025-03-31", "2026-07-31"])
        self.assertEqual(len(extended["complete_dates"]), 607)
        with tempfile.TemporaryDirectory() as tmp:
            path = save_research_dataset(Path(tmp), extended, extended_metrics)
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["source_window"], ["20250101", "20260830"])
            self.assertEqual(_load_fixture(path / "fixture.json")[0], extended)


if __name__ == "__main__":
    unittest.main()
