# app/services/rddbc040_service.py
# -*- coding: utf-8 -*-
# VERSION = "rddbc040_service/2026-02-24-001"
# 제품코드(상품) 마스터(Rddbc040)

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
from datetime import datetime, timedelta

import logging
import time
import pandas as pd
import re

try:
    import streamlit as st  # type: ignore
except Exception:
    st = None

from app.db.mssql_client import read_df, log_sql
from app.db.schema_map import SCHEMA as S
from app.db.schema_utils import al as _al_shared
from app.db.sql_utils import sql_safe_int

log = logging.getLogger("ssai")


def _cache_data(ttl: int = 60, show_spinner: bool = False):
    if st is not None and hasattr(st, "cache_data"):
        return st.cache_data(ttl=ttl, show_spinner=show_spinner)
    def _noop(fn): return fn
    return _noop


def _log_sql(sql: str, params: Any = None, tag: str = "[rddbc040]") -> None:
    try:
        log_sql(tag, sql, params)
        return
    except TypeError:
        pass
    try:
        log_sql(sql, params)
        return
    except TypeError:
        pass
    try:
        log_sql(sql)
    except Exception:
        return


def _sql_safe_int(col_ref: str) -> str:
    return sql_safe_int(col_ref)


def _get_table_and_cols() -> Tuple[str, Dict[str, str]]:
    try:
        return S["tables"]["rddbc040"], S["cols"]["rddbc040"]
    except Exception:
        # 최소 fallback (핵심만)
        return "dbo.Rddbc040", {
            "physic_cd": "Rd04_Physic_Cd",
            "insu_cd": "Rd04_Insu_Cd",
            "physic_nm": "Rd04_Physic_Nm",
            "ven_cd": "Rd04_Ven_Cd",
            "bar1": "Rd04_Bar_Code1",
            "bar2": "Rd04_Bar_Code2",
            "bar3": "Rd04_Bar_Code3",
            "bar4": "Rd04_Bar_Code4",
            "bar5": "Rd04_Bar_Code5",
            "group_g": "Rd04_Physic_Group_Gcode",
            "group": "Rd04_Physic_Group",
            "di_g": "Rd04_Physic_Di_Gcode",
            "di": "Rd04_Physic_Di",
            "flag_g": "Rd04_Physic_Flag_Gcode",
            "flag": "Rd04_Physic_Flag",
            "cons_g": "Rd04_Cons_Gcode",
            "cons": "Rd04_Cons",
            "physic_gu_g": "Rd04_Physic_Gu_Gcode",
            "physic_gu": "Rd04_Physic_Gu",
            "insu_date": "Rd04_Insu_Date",
            "insu_price": "Rd04_Insu_Price",
            "unit": "Rd04_Unit",
            "standard": "Rd04_Standard",
            "use_gu": "Rd04_Use_Gu",
            "delflag": "Rd04_Del_Flag",
            "add_date": "Rd04_Add_Date",
            "add_cd": "Rd04_Add_Cd",
            "mod_date": "Rd04_Mod_Date",
            "mod_cd": "Rd04_Mod_Cd",
        }


def _get_table_and_cols_030() -> Tuple[str, Dict[str, str]]:
    try:
        return S["tables"]["rddbc030"], S["cols"]["rddbc030"]
    except Exception:
        return "dbo.Rddbc030", {"ven_cd": "Rd03_Ven_Cd", "ven_nm": "Rd03_Ven_Nm"}


def _get_table_and_cols_060() -> Tuple[str, Dict[str, str]]:
    try:
        return S["tables"]["rddbc060"], S["cols"]["rddbc060"]
    except Exception:
        return "dbo.Rddbc060", {"user_cd": "Rd06_User_Cd", "user_nm": "Rd06_User_Nm"}


def _get_table_and_cols_010() -> Tuple[str, Dict[str, str]]:
    try:
        return S["tables"]["rddbc010"], S["cols"]["rddbc010"]
    except Exception:
        return "dbo.Rddbc010", {"gcode": "Rd01_Gcode", "tcode": "Rd01_Tcode", "hnm": "Rd01_Hnm"}


def _al(cols: Dict[str, str], key: str, default: str) -> str:
    return _al_shared(cols, key, default)


def _norm_params(params):
    if params is None:
        return None
    if isinstance(params, list):
        return tuple(params) if params else None
    return params

def _digits_only(value: Optional[str]) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _last_day_of_month(yyyymm: str) -> str:
    first = datetime.strptime(yyyymm + "01", "%Y%m%d")
    if first.month == 12:
        next_first = first.replace(year=first.year + 1, month=1, day=1)
    else:
        next_first = first.replace(month=first.month + 1, day=1)
    return (next_first - timedelta(days=1)).strftime("%Y%m%d")


def _norm_date_from(value: Optional[str]) -> str:
    digits = _digits_only(value)
    if len(digits) == 8:
        return digits
    if len(digits) == 6:
        return digits + "01"
    return ""


def _norm_date_to(value: Optional[str]) -> str:
    digits = _digits_only(value)
    if len(digits) == 8:
        return digits
    if len(digits) == 6:
        return _last_day_of_month(digits)
    return ""

def _parse_num_range(value: Optional[str]) -> Tuple[Optional[float], Optional[float]]:
    s = str(value or "").replace(",", "").strip()
    if not s:
        return None, None

    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(?:~|-)\s*([0-9]+(?:\.[0-9]+)?)$", s)
    if m:
        return float(m.group(1)), float(m.group(2))

    try:
        v = float(s)
        return v, v
    except Exception:
        return None, None

def _build_from_join() -> Tuple[str, Dict[str, str]]:
    """
    Rddbc040 + 조인:
    - Rddbc030: 제약사명
    - Rddbc010: (gcode,tcode) 코드명들
    - Rddbc060: 등록/수정자명
    """
    T040, C040 = _get_table_and_cols()
    T030, C030 = _get_table_and_cols_030()
    T060, C060 = _get_table_and_cols_060()
    T010, C010 = _get_table_and_cols_010()

    physic_cd = _al(C040, "physic_cd", "Rd04_Physic_Cd")
    ven_cd = _al(C040, "ven_cd", "Rd04_Ven_Cd")
    add_cd = _al(C040, "add_cd", "Rd04_Add_Cd")
    mod_cd = _al(C040, "mod_cd", "Rd04_Mod_Cd")

    group_g = _al(C040, "group_g", "Rd04_Physic_Group_Gcode")
    group_t = _al(C040, "group", "Rd04_Physic_Group")
    di_g = _al(C040, "di_g", "Rd04_Physic_Di_Gcode")
    di_t = _al(C040, "di", "Rd04_Physic_Di")
    flag_g = _al(C040, "flag_g", "Rd04_Physic_Flag_Gcode")
    flag_t = _al(C040, "flag", "Rd04_Physic_Flag")
    cons_g = _al(C040, "cons_g", "Rd04_Cons_Gcode")
    cons_t = _al(C040, "cons", "Rd04_Cons")
    pg_g = _al(C040, "physic_gu_g", "Rd04_Physic_Gu_Gcode")
    pg_t = _al(C040, "physic_gu", "Rd04_Physic_Gu")

    ven_cd_030 = _al(C030, "ven_cd", "Rd03_Ven_Cd")
    ven_nm_030 = _al(C030, "ven_nm", "Rd03_Ven_Nm")

    user_cd = _al(C060, "user_cd", "Rd06_User_Cd")
    user_nm = _al(C060, "user_nm", "Rd06_User_Nm")

    gcode = _al(C010, "gcode", "Rd01_Gcode")
    tcode = _al(C010, "tcode", "Rd01_Tcode")
    hnm = _al(C010, "hnm", "Rd01_Hnm")

    from_join = f"""
FROM {T040} a
LEFT JOIN {T030} v   ON a.{ven_cd} = v.{ven_cd_030}
LEFT JOIN {T060} au  ON a.{add_cd} = au.{user_cd}
LEFT JOIN {T060} mu  ON a.{mod_cd} = mu.{user_cd}

LEFT JOIN {T010} g   ON a.{group_g} = g.{gcode} AND a.{group_t} = g.{tcode}
LEFT JOIN {T010} d   ON a.{di_g}    = d.{gcode} AND a.{di_t}    = d.{tcode}
LEFT JOIN {T010} f   ON a.{flag_g}  = f.{gcode} AND a.{flag_t}  = f.{tcode}
LEFT JOIN {T010} c   ON a.{cons_g}  = c.{gcode} AND a.{cons_t}  = c.{tcode}
LEFT JOIN {T010} pg  ON a.{pg_g}    = pg.{gcode} AND a.{pg_t}   = pg.{tcode}
""".strip()

    extra = {
        "ven_nm": f"v.{ven_nm_030}",
        "group_name": f"g.{hnm}",
        "di_name": f"d.{hnm}",
        "flag_name": f"f.{hnm}",
        "cons_name": f"c.{hnm}",
        "physic_gu_name": f"pg.{hnm}",
        "add_user_nm": f"au.{user_nm}",
        "mod_user_nm": f"mu.{user_nm}",
        "order_key": f"a.{physic_cd}",
    }
    return from_join, extra


# 검색/목록: 제품코드(상품) 검색 및 목록 조회. 다양한 검색조건 허용. 결과는 조인된 정보 포함 전체 컬럼.
@_cache_data(ttl=60, show_spinner=False)
def search_goods_full(
    *,
    top: int = 2000,
    keyword: str = "",
    physic_cd: str = "",
    insu_cd: str = "",
    barcode: str = "",
    ven_nm_kw: str = "",
    group_name_kw: str = "",
    di_name_kw: str = "",

    physic_gu_name_kw: str = "",
    add_user_nm_kw: str = "",
    add_date_from: str = "",
    add_date_to: str = "",
    mod_user_nm_kw: str = "",
    mod_date_from: str = "",
    mod_date_to: str = "",

    unit_price_kw: str = "",
    final_price_date_kw: str = "",
    only_use: bool = False,
    with_count: bool = True,
    order_numeric: bool = True,
) -> pd.DataFrame:
    """
    제품코드 검색/목록.
    - Rd04_Del_Flag='E'라도 조회에는 포함
    - only_use=True이면 Rd04_Use_Gu='0'
    """
    T040, C040 = _get_table_and_cols()
    from_join, extra = _build_from_join()

    top = int(top or 2000)
    keyword = (keyword or "").strip()
    with_count = bool(with_count)
    order_numeric = bool(order_numeric)
    physic_cd = (physic_cd or "").strip()
    insu_cd = (insu_cd or "").strip()
    barcode = (barcode or "").strip()
    ven_nm_kw = (ven_nm_kw or "").strip()
    group_name_kw = (group_name_kw or "").strip()
    di_name_kw = (di_name_kw or "").strip()

    physic_gu_name_kw = (physic_gu_name_kw or "").strip()
    add_user_nm_kw = (add_user_nm_kw or "").strip()
    add_date_from = (add_date_from or "").strip()
    add_date_to = (add_date_to or "").strip()
    mod_user_nm_kw = (mod_user_nm_kw or "").strip()
    mod_date_from = (mod_date_from or "").strip()
    mod_date_to = (mod_date_to or "").strip()

    unit_price_kw = (unit_price_kw or "").strip()
    final_price_date_kw = (final_price_date_kw or "").strip()

    where = ["1=1"]
    params: list[Any] = []

    col_physic_cd = _al(C040, "physic_cd", "Rd04_Physic_Cd")
    col_insu_cd = _al(C040, "insu_cd", "Rd04_Insu_Cd")
    col_nm = _al(C040, "physic_nm", "Rd04_Physic_Nm")
    
    col_use = _al(C040, "use_gu", "Rd04_Use_Gu")
    col_add_date = _al(C040, "add_date", "Rd04_Add_Date")
    col_mod_date = _al(C040, "mod_date", "Rd04_Mod_Date")

    col_bar1 = _al(C040, "bar1", "Rd04_Bar_Code1")
    col_bar2 = _al(C040, "bar2", "Rd04_Bar_Code2")
    col_bar3 = _al(C040, "bar3", "Rd04_Bar_Code3")
    col_bar4 = _al(C040, "bar4", "Rd04_Bar_Code4")
    col_bar5 = _al(C040, "bar5", "Rd04_Bar_Code5")

    col_acc_unit = _al(C040, "acc_unit", "Rd04_Acc_Unit")
    col_insu_date = _al(C040, "insu_date", "Rd04_Insu_Date")
    col_insu_price = _al(C040, "insu_price", "Rd04_Insu_Price")
    col_before_insu_date = _al(C040, "before_insu_date", "Rd04_Before_Insu_Date")
    col_before_insu_price = _al(C040, "before_insu_price", "Rd04_Before_Insu_Price")
    col_physic_gu = _al(C040, "physic_gu", "Rd04_Physic_Gu")

    norm_insu_date_expr = f"ISNULL(NULLIF(LTRIM(RTRIM(a.{col_insu_date})), ''), '00000000')"
    norm_before_insu_date_expr = f"ISNULL(NULLIF(LTRIM(RTRIM(a.{col_before_insu_date})), ''), '00000000')"
    insu_unit_price_expr = f"ISNULL(a.{col_insu_price}, 0) * ISNULL(a.{col_acc_unit}, 0)"
    before_insu_unit_price_expr = f"ISNULL(a.{col_before_insu_price}, 0) * ISNULL(a.{col_acc_unit}, 0)"

    calc_apply = f"""
CROSS APPLY (
    SELECT
        {norm_insu_date_expr} AS norm_insu_date,
        {norm_before_insu_date_expr} AS norm_before_insu_date,
        {insu_unit_price_expr} AS insu_unit_price,
        {before_insu_unit_price_expr} AS before_insu_unit_price,
        CASE
            WHEN {norm_insu_date_expr} >= {norm_before_insu_date_expr}
            THEN {norm_insu_date_expr}
            ELSE {norm_before_insu_date_expr}
        END AS final_price_date,
        CASE
            WHEN {norm_insu_date_expr} >= {norm_before_insu_date_expr}
            THEN {insu_unit_price_expr}
            ELSE {before_insu_unit_price_expr}
        END AS unit_price
) calc
""".strip()

    from_join_calc = f"{from_join}\n{calc_apply}"

    if only_use:
        where.append(f"a.{col_use} = ?")
        params.append("0")

    if physic_cd:
        where.append(f"a.{col_physic_cd} = ?")
        params.append(physic_cd)

    if insu_cd:
        if len(insu_cd) >= 10:
            where.append(f"a.{col_insu_cd} = ?")
            params.append(insu_cd)
        else:
            where.append(f"a.{col_insu_cd} LIKE ?")
            params.append(f"%{insu_cd}%")

    if barcode:
        where.append(
            f"(a.{col_bar1} = ? OR a.{col_bar2} = ? OR a.{col_bar3} = ? OR a.{col_bar4} = ? OR a.{col_bar5} = ?)"
        )
        params.extend([barcode, barcode, barcode, barcode, barcode])

    if ven_nm_kw:
        where.append(f"{extra['ven_nm']} LIKE ?")
        params.append(f"%{ven_nm_kw}%")

    if group_name_kw:
        where.append(f"{extra['group_name']} LIKE ?")
        params.append(f"%{group_name_kw}%")

    if di_name_kw:
        where.append(f"{extra['di_name']} LIKE ?")
        params.append(f"%{di_name_kw}%")

    if physic_gu_name_kw:
        where.append(f"{extra['physic_gu_name']} LIKE ?")
        params.append(f"%{physic_gu_name_kw}%")

    if add_user_nm_kw:
        where.append(f"{extra['add_user_nm']} LIKE ?")
        params.append(f"%{add_user_nm_kw}%")

    norm_add_from = _norm_date_from(add_date_from)
    if norm_add_from:
        where.append(f"a.{col_add_date} >= ?")
        params.append(norm_add_from)

    norm_add_to = _norm_date_to(add_date_to)
    if norm_add_to:
        where.append(f"a.{col_add_date} <= ?")
        params.append(norm_add_to)

    if mod_user_nm_kw:
        where.append(f"{extra['mod_user_nm']} LIKE ?")
        params.append(f"%{mod_user_nm_kw}%")

    norm_mod_from = _norm_date_from(mod_date_from)
    if norm_mod_from:
        where.append(f"a.{col_mod_date} >= ?")
        params.append(norm_mod_from)

    norm_mod_to = _norm_date_to(mod_date_to)
    if norm_mod_to:
        where.append(f"a.{col_mod_date} <= ?")
        params.append(norm_mod_to)

    if unit_price_kw:
        price_from, price_to = _parse_num_range(unit_price_kw)
        if price_from is not None:
            where.append("calc.unit_price >= ?")
            params.append(price_from)
        if price_to is not None:
            where.append("calc.unit_price <= ?")
            params.append(price_to)

    if final_price_date_kw:
        parts = re.split(r"\s*(?:~|-)\s*", final_price_date_kw, maxsplit=1)
        raw_from = parts[0] if parts else ""
        raw_to = parts[1] if len(parts) > 1 else raw_from

        norm_final_from = _norm_date_from(raw_from)
        norm_final_to = _norm_date_to(raw_to)

        if norm_final_from:
            where.append("calc.final_price_date >= ?")
            params.append(norm_final_from)
        if norm_final_to:
            where.append("calc.final_price_date <= ?")
            params.append(norm_final_to)

    if keyword:
        where.append(f"(a.{col_nm} LIKE ? OR a.{col_physic_cd} LIKE ? OR a.{col_insu_cd} LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"])

    where_sql = "WHERE " + " AND ".join(where)

    order_expr = _sql_safe_int(extra["order_key"]) if order_numeric else extra["order_key"]

    select_sql = f"""
SELECT TOP {top}
    a.*,
    {extra['ven_nm']} AS ven_nm,
    {extra['group_name']} AS group_name,
    {extra['di_name']} AS di_name,
    {extra['flag_name']} AS flag_name,
    {extra['cons_name']} AS cons_name,
    {extra['physic_gu_name']} AS physic_gu_name,
    {extra['add_user_nm']} AS add_user_nm,
    {extra['mod_user_nm']} AS mod_user_nm,

    a.{col_acc_unit} AS [계산단위],
    calc.norm_insu_date AS [보험수가변경일자],
    a.{col_insu_price} AS [보험가격],
    calc.insu_unit_price AS [보험단가],
    calc.norm_before_insu_date AS [이전보험수가변경일자],
    a.{col_before_insu_price} AS [이전보험가격],
    calc.before_insu_unit_price AS [이전보험단가],
    calc.final_price_date AS [최종단가변경일자],
    calc.unit_price AS [단가],
    a.{col_physic_gu} AS [특수관리제품코드],
    {extra['physic_gu_name']} AS [특수관리제품]
{from_join_calc}
{where_sql}
ORDER BY {order_expr}
""".strip()

    total_count = 0

    if with_count:
        count_sql = f"""
SELECT COUNT(1) AS cnt
{from_join_calc}
{where_sql}
""".strip()

        _log_sql(count_sql, params, tag="[rddbc040] search_goods_full.count")
        df_cnt = read_df(count_sql, _norm_params(params))
        total_count = int(df_cnt.iloc[0]["cnt"]) if df_cnt is not None and not df_cnt.empty else 0

    t0 = time.perf_counter()
    _log_sql(select_sql, params, tag="[rddbc040] search_goods_full.select")
    df = read_df(select_sql, _norm_params(params))
    ms = int((time.perf_counter() - t0) * 1000)

    if not with_count:
        total_count = 0 if df is None else int(len(df))

    try:
        df.attrs["row_count_total"] = int(total_count)
        df.attrs["column_count"] = int(len(df.columns)) if df is not None else 0
        df.attrs["basis"] = "DB"
    except Exception:
        pass

    log.info(
        "[svc.rddbc040] action=search_goods_full rows=%s total=%s %s ms",
        (0 if df is None else len(df)),
        total_count,
        ms,
    )

    return df if df is not None else pd.DataFrame()

# 상세조회: 제품코드(상품) 하나에 대한 상세정보 조회. 검색조건은 제품코드(physic_cd) 단일값으로만 허용.
@_cache_data(ttl=60, show_spinner=False)
def get_goods_detail_full(*, physic_cd: str) -> pd.DataFrame:
    physic_cd = (physic_cd or "").strip()
    if not physic_cd:
        return pd.DataFrame()

    T040, C040 = _get_table_and_cols()
    from_join, extra = _build_from_join()

    col_physic_cd = _al(C040, "physic_cd", "Rd04_Physic_Cd")
    col_acc_unit = _al(C040, "acc_unit", "Rd04_Acc_Unit")
    col_insu_date = _al(C040, "insu_date", "Rd04_Insu_Date")
    col_insu_price = _al(C040, "insu_price", "Rd04_Insu_Price")
    col_before_insu_date = _al(C040, "before_insu_date", "Rd04_Before_Insu_Date")
    col_before_insu_price = _al(C040, "before_insu_price", "Rd04_Before_Insu_Price")
    col_physic_gu = _al(C040, "physic_gu", "Rd04_Physic_Gu")

    norm_insu_date_expr = f"ISNULL(NULLIF(LTRIM(RTRIM(a.{col_insu_date})), ''), '00000000')"
    norm_before_insu_date_expr = f"ISNULL(NULLIF(LTRIM(RTRIM(a.{col_before_insu_date})), ''), '00000000')"

    expr_final_price_date = f"""
CASE
    WHEN {norm_insu_date_expr} >= {norm_before_insu_date_expr}
    THEN {norm_insu_date_expr}
    ELSE {norm_before_insu_date_expr}
END
""".strip()

    expr_unit_price = f"""
CASE
    WHEN {norm_insu_date_expr} >= {norm_before_insu_date_expr}
    THEN ISNULL(a.{col_insu_price}, 0) * ISNULL(a.{col_acc_unit}, 0)
    ELSE ISNULL(a.{col_before_insu_price}, 0) * ISNULL(a.{col_acc_unit}, 0)
END
""".strip()

    sql = f"""
SELECT TOP 1
    a.*,
    {extra['ven_nm']} AS ven_nm,
    {extra['group_name']} AS group_name,
    {extra['di_name']} AS di_name,
    {extra['flag_name']} AS flag_name,
    {extra['cons_name']} AS cons_name,
    {extra['physic_gu_name']} AS physic_gu_name,
    {extra['add_user_nm']} AS add_user_nm,
    {extra['mod_user_nm']} AS mod_user_nm,

    a.{col_acc_unit} AS [계산단위],
    a.{col_insu_date} AS [보험수가변경일자],
    a.{col_insu_price} AS [보험가격],
    ISNULL(a.{col_insu_price}, 0) * ISNULL(a.{col_acc_unit}, 0) AS [보험단가],
    a.{col_before_insu_date} AS [이전보험수가변경일자],
    a.{col_before_insu_price} AS [이전보험가격],
    ISNULL(a.{col_before_insu_price}, 0) * ISNULL(a.{col_acc_unit}, 0) AS [이전보험단가],
    {expr_final_price_date} AS [최종단가변경일자],
    {expr_unit_price} AS [단가],
    a.{col_physic_gu} AS [특수관리제품코드],
    {extra['physic_gu_name']} AS [특수관리제품]
{from_join}
WHERE a.{col_physic_cd} = ?
""".strip()

    t0 = time.perf_counter()
    _log_sql(sql, (physic_cd,), tag="[rddbc040] get_goods_detail_full")
    df = read_df(sql, (physic_cd,))
    ms = int((time.perf_counter() - t0) * 1000)

    log.info("[svc.rddbc040] action=get_goods_detail_full physic_cd=%s rows=%s %s ms",
             physic_cd, (0 if df is None else len(df)), ms)
    return df if df is not None else pd.DataFrame()
