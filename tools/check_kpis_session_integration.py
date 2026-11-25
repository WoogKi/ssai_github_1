"""Focused session lifecycle checks; central DB/HTTP/Streamlit rerun are doubles."""
from __future__ import annotations

import unittest
from contextlib import ExitStack, nullcontext
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from app.services import external_api_management as management
from app.services import external_api_session as generation
from app.services import ssai_auth_service as auth
from app.services.kpis_rest_client import KpisClient
from app.ui import ssai_login as login
from tools.check_company_permission_refresh import _user
from tools.check_kpis_api_management import Gates


class SessionGate(unittest.TestCase):
    def setUp(self):
        self.user = replace(_user(), user_id=42, default_company_id=8)
        self.permissions = [management.VIEW, management.MANAGE, management.SECRET,
                            management.GRANT, management.AUDIT, "MCP_EXTERNAL_RESOURCE_READ", "EXTERNAL_API_TEST"]
        self.state = {login.SESSION_AUTH_USER: self.user, login.SESSION_COMPANY: {"company_id": 8},
                      login.SESSION_AUTH_PERMISSIONS: list(self.permissions)}
        self.row = asdict(self.user)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(login.st, "session_state", self.state))
        self.stack.enter_context(patch.object(login.st, "rerun"))
        self.db = Mock()
        self.stack.enter_context(patch.object(auth, "connect_ssai_db", return_value=self.db))
        self.read_user = self.stack.enter_context(patch.object(auth, "get_user_by_login_id", side_effect=lambda *a: self.row))
        self.read_permissions = self.stack.enter_context(patch.object(auth, "get_user_permissions", side_effect=lambda *a, **kw: self.permissions))
        self.membership = Mock(return_value=[4, 8])
        self.company_access = self.stack.enter_context(patch.object(
            auth, "get_active_companies_for_user", side_effect=lambda *a, **kw:
            [{"company_id": cid} for cid in self.membership()]))

    def load(self):
        return management.load_current_api_session()

    def select(self, company):
        with patch.object(login, "connect_ssai_db", return_value=nullcontext(object())), patch.object(
                login, "get_user_permissions", side_effect=lambda *a, **kw: self.permissions), patch.object(
                login, "ensure_user_storage_dirs"), patch.object(login, "build_login_profile", return_value={}):
            login._after_company_selected({"company_id": company})

    def fixture(self):
        fixture = Gates(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_epoch_stable_within_session_and_not_one(self):
        a, b = self.load(), self.load()
        self.assertEqual(a.epoch, b.epoch)
        self.assertGreater(a.epoch, 1)
        self.db.close.assert_called()
        self.read_permissions.assert_called_with(self.db, user_id=42, company_id=8)

    def test_logout_same_user_relogin_never_revives_old_epoch(self):
        before = self.load()
        self.state["__external_api_secret"] = "synthetic"
        with patch.object(login, "_clear_company_dependent_state"):
            login.logout()
        self.assertIsNone(self.load())
        self.assertNotIn("__external_api_secret", self.state)
        login._complete_login(SimpleNamespace(success=True, user=self.user, permissions=self.permissions))
        self.select(8)
        self.assertNotEqual(before.epoch, self.load().epoch)

    def test_company_roundtrip_even_same_physical_db(self):
        a = self.load()
        self.select(4)
        b = self.load()
        self.select(8)
        c = self.load()
        self.assertEqual(len({a.epoch, b.epoch, c.epoch}), 3)
        self.assertEqual(c.company_id, a.company_id)

    def test_user_switch_invalidates_binding(self):
        a = self.load()
        self.state[login.SESSION_AUTH_USER] = replace(self.user, user_id=43)
        self.row = dict(asdict(self.user), user_id=43)
        b = self.load()
        self.assertNotEqual(a.epoch, b.epoch)
        self.assertEqual(b.user_id, "43")

    def test_central_permission_revocation_and_restore_rotate(self):
        a = self.load()
        self.permissions.remove("MCP_EXTERNAL_RESOURCE_READ")
        b = self.load()
        self.permissions.append("MCP_EXTERNAL_RESOURCE_READ")
        c = self.load()
        self.assertEqual(len({a.epoch, b.epoch, c.epoch}), 3)
        self.assertNotIn("MCP_EXTERNAL_RESOURCE_READ", b.permissions)
        # API fresh permissions do not mutate other modules' cached permission source.
        self.assertEqual(set(self.state[login.SESSION_AUTH_PERMISSIONS]), set(self.permissions))

    def test_disabled_user_or_company_link_and_restore(self):
        for field in ("is_active", "approval_status", "membership"):
            with self.subTest(field=field):
                a = self.load()
                if field == "membership": self.membership.return_value = []
                else: self.row[field] = False if field == "is_active" else "REJECTED"
                self.assertIsNone(self.load())
                self.row.update(is_active=True, approval_status="APPROVED")
                self.membership.return_value = [4, 8]
                self.assertNotEqual(a.epoch, self.load().epoch)

    def test_auth_db_failure_revokes_and_closes(self):
        a = self.load()
        self.read_permissions.side_effect = RuntimeError("synthetic-sensitive-error")
        self.assertIsNone(self.load())
        self.db.close.assert_called()
        self.read_permissions.side_effect = lambda *a, **kw: self.permissions
        self.assertNotEqual(a.epoch, self.load().epoch)

    def test_company_selection_mode_denies_until_verified(self):
        a = self.load()
        self.state[login.SESSION_COMPANY_PICK_MODE] = True
        self.assertIsNone(self.load())
        self.state[login.SESSION_COMPANY_PICK_MODE] = False
        self.assertNotEqual(a.epoch, self.load().epoch)

    def test_company_switch_during_central_read_cannot_bind_old_result(self):
        self.load()
        def switch(*args, **kwargs):
            generation.invalidate(self.state)
            self.state[login.SESSION_COMPANY] = {"company_id": 4}
            return self.permissions
        self.read_permissions.side_effect = switch
        self.assertIsNone(self.load())

    def test_refresh_failure_existing_login_hook_invalidates(self):
        a = self.load()
        with patch.object(login, "connect_ssai_db", side_effect=RuntimeError("synthetic")):
            self.assertFalse(login._refresh_selected_company_permissions(self.user, {"company_id": 8}))
        self.assertEqual(self.state[login.SESSION_AUTH_PERMISSIONS], [])
        self.assertNotEqual(a.epoch, self.load().epoch)

    def test_old_request_after_roundtrip_is_blocked_without_http(self):
        f = self.fixture()
        old = replace(f.request, session_epoch=self.load().epoch)
        self.select(4)
        self.select(8)
        client = KpisClient(f.store, self.load, enabled=True, transport=httpx.MockTransport(f.handler))
        result = client.call(old, f.query, explicit=True)
        self.assertEqual(result["physical_call_count"], 0)
        self.assertEqual(f.sent, [])

    def test_current_verified_session_can_call_ma117_synthetic_once(self):
        f = self.fixture()
        actor = self.load()
        request = replace(f.request, session_epoch=actor.epoch)
        result = KpisClient(f.store, self.load, enabled=True, transport=httpx.MockTransport(f.handler)).call(request, f.query, explicit=True)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["physical_call_count"], 1)
        self.assertEqual(self.load().epoch, actor.epoch)

    def test_central_revoke_after_jwt_blocks_service_query(self):
        f = self.fixture()
        f.configure("MA116")
        request = replace(f.request, service="MA116", session_epoch=self.load().epoch)
        def handler(req):
            response = f.handler(req)
            self.permissions.remove("MCP_EXTERNAL_RESOURCE_READ")
            return response
        result = KpisClient(f.store, self.load, enabled=True, transport=httpx.MockTransport(handler)).call(
            request, {"rcvYr": "2026", "rcvQt": "3"}, explicit=True)
        self.assertEqual(result["status"], "DENIED")
        self.assertEqual(result["physical_call_count"], 1)
        self.assertEqual(len(f.sent), 1)

    def test_new_browser_session_has_different_generation(self):
        original = self.load()
        other = {login.SESSION_AUTH_USER: self.user, login.SESSION_COMPANY: {"company_id": 8}}
        with patch.object(login.st, "session_state", other):
            new = self.load()
        self.assertNotEqual(original.epoch, new.epoch)
        self.assertEqual(original.epoch, self.load().epoch)

    def test_revoke_before_erp_read_is_blocked(self):
        f = self.fixture()
        f.store.set_credentials(f.actor, 8, "MA117", "PRODUCTION", {})
        f.store.set_erp_reference(f.actor, 8, "MA117", "PRODUCTION")
        f.store.configure(f.actor, 8, "MA117", "PRODUCTION", applicant="0000000000", enabled=True,
                          expires=f.until, evidence="synthetic")
        request = replace(f.request, session_epoch=self.load().epoch)
        invocations = []
        def load():
            invocations.append(1)
            if len(invocations) == 3: self.permissions.remove("MCP_EXTERNAL_RESOURCE_READ")
            return self.load()
        key_loader = Mock(return_value="synthetic-key")
        result = KpisClient(f.store, load, enabled=True, erp_key_loader=key_loader,
                            transport=httpx.MockTransport(f.handler)).call(request, f.query, explicit=True)
        key_loader.assert_not_called()
        self.assertEqual(result["physical_call_count"], 0)
        self.assertEqual(result["credential_read_count"], 0)

    def test_logout_after_http_discards_response(self):
        f = self.fixture()
        request = replace(f.request, session_epoch=self.load().epoch)
        def handler(req):
            response = f.handler(req)
            with patch.object(login, "_clear_company_dependent_state"):
                login.logout()
            return response
        result = KpisClient(f.store, self.load, enabled=True, transport=httpx.MockTransport(handler)).call(request, f.query, explicit=True)
        self.assertEqual(result["physical_call_count"], 1)
        self.assertEqual(result["status"], "DENIED")
        self.assertEqual(result["rows"], [])

    def test_actual_admin_menu_has_no_legacy_admin_bypass(self):
        from app.ui import ssai_admin as admin
        labels = []
        fake = SimpleNamespace(session_state={}, sidebar=SimpleNamespace(expander=lambda *a, **k: nullcontext()),
                               button=lambda label, **kw: labels.append(label) or False)
        for permissions, expected in (({"USER_MANAGE_ALL"}, False), ({management.VIEW}, True)):
            labels.clear()
            with patch.object(admin, "st", fake), patch.object(admin, "get_current_user", return_value=self.user), patch.object(
                    admin, "has_permission", side_effect=lambda p: p in permissions):
                admin.render_ssai_admin_sidebar()
            self.assertEqual("외부 API 관리" in labels, expected)

    def test_form_scope_includes_epoch(self):
        from app.ui.external_api_admin import clear_changed_scope
        self.state["__external_api_scope"] = ("42", 8, 10)
        self.state["__external_api_42_8_10_secret"] = "synthetic"
        clear_changed_scope(self.state, ("42", 8, 20))
        self.assertNotIn("__external_api_42_8_10_secret", self.state)
        self.assertIn(login.SESSION_AUTH_USER, self.state)


if __name__ == "__main__":
    unittest.main()
