# tools/check_analytics_regression.py
# -*- coding: utf-8 -*-
# SIMS 분석/KPI 회귀 체크 도구
# 작성자: ChatGPT (2026-05-02)
# VERSION = "check_analytics_regression/2026-05-02-v1"
# 참고: 이 스크립트는SIMS 분석/KPI 회귀 여부를 점검하기 위한 도구입니다.

"""
Analytics/KPI regression checker.

기본 import/helper 확인:
    & "C:\\Program Files\\Python313\\python.exe" tools\\check_analytics_regression.py

실제 서비스 DB 조회 smoke test:
    & "C:\\Program Files\\Python313\\python.exe" tools\\check_analytics_regression.py --live

NLQ 라우팅까지 확인:
    & "C:\\Program Files\\Python313\\python.exe" tools\\check_analytics_regression.py --nlq

전체 확인:
    & "C:\\Program Files\\Python313\\python.exe" tools\\check_analytics_regression.py --live --nlq
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd


# ---------------------------------------------------------------------
# Project root 보정
# ---------------------------------------------------------------------
THIS_FILE = Path(__file__).resolve()
PROJECT_ROOT = THIS_FILE.parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.chdir(PROJECT_ROOT)


# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)

log = logging.getLogger("analytics_regression")


# ---------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------
@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class ServiceCase:
    name: str
    function_name: str
    params: dict[str, Any]
    expected_title_contains: str
    expected_meta_key: str | None = None
    expected_analysis_type: str | None = None
    expected_condition_tokens: tuple[str, ...] = ()
    require_seq_column: bool = True
    require_summary_md: bool = True
    require_message: bool = True
    allow_zero_rows: bool = False
    check_code_columns: bool = True


@dataclass
class NlqCase:
    query: str
    expected_action: str
    expected_analysis_type: str | None = None
    expected_meta_key: str | None = None
    expected_params: dict[str, Any] | None = None
    expected_condition_tokens: tuple[str, ...] = ()
    require_summary_md: bool = True
    require_message: bool = True
    allow_empty_meta_counts: bool = False

def _ok(name: str, detail: str = "") -> CheckResult:
    return CheckResult(name=name, ok=True, detail=detail)


def _fail(name: str, detail: str = "") -> CheckResult:
    return CheckResult(name=name, ok=False, detail=detail)


def _print_results(title: str, results: list[CheckResult]) -> int:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)

    failed = 0
    for r in results:
        mark = "OK " if r.ok else "FAIL"
        print(f"[{mark}] {r.name}")
        if r.detail:
            print(f"      {r.detail}")
        if not r.ok:
            failed += 1

    print("-" * 78)
    print(f"총 {len(results)}건 / 성공 {len(results) - failed}건 / 실패 {failed}건")
    return failed


def _safe_len_df(obj: Any) -> int | None:
    try:
        import pandas as pd
        if isinstance(obj, pd.DataFrame):
            return int(len(obj))
    except Exception:
        pass
    return None


def _payload_columns(payload: dict[str, Any]) -> list[str]:
    cols = payload.get("columns")
    if isinstance(cols, list) and cols:
        return [str(c) for c in cols]

    df_display = payload.get("df_display")
    try:
        import pandas as pd
        if isinstance(df_display, pd.DataFrame):
            return [str(c) for c in df_display.columns]
    except Exception:
        pass

    df = payload.get("df")
    try:
        import pandas as pd
        if isinstance(df, pd.DataFrame):
            return [str(c) for c in df.columns]
    except Exception:
        pass

    records = payload.get("records")
    if isinstance(records, list) and records and isinstance(records[0], dict):
        return [str(c) for c in records[0].keys()]

    return []


def _payload_df(payload: dict[str, Any]) -> Any:
    try:
        import pandas as pd
        for key in ("df", "df_display"):
            obj = payload.get(key)
            if isinstance(obj, pd.DataFrame):
                return obj
    except Exception:
        pass
    return None


def _code_column_dtype_problem(payload: dict[str, Any]) -> str:
    try:
        import pandas as pd
    except Exception:
        return ""

    df = _payload_df(payload)
    if not isinstance(df, pd.DataFrame) or df.empty:
        return ""

    code_cols = [
        "제품코드",
        "제조사코드",
        "거래처코드",
        "매입처코드",
        "재고적용처코드",
        "보험코드",
        "표준코드",
        "바코드",
    ]
    bad_cols = [
        col for col in code_cols
        if col in df.columns and pd.api.types.is_numeric_dtype(df[col])
    ]
    if bad_cols:
        return f"코드 컬럼이 numeric dtype으로 변환됨: {bad_cols!r}"
    return ""


def _payload_row_count(payload: dict[str, Any]) -> int:
    meta = payload.get("meta") or {}
    for key in ("row_count_total", "row_count"):
        try:
            v = meta.get(key)
            if v is not None:
                return int(v)
        except Exception:
            pass

    for key in ("df_display", "df"):
        n = _safe_len_df(payload.get(key))
        if n is not None:
            return n

    records = payload.get("records")
    if isinstance(records, list):
        return len(records)

    return 0

def _condition_text_from_payload(payload: dict[str, Any]) -> str:
    """
    조회조건 검증용 문자열.
    분석/KPI는 condition/query_summary/summary_md/message/params 중
    어디에 조건이 들어가도 검증할 수 있게 한곳에 모은다.
    """
    if not isinstance(payload, dict):
        return ""

    meta = payload.get("meta") or {}
    params = payload.get("params") or {}

    parts = [
        meta.get("condition"),
        meta.get("query_summary"),
        meta.get("summary_md"),
        payload.get("message"),
        payload.get("data") if isinstance(payload.get("data"), str) else "",
        str(params or ""),
    ]

    text = " ".join(str(x or "") for x in parts)
    text = text.replace("\n", " ").replace("\r", " ")
    text = " ".join(text.split())

    # params에 20250101 형식으로 들어온 날짜도
    # expected_condition_tokens=("2025-01-01", ...)와 비교 가능하게 한다.
    text = _append_date_variants(text)

    return text

def _missing_condition_tokens(payload: dict[str, Any], tokens: tuple[str, ...]) -> list[str]:
    if not tokens:
        return []

    text = _condition_text_from_payload(payload)
    return [str(token) for token in tokens if str(token) not in text]


def _short_text(value: Any, limit: int = 140) -> str:
    text = str(value or "").replace("\n", " ").replace("\r", " ")
    text = " ".join(text.split())
    if len(text) > limit:
        return text[:limit] + "..."
    return text

def _append_date_variants(text: str) -> str:
    """
    condition_text 안의 YYYYMMDD / YYYYMM 값을
    YYYY-MM-DD / YYYY-MM 형태로도 같이 검사할 수 있게 보강한다.

    예:
    - 20250101 → 2025-01-01
    - 20251231 → 2025-12-31
    - 202501   → 2025-01
    """
    import re

    src = str(text or "")
    variants: list[str] = []

    for m in re.findall(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", src):
        y, mo, d = m
        variants.append(f"{y}-{mo}-{d}")

    for m in re.findall(r"(?<!\d)(20\d{2})(\d{2})(?!\d)", src):
        y, mo = m
        variants.append(f"{y}-{mo}")

    if variants:
        src += " " + " ".join(dict.fromkeys(variants))

    return src

# ---------------------------------------------------------------------
# Basic checks
# ---------------------------------------------------------------------
def run_basic_checks() -> list[CheckResult]:
    results: list[CheckResult] = []

    module_name = "app.services.analytics_sales_trend_service"
    required_functions = [
        "get_sales_trend_result",
        "get_sales_trend_summary_result",
        "get_sales_forecast_result",
        "get_stock_shortage_result",
    ]

    try:
        mod = importlib.import_module(module_name)
        results.append(_ok(f"import {module_name}"))
    except Exception as e:
        return [_fail(f"import {module_name}", f"{type(e).__name__}: {e}")]

    for fn_name in required_functions:
        fn = getattr(mod, fn_name, None)
        if callable(fn):
            results.append(_ok(f"{module_name}.{fn_name}"))
        else:
            results.append(_fail(f"{module_name}.{fn_name}", "callable 함수 없음"))

    # NLQ router 쪽 분석 action 해석 함수 확인
    try:
        router = importlib.import_module("app.sims.nlq.nlq_router")
        resolve = getattr(router, "_resolve_analytics_action", None)
        if callable(resolve):
            tests = [
                ("품목별 매출 추세 2025년 조회", "품목별 매출 추세 분석"),
                ("품목별 매출 추세 요약표 2025년 조회", "품목별 매출 추세 요약표"),
                ("품목별 매출 예상 2025년 조회", "품목별 매출 예상"),
                ("매출처별 매출 예상 2025년 조회", "매출처별 매출 예상"),
                ("영업사원별 매출 예상 2025년 조회", "영업사원별 매출 예상"),
                ("지역별 매출 예상 2025년 조회", "지역별 매출 예상"),
                ("품목별 재고부족현황 2025년 조회", "품목별 재고부족현황"),
                ("매입처별 재고부족 현황 2025년 조회", "매입처별 재고부족 현황"),
            ]
            for q, expected in tests:
                got = resolve(q)
                if got == expected:
                    results.append(_ok(f"analytics action resolver: {q}", got))
                else:
                    results.append(_fail(f"analytics action resolver: {q}", f"expected={expected!r}, got={got!r}"))
        else:
            results.append(_fail("analytics action resolver", "_resolve_analytics_action 없음"))
    except Exception as e:
        results.append(_fail("analytics action resolver", f"{type(e).__name__}: {e}"))

    try:
        old_current_yyyymm = getattr(mod, "_current_yyyymm", None)
        setattr(mod, "_current_yyyymm", lambda: "202607")

        def _row(month: str, amt: int, qty: int = 1, product_code: str = "0001", buy_cd: str = "B1") -> dict[str, Any]:
            return {"기준월": month, "제품코드": product_code, "제품명": "테스트", "규격": "EA", "제조사코드": "M1", "제조사명": "제조사", "제품그룹명": "G", "제품구분명": "D", "제품분류명": "C", "매입처코드": buy_cd, "출고수량": qty, "출고할증수량": 0, "매출공급가액": amt, "매출세액": 0, "매출합계": amt, "집계건수": 1}

        raw_df = pd.DataFrame(
            [
                _row("202601", 100),
                _row("202602", 100),
                _row("202603", 100),
                _row("202604", 200),
                _row("202605", 200),
                _row("202606", 200),
                _row("202607", 300),
                _row("202608", 999),
            ]
        )
        summary_df = mod.get_sales_trend_summary_df(
            {
                "month_from": "202601",
                "month_to": "202608",
                "date_from": "20260101",
                "date_to": "20260831",
                "source_mode": "monthly_book",
            },
            raw_df=raw_df,
        )
        row = summary_df.iloc[0].to_dict()

        expected = {
            "완료월수": 6,
            "완료월총매출": 900,
            "완료월평균매출": 150,
            "월평균매출": 150,
            "최근3개월평균매출": 200,
            "최근6개월평균매출": 150,
            "최근3개월증감률": 33.3333333333,
            "당월 현재매출": 300,
            "당월 예상매출": 230,
            "당월 잔여예상": 0,
            "당월 진척률": 130.4347826087,
        }
        mismatches = []
        for key, exp in expected.items():
            got = float(row.get(key) or 0)
            if abs(got - float(exp)) > 1e-6:
                mismatches.append(f"{key}: expected={exp}, got={got}")

        if mismatches:
            results.append(_fail("sales period policy current/future exclusion", "; ".join(mismatches)))
        else:
            meta = mod._forecast_meta_from_df(summary_df)
            period_meta = mod._period_policy_meta_from_summary_df(summary_df)
            if int(meta.get("month_count") or 0) != 7 or int(period_meta.get("completed_month_count") or 0) != 6:
                results.append(_fail("sales period policy month counters", f"month_count={meta.get('month_count')}, completed={period_meta.get('completed_month_count')}"))
            elif str(row.get("추세판정") or "") != "증가":
                results.append(_fail("sales period policy trend judge", f"expected='증가', got={row.get('추세판정')!r}"))
            else:
                results.append(_ok("sales period policy current/future exclusion", "current=202607 and future=202608 excluded; forecast rate applied"))

        trend_df = mod._add_trend_columns(raw_df)
        period_values = dict(zip(trend_df["기준월"], trend_df["기간구분"]))
        rolling_values = dict(zip(trend_df["기준월"], trend_df["최근3개월평균매출"]))
        rolling_ok = (
            abs(float(rolling_values.get("202604") or 0) - 100) < 1e-6
            and abs(float(rolling_values.get("202607") or 0) - 200) < 1e-6
            and len(set(round(float(v), 6) for v in rolling_values.values())) > 1
        )
        period_ok = (
            period_values.get("202606") == "완료월"
            and period_values.get("202607") == "당월진행"
            and period_values.get("202608") == "미래월"
        )
        if rolling_ok and period_ok:
            results.append(_ok("sales trend detail period labels and rolling averages", "period labels set; row-wise rolling averages preserved"))
        else:
            results.append(_fail("sales trend detail period labels and rolling averages", f"periods={period_values}, rolling={rolling_values}"))

        duplicate_vendor_df = pd.DataFrame(
            [
                _row("202601", 200_000_000, 500, "00439", "B1"),
                _row("202601", 270_721_841, 500, "00439", "B2"),
                _row("202602", 100_000_000, 300, "00439", "B1"),
                _row("202602", 210_956_793, 458, "00439", "B2"),
                _row("202603", 284_213_860, 750, "00439", "B1"),
                _row("202604", 402_633_120, 770, "00439", "B1"),
                _row("202605", 420_000_000, 780, "00439", "B1"),
                _row("202606", 449_938_280, 790, "00439", "B1"),
                _row("202607", 200_886_348, 800, "00439", "B1"),
            ]
        )
        duplicate_trend = mod._add_trend_columns(duplicate_vendor_df)
        feb_rows = duplicate_trend[duplicate_trend["기준월"] == "202602"]
        jun_row = duplicate_trend[duplicate_trend["기준월"] == "202606"].iloc[0]
        jul_row = duplicate_trend[duplicate_trend["기준월"] == "202607"].iloc[0]
        duplicate_checks = [
            ("2026-02 전월대비매출", set(feb_rows["전월대비매출"].round(2).tolist()), {-159_765_048.0}),
            ("2026-02 전월대비수량", set(feb_rows["전월대비수량"].round(2).tolist()), {-242.0}),
            ("2026-02 최근3개월평균매출", set(feb_rows["최근3개월평균매출"].round(2).tolist()), {470_721_841.0}),
        ]
        duplicate_mismatches = []
        for label, got, exp in duplicate_checks:
            if got != exp:
                duplicate_mismatches.append(f"{label}: expected={exp}, got={got}")
        expected_points = {
            "2026-06 최근3개월평균매출": (jun_row.get("최근3개월평균매출"), 368_948_993.33),
            "2026-06 최근6개월평균매출": (jun_row.get("최근6개월평균매출"), 377_705_122.8),
            "2026-07 최근3개월평균매출": (jul_row.get("최근3개월평균매출"), 424_190_466.67),
            "2026-07 최근6개월평균매출": (jul_row.get("최근6개월평균매출"), 389_743_982.33),
        }
        for label, (got, exp) in expected_points.items():
            if abs(float(got or 0) - exp) > 0.01:
                duplicate_mismatches.append(f"{label}: expected={exp}, got={got}")
        if duplicate_mismatches:
            results.append(_fail("sales trend detail monthly aggregate metrics", "; ".join(duplicate_mismatches)))
        else:
            results.append(_ok("sales trend detail monthly aggregate metrics", "duplicate vendor rows share product-month metrics"))

        month_point_mismatches = []
        required_month_cols = ["월시점 증감률", "월시점 추세판정", "월시점 판정결과", "추세판정", "판정결과"]
        for c in required_month_cols:
            if c not in duplicate_trend.columns:
                month_point_mismatches.append(f"missing column {c}")
        if not month_point_mismatches:
            feb_judges = set(feb_rows["월시점 추세판정"].astype(str).tolist())
            feb_compat_judges = set(feb_rows["추세판정"].astype(str).tolist())
            if len(feb_judges) != 1 or feb_judges != feb_compat_judges:
                month_point_mismatches.append(f"202602 duplicate rows judge mismatch: {feb_judges}, compat={feb_compat_judges}")
            feb_expected_values = set(feb_rows["월시점 예상매출"].round(2).tolist())
            if len(feb_expected_values) != 1:
                month_point_mismatches.append(f"202602 duplicate rows expected sales mismatch: {feb_expected_values}")
            if str(jun_row.get("월시점 추세판정") or "") != "안정":
                month_point_mismatches.append(f"202606 expected 안정, got={jun_row.get('월시점 추세판정')!r}")
            if str(jul_row.get("월시점 추세판정") or "") != "안정":
                month_point_mismatches.append(f"202607 expected 안정, got={jul_row.get('월시점 추세판정')!r}")
            if str(jul_row.get("기간구분") or "") != "당월진행":
                month_point_mismatches.append(f"202607 period expected 당월진행, got={jul_row.get('기간구분')!r}")
            judge_seq = duplicate_trend.drop_duplicates(["제품코드", "기준월"])["월시점 추세판정"].astype(str).tolist()
            if len(set(judge_seq)) <= 1:
                month_point_mismatches.append(f"monthly judges appear copied: {judge_seq}")
            suffix_cols = [c for c in duplicate_trend.columns if str(c).endswith(("_x", "_y"))]
            if suffix_cols:
                month_point_mismatches.append(f"unexpected suffix columns: {suffix_cols}")
        if month_point_mismatches:
            results.append(_fail("sales trend detail month-point judge", "; ".join(month_point_mismatches)))
        else:
            results.append(_ok("sales trend detail month-point judge", "monthly point-in-time judges are merged per product-month"))

        product_00439_summary = mod.get_sales_trend_summary_df(
            {
                "month_from": "202601",
                "month_to": "202607",
                "date_from": "20260101",
                "date_to": "20260731",
                "source_mode": "monthly_book",
            },
            raw_df=duplicate_vendor_df,
        )
        row_00439 = product_00439_summary.iloc[0].to_dict()
        meta_00439 = mod._period_policy_meta_from_summary_df(product_00439_summary)
        display_mismatches = []
        expected_00439 = {
            "완료월평균매출": 389_743_982,
            "당월 현재매출": 200_886_348,
            "당월 예상매출": 442_935_939,
            "당월 잔여예상": 242_049_591,
            "당월 진척률": 45.3522419545,
        }
        for key, exp in expected_00439.items():
            got = float(row_00439.get(key) or 0)
            if abs(got - exp) > 0.02:
                display_mismatches.append(f"{key}: expected={exp}, got={got}")
        progress_value = row_00439.get("당월 진척률")
        if int(float(progress_value or 0)) == float(progress_value or 0):
            display_mismatches.append(f"당월 진척률 lost decimal precision: {progress_value}")
        if f"{float(progress_value or 0):.2f}%" != "45.35%":
            display_mismatches.append(f"당월 진척률 display expected 45.35%, got={float(progress_value or 0):.2f}%")
        if any(c.startswith("_당월") for c in product_00439_summary.columns):
            display_mismatches.append("internal _당월 columns exposed in summary")
        if round(float(meta_00439.get("avg_completed_month_sales_amt") or 0)) != 389_743_982:
            display_mismatches.append(f"meta avg_completed_month_sales_amt={meta_00439.get('avg_completed_month_sales_amt')}")
        trend_jul_expected = float(jul_row.get("월시점 예상매출") or 0)
        trend_jul_actual = float(jul_row.get("월시점 실제매출") or 0)
        trend_jul_progress = float(jul_row.get("월시점 달성률") or 0)
        if abs(round(trend_jul_expected) - round(float(row_00439.get("당월 예상매출") or 0))) > 0:
            display_mismatches.append(f"trend/summary expected mismatch: trend={trend_jul_expected}, summary={row_00439.get('당월 예상매출')}")
        if abs(trend_jul_actual - float(row_00439.get("당월 현재매출") or 0)) > 0.02:
            display_mismatches.append(f"trend/summary actual mismatch: trend={trend_jul_actual}, summary={row_00439.get('당월 현재매출')}")
        if abs(trend_jul_progress - float(row_00439.get("당월 진척률") or 0)) > 1e-6:
            display_mismatches.append(f"trend/summary progress mismatch: trend={trend_jul_progress}, summary={row_00439.get('당월 진척률')}")
        old_summary_for_forecast = getattr(mod, "get_sales_trend_summary_df", None)
        try:
            setattr(mod, "get_sales_trend_summary_df", lambda params=None, raw_df=None: product_00439_summary.copy())
            forecast_00439 = mod.get_sales_forecast_df({})
            forecast_row_00439 = forecast_00439.iloc[0].to_dict()
            if abs(float(forecast_row_00439.get("당월 예상매출") or 0) - float(row_00439.get("당월 예상매출") or 0)) > 0.02:
                display_mismatches.append(f"summary/forecast expected mismatch: forecast={forecast_row_00439.get('당월 예상매출')}, summary={row_00439.get('당월 예상매출')}")
            if abs(float(forecast_row_00439.get("당월 현재매출") or 0) - float(row_00439.get("당월 현재매출") or 0)) > 0.02:
                display_mismatches.append(f"summary/forecast actual mismatch: forecast={forecast_row_00439.get('당월 현재매출')}, summary={row_00439.get('당월 현재매출')}")
            if abs(float(forecast_row_00439.get("당월 진척률") or 0) - float(row_00439.get("당월 진척률") or 0)) > 1e-6:
                display_mismatches.append(f"summary/forecast progress mismatch: forecast={forecast_row_00439.get('당월 진척률')}, summary={row_00439.get('당월 진척률')}")
        finally:
            if old_summary_for_forecast is not None:
                setattr(mod, "get_sales_trend_summary_df", old_summary_for_forecast)
        if display_mismatches:
            results.append(_fail("sales forecast current month display summary", "; ".join(display_mismatches)))
        else:
            results.append(_ok("sales forecast current month display summary", "00439 current-month display values and trend/summary/forecast match expected"))

        past_df = pd.DataFrame([
            _row("202601", 100),
            _row("202602", 100),
            _row("202603", 100),
            _row("202604", 200),
            _row("202605", 200),
            _row("202606", 300),
        ])
        past_summary = mod.get_sales_trend_summary_df(
            {"month_from": "202601", "month_to": "202606", "date_from": "20260101", "date_to": "20260630", "source_mode": "monthly_book"},
            raw_df=past_df,
        )
        past_row = past_summary.iloc[0].to_dict()
        past_df_changed = pd.DataFrame([
            _row("202601", 100),
            _row("202602", 100),
            _row("202603", 100),
            _row("202604", 200),
            _row("202605", 200),
            _row("202606", 999),
        ])
        past_summary_changed = mod.get_sales_trend_summary_df(
            {"month_from": "202601", "month_to": "202606", "date_from": "20260101", "date_to": "20260630", "source_mode": "monthly_book"},
            raw_df=past_df_changed,
        )
        past_row_changed = past_summary_changed.iloc[0].to_dict()
        past_expected = float(past_row.get("당월 예상매출") or 0)
        past_expected_changed = float(past_row_changed.get("당월 예상매출") or 0)
        past_progress = float(past_row.get("당월 진척률") or 0)
        if (
            float(past_row.get("완료월수") or 0) == 5
            and float(past_row.get("당월 현재매출") or 0) == 300
            and past_expected > 0
            and past_progress > 0
            and abs(past_expected - past_expected_changed) < 1e-9
        ):
            results.append(_ok("sales period policy past month-end evaluation", "past month-end keeps end month as evaluation and forecast is basis-month only"))
        else:
            results.append(_fail("sales period policy past month-end evaluation", f"row={past_row}, changed={past_row_changed}"))

        source_policy_cases = [
            ("date_to == today", {"month_from": "202601", "date_to": "20260712", "policy_date": "20260712"}, False, "20260712", "current_monthly", "202607", ["202601", "202602", "202603", "202604", "202605", "202606"]),
            ("date_to > today", {"month_from": "202601", "date_to": "20260831", "policy_date": "20260712"}, False, "20260712", "current_monthly", "202607", ["202601", "202602", "202603", "202604", "202605", "202606"]),
            ("past month end", {"month_from": "202601", "date_to": "20260630", "policy_date": "20260712"}, False, "20260630", "historical_month_end", "202606", ["202601", "202602", "202603", "202604", "202605"]),
            ("past mid month", {"month_from": "202601", "date_to": "20260702", "policy_date": "20260712"}, True, "20260702", "historical_midmonth", "202607", ["202601", "202602", "202603", "202604", "202605", "202606"]),
        ]
        source_policy_mismatches = []
        for label, params_case, expected_hybrid, expected_effective, expected_mode, expected_eval_month, expected_basis in source_policy_cases:
            policy = mod._resolve_period_source_policy(params_case)
            if bool(policy.get("use_hybrid")) != expected_hybrid:
                source_policy_mismatches.append(f"{label}: hybrid expected={expected_hybrid}, got={policy.get('use_hybrid')}")
            if bool(policy.get("use_hybrid_detail")) != expected_hybrid:
                source_policy_mismatches.append(f"{label}: hybrid_detail expected={expected_hybrid}, got={policy.get('use_hybrid_detail')}")
            if str(policy.get("effective_date_to") or "") != expected_effective:
                source_policy_mismatches.append(f"{label}: effective expected={expected_effective}, got={policy.get('effective_date_to')}")
            if str(policy.get("evaluation_mode") or "") != expected_mode:
                source_policy_mismatches.append(f"{label}: mode expected={expected_mode}, got={policy.get('evaluation_mode')}")
            if str(policy.get("evaluation_month") or "") != expected_eval_month:
                source_policy_mismatches.append(f"{label}: eval_month expected={expected_eval_month}, got={policy.get('evaluation_month')}")
            if list(policy.get("basis_months") or []) != expected_basis:
                source_policy_mismatches.append(f"{label}: basis expected={expected_basis}, got={policy.get('basis_months')}")
        if source_policy_mismatches:
            results.append(_fail("sales source date policy resolver", "; ".join(source_policy_mismatches)))
        else:
            results.append(_ok("sales source date policy resolver", "today/future/month-end/mid-month branches verified"))

        old_monthly_source = getattr(mod, "get_sales_trend_monthly_df", None)
        old_detail_source = getattr(mod, "get_sales_trend_detail_df", None)
        try:
            source_calls = {"monthly": 0, "detail": 0}
            monthly_params_seen: list[dict[str, Any]] = []
            monthly_source_df = pd.DataFrame(
                [
                    _row("202606", 600, 6, "MIX1", "B1"),
                    _row("202607", 900, 9, "MIX1", "B1"),
                ]
            )
            detail_source_df = pd.DataFrame([_row("202607", 70, 7, "MIX1", "B1")])
            detail_source_df["거래처코드"] = "C1"
            detail_source_df["거래처명"] = "거래처"
            detail_source_df["시도명"] = "서울"
            detail_source_df["시구군명"] = "강남구"
            detail_source_df["법정읍면동명"] = "역삼동"
            detail_source_df["출고건수"] = 1
            detail_source_df["거래처수"] = 1
            month_col = list(monthly_source_df.columns)[0]
            branch_columns: dict[str, list[str]] = {}

            def _fake_monthly_source(params: Optional[dict[str, Any]] = None, source_mode: str = "monthly_book") -> pd.DataFrame:
                source_calls["monthly"] += 1
                monthly_params_seen.append(dict(params or {}))
                return monthly_source_df.copy()

            def _fake_detail_source(params: Optional[dict[str, Any]] = None) -> pd.DataFrame:
                source_calls["detail"] += 1
                return detail_source_df.copy()

            setattr(mod, "get_sales_trend_monthly_df", _fake_monthly_source)
            setattr(mod, "get_sales_trend_detail_df", _fake_detail_source)

            for label, params_case in [
                ("today monthly-only", {"date_to": "20260712", "policy_date": "20260712"}),
                ("future monthly-only", {"date_to": "20260831", "policy_date": "20260712"}),
                ("past month-end monthly-only", {"date_to": "20260630", "policy_date": "20260712"}),
            ]:
                source_calls["detail"] = 0
                monthly_params_seen.clear()
                out_source = mod.get_sales_trend_df(
                    {
                        "month_from": "202606",
                        "month_to": "202608",
                        "source_mode": "monthly_book",
                        **params_case,
                    }
                )
                if source_calls["detail"] != 0:
                    source_policy_mismatches.append(f"{label}: detail calls expected=0, got={source_calls['detail']}")
                if label == "future monthly-only" and str((monthly_params_seen[-1] or {}).get("date_to") or "") != "20260712":
                    source_policy_mismatches.append(f"{label}: monthly date_to not capped, params={monthly_params_seen[-1]}")
                if out_source.empty:
                    source_policy_mismatches.append(f"{label}: empty source result")
                suffix_cols = [c for c in out_source.columns if str(c).endswith(("_x", "_y"))]
                if suffix_cols:
                    source_policy_mismatches.append(f"{label}: unexpected suffix columns={suffix_cols}")
                branch_columns[label] = list(out_source.columns)

            source_calls["detail"] = 0
            out_hybrid = mod.get_sales_trend_df(
                {
                    "month_from": "202606",
                    "month_to": "202607",
                    "date_to": "20260702",
                    "policy_date": "20260712",
                    "source_mode": "monthly_book",
                }
            )
            hybrid_months = out_hybrid[month_col].astype(str).tolist() if month_col in out_hybrid.columns else []
            if source_calls["detail"] != 1:
                source_policy_mismatches.append(f"past mid month hybrid: detail calls expected=1, got={source_calls['detail']}")
            if hybrid_months.count("202607") != 1:
                source_policy_mismatches.append(f"past mid month hybrid: expected one replaced 202607 row, months={hybrid_months}")
            if not bool(getattr(out_hybrid, "attrs", {}).get("mixed_current_month_detail")):
                source_policy_mismatches.append("past mid month hybrid: mixed attrs missing")
            hybrid_suffix_cols = [c for c in out_hybrid.columns if str(c).endswith(("_x", "_y"))]
            if hybrid_suffix_cols:
                source_policy_mismatches.append(f"past mid month hybrid: unexpected suffix columns={hybrid_suffix_cols}")
            branch_columns["past mid month hybrid"] = list(out_hybrid.columns)
            internal_cols = {"거래처코드", "거래처명", "시도명", "시구군명", "법정읍면동명", "출고건수", "거래처수", "평균공급단가"}
            leaked_cols = sorted(c for c in out_hybrid.columns if c in internal_cols)
            if leaked_cols:
                source_policy_mismatches.append(f"past mid month hybrid: leaked internal columns={leaked_cols}")
            if len({tuple(cols) for cols in branch_columns.values()}) != 1:
                source_policy_mismatches.append(f"branch public columns mismatch={branch_columns}")

            current_raw = mod.get_sales_trend_df(
                {
                    "month_from": "202606",
                    "month_to": "202607",
                    "date_to": "20260712",
                    "policy_date": "20260712",
                    "source_mode": "monthly_book",
                }
            )
            current_summary = mod.get_sales_trend_summary_df(
                {
                    "month_from": "202606",
                    "month_to": "202607",
                    "date_to": "20260712",
                    "policy_date": "20260712",
                    "source_mode": "monthly_book",
                },
                raw_df=current_raw,
            )
            hybrid_summary = mod.get_sales_trend_summary_df(
                {
                    "month_from": "202606",
                    "month_to": "202607",
                    "date_to": "20260702",
                    "policy_date": "20260712",
                    "source_mode": "monthly_book",
                },
                raw_df=out_hybrid,
            )
            current_eval = current_summary.iloc[0].to_dict()
            hybrid_eval = hybrid_summary.iloc[0].to_dict()
            if abs(float(current_eval.get("당월 예상매출") or 0) - float(hybrid_eval.get("당월 예상매출") or 0)) > 1e-9:
                source_policy_mismatches.append(f"20260702/20260712 expected mismatch: current={current_eval}, hybrid={hybrid_eval}")
            if abs(float(current_eval.get("당월 현재매출") or 0) - float(hybrid_eval.get("당월 현재매출") or 0)) < 1e-9:
                source_policy_mismatches.append(f"20260702/20260712 actual should differ: current={current_eval}, hybrid={hybrid_eval}")
            if abs(float(current_eval.get("당월 진척률") or 0) - float(hybrid_eval.get("당월 진척률") or 0)) < 1e-9:
                source_policy_mismatches.append(f"20260702/20260712 progress should differ: current={current_eval}, hybrid={hybrid_eval}")

            if source_policy_mismatches:
                results.append(_fail("sales source monthly-only detail skip", "; ".join(source_policy_mismatches)))
            else:
                results.append(_ok("sales source monthly-only detail skip", "monthly-only skipped detail; mid-month hybrid replaced current month"))
        finally:
            if old_monthly_source is not None:
                setattr(mod, "get_sales_trend_monthly_df", old_monthly_source)
            if old_detail_source is not None:
                setattr(mod, "get_sales_trend_detail_df", old_detail_source)

        old_forecast_df = getattr(mod, "get_sales_forecast_df", None)
        old_stock_df = getattr(mod, "_load_product_current_stock", None)
        try:
            stock_base_df = pd.DataFrame(
                [
                    {
                        "제품코드": "STK1",
                        "제품명": "재고테스트1",
                        "규격": "EA",
                        "제조사명": "제조사",
                        "매입처명": "매입처",
                        "2026-01 수량": 10,
                        "2026-02 수량": 20,
                        "2026-03 수량": 30,
                        "2026-04 수량": 40,
                        "2026-05 수량": 50,
                        "2026-06 수량": 60,
                        "2026-07 수량": 25,
                        "2026-08 수량": 999,
                        "총출고수량": 1234,
                    },
                    {
                        "제품코드": "STK2",
                        "제품명": "재고테스트2",
                        "규격": "EA",
                        "제조사명": "제조사",
                        "매입처명": "매입처",
                        "2026-01 수량": 0,
                        "2026-02 수량": 0,
                        "2026-03 수량": 0,
                        "2026-04 수량": 0,
                        "2026-05 수량": 0,
                        "2026-06 수량": 0,
                        "2026-07 수량": 0,
                        "2026-08 수량": 999,
                        "총출고수량": 999,
                    },
                    {
                        "제품코드": "STK3",
                        "제품명": "재고테스트3",
                        "규격": "EA",
                        "제조사명": "제조사",
                        "매입처명": "매입처",
                        "2026-01 수량": 5,
                        "2026-02 수량": 5,
                        "2026-03 수량": 5,
                        "2026-04 수량": 5,
                        "2026-05 수량": 5,
                        "2026-06 수량": 5,
                        "2026-07 수량": 0,
                        "2026-08 수량": 999,
                        "총출고수량": 999,
                    },
                ]
            )
            stock_current_df = pd.DataFrame(
                [
                    {"제품코드": "STK1", "장부재고수량": 20, "실재고수량": 20, "장부재고금액": 20_000, "실재고금액": 40_000, "장부재고평가단가": 1000, "실재고평가단가": 2000, "당월입고수량": 1, "당월출고수량": 2, "당월재고증감수량": -1},
                    {"제품코드": "STK2", "장부재고수량": 10, "실재고수량": 10, "장부재고금액": 20_000, "실재고금액": 30_000, "장부재고평가단가": 2000, "실재고평가단가": 3000, "당월입고수량": 0, "당월출고수량": 0, "당월재고증감수량": 0},
                    {"제품코드": "STK3", "장부재고수량": -2, "실재고수량": -2, "장부재고금액": -200, "실재고금액": -400, "장부재고평가단가": 100, "실재고평가단가": 200, "당월입고수량": 0, "당월출고수량": 0, "당월재고증감수량": 0},
                ]
            )

            setattr(mod, "get_sales_forecast_df", lambda params: stock_base_df.copy())
            setattr(mod, "_load_product_current_stock", lambda *args, **kwargs: stock_current_df.copy())

            stock_result = mod.get_stock_shortage_df(
                {
                    "month_from": "202601",
                    "month_to": "202608",
                    "date_from": "20260101",
                    "date_to": "20260831",
                    "source_mode": "monthly_book",
                    "stock_mode": "book",
                }
            )
            stock_row1 = stock_result[stock_result["제품코드"].astype(str) == "STK1"].iloc[0].to_dict()
            stock_row2 = stock_result[stock_result["제품코드"].astype(str) == "STK2"].iloc[0].to_dict()
            stock_row3 = stock_result[stock_result["제품코드"].astype(str) == "STK3"].iloc[0].to_dict()
            meta_stock = mod._stock_shortage_meta_from_df(stock_result)

            stock_mismatches = []
            expected_stock_1 = {
                "완료월수": 6,
                "완료월총출고수량": 210,
                "완료월평균출고수량": 35,
                "최근3개월평균출고수량": 50,
                "최근6개월평균출고수량": 35,
                "최근3개월수량증감률": 42.8571428571,
                "당월 현재출고수량": 25,
                "당월 예상출고수량": 57.5,
                "당월 잔여예상출고수량": 32.5,
                "당월 출고진척률": 43.4782608696,
                "예상월말재고수량": -12.5,
                "부족예상수량": 12.5,
                "부족예상금액": 12500,
                "당월 재고충족률": 61.5384615385,
            }
            for key, exp in expected_stock_1.items():
                got = float(stock_row1.get(key) or 0)
                if abs(got - exp) > 1e-6:
                    stock_mismatches.append(f"STK1 {key}: expected={exp}, got={got}")
            if float(stock_row1.get("월평균출고수량") or 0) != float(stock_row1.get("완료월평균출고수량") or 0):
                stock_mismatches.append("월평균출고수량 is not aligned to 완료월평균출고수량")
            if str(stock_row1.get("재고부족판정") or "") != "부족":
                stock_mismatches.append(f"STK1 재고부족판정 expected 부족, got={stock_row1.get('재고부족판정')!r}")
            if float(stock_row2.get("당월 재고충족률") or 0) != 100 or str(stock_row2.get("재고부족판정") or "") != "수요없음":
                stock_mismatches.append(f"STK2 expected no-demand fill=100, row={stock_row2}")
            if abs(float(stock_row3.get("부족예상수량") or 0) - 7) > 1e-6:
                stock_mismatches.append(f"STK3 negative stock shortage expected=7, got={stock_row3.get('부족예상수량')}")
            if abs(float(meta_stock.get("overall_stock_fill_rate") or 0) - (30 / 37.5 * 100)) > 1e-6:
                stock_mismatches.append(f"overall fill rate expected=80, got={meta_stock.get('overall_stock_fill_rate')}")
            if abs(float(meta_stock.get("current_month_demand_progress_pct") or 0) - (25 / 62.5 * 100)) > 1e-6:
                stock_mismatches.append(f"weighted demand progress expected=40, got={meta_stock.get('current_month_demand_progress_pct')}")
            if str(stock_result.get("분석자료원", pd.Series([""])).iloc[0]) != "월집계-장부재고(Rddbc220)":
                stock_mismatches.append(f"monthly source label mismatch: {stock_result.get('분석자료원')}")
            internal_cols = [c for c in ["당월입고수량", "당월출고수량", "당월재고증감수량"] if c in stock_result.columns]
            if internal_cols:
                stock_mismatches.append(f"stock shortage internal columns leaked: {internal_cols}")
            if any(str(c).endswith(("_x", "_y")) for c in stock_result.columns):
                stock_mismatches.append("stock shortage result has merge suffix columns")
            if "2026-08 수량" in stock_result.columns and float(stock_row1.get("완료월총출고수량") or 0) >= 999:
                stock_mismatches.append("future month quantity appears included in completed demand")

            stock_hybrid_result = mod.get_stock_shortage_df(
                {
                    "month_from": "202601",
                    "month_to": "202607",
                    "date_from": "20260101",
                    "date_to": "20260702",
                    "policy_date": "20260712",
                    "source_mode": "monthly_book",
                    "stock_mode": "book",
                }
            )
            hybrid_source = str(stock_hybrid_result.get("분석자료원", pd.Series([""])).iloc[0])
            hybrid_stock_source = str(stock_hybrid_result.get("현재고원천", pd.Series([""])).iloc[0])
            if "평가월: 출고상세(Rddbc120)" not in hybrid_source or "현재재고: 전월말+입출고상세" not in hybrid_source:
                stock_mismatches.append(f"hybrid source label mismatch: {hybrid_source}")
            if "입고상세(Rddbc110)" not in hybrid_stock_source or "출고상세(Rddbc120)" not in hybrid_stock_source:
                stock_mismatches.append(f"hybrid stock source label mismatch: {hybrid_stock_source}")
            leaked_hybrid = [
                c for c in ["당월입고수량", "당월출고수량", "당월재고증감수량"]
                if c in stock_hybrid_result.columns
            ]
            if leaked_hybrid:
                stock_mismatches.append(f"hybrid stock internal columns leaked: {leaked_hybrid}")

            stock_past_month_end = mod.get_stock_shortage_df(
                {
                    "month_from": "202601",
                    "month_to": "202606",
                    "date_from": "20260101",
                    "date_to": "20260630",
                    "policy_date": "20260712",
                    "source_mode": "monthly_book",
                    "stock_mode": "book",
                }
            )
            stock_base_df_changed = stock_base_df.copy()
            stock_base_df_changed.loc[stock_base_df_changed["제품코드"].astype(str) == "STK1", "2026-06 수량"] = 999
            setattr(mod, "get_sales_forecast_df", lambda params: stock_base_df_changed.copy())
            stock_past_month_end_changed = mod.get_stock_shortage_df(
                {
                    "month_from": "202601",
                    "month_to": "202606",
                    "date_from": "20260101",
                    "date_to": "20260630",
                    "policy_date": "20260712",
                    "source_mode": "monthly_book",
                    "stock_mode": "book",
                }
            )
            setattr(mod, "get_sales_forecast_df", lambda params: stock_base_df.copy())
            past_stock_row1 = stock_past_month_end[stock_past_month_end["제품코드"].astype(str) == "STK1"].iloc[0].to_dict()
            past_stock_row1_changed = stock_past_month_end_changed[stock_past_month_end_changed["제품코드"].astype(str) == "STK1"].iloc[0].to_dict()
            if abs(float(past_stock_row1.get("당월 예상출고수량") or 0) - 46) > 1e-6:
                stock_mismatches.append(f"past month-end expected demand expected=46, got={past_stock_row1.get('당월 예상출고수량')}")
            if abs(float(past_stock_row1.get("당월 예상출고수량") or 0) - float(past_stock_row1_changed.get("당월 예상출고수량") or 0)) > 1e-9:
                stock_mismatches.append(f"past month-end expected demand changed by actual month: before={past_stock_row1}, after={past_stock_row1_changed}")
            if float(past_stock_row1.get("당월 현재출고수량") or 0) != 60:
                stock_mismatches.append(f"past month-end actual demand expected=60, got={past_stock_row1.get('당월 현재출고수량')}")
            if float(past_stock_row1.get("당월 예상출고수량") or 0) <= 0:
                stock_mismatches.append("past month-end expected demand should not be zero")

            stock_real_result = mod.get_stock_shortage_df(
                {
                    "month_from": "202601",
                    "month_to": "202608",
                    "date_from": "20260101",
                    "date_to": "20260831",
                    "source_mode": "monthly_real",
                    "stock_mode": "real",
                }
            )
            stock_real_row1 = stock_real_result[stock_real_result["제품코드"].astype(str) == "STK1"].iloc[0].to_dict()
            if float(stock_real_row1.get("재고평가단가") or 0) != 2000:
                stock_mismatches.append(f"real stock unit price expected=2000, got={stock_real_row1.get('재고평가단가')}")
            if abs(float(stock_real_row1.get("부족예상금액") or 0) - 25000) > 1e-6:
                stock_mismatches.append(f"real stock shortage amount expected=25000, got={stock_real_row1.get('부족예상금액')}")

            if stock_mismatches:
                results.append(_fail("stock shortage current-month period policy", "; ".join(stock_mismatches)))
            else:
                results.append(_ok("stock shortage current-month period policy", "completed demand, current demand, shortage, fill rate, and unit price selection verified"))
        finally:
            if old_forecast_df is not None:
                setattr(mod, "get_sales_forecast_df", old_forecast_df)
            if old_stock_df is not None:
                setattr(mod, "_load_product_current_stock", old_stock_df)
    except Exception as e:
        results.append(_fail("sales period policy current/future exclusion", f"{type(e).__name__}: {e}"))
    finally:
        try:
            if old_current_yyyymm is not None:
                setattr(mod, "_current_yyyymm", old_current_yyyymm)
        except Exception:
            pass

    try:
        import app.services.analytics_manufacturer_sales_trend_service as manufacturer_mod

        def _manufacturer_raw(date_to: str = "20260712") -> pd.DataFrame:
            july_a = 50 if str(date_to or "") >= "20260712" else 20
            rows = []
            for m, a1, a2, b in [
                ("202601", 40, 60, 10),
                ("202602", 40, 60, 20),
                ("202603", 40, 60, 30),
                ("202604", 40, 60, 40),
                ("202605", 40, 60, 50),
                ("202606", 40, 60, 60),
                ("202607", 10, july_a - 10, 70),
            ]:
                rows.extend([
                    {"기준월": m, "제품코드": "P1", "제품명": "A1", "규격": "", "제조사명": " 제약A ", "매출공급가액": a1, "매출합계": a1},
                    {"기준월": m, "제품코드": "P2", "제품명": "A2", "규격": "", "제조사명": "제약A", "매출공급가액": a2, "매출합계": a2},
                    {"기준월": m, "제품코드": "P3", "제품명": "B1", "규격": "", "제조사명": None, "매출공급가액": b, "매출합계": b},
                ])
            for m, amt in [
                ("202601", 60_000_000),
                ("202602", 50_000_000),
                ("202603", 40_000_000),
                ("202604", 30_000_000),
                ("202605", 20_000_000),
                ("202606", 10_000_000),
                ("202607", 5_000_000),
            ]:
                rows.append({"기준월": m, "제품코드": "P5", "제품명": "D1", "규격": "", "제조사명": "감소D", "매출공급가액": amt, "매출합계": amt})
            rows.append({"기준월": "202607", "제품코드": "P4", "제품명": "C1", "규격": "", "제조사명": "신규C", "매출공급가액": 30, "매출합계": 30})
            df = pd.DataFrame(rows)
            df.attrs["mixed_current_month_detail"] = str(date_to or "") < "20260712" and str(date_to or "")[:6] == "202607"
            df.attrs["source_label_completed"] = "월집계-장부재고(Rddbc220)"
            df.attrs["source_label_current"] = "출고상세(Rddbc120)"
            return df

        old_loader = getattr(manufacturer_mod, "get_sales_trend_df", None)
        captured_manufacturer_params = []

        def _manufacturer_loader(params):
            captured_manufacturer_params.append(dict(params or {}))
            return _manufacturer_raw(str((params or {}).get("date_to") or "20260712"))

        setattr(manufacturer_mod, "get_sales_trend_df", _manufacturer_loader)
        try:
            params_current = {
                "month_from": "202601",
                "month_to": "202607",
                "date_from": "20260101",
                "date_to": "20260712",
                "policy_date": "20260712",
                "source_mode": "monthly_book",
            }
            params_mid = dict(params_current, date_to="20260702")
            params_past_end = dict(params_current, month_to="202606", date_to="20260630")
            detail = manufacturer_mod.get_manufacturer_sales_trend(params_current)
            detail_mid = manufacturer_mod.get_manufacturer_sales_trend(params_mid)
            detail_past = manufacturer_mod.get_manufacturer_sales_trend(params_past_end)
            mismatches = []
            if getattr(detail, "attrs", {}).get("evaluation_mode") != "current_monthly":
                mismatches.append(f"current mode expected current_monthly got={getattr(detail, 'attrs', {}).get('evaluation_mode')}")
            if getattr(detail_mid, "attrs", {}).get("evaluation_mode") != "historical_midmonth":
                mismatches.append(f"mid mode expected historical_midmonth got={getattr(detail_mid, 'attrs', {}).get('evaluation_mode')}")
            if getattr(detail_past, "attrs", {}).get("evaluation_mode") != "historical_month_end":
                mismatches.append(f"past month-end mode expected historical_month_end got={getattr(detail_past, 'attrs', {}).get('evaluation_mode')}")
            address_params = dict(params_current, sido_nm="서울", gugun_nm="강남", road_nm="테헤란로")
            _ = manufacturer_mod.get_manufacturer_sales_trend(address_params)
            last_params = captured_manufacturer_params[-1] if captured_manufacturer_params else {}
            address_mismatches = []
            for k, expected in {"sido_nm": "서울", "gugun_nm": "강남", "road_nm": "테헤란로"}.items():
                if last_params.get(k) != expected:
                    address_mismatches.append(f"manufacturer address param not forwarded {k}={last_params.get(k)!r}")
            query_condition = manufacturer_mod._fmt_analytics_query_summary(address_params, "월집계-장부재고(Rddbc220)")
            for expected in ["시도명 서울", "시구군명 강남", "도로명 테헤란로"]:
                if expected not in query_condition:
                    address_mismatches.append(f"manufacturer address query condition missing {expected}: {query_condition}")
            if address_mismatches:
                results.append(_fail("manufacturer sales trend address filters", "; ".join(address_mismatches)))
            else:
                results.append(_ok("manufacturer sales trend address filters", "sido/gugun/road params and query summary verified"))

            if "제약사명" not in detail.columns:
                mismatches.append("missing 제약사명")
            forbidden_tokens = ["수량", "다음월예상매출", "예상등급"]
            forbidden = [
                c for c in detail.columns
                if any(x in str(c) for x in forbidden_tokens)
                or str(c).endswith(("_x", "_y"))
                or str(c).startswith("_")
                or c in {"제품코드", "제품명", "규격"}
            ]
            if forbidden:
                mismatches.append(f"forbidden public columns={forbidden}")
            required_detail_cols = [
                "기준월",
                "매출공급가액",
                "최근3개월평균매출",
                "월시점 실제매출",
                "월시점 예상매출",
                "월시점 달성률",
                "월시점 예상기준",
                "월시점 적용증감률",
                "월시점 예상대비차이",
                "월시점 잔여예상",
                "월시점 추세판정",
                "월시점 판정결과",
                "판정결과",
                "추세판정",
            ]
            for c in required_detail_cols:
                if c not in detail.columns:
                    mismatches.append(f"missing detail column {c}")
            detail_analysis_block = [
                "월시점 완료월수",
                "월시점 완료월평균매출",
                "월시점 최근3개월평균매출",
                "월시점 최근6개월평균매출",
                "월시점 증감률",
                "월시점 추세판정",
                "월시점 판정결과",
                "월시점 실제매출",
                "월시점 예상기준",
                "월시점 적용증감률",
                "월시점 예상매출",
                "월시점 예상대비차이",
                "월시점 잔여예상",
                "월시점 달성률",
                "추세판정",
                "판정결과",
            ]
            detail_cols = list(detail.columns)
            block_positions = [detail_cols.index(c) for c in detail_analysis_block if c in detail_cols]
            if block_positions != sorted(block_positions) or len(block_positions) != len(detail_analysis_block):
                mismatches.append("detail analysis block order mismatch")
            non_zero_metric_sum = float(
                detail[[c for c in ["월시점 실제매출", "월시점 예상매출", "월시점 달성률"] if c in detail.columns]]
                .apply(pd.to_numeric, errors="coerce")
                .fillna(0)
                .abs()
                .sum()
                .sum()
            )
            if non_zero_metric_sum <= 0:
                mismatches.append("detail month-point actual/expected/progress are all zero")
            zero_check_cols = [
                "매출공급가액",
                "매출세액",
                "매출합계",
                "집계건수",
                "월시점 실제매출",
                "월시점 예상매출",
                "월시점 예상대비차이",
                "월시점 잔여예상",
            ]
            present_zero_cols = [c for c in zero_check_cols if c in detail.columns]
            zero_rows = (
                detail[present_zero_cols].apply(pd.to_numeric, errors="coerce").fillna(0).abs().sum(axis=1).eq(0).sum()
                if present_zero_cols
                else 0
            )
            if int(zero_rows) != 0:
                mismatches.append(f"detail public zero rows should be removed rows={zero_rows}")
            detail_key_counts = detail.groupby(["제약사명", "기준월"]).size()
            if not detail_key_counts.empty and int(detail_key_counts.max()) != 1:
                mismatches.append("detail should have one row per manufacturer-month")

            raw_cur = _manufacturer_raw("20260712")
            raw_sum = float(pd.to_numeric(raw_cur["매출공급가액"], errors="coerce").fillna(0).sum())
            detail_sum = float(pd.to_numeric(detail["매출공급가액"], errors="coerce").fillna(0).sum())
            if abs(raw_sum - detail_sum) > 1e-9:
                mismatches.append(f"detail monthly sum mismatch raw={raw_sum}, detail={detail_sum}")
            a_july = detail[(detail["제약사명"].astype(str) == "제약A") & (detail["기준월"].astype(str) == "202607")].iloc[0]
            a_mid_july = detail_mid[(detail_mid["제약사명"].astype(str) == "제약A") & (detail_mid["기준월"].astype(str) == "202607")].iloc[0]
            if float(a_july["매출공급가액"]) != 50:
                mismatches.append(f"manufacturer detail should aggregate manufacturer-month sales expected=50 got={a_july['매출공급가액']}")
            if float(a_mid_july["매출공급가액"]) == float(a_july["매출공급가액"]):
                mismatches.append("midmonth/current manufacturer actual sales should differ")
            sorted_check = detail.sort_values(["제약사명", "기준월"], ascending=[True, True]).reset_index(drop=True)
            if list(detail[["제약사명", "기준월"]].itertuples(index=False, name=None)) != list(sorted_check[["제약사명", "기준월"]].itertuples(index=False, name=None)):
                mismatches.append("detail final sort should be manufacturer asc + month asc")
            expected_seq = list(range(1, len(detail) + 1))
            if "순번" not in detail.columns or list(pd.to_numeric(detail["순번"], errors="coerce").fillna(0).astype(int)) != expected_seq:
                mismatches.append("detail sequence should be reassigned after final sort")
            if pd.isna(a_july.get("전월대비매출")):
                mismatches.append("detail last month diff sales should use same formula, not NaN")
            if pd.isna(a_july.get("전월대비매출증감률")):
                mismatches.append("detail last month diff pct should use same formula, not NaN")
            bad_period_values = {"당월진행", "current_monthly", "historical_midmonth", "historical_month_end"}
            leaked_period = sorted(set(detail.get("기간구분", pd.Series(dtype=str)).astype(str)) & bad_period_values)
            if leaked_period:
                mismatches.append(f"detail public period leaked forbidden values={leaked_period}")
            mid_period_values = set(detail_mid.get("기간구분", pd.Series(dtype=str)).astype(str))
            if "부분월" not in mid_period_values:
                mismatches.append(f"midmonth detail should expose 부분월 for final month values={sorted(mid_period_values)}")
            blank_july = detail[(detail["제약사명"].astype(str) == "제약사 미지정") & (detail["기준월"].astype(str) == "202607")].iloc[0]
            if float(blank_july["매출공급가액"]) != 70:
                mismatches.append("blank manufacturer group not preserved in detail")
            new_july = detail[(detail["제약사명"].astype(str) == "신규C") & (detail["기준월"].astype(str) == "202607")].iloc[0]
            if str(new_july.get("추세판정") or "") != "자료부족":
                mismatches.append(f"new manufacturer judge expected 자료부족 got={new_july.get('추세판정')}")

            if mismatches:
                results.append(_fail("manufacturer sales trend detail", "; ".join(mismatches)))
            else:
                results.append(_ok("manufacturer sales trend detail", "analysis block, zero-row removal, sort, and monthly sums verified"))

            summary = manufacturer_mod.get_manufacturer_sales_trend_summary(params_current)
            summary_mid = manufacturer_mod.get_manufacturer_sales_trend_summary(params_mid)
            summary_past = manufacturer_mod.get_manufacturer_sales_trend_summary(params_past_end)
            detail_res = manufacturer_mod.get_manufacturer_sales_trend_result(params_current)
            res = manufacturer_mod.get_manufacturer_sales_trend_summary_result(params_current)
            res_past = manufacturer_mod.get_manufacturer_sales_trend_summary_result(params_past_end)
            mismatches = []
            forbidden = [
                c for c in summary.columns
                if any(x in str(c) for x in forbidden_tokens)
                or str(c).endswith(("_x", "_y"))
                or str(c).startswith("_")
                or c in {"제품코드", "제품명", "규격"}
            ]
            if forbidden:
                mismatches.append(f"forbidden summary columns={forbidden}")
            if not any(str(c).endswith(" 매출") and str(c)[:4].isdigit() for c in summary.columns):
                mismatches.append("missing dynamic monthly sales columns")
            required_summary_cols = [
                "순번",
                "제약사명",
                "총매출공급가액",
                "총매출세액",
                "총매출액",
                "완료월총매출",
                "월평균매출",
                "완료월수",
                "완료월평균매출",
                "당월 현재매출",
                "당월 예상매출",
                "당월 잔여예상",
                "당월 진척률",
                "매출발생월수",
                "최근3개월평균매출",
                "최근6개월평균매출",
                "최근3개월증감률",
                "추세판정",
                "제품수",
                "매입처수",
                "총집계건수",
                "분석자료원",
                "기간구분",
            ]
            for c in required_summary_cols:
                if c not in summary.columns:
                    mismatches.append(f"missing summary column {c}")
            summary_cols = list(summary.columns)
            summary_positions = [summary_cols.index(c) for c in required_summary_cols if c in summary_cols]
            if summary_positions != sorted(summary_positions) or len(summary_positions) != len(required_summary_cols):
                mismatches.append("summary core column order mismatch")
            if "평가월 매출" in summary.columns:
                mismatches.append("current monthly summary should expose 당월 현재매출, not 평가월 매출")
            for label, df_check in [("mid", summary_mid), ("past", summary_past)]:
                for c in ["평가월 매출", "평가월 예상매출", "평가월 잔여예상", "평가월 진척률"]:
                    if c not in df_check.columns:
                        mismatches.append(f"{label} historical summary missing {c}")
                if "당월 현재매출" in df_check.columns:
                    mismatches.append(f"{label} historical summary should not expose 당월 현재매출")
                if any(c in df_check.columns for c in ["당월 예상매출", "당월 잔여예상", "당월 진척률"]):
                    mismatches.append(f"{label} historical summary should not expose 당월 expected/progress labels")
            if any(str(c).endswith(" 수량") for c in summary.columns):
                mismatches.append("summary should not expose monthly qty columns")
            summary_sum = float(pd.to_numeric(summary["총매출공급가액"], errors="coerce").fillna(0).sum()) if "총매출공급가액" in summary.columns else -1
            if abs(raw_sum - summary_sum) > 1e-9:
                mismatches.append(f"summary total mismatch raw={raw_sum}, summary={summary_sum}")
            names = set(summary["제약사명"].astype(str).tolist()) if "제약사명" in summary.columns else set()
            if not {"제약A", "제약사 미지정"}.issubset(names):
                mismatches.append(f"manufacturer universe not preserved names={sorted(names)}")
            meta = res.get("meta") or {}
            if meta.get("analysis_type") != "manufacturer_sales_trend_summary" or meta.get("summary_type") != "manufacturer_trend_summary":
                mismatches.append(f"unexpected summary meta={meta}")
            if meta.get("evaluation_mode") != "current_monthly":
                mismatches.append(f"summary meta evaluation_mode expected current_monthly got={meta.get('evaluation_mode')}")
            period_caption = str(meta.get("period_caption") or "")
            if "당월 2026-07" not in period_caption or "current_monthly" in period_caption:
                mismatches.append(f"current summary period caption unexpected={period_caption}")
            mid_caption = str(getattr(summary_mid, "attrs", {}).get("period_caption") or "")
            if "평가월 2026-07(07-02까지)" not in mid_caption or "당월" in mid_caption or "historical_midmonth" in mid_caption:
                mismatches.append(f"mid summary period caption unexpected={mid_caption}")
            past_caption = str(getattr(summary_past, "attrs", {}).get("period_caption") or "")
            if "완료월 2026-01~2026-05" not in past_caption or "평가월 2026-06" not in past_caption or "historical_month_end" in past_caption:
                mismatches.append(f"past summary period caption unexpected={past_caption}")
            if "current_monthly" in set(summary.get("기간구분", pd.Series(dtype=str)).astype(str)):
                mismatches.append("summary public period label leaked internal current_monthly")
            a_summary = summary[summary["제약사명"].astype(str) == "제약A"].iloc[0]
            a_summary_mid = summary_mid[summary_mid["제약사명"].astype(str) == "제약A"].iloc[0]
            for c in ["완료월총매출", "완료월수", "완료월평균매출", "최근3개월평균매출", "최근6개월평균매출", "최근3개월증감률", "추세판정"]:
                if str(c) == "추세판정":
                    if str(a_summary[c]) != str(a_summary_mid[c]):
                        mismatches.append(f"current/mid completed judge differs {a_summary[c]} vs {a_summary_mid[c]}")
                elif abs(float(a_summary[c]) - float(a_summary_mid[c])) > 1e-9:
                    mismatches.append(f"current/mid completed metric differs {c}: {a_summary[c]} vs {a_summary_mid[c]}")
            if float(a_summary["당월 현재매출"]) == float(a_summary_mid["평가월 매출"]):
                mismatches.append("current/mid current month sales should differ in summary")
            if float(a_summary.get("당월 예상매출", 0)) <= 0:
                mismatches.append("current summary expected sales should be populated")
            if float(a_summary.get("당월 진척률", 0)) <= 0:
                mismatches.append("current summary progress should be populated")
            a_past = summary_past[summary_past["제약사명"].astype(str) == "제약A"].iloc[0]
            if int(a_past["완료월수"]) != 5:
                mismatches.append(f"past month-end completed month count expected=5 got={a_past['완료월수']}")
            new_summary = summary[summary["제약사명"].astype(str) == "신규C"].iloc[0]
            if str(new_summary.get("추세판정") or "") != "비교자료 부족":
                mismatches.append(f"new manufacturer summary judge expected 비교자료 부족 got={new_summary.get('추세판정')}")
            for m in ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07"]:
                col = f"{m} 매출"
                if col in summary.columns:
                    detail_month = m.replace("-", "")
                    dsum = float(pd.to_numeric(detail[detail["기준월"].astype(str) == detail_month]["매출공급가액"], errors="coerce").fillna(0).sum())
                    ssum = float(pd.to_numeric(summary[col], errors="coerce").fillna(0).sum())
                    if abs(dsum - ssum) > 1e-9:
                        mismatches.append(f"detail/summary month sum mismatch {m}: detail={dsum}, summary={ssum}")
            if abs(float(pd.to_numeric(summary["총매출공급가액"], errors="coerce").fillna(0).sum()) - detail_sum) > 1e-9:
                mismatches.append("detail total and summary total differ under identical params")

            try:
                import os
                from app.ui.sims_table_display import resolve_sims_table_mode
                old_chat_env = os.environ.get("SIMS_CHAT_FAST_TABLE_CELL_THRESHOLD")
                old_panel_env = os.environ.get("SIMS_FAST_TABLE_CELL_THRESHOLD")
                os.environ["SIMS_CHAT_FAST_TABLE_CELL_THRESHOLD"] = "10"
                os.environ["SIMS_FAST_TABLE_CELL_THRESHOLD"] = "10"
                detail_chat_mode = resolve_sims_table_mode(detail, action="제약사별 매출 추세 분석", render_path="chat")
                detail_panel_mode = resolve_sims_table_mode(detail, action="제약사별 매출 추세 분석", render_path="panel")
                if detail_chat_mode.get("mode") != "fast" or detail_panel_mode.get("mode") != "fast":
                    mismatches.append(f"detail table mode fast expected chat={detail_chat_mode} panel={detail_panel_mode}")
                chat_mode = resolve_sims_table_mode(summary, action="제약사별 매출 추세 분석 요약표", render_path="chat")
                panel_mode = resolve_sims_table_mode(summary, action="제약사별 매출 추세 분석 요약표", render_path="panel")
                if chat_mode.get("mode") != "fast" or panel_mode.get("mode") != "fast":
                    mismatches.append(f"table mode fast expected chat={chat_mode} panel={panel_mode}")
                os.environ["SIMS_CHAT_FAST_TABLE_CELL_THRESHOLD"] = "999999"
                os.environ["SIMS_FAST_TABLE_CELL_THRESHOLD"] = "999999"
                chat_small = resolve_sims_table_mode(summary, action="제약사별 매출 추세 분석 요약표", render_path="chat")
                panel_small = resolve_sims_table_mode(summary, action="제약사별 매출 추세 분석 요약표", render_path="panel")
                if chat_small.get("mode") != "small" or panel_small.get("mode") != "small":
                    mismatches.append(f"table mode small expected chat={chat_small} panel={panel_small}")
            finally:
                if 'old_chat_env' in locals():
                    if old_chat_env is None:
                        os.environ.pop("SIMS_CHAT_FAST_TABLE_CELL_THRESHOLD", None)
                    else:
                        os.environ["SIMS_CHAT_FAST_TABLE_CELL_THRESHOLD"] = old_chat_env
                if 'old_panel_env' in locals():
                    if old_panel_env is None:
                        os.environ.pop("SIMS_FAST_TABLE_CELL_THRESHOLD", None)
                    else:
                        os.environ["SIMS_FAST_TABLE_CELL_THRESHOLD"] = old_panel_env

            def _manufacturer_bucket(value):
                text = str(value or "").strip()
                if text in {"증가", "신규/증가"}:
                    return "증가"
                if text == "감소":
                    return "감소"
                if text == "안정":
                    return "안정"
                return "자료부족"

            def _expected_counts(frame):
                counts = {"증가": 0, "감소": 0, "안정": 0, "자료부족": 0}
                if frame is None or frame.empty:
                    return counts
                if "기준월" in frame.columns:
                    work_counts = (
                        frame.assign(_기준월_sort=frame["기준월"].astype(str))
                        .sort_values(["제약사명", "_기준월_sort"])
                        .drop_duplicates("제약사명", keep="last")
                    )
                else:
                    work_counts = frame.drop_duplicates("제약사명", keep="last")
                for value in work_counts.get("추세판정", pd.Series(dtype=object)).tolist():
                    counts[_manufacturer_bucket(value)] += 1
                return counts

            header_mismatches = []
            detail_meta = detail_res.get("meta") or {}
            summary_meta = res.get("meta") or {}
            past_meta = res_past.get("meta") or {}
            expected_detail_counts = _expected_counts(detail)
            expected_summary_counts = _expected_counts(summary)
            for label, frame, meta_check, expected_counts in [
                ("detail", detail, detail_meta, expected_detail_counts),
                ("summary", summary, summary_meta, expected_summary_counts),
            ]:
                counts = meta_check.get("trend_judge_counts") or {}
                four_total = sum(int(counts.get(k, 0) or 0) for k in ["증가", "감소", "안정", "자료부족"])
                manufacturer_count = int(meta_check.get("manufacturer_count") or 0)
                if four_total != manufacturer_count:
                    header_mismatches.append(f"{label} judge count total {four_total} != manufacturer_count {manufacturer_count}")
                for k in ["증가", "감소", "안정", "자료부족"]:
                    if int(counts.get(k, 0) or 0) != int(expected_counts.get(k, 0) or 0):
                        header_mismatches.append(f"{label} judge {k} expected={expected_counts.get(k)} got={counts.get(k)}")
                if not isinstance(counts.get("자료부족", 0), int):
                    header_mismatches.append(f"{label} 자료부족 count should be integer")

            if detail_meta.get("current_progress_title") != "당월 진행 요약":
                header_mismatches.append(f"detail current progress title unexpected={detail_meta.get('current_progress_title')}")
            if summary_meta.get("current_progress_title") != "당월 진행 요약":
                header_mismatches.append(f"summary current progress title unexpected={summary_meta.get('current_progress_title')}")
            if past_meta.get("current_progress_title") != "평가월 진행 요약":
                header_mismatches.append(f"past progress title unexpected={past_meta.get('current_progress_title')}")
            for meta_label, meta_check in [("detail", detail_meta), ("summary", summary_meta), ("past", past_meta)]:
                for key in [
                    "completed_month_count",
                    "avg_completed_month_sales_amt",
                    "sum_current_month_sales_amt",
                    "sum_current_month_expected_amt",
                    "sum_current_month_remaining_expected_amt",
                    "current_month_progress_pct",
                ]:
                    if key not in meta_check:
                        header_mismatches.append(f"{meta_label} missing header meta {key}")

            eval_month = str(detail_meta.get("evaluation_month") or "")
            eval_rows = detail[detail["기준월"].astype(str) == eval_month] if eval_month and "기준월" in detail.columns else detail
            actual_total = float(pd.to_numeric(eval_rows.get("월시점 실제매출", 0), errors="coerce").fillna(0).sum())
            expected_total = float(pd.to_numeric(eval_rows.get("월시점 예상매출", 0), errors="coerce").fillna(0).sum())
            expected_progress = (actual_total / expected_total * 100) if abs(expected_total) >= 1e-12 else 0
            if abs(float(detail_meta.get("current_month_progress_pct") or 0) - expected_progress) > 1e-9:
                header_mismatches.append("detail progress should use summed actual / summed expected")
            summary_actual = float(pd.to_numeric(summary.get("당월 현재매출", 0), errors="coerce").fillna(0).sum())
            summary_expected = float(pd.to_numeric(summary.get("당월 예상매출", 0), errors="coerce").fillna(0).sum())
            summary_progress = (summary_actual / summary_expected * 100) if abs(summary_expected) >= 1e-12 else 0
            if abs(float(summary_meta.get("current_month_progress_pct") or 0) - summary_progress) > 1e-9:
                header_mismatches.append("summary progress should use summed actual / summed expected")

            if header_mismatches:
                results.append(_fail("manufacturer sales trend header summaries", "; ".join(header_mismatches)))
            else:
                results.append(_ok("manufacturer sales trend header summaries", "summary cards, progress totals, judge buckets, and table modes verified"))

            try:
                from app.ui.current_table_followups.action_dispatcher import handle_current_table_followup_by_action

                pushed_tables = []
                pushed_notices = []

                def _test_find_col(frame, *, exact=(), include_any=(), exclude_any=()):
                    cols = [str(c) for c in frame.columns]
                    for name in exact:
                        if name in cols:
                            return name
                    for col in cols:
                        if include_any and not any(w in col for w in include_any):
                            continue
                        if exclude_any and any(w in col for w in exclude_any):
                            continue
                        return col
                    return ""

                def _test_to_num(sr):
                    return pd.to_numeric(
                        sr.fillna("").astype(str).str.replace(",", "", regex=False).str.replace("%", "", regex=False),
                        errors="coerce",
                    ).fillna(0)

                def _test_push_table(**kwargs):
                    pushed_tables.append(kwargs)
                    return True

                def _test_push_notice(**kwargs):
                    pushed_notices.append(kwargs)
                    return True

                class _NoopLog:
                    def info(self, *args, **kwargs):
                        return None

                    def exception(self, *args, **kwargs):
                        return None

                helpers = {
                    "find_col": _test_find_col,
                    "to_num": _test_to_num,
                    "push_table": _test_push_table,
                    "push_notice": _test_push_notice,
                }

                followup_mismatches = []
                source_action = "제약사별 매출 추세 분석 요약표"
                source_key = "test_manufacturer_summary"

                pushed_tables.clear()
                pushed_notices.clear()
                handled = handle_current_table_followup_by_action(
                    df=summary,
                    query="현재표 추세판정 집계",
                    top_n=20,
                    table_key=source_key,
                    source_action=source_action,
                    helpers=helpers,
                    log=_NoopLog(),
                )
                if not handled or not pushed_tables:
                    followup_mismatches.append("trend judge group should return table")
                else:
                    group_payload = pushed_tables[-1]
                    group_df = group_payload.get("df")
                    extra_meta = group_payload.get("extra_meta") or {}
                    if extra_meta.get("group_column") != "추세판정":
                        followup_mismatches.append(f"group column expected 추세판정 got={extra_meta.get('group_column')}")
                    if not isinstance(group_df, pd.DataFrame) or group_df.empty:
                        followup_mismatches.append("trend judge group dataframe empty")
                    else:
                        if "제약사수" not in group_df.columns:
                            followup_mismatches.append("trend judge group missing 제약사수")
                        elif int(pd.to_numeric(group_df["제약사수"], errors="coerce").fillna(0).sum()) != int(summary["제약사명"].nunique()):
                            followup_mismatches.append("trend judge group manufacturer count sum mismatch")
                        if "총매출액" in group_df.columns:
                            grouped_total = float(pd.to_numeric(group_df["총매출액"], errors="coerce").fillna(0).sum())
                            original_total = float(pd.to_numeric(summary["총매출액"], errors="coerce").fillna(0).sum())
                            if abs(grouped_total - original_total) > 1e-9:
                                followup_mismatches.append("trend judge group total sales sum mismatch")
                        if "현재표 분석/KPI 후속분석 불가" in str(group_payload.get("title") or ""):
                            followup_mismatches.append("trend judge group fell through to analytics kpi unsupported notice")

                for query, expected_col in [
                    ("현재표 추세판정 감소만 보여줘", "추세판정"),
                    ("현재표 제약사명 제약A 상세", "제약사명"),
                    ("현재표 당월 진척률 100 이상", "당월 진척률"),
                    ("현재표 총매출액 1억 이상", "총매출액"),
                ]:
                    pushed_tables.clear()
                    pushed_notices.clear()
                    handled = handle_current_table_followup_by_action(
                        df=summary,
                        query=query,
                        top_n=20,
                        table_key=source_key,
                        source_action=source_action,
                        helpers=helpers,
                        log=_NoopLog(),
                    )
                    if not handled or not pushed_tables:
                        followup_mismatches.append(f"followup should return table query={query}")
                        continue
                    out_df = pushed_tables[-1].get("df")
                    meta_extra = pushed_tables[-1].get("extra_meta") or {}
                    if not isinstance(out_df, pd.DataFrame):
                        followup_mismatches.append(f"followup output not dataframe query={query}")
                        continue
                    if expected_col not in out_df.columns:
                        followup_mismatches.append(f"followup output missing original column {expected_col} query={query}")
                    if "순번" not in out_df.columns:
                        followup_mismatches.append(f"followup should regenerate sequence query={query}")
                    if expected_col == "추세판정" and not out_df["추세판정"].astype(str).str.contains("감소").all():
                        followup_mismatches.append("trend judge text filter contains non 감소 rows")
                    if expected_col == "제약사명" and not out_df["제약사명"].astype(str).str.contains("제약A").all():
                        followup_mismatches.append("manufacturer text filter contains non 제약A rows")
                    if expected_col in {"당월 진척률", "총매출액"} and meta_extra.get("filter_column") != expected_col:
                        followup_mismatches.append(f"numeric filter meta column mismatch query={query} meta={meta_extra}")

                if followup_mismatches:
                    results.append(_fail("current table generic group/filter followups", "; ".join(followup_mismatches)))
                else:
                    results.append(_ok("current table generic group/filter followups", "generic group, text filter, numeric filter, and analytics fallback blocking verified"))
            except Exception as e:
                results.append(_fail("current table generic group/filter followups", f"{type(e).__name__}: {e}"))

            if mismatches:
                results.append(_fail("manufacturer sales trend summary", "; ".join(mismatches)))
            else:
                results.append(_ok("manufacturer sales trend summary", "summary schema, monthly pivot, totals, universe, and meta verified"))
        finally:
            if old_loader is not None:
                setattr(manufacturer_mod, "get_sales_trend_df", old_loader)
    except Exception as e:
        results.append(_fail("manufacturer sales trend", f"{type(e).__name__}: {e}"))


    try:
        supp_mod = importlib.import_module("app.services.analytics_supplier_stock_shortage_service")
        product_base = pd.DataFrame([
            {
                "제품코드": "P001",
                "제품명": "테스트제품",
                "규격": "EA",
                "제조사명": "제조사A",
                "제품그룹명": "G",
                "제품구분명": "D",
                "제품분류명": "C",
                "재고기준": "장부",
                "현재재고수량": 100,
                "현재재고금액": 2000,
                "재고평가단가": 20,
                "당월 예상출고수량": 100,
                "당월 잔여예상출고수량": 150,
                "당월 출고진척률": 50,
                "부족예상수량": 50,
                "부족예상금액": 1000,
                "1개월부족수량": 10,
                "2개월부족수량": 20,
                "3개월부족수량": 30,
                "재고커버월수": 0.67,
                "당월 재고충족률": 66.67,
                "재고부족판정": "부족",
                "부족등급": "부족",
            },
            {
                "제품코드": "P002",
                "제품명": "테스트제품2",
                "규격": "EA",
                "제조사명": "제조사A",
                "제품그룹명": "G",
                "제품구분명": "D",
                "제품분류명": "C",
                "재고기준": "장부",
                "현재재고수량": 13,
                "현재재고금액": 143,
                "재고평가단가": 11,
                "당월 예상출고수량": 0,
                "당월 잔여예상출고수량": 0,
                "당월 출고진척률": 0,
                "부족예상수량": 0,
                "부족예상금액": 0,
                "1개월부족수량": 0,
                "2개월부족수량": 0,
                "3개월부족수량": 0,
                "재고커버월수": 0,
                "당월 재고충족률": 100,
                "재고부족판정": "수요없음",
                "부족등급": "정상",
            }
        ])
        supplier_stock = pd.DataFrame([
            {
                "제품코드": "P001",
                "매입처코드": "A",
                "매입처명": "매입처A",
                "매입처원본재고수량": 120,
                "매입처원본재고금액": 1200,
                "양수재고수량": 120,
                "양수재고금액": 700,
                "최근6완료월매입금액": 70,
                "전체완료월매입금액": 70,
                "매입처입고누계수량": 70,
            },
            {
                "제품코드": "P001",
                "매입처코드": "B",
                "매입처명": "매입처B",
                "매입처원본재고수량": -20,
                "매입처원본재고금액": -200,
                "양수재고수량": 80,
                "양수재고금액": 300,
                "최근6완료월매입금액": 30,
                "전체완료월매입금액": 30,
                "매입처입고누계수량": 30,
            },
            {
                "제품코드": "P002",
                "매입처코드": "C",
                "매입처명": "매입처C",
                "매입처원본재고수량": 7,
                "매입처원본재고금액": 999999,
                "양수재고수량": 7,
                "양수재고금액": 77,
                "최근6완료월매입금액": 0,
                "전체완료월매입금액": 0,
                "매입처입고누계수량": 7,
            },
            {
                "제품코드": "P002",
                "매입처코드": "D",
                "매입처명": "매입처D",
                "매입처원본재고수량": 6,
                "매입처원본재고금액": 888888,
                "양수재고수량": 6,
                "양수재고금액": 66,
                "최근6완료월매입금액": 0,
                "전체완료월매입금액": 0,
                "매입처입고누계수량": 6,
            },
        ])
        detail = supp_mod.build_supplier_allocation_detail(product_base, supplier_stock)
        summary_all = supp_mod.build_supplier_shortage_summary(detail, {})
        summary_b = supp_mod.build_supplier_shortage_summary(detail, {"buy_nm": "매입처B"})

        a_amt = float(summary_all.loc[summary_all["매입처코드"] == "A", "배정부족예상금액"].iloc[0])
        b_amt = float(summary_all.loc[summary_all["매입처코드"] == "B", "배정부족예상금액"].iloc[0])
        total_qty = float(detail["배정부족예상수량"].sum())
        stock_sum = float(detail.loc[detail["제품코드"] == "P001", "매입처원본재고수량"].sum())
        p001_stock_amt = float(detail.loc[detail["제품코드"] == "P001", "매입처원본재고금액"].sum())
        p002_stock_amt = float(detail.loc[detail["제품코드"] == "P002", "매입처원본재고금액"].sum())
        p002_qty_7_amt = float(detail.loc[detail["매입처코드"] == "C", "매입처원본재고금액"].iloc[0])
        p002_qty_6_amt = float(detail.loc[detail["매입처코드"] == "D", "매입처원본재고금액"].iloc[0])
        neg_b = float(detail.loc[detail["매입처코드"] == "B", "매입처원본재고수량"].iloc[0])
        neg_b_amt = float(detail.loc[detail["매입처코드"] == "B", "매입처원본재고금액"].iloc[0])
        filtered_b_amt = float(summary_b["배정부족예상금액"].sum())

        mismatches = []
        if abs(a_amt - 700) > 1e-6 or abs(b_amt - 300) > 1e-6:
            mismatches.append(f"70/30 allocation expected 700/300 got {a_amt}/{b_amt}")
        if abs(total_qty - 50) > 1e-6:
            mismatches.append(f"shortage qty should remain product-level 50 got {total_qty}")
        if abs(stock_sum - 100) > 1e-6 or abs(neg_b + 20) > 1e-6:
            mismatches.append(f"negative supplier stock not preserved stock_sum={stock_sum} neg_b={neg_b}")
        if abs(p001_stock_amt - 2000) > 1e-6 or abs(neg_b_amt + 400) > 1e-6:
            mismatches.append(f"stock amount must use product unit price p001_sum={p001_stock_amt} neg_b_amt={neg_b_amt}")
        if abs(p002_stock_amt - 143) > 1e-6 or abs(p002_qty_7_amt - 77) > 1e-6 or abs(p002_qty_6_amt - 66) > 1e-6:
            mismatches.append(f"7/6 stock amount expected 77/66 sum 143 got {p002_qty_7_amt}/{p002_qty_6_amt}/{p002_stock_amt}")
        if not set(detail["재고정합성"].dropna().astype(str).unique()).issubset({"일치"}):
            mismatches.append(f"stock consistency should match public basis got={detail['재고정합성'].dropna().astype(str).unique().tolist()}")
        if abs(filtered_b_amt - 300) > 1e-6 or len(summary_b) != 1:
            mismatches.append(f"supplier filter must apply after allocation got rows={len(summary_b)} amount={filtered_b_amt}")

        if mismatches:
            results.append(_fail("supplier stock shortage allocation fixture", "; ".join(mismatches)))
        else:
            results.append(_ok("supplier stock shortage allocation fixture", "unit-price stock amount, negative stock, 70/30 allocation, and post-filter OK"))

        old_product_loader = getattr(supp_mod, "load_product_shortage_base")
        old_supplier_loader = getattr(supp_mod, "load_supplier_product_stock")
        try:
            setattr(supp_mod, "load_product_shortage_base", lambda params: product_base.copy())
            setattr(supp_mod, "load_supplier_product_stock", lambda product_codes, params: supplier_stock.copy())
            payload = supp_mod.get_supplier_stock_shortage_result({"stock_mode": "book"})
            result_df = payload.get("df") if isinstance(payload.get("df"), pd.DataFrame) else payload.get("df_display")
            meta = payload.get("meta") or {}
            import io
            import json

            unsafe_types = (pd.DataFrame, pd.Series, bytes, bytearray)
            unsafe_meta = [k for k, v in meta.items() if isinstance(v, unsafe_types)]
            unsafe_attrs = [
                k
                for k, v in getattr(result_df, "attrs", {}).items()
                if isinstance(v, unsafe_types)
            ] if isinstance(result_df, pd.DataFrame) else []
            room = {
                "id": "fixture",
                "messages": [
                    {
                        "role": "assistant",
                        "type": payload.get("type"),
                        "action": payload.get("action"),
                        "title": payload.get("title"),
                        "meta": meta,
                    }
                ],
            }
            json.dumps(room, ensure_ascii=False)

            from app.ui.chat_middleware import _make_table_downloads
            from openpyxl import load_workbook

            _, xlsx_buf = _make_table_downloads(result_df)
            sheet_names = load_workbook(io.BytesIO(xlsx_buf.getvalue()), read_only=True).sheetnames
            follow_df = result_df.head(1).copy()
            follow_df.attrs.clear()
            _, follow_xlsx_buf = _make_table_downloads(follow_df)
            follow_sheet_names = load_workbook(io.BytesIO(follow_xlsx_buf.getvalue()), read_only=True).sheetnames
            if unsafe_meta or unsafe_attrs:
                results.append(_fail("supplier stock shortage json-safe payload", f"unsafe_meta={unsafe_meta} unsafe_attrs={unsafe_attrs}"))
            elif sheet_names != ["매입처별요약", "제품매입처상세"]:
                results.append(_fail("supplier stock shortage json-safe payload", f"unexpected excel sheets={sheet_names}"))
            elif follow_sheet_names != ["SIMS"]:
                results.append(_fail("supplier stock shortage json-safe payload", f"current-table result should be one sheet got={follow_sheet_names}"))
            else:
                results.append(_ok("supplier stock shortage json-safe payload", "json.dumps room OK; original Excel 2 sheets; current-table Excel one sheet"))
        finally:
            setattr(supp_mod, "load_product_shortage_base", old_product_loader)
            setattr(supp_mod, "load_supplier_product_stock", old_supplier_loader)

        try:
            generic_mod = importlib.import_module("app.ui.current_table_followups.generic")
            captured: dict[str, object] = {}

            def _push_table(**kwargs):
                captured.update(kwargs)
                return True

            top_df = pd.DataFrame({
                "매입처코드": [f"B{i:02d}" for i in range(25)],
                "배정부족예상금액": list(range(25)),
                "매입처원본재고금액": list(range(100, 125)),
            })
            top_df.attrs["supplier_detail_key"] = "detail-key-should-not-inherit"
            ok_top = generic_mod.handle_common_column_filter_followup(
                df=top_df,
                query="현재표 배정부족예상금액 TOP 20",
                top_n=20,
                table_key="supplier-table",
                source_action="매입처별 재고부족 현황",
                helpers={"push_table": _push_table},
                log=log,
            )
            out_top = captured.get("df")
            if not ok_top or not isinstance(out_top, pd.DataFrame):
                results.append(_fail("supplier current-table top amount", "TOP 20 route did not push table"))
            elif len(out_top) != 20 or float(out_top["배정부족예상금액"].iloc[0]) != 24 or float(out_top["배정부족예상금액"].iloc[-1]) != 5:
                results.append(_fail("supplier current-table top amount", f"unexpected TOP rows={len(out_top)} head/tail={out_top['배정부족예상금액'].head(1).tolist()}/{out_top['배정부족예상금액'].tail(1).tolist()}"))
            elif getattr(out_top, "attrs", {}).get("supplier_detail_key"):
                results.append(_fail("supplier current-table top amount", "supplier_detail_key leaked into current-table TOP result"))
            else:
                results.append(_ok("supplier current-table top amount", "배정부족예상금액 TOP 20 sorted desc; detail attrs cleared"))
        except Exception as e:
            results.append(_fail("supplier current-table top amount", f"{type(e).__name__}: {e}"))

        try:
            chat_mod = importlib.import_module("app.ui.chat_middleware")
            amount_df = pd.DataFrame({
                "기준월": ["202607"],
                "매입처코드": ["B001"],
                "완료월수": [6],
                "배정부족예상금액": [1234],
                "배정1개월부족금액": [100],
                "매입처원본재고금액": [200],
            })
            profile = chat_mod._build_sims_sales_time_profile(
                amount_df,
                chat_mod._sims_business_terms("매입처별 재고부족 현황"),
            )
            amount_col = str((profile or {}).get("amount_col") or "")
            amount_label = str((profile or {}).get("amount_label") or "")
            if amount_col != "배정부족예상금액":
                results.append(_fail("supplier amount column priority", f"expected 배정부족예상금액 got={amount_col}"))
            elif amount_label != "배정부족예상금액":
                results.append(_fail("supplier amount column priority", f"expected amount_label=배정부족예상금액 got={amount_label}"))
            else:
                current_profile = chat_mod._build_sims_sales_time_profile(
                    amount_df,
                    chat_mod._sims_business_terms("현재표 배정부족예상금액 TOP 20"),
                )
                current_amount_col = str((current_profile or {}).get("amount_col") or "")
                current_amount_label = str((current_profile or {}).get("amount_label") or "")
                inherited_ctx = chat_mod._build_sims_analysis_context_from_df(
                    amount_df,
                    result={},
                    action_name="현재표 부족제품수 10 이상 목록",
                    params={},
                    meta={
                        "current_table_followup": True,
                        "flow": "매입처별 재고부족",
                        "amount_label": "배정부족예상금액",
                        "amount_priority": (
                            "배정부족예상금액",
                            "배정1개월부족금액",
                            "매입처원본재고금액",
                        ),
                    },
                )
                inherited_amount_col = str(((inherited_ctx or {}).get("sales_time_profile") or {}).get("amount_col") or "")
                if current_amount_col != "배정부족예상금액" or current_amount_label != "배정부족예상금액":
                    results.append(_fail("supplier amount column priority", f"current-table amount mismatch col={current_amount_col} label={current_amount_label}"))
                elif inherited_amount_col != "배정부족예상금액":
                    results.append(_fail("supplier amount column priority", f"inherited current-table amount mismatch col={inherited_amount_col}"))
                else:
                    results.append(_ok("supplier amount column priority", "amount_col=배정부족예상금액 for source, followup, and inherited profile"))
        except Exception as e:
            results.append(_fail("supplier amount column priority", f"{type(e).__name__}: {e}"))

        try:
            small_df = pd.DataFrame({"배정부족예상금액": range(20), "부족제품수": range(20)})
            eight_df = pd.DataFrame({"배정부족예상금액": range(8), "부족제품수": range(8)})
            mid_259_df = pd.DataFrame({
                "배정부족예상금액": range(259),
                "부족제품수": range(259),
                "매입처명": [f"B{i}" for i in range(259)],
            })
            mid_470_df = pd.DataFrame({
                "배정부족예상금액": range(470),
                "음수재고제품수": range(470),
                "매입처명": [f"B{i}" for i in range(470)],
            })
            group_df = pd.DataFrame({"주요배분기준": ["A", "B", "C", "D"], "배정부족예상금액": [1, 2, 3, 4]})
            meta_followup = {"current_table_followup": True}
            checks = {
                "20": chat_mod._chat_is_current_followup_fast_table(small_df, meta_followup),
                "8": chat_mod._chat_is_current_followup_fast_table(eight_df, meta_followup),
                "259": chat_mod._chat_is_current_followup_fast_table(mid_259_df, meta_followup),
                "470": chat_mod._chat_is_current_followup_fast_table(mid_470_df, meta_followup),
                "4": chat_mod._chat_is_current_followup_fast_table(group_df, meta_followup),
            }
            if checks != {"20": False, "8": False, "259": True, "470": True, "4": False}:
                results.append(_fail("supplier current-table fast render policy", f"unexpected modes={checks}"))
            else:
                results.append(_ok("supplier current-table fast render policy", "20/8/4 small; 259/470 fast"))
        except Exception as e:
            results.append(_fail("supplier current-table fast render policy", f"{type(e).__name__}: {e}"))

        try:
            import streamlit as st

            old_reason = st.session_state.get("__ui_rerun_reason")
            old_reason_current = st.session_state.get("__ui_rerun_reason_current")
            old_path = st.session_state.get("__sims_table_render_path")
            old_last_key = st.session_state.get("__sims_last_table_key")
            old_latest_followup = st.session_state.get("__sims_latest_followup_table_key")
            try:
                st.session_state["__sims_table_render_path"] = "history"
                st.session_state["__sims_last_table_key"] = "new-table"
                st.session_state["__sims_latest_followup_table_key"] = "new-table"
                old_item = {"type": "table", "action": "매입처별 재고부족 현황", "table_key": "old-table"}
                old_meta = {"table_key": "old-table", "row_count": 975}
                new_item = {"type": "table", "action": "현재표 배정부족예상금액 TOP 20", "table_key": "new-table"}
                new_meta = {"table_key": "new-table", "row_count": 20}

                st.session_state["__ui_rerun_reason_current"] = "sims_panel_open"
                panel_open_old = chat_mod._should_full_render_sims_table(old_item, old_meta, "old")
                panel_open_new = chat_mod._should_full_render_sims_table(new_item, new_meta, "new")
                st.session_state["__ui_rerun_reason_current"] = "chat_room_change"
                room_change_old = chat_mod._should_full_render_sims_table(old_item, old_meta, "old")
                st.session_state["__ui_rerun_reason"] = "sims_action_change"
                st.session_state["__ui_rerun_reason_current"] = "current_table_followup"
                st.session_state[chat_mod._old_sims_table_force_key(old_item, old_meta, "old")] = True
                followup_old = chat_mod._should_full_render_sims_table(old_item, old_meta, "old")
                followup_new = chat_mod._should_full_render_sims_table(new_item, new_meta, "new")

                if panel_open_old or panel_open_new or room_change_old:
                    results.append(_fail("supplier history lightweight rerun policy", f"light rerun should skip history tables panel_old={panel_open_old} panel_new={panel_open_new} room_old={room_change_old}"))
                elif followup_old or not followup_new:
                    results.append(_fail("supplier history lightweight rerun policy", f"followup should skip old/render latest old={followup_old} new={followup_new}"))
                elif chat_mod._ui_rerun_reason() != "current_table_followup":
                    results.append(_fail("supplier history lightweight rerun policy", f"stale reason priority failed got={chat_mod._ui_rerun_reason()}"))
                else:
                    results.append(_ok("supplier history lightweight rerun policy", "panel/action rerun skips old tables; followup renders latest only; stale action reason ignored"))
            finally:
                st.session_state.pop(chat_mod._old_sims_table_force_key(old_item, old_meta, "old"), None)
                if old_reason is None:
                    st.session_state.pop("__ui_rerun_reason", None)
                else:
                    st.session_state["__ui_rerun_reason"] = old_reason
                if old_reason_current is None:
                    st.session_state.pop("__ui_rerun_reason_current", None)
                else:
                    st.session_state["__ui_rerun_reason_current"] = old_reason_current
                if old_path is None:
                    st.session_state.pop("__sims_table_render_path", None)
                else:
                    st.session_state["__sims_table_render_path"] = old_path
                if old_last_key is None:
                    st.session_state.pop("__sims_last_table_key", None)
                else:
                    st.session_state["__sims_last_table_key"] = old_last_key
                if old_latest_followup is None:
                    st.session_state.pop("__sims_latest_followup_table_key", None)
                else:
                    st.session_state["__sims_latest_followup_table_key"] = old_latest_followup
        except Exception as e:
            results.append(_fail("supplier history lightweight rerun policy", f"{type(e).__name__}: {e}"))

        try:
            main_src = Path("app/Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
            required_close_keys = [
                '"__sims_open"',
                '"__sims_open_ui"',
                '"__sims_panel_active"',
                '"__sims_force_open"',
                '"__sims_run_flag"',
                '"__sims_inner_submit"',
                '"__sims_selected_snapshot"',
            ]
            has_close_helper = "def _close_sims_panel_for_room_change" in main_src
            has_close_guard = '"__sims_close_for_chat_room_change"' in main_src
            has_guard_consume = "def _consume_sims_close_for_chat_room_change" in main_src
            has_panel_close_log = "[chat.room.panel_close]" in main_src
            has_after_success_log = '_log_sims_panel_room_close_state("after success=True")' in main_src
            has_no_open_assignment = '"__sims_open",\n            "__sims_open_ui"' not in main_src and 'ss["__sims_open"] = False' not in main_src
            room_switch_block = main_src[main_src.find("if picked and picked != ss.current_room:"):main_src.find("cur_name = id_to_name.get", main_src.find("if picked and picked != ss.current_room:"))]
            has_no_direct_close_in_room_switch = "_close_sims_panel_for_room_change()" not in room_switch_block
            has_render_block = 'if st.session_state.get("__ui_rerun_reason_current") == "chat_room_change":' in main_src and "should_render = False" in main_src
            has_room_reason = '"chat_room_change"' in main_src
            has_switch_total = 'switch_total = float(stats.get("event_to_main_elapsed") or 0.0) + float(stats.get("history_elapsed") or 0.0)' in main_src
            has_switch_event_id = '"__chat_room_switch_event_id"' in main_src and "event_id=%s" in room_switch_block
            has_event_id_perf = '"__ui_event_id"' in main_src and "[ui.event_to_rerun] event_id=%s" in main_src
            has_startup_pending = '"__auth_login_perf_pending"' in main_src and '"__auth_startup_perf_emitted_sig"' in main_src
            has_no_unconditional_startup_log = "[auth.startup.perf] company_select=" not in main_src
            has_save_detail = '"__chat_room_switch_save_detail"' in main_src and "json_serialize=%.3fs" in main_src
            has_sims_open_perf = "[sims.panel_open.perf]" in main_src and '"__sims_panel_open_fragment_elapsed"' in main_src
            has_authenticate_perf = "__auth_login_authenticate_elapsed" in main_src and "authenticate=%.3fs" in main_src
            has_script_path_perf = (
                "[ui.script_path.perf]" in main_src
                and "room_selector=%.3fs" in main_src
                and "sims_fragment=%.3fs" in main_src
                and "unattributed=%.3fs" in main_src
                and 'st.session_state["__ui_script_perf_durations"] = {}' in main_src
            )
            has_save_skip = (
                "[chat.save.skip]" in main_src
                and "reason=unchanged" in main_src
                and "unchanged_or_selection_only" in main_src
                and "compare_mode=%s" in main_src
                and "_record_chat_save_skip" in main_src
            )
            has_selection_only_save_skip = (
                "removed_empty_pending = _drop_empty_auto_rooms(keep_room_id=picked)" in room_switch_block
                and "dirty_reason=\"selection_only\"" in room_switch_block
                and "save_chat_rooms()" in room_switch_block
            )
            has_chat_save_diff = "[chat.save.diff]" in main_src and "changed_fields=%s" in main_src
            has_latest_message_anchor = (
                '"__chat_scroll_to_bottom_once"' not in main_src
                and '"__chat_scroll_event_id"' not in main_src
                and "[chat.room.autoscroll]" not in main_src
                and "setTimeout(run" not in main_src
                and "focus({ preventScroll: true })" not in main_src
                and "ssai-chat-bottom-anchor" in main_src
                and "ssai-latest-message-link" in main_src
                and 'href="#ssai-chat-bottom-anchor"' in main_src
                and '[data-testid="stChatInput"]' in main_src
                and "position: sticky" in main_src
            )
            missing = [k for k in required_close_keys if k not in main_src]
            if not has_close_helper or missing:
                results.append(_fail("chat room switch panel close policy", f"helper={has_close_helper} missing={missing}"))
            elif not has_room_reason or not has_switch_total or not has_close_guard or not has_guard_consume or not has_panel_close_log or not has_after_success_log or not has_no_open_assignment or not has_no_direct_close_in_room_switch or not has_render_block or not has_switch_event_id or not has_event_id_perf or not has_startup_pending or not has_no_unconditional_startup_log or not has_save_detail or not has_sims_open_perf or not has_authenticate_perf or not has_script_path_perf or not has_save_skip or not has_selection_only_save_skip or not has_chat_save_diff or not has_latest_message_anchor:
                results.append(_fail("chat room switch panel close policy", f"reason={has_room_reason} switch_total={has_switch_total} guard={has_close_guard} consume={has_guard_consume} log={has_panel_close_log} after_success={has_after_success_log} no_open_assign={has_no_open_assignment} no_direct_close={has_no_direct_close_in_room_switch} render_block={has_render_block} switch_event_id={has_switch_event_id} event_perf={has_event_id_perf} startup_pending={has_startup_pending} no_unconditional_startup={has_no_unconditional_startup_log} save_detail={has_save_detail} sims_open_perf={has_sims_open_perf} authenticate_perf={has_authenticate_perf} script_path_perf={has_script_path_perf} save_skip={has_save_skip} selection_only_skip={has_selection_only_save_skip} save_diff={has_chat_save_diff} latest_anchor={has_latest_message_anchor}"))
            else:
                results.append(_ok("chat room switch panel close policy", "room change consumes close guard, blocks SIMS render, logs event-scoped perf, startup perf is one-shot, skips unchanged saves, and uses manual latest-message anchor"))
        except Exception as e:
            results.append(_fail("chat room switch panel close policy", f"{type(e).__name__}: {e}"))

        try:
            login_src = Path("app/ui/ssai_login.py").read_text(encoding="utf-8")
            render_defs = len(re.findall(r"^def\s+render_company_selector\s*\(", login_src, flags=re.M))
            checks = {
                "single_render_company_selector": render_defs == 1,
                "empty_login_default": 'st.text_input("로그인 ID", value="", placeholder="아이디를 입력하세요")' in login_src,
                "enter_to_submit": "enter_to_submit=True" in login_src,
                "password_key_defined": 'password_key = "__ssai_company_change_sims_password"' in login_src,
                "clear_password_key_defined": 'clear_password_key = "__ssai_clear_company_change_sims_password"' in login_src,
                "logout_clears_password": '"__ssai_company_change_sims_password"' in login_src and '"__ssai_clear_company_change_sims_password"' in login_src,
                "ssart_admin_fallback_only_when_missing": 'if _is_ssart_user(user) and not sims_user_id_for_change:' in login_src,
                "normal_user_sims_id": 'sims_user_id_for_change = str(user.sims_user_id or "").strip()' in login_src,
                "sims_password_verify": "verify_sims_plain_password(" in login_src,
            }
            failed = [k for k, ok in checks.items() if not ok]
            if failed:
                results.append(_fail("ssai login company selector policy", f"failed={failed} render_defs={render_defs}"))
            else:
                results.append(_ok("ssai login company selector policy", "selector/password keys/login defaults/logout cleanup verified"))
        except Exception as e:
            results.append(_fail("ssai login company selector policy", f"{type(e).__name__}: {e}"))
    except Exception as e:
        results.append(_fail("supplier stock shortage allocation fixture", f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}"))

    results.extend(_run_customer_sales_forecast_basic_checks())
    return results


def _run_customer_sales_forecast_basic_checks() -> list[CheckResult]:
    results: list[CheckResult] = []
    try:
        customer_mod = importlib.import_module("app.services.analytics_customer_sales_forecast_service")
        chat_mod = importlib.import_module("app.ui.chat_middleware")
        views_mod = importlib.import_module("app.sims.views.analytics_views")
    except Exception as e:
        return [_fail("customer sales forecast import", f"{type(e).__name__}: {e}")]

    calls = {"r130": 0, "master": 0, "date_ranges": []}

    monthly_rows = pd.DataFrame(
        [
            {"기준월": "202601", "매출처코드": "50001", "매출공급가액": 1000, "매출세액": 100, "매출합계": 1100, "집계건수": 1},
            {"기준월": "202602", "매출처코드": "50001", "매출공급가액": 1200, "매출세액": 120, "매출합계": 1320, "집계건수": 1},
            {"기준월": "202603", "매출처코드": "50001", "매출공급가액": 1300, "매출세액": 130, "매출합계": 1430, "집계건수": 1},
            {"기준월": "202604", "매출처코드": "50001", "매출공급가액": 1400, "매출세액": 140, "매출합계": 1540, "집계건수": 1},
            {"기준월": "202605", "매출처코드": "50001", "매출공급가액": 1500, "매출세액": 150, "매출합계": 1650, "집계건수": 1},
            {"기준월": "202606", "매출처코드": "50001", "매출공급가액": 1600, "매출세액": 160, "매출합계": 1760, "집계건수": 1},
            {"기준월": "202607", "매출처코드": "50001", "매출공급가액": 1700, "매출세액": 170, "매출합계": 1870, "집계건수": 1},
            {"기준월": "202601", "매출처코드": "50002", "매출공급가액": 0, "매출세액": 0, "매출합계": 0, "집계건수": 1},
            {"기준월": "202607", "매출처코드": "50002", "매출공급가액": 900, "매출세액": 90, "매출합계": 990, "집계건수": 1},
        ]
    )
    master_rows = pd.DataFrame(
        [
            {"매출처코드": "50001", "매출처명": "한미거래처", "영업사원코드": "S1", "담당영업사원명": "김영업", "시도명": "서울", "시구군명": "강남구", "도로명": "테헤란로"},
            {"매출처코드": "50002", "매출처명": "종근당거래처", "영업사원코드": "S2", "담당영업사원명": "박영업", "시도명": "부산", "시구군명": "해운대구", "도로명": "센텀로"},
        ]
    )

    old_130 = getattr(customer_mod, "_load_rddbc130_monthly", None)
    old_master = getattr(customer_mod, "_load_customer_master", None)

    def fake_130(params, policy):
        calls["r130"] += 1
        date_from = str(params.get("date_from") or "")
        date_to = str(policy.get("effective_date_to") or policy.get("requested_date_to") or params.get("date_to") or "")
        calls["date_ranges"].append((date_from, date_to, policy.get("evaluation_mode")))
        out = monthly_rows.copy()
        if date_to == "20260702":
            out.loc[(out["매출처코드"].astype(str) == "50001") & (out["기준월"].astype(str) == "202607"), ["매출공급가액", "매출세액", "매출합계"]] = [300, 30, 330]
            out.loc[(out["매출처코드"].astype(str) == "50002") & (out["기준월"].astype(str) == "202607"), ["매출공급가액", "매출세액", "매출합계"]] = [200, 20, 220]
        out.attrs.update(
            {
                "source_table": "Rddbc130",
                "source_mode": "transaction_statement",
                "trans_di": "3",
                "date_from": date_from,
                "date_to": date_to,
                "raw_rows": int(out["집계건수"].sum()),
                "monthly_rows": int(len(out)),
                "total_supply": float(out["매출공급가액"].sum()),
                "total_tax": float(out["매출세액"].sum()),
                "total_amount": float(out["매출합계"].sum()),
            }
        )
        return out

    def fake_master(params):
        calls["master"] += 1
        out = master_rows.copy()
        if params.get("sido_nm"):
            out = out[out["시도명"].str.contains(str(params.get("sido_nm")), na=False)].copy()
        if params.get("ven_nm"):
            out = out[out["매출처명"].str.contains(str(params.get("ven_nm")), na=False)].copy()
        return out

    try:
        setattr(customer_mod, "_load_rddbc130_monthly", fake_130)
        setattr(customer_mod, "_load_customer_master", fake_master)

        params_current = {
            "month_from": "202601",
            "month_to": "202607",
            "date_from": "20260101",
            "date_to": "20260712",
            "policy_date": "20260712",
            "top": 0,
        }
        current = customer_mod.get_customer_sales_forecast_df(params_current)
        current_res = customer_mod.get_customer_sales_forecast_result(params_current)
        params_mid = {**params_current, "date_to": "20260702"}
        mid = customer_mod.get_customer_sales_forecast_df(params_mid)
        current_salesperson = customer_mod.get_salesperson_sales_forecast_df(params_current)
        current_region = customer_mod.get_region_sales_forecast_df(params_current)
        current_salesperson_res = customer_mod.get_salesperson_sales_forecast_result(params_current)
        current_region_res = customer_mod.get_region_sales_forecast_result(params_current)
        mid_salesperson = customer_mod.get_salesperson_sales_forecast_df(params_mid)
        mid_region = customer_mod.get_region_sales_forecast_df(params_mid)
        mismatches: list[str] = []
        required_cols = [
            "순번",
            "매출처코드",
            "매출처명",
            "총매출공급가액",
            "총매출세액",
            "총매출액",
            "완료월총매출",
            "완료월평균매출",
            "당월 현재매출",
            "당월 예상매출",
            "당월 잔여예상",
            "당월 진척률",
            "다음월예상매출",
            "예상등급",
            "2026-07 매출",
        ]
        for c in required_cols:
            if c not in current.columns:
                mismatches.append(f"missing current column {c}")
        salesperson_required_cols = [
            "순번",
            "영업사원코드",
            "담당영업사원명",
            "매출처수",
            "총매출액",
            "당월 현재매출",
            "당월 예상매출",
            "예상등급",
            "2026-07 매출",
        ]
        for c in salesperson_required_cols:
            if c not in current_salesperson.columns:
                mismatches.append(f"missing salesperson forecast column {c}")
        region_required_cols = [
            "순번",
            "시도명",
            "시구군명",
            "매출처수",
            "영업사원수",
            "총매출액",
            "당월 현재매출",
            "당월 예상매출",
            "예상등급",
            "2026-07 매출",
        ]
        for c in region_required_cols:
            if c not in current_region.columns:
                mismatches.append(f"missing region forecast column {c}")
        if any(str(c).endswith(" 수량") or str(c) in {"제품코드", "제품명"} for c in current.columns):
            mismatches.append("customer forecast exposed product/qty columns")
        if any(str(c).endswith(" 수량") or str(c) in {"제품코드", "제품명", "매출처코드", "매출처명"} for c in current_salesperson.columns):
            mismatches.append("salesperson forecast exposed customer/product/qty columns")
        if any(str(c).endswith(" 수량") or str(c) in {"제품코드", "제품명", "매출처코드", "매출처명"} for c in current_region.columns):
            mismatches.append("region forecast exposed customer/product/qty columns")
        if calls["r130"] < 3:
            mismatches.append("Rddbc130 transaction statement loader should be used for every period")
        if any(start != "20260101" for start, _end, _mode in calls["date_ranges"]):
            mismatches.append(f"Rddbc130 loader did not preserve date_from ranges={calls['date_ranges']}")
        if not any(end == "20260702" for _start, end, _mode in calls["date_ranges"]):
            mismatches.append(f"Rddbc130 loader did not receive exact historical date_to ranges={calls['date_ranges']}")
        row_50001 = current[current["매출처코드"].astype(str) == "50001"].iloc[0]
        if int(row_50001["완료월수"]) != 6:
            mismatches.append(f"current completed month count expected=6 got={row_50001['완료월수']}")
        if abs(float(row_50001["당월 현재매출"]) - 1870) > 1e-9:
            mismatches.append(f"current month sales expected Rddbc130 total 1870 got={row_50001['당월 현재매출']}")
        mid_row = mid[mid["매출처코드"].astype(str) == "50001"].iloc[0]
        if "평가월 매출" not in mid.columns or "당월 현재매출" in mid.columns:
            mismatches.append("historical midmonth label map failed")
        if abs(float(mid_row["평가월 매출"]) - 330) > 1e-9:
            mismatches.append(f"midmonth evaluation sales expected Rddbc130 total 330 got={mid_row['평가월 매출']}")
        if abs(float(mid_row["평가월 예상매출"]) - float(row_50001["당월 예상매출"])) > 1e-9:
            mismatches.append("current and midmonth expected sales should match from completed months")
        if "평가월 매출" not in mid_salesperson.columns or "당월 현재매출" in mid_salesperson.columns:
            mismatches.append("salesperson historical midmonth label map failed")
        if "평가월 매출" not in mid_region.columns or "당월 현재매출" in mid_region.columns:
            mismatches.append("region historical midmonth label map failed")
        total_customer = float(current["총매출액"].sum())
        if abs(float(current_salesperson["총매출액"].sum()) - total_customer) > 1e-9:
            mismatches.append("salesperson forecast total should match customer forecast total")
        if abs(float(current_region["총매출액"].sum()) - total_customer) > 1e-9:
            mismatches.append("region forecast total should match customer forecast total")
        current_sales_customer = float(current["당월 현재매출"].sum())
        if abs(float(current_salesperson["당월 현재매출"].sum()) - current_sales_customer) > 1e-9:
            mismatches.append("salesperson current sales should match customer current sales")
        if abs(float(current_region["당월 현재매출"].sum()) - current_sales_customer) > 1e-9:
            mismatches.append("region current sales should match customer current sales")
        if current_salesperson[["영업사원코드", "담당영업사원명"]].duplicated().any():
            mismatches.append("salesperson forecast should keep one final row per salesperson")
        if current_region[["시도명", "시구군명"]].duplicated().any():
            mismatches.append("region forecast should keep one final row per region")
        meta = current_res.get("meta") or {}
        if meta.get("analysis_type") != "customer_sales_forecast" or meta.get("summary_type") != "customer_forecast":
            mismatches.append(f"unexpected result meta={meta}")
        if meta.get("source_table") != "Rddbc130" or meta.get("source_mode") != "transaction_statement" or str(meta.get("trans_di")) != "3":
            mismatches.append(f"unexpected source meta={meta}")
        if meta.get("raw_rows", 0) <= 0 or meta.get("monthly_rows", 0) <= 0:
            mismatches.append(f"source row meta missing={meta}")
        if not isinstance(meta.get("salesperson_count"), int):
            mismatches.append(f"salesperson_count should be int got={type(meta.get('salesperson_count')).__name__}")
        if isinstance(meta.get("salesperson_count"), dict):
            mismatches.append("salesperson_count must not contain distribution dict")
        if not isinstance(meta.get("region_count"), int):
            mismatches.append(f"region_count should be int got={type(meta.get('region_count')).__name__}")
        if isinstance(meta.get("region_count"), dict):
            mismatches.append("region_count must not contain distribution dict")
        if not isinstance(meta.get("salesperson_distribution"), dict) or not meta.get("salesperson_distribution"):
            mismatches.append("salesperson_distribution should be separate non-empty dict")
        if not isinstance(meta.get("province_distribution"), dict) or not meta.get("province_distribution"):
            mismatches.append("province_distribution should be separate non-empty dict")
        if not isinstance(meta.get("region_distribution"), dict) or not meta.get("region_distribution"):
            mismatches.append("region_distribution should be separate non-empty dict")
        if not isinstance(meta.get("forecast_grade_counts"), dict) or not meta.get("forecast_grade_counts"):
            mismatches.append("forecast_grade_counts missing")
        if current["매출처코드"].duplicated().any():
            mismatches.append("customer forecast should keep one final row per customer")
        sp_meta = current_salesperson_res.get("meta") or {}
        if sp_meta.get("analysis_type") != "salesperson_sales_forecast" or sp_meta.get("summary_type") != "salesperson_forecast":
            mismatches.append(f"unexpected salesperson meta={sp_meta}")
        if not isinstance(sp_meta.get("salesperson_count"), int) or isinstance(sp_meta.get("salesperson_count"), dict):
            mismatches.append(f"salesperson forecast salesperson_count should be int got={type(sp_meta.get('salesperson_count')).__name__}")
        rg_meta = current_region_res.get("meta") or {}
        if rg_meta.get("analysis_type") != "region_sales_forecast" or rg_meta.get("summary_type") != "region_forecast":
            mismatches.append(f"unexpected region meta={rg_meta}")
        if not isinstance(rg_meta.get("region_count"), int) or isinstance(rg_meta.get("region_count"), dict):
            mismatches.append(f"region forecast region_count should be int got={type(rg_meta.get('region_count')).__name__}")
        llm_df = current.copy()
        llm_df.insert(1, "거래일자", "20260712")
        amount_profile = chat_mod._build_sims_sales_time_profile(  # noqa: SLF001
            llm_df,
            chat_mod._sims_business_terms("매출처별 매출 예상"),  # noqa: SLF001
        )
        if amount_profile.get("amount_col") == "매출처코드":
            mismatches.append("customer forecast LLM amount_col must not be 매출처코드")
        if amount_profile.get("amount_col") != "총매출액":
            mismatches.append(f"customer forecast LLM amount_col expected 총매출액 got={amount_profile.get('amount_col')}")
        sp_llm = current_salesperson.copy()
        sp_llm.insert(1, "거래일자", "20260712")
        sp_amount_profile = chat_mod._build_sims_sales_time_profile(  # noqa: SLF001
            sp_llm,
            chat_mod._sims_business_terms("영업사원별 매출 예상"),  # noqa: SLF001
        )
        if sp_amount_profile.get("amount_col") != "총매출액":
            mismatches.append(f"salesperson forecast LLM amount_col expected 총매출액 got={sp_amount_profile.get('amount_col')}")
        rg_llm = current_region.copy()
        rg_llm.insert(1, "거래일자", "20260712")
        rg_amount_profile = chat_mod._build_sims_sales_time_profile(  # noqa: SLF001
            rg_llm,
            chat_mod._sims_business_terms("지역별 매출 예상"),  # noqa: SLF001
        )
        if rg_amount_profile.get("amount_col") != "총매출액":
            mismatches.append(f"region forecast LLM amount_col expected 총매출액 got={rg_amount_profile.get('amount_col')}")

        old_form = getattr(views_mod, "_render_customer_sales_forecast_form", None)
        old_st = getattr(views_mod, "st", None)

        class _FakeSt:
            @staticmethod
            def subheader(*_args, **_kwargs):
                return None

            @staticmethod
            def caption(*_args, **_kwargs):
                return None

        try:
            setattr(views_mod, "st", _FakeSt())
            setattr(views_mod, "_render_customer_sales_forecast_form", lambda _action_key: (False, {}))
            for fn_name, title in [
                ("render_customer_sales_forecast_analysis", "매출처별 매출 예상"),
                ("render_salesperson_sales_forecast_analysis", "영업사원별 매출 예상"),
                ("render_region_sales_forecast_analysis", "지역별 매출 예상"),
            ]:
                payload = getattr(views_mod, fn_name)()
                if not isinstance(payload, dict) or payload.get("final") is not False or payload.get("title") != title:
                    mismatches.append(f"{fn_name} initial render contract failed payload={payload}")
        finally:
            if old_form is not None:
                setattr(views_mod, "_render_customer_sales_forecast_form", old_form)
            if old_st is not None:
                setattr(views_mod, "st", old_st)

        filtered = customer_mod.get_customer_sales_forecast_df({**params_current, "sido_nm": "서울"})
        if set(filtered["매출처코드"].astype(str).tolist()) != {"50001"}:
            mismatches.append(f"master address filter failed codes={filtered['매출처코드'].astype(str).tolist()}")

        if mismatches:
            results.append(_fail("customer sales forecast", "; ".join(mismatches)))
        else:
            results.append(_ok("customer sales forecast", "Rddbc130 transaction statement source, labels, filters, schema, and meta verified"))
    except Exception as e:
        results.append(_fail("customer sales forecast", f"{type(e).__name__}: {e}"))
    finally:
        if old_130 is not None:
            setattr(customer_mod, "_load_rddbc130_monthly", old_130)
        if old_master is not None:
            setattr(customer_mod, "_load_customer_master", old_master)

    return results


# ---------------------------------------------------------------------
# Service live checks
# ---------------------------------------------------------------------
def _service_cases() -> list[ServiceCase]:
    common_params = {
        "month_from": "202501",
        "month_to": "202512",
        "date_from": "20250101",
        "date_to": "20251231",
        "source_mode": "monthly_book",
        "top": 2000,
    }

    common_condition_tokens = ("2025-01-01", "2025-12-31", "장부재고")

    return [
        ServiceCase(
            name="품목별 매출 추세 분석",
            function_name="get_sales_trend_result",
            params=dict(common_params),
            expected_title_contains="품목별 매출 추세",
            expected_meta_key="trend_judge_counts",
            expected_analysis_type="sales_trend",
            expected_condition_tokens=common_condition_tokens,
            require_seq_column=True,
        ),
        ServiceCase(
            name="품목별 매출 추세 요약표",
            function_name="get_sales_trend_summary_result",
            params=dict(common_params),
            expected_title_contains="품목별 매출 추세 요약표",
            expected_meta_key="trend_judge_counts",
            expected_analysis_type="sales_trend",
            expected_condition_tokens=common_condition_tokens,
            require_seq_column=True,
        ),
        ServiceCase(
            name="품목별 매출 예상",
            function_name="get_sales_forecast_result",
            params=dict(common_params),
            expected_title_contains="품목별 매출 예상",
            expected_meta_key="forecast_grade_counts",
            expected_analysis_type="sales_forecast",
            expected_condition_tokens=common_condition_tokens,
            require_seq_column=True,
        ),
        ServiceCase(
            name="품목별 재고부족현황",
            function_name="get_stock_shortage_result",
            params={
                **common_params,
                "stock_mode": "book",
            },
            expected_title_contains="품목별 재고부족현황",
            expected_meta_key="shortage_grade_counts",
            expected_analysis_type="stock_shortage",
            expected_condition_tokens=common_condition_tokens,
            require_seq_column=True,
        ),
        ServiceCase(
            name="품목별 매출 추세 분석 - 추세판정 필터",
            function_name="get_sales_trend_result",
            params={
                **common_params,
                "trend_judge": "감소",
            },
            expected_title_contains="품목별 매출 추세",
            expected_meta_key="trend_judge_counts",
            expected_analysis_type="sales_trend",
            expected_condition_tokens=common_condition_tokens + ("추세판정", "감소"),
            allow_zero_rows=True,
        ),
        ServiceCase(
            name="품목별 매출 추세 요약표 - 추세판정 필터",
            function_name="get_sales_trend_summary_result",
            params={
                **common_params,
                "trend_judge": "증가",
            },
            expected_title_contains="품목별 매출 추세 요약표",
            expected_meta_key="trend_judge_counts",
            expected_analysis_type="sales_trend",
            expected_condition_tokens=common_condition_tokens + ("추세판정", "증가"),
            allow_zero_rows=True,
        ),
        ServiceCase(
            name="품목별 매출 예상 - 추세판정 필터",
            function_name="get_sales_forecast_result",
            params={
                **common_params,
                "trend_judge": "반품주의",
            },
            expected_title_contains="품목별 매출 예상",
            expected_meta_key="forecast_grade_counts",
            expected_analysis_type="sales_forecast",
            expected_condition_tokens=common_condition_tokens + ("추세판정", "반품주의"),
            allow_zero_rows=True,
        ),
        ServiceCase(
            name="품목별 재고부족현황 - 부족등급 필터",
            function_name="get_stock_shortage_result",
            params={
                **common_params,
                "stock_mode": "book",
                "shortage_grade": "정상",
            },
            expected_title_contains="품목별 재고부족현황",
            expected_meta_key="shortage_grade_counts",
            expected_analysis_type="stock_shortage",
            expected_condition_tokens=common_condition_tokens + ("부족등급", "정상"),
            allow_zero_rows=True,
        ),
    ]

def _evaluate_service_payload(case: ServiceCase, payload: Any) -> CheckResult:
    name = f"service: {case.name}"

    if not isinstance(payload, dict):
        return _fail(name, f"payload가 dict가 아님: {type(payload).__name__}")

    title = str(payload.get("title") or payload.get("action") or "").strip()
    action = str(payload.get("action") or "").strip()
    meta = payload.get("meta") or {}
    ptype = str(payload.get("type") or "").strip()

    if case.expected_title_contains not in title and case.expected_title_contains not in action:
        return _fail(
            name,
            f"title/action mismatch: expected contains {case.expected_title_contains!r}, title={title!r}, action={action!r}",
        )

    row_count = _payload_row_count(payload)
    if row_count <= 0 and not case.allow_zero_rows:
        return _fail(name, f"row_count가 0 이하: rows={row_count}, title={title!r}, type={ptype!r}")

    cols = _payload_columns(payload)

    if row_count > 0 and case.require_seq_column and "순번" not in cols:
        return _fail(name, f"'순번' 컬럼 없음. columns={cols[:20]}")

    if row_count > 0 and case.expected_meta_key:
        val = meta.get(case.expected_meta_key)
        if not isinstance(val, dict) or not val:
            return _fail(name, f"meta[{case.expected_meta_key!r}] 없음 또는 빈값: {val!r}")
        if case.expected_meta_key == "shortage_grade_counts":
            try:
                if sum(int(v or 0) for v in val.values()) != row_count:
                    return _fail(
                        name,
                        f"shortage_grade_counts 합계 불일치: counts={val!r}, rows={row_count}",
                    )
            except Exception:
                return _fail(name, f"shortage_grade_counts 값 변환 실패: {val!r}")

    if case.expected_analysis_type:
        got_analysis_type = str(meta.get("analysis_type") or "").strip()
        if got_analysis_type != case.expected_analysis_type:
            return _fail(
                name,
                (
                    f"analysis_type mismatch: "
                    f"expected={case.expected_analysis_type!r}, got={got_analysis_type!r}, "
                    f"meta_keys={list(meta.keys())}"
                ),
            )

    missing_tokens = _missing_condition_tokens(payload, case.expected_condition_tokens)
    if missing_tokens:
        return _fail(
            name,
            (
                f"조회조건 토큰 누락: missing={missing_tokens!r}, "
                f"condition_text={_condition_text_from_payload(payload)!r}"
            ),
        )

    if row_count > 0 and case.check_code_columns:
        code_problem = _code_column_dtype_problem(payload)
        if code_problem:
            return _fail(name, code_problem)

    summary_md = str(meta.get("summary_md") or "").strip()
    message = str(payload.get("message") or "").strip()

    if case.require_summary_md and not summary_md:
        return _fail(name, f"summary_md 누락: meta_keys={list(meta.keys())}")

    if case.require_message and not message:
        return _fail(name, f"message 누락: payload_keys={list(payload.keys())}")

    condition_preview = _short_text(_condition_text_from_payload(payload), 120)

    detail = (
        f"title={title!r}, action={action!r}, type={ptype!r}, "
        f"rows={row_count}, cols={len(cols)}, "
        f"analysis_type={meta.get('analysis_type')!r}, "
        f"{case.expected_meta_key}={meta.get(case.expected_meta_key) if case.expected_meta_key else None}, "
        f"summary_md={'Y' if summary_md else 'N'}, message={'Y' if message else 'N'}, "
        f"condition={condition_preview!r}"
    )
    return _ok(name, detail)

def run_service_live_checks() -> list[CheckResult]:
    results: list[CheckResult] = []

    try:
        mod = importlib.import_module("app.services.analytics_sales_trend_service")
    except Exception as e:
        return [_fail("import analytics service", f"{type(e).__name__}: {e}")]

    for case in _service_cases():
        fn = getattr(mod, case.function_name, None)
        if not callable(fn):
            results.append(_fail(f"service: {case.name}", f"{case.function_name} callable 없음"))
            continue

        try:
            payload = fn(case.params)
            results.append(_evaluate_service_payload(case, payload))
        except Exception as e:
            detail = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}"
            results.append(_fail(f"service: {case.name}", detail))

    return results


# ---------------------------------------------------------------------
# NLQ live checks
# ---------------------------------------------------------------------
class PayloadCapture:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def fake_push(self, payload=None, action=None, *args, **kwargs):
        if payload is None and args:
            payload = args[0]
        if action is None and len(args) >= 2:
            action = args[1]

        if isinstance(payload, dict):
            p = dict(payload)
            if action and not p.get("action"):
                p["action"] = action
            self.payloads.append(p)
        else:
            self.payloads.append(
                {
                    "final": True,
                    "type": "unknown",
                    "action": action,
                    "data": payload,
                    "meta": {},
                }
            )
        return True

    def pop_last(self) -> dict[str, Any] | None:
        if not self.payloads:
            return None
        return self.payloads[-1]


def _patch_push_function(capture: PayloadCapture) -> None:
    module_names = [
        "app.ui.chat_middleware",
        "app.sims.nlq.nlq_router",
    ]

    for module_name in module_names:
        try:
            mod = importlib.import_module(module_name)
            if hasattr(mod, "push_sims_result_to_chat"):
                setattr(mod, "push_sims_result_to_chat", capture.fake_push)
        except Exception:
            pass


def _make_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _next_seq_factory() -> Callable[[], int]:
    seq = {"v": 0}

    def _next_seq() -> int:
        seq["v"] += 1
        return seq["v"]

    return _next_seq


def _nlq_cases() -> list[NlqCase]:
    base_tokens = ("2025-01-01", "2025-12-31")

    return [
        NlqCase(
            "품목별 매출 추세 2025년 조회",
            "품목별 매출 추세 분석",
            expected_analysis_type="sales_trend",
            expected_meta_key="trend_judge_counts",
            expected_condition_tokens=base_tokens,
        ),
        NlqCase(
            "품목별 매출 추세 요약표 2025년 조회",
            "품목별 매출 추세 요약표",
            expected_analysis_type="sales_trend",
            expected_meta_key="trend_judge_counts",
            expected_condition_tokens=base_tokens,
        ),
        NlqCase(
            "품목별 매출 예상 2025년 조회",
            "품목별 매출 예상",
            expected_analysis_type="sales_forecast",
            expected_meta_key="forecast_grade_counts",
            expected_condition_tokens=base_tokens,
        ),
        NlqCase(
            "품목별 재고부족현황 2025년 장부재고 기준 조회",
            "품목별 재고부족현황",
            expected_analysis_type="stock_shortage",
            expected_meta_key="shortage_grade_counts",
            expected_params={"stock_mode": "book"},
            expected_condition_tokens=base_tokens + ("장부재고",),
        ),
        NlqCase(
            "품목별 재고부족현황 2025년 장부재고 기준 부족등급 정상 조회",
            "품목별 재고부족현황",
            expected_analysis_type="stock_shortage",
            expected_meta_key="shortage_grade_counts",
            expected_params={"stock_mode": "book", "shortage_grade": "정상"},
            expected_condition_tokens=base_tokens + ("장부재고", "부족등급", "정상"),
            allow_empty_meta_counts=True,
        ),
        NlqCase(
            "품목별 매출 추세 2025년 감소 조회",
            "품목별 매출 추세 분석",
            expected_analysis_type="sales_trend",
            expected_meta_key="trend_judge_counts",
            expected_params={"trend_judge": "감소"},
            expected_condition_tokens=base_tokens + ("추세판정", "감소"),
        ),
        NlqCase(
            "품목별 매출 추세 요약표 2025년 증가 조회",
            "품목별 매출 추세 요약표",
            expected_analysis_type="sales_trend",
            expected_meta_key="trend_judge_counts",
            expected_params={"trend_judge": "증가"},
            expected_condition_tokens=base_tokens + ("추세판정", "증가"),
        ),
        NlqCase(
            "품목별 매출 예상 2025년 반품주의 조회",
            "품목별 매출 예상",
            expected_analysis_type="sales_forecast",
            expected_meta_key="forecast_grade_counts",
            expected_params={"trend_judge": "반품주의"},
            expected_condition_tokens=base_tokens + ("추세판정", "반품주의"),
        ),
    ]

def _evaluate_nlq_case(case: NlqCase, handled: bool, payload: dict[str, Any] | None) -> CheckResult:
    name = f"nlq: {case.query}"

    if not handled:
        return _fail(name, "try_handle_nlq()가 False 반환")

    if not isinstance(payload, dict):
        return _fail(name, "payload 없음")

    action = str(payload.get("action") or payload.get("title") or "").strip()
    meta = payload.get("meta") or {}

    params = payload.get("params") or {}
    if case.expected_params:
        for k, expected_v in case.expected_params.items():
            got_v = params.get(k)
            if got_v != expected_v:
                return _fail(
                    name,
                    f"params mismatch {k!r}: expected={expected_v!r}, got={got_v!r}, params={params!r}",
                )

    if case.expected_action not in action:
        return _fail(name, f"action mismatch expected contains {case.expected_action!r}, got {action!r}")

    if not bool(meta.get("analysis_nlq")):
        return _fail(name, f"meta.analysis_nlq 누락: meta keys={list(meta.keys())}")

    if case.expected_meta_key:
        val = meta.get(case.expected_meta_key)
        if not isinstance(val, dict) or (not val and not case.allow_empty_meta_counts):
            return _fail(name, f"meta[{case.expected_meta_key!r}] 없음 또는 빈값: {val!r}")

    if case.expected_analysis_type:
        got_analysis_type = str(meta.get("analysis_type") or "").strip()
        if got_analysis_type != case.expected_analysis_type:
            return _fail(
                name,
                (
                    f"analysis_type mismatch: "
                    f"expected={case.expected_analysis_type!r}, got={got_analysis_type!r}, "
                    f"meta_keys={list(meta.keys())}"
                ),
            )

    missing_tokens = _missing_condition_tokens(payload, case.expected_condition_tokens)
    if missing_tokens:
        return _fail(
            name,
            (
                f"조회조건 토큰 누락: missing={missing_tokens!r}, "
                f"condition_text={_condition_text_from_payload(payload)!r}"
            ),
        )

    summary_md = str(meta.get("summary_md") or "").strip()
    message = str(payload.get("message") or "").strip()

    if case.require_summary_md and not summary_md:
        return _fail(name, f"summary_md 누락: meta_keys={list(meta.keys())}")

    if case.require_message and not message:
        return _fail(name, f"message 누락: payload_keys={list(payload.keys())}")

    row_count = _payload_row_count(payload)
    cols = _payload_columns(payload)
    condition_preview = _short_text(_condition_text_from_payload(payload), 120)

    detail = (
        f"action={action!r}, rows={row_count}, cols={len(cols)}, "
        f"type={payload.get('type')!r}, "
        f"analysis_type={meta.get('analysis_type')!r}, "
        f"analysis_nlq={meta.get('analysis_nlq')!r}, "
        f"summary_md={'Y' if summary_md else 'N'}, message={'Y' if message else 'N'}, "
        f"condition={condition_preview!r}"
    )
    return _ok(name, detail)

def run_nlq_live_checks() -> list[CheckResult]:
    results: list[CheckResult] = []

    try:
        router = importlib.import_module("app.sims.nlq.nlq_router")
        try_handle_nlq = getattr(router, "try_handle_nlq")
    except Exception as e:
        return [_fail("import router.try_handle_nlq", f"{type(e).__name__}: {e}")]

    capture = PayloadCapture()
    _patch_push_function(capture)

    for case in _nlq_cases():
        room: dict[str, Any] = {"messages": []}
        session_state: dict[str, Any] = {
            "__sims_selected": {},
            "__io_pending_product_pick": {},
        }
        next_seq = _next_seq_factory()
        before_count = len(capture.payloads)

        try:
            handled = bool(
                try_handle_nlq(
                    case.query,
                    room=room,
                    session_state=session_state,
                    make_ts=_make_ts,
                    next_seq=next_seq,
                    logger=log,
                )
            )

            payload = capture.pop_last() if len(capture.payloads) > before_count else None
            results.append(_evaluate_nlq_case(case, handled, payload))

        except Exception as e:
            detail = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}"
            results.append(_fail(f"nlq: {case.query}", detail))

    return results


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="SIMS analytics/KPI regression checker")
    parser.add_argument(
        "--live",
        action="store_true",
        help="실제 analytics service DB 조회 smoke test",
    )
    parser.add_argument(
        "--nlq",
        action="store_true",
        help="try_handle_nlq 분석/KPI 라우팅까지 확인",
    )
    args = parser.parse_args()

    print(f"Project root: {PROJECT_ROOT}")

    failed = 0

    basic_results = run_basic_checks()
    failed += _print_results("BASIC IMPORT / HELPER CHECKS", basic_results)

    if args.live:
        service_results = run_service_live_checks()
        failed += _print_results("SERVICE LIVE CHECKS", service_results)

    if args.nlq:
        nlq_results = run_nlq_live_checks()
        failed += _print_results("NLQ LIVE ROUTING CHECKS", nlq_results)

    print()
    if failed:
        print(f"RESULT: FAIL ({failed} failed)")
        return 1

    print("RESULT: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
