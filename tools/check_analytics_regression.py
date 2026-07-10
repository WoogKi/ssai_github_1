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
