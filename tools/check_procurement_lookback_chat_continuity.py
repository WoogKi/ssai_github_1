"""Focused offline contract for order-unit lookback and room chat continuity."""

from __future__ import annotations

import ast
from datetime import date
from decimal import Decimal
import logging
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _chat_functions(*names: str) -> dict:
    source = (Path(__file__).resolve().parents[1] / "app/Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    nodes = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {"re": re, "log": logging.getLogger("fixture"),
                 "KNOWLEDGE_ANSWER_MESSAGE_TYPE": "knowledge_answer"}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "chat_functions", "exec"), namespace)
    return namespace


def _row(day: str, quantity: object, *, vendor: str = "00100") -> dict:
    return {"발주일자": day, "발주수량": quantity, "제품코드": "00001", "발주거래처코드": vendor}


def test_unit_windows() -> None:
    from app.services.order_calculation_contract import recommend_quantity
    from app.services.order_calculation_service import _infer_unit_from_windows, build_order_unit_history_params

    reference = date(2026, 10, 31)
    params = build_order_unit_history_params({"policy_date": "20261031"}, reference=reference)
    assert (params["date_from"], params["date_to"]) == ("20260731", "20261031")
    assert params["_order_unit_history_minimal"] is True
    recent_start, period = "20260930", ["20260731", "20261031"]

    def infer(rows: list[dict], **kwargs):
        return _infer_unit_from_windows(rows, recent_start=recent_start, period=period, **kwargs)

    recent = [_row("20261001", 10), _row("20261005", 20), _row("20261010", 10)]
    old = [_row("20260801", 5), _row("20260805", 5), _row("20260810", 10)]
    unit, reason, count = infer(old + recent)
    assert unit == 10 and count == 3 and "20260930~20261031" in reason
    unit, reason, count = infer([_row("20260801", 10), _row("20260805", 40),
                                 _row("20260810", 10), _row("20261002", 40)])
    assert unit == 10 and count == 1 and "20260731~20261031" in reason
    assert recommend_quantity(Decimal("53.043956"), unit, increasing=True) == 60
    assert infer([_row("20260801", 10), _row("20260805", 40)])[0] is None
    changed = [_row("20260801", 10), _row("20260805", 10), _row("20260810", 20),
               _row("20261001", 20), _row("20261005", 20)]
    assert infer(changed)[0] is None and "최소 발주수량 변화" in infer(changed)[1]
    for rows in (
        [_row("20260801", 10), _row("20260805", 10), _row("20261002", 15)],
        [_row("20261001", 10), _row("20261005", 13), _row("20261010", 10)] + old,
        [_row("20261001", "1.5"), _row("20261005", 3), _row("20261010", "1.5")] + old,
    ):
        assert infer(rows)[0] is None
    assert infer(recent + old, full_complete=False)[0] == 10
    assert infer(old, full_complete=False)[0] is None
    assert infer(recent, recent_complete=False)[0] is None
    assert infer([])[0] is None


def test_unit_source_one_call_and_assembly() -> None:
    from app.services.order_calculation_service import (
        build_order_unit_history_params, assemble_result, _unit_history_completeness,
    )
    from app.services.rddbc170_rddbc180_order_service import get_order_df
    from tools.check_order_calculation_contract import fixture

    params = build_order_unit_history_params({"policy_date": "20261003"}, reference=date(2026, 10, 3))
    assert params["date_from"] == "20260703"
    with patch("app.services.rddbc170_rddbc180_order_service.execute_bound_select", return_value=pd.DataFrame()) as read:
        get_order_df(params, mode="order")
    assert read.call_count == 1
    sql, binds = read.call_args.args
    assert "Rddbc180" in sql and "Rd18_Quantity" in sql and "Rd18_Oquantity" not in sql.split("AS [발주수량]")[0]
    assert "20260703" in binds and "20261003" in binds
    assert "Rd18_Or_Di IN" in sql
    assert all(code in binds for code in ("1", "2", "3"))
    detail = pd.DataFrame([
        {**_row("20261001", 10), "발주순번": 1, "상세순번": 1},
        {**_row("20260901", 10), "발주순번": 2, "상세순번": 1},
        {**_row("20260801", 10), "발주순번": 3, "상세순번": 1},
    ])
    assert _unit_history_completeness(detail, recent_start="20260903", top=10) == (True, True)
    assert _unit_history_completeness(detail, recent_start="20260903", top=3) == (True, False)
    assert _unit_history_completeness(detail.iloc[:1], recent_start="20260903", top=1) == (False, False)
    assert _unit_history_completeness(pd.concat([detail, detail.iloc[[2]]]),
                                      recent_start="20260903", top=10) == (True, False)
    assert _unit_history_completeness(pd.concat([detail, detail.iloc[[0]]]),
                                      recent_start="20260903", top=10) == (False, False)

    order_params, sources = fixture()
    sources["order_history_unit"] = pd.DataFrame([
        _row("20260710", 10), _row("20260720", 40), _row("20260801", 10), _row("20260901", 40),
    ])
    sources["order_history_unit_period"] = ["20260624", "20260924"]
    sources["order_history_unit_recent_period"] = ["20260824", "20260924"]
    sources["order_history_complete"] = sources["order_history_recent_complete"] = True
    before = assemble_result(order_params, {**sources, "order_history_unit": pd.DataFrame()}).iloc[0]
    after = assemble_result(order_params, sources).iloc[0]
    assert before["발주단위"] is None and after["발주단위"] == 10
    for column in ("계산 발주수량", "재고수량", "입고예정수량", "발주단가", "단가판정", "계산상태"):
        assert before[column] == after[column], column
    assert after["최근 발주횟수"] == 1
    sources["suppliers"] = sources["suppliers"].assign(recent_inbound_vendor_code="00999")
    assert assemble_result(order_params, sources).iloc[0]["발주단위"] is None
    sources["suppliers"] = sources["suppliers"].assign(recent_inbound_vendor_code="00100")
    sources["order_history_complete"] = False
    assert assemble_result(order_params, sources).iloc[0]["발주단위"] is None


def test_chat_payload_and_result_reference() -> None:
    from app.services.datetime_tool import operating_llm_context
    from app.services.erp_table_nlq import is_order_calculation_request
    from app.ui.current_table_followups.action_dispatcher import (
        classify_current_table_followup_intent, build_current_table_interpretive_facts,
    )
    from app.services.order_calculation_service import assemble_result
    from tools.check_order_calculation_contract import fixture

    ns = _chat_functions("_is_sims_owned_history_message", "_completed_general_history",
                         "_current_result_analysis_reference", "_normalize_for_lmstudio", "build_messages_with_system")
    ns.update({
        "st": SimpleNamespace(session_state={}),
        "is_sims_related_question": lambda q: "발주계산" in q or "매입조회" in q,
        "is_general_writing_request": lambda _q: False,
        "GENERAL_SYSTEM_PROMPT": "ordinary chat", "BASE_SYSTEM_PROMPT": "SIMS chat",
        "get_sims_context_data": lambda **_kw: None, "get_sims_context_text": lambda **_kw: None,
        "is_sims_result_followup_question": lambda _q: False,
        "_clip_for_model": lambda text: text,
        "_script_perf_add": lambda *_args: None,
        "classify_current_table_followup_intent": classify_current_table_followup_intent,
        "time": __import__("time"),
    })
    history = [
        {"role": "user", "content": "발주계산"},
        {"role": "assistant", "content": "완료", "type": "sims_result", "meta": {"table_key": "t1"}},
        {"role": "user", "content": "내일 아산에 가요"},
        {"role": "assistant", "content": "좋은 여행 되세요"},
        {"role": "user", "content": "그럼 비가 올까요?"},
    ]
    with patch("app.services.datetime_tool.operating_llm_context", return_value="KST 2026-10-03"):
        payload = ns["_normalize_for_lmstudio"](ns["build_messages_with_system"](history, user_text="그럼 비가 올까요?"))
    assert [m["role"] for m in payload] == ["user", "assistant", "user"]
    assert "내일 아산에 가요" in payload[0]["content"] and "발주계산" not in str(payload)
    assert payload[-1]["content"] == "그럼 비가 올까요?"
    assert "KST 2026-10-03" in payload[0]["content"]
    history.extend([{"role": "assistant", "content": "실시간 날씨는 확인할 수 없습니다"},
                    {"role": "user", "content": "모레는?"}])
    with patch("app.services.datetime_tool.operating_llm_context", return_value="KST 2026-10-03"):
        payload = ns["_normalize_for_lmstudio"](ns["build_messages_with_system"](history, user_text="모레는?"))
    assert payload[-1]["content"] == "모레는?" and "그럼 비가 올까요?" in str(payload)

    consecutive = [{"role": "user", "content": "내일 아산에 가요"},
                   {"role": "user", "content": "그럼 비가 올까요?"}]
    with patch("app.services.datetime_tool.operating_llm_context", return_value="KST 2026-10-03"):
        payload = ns["_normalize_for_lmstudio"](ns["build_messages_with_system"](consecutive, user_text="그럼 비가 올까요?"))
    assert "PRIOR_USER_CONTEXT" in payload[0]["content"]
    assert payload[-1]["content"].endswith("그럼 비가 올까요?")

    named = ns["_current_result_analysis_reference"](
        "발주계산 결과 분석해줘", candidate_action="발주 계산", source_key="t1", latest_assistant={})
    assert named == (True, False) and is_order_calculation_request("발주계산 결과 분석해줘")
    brief = ns["_current_result_analysis_reference"](
        "왜 이렇게 많이 나왔어?", candidate_action="", source_key="t1",
        latest_assistant={"meta": {"table_key": "t1"}})
    assert brief == (False, True)
    assert ns["_current_result_analysis_reference"](
        "조건을 바꿔 다시 발주계산해줘", candidate_action="발주 계산", source_key="t1",
        latest_assistant={}) == (False, False)
    assert ns["_current_result_analysis_reference"](
        "왜 이렇게 많이 나왔어?", candidate_action="", source_key="t2",
        latest_assistant={"meta": {"table_key": "t1"}}) == (False, False)
    params, sources = fixture()
    full = assemble_result(params, sources)
    for query in ("발주계산 결과 분석해줘", "왜 이렇게 많이 나왔어?"):
        facts = build_current_table_interpretive_facts(
            df=full, query=query, source_action="발주 계산", source_meta={"table_key": "t1"},
        )
        assert facts["status"] == "success" and facts["source_row_count"] == len(full)
    assert "_current_result_analysis_reference(" in (Path(__file__).resolve().parents[1] / "app/Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")


if __name__ == "__main__":
    test_unit_windows()
    test_unit_source_one_call_and_assembly()
    test_chat_payload_and_result_reference()
    print("PASS: procurement lookback and chat continuity")
