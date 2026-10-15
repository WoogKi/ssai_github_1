"""Thin Knowledge Chat routing helpers.

This module does not call an LLM or access Streamlit session state.  The main
chat owns lifecycle and persistence; the adapter only recognizes an authorized
request shape and builds a bounded, evidence-only prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import re
from typing import Mapping

from app.services.knowledge_document_service import ContextPacket


_DOCUMENT_PREFIX = "/knowledge"
_TECHNICAL_PREFIX = "/knowledge-tech"
_BUSINESS_HELP_RETRIEVAL_CONTEXT = "업무질문 도움말"

_BUSINESS_HELP_TOPICS = (
    "ssai",
    "sims",
    "업무",
    "질문",
    "계약단가",
    "최종매입단가",
    "발주",
    "입고예정",
    "단가적용처",
    "재고적용처",
    "전문약",
    "일반약",
    "etc",
    "otc",
)
_BUSINESS_HELP_CUES = (
    "어떻게물어",
    "어떻게질문",
    "어떻게조회",
    "어떻게사용",
    "물어보면",
    "질문예시",
    "조회예시",
    "조회방법",
    "사용방법",
    "어떤질문",
    "어떤업무",
    "조회할수있",
    "조회하는방법",
    "조회하려면",
    "뭘물어볼수있",
    "관련프롬프트",
    "프롬프트알려",
    "사용법",
)
_BUSINESS_HELP_TOPIC_OPTIONAL_CUES = (
    "질문예시",
    "발주계산식",
)


@dataclass(frozen=True)
class KnowledgeChatRoute:
    query: str
    technical_detail_mode: bool
    retrieval_query: str = ""


@dataclass(frozen=True)
class KnowledgeFollowupRoute:
    query: str
    parent_message_id: str
    room_id: str


@dataclass(frozen=True)
class SimsHelpIntent:
    reason: str
    query: str = ""
    subject_label: str = ""
    subject_value: str = ""
    period: str = ""
    period_fields: tuple[tuple[str, str], ...] = ()


_SIMS_QUERY_EXAMPLE_CATALOG: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("일일점검", (
        ("SIMS 일일점검", "SIMS 일일점검"),
        ("SIMS 일일점검", "SIMS 일일점검 제약사 삼진"),
        ("SIMS 일일점검", "SIMS 일일점검 발주담당자 김"),
    )),
    ("재고 / 제품", (
        ("현재고 조회", "현재고 제조사명 한미"),
        ("제품재고현황 조회", "제품재고장 제조사 한미"),
        ("제품수불현황 조회", "제품수불현황 제품 00269 2024~2026 조회"),
        ("제품정보 조회", "제품정보 조회"),
    )),
    ("입고 / 출고 / 거래명세", (
        ("입고명세 조회", "입고명세조회 제조사 동제"),
        ("출고명세 조회", "출고명세조회 제조사 동제"),
        ("거래명세서 공통 조회", "거래명세서 공통 20260917 조회"),
        ("입고예정조회", "입고 예정 조회"),
    )),
    ("매출 / 분석", (
        ("품목별 매출 추세 분석", "품목별 매출추세분석 조회"),
        ("품목별 매출 예상", "품목별 매출예상"),
        ("지역별 매출 예상", "지역별 매출예상 영업사원 민우"),
        ("품목별 재고부족현황", "품목별 재고부족현황 제품 아모크라"),
    )),
    ("마스터 조회", (
        ("제품코드 목록", "제품코드조회"),
        ("거래처 목록", "거래처코드조회"),
        ("사용자목록 + 부서명", "사용자 김 조회"),
        ("그룹코드조회", "업무코드 한글명 배송 조회"),
    )),
    ("계약단가 / 매입단가", (
        ("최종 계약단가 조회", "일반의약품 계약단가 조회"),
        ("최종 계약단가 조회", "전문약 제약사 삼진 계약단가 조회"),
        ("최종 매입단가 조회", "전문약 최종 매입단가 조회"),
        ("최종 매입단가 조회", "비보험약 최종 매입단가 조회"),
    )),
    ("발주", (
        ("발주조회", "발주 조회"),
        ("발주 계산", "발주계산"),
        ("발주 계산", "발주담당자 김 발주 계산 적정재고 30일로 해줘"),
    )),
    ("현재표 후속 질문", (
        ("제품수불현황 조회", "현재표 월별 집계"),
        ("제품수불현황 조회", "현재표 일별 집계"),
        ("제품수불현황 조회", "현재표 요일별 집계"),
        ("현재고 조회", "현재표 제품별 재고수량 TOP 20"),
    )),
)


def parse_sims_query_examples_request(value: object) -> bool:
    """Recognize SIMS-scoped example help before invocation stripping and RAG."""
    if not isinstance(value, str):
        return False
    from app.sims.nlq.nlq_router import _normalize_sims_invocation

    compact = re.sub(r"\s+", "", _normalize_sims_invocation(value)).casefold()
    return bool(re.fullmatch(
        r"sims(?:(?:조회|사용)(?:예시|사례)|사용법)(?:알려줘|보여줘|알려주세요|보여주세요)?[?？]?",
        compact,
    ))


def sims_query_examples_text(*, allowed_actions: set[str]) -> str:
    """Render one role-filtered, DB-free catalog of currently registered actions."""
    from app.sims.nlq.action_inventory import implemented_actions

    supported = {spec.canonical_action for spec in implemented_actions()}
    visible = supported & allowed_actions
    sections = ["SIMS에서 아래처럼 질문할 수 있습니다."]
    for heading, entries in _SIMS_QUERY_EXAMPLE_CATALOG:
        examples = [example for action, example in entries if action in visible]
        if examples:
            lead = "조회 결과가 나온 뒤에는:\n" if heading == "현재표 후속 질문" else ""
            sections.append(f"[{heading}]\n{lead}" + "\n".join(f"- {example}" for example in examples))
    sections.append("원하는 업무를 말씀하시면 그 업무에 맞는 질문 예시를 더 알려드릴 수 있습니다.")
    return "\n\n".join(sections)


def parse_incomplete_sims_help_request(value: object, *, today: date | None = None) -> SimsHelpIntent | None:
    """Recognize incomplete business shapes without claiming an executable action."""
    if not isinstance(value, str):
        return None
    query = re.sub(r"\s+", " ", value.strip())
    compact = re.sub(r"\s+", "", query).casefold()
    # These shapes otherwise lose an explicit condition or execute a different
    # registered action. Resolve them before current-table and ERP routing.
    if re.search(r"(?i)^(?:sims\s+)?일일점(?!검)[^\s]*", query):
        return SimsHelpIntent("action_confirmation", query)
    if "현재고금액" in compact and "매출금액" in compact:
        return SimsHelpIntent("single_action_required", query)
    from app.sims.nlq.nlq_router import (
        _unregistered_grouped_sales_budget, _unregistered_grouped_sales_status,
    )

    grouped_budget = _unregistered_grouped_sales_budget(query)
    if grouped_budget:
        return SimsHelpIntent("unregistered_grouped_sales_budget", query, grouped_budget[1])
    grouped_status = _unregistered_grouped_sales_status(query)
    if grouped_status:
        return SimsHelpIntent("unregistered_grouped_sales_status", query, grouped_status[1])
    # A requested but unrecognized table/document is not a reference to the
    # previous result. Keep its wording; do not infer a business from history.
    if (re.search(r"(?:^|\s)[가-힣A-Za-z0-9]+표(?:[은는을를도가만]|\s|$)", query)
            and re.search(r"조회|검색|찾아|보여|알려", query)
            and not re.search(r"현재\s*표|이\s*표|위\s*표|방금\s*표|조회\s*결과", query)):
        from app.sims.nlq.nlq_router import (
            _resolve_analytics_registered_phrase_action,
            resolve_new_sims_nlq_candidate,
        )

        candidate = resolve_new_sims_nlq_candidate(query)
        candidate_action = str((candidate or {}).get("action") or "")
        action_label = re.sub(r"(?:조회|분석)$", "", candidate_action)
        label_matches = bool(action_label and re.sub(r"\s+", "", action_label) in compact)
        registered_alias = bool(
            (candidate or {}).get("route") == "analytics"
            and _resolve_analytics_registered_phrase_action(query) == candidate_action
        )
        if not label_matches and not registered_alias:
            return SimsHelpIntent("document_type_confirmation", query)
    if "발주처기준" in compact and "현재고금액" in compact:
        return SimsHelpIntent("condition_confirmation", query)
    if (re.search(r"발주\s*조회", query)
            and re.search(r"\b(?:\d{1,2}/\d{1,2}|\d{1,2}월\s*\d{1,2}일)\b", query)
            and re.search(r"발주\s*조회\s+[가-힣]{2,}(?:\s|$)", query)):
        return SimsHelpIntent("condition_confirmation", query)
    from app.sims.nlq.nlq_router import (
        _dashboard_unrecognized_staff_label, _extract_dashboard_nlq_conditions,
        _resolve_dashboard_nlq_action,
    )

    incomplete_staff = _dashboard_unrecognized_staff_label(query)
    if incomplete_staff:
        return SimsHelpIntent("dashboard_condition_confirmation", query, incomplete_staff[1], incomplete_staff[0])

    reversed_dashboard = re.fullmatch(r"(?i)(?:sims\s+)?(.+?)\s+일일\s*점검", query)
    if reversed_dashboard and not _resolve_dashboard_nlq_action(query):
        conditions, residual = _extract_dashboard_nlq_conditions(reversed_dashboard.group(1))
        if not residual and len(conditions) == 1:
            role, name = next(iter(conditions.items()))
            if role in {"발주담당자", "제약담당자"} and name:
                return SimsHelpIntent("dashboard_action_order", query, role, name)
    from app.services.erp_table_nlq import is_order_calculation_request, resolve_registered_erp_table_nlq

    if is_order_calculation_request(query):
        parsed = resolve_registered_erp_table_nlq(query, today=today)
        unconsumed = (parsed or {}).get("params", {}).get("_unconsumed_order_condition")
        if unconsumed:
            if unconsumed in {"발주담당자", "제약담당자"}:
                return SimsHelpIntent("condition_confirmation", query, unconsumed, "")
            from app.services.order_stock_location_scope import extract_order_stock_terms

            stock_terms = extract_order_stock_terms(f"재고위치 {unconsumed}")
            stock_like = bool(stock_terms and (stock_terms[0] or stock_terms[1]) and (
                re.search(r"창고|스토어|재고위치", unconsumed) or stock_terms[0]
            ))
            return SimsHelpIntent(
                "condition_confirmation", query,
                "재고위치" if stock_like else "", str(unconsumed) if stock_like else "",
            )
    from app.services.io_nlq import extract_nlq_natural_period, strip_nlq_period_tokens_for_entity_residual

    period_params = extract_nlq_natural_period(query, today=today)
    period_fields = tuple((key, period_params[key]) for key in (
        "date_from", "date_to", "month_from", "month_to",
    ) if period_params.get(key))
    period = (period_params.get("date_from") or period_params.get("month_from") or "")
    period_to = period_params.get("date_to") or period_params.get("month_to") or ""
    if period_to and period_to != period:
        period += f"~{period_to}"
    if re.fullmatch(r"\d{5}", compact):
        return SimsHelpIntent("code_role_required", query, subject_value=compact)
    match = re.fullmatch(r"(?:sims|ssai)\s+(발주담당자|거래처|제약사|제품)\s+(.+)", query, re.I)
    if match:
        # The existing master/IO intent readers are pure. An executable
        # request must continue through its normal authorization and source.
        from app.services.io_nlq import resolve_io_nlq
        from app.sims.nlq.nlq_router import _should_try_goods_before_users, _should_try_vendors_before_goods

        if (resolve_io_nlq(query) is not None or _should_try_goods_before_users(query)
                or _should_try_vendors_before_goods(query)):
            return None
        subject = strip_nlq_period_tokens_for_entity_residual(match.group(2)).strip(" ,:/-~")
        return SimsHelpIntent("action_required", query, match.group(1), subject, period, period_fields)
    match = re.fullmatch(r"(매입처|발주처)\s+(.+)\s+상세", query)
    if match:
        subject = strip_nlq_period_tokens_for_entity_residual(match.group(2)).strip(" ,:/-~")
        return SimsHelpIntent("detail_role_required", query, match.group(1), subject, period, period_fields)
    match = re.fullmatch(r"(.+)\s+(?:월별|일별)\s*재고\s*현황", query)
    if match:
        subject = strip_nlq_period_tokens_for_entity_residual(match.group(1)).strip(" ,:/-~")
        return SimsHelpIntent("stock_action_required", query, subject_value=subject, period=period,
                              period_fields=period_fields)
    return None


def cached_company_stock_location_names(session_state: Mapping[str, object], company_id: object) -> dict[str, str]:
    """Use only already-loaded choices for this company; help must not query ERP."""
    from app.sims.views.dashboard_lite import DASHBOARD_LITE_OPTION_CACHE_KEY, DASHBOARD_LITE_OPTION_CACHE_VERSION

    company = str(company_id or "").strip()
    if not company or str(session_state.get("__dashboard_lite_profile_loaded_for") or "") != company:
        return {}
    options = session_state.get(DASHBOARD_LITE_OPTION_CACHE_KEY)
    if not isinstance(options, dict) or options.get("cache_version") != DASHBOARD_LITE_OPTION_CACHE_VERSION:
        return {}
    return {
        str(code): str(name) for code, name in dict(options.get("stock_code_to_name") or {}).items()
        if str(code).strip() and str(name).strip()
    }


def verified_sims_help_examples(
    intent: SimsHelpIntent, *, allowed_actions: set[str], today: date | None = None,
    stock_location_names: Mapping[str, str] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Build examples from the user's role, then verify action and condition parsing."""
    from app.services.io_nlq import resolve_io_nlq
    from app.sims.nlq.action_inventory import implemented_actions

    supported = {spec.canonical_action for spec in implemented_actions()}
    role = intent.subject_label
    value = intent.subject_value
    period = dict(intent.period_fields)
    period_phrase = intent.period
    period_actions = {"입고명세 조회", "발주조회", "실재고월집계 조회"}
    candidates: list[tuple[str, str, str, str]] = []
    if intent.reason == "code_role_required":
        return ()
    elif intent.reason == "grouping_scope_required":
        examples = (
            ("제조사", "maker_nm"),
            ("발주처", "order_nm"),
        )
        if role == "발주처":
            examples = tuple(reversed(examples))
        candidates.extend(
            ("제품재고현황 조회", f"제품재고장 {label}명 {value}", key, value)
            for label, key in examples
        )
    elif intent.reason == "action_required":
        if role == "발주담당자":
            candidates.extend((
                ("발주 계산", f"발주담당자 {value} 발주계산", "order_staff_nm", value),
                ("발주조회", f"발주담당자 {value} 발주조회", "order_staff_nm", value),
            ))
        elif role == "제약사":
            candidates.append(("제품정보 조회", f"제약사 {value} 제품정보 조회", "maker_nm", value))
        elif role == "거래처":
            candidates.append(("입고명세 조회", f"거래처 {value} 입고명세 조회", "ven_nm", value))
        elif role == "제품":
            candidates.append(("제품정보 조회", f"제품명 {value} 제품정보 조회", "physic_nm", value))
    elif intent.reason == "detail_role_required":
        candidates.append(("입고명세 조회", f"{role}명 {value} 입고명세 조회", "ven_nm", value))
    elif intent.reason == "stock_action_required":
        candidates.append(("실재고월집계 조회", "실재고월집계 조회" if period else "실재고월집계 조회 이달", "month_from", ""))
    elif intent.reason == "dashboard_condition_confirmation":
        from app.sims.nlq.nlq_router import _extract_dashboard_nlq_conditions, resolve_new_sims_nlq_candidate

        action = "SIMS 일일점검"
        if action not in supported or action not in allowed_actions:
            return ()
        short_label, role = value, intent.subject_label
        pattern = rf"(?<![가-힣A-Za-z0-9]){re.escape(short_label)}(?!자)(?=\s+\S)"
        example = re.sub(pattern, role, intent.query, count=1)
        if example == intent.query:
            return ()
        candidate = resolve_new_sims_nlq_candidate(example)
        original_conditions, _ = _extract_dashboard_nlq_conditions(intent.query)
        conditions, residual = _extract_dashboard_nlq_conditions(example)
        if (candidate or {}).get("action") != action or residual or not conditions.get(role):
            return ()
        if any(conditions.get(label) != condition for label, condition in original_conditions.items() if label != role):
            return ()
        return ((action, example),)
    elif intent.reason == "dashboard_action_order":
        from app.sims.nlq.nlq_router import _extract_dashboard_nlq_conditions, resolve_new_sims_nlq_candidate

        action = "SIMS 일일점검"
        example = f"SIMS 일일점검 {role} {value}"
        if action not in supported or action not in allowed_actions:
            return ()
        candidate = resolve_new_sims_nlq_candidate(example)
        conditions, residual = _extract_dashboard_nlq_conditions(example)
        return ((action, example),) if (
            candidate and candidate.get("action") == action and conditions.get(role) == value and not residual
        ) else ()
    elif intent.reason in {"unregistered_grouped_sales_status", "unregistered_grouped_sales_budget"}:
        from app.sims.nlq.action_inventory import ANALYTICS_INTENT_ACTIONS
        from app.sims.nlq.nlq_router import _resolve_analytics_action, resolve_new_sims_nlq_candidate

        label = intent.subject_label
        grouping = next(
            (axis for (metric, axis), action in ANALYTICS_INTENT_ACTIONS.items()
             if metric == "sales_forecast" and action.startswith(f"{label}별")),
            "",
        )
        action = ANALYTICS_INTENT_ACTIONS.get(("sales_forecast", grouping))
        original = re.sub(r"\s+", "", intent.query)
        if not action or action not in supported or action not in allowed_actions:
            return ()
        requested_term = "매출예산" if intent.reason == "unregistered_grouped_sales_budget" else "매출현황"
        if original not in {f"{label}별{requested_term}", f"{label}별{requested_term}조회"}:
            return ()
        example = action
        candidate = resolve_new_sims_nlq_candidate(example)
        if _resolve_analytics_action(example) != action or (candidate or {}).get("action") != action:
            return ()
        return ((action, example),)
    elif intent.reason == "condition_confirmation" and role == "재고위치":
        from app.services.erp_table_nlq import resolve_registered_erp_table_nlq
        from app.services.order_stock_location_scope import extract_order_stock_terms
        from app.services.io_nlq import resolve_stock_location_codes

        action = "발주 계산"
        names_by_code = dict(stock_location_names or {})
        if action not in supported or action not in allowed_actions:
            return ()
        terms = extract_order_stock_terms(f"재고위치 {value}")
        if not terms or not (terms[0] or terms[1]):
            return ()
        if names_by_code:
            selected, error, _ = resolve_stock_location_codes(
                *terms, allowed_codes=list(names_by_code), location_names=names_by_code,
            )
            if error or not selected:
                return ()
        offset = intent.query.rfind(value)
        if offset < 0:
            return ()
        example = intent.query[:offset] + "재고위치 " + intent.query[offset:]
        original = resolve_registered_erp_table_nlq(intent.query, today=today)
        reparsed = resolve_registered_erp_table_nlq(example, today=today)
        if (original or {}).get("action") != action or (reparsed or {}).get("action") != action:
            return ()
        original_params = dict(original.get("params") or {})
        reparsed_params = dict(reparsed.get("params") or {})
        if reparsed_params.get("_unconsumed_order_condition"):
            return ()
        if reparsed_params.get("stock_cd_list") != terms[0] or reparsed_params.get("stock_nm_list") != terms[1]:
            return ()
        excluded = {"stock_cd_list", "stock_nm_list", "stock_cds", "stock_cd", "stock_nm", "stock_names"}
        def retained(params: dict) -> dict:
            return {key: item for key, item in params.items() if key not in excluded and not key.startswith("_")}
        return ((action, example),) if retained(original_params) == retained(reparsed_params) else ()
    elif intent.reason in {"action_confirmation", "condition_confirmation", "single_action_required",
                           "document_type_confirmation"}:
        return ()
    else:
        raise ValueError("Unknown SIMS help intent")

    verified = []
    for action, example, key, expected in candidates:
        if action not in supported or action not in allowed_actions:
            continue
        if period:
            if action not in period_actions:
                if action != "제품정보 조회":
                    continue
            elif action == "실재고월집계 조회" and "date_from" in period:
                continue
            else:
                example = f"{example} {period_phrase}"
        parsed = resolve_io_nlq(example, today=today)
        params = (parsed or {}).get("params") or {}
        condition_matches = str(params.get(key) or "") == expected if expected else bool(params.get(key))
        from app.services.io_nlq import extract_nlq_natural_period
        reparsed_period = extract_nlq_natural_period(example, today=today)
        period_matches = action not in period_actions or all(
            reparsed_period.get(name) == bound and params.get(name) == bound
            for name, bound in period.items()
        )
        if (parsed or {}).get("action") == action and condition_matches and period_matches:
            verified.append((action, example))
    return tuple(verified)


def verified_sims_help_text(
    intent: SimsHelpIntent, *, allowed_actions: set[str], allow_knowledge_help: bool = False,
    selected_action: str = "", today: date | None = None,
    stock_location_names: Mapping[str, str] | None = None,
) -> str:
    """Only advertise actions present in the current executable inventory."""
    examples = verified_sims_help_examples(
        intent, allowed_actions=allowed_actions, today=today,
        stock_location_names=stock_location_names,
    )
    titles = {
        "code_role_required": "코드가 가리키는 종류를 지정해 주세요.",
        "action_required": "조회할 업무 동작을 지정해 주세요.",
        "detail_role_required": "상세 조회 대상이 거래처인지 입고명세인지 확인해 주세요.",
        "stock_action_required": "월집계와 제품별 재고현황 중 필요한 조회를 선택해 주세요.",
        "grouping_scope_required": "집계 기준과 조회 대상 중 어떤 조건인지 구분해 주세요.",
        "dashboard_action_order": "일일점검 업무명과 담당자 조건을 구분해 주세요. 아직 조회하지 않았습니다.",
        "dashboard_condition_confirmation": "담당자 조건의 정식 이름을 확인해 주세요. 이번 일일점검은 실행하지 않았습니다.",
        "action_confirmation": "지원되는 업무명을 확인해 주세요. 다른 업무로 조회하지 않았습니다.",
        "document_type_confirmation": "어떤 업무 또는 전표 종류를 뜻하는지 확인해 주세요. 이전 조회 결과를 적용하거나 새로 조회하지 않았습니다.",
        "unregistered_grouped_sales_status": "현재 질문에 해당하는 조회 업무를 확정하지 못했습니다. 이번 조회는 실행하지 않았습니다.",
        "unregistered_grouped_sales_budget": "매출예산은 매출 예상과 다른 업무입니다. 예산 조회는 실행하지 않았습니다.",
        "condition_confirmation": "조회조건의 의미를 확인해 주세요. 조건을 생략한 전체 조회는 실행하지 않았습니다.",
        "single_action_required": "한 번에 한 업무를 질문해 주세요. 두 업무를 하나로 조회하지 않았습니다.",
    }
    if intent.reason not in titles:
        raise ValueError("Unknown SIMS help intent")
    answer = titles[intent.reason]
    if intent.reason == "unregistered_grouped_sales_status":
        answer += "\n\n매출현황과 매출 예상은 서로 다른 개념입니다."
    if intent.reason == "condition_confirmation" and intent.subject_label == "재고위치":
        answer = "발주계산에서 재고위치를 지정하려는 것으로 이해했습니다. 이번 발주계산은 실행하지 않았습니다."
    elif intent.reason == "condition_confirmation" and intent.subject_label in {"발주담당자", "제약담당자"}:
        answer = f"{intent.subject_label} 이름을 지정해 주세요. 조건 없이 전체 발주계산을 실행하지 않았습니다."
    if intent.query:
        answer += f"\n\n입력한 요청: {intent.query}"
    if intent.subject_label and intent.subject_value:
        if intent.reason == "grouping_scope_required":
            answer += f"\n입력한 검색어: {intent.subject_value}"
        elif intent.reason == "condition_confirmation" and intent.subject_label == "재고위치":
            answer += f"\n\n확인하지 못한 조건: {intent.subject_value}"
        elif intent.reason == "dashboard_condition_confirmation":
            answer += f"\n확인하지 못한 조건: {intent.subject_value}"
        else:
            answer += f"\n확인한 대상: {intent.subject_label} {intent.subject_value}"
    elif intent.subject_value and intent.reason == "stock_action_required":
        answer += f"\n입력한 대상 표현: {intent.subject_value} (대상 종류 미확정)"
    elif intent.reason == "code_role_required":
        answer += "\n입력한 코드는 종류가 확인되지 않았습니다."
    if intent.period:
        answer += f"\n입력한 기간: {intent.period}"
        if examples and any(not example.endswith(intent.period) for _, example in examples):
            answer += "\n아래 기간 미포함 예문에는 입력한 기간이 적용되지 않습니다."
    if selected_action and selected_action in {action for action, _ in examples}:
        answer += f"\n승인된 업무 도움말의 관련 동작: {selected_action}"
    if examples:
        label = "일반 사용법 예시" if (intent.reason in {"code_role_required", "stock_action_required"}
                                  or intent.period and any(not example.endswith(intent.period) for _, example in examples)) else "현재 질문을 보완한 예시"
        if intent.reason == "condition_confirmation" and intent.subject_label == "재고위치":
            answer += "\n\n다음과 같이 질문해 주세요."
            answer += "\n\n" + " / ".join(example for _, example in examples)
            if not stock_location_names:
                answer += "\n\n현재 재고위치 등록 여부는 확인하지 않았습니다."
        elif intent.reason == "unregistered_grouped_sales_status":
            answer += "\n\nSIMS AI에서 지원하는 관련 업무: " + ", ".join(action for action, _ in examples)
            answer += "\n매출 예측을 원하시면 다음처럼 질문해 주세요: " + " / ".join(example for _, example in examples)
        elif intent.reason == "unregistered_grouped_sales_budget":
            answer += "\n\n별도로 지원되는 관련 업무: " + ", ".join(action for action, _ in examples)
            answer += "\n매출 예측을 원하시는 경우에만 다음처럼 질문해 주세요: " + " / ".join(example for _, example in examples)
        else:
            answer += f"\n\n{label}: " + " / ".join(example for _, example in examples)
    elif intent.reason == "condition_confirmation" and intent.subject_label == "재고위치":
        answer += "\n\n재고위치를 뜻하셨다면 등록된 재고위치를 확인해 '재고위치' 조건을 명시해 주세요."
    elif intent.reason == "condition_confirmation" and intent.subject_label in {"발주담당자", "제약담당자"}:
        answer += f"\n\n질문 형식: {intent.subject_label} [이름] 발주계산"
    if allow_knowledge_help:
        answer += "\n\n추가 설명은 'SIMS 질문 예시 알려줘'로 요청할 수 있습니다."
    elif intent.reason in {"action_confirmation", "condition_confirmation", "single_action_required",
                           "document_type_confirmation", "unregistered_grouped_sales_budget"}:
        answer += "\n\n지원되는 질문은 'SIMS 질문 예시 알려줘'에서 확인할 수 있습니다."
    return answer + "\n\n조회는 조건을 선택해 다시 입력한 뒤 기존 권한 검사를 거쳐 실행됩니다."


def build_sims_help_choice_prompt(
    *, intent: SimsHelpIntent, examples: tuple[tuple[str, str], ...], packet: ContextPacket,
) -> list[dict[str, str]]:
    """Use approved evidence only to select a verified action, never to generate a query."""
    if packet.reason_code != "ready" or not packet.text or not packet.citations or not examples:
        raise ValueError("Approved help evidence and verified actions are required")
    actions = sorted({action for action, _ in examples})
    return [
        {"role": "system", "content": (
            "승인된 업무 도움말과 허용된 동작만 보고 가장 관련 있는 동작 하나를 고르세요. "
            "확정할 수 없으면 빈 문자열을 고르세요. 설명·예문·SQL은 생성하지 마세요. "
            "반드시 {\"action\": \"허용된 동작 또는 빈 문자열\"} JSON만 출력하세요."
        )},
        {"role": "user", "content": f"질문: {intent.query}\n허용된 동작: {actions}\n승인된 도움말: {packet.text}"},
    ]


def parse_sims_help_choice(value: str, *, examples: tuple[tuple[str, str], ...]) -> str:
    try:
        answer = json.loads(value)
    except (TypeError, ValueError):
        return ""
    if not isinstance(answer, dict) or set(answer) != {"action"}:
        return ""
    selected = answer["action"]
    return selected if isinstance(selected, str) and selected in {action for action, _ in examples} else ""


def build_knowledge_followup_queue_request(
    *,
    query: object,
    parent_message_id: object,
    room_id: object,
) -> dict[str, str]:
    """Build the small JSON-safe request owned by the dedicated UI queue."""
    values = {
        "query": str(query or "").strip(),
        "parent_message_id": str(parent_message_id or "").strip(),
        "room_id": str(room_id or "").strip(),
    }
    if not all(values.values()):
        raise ValueError("Knowledge follow-up queue fields must be non-empty")
    return values


def parse_knowledge_followup_queue_request(
    value: object,
    *,
    current_room_id: object,
    last_user_text: object,
) -> KnowledgeFollowupRoute | None:
    """Accept only the exact room/query tuple produced by the follow-up UI."""
    if not isinstance(value, dict) or set(value) != {"query", "parent_message_id", "room_id"}:
        return None
    try:
        request = build_knowledge_followup_queue_request(**value)
    except (TypeError, ValueError):
        return None
    if request["room_id"] != str(current_room_id or "").strip():
        return None
    if request["query"] != str(last_user_text or "").strip():
        return None
    return KnowledgeFollowupRoute(**request)


def parse_explicit_knowledge_request(value: object) -> KnowledgeChatRoute | None:
    """Route only explicit commands; ordinary Chat/NLQ input remains untouched."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    lowered = text.lower()
    for prefix, technical_detail_mode in (
        (_TECHNICAL_PREFIX, True),
        (_DOCUMENT_PREFIX, False),
    ):
        if not lowered.startswith(prefix):
            continue
        if len(text) == len(prefix) or not text[len(prefix)].isspace():
            return None
        query = text[len(prefix):].strip()
        return KnowledgeChatRoute(query=query, technical_detail_mode=technical_detail_mode) if query else None
    return None


def parse_business_help_knowledge_request(value: object) -> KnowledgeChatRoute | None:
    """Recognize business-question usage intent without consuming data queries."""
    if not isinstance(value, str):
        return None
    query = value.strip()
    if not query or query.startswith("/"):
        return None
    compact = re.sub(r"\s+", "", query).casefold()
    has_topic = any(topic in compact for topic in _BUSINESS_HELP_TOPICS)
    has_help_cue = any(cue in compact for cue in _BUSINESS_HELP_CUES)
    has_topic_optional_cue = any(cue in compact for cue in _BUSINESS_HELP_TOPIC_OPTIONAL_CUES)
    if not ((has_topic and has_help_cue) or has_topic_optional_cue):
        return None
    return KnowledgeChatRoute(
        query=query,
        technical_detail_mode=False,
        retrieval_query=_BUSINESS_HELP_RETRIEVAL_CONTEXT,
    )


def build_knowledge_prompt(*, query: str, packet: ContextPacket) -> list[dict[str, str]]:
    """Keep the model bounded to the already-authorized ContextPacket."""
    if packet.reason_code != "ready" or not packet.text or not packet.citations:
        raise ValueError("Knowledge prompt requires ready evidence")
    return [
        {
            "role": "system",
            "content": (
                "승인된 Knowledge 근거만 사용해 한국어로 간결히 답하세요. "
                "근거에 없는 내용은 추측하지 말고 자료 부족이라고 답하세요. "
                "citation은 시스템이 별도로 표시하므로 본문에 임의 citation을 만들지 마세요."
            ),
        },
        {
            "role": "user",
            "content": f"질문:\n{query}\n\n승인된 Knowledge 근거:\n{packet.text}",
        },
    ]
