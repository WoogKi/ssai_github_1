"""Prepare or run one read-only DB-side narrow Dashboard sales bundle probe.

The probe is deliberately separate from production routing.  It emits three
narrow physical projections while preserving one logical sales source.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import event

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import mssql_client
from app.services.analytics_sales_trend_service import (
    _apply_month_or_date_params,
    _apply_period_source_policy_params,
    _build_dashboard_monthly_common_predicates,
    _build_dashboard_sales_branch_predicates,
    _monthly_spec,
    _resolve_source_mode,
    build_dashboard_narrow_bundle_from_projections,
    coalesce_params,
    query_to_df,
)
from tools.check_dashboard_sales_purchase_db_multigrain_equality import dashboard_probe_contract


def _where(clauses: list[str]) -> str:
    return ("\n  AND " + "\n  AND ".join(clauses)) if clauses else ""


def _trim(sql: str) -> str:
    return f"COALESCE(LTRIM(RTRIM(CONVERT(NVARCHAR(255), {sql}))), N'')"


def _identity_key(fields: list[str]) -> str:
    parts = [f"CONVERT(NVARCHAR(10), LEN({field})), N':', {field}, N'|'" for field in fields]
    return "CONCAT(" + ", ".join(parts) + ")"


def _sales_cte(params: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    prepared = _apply_period_source_policy_params(_apply_month_or_date_params(coalesce_params(params)))
    policy = prepared.get("_period_source_policy") or {}
    if bool(policy.get("use_hybrid") or policy.get("use_hybrid_detail")):
        raise ValueError("narrow DB probe does not support hybrid monthly/detail policy; no monthly fallback is allowed")
    source_mode = _resolve_source_mode(prepared)
    if source_mode not in {"monthly_book", "monthly_real"}:
        raise ValueError(f"monthly source required, got {source_mode}")
    spec = _monthly_spec(source_mode)
    prefix = spec["prefix"]
    common, bind = _build_dashboard_monthly_common_predicates(
        prepared, spec, supplier_bind_prefix="narrow_supplier", stock_bind_prefix="narrow_stock_cd"
    )
    sales_branch = _build_dashboard_sales_branch_predicates(prepared, spec, bind, table_alias="B")
    product_code = _trim("B.제품코드")
    product_name = _trim("P.Rd04_Physic_Nm")
    standard = _trim("P.Rd04_Standard")
    manufacturer_code = _trim("P.Rd04_Ven_Cd")
    manufacturer_name = _trim("Make_Ven.Rd03_Ven_Nm")
    group_gcode = _trim("P.Rd04_Physic_Group_Gcode")
    group_code = _trim("P.Rd04_Physic_Group")
    group_name = _trim("Physic_Group_Nm.Rd01_Hnm")
    di_gcode = _trim("P.Rd04_Physic_Di_Gcode")
    di_code = _trim("P.Rd04_Physic_Di")
    di_name = _trim("Physic_Di_Nm.Rd01_Hnm")
    tax_gcode = _trim("P.Rd04_Physic_Tax_Gcode")
    tax_code = _trim("P.Rd04_Physic_Tax")
    tax_name = _trim("Physic_Tax_Nm.Rd01_Hnm")
    identity_fields = [product_code, product_name, standard, manufacturer_code, manufacturer_name, group_gcode, group_code, group_name, di_gcode, di_code, di_name, tax_gcode, tax_code, tax_name]
    key = _identity_key(identity_fields)
    cte = f"""
WITH BaseRows AS (
    SELECT
        LEFT(M.{prefix}_Stock_YyMm, 6) AS 기준월,
        M.{prefix}_Physic_Cd AS 제품코드,
        M.{prefix}_Ven_Cd AS 매입처코드,
        M.{prefix}_Io_Gu_Gcode AS {prefix}_Io_Gu_Gcode,
        M.{prefix}_Io_Gu AS {prefix}_Io_Gu,
        M.{prefix}_Out_Quantity AS {prefix}_Out_Quantity,
        M.{prefix}_Out_Oquantity AS {prefix}_Out_Oquantity,
        M.{prefix}_Out_Supply_Price AS {prefix}_Out_Supply_Price,
        M.{prefix}_Out_Tax_Price AS {prefix}_Out_Tax_Price
    FROM {spec['table']} AS M WITH (NOLOCK)
    WHERE 1 = 1 {_where(common)}
), SalesRows AS (
    SELECT * FROM BaseRows AS B
    WHERE 1 = 1 {_where(sales_branch)}
), EnrichedSales AS (
    SELECT
        B.기준월, {product_code} AS 제품코드, {_trim('B.매입처코드')} AS 매입처코드,
        {key} AS __dashboard_product_identity_id,
        {product_name} AS 제품명, {standard} AS 규격,
        {manufacturer_code} AS 제조사코드, {manufacturer_name} AS 제조사명,
        {group_gcode} AS 제품그룹Gcode, {group_code} AS 제품그룹코드, {group_name} AS 제품그룹명,
        {di_gcode} AS 제품구분Gcode, {di_code} AS 제품구분코드, {di_name} AS 제품구분명,
        {tax_gcode} AS 제품분류Gcode, {tax_code} AS 제품분류코드, {tax_name} AS 제품분류명,
        CASE WHEN {manufacturer_name} = N'' THEN N'제약사 미지정' ELSE {manufacturer_name} END AS 제약사명,
        CASE WHEN LEFT(B.{prefix}_Io_Gu, 1) = '6' THEN -1 * COALESCE(B.{prefix}_Out_Quantity, 0) ELSE COALESCE(B.{prefix}_Out_Quantity, 0) END AS 출고수량,
        CASE WHEN LEFT(B.{prefix}_Io_Gu, 1) = '6' THEN -1 * COALESCE(B.{prefix}_Out_Oquantity, 0) ELSE COALESCE(B.{prefix}_Out_Oquantity, 0) END AS 출고할증수량,
        CASE WHEN LEFT(B.{prefix}_Io_Gu, 1) = '6' THEN -1 * COALESCE(B.{prefix}_Out_Supply_Price, 0) ELSE COALESCE(B.{prefix}_Out_Supply_Price, 0) END AS 매출공급가액,
        CASE WHEN LEFT(B.{prefix}_Io_Gu, 1) = '6' THEN -1 * COALESCE(B.{prefix}_Out_Tax_Price, 0) ELSE COALESCE(B.{prefix}_Out_Tax_Price, 0) END AS 매출세액,
        CASE WHEN LEFT(B.{prefix}_Io_Gu, 1) = '6' THEN -1 * (COALESCE(B.{prefix}_Out_Supply_Price, 0) + COALESCE(B.{prefix}_Out_Tax_Price, 0)) ELSE COALESCE(B.{prefix}_Out_Supply_Price, 0) + COALESCE(B.{prefix}_Out_Tax_Price, 0) END AS 매출합계
    FROM SalesRows AS B
    LEFT JOIN dbo.Rddbc040 AS P WITH (NOLOCK) ON P.Rd04_Physic_Cd = B.제품코드
    LEFT JOIN dbo.Rddbc030 AS Make_Ven WITH (NOLOCK) ON Make_Ven.Rd03_Ven_Cd = P.Rd04_Ven_Cd
    LEFT JOIN dbo.Rddbc010 AS Physic_Group_Nm WITH (NOLOCK) ON Physic_Group_Nm.Rd01_Gcode = P.Rd04_Physic_Group_Gcode AND Physic_Group_Nm.Rd01_Tcode = P.Rd04_Physic_Group
    LEFT JOIN dbo.Rddbc010 AS Physic_Di_Nm WITH (NOLOCK) ON Physic_Di_Nm.Rd01_Gcode = P.Rd04_Physic_Di_Gcode AND Physic_Di_Nm.Rd01_Tcode = P.Rd04_Physic_Di
    LEFT JOIN dbo.Rddbc010 AS Physic_Tax_Nm WITH (NOLOCK) ON Physic_Tax_Nm.Rd01_Gcode = P.Rd04_Physic_Tax_Gcode AND Physic_Tax_Nm.Rd01_Tcode = P.Rd04_Physic_Tax
)
"""
    return cte, bind, {"prepared": prepared, "spec": spec, "source_mode": source_mode}


def build_narrow_projection_sql(params: dict[str, Any]) -> tuple[dict[str, tuple[str, dict[str, Any]]], dict[str, Any]]:
    sales_cte, bind, meta = _sales_cte(params)
    spec = meta["spec"]
    prefix = spec["prefix"]
    identity_sql = sales_cte + f"""
SELECT __dashboard_product_identity_id, 제품코드, 제품명, 규격, 제조사코드, 제조사명,
       제품그룹Gcode, 제품그룹코드, 제품그룹명, 제품구분Gcode, 제품구분코드, 제품구분명,
       제품분류Gcode, 제품분류코드, 제품분류명, N'{spec['title']}' AS 분석자료원,
       COUNT(DISTINCT 매입처코드) AS 매입처수
FROM EnrichedSales
GROUP BY __dashboard_product_identity_id, 제품코드, 제품명, 규격, 제조사코드, 제조사명,
         제품그룹Gcode, 제품그룹코드, 제품그룹명, 제품구분Gcode, 제품구분코드, 제품구분명,
         제품분류Gcode, 제품분류코드, 제품분류명
OPTION (RECOMPILE)
"""
    sales_sql = sales_cte + """
, Grouped AS (
    SELECT 기준월, 제품코드, __dashboard_product_identity_id, 제약사명,
           GROUPING(제품코드) AS product_rollup, GROUPING(제약사명) AS manufacturer_rollup,
           SUM(출고수량) AS 출고수량, SUM(출고할증수량) AS 출고할증수량,
           SUM(매출공급가액) AS 매출공급가액, SUM(매출세액) AS 매출세액, SUM(매출합계) AS 매출합계,
           COUNT(*) AS 집계건수, COUNT(DISTINCT 제품코드) AS 제품수, COUNT(DISTINCT 매입처코드) AS 매입처수
    FROM EnrichedSales
    GROUP BY GROUPING SETS ((기준월, 제품코드, __dashboard_product_identity_id), (기준월, 제약사명), (기준월))
)
SELECT CASE WHEN product_rollup = 0 THEN 'product_month_sales'
            WHEN manufacturer_rollup = 0 THEN 'manufacturer_month'
            ELSE 'sales_month_total' END AS projection_kind,
       기준월, __dashboard_product_identity_id, 제품코드, 제약사명,
       출고수량, 출고할증수량, 매출공급가액, 매출세액, 매출합계, 집계건수, 제품수, 매입처수
FROM Grouped
OPTION (RECOMPILE)
"""
    common, purchase_bind = _build_dashboard_monthly_common_predicates(
        meta["prepared"], spec, supplier_bind_prefix="narrow_purchase_supplier", stock_bind_prefix="narrow_purchase_stock_cd"
    )
    merged_bind = dict(bind)
    merged_bind.update(purchase_bind)
    in_qty = f"CAST(ISNULL(M.{prefix}_In_Quantity, 0) AS FLOAT) + CAST(ISNULL(M.{prefix}_In_Oquantity, 0) AS FLOAT)" if meta["source_mode"] == "monthly_real" else f"CAST(ISNULL(M.{prefix}_In_Quantity, 0) AS FLOAT)"
    history_from = str(meta["prepared"].get("dashboard_lite_history_month_from") or meta["prepared"]["month_from"])
    evaluation = str(meta["prepared"]["evaluation_month"])
    merged_bind.update({"narrow_history_month_from": history_from, "narrow_evaluation_month": evaluation})
    purchase_sql = f"""
WITH PurchaseGrouped AS (
    SELECT LEFT(M.{prefix}_Stock_YyMm, 6) AS 기준월,
           {_trim(f'M.{prefix}_Physic_Cd')} AS 제품코드,
           {_trim(f'M.{prefix}_Ven_Cd')} AS 매입처코드,
           SUM({in_qty}) AS 입고수량,
           SUM(CAST(ISNULL(M.{prefix}_In_Supply_Price, 0) AS FLOAT)) AS 매입금액,
           SUM(CASE WHEN CAST(ISNULL(M.{prefix}_In_Supply_Price, 0) AS FLOAT) > 0 OR {in_qty} > 0 THEN 1 ELSE 0 END) AS 매입발생건수
    FROM {spec['table']} AS M WITH (NOLOCK)
    WHERE 1 = 1 {_where(common)}
    GROUP BY LEFT(M.{prefix}_Stock_YyMm, 6), M.{prefix}_Physic_Cd, M.{prefix}_Ven_Cd
), Classified AS (
    SELECT *, CASE
        WHEN 제품코드 = N'' THEN 'missing_product_code'
        WHEN 기준월 = N'' THEN 'missing_month'
        WHEN 기준월 NOT LIKE '[0-9][0-9][0-9][0-9][0-9][0-9]' THEN 'other_excluded'
        WHEN 기준월 < %(narrow_history_month_from)s OR 기준월 >= %(narrow_evaluation_month)s THEN 'other_excluded'
        ELSE 'classified' END AS classification
    FROM PurchaseGrouped
), MonthTotals AS (
    SELECT 기준월, SUM(매입금액) AS 매입금액
    FROM PurchaseGrouped
    WHERE 기준월 LIKE '[0-9][0-9][0-9][0-9][0-9][0-9]'
    GROUP BY 기준월
)
SELECT 'purchase_month_total' AS projection_kind, 기준월, 매입금액,
       0 AS purchase_source_rows, 0 AS purchase_positive_rows, 0 AS purchase_nonpositive_rows,
       0 AS purchase_unclassified_rows, 0 AS missing_product_code_rows, 0 AS missing_month_rows,
       0 AS invalid_numeric_rows, 0 AS other_excluded_rows
FROM MonthTotals
UNION ALL
SELECT 'purchase_diagnostics', N'', CAST(0 AS FLOAT),
       COUNT(*),
       SUM(CASE WHEN classification = 'classified' AND (매입금액 > 1e-9 OR 입고수량 > 1e-9) THEN 1 ELSE 0 END),
       SUM(CASE WHEN classification = 'classified' AND NOT (매입금액 > 1e-9 OR 입고수량 > 1e-9) THEN 1 ELSE 0 END),
       SUM(CASE WHEN classification <> 'classified' THEN 1 ELSE 0 END),
       SUM(CASE WHEN classification = 'missing_product_code' THEN 1 ELSE 0 END),
       SUM(CASE WHEN classification = 'missing_month' THEN 1 ELSE 0 END),
       CAST(0 AS BIGINT),
       SUM(CASE WHEN classification = 'other_excluded' THEN 1 ELSE 0 END)
FROM Classified
OPTION (RECOMPILE)
"""
    return {
        "product_identity": (identity_sql, bind),
        "sales_facts": (sales_sql, bind),
        "purchase_facts": (purchase_sql, merged_bind),
    }, {
        "source_mode": meta["source_mode"], "physical_query_count": 3,
        "logical_source_call_count": 1, "hybrid_supported": False,
        "projection_names": ["product_identity", "product_month_sales", "manufacturer_month", "sales_month_total", "purchase_month_total", "purchase_diagnostics"],
        "excluded_grains": ["purchase_product_vendor", "manufacturer_vendor", "legacy_sales", "legacy_purchase_vendor", "inbound", "stock"],
    }


def _write(path: Path | None, value: dict[str, Any]) -> None:
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def main(output: Path | None, *, execute: bool, timeout_seconds: int) -> int:
    params, contract = dashboard_probe_contract()
    result: dict[str, Any] = {
        "status": "PREPARED", "started_at": datetime.now().isoformat(timespec="seconds"),
        "company_id": 8, "retry_count": 0, "statement_timeout_seconds": int(timeout_seconds),
        "source_call_count_contract": 3, "dashboard_contract": contract,
    }
    _write(output, result)
    try:
        queries, plan = build_narrow_projection_sql(params)
        result["plan"] = plan
        if not execute:
            result.update({"actual_one_shot_ready": True, "finished_at": datetime.now().isoformat(timespec="seconds")})
            _write(output, result)
            return 0
        engine = mssql_client.get_engine()
        def _set_timeout(_conn: Any, cursor: Any, _statement: str, _parameters: Any, _context: Any, _executemany: bool) -> None:
            cursor.timeout = int(timeout_seconds)
        event.listen(engine, "before_cursor_execute", _set_timeout)
        try:
            mssql_client.set_current_company_id(8)
            frames: dict[str, pd.DataFrame] = {}
            details: dict[str, dict[str, int]] = {}
            for name, (sql, bind) in queries.items():
                started = time.perf_counter()
                frame = query_to_df(sql, bind)
                frame = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()
                frames[name] = frame
                details[name] = {
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "row_count": int(len(frame)), "column_count": int(len(frame.columns)),
                    "pandas_deep_memory_bytes": int(frame.memory_usage(index=True, deep=True).sum()),
                }
                result["projection_results"] = details
                _write(output, result)
            bundle = build_dashboard_narrow_bundle_from_projections(
                frames["product_identity"], frames["sales_facts"], frames["purchase_facts"]
            )
            result.update({
                "status": "EXECUTED", "physical_query_count": len(queries),
                "projection_results": details,
                "total_return_rows": int(sum(item["row_count"] for item in details.values())),
                "estimated_transport_bytes": int(sum(item["pandas_deep_memory_bytes"] for item in details.values())),
                "assembled_bundle": {"product_identity_rows": len(bundle.product_identity_df), "product_month_sales_rows": len(bundle.product_month_sales_df), "manufacturer_month_rows": len(bundle.manufacturer_month_df), "sales_month_total_rows": len(bundle.sales_month_total_df), "purchase_month_total_rows": len(bundle.purchase_month_total_df)},
            })
            return 0
        finally:
            event.remove(engine, "before_cursor_execute", _set_timeout)
            mssql_client.set_current_company_id(None)
    except Exception as exc:
        result.update({"status": "FAIL", "exception_type": type(exc).__name__, "exception": str(exc), "traceback": traceback.format_exc(limit=4)})
        return 1
    finally:
        result["finished_at"] = datetime.now().isoformat(timespec="seconds")
        _write(output, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prepare or run one read-only narrow Dashboard sales probe.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true", help="Run exactly three read-only narrow projection statements.")
    parser.add_argument("--query-timeout-seconds", type=int, default=120)
    args = parser.parse_args()
    if int(args.query_timeout_seconds) <= 0:
        raise SystemExit("query timeout must be positive")
    raise SystemExit(main(args.output, execute=bool(args.execute), timeout_seconds=int(args.query_timeout_seconds)))
