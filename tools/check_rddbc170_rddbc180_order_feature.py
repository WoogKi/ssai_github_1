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


def _fixture() -> pd.DataFrame:
    base = {
        "발주일자": "20260907", "발주거래처코드": "00007", "발주거래처명": "삼진",
        "발주순번": 1, "상세순번": 1, "납기일자": "20260910", "제품코드": "00001",
        "제품명": "테스트", "단가적용처코드": "00008", "단가적용처명": "단가처",
        "재고적용처코드": "00009", "재고적용처명": "재고처", "재고위치코드": "00001",
        "재고위치명": "본사", "단가": 100.0, "발주수량": 10.0, "할증수량": 1.0,
        "입고수량": 0.0, "미입고수량": 11.0, "발주상태코드": "1", "발주상태명": "발주",
        "발주담당자코드": "00010", "발주담당자명": "담당", "예상매출처코드": "00011",
        "예상매출처명": "예상", "실납처코드": "00012", "실납처명": "실납",
    }
    invalid = dict(base)
    invalid.update({"발주일자": "30140905", "상세순번": 2, "발주상태코드": "9", "미입고수량": None})
    zero = dict(base)
    zero.update({"상세순번": 3, "발주수량": 0.0, "할증수량": 0.0, "미입고수량": 0.0})
    negative = dict(base)
    negative.update({"상세순번": 4, "발주수량": -2.0, "할증수량": 0.0, "미입고수량": -2.0})
    return pd.DataFrame([base, invalid, zero, negative])


def main() -> int:
    failures: list[str] = []
    from app.services import rddbc170_rddbc180_order_service as service
    from app.services.business_calendar_service import recent_business_dates
    from app.services.erp_table_nlq import resolve_registered_erp_table_nlq
    from app.services.io_nlq import apply_nlq_default_period_policy, resolve_io_nlq
    from app.services.ssai_permission_policy import get_required_permission
    from app.sims.meta.erp_table_feature_registry import (
        get_action_spec, menu_actions, registry_completeness_errors, resolve_dotted_callable,
    )
    from app.sims.views.rddbc_io_order_views import _merge_filter_params
    from app.sims.views.rddbc_io_shared import _maybe_to_numeric
    from app.ui.sims_table_display import _numeric_display_kind
    from app.ui.current_table_followups.action_dispatcher import handle_current_table_followup_by_action

    if registry_completeness_errors():
        failures.append(f"registry incomplete: {registry_completeness_errors()}")
    for action_name in ("발주조회", "입고예정조회"):
        registered = get_action_spec(action_name)
        if not registered or registered[0].table_key != "rddbc170_rddbc180":
            failures.append(f"action registration missing: {action_name}")
            continue
        feature, action = registered
        if feature.primary_key != ("Rd18_Or_YyMmDd", "Rd18_OrVen_Cd", "Rd18_Or_Seq", "Rd18_Orsub_Seq"):
            failures.append(f"detail grain changed: {feature.primary_key}")
        if feature.required_permission != "IO_READ" or get_required_permission(action=action_name) != "IO_READ":
            failures.append(f"permission mismatch: {action_name}")
        for target in (action.service_function, action.export_function, action.view_function):
            try:
                resolve_dotted_callable(target)
            except Exception as exc:
                failures.append(f"callable failed: {target}: {type(exc).__name__}")
    menu = menu_actions(business_group="재고관리", target="발주")
    if menu != ("발주조회", "입고예정조회"):
        failures.append(f"menu mismatch: {menu}")

    monday = date(2026, 9, 7)
    business_dates = recent_business_dates(today=monday, count=4)
    if business_dates != ("20260907", "20260904", "20260903", "20260902"):
        failures.append(f"today plus three prior business-day boundary failed: {business_dates}")
    override = recent_business_dates(
        today=monday,
        count=4,
        overrides={"20260904": "holiday", "20260905": "work"},
    )
    if override != ("20260907", "20260905", "20260903", "20260902"):
        failures.append(f"holiday/work override failed: {override}")

    merged_filters = _merge_filter_params(
        {"maker_nm": "삼진"},
        {"order_vendor_nm": "종근당주식회사"},
    )
    normalized_filters = service.normalize_order_params(
        {
            **merged_filters,
            "date_from": "20260808",
            "date_to": "20260907",
        },
        mode="order",
    )
    filter_clauses, filter_values = service._filters(normalized_filters, mode="order")
    filter_sql = " ".join(filter_clauses)
    if "PMV.Rd03_Ven_Nm LIKE ?" not in filter_sql or "%삼진%" not in filter_values:
        failures.append("Panel maker_nm did not bind to the product-maker PMV alias")
    if "OV.Rd03_Ven_Nm LIKE ?" not in filter_sql or "%종근당주식회사%" not in filter_values:
        failures.append("Panel order_vendor_nm did not bind independently to the OV alias")

    for column, value in (("발주순번", 16.0), ("상세순번", 1.0)):
        normalized_identifier = _maybe_to_numeric(pd.Series([value]), column).iloc[0]
        if int(normalized_identifier) != int(value) or _numeric_display_kind(column) != "int":
            failures.append(f"integer identifier display contract failed: {column}")

    nlq_cases = (
        ("최근 발주내역 조회", "발주조회", {}),
        ("최근 발주 조회", "발주조회", {}),
        ("오늘 발주 조회", "발주조회", {"date_from": "20260907", "date_to": "20260907"}),
        ("이번주 발주 조회", "발주조회", {"date_from": "20260907", "date_to": "20260907"}),
        ("삼진 발주내역 조회", "발주조회", {"order_vendor_nm": "삼진"}),
        ("제품 아모틴 발주 조회", "발주조회", {"physic_nm": "아모틴"}),
        ("아라바정 제품 발주 조회", "발주조회", {"physic_nm": "아라바정"}),
        ("어제 발주 조회", "발주조회", {"date_from": "20260906", "date_to": "20260906"}),
        ("입고중 발주 조회", "발주조회", {"status_code": "2"}),
        ("입고중 발주조회", "발주조회", {"status_code": "2"}),
        ("발주상태 입고중 조회", "발주조회", {"status_code": "2"}),
        ("미입고 발주 조회", "발주조회", {"has_outstanding": True}),
        ("단가적용처 50002 발주 조회", "발주조회", {"cost_apply_cd": "50002"}),
        ("재고적용처 50001 발주 조회", "발주조회", {"stock_apply_cd": "50001"}),
        ("9월 동구바이오 발주 조회", "발주조회", {"date_from": "20260901", "date_to": "20260930", "order_vendor_nm": "동구바이오"}),
        ("종근당 전문약 발주 조회", "발주조회", {"order_vendor_nm": "종근당", "product_di_semantic_group": "insurance"}),
        ("9월 종근당 전문약 발주 조회", "발주조회", {"date_from": "20260901", "date_to": "20260930", "order_vendor_nm": "종근당", "product_di_semantic_group": "insurance"}),
        ("단가적용처 한미 발주 조회", "발주조회", {"cost_apply_nm": "한미"}),
        ("재고적용처 한미 입고예정 조회", "입고예정조회", {"stock_apply_nm": "한미"}),
        ("입고예정", "입고예정조회", {}),
        ("입고 예정", "입고예정조회", {}),
        ("입고예정 조회", "입고예정조회", {}),
        ("입고 예정 조회", "입고예정조회", {}),
        ("전문약 입고예정", "입고예정조회", {"product_di_semantic_group": "insurance"}),
        ("일반약 입고예정", "입고예정조회", {"product_di_semantic_group": "non_insurance"}),
        ("ETC 입고예정", "입고예정조회", {"product_di_semantic_group": "insurance"}),
        ("OTC 입고예정", "입고예정조회", {"product_di_semantic_group": "non_insurance"}),
        ("전문의약품 입고예정", "입고예정조회", {"product_di_semantic_group": "insurance"}),
        ("일반의약품 입고예정", "입고예정조회", {"product_di_semantic_group": "non_insurance"}),
        ("삼진 입고예정 조회", "입고예정조회", {"order_vendor_nm": "삼진"}),
        ("입고중 제품 조회", "입고예정조회", {"status_code": "2"}),
    )
    for text, expected_action, expected_params in nlq_cases:
        parsed = resolve_registered_erp_table_nlq(text, today=monday)
        if not parsed or parsed.get("action") != expected_action:
            failures.append(f"NLQ action mismatch: {text!r} -> {parsed}")
            continue
        for key, value in expected_params.items():
            if parsed.get("params", {}).get(key) != value:
                failures.append(f"NLQ filter mismatch: {text!r}/{key} -> {parsed}")
        if text in {
            "어제 발주 조회", "입고중 발주조회", "발주상태 입고중 조회",
            "단가적용처 50002 발주 조회", "재고적용처 50001 발주 조회",
        } and parsed.get("params", {}).get("order_vendor_nm"):
            failures.append(f"consumed syntax leaked into order vendor: {text!r} -> {parsed}")

    for text in ("입고명세 조회", "오늘 입고명세 조회"):
        parsed = resolve_io_nlq(text, today=monday)
        if not parsed or parsed.get("action") != "입고명세 조회":
            failures.append(f"R110 inbound-detail routing regressed: {text!r} -> {parsed}")

    from app.services.web_search_service import parse_web_search_request

    if parse_web_search_request("최근 발주내역 조회") is not None:
        failures.append("registered order action was intercepted by web-search routing")
    if parse_web_search_request("최근 제약업계 소식") is None:
        failures.append("external freshness request no longer reaches web-search routing")

    yesterday = resolve_registered_erp_table_nlq("어제 발주 조회", today=monday) or {}
    yesterday_params, yesterday_policy = apply_nlq_default_period_policy(
        yesterday.get("params", {}), "발주조회", today=monday,
    )
    if (
        yesterday_params.get("date_from") != "20260906"
        or yesterday_params.get("date_to") != "20260906"
        or yesterday_policy.get("auto_applied")
    ):
        failures.append(f"explicit yesterday period policy failed: {yesterday_params}/{yesterday_policy}")

    vendor_params, vendor_policy = apply_nlq_default_period_policy(
        {"order_vendor_nm": "동구바이오"}, "발주조회", today=monday,
    )
    if (
        vendor_params.get("date_from") != "20260808"
        or vendor_params.get("date_to") != "20260907"
        or vendor_policy.get("policy_reason") != "explicit_condition"
    ):
        failures.append(f"explicit order-vendor period policy failed: {vendor_params}/{vendor_policy}")

    captured: list[tuple[str, list[object]]] = []
    def fake_select(sql: str, params) -> pd.DataFrame:
        captured.append((sql, list(params)))
        source = _fixture()
        if "(D.Rd18_Quantity + D.Rd18_Oquantity) >= 0" in sql:
            source = source[(source["발주수량"] + source["할증수량"]) >= 0]
        return source.reset_index(drop=True)

    with patch.dict("os.environ", {"SIMS_CHAT_DISPLAY_MAX_ROWS": "300", "SIMS_IO_QUERY_MAX_ROWS": "100000"}), patch.object(service, "execute_bound_select", side_effect=fake_select):
        order = service.get_order_result({"date_from": "20260901", "date_to": "20260907", "_display_context": "chat"})
        expected = service.get_expected_inbound_result({"_today": "20260907", "_display_context": "chat"})
    if len(captured) != 2:
        failures.append(f"one source call per request failed: {len(captured)}")
    for payload in (order, expected):
        meta = payload.get("meta", {})
        if meta.get("source_call_count") != 1 or meta.get("display_top") != 300:
            failures.append(f"source/display provenance mismatch: {meta}")
        df = payload.get("df")
        if not isinstance(df, pd.DataFrame) or df.loc[0, "발주거래처코드"] != "00007":
            failures.append("code leading-zero/full source contract failed")
        if not isinstance(df, pd.DataFrame) or df.loc[1, "발주일자 품질상태"] != "비정상":
            failures.append("invalid date was not fail-closed")
        if not isinstance(df, pd.DataFrame) or (df["발주수량"] + df["할증수량"] < 0).any():
            failures.append("negative return-request row reappeared in full/current-table source")
        if not isinstance(df, pd.DataFrame) or not (df["발주수량"] + df["할증수량"] == 0).any():
            failures.append("zero-quantity order was incorrectly excluded")
        expected_prefix = (
            "조회순번", "발주일자", "발주거래처코드", "발주거래처명",
            "발주순번", "상세순번", "납기일자", "제품코드", "제품명",
        )
        if not isinstance(df, pd.DataFrame) or tuple(df.columns[:len(expected_prefix)]) != expected_prefix:
            failures.append(f"order result column contract changed: {tuple(df.columns[:len(expected_prefix)]) if isinstance(df, pd.DataFrame) else ()}")
    if expected.get("meta", {}).get("business_day_count") != 4:
        failures.append("expected-inbound business-day provenance is not four dates")
    if "오늘 + 직전 3영업일" not in str(expected.get("meta", {}).get("summary_md") or ""):
        failures.append("expected-inbound summary does not describe the four-date contract")

    pushed: list[dict] = []
    calls_before_followup = len(captured)
    handled = handle_current_table_followup_by_action(
        df=order.get("df"),
        query="현재표 발주거래처명 삼진 보여줘 TOP 1",
        top_n=1,
        table_key="sims_order_gate",
        source_action="발주조회",
        helpers={
            "find_col": lambda df, exact=(), include_any=(), exclude_any=(): next(
                (column for column in df.columns if column in exact or (any(token in str(column) for token in include_any) and not any(token in str(column) for token in exclude_any))),
                "",
            ),
            "push_table": lambda **kwargs: pushed.append(kwargs) or True,
            "push_notice": lambda **_kwargs: True,
        },
        log=logging.getLogger("order.gate"),
        source_meta=order.get("meta"),
    )
    if not handled or not pushed or len(pushed[-1].get("df", [])) != 1:
        failures.append("current-table literal/TOP source reuse failed")
    if len(captured) != calls_before_followup:
        failures.append("current-table follow-up re-queried the DB source")

    order_sql = " ".join(captured[0][0].split()) if captured else ""
    expected_sql = " ".join(captured[1][0].split()) if len(captured) > 1 else ""
    required = (
        "H.Rd17_Or_YyMmDd = D.Rd18_Or_YyMmDd",
        "H.Rd17_Orven_Cd = D.Rd18_OrVen_Cd",
        "H.Rd17_Or_Seq = D.Rd18_Or_Seq",
        "WHEN '1' THEN D.Rd18_Quantity + D.Rd18_Oquantity",
        "WHEN '2' THEN D.Rd18_Quantity + D.Rd18_Oquantity - D.Rd18_In_Quantity",
        "WHEN '3' THEN 0",
    )
    if any(token not in order_sql for token in required):
        failures.append("join/status formula contract missing")
    normal_order_predicate = "(D.Rd18_Quantity + D.Rd18_Oquantity) >= 0"
    if order_sql.count(normal_order_predicate) != 1 or expected_sql.count(normal_order_predicate) != 1:
        failures.append("shared normal-order source predicate missing or duplicated")
    if order_sql.count(" AS PV ") != 1 or order_sql.count(" AS PMV ") != 1:
        failures.append("vendor/product enrichment aliases are not unique")
    forbidden = ("DISTINCT", "GROUP BY", "ROW_NUMBER(", "RD17_IO_GU <> '190'", "GREATEST(", "ABS(")
    if any(token in order_sql.upper() for token in forbidden):
        failures.append("grain/filter/clamp contract violated")
    if "D.Rd18_Or_Di IN ('1', '2')" not in expected_sql or "RecentBusinessDates" not in expected_sql:
        failures.append("expected-inbound status/business-day filter missing")
    if "SELECT TOP 4 C.business_date" not in expected_sql:
        failures.append("expected-inbound SQL does not select today plus three prior business days")
    expected_where = expected_sql.rsplit("WHERE", 1)[-1]
    if "Rd17_Put_YyMmDd" in expected_where:
        failures.append("due date controls expected-inbound membership")
    if "dbo.WB_Holiday" not in expected_sql or len(captured[1][1]) < 21:
        failures.append("calendar authority/bound parameters missing")

    if failures:
        print("R170/R180 focused gate: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("R170/R180 focused gate: PASS")
    print("- registry/menu/permission/grain/raw joins: PASS")
    print("- status/outstanding/no-clamp/four-date business calendar: PASS")
    print("- Panel maker/order-vendor roles and integer identifiers: PASS")
    print("- result order/NLQ/source display/provenance: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
