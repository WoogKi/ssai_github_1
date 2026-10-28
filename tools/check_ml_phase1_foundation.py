"""DB-free contract gate for the first ML data and baseline layer."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app.ml.baselines import backtest, evaluate, forecast_from_example
from app.ml.phase1 import OfflineCalendar, build_daily, build_example, business_number_quantity, document_amount_once, prepare_sources
from app.ml.run_state import RunState
from tools.run_ml_phase1_offline import RunCancelled, run_fixture


AS_OF = date(2026, 9, 30)
START = AS_OF - timedelta(days=89)
END = AS_OF + timedelta(days=30)


def outbound(*, day: date = AS_OF, seq: str = "1", vendor: str = "V1", product: str = "0001",
             stock_apply: str = "SA", cost_apply: str = "CA", add: str = "20260901120000",
             mod: str = "20260901120000", io: str = "501", qty: str = "2") -> dict:
    return {
        "company_id": 3, "Rd12_Out_YyMmDd": day.strftime("%Y%m%d"), "Rd12_Ven_Cd": vendor,
        "Rd12_Out_Seq": seq, "Rd12_Physic_Cd": product, "Rd12_Io_Gu": io,
        "Rd12_Quantity": qty, "Rd12_Oquantity": "1", "Rd12_Stock_Cd_Gcode": "S",
        "Rd12_Stock_Cd": "00001", "Rd12_Stock_Apply_Cd": stock_apply,
        "Rd12_Cost_Apply_Cd": cost_apply, "Rd12_Trans_Di": "3",
        "Rd12_Trans_YyMmDd": day.strftime("%Y%m%d"), "Rd12_Trans_Seq": "10",
        "Rd12_Add_Time": add, "Rd12_Mod_Time": mod,
    }


def prepared(rows=None, *, documents=None, products=None):
    return prepare_sources(
        company_id=3, outbound_rows=rows if rows is not None else [outbound()],
        document_rows=documents if documents is not None else [{
            "company_id": 3, "Rd13_Trans_Di": "3", "Rd13_Trans_YyMmDd": "20260930",
            "Rd13_Ven_Cd": "V1", "Rd13_Trans_Seq": "10", "amount": "100.50",
        }],
        vendor_rows=[
            {"company_id": 3, "Rd03_Ven_Cd": "V1", "Rd03_Ven_Num": "B", "Rd03_Other1": "H",
             "Rd03_Supply_Cd_Gcode": "G", "Rd03_Supply_Cd": "X"},
            {"company_id": 3, "Rd03_Ven_Cd": "V2", "Rd03_Ven_Num": "B", "Rd03_Other1": "",
             "Rd03_Supply_Cd_Gcode": "G", "Rd03_Supply_Cd": "X"},
        ],
        product_rows=products if products is not None else [
            {"company_id": 3, "Rd04_Physic_Cd": "0001", "Rd04_Add_Date": "20260101"},
            {"company_id": 3, "Rd04_Physic_Cd": "A2", "Rd04_Add_Date": "20260101"},
        ],
        standard_rows=[], code_rows=[{"company_id": 3, "Rd01_Gcode": "G", "Rd01_Tcode": "X", "Rd01_Hnm": "약국"}],
        saved_io_codes=["501", "601"], channel_by_io={"501": "online"},
        valid_care_provider_codes=["H"], supplier_type_by_label={"약국": "pharmacy"},
    )


def days(start=START, end=END):
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def calendar():
    return OfflineCalendar(START.replace(day=1), END, frozenset(), date(2026, 1, 1), "fixture-v1")


class MlPhase1Gate(unittest.TestCase):
    def test_join_preserves_pk_quantity_and_document_amount_once(self):
        rows = [outbound(seq="1"), outbound(seq="2"), outbound(seq="3", vendor="V2", product="A2")]
        result = prepared(rows)
        self.assertEqual(result.source_row_count, 3)
        self.assertEqual(result.source_quantity, Decimal(6))
        self.assertEqual(result.document_match_counts, {"matched": 2, "not_linked": 1})
        self.assertEqual(document_amount_once(result, "amount"), Decimal("100.50"))
        self.assertEqual(len({row.source.source_key for row in result.rows}), 3)
        self.assertEqual(result.rows[0].source.stock_apply_code, "SA")
        self.assertEqual(result.rows[0].source.cost_apply_code, "CA")
        self.assertEqual(result.rows[0].channel, "online")
        self.assertEqual(result.rows[0].care_provider_status, "care_provider")
        self.assertEqual(result.rows[2].care_provider_status, "unknown")
        self.assertEqual(result.rows[0].supplier_type, "pharmacy")
        self.assertEqual(result.rows[0].standard_codes, ())
        self.assertEqual({row.source.vendor_code for row in result.rows}, {"V1", "V2"})
        self.assertEqual(business_number_quantity(result), {(3, "B"): Decimal(6)})
        self.assertEqual([row.business_number for row in result.rows], ["B", "B", "B"])

    def test_duplicate_pk_and_header_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate R120"):
            prepared([outbound(), outbound()])
        doc = {"company_id": 3, "Rd13_Trans_Di": "3", "Rd13_Trans_YyMmDd": "20260930", "Rd13_Ven_Cd": "V1", "Rd13_Trans_Seq": "10"}
        with self.assertRaisesRegex(ValueError, "duplicate R130"):
            prepared(documents=[doc, doc])

    def test_direction_and_company_boundary(self):
        wrong = outbound()
        wrong["Rd12_Trans_Di"] = "1"
        self.assertEqual(prepared([wrong]).rows[0].document_status, "direction_mismatch")
        wrong["company_id"] = 4
        with self.assertRaisesRegex(ValueError, "company boundary"):
            prepared([wrong])

    def test_daily_complete_zero_vs_incomplete_and_scope(self):
        result = prepared([outbound(), outbound(seq="2", stock_apply="SB", cost_apply="CB", io="601")])
        complete = [day for day in days() if day != AS_OF - timedelta(days=1)]
        data = build_daily(result, company_id=3, date_from=START, date_to=END, complete_dates=complete)
        self.assertEqual(data.points[3, "0001", AS_OF].normal_quantity, Decimal(2))
        self.assertEqual(data.points[3, "0001", AS_OF - timedelta(days=1)].normal_quantity, None)
        self.assertEqual(data.points[3, "A2", AS_OF].normal_quantity, Decimal(0))
        self.assertEqual(len(data.scope_totals), 1)
        self.assertEqual(next(iter(data.scope_totals))[3:], ("S", "00001", "SA", "CA"))
        self.assertEqual(len(data.rows), 2)

    def test_label_dates_and_calendar(self):
        result = prepared([outbound(), outbound(day=AS_OF + timedelta(days=30), seq="4", qty="5")])
        data = build_daily(result, company_id=3, date_from=START, date_to=END, complete_dates=days())
        example = build_example(data, company_id=3, product_code="0001", as_of=AS_OF,
                                calendar=calendar(), timestamp_policy_confirmed=True)
        self.assertEqual(example.feature_status, "ready")
        self.assertEqual(example.label_status, "complete")
        self.assertEqual(example.actual_qty_30d, Decimal(5))
        self.assertEqual(example.qty_30d, Decimal(2))
        self.assertEqual(example.qty_90d, Decimal(2))
        self.assertEqual(example.calendar_status, "ready")
        self.assertIsNotNone(example.first_business_day_of_month)
        moving = forecast_from_example(example, method="moving_average")
        self.assertEqual(moving.predicted_quantity, Decimal(2))
        ewma = forecast_from_example(example, method="ewma", dataset=data)
        self.assertGreater(ewma.predicted_quantity, Decimal(0))
        self.assertEqual(backtest(data, company_id=3, product_codes=["0001"], as_of_dates=[AS_OF],
                                  calendar=calendar(), timestamp_policy_confirmed=True,
                                  method="moving_average"), (moving,))
        metrics = evaluate([moving])
        self.assertEqual(metrics.wape, Decimal("0.6"))
        self.assertEqual(metrics.mae, Decimal(3))
        self.assertEqual(metrics.bias, Decimal(-3))

    def test_incomplete_label_and_asof_leakage(self):
        result = prepared([outbound(mod="20261001120000")])
        data = build_daily(result, company_id=3, date_from=START, date_to=END,
                           complete_dates=[day for day in days() if day != AS_OF + timedelta(days=1)])
        example = build_example(data, company_id=3, product_code="0001", as_of=AS_OF,
                                calendar=calendar(), timestamp_policy_confirmed=True)
        self.assertEqual(example.feature_status, "asof_history_unavailable")
        self.assertEqual(example.label_status, "incomplete")
        self.assertIsNone(example.actual_qty_30d)
        self.assertIsNone(forecast_from_example(example, method="moving_average").predicted_quantity)
        self.assertEqual(build_example(data, company_id=3, product_code="0001", as_of=AS_OF,
                                       calendar=calendar(), timestamp_policy_confirmed=False).feature_status,
                         "timestamp_policy_unverified")
        late = prepared([outbound(add="20261001120000")])
        late_data = build_daily(late, company_id=3, date_from=START, date_to=END, complete_dates=days())
        self.assertEqual(build_example(late_data, company_id=3, product_code="0001", as_of=AS_OF,
                                       calendar=calendar(), timestamp_policy_confirmed=True).feature_status,
                         "asof_history_unavailable")

    def test_missing_quantity_and_numeric_product_rejected(self):
        row = outbound()
        row["Rd12_Quantity"] = None
        with self.assertRaisesRegex(ValueError, "quantity"):
            prepared([row])
        row["Rd12_Quantity"] = 2
        row["Rd12_Physic_Cd"] = 1
        with self.assertRaisesRegex(ValueError, "leading zeroes"):
            prepared([row])

    def test_product_registration_and_sales_evidence_asof(self):
        products = [
            {"company_id": 3, "Rd04_Physic_Cd": "0001", "Rd04_Add_Date": "20261010"},
            {"company_id": 3, "Rd04_Physic_Cd": "A2"},
        ]
        data = build_daily(prepared(rows=[], products=products), company_id=3,
                           date_from=START, date_to=END, complete_dates=days())
        self.assertEqual(data.points[3, "0001", AS_OF].state, "not_yet_registered")
        self.assertIsNone(data.points[3, "0001", AS_OF].normal_quantity)
        self.assertEqual(data.points[3, "0001", date(2026, 10, 10)].state, "complete")
        self.assertEqual(data.points[3, "A2", AS_OF].state, "existence_unverified")
        example = build_example(data, company_id=3, product_code="0001", as_of=AS_OF,
                                calendar=calendar(), timestamp_policy_confirmed=True)
        self.assertEqual(example.feature_status, "not_yet_registered")
        prior_sale = prepared([outbound(day=START, seq="old")], products=products)
        with_sale = build_daily(prior_sale, company_id=3, date_from=START, date_to=END, complete_dates=days())
        self.assertEqual(with_sale.points[3, "0001", AS_OF].product_existence_status,
                         "sales_evidence_master_conflict")
        self.assertEqual(with_sale.points[3, "0001", AS_OF].normal_quantity, Decimal(0))
        late_sale = prepared([outbound(day=START, seq="late", add="20261101120000")], products=products)
        late_data = build_daily(late_sale, company_id=3, date_from=START, date_to=END, complete_dates=days())
        self.assertEqual(late_data.points[3, "0001", AS_OF].state, "not_yet_registered")

    def test_negative_normal_quantity_blocks_forecast_without_erasure(self):
        rows = [outbound(qty="-3"), outbound(seq="ret", io="601", qty="7")]
        result = prepared(rows)
        data = build_daily(result, company_id=3, date_from=START, date_to=END, complete_dates=days())
        self.assertEqual(result.source_quantity, Decimal(4))
        self.assertEqual(data.points[3, "0001", AS_OF].normal_quantity, Decimal(-3))
        self.assertEqual(data.points[3, "0001", AS_OF].negative_normal_row_count, 1)
        example = build_example(data, company_id=3, product_code="0001", as_of=AS_OF,
                                calendar=calendar(), timestamp_policy_confirmed=True)
        self.assertEqual(example.feature_status, "negative_normal_quantity_unresolved")
        self.assertIsNone(forecast_from_example(example, method="moving_average").predicted_quantity)
        self.assertEqual(data.scope_totals[next(iter(data.scope_totals))], Decimal(-3))
        future = prepared([outbound(day=AS_OF + timedelta(days=1), qty="-2")])
        future_data = build_daily(future, company_id=3, date_from=START, date_to=END, complete_dates=days())
        future_example = build_example(future_data, company_id=3, product_code="0001", as_of=AS_OF,
                                       calendar=calendar(), timestamp_policy_confirmed=True)
        self.assertEqual(future_example.label_status, "negative_quantity_review")
        self.assertIsNone(future_example.actual_qty_30d)
        forced = replace(example, feature_status="ready", qty_30d=Decimal(-3))
        guarded = forecast_from_example(forced, method="moving_average")
        self.assertEqual(guarded.status, "negative_forecast_unresolved")
        self.assertIsNone(guarded.predicted_quantity)

    def test_sparse_points_match_explicit_daily_reference(self):
        rows = [outbound(day=START, qty="4"), outbound(day=AS_OF, seq="2", qty="-1"),
                outbound(day=AS_OF, seq="3", io="601", qty="9")]
        result = prepared(rows)
        complete = set(days()) - {AS_OF - timedelta(days=1)}
        data = build_daily(result, company_id=3, date_from=START, date_to=END, complete_dates=complete)
        for product in result.product_codes:
            for day in days():
                point = data.points[3, product, day]
                if day not in complete:
                    self.assertIsNone(point.normal_quantity)
                else:
                    expected = sum((row.source.quantity for row in result.rows
                                    if row.source.product_code == product and row.source.out_date == day
                                    and row.source.io_code in result.normal_codes), Decimal(0))
                    self.assertEqual(point.normal_quantity, expected)

    def test_calendar_missing_and_zero_actual_wape(self):
        data = build_daily(prepared(), company_id=3, date_from=START, date_to=END, complete_dates=days())
        missing = OfflineCalendar(START, AS_OF, frozenset(), AS_OF, "fixture-v1")
        example = build_example(data, company_id=3, product_code="0001", as_of=AS_OF,
                                calendar=missing, timestamp_policy_confirmed=True)
        self.assertEqual(example.calendar_status, "unavailable")
        self.assertEqual(forecast_from_example(example, method="moving_average").status, "calendar_unavailable")
        good = build_example(data, company_id=3, product_code="0001", as_of=AS_OF,
                             calendar=calendar(), timestamp_policy_confirmed=True)
        self.assertIsNone(evaluate([forecast_from_example(good, method="moving_average")]).wape)

    def test_status_and_events_do_not_invent_progress(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd() / ".codex_task_output") as directory:
            run = RunState(Path(directory), run_id="fixture_1", dataset_version="ds1")
            first = run.update(stage="prepare", event="START", done=0)
            self.assertIsNone(first["progress_percent"])
            run.update(stage="prepare", event="DONE", done=2, total=2)
            self.assertEqual(json.loads((run.path / "status.json").read_text(encoding="utf-8"))["progress_percent"], 100)
            self.assertEqual(len((run.path / "events.jsonl").read_text(encoding="utf-8").splitlines()), 2)

    def test_status_replace_transient_windows_lock(self):
        import os
        with tempfile.TemporaryDirectory(dir=Path.cwd() / ".codex_task_output") as directory:
            run = RunState(Path(directory), run_id="replace", dataset_version="ds1")
            original = os.replace
            attempts = []

            def transient(src, dst):
                attempts.append(1)
                if len(attempts) == 1:
                    raise PermissionError("temporary reader lock")
                original(src, dst)

            with patch("app.ml.run_state.os.replace", side_effect=transient):
                run.update(stage="prepare", event="PROGRESS", done=1, total=2)
            self.assertEqual(len(attempts), 2)
            self.assertEqual(json.loads((run.path / "status.json").read_text(encoding="utf-8"))["progress_percent"], 50)

    def test_offline_runner_writes_deterministic_forecasts(self):
        fixture = {
            "schema_version": "ml_phase1_fixture_v1", "company_id": 3, "dataset_version": "synthetic-v1",
            "outbound_rows": [outbound()],
            "document_rows": [], "vendor_rows": [], "product_rows": [{"company_id": 3, "Rd04_Physic_Cd": "0001", "Rd04_Add_Date": "20260101"}],
            "standard_rows": [], "code_rows": [], "saved_io_codes": ["501"],
            "date_from": START.isoformat(), "date_to": END.isoformat(),
            "complete_dates": [day.isoformat() for day in days()],
            "as_of_dates": [AS_OF.isoformat()], "timestamp_policy_confirmed": True,
            "calendar": {"start_date": START.replace(day=1).isoformat(), "end_date": END.isoformat(),
                         "holiday_dates": [], "known_as_of": "2026-01-01", "version": "synthetic-calendar"},
        }
        with tempfile.TemporaryDirectory(dir=Path.cwd() / ".codex_task_output") as directory:
            root = Path(directory)
            source = root / "fixture.json"
            source.write_text(json.dumps(fixture), encoding="utf-8")
            first = run_fixture(source, root, run_id="first", progress_every=1)
            second = run_fixture(source, root, run_id="second", progress_every=1)
            self.assertEqual(first["models"], second["models"])
            first_predictions = json.loads((root / "first" / "predictions.json").read_text(encoding="utf-8"))
            second_predictions = json.loads((root / "second" / "predictions.json").read_text(encoding="utf-8"))
            self.assertEqual(first_predictions, second_predictions)
            self.assertEqual({row["method"] for row in first_predictions}, {"moving_average", "ewma"})
            self.assertEqual(json.loads((root / "first" / "status.json").read_text(encoding="utf-8"))["event"], "DONE")
            events = [json.loads(line) for line in (root / "first" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(events), 8)
            self.assertEqual([(row["completed_count"], row["total_count"], row["progress_percent"])
                              for row in events if row["event"] == "PROGRESS"], [(1, 2, 50)])
            fixture["date_to"] = START.isoformat()
            source.write_text(json.dumps(fixture), encoding="utf-8")
            with self.assertRaises(ValueError):
                run_fixture(source, root, run_id="error")
            self.assertEqual(json.loads((root / "error" / "status.json").read_text(encoding="utf-8"))["event"], "ERROR")
            fixture["date_to"] = END.isoformat()
            source.write_text(json.dumps(fixture), encoding="utf-8")
            with self.assertRaises(RunCancelled):
                run_fixture(source, root, run_id="cancel", progress_every=1, cancel_check=lambda: True)
            cancelled = json.loads((root / "cancel" / "status.json").read_text(encoding="utf-8"))
            self.assertEqual((cancelled["event"], cancelled["completed_count"], cancelled["total_count"]),
                             ("CANCEL", 1, 2))


if __name__ == "__main__":
    with patch("app.db.mssql_client.pyodbc.connect", side_effect=AssertionError("DB access forbidden")):
        unittest.main(verbosity=2)
