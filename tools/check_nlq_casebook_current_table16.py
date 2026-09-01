"""Focused in-memory audit for the 16 current-table casebook REVIEW cases."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ui.current_table_followups.action_dispatcher import handle_current_table_followup_by_action


LOG = logging.getLogger(__name__)
TABLE_KEY = "casebook-current-table-fixture"


@dataclass(frozen=True)
class Case:
    case_id: str
    source_action: str
    query: str
    runtime_query: str
    frame: pd.DataFrame
    expected_kind: str
    expected_status: str
    expected_rows: int | None
    disposition: str
    reason: str


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


def _dispatch(case: Case) -> tuple[bool, str, dict[str, Any]]:
    pushed: list[tuple[str, dict[str, Any]]] = []

    def push_table(**kwargs: Any) -> bool:
        pushed.append(("table", kwargs))
        return True

    def push_notice(**kwargs: Any) -> bool:
        pushed.append(("notice", kwargs))
        return True

    handled = handle_current_table_followup_by_action(
        df=case.frame.copy(deep=True),
        query=case.runtime_query,
        top_n=20,
        table_key=TABLE_KEY,
        source_action=case.source_action,
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
        raise AssertionError(f"handled={handled!r}, pushed={pushed!r}")
    kind, payload = pushed[0]
    return handled, kind, payload


def _inventory_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"제품명": "제품A", "제조사명": "한미약품", "재고수량": 10, "재고금액": 100},
            {"제품명": "제품B", "제조사명": "한미약품", "재고수량": 0, "재고금액": 0},
            {"제품명": "제품C", "제조사명": "다른제약", "재고수량": -2, "재고금액": -20},
        ]
    )


def _forecast_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"제품명": "제품A", "예상등급": "감소예상", "다음월예상매출": 100},
            {"제품명": "제품B", "예상등급": "안정", "다음월예상매출": 200},
            {"제품명": "제품C", "예상등급": "감소예상", "다음월예상매출": 50},
        ]
    )


def _trend_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"지역": "서울", "추세판정": "증가", "매출금액": 100},
            {"지역": "부산", "추세판정": "감소", "매출금액": 50},
            {"지역": "대구", "추세판정": "증가", "매출금액": 75},
        ]
    )


def _shortage_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"제품명": "제품A", "적용증감율": 5, "예상등급": "감소예상", "부족예상수량": 10},
            {"제품명": "제품B", "적용증감율": 12, "예상등급": "안정", "부족예상수량": 0},
            {"제품명": "제품C", "적용증감율": 8, "예상등급": "감소예상", "부족예상수량": 3},
        ]
    )


def _sales_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"출고일자": "20260801", "제품명": "제품A", "수량": 1, "공급가액": 100, "세액": 0},
            {"출고일자": "20260802", "제품명": "제품B", "수량": 10, "공급가액": 50, "세액": 0},
        ]
    )


def _trans_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "거래명세서일자": "20260801",
                "거래처명": "대학약국",
                "거래명세서구분": "1",
                "공급가액": 10,
                "세액": 1,
                "합계금액": 11,
                "상세합계일치": "Y",
            },
            {
                "거래명세서일자": "20260802",
                "거래처명": "다른약국",
                "거래명세서구분": "3",
                "공급가액": 20,
                "세액": 2,
                "합계금액": 22,
                "상세합계일치": "N",
            },
        ]
    )


def _cases() -> tuple[Case, ...]:
    inventory = _inventory_frame()
    forecast = _forecast_frame()
    trend = _trend_frame()
    shortage = _shortage_frame()
    sales = _sales_frame()
    trans = _trans_frame()
    return (
        Case("NLQ-0022", "제품재고장", "현재표 제조사명별 분석", "현재표 제조사명별 분석", inventory, "table", "success", 2, "PASS", "제조사 group"),
        Case("NLQ-0023", "제품재고장", "현재표 제조사명 한미약품 상세히 보여줘", "현재표 제조사명 한미약품 상세히 보여줘", inventory, "table", "success", 2, "PASS", "제조사 filter"),
        Case("NLQ-0025", "제품재고장", "현재표 재고수량이 가장 많은 제품", "현재표 재고수량이 가장 많은 제품", inventory, "table", "success", 1, "PASS", "재고수량 rank"),
        Case("NLQ-0026", "제품재고장", "현재표 재고수량 0 이하 목록", "현재표 재고수량 0 이하 목록", inventory, "table", "success", 2, "PASS", "재고수량 filter"),
        Case("NLQ-0027", "제품재고장", "현재표 재고수량 0 이상 목록", "현재표 재고수량 0 이상 목록", inventory, "table", "success", 2, "PASS", "재고수량 filter"),
        Case("NLQ-0031", "제품재고장", "현재표 재고금액이 가장 많은 제품", "현재표 재고금액이 가장 많은 제품", inventory, "table", "success", 1, "PASS", "재고금액 rank"),
        Case("NLQ-0063", "품목별 매출 예상", "현재표 예상등급 분석", "현재표 예상등급 분석", forecast, "table", "success", 2, "PASS", "예상등급 group"),
        Case("NLQ-0064", "품목별 매출 예상", "예상등급 감소예상 상세히 보여줘", "현재표 예상등급 감소예상 상세히 보여줘", forecast, "table", "success", 2, "PASS", "implicit current-table filter normalization"),
        Case("NLQ-0076", "지역별 매출현황", "추세판정 요약", "현재표 추세판정 요약", trend, "table", "success", 2, "PASS", "implicit current-table group normalization"),
        Case("NLQ-0081", "품목별 재고부족현황", "현재표 적용증감율 < 10", "현재표 적용증감율 < 10", shortage, "table", "success", 2, "PASS", "적용증감율 numeric filter"),
        Case("NLQ-0082", "품목별 재고부족현황", "현재표 예상등급 감소예상 상세히 보여줘", "현재표 예상등급 감소예상 상세히 보여줘", shortage, "table", "success", 2, "PASS", "예상등급 filter"),
        Case("NLQ-0115", "출고명세 조회", "현재표 매출수량이 가장 많은 제품", "현재표 매출수량이 가장 많은 제품", sales, "table", "success", 1, "PASS", "매출수량 product rank"),
        Case("NLQ-0172", "거래명세서 공통 조회", "현재표 상세합계 불일치 목록", "현재표 상세합계 불일치 목록", trans, "table", "success", 1, "PASS", "상세합계 N filter"),
        Case("NLQ-0178", "거래명세서 공통 조회", "현재표 거래처명 대학약국 상세표", "현재표 거래처명 대학약국 상세표", trans, "table", "success", 1, "PASS", "거래처 detail filter"),
        Case("NLQ-0179", "거래명세서 공통 조회", "현재표에서 가장 입고 횟수가 많은 제품은?", "현재표에서 가장 입고 횟수가 많은 제품은?", trans, "notice", "unsupported", None, "PASS", "공식 unsupported: 거래명세서 헤더 현재표에는 product grain 없음"),
        Case("NLQ-0180", "거래명세서 공통 조회", "현재표 입고횟수 1위 제품", "현재표 입고횟수 1위 제품", trans, "notice", "unsupported", None, "PASS", "공식 unsupported: 거래명세서 헤더 현재표에는 product grain 없음"),
    )


def main() -> int:
    failures: list[str] = []
    for case in _cases():
        try:
            _handled, kind, payload = _dispatch(case)
            meta = dict(payload.get("extra_meta") or {})
            result_df = payload.get("df")
            status = str(meta.get("result_status") or "")
            rows = len(result_df) if isinstance(result_df, pd.DataFrame) else None
            if kind != case.expected_kind or status != case.expected_status:
                raise AssertionError(f"kind={kind}, status={status}, payload={payload!r}")
            if case.expected_rows is not None and rows != case.expected_rows:
                raise AssertionError(f"rows={rows}, expected={case.expected_rows}, payload={payload!r}")
            if payload.get("source_table_key") != TABLE_KEY or payload.get("source_rows") != len(case.frame):
                raise AssertionError(f"provenance={payload!r}")
            print(
                f"PASS {case.case_id} disposition={case.disposition} kind={kind} "
                f"status={status} rows={rows if rows is not None else '-'}"
            )
        except Exception as exc:
            failures.append(f"FAIL {case.case_id} {case.query}: {type(exc).__name__}: {exc}")

    for failure in failures:
        print(failure)
    print(f"SUMMARY total=16 pass={16 - len(failures)} fail={len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
