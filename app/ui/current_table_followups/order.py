"""Current-table deterministic time summaries for purchase-order results."""

from __future__ import annotations

from typing import Any, Callable

import pandas as pd

from app.ui.current_table_followups.time_grouping import derive_current_table_time_grouping


def handle_order_followup(
    *,
    df: pd.DataFrame,
    query: str,
    top_n: int,
    table_key: str,
    source_action: str,
    helpers: dict[str, Callable[..., Any]],
    log: Any,
) -> bool:
    """Aggregate an order-like current table using the dispatcher-selected date."""
    requested_grouping = str(helpers.get("_requested_grouping") or "").strip()
    if not (
        bool(helpers.get("_is_deterministic_request"))
        and requested_grouping in {"month", "day", "weekday"}
    ):
        return False

    date_col = str(helpers.get("_resolved_date_column") or "").strip()
    push_table = helpers["push_table"]
    push_notice = helpers["push_notice"]
    to_num = helpers.get("to_num") or (
        lambda values: pd.to_numeric(values, errors="coerce").fillna(0)
    )
    is_expected_inbound = "입고예정" in str(source_action or "").replace(" ", "")
    source_label = "입고예정" if is_expected_inbound else "발주"
    if not date_col or date_col not in df.columns:
        return bool(push_notice(
            title=f"현재표 {source_label} 일자 집계 불가",
            action=f"현재표 {source_label} 일자 집계 불가",
            message=f"현재표에는 {source_label} 일자 집계에 필요한 날짜 컬럼이 없습니다.",
            query_summary=f"현재표 / {source_label} 일자 집계 불가",
            source_query=query,
        ))

    group_column = {"month": "월", "weekday": "요일", "day": "일자"}[requested_grouping]
    grouping_values = derive_current_table_time_grouping(df[date_col], requested_grouping)
    work = pd.DataFrame({group_column: grouping_values}, index=df.index)
    work = work[work[group_column].ne("")].copy()
    if work.empty:
        return bool(push_notice(
            title=f"현재표 {source_label} 일자 집계 결과 없음",
            action=f"현재표 {source_label} 일자 집계 결과 없음",
            message=f"현재표에서 유효한 {date_col} 값을 찾지 못했습니다.",
            query_summary=f"현재표 / {source_label} 일자 집계 결과 없음 / 0건",
            source_query=query,
            extra_meta={"execution_status": "no_data", "result_status": "no_data"},
        ))

    for column in ("발주수량", "입고수량", "미입고수량"):
        if column in df.columns:
            work[column] = to_num(df.loc[work.index, column])
    work["건수"] = 1

    aggregations: dict[str, tuple[str, str]] = {"건수": ("건수", "sum")}
    for column in ("발주수량", "입고수량", "미입고수량"):
        if column in work.columns:
            aggregations[column] = (column, "sum")
    result = work.groupby(group_column, dropna=False).agg(**aggregations).reset_index()
    if requested_grouping == "weekday":
        result["_weekday_order"] = result[group_column].map({"월요일": 0, "화요일": 1, "수요일": 2, "목요일": 3, "금요일": 4, "토요일": 5, "일요일": 6})
        result = result.sort_values("_weekday_order").drop(columns=["_weekday_order"])
    else:
        result = result.sort_values(group_column)
    result = result.reset_index(drop=True)
    result.insert(0, "순번", range(1, len(result) + 1))

    title = f"현재표 {date_col} 기준 {group_column}별 {source_label} 집계"
    try:
        log.info(
            "[chat.followup_table] %s time aggregate built date_col=%s grouping=%s source_rows=%s rows=%s table_key=%s",
            source_label,
            date_col,
            requested_grouping,
            len(df),
            len(result),
            table_key,
        )
    except Exception:
        pass
    return bool(push_table(
        title=title,
        action=title,
        df=result,
        query_summary=f"현재표 / {date_col} 기준 {group_column}별 {source_label} 집계 / 전체 {len(df):,}건 기준",
        source_query=query,
        source_table_key=table_key,
        source_rows=len(df),
        display_limit=None,
    ))
