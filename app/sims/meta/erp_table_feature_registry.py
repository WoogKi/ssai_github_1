"""Metadata registry for incrementally added ERP table features.

Rddbc070 and later table features are registered incrementally. Existing table
features keep their current routing until independently migrated.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import re
from typing import Any, Callable


@dataclass(frozen=True)
class ErpFilterSpec:
    key: str
    label: str
    column: str
    kind: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ErpTableActionSpec:
    action: str
    aliases: tuple[str, ...]
    mode: str
    service_function: str
    export_function: str
    view_function: str
    payload_key: str
    nlq_period_policy: str = "list_detail"


@dataclass(frozen=True)
class ErpTableFeatureSpec:
    table_key: str
    source_table: str
    category: str
    required_permission: str
    primary_key: tuple[str, ...]
    filters: tuple[ErpFilterSpec, ...]
    actions: tuple[ErpTableActionSpec, ...]
    current_table_kind: str = "generic"
    menu_business_group: str = ""
    menu_target: str = ""
    limit_policy: str = "common_display_export_safety"
    source_limit_env: str = "SIMS_IO_QUERY_MAX_ROWS"


RDDBC070 = ErpTableFeatureSpec(
    table_key="rddbc070",
    source_table="dbo.Rddbc070",
    category="제품",
    required_permission="IO_READ",
    primary_key=("Rd07_Cost_Apply_Cd", "Rd07_Physic_Cd", "Rd07_Start_Date"),
    filters=(
        ErpFilterSpec(
            "ven_cd", "단가적용거래처 코드", "Rd07_Cost_Apply_Cd", "code",
            ("단가적용거래처코드", "단가적용처코드", "거래처코드"),
        ),
        ErpFilterSpec(
            "ven_nm", "단가적용거래처명", "Rd03_Ven_Nm", "text",
            (
                "단가적용거래처명", "단가적용거래처",
                "단가적용처명", "단가적용처", "거래처명", "거래처",
            ),
        ),
        ErpFilterSpec(
            "physic_cd", "제품코드", "Rd07_Physic_Cd", "code",
            ("제품코드", "품목코드"),
        ),
        ErpFilterSpec(
            "physic_nm", "제품명", "Rd04_Physic_Nm", "text",
            ("제품명", "품목명", "제품", "품목"),
        ),
        ErpFilterSpec(
            "product_keyword", "제품 키워드", "Rd04_Physic_Nm/Rd04_Physic_Cd/Rd04_Insu_Cd", "text",
            ("제품키워드", "품목키워드", "키워드"),
        ),
        ErpFilterSpec("insu_cd", "보험코드", "Rd04_Insu_Cd", "code", ("보험코드",)),
        ErpFilterSpec("barcode", "바코드", "Rd04_Bar_Code1..5", "code", ("바코드",)),
        ErpFilterSpec("maker_nm", "제약사명", "Rd03_Ven_Nm", "text", ("제약사", "제조사")),
        ErpFilterSpec("product_group_nm", "제품그룹명", "Rd01_Hnm", "text", ("제품그룹",)),
        ErpFilterSpec("product_di_nm", "제품구분명", "Rd01_Hnm", "text", ("제품구분", "구분명")),
        ErpFilterSpec(
            "product_di_semantic_group", "상위 제품구분", "Rd04_Physic_Di", "semantic_group",
            (
                "전문약", "전문제품", "보험약", "보험제품", "ETC",
                "일반약", "일반제품", "비보험약", "비보험제품", "OTC",
            ),
        ),
        ErpFilterSpec("product_class_nm", "제품분류명", "Rd01_Hnm", "text", ("제품분류",)),
        ErpFilterSpec("product_unit_price", "제품마스터 단가", "calculated unit_price", "range", ("제품단가",)),
        ErpFilterSpec("product_final_price_date", "최종단가변경일자", "calculated final_price_date", "date_range"),
        ErpFilterSpec("product_only_use", "사용 제품만", "Rd04_Use_Gu", "bool", ("사용품목만",)),
        ErpFilterSpec("product_add_user_nm", "제품 등록자", "Rd06_User_Nm", "text"),
        ErpFilterSpec("product_add_date_from", "제품 등록일 시작", "Rd04_Add_Date", "date"),
        ErpFilterSpec("product_add_date_to", "제품 등록일 종료", "Rd04_Add_Date", "date"),
        ErpFilterSpec("product_mod_user_nm", "제품 수정자", "Rd06_User_Nm", "text"),
        ErpFilterSpec("product_mod_date_from", "제품 수정일 시작", "Rd04_Mod_Date", "date"),
        ErpFilterSpec("product_mod_date_to", "제품 수정일 종료", "Rd04_Mod_Date", "date"),
        ErpFilterSpec("date_from", "계약시작일 시작", "Rd07_Start_Date", "date"),
        ErpFilterSpec("date_to", "계약시작일 종료", "Rd07_Start_Date", "date"),
        ErpFilterSpec("as_of", "기준일", "Rd07_Start_Date", "as_of", ("기준일",)),
    ),
    actions=(
        ErpTableActionSpec(
            action="최종 계약단가 조회",
            aliases=(
                "최종계약단가조회", "기준일 최종 계약단가 조회",
                "현재 계약단가 조회", "현재계약단가조회", "기준일 현재 계약단가 조회",
                "현재 계약 단가 조회", "계약단가 조회", "계약 단가", "계약단가",
                "단가계약 조회", "단가 계약 조회", "단가계약",
            ),
            mode="current",
            service_function="app.services.rddbc070_service.get_rddbc070_current_result",
            export_function="app.services.rddbc070_service.get_rddbc070_current_export_df",
            view_function="app.sims.views.rddbc_io_views.view_rddbc070_current",
            payload_key="__io070_current_last_payload",
            nlq_period_policy="explicit_only",
        ),
        ErpTableActionSpec(
            action="계약단가 이력 조회",
            aliases=(
                "계약단가이력조회", "계약단가 이력", "계약 변경내역",
                "계약변경내역", "과거 계약단가", "과거계약단가",
            ),
            mode="history",
            service_function="app.services.rddbc070_service.get_rddbc070_history_result",
            export_function="app.services.rddbc070_service.get_rddbc070_history_export_df",
            view_function="app.sims.views.rddbc_io_views.view_rddbc070_history",
            payload_key="__io070_history_last_payload",
            nlq_period_policy="explicit_only",
        ),
    ),
    menu_business_group="마스터관리",
    menu_target="제품",
)


RDDBC230 = ErpTableFeatureSpec(
    table_key="rddbc230",
    source_table="dbo.Rddbc230",
    category="재고",
    required_permission="IO_READ",
    primary_key=(
        "Rd23_Physic_Cd", "Rd23_Ven_Cd", "Rd23_Stock_Cd",
        "Rd23_Stock_Apply_Cd", "Rd23_Cost_Apply_Cd",
        "Rd23_Unit_Cost", "Rd23_Fin_Unit_Cost",
    ),
    filters=(
        ErpFilterSpec("physic_cd", "제품코드", "Rd23_Physic_Cd", "code", ("제품코드", "품목코드")),
        ErpFilterSpec("physic_nm", "제품명", "Rd04_Physic_Nm", "text", ("제품명", "품목명", "제품", "품목")),
        ErpFilterSpec("product_keyword", "제품 키워드", "Rd04_Physic_Nm/Rd04_Physic_Cd/Rd04_Insu_Cd", "text", ("제품키워드", "품목키워드", "키워드")),
        ErpFilterSpec("insu_cd", "보험코드", "Rd04_Insu_Cd", "code", ("보험코드",)),
        ErpFilterSpec("barcode", "바코드", "Rd04_Bar_Code1..5", "code", ("바코드",)),
        ErpFilterSpec("maker_nm", "제약사명", "Rd03_Ven_Nm", "text", ("제약사", "제조사")),
        ErpFilterSpec("product_group_nm", "제품그룹명", "Rd01_Hnm", "text", ("제품그룹",)),
        ErpFilterSpec("product_di_nm", "제품구분명", "Rd01_Hnm", "text", ("제품구분", "구분명")),
        ErpFilterSpec(
            "product_di_semantic_group", "상위 제품구분", "Rd04_Physic_Di", "semantic_group",
            (
                "전문약", "전문제품", "보험약", "보험제품", "ETC",
                "일반약", "일반제품", "비보험약", "비보험제품", "OTC",
            ),
        ),
        ErpFilterSpec("product_class_nm", "제품분류명", "Rd01_Hnm", "text", ("제품분류",)),
        ErpFilterSpec("buy_cd", "매입처코드", "Rd23_Ven_Cd", "code", ("매입처코드",)),
        ErpFilterSpec("buy_nm", "매입처명", "Rd03_Ven_Nm", "text", ("매입처명", "매입처")),
        ErpFilterSpec("stock_cd", "재고위치", "Rd23_Stock_Cd", "code", ("재고위치", "재고위치코드")),
        ErpFilterSpec("stock_nm", "재고위치명", "Rd01_Hnm", "text", ("재고위치명",)),
        ErpFilterSpec("stock_apply_cd", "재고적용코드", "Rd23_Stock_Apply_Cd", "code", ("재고적용코드", "재고적용처코드")),
        ErpFilterSpec("stock_apply_nm", "재고적용처명", "Rd03_Ven_Nm", "text", ("재고적용처명", "재고적용처")),
        ErpFilterSpec("cost_apply_cd", "단가적용코드", "Rd23_Cost_Apply_Cd", "code", ("단가적용코드", "단가적용처코드")),
        ErpFilterSpec("cost_apply_nm", "단가적용처명", "Rd03_Ven_Nm", "text", ("단가적용처명", "단가적용처")),
        ErpFilterSpec("unit_cost", "실입고단가", "Rd23_Unit_Cost", "range", ("실입고단가",)),
        ErpFilterSpec("fin_unit_cost", "장부입고단가", "Rd23_Fin_Unit_Cost", "range", ("장부입고단가",)),
        ErpFilterSpec("in_quantity", "입고수량", "Rd23_In_Quantity", "range", ("입고수량",)),
        ErpFilterSpec("out_quantity", "출고수량", "Rd23_Out_Quantity", "range", ("출고수량",)),
    ),
    actions=(
        ErpTableActionSpec(
            action="최종 매입단가 조회",
            aliases=(
                "최종매입단가조회", "제품별 최종 매입단가 조회", "제품별최종매입단가조회",
                "최종 매입가 조회", "최종매입가조회", "최종 매입가", "최종매입가",
                "실입고단가 조회", "실입고단가조회", "장부입고단가 조회", "장부입고단가조회",
            ),
            mode="state",
            service_function="app.services.rddbc230_service.get_rddbc230_result",
            export_function="app.services.rddbc230_service.get_rddbc230_export_df",
            view_function="app.sims.views.rddbc_io_views.view_rddbc230",
            payload_key="__io230_last_payload",
            nlq_period_policy="explicit_only",
        ),
    ),
    menu_business_group="재고관리",
    menu_target="재고",
    source_limit_env="SIMS_EXPORT_MAX_ROWS",
)


RDDBC170_RDDBC180 = ErpTableFeatureSpec(
    table_key="rddbc170_rddbc180",
    source_table="dbo.Rddbc180",
    category="발주",
    required_permission="IO_READ",
    primary_key=(
        "Rd18_Or_YyMmDd", "Rd18_OrVen_Cd", "Rd18_Or_Seq", "Rd18_Orsub_Seq",
    ),
    filters=(
        ErpFilterSpec("date_from", "발주일자 시작", "Rd17_Or_YyMmDd", "date", ("발주일자",)),
        ErpFilterSpec("date_to", "발주일자 종료", "Rd17_Or_YyMmDd", "date", ("발주일자",)),
        ErpFilterSpec("due_date_from", "납기일자 시작", "Rd17_Put_YyMmDd", "date", ("납기일자",)),
        ErpFilterSpec("due_date_to", "납기일자 종료", "Rd17_Put_YyMmDd", "date", ("납기일자",)),
        ErpFilterSpec("order_vendor_cd", "발주처코드", "Rd18_OrVen_Cd", "code", ("발주거래처코드",)),
        ErpFilterSpec("order_vendor_nm", "발주처명", "Rd03_Ven_Nm", "text", ("발주처", "발주거래처명", "발주거래처")),
        ErpFilterSpec("physic_cd", "제품코드", "Rd18_Physic_Cd", "code", ("품목코드",)),
        ErpFilterSpec("physic_nm", "제품명", "Rd04_Physic_Nm", "text", ("제품", "품목명", "품목")),
        ErpFilterSpec("product_keyword", "제품 키워드", "Rd04_Physic_Nm/Rd04_Physic_Cd/Rd04_Insu_Cd", "text", ("제품키워드", "품목키워드", "키워드")),
        ErpFilterSpec("insu_cd", "보험코드", "Rd04_Insu_Cd", "code", ("보험코드",)),
        ErpFilterSpec("barcode", "바코드", "Rd04_Bar_Code1..5", "code", ("바코드",)),
        ErpFilterSpec("maker_nm", "제약사명", "Rd03_Ven_Nm", "text", ("제약사", "제조사")),
        ErpFilterSpec("product_group_nm", "제품그룹명", "Rd01_Hnm", "text", ("제품그룹",)),
        ErpFilterSpec("product_di_nm", "제품구분명", "Rd01_Hnm", "text", ("제품구분", "구분명")),
        ErpFilterSpec("product_di_semantic_group", "상위 제품구분", "Rd04_Physic_Di", "semantic_group", ("전문약", "전문제품", "전문의약품", "보험약", "보험제품", "ETC", "일반약", "일반제품", "일반의약품", "비보험약", "비보험제품", "OTC")),
        ErpFilterSpec("product_class_nm", "제품분류명", "Rd01_Hnm", "text", ("제품분류",)),
        ErpFilterSpec("status_code", "발주상태", "Rd18_Or_Di", "code", ("발주상태코드",)),
        ErpFilterSpec("cost_apply_cd", "단가적용처코드", "Rd18_Cost_Apply_Cd", "code", ("단가적용코드",)),
        ErpFilterSpec("cost_apply_nm", "단가적용처명", "Rd03_Ven_Nm", "text", ("단가적용처",)),
        ErpFilterSpec("stock_apply_cd", "재고적용처코드", "Rd18_Stock_Apply_Cd", "code", ("재고적용코드",)),
        ErpFilterSpec("stock_apply_nm", "재고적용처명", "Rd03_Ven_Nm", "text", ("재고적용처",)),
        ErpFilterSpec("stock_cd", "재고위치코드", "Rd18_Stock_Cd", "code", ("재고위치",)),
        ErpFilterSpec("stock_nm", "재고위치명", "Rd01_Hnm", "text", ("재고위치명",)),
        ErpFilterSpec("expected_vendor_cd", "예상매출처코드", "Rd18_Pro_Ven_Cd", "code"),
        ErpFilterSpec("expected_vendor_nm", "예상매출처명", "Rd03_Ven_Nm", "text", ("예상매출처",)),
        ErpFilterSpec("real_vendor_cd", "실납처코드", "Rd18_Real_Ven_Cd", "code"),
        ErpFilterSpec("real_vendor_nm", "실납처명", "Rd03_Ven_Nm", "text", ("실납처",)),
        ErpFilterSpec("has_outstanding", "미입고 존재", "calculated outstanding quantity", "bool", ("미입고",)),
    ),
    actions=(
        ErpTableActionSpec(
            action="발주조회",
            aliases=("발주 조회", "발주내역 조회", "발주 내역 조회", "최근 발주내역 조회", "최근 발주 조회", "오늘 발주 조회", "이번주 발주 조회", "입고중 발주 조회", "미입고 발주 조회", "발주상태"),
            mode="order",
            service_function="app.services.rddbc170_rddbc180_order_service.get_order_result",
            export_function="app.services.rddbc170_rddbc180_order_service.get_order_export_df",
            view_function="app.sims.views.rddbc_io_views.view_order_query",
            payload_key="__io170_order_last_payload",
        ),
        ErpTableActionSpec(
            action="입고예정조회",
            aliases=(
                "입고예정", "입고 예정", "입고예정 조회", "입고예정 제품 조회",
                "오늘 입고예정 조회", "미입고 예정 조회", "입고중 제품 조회",
            ),
            mode="expected",
            service_function="app.services.rddbc170_rddbc180_order_service.get_expected_inbound_result",
            export_function="app.services.rddbc170_rddbc180_order_service.get_expected_inbound_export_df",
            view_function="app.sims.views.rddbc_io_views.view_expected_inbound_query",
            payload_key="__io180_expected_last_payload",
        ),
    ),
    menu_business_group="재고관리",
    menu_target="발주",
)


ERP_TABLE_FEATURES: tuple[ErpTableFeatureSpec, ...] = (RDDBC070, RDDBC230, RDDBC170_RDDBC180)


def iter_action_specs() -> tuple[tuple[ErpTableFeatureSpec, ErpTableActionSpec], ...]:
    return tuple(
        (feature, action)
        for feature in ERP_TABLE_FEATURES
        for action in feature.actions
    )


def menu_actions(*, business_group: str, target: str) -> tuple[str, ...]:
    """Return registry actions placed in one existing three-level menu slot."""
    return tuple(
        action.action
        for feature, action in iter_action_specs()
        if feature.menu_business_group == str(business_group or "").strip()
        and feature.menu_target == str(target or "").strip()
    )


def menu_targets(*, business_group: str) -> tuple[str, ...]:
    """Return registry targets in declaration order for one business group."""
    return tuple(
        dict.fromkeys(
            feature.menu_target
            for feature in ERP_TABLE_FEATURES
            if feature.menu_business_group == str(business_group or "").strip()
        )
    )


def get_action_spec(action: Any) -> tuple[ErpTableFeatureSpec, ErpTableActionSpec] | None:
    normalized = re.sub(r"\s+", " ", str(action or "").strip())
    compact = re.sub(r"\s+", "", normalized)
    for feature, action_spec in iter_action_specs():
        labels = (action_spec.action, *action_spec.aliases)
        if any(compact == re.sub(r"\s+", "", label) for label in labels):
            return feature, action_spec
    return None


def filter_labels(feature: ErpTableFeatureSpec, key: str) -> tuple[str, ...]:
    """Return the registry-owned labels used by UI/NLQ filter consumers."""
    for spec in feature.filters:
        if spec.key == key:
            return (spec.label, *spec.aliases)
    return ()


def match_action_in_text(text: Any) -> tuple[ErpTableFeatureSpec, ErpTableActionSpec] | None:
    normalized = str(text or "").strip()
    compact = re.sub(r"\s+", "", normalized)
    if not compact:
        return None
    candidates: list[tuple[int, ErpTableFeatureSpec, ErpTableActionSpec]] = []
    for feature, action_spec in iter_action_specs():
        for label in (action_spec.action, *action_spec.aliases):
            label_compact = re.sub(r"\s+", "", label)
            if label_compact and label_compact in compact:
                candidates.append((len(label_compact), feature, action_spec))
    if not candidates:
        return None
    if any(marker in compact for marker in ("이력", "과거", "변경내역")):
        history_candidates = [item for item in candidates if item[2].mode == "history"]
        if not history_candidates:
            matched_features = {item[1].table_key: item[1] for item in candidates}
            history_candidates = [
                (0, feature, action_spec)
                for feature in matched_features.values()
                for action_spec in feature.actions
                if action_spec.mode == "history"
            ]
        if history_candidates:
            candidates = history_candidates
    _, feature, action_spec = max(candidates, key=lambda item: item[0])
    return feature, action_spec


def resolve_dotted_callable(target: str) -> Callable[..., Any]:
    module_name, attr_name = str(target).rsplit(".", 1)
    value = getattr(importlib.import_module(module_name), attr_name)
    if not callable(value):
        raise TypeError(f"등록된 handler가 callable이 아닙니다: {target}")
    return value


def registry_completeness_errors() -> list[str]:
    errors: list[str] = []
    seen_actions: set[str] = set()
    for feature, action in iter_action_specs():
        if action.action in seen_actions:
            errors.append(f"duplicate action: {action.action}")
        seen_actions.add(action.action)
        if not feature.source_table.startswith("dbo.Rddbc"):
            errors.append(f"invalid source table: {feature.source_table}")
        if not feature.primary_key:
            errors.append(f"missing primary key: {feature.table_key}")
        if not feature.menu_business_group or not feature.menu_target:
            errors.append(f"missing menu placement: {feature.table_key}")
        if feature.menu_target and feature.category != feature.menu_target:
            errors.append(f"menu/category mismatch: {feature.table_key}")
        if feature.limit_policy != "common_display_export_safety":
            errors.append(f"unsupported limit policy: {feature.table_key}")
        for name, target in (
            ("service", action.service_function),
            ("export", action.export_function),
            ("view", action.view_function),
        ):
            if not target or "." not in target:
                errors.append(f"missing {name}: {action.action}")
        if not action.payload_key:
            errors.append(f"missing payload key: {action.action}")
        if action.nlq_period_policy not in {"list_detail", "explicit_only"}:
            errors.append(f"unsupported NLQ period policy: {action.action}")
    return errors
