"""Offline contract/format regression using synthetic records and MockTransport."""
import copy
import csv
from dataclasses import replace
from io import BytesIO, StringIO
import json
import socket
import unittest
from unittest.mock import Mock, patch

import httpx
import pandas as pd
from openpyxl import load_workbook

from app.services.external_api_management import ApiManagementError
from app.services.kpis_rest_client import OUTPUTS, validate_query
from app.ui import kpis_erp_query as ui, chat_middleware as chat, sims_table_display as display
from tools import check_kpis_query_ui as existing


class ContractAndDisplay(unittest.TestCase):
    def setUp(self):
        self.fixture = existing.QueryUI(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f
        guard = patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def response(self, service, query, data):
        return self.f.call(request=replace(self.f.req, service=service), query=query,
            handler=lambda req: httpx.Response(200, json=data))

    def test_ma102_invalid_recipient_and_institution_stop_before_key(self):
        for value, reason in ((None, "ma102_counterparty_required"), ("", "ma102_counterparty_required"),
                ("123-12-123", "invalid_counterparty_number"), ("12345678", "invalid_counterparty_number"),
                ("1" * 11, "invalid_counterparty_number"), ("１" * 10, "invalid_counterparty_number")):
            result = self.response("MA102", {"msupSplyFrmCd": "5", "tkrHbin": value}, {})
            self.assertEqual(result["reason_code"], reason)
            self.assertEqual(result["physical_call_count"], 0)
        with self.assertRaisesRegex(ApiManagementError, "invalid_institution_code"):
            validate_query("MA102", {"msupSplyFrmCd": "5", "tkrHbin": "0000000001", "ykiho": "123"})
        self.f.key.assert_not_called()

    def test_ma102_all_supply_forms_wire_roles_and_name_unchanged(self):
        for code in ("5", "6", "8", "9"):
            query = {"msupSplyFrmCd": code, "tkrHbin": "0000000001", "tkrpNm": "합성기관", "ykiho": "00000001"}
            result = self.f.call(request=replace(self.f.req, service="MA102"), query=query)
            self.assertEqual(result["status"], "SUCCESS")
            body = json.loads(self.f.sent[-1].content)
            self.assertEqual({k: body[k] for k in query}, query)
            self.assertEqual(body["aplHbin"], self.f.entry["applicant"])
            self.assertEqual(body["splyHbin"], self.f.entry["applicant"])
            self.assertNotEqual(body["tkrHbin"], body["aplHbin"])
            self.assertEqual(result["retry_count"], 0)

    def test_ma102_documented_rejection_and_unknown_message_non_disclosure(self):
        query = {"msupSplyFrmCd": "9", "tkrHbin": "0000000001"}
        for message, reason in (("공급받은자사업자등록번호 은/는 필수 조회 조건입니다.", "ma102_counterparty_required"),
                                ("secret-provider-details", "provider_rejected")):
            with self.assertLogs("ssai.kpis", level="INFO") as logs:
                result = self.response("MA102", query, {"result": "fail", "resultMessage": message,
                    "msupBscqList": None, "apiKey": "secret-provider-details"})
            self.assertEqual(result["reason_code"], reason)
            self.assertEqual(result["status"], "DENIED")
            self.assertEqual(result["physical_call_count"], 1)
            self.assertNotIn("secret-provider-details", str(logs.output) + str(result))
            self.assertEqual(self.f.events[-1]["result"], reason)
        self.assertIn("거부", ui.call_error_message("provider_rejected"))
        self.assertIn("필수", ui.call_error_message("ma102_counterparty_required"))

    def test_ma102_form_specific_errors_no_automatic_call(self):
        with self.fixture.screen(service="MA102") as (page, factory, push):
            next(v for v in page.button if v.label == "KPIS 조회").click().run(timeout=20)
            self.assertIn("사업자번호는 필수", page.error[0].value)
            next(v for v in page.text_input if v.label == "거래처 사업자번호").set_value("123-12-123")
            next(v for v in page.button if v.label == "KPIS 조회").click().run(timeout=20)
            self.assertIn("숫자 10자리", page.error[0].value)
            factory.assert_not_called()
            push.assert_not_called()
            next(v for v in page.text_input if v.label == "거래처 사업자번호").set_value("0000000001")
            next(v for v in page.button if v.label == "KPIS 조회").click().run(timeout=20)
            self.assertFalse(page.error)
            page.run(timeout=20)
            factory.assert_called_once()
            push.assert_called_once()

    def test_ma109_short_period_eight_rows_ten_fields(self):
        rows = [{name: "synthetic" for name in OUTPUTS["MA109"][1]} for _ in range(8)]
        query = {"rpayCmmStaDd": "20261123", "rpayCmmEndDd": "20261124"}
        with self.assertLogs("ssai.kpis", level="INFO") as logs:
            result = self.response("MA109", query, {"result": "success", "riskMsupList": rows})
        self.assertEqual(result["rows"], rows)
        self.assertEqual(result["physical_call_count"], 1)
        self.assertIn("list_length=8 over_limit=False", logs.output[0])
        push = Mock(return_value={})
        self.assertTrue(ui.publish_result(result, query, (8, "42", 12345), load_session=lambda: self.f.actor, push=push))
        self.assertEqual(push.call_args.args[0]["df"].shape, (8, 10))

    def test_ma109_shapes_diagnostics_and_safe_reasons(self):
        key = "riskMsupList"
        for payload, reason, kind in (({}, "ma109_rows_missing", "NoneType"),
                ({key: None}, "ma109_rows_null", "NoneType"), ({key: {}}, "ma109_rows_type", "dict"),
                ({key: "secret-payload"}, "ma109_rows_type", "str"),
                ({key: [{}] * 5001}, "ma109_rows_limit", "list")):
            with self.subTest(reason=reason), self.assertLogs("ssai.kpis", level="INFO") as logs:
                result = self.response("MA109", {"rpayCmmYr": "2026"},
                    {"result": "success", "apiKey": "secret-payload", **payload})
            self.assertEqual(result["reason_code"], reason)
            self.assertEqual(result["status"], "DENIED")
            self.assertEqual(result["rows"], [])
            self.assertEqual(result["physical_call_count"], 1)
            self.assertIn("list_type=" + kind, logs.output[0])
            self.assertNotIn("secret-payload", str(logs.output) + str(result))
            self.assertNotEqual(ui.call_error_message(reason), ui.call_error_message("unknown"))
            self.assertEqual(self.f.events[-1]["result"], reason)

    def test_ma109_boundary_empty_and_provider_denial(self):
        for count in (0, 1000):
            rows = [{"msupStdCd": "0000000000001"}] * count
            result = self.response("MA109", {"rpayCmmYr": "2026"}, {"result": "success", "riskMsupList": rows})
            self.assertEqual(result["status"], "SUCCESS")
            self.assertEqual(len(result["rows"]), count)
        result = self.response("MA109", {"rpayCmmYr": "2026"}, {"result": "fail", "riskMsupList": None})
        self.assertEqual(result["reason_code"], "provider_rejected")

    def test_ma109_two_mib_cap_still_precedes_row_validation(self):
        result = self.response("MA109", {"rpayCmmYr": "2026"},
            {"result": "success", "riskMsupList": [], "padding": "x" * (2 * 1024 * 1024)})
        self.assertEqual(result["reason_code"], "response_limit")
        self.assertEqual(result["physical_call_count"], 1)
        self.assertEqual(result["retry_count"], 0)

    def test_ma110_429_rows_labels_raw_values_and_downloads(self):
        rows = [{"msupSplyAmt": str(i * 10001), "msupSplyQty": str(i), "rcvCnt": str(i + 1)} for i in range(429)]
        original = copy.deepcopy(rows)
        result = self.response("MA110", {"splyDd": "20261123"},
            {"result": "success", "msupBriefSummary01List": rows})
        push = Mock(return_value={})
        self.assertTrue(ui.publish_result(result, {"splyDd": "20261123"}, (8, "42", 12345),
                                         load_session=lambda: self.f.actor, push=push))
        payload = push.call_args.args[0]
        frame = payload["df"]
        self.assertEqual(list(frame), ["공급금액", "공급수량", "접수건수"])
        self.assertEqual(result["rows"], original)
        self.assertEqual(frame.rename(columns={v: k for k, v in ui.SUPPLY_COLUMNS.items()}).to_dict("records"), original)
        normalized = chat._normalize_result_for_chat(payload)
        self.assertEqual(normalized["meta"]["row_count_total"], 429)
        self.assertEqual(len(normalized["df"]), 429)
        with patch.object(chat, "_chat_display_max_rows", return_value=300):
            chat._apply_chat_display_limit_to_payload(normalized)
        self.assertEqual(len(normalized["df_display"]), 300)
        self.assertEqual(len(normalized["df"]), 429)
        self.assertEqual(normalized["meta"]["download_row_count"], 429)
        csv_file, excel_file = chat._make_table_downloads(normalized["df"])
        exported = list(csv.DictReader(StringIO(csv_file.getvalue().decode("utf-8-sig"))))
        self.assertEqual(exported, frame.to_dict("records"))
        book = load_workbook(BytesIO(excel_file.getvalue()), read_only=True)
        try:
            values = list(book.active.values)
            self.assertEqual(len(values), 430)
            self.assertEqual(values[0], tuple(frame.columns))
            self.assertEqual([tuple(str(v) for v in row) for row in values[1:]], list(frame.itertuples(index=False, name=None)))
        finally:
            book.close()

    def test_ma110_shared_numeric_format_blanks_negatives_no_source_mutation(self):
        frame = pd.DataFrame({name: ["1234567", "-1234", None, float("nan")] for name in ui.SUPPLY_COLUMNS.values()})
        before = frame.copy(deep=True)
        view, config, _, _ = display.build_sims_table_display_config(frame, add_row_no=False)
        for name in frame:
            self.assertEqual(list(view[name]), ["1,234,567", "-1,234", "", ""])
            self.assertEqual(config[name]["alignment"], "right")
            self.assertEqual(display.resolve_sims_excel_number_format(name), "#,##0")
        pd.testing.assert_frame_equal(frame, before)

    def test_ma110_allowlist_unchanged_despite_extra_guide_example_fields(self):
        row = {"msupSplyAmt": "2", "msupSplyQty": "1", "rcvCnt": "1", "splyTpCd": "1",
               "splyDd": "20261123", "tkrpHbin": "0000000001", "ykiho": "00000001"}
        result = self.response("MA110", {"splyDd": "20261123"}, {"result": "success", "msupBriefSummary01List": [row]})
        self.assertEqual(set(result["rows"][0]), set(ui.SUPPLY_COLUMNS))
        self.assertEqual(OUTPUTS["MA110"][1], set(ui.SUPPLY_COLUMNS))


if __name__ == "__main__":
    unittest.main()
