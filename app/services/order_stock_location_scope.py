"""Company-scoped stock locations for R170/R180 orders and order calculation."""

from __future__ import annotations

import re
from typing import Any, Mapping

from app.services.io_nlq import resolve_stock_location_codes


def _values(value: Any) -> list[str]:
    items = value if isinstance(value, (list, tuple, set)) else [value]
    return [
        part.strip() for item in items if str(item or "").strip()
        for part in re.split(r"\s*[,/+]\s*|(?<=\S)[와과]\s+|\s+[와과]\s*", str(item))
        if part.strip()
    ]


def extract_order_stock_terms(text: str) -> tuple[list[str], list[str]] | None:
    """Read the location label without consuming another order filter or action."""
    match = re.search(r"재고위치(?:코드|명)?\s*[:=]?\s*(.*)", text)
    if not match:
        return None
    raw = re.split(
        r"\s+(?=(?:제품(?:코드|명)?|품목(?:코드|명)?|제조사|제약사|발주처|"
        r"단가적용처|재고적용처|발주담당자|발주\s*(?:조회|계산|내역|현황)|"
        r"입고\s*예정|조회|검색|보여줘|오늘|어제|이번|지난)\b)",
        match.group(1), maxsplit=1,
    )[0].strip()
    parts = re.split(r"\s*[,/+]\s*|(?<=\S)[와과]\s+|\s+[와과]\s*", raw)
    if not parts or any(not part.strip() for part in parts):
        return [], []
    codes, names = [], []
    for part in parts:
        value = part.strip()
        if re.fullmatch(r"\d{1,6}", value):
            codes.append(value)
        else:
            names.append(value)
    return list(dict.fromkeys(codes)), list(dict.fromkeys(names))


def resolve_order_stock_scope(
    params: Mapping[str, Any], *, default_codes: list[str],
    registered_names: Mapping[str, str] | None = None,
    saved_only: bool = False,
) -> tuple[dict[str, Any], str, list[str]]:
    """Resolve an explicit location or preserve the company's saved default."""
    out = dict(params)
    codes = list(dict.fromkeys(
        _values(out.get("stock_cd_list") or out.get("stock_cds")) + _values(out.get("stock_cd"))
    ))
    names = list(dict.fromkeys(
        _values(out.get("stock_nm_list") or out.get("stock_names")) + _values(out.get("stock_nm"))
    ))
    if out.get("_order_stock_terms_invalid"):
        return out, "location_unresolved", []
    if not codes and not names:
        selected = list(dict.fromkeys(_values(default_codes)))
        if not selected:
            return out, "no_saved_locations", []
        source = "company_default"
    else:
        names_by_code = dict(registered_names or {})
        allowed_codes = (
            [code for code in _values(default_codes) if code in names_by_code]
            if saved_only else list(names_by_code)
        )
        selected, error, candidates = resolve_stock_location_codes(
            codes, names, allowed_codes=allowed_codes, location_names=names_by_code,
        )
        if error:
            return out, error, candidates
        source = "explicit"
    out["stock_cd_list"] = selected
    out["stock_cds"] = list(selected)
    out["stock_cd"] = selected[0] if len(selected) == 1 else ""
    out.pop("stock_nm", None)
    out.pop("stock_nm_list", None)
    out.pop("stock_names", None)
    out["_order_stock_scope_source"] = source
    out["_order_stock_scope_policy"] = "saved" if saved_only else "registered"
    out["_order_stock_scope_resolved"] = True
    return out, "", []


def prepare_order_stock_scope(
    params: Mapping[str, Any], *, default_codes: list[str] | None = None,
    registered_names: Mapping[str, str] | None = None,
    saved_only: bool = False,
) -> tuple[dict[str, Any], str, list[str]]:
    """Load only the authorities needed for the requested company scope."""
    from app.db.mssql_client import get_current_company_id

    company_id = get_current_company_id()
    if company_id is None or (
        params.get("company_id") is not None and int(params["company_id"]) != int(company_id)
    ):
        return dict(params), "company_mismatch", []
    if params.get("_order_stock_scope_resolved") and (
        not saved_only or params.get("_order_stock_scope_policy") == "saved"
    ):
        return dict(params), "", []
    if default_codes is None:
        from app.services.ssai_analysis_profile_service import (
            load_dashboard_profile_checked, normalize_company_default_conditions,
        )

        profile = load_dashboard_profile_checked(company_id=int(company_id))
        if profile.status != "ready":
            return dict(params), "profile_unavailable", []
        default_codes = normalize_company_default_conditions(profile.profile).get("stock_cd_list") or []
    explicit = any(_values(params.get(key)) for key in (
        "stock_cd_list", "stock_cds", "stock_cd", "stock_nm_list", "stock_names", "stock_nm",
    ))
    if explicit and registered_names is None:
        from app.services.io_nlq import get_current_stock_location_name_map

        registered_names = get_current_stock_location_name_map()
    return resolve_order_stock_scope(
        params, default_codes=default_codes, registered_names=registered_names,
        saved_only=saved_only,
    )
