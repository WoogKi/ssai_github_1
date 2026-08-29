"""Read-only company-8 equality probe for the Dashboard DB-side multi-grain candidate."""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.mssql_client import set_current_company_id
from app.services.analytics_sales_trend_service import (
    _apply_month_or_date_params,
    _apply_period_source_policy_params,
    _build_dashboard_purchase_vendor_where,
    _build_monthly_fast_where,
    _monthly_spec,
    _resolve_source_mode,
    build_dashboard_sales_purchase_grains,
    coalesce_params,
    get_dashboard_sales_source_bundle,
    query_to_df,
)
from app.services.dashboard_lite_facts import (
    _dashboard_internal_source_params,
    normalize_dashboard_lite_params,
)


# These are the company-8 Dashboard request values recorded in the 2026-08-25
# runtime gate.  Keep the display scope separate from the internally expanded
# sales-source scope; the latter is the only input the sales bundle receives.
_DASHBOARD_REQUEST = {
    "company_id": 8,
    "month_from": "202602",
    "month_to": "202607",
    "evaluation_month": "202608",
    "date_from": "20260201",
    "date_to": "20260731",
    "policy_date": "20260825",
    "source_mode": "monthly_book",
    "stock_cd_list": ["00001", "00008", "00013"],
    "stock_cds": ["00001", "00008", "00013"],
}
_DASHBOARD_TODAY = date(2026, 8, 25)


def dashboard_probe_contract() -> tuple[dict, dict]:
    """Return the exact Dashboard sales-source contract without opening a DB."""
    service_params = normalize_dashboard_lite_params(_DASHBOARD_REQUEST, today=_DASHBOARD_TODAY)
    source_params = _dashboard_internal_source_params(service_params, today=_DASHBOARD_TODAY)
    prepared = _apply_period_source_policy_params(_apply_month_or_date_params(coalesce_params(source_params)))
    policy = dict(prepared.get("_period_source_policy") or {})
    contract = {
        "dashboard_request": {
            key: service_params.get(key, "")
            for key in ("month_from", "month_to", "evaluation_month", "date_from", "date_to", "policy_date", "source_mode")
        },
        "sales_source": {
            key: prepared.get(key, "")
            for key in ("month_from", "month_to", "date_from", "date_to", "policy_date", "source_mode", "dashboard_lite_trend_month_from", "dashboard_lite_history_month_from")
        },
        "period_source_policy": {
            key: policy.get(key)
            for key in ("today", "requested_date_to", "effective_date_to", "effective_month_to", "evaluation_mode", "is_month_end", "use_hybrid", "use_hybrid_detail", "use_monthly_only")
        },
    }
    return source_params, contract


def _candidate_sql(params: dict) -> tuple[str, dict]:
    prepared = _apply_period_source_policy_params(_apply_month_or_date_params(coalesce_params(params)))
    source_mode = _resolve_source_mode(prepared)
    if source_mode not in {"monthly_book", "monthly_real"}:
        raise ValueError(f"monthly source required, got {source_mode}")
    spec = _monthly_spec(source_mode)
    prefix = spec["prefix"]
    sales_where, sales_bind = _build_monthly_fast_where(prepared, spec)
    purchase_where, purchase_bind = _build_dashboard_purchase_vendor_where(prepared, spec)
    bind = dict(sales_bind)
    bind.update({key: value for key, value in purchase_bind.items() if key not in bind})
    in_qty = (
        f"CAST(ISNULL(M.{prefix}_In_Quantity, 0) AS FLOAT) + CAST(ISNULL(M.{prefix}_In_Oquantity, 0) AS FLOAT)"
        if source_mode == "monthly_real" else f"CAST(ISNULL(M.{prefix}_In_Quantity, 0) AS FLOAT)"
    )
    sql = f"""
WITH SalesBase AS (
    SELECT LEFT(M.{prefix}_Stock_YyMm, 6) AS 기준월, M.{prefix}_Physic_Cd AS 제품코드,
           M.{prefix}_Ven_Cd AS 매입처코드,
           SUM(CASE WHEN LEFT(M.{prefix}_Io_Gu, 1) = '6' THEN -1 * COALESCE(M.{prefix}_Out_Quantity, 0) ELSE COALESCE(M.{prefix}_Out_Quantity, 0) END) AS 출고수량,
           SUM(CASE WHEN LEFT(M.{prefix}_Io_Gu, 1) = '6' THEN -1 * COALESCE(M.{prefix}_Out_Oquantity, 0) ELSE COALESCE(M.{prefix}_Out_Oquantity, 0) END) AS 출고할증수량,
           SUM(CASE WHEN LEFT(M.{prefix}_Io_Gu, 1) = '6' THEN -1 * COALESCE(M.{prefix}_Out_Supply_Price, 0) ELSE COALESCE(M.{prefix}_Out_Supply_Price, 0) END) AS 매출공급가액,
           SUM(CASE WHEN LEFT(M.{prefix}_Io_Gu, 1) = '6' THEN -1 * COALESCE(M.{prefix}_Out_Tax_Price, 0) ELSE COALESCE(M.{prefix}_Out_Tax_Price, 0) END) AS 매출세액,
           SUM(CASE WHEN LEFT(M.{prefix}_Io_Gu, 1) = '6' THEN -1 * (COALESCE(M.{prefix}_Out_Supply_Price, 0) + COALESCE(M.{prefix}_Out_Tax_Price, 0)) ELSE COALESCE(M.{prefix}_Out_Supply_Price, 0) + COALESCE(M.{prefix}_Out_Tax_Price, 0) END) AS 매출합계,
           COUNT(*) AS 집계건수
    FROM {spec["table"]} AS M WITH (NOLOCK)
    WHERE 1 = 1 {sales_where}
    GROUP BY LEFT(M.{prefix}_Stock_YyMm, 6), M.{prefix}_Physic_Cd, M.{prefix}_Ven_Cd
), PurchaseMonthly AS (
    SELECT LEFT(M.{prefix}_Stock_YyMm, 6) AS 기준월, M.{prefix}_Physic_Cd AS 제품코드, M.{prefix}_Ven_Cd AS 매입처코드,
           SUM({in_qty}) AS 입고수량,
           SUM(CAST(ISNULL(M.{prefix}_In_Supply_Price, 0) AS FLOAT)) AS 매입금액,
           SUM(CASE WHEN CAST(ISNULL(M.{prefix}_In_Supply_Price, 0) AS FLOAT) > 0 OR {in_qty} > 0 THEN 1 ELSE 0 END) AS 매입발생건수
    FROM {spec["table"]} AS M WITH (NOLOCK)
    WHERE 1 = 1 {purchase_where}
    GROUP BY LEFT(M.{prefix}_Stock_YyMm, 6), M.{prefix}_Physic_Cd, M.{prefix}_Ven_Cd
)
SELECT 'sales_product_month' AS grain, 기준월, 제품코드, '' AS 매입처코드,
       SUM(출고수량) AS 출고수량, SUM(출고할증수량) AS 출고할증수량, SUM(매출공급가액) AS 매출공급가액,
       SUM(매출세액) AS 매출세액, SUM(매출합계) AS 매출합계, SUM(집계건수) AS 집계건수,
       COUNT(DISTINCT 매입처코드) AS 매입처수, CAST(0 AS FLOAT) AS 입고수량, CAST(0 AS FLOAT) AS 매입금액, CAST(0 AS BIGINT) AS 매입발생건수
FROM SalesBase GROUP BY 기준월, 제품코드
UNION ALL
SELECT 'manufacturer_vendor', '', 제품코드, 매입처코드, 0,0,0,0,0,0,0,0,0,0 FROM (SELECT DISTINCT 제품코드, 매입처코드 FROM SalesBase) AS X
UNION ALL
SELECT 'purchase_product_vendor', '', 제품코드, 매입처코드, 0,0,0,0,0,0,0,
       SUM(CASE WHEN 기준월 BETWEEN %(recent_from)s AND %(recent_to)s THEN 입고수량 ELSE 0 END),
       SUM(CASE WHEN 기준월 BETWEEN %(recent_from)s AND %(recent_to)s THEN 매입금액 ELSE 0 END),
       SUM(CASE WHEN 기준월 BETWEEN %(recent_from)s AND %(recent_to)s THEN 매입발생건수 ELSE 0 END)
FROM PurchaseMonthly
WHERE 기준월 >= %(history_month_from)s AND 기준월 < %(evaluation_month)s
GROUP BY 제품코드, 매입처코드
UNION ALL
SELECT 'purchase_product_month', 기준월, 제품코드, '', 0,0,0,0,0,0,0, SUM(입고수량), SUM(매입금액), SUM(매입발생건수)
FROM PurchaseMonthly GROUP BY 기준월, 제품코드
"""
    return sql, bind


def _norm(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    result = frame.reindex(columns=columns).copy()
    for column in columns:
        if "코드" in column or column == "기준월":
            result[column] = result[column].fillna("").astype(str).str.strip()
        else:
            result[column] = pd.to_numeric(result[column], errors="coerce").fillna(0.0)
    return result.sort_values(columns[:2], kind="stable").reset_index(drop=True)


def _write_result(path: Path | None, result: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def main(output_path: Path | None = None) -> int:
    params, contract = dashboard_probe_contract()
    evaluation_month = str(params["evaluation_month"])
    history_month_from = str(params["dashboard_lite_history_month_from"])
    result: dict = {"status": "started", "started_at": datetime.now().isoformat(timespec="seconds"), "company_id": 8, "retry_count": 0, "source_call_count_contract": 3}
    result["dashboard_contract"] = contract
    _write_result(output_path, result)
    try:
        set_current_company_id(8)
        started = time.perf_counter()
        legacy = get_dashboard_sales_source_bundle(params)
        result["legacy_ms"] = int((time.perf_counter() - started) * 1000)
        result["legacy_rows"] = {"sales": len(legacy["sales_df"]), "purchase": len(legacy["purchase_vendor_df"])}
        result["stage"] = "legacy_complete"
        _write_result(output_path, result)
        grains = build_dashboard_sales_purchase_grains(legacy["sales_df"], legacy["purchase_vendor_df"], evaluation_month=evaluation_month, history_month_from=history_month_from)
        sql, bind = _candidate_sql(params)
        bind.update({"recent_from": "202602", "recent_to": "202607", "history_month_from": history_month_from, "evaluation_month": evaluation_month})
        result["stage"] = "candidate_started"
        _write_result(output_path, result)
        started = time.perf_counter()
        candidate = query_to_df(sql, bind)
        result["candidate_ms"] = int((time.perf_counter() - started) * 1000)
        result["candidate_rows"] = candidate["grain"].value_counts().to_dict()
        result["stage"] = "candidate_complete"
        _write_result(output_path, result)
        sales = candidate.loc[candidate["grain"].eq("sales_product_month")]
        expected_sales = _norm(grains.sales_product_month_df, ["기준월", "제품코드", "출고수량", "출고할증수량", "매출공급가액", "매출세액", "매출합계", "집계건수", "매입처수"])
        actual_sales = _norm(sales, expected_sales.columns.tolist())
        pd.testing.assert_frame_equal(actual_sales, expected_sales, check_dtype=False, check_like=False)
        expected_cardinality = grains.manufacturer_vendor_df.groupby("제품코드")["매입처코드"].nunique().sort_index().to_dict()
        card = candidate.loc[candidate["grain"].eq("manufacturer_vendor")]
        actual_cardinality = card.groupby("제품코드")["매입처코드"].nunique().sort_index().to_dict()
        assert actual_cardinality == expected_cardinality
        expected_trend = _norm(grains.purchase_product_month_df, ["기준월", "제품코드", "입고수량", "매입금액", "매입발생건수"])
        actual_trend = _norm(candidate.loc[candidate["grain"].eq("purchase_product_month")], expected_trend.columns.tolist())
        pd.testing.assert_frame_equal(actual_trend, expected_trend, check_dtype=False, check_like=False)
        result.update({"status": "PASS", "stage": "complete", "equalities": ["product_month_sales", "product_vendor_cardinality", "purchase_product_month_trend"]})
        return 0
    except Exception as exc:
        result.update({"status": "FAIL", "stage": result.get("stage", "setup"), "exception_type": type(exc).__name__, "exception": str(exc), "traceback": traceback.format_exc(limit=5)})
        return 1
    finally:
        result["finished_at"] = datetime.now().isoformat(timespec="seconds")
        _write_result(output_path, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    raise SystemExit(main(args.output))
