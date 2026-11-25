"""ERP panel access only; DB/HTTP doubles, no permissions seeded or live keys read."""
from __future__ import annotations

import os
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from streamlit.testing.v1 import AppTest

from app.services import external_api_management as management, kpis_erp_direct as direct
from app.services.kpis_mcp_contract import Request, CompanyApproval, SSAI_ACTIVE_COMPANY, preflight, erp_direct_preflight
from app.services.ssai_login_profile_service import ERP_IDENTITY_KEY, ErpOwnIdentity
from app.ui import kpis_erp_query as ui, ssai_login as login
from tools import check_kpis_erp_direct as direct_checks, check_kpis_session_integration as session_checks


class PanelAccess(unittest.TestCase):
    def direct_fixture(self):
        f = direct_checks.DirectChecks(methodName="runTest")
        f.setUp()
        self.addCleanup(f.doCleanups)
        return f

    def central_fixture(self):
        f = session_checks.SessionGate(methodName="runTest")
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.permissions[:] = []
        f.state[login.SESSION_COMPANY]["is_active"] = True
        f.state[ERP_IDENTITY_KEY] = ErpOwnIdentity(8, "42", "verified", "1234567890")
        return f

    def test_no_mcp_regular_and_admin_same_direct_policy(self):
        f = self.direct_fixture()
        for user_type in ("WHOLESALE_STAFF", "SSART_ADMIN"):
            f.state[login.SESSION_AUTH_USER] = SimpleNamespace(user_id=42, user_type=user_type)
            self.assertEqual(f.call()["status"], "SUCCESS")
        self.assertFalse(f.actor.permissions)
        self.assertEqual(f.key.call_count, 2)

    def test_default_mcp_gate_not_relaxed(self):
        f = self.direct_fixture()
        actor = replace(f.actor, service_modes=frozenset({("MA117", "PRODUCTION")}))
        approval = CompanyApproval(8, "MA117", "PRODUCTION", True, None, SSAI_ACTIVE_COMPANY, SSAI_ACTIVE_COMPANY)
        args = dict(load_session=lambda: actor, approval=approval, feature_enabled=True, explicit_request=True)
        self.assertEqual(preflight(f.req, **args)["reason_code"], "permission_denied")
        self.assertEqual(erp_direct_preflight(f.req, **args)["status"], "PREFLIGHT_ONLY")
        for mode, service in (("MOCK", "MA117"), ("TEST", "MA117"), ("PRODUCTION", "MA116")):
            self.assertEqual(erp_direct_preflight(replace(f.req, mode=mode, service=service), **args)["status"], "DENIED")
        dated = replace(approval, authorization_basis="EXPIRY", evidence_id="fixture", expires_at=f.until)
        self.assertEqual(erp_direct_preflight(f.req, **dict(args, approval=dated))["status"], "DENIED")

    def test_central_inactive_and_inaccessible_company_block(self):
        f = self.central_fixture()
        for invalid in ("inactive", "unapproved", "inaccessible"):
            actor = management.load_current_api_session()
            key = Mock(return_value="synthetic-only")
            if invalid == "inactive": f.row["is_active"] = False
            elif invalid == "unapproved": f.row["approval_status"] = "REJECTED"
            else: f.membership.return_value = []
            client = direct.KpisClient(direct.ErpDirectAccess(audit=lambda _: None), management.load_current_api_session,
                                       enabled=True, erp_key_loader=key, transport=httpx.MockTransport(lambda _: self.fail("HTTP forbidden")))
            result = client.call(Request("MA117", "PRODUCTION", 8, "42", actor.epoch),
                                 dict(srchStaDd="20261101", srchEndDd="20261124"), explicit=True)
            self.assertEqual(result["status"], "DENIED")
            key.assert_not_called()
            f.row.update(is_active=True, approval_status="APPROVED")
            f.membership.return_value = [8]

    def test_real_central_session_without_permissions_can_call(self):
        f = self.central_fixture()
        actor = management.load_current_api_session()
        key, sent = Mock(return_value="synthetic-key"), []
        def respond(request):
            sent.append(request)
            return httpx.Response(200, json={"result": "success", "msupNtfcItemList": []})
        with patch.object(direct, "append_safe_audit", lambda _: None), patch.object(direct, "load_erp_api_key", key), patch.dict(os.environ, {"SSAI_KPIS_LIVE_ENABLED": "1", "SSAI_KPIS_ERP_KEY_READ_APPROVED": "1"}):
            client = direct.configured_direct_client()
            client.store.audit = lambda _: None
            client.transport = httpx.MockTransport(respond)
            result = client.call(Request("MA117", "PRODUCTION", 8, "42", actor.epoch),
                                 dict(srchStaDd="20261101", srchEndDd="20261124"), explicit=True)
        self.assertEqual(result["status"], "SUCCESS")
        key.assert_called_once_with(8, "MA117")
        self.assertEqual(len(sent), 1)

    def test_company_switch_or_identity_loss_before_publish_blocks(self):
        f = self.direct_fixture()
        result = f.call()
        push = Mock()
        for identity in (None, replace(f.identity, company_id=4)):
            f.state[ERP_IDENTITY_KEY] = identity
            self.assertFalse(ui.publish_result(result, f.query, (8, "42", 12345), load_session=lambda: f.actor, push=push))
        f.state[ERP_IDENTITY_KEY] = f.identity
        f.company["is_active"] = False
        self.assertFalse(ui.publish_result(result, f.query, (8, "42", 12345), load_session=lambda: f.actor, push=push))
        push.assert_not_called()

    def test_panel_menu_reuses_selection_contract(self):
        page = AppTest.from_string("from app.ui.sims_entry import _panel_sidebar\n_panel_sidebar()")
        page.run(timeout=20)
        self.assertFalse(page.exception)
        group = next(x for x in page.selectbox if x.label == "업무그룹")
        self.assertIn("KPIS", group.options)
        group.select("KPIS").run(timeout=20)
        self.assertFalse(page.exception)
        action = next(x for x in page.selectbox if x.label == "작업선택")
        self.assertEqual(action.options, list(ui.KPIS_ACTIONS))
        self.assertEqual(page.session_state["__sims_selected"], {"category": "KPIS", "action": next(iter(ui.KPIS_ACTIONS))})

    def test_panel_form_pushes_once_no_duplicate_table_on_rerun(self):
        f = self.direct_fixture()
        from app.ui import sims_panel as panel
        result = f.call()
        client = Mock()
        client.call.return_value = result
        push = Mock(return_value={})
        with patch.object(ui, "load_current_api_session", return_value=f.actor), patch.object(ui, "configured_direct_client", return_value=client), patch("app.ui.chat_middleware.wssz", push), patch.object(panel, "render_sims_context_controls"), patch.dict(os.environ, {"SSAI_KPIS_LIVE_ENABLED": "1", "SSAI_KPIS_ERP_KEY_READ_APPROVED": "1"}):
            page = AppTest.from_string("from app.ui.sims_panel import render_sims_main\nrender_sims_main({'category':'KPIS','action':'KPIS 조회'})")
            page.run(timeout=20)
            self.assertFalse(page.exception)
            page.date_input[0].set_value("2026-11-01")
            page.date_input[1].set_value("2026-11-24")
            self.assertFalse(page.checkbox)
            page.button[0].click().run(timeout=20)
            self.assertFalse(page.exception)
            page.run(timeout=20)
            self.assertFalse(page.exception)
            client.call.assert_called_once()
            push.assert_called_once()
            self.assertEqual(len(page.dataframe), 0)

    def test_admin_entry_removed_but_management_permission_unchanged(self):
        from app.ui import ssai_admin as admin
        user = SimpleNamespace(user_type="WHOLESALE_STAFF", user_grade="STAFF")
        for grants in (set(), {"MCP_EXTERNAL_RESOURCE_READ"}, {"EXTERNAL_API_VIEW"}):
            with patch.object(admin, "get_current_user", return_value=user), patch.object(admin, "has_permission", side_effect=lambda code: code in grants):
                page = AppTest.from_string("from app.ui.ssai_admin import render_ssai_admin_sidebar\nrender_ssai_admin_sidebar()")
                page.run(timeout=20)
                self.assertFalse(page.exception)
                labels = [b.label for b in page.button]
                self.assertNotIn("KPIS 조회", labels)
                self.assertEqual("외부 API 관리" in labels, "EXTERNAL_API_VIEW" in grants)
        old = AppTest.from_string("import streamlit as st\nfrom app.ui.ssai_admin import render_ssai_admin_page, SESSION_ADMIN_PAGE\nst.session_state[SESSION_ADMIN_PAGE]='kpis_erp'\nassert render_ssai_admin_page() is False\nassert SESSION_ADMIN_PAGE not in st.session_state")
        old.run(timeout=20)
        self.assertFalse(old.exception)


if __name__ == "__main__":
    unittest.main()
