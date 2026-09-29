from __future__ import annotations

import logging
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.io_nlq import (
    _extract_unlabeled_entity_phrase,
    resolve_current_stock_entity_condition,
    resolve_io_nlq,
)
from app.services.product_master_filter_contract import (
    add_named_product_prescription_filter,
    add_named_product_prescription_code_filter,
    classify_product_prescription_semantic,
    extract_product_prescription_semantic,
    has_product_prescription_semantic_conflict,
    is_management_only_standard_code,
    is_valid_drug_standard_code,
    strip_product_prescription_semantic_terms,
)
from app.services.rddbc110_service import _base_filters as inbound_filters
from app.services.rddbc120_service import _base_filters as outbound_filters
from app.services import product_inventory_service
from app.sims.nlq.nlq_goods import _DI_LABEL_PATTERNS, _extract_labeled_keyword
from app.sims.nlq.nlq_router import _build_analytics_params, _resolve_analytics_action


def fail(message: str) -> None:
    raise AssertionError(message)


def parsed(query: str) -> dict:
    result = resolve_io_nlq(query)
    if not isinstance(result, dict):
        fail(f"not routed: {query}: {result!r}")
    return result


def assert_io(query: str, action: str, semantic: str) -> None:
    result = parsed(query)
    params = dict(result.get("params") or {})
    if result.get("action") != action:
        fail(f"action mismatch: {query}: {result!r}")
    if params.get("product_prescription_semantic") != semantic:
        fail(f"semantic mismatch: {query}: {params!r}")
    if params.get("product_di_semantic_group"):
        fail(f"semantic leaked to insurance axis: {query}: {params!r}")
    if params.get("nlq_unlabeled_name"):
        fail(f"semantic leaked to free-search residual: {query}: {params!r}")
    if action in {"입고명세 조회", "출고명세 조회"} and _extract_unlabeled_entity_phrase(query, action):
        fail(f"semantic remained in action residual: {query}")


def assert_sql_contract() -> None:
    for builder, prefix in ((inbound_filters, "in_"), (outbound_filters, "out_")):
        params = {"product_prescription_semantic": "prescription"}
        sql = builder(params)
        if "Rddbc046 AS PrescriptionStd" not in sql or "Rd046_Main_Standard_Cd" not in sql:
            fail(f"R046 authority missing: {prefix}: {sql}")
        if not any(key.startswith(prefix + "product_prescription") for key in params):
            fail(f"prescription binds missing: {prefix}: {params!r}")

    clauses: list[str] = []
    params = {"product_prescription_semantic": "otc"}
    add_named_product_prescription_filter(
        clauses,
        params,
        product_code_expression="P.Rd04_Physic_Cd",
        product_di_code_expression="P.Rd04_Physic_Di",
        bind_prefix="focused",
    )
    if not clauses or "Rddbc046" not in clauses[0] or "880%" not in clauses[0]:
        fail(f"shared R046 predicate missing: {clauses!r}")
    if sorted(value for key, value in params.items() if key.startswith("focused_")) != ["1", "5"]:
        fail(f"OTC code binds mismatch: {params!r}")

    code_clauses: list[str] = []
    code_params = {"product_prescription_semantic": "prescription"}
    add_named_product_prescription_code_filter(
        code_clauses,
        code_params,
        product_di_code_expression="P.Rd04_Physic_Di",
        bind_prefix="inventory_only",
    )
    if not code_clauses or "Rddbc046" in code_clauses[0]:
        fail(f"inventory code-only predicate contains R046: {code_clauses!r}")

    cfg = product_inventory_service._settings({"stock_mode": "real"})
    for semantic, expected_codes in (
        ("prescription", ["0", "2", "3", "6", "7"]),
        ("otc", ["1", "5"]),
    ):
        semantic_params = {
            "product_prescription_semantic": semantic,
            "date_from": "20260901", "date_to": "20260930",
            "month_from": "202609", "month_to": "202609",
        }
        if product_inventory_service._month_carry_requires_master_filter(semantic_params):
            fail(f"prescription semantic forced legacy month carry: {semantic}")
        carry_sql, carry_params = product_inventory_service._build_month_carry_sql(semantic_params, cfg)
        period_sql, period_params = product_inventory_service._build_month_period_sql(semantic_params, cfg)
        detail_results = [
            product_inventory_service._build_detail_sql(
                direction, semantic_params, cfg, start, end, bucket
            )
            for direction, start, end, bucket in (
                ("in", "20260801", "20260831", "carry"),
                ("out", "20260801", "20260831", "carry"),
                ("in", "20260901", "20260930", "period"),
                ("out", "20260901", "20260930", "period"),
            )
        ]
        branches = (
            ("month_carry", carry_sql, carry_params),
            ("month_period", period_sql, period_params),
            *[("detail", sql, sql_params) for sql, sql_params in detail_results],
        )
        if "WITH MonthAgg AS" not in carry_sql:
            fail(f"optimized MonthAgg shape missing: {semantic}")
        for label, sql, sql_params in branches:
            if "PrescriptionStd" in sql or "Rd046_Main_Standard_Cd" in sql:
                fail(f"inventory branch retained R046 validity predicate: {semantic}/{label}")
            if "Rd04_Physic_Di" not in sql:
                fail(f"inventory code predicate missing: {semantic}/{label}")
            actual_codes = sorted(
                value for key, value in sql_params.items()
                if "product_prescription" in key and key != "product_prescription_semantic"
            )
            if actual_codes != expected_codes:
                fail(f"inventory code binds mismatch: {semantic}/{label}: {actual_codes!r}")

    plain_params = {
        "date_from": "20260901", "date_to": "20260930",
        "month_from": "202609", "month_to": "202609",
    }
    plain_sql, plain_binds = product_inventory_service._build_month_carry_sql(plain_params, cfg)
    if "WITH MonthAgg AS" not in plain_sql or any("product_prescription" in key for key in plain_binds):
        fail("unfiltered month-carry SQL shape changed")


def assert_goods_router_contract() -> None:
    import app.sims.nlq.nlq_goods as nlq_goods

    calls: list[dict] = []
    delivered: list[dict] = []
    original_search = nlq_goods.search_goods_full
    original_push = nlq_goods.push_sims_result_to_chat

    def fake_search(**kwargs) -> pd.DataFrame:
        calls.append(dict(kwargs))
        return pd.DataFrame()

    nlq_goods.search_goods_full = fake_search
    nlq_goods.push_sims_result_to_chat = lambda payload, _action: delivered.append(payload)
    try:
        for query in ("전문약 제품조회", "전문약품 제품조회", "전문의약품 제품조회", "ETC 제품조회"):
            calls.clear()
            delivered.clear()
            handled = nlq_goods.try_handle_goods_nlq(
                query, room={"room_id": "fixture", "messages": []}, session_state={},
                make_ts=lambda: "fixture", next_seq=lambda: 1,
                logger=logging.getLogger("product-master-semantic-gate"),
            )
            if not handled or len(calls) != 1:
                fail(f"unlabelled product semantic was not routed: {query}")
            if calls[0].get("product_prescription_semantic") != "prescription" or calls[0].get("keyword"):
                fail(f"unlabelled product semantic became a name: {query}: {calls[0]!r}")

        labelled_cases = (
            ("제품구분: ETC 제품조회", "di_name_kw"),
            ("제품명: ETC 제품조회", "keyword"),
        )
        for query, expected_key in labelled_cases:
            calls.clear()
            nlq_goods.try_handle_goods_nlq(
                query, room={"room_id": "fixture", "messages": []}, session_state={},
                make_ts=lambda: "fixture", next_seq=lambda: 1,
                logger=logging.getLogger("product-master-semantic-gate"),
            )
            if len(calls) != 1 or calls[0].get(expected_key) != "ETC":
                fail(f"labelled ETC owner was not preserved: {query}: {calls!r}")
            if calls[0].get("product_prescription_semantic"):
                fail(f"labelled ETC became prescription semantic: {query}: {calls[0]!r}")

        calls.clear()
        delivered.clear()
        nlq_goods.try_handle_goods_nlq(
            "전문약 OTC 제품조회", room={"room_id": "fixture", "messages": []}, session_state={},
            make_ts=lambda: "fixture", next_seq=lambda: 1,
            logger=logging.getLogger("product-master-semantic-gate"),
        )
        if calls:
            fail(f"conflicting product semantic reached service: {calls!r}")
    finally:
        nlq_goods.search_goods_full = original_search
        nlq_goods.push_sims_result_to_chat = original_push


def assert_router_service_contract() -> None:
    import app.sims.nlq.nlq_router as nlq_router
    import app.ui.chat_middleware as chat_middleware

    calls: list[dict] = []
    delivered: list[dict] = []
    original_service = product_inventory_service.get_product_inventory_result
    original_defaults = nlq_router._apply_current_stock_defaults
    original_push = chat_middleware.push_sims_result_to_chat

    def fake_service(params: dict) -> dict:
        calls.append(dict(params or {}))
        frame = pd.DataFrame([{"제품명": "fixture", "재고수량": 1}])
        return {
            "final": True, "type": "table", "title": "현재고 조회", "action": "현재고 조회",
            "params": dict(params or {}), "data": frame, "df": frame, "df_display": frame,
            "meta": {"result_status": "success", "row_count": 1, "source_call_count": 1},
        }

    product_inventory_service.get_product_inventory_result = fake_service
    nlq_router._apply_current_stock_defaults = lambda params, *, session_state: {
        **dict(params or {}), "current_stock_query": True,
    }
    chat_middleware.push_sims_result_to_chat = lambda payload, _action: delivered.append(payload)
    try:
        for query, semantic in (("전문의약품 현재고", "prescription"), ("OTC 현재고", "otc"), ("현재고", "")):
            before = len(calls)
            handled = nlq_router._try_handle_io_nlq(
                query, room={"room_id": "fixture", "messages": []}, session_state={},
                make_ts=lambda: "fixture", next_seq=lambda: 1,
                logger=logging.getLogger("product-prescription-gate"),
            )
            if not handled or len(calls) != before + 1:
                fail(f"current-stock service was not called: {query}")
            if semantic and calls[-1].get("product_prescription_semantic") != semantic:
                fail(f"current-stock semantic mismatch: {query}: {calls[-1]!r}")
            if not semantic and calls[-1].get("product_prescription_semantic"):
                fail(f"bare current stock gained a semantic: {calls[-1]!r}")

        for conflict_query in ("전문약 OTC 현재고", "전문약과 일반약 현재고"):
            before = len(calls)
            delivered.clear()
            nlq_router._try_handle_io_nlq(
                conflict_query, room={"room_id": "fixture", "messages": []}, session_state={},
                make_ts=lambda: "fixture", next_seq=lambda: 1,
                logger=logging.getLogger("product-prescription-gate"),
            )
            meta = dict((delivered[0].get("meta") if delivered else {}) or {})
            if len(calls) != before or not meta.get("service_call_skipped"):
                fail(f"conflict reached current-stock service: {conflict_query}: {meta!r}")
    finally:
        product_inventory_service.get_product_inventory_result = original_service
        nlq_router._apply_current_stock_defaults = original_defaults
        chat_middleware.push_sims_result_to_chat = original_push


def main() -> None:
    prescription = ("전문약", "전문약품", "전문의약품", "ETC", "etc", "Etc")
    otc = ("일반약", "일반약품", "일반의약품", "OTC", "otc", "Otc")
    for alias in prescription:
        if extract_product_prescription_semantic(alias) != "prescription":
            fail(f"prescription alias mismatch: {alias}")
    for alias in otc:
        if extract_product_prescription_semantic(alias) != "otc":
            fail(f"OTC alias mismatch: {alias}")

    for alias, semantic in (
        *[(value, "prescription") for value in prescription],
        *[(value, "otc") for value in otc],
    ):
        for suffix, action in (
            ("입고명세", "입고명세 조회"), ("출고명세", "출고명세 조회"),
            ("발주현황", "발주조회"), ("입고예정조회", "입고예정조회"),
        ):
            assert_io(f"{alias} {suffix}", action, semantic)

    period = parsed("전문의약품 입고명세 202609")
    params = dict(period.get("params") or {})
    if (params.get("date_from"), params.get("date_to")) != ("20260901", "20260930"):
        fail(f"period parsing regression: {period!r}")

    for query in (
        "제품구분 보험(전문) 제품조회", "제품구분 전문 제품조회", "제품구분 ETC 제품조회",
        "제품구분: ETC 제품조회", "제품구분=전문약 제품조회", "구분명: OTC 제품조회",
    ):
        if extract_product_prescription_semantic(query):
            fail(f"label owner was rewritten as semantic: {query}")
        if not _extract_labeled_keyword(query, _DI_LABEL_PATTERNS):
            fail(f"labelled product division was not preserved: {query}")

    labelled_product = parsed("제품명: ETC 입고명세")
    labelled_params = dict(labelled_product.get("params") or {})
    if labelled_params.get("product_prescription_semantic") or labelled_params.get("physic_nm") != "ETC":
        fail(f"labelled product name was rewritten as semantic: {labelled_product!r}")

    particle_cases = (
        ("전문약은 현재고", "현재고 조회", "prescription"),
        ("일반약도 현재고", "현재고 조회", "otc"),
        ("ETC는 입고명세", "입고명세 조회", "prescription"),
        ("OTC의 출고명세", "출고명세 조회", "otc"),
    )
    for query, action, semantic in particle_cases:
        assert_io(query, action, semantic)

    for entity in ("MYETC제품", "OTC케어", "제조사ETC코리아"):
        query = f"{entity} 입고명세"
        if extract_product_prescription_semantic(query):
            fail(f"embedded token misclassified: {query}")
        if entity not in strip_product_prescription_semantic_terms(query):
            fail(f"embedded token removed: {query}")

    if not has_product_prescription_semantic_conflict("전문약 OTC 입고명세"):
        fail("prescription/OTC conflict was not detected")
    if not has_product_prescription_semantic_conflict("전문약과 일반약 현재고"):
        fail("particle-linked prescription/OTC conflict was not detected")
    duplicate = parsed("전문약 ETC 입고명세")
    if (duplicate.get("params") or {}).get("product_prescription_semantic") != "prescription":
        fail(f"same-semantic duplicate mismatch: {duplicate!r}")

    valid = "8801234567890"
    invalid = "9901234567890"
    if not is_valid_drug_standard_code(valid) or is_valid_drug_standard_code(invalid):
        fail("standard-code validity boundary mismatch")
    if classify_product_prescription_semantic("3", valid, valid) != "prescription":
        fail("professional product classification mismatch")
    if classify_product_prescription_semantic("1", valid, valid) != "otc":
        fail("OTC product classification mismatch")
    if classify_product_prescription_semantic("3", invalid, valid):
        fail("invalid standard code was classified")
    if classify_product_prescription_semantic("3", valid, invalid):
        fail("invalid main standard code was classified")
    if not is_management_only_standard_code(valid, valid):
        fail("management-only standard code was not detected")

    resolution = resolve_current_stock_entity_condition("현재고", params={})
    if resolution.get("status") != "resolved" or resolution.get("resolved_kind") != "unfiltered_current_stock":
        fail(f"bare current-stock contract mismatch: {resolution!r}")

    analytics_cases = (
        ("전문약 품목별 재고부족현황", "품목별 재고부족현황", "prescription"),
        ("ETC 품목별부족현황", "품목별 재고부족현황", "prescription"),
        ("OTC 품목별 매출추세분석", "품목별 매출 추세 분석", "otc"),
        ("일반약 제약사별 매출추세분석", "제약사별 매출 추세 분석", "otc"),
        ("ETC 품목별 매출예상", "품목별 매출 예상", "prescription"),
    )
    for query, expected_action, semantic in analytics_cases:
        action = _resolve_analytics_action(query)
        analytics_params = _build_analytics_params(query, action or "")
        if action != expected_action or analytics_params.get("product_prescription_semantic") != semantic:
            fail(f"analytics semantic mismatch: {query}: {action}/{analytics_params!r}")
        if analytics_params.get("product_di_semantic_group"):
            fail(f"analytics semantic leaked to insurance axis: {query}: {analytics_params!r}")

    assert_sql_contract()
    assert_goods_router_contract()
    assert_router_service_contract()
    print("PASS: prescription/OTC semantic, optimized inventory scope, product-master routing")


if __name__ == "__main__":
    main()
