"""DB-free semantic accuracy checks independent of the Golden casebook."""

from __future__ import annotations

import argparse
import csv
from datetime import date
import logging
from pathlib import Path
import sys
import traceback
from unittest.mock import MagicMock, patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.io_nlq import resolve_io_nlq, resolve_unlabeled_io_entity_condition
from app.services.datetime_tool import operating_now
from app.sims.nlq import nlq_goods
from app.sims.nlq import nlq_router
from app.services.snapshot_product_information_service import normalize_product_information_params
from app.ui.knowledge_chat_adapter import (
    parse_incomplete_sims_help_request,
    parse_sims_query_examples_request,
)
from app.ui.chat_middleware import _build_sims_result_header_view, _sims_non_query_notice
from app.ui import chat_middleware
from app.ui.current_table_followups.action_dispatcher import (
    build_current_table_interpretive_facts,
    classify_current_table_followup_intent,
)
from tools.check_nlq_current_table_feedback_regression import _dispatch_any


def run() -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []

    def record(question: str, expected_action: str, expected_condition: str, actual: str, ok: bool, basis: str) -> None:
        checks.append({
            "question": question, "expected_action": expected_action,
            "expected_condition": expected_condition, "actual_condition": actual,
            "result_status": "offline", "result_invariant": basis,
            "verdict": "PASS" if ok else "FAIL", "evidence": basis,
        })

    def parse(question: str, action: str, required: dict[str, object], forbidden: tuple[str, ...] = (), *, with_entity_resolver: bool = False) -> None:
        parsed = resolve_io_nlq(question, today=date(2026, 10, 8)) or {}
        params = parsed.get("params") or {}
        if with_entity_resolver:
            params = resolve_unlabeled_io_entity_condition(question, action=action, params=params).get("params") or {}
        ok = parsed.get("action") == action and all(params.get(k) == v for k, v in required.items())
        ok = ok and all(not params.get(k) for k in forbidden)
        record(question, action, repr(required), repr({k: params.get(k) for k in (*required, *forbidden)}), ok, "parsed condition retained; forbidden condition absent")

    parse("제품재고장 아스피린 재고위치 본사", "제품재고현황 조회", {"stock_nm": "본사", "nlq_unlabeled_name": "아스피린"}, with_entity_resolver=True)
    parse("제품재고장 재고위치 00001 2026 조회", "제품재고현황 조회", {"stock_cd": "00001"}, ("physic_cd",))
    parse("제품재고현황 재고위치 00001 2026 조회", "제품재고현황 조회", {"stock_cd": "00001"}, ("physic_cd",))
    parse("보험약 제품정보 조회", "제품정보 조회", {"product_di_semantic_group": "insurance"}, ("_product_information_unlabeled_name",))
    parse("비보험약 제품정보 조회", "제품정보 조회", {"product_di_semantic_group": "non_insurance"}, ("_product_information_unlabeled_name",))
    parse("보험약 제품정보조회", "제품정보 조회", {"product_di_semantic_group": "insurance"}, ("_product_information_unlabeled_name",))
    parse("비보험약 제품정보조회", "제품정보 조회", {"product_di_semantic_group": "non_insurance"}, ("_product_information_unlabeled_name",))
    parse("전문약 비보험약 제품정보 조회", "제품정보 조회", {"product_prescription_semantic": "prescription", "product_di_semantic_group": "non_insurance"})
    parse("제품정보 빈도구분 A", "제품정보 조회", {"frequency_grade": "A"}, ("_product_information_unlabeled_name",))
    parse("제품정보 빈도구분 a", "제품정보 조회", {"frequency_grade": "A"}, ("_product_information_unlabeled_name",))
    for query, expected in (("보험약 제품정보 조회", "insurance"), ("비보험약 제품정보 조회", "non_insurance")):
        parsed = resolve_io_nlq(query) or {}
        normalized = normalize_product_information_params(parsed.get("params"))
        record(query + " service", "제품정보 조회", expected,
               str(normalized.get("product_di_semantic_group")),
               normalized.get("product_di_semantic_group") == expected,
               "existing product-master filter contract retains insurance axis")
    normalized_frequency = normalize_product_information_params(
        (resolve_io_nlq("제품정보 빈도구분 a") or {}).get("params")
    )
    record("제품정보 빈도구분 a service", "제품정보 조회", "frequency_grade=A",
           str(normalized_frequency.get("frequency_grade")),
           normalized_frequency.get("frequency_grade") == "A",
           "snapshot projection receives normalized frequency grade")
    parse("오늘입고현황", "입고명세 조회", {"date_from": "20261008", "date_to": "20261008"})
    parse("오늘 입고현황", "입고명세 조회", {"date_from": "20261008", "date_to": "20261008"})
    parse("오늘거래명세서 공통 조회", "거래명세서 공통 조회", {"date_from": "20261008", "date_to": "20261008"})
    parse("입고거래명세서 2026 조회", "거래명세서 공통 조회", {"trans_di": "1", "month_from": "202601", "month_to": "202612"}, ("trans_seq",))
    parse("입고거래명세서순번 2026 조회", "거래명세서 공통 조회", {"trans_seq": "2026"})
    parse("발주 계산해줘", "발주 계산", {}, ("_registered_unlabeled_entity", "physic_nm", "order_vendor_nm"))
    for query in ("발주처별 동제 제품재고장", "제조사별 동제 제품재고장"):
        parse(query, "제품재고현황 조회", {"_ambiguous_grouping_label": True}, ("order_nm", "maker_nm"))
        delivered: list[dict[str, object]] = []
        with (
            patch("app.ui.chat_middleware.push_sims_result_to_chat", side_effect=lambda payload, action: delivered.append(payload)),
            patch("app.services.io_nlq.resolve_unlabeled_io_entity_condition", side_effect=AssertionError("ERP resolver called")),
            patch("app.services.io_nlq.resolve_current_stock_entity_condition", side_effect=AssertionError("ERP resolver called")),
            patch("app.db.mssql_client.get_conn", side_effect=AssertionError("DB connection attempted")) as db_connect,
        ):
            handled = nlq_router._try_handle_io_nlq(
                query, room={"messages": []}, session_state={}, make_ts=lambda: "fixture",
                next_seq=lambda: 1, logger=logging.getLogger(__name__),
            )
        meta = delivered[0].get("meta") if delivered else {}
        status = str((meta or {}).get("result_status") or "")
        message = str(delivered[0].get("message") or "") if delivered else ""
        preferred = "제품재고장 발주처명 동제" if query.startswith("발주처") else "제품재고장 제조사명 동제"
        alternative = "제품재고장 제조사명 동제" if query.startswith("발주처") else "제품재고장 발주처명 동제"
        record(query + " route", "SIMS clarification", "input_required; ERP calls 0", status,
               handled and status == "input_required" and bool((meta or {}).get("input_required"))
               and (meta or {}).get("source_call_count") == 0 and db_connect.call_count == 0
               and preferred in message and alternative in message
               and message.index(preferred) < message.index(alternative)
               and "입력한 검색어: 동제" in message and "확인한 대상" not in message,
               "verified Help examples and fail-closed status before entity resolution")
        notice_meta = dict(meta or {})
        notice_meta.update({"sum_stock_qty": float("nan"), "table_key": "stale-table"})
        notice_header = _build_sims_result_header_view(delivered[0], notice_meta) if delivered else {}
        record(query + " display", "SIMS clarification", "no metrics, no stored result", str(notice_header.get("line1")),
               _sims_non_query_notice(notice_meta)
               and notice_header.get("line1") == "조회 조건 확인 필요"
               and not notice_header.get("followup_available"),
               "non-query result bypasses inventory metrics and current-table affordance")
        fake_ui = MagicMock()
        fake_ui.session_state = {}
        with (
            patch.object(chat_middleware, "st", fake_ui),
            patch.object(chat_middleware, "_should_render_sims_message_once", return_value=True),
            patch.object(chat_middleware, "_render_product_inventory_metrics") as inventory_metrics,
        ):
            chat_middleware._render_chat_item_body(delivered[0])
        rendered = " ".join(str(call.args[0]) for call in fake_ui.markdown.call_args_list if call.args)
        captions = " ".join(str(call.args[0]) for call in fake_ui.caption.call_args_list if call.args)
        record(query + " rendered", "SIMS clarification", "no inventory metrics or false result", (rendered + " | " + captions)[:1000],
               inventory_metrics.call_count == 0 and "nan" not in (rendered + captions).lower()
               and "결과 정보가 저장되어 있습니다" not in (rendered + captions)
               and "입력한 검색어" in rendered,
               "production chat renderer shows only the guidance payload")

    success_meta = {"result_status": "success", "row_count": 1, "table_key": "stock-table"}
    success_header = _build_sims_result_header_view(
        {"action": "제품재고현황 조회"}, success_meta, pd.DataFrame({"재고수량": [3]})
    )
    record("제품재고장 정상 조회 display", "제품재고현황 조회", "result summary retained",
           str(success_header.get("line1")),
           not _sims_non_query_notice(success_meta)
           and success_header.get("line1") == "결과: 1건"
           and bool(success_header.get("followup_available")),
           "successful inventory lookup retains result header, metrics branch, and follow-up")
    fake_ui = MagicMock()
    fake_ui.session_state = {}
    with (
        patch.object(chat_middleware, "st", fake_ui),
        patch.object(chat_middleware, "_should_render_sims_message_once", return_value=True),
        patch.object(chat_middleware, "_render_product_inventory_metrics") as inventory_metrics,
    ):
        chat_middleware._render_chat_item_body({
            "type": "text", "action": "제품재고현황 조회", "message": "정상 조회",
            "meta": success_meta,
        })
    record("제품재고장 정상 조회 rendered", "제품재고현황 조회", "inventory metrics retained",
           str(inventory_metrics.call_count), inventory_metrics.call_count == 1,
           "successful lookup still enters the existing inventory metrics renderer")

    seen: list[dict[str, object]] = []
    with (
        patch.object(nlq_goods, "search_goods_full", side_effect=lambda **kw: (seen.append(kw) or pd.DataFrame())),
        patch.object(nlq_goods, "push_sims_result_to_chat", return_value=True),
    ):
        for query in ("제품 조회", "제품코드 조회"):
            nlq_goods.try_handle_goods_nlq(
                query, room={"messages": []}, session_state={}, make_ts=lambda: "fixture",
                next_seq=lambda: 1, logger=logging.getLogger(__name__),
            )
    comparable = len(seen) == 2 and all(seen[0].get(k) == seen[1].get(k) for k in ("keyword", "only_use", "top"))
    record("제품 조회 / 제품코드 조회", "제품코드 목록", "same unfiltered source params", repr([(x.get("keyword"), x.get("only_use"), x.get("top")) for x in seen]), comparable, "same action must not acquire hidden keyword")

    inventory = pd.DataFrame({"제조사명": ["동제", "한미"], "제품분류명": ["일반", "전문"], "제품명": ["A", "B"], "재고수량": [3, 4]})
    for dimension in ("제조사", "제품분류"):
        base = f"현재표 {dimension}별 재고수량 집계"
        pair = [base, base + "해줘"]
        outputs = [_dispatch_any(inventory, query, "제품재고현황 조회") for query in pair]
        frames = [payload.get("df") for kind, payload in outputs if kind == "table"]
        ok = len(frames) == 2 and frames[0].equals(frames[1]) and "재고수량" in frames[0].columns
        record(" / ".join(pair), "현재표 집계", "same manufacturer/class and stock quantity", repr([kind for kind, _ in outputs]), ok, "command suffix cannot become filter value")

    goods = pd.DataFrame({"제품코드": ["00001", "00002"], "구분명": ["보험", "비보험"]})
    group_kind, group_payload = _dispatch_any(goods, "현재표 제품구분별 집계", "제품코드 목록")
    facts = build_current_table_interpretive_facts(
        df=goods, query="현재표 제품구분별 분석", source_action="제품코드 목록",
    )
    ok = (
        group_kind == "table" and classify_current_table_followup_intent("현재표 제품구분별 분석") == "llm_analysis"
        and facts.get("status") == "success" and facts.get("whole_table_facts", {}).get("group_count") == 2
    )
    record("현재표 제품구분별 집계 / 분석", "현재표 제품구분별 분석", "existing 구분명 dimension", str(facts.get("status")), ok, "analysis uses bounded whole-table facts, not displayed row preview")
    for query in ("SIMS 조회 예시 알려줘", "심스 조회 예시 알려줘"):
        help_match = parse_sims_query_examples_request(query)
        record(query, "SIMS local help", "help before RAG", str(help_match), help_match,
               "local deterministic help intent; no ERP or LLM call")
    for query in ("SIMS 일일점검", "심스 일일점검", "현재고 제조사명 한미", "제품정보 조회"):
        intercepted = parse_sims_query_examples_request(query) or parse_incomplete_sims_help_request(query) is not None
        record(query, "normal NLQ", "no help interception", str(intercepted), not intercepted,
               "normal NLQ must continue to production router")

    service_targets = (
        ("발주 계산해줘", "발주 계산", "app.services.order_calculation_service.get_order_calculation_result"),
        ("오늘거래명세서 공통 조회", "거래명세서 공통 조회", "app.services.rddbc130_service.get_rddbc130_result"),
        ("오늘입고현황", "입고명세 조회", "app.services.rddbc110_service.get_rddbc110_result"),
        ("입고거래명세서 2026 조회", "거래명세서 공통 조회", "app.services.rddbc130_service.get_rddbc130_result"),
    )
    for query, expected_action, service_target in service_targets:
        called: list[dict[str, object]] = []
        delivered: list[dict[str, object]] = []
        db_attempts: list[str] = []

        def block_db(*args: object, **kwargs: object) -> None:
            db_attempts.append(" > ".join(frame.name for frame in traceback.extract_stack(limit=16)))
            raise AssertionError("DB connection attempted")

        def service_stub(params: dict[str, object]) -> dict[str, object]:
            called.append(dict(params))
            return {"action": expected_action, "params": dict(params), "data": "fixture",
                    "meta": {"result_status": "success", "row_count": 0, "source_call_count": 0}}

        with (
            patch(service_target, side_effect=service_stub),
            patch.object(nlq_router, "_get_trans_doc_full_summary", return_value={"row_count_total": 0}),
            patch("app.ui.chat_middleware.push_sims_result_to_chat", side_effect=lambda payload, action: delivered.append(payload)),
            patch("app.db.mssql_client.get_conn", side_effect=block_db) as db_connect,
        ):
            handled = nlq_router.try_handle_nlq(
                query, room={"messages": []}, session_state={}, make_ts=lambda: "fixture",
                next_seq=lambda: 1, logger=logging.getLogger(__name__),
            )
        params = called[0] if called else {}
        today_key = operating_now().date().strftime("%Y%m%d")
        date_ok = (params.get("date_from") == today_key and params.get("date_to") == today_key) if query.startswith("오늘") else True
        entity_ok = not any(str(params.get(key) or "").strip() in {"해줘", "오늘", "거래명세서", "입고현황"}
                            for key in ("nlq_unlabeled_name", "_registered_unlabeled_entity", "physic_nm", "ven_nm", "order_vendor_nm"))
        sequence_ok = not params.get("trans_seq") if query == "입고거래명세서 2026 조회" else True
        delivered_ok = bool(delivered) and delivered[-1].get("action") == expected_action and str(
            (delivered[-1].get("meta") or {}).get("result_status")
        ) == "success"
        success = handled and len(called) == 1 and delivered_ok and db_connect.call_count == 0 and date_ok and entity_ok and sequence_ok
        record(query + " production route", expected_action, "one service call; no command/date entity",
               repr({"calls": len(called), "date_from": params.get("date_from"), "date_to": params.get("date_to"),
                     "entity": {key: params.get(key) for key in ("nlq_unlabeled_name", "physic_nm", "ven_nm")},
                     "handled": handled, "delivery": [(str(item.get("action")), str((item.get("meta") or {}).get("result_status"))) for item in delivered],
                     "db_attempts": db_attempts}),
               success, "actual production router to mocked service boundary; DB connection prohibited")
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rows = run()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    for row in rows:
        print(row["verdict"], row["question"], row["actual_condition"])
    print(f"PASS={sum(row['verdict'] == 'PASS' for row in rows)} FAIL={sum(row['verdict'] == 'FAIL' for row in rows)}")
    return int(any(row["verdict"] != "PASS" for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
