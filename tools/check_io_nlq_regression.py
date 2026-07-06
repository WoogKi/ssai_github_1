# tools/check_io_nlq_regression.py
# -*- coding: utf-8 -*-
# VERSION = "check_io_nlq_regression/2026-05-02-v1"
# 작성자: ChatGPT (OpenAI)
# 정의 :  IO / Docs / Stock NLQ regression checker
# Note: This script is a tool to check for regressions in SIMS analysis/KPI related to NLQ handling.

"""
IO / Docs / Stock NLQ regression checker.

기본 import 확인:
    & "C:\\Program Files\\Python313\\python.exe" tools\\check_io_nlq_regression.py

가벼운 대표 NLQ 라우팅/DB 조회:
    & "C:\\Program Files\\Python313\\python.exe" tools\\check_io_nlq_regression.py --live

전체 대표 NLQ 라우팅/DB 조회:
    & "C:\\Program Files\\Python313\\python.exe" tools\\check_io_nlq_regression.py --live-all

payload 확인:
    & "C:\\Program Files\\Python313\\python.exe" tools\\check_io_nlq_regression.py --live --show-payload
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

log = logging.getLogger("io_nlq_regression")


# ---------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------
@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class NlqCase:
    query: str
    expected_action: str
    require_handled: bool = True
    allow_no_payload: bool = False
    allow_zero_rows: bool = True
    expected_condition_tokens: tuple[str, ...] = ()
    forbidden_condition_tokens: tuple[str, ...] = ()
    expected_date_range: tuple[str, str] = ()
    date_column_candidates: tuple[str, ...] = ()

@dataclass
class ParserCase:
    query: str
    expected_action: str
    expected_params: dict[str, Any]
    forbidden_params: tuple[str, ...] = ()

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


def _safe_import(module_name: str) -> CheckResult:
    try:
        importlib.import_module(module_name)
        return _ok(f"import {module_name}")
    except Exception as e:
        return _fail(f"import {module_name}", f"{type(e).__name__}: {e}")


def _require_attr(module_name: str, attr_name: str) -> CheckResult:
    try:
        mod = importlib.import_module(module_name)
    except Exception as e:
        return _fail(f"import {module_name}", f"{type(e).__name__}: {e}")

    if not hasattr(mod, attr_name):
        return _fail(f"{module_name}.{attr_name}", "attribute 없음")

    obj = getattr(mod, attr_name)
    if not callable(obj):
        return _fail(f"{module_name}.{attr_name}", f"callable 아님: {type(obj).__name__}")

    return _ok(f"{module_name}.{attr_name}")


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

    for key in ("df_display", "df"):
        obj = payload.get(key)
        try:
            import pandas as pd

            if isinstance(obj, pd.DataFrame):
                return [str(c) for c in obj.columns]
        except Exception:
            pass

    records = payload.get("records")
    if isinstance(records, list) and records and isinstance(records[0], dict):
        return [str(c) for c in records[0].keys()]

    return []


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

def _payload_to_df(payload: dict[str, Any]):
    try:
        import pandas as pd

        for key in ("df_display", "df"):
            obj = payload.get(key)
            if isinstance(obj, pd.DataFrame):
                return obj

        records = payload.get("records")
        if isinstance(records, list):
            return pd.DataFrame(records)
    except Exception:
        return None

    return None


def _norm_yyyymmdd(value: Any) -> str:
    import re

    s = re.sub(r"[^0-9]", "", str(value or ""))
    if len(s) >= 8:
        return s[:8]
    return ""

def _parser_cases() -> list[ParserCase]:
    return [
        ParserCase(
            query="입고명세 매입처명 한미 2026-04-01~2026-04-30 조회",
            expected_action="입고명세 조회",
            expected_params={
                "ven_nm": "한미",
                "date_from": "20260401",
                "date_to": "20260430",
                "month_from": "202604",
                "month_to": "202604",
            },
            forbidden_params=("buy_nm",),
        ),
        ParserCase(
            query="입고명세 거래처명 한미 2026-04-01~2026-04-30 조회",
            expected_action="입고명세 조회",
            expected_params={
                "ven_nm": "한미",
                "date_from": "20260401",
                "date_to": "20260430",
                "month_from": "202604",
                "month_to": "202604",
            },
        ),
        ParserCase(
            query="한미 매입처 거래내역 조회",
            expected_action="입고명세 조회",
            expected_params={
                "ven_nm": "한미",
            },
            forbidden_params=("buy_nm",),
        ),
        ParserCase(
            query="한미 매입처에서 거래내역 조회",
            expected_action="입고명세 조회",
            expected_params={
                "ven_nm": "한미",
            },
            forbidden_params=("buy_nm",),
        ),
        ParserCase(
            query="출고명세 매출처명 대학약국 2026-04-19~2026-05-19 조회",
            expected_action="출고명세 조회",
            expected_params={
                "ven_nm": "대학약국",
                "date_from": "20260419",
                "date_to": "20260519",
                "month_from": "202604",
                "month_to": "202605",
            },
        ),
        ParserCase(
            query="출고명세 거래처명 대학약국 2026.04.19 ~ 2026.05.19 조회",
            expected_action="출고명세 조회",
            expected_params={
                "ven_nm": "대학약국",
                "date_from": "20260419",
                "date_to": "20260519",
                "month_from": "202604",
                "month_to": "202605",
            },
        ),
        ParserCase(
            query="출고명세 거래처명 대학약국 2026/04/19 ~ 2026/05/19 조회",
            expected_action="출고명세 조회",
            expected_params={
                "ven_nm": "대학약국",
                "date_from": "20260419",
                "date_to": "20260519",
                "month_from": "202604",
                "month_to": "202605",
            },
        ),
    ]


def _evaluate_parser_case(case: ParserCase) -> CheckResult:
    name = f"parse: {case.query}"

    try:
        from app.services.io_nlq import resolve_io_nlq

        parsed = resolve_io_nlq(case.query)
    except Exception as e:
        return _fail(name, f"{type(e).__name__}: {e}")

    if not isinstance(parsed, dict):
        return _fail(name, f"parsed 결과 없음: {parsed!r}")

    action = str(parsed.get("action") or "").strip()
    params = parsed.get("params") or {}

    if case.expected_action != action:
        return _fail(
            name,
            f"action mismatch: expected={case.expected_action!r}, got={action!r}, parsed={parsed!r}",
        )

    for key, expected_value in case.expected_params.items():
        got = params.get(key)
        if str(got or "") != str(expected_value):
            return _fail(
                name,
                (
                    f"param mismatch: key={key!r}, "
                    f"expected={expected_value!r}, got={got!r}, params={params!r}"
                ),
            )

    bad_keys = [key for key in case.forbidden_params if key in params]
    if bad_keys:
        return _fail(
            name,
            f"금지 param 포함: bad={bad_keys}, params={params!r}",
        )

    return _ok(name, f"action={action!r}, params={params!r}")


def run_parser_checks() -> list[CheckResult]:
    return [_evaluate_parser_case(case) for case in _parser_cases()]


# ---------------------------------------------------------------------
# Basic import checks
# ---------------------------------------------------------------------
def run_basic_checks() -> list[CheckResult]:
    results: list[CheckResult] = []

    # 라우터
    results.append(_require_attr("app.sims.nlq.nlq_router", "try_handle_nlq"))

    # IO NLQ parser/service modules
    modules = [
        "app.services.io_nlq",
        "app.services.rddbc110_service",
        "app.services.rddbc120_service",
        "app.services.rddbc130_service",
        "app.services.rddbc140_service",
        "app.services.rddbc210_service",
        "app.services.rddbc220_service",
        "app.services.product_flow_service",
        "app.services.product_inventory_service",
        "app.ui.chat_middleware",
    ]

    for module_name in modules:
        results.append(_safe_import(module_name))

    # push function 존재 확인
    results.append(_require_attr("app.ui.chat_middleware", "push_sims_result_to_chat"))

    # 선택적 parser 함수 확인: 프로젝트 버전에 따라 이름이 다를 수 있으므로 있으면 확인, 없으면 skip 처리
    optional_attrs = [
        ("app.services.io_nlq", "parse_io_nlq"),
        ("app.services.io_nlq", "try_parse_io_nlq"),
        ("app.services.io_nlq", "parse_nlq"),
        ("app.services.io_nlq", "detect_io_action"),
    ]

    found_any = False
    for module_name, attr_name in optional_attrs:
        try:
            mod = importlib.import_module(module_name)
            obj = getattr(mod, attr_name, None)
            if callable(obj):
                found_any = True
                results.append(_ok(f"optional parser found: {module_name}.{attr_name}"))
        except Exception:
            pass

    if not found_any:
        results.append(
            _ok(
                "optional io_nlq parser function",
                "공개 parser 함수명은 확인하지 않음. 라우터 live 테스트로 검증.",
            )
        )

    return results


# ---------------------------------------------------------------------
# Payload capture
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

    def last_since(self, before_count: int) -> dict[str, Any] | None:
        if len(self.payloads) <= before_count:
            return None
        return self.payloads[-1]


def _patch_push_function(capture: PayloadCapture) -> None:
    """
    NLQ 라우터와 관련 모듈에 push_sims_result_to_chat가 직접 import되어 있을 수 있으므로
    가능한 모듈의 같은 이름을 fake_push로 교체한다.
    """
    module_names = [
        "app.ui.chat_middleware",
        "app.sims.nlq.nlq_router",
        "app.services.io_nlq",
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


def _extract_payload_from_room(room: dict[str, Any]) -> dict[str, Any] | None:
    """
    혹시 fake_push를 타지 않고 room.messages에 직접 들어간 결과가 있으면 회수한다.
    """
    msgs = room.get("messages") or []
    if not msgs:
        return None

    last = msgs[-1]
    if isinstance(last, dict):
        content = last.get("content")
        if isinstance(content, dict):
            return content

        return {
            "final": True,
            "type": "text",
            "action": last.get("action"),
            "data": content,
            "message": content,
            "meta": last.get("meta") or {},
        }

    return None


# ---------------------------------------------------------------------
# Live cases
# ---------------------------------------------------------------------
def _smoke_live_cases() -> list[NlqCase]:
    """
    빠른 대표 테스트.
    검증 4종처럼 무거울 수 있는 조회는 live-all에 배치한다.
    """
    return [
        NlqCase(
            "입고명세 20250101~20250131 조회",
            "입고명세 조회",
            expected_condition_tokens=("2025-01-01", "2025-01-31"),
            expected_date_range=("20250101", "20250131"),
            date_column_candidates=("입고일자", "Rd11_In_YyMmDd"),
        ),
        NlqCase(
            "출고명세 20250101~20250131 조회",
            "출고명세 조회",
            expected_condition_tokens=("2025-01-01", "2025-01-31"),
            expected_date_range=("20250101", "20250131"),
            date_column_candidates=("출고일자", "Rd12_Out_YyMmDd"),
        ),
        NlqCase(
            "거래명세서 공통 202501 매입분 조회",
            "거래명세서 공통 조회",
            expected_condition_tokens=("2025-01", "매입분"),
        ),
        NlqCase(
            "세금계산서 공통 202501 매출 조회",
            "세금계산서 공통 조회",
            expected_condition_tokens=("2025-01", "매출"),
        ),
        NlqCase(
            "실재고월집계 2025 제품 00029 조회",
            "실재고월집계 조회",
            expected_condition_tokens=("2025-01", "2025-12", "제품 00029"),
        ),
        NlqCase(
            "장부재고월집계 2025 제품 00029 조회",
            "장부재고월집계 조회",
            expected_condition_tokens=("2025-01", "2025-12", "제품 00029"),
        ),
    ]

def _all_live_cases() -> list[NlqCase]:
    return [
        # 입고명세
        NlqCase("입고명세 20250101~20250131 조회", "입고명세 조회"),
        NlqCase("입고명세 거래처명 동제 조회", "입고명세 조회"),
        NlqCase("입고명세 제조사명 한미 조회", "입고명세 조회"),
        NlqCase("입고명세 제품 00029 조회", "입고명세 조회"),
        NlqCase("입고명세 재고위치 0001 조회", "입고명세 조회"),

        # 출고명세
        NlqCase("출고명세 20250101~20250131 조회", "출고명세 조회"),
        NlqCase("출고명세 거래처명 동제 조회", "출고명세 조회"),
        NlqCase("출고명세 매입처명 한미 조회", "출고명세 조회"),
        NlqCase("출고명세 실납처명 동제 조회", "출고명세 조회"),
        NlqCase("출고명세 영업사원명 김 조회", "출고명세 조회"),

        # 거래명세서 공통
        NlqCase("거래명세서 공통 202501 매입분 조회", "거래명세서 공통 조회"),
        NlqCase("거래명세서 공통 202501 매출분 조회", "거래명세서 공통 조회"),
        NlqCase("거래명세서 공통 거래처명 동제 조회", "거래명세서 공통 조회"),

        # 세금계산서 공통
        NlqCase("세금계산서 공통 202501 매입 조회", "세금계산서 공통 조회"),
        NlqCase("세금계산서 공통 202501 매출 조회", "세금계산서 공통 조회"),
        NlqCase("세금계산서 공통 거래처명 동제 조회", "세금계산서 공통 조회"),

        # 검증 4종
        # 기간 없는 검증은 기본 기간이 적용되어야 한다.
        NlqCase("입고 거래명세서 불일치 조회", "입고↔거래명세서 검증"),
        NlqCase("입고 세금계산서 불일치 조회", "입고↔세금계산서 검증"),
        NlqCase("출고 거래명세서 불일치 조회", "출고↔거래명세서 검증"),
        NlqCase("출고 세금계산서 불일치 조회", "출고↔세금계산서 검증"),

        # 월집계
        NlqCase("실재고월집계 202508 제품 00029 조회", "실재고월집계 조회"),
        NlqCase("실재고월집계 2025 제품 00029 조회", "실재고월집계 조회"),
        NlqCase("실재고월집계 202601 재고위치 0001 조회", "실재고월집계 조회"),

        NlqCase("장부재고월집계 202508 제품 00029 조회", "장부재고월집계 조회"),
        NlqCase("장부재고월집계 2025 제품 00029 조회", "장부재고월집계 조회"),
        NlqCase("장부재고월집계 202601 재고위치 0001 조회", "장부재고월집계 조회"),

        # 제품수불 / 제품재고
        NlqCase("제품수불현황 20250101~20251231 제품 00029 조회", "제품수불현황 조회"),
        NlqCase("제품수불현황 제품 00029 재고위치 0001 조회", "제품수불현황 조회"),
        NlqCase("제품수불현황 장부수불 제품 00029 조회", "제품수불현황 조회"),

        # 최근 실사용 회귀 케이스

        # 매입처명/매출처명은 IO 상세에서는 실제 명세의 거래처 조건으로 해석해야 한다.
        NlqCase(
            "입고명세 매입처명 한미 2026-04-01~2026-04-30 조회",
            "입고명세 조회",
            expected_condition_tokens=("2026-04-01", "2026-04-30", "거래처 한미"),
            forbidden_condition_tokens=("매입처 한미", "최근 1개월 자동적용"),
            expected_date_range=("20260401", "20260430"),
            date_column_candidates=("입고일자", "Rd11_In_YyMmDd"),
        ),
        NlqCase(
            "입고명세 매입처명 온라인 2026-04-01~2026-04-30 조회",
            "입고명세 조회",
            expected_condition_tokens=("2026-04-01", "2026-04-30", "거래처 온라인"),
            forbidden_condition_tokens=("매입처 온라인", "최근 1개월 자동적용"),
            expected_date_range=("20260401", "20260430"),
            date_column_candidates=("입고일자", "Rd11_In_YyMmDd"),
        ),
        NlqCase(
            "입고명세 매입처명 인천 2026-04-01~2026-04-30 조회",
            "입고명세 조회",
            expected_condition_tokens=("2026-04-01", "2026-04-30", "거래처 인천"),
            forbidden_condition_tokens=("매입처 인천", "최근 1개월 자동적용"),
            expected_date_range=("20260401", "20260430"),
            date_column_candidates=("입고일자", "Rd11_In_YyMmDd"),
        ),
        NlqCase(
            "출고명세 매출처명 대학약국 2026-04-19~2026-05-19 조회",
            "출고명세 조회",
            expected_condition_tokens=("2026-04-19", "2026-05-19", "거래처 대학약국"),
            forbidden_condition_tokens=("매출처 대학약국", "최근 1개월 자동적용"),
            expected_date_range=("20260419", "20260519"),
            date_column_candidates=("출고일자", "Rd12_Out_YyMmDd"),
        ),

        # - "제품 수불 현황" 띄어쓰기 보정
        # - 제품명 후보 안내표
        # - 제품코드 + 연도범위 조회
        # - 최근 입고명세 기간 조회
        NlqCase("제품 수불 현황 직듀오서방정", "제품수불현황 조회"),
        NlqCase("제품수불부 펜타듀르패취 조회", "제품수불현황 조회"),
        NlqCase("제품수불부 제품 00339 2024~2026 조회", "제품수불현황 조회"),
        NlqCase("입고명세 20260401~20260508 조회", "입고명세 조회"),

        NlqCase(
            "제품재고장 2025 제품그룹명 일반 조회",
            "제품재고현황 조회",
            expected_condition_tokens=("2025", "제품그룹명 일반"),
            forbidden_condition_tokens=("제품명 일반", "제품명 그룹명 일반"),
        ),
        NlqCase(
            "제품재고장 2025 구분명 의약품 조회",
            "제품재고현황 조회",
            expected_condition_tokens=("2025", "구분명 의약품"),
            forbidden_condition_tokens=("제품명 의약품",),
        ),
        NlqCase(
            "제품재고장 2025 제품분류명 정제 조회",
            "제품재고현황 조회",
            expected_condition_tokens=("2025", "제품분류명 정제"),
            forbidden_condition_tokens=("제품명 정제", "제품명 분류명 정제"),
        ),

        NlqCase(
            "제품재고현황 2023 ~ 2026 제품 00029 조회",
            "제품재고현황 조회",
            expected_condition_tokens=("2023", "2026", "제품코드 00029"),
            forbidden_condition_tokens=("제품명 00029",),
        ),

        NlqCase(
            "제품재고현황 제조사 대웅제약 2023~2026년 조회",
            "제품재고현황 조회",
            expected_condition_tokens=("2023", "2026", "제조사명 대웅제약"),
            forbidden_condition_tokens=("제품명 대웅제약",),
        ),
        NlqCase(
            "제품재고현황 제조사 건일제약 2023~2026년 조회",
            "제품재고현황 조회",
            expected_condition_tokens=("2023", "2026", "제조사명 건일제약"),
            forbidden_condition_tokens=("제품명 건일제약",),
        ),
        NlqCase(
            "제품재고현황 제조사 삼진제약 2023~2026년 조회",
            "제품재고현황 조회",
            expected_condition_tokens=("2023", "2026", "제조사명 삼진제약"),
            forbidden_condition_tokens=("제품명 삼진제약",),
        ),

    ]


def _evaluate_nlq_case(case: NlqCase, handled: bool, payload: dict[str, Any] | None) -> CheckResult:
    name = f"live: {case.query}"

    if case.require_handled and not handled:
        return _fail(name, "try_handle_nlq()가 False 반환")

    if not payload:
        if case.allow_no_payload:
            return _ok(name, "payload 없음 허용")
        return _fail(name, "payload 없음")

    action = str(payload.get("action") or payload.get("title") or "").strip()
    title = str(payload.get("title") or "").strip()
    ptype = str(payload.get("type") or "").strip()
    meta = payload.get("meta") or {}
    message = str(payload.get("message") or payload.get("data") or "").strip()

    if case.expected_action not in action and case.expected_action not in title:
        return _fail(
            name,
            f"action mismatch: expected contains {case.expected_action!r}, action={action!r}, title={title!r}",
        )

    row_count = _payload_row_count(payload)

    if not case.allow_zero_rows and row_count <= 0:
        return _fail(name, f"row_count가 0 이하: rows={row_count}")

    # IO 계열도 가능한 경우 query_summary 또는 조회조건이 있어야 한다.
    summary_md = str(meta.get("summary_md") or "").strip()
    query_summary = str(meta.get("query_summary") or "").strip()

    has_condition_text = (
        "조회조건:" in summary_md
        or "조회조건:" in message
        or bool(query_summary)
        or bool(meta.get("condition"))
    )

    # 검증류 일부 payload는 note만 있을 수 있으므로 경고 수준으로 detail에 표시한다.
    condition_mark = "Y" if has_condition_text else "N"

    if case.expected_date_range and case.date_column_candidates and row_count > 0:
        df = _payload_to_df(payload)
        if df is not None and not df.empty:
            date_col = None
            for cand in case.date_column_candidates:
                if cand in df.columns:
                    date_col = cand
                    break

            if not date_col:
                return _fail(
                    name,
                    f"날짜 컬럼 없음: candidates={case.date_column_candidates}, columns={list(df.columns)[:30]}",
                )

            start, end = case.expected_date_range
            bad_values = []

            for v in df[date_col].head(500).tolist():
                d = _norm_yyyymmdd(v)
                if d and not (start <= d <= end):
                    bad_values.append(str(v))
                    if len(bad_values) >= 5:
                        break

            if bad_values:
                return _fail(
                    name,
                    f"날짜 범위 밖 자료 포함: col={date_col}, range={start}~{end}, bad={bad_values}",
                )

    # IO NLQ는 조건 토큰이 반드시 있어야 한다.
    # 조건 토큰은 summary_md, message, query_summary, meta.condition 중 어디에나 있을 수 있다.
    if case.expected_condition_tokens or case.forbidden_condition_tokens:
        condition_text = " ".join(
            [
                str(summary_md or ""),
                str(message or ""),
                str(query_summary or ""),
                str(meta.get("condition") or ""),
            ]
        )

        if case.expected_condition_tokens:
            missing_tokens = [
                token
                for token in case.expected_condition_tokens
                if str(token) not in condition_text
            ]

            if missing_tokens:
                return _fail(
                    name,
                    f"조회조건 토큰 누락: missing={missing_tokens}, condition_text={condition_text[:300]!r}",
                )

        if case.forbidden_condition_tokens:
            bad_tokens = [
                token
                for token in case.forbidden_condition_tokens
                if str(token) in condition_text
            ]

            if bad_tokens:
                return _fail(
                    name,
                    f"금지 조회조건 포함: bad={bad_tokens}, condition_text={condition_text[:300]!r}",
                )

    # 입고명세/출고명세는 LLM 분석용 상세 요약 meta가 있어야 한다.
    if case.expected_action in {"입고명세 조회", "출고명세 조회"}:
        if not summary_md:
            return _fail(name, f"{case.expected_action} summary_md 누락")

        expected_key = "in_detail_summary" if case.expected_action == "입고명세 조회" else "out_detail_summary"

        has_detail_summary = (
            isinstance(meta.get(expected_key), dict)
            and bool(meta.get(expected_key))
        )

        if not has_detail_summary:
            return _fail(
                name,
                f"{case.expected_action} {expected_key} 누락: meta_keys={sorted(list(meta.keys()))}",
            )

        # 0건 text 결과는 최소 summary만 있어도 허용한다.
        # 실제 row가 있는 경우는 거래처/제품별 집계까지 보호한다.
        if row_count > 0:
            detail_summary = meta.get(expected_key) or {}

            required_keys = [
                "row_count",
                "qty_sum",
                "supply_sum",
                "tax_sum",
                "amount_sum",
                "vendor_count",
                "product_count",
                "top_products",
            ]

            if case.expected_action == "입고명세 조회":
                required_keys += ["top_purchase_vendors"]
            else:
                required_keys += ["top_sales_vendors", "top_sales_staff"]

            missing_summary_keys = [
                k for k in required_keys
                if k not in detail_summary
            ]

            if missing_summary_keys:
                return _fail(
                    name,
                    f"{case.expected_action} detail summary key 누락: missing={missing_summary_keys}, keys={sorted(list(detail_summary.keys()))}",
                )

    # 거래명세서 공통은 LLM 분석용 거래명세서 요약 meta가 있어야 한다.
    if case.expected_action == "거래명세서 공통 조회":
        if not summary_md:
            return _fail(name, "거래명세서 공통 summary_md 누락")

        has_doc_summary = (
            isinstance(meta.get("trans_doc_summary"), dict)
            and bool(meta.get("trans_doc_summary"))
        )

        if not has_doc_summary:
            return _fail(
                name,
                f"거래명세서 공통 trans_doc_summary 누락: meta_keys={sorted(list(meta.keys()))}",
            )

        if row_count > 0:
            doc_summary = meta.get("trans_doc_summary") or {}
            required_keys = [
                "row_count",
                "row_count_total",
                "display_row_count",
                "supply_sum",
                "tax_sum",
                "amount_sum",
                "vendor_count",
                "by_trans_type",
                "top_vendors",
                "by_match_status",
            ]

            missing_summary_keys = [
                k for k in required_keys
                if k not in doc_summary
            ]

            if missing_summary_keys:
                return _fail(
                    name,
                    f"거래명세서 공통 summary key 누락: missing={missing_summary_keys}, keys={sorted(list(doc_summary.keys()))}",
                )

    # 실재고월집계/장부재고월집계는 LLM 분석용 월집계 summary meta가 있어야 한다.
    if case.expected_action in {"실재고월집계 조회", "장부재고월집계 조회"}:
        if not summary_md:
            return _fail(name, f"{case.expected_action} summary_md 누락")

        has_monthly_summary = (
            isinstance(meta.get("monthly_stock_detail_summary"), dict)
            and bool(meta.get("monthly_stock_detail_summary"))
        )

        if not has_monthly_summary:
            return _fail(
                name,
                f"{case.expected_action} monthly_stock_detail_summary 누락: meta_keys={sorted(list(meta.keys()))}",
            )

        if row_count > 0:
            monthly_summary = meta.get("monthly_stock_detail_summary") or {}
            required_keys = [
                "row_count",
                "row_count_total",
                "display_row_count",
                "stock_basis",
                "in_qty_sum",
                "in_supply_sum",
                "in_tax_sum",
                "out_qty_sum",
                "out_supply_sum",
                "out_tax_sum",
                "product_count",
                "vendor_count",
                "stock_location_count",
                "by_month",
                "by_side",
                "by_io_type",
                "top_products",
                "top_vendors",
                "top_stock_locations",
            ]

            missing_summary_keys = [
                k for k in required_keys
                if k not in monthly_summary
            ]

            if missing_summary_keys:
                return _fail(
                    name,
                    f"{case.expected_action} monthly summary key 누락: missing={missing_summary_keys}, keys={sorted(list(monthly_summary.keys()))}",
                )


    # 제품수불현황은 LLM 분석용 요약 meta가 있어야 한다.
    # 단, 후보표(candidate_table)는 실제 수불표가 아니므로 flow_summary 대신 candidate_table을 허용한다.
    if case.expected_action == "제품수불현황 조회":
        if not summary_md:
            return _fail(name, "제품수불현황 summary_md 누락")

        is_candidate_table = bool(meta.get("candidate_table"))
        has_flow_summary = isinstance(meta.get("flow_summary"), dict) and bool(meta.get("flow_summary"))

        if not is_candidate_table and not has_flow_summary:
            return _fail(
                name,
                f"제품수불현황 flow_summary 누락: meta_keys={sorted(list(meta.keys()))}"
            )

    # 제품재고현황은 LLM 분석용 inventory_summary가 있어야 한다.
    # 단, 0건 text 결과는 inventory_summary가 없거나 비어 있어도 허용한다.
    if case.expected_action == "제품재고현황 조회":
        if not summary_md:
            return _fail(name, "제품재고현황 summary_md 누락")

        if row_count > 0:
            has_inventory_summary = (
                isinstance(meta.get("inventory_summary"), dict)
                and bool(meta.get("inventory_summary"))
            )
            if not has_inventory_summary:
                return _fail(
                    name,
                    f"제품재고현황 inventory_summary 누락: meta_keys={sorted(list(meta.keys()))}",
                )


    cols = _payload_columns(payload)

    cond_preview = (
        query_summary
        or str(meta.get("condition") or "")
        or summary_md
        or message
    )
    cond_preview = str(cond_preview or "").replace("\n", " ").strip()
    if len(cond_preview) > 100:
        cond_preview = cond_preview[:100] + "..."

    detail = (
        f"action={action!r}, title={title!r}, type={ptype or None}, "
        f"rows={row_count}, cols={len(cols)}, condition={cond_preview!r}"
    )

    return _ok(name, detail)

def run_live_checks(*, live_all: bool = False, show_payload: bool = False) -> list[CheckResult]:
    results: list[CheckResult] = []

    try:
        router = importlib.import_module("app.sims.nlq.nlq_router")
        try_handle_nlq = getattr(router, "try_handle_nlq")
    except Exception as e:
        return [_fail("import router.try_handle_nlq", f"{type(e).__name__}: {e}")]

    capture = PayloadCapture()
    _patch_push_function(capture)

    cases = _all_live_cases() if live_all else _smoke_live_cases()

    for case in cases:
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

            payload = capture.last_since(before_count)
            if payload is None:
                payload = _extract_payload_from_room(room)

            if show_payload and payload is not None:
                print()
                print(f"[PAYLOAD] {case.query}")
                print(f"  action={payload.get('action')!r}")
                print(f"  title={payload.get('title')!r}")
                print(f"  type={payload.get('type')!r}")
                print(f"  meta={payload.get('meta')!r}")

            results.append(_evaluate_nlq_case(case, handled, payload))

        except Exception as e:
            detail = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}"
            results.append(_fail(f"live: {case.query}", detail))

    return results


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="SIMS IO / Docs / Stock NLQ regression checker")
    parser.add_argument(
        "--live",
        action="store_true",
        help="가벼운 대표 NLQ 문장을 실제 라우팅/DB 조회로 테스트",
    )
    parser.add_argument(
        "--live-all",
        action="store_true",
        help="전체 대표 NLQ 문장을 실제 라우팅/DB 조회로 테스트",
    )
    parser.add_argument(
        "--show-payload",
        action="store_true",
        help="live 테스트 시 capture payload meta 출력",
    )
    args = parser.parse_args()

    print(f"Project root: {PROJECT_ROOT}")

    failed = 0

    basic_results = run_basic_checks()
    failed += _print_results("BASIC IMPORT CHECKS", basic_results)

    parser_results = run_parser_checks()
    failed += _print_results("IO NLQ PARSER CHECKS", parser_results)

    if args.live or args.live_all:

        live_results = run_live_checks(
            live_all=bool(args.live_all),
            show_payload=bool(args.show_payload),
        )
        failed += _print_results("IO NLQ LIVE ROUTING CHECKS", live_results)

    print()
    if failed:
        print(f"RESULT: FAIL ({failed} failed)")
        return 1

    print("RESULT: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())