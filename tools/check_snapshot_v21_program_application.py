from __future__ import annotations

import sys
import ast
import hashlib
import json
import logging
import os
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot import FrequencyProjectionReadResult
from app.services.io_nlq import resolve_io_nlq
from app.services.product_inventory_service import (
    _CONTRIBUTION_GRADE_COLUMN,
    _FREQUENCY_COUNT_COLUMN,
    _FREQUENCY_GRADE_COLUMN,
    _PROFIT_GRADE_COLUMN,
    _current_stock_display_columns,
    attach_dashboard_frequency_snapshot,
)
from app.services.snapshot_product_information_service import (
    get_snapshot_product_information_result,
)
from app.sims.nlq.action_inventory import implemented_actions
from app.sims.nlq.nlq_router import resolve_new_sims_nlq_candidate


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _scope(**_kwargs):
    return SimpleNamespace(
        stock_codes=("00001",), product_group_codes=("0013:9999",),
        product_di_codes=("0004:01",), product_class_codes=(), stock_mode="real",
    )


def _projection(**_kwargs):
    return FrequencyProjectionReadResult(
        status="ready",
        rows=(
            {
                "product_code": "00001", "frequency_grade": "F", "profit_grade": "B",
                "contribution_grade": "A", "lifecycle_status": "new_product",
                "first_normal_inbound_month": "202609", "row_status": "ready",
                "occurrence_count_3m": 3, "outbound_day_count_3m": 2,
                "outbound_customer_count_3m": 2, "outbound_qty_3m": 10,
                "outbound_paid_qty_3m": 10, "return_event_count_3m": 0,
                "return_qty_3m": 0, "return_supply_amount_3m": 0,
                "avg_purchase_unit_cost": 100, "purchase_price_basis_month": "202608",
                "purchase_price_status": "ready", "avg_sales_unit_price": 150,
                "sales_price_basis_month": "202608", "sales_price_status": "ready",
                "estimated_unit_profit": 50, "estimated_profit_rate": 0.333333,
                "estimated_contribution_amount": 500, "profitability_status": "ready",
            },
        ),
        manifest_id=7, generation_no=1, checksum="a" * 64,
        authority_status="ready", resolution_status="exact_match", contract_version="2.1",
    )


def test_product_information_service() -> None:
    payload = get_snapshot_product_information_result(
        {"company_id": 7, "frequency_grade": "F", "profit_grade": "B"},
        profile_resolver=_scope,
        projection_reader=_projection,
        master_loader=lambda params: pd.DataFrame([{
            "제품코드": "00001", "제품명": "검증품목", "제약사": "검증제약", "규격": "10정",
        }]),
    )
    frame = payload["df"]
    _assert(payload["meta"]["source_call_count"] == 1, "product enrichment source count mismatch")
    _assert(payload["meta"]["snapshot_read_call_count"] == 1, "Snapshot read provenance missing")
    _assert(payload["meta"]["snapshot_contract_version"] == "2.1", "v2.1 contract missing")
    _assert(list(frame.loc[0, ["제품코드", "출고빈도등급", "손익등급", "기여도등급"]]) == ["00001", "F", "B", "A"], "grade projection mismatch")
    _assert("추정기여금액" in frame.columns and "3개월반품공급가액" in frame.columns, "detail statistics missing")


def test_inventory_grade_attachment_and_placement() -> None:
    source = pd.DataFrame([{"제품코드": "00001", "KD코드": "012345", "재고수량": 4}])
    attached, meta = attach_dashboard_frequency_snapshot(
        source,
        params={"company_id": 7, "evaluation_month": "202609"},
        date_to="20260912",
        profile_scope_resolver=_scope,
        projection_reader=_projection,
    )
    columns = list(attached.columns)
    kd = columns.index("KD코드")
    _assert(columns[kd + 1:kd + 4] == [_FREQUENCY_GRADE_COLUMN, _PROFIT_GRADE_COLUMN, _CONTRIBUTION_GRADE_COLUMN], "product inventory grade placement mismatch")
    _assert(attached.loc[0, _PROFIT_GRADE_COLUMN] == "B" and attached.loc[0, _CONTRIBUTION_GRADE_COLUMN] == "A", "profit grades not attached")
    _assert(meta["frequency_additional_erp_source_call_count"] == 0, "inventory attachment added ERP call")

    current_columns = _current_stock_display_columns(pd.DataFrame(columns=[
        _FREQUENCY_GRADE_COLUMN, _FREQUENCY_COUNT_COLUMN,
        _PROFIT_GRADE_COLUMN, _CONTRIBUTION_GRADE_COLUMN,
    ]))
    stock = current_columns.index("재고수량")
    _assert(current_columns[stock + 1:stock + 4] == ["출고빈도등급", "손익등급", "기여도등급"], "current-stock grade placement mismatch")


def test_nlq_and_inventory_boundaries() -> None:
    labeled = resolve_io_nlq("제품정보 조회 제약사 중외제약")
    _assert(labeled["params"].get("maker_nm") == "중외제약", "manufacturer missing")
    _assert(not labeled["params"].get("physic_nm"), "action residual leaked into product")
    for question, key, value in (
        ("제품정보 조회 키워드 아리바정", "product_keyword", "아리바정"),
        ("제품정보 조회 보험코드 644100990", "insu_cd", "644100990"),
        ("제품정보 조회 바코드 8806449135066", "barcode", "8806449135066"),
        ("제품정보 조회 제품그룹명 전문", "product_group_nm", "전문"),
        ("제품정보 조회 구분명 보험", "product_di_nm", "보험"),
        ("제품정보 조회 제품분류명 내복제", "product_class_nm", "내복제"),
        ("출고빈도등급 F 제품정보 조회", "frequency_grade", "F"),
    ):
        parsed = resolve_io_nlq(question)
        _assert(parsed["params"].get(key) == value, f"product information filter missing: {key}")
    info = resolve_io_nlq("손익등급 A 제품정보 조회")
    inventory = resolve_io_nlq("제품재고장 조회")
    flow = resolve_io_nlq("제품수불현황 조회")
    _assert(info and info["action"] == "제품정보 조회" and info["params"].get("profit_grade") == "A", "product information NLQ failed")
    _assert(inventory and inventory["action"] == "제품재고현황 조회", "product inventory route regressed")
    _assert(flow and flow["action"] == "제품수불현황 조회", "product flow route regressed")
    _assert(resolve_io_nlq("제품 조회") is None, "master product query was captured by Snapshot action")
    for question in ("제품정보 조회", "제품 정보 조회", "신규품목 제품정보 조회"):
        candidate = resolve_new_sims_nlq_candidate(question)
        _assert(candidate == {"route": "io", "action": "제품정보 조회"}, f"public router mismatch: {question}")
    _assert(resolve_new_sims_nlq_candidate("제품 조회") is None, "public router captured master product query")
    specs = {item.canonical_action: item for item in implemented_actions()}
    _assert(specs["제품정보 조회"].handler_kind == "snapshot_service", "canonical action inventory missing")


def test_production_attachment_passes_full_profile() -> None:
    def reader(**kwargs):
        _assert(kwargs["product_group_codes"] == ["0013:9999"], "group profile missing")
        _assert(kwargs["product_di_codes"] == ["0004:01"], "di profile missing")
        _assert(kwargs["product_class_codes"] == [], "empty class not preserved")
        _assert(kwargs["stock_mode"] == "real", "stock mode missing")
        return _projection()

    with patch("app.services.product_inventory_service.read_approved_frequency_projection", reader):
        attached, meta = attach_dashboard_frequency_snapshot(
            pd.DataFrame([{"제품코드": "00001", "재고수량": 4}]),
            params={"company_id": 7, "evaluation_month": "202609"}, date_to="20260913",
            profile_scope_resolver=_scope,
        )
    _assert(attached.loc[0, _FREQUENCY_GRADE_COLUMN] == "F", "production attachment failed")
    _assert(meta["frequency_snapshot_status"] == "ready", "profile resolution failed")


def test_panel_submission_round_trip_identity() -> None:
    import app.ui.chat_middleware as middleware
    from app.ui.sims_panel_submission import record_panel_submission, consume_panel_submission
    from app.ui.chat_message_controls import claim_chat_message_controls
    import uuid

    state = {"__sims_was_final": True, "__sims_run_seq": 1,
             "__sims_selected": {"category": "재고관리", "action": "제품정보 조회"}}
    ns = dict(Any=Any, Dict=Dict, Mapping=Mapping, st=SimpleNamespace(session_state=state),
              json=json, hashlib=hashlib, os=os, pd=pd, log=logging.getLogger("gate"),
              _panel_payload_action=lambda payload, action: action,
              _panel_stamp_payload_company=lambda payload: None,
              _panel_payload_matches_current_company=lambda payload: True,
              _panel_result_target_chat_enabled=lambda: True,
              _render_panel_chat_only_done=lambda *args: None,
              _sims_payload_matches_current_company=lambda payload: True,
              _ensure_sims_panel_room_title=lambda *args: None,
              _sync_room_meta=lambda *args, **kwargs: None,
              save_chat_rooms=lambda: None, _chat_log_kv=lambda room: "fixture")
    for path, names in (
        ("app/ui/sims_panel.py", {"_panel_query_fingerprint", "_make_panel_source_sig", "_store_panel_final_payload_for_chat", "_render_payload"}),
        ("app/Lmstudio_SSAI_chat_main.py", {"_push_panel_result_to_current_chat"}),
    ):
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8-sig"))
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        _assert(len(nodes) == len(names), "production bridge functions not found")
        exec(compile(ast.Module(body=nodes, type_ignores=[]), path, "exec"), ns)
    frame = pd.DataFrame({"제품코드": ["00001"]})
    payload = {"action": "제품정보 조회", "final": True, "params": {"maker_nm": "검증제약"},
               "df": frame, "df_display": frame, "meta": {"table_key": "fixture_table"}}
    room = {"messages": []}

    def deliver(result, action):
        result["id"] = str(uuid.uuid4())
        result["role"] = "assistant"
        room["messages"].append(result)
        state["__sims_current_table_source_key"] = result["meta"]["table_key"]
        state["__sims_last_table_key"] = result["meta"]["table_key"]

    def submit(result, action):
        state["__sims_selected"]["action"] = action
        record_panel_submission(state)
        event = consume_panel_submission(state, action)
        result = {**result, "meta": {**result["meta"], "_panel_submission_id": event, "_panel_source_sig": event}}
        ns["_store_panel_final_payload_for_chat"](result, action, submission_id=event)
        return result

    def restore(result):
        # Execute the real production cached-render path, then the real Chat bridge.
        state["__sims_panel_last_final_payload"] = result
        before = (state.get("__sims_current_table_source_key"), state.get("__sims_last_table_key"))
        ns["_render_payload"](result, result["action"])
        _assert(not ns["_push_panel_result_to_current_chat"](room), "cached result re-pushed")
        _assert(before == (state.get("__sims_current_table_source_key"), state.get("__sims_last_table_key")), "cached result stole current-table ownership")

    with patch.object(middleware, "push_sims_result_to_chat", deliver):
        payload = submit(payload, "제품정보 조회")
        _assert(ns["_push_panel_result_to_current_chat"](room), "first submission not pushed")
        restore(payload)
        _assert(len(room["messages"]) == 1, "duplicate history/feedback message")

        # Followup delivery preserves the successful query's source.
        room["messages"].append({"id": str(uuid.uuid4()), "role": "assistant", "meta": {"current_table_followup": True}})
        restore(payload)
        _assert(len(room["messages"]) == 2, "followup re-pushed original Panel")
        for action in ("현재고 조회", "제품재고현황 조회"):
            new_payload = submit({**payload, "action": action, "meta": {"table_key": action}}, action)
            _assert(ns["_push_panel_result_to_current_chat"](room), "other query missing")
            restore(payload)
            _assert(state["__sims_current_table_source_key"] == action, "old Panel replaced new source")

        # Same query/filter/table key, explicit new button press: new event.
        new_payload = submit(payload, "제품정보 조회")
        _assert(ns["_push_panel_result_to_current_chat"](room), "new submission suppressed")
        restore(new_payload)
        _assert(len(room["messages"]) == 5, "history count differs from actual submissions/followup")
        for message in room["messages"]:
            _assert(claim_chat_message_controls(state, message), "feedback missing")
            _assert(not claim_chat_message_controls(state, message), "feedback claimed twice")
        ns["_store_panel_final_payload_for_chat"](payload, "제품정보 조회")
        _assert("__sims_last_final_payload_for_chat" not in state, "cache enqueued without submit authorization")


def test_shared_snapshot_format_and_log() -> None:
    from app.ui.sims_table_display import (
        prepare_sims_table_display_df, _numeric_display_kind, _format_numeric_display_value,
        resolve_sims_excel_number_format,
    )
    raw = pd.DataFrame({"추정단위손익": [Decimal("-12.345")], "추정손익률": [Decimal("-0.125")],
                        "최초정상입고월": ["202609"], "매입단가기준월": ["202608"],
                        "매출단가기준월": [None]})
    view = prepare_sims_table_display_df(raw, action_name="제품정보 조회")
    _assert(view.loc[0, "최초정상입고월"] == "2026-09", "month formatting failed")
    _assert(view.loc[0, "매출단가기준월"] == "", "null month not blank")
    _assert(view.loc[0, "추정손익률"] == -12.5, "fraction percent display failed")
    _assert(_format_numeric_display_value(view.loc[0, "추정단위손익"], _numeric_display_kind("추정단위손익")) == "-12.35", "profit precision failed")
    _assert(raw.loc[0, "추정손익률"] == Decimal("-0.125"), "raw/download ratio mutated")
    _assert(resolve_sims_excel_number_format("추정손익률") == "0.00%", "Excel raw fraction format failed")
    _assert(_format_numeric_display_value(3394, _numeric_display_kind("제품수")) == "3,394", "followup product count formatted as decimal")
    with patch("app.services.snapshot_product_information_service.log.info") as info:
        get_snapshot_product_information_result(
            {"company_id": 7}, profile_resolver=_scope, projection_reader=_projection,
            master_loader=lambda params: pd.DataFrame({"제품코드": ["00001"]}),
        )
    _assert(info.call_count == 1, "query log duplicated")
    _assert(info.call_args.args[0].startswith("[product_information.query]"), "query provenance tag missing")


def test_panel_dispatch_consumes_only_button_events() -> None:
    from app.ui.sims_panel_submission import record_panel_submission
    action = "제품정보 조회"
    selected = {"category": "재고관리", "action": action}
    state = {"__sims_selected": selected, "__sims_last_action": f"재고관리::{action}", "__sims_form_id": 1}
    cached = {"final": True, "meta": {"table_key": "same_key"}}
    rendered = []
    button_pressed = [False]

    def view():
        if button_pressed[0]:
            record_panel_submission(state)
        return cached

    ns = dict(Any=Any, Dict=Dict, Optional=Optional,
              st=SimpleNamespace(session_state=state, info=lambda *a: None),
              log=logging.getLogger("gate"),
              _ensure_sims_state=lambda: None, render_sims_context_controls=lambda: None,
              _CATEGORIES={"재고관리": {"actions": {action: view}}},
              _log_state=lambda *a: None,
              _pop_panel_skip_view_payload_for_chat_rerun=lambda **kw: None,
              _get_panel_last_final_payload=lambda *a: cached,
              _render_payload=lambda payload, action, **kw: rendered.append((payload, kw["submission_id"])))
    tree = ast.parse((ROOT / "app/ui/sims_panel.py").read_text(encoding="utf-8-sig"))
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "render_sims_main")
    exec(compile(ast.Module(body=[node], type_ignores=[]), "panel-dispatch", "exec"), ns)
    button_pressed[0] = True
    ns["render_sims_main"](selected)
    first = rendered[-1][1]
    _assert(bool(first), "button event not authorized by production dispatch")
    button_pressed[0] = False
    ns["render_sims_main"](selected)
    _assert(rendered[-1][1] == "", "cached final got submit ownership")
    button_pressed[0] = True
    ns["render_sims_main"](selected)
    _assert(rendered[-1][1] and rendered[-1][1] != first, "same conditions did not get new event")
    _assert("__sims_pending_submission" not in state, "submit event not consumed")
    button_pressed[0] = False
    record_panel_submission(state)
    ns["render_sims_main"](selected)
    _assert(bool(rendered[-1][1]), "on_click callback event lost before view")
    ns["render_sims_main"](selected)
    _assert(not rendered[-1][1], "on_click event reused on rerun")


def validate_company7_read_only() -> None:
    from app.db.mssql_client import set_current_company_id
    set_current_company_id(7)
    parsed = resolve_io_nlq("제품정보 조회 제약사 중외제약")
    payload = get_snapshot_product_information_result(parsed["params"])
    _assert(payload["meta"]["result_status"] == "success", "company7 product information unavailable")
    frame = payload["df"]
    _assert(frame["제약사"].str.contains("중외제약", regex=False).all(), "manufacturer filter ignored")
    for anchor in ("재고수량", "KD코드"):
        source = pd.DataFrame({"제품코드": frame["제품코드"], anchor: [0] * len(frame)})
        attached, meta = attach_dashboard_frequency_snapshot(source, params={"company_id": 7}, date_to="20260913")
        _assert(meta["frequency_snapshot_status"] == "ready", "company7 attachment not ready")
        for column in (_FREQUENCY_GRADE_COLUMN, _PROFIT_GRADE_COLUMN, _CONTRIBUTION_GRADE_COLUMN):
            _assert(attached[column].tolist() == frame[column].tolist(), f"company7 grade mismatch: {column}")
        print("PASS company7 attachment", anchor, len(attached), meta["frequency_missing_product_count"])
    print("PASS company7 product information", len(frame), frame["출고빈도등급"].value_counts().to_dict())


def main() -> int:
    tests = (
        test_product_information_service,
        test_inventory_grade_attachment_and_placement,
        test_nlq_and_inventory_boundaries,
        test_production_attachment_passes_full_profile,
        test_panel_submission_round_trip_identity,
        test_shared_snapshot_format_and_log,
        test_panel_dispatch_consumes_only_button_events,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS Snapshot v2.1 program application {len(tests)}/{len(tests)}")
    if "--company7-read-only" in sys.argv:
        validate_company7_read_only()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
