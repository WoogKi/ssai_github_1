# -*- coding: utf-8 -*-
"""NLQ 사례집 new_query REVIEW 19건의 DB 없는 라우팅/계약 점검."""
from __future__ import annotations

import importlib
import io
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd


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

    codes = importlib.import_module("app.sims.nlq.nlq_codes")
    chat = importlib.import_module("app.ui.chat_middleware")
    raw = pd.DataFrame({"Rd01_Tcode": [f"{n:05d}" for n in range(75)]})
    view = pd.DataFrame({"항목코드": raw["Rd01_Tcode"], "한글명": ["재고위치"] * 75})
    with patch.object(codes, "push_sims_result_to_chat") as pushed:
        codes._push_codes_result(
            txt="업무코드 재고위치 조회", title="코드명 검색", action="코드명 검색",
            df=raw, df_display=view, params_out={"그룹코드": "0018"},
            query_summary="재고위치", source="업무코드마스터(Rddbc010)",
        )
    result = pushed.call_args.args[0]
    exported = result.get("df")
    csv_file, excel_file = chat._make_table_downloads(exported)
    csv_rows = pd.read_csv(io.BytesIO(csv_file.getvalue()), dtype=str)
    excel_rows = pd.read_excel(io.BytesIO(excel_file.getvalue()), dtype=str)
    if (
        isinstance(exported, pd.DataFrame)
        and len(exported) == result["meta"]["row_count_total"] == 75
        and chat._export_candidate_covers_display(exported, result["df_display"])
        and list(exported.columns) == list(result["df_display"].columns)
        and exported["항목코드"].tolist() == view["항목코드"].tolist()
        and csv_rows["항목코드"].tolist() == excel_rows["항목코드"].tolist() == view["항목코드"].tolist()
    ):
        passed += 1
        print("[PASS] 업무코드 재고위치 조회: 전체 75행 CSV/Excel 원본 보존")
    else:
        failures.append("업무코드 다운로드 원본과 화면 결과가 일치하지 않습니다")

    room = {"id": "codes-export-fixture", "history": [], "messages": []}
    state = {"chat_rooms": [room], "current_room": room["id"]}
    with patch.object(chat.st, "session_state", state), \
         patch.object(chat, "maybe_create_structured_presentation", return_value={"status": "skipped"}), \
         patch.object(chat, "_append_finalized_nlq_case"), \
         patch.object(codes, "push_sims_result_to_chat", side_effect=chat.push_sims_result_to_chat):
        codes._push_codes_result(
            txt="업무코드 재고위치 조회", title="코드명 검색", action="코드명 검색",
            df=raw, df_display=view, params_out={"그룹코드": "0018"},
            query_summary="재고위치", source="업무코드마스터(Rddbc010)",
        )
        history_item = room["history"][-1]
        history_meta = history_item["meta"]
        table_key = history_meta["table_key"]
        display_source = state["sims_tables"][table_key]
        full_source = state["sims_export_tables"][table_key]
        resolved = chat._resolve_payload_full_download_source(
            history_item, history_meta, display_df=display_source,
        )
        if (
            state["__sims_current_table_source_key"] == table_key
            and len(display_source) == len(full_source) == 75
            and history_meta["download_row_count"] == 75
            and resolved["source_status"] == "complete_display"
            and resolved["source_rows"] == 75
            and resolved["df"].equals(view)
        ):
            passed += 1
            print("[PASS] 업무코드 재고위치 조회: 채팅 보관소·현재표·다운로드 원본 75행")
        else:
            failures.append("업무코드 채팅 보관소의 다운로드 원본이 화면 75행과 다릅니다")

    full = pd.DataFrame({"제품코드": [f"{n:05d}" for n in range(9000)], "전체원본열": range(9000)})
    preview = full.head(300)[["제품코드"]]
    if (
        list(chat._download_prompt_columns(full, preview)) == list(full.columns)
        and list(chat._download_prompt_columns(None, preview)) == list(preview.columns)
        and len(full) == 9000 and len(preview) == 300
    ):
        passed += 1
        print("[PASS] 이전/대형표: 전체 원본 열 유지, 원본 없음은 안전한 표시 열")
    else:
        failures.append("대형표 원본 누락 시 다운로드 안내 열 처리 오류")

    old_item = {"action": "발주조회", "table_key": "old-order", "df": full}
    old_meta = {"action": "발주조회", "table_key": "old-order", "row_count_total": 9000,
                "download_column_count": 2}
    current_item = {"action": "코드명 검색", "table_key": "current-codes", "df": view}
    current_meta = {"action": "코드명 검색", "table_key": "current-codes", "row_count_total": 75,
                    "download_column_count": 2}
    with patch.object(chat.st, "session_state", {}):
        old_source = chat._resolve_payload_full_download_source(old_item, old_meta, display_df=preview)
        current_source = chat._resolve_payload_full_download_source(current_item, current_meta, display_df=view)
        missing_source = chat._resolve_payload_full_download_source(
            {"action": "발주조회", "table_key": "old-order"}, old_meta, display_df=preview,
        )
    if (
        old_source["source_status"] == "full" and old_source["source_rows"] == 9000
        and current_source["source_status"] == "complete_display" and current_source["source_rows"] == 75
        and missing_source["df"] is None and missing_source["source_status"] != "full"
    ):
        passed += 1
        print("[PASS] 현재표·이전표·대형표: 각자의 전체 원본 사용, 유실 원본을 300행으로 대체하지 않음")
    else:
        failures.append("현재표/이전표/대형표 다운로드 원본 경계 오류")

    captions = []
    with patch.object(chat, "_select_sims_analysis_ctx_for_table", return_value=({}, "fixture")), \
         patch.object(chat, "st", SimpleNamespace(
             columns=lambda n: tuple(nullcontext() for _ in range(n)),
             caption=captions.append, button=lambda *a, **k: False,
         )):
        chat._render_sims_result_actions_lazy(
            key_suffix="missing-history", table_key="old-order", download_df=None,
            csv_name="old.csv", xlsx_name="old.xlsx", prompt="", expected_rows=9000,
            display_rows=300, source_status="display_only_partial",
        )
    if captions == ["전체 다운로드 원본을 확인할 수 없습니다. 조회를 다시 실행해 주세요."]:
        passed += 1
        print("[PASS] 이전표 원본 유실: 300행을 전체 파일로 제공하지 않고 안내")
    else:
        failures.append("원본 유실 이전표에 다운로드 차단 안내가 표시되지 않습니다")

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
