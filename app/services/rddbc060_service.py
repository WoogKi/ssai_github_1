# app/services/rddbc060_service.py
# 사용자
# 2026/05/17

from __future__ import annotations

import os
from typing import Optional
from datetime import datetime, timedelta

import logging
import time

from app.db.mssql_client import read_df, log_sql
from app.db.schema_map import SCHEMA as S
from app.db.schema_utils import al as _al_shared

log = logging.getLogger("ssai.sims.rddbc060")

try:
    import streamlit as st
    _cache_data = st.cache_data
except Exception:
    def _cache_data(*args, **kwargs):
        def deco(fn):
            return fn
        return deco


# -----------------------------------------------------------------------------
# Schema / Alias
# -----------------------------------------------------------------------------
T = S["tables"]["rddbc060"]
T010 = S["tables"]["rddbc010"]

C = S["cols"]["rddbc060"]
C010 = S["cols"]["rddbc010"]

_ALIAS_060 = (S.get("aliases") or S.get("alias") or {}).get("rddbc060", {})


def _al(d, k, default):
    return _al_shared(d, k, default)


def _col(cols: dict, *keys: str, default: str) -> str:
    for k in keys:
        v = cols.get(k)
        if v:
            return v
    return default


# Rddbc060
COL_USER_CD = _col(C, "user_cd", default="Rd06_User_Cd")
COL_USER_ID = _col(C, "user_id", default="Rd06_User_ID")
COL_SABUN = _col(C, "sabun", default="Rd06_Sabun")
COL_USER_NM = _col(C, "user_nm", default="Rd06_User_Nm")
COL_PASSWORD = _col(C, "password", default="Rd06_Password")

COL_DEPT_G = _col(C, "department_gcode", default="Rd06_Department_Gcode")
COL_DEPT = _col(C, "department", default="Rd06_Department")

COL_DUTY_G = _col(C, "duty_gcode", default="Rd06_Duty_Gcode")
COL_DUTY = _col(C, "duty", default="Rd06_Duty")

COL_DISTRICT_G = _col(C, "district_gcode", default="Rd06_District_Gcode")
COL_DISTRICT = _col(C, "district", default="Rd06_District")

COL_SALES_TEAM = _col(C, "sales_team", default="Rd06_Sales_Team")

COL_STOCK_G = _col(C, "stock_cd_gcode", default="Rd06_Stock_Cd_Gcode")
COL_STOCK = _col(C, "stock_cd", default="Rd06_Stock_Cd")

COL_DELFLAG = _col(C, "delflag", "del_flag", default="Rd06_Del_Flag")
COL_ADDDATE = _col(C, "add_date", default="Rd06_Add_Date")
COL_ADDCD = _col(C, "add_cd", default="Rd06_Add_Cd")
COL_MODDATE = _col(C, "mod_date", default="Rd06_Mod_Date")
COL_MODCD = _col(C, "mod_cd", default="Rd06_Mod_Cd")

COL_PAY_FLAG = _col(C, "pay_flag", default="Rd06_Pay_Flag")
COL_WEB_FLAG = _col(C, "web_flag", default="Rd06_Web_Flag")
COL_CREDIT_LIMIT = _col(C, "credit_limit", default="Rd06_Credit_Limit")
COL_UNIT_PERCENT = _col(C, "unit_percent", default="Rd06_Unit_Percent")
COL_UNITCOST_CHK = _col(C, "unitcost_chk", default="Rd06_UnitCost_Chk")

COL_JUMIN = _col(C, "jumin", default="Rd06_Jumin")
COL_EMAIL = _col(C, "email", default="Rd06_Email")
COL_CELL = _col(C, "cellular_phone", default="Rd06_Cellular_Phone")
COL_PHONE = _col(C, "phone", default="Rd06_Phone")
COL_OFFICE_PHONE = _col(C, "office_phone", default="Rd06_Office_Phone")
COL_REMARK = _col(C, "remark", default="Rd06_Remark")

COL_SMS_ID = _col(C, "sms_id", default="Rd06_SMS_ID")
COL_SMS_PW = _col(C, "sms_pw", default="Rd06_SMS_PW")
COL_POL_ID = _col(C, "pol_id", default="Rd06_POL_ID")
COL_POL_PW = _col(C, "pol_pw", default="Rd06_POL_PW")

COL_WORK_GU = _col(C, "work_gu", default="Rd06_Work_Gu")
COL_WORK_PWD = _col(C, "work_pwd", default="Rd06_Work_PWD")
COL_PG_ADMIN = _col(C, "pg_admin", default="Rd06_PG_Admin")
COL_ALLVIEW = _col(C, "allview", default="Rd06_AllView")
COL_LIMIT_AMT = _col(C, "limit_amt", default="RD06_Limit_Amt")
COL_USE_VEN = _col(C, "use_ven", default="Rd06_Use_Ven")

# Rddbc010
COL010_GCODE = _col(C010, "gcode", default="Rd01_Gcode")
COL010_TCODE = _col(C010, "tcode", default="Rd01_Tcode")
COL010_HNM = _col(C010, "hnm", default="Rd01_Hnm")


# -----------------------------------------------------------------------------
# Utils
# -----------------------------------------------------------------------------
def _trim(expr: str) -> str:
    return f"LTRIM(RTRIM({expr}))"


def _clean_text(value: Optional[str]) -> str:
    return str(value or "").strip()


def _like(value: Optional[str]) -> Optional[str]:
    text = _clean_text(value)
    if not text:
        return None
    return f"%{text}%"

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

def _service_master_max_rows(default: int = 30000) -> int:
    """
    마스터 조회 공통 상한.
    새 env를 만들지 않고 기존 SIMS_PANEL_DISPLAY_MAX_ROWS /
    SIMS_CHAT_DISPLAY_MAX_ROWS 값을 사용한다.
    """
    try:
        raw = (
            os.getenv("SIMS_PANEL_DISPLAY_MAX_ROWS")
            or os.getenv("SIMS_CHAT_DISPLAY_MAX_ROWS")
            or str(default)
        )
        v = int(str(raw or default).strip())
    except Exception:
        v = int(default)

    if v < 1:
        v = int(default)

    return v


def _normalize_top(value: int, default: int = 200, max_value: Optional[int] = None) -> int:
    try:
        v = int(value)
    except Exception:
        v = default

    if v < 1:
        v = default

    if max_value is None:
        max_value = _service_master_max_rows()

    return min(v, int(max_value))


def _active_clause(alias: str = "U") -> str:
    # Rd06_Del_Flag='E' 는 조회에는 항상 포함해야 함
    return (
        f"ISNULL(NULLIF({_trim(f'{alias}.{COL_DELFLAG}')}, ''), '0') "
        f"NOT IN ('1', 'Y')"
    )


# -----------------------------------------------------------------------------
# Security: 사용자 마스터 조회 결과 민감 컬럼 제거
# -----------------------------------------------------------------------------
_SENSITIVE_COLUMN_EXACT = {
    "비밀번호",
    "주민번호",
    "Rd06_Password",
    "Rd06_Password_ENCrypt",
    "Rd06_Jumin",
    "Rd06_SMS_PW",
    "Rd06_POL_PW",
    "Rd06_Work_PWD",
}

_SENSITIVE_COLUMN_TOKENS = (
    "password",
    "passwd",
    "pwd",
    "비밀번호",
    "jumin",
    "주민",
    "sms_pw",
    "pol_pw",
    "work_pwd",
)


def _is_sensitive_user_column(col: object) -> bool:
    s = str(col or "").strip()
    if not s:
        return False
    if s in _SENSITIVE_COLUMN_EXACT:
        return True

    low = s.lower().replace(" ", "").replace("-", "_")
    return any(token in low for token in _SENSITIVE_COLUMN_TOKENS)


def drop_sensitive_user_columns(df):
    """
    사용자 목록 결과에서 화면/채팅/다운로드/LLM 컨텍스트에 노출되면 안 되는 컬럼을 제거한다.

    인증용 SIMS 비밀번호 조회는 ssai_auth_service의 별도 SQL에서 처리한다.
    이 서비스는 조회/표시 전용이므로 민감 컬럼을 반환하지 않는다.
    """
    if df is None:
        return df

    try:
        drop_cols = [c for c in df.columns if _is_sensitive_user_column(c)]
        if drop_cols:
            return df.drop(columns=drop_cols, errors="ignore")
    except Exception:
        pass

    return df


def _run_df(action: str, sql: str, params: tuple):
    t0 = time.perf_counter()
    try:
        df = read_df(sql, params)
        df = drop_sensitive_user_columns(df)
        ms = int((time.perf_counter() - t0) * 1000)
        try:
            log.info(
                "[svc.rddbc060] action=%s rows=%s elapsed_ms=%s params=%s",
                action, len(df), ms, params
            )
        except Exception:
            pass
        return df
    except Exception:
        log_sql(f"rddbc060.{action}.ERROR", sql, params)
        raise


def _code_join(alias_name: str, g_expr: str, t_expr: str) -> str:
    return f"""
    LEFT JOIN {T010} AS {alias_name} WITH (NOLOCK)
           ON {_trim(f'{alias_name}.{COL010_GCODE}')} = {_trim(g_expr)}
          AND {_trim(f'{alias_name}.{COL010_TCODE}')} = {_trim(t_expr)}
    """


def _base_select(top: int) -> str:
    return f"""
    SELECT TOP {top}
           U.{COL_USER_CD}        AS Rd06_User_Cd,
           U.{COL_USER_ID}        AS Rd06_User_ID,
           U.{COL_SABUN}          AS Rd06_Sabun,
           U.{COL_USER_NM}        AS Rd06_User_Nm,

           U.{COL_DEPT_G}         AS Rd06_Department_Gcode,
           U.{COL_DEPT}           AS Rd06_Department,
           Dept.{COL010_HNM}      AS [부서명],

           U.{COL_DUTY_G}         AS Rd06_Duty_Gcode,
           U.{COL_DUTY}           AS Rd06_Duty,
           Duty.{COL010_HNM}      AS [직책],

           U.{COL_DISTRICT_G}     AS Rd06_District_Gcode,
           U.{COL_DISTRICT}       AS Rd06_District,
           District.{COL010_HNM}  AS [영업지역],

           U.{COL_SALES_TEAM}     AS Rd06_Sales_Team,

           U.{COL_STOCK_G}        AS Rd06_Stock_Cd_Gcode,
           U.{COL_STOCK}          AS Rd06_Stock_Cd,
           Stock.{COL010_HNM}     AS [재고위치],

           U.{COL_DELFLAG}        AS Rd06_Del_Flag,
           U.{COL_ADDDATE}        AS Rd06_Add_Date,
           U.{COL_ADDCD}          AS Rd06_Add_Cd,
           ISNULL(NULLIF({_trim(f'AddU.{COL_USER_NM}')}, ''), {_trim(f'U.{COL_ADDCD}')}) AS add_user_nm,

           U.{COL_MODDATE}        AS Rd06_Mod_Date,
           U.{COL_MODCD}          AS Rd06_Mod_Cd,
           ISNULL(NULLIF({_trim(f'ModU.{COL_USER_NM}')}, ''), {_trim(f'U.{COL_MODCD}')}) AS mod_user_nm,

           U.{COL_PAY_FLAG}       AS Rd06_Pay_Flag,
           U.{COL_WEB_FLAG}       AS Rd06_Web_Flag,
           U.{COL_CREDIT_LIMIT}   AS Rd06_Credit_Limit,
           U.{COL_UNIT_PERCENT}   AS Rd06_Unit_Percent,
           U.{COL_UNITCOST_CHK}   AS Rd06_UnitCost_Chk,
           U.{COL_EMAIL}          AS Rd06_Email,
           U.{COL_CELL}           AS Rd06_Cellular_Phone,
           U.{COL_PHONE}          AS Rd06_Phone,
           U.{COL_OFFICE_PHONE}   AS Rd06_Office_Phone,
           U.{COL_REMARK}         AS Rd06_Remark,

           U.{COL_SMS_ID}         AS Rd06_SMS_ID,
           U.{COL_POL_ID}         AS Rd06_POL_ID,

           U.{COL_WORK_GU}        AS Rd06_Work_Gu,
           U.{COL_PG_ADMIN}       AS Rd06_PG_Admin,
           U.{COL_ALLVIEW}        AS Rd06_AllView,
           U.{COL_LIMIT_AMT}      AS RD06_Limit_Amt,
           U.{COL_USE_VEN}        AS Rd06_Use_Ven
    """


def _base_from() -> str:
    return f"""
    FROM {T} AS U WITH (NOLOCK)

    LEFT JOIN {T} AS AddU WITH (NOLOCK)
           ON {_trim(f'AddU.{COL_USER_CD}')} = {_trim(f'U.{COL_ADDCD}')}

    LEFT JOIN {T} AS ModU WITH (NOLOCK)
           ON {_trim(f'ModU.{COL_USER_CD}')} = {_trim(f'U.{COL_MODCD}')}

    {_code_join("Dept", f"U.{COL_DEPT_G}", f"U.{COL_DEPT}")}
    {_code_join("Duty", f"U.{COL_DUTY_G}", f"U.{COL_DUTY}")}
    {_code_join("District", f"U.{COL_DISTRICT_G}", f"U.{COL_DISTRICT}")}
    {_code_join("Stock", f"U.{COL_STOCK_G}", f"U.{COL_STOCK}")}
    """


# -----------------------------------------------------------------------------
# Main queries
# -----------------------------------------------------------------------------
@_cache_data(ttl=60, show_spinner=False)
def search_rows(
    *,
    user_cd: str = "",
    user_id: str = "",
    sabun: str = "",
    user_nm_kw: str = "",
    department: str = "",
    duty: str = "",
    district: str = "",
    stock_cd: str = "",
    add_user_nm_kw: str = "",
    add_date_from: str = "",
    add_date_to: str = "",
    mod_user_nm_kw: str = "",
    mod_date_from: str = "",
    mod_date_to: str = "",
    keyword: str = "",        
    only_active: bool = True,
    top: int = 200,
):
    top = _normalize_top(top, default=200)

    where = []
    params = []

    if only_active:
        where.append(_active_clause("U"))

    user_cd = _clean_text(user_cd)
    if user_cd:
        where.append(f"{_trim(f'U.{COL_USER_CD}')} = ?")
        params.append(user_cd)

    user_id = _clean_text(user_id)
    if user_id:
        where.append(f"{_trim(f'U.{COL_USER_ID}')} = ?")
        params.append(user_id)

    sabun = _clean_text(sabun)
    if sabun:
        where.append(f"{_trim(f'U.{COL_SABUN}')} = ?")
        params.append(sabun)

    kw_user_nm = _like(user_nm_kw)
    if kw_user_nm:
        where.append(f"{_trim(f'U.{COL_USER_NM}')} LIKE ?")
        params.append(kw_user_nm)

    department = _clean_text(department)
    if department:
        where.append(f"{_trim(f'U.{COL_DEPT}')} = ?")
        params.append(department)

    duty = _clean_text(duty)
    if duty:
        where.append(f"{_trim(f'U.{COL_DUTY}')} = ?")
        params.append(duty)

    district = _clean_text(district)
    if district:
        where.append(f"{_trim(f'U.{COL_DISTRICT}')} = ?")
        params.append(district)

    stock_cd = _clean_text(stock_cd)
    if stock_cd:
        where.append(f"{_trim(f'U.{COL_STOCK}')} = ?")
        params.append(stock_cd)

    kw_add_user = _like(add_user_nm_kw)
    if kw_add_user:
        where.append(
            f"ISNULL(NULLIF({_trim(f'AddU.{COL_USER_NM}')}, ''), {_trim(f'U.{COL_ADDCD}')}) LIKE ?"
        )
        params.append(kw_add_user)

    norm_add_from = _norm_date_from(add_date_from)
    if norm_add_from:
        where.append(f"{_trim(f'U.{COL_ADDDATE}')} >= ?")
        params.append(norm_add_from)

    norm_add_to = _norm_date_to(add_date_to)
    if norm_add_to:
        where.append(f"{_trim(f'U.{COL_ADDDATE}')} <= ?")
        params.append(norm_add_to)

    kw_mod_user = _like(mod_user_nm_kw)
    if kw_mod_user:
        where.append(
            f"ISNULL(NULLIF({_trim(f'ModU.{COL_USER_NM}')}, ''), {_trim(f'U.{COL_MODCD}')}) LIKE ?"
        )
        params.append(kw_mod_user)

    norm_mod_from = _norm_date_from(mod_date_from)
    if norm_mod_from:
        where.append(f"{_trim(f'U.{COL_MODDATE}')} >= ?")
        params.append(norm_mod_from)

    norm_mod_to = _norm_date_to(mod_date_to)
    if norm_mod_to:
        where.append(f"{_trim(f'U.{COL_MODDATE}')} <= ?")
        params.append(norm_mod_to)

    kw = _like(keyword)


    kw = _like(keyword)
    if kw:
        where.append(
            "("
            + " OR ".join(
                [
                    f"{_trim(f'U.{COL_USER_CD}')} LIKE ?",
                    f"{_trim(f'U.{COL_USER_ID}')} LIKE ?",
                    f"{_trim(f'U.{COL_SABUN}')} LIKE ?",
                    f"{_trim(f'U.{COL_USER_NM}')} LIKE ?",
                    f"{_trim('Dept.' + COL010_HNM)} LIKE ?",
                    f"{_trim('Duty.' + COL010_HNM)} LIKE ?",
                    f"{_trim('District.' + COL010_HNM)} LIKE ?",
                    f"{_trim('Stock.' + COL010_HNM)} LIKE ?",
                    f"{_trim(f'AddU.{COL_USER_NM}')} LIKE ?",
                    f"{_trim(f'ModU.{COL_USER_NM}')} LIKE ?",
                ]
            )
            + ")"
        )
        params.extend([kw] * 10)

    if not where:
        where = ["1=1"]

    sql = f"""
    {_base_select(top)}
    {_base_from()}
    WHERE {" AND ".join(where)}
    ORDER BY U.{COL_USER_CD}
    """
    return _run_df("search_rows", sql, tuple(params))


# -----------------------------------------------------------------------------
# Compatibility wrappers
# -----------------------------------------------------------------------------
@_cache_data(ttl=60, show_spinner=False)
def list_users_full(top: int = 1000, only_active: bool = True):
    return search_rows(
        only_active=only_active,
        top=top,
    )


@_cache_data(ttl=60, show_spinner=False)
def search_users_full(
    *,
    top: int = 200,
    only_active: bool = True,
    user_cd: str = "",
    user_id: str = "",
    sabun: str = "",
    user_nm: str = "",
    department: str = "",
    duty: str = "",
    district: str = "",
    stock_cd: str = "",
    add_user_nm: str = "",
    add_date_from: str = "",
    add_date_to: str = "",
    mod_user_nm: str = "",
    mod_date_from: str = "",
    mod_date_to: str = "",
    keyword: str = "",    
    **kwargs,
):
    return search_rows(
        user_cd=user_cd or kwargs.get("rd06_user_cd", ""),
        user_id=user_id or kwargs.get("rd06_user_id", ""),
        sabun=sabun or kwargs.get("rd06_sabun", ""),
        user_nm_kw=user_nm or kwargs.get("user_nm_kw", ""),
        department=department or kwargs.get("dept_cd", ""),
        duty=duty or kwargs.get("duty_cd", ""),
        district=district or kwargs.get("district_cd", ""),
        stock_cd=stock_cd or kwargs.get("stock_cd", ""),
        add_user_nm_kw=add_user_nm or kwargs.get("add_user_nm_kw", "") or kwargs.get("add_user_nm", ""),
        add_date_from=add_date_from or kwargs.get("add_date_from", ""),
        add_date_to=add_date_to or kwargs.get("add_date_to", ""),
        mod_user_nm_kw=mod_user_nm or kwargs.get("mod_user_nm_kw", "") or kwargs.get("modifier_nm", ""),
        mod_date_from=mod_date_from or kwargs.get("mod_date_from", ""),
        mod_date_to=mod_date_to or kwargs.get("mod_date_to", ""),
        keyword=keyword or kwargs.get("q", ""),
        only_active=only_active,
        top=top,
    )

def get_user_full(user_cd: str, *, only_active: bool = False):
    return search_rows(
        user_cd=user_cd,
        only_active=only_active,
        top=1,
    )


def search_name(keyword: str = "", top: int = 200, only_active: bool = True):
    return search_rows(
        keyword=keyword,
        only_active=only_active,
        top=top,
    )


def list_by_department(department: str, top: int = 1000, only_active: bool = True):
    return search_rows(
        department=department,
        only_active=only_active,
        top=top,
    )


# -----------------------------------------------------------------------------
# 업무코드종류 옵션 helper
# -----------------------------------------------------------------------------
def _list_codes_by_group(gcode: str, top: int = 500):
    sql = f"""
    SELECT TOP {int(top)}
           Cd.{COL010_GCODE} AS Rd01_Gcode,
           Cd.{COL010_TCODE} AS Rd01_Tcode,
           Cd.{COL010_HNM}   AS Rd01_Hnm
    FROM {T010} AS Cd WITH (NOLOCK)
    WHERE {_trim(f'Cd.{COL010_GCODE}')} = ?
      AND ISNULL(NULLIF({_trim('Cd.Rd01_Del_Flag')}, ''), '0') NOT IN ('1', 'Y')
    ORDER BY Cd.{COL010_TCODE}
    """
    return _run_df("list_codes_by_group", sql, (_clean_text(gcode),))


@_cache_data(ttl=60, show_spinner=False)
def list_department_codes(top: int = 500):
    return _list_codes_by_group("0005", top=top)


@_cache_data(ttl=60, show_spinner=False)
def list_duty_codes(top: int = 500):
    return _list_codes_by_group("0003", top=top)


@_cache_data(ttl=60, show_spinner=False)
def list_district_codes(top: int = 500):
    return _list_codes_by_group("0008", top=top)


@_cache_data(ttl=60, show_spinner=False)
def list_stock_codes(top: int = 500):
    return _list_codes_by_group("0018", top=top)