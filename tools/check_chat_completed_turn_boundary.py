"""Offline production-branch gate for completed SIMS turns and LLM payloads."""

from __future__ import annotations

import ast
from contextlib import nullcontext
import json
import logging
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch
import uuid

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.sims.nlq.nlq_router import resolve_new_sims_nlq_candidate
from app.ui.current_table_followups.action_dispatcher import (
    build_current_table_interpretive_facts,
    classify_current_table_followup_intent,
    current_table_analysis_query_matches,
    select_current_table_analysis_context,
)
from app.ui.current_table_followups.analysis_facts import fit_analysis_context


SOURCE = Path(__file__).resolve().parents[1] / "app/Lmstudio_SSAI_chat_main.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
FUNCTIONS = {
    node.name: node for node in TREE.body
    if isinstance(node, ast.FunctionDef)
}


def _queue_block() -> ast.If:
    for node in ast.walk(TREE):
        if (isinstance(node, ast.If) and node.lineno > 14000
                and "__queue_ai" in ast.unparse(node.test)
                and "st.session_state.get" in ast.unparse(node.test)):
            return node
    raise AssertionError("production __queue_ai branch missing")


def _route_block(test: str) -> ast.If:
    matches = [node for node in ast.walk(TREE)
               if isinstance(node, ast.If) and 13000 < node.lineno < 13250
               and ast.unparse(node.test) == test]
    assert len(matches) == 1, (test, len(matches))
    return matches[0]


def _namespace() -> dict:
    names = (
        "_is_sims_owned_history_message", "_completed_general_history",
        "_normalize_for_lmstudio", "build_messages_with_system",
        "_current_result_analysis_reference", "_current_table_get_latest_df",
        "_prepare_current_table_analysis_override",
    )
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    body.extend(FUNCTIONS[name] for name in names)
    namespace = {
        "st": SimpleNamespace(session_state={}), "log": logging.getLogger("chat_boundary_gate"),
        "pd": pd, "re": re, "json": json, "uuid": uuid, "time": time,
        "Optional": Optional, "KNOWLEDGE_ANSWER_MESSAGE_TYPE": "knowledge_answer",
        "GENERAL_SYSTEM_PROMPT": "ordinary chat", "BASE_SYSTEM_PROMPT": "SIMS chat",
        "is_general_writing_request": lambda _q: False,
        "is_sims_related_question": lambda q: "발주계산" in q or "매입조회" in q,
        "is_sims_result_followup_question": lambda _q: False,
        "get_sims_context_data": lambda **_kw: None,
        "get_sims_context_text": lambda **_kw: None,
        "_clip_for_model": lambda text, limit=20000: str(text)[:limit],
        "_script_perf_add": lambda *_a: None,
        "_is_explicit_current_table_writing_request": lambda _q: False,
        "select_current_table_analysis_context": select_current_table_analysis_context,
        "classify_current_table_followup_intent": classify_current_table_followup_intent,
        "build_current_table_interpretive_facts": build_current_table_interpretive_facts,
        "fit_analysis_context": fit_analysis_context,
        "build_response_format_instruction": lambda *_a, **_kw: "현재 질문에만 답하세요.",
        "SIMS_JSON_CHAR_LIMIT": 12000,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(SOURCE), "exec"), namespace)
    return namespace


def _room(company: str = "4", room_id: str = "r1") -> dict:
    return {"id": room_id, "company": company, "messages": [], "sims_messages": [], "gen_messages": []}


def _lookup(room: dict, query: str, key: str, *, status: str = "success") -> None:
    room["messages"].extend([
        {"role": "user", "content": query},
        {"role": "assistant", "type": "table", "title": "발주 계산", "action": "발주 계산",
         "meta": {"table_key": key, "result_status": status}, "content": "발주 계산"},
    ])
    room["sims_messages"].append({"role": "user", "content": query})


def _run_queue(ns: dict, room: dict, query: str, *, override: dict | None = None) -> list[dict]:
    room["messages"].append({"role": "user", "content": query})
    ns["current_room"] = room
    state = ns["st"].session_state
    state["__queue_ai"] = True
    state["__sims_open"] = True
    if override is not None:
        state["__current_table_analysis_ctx_override"] = override
        state["__current_table_analysis_query"] = query
    sent: list[list[dict]] = []
    ns.update({
        "is_sims_related_question": lambda q: "발주계산" in q or "매입조회" in q,
        "is_sims_result_followup_question": lambda _q: False,
        "current_table_analysis_query_matches": current_table_analysis_query_matches,
        "_attachment_reanalysis_context": lambda _room: {},
        "parse_knowledge_followup_queue_request": lambda *_a, **_kw: None,
        "parse_explicit_knowledge_request": lambda _q: None,
        "parse_business_help_knowledge_request": lambda _q: None,
        "parse_explicit_mcp_resource_request": lambda _q: None,
        "parse_web_search_followup_request": lambda *_a, **_kw: None,
        "latest_ready_web_search_message": lambda _m: None,
        "parse_web_search_request": lambda _q: None,
        "pending_area": nullcontext(),
        "stream_and_append_assistant": lambda **kw: sent.append(ns["_normalize_for_lmstudio"](kw["messages_for_ai"])),
    })
    with patch("app.services.datetime_tool.operating_llm_context", return_value="KST 2026-10-04"):
        exec(compile(ast.Module(body=[_queue_block()], type_ignores=[]), str(SOURCE), "exec"), ns)
    assert len(sent) == 1
    return sent[0]


def _assert_current_once(payload: list[dict], query: str) -> None:
    assert payload[-1]["role"] == "user"
    assert payload[-1]["content"].count(query) == 1
    assert all(query not in item["content"] for item in payload[:-1])


def _route_to_handoff(ns: dict, room: dict, query: str, candidate_action: str) -> bool:
    state = ns["st"].session_state
    key = str(state.get("__sims_current_table_source_key") or "")
    named, brief = ns["_current_result_analysis_reference"](
        query, candidate_action=candidate_action, source_key=key,
        latest_assistant={"meta": {"table_key": key}} if key else {},
    )
    assert named or brief
    ns.update({
        "st": ns["st"], "current_room": room, "handled": False,
        "current_table_followup_input": query, "named_result_analysis": named,
        "source_action": str(state.get("__sims_current_table_source_action") or ""),
        "new_sims_action": candidate_action,
        "is_current_table_forced_followup": True,
        "is_implicit_analytics_current_followup": False,
        "_has_current_table_source_df": lambda: bool(key and
            isinstance((state.get("__sims_export_tables_by_key") or {}).get(key), pd.DataFrame)),
        "_try_handle_current_table_dataframe_followup": lambda *_a, **_kw: False,
        "_push_no_current_table_notice": lambda _q: True,
        "make_ts": lambda: "now", "_next_seq": lambda: 1,
    })
    # Execute the production source/no-source decision, then its production
    # llm_analysis handoff branch. No ERP service is bound in this namespace.
    exec(compile(ast.Module(body=[_route_block("is_current_table_forced_followup or is_implicit_analytics_current_followup")],
                            type_ignores=[]), str(SOURCE), "exec"), ns)
    if not ns["handled"]:
        ns["current_table_intent"] = classify_current_table_followup_intent(query)
        exec(compile(ast.Module(body=[_route_block("current_table_intent == 'llm_analysis'")],
                                type_ignores=[]), str(SOURCE), "exec"), ns)
    return not ns["handled"]


def test_completed_turn_and_general_followups() -> None:
    ns = _namespace()
    room = _room()
    _lookup(room, "담당자 김 202609 발주계산", "t1", status="no_data")
    _lookup(room, "담당자 김 202610 발주계산", "t2")
    payload = _run_queue(ns, room, "발주계산 결과 분석해줘")
    assert [m["role"] for m in payload] == ["user", "assistant", "user", "assistant", "user"]
    assert "no_data" in payload[1]["content"] and "success" in payload[3]["content"]
    assert "202609" in payload[0]["content"] and "202610" in payload[2]["content"]
    _assert_current_once(payload, "발주계산 결과 분석해줘")

    room["messages"].append({"role": "assistant", "content": "현재표의 근거를 설명했습니다."})
    ns["st"].session_state["__sims_open"] = False
    payload = _run_queue(ns, room, "그럼 이전 기간은?")
    assert payload[-3]["role"] == "user" and "발주계산 결과 분석해줘" in payload[-3]["content"]
    assert payload[-2]["role"] == "assistant" and "설명했습니다" in payload[-2]["content"]
    _assert_current_once(payload, "그럼 이전 기간은?")

    other = _room(room_id="r2")
    _lookup(other, "제품코드 조회", "x1")
    payload = _run_queue(ns, other, "내일 날씨는?")
    assert "발주계산" not in str(payload) and payload[-1]["content"].endswith("내일 날씨는?")


def test_current_table_handoff_and_source_boundary() -> None:
    from tools.check_order_calculation_contract import fixture
    from app.services.order_calculation_service import assemble_result

    ns = _namespace()
    room = _room()
    _lookup(room, "지난달 발주계산", "old")
    _lookup(room, "이번달 발주계산", "current")
    state = ns["st"].session_state
    notices: list[dict] = []
    ns["_current_table_push_notice"] = lambda **kw: notices.append(kw)
    params, sources = fixture()
    full = assemble_result(params, sources)
    full.loc[full.index[0], "실제 발주수량"] = 77
    fact_source_values: list[object] = []
    def capture_facts(**kwargs):
        fact_source_values.append(kwargs["df"].iloc[0]["실제 발주수량"])
        return build_current_table_interpretive_facts(**kwargs)
    ns["build_current_table_interpretive_facts"] = capture_facts
    state.update({
        "__sims_current_table_source_key": "current", "__sims_current_table_source_action": "발주 계산",
        "__sims_export_tables_by_key": {"current": full},
        "__sims_analysis_ctx_by_table_key": {"current": {
            "kind": "SIMS_ANALYSIS_CONTEXT_V1", "table_key": "current",
            "action": "발주 계산", "source_meta": {"table_key": "current"},
        }},
    })
    read_calls: list[str] = []
    ns["erp_read"] = lambda *_a: read_calls.append("read")
    for query, candidate in (("발주계산 결과 분석해줘", "발주 계산"), ("왜 이렇게 많이 나왔어?", "")):
        assert _route_to_handoff(ns, room, query, candidate)
        override = state.pop("__current_table_analysis_ctx_override")
        state.pop("__current_table_analysis_query")
        assert override["source_table_key"] == "current"
        assert override["whole_table_facts"] or override["current_table_interpretive_facts"]
        payload = _run_queue(ns, room, query, override=override)
        assert len(payload) == 1 and payload[-1]["role"] == "user"
        assert "current" in payload[0]["content"] and "old" not in payload[0]["content"]
        _assert_current_once(payload, query)
        room["messages"].append({"role": "assistant", "content": "현재표 기준으로 설명했습니다."})
    assert not read_calls
    assert fact_source_values == [77, 77]

    # Explicit recalculation remains a new NLQ candidate, not a table explanation.
    query = "조건을 바꿔 다시 발주계산해줘"
    assert not any(ns["_current_result_analysis_reference"](
        query, candidate_action="발주 계산", source_key="current", latest_assistant={}
    ))
    assert resolve_new_sims_nlq_candidate(query) is not None

    # A mismatched table, missing source, or changed room/company must not borrow this context.
    state["__sims_export_tables_by_key"]["other"] = full.copy()
    state["__sims_current_table_source_key"] = "other"
    assert not _route_to_handoff(ns, room, "발주계산 결과 분석해줘", "발주 계산")
    assert len(notices) == 1 and "컨텍스트" in notices[0]["title"]
    # Company switch clears the old table/context authority before any handoff.
    room = _room(company="7", room_id="company7-room")
    state["__sims_current_table_source_key"] = ""
    state["__sims_current_table_source_action"] = ""
    state["__sims_export_tables_by_key"] = {}
    state["__sims_analysis_ctx_by_table_key"] = {}
    assert not _route_to_handoff(ns, room, "발주계산 결과 분석해줘", "발주 계산")
    assert len(notices) == 1
    assert not read_calls


if __name__ == "__main__":
    test_completed_turn_and_general_followups()
    test_current_table_handoff_and_source_boundary()
    print("PASS: completed-turn Chat branch and current-table LLM payload")
