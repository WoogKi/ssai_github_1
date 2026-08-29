"""Prepare or execute the minimal vendor relation projection for Dashboard sales.

The production route is intentionally untouched.  This tool separates the
only manufacturer-month fact not recoverable from product-month sales: the
normalized manufacturer/month/vendor relationship required for vendor nunique.
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
from app.services.analytics_sales_trend_service import _dashboard_normalized_manufacturer, query_to_df
from tools.check_dashboard_sales_purchase_db_multigrain_equality import dashboard_probe_contract
from tools.check_dashboard_sales_purchase_sales_projection_stage_profiler import _sales_rows_cte


MANUFACTURER_MONTH_COLUMNS = [
    "제약사명", "기준월", "매출공급가액", "매출세액", "매출합계", "집계건수", "제품수", "매입처수",
]
VENDOR_RELATION_COLUMNS = ["기준월", "제품코드", "매입처코드"]


def build_manufacturer_vendor_relation_sql(params: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Return only the normalized sales vendor relationship, with no master join."""
    cte, bind, meta = _sales_rows_cte(params)
    sql = cte + """
SELECT 기준월, 제품코드, 매입처코드
FROM NumericSales
GROUP BY 기준월, 제품코드, 매입처코드
OPTION (RECOMPILE)
"""
    plan = {
        "source_mode": meta["source_mode"],
        "logical_source_call_count": 1,
        "physical_query_count": 1,
        "projection_name": "manufacturer_vendor_relation",
        "grain": ["기준월", "제품코드", "매입처코드"],
        "excluded_columns": ["출고수량", "출고할증수량", "매출공급가액", "매출세액", "매출합계", "집계건수"],
        "excluded_operations": ["master_join", "manufacturer_join", "identity_concat", "final_count_distinct"],
        "hybrid_supported": False,
    }
    return sql, bind, plan


def build_manufacturer_month_from_narrow_projections(
    product_month_sales_df: pd.DataFrame,
    product_identity_df: pd.DataFrame,
    vendor_relation_df: pd.DataFrame,
) -> pd.DataFrame:
    """Reassemble the exact narrow manufacturer-month facts in Python.

    Product-month values contain all additive metrics.  The vendor relation is
    deliberately only used for the non-additive manufacturer/month vendor
    cardinality.  A non-unique product identity fails closed rather than
    selecting a manufacturer arbitrarily.
    """
    product = product_month_sales_df.copy()
    identity = product_identity_df.copy()
    missing_product = [name for name in ("기준월", "제품코드", "매출공급가액", "매출세액", "매출합계", "집계건수") if name not in product.columns]
    missing_identity = [name for name in ("제품코드", "제조사명") if name not in identity.columns]
    missing_relation = [name for name in VENDOR_RELATION_COLUMNS if name not in vendor_relation_df.columns]
    if missing_product or missing_identity or missing_relation:
        raise ValueError(f"manufacturer reconstruction missing columns: product={missing_product}, identity={missing_identity}, vendor_relation={missing_relation}")
    identity_map = identity.loc[:, ["제품코드", "제조사명"]].copy()
    identity_map["제품코드"] = identity_map["제품코드"].fillna("").astype(str).str.strip()
    if identity_map["제품코드"].duplicated().any():
        raise ValueError("manufacturer reconstruction requires one identity row per product code")
    identity_map["제약사명"] = identity_map["제조사명"].map(_dashboard_normalized_manufacturer)
    identity_map = identity_map.loc[:, ["제품코드", "제약사명"]]

    product["제품코드"] = product["제품코드"].fillna("").astype(str).str.strip()
    product["기준월"] = product["기준월"].fillna("").astype(str).str.strip()
    product = product.merge(identity_map, on="제품코드", how="left", validate="many_to_one")
    if product["제약사명"].isna().any():
        raise ValueError("manufacturer reconstruction product_month product code missing from identity")
    for column in ("매출공급가액", "매출세액", "매출합계", "집계건수"):
        product[column] = pd.to_numeric(product[column], errors="coerce").fillna(0)
    monetary = (
        product.groupby(["제약사명", "기준월"], dropna=False, as_index=False)
        .agg(
            매출공급가액=("매출공급가액", "sum"),
            매출세액=("매출세액", "sum"),
            매출합계=("매출합계", "sum"),
            집계건수=("집계건수", "sum"),
            제품수=("제품코드", "nunique"),
        )
    )

    relation = vendor_relation_df.loc[:, VENDOR_RELATION_COLUMNS].copy()
    for column in VENDOR_RELATION_COLUMNS:
        relation[column] = relation[column].fillna("").astype(str).str.strip()
    relation = relation.drop_duplicates()
    relation = relation.merge(identity_map, on="제품코드", how="left", validate="many_to_one")
    if relation["제약사명"].isna().any():
        raise ValueError("manufacturer reconstruction vendor relation product code missing from identity")
    vendor_counts = (
        relation.groupby(["제약사명", "기준월"], dropna=False)["매입처코드"]
        .nunique()
        .rename("매입처수")
        .reset_index()
    )
    result = monetary.merge(vendor_counts, on=["제약사명", "기준월"], how="left", validate="one_to_one")
    result["매입처수"] = pd.to_numeric(result["매입처수"], errors="coerce").fillna(0).astype(int)
    result["제품수"] = pd.to_numeric(result["제품수"], errors="coerce").fillna(0).astype(int)
    return result.reindex(columns=MANUFACTURER_MONTH_COLUMNS).sort_values(["제약사명", "기준월"], kind="stable").reset_index(drop=True)


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
        "equality": {"fixture_reconstruction": "RUN_GATE", "actual_legacy_same_source": "UNPROVEN_NOT_EXECUTED"},
    }
    _write(output, result)
    try:
        sql, bind, plan = build_manufacturer_vendor_relation_sql(params)
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
            frame = query_to_df(sql, bind)
            frame = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame(columns=VENDOR_RELATION_COLUMNS)
            result.update({
                "status": "EXECUTED",
                "physical_query_count": 1,
                "projection_result": {
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "row_count": int(len(frame)),
                    "column_count": int(len(frame.columns)),
                    "pandas_deep_memory_bytes": int(frame.memory_usage(index=True, deep=True).sum()),
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
    parser = argparse.ArgumentParser(description="Prepare or run one read-only Dashboard manufacturer vendor-relation probe.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true", help="Run the vendor relation SQL exactly once.")
    parser.add_argument("--query-timeout-seconds", type=int, default=120)
    args = parser.parse_args()
    if int(args.query_timeout_seconds) <= 0:
        raise SystemExit("query timeout must be positive")
    raise SystemExit(main(args.output, execute=bool(args.execute), timeout_seconds=int(args.query_timeout_seconds)))
