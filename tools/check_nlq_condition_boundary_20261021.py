"""DB-free regression for current-stock typo and product-information labels."""

from __future__ import annotations

from datetime import date
import logging
from pathlib import Path
import sys
from unittest.mock import patch

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.io_nlq import resolve_io_nlq
from app.sims.nlq import nlq_router


def check_stock() -> None:
    from app.services import io_nlq, product_inventory_service, ssai_analysis_profile_service
    from app.db import mssql_client
    from app.ui import chat_middleware, ssai_login

    names = {"00001": ".본사 창고", "00247": "(전주)창고", "00901": ".오토스토어 대기창고"}
    profile = {"stock_cd_list": list(names)}
    calls: list[dict] = []
    delivered: list[dict] = []

    def service(params):
        calls.append(dict(params))
        return {"final": True, "type": "text", "data": "fixture", "message": "fixture",
                "meta": {"result_status": "success", "row_count": 1, "source_call_count": 1}}

    def run(question):
        calls.clear()
        delivered.clear()
        parsed = resolve_io_nlq(question, today=date(2026, 10, 21))
        with (
            patch.object(ssai_login, "get_selected_company", return_value={"company_id": 4}),
            patch.object(ssai_analysis_profile_service, "load_dashboard_profile", return_value=profile),
            patch.object(io_nlq, "get_current_stock_location_name_map", return_value=names),
            patch.object(product_inventory_service, "get_product_inventory_result", side_effect=service),
            patch.object(chat_middleware, "push_sims_result_to_chat",
                         side_effect=lambda payload, _: delivered.append(payload)),
            patch.object(mssql_client, "_get_engine", side_effect=AssertionError("DB called")) as db,
        ):
            handled = nlq_router._try_handle_io_nlq(
                question, room={}, session_state={}, make_ts=lambda: "fixture", next_seq=lambda: 1,
                logger=logging.getLogger(__name__),
            )
        assert handled and len(delivered) == 1 and db.call_count == 0, question
        return parsed, calls[:], delivered[0]

    left = run("현재고 제고위치 오토스토어")
    right = run("현재고 재고위치 오토스토어")
    assert left[0] == right[0]
    assert len(left[1]) == len(right[1]) == 1
    assert left[1][0]["stock_cds"] == right[1][0]["stock_cds"] == ["00901"]
    direct = run("현재고 재고위치코드 00901")
    assert len(direct[1]) == 1 and direct[1][0]["stock_cds"] == ["00901"]
    missing = run("현재고 제고위치 존재하지않는위치")
    assert not missing[1] and missing[2]["meta"]["result_status"] == "input_required"
    assert missing[2]["meta"]["source_call_count"] == 0
    multiple = run("현재고 재고위치 본사, 오토")
    assert len(multiple[1]) == 1 and multiple[1][0]["stock_cds"] == ["00001", "00901"]
    named_product = resolve_io_nlq("현재고 제품명 제고위치 오토스토어")
    assert named_product["params"]["physic_nm"] == "제고위치 오토스토어"
    assert not named_product["params"].get("stock_nm")
    assert resolve_io_nlq("제품정보 제품명 제고위치 오토스토어")["params"]["physic_nm"] == "제고위치 오토스토어"


def check_product_information() -> None:
    from app.services import snapshot_product_information_service as service
    from app.ui import chat_middleware

    def forbidden(*args, **kwargs):
        raise AssertionError("Snapshot or ERP called")

    blocked = (
        ("제품정보 제품명 이가탄 등록자 오준혁", "add_nm"),
        ("제품정보 제품명 이가탄 수정자 오준혁", "mod_nm"),
        ("제품정보 등록자 오준혁", "add_nm"),
        ("제품정보 수정자 오준혁", "mod_nm"),
    )
    for question, key in blocked:
        parsed = resolve_io_nlq(question, today=date(2026, 10, 21))
        assert parsed["action"] == "제품정보 조회" and parsed["params"][key] == "오준혁", parsed
        if "제품명 이가탄" in question:
            assert parsed["params"]["physic_nm"] == "이가탄", parsed
        with patch.object(service, "_company_id", return_value=8):
            payload = service.get_snapshot_product_information_result(
                parsed["params"], profile_resolver=forbidden,
                projection_reader=forbidden, master_loader=forbidden,
            )
        meta = payload["meta"]
        assert meta["result_status"] == "input_required"
        assert meta["source_call_count"] == meta["snapshot_read_call_count"] == 0
        assert "오준혁" in payload["message"]

    delivered = []
    with (
        patch.object(service, "_company_id", return_value=8),
        patch.object(service, "resolve_dashboard_profile_stock_scope", side_effect=forbidden),
        patch.object(service, "read_approved_frequency_projection", side_effect=forbidden),
        patch.object(service, "load_product_information_master", side_effect=forbidden),
        patch.object(chat_middleware, "push_sims_result_to_chat",
                     side_effect=lambda payload, _: delivered.append(payload)),
    ):
        assert nlq_router._try_handle_io_nlq(
            "제품정보 제품명 이가탄 등록자 오준혁", room={}, session_state={},
            make_ts=lambda: "fixture", next_seq=lambda: 1, logger=logging.getLogger(__name__),
        )
    assert len(delivered) == 1 and delivered[0]["meta"]["result_status"] == "input_required"
    assert delivered[0]["meta"]["source_call_count"] == 0
    assert delivered[0]["meta"]["snapshot_read_call_count"] == 0

    normal = (
        ("제품정보 제품명 등록자 오준혁", "physic_nm", "등록자 오준혁"),
        ("제품정보 아스피린", "_product_information_unlabeled_name", "아스피린"),
        ("제품정보 제품명 이가탄", "physic_nm", "이가탄"),
        ("제품정보 제약사 한미", "maker_nm", "한미"),
        ("제품정보 발주처 담당자 윤상민", "order_vendor_manager_nm", "윤상민"),
    )
    for question, key, value in normal:
        parsed = resolve_io_nlq(question, today=date(2026, 10, 21))
        assert parsed["action"] == "제품정보 조회" and parsed["params"].get(key) == value, parsed
        assert not parsed["params"].get("add_nm") and not parsed["params"].get("mod_nm"), parsed
    from app.sims.nlq.nlq_goods import _extract_add_user_name
    assert _extract_add_user_name("제품조회 등록자 오준혁") == "오준혁"


if __name__ == "__main__":
    check_stock()
    check_product_information()
    print("PASS NLQ condition boundaries: stock typo, product name/audit split; DB calls=0")
