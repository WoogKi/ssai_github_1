"""DB-free login/ERP identity boundary checks; all numbers are synthetic."""
from __future__ import annotations

import unittest
from contextlib import ExitStack, nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from app.services import ssai_login_profile_service as profiles
from app.services import kpis_erp_direct as direct
from app.services.kpis_mcp_contract import CompanyApproval, SSAI_ACTIVE_COMPANY, preflight
from app.ui import ssai_login as login
from tools import check_kpis_erp_direct as checks
from tools.check_company_permission_refresh import _user
from tools import check_kpis_session_integration as session_checks
from app.services import external_api_management as management
from app.services.kpis_mcp_contract import Request
from app.services.kpis_rest_client import KpisClient


class IdentityChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("pyodbc.connect", side_effect=AssertionError("live DB forbidden")))
        self.stack.enter_context(patch("httpx.HTTPTransport.handle_request", side_effect=AssertionError("live HTTP forbidden")))

    def test_number_boundaries_and_duplicate_mapping(self):
        for raw in ("1234567890", " 123-45-67890 "):
            identity = profiles.own_identity(8, 42, dict(own_company_rows=1, own_vendor_rows=1, own_company_number=raw))
            self.assertEqual(identity.applicant, "1234567890")
            self.assertNotIn("1234567890", repr(identity))
        for count, raw in ((0, "1234567890"), (2, "1234567890"), (1, None), (1, "0000000000"),
                           (1, "12345678"), (1, "123 45 67890"), (1, "１２３４５６７８９０")):
            identity = profiles.own_identity(8, 42, dict(own_company_rows=count, own_vendor_rows=1, own_company_number=raw))
            self.assertNotEqual(identity.status, "verified")
            self.assertEqual(identity.applicant, "")

    def test_single_existing_query_and_no_user_row_multiplication(self):
        db, cursor = Mock(), Mock()
        db.cursor.return_value = cursor
        cursor.description = [(k,) for k in ("sims_user_id", "sims_user_name", "sims_profile_found", "own_company_rows", "own_vendor_rows", "own_company_number")]
        cursor.execute.return_value.fetchone.return_value = ("synthetic", "synthetic", 1, 1, 1, "123-45-67890")
        with patch.object(profiles, "connect_company_db", return_value=nullcontext(db)) as connect:
            profile = profiles.build_login_profile(user=SimpleNamespace(user_id=42, sims_user_id="synthetic"),
                                                   company=dict(company_id=8))
        connect.assert_called_once_with(8)
        cursor.execute.assert_called_once()
        sql, *params = cursor.execute.call_args.args
        self.assertEqual(params, ["synthetic"] * 3)
        for expected in ("OUTER APPLY", "COUNT_BIG(*)", "V.Rd03_Ven_Cd = OwnSetting.own_vendor_code",
                         "MIN(C.Rd0A_Value)", "FROM (SELECT 1 AS login_scope)",
                         "C.Rd0A_Gcode = '0003'", "C.Rd0A_Tcode = '000001'", "V.Rd03_Ven_Num"):
            self.assertIn(expected, sql)
        own_sql = sql[sql.index("SELECT COUNT_BIG(*) AS own_company_rows"):sql.index(") OwnCompany")]
        self.assertNotIn("TOP", own_sql)
        self.assertNotIn("GROUP BY", own_sql)
        self.assertNotIn("NOLOCK", own_sql)
        self.assertEqual(profile[profiles.ERP_IDENTITY_KEY].applicant, "1234567890")

    def test_invalid_number_keeps_normal_login_profile(self):
        for row in (dict(own_company_rows=0), dict(own_company_rows=1, own_company_number=""),
                    dict(own_company_rows=2, own_company_number="1234567890")):
            with patch.object(profiles, "get_sims_login_profile", return_value=dict(row, sims_user_name="synthetic", sims_profile_found=1, own_vendor_rows=1)):
                profile = profiles.build_login_profile(user=SimpleNamespace(user_id=42, sims_user_id="synthetic"),
                                                       company=dict(company_id=8))
            self.assertEqual(profile["profile_source"], "SIMS_Rddbc060")
            self.assertEqual(profile["sims_user_name"], "synthetic")
            self.assertNotEqual(profile[profiles.ERP_IDENTITY_KEY].status, "verified")

    def test_company_selection_isolates_private_identity_and_clears_on_failure(self):
        user = replace(_user(), user_id=42)
        state = {login.SESSION_AUTH_USER: user}
        identity = profiles.ErpOwnIdentity(8, "42", "verified", "1234567890")
        with patch.object(login.st, "session_state", state), patch.object(login, "_refresh_selected_company_permissions", return_value=True), patch.object(login, "ensure_user_storage_dirs"), patch.object(login, "build_login_profile", return_value={"company_id": 8, profiles.ERP_IDENTITY_KEY: identity}) as build:
            login._after_company_selected(dict(company_id=8, is_active=True))
            self.assertEqual(state[profiles.ERP_IDENTITY_KEY], identity)
            self.assertNotIn(profiles.ERP_IDENTITY_KEY, state[login.SESSION_LOGIN_PROFILE])
            self.assertNotIn("1234567890", repr(state[login.SESSION_LOGIN_PROFILE]))
            self.assertNotIn("1234567890", repr(state[login.SESSION_COMPANY]))
            build.side_effect = ValueError("1234567890")
            login._after_company_selected(dict(company_id=4, is_active=True))
            self.assertNotIn(profiles.ERP_IDENTITY_KEY, state)
            self.assertNotIn("1234567890", repr(state[login.SESSION_LOGIN_PROFILE]))

    def test_logout_clears_identity(self):
        state = {profiles.ERP_IDENTITY_KEY: profiles.ErpOwnIdentity(8, "42", "verified", "1234567890")}
        with patch.object(login.st, "session_state", state), patch.object(login.st, "rerun"), patch.object(login, "_clear_company_dependent_state"):
            login.logout()
        self.assertNotIn(profiles.ERP_IDENTITY_KEY, state)

    def fixture(self):
        fixture = checks.DirectChecks(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_no_env_and_no_business_expiry_fabricated(self):
        f = self.fixture()
        with patch.dict("os.environ", {}, clear=True):
            profile = direct.load_direct_approval(8, "MA117")
            self.assertIsNone(profile["expires_at"])
            self.assertEqual(profile["authorization_basis"], SSAI_ACTIVE_COMPANY)
            self.assertEqual(f.call()["status"], "SUCCESS")
        for event in f.events:
            self.assertNotIn(f.identity.applicant, repr(event))

    def test_identity_switch_and_inactive_before_key_blocks(self):
        f = self.fixture()
        for change in (lambda: f.company.update(is_active=False),
                       lambda: f.state.update({login.SESSION_AUTH_USER: SimpleNamespace(user_id=43)})):
            def audit(event):
                if event["action"] == "ERP_KEY_READ":
                    change()
            self.assertEqual(f.call(audit=audit)["status"], "DENIED")
            f.company["is_active"] = True
        f.key.assert_not_called()

    def test_applicant_echo_is_not_rendered(self):
        f = self.fixture()
        def echo(request):
            return httpx.Response(200, json={"result": "success", "msupNtfcItemList": [{"brdTtl": f.identity.applicant}]})
        result = f.call(handler=echo)
        self.assertEqual(result["reason_code"], "credential_in_response")
        self.assertEqual(result["rows"], [])
        self.assertNotIn(f.identity.applicant, repr(result))

    def test_real_session_loader_composition_and_live_revocation(self):
        f = session_checks.SessionGate(methodName="runTest")
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.state[login.SESSION_COMPANY]["is_active"] = True
        f.state[profiles.ERP_IDENTITY_KEY] = profiles.ErpOwnIdentity(8, "42", "verified", "1234567890")
        actor = management.load_current_api_session()
        request = Request("MA117", "PRODUCTION", 8, "42", actor.epoch)
        key, events = Mock(return_value="synthetic-only"), []
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"result": "success", "msupNtfcItemList": [{"brdTtl": "synthetic"}]}))
        client = KpisClient(direct.ErpDirectAccess(audit=events.append), management.load_current_api_session,
                            enabled=True, erp_key_loader=key, transport=transport)
        query = dict(srchStaDd="20261101", srchEndDd="20261124")
        self.assertEqual(client.call(request, query, explicit=True)["status"], "SUCCESS")
        key.assert_called_once_with(8, "MA117")
        key.reset_mock()
        def revoke(event):
            if event["action"] == "ERP_KEY_READ":
                f.membership.return_value = []
        client.store = direct.ErpDirectAccess(audit=revoke)
        result = client.call(request, query, explicit=True)
        self.assertEqual(result["status"], "DENIED")
        self.assertEqual(result["physical_call_count"], 0)
        key.assert_not_called()

    def test_legacy_expiry_and_jwt_not_relaxed(self):
        f = self.fixture()
        session = replace(f.actor, permissions=frozenset({"MCP_EXTERNAL_RESOURCE_READ"}),
                          service_modes=frozenset({("MA117", "PRODUCTION")}))
        def gate(approval, request=f.req, current=session):
            return preflight(request, load_session=lambda: current, approval=approval,
                             feature_enabled=True, explicit_request=True)["status"]
        active = CompanyApproval(8, "MA117", "PRODUCTION", True, None, SSAI_ACTIVE_COMPANY, SSAI_ACTIVE_COMPANY)
        self.assertEqual(gate(active), "PREFLIGHT_ONLY")
        for approval in (replace(active, enabled=False), replace(active, authorization_basis="EXPIRY"),
                         replace(active, evidence_id="untrusted"), replace(active, expires_at=f.until),
                         CompanyApproval(8, "MA117", "PRODUCTION", True, datetime.now(timezone.utc)-timedelta(seconds=1), "synthetic")):
            self.assertEqual(gate(approval), "DENIED")
        req = replace(f.req, service="MA116")
        current = replace(session, service_modes=frozenset({("MA116", "PRODUCTION")}))
        self.assertEqual(gate(replace(active, service="MA116"), req, current), "DENIED")


if __name__ == "__main__":
    unittest.main()
