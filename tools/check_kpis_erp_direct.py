"""ERP-direct checks with synthetic credentials, blocked real DB, MockTransport."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from app.services import kpis_erp_direct as direct
from app.services.external_api_management import ApiManagementError, load_erp_api_key
from app.services.kpis_mcp_contract import Request, Session
from app.services.kpis_rest_client import KpisClient, OUTPUTS, SERVICES
from app.ui.kpis_erp_query import publish_result
from app.ui import ssai_login as login
from app.services.ssai_login_profile_service import ERP_IDENTITY_KEY, ErpOwnIdentity


class DirectChecks(unittest.TestCase):
    def setUp(self):
        self.until = datetime.now(timezone.utc) + timedelta(hours=1)
        self.actor = Session(8, "42", 12345, frozenset(),
                             frozenset(), False, self.until)
        self.entry = dict(applicant="1234567890")
        self.company = dict(company_id=8, is_active=True)
        self.identity = ErpOwnIdentity(8, "42", "verified", self.entry["applicant"])
        self.state = {login.SESSION_AUTH_USER: SimpleNamespace(user_id=42),
                      login.SESSION_COMPANY: self.company, ERP_IDENTITY_KEY: self.identity}
        state_patch = patch.object(login.st, "session_state", self.state)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.env = patch.dict(os.environ, {"SSAI_KPIS_ERP_DIRECT_APPROVALS": "ignored-invalid-legacy-setting"})
        self.env.start()
        self.addCleanup(self.env.stop)
        for target in ("sqlite3.connect", "app.services.ssai_auth_service.connect_ssai_db",
                       "app.services.ssai_auth_service.connect_company_db"):
            guard = patch(target, side_effect=AssertionError("real_db_forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        self.events, self.sent = [], []
        self.key = Mock(return_value="synthetic-erp-key-only")
        self.req = Request("MA117", "PRODUCTION", 8, "42", 12345)
        self.query = dict(srchStaDd="20261101", srchEndDd="20261124")

    def handler(self, request):
        self.sent.append(request)
        code = next(c for c in direct.DIRECT_SERVICES if SERVICES[c].path == request.url.path)
        field = "brdTtl" if code == "MA117" else sorted(OUTPUTS[code][1])[0]
        return httpx.Response(200, json={"result": "success", OUTPUTS[code][0]: [{field: "합성 공지"}]})

    def call(self, **kwargs):
        client = KpisClient(direct.ErpDirectAccess(audit=kwargs.pop("audit", self.events.append)),
                            kwargs.pop("load_session", lambda: self.actor), enabled=kwargs.pop("enabled", True),
                            erp_key_loader=self.key, transport=httpx.MockTransport(kwargs.pop("handler", self.handler)))
        return client.call(kwargs.pop("request", self.req), kwargs.pop("query", self.query),
                           explicit=kwargs.pop("explicit", True))

    def test_ma117_without_sqlite_or_vault(self):
        result = self.call()
        self.assertEqual(result["status"], "SUCCESS")
        self.key.assert_called_once_with(8, "MA117")
        self.assertEqual(result["physical_call_count"], 1)
        self.assertEqual(result["credential_read_count"], 1)
        self.assertEqual(str(self.sent[0].url), "https://newopenapi.kpis.or.kr/v1/services/getMsupNtfcItemInq.do")
        body = json.loads(self.sent[0].content)
        self.assertEqual(set(body), {"srchStaDd", "srchEndDd", "aplHbin", "apiKey"})
        self.assertEqual(body["aplHbin"], self.entry["applicant"])
        self.assertNotIn("synthetic-erp-key-only", json.dumps(result))
        self.assertNotIn("synthetic-erp-key-only", json.dumps(self.events))
        self.assertEqual([e["action"] for e in self.events], ["ERP_KEY_READ", "CALL_START", "CALL_END"])

    def test_no_credential_cache(self):
        self.call()
        self.call()
        self.assertEqual(self.key.call_count, 2)

    def test_shared_service_path_and_jwt_separation(self):
        for code, query in (("MA101", {"msupStdCd": "0000000000000"}),
                            ("MA102", {"msupSplyFrmCd": "5", "tkrHbin": "0000000001"}), ("MA110", {"splyDd": "20261101"})):
            self.assertEqual(self.call(request=replace(self.req, service=code), query=query)["status"], "SUCCESS")
        count = self.key.call_count
        result = self.call(request=replace(self.req, service="MA116"), query={"rcvYr": "2026", "rcvQt": "3"})
        self.assertEqual(result["reason_code"], "erp_direct_service_not_supported")
        self.assertEqual(self.key.call_count, count)

    def test_no_identity_no_key_no_http(self):
        cases = [None, replace(self.identity, company_id=4), replace(self.identity, user_id="43"),
                 replace(self.identity, status="ambiguous"), replace(self.identity, applicant=""),
                 {"applicant": self.entry["applicant"]}]
        for identity in cases:
            with patch.dict(self.state, {ERP_IDENTITY_KEY: identity}):
                self.assertEqual(self.call()["status"], "DENIED")
        with patch.dict(self.company, {"is_active": False}):
            self.assertEqual(self.call()["status"], "DENIED")
        self.key.assert_not_called()
        self.assertEqual(self.sent, [])

    def test_flags_explicit_permission_company_and_epoch(self):
        for kwargs in ({"enabled": False}, {"explicit": False},
                       {"load_session": lambda: None},
                       {"load_session": lambda: replace(self.actor, company_id=4)},
                       {"load_session": lambda: replace(self.actor, epoch=999)},
                       {"request": replace(self.req, mode="TEST")}):
            self.assertEqual(self.call(**kwargs)["status"], "DENIED")
        self.key.assert_not_called()
        self.assertFalse(self.sent)

    def test_company_switch_after_key_blocks_http(self):
        def key(company, service):
            self.actor = replace(self.actor, company_id=4, epoch=999)
            return "synthetic-key"
        self.key.side_effect = key
        self.assertEqual(self.call()["physical_call_count"], 0)
        self.assertFalse(self.sent)

    def test_revoke_after_response_discards_rows(self):
        def handler(request):
            result = self.handler(request)
            self.actor = None
            return result
        result = self.call(handler=handler)
        self.assertEqual(result["status"], "DENIED")
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["physical_call_count"], 1)

    def test_identity_change_before_erp_read_blocks(self):
        def audit(event):
            self.events.append(event)
            if event["action"] == "ERP_KEY_READ":
                self.state[ERP_IDENTITY_KEY] = replace(self.identity, applicant="9876543210")
        self.assertEqual(self.call(audit=audit)["status"], "DENIED")
        self.key.assert_not_called()

    def test_audit_failure_prevents_secret_access(self):
        def audit(event):
            raise OSError("synthetic-disk-failure")
        result = self.call(audit=audit)
        self.assertEqual(result["reason_code"], "audit_unavailable")
        self.key.assert_not_called()
        self.assertFalse(self.sent)

    def test_invalid_query_blocks_key(self):
        for query in (dict(self.query, apiKey="injection"), dict(self.query, srchStaDd="20260230")):
            self.assertEqual(self.call(query=query)["status"], "DENIED")
        self.key.assert_not_called()

    def test_timeout_no_retry_and_key_echo_blocked(self):
        def timeout(req):
            raise httpx.ReadTimeout("synthetic-erp-key-only")
        result = self.call(handler=timeout)
        self.assertEqual((result["reason_code"], result["physical_call_count"], result["retry_count"]), ("timeout", 1, 0))
        def echo(req):
            return httpx.Response(200, json={"result": "success", "msupNtfcItemList": [{"brdTtl": "synthetic-erp-key-only"}]})
        result = self.call(handler=echo)
        self.assertEqual(result["reason_code"], "credential_in_response")
        self.assertEqual(result["rows"], [])

    def test_erp_loader_uses_company_and_exact_mapping(self):
        for code, tcode in (("MA117", "000010"), ("MA101", "000001"), ("MA102", "000006"), ("MA110", "000007")):
            cursor, connection = Mock(), Mock()
            cursor.fetchmany.return_value = [("synthetic-only",)]
            connection.cursor.return_value = cursor
            with patch("app.services.ssai_auth_service.connect_company_db", return_value=connection) as connect:
                self.assertEqual(load_erp_api_key(8, code), "synthetic-only")
                connect.assert_called_once_with(8)
                sql, group, actual = cursor.execute.call_args.args
                self.assertEqual((group, actual), ("000010", tcode))
                self.assertIn("TOP (2)", sql)
                self.assertNotIn("SELECT *", sql)
                self.assertEqual(connection.timeout, 10)
                cursor.close.assert_called_once()
                connection.close.assert_called_once()
                for rows in ([], [("a",), ("b",)], [(None,)], [("",)]):
                    cursor.fetchmany.return_value = rows
                    with self.assertRaises(ApiManagementError): load_erp_api_key(8, code)

    def test_safe_log_and_unavailable_log(self):
        with tempfile.TemporaryDirectory() as root, patch("app.utils.env_config.read_project_env_file", return_value={"LOG_FILE": str(Path(root) / "app.log")}):
            event = dict(company_id=8, service="MA117", action="CALL_END", calls=1, result="ok")
            direct.append_safe_audit(event)
            self.assertEqual(json.loads((Path(root) / "app.log.kpis.jsonl").read_text()), event)
        with patch("app.utils.env_config.read_project_env_file", return_value={}):
            with self.assertRaises(ApiManagementError): direct.append_safe_audit({})

    def test_chat_payload_and_company_switch_block(self):
        result = self.call()
        push = Mock(return_value={})
        expected = (8, "42", 12345)
        self.assertTrue(publish_result(result, self.query, expected, load_session=lambda: self.actor, push=push))
        payload = push.call_args.args[0]
        self.assertEqual(payload["meta"]["_ssai_company_id"], 8)
        self.assertIn("제목", payload["df"].columns)
        self.assertNotIn("synthetic-erp-key-only", str(payload))
        self.actor = replace(self.actor, company_id=4)
        self.assertFalse(publish_result(result, self.query, expected, load_session=lambda: self.actor, push=push))
        push.assert_called_once()

    def test_ui_never_opens_vault_and_sends_once(self):
        from streamlit.testing.v1 import AppTest
        from app.ui import kpis_erp_query as ui
        result = self.call()
        client = Mock()
        client.call.return_value = result
        push = Mock(return_value={})
        with patch.object(ui, "load_current_api_session", side_effect=lambda: self.actor), patch.object(
                ui, "configured_direct_client", return_value=client), patch(
                "app.ui.chat_middleware.wssz", push), patch.dict(os.environ, {
                    "SSAI_KPIS_LIVE_ENABLED": "1", "SSAI_KPIS_ERP_KEY_READ_APPROVED": "1"}):
            page = AppTest.from_string("from app.ui.kpis_erp_query import render_kpis_erp_query\nrender_kpis_erp_query()")
            page.run(timeout=15)
            self.assertFalse(page.exception)
            self.assertEqual(len(page.date_input), 2)
            page.date_input[0].set_value("2026-11-01")
            page.date_input[1].set_value("2026-11-24")
            self.assertFalse(page.checkbox)
            page.button[0].click().run(timeout=15)
            self.assertFalse(page.exception)
            client.call.assert_called_once()
            push.assert_called_once()
            self.assertEqual(len(page.dataframe), 0)

    def test_common_table_normalization_and_export(self):
        from app.ui import chat_middleware as chat
        push = Mock(return_value={})
        publish_result(self.call(), self.query, (8, "42", 12345), load_session=lambda: self.actor, push=push)
        normalized = chat._normalize_result_for_chat(push.call_args.args[0])
        self.assertEqual(normalized["meta"]["source"], "KPIS")
        self.assertEqual(normalized["meta"]["row_count_total"], 1)
        with patch("app.ui.ssai_login.get_selected_company", return_value={"company_id": 8}):
            self.assertTrue(chat._chat_payload_matches_current_company(normalized))
        with patch("app.ui.ssai_login.get_selected_company", return_value={"company_id": 4}):
            self.assertFalse(chat._chat_payload_matches_current_company(normalized))
        csv, excel = chat._make_table_downloads(normalized["df"])
        self.assertIn("합성 공지", csv.getvalue().decode("utf-8-sig"))
        self.assertTrue(excel.getvalue().startswith(b"PK"))

    def test_kpis_permission_does_not_grant_user_management(self):
        from streamlit.testing.v1 import AppTest
        from types import SimpleNamespace
        from app.ui import ssai_admin as admin
        user = SimpleNamespace(user_type="WHOLESALE_STAFF", user_grade="STAFF")
        with patch.object(admin, "get_current_user", return_value=user), patch.object(
                admin, "has_permission", side_effect=lambda p: p == "MCP_EXTERNAL_RESOURCE_READ"), patch.object(
                admin, "render_user_approval_page") as user_page, patch.object(admin, "render_user_lookup_debug") as debug:
            page = AppTest.from_string("from app.ui.ssai_admin import render_ssai_admin_sidebar\nrender_ssai_admin_sidebar()")
            page.run(timeout=15)
            self.assertFalse(page.exception)
            self.assertNotIn("KPIS 조회", [b.label for b in page.button])
            self.assertNotIn("회원사 사용자 관리", [b.label for b in page.button])
            denied = AppTest.from_string("import streamlit as st\nfrom app.ui.ssai_admin import SESSION_ADMIN_PAGE, render_ssai_admin_page\nst.session_state[SESSION_ADMIN_PAGE] = 'user_approval'\nrender_ssai_admin_page()")
            denied.run(timeout=15)
            self.assertFalse(denied.exception)
            self.assertTrue(denied.error)
            user_page.assert_not_called()
            debug.assert_not_called()


if __name__ == "__main__":
    unittest.main()
