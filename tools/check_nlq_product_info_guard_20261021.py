"""Offline guard for product-information conditions that are not applied."""

from __future__ import annotations

from datetime import date
import logging
from pathlib import Path
import sys
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.io_nlq import resolve_io_nlq
from app.services.snapshot_product_information_service import get_snapshot_product_information_result
from app.sims.nlq import nlq_router


def _forbidden(*args, **kwargs):
    raise AssertionError("Snapshot or ERP accessed")


def _guarded_service(params):
    with patch("app.services.snapshot_product_information_service._company_id", return_value=8):
        return get_snapshot_product_information_result(
            params, profile_resolver=_forbidden, projection_reader=_forbidden,
            master_loader=_forbidden,
        )


def main() -> None:
    questions = (
        ("제품정보 등록자 오준혁", "등록자 오준혁"),
        ("제품정보 등록자명 오준혁", "등록자 오준혁"),
        ("제품정보 수정자 오준혁", "수정자 오준혁"),
        ("제품정보 수정자명 오준혁", "수정자 오준혁"),
        ("제품정보 등록일 202610", "등록일 202610"),
        ("제품정보 수정일 202610", "수정일 202610"),
        ("제품정보 202610", "조회기간 시작 202610"),
    )
    for question, condition in questions:
        parsed = resolve_io_nlq(question, today=date(2026, 10, 21))
        assert parsed and parsed["action"] == "제품정보 조회", (question, parsed)
        payload = _guarded_service(parsed["params"])
        meta = payload["meta"]
        assert meta["result_status"] == "input_required", (question, meta)
        assert meta["source_call_count"] == meta["snapshot_read_call_count"] == 0
        assert any(condition in item for item in meta["unsupported_conditions"]), (question, meta)
        assert condition in payload["message"]

    delivered = []
    with (
        patch("app.services.snapshot_product_information_service.get_snapshot_product_information_result",
              side_effect=_guarded_service),
        patch("app.ui.chat_middleware.push_sims_result_to_chat",
              side_effect=lambda payload, _: delivered.append(payload)),
        patch("app.db.mssql_client.get_conn", side_effect=_forbidden) as db,
    ):
        handled = nlq_router._try_handle_io_nlq(
            "제품정보 등록자 오준혁", room={"messages": []}, session_state={},
            make_ts=lambda: "fixture", next_seq=lambda: 1, logger=logging.getLogger(__name__),
        )
    assert handled and len(delivered) == 1, delivered
    assert delivered[0]["meta"]["result_status"] == "input_required"
    assert delivered[0]["meta"]["source_call_count"] == 0
    assert delivered[0]["meta"]["snapshot_read_call_count"] == 0
    assert db.call_count == 0

    for question, key, value in (
        ("제품정보 아스피린", "_product_information_unlabeled_name", "아스피린"),
        ("제품정보 제품명 이가탄", "physic_nm", "이가탄"),
        ("제품정보 제약사 한미", "maker_nm", "한미"),
        ("제품정보 발주처 담당자 윤상민", "order_vendor_manager_nm", "윤상민"),
        ("제품정보 제품명 등록자 오준혁", "physic_nm", "등록자 오준혁"),
    ):
        parsed = resolve_io_nlq(question, today=date(2026, 10, 21))
        assert parsed and parsed["params"].get(key) == value, (question, parsed)
        assert "add_nm" not in parsed["params"] or key != "physic_nm" or value != "등록자 오준혁"

    for key, value in (
        ("product_add_user_nm", "오준혁"), ("product_mod_user_nm", "오준혁"),
        ("product_add_date_from", "20261001"), ("product_mod_date_to", "20261021"),
        ("product_unit_price", "1000"), ("product_final_price_date", "202610"),
        ("date_from", "20261001"),
    ):
        meta = _guarded_service({key: value})["meta"]
        assert meta["result_status"] == "input_required" and meta["source_call_count"] == 0
        assert meta["snapshot_read_call_count"] == 0

    from app.sims.nlq.nlq_goods import _extract_add_user_name
    assert _extract_add_user_name("제품조회 등록자 오준혁") == "오준혁"
    print("PASS product-info guard: parsed conditions blocked before Snapshot/ERP, valid filters retained")


if __name__ == "__main__":
    main()
