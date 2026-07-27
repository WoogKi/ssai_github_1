"""Read-only inbound-date facts for Dashboard Lite.

This module deliberately owns the single Rddbc110 query used by the dashboard.
Business codes remain strings end to end so leading zeroes are never lost.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
import time
from typing import Any, Mapping

import pandas as pd

from app.services.rddbc_io_common import query_to_df


log = logging.getLogger("ssai.sims.dashboard_inbound")

INBOUND_GCODE = "0012"
NORMAL_INBOUND_TCODES = ("001", "002")
INBOUND_RETURN_TCODES = ("101", "102", "193")


def _codes(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    # ERP Gcode/Tcode are business strings.  Never turn numeric values into a
    # plausible-looking code because that would lose leading-zero semantics.
    return [item.strip() for item in values if isinstance(item, str) and item.strip()]


def _pairs(value: Any, expected_gcode: str) -> list[str]:
    out: list[str] = []
    for pair in _codes(value):
        gcode, sep, tcode = pair.partition(":")
        if sep and gcode == expected_gcode and tcode:
            out.append(tcode)
    return out


def _add_pair_filter(
    clauses: list[str],
    binds: dict[str, Any],
    *,
    gcode_column: str,
    tcode_column: str,
    bind_key: str,
    values: Any,
    expected_gcode: str,
) -> None:
    tcodes = _pairs(values, expected_gcode)
    if tcodes:
        clauses.append(f"{gcode_column} = '{expected_gcode}'")
        _add_in(clauses, binds, tcode_column, bind_key, tcodes)


def _add_in(clauses: list[str], binds: dict[str, Any], column: str, key: str, values: list[str]) -> None:
    if not values:
        return
    names: list[str] = []
    for index, value in enumerate(values):
        bind_key = f"{key}_{index}"
        binds[bind_key] = str(value)
        names.append(f"%({bind_key})s")
    clauses.append(f"{column} IN ({', '.join(names)})")


def _parse_cutoff(value: str) -> date:
    text = "".join(char for char in str(value or "") if char.isdigit())[:8]
    if len(text) != 8:
        return date.today()
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return date.today()


def _sql(params: Mapping[str, Any], *, start_date: str, cutoff_date: str) -> tuple[str, dict[str, Any]]:
    binds: dict[str, Any] = {"inbound_start": str(start_date), "inbound_cutoff": str(cutoff_date)}
    product_filters: list[str] = []
    _add_in(product_filters, binds, "P.Rd04_Ven_Cd", "manufacturer", _codes(params.get("dashboard_manufacturer_codes") or params.get("manufacturer_test_codes")))
    _add_pair_filter(product_filters, binds, gcode_column="P.Rd04_Physic_Group_Gcode", tcode_column="P.Rd04_Physic_Group", bind_key="product_group", values=params.get("dashboard_product_group_list") or params.get("product_group_list"), expected_gcode="0013")
    _add_pair_filter(product_filters, binds, gcode_column="P.Rd04_Physic_Di_Gcode", tcode_column="P.Rd04_Physic_Di", bind_key="product_di", values=params.get("dashboard_product_di_list") or params.get("product_di_list"), expected_gcode="0004")
    _add_pair_filter(product_filters, binds, gcode_column="P.Rd04_Physic_Tax_Gcode", tcode_column="P.Rd04_Physic_Tax", bind_key="product_class", values=params.get("dashboard_product_class_list") or params.get("product_class_list"), expected_gcode="0031")

    transaction_filters = [
        "I.Rd11_Io_Gu_Gcode = '0012'",
        "I.Rd11_In_YyMmDd >= %(inbound_start)s",
        "I.Rd11_In_YyMmDd <= %(inbound_cutoff)s",
        "I.Rd11_Io_Gu IN ('001', '002', '101', '102', '193')",
    ]
    stock_codes = _codes(params.get("stock_cd_list"))
    if stock_codes:
        transaction_filters.append("I.Rd11_Stock_Cd_Gcode = '0018'")
        _add_in(transaction_filters, binds, "I.Rd11_Stock_Cd", "stock_cd", stock_codes)
    vendor_filters: list[str] = []
    _add_pair_filter(vendor_filters, binds, gcode_column="FilterVendor.Rd03_Ven_Group_Gcode", tcode_column="FilterVendor.Rd03_Ven_Group", bind_key="vendor_group", values=params.get("vendor_group_list"), expected_gcode="0019")
    _add_pair_filter(vendor_filters, binds, gcode_column="FilterVendor.Rd03_Ven_Kind_Gcode", tcode_column="FilterVendor.Rd03_Ven_Kind", bind_key="vendor_kind", values=params.get("vendor_kind_list"), expected_gcode="0009")
    if vendor_filters:
        transaction_filters.append(
            "EXISTS (SELECT 1 FROM dbo.Rddbc030 AS FilterVendor WITH (NOLOCK) "
            "WHERE FilterVendor.Rd03_Ven_Cd = I.Rd11_Ven_Cd AND "
            + " AND ".join(vendor_filters)
            + ")"
        )
    where_sql = "\n  AND ".join(product_filters) if product_filters else "1 = 1"
    on_sql = "\n   AND ".join(transaction_filters)
    return f"""
SELECT
    LTRIM(RTRIM(P.Rd04_Physic_Cd)) AS product_code,
    LTRIM(RTRIM(P.Rd04_Orven_Cd)) AS master_order_vendor_code,
    LTRIM(RTRIM(MasterVendor.Rd03_Ven_Nm)) AS master_order_vendor_name,
    LTRIM(RTRIM(I.Rd11_In_YyMmDd)) AS inbound_date,
    LTRIM(RTRIM(I.Rd11_Io_Gu)) AS io_tcode,
    LTRIM(RTRIM(I.Rd11_Ven_Cd)) AS vendor_code,
    LTRIM(RTRIM(InboundVendor.Rd03_Ven_Nm)) AS inbound_vendor_name,
    CAST(COALESCE(I.Rd11_Quantity, 0) AS decimal(28, 6)) AS quantity,
    CAST(COALESCE(I.Rd11_Oquantity, 0) AS decimal(28, 6)) AS oquantity,
    CAST(COALESCE(I.Rd11_Supply_Price, 0) AS decimal(28, 6)) AS supply_price
FROM dbo.Rddbc040 AS P WITH (NOLOCK)
LEFT JOIN dbo.Rddbc110 AS I WITH (NOLOCK)
    ON I.Rd11_Physic_Cd = P.Rd04_Physic_Cd
   AND {on_sql}
LEFT JOIN dbo.Rddbc030 AS InboundVendor WITH (NOLOCK)
    ON InboundVendor.Rd03_Ven_Cd = I.Rd11_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS MasterVendor WITH (NOLOCK)
    ON MasterVendor.Rd03_Ven_Cd = P.Rd04_Orven_Cd
WHERE {where_sql}
""", binds


def build_dashboard_inbound_facts_frame(
    source_df: pd.DataFrame | None,
    *,
    data_cutoff_date: str,
    cycle_lookback_days: int = 365,
    vendor_lookback_days: int = 90,
) -> pd.DataFrame:
    """Reduce the one-query row source to one vectorized inbound facts row per product."""
    started = time.perf_counter()
    columns = [
        "product_code", "last_normal_inbound_date", "normal_inbound_day_count_365",
        "avg_inbound_cycle_days", "inbound_delay_days", "inbound_delay_threshold_days",
        "inbound_data_status", "inbound_delayed_candidate", "normal_inbound_raw_qty_365",
        "normal_inbound_positive_qty_365", "inbound_return_raw_qty_365",
        "normal_inbound_90_exists", "normal_inbound_365_exists", "recent_inbound_vendor_code",
        "recent_inbound_vendor_name", "recent_inbound_vendor_qty_90", "recent_inbound_vendor_last_date", "recent_inbound_vendor_source",
        "recent_inbound_vendor_fallback", "inbound_cycle_days", "inbound_vendor_days", "data_cutoff_date",
    ]
    raw = source_df.copy() if isinstance(source_df, pd.DataFrame) else pd.DataFrame()
    if raw.empty or "product_code" not in raw.columns:
        result = pd.DataFrame(columns=columns)
        result.attrs["inbound_build_elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        log.info("[dashboard.inbound.build] source_rows=0 product_rows=0 normalize_ms=0 event_aggregate_ms=0 gap_aggregate_ms=0 vendor_aggregate_ms=0 merge_finalize_ms=0 build_elapsed_ms=%s", result.attrs["inbound_build_elapsed_ms"])
        return result

    normalize_started = time.perf_counter()
    for key in ("inbound_date", "io_tcode", "vendor_code", "inbound_vendor_name", "master_order_vendor_code", "master_order_vendor_name"):
        if key not in raw.columns:
            raw[key] = ""
        raw[key] = raw[key].fillna("").astype(str).str.strip()
    raw["product_code"] = raw["product_code"].fillna("").astype(str).str.strip()
    raw = raw.loc[raw["product_code"].ne("")].copy()
    for key in ("quantity", "oquantity", "supply_price"):
        if key not in raw.columns:
            raw[key] = 0.0
        raw[key] = pd.to_numeric(raw[key], errors="coerce").fillna(0.0)
    cutoff = _parse_cutoff(data_cutoff_date)
    cutoff_ts = pd.Timestamp(cutoff)
    cycle_start_ts = cutoff_ts - pd.Timedelta(days=max(1, int(cycle_lookback_days)) - 1)
    vendor_start_ts = cutoff_ts - pd.Timedelta(days=max(1, int(vendor_lookback_days)) - 1)
    raw["inbound_date_value"] = pd.to_datetime(raw["inbound_date"], format="%Y%m%d", errors="coerce")
    raw["event_qty"] = raw["quantity"] + raw["oquantity"]
    raw["within_cycle"] = raw["inbound_date_value"].between(cycle_start_ts, cutoff_ts, inclusive="both")
    raw["within_vendor"] = raw["inbound_date_value"].between(vendor_start_ts, cutoff_ts, inclusive="both")
    raw["is_normal"] = raw["within_cycle"] & raw["io_tcode"].isin(NORMAL_INBOUND_TCODES)
    raw["is_return"] = raw["within_cycle"] & raw["io_tcode"].isin(INBOUND_RETURN_TCODES)
    raw["is_positive_normal"] = raw["is_normal"] & raw["event_qty"].gt(0)
    normalize_ms = int((time.perf_counter() - normalize_started) * 1000)

    master = raw[["product_code", "master_order_vendor_code", "master_order_vendor_name"]].copy()
    master[["master_order_vendor_code", "master_order_vendor_name"]] = master[["master_order_vendor_code", "master_order_vendor_name"]].replace("", pd.NA)
    master = master.groupby("product_code", as_index=False, sort=False).first().fillna("")
    positive = raw.loc[raw["is_positive_normal"], ["product_code", "inbound_date_value", "event_qty", "quantity", "supply_price", "vendor_code", "inbound_vendor_name", "within_vendor"]].copy()
    event_aggregate_started = time.perf_counter()
    normal_raw = raw.loc[raw["is_normal"]].groupby("product_code", sort=False)["event_qty"].sum().rename("normal_inbound_raw_qty_365")
    normal_positive = positive.groupby("product_code", sort=False)["event_qty"].sum().rename("normal_inbound_positive_qty_365")
    returns = raw.loc[raw["is_return"]].groupby("product_code", sort=False)["event_qty"].sum().rename("inbound_return_raw_qty_365")
    history_365 = positive.groupby("product_code", sort=False).size().rename("_history_365_count")
    history_90 = positive.loc[positive["within_vendor"]].groupby("product_code", sort=False).size().rename("_history_90_count")
    event_stats = pd.concat([normal_raw, normal_positive, returns, history_365, history_90], axis=1).reset_index()
    event_aggregate_ms = int((time.perf_counter() - event_aggregate_started) * 1000)
    gap_started = time.perf_counter()
    normal_days = positive[["product_code", "inbound_date_value"]].drop_duplicates().sort_values(["product_code", "inbound_date_value"], kind="stable")
    normal_days["gap_days"] = normal_days.groupby("product_code", sort=False)["inbound_date_value"].diff().dt.days
    day_stats = normal_days.groupby("product_code", as_index=False, sort=False).agg(
        last_date=("inbound_date_value", "max"),
        normal_inbound_day_count_365=("inbound_date_value", "size"),
        avg_inbound_cycle_days=("gap_days", "mean"),
    )
    gap_aggregate_ms = int((time.perf_counter() - gap_started) * 1000)

    vendor_started = time.perf_counter()
    recent = positive.loc[positive["within_vendor"] & positive["vendor_code"].ne("")].copy()
    if recent.empty:
        vendor_top = pd.DataFrame(columns=["product_code", "recent_inbound_vendor_code", "recent_inbound_vendor_name", "recent_inbound_vendor_qty_90", "recent_inbound_vendor_last_date"])
    else:
        recent["inbound_vendor_name"] = recent["inbound_vendor_name"].replace("", pd.NA)
        vendor_top = recent.groupby(["product_code", "vendor_code"], as_index=False, sort=False).agg(
            recent_inbound_vendor_qty_90=("quantity", "sum"),
            _supply_price=("supply_price", "sum"),
            _last_date=("inbound_date_value", "max"),
            recent_inbound_vendor_name=("inbound_vendor_name", "first"),
        )
        vendor_top = vendor_top.sort_values(
            ["product_code", "recent_inbound_vendor_qty_90", "_supply_price", "_last_date", "vendor_code"],
            ascending=[True, False, False, False, True], kind="stable",
        ).drop_duplicates("product_code", keep="first")
        vendor_top = vendor_top.rename(columns={"vendor_code": "recent_inbound_vendor_code", "_last_date": "recent_inbound_vendor_last_date"})
        vendor_top["recent_inbound_vendor_name"] = vendor_top["recent_inbound_vendor_name"].fillna("")
        vendor_top["recent_inbound_vendor_last_date"] = vendor_top["recent_inbound_vendor_last_date"].dt.strftime("%Y%m%d")
        vendor_top = vendor_top[["product_code", "recent_inbound_vendor_code", "recent_inbound_vendor_name", "recent_inbound_vendor_qty_90", "recent_inbound_vendor_last_date"]]
    vendor_aggregate_ms = int((time.perf_counter() - vendor_started) * 1000)

    finalize_started = time.perf_counter()
    result = master.merge(day_stats, on="product_code", how="left").merge(event_stats, on="product_code", how="left").merge(vendor_top, on="product_code", how="left")
    for key in ("normal_inbound_raw_qty_365", "normal_inbound_positive_qty_365", "inbound_return_raw_qty_365", "recent_inbound_vendor_qty_90"):
        result[key] = pd.to_numeric(result.get(key), errors="coerce").fillna(0.0)
    result["normal_inbound_day_count_365"] = result["normal_inbound_day_count_365"].fillna(0).astype(int)
    result["normal_inbound_365_exists"] = result["_history_365_count"].fillna(0).gt(0)
    result["normal_inbound_90_exists"] = result["_history_90_count"].fillna(0).gt(0)
    result["inbound_delay_days"] = (cutoff_ts - result["last_date"]).dt.days
    result["inbound_delay_threshold_days"] = result["avg_inbound_cycle_days"].mul(2.0).clip(lower=14.0)
    result["inbound_delayed_candidate"] = result["normal_inbound_day_count_365"].ge(2) & result["inbound_delay_days"].gt(result["inbound_delay_threshold_days"])
    result["inbound_data_status"] = "normal"
    result.loc[result["normal_inbound_day_count_365"].le(1), "inbound_data_status"] = "insufficient"
    result.loc[result["inbound_delayed_candidate"], "inbound_data_status"] = "delayed_candidate"
    has_actual = result["recent_inbound_vendor_code"].fillna("").astype(str).ne("")
    has_master = result["master_order_vendor_code"].fillna("").astype(str).ne("")
    result["recent_inbound_vendor_source"] = "none"
    result.loc[has_master & ~has_actual, "recent_inbound_vendor_source"] = "master_order_vendor"
    result.loc[has_actual, "recent_inbound_vendor_source"] = "actual_inbound"
    result.loc[~has_actual & has_master, "recent_inbound_vendor_code"] = result.loc[~has_actual & has_master, "master_order_vendor_code"]
    result.loc[~has_actual & has_master, "recent_inbound_vendor_name"] = result.loc[~has_actual & has_master, "master_order_vendor_name"]
    result["recent_inbound_vendor_fallback"] = result["recent_inbound_vendor_source"].eq("master_order_vendor")
    result["last_normal_inbound_date"] = result["last_date"].dt.strftime("%Y%m%d").fillna("")
    result["recent_inbound_vendor_last_date"] = result["recent_inbound_vendor_last_date"].fillna("")
    result["recent_inbound_vendor_name"] = result["recent_inbound_vendor_name"].fillna("")
    result["recent_inbound_vendor_code"] = result["recent_inbound_vendor_code"].fillna("")
    result["inbound_cycle_days"] = int(cycle_lookback_days)
    result["inbound_vendor_days"] = int(vendor_lookback_days)
    result["data_cutoff_date"] = cutoff.strftime("%Y%m%d")
    result = result.reindex(columns=columns)
    merge_finalize_ms = int((time.perf_counter() - finalize_started) * 1000)
    result.attrs["inbound_build_elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    log.info(
        "[dashboard.inbound.build] source_rows=%s product_rows=%s normalize_ms=%s event_aggregate_ms=%s gap_aggregate_ms=%s vendor_aggregate_ms=%s merge_finalize_ms=%s build_elapsed_ms=%s",
        len(raw), len(result), normalize_ms, event_aggregate_ms, gap_aggregate_ms, vendor_aggregate_ms, merge_finalize_ms, result.attrs["inbound_build_elapsed_ms"],
    )
    return result


def get_dashboard_inbound_facts(
    params: dict,
    *,
    data_cutoff_date: str,
    cycle_lookback_days: int = 365,
    vendor_lookback_days: int = 90,
) -> pd.DataFrame:
    """Execute exactly one read-only Rddbc110 query and return product facts."""
    started = time.perf_counter()
    cutoff = _parse_cutoff(data_cutoff_date)
    cycle_days = max(1, int(cycle_lookback_days))
    vendor_days = max(1, int(vendor_lookback_days))
    start_date = (cutoff - timedelta(days=cycle_days - 1)).strftime("%Y%m%d")
    sql, binds = _sql(params or {}, start_date=start_date, cutoff_date=cutoff.strftime("%Y%m%d"))
    query_started = time.perf_counter()
    source = query_to_df(sql, binds)
    query_elapsed_ms = int((time.perf_counter() - query_started) * 1000)
    build_started = time.perf_counter()
    facts = build_dashboard_inbound_facts_frame(
        source,
        data_cutoff_date=cutoff.strftime("%Y%m%d"),
        cycle_lookback_days=cycle_days,
        vendor_lookback_days=vendor_days,
    )
    facts.attrs["inbound_source_rows"] = int(len(source))
    facts.attrs["inbound_query_elapsed_ms"] = query_elapsed_ms
    facts.attrs["inbound_build_elapsed_ms"] = int((time.perf_counter() - build_started) * 1000)
    facts.attrs["inbound_source_elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    facts.attrs["inbound_query_count"] = 1
    facts.attrs["inbound_sql"] = sql
    facts.attrs["inbound_binds"] = dict(binds)
    log.info(
        "[dashboard.inbound.source] cycle_days=%s vendor_days=%s source_rows=%s product_rows=%s query_elapsed_ms=%s query_count=1 cache_used=False",
        cycle_days, vendor_days, len(source), len(facts), query_elapsed_ms,
    )
    return facts
