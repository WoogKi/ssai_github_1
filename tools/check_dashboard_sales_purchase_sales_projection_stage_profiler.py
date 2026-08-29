"""Prepare or execute narrow, independent Dashboard sales projection stages.

This is a read-only diagnostic tool.  It does not change the production
Dashboard source routing and deliberately keeps the logical sales source as one
while exposing the expensive physical projection stages separately.
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
    coalesce_params,
    query_to_df,
)
from tools.check_dashboard_sales_purchase_db_multigrain_equality import dashboard_probe_contract
from tools.check_dashboard_sales_purchase_db_narrow_perf_probe import _identity_key, _trim, _where


SALES_FACT_COLUMNS = [
    "projection_kind", "기준월", "__dashboard_product_identity_id", "제품코드", "제약사명",
    "출고수량", "출고할증수량", "매출공급가액", "매출세액", "매출합계", "집계건수", "제품수", "매입처수",
]
PRODUCT_METRIC_COLUMNS = ["출고수량", "출고할증수량", "매출공급가액", "매출세액", "매출합계", "집계건수"]


def _sales_rows_cte(params: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    prepared = _apply_period_source_policy_params(_apply_month_or_date_params(coalesce_params(params)))
    policy = prepared.get("_period_source_policy") or {}
    if bool(policy.get("use_hybrid") or policy.get("use_hybrid_detail")):
        raise ValueError("sales stage profiler requires monthly-only policy; hybrid fallback is not allowed")
    source_mode = _resolve_source_mode(prepared)
    if source_mode not in {"monthly_book", "monthly_real"}:
        raise ValueError(f"monthly source required, got {source_mode}")
    spec = _monthly_spec(source_mode)
    prefix = spec["prefix"]
    common, bind = _build_dashboard_monthly_common_predicates(
        prepared, spec, supplier_bind_prefix="stage_supplier", stock_bind_prefix="stage_stock_cd"
    )
    sales_branch = _build_dashboard_sales_branch_predicates(prepared, spec, bind, table_alias="B")
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
), NumericSales AS (
    SELECT
        기준월,
        {_trim('제품코드')} AS 제품코드,
        {_trim('매입처코드')} AS 매입처코드,
        CASE WHEN LEFT({prefix}_Io_Gu, 1) = '6' THEN -1 * COALESCE({prefix}_Out_Quantity, 0) ELSE COALESCE({prefix}_Out_Quantity, 0) END AS 출고수량,
        CASE WHEN LEFT({prefix}_Io_Gu, 1) = '6' THEN -1 * COALESCE({prefix}_Out_Oquantity, 0) ELSE COALESCE({prefix}_Out_Oquantity, 0) END AS 출고할증수량,
        CASE WHEN LEFT({prefix}_Io_Gu, 1) = '6' THEN -1 * COALESCE({prefix}_Out_Supply_Price, 0) ELSE COALESCE({prefix}_Out_Supply_Price, 0) END AS 매출공급가액,
        CASE WHEN LEFT({prefix}_Io_Gu, 1) = '6' THEN -1 * COALESCE({prefix}_Out_Tax_Price, 0) ELSE COALESCE({prefix}_Out_Tax_Price, 0) END AS 매출세액,
        CASE WHEN LEFT({prefix}_Io_Gu, 1) = '6' THEN -1 * (COALESCE({prefix}_Out_Supply_Price, 0) + COALESCE({prefix}_Out_Tax_Price, 0)) ELSE COALESCE({prefix}_Out_Supply_Price, 0) + COALESCE({prefix}_Out_Tax_Price, 0) END AS 매출합계
    FROM SalesRows
)
"""
    return cte, bind, {"prepared": prepared, "spec": spec, "source_mode": source_mode}


def build_sales_projection_stage_sql(params: dict[str, Any]) -> tuple[dict[str, tuple[str, dict[str, Any]]], dict[str, Any]]:
    """Build independent numeric, identity, and manufacturer-month stages.

    The stages repeat the same authoritative monthly predicates, but avoid the
    old wide EnrichedSales + GROUPING SETS aggregate.  This is intentionally a
    profiler candidate, not a production routing change.
    """
    cte, bind, meta = _sales_rows_cte(params)
    spec = meta["spec"]
    product_code = _trim("V.제품코드")
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
    identity_fields = [
        product_code, product_name, standard, manufacturer_code, manufacturer_name,
        group_gcode, group_code, group_name, di_gcode, di_code, di_name, tax_gcode, tax_code, tax_name,
    ]
    identity_key = _identity_key(identity_fields)

    product_month_sql = cte + """
SELECT N'product_month_sales' AS projection_kind, 기준월, 제품코드,
       SUM(출고수량) AS 출고수량, SUM(출고할증수량) AS 출고할증수량,
       SUM(매출공급가액) AS 매출공급가액, SUM(매출세액) AS 매출세액,
       SUM(매출합계) AS 매출합계, COUNT(*) AS 집계건수
FROM NumericSales
GROUP BY 기준월, 제품코드
OPTION (RECOMPILE)
"""
    product_identity_sql = cte + f"""
, ProductVendorCounts AS (
    SELECT 제품코드, COUNT(DISTINCT 매입처코드) AS 매입처수
    FROM NumericSales
    GROUP BY 제품코드
)
SELECT {identity_key} AS __dashboard_product_identity_id,
       {product_code} AS 제품코드, {product_name} AS 제품명, {standard} AS 규격,
       {manufacturer_code} AS 제조사코드, {manufacturer_name} AS 제조사명,
       {group_gcode} AS 제품그룹Gcode, {group_code} AS 제품그룹코드, {group_name} AS 제품그룹명,
       {di_gcode} AS 제품구분Gcode, {di_code} AS 제품구분코드, {di_name} AS 제품구분명,
       {tax_gcode} AS 제품분류Gcode, {tax_code} AS 제품분류코드, {tax_name} AS 제품분류명,
       N'{spec['title']}' AS 분석자료원, V.매입처수
FROM ProductVendorCounts AS V
LEFT JOIN dbo.Rddbc040 AS P WITH (NOLOCK) ON P.Rd04_Physic_Cd = V.제품코드
LEFT JOIN dbo.Rddbc030 AS Make_Ven WITH (NOLOCK) ON Make_Ven.Rd03_Ven_Cd = P.Rd04_Ven_Cd
LEFT JOIN dbo.Rddbc010 AS Physic_Group_Nm WITH (NOLOCK) ON Physic_Group_Nm.Rd01_Gcode = P.Rd04_Physic_Group_Gcode AND Physic_Group_Nm.Rd01_Tcode = P.Rd04_Physic_Group
LEFT JOIN dbo.Rddbc010 AS Physic_Di_Nm WITH (NOLOCK) ON Physic_Di_Nm.Rd01_Gcode = P.Rd04_Physic_Di_Gcode AND Physic_Di_Nm.Rd01_Tcode = P.Rd04_Physic_Di
LEFT JOIN dbo.Rddbc010 AS Physic_Tax_Nm WITH (NOLOCK) ON Physic_Tax_Nm.Rd01_Gcode = P.Rd04_Physic_Tax_Gcode AND Physic_Tax_Nm.Rd01_Tcode = P.Rd04_Physic_Tax
OPTION (RECOMPILE)
"""
    manufacturer_sql = cte + f"""
, ProductVendorMonth AS (
    SELECT 기준월, 제품코드, 매입처코드,
           SUM(매출공급가액) AS 매출공급가액, SUM(매출세액) AS 매출세액,
           SUM(매출합계) AS 매출합계, COUNT(*) AS 집계건수
    FROM NumericSales
    GROUP BY 기준월, 제품코드, 매입처코드
), ManufacturerMapped AS (
    SELECT PVM.*, CASE WHEN {_trim('Make_Ven.Rd03_Ven_Nm')} = N'' THEN N'제약사 미지정' ELSE {_trim('Make_Ven.Rd03_Ven_Nm')} END AS 제약사명
    FROM ProductVendorMonth AS PVM
    LEFT JOIN dbo.Rddbc040 AS P WITH (NOLOCK) ON P.Rd04_Physic_Cd = PVM.제품코드
    LEFT JOIN dbo.Rddbc030 AS Make_Ven WITH (NOLOCK) ON Make_Ven.Rd03_Ven_Cd = P.Rd04_Ven_Cd
)
SELECT N'manufacturer_month' AS projection_kind, 기준월, 제약사명,
       SUM(매출공급가액) AS 매출공급가액, SUM(매출세액) AS 매출세액,
       SUM(매출합계) AS 매출합계, SUM(집계건수) AS 집계건수,
       COUNT(DISTINCT 제품코드) AS 제품수, COUNT(DISTINCT 매입처코드) AS 매입처수
FROM ManufacturerMapped
GROUP BY 기준월, 제약사명
OPTION (RECOMPILE)
"""
    return {
        "product_month_sales": (product_month_sql, bind),
        "product_identity": (product_identity_sql, bind),
        "manufacturer_month": (manufacturer_sql, bind),
    }, {
        "source_mode": meta["source_mode"],
        "logical_source_call_count": 1,
        "physical_query_count": 3,
        "projection_names": ["product_month_sales", "product_identity", "manufacturer_month", "sales_month_total_derived"],
        "sales_month_total_derivation": "product_month_sales.매출합계 grouped by 기준월",
        "hybrid_supported": False,
    }


def assemble_sales_facts_from_stages(
    product_month_df: pd.DataFrame,
    product_identity_df: pd.DataFrame,
    manufacturer_month_df: pd.DataFrame,
) -> pd.DataFrame:
    """Assemble the existing narrow sales_facts shape without a DB rollup union."""
    product = product_month_df.copy()
    identity = product_identity_df.copy()
    if "제품코드" not in identity.columns or identity["제품코드"].duplicated().any():
        raise ValueError("stage product_identity must provide one identity row per product code")
    product = product.merge(
        identity.loc[:, ["제품코드", "__dashboard_product_identity_id"]],
        on="제품코드", how="left", validate="many_to_one",
    )
    if product["__dashboard_product_identity_id"].isna().any():
        raise ValueError("stage product_month_sales has product code missing from product_identity")
    product["projection_kind"] = "product_month_sales"
    product["제약사명"] = ""
    product["제품수"] = 0
    product["매입처수"] = 0
    product = product.reindex(columns=SALES_FACT_COLUMNS)

    manufacturer = manufacturer_month_df.copy()
    manufacturer["projection_kind"] = "manufacturer_month"
    manufacturer["__dashboard_product_identity_id"] = ""
    manufacturer["제품코드"] = ""
    manufacturer["출고수량"] = 0
    manufacturer["출고할증수량"] = 0
    manufacturer = manufacturer.reindex(columns=SALES_FACT_COLUMNS)

    totals = (
        product.loc[:, ["기준월", "매출합계"]].groupby("기준월", dropna=False, as_index=False).sum()
        if not product.empty else pd.DataFrame(columns=["기준월", "매출합계"])
    )
    totals["projection_kind"] = "sales_month_total"
    for column in SALES_FACT_COLUMNS:
        if column not in totals.columns:
            totals[column] = "" if column in {"__dashboard_product_identity_id", "제품코드", "제약사명"} else 0
    totals = totals.reindex(columns=SALES_FACT_COLUMNS)
    return pd.concat([product, manufacturer, totals], ignore_index=True)


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
        "equality": {
            "offline_narrow_bundle_contract": "RUN_STATIC_GATE",
            "actual_legacy_same_source": "UNPROVEN_NOT_EXECUTED",
        },
    }
    _write(output, result)
    try:
        queries, plan = build_sales_projection_stage_sql(params)
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
                    "row_count": int(len(frame)),
                    "column_count": int(len(frame.columns)),
                    "pandas_deep_memory_bytes": int(frame.memory_usage(index=True, deep=True).sum()),
                }
                result["projection_results"] = details
                _write(output, result)
            assembled = assemble_sales_facts_from_stages(
                frames["product_month_sales"], frames["product_identity"], frames["manufacturer_month"]
            )
            details["sales_month_total_derived"] = {
                "elapsed_ms": 0,
                "row_count": int(assembled["projection_kind"].eq("sales_month_total").sum()),
                "column_count": 2,
                "pandas_deep_memory_bytes": int(assembled.loc[assembled["projection_kind"].eq("sales_month_total"), ["기준월", "매출합계"]].memory_usage(index=True, deep=True).sum()),
            }
            result.update({
                "status": "EXECUTED",
                "physical_query_count": len(queries),
                "projection_results": details,
                "total_return_rows": int(sum(details[name]["row_count"] for name in queries)),
                "estimated_transport_bytes": int(sum(details[name]["pandas_deep_memory_bytes"] for name in queries)),
                "assembled_sales_facts_rows": int(len(assembled)),
                "equality": {
                    "offline_narrow_bundle_contract": "RUN_STATIC_GATE",
                    "actual_legacy_same_source": "UNPROVEN_NOT_EXECUTED",
                    "stage_shape": "PASS",
                },
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
    parser = argparse.ArgumentParser(description="Prepare or run one read-only Dashboard sales stage profiler.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true", help="Run each of the three read-only sales stages exactly once.")
    parser.add_argument("--query-timeout-seconds", type=int, default=120)
    args = parser.parse_args()
    if int(args.query_timeout_seconds) <= 0:
        raise SystemExit("query timeout must be positive")
    raise SystemExit(main(args.output, execute=bool(args.execute), timeout_seconds=int(args.query_timeout_seconds)))
