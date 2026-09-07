"""Deterministic NLQ parsing for registry-backed ERP table features."""

from __future__ import annotations

from datetime import date, datetime
import re
from typing import Any, Optional

from app.sims.meta.erp_table_feature_registry import filter_labels, match_action_in_text
from app.services.product_master_filter_contract import extract_product_di_semantic_group


_ACTION_WORDS = (
    "제품별 최종 매입단가 조회", "최종 매입단가 조회", "최종매입단가조회",
    "최종 매입가 조회", "최종매입가조회", "최종 매입가", "최종매입가",
    "실입고단가 조회", "실입고단가조회", "장부입고단가 조회", "장부입고단가조회",
    "기준일 최종 계약단가 조회", "기준일 현재 계약단가 조회",
    "계약단가 이력 조회", "계약단가이력조회", "계약 변경내역", "계약변경내역",
    "최종 계약단가 조회", "최종계약단가조회", "현재 계약단가 조회", "현재계약단가조회",
    "단가적용 계약단가 조회", "계약단가 조회", "계약 단가 조회", "단가계약 조회",
    "단가 계약 조회", "과거 계약단가", "과거계약단가", "계약단가", "계약 단가", "단가계약",
)
_VALUE_BOUNDARY = (
    r"(?=\s+(?:매입처(?:코드|명)?|재고위치(?:코드)?|재고적용처(?:코드|명)?|재고적용코드|"
    r"단가적용거래처(?:코드|명)?|단가적용처(?:코드|명)?|단가적용코드|거래처(?:코드|명)?|"
    r"제품(?:코드|명|키워드|그룹명?|구분명?|분류명?|단가|등록자|수정자)?|"
    r"품목(?:코드|명|키워드)?|보험코드|바코드|제약사명?|제조사|키워드|"
    r"실입고단가|장부입고단가|입고수량|출고수량|최종단가변경일자|"
    r"제품등록일|제품수정일|계약시작일(?:자)?|기준일|조회건수|TOP)\b|$)"
)
_HISTORY_WORDS = ("이력", "과거", "변경내역")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _yyyymmdd(value: str) -> str:
    digits = re.sub(r"[^0-9]", "", _clean(value))
    if len(digits) != 8:
        return ""
    try:
        datetime.strptime(digits, "%Y%m%d")
    except ValueError:
        return ""
    return digits


def _extract_code(text: str, labels: tuple[str, ...]) -> str:
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(rf"(?:{label_pattern})\s*[:=]?\s*([A-Za-z0-9]{{1,15}})", text)
    return _clean(match.group(1)) if match else ""


def _extract_name(text: str, labels: tuple[str, ...]) -> str:
    generic_labels = {
        "거래처", "매입처", "재고적용처", "단가적용처", "단가적용거래처",
        "제품", "품목",
    }
    label_pattern = "|".join(
        rf"(?<![0-9A-Za-z가-힣]){re.escape(label)}(?!코드|명|키워드|그룹|구분|분류|단가|등록|수정|별)"
        if label in generic_labels else re.escape(label)
        for label in labels
    )
    match = re.search(rf"(?:{label_pattern})\s*[:=]?\s*(.+?){_VALUE_BOUNDARY}", text)
    if not match:
        return ""
    value = _clean(match.group(1))
    for action_word in _ACTION_WORDS:
        value = value.replace(action_word, " ")
    value = re.sub(r"\s+(?:조회|검색|확인|보여줘|알려줘)\s*$", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _extract_unlabeled_product_prefix(text: str, *, explicit_labels: tuple[str, ...]) -> str:
    """Read only the unlabeled subject before an R070 action as a product name."""
    action_starts = [text.find(word) for word in _ACTION_WORDS if text.find(word) >= 0]
    if not action_starts:
        return ""
    prefix = _clean(text[: min(action_starts)])
    if not prefix:
        return ""
    for word in _HISTORY_WORDS:
        prefix = prefix.replace(word, " ")
    prefix = re.sub(r"\b(?:조회|검색|확인|보여줘|알려줘)\b", " ", prefix)
    prefix = re.sub(r"\s+", " ", prefix).strip()
    if any(label and prefix.startswith(label) for label in explicit_labels):
        return ""
    if not prefix or re.search(r"(?:19|20)\d{2}|\b(?:TOP|조회건수)\b", prefix, re.IGNORECASE):
        return ""
    return prefix


def _extract_date_range(text: str, labels: tuple[str, ...]) -> tuple[str, str]:
    if not labels:
        return "", ""
    pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?:{pattern})\s*[:=]?\s*((?:19|20)\d{{2}}[-./]?\d{{2}}[-./]?\d{{2}})"
        rf"(?:\s*(?:~|부터|-)\s*((?:19|20)\d{{2}}[-./]?\d{{2}}[-./]?\d{{2}}))?",
        text,
    )
    if not match:
        return "", ""
    first = _yyyymmdd(match.group(1))
    second = _yyyymmdd(match.group(2) or match.group(1))
    return first, second


def _extract_explicit_contract_period(
    text: str,
    *,
    infer_calendar_period: bool,
) -> tuple[str, str]:
    """Return only a user-written contract-start period; never invent a range."""
    labeled = re.search(
        r"계약시작일(?:자)?\s*[:=]?\s*((?:19|20)\d{2}[-./]?\d{2}[-./]?\d{2})"
        r"\s*(?:~|부터|-)\s*((?:19|20)\d{2}[-./]?\d{2}[-./]?\d{2})",
        text,
    )
    if labeled:
        return _yyyymmdd(labeled.group(1)), _yyyymmdd(labeled.group(2))

    if not infer_calendar_period or "기준일" in text:
        return "", ""

    year_month = re.search(r"((?:19|20)\d{2})\s*년\s*(1[0-2]|0?[1-9])\s*월", text)
    if year_month:
        year, month = int(year_month.group(1)), int(year_month.group(2))
        if month == 12:
            next_year, next_month = year + 1, 1
        else:
            next_year, next_month = year, month + 1
        from datetime import timedelta

        month_end = date(next_year, next_month, 1) - timedelta(days=1)
        return f"{year:04d}{month:02d}01", month_end.strftime("%Y%m%d")

    year_only = re.search(r"((?:19|20)\d{2})\s*년", text)
    if year_only:
        year = year_only.group(1)
        return f"{year}0101", f"{year}1231"
    return "", ""


def _extract_numeric_filter(text: str, labels: tuple[str, ...]) -> str:
    if not labels:
        return ""
    pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?:{pattern})\s*[:=]?\s*(-?[0-9,]+(?:\.[0-9]+)?(?:\s*(?:~|-)\s*-?[0-9,]+(?:\.[0-9]+)?)?)",
        text,
    )
    return _clean(match.group(1)) if match else ""


def _resolve_rddbc230_nlq(
    raw: str,
    feature: Any,
    action_spec: Any,
) -> dict[str, Any]:
    params: dict[str, Any] = {"mode": action_spec.mode, "_display_context": "chat"}
    labels = lambda key: filter_labels(feature, key)

    for key in ("physic_cd", "buy_cd", "stock_cd", "stock_apply_cd", "cost_apply_cd"):
        value = _extract_code(raw, labels(key))
        if value:
            params[key] = value
    for key in ("physic_nm", "buy_nm", "stock_nm", "stock_apply_nm", "cost_apply_nm"):
        value = _extract_name(raw, labels(key))
        if value:
            params[key] = value

    product_di_nm = _extract_name(raw, labels("product_di_nm"))
    product_di_semantic_group = (
        "" if product_di_nm else extract_product_di_semantic_group(raw)
    )
    if product_di_nm:
        params["product_di_nm"] = product_di_nm
    if product_di_semantic_group:
        params["product_di_semantic_group"] = product_di_semantic_group

    if (
        not params.get("physic_cd")
        and not params.get("physic_nm")
        and not product_di_semantic_group
    ):
        explicit_labels = tuple(
            dict.fromkeys(
                label
                for filter_spec in feature.filters
                for label in (filter_spec.label, *filter_spec.aliases)
            )
        )
        physic_nm = _extract_unlabeled_product_prefix(raw, explicit_labels=explicit_labels)
        if physic_nm:
            params["physic_nm"] = physic_nm

    for key in ("insu_cd", "barcode"):
        value = _extract_code(raw, labels(key))
        if value:
            params[key] = value
    for key in (
        "product_keyword", "maker_nm", "product_group_nm",
        "product_class_nm",
    ):
        value = _extract_name(raw, labels(key))
        if value:
            params[key] = value
    for key in ("unit_cost", "fin_unit_cost", "in_quantity", "out_quantity"):
        value = _extract_numeric_filter(raw, labels(key))
        if value:
            params[key] = value

    top_match = re.search(r"(?:TOP|조회건수)\s*(\d{1,6})", raw, flags=re.IGNORECASE)
    if top_match:
        params["display_top"] = int(top_match.group(1))
    return {"action": action_spec.action, "params": params}


def resolve_registered_erp_table_nlq(
    text: str,
    *,
    today: date | None = None,
) -> Optional[dict[str, Any]]:
    matched = match_action_in_text(text)
    if matched is None:
        return None
    feature, action_spec = matched
    raw = re.sub(r"\s+", " ", _clean(text))
    if feature.table_key == "rddbc230":
        return _resolve_rddbc230_nlq(raw, feature, action_spec)
    if feature.table_key != "rddbc070":
        return None

    params: dict[str, Any] = {"mode": action_spec.mode, "_display_context": "chat"}

    labels = lambda key: filter_labels(feature, key)
    ven_cd = _extract_code(raw, labels("ven_cd"))
    ven_nm = _extract_name(raw, labels("ven_nm"))
    physic_cd = _extract_code(raw, labels("physic_cd"))
    physic_nm = _extract_name(raw, labels("physic_nm"))
    product_di_nm = _extract_name(raw, labels("product_di_nm"))
    product_di_semantic_group = (
        "" if product_di_nm else extract_product_di_semantic_group(raw)
    )
    if not physic_cd and not physic_nm and not product_di_semantic_group:
        explicit_labels = tuple(
            dict.fromkeys(
                label
                for filter_spec in feature.filters
                for label in (filter_spec.label, *filter_spec.aliases)
            )
        )
        physic_nm = _extract_unlabeled_product_prefix(
            raw,
            explicit_labels=explicit_labels,
        )

    if ven_cd:
        params["ven_cd"] = ven_cd
    if ven_nm and ven_nm != ven_cd:
        params["ven_nm"] = ven_nm
    if physic_cd:
        params["physic_cd"] = physic_cd
    if physic_nm and physic_nm != physic_cd:
        params["physic_nm"] = physic_nm
    if product_di_nm:
        params["product_di_nm"] = product_di_nm
    if product_di_semantic_group:
        params["product_di_semantic_group"] = product_di_semantic_group

    for key in ("insu_cd", "barcode"):
        value = _extract_code(raw, labels(key))
        if value:
            params[key] = value
    for key in (
        "product_keyword", "maker_nm", "product_group_nm",
        "product_class_nm", "product_add_user_nm", "product_mod_user_nm",
    ):
        value = _extract_name(raw, labels(key))
        if value:
            params[key] = value
    price_match = re.search(
        r"(?:제품마스터\s*단가|제품단가)\s*[:=]?\s*([0-9,]+(?:\.\d+)?(?:\s*(?:~|-)\s*[0-9,]+(?:\.\d+)?)?)",
        raw,
    )
    if price_match:
        params["product_unit_price"] = _clean(price_match.group(1))
    if any(label in raw for label in labels("product_only_use")):
        params["product_only_use"] = True

    final_from, final_to = _extract_date_range(raw, labels("product_final_price_date"))
    if final_from:
        params["product_final_price_date"] = (
            final_from if final_from == final_to else f"{final_from}~{final_to}"
        )
    for prefix, from_key, to_key in (
        ("product_add_date", "product_add_date_from", "product_add_date_to"),
        ("product_mod_date", "product_mod_date_from", "product_mod_date_to"),
    ):
        date_labels = tuple(dict.fromkeys((*labels(from_key), *labels(to_key))))
        value_from, value_to = _extract_date_range(raw, date_labels)
        if value_from:
            params[from_key] = value_from
            params[to_key] = value_to

    period_from, period_to = _extract_explicit_contract_period(
        raw,
        infer_calendar_period=action_spec.mode == "history",
    )
    if period_from:
        params["date_from"] = period_from
        params["date_to"] = period_to

    as_of_match = re.search(
        r"기준일\s*[:=]?\s*((?:19|20)\d{2}[-./]?\d{2}[-./]?\d{2})",
        raw,
    )
    if as_of_match:
        params["as_of"] = _yyyymmdd(as_of_match.group(1))
    elif action_spec.mode == "current":
        params["as_of"] = (today or date.today()).strftime("%Y%m%d")

    top_match = re.search(r"(?:TOP|조회건수)\s*(\d{1,6})", raw, flags=re.IGNORECASE)
    if top_match:
        params["display_top"] = int(top_match.group(1))

    return {"action": action_spec.action, "params": params}
