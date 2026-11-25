"""Focused activation UI checks using temporary storage and synthetic sessions only."""
from __future__ import annotations

import os
import unittest
from dataclasses import replace
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.services import external_api_management as management
from tools import check_kpis_api_management as existing


class ActivationUI(unittest.TestCase):
    def setUp(self):
        self.fixture = existing.Gates()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.actor = replace(self.fixture.actor, epoch=12345)
        for target in ("app.services.ssai_auth_service.connect_ssai_db",
                       "app.services.ssai_auth_service.connect_company_db",
                       "httpx.Client.send"):
            guard = patch(target, side_effect=AssertionError("live_access_forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        for guard in (
            patch.object(management, "load_current_api_session", side_effect=lambda: self.actor),
            patch.object(management, "configured_store", return_value=self.fixture.store),
            patch.dict(os.environ, {"SSAI_KPIS_LIVE_ENABLED": "0",
                                    "SSAI_KPIS_TEST_HTTP_APPROVED": "0",
                                    "SSAI_KPIS_ERP_KEY_READ_APPROVED": "0"}),
        ):
            guard.start()
            self.addCleanup(guard.stop)
        self.page = AppTest.from_string(
            "from app.ui.external_api_admin import render_external_api_admin\n"
            "render_external_api_admin()"
        )
        self.run_page()

    def run_page(self):
        self.page.run(timeout=15)
        self.assertEqual(len(self.page.exception), 0)

    def widget(self, kind, label):
        return next(w for w in getattr(self.page, kind) if w.label == label)

    def production(self):
        self.widget("radio", "환경").set_value("PRODUCTION")
        self.run_page()

    def test_profile_activation_and_disabled_live_button(self):
        self.production()
        self.widget("text_input", "API 신청 사업자번호 / 요양기관기호").set_value("0000000000")
        self.widget("text_input", "이용 승인 근거 식별자").set_value("synthetic-ui-approval")
        self.widget("checkbox", "서비스 활성화").check()
        self.widget("button", "프로필 저장").click()
        self.run_page()
        self.assertTrue(self.fixture.store.view(self.actor, 8)[0]["enabled"])
        self.assertTrue(self.widget("button", "API 조회").disabled)

    def test_erp_reference_and_history_without_erp_read(self):
        self.fixture.store.set_credentials(self.actor, 8, "MA117", "PRODUCTION", {})
        self.production()
        self.widget("radio", "인증정보 저장 방식").set_value("기존 ERP 참조")
        self.run_page()
        self.widget("button", "기존 ERP API KEY 참조 설정").click()
        self.run_page()
        profile = self.fixture.store.view(self.actor, 8)[0]
        self.assertEqual(profile["credential_source"], "ERP_RDDBC0A4")
        self.assertFalse(profile["enabled"])
        self.run_page()
        self.assertTrue(any("ERP_REFERENCE" in frame.value.to_string() for frame in self.page.dataframe))
        self.assertTrue(all(row["calls"] == 0 for row in self.fixture.store.history(self.actor, 8)))

    def test_jwt_form_encrypted_storage_without_token_issue(self):
        self.fixture.configure("MA116")
        self.page.selectbox[0].set_value("MA116")
        self.run_page()
        self.production()
        self.widget("text_input", "JWT Client ID").set_value("synthetic-ui-client")
        self.widget("text_input", "JWT Client Secret").set_value("synthetic-ui-secret")
        self.widget("button", "인증정보 등록·교체").click()
        self.run_page()
        profile = next(p for p in self.fixture.store.view(self.actor, 8) if p["service"] == "MA116")
        self.assertTrue(profile["credential_registered"])
        self.assertFalse(profile["enabled"])
        self.assertNotIn(b"synthetic-ui-secret", self.fixture.path.read_bytes())
        self.assertTrue(all(row["calls"] == 0 for row in self.fixture.store.history(self.actor, 8)))

    def test_user_grant_visible_and_revocable(self):
        self.production()
        self.widget("text_input", "사용자 ID").set_value("43")
        self.widget("checkbox", "서비스 사용 허용").check()
        self.widget("button", "사용 권한 저장").click()
        self.run_page()
        self.assertIn("43", [r["user"] for r in self.fixture.store.grants_view(self.actor, 8)])
        self.widget("text_input", "사용자 ID").set_value("43")
        self.widget("checkbox", "서비스 사용 허용").uncheck()
        self.widget("button", "사용 권한 저장").click()
        self.run_page()
        self.assertNotIn("43", [r["user"] for r in self.fixture.store.grants_view(self.actor, 8)])

    def test_company_switch_clears_forms_and_hides_previous_profiles(self):
        self.widget("text_input", "API KEY").set_value("synthetic-unsaved")
        self.run_page()
        self.actor = replace(self.actor, company_id=4, epoch=54321)
        self.run_page()
        self.assertEqual(len(self.page.dataframe[0].value), 0)
        self.assertEqual(self.widget("text_input", "API KEY").value, "")
        self.assertTrue(any(c.value == "회사 4" for c in self.page.caption))
        self.assertEqual(len(self.fixture.store.view(self.fixture.actor, 8)), 1)

    def test_view_only_has_no_secret_grant_or_call_controls(self):
        self.actor = replace(self.actor, permissions=frozenset({management.VIEW}), epoch=54321)
        self.run_page()
        self.assertEqual(len(self.page.text_input), 0)
        self.assertEqual(len(self.page.button), 0)
        self.assertEqual(len(self.page.dataframe), 1)


if __name__ == "__main__":
    unittest.main()
