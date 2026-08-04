"""Canonical SIMS action identity used by static NLQ coverage checks.

This module is deliberately data-only: importing it must not start Streamlit,
open a database connection, access session state, or import panel/view modules.
"""

from __future__ import annotations

from dataclasses import dataclass


IMPLEMENTED = "implemented"
DASHBOARD_ONLY = "dashboard_only"
DEPRECATED = "deprecated"


# Analytics NLQ is intentionally modeled as metric x grouping rather than as
# a phrase-to-action fallback.  The router uses this data to reject a requested
# grouping that has no canonical action before it can call a service.
ANALYTICS_INTENT_ACTIONS: dict[tuple[str, str], str] = {
    ("sales_forecast", "product"): "품목별 매출 예상",
    ("sales_forecast", "customer"): "매출처별 매출 예상",
    ("sales_forecast", "salesperson"): "영업사원별 매출 예상",
    ("sales_forecast", "region"): "지역별 매출 예상",
    ("sales_trend", "product"): "품목별 매출 추세 분석",
    ("sales_trend", "manufacturer"): "제약사별 매출 추세 분석",
    ("sales_trend_summary", "product"): "품목별 매출 추세 요약표",
    ("sales_trend_summary", "manufacturer"): "제약사별 매출 추세 분석 요약표",
    ("stock_shortage", "product"): "품목별 재고부족현황",
    ("stock_shortage", "purchase_vendor"): "매입처별 재고부족 현황",
}


@dataclass(frozen=True)
class CanonicalAction:
    canonical_action: str
    panel_category: str
    panel_action: str
    label_aliases: tuple[str, ...]
    nlq_action: str
    handler_kind: str
    handler_target: str
    implementation_status: str = IMPLEMENTED
    dashboard_only_reason: str = ""


def _spec(
    action: str,
    category: str,
    *,
    aliases: tuple[str, ...] = (),
    handler_kind: str,
    handler_target: str,
    status: str = IMPLEMENTED,
    dashboard_only_reason: str = "",
) -> CanonicalAction:
    return CanonicalAction(
        canonical_action=action,
        panel_category=category,
        panel_action=action,
        label_aliases=aliases,
        nlq_action=action if status == IMPLEMENTED else "",
        handler_kind=handler_kind,
        handler_target=handler_target,
        implementation_status=status,
        dashboard_only_reason=dashboard_only_reason,
    )


CANONICAL_ACTIONS: tuple[CanonicalAction, ...] = (
    _spec("사용자목록 + 부서명", "사용자", handler_kind="master", handler_target="app.sims.nlq.nlq_users.try_handle_users_nlq"),
    _spec("부서별 사용자 수", "사용자", aliases=("부서별사용자수",), handler_kind="master", handler_target="app.sims.nlq.nlq_users.try_handle_users_nlq"),
    _spec("최근 입사자", "사용자", handler_kind="master", handler_target="app.sims.nlq.nlq_users.try_handle_users_nlq"),
    _spec("그룹코드조회", "코드마스터", aliases=("그룹별 코드 조회",), handler_kind="master", handler_target="app.sims.nlq.nlq_codes.try_handle_codes_nlq"),
    _spec("코드명 검색", "코드마스터", handler_kind="master", handler_target="app.sims.nlq.nlq_codes.try_handle_codes_nlq"),
    _spec("거래처 목록", "거래처", handler_kind="master", handler_target="app.sims.nlq.nlq_vendors.try_handle_vendors_nlq"),
    _spec("거래처 상세", "거래처", handler_kind="master", handler_target="app.sims.nlq.nlq_vendors.try_handle_vendors_nlq"),
    _spec("도로명주소 조회", "도로명주소", handler_kind="router", handler_target="app.sims.nlq.nlq_router._try_handle_road_address_nlq"),
    _spec("제품코드 목록", "제품", aliases=("제품코드목록",), handler_kind="master", handler_target="app.sims.nlq.nlq_goods.try_handle_goods_nlq"),
    _spec("제품코드 상세", "제품", aliases=("제품코드상세",), handler_kind="master", handler_target="app.sims.nlq.nlq_goods.try_handle_goods_nlq"),
    _spec("Dashboard Lite v0.1", "분석/KPI", handler_kind="dashboard", handler_target="app.sims.views.dashboard_lite.render_dashboard_lite", status=DASHBOARD_ONLY, dashboard_only_reason="Dashboard는 저장 프로필과 조회 폼을 사용하는 전용 화면이며 일반 NLQ action으로 등록하지 않는다."),
    _spec("제약사별 매출 추세 분석", "분석/KPI", handler_kind="analytics", handler_target="app.sims.nlq.nlq_router._get_analytics_handler"),
    _spec("제약사별 매출 추세 분석 요약표", "분석/KPI", handler_kind="analytics", handler_target="app.sims.nlq.nlq_router._get_analytics_handler"),
    _spec("품목별 매출 추세 분석", "분석/KPI", handler_kind="analytics", handler_target="app.sims.nlq.nlq_router._get_analytics_handler"),
    _spec("품목별 매출 추세 요약표", "분석/KPI", handler_kind="analytics", handler_target="app.sims.nlq.nlq_router._get_analytics_handler"),
    _spec("품목별 매출 예상", "분석/KPI", handler_kind="analytics", handler_target="app.sims.nlq.nlq_router._get_analytics_handler"),
    _spec("매출처별 매출 예상", "분석/KPI", handler_kind="analytics", handler_target="app.sims.nlq.nlq_router._get_analytics_handler"),
    _spec("영업사원별 매출 예상", "분석/KPI", handler_kind="analytics", handler_target="app.sims.nlq.nlq_router._get_analytics_handler"),
    _spec("지역별 매출 예상", "분석/KPI", handler_kind="analytics", handler_target="app.sims.nlq.nlq_router._get_analytics_handler"),
    _spec("품목별 재고부족현황", "분석/KPI", handler_kind="analytics", handler_target="app.sims.nlq.nlq_router._get_analytics_handler"),
    _spec("매입처별 재고부족 현황", "분석/KPI", handler_kind="analytics", handler_target="app.sims.nlq.nlq_router._get_analytics_handler"),
    _spec("입고명세 조회", "입출고/명세서/재고", handler_kind="io_service", handler_target="app.services.rddbc110_service.get_rddbc110_result"),
    _spec("출고명세 조회", "입출고/명세서/재고", handler_kind="io_service", handler_target="app.services.rddbc120_service.get_rddbc120_result"),
    _spec("거래명세서 공통 조회", "입출고/명세서/재고", handler_kind="io_service", handler_target="app.services.rddbc130_service.get_rddbc130_result"),
    _spec("세금계산서 공통 조회", "입출고/명세서/재고", handler_kind="io_service", handler_target="app.services.rddbc140_service.get_rddbc140_result"),
    _spec("실재고월집계 조회", "입출고/명세서/재고", handler_kind="io_service", handler_target="app.services.rddbc210_service.get_rddbc210_result"),
    _spec("장부재고월집계 조회", "입출고/명세서/재고", handler_kind="io_service", handler_target="app.services.rddbc220_service.get_rddbc220_result"),
    _spec("입고↔거래명세서 검증", "입출고/명세서/재고", handler_kind="io_service", handler_target="app.services.rddbc110_service.get_rddbc110_result"),
    _spec("입고↔세금계산서 검증", "입출고/명세서/재고", handler_kind="io_service", handler_target="app.services.rddbc110_service.get_rddbc110_result"),
    _spec("출고↔거래명세서 검증", "입출고/명세서/재고", handler_kind="io_service", handler_target="app.services.rddbc120_service.get_rddbc120_result"),
    _spec("출고↔세금계산서 검증", "입출고/명세서/재고", handler_kind="io_service", handler_target="app.services.rddbc120_service.get_rddbc120_result"),
    _spec("제품수불현황 조회", "입출고/명세서/재고", aliases=("제품수불현황",), handler_kind="io_service", handler_target="app.services.product_flow_service.get_product_flow_result"),
    _spec("제품재고현황 조회", "입출고/명세서/재고", aliases=("제품재고현황",), handler_kind="io_service", handler_target="app.services.product_inventory_service.get_product_inventory_result"),
)


IO_VIEW_FALLBACK_TARGETS = {
    "입고명세 조회": "view_rddbc110",
    "출고명세 조회": "view_rddbc120",
    "거래명세서 공통 조회": "view_rddbc130",
    "세금계산서 공통 조회": "view_rddbc140",
    "실재고월집계 조회": "view_rddbc210",
    "장부재고월집계 조회": "view_rddbc220",
    "입고↔거래명세서 검증": "view_rddbc110_trans_check",
    "입고↔세금계산서 검증": "view_rddbc110_tax_check",
    "출고↔거래명세서 검증": "view_rddbc120_trans_check",
    "출고↔세금계산서 검증": "view_rddbc120_tax_check",
    "제품수불현황 조회": "view_product_flow",
    "제품재고현황 조회": "view_product_inventory",
}


def implemented_actions() -> tuple[CanonicalAction, ...]:
    return tuple(item for item in CANONICAL_ACTIONS if item.implementation_status == IMPLEMENTED)


def all_panel_labels() -> tuple[str, ...]:
    labels: list[str] = []
    for item in CANONICAL_ACTIONS:
        labels.append(item.panel_action)
        labels.extend(item.label_aliases)
    return tuple(labels)
