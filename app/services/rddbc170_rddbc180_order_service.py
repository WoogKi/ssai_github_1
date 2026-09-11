"""Rddbc170/Rddbc180 order-detail and expected-inbound queries."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional

import pandas as pd

from app.services.business_calendar_service import RecentBusinessDaysResult, kst_today, recent_business_days
from app.services.erp_table_query_service import build_feature_result, execute_bound_select, registered_query_limits
from app.services.product_master_filter_contract import (
    append_product_master_filter_clauses,
    build_product_master_enrichment_sql,
    normalize_product_master_filters,
)


TABLE = "rddbc170_rddbc180"
ORDER_ACTION = "발주조회"
EXPECTED_ACTION = "입고예정조회"

_PRODUCT_JOINS, _PRODUCT_EXPRESSIONS = build_product_master_enrichment_sql(
    product_alias="P", aliases={"maker": "PMV"}, include_audit_price=False,
)

_ORDER_SPEC = None
_NORMAL_ORDER_PREDICATE = "(D.Rd18_Quantity + D.Rd18_Oquantity) >= 0"
_EXPECTED_BUSINESS_DAY_COUNT = 4


class BusinessCalendarUnavailableError(RuntimeError):
    """Raised before an ERP query when the shared Calendar authority is unavailable."""


def _expected_business_dates(params: dict[str, Any]) -> tuple[str, ...]:
    existing = tuple(str(value).strip() for value in params.get("_business_dates") or () if str(value).strip())
    if len(existing) == _EXPECTED_BUSINESS_DAY_COUNT:
        return existing
    calendar: RecentBusinessDaysResult = recent_business_days(
        base_date=_date_to_obj(params.get("_today")),
        count=_EXPECTED_BUSINESS_DAY_COUNT,
        include_base=True,
    )
    if calendar.status != "ready" or len(calendar.dates) != _EXPECTED_BUSINESS_DAY_COUNT:
        raise BusinessCalendarUnavailableError(
            f"ssai_business_calendar_unavailable:{calendar.reason_code or calendar.status}"
        )
    return calendar.dates


def _feature_spec():
    global _ORDER_SPEC
    if _ORDER_SPEC is None:
        from app.sims.meta.erp_table_feature_registry import get_action_spec
        _ORDER_SPEC = get_action_spec(ORDER_ACTION)[0]
    return _ORDER_SPEC


_SELECT_COLUMNS = """
    RTRIM(H.Rd17_Or_YyMmDd) AS [발주일자],
    RTRIM(D.Rd18_OrVen_Cd) AS [발주거래처코드],
    ISNULL(OV.Rd03_Ven_Nm, '') AS [발주거래처명],
    D.Rd18_Or_Seq AS [발주순번],
    D.Rd18_Orsub_Seq AS [상세순번],
    RTRIM(H.Rd17_Put_YyMmDd) AS [납기일자],
    RTRIM(D.Rd18_Physic_Cd) AS [제품코드],
    ISNULL(P.Rd04_Physic_Nm, '') AS [제품명],
    RTRIM(D.Rd18_Cost_Apply_Cd) AS [단가적용처코드],
    ISNULL(CV.Rd03_Ven_Nm, '') AS [단가적용처명],
    RTRIM(D.Rd18_Stock_Apply_Cd) AS [재고적용처코드],
    ISNULL(SV.Rd03_Ven_Nm, '') AS [재고적용처명],
    RTRIM(D.Rd18_Stock_Cd) AS [재고위치코드],
    ISNULL(ST.Rd01_Hnm, '') AS [재고위치명],
    D.Rd18_Unit_Cost AS [단가],
    D.Rd18_Quantity AS [발주수량],
    D.Rd18_Oquantity AS [할증수량],
    D.Rd18_In_Quantity AS [입고수량],
    CASE D.Rd18_Or_Di
        WHEN '1' THEN D.Rd18_Quantity + D.Rd18_Oquantity
        WHEN '2' THEN D.Rd18_Quantity + D.Rd18_Oquantity - D.Rd18_In_Quantity
        WHEN '3' THEN 0
        ELSE NULL
    END AS [미입고수량],
    RTRIM(D.Rd18_Or_Di) AS [발주상태코드],
    ISNULL(OS.Rd01_Hnm, '') AS [발주상태명],
    RTRIM(H.Rd17_DamDang) AS [발주담당자코드],
    ISNULL(U.Rd06_User_Nm, '') AS [발주담당자명],
    RTRIM(D.Rd18_Pro_Ven_Cd) AS [예상매출처코드],
    ISNULL(PV.Rd03_Ven_Nm, '') AS [예상매출처명],
    RTRIM(D.Rd18_Real_Ven_Cd) AS [실납처코드],
    ISNULL(RV.Rd03_Ven_Nm, '') AS [실납처명]
""".strip()

_JOINS = f"""
FROM dbo.Rddbc180 AS D
INNER JOIN dbo.Rddbc170 AS H
    ON H.Rd17_Or_YyMmDd = D.Rd18_Or_YyMmDd
   AND H.Rd17_Orven_Cd = D.Rd18_OrVen_Cd
   AND H.Rd17_Or_Seq = D.Rd18_Or_Seq
LEFT JOIN dbo.Rddbc040 AS P ON D.Rd18_Physic_Cd = P.Rd04_Physic_Cd
LEFT JOIN dbo.Rddbc030 AS OV ON D.Rd18_OrVen_Cd = OV.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS CV ON D.Rd18_Cost_Apply_Cd = CV.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS SV ON D.Rd18_Stock_Apply_Cd = SV.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS PV ON D.Rd18_Pro_Ven_Cd = PV.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS RV ON D.Rd18_Real_Ven_Cd = RV.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc010 AS ST
    ON D.Rd18_Stock_Cd_Gcode = ST.Rd01_Gcode
   AND D.Rd18_Stock_Cd = ST.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS OS
    ON D.Rd18_Or_Di_Gcode = OS.Rd01_Gcode
   AND D.Rd18_Or_Di = OS.Rd01_Tcode
LEFT JOIN dbo.Rddbc060 AS U ON H.Rd17_DamDang = U.Rd06_User_Cd
{_PRODUCT_JOINS}
""".strip()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _clean_codes(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    values = value if isinstance(value, (list, tuple, set)) else (value,)
    return tuple(dict.fromkeys(code for code in (_clean(item) for item in values) if code))


def _order_status_codes(value: Any, *, fallback: Any = "") -> tuple[str, ...]:
    codes = _clean_codes(value)
    if not codes and _clean(fallback):
        codes = (_clean(fallback),)
    return tuple(code for code in codes if code in {"1", "2", "3"})


def _date_value(value: Any, *, default: str = "") -> str:
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    digits = "".join(ch for ch in _clean(value) if ch.isdigit())
    if len(digits) != 8:
        return default
    try:
        parsed = datetime.strptime(digits, "%Y%m%d")
    except ValueError:
        return default
    if not 1900 <= parsed.year <= 2099:
        return default
    return digits


def _display_date(value: Any) -> str:
    text = _date_value(value)
    return f"{text[:4]}-{text[4:6]}-{text[6:]}" if text else _clean(value)


def _append_query_condition(parts: list[str], label: str, code: Any = "", name: Any = "") -> None:
    code_text = _clean(code)
    name_text = _clean(name)
    if code_text and name_text:
        parts.append(f"{label} {code_text} {name_text}")
    elif code_text:
        parts.append(f"{label} {code_text}")
    elif name_text:
        parts.append(f"{label} {name_text}")


def _expected_inbound_query_summary(params: dict[str, Any]) -> str:
    """Render the already-authoritative Calendar scope without recalculating it."""
    if params.get("_expected_inbound_explicit_period"):
        parts = [
            "발주기간 " + _display_date(params.get("date_from")) + " ~ " + _display_date(params.get("date_to")),
        ]
    else:
        dates = tuple(_date_value(value) for value in params.get("_business_dates") or ())
        dates = tuple(value for value in dates if value)
        if len(dates) != _EXPECTED_BUSINESS_DAY_COUNT:
            raise BusinessCalendarUnavailableError("expected inbound Calendar scope is unavailable")

        base_date = _display_date(params.get("_today") or dates[0])
        scope_dates = ", ".join(_display_date(value) for value in sorted(dates))
        parts = [f"기준일 {base_date}", f"오늘 포함 최근 {_EXPECTED_BUSINESS_DAY_COUNT}영업일: {scope_dates}"]
    _append_query_condition(parts, "제품", params.get("physic_cd"), params.get("physic_nm"))
    _append_query_condition(parts, "발주처", params.get("order_vendor_cd"), params.get("order_vendor_nm"))
    _append_query_condition(parts, "단가적용처", params.get("cost_apply_cd"), params.get("cost_apply_nm"))
    _append_query_condition(parts, "재고적용처", params.get("stock_apply_cd"), params.get("stock_apply_nm"))
    _append_query_condition(parts, "재고위치", params.get("stock_cd"), params.get("stock_nm"))
    status_codes = _order_status_codes(params.get("status_codes"), fallback=params.get("status_code"))
    if status_codes:
        parts.append("발주상태 " + ",".join(status_codes))
    if params.get("has_outstanding"):
        parts.append("미입고 있음")
    return " / ".join(parts)


def normalize_order_params(params: Optional[dict[str, Any]] = None, *, mode: str) -> dict[str, Any]:
    source = dict(params or {})
    today = kst_today()
    out = normalize_product_master_filters(source)
    out.update(source)
    for key in (
        "physic_nm", "order_vendor_cd", "order_vendor_nm", "status_code",
        "cost_apply_cd", "cost_apply_nm", "stock_apply_cd", "stock_apply_nm",
        "stock_cd", "stock_nm", "expected_vendor_cd", "expected_vendor_nm",
        "real_vendor_cd", "real_vendor_nm",
    ):
        out[key] = _clean(source.get(key))
    out["status_codes"] = _order_status_codes(source.get("status_codes"), fallback=out["status_code"])
    if len(out["status_codes"]) == 1:
        out["status_code"] = out["status_codes"][0]
    out["stock_cd_list"] = _clean_codes(source.get("stock_cd_list") or source.get("stock_cds"))
    out["include_blank_stock_cd"] = bool(source.get("include_blank_stock_cd", False))
    if mode == "order":
        out["date_from"] = _date_value(source.get("date_from"), default=(today - timedelta(days=30)).strftime("%Y%m%d"))
        out["date_to"] = _date_value(source.get("date_to"), default=today.strftime("%Y%m%d"))
    else:
        # IO NLQ's default period policy supplies today's date for its generic
        # display contract.  It is not a user-specified expected-inbound range.
        auto_period = bool(source.get("_expected_inbound_auto_period"))
        explicit_from = "" if auto_period else _date_value(source.get("date_from"))
        explicit_to = "" if auto_period else _date_value(source.get("date_to"))
        out["_expected_inbound_explicit_period"] = bool(explicit_from and explicit_to)
        if out["_expected_inbound_explicit_period"]:
            out["date_from"] = explicit_from
            out["date_to"] = explicit_to
            out["_business_dates"] = ()
            out["_business_calendar_authority"] = "not_applicable_explicit_period"
        else:
            out["date_from"] = ""
            out["date_to"] = ""
            out["_business_dates"] = _expected_business_dates(source)
            out["_business_calendar_authority"] = "ssai_common_calendar"
    out["due_date_from"] = _date_value(source.get("due_date_from"))
    out["due_date_to"] = _date_value(source.get("due_date_to"))
    out["has_outstanding"] = bool(source.get("has_outstanding", False))
    limits = registered_query_limits(out, source_limit_env=_feature_spec().source_limit_env)
    out.update({"mode": mode, "display_top": limits.display_top, "source_top": limits.source_top, "top": limits.source_top, "_limit_contract": limits})
    return out


def _filters(params: dict[str, Any], *, mode: str) -> tuple[list[str], list[Any]]:
    clauses = [_NORMAL_ORDER_PREDICATE]
    values: list[Any] = []
    if mode == "expected":
        status_codes = _order_status_codes(params.get("status_codes"), fallback=params.get("status_code"))
        if not status_codes:
            clauses.append("D.Rd18_Or_Di IN ('1', '2')")
        else:
            allowed_statuses = tuple(code for code in status_codes if code in {"1", "2"})
            if allowed_statuses:
                clauses.append(f"D.Rd18_Or_Di IN ({','.join('?' for _ in allowed_statuses)})")
                values.extend(allowed_statuses)
            else:
                clauses.append("1 = 0")
        if params.get("_expected_inbound_explicit_period"):
            clauses.extend(("H.Rd17_Or_YyMmDd >= ?", "H.Rd17_Or_YyMmDd <= ?"))
            values.extend((params["date_from"], params["date_to"]))
        else:
            business_dates = _expected_business_dates(params)
            clauses.append(f"H.Rd17_Or_YyMmDd IN ({','.join('?' for _ in business_dates)})")
            values.extend(business_dates)
    else:
        clauses.extend(("H.Rd17_Or_YyMmDd >= ?", "H.Rd17_Or_YyMmDd <= ?"))
        values.extend((params["date_from"], params["date_to"]))
    exact = (
        ("order_vendor_cd", "D.Rd18_OrVen_Cd"),
        ("cost_apply_cd", "D.Rd18_Cost_Apply_Cd"), ("stock_apply_cd", "D.Rd18_Stock_Apply_Cd"),
        ("expected_vendor_cd", "D.Rd18_Pro_Ven_Cd"),
        ("real_vendor_cd", "D.Rd18_Real_Ven_Cd"),
    )
    likes = (
        ("physic_nm", "P.Rd04_Physic_Nm"), ("order_vendor_nm", "OV.Rd03_Ven_Nm"),
        ("cost_apply_nm", "CV.Rd03_Ven_Nm"), ("stock_apply_nm", "SV.Rd03_Ven_Nm"),
        ("stock_nm", "ST.Rd01_Hnm"), ("expected_vendor_nm", "PV.Rd03_Ven_Nm"),
        ("real_vendor_nm", "RV.Rd03_Ven_Nm"),
    )
    for key, expression in exact:
        if params.get(key):
            clauses.append(f"{expression} = ?")
            values.append(params[key])
    stock_codes = _clean_codes(params.get("stock_cd_list"))
    if stock_codes:
        stock_clause = f"D.Rd18_Stock_Cd IN ({','.join('?' for _ in stock_codes)})"
        if params.get("include_blank_stock_cd"):
            stock_clause = f"({stock_clause} OR NULLIF(RTRIM(D.Rd18_Stock_Cd), '') IS NULL)"
        clauses.append(stock_clause)
        values.extend(stock_codes)
    elif params.get("stock_cd"):
        clauses.append("D.Rd18_Stock_Cd = ?")
        values.append(params["stock_cd"])
    for key, expression in likes:
        if params.get(key):
            clauses.append(f"{expression} LIKE ?")
            values.append(f"%{params[key]}%")
    for key, expression, op in (("due_date_from", "H.Rd17_Put_YyMmDd", ">="), ("due_date_to", "H.Rd17_Put_YyMmDd", "<=")):
        if params.get(key):
            clauses.append(f"{expression} {op} ?")
            values.append(params[key])
    if params.get("has_outstanding"):
        clauses.append("CASE D.Rd18_Or_Di WHEN '1' THEN D.Rd18_Quantity + D.Rd18_Oquantity WHEN '2' THEN D.Rd18_Quantity + D.Rd18_Oquantity - D.Rd18_In_Quantity WHEN '3' THEN 0 END > 0")
    append_product_master_filter_clauses(clauses, values, params, expressions={**_PRODUCT_EXPRESSIONS, "physic_cd": "D.Rd18_Physic_Cd"})
    return clauses, values


def _prepare(df: pd.DataFrame, *, mode: str) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    out = df.copy()
    def quality(value: Any) -> str:
        return "정상" if _date_value(value) else "비정상"
    out["발주일자 품질상태"] = out["발주일자"].map(quality)
    if mode == "expected":
        out["영업일 기준 유효 여부"] = "Y"
    out.insert(0, "조회순번", range(1, len(out) + 1))
    return out


def get_order_df(params: Optional[dict[str, Any]] = None, *, mode: str = "order") -> pd.DataFrame:
    qparams = normalize_order_params(params, mode=mode)
    clauses, values = _filters(qparams, mode=mode)
    sql = f"""SELECT TOP {int(qparams['top'])}\n{_SELECT_COLUMNS}\n{_JOINS}\nWHERE {' AND '.join(clauses)}\nORDER BY H.Rd17_Or_YyMmDd DESC,D.Rd18_OrVen_Cd,D.Rd18_Or_Seq,D.Rd18_Orsub_Seq"""
    return _prepare(execute_bound_select(sql, values), mode=mode)


def get_expected_inbound_product_totals(
    params: Optional[dict[str, Any]] = None,
) -> pd.DataFrame:
    """Return expected inbound quantity at product grain for one stock scope."""
    qparams = normalize_order_params(params, mode="expected")
    clauses, values = _filters(qparams, mode="expected")
    sql = f"""SELECT
    RTRIM(D.Rd18_Physic_Cd) AS [제품코드],
    SUM(CASE D.Rd18_Or_Di
        WHEN '1' THEN D.Rd18_Quantity + D.Rd18_Oquantity
        WHEN '2' THEN D.Rd18_Quantity + D.Rd18_Oquantity - D.Rd18_In_Quantity
        ELSE 0
    END) AS [입고예정수량],
    SUM(CASE WHEN NULLIF(RTRIM(D.Rd18_Stock_Cd), '') IS NULL THEN 1 ELSE 0 END)
        AS [_공백재고위치행수]
{_JOINS}
WHERE {' AND '.join(clauses)}
GROUP BY D.Rd18_Physic_Cd
ORDER BY D.Rd18_Physic_Cd"""
    out = execute_bound_select(sql, values)
    include_blank = bool(qparams.get("include_blank_stock_cd"))
    stock_scope = list(_clean_codes(qparams.get("stock_cd_list")) or _clean_codes(qparams.get("stock_cd")))
    attrs = {
        "source_call_count": 1,
        "source_grain": "제품코드",
        "source_detail_grain": "Or_YyMmDd+OrVen_Cd+Or_Seq+Orsub_Seq",
        "business_day_count": _EXPECTED_BUSINESS_DAY_COUNT,
        "business_calendar_authority": qparams.get("_business_calendar_authority", "ssai_common_calendar"),
        "stock_scope": stock_scope,
        "include_blank_stock_cd": include_blank,
        "blank_stock_location_included_rows": 0,
        "blank_stock_location_excluded_rows": None if not include_blank else 0,
        "preserved_detail_dimensions": ["단가적용처코드", "재고적용처코드", "재고위치코드"],
    }
    if out is None or not isinstance(out, pd.DataFrame) or out.empty:
        empty = pd.DataFrame(columns=["제품코드", "입고예정수량"])
        empty.attrs.update(attrs)
        return empty
    blank_series = (
        out["_공백재고위치행수"]
        if "_공백재고위치행수" in out.columns
        else pd.Series(0, index=out.index)
    )
    blank_rows = int(pd.to_numeric(blank_series, errors="coerce").fillna(0).sum())
    out = out[["제품코드", "입고예정수량"]].copy()
    out["제품코드"] = out["제품코드"].fillna("").astype(str).str.strip()
    out["입고예정수량"] = pd.to_numeric(out["입고예정수량"], errors="coerce").fillna(0)
    out = out.loc[out["제품코드"].ne("")].reset_index(drop=True)
    if out["제품코드"].duplicated().any():
        raise ValueError("expected inbound product projection must be one row per product")
    attrs["blank_stock_location_included_rows"] = blank_rows if include_blank else 0
    attrs["blank_stock_location_excluded_rows"] = 0 if include_blank else None
    out.attrs.update(attrs)
    return out


def _date_to_obj(value: Any) -> date | None:
    text = _date_value(value)
    return datetime.strptime(text, "%Y%m%d").date() if text else None


def _result(params: Optional[dict[str, Any]], *, mode: str, title: str) -> dict[str, Any]:
    qparams = normalize_order_params(params, mode=mode)
    limits = qparams.pop("_limit_contract")
    df = get_order_df(qparams, mode=mode)
    display = df.head(int(qparams["display_top"])).copy()
    query_summary = (
        _expected_inbound_query_summary(qparams)
        if mode == "expected"
        else qparams["date_from"] + "~" + qparams["date_to"]
    )
    summary = f"조회조건: {query_summary}\n\n결과: {len(df):,}건"
    payload = build_feature_result(table=TABLE, title=title, params=qparams, df=df, summary_md=summary)
    payload.update({"df": df, "df_display": display, "records": display.to_dict(orient="records"), "columns": list(display.columns)})
    limit_hit = len(df) >= int(limits.source_top)
    meta = dict(payload.get("meta") or {})
    meta.update({
        "source_table": "Rddbc170+Rddbc180", "source_mode": mode,
        "source_grain": "Or_YyMmDd+OrVen_Cd+Or_Seq+Orsub_Seq", "source_call_count": 1,
        "full_source_ready": True, "full_source_bounded": True,
        "full_source_limit_rows": int(limits.source_top), "full_source_limit_source": limits.source_limit_source,
        "full_source_limit_hit": limit_hit, "full_source_truncated": "unverified" if limit_hit else False,
        "display_row_count": len(display), "row_count": len(display), "row_count_total": len(df),
        "display_top": int(qparams["display_top"]), "display_limit_source": limits.display_limit_source,
        "business_calendar_authority": qparams.get("_business_calendar_authority", "not_applicable") if mode == "expected" else "not_applicable",
        "business_day_count": _EXPECTED_BUSINESS_DAY_COUNT if mode == "expected" and not qparams.get("_expected_inbound_explicit_period") else 0,
        "business_dates": tuple(qparams.get("_business_dates") or ()) if mode == "expected" else (),
        "expected_inbound_period_kind": "explicit_order_period" if qparams.get("_expected_inbound_explicit_period") else "recent_business_days",
        "query_summary": query_summary,
        "condition": query_summary,
    })
    payload["meta"] = meta
    return payload


def get_order_result(params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return _result(params, mode="order", title=ORDER_ACTION)


def get_expected_inbound_result(params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return _result(params, mode="expected", title=EXPECTED_ACTION)


def get_order_export_df(params: Optional[dict[str, Any]] = None) -> pd.DataFrame:
    return get_order_df(params, mode="order")


def get_expected_inbound_export_df(params: Optional[dict[str, Any]] = None) -> pd.DataFrame:
    return get_order_df(params, mode="expected")
