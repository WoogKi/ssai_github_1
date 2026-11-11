"""Offline checks for the three 2026-10-03 manual-smoke findings."""

from __future__ import annotations

import ast
import inspect
from contextlib import ExitStack
from datetime import date
from decimal import Decimal
import logging
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _main_functions(*names: str) -> dict:
    source = (Path(__file__).resolve().parents[1] / "app/Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if len(nodes) != len(names) or {node.name for node in nodes} != set(names):
        raise AssertionError("manual-smoke production functions missing or duplicated")
    namespace = {"re": __import__("re"), "log": logging.getLogger("fixture"),
                 "KNOWLEDGE_ANSWER_MESSAGE_TYPE": "knowledge_answer"}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "chat_functions", "exec"), namespace)
    return namespace


def test_vendor_router_arguments_and_status() -> None:
    from app.sims.nlq import nlq_router, nlq_vendors
    from app.services import rddbc030_service

    def run(question: str, *, error: bool = False) -> tuple[dict, dict]:
        calls, payloads = [], []

        def service(**kwargs):
            service.__signature__.bind(**kwargs)
            calls.append(kwargs)
            if error:
                raise RuntimeError("offline service error")
            return pd.DataFrame()

        service.__signature__ = inspect.signature(rddbc030_service.search_vendors_full)
        fixture_logger = logging.getLogger("fixture")

        with (patch.object(rddbc030_service, "search_vendors_full", new=service),
              patch.object(nlq_vendors, "push_sims_result_to_chat", side_effect=lambda payload, _action: payloads.append(payload)),
              patch.object(fixture_logger, "exception") as logged_exception):
            assert nlq_router.try_handle_nlq(question, room={}, session_state={},
                                             make_ts=lambda: "fixture", next_seq=lambda: 1,
                                             logger=fixture_logger)
        assert logged_exception.call_count == int(error)
        assert len(calls) == len(payloads) == 1, (question, calls, payloads)
        return calls[0], payloads[0]["meta"]

    plain, plain_meta = run("거래처 조회")
    prefixed, prefixed_meta = run("  SiMs 거래처 조회  ")
    assert plain == prefixed and not plain.get("ven_nm")
    assert plain_meta["result_status"] == prefixed_meta["result_status"] == "no_data"
    named, _ = run("sims 거래처명 경동 거래처 조회")
    assert named.get("ven_nm") == "경동", named
    literal, _ = run("sims 거래처명 sims 거래처 조회")
    assert literal["ven_nm"] == "sims"
    embedded, _ = run("거래처명 AcmeSims 거래처 조회")
    assert embedded["ven_nm"] == "AcmeSims"
    _, failed_meta = run("거래처 조회", error=True)
    assert failed_meta["result_status"] == "query_error"


def test_general_chat_payload() -> None:
    ns = _main_functions("_is_sims_owned_history_message", "_completed_general_history",
                         "_normalize_for_lmstudio", "build_messages_with_system")
    ns.update({
        "st": SimpleNamespace(session_state={}),
        "is_sims_related_question": lambda _q: False,
        "is_general_writing_request": lambda _q: False,
        "GENERAL_SYSTEM_PROMPT": "ordinary chat",
        "BASE_SYSTEM_PROMPT": "SIMS chat",
        "get_sims_context_data": lambda **_kw: None,
        "get_sims_context_text": lambda **_kw: None,
        "is_sims_result_followup_question": lambda q: "이 표" in q or "아까 조회" in q,
        "_build_current_room_compact_context": lambda **_kw: "past business result",
        "_clip_for_model": lambda text: text,
        "_script_perf_add": lambda *_args: None,
        "time": __import__("time"),
    })
    history = [
        {"role": "user", "content": "매출분석 오가논"},
        {"role": "assistant", "content": "조회조건 확인", "meta": {"nlq": True}},
        {"role": "user", "content": "제품코드 63431 조회"},
        {"role": "assistant", "content": "자료 없음", "meta": {"nlq": True}},
        {"role": "user", "content": "보통 무슨 날씨가 좋아?"},
        {"role": "assistant", "content": "맑은 날씨가 좋습니다."},
        {"role": "user", "content": "매입현황 2026"},
        {"role": "assistant", "content": "조회 완료", "meta": {"nlq": True}},
        {"role": "user", "content": "내일 아산 비와?"},
    ]
    with patch("app.services.datetime_tool.operating_llm_context", return_value="KST 2026-10-03"):
        payload = ns["_normalize_for_lmstudio"](ns["build_messages_with_system"](history, user_text="내일 아산 비와?"))
    assert [message["role"] for message in payload] == ["user", "assistant", "user"], payload
    assert payload[-1]["content"] == "내일 아산 비와?"
    assert "KST 2026-10-03" in payload[0]["content"]
    assert "매출분석" not in str(payload) and "63431" not in str(payload) and "매입현황" not in str(payload)
    assert "past business result" not in str(payload)
    assert ns["_completed_general_history"]([{"role": "user", "content": "내일 아산 비와?"}]) == [
        {"role": "user", "content": "내일 아산 비와?"}
    ]
    with patch("app.services.datetime_tool.operating_llm_context", return_value="KST 2026-10-03"):
        followup = ns["build_messages_with_system"](
            [{"role": "user", "content": "이 표를 분석해줘"}], user_text="이 표를 분석해줘"
        )
    assert "past business result" in str(followup)


def test_order_precision_boundary() -> None:
    from app.services.order_calculation_contract import demand_trend_adjustment, recommend_quantity
    from app.services.order_calculation_service import _exact_three_month_averages

    monthly = dict(zip((f"2026-{month:02d} 수량" for month in (9, 8, 7, 6, 5, 4)),
                       (110, 110, 110, 200, 200, 150)))
    old_rate, old_adj, _ = demand_trend_adjustment(
        recent_3m_avg=Decimal(110), previous_3m_avg=Decimal("183.33333333333334"),
        completed_months=6, frequency_grade="E",
    )
    old_raw = Decimal(110) * (1 + old_adj) * Decimal(15) / Decimal(20)
    assert old_raw < 66 and recommend_quantity(old_raw, Decimal(1), increasing=False) == 65
    recent, previous = _exact_three_month_averages(
        monthly, date(2026, 10, 6), Decimal(110), Decimal("183.33333333333334"), 6
    )
    rate, adjustment, _ = demand_trend_adjustment(
        recent_3m_avg=recent, previous_3m_avg=previous, completed_months=6, frequency_grade="E",
    )
    raw = Decimal(110) * (1 + adjustment) * Decimal(15) / Decimal(20)
    assert raw == 66 and recommend_quantity(raw, Decimal(1), increasing=False) == 66
    assert recommend_quantity(old_raw, None, increasing=False) == 60
    assert recommend_quantity(raw, None, increasing=False) == 60
    assert old_rate != rate and old_adj != adjustment
    assert _exact_three_month_averages({}, date(2026, 10, 6), Decimal(110), Decimal(120), 6) == (
        Decimal(110), Decimal(120)
    )
    for value in (Decimal("65.9999"), Decimal(66), Decimal("66.0001")):
        assert recommend_quantity(value, Decimal(1), increasing=False) == int(value)
        assert recommend_quantity(value, Decimal(1), increasing=True) == (66 if value <= 66 else 67)
    assert recommend_quantity(Decimal("65.9999"), Decimal(10), increasing=False) == 60
    assert recommend_quantity(Decimal("65.9999"), Decimal(10), increasing=True) == 70
    assert recommend_quantity(Decimal(0), None, increasing=False) == 0
    assert recommend_quantity(Decimal(3), Decimal(10), increasing=False) == 10

    from tools.check_order_calculation_contract import fixture
    from app.services.order_calculation_service import assemble_result

    params, sources = fixture()
    params.update(order_date="2026-10-06", safety_days=3, target_days=15, closing_day=25)
    sources["business_dates"] = [
        date(2026, 10, day) for day in range(1, 32)
        if date(2026, 10, day).weekday() < 5 and day not in (5, 9)
    ]
    sources["base"]["출고빈도등급"] = "E"
    demand = sources["demand"]
    demand["현재재고수량"] = Decimal(0)
    demand["예상기준월수량"] = Decimal(110)
    demand["최근3개월평균수요수량"] = 110.0
    demand["직전3개월평균수요수량"] = 183.33333333333334
    demand["완료월수"] = 6
    sources["pending"] = pd.DataFrame({"제품코드": ["00001"], "입고예정수량": [Decimal(0)]})
    default_sources = dict(sources)
    # Precision and default-unit policy are separate contracts. Confirm unit 1
    # through the existing three-distinct-order-days rule, not a service mock.
    order = sources["order_history"].iloc[0].to_dict()
    sources["order_history_unit"] = pd.DataFrame([
        {**order, "발주일자": f"202609{day}", "발주수량": Decimal(1)}
        for day in (20, 21, 22)
    ])
    before = assemble_result(params, sources).iloc[0]
    exact_sources = {**sources, "demand": demand.assign(**monthly)}
    after = assemble_result(params, exact_sources).iloc[0]
    assert before["계산 발주수량"] < 66 and before["추천 발주수량"] == 65
    assert after["계산 발주수량"] == 66 and after["추천 발주수량"] == 66
    assert before["발주단위"] == after["발주단위"] == Decimal(1)
    for column in ("재고수량", "입고예정수량", "발주단위", "발주단가", "단가출처", "단가판정", "계산상태", "발주처"):
        assert before[column] == after[column], column
    assert len(assemble_result(params, exact_sources)) == len(assemble_result(params, sources)) == 1
    for supplied in (default_sources, {**default_sources, "demand": demand.assign(**monthly)}):
        result = assemble_result(params, supplied).iloc[0]
        assert result["추천 발주수량"] == 60
        assert result["수량조정정책"] == "기본 조정단위10"


def main() -> None:
    if not __debug__:
        raise RuntimeError("Run without -O: this smoke gate requires assertions")
    # Block real I/O before importing service modules, including captured aliases.
    with ExitStack() as stack:
        guards = [stack.enter_context(patch(target, side_effect=AssertionError("DB/network forbidden in offline smoke")))
                  for target in ("pyodbc.connect", "sqlalchemy.engine.Engine.connect",
                                 "socket.socket.connect", "socket.socket.connect_ex")]
        for check in (test_vendor_router_arguments_and_status, test_general_chat_payload,
                      test_order_precision_boundary):
            check()
            for guard in guards:
                guard.assert_not_called()
            print(f"PASS: {check.__name__}")
    print("PASS: manual-smoke three issues")


if __name__ == "__main__":
    main()
