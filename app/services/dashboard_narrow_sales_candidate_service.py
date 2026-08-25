"""Read-only monthly Dashboard sales candidate loader.

This is the production counterpart of the validated narrow candidate.  It is
only selectable for an explicit monthly-only policy; hybrid detail requests
remain on their existing representation rather than being silently coerced.
"""
from __future__ import annotations

import time
from typing import Any, Dict

import pandas as pd

from app.db.mssql_client import dashboard_measurement_phase, get_active_dashboard_query_measurement
from app.services.analytics_sales_trend_service import (
    DashboardNarrowSalesPurchaseBundle,
    _apply_month_or_date_params,
    _apply_period_source_policy_params,
    _build_dashboard_monthly_common_predicates,
    _build_dashboard_sales_branch_predicates,
    build_dashboard_product_dimension_scope_predicates,
    _monthly_spec,
    _resolve_source_mode,
    build_dashboard_narrow_bundle_from_projections,
    coalesce_params,
    query_to_df,
)


def _where(clauses: list[str]) -> str:
    return ("\n  AND " + "\n  AND ".join(clauses)) if clauses else ""


def _trim(sql: str) -> str:
    return f"COALESCE(LTRIM(RTRIM(CONVERT(NVARCHAR(255), {sql}))), N'')"


def _identity_key(fields: list[str]) -> str:
    parts = [f"CONVERT(NVARCHAR(10), LEN({field})), N':', {field}, N'|'" for field in fields]
    return "CONCAT(" + ", ".join(parts) + ")"


def _dimension_values(params: Dict[str, Any], *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        raw = params.get(key)
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple, set)):
            continue
        for item in raw:
            value = str(item or "").strip()
            if value and value not in values:
                values.append(value)
    return values


def _supports_exact_product_group_scope(params: Dict[str, Any]) -> bool:
    values = _dimension_values(params, "dashboard_product_group_list", "product_group_list")
    if not values:
        return False
    for value in values:
        gcode, separator, tcode = value.partition(":")
        if separator != ":" or gcode.strip() != "0013" or not tcode.strip():
            return False
    return True


def _product_scope_cte(
    *,
    params: Dict[str, Any],
    bind: Dict[str, Any],
    product_code_sql: str,
) -> tuple[str, str, bool]:
    clauses = build_dashboard_product_dimension_scope_predicates(params, bind, product_alias="P")
    if not clauses:
        return "", "", False
    cte = f"""FilteredProducts AS (
    SELECT DISTINCT P.Rd04_Physic_Cd AS 제품코드
    FROM dbo.Rddbc040 AS P WITH (NOLOCK)
    WHERE 1 = 1 {_where(clauses)}
)"""
    return cte, f"INNER JOIN FilteredProducts AS FP ON FP.제품코드 = {product_code_sql}", True


def _sales_cte(params: Dict[str, Any]) -> tuple[str, Dict[str, Any], dict[str, Any]]:
    prepared = _apply_period_source_policy_params(_apply_month_or_date_params(coalesce_params(params)))
    if not _dimension_values(prepared, "dashboard_product_group_list") and _dimension_values(prepared, "product_group_list"):
        prepared["dashboard_product_group_list"] = _dimension_values(prepared, "product_group_list")
    policy = prepared.get("_period_source_policy") or {}
    if bool(policy.get("use_hybrid") or policy.get("use_hybrid_detail")):
        raise ValueError("Dashboard narrow sales candidate requires monthly-only policy")
    source_mode = _resolve_source_mode(prepared)
    if source_mode not in {"monthly_book", "monthly_real"}:
        raise ValueError(f"Dashboard narrow sales candidate requires monthly source, got {source_mode}")
    spec = _monthly_spec(source_mode)
    p = spec["prefix"]
    common, bind = _build_dashboard_monthly_common_predicates(
        prepared, spec, supplier_bind_prefix="narrow_supplier", stock_bind_prefix="narrow_stock_cd"
    )
    sales_branch = _build_dashboard_sales_branch_predicates(prepared, spec, bind, table_alias="B")
    product_scope_cte, product_scope_join, product_scope_applied = _product_scope_cte(
        params=prepared,
        bind=bind,
        product_code_sql=_trim(f"M.{p}_Physic_Cd"),
    )
    cte_sections = []
    if product_scope_cte:
        cte_sections.append(product_scope_cte)
    cte_sections.extend((
        f"""BaseRows AS (
    SELECT LEFT(M.{p}_Stock_YyMm, 6) AS 기준월, M.{p}_Physic_Cd AS 제품코드,
           M.{p}_Ven_Cd AS 매입처코드, M.{p}_Io_Gu_Gcode AS {p}_Io_Gu_Gcode,
           M.{p}_Io_Gu AS {p}_Io_Gu, M.{p}_Out_Quantity AS {p}_Out_Quantity,
           M.{p}_Out_Oquantity AS {p}_Out_Oquantity,
           M.{p}_Out_Supply_Price AS {p}_Out_Supply_Price, M.{p}_Out_Tax_Price AS {p}_Out_Tax_Price
    FROM {spec['table']} AS M WITH (NOLOCK)
    {product_scope_join}
    WHERE 1 = 1 {_where(common)}
)""",
        f"""SalesRows AS (
    SELECT * FROM BaseRows AS B WHERE 1 = 1 {_where(sales_branch)}
)""",
        f"""NumericSales AS (
    SELECT 기준월, {_trim('제품코드')} AS 제품코드, {_trim('매입처코드')} AS 매입처코드,
           CASE WHEN LEFT({p}_Io_Gu, 1) = '6' THEN -1 * COALESCE({p}_Out_Quantity, 0) ELSE COALESCE({p}_Out_Quantity, 0) END AS 출고수량,
           CASE WHEN LEFT({p}_Io_Gu, 1) = '6' THEN -1 * COALESCE({p}_Out_Oquantity, 0) ELSE COALESCE({p}_Out_Oquantity, 0) END AS 출고할증수량,
           CASE WHEN LEFT({p}_Io_Gu, 1) = '6' THEN -1 * COALESCE({p}_Out_Supply_Price, 0) ELSE COALESCE({p}_Out_Supply_Price, 0) END AS 매출공급가액,
           CASE WHEN LEFT({p}_Io_Gu, 1) = '6' THEN -1 * COALESCE({p}_Out_Tax_Price, 0) ELSE COALESCE({p}_Out_Tax_Price, 0) END AS 매출세액,
           CASE WHEN LEFT({p}_Io_Gu, 1) = '6' THEN -1 * (COALESCE({p}_Out_Supply_Price, 0) + COALESCE({p}_Out_Tax_Price, 0)) ELSE COALESCE({p}_Out_Supply_Price, 0) + COALESCE({p}_Out_Tax_Price, 0) END AS 매출합계
    FROM SalesRows
)""",
    ))
    return "WITH " + ",\n".join(cte_sections) + "\n", bind, {"prepared": prepared, "spec": spec, "source_mode": source_mode, "product_scope_applied": product_scope_applied}


def can_use_dashboard_narrow_sales_candidate(params: Dict[str, Any]) -> tuple[bool, str]:
    """Make the representation choice explicit; never coerce hybrid to monthly."""
    prepared = _apply_period_source_policy_params(_apply_month_or_date_params(coalesce_params(params)))
    if not str(prepared.get("evaluation_month") or "").strip():
        return False, "missing_evaluation_month"
    policy = prepared.get("_period_source_policy") or {}
    if bool(policy.get("use_hybrid") or policy.get("use_hybrid_detail")):
        return False, "hybrid_detail_contract"
    if _resolve_source_mode(prepared) not in {"monthly_book", "monthly_real"}:
        return False, "non_monthly_source"
    if _dimension_values(prepared, "product_di_list", "dashboard_product_di_list", "product_class_list", "dashboard_product_class_list", "exclude_product_group_list", "exclude_product_di_list", "exclude_product_class_list"):
        return False, "product_dimension_filter_contract"
    product_group_values = _dimension_values(prepared, "product_group_list", "dashboard_product_group_list")
    if product_group_values:
        if not _supports_exact_product_group_scope(prepared):
            return False, "product_group_pair_contract"
        if str(prepared.get("product_group") or "").strip() or str(prepared.get("product_group_nm") or "").strip() or _dimension_values(prepared, "product_group_nm_list", "exclude_product_group_nm_list"):
            return False, "product_group_name_contract"
        return True, "monthly_product_group_contract"
    return True, "monthly_only_contract"


def _queries(params: Dict[str, Any]) -> tuple[dict[str, tuple[str, Dict[str, Any]]], dict[str, Any]]:
    cte, bind, meta = _sales_cte(params)
    spec = meta["spec"]
    p = spec["prefix"]
    code = _trim("V.제품코드")
    fields = [
        code, _trim("P.Rd04_Physic_Nm"), _trim("P.Rd04_Standard"), _trim("P.Rd04_Ven_Cd"), _trim("Make_Ven.Rd03_Ven_Nm"),
        _trim("P.Rd04_Physic_Group_Gcode"), _trim("P.Rd04_Physic_Group"), _trim("Physic_Group_Nm.Rd01_Hnm"),
        _trim("P.Rd04_Physic_Di_Gcode"), _trim("P.Rd04_Physic_Di"), _trim("Physic_Di_Nm.Rd01_Hnm"),
        _trim("P.Rd04_Physic_Tax_Gcode"), _trim("P.Rd04_Physic_Tax"), _trim("Physic_Tax_Nm.Rd01_Hnm"),
    ]
    identity = _identity_key(fields)
    product_month = cte + """
SELECT 기준월, 제품코드, SUM(출고수량) AS 출고수량, SUM(출고할증수량) AS 출고할증수량,
       SUM(매출공급가액) AS 매출공급가액, SUM(매출세액) AS 매출세액, SUM(매출합계) AS 매출합계, COUNT(*) AS 집계건수
FROM NumericSales GROUP BY 기준월, 제품코드 OPTION (RECOMPILE)
"""
    product_identity = cte + f"""
, ProductVendorCounts AS (SELECT 제품코드, COUNT(DISTINCT 매입처코드) AS 매입처수 FROM NumericSales GROUP BY 제품코드)
SELECT {identity} AS __dashboard_product_identity_id, {code} AS 제품코드,
       {_trim('P.Rd04_Physic_Nm')} AS 제품명, {_trim('P.Rd04_Standard')} AS 규격,
       {_trim('P.Rd04_Ven_Cd')} AS 제조사코드, {_trim('Make_Ven.Rd03_Ven_Nm')} AS 제조사명,
       {_trim('P.Rd04_Physic_Group_Gcode')} AS 제품그룹Gcode, {_trim('P.Rd04_Physic_Group')} AS 제품그룹코드, {_trim('Physic_Group_Nm.Rd01_Hnm')} AS 제품그룹명,
       {_trim('P.Rd04_Physic_Di_Gcode')} AS 제품구분Gcode, {_trim('P.Rd04_Physic_Di')} AS 제품구분코드, {_trim('Physic_Di_Nm.Rd01_Hnm')} AS 제품구분명,
       {_trim('P.Rd04_Physic_Tax_Gcode')} AS 제품분류Gcode, {_trim('P.Rd04_Physic_Tax')} AS 제품분류코드, {_trim('Physic_Tax_Nm.Rd01_Hnm')} AS 제품분류명,
       N'{spec['title']}' AS 분석자료원, V.매입처수
FROM ProductVendorCounts AS V
LEFT JOIN dbo.Rddbc040 AS P WITH (NOLOCK) ON P.Rd04_Physic_Cd = V.제품코드
LEFT JOIN dbo.Rddbc030 AS Make_Ven WITH (NOLOCK) ON Make_Ven.Rd03_Ven_Cd = P.Rd04_Ven_Cd
LEFT JOIN dbo.Rddbc010 AS Physic_Group_Nm WITH (NOLOCK) ON Physic_Group_Nm.Rd01_Gcode = P.Rd04_Physic_Group_Gcode AND Physic_Group_Nm.Rd01_Tcode = P.Rd04_Physic_Group
LEFT JOIN dbo.Rddbc010 AS Physic_Di_Nm WITH (NOLOCK) ON Physic_Di_Nm.Rd01_Gcode = P.Rd04_Physic_Di_Gcode AND Physic_Di_Nm.Rd01_Tcode = P.Rd04_Physic_Di
LEFT JOIN dbo.Rddbc010 AS Physic_Tax_Nm WITH (NOLOCK) ON Physic_Tax_Nm.Rd01_Gcode = P.Rd04_Physic_Tax_Gcode AND Physic_Tax_Nm.Rd01_Tcode = P.Rd04_Physic_Tax
OPTION (RECOMPILE)
"""
    relation = cte + "SELECT 기준월, 제품코드, 매입처코드 FROM NumericSales GROUP BY 기준월, 제품코드, 매입처코드 OPTION (RECOMPILE)"
    common, purchase_bind = _build_dashboard_monthly_common_predicates(meta["prepared"], spec, supplier_bind_prefix="narrow_purchase_supplier", stock_bind_prefix="narrow_purchase_stock_cd")
    purchase_bind = dict(purchase_bind)
    sales_universe_common, sales_universe_bind = _build_dashboard_monthly_common_predicates(
        meta["prepared"],
        spec,
        supplier_bind_prefix="narrow_purchase_supplier",
        stock_bind_prefix="narrow_purchase_stock_cd",
        table_alias="S",
    )
    purchase_bind.update(sales_universe_bind)
    purchase_sales_branch = _build_dashboard_sales_branch_predicates(
        meta["prepared"], spec, purchase_bind, table_alias="S"
    )
    purchase_product_scope_cte, purchase_product_scope_join, _purchase_product_scope_applied = _product_scope_cte(
        params=meta["prepared"],
        bind=purchase_bind,
        product_code_sql=_trim(f"S.{p}_Physic_Cd"),
    )
    purchase_bind.update({"narrow_history_month_from": str(meta["prepared"].get("dashboard_lite_history_month_from") or meta["prepared"]["month_from"]), "narrow_evaluation_month": str(meta["prepared"]["evaluation_month"])})
    in_qty = f"CAST(ISNULL(M.{p}_In_Quantity, 0) AS FLOAT) + CAST(ISNULL(M.{p}_In_Oquantity, 0) AS FLOAT)" if meta["source_mode"] == "monthly_real" else f"CAST(ISNULL(M.{p}_In_Quantity, 0) AS FLOAT)"
    purchase_ctes = []
    if purchase_product_scope_cte:
        purchase_ctes.append(purchase_product_scope_cte)
    purchase_ctes.extend((
        f"""FilteredSalesProducts AS (
 SELECT DISTINCT {_trim(f'S.{p}_Physic_Cd')} AS 제품코드
 FROM {spec['table']} AS S WITH (NOLOCK)
 {purchase_product_scope_join}
 WHERE 1 = 1 {_where(sales_universe_common)} {_where(purchase_sales_branch)}
)""",
        f"""PurchaseGrouped AS (
 SELECT LEFT(M.{p}_Stock_YyMm, 6) AS 기준월, {_trim(f'M.{p}_Physic_Cd')} AS 제품코드, {_trim(f'M.{p}_Ven_Cd')} AS 매입처코드,
        SUM({in_qty}) AS 입고수량, SUM(CAST(ISNULL(M.{p}_In_Supply_Price, 0) AS FLOAT)) AS 매입금액,
        SUM(CASE WHEN CAST(ISNULL(M.{p}_In_Supply_Price, 0) AS FLOAT) > 0 OR {in_qty} > 0 THEN 1 ELSE 0 END) AS 매입발생건수
 FROM {spec['table']} AS M WITH (NOLOCK)
 INNER JOIN FilteredSalesProducts AS FSP ON FSP.제품코드 = {_trim(f'M.{p}_Physic_Cd')}
 WHERE 1 = 1 {_where(common)}
 GROUP BY LEFT(M.{p}_Stock_YyMm, 6), M.{p}_Physic_Cd, M.{p}_Ven_Cd
)""",
    ))
    purchase = f"""
WITH {',\n'.join(purchase_ctes)},
Classified AS (
 SELECT *, CASE WHEN 제품코드 = N'' THEN 'missing_product_code' WHEN 기준월 = N'' THEN 'missing_month'
   WHEN 기준월 NOT LIKE '[0-9][0-9][0-9][0-9][0-9][0-9]' THEN 'other_excluded'
   WHEN 기준월 < %(narrow_history_month_from)s OR 기준월 >= %(narrow_evaluation_month)s THEN 'other_excluded' ELSE 'classified' END AS classification FROM PurchaseGrouped
), MonthTotals AS (SELECT 기준월, SUM(매입금액) AS 매입금액 FROM PurchaseGrouped WHERE 기준월 LIKE '[0-9][0-9][0-9][0-9][0-9][0-9]' GROUP BY 기준월)
SELECT 'purchase_month_total' AS projection_kind, 기준월, 매입금액, 0 AS purchase_source_rows, 0 AS purchase_positive_rows, 0 AS purchase_nonpositive_rows, 0 AS purchase_unclassified_rows, 0 AS missing_product_code_rows, 0 AS missing_month_rows, 0 AS invalid_numeric_rows, 0 AS other_excluded_rows FROM MonthTotals
UNION ALL
SELECT 'purchase_diagnostics', N'', CAST(0 AS FLOAT), COUNT(*), SUM(CASE WHEN classification = 'classified' AND (매입금액 > 1e-9 OR 입고수량 > 1e-9) THEN 1 ELSE 0 END), SUM(CASE WHEN classification = 'classified' AND NOT (매입금액 > 1e-9 OR 입고수량 > 1e-9) THEN 1 ELSE 0 END), SUM(CASE WHEN classification <> 'classified' THEN 1 ELSE 0 END), SUM(CASE WHEN classification = 'missing_product_code' THEN 1 ELSE 0 END), SUM(CASE WHEN classification = 'missing_month' THEN 1 ELSE 0 END), CAST(0 AS BIGINT), SUM(CASE WHEN classification = 'other_excluded' THEN 1 ELSE 0 END) FROM Classified
OPTION (RECOMPILE)
"""
    return {"product_month_sales": (product_month, bind), "product_identity": (product_identity, bind), "manufacturer_vendor_relation": (relation, bind), "purchase_facts": (purchase, purchase_bind)}, meta


def _manufacturer_month(product: pd.DataFrame, identity: pd.DataFrame, relation: pd.DataFrame) -> pd.DataFrame:
    identity_map = identity.loc[:, ["제품코드", "제조사명"]].copy()
    identity_map["제품코드"] = identity_map["제품코드"].fillna("").astype(str).str.strip()
    if identity_map["제품코드"].duplicated().any():
        raise ValueError("Dashboard narrow candidate requires one identity per product code")
    identity_map["제약사명"] = identity_map["제조사명"].fillna("").astype(str).str.strip().replace("", "제약사 미지정")
    product = product.merge(identity_map.loc[:, ["제품코드", "제약사명"]], on="제품코드", how="left", validate="many_to_one")
    if product["제약사명"].isna().any():
        raise ValueError("Dashboard narrow candidate product identity is incomplete")
    numeric = ["매출공급가액", "매출세액", "매출합계", "집계건수"]
    for col in numeric:
        product[col] = pd.to_numeric(product[col], errors="coerce").fillna(0)
    monetary = product.groupby(["제약사명", "기준월"], dropna=False, as_index=False).agg(매출공급가액=("매출공급가액", "sum"), 매출세액=("매출세액", "sum"), 매출합계=("매출합계", "sum"), 집계건수=("집계건수", "sum"), 제품수=("제품코드", "nunique"))
    relation = relation.drop_duplicates().merge(identity_map.loc[:, ["제품코드", "제약사명"]], on="제품코드", how="left", validate="many_to_one")
    if relation["제약사명"].isna().any():
        raise ValueError("Dashboard narrow candidate vendor relation identity is incomplete")
    vendors = relation.groupby(["제약사명", "기준월"], dropna=False)["매입처코드"].nunique().rename("매입처수").reset_index()
    return monetary.merge(vendors, on=["제약사명", "기준월"], how="left", validate="one_to_one").sort_values(["제약사명", "기준월"], kind="stable").reset_index(drop=True)


def _sales_facts(product: pd.DataFrame, identity: pd.DataFrame, manufacturer: pd.DataFrame) -> pd.DataFrame:
    product = product.merge(identity.loc[:, ["제품코드", "__dashboard_product_identity_id"]], on="제품코드", how="left", validate="many_to_one")
    if product["__dashboard_product_identity_id"].isna().any():
        raise ValueError("Dashboard narrow candidate product-month identity is incomplete")
    product["projection_kind"] = "product_month_sales"; product["제약사명"] = ""; product["제품수"] = 0; product["매입처수"] = 0
    manufacturer["projection_kind"] = "manufacturer_month"; manufacturer["__dashboard_product_identity_id"] = ""; manufacturer["제품코드"] = ""; manufacturer["출고수량"] = 0; manufacturer["출고할증수량"] = 0
    totals = product.groupby("기준월", dropna=False, as_index=False)["매출합계"].sum(); totals["projection_kind"] = "sales_month_total"
    for col in ("__dashboard_product_identity_id", "제품코드", "제약사명"):
        totals[col] = ""
    for col in ("출고수량", "출고할증수량", "매출공급가액", "매출세액", "집계건수", "제품수", "매입처수"):
        totals[col] = 0
    columns = ["projection_kind", "기준월", "__dashboard_product_identity_id", "제품코드", "제약사명", "출고수량", "출고할증수량", "매출공급가액", "매출세액", "매출합계", "집계건수", "제품수", "매입처수"]
    return pd.concat([product.reindex(columns=columns), manufacturer.reindex(columns=columns), totals.reindex(columns=columns)], ignore_index=True)


def load_dashboard_narrow_sales_candidate(params: Dict[str, Any]) -> dict[str, Any]:
    """Load the verified compact representation and record each physical SQL."""
    started = time.perf_counter()
    queries, meta = _queries(params)
    measurement = get_active_dashboard_query_measurement()
    frames: dict[str, pd.DataFrame] = {}
    details: dict[str, dict[str, int]] = {}
    for name, (sql, bind) in queries.items():
        phase_started = time.perf_counter()
        if measurement is not None:
            with dashboard_measurement_phase(measurement, phase=f"narrow_{name}", source="sales", source_mode=meta["source_mode"]) as state:
                frame = query_to_df(sql, bind)
                frame = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()
                state["result_rows"] = len(frame); state["result_cols"] = len(frame.columns)
        else:
            frame = query_to_df(sql, bind)
            frame = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()
        frames[name] = frame
        details[name] = {"elapsed_ms": int((time.perf_counter() - phase_started) * 1000), "row_count": len(frame), "column_count": len(frame.columns), "pandas_deep_memory_bytes": int(frame.memory_usage(index=True, deep=True).sum())}
    assembly_started = time.perf_counter()
    manufacturer_started = time.perf_counter()
    manufacturer = _manufacturer_month(frames["product_month_sales"].copy(), frames["product_identity"], frames["manufacturer_vendor_relation"])
    manufacturer_reconstruction_ms = int((time.perf_counter() - manufacturer_started) * 1000)
    bundle = build_dashboard_narrow_bundle_from_projections(frames["product_identity"], _sales_facts(frames["product_month_sales"].copy(), frames["product_identity"], manufacturer), frames["purchase_facts"])
    return {"bundle": bundle, "vendor_relation_df": frames["manufacturer_vendor_relation"], "perf": {"representation": "narrow_monthly_v1", "source_mode": meta["source_mode"], "physical_query_count": len(queries), "projection_results": details, "manufacturer_reconstruction_ms": manufacturer_reconstruction_ms, "bundle_assembly_ms": int((time.perf_counter() - assembly_started) * 1000), "elapsed_ms": int((time.perf_counter() - started) * 1000)}}
