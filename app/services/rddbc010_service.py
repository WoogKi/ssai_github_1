# app/services/rddbc010_service.py
# 사용자
# 2026/05/18

from __future__ import annotations

import os
from typing import Optional
import logging
import time

from app.db.mssql_client import read_df, log_sql
from app.db.schema_map import SCHEMA as S
from app.db.schema_utils import al as _al_shared

log = logging.getLogger("ssai.sims.rddbc010")

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
T = S["tables"]["rddbc010"]
T060 = S["tables"]["rddbc060"]

C = S["cols"]["rddbc010"]
C060 = S["cols"]["rddbc060"]

_ALIAS_010 = (S.get("aliases") or S.get("alias") or {}).get("rddbc010", {})
_ALIAS_060 = (S.get("aliases") or S.get("alias") or {}).get("rddbc060", {})

GCODE_KIND = "9999"


def _al(d, k, default):
    return _al_shared(d, k, default)


def _col(cols: dict, *keys: str, default: str) -> str:
    for k in keys:
        v = cols.get(k)
        if v:
            return v
    return default


# alias: codes.py 호환을 위해 영문 alias 유지
AL_KINDNM = _al(_ALIAS_010, "kind_name", "kind_name")
AL_ADDUNM = _al(_ALIAS_060, "add_user_nm", "add_user_nm")
AL_MODUNM = _al(_ALIAS_060, "mod_user_nm", "mod_user_nm")

COL_GCODE = _col(C, "gcode", default="Rd01_Gcode")
COL_TCODE = _col(C, "tcode", default="Rd01_Tcode")
COL_HNM = _col(C, "hnm", default="Rd01_Hnm")
COL_ENM = _col(C, "enm", default="Rd01_Enm")
COL_SNM = _col(C, "snm", default="Rd01_Snm")
COL_OTHER1 = _col(C, "other1", default="Rd01_Other1")
COL_OTHER2 = _col(C, "other2", default="Rd01_Other2")
COL_OTHER3 = _col(C, "other3", default="Rd01_Other3")
COL_COLNUM = _col(C, "col_num", default="Rd01_Col_Num")
COL_MODFLAG = _col(C, "mod_flag", default="Rd01_Mod_Flag")
COL_DELFLAG = _col(C, "delflag", "del_flag", default="Rd01_Del_Flag")
COL_ADDDATE = _col(C, "add_date", default="Rd01_Add_Date")
COL_ADDCD = _col(C, "add_cd", default="Rd01_Add_Cd")
COL_MODDATE = _col(C, "mod_date", default="Rd01_Mod_Date")
COL_MODCD = _col(C, "mod_cd", default="Rd01_Mod_Cd")
COL_DEBIT1 = _col(C, "debit_acc_cd1", default="Rd01_Debit_Acc_Cd1")
COL_DEBIT2 = _col(C, "debit_acc_cd2", default="Rd01_Debit_Acc_Cd2")
COL_CREDIT1 = _col(C, "credit_acc_cd1", default="Rd01_Credit_Acc_Cd1")
COL_CREDIT2 = _col(C, "credit_acc_cd2", default="Rd01_Credit_Acc_Cd2")

COL060_USERCD = _col(C060, "user_cd", default="Rd06_User_Cd")
COL060_USERNM = _col(C060, "user_nm", default="Rd06_User_Nm")


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
    import datetime as _dt

    ym = _digits_only(yyyymm)
    if len(ym) != 6:
        return ""

    first = _dt.datetime.strptime(ym + "01", "%Y%m%d")
    if first.month == 12:
        next_first = first.replace(year=first.year + 1, month=1, day=1)
    else:
        next_first = first.replace(month=first.month + 1, day=1)

    return (next_first - _dt.timedelta(days=1)).strftime("%Y%m%d")


def _audit_date_from(value: Optional[str]) -> str:
    digits = _digits_only(value)
    if len(digits) == 8:
        return digits
    if len(digits) == 6:
        return digits + "01"
    if len(digits) == 4:
        return digits + "0101"
    return ""


def _audit_date_to(value: Optional[str]) -> str:
    digits = _digits_only(value)
    if len(digits) == 8:
        return digits
    if len(digits) == 6:
        return _last_day_of_month(digits)
    if len(digits) == 4:
        return digits + "1231"
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


def _active_clause(alias: str = "Cd") -> str:
    return (
        f"ISNULL(NULLIF({_trim(f'{alias}.{COL_DELFLAG}')}, ''), '0') "
        f"NOT IN ('1', 'Y')"
    )


def _run_df(action: str, sql: str, params: tuple):
    t0 = time.perf_counter()
    try:
        df = read_df(sql, params)
        ms = int((time.perf_counter() - t0) * 1000)
        try:
            log.info(
                "[svc.rddbc010] action=%s rows=%s elapsed_ms=%s params=%s",
                action, len(df), ms, params
            )
        except Exception:
            pass
        return df
    except Exception:
        log_sql(f"rddbc010.{action}.ERROR", sql, params)
        raise


def _base_select(top: int) -> str:
    return f"""
    SELECT TOP {top}
           Cd.{COL_GCODE}                                        AS Rd01_Gcode,
           Kind.{COL_HNM}                                        AS kind_name,
           Cd.{COL_TCODE}                                        AS Rd01_Tcode,
           Cd.{COL_HNM}                                          AS Rd01_Hnm,
           Cd.{COL_ENM}                                          AS Rd01_Enm,
           Cd.{COL_SNM}                                          AS Rd01_Snm,
           Cd.{COL_OTHER1}                                       AS Rd01_Other1,
           Cd.{COL_OTHER2}                                       AS Rd01_Other2,
           Cd.{COL_OTHER3}                                       AS Rd01_Other3,
           Cd.{COL_COLNUM}                                       AS Rd01_Col_Num,
           Cd.{COL_MODFLAG}                                      AS Rd01_Mod_Flag,
           Cd.{COL_DELFLAG}                                      AS Rd01_Del_Flag,
           Cd.{COL_ADDDATE}                                      AS Rd01_Add_Date,
           NULLIF({_trim(f'AddU.{COL060_USERNM}')}, '')          AS add_user_nm,
           Cd.{COL_ADDCD}                                        AS Rd01_Add_Cd,
           Cd.{COL_MODDATE}                                      AS Rd01_Mod_Date,
           NULLIF({_trim(f'ModU.{COL060_USERNM}')}, '')          AS mod_user_nm,
           Cd.{COL_MODCD}                                        AS Rd01_Mod_Cd,
           Cd.{COL_DEBIT1}                                       AS Rd01_Debit_Acc_Cd1,
           Cd.{COL_DEBIT2}                                       AS Rd01_Debit_Acc_Cd2,
           Cd.{COL_CREDIT1}                                      AS Rd01_Credit_Acc_Cd1,
           Cd.{COL_CREDIT2}                                      AS Rd01_Credit_Acc_Cd2
    """


def _base_from() -> str:
    return f"""
    FROM {T} AS Cd WITH (NOLOCK)
    LEFT JOIN {T} AS Kind WITH (NOLOCK)
           ON {_trim(f'Kind.{COL_GCODE}')} = '{GCODE_KIND}'
          AND {_trim(f'Kind.{COL_TCODE}')} = {_trim(f'Cd.{COL_GCODE}')}

    LEFT JOIN {T060} AS AddU WITH (NOLOCK)
           ON {_trim(f'AddU.{COL060_USERCD}')} = {_trim(f'Cd.{COL_ADDCD}')}

    LEFT JOIN {T060} AS ModU WITH (NOLOCK)
           ON {_trim(f'ModU.{COL060_USERCD}')} = {_trim(f'Cd.{COL_MODCD}')}
    """

# -----------------------------------------------------------------------------
# Main queries
# -----------------------------------------------------------------------------
@_cache_data(ttl=60, show_spinner=False)
def list_group_kinds(top: int = 500, only_active: bool = True):
    """
    코드종류 사전 조회
    Rd01_Gcode='9999' 행의 Rd01_Tcode = 실제 그룹코드
    """
    top = _normalize_top(top, default=500)

    where = [f"{_trim(f'Cd.{COL_GCODE}')} = ?"]
    params = [GCODE_KIND]

    if only_active:
        where.append(_active_clause("Cd"))

    sql = f"""
    {_base_select(top)}
    {_base_from()}
    WHERE {" AND ".join(where)}
    ORDER BY Cd.{COL_TCODE}
    """
    return _run_df("list_group_kinds", sql, tuple(params))


@_cache_data(ttl=60, show_spinner=False)
def search_rows(
    *,
    gcode: str = "",
    tcode: str = "",
    keyword: str = "",
    kind_name_kw: str = "",
    hnm_kw: str = "",
    enm_kw: str = "",
    snm_kw: str = "",
    other_kw: str = "",
    add_user_nm_kw: str = "",
    add_date_from: str = "",
    add_date_to: str = "",
    mod_user_nm_kw: str = "",
    mod_date_from: str = "",
    mod_date_to: str = "",
    only_active: bool = True,
    top: int = 200,
):
    """
    업무코드 마스터 직접 조회

    - Cd.Rd01_Gcode = 코드종류(그룹코드)
    - Cd.Rd01_Tcode = 상세코드
    - Kind(9999).Rd01_Hnm = 코드종류명
    - Cd.Rd01_Hnm = 실제 한글명
    """
    top = _normalize_top(top, default=200)

    where = []
    params = []

    if only_active:
        where.append(_active_clause("Cd"))

    gcode = _clean_text(gcode)
    tcode = _clean_text(tcode)

    if gcode:
        where.append(f"{_trim(f'Cd.{COL_GCODE}')} = ?")
        params.append(gcode)

    if tcode:
        where.append(f"{_trim(f'Cd.{COL_TCODE}')} = ?")
        params.append(tcode)

    kw_kind = _like(kind_name_kw)
    if kw_kind:
        where.append(f"{_trim(f'Kind.{COL_HNM}')} LIKE ?")
        params.append(kw_kind)

    kw = _like(keyword)
    if kw:
        where.append(
            "("
            + " OR ".join(
                [
                    f"{_trim(f'Kind.{COL_HNM}')} LIKE ?",
                    f"{_trim(f'Cd.{COL_HNM}')} LIKE ?",
                    f"{_trim(f'Cd.{COL_SNM}')} LIKE ?",
                    f"{_trim(f'Cd.{COL_ENM}')} LIKE ?",
                    f"{_trim(f'Cd.{COL_OTHER1}')} LIKE ?",
                    f"{_trim(f'Cd.{COL_OTHER2}')} LIKE ?",
                    f"{_trim(f'Cd.{COL_OTHER3}')} LIKE ?",
                ]
            )
            + ")"
        )
        params.extend([kw] * 7)

    kw_hnm = _like(hnm_kw)
    if kw_hnm:
        where.append(f"{_trim(f'Cd.{COL_HNM}')} LIKE ?")
        params.append(kw_hnm)

    kw_enm = _like(enm_kw)
    if kw_enm:
        where.append(f"{_trim(f'Cd.{COL_ENM}')} LIKE ?")
        params.append(kw_enm)

    kw_snm = _like(snm_kw)
    if kw_snm:
        where.append(f"{_trim(f'Cd.{COL_SNM}')} LIKE ?")
        params.append(kw_snm)

    kw_other = _like(other_kw)
    if kw_other:
        where.append(
            "("
            + " OR ".join(
                [
                    f"{_trim(f'Cd.{COL_OTHER1}')} LIKE ?",
                    f"{_trim(f'Cd.{COL_OTHER2}')} LIKE ?",
                    f"{_trim(f'Cd.{COL_OTHER3}')} LIKE ?",
                ]
            )
            + ")"
        )
        params.extend([kw_other] * 3)

    kw_add_user = _like(add_user_nm_kw)
    if kw_add_user:
        where.append(
            f"ISNULL(NULLIF({_trim(f'AddU.{COL060_USERNM}')}, ''), {_trim(f'Cd.{COL_ADDCD}')}) LIKE ?"
        )
        params.append(kw_add_user)

    norm_add_from = _audit_date_from(add_date_from)
    if norm_add_from:
        where.append(f"{_trim(f'Cd.{COL_ADDDATE}')} >= ?")
        params.append(norm_add_from)

    norm_add_to = _audit_date_to(add_date_to)
    if norm_add_to:
        where.append(f"{_trim(f'Cd.{COL_ADDDATE}')} <= ?")
        params.append(norm_add_to)

    kw_mod_user = _like(mod_user_nm_kw)
    if kw_mod_user:
        where.append(
            f"ISNULL(NULLIF({_trim(f'ModU.{COL060_USERNM}')}, ''), {_trim(f'Cd.{COL_MODCD}')}) LIKE ?"
        )
        params.append(kw_mod_user)

    norm_mod_from = _audit_date_from(mod_date_from)
    if norm_mod_from:
        where.append(f"{_trim(f'Cd.{COL_MODDATE}')} >= ?")
        params.append(norm_mod_from)

    norm_mod_to = _audit_date_to(mod_date_to)
    if norm_mod_to:
        where.append(f"{_trim(f'Cd.{COL_MODDATE}')} <= ?")
        params.append(norm_mod_to)

    if not where:
        where = ["1=1"]

    sql = f"""
    {_base_select(top)}
    {_base_from()}
    WHERE {" AND ".join(where)}
    ORDER BY Cd.{COL_GCODE}, Cd.{COL_TCODE}
    """
    return _run_df("search_rows", sql, tuple(params))


# -----------------------------------------------------------------------------
# Compatibility API for existing views
# -----------------------------------------------------------------------------
@_cache_data(ttl=60, show_spinner=False)
def search_name(keyword: str, gcode: Optional[str] = None, top: int = 100):
    """
    구형 호출 호환용
    """
    return search_rows(
        gcode=_clean_text(gcode or ""),
        keyword=keyword,
        only_active=True,
        top=top,
    )


@_cache_data(ttl=60, show_spinner=False)
def list_by_group(gcode: str, top: int = 1000):
    """
    구형 그룹별 조회 호환용
    """
    return search_rows(
        gcode=gcode,
        only_active=True,
        top=top,
    )


def get_one_ex(gcode: str, tcode: str):
    """
    확장 단건 조회
    """
    return search_rows(
        gcode=gcode,
        tcode=tcode,
        only_active=True,
        top=1,
    )


def search_by_name(keyword: str, top: int = 1000):
    """
    과거 호출 호환용
    """
    return search_name(keyword=keyword, gcode=None, top=top)


# -----------------------------------------------------------------------------
# Helper lookups
# -----------------------------------------------------------------------------
@_cache_data(ttl=60, show_spinner=False)
def find_group_codes_by_kind_name(kind_name: str, top: int = 50, only_active: bool = True):
    """
    코드종류명(한글) -> 그룹코드 후보
    예: '배송' 입력 시 9999 사전에서 해당 Tcode 목록 반환
    """
    top = _normalize_top(top, default=50, max_value=500)
    kw = _like(kind_name)

    where = [f"{_trim(f'Cd.{COL_GCODE}')} = ?"]
    params = [GCODE_KIND]

    if only_active:
        where.append(_active_clause("Cd"))

    if kw:
        where.append(f"{_trim(f'Cd.{COL_HNM}')} LIKE ?")
        params.append(kw)

    sql = f"""
    SELECT TOP {top}
           Cd.{COL_TCODE} AS Rd01_Gcode,
           Cd.{COL_HNM}   AS {AL_KINDNM},
           Cd.{COL_GCODE} AS Dict_Gcode,
           Cd.{COL_TCODE} AS Dict_Tcode,
           Cd.{COL_HNM}   AS Dict_Hnm
    FROM {T} AS Cd WITH (NOLOCK)
    WHERE {" AND ".join(where)}
    ORDER BY Cd.{COL_TCODE}
    """
    return _run_df("find_group_codes_by_kind_name", sql, tuple(params))


@_cache_data(ttl=60, show_spinner=False)
def get_kind_name(gcode: str) -> str:
    sql = f"""
    SELECT TOP 1
           Cd.{COL_HNM} AS kind_name
    FROM {T} AS Cd WITH (NOLOCK)
    WHERE {_trim(f'Cd.{COL_GCODE}')} = ?
      AND {_trim(f'Cd.{COL_TCODE}')} = ?
    ORDER BY Cd.{COL_TCODE}
    """
    df = _run_df("get_kind_name", sql, (GCODE_KIND, _clean_text(gcode)))
    if df is None or len(df) == 0:
        return ""
    return str(df.iloc[0]["kind_name"]).strip()


@_cache_data(ttl=60, show_spinner=False)
def get_code_name(gcode: str, tcode: str) -> str:
    sql = f"""
    SELECT TOP 1
           Cd.{COL_HNM} AS code_name
    FROM {T} AS Cd WITH (NOLOCK)
    WHERE {_trim(f'Cd.{COL_GCODE}')} = ?
      AND {_trim(f'Cd.{COL_TCODE}')} = ?
    ORDER BY Cd.{COL_TCODE}
    """
    df = _run_df("get_code_name", sql, (_clean_text(gcode), _clean_text(tcode)))
    if df is None or len(df) == 0:
        return ""
    return str(df.iloc[0]["code_name"]).strip()
