from __future__ import annotations

import logging
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fixture(*, zero_latest: bool = True) -> pd.DataFrame:
    latest_value = 0 if zero_latest else 1200
    return pd.DataFrame(
        [
            {
                "단가적용거래처": "50002",
                "단가적용처명": "테스트거래처",
                "제품코드": "00085",
                "제품명": "테스트제품",
                "제약사": "테스트제약",
                "제품그룹": "일반",
                "제품구분코드": "03",
                "제품구분": "전문약(보험)",
                "제품분류": "전문의약품",
                "보험단가": 1350,
                "등록자": "00128",
                "등록자명": "등록사용자",
                "수정자": "00123",
                "수정자명": "수정사용자",
                "계약시작일자": "20260901",
                "실입고단가": latest_value,
                "장부입고단가": latest_value,
                "otc 입고단가": latest_value,
                "실줄고단가": latest_value,
                "장부출고단가": latest_value,
                "otc 출고단가": latest_value,
                "삭제 flag": "",
            }
        ]
    )


def _find_col(df, exact=(), include_any=(), exclude_any=()):
    for name in exact:
        if name in df.columns:
            return name
    for column in df.columns:
        text = str(column)
        if include_any and not any(token in text for token in include_any):
            continue
        if any(token in text for token in exclude_any):
            continue
        return text
    return ""


def main() -> int:
    failures: list[str] = []

    from app.services import rddbc070_service as service
    from app.services.erp_table_nlq import resolve_registered_erp_table_nlq
    from app.services.erp_table_query_service import registered_query_limits
    from app.services.io_nlq import apply_nlq_default_period_policy, get_nlq_period_action_class
    from app.services.ssai_permission_policy import get_required_permission
    from app.sims.meta.erp_table_feature_registry import (
        get_action_spec,
        menu_actions,
        registry_completeness_errors,
        resolve_dotted_callable,
    )
    from app.ui.current_table_followups.action_dispatcher import (
        detect_current_table_kind,
        handle_current_table_followup_by_action,
    )
    from app.sims.views.rddbc_io_shared import (
        _build_io_banding_df,
        _build_io_display_styler,
        _build_io_negative_df,
        _prepare_io_display_df,
    )

    if registry_completeness_errors():
        failures.append(f"registry incomplete: {registry_completeness_errors()}")
    runtime_io_limit = int(os.getenv("SIMS_IO_QUERY_MAX_ROWS", "30000"))
    runtime_export_limit = int(os.getenv("SIMS_EXPORT_MAX_ROWS", "100000"))
    runtime_chat_limit = int(os.getenv("SIMS_CHAT_DISPLAY_MAX_ROWS", "300"))
    with patch.dict(
        "os.environ",
        {
            "SIMS_PANEL_DISPLAY_MAX_ROWS": "1000",
            "SIMS_CHAT_DISPLAY_MAX_ROWS": str(runtime_chat_limit),
            "SIMS_EXPORT_MAX_ROWS": str(runtime_export_limit),
            "SIMS_IO_QUERY_MAX_ROWS": str(runtime_io_limit),
        },
    ):
        panel_limits = registered_query_limits({"_display_context": "panel"})
        chat_limits = registered_query_limits({"_display_context": "chat"})
        top20_limits = registered_query_limits(
            {"_display_context": "chat", "display_top": 20}
        )
        top999_limits = registered_query_limits(
            {"_display_context": "chat", "display_top": 999}
        )
        r230_runtime_limits = registered_query_limits(
            {"_display_context": "chat"},
            source_limit_env="SIMS_EXPORT_MAX_ROWS",
        )
    if (panel_limits.display_top, chat_limits.display_top) != (1000, runtime_chat_limit):
        failures.append(f"panel/chat display env contract mismatch: {panel_limits}/{chat_limits}")
    if top20_limits.display_top != 20 or top20_limits.source_top != runtime_io_limit:
        failures.append(f"NLQ TOP changed source limit: {top20_limits}")
    if top999_limits.display_top != min(999, runtime_chat_limit):
        failures.append(f"NLQ TOP did not clamp to chat cap: {top999_limits}")
    r070_feature = get_action_spec("최종 계약단가 조회")
    r230_feature = get_action_spec("최종 매입단가 조회")
    if not r070_feature or r070_feature[0].source_limit_env != "SIMS_IO_QUERY_MAX_ROWS":
        failures.append("R070 must select SIMS_IO_QUERY_MAX_ROWS")
    if not r230_feature or r230_feature[0].source_limit_env != "SIMS_EXPORT_MAX_ROWS":
        failures.append("R230 must select its registry-owned SIMS_EXPORT_MAX_ROWS")
    if (
        panel_limits.source_top != runtime_io_limit
        or chat_limits.source_top != runtime_io_limit
        or panel_limits.source_limit_source != "SIMS_IO_QUERY_MAX_ROWS"
        or r230_runtime_limits.source_top != runtime_export_limit
        or r230_runtime_limits.source_limit_source != "SIMS_EXPORT_MAX_ROWS"
    ):
        failures.append(
            "runtime env limit resolution mismatch: "
            f"panel={panel_limits}, chat={chat_limits}, r230={r230_runtime_limits}"
        )
    with patch.dict(
        "os.environ",
        {
            "SIMS_EXPORT_MAX_ROWS": "87654",
            "SIMS_IO_QUERY_MAX_ROWS": "43210",
        },
    ):
        io_key_probe = registered_query_limits({})
        export_key_probe = registered_query_limits({}, source_limit_env="SIMS_EXPORT_MAX_ROWS")
    if io_key_probe.source_top != 43210 or export_key_probe.source_top != 87654:
        failures.append(
            f"source env keys are not independently resolved: io={io_key_probe}, export={export_key_probe}"
        )
    for action in ("최종 계약단가 조회", "계약단가 이력 조회"):
        registered = get_action_spec(action)
        if not registered:
            failures.append(f"registry action missing: {action}")
            continue
        for target in (
            registered[1].service_function,
            registered[1].export_function,
            registered[1].view_function,
        ):
            try:
                resolve_dotted_callable(target)
            except Exception as exc:
                failures.append(f"handler import failed: {target}: {type(exc).__name__}")
        if get_required_permission(action=action) != "IO_READ":
            failures.append(f"permission mismatch: {action}")
        if registered[1].nlq_period_policy != "explicit_only":
            failures.append(f"registered explicit-only period policy missing: {action}")
        if get_nlq_period_action_class(action) != "explicit_only":
            failures.append(f"resolved explicit-only period policy missing: {action}")

    product_menu = menu_actions(business_group="마스터관리", target="제품")
    for action in ("최종 계약단가 조회", "계약단가 이력 조회"):
        if product_menu.count(action) != 1:
            failures.append(f"three-level product menu count mismatch: {action}={product_menu.count(action)}")
        registered = get_action_spec(action)
        if not registered or registered[0].category != "제품":
            failures.append(f"panel/handler category mismatch: {action}")
    if "현재 계약단가 조회" in product_menu or "계약단가 조회" in product_menu:
        failures.append(f"legacy contract-price labels remain exposed in menu: {product_menu}")
    legacy_current = get_action_spec("현재 계약단가 조회")
    generic_contract = get_action_spec("계약단가 조회")
    if (
        not legacy_current
        or legacy_current[1].action != "최종 계약단가 조회"
        or not generic_contract
        or generic_contract[1].action != "최종 계약단가 조회"
    ):
        failures.append("legacy/generic contract-price aliases do not resolve to final action")
    from app.sims.config import ACTIONS_BY_CATEGORY
    from app.sims.nlq.action_inventory import implemented_actions
    from app.ui import sims_entry

    for action in ("최종 계약단가 조회", "계약단가 이력 조회"):
        if ACTIONS_BY_CATEGORY.get("제품", []).count(action) != 1:
            failures.append(f"hub product menu count mismatch: {action}")
        if sum(
            1
            for item in implemented_actions()
            if item.canonical_action == action and item.panel_category == "제품"
        ) != 1:
            failures.append(f"NLQ action inventory mismatch: {action}")
    sidebar_source = Path(sims_entry.__file__).read_text(encoding="utf-8")
    if 'business_group="마스터관리"' not in sidebar_source or "menu_actions(" not in sidebar_source:
        failures.append("Panel(A) sidebar does not consume registry menu placement")
    if '"제품코드 목록"' not in sidebar_source or '"제품코드 상세"' not in sidebar_source:
        failures.append("existing product master menu changed")

    nlq_cases = (
        ("계약단가 조회", "최종 계약단가 조회", {}),
        ("계약단가조회", "최종 계약단가 조회", {}),
        ("계약 단가 조회", "최종 계약단가 조회", {}),
        ("단가계약조회", "최종 계약단가 조회", {}),
        ("단가 계약 조회", "최종 계약단가 조회", {}),
        ("계약 단가", "최종 계약단가 조회", {}),
        ("아모틴 계약단가 조회", "최종 계약단가 조회", {"physic_nm": "아모틴"}),
        ("아모틴 단가계약 조회", "최종 계약단가 조회", {"physic_nm": "아모틴"}),
        ("제품명 아모틴 계약단가 조회", "최종 계약단가 조회", {"physic_nm": "아모틴"}),
        ("신제품A 계약단가 조회", "최종 계약단가 조회", {"physic_nm": "신제품A"}),
        ("제품구분 전문 계약단가 조회", "최종 계약단가 조회", {"product_di_nm": "전문"}),
        ("전문약 계약단가 조회", "최종 계약단가 조회", {"product_di_semantic_group": "insurance"}),
        ("전문제품 계약단가 조회", "최종 계약단가 조회", {"product_di_semantic_group": "insurance"}),
        ("보험약 계약단가 조회", "최종 계약단가 조회", {"product_di_semantic_group": "insurance"}),
        ("보험제품 계약단가 조회", "최종 계약단가 조회", {"product_di_semantic_group": "insurance"}),
        ("전문의약품 계약단가 조회", "최종 계약단가 조회", {"product_di_semantic_group": "insurance"}),
        ("ETC 계약단가 조회", "최종 계약단가 조회", {"product_di_semantic_group": "insurance"}),
        ("일반약 계약단가 조회", "최종 계약단가 조회", {"product_di_semantic_group": "non_insurance"}),
        ("일반제품 계약단가 조회", "최종 계약단가 조회", {"product_di_semantic_group": "non_insurance"}),
        ("비보험약 계약단가 조회", "최종 계약단가 조회", {"product_di_semantic_group": "non_insurance"}),
        ("비보험제품 계약단가 조회", "최종 계약단가 조회", {"product_di_semantic_group": "non_insurance"}),
        ("일반의약품 계약단가 조회", "최종 계약단가 조회", {"product_di_semantic_group": "non_insurance"}),
        ("OTC 계약단가 조회", "최종 계약단가 조회", {"product_di_semantic_group": "non_insurance"}),
        (
            "일반약 제약사 삼진 계약단가 조회",
            "최종 계약단가 조회",
            {"maker_nm": "삼진", "product_di_semantic_group": "non_insurance"},
        ),
        (
            "전문약 제약사 삼진 계약단가 조회",
            "최종 계약단가 조회",
            {"maker_nm": "삼진", "product_di_semantic_group": "insurance"},
        ),
        ("제약사 동제 단가계약 조회", "최종 계약단가 조회", {"maker_nm": "동제"}),
        ("제약사 동제 계약단가 조회", "최종 계약단가 조회", {"maker_nm": "동제"}),
        ("제품명 우루사 계약단가", "최종 계약단가 조회", {"physic_nm": "우루사"}),
        ("단가적용처 한미 계약단가 조회", "최종 계약단가 조회", {"ven_nm": "한미"}),
        ("단가적용처명 한미 계약단가 조회", "최종 계약단가 조회", {"ven_nm": "한미"}),
        ("단가적용거래처 한미 계약단가 조회", "최종 계약단가 조회", {"ven_nm": "한미"}),
        ("단가적용거래처명 한미 계약단가 조회", "최종 계약단가 조회", {"ven_nm": "한미"}),
        ("계약단가 이력 조회 거래처명 동제 제품명 우루사", "계약단가 이력 조회", {"ven_nm": "동제", "physic_nm": "우루사"}),
        ("계약 변경내역 거래처명 동제", "계약단가 이력 조회", {"ven_nm": "동제"}),
        ("과거 계약단가 제품명 우루사", "계약단가 이력 조회", {"physic_nm": "우루사"}),
        ("과거 아모틴 계약단가 조회", "계약단가 이력 조회", {"physic_nm": "아모틴"}),
        ("아모틴 계약단가 이력 조회", "계약단가 이력 조회", {"physic_nm": "아모틴"}),
        ("최종 계약단가 조회 기준일 20260901 거래처코드 50002 제품코드 00085", "최종 계약단가 조회", {"as_of": "20260901", "ven_cd": "50002", "physic_cd": "00085"}),
        ("기준일 현재 계약단가 조회 기준일 2026-09-01", "최종 계약단가 조회", {"as_of": "20260901"}),
        (
            "계약단가 조회 제약사 동아 제품그룹 일반약 제품분류 전문 보험코드 1234567890 바코드 880123",
            "최종 계약단가 조회",
            {
                "maker_nm": "동아", "product_group_nm": "일반약",
                "product_class_nm": "전문", "insu_cd": "1234567890", "barcode": "880123",
            },
        ),
        ("계약단가 TOP 20", "최종 계약단가 조회", {"display_top": 20}),
    )
    for text, expected_action, expected_params in nlq_cases:
        parsed = resolve_registered_erp_table_nlq(text)
        if not parsed or parsed.get("action") != expected_action:
            failures.append(f"NLQ action mismatch: {text}: {parsed}")
            continue
        for key, value in expected_params.items():
            if parsed.get("params", {}).get(key) != value:
                failures.append(f"NLQ param mismatch: {text}: {key}={parsed.get('params', {}).get(key)!r}")
        if "거래처코드" in text and parsed.get("params", {}).get("ven_nm"):
            failures.append(f"NLQ vendor code leaked into name filter: {text}: {parsed}")
        if "제품코드" in text and parsed.get("params", {}).get("physic_nm"):
            failures.append(f"NLQ product code leaked into name filter: {text}: {parsed}")

    default_as_of = resolve_registered_erp_table_nlq(
        "계약단가 조회", today=pd.Timestamp("2026-09-06").date()
    )
    if default_as_of is None or default_as_of.get("params", {}).get("as_of") != "20260906":
        failures.append(f"final contract default as-of mismatch: {default_as_of}")
    for text in ("단가적용처 한미 계약단가 조회", "제품명 에멘드 계약단가 조회"):
        parsed = resolve_registered_erp_table_nlq(text, today=pd.Timestamp("2026-09-06").date())
        period_params, period_policy = apply_nlq_default_period_policy(
            parsed.get("params", {}), parsed.get("action", ""),
            today=pd.Timestamp("2026-09-06").date(),
        )
        if any(period_params.get(key) for key in ("date_from", "date_to", "month_from", "month_to")):
            failures.append(f"R070 NLQ invented a rolling period: {text}: {period_params}")
        if period_params.get("as_of") != "20260906" or period_policy.get("auto_applied"):
            failures.append(f"R070 as-of/explicit-only policy mismatch: {text}: {period_params}/{period_policy}")

    explicit_history = resolve_registered_erp_table_nlq("2025년 계약단가 이력")
    if not explicit_history or explicit_history.get("params", {}).get("date_from") != "20250101" or explicit_history.get("params", {}).get("date_to") != "20251231":
        failures.append(f"explicit history year period mismatch: {explicit_history}")

    from app.sims.nlq.nlq_router import _is_explicit_io_nlq_phrase

    for text in ("제약사 동제 단가계약 조회", "제품명 우루사 계약단가"):
        if not _is_explicit_io_nlq_phrase(text):
            failures.append(f"registered contract-price intent did not precede product routing: {text}")

    from app.sims.nlq import nlq_router

    action_only_pushes: list[dict] = []
    with (
        patch.object(service, "execute_bound_select", return_value=_fixture(zero_latest=False)),
        patch(
            "app.services.io_nlq.resolve_unlabeled_io_entity_condition",
            side_effect=AssertionError("registered action-only query called the generic entity resolver"),
        ),
        patch(
            "app.ui.chat_middleware.push_sims_result_to_chat",
            side_effect=lambda payload, *_args, **_kwargs: action_only_pushes.append(payload),
        ),
    ):
        action_only_handled = nlq_router._try_handle_io_nlq(
            "계약단가 조회",
            room={"messages": []},
            session_state={},
            make_ts=lambda: "2026-09-07 00:00:00",
            next_seq=lambda: 1,
            logger=logging.getLogger("r070.action-only.gate"),
        )
    if not action_only_handled or len(action_only_pushes) != 1:
        failures.append(
            "registered action-only contract query did not execute its default list service: "
            f"handled={action_only_handled}, pushes={len(action_only_pushes)}"
        )

    from app.ui import chat_middleware
    from app.ui.sims_panel import _panel_payload_action

    source_df = pd.concat([_fixture(zero_latest=False)] * 366, ignore_index=True)
    display_df = source_df.head(300).copy()
    source_key = "sims_r070_action_owner"
    source_meta = {
        "table_key": source_key,
        "action": "최종 계약단가 조회",
        "row_count_total": len(source_df),
        "display_row_count": len(display_df),
        "source_call_count": 1,
    }
    source_item = {
        "type": "table",
        "action": "최종 계약단가 조회",
        "df": source_df,
        "df_display": display_df,
        "meta": source_meta,
    }
    if _panel_payload_action(source_item, "최종 매입단가 조회") != "최종 계약단가 조회":
        failures.append("panel action selection overrode the payload-owned canonical action")
    if chat_middleware._canonical_sims_action(
        source_item,
        source_meta,
        "최종 매입단가 조회",
    ) != "최종 계약단가 조회":
        failures.append("chat push action selection overrode the payload-owned canonical action")

    fake_st = SimpleNamespace(
        session_state={
            "sims_export_tables": {source_key: source_df},
            "__sims_export_tables_by_key": {source_key: source_df},
            "__sims_export_table_provenance_by_key": {
                source_key: {
                    "table_key": source_key,
                    "action": "최종 계약단가 조회",
                    "rows": len(source_df),
                }
            },
        }
    )
    contaminated_item = dict(source_item)
    contaminated_item["action"] = "최종 매입단가 조회"
    contaminated_item["meta"] = dict(source_meta, action="최종 매입단가 조회")
    with (
        patch.object(chat_middleware, "st", fake_st),
        patch.object(
            service,
            "get_rddbc070_current_export_df",
            side_effect=AssertionError("R070 source queried twice"),
        ),
        patch(
            "app.services.rddbc230_service.get_rddbc230_export_df",
            side_effect=AssertionError("R070 source was reclassified as R230"),
        ),
    ):
        reused_sources = [
            chat_middleware._get_full_download_df_for_sims_item(
                contaminated_item,
                contaminated_item["meta"],
                display_df,
            )
            for _ in ("excel", "csv")
        ]
    reused_source = reused_sources[-1]
    if not isinstance(reused_source, pd.DataFrame) or len(reused_source) != len(source_df):
        failures.append("verified R070 full source was not reused by table_key")
    if contaminated_item["meta"].get("source_call_count") != 1:
        failures.append("full-source reuse changed source_call_count")
    final_provenance = fake_st.session_state["__sims_export_table_provenance_by_key"][source_key]
    if (
        final_provenance.get("action") != "최종 계약단가 조회"
        or final_provenance.get("table_key") != source_key
    ):
        failures.append(f"R070 table ownership provenance changed: {final_provenance}")

    captured: list[tuple[str, tuple]] = []

    def _capture(sql: str, values) -> pd.DataFrame:
        captured.append((sql, tuple(values)))
        return _fixture(zero_latest=False)

    with patch.object(service, "execute_bound_select", side_effect=_capture):
        current = service.get_rddbc070_current_result(
            {"ven_cd": "50002", "physic_cd": "00085", "as_of": "20260901", "top": 1}
        )
    if len(captured) != 1:
        failures.append(f"current physical call count expected=1 got={len(captured)}")
    else:
        sql, values = captured[0]
        required_sql = (
            "ROW_NUMBER() OVER",
            "PARTITION BY C.Rd07_Cost_Apply_Cd, C.Rd07_Physic_Cd",
            "ORDER BY C.Rd07_Start_Date DESC",
            "C.Rd07_Start_Date <= ?",
            "C.Rd07_Del_Flag <> 'E'",
            "LTRIM(RTRIM(C.Rd07_Cost_Apply_Cd)) <> ''",
            "LTRIM(RTRIM(C.Rd07_Start_Date)) <> '00000000'",
            "C.Rd07_In_Amt = 0",
            "C.Rd07_Out_Oamt = 0",
            "R.__rn = 1",
            "CAST(C.Rd07_Cost_Apply_Cd AS VARCHAR(50))",
            "LEFT JOIN dbo.Rddbc030 AS PV",
            "LEFT JOIN dbo.Rddbc010 AS PG",
            "LEFT JOIN dbo.Rddbc010 AS PD",
            "LEFT JOIN dbo.Rddbc010 AS PC",
        )
        if any(token not in sql for token in required_sql):
            failures.append("current latest/delete SQL contract missing")
        if "{_PRODUCT_MASTER_JOINS}" in sql or "{_PRODUCT_MASTER_JOINS}" in service._JOINS:
            failures.append("current SQL contains unresolved product-master JOIN placeholder")
        if sql.count("LTRIM(RTRIM(C.Rd07_Cost_Apply_Cd)) <> ''") != 1:
            failures.append("trim-empty cost-apply candidate exclusion missing or duplicated")
        if not all(
            f"C.{column} = 0" in sql
            for column in (
                "Rd07_In_Amt", "Rd07_In_Ramt", "Rd07_In_Oamt",
                "Rd07_Out_Amt", "Rd07_Out_Ramt", "Rd07_Out_Oamt",
            )
        ):
            failures.append("all-six-zero candidate exclusion missing")
        if "50002" in sql or "00085" in sql or "20260901" in sql:
            failures.append("user values were embedded in SQL instead of bound")
        if values[:3] != ("50002", "00085", "20260901"):
            failures.append(f"bound value order mismatch: {values}")
        if sql.index("ROW_NUMBER() OVER") > sql.rindex("SELECT TOP"):
            failures.append("current source cap was applied before latest-contract ranking")
        price_tokens = [
            "AS [실입고단가]", "AS [장부입고단가]", "AS [otc 입고단가]",
            "AS [실줄고단가]", "AS [장부출고단가]", "AS [otc 출고단가]",
            "AS [보험단가]",
        ]
        if [sql.index(token) for token in price_tokens] != sorted(sql.index(token) for token in price_tokens):
            failures.append("R070 price/insurance display column order mismatch")

    current_df = current.get("df")
    if not isinstance(current_df, pd.DataFrame) or current_df.empty:
        failures.append("current result dataframe missing")
    else:
        row = current_df.iloc[0]
        if list(current_df["조회순번"]) != [1]:
            failures.append(f"current display row number mismatch: {list(current_df['조회순번'])}")
        if row.get("보험단가") != 1350:
            failures.append(f"current insurance price projection missing: {row.get('보험단가')}")
        if row.get("계약상태") != "단가 있음":
            failures.append(f"non-garbage current contract state mismatch: {row.get('계약상태')}")
        expected_prefix = [
            "조회순번", "단가적용거래처", "단가적용처명", "제품코드", "제품명",
            "제약사", "제품그룹", "제품구분코드", "제품구분", "제품분류", "계약시작일자",
        ]
        if list(current_df.columns[: len(expected_prefix)]) != expected_prefix:
            failures.append(f"R070 identifier/date display order mismatch: {list(current_df.columns)}")
        if (
            row.get("등록자") != "00128"
            or row.get("등록자명") != "등록사용자"
            or row.get("수정자") != "00123"
            or row.get("수정자명") != "수정사용자"
        ):
            failures.append(f"R070 contract audit projection mismatch: {row.to_dict()}")
        if row.get("제품구분코드") != "03" or not isinstance(row.get("제품구분코드"), str):
            failures.append(f"R070 product-di source string contract mismatch: {row.to_dict()}")
    meta = dict(current.get("meta") or {})
    if meta.get("source_table") != "Rddbc070" or meta.get("source_mode") != "current":
        failures.append(f"current provenance mismatch: {meta}")
    if meta.get("source_call_count") != 1 or not meta.get("full_source_ready"):
        failures.append(f"current source contract mismatch: {meta}")
    if meta.get("candidate_data_policy") != "exclude_trim_empty_cost_apply_zero_start_date_and_all_six_prices_zero":
        failures.append(f"garbage candidate trace contract mismatch: {meta}")
    if meta.get("zero_price_contract") != "partial_zero_preserved_selected_price_zero_no_revive":
        failures.append(f"partial-zero trace contract mismatch: {meta}")

    semantic_cases = (
        ("insurance", " < ?"),
        ("non_insurance", " >= ?"),
    )
    for semantic_group, expected_operator in semantic_cases:
        captured.clear()
        with patch.object(service, "execute_bound_select", side_effect=_capture):
            service.get_rddbc070_current_result(
                {"product_di_semantic_group": semantic_group, "as_of": "20260907"}
            )
        semantic_sql, semantic_values = captured[0]
        if (
            "P.Rd04_Physic_Di" not in semantic_sql
            or "LTRIM(RTRIM(ISNULL(P.Rd04_Physic_Di, ''))) AS [제품구분코드]" not in semantic_sql
            or "NOT LIKE '%[^0-9]%'" not in semantic_sql
            or "THEN CAST" not in semantic_sql
            or expected_operator not in semantic_sql
            or 5 not in semantic_values
        ):
            failures.append(
                f"product-di semantic group SQL contract mismatch: {semantic_group}: "
                f"values={semantic_values}"
            )

    captured.clear()
    with patch.object(service, "execute_bound_select", side_effect=_capture):
        service.get_rddbc070_current_result(
            {"product_di_nm": "전문", "as_of": "20260907"}
        )
    like_sql, like_values = captured[0]
    if "PD.Rd01_Hnm LIKE ?" not in like_sql or "%전문%" not in like_values:
        failures.append("existing product-di name LIKE contract changed")

    bounded_rows = registered_query_limits({}).source_top
    bounded_fixture = pd.concat([_fixture(zero_latest=False)] * bounded_rows, ignore_index=True)
    with patch.object(service, "execute_bound_select", return_value=bounded_fixture) as bounded_select:
        bounded_payload = service.get_rddbc070_history_result({"display_top": 200})
    bounded_meta = dict(bounded_payload.get("meta") or {})
    if bounded_select.call_count != 1:
        failures.append(f"bounded source physical calls expected=1 got={bounded_select.call_count}")
    if len(bounded_payload.get("df", [])) != bounded_rows or len(bounded_payload.get("df_display", [])) != 200:
        failures.append("bounded full/display frames were not separated")
    elif (
        list(bounded_payload["df"]["조회순번"].head(2)) != [1, 2]
        or list(bounded_payload["df_display"]["조회순번"].head(2)) != [1, 2]
        or "보험단가" not in bounded_payload["df"].columns
        or bounded_payload["df"].iloc[0].get("제품구분코드") != "03"
        or bounded_payload["df_display"].iloc[0].get("제품구분코드") != "03"
        or bounded_payload.get("records", [{}])[0].get("제품구분코드") != "03"
        or "계약시작일자" not in bounded_payload["df"].columns
        or "계약시작일자" not in bounded_payload["df_display"].columns
        or "계약시작일자" not in bounded_payload.get("columns", [])
        or "계약시작일자" not in bounded_payload.get("records", [{}])[0]
    ):
        failures.append("full/display insurance price or display row-number projection missing")
    if (
        bounded_meta.get("full_source_limit_rows") != bounded_rows
        or bounded_meta.get("full_source_limit_hit") is not True
        or bounded_meta.get("full_source_truncated") != "unverified"
        or bounded_meta.get("full_source_limit_source") != "SIMS_IO_QUERY_MAX_ROWS"
        or bounded_meta.get("limit_policy") != "common_display_export_safety"
    ):
        failures.append(f"bounded provenance mismatch: {bounded_meta}")

    vendor_aliases = (
        "단가적용처 한미 계약단가 조회",
        "단가적용처명 한미 계약단가 조회",
        "단가적용거래처 한미 계약단가 조회",
        "단가적용거래처명 한미 계약단가 조회",
    )
    for text in vendor_aliases:
        parsed = resolve_registered_erp_table_nlq(text)
        if not parsed or parsed.get("params", {}).get("ven_nm") != "한미":
            failures.append(f"R070 vendor alias did not resolve deterministically: {text}: {parsed}")
            continue
        captured.clear()
        with patch.object(service, "execute_bound_select", side_effect=_capture):
            filtered_payload = service.get_rddbc070_current_result(parsed["params"])
        if len(captured) != 1:
            failures.append(f"R070 vendor alias source calls expected=1: {text}: {len(captured)}")
            continue
        alias_sql, alias_values = captured[0]
        if "V.Rd03_Ven_Nm LIKE ?" not in alias_sql or "%한미%" not in alias_values:
            failures.append(f"R070 vendor alias was not bound into SQL: {text}: {alias_values}")
        if len(filtered_payload.get("df", [])) != 1:
            failures.append(f"R070 vendor alias unexpectedly used an unfiltered source: {text}")

    from app.sims.nlq.nlq_router import _io_payload_table_frames

    normalized_full, normalized_display = _io_payload_table_frames(bounded_payload)
    if (
        not isinstance(normalized_full, pd.DataFrame)
        or not isinstance(normalized_display, pd.DataFrame)
        or len(normalized_full) != bounded_rows
        or len(normalized_display) != 200
    ):
        failures.append("NLQ full/display normalization contract missing")

    from app.ui.chat_middleware import _build_sims_result_header_view

    bounded_header = _build_sims_result_header_view(
        bounded_payload,
        bounded_meta,
        bounded_payload.get("df_display"),
    )
    bounded_line = str(bounded_header.get("line1") or "")
    if (
        f"{bounded_rows:,}건 조회" not in bounded_line
        or "안전 한도 도달" not in bounded_line
        or "전체 건수 미확인" not in bounded_line
        or f"전체 {bounded_rows:,}건" in bounded_line
    ):
        failures.append(f"bounded result count disclosure mismatch: {bounded_line!r}")

    inbound_key = service.inbound_contract_lookup_key(
        {
            "Rd11_Cost_Apply_Cd": "50002",
            "Rd11_Physic_Cd": "00085",
            "Rd11_In_YyMmDd": "20260901",
        }
    )
    outbound_key = service.outbound_contract_lookup_key(
        {
            "Rd12_Cost_Apply_Cd": "50002",
            "Rd12_Physic_Cd": "00085",
            "Rd12_Out_YyMmDd": "20260901",
        }
    )
    if inbound_key != outbound_key:
        failures.append(f"inbound/outbound lookup key mismatch: {inbound_key} != {outbound_key}")
    six_price_columns = (
        "실입고단가", "장부입고단가", "otc 입고단가",
        "실줄고단가", "장부출고단가", "otc 출고단가",
    )

    def _contract_row(*, vendor: str, product: str, start: str, value: int, deleted: str = "") -> dict:
        row = {
            "단가적용거래처": vendor,
            "제품코드": product,
            "계약시작일자": start,
            "삭제 flag": deleted,
        }
        row.update({column: value for column in six_price_columns})
        return row

    contract_rows = pd.DataFrame(
        [
            _contract_row(vendor="50002", product="00085", start="20260101", value=1000),
            _contract_row(vendor="50002", product="00085", start="20260801", value=0),
            _contract_row(vendor="", product="00085", start="20260810", value=2000),
            _contract_row(vendor="50002", product="00085", start="20260820", value=9999, deleted="E"),
            _contract_row(vendor="50002", product="99999", start="20260901", value=7777),
            _contract_row(vendor="99999", product="00085", start="20260901", value=8888),
            _contract_row(vendor="50002", product="00085", start="20261001", value=5555),
        ]
    )
    selected = service.select_latest_effective_contract(contract_rows, inbound_key)
    if not selected or selected.get("계약시작일자") != "20260101":
        failures.append(f"garbage-before-rank effective contract selection failed: {selected}")
    resolved = service.resolve_contract_price(
        contract_row=selected,
        direction="inbound",
        price_basis="real",
        product_type="1",
        transaction_insurance_price=2000,
        last_purchase_price=1500,
    )
    if resolved.get("status") != "resolved" or resolved.get("amount") != 1000:
        failures.append(f"valid prior contract was not selected after garbage exclusion: {resolved}")

    zero_date_only = pd.DataFrame(
        [_contract_row(vendor="50002", product="00085", start="00000000", value=1000)]
    )
    if service.select_latest_effective_contract(zero_date_only, inbound_key) is not None:
        failures.append("zero contract-start date remained an effective-contract candidate")

    partial_zero_rows = pd.DataFrame(
        [
            _contract_row(vendor="50002", product="00085", start="20260101", value=1000),
            {
                **_contract_row(vendor="50002", product="00085", start="20260801", value=1200),
                "실입고단가": 0,
            },
        ]
    )
    partial_selected = service.select_latest_effective_contract(partial_zero_rows, inbound_key)
    if not partial_selected or partial_selected.get("계약시작일자") != "20260801":
        failures.append(f"partial-zero candidate was incorrectly excluded: {partial_selected}")
    partial_terminated = service.resolve_contract_price(
        contract_row=partial_selected,
        direction="inbound",
        price_basis="real",
        product_type="1",
        transaction_insurance_price=2000,
        last_purchase_price=1500,
    )
    if partial_terminated.get("status") != "terminated" or partial_terminated.get("amount") is not None:
        failures.append(f"selected partial-zero price revived by fallback: {partial_terminated}")

    for product_type in ("1", "2", "3"):
        inbound_fallback = service.resolve_contract_price(
            contract_row=None,
            direction="inbound",
            price_basis="real",
            product_type=product_type,
            transaction_insurance_price=1000,
        )
        outbound_fallback = service.resolve_contract_price(
            contract_row=None,
            direction="outbound",
            price_basis="real",
            product_type=product_type,
            transaction_insurance_price=1000,
        )
        if inbound_fallback.get("amount") != 950.0 or outbound_fallback.get("amount") != 1000.0:
            failures.append(f"insurance fallback mismatch type={product_type}: {inbound_fallback}/{outbound_fallback}")
    if service.resolve_contract_price(
        contract_row=None,
        direction="inbound",
        price_basis="real",
        product_type="1",
    ).get("status") != "input_required":
        failures.append("insurance fallback used current/unknown insurance price")
    for product_type in ("5", "6", "7", "8"):
        non_insurance = service.resolve_contract_price(
            contract_row=None,
            direction="outbound",
            price_basis="real",
            product_type=product_type,
            last_purchase_price=730,
        )
        if non_insurance.get("amount") != 730.0 or non_insurance.get("source") != "last_purchase":
            failures.append(f"non-insurance fallback mismatch type={product_type}: {non_insurance}")
    if service.resolve_contract_price(
        contract_row=None,
        direction="inbound",
        price_basis="real",
        product_type="4",
        transaction_insurance_price=1000,
        last_purchase_price=700,
    ).get("status") != "unsupported_product_type":
        failures.append("unknown product type did not fail closed")

    captured.clear()
    with patch.object(service, "execute_bound_select", side_effect=_capture):
        history = service.get_rddbc070_history_result(
            {"ven_nm": "테스트", "physic_nm": "제품", "date_from": "20260101", "date_to": "20261231"}
        )
        exported = service.get_rddbc070_history_export_df({"ven_cd": "50002"})
    if len(captured) != 2:
        failures.append(f"history/export calls expected=2 got={len(captured)}")
    else:
        history_sql, history_values = captured[0]
        if "V.Rd03_Ven_Nm LIKE ?" not in history_sql or "P.Rd04_Physic_Nm LIKE ?" not in history_sql:
            failures.append("history name filters missing")
        if "C.Rd07_Start_Date >= ?" not in history_sql or "C.Rd07_Start_Date <= ?" not in history_sql:
            failures.append("history date filters missing")
        if (
            "LTRIM(RTRIM(C.Rd07_Cost_Apply_Cd)) <> ''" not in history_sql
            or not all(
                f"C.{column} = 0" in history_sql
                for column in (
                    "Rd07_In_Amt", "Rd07_In_Ramt", "Rd07_In_Oamt",
                    "Rd07_Out_Amt", "Rd07_Out_Ramt", "Rd07_Out_Oamt",
                )
            )
        ):
            failures.append("history garbage candidate exclusion missing")
        if "{_PRODUCT_MASTER_JOINS}" in history_sql:
            failures.append("history SQL contains unresolved product-master JOIN placeholder")
        if history_values != ("%테스트%", "%제품%", "20260101", "20261231"):
            failures.append(f"history bound values mismatch: {history_values}")
    if not isinstance(history.get("df"), pd.DataFrame) or not isinstance(exported, pd.DataFrame):
        failures.append("history/export full source missing")
    elif (
        "계약시작일자" not in history["df"].columns
        or "계약시작일자" not in exported.columns
        or history["df"].iloc[0].get("제품구분코드") != "03"
        or exported.iloc[0].get("제품구분코드") != "03"
    ):
        failures.append("history/export contract-start date missing")

    history_fixture = pd.concat(
        [
            _fixture(zero_latest=False).assign(계약시작일자="20260101"),
            _fixture(zero_latest=False).assign(계약시작일자="20260901", 실입고단가=0),
        ],
        ignore_index=True,
    )
    with patch.object(service, "execute_bound_select", return_value=history_fixture):
        history_rows = service.get_rddbc070_history_df({"top": 10})
    if len(history_rows) != 2 or list(history_rows["조회순번"]) != [1, 2]:
        failures.append("history did not preserve multiple contract rows with stable display sequence")

    grain_rows = pd.DataFrame(
        [
            _contract_row(vendor=vendor, product=product, start=start, value=value)
            for vendor, product, start, value in (
                ("50003", "00085", "20250101", 100),
                ("50003", "00085", "20260101", 110),
                ("50095", "00085", "20250101", 200),
                ("50095", "00085", "20260201", 220),
                ("50003", "00086", "20260301", 300),
                ("50095", "00086", "20260401", 400),
            )
        ]
    )
    selected_grains = []
    for vendor in ("50003", "50095"):
        for product in ("00085", "00086"):
            selected = service.select_latest_effective_contract(
                grain_rows,
                service.ContractPriceLookupKey(vendor, product, "20260906"),
            )
            if selected:
                selected_grains.append((selected["단가적용거래처"], selected["제품코드"]))
    if selected_grains != [
        ("50003", "00085"), ("50003", "00086"),
        ("50095", "00085"), ("50095", "00086"),
    ]:
        failures.append(f"cost-apply + product final grain collapsed: {selected_grains}")

    captured.clear()
    product_params = {
        "physic_cd": "00085",
        "product_keyword": "우루사",
        "insu_cd": "1234567890",
        "barcode": "880123",
        "maker_nm": "동아",
        "product_group_nm": "일반약",
        "product_di_nm": "전문약(보험)",
        "product_class_nm": "전문의약품",
        "product_unit_price": "1000~2000",
        "product_final_price_date": "202601~202612",
        "product_only_use": True,
        "product_add_user_nm": "관리자",
        "product_add_date_from": "202601",
        "product_add_date_to": "202612",
        "product_mod_user_nm": "수정자",
        "product_mod_date_from": "20260101",
        "product_mod_date_to": "20261231",
    }
    with patch.object(service, "execute_bound_select", side_effect=_capture):
        product_payload = service.get_rddbc070_history_result(product_params)
    if len(captured) != 1:
        failures.append(f"product-filter source calls expected=1 got={len(captured)}")
    else:
        product_sql, product_values = captured[0]
        required_product_sql = (
            "PV.Rd03_Ven_Nm LIKE ?", "PG.Rd01_Hnm LIKE ?", "PD.Rd01_Hnm LIKE ?",
            "PC.Rd01_Hnm LIKE ?", "P.Rd04_Insu_Cd = ?", "P.Rd04_Bar_Code1 = ?",
            "PMCALC.unit_price >= ?", "PMCALC.final_price_date >= ?",
            "PAU.Rd06_User_Nm LIKE ?", "PMU.Rd06_User_Nm LIKE ?", "P.Rd04_Use_Gu = ?",
        )
        if any(token not in product_sql for token in required_product_sql):
            failures.append("shared product-master SQL filter contract missing")
        if any(value in product_sql for value in ("우루사", "동아", "1234567890", "880123")):
            failures.append("product filter values were embedded instead of bound")
        if not product_values:
            failures.append("product filter bind values missing")
    product_df = product_payload.get("df")
    expected_product_columns = {
        "조회순번", "제약사", "제품그룹", "제품구분코드", "제품구분", "제품분류", "보험단가"
    }
    if not isinstance(product_df, pd.DataFrame) or not expected_product_columns.issubset(product_df.columns):
        failures.append(f"product-master result columns missing: {getattr(product_df, 'columns', [])}")

    goods_source = (ROOT / "app" / "sims" / "views" / "goods.py").read_text(encoding="utf-8")
    r070_view_source = (ROOT / "app" / "sims" / "views" / "rddbc_io_contract_views.py").read_text(encoding="utf-8")
    if "render_product_master_filters(" not in goods_source or "render_product_master_filters(" not in r070_view_source:
        failures.append("R040/R070 shared product-filter UI consumer missing")
    if "number_input(" in r070_view_source or '"조회건수"' in r070_view_source:
        failures.append("R070 panel still exposes a TOP/row-count widget")
    if "only_use_default=False" not in r070_view_source:
        failures.append("R070 history-preserving product use default missing")
    if 'st.session_state[f"{payload_key}_fresh"] = True' in r070_view_source:
        failures.append("R070 panel submit still marks a persisted payload fresh for the next rerun")
    if 'st.session_state.pop(f"{payload_key}_fresh", None)' not in r070_view_source:
        failures.append("R070 panel submit fresh lifecycle cleanup missing")
    main_source = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    if main_source.count('meta.pop("_force_push", None)') < 2:
        failures.append("Panel bridge force-push bypass cleanup missing")

    try:
        service.normalize_rddbc070_params({"contract_yn": "Y"}, mode="history")
        failures.append("unconfirmed contract_yn filter did not fail closed")
    except ValueError:
        pass
    try:
        service.normalize_rddbc070_params({"del_flag": "N"}, mode="history")
        failures.append("unconfirmed del_flag filter did not fail closed")
    except ValueError:
        pass

    pushed: list[dict] = []
    notices: list[dict] = []
    helpers = {
        "find_col": _find_col,
        "push_table": lambda **kwargs: pushed.append(kwargs) or True,
        "push_notice": lambda **kwargs: notices.append(kwargs) or True,
    }
    current_table_df = pd.DataFrame(
        [
            {
                "단가적용거래처": "50002", "제품코드": "00085", "제품구분코드": "01",
                "실입고단가": 1000, "계약상태": "단가 있음",
            },
            {
                "단가적용거래처": "50003", "제품코드": "00086", "제품구분코드": "A",
                "실입고단가": 2000, "계약상태": "일부 단가 0(해석 미확정)",
            },
        ]
    )
    if detect_current_table_kind("최종 계약단가 조회") != "generic":
        failures.append("R070 current-table kind must remain generic")
    handled = handle_current_table_followup_by_action(
        df=current_table_df,
        query="현재표 실입고단가 TOP 1",
        top_n=1,
        table_key="sims_r070",
        source_action="최종 계약단가 조회",
        helpers=helpers,
        log=logging.getLogger("r070.gate"),
    )
    if not handled or not pushed or len(pushed[-1].get("df", [])) != 1:
        failures.append(f"current-table TOP failed: handled={handled} pushed={len(pushed)} notices={len(notices)}")

    pushed.clear()
    notices.clear()
    handled = handle_current_table_followup_by_action(
        df=current_table_df,
        query="현재표 단가적용거래처별 집계",
        top_n=20,
        table_key="sims_r070",
        source_action="최종 계약단가 조회",
        helpers=helpers,
        log=logging.getLogger("r070.gate"),
        source_meta={"source_table": "Rddbc070"},
    )
    grouped_df = pushed[-1].get("df") if pushed else None
    if (
        not handled
        or not isinstance(grouped_df, pd.DataFrame)
        or "행수" not in grouped_df.columns
        or "실입고단가" in grouped_df.columns
    ):
        failures.append(f"current-table exact-column count group failed: {grouped_df}")

    pushed.clear()
    notices.clear()
    handled = handle_current_table_followup_by_action(
        df=current_table_df,
        query="현재표 계약상태 단가 있음 상세히 보여줘",
        top_n=20,
        table_key="sims_r070",
        source_action="최종 계약단가 조회",
        helpers=helpers,
        log=logging.getLogger("r070.gate"),
        source_meta={"source_table": "Rddbc070"},
    )
    filtered_df = pushed[-1].get("df") if pushed else None
    if not handled or not isinstance(filtered_df, pd.DataFrame) or len(filtered_df) != 1:
        failures.append(
            f"current-table exact-column literal filter failed: df={filtered_df} notices={notices}"
        )

    pushed.clear()
    notices.clear()
    handled = handle_current_table_followup_by_action(
        df=current_table_df,
        query="현재표 제품구분코드 01 상세히 보여줘",
        top_n=20,
        table_key="sims_r070",
        source_action="최종 계약단가 조회",
        helpers=helpers,
        log=logging.getLogger("r070.gate"),
        source_meta={"source_table": "Rddbc070"},
    )
    product_di_filtered = pushed[-1].get("df") if pushed else None
    if (
        not handled
        or not isinstance(product_di_filtered, pd.DataFrame)
        or product_di_filtered["제품구분코드"].tolist() != ["01"]
    ):
        failures.append(
            "current-table product-di string filter failed: "
            f"df={product_di_filtered} notices={notices}"
        )

    pushed.clear()
    notices.clear()
    handled = handle_current_table_followup_by_action(
        df=current_table_df,
        query="현재표 단가적용거래처별 수수료금액 집계",
        top_n=20,
        table_key="sims_r070",
        source_action="최종 계약단가 조회",
        helpers=helpers,
        log=logging.getLogger("r070.gate"),
        source_meta={"source_table": "Rddbc070"},
    )
    if not handled or pushed or not notices:
        failures.append("current-table unknown metric did not remain fail-closed")

    display_fixture = pd.DataFrame(
        {
            "계약시작일자": ["20260901"] * 6,
            "실입고단가": [100, -20, 30, 40, 50, 60],
            "기타": [None, "", "a", "b", "c", "d"],
        }
    )
    prepared = _prepare_io_display_df(display_fixture, add_row_no=False)
    negative_style = _build_io_negative_df(prepared)
    band_style = _build_io_banding_df(prepared, band_size=5)
    if "color: #d92d20" not in str(negative_style.loc[1, "실입고단가"]):
        failures.append("shared negative-number style not applied")
    if "background-color: #fafafa" not in str(band_style.loc[5, "실입고단가"]):
        failures.append("shared five-row banding not applied")
    if str(prepared.loc[0, "기타"]) != "":
        failures.append("shared null-to-blank display contract missing")
    if prepared.loc[0, "계약시작일자"] != "2026-09-01":
        failures.append(f"contract-start display date format mismatch: {prepared.loc[0, '계약시작일자']!r}")
    invalid_date_display = _prepare_io_display_df(
        pd.DataFrame({"계약시작일자": [None, "", "00000000", "20260230", "20260901"]}),
        add_row_no=False,
    )
    if invalid_date_display["계약시작일자"].tolist() != ["", "", "", "", "2026-09-01"]:
        failures.append(
            "contract-start invalid-date display contract mismatch: "
            f"{invalid_date_display['계약시작일자'].tolist()}"
        )

    audit_display = _prepare_io_display_df(
        _fixture(zero_latest=False)[["등록자", "등록자명", "수정자", "수정자명"]],
        add_row_no=False,
    )
    if audit_display.loc[0, "등록자"] != "00128" or audit_display.loc[0, "수정자"] != "00123":
        failures.append(f"R070 audit codes lost their string identity: {audit_display.iloc[0].to_dict()}")
    from app.ui.sims_table_display import is_sims_numeric_display_col
    if is_sims_numeric_display_col(audit_display, "등록자") or is_sims_numeric_display_col(audit_display, "수정자"):
        failures.append("R070 audit code columns were assigned a numeric formatter")
    vendor_display = _prepare_io_display_df(
        pd.DataFrame({"단가적용거래처": [50003, "050095"]}),
        add_row_no=False,
    )
    if vendor_display["단가적용거래처"].tolist() != ["50003", "050095"]:
        failures.append(f"cost-apply identifier display mismatch: {vendor_display.to_dict(orient='records')}")
    if is_sims_numeric_display_col(vendor_display, "단가적용거래처"):
        failures.append("cost-apply identifier was assigned a numeric formatter")
    product_di_display = _prepare_io_display_df(
        pd.DataFrame({"제품구분코드": ["01", "3", "5", "A", "B"]}),
        add_row_no=False,
    )
    if product_di_display["제품구분코드"].tolist() != ["01", "3", "5", "A", "B"]:
        failures.append(
            "product-di leading-zero/non-numeric display contract mismatch: "
            f"{product_di_display.to_dict(orient='records')}"
        )
    if is_sims_numeric_display_col(product_di_display, "제품구분코드"):
        failures.append("product-di code was assigned a numeric formatter")
    from app.ui.sims_table_display import resolve_sims_table_mode

    semantic_meta = {
        "registered_erp_table": True,
        "semantic_styled_max_rows": 300,
    }
    semantic_fixture = pd.concat([display_fixture] * 50, ignore_index=True)
    if len(semantic_fixture) != 300:
        failures.append("semantic table fixture must contain 300 rows")
    for rows in (200, 300):
        mode = resolve_sims_table_mode(
            semantic_fixture.head(rows),
            render_path="chat",
            meta=semantic_meta,
        )
        if mode.get("mode") != "small" or mode.get("reason") != "registered_erp_semantic_rows":
            failures.append(f"registered ERP {rows}-row styled mode mismatch: {mode}")
    large_mode = resolve_sims_table_mode(
        pd.concat([semantic_fixture] * 100, ignore_index=True),
        render_path="chat",
        meta=semantic_meta,
    )
    if large_mode.get("mode") != "fast":
        failures.append(f"registered ERP 30,000-row source did not keep fast mode: {large_mode}")
    styler = _build_io_display_styler(semantic_fixture, add_row_no=False, band_size=5)
    table_styles = getattr(styler, "table_styles", []) or []
    if not any(style.get("selector") == "tbody tr:hover td" for style in table_styles):
        failures.append("shared semantic table hover style missing")

    if failures:
        print("Rddbc070 contract-price Gate: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Rddbc070 contract-price Gate: PASS")
    print("- Panel/Hub/handler/NLQ/export/permission completeness PASS")
    print("- final/history/latest/garbage-before-rank/partial-zero/delete/filter/binding PASS")
    print("- inbound/outbound exact lookup and insurance/non-insurance fallback PASS")
    print("- NLQ/current-table TOP/full/display bounded provenance/single source call PASS")
    print("- shared R040/R070 product filters and row-number/insurance result projection PASS")
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
