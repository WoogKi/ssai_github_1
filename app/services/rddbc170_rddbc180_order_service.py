"""Rddbc170/Rddbc180 order-detail and expected-inbound queries."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional

import pandas as pd

from app.services.business_calendar_service import build_recent_business_days_cte, kst_today
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
    if mode == "order":
        out["date_from"] = _date_value(source.get("date_from"), default=(today - timedelta(days=30)).strftime("%Y%m%d"))
        out["date_to"] = _date_value(source.get("date_to"), default=today.strftime("%Y%m%d"))
    else:
        out["date_from"] = ""
        out["date_to"] = ""
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
        clauses.extend(("D.Rd18_Or_Di IN ('1', '2')", "H.Rd17_Or_YyMmDd IN (SELECT business_date FROM RecentBusinessDates)"))
    else:
        clauses.extend(("H.Rd17_Or_YyMmDd >= ?", "H.Rd17_Or_YyMmDd <= ?"))
        values.extend((params["date_from"], params["date_to"]))
    exact = (
        ("order_vendor_cd", "D.Rd18_OrVen_Cd"), ("status_code", "D.Rd18_Or_Di"),
        ("cost_apply_cd", "D.Rd18_Cost_Apply_Cd"), ("stock_apply_cd", "D.Rd18_Stock_Apply_Cd"),
        ("stock_cd", "D.Rd18_Stock_Cd"), ("expected_vendor_cd", "D.Rd18_Pro_Ven_Cd"),
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
    prefix = ""
    if mode == "expected":
        calendar = build_recent_business_days_cte(
            today=_date_to_obj(qparams.get("_today")),
            count=_EXPECTED_BUSINESS_DAY_COUNT,
        )
        prefix = calendar.cte_sql + "\n"
        values = [*calendar.params, *values]
    sql = f"""{prefix}SELECT TOP {int(qparams['top'])}\n{_SELECT_COLUMNS}\n{_JOINS}\nWHERE {' AND '.join(clauses)}\nORDER BY H.Rd17_Or_YyMmDd DESC,D.Rd18_OrVen_Cd,D.Rd18_Or_Seq,D.Rd18_Orsub_Seq"""
    return _prepare(execute_bound_select(sql, values), mode=mode)


def _date_to_obj(value: Any) -> date | None:
    text = _date_value(value)
    return datetime.strptime(text, "%Y%m%d").date() if text else None


def _result(params: Optional[dict[str, Any]], *, mode: str, title: str) -> dict[str, Any]:
    qparams = normalize_order_params(params, mode=mode)
    limits = qparams.pop("_limit_contract")
    df = get_order_df(qparams, mode=mode)
    display = df.head(int(qparams["display_top"])).copy()
    summary = f"조회조건: {'오늘 + 직전 3영업일' if mode == 'expected' else qparams['date_from'] + '~' + qparams['date_to']}\n\n결과: {len(df):,}건"
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
        "business_calendar_authority": "dbo.WB_Holiday:holiday/work" if mode == "expected" else "not_applicable",
        "business_day_count": _EXPECTED_BUSINESS_DAY_COUNT if mode == "expected" else 0,
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
