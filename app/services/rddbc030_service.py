# app/services/rddbc030_service.py
from __future__ import annotations

import os
from typing import Optional
from datetime import datetime, timedelta
import logging
import time

from app.db.mssql_client import read_df
from app.db.schema_map import SCHEMA as S
from app.db.schema_utils import al as _al_shared

log = logging.getLogger("ssai.sims.rddbc030")

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
T = S["tables"]["rddbc030"]
T010 = S["tables"]["rddbc010"]
T060 = S["tables"]["rddbc060"]
T021 = S["tables"].get("rddbc021", "dbo.Rddbc021")

C = S["cols"]["rddbc030"]
C010 = S["cols"]["rddbc010"]
C060 = S["cols"]["rddbc060"]
C021 = S["cols"].get("rddbc021", {})

_ALIAS_030 = (S.get("aliases") or S.get("alias") or {}).get("rddbc030", {})
_ALIAS_060 = (S.get("aliases") or S.get("alias") or {}).get("rddbc060", {})


def _al(d, k, default):
    return _al_shared(d, k, default)


def _col(cols: dict, *keys: str, default: str) -> str:
    for k in keys:
        v = cols.get(k)
        if v:
            return v
    return default


# -----------------------------------------------------------------------------
# Columns
# -----------------------------------------------------------------------------
COL_VEN_CD = _col(C, "ven_cd", default="Rd03_Ven_Cd")
COL_VEN_NM = _col(C, "ven_nm", default="Rd03_Ven_Nm")

COL_VEN_PRT = _col(C, "ven_prt", default="Rd03_Ven_PRT")
COL_OWNER_NM = _col(C, "owner_nm", default="Rd03_Owner_Nm")

COL_ADDRESS = _col(C, "address", "addr", default="Rd03_Address")
COL_ADDRESS2 = _col(C, "address2", "addr2", default="Rd03_Address2")

# 도로명주소 연결용 Rddbc030 필드
COL_ROAD_CD = _col(C, "road_cd", default="Rd03_RoadCd")
COL_DONG_SEQ = _col(C, "dong_seq", default="Rd03_DongSeq")
COL_BUILDING_NUM = _col(C, "building_num", default="Rd03_BuildingNum")
COL_BUILDING_SUB_NUM = _col(C, "building_sub_num", default="RD03_BUILDINGSUBNUM")
COL_BUILDING_DETAIL_NM = _col(C, "building_detail_nm", default="Rd03_BuildingDetailNm")
COL_ROAD_AREA = _col(C, "road_area", default="Rd03_RoadArea")

COL_BIZ_NO = _col(C, "biz_no", "ven_num", "business_no", default="Rd03_Ven_Num")

COL_PHONE = _col(C, "phone", default="Rd03_Phone")
COL_FAX = _col(C, "fax", default="Rd03_Fax")
COL_HP = _col(C, "hp", "mobile", default="Rd03_HP")
COL_EMAIL = _col(C, "email", "email_addr", default="Rd03_EMail")

COL_REMARK = _col(C, "remark", default="Rd03_Remark")
COL_SALES_MAN = _col(C, "sales_man", default="Rd03_Sales_Man")

COL_VEN_GROUP_G = _col(C, "ven_group_gcode", default="Rd03_Ven_Group_Gcode")
COL_VEN_GROUP = _col(C, "ven_group", default="Rd03_Ven_Group")
COL_VEN_KIND_G = _col(C, "ven_kind_gcode", default="Rd03_Ven_Kind_Gcode")
COL_VEN_KIND = _col(C, "ven_kind", default="Rd03_Ven_Kind")

COL_COST_APPLY_CD = _col(C, "cost_apply_cd", default="Rd03_Cost_Apply_Cd")
COL_STOCK_APPLY_CD = _col(C, "stock_apply_cd", default="Rd03_Stock_Apply_Cd")

COL_DELIVERY_DI_G = _col(C, "delivery_di_gcode", default="Rd03_Delivery_Di_Gcode")
COL_DELIVERY_DI = _col(C, "delivery_di", default="Rd03_Delivery_Di")

COL_UNIFY_VEN_CD = _col(C, "unify_ven_cd", default="Rd03_Unify_Ven_Cd")

COL_VEN_RANK_G = _col(C, "ven_rank_gcode", default="Rd03_Ven_Rank_Gcode")
COL_VEN_RANK = _col(C, "ven_rank", default="Rd03_Ven_Rank")

COL_PRINTER_KIND_G = _col(C, "printer_kind_gcode", default="Rd03_Printer_Kind_Gcode")
COL_PRINTER_KIND = _col(C, "printer_kind", default="Rd03_Printer_Kind")

COL_CONTRACT_CD_G = _col(C, "contract_cd_gcode", default="Rd03_Contract_Cd_Gcode")
COL_CONTRACT_CD = _col(C, "contract_cd", default="Rd03_Contract_Cd")

COL_SUPPLY_CD_G = _col(C, "supply_cd_gcode", default="Rd03_Supply_Cd_Gcode")
COL_SUPPLY_CD = _col(C, "supply_cd", default="Rd03_Supply_Cd")

COL_UNITY_GU_G = _col(C, "unity_gu_gcode", default="Rd03_Unity_Gu_Gcode")
COL_UNITY_GU = _col(C, "unity_gu", default="Rd03_Unity_Gu")

COL_VEN_TAX_G = _col(C, "ven_tax_gcode", default="Rd03_Ven_Tax_Gcode")
COL_VEN_TAX = _col(C, "ven_tax", default="Rd03_Ven_Tax")

COL_PERMIT_CLASS_G = _col(C, "permit_class_gcode", default="Rd03_Permit_Class_Gcode")
COL_PERMIT_CLASS = _col(C, "permit_class", default="Rd03_Permit_Class")

COL_TAX_TYPE_G = _col(C, "tax_type_gcode", default="Rd03_Tax_Type_Gcode")
COL_TAX_TYPE = _col(C, "tax_type", default="Rd03_Tax_Type")

COL_UDI_SUPPLY_CD_G = _col(C, "udi_supply_cd_gcode", default="Rd03_Udi_Supply_Cd_Gcode")
COL_UDI_SUPPLY_CD = _col(C, "udi_supply_cd", default="Rd03_Udi_Supply_Cd")

COL_DELFLAG = _col(C, "delflag", "del_flag", default="Rd03_Del_Flag")
COL_ADDDATE = _col(C, "add_date", default="Rd03_Add_Date")
COL_ADDCD = _col(C, "add_cd", default="Rd03_Add_Cd")
COL_MODDATE = _col(C, "mod_date", default="Rd03_Mod_Date")
COL_MODCD = _col(C, "mod_cd", default="Rd03_Mod_Cd")

COL010_GCODE = _col(C010, "gcode", default="Rd01_Gcode")
COL010_TCODE = _col(C010, "tcode", default="Rd01_Tcode")
COL010_HNM = _col(C010, "hnm", default="Rd01_Hnm")

COL060_USERCD = _col(C060, "user_cd", default="Rd06_User_Cd")
COL060_USERNM = _col(C060, "user_nm", default="Rd06_User_Nm")

AL_ADDUNM = _al(_ALIAS_060, "add_user_nm", "add_user_nm")
AL_MODUNM = _al(_ALIAS_060, "mod_user_nm", "mod_user_nm")

COL021_ROAD_CD = _col(C021, "road_cd", default="Rd021_RoadCd")
COL021_DONG_SEQ = _col(C021, "dong_seq", default="Rd021_DongSeq")
COL021_ROAD_NM = _col(C021, "road_nm", default="Rd021_RoadNm")
COL021_ROAD_ENM = _col(C021, "road_enm", default="Rd021_RoadEnm")
COL021_SIDO = _col(C021, "sido", default="Rd021_Sido")
COL021_GUGUN = _col(C021, "gugun", default="Rd021_Gugun")
COL021_DONG_GU = _col(C021, "dong_gu", default="Rd021_DongGu")
COL021_DONG_CD = _col(C021, "dong_cd", default="Rd021_DongCd")
COL021_DONG_NM = _col(C021, "dong_nm", default="Rd021_DongNm")


# -----------------------------------------------------------------------------
# Utils
# -----------------------------------------------------------------------------
def _trim(expr: str) -> str:
    return f"LTRIM(RTRIM({expr}))"


def _fixed_char_join_eq(left: str, right: str) -> str:
    """Use native equality for verified fixed-char PK/FK master joins."""
    return f"{left} = {right}"


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

def _normalize_top(value: int, default: int = 200, max_value: Optional[int] = None) -> int:
    try:
        v = int(value)
    except Exception:
        v = default

    if v < 0:
        v = default

    if max_value is None:
        return v

    normalized_max = int(max_value)
    if normalized_max <= 0:
        return v
    if v <= 0:
        return 0
    return min(v, normalized_max)


def _active_clause(alias: str = "V") -> str:
    # Rddbc030 은 'E' 도 신규 등록 불가 상태라고 문서에 명시됨.
    return (
        f"ISNULL(NULLIF({_trim(f'{alias}.{COL_DELFLAG}')}, ''), '0') "
        f"NOT IN ('1', 'Y', 'E')"
    )


def _scope_clause(scope: str, alias: str = "V") -> str:
    col = _trim(f"{alias}.{COL_VEN_CD}")
    scope = _clean_text(scope).lower()

    mapping = {
        "maker": f"{col} BETWEEN '10001' AND '18999'",
        "purchase": f"{col} BETWEEN '00001' AND '3ZZZZ'",
        "account_purchase": f"{col} BETWEEN '40000' AND '4ZZZZ'",
        "sales": f"{col} BETWEEN '50000' AND '8ZZZZ'",
        "account_sales": f"{col} BETWEEN '90001' AND '9ZZZZ'",
        # Historical documents may reference both business and account sales
        # masters.  Keep this separate from the active UI sales scope.
        "sales_history": f"{col} BETWEEN '50000' AND '9ZZZZ'",
        "purchase_history": f"{col} BETWEEN '00001' AND '4ZZZZ'",
        "cost_apply": f"{col} BETWEEN '50000' AND '8ZZZZ'",
        "stock_apply": f"{col} BETWEEN '50000' AND '8ZZZZ'",
    }
    return mapping.get(scope, "")


def _run_df(action: str, sql: str, params: tuple):
    t0 = time.perf_counter()
    try:
        df = read_df(sql, params)
        ms = int((time.perf_counter() - t0) * 1000)
        try:
            log.info(
                "[svc.rddbc030] action=%s rows=%s elapsed_ms=%s param_count=%s",
                action, len(df), ms, len(params)
            )
        except Exception:
            pass
        return df
    except Exception as exc:
        ms = int((time.perf_counter() - t0) * 1000)
        log.warning(
            "[svc.rddbc030] action=%s status=error elapsed_ms=%s param_count=%s exception_class=%s",
            action, ms, len(params), type(exc).__name__,
        )
        raise


def _code_join(alias_name: str, g_expr: str, t_expr: str) -> str:
    return f"""
    LEFT JOIN {T010} AS {alias_name} WITH (NOLOCK)
           ON {_fixed_char_join_eq(f'{alias_name}.{COL010_GCODE}', g_expr)}
          AND {_fixed_char_join_eq(f'{alias_name}.{COL010_TCODE}', t_expr)}
    """


def _base_select(top: int) -> str:
    top_clause = f"TOP {top}" if top > 0 else ""
    return f"""
    SELECT {top_clause}
           V.{COL_VEN_CD}        AS Rd03_Ven_Cd,
           V.{COL_VEN_NM}        AS Rd03_Ven_Nm,
           V.{COL_VEN_PRT}       AS Rd03_Ven_PRT,
           V.{COL_OWNER_NM}      AS Rd03_Owner_Nm,

            V.{COL_ADDRESS}       AS Rd03_Address,
           V.{COL_ADDRESS2}      AS Rd03_Address2,

           V.{COL_ROAD_CD}       AS Rd03_RoadCd,
           V.{COL_DONG_SEQ}      AS Rd03_DongSeq,
           V.{COL_BUILDING_NUM}  AS Rd03_BuildingNum,
           V.{COL_BUILDING_SUB_NUM} AS RD03_BUILDINGSUBNUM,
           V.{COL_BUILDING_DETAIL_NM} AS Rd03_BuildingDetailNm,
           V.{COL_ROAD_AREA}     AS Rd03_RoadArea,

           Road1.{COL021_SIDO}   AS road_sido_nm,
           Road1.{COL021_GUGUN}  AS road_gugun_nm,
           Road1.{COL021_DONG_NM} AS road_dong_nm,
           Road1.{COL021_ROAD_NM} AS road_nm,
           Road1.{COL021_ROAD_ENM} AS road_enm,

           LTRIM(RTRIM(
               ISNULL(Road1.{COL021_SIDO}, '') + ' ' +
               ISNULL(Road1.{COL021_GUGUN}, '') + ' ' +
               ISNULL(Road1.{COL021_ROAD_NM}, '') + ' ' +
               ISNULL(V.{COL_BUILDING_NUM}, '') +
               CASE
                   WHEN ISNULL(V.{COL_BUILDING_SUB_NUM}, 0) = 0
                   THEN ' '
                   ELSE '-' + CAST(V.{COL_BUILDING_SUB_NUM} AS VARCHAR(20)) + ' '
               END +
               ISNULL(V.{COL_BUILDING_DETAIL_NM}, '') +
               CASE
                   WHEN LTRIM(RTRIM(ISNULL(V.{COL_ROAD_CD}, ''))) = ''
                   THEN ''
                   ELSE ' (' + ISNULL(Road1.{COL021_DONG_NM}, '') + ')'
               END
           )) AS road_full_addr,

           V.{COL_BIZ_NO}        AS Rd03_Ven_Num,
           V.{COL_PHONE}         AS Rd03_Phone,
           V.{COL_FAX}           AS Rd03_Fax,
           V.{COL_HP}            AS Rd03_HP,
           V.{COL_EMAIL}         AS Rd03_EMail,

           V.{COL_REMARK}        AS Rd03_Remark,
           V.{COL_SALES_MAN}     AS Rd03_Sales_Man,

           ISNULL(NULLIF({_trim(f'SalesMan.{COL060_USERNM}')}, ''), {_trim(f'V.{COL_SALES_MAN}')}) AS sales_man_nm,

           V.{COL_VEN_GROUP_G}   AS Rd03_Ven_Group_Gcode,
           V.{COL_VEN_GROUP}     AS Rd03_Ven_Group,
           VenGroup.{COL010_HNM} AS ven_group_nm,

           V.{COL_VEN_KIND_G}    AS Rd03_Ven_Kind_Gcode,
           V.{COL_VEN_KIND}      AS Rd03_Ven_Kind,
           VenKind.{COL010_HNM}  AS ven_kind_nm,

           V.{COL_COST_APPLY_CD} AS Rd03_Cost_Apply_Cd,
           CostApply.{COL_VEN_NM} AS cost_apply_nm,

           V.{COL_STOCK_APPLY_CD} AS Rd03_Stock_Apply_Cd,
           StockApply.{COL_VEN_NM} AS stock_apply_nm,

           V.{COL_DELIVERY_DI_G}  AS Rd03_Delivery_Di_Gcode,
           V.{COL_DELIVERY_DI}    AS Rd03_Delivery_Di,
           DeliveryDi.{COL010_HNM} AS delivery_di_nm,

           V.{COL_UNIFY_VEN_CD}   AS Rd03_Unify_Ven_Cd,
           UnifyVen.{COL_VEN_NM}  AS unify_ven_nm,

           V.{COL_VEN_RANK_G}     AS Rd03_Ven_Rank_Gcode,
           V.{COL_VEN_RANK}       AS Rd03_Ven_Rank,
           VenRank.{COL010_HNM}   AS ven_rank_nm,

           V.{COL_PRINTER_KIND_G} AS Rd03_Printer_Kind_Gcode,
           V.{COL_PRINTER_KIND}   AS Rd03_Printer_Kind,
           PrinterKind.{COL010_HNM} AS printer_kind_nm,

           V.{COL_CONTRACT_CD_G}  AS Rd03_Contract_Cd_Gcode,
           V.{COL_CONTRACT_CD}    AS Rd03_Contract_Cd,
           ContractCd.{COL010_HNM} AS contract_cd_nm,

           V.{COL_SUPPLY_CD_G}    AS Rd03_Supply_Cd_Gcode,
           V.{COL_SUPPLY_CD}      AS Rd03_Supply_Cd,
           SupplyCd.{COL010_HNM}  AS supply_cd_nm,

           V.{COL_UNITY_GU_G}     AS Rd03_Unity_Gu_Gcode,
           V.{COL_UNITY_GU}       AS Rd03_Unity_Gu,
           UnityGu.{COL010_HNM}   AS unity_gu_nm,

           V.{COL_VEN_TAX_G}      AS Rd03_Ven_Tax_Gcode,
           V.{COL_VEN_TAX}        AS Rd03_Ven_Tax,
           VenTax.{COL010_HNM}    AS ven_tax_nm,

           V.{COL_PERMIT_CLASS_G} AS Rd03_Permit_Class_Gcode,
           V.{COL_PERMIT_CLASS}   AS Rd03_Permit_Class,
           PermitClass.{COL010_HNM} AS permit_class_nm,

           V.{COL_TAX_TYPE_G}     AS Rd03_Tax_Type_Gcode,
           V.{COL_TAX_TYPE}       AS Rd03_Tax_Type,
           TaxType.{COL010_HNM}   AS tax_type_nm,

           V.{COL_UDI_SUPPLY_CD_G} AS Rd03_Udi_Supply_Cd_Gcode,
           V.{COL_UDI_SUPPLY_CD}   AS Rd03_Udi_Supply_Cd,
           UdiSupplyCd.{COL010_HNM} AS udi_supply_cd_nm,

           V.{COL_DELFLAG}       AS Rd03_Del_Flag,
           V.{COL_ADDDATE}       AS Rd03_Add_Date,
           V.{COL_ADDCD}         AS Rd03_Add_Cd,
           ISNULL(NULLIF({_trim(f'AddU.{COL060_USERNM}')}, ''), {_trim(f'V.{COL_ADDCD}')}) AS {AL_ADDUNM},
           V.{COL_MODDATE}       AS Rd03_Mod_Date,
           V.{COL_MODCD}         AS Rd03_Mod_Cd,
           ISNULL(NULLIF({_trim(f'ModU.{COL060_USERNM}')}, ''), {_trim(f'V.{COL_MODCD}')}) AS {AL_MODUNM}
    """


def _base_from() -> str:
    return f"""
    FROM {T} AS V WITH (NOLOCK)

    LEFT JOIN {T060} AS AddU WITH (NOLOCK)
           ON {_fixed_char_join_eq(f'AddU.{COL060_USERCD}', f'V.{COL_ADDCD}')}
    LEFT JOIN {T060} AS ModU WITH (NOLOCK)
           ON {_fixed_char_join_eq(f'ModU.{COL060_USERCD}', f'V.{COL_MODCD}')}
    LEFT JOIN {T060} AS SalesMan WITH (NOLOCK)
           ON {_fixed_char_join_eq(f'SalesMan.{COL060_USERCD}', f'V.{COL_SALES_MAN}')}

    LEFT JOIN {T021} AS Road1 WITH (NOLOCK)
           ON {_fixed_char_join_eq(f'Road1.{COL021_ROAD_CD}', f'V.{COL_ROAD_CD}')}
          AND {_fixed_char_join_eq(f'Road1.{COL021_DONG_SEQ}', f'V.{COL_DONG_SEQ}')}

    {_code_join("VenGroup", f"V.{COL_VEN_GROUP_G}", f"V.{COL_VEN_GROUP}")}
    {_code_join("VenKind", f"V.{COL_VEN_KIND_G}", f"V.{COL_VEN_KIND}")}
    {_code_join("DeliveryDi", f"V.{COL_DELIVERY_DI_G}", f"V.{COL_DELIVERY_DI}")}
    {_code_join("VenRank", f"V.{COL_VEN_RANK_G}", f"V.{COL_VEN_RANK}")}
    {_code_join("PrinterKind", f"V.{COL_PRINTER_KIND_G}", f"V.{COL_PRINTER_KIND}")}
    {_code_join("ContractCd", f"V.{COL_CONTRACT_CD_G}", f"V.{COL_CONTRACT_CD}")}
    {_code_join("SupplyCd", f"V.{COL_SUPPLY_CD_G}", f"V.{COL_SUPPLY_CD}")}
    {_code_join("UnityGu", f"V.{COL_UNITY_GU_G}", f"V.{COL_UNITY_GU}")}
    {_code_join("VenTax", f"V.{COL_VEN_TAX_G}", f"V.{COL_VEN_TAX}")}
    {_code_join("PermitClass", f"V.{COL_PERMIT_CLASS_G}", f"V.{COL_PERMIT_CLASS}")}
    {_code_join("TaxType", f"V.{COL_TAX_TYPE_G}", f"V.{COL_TAX_TYPE}")}
    {_code_join("UdiSupplyCd", f"V.{COL_UDI_SUPPLY_CD_G}", f"V.{COL_UDI_SUPPLY_CD}")}

    LEFT JOIN {T} AS CostApply WITH (NOLOCK)
           ON {_fixed_char_join_eq(f'CostApply.{COL_VEN_CD}', f'V.{COL_COST_APPLY_CD}')}
    LEFT JOIN {T} AS StockApply WITH (NOLOCK)
           ON {_fixed_char_join_eq(f'StockApply.{COL_VEN_CD}', f'V.{COL_STOCK_APPLY_CD}')}
    LEFT JOIN {T} AS UnifyVen WITH (NOLOCK)
           ON {_fixed_char_join_eq(f'UnifyVen.{COL_VEN_CD}', f'V.{COL_UNIFY_VEN_CD}')}
    """


# -----------------------------------------------------------------------------
# Main query
# -----------------------------------------------------------------------------
@_cache_data(ttl=60, show_spinner=False)
def search_rows(
    *,
    scope: str = "",
    ven_cd: str = "",
    ven_nm_kw: str = "",
    owner_nm_kw: str = "",
    biz_no_kw: str = "",
    phone_kw: str = "",
    addr_kw: str = "",

    road_addr_kw: str = "",
    sido_nm: str = "",
    gugun_nm: str = "",
    dong_nm: str = "",
    road_nm: str = "",
    keyword: str = "",

    cost_apply_cd: str = "",    
    stock_apply_cd: str = "",
    unify_ven_cd: str = "",
    cost_apply_nm_kw: str = "",
    stock_apply_nm_kw: str = "",
    
    sales_man_nm_kw: str = "",
    add_user_nm_kw: str = "",
    add_date_from: str = "",
    add_date_to: str = "",
    mod_user_nm_kw: str = "",
    mod_date_from: str = "",
    mod_date_to: str = "",

    ven_group: str = "",
    ven_kind: str = "",
    only_active: bool = True,
    top: int = 200,
):
    
    top = _normalize_top(top, default=200)

    where = []
    params = []

    if only_active:
        where.append(_active_clause("V"))

    scope_clause = _scope_clause(scope, "V")
    if scope_clause:
        where.append(scope_clause)

    ven_cd = _clean_text(ven_cd)
    if ven_cd:
        where.append(f"{_trim(f'V.{COL_VEN_CD}')} = ?")
        params.append(ven_cd)

    kw_ven_nm = _like(ven_nm_kw)
    if kw_ven_nm:
        where.append(f"{_trim(f'V.{COL_VEN_NM}')} LIKE ?")
        params.append(kw_ven_nm)

    kw_owner = _like(owner_nm_kw)
    if kw_owner:
        where.append(f"{_trim(f'V.{COL_OWNER_NM}')} LIKE ?")
        params.append(kw_owner)

    kw_biz = _like(biz_no_kw)
    if kw_biz:
        where.append(f"{_trim(f'V.{COL_BIZ_NO}')} LIKE ?")
        params.append(kw_biz)

    kw_phone = _like(phone_kw)
    if kw_phone:
        where.append(
            "("
            + " OR ".join([
                f"{_trim(f'V.{COL_PHONE}')} LIKE ?",
                f"{_trim(f'V.{COL_FAX}')} LIKE ?",
                f"{_trim(f'V.{COL_HP}')} LIKE ?",
            ])
            + ")"
        )
        params.extend([kw_phone] * 3)

    kw_addr = _like(addr_kw)
    if kw_addr:
        where.append(
            "("
            + " OR ".join([
                f"{_trim(f'V.{COL_ADDRESS}')} LIKE ?",
                f"{_trim(f'V.{COL_ADDRESS2}')} LIKE ?",
                f"{_trim(f'Road1.{COL021_SIDO}')} LIKE ?",
                f"{_trim(f'Road1.{COL021_GUGUN}')} LIKE ?",
                f"{_trim(f'Road1.{COL021_DONG_NM}')} LIKE ?",
                f"{_trim(f'Road1.{COL021_ROAD_NM}')} LIKE ?",
            ])
            + ")"
        )
        params.extend([kw_addr] * 6)

    kw_road_addr = _like(road_addr_kw)
    if kw_road_addr:
        where.append(
            "("
            + " OR ".join([
                f"{_trim(f'Road1.{COL021_SIDO}')} LIKE ?",
                f"{_trim(f'Road1.{COL021_GUGUN}')} LIKE ?",
                f"{_trim(f'Road1.{COL021_DONG_NM}')} LIKE ?",
                f"{_trim(f'Road1.{COL021_ROAD_NM}')} LIKE ?",
                f"{_trim(f'V.{COL_BUILDING_NUM}')} LIKE ?",
                f"{_trim(f'V.{COL_BUILDING_DETAIL_NM}')} LIKE ?",
            ])
            + ")"
        )
        params.extend([kw_road_addr] * 6)

    kw_sido = _like(sido_nm)
    if kw_sido:
        where.append(f"{_trim(f'Road1.{COL021_SIDO}')} LIKE ?")
        params.append(kw_sido)

    kw_gugun = _like(gugun_nm)
    if kw_gugun:
        where.append(f"{_trim(f'Road1.{COL021_GUGUN}')} LIKE ?")
        params.append(kw_gugun)

    kw_dong = _like(dong_nm)
    if kw_dong:
        where.append(f"{_trim(f'Road1.{COL021_DONG_NM}')} LIKE ?")
        params.append(kw_dong)

    kw_road = _like(road_nm)
    if kw_road:
        where.append(f"{_trim(f'Road1.{COL021_ROAD_NM}')} LIKE ?")
        params.append(kw_road)


    cost_apply_cd = _clean_text(cost_apply_cd)
    if cost_apply_cd:
        where.append(f"{_trim(f'V.{COL_COST_APPLY_CD}')} = ?")
        params.append(cost_apply_cd)

    stock_apply_cd = _clean_text(stock_apply_cd)
    if stock_apply_cd:
        where.append(f"{_trim(f'V.{COL_STOCK_APPLY_CD}')} = ?")
        params.append(stock_apply_cd)


    kw_cost_apply_nm = _like(cost_apply_nm_kw)
    if kw_cost_apply_nm:
        where.append(f"{_trim(f'CostApply.{COL_VEN_NM}')} LIKE ?")
        params.append(kw_cost_apply_nm)

    kw_stock_apply_nm = _like(stock_apply_nm_kw)
    if kw_stock_apply_nm:
        where.append(f"{_trim(f'StockApply.{COL_VEN_NM}')} LIKE ?")
        params.append(kw_stock_apply_nm)

    kw_sales_man_nm = _like(sales_man_nm_kw)
    if kw_sales_man_nm:
        where.append(
            f"ISNULL(NULLIF({_trim(f'SalesMan.{COL060_USERNM}')}, ''), {_trim(f'V.{COL_SALES_MAN}')}) LIKE ?"
        )
        params.append(kw_sales_man_nm)

    kw_add_user = _like(add_user_nm_kw)
    if kw_add_user:
        where.append(f"{_trim(f'AddU.{COL060_USERNM}')} LIKE ?")
        params.append(kw_add_user)

    add_date_from = _norm_date_from(add_date_from)
    if add_date_from:
        where.append(f"{_trim(f'V.{COL_ADDDATE}')} >= ?")
        params.append(add_date_from)

    add_date_to = _norm_date_to(add_date_to)
    if add_date_to:
        where.append(f"{_trim(f'V.{COL_ADDDATE}')} <= ?")
        params.append(add_date_to)


    kw_mod_user_nm = _like(mod_user_nm_kw)
    if kw_mod_user_nm:
        where.append(
            f"ISNULL(NULLIF({_trim(f'ModU.{COL060_USERNM}')}, ''), {_trim(f'V.{COL_MODCD}')}) LIKE ?"
        )
        params.append(kw_mod_user_nm)

    mod_date_from = _norm_date_from(mod_date_from)
    if mod_date_from:
        where.append(f"{_trim(f'V.{COL_MODDATE}')} >= ?")
        params.append(mod_date_from)

    mod_date_to = _norm_date_to(mod_date_to)
    if mod_date_to:
        where.append(f"{_trim(f'V.{COL_MODDATE}')} <= ?")
        params.append(mod_date_to)

    unify_ven_cd = _clean_text(unify_ven_cd)
    if unify_ven_cd:
        where.append(f"{_trim(f'V.{COL_UNIFY_VEN_CD}')} = ?")
        params.append(unify_ven_cd)

    ven_group = _clean_text(ven_group)
    if ven_group:
        where.append(f"{_trim(f'V.{COL_VEN_GROUP}')} = ?")
        params.append(ven_group)

    ven_kind = _clean_text(ven_kind)
    if ven_kind:
        where.append(f"{_trim(f'V.{COL_VEN_KIND}')} = ?")
        params.append(ven_kind)

    kw = _like(keyword)
    if kw:

        kw = _like(keyword)
        if kw:
            keyword_terms = [
                f"{_trim(f'V.{COL_VEN_CD}')} LIKE ?",
                f"{_trim(f'V.{COL_VEN_NM}')} LIKE ?",
                f"{_trim(f'V.{COL_VEN_PRT}')} LIKE ?",
                f"{_trim(f'V.{COL_OWNER_NM}')} LIKE ?",

                f"{_trim(f'V.{COL_ADDRESS}')} LIKE ?",
                f"{_trim(f'V.{COL_ADDRESS2}')} LIKE ?",
                f"{_trim(f'Road1.{COL021_SIDO}')} LIKE ?",
                f"{_trim(f'Road1.{COL021_GUGUN}')} LIKE ?",
                f"{_trim(f'Road1.{COL021_DONG_NM}')} LIKE ?",
                f"{_trim(f'Road1.{COL021_ROAD_NM}')} LIKE ?",
                f"{_trim(f'V.{COL_BUILDING_NUM}')} LIKE ?",
                f"{_trim(f'V.{COL_BUILDING_DETAIL_NM}')} LIKE ?",
                f"{_trim(f'V.{COL_PHONE}')} LIKE ?",


                f"{_trim(f'V.{COL_FAX}')} LIKE ?",
                f"{_trim(f'V.{COL_HP}')} LIKE ?",
                f"{_trim(f'V.{COL_EMAIL}')} LIKE ?",
                f"{_trim(f'V.{COL_BIZ_NO}')} LIKE ?",

                f"ISNULL(NULLIF({_trim(f'SalesMan.{COL060_USERNM}')}, ''), {_trim(f'V.{COL_SALES_MAN}')}) LIKE ?",
                f"ISNULL(NULLIF({_trim(f'ModU.{COL060_USERNM}')}, ''), {_trim(f'V.{COL_MODCD}')}) LIKE ?",

                f"{_trim(f'VenGroup.{COL010_HNM}')} LIKE ?",
                f"{_trim(f'VenKind.{COL010_HNM}')} LIKE ?",
                f"{_trim(f'DeliveryDi.{COL010_HNM}')} LIKE ?",
                f"{_trim(f'VenRank.{COL010_HNM}')} LIKE ?",
                f"{_trim(f'ContractCd.{COL010_HNM}')} LIKE ?",
                f"{_trim(f'SupplyCd.{COL010_HNM}')} LIKE ?",

                f"{_trim(f'CostApply.{COL_VEN_NM}')} LIKE ?",
                f"{_trim(f'StockApply.{COL_VEN_NM}')} LIKE ?",
                f"{_trim(f'UnifyVen.{COL_VEN_NM}')} LIKE ?",
            ]

            where.append("(" + " OR ".join(keyword_terms) + ")")
            params.extend([kw] * len(keyword_terms))

    sql = f"""
    {_base_select(top)}
    {_base_from()}
    WHERE {" AND ".join(where)}
    ORDER BY V.{COL_VEN_CD}
    """
    return _run_df("search_rows", sql, tuple(params))

# -----------------------------------------------------------------------------
# Compatibility wrappers
# -----------------------------------------------------------------------------
@_cache_data(ttl=60, show_spinner=False)
def search_name(keyword: str, scope: Optional[str] = None, top: int = 100):
    return search_rows(
        scope=_clean_text(scope or ""),
        keyword=keyword,
        only_active=True,
        top=top,
    )


@_cache_data(ttl=60, show_spinner=False)
def list_by_scope(scope: str, top: int = 1000):
    return search_rows(
        scope=scope,
        only_active=True,
        top=top,
    )


def get_one_ex(ven_cd: str):
    return search_rows(
        ven_cd=ven_cd,
        only_active=True,
        top=1,
    )

# -----------------------------------------------------------------------------
# Legacy / vendors.py compatibility wrappers
# -----------------------------------------------------------------------------
@_cache_data(ttl=60, show_spinner=False)
def search_vendors_full(
    *,
    top: int = 200,
    only_active: bool = True,
    scope: str = "",
    ven_cd: str = "",
    ven_nm: str = "",
    owner_nm: str = "",
    biz_no: str = "",
    phone_kw: str = "",

    addr_kw: str = "",
    road_addr_kw: str = "",
    sido_nm: str = "",
    gugun_nm: str = "",
    dong_nm: str = "",
    road_nm: str = "",
    keyword: str = "",

    cost_apply_cd: str = "",
    stock_apply_cd: str = "",
    unify_ven_cd: str = "",
    ven_group: str = "",
    ven_kind: str = "",
    cost_apply_nm: str = "",
    stock_apply_nm: str = "",

    sales_man_nm: str = "",
    add_user_nm: str = "",
    add_date_from: str = "",
    add_date_to: str = "",
    mod_user_nm: str = "",
    mod_date_from: str = "",
    mod_date_to: str = "",


    **kwargs,
):
    """
    vendors.py 구형 호출 호환용.
    """
    return search_rows(
        scope=scope or kwargs.get("scope", ""),
        ven_cd=ven_cd or kwargs.get("vendor_cd", "") or kwargs.get("rd03_ven_cd", ""),

        ven_nm_kw=ven_nm or kwargs.get("ven_nm_kw", "") or kwargs.get("vendor_nm", ""),
        owner_nm_kw=owner_nm or kwargs.get("owner_nm_kw", ""),
        biz_no_kw=biz_no or kwargs.get("biz_no_kw", "") or kwargs.get("vendor_num", ""),
        phone_kw=phone_kw or kwargs.get("phone_kw", "") or kwargs.get("phone", "") or kwargs.get("tel", ""),

        addr_kw=addr_kw or kwargs.get("addr_kw", "") or kwargs.get("address_kw", "") or kwargs.get("address", ""),
        road_addr_kw=road_addr_kw or kwargs.get("road_addr_kw", "") or kwargs.get("road_address_kw", ""),
        sido_nm=sido_nm or kwargs.get("sido_nm", "") or kwargs.get("road_sido_nm", ""),
        gugun_nm=gugun_nm or kwargs.get("gugun_nm", "") or kwargs.get("road_gugun_nm", ""),
        dong_nm=dong_nm or kwargs.get("dong_nm", "") or kwargs.get("road_dong_nm", ""),
        road_nm=road_nm or kwargs.get("road_nm", ""),
        keyword=keyword or kwargs.get("q", ""),

        
        cost_apply_cd=cost_apply_cd or kwargs.get("cost_vendor_cd", ""),
        stock_apply_cd=stock_apply_cd or kwargs.get("stock_vendor_cd", ""),
        unify_ven_cd=unify_ven_cd or kwargs.get("master_vendor_cd", ""),
        ven_group=ven_group or kwargs.get("ven_group", ""),
        ven_kind=ven_kind or kwargs.get("ven_kind", ""),
        only_active=only_active,
        cost_apply_nm_kw=cost_apply_nm or kwargs.get("cost_apply_nm_kw", "") or kwargs.get("cost_apply_nm", ""),
        stock_apply_nm_kw=stock_apply_nm or kwargs.get("stock_apply_nm_kw", "") or kwargs.get("stock_apply_nm", ""),
        sales_man_nm_kw=sales_man_nm or kwargs.get("sales_man_nm_kw", "") or kwargs.get("salesman_nm", ""),

        add_user_nm_kw=add_user_nm or kwargs.get("add_user_nm_kw", "") or kwargs.get("add_user_nm", "") or kwargs.get("creator_nm", ""),
        add_date_from=add_date_from or kwargs.get("add_date_from", ""),
        add_date_to=add_date_to or kwargs.get("add_date_to", ""),
        mod_user_nm_kw=mod_user_nm or kwargs.get("mod_user_nm_kw", "") or kwargs.get("modifier_nm", ""),
        mod_date_from=mod_date_from or kwargs.get("mod_date_from", ""),
        mod_date_to=mod_date_to or kwargs.get("mod_date_to", ""),
        top=top,
    )


def get_vendor_full(ven_cd: str, *, only_active: bool = True):
    return search_rows(
        ven_cd=ven_cd,
        only_active=only_active,
        top=1,
    )


def search_vendors(keyword: str = "", top: int = 200, only_active: bool = True, scope: str = ""):
    return search_rows(
        keyword=keyword,
        only_active=only_active,
        top=top,
        scope=scope,
    )


def list_vendors_by_scope(scope: str, top: int = 1000, only_active: bool = True):
    return search_rows(
        scope=scope,
        only_active=only_active,
        top=top,
    )
