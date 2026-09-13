# app/ui/sims_analysis_profiles.py
# -*- coding: utf-8 -*-
"""Pure helpers for SIMS table-scoped LLM analysis.

This module only shapes LLM context and prompt hints. It never mutates
screen/export DataFrames, Streamlit state, database rows, or chat storage
schema.
"""

from __future__ import annotations

from typing import Any, Iterable
import re

import pandas as pd


_SENSITIVE_EXACT = {
    "비밀번호",
    "주민번호",
    "사업자번호",
    "사업자등록번호",
    "전화번호",
    "휴대전화번호",
    "휴대폰",
    "핸드폰",
    "대표자명",
    "대표자",
    "사용자명",
    "로그인ID",
    "로그인아이디",
    "이메일",
    "이메일주소",
    "상세주소",
    "주소",
    "계좌번호",
}

_SENSITIVE_TOKENS = (
    "password",
    "passwd",
    "pwd",
    "token",
    "api_key",
    "apikey",
    "secret",
    "jumin",
    "resident",
    "businessno",
    "bizno",
    "phone",
    "mobile",
    "tel",
    "fax",
    "email",
    "login",
    "account",
    "bank",
    "비밀번호",
    "주민",
    "사업자",
    "전화",
    "휴대",
    "핸드폰",
    "대표자",
    "사용자",
    "로그인",
    "이메일",
    "상세주소",
    "주소",
    "계좌",
)

_SAFE_REGION_COLUMNS = {
    "시도",
    "시도명",
    "시군구",
    "시군구명",
    "구군",
    "구군명",
    "도로명",
    "법정동",
    "법정동명",
    "법정읍면동",
    "법정읍면동명",
    "지역",
    "지역명",
}

_SENSITIVE_LABEL_RE = re.compile(
    r"(전화번호|휴대전화번호|휴대폰|핸드폰|사업자번호|사업자등록번호|대표자명|대표자|이메일|로그인\s*ID|로그인아이디|사용자명|주소|상세주소|도로명주소|계좌번호|password|token|api[_-]?key|secret)"
    r"\s*[:=]?\s*([^/,\n]+)",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(r"\b(?:0\d{1,2})[-\s]?\d{3,4}[-\s]?\d{4}\b")
_BIZNO_RE = re.compile(r"\b\d{3}[-\s]?\d{2}[-\s]?\d{5}\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SECRET_ASSIGN_RE = re.compile(r"\b(password|passwd|pwd|token|api[_-]?key|secret)\b\s*[:=]\s*[^/,\s]+", re.IGNORECASE)

_FREE_TEXT_CONTEXT_KEYS = {
    "query_summary",
    "source_query",
    "condition",
    "message",
    "summary_md",
    "llm_summary_md",
    "analysis_text",
}

_BUSINESS_IDENTIFIER_KEYS = {
    "제품코드",
    "품목코드",
    "상품코드",
    "product_code",
    "goods_code",
    "item_code",
    "prd_cd",
}

_EXACT_SENSITIVE_KEYS = {
    "전화번호",
    "휴대전화번호",
    "휴대폰",
    "핸드폰",
    "사업자번호",
    "사업자등록번호",
    "대표자명",
    "대표자",
    "이메일",
    "이메일주소",
    "로그인ID",
    "로그인아이디",
    "사용자명",
    "주소",
    "상세주소",
    "도로명주소",
    "계좌번호",
}

_ALLOWED_GENERIC_COLUMNS = {
    "거래처명",
    "거래처종류",
    "거래처종류명",
    "거래처그룹",
    "거래처그룹명",
    "제품명",
    "제품코드",
    "품목명",
    "품목코드",
    "제조사",
    "제조사명",
    "제품그룹",
    "제품그룹명",
    "제품분류",
    "제품분류명",
    "제품구분",
    "제품구분명",
    "영업사원",
    "영업사원명",
}


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).lower()


def _compact_set(values: Iterable[Any]) -> set[str]:
    return {_compact(v) for v in values}


def _is_free_text_context_key(key: Any) -> bool:
    return _compact(key) in _compact_set(_FREE_TEXT_CONTEXT_KEYS)


def _is_business_identifier_key(key: Any) -> bool:
    return _compact(key) in _compact_set(_BUSINESS_IDENTIFIER_KEYS)


def _is_aggregate_key(key: Any) -> bool:
    compact = _compact(key)
    if not compact:
        return False
    return (
        compact.endswith("_count")
        or compact.endswith("count")
        or compact.endswith("\uac74\uc218")
        or compact.endswith("\uc218")
    )


def _is_exact_sensitive_mapping_key(key: Any) -> bool:
    name = str(key or "").strip()
    compact = _compact(name)
    if not compact:
        return False
    if name in _EXACT_SENSITIVE_KEYS or compact in _compact_set(_EXACT_SENSITIVE_KEYS):
        return True
    return any(token in compact for token in ("password", "passwd", "pwd", "token", "apikey", "api_key", "secret"))


def is_sensitive_llm_column(column: Any, *, profile_id: str = "") -> bool:
    """Return True when a row-level sample column should be excluded from LLM."""
    name = str(column or "").strip()
    if not name:
        return False
    compact = _compact(name)
    profile = str(profile_id or "").strip()

    if profile == "road_address" and name in _SAFE_REGION_COLUMNS:
        return False
    if name in _ALLOWED_GENERIC_COLUMNS:
        return False

    if name in _SENSITIVE_EXACT:
        return True
    return any(token in compact for token in _SENSITIVE_TOKENS)


def snapshot_grade_analysis_contract(action: str = "", columns: Iterable[Any] = ()) -> str:
    """Shared interpretation contract for product rows and their grouped results."""
    labels = {_clean_text(col) for col in columns}
    relevant = {"출고빈도등급", "손익등급", "기여도등급", "추정단위손익", "추정기여금액"}
    if action != "제품정보 조회" and not labels.intersection(relevant) and not any(label in action for label in relevant):
        return ""
    return (
        "\n[Snapshot 제품통계 해석 계약]\n"
        "- 출고빈도 A~E는 정상 출고 발생횟수 기준 상대등급이며 A에서 E로 갈수록 낮다. "
        "E는 낮은 출고빈도이며 높은 빈도가 아니다. 단순 제품수 비중으로 빈도 수준을 뒤집지 마라.\n"
        "- F는 선택 재고범위의 최초 정상 입고월 M0~M2 신규품목(3개월 이내 최초 입고 품목), "
        "M3부터 A~E/X 평가. X는 관찰기간 이후 최근 완료 3개월 정상 출고 없음. "
        "자료 부족은 X/E나 손실을 뜻하지 않는다.\n"
        "- 손익등급과 기여도등급은 서로 독립적인 대상제품 내 상대등급 A~E이며 A가 상위, E가 하위다. "
        "출고빈도등급과도 독립이며 등급만으로 확정 흑자/적자를 판단하지 마라.\n"
        "- 집계표에서 그룹 컬럼은 분류 기준, 수량·추정기여금액 등은 그 그룹의 측정값이다. "
        "손익등급별 추정기여금액 합계가 가장 큰 그룹은 '추정기여금액 합계가 가장 큰 손익등급 그룹'이다. "
        "이를 '최대 기여 등급'으로 재분류하거나 기여도등급으로 바꾸어 표현하지 마라. "
        "그룹 합계의 순위는 개별 제품의 등급 순위와 다르다.\n"
        "- 추정단위손익=평균매출단가-평균매입단가, 추정손익률=추정단위손익/평균매출단가, "
        "추정기여금액=추정단위손익×3개월출고수량. 모두 관리용 추정치이며 실제 매출액이나 회계 확정손익이 아니다.\n"
        "- LLM 분석 버튼은 클릭한 표의 원자료/집계 결과만 분석한다. 집계표를 제품 원본으로 간주하거나 "
        "없는 매출·마진 지표를 제안하지 마라.\n"
    )


def contains_sensitive_llm_text(value: Any) -> bool:
    """Return True when a free-text condition appears to contain sensitive values."""
    text = _clean_text(value)
    if not text:
        return False
    return bool(
        _SENSITIVE_LABEL_RE.search(text)
        or _PHONE_RE.search(text)
        or _BIZNO_RE.search(text)
        or _EMAIL_RE.search(text)
        or _SECRET_ASSIGN_RE.search(text)
    )


def sanitize_llm_text(value: Any, *, label: Any = "") -> str:
    """Redact sensitive values from free-text copied into LLM context."""
    text = _clean_text(value)
    if not text:
        return ""

    def _label_repl(match: re.Match[str]) -> str:
        name = _clean_text(match.group(1))
        return f"{name} 조건 적용"

    out = _SENSITIVE_LABEL_RE.sub(_label_repl, text)
    out = _SECRET_ASSIGN_RE.sub(lambda m: f"{m.group(1)} 조건 적용", out)
    out = _PHONE_RE.sub("전화번호 조건 적용", out)
    out = _BIZNO_RE.sub("사업자번호 조건 적용", out)
    out = _EMAIL_RE.sub("이메일 조건 적용", out)

    if label and is_sensitive_llm_column(label):
        return f"{_clean_text(label)} 조건 적용"
    return re.sub(r"\s+", " ", out).strip()


def sanitize_llm_mapping(value: Any, *, profile: dict[str, Any] | None = None, key: Any = "") -> Any:
    """Return a JSON-safe copy for LLM context with key-aware sensitive values removed."""
    if isinstance(value, dict):
        return {
            str(k): sanitize_llm_mapping(v, profile=profile, key=k)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [sanitize_llm_mapping(v, profile=profile, key=key) for v in value]

    if _is_business_identifier_key(key):
        return value
    if _is_aggregate_key(key) and not _is_exact_sensitive_mapping_key(key):
        return value
    if _is_exact_sensitive_mapping_key(key):
        if value is None or _clean_text(value) == "":
            return ""
        return f"{_clean_text(key)} \uc870\uac74 \uc801\uc6a9"
    if isinstance(value, str) and _is_free_text_context_key(key):
        return sanitize_llm_text(value, label=key)
    return value


def sanitize_sims_llm_dataframe(df: pd.DataFrame, profile: dict[str, Any] | None = None) -> pd.DataFrame:
    """Return a copy with sensitive row-level sample columns removed."""
    if not isinstance(df, pd.DataFrame):
        return df
    profile_id = str((profile or {}).get("profile_id") or "")
    drop_cols = [c for c in df.columns if is_sensitive_llm_column(c, profile_id=profile_id)]
    if not drop_cols:
        return df.copy()
    return df.drop(columns=drop_cols, errors="ignore").copy()


def _action_contains(action: str, *needles: str) -> bool:
    return any(n and n in action for n in needles)


def _current_followup_profile(action: str) -> tuple[str, str, list[str]]:
    compact = _compact(action)
    if "top" in compact or "상위" in action:
        return (
            "current_table_top",
            "선택 차원과 기준 지표의 상위 그룹 비교",
            ["상위 그룹", "기준 지표", "집중도", "비정상적으로 큰 값"],
        )
    if "집계" in action or "요약표" in action:
        return (
            "current_table_group",
            "선택 차원별 건수·수량·금액 집계 비교",
            ["차원별 건수", "수량·금액 합계", "집중도", "미지정 그룹"],
        )
    return (
        "current_table_filter",
        "사용자가 지정한 조건으로 제한된 결과의 특징 확인",
        ["필터 조건", "잔여 결과 규모", "주요 수치", "확인할 점"],
    )


def build_sims_analysis_profile(
    action: str,
    *,
    params: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
    columns: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Classify a SIMS result into a compact LLM analysis profile."""
    action_name = _clean_text(action)
    meta = dict(meta or {})
    analysis_type = _clean_text(meta.get("analysis_type"))
    profile_id = "generic"
    purpose = "현재 조회 조건과 결과 컬럼을 기준으로 주요 분포·수치·결측을 요약"
    focus = ["주요 분포", "중요 수치", "결측 또는 이상값", "확인할 점"]

    if bool(meta.get("current_table_followup")) or action_name.startswith("현재표"):
        profile_id, purpose, focus = _current_followup_profile(action_name)
    elif action_name == "제품정보 조회":
        profile_id = "snapshot_product_information"
        purpose = "승인된 제품 통계의 출고빈도·수명주기·관리용 추정손익·추정기여 확인 (실제 매출 분석 아님)"
        focus = ["전체 제품수와 등급 분포", "신규품목 F와 무출고 X 구분", "가격자료의 기준월과 자료 부족", "추정단위손익·추정손익률", "추정기여금액 (매출액 아님)", "샘플을 전체로 일반화 금지"]
    elif _action_contains(action_name, "제품수불현황", "제품수불") or analysis_type == "product_flow":
        profile_id = "product_flow"
        purpose = "제품별 입고·출고·재고 변동과 수불금액 확인"
        focus = ["입고수량", "출고수량", "재고수량", "수불금액", "거래처·제품·영업사원별 편중", "기간 내 변동", "결측 또는 비정상적으로 큰 값"]
    elif (
        _action_contains(action_name, "재고부족", "제품재고", "재고현황")
        or analysis_type in {"stock_shortage", "supplier_stock_shortage", "product_inventory"}
    ):
        profile_id = "stock_risk"
        purpose = "현재 재고, 부족 예상, 배정 부족과 공급 위험 확인"
        focus = ["현재고", "부족예상수량", "부족예상금액", "공급사 또는 제품 집중", "긴급 확인 대상", "데이터에 없는 납기나 원인 단정 금지"]
    elif _action_contains(action_name, "입고명세", "출고명세", "거래명세", "세금계산서", "매출", "매입"):
        profile_id = "trade_document"
        purpose = "기간·거래처·제품별 거래금액과 수량 흐름 확인"
        focus = ["금액과 수량", "기간 변화", "거래처 또는 제품 집중도", "매입/매출 구분", "불일치 컬럼이 있을 때만 언급"]
    elif (
        _action_contains(action_name, "거래처 목록", "제품코드", "제품 목록", "사용자 목록", "업무코드")
        or analysis_type in {"vendor_master", "goods_master", "users_master", "codes_master"}
    ):
        profile_id = "master"
        purpose = "등록 현황, 분류, 상태 및 필수정보 완전성 확인"
        focus = ["분류", "등록·사용 상태", "중복 또는 결측 가능성", "지역·제조사·유형 분포", "개인정보 원문 반복 금지"]
    elif _action_contains(action_name, "도로명주소") or analysis_type == "road_address_master":
        profile_id = "road_address"
        purpose = "검색 조건에 맞는 도로명·지역 주소 후보 확인"
        focus = ["시도·시군구·도로명 범위", "후보 건수", "검색 조건과 결과의 적합성", "상세주소·연락처·사업자번호 분석 금지"]

    return {
        "profile_id": profile_id,
        "screen_purpose": purpose,
        "analysis_focus": focus,
        "sensitive_columns": sorted(str(c) for c in (columns or []) if is_sensitive_llm_column(c, profile_id=profile_id)),
        "response_mode": "derived_table" if profile_id.startswith("current_table_") else "source_table",
    }


def _safe_condition_piece(key: Any, value: Any, *, profile_id: str = "") -> str:
    label = _clean_text(key)
    if not label or value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        values = [_clean_text(v) for v in value if _clean_text(v)]
        raw = ", ".join(values[:3])
    else:
        raw = _clean_text(value)
    if not raw:
        return ""
    if is_sensitive_llm_column(label, profile_id=profile_id):
        return f"{label} 조건"
    if _is_business_identifier_key(label) or _is_aggregate_key(label):
        if len(raw) > 80:
            raw = raw[:80].rstrip() + "..."
        return f"{label} {raw}"
    raw = sanitize_llm_text(raw, label=label)
    if len(raw) > 80:
        raw = raw[:80].rstrip() + "..."
    return f"{label} {raw}"


def build_query_scope_summary(
    *,
    params: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
) -> str:
    """Build a deterministic, sanitized scope summary from existing metadata."""
    params = dict(params or {})
    meta = dict(meta or {})
    profile_id = str((profile or {}).get("profile_id") or "")

    for key in ("query_summary", "source_query", "condition"):
        raw = _clean_text(meta.get(key))
        if raw and len(raw) <= 220:
            sanitized = sanitize_llm_text(raw, label=key)
            if sanitized:
                return sanitized

    pieces: list[str] = []
    for start_key, end_key in (("start_date", "end_date"), ("from_date", "to_date"), ("시작일", "종료일")):
        start = _clean_text(params.get(start_key) or meta.get(start_key))
        end = _clean_text(params.get(end_key) or meta.get(end_key))
        if start or end:
            pieces.append(f"기간 {start or '?'} ~ {end or '?'}")
            break

    allowed_keys = (
        "거래처",
        "거래처명",
        "제품",
        "제품명",
        "제품코드",
        "제조사",
        "재고위치",
        "분류",
        "상태",
        "시도",
        "시군구",
        "도로명",
        "keyword",
        "search",
    )
    combined = {**params, **meta}
    for key, value in combined.items():
        label = _clean_text(key)
        compact_label = _compact(label)
        if not label or label in {"table_key", "source_table_key", "action", "analysis_text"}:
            continue
        if not any(_compact(k) in compact_label for k in allowed_keys):
            continue
        piece = _safe_condition_piece(label, value, profile_id=profile_id)
        if piece and piece not in pieces:
            pieces.append(piece)
        if len(pieces) >= 5:
            break

    if pieces:
        return " / ".join(pieces)
    return "별도 제한 조건이 확인되지 않아 전체 조회 결과를 기준으로 분석합니다."


def wants_summary_only(user_text: str) -> bool:
    compact = _compact(user_text)
    if any(token in compact for token in ("의견", "문제점", "주의사항", "확인할점", "이상항목", "분석")):
        return False
    return any(token in compact for token in ("요약", "정리"))


def wants_opinion(user_text: str) -> bool:
    compact = _compact(user_text)
    return any(token in compact for token in ("의견", "문제점", "주의사항", "확인할점", "이상항목", "분석"))


def build_response_format_instruction(user_text: str, *, default_include_opinion: bool = True) -> str:
    """Return a compact Korean response-shape instruction for SIMS analysis."""
    summary_only = wants_summary_only(user_text)
    include_opinion = (not summary_only) and (default_include_opinion or wants_opinion(user_text))
    if summary_only:
        sections = "조회 이해, 핵심 요약 두 부분만 사용하고 LLM 의견은 강제하지 마세요."
    elif include_opinion:
        sections = "조회 이해, 핵심 요약, 주요 특징·확인할 점, LLM 의견 네 부분으로 답하세요."
    else:
        sections = "조회 이해, 핵심 요약, 주요 특징·확인할 점 중심으로 답하세요."

    return (
        f"{sections} 전체는 약 8~12줄로 짧게 유지하고, 중요한 수치 3~5개만 사용하세요. "
        "이미 제공된 표·집계를 길게 반복하지 말고 핵심 차이만 설명하세요. "
        "LLM 의견은 일반적으로 1~2문장으로 쓰되, 데이터와 확인 근거가 충분하고 주요 특징이 여러 개이면 2~3문장까지 허용하세요. "
        "근거가 부족하면 길이를 늘리지 말고 추가 확인 필요라고 쓰세요. "
        "화면 목적은 screen_purpose를, 조건 의미는 query_scope_summary를 근거로 쓰며 사용자의 숨은 목적이나 업무 원인을 추측하지 마세요. "
        "sample_records는 예시 행일 뿐 전체로 일반화하지 말고, display_row_count와 row_count가 다르면 화면 일부와 전체 결과를 구분하세요."
    )
