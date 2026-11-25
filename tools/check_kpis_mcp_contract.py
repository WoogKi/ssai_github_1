"""DB·외부 통신 없는 KPIS 등록부 및 기존 MCP 권한 경계 검사."""
from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from app.services import kpis_mcp_contract as contract
from app.services.mcp_adapter import MCP_EXTERNAL_RESOURCE_READ, MCP_RESOURCE_ALLOWLIST
from app.ui.mcp_chat_adapter import parse_explicit_mcp_resource_request


class KpisContractTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 23, tzinfo=timezone.utc)
        self.until = self.now + timedelta(minutes=1)
        self.request = contract.Request("MA117", "TEST", 2, "synthetic-user", 1)
        self.session = contract.Session(2, "synthetic-user", 1,
            frozenset({MCP_EXTERNAL_RESOURCE_READ}), frozenset({("MA117", "TEST")}), True, self.until)
        self.approval = contract.CompanyApproval(2, "MA117", "TEST", True, self.until, "synthetic-approval")

    def check(self, request=None, session=None, approval=None, **kwargs):
        return contract.preflight(request or self.request, load_session=lambda: session or self.session,
            approval=approval or self.approval, feature_enabled=True, explicit_request=True,
            now=self.now, **kwargs)

    def test_catalog_priority_and_save_exclusion(self):
        self.assertEqual(len(contract.SERVICES),18)
        self.assertEqual(len(contract.READ_SERVICES),16)
        self.assertEqual(contract.FIRST_PRIORITY, ("MA117","MA101","MA102","MA116","MA110"))
        self.assertEqual(contract.SERVICES["MA109"].name,"EB 반송 마스터 조회")
        self.assertEqual(contract.SERVICES["MA119"].response_kind,"paged_json")
        self.assertEqual(contract.SERVICES["MA118"].response_kind,"gzip")
        for code in contract.READ_SERVICES:
            self.assertEqual(contract.SERVICES[code].method,"POST")
            self.assertTrue(contract.SERVICES[code].path.startswith("/v1/services/"))
        for code in ("MA111","MA113","MA108"):
            self.assertNotIn(code,contract.READ_SERVICES)
            self.assertEqual(self.check(replace(self.request,service=code))["status"],"DENIED")
        with self.assertRaises(TypeError):
            contract.SERVICES["MA111"]=contract.SERVICES["MA117"]

    def test_all_read_services_require_scoped_approval(self):
        for code in contract.READ_SERVICES:
            for mode in contract.MODES:
                with self.subTest(code=code,mode=mode):
                    request=replace(self.request,service=code,mode=mode)
                    session=replace(self.session,service_modes=frozenset({(code,mode)}))
                    approval=replace(self.approval,service=code,mode=mode)
                    result=self.check(request,session,approval)
                    self.assertEqual(result["status"],"PREFLIGHT_ONLY")
                    self.assertFalse(result["runtime_ready"])
                    self.assertEqual(result["physical_call_count"],0)
                    self.assertEqual(result["retry_count"],0)

    def test_default_off_and_explicit_request_before_session_access(self):
        provider=Mock(side_effect=AssertionError("must not access session"))
        for options in ({},dict(feature_enabled=True),dict(feature_enabled=1,explicit_request=True)):
            result=contract.preflight(self.request,load_session=provider,approval=self.approval,**options)
            self.assertEqual(result["status"],"DENIED")
        provider.assert_not_called()

    def test_company_user_and_epoch_isolation(self):
        for session in (replace(self.session,company_id=3),replace(self.session,user_id="other"),
                        replace(self.session,epoch=2),replace(self.session,expires_at=self.now)):
            self.assertEqual(self.check(session=session)["reason_code"],"stale_or_mismatched_session")
        provider=Mock(side_effect=[self.session,replace(self.session,company_id=3)])
        args=dict(load_session=provider,approval=self.approval,feature_enabled=True,explicit_request=True,now=self.now)
        self.assertEqual(contract.preflight(self.request,**args)["status"],"PREFLIGHT_ONLY")
        self.assertEqual(contract.preflight(self.request,**args)["status"],"DENIED")
        self.assertEqual(provider.call_count,2)

    def test_erp_permission_is_not_mcp_or_api_permission(self):
        for permissions in (frozenset(),frozenset({"KPI_READ","MASTER_READ"}),MCP_EXTERNAL_RESOURCE_READ):
            self.assertEqual(self.check(session=replace(self.session,permissions=permissions))["reason_code"],"permission_denied")
        self.assertEqual(self.check(session=replace(self.session,service_modes=frozenset()))["reason_code"],"service_mode_denied")

    def test_test_production_and_company_approvals_never_fallback(self):
        self.assertEqual(self.check(session=replace(self.session,test_authorized=False))["reason_code"],"test_permission_required")
        for approval in (replace(self.approval,company_id=3),replace(self.approval,mode="PRODUCTION"),
                         replace(self.approval,service="MA101"),replace(self.approval,enabled=False),
                         replace(self.approval,expires_at=self.now),replace(self.approval,evidence_id="")):
            self.assertEqual(self.check(approval=approval)["reason_code"],"company_approval_required")
        self.assertEqual(self.check(replace(self.request,mode="PRODUCTION"))["reason_code"],"service_mode_denied")

    def test_invalid_types_timeout_and_clock(self):
        for request in (replace(self.request,company_id=True),replace(self.request,session_epoch=True),
                        replace(self.request,timeout_s=True),replace(self.request,timeout_s=0),
                        replace(self.request,timeout_s=31),replace(self.request,mode="test")):
            self.assertEqual(self.check(request)["status"],"DENIED")
        for expiry in (None,self.now.replace(tzinfo=None)):
            self.assertEqual(self.check(session=replace(self.session,expires_at=expiry))["status"],"DENIED")
        provider=Mock(side_effect=AssertionError("invalid clock must fail first"))
        result=contract.preflight(self.request,load_session=provider,approval=self.approval,
            feature_enabled=True,explicit_request=True,now=self.now.replace(tzinfo=None))
        self.assertEqual(result["reason_code"],"invalid_clock")
        provider.assert_not_called()

    def test_errors_and_safe_result_do_not_echo_identity_or_secrets(self):
        provider=Mock(side_effect=RuntimeError("password=synthetic-secret"))
        result=contract.preflight(self.request,load_session=provider,approval=self.approval,
            feature_enabled=True,explicit_request=True,now=self.now)
        self.assertEqual(result["reason_code"],"session_unavailable")
        for value in (result,self.check()):
            text=json.dumps(value)
            for forbidden in ("synthetic-secret","synthetic-user","synthetic-approval","password","api_key"):
                self.assertNotIn(forbidden,text)

    def test_current_chat_resource_allowlist_is_not_extended(self):
        self.assertEqual(MCP_RESOURCE_ALLOWLIST,frozenset({"official-recall-notice"}))
        self.assertIsNone(parse_explicit_mcp_resource_request("MA117 공지사항을 보여줘"))
        self.assertIsNone(parse_explicit_mcp_resource_request("출고현황을 보여줘"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
