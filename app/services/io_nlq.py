# app/services/io_nlq.py

from __future__ import annotations

import logging
import re
import time
from datetime import date, timedelta

import pandas as pd

from typing import Any, Dict, Optional

from app.services.rddbc_io_common import clean_text


log = logging.getLogger("ssai")


_IO_PREFIX_WORDS = {
    "정상입고": "0",
    "입고반품": "1",
    "장부입고": "2",
    "미결입고": "3",
    "기타입고": "4",
    "정상출고": "5",
    "출고반품": "6",
    "장부출고": "7",
    "미결출고": "8",
    "기타출고": "9",
}

_PRODUCT_FLOW_WORDS = (
    "제품수불현황",
    "제품수불부",
    "제품수불",
    "수불현황",
    "수불부",
)

_PRODUCT_INVENTORY_WORDS = (
    "제품재고현황",
    "제품재고장",
    "재고현황",
    "재고장",
)

_PRODUCT_IO_WORDS = _PRODUCT_FLOW_WORDS + _PRODUCT_INVENTORY_WORDS

_TRANSACTION_SIGNAL_WORDS = (
    "내역",
    "거래내역",
    "현황",
    "명세",
    "이력",
    "전표",
    "집계",
    "검증",
    "수불",
    "재고",
)

_MASTER_QUERY_WORDS = (
    "조회",
    "목록",
    "마스터",
    "검색",
)

_VENDOR_MASTER_ATTR_WORDS = (
    "거래처명",
    "거래처코드",
    "대표자",
    "대표자명",
    "사업자번호",
    "사업자등록번호",
    "주소",
    "소재지",
    "영업사원",
    "영업사원명",
    "매입처",
    "매출처",
    "제조사",
    "발주처",
    "단가적용처",
    "단가적용처명",
    "재고적용처",
    "재고적용처명",
    "수정자",
    "수정자명",
    "수정일자",
)

_PURCHASE_SIDE_VENDOR_WORDS = (
    "매입처",
    "제조사",
    "발주처",
)

_SALES_SIDE_VENDOR_WORDS = (
    "매출처",
)

def _has_transaction_signal(text: str) -> bool:
    t = _norm(text)
    return any(w in t for w in _TRANSACTION_SIGNAL_WORDS)


def _has_master_query_signal(text: str) -> bool:
    t = _norm(text)
    return any(w in t for w in _MASTER_QUERY_WORDS)


def _looks_vendor_master_query(text: str) -> bool:
    t = _norm(text)
    if "거래처" not in t:
        return False

    # 거래/집계 신호가 같이 있으면 마스터 조회로 보지 않는다.
    if _has_transaction_signal(t):
        return False

    # 거래처 마스터 자체 신호
    if _has_master_query_signal(t):
        return True

    # 거래처 마스터 속성어
    return any(w in t for w in _VENDOR_MASTER_ATTR_WORDS)

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    return any(w in text for w in words)


def _extract_date_range(text: str) -> Dict[str, str]:
    """
    일자 조건 추출.

    지원:
    - 20260419
    - 20260419~20260519
    - 2026-04-19
    - 2026-04-19~2026-05-19
    - 2026.04.19 ~ 2026.05.19
    - 2026/04/19 ~ 2026/05/19
    - 2026년 04월 19일 ~ 2026년 05월 19일
    """
    out: Dict[str, str] = {}
    t = str(text or "")

    def _ymd(y: str, m: str, d: str) -> str:
        return f"{str(y).zfill(4)}{str(m).zfill(2)}{str(d).zfill(2)}"

    # YYYY-MM-DD / YYYY.MM.DD / YYYY/MM/DD 범위
    date_pat = r"((?:19|20)\d{2})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})"
    m = re.search(
        rf"{date_pat}\s*(?:~|부터|에서|to)\s*{date_pat}",
        t,
        flags=re.IGNORECASE,
    )
    if m:
        out["date_from"] = _ymd(m.group(1), m.group(2), m.group(3))
        out["date_to"] = _ymd(m.group(4), m.group(5), m.group(6))
        return out

    # YYYY년 MM월 DD일 범위
    kor_date_pat = r"((?:19|20)\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일?"
    m = re.search(
        rf"{kor_date_pat}\s*(?:~|부터|에서|to)\s*{kor_date_pat}",
        t,
        flags=re.IGNORECASE,
    )
    if m:
        out["date_from"] = _ymd(m.group(1), m.group(2), m.group(3))
        out["date_to"] = _ymd(m.group(4), m.group(5), m.group(6))
        return out

    # YYYYMMDD ~ YYYYMMDD
    pairs = re.findall(r"((?:19|20)\d{6})\s*[~\-]\s*((?:19|20)\d{6})", t)
    if pairs:
        out["date_from"], out["date_to"] = pairs[0]
        return out

    # 단일 formatted date 2개 이상
    formatted = re.findall(date_pat, t)
    if len(formatted) >= 2:
        y1, m1, d1 = formatted[0]
        y2, m2, d2 = formatted[1]
        out["date_from"] = _ymd(y1, m1, d1)
        out["date_to"] = _ymd(y2, m2, d2)
        return out
    elif len(formatted) == 1:
        y, mth, d = formatted[0]
        v = _ymd(y, mth, d)
        out["date_from"] = v
        out["date_to"] = v
        return out

    # 단일 Korean date 2개 이상
    formatted_kr = re.findall(kor_date_pat, t)
    if len(formatted_kr) >= 2:
        y1, m1, d1 = formatted_kr[0]
        y2, m2, d2 = formatted_kr[1]
        out["date_from"] = _ymd(y1, m1, d1)
        out["date_to"] = _ymd(y2, m2, d2)
        return out
    elif len(formatted_kr) == 1:
        y, mth, d = formatted_kr[0]
        v = _ymd(y, mth, d)
        out["date_from"] = v
        out["date_to"] = v
        return out

    # 기존 YYYYMMDD 처리
    singles = re.findall(r"(?:19|20)\d{6}", t)
    if len(singles) >= 2:
        out["date_from"], out["date_to"] = singles[0], singles[1]
    elif len(singles) == 1:
        out["date_from"] = singles[0]
        out["date_to"] = singles[0]

    return out


def _extract_month_range(text: str) -> Dict[str, str]:
    """
    월 조건 추출.

    지원:
    - 202604
    - 202604~202605
    - 2026-04
    - 2026-04~2026-05
    - 2026.04 ~ 2026.05
    - 2026년 04월 ~ 2026년 05월
    """
    out: Dict[str, str] = {}
    t = str(text or "")

    def _ym(y: str, m: str) -> str:
        return f"{str(y).zfill(4)}{str(m).zfill(2)}"

    # YYYY-MM / YYYY.MM / YYYY/MM 범위
    month_pat = r"((?:19|20)\d{2})\s*[-./]\s*(\d{1,2})"
    m = re.search(
        rf"{month_pat}\s*(?:~|부터|에서|to)\s*{month_pat}",
        t,
        flags=re.IGNORECASE,
    )
    if m:
        out["month_from"] = _ym(m.group(1), m.group(2))
        out["month_to"] = _ym(m.group(3), m.group(4))
        return out

    # YYYY년 MM월 범위
    kor_month_pat = r"((?:19|20)\d{2})\s*년\s*(\d{1,2})\s*월"
    m = re.search(
        rf"{kor_month_pat}\s*(?:~|부터|에서|to)\s*{kor_month_pat}",
        t,
        flags=re.IGNORECASE,
    )
    if m:
        out["month_from"] = _ym(m.group(1), m.group(2))
        out["month_to"] = _ym(m.group(3), m.group(4))
        return out
    
    # YYYY년 MM월 / YYYY MM월 단일 또는 2개 이상
    # 예:
    # - 2026년 06월
    # - 2026 06월
    # - 2026년 6월
    flex_kor_month_pat = r"((?:19|20)\d{2})\s*(?:년\s*)?(\d{1,2})\s*월"
    formatted_kr_month = re.findall(flex_kor_month_pat, t, flags=re.IGNORECASE)

    if len(formatted_kr_month) >= 2:
        y1, m1 = formatted_kr_month[0]
        y2, m2 = formatted_kr_month[1]
        out["month_from"] = _ym(y1, m1)
        out["month_to"] = _ym(y2, m2)
        return out
    elif len(formatted_kr_month) == 1:
        y, mth = formatted_kr_month[0]
        v = _ym(y, mth)
        out["month_from"] = v
        out["month_to"] = v
        return out

    # 기존 YYYYMM ~ YYYYMM
    pairs = re.findall(r"((?:19|20)\d{4})\s*[~\-]\s*((?:19|20)\d{4})", t)
    if pairs:
        out["month_from"], out["month_to"] = pairs[0]
        return out

    # 단일 YYYY-MM / YYYY.MM / YYYY/MM
    # 단, YYYY-MM-DD의 앞부분은 월 조건으로 잡지 않는다.
    formatted = re.findall(
        r"((?:19|20)\d{2})\s*[-./]\s*(\d{1,2})(?!\s*[-./]\s*\d{1,2})",
        t,
    )
    if len(formatted) >= 2:
        y1, m1 = formatted[0]
        y2, m2 = formatted[1]
        out["month_from"] = _ym(y1, m1)
        out["month_to"] = _ym(y2, m2)
        return out
    elif len(formatted) == 1:
        y, mth = formatted[0]
        v = _ym(y, mth)
        out["month_from"] = v
        out["month_to"] = v
        return out

    # 기존 YYYYMM 처리
    singles = re.findall(r"(?:19|20)\d{4}", t)
    if len(singles) >= 2:
        out["month_from"], out["month_to"] = singles[0], singles[1]
    elif len(singles) == 1:
        out["month_from"] = singles[0]
        out["month_to"] = singles[0]

    return out

def _extract_year_range_as_months(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}

    # 1900~2026, 2023~2026, 2023년부터 2026년까지
    pairs = re.findall(
        r"(?<!\d)((?:19|20)\d{2})\s*년?\s*(?:~|-|부터|에서|to)\s*((?:19|20)\d{2})\s*년?(?:까지)?(?!\d)",
        text,
        flags=re.IGNORECASE,
    )
    if pairs:
        y1, y2 = pairs[0]
        out["month_from"] = f"{y1}01"
        out["month_to"] = f"{y2}12"
        return out

    # 2025 단독 연도
    singles = re.findall(r"(?<!\d)((?:19|20)\d{2})\s*년?(?!\d)", text)
    if len(singles) == 1:
        y = singles[0]
        out["month_from"] = f"{y}01"
        out["month_to"] = f"{y}12"

    return out


def _last_day_from_yyyymm(yyyymm: str) -> str:
    s = re.sub(r"[^0-9]", "", str(yyyymm or ""))
    if len(s) != 6:
        return ""

    try:
        y = int(s[:4])
        m = int(s[4:6])
    except Exception:
        return ""

    if not (1 <= m <= 12):
        return ""

    if m == 12:
        nxt = date(y + 1, 1, 1)
    else:
        nxt = date(y, m + 1, 1)

    return (nxt - timedelta(days=1)).strftime("%Y%m%d")


def get_nlq_period_action_class(action: str) -> str:
    """Return the canonical NLQ period policy class without importing UI code."""
    try:
        from app.sims.nlq.action_inventory import implemented_actions

        for spec in implemented_actions():
            if spec.canonical_action != str(action or "").strip():
                continue
            if spec.handler_kind == "analytics":
                return "aggregate_analysis"
            if spec.handler_target.endswith("product_flow_service.get_product_flow_result"):
                return "single_entity_history"
            if spec.handler_target.endswith("product_inventory_service.get_product_inventory_result"):
                return "inventory_movement"
            if spec.handler_target.endswith("rddbc210_service.get_rddbc210_result") or spec.handler_target.endswith("rddbc220_service.get_rddbc220_result"):
                return "inventory_snapshot"
            if spec.handler_kind == "io_service":
                return "list_detail"
    except Exception:
        pass
    return "other"


def _has_period_param(params: Dict[str, Any]) -> bool:
    return any(clean_text((params or {}).get(key)) for key in (
        "date_from", "date_to", "month_from", "month_to",
    ))


def _has_explicit_value(params: Dict[str, Any], keys: tuple[str, ...]) -> bool:
    for key in keys:
        value = (params or {}).get(key)
        if isinstance(value, (list, tuple, set)):
            if any(clean_text(item) for item in value):
                return True
        elif clean_text(value):
            return True
    return False


_NLQ_EXPLICIT_CONDITION_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("product", ("physic_cd", "physic_nm")),
    ("product_category", (
        "product_group_cd", "product_group_nm", "product_di", "product_di_list",
        "dashboard_product_di_list", "product_class", "product_class_list",
        "dashboard_product_class_list",
    )),
    ("manufacturer", ("maker_cd", "maker_nm", "product_ven_cd", "product_ven_nm")),
    ("vendor", (
        "ven_cd", "ven_nm", "buy_cd", "buy_nm", "sale_cd", "sale_nm",
        "order_cd", "order_nm", "real_ven_cd", "real_ven_nm",
    )),
    ("stock", ("stock_cd", "stock_cds", "stock_cd_list", "stock_nm")),
    ("salesperson", ("sales_man", "sales_man_nm", "salesperson_cd", "salesperson_nm")),
    ("region", ("region_cd", "region_nm")),
    ("io_type", ("io_gu", "io_gu_list", "io_gu_prefix")),
)


def get_nlq_explicit_condition_names(params: Dict[str, Any]) -> list[str]:
    """Return only user-parsed, result-narrowing condition categories."""
    return [
        name
        for name, keys in _NLQ_EXPLICIT_CONDITION_GROUPS
        if _has_explicit_value(params, keys)
    ]


def _policy_today(params: Dict[str, Any], today: date | None) -> date:
    """Resolve a testable policy date without treating it as a user period."""
    if today is not None:
        return today
    for key in ("policy_date", "as_of_date"):
        raw = clean_text((params or {}).get(key))
        if re.fullmatch(r"\d{8}", raw):
            try:
                return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
            except ValueError:
                pass
    return date.today()


def _subtract_months(month_start: date, months: int) -> date:
    index = month_start.year * 12 + (month_start.month - 1) - int(months)
    return date(index // 12, index % 12 + 1, 1)


def apply_nlq_default_period_policy(
    params: Dict[str, Any],
    action: str,
    *,
    today: date | None = None,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Apply one canonical NLQ default-period policy after parsing user input.

    The function receives parser output before company or screen defaults are
    injected, so its condition list remains limited to user-specified filters.
    """
    out = dict(params or {})
    action_class = get_nlq_period_action_class(action)
    # Parser may have populated a legacy default range before the canonical
    # action is known. That is not a user-specified period and must not win
    # over this policy.
    parser_default_date = clean_text(out.get("_default_date_applied")).upper() == "Y"
    explicit_period_present = _has_period_param(out) and not parser_default_date
    explicit_condition_names = get_nlq_explicit_condition_names(out)
    policy = {
        "action": str(action or "").strip(),
        "action_class": action_class,
        "explicit_period_present": explicit_period_present,
        "explicit_condition_names": explicit_condition_names,
        "explicit_condition_count": len(explicit_condition_names),
        "default_policy": "none",
        "policy_reason": "explicit_period" if explicit_period_present else "not_applicable",
        "auto_applied": False,
        "final_date_from": clean_text(out.get("date_from")),
        "final_date_to": clean_text(out.get("date_to")),
    }

    if clean_text(out.get("_default_month_applied")):
        policy.update({
            "explicit_period_present": False,
            "default_policy": "current_month_snapshot",
            "policy_reason": "monthly_inventory",
            "auto_applied": True,
        })
        return out, policy

    if explicit_period_present:
        return out, policy

    current_day = _policy_today(out, today)
    if action_class == "aggregate_analysis":
        completed_month_end = current_day.replace(day=1) - timedelta(days=1)
        date_from = _subtract_months(completed_month_end.replace(day=1), 5)
        date_to = completed_month_end
        default_policy = "completed_6months"
        policy_reason = "analytics_completed_months"
    elif action_class == "single_entity_history":
        date_from = current_day - timedelta(days=6)
        default_policy = "recent_7days"
        policy_reason = "single_product_history"
    elif action_class == "inventory_movement":
        # 제품재고현황/제품재고장은 입출고 계산을 포함하는 월 조회다.
        # 조건 유무와 무관하게 기간이 없으면 현재월 시작일부터 평가일까지
        # 동일하게 잡는다.
        date_from = current_day.replace(day=1)
        default_policy = "current_month_inventory"
        policy_reason = "inventory_month_query"
    elif action_class == "list_detail":
        if explicit_condition_names:
            date_from = current_day.replace(day=1)
            default_policy = "current_month"
            policy_reason = "explicit_condition"
        else:
            date_from = current_day
            default_policy = "recent_1day"
            policy_reason = "no_explicit_condition"
    else:
        return out, policy

    out["date_from"] = date_from.strftime("%Y%m%d")
    out["date_to"] = date_to.strftime("%Y%m%d") if action_class == "aggregate_analysis" else current_day.strftime("%Y%m%d")
    out["month_from"] = date_from.strftime("%Y%m")
    out["month_to"] = out["date_to"][:6]
    out.pop("_default_date_applied", None)
    policy.update({
        "default_policy": default_policy,
        "policy_reason": policy_reason,
        "auto_applied": True,
        "final_date_from": out["date_from"],
        "final_date_to": out["date_to"],
    })
    return out, policy


def _apply_date_params_for_product_flow(params: Dict[str, Any], text: str) -> Dict[str, Any]:
    """
    제품수불현황은 실제 조회에 date_from/date_to가 필요하다.
    2024~2026 같은 연도 표현 또는 202605 같은 월 표현을 date range로 보정한다.
    """
    out = dict(params or {})

    date_from = clean_text(out.get("date_from"))
    date_to = clean_text(out.get("date_to"))

    # 이미 일자 범위가 있으면 월만 보조로 채운다.
    if date_from or date_to:
        # 일자 조건이 명확하면 기준월은 일자에서 다시 만든다.
        if date_from:
            out["month_from"] = date_from[:6]
        if date_to:
            out["month_to"] = date_to[:6]
        elif date_from:
            out["month_to"] = date_from[:6]
        return out
    
    month_from = clean_text(out.get("month_from"))
    month_to = clean_text(out.get("month_to"))

    # 2024~2026 같은 4자리 연도 범위 보정
    if not month_from and not month_to:
        year_params = _extract_year_range_as_months(text)
        if year_params:
            out.update(year_params)
            out["_year_month_range_applied"] = "Y"
            month_from = clean_text(out.get("month_from"))
            month_to = clean_text(out.get("month_to"))

    # 월 범위를 일자로 변환
    if month_from and not clean_text(out.get("date_from")):
        out["date_from"] = f"{month_from[:6]}01"

    if month_to and not clean_text(out.get("date_to")):
        out["date_to"] = _last_day_from_yyyymm(month_to[:6])

    return out

def _apply_date_params_for_product_inventory(params: Dict[str, Any], text: str) -> Dict[str, Any]:
    """
    제품재고현황/제품재고장은 실제 조회에 date_from/date_to가 필요하다.
    제품수불현황과 동일하게 2023~2026, 202605 같은 기간 표현을 date range로 보정한다.
    """
    out = _apply_date_params_for_product_flow(params, text)
    date_from = clean_text(out.get("date_from"))
    date_to = clean_text(out.get("date_to"))

    # A single explicit day is an inventory movement cutoff, not a one-day
    # movement query. Preserve explicit ranges, including a same-day range.
    has_explicit_range = bool(
        re.search(r"(?:~|부터|까지|에서|\bto\b|\d{8}\s*-\s*\d{8})", text or "", re.IGNORECASE)
    )
    if date_from and date_from == date_to and not has_explicit_range:
        out["date_from"] = f"{date_to[:6]}01"
        out["month_from"] = date_to[:6]
    return out


_NAMED_CONDITION_VALUE_KEYS = (
    "physic_nm", "maker_nm", "product_ven_nm", "ven_nm", "buy_nm",
    "sale_nm", "order_nm", "real_ven_nm", "sales_man_nm", "stock_nm",
    "stock_apply_nm", "product_group_nm", "product_di_nm", "product_class_nm",
    "ven_group_nm", "ven_kind_nm", "add_nm", "mod_nm", "region_nm",
)


def _action_suffix_phrases(action: str) -> tuple[str, ...]:
    """Return registered action labels that are safe to remove only at value tails."""
    phrases = {str(action or "").strip()}
    try:
        from app.sims.nlq.action_inventory import implemented_actions

        for spec in implemented_actions():
            phrases.add(str(spec.canonical_action or "").strip())
            phrases.add(str(spec.panel_action or "").strip())
            phrases.update(str(alias or "").strip() for alias in spec.label_aliases)
    except Exception:
        pass

    # These are parser-supported aliases for the two product services.  They
    # are action labels, not value-specific exceptions.
    phrases.update(_PRODUCT_FLOW_WORDS)
    phrases.update(_PRODUCT_INVENTORY_WORDS)
    return tuple(sorted((item for item in phrases if item), key=len, reverse=True))


def sanitize_io_named_condition_values(
    params: Dict[str, Any],
    *,
    action: str,
) -> Dict[str, Any]:
    """Remove a trailing resolved action phrase from parsed named conditions.

    Label-based parsing intentionally captures the rest of a sentence.  This
    shared boundary prevents a final action phrase (for example ``출고명세
    조회``) from becoming part of a manufacturer, vendor, or product value.
    It only removes registered action labels at the *end* of a value.
    """
    out = dict(params or {})
    suffixes = _action_suffix_phrases(action)
    cleaned_fields: list[tuple[str, int, int]] = []

    for key in _NAMED_CONDITION_VALUE_KEYS:
        raw_value = clean_text(out.get(key))
        if not raw_value:
            continue

        value = raw_value
        for phrase in suffixes:
            # Canonical labels may include 조회/현황/목록 themselves.  The
            # shortened form is also accepted only when it is a value tail.
            base = re.sub(r"\s*(?:조회|현황|목록|검색|확인)\s*$", "", phrase).strip()
            for candidate in (phrase, base):
                if not candidate:
                    continue
                pattern = rf"(?:\s+){re.escape(candidate)}(?:\s*(?:조회|현황|목록|검색|확인))?\s*$"
                stripped = re.sub(pattern, "", value).strip()
                if stripped != value:
                    value = stripped
                    break
            if value != raw_value:
                break

        value = _trim_named_value(value)
        if value != raw_value:
            out[key] = value
            cleaned_fields.append((key, len(raw_value), len(value)))

    if cleaned_fields:
        # Do not emit business values or inject diagnostics into service
        # parameters. The log is limited to field names and value lengths.
        log.info(
            "[nlq.condition_cleanup] action=%s suffix_removed=%s fields=%s value_lengths=%s",
            str(action or ""),
            True,
            [field for field, _, _ in cleaned_fields],
            [(before, after) for _, before, after in cleaned_fields],
        )
    return out


def _apply_date_params_for_io_detail(params: Dict[str, Any], text: str) -> Dict[str, Any]:
    """
    입고명세/출고명세/거래명세서/세금계산서 같은 일자 기준 조회에서
    2026, 2024~2026, 202605 같은 표현을 date_from/date_to로 보정한다.
    """
    out = dict(params or {})

    date_from = clean_text(out.get("date_from"))
    date_to = clean_text(out.get("date_to"))

    if date_from or date_to:
        # 일자 조건이 명확하면 기준월은 일자에서 다시 만든다.
        # _extract_month_range()가 YYYY-MM-DD의 일부를 월로 오인해
        # month_from=202600 같은 값을 만든 경우를 방지한다.
        if date_from:
            out["month_from"] = date_from[:6]
        if date_to:
            out["month_to"] = date_to[:6]
        elif date_from:
            out["month_to"] = date_from[:6]
        return out
    
    month_from = clean_text(out.get("month_from"))
    month_to = clean_text(out.get("month_to"))

    if not month_from and not month_to:
        year_params = _extract_year_range_as_months(text)
        if year_params:
            out.update(year_params)
            out["_year_month_range_applied"] = "Y"
            month_from = clean_text(out.get("month_from"))
            month_to = clean_text(out.get("month_to"))

    if month_from and not clean_text(out.get("date_from")):
        out["date_from"] = f"{month_from[:6]}01"

    if month_to and not clean_text(out.get("date_to")):
        out["date_to"] = _last_day_from_yyyymm(month_to[:6])

    return out

def _ensure_default_date_range_for_heavy_check(params: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(params or {})
    if (
        clean_text(out.get("date_from"))
        or clean_text(out.get("date_to"))
        or clean_text(out.get("month_from"))
        or clean_text(out.get("month_to"))
    ):
        return out

    today = date.today()
    first_day = today.replace(day=1)

    out["date_from"] = first_day.strftime("%Y%m%d")
    out["date_to"] = today.strftime("%Y%m%d")
    out["_default_date_applied"] = "Y"
    return out

def _apply_month_params_for_monthly_stock(params: Dict[str, Any], text: str) -> Dict[str, Any]:
    out = dict(params or {})

    # date_from/date_to 로 들어온 경우 월집계용 month_from/month_to 로 보정
    date_from = clean_text(out.get("date_from"))
    date_to = clean_text(out.get("date_to"))

    if not clean_text(out.get("month_from")) and date_from:
        out["month_from"] = date_from[:6]

    if not clean_text(out.get("month_to")) and date_to:
        out["month_to"] = date_to[:6]

    # 2025 같은 4자리 연도는 202501~202512 로 해석
    if (
        not clean_text(out.get("month_from"))
        and not clean_text(out.get("month_to"))
        and not clean_text(out.get("date_from"))
        and not clean_text(out.get("date_to"))
    ):
        year_params = _extract_year_range_as_months(text)
        if year_params:
            out.update(year_params)
            out["_year_month_range_applied"] = "Y"

    # 월도 일자도 연도도 없으면 이번 달 기본 적용
    if (
        not clean_text(out.get("month_from"))
        and not clean_text(out.get("month_to"))
        and not clean_text(out.get("date_from"))
        and not clean_text(out.get("date_to"))
    ):
        this_month = date.today().strftime("%Y%m")
        out["month_from"] = this_month
        out["month_to"] = this_month
        out["_default_month_applied"] = "Y"
        
    # 월집계에서 매입/입고, 매출/출고 방향을 별도 조건으로 보관
    # 매입처/매출처라는 단어는 거래처 속성이므로 방향 조건으로 보지 않음
    if "입고" in text or ("매입" in text and "매입처" not in text):
        out["stock_side"] = "in"
    elif "출고" in text or ("매출" in text and "매출처" not in text):
        out["stock_side"] = "out"

    return out

def _extract_code(text: str, label: str, digits: int) -> Optional[str]:
    patterns = [
        rf"{label}\s*[:=]?\s*(\d{{{digits}}})",
        rf"{label}\s+(\d{{{digits}}})",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return None


def _is_product_code_token(value: Any, *, explicit: bool = False) -> bool:
    """제품코드는 숫자 변환 없이 5자리 업무코드 문자열로만 판정한다."""
    token = str(value or "").strip()
    if re.fullmatch(r"\d{5}", token):
        return True
    if explicit and re.fullmatch(r"[A-Za-z0-9]{5}", token):
        return True
    return bool(
        re.fullmatch(r"[A-Za-z0-9]{5}", token)
        and re.search(r"\d", token)
    )


def _extract_product_code_for_io(text: str) -> Optional[str]:
    """IO 문장의 명시·단독 제품코드를 문자열 그대로 추출한다."""
    t = _norm(text)
    for match in re.finditer(
        r"(?P<label>제품코드|제품)\s*[:=]?\s*(?P<code>[A-Za-z0-9]{5})(?![A-Za-z0-9])",
        t,
    ):
        label = match.group("label")
        code = match.group("code")
        if _is_product_code_token(code, explicit=(label == "제품코드")):
            return code

    if _has_any(t, _PRODUCT_IO_WORDS):
        for match in re.finditer(r"(?<![A-Za-z0-9])([A-Za-z0-9]{5})(?![A-Za-z0-9])", t):
            code = match.group(1)
            if _is_product_code_token(code):
                return code
    return None

def _extract_code_flex(text: str, label: str, min_digits: int = 1, max_digits: int = 6) -> Optional[str]:
    patterns = [
        rf"{label}\s*[:=]?\s*(\d{{{min_digits},{max_digits}}})",
        rf"{label}\s+(\d{{{min_digits},{max_digits}}})",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return None


def _normalize_stock_code_token(value: str) -> str:
    digits = re.sub(r"[^0-9]", "", str(value or ""))
    if not digits:
        return ""

    # 업무코드 자릿수는 Rddbc010.Rd01_Col_Num 기준이다.
    # 여기서 임의로 5자리/6자리 보정하지 않는다.
    # 예: 00001은 00001 그대로, 000001은 000001 그대로 둔다.
    if 1 <= len(digits) <= 6:
        return digits

    return ""

def _extract_stock_codes(text: str) -> list[str]:
    codes: list[str] = []

    # 재고위치 000001,000002 / 재고위치코드 1/2 형태만 코드로 인정
    # 공백 뒤의 2026 같은 연도는 재고위치코드로 잡지 않는다.
    block_matches = re.findall(
        r"(?:재고위치코드|재고위치)\s*[:=]?\s*(\d{1,6}(?:\s*[,/]\s*\d{1,6})*)",
        text,
    )

    for block in block_matches:
        for raw_code in re.findall(r"\d{1,6}", block):
            # 2025, 2026 같은 단독 연도는 재고위치코드가 아니다.
            if re.fullmatch(r"(?:19|20)\d{2}", raw_code):
                continue

            code = _normalize_stock_code_token(raw_code)
            if code and code not in codes:
                codes.append(code)

    return codes

_NAME_STOP_WORDS = (
    # 공통 종료어
    "조회", "검색", "보여줘", "알려줘", "찾아줘", "찾아", "목록",
    "기준", "으로", "로", "만", "중", "그리고", "및",

    # 입출고/재고/수불 액션어
    "현황", "수불", "수불부", "수불현황",
    "제품수불", "제품수불부", "제품수불현황",
    "재고", "재고장", "재고현황",
    "제품재고장", "제품재고현황",
    "실수불", "장부수불", "실재고", "장부재고",
    "입출고일자", "명세서일자",

    # 제품재고장 조건/기준어
    "총평균단가", "최종매입가", "기준가", "현보험약가",
    "제조사", "발주처", "매입처",
    "제품그룹명", "제품그룹",
    "제품구분명", "제품구분", "구분명",
    "제품분류명", "제품분류", "분류명",

    # 분석/KPI 액션어
    # extract_params()를 분석/KPI NLQ에서도 재사용하므로
    # "제조사명 일동 품목별 매출 예상 조회" 같은 문장에서
    # 조건값이 "일동 품목별 매출 예상"으로 길어지는 것을 방지한다.
    "품목별",
    "매출",
    "매출추세",
    "매출예상",
    "예상매출",
    "예상",
    "추세",
    "요약",
    "요약표",
    "분석",
    "KPI",
    "kpi",
    "재고부족",
    "부족현황",
    "부족",
)

def _looks_like_date_token(value: str) -> bool:
    """
    제품명/거래처명 같은 명칭 추출 중 날짜/월/연도 토큰을 만나면
    그 뒤를 조건값으로 보지 않기 위한 판정.

    예:
    - 2025
    - 202501
    - 20250101
    - 2026-04
    - 2026-04-19
    - 2026-04-19~2026-05-19
    """
    raw = str(value or "").strip()
    if not raw:
        return False

    compact = re.sub(r"\s+", "", raw)

    # YYYY-MM-DD / YYYY.MM.DD / YYYY/MM/DD 단일 또는 범위
    if re.fullmatch(
        r"(?:19|20)\d{2}[-./]\d{1,2}[-./]\d{1,2}"
        r"(?:(?:~|부터|에서|to)(?:19|20)\d{2}[-./]\d{1,2}[-./]\d{1,2})?",
        compact,
        flags=re.IGNORECASE,
    ):
        return True

    # YYYY-MM / YYYY.MM / YYYY/MM 단일 또는 범위
    if re.fullmatch(
        r"(?:19|20)\d{2}[-./]\d{1,2}"
        r"(?:(?:~|부터|에서|to)(?:19|20)\d{2}[-./]\d{1,2})?",
        compact,
        flags=re.IGNORECASE,
    ):
        return True

    # YYYY년MM월DD일 / YYYY년MM월
    if re.fullmatch(
        r"(?:19|20)\d{2}년\d{1,2}월(?:\d{1,2}일?)?"
        r"(?:(?:~|부터|에서|to)(?:19|20)\d{2}년\d{1,2}월(?:\d{1,2}일?)?)?",
        compact,
        flags=re.IGNORECASE,
    ):
        return True

    s = re.sub(r"[^0-9]", "", raw)
    if not s:
        return False

    # 4자리 연도: 2025
    if len(s) == 4:
        try:
            y = int(s)
            return 1900 <= y <= 2099
        except Exception:
            return False

    # YYYYMM / YYYYMMDD
    if len(s) in (6, 8):
        return True

    # YYYYMMDD~YYYYMMDD처럼 붙은 범위
    if len(s) == 16 and re.fullmatch(r"(?:19|20)\d{6}(?:19|20)\d{6}", s):
        return True

    # YYYYMM~YYYYMM처럼 붙은 범위
    if len(s) == 12 and re.fullmatch(r"(?:19|20)\d{4}(?:19|20)\d{4}", s):
        return True

    return False

def _trim_named_value(value: str) -> str:
    v = _norm(value)
    if not v:
        return ""

    # 날짜 범위 제거 후 남는 "~", "-", "/" 같은 기호가
    # 제품명/거래처명 앞뒤에 붙지 않도록 정리한다.
    v = re.sub(r"^[\s~\-–—:/,._]+", "", v).strip()
    v = re.sub(r"[\s~\-–—:/,._]+$", "", v).strip()

    if not v:
        return ""

    # 값 전체가 날짜/월/연도 토큰인 경우만 제거한다.
    # 기존처럼 문자열 안에 날짜가 포함됐다는 이유만으로 전체를 버리면
    # "삼진제약 2023~2026년 조회" 같은 제조사명이 사라진다.
    date_only_text = re.sub(r"\s+", "", v)

    if re.fullmatch(r"(?:(?:19|20)\d{2}|(?:19|20)\d{4}|(?:19|20)\d{6})", date_only_text):
        return ""
    if re.fullmatch(r"(?:(?:19|20)\d{2}|(?:19|20)\d{4}|(?:19|20)\d{6})년?", date_only_text):
        return ""
    if re.fullmatch(
        r"(?:(?:19|20)\d{2}|(?:19|20)\d{4}|(?:19|20)\d{6})\s*(?:~|-|부터|에서|to)\s*(?:(?:19|20)\d{2}|(?:19|20)\d{4}|(?:19|20)\d{6})년?",
        v,
        flags=re.IGNORECASE,
    ):
        return ""

    for sep in [",", "/", " / ", " , ", " 그리고 ", " 및 "]:

        if sep in v:
            v = v.split(sep, 1)[0].strip()

    parts = v.split()
    kept: list[str] = []
    for p in parts:
        if _looks_like_date_token(p):
            break
        if p in _NAME_STOP_WORDS:
            break
        kept.append(p)

    return " ".join(kept).strip()

def _extract_vendor_name_before_side_label(text: str, labels: tuple[str, ...]) -> str:
    """
    '한미 매입처 거래내역 조회'처럼
    거래처명이 매입처/매출처 라벨 앞에 오는 표현을 보정한다.
    """
    t = _norm(text)
    if not t:
        return ""

    labels_sorted = sorted(labels, key=len, reverse=True)
    labels_pat = "|".join(re.escape(x) for x in labels_sorted)

    m = re.search(rf"(.+?)\s*(?:{labels_pat})(?:에서|의)?(?:\s|$)", t)
    if not m:
        return ""

    prefix = _norm(m.group(1))

    # 앞쪽에 붙은 action/불필요어 제거
    prefix = re.sub(
        r"(입고명세|출고명세|거래명세서\s*공통|세금계산서\s*공통|"
        r"입고|출고|매입|매출|명세|내역|거래내역|조회|검색|보여줘|알려줘)",
        " ",
        prefix,
    )

    prefix = _norm(prefix)
    if not prefix:
        return ""

    # 여러 단어가 남으면 마지막 단어를 거래처명 후보로 본다.
    parts = [p for p in prefix.split() if p and not _looks_like_date_token(p)]
    if not parts:
        return ""

    return _trim_named_value(parts[-1])

def _extract_vendor_name_before_side_label(text: str, labels: tuple[str, ...]) -> str:
    """
    '한미 매입처 거래내역 조회',
    '한미 매입처에서 거래내역 조회',
    '대학약국 매출처 거래내역 조회'
    처럼 거래처명이 매입처/매출처 라벨 앞에 오는 표현을 보정한다.
    """
    t = _norm(text)
    if not t:
        return ""

    labels_sorted = sorted(labels, key=len, reverse=True)
    labels_pat = "|".join(re.escape(x) for x in labels_sorted)

    m = re.search(rf"(.+?)\s*(?:{labels_pat})(?:에서|의)?(?:\s|$)", t)
    if not m:
        return ""

    prefix = _norm(m.group(1))

    # 앞쪽 action/불필요어 제거 후 마지막 단어를 거래처명 후보로 사용
    prefix = re.sub(
        r"(입고명세|출고명세|거래명세서\s*공통|세금계산서\s*공통|"
        r"입고|출고|매입|매출|명세|내역|거래내역|조회|검색|보여줘|알려줘)",
        " ",
        prefix,
    )
    prefix = _norm(prefix)

    if not prefix:
        return ""

    parts = [p for p in prefix.split() if p and not _looks_like_date_token(p)]
    if not parts:
        return ""

    return _trim_named_value(parts[-1])

def _normalize_io_detail_vendor_params(
    params: Dict[str, Any],
    *,
    action: str,
    raw: str,
) -> Dict[str, Any]:
    """
    입고/출고 명세의 거래처 조건 보정.

    IO 상세에서는 사용자가 말하는
    - 입고명세 매입처명 XXX
    - 출고명세 매출처명 XXX
    은 제품마스터의 매입처/매출처가 아니라
    실제 명세의 거래처 조건으로 보는 것이 자연스럽다.

    따라서 입고명세의 매입처명은 ven_nm으로,
    출고명세의 매출처명도 ven_nm으로 보정한다.
    """
    out = dict(params or {})
    a = str(action or "").strip()
    t = str(raw or "").strip()

    is_in_detail = a in {
        "입고명세 조회",
        "입고↔거래명세서 검증",
        "입고↔세금계산서 검증",
    }

    is_out_detail = a in {
        "출고명세 조회",
        "출고↔거래명세서 검증",
        "출고↔세금계산서 검증",
    }

    # 입고명세 매입처명 한미 → 거래처명 한미
    if is_in_detail and "매입처" in t:
        prefix_ven_nm = _extract_vendor_name_before_side_label(t, ("매입처명", "매입처"))

        if clean_text(prefix_ven_nm):
            out["ven_nm"] = clean_text(prefix_ven_nm)
            out.pop("buy_nm", None)
        elif not clean_text(out.get("ven_nm")) and clean_text(out.get("buy_nm")):
            out["ven_nm"] = clean_text(out.get("buy_nm"))
            out.pop("buy_nm", None)

        if not clean_text(out.get("ven_cd")) and clean_text(out.get("buy_cd")):
            out["ven_cd"] = clean_text(out.get("buy_cd"))
            out.pop("buy_cd", None)

    # 출고명세 매출처명 대학약국 → 거래처명 대학약국
    # 현재 extract_params()는 매출처명을 별도 추출하지 않으므로 여기서 직접 보강한다.
    if is_out_detail and "매출처" in t:
        prefix_ven_nm = _extract_vendor_name_before_side_label(t, ("매출처명", "매출처"))
        sales_ven_nm = prefix_ven_nm or _extract_named_text(t, ("매출처명", "매출처"))
        sales_ven_cd = _extract_code(t, "매출처", 5) or _extract_code(t, "매출처코드", 5)

        if not clean_text(out.get("ven_nm")) and clean_text(sales_ven_nm):
            out["ven_nm"] = clean_text(sales_ven_nm)

        if not clean_text(out.get("ven_cd")) and clean_text(sales_ven_cd):
            out["ven_cd"] = clean_text(sales_ven_cd)

    return out

def _extract_named_text(text: str, labels: tuple[str, ...]) -> Optional[str]:
    """
    라벨 뒤의 명칭 조건을 추출한다.

    주의:
    - "제조사명"과 "제조사"처럼 접두어가 겹치는 라벨이 있으므로
      긴 라벨을 먼저 매칭한다.
    - 실제 값은 _trim_named_value()에서 액션어/날짜/불필요어를 제거한다.
    """
    labels_sorted = sorted(labels, key=len, reverse=True)
    labels_pat = "|".join(re.escape(x) for x in labels_sorted)

    patterns = [
        rf"(?:{labels_pat})\s*[:=]?\s*([^\n,]+)",
        rf"(?:{labels_pat})\s+([^\n,]+)",
    ]

    for pat in patterns:
        m = re.search(pat, text)
        if m:
            val = _trim_named_value(m.group(1))
            if val:
                return val

    return None

def _extract_implicit_product_name(text: str) -> Optional[str]:
    m = re.search(r"([가-힣A-Za-z0-9._()\-/]+)\s+제품(?:\s|$)", text)
    if not m:
        return None

    cand = _trim_named_value(m.group(1))
    if not cand:
        return None

    if _looks_like_date_token(cand):
        return None

    if cand in {"제품", "제품명", "제품코드"}:
        return None

    return cand

def _extract_product_name_after_label_for_io(text: str) -> Optional[str]:
    """
    IO/재고/수불 NLQ에서 '제품 xxx', '제품명 xxx' 형태의 제품명을 추출한다.

    주의:
    - 원문에는 '제품수불현황'처럼 '제품'이 액션어 안에 들어갈 수 있으므로
      먼저 액션어를 제거한 뒤 라벨을 찾는다.
    - '제품 00029'처럼 5자리 숫자는 제품코드로 보고 제품명으로 반환하지 않는다.
    """
    t = _norm(text)
    if not t:
        return None

    # 액션어 제거: '제품수불현황 제품 스토린액 조회'에서 앞의 제품수불현황 제거
    t = re.sub(
        r"(제품수불현황|제품수불부|제품수불|수불현황|수불부|"
        r"제품재고현황|제품재고장|재고현황|재고장)",
        " ",
        t,
    )
    t = _norm(t)

    m = re.search(
        r"(?:제품명|품목명|상품명)\s*[:=]?\s*([^\n,]+)",
        t,
    )

    if not m:
        # '제품' 단독 라벨만 허용한다.
        # 제품그룹명/제품구분명/제품분류명/제품코드 안의 '제품'은 제품명 라벨로 보면 안 된다.
        m = re.search(
            r"(?<![가-힣A-Za-z0-9])제품(?!그룹명|그룹|구분명|구분|분류명|분류|코드|명)\s+([^\n,]+)",
            t,
        )
        
    if not m:
        return None

    val = _trim_named_value(m.group(1))
    if not val:
        return None

    # 제품 00029 같은 경우는 제품코드 조건이므로 이름으로 보지 않는다.
    if _is_product_code_token(val):
        return None

    if _looks_like_date_token(val):
        return None

    if val in {"제품", "제품명", "제품코드", "품목", "품목명", "상품", "상품명"}:
        return None

    return val


def _extract_loose_product_name_for_io(text: str) -> Optional[str]:
    t = _norm(text)
    if not t:
        return None

    # 제품 00029 / 제품코드 00029처럼 제품코드가 명확한 문장은
    # 남은 기호(~ 등)를 제품명으로 추정하지 않는다.
    if re.search(r"(?:제품코드|제품)\s*[:=]?\s*[A-Za-z0-9]{5}(?![A-Za-z0-9])", t):
        return None

    # 제조사/발주처/그룹/구분/분류/거래처/재고위치 등
    # 명시 조건 라벨이 있는 문장은 남은 단어를 제품명으로 추정하지 않는다.
    #
    # 예:
    # - 제품재고현황 제조사 건일제약 2023~2026년 조회
    #   → 제조사명 건일제약만 조건이어야 함
    #   → 제품명 건일제약으로 중복 추정하면 안 됨
    #
    # - 제품재고장 2025 제품그룹명 일반 조회
    #   → 제품그룹명 일반만 조건이어야 함
    #   → 제품명 일반으로 중복 추정하면 안 됨
    #
    # 제품명/제품 xxx처럼 제품을 명시한 경우는
    # 이 함수보다 앞의 _extract_product_name_after_label_for_io()에서 처리한다.
    explicit_non_product_filter_labels = (
        "제조사명",
        "제조사",
        "제약사명",
        "제약사",
        "발주처명",
        "발주처",
        "매입처명",
        "매입처",
        "거래처명",
        "거래처",
        "실납처명",
        "실납처",
        "영업사원명",
        "영업사원",
        "재고위치명",
        "재고위치",
        "제품그룹명",
        "제품그룹",
        "그룹명",
        "제품구분명",
        "제품구분",
        "구분명",
        "제품분류명",
        "제품분류",
        "분류명",
    )

    if any(label in t for label in explicit_non_product_filter_labels):
        return None

    # 액션/불필요 표현 제거
    t = re.sub(
        r"(제품수불현황|제품수불부|제품수불|수불현황|수불부|"
        r"제품재고현황|제품재고장|재고현황|재고장|"
        r"조회|검색|보여줘|알려줘|찾아줘|찾아|해줘)",
        " ",
        t,
    )

    # 날짜/월/연도 제거
    t = re.sub(r"(?:19|20)\d{6}", " ", t)  # YYYYMMDD
    t = re.sub(r"(?:19|20)\d{4}", " ", t)  # YYYYMM
    t = re.sub(
        r"(?<!\d)(?:19|20)\d{2}\s*년?\s*(?:~|-|부터|에서|to)\s*(?:19|20)\d{2}\s*년?(?!\d)",
        " ",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(r"(?<!\d)(?:19|20)\d{2}\s*년?(?!\d)", " ", t)


    # 재고위치/코드 표현 제거
    t = re.sub(r"(재고위치코드|재고위치)\s*[:=]?\s*\d{1,6}", " ", t)
    t = re.sub(r"(제품코드|제품)\s*[:=]?\s*[A-Za-z0-9]{5}(?![A-Za-z0-9])", " ", t)

    # 기준/범위 표현 제거
    t = re.sub(
        r"(실수불|장부수불|실재고|장부재고|"
        r"입출고일자|명세서일자|"
        r"총평균단가|최종매입가|기준가|현보험약가|"
        r"제조사기준|발주처기준|매입처기준|"
        r"제조사|발주처|매입처|"
        r"제품그룹명|제품구분명|구분명|제품분류명)",
        " ",
        t,
    )

    # 구두점/다중공백 정리
    # 날짜 범위 제거 후 남은 "~"도 제거한다.
    # A standalone month left after parsing a year/month expression is not an
    # implicit product name (for example, "2026년 6월 제품재고").
    t = re.sub(r"(?<!\d)\d{1,2}\s*월(?!\d)", " ", t)
    # Range connectors are syntax, never an implicit product name after the
    # surrounding date tokens have been removed.
    t = re.sub(r"\b(?:부터|까지|에서|to)\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"[,/()]+", " ", t)
    t = re.sub(r"[~\-–—]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()

    
    cand = _trim_named_value(t)
    if not cand:
        return None
    if _looks_like_date_token(cand):
        return None

    return cand


def _resolve_single_product_code_by_name_with_error(name: str) -> tuple[Optional[str], Optional[Exception]]:
    if not clean_text(name):
        return None, None

    try:
        from app.services.rddbc_io_common import query_to_df
        df = query_to_df(
            """
            SELECT TOP 2
                LTRIM(RTRIM(Rd04_Physic_Cd)) AS physic_cd,
                LTRIM(RTRIM(Rd04_Physic_Nm)) AS physic_nm
            FROM dbo.Rddbc040
            WHERE Rd04_Physic_Nm LIKE %(physic_nm_like)s
            GROUP BY Rd04_Physic_Cd, Rd04_Physic_Nm
            ORDER BY Rd04_Physic_Cd
            """,
            {"physic_nm_like": f"%{clean_text(name)}%"},
        )
    except Exception as exc:
        return None, exc

    if df is None or len(df) != 1:
        return None, None

    try:
        row = df.iloc[0]
        if clean_text(row["physic_nm"]) != clean_text(name):
            return None, None
        return clean_text(row["physic_cd"]), None
    except Exception as exc:
        return None, exc


def _resolve_single_product_code_by_name(name: str) -> Optional[str]:
    """Compatibility wrapper for callers that need only an unambiguous code."""
    code, _ = _resolve_single_product_code_by_name_with_error(name)
    return code


def _extract_unlabeled_entity_phrase(text: str, action: str) -> str:
    """Return a conservative proper-noun candidate left after IO syntax removal.

    This is intentionally narrow: labelled conditions remain the parser's source
    of truth, and a phrase is considered only when it is the sole residual token
    around a resolved IO action.
    """
    candidate = _norm(text)
    if not candidate:
        return ""

    # Action spelling is intentionally normalized here rather than per query.
    # In particular, removing ``출고명세`` before ``출고명세서`` leaves a
    # dangling "서" token and silently drops the preceding search phrase.
    action_patterns = (
        r"입고\s*명세(?:서)?",
        r"출고\s*명세(?:서)?",
        r"거래\s*명세서(?:\s*공통)?",
        r"세금\s*계산서(?:\s*공통)?",
        r"제품\s*수불(?:현황|부)?",
        r"제품\s*재고(?:현황|장)?",
        re.escape(action.replace(" 조회", "")),
        r"조회|검색|찾아줘|찾아봐|보여줘|알려줘|확인",
    )
    for pattern in action_patterns:
        if pattern:
            candidate = re.sub(pattern, " ", candidate)

    candidate = re.sub(r"(?:19|20)\d{6}|(?:19|20)\d{4}", " ", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip(" ,:/-~")
    if not candidate or _looks_like_date_token(candidate):
        return ""
    if len(candidate.split()) != 1:
        return ""
    return candidate


def _has_explicit_name_label(text: str) -> bool:
    """Return whether the user explicitly chose one name-search semantic."""
    t = _norm(text)
    if not t:
        return False

    return bool(re.search(
        r"(?:거래처(?:명|코드)?|매입처(?:명|코드)?|매출처(?:명|코드)?|실납처(?:명|코드)?|"
        r"제조사(?:명|코드)?|제약사(?:명|코드)?|발주처(?:명|코드)?|"
        r"제품명|품목명|상품명|제품(?!수불|재고|코드|명|그룹|구분|분류))\s*(?:[:=]|\s)",
        t,
    ))


def _log_entity_resolver(
    *,
    action: str,
    resolver_type: str,
    status: str,
    candidate_count: int,
    elapsed_ms: int,
    exception_class: str = "",
    safe_error_code: str = "",
    final_decision: str = "",
) -> None:
    """Emit only resolver metadata; entity values and SQL stay out of logs."""
    log.info(
        "[nlq.entity_resolver] action=%s resolver_type=%s status=%s "
        "candidate_count=%s elapsed_ms=%s exception_class=%s safe_error_code=%s final_decision=%s",
        action,
        resolver_type,
        status,
        candidate_count,
        elapsed_ms,
        exception_class,
        safe_error_code,
        final_decision,
    )


def _safe_entity_resolver_error_code(exc: Optional[Exception]) -> str:
    """Return a stable, non-sensitive master-resolution error category."""
    if exc is None:
        return ""
    return {
        "NameError": "runtime_name_error",
        "ProgrammingError": "master_query_contract",
        "OperationalError": "master_connection",
        "TimeoutError": "master_timeout",
    }.get(type(exc).__name__, "master_resolver_error")


def _lookup_transaction_vendor_candidates(name: str, *, action: str = "") -> dict[str, Any]:
    """Resolve historical counterparties by the canonical transaction name.

    ``Rd03_Del_Flag='E'`` prevents new registration only.  Historical outbound
    detail lookups must still be able to resolve that counterparty, so this
    deliberately does not use the UI's active-vendor filter.
    """
    started_at = time.perf_counter()
    name = clean_text(name)
    action_text = clean_text(action)
    scope = "purchase_history" if "입고" in action_text else "sales_history"
    try:
        from app.services.rddbc030_service import search_rows

        vendor_df = search_rows(
            scope=scope,
            ven_nm_kw=name,
            top=30,
            only_active=False,
        )
        rows: list[dict[str, str]] = []
        if isinstance(vendor_df, pd.DataFrame):
            seen_codes: set[str] = set()
            for _, row in vendor_df.iterrows():
                vendor_name = clean_text(row.get("Rd03_Ven_Nm"))
                vendor_code = clean_text(row.get("Rd03_Ven_Cd"))
                if name != vendor_name or not vendor_code or vendor_code in seen_codes:
                    continue
                seen_codes.add(vendor_code)
                rows.append({
                    "match_type": "transaction_vendor",
                    "match_value": vendor_name,
                    "match_code": vendor_code,
                })
        return {"candidates": rows, "error": None, "started_at": started_at}
    except Exception as exc:
        return {"candidates": [], "error": exc, "started_at": started_at}


def _lookup_unlabeled_io_entity_candidates(name: str, *, action: str = "") -> dict[str, Any]:
    """Verify an unlabeled name against existing IO master relationships.

    Exact-name matching is deliberate.  A partial match must not silently turn
    into a transaction-vendor, manufacturer, or product predicate.
    """
    name = clean_text(name)
    if not name:
        return {"candidates": [], "outcomes": []}

    out: list[dict[str, str]] = []
    outcomes: list[dict[str, Any]] = []

    def record(resolver_type: str, candidate_count: int, started_at: float, exc: Optional[Exception] = None) -> None:
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        status = "error" if exc is not None else ("success" if candidate_count else "not_found")
        outcome = {
            "resolver_type": resolver_type,
            "status": status,
            "candidate_count": int(candidate_count),
            "elapsed_ms": elapsed_ms,
            "exception_class": type(exc).__name__ if exc is not None else "",
            "safe_error_code": _safe_entity_resolver_error_code(exc),
        }
        outcomes.append(outcome)
        _log_entity_resolver(action=action, final_decision="", **outcome)

    # Historical detail lookup needs the same sales-side vendor-code contract
    # as Rddbc120, including masters no longer available for new registration.
    vendor_lookup = _lookup_transaction_vendor_candidates(name, action=action)
    vendor_rows = list(vendor_lookup.get("candidates") or [])
    out.extend(vendor_rows)
    record(
        "transaction_vendor",
        len(vendor_rows),
        float(vendor_lookup.get("started_at") or time.perf_counter()),
        vendor_lookup.get("error"),
    )

    started_at = time.perf_counter()
    try:
        # Supplier scope already verifies that the vendor is actually linked
        # to active product master rows.
        from app.services.product_supplier_scope_service import (
            SCOPE_MANUFACTURER,
            resolve_supplier_vendor_codes,
        )

        manufacturer_rows = resolve_supplier_vendor_codes(name, mode=SCOPE_MANUFACTURER)
        matched = int(any(clean_text(row.get("name")) == name for row in manufacturer_rows))
        if matched:
            for row in manufacturer_rows:
                if clean_text(row.get("name")) == name and clean_text(row.get("code")):
                    out.append({
                        "match_type": "manufacturer",
                        "match_value": name,
                        "match_code": clean_text(row.get("code")),
                    })
        record("manufacturer", matched, started_at)
    except Exception as exc:
        record("manufacturer", 0, started_at, exc)

    # Keep the existing single-product resolver as the product-master proof
    # path. It deliberately returns a code only for one unambiguous product.
    started_at = time.perf_counter()
    product_code, product_error = _resolve_single_product_code_by_name_with_error(name)
    if product_code:
        out.append({"match_type": "product", "match_value": name, "match_code": product_code})
    record("product", int(bool(product_code)), started_at, product_error)

    return {"candidates": out, "outcomes": outcomes}


def resolve_unlabeled_io_entity_condition(
    text: str,
    *,
    action: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Prepare the common name-search contract without overriding labels.

    ``candidate_required`` is returned when master evidence points to more than
    one semantic condition.  The router owns the existing chat candidate-table
    contract and therefore decides how it is presented to the user.
    """
    out = dict(params or {})
    phrase = _extract_unlabeled_entity_phrase(text, action)

    # Detail and inventory lists are multi-result searches.  A bare token may
    # have been tentatively placed in ``physic_nm`` by the generic parser (for
    # example, "한미약품 제품재고장").  Reclassify it only when the question
    # contains no explicit semantic label, then preserve the common OR-LIKE
    # contract for transaction vendor, product and manufacturer names.
    if (
        action in {"입고명세 조회", "출고명세 조회", "제품재고현황 조회"}
        and phrase
        and not _has_explicit_name_label(text)
    ):
        out.pop("physic_nm", None)
        out["nlq_unlabeled_name"] = phrase
        _log_entity_resolver(
            action=action,
            resolver_type="detail_name_search",
            status="success",
            candidate_count=0,
            elapsed_ms=0,
            final_decision="resolved_like",
        )
        return {
            "status": "resolved",
            "params": out,
            "phrase": phrase,
            "resolved_kind": "unlabeled_like",
            "candidates": [],
        }

    explicit_keys = (
        "ven_cd", "ven_nm", "physic_cd", "physic_nm", "maker_cd",
        "maker_nm", "product_ven_cd", "product_ven_nm", "order_cd",
        "order_nm", "buy_cd", "buy_nm", "real_ven_cd", "real_ven_nm",
        "stock_cd", "stock_nm", "sales_man", "sales_man_nm",
    )
    if any(clean_text(out.get(key)) for key in explicit_keys):
        return {"status": "not_applicable", "params": out, "candidates": []}

    if not phrase:
        return {"status": "not_applicable", "params": out, "candidates": []}

    # Detail and inventory lists are multi-result searches.  They must not
    # resolve a name through master candidates: the service applies one OR LIKE
    # predicate over transaction vendor, product, and manufacturer names.
    if action in {"입고명세 조회", "출고명세 조회", "제품재고현황 조회"}:
        out["nlq_unlabeled_name"] = phrase
        _log_entity_resolver(
            action=action,
            resolver_type="detail_name_search",
            status="success",
            candidate_count=0,
            elapsed_ms=0,
            final_decision="resolved_like",
        )
        return {
            "status": "resolved",
            "params": out,
            "phrase": phrase,
            "resolved_kind": "unlabeled_like",
            "candidates": [],
        }

    lookup = _lookup_unlabeled_io_entity_candidates(phrase, action=action)
    # Accept the pre-closure list return in monkeypatch callers while the
    # production path keeps outcome metadata for errors and timeouts.
    if isinstance(lookup, list):
        candidates = lookup
        outcomes: list[dict[str, Any]] = []
    else:
        candidates = list(lookup.get("candidates") or [])
        outcomes = list(lookup.get("outcomes") or [])
    candidate_keys = {
        (str(row.get("match_type") or ""), clean_text(row.get("match_code")) or clean_text(row.get("match_value")))
        for row in candidates
    }
    kinds = {kind for kind, _ in candidate_keys}
    has_resolver_error = any(str(row.get("status") or "") in {"error", "timeout"} for row in outcomes)
    error_codes = ",".join(sorted({
        str(row.get("safe_error_code") or "")
        for row in outcomes
        if row.get("safe_error_code")
    }))
    if has_resolver_error:
        _log_entity_resolver(
            action=action,
            resolver_type="all",
            status="error",
            candidate_count=len(candidates),
            elapsed_ms=sum(int(row.get("elapsed_ms") or 0) for row in outcomes),
            exception_class=",".join(sorted({str(row.get("exception_class") or "") for row in outcomes if row.get("exception_class")})),
            safe_error_code=error_codes,
            final_decision="resolution_unavailable",
        )
        return {
            "status": "resolution_unavailable",
            "params": out,
            "phrase": phrase,
            "candidates": candidates,
            "resolver_outcomes": outcomes,
        }

    if len(candidate_keys) != 1:
        status = "candidate_required" if candidates else "not_found"
        _log_entity_resolver(
            action=action,
            resolver_type="all",
            status="ambiguous" if candidates else "not_found",
            candidate_count=len(candidates),
            elapsed_ms=sum(int(row.get("elapsed_ms") or 0) for row in outcomes),
            final_decision=status,
        )
        return {
            "status": status,
            "params": out,
            "phrase": phrase,
            "candidates": candidates,
            "resolver_outcomes": outcomes,
        }

    kind = next(iter(kinds))
    if kind == "transaction_vendor":
        candidate = candidates[0]
        out["ven_cd"] = clean_text(candidate.get("match_code"))
        out["ven_nm_display"] = phrase
    elif kind == "manufacturer":
        candidate = candidates[0]
        out["product_ven_cd"] = clean_text(candidate.get("match_code"))
        out["maker_nm_display"] = phrase
        out["product_ven_nm_display"] = phrase
    elif kind == "product":
        out["physic_nm"] = phrase
    else:
        return {"status": "not_applicable", "params": out, "candidates": []}

    _log_entity_resolver(
        action=action,
        resolver_type="all",
        status="success",
        candidate_count=len(candidates),
        elapsed_ms=sum(int(row.get("elapsed_ms") or 0) for row in outcomes),
        exception_class="",
        final_decision="resolved",
    )
    return {
        "status": "resolved",
        "params": out,
        "phrase": phrase,
        "resolved_kind": kind,
        "candidates": candidates,
        "resolver_outcomes": outcomes,
    }

def extract_params(text: str) -> Dict[str, Any]:
    text = _norm(text)
    params: Dict[str, Any] = {}

    params.update(_extract_date_range(text))
    params.update({k: v for k, v in _extract_month_range(text).items() if k not in params})

    ven_cd = _extract_code(text, "거래처", 5) or _extract_code(text, "거래처코드", 5)
    ven_nm = _extract_named_text(text, ("거래처명", "거래처"))

    physic_cd = (
        _extract_product_code_for_io(text)
        or _extract_code(text, "제품", 5)
        or _extract_code(text, "제품코드", 5)
    )

    maker_cd = _extract_code(text, "제조사", 5) or _extract_code(text, "제약사", 5)
    order_cd = _extract_code(text, "발주처", 5)
    buy_cd = _extract_code(text, "매입처", 5)

    real_ven_cd = _extract_code(text, "실납처", 5) or _extract_code(text, "실납처코드", 5)
    sales_man = _extract_code(text, "영업사원", 5) or _extract_code(text, "영업사원코드", 5)

    physic_nm = (
        _extract_named_text(text, ("제품명", "품목명", "상품명"))
        or (
            _extract_product_name_after_label_for_io(text)
            if _has_any(text, _PRODUCT_IO_WORDS)
            else None
        )
        or (
            _extract_loose_product_name_for_io(text)
            if _has_any(text, _PRODUCT_IO_WORDS)
            else None
        )
        or _extract_implicit_product_name(text)
    )

    if physic_cd and physic_nm:
        # 제품코드가 명확히 잡힌 경우에는 제품명 보조조건이
        # 액션어/날짜/불필요어/기호로 잘못 남는 것을 제거한다.
        if (
            str(physic_nm).strip() == str(physic_cd).strip()
            or
            _looks_like_date_token(physic_nm)
            or not re.search(r"[가-힣A-Za-z0-9]", str(physic_nm or ""))
            or physic_nm in {"~", "-", "–", "—", "~년"}
            or physic_nm in _PRODUCT_IO_WORDS
            or physic_nm in _PRODUCT_FLOW_WORDS
            or physic_nm in _PRODUCT_INVENTORY_WORDS
            or physic_nm in {"제품수불현황", "제품수불부", "제품수불", "수불현황", "수불부"}
        ):
            physic_nm = ""

    maker_nm = _extract_named_text(text, ("제조사명", "제약사명", "제조사", "제약사"))
    order_nm = _extract_named_text(text, ("발주처명", "발주처"))
    buy_nm = _extract_named_text(text, ("매입처명", "매입처"))
    real_ven_nm = _extract_named_text(text, ("실납처명", "실납처"))
    sales_man_nm = _extract_named_text(text, ("영업사원명",))

    product_group_nm = _extract_named_text(text, ("제품그룹명", "제품그룹", "그룹명"))
    product_di_nm = _extract_named_text(text, ("제품구분명", "제품구분", "구분명"))
    product_class_nm = _extract_named_text(text, ("제품분류명", "제품분류", "분류명"))

    ven_group_nm = _extract_named_text(text, ("거래처그룹명", "거래처그룹"))
    ven_kind_nm = _extract_named_text(text, ("거래처종류명", "거래처종류"))


    in_seq = _extract_code(text, "입고순번", 5)
    out_seq = _extract_code(text, "출고순번", 5)

    trans_seq = (
        _extract_code_flex(text, "거래명세서순번", 1, 6)
        or _extract_code_flex(text, "거래명세서", 1, 6)
    )

    tax_seq = (
        _extract_code_flex(text, "세금계산서순번", 1, 6)
        or _extract_code_flex(text, "세금계산서", 1, 6)
    )
    trans_di = _extract_code(text, "거래명세서구분", 1)
    tax_di = _extract_code(text, "세금계산서구분", 1)

    stock_nm = _extract_named_text(text, ("재고위치명", "재고명"))
    stock_apply_cd = _extract_code(text, "재고적용처", 5) or _extract_code(text, "재고적용처코드", 5)
    stock_apply_nm = _extract_named_text(text, ("재고적용처명", "재고적용처"))

    add_nm = _extract_named_text(text, ("등록자명", "등록자"))
    mod_nm = _extract_named_text(text, ("수정자명", "수정자"))

    stock_cds = _extract_stock_codes(text)

    # "재고위치 본사창고" 같은 이름 조건 보정
    # 단, "재고위치 000001"처럼 숫자 코드는 stock_cds로 이미 잡히므로 이름으로 중복 처리하지 않는다.
    if not stock_nm and not stock_cds:
        stock_nm = _extract_named_text(text, ("재고위치",))

    if ven_cd:
        params["ven_cd"] = ven_cd
    if ven_nm:
        params["ven_nm"] = ven_nm

    if physic_cd:
        params["physic_cd"] = physic_cd
    if physic_nm:
        params["physic_nm"] = physic_nm

    if maker_cd:
        params["maker_cd"] = maker_cd
        params["product_ven_cd"] = maker_cd
    if maker_nm:
        params["maker_nm"] = maker_nm
        params["product_ven_nm"] = maker_nm

    if order_cd:
        params["order_cd"] = order_cd
    if order_nm:
        params["order_nm"] = order_nm

    if buy_cd:
        params["buy_cd"] = buy_cd
    if buy_nm:
        params["buy_nm"] = buy_nm

    if real_ven_cd:
        params["real_ven_cd"] = real_ven_cd
    if real_ven_nm:
        params["real_ven_nm"] = real_ven_nm

    if sales_man:
        params["sales_man"] = sales_man
    if sales_man_nm:
        params["sales_man_nm"] = sales_man_nm

    if product_group_nm:
        params["product_group_nm"] = product_group_nm

    if product_di_nm:
        params["product_di_nm"] = product_di_nm

    if product_class_nm:
        params["product_class_nm"] = product_class_nm

    if ven_group_nm:
        params["ven_group_nm"] = ven_group_nm
    if ven_kind_nm:
        params["ven_kind_nm"] = ven_kind_nm

    if in_seq:
        params["in_seq"] = in_seq
    if out_seq:
        params["out_seq"] = out_seq

    if trans_di:
        params["trans_di"] = trans_di
    if tax_di:
        params["tax_di"] = tax_di

    if stock_nm:
        params["stock_nm"] = stock_nm

    if stock_apply_cd:
        params["stock_apply_cd"] = stock_apply_cd
    if stock_apply_nm:
        params["stock_apply_nm"] = stock_apply_nm


    if add_nm:
        params["add_nm"] = add_nm
    if mod_nm:
        params["mod_nm"] = mod_nm


    if trans_seq:
        params["trans_seq"] = trans_seq
    if tax_seq:
        params["tax_seq"] = tax_seq

    if stock_cds:
        params["stock_cds"] = stock_cds
        if len(stock_cds) == 1:
            params["stock_cd"] = stock_cds[0]

    for word, prefix in _IO_PREFIX_WORDS.items():
        if word in text:
            params["io_gu_prefix"] = prefix
            break

    if "거래명세서" in text:
        if (
            "매입분" in text
            or "매입만" in text
            or "입고분" in text
            or "입고만" in text
            or "입고거래명세서" in text
            or "입고 거래명세서" in text
        ):
            params.setdefault("trans_di", "1")
        elif (
            "매출분" in text
            or "매출만" in text
            or "출고분" in text
            or "출고만" in text
            or "출고거래명세서" in text
            or "출고 거래명세서" in text
        ):
            params.setdefault("trans_di", "3")
        elif "매입" in text:
            params.setdefault("trans_di", "1")
        elif "매출" in text:
            params.setdefault("trans_di", "3")


    if "세금계산서" in text:
        if "회계매입" in text:
            params.setdefault("tax_di", "2")
        elif "회계매출" in text:
            params.setdefault("tax_di", "4")
        elif "매입분" in text or "매입만" in text or "매입" in text:
            params.setdefault("tax_di", "1")
        elif "매출분" in text or "매출만" in text or "매출" in text:
            params.setdefault("tax_di", "3")
            
    # 제품수불현황용
    if _has_any(text, _PRODUCT_FLOW_WORDS):
        if "실수불" in text:
            params["stock_mode"] = "실수불"
        elif "장부수불" in text:
            params["stock_mode"] = "장부수불"

        if "조회범위 전체" in text or "전체로 조회" in text:
            params["flow_scope"] = "전체"
        elif "조회범위 매입" in text or "매입만" in text:
            params["flow_scope"] = "매입"
        elif "조회범위 매출" in text or "매출만" in text:
            params["flow_scope"] = "매출"

        if "입출고일자" in text:
            params["date_basis"] = "입출고일자"
        elif "명세서일자" in text:
            params["date_basis"] = "명세서일자"

    # 제품재고현황용
    if "실재고" in text and "월집계" not in text:
        params["stock_mode"] = "실재고"
    elif "장부재고" in text and "월집계" not in text:
        params["stock_mode"] = "장부재고"

    if (
        "재고집계기준 제조사" in text
        or "제조사로 조회" in text
        or "제조사 기준" in text
    ):
        params["group_basis"] = "제조사"
    elif (
        "재고집계기준 발주처" in text
        or "발주처로 조회" in text
        or "발주처 기준" in text
    ):
        params["group_basis"] = "발주처"
    elif (
        "재고집계기준 매입처" in text
        or "매입처로 조회" in text
        or "매입처 기준" in text
    ):
        params["group_basis"] = "매입처"

    if (
        "재고단가기준 총평균단가" in text
        or "총평균단가로 조회" in text
        or "총평균단가 기준" in text
    ):
        params["price_mode"] = "총평균단가"
    elif (
        "재고단가기준 최종매입가" in text
        or "최종매입가로 조회" in text
        or "최종매입가 기준" in text
    ):
        params["price_mode"] = "최종매입가"
    elif (
        "재고단가기준 기준가" in text
        or "기준가로 조회" in text
        or "기준가 기준" in text
    ):
        params["price_mode"] = "기준가"
    elif (
        "재고단가기준 현보험약가" in text
        or "현보험약가로 조회" in text
        or "현보험약가 기준" in text
    ):
        params["price_mode"] = "현보험약가"

    return params

# Current-user text is the sole source for outbound validation intent.  In
# particular, retrieved RAG context and an already-resolved action must never
# add validation work to a detail query.
_OUTBOUND_DETAIL_ACTIONS = {
    "출고명세 조회",
    "출고↔거래명세서 검증",
    "출고↔세금계산서 검증",
}
_IO_VALIDATION_EXPLANATION_WORDS = (
    "무슨뜻",
    "방법",
    "설명",
    "원인",
    "문서",
    "rag",
    "사용법",
    "왜",
    "이유",
    "검색",
    "자료",
)


def _is_structured_outbound_validation_request(raw: str) -> bool:
    # This comparison is intentionally limited to validation intent. The
    # general parser still receives its original normalized text.
    normalized = re.sub(r"\s+", "", _norm(raw))
    if not ("출고명세" in normalized and any(word in normalized for word in ("검증", "불일치"))):
        return False
    return not is_io_validation_explanation_request(normalized)


def is_io_validation_explanation_request(raw: str) -> bool:
    """Return whether a transaction/tax question has explanation or RAG intent."""
    normalized = re.sub(r"\s+", "", _norm(raw))
    if not any(document in normalized for document in ("거래명세서", "세금계산서")):
        return False
    lowered = normalized.lower()
    return any(word in lowered for word in _IO_VALIDATION_EXPLANATION_WORDS)


def is_outbound_validation_explanation_request(raw: str) -> bool:
    """Backward-compatible alias for the shared IO validation explanation guard."""
    return is_io_validation_explanation_request(raw)


def _apply_outbound_validation_intent(
    params: Dict[str, Any],
    *,
    action: str,
    raw: str,
) -> Dict[str, Any]:
    """Attach outbound validation flags after the structured action is known."""
    result = dict(params or {})
    if action not in _OUTBOUND_DETAIL_ACTIONS:
        return result

    for key in (
        "validation_requested",
        "validation_trans",
        "validation_tax",
        "only_mismatch",
        "only_mismatch_trans",
        "only_mismatch_tax",
        "validation_intent_source",
    ):
        result.pop(key, None)

    result.update(
        {
            "validation_requested": False,
            "validation_trans": False,
            "validation_tax": False,
        }
    )
    if not _is_structured_outbound_validation_request(raw):
        return result

    normalized = re.sub(r"\s+", "", _norm(raw))
    has_transaction = "거래명세서" in normalized
    has_tax = "세금계산서" in normalized
    if not has_transaction and not has_tax:
        # A generic outbound validation has no existing single-side contract.
        # Validate both correlations rather than guessing one of them.
        has_transaction = True
        has_tax = True

    result.update(
        {
            "validation_requested": True,
            "validation_trans": has_transaction,
            "validation_tax": has_tax,
            "validation_intent_source": "user_text",
        }
    )
    if "불일치" in normalized:
        result["only_mismatch"] = "Y"
        if has_transaction:
            result["only_mismatch_trans"] = "Y"
        if has_tax:
            result["only_mismatch_tax"] = "Y"
    return result


# 입출고/재고 NLQ 해석의 최상위 함수
# 입출고/재고 관련 신호가 있는지 보고, 관련 신호가 있으면 extract_params()로 파싱한 뒤
# 입출고/재고 NLQ 의도를 판정한다.
def resolve_io_nlq(text: str) -> Optional[Dict[str, Any]]:
    raw = _norm(text)
    if not raw:
        return None

    # Explanation/help questions must continue to the normal answer or RAG
    # route.  They are not structured requests for an outbound DB result.
    if is_io_validation_explanation_request(raw):
        return None

    params = extract_params(raw)

    def _result(action: str, params_in: Dict[str, Any]) -> Dict[str, Any]:
        fixed_params = _normalize_io_detail_vendor_params(
            params_in,
            action=action,
            raw=raw,
        )
        fixed_params = sanitize_io_named_condition_values(
            fixed_params,
            action=action,
        )
        fixed_params = _apply_outbound_validation_intent(
            fixed_params,
            action=action,
            raw=raw,
        )
        return {"action": action, "params": fixed_params}

    # 입고/출고/명세서/세금계산서 일자 기준 조회에서도
    # "2026", "2024~2026", "202605" 같은 기간 표현을 date_from/date_to로 보정한다.
    if (
        not _has_any(raw, _PRODUCT_IO_WORDS)
        and "월집계" not in raw
        and any(k in raw for k in ("입고명세", "출고명세", "거래명세서", "세금계산서", "입고", "출고", "매입", "매출"))
    ):
        params = _apply_date_params_for_io_detail(params, raw)

    if _has_any(raw, _PRODUCT_FLOW_WORDS):

        params = _apply_date_params_for_product_flow(params, raw)

    is_common_doc_query = "공통" in raw
    is_check_query = ("불일치" in raw or "검증" in raw or "누락" in raw)

    if not is_common_doc_query and is_check_query:
        if ("입고" in raw or "매입" in raw) and "거래명세서" in raw:
            check_params = _ensure_default_date_range_for_heavy_check(params)
            return _result(
                "입고↔거래명세서 검증",
                {**check_params, "only_mismatch_trans": "Y"},
            )

        if ("입고" in raw or "매입" in raw) and "세금계산서" in raw:
            check_params = _ensure_default_date_range_for_heavy_check(params)
            return _result(
                "입고↔세금계산서 검증",
                {**check_params, "only_mismatch_tax": "Y"},
            )

        if (
            ("출고" in raw or "매출" in raw)
            and "거래명세서" in raw
            and "세금계산서" in raw
            and _is_structured_outbound_validation_request(raw)
        ):
            check_params = _ensure_default_date_range_for_heavy_check(params)
            return _result("출고명세 조회", check_params)

        if (
            ("출고" in raw or "매출" in raw)
            and "거래명세서" in raw
            and _is_structured_outbound_validation_request(raw)
        ):
            check_params = _ensure_default_date_range_for_heavy_check(params)
            return _result(
                "출고↔거래명세서 검증",
                check_params,
            )

        if (
            ("출고" in raw or "매출" in raw)
            and "세금계산서" in raw
            and _is_structured_outbound_validation_request(raw)
        ):
            check_params = _ensure_default_date_range_for_heavy_check(params)
            return _result(
                "출고↔세금계산서 검증",
                check_params,
            )

        if (
            ("출고" in raw or "매출" in raw)
            and "출고명세" in raw
            and _is_structured_outbound_validation_request(raw)
        ):
            check_params = _ensure_default_date_range_for_heavy_check(params)
            return _result("출고명세 조회", check_params)

    # 월집계는 제품수불/제품재고보다 먼저 판정한다.
    # 예: "실재고월집계 2025 제품 00029 조회"
    if "실재고" in raw and "월집계" in raw:
        stock_params = _apply_month_params_for_monthly_stock(params, raw)
        return _result("실재고월집계 조회", stock_params)

    if "장부재고" in raw and "월집계" in raw:
        stock_params = _apply_month_params_for_monthly_stock(params, raw)
        return _result("장부재고월집계 조회", stock_params)


    if _has_any(raw, _PRODUCT_FLOW_WORDS):
        return _result("제품수불현황 조회", params)

    if _has_any(raw, _PRODUCT_INVENTORY_WORDS):
        params = _apply_date_params_for_product_inventory(params, raw)
        return _result("제품재고현황 조회", params)

    # 명시적으로 입고명세 / 출고명세를 말한 경우에는
    # 거래명세서순번 / 세금계산서순번이 들어 있어도 상세 조회를 우선한다.
    if "출고명세" in raw:
        return _result("출고명세 조회", params)

    if "입고명세" in raw:
        return _result("입고명세 조회", params)

    # 공통 조회는 '공통'을 명시했을 때 우선
    if "거래명세서 공통" in raw:
        return _result("거래명세서 공통 조회", params)

    if "세금계산서 공통" in raw:
        return _result("세금계산서 공통 조회", params)

    # 상세 명시가 없을 때만 공통 조회로 해석
    if "거래명세서" in raw and "입고명세" not in raw and "출고명세" not in raw:
        return _result("거래명세서 공통 조회", params)

    if "세금계산서" in raw and "입고명세" not in raw and "출고명세" not in raw:
        return _result("세금계산서 공통 조회", params)
    
    txn_signal = _has_transaction_signal(raw)

    # 거래처 마스터 의도가 분명하면 io_nlq가 가로채지 않는다.
    # 예:
    # - "한미 매입처에서 거래처 조회 해줘" -> None (vendors NLQ)
    # - "거래처 마스터에서 대표자명 김 조회" -> None (vendors NLQ)
    # - "수정일자 20250714 거래처 조회" -> None (vendors NLQ)
    if _looks_vendor_master_query(raw):
        return None

    # 명시 액션(입고명세/출고명세)이 있으면 거래축 조건어보다 우선한다.
    if "입고" in raw or ("매입" in raw and "매입처" not in raw):
        return _result("입고명세 조회", params)

    if "출고" in raw or ("매출" in raw and "매출처" not in raw):
        return _result("출고명세 조회", params)
    
    # 거래처 속성어 + 거래/집계 신호는 거래축 조건어로 해석
    # 예:
    # - "한미 매입처에서 거래처 내역 조회 해줘" -> 입고명세 조회
    # - "동아 제조사 거래처 내역 조회"       -> 입고명세 조회
    # - "백합 발주처 거래내역 조회"          -> 입고명세 조회
    # - "한미 매출처 거래내역 보여줘"        -> 출고명세 조회
    if txn_signal and any(w in raw for w in _PURCHASE_SIDE_VENDOR_WORDS):
        return _result("입고명세 조회", params)

    if txn_signal and any(w in raw for w in _SALES_SIDE_VENDOR_WORDS):
        return _result("출고명세 조회", params)
    

    # 일반 fallback
    # - "매입처", "매출처" 단독은 입고/출고 의미로 보지 않는다.

    if "출고" in raw:
        return _result("출고명세 조회", params)

    if "입고" in raw:
        return _result("입고명세 조회", params)

        return None
