from __future__ import annotations

import logging
import os
from pathlib import Path
import sys
from unittest.mock import patch

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fixture() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "제품코드": "00085",
                "제품명": "에멘드캡슐",
                "제약사": "테스트제약",
                "제품그룹": "전문",
                "제품구분코드": "01",
                "제품구분": "보험(전문)",
                "제품분류": "전문의약품",
                "매입처코드": "050003",
                "매입처명": "한미",
                "재고위치": "000001",
                "재고위치명": "본사창고",
                "재고적용코드": "050004",
                "재고적용처명": "재고적용처",
                "단가적용코드": "050005",
                "단가적용처명": "단가적용처",
                "실입고단가": 1234.5,
                "장부입고단가": 1300.0,
                "입고수량": 10.0,
                "출고수량": 7.0,
                "재고수량": 3.0,
                "실재고금액": 3703.5,
                "장부재고금액": 3900.0,
            },
            {
                "제품코드": "00086",
                "제품명": "다른제품",
                "제약사": "테스트제약",
                "제품그룹": "일반",
                "제품구분코드": "A",
                "제품구분": "기타",
                "제품분류": "일반",
                "매입처코드": "050003",
                "매입처명": "한미",
                "재고위치": "000002",
                "재고위치명": "지점창고",
                "재고적용코드": "050004",
                "재고적용처명": "재고적용처",
                "단가적용코드": "050005",
                "단가적용처명": "단가적용처",
                "실입고단가": 100.0,
                "장부입고단가": 120.0,
                "입고수량": 3.0,
                "출고수량": 8.0,
                "재고수량": -5.0,
                "실재고금액": -500.0,
                "장부재고금액": -600.0,
            },
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

    from app.services import rddbc230_service as service
    from app.services.erp_table_nlq import resolve_registered_erp_table_nlq
    from app.services.erp_table_query_service import registered_query_limits
    from app.services.ssai_permission_policy import get_required_permission
    from app.sims.meta.erp_table_feature_registry import (
        get_action_spec,
        menu_actions,
        menu_targets,
        registry_completeness_errors,
        resolve_dotted_callable,
    )
    from app.ui.current_table_followups.action_dispatcher import (
        detect_current_table_kind,
        handle_current_table_followup_by_action,
    )
    from app.sims.views.rddbc_io_shared import _build_io_negative_df, _prepare_io_display_df
    from app.ui.sims_table_display import is_sims_numeric_display_col

    if registry_completeness_errors():
        failures.append(f"registry incomplete: {registry_completeness_errors()}")

    registered = get_action_spec("최종 매입단가 조회")
    if not registered or registered[0].table_key != "rddbc230":
        failures.append("R230 canonical action is not uniquely registered")
    else:
        feature, action = registered
        if feature.primary_key != (
            "Rd23_Physic_Cd", "Rd23_Ven_Cd", "Rd23_Stock_Cd",
            "Rd23_Stock_Apply_Cd", "Rd23_Cost_Apply_Cd",
            "Rd23_Unit_Cost", "Rd23_Fin_Unit_Cost",
        ):
            failures.append(f"raw grain changed: {feature.primary_key}")
        if feature.required_permission != "IO_READ":
            failures.append(f"permission metadata mismatch: {feature.required_permission}")
        filter_keys = {item.key for item in feature.filters}
        forbidden_filter_keys = {
            "product_unit_price", "product_final_price_date",
            "product_add_user_nm", "product_add_date_from", "product_add_date_to",
            "product_mod_user_nm", "product_mod_date_from", "product_mod_date_to",
            "product_only_use",
        }
        if forbidden_filter_keys.intersection(filter_keys):
            failures.append(f"R230 exposes foreign master filters: {forbidden_filter_keys.intersection(filter_keys)}")
        if "product_di_semantic_group" not in filter_keys:
            failures.append("R230 semantic product group metadata missing")
        if "stock_nm" not in filter_keys:
            failures.append("R230 stock-location name filter metadata missing")
        if feature.source_limit_env != "SIMS_EXPORT_MAX_ROWS":
            failures.append(f"R230 source limit policy mismatch: {feature.source_limit_env}")
        for target in (action.service_function, action.export_function, action.view_function):
            try:
                resolve_dotted_callable(target)
            except Exception as exc:
                failures.append(f"registered callable failed: {target}: {type(exc).__name__}")

    if menu_targets(business_group="재고관리") != ("재고",):
        failures.append(f"inventory menu targets mismatch: {menu_targets(business_group='재고관리')}")
    menu = menu_actions(business_group="재고관리", target="재고")
    if menu.count("최종 매입단가 조회") != 1:
        failures.append(f"three-level menu count mismatch: {menu}")
    if get_required_permission(action="최종 매입단가 조회") != "IO_READ":
        failures.append("R230 action permission is not IO_READ")

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
        panel_limits = registered_query_limits(
            {"_display_context": "panel"}, source_limit_env="SIMS_EXPORT_MAX_ROWS",
        )
        chat_limits = registered_query_limits(
            {"_display_context": "chat"}, source_limit_env="SIMS_EXPORT_MAX_ROWS",
        )
        top20 = registered_query_limits(
            {"_display_context": "chat", "display_top": 20},
            source_limit_env="SIMS_EXPORT_MAX_ROWS",
        )
        io_limits = registered_query_limits({"_display_context": "chat"})
    if (panel_limits.display_top, chat_limits.display_top) != (1000, runtime_chat_limit):
        failures.append(f"display limits mismatch: {panel_limits}/{chat_limits}")
    if (top20.display_top, top20.source_top) != (20, runtime_export_limit):
        failures.append(f"NLQ TOP changed source cap: {top20}")
    if (chat_limits.display_top, chat_limits.source_top) != (runtime_chat_limit, runtime_export_limit):
        failures.append(f"R230 chat/source limits mismatch: {chat_limits}")
    if io_limits.source_top != runtime_io_limit or io_limits.source_limit_source != "SIMS_IO_QUERY_MAX_ROWS":
        failures.append(f"other IO source limit changed: {io_limits}")

    nlq_cases = (
        ("최종 매입단가 조회", {}),
        ("제품별 최종 매입단가 조회", {}),
        ("에멘드 최종 매입단가 조회", {"physic_nm": "에멘드"}),
        ("매입처 한미 최종 매입단가 조회", {"buy_nm": "한미"}),
        ("단가적용처 한미 최종 매입단가 조회", {"cost_apply_nm": "한미"}),
        ("재고적용처 한미 최종 매입단가 조회", {"stock_apply_nm": "한미"}),
        ("재고위치 000001 최종 매입단가 조회", {"stock_cd": "000001"}),
        ("재고위치명 본사 최종 매입단가 조회", {"stock_nm": "본사"}),
        ("실입고단가 조회", {}),
        ("장부입고단가 조회", {}),
        ("전문약 최종 매입단가 조회", {"product_di_semantic_group": "insurance"}),
        ("ETC 최종 매입가 조회", {"product_di_semantic_group": "insurance"}),
        ("최종 매입가", {}),
        ("최종 매입가 조회", {}),
        ("일반약 최종 매입단가 조회", {"product_di_semantic_group": "non_insurance"}),
        ("OTC 최종 매입단가 조회", {"product_di_semantic_group": "non_insurance"}),
        ("제품구분 전문 최종 매입단가 조회", {"product_di_nm": "전문"}),
        ("최종 매입단가 조회 TOP 20", {"display_top": 20}),
    )
    for text, expected in nlq_cases:
        parsed = resolve_registered_erp_table_nlq(text)
        if not parsed or parsed.get("action") != "최종 매입단가 조회":
            failures.append(f"NLQ action mismatch: {text!r} -> {parsed}")
            continue
        for key, value in expected.items():
            if parsed.get("params", {}).get(key) != value:
                failures.append(f"NLQ filter mismatch: {text!r}/{key} -> {parsed}")
        if text == "제품별 최종 매입단가 조회" and parsed.get("params", {}).get("physic_nm"):
            failures.append(f"제품별 was parsed as product name: {parsed}")

    captured: list[tuple[str, list[object]]] = []

    def fake_select(sql: str, params) -> pd.DataFrame:
        captured.append((sql, list(params)))
        return _fixture().copy()

    params = {
        "physic_cd": "00085", "physic_nm": "에멘드",
        "buy_cd": "050003", "buy_nm": "한미", "stock_cd": "000001", "stock_nm": "본사",
        "stock_apply_cd": "050004", "stock_apply_nm": "재고",
        "cost_apply_cd": "050005", "cost_apply_nm": "단가",
        "unit_cost": "1000~2000", "fin_unit_cost": "1200",
        "in_quantity": "1~20", "out_quantity": "7",
        "_display_context": "chat",
    }
    with patch.dict(
        "os.environ",
        {
            "SIMS_CHAT_DISPLAY_MAX_ROWS": str(runtime_chat_limit),
            "SIMS_EXPORT_MAX_ROWS": str(runtime_export_limit),
            "SIMS_IO_QUERY_MAX_ROWS": str(runtime_io_limit),
        },
    ), patch.object(service, "execute_bound_select", side_effect=fake_select):
        payload = service.get_rddbc230_result(params)
    if len(captured) != 1 or payload.get("meta", {}).get("source_call_count") != 1:
        failures.append(f"single-source contract failed: calls={len(captured)} meta={payload.get('meta')}")
    else:
        sql, values = captured[0]
        normalized_sql = " ".join(sql.split())
        required_joins = (
            "S.Rd23_Physic_Cd = P.Rd04_Physic_Cd",
            "S.Rd23_Ven_Cd = BV.Rd03_Ven_Cd",
            "S.Rd23_Stock_Apply_Cd = SV.Rd03_Ven_Cd",
            "S.Rd23_Cost_Apply_Cd = CV.Rd03_Ven_Cd",
            "ST.Rd01_Gcode = '0018'",
            "S.Rd23_Stock_Cd = ST.Rd01_Tcode",
        )
        if any(join not in normalized_sql for join in required_joins):
            failures.append("native raw-equality JOIN contract missing")
        required_calculations = (
            "(S.Rd23_In_Quantity - S.Rd23_Out_Quantity) AS [재고수량]",
            "((S.Rd23_In_Quantity - S.Rd23_Out_Quantity) * S.Rd23_Unit_Cost) AS [실재고금액]",
            "((S.Rd23_In_Quantity - S.Rd23_Out_Quantity) * S.Rd23_Fin_Unit_Cost) AS [장부재고금액]",
        )
        if any(calculation not in normalized_sql for calculation in required_calculations):
            failures.append("R230 derived quantity/amount SQL contract missing")
        if "ST.Rd01_Hnm LIKE ?" not in normalized_sql or "%본사%" not in values:
            failures.append("stock-location name filter is not bound to Rddbc010")
        if "SV.Rd03_Ven_Nm LIKE ?" not in normalized_sql:
            failures.append("stock-apply name filter was removed")
        if f"SELECT TOP {runtime_export_limit}" not in normalized_sql:
            failures.append("R230 source TOP did not resolve from SIMS_EXPORT_MAX_ROWS")
        if any(token in normalized_sql.upper() for token in ("GROUP BY", "ROW_NUMBER(", "SELECT DISTINCT")):
            failures.append("R230 source was grouped/deduplicated/ranked")
        if "LTRIM(RTRIM(S.Rd23_" in " ".join(
            line.strip() for line in sql.splitlines() if line.strip().startswith("ON ")
        ):
            failures.append("R230 JOIN key was trimmed")
        if sql.count("?") != len(values) or not values:
            failures.append(f"parameter binding mismatch: placeholders={sql.count('?')} values={values}")
        if any(str(value) in sql for value in ("00085", "050003", "050004", "050005")):
            failures.append("bound filter value leaked into SQL text")

    full_df = payload.get("df")
    display_df = payload.get("df_display")
    if not isinstance(full_df, pd.DataFrame) or not isinstance(display_df, pd.DataFrame):
        failures.append("full/display DataFrames missing")
    expected_columns = {
        "조회순번", "제품코드", "제품명", "매입처코드", "매입처명", "재고위치", "재고위치명",
        "재고적용코드", "재고적용처명", "단가적용코드", "단가적용처명",
        "실입고단가", "장부입고단가", "입고수량", "출고수량",
        "재고수량", "실재고금액", "장부재고금액",
    }
    if not isinstance(full_df, pd.DataFrame) or not expected_columns.issubset(full_df.columns):
        failures.append(f"required result columns missing: {getattr(full_df, 'columns', [])}")
    audit_columns = {"등록자", "등록일자", "수정자", "수정일자"}
    if isinstance(full_df, pd.DataFrame) and audit_columns.intersection(full_df.columns):
        failures.append("nonexistent audit fields were synthesized")
    if payload.get("meta", {}).get("audit_columns_present") is not False:
        failures.append("audit absence provenance missing")
    if (
        payload.get("meta", {}).get("full_source_limit_rows") != runtime_export_limit
        or payload.get("meta", {}).get("full_source_limit_source") != "SIMS_EXPORT_MAX_ROWS"
        or payload.get("meta", {}).get("source_call_count") != 1
    ):
        failures.append(f"R230 source-limit provenance mismatch: {payload.get('meta')}")
    if isinstance(full_df, pd.DataFrame):
        negative = full_df.loc[full_df["제품코드"] == "00086"].iloc[0]
        if (
            float(negative["재고수량"]) != -5.0
            or float(negative["실재고금액"]) != -500.0
            or float(negative["장부재고금액"]) != -600.0
        ):
            failures.append(f"negative derived inventory values changed: {negative.to_dict()}")

    code_fixture = pd.DataFrame(
        {
            "제품코드": ["00085"], "매입처코드": ["050003"], "재고위치": ["000001"],
            "재고적용코드": ["050004"], "단가적용코드": ["050005"],
            "실입고단가": [1234.5],
        }
    )
    prepared = _prepare_io_display_df(code_fixture, add_row_no=False)
    for column in ("제품코드", "매입처코드", "재고위치", "재고적용코드", "단가적용코드"):
        if prepared.loc[0, column] != code_fixture.loc[0, column]:
            failures.append(f"leading-zero code lost: {column}={prepared.loc[0, column]!r}")
        if is_sims_numeric_display_col(prepared, column):
            failures.append(f"code received numeric formatter: {column}")

    negative_columns = ("입고수량", "출고수량", "재고수량", "실재고금액", "장부재고금액")
    negative_styles = _build_io_negative_df(
        pd.DataFrame([{column: -1 for column in negative_columns}])
    )
    for column in negative_columns:
        if "color: #d92d20" not in str(negative_styles.loc[0, column]):
            failures.append(f"common negative renderer contract missing: {column}")

    if detect_current_table_kind("최종 매입단가 조회") != "generic":
        failures.append("R230 current-table kind must be generic")
    pushed: list[dict] = []
    notices: list[dict] = []
    helpers = {
        "find_col": _find_col,
        "push_table": lambda **kwargs: pushed.append(kwargs) or True,
        "push_notice": lambda **kwargs: notices.append(kwargs) or True,
    }
    handled = handle_current_table_followup_by_action(
        df=prepared,
        query="현재표 재고위치 000001 상세히 보여줘 TOP 1",
        top_n=1,
        table_key="sims_r230",
        source_action="최종 매입단가 조회",
        helpers=helpers,
        log=logging.getLogger("r230.gate"),
        source_meta={"source_table": "Rddbc230"},
    )
    if not handled or not pushed or len(pushed[-1].get("df", [])) != 1:
        failures.append(f"current-table literal/TOP failed: handled={handled} notices={notices}")

    sidebar_source = (ROOT / "app" / "ui" / "sims_entry.py").read_text(encoding="utf-8")
    if 'business_group="재고관리"' not in sidebar_source or "inventory_action_options_map" not in sidebar_source:
        failures.append("Panel sidebar does not consume registry inventory menu")
    view_source = (ROOT / "app" / "sims" / "views" / "rddbc_io_stock_cost_views.py").read_text(encoding="utf-8")
    if "render_product_master_filters(" not in view_source:
        failures.append("R230 does not reuse product-master filter UI")
    if (
        "등록자" in view_source
        or "수정자" in view_source
        or "number_input(" in view_source
        or "st.expander(" in view_source
        or "include_price_filters=False" not in view_source
        or "include_audit_filters=False" not in view_source
        or "include_only_use=False" not in view_source
    ):
        failures.append("R230 view contains forbidden audit/TOP UI")
    for removed_label in ("실입고단가", "장부입고단가", "입고수량", "출고수량"):
        if f'st.text_input("{removed_label}"' in view_source:
            failures.append(f"R230 numeric UI filter remains: {removed_label}")
    first_row = tuple(view_source.find(f'st.text_input("{label}"') for label in (
        "매입처코드", "매입처명", "재고위치", "재고위치명",
    ))
    second_row = tuple(view_source.find(f'st.text_input("{label}"') for label in (
        "단가적용코드", "단가적용처명", "재고적용코드", "재고적용처명",
    ))
    if -1 in first_row or tuple(sorted(first_row)) != first_row:
        failures.append(f"R230 first-row UI order mismatch: {first_row}")
    if -1 in second_row or tuple(sorted(second_row)) != second_row or min(second_row) < max(first_row):
        failures.append(f"R230 second-row UI order mismatch: {second_row}")
    chat_source = (ROOT / "app" / "ui" / "chat_middleware.py").read_text(encoding="utf-8")
    if 'is_io_table = bool(meta.get("registered_erp_table")) or any(' not in chat_source:
        failures.append("registered ERP table does not share the IO semantic renderer")

    if failures:
        print("Rddbc230 feature Gate: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Rddbc230 feature Gate: PASS")
    print("- registry/menu/IO_READ/handler/NLQ completeness PASS")
    print("- raw grain/native joins/bound filters/single source call PASS")
    print("- full/display/current-table/export/code identity/audit absence PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
