"""DB-free integrated KPIS projections, bounded mappings and explicit UI execution."""
import copy
import csv
import json
import socket
import tracemalloc
import unittest
from dataclasses import replace
from datetime import date
from io import BytesIO, StringIO
from unittest.mock import Mock, patch

import httpx
import pandas as pd
from openpyxl import load_workbook

from app.services import rddbc040_service as goods
from app.services.business_calendar_service import recent_business_days, RecentBusinessDaysResult
from app.services.ssai_business_calendar_repository import CalendarAuthorityRead
from app.services.external_api_management import ApiManagementError
from app.services.kpis_rest_client import OUTPUTS, validate_query, response_row_limit
from app.ui import kpis_erp_query as ui, chat_middleware as chat
from app.ui.kpis_result_display import with_product_mapping
from tools.check_kpis_query_ui import QueryUI


class Integrated(unittest.TestCase):
    def setUp(self):
        self.fixture = QueryUI(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f
        self.scope = (8, "42", 12345)
        guard = patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def response(self, service, rows, *, extra=None):
        query = {"rpayCmmStaDd": "20261025", "rpayCmmEndDd": "20261125"} if service == "MA109" else {"splyDd": "20261124"}
        data = {"result": "success", OUTPUTS[service][0]: rows, **(extra or {})}
        return self.f.call(request=replace(self.f.req, service=service), query=query,
                           handler=lambda req: httpx.Response(200, json=data))

    def test_defaults_month_end_and_common_holidays(self):
        self.assertEqual(ui._initial_dates("MA117", date(2026, 11, 25)),
                         {"srchStaDd": date(2026, 11, 18), "srchEndDd": date(2026, 11, 25)})
        self.assertEqual(ui._initial_dates("MA109", date(2026, 11, 25))["rpayCmmStaDd"], date(2026, 10, 25))
        self.assertEqual(ui._initial_dates("MA109", date(2024, 3, 31))["rpayCmmStaDd"], date(2024, 2, 29))
        self.assertEqual(ui._initial_dates("MA109", date(2026, 1, 31))["rpayCmmStaDd"], date(2025, 12, 31))
        authority = CalendarAuthorityRead(status="ready", holiday_dates=frozenset({"20261120"}))
        result = recent_business_days(base_date=date(2026, 11, 23), count=1, include_base=False,
                                      calendar_loader=Mock(return_value=authority))
        self.assertEqual(result.dates, ("20261119",))
        with patch.object(ui, "recent_business_days", return_value=result):
            self.assertEqual(ui._initial_dates("MA110", date(2026, 11, 23))["splyDd"], date(2026, 11, 19))
        with patch.object(ui, "recent_business_days", return_value=RecentBusinessDaysResult(status="unavailable")):
            self.assertIsNone(ui._initial_dates("MA110", date(2026, 11, 23))["splyDd"])

    def test_date_defaults_edit_persistence_no_auto_api(self):
        for service, expected in (("MA117", [date(2026, 11, 18), date(2026, 11, 25)]),
                                  ("MA109", [date(2026, 10, 25), date(2026, 11, 25)]),
                                  ("MA110", [date(2026, 11, 24)])):
            with self.subTest(service=service), self.fixture.screen(service=service) as (page, factory, push):
                self.assertEqual([v.value for v in page.date_input], expected)
                page.date_input[0].set_value("2026-11-01").run()
                page.run()
                self.assertEqual(page.date_input[0].value, date(2026, 11, 1))
                factory.assert_not_called()
                push.assert_not_called()

    def test_number_forms_identical_wire_not_mutated_and_invalid_blocked(self):
        for service in ("MA102", "MA110"):
            base = {"msupSplyFrmCd": "5"} if service == "MA102" else {"splyDd": "20261124"}
            for number in ("0000000001", "000-00-00001"):
                query = dict(base, tkrHbin=number)
                original = dict(query)
                checked = validate_query(service, query)
                self.assertEqual(checked["tkrHbin"], "0000000001")
                self.assertEqual(query, original)
                result = self.f.call(request=replace(self.f.req, service=service), query=query)
                self.assertEqual(result["status"], "SUCCESS")
                body = json.loads(self.f.sent[-1].content)
                self.assertEqual(body["tkrHbin"], "0000000001")
                if service == "MA110":
                    self.assertIsNone(body["splyTpCd"])
            for number in ("000-000-0001", "000a000001", "１" * 10, "123", " 0000000001"):
                with self.assertRaises(ApiManagementError):
                    validate_query(service, dict(base, tkrHbin=number))

    def test_hyphen_ui_fits_and_submits_once(self):
        for service in ("MA102", "MA110"):
            with self.fixture.screen(service=service) as (page, factory, push):
                field = next(v for v in page.text_input if v.label == "거래처 사업자번호")
                self.assertEqual(field.proto.max_chars, 12)
                field.set_value("000-00-00001")
                next(v for v in page.button if v.label == "KPIS 조회").click().run()
                self.assertFalse(page.exception)
                self.assertFalse(page.error)
                self.assertEqual(json.loads(self.f.sent[-1].content)["tkrHbin"], "0000000001")
                page.run()
                factory.assert_called_once()
                push.assert_called_once()

    def test_unified_search_labels_auto_all_codes_and_exclusion_persist(self):
        frame = pd.DataFrame([
            ("00001", "합성제품", "0000000000001", "10mg", "합성제약"),
            ("00001", "합성제품", "0000000000002", "10mg", "합성제약"),
            ("A0002", "다른제품", "0000000000003", "20mg", "다른제약"),
        ], columns=goods.STANDARD_PRODUCT_COLUMNS)
        for term, kwargs in (("00001", {"physic_cd": "00001"}), ("A0002", {"physic_cd": "A0002"}),
                             ("합성", {"keyword": "합성"}), ("123", {"keyword": "123"})):
            with patch.object(goods, "search_standard_code_candidates", return_value=frame) as search:
                with self.fixture.screen(service="MA101") as (page, factory, _):
                    page.radio[0].set_value("ERP 제품 검색").run()
                    page.text_input[0].set_value(term).run()
                    next(b for b in page.button if b.label == "제품 검색").click().run()
                    search.assert_called_once_with(8, **kwargs)
                    self.assertIn("10mg / 합성제약", page.multiselect[0].options[0])
                    page.multiselect[0].set_value(["00001"]).run()
                    self.assertEqual(page.multiselect[1].value, ["0000000000001", "0000000000002"])
                    page.multiselect[1].set_value(["0000000000002"]).run()
                    page.run()
                    self.assertEqual(page.multiselect[1].value, ["0000000000002"])
                    page.multiselect[0].set_value(["00001", "A0002"]).run()
                    self.assertEqual(len(page.multiselect[1].value), 3)
                    page.text_input[0].set_value(term + "x").run()
                    self.assertFalse(page.multiselect)
                    factory.assert_not_called()

    def test_auto_codes_over_twenty_never_calls_until_user_excludes(self):
        frame = pd.DataFrame([("00001", "합성", f"{n:013d}", "1mg", "제약") for n in range(21)],
                             columns=goods.STANDARD_PRODUCT_COLUMNS)
        with patch.object(goods, "search_standard_code_candidates", return_value=frame):
            with self.fixture.screen(service="MA101") as (page, factory, _):
                page.radio[0].set_value("ERP 제품 검색").run()
                page.text_input[0].set_value("합성")
                next(b for b in page.button if b.label == "제품 검색").click().run()
                page.multiselect[0].set_value(["00001"]).run()
                self.assertEqual(len(page.multiselect[1].value), 21)
                next(b for b in page.button if b.label == "KPIS 조회").click().run()
                factory.assert_not_called()
                self.assertTrue(page.warning)

    def test_service_limits_all_rows_and_bytes(self):
        row = {name: "synthetic" for name in OUTPUTS["MA109"][1]}
        row["msupStdCd"] = "0000000000001"
        for count in (8, 138, 1000, 2587, 5000):
            result = self.response("MA109", [row] * count)
            self.assertEqual(result["status"], "SUCCESS")
            self.assertEqual(len(result["rows"]), count)
            self.assertEqual(result["physical_call_count"], 1)
        result = self.response("MA109", [row] * 5001)
        self.assertEqual(result["reason_code"], "ma109_rows_limit")
        result = self.response("MA109", [row], extra={"padding": "x" * (2 * 1024 * 1024)})
        self.assertEqual(result["reason_code"], "response_limit")
        for service in ("MA117", "MA101", "MA102", "MA110"):
            self.assertEqual(response_row_limit(service), 1000)
        result = self.response("MA110", [{"rcvCnt": "1"}] * 1001)
        self.assertEqual(result["reason_code"], "ma110_rows_limit")

    def test_2587_full_export_and_current_table_no_row_expansion(self):
        rows = [{name: ("0000000000001" if name == "msupStdCd" else "synthetic")
                 for name in OUTPUTS["MA109"][1]} for _ in range(2587)]
        tracemalloc.start()
        result = self.response("MA109", rows)
        before = copy.deepcopy(result["rows"])
        mappings = pd.DataFrame([("00001", "약", "0000000000001", "10mg", "제약", 2),
                                 ("00002", "약", "0000000000001", "20mg", "제약2", 1)],
                                columns=goods.STANDARD_PRODUCT_COLUMNS + ["연결원천건수"])
        result.update(erp_mapping=mappings, erp_mapping_status="ready")
        push = Mock(return_value={})
        self.assertTrue(ui.publish_result(result, {}, self.scope, load_session=lambda: self.f.actor, push=push))
        payload = chat._normalize_result_for_chat(push.call_args.args[0])
        with patch.object(chat, "_chat_display_max_rows", return_value=300):
            chat._apply_chat_display_limit_to_payload(payload)
        self.assertEqual(len(payload["df_display"]), 300)
        self.assertEqual(len(payload["df"]), 2587)
        self.assertTrue((payload["df"]["ERP 매핑 건수"] == 2).all())
        csv_file, excel_file = chat._make_table_downloads(payload["df"])
        exported = list(csv.DictReader(StringIO(csv_file.getvalue().decode("utf-8-sig"))))
        self.assertEqual(len(exported), 2587)
        self.assertEqual(exported[-1]["표준코드"], "0000000000001")
        book = load_workbook(BytesIO(excel_file.getvalue()), read_only=True)
        try:
            self.assertEqual(book.active.max_row, 2588)
            self.assertEqual(book.active.max_column, 18)
        finally:
            book.close()
        self.assertEqual(result["rows"], before)
        with patch.object(chat, "drain_inbox_to_chat"), patch.object(chat, "_append_finalized_nlq_case"), patch.object(
                chat, "maybe_create_structured_presentation", return_value={"status": "skipped"}):
            self.assertTrue(ui.publish_result(result, {}, self.scope, load_session=lambda: self.f.actor, push=chat.wssz))
        key = self.f.state["__sims_current_table_source_key"]
        self.assertEqual(len(self.f.state["sims_export_tables"][key]), 2587)
        self.assertEqual(len(self.f.state["__chat_inbox"]), 1)
        self.assertEqual(list(self.f.state["sims_export_tables"][key])[:10], list(ui.RECALL_COLUMNS.values()))
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print(f"SYNTHETIC_2587_EXPORT_PEAK_BYTES={peak}")

    def test_mapping_projection_duplicates_missing_and_invalid(self):
        frame = pd.DataFrame({"표준코드": ["0000000000001", "0000000000001", "0000000000002", "bad"],
                              "금액": [10, 20, 30, 40]})
        mapping = pd.DataFrame([("00001", "동명", "0000000000001", "1mg", "제약1", 3),
                                ("00002", "동명", "0000000000001", "2mg", "제약2", 1)],
                               columns=goods.STANDARD_PRODUCT_COLUMNS + ["연결원천건수"])
        result = with_product_mapping(frame, mapping, "ready")
        self.assertEqual(len(result), 4)
        self.assertEqual(result["금액"].sum(), 100)
        self.assertEqual(result["ERP 매핑 건수"].iloc[0], 2)
        self.assertEqual(result["ERP 이력중복 건수"].iloc[0], 2)
        self.assertIn("00002 / 동명 / 2mg / 제약2", result["ERP 매핑 상세"].iloc[0])
        self.assertEqual(result["ERP 매핑 상태"].iloc[2], "매핑 없음")
        self.assertEqual(result["ERP 매핑 상태"].iloc[3], "표준코드 형식 확인")
        failed = with_product_mapping(frame, pd.DataFrame(), "unavailable")
        self.assertTrue((failed["ERP 매핑 상태"] == "ERP 매핑 조회 실패").all())

    def test_mapping_cell_limit_not_silent_truncation_and_single_product(self):
        frame = pd.DataFrame({"표준코드": ["0000000000001"]})
        mapping = pd.DataFrame([("00001", "합성", "0000000000001", "1mg", "제약", 1)],
                               columns=goods.STANDARD_PRODUCT_COLUMNS + ["연결원천건수"])
        result = with_product_mapping(frame, mapping, "ready")
        self.assertEqual(result["ERP 매핑 상태"].iloc[0], "단일 제품")
        mapping.loc[0, "제품명"] = "x" * 30001
        result = with_product_mapping(frame, mapping, "ready")
        self.assertEqual(result["ERP 매핑 상태"].iloc[0], "매핑 표시 상한 초과")
        self.assertEqual(result["표준코드"].iloc[0], "0000000000001")
        self.assertEqual(result["ERP 제품명"].iloc[0], "")

    def test_calendar_unavailable_manual_date_retained(self):
        with patch.object(ui, "recent_business_days", return_value=RecentBusinessDaysResult(status="unavailable")) as calendar:
            with self.fixture.screen(service="MA110") as (page, factory, _):
                self.assertIsNone(page.date_input[0].value)
                self.assertTrue(page.info)
                page.date_input[0].set_value("2026-11-20").run()
                page.run()
                self.assertEqual(page.date_input[0].value, date(2026, 11, 20))
                calendar.assert_called_once()
                factory.assert_not_called()

    def test_repeated_mapping_export_budget_preserves_original_rows(self):
        frame = pd.DataFrame({"표준코드": ["0000000000001"] * 2000})
        mapping = pd.DataFrame([("00001", "x" * 5000, "0000000000001", "1mg", "제약", 1)],
                               columns=goods.STANDARD_PRODUCT_COLUMNS + ["연결원천건수"])
        result = with_product_mapping(frame, mapping, "ready")
        self.assertEqual(len(result), 2000)
        self.assertTrue((result["ERP 매핑 상태"] == "매핑 표시 전체 상한 초과").all())
        self.assertTrue((result["표준코드"] == frame["표준코드"]).all())

    def test_clearing_calendar_does_not_claim_authority_unavailable(self):
        with self.fixture.screen(service="MA110") as (page, factory, _):
            page.date_input[0].set_value(None).run()
            page.run()
            self.assertIsNone(page.date_input[0].value)
            self.assertFalse(page.info)
            factory.assert_not_called()

    def test_mapping_pipeline_rechecks_scope_and_never_requeries_api(self):
        client = Mock()
        client.call.return_value = dict(status="SUCCESS", service="MA109", company_id=8,
            rows=[{"msupStdCd": "0000000000001"}] * 2587, physical_call_count=1)
        with patch.object(ui, "load_current_api_session", side_effect=lambda: self.f.actor), patch.object(
                ui, "configured_direct_client", return_value=client), patch.object(
                goods, "lookup_standard_code_products", return_value=pd.DataFrame()) as lookup:
            result = ui._call_selected("MA109", [{"rpayCmmYr": "2026"}], self.scope)
            self.assertEqual(len(result["rows"]), 2587)
            lookup.assert_called_once()
            self.assertEqual(lookup.call_args.args, (8, {"0000000000001"}))
            client.call.assert_called_once()
            def revoke(*args, **kwargs):
                self.f.actor = replace(self.f.actor, epoch=99)
                kwargs["check_scope"]()
            lookup.side_effect = revoke
            with self.assertRaises(ApiManagementError):
                ui._call_selected("MA109", [{"rpayCmmYr": "2026"}], self.scope)


class MappingSql(unittest.TestCase):
    def test_company_batch_bound_parameters_history_count_and_cleanup(self):
        codes = [f"{i:013d}" for i in range(1001)]
        conn, cur = Mock(), Mock()
        conn.cursor.return_value = cur
        cur.fetchmany.return_value = []
        check = Mock()
        # Use the actual service, not the UI fixture's lookup stub.
        with patch("app.services.ssai_auth_service.connect_company_db", return_value=conn) as connect:
            result = goods.lookup_standard_code_products(8, codes + codes, check_scope=check)
        connect.assert_called_once_with(8)
        self.assertEqual(cur.execute.call_count, 3)
        self.assertEqual([len(c.args) - 1 for c in cur.execute.call_args_list], [500, 500, 1])
        sql = cur.execute.call_args_list[0].args[0]
        self.assertIn("COUNT_BIG(*)", sql)
        self.assertIn("P.Rd04_Ven_Cd = V.Rd03_Ven_Cd", sql)
        self.assertNotIn("Orven", sql)
        self.assertNotIn(codes[0], sql)
        self.assertEqual(check.call_count, 7)
        self.assertEqual(list(result), goods.STANDARD_PRODUCT_COLUMNS + ["연결원천건수"])
        cur.close.assert_called_once()
        conn.close.assert_called_once()

    def test_mapping_limit_and_revoke_never_partial_success(self):
        conn, cur = Mock(), Mock()
        conn.cursor.return_value = cur
        cur.fetchmany.return_value = [("00001", "name", "0000000000001", "spec", "maker", 1)] * 20001
        with patch("app.services.ssai_auth_service.connect_company_db", return_value=conn):
            with self.assertRaisesRegex(ValueError, "standard_mapping_limit"):
                goods.lookup_standard_code_products(8, ["0000000000001"], check_scope=Mock())
        self.assertEqual(cur.execute.call_count, 1)
        conn.close.assert_called_once()
        with patch("app.services.ssai_auth_service.connect_company_db") as connect:
            with self.assertRaises(ApiManagementError):
                goods.lookup_standard_code_products(8, ["0000000000001"],
                    check_scope=Mock(side_effect=ApiManagementError("authorization_changed")))
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
