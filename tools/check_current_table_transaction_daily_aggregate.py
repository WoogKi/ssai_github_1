"""In-memory regression gate for transaction-document time aggregations."""

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
    handle_current_table_followup_by_action,
)


LOG = logging.getLogger(__name__)
SOURCE_ACTION = "거래명세서 공통 조회"
TABLE_KEY = "transaction-daily-aggregate-fixture"


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


def _frame(*, include_date: bool = True) -> pd.DataFrame:
    rows = [
        {"거래명세서일자": "20260901", "거래명세서구분": "1", "공급가액": 100, "세액": 10, "합계금액": 110},
        {"거래명세서일자": "20260903", "거래명세서구분": "1", "공급가액": 300, "세액": 30, "합계금액": 330},
        {"거래명세서일자": "20260901", "거래명세서구분": "3", "공급가액": 200, "세액": 20, "합계금액": 220},
        {"거래명세서일자": "20260902", "거래명세서구분": "3", "공급가액": 400, "세액": 40, "합계금액": 440},
        # A source-table subtotal must not become a dated business row.
        {"거래명세서일자": "합계", "거래명세서구분": "", "공급가액": 9_000, "세액": 900, "합계금액": 9_900},
    ]
    df = pd.DataFrame(rows)
    return df.drop(columns=["거래명세서일자"]) if not include_date else df


def _dispatch(query: str, frame: pd.DataFrame) -> tuple[str, dict[str, Any]]:
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
        table_key=TABLE_KEY,
        source_action=SOURCE_ACTION,
        helpers={
            "find_col": _find_col,
            "to_num": _to_num,
            "push_table": push_table,
            "push_notice": push_notice,
        },
        log=LOG,
        source_meta={"result_status": "success"},
    )
    if not handled or len(pushed) != 1:
        raise AssertionError(f"query={query!r}, handled={handled!r}, pushed={pushed!r}")
    return pushed[0]


def _assert_capability(query: str, frame: pd.DataFrame, *, status: str, grouping: str) -> None:
    capability = _current_table_followup_capability(
        df=frame,
        query=query,
        source_action=SOURCE_ACTION,
        kind="trans_doc",
        source_meta={"result_status": "success"},
    )
    actual = (capability["status"], capability["requested_metric"], capability["requested_grouping"])
    expected = (status, "transaction_amount", grouping)
    if actual != expected:
        raise AssertionError(f"query={query!r}, capability={actual!r}, expected={expected!r}")


def _assert_table(
    query: str,
    frame: pd.DataFrame,
    *,
    expected_count: int,
    expected_amount: float,
    expected_rows: int | None = None,
    expects_split: bool | None = None,
    grouping: str = "day",
) -> None:
    _assert_capability(query, frame, status="success", grouping=grouping)
    kind, payload = _dispatch(query, frame)
    if kind != "table":
        raise AssertionError(f"query={query!r}, expected table, payload={payload!r}")
    result = payload["df"]
    if "거래금액" not in result.columns:
        raise AssertionError(f"query={query!r}, canonical 거래금액 missing: {list(result.columns)!r}")
    if int(_to_num(result["건수"]).sum()) != expected_count:
        raise AssertionError(f"query={query!r}, count={result['건수'].tolist()!r}")
    if float(_to_num(result["거래금액"]).sum()) != expected_amount:
        raise AssertionError(f"query={query!r}, amount={result['거래금액'].tolist()!r}")
    if expected_rows is not None and len(result) != expected_rows:
        raise AssertionError(f"query={query!r}, rows={len(result)}, expected={expected_rows}")
    if grouping == "day":
        if "일자" not in result.columns or not result["일자"].astype(str).map(lambda value: bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))).all():
            raise AssertionError(f"query={query!r}, unstable day={result.get('일자')!r}")
    if expects_split is not None and (("거래명세서구분" in result.columns) != expects_split):
        raise AssertionError(f"query={query!r}, split columns={list(result.columns)!r}")
    if payload.get("source_table_key") != TABLE_KEY or payload.get("source_rows") != len(frame):
        raise AssertionError(f"query={query!r}, provenance={payload!r}")
    if payload.get("extra_meta", {}).get("source_call_count") != 0:
        raise AssertionError(f"query={query!r}, unexpected source calls={payload!r}")
    print(f"PASS {query} rows={len(result)} count={expected_count} amount={expected_amount:g}")


def _assert_llm_handoff(query: str, frame: pd.DataFrame) -> None:
    if classify_current_table_followup_intent(query) != "llm_analysis":
        raise AssertionError(f"query={query!r}, classifier is not llm_analysis")
    facts = build_current_table_interpretive_facts(
        df=frame,
        query=query,
        source_action=SOURCE_ACTION,
        source_meta={"result_status": "success"},
    )
    if facts.get("status") != "success" or facts.get("missing_columns"):
        raise AssertionError(f"query={query!r}, facts={facts!r}")
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
        table_key=TABLE_KEY,
        source_action=SOURCE_ACTION,
        helpers={
            "find_col": _find_col,
            "to_num": _to_num,
            "push_table": push_table,
            "push_notice": push_notice,
        },
        log=LOG,
        source_meta={"result_status": "success"},
    )
    if handled or pushed:
        raise AssertionError(f"query={query!r}, deterministic handler intercepted LLM: {pushed!r}")
    print(f"PASS {query} llm_analysis")


def main() -> int:
    frame = _frame()
    for query in (
        "현재표 거래명세서 일자별 집계",
        "현재표 거래명세서일자별 집계",
        "현재표 거래명세서일자 집계",
        "현재표 일자별 집계",
        "현재표 일자 집계",
        "현재표 일자별 거래금액",
        "현재표 일자별 거래금액 집계",
    ):
        _assert_table(query, frame, expected_count=4, expected_amount=1_100, expected_rows=4, expects_split=True)

    for query in ("현재표 출고 일자별 집계", "현재표 출고일자별 집계", "현재표 매출 일자별 집계"):
        _assert_table(query, frame, expected_count=2, expected_amount=660, expected_rows=2, expects_split=False)
    for query in ("현재표 입고 일자별 집계", "현재표 입고일자별 집계", "현재표 매입 일자별 집계"):
        _assert_table(query, frame, expected_count=2, expected_amount=440, expected_rows=2, expects_split=False)
    for query in ("현재표 일자별 통합 집계", "현재표 일자별 통합집계", "현재표 일자별 매입매출 합계"):
        _assert_table(query, frame, expected_count=4, expected_amount=1_100, expected_rows=3, expects_split=False)

    _assert_table("현재표 월별 집계", frame, expected_count=4, expected_amount=1_100, expected_rows=2, expects_split=True, grouping="month")
    _assert_table("현재표 거래명세서월 집계", frame, expected_count=4, expected_amount=1_100, expected_rows=2, expects_split=True, grouping="month")
    _assert_table("현재표 거래명세서 월별 집계", frame, expected_count=4, expected_amount=1_100, expected_rows=2, expects_split=True, grouping="month")
    _assert_table("현재표 요일별 집계", frame, expected_count=4, expected_amount=1_100, expected_rows=3, expects_split=False, grouping="weekday")
    _assert_table("현재표 요일 집계", frame, expected_count=4, expected_amount=1_100, expected_rows=3, expects_split=False, grouping="weekday")

    _assert_llm_handoff("현재표 거래명세서 일자 분석해줘", frame)
    _assert_llm_handoff("현재표 거래명세서 일자별 분석해줘", frame)
    _assert_llm_handoff("현재표 거래명세서일자 분석해줘", frame)

    missing_date = _frame(include_date=False)
    capability = _current_table_followup_capability(
        df=missing_date,
        query="현재표 일자별 집계",
        source_action=SOURCE_ACTION,
        kind="trans_doc",
        source_meta={"result_status": "success"},
    )
    if capability["status"] != "column_unavailable":
        raise AssertionError(f"missing-date capability={capability!r}")
    kind, payload = _dispatch("현재표 일자별 집계", missing_date)
    if kind != "notice" or payload.get("title") != "현재표 컬럼 부족":
        raise AssertionError(f"missing-date payload={payload!r}")
    print("PASS missing date column -> column_unavailable")
    print("RESULT: PASS (25 cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
