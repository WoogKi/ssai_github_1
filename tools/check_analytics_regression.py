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
                ("품목별 재고부족현황 2025년 조회", "품목별 재고부족현황"),
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
