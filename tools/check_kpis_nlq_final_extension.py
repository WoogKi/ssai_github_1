"""Synthetic-only final KPIS NLQ regression; real ERP/network are forbidden."""
from __future__ import annotations

from dataclasses import replace
from io import BytesIO, StringIO
import csv
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import httpx
import pandas as pd
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import check_kpis_nlq_phase1 as phase
from app.services import rddbc040_service as products
from app.services.kpis_nlq import parse_request
from app.ui import kpis_nlq_chat as chat


def candidates(count):
    return pd.DataFrame([dict(제품코드=f"{i:05d}", 제품명="합성제품", 규격=f"{i}mg",
                              제약사명="합성제약", 표준코드=f"{i:013d}")
                         for i in range(1, count + 1)], columns=products.STANDARD_PRODUCT_COLUMNS)


class Grammar(unittest.TestCase):
    def parse(self, text):
        return parse_request(text, today=phase.TODAY)

    def test_product_phrasing_and_default_cap(self):
        for text in ("KPIS 표준코드 이력조회 아스피린", "KPIS 아스피린 표준코드 변경이력",
                     "KPIS 표준코드 이력조회 노르믹스"):
            p = self.parse(text)
            self.assertEqual(p.status, "products")
            self.assertIn(p.product_term, ("아스피린", "노르믹스"))
            self.assertEqual(p.product_limit, 10)
        for suffix, count in (("상위 10개", 10), ("상위 5개", 5), ("상위 20개", 20), ("상위 1개", 1), ("TOP 3", 3)):
            self.assertEqual(self.parse("KPIS 아스피린 표준코드 이력 " + suffix).product_limit, count)

    def test_over_twenty_is_rejected_not_capped(self):
        for count in (21, 100, 9999):
            p = self.parse(f"KPIS 아스피린 표준코드 이력 상위 {count}개")
            self.assertEqual(p.status, "clarify")
            self.assertEqual(p.reason, "product_limit_exceeded")
            self.assertIn("최대 20개", p.message)

    def test_direct_and_product_code_are_distinct(self):
        self.assertEqual(self.parse("KPIS 표준코드 0000000000001").query, {"msupStdCd": "0000000000001"})
        p = self.parse("KPIS 표준코드 이력 제품코드: 00001")
        self.assertEqual((p.status, p.product_term), ("products", "00001"))

    def test_no_invented_ranking_or_identifier(self):
        for tail in ("상위 0개", "매출순위", "현재만", "판매 상위 5개", "상위 2개 상위 3개"):
            self.assertEqual(self.parse("KPIS 아스피린 표준코드 이력 " + tail).status, "clarify")
        for value in ("000000000001", "00000000000001", "0000000000001 0000000000002"):
            self.assertEqual(self.parse("KPIS 표준코드 " + value).status, "clarify")

    def test_complete_korean_vendor_aliases_and_roles(self):
        for kind in ("거래처 구분", "거래처구분"):
            for business in ("사업자번호", "사업자등록번호", "거래처 사업자번호"):
                for hospital in ("요양기관기호", "요양기관코드"):
                    p = self.parse(f"KPIS 합성병원 거래처 등록 여부 {kind} 5 {business} 000-00-00001 {hospital} 00000001")
                    self.assertEqual(p.status, "ready")
                    self.assertEqual(p.query, dict(msupSplyFrmCd="5", tkrHbin="0000000001", ykiho="00000001", tkrpNm="합성병원"))

    def test_vendor_name_not_changed_by_cleanup(self):
        p = self.parse("KPIS 정보병원 거래처 등록 여부 거래처구분 6 사업자번호 0000000001")
        self.assertEqual(p.query["tkrpNm"], "정보병원")
        p = self.parse("KPIS 정보병원 거래처 등록 여부 거래처구분: 6 사업자등록번호: 0000000001 요양기관코드=00000001")
        self.assertEqual(p.status, "ready")
        self.assertEqual(p.query["tkrpNm"], "정보병원")

    def test_vendor_required_and_invalid_conditions(self):
        for text in ("합성병원 거래처 등록 여부", "합성병원 거래처 구분 5",
                     "합성병원 거래처 사업자번호 0000000001", "합성병원 거래처 구분 0 사업자번호 0000000001",
                     "합성병원 거래처 구분 5 사업자번호 000-00-001",
                     "합성병원 거래처 구분 5 사업자번호 0000000001 요양기관코드 000001",
                     "합성병원 거래처 구분 5 사업자번호 0000000001 요양기관코드 000000001"):
            self.assertEqual(self.parse("KPIS " + text).status, "clarify")

    def test_three_month_range_forms(self):
        for value in ("202609 ~ 202611", "2026-09 ~ 2026-11", "2026년 9월 ~ 11월"):
            p = self.parse("KPIS 회수의약품 조회 " + value)
            self.assertEqual(p.status, "ready")
            self.assertEqual(p.query, dict(rpayCmmStaDd="20260901", rpayCmmEndDd="20261130"))

    def test_leap_and_year_boundary(self):
        for value, start, end in (("2023-12 ~ 2024-02", "20231201", "20240229"),
                                  ("2026년 12월 ~ 2027년 1월", "20261201", "20270131"),
                                  ("202602 ~ 202602", "20260201", "20260228")):
            p = self.parse("KPIS 회수 " + value)
            self.assertEqual(p.status, "ready")
            self.assertEqual(p.query, dict(rpayCmmStaDd=start, rpayCmmEndDd=end))

    def test_invalid_month_not_defaulted(self):
        for value in ("202611 ~ 202609", "202600 ~ 202601", "202601 ~ 202613", "000001 ~ 202611",
                      "202609 ~", "~ 202611", "2026년 9월 ~", "2026년 ~ 11월", "2026-09 ~ 11",
                      "2026년 11월 ~ 9월", "2026-09-01 ~ 2026-11", "202609 ~ 202611 ~ 202612"):
            with self.subTest(value=value):
                self.assertEqual(self.parse("KPIS 회수 " + value).status, "clarify")

    def test_existing_recall_contracts_and_non_kpis(self):
        self.assertEqual(self.parse("KPIS 회수 2026년").query, {"rpayCmmYr": "2026"})
        self.assertEqual(self.parse("KPIS 회수 20261123 ~ 20261124").query,
                         dict(rpayCmmStaDd="20261123", rpayCmmEndDd="20261124"))
        self.assertEqual(self.parse("KPIS 회수 최근 한 달").status, "defaults")
        for text in ("표준코드 아스피린", "KPI 제품 상위 5개", "거래처 합성병원", "현재표 합계", "회수 202609 ~ 202611"):
            self.assertIsNone(self.parse(text))


class Selection(unittest.TestCase):
    def run_selection(self, frame, text="KPIS 아스피린 표준코드 이력"):
        guard = Mock()
        with patch.object(products, "search_standard_code_candidates", return_value=frame) as search:
            result = chat._product_queries(parse_request(text, today=phase.TODAY), (4, "synthetic", 1), guard)
        self.assertEqual(guard.call_count, 2)
        return result, search

    def test_zero_one_five_six_ten_and_twenty_products(self):
        for count in (0, 1, 5, 6, 10, 20):
            (queries, summary), search = self.run_selection(candidates(count))
            self.assertEqual(len(queries), min(count, 10))
            search.assert_called_once_with(4, keyword="아스피린")
            if count:
                self.assertEqual(queries[0], {"msupStdCd": "0000000000001"})
                self.assertIn("합성제약", summary)
                self.assertIn(f"검색 후보 {count}개 제품", summary)
                self.assertIn(f"앞 {min(count, 10)}개 제품 선택", summary)

    def test_order_code_search_and_requested_five(self):
        frame = candidates(6).iloc[::-1]
        (queries, summary), search = self.run_selection(frame, "KPIS 표준코드 이력 00001 상위 5개")
        search.assert_called_once_with(4, physic_cd="00001")
        self.assertEqual([q["msupStdCd"] for q in queries], [f"{i:013d}" for i in (6, 5, 4, 3, 2)])
        self.assertIn("상위 5개", summary)

    def test_existing_alphanumeric_code_rule(self):
        (_, _), search = self.run_selection(candidates(1), "KPIS 표준코드 이력 A1234")
        search.assert_called_once_with(4, physic_cd="A1234")

    def test_twenty_of_two_hundred_partial_candidates(self):
        frame = candidates(200)
        frame.attrs["kpis_candidates_partial"] = True
        (queries, summary), _ = self.run_selection(frame, "KPIS 아스피린 표준코드 이력 상위 20개")
        self.assertEqual(len(queries), 20)
        self.assertIn("검색 후보 200개 제품", summary)
        self.assertIn("앞 20개 제품 선택", summary)
        self.assertIn("전체 검색 건수는 미확정", summary)

    def test_duplicate_standard_and_history_not_extra_calls(self):
        frame = candidates(3)
        frame.loc[1, "표준코드"] = frame.loc[0, "표준코드"]
        frame = pd.concat([frame, frame.iloc[:1]], ignore_index=True)
        (queries, summary), _ = self.run_selection(frame)
        self.assertEqual(len(queries), 2)
        self.assertIn("중복 코드 1회 조회", summary)

    def test_missing_invalid_multiple_excluded_no_replacement(self):
        frame = candidates(6)
        frame.loc[0, "표준코드"] = ""
        frame.loc[1, "표준코드"] = "123"
        other = frame.iloc[[2]].copy()
        other["표준코드"] = "0000000000009"
        frame = pd.concat([frame, other], ignore_index=True)
        (queries, summary), _ = self.run_selection(frame, "KPIS 아스피린 표준코드 이력 상위 5개")
        self.assertEqual(queries, [{"msupStdCd": "0000000000004"}, {"msupStdCd": "0000000000005"}])
        for reason in ("미등록", "형식 오류", "복수 등록"):
            self.assertIn(reason, summary)

    def test_partial_mapping_boundary_is_not_guessed(self):
        frame = candidates(2)
        frame.attrs["kpis_candidates_partial"] = True
        (queries, summary), _ = self.run_selection(frame)
        self.assertEqual(len(queries), 1)
        self.assertIn("200개", summary)
        self.assertIn("상한 경계", summary)

    def test_null_standard_is_missing_not_a_generated_code(self):
        for value in (None, float("nan"), pd.NA):
            frame = candidates(1)
            frame.loc[0, "표준코드"] = value
            (queries, summary), _ = self.run_selection(frame)
            self.assertEqual(queries, [])
            self.assertIn("미등록", summary)


class Adapter(unittest.TestCase):
    setUp = phase.ChatAdapter.setUp
    make_message = phase.ChatAdapter.make_message
    run_question = phase.ChatAdapter.run_question

    def test_over_limit_no_search_no_api(self):
        with patch.object(products, "search_standard_code_candidates", side_effect=AssertionError("no search")) as search:
            self.run_question("KPIS 아스피린 표준코드 이력 상위 21개")
        search.assert_not_called()
        self.call.assert_not_called()
        self.publish.assert_not_called()
        self.assertIn("최대 20개", self.room["messages"][-1]["content"])

    def test_zero_products_no_api_and_rerun_no_search(self):
        with patch.object(products, "search_standard_code_candidates", return_value=candidates(0)) as search:
            self.run_question("KPIS 아스피린 표준코드 이력")
            self.run_question("KPIS 아스피린 표준코드 이력")
        search.assert_called_once()
        self.call.assert_not_called()
        self.publish.assert_not_called()
        self.assertEqual(len(self.room["messages"]), 2)

    def test_all_invalid_no_api(self):
        frame = candidates(1)
        frame["표준코드"] = ""
        with patch.object(products, "search_standard_code_candidates", return_value=frame):
            self.run_question("KPIS 아스피린 표준코드 이력")
        self.call.assert_not_called()
        self.assertIn("미등록", self.room["messages"][-1]["content"])

    def test_room_switch_during_search_prevents_api(self):
        def search(*args, **kwargs):
            self.state["current_room"] = "other"
            return candidates(5)
        with patch.object(products, "search_standard_code_candidates", side_effect=search):
            self.run_question("KPIS 아스피린 표준코드 이력")
        self.call.assert_not_called()
        self.publish.assert_not_called()

    def test_ma102_complete_korean_calls_once_and_no_raw_log(self):
        text = "KPIS 합성병원 거래처 등록 여부 거래처 구분 5 사업자등록번호 000-00-00001 요양기관코드 00000001"
        with self.assertLogs("ssai.kpis", level="INFO") as logs:
            self.run_question(text)
            self.run_question(text)
        self.call.assert_called_once()
        self.assertEqual(self.call.call_args.args[1], [dict(msupSplyFrmCd="5", tkrHbin="0000000001", ykiho="00000001", tkrpNm="합성병원")])
        self.publish.assert_called_once()
        self.assertNotIn("0000000001", str(logs.output))
        self.assertNotIn("합성병원", str(logs.output))

    def test_incomplete_vendor_and_month_zero_calls(self):
        for index, text in enumerate(("KPIS 합성병원 거래처 등록 여부", "KPIS 거래처 구분 5",
                                     "KPIS 거래처 구분 5 사업자번호 000-00-001", "KPIS 회수 202609 ~")):
            self.run_question(text, str(index))
        self.call.assert_not_called()
        self.publish.assert_not_called()

    def test_month_exact_rest_dates(self):
        self.run_question("KPIS 회수의약품 조회 202609 ~ 202611")
        self.assertEqual(self.call.call_args.args[1], [dict(rpayCmmStaDd="20260901", rpayCmmEndDd="20261130")])
        self.call.assert_called_once()


class BatchExecution(unittest.TestCase):
    def setUp(self):
        from tools.check_kpis_erp_direct import DirectChecks
        from app.services.kpis_rest_client import KpisClient
        from app.services.kpis_erp_direct import ErpDirectAccess
        from app.ui import chat_middleware

        self.fixture = DirectChecks(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.room = dict(id="synthetic-batch", company_id=8, user_id="42", messages=[], history=[])
        f.state.update(current_room=self.room["id"], chat_rooms=[self.room])
        self.search = Mock(return_value=candidates(21))
        mapping = candidates(21)
        mapping["연결원천건수"] = 1
        self.mapping = Mock(return_value=mapping)
        self.fail_at = None
        self.empty_at = None
        self.after_http = lambda: None
        self.saves = Mock()
        self.client = KpisClient(ErpDirectAccess(audit=f.events.append), lambda: f.actor,
                                 enabled=True, erp_key_loader=f.key, transport=httpx.MockTransport(self.handler))
        patches = (("socket.socket.connect", dict(side_effect=AssertionError("network forbidden"))),
                   ("socket.create_connection", dict(side_effect=AssertionError("network forbidden"))),
                   ("pyodbc.connect", dict(side_effect=AssertionError("ERP forbidden"))))
        for target, options in patches:
            p = patch(target, **options); p.start(); self.addCleanup(p.stop)
        for obj, key, value in ((chat.ui, "configured_direct_client", Mock(return_value=self.client)),
                                (chat.ui, "load_current_api_session", Mock(side_effect=lambda: f.actor)),
                                (chat.ui, "kst_today", Mock(return_value=phase.TODAY)),
                                (products, "search_standard_code_candidates", self.search),
                                (products, "lookup_standard_code_products", self.mapping),
                                (chat_middleware, "maybe_create_structured_presentation", Mock(return_value={"status": "skipped"})),
                                (chat_middleware, "_append_finalized_nlq_case", Mock())):
            p = patch.object(obj, key, value); p.start(); self.addCleanup(p.stop)

    def handler(self, request):
        from app.services.kpis_rest_client import OUTPUTS
        f = self.fixture
        f.sent.append(request)
        code = json.loads(request.content)["msupStdCd"]
        self.after_http()
        if len(f.sent) == self.fail_at:
            return httpx.Response(200, json={"result": "fail"})
        row = {name: "" for name in OUTPUTS["MA101"][1]}
        row.update(stdCd=code, upCost="1234")
        rows = [] if len(f.sent) == self.empty_at else [dict(row) for _ in range(3)]
        return httpx.Response(200, json={"result": "success", OUTPUTS["MA101"][0]: rows})

    def run_question(self, event="synthetic-event", suffix=" 상위 20개"):
        return chat.try_handle_kpis_chat("KPIS 표준코드 이력조회 합성제품" + suffix, self.room, event_id=event,
            make_message=lambda role, content: dict(id=f"message-{len(self.room['messages'])}", role=role, content=content),
            materialize=lambda room: room.update(auto_created=False), save=self.saves, is_current=lambda: True)

    def test_twenty_calls_all_history_mapping_one_publish_and_exports(self):
        from app.ui import chat_middleware as middle
        self.run_question()
        self.run_question()
        f = self.fixture
        self.assertEqual(len(f.sent), 20)
        self.search.assert_called_once_with(8, keyword="합성제품")
        self.mapping.assert_called_once()
        self.assertEqual(len(self.mapping.call_args.args[1]), 20)
        self.assertEqual(f.key.call_count, 20)
        self.assertEqual(len([e for e in f.events if e["action"] == "CALL_START"]), 20)
        tables = [m for m in self.room["messages"] if m.get("meta", {}).get("source") == "KPIS"]
        self.assertEqual(len(tables), 1)
        self.assertEqual(self.saves.call_count, 2)
        self.assertFalse(self.room["auto_created"])
        frame = f.state["sims_export_tables"][f.state["__sims_current_table_source_key"]]
        self.assertEqual(len(frame), 60)
        self.assertEqual(list(frame)[:18], list(chat.ui.STANDARD_COLUMNS.values()))
        self.assertTrue(frame["ERP 제품명"].eq("합성제품").all())
        self.assertTrue(frame["ERP 제약사명"].eq("합성제약").all())
        self.assertTrue(frame["ERP 매핑 건수"].eq(1).all())
        csv_file, excel = middle._make_table_downloads(frame)
        parsed = list(csv.DictReader(StringIO(csv_file.getvalue().decode("utf-8-sig"))))
        self.assertEqual(len(parsed), 60)
        self.assertEqual(list(parsed[0])[:18], list(chat.ui.STANDARD_COLUMNS.values()))
        book = load_workbook(BytesIO(excel.getvalue()), read_only=True)
        self.assertEqual(book.active.max_row, 61)
        book.close()
        self.assertIn("성공 3건", str(tables[0]))
        self.assertNotIn("synthetic-erp-key-only", str(self.room))

    def test_default_ten_physical_calls(self):
        self.run_question(suffix="")
        self.assertEqual(len(self.fixture.sent), 10)
        self.assertEqual(self.saves.call_count, 2)

    def test_requested_five_physical_calls(self):
        self.run_question(suffix=" 상위 5개")
        self.assertEqual(len(self.fixture.sent), 5)

    def test_requested_twenty_one_no_erp_key_or_http(self):
        self.run_question(suffix=" 상위 21개")
        self.search.assert_not_called()
        self.fixture.key.assert_not_called()
        self.assertEqual(self.fixture.sent, [])

    def test_valid_zero_rows_is_distinct_from_failure(self):
        self.search.return_value = candidates(2)
        self.empty_at = 1
        self.run_question()
        self.assertEqual(len(self.fixture.sent), 2)
        frame = self.fixture.state["sims_export_tables"][self.fixture.state["__sims_current_table_source_key"]]
        self.assertEqual(len(frame), 3)
        self.assertIn("성공 0건", str(self.room["messages"][-1]))
        self.assertIn("성공 3건", str(self.room["messages"][-1]))

    def test_duplicate_codes_called_once_and_no_row_expansion(self):
        frame = candidates(2)
        frame.loc[1, "표준코드"] = frame.loc[0, "표준코드"]
        self.search.return_value = frame
        self.mapping.return_value = frame.assign(연결원천건수=1)
        self.run_question()
        self.assertEqual(len(self.fixture.sent), 1)
        frame = self.fixture.state["sims_export_tables"][self.fixture.state["__sims_current_table_source_key"]]
        self.assertEqual(len(frame), 3)
        self.assertTrue(frame["ERP 매핑 건수"].eq(2).all())

    def test_partial_failure_stops_no_table_and_reports_all_codes(self):
        self.fail_at = 3
        self.run_question()
        self.run_question()
        self.assertEqual(len(self.fixture.sent), 3)
        self.mapping.assert_not_called()
        self.assertNotIn("__sims_current_table_source_key", self.fixture.state)
        self.assertEqual(len(self.room["messages"]), 2)
        message = self.room["messages"][-1]["content"]
        self.assertIn("전체 조회 미완료", message)
        self.assertIn("부분 결과표는 게시하지 않았습니다", message)
        self.assertEqual(message.count("성공 3건"), 2)
        self.assertIn("실패:", message)
        self.assertEqual(message.count("미실행 또는 완료 미확인"), 17)

    def test_first_failure_is_not_zero_rows_success(self):
        self.fail_at = 1
        self.run_question()
        self.assertEqual(len(self.fixture.sent), 1)
        self.assertIn("실패:", self.room["messages"][-1]["content"])
        self.assertNotIn("성공 0건", self.room["messages"][-1]["content"])
        self.assertNotIn("__sims_current_table_source_key", self.fixture.state)

    def test_room_switch_between_calls_stops_immediately(self):
        self.after_http = lambda: self.fixture.state.update(current_room="other")
        self.run_question()
        self.assertEqual(len(self.fixture.sent), 1)
        self.mapping.assert_not_called()
        self.assertEqual(len(self.room["messages"]), 1)

    def test_epoch_change_between_calls_stops(self):
        def change():
            self.fixture.actor = replace(self.fixture.actor, epoch=54321)
        self.after_http = change
        self.run_question()
        self.assertEqual(len(self.fixture.sent), 1)
        self.mapping.assert_not_called()
        self.assertEqual(len(self.room["messages"]), 1)

    def test_logout_during_search_blocks_key_and_http(self):
        def search(*args, **kwargs):
            self.fixture.actor = None
            return candidates(5)
        self.search.side_effect = search
        self.run_question()
        self.fixture.key.assert_not_called()
        self.assertEqual(self.fixture.sent, [])

    def test_permission_revoke_between_calls_stops(self):
        self.after_http = lambda: self.fixture.company.update(is_active=False)
        self.run_question()
        self.assertEqual(len(self.fixture.sent), 1)
        self.mapping.assert_not_called()
        self.assertEqual(len(self.room["messages"]), 1)


if __name__ == "__main__":
    unittest.main()
