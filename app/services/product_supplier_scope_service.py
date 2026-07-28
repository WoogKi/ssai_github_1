"""Temporary Dashboard product supplier-scope contract.

The ERP vendor and user codes are fixed-width business strings.  This module
therefore deliberately rejects non-string codes instead of guessing a padded
value, and makes the mutually-exclusive scope explicit before SQL is built.
"""

from __future__ import annotations

from typing import Any, Mapping

import pandas as pd

from app.db.mssql_client import query_to_df


SCOPE_ALL = "all"
SCOPE_MANUFACTURER = "manufacturer"
SCOPE_ORDER_VENDOR = "order_vendor"
SCOPE_MODES = {SCOPE_ALL, SCOPE_MANUFACTURER, SCOPE_ORDER_VENDOR}


def _codes(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    seen: set[str] = set()
    result: list[str] = []
    for item in values:
        if not isinstance(item, str):
            continue
        code = item.strip()
        if code and code not in seen:
            seen.add(code)
            result.append(code)
    return result


def normalize_product_supplier_scope(params: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return one supplier group only; legacy manufacturer aliases are input-only."""
    source = dict(params or {})
    raw_mode = str(source.get("product_supplier_scope_mode") or "").strip()
    legacy_all = raw_mode == SCOPE_ALL
    # "all" is a legacy input-only value. New Dashboard requests always use
    # a concrete supplier family with empty details to mean all products.
    if raw_mode not in {SCOPE_MANUFACTURER, SCOPE_ORDER_VENDOR}:
        raw_mode = SCOPE_MANUFACTURER
    manufacturer_codes = [] if legacy_all else _codes(source.get("manufacturer_codes"))
    if not manufacturer_codes and raw_mode == SCOPE_MANUFACTURER and not legacy_all:
        manufacturer_codes = _codes(source.get("manufacturer_test_codes") or source.get("dashboard_manufacturer_codes"))
    result = {
        "product_supplier_scope_mode": raw_mode,
        "manufacturer_codes": manufacturer_codes if raw_mode == SCOPE_MANUFACTURER else [],
        "manufacturer_manager_codes": _codes(source.get("manufacturer_manager_codes")) if raw_mode == SCOPE_MANUFACTURER and not legacy_all else [],
        "order_vendor_codes": _codes(source.get("order_vendor_codes")) if raw_mode == SCOPE_ORDER_VENDOR else [],
        "purchase_manager_codes": _codes(source.get("purchase_manager_codes")) if raw_mode == SCOPE_ORDER_VENDOR else [],
    }
    result["inactive_scope_cleared"] = any(
        _codes(source.get(key))
        for key in (
            ("order_vendor_codes", "purchase_manager_codes") if raw_mode == SCOPE_MANUFACTURER else
            ("manufacturer_codes", "manufacturer_manager_codes", "manufacturer_test_codes", "dashboard_manufacturer_codes")
        )
    )
    return result


def supplier_scope_filter_active(params: Mapping[str, Any] | None) -> bool:
    """Return whether a supplier condition, not merely its mode, narrows products."""
    scope = normalize_product_supplier_scope(params)
    if scope["product_supplier_scope_mode"] == SCOPE_MANUFACTURER:
        return bool(scope["manufacturer_codes"] or scope["manufacturer_manager_codes"])
    return bool(scope["order_vendor_codes"] or scope["purchase_manager_codes"])


def apply_product_supplier_scope(params: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy and normalize scope, retaining the legacy SQL alias only as an adapter."""
    out = dict(params or {})
    scope = normalize_product_supplier_scope(out)
    out.update(scope)
    out["dashboard_manufacturer_codes"] = list(scope["manufacturer_codes"]) if scope["product_supplier_scope_mode"] == SCOPE_MANUFACTURER else []
    return out


def supplier_scope_fingerprint(params: Mapping[str, Any] | None) -> tuple[Any, ...]:
    scope = normalize_product_supplier_scope(params)
    return (
        scope["product_supplier_scope_mode"],
        tuple(sorted(scope["manufacturer_codes"])),
        tuple(sorted(scope["manufacturer_manager_codes"])),
        tuple(sorted(scope["order_vendor_codes"])),
        tuple(sorted(scope["purchase_manager_codes"])),
    )


def build_product_supplier_scope_sql(
    params: Mapping[str, Any] | None,
    binds: dict[str, Any],
    *,
    product_code_sql: str,
    bind_prefix: str,
) -> str:
    """Build one product-master EXISTS predicate for the active scope only."""
    scope = normalize_product_supplier_scope(params)
    mode = scope["product_supplier_scope_mode"]
    if mode == SCOPE_MANUFACTURER:
        product_column, codes, managers = "Rd04_Ven_Cd", scope["manufacturer_codes"], scope["manufacturer_manager_codes"]
    else:
        product_column, codes, managers = "Rd04_Orven_Cd", scope["order_vendor_codes"], scope["purchase_manager_codes"]
    product_alias = "SupplierProduct"
    vendor_alias = "SupplierVendor"
    conditions = [f"{product_alias}.Rd04_Physic_Cd = {product_code_sql}"]
    if codes:
        names = []
        for index, code in enumerate(codes):
            key = f"{bind_prefix}_vendor_{index}"
            binds[key] = code
            names.append(f"%({key})s")
        conditions.append(f"{product_alias}.{product_column} IN ({', '.join(names)})")
    if managers:
        names = []
        for index, code in enumerate(managers):
            key = f"{bind_prefix}_manager_{index}"
            binds[key] = code
            names.append(f"%({key})s")
        conditions.append(f"{vendor_alias}.Rd03_Sales_Man IN ({', '.join(names)})")
    if len(conditions) == 1:
        return ""
    return (
        f"EXISTS (SELECT 1 FROM dbo.Rddbc040 AS {product_alias} WITH (NOLOCK) "
        f"LEFT JOIN dbo.Rddbc030 AS {vendor_alias} WITH (NOLOCK) ON {product_alias}.{product_column} = {vendor_alias}.Rd03_Ven_Cd "
        "WHERE " + " AND ".join(conditions) + ")"
    )


def resolve_supplier_vendor_codes(search_text: Any, *, mode: str) -> list[dict[str, str]]:
    """Resolve one vendor class at submit time: code, exact name, then name LIKE."""
    text = " ".join(str(search_text or "").split())
    if not text or text == "전체" or mode not in {SCOPE_MANUFACTURER, SCOPE_ORDER_VENDOR}:
        return []
    product_column = "Rd04_Ven_Cd" if mode == SCOPE_MANUFACTURER else "Rd04_Orven_Cd"
    base = (
        "SELECT DISTINCT LTRIM(RTRIM(P." + product_column + ")) AS vendor_code, "
        "COALESCE(NULLIF(LTRIM(RTRIM(V.Rd03_Ven_Nm)), ''), LTRIM(RTRIM(P." + product_column + "))) AS vendor_name "
        "FROM dbo.Rddbc040 AS P WITH (NOLOCK) LEFT JOIN dbo.Rddbc030 AS V WITH (NOLOCK) "
        "ON P." + product_column + " = V.Rd03_Ven_Cd WHERE LTRIM(RTRIM(P." + product_column + ")) <> '' "
    )
    exact = query_to_df(base + "AND LTRIM(RTRIM(P." + product_column + ")) = ? ORDER BY vendor_name, vendor_code", (text,))
    df = exact if isinstance(exact, pd.DataFrame) and not exact.empty else query_to_df(base + "AND LTRIM(RTRIM(V.Rd03_Ven_Nm)) = ? ORDER BY vendor_name, vendor_code", (text,))
    if not isinstance(df, pd.DataFrame) or df.empty:
        df = query_to_df(base + "AND LTRIM(RTRIM(V.Rd03_Ven_Nm)) LIKE ? ORDER BY vendor_name, vendor_code", (f"%{text}%",))
    if not isinstance(df, pd.DataFrame):
        return []
    rows: list[dict[str, str]] = []
    for _, row in df.iterrows():
        code = str(row.get("vendor_code") or "").strip()
        if code:
            rows.append({"code": code, "name": str(row.get("vendor_name") or "").strip()})
    return rows


def load_supplier_manager_options(*, mode: str, vendor_codes: Any = None) -> list[dict[str, str]]:
    """Load only managers attached to actual product supplier links."""
    if mode not in {SCOPE_MANUFACTURER, SCOPE_ORDER_VENDOR}:
        return []
    product_column = "Rd04_Ven_Cd" if mode == SCOPE_MANUFACTURER else "Rd04_Orven_Cd"
    bind_values: tuple[str, ...] = ()
    predicate = ""
    codes = _codes(vendor_codes)
    if codes:
        predicate = f" AND P.{product_column} IN ({', '.join('?' for _ in codes)})"
        bind_values = tuple(codes)
    sql = (
        "SELECT ManagerRows.user_code, ManagerRows.user_name FROM ("
        "SELECT DISTINCT LTRIM(RTRIM(V.Rd03_Sales_Man)) AS user_code, "
        "COALESCE(NULLIF(LTRIM(RTRIM(U.Rd06_User_Nm)), ''), LTRIM(RTRIM(V.Rd03_Sales_Man))) AS user_name "
        "FROM dbo.Rddbc040 AS P WITH (NOLOCK) INNER JOIN dbo.Rddbc030 AS V WITH (NOLOCK) "
        f"ON P.{product_column} = V.Rd03_Ven_Cd LEFT JOIN dbo.Rddbc060 AS U WITH (NOLOCK) "
        "ON V.Rd03_Sales_Man = U.Rd06_User_Cd WHERE LTRIM(RTRIM(V.Rd03_Sales_Man)) <> ''"
        + predicate + ") AS ManagerRows ORDER BY ManagerRows.user_name, ManagerRows.user_code"
    )
    df = query_to_df(sql, bind_values)
    if not isinstance(df, pd.DataFrame):
        return []
    return [{"code": str(row.get("user_code") or "").strip(), "name": str(row.get("user_name") or "").strip()} for _, row in df.iterrows() if str(row.get("user_code") or "").strip()]


def supplier_scope_summary(params: Mapping[str, Any] | None, *, vendor_rows: Any = None, manager_rows: Any = None) -> dict[str, str]:
    scope = normalize_product_supplier_scope(params)
    mode = scope["product_supplier_scope_mode"]
    label = {SCOPE_ALL: "전체", SCOPE_MANUFACTURER: "제약사", SCOPE_ORDER_VENDOR: "발주처"}[mode]
    vendor_label = "전체" if not (scope["manufacturer_codes"] or scope["order_vendor_codes"]) else f"{len(scope['manufacturer_codes'] or scope['order_vendor_codes'])}개사"
    manager_label = "전체" if not (scope["manufacturer_manager_codes"] or scope["purchase_manager_codes"]) else f"{len(scope['manufacturer_manager_codes'] or scope['purchase_manager_codes'])}명"
    return {"mode_label": label, "vendor_label": vendor_label, "manager_label": manager_label}
