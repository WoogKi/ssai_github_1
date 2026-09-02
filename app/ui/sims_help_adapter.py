"""Deterministic, permission-filtered SIMS user help.

This module intentionally has no Streamlit, database, RAG, Web, or LLM dependency.
It recognizes only explicit help phrases and renders user-facing examples from a
small action-backed catalog.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Mapping

from app.services.ssai_permission_policy import get_required_permission


@dataclass(frozen=True)
class SimsHelpRoute:
    intent: str = "sims_user_help"


@dataclass(frozen=True)
class SimsHelpExample:
    help_category: str
    title: str
    example: str
    action: str = ""
    category: str = ""
    required_any_permissions: tuple[str, ...] = ()
    required_all_permissions: tuple[str, ...] = ()


_EXPLICIT_HELP_PHRASES = frozenset(
    {
        "sims관련프롬프트알려줘",
        "sims에서뭘물어볼수있어",
        "sims질문예시알려줘",
        "질문예시알려줘",
        "sims사용법알려줘",
        "sims사용법상세히알려줘",
        "sims사용법자세히알려줘",
    }
)


# Add future user-facing examples here with their existing action/category
# permission mapping. Do not add implementation or database terminology.
SIMS_HELP_EXAMPLES: tuple[SimsHelpExample, ...] = (
    SimsHelpExample("재고", "현재고", "현재고 조회", action="현재고 조회", category="입출고/명세서/재고"),
    SimsHelpExample("입고/출고", "입고", "오늘 입고현황", action="입고명세 조회", category="입출고/명세서/재고"),
    SimsHelpExample("입고/출고", "출고", "오늘 출고현황", action="출고명세 조회", category="입출고/명세서/재고"),
    SimsHelpExample("매출/매입", "매출", "오늘 매출현황", action="출고명세 조회", category="입출고/명세서/재고"),
    SimsHelpExample("매출/매입", "매입", "오늘 매입현황", action="입고명세 조회", category="입출고/명세서/재고"),
    SimsHelpExample("KPI/예측", "매출 추세 요약", "품목별 매출 추세 요약표", action="품목별 매출 추세 요약표", category="분석/KPI"),
    SimsHelpExample("KPI/예측", "품목별 매출 예상", "품목별 매출 예상", action="품목별 매출 예상", category="분석/KPI"),
    SimsHelpExample("KPI/예측", "영업사원별 매출 예상", "영업사원별 매출 예상", action="영업사원별 매출 예상", category="분석/KPI"),
    SimsHelpExample("KPI/예측", "거래처별 매출 예상", "거래처별 매출 예상", action="매출처별 매출 예상", category="분석/KPI"),
    SimsHelpExample("KPI/예측", "품목별 재고부족현황", "품목별 재고부족현황", action="품목별 재고부족현황", category="분석/KPI"),
    SimsHelpExample("KPI/예측", "매입처별 재고부족현황", "매입처별 재고부족 현황", action="매입처별 재고부족 현황", category="분석/KPI"),
    SimsHelpExample(
        "현재표 분석",
        "현재표 분석",
        "현재표 거래처별 매출금액 분석",
        required_any_permissions=("MASTER_READ", "IO_READ", "KPI_READ"),
    ),
    SimsHelpExample("현재표 분석", "현재표 집계", "현재표 <칼럼명>별 집계", required_any_permissions=("MASTER_READ", "IO_READ", "KPI_READ")),
    SimsHelpExample("현재표 분석", "현재표 분석", "현재표 <칼럼명>별 분석", required_any_permissions=("MASTER_READ", "IO_READ", "KPI_READ")),
    SimsHelpExample("현재표 분석", "현재표 요약", "현재표 <칼럼명>별 요약", required_any_permissions=("MASTER_READ", "IO_READ", "KPI_READ")),
    SimsHelpExample("현재표 분석", "현재표 TOP", "현재표 <칼럼명> TOP 10", required_any_permissions=("MASTER_READ", "IO_READ", "KPI_READ")),
    SimsHelpExample("현재표 분석", "현재표 금액 TOP", "현재표에서 금액 TOP 10", required_any_permissions=("MASTER_READ", "IO_READ", "KPI_READ")),
    SimsHelpExample("SIMS 일일점검", "SIMS 일일점검", "SIMS 일일점검", action="SIMS 일일점검", category="분석/KPI"),
)


def _normalize_explicit_help_phrase(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"\s+", "", value.strip().lower())
    return text.rstrip("?!.")


def parse_sims_help_request(value: object) -> SimsHelpRoute | None:
    """Recognize only the explicitly supported user-help phrases."""
    if _normalize_explicit_help_phrase(value) not in _EXPLICIT_HELP_PHRASES:
        return None
    return SimsHelpRoute()


def _example_is_allowed(example: SimsHelpExample, permission_codes: set[str]) -> bool:
    if example.required_all_permissions and not set(example.required_all_permissions).issubset(permission_codes):
        return False
    if example.required_any_permissions:
        return bool(permission_codes.intersection(example.required_any_permissions))
    required = get_required_permission(category=example.category, action=example.action)
    return required is None or required in permission_codes


def _knowledge_help_examples(
    permission_codes: set[str],
    availability: Mapping[str, object] | None,
) -> tuple[SimsHelpExample, ...]:
    if "RAG_USE" not in permission_codes or not isinstance(availability, Mapping):
        return ()
    examples: list[SimsHelpExample] = []
    general_query = str(availability.get("general") or "").strip()
    if general_query:
        examples.append(SimsHelpExample("Knowledge 자료", "승인 자료 질문", f"/knowledge {general_query}"))
    erp_query = str(availability.get("erp_technical") or "").strip()
    if erp_query:
        examples.append(SimsHelpExample("Knowledge 자료", "ERP 기술 상세", f"/knowledge-tech {erp_query} 관련 기술 내용을 알려줘"))
    project_query = str(availability.get("project_source_technical") or "").strip()
    if project_query:
        examples.append(SimsHelpExample("Knowledge 자료", "기술 상세 자료", f"/knowledge-tech {project_query} 관련 기술 내용을 알려줘"))
    return tuple(examples)


def build_sims_help_text(
    permission_codes: Iterable[object],
    *,
    knowledge_availability: Mapping[str, object] | None = None,
) -> str:
    """Build bounded user-facing examples from currently effective permissions."""
    effective_permissions = {
        str(permission_code).strip()
        for permission_code in permission_codes
        if str(permission_code).strip()
    }
    allowed_examples = [
        example
        for example in SIMS_HELP_EXAMPLES
        if _example_is_allowed(example, effective_permissions)
    ]
    allowed_examples.extend(_knowledge_help_examples(effective_permissions, knowledge_availability))
    if not allowed_examples:
        return "현재 권한으로 안내할 수 있는 SIMS 조회 예시가 없습니다. 관리자에게 권한을 문의해 주세요."

    lines = [
        "SIMS에서는 재고, 입고/출고, 매출/매입, KPI/예측과 현재표 분석을 질문할 수 있습니다.",
        "현재 권한으로 사용할 수 있는 예시입니다.",
    ]
    current_category = ""
    for example in allowed_examples:
        if example.help_category != current_category:
            current_category = example.help_category
            lines.append(f"\n**{current_category}**")
        lines.append(f"- {example.title}: `{example.example}`")
    lines.append("조회 결과가 나온 뒤에는 `현재표 ... 분석`처럼 방금 결과를 이어서 분석할 수 있습니다.")
    return "\n".join(lines)
