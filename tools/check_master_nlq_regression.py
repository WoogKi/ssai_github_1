# tools/check_master_nlq_regression.py
# -*- coding: utf-8 -*-
# VERSION = "check_master_nlq_regression/2026-05-02-v1"
# 참고: 이 스크립트는 마스터 NLQ 회귀 여부를 점검하기 위한 도구입니다.

"""
Master NLQ regression checker.

기본 실행:
    & "C:\\Program Files\\Python313\\python.exe" tools\\check_master_nlq_regression.py

실 DB/NLQ 라우팅까지 확인:
    & "C:\\Program Files\\Python313\\python.exe" tools\\check_master_nlq_regression.py --live

전체 대표 문장 확인:
    & "C:\\Program Files\\Python313\\python.exe" tools\\check_master_nlq_regression.py --live-all
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys
import traceback
from unittest.mock import patch
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

log = logging.getLogger("master_nlq_regression")


# ---------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------
@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class LiveCase:
    query: str
    expected_domain: str | None = None
    expected_action: str | None = None
    require_summary: bool = True
    require_handled: bool = True
    expected_condition_tokens: tuple[str, ...] = ()


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


def _require_attr(module_name: str, attr_name: str) -> CheckResult:
    try:
        mod = importlib.import_module(module_name)
    except Exception as e:
        return _fail(
            f"import {module_name}",
            f"{type(e).__name__}: {e}",
        )

    if not hasattr(mod, attr_name):
        return _fail(
            f"{module_name}.{attr_name}",
            "attribute 없음",
        )

    obj = getattr(mod, attr_name)
    if not callable(obj):
        return _fail(
            f"{module_name}.{attr_name}",
            f"callable 아님: {type(obj).__name__}",
        )

    return _ok(f"{module_name}.{attr_name}")


# ---------------------------------------------------------------------
# Basic import/function checks
# ---------------------------------------------------------------------
def run_basic_checks() -> list[CheckResult]:
    results: list[CheckResult] = []

    required = [
        ("app.sims.nlq.nlq_router", "try_handle_nlq"),
        ("app.sims.nlq.nlq_goods", "try_handle_goods_nlq"),
        ("app.sims.nlq.nlq_vendors", "try_handle_vendors_nlq"),
        ("app.sims.nlq.nlq_users", "try_handle_users_nlq"),
        ("app.sims.nlq.nlq_codes", "try_handle_codes_nlq"),
        ("app.services.rddbc040_service", "search_goods_full"),
        ("app.services.rddbc030_service", "search_vendors_full"),
        ("app.services.rddbc060_service", "search_users_full"),
        ("app.services.rddbc010_service", "search_rows"),
        ("app.services.rddbc021_service", "search_road_address"),
        ("app.sims.views.road_address", "_prepare_road_address_display"),
    ]

    for module_name, attr_name in required:
        results.append(_require_attr(module_name, attr_name))

    # 채팅 master source 상한은 display ENV와 독립이다.
    try:
        limits = importlib.import_module("app.sims.nlq.master_source_limit")
        resolve_limit = getattr(limits, "resolve_chat_source_limit")
        previous = os.environ.get("SIMS_MAX_ROWS_CHAT")
        previous_panel = os.environ.get("SIMS_PANEL_DISPLAY_MAX_ROWS")
        try:
            os.environ["SIMS_PANEL_DISPLAY_MAX_ROWS"] = "1000"
            os.environ["SIMS_MAX_ROWS_CHAT"] = "0"
            zero_ok = (
                resolve_limit(2000) == 0
                and resolve_limit(2000, action_cap=500) == 500
                and resolve_limit(1000, action_cap=1000) == 1000
            )
            os.environ["SIMS_MAX_ROWS_CHAT"] = "1500"
            positive_ok = (
                resolve_limit(2000) == 1500
                and resolve_limit(2000, action_cap=500) == 500
            )
        finally:
            if previous is None:
                os.environ.pop("SIMS_MAX_ROWS_CHAT", None)
            else:
                os.environ["SIMS_MAX_ROWS_CHAT"] = previous
            if previous_panel is None:
                os.environ.pop("SIMS_PANEL_DISPLAY_MAX_ROWS", None)
            else:
                os.environ["SIMS_PANEL_DISPLAY_MAX_ROWS"] = previous_panel

        if zero_ok and positive_ok:
            results.append(_ok("chat master source limit", "ENV 0=unlimited, 양수/action cap 계약"))
        else:
            results.append(_fail("chat master source limit", "SIMS_MAX_ROWS_CHAT resolver 계약 불일치"))
    except Exception as e:
        results.append(_fail("chat master source limit", f"{type(e).__name__}: {e}"))

    try:
        vendors = importlib.import_module("app.services.rddbc030_service")
        base_select = getattr(vendors, "_base_select")
        no_top_sql = str(base_select(0) or "")
        if "SELECT TOP" not in no_top_sql.upper():
            results.append(_ok("vendor SQL unlimited top", "top=0 → SQL TOP 생략"))
        else:
            results.append(_fail("vendor SQL unlimited top", "top=0인데 SQL TOP이 남아 있음"))
    except Exception as e:
        results.append(_fail("vendor SQL unlimited top", f"{type(e).__name__}: {e}"))

    try:
        vendors = importlib.import_module("app.services.rddbc030_service")
        base_from = str(getattr(vendors, "_base_from")() or "")
        expected_native_joins = (
            "AddU.Rd06_User_Cd = V.Rd03_Add_Cd",
            "ModU.Rd06_User_Cd = V.Rd03_Mod_Cd",
            "SalesMan.Rd06_User_Cd = V.Rd03_Sales_Man",
            "Road1.Rd021_RoadCd = V.Rd03_RoadCd",
            "Road1.Rd021_DongSeq = V.Rd03_DongSeq",
            "CostApply.Rd03_Ven_Cd = V.Rd03_Cost_Apply_Cd",
            "StockApply.Rd03_Ven_Cd = V.Rd03_Stock_Apply_Cd",
            "UnifyVen.Rd03_Ven_Cd = V.Rd03_Unify_Ven_Cd",
            "VenGroup.Rd01_Gcode = V.Rd03_Ven_Group_Gcode",
            "VenGroup.Rd01_Tcode = V.Rd03_Ven_Group",
        )
        if all(join in base_from for join in expected_native_joins) and "LTRIM(RTRIM(" not in base_from:
            results.append(_ok("vendor fixed-char native joins", "PK/FK join은 raw equality, 검색 정규화와 분리"))
        else:
            results.append(_fail("vendor fixed-char native joins", "master PK/FK join에 trim 함수 또는 raw equality 누락"))
    except Exception as e:
        results.append(_fail("vendor fixed-char native joins", f"{type(e).__name__}: {e}"))

    try:
        import inspect
        import pandas as pd

        view = importlib.import_module("app.sims.views.vendors")
        char8 = view._format_vendor_char8_date_series(
            pd.Series([" 20260830 ", "00000000", "2026/08/31", "invalid", None, "NaT"])
        ).tolist()
        stamped = view._format_vendor_datetime_series(
            pd.Series(["2026-08-30 12:34:56", "invalid", None, "NaT"])
        ).tolist()
        safe_like = view._is_sql_like_pushdown_safe
        render_src = inspect.getsource(view.render_vendor_list)
        if (
            char8 == ["2026-08-30", "", "2026-08-31", "", "", ""]
            and stamped == ["2026-08-30 12:34:56", "invalid", "", ""]
            and safe_like("한미")
            and not safe_like("한미_%")
            and "cost_apply_nm=_clean_text(cost_apply_nm) if _is_sql_like_pushdown_safe(cost_apply_nm) else \"\"" in render_src
            and "stock_apply_nm=_clean_text(stock_apply_nm) if _is_sql_like_pushdown_safe(stock_apply_nm) else \"\"" in render_src
        ):
            results.append(_ok("vendor display vectorization and ref-name pushdown", "date/null display contract and SQL-safe pushdown boundary"))
        else:
            results.append(_fail("vendor display vectorization and ref-name pushdown", f"char8={char8!r}, stamped={stamped!r}"))
    except Exception as e:
        results.append(_fail("vendor display vectorization and ref-name pushdown", f"{type(e).__name__}: {e}"))

    # 조건 없는 거래처 master 목록도 top=0 경로에서 handler가 완료되어야 한다.
    try:
        import pandas as pd

        vmod = importlib.import_module("app.sims.nlq.nlq_vendors")
        service = importlib.import_module("app.services.rddbc030_service")
        view = importlib.import_module("app.sims.views.vendors")
        original_search = service.search_vendors_full
        original_display = view._prepare_vendor_display
        original_push = vmod.push_sims_result_to_chat
        original_summary = vmod._build_vendor_master_llm_summary
        previous = os.environ.get("SIMS_MAX_ROWS_CHAT")
        captured: list[dict[str, Any]] = []
        calls: list[dict[str, Any]] = []

        def fake_search(**kwargs):
            calls.append(dict(kwargs))
            return pd.DataFrame({"거래처명": ["fixture 거래처"], "거래처코드": ["00001"]})

        try:
            os.environ["SIMS_MAX_ROWS_CHAT"] = "0"
            service.search_vendors_full = fake_search
            view._prepare_vendor_display = lambda df: df.copy()
            vmod.push_sims_result_to_chat = lambda payload, *_args, **_kwargs: captured.append(payload)
            vmod._build_vendor_master_llm_summary = None
            handled = vmod.try_handle_vendors_nlq(
                "거래처 조회",
                room={"messages": []},
                session_state={},
                make_ts=lambda: "fixture",
                next_seq=lambda: 1,
                logger=log,
            )
        finally:
            service.search_vendors_full = original_search
            view._prepare_vendor_display = original_display
            vmod.push_sims_result_to_chat = original_push
            vmod._build_vendor_master_llm_summary = original_summary
            if previous is None:
                os.environ.pop("SIMS_MAX_ROWS_CHAT", None)
            else:
                os.environ["SIMS_MAX_ROWS_CHAT"] = previous

        if handled and calls and calls[-1].get("top") == 0 and captured:
            results.append(_ok("vendor unfiltered top=0 handler", "거래처 조회 → handler/push, SQL TOP 생략 계약"))
        else:
            results.append(_fail("vendor unfiltered top=0 handler", f"handled={handled}, calls={calls!r}, pushed={len(captured)}"))
    except Exception as e:
        results.append(_fail("vendor unfiltered top=0 handler", f"{type(e).__name__}: {e}"))

    # 제품명 parser smoke check
    try:
        g = importlib.import_module("app.sims.nlq.nlq_goods")
        fn = getattr(g, "_extract_name_keyword", None)
        if callable(fn):
            v = fn("제품명 아티반 제품 조회")
            if str(v or "").strip() == "아티반":
                results.append(_ok("goods 제품명 parser", "제품명 아티반 제품 조회 → 아티반"))
            else:
                results.append(_fail("goods 제품명 parser", f"expected='아티반', got={v!r}"))
        else:
            results.append(_fail("goods 제품명 parser", "_extract_name_keyword 없음"))
    except Exception as e:
        results.append(_fail("goods 제품명 parser", f"{type(e).__name__}: {e}"))

    # 시스템/업무 동작 설명은 loose master 검색을 거치지 않고 일반 답변 경로로 남아야 한다.
    try:
        router = importlib.import_module("app.sims.nlq.nlq_router")
        goods = importlib.import_module("app.sims.nlq.nlq_goods")
        vendors = importlib.import_module("app.sims.nlq.nlq_vendors")
        explanation_cases = (
            "지금 SIMS ERP 연결에 대해 얼마나 알고 있어?",
            "ERP 연결 구조를 설명해줘",
            "제품재고장은 어떻게 동작해?",
            "현재고 계산 방식 알려줘",
        )
        with (
            patch.object(goods, "try_handle_goods_nlq", return_value=True) as goods_handler,
            patch.object(vendors, "try_handle_vendors_nlq", return_value=True) as vendor_handler,
            patch.object(router, "_try_handle_io_nlq", return_value=True) as io_handler,
        ):
            handled = [
                router.try_handle_nlq(
                    question,
                    room={},
                    session_state={},
                    make_ts=lambda: "fixture",
                    next_seq=lambda: 1,
                    logger=log,
                )
                for question in explanation_cases
            ]
        explanation_ok = (
            not any(handled)
            and goods_handler.call_count == 0
            and vendor_handler.call_count == 0
            and io_handler.call_count == 0
        )
        results.append(
            _ok("general explanation bypasses loose master handlers", "normal answer route")
            if explanation_ok
            else _fail(
                "general explanation bypasses loose master handlers",
                f"handled={handled}, goods={goods_handler.call_count}, vendors={vendor_handler.call_count}, io={io_handler.call_count}",
            )
        )
    except Exception as e:
        results.append(_fail("general explanation bypasses loose master handlers", f"{type(e).__name__}: {e}"))

    # 명시 업무코드 intent는 입출고 라벨과 충돌해도 IO보다 먼저 codes로 간다.
    try:
        router = importlib.import_module("app.sims.nlq.nlq_router")
        codes = importlib.import_module("app.sims.nlq.nlq_codes")
        calls: list[str] = []

        def _codes_handler(text, **_kwargs):
            calls.append(f"codes:{text}")
            return True

        with (
            patch.object(codes, "try_handle_codes_nlq", side_effect=_codes_handler),
            patch.object(router, "_try_handle_io_nlq", side_effect=lambda text, **_kwargs: calls.append(f"io:{text}") or True),
        ):
            code_handled = [
                router.try_handle_nlq(
                    question,
                    room={},
                    session_state={},
                    make_ts=lambda: "fixture",
                    next_seq=lambda: 1,
                    logger=log,
                )
                for question in ("업무코드 입출고구분 조회", "업무코드 코드종류 입출고구분 조회")
            ]
            outbound_handled = [
                router.try_handle_nlq(
                    question,
                    room={},
                    session_state={},
                    make_ts=lambda: "fixture",
                    next_seq=lambda: 1,
                    logger=log,
                )
                for question in ("출고명세 조회", "오늘 출고내역")
            ]
        code_precedence_ok = (
            all(code_handled)
            and all(outbound_handled)
            and calls[:2] == [
                "codes:업무코드 입출고구분 조회",
                "codes:업무코드 코드종류 입출고구분 조회",
            ]
            and calls[2:] == ["io:출고명세 조회", "io:오늘 출고내역"]
        )
        results.append(
            _ok("explicit business-code intent precedes IO", "codes before IO; outbound stays IO")
            if code_precedence_ok
            else _fail("explicit business-code intent precedes IO", repr(calls))
        )
    except Exception as e:
        results.append(_fail("explicit business-code intent precedes IO", f"{type(e).__name__}: {e}"))

    # Entry candidate is a route hint only; the final handler action is carried
    # by codes payload metadata and must not be misreported as an IO action.
    try:
        router = importlib.import_module("app.sims.nlq.nlq_router")
        codes = importlib.import_module("app.sims.nlq.nlq_codes")
        candidates = [
            router.resolve_new_sims_nlq_candidate(question)
            for question in ("업무코드 입출고구분 조회", "업무코드 코드종류 입출고구분 조회")
        ]
        source = Path(codes.__file__).read_text(encoding="utf-8")
        candidate_trace_ok = (
            all(candidate == {"route": "master", "action": "업무코드 조회"} for candidate in candidates)
            and '"route": "master"' in source
            and '"canonical_action": action' in source
        )
        results.append(
            _ok("business-code candidate and final trace boundary", "candidate=master; codes payload owns final action")
            if candidate_trace_ok
            else _fail("business-code candidate and final trace boundary", f"candidates={candidates!r}")
        )
    except Exception as e:
        results.append(_fail("business-code candidate and final trace boundary", f"{type(e).__name__}: {e}"))

    # 거래처 loose name parser smoke check
    try:
        vmod = importlib.import_module("app.sims.nlq.nlq_vendors")
        fn = getattr(vmod, "_extract_loose_vendorname_keyword", None)
        if callable(fn):
            v = fn("경동 거래처 조회")
            if str(v or "").strip() == "경동":
                results.append(_ok("vendors loose 거래처명 parser", "경동 거래처 조회 → 경동"))
            else:
                results.append(_fail("vendors loose 거래처명 parser", f"expected='경동', got={v!r}"))
        else:
            results.append(_ok("vendors loose 거래처명 parser", "private helper 없음: skip"))
    except Exception as e:
        results.append(_fail("vendors loose 거래처명 parser", f"{type(e).__name__}: {e}"))

    # 도로명주소 loose keyword parser smoke check
    try:
        r = importlib.import_module("app.sims.nlq.nlq_router")
        fn = getattr(r, "_extract_loose_road_keyword", None)
        if callable(fn):
            v = fn("없는주소가나다빵 도로명주소 조회")
            if str(v or "").strip() == "없는주소가나다빵":
                results.append(
                    _ok(
                        "road_address loose keyword parser",
                        "없는주소가나다빵 도로명주소 조회 → 없는주소가나다빵",
                    )
                )
            else:
                results.append(
                    _fail(
                        "road_address loose keyword parser",
                        f"expected='없는주소가나다빵', got={v!r}",
                    )
                )
        else:
            results.append(_fail("road_address loose keyword parser", "_extract_loose_road_keyword 없음"))
    except Exception as e:
        results.append(_fail("road_address loose keyword parser", f"{type(e).__name__}: {e}"))

    # 도로명주소 query summary helper smoke check
    try:
        r = importlib.import_module("app.sims.nlq.nlq_router")
        fn = getattr(r, "_build_road_query_summary", None)
        if callable(fn):
            v = fn(
                {
                    "sido_nm": "서울",
                    "gugun_nm": "",
                    "dong_nm": "",
                    "road_nm": "",
                    "road_addr_kw": "",
                    "keyword": "",
                }
            )
            if str(v or "").strip() == "시도명 서울":
                results.append(_ok("road_address query_summary helper", "시도명 서울"))
            else:
                results.append(_fail("road_address query_summary helper", f"expected='시도명 서울', got={v!r}"))
        else:
            results.append(_fail("road_address query_summary helper", "_build_road_query_summary 없음"))
    except Exception as e:
        results.append(_fail("road_address query_summary helper", f"{type(e).__name__}: {e}"))

    return results


# ---------------------------------------------------------------------
# Live NLQ checks
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
    """
    NLQ 모듈들이 push_sims_result_to_chat를 직접 import한 경우도 있으므로,
    관련 모듈의 같은 이름을 모두 fake_push로 교체한다.
    """
    module_names = [
        "app.ui.chat_middleware",
        "app.sims.nlq.nlq_router",
        "app.sims.nlq.nlq_goods",
        "app.sims.nlq.nlq_vendors",
        "app.sims.nlq.nlq_users",
        "app.sims.nlq.nlq_codes",
    ]

    for module_name in module_names:
        try:
            mod = importlib.import_module(module_name)
            if hasattr(mod, "push_sims_result_to_chat"):
                setattr(mod, "push_sims_result_to_chat", capture.fake_push)
        except Exception:
            # import 실패는 basic check에서 잡히므로 여기서는 계속 진행
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


def _evaluate_live_case(case: LiveCase, handled: bool, payload: dict[str, Any] | None) -> CheckResult:
    name = f"live: {case.query}"

    if case.require_handled and not handled:
        return _fail(name, "try_handle_nlq()가 False 반환")

    if not payload:
        return _fail(name, "payload 없음")

    action = str(payload.get("action") or payload.get("title") or "").strip()
    meta = payload.get("meta") or {}
    domain = str(meta.get("domain") or "").strip()
    summary_md = str(meta.get("summary_md") or "").strip()
    query_summary = str(
        meta.get("query_summary")
        or meta.get("condition")
        or ""
    ).strip()
    message = str(payload.get("message") or payload.get("data") or "").strip()
    row_count = meta.get("row_count")
    row_total = meta.get("row_count_total")

    if case.expected_action and case.expected_action not in action:
        return _fail(name, f"action expected contains {case.expected_action!r}, got {action!r}")

    if case.expected_domain and domain != case.expected_domain:
        return _fail(name, f"domain expected {case.expected_domain!r}, got {domain!r}")

    if case.require_summary:
        text_for_summary = summary_md or message

        has_query_condition = (
            "조회조건:" in text_for_summary
            or bool(query_summary)
        )

        if not has_query_condition:
            return _fail(
                name,
                (
                    "조회조건 누락: "
                    f"query_summary={query_summary!r}, "
                    f"summary_md={summary_md!r}, "
                    f"message={message[:80]!r}"
                ),
            )

    if case.expected_condition_tokens:
        condition_text = " ".join(
            str(x or "")
            for x in [
                query_summary,
                summary_md,
                message,
                str(payload.get("params") or ""),
            ]
        )

        missing_tokens = [
            token
            for token in case.expected_condition_tokens
            if str(token) not in condition_text
        ]

        if missing_tokens:
            return _fail(
                name,
                (
                    f"조회조건 토큰 누락: missing={missing_tokens!r}, "
                    f"query_summary={query_summary!r}, "
                    f"summary_md={summary_md[:160]!r}, "
                    f"message={message[:120]!r}"
                ),
            )

    cond_preview = query_summary or summary_md or message
    cond_preview = str(cond_preview or "").replace("\n", " ")
    if len(cond_preview) > 90:
        cond_preview = cond_preview[:90] + "..."

    detail = (
        f"action={action!r}, domain={domain!r}, "
        f"rows={row_count}/{row_total}, type={payload.get('type')!r}, "
        f"condition={cond_preview!r}"
    )

    return _ok(name, detail)


def _smoke_live_cases() -> list[LiveCase]:
    return [
        LiveCase("제품명 아티반 제품 조회", expected_domain="goods"),
        LiveCase("거래처명 경동 거래처 조회", expected_domain="vendors"),
        LiveCase("사용자명 김 사용자 조회", expected_domain="users"),
        LiveCase("코드종류명 부서 조회", expected_domain="codes"),
        LiveCase("도로명주소 강남대로 조회", expected_domain="road_address", expected_action="도로명주소 조회"),
        LiveCase("없는주소가나다빵 도로명주소 조회", expected_domain="road_address", expected_action="도로명주소 조회"),
        LiveCase(
            "도로명주소 강남대로 조회",
            expected_domain="road_address",
            expected_action="도로명주소 조회",
            expected_condition_tokens=("도로명주소", "강남대로"),
        ),
        LiveCase(
            "없는주소가나다빵 도로명주소 조회",
            expected_domain="road_address",
            expected_action="도로명주소 조회",
            expected_condition_tokens=("통합검색", "없는주소가나다빵"),
        ),


    ]


def _all_live_cases() -> list[LiveCase]:
    return [
        # 제품
        LiveCase("제품명 아티반 제품 조회", expected_domain="goods"),
        LiveCase("제조사명 일동 제품 조회", expected_domain="goods"),
        LiveCase("제품그룹명 일반 제품 조회", expected_domain="goods"),
        LiveCase("보험코드 6425 제품 조회", expected_domain="goods"),
        LiveCase("제품코드 00029 제품 조회", expected_domain="goods"),
        LiveCase("제품명완창가나다 제품 조회", expected_domain="goods"),
        LiveCase("제품 등록자 관리자 조회", expected_domain="goods"),
        LiveCase("제품 등록일자 2025 조회", expected_domain="goods"),
        LiveCase("제품 등록일자 202501 조회", expected_domain="goods"),
        LiveCase("제품 등록일자 20250101~20251231 조회", expected_domain="goods"),
        LiveCase("제품 등록일자 2025-01-01 ~ 2025-12-31 조회", expected_domain="goods"),
        LiveCase("제품 수정자 관리자 조회", expected_domain="goods"),
        LiveCase("제품 수정일자 2025 조회", expected_domain="goods"),
        LiveCase("제품 수정일자 202501 조회", expected_domain="goods"),
        LiveCase("제품 수정일자 20250101~20251231 조회", expected_domain="goods"),
        LiveCase("제품 수정일자 2025-01-01 ~ 2025-12-31 조회", expected_domain="goods"),

        # 거래처
        LiveCase("거래처명 경동 거래처 조회", expected_domain="vendors"),
        LiveCase("경동 거래처 조회", expected_domain="vendors"),
        LiveCase("대표자명 김 조회", expected_domain="vendors"),
        LiveCase("대표자명 김 거래처 조회", expected_domain="vendors"),
        LiveCase("재고적용처명 재고적용 거래처 조회", expected_domain="vendors"),
        LiveCase("단가적용처명 단가적용 거래처 조회", expected_domain="vendors"),
        LiveCase("거래처 매출처전체 조회", expected_domain="vendors"),
        LiveCase("거래처 매입처전체 조회", expected_domain="vendors"),
        LiveCase("거래처 매출처 조회", expected_domain="vendors"),
        LiveCase("거래처 매입처 조회", expected_domain="vendors"),
        LiveCase("거래처 회계매출처 조회", expected_domain="vendors"),
        LiveCase("거래처 회계매입처 조회", expected_domain="vendors"),
        LiveCase("거래처 수정일자 2025 조회", expected_domain="vendors"),
        LiveCase("거래처 등록일자 2025 조회", expected_domain="vendors"),
        LiveCase("거래처 수정일자 202501 조회", expected_domain="vendors"),
        LiveCase("거래처 수정일자 20250101~20251231 조회", expected_domain="vendors"),        
        LiveCase("거래처 수정일자 202501", expected_domain="vendors"),
        LiveCase("거래처 수정자 관리자 조회", expected_domain="vendors"),
        LiveCase("거래처 등록자 관리자 조회", expected_domain="vendors"),

        # 사용자
        LiveCase("사용자명 김 사용자 조회", expected_domain="users"),
        LiveCase("김 사용자 조회", expected_domain="users"),
        LiveCase("사용자ID admin 사용자 조회", expected_domain="users"),
        LiveCase("사번 001 사용자 조회", expected_domain="users"),
        LiveCase("부서명 영업 사용자 조회", expected_domain="users"),
        LiveCase("직책 사원 사용자 조회", expected_domain="users"),
        LiveCase("영업지역 병원 사용자 조회", expected_domain="users"),
        LiveCase("재고위치 본사 사용자 조회", expected_domain="users"),
        LiveCase("수정자 관리자 사용자 조회", expected_domain="users"),
        LiveCase("최근입사자 20250101 조회", expected_domain="users"),
        LiveCase("부서별 사용자 수", expected_domain="users", require_summary=False),
        LiveCase("사용자 등록자 관리자 조회", expected_domain="users"),
        LiveCase("사용자 등록일자 2025 조회", expected_domain="users"),
        LiveCase("사용자 등록일자 202501 조회", expected_domain="users"),
        LiveCase("사용자 등록일자 20250101~20251231 조회", expected_domain="users"),
        LiveCase("사용자 등록일자 2025-01-01 ~ 2025-12-31 조회", expected_domain="users"),
        LiveCase("사용자 수정자 관리자 조회", expected_domain="users"),
        LiveCase("사용자 수정일자 2025 조회", expected_domain="users"),
        LiveCase("사용자 수정일자 202501 조회", expected_domain="users"),
        LiveCase("사용자 수정일자 20250101~20251231 조회", expected_domain="users"),
        LiveCase("사용자 수정일자 2025-01-01 ~ 2025-12-31 조회", expected_domain="users"),


        # 업무코드
        LiveCase("업무코드 코드종류 조회", expected_domain="codes"),
        LiveCase("코드종류명 부서 조회", expected_domain="codes"),
        LiveCase("업무코드 코드종류명 부서 조회", expected_domain="codes"),
        LiveCase("업무코드 그룹코드 0005 조회", expected_domain="codes"),
        LiveCase("업무코드 상세코드 001 조회", expected_domain="codes"),
        LiveCase("업무코드 한글명 배송 조회", expected_domain="codes"),
        LiveCase("업무코드 수정자 관리자 조회", expected_domain="codes"),
        LiveCase("업무코드 수정일자 20250808 조회", expected_domain="codes"),
        LiveCase("업무코드 등록자 관리자 조회", expected_domain="codes"),
        LiveCase("업무코드 등록일자 2025 조회", expected_domain="codes"),
        LiveCase("업무코드 등록일자 202501 조회", expected_domain="codes"),
        LiveCase("업무코드 등록일자 20250101~20251231 조회", expected_domain="codes"),
        LiveCase("업무코드 등록일자 2025-01-01 ~ 2025-12-31 조회", expected_domain="codes"),
        LiveCase("업무코드 수정일자 2025 조회", expected_domain="codes"),
        LiveCase("업무코드 수정일자 202501 조회", expected_domain="codes"),
        LiveCase("업무코드 수정일자 20250101~20251231 조회", expected_domain="codes"),
        LiveCase("업무코드 수정일자 2025-01-01 ~ 2025-12-31 조회", expected_domain="codes"),
        LiveCase("업무코드 없는코드가나다 조회", expected_domain="codes"),

        # 도로명주소
        LiveCase(
            "도로명주소 강남대로 조회",
            expected_domain="road_address",
            expected_action="도로명주소 조회",
            expected_condition_tokens=("도로명주소", "강남대로"),
        ),
        LiveCase(
            "도로명 강남대로 조회",
            expected_domain="road_address",
            expected_action="도로명주소 조회",
            expected_condition_tokens=("도로명", "강남대로"),
        ),
        LiveCase(
            "시도명 서울 도로명주소 조회",
            expected_domain="road_address",
            expected_action="도로명주소 조회",
            expected_condition_tokens=("시도명", "서울"),
        ),
        LiveCase(
            "시구군명 강남구 도로명주소 조회",
            expected_domain="road_address",
            expected_action="도로명주소 조회",
            expected_condition_tokens=("시구군명", "강남구"),
        ),
        LiveCase(
            "법정동명 역삼 도로명주소 조회",
            expected_domain="road_address",
            expected_action="도로명주소 조회",
            expected_condition_tokens=("법정읍면동명", "역삼"),
        ),
        LiveCase(
            "법정읍면동명 역삼 도로명주소 조회",
            expected_domain="road_address",
            expected_action="도로명주소 조회",
            expected_condition_tokens=("법정읍면동명", "역삼"),
        ),
        LiveCase(
            "없는주소가나다빵 도로명주소 조회",
            expected_domain="road_address",
            expected_action="도로명주소 조회",
            expected_condition_tokens=("통합검색", "없는주소가나다빵"),
        ),        
    ]


def run_live_checks(*, live_all: bool = False, show_payload: bool = False) -> list[CheckResult]:
    results: list[CheckResult] = []

    try:
        router = importlib.import_module("app.sims.nlq.nlq_router")
        try_handle_nlq = getattr(router, "try_handle_nlq")
    except Exception as e:
        return [_fail("live import router.try_handle_nlq", f"{type(e).__name__}: {e}")]

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

            payload = None
            if len(capture.payloads) > before_count:
                payload = capture.pop_last()
            if payload is None:
                payload = _extract_payload_from_room(room)

            if show_payload and payload is not None:
                print()
                print(f"[PAYLOAD] {case.query}")
                print(f"  action={payload.get('action')!r}")
                print(f"  type={payload.get('type')!r}")
                print(f"  meta={payload.get('meta')!r}")

            results.append(_evaluate_live_case(case, handled, payload))

        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
            detail += "\n" + traceback.format_exc(limit=3)
            results.append(_fail(f"live: {case.query}", detail))

    return results


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="SIMS master NLQ regression checker")
    parser.add_argument(
        "--live",
        action="store_true",
        help="실제 try_handle_nlq 라우팅과 DB 조회까지 smoke test",
    )
    parser.add_argument(
        "--live-all",
        action="store_true",
        help="대표 NLQ 문장 전체를 실제 라우팅/DB 조회로 테스트",
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
    failed += _print_results("BASIC IMPORT / HELPER CHECKS", basic_results)

    if args.live or args.live_all:
        live_results = run_live_checks(
            live_all=bool(args.live_all),
            show_payload=bool(args.show_payload),
        )
        failed += _print_results("LIVE NLQ ROUTING CHECKS", live_results)

    print()
    if failed:
        print(f"RESULT: FAIL ({failed} failed)")
        return 1

    print("RESULT: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
