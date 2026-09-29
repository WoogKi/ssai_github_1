"""In-memory regression gate for current-table source date authorities."""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ui.current_table_followups.action_dispatcher import (
    _current_table_followup_capability,
    build_current_table_interpretive_facts,
    classify_current_table_followup_intent,
    detect_current_table_kind,
    handle_current_table_followup_by_action,
)


LOG = logging.getLogger(__name__)


def _find_col(
    df: pd.DataFrame,
    *,
    exact: tuple[str, ...] = (),
    include_any: tuple[str, ...] = (),
    exclude_any: tuple[str, ...] = (),
) -> str | None:
    columns = [str(column) for column in df.columns]
    for candidate in exact:
        if candidate in columns:
            return candidate
    for column in columns:
        if include_any and not any(token in column for token in include_any):
            continue
        if any(token in column for token in exclude_any):
            continue
        return column
    return None


def _to_num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(0)


def _dispatch(query: str, frame: pd.DataFrame, action: str) -> tuple[str, dict[str, Any]]:
    pushed: list[tuple[str, dict[str, Any]]] = []

    def push_table(**kwargs: Any) -> bool:
        pushed.append(("table", kwargs))
        return True

    def push_notice(**kwargs: Any) -> bool:
        pushed.append(("notice", kwargs))
        return True

    handled = handle_current_table_followup_by_action(
        df=frame.copy(deep=True),
        query=query,
        top_n=20,
        table_key="source-date-authority-fixture",
        source_action=action,
        helpers={"find_col": _find_col, "to_num": _to_num, "push_table": push_table, "push_notice": push_notice},
        log=LOG,
        source_meta={"result_status": "success"},
    )
    if not handled or len(pushed) != 1:
        raise AssertionError(f"query={query!r}, action={action!r}, handled={handled!r}, pushed={pushed!r}")
    return pushed[0]


def _assert_table(query: str, frame: pd.DataFrame, action: str, *, grouping: str, date_column: str) -> pd.DataFrame:
    kind = detect_current_table_kind(action)
    capability = _current_table_followup_capability(
        df=frame,
        query=query,
        source_action=action,
        kind=kind,
        source_meta={"result_status": "success"},
    )
    if capability["status"] != "success" or capability["requested_grouping"] != grouping:
        raise AssertionError(f"capability query={query!r}: {capability!r}")
    event, payload = _dispatch(query, frame, action)
    if event != "table":
        raise AssertionError(f"query={query!r}, expected table, payload={payload!r}")
    if payload.get("extra_meta", {}).get("source_call_count") != 0:
        raise AssertionError(f"query={query!r}, source calls changed: {payload!r}")
    result = payload["df"]
    if int(_to_num(result["건수"]).sum()) != len(frame):
        raise AssertionError(f"query={query!r}, count mismatch={result!r}")
    result_amount_column = next(
        (column for column in ("입고금액", "매출금액", "거래금액") if column in result.columns),
        "",
    )
    if result_amount_column:
        source_amount_column = "합계금액" if "합계금액" in frame.columns else result_amount_column
        if float(_to_num(result[result_amount_column]).sum()) != float(_to_num(frame[source_amount_column]).sum()):
            raise AssertionError(f"query={query!r}, amount mismatch={result!r}")
    for column in ("발주수량", "입고수량", "미입고수량"):
        if column in result.columns and float(_to_num(result[column]).sum()) != float(_to_num(frame[column]).sum()):
            raise AssertionError(f"query={query!r}, {column} mismatch={result!r}")
    if grouping == "month":
        values = result["월"].dropna().astype(str).tolist()
        if not values or not all(re.fullmatch(r"\d{4}-\d{2}", value) for value in values):
            raise AssertionError(f"query={query!r}, month display values={values!r}")
    if grouping == "weekday":
        values = result["요일"].dropna().astype(str).tolist()
        if not values or not all(value in {"월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"} for value in values):
            raise AssertionError(f"query={query!r}, weekday values={values!r}")
    print(f"PASS {action} / {query} -> {date_column} / {grouping}")
    return result


def _facts_group_values(facts: dict[str, Any]) -> list[str]:
    whole = facts.get("whole_table_facts") or {}
    distribution = whole.get("group_distribution") or facts.get("group_distribution") or []
    if distribution:
        return [str(item.get("group", "")) for item in distribution]
    rows = facts.get("facts") or []
    group_column = facts.get("group_column") or whole.get("group_column")
    return [str(row.get(group_column, "")) for row in rows if group_column]


def _assert_llm(
    query: str,
    frame: pd.DataFrame,
    action: str,
    *,
    grouping: str,
    date_column: str,
) -> None:
    if classify_current_table_followup_intent(query) != "llm_analysis":
        raise AssertionError(f"query={query!r} did not classify as llm_analysis")
    facts = build_current_table_interpretive_facts(
        df=frame,
        query=query,
        source_action=action,
        source_meta={"result_status": "success"},
    )
    fact_group_column = facts.get("group_column") or facts.get("whole_table_facts", {}).get("group_column")
    expected_group_column = {"day": "일자", "month": "월", "weekday": "요일"}[grouping]
    if (
        facts.get("status") != "success"
        or fact_group_column != expected_group_column
        or facts.get("date_source_column") != date_column
    ):
        raise AssertionError(f"LLM facts query={query!r}: {facts!r}")
    values = [value for value in _facts_group_values(facts) if value]
    if grouping == "weekday" and (not values or not all(value.endswith("요일") for value in values)):
        raise AssertionError(f"LLM weekday facts query={query!r}: {values!r}")
    if grouping == "month" and (not values or not all(re.fullmatch(r"\d{4}-\d{2}", value) for value in values)):
        raise AssertionError(f"LLM month facts query={query!r}: {values!r}")
    print(f"PASS {action} / {query} -> LLM {date_column} / {expected_group_column}")


def _assert_unavailable(
    query: str,
    frame: pd.DataFrame,
    action: str,
    *,
    missing_column: str,
    forbidden_default: str = "",
) -> None:
    kind = detect_current_table_kind(action)
    capability = _current_table_followup_capability(
        df=frame,
        query=query,
        source_action=action,
        kind=kind,
        source_meta={"result_status": "success"},
    )
    if capability.get("status") != "column_unavailable":
        raise AssertionError(f"query={query!r}, expected unavailable: {capability!r}")
    missing = list(capability.get("missing_columns") or [])
    if missing_column not in missing:
        raise AssertionError(f"query={query!r}, missing label lost: {capability!r}")
    if forbidden_default and forbidden_default in (capability.get("available_columns") or []):
        raise AssertionError(f"query={query!r}, silently substituted {forbidden_default!r}: {capability!r}")
    if classify_current_table_followup_intent(query) == "llm_analysis":
        facts = build_current_table_interpretive_facts(
            df=frame,
            query=query,
            source_action=action,
            source_meta={"result_status": "success"},
        )
        facts_missing = list((facts.get("capability") or {}).get("missing_columns") or [])
        if facts.get("status") != "column_unavailable" or missing_column not in facts_missing:
            raise AssertionError(f"query={query!r}, expected unavailable LLM facts: {facts!r}")
    else:
        event, payload = _dispatch(query, frame, action)
        result_status = str((payload.get("extra_meta") or {}).get("result_status") or "")
        if event != "notice" or result_status != "column_unavailable":
            raise AssertionError(f"query={query!r}, expected unavailable notice: {event!r}, {payload!r}")
    print(f"PASS {action} / {query} -> column_unavailable {missing_column}")


def _order_frame() -> pd.DataFrame:
    return pd.DataFrame([
        {"발주일자": "20260901", "납기일자": "20260903", "발주거래처명": "A", "제품명": "P1", "발주수량": 10, "입고수량": 4, "미입고수량": 6},
        {"발주일자": "20260902", "납기일자": "20260903", "발주거래처명": "B", "제품명": "P2", "발주수량": 20, "입고수량": 10, "미입고수량": 10},
        {"발주일자": "20261001", "납기일자": "20261005", "발주거래처명": "A", "제품명": "P3", "발주수량": 30, "입고수량": 0, "미입고수량": 30},
    ])


def _detail_frame(date_column: str) -> pd.DataFrame:
    return pd.DataFrame([
        {date_column: "20260901", "제품명": "P1", "거래처명": "A", "수량": 10, "공급가액": 100, "세액": 10, "합계금액": 110},
        {date_column: "20260902", "제품명": "P2", "거래처명": "B", "수량": 20, "공급가액": 200, "세액": 20, "합계금액": 220},
    ])


def main() -> int:
    order = _order_frame()
    default_day = _assert_table("현재표 일자 집계", order, "발주조회", grouping="day", date_column="발주일자")
    _assert_table("현재표 발주일자별 집계", order, "발주조회", grouping="day", date_column="발주일자")
    explicit_order = _assert_table("현재표 발주일자 집계", order, "발주조회", grouping="day", date_column="발주일자")
    due_day = _assert_table("현재표 납기일자 집계", order, "발주조회", grouping="day", date_column="납기일자")
    _assert_table("현재표 요일 집계", order, "발주조회", grouping="weekday", date_column="발주일자")
    if default_day[["일자", "건수"]].equals(due_day[["일자", "건수"]]):
        raise AssertionError("발주일자와 납기일자 grouping이 서로 달라야 합니다.")
    if not default_day[["일자", "건수"]].equals(explicit_order[["일자", "건수"]]):
        raise AssertionError("기본 일자 authority가 발주일자와 다릅니다.")
    _assert_table("현재표 월별 집계", order, "발주조회", grouping="month", date_column="발주일자")
    _assert_llm("현재표 발주일자 분석해줘", order, "발주조회", grouping="day", date_column="발주일자")
    _assert_llm("현재표 일자 분석해줘", order, "발주조회", grouping="day", date_column="발주일자")
    _assert_llm("현재표 납기일자 분석해줘", order, "발주조회", grouping="day", date_column="납기일자")
    _assert_llm("현재표 요일별 분석", order, "발주조회", grouping="weekday", date_column="발주일자")
    _assert_llm("현재표 발주일자 요일별 분석", order, "발주조회", grouping="weekday", date_column="발주일자")
    _assert_llm("현재표 납기일자 요일별 분석", order, "발주조회", grouping="weekday", date_column="납기일자")
    _assert_llm("현재표 월별 분석", order, "발주조회", grouping="month", date_column="발주일자")

    expected_inbound = _order_frame()
    expected_default_day = _assert_table("현재표 입고예정 일자별 집계", expected_inbound, "입고예정조회", grouping="day", date_column="발주일자")
    _assert_table("현재표 입고예정 월별 집계", expected_inbound, "입고예정조회", grouping="month", date_column="발주일자")
    _assert_table("현재표 입고예정 요일별 집계", expected_inbound, "입고예정조회", grouping="weekday", date_column="발주일자")
    expected_order_day = _assert_table("현재표 입고예정 발주일자별 집계", expected_inbound, "입고예정조회", grouping="day", date_column="발주일자")
    expected_due_day = _assert_table("현재표 입고예정 납기일자별 집계", expected_inbound, "입고예정조회", grouping="day", date_column="납기일자")
    _assert_table("현재표 입고예정 납기일자 기준 집계", expected_inbound, "입고예정조회", grouping="day", date_column="납기일자")
    if not expected_default_day[["일자", "건수"]].equals(expected_order_day[["일자", "건수"]]):
        raise AssertionError("입고예정 기본 일자 authority가 발주일자와 다릅니다.")
    if expected_default_day[["일자", "건수"]].equals(expected_due_day[["일자", "건수"]]):
        raise AssertionError("입고예정 발주일자와 납기일자 grouping이 서로 달라야 합니다.")
    _assert_llm("현재표 일자 분석해줘", expected_inbound, "입고예정조회", grouping="day", date_column="발주일자")
    _assert_llm("현재표 납기일자 분석해줘", expected_inbound, "입고예정조회", grouping="day", date_column="납기일자")
    _assert_llm("현재표 요일별 분석", expected_inbound, "입고예정조회", grouping="weekday", date_column="발주일자")

    inbound = _detail_frame("입고일자")
    for query, grouping in (("현재표 일자 집계", "day"), ("현재표 일자별 집계", "day"), ("현재표 입고일자 집계", "day"), ("현재표 요일 집계", "weekday")):
        _assert_table(query, inbound, "입고명세 조회", grouping=grouping, date_column="입고일자")
    _assert_table("현재표 월별 집계", inbound, "입고명세 조회", grouping="month", date_column="입고일자")
    _assert_llm("현재표 입고일자 분석해줘", inbound, "입고명세 조회", grouping="day", date_column="입고일자")
    _assert_llm("현재표 일자 분석해줘", inbound, "입고명세 조회", grouping="day", date_column="입고일자")
    _assert_llm("현재표 요일별 분석", inbound, "입고명세 조회", grouping="weekday", date_column="입고일자")
    _assert_llm("현재표 월별 분석", inbound, "입고명세 조회", grouping="month", date_column="입고일자")

    outbound = _detail_frame("출고일자")
    for query, grouping in (("현재표 일자 집계", "day"), ("현재표 일자별 집계", "day"), ("현재표 출고일자 집계", "day"), ("현재표 요일 집계", "weekday")):
        _assert_table(query, outbound, "출고명세 조회", grouping=grouping, date_column="출고일자")
    _assert_table("현재표 월별 집계", outbound, "출고명세 조회", grouping="month", date_column="출고일자")
    _assert_llm("현재표 출고일자 분석해줘", outbound, "출고명세 조회", grouping="day", date_column="출고일자")
    _assert_llm("현재표 일자 분석해줘", outbound, "출고명세 조회", grouping="day", date_column="출고일자")
    _assert_llm("현재표 요일별 분석", outbound, "출고명세 조회", grouping="weekday", date_column="출고일자")
    _assert_llm("현재표 월별 분석", outbound, "출고명세 조회", grouping="month", date_column="출고일자")

    trans = _detail_frame("거래명세서일자")
    trans["거래명세서구분"] = "1"
    _assert_table("현재표 거래명세서일자 집계", trans, "거래명세서 공통 조회", grouping="day", date_column="거래명세서일자")
    _assert_table("현재표 일자 집계", trans, "거래명세서 공통 조회", grouping="day", date_column="거래명세서일자")
    _assert_table("현재표 요일 집계", trans, "거래명세서 공통 조회", grouping="weekday", date_column="거래명세서일자")
    _assert_table("현재표 월별 집계", trans, "거래명세서 공통 조회", grouping="month", date_column="거래명세서일자")
    _assert_llm("현재표 거래명세서일자 분석해줘", trans, "거래명세서 공통 조회", grouping="day", date_column="거래명세서일자")
    _assert_llm("현재표 요일별 분석", trans, "거래명세서 공통 조회", grouping="weekday", date_column="거래명세서일자")
    _assert_llm("현재표 월별 분석", trans, "거래명세서 공통 조회", grouping="month", date_column="거래명세서일자")

    _assert_unavailable("현재표 출고일자 집계", order, "발주조회", missing_column="출고일자", forbidden_default="발주일자")
    _assert_unavailable("현재표 입고일자 분석해줘", order, "발주조회", missing_column="입고일자", forbidden_default="발주일자")
    _assert_unavailable("현재표 납기일자 집계", inbound, "입고명세 조회", missing_column="납기일자", forbidden_default="입고일자")
    _assert_unavailable("현재표 출고일자 분석해줘", inbound, "입고명세 조회", missing_column="출고일자", forbidden_default="입고일자")
    _assert_unavailable("현재표 입고일자 집계", outbound, "출고명세 조회", missing_column="입고일자", forbidden_default="출고일자")
    _assert_unavailable("현재표 발주일자 분석해줘", trans, "거래명세서 공통 조회", missing_column="발주일자", forbidden_default="거래명세서일자")

    ambiguous = pd.DataFrame({"등록일자": ["20260901"], "수량": [1]})
    capability = _current_table_followup_capability(
        df=ambiguous, query="현재표 일자 집계", source_action="거래처 목록", kind="generic", source_meta={"result_status": "success"}
    )
    if capability["status"] != "column_unavailable":
        raise AssertionError(f"no-authority source chose a date: {capability!r}")
    print("PASS no-authority source remains column_unavailable")
    no_date = pd.DataFrame({"거래처명": ["A"], "수량": [1]})
    _assert_unavailable("현재표 등록일자 집계", no_date, "거래처 목록", missing_column="등록일자")
    print("RESULT: PASS (56 cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
