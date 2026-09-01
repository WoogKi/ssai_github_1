# -*- coding: utf-8 -*-
"""NLQ 사례집 new_query REVIEW 19건의 DB 없는 라우팅/계약 점검."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _route_case(router, query: str, expected: str) -> tuple[bool, str]:
    calls: list[str] = []

    def mark(name: str, handled: bool = False):
        def _handler(*_args, **_kwargs):
            calls.append(name)
            return handled
        return _handler

    with (
        patch.object(router, "_try_handle_dashboard_nlq", mark("dashboard")),
        patch.object(router, "_try_handle_analytics_nlq", mark("analytics")),
        patch.object(router, "_try_handle_road_address_nlq", mark("road")),
        patch.object(router, "_try_handle_io_nlq", mark("io", expected == "io")),
        patch("app.sims.nlq.nlq_codes.try_handle_codes_nlq", mark("codes", expected == "codes")),
        patch("app.sims.nlq.nlq_goods.try_handle_goods_nlq", mark("goods", expected == "goods")),
        patch("app.sims.nlq.nlq_vendors.try_handle_vendors_nlq", mark("vendors", expected == "vendors")),
        patch("app.sims.nlq.nlq_users.try_handle_users_nlq", mark("users", expected == "users")),
    ):
        handled = router.try_handle_nlq(
            query, room={}, session_state={}, make_ts=lambda: "fixture", next_seq=lambda: 1, logger=None
        )
    return handled and expected in calls, ",".join(calls)


def main() -> int:
    router = importlib.import_module("app.sims.nlq.nlq_router")
    io_nlq = importlib.import_module("app.services.io_nlq")
    failures: list[str] = []
    passed = 0

    # 2건: 현재고의 무라벨 제조사/제품 entity를 실제 resolver로 확인한다.
    def stock_code_fixture(*, maker_phrase: str = "", product_phrase: str = ""):
        return {
            "maker_rows": ([{"match_type": "manufacturer", "match_code": "10001", "match_value": "삼진제약"}]
                           if maker_phrase == "삼진" else []),
            "product_rows": ([{"match_type": "product", "match_code": "00001", "match_value": "아스피린프로텍트정"}]
                             if product_phrase == "아스피린프로텍트정" else []),
            "maker_elapsed_ms": 0.0,
            "product_elapsed_ms": 0.0,
            "errors": [],
        }
    with (
        patch.object(io_nlq, "get_current_stock_location_name_map", return_value={}),
        patch.object(io_nlq, "_resolve_current_stock_code_sets", side_effect=stock_code_fixture),
    ):
        stock_cases = (("현재고 아스피린프로텍트정", "physic_nm"), ("현재고 삼진", "maker_nm"))
        for query, required_key in stock_cases:
            parsed = io_nlq.resolve_io_nlq(query) or {}
            entity = io_nlq.resolve_current_stock_entity_condition(query, params=dict(parsed.get("params") or {}))
            params = dict(entity.get("params") or {})
            ok = parsed.get("action") == "현재고 조회" and entity.get("status") == "resolved" and bool(params.get(required_key))
            if ok:
                passed += 1
                print(f"[PASS] {query}: current_stock / {required_key}={params.get(required_key)}")
            else:
                failures.append(f"{query}: parsed={parsed!r}, entity={entity!r}")

    route_cases = [
        ("업무코드 코드종류 조회", "codes"),
        ("업무코드 한글명 배송 조회", "codes"),
        ("업무코드 본사 창고 조회", "codes"),
        ("사용자조회", "users"),
        ("사용자 김 조회", "users"),
        ("사용자 등록자 관리자 조회", "users"),
        ("제품코드목록", "goods"),
        ("제품코드조회 제약사 삼진제약 제품그룹 전문", "goods"),
        ("제품코드조회", "goods"),
        ("매입처 조회", "vendors"),
        ("매입처 삼진 조회", "vendors"),
        ("매출처 조회", "vendors"),
        ("매출처 소망 조회", "vendors"),
        ("제약사 조회", "vendors"),
        ("제약사 한미 조회", "vendors"),
        ("거래처코드조회", "vendors"),
    ]
    for query, expected in route_cases:
        ok, calls = _route_case(router, query, expected)
        if ok:
            passed += 1
            print(f"[PASS] {query}: route={expected}; calls={calls}")
        else:
            failures.append(f"{query}: expected={expected}, calls={calls}")

    # 공통 IO action 정규화의 단일 오타 보정은 정상 표기와 같은 action이어야 한다.
    canonical = io_nlq.resolve_io_nlq("삼진제약 매입현황") or {}
    typo = io_nlq.resolve_io_nlq("삼진제약 메입현황") or {}
    if canonical.get("action") == typo.get("action") == "입고명세 조회":
        passed += 1
        print("[PASS] 삼진제약 메입현황: 메입 -> 매입 -> 입고명세 조회")
    else:
        failures.append(f"매입/메입 parser boundary unexpected: canonical={canonical!r}, typo={typo!r}")

    print(f"RESULT: PASS={passed} REVIEW=0 FAIL={len(failures)}")
    for failure in failures:
        print(f"[FAIL] {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
