"""Offline product-information visibility, terminology, routing, and order-default gate."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys
import logging

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot import FrequencyProjectionReadResult
from app.services.erp_table_nlq import resolve_registered_erp_table_nlq
from app.services.io_nlq import resolve_io_nlq
from app.services.order_calculation_service import apply_application_defaults
from app.services.snapshot_product_information_service import (
    SENSITIVE_PRODUCT_INFORMATION_COLUMNS,
    get_snapshot_product_information_result,
    project_product_information_frame_for_viewer,
    project_product_information_payload_for_viewer,
)
from app.ui.chat_middleware import _normalize_result_for_chat
from app.ui.current_table_followups.action_dispatcher import handle_current_table_followup_by_action
from app.ui.sims_analysis_profiles import snapshot_grade_analysis_contract
from app.sims.nlq import nlq_router


MANAGEMENT = SimpleNamespace(user_type="SSART_USER")
MEMBER = SimpleNamespace(user_type="WHOLESALE_USER")


def _scope(**_kwargs):
    return SimpleNamespace(
        stock_codes=("00001",),
        product_group_codes=(),
        product_di_codes=(),
        product_class_codes=(),
        stock_mode="real",
    )


def _projection(**_kwargs):
    rows = tuple(
        {
            "product_code": f"{index:05d}",
            "frequency_grade": "A",
            "profit_grade": "B",
            "contribution_grade": "C",
            "lifecycle_status": "established_product",
            "estimated_unit_profit": 10 + index,
            "estimated_profit_rate": 0.1,
            "estimated_contribution_amount": 100 + index,
        }
        for index in range(1, 4)
    )
    return FrequencyProjectionReadResult(
        status="ready",
        rows=rows,
        manifest_id=1,
        generation_no=1,
        checksum="a" * 64,
        authority_status="ready",
        resolution_status="exact_match",
        contract_version="2.1",
    )


def _master(params):
    name = str(params.get("_product_information_unlabeled_name") or "")
    maker = "베링거" if name == "베링거" or params.get("maker_nm") == "베링거" else "검증제약"
    return pd.DataFrame(
        [
            {"제품코드": f"{index:05d}", "제품명": f"검증제품{index}", "제약사": maker, "규격": "10정"}
            for index in range(1, 4)
        ]
    )


def _query(viewer, params=None):
    with patch("app.services.snapshot_product_information_service.get_current_company_id", return_value=7):
        return get_snapshot_product_information_result(
            {"company_id": 7, **dict(params or {})},
            profile_resolver=_scope,
            projection_reader=_projection,
            master_loader=_master,
            viewer_user=viewer,
        )


def test_visibility_projection() -> None:
    management = _query(MANAGEMENT)
    member = _query(MEMBER)
    for column in SENSITIVE_PRODUCT_INFORMATION_COLUMNS:
        assert column in management["df"].columns
        assert column not in member["df"].columns
        assert column not in member["df_display"].columns
        assert column not in member["columns"]
        assert all(column not in row for row in member["records"])
        assert column not in str(member["meta"].get("llm_summary_md") or "")
    assert {"품목손익등급", "품목기여등급"}.issubset(member["df"].columns)

    old_payload = {
        "action": "제품정보 조회",
        "title": "제품정보 조회",
        "df": management["df"],
        "df_display": management["df_display"],
        "records": management["records"],
        "columns": management["columns"],
        "meta": management["meta"],
    }
    restored = project_product_information_payload_for_viewer(old_payload, viewer_user=MEMBER)
    assert not set(SENSITIVE_PRODUCT_INFORMATION_COLUMNS).intersection(restored["df"].columns)
    restored_frame = project_product_information_frame_for_viewer(
        management["df"],
        viewer_user=MEMBER,
    )
    assert not set(SENSITIVE_PRODUCT_INFORMATION_COLUMNS).intersection(restored_frame.columns)
    with patch("app.services.snapshot_product_information_service._current_viewer", return_value=MEMBER):
        normalized = _normalize_result_for_chat(old_payload)
    assert not set(SENSITIVE_PRODUCT_INFORMATION_COLUMNS).intersection(normalized["df"].columns)
    contract = snapshot_grade_analysis_contract("제품정보 조회", member["df"].columns)
    assert not any(column in contract for column in SENSITIVE_PRODUCT_INFORMATION_COLUMNS)


def _dispatch(frame: pd.DataFrame, query: str):
    pushed = []
    notices = []
    handled = handle_current_table_followup_by_action(
        df=frame,
        query=query,
        top_n=300,
        table_key="fixture",
        source_action="제품정보 조회",
        helpers={
            "push_table": lambda **kwargs: pushed.append(kwargs) or True,
            "push_notice": lambda **kwargs: notices.append(kwargs) or True,
            "find_col": lambda source, names: next((name for name in names if name in source), ""),
            "to_num": lambda values: pd.to_numeric(values, errors="coerce"),
            "add_seq": lambda source: source,
            "fmt_num": str,
        },
        source_meta={},
        log=logging.getLogger("fixture"),
    )
    return handled, pushed, notices


def test_grade_names_and_aliases() -> None:
    frame = _query(MEMBER)["df"]
    for question in ("현재표 품목손익등급별 집계", "현재표 손익등급별 집계"):
        handled, pushed, _notices = _dispatch(frame, question)
        assert handled and pushed and "품목손익등급" in pushed[0]["df"].columns
    for question in ("현재표 품목기여등급별 집계", "현재표 기여등급별 집계", "현재표 기여도등급별 집계"):
        handled, pushed, _notices = _dispatch(frame, question)
        assert handled and pushed and "품목기여등급" in pushed[0]["df"].columns
    for question, key in (
        ("품목손익등급 A 제품정보", "profit_grade"),
        ("손익등급 A 제품정보", "profit_grade"),
        ("품목기여등급 A 제품정보", "contribution_grade"),
        ("기여등급 A 제품정보", "contribution_grade"),
    ):
        assert resolve_io_nlq(question)["params"][key] == "A"


def test_unlabeled_product_information() -> None:
    unlabeled = resolve_io_nlq("베링거 제품정보 조회")
    labeled = resolve_io_nlq("제약사 베링거 제품정보 조회")
    assert unlabeled["params"] == {"_product_information_unlabeled_name": "베링거"}
    assert labeled["params"].get("maker_nm") == "베링거"
    left = _query(MEMBER, unlabeled["params"])
    right = _query(MEMBER, labeled["params"])
    assert left["meta"]["source_call_count"] == right["meta"]["source_call_count"] == 1
    assert left["meta"]["row_count_total"] == right["meta"]["row_count_total"] == 3

    def compatible_master(_params):
        return pd.DataFrame([
            {"제품코드": "00001", "제품명": "동일 검증제품", "제약사": "동일 제약", "규격": "10정"},
            {"제품코드": "00002", "제품명": "다른제품", "제약사": "동일 제약", "규격": "10정"},
        ])

    def ambiguous_master(_params):
        return pd.DataFrame([
            {"제품코드": "00001", "제품명": "충돌 검증제품", "제약사": "다른제약", "규격": "10정"},
            {"제품코드": "00002", "제품명": "다른제품", "제약사": "충돌 제약", "규격": "10정"},
        ])

    def exact_code_conflict_master(_params):
        return pd.DataFrame([
            {"제품코드": "50002", "제품명": "50002 검증제품", "제약사": "다른제약", "규격": "10정"},
            {"제품코드": "00002", "제품명": "50002 다른제품", "제약사": "다른제약", "규격": "10정"},
        ])

    with patch("app.services.snapshot_product_information_service.get_current_company_id", return_value=7):
        compatible = get_snapshot_product_information_result(
            {"company_id": 7, "_product_information_unlabeled_name": "동일"},
            profile_resolver=_scope,
            projection_reader=_projection,
            master_loader=compatible_master,
            viewer_user=MEMBER,
        )
        ambiguous = get_snapshot_product_information_result(
            {"company_id": 7, "_product_information_unlabeled_name": "충돌"},
            profile_resolver=_scope,
            projection_reader=_projection,
            master_loader=ambiguous_master,
            viewer_user=MEMBER,
        )
        exact_code_conflict = get_snapshot_product_information_result(
            {"company_id": 7, "_product_information_unlabeled_name": "50002"},
            profile_resolver=_scope,
            projection_reader=_projection,
            master_loader=exact_code_conflict_master,
            viewer_user=MEMBER,
        )
        no_match = get_snapshot_product_information_result(
            {"company_id": 7, "_product_information_unlabeled_name": "없는이름"},
            profile_resolver=_scope,
            projection_reader=_projection,
            master_loader=lambda _params: pd.DataFrame(columns=["제품코드", "제품명", "제약사", "규격"]),
            viewer_user=MEMBER,
        )
    assert compatible["meta"]["result_status"] == "success"
    assert compatible["meta"]["row_count_total"] == 2
    assert compatible["meta"]["source_call_count"] == 1
    assert ambiguous["meta"]["result_status"] == "candidate_required"
    assert ambiguous["meta"]["source_call_count"] == 1
    assert exact_code_conflict["meta"]["result_status"] == "candidate_required"
    assert exact_code_conflict["meta"]["source_call_count"] == 1
    assert no_match["meta"]["result_status"] == "no_data"
    assert no_match["meta"]["source_call_count"] == 1

    category = resolve_io_nlq("전문약 제품정보 조회")
    assert category["params"].get("product_di_semantic_group") == "insurance"
    assert not category["params"].get("_product_information_unlabeled_name")


def test_product_information_router_owns_unlabeled_authority() -> None:
    delivered: list[dict] = []
    service_params: list[dict] = []

    def service(params=None, **kwargs):
        current = dict(params or kwargs)
        service_params.append(current)
        return {
            "final": True,
            "type": "table",
            "action": "제품정보 조회",
            "df": pd.DataFrame([{"제품코드": "00001", "제품명": "검증제품"}]),
            "meta": {"result_status": "success", "source_call_count": 1},
        }

    def reject_generic_resolver(*_args, **_kwargs):
        raise AssertionError("제품정보 전용 조건이 일반 entity resolver로 넘어갔습니다.")

    def push(payload, _action):
        delivered.append(payload)
        return payload.get("meta") or {}

    with (
        patch("app.services.io_nlq.resolve_unlabeled_io_entity_condition", side_effect=reject_generic_resolver),
        patch("app.services.snapshot_product_information_service.get_snapshot_product_information_result", side_effect=service),
        patch("app.ui.chat_middleware.push_sims_result_to_chat", side_effect=push),
    ):
        for query in ("베링거 제품정보 조회", "전문약 제품정보 조회"):
            assert nlq_router._try_handle_io_nlq(
                query,
                room={},
                session_state={},
                make_ts=lambda: "2026-09-18 00:00:00",
                next_seq=lambda: 1,
                logger=logging.getLogger("fixture"),
            )

    assert service_params[0].get("_product_information_unlabeled_name") == "베링거"
    assert service_params[1].get("product_di_semantic_group") == "insurance"
    assert "_product_information_unlabeled_name" not in service_params[1]
    assert len(delivered) == 2
    assert all((payload.get("meta") or {}).get("source_call_count") == 1 for payload in delivered)


def test_member_current_table_denial() -> None:
    frame = _query(MEMBER)["df"]
    handled, pushed, notices = _dispatch(frame, "현재표 추정손익률 보여줘")
    assert not pushed
    assert not any(
        column in str(item)
        for item in notices
        for column in SENSITIVE_PRODUCT_INFORMATION_COLUMNS
    )
    assert not set(SENSITIVE_PRODUCT_INFORMATION_COLUMNS).intersection(frame.columns)
    old_management_frame = _query(MANAGEMENT)["df"]
    with patch("app.services.snapshot_product_information_service._current_viewer", return_value=MEMBER):
        handled, pushed, notices = _dispatch(old_management_frame, "현재표 추정손익률 보여줘")
    assert handled and not pushed and notices


def test_order_default() -> None:
    defaults = apply_application_defaults({})
    assert defaults["query_mode"] == "발주해당자료만" and defaults["only_needed"] is True
    for question in ("발주 계산", "제약사 베링거 발주계산"):
        params = resolve_registered_erp_table_nlq(question)["params"]
        assert params["query_mode"] == "발주해당자료만" and params["only_needed"] is True
    for question in ("전체 발주계산", "발주계산 전체 보여줘"):
        params = resolve_registered_erp_table_nlq(question)["params"]
        assert params["query_mode"] == "전체" and params["only_needed"] is False


if __name__ == "__main__":
    test_visibility_projection()
    print("PASS management/member product-information projection")
    test_grade_names_and_aliases()
    print("PASS official grade names and legacy aliases")
    test_unlabeled_product_information()
    print("PASS unlabeled product-information authority with one source call")
    test_product_information_router_owns_unlabeled_authority()
    print("PASS product-information route bypasses the generic entity resolver")
    test_member_current_table_denial()
    print("PASS member current-table cost-metric denial")
    test_order_default()
    print("PASS order calculation default and explicit whole mode")
