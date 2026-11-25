"""Company identity pipeline without ERP/HTTP access; all identity values are synthetic."""
from __future__ import annotations

import unittest
from contextlib import ExitStack, nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from streamlit.testing.v1 import AppTest

from app.services import ssai_login_profile_service as profiles, kpis_erp_direct as direct
from app.services.external_api_management import ApiManagementError
from app.services.kpis_mcp_contract import Session
from app.ui import ssai_login as login, kpis_erp_query as ui
from tools.check_company_permission_refresh import _user


class OwnIdentityFix(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("pyodbc.connect", side_effect=AssertionError("DB forbidden")))
        self.stack.enter_context(patch("httpx.HTTPTransport.handle_request", side_effect=AssertionError("HTTP forbidden")))
        self.user = replace(_user(), user_id=42, user_type="SSART_ADMIN", sims_user_id=None)
        self.company = dict(company_id=8, is_active=True)

    def source(self, row):
        db, cursor = Mock(), Mock()
        db.cursor.return_value = cursor
        cursor.description = [(k,) for k in row]
        cursor.execute.return_value.fetchone.return_value = tuple(row.values())
        return db, cursor

    def row(self, **changes):
        return dict(dict(sims_profile_found=0, sims_user_name=None, own_company_rows=1,
                         own_vendor_rows=1, own_company_number="123-45-67890"), **changes)

    def profile(self, row, *, user=None):
        db, cursor = self.source(row)
        with patch.object(profiles, "connect_company_db", return_value=nullcontext(db)) as connect:
            result = profiles.build_login_profile(user=user or self.user, company=self.company)
        connect.assert_called_once_with(8)
        cursor.execute.assert_called_once()
        return result, cursor

    def test_unbound_erp_user_still_reads_company_once(self):
        result, cursor = self.profile(self.row())
        identity = result.pop(profiles.ERP_IDENTITY_KEY)
        self.assertEqual(identity.status, "verified")
        self.assertEqual(identity.applicant, "1234567890")
        self.assertEqual(result["profile_source"], "SSAI_FALLBACK")
        self.assertEqual(cursor.execute.call_args.args[1:], ("", "", ""))
        self.assertNotIn(identity.applicant, repr(result))
        self.assertEqual(result["sims_user_name"], "")

    def test_missing_r060_row_does_not_discard_company_identity(self):
        result, cursor = self.profile(self.row(), user=replace(self.user, sims_user_id="synthetic-missing"))
        self.assertEqual(result[profiles.ERP_IDENTITY_KEY].status, "verified")
        self.assertEqual(result["profile_error"], "SIMS_USER_PROFILE_NOT_FOUND")
        sql = cursor.execute.call_args.args[0]
        self.assertIn("FROM (SELECT 1 AS login_scope)", sql)
        self.assertIn("WHERE ? <> ''", sql)
        self.assertNotIn("WHERE RTRIM(LTRIM(U.Rd06_User_ID))", sql)
        self.assertNotIn("SELECT *", sql.upper())
        self.assertNotIn("PASSWORD", sql.upper())

    def test_employee_profile_and_existing_fields_preserved(self):
        row = self.row(sims_profile_found=1, sims_user_name="synthetic-employee", department_name="synthetic-dept")
        result, _ = self.profile(row, user=replace(self.user, sims_user_id="synthetic"))
        self.assertEqual(result["profile_source"], "SIMS_Rddbc060")
        self.assertEqual(result["sims_user_name"], "synthetic-employee")
        self.assertEqual(result["department_name"], "synthetic-dept")
        self.assertEqual(result[profiles.ERP_IDENTITY_KEY].status, "verified")

    def test_setting_and_vendor_failures_are_distinct_and_never_choose_duplicate(self):
        cases = [(0, 0, "missing"), (2, 1, "ambiguous"), (1, 0, "vendor_missing"),
                 (1, 2, "vendor_ambiguous"), (None, 1, "contract_invalid"), (1, None, "contract_invalid")]
        for settings, vendors, expected in cases:
            with self.subTest(expected=expected):
                identity = profiles.own_identity(8, 42, self.row(own_company_rows=settings, own_vendor_rows=vendors))
                self.assertEqual(identity.status, expected)
                self.assertEqual(identity.applicant, "")

    def test_invalid_numbers_block_only_kpis(self):
        for value in (None, "", "0000000000", "123456789", "１２３４５６７８９０", "123 45 67890"):
            with self.subTest(value=value):
                result, _ = self.profile(self.row(sims_profile_found=1, own_company_number=value))
                self.assertEqual(result["profile_source"], "SIMS_Rddbc060")
                self.assertEqual(result[profiles.ERP_IDENTITY_KEY].status, "invalid_number")

    def test_sql_failure_safe_diagnostics_and_login_fallback(self):
        with patch.object(profiles, "connect_company_db", side_effect=RuntimeError("private-key-number-server")), self.assertLogs(
                profiles.log, "WARNING") as captured:
            result = profiles.build_login_profile(user=self.user, company=self.company)
        self.assertEqual(result["profile_source"], "SSAI_FALLBACK")
        self.assertEqual(result[profiles.ERP_IDENTITY_KEY].status, "query_failed")
        self.assertIn("stage=profile_query reason=query_failed error_type=RuntimeError", captured.output[0])
        self.assertNotIn("private-key-number-server", repr(result) + " ".join(captured.output))

    def test_company_select_to_ma117_form_uses_private_session_without_reread(self):
        state = {login.SESSION_AUTH_USER: self.user}
        db, cursor = self.source(self.row())
        actor = Session(8, "42", 12345, frozenset(), frozenset(), False,
                        datetime.now(timezone.utc) + timedelta(seconds=30))
        with patch.object(login.st, "session_state", state), patch.object(login, "_refresh_selected_company_permissions", return_value=True), patch.object(
                login, "ensure_user_storage_dirs"), patch.object(profiles, "connect_company_db", return_value=nullcontext(db)) as connect:
            with self.assertLogs(level="INFO") as captured:
                login._after_company_selected(self.company)
            identity = state[profiles.ERP_IDENTITY_KEY]
            self.assertEqual(identity.status, "verified")
            self.assertNotIn(identity.applicant, repr(state[login.SESSION_LOGIN_PROFILE]))
            self.assertNotIn(identity.applicant, repr(state[login.SESSION_COMPANY]))
            self.assertNotIn(identity.applicant, " ".join(captured.output))
            self.assertTrue(any("stage=session_store present=True binding_matches=True" in line for line in captured.output))
            connect.side_effect = AssertionError("duplicate ERP profile read")
            self.assertEqual(direct.load_direct_approval(8, "MA117")["applicant"], identity.applicant)
            with patch.object(ui, "load_current_api_session", return_value=actor), patch.object(
                    ui, "configured_direct_client", side_effect=AssertionError("HTTP forbidden")):
                page = AppTest.from_string("from app.ui.kpis_erp_query import render_kpis_erp_query\nrender_kpis_erp_query()")
                page.run(timeout=20)
            self.assertFalse(page.exception)
            self.assertFalse(page.error)
            self.assertEqual(len(page.date_input), 2)
            self.assertFalse(page.checkbox)
            connect.assert_called_once_with(8)
            cursor.execute.assert_called_once()

    def test_company_switch_replaces_identity_and_rejects_old_binding(self):
        state = {login.SESSION_AUTH_USER: self.user}
        db, cursor = self.source(self.row())
        with patch.object(login.st, "session_state", state), patch.object(login, "_refresh_selected_company_permissions", return_value=True), patch.object(
                login, "ensure_user_storage_dirs"), patch.object(profiles, "connect_company_db", return_value=nullcontext(db)):
            login._after_company_selected(self.company)
            old = state[profiles.ERP_IDENTITY_KEY]
            cursor.execute.return_value.fetchone.return_value = tuple(self.row(own_company_number="9876543210").values())
            login._after_company_selected(dict(company_id=4, is_active=True))
            current = state[profiles.ERP_IDENTITY_KEY]
            self.assertEqual(current.company_id, 4)
            self.assertEqual(current.applicant, "9876543210")
            state[profiles.ERP_IDENTITY_KEY] = old
            with self.assertLogs(direct.log, "WARNING") as captured, self.assertRaises(ApiManagementError):
                direct.load_direct_approval(4, "MA117")
            self.assertIn("session_binding_mismatch", captured.output[0])
            self.assertNotIn(old.applicant, " ".join(captured.output))

    def test_consumer_logs_only_whitelisted_status_not_number_or_exception_text(self):
        state = {login.SESSION_AUTH_USER: self.user, login.SESSION_COMPANY: self.company}
        with patch.object(login.st, "session_state", state):
            for status in ("missing", "ambiguous", "vendor_missing", "invalid_number", "query_failed", "private-key-string"):
                state[profiles.ERP_IDENTITY_KEY] = profiles.ErpOwnIdentity(8, "42", status, "1234567890")
                with self.assertLogs(direct.log, "WARNING") as captured, self.assertRaises(ApiManagementError):
                    direct.load_direct_approval(8, "MA117")
                self.assertNotIn("private-key-string", " ".join(captured.output))
                self.assertNotIn("1234567890", " ".join(captured.output))

    def test_profile_builder_failure_clears_old_identity(self):
        state = {login.SESSION_AUTH_USER: self.user,
                 profiles.ERP_IDENTITY_KEY: profiles.ErpOwnIdentity(8, "42", "verified", "1234567890")}
        with patch.object(login.st, "session_state", state), patch.object(login, "_refresh_selected_company_permissions", return_value=True), patch.object(
                login, "ensure_user_storage_dirs"), patch.object(login, "build_login_profile", side_effect=ValueError("1234567890")), self.assertLogs(
                login.log, "WARNING") as captured:
            login._after_company_selected(dict(company_id=4, is_active=True))
        self.assertNotIn(profiles.ERP_IDENTITY_KEY, state)
        self.assertEqual(state[login.SESSION_LOGIN_PROFILE]["profile_source"], "ERROR")
        self.assertNotIn("1234567890", " ".join(captured.output) + repr(state[login.SESSION_LOGIN_PROFILE]))


if __name__ == "__main__":
    unittest.main()
