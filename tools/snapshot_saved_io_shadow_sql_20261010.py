"""Read-only, aggregate-only SQL builders for the next approved company Shadow.

These builders never connect to a database. The caller must verify the company
connection and run each statement sequentially with a bounded query timeout.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from app.services.saved_io_scope import SavedIoScope, bind_io_codes
from app.services.ssai_analysis_profile_service import normalize_business_code


def _boundary(value: str) -> str:
    text = str(value or "").strip()
    if len(text) != 8 or not text.isascii() or not text.isdigit():
        raise ValueError("Shadow boundary must be YYYYMMDD")
    datetime.strptime(text, "%Y%m%d")
    return text


def _base(
    *, stock_codes: Iterable[str], date_from: str | None, date_to: str, prefix: str,
) -> tuple[str, dict[str, Any]]:
    stocks = tuple(sorted({normalize_business_code(code) for code in stock_codes}))
    if not stocks or "" in stocks:
        raise ValueError("verified company stock scope is required")
    start = _boundary(date_from) if date_from is not None else None
    end = _boundary(date_to)
    if start is not None and start > end:
        raise ValueError("Shadow date range is reversed")
    binds: dict[str, Any] = {"shadow_to": end}
    date_clause = "I.Rd11_In_YyMmDd <= :shadow_to"
    if start is not None:
        binds["shadow_from"] = start
        date_clause = "I.Rd11_In_YyMmDd >= :shadow_from AND " + date_clause
    stock_clause = bind_io_codes("I.Rd11_Stock_Cd", stocks, binds, prefix=f"{prefix}_stock")
    return (
        "I.Rd11_Io_Gu_Gcode = '0012' AND I.Rd11_Stock_Cd_Gcode = '0018' "
        f"AND {date_clause} AND {stock_clause}",
        binds,
    )


def build_code_summary_sql(
    io_scope: SavedIoScope, *, stock_codes: Iterable[str], date_from: str, date_to: str,
) -> tuple[str, dict[str, Any]]:
    """One bounded R110 scan, one row per selected inbound or inbound-return code."""
    where, binds = _base(stock_codes=stock_codes, date_from=date_from, date_to=date_to, prefix="summary")
    selected = bind_io_codes(
        "I.Rd11_Io_Gu", (*io_scope.inbound_codes, *io_scope.inbound_return_codes),
        binds, prefix="summary_io",
    )
    return f"""
SELECT I.Rd11_Io_Gu AS io_tcode,
       COUNT_BIG(*) AS source_rows,
       SUM(CASE WHEN COALESCE(I.Rd11_Quantity, 0) > 0
                     AND COALESCE(I.Rd11_Fin_Supply_Price, I.Rd11_Supply_Price, 0) > 0
                THEN CONVERT(bigint, 1) ELSE CONVERT(bigint, 0) END) AS paid_price_rows,
       SUM(CASE WHEN COALESCE(I.Rd11_Quantity, 0) < 0
                     OR COALESCE(I.Rd11_Fin_Supply_Price, I.Rd11_Supply_Price, 0) < 0
                THEN CONVERT(bigint, 1) ELSE CONVERT(bigint, 0) END) AS reversal_candidate_rows,
       SUM(CAST(COALESCE(I.Rd11_Quantity, 0) AS decimal(38, 6))) AS paid_quantity,
       SUM(CAST(COALESCE(I.Rd11_Fin_Supply_Price, I.Rd11_Supply_Price, 0) AS decimal(38, 6))) AS supply_amount,
       MIN(I.Rd11_In_YyMmDd) AS first_date,
       MAX(I.Rd11_In_YyMmDd) AS last_date
FROM dbo.Rddbc110 AS I
WHERE {where} AND {selected}
GROUP BY I.Rd11_Io_Gu
ORDER BY I.Rd11_Io_Gu
""".strip(), binds


def _comparison_scope(io_scope: SavedIoScope) -> tuple[str, ...]:
    if "008" not in io_scope.price_candidate_codes:
        raise ValueError("008 is not selected in the saved company profile")
    if not io_scope.confirmed_inbound_codes:
        raise ValueError("001/002 baseline is not selected in the saved company profile")
    return (*io_scope.confirmed_inbound_codes, "008")


def build_price_comparison_sql(
    io_scope: SavedIoScope, *, stock_codes: Iterable[str], date_from: str, date_to: str,
    product_grain: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Compare latest eligible monthly costs, optionally retaining product identity."""
    selected_codes = _comparison_scope(io_scope)
    where, binds = _base(stock_codes=stock_codes, date_from=date_from, date_to=date_to, prefix="price")
    selected = bind_io_codes("I.Rd11_Io_Gu", selected_codes, binds, prefix="price_io")
    baseline = bind_io_codes("I.Rd11_Io_Gu", io_scope.confirmed_inbound_codes, binds, prefix="price_base")
    amount = "COALESCE(I.Rd11_Fin_Supply_Price, I.Rd11_Supply_Price, 0)"
    comparison = f"""
WITH Monthly AS (
    SELECT LTRIM(RTRIM(I.Rd11_Physic_Cd)) AS product_code,
           LEFT(I.Rd11_In_YyMmDd, 6) AS basis_month,
           SUM(CASE WHEN {baseline} THEN CAST(I.Rd11_Quantity AS decimal(38, 6)) ELSE 0 END) AS baseline_paid,
           SUM(CASE WHEN {baseline} THEN CAST({amount} AS decimal(38, 6)) ELSE 0 END) AS baseline_amount,
           SUM(CAST(I.Rd11_Quantity AS decimal(38, 6))) AS candidate_paid,
           SUM(CAST({amount} AS decimal(38, 6))) AS candidate_amount
    FROM dbo.Rddbc110 AS I
    WHERE {where} AND {selected}
      AND COALESCE(I.Rd11_Quantity, 0) > 0 AND {amount} > 0
      AND NULLIF(LTRIM(RTRIM(I.Rd11_Physic_Cd)), '') IS NOT NULL
    GROUP BY LTRIM(RTRIM(I.Rd11_Physic_Cd)), LEFT(I.Rd11_In_YyMmDd, 6)
), Latest AS (
    SELECT *,
           MAX(CASE WHEN baseline_paid > 0 THEN basis_month END) OVER (PARTITION BY product_code) AS baseline_month,
           MAX(CASE WHEN candidate_paid > 0 THEN basis_month END) OVER (PARTITION BY product_code) AS candidate_month
    FROM Monthly
), PerProduct AS (
    SELECT product_code,
           MAX(baseline_month) AS baseline_month,
           MAX(candidate_month) AS candidate_month,
           MAX(CASE WHEN basis_month = baseline_month AND baseline_paid > 0
                    THEN CAST(baseline_amount / baseline_paid AS decimal(38, 10)) END) AS baseline_unit_cost,
           MAX(CASE WHEN basis_month = candidate_month AND candidate_paid > 0
                    THEN CAST(candidate_amount / candidate_paid AS decimal(38, 10)) END) AS candidate_unit_cost
    FROM Latest GROUP BY product_code
)
"""
    if product_grain:
        return (comparison + """
SELECT product_code, baseline_month, candidate_month,
       baseline_unit_cost, candidate_unit_cost
FROM PerProduct
ORDER BY product_code
""").strip(), binds
    return (comparison + """
SELECT COUNT_BIG(*) AS candidate_products,
       SUM(CASE WHEN baseline_unit_cost IS NULL THEN CONVERT(bigint, 1) ELSE CONVERT(bigint, 0) END) AS newly_priced_products,
       SUM(CASE WHEN baseline_unit_cost IS NOT NULL AND candidate_unit_cost <> baseline_unit_cost
                THEN CONVERT(bigint, 1) ELSE CONVERT(bigint, 0) END) AS changed_unit_cost_products,
       SUM(CASE WHEN baseline_month IS NOT NULL AND candidate_month <> baseline_month
                THEN CONVERT(bigint, 1) ELSE CONVERT(bigint, 0) END) AS changed_price_month_products
FROM PerProduct
""").strip(), binds


def build_exact_key_overlap_sql(
    io_scope: SavedIoScope, *, stock_codes: Iterable[str], date_from: str, date_to: str,
) -> tuple[str, dict[str, Any]]:
    """Detect same-document 001/002 versus 008 overlaps, not semantic duplicates."""
    selected_codes = _comparison_scope(io_scope)
    where, binds = _base(stock_codes=stock_codes, date_from=date_from, date_to=date_to, prefix="overlap")
    selected = bind_io_codes("I.Rd11_Io_Gu", selected_codes, binds, prefix="overlap_io")
    baseline = bind_io_codes("I.Rd11_Io_Gu", io_scope.confirmed_inbound_codes, binds, prefix="overlap_base")
    return f"""
WITH DocumentKeys AS (
    SELECT I.Rd11_In_YyMmDd, I.Rd11_Ven_Cd, I.Rd11_In_Seq,
           LTRIM(RTRIM(I.Rd11_Physic_Cd)) AS product_code, I.Rd11_Stock_Cd,
           MAX(CASE WHEN {baseline} THEN 1 ELSE 0 END) AS baseline_seen,
           MAX(CASE WHEN I.Rd11_Io_Gu = :overlap_pda THEN 1 ELSE 0 END) AS pda_seen,
           COUNT_BIG(*) AS key_rows
    FROM dbo.Rddbc110 AS I
    WHERE {where} AND {selected}
    GROUP BY I.Rd11_In_YyMmDd, I.Rd11_Ven_Cd, I.Rd11_In_Seq,
             LTRIM(RTRIM(I.Rd11_Physic_Cd)), I.Rd11_Stock_Cd
)
SELECT COUNT_BIG(*) AS document_keys,
       SUM(CASE WHEN baseline_seen = 1 AND pda_seen = 1
                THEN CONVERT(bigint, 1) ELSE CONVERT(bigint, 0) END) AS exact_key_overlap,
       SUM(CASE WHEN key_rows > 1 THEN CONVERT(bigint, 1) ELSE CONVERT(bigint, 0) END) AS multirow_keys
FROM DocumentKeys
""".strip(), {**binds, "overlap_pda": "008"}


def build_pda_first_month_sql(
    io_scope: SavedIoScope, *, stock_codes: Iterable[str], cutoff_date: str,
) -> tuple[str, dict[str, Any]]:
    """Return one 008 minimum month per product for exact Snapshot comparison."""
    _comparison_scope(io_scope)
    where, binds = _base(stock_codes=stock_codes, date_from=None, date_to=cutoff_date, prefix="first")
    binds["first_pda"] = "008"
    return f"""
SELECT LTRIM(RTRIM(I.Rd11_Physic_Cd)) AS product_code,
       LEFT(MIN(I.Rd11_In_YyMmDd), 6) AS pda_first_month
FROM dbo.Rddbc110 AS I
WHERE {where} AND I.Rd11_Io_Gu = :first_pda
  AND COALESCE(I.Rd11_Quantity, 0) + COALESCE(I.Rd11_Oquantity, 0) > 0
  AND LEN(I.Rd11_In_YyMmDd) = 8
  AND I.Rd11_In_YyMmDd NOT LIKE '%[^0-9]%'
  AND ISDATE(I.Rd11_In_YyMmDd) = 1
GROUP BY LTRIM(RTRIM(I.Rd11_Physic_Cd))
ORDER BY product_code
""".strip(), binds


def build_basis_outbound_presence_sql(
    io_scope: SavedIoScope, *, stock_codes: Iterable[str], date_from: str, date_to: str,
) -> tuple[str, dict[str, Any]]:
    """Return product-grain candidate outbound evidence, never R120 detail rows."""
    stocks = tuple(sorted({normalize_business_code(code) for code in stock_codes}))
    if not stocks or "" in stocks:
        raise ValueError("verified company stock scope is required")
    start, end = _boundary(date_from), _boundary(date_to)
    if start > end:
        raise ValueError("Shadow date range is reversed")
    binds: dict[str, Any] = {"outbound_from": start, "outbound_to": end}
    stock_clause = bind_io_codes("O.Rd12_Stock_Cd", stocks, binds, prefix="outbound_stock")
    code_clause = bind_io_codes("O.Rd12_Io_Gu", io_scope.outbound_codes, binds, prefix="outbound_io")
    return f"""
SELECT LTRIM(RTRIM(O.Rd12_Physic_Cd)) AS product_code,
       COUNT_BIG(*) AS candidate_outbound_rows
FROM dbo.Rddbc120 AS O
WHERE O.Rd12_Io_Gu_Gcode = '0012'
  AND O.Rd12_Stock_Cd_Gcode = '0018'
  AND O.Rd12_Out_YyMmDd >= :outbound_from AND O.Rd12_Out_YyMmDd <= :outbound_to
  AND {stock_clause} AND {code_clause}
  AND COALESCE(O.Rd12_Quantity, 0) + COALESCE(O.Rd12_Oquantity, 0) > 0
  AND NULLIF(LTRIM(RTRIM(O.Rd12_Physic_Cd)), '') IS NOT NULL
GROUP BY LTRIM(RTRIM(O.Rd12_Physic_Cd))
ORDER BY product_code
""".strip(), binds


def build_full_inbound_code_summary_sql(
    io_scope: SavedIoScope, *, stock_codes: Iterable[str], date_from: str, date_to: str,
) -> tuple[str, dict[str, Any]]:
    """Aggregate selected 0xx and 1xx separately; preserve raw and finalized money."""
    where, binds = _base(stock_codes=stock_codes, date_from=date_from, date_to=date_to, prefix="audit_in")
    selected = bind_io_codes(
        "I.Rd11_Io_Gu", (*io_scope.inbound_codes, *io_scope.inbound_return_codes),
        binds, prefix="audit_in_io",
    )
    return f"""
SELECT I.Rd11_Io_Gu AS io_tcode,
       COUNT_BIG(*) AS source_rows,
       COUNT(DISTINCT NULLIF(LTRIM(RTRIM(I.Rd11_Physic_Cd)), '')) AS product_count,
       COUNT(DISTINCT NULLIF(LTRIM(RTRIM(I.Rd11_Ven_Cd)), '')) AS vendor_count,
       COUNT(DISTINCT NULLIF(LTRIM(RTRIM(I.Rd11_Cost_Apply_Cd)), '')) AS cost_apply_count,
       COUNT(DISTINCT NULLIF(LTRIM(RTRIM(I.Rd11_Stock_Apply_Cd)), '')) AS stock_apply_count,
       SUM(CAST(COALESCE(I.Rd11_Quantity, 0) AS decimal(38, 6))) AS quantity,
       SUM(CAST(COALESCE(I.Rd11_Oquantity, 0) AS decimal(38, 6))) AS bonus_quantity,
       SUM(CAST(COALESCE(I.Rd11_Supply_Price, 0) AS decimal(38, 6))) AS raw_supply,
       SUM(CAST(COALESCE(I.Rd11_Fin_Supply_Price, 0) AS decimal(38, 6))) AS final_supply,
       SUM(CASE WHEN I.Rd11_Fin_Supply_Price IS NULL THEN CONVERT(bigint, 1) ELSE 0 END) AS final_null_rows,
       SUM(CASE WHEN COALESCE(I.Rd11_Quantity, 0) > 0
                     AND COALESCE(I.Rd11_Fin_Supply_Price, I.Rd11_Supply_Price, 0) > 0
                THEN CONVERT(bigint, 1) ELSE 0 END) AS effective_paid_rows,
       SUM(CASE WHEN COALESCE(I.Rd11_Quantity, 0) > 0 AND COALESCE(I.Rd11_Supply_Price, 0) > 0
                THEN CONVERT(bigint, 1) ELSE 0 END) AS raw_paid_rows,
       SUM(CASE WHEN COALESCE(I.Rd11_Quantity, 0) > 0 AND COALESCE(I.Rd11_Fin_Supply_Price, 0) > 0
                THEN CONVERT(bigint, 1) ELSE 0 END) AS final_paid_rows,
       SUM(CASE WHEN COALESCE(I.Rd11_Quantity, 0) + COALESCE(I.Rd11_Oquantity, 0) > 0
                     AND COALESCE(I.Rd11_Fin_Supply_Price, I.Rd11_Supply_Price, 0) = 0
                THEN CONVERT(bigint, 1) ELSE 0 END) AS zero_effective_price_rows,
       SUM(CASE WHEN COALESCE(I.Rd11_Quantity, 0) < 0
                     OR COALESCE(I.Rd11_Fin_Supply_Price, I.Rd11_Supply_Price, 0) < 0
                THEN CONVERT(bigint, 1) ELSE 0 END) AS negative_candidate_rows,
       SUM(CASE WHEN I.Rd11_Fin_Supply_Price IS NOT NULL
                     AND I.Rd11_Fin_Supply_Price <> COALESCE(I.Rd11_Supply_Price, 0)
                THEN CONVERT(bigint, 1) ELSE 0 END) AS raw_final_different_rows,
       SUM(CASE WHEN NULLIF(LTRIM(RTRIM(I.Rd11_Link_Yymmdd)), '') IS NOT NULL
                THEN CONVERT(bigint, 1) ELSE 0 END) AS linked_date_rows,
       MIN(I.Rd11_In_YyMmDd) AS first_date, MAX(I.Rd11_In_YyMmDd) AS last_date
FROM dbo.Rddbc110 AS I
WHERE {where} AND {selected}
GROUP BY I.Rd11_Io_Gu
ORDER BY I.Rd11_Io_Gu
""".strip(), binds


def build_inbound_product_code_audit_sql(
    io_scope: SavedIoScope, *, stock_codes: Iterable[str], date_from: str, date_to: str,
) -> tuple[str, dict[str, Any]]:
    """Product/code-grain price evidence, never raw transaction rows."""
    where, binds = _base(stock_codes=stock_codes, date_from=date_from, date_to=date_to, prefix="audit_price")
    selected = bind_io_codes("I.Rd11_Io_Gu", io_scope.inbound_codes, binds, prefix="audit_price_io")
    amount = "COALESCE(I.Rd11_Fin_Supply_Price, I.Rd11_Supply_Price, 0)"
    return f"""
SELECT LTRIM(RTRIM(I.Rd11_Physic_Cd)) AS product_code, I.Rd11_Io_Gu AS io_tcode,
       COUNT_BIG(*) AS source_rows,
       SUM(CASE WHEN COALESCE(I.Rd11_Quantity, 0) > 0 AND {amount} > 0
                THEN CONVERT(bigint, 1) ELSE 0 END) AS effective_paid_rows,
       SUM(CASE WHEN COALESCE(I.Rd11_Quantity, 0) > 0 AND COALESCE(I.Rd11_Supply_Price, 0) > 0
                     AND COALESCE(I.Rd11_Fin_Supply_Price, 0) = 0
                THEN CONVERT(bigint, 1) ELSE 0 END) AS raw_paid_final_zero_rows,
       SUM(CASE WHEN COALESCE(I.Rd11_Quantity, 0) > 0 AND COALESCE(I.Rd11_Fin_Supply_Price, 0) > 0
                THEN CONVERT(bigint, 1) ELSE 0 END) AS final_paid_rows,
       SUM(CASE WHEN COALESCE(I.Rd11_Quantity, 0) < 0 OR {amount} < 0
                THEN CONVERT(bigint, 1) ELSE 0 END) AS negative_candidate_rows,
       SUM(CAST(COALESCE(I.Rd11_Quantity, 0) AS decimal(38, 6))) AS quantity,
       SUM(CAST(COALESCE(I.Rd11_Oquantity, 0) AS decimal(38, 6))) AS bonus_quantity,
       SUM(CAST(COALESCE(I.Rd11_Supply_Price, 0) AS decimal(38, 6))) AS raw_supply,
       SUM(CAST(COALESCE(I.Rd11_Fin_Supply_Price, 0) AS decimal(38, 6))) AS final_supply,
       MIN(I.Rd11_In_YyMmDd) AS first_date,
       MAX(CASE WHEN COALESCE(I.Rd11_Quantity, 0) > 0 AND {amount} > 0
                THEN LEFT(I.Rd11_In_YyMmDd, 6) END) AS latest_effective_paid_month
FROM dbo.Rddbc110 AS I
WHERE {where} AND {selected}
  AND NULLIF(LTRIM(RTRIM(I.Rd11_Physic_Cd)), '') IS NOT NULL
GROUP BY LTRIM(RTRIM(I.Rd11_Physic_Cd)), I.Rd11_Io_Gu
ORDER BY product_code, io_tcode
""".strip(), binds


def build_full_outbound_code_summary_sql(
    io_scope: SavedIoScope, *, stock_codes: Iterable[str], date_from: str, date_to: str,
    target_products: Iterable[str],
) -> tuple[str, dict[str, Any]]:
    """5xx/6xx code aggregates; split only the approved missing-sale target products."""
    stocks = tuple(sorted({normalize_business_code(code) for code in stock_codes}))
    targets = tuple(sorted({str(code).strip() for code in target_products}))
    if not stocks or not targets or len(targets) > 20 or "" in (*stocks, *targets):
        raise ValueError("verified stock scope and up to 20 target products are required")
    start, end = _boundary(date_from), _boundary(date_to)
    if start > end:
        raise ValueError("Shadow date range is reversed")
    binds: dict[str, Any] = {"audit_out_from": start, "audit_out_to": end}
    stock = bind_io_codes("O.Rd12_Stock_Cd", stocks, binds, prefix="audit_out_stock")
    selected = bind_io_codes(
        "O.Rd12_Io_Gu", (*io_scope.outbound_codes, *io_scope.outbound_return_codes),
        binds, prefix="audit_out_io",
    )
    target = bind_io_codes("LTRIM(RTRIM(O.Rd12_Physic_Cd))", targets, binds, prefix="audit_out_target")
    target_expr = f"CASE WHEN {target} THEN LTRIM(RTRIM(O.Rd12_Physic_Cd)) ELSE '' END"
    amount = "COALESCE(O.Rd12_Fin_Supply_Price, O.Rd12_Supply_Price, 0)"
    return f"""
SELECT O.Rd12_Io_Gu AS io_tcode, TargetScope.target_product,
       COUNT_BIG(*) AS source_rows,
       COUNT(DISTINCT NULLIF(LTRIM(RTRIM(O.Rd12_Physic_Cd)), '')) AS product_count,
       SUM(CAST(COALESCE(O.Rd12_Quantity, 0) AS decimal(38, 6))) AS quantity,
       SUM(CAST(COALESCE(O.Rd12_Oquantity, 0) AS decimal(38, 6))) AS bonus_quantity,
       SUM(CAST(COALESCE(O.Rd12_Supply_Price, 0) AS decimal(38, 6))) AS raw_supply,
       SUM(CAST(COALESCE(O.Rd12_Fin_Supply_Price, 0) AS decimal(38, 6))) AS final_supply,
       SUM(CASE WHEN COALESCE(O.Rd12_Quantity, 0) > 0 AND {amount} > 0
                THEN CONVERT(bigint, 1) ELSE 0 END) AS effective_paid_rows,
       SUM(CASE WHEN COALESCE(O.Rd12_Quantity, 0) < 0 OR {amount} < 0
                THEN CONVERT(bigint, 1) ELSE 0 END) AS negative_candidate_rows,
       MIN(O.Rd12_Out_YyMmDd) AS first_date, MAX(O.Rd12_Out_YyMmDd) AS last_date
FROM dbo.Rddbc120 AS O
CROSS APPLY (VALUES ({target_expr})) AS TargetScope(target_product)
WHERE O.Rd12_Io_Gu_Gcode = '0012' AND O.Rd12_Stock_Cd_Gcode = '0018'
  AND O.Rd12_Out_YyMmDd >= :audit_out_from AND O.Rd12_Out_YyMmDd <= :audit_out_to
  AND {stock} AND {selected}
GROUP BY O.Rd12_Io_Gu, TargetScope.target_product
ORDER BY io_tcode, target_product
""".strip(), binds
