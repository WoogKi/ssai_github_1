"""Shared product-master filter contract for product and ERP-table queries."""

from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import Any, Mapping, MutableSequence


PRODUCT_FILTER_KEYS = (
    "physic_cd", "product_keyword", "insu_cd", "barcode", "maker_nm", "order_nm",
    "maker_manager_nm", "order_vendor_manager_nm",
    "product_group_nm", "product_di_nm", "product_di_semantic_group",
    "product_prescription_semantic", "product_class_nm",
    "product_unit_price", "product_final_price_date", "product_only_use",
    "product_add_user_nm", "product_add_date_from", "product_add_date_to",
    "product_mod_user_nm", "product_mod_date_from", "product_mod_date_to",
)

_PRODUCT_MANAGER_LABEL = re.compile(
    r"(?<![0-9A-Za-z가-힣])(?P<label>제약사?\s*담당자|발주처?\s*담당자)"
    r"\s*[:=]?\s*(?P<value>(?!(?:조회|검색|보여줘|알려줘|해줘)(?:\s|$))[^\s,?.!]+)"
)
_PRODUCT_MANAGER_ROLE = re.compile(
    r"(?<![0-9A-Za-z가-힣])(?:제약사?\s*담당자|발주처?\s*담당자)"
)


def extract_product_manager_filters(text: Any) -> tuple[dict[str, str], str]:
    """Consume explicit product-master staff conditions before vendor-name parsing."""
    source = _text(text)
    filters: dict[str, str] = {}
    for match in reversed(list(_PRODUCT_MANAGER_LABEL.finditer(source))):
        key = "maker_manager_nm" if match.group("label").startswith("제약") else "order_vendor_manager_nm"
        filters[key] = match.group("value")
        source = source[:match.start()] + " " + source[match.end():]
    if _PRODUCT_MANAGER_ROLE.search(source):
        filters["_product_manager_condition_invalid"] = "1"
    return filters, re.sub(r"\s+", " ", source).strip()

PRODUCT_DI_SEMANTIC_GROUP_TERMS: dict[str, str] = {
    "보험약": "insurance",
    "보험제품": "insurance_product",
    "비보험약": "non_insurance_drug",
    "비보험제품": "non_insurance",
    "보험": "insurance_product",
    "비보험": "non_insurance",
}

PRODUCT_PRESCRIPTION_SEMANTIC_TERMS: dict[str, str] = {
    "전문의약품": "prescription",
    "전문약품": "prescription",
    "전문약": "prescription",
    "전문제품": "prescription",
    "ETC": "prescription",
    "일반의약품": "otc",
    "일반약품": "otc",
    "일반약": "otc",
    "일반제품": "otc",
    "OTC": "otc",
}

PRODUCT_PRESCRIPTION_SEMANTICS = frozenset(("prescription", "otc"))
PRODUCT_DI_SEMANTICS = frozenset(("insurance", "insurance_product", "non_insurance", "non_insurance_drug"))
DRUG_PRODUCT_DI_CODES = frozenset(("1", "2", "3", "5", "6", "7"))
PRESCRIPTION_PRODUCT_DI_CODES = frozenset(("2", "3", "6", "7"))
OTC_PRODUCT_DI_CODES = frozenset(("1", "5"))
INSURANCE_PRODUCT_DI_CODES = frozenset(("1", "2", "3"))
INSURANCE_PRODUCT_SCOPE_DI_CODES = frozenset(("0", "1", "2", "3", "4"))
NON_INSURANCE_PRODUCT_DI_CODES = frozenset(("5", "6", "7"))

_PRODUCT_DI_SEMANTIC_OWNER_LABELS = (
    "제품명", "품목명", "상품명", "제품그룹명", "제품그룹", "제품분류명", "제품분류",
    "제품구분명", "제품구분", "구분명", "구분",
)

_PRODUCT_PRESCRIPTION_PARTICLE_PATTERN = r"(?:으로|은|는|이|가|을|를|의|도|만|과|와|로)"


def _semantic_term_has_explicit_owner(source: str, start: int) -> bool:
    prefix = source[:start].rstrip()
    return any(
        re.search(
            rf"(?<![0-9A-Za-z가-힣]){re.escape(label)}\s*(?::|：|=)?\s*$",
            prefix,
        )
        for label in _PRODUCT_DI_SEMANTIC_OWNER_LABELS
    )


def _product_prescription_semantic_pattern(label: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![0-9A-Za-z가-힣]){re.escape(label)}"
        rf"(?:{_PRODUCT_PRESCRIPTION_PARTICLE_PATTERN})?(?![0-9A-Za-z가-힣])",
        re.IGNORECASE,
    )


def extract_product_di_semantic_group(text: Any) -> str:
    """Resolve an explicit upper product group without changing name-LIKE filters."""
    groups = extract_product_di_semantic_groups(text)
    return groups[0] if len(groups) == 1 else ""


def extract_product_di_semantic_groups(text: Any) -> tuple[str, ...]:
    """Return every distinct standalone product-division semantic in the text."""
    source = _text(text)
    groups: list[str] = []
    for label, group in sorted(
        PRODUCT_DI_SEMANTIC_GROUP_TERMS.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        for match in re.finditer(
            rf"(?:^|\s){re.escape(label)}(?=\s|$)",
            source,
            flags=re.IGNORECASE,
        ):
            if not _semantic_term_has_explicit_owner(source, match.start()) and group not in groups:
                groups.append(group)
    return tuple(groups)


def has_product_di_semantic_conflict(text: Any) -> bool:
    """Return True when one request contains both professional and OTC semantics."""
    return len(extract_product_di_semantic_groups(text)) > 1


def extract_product_prescription_semantics(text: Any) -> tuple[str, ...]:
    """Return distinct unlabelled prescription/OTC meanings in request order."""
    source = _text(text)
    semantics: list[str] = []
    for label, semantic in sorted(
        PRODUCT_PRESCRIPTION_SEMANTIC_TERMS.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        for match in _product_prescription_semantic_pattern(label).finditer(source):
            if not _semantic_term_has_explicit_owner(source, match.start()) and semantic not in semantics:
                semantics.append(semantic)
    return tuple(semantics)


def extract_product_prescription_semantic(text: Any) -> str:
    semantics = extract_product_prescription_semantics(text)
    return semantics[0] if len(semantics) == 1 else ""


def has_product_prescription_semantic_conflict(text: Any) -> bool:
    return len(extract_product_prescription_semantics(text)) > 1


def strip_product_prescription_semantic_terms(text: Any) -> str:
    """Remove only unlabelled, standalone prescription/OTC aliases."""
    source = _text(text)
    for label in sorted(PRODUCT_PRESCRIPTION_SEMANTIC_TERMS, key=len, reverse=True):
        pattern = _product_prescription_semantic_pattern(label)
        matches = list(pattern.finditer(source))
        for match in reversed(matches):
            if not _semantic_term_has_explicit_owner(source, match.start()):
                source = source[:match.start()] + " " + source[match.end():]
    return re.sub(r"\s+", " ", source).strip()


def valid_drug_standard_code_sql(standard_expression: str, main_expression: str) -> str:
    standard = f"LTRIM(RTRIM(ISNULL({standard_expression}, '')))"
    main = f"LTRIM(RTRIM(ISNULL({main_expression}, '')))"
    return (
        f"LEN({standard}) = 13 AND {standard} NOT LIKE '%[^0-9]%' AND {standard} LIKE '880%' "
        f"AND LEN({main}) = 13 AND {main} NOT LIKE '%[^0-9]%' AND {main} LIKE '880%'"
    )


def is_valid_drug_standard_code(value: Any) -> bool:
    code = _text(value)
    return len(code) == 13 and all("0" <= ch <= "9" for ch in code) and code.startswith("880")


def classify_product_prescription_semantic(
    product_di_cd: Any,
    standard_cd: Any,
    main_standard_cd: Any,
    product_di_nm: Any = "",
) -> str:
    code = _text(product_di_cd)
    if code in PRESCRIPTION_PRODUCT_DI_CODES:
        return "prescription"
    if code in OTC_PRODUCT_DI_CODES:
        return "otc"
    # Numeric non-drug divisions are authoritative. Character divisions keep
    # their company code-master name meaning and must not be numerically cast.
    if not code or code.isdecimal():
        return ""
    name = _text(product_di_nm).replace(" ", "")
    if name in {"전문", "전문의약품", "수입", "약가유연제"} or re.fullmatch(
        r"(?:비?보험)\((?:전문|수입|약가유연제)\)", name
    ):
        return "prescription"
    if name in {"일반", "일반의약품"} or re.fullmatch(r"(?:비?보험)\(일반\)", name):
        return "otc"
    return ""


def _product_di_name_predicate(name_expression: str, semantic: str) -> str:
    name = f"REPLACE(LTRIM(RTRIM(ISNULL({name_expression}, N''))), N' ', N'')"
    if semantic == "prescription":
        labels = ("전문", "전문의약품", "수입", "약가유연제", "보험(전문)", "보험(수입)",
                  "보험(약가유연제)", "비보험(전문)", "비보험(수입)", "비보험(약가유연제)")
    elif semantic == "otc":
        labels = ("일반", "일반의약품", "보험(일반)", "비보험(일반)")
    elif semantic in {"insurance", "insurance_product"}:
        tokens = _product_di_semantic_tokens_sql(name_expression)
        return f"({tokens} LIKE N'%|보험|%' AND {tokens} NOT LIKE N'%|비보험|%')"
    elif semantic in {"non_insurance", "non_insurance_drug"}:
        tokens = _product_di_semantic_tokens_sql(name_expression)
        return f"({tokens} LIKE N'%|비보험|%' AND {tokens} NOT LIKE N'%|보험|%')"
    else:
        raise ValueError("지원하지 않는 제품구분 의미입니다.")
    return f"{name} IN ({', '.join('N' + repr(label) for label in labels)})"


def _product_di_semantic_tokens_sql(name_expression: str) -> str:
    tokenized = f"ISNULL({name_expression}, N'')"
    for delimiter in (" ", "(", ")", "/", "·", ","):
        tokenized = f"REPLACE({tokenized}, N'{delimiter}', N'|')"
    return f"(N'|' + {tokenized} + N'|')"


def _company_product_di_exists_sql(product_di_code_expression: str,
                                   product_di_gcode_expression: str, semantic: str) -> str:
    return (
        "EXISTS (SELECT 1 FROM dbo.Rddbc010 AS CompanyProductDi WITH (NOLOCK) "
        f"WHERE CompanyProductDi.Rd01_Gcode = {product_di_gcode_expression} "
        f"AND CompanyProductDi.Rd01_Tcode = {product_di_code_expression} "
        f"AND {_product_di_name_predicate('CompanyProductDi.Rd01_Hnm', semantic)})"
    )


def _product_prescription_code_predicate(product_di_code_expression: str, semantic: str) -> str:
    codes = PRESCRIPTION_PRODUCT_DI_CODES if semantic == "prescription" else OTC_PRODUCT_DI_CODES
    expression = f"LTRIM(RTRIM(ISNULL({product_di_code_expression}, N'')))"
    values = ", ".join(f"N'{code}'" for code in sorted(codes))
    return f"{expression} IN ({values})"


def _product_di_semantic_code_predicate(product_di_code_expression: str, semantic: str) -> str:
    if semantic == "insurance":
        codes = INSURANCE_PRODUCT_DI_CODES
    elif semantic == "insurance_product":
        codes = INSURANCE_PRODUCT_SCOPE_DI_CODES
    else:
        codes = NON_INSURANCE_PRODUCT_DI_CODES
    expression = f"LTRIM(RTRIM(ISNULL({product_di_code_expression}, N'')))"
    values = ", ".join(f"N'{code}'" for code in sorted(codes))
    return f"{expression} IN ({values})"


def _non_numeric_product_di_predicate(product_di_code_expression: str) -> str:
    """Allow name semantics only for non-numeric company product-division codes."""
    expression = f"LTRIM(RTRIM(ISNULL({product_di_code_expression}, N'')))"
    return f"({expression} <> N'' AND {expression} LIKE N'%[^0-9]%')"


def is_management_only_standard_code(standard_cd: Any, main_standard_cd: Any) -> bool:
    return (
        is_valid_drug_standard_code(standard_cd)
        and is_valid_drug_standard_code(main_standard_cd)
        and _text(standard_cd) == _text(main_standard_cd)
    )


def _prescription_exists_sql(product_code_expression: str) -> str:
    valid = valid_drug_standard_code_sql(
        "PrescriptionStd.Rd046_Standard_Cd",
        "PrescriptionStd.Rd046_Main_Standard_Cd",
    )
    return (
        "EXISTS (SELECT 1 FROM dbo.Rddbc046 AS PrescriptionStd WITH (NOLOCK) "
        f"WHERE PrescriptionStd.Rd046_Physic_Cd = {product_code_expression} AND {valid})"
    )


def add_named_product_prescription_filter(
    clauses: MutableSequence[str],
    params: dict[str, Any],
    *,
    product_code_expression: str,
    product_di_code_expression: str,
    product_di_gcode_expression: str,
    bind_prefix: str = "product_prescription",
) -> None:
    add_named_product_prescription_code_filter(
        clauses,
        params,
        product_di_code_expression=product_di_code_expression,
        product_di_gcode_expression=product_di_gcode_expression,
        bind_prefix=bind_prefix,
    )


def add_named_product_prescription_code_filter(
    clauses: MutableSequence[str],
    params: dict[str, Any],
    *,
    product_di_code_expression: str,
    product_di_gcode_expression: str,
    bind_prefix: str = "product_prescription",
) -> None:
    """Append the R040 prescription/OTC code predicate without R046 validity."""
    semantic = _text(params.get("product_prescription_semantic"))
    if not semantic:
        return
    if semantic not in PRODUCT_PRESCRIPTION_SEMANTICS:
        raise ValueError("지원하지 않는 전문/일반 제품 의미입니다.")
    numeric = _product_prescription_code_predicate(product_di_code_expression, semantic)
    code_master = _company_product_di_exists_sql(
        product_di_code_expression, product_di_gcode_expression, semantic,
    )
    clauses.append(
        f"({numeric} OR ({_non_numeric_product_di_predicate(product_di_code_expression)} "
        f"AND {code_master}))"
    )


def add_named_management_only_exclusion(
    clauses: MutableSequence[str],
    *,
    product_code_expression: str,
    product_di_code_expression: str,
) -> None:
    """Exclude only valid matching standard/representative drug codes."""
    valid = valid_drug_standard_code_sql(
        "ManagementStd.Rd046_Standard_Cd",
        "ManagementStd.Rd046_Main_Standard_Cd",
    )
    drug_codes = ", ".join(f"N'{code}'" for code in sorted(DRUG_PRODUCT_DI_CODES))
    product_di = f"LTRIM(RTRIM(ISNULL({product_di_code_expression}, N'')))"
    clauses.append(
        "NOT EXISTS (SELECT 1 FROM dbo.Rddbc046 AS ManagementStd WITH (NOLOCK) "
        f"WHERE ManagementStd.Rd046_Physic_Cd = {product_code_expression} AND {valid} "
        "AND LTRIM(RTRIM(ManagementStd.Rd046_Standard_Cd)) = "
        f"LTRIM(RTRIM(ManagementStd.Rd046_Main_Standard_Cd)) AND {product_di} IN ({drug_codes}))"
    )


def add_named_product_di_semantic_filter(
    clauses: MutableSequence[str],
    params: dict[str, Any],
    *,
    product_di_name_expression: str,
    bind_prefix: str = "product_di_semantic",
) -> None:
    """Append the canonical semantic-name predicate to named-bind SQL callers."""
    group = _text(params.get("product_di_semantic_group"))
    if not group:
        return
    if group not in PRODUCT_DI_SEMANTICS:
        raise ValueError("지원하지 않는 상위 제품구분입니다.")
    expression = _text(product_di_name_expression)
    if not expression:
        raise ValueError("제품구분 이름 조건을 적용할 수 없습니다.")
    clauses.append(_product_di_name_predicate(expression, group))


def strip_product_di_semantic_terms(text: Any) -> str:
    """제품구분으로 소비된 의미어를 다른 이름 조건이 다시 사용하지 못하게 제거한다."""
    source = _text(text)
    for label in sorted(PRODUCT_DI_SEMANTIC_GROUP_TERMS, key=len, reverse=True):
        pattern = re.compile(rf"(?:^|\s){re.escape(label)}(?=\s|$)", re.IGNORECASE)
        source = pattern.sub(
            lambda match: match.group(0)
            if _semantic_term_has_explicit_owner(source, match.start())
            else " ",
            source,
        )
    return re.sub(r"\s+", " ", source).strip()


def classify_product_di_business_semantic(product_di_cd: Any, product_di_nm: Any) -> str:
    """Classify a company-owned product code by its authoritative code-master name."""
    if not _text(product_di_cd):
        return ""
    tokens = set(re.split(r"[ ()/·,]+", _text(product_di_nm)))
    if "보험" in tokens and "비보험" in tokens:
        return ""
    if "비보험" in tokens:
        return "non_insurance"
    if "보험" in tokens:
        return "insurance"
    return ""


def build_product_master_enrichment_sql(
    *,
    product_alias: str,
    aliases: Mapping[str, str] | None = None,
    include_audit_price: bool = True,
    include_vendor_managers: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Build the canonical R040 name/audit/price joins for another table query."""
    names = {
        "maker": "PV", "group": "PG", "di": "PD", "class": "PC",
        "add_user": "PAU", "mod_user": "PMU", "price": "PMCALC", "standard": "PSTD",
        "order_vendor": "POV", "maker_manager": "PMS", "order_vendor_manager": "POS",
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
LEFT JOIN dbo.Rddbc046 AS {names['standard']} WITH (NOLOCK)
    ON {p}.Rd04_Physic_Cd = {names['standard']}.Rd046_Physic_Cd
""".strip()
    if include_vendor_managers:
        joins = f"""
{joins}
LEFT JOIN dbo.Rddbc030 AS {names['order_vendor']} WITH (NOLOCK)
    ON {p}.Rd04_Orven_Cd = {names['order_vendor']}.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc060 AS {names['maker_manager']} WITH (NOLOCK)
    ON {names['maker']}.Rd03_Sales_Man = {names['maker_manager']}.Rd06_User_Cd
LEFT JOIN dbo.Rddbc060 AS {names['order_vendor_manager']} WITH (NOLOCK)
    ON {names['order_vendor']}.Rd03_Sales_Man = {names['order_vendor_manager']}.Rd06_User_Cd
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
        "order_vendor_nm": f"{names['order_vendor']}.Rd03_Ven_Nm",
        "maker_manager_nm": f"{names['maker_manager']}.Rd06_User_Nm" if include_vendor_managers else "",
        "order_vendor_manager_nm": f"{names['order_vendor_manager']}.Rd06_User_Nm" if include_vendor_managers else "",
        "product_group_nm": f"{names['group']}.Rd01_Hnm",
        "product_di_nm": f"{names['di']}.Rd01_Hnm",
        "product_di_cd": f"{p}.Rd04_Physic_Di",
        "product_di_gcode": f"{p}.Rd04_Physic_Di_Gcode",
        "standard_cd": f"{names['standard']}.Rd046_Standard_Cd",
        "main_standard_cd": f"{names['standard']}.Rd046_Main_Standard_Cd",
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
    if semantic_group and semantic_group not in PRODUCT_DI_SEMANTICS:
        raise ValueError("지원하지 않는 상위 제품구분입니다.")
    prescription_semantic = out.get("product_prescription_semantic", "")
    if prescription_semantic and prescription_semantic not in PRODUCT_PRESCRIPTION_SEMANTICS:
        raise ValueError("지원하지 않는 전문/일반 제품 의미입니다.")
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
    if values["order_nm"] and not _text(expressions.get("order_vendor_nm")):
        raise ValueError("발주처 조건을 적용할 수 없습니다.")
    add_like("order_nm", "order_vendor_nm")
    for key in ("maker_manager_nm", "order_vendor_manager_nm"):
        if values[key] and not _text(expressions.get(key)):
            raise ValueError("담당자 조건을 적용할 수 없습니다.")
        add_like(key, key)
    add_like("product_group_nm", "product_group_nm")
    add_like("product_di_nm", "product_di_nm")
    product_code = _text(expressions.get("physic_cd"))
    product_di_code = _text(expressions.get("product_di_cd"))
    product_di_gcode = _text(expressions.get("product_di_gcode"))
    semantic_group = values["product_di_semantic_group"] if not values["product_di_nm"] else ""
    product_di_name = _text(expressions.get("product_di_nm"))
    if semantic_group and not product_di_name:
        raise ValueError("제품구분 이름 조건을 적용할 수 없습니다.")
    if semantic_group:
        if not product_di_code:
            raise ValueError("제품구분 코드 조건을 적용할 수 없습니다.")
        numeric = _product_di_semantic_code_predicate(product_di_code, semantic_group)
        # Numeric product-division codes are authoritative.  Letter codes retain
        # their company-owned display-name semantics without numeric coercion.
        if semantic_group in {"insurance", "non_insurance_drug"}:
            clauses.append(numeric)
        else:
            clauses.append(
                "(" + numeric + " OR (" + _non_numeric_product_di_predicate(product_di_code)
                + " AND " + _product_di_name_predicate(product_di_name, semantic_group) + "))"
            )
    prescription_semantic = values["product_prescription_semantic"] if not values["product_di_nm"] else ""
    if prescription_semantic and not (product_di_code and product_di_gcode):
        raise ValueError("전문/일반 제품 조건을 적용할 수 없습니다.")
    if prescription_semantic:
        clauses.append(
            "(" + _product_prescription_code_predicate(product_di_code, prescription_semantic)
            + " OR (" + _non_numeric_product_di_predicate(product_di_code)
            + " AND " + _company_product_di_exists_sql(
                product_di_code, product_di_gcode, prescription_semantic,
            ) + "))"
        )
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
