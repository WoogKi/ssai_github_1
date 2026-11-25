"""Central selector/API parity and MA117 rendering, without DB or HTTP access."""
from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import MagicMock, Mock, patch

from streamlit.testing.v1 import AppTest

from app.services import external_api_management as management
from app.services import ssai_auth_service as auth
from app.services.ssai_login_profile_service import ERP_IDENTITY_KEY, ErpOwnIdentity
from app.ui import kpis_erp_query as ui, ssai_login as login
from tools.check_kpis_session_integration import SessionGate


SELECT_COMPANIES = auth.get_active_companies_for_user


class SessionAuth(unittest.TestCase):
    def fixture(self):
        f = SessionGate(methodName="runTest")
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.permissions[:] = []
        f.state[login.SESSION_COMPANY]["is_active"] = True
        f.state[ERP_IDENTITY_KEY] = ErpOwnIdentity(8, "42", "verified", "1234567890")
        f.company_access.side_effect = SELECT_COMPANIES
        f.linked = set()
        f.active = {8, 4}
        f.sql = []
        cursor = Mock()
        cursor.description = [("company_id",)]

        def execute(sql, *args):
            f.sql.append(sql)
            self.assertIn("is_active = 1", sql)
            ids = f.active
            if "SSAI_USER_COMPANIES" in sql:
                self.assertEqual(args, (42,))
                self.assertIn("uc.is_active = 1", sql)
                self.assertIn("c.is_active = 1", sql)
                ids = ids & f.linked
            else:
                self.assertEqual(args, ())
            cursor.fetchall.return_value = [(cid,) for cid in sorted(ids)]
            return cursor

        cursor.execute.side_effect = execute
        f.db.cursor.return_value = cursor
        return f

    def test_global_access_without_direct_membership_matches_selector(self):
        f = self.fixture()
        f.permissions[:] = ["SIMS_DB_ACCESS_ALL"]
        self.assertFalse(f.linked)
        # The old API membership-only gate rejects this normal company-selection case.
        self.assertNotIn(8, f.linked)
        selected = SELECT_COMPANIES(auth.make_auth_user(f.row), conn=f.db)
        self.assertIn(8, [c["company_id"] for c in selected])
        actor = f.load()
        self.assertIsNotNone(actor)
        self.assertNotIn("MCP_EXTERNAL_RESOURCE_READ", actor.permissions)
        self.assertNotIn("SSAI_USER_COMPANIES", f.sql[-1])
        auth.connect_ssai_db.assert_called_once()
        f.db.close.assert_called_once()

    def test_direct_membership_requires_active_company(self):
        f = self.fixture()
        f.linked = {8}
        self.assertIsNotNone(f.load())
        self.assertIn("SSAI_USER_COMPANIES", f.sql[-1])
        f.active.remove(8)
        self.assertIsNone(f.load())

    def test_global_grant_revocation_rejects_cached_login_permission(self):
        f = self.fixture()
        f.permissions[:] = ["SIMS_DB_ACCESS_ALL"]
        f.state[login.SESSION_AUTH_PERMISSIONS] = list(f.permissions)
        before = f.load()
        f.permissions.clear()
        self.assertIsNone(f.load())
        f.permissions.append("SIMS_DB_ACCESS_ALL")
        self.assertNotEqual(before.epoch, f.load().epoch)

    def test_fresh_role_not_cached_admin_and_no_new_admin_bypass(self):
        f = self.fixture()
        f.state[login.SESSION_AUTH_USER] = replace(f.user, user_type="SSART_ADMIN")
        self.assertIsNone(f.load())
        # Preserve only the existing selector's legacy role policy, including active-company filtering.
        f.row["user_type"] = "SSART_ADMIN"
        self.assertIsNotNone(f.load())
        f.active.remove(8)
        self.assertIsNone(f.load())

    def test_missing_inactive_unapproved_and_wrong_user_denied(self):
        f = self.fixture()
        f.permissions[:] = ["SIMS_DB_ACCESS_ALL"]
        original = dict(f.row)
        cases = [(None, "user_missing"), (dict(original, is_active=False), "user_inactive"),
                 (dict(original, approval_status="REJECTED"), "user_unapproved"),
                 (dict(original, user_id=99), "user_mismatch")]
        for row, reason in cases:
            f.row = row
            with self.subTest(reason=reason), self.assertLogs(management.log, "WARNING") as captured:
                self.assertIsNone(f.load())
            self.assertIn("reason=" + reason, captured.output[0])

    def test_safe_exception_diagnostics_close_and_invalidate(self):
        f = self.fixture()
        f.linked = {8}
        before = f.load()
        f.read_permissions.side_effect = RuntimeError("secret-user-business-key-database")
        with self.assertLogs(management.log, "WARNING") as captured:
            self.assertIsNone(f.load())
        self.assertIn("stage=company_access", captured.output[0])
        self.assertIn("error_type=RuntimeError", captured.output[0])
        self.assertNotIn("secret-user-business-key-database", " ".join(captured.output))
        self.assertNotIn("1234567890", " ".join(captured.output))
        f.db.close.assert_called()
        f.read_permissions.side_effect = lambda *a, **kw: f.permissions
        self.assertNotEqual(before.epoch, f.load().epoch)

    def test_company_switch_while_resolving_access_rejects_old_session(self):
        f = self.fixture()
        f.linked = {8}
        def changed(user, *, conn):
            rows = SELECT_COMPANIES(user, conn=conn)
            f.state[login.SESSION_COMPANY] = {"company_id": 4, "is_active": True}
            return rows
        f.company_access.side_effect = changed
        with self.assertLogs(management.log, "WARNING") as captured:
            self.assertIsNone(f.load())
        self.assertIn("reason=session_changed", captured.output[0])

    def test_ma117_form_with_real_session_gate_without_mcp_or_live_calls(self):
        f = self.fixture()
        with patch.object(ui, "configured_direct_client", side_effect=AssertionError("HTTP forbidden")), patch.object(
                management, "load_erp_api_key", side_effect=AssertionError("ERP forbidden")), patch.object(
                auth, "connect_company_db", side_effect=AssertionError("ERP forbidden")):
            for role, grants, links in (("WHOLESALE_ADMIN", ["SIMS_DB_ACCESS_ALL"], set()),
                                        ("SSART_ADMIN", [], set()), ("WHOLESALE_USER", [], {8})):
                with self.subTest(role=role):
                    f.row["user_type"], f.linked = role, links
                    f.permissions[:] = grants
                    page = AppTest.from_string("from app.ui.kpis_erp_query import render_kpis_erp_query\nrender_kpis_erp_query()")
                    page.run(timeout=20)
                    self.assertFalse(page.exception)
                    self.assertFalse(page.error)
                    self.assertEqual(len(page.date_input), 2)
                    self.assertEqual({x.label for x in page.date_input}, {ui.LABELS[k] for k in ui.INPUTS["MA117"]})
                    self.assertFalse(page.checkbox)
                    self.assertEqual(page.button[0].label, "KPIS 조회")

    def test_existing_selector_default_connection_path_unchanged(self):
        f = self.fixture()
        f.linked = {8}
        context = MagicMock()
        context.__enter__.return_value = f.db
        with patch.object(auth, "connect_ssai_db", return_value=context) as connect:
            self.assertEqual(SELECT_COMPANIES(f.user), [{"company_id": 8}])
            connect.assert_called_once_with()
            context.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
