"""Offline checks for NLQ condition ownership, help, and user exports."""

from __future__ import annotations

import ast
import io
import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.io_nlq import resolve_io_nlq
from app.sims.nlq import nlq_router, nlq_users
from app.sims.views.users import _build_user_list_view
from app.ui.chat_middleware import _normalize_result_for_chat, _resolve_payload_full_download_source
from app.ui.knowledge_chat_adapter import (
    cached_company_stock_location_names,
    parse_incomplete_sims_help_request,
    verified_sims_help_examples,
    verified_sims_help_text,
)


def _check_calculation_conditions() -> None:
    for question in (
        "발주계산 결제일 20일 본사창고 + 오토스토어",
        "발주계산 안전재고 3일 결제일 20일 미등록창고",
    ):
        parsed = resolve_io_nlq(question)
        assert parsed and parsed["action"] == "발주 계산"
        assert parsed["params"].get("_unconsumed_order_condition")
        intent = parse_incomplete_sims_help_request(question)
        assert intent and intent.reason == "condition_confirmation"
        with patch.object(nlq_router, "_try_handle_io_nlq", side_effect=AssertionError("ERP route")):
            assert not nlq_router.try_handle_nlq(
                question, room={"messages": []}, session_state={}, make_ts=lambda: "now",
                next_seq=lambda: 1, logger=logging.getLogger(__name__),
            )

    explicit = "발주계산 결제일 20일 재고위치 본사창고 + 오토스토어"
    parsed = resolve_io_nlq(explicit)
    assert parsed["params"]["stock_nm_list"] == ["본사창고", "오토스토어"]
    assert parsed["params"]["closing_day"] == 20
    assert not parsed["params"].get("_unconsumed_order_condition")
    assert parse_incomplete_sims_help_request(explicit) is None

    for question in ("발주계산 결제일 20일", "발주계산 안전재고 3일 결제일 20일", "발주계산 결제일 20일 전체"):
        parsed = resolve_io_nlq(question)
        assert parsed["params"]["closing_day"] == 20
        assert not parsed["params"].get("stock_cd_list")
        assert parse_incomplete_sims_help_request(question) is None


def _check_dashboard_help() -> None:
    for question in (
        "발주담당자 가상민우 일일점검",
        "sims 발주담당자 가상민우 일일점검",
        "제약담당자 가상민우 일일점검",
    ):
        intent = parse_incomplete_sims_help_request(question)
        assert intent and intent.reason == "dashboard_action_order"
        examples = verified_sims_help_examples(intent, allowed_actions={"SIMS 일일점검"})
        assert examples == (("SIMS 일일점검", f"SIMS 일일점검 {intent.subject_label} 가상민우"),)
        text = verified_sims_help_text(intent, allowed_actions={"SIMS 일일점검"})
        assert examples[0][1] in text and "발주조회" not in text and "발주계산" not in text
        assert not verified_sims_help_examples(intent, allowed_actions=set())
        with patch.object(nlq_router, "_try_handle_io_nlq", side_effect=AssertionError("ERP route")):
            assert not nlq_router.try_handle_nlq(
                question, room={"messages": []}, session_state={}, make_ts=lambda: "now",
                next_seq=lambda: 1, logger=logging.getLogger(__name__),
            )
    assert parse_incomplete_sims_help_request("SIMS 일일점검 발주담당자 가상민우") is None
    assert nlq_router.resolve_new_sims_nlq_candidate("SIMS 일일점검 발주담당자 가상민우")["action"] == "SIMS 일일점검"


def _check_dashboard_staff_condition() -> None:
    logger = logging.getLogger(__name__)
    blocked = []
    with (patch("app.ui.ssai_login.get_selected_company", side_effect=AssertionError("company lookup")),
          patch("app.services.ssai_analysis_profile_service.load_dashboard_profile", side_effect=AssertionError("profile DB")),
          patch("app.services.product_supplier_scope_service.resolve_supplier_vendor_codes", side_effect=AssertionError("supplier")),
          patch("app.sims.views.dashboard_lite.build_dashboard_lite_result_payload", side_effect=AssertionError("ERP")),
          patch("app.ui.chat_middleware.push_sims_result_to_chat", side_effect=lambda payload, action: blocked.append((payload, action)))):
        for short, full in (("발주담당", "발주담당자"), ("제약담당", "제약담당자")):
            for label in (short, full):
                question = f"SIMS 일일점검 {label}"
                assert parse_incomplete_sims_help_request(question) is None
                assert nlq_router._extract_dashboard_nlq_conditions(question) == ({full: ""}, "")
                params, notice = nlq_router._build_dashboard_nlq_params(
                    question, session_state={}, logger=logger,
                )
                assert params == {} and notice["meta"]["result_status"] == "input_required"
                assert notice["meta"]["reason_code"] == "missing_staff_name"
                assert notice["meta"]["source_call_count"] == 0 and full in notice["message"]
                assert nlq_router.try_handle_nlq(
                    question, room={"messages": []}, session_state={},
                    make_ts=lambda: "now", next_seq=lambda: 1, logger=logger,
                )
    assert len(blocked) == 4
    assert all(action == "SIMS 일일점검" and payload["meta"]["result_status"] == "input_required"
               and payload["meta"]["source_call_count"] == 0 for payload, action in blocked)
    for short, full in (("발주담당", "발주담당자"), ("제약담당", "제약담당자")):
        for question in (f"SIMS 일일점검 {short} 가상민우", f"{short} 가상민우 SIMS 일일점검"):
            assert parse_incomplete_sims_help_request(question) is None
            assert nlq_router._dashboard_unrecognized_staff_label(question) is None
            assert nlq_router._extract_dashboard_nlq_conditions(question) == ({full: "가상민우"}, "")
            assert nlq_router.resolve_new_sims_nlq_candidate(question)["action"] == "SIMS 일일점검"

    mixed = "SIMS 일일점검 발주담당 가상민우 제약사 삼진"
    assert parse_incomplete_sims_help_request(mixed) is None
    assert nlq_router._extract_dashboard_nlq_conditions(mixed) == (
        {"발주담당자": "가상민우", "제약사": "삼진"}, "",
    )
    assert nlq_router._dashboard_unrecognized_staff_label("SIMS 일일점검 담당 가상민우") is None

    calls = []
    def supplier(query, *, mode):
        calls.append((query, mode))
        return [{"code": "V1", "name": query}]

    cases = (
        ("SIMS 일일점검 발주담당자 가상민우", "dashboard_order_staff_nm", "가상민우", []),
        ("SIMS 일일점검 발주담당 가상민우", "dashboard_order_staff_nm", "가상민우", []),
        ("발주담당 가상민우 SIMS 일일점검", "dashboard_order_staff_nm", "가상민우", []),
        ("SIMS 일일점검 제약담당자 가상민우", "dashboard_pharma_staff_nm", "가상민우", []),
        ("SIMS 일일점검 제약담당 가상민우", "dashboard_pharma_staff_nm", "가상민우", []),
        ("SIMS 일일점검 제약사 삼진", "manufacturer_test_query", "삼진", [("삼진", "manufacturer")]),
        ("SIMS 일일점검 삼진", "manufacturer_test_query", "삼진", [("삼진", "manufacturer")]),
        ("SIMS 일일점검 발주처 삼진", "order_vendor_codes", ["V1"], [("삼진", "order_vendor")]),
        ("SIMS 일일점검", "manufacturer_test_query", "", []),
    )
    with (patch("app.ui.ssai_login.get_selected_company", return_value={"company_id": 4}),
          patch("app.services.product_supplier_scope_service.resolve_supplier_vendor_codes", side_effect=supplier)):
        for question, key, expected, supplier_calls in cases:
            calls.clear()
            assert parse_incomplete_sims_help_request(question) is None
            params, notice = nlq_router._build_dashboard_nlq_params(
                question, session_state={"__analysis_profile_company_cache": {"4": {}}}, logger=logger,
            )
            assert notice is None and params[key] == expected, (question, params, notice)
            if key in {"dashboard_order_staff_nm", "dashboard_pharma_staff_nm"}:
                label = "order_staff_label" if key == "dashboard_order_staff_nm" else "pharma_staff_label"
                assert params[label] == expected and not params.get("manufacturer_test_query"), question
            assert calls == supplier_calls, (question, calls)
            assert nlq_router.resolve_new_sims_nlq_candidate(question)["action"] == "SIMS 일일점검"

    sent, executed = [], []
    def dashboard_result(params, **kwargs):
        executed.append(dict(params))
        return (
            {"meta": {"result_status": "success", "source_call_count": 3}},
            {"params": dict(params), "facts": {"source_call_count": 3, "inventory": {"readiness_rows": [{}]}}},
        )
    with (patch("app.ui.ssai_login.get_selected_company", return_value={"company_id": 4}),
          patch("app.services.ssai_analysis_profile_service.load_dashboard_profile", side_effect=AssertionError("DB")),
          patch("app.services.product_supplier_scope_service.resolve_supplier_vendor_codes", side_effect=AssertionError("supplier")),
          patch("app.sims.views.dashboard_lite.build_dashboard_lite_result_payload", side_effect=dashboard_result),
          patch("app.sims.views.dashboard_lite.dashboard_request_publish_allowed", return_value=True),
          patch("app.ui.chat_middleware.get_current_chat_room_id", return_value="fixture-room"),
          patch("app.ui.chat_middleware.push_sims_result_to_chat", side_effect=lambda payload, action: sent.append((payload, action)))):
        for question in ("발주담당 신 SIMS 일일점검", "SIMS 일일점검 발주담당자 신"):
            assert nlq_router._try_handle_dashboard_nlq(
                question, room={"id": "fixture-room"},
                session_state={"__analysis_profile_company_cache": {"4": {}}}, logger=logger,
            )
    assert len(executed) == len(sent) == 2
    assert all(params["dashboard_order_staff_nm"] == params["order_staff_label"] == "신" for params in executed)
    assert all(action == "SIMS 일일점검" and payload["meta"]["result_status"] == "success"
               and payload["meta"]["source_call_count"] == 3 for payload, action in sent)


def _check_ambiguous_document_help() -> None:
    source = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8-sig")
    func = next(node for node in ast.parse(source).body
                if isinstance(node, ast.FunctionDef) and node.name == "is_sims_result_followup_question")
    namespace = {
        "re": re,
        "st": SimpleNamespace(session_state={"__sims_ctx": {"action": "발주조회"}}),
        "_looks_like_explicit_sims_nlq_command": lambda _: False,
        "parse_incomplete_sims_help_request": parse_incomplete_sims_help_request,
        "is_explicit_current_table_followup_reference": lambda text: "현재표" in text,
    }
    exec(compile(ast.Module(body=[func], type_ignores=[]), "<followup-check>", "exec"), namespace)
    is_followup = namespace["is_sims_result_followup_question"]

    for question in ("오늘 련제표 보여워", "오늘 전표 보여줘", "어제 정산표 조회해줘",
                     "오늘 매출표 보여줘", "출고빈도등급표 보여줘"):
        intent = parse_incomplete_sims_help_request(question)
        assert intent and intent.reason == "document_type_confirmation"
        assert not is_followup(question)
        assert not verified_sims_help_examples(intent, allowed_actions={"발주조회", "발주 계산"})
        answer = verified_sims_help_text(intent, allowed_actions={"발주조회", "발주 계산"})
        assert question in answer and "전표 종류" in answer
        assert "발주조회" not in answer and "발주 조회" not in answer
        with patch.object(nlq_router, "_try_handle_io_nlq", side_effect=AssertionError("ERP route")):
            assert not nlq_router.try_handle_nlq(
                question, room={"messages": []}, session_state={}, make_ts=lambda: "now",
                next_seq=lambda: 1, logger=logging.getLogger(__name__),
            )

    for question in ("현재표 구분별 분석", "이 표 분석해줘", "현재표 월별 집계"):
        assert parse_incomplete_sims_help_request(question) is None
        assert is_followup(question)
    for question, action in (
        ("매출 추세 요약표 2025년 조회", "품목별 매출 추세 요약표"),
        ("제조사별 매출 추세 요약표 2025년 조회", "제약사별 매출 추세 분석 요약표"),
        ("매출추세요약표 2025년 조회", "품목별 매출 추세 요약표"),
    ):
        assert parse_incomplete_sims_help_request(question) is None
        candidate = nlq_router.resolve_new_sims_nlq_candidate(question)
        assert candidate and candidate["route"] == "analytics" and candidate["action"] == action
        period = nlq_router._extract_analytics_year_range(question)
        assert period["month_from"] == "202501" and period["month_to"] == "202512"
    for question in ("오늘 발주조회 보여줘", "오늘 거래명세서 보여줘"):
        assert parse_incomplete_sims_help_request(question) is None
        assert nlq_router.resolve_new_sims_nlq_candidate(question) is not None


def _check_condition_help_examples() -> None:
    from app.sims.views.dashboard_lite import DASHBOARD_LITE_OPTION_CACHE_KEY, DASHBOARD_LITE_OPTION_CACHE_VERSION

    names = {"00001": "본사창고", "00901": "오토스토어"}
    cache = {
        "__dashboard_lite_profile_loaded_for": "4",
        DASHBOARD_LITE_OPTION_CACHE_KEY: {
            "cache_version": DASHBOARD_LITE_OPTION_CACHE_VERSION,
            "stock_code_to_name": names,
        },
    }
    with patch("app.sims.views.rddbc_io_shared._load_stock_code_options", side_effect=AssertionError("ERP options")):
        assert cached_company_stock_location_names(cache, 4) == names
        assert cached_company_stock_location_names(cache, 3) == {}
        assert cached_company_stock_location_names({}, 4) == {}
        assert cached_company_stock_location_names({**cache, "__dashboard_lite_profile_loaded_for": "3"}, 4) == {}
        stale = {**cache, DASHBOARD_LITE_OPTION_CACHE_KEY: {"cache_version": -1, "stock_code_to_name": names}}
        assert cached_company_stock_location_names(stale, 4) == {}
    for question in (
        "발주계산 결제일 20일 본사창고 + 오토스토어",
        "발주담당자 가상민우 발주계산 결제일 20일 본사창고 + 오토스토어",
    ):
        intent = parse_incomplete_sims_help_request(question)
        assert intent and intent.reason == "condition_confirmation"
        assert intent.subject_label == "재고위치"
        assert intent.subject_value == "본사창고 + 오토스토어"
        for available in ({}, names):
            examples = verified_sims_help_examples(
                intent, allowed_actions={"발주 계산"}, stock_location_names=available,
            )
            assert len(examples) == 1 and examples[0][0] == "발주 계산"
            example = examples[0][1]
            assert example == question.replace("본사창고 + 오토스토어", "재고위치 본사창고 + 오토스토어")
            reparsed = resolve_io_nlq(example)
            assert reparsed["action"] == "발주 계산"
            assert reparsed["params"]["closing_day"] == 20
            assert reparsed["params"]["stock_nm_list"] == ["본사창고", "오토스토어"]
            if "발주담당자" in question:
                assert reparsed["params"]["order_staff_nm"] == "가상민우"
            assert parse_incomplete_sims_help_request(example) is None
            text = verified_sims_help_text(intent, allowed_actions={"발주 계산"}, stock_location_names=available)
            assert "\n\n확인하지 못한 조건: 본사창고 + 오토스토어" in text
            assert example in text and "이번 발주계산은 실행하지 않았습니다" in text
            assert "다음과 같이 질문해 주세요.\n\n" in text
            assert ("현재 재고위치 등록 여부는 확인하지 않았습니다" in text) == (not available)
        assert not verified_sims_help_examples(intent, allowed_actions=set(), stock_location_names=names)

    unknown = parse_incomplete_sims_help_request("발주계산 결제일 20일 미등록창고")
    assert unknown and unknown.subject_label == "재고위치"
    unknown_format = verified_sims_help_examples(
        unknown, allowed_actions={"발주 계산"}, stock_location_names={},
    )
    assert unknown_format and resolve_io_nlq(unknown_format[0][1])["params"]["stock_nm_list"] == ["미등록창고"]
    unknown_text = verified_sims_help_text(unknown, allowed_actions={"발주 계산"}, stock_location_names={})
    assert "확인하지 못한 조건: 미등록창고" in unknown_text
    assert "현재 재고위치 등록 여부는 확인하지 않았습니다" in unknown_text
    assert not verified_sims_help_examples(unknown, allowed_actions={"발주 계산"}, stock_location_names=names)
    cached_text = verified_sims_help_text(unknown, allowed_actions={"발주 계산"}, stock_location_names=names)
    assert "재고위치 미등록창고" not in cached_text
    assert "등록된 재고위치" in cached_text


def _check_user_export() -> None:
    approved = (
        "Rd06_User_Cd", "Rd06_User_ID", "Rd06_Sabun", "Rd06_User_Nm",
        "부서명", "직책", "영업지역", "재고위치", "이메일", "휴대폰",
        "전화번호", "사무실전화", "등록자코드", "등록자명", "등록일자",
        "수정자코드", "수정자명", "수정일자", "삭제여부",
    )
    for size in (1, 28, 231, 621):
        raw = pd.DataFrame({key: [f"값{i}" for i in range(size)] for key in approved})
        raw["Rd06_User_Cd"] = [f"{i:05d}" for i in range(size)]
        for index in range(24):
            raw[f"internal_{index}"] = "hidden"
        raw["Rd06_Password"] = "secret"
        raw["Rd06_Jumin"] = "secret"
        assert len(raw.columns) == 45
        view = _build_user_list_view(raw)
        assert len(view.columns) == 19
        with (patch.object(nlq_users, "_push_result_payload", side_effect=lambda **kw: kw["result"]),
              patch("app.sims.views.users._build_users_master_llm_summary", return_value={})):
            result = nlq_users._push_table_result(
                room={"messages": []}, session_state={}, df=raw, df_display=view,
                title="사용자목록 + 부서명", action="사용자목록 + 부서명", txt="사용자 조회",
                params={}, query_summary="전체", logger=logging.getLogger(__name__),
            )
        assert len(result["df"]) == size
        assert len(result["df_display"]) == min(size, 500)
        assert list(result["df"]) == list(result["df_display"])
        assert not any(str(column).startswith("Rd06_") or str(column).startswith("internal_") for column in result["df"])
        result = _normalize_result_for_chat(result)
        assert len(result["df"]) == size and len(result["df_display"]) <= min(size, 500)
        source = _resolve_payload_full_download_source(result, result["meta"], display_df=result["df_display"])
        assert source["source_status"] in {"full", "complete_display"}
        assert len(source["df"]) == size
        assert len(pd.read_csv(io.BytesIO(source["df"].to_csv(index=False).encode("utf-8-sig")), dtype=str)) == size
        workbook = io.BytesIO()
        source["df"].to_excel(workbook, index=False)
        workbook.seek(0)
        assert len(pd.read_excel(workbook, dtype=str)) == size
        with (patch.object(nlq_users, "_push_result_payload", side_effect=lambda **kw: kw["result"]),
              patch("app.sims.views.users._build_users_master_llm_summary", return_value={})):
            try:
                nlq_users._push_table_result(
                    room={"messages": []}, session_state={}, df=raw.iloc[:-1], df_display=view,
                    title="사용자목록 + 부서명", action="사용자목록 + 부서명", txt="사용자 조회",
                    params={}, query_summary="전체", logger=logging.getLogger(__name__),
                )
            except ValueError:
                pass
            else:
                raise AssertionError("Incomplete user export was accepted")


def main() -> None:
    _check_calculation_conditions()
    _check_dashboard_help()
    _check_dashboard_staff_condition()
    _check_ambiguous_document_help()
    _check_condition_help_examples()
    _check_user_export()
    print("PASS: NLQ conditions, verified Dashboard help, user CSV/Excel 1/28/231/621")


if __name__ == "__main__":
    main()
