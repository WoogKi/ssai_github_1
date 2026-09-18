"""발주 현재고의 ERP 재고구분 계약을 오프라인 또는 단발 읽기 전용으로 점검한다."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import mssql_client as db
from app.services import analytics_sales_trend_service as stock_service
from app.services.order_calculation_service import get_order_calculation_result
from app.services.dashboard_inventory_frequency_snapshot_service import (
    resolve_dashboard_profile_stock_scope,
)
from app.services.rddbc_io_common import query_to_df, stock_movement_io_prefixes


def _label(code: str) -> str:
    return "P-" + hashlib.sha256(code.encode("utf-8")).hexdigest()[:8]


def _prefix_sql(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _scope_clause(field: str, values: list[str], binds: dict[str, Any], prefix: str) -> str:
    if not values:
        return ""
    names = []
    for index, value in enumerate(values):
        key = f"{prefix}_{index}"
        binds[key] = value
        names.append(f"%({key})s")
    return f" AND {field} IN ({', '.join(names)})"


def _diagnostic_sql(
    *,
    stock_mode: str,
    product_codes: list[str],
    stock_codes: list[str],
    stock_apply_cd: str,
    cutoff_date: str,
    policy_date: str,
) -> tuple[str, dict[str, Any]]:
    real = stock_mode == "real"
    monthly_table = "dbo.Rddbc210" if real else "dbo.Rddbc220"
    monthly_prefix = "Rd21" if real else "Rd22"
    in_date = "Rd11_In_YyMmDd" if real else "Rd11_Trans_YyMmDd"
    out_date = "Rd12_Out_YyMmDd" if real else "Rd12_Trans_YyMmDd"
    in_qty = "ISNULL(T.Rd11_Quantity, 0) + ISNULL(T.Rd11_Oquantity, 0)" if real else "ISNULL(T.Rd11_Quantity, 0)"
    out_qty = "ISNULL(T.Rd12_Quantity, 0) + ISNULL(T.Rd12_Oquantity, 0)" if real else "ISNULL(T.Rd12_Quantity, 0)"
    monthly_in = f"ISNULL(M.{monthly_prefix}_In_Quantity, 0)" + (f" + ISNULL(M.{monthly_prefix}_In_Oquantity, 0)" if real else "")
    monthly_out = f"ISNULL(M.{monthly_prefix}_Out_Quantity, 0)" + (f" + ISNULL(M.{monthly_prefix}_Out_Oquantity, 0)" if real else "")
    prefixes = stock_movement_io_prefixes(stock_mode)
    inbound_sql = _prefix_sql(prefixes["inbound"])
    outbound_sql = _prefix_sql(prefixes["outbound"])
    period_policy = stock_service._resolve_period_source_policy({
        "date_to": cutoff_date,
        "policy_date": policy_date,
    })
    use_hybrid = bool(period_policy["use_hybrid_detail"])
    effective_date_to = str(period_policy["effective_date_to"])
    effective_month_to = str(period_policy["effective_month_to"])
    monthly_to = stock_service._prev_yyyymm(effective_month_to) if use_hybrid else effective_month_to
    binds: dict[str, Any] = {
        "io_gcode": "0012",
        "month_to": monthly_to,
    }
    if use_hybrid:
        binds["date_from"] = effective_month_to + "01"
        binds["date_to"] = effective_date_to
    product_names = []
    for index, code in enumerate(product_codes):
        key = f"product_{index}"
        binds[key] = code
        product_names.append(f"%({key})s")
    products = ", ".join(product_names)
    monthly_scope = _scope_clause(f"M.{monthly_prefix}_Stock_Cd", stock_codes, binds, "month_stock")
    inbound_scope = _scope_clause("T.Rd11_Stock_Cd", stock_codes, binds, "in_stock")
    outbound_scope = _scope_clause("T.Rd12_Stock_Cd", stock_codes, binds, "out_stock")
    monthly_source_part = "monthly_prior" if use_hybrid else "monthly_current"
    sql = f"""
SELECT LTRIM(RTRIM(M.{monthly_prefix}_Physic_Cd)) AS product_code,
       '{monthly_source_part}' AS source_part,
       LEFT(LTRIM(RTRIM(M.{monthly_prefix}_Io_Gu)), 1) AS io_prefix,
       SUM(CASE WHEN LEFT(LTRIM(RTRIM(M.{monthly_prefix}_Io_Gu)), 1) IN ({inbound_sql})
                THEN {monthly_in} ELSE 0 END)
       - SUM(CASE WHEN LEFT(LTRIM(RTRIM(M.{monthly_prefix}_Io_Gu)), 1) IN ({outbound_sql})
                  THEN {monthly_out} ELSE 0 END) AS stock_contribution
FROM {monthly_table} AS M WITH (NOLOCK)
WHERE M.{monthly_prefix}_Physic_Cd IN ({products})
  AND M.{monthly_prefix}_Stock_YyMm <= %(month_to)s
  AND M.{monthly_prefix}_Io_Gu_Gcode = %(io_gcode)s
  AND LEFT(LTRIM(RTRIM(M.{monthly_prefix}_Io_Gu)), 1) IN ({inbound_sql}, {outbound_sql})
  {monthly_scope}
GROUP BY LTRIM(RTRIM(M.{monthly_prefix}_Physic_Cd)), LEFT(LTRIM(RTRIM(M.{monthly_prefix}_Io_Gu)), 1)
"""
    if use_hybrid:
        sql += f"""
UNION ALL
SELECT LTRIM(RTRIM(T.Rd11_Physic_Cd)), 'current_in', LEFT(LTRIM(RTRIM(T.Rd11_Io_Gu)), 1),
       SUM({in_qty})
FROM dbo.Rddbc110 AS T WITH (NOLOCK)
WHERE T.Rd11_Physic_Cd IN ({products})
  AND {in_date} >= %(date_from)s AND {in_date} <= %(date_to)s
  AND T.Rd11_Io_Gu_Gcode = %(io_gcode)s
  AND LEFT(LTRIM(RTRIM(T.Rd11_Io_Gu)), 1) IN ({inbound_sql})
  {inbound_scope}
GROUP BY LTRIM(RTRIM(T.Rd11_Physic_Cd)), LEFT(LTRIM(RTRIM(T.Rd11_Io_Gu)), 1)
UNION ALL
SELECT LTRIM(RTRIM(T.Rd12_Physic_Cd)), 'current_out', LEFT(LTRIM(RTRIM(T.Rd12_Io_Gu)), 1),
       -SUM({out_qty})
FROM dbo.Rddbc120 AS T WITH (NOLOCK)
WHERE T.Rd12_Physic_Cd IN ({products})
  AND {out_date} >= %(date_from)s AND {out_date} <= %(date_to)s
  AND T.Rd12_Io_Gu_Gcode = %(io_gcode)s
  AND LEFT(LTRIM(RTRIM(T.Rd12_Io_Gu)), 1) IN ({outbound_sql})
  {outbound_scope}
GROUP BY LTRIM(RTRIM(T.Rd12_Physic_Cd)), LEFT(LTRIM(RTRIM(T.Rd12_Io_Gu)), 1)
"""
    return sql, binds


def _order_screen_stock(
    *,
    company_id: int,
    product_code: str,
    stock_apply_cd: str,
    cutoff_date: str,
) -> dict[str, Any]:
    result = get_order_calculation_result({
        "company_id": company_id,
        "order_date": f"{cutoff_date[:4]}-{cutoff_date[4:6]}-{cutoff_date[6:8]}",
        "physic_cd": product_code,
        "stock_apply_cd": stock_apply_cd,
        "query_mode": "전체",
    })
    frame = result.get("df")
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    if not isinstance(frame, pd.DataFrame) or frame.empty or "재고수량" not in frame.columns:
        return {
            "stock_values": [],
            "row_count": 0,
            "logical_source_call_count": meta.get("source_call_count"),
        }
    values = sorted({
        float(value)
        for value in pd.to_numeric(frame["재고수량"], errors="coerce").dropna().tolist()
    })
    return {
        "stock_values": values,
        "row_count": int(len(frame)),
        "logical_source_call_count": meta.get("source_call_count"),
    }


def _offline_result() -> dict[str, Any]:
    real = stock_movement_io_prefixes("real")
    book = stock_movement_io_prefixes("book")
    assert real == {"inbound": ("0", "1", "3", "4"), "outbound": ("5", "6", "8", "9")}
    assert book == {"inbound": ("0", "1", "2", "4"), "outbound": ("5", "6", "7", "9")}
    # ERP 반품은 원천 음수 수량을 유지한다: 입고반품 -2 + 출고반품 -3의 출고 차감 = 순증가 1.
    assert -2 - (-3) == 1
    for mode in ("real", "book"):
        current_sql, current_binds = _diagnostic_sql(
            stock_mode=mode,
            product_codes=["P001"],
            stock_codes=["00001"],
            stock_apply_cd="50001",
            cutoff_date="20260918",
            policy_date="20260918",
        )
        contract = stock_movement_io_prefixes(mode)
        assert _prefix_sql(contract["inbound"]) in current_sql
        assert _prefix_sql(contract["outbound"]) in current_sql
        assert "monthly_current" in current_sql
        assert "Rddbc110" not in current_sql and "Rddbc120" not in current_sql
        assert "Stock_Apply_Cd" not in current_sql and "stock_apply_cd" not in current_binds
        assert current_binds["month_to"] == "202609"

        historical_sql, historical_binds = _diagnostic_sql(
            stock_mode=mode,
            product_codes=["P001"],
            stock_codes=["00001"],
            stock_apply_cd="50001",
            cutoff_date="20260815",
            policy_date="20260918",
        )
        assert "monthly_prior" in historical_sql
        assert "Rddbc110" in historical_sql and "Rddbc120" in historical_sql
        assert "Stock_Apply_Cd" not in historical_sql and "stock_apply_cd" not in historical_binds
        assert historical_binds["month_to"] == "202607"
        assert historical_binds["date_from"] == "20260801"
        assert historical_binds["date_to"] == "20260815"

        month_end_sql, month_end_binds = _diagnostic_sql(
            stock_mode=mode,
            product_codes=["P001"],
            stock_codes=["00001"],
            stock_apply_cd="50001",
            cutoff_date="20260831",
            policy_date="20260918",
        )
        assert "monthly_current" in month_end_sql
        assert "Rddbc110" not in month_end_sql and "Rddbc120" not in month_end_sql
        assert month_end_binds["month_to"] == "202608"
    return {
        "status": "PASS",
        "mode": "offline",
        "period_policies": ["current_monthly", "historical_midmonth", "historical_month_end"],
        "write_count": 0,
    }


def _live_result(args: argparse.Namespace) -> dict[str, Any]:
    codes = sorted({str(value).strip() for value in args.product_code if str(value).strip()})
    if not codes:
        raise ValueError("--live에는 --product-code가 하나 이상 필요합니다.")
    db.set_current_company_id(args.company_id)
    scope = resolve_dashboard_profile_stock_scope(company_id=args.company_id)
    stock_codes = list(scope.stock_codes)
    stock_apply_cd = str(args.stock_apply_code or "").strip()
    period_policy = stock_service._resolve_period_source_policy({
        "date_to": args.cutoff_date,
        "policy_date": args.policy_date,
    })
    sql, binds = _diagnostic_sql(
        stock_mode=scope.stock_mode,
        product_codes=codes,
        stock_codes=stock_codes,
        stock_apply_cd=stock_apply_cd,
        cutoff_date=args.cutoff_date,
        policy_date=args.policy_date,
    )
    with db.read_only_request(timeout_seconds=args.timeout_seconds):
        contributions = query_to_df(sql, binds)
    expected = (
        contributions.groupby("product_code", as_index=False)["stock_contribution"].sum()
        if isinstance(contributions, pd.DataFrame) and not contributions.empty
        else pd.DataFrame(columns=["product_code", "stock_contribution"])
    )
    detail = []
    for code in codes:
        rows = contributions.loc[contributions["product_code"].astype(str).str.strip().eq(code)] if not contributions.empty else pd.DataFrame()
        expected_row = expected.loc[expected["product_code"].astype(str).str.strip().eq(code)]
        expected_stock = float(expected_row.iloc[0]["stock_contribution"]) if not expected_row.empty else 0.0
        screen = _order_screen_stock(
            company_id=args.company_id,
            product_code=code,
            stock_apply_cd=stock_apply_cd,
            cutoff_date=str(period_policy["effective_date_to"]),
        )
        differences = [value - expected_stock for value in screen["stock_values"]]
        detail.append({
            "product": _label(code),
            "prefix_contributions": [
                {
                    "source_part": str(row.source_part),
                    "io_prefix": str(row.io_prefix),
                    "stock_contribution": float(row.stock_contribution),
                }
                for row in rows.itertuples()
            ],
            "expected_stock": expected_stock,
            "order_screen_stock_values": screen["stock_values"],
            "order_screen_row_count": screen["row_count"],
            "order_screen_logical_source_call_count": screen["logical_source_call_count"],
            "differences": differences,
        })
    all_matched = all(
        row["order_screen_stock_values"]
        and all(abs(value) < 1e-9 for value in row["differences"])
        for row in detail
    )
    return {
        "status": "PASS" if all_matched else "FAIL",
        "mode": "live_read_only",
        "company_id": args.company_id,
        "stock_mode": scope.stock_mode,
        "stock_code_count": len(stock_codes),
        "io_gu_profile_count_ignored_for_stock": len(scope.io_gu_codes),
        "stock_apply_cd_ignored_for_stock": stock_apply_cd,
        "period_policy": period_policy,
        "diagnostic_select_count": 1,
        "diagnostic_source_shape": "monthly_plus_detail" if period_policy["use_hybrid_detail"] else "monthly_only",
        "order_screen_request_count": len(codes),
        "write_count": 0,
        "products": detail,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--company-id", type=int, default=7)
    parser.add_argument("--product-code", action="append", default=[])
    parser.add_argument("--stock-apply-code", default="50001")
    parser.add_argument("--cutoff-date", default="20260918")
    parser.add_argument("--policy-date", default="20260918")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    args = parser.parse_args()
    try:
        result = _live_result(args) if args.live else _offline_result()
    except Exception as exc:
        result = {
            "status": "HOLD",
            "mode": "live_read_only" if args.live else "offline",
            "reason": f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}",
            "retry_count": 0,
            "write_count": 0,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
