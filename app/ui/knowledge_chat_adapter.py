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


def verified_sims_help_examples(
    intent: SimsHelpIntent, *, allowed_actions: set[str], today: date | None = None,
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
) -> str:
    """Only advertise actions present in the current executable inventory."""
    examples = verified_sims_help_examples(intent, allowed_actions=allowed_actions, today=today)
    titles = {
        "code_role_required": "코드가 가리키는 종류를 지정해 주세요.",
        "action_required": "조회할 업무 동작을 지정해 주세요.",
        "detail_role_required": "상세 조회 대상이 거래처인지 입고명세인지 확인해 주세요.",
        "stock_action_required": "월집계와 제품별 재고현황 중 필요한 조회를 선택해 주세요.",
    }
    if intent.reason not in titles:
        raise ValueError("Unknown SIMS help intent")
    answer = titles[intent.reason]
    if intent.query:
        answer += f"\n\n입력한 요청: {intent.query}"
    if intent.subject_label and intent.subject_value:
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
        answer += f"\n\n{label}: " + " / ".join(example for _, example in examples)
    if allow_knowledge_help:
        answer += "\n\n추가 설명은 'SIMS 질문 예시 알려줘'로 요청할 수 있습니다."
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
