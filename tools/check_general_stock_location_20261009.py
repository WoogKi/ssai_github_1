"""Offline current-stock and product-inventory location scope regression."""

from __future__ import annotations

import logging
from pathlib import Path
import sys
from unittest.mock import patch

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    from app.services import io_nlq, product_inventory_service, ssai_analysis_profile_service
    from app.services import rddbc030_service
    from app.db import mssql_client
    from app.sims.nlq import nlq_router
    from app.ui import chat_middleware, ssai_login

    names = {"00001": ".본사 창고", "00247": "(전주)창고", "00901": ".오토스토어 대기창고", "00902": ".전주_반품대기", "00004": "기타창고"}
    profiles = {4: {"stock_cd_list": ["00001", "00247", "00901"]}, 8: {"stock_cd_list": ["00111"]}}
    queries: list[dict] = []
    deliveries: list[dict] = []
    resolved_queries: list[tuple[str, str]] = []

    def resolve_entity(maker_phrase: str = "", product_phrase: str = "") -> dict:
        resolved_queries.append((maker_phrase, product_phrase))
        return {
            "maker_rows": [{"match_type": "manufacturer", "match_code": "M001", "match_value": "동제"}]
            if maker_phrase == "동제" else [],
            "product_rows": [{"match_type": "product", "match_code": "P001", "match_value": "아스피린"}]
            if product_phrase == "아스피린" else [],
            "maker_elapsed_ms": 0, "product_elapsed_ms": 0, "errors": [],
        }

    def inventory_result(params):
        queries.append(dict(params))
        return {"final": True, "type": "text", "data": "fixture", "message": "fixture", "meta": {"result_status": "success", "row_count": 1}}

    def run(question: str, company: int = 4):
        queries.clear()
        deliveries.clear()
        with (
            patch.object(ssai_login, "get_selected_company", return_value={"company_id": company}),
            patch.object(ssai_analysis_profile_service, "load_dashboard_profile", side_effect=lambda *, company_id: profiles[company_id]),
            patch.object(io_nlq, "get_current_stock_location_name_map", return_value=names if company == 4 else {"00111": "별도창고"}),
            patch.object(io_nlq, "_resolve_current_stock_code_sets", side_effect=resolve_entity),
            patch.object(rddbc030_service, "search_rows", side_effect=AssertionError("unexpected DB lookup")),
            patch.object(mssql_client, "_get_engine", side_effect=AssertionError("DB connection forbidden")),
            patch.object(product_inventory_service, "get_product_inventory_result", side_effect=inventory_result),
            patch.object(chat_middleware, "push_sims_result_to_chat", side_effect=lambda payload, _action: deliveries.append(payload)),
        ):
            assert nlq_router._try_handle_io_nlq(
                question, room={}, session_state={}, logger=logging.getLogger(__name__),
                make_ts=lambda: "fixture", next_seq=lambda: 1,
            )
        assert len(deliveries) == 1, question
        return queries[:], deliveries[0]

    for action in ("현재고", "제품재고장"):
        for suffix, expected in (
            ("", ["00001", "00247", "00901"]),
            (" 재고위치 00247", ["00247"]),
            (" 재고위치 (전주)", ["00247"]),
            (" 재고위치 본사, 오토", ["00001", "00901"]),
            (" 재고위치 00001/00901", ["00001", "00901"]),
            (" 재고위치 본사 + 오토", ["00001", "00901"]),
            (" 재고위치 본사와 오토", ["00001", "00901"]),
            (" 재고위치 00001, 오토", ["00001", "00901"]),
            (" 재고위치 00004", ["00004"]),
        ):
            calls, delivered = run(action + suffix)
            assert len(calls) == 1, (action, suffix, delivered)
            assert calls[0]["stock_cds"] == expected, (action, suffix, calls[0])
            assert not calls[0].get("stock_nm"), (action, suffix, calls[0])
        for suffix in (
            " 재고위치 00001,99999", " 재고위치 본사, 미등록",
        ):
            calls, delivered = run(action + suffix)
            assert not calls and delivered["meta"]["result_status"] == "input_required", (action, suffix, delivered)
            assert delivered["meta"]["source_call_count"] == 0

        calls, delivered = run(action + " 재고위치 전체창고")
        expected = ["00001", "00247", "00901"] if action == "현재고" else []
        assert len(calls) == 1 and calls[0]["stock_cds"] == expected, (action, calls, delivered)
        assert not calls[0].get("nlq_unlabeled_name") and not calls[0].get("physic_nm"), calls[0]

        calls, delivered = run(action + " 재고위치 전체 창고")
        assert len(calls) == 1 and calls[0]["stock_cds"] == expected, (action, calls, delivered)
        assert not calls[0].get("nlq_unlabeled_name") and not calls[0].get("physic_nm"), calls[0]

        calls, delivered = run(action + " 재고위치 (전주) 아스피린")
        assert len(calls) == 1 and calls[0]["stock_cds"] == ["00247"], (action, calls, delivered)
        assert calls[0].get("physic_nm") == "아스피린", (action, calls[0])

        calls, delivered = run(action + " 재고위치 00001 아스피린")
        assert len(calls) == 1 and calls[0]["stock_cds"] == ["00001"], (action, calls, delivered)
        assert calls[0].get("physic_nm") == "아스피린", (action, calls[0])

        calls, delivered = run(action + " 재고위치 (전주) 제품명 아스피린")
        assert len(calls) == 1 and calls[0]["stock_cds"] == ["00247"], (action, calls, delivered)
        assert calls[0].get("physic_nm") == "아스피린", (action, calls[0])

        calls, delivered = run(action + " 재고위치 (전주) 제조사명 동제")
        assert len(calls) == 1 and calls[0]["stock_cds"] == ["00247"], (action, calls, delivered)
        assert calls[0].get("maker_nm") == "동제", (action, calls[0])

        calls, delivered = run(action + " 재고위치 (전주) 아스피린 제조사명 동제")
        assert len(calls) == 1 and calls[0]["stock_cds"] == ["00247"], (action, calls, delivered)
        assert calls[0].get("physic_nm") == "아스피린" and calls[0].get("maker_nm") == "동제", calls[0]
        if action == "현재고":
            assert resolved_queries[-1] == ("동제", "아스피린"), resolved_queries[-1]

        calls, delivered = run(action + " 재고위치 (전주) 아스피린 제품명 아스피린")
        assert len(calls) == 1 and calls[0]["stock_cds"] == ["00247"], (action, calls, delivered)
        assert calls[0].get("physic_nm") == "아스피린", calls[0]

        calls, delivered = run(action + " 재고위치 (전주) 아스피린 제품명 타이레놀")
        assert not calls and delivered["meta"]["result_status"] == "input_required", (action, calls, delivered)
        assert delivered["meta"]["source_call_count"] == 0

        calls, delivered = run(action + " 재고위치 00001 아스피린 제품명 타이레놀")
        assert not calls and delivered["meta"]["result_status"] == "input_required", (action, calls, delivered)
        assert delivered["meta"]["source_call_count"] == 0

        calls, delivered = run(action + " 재고위치 전주")
        assert not calls and delivered["meta"]["result_status"] == "input_required"
        assert delivered["meta"]["stock_scope_reason"] == "location_name_ambiguous"
        assert "00247" in str(delivered.get("message")) and "00902" in str(delivered.get("message"))
        assert "00004" not in str(delivered.get("message"))

        calls, delivered = run(action + " 재고위치 정주")
        assert not calls and delivered["meta"]["result_status"] == "input_required"
        assert "선택 가능한 위치:" not in str(delivered.get("message")), delivered

    calls, delivered = run("현재고 재고위치 00001", company=8)
    assert not calls and delivered["meta"]["result_status"] == "input_required"
    for action in ("현재고", "제품재고장"):
        calls, _ = run(action + " 전체창고")
        expected = ["00001", "00247", "00901"] if action == "현재고" else []
        assert len(calls) == 1 and calls[0]["stock_cds"] == expected, (action, calls)

    selected, error, candidates = io_nlq.resolve_stock_location_codes(
        [], ["본사"], allowed_codes=["00001", "00002"],
        location_names={"00001": "본사 1창고", "00002": "본사 2창고"},
    )
    assert not selected and error == "location_name_ambiguous" and candidates == ["00001", "00002"]

    with patch.object(product_inventory_service, "query_to_df", return_value=pd.DataFrame({"stock_cd": ["00001"]})):
        assert product_inventory_service._registered_stock_codes(["00001", "99999"]) == ["00001"]
        with patch.object(product_inventory_service, "_collect_source_df", side_effect=AssertionError("main ERP query")):
            result = product_inventory_service.get_product_inventory_result({"stock_cds": ["00001", "99999"]})
        assert result["meta"]["result_status"] == "input_required"
    choices = pd.DataFrame({
        "stock_cd": ["00001", "00002"],
        "stock_nm": [".본사 창고", ".본사 보관창고"],
    })
    with patch.object(product_inventory_service, "query_to_df", return_value=choices):
        assert product_inventory_service._resolve_stock_codes({"stock_nm": "본사 창고"}) == ["00001"]
        assert product_inventory_service._resolve_stock_codes({"stock_nm": "본사"}) == []
    print("PASS general stock location router/registration fixture; DB connections=0")


if __name__ == "__main__":
    main()
