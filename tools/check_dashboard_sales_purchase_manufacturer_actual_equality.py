"""Prepare or run a same-source equality gate for Dashboard manufacturer facts.

One canonical sales statement is materialized once into a DataFrame.  Both the
legacy narrow-bundle baseline and vendor-relation reconstruction are derived
from that exact in-process frame, avoiding sequential NOLOCK reads as an
equality signal.
"""
from __future__ import annotations

import argparse
import json
import math
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
from app.services.analytics_sales_trend_service import build_dashboard_narrow_sales_purchase_bundle, query_to_df
from tools.check_dashboard_sales_purchase_db_multigrain_equality import dashboard_probe_contract
from tools.check_dashboard_sales_purchase_manufacturer_vendor_relation_probe import (
    MANUFACTURER_MONTH_COLUMNS,
    VENDOR_RELATION_COLUMNS,
    build_manufacturer_month_from_narrow_projections,
)
from tools.check_dashboard_sales_purchase_sales_projection_stage_profiler import _sales_rows_cte


KEY_COLUMNS = ["제약사명", "기준월"]


def build_same_source_manufacturer_equality_sql(params: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Build one canonical source statement used by both comparison paths."""
    cte, bind, meta = _sales_rows_cte(params)
    spec = meta["spec"]
    sql = cte + f"""
, CanonicalSales AS (
    SELECT 기준월, 제품코드, 매입처코드,
           SUM(출고수량) AS 출고수량, SUM(출고할증수량) AS 출고할증수량,
           SUM(매출공급가액) AS 매출공급가액, SUM(매출세액) AS 매출세액,
           SUM(매출합계) AS 매출합계, COUNT(*) AS 집계건수
    FROM NumericSales
    GROUP BY 기준월, 제품코드, 매입처코드
)
SELECT C.기준월, C.제품코드,
       COALESCE(LTRIM(RTRIM(CONVERT(NVARCHAR(255), P.Rd04_Physic_Nm))), N'') AS 제품명,
       COALESCE(LTRIM(RTRIM(CONVERT(NVARCHAR(255), P.Rd04_Standard))), N'') AS 규격,
       COALESCE(LTRIM(RTRIM(CONVERT(NVARCHAR(255), P.Rd04_Ven_Cd))), N'') AS 제조사코드,
       COALESCE(LTRIM(RTRIM(CONVERT(NVARCHAR(255), Make_Ven.Rd03_Ven_Nm))), N'') AS 제조사명,
       C.매입처코드, C.출고수량, C.출고할증수량, C.매출공급가액, C.매출세액, C.매출합계, C.집계건수,
       N'{spec['title']}' AS 분석자료원
FROM CanonicalSales AS C
LEFT JOIN dbo.Rddbc040 AS P WITH (NOLOCK) ON P.Rd04_Physic_Cd = C.제품코드
LEFT JOIN dbo.Rddbc030 AS Make_Ven WITH (NOLOCK) ON Make_Ven.Rd03_Ven_Cd = P.Rd04_Ven_Cd
OPTION (RECOMPILE)
"""
    plan = {
        "source_mode": meta["source_mode"],
        "logical_source_call_count": 1,
        "physical_query_count": 1,
        "canonical_grain": ["기준월", "제품코드", "매입처코드"],
        "comparison_boundary": "one SQL result materialized once before both Python paths",
        "hybrid_supported": False,
    }
    return sql, bind, plan


def _json_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return str(value)


def compare_manufacturer_frames(legacy: pd.DataFrame, reconstructed: pd.DataFrame) -> list[dict[str, Any]]:
    """Return every mismatched key/value/delta for an exact same-source comparison."""
    left = legacy.reindex(columns=MANUFACTURER_MONTH_COLUMNS).copy()
    right = reconstructed.reindex(columns=MANUFACTURER_MONTH_COLUMNS).copy()
    for frame in (left, right):
        for column in KEY_COLUMNS:
            frame[column] = frame[column].fillna("").astype(str).str.strip()
    merged = left.merge(right, on=KEY_COLUMNS, how="outer", suffixes=("_legacy", "_reconstructed"), indicator=True)
    mismatches: list[dict[str, Any]] = []
    value_columns = [column for column in MANUFACTURER_MONTH_COLUMNS if column not in KEY_COLUMNS]
    for _, row in merged.iterrows():
        differences: dict[str, dict[str, Any]] = {}
        if row["_merge"] != "both":
            differences["row_presence"] = {"legacy": row["_merge"] != "right_only", "reconstructed": row["_merge"] != "left_only", "delta": None}
        for column in value_columns:
            legacy_value = row.get(f"{column}_legacy")
            reconstructed_value = row.get(f"{column}_reconstructed")
            both_missing = pd.isna(legacy_value) and pd.isna(reconstructed_value)
            if both_missing:
                continue
            if column in {"제품수", "매입처수", "집계건수"}:
                equal = pd.to_numeric(pd.Series([legacy_value]), errors="coerce").fillna(0).iloc[0] == pd.to_numeric(pd.Series([reconstructed_value]), errors="coerce").fillna(0).iloc[0]
            else:
                left_num = pd.to_numeric(pd.Series([legacy_value]), errors="coerce").iloc[0]
                right_num = pd.to_numeric(pd.Series([reconstructed_value]), errors="coerce").iloc[0]
                equal = not (pd.isna(left_num) or pd.isna(right_num)) and math.isclose(float(left_num), float(right_num), rel_tol=0.0, abs_tol=0.0)
            if not equal:
                left_num = pd.to_numeric(pd.Series([legacy_value]), errors="coerce").iloc[0]
                right_num = pd.to_numeric(pd.Series([reconstructed_value]), errors="coerce").iloc[0]
                differences[column] = {
                    "legacy": _json_value(legacy_value),
                    "reconstructed": _json_value(reconstructed_value),
                    "delta": None if pd.isna(left_num) or pd.isna(right_num) else float(right_num - left_num),
                }
        if differences:
            mismatches.append({"key": {column: str(row[column]) for column in KEY_COLUMNS}, "differences": differences})
    return mismatches


def run_same_source_reconstruction(canonical_sales: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, list[dict[str, Any]]]:
    """Derive both manufacturer paths from the one canonical source frame."""
    baseline = build_dashboard_narrow_sales_purchase_bundle(
        canonical_sales, pd.DataFrame(), evaluation_month="202608", history_month_from="202507"
    )
    relations = canonical_sales.loc[:, VENDOR_RELATION_COLUMNS].copy()
    reconstructed = build_manufacturer_month_from_narrow_projections(
        baseline.product_month_sales_df.drop(columns=["__dashboard_product_identity_id"]),
        baseline.product_identity_df,
        relations,
    )
    legacy = baseline.manufacturer_month_df.reindex(columns=MANUFACTURER_MONTH_COLUMNS)
    return legacy, reconstructed, compare_manufacturer_frames(legacy, reconstructed)


def _write(path: Path | None, value: dict[str, Any]) -> None:
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def main(output: Path | None, *, execute: bool, timeout_seconds: int) -> int:
    params, contract = dashboard_probe_contract()
    result: dict[str, Any] = {
        "status": "UNPROVABLE", "started_at": datetime.now().isoformat(timespec="seconds"),
        "company_id": 8, "retry_count": 0, "statement_timeout_seconds": int(timeout_seconds),
        "source_call_count_contract": 3, "dashboard_contract": contract,
        "determination": "UNPROVABLE_NOT_EXECUTED",
        "physical_query_count": 0,
    }
    _write(output, result)
    try:
        sql, bind, plan = build_same_source_manufacturer_equality_sql(params)
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
            started = time.perf_counter()
            canonical = query_to_df(sql, bind)
            canonical = canonical if isinstance(canonical, pd.DataFrame) else pd.DataFrame()
            legacy, reconstructed, mismatches = run_same_source_reconstruction(canonical)
            result.update({
                "status": "PASS" if not mismatches else "FAIL",
                "determination": "PASS_SAME_MATERIALIZED_SOURCE" if not mismatches else "FAIL_SAME_MATERIALIZED_SOURCE",
                "physical_query_count": 1,
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "canonical_source_row_count": int(len(canonical)),
                "legacy_row_count": int(len(legacy)),
                "reconstructed_row_count": int(len(reconstructed)),
                "mismatch_count": int(len(mismatches)),
                "mismatches": mismatches,
                "nolock_residual_risk": "The source can be transactionally inconsistent, but both comparison paths use the same materialized DataFrame; it cannot create a cross-path drift mismatch.",
            })
            return 0 if not mismatches else 1
        finally:
            event.remove(engine, "before_cursor_execute", _set_timeout)
            mssql_client.set_current_company_id(None)
    except Exception as exc:
        result.update({"status": "UNPROVABLE", "determination": "UNPROVABLE_EXECUTION_ERROR", "exception_type": type(exc).__name__, "exception": str(exc), "traceback": traceback.format_exc(limit=4)})
        return 1
    finally:
        result["finished_at"] = datetime.now().isoformat(timespec="seconds")
        _write(output, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prepare or run one same-source manufacturer reconstruction equality gate.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true", help="Run one canonical read-only source statement exactly once.")
    parser.add_argument("--query-timeout-seconds", type=int, default=120)
    args = parser.parse_args()
    if int(args.query_timeout_seconds) <= 0:
        raise SystemExit("query timeout must be positive")
    raise SystemExit(main(args.output, execute=bool(args.execute), timeout_seconds=int(args.query_timeout_seconds)))
