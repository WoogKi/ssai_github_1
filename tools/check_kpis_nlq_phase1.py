"""DB-free KPIS entry, grammar and chat adapter contracts. No live clients."""
from __future__ import annotations

import ast
from datetime import date
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from contextlib import ExitStack
from io import BytesIO, StringIO
import csv
import json
import os
import re

import httpx
import pandas as pd
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services.kpis_nlq import is_explicit_kpis, parse_request
from app.services.external_api_management import ApiManagementError
from app.ui import kpis_nlq_chat as chat

TODAY = date(2026, 11, 25)


class Grammar(unittest.TestCase):
    def parse(self, text):
        return parse_request(text, today=TODAY)

    def test_marker_and_ordinary_questions(self):
        for text in ("최근 공지사항 조회", "아스피린 제품정보 조회", "거래처 대자인병원 조회",
                     "어제 출고현황", "이번 달 매출현황", "KPI 공지사항", "매출 KPI 분석",
                     "XKPIS 공지", "KPISS 공지", "KPIS123 공지", "K P I S 공지",
                     "현재표 공급금액 합계", "방금 결과 분석", "현재표 상위 10개"):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(text))
        for text in ("KPIS 공지", "kpis 공지", "(KPIS) 공지", "KPIS최근 공지사항 조회"):
            self.assertTrue(is_explicit_kpis(text))
            self.assertEqual(self.parse(text).service, "MA117")

    def test_unsupported_ambiguous_and_followup(self):
        for text in ("KPIS 매출", "KPIS MA118", "KPIS MA117 MA109", "KPIS 공지와 회수 조회",
                     "KPIS 현재표 요약", "KPIS", "KPIS 공급내역 삭제", "KPIS 공지 최신 10개"):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text).status, "clarify")

    def test_notice_defaults_and_explicit(self):
        for text in ("KPIS 최근 공지사항 조회", "KPIS 최근 1주일 공지", "KPIS 최근 7일 공지"):
            self.assertEqual(self.parse(text).status, "defaults")
        p = self.parse("KPIS 공지사항 2026-11-01 ~ 2026-11-24 조회")
        self.assertEqual(p.query, dict(srchStaDd="20261101", srchEndDd="20261124"))
        self.assertEqual(p.status, "ready")
        p = self.parse("KPIS 어제 공지사항 조회")
        self.assertEqual(p.query, dict(srchStaDd="20261124", srchEndDd="20261124"))

    def test_bad_dates_not_defaulted(self):
        for text in ("KPIS 공지 2026-02-30 ~ 2026-03-01", "KPIS 공지 2026-12-01 ~ 2026-11-01",
                     "KPIS 공지 2026-11-01", "KPIS 공지 시작일 종료일", "KPIS 공지 지난달",
                     "KPIS 공지 최근 3개월", "KPIS 공급내역 공급일", "KPIS 공급내역 2026-11-01 ~ 2026-11-02",
                     "KPIS 공급내역 어제 2026-11-24", "KPIS 공지 오늘 어제"):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text).status, "clarify")

    def test_named_date_roles_not_text_order(self):
        p = self.parse("KPIS 공지 종료일 2026-11-25 시작일 2026-11-18")
        self.assertEqual(p.status, "ready")
        self.assertEqual(p.query, {"srchStaDd": "20261118", "srchEndDd": "20261125"})
        for text in ("KPIS 공지 종료일 2026-11-18 시작일 2026-11-25",
                     "KPIS 공지 시작일 2026-11-18 시작일 2026-11-25",
                     "KPIS 공급내역 종료일 2026-11-25", "KPIS 공지 종료일 2026-11-18 ~ 2026-11-25"):
            self.assertEqual(self.parse(text).status, "clarify")

    def test_main_wrapper_ordinary_input_has_no_context_or_ui_work(self):
        from tools.check_chat_room_login_autoselect import install, MAIN
        current = Mock(side_effect=AssertionError("ordinary path must not load KPIS context"))
        ns = {"get_current_user": current, "_selected_chat_company_id": current}
        install(MAIN, {"_try_handle_kpis_chat"}, ns)
        self.assertFalse(ns["_try_handle_kpis_chat"]("이번 달 매출현황", {}, "e"))
        current.assert_not_called()

    def test_consumed_legacy_submit_once(self):
        from tools.check_chat_room_login_autoselect import install, MAIN
        from app.services.chat_composer_submission import queue_text_dispatch_after_echo, consume_text_dispatch_event_after_echo
        from app.services.chat_company_scope_policy import room_matches_company
        import uuid
        state = {}
        context = {"company_id": "4", "room_id": "r", "user_id": "1"}
        queue_text_dispatch_after_echo(state, text="KPIS 공지사항 조회", context=context)
        text, event_id, status = consume_text_dispatch_event_after_echo(state, context=context)
        self.assertEqual(status, "ready")
        self.assertEqual(event_id, "")
        ns = dict(uuid=uuid, get_current_user=lambda: SimpleNamespace(user_id=1),
                  _selected_chat_company_id=lambda: "4", _room_matches_company=room_matches_company,
                  _sync_room_meta=Mock(), save_chat_rooms=Mock())
        install(MAIN, {"_try_handle_kpis_chat"}, ns)
        with patch.object(chat, "try_handle_kpis_chat", return_value=True) as adapter:
            self.assertTrue(ns["_try_handle_kpis_chat"](text, {"company_id": "4"}, event_id))
            self.assertTrue(adapter.call_args.kwargs["event_id"])
            self.assertTrue(adapter.call_args.kwargs["is_current"]())
        self.assertEqual(consume_text_dispatch_event_after_echo(state, context=context)[2], "missing")

    def test_recall(self):
        for text in ("KPIS 회수의약품 조회", "KPIS 최근 한 달 회수의약품", "KPIS 최근 1개월 회수 조회"):
            self.assertEqual(self.parse(text).status, "defaults")
        p = self.parse("KPIS 2026년 회수의약품 조회")
        self.assertEqual(p.query, {"rpayCmmYr": "2026"})
        self.assertEqual(p.status, "ready")
        p = self.parse("KPIS 회수의약품 20261123 ~ 20261124")
        self.assertEqual(p.query, {"rpayCmmStaDd": "20261123", "rpayCmmEndDd": "20261124"})
        for text in ("KPIS 회수 2026년 20261123 ~ 20261124", "KPIS 회수 0000년"):
            self.assertEqual(self.parse(text).status, "clarify")

    def test_supply(self):
        p = self.parse("KPIS 어제 공급내역 조회")
        self.assertEqual(p.query, {"splyDd": "20261124", "splyTpCd": None})
        self.assertEqual(p.status, "ready")
        self.assertEqual(self.parse("KPIS 직전 영업일 공급내역").status, "defaults")
        for word, code in (("출고", "1"), ("반품", "2"), ("폐기", "3"), ("정정", "4"), ("출고+정정", None)):
            p = self.parse(f"KPIS 공급내역 2026년 11월 24일 {word}")
            self.assertEqual(p.status, "ready")
            self.assertEqual(p.query["splyTpCd"], code)
        for text in ("KPIS 공급내역 어제 취소", "KPIS 공급내역 어제 구분 5", "KPIS 공급내역 어제 출고 반품"):
            self.assertEqual(self.parse(text).status, "clarify")

    def test_standard_exact_no_first_candidate(self):
        p = self.parse("KPIS 표준코드 이력 0000000000001")
        self.assertEqual(p.query, {"msupStdCd": "0000000000001"})
        self.assertEqual(p.status, "ready")
        for text in ("KPIS 표준코드 이력 123456789012",
                     "KPIS 표준코드 0000000000001 0000000000002", "KPIS 표준코드 0000000000001 현재만"):
            self.assertEqual(self.parse(text).status, "clarify")

    def test_vendor_required_fields_and_optional_contract(self):
        for code in ("5", "6", "8", "9"):
            p = self.parse(f'KPIS MA102 구분 {code} 사업자번호 000-00-00001 거래처명 "합성거래처" 요양기관기호 00000001')
            self.assertEqual(p.status, "ready")
            self.assertEqual(p.query, dict(msupSplyFrmCd=code, tkrHbin="0000000001", tkrpNm="합성거래처", ykiho="00000001"))
        for text in ("KPIS 대자인병원 거래처 등록 여부", "KPIS 거래처 사업자번호 0000000001",
                     "KPIS MA102 구분 6", "KPIS MA102 구분 0 사업자번호 0000000001",
                     "KPIS MA102 구분 6 사업자번호 000-00-001", "KPIS MA102 구분 6 사업자번호 0000000001 요양기관기호 123",
                     "KPIS 거래처 요양기관 사업자번호 0000000001"):
            self.assertEqual(self.parse(text).status, "clarify")
        p = self.parse("KPIS MA110 어제 거래처 사업자번호 000-00-00001")
        self.assertEqual(p.status, "ready")
        self.assertEqual(p.query["tkrHbin"], "0000000001")

    def test_ma116_always_pending(self):
        self.assertEqual(self.parse("KPIS MA116").reason, "jwt_pending")

    def test_vendor_help_labels_match_existing_ui_options(self):
        from app.services.kpis_nlq import CANDIDATES
        message = CANDIDATES["MA102"]
        options = chat.ui.CODE_OPTIONS["msupSplyFrmCd"]
        self.assertEqual(set(options), {"5", "6", "8", "9"})
        self.assertEqual(re.findall(r"([5689])\(([^)]+)\)", message),
                         [(code, label.removesuffix(' (' + code + ')')) for code, label in options.items()])
        self.assertIn("사업자번호 10자리", message)
        self.assertIn("ERP 거래처 후보를 직접 선택", message)
        self.assertIn("이름으로 구분이나 번호를 추정하지 않으며", message)

    def test_vendor_korean_alias_combinations_same_query(self):
        expected = dict(msupSplyFrmCd="5", tkrHbin="0000000001", tkrpNm="합성병원", ykiho="00000001")
        for action in ("등록 여부", "조회"):
            for kind in ("거래처구분", "거래처 구분"):
                for business in ("사업자등록번호", "사업자 등록번호", "사업자번호", "거래처 사업자 등록번호"):
                    for institution in ("요양기관코드", "요양기관번호", "요양기관기호"):
                        with self.subTest(action=action, kind=kind, business=business, institution=institution):
                            p = self.parse(f"KPIS 합성병원 거래처 {action} {kind} 5 {business} 000-00-00001 {institution} 00000001")
                            self.assertEqual((p.service, p.status, p.reason), ("MA102", "ready", "ready"))
                            self.assertEqual(p.query, expected)

    def test_new_vendor_alias_does_not_reroute_erp_or_extend_ma110(self):
        self.assertIsNone(self.parse("합성병원 거래처 조회 거래처구분 5 사업자 등록번호 0000000001 요양기관번호 00000001"))
        for text in ("KPIS 공급내역 어제 사업자 등록번호 0000000001",
                     "KPIS 공급내역 어제 요양기관번호 00000001"):
            self.assertEqual(self.parse(text).status, "clarify")

    def test_main_hook_before_all_erp_routes_and_ordinary_tail_unchanged(self):
        path = "app/Lmstudio_SSAI_chat_main.py"
        # Windows patch.dict restoration can drop empty values from the inherited OS environment.
        before = subprocess.check_output(["git", "show", "HEAD:" + path], cwd=ROOT, env=dict(os.environ)).decode("utf-8-sig")
        after = (ROOT / path).read_text(encoding="utf-8-sig")
        def input_node(source):
            tree = ast.parse(source)
            return next(n for n in tree.body if isinstance(n, ast.If) and ast.unparse(n.test) == "user_input and user_input.strip()")
        old, new = input_node(before), input_node(after)
        self.assertIn("_try_handle_kpis_chat", ast.unparse(new.body[1]))
        self.assertEqual(ast.unparse(new.body[1].body[0]), "st.rerun()")
        self.assertEqual([ast.dump(n) for n in old.body], [ast.dump(n) for n in [new.body[0], *new.body[2:]]])


class ChatAdapter(unittest.TestCase):
    def setUp(self):
        self.state = {"current_room": "room-a"}
        self.room = {"id": "room-a", "company_id": 4, "user_id": "tester", "messages": []}
        self.actor = SimpleNamespace(company_id=4, user_id="tester", epoch="epoch-a")
        self.counter = 0
        self.save = Mock()
        self.materialize = Mock()
        self.current = Mock(return_value=True)
        self.call = Mock(return_value={"status": "SUCCESS", "reason_code": "success", "rows": [], "service": "MA117"})
        self.publish = Mock(return_value=True)
        for target, value in (("st", SimpleNamespace(session_state=self.state)),):
            patcher = patch.object(chat, target, value)
            patcher.start(); self.addCleanup(patcher.stop)
        for target, value in (("kst_today", Mock(return_value=TODAY)), ("load_current_api_session", Mock(return_value=self.actor)),
                              ("_fresh_actor", Mock(return_value=self.actor)), ("_call_selected", self.call),
                              ("publish_result", self.publish)):
            patcher = patch.object(chat.ui, target, value)
            patcher.start(); self.addCleanup(patcher.stop)
        for target in ("socket.socket.connect", "socket.create_connection", "pyodbc.connect", "sqlite3.connect"):
            guard = patch(target, side_effect=AssertionError("I/O forbidden"))
            guard.start(); self.addCleanup(guard.stop)

    def make_message(self, role, content):
        self.counter += 1
        return dict(id=str(self.counter), role=role, content=content)

    def run_question(self, text, event="event-a"):
        return chat.try_handle_kpis_chat(text, self.room, event_id=event, make_message=self.make_message,
                                        materialize=self.materialize, save=self.save, is_current=self.current)

    def test_ordinary_untouched(self):
        self.assertFalse(self.run_question("이번 달 매출현황"))
        self.call.assert_not_called(); self.save.assert_not_called()
        chat.ui.load_current_api_session.assert_not_called()
        self.assertEqual(self.state, {"current_room": "room-a"})

    def test_notice_defaults_once_and_no_rerun(self):
        self.assertTrue(self.run_question("KPIS 최근 공지사항 조회"))
        self.assertEqual(self.call.call_args.args, ("MA117", [{"srchStaDd": "20261118", "srchEndDd": "20261125"}], (4, "tester", "epoch-a")))
        self.assertTrue(self.run_question("KPIS 최근 공지사항 조회"))
        self.call.assert_called_once(); self.publish.assert_called_once()
        self.assertEqual(len(self.room["messages"]), 1)
        self.assertFalse(self.state["__queue_ai"])

    def test_explicit_date_never_calls_calendar(self):
        with patch.object(chat.ui, "_initial_dates", side_effect=AssertionError("no defaults")):
            self.run_question("KPIS 어제 공급내역 조회")
        self.assertEqual(self.call.call_args.args[1], [{"splyDd": "20261124", "splyTpCd": None}])

    def test_calendar_unavailable_no_api(self):
        with patch.object(chat.ui, "_initial_dates", return_value={"splyDd": None, "_calendar_unavailable": True}):
            self.run_question("KPIS 공급내역 조회")
        self.call.assert_not_called(); self.publish.assert_not_called()
        self.assertIn("영업일", self.room["messages"][-1]["content"])

    def test_calendar_ready_and_recall_defaults(self):
        with patch.object(chat.ui, "_initial_dates", return_value={"splyDd": date(2026, 11, 23)}):
            self.run_question("KPIS 공급내역 조회")
        self.assertEqual(self.call.call_args.args[1][0]["splyDd"], "20261123")
        self.run_question("KPIS 회수의약품 조회", "event-b")
        self.assertEqual(self.call.call_args.args[1], [{"rpayCmmStaDd": "20261025", "rpayCmmEndDd": "20261125"}])

    def test_clarification_never_calls_api_or_erp(self):
        for index, text in enumerate(("KPIS 매출 조회", "KPIS MA116", "KPIS 표준코드 이력 123456789012",
                                     "KPIS 대자인병원 거래처", "KPIS 공급내역 공급일", "KPIS MA102 구분 5")):
            self.run_question(text, f"e{index}")
        self.call.assert_not_called(); self.publish.assert_not_called()
        chat.ui.load_current_api_session.assert_not_called()

    def test_vendor_help_for_each_incomplete_condition_no_call(self):
        from app.services.kpis_nlq import CANDIDATES
        cases = (
            ("KPIS MA102 사업자번호 0000000001", "missing_query"),
            ("KPIS MA102 거래처 구분 5", "ma102_counterparty_required"),
            ("KPIS 합성병원 거래처 등록 여부", "ma102_counterparty_required"),
            ("KPIS MA102 거래처구분 7 사업자등록번호 0000000001", "invalid_supply_form"),
        )
        for index, (question, reason) in enumerate(cases):
            with self.subTest(case=index):
                plan = parse_request(question, today=TODAY)
                self.assertEqual(plan.reason, reason)
                self.assertEqual(plan.status, "clarify")
                self.assertEqual(plan.message, CANDIDATES["MA102"])
                self.run_question(question, f"help-{index}")
                self.assertEqual(self.room["messages"][-1]["content"], CANDIDATES["MA102"])
                self.assertEqual(len(self.room["messages"]), (index + 1) * 2)
                self.run_question(question, f"help-{index}")
                self.assertEqual(len(self.room["messages"]), (index + 1) * 2)
        self.call.assert_not_called()
        self.publish.assert_not_called()
        chat.ui.load_current_api_session.assert_not_called()

    def test_direct_standard_vendor_contract(self):
        self.run_question("KPIS 표준코드 이력 0000000000001")
        self.assertEqual(self.call.call_args.args[:2], ("MA101", [{"msupStdCd": "0000000000001"}]))
        self.run_question("KPIS MA102 구분 6 사업자번호 000-00-00001", "event-b")
        self.assertEqual(self.call.call_args.args[1], [{"msupSplyFrmCd": "6", "tkrHbin": "0000000001"}])

    def test_vendor_alias_complete_conditions_execute_once_each(self):
        questions = (
            "KPIS 합성병원 거래처 등록 여부 거래처구분 5 사업자등록번호 000-00-00001 요양기관코드 00000001",
            "KPIS 합성병원 거래처 조회 거래처구분 5 사업자 등록번호 000-00-00001 요양기관번호 00000001",
        )
        for index, text in enumerate(questions):
            self.run_question(text, f"alias-{index}")
            self.run_question(text, f"alias-{index}")
        self.assertEqual(self.call.call_count, 2)
        self.assertEqual(self.publish.call_count, 2)
        self.assertEqual(self.call.call_args_list[0], self.call.call_args_list[1])
        self.assertEqual(self.call.call_args.args[:2], ("MA102", [dict(msupSplyFrmCd="5", tkrHbin="0000000001", tkrpNm="합성병원", ykiho="00000001")]))

    def test_vendor_new_alias_missing_invalid_or_duplicate_no_api(self):
        for index, condition in enumerate((
            "사업자 등록번호 0000000001 요양기관번호 00000001",
            "거래처구분 5 요양기관번호 00000001",
            "거래처구분 5 사업자 등록번호 000-00-001 요양기관번호 00000001",
            "거래처구분 5 사업자 등록번호 0000000001 요양기관번호 0000001",
            "거래처구분 5 사업자 등록번호 0000000001 요양기관번호 000000001",
            "거래처구분 7 사업자 등록번호 0000000001 요양기관번호 00000001",
            "거래처구분 5 사업자 등록번호 0000000001 요양기관번호 00000001 요양기관코드 00000001",
        )):
            question = "KPIS 합성병원 거래처 조회 " + condition
            self.assertEqual(parse_request(question, today=TODAY).status, "clarify")
            self.run_question(question, f"invalid-alias-{index}")
        self.call.assert_not_called()
        self.publish.assert_not_called()
        chat.ui.load_current_api_session.assert_not_called()

    def test_logout_and_access_revoke_no_call(self):
        chat.ui.load_current_api_session.return_value = None
        self.run_question("KPIS 공지사항 조회")
        self.call.assert_not_called()
        chat.ui.load_current_api_session.return_value = self.actor
        chat.ui._fresh_actor.side_effect = ApiManagementError("authorization_changed")
        self.run_question("KPIS 공지사항 조회", "event-b")
        self.call.assert_not_called()

    def test_wrong_room_or_company_or_user(self):
        self.state["current_room"] = "other"
        self.run_question("KPIS 공지사항 조회")
        self.call.assert_not_called(); self.save.assert_not_called()
        self.state["current_room"] = "room-a"
        self.actor.company_id = 7
        self.run_question("KPIS 공지사항 조회")
        self.call.assert_not_called()
        self.actor.company_id = 4
        self.actor.user_id = "other"
        self.run_question("KPIS 공지사항 조회", "event-b")
        self.call.assert_not_called()

    def test_string_company_uses_existing_scope_policy(self):
        self.room["company_id"] = "4"
        self.run_question("KPIS 공지사항 조회")
        self.call.assert_called_once()
        self.publish.assert_called_once()

    def test_changed_room_during_call_no_publish(self):
        def change(*args):
            self.state["current_room"] = "other"
            return {"status": "SUCCESS"}
        self.call.side_effect = change
        self.run_question("KPIS 공지사항 조회")
        self.publish.assert_not_called()

    def test_current_company_guard_before_save_and_after_call(self):
        self.current.return_value = False
        self.run_question("KPIS 공지사항 조회")
        self.save.assert_not_called(); self.call.assert_not_called()
        self.current.return_value = True
        def change(*args):
            self.current.return_value = False
            return {"status": "SUCCESS"}
        self.call.side_effect = change
        self.run_question("KPIS 공지사항 조회")
        self.publish.assert_not_called()
        self.save.assert_called_once()

    def test_changed_epoch_during_call_no_publish(self):
        chat.ui._fresh_actor.side_effect = [self.actor, ApiManagementError("authorization_changed")]
        self.run_question("KPIS 공지사항 조회")
        self.call.assert_called_once(); self.publish.assert_not_called()

    def test_failure_no_fallback_no_retry_and_no_raw_error_log(self):
        self.call.side_effect = RuntimeError("PRIVATE_RAW_EXCEPTION")
        with self.assertLogs("ssai.kpis", level="INFO") as logs:
            self.run_question("KPIS 공지사항 조회")
        self.assertNotIn("PRIVATE_RAW_EXCEPTION", str(logs.output))
        self.assertNotIn("PRIVATE_RAW_EXCEPTION", str(self.room))
        self.run_question("KPIS 공지사항 조회")
        self.call.assert_called_once(); self.publish.assert_not_called()

    def test_provider_rejected_stays_error(self):
        self.call.return_value = {"status": "DENIED", "reason_code": "provider_rejected"}
        self.run_question("KPIS MA102 구분 5 사업자번호 0000000001")
        self.publish.assert_not_called()
        self.assertIn("거부", self.room["messages"][-1]["content"])


class SharedExecution(unittest.TestCase):
    def test_real_client_publisher_drain_and_full_exports_with_mock_http(self):
        from tools.check_kpis_erp_direct import DirectChecks
        from app.services.kpis_rest_client import KpisClient, OUTPUTS, SERVICES
        from app.services.kpis_erp_direct import ErpDirectAccess
        from app.ui import chat_middleware as middle

        fixture = DirectChecks(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        room = dict(id="synthetic-room", company_id=8, user_id="42", messages=[], history=[])
        fixture.state.update(current_room=room["id"], chat_rooms=[room])
        expected_rows = {"MA117": 2, "MA101": 3, "MA102": 1, "MA109": 2587, "MA110": 429}

        def handler(request):
            fixture.sent.append(request)
            service = next(code for code in expected_rows if SERVICES[code].path == request.url.path)
            row = {name: "" for name in OUTPUTS[service][1]}
            if service == "MA117":
                row.update(brdTtl="합성", brdDtlTxt="<p>본문</p>", fileYn="N")
            elif service == "MA101":
                row.update(stdCd="0000000000001", upCost="1000")
            elif service == "MA109":
                row.update(msupStdCd="0000000000001")
            elif service == "MA110":
                row.update(msupSplyAmt="1000", msupSplyQty="2", rcvCnt="1")
            return httpx.Response(200, json={"result": "success", OUTPUTS[service][0]: [row] * expected_rows[service]})

        client = KpisClient(ErpDirectAccess(audit=fixture.events.append), lambda: fixture.actor, enabled=True,
                            erp_key_loader=fixture.key, transport=httpx.MockTransport(handler))
        queries = (("MA117", "KPIS 최근 공지사항 조회"), ("MA101", "KPIS 표준코드 0000000000001"),
                   ("MA102", "KPIS MA102 구분 6 사업자번호 0000000001"),
                   ("MA109", "KPIS 회수의약품 조회"), ("MA110", "KPIS 어제 공급내역 조회"))
        make = lambda role, content: dict(id=f"synthetic-{len(room['messages'])}", role=role, content=content)
        saves = []
        with ExitStack() as stack:
            for target, kwargs in (("socket.socket.connect", {"side_effect": AssertionError("network forbidden")}),
                                   ("pyodbc.connect", {"side_effect": AssertionError("ERP forbidden")}),
                                   ("app.services.rddbc040_service.lookup_standard_code_products", {"return_value": pd.DataFrame()})):
                stack.enter_context(patch(target, **kwargs))
            stack.enter_context(patch.object(chat.ui, "configured_direct_client", return_value=client))
            stack.enter_context(patch.object(chat.ui, "load_current_api_session", side_effect=lambda: fixture.actor))
            stack.enter_context(patch.object(chat.ui, "kst_today", return_value=TODAY))
            stack.enter_context(patch.object(middle, "maybe_create_structured_presentation", return_value={"status": "skipped"}))
            stack.enter_context(patch.object(middle, "_append_finalized_nlq_case"))
            for service, text in queries:
                with self.subTest(service=service):
                    before = len(fixture.sent)
                    args = dict(event_id=service, make_message=make, materialize=lambda r: r.update(auto_created=False),
                                save=lambda: saves.append(len(room["messages"])), is_current=lambda: True)
                    self.assertTrue(chat.try_handle_kpis_chat(text, room, **args))
                    self.assertEqual(len(fixture.sent), before + 1)
                    self.assertTrue(chat.try_handle_kpis_chat(text, room, **args))
                    self.assertEqual(len(fixture.sent), before + 1)
                    source = fixture.state["__sims_current_table_source_key"]
                    frame = fixture.state["sims_export_tables"][source]
                    self.assertEqual(len(frame), expected_rows[service])
                    if service == "MA101":
                        self.assertEqual(list(frame)[:18], list(chat.ui.STANDARD_COLUMNS.values()))
                    if service == "MA110":
                        self.assertIsNone(json.loads(fixture.sent[-1].content)["splyTpCd"])
                    if service in {"MA109", "MA110"}:
                        csv_file, excel_file = middle._make_table_downloads(frame)
                        self.assertEqual(len(list(csv.DictReader(StringIO(csv_file.getvalue().decode("utf-8-sig"))))), expected_rows[service])
                        book = load_workbook(BytesIO(excel_file.getvalue()), read_only=True)
                        self.assertEqual(book.active.max_row, expected_rows[service] + 1)
                        book.close()
                    tables = [m for m in room["messages"] if m.get("meta", {}).get("source") == "KPIS"]
                    self.assertEqual(len(tables), before + 1)
            self.assertEqual(len(saves), 10)  # One user-save and one result-save per submit.
            self.assertFalse(room["auto_created"])
            self.assertEqual(len([e for e in fixture.events if e["action"] == "CALL_START"]), 5)


if __name__ == "__main__":
    unittest.main()
