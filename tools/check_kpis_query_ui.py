"""Calendar and button-submit contracts; only synthetic ERP keys and mock HTTP."""
from __future__ import annotations

import json
import os
import unittest
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import Mock, patch
from types import SimpleNamespace

import httpx
import pandas as pd
from datetime import date
from app.services.business_calendar_service import RecentBusinessDaysResult
from streamlit.testing.v1 import AppTest
from streamlit.runtime.state import SessionStateProxy

from app.services import kpis_erp_direct as direct
from app.services.kpis_rest_client import KpisClient, validate_query
from app.services.external_api_management import ApiManagementError
from app.services.ssai_login_profile_service import ERP_IDENTITY_KEY
from app.ui import kpis_erp_query as ui, ssai_login as login
from tools import check_kpis_erp_direct as checks


class QueryUI(unittest.TestCase):
    def setUp(self):
        self.f = checks.DirectChecks(methodName="runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        for guard in (
            patch.object(ui, "kst_today", return_value=date(2026, 11, 25)),
            patch.object(ui, "recent_business_days", return_value=RecentBusinessDaysResult(status="ready", dates=("20261124",))),
            patch("app.services.rddbc040_service.lookup_standard_code_products", return_value=pd.DataFrame()),
        ):
            guard.start()
            self.addCleanup(guard.stop)

    @contextmanager
    def screen(self, live="1", key="1", service="MA117"):
        f = self.f
        client = KpisClient(direct.ErpDirectAccess(audit=f.events.append), lambda: f.actor,
                            enabled=True, erp_key_loader=f.key,
                            transport=httpx.MockTransport(f.handler))
        push = Mock(return_value={})
        with patch.object(login, "st", SimpleNamespace(session_state=f.state)), patch(
                "streamlit.session_state", SessionStateProxy()), patch.object(
                ui, "load_current_api_session", side_effect=lambda: f.actor), patch.object(
                ui, "configured_direct_client", return_value=client) as factory, patch(
                "app.ui.chat_middleware.wssz", push), patch.dict(os.environ, {
                    "SSAI_KPIS_LIVE_ENABLED": live, "SSAI_KPIS_ERP_KEY_READ_APPROVED": key}):
            page = AppTest.from_string(f"from app.ui.kpis_erp_query import render_kpis_erp_query\nrender_kpis_erp_query({service!r})")
            page.run(timeout=20)
            self.assertFalse(page.exception)
            yield page, factory, push

    def dates(self, page, start="2026-11-23", end="2026-11-24"):
        page.date_input[0].set_value(start)
        page.date_input[1].set_value(end)

    def test_calendar_format_wire_dates_and_single_publish(self):
        with self.screen() as (page, factory, push):
            self.assertEqual(len(page.date_input), 2)
            self.assertFalse(page.text_input)
            self.assertFalse(page.checkbox)
            self.assertTrue(all(x.proto.format == "YYYY-MM-DD" for x in page.date_input))
            self.assertEqual([x.value for x in page.date_input], [date(2026, 11, 18), date(2026, 11, 25)])
            self.dates(page)
            page.run(timeout=20)
            factory.assert_not_called()
            page.button[0].click().run(timeout=20)
            self.assertFalse(page.exception)
            self.assertFalse(page.error)
            self.assertEqual(len(self.f.sent), 1)
            self.f.key.assert_called_once_with(8, "MA117")
            body = json.loads(self.f.sent[0].content)
            self.assertEqual((body["srchStaDd"], body["srchEndDd"]), ("20261123", "20261124"))
            push.assert_called_once()
            self.assertIn("2026-11-23", push.call_args.args[0]["meta"]["query_summary"])
            self.assertEqual(push.call_args.args[0]["params"]["srchStaDd"], "20261123")
            for _ in range(2):
                page.run(timeout=20)
            factory.assert_called_once()
            self.assertEqual(len(self.f.sent), 1)
            push.assert_called_once()
            self.assertFalse(page.dataframe)

    def test_legacy_flags_do_not_disable_authenticated_direct_form(self):
        for live, key in (("0", "0"), ("1", "0"), ("0", "1"), ("1", "1")):
            with self.subTest(live=live, key=key), self.screen(live, key) as (page, factory, push):
                self.assertFalse(page.button[0].disabled)
                self.assertFalse(page.info)
                self.dates(page)
                factory.assert_not_called()
                push.assert_not_called()
                page.button[0].click().run(timeout=20)
                self.assertFalse(page.exception)
                self.assertFalse(page.error)
                push.assert_called_once()
        self.assertEqual(self.f.key.call_count, 4)
        self.assertEqual(len(self.f.sent), 4)

    def test_empty_and_reversed_dates_block_before_client_creation(self):
        with self.screen() as (page, factory, push):
            page.date_input[0].set_value(None)
            page.date_input[1].set_value(None)
            page.button[0].click().run(timeout=20)
            self.assertIn("필수 조회조건", page.error[0].value)
            self.dates(page, "2026-11-24", "2026-11-23")
            page.button[0].click().run(timeout=20)
            self.assertIn("시작일", page.error[0].value)
            factory.assert_not_called()
            push.assert_not_called()
        self.f.key.assert_not_called()

    def test_same_day_leap_date_uses_existing_validator(self):
        with self.screen() as (page, _, push):
            self.dates(page, "2024-02-29", "2024-02-29")
            page.button[0].click().run(timeout=20)
            self.assertFalse(page.error)
            push.assert_called_once()
            self.assertEqual(json.loads(self.f.sent[0].content)["srchEndDd"], "20240229")
        with self.assertRaises(ApiManagementError):
            validate_query("MA117", dict(srchStaDd="20260229", srchEndDd="20260301"))

    def test_ma110_single_date_and_other_conditions_preserved(self):
        with self.screen(service="MA110") as (page, _, push):
            self.assertEqual(len(page.date_input), 1)
            self.assertEqual(len(page.text_input), 2)
            page.date_input[0].set_value("2026-11-23")
            next(v for v in page.button if v.label == "KPIS 조회").click().run(timeout=20)
            self.assertFalse(page.exception)
            self.assertFalse(page.error)
            self.assertEqual(json.loads(self.f.sent[0].content)["splyDd"], "20261123")
            push.assert_called_once()

    def test_old_text_widget_state_is_not_reused_as_calendar(self):
        self.f.state["__external_api_scope"] = (8, "42", 12345)
        self.f.state["__external_api_direct_8_42_12345MA117srchStaDd"] = "20261123"
        with self.screen() as (page, factory, _):
            self.assertEqual([x.value for x in page.date_input], [date(2026, 11, 18), date(2026, 11, 25)])
            self.assertFalse(page.exception)
            factory.assert_not_called()

    def test_company_change_resets_dates_and_revoked_actor_blocks(self):
        with self.screen() as (page, factory, push):
            self.dates(page)
            page.run(timeout=20)
            self.f.actor = replace(self.f.actor, company_id=4, epoch=54321)
            self.f.state[login.SESSION_COMPANY] = dict(company_id=4, is_active=True)
            self.f.state[ERP_IDENTITY_KEY] = replace(self.f.identity, company_id=4)
            page.run(timeout=20)
            self.assertFalse(page.exception)
            self.assertEqual([x.value for x in page.date_input], [date(2026, 11, 18), date(2026, 11, 25)])
            self.f.actor = None
            page.run(timeout=20)
            self.assertTrue(page.error)
            self.assertFalse(page.button)
            factory.assert_not_called()
            push.assert_not_called()


if __name__ == "__main__":
    unittest.main()
