"""Read-only Rddbc230 final purchase-cost state query."""

from __future__ import annotations

import re
from typing import Any, Optional

import pandas as pd

from app.services.erp_table_query_service import (
    build_feature_result,
    execute_bound_select,
    registered_query_limits,
)
from app.services.product_master_filter_contract import (
    append_product_master_filter_clauses,
    build_product_master_enrichment_sql,
    normalize_product_master_filters,
)
from app.sims.meta.erp_table_feature_registry import RDDBC230


TABLE = "rddbc230"
ACTION = "최종 매입단가 조회"

_RESULT_COLUMN_ORDER = (
    "조회순번",
    "제품코드", "제품명", "제약사", "제품그룹", "제품구분코드", "제품구분", "제품분류",
    "매입처코드", "매입처명", "재고위치", "재고위치명",
    "재고적용코드", "재고적용처명", "단가적용코드", "단가적용처명",
    "실입고단가", "장부입고단가", "입고수량", "출고수량",
    "재고수량", "실재고금액", "장부재고금액",
)

_PRODUCT_MASTER_JOINS, _PRODUCT_MASTER_EXPRESSIONS = build_product_master_enrichment_sql(
    product_alias="P",
    include_audit_price=False,
)

_UNSUPPORTED_PRODUCT_FILTER_KEYS = (
    "product_unit_price", "product_final_price_date",
    "product_add_user_nm", "product_add_date_from", "product_add_date_to",
    "product_mod_user_nm", "product_mod_date_from", "product_mod_date_to",
    "product_only_use",
)

_SELECT_COLUMNS = f"""
    LTRIM(RTRIM(ISNULL(S.Rd23_Physic_Cd, ''))) AS [제품코드],
    LTRIM(RTRIM(ISNULL(P.Rd04_Physic_Nm, ''))) AS [제품명],
    LTRIM(RTRIM(ISNULL({_PRODUCT_MASTER_EXPRESSIONS['maker_nm']}, ''))) AS [제약사],
    LTRIM(RTRIM(ISNULL({_PRODUCT_MASTER_EXPRESSIONS['product_group_nm']}, ''))) AS [제품그룹],
    LTRIM(RTRIM(ISNULL(P.Rd04_Physic_Di, ''))) AS [제품구분코드],
    LTRIM(RTRIM(ISNULL({_PRODUCT_MASTER_EXPRESSIONS['product_di_nm']}, ''))) AS [제품구분],
    LTRIM(RTRIM(ISNULL({_PRODUCT_MASTER_EXPRESSIONS['product_class_nm']}, ''))) AS [제품분류],
    LTRIM(RTRIM(ISNULL(S.Rd23_Ven_Cd, ''))) AS [매입처코드],
    LTRIM(RTRIM(ISNULL(BV.Rd03_Ven_Nm, ''))) AS [매입처명],
    LTRIM(RTRIM(ISNULL(S.Rd23_Stock_Cd, ''))) AS [재고위치],
    LTRIM(RTRIM(ISNULL(ST.Rd01_Hnm, ''))) AS [재고위치명],
    LTRIM(RTRIM(ISNULL(S.Rd23_Stock_Apply_Cd, ''))) AS [재고적용코드],
    LTRIM(RTRIM(ISNULL(SV.Rd03_Ven_Nm, ''))) AS [재고적용처명],
    LTRIM(RTRIM(ISNULL(S.Rd23_Cost_Apply_Cd, ''))) AS [단가적용코드],
    LTRIM(RTRIM(ISNULL(CV.Rd03_Ven_Nm, ''))) AS [단가적용처명],
    S.Rd23_Unit_Cost AS [실입고단가],
    S.Rd23_Fin_Unit_Cost AS [장부입고단가],
    S.Rd23_In_Quantity AS [입고수량],
    S.Rd23_Out_Quantity AS [출고수량],
    (S.Rd23_In_Quantity - S.Rd23_Out_Quantity) AS [재고수량],
    ((S.Rd23_In_Quantity - S.Rd23_Out_Quantity) * S.Rd23_Unit_Cost) AS [실재고금액],
    ((S.Rd23_In_Quantity - S.Rd23_Out_Quantity) * S.Rd23_Fin_Unit_Cost) AS [장부재고금액]
""".strip()

_JOINS = f"""
FROM dbo.Rddbc230 AS S WITH (NOLOCK)
LEFT JOIN dbo.Rddbc040 AS P WITH (NOLOCK)
    ON S.Rd23_Physic_Cd = P.Rd04_Physic_Cd
LEFT JOIN dbo.Rddbc030 AS BV WITH (NOLOCK)
    ON S.Rd23_Ven_Cd = BV.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS SV WITH (NOLOCK)
    ON S.Rd23_Stock_Apply_Cd = SV.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS CV WITH (NOLOCK)
    ON S.Rd23_Cost_Apply_Cd = CV.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc010 AS ST WITH (NOLOCK)
    ON ST.Rd01_Gcode = '0018'
   AND S.Rd23_Stock_Cd = ST.Rd01_Tcode
{_PRODUCT_MASTER_JOINS}
""".strip()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _numeric_range(value: Any, *, label: str) -> tuple[float, float] | None:
    text = _clean(value).replace(",", "")
    if not text:
        return None
    match = re.fullmatch(
        r"(-?[0-9]+(?:\.[0-9]+)?)(?:\s*(?:~|-)\s*(-?[0-9]+(?:\.[0-9]+)?))?",
        text,
    )
    if not match:
        raise ValueError(f"{label}은 숫자 또는 시작~종료 범위여야 합니다.")
    first = float(match.group(1))
    second = float(match.group(2)) if match.group(2) is not None else first
    return (min(first, second), max(first, second))


def normalize_rddbc230_params(params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    out = dict(params or {})
    product_filters = normalize_product_master_filters(out)
    for key in _UNSUPPORTED_PRODUCT_FILTER_KEYS:
        product_filters[key] = ""
    out.update(product_filters)
    for key in (
        "physic_nm", "buy_cd", "buy_nm", "stock_cd", "stock_nm",
        "stock_apply_cd", "stock_apply_nm", "cost_apply_cd", "cost_apply_nm",
        "unit_cost", "fin_unit_cost", "in_quantity", "out_quantity",
    ):
        out[key] = _clean(out.get(key))
    limits = registered_query_limits(out, source_limit_env=RDDBC230.source_limit_env)
    out["display_top"] = limits.display_top
    out["source_top"] = limits.source_top
    out["top"] = limits.source_top
    out["_limit_contract"] = limits
    out["mode"] = "state"
    return out


def _filters(params: dict[str, Any]) -> tuple[list[str], list[Any]]:
    clauses = ["1 = 1"]
    values: list[Any] = []

    exact_filters = (
        ("buy_cd", "S.Rd23_Ven_Cd"),
        ("stock_cd", "S.Rd23_Stock_Cd"),
        ("stock_apply_cd", "S.Rd23_Stock_Apply_Cd"),
        ("cost_apply_cd", "S.Rd23_Cost_Apply_Cd"),
    )
    for key, expression in exact_filters:
        if params.get(key):
            clauses.append(f"{expression} = ?")
            values.append(params[key])

    like_filters = (
        ("physic_nm", "P.Rd04_Physic_Nm"),
        ("buy_nm", "BV.Rd03_Ven_Nm"),
        ("stock_nm", "ST.Rd01_Hnm"),
        ("stock_apply_nm", "SV.Rd03_Ven_Nm"),
        ("cost_apply_nm", "CV.Rd03_Ven_Nm"),
    )
    for key, expression in like_filters:
        if params.get(key):
            clauses.append(f"{expression} LIKE ?")
            values.append(f"%{params[key]}%")

    append_product_master_filter_clauses(
        clauses,
        values,
        params,
        expressions={**_PRODUCT_MASTER_EXPRESSIONS, "physic_cd": "S.Rd23_Physic_Cd"},
    )

    for key, expression, label in (
        ("unit_cost", "S.Rd23_Unit_Cost", "실입고단가"),
        ("fin_unit_cost", "S.Rd23_Fin_Unit_Cost", "장부입고단가"),
        ("in_quantity", "S.Rd23_In_Quantity", "입고수량"),
        ("out_quantity", "S.Rd23_Out_Quantity", "출고수량"),
    ):
        bounds = _numeric_range(params.get(key), label=label)
        if bounds is None:
            continue
        lower, upper = bounds
        if lower == upper:
            clauses.append(f"{expression} = ?")
            values.append(lower)
        else:
            clauses.extend((f"{expression} >= ?", f"{expression} <= ?"))
            values.extend((lower, upper))
    return clauses, values


def _prepare_result_projection(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    out = df.copy()
    if "조회순번" not in out.columns:
        out.insert(0, "조회순번", range(1, len(out) + 1))
    leading = [column for column in _RESULT_COLUMN_ORDER if column in out.columns]
    trailing = [column for column in out.columns if column not in leading]
    return out.loc[:, [*leading, *trailing]]


def get_rddbc230_df(params: Optional[dict[str, Any]] = None) -> pd.DataFrame:
    qparams = normalize_rddbc230_params(params)
    clauses, values = _filters(qparams)
    sql = f"""
SELECT TOP {int(qparams['top'])}
{_SELECT_COLUMNS}
{_JOINS}
WHERE {' AND '.join(clauses)}
ORDER BY
    S.Rd23_Physic_Cd, S.Rd23_Ven_Cd, S.Rd23_Stock_Cd,
    S.Rd23_Stock_Apply_Cd, S.Rd23_Cost_Apply_Cd,
    S.Rd23_Unit_Cost, S.Rd23_Fin_Unit_Cost
""".strip()
    return _prepare_result_projection(execute_bound_select(sql, values))


def _query_summary(params: dict[str, Any], row_count: int) -> str:
    parts: list[str] = []
    for key, label in (
        ("physic_cd", "제품코드"), ("physic_nm", "제품명"),
        ("buy_cd", "매입처코드"), ("buy_nm", "매입처"),
        ("stock_cd", "재고위치"), ("stock_nm", "재고위치명"),
        ("stock_apply_cd", "재고적용코드"), ("stock_apply_nm", "재고적용처"),
        ("cost_apply_cd", "단가적용코드"), ("cost_apply_nm", "단가적용처"),
        ("unit_cost", "실입고단가"), ("fin_unit_cost", "장부입고단가"),
        ("in_quantity", "입고수량"), ("out_quantity", "출고수량"),
        ("maker_nm", "제약사"), ("product_group_nm", "제품그룹"),
        ("product_di_nm", "제품구분"),
        ("product_di_semantic_group", "상위 제품구분"),
        ("product_class_nm", "제품분류"),
    ):
        if params.get(key):
            parts.append(f"{label} {params[key]}")
    condition = " / ".join(parts) if parts else "전체"
    return f"조회조건: {condition}\n\n결과: {row_count:,}건"


def get_rddbc230_result(params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    qparams = normalize_rddbc230_params(params)
    limits = qparams.pop("_limit_contract")
    display_top = int(qparams["display_top"])
    df = get_rddbc230_df(qparams)
    summary = _query_summary(qparams, len(df))
    payload = build_feature_result(
        table=TABLE,
        title=ACTION,
        params=qparams,
        df=df,
        summary_md=summary,
    )
    display_df = df.head(display_top).copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    source_limit = int(limits.source_top)
    limit_hit = bool(isinstance(df, pd.DataFrame) and len(df) >= source_limit)
    payload["df"] = df
    payload["df_display"] = display_df
    payload["records"] = display_df.to_dict(orient="records")
    payload["columns"] = list(display_df.columns)
    meta = dict(payload.get("meta") or {})
    meta.update(
        {
            "source_table": "Rddbc230",
            "source_mode": "state",
            "source_grain": (
                "Physic_Cd+Ven_Cd+Stock_Cd+Stock_Apply_Cd+Cost_Apply_Cd+"
                "Unit_Cost+Fin_Unit_Cost"
            ),
            "query_summary": summary.split("\n", 1)[0].replace("조회조건: ", ""),
            "condition": summary.split("\n", 1)[0].replace("조회조건: ", ""),
            "row_number_source": "result_display_projection",
            "display_row_count": int(len(display_df)),
            "row_count": int(len(display_df)),
            "row_count_total": int(len(df)),
            "source_call_count": 1,
            "full_source_ready": True,
            "full_source_bounded": True,
            "full_source_limit_rows": source_limit,
            "full_source_limit_source": limits.source_limit_source,
            "full_source_limit_hit": limit_hit,
            "full_source_truncated": "unverified" if limit_hit else False,
            "display_top": display_top,
            "display_limit_rows": int(limits.display_limit_rows),
            "display_limit_source": limits.display_limit_source,
            "requested_display_top": limits.requested_display_top,
            "export_limit_rows": int(limits.export_limit_rows),
            "source_safety_limit_rows": int(limits.source_safety_limit_rows),
            "limit_policy": "common_display_export_safety",
            "audit_columns_present": False,
        }
    )
    payload["meta"] = meta
    return payload


def get_rddbc230_export_df(params: Optional[dict[str, Any]] = None) -> pd.DataFrame:
    qparams = dict(params or {})
    qparams.pop("top", None)
    qparams.pop("display_top", None)
    qparams["_display_context"] = "chat"
    return get_rddbc230_df(qparams)
