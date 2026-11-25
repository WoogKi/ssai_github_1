"""Environment-independent ERP-direct reads; all credentials and HTTP are synthetic."""
from contextlib import contextmanager
from dataclasses import replace
from itertools import product
import os
import unittest
from unittest.mock import Mock, patch

import httpx
from streamlit.testing.v1 import AppTest

from app.services import kpis_erp_direct as direct
from app.services.external_api_management import ApiManagementError
from app.services.kpis_mcp_contract import preflight
from app.services.kpis_rest_client import KpisClient
from app.ui import kpis_erp_query as ui
from tools import check_kpis_erp_direct as existing

FLAGS = ('SSAI_KPIS_LIVE_ENABLED', 'SSAI_KPIS_ERP_KEY_READ_APPROVED')
QUERIES = {'MA117': {'srchStaDd': '20261101', 'srchEndDd': '20261124'},
           'MA101': {'msupStdCd': '0000000000001'}, 'MA102': {'msupSplyFrmCd': '5', 'tkrHbin': '0000000001'},
           'MA110': {'splyDd': '20261101'}}


class DirectGateChecks(unittest.TestCase):
    def setUp(self):
        self.f = existing.DirectChecks(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    @contextmanager
    def configured(self, live=None, key=None):
        access = direct.ErpDirectAccess
        with patch.dict(os.environ):
            for name, value in zip(FLAGS, (live, key)):
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            with patch.object(direct, 'ErpDirectAccess', side_effect=lambda: access(audit=self.f.events.append)), patch.object(
                    direct, 'load_current_api_session', side_effect=lambda: self.f.actor), patch.object(
                    direct, 'load_erp_api_key', self.f.key):
                client = direct.configured_direct_client()
                client.transport = httpx.MockTransport(self.f.handler)
                yield client

    def test_all_nine_flag_states_and_four_read_services_use_real_factory(self):
        for live, key in product((None, '0', '1'), repeat=2):
            for service, query in QUERIES.items():
                with self.subTest(live=live, key=key, service=service), self.configured(live, key) as client:
                    result = client.call(replace(self.f.req, service=service), query, explicit=True)
                    self.assertEqual(result['status'], 'SUCCESS')
                    self.assertEqual((result['physical_call_count'], result['credential_read_count'], result['retry_count']), (1, 1, 0))
                    self.f.key.assert_called_with(8, service)
        self.assertEqual(len(self.f.sent), 36)
        self.assertEqual(self.f.key.call_count, 36)

    def test_unauthenticated_mismatched_and_expired_sessions_never_read_key(self):
        from datetime import datetime, timedelta, timezone
        actor = self.f.actor
        for changed in (None, replace(actor, company_id=4), replace(actor, user_id='43'), replace(actor, epoch=9),
                        replace(actor, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))):
            self.f.actor = changed
            with self.configured() as client:
                result = client.call(self.f.req, self.f.query, explicit=True)
                self.assertEqual(result['status'], 'DENIED')
        self.f.key.assert_not_called()
        self.assertFalse(self.f.sent)

    def test_inactive_company_and_invalid_identity_still_block(self):
        from app.services.ssai_login_profile_service import ERP_IDENTITY_KEY
        for state in ({'is_active': False},):
            with patch.dict(self.f.company, state), self.configured() as client:
                self.assertEqual(client.call(self.f.req, self.f.query, explicit=True)['status'], 'DENIED')
        with patch.dict(self.f.state, {ERP_IDENTITY_KEY: None}), self.configured() as client:
            self.assertEqual(client.call(self.f.req, self.f.query, explicit=True)['status'], 'DENIED')
        self.f.key.assert_not_called()

    def test_explicit_and_query_validation_before_key(self):
        with self.configured() as client:
            for query, explicit in ((self.f.query, False), ({'srchStaDd': '20260230', 'srchEndDd': '20260301'}, True)):
                self.assertEqual(client.call(self.f.req, query, explicit=explicit)['status'], 'DENIED')
        self.f.key.assert_not_called()

    def test_key_errors_are_safe_and_no_http(self):
        for reason in ('erp_key_missing_or_duplicate', 'erp_key_invalid', 'erp_key_unavailable'):
            self.f.key.side_effect = ApiManagementError(reason)
            with self.configured() as client:
                result = client.call(self.f.req, self.f.query, explicit=True)
            self.assertEqual(result['reason_code'], reason)
            self.assertEqual(result['physical_call_count'], 0)
            self.assertNotIn('synthetic-erp-key-only', str(result))
            self.assertNotIn(reason, ui.call_error_message(reason))
        self.assertFalse(self.f.sent)
        self.assertNotIn('private-value', ui.call_error_message('private-value'))

    def test_company_switch_after_key_and_revoke_after_response(self):
        actor = self.f.actor
        def switch(*args):
            self.f.actor = replace(actor, company_id=4, epoch=99)
            return 'synthetic-only'
        self.f.key.side_effect = switch
        with self.configured() as client:
            self.assertEqual(client.call(self.f.req, self.f.query, explicit=True)['physical_call_count'], 0)
        self.f.actor = actor
        self.f.key.side_effect = None
        def revoke(request):
            response = self.f.handler(request)
            self.f.actor = None
            return response
        with self.configured() as client:
            client.transport = httpx.MockTransport(revoke)
            result = client.call(self.f.req, self.f.query, explicit=True)
        self.assertEqual(result['status'], 'DENIED')
        self.assertEqual(result['rows'], [])

    def test_jwt_write_and_test_modes_remain_denied(self):
        with self.configured('1', '1') as client:
            for service in ('MA116', 'MA111', 'MA113', 'MA118'):
                result = client.call(replace(self.f.req, service=service), {}, explicit=True)
                self.assertEqual(result['reason_code'], 'erp_direct_service_not_supported')
            self.assertEqual(client.call(replace(self.f.req, mode='TEST'), self.f.query, explicit=True)['status'], 'DENIED')
        self.f.key.assert_not_called()

    def test_non_direct_feature_gate_and_mcp_permission_unchanged(self):
        store = Mock(spec=['execution_profile', 'audit'])
        client = KpisClient(store, lambda: self.f.actor, enabled=False, erp_key_loader=self.f.key)
        result = client.call(self.f.req, self.f.query, explicit=True)
        self.assertEqual(result['reason_code'], 'feature_disabled')
        store.execution_profile.assert_not_called()
        result = preflight(self.f.req, load_session=lambda: self.f.actor, approval=None,
                           feature_enabled=True, explicit_request=True)
        self.assertEqual(result['reason_code'], 'permission_denied')
        self.f.key.assert_not_called()

    def test_no_environment_ui_submit_once_and_rerun_no_http(self):
        with self.configured() as client, patch.object(ui, 'configured_direct_client', return_value=client) as factory, patch.object(
                ui, 'load_current_api_session', side_effect=lambda: self.f.actor), patch('app.ui.chat_middleware.wssz', return_value={}) as push:
            page = AppTest.from_string('from app.ui.kpis_erp_query import render_kpis_erp_query\nrender_kpis_erp_query()').run(timeout=20)
            self.assertFalse(page.exception)
            self.assertFalse(page.button[0].disabled)
            self.assertFalse(page.info)
            page.date_input[0].set_value(None)
            page.date_input[1].set_value(None)
            page.button[0].click().run(timeout=20)
            self.assertTrue(page.error)
            factory.assert_not_called()
            page.date_input[0].set_value('2026-11-01')
            page.date_input[1].set_value('2026-11-24')
            page.button[0].click().run(timeout=20)
            self.assertFalse(page.error)
            self.assertFalse(page.exception)
            page.run(timeout=20)
            factory.assert_called_once()
            push.assert_called_once()
            self.assertEqual(len(self.f.sent), 1)
            self.assertEqual([e['action'] for e in self.f.events], ['ERP_KEY_READ', 'CALL_START', 'CALL_END'])


if __name__ == '__main__':
    unittest.main()
