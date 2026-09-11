"""Focused truth-table checks for product trend, forecast, and shortage logic."""

from __future__ import annotations

import ast
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services import analytics_sales_trend_service as analytics
from app.services import analytics_supplier_stock_shortage_service as supplier_shortage
from app.services import rddbc170_rddbc180_order_service as order_service
from app.services import ssai_analysis_profile_service as profile_service
from app.services import dashboard_lite_facts as dashboard_facts
from app.services.dashboard_inventory_frequency_snapshot import assign_frequency_grades
from app.services.io_nlq import apply_nlq_default_period_policy
from app.ui.sims_table_display import (
    apply_sims_export_projection,
    resolve_sims_excel_number_format,
    resolve_sims_table_mode,
)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _forecast_row(**overrides: object) -> pd.Series:
    values: dict[str, object] = {
        "완료월총매출": 600.0,
        "총매출액": 600.0,
        "완료월평균매출": 100.0,
        "월평균매출": 100.0,
        "최근3개월평균매출": 100.0,
        "직전3개월평균매출": 100.0,
        "최근6개월평균매출": 100.0,
        "최근3개월증감률": 0.0,
        "매출발생월수": 6,
        "추세판정": "안정",
    }
    values.update(overrides)
    return pd.Series(values)


def test_trend_truth_table_and_nested_window_meaning() -> None:
    cases = (
        ((100.0, 100.0, 100.0, True), "반품주의"),
        ((-1.0, 100.0, 100.0, False), "반품주의"),
        ((0.0, 0.0, 0.0, False), "자료부족"),
        ((100.0, 0.0, 0.0, False), "감소"),
        ((100.0, 115.0, 100.0, False), "증가"),
        ((100.0, 85.0, 100.0, False), "감소"),
        ((100.0, 100.0, 100.0, False), "안정"),
    )
    for args, expected in cases:
        _assert(analytics._trend_judge(*args) == expected, f"trend {args} != {expected}")

    _assert(
        analytics._trend_judge(100.0, 10.0, 5.0, False, previous3=0.0) == "신규/증가",
        "new/increase must use the preceding three-month window",
    )
    _assert(
        analytics._trend_judge(100.0, 10.0, 5.0, False, previous3=1.0) == "증가",
        "positive preceding sales must not be classified as new/increase",
    )

    # If recent six months contain the recent three months, the 1.15/0.85
    # ratio means about +35.29%/-26.09% versus the preceding three months.
    up_prior3 = 2.0 * 115.0 / 1.15 - 115.0
    down_prior3 = 2.0 * 85.0 / 0.85 - 85.0
    _assert(math.isclose(115.0 / up_prior3 - 1.0, 0.3529411764705883), "up threshold meaning changed")
    _assert(math.isclose(85.0 / down_prior3 - 1.0, -0.26086956521739135), "down threshold meaning changed")

    nonnegative = [0.0, 0.0, 0.0, 10.0, 20.0, 30.0]
    previous3 = sum(nonnegative[:3]) / 3
    recent3 = sum(nonnegative[-3:]) / 3
    _assert(previous3 <= 0 < recent3, "disjoint-window reachability premise changed")


def test_forecast_truth_table_and_momentum_projection() -> None:
    cases = (
        (_forecast_row(추세판정="반품주의"), "반품주의"),
        (_forecast_row(완료월총매출=0, 총매출액=0), "자료부족"),
        (_forecast_row(매출발생월수=1), "자료부족"),
        (_forecast_row(직전3개월평균매출=0, 최근6개월평균매출=5, 최근3개월평균매출=10), "신규확인"),
        (_forecast_row(최근6개월평균매출=100, 최근3개월평균매출=0), "감소예상"),
        (_forecast_row(최근3개월증감률=20), "상승예상"),
        (_forecast_row(최근3개월증감률=-20), "감소예상"),
        (_forecast_row(최근3개월평균매출=116), "상승예상"),
        (_forecast_row(최근3개월평균매출=74), "감소예상"),
        (_forecast_row(), "안정예상"),
    )
    for row, expected in cases:
        _assert(analytics._forecast_grade(row) == expected, f"forecast grade != {expected}")

    label, applied_pct, projected = analytics._forecast_projection_from_row(
        _forecast_row(
            최근3개월평균매출=120,
            최근6개월평균매출=100,
            최근3개월증감률=20,
        )
    )
    _assert(label == "최근3개월평균매출", "forecast base priority changed")
    _assert(math.isclose(applied_pct, 10.0), "half-rate policy changed")
    _assert(math.isclose(projected, 132.0), "momentum projection changed")

    _, capped_up, projected_up = analytics._forecast_projection_from_row(
        _forecast_row(최근3개월평균매출=100, 최근3개월증감률=100)
    )
    _, capped_down, projected_down = analytics._forecast_projection_from_row(
        _forecast_row(최근3개월평균매출=100, 최근3개월증감률=-100)
    )
    _assert(math.isclose(capped_up, 15.0) and math.isclose(projected_up, 115.0), "positive clamp changed")
    _assert(math.isclose(capped_down, -15.0) and math.isclose(projected_down, 85.0), "negative clamp changed")


def test_shortage_truth_table_and_values() -> None:
    cases = (
        ((0.0, 0.0, 0.0), "재고없음/수요없음"),
        ((10.0, 0.0, 999.0), "수요관찰"),
        ((0.0, 10.0, 0.0), "재고없음"),
        ((9.0, 10.0, 0.9), "1개월내 부족"),
        ((10.0, 10.0, 1.0), "2개월내 부족"),
        ((19.0, 10.0, 1.9), "2개월내 부족"),
        ((20.0, 10.0, 2.0), "3개월내 부족"),
        ((29.0, 10.0, 2.9), "3개월내 부족"),
        ((30.0, 10.0, 3.0), "3개월내 부족주의"),
        ((31.0, 10.0, 3.1), "정상"),
    )
    for (stock, demand, cover), expected in cases:
        row = pd.Series({"현재재고수량": stock, "예상기준월수량": demand, "재고커버월수": cover})
        _assert(analytics._stock_shortage_grade(row) == expected, f"shortage {stock}/{demand}/{cover} != {expected}")

    stock = 15.0
    demand = 10.0
    shortages = [max(demand * horizon - stock, 0.0) for horizon in (1, 2, 3)]
    _assert(shortages == [0.0, 5.0, 15.0], "shortage quantity horizon changed")
    view_tree = ast.parse(
        (PROJECT_ROOT / "app" / "sims" / "views" / "analytics_views.py").read_text(encoding="utf-8-sig")
    )
    option_values = ()
    for node in view_tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "SHORTAGE_GRADE_OPTIONS"
            for target in node.targets
        ):
            option_values = ast.literal_eval(node.value)
            break
    _assert(option_values, "SHORTAGE_GRADE_OPTIONS must be statically discoverable")
    _assert("2개월내 부족주의" not in option_values, "new query UI must not expose the unreachable legacy label")


def test_shortage_current_time_axis_and_historical_policy() -> None:
    params, policy = apply_nlq_default_period_policy(
        {}, "품목별 재고부족현황", today=pd.Timestamp("2026-09-09").date()
    )
    _assert(policy["action_class"] == "current_inventory_analysis", "shortage period class changed")
    _assert(params["_shortage_evaluation_date"] == "20260909", "default evaluation date must be today")
    _assert(params["_shortage_analysis_date_from"] == "20260301", "completed six-month start changed")
    _assert(params["_shortage_analysis_date_to"] == "20260831", "completed six-month end changed")
    _assert(params["date_to"] == "20260909", "current stock source cutoff must remain the evaluation date")

    historical = analytics.normalize_stock_shortage_time_axis({
        "date_to": "20260815",
        "policy_date": "20260909",
    })
    _assert(historical["_shortage_evaluation_date"] == "20260815", "historical evaluation date changed")
    _assert(historical["_shortage_analysis_date_from"] == "20260201", "historical demand start changed")
    _assert(historical["_shortage_analysis_date_to"] == "20260731", "historical demand end changed")
    resolved = analytics._resolve_period_source_policy(historical)
    _assert(resolved["evaluation_mode"] == "historical_midmonth", "historical source policy changed")

    trend_params, trend_policy = apply_nlq_default_period_policy(
        {}, "품목별 매출 추세 분석", today=pd.Timestamp("2026-09-09").date()
    )
    _assert(trend_policy["default_policy"] == "completed_6months", "trend completed-six-month policy changed")
    _assert(trend_params["date_to"] == "20260831", "trend period was changed by shortage policy")

    supplier_params, supplier_policy = apply_nlq_default_period_policy(
        {}, "매입처별 재고부족 현황", today=pd.Timestamp("2026-09-09").date()
    )
    _assert(supplier_policy["action_class"] == "current_inventory_analysis", "supplier shortage period class changed")
    _assert(supplier_params["_shortage_evaluation_date"] == "20260909", "supplier default evaluation date must be today")
    _assert(supplier_params["_shortage_analysis_date_from"] == "20260301", "supplier completed six-month start changed")
    _assert(supplier_params["_shortage_analysis_date_to"] == "20260831", "supplier completed six-month end changed")
    _assert(supplier_params["date_to"] == "20260909", "supplier current stock cutoff must remain the evaluation date")

    supplier_historical = supplier_shortage.normalize_stock_shortage_time_axis({
        "date_to": "20260815", "policy_date": "20260909",
    })
    _assert(supplier_historical["_shortage_evaluation_date"] == "20260815", "supplier historical evaluation changed")
    _assert(supplier_historical["_shortage_analysis_date_to"] == "20260731", "supplier historical basis changed")

    captured_supplier_params: dict[str, object] = {}
    original_supplier_base = supplier_shortage.load_product_shortage_base
    try:
        def capture_supplier_base(candidate: dict[str, object]) -> pd.DataFrame:
            captured_supplier_params.update(candidate)
            return pd.DataFrame()

        supplier_shortage.load_product_shortage_base = capture_supplier_base
        supplier_shortage.get_supplier_stock_shortage_df({
            "date_to": "20260815", "policy_date": "20260909",
        })
    finally:
        supplier_shortage.load_product_shortage_base = original_supplier_base
    _assert(captured_supplier_params.get("_shortage_evaluation_date") == "20260815", "supplier wrapper lost historical evaluation")
    _assert(captured_supplier_params.get("_shortage_analysis_date_from") == "20260201", "supplier wrapper lost historical demand start")
    _assert(captured_supplier_params.get("_shortage_analysis_date_to") == "20260731", "supplier wrapper lost historical demand end")


def test_dashboard_daily_check_period_metadata_contract() -> None:
    params = dashboard_facts.normalize_dashboard_lite_params(
        dashboard_facts.default_dashboard_lite_scope(today=pd.Timestamp("2026-09-09").date()),
        today=pd.Timestamp("2026-09-09").date(),
    )
    period = {
        "month_from": params["month_from"],
        "month_to": params["month_to"],
        "evaluation_month": params["evaluation_month"],
        "judgement_date": params["policy_date"],
    }
    _assert(period["month_from"] == "202603" and period["month_to"] == "202608", "daily-check display period changed")
    _assert(period["evaluation_month"] == "202609", "daily-check evaluation month changed")
    _assert(period["judgement_date"] == "20260909", "daily-check policy date changed")


def test_shortage_demand_consistency_and_user_projection() -> None:
    actual_demand = pd.Series({
        "현재재고수량": 0,
        "예상기준월수량": 0,
        "평가월 예상수요수량": 0,
        "평가월 실제수요수량": 3,
        "평가월 잔여예상수요수량": 0,
        "재고커버월수": 0,
        "부족예상수량": 0,
        "당월 잔여예상출고수량": 0,
        "당월 재고충족률": 100,
    })
    _assert(analytics._stock_shortage_grade(actual_demand) == "재고없음", "actual demand was labeled no-demand")
    _assert(analytics._stock_shortage_current_judge(actual_demand) == "적정", "zero remaining demand must not conflict with fill rate")

    no_demand = actual_demand.copy()
    no_demand["평가월 실제수요수량"] = 0
    _assert(analytics._stock_shortage_grade(no_demand) == "재고없음/수요없음", "true no-demand grade changed")
    _assert(analytics._stock_shortage_current_judge(no_demand) == "수요없음", "true no-demand judge changed")

    full = pd.DataFrame({
        "제품코드": ["07886"],
        "제조사명": ["종근당"],
        "현재재고수량": [-1],
        "평가월 예상수요수량": [5],
        "당월 예상출고수량": [5],
        "최근3개월평균수요수량": [2],
        "최근3개월평균출고수량": [2],
        "입고예정수량": [5],
    })
    projected = analytics._stock_shortage_user_projection(full)
    _assert("당월 예상출고수량" not in projected.columns, "duplicate display alias remained")
    _assert("평가월 예상수요수량" in projected.columns, "representative demand column was removed")
    _assert("당월 예상출고수량" in full.columns, "full current-table source was mutated")
    _assert(projected["제조사명"].eq("종근당").all(), "manufacturer query scope was lost")
    full.attrs["sims_export_columns"] = list(projected.columns)
    exported = apply_sims_export_projection(full)
    _assert(list(exported.columns) == list(projected.columns), "Excel/CSV projection diverged from display")
    _assert(resolve_sims_table_mode(projected, meta={"semantic_styled_max_rows": 300})["mode"] == "small", "300-row semantic style contract changed")
    _assert(resolve_sims_excel_number_format("현재재고수량") == "#,##0", "shared Excel number format changed")


def test_historical_shortage_does_not_mix_current_expected_inbound() -> None:
    original_stock_loader = analytics._load_product_current_stock
    original_inbound_loader = order_service.get_expected_inbound_product_totals
    inbound_calls = 0

    def forbidden_inbound_loader(_params: dict[str, object]) -> pd.DataFrame:
        nonlocal inbound_calls
        inbound_calls += 1
        raise AssertionError("historical shortage must not call the current expected-inbound source")

    analytics._load_product_current_stock = lambda *_args, **_kwargs: pd.DataFrame({
        "제품코드": ["07886"],
        "장부재고수량": [0],
        "실재고수량": [0],
        "장부재고금액": [0],
        "실재고금액": [0],
        "장부재고평가단가": [0],
        "실재고평가단가": [0],
    })
    order_service.get_expected_inbound_product_totals = forbidden_inbound_loader
    base = pd.DataFrame({
        "제품코드": ["07886"],
        "제품명": ["종근 리포덱스정#450/100T"],
        "최근3개월평균수요수량": [5],
        "최근6개월평균수요수량": [5],
        "예상기준월수량": [5],
        "평가월 예상수요수량": [5],
        "평가월 실제수요수량": [1],
        "평가월 잔여예상수요수량": [4],
    })
    try:
        result = analytics.get_stock_shortage_df(
            {"date_to": "20260815", "policy_date": "20260909"},
            sales_forecast_df=base,
            include_expected_inbound=True,
        )
    finally:
        analytics._load_product_current_stock = original_stock_loader
        order_service.get_expected_inbound_product_totals = original_inbound_loader

    _assert(inbound_calls == 0, "current expected-inbound source was called for a historical evaluation")
    _assert(result["입고예정수량"].eq(0).all(), "historical expected inbound must fail closed")
    _assert(result.attrs["expected_inbound_source_call_count"] == 0, "historical inbound call provenance changed")
    _assert(result.attrs["expected_inbound_historical_authority"] == "unavailable", "historical authority status changed")


def test_frequency_and_expected_inbound_grain_contracts() -> None:
    grades = assign_frequency_grades({"00001": 3, "00002": 0}, ["00001", "00002"])
    by_code = {row["product_code"]: row for row in grades}
    _assert(by_code["00001"]["occurrence_count_3m"] == 3, "snapshot event count changed")
    _assert(by_code["00002"]["frequency_grade"] == "X", "zero-event grade changed")

    def projection_reader(**kwargs: object) -> SimpleNamespace:
        _assert(kwargs["stock_codes"] == ("000001", "000002"), "snapshot stock scope changed")
        return SimpleNamespace(
            status="ready",
            reason="",
            usable=True,
            generation_no=7,
            checksum="approved-checksum",
            rows=(
                {"product_code": "00001", "frequency_grade": "A", "occurrence_count_3m": 3},
                {"product_code": "00002", "frequency_grade": "X", "occurrence_count_3m": 0},
            ),
        )

    attached = analytics._attach_approved_outbound_characteristics(
        pd.DataFrame({"제품코드": ["00001", "00002"]}),
        {
            "stock_cd_list": ["000002", "000001"],
            "policy_date": "20260908",
            "date_to": "20260908",
        },
        projection_reader=projection_reader,
    )
    _assert(attached["출고빈도등급"].tolist() == ["A", "X"], "approved frequency grade projection changed")
    _assert(attached["출고횟수"].tolist() == [3, 0], "approved occurrence projection changed")
    _assert(attached["출고일수"].isna().all(), "unsupported distinct-day facts must fail closed")
    _assert(attached["출고거래처수"].isna().all(), "unsupported distinct-vendor facts must fail closed")
    _assert(attached.attrs["outbound_characteristics_additional_erp_call_count"] == 0, "live fallback added")

    # The same product/date in two stock locations is one product outbound day,
    # while summing stock-grain day counts would incorrectly return two.
    outbound = pd.DataFrame([
        {"product": "00001", "date": "20260901", "stock": "000001", "vendor": "01000"},
        {"product": "00001", "date": "20260901", "stock": "000002", "vendor": "01001"},
    ])
    stock_day_sum = outbound.groupby(["product", "stock"])["date"].nunique().sum()
    product_days = outbound.groupby("product")["date"].nunique().iloc[0]
    _assert(stock_day_sum == 2 and product_days == 1, "distinct-day duplication fixture changed")
    _assert(outbound.groupby("product")["vendor"].nunique().iloc[0] == 2, "distinct-vendor fixture changed")

    inbound = pd.DataFrame([
        {"company": "07", "product": "00001", "stock": "000001", "detail": "01", "미입고수량": 3},
        {"company": "07", "product": "00001", "stock": "000001", "detail": "02", "미입고수량": 2},
        {"company": "07", "product": "00002", "stock": "000001", "detail": "01", "미입고수량": 4},
    ])
    expected = (
        inbound.groupby(["company", "product"], as_index=False)["미입고수량"].sum()
        .rename(columns={"product": "제품코드", "미입고수량": "입고예정수량"})
    )
    expected.attrs.update({"source_call_count": 1, "business_day_count": 4, "stock_scope": ["000001"]})
    shortage = pd.DataFrame([
        {"제품코드": "00001", "부족예상수량": 7},
        {"제품코드": "00002", "부족예상수량": 1},
    ])
    before = shortage["부족예상수량"].copy()
    merged = analytics._merge_expected_inbound_projection(shortage, expected)
    _assert(merged["입고예정수량"].tolist() == [5, 4], "expected-inbound pre-aggregation changed")
    _assert(merged["입고예정 반영 부족수량"].tolist() == [2, 0], "inbound-adjusted shortage changed")
    _assert(merged["부족예상수량"].equals(before), "existing shortage quantity changed")
    _assert(merged.attrs["expected_inbound_source_call_count"] == 1, "expected inbound source call changed")
    _assert(merged.attrs["expected_inbound_business_day_count"] == 4, "four-business-day policy changed")

    duplicate = pd.concat([expected.iloc[[0]], expected.iloc[[0]]], ignore_index=True)
    try:
        analytics._merge_expected_inbound_projection(shortage, duplicate)
    except ValueError:
        pass
    else:
        raise AssertionError("duplicate product inbound projection must fail closed")


def test_expected_inbound_sql_projection_contract() -> None:
    captured: list[dict[str, object]] = []
    original_execute = order_service.execute_bound_select
    original_calendar = order_service.recent_business_days

    def fake_execute(sql: str, values: list[object]) -> pd.DataFrame:
        captured.append({"sql": sql, "values": values})
        if values[-1:] == ["000009"] and len(values) == 5:
            return pd.DataFrame(columns=["제품코드", "입고예정수량", "_공백재고위치행수"])
        return pd.DataFrame({
            "제품코드": ["07886"],
            "입고예정수량": [5],
            "_공백재고위치행수": [1 if "OR NULLIF" in sql else 0],
        })

    order_service.execute_bound_select = fake_execute
    order_service.recent_business_days = lambda **_kwargs: order_service.RecentBusinessDaysResult(
        status="ready",
        dates=("20260908", "20260907", "20260904", "20260903"),
    )
    try:
        full_result = order_service.get_expected_inbound_product_totals(
            {
                "_today": "20260908",
                "stock_cd_list": ["000001", "000008", "000009"],
                "include_blank_stock_cd": True,
            }
        )
        partial_result = order_service.get_expected_inbound_product_totals(
            {"_today": "20260908", "stock_cd_list": ["000001"]}
        )
        mismatch_result = order_service.get_expected_inbound_product_totals(
            {"_today": "20260908", "stock_cd_list": ["000009"]}
        )
    finally:
        order_service.execute_bound_select = original_execute
        order_service.recent_business_days = original_calendar

    full_sql = str(captured[0]["sql"])
    partial_sql = str(captured[1]["sql"])
    full_values = list(captured[0]["values"])
    _assert("GROUP BY D.Rd18_Physic_Cd" in full_sql, "expected inbound must pre-aggregate by product")
    _assert("D.Rd18_Stock_Cd IN (?,?,?)" in full_sql, "stock scope must be pushed down")
    _assert("OR NULLIF(RTRIM(D.Rd18_Stock_Cd), '') IS NULL" in full_sql, "full scope must include blank stock")
    _assert("OR NULLIF(RTRIM(D.Rd18_Stock_Cd), '') IS NULL" not in partial_sql, "partial scope must exclude blank stock")
    _assert(order_service._NORMAL_ORDER_PREDICATE in full_sql, "negative return exclusion changed")
    _assert("IN ('1', '2')" in full_sql, "expected inbound status scope changed")
    _assert("dbo.WB_Holiday" not in full_sql, "ERP holiday-table dependency returned")
    _assert(full_values[:4] == ["20260908", "20260907", "20260904", "20260903"], "business-date binding changed")
    _assert("ABS(" not in full_sql.upper() and "CLIP" not in full_sql.upper(), "quantity clamp/absolute conversion added")
    _assert(full_values[-3:] == ["000001", "000008", "000009"], "leading-zero stock scope changed")
    _assert(full_result.loc[0, "제품코드"] == "07886", "blank-stock fixture product changed")
    _assert(full_result.loc[0, "입고예정수량"] == 5, "full scope must retain blank-stock quantity")
    _assert(full_result.attrs["blank_stock_location_included_rows"] == 1, "blank include diagnostic changed")
    _assert(partial_result.attrs["blank_stock_location_included_rows"] == 0, "partial scope reported blank inclusion")
    _assert(mismatch_result.empty, "mismatched explicit stock must remain excluded")
    _assert(full_result.attrs["source_call_count"] == 1, "expected inbound query count changed")
    _assert(full_result.attrs["business_day_count"] == 4, "expected inbound business-day count changed")


def test_company_default_stock_scope_authority() -> None:
    def profile_loader(**kwargs: object) -> profile_service.DashboardProfileLoadResult:
        _assert(kwargs["company_id"] == 7, "company scope changed")
        return profile_service.DashboardProfileLoadResult(
            status="ready",
            company_id=7,
            profile={"stock_cd_list": ["0018:00001", "0018:00008", "0018:00009"]},
        )

    full = profile_service.resolve_company_default_stock_scope(
        company_id=7,
        selected_codes=["00009", "00001", "00008"],
        profile_loader=profile_loader,
    )
    partial = profile_service.resolve_company_default_stock_scope(
        company_id=7,
        selected_codes=["00001", "00008"],
        profile_loader=profile_loader,
    )
    explicit_full = profile_service.resolve_company_default_stock_scope(
        company_id=7,
        selected_codes=[],
        explicit_full=True,
        profile_loader=profile_loader,
    )
    _assert(full["is_full_default_scope"] is True, "company Default scope was not recognized")
    _assert(full["scope_source"] == "company_default", "Default authority provenance changed")
    _assert(partial["is_full_default_scope"] is False, "partial scope was treated as full")
    _assert(explicit_full["is_full_default_scope"] is True, "explicit full selection was lost")
    _assert(full["selected_count"] == 3 and full["default_count"] == 3, "scope counts changed")


def main() -> int:
    tests = (
        test_trend_truth_table_and_nested_window_meaning,
        test_forecast_truth_table_and_momentum_projection,
        test_shortage_truth_table_and_values,
        test_shortage_current_time_axis_and_historical_policy,
        test_dashboard_daily_check_period_metadata_contract,
        test_shortage_demand_consistency_and_user_projection,
        test_historical_shortage_does_not_mix_current_expected_inbound,
        test_frequency_and_expected_inbound_grain_contracts,
        test_expected_inbound_sql_projection_contract,
        test_company_default_stock_scope_authority,
    )
    for test in tests:
        test()
    print(f"PASS analytics trend/forecast/shortage logic ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
