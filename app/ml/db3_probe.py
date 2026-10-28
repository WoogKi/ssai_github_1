"""Bounded, parameterized SELECT plan for a company-3 ML data probe."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Any, Callable, Mapping

from app.ml.dataset_contract import BASE_FROM, BASE_TO, SAMPLE_MAX_ROWS, validate_db3_scope
from app.ml.phase1 import document_amount_once, prepare_sources


KST = timezone(timedelta(hours=9))
Query = Callable[[str, tuple[Any, ...], int], list[dict[str, Any]]]

R120_DAILY = """
SELECT Rd12_Out_YyMmDd AS out_date, LEFT(LTRIM(RTRIM(Rd12_Io_Gu)), 1) AS io_family,
       COUNT_BIG(*) AS row_count,
       SUM(CAST(ISNULL(Rd12_Quantity, 0) AS decimal(38, 4))) AS quantity,
       SUM(CAST(ISNULL(Rd12_Oquantity, 0) AS decimal(38, 4))) AS bonus_quantity,
       SUM(CASE WHEN Rd12_Quantity < 0 THEN 1 ELSE 0 END) AS negative_rows,
       SUM(CASE WHEN Rd12_Add_Time IS NULL THEN 1 ELSE 0 END) AS missing_add_time,
       SUM(CASE WHEN Rd12_Mod_Time IS NULL THEN 1 ELSE 0 END) AS missing_mod_time
FROM dbo.Rddbc120
WHERE Rd12_Out_YyMmDd >= ? AND Rd12_Out_YyMmDd < ?
GROUP BY Rd12_Out_YyMmDd, LEFT(LTRIM(RTRIM(Rd12_Io_Gu)), 1)
ORDER BY out_date, io_family
"""

R130_DAILY = """
SELECT Rd13_Trans_YyMmDd AS out_date, COUNT_BIG(*) AS row_count,
       SUM(CAST(ISNULL(Rd13_Supply_Price, 0) AS decimal(38, 4))) AS supply_amount,
       SUM(CAST(ISNULL(Rd13_Tax_Price, 0) AS decimal(38, 4))) AS tax_amount
FROM dbo.Rddbc130
WHERE Rd13_Trans_Di = '3' AND Rd13_Trans_YyMmDd >= ? AND Rd13_Trans_YyMmDd < ?
GROUP BY Rd13_Trans_YyMmDd
ORDER BY out_date
"""

CANDIDATES = """
SELECT TOP (2) LTRIM(RTRIM(Rd12_Physic_Cd)) AS product_code, COUNT_BIG(*) AS normal_rows
FROM dbo.Rddbc120
WHERE Rd12_Out_YyMmDd >= ? AND Rd12_Out_YyMmDd < ? AND Rd12_Io_Gu LIKE '5%'
GROUP BY Rd12_Physic_Cd
HAVING COUNT_BIG(*) BETWEEN 5 AND 200
ORDER BY COUNT_BIG(*) DESC, product_code
"""

SAMPLE_R120 = """
SELECT TOP (501) Rd12_Out_YyMmDd, Rd12_Ven_Cd, Rd12_Out_Seq, Rd12_Physic_Cd,
       Rd12_Io_Gu, Rd12_Quantity, Rd12_Oquantity, Rd12_Stock_Cd_Gcode,
       Rd12_Stock_Cd, Rd12_Stock_Apply_Cd, Rd12_Cost_Apply_Cd,
       Rd12_Trans_Di, Rd12_Trans_YyMmDd, Rd12_Trans_Seq,
       Rd12_Add_Time, Rd12_Mod_Time
FROM dbo.Rddbc120
WHERE Rd12_Out_YyMmDd >= ? AND Rd12_Out_YyMmDd < ?
  AND Rd12_Physic_Cd IN ({product_marks})
  AND (Rd12_Io_Gu LIKE '5%' OR Rd12_Io_Gu LIKE '6%')
ORDER BY Rd12_Out_YyMmDd, Rd12_Ven_Cd, Rd12_Out_Seq
"""

SAMPLE_R130 = """
SELECT TOP (501) H.Rd13_Trans_Di, H.Rd13_Trans_YyMmDd, H.Rd13_Ven_Cd,
       H.Rd13_Trans_Seq, MAX(H.Rd13_Supply_Price) AS Rd13_Supply_Price,
       MAX(H.Rd13_Tax_Price) AS Rd13_Tax_Price, COUNT_BIG(*) AS header_count
FROM dbo.Rddbc130 AS H
WHERE H.Rd13_Trans_Di = '3'
  AND EXISTS (
      SELECT 1 FROM dbo.Rddbc120 AS O
      WHERE O.Rd12_Trans_Di = H.Rd13_Trans_Di
        AND O.Rd12_Trans_YyMmDd = H.Rd13_Trans_YyMmDd
        AND O.Rd12_Ven_Cd = H.Rd13_Ven_Cd
        AND O.Rd12_Trans_Seq = H.Rd13_Trans_Seq
        AND O.Rd12_Out_YyMmDd >= ? AND O.Rd12_Out_YyMmDd < ?
        AND O.Rd12_Physic_Cd IN ({product_marks})
        AND (O.Rd12_Io_Gu LIKE '5%' OR O.Rd12_Io_Gu LIKE '6%')
  )
GROUP BY H.Rd13_Trans_Di, H.Rd13_Trans_YyMmDd, H.Rd13_Ven_Cd, H.Rd13_Trans_Seq
ORDER BY H.Rd13_Trans_YyMmDd, H.Rd13_Ven_Cd, H.Rd13_Trans_Seq
"""


def _select_codes(table: str, columns: str, field: str, codes: tuple[str, ...]) -> str:
    if table not in {"Rddbc030", "Rddbc040", "Rddbc046"}:
        raise ValueError("unapproved master table")
    return f"SELECT TOP (501) {columns} FROM dbo.{table} WHERE {field} IN ({', '.join('?' for _ in codes)})"


def run_db3_probe(query: Query, *, company_id: int = 3, alias: str = "DB3") -> dict[str, Any]:
    validate_db3_scope(company_id=company_id, date_from=BASE_FROM, date_to=BASE_TO, alias=alias)
    durations: list[int] = []
    query_count = 0

    def limited(sql: str, params: tuple[Any, ...], cap: int) -> list[dict[str, Any]]:
        nonlocal query_count
        if query_count >= 9 or not sql.lstrip().upper().startswith("SELECT"):
            raise ValueError("probe query count or SELECT boundary exceeded")
        started = perf_counter()
        rows = query(sql, params, cap)
        durations.append(int((perf_counter() - started) * 1000))
        query_count += 1
        if len(rows) > cap:
            raise ValueError("query result exceeded bound")
        return rows

    end_exclusive = "20261002"
    daily_r120 = limited(R120_DAILY, ("20260401", end_exclusive), 2200)
    daily_r130 = limited(R130_DAILY, ("20260401", end_exclusive), 200)
    candidates = limited(CANDIDATES, ("20260401", "20260831"), 2)
    products = tuple(str(row["product_code"]).strip() for row in candidates)
    if not products or len(set(products)) != len(products) or any(not item for item in products):
        raise ValueError("no bounded unique representative products")
    marks = ", ".join("?" for _ in products)
    sample_r120 = limited(SAMPLE_R120.format(product_marks=marks), ("20260401", "20260831", *products), 501)
    if not sample_r120 or len(sample_r120) > SAMPLE_MAX_ROWS:
        raise ValueError("R120 sample is empty or truncated")
    headers = limited(SAMPLE_R130.format(product_marks=marks), ("20260401", "20260831", *products), 501)
    if len(headers) > SAMPLE_MAX_ROWS:
        raise ValueError("R130 sample is truncated")
    duplicate_headers = sum(int(row["header_count"]) - 1 for row in headers)
    if duplicate_headers:
        raise ValueError("duplicate R130 header key; sample not suitable for forecasting")
    vendor_codes = tuple(sorted({str(row["Rd12_Ven_Cd"]).strip() for row in sample_r120}))
    vendors = limited(_select_codes(
        "Rddbc030", "Rd03_Ven_Cd, Rd03_Ven_Num, Rd03_Other1, Rd03_Supply_Cd_Gcode, Rd03_Supply_Cd",
        "Rd03_Ven_Cd", vendor_codes), vendor_codes, 501)
    product_rows = limited(_select_codes(
        "Rddbc040", "Rd04_Physic_Cd, Rd04_Add_Date", "Rd04_Physic_Cd", products), products, 501)
    standards = limited(_select_codes(
        "Rddbc046", "Rd046_Physic_Cd, Rd046_Standard_Cd", "Rd046_Physic_Cd", products), products, 501)
    if len(standards) == 501:
        raise ValueError("standard-code sample truncated")
    io_codes = tuple(sorted({str(row["Rd12_Io_Gu"]).strip() for row in sample_r120}))
    group_codes = tuple(sorted({"0012", *(str(row.get("Rd03_Supply_Cd_Gcode") or "").strip() for row in vendors)} - {""}))
    code_rows = limited(
        f"SELECT TOP (501) Rd01_Gcode, Rd01_Tcode, Rd01_Hnm FROM dbo.Rddbc010 WHERE Rd01_Gcode IN ({', '.join('?' for _ in group_codes)})",
        group_codes, 501,
    )
    if len(code_rows) == 501:
        raise ValueError("business-code sample truncated")
    for collection in (sample_r120, headers, vendors, product_rows, standards, code_rows):
        for row in collection:
            row["company_id"] = company_id
    prepared = prepare_sources(
        company_id=company_id, outbound_rows=sample_r120, document_rows=headers,
        vendor_rows=vendors, product_rows=product_rows, standard_rows=standards,
        code_rows=code_rows, saved_io_codes=io_codes,
    )
    if prepared.source_row_count != len(sample_r120):
        raise ValueError("R120 sample row count changed")
    fixture = {
        "schema_version": "ml_phase1_fixture_v1", "dataset_version": "db3-probe-" + datetime.now(KST).strftime("%Y%m%d%H%M%S"),
        "company_id": company_id, "date_from": "2026-04-01", "date_to": "2026-08-30",
        "outbound_rows": sample_r120, "document_rows": headers, "vendor_rows": vendors,
        "product_rows": product_rows, "standard_rows": standards, "code_rows": code_rows,
        "saved_io_codes": io_codes, "complete_dates": [],
        "as_of_dates": ["2026-06-30", "2026-07-31"],
        "timestamp_policy_confirmed": False, "calendar": None,
    }
    quality = {
        "source_rows_preserved": prepared.source_row_count == len(sample_r120),
        "source_quantity": str(prepared.source_quantity),
        "negative_5xx_rows": sum(1 for row in prepared.rows if row.source.io_code.startswith("5") and row.source.quantity < 0),
        "bonus_nonzero_rows": sum(1 for row in prepared.rows if row.source.bonus_quantity != 0),
        "document_match_counts": dict(prepared.document_match_counts),
        "unique_document_supply_amount": str(document_amount_once(prepared, "Rd13_Supply_Price")),
        "products_in_sample": len(products), "standard_code_missing_products": sum(
            1 for product in products if not any(row.source.product_code == product and row.standard_codes for row in prepared.rows)
        ),
        "vendor_business_number_known": sum(1 for row in vendors if row.get("Rd03_Ven_Num")),
    }
    return {"fixture": fixture, "daily_r120": daily_r120, "daily_r130": daily_r130,
            "quality": quality, "query_count": query_count, "query_durations_ms": durations}
