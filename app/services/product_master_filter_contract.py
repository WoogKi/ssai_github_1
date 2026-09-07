"""Shared product-master filter contract for product and ERP-table queries."""

from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import Any, Mapping, MutableSequence


PRODUCT_FILTER_KEYS = (
    "physic_cd", "product_keyword", "insu_cd", "barcode", "maker_nm",
    "product_group_nm", "product_di_nm", "product_di_semantic_group", "product_class_nm",
    "product_unit_price", "product_final_price_date", "product_only_use",
    "product_add_user_nm", "product_add_date_from", "product_add_date_to",
    "product_mod_user_nm", "product_mod_date_from", "product_mod_date_to",
)

PRODUCT_DI_SEMANTIC_GROUP_TERMS: dict[str, str] = {
    "전문의약품": "insurance",
    "전문약": "insurance",
    "전문제품": "insurance",
    "보험약": "insurance",
    "보험제품": "insurance",
    "ETC": "insurance",
    "일반의약품": "non_insurance",
    "일반약": "non_insurance",
    "일반제품": "non_insurance",
    "비보험약": "non_insurance",
    "비보험제품": "non_insurance",
    "OTC": "non_insurance",
}


def extract_product_di_semantic_group(text: Any) -> str:
    """Resolve an explicit upper product group without changing name-LIKE filters."""
    source = _text(text)
    for label, group in sorted(
        PRODUCT_DI_SEMANTIC_GROUP_TERMS.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if re.search(
            rf"(?:^|\s){re.escape(label)}(?=\s|$)",
            source,
            flags=re.IGNORECASE,
        ):
            return group
    return ""


def build_product_master_enrichment_sql(
    *,
    product_alias: str,
    aliases: Mapping[str, str] | None = None,
    include_audit_price: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Build the canonical R040 name/audit/price joins for another table query."""
    names = {
        "maker": "PV", "group": "PG", "di": "PD", "class": "PC",
        "add_user": "PAU", "mod_user": "PMU", "price": "PMCALC",
    }
    names.update(dict(aliases or {}))
    p = product_alias
    joins = f"""
LEFT JOIN dbo.Rddbc030 AS {names['maker']} WITH (NOLOCK)
    ON {p}.Rd04_Ven_Cd = {names['maker']}.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc010 AS {names['group']} WITH (NOLOCK)
    ON {p}.Rd04_Physic_Group_Gcode = {names['group']}.Rd01_Gcode
   AND {p}.Rd04_Physic_Group = {names['group']}.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS {names['di']} WITH (NOLOCK)
    ON {p}.Rd04_Physic_Di_Gcode = {names['di']}.Rd01_Gcode
   AND {p}.Rd04_Physic_Di = {names['di']}.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS {names['class']} WITH (NOLOCK)
    ON {p}.Rd04_Physic_Gu_Gcode = {names['class']}.Rd01_Gcode
   AND {p}.Rd04_Physic_Gu = {names['class']}.Rd01_Tcode
""".strip()
    if include_audit_price:
        joins = f"""
{joins}
LEFT JOIN dbo.Rddbc060 AS {names['add_user']} WITH (NOLOCK)
    ON {p}.Rd04_Add_Cd = {names['add_user']}.Rd06_User_Cd
LEFT JOIN dbo.Rddbc060 AS {names['mod_user']} WITH (NOLOCK)
    ON {p}.Rd04_Mod_Cd = {names['mod_user']}.Rd06_User_Cd
CROSS APPLY (
    SELECT
        CASE
            WHEN ISNULL(NULLIF(LTRIM(RTRIM({p}.Rd04_Insu_Date)), ''), '00000000')
               >= ISNULL(NULLIF(LTRIM(RTRIM({p}.Rd04_Before_Insu_Date)), ''), '00000000')
            THEN ISNULL(NULLIF(LTRIM(RTRIM({p}.Rd04_Insu_Date)), ''), '00000000')
            ELSE ISNULL(NULLIF(LTRIM(RTRIM({p}.Rd04_Before_Insu_Date)), ''), '00000000')
        END AS final_price_date,
        CASE
            WHEN ISNULL(NULLIF(LTRIM(RTRIM({p}.Rd04_Insu_Date)), ''), '00000000')
               >= ISNULL(NULLIF(LTRIM(RTRIM({p}.Rd04_Before_Insu_Date)), ''), '00000000')
            THEN ISNULL({p}.Rd04_Insu_Price, 0) * ISNULL({p}.Rd04_Acc_Unit, 0)
            ELSE ISNULL({p}.Rd04_Before_Insu_Price, 0) * ISNULL({p}.Rd04_Acc_Unit, 0)
        END AS unit_price
) AS {names['price']}
""".strip()
    expressions: dict[str, Any] = {
        "use_gu": f"{p}.Rd04_Use_Gu",
        "physic_cd": f"{p}.Rd04_Physic_Cd",
        "insu_cd": f"{p}.Rd04_Insu_Cd",
        "barcodes": tuple(f"{p}.Rd04_Bar_Code{index}" for index in range(1, 6)),
        "maker_nm": f"{names['maker']}.Rd03_Ven_Nm",
        "product_group_nm": f"{names['group']}.Rd01_Hnm",
        "product_di_nm": f"{names['di']}.Rd01_Hnm",
        "product_di_cd": f"{p}.Rd04_Physic_Di",
        "product_class_nm": f"{names['class']}.Rd01_Hnm",
        "product_add_user_nm": f"{names['add_user']}.Rd06_User_Nm" if include_audit_price else "",
        "product_add_date": f"{p}.Rd04_Add_Date" if include_audit_price else "",
        "product_mod_user_nm": f"{names['mod_user']}.Rd06_User_Nm" if include_audit_price else "",
        "product_mod_date": f"{p}.Rd04_Mod_Date" if include_audit_price else "",
        "product_unit_price": f"{names['price']}.unit_price" if include_audit_price else "",
        "product_final_price_date": f"{names['price']}.final_price_date" if include_audit_price else "",
        "keyword": (f"{p}.Rd04_Physic_Nm", f"{p}.Rd04_Physic_Cd", f"{p}.Rd04_Insu_Cd"),
    }
    return joins, expressions


def _text(value: Any) -> str:
    return str(value or "").strip()


def normalize_product_master_filters(values: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(values or {})
    out = {key: _text(source.get(key)) for key in PRODUCT_FILTER_KEYS if key != "product_only_use"}
    out["product_only_use"] = bool(source.get("product_only_use", False))
    semantic_group = out.get("product_di_semantic_group", "")
    if semantic_group and semantic_group not in {"insurance", "non_insurance"}:
        raise ValueError("지원하지 않는 상위 제품구분입니다.")
    return out


def _date_bound(value: Any, *, upper: bool) -> str:
    digits = "".join(ch for ch in _text(value) if ch.isdigit())
    if len(digits) == 8:
        return digits
    if len(digits) != 6:
        return ""
    first = datetime.strptime(digits + "01", "%Y%m%d")
    if not upper:
        return first.strftime("%Y%m%d")
    if first.month == 12:
        next_first = first.replace(year=first.year + 1, month=1)
    else:
        next_first = first.replace(month=first.month + 1)
    return (next_first - timedelta(days=1)).strftime("%Y%m%d")


def _number_range(value: Any) -> tuple[float | None, float | None]:
    text = _text(value).replace(",", "")
    if not text:
        return None, None
    match = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(?:~|-)\s*([0-9]+(?:\.[0-9]+)?)$", text)
    if match:
        return float(match.group(1)), float(match.group(2))
    try:
        number = float(text)
    except ValueError:
        return None, None
    return number, number


def append_product_master_filter_clauses(
    clauses: MutableSequence[str],
    bind_values: MutableSequence[Any],
    filters: Mapping[str, Any] | None,
    *,
    expressions: Mapping[str, Any],
) -> None:
    """Append the canonical R040 product filters using caller-owned SQL expressions."""
    values = normalize_product_master_filters(filters)

    def add_like(key: str, expression_key: str) -> None:
        value = _text(values.get(key))
        expression = _text(expressions.get(expression_key))
        if value and expression:
            clauses.append(f"{expression} LIKE ?")
            bind_values.append(f"%{value}%")

    if values["product_only_use"] and expressions.get("use_gu"):
        clauses.append(f"{expressions['use_gu']} = ?")
        bind_values.append("0")
    if values["physic_cd"] and expressions.get("physic_cd"):
        clauses.append(f"{expressions['physic_cd']} = ?")
        bind_values.append(values["physic_cd"])
    if values["insu_cd"] and expressions.get("insu_cd"):
        operator = "=" if len(values["insu_cd"]) >= 10 else "LIKE"
        clauses.append(f"{expressions['insu_cd']} {operator} ?")
        bind_values.append(values["insu_cd"] if operator == "=" else f"%{values['insu_cd']}%")
    if values["barcode"]:
        barcode_expressions = tuple(expressions.get("barcodes") or ())
        if barcode_expressions:
            clauses.append("(" + " OR ".join(f"{expr} = ?" for expr in barcode_expressions) + ")")
            bind_values.extend([values["barcode"]] * len(barcode_expressions))

    add_like("maker_nm", "maker_nm")
    add_like("product_group_nm", "product_group_nm")
    add_like("product_di_nm", "product_di_nm")
    semantic_group = values["product_di_semantic_group"]
    product_di_cd = _text(expressions.get("product_di_cd"))
    if semantic_group and product_di_cd:
        numeric_product_di = (
            "CASE WHEN LTRIM(RTRIM(ISNULL(" + product_di_cd + ", ''))) <> '' "
            "AND LTRIM(RTRIM(ISNULL(" + product_di_cd + ", ''))) NOT LIKE '%[^0-9]%' "
            "THEN CAST(LTRIM(RTRIM(" + product_di_cd + ")) AS INT) END"
        )
        clauses.append(f"{numeric_product_di} {'<' if semantic_group == 'insurance' else '>='} ?")
        bind_values.append(5)
    add_like("product_class_nm", "product_class_nm")
    add_like("product_add_user_nm", "product_add_user_nm")
    add_like("product_mod_user_nm", "product_mod_user_nm")

    for key, expression_key, upper in (
        ("product_add_date_from", "product_add_date", False),
        ("product_add_date_to", "product_add_date", True),
        ("product_mod_date_from", "product_mod_date", False),
        ("product_mod_date_to", "product_mod_date", True),
    ):
        bound = _date_bound(values[key], upper=upper)
        expression = _text(expressions.get(expression_key))
        if bound and expression:
            clauses.append(f"{expression} {'<=' if upper else '>='} ?")
            bind_values.append(bound)

    price_from, price_to = _number_range(values["product_unit_price"])
    price_expression = _text(expressions.get("product_unit_price"))
    if price_expression and price_from is not None:
        clauses.append(f"{price_expression} >= ?")
        bind_values.append(price_from)
    if price_expression and price_to is not None:
        clauses.append(f"{price_expression} <= ?")
        bind_values.append(price_to)

    final_parts = re.split(r"\s*(?:~|-)\s*", values["product_final_price_date"], maxsplit=1)
    final_from = _date_bound(final_parts[0] if final_parts else "", upper=False)
    final_to = _date_bound(final_parts[1] if len(final_parts) > 1 else (final_parts[0] if final_parts else ""), upper=True)
    final_expression = _text(expressions.get("product_final_price_date"))
    if final_expression and final_from:
        clauses.append(f"{final_expression} >= ?")
        bind_values.append(final_from)
    if final_expression and final_to:
        clauses.append(f"{final_expression} <= ?")
        bind_values.append(final_to)

    keyword = values["product_keyword"]
    keyword_expressions = tuple(expressions.get("keyword") or ())
    if keyword and keyword_expressions:
        clauses.append("(" + " OR ".join(f"{expr} LIKE ?" for expr in keyword_expressions) + ")")
        bind_values.extend([f"%{keyword}%"] * len(keyword_expressions))
