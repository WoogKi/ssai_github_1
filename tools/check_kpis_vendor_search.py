"""DB-free vendor selection and MA102 display regression."""
import copy
import csv
import json
import socket
import unittest
from dataclasses import replace
from io import BytesIO, StringIO
from unittest.mock import Mock, patch

import httpx
import pandas as pd
from openpyxl import load_workbook

from app.services import rddbc030_service as vendors
from app.services.external_api_management import ApiManagementError
from app.ui import kpis_erp_query as ui, chat_middleware as chat, sims_table_display as display
from app.ui import kpis_vendor_combobox as combo
from tools.check_kpis_query_ui import QueryUI


class VendorSearch(unittest.TestCase):
    def setUp(self):
        self.fixture = QueryUI(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f
        guard = patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        self.rows = pd.DataFrame([
            ("50001", "합성거래처", "000-00-00001"),
            ("50002", "합성거래처", "0000000002"),
            ("50003", "번호없음", None),
        ], columns=["ERP 거래처코드", "ERP 거래처명", "사업자번호"])
        renderer = patch.object(combo, "render_vendor_combobox", side_effect=self.component_adapter)
        renderer.start()
        self.addCleanup(renderer.stop)

    def component_adapter(self, key, data, on_event):
        # AppTest cannot execute component JS. This adapter tests Python event handling;
        # the real component's input/IME/debounce is exercised separately in Chromium.
        import streamlit as st
        def emit(kind):
            st.session_state[key] = {"event": {"id": data["ack"] + 1, "kind": kind,
                "term": st.session_state.get(key + "_test_term", ""),
                "index": st.session_state.get(key + "_test_pick")}}
            on_event(st.session_state[key]["event"])
        st.text_input("거래처명 또는 ERP 거래처코드", key=key + "_test_term", on_change=emit, args=("search",))
        if st.session_state.get(key + "_test_last") != data["selected"]:
            st.session_state[key + "_test_pick"] = data["selected"]
        st.session_state[key + "_test_last"] = data["selected"]
        st.selectbox("ERP 거래처 선택", list(range(len(data["options"]))), index=None,
            format_func=lambda i: data["options"][i], key=key + "_test_pick", disabled=not data["options"],
            on_change=emit, args=("select",))
        if "200개" in data["message"]:
            st.warning(data["message"])
        return None

    def element(self, page, kind, label):
        return next(v for v in getattr(page, kind) if v.label == label)

    def search(self, page, term="합성"):
        self.assertFalse(page.radio)
        self.assertNotIn("거래처 검색", [v.label for v in page.button])
        self.assertNotIn("ERP 거래처 범위", [v.label for v in page.selectbox])
        self.element(page, "text_input", "거래처명 또는 ERP 거래처코드").set_value(term).run()

    def test_select_explicit_tkr_only_both_services_single_publish_rerun(self):
        for service in ("MA102", "MA110"):
            with self.subTest(service=service), patch.object(vendors, "search_vendor_candidates", return_value=self.rows) as search:
                with self.fixture.screen(service=service) as (page, factory, push):
                    self.search(page)
                    search.assert_called_once_with(8, scope="", ven_nm_kw="합성")
                    picker = self.element(page, "selectbox", "ERP 거래처 선택")
                    self.assertIsNone(picker.value)
                    self.assertEqual(len(picker.options), 3)
                    self.assertIn("50002", picker.options[1])
                    self.assertIn("0000000002", picker.options[1])
                    self.assertEqual(self.element(page, "text_input", "거래처 사업자번호").value, "")
                    factory.assert_not_called()
                    picker.set_value(0).run()
                    factory.assert_not_called()
                    self.element(page, "button", "KPIS 조회").click().run()
                    self.assertFalse(page.exception)
                    self.assertFalse(page.error)
                    body = json.loads(self.f.sent[-1].content)
                    self.assertEqual(body["tkrHbin"], "0000000001")
                    self.assertEqual(body["aplHbin"], self.f.entry["applicant"])
                    self.assertEqual(body["splyHbin"], self.f.entry["applicant"])
                    self.assertFalse(body.get("tkrpNm"))
                    self.assertFalse(body.get("ykiho"))
                    if service == "MA110":
                        self.assertIsNone(body["splyTpCd"])
                    page.run()
                    search.assert_called_once()
                    factory.assert_called_once()
                    push.assert_called_once()

    def test_missing_invalid_numbers_and_search_changes(self):
        with patch.object(vendors, "search_vendor_candidates", return_value=self.rows) as search:
            with self.fixture.screen(service="MA110") as (page, factory, push):
                self.search(page, "50001")
                search.assert_called_once_with(8, scope="", ven_cd="50001")
                self.element(page, "selectbox", "ERP 거래처 선택").set_value(2).run()
                self.assertTrue(self.element(page, "button", "KPIS 조회").disabled)
                self.assertIn("사업자번호가 없습니다", page.warning[0].value)
                self.rows.loc[0, "사업자번호"] = "invalid"
                self.search(page, "변경")
                self.assertIsNone(self.element(page, "selectbox", "ERP 거래처 선택").value)
                self.assertEqual(self.element(page, "text_input", "거래처 사업자번호").value, "")
                self.element(page, "selectbox", "ERP 거래처 선택").set_value(0).run()
                self.element(page, "button", "KPIS 조회").click().run()
                self.assertIn("숫자 10자리", page.error[0].value)
                factory.assert_not_called()
                push.assert_not_called()

    def test_term_change_and_partial_results(self):
        self.rows.attrs["kpis_candidates_partial"] = True
        with patch.object(vendors, "search_vendor_candidates", return_value=self.rows) as search:
            with self.fixture.screen(service="MA102") as (page, factory, _):
                self.search(page, "5A001")
                search.assert_called_once_with(8, scope="", ven_cd="5A001")
                self.assertIn("200개", page.warning[0].value)
                self.element(page, "selectbox", "ERP 거래처 선택").set_value(1).run()
                self.search(page, "변경")
                self.assertEqual(search.call_count, 2)
                self.assertEqual(search.call_args.kwargs, {"scope": "", "ven_nm_kw": "변경"})
                self.assertEqual(self.element(page, "text_input", "거래처 사업자번호").value, "")
                self.assertIsNone(self.element(page, "selectbox", "ERP 거래처 선택").value)
                factory.assert_not_called()

    def test_empty_search_failure_and_no_leaking_exception(self):
        for value in (self.rows.iloc[:0], RuntimeError("private-vendor-details")):
            with patch.object(vendors, "search_vendor_candidates", side_effect=[value]) as search:
                with self.fixture.screen(service="MA102") as (page, factory, _):
                    self.search(page)
                    self.assertTrue(self.element(page, "selectbox", "ERP 거래처 선택").disabled)
                    self.assertNotIn("private-vendor-details", str(page.error))
                    page.run()
                    search.assert_called_once()
                    factory.assert_not_called()

    def test_committed_term_only_empty_and_unchanged_reruns_never_search(self):
        for service in ("MA102", "MA110"):
            with self.subTest(service=service), patch.object(vendors, "search_vendor_candidates", return_value=self.rows) as search:
                with self.fixture.screen(service=service) as (page, factory, push):
                    self.search(page, "   ")
                    page.run()
                    search.assert_not_called()
                    self.search(page, "합성")
                    self.search(page, " 합성 ")
                    self.element(page, "selectbox", "ERP 거래처 선택").set_value(0).run()
                    self.element(page, "text_input", "거래처 사업자번호").set_value("0000000009").run()
                    page.run()
                    search.assert_called_once()
                    self.search(page, "")
                    self.assertTrue(self.element(page, "selectbox", "ERP 거래처 선택").disabled)
                    self.assertEqual(self.element(page, "text_input", "거래처 사업자번호").value, "0000000009")
                    search.assert_called_once()
                    factory.assert_not_called()
                    push.assert_not_called()

    def test_failed_new_search_discards_old_candidates_and_auto_number(self):
        with patch.object(vendors, "search_vendor_candidates", side_effect=[self.rows, RuntimeError("private")]) as search:
            with self.fixture.screen(service="MA102") as (page, factory, _):
                self.search(page)
                self.element(page, "selectbox", "ERP 거래처 선택").set_value(0).run()
                self.search(page, "변경")
                self.assertTrue(page.error)
                self.assertTrue(self.element(page, "selectbox", "ERP 거래처 선택").disabled)
                self.assertEqual(self.element(page, "text_input", "거래처 사업자번호").value, "")
                page.run()
                self.assertEqual(search.call_count, 2)
                factory.assert_not_called()

    def test_200_candidates_are_explicit_and_distinct(self):
        frame = pd.DataFrame([(f"{50000+i}", "동일명칭", f"{i:010d}") for i in range(200)], columns=self.rows.columns)
        frame.attrs["kpis_candidates_partial"] = True
        with patch.object(vendors, "search_vendor_candidates", return_value=frame) as search:
            with self.fixture.screen(service="MA110") as (page, factory, _):
                self.search(page)
                picker = self.element(page, "selectbox", "ERP 거래처 선택")
                self.assertEqual(len(set(picker.options)), 200)
                self.assertIsNone(picker.value)
                picker.set_value(199).run()
                self.assertEqual(self.element(page, "text_input", "거래처 사업자번호").value, "0000000199")
                self.assertTrue(any("200개" in w.value for w in page.warning))
                search.assert_called_once()
                factory.assert_not_called()

    def test_scope_change_logout_and_revoke_during_search(self):
        with patch.object(vendors, "search_vendor_candidates", return_value=self.rows):
            with self.fixture.screen(service="MA102") as (page, factory, _):
                self.search(page)
                self.element(page, "selectbox", "ERP 거래처 선택").set_value(0).run()
                self.f.actor = replace(self.f.actor, epoch=self.f.actor.epoch + 1)
                page.run()
                self.assertTrue(self.element(page, "selectbox", "ERP 거래처 선택").disabled)
                self.f.actor = None
                page.run()
                self.assertFalse(page.selectbox)
                factory.assert_not_called()

    def test_company_switch_clears_candidates_without_querying(self):
        from app.services.ssai_login_profile_service import ERP_IDENTITY_KEY
        from app.ui import ssai_login as login
        with patch.object(vendors, "search_vendor_candidates", return_value=self.rows) as search:
            with self.fixture.screen(service="MA110") as (page, factory, _):
                self.search(page)
                self.element(page, "selectbox", "ERP 거래처 선택").set_value(0).run()
                self.f.actor = replace(self.f.actor, company_id=9, epoch=12346)
                self.f.state[login.SESSION_COMPANY] = {"company_id": 9, "is_active": True}
                self.f.state[ERP_IDENTITY_KEY] = replace(self.f.identity, company_id=9)
                page.run()
                self.assertFalse(page.error)
                self.assertTrue(self.element(page, "selectbox", "ERP 거래처 선택").disabled)
                search.assert_called_once()
                factory.assert_not_called()

    def test_denied_company_prevents_vendor_search(self):
        with patch.object(vendors, "search_vendor_candidates", return_value=self.rows) as search:
            with self.fixture.screen(service="MA102") as (page, factory, _):
                self.element(page, "text_input", "거래처명 또는 ERP 거래처코드").set_value("합성")
                self.f.company["is_active"] = False
                page.run()
                self.assertTrue(page.error)
                search.assert_not_called()
                factory.assert_not_called()

    def test_revoke_after_db_discards_candidates(self):
        def revoke(*args, **kwargs):
            self.f.actor = replace(self.f.actor, epoch=self.f.actor.epoch + 1)
            return self.rows
        with patch.object(vendors, "search_vendor_candidates", side_effect=revoke):
            with self.fixture.screen(service="MA102") as (page, factory, _):
                self.search(page)
                self.assertTrue(self.element(page, "selectbox", "ERP 거래처 선택").disabled)
                factory.assert_not_called()

    def test_edit_detaches_selection_and_survives_search_and_submit(self):
        for service in ("MA102", "MA110"):
            with self.subTest(service=service), patch.object(vendors, "search_vendor_candidates", return_value=self.rows) as search:
                with self.fixture.screen(service=service) as (page, factory, push):
                    self.search(page)
                    self.element(page, "selectbox", "ERP 거래처 선택").set_value(0).run()
                    self.assertEqual(self.element(page, "text_input", "거래처 사업자번호").value, "000-00-00001")
                    self.element(page, "text_input", "거래처 사업자번호").set_value("000-00-00009").run()
                    self.assertIsNone(self.element(page, "selectbox", "ERP 거래처 선택").value)
                    page.run()
                    self.assertEqual(self.element(page, "text_input", "거래처 사업자번호").value, "000-00-00009")
                    self.element(page, "text_input", "거래처명 또는 ERP 거래처코드").set_value("변경").run()
                    self.assertEqual(self.element(page, "text_input", "거래처 사업자번호").value, "000-00-00009")
                    self.element(page, "button", "KPIS 조회").click().run()
                    self.assertFalse(page.exception)
                    self.assertFalse(page.error)
                    self.assertEqual(json.loads(self.f.sent[-1].content)["tkrHbin"], "0000000009")
                    page.run()
                    self.assertEqual(search.call_count, 2)
                    factory.assert_called_once()
                    push.assert_called_once()

    def test_new_selection_replaces_manual_number_and_research_clears_autofill(self):
        with patch.object(vendors, "search_vendor_candidates", return_value=self.rows) as search:
            with self.fixture.screen(service="MA102") as (page, factory, _):
                self.element(page, "text_input", "거래처 사업자번호").set_value("0000000009").run()
                self.search(page)
                self.assertEqual(self.element(page, "text_input", "거래처 사업자번호").value, "0000000009")
                self.element(page, "selectbox", "ERP 거래처 선택").set_value(0).run()
                self.element(page, "selectbox", "ERP 거래처 선택").set_value(1).run()
                self.assertEqual(self.element(page, "text_input", "거래처 사업자번호").value, "0000000002")
                self.search(page, "변경")
                self.assertIsNone(self.element(page, "selectbox", "ERP 거래처 선택").value)
                self.assertEqual(self.element(page, "text_input", "거래처 사업자번호").value, "")
                page.run()
                self.assertEqual(search.call_count, 2)
                factory.assert_not_called()

    def test_edit_then_submit_same_event_and_clear_never_restores_old_number(self):
        with patch.object(vendors, "search_vendor_candidates", return_value=self.rows):
            with self.fixture.screen(service="MA102") as (page, factory, push):
                self.search(page)
                self.element(page, "selectbox", "ERP 거래처 선택").set_value(0).run()
                self.element(page, "text_input", "거래처 사업자번호").set_value("0000000008")
                self.element(page, "button", "KPIS 조회").click().run()
                self.assertFalse(page.exception)
                self.assertFalse(page.error)
                self.assertEqual(json.loads(self.f.sent[-1].content)["tkrHbin"], "0000000008")
                self.element(page, "text_input", "거래처 사업자번호").set_value("")
                self.element(page, "button", "KPIS 조회").click().run()
                self.assertIn("필수", page.error[0].value)
                self.assertEqual(self.element(page, "text_input", "거래처 사업자번호").value, "")
                factory.assert_called_once()
                push.assert_called_once()

    def test_blank_form_contract_and_type5_fail_type6_success(self):
        for value in ("", None, " "):
            query = {"msupSplyFrmCd": value, "tkrHbin": "0000000001"}
            with self.assertRaises(ApiManagementError):
                ui.validate_query("MA102", query)
        with self.assertRaises(ApiManagementError):
            ui.validate_query("MA102", {"tkrHbin": "0000000001"})
        self.assertEqual(set(ui.CODE_OPTIONS["msupSplyFrmCd"]), {"5", "6", "8", "9"})
        for code, result_code in (("5", "fail"), ("6", "success")):
            result = self.f.call(request=replace(self.f.req, service="MA102"),
                query={"msupSplyFrmCd": code, "tkrHbin": "0000000001"},
                handler=lambda req: httpx.Response(200, json={"result": result_code,
                    "msupBscqList": [{"bscqRegYn": "Y", "bscqNm": "", "cloDd": ""}]}))
            self.assertEqual(result["status"], "DENIED" if code == "5" else "SUCCESS")
            if code == "5":
                self.assertEqual(result["reason_code"], "provider_rejected")
            self.assertEqual(result["physical_call_count"], 1)
            self.assertEqual(result["retry_count"], 0)

    def test_ma102_labels_date_raw_codes_and_full_download(self):
        rows = [{"bscqRegYn": "N", "bscqNm": "", "cloDd": "20261125"},
                {"bscqRegYn": "Y", "bscqNm": None, "cloDd": None}]
        original = copy.deepcopy(rows)
        result = self.f.call(request=replace(self.f.req, service="MA102"),
            query={"msupSplyFrmCd": "6", "tkrHbin": "0000000001"},
            handler=lambda req: httpx.Response(200, json={"result": "success", "msupBscqList": rows}))
        push = Mock(return_value={})
        self.assertTrue(ui.publish_result(result, {}, (8, "42", 12345), load_session=lambda: self.f.actor, push=push))
        frame = push.call_args.args[0]["df"]
        self.assertEqual(list(frame), ["거래처등록여부", "거래처명칭", "폐기일자"])
        view, _, _, _ = display.build_sims_table_display_config(frame, add_row_no=False)
        self.assertEqual(view["폐기일자"].tolist(), ["2026-11-25", ""])
        self.assertEqual(frame["거래처등록여부"].tolist(), ["N", "Y"])
        self.assertEqual(result["rows"], original)
        csv_file, excel_file = chat._make_table_downloads(frame)
        csv_rows = list(csv.reader(StringIO(csv_file.getvalue().decode("utf-8-sig"))))
        self.assertEqual(csv_rows[0], list(frame))
        self.assertEqual(len(csv_rows), 3)
        book = load_workbook(BytesIO(excel_file.getvalue()), read_only=True)
        try:
            self.assertEqual(list(book.active.values)[0], tuple(frame.columns))
            self.assertEqual(book.active.max_row, 3)
        finally:
            book.close()


class VendorSql(unittest.TestCase):
    def test_scoped_exact_name_limit_and_cleanup(self):
        for condition, expected in (({"ven_cd": "5A001"}, ("5A001",)),
                                    ({"ven_nm_kw": "합성"}, ("%합성%",))):
            conn, cursor = Mock(), Mock()
            conn.cursor.return_value = cursor
            cursor.fetchmany.return_value = [(f"{n:05d}", "합성", "0000000001") for n in range(201)]
            with patch("app.services.ssai_auth_service.connect_company_db", return_value=conn) as connect:
                frame = vendors.search_vendor_candidates(8, scope="sales", **condition)
            connect.assert_called_once_with(8)
            self.assertEqual(conn.timeout, 10)
            sql, *params = cursor.execute.call_args.args
            self.assertEqual(tuple(params), expected)
            self.assertIn("TOP (201)", sql)
            self.assertIn("BETWEEN '50000' AND '8ZZZZ'", sql)
            self.assertIn("NOT IN ('1', 'Y', 'E')", sql)
            self.assertNotIn("JOIN", sql)
            self.assertNotIn("합성", sql)
            self.assertEqual(len(frame), 200)
            self.assertTrue(frame.attrs["kpis_candidates_partial"])
            cursor.fetchmany.assert_called_once_with(201)
            cursor.close.assert_called_once()
            conn.close.assert_called_once()

    def test_invalid_search_before_connection_and_failed_query_cleanup(self):
        with patch("app.services.ssai_auth_service.connect_company_db") as connect:
            for company, kwargs in ((0, {"ven_cd": "50001"}), (8, {}), (8, {"ven_nm_kw": "%"}),
                    (8, {"ven_cd": "50001", "ven_nm_kw": "합성"}), (8, {"ven_cd": "50001", "scope": "unknown"})):
                with self.assertRaises(ValueError):
                    vendors.search_vendor_candidates(company, **kwargs)
            connect.assert_not_called()
            conn = connect.return_value
            cursor = conn.cursor.return_value
            cursor.execute.side_effect = RuntimeError("synthetic")
            with self.assertRaises(RuntimeError):
                vendors.search_vendor_candidates(8, ven_cd="50001")
            cursor.close.assert_called_once()
            conn.close.assert_called_once()

    def test_existing_search_rows_filter_contract_unchanged(self):
        with patch.object(vendors, "_run_df", return_value=pd.DataFrame()) as run:
            vendors.search_rows(scope="sales_history", ven_cd="50001", ven_nm_kw="합성", only_active=False, top=30)
        _, sql, params = run.call_args.args
        self.assertIn("BETWEEN '50000' AND '9ZZZZ'", sql)
        self.assertNotIn("NOT IN ('1', 'Y', 'E')", sql)
        self.assertEqual(params, ("50001", "%합성%"))


if __name__ == "__main__":
    unittest.main()
