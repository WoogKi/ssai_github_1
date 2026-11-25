"""DB-free synthetic-only focused checks. No ERP/KPIS/LM Studio calls."""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from cryptography.fernet import Fernet

from app.services.external_api_management import (
    ApiStore, ApiManagementError, VIEW, MANAGE, SECRET, GRANT, AUDIT,
)
from app.services.kpis_mcp_contract import FIRST_PRIORITY, Request, Session
from app.services.kpis_rest_client import KpisClient, SERVICES, OUTPUTS


class Gates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "vault.sqlite"
        self.store = ApiStore(self.path, Fernet.generate_key())
        self.store.provision()
        self.until = datetime.now(timezone.utc) + timedelta(hours=1)
        self.actor = Session(8, "42", 1, frozenset({VIEW, MANAGE, SECRET, GRANT, AUDIT,
                             "MCP_EXTERNAL_RESOURCE_READ"}), frozenset(), True, self.until)
        self.request = Request("MA117", "PRODUCTION", 8, "42", 1)
        self.query = {"srchStaDd": "20261101", "srchEndDd": "20261123"}
        self.sent = []
        self.configure()

    def configure(self, service="MA117", mode="PRODUCTION"):
        args = dict(applicant="0000000000", enabled=True, expires=self.until, evidence="synthetic-approval")
        self.store.configure(self.actor, 8, service, mode, **args)
        creds = {"client_id": "synthetic-client", "client_secret": "synthetic-secret-abc"} if service == "MA116" else {"apiKey": "synthetic-api-key-xyz"}
        self.store.set_credentials(self.actor, 8, service, mode, creds)
        self.store.configure(self.actor, 8, service, mode, **args)
        self.store.grant(self.actor, 8, service, mode, "42", self.until)

    def handler(self, req):
        self.sent.append(req)
        if req.url.path == "/v1/auth/jwt":
            return httpx.Response(200, json={"status": "success", "jwt": "synthetic.jwt.value"})
        service = next(c for c in FIRST_PRIORITY if SERVICES[c].path == req.url.path)
        field = sorted(OUTPUTS[service][1])[0]
        return httpx.Response(200, json={"result": "success", "apiKey": "synthetic-api-key-xyz",
                            OUTPUTS[service][0]: [{field: "synthetic-value", "password": "must-not-leak"}]})

    def call(self, handler=None, actor=None, request=None, query=None, **kwargs):
        client = KpisClient(self.store, lambda: actor or self.actor, enabled=True,
                            transport=httpx.MockTransport(handler or self.handler), **kwargs)
        return client.call(request or self.request, self.query if query is None else query, explicit=True)

    def test_key_encryption_and_masked_view(self):
        raw = self.path.read_bytes()
        self.assertNotIn(b"synthetic-api-key-xyz", raw)
        self.assertNotIn(b"0000000000", raw)
        self.assertNotIn("apiKey", json.dumps(self.store.view(self.actor, 8)))
        self.assertNotIn("synthetic-api", json.dumps(self.store.history(self.actor, 8)))

    def test_no_admin_implicit_secret_grant(self):
        admin = replace(self.actor, permissions=frozenset({"USER_MANAGE_ALL", "COMPANY_MANAGE", VIEW}))
        with self.assertRaises(ApiManagementError):
            self.store.set_credentials(admin, 8, "MA117", "PRODUCTION", {"apiKey": "x"})
        with self.assertRaises(ApiManagementError):
            self.store.grant(admin, 8, "MA117", "PRODUCTION", "42", self.until)

    def test_company_and_cipher_binding(self):
        for fn in (lambda: self.store.view(self.actor, 4), lambda: self.store.history(self.actor, 4)):
            with self.assertRaises(ApiManagementError): fn()
        with self.store.db() as db:
            db.execute("UPDATE profiles SET company=4")
        with self.assertRaisesRegex(ApiManagementError, "profile_integrity"):
            self.store.view(replace(self.actor, company_id=4), 4)

    def test_rotation_revocation_disables(self):
        self.store.set_credentials(self.actor, 8, "MA117", "PRODUCTION", {"apiKey": "changed-secret"})
        self.assertFalse(self.store.view(self.actor, 8)[0]["enabled"])
        self.assertEqual(self.call()["physical_call_count"], 0)
        self.store.set_credentials(self.actor, 8, "MA117", "PRODUCTION", {})
        self.assertFalse(self.store.view(self.actor, 8)[0]["credential_registered"])

    def test_applicant_change_cannot_reuse_secret(self):
        with self.assertRaisesRegex(ApiManagementError, "revoke_credentials"):
            self.store.configure(self.actor, 8, "MA117", "PRODUCTION", applicant="1111111111",
                                 enabled=True, expires=self.until, evidence="changed")

    def test_no_implicit_store_creation(self):
        missing = ApiStore(Path(self.tmp.name) / "missing.sqlite", Fernet.generate_key())
        with self.assertRaises(ApiManagementError): missing.view(self.actor, 8)
        self.assertFalse(missing.path.exists())
        with self.assertRaises(FileExistsError): self.store.provision()

    def test_api_key_fixed_endpoint_one_call_safe_result(self):
        result = self.call()
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["physical_call_count"], 1)
        self.assertEqual(result["retry_count"], 0)
        self.assertEqual(self.sent[0].url.host, "newopenapi.kpis.or.kr")
        body = json.loads(self.sent[0].content)
        self.assertEqual(body["aplHbin"], "0000000000")
        self.assertEqual(body["apiKey"], "synthetic-api-key-xyz")
        for value in ("synthetic-api", "must-not-leak", "password", "apiKey"):
            self.assertNotIn(value, json.dumps(result))

    def test_jwt_two_calls_no_token_storage(self):
        self.configure("MA116")
        result = self.call(request=replace(self.request, service="MA116"), query={"rcvYr": "2026", "rcvQt": "3"})
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["physical_call_count"], 2)
        self.assertEqual(self.sent[1].headers["Authorization"], "Bearer synthetic.jwt.value")
        self.assertNotIn(b"synthetic.jwt.value", self.path.read_bytes())
        self.assertNotIn("synthetic.jwt", json.dumps(result))

    def test_first_five_contracts(self):
        queries = {"MA117": self.query, "MA101": {"msupStdCd": "0000000000000"},
                   "MA102": {"msupSplyFrmCd": "6", "tkrHbin": "0000000001"}, "MA116": {"rcvYr": "2026", "rcvQt": "4"},
                   "MA110": {"splyDd": "20261101"}}
        for code in FIRST_PRIORITY:
            with self.subTest(code=code):
                self.configure(code)
                self.assertEqual(self.call(request=replace(self.request, service=code), query=queries[code])["status"], "SUCCESS")

    def test_untrusted_inputs_and_dates_block(self):
        for query in ({**self.query, "apiKey": "injected"}, {**self.query, "aplHbin": "other"},
                      {**self.query, "url": "http://localhost"}, {**self.query, "srchStaDd": "20260230"},
                      {"srchStaDd": "20261123", "srchEndDd": "20261101"}):
            with self.subTest(query_fields=list(query)):
                self.assertEqual(self.call(query=query)["physical_call_count"], 0)
        for code in ("MA111", "MA113", "MA118", "unknown"):
            self.assertEqual(self.call(request=replace(self.request, service=code))["physical_call_count"], 0)

    def test_permission_expiry_company_and_grant_fail_closed(self):
        for actor in (replace(self.actor, company_id=4), replace(self.actor, user_id="43"),
                      replace(self.actor, epoch=2), replace(self.actor, permissions=frozenset()),
                      replace(self.actor, expires_at=datetime.now(timezone.utc)-timedelta(seconds=1))):
            self.assertEqual(self.call(actor=actor)["physical_call_count"], 0)
        self.store.grant(self.actor, 8, "MA117", "PRODUCTION", "42")
        self.assertEqual(self.call()["physical_call_count"], 0)

    def test_test_credentials_and_http_approval_separate(self):
        self.assertEqual(self.call(request=replace(self.request, mode="TEST"))["physical_call_count"], 0)
        self.configure(mode="TEST")
        req = replace(self.request, mode="TEST")
        self.assertEqual(self.call(request=req)["reason_code"], "test_plain_http_not_approved")
        self.assertEqual(self.call(request=req, actor=replace(self.actor, test_authorized=False),
                                   test_http_approved=True)["physical_call_count"], 0)
        self.assertEqual(self.call(request=req, test_http_approved=True)["status"], "SUCCESS")
        self.assertEqual(self.sent[-1].url.host, "devopenapi.kpis.or.kr")

    def test_redirect_error_timeout_no_retry_or_secret_message(self):
        def timeout(req):
            self.sent.append(req)
            raise httpx.ReadTimeout("synthetic-api-key-xyz", request=req)
        for handler in (lambda r: httpx.Response(302, headers={"Location": "https://untrusted.invalid"}),
                        lambda r: httpx.Response(500, text="synthetic-api-key-xyz"), timeout):
            result = self.call(handler=handler)
            self.assertEqual(result["status"], "DENIED")
            self.assertEqual(result["physical_call_count"], 1)
            self.assertNotIn("synthetic-api-key", json.dumps(result))

    def test_response_limits_schema_and_echo(self):
        values = [dict(result="success", msupNtfcItemList=[{"brdTtl": "synthetic-api-key-xyz"}]),
                  dict(result="success", msupNtfcItemList=[{"brdTtl": "x"}]*1001),
                  dict(result="success", msupNtfcItemList=None), dict(result="fail", resultMessage="secret")]
        for value in values:
            result = self.call(handler=lambda r: httpx.Response(200, json=value))
            self.assertEqual(result["status"], "DENIED")
            self.assertEqual(result["rows"], [])
        self.assertEqual(self.call(handler=lambda r: httpx.Response(200, content=b' '* (2097152+1),
                         headers={"Content-Type": "application/json"}))["reason_code"], "response_limit")

    def test_revocation_between_jwt_and_query(self):
        self.configure("MA116")
        def handler(req):
            result = self.handler(req)
            self.store.grant(self.actor, 8, "MA116", "PRODUCTION", "42")
            return result
        result = self.call(handler=handler, request=replace(self.request, service="MA116"),
                           query={"rcvYr": "2026", "rcvQt": "3"})
        self.assertEqual(result["status"], "DENIED")
        self.assertEqual(result["physical_call_count"], 1)

    def test_ui_menu_and_secret_form(self):
        # Streamlit's test renderer, no real server/DB/browser or external call.
        from unittest.mock import patch
        from streamlit.testing.v1 import AppTest
        with patch("app.services.external_api_management.load_current_api_session", return_value=self.actor), patch(
                "app.services.external_api_management.configured_store", return_value=self.store):
            page = AppTest.from_string("from app.ui.external_api_admin import render_external_api_admin\nrender_external_api_admin()")
            page.run(timeout=10)
            self.assertEqual(len(page.exception), 0)
            self.assertTrue(any(b.label == "인증정보 등록·교체" for b in page.button))
            self.assertFalse(any("synthetic-api-key" in str(t.value) for t in page.text_input))
        with patch("app.services.external_api_management.load_current_api_session", return_value=replace(self.actor, permissions=frozenset({"USER_MANAGE_ALL"}))):
            denied = AppTest.from_string("from app.ui.external_api_admin import render_external_api_admin\nrender_external_api_admin()")
            denied.run(timeout=10)
            self.assertEqual(len(denied.exception), 0)
            self.assertEqual(len(denied.text_input), 0)

    def test_company_change_and_invalid_session_clear_form_state(self):
        from app.ui.external_api_admin import clear_changed_scope
        for scope in (("42", 4), None):
            state = {"__external_api_scope": ("42", 8), "__external_api_42_8_secret": "synthetic-secret", "unrelated": 7}
            clear_changed_scope(state, scope)
            self.assertNotIn("__external_api_42_8_secret", state)
            self.assertEqual(state["unrelated"], 7)

    def test_disabled_client_and_explicit_gate(self):
        client = KpisClient(self.store, lambda: self.actor, transport=httpx.MockTransport(self.handler))
        self.assertEqual(client.call(self.request, self.query, explicit=True)["physical_call_count"], 0)
        client.enabled = True
        self.assertEqual(client.call(self.request, self.query)["physical_call_count"], 0)
        self.assertEqual(self.sent, [])

    def test_secret_echo_with_json_escaping_blocked(self):
        self.store.set_credentials(self.actor, 8, "MA117", "PRODUCTION", {"apiKey": 'synthetic"quoted'})
        self.store.configure(self.actor, 8, "MA117", "PRODUCTION", applicant="0000000000", enabled=True,
                             expires=self.until, evidence="synthetic")
        result = self.call(handler=lambda r: httpx.Response(200, json={"result": "success",
                            "msupNtfcItemList": [{"brdTtl": 'synthetic"quoted'}]}))
        self.assertEqual(result["reason_code"], "credential_in_response")
        self.assertEqual(result["rows"], [])

    def test_erp_reference_no_copy_no_read_until_approved(self):
        from unittest.mock import Mock
        self.configure("MA101")
        self.store.set_credentials(self.actor, 8, "MA101", "PRODUCTION", {})
        self.store.set_erp_reference(self.actor, 8, "MA101", "PRODUCTION")
        self.store.configure(self.actor, 8, "MA101", "PRODUCTION", applicant="0000000000", enabled=True,
                             expires=self.until, evidence="synthetic")
        req = replace(self.request, service="MA101")
        query = {"msupStdCd": "0000000000000"}
        self.assertEqual(self.call(request=req, query=query)["reason_code"], "erp_key_read_not_approved")
        loader = Mock(return_value="synthetic-erp-key")
        result = self.call(request=req, query=query, erp_key_loader=loader)
        self.assertEqual(result["status"], "SUCCESS")
        loader.assert_called_once_with(8, "MA101")
        self.assertEqual(result["credential_read_count"], 1)
        self.assertNotIn(b"synthetic-erp-key", self.path.read_bytes())
        loader.reset_mock()
        self.call(request=req, query=query, actor=replace(self.actor, company_id=4), erp_key_loader=loader)
        loader.assert_not_called()

    def test_erp_missing_mapping_and_test_mode_blocked(self):
        for code, mode in (("MA116", "PRODUCTION"), ("MA118", "PRODUCTION"), ("MA101", "TEST")):
            with self.assertRaises(ApiManagementError):
                self.store.set_erp_reference(self.actor, 8, code, mode)

    def test_erp_mapping_guide_and_user_evidence(self):
        from app.services.external_api_management import ERP_KEY_CODES
        self.assertEqual(dict(ERP_KEY_CODES), {"MA101": "000001", "MA103": "000002", "MA104": "000003",
                         "MA105": "000004", "MA106": "000005", "MA102": "000006", "MA110": "000007",
                         "MA107": "000008", "MA109": "000009", "MA117": "000010"})
        self.assertNotIn("MA105", FIRST_PRIORITY)
        self.assertNotIn("MA116", ERP_KEY_CODES)

    def test_grant_listing_is_scoped_and_permissioned(self):
        self.assertEqual(self.store.grants_view(self.actor, 8)[0]["user"], "42")
        with self.assertRaises(ApiManagementError):
            self.store.grants_view(self.actor, 4)
        with self.assertRaises(ApiManagementError):
            self.store.grants_view(replace(self.actor, permissions=frozenset({VIEW})), 8)

    def test_erp_loader_bounded_parameters_and_cleanup(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from app.services.external_api_management import load_erp_api_key
        cursor = Mock()
        cursor.fetchmany.return_value = [("synthetic-only",)]
        connection = Mock()
        connection.cursor.return_value = cursor
        connector = Mock(return_value=connection)
        with patch.dict(sys.modules, {"app.services.ssai_auth_service": SimpleNamespace(connect_company_db=connector)}):
            self.assertEqual(load_erp_api_key(8, "MA101"), "synthetic-only")
            connector.assert_called_once_with(8)
            sql, group, code = cursor.execute.call_args.args
            self.assertIn("TOP (2)", sql)
            self.assertNotIn("SELECT *", sql)
            self.assertEqual((group, code), ("000010", "000001"))
            self.assertEqual(connection.timeout, 10)
            cursor.close.assert_called_once()
            connection.close.assert_called_once()
            cursor.fetchmany.return_value = [("synthetic-a",), ("synthetic-b",)]
            with self.assertRaisesRegex(ApiManagementError, "missing_or_duplicate"):
                load_erp_api_key(8, "MA101")


if __name__ == "__main__":
    unittest.main()
