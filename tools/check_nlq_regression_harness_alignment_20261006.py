"""Focused company-3 regression that uses the real chat table-stash boundary.

The 227 live runner is intentionally flat: it sends one casebook question to
``try_handle_nlq``.  That is not a valid execution model for Current Table
follow-ups, which require a prior result to pass through the production chat
stash before the follow-up is dispatched.  This focused gate keeps the
casebook text as evidence while exercising that complete boundary.

No production routing or SQL is implemented here.  The Current Table handler
body is compiled directly from the production source, following the existing
``check_chat_completed_turn_boundary`` gate pattern, so this harness cannot
quietly drift into a second dispatcher implementation.
"""

from __future__ import annotations

import argparse
import ast
import csv
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.mssql_client import get_current_company_id, set_current_company_id
from app.sims.nlq.nlq_vendors import _MAKER_VENDOR_LOOKUP_RE
from app.ui.current_table_followups.action_dispatcher import (
    classify_current_table_followup_intent,
    handle_current_table_followup_by_action,
    is_explicit_current_trans_doc_validation_request,
    parse_current_table_rank_request,
)
from tools.check_nlq_official_206_regression import _action_name_matches


LOG = logging.getLogger("nlq-regression-harness-alignment")
MAIN_SOURCE = ROOT / "app" / "Lmstudio_SSAI_chat_main.py"
MAIN_TREE = ast.parse(MAIN_SOURCE.read_text(encoding="utf-8"))
MAIN_FUNCTIONS = {
    node.name: node for node in MAIN_TREE.body if isinstance(node, ast.FunctionDef)
}
MAIN_ASSIGNMENTS = {
    target.id: node
    for node in MAIN_TREE.body
    if isinstance(node, ast.Assign)
    for target in node.targets
    if isinstance(target, ast.Name)
}


@dataclass(frozen=True)
class FocusedCase:
    case_id: str
    question: str
    expected_action: str
    parent_question: str = ""
    parent_action: str = ""
    runtime_followup: str = ""

    @property
    def is_current_table(self) -> bool:
        return bool(self.parent_question)


FOCUSED_CASES = (
    FocusedCase(
        "NLQ-0067", "추세판정 요약", "영업사원별 매출예상",
        parent_question="영업사원별 매출 예상", parent_action="영업사원별 매출 예상",
        runtime_followup="현재표 추세판정 요약",
    ),
    FocusedCase(
        "NLQ-0072", "추세판정 요약", "매출처별 매출예상",
        parent_question="매출처별 매출 예상", parent_action="매출처별 매출 예상",
        runtime_followup="현재표 추세판정 요약",
    ),
    FocusedCase(
        "NLQ-0086", "현재표 부족제품수 top 10 보여줘", "매입처별 재고부족현황",
        parent_question="매입처별 재고부족 현황", parent_action="매입처별 재고부족 현황",
        runtime_followup="현재표 부족제품수 top 10 보여줘",
    ),
    FocusedCase("NLQ-0152", "제약사 조회", "거래처코드목록"),
    FocusedCase("NLQ-0153", "제약사 한미 조회", "거래처코드목록"),
)


class DeliveryCapture:
    """Capture delivery metadata while retaining the production chat stash."""

    def __init__(self, original_push) -> None:
        self._original_push = original_push
        self.items: list[dict[str, Any]] = []

    def push(self, payload: Any = None, action: str | None = None, *args: Any, **kwargs: Any):
        if payload is None and args:
            payload = args[0]
        item = dict(payload) if isinstance(payload, dict) else {"data": payload}
        item["meta"] = dict(item.get("meta") or {})
        item["_capture_action"] = str(action or "")
        self.items.append(item)
        return self._original_push(payload, action, **kwargs)


def _main_namespace(session_state: dict[str, Any], delivery) -> dict[str, Any]:
    """Load the exact production helper functions without executing the app UI."""
    function_names = (
        "_normalize_implicit_analytics_current_followup",
        "_normalize_current_table_followup_input",
        "_current_table_first_series",
        "_current_table_find_col",
        "_current_table_to_num",
        "_current_table_get_latest_df",
        "_current_table_fill_alias_values",
        "_current_table_clean_none_for_display",
        "_current_table_numeric_filter_op",
        "_merge_current_table_push_meta",
        "_current_table_push_table",
        "_current_table_push_notice",
        "_try_handle_current_table_dataframe_followup",
    )
    assignment_names = (
        "_ANALYTICS_KPI_SOURCE_ACTIONS",
        "_CURRENT_TABLE_PUSH_META_PROTECTED_KEYS",
    )
    body: list[ast.stmt] = [
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        *(MAIN_ASSIGNMENTS[name] for name in assignment_names),
        *(MAIN_FUNCTIONS[name] for name in function_names),
    ]
    namespace = {
        "st": SimpleNamespace(session_state=session_state),
        "pd": pd,
        "re": re,
        "uuid": __import__("uuid"),
        "log": LOG,
        "parse_current_table_rank_request": parse_current_table_rank_request,
        "classify_current_table_followup_intent": classify_current_table_followup_intent,
        "is_explicit_current_trans_doc_validation_request": is_explicit_current_trans_doc_validation_request,
        "handle_current_table_followup_by_action": handle_current_table_followup_by_action,
        "push_sims_result_to_chat": delivery,
    }
    module = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    exec(compile(module, str(MAIN_SOURCE), "exec"), namespace)
    return namespace


def _payload_action(item: dict[str, Any] | None) -> str:
    return str((item or {}).get("action") or (item or {}).get("title") or "").strip()


def _payload_meta(item: dict[str, Any] | None) -> dict[str, Any]:
    return dict((item or {}).get("meta") or {})


def _source_call_count(item: dict[str, Any] | None) -> int:
    value = _payload_meta(item).get("source_call_count")
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = [
        "case_id", "question", "company_id", "parent_question", "parent_action",
        "parent_status", "parent_rows", "source_table_key", "source_action",
        "runtime_followup", "followup_action", "followup_status", "followup_rows",
        "route_pass", "schema_pass", "condition_pass", "semantic_pass", "data_pass",
        "current_table_extra_erp_source_call", "elapsed_ms", "verdict", "error",
        "followup_handled", "requested_metric", "requested_top", "result_columns",
        "metric_order_pass", "source_binding_pass",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def _write_progress(path: Path, *, case_id: str, phase: str, completed: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".pending")
    with pending.open("w", encoding="utf-8") as handle:
        json.dump({"company_id": 3, "case_id": case_id, "phase": phase, "completed": completed}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(pending, path)


def _followup_result_contract(query: str, payload: dict[str, Any] | None, source_action: str = "") -> dict[str, Any]:
    """Check the requested metric and rank against the actual derived table."""
    direction, top_n = parse_current_table_rank_request(query, default_limit=20)
    result_df = (payload or {}).get("df")
    columns = [str(col) for col in result_df.columns] if isinstance(result_df, pd.DataFrame) else []
    compact_query = re.sub(r"\s+", "", query)
    numeric_columns = [
        column for column in columns
        if column not in {"순번", "건수"}
        and column.replace(" ", "") in compact_query
        and pd.to_numeric(result_df[column], errors="coerce").notna().all()
    ] if isinstance(result_df, pd.DataFrame) else []
    if (
        "거래명세서" in source_action
        and any(term in compact_query for term in ("매입금액", "매출금액"))
        and "거래금액" in columns
        and isinstance(result_df, pd.DataFrame)
        and pd.to_numeric(result_df["거래금액"], errors="coerce").notna().all()
    ):
        numeric_columns.append("거래금액")
    requested_metric = max(numeric_columns, key=len, default="")
    metric_present = bool(requested_metric and requested_metric in columns)
    row_limit_ok = bool(isinstance(result_df, pd.DataFrame) and 0 < len(result_df) <= top_n)
    order_ok = False
    if metric_present and row_limit_ok:
        values = pd.to_numeric(result_df[requested_metric], errors="coerce")
        order_ok = bool(values.notna().all() and values.is_monotonic_decreasing)
    requested_date_column = next(
        (column for token, column in (("요일별", "요일"), ("월별", "월"), ("일별", "일자"))
         if token in compact_query),
        "",
    )
    table_contract_pass = bool(
        isinstance(result_df, pd.DataFrame) and not result_df.empty
        and (not requested_date_column or requested_date_column in columns)
    )
    return {
        "requested_metric": requested_metric,
        "requested_top": top_n if direction else "",
        "result_columns": "|".join(columns),
        "metric_order_pass": bool(metric_present and row_limit_ok and order_ok),
        "rank_contract_pass": bool(table_contract_pass and (not direction or (metric_present and row_limit_ok and order_ok))),
    }


def _run_direct(case: FocusedCase, router, state: dict[str, Any], room: dict[str, Any], capture: DeliveryCapture, next_seq) -> dict[str, Any]:
    before = len(capture.items)
    started = time.perf_counter()
    error = ""
    try:
        handled = bool(router.try_handle_nlq(
            case.question, room=room, session_state=state,
            make_ts=lambda: datetime.now().isoformat(timespec="seconds"),
            next_seq=next_seq, logger=LOG,
        ))
    except BaseException as exc:
        handled = False
        error = type(exc).__name__
    payload = capture.items[-1] if len(capture.items) > before else None
    actual_action = _payload_action(payload)
    route_pass = bool(handled and _action_name_matches(case.expected_action, actual_action))
    meta = _payload_meta(payload)
    result_df = (payload or {}).get("df")
    maker_lookup = _MAKER_VENDOR_LOOKUP_RE.fullmatch(case.question.strip())
    if maker_lookup:
        columns = set(result_df.columns) if isinstance(result_df, pd.DataFrame) else set()
        schema_pass = {"거래처코드", "거래처명"}.issubset(columns)
        condition_pass = bool(meta.get("scope") == "maker" and schema_pass)
        name = maker_lookup.group("name")
        if name and condition_pass:
            condition_pass = bool(result_df["거래처명"].astype(str).str.contains(name, regex=False).all())
        semantic_pass = bool(meta.get("domain") == "vendors" and meta.get("scope") == "maker")
        data_pass = bool(meta.get("result_status") == "success" and isinstance(result_df, pd.DataFrame) and not result_df.empty)
    else:
        schema_pass = condition_pass = semantic_pass = route_pass
        data_pass = bool(handled and payload is not None)
    verdict_pass = all((route_pass, schema_pass, condition_pass, semantic_pass, data_pass))
    return {
        "case_id": case.case_id, "question": case.question, "company_id": 3,
        "parent_question": "", "parent_action": "", "parent_status": "",
        "parent_rows": "", "source_table_key": "", "source_action": "",
        "runtime_followup": "", "followup_action": actual_action,
        "followup_status": str(meta.get("result_status") or ""),
        "followup_rows": meta.get("row_count_total", meta.get("row_count", "")),
        "route_pass": route_pass, "schema_pass": schema_pass,
        "condition_pass": condition_pass, "semantic_pass": semantic_pass,
        "data_pass": data_pass,
        "current_table_extra_erp_source_call": 0,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "verdict": "PASS" if verdict_pass else "FAIL", "error": error,
    }


def _run_current_table(case: FocusedCase, router, state: dict[str, Any], room: dict[str, Any], capture: DeliveryCapture, next_seq, *, cached_parent=None, parent_ready_hook=None) -> dict[str, Any]:
    started = time.perf_counter()
    error = ""
    if cached_parent is None:
        parent_before = len(capture.items)
        try:
            parent_handled = bool(router.try_handle_nlq(
                case.parent_question, room=room, session_state=state,
                make_ts=lambda: datetime.now().isoformat(timespec="seconds"),
                next_seq=next_seq, logger=LOG,
            ))
        except BaseException as exc:
            parent_handled = False
            error = f"parent:{type(exc).__name__}"
        parent = capture.items[-1] if len(capture.items) > parent_before else None
        if parent_ready_hook is not None:
            parent_ready_hook(parent_handled, parent, state)
    else:
        parent_handled = bool(cached_parent["handled"])
        parent = cached_parent["payload"]
    parent_meta = _payload_meta(parent)
    source_key = str(state.get("__sims_current_table_source_key") or "")
    source_action = str(state.get("__sims_current_table_source_action") or "")
    source_frames = [
        store.get(source_key)
        for name in ("__sims_export_tables_by_key", "sims_export_tables", "sims_tables")
        for store in (state.get(name),)
        if source_key and isinstance(store, dict) and isinstance(store.get(source_key), pd.DataFrame)
    ]
    source_frame = max(source_frames, key=len) if source_frames else None
    date_columns = [column for column in ("입출고일자", "수불일자", "입고일자", "출고일자", "기준일자", "일자")
                    if isinstance(source_frame, pd.DataFrame) and column in source_frame.columns]
    valid_date_rows = ""
    if date_columns:
        from app.ui.current_table_followups.time_grouping import derive_current_table_time_grouping
        valid_date_rows = int(derive_current_table_time_grouping(source_frame[date_columns[0]], "day").ne("").sum())
    namespace = _main_namespace(state, capture.push)
    runtime_followup = case.runtime_followup
    if (
        source_action in namespace["_ANALYTICS_KPI_SOURCE_ACTIONS"]
        and not any(token in runtime_followup.replace(" ", "") for token in ("현재표", "현재조회결과", "현재결과"))
    ):
        runtime_followup = namespace["_normalize_implicit_analytics_current_followup"](runtime_followup)
    followup_before = len(capture.items)
    try:
        followup_handled = bool(namespace["_try_handle_current_table_dataframe_followup"](
            runtime_followup, room=room,
            make_ts=lambda: datetime.now().isoformat(timespec="seconds"), next_seq=next_seq,
        ))
    except BaseException as exc:
        followup_handled = False
        error = f"{error};followup:{type(exc).__name__}".strip(";")
    followup = capture.items[-1] if len(capture.items) > followup_before else None
    followup_meta = _payload_meta(followup)
    followup_action = _payload_action(followup)
    followup_status = str(followup_meta.get("result_status") or "")
    parent_action = _payload_action(parent)
    parent_ok = bool(
        parent_handled and parent and source_key and source_action
        and source_action == parent_action
        and (not case.parent_action or _action_name_matches(case.parent_action, parent_action))
    )
    result_contract = _followup_result_contract(runtime_followup, followup, source_action)
    source_binding_pass = bool(
        followup_meta.get("source_table_key") == source_key
        and followup_meta.get("source_action") == source_action
    )
    success = bool(
        parent_ok and followup_handled and followup_status == "success"
        and source_binding_pass and _source_call_count(followup) == 0
        and result_contract["rank_contract_pass"]
    )
    expected_current_action = "현재표 추세판정별 집계" if "추세판정" in runtime_followup else ""
    route_pass = bool(success and (not expected_current_action or followup_action == expected_current_action))
    return {
        "case_id": case.case_id, "question": case.question, "company_id": 3,
        "parent_question": case.parent_question, "parent_action": _payload_action(parent),
        "parent_status": str(parent_meta.get("result_status") or ""),
        "parent_rows": parent_meta.get("row_count_total", parent_meta.get("row_count", "")),
        "source_rows": len(source_frame) if isinstance(source_frame, pd.DataFrame) else "",
        "date_columns": "|".join(date_columns),
        "valid_date_rows": valid_date_rows,
        "parent_source_call_count": 0 if cached_parent is not None else _source_call_count(parent),
        "parent_reused": cached_parent is not None,
        "source_table_key": "present" if source_key else "", "source_action": source_action,
        "runtime_followup": runtime_followup, "followup_action": followup_action,
        "followup_status": followup_status,
        "followup_rows": followup_meta.get("row_count_total", followup_meta.get("row_count", "")),
        "route_pass": route_pass, "schema_pass": bool(parent_ok and followup is not None and result_contract["rank_contract_pass"]),
        "condition_pass": bool(parent_ok and followup_handled and result_contract["rank_contract_pass"]), "semantic_pass": route_pass,
        "data_pass": bool(parent_ok and followup is not None and result_contract["rank_contract_pass"]),
        "current_table_extra_erp_source_call": _source_call_count(followup),
        "followup_handled": followup_handled,
        "requested_metric": result_contract["requested_metric"],
        "requested_top": result_contract["requested_top"],
        "result_columns": result_contract["result_columns"],
        "metric_order_pass": result_contract["metric_order_pass"],
        "source_binding_pass": source_binding_pass,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "verdict": "PASS" if route_pass else "FAIL", "error": error,
    }


def _harness_error_result(case: FocusedCase, exc: BaseException) -> dict[str, Any]:
    """Persist an unexpected harness failure without hiding the remaining cases."""
    return {
        "case_id": case.case_id, "question": case.question, "company_id": 3,
        "parent_question": case.parent_question, "parent_action": "", "parent_status": "",
        "parent_rows": "", "source_table_key": "", "source_action": "",
        "runtime_followup": case.runtime_followup, "followup_action": "", "followup_status": "",
        "followup_rows": "", "route_pass": False, "schema_pass": False,
        "condition_pass": False, "semantic_pass": False, "data_pass": False,
        "current_table_extra_erp_source_call": 0, "elapsed_ms": 0,
        "verdict": "FAIL", "error": f"harness:{type(exc).__name__}",
    }


def run(output_csv: Path, *, case_ids: tuple[str, ...] = (), progress_path: Path | None = None) -> list[dict[str, Any]]:
    from app.sims.nlq import nlq_goods, nlq_router, nlq_vendors
    from app.ui import chat_middleware, ssai_login

    current_company = get_current_company_id()
    original_st = chat_middleware.st
    original_push = chat_middleware.push_sims_result_to_chat
    results: list[dict[str, Any]] = []
    sequence = 0

    def next_seq() -> int:
        nonlocal sequence
        sequence += 1
        return sequence

    set_current_company_id(3)
    try:
        selected_by_id = {case.case_id: case for case in FOCUSED_CASES}
        selected_cases = (
            tuple(selected_by_id[case_id] for case_id in case_ids)
            if case_ids
            else FOCUSED_CASES
        )
        for case in selected_cases:
            if progress_path is not None:
                _write_progress(progress_path, case_id=case.case_id, phase="START", completed=len(results))
            state: dict[str, Any] = {
                "current_room": {"id": f"harness-{case.case_id}", "company": "3", "messages": []},
                "chat_rooms": [], "sims_tables": {}, "sims_export_tables": {},
                "__sims_export_tables_by_key": {}, "__io_pending_product_pick": {},
            }
            room = state["current_room"]
            capture = DeliveryCapture(chat_middleware.wssz)
            chat_middleware.st = SimpleNamespace(session_state=state)
            with (
                patch.object(chat_middleware, "push_sims_result_to_chat", capture.push),
                patch.object(nlq_goods, "push_sims_result_to_chat", capture.push),
                patch.object(nlq_vendors, "push_sims_result_to_chat", capture.push),
                patch.object(ssai_login, "get_selected_company", return_value={"company_id": 3}),
            ):
                try:
                    if case.is_current_table:
                        result = _run_current_table(case, nlq_router, state, room, capture, next_seq)
                    else:
                        result = _run_direct(case, nlq_router, state, room, capture, next_seq)
                except BaseException as exc:
                    LOG.exception("focused harness failure case_id=%s", case.case_id)
                    result = _harness_error_result(case, exc)
            results.append(result)
            _write_csv(output_csv, results)
            if progress_path is not None:
                _write_progress(progress_path, case_id=case.case_id, phase="DONE", completed=len(results))
    finally:
        chat_middleware.st = original_st
        chat_middleware.push_sims_result_to_chat = original_push
        set_current_company_id(current_company)
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company-id", type=int, required=True)
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--progress-json", type=Path)
    parser.add_argument("--case-id", action="append", choices=[case.case_id for case in FOCUSED_CASES])
    args = parser.parse_args()
    if args.company_id != 3 or not args.execute_live:
        raise SystemExit("company-id must be 3 and --execute-live is required")
    rows = run(
        args.output_csv.resolve(), case_ids=tuple(args.case_id or ()),
        progress_path=args.progress_json.resolve() if args.progress_json else None,
    )
    print(json.dumps({"company_id": 3, "rows": len(rows), "pass": sum(row["verdict"] == "PASS" for row in rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
