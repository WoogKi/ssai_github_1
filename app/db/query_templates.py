"""
app/db/query_templates.py
-------------------------
Common, parameterized MSSQL query templates for ERP-style analytics.
Designed for use with pyodbc (positional parameters: "?") and the helper
functions in `app/db/mssql_client.py` (e.g., read_df(sql, params)).

⚠️ Schema-agnostic
These templates assume generic table/column names. Replace placeholders
(dbo.SalesOrder, OrderDate, Amount, etc.) with your actual schema.
Keep parameter order exactly as noted in PARAMS_ORDER.

Usage example:
    from app.db.mssql_client import read_df
    from app.db.query_templates import SALES_BY_DATE_RANGE, PARAMS_ORDER

    df = read_df(SALES_BY_DATE_RANGE, ( '2025-09-01', '2025-09-18' ))
    # PARAMS_ORDER['SALES_BY_DATE_RANGE'] == ['start_date', 'end_date']

Notes:
- Prefer OFFSET/FETCH for paging instead of TOP(?) to avoid edge cases.
- Always parameterize with "?" to prevent SQL injection.
- For LIKE searches, pass pattern including % in params (e.g., "%ABC%").
"""

from typing import Dict, List

__all__ = [
    "SALES_BY_DATE_RANGE",
    "SALES_BY_DATE_RANGE_PAGED",
    "INOUT_BY_DATE_RANGE",
    "RECENT_RECEIPTS",
    "RECENT_SHIPMENTS",
    "CUSTOMER_SALES_RANKING",
    "PRODUCT_SALES_RANKING",
    "MONTHLY_SALES_SUMMARY",
    "INVENTORY_SNAPSHOT_BY_DATE",
    "AR_AGING_SUMMARY",
    "AP_AGING_SUMMARY",
    "INVENTORY_TURNOVER_ROLLING",
    "SEARCH_PRODUCT_BY_NAME",
    "PARAMS_ORDER",
]

# ----- Sales / Orders -----

SALES_BY_DATE_RANGE = """
SELECT
    so.SalesOrderID,              -- PK
    so.OrderDate,
    so.CustomerID,
    so.ItemID,
    so.Qty,
    so.UnitPrice,
    so.Amount,                    -- (Qty * UnitPrice) or stored total
    so.SalespersonID
FROM dbo.SalesOrder AS so WITH (NOLOCK)
WHERE so.OrderDate BETWEEN ? AND ?
ORDER BY so.OrderDate ASC;
"""
# params: (start_date, end_date)

SALES_BY_DATE_RANGE_PAGED = """
SELECT
    so.SalesOrderID,
    so.OrderDate,
    so.CustomerID,
    so.ItemID,
    so.Qty,
    so.UnitPrice,
    so.Amount
FROM dbo.SalesOrder AS so WITH (NOLOCK)
WHERE so.OrderDate BETWEEN ? AND ?
ORDER BY so.OrderDate DESC
OFFSET ? ROWS FETCH NEXT ? ROWS ONLY;  -- (offset, page_size)
"""
# params: (start_date, end_date, offset, page_size)

# ----- Warehouse In/Out (입출고) -----

INOUT_BY_DATE_RANGE = """
SELECT
    io.IoDate,          -- 날짜
    SUM(io.InQty)  AS InQty,
    SUM(io.OutQty) AS OutQty
FROM dbo.InOutDaily AS io WITH (NOLOCK)
WHERE io.IoDate BETWEEN ? AND ?
GROUP BY io.IoDate
ORDER BY io.IoDate ASC;
"""
# params: (start_date, end_date)

RECENT_RECEIPTS = """
SELECT TOP (100)
    r.ReceiptID,
    r.ReceiptDate,
    r.SupplierID,
    r.ItemID,
    r.Qty,
    r.UnitCost,
    r.Amount
FROM dbo.Receipts AS r WITH (NOLOCK)
WHERE r.ReceiptDate >= DATEADD(DAY, -?, CAST(GETDATE() AS date))  -- last ? days
ORDER BY r.ReceiptDate DESC;
"""
# params: (days_back,)

RECENT_SHIPMENTS = """
SELECT TOP (100)
    s.ShipmentID,
    s.ShipDate,
    s.CustomerID,
    s.ItemID,
    s.Qty,
    s.UnitPrice,
    s.Amount
FROM dbo.Shipments AS s WITH (NOLOCK)
WHERE s.ShipDate >= DATEADD(DAY, -?, CAST(GETDATE() AS date))
ORDER BY s.ShipDate DESC;
"""
# params: (days_back,)

# ----- Rankings -----

CUSTOMER_SALES_RANKING = """
SELECT TOP (?)
    c.CustomerID,
    c.CustomerName,
    SUM(so.Amount) AS SalesAmount
FROM dbo.SalesOrder AS so WITH (NOLOCK)
JOIN dbo.Customer AS c WITH (NOLOCK) ON c.CustomerID = so.CustomerID
WHERE so.OrderDate BETWEEN ? AND ?
GROUP BY c.CustomerID, c.CustomerName
ORDER BY SalesAmount DESC;
"""
# params: (top_n, start_date, end_date)
# NOTE: TOP (?) works in most drivers with int param; if you see issues, switch to OFFSET/FETCH pagination.

PRODUCT_SALES_RANKING = """
SELECT TOP (?)
    p.ItemID,
    p.ItemName,
    SUM(so.Qty)    AS TotalQty,
    SUM(so.Amount) AS SalesAmount
FROM dbo.SalesOrder AS so WITH (NOLOCK)
JOIN dbo.Product AS p WITH (NOLOCK) ON p.ItemID = so.ItemID
WHERE so.OrderDate BETWEEN ? AND ?
GROUP BY p.ItemID, p.ItemName
ORDER BY SalesAmount DESC;
"""
# params: (top_n, start_date, end_date)

# ----- Periodic summaries -----

MONTHLY_SALES_SUMMARY = """
SELECT
    CONVERT(char(7), so.OrderDate, 23) AS YearMonth,  -- 'YYYY-MM'
    SUM(so.Amount) AS SalesAmount,
    COUNT(*)       AS OrderCount
FROM dbo.SalesOrder AS so WITH (NOLOCK)
WHERE so.OrderDate BETWEEN ? AND ?
GROUP BY CONVERT(char(7), so.OrderDate, 23)
ORDER BY YearMonth;
"""
# params: (start_date, end_date)

# ----- Inventory (재고) -----

INVENTORY_SNAPSHOT_BY_DATE = """
-- Snapshot 테이블이 있는 경우 예시
SELECT
    s.SnapDate,
    s.ItemID,
    s.LocationID,
    s.OnHandQty,
    s.AvailableQty
FROM dbo.InventorySnapshot AS s WITH (NOLOCK)
WHERE s.SnapDate = ?
ORDER BY s.ItemID, s.LocationID;
"""
# params: (snap_date,)

INVENTORY_TURNOVER_ROLLING = """
-- Item별 롤링 90일 기준 재고회전율 예시
-- 회전율 ≈ COGS / AvgInventory; 여기서는 출고수량 기반 근사치 사용
WITH outflow AS (
    SELECT i.ItemID, SUM(i.OutQty) AS OutQty90
    FROM dbo.InOutDaily AS i WITH (NOLOCK)
    WHERE i.IoDate BETWEEN DATEADD(DAY, -90, CAST(GETDATE() AS date)) AND CAST(GETDATE() AS date)
    GROUP BY i.ItemID
),
avg_inv AS (
    SELECT k.ItemID, AVG(k.OnHandQty) AS AvgOnHand90
    FROM dbo.InventorySnapshot AS k WITH (NOLOCK)
    WHERE k.SnapDate BETWEEN DATEADD(DAY, -90, CAST(GETDATE() AS date)) AND CAST(GETDATE() AS date)
    GROUP BY k.ItemID
)
SELECT
    COALESCE(o.ItemID, a.ItemID) AS ItemID,
    o.OutQty90,
    a.AvgOnHand90,
    CASE WHEN a.AvgOnHand90 > 0 THEN CAST(o.OutQty90 AS float) / a.AvgOnHand90 ELSE NULL END AS Turnover90D
FROM outflow o
FULL OUTER JOIN avg_inv a ON a.ItemID = o.ItemID
ORDER BY Turnover90D DESC;
"""
# params: () none

# ----- AR/AP (채권/채무) -----

AR_AGING_SUMMARY = """
-- 매출채권 연령분석 (예시: 0-30, 31-60, 61-90, 90+)
SELECT
    a.CustomerID,
    SUM(CASE WHEN DaysPastDue BETWEEN 0  AND 30 THEN a.Balance ELSE 0 END) AS DUE_0_30,
    SUM(CASE WHEN DaysPastDue BETWEEN 31 AND 60 THEN a.Balance ELSE 0 END) AS DUE_31_60,
    SUM(CASE WHEN DaysPastDue BETWEEN 61 AND 90 THEN a.Balance ELSE 0 END) AS DUE_61_90,
    SUM(CASE WHEN DaysPastDue > 90 THEN a.Balance ELSE 0 END)             AS DUE_90_PLUS,
    SUM(a.Balance) AS TotalBalance
FROM (
    SELECT
        ar.CustomerID,
        ar.InvoiceID,
        ar.InvoiceDate,
        ar.DueDate,
        ar.Balance,
        DATEDIFF(DAY, ar.DueDate, CAST(GETDATE() AS date)) AS DaysPastDue
    FROM dbo.AR_Open AS ar WITH (NOLOCK)
) AS a
GROUP BY a.CustomerID
ORDER BY TotalBalance DESC;
"""
# params: () none

AP_AGING_SUMMARY = """
-- 매입채무 연령분석 (예시: 0-30, 31-60, 61-90, 90+)
SELECT
    a.SupplierID,
    SUM(CASE WHEN DaysPastDue BETWEEN 0  AND 30 THEN a.Balance ELSE 0 END) AS DUE_0_30,
    SUM(CASE WHEN DaysPastDue BETWEEN 31 AND 60 THEN a.Balance ELSE 0 END) AS DUE_31_60,
    SUM(CASE WHEN DaysPastDue BETWEEN 61 AND 90 THEN a.Balance ELSE 0 END) AS DUE_61_90,
    SUM(CASE WHEN DaysPastDue > 90 THEN a.Balance ELSE 0 END)             AS DUE_90_PLUS,
    SUM(a.Balance) AS TotalBalance
FROM (
    SELECT
        ap.SupplierID,
        ap.BillID,
        ap.BillDate,
        ap.DueDate,
        ap.Balance,
        DATEDIFF(DAY, ap.DueDate, CAST(GETDATE() AS date)) AS DaysPastDue
    FROM dbo.AP_Open AS ap WITH (NOLOCK)
) AS a
GROUP BY a.SupplierID
ORDER BY TotalBalance DESC;
"""
# params: () none

# ----- Search helpers -----

SEARCH_PRODUCT_BY_NAME = """
SELECT TOP (50)
    p.ItemID,
    p.ItemName,
    p.Category,
    p.Unit,
    p.Price
FROM dbo.Product AS p WITH (NOLOCK)
WHERE p.ItemName LIKE ?
ORDER BY p.ItemName;
"""
# params: (pattern,)  e.g., ("%암로디핀%",)

# Keep param order for easy reference (name -> param names in order)
PARAMS_ORDER: Dict[str, List[str]] = {
    "SALES_BY_DATE_RANGE": ["start_date", "end_date"],
    "SALES_BY_DATE_RANGE_PAGED": ["start_date", "end_date", "offset", "page_size"],
    "INOUT_BY_DATE_RANGE": ["start_date", "end_date"],
    "RECENT_RECEIPTS": ["days_back"],
    "RECENT_SHIPMENTS": ["days_back"],
    "CUSTOMER_SALES_RANKING": ["top_n", "start_date", "end_date"],
    "PRODUCT_SALES_RANKING": ["top_n", "start_date", "end_date"],
    "MONTHLY_SALES_SUMMARY": ["start_date", "end_date"],
    "INVENTORY_SNAPSHOT_BY_DATE": ["snap_date"],
    "AR_AGING_SUMMARY": [],
    "AP_AGING_SUMMARY": [],
    "INVENTORY_TURNOVER_ROLLING": [],
    "SEARCH_PRODUCT_BY_NAME": ["pattern"],
}
