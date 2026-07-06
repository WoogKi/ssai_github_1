# app/services/rddbc110_service.py
#  입고명세 조회

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import pandas as pd

from app.services.rddbc_io_common import (
    add_filter,
    build_result_payload,
    apply_labels_safe,
    clean_text,
    coalesce_params,
    like_value,
    make_date_filters,
    normalize_top,
    query_to_df,
)

TABLE = "rddbc110"
log = logging.getLogger("ssai.sims.rddbc110")

def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except Exception:
        return default


def _io_query_top(params: Dict[str, Any], *, default: int = 200) -> int:
    """
    일반 화면 조회는 기본 200건.
    _max_top이 있으면 다운로드/내부 전용 최대치를 별도로 허용한다.
    """
    try:
        max_top = int(params.pop("_max_top"))
    except Exception:
        # 화면의 Top/조회건수 입력은 제거하고, 기존 공통 표시 제한 env를 조회 상한으로 사용한다.
        max_top = _env_int(
            "SIMS_PANEL_DISPLAY_MAX_ROWS",
            _env_int("SIMS_CHAT_DISPLAY_MAX_ROWS", _env_int("SIMS_IO_QUERY_MAX_ROWS", 30000)),
        )

    return normalize_top(
        params.get("top", default),
        default=default,
        max_value=max_top,
    )


def _io_export_top() -> int:
    """
    CSV/Excel 다운로드용 최대 건수.
    필요하면 .env에서 SIMS_EXPORT_MAX_ROWS=200000 처럼 조정.
    """
    return _env_int("SIMS_EXPORT_MAX_ROWS", 100000)

#  입고명세 조회
# 다양한 필터 조건을 적용하여 입고명세 데이터를 조회하고, 결과를 데이터프레임으로 반환하는 함수입니다.
def _base_filters(params: Dict[str, Any]) -> str:

    clauses: list[str] = []
    clauses += make_date_filters("In_Put.Rd11_In_YyMmDd", params)

    if clean_text(params.get("in_seq")):
        add_filter(clauses, "In_Put.Rd11_In_Seq = %(in_seq)s")

    if clean_text(params.get("ven_cd")):
        add_filter(clauses, "In_Put.Rd11_Ven_Cd = %(ven_cd)s")
    if like_value(params.get("ven_nm")):
        params["ven_nm_like"] = like_value(params.get("ven_nm"))
        add_filter(clauses, "Ven_Cd.Rd03_Ven_Nm LIKE %(ven_nm_like)s")
    if clean_text(params.get("physic_cd")):
        add_filter(clauses, "In_Put.Rd11_Physic_Cd = %(physic_cd)s")
    if like_value(params.get("physic_nm")):
        params["physic_nm_like"] = like_value(params.get("physic_nm"))
        add_filter(clauses, "Physic_Cd.Rd04_Physic_Nm LIKE %(physic_nm_like)s")
#   제품관련 필터는 제품명, 제품군명, DI명, 제조사명, 제품분류명으로 구성되며, 각각의 경우에 대해 서브쿼리를 사용하여 필터링을 적용합니다.    
    if like_value(params.get("product_ven_nm")):
        params["product_ven_nm_like"] = like_value(params.get("product_ven_nm"))
        add_filter(
            clauses,
            """EXISTS (
                SELECT 1
                FROM dbo.Rddbc030 AS Make_Ven
                WHERE Make_Ven.Rd03_Ven_Cd = Physic_Cd.Rd04_Ven_Cd
                  AND Make_Ven.Rd03_Ven_Nm LIKE %(product_ven_nm_like)s
            )""",
        )

    if like_value(params.get("product_group_nm")):
        params["product_group_nm_like"] = like_value(params.get("product_group_nm"))
        add_filter(
            clauses,
            """EXISTS (
                SELECT 1
                FROM dbo.Rddbc010 AS Physic_Group_Nm
                WHERE Physic_Group_Nm.Rd01_Gcode = Physic_Cd.Rd04_Physic_Group_Gcode
                  AND Physic_Group_Nm.Rd01_Tcode = Physic_Cd.Rd04_Physic_Group
                  AND Physic_Group_Nm.Rd01_Hnm LIKE %(product_group_nm_like)s
            )""",
        )

    if like_value(params.get("product_di_nm")):
        params["product_di_nm_like"] = like_value(params.get("product_di_nm"))
        add_filter(
            clauses,
            """EXISTS (
                SELECT 1
                FROM dbo.Rddbc010 AS Physic_Di_Nm
                WHERE Physic_Di_Nm.Rd01_Gcode = Physic_Cd.Rd04_Physic_Di_Gcode
                  AND Physic_Di_Nm.Rd01_Tcode = Physic_Cd.Rd04_Physic_Di
                  AND Physic_Di_Nm.Rd01_Hnm LIKE %(product_di_nm_like)s
            )""",
        )

    if like_value(params.get("product_class_nm")):
        params["product_class_nm_like"] = like_value(params.get("product_class_nm"))
        add_filter(
            clauses,
            """EXISTS (
                SELECT 1
                FROM dbo.Rddbc010 AS Physic_Gu_Nm
                WHERE Physic_Gu_Nm.Rd01_Gcode = Physic_Cd.Rd04_Physic_Gu_Gcode
                  AND Physic_Gu_Nm.Rd01_Tcode = Physic_Cd.Rd04_Physic_Gu
                  AND Physic_Gu_Nm.Rd01_Hnm LIKE %(product_class_nm_like)s
            )""",
        )    
    
    
    
    
    if clean_text(params.get("stock_cd")):
        add_filter(clauses, "In_Put.Rd11_Stock_Cd = %(stock_cd)s")
    if clean_text(params.get("io_gu_prefix")):
        add_filter(clauses, "LEFT(In_Put.Rd11_Io_Gu, 1) = %(io_gu_prefix)s")

    if clean_text(params.get("trans_seq")):
        add_filter(clauses, "In_Put.Rd11_Trans_Seq = %(trans_seq)s")

    trans_link = clean_text(params.get("trans_link"))
    if trans_link == "Y":
        add_filter(clauses, "NULLIF(LTRIM(RTRIM(In_Put.Rd11_Trans_Seq)), '') IS NOT NULL")
    elif trans_link == "N":
        add_filter(clauses, "NULLIF(LTRIM(RTRIM(In_Put.Rd11_Trans_Seq)), '') IS NULL")

    if clean_text(params.get("tax_seq")):
        add_filter(clauses, "In_Put.Rd11_Tax_Seq = %(tax_seq)s")

    tax_link = clean_text(params.get("tax_link"))
    if tax_link == "Y":
        add_filter(clauses, "In_Put.Rd11_Tax_Di = '1'")
    elif tax_link == "N":
        add_filter(clauses, "(In_Put.Rd11_Tax_Di IS NULL OR In_Put.Rd11_Tax_Di <> '1')")

    if clean_text(params.get("unify_ven_cd")):
        add_filter(clauses, "In_Put.Rd11_Unify_Ven_Cd = %(unify_ven_cd)s")
    if clean_text(params.get("validation")):
        add_filter(clauses, "In_Put.Rd11_Validation = %(validation)s")
    if clean_text(params.get("physic_cd")):
        add_filter(clauses, "In_Put.Rd11_Physic_Cd = %(physic_cd)s")

    if like_value(params.get("physic_nm")):
        params["physic_nm_like"] = like_value(params.get("physic_nm"))
        add_filter(clauses, "Physic_Cd.Rd04_Physic_Nm LIKE %(physic_nm_like)s")

#   제품관련 필터는 제품명, 제품군명, DI명, 제조사명, 제품분류명으로 구성되며, 각각의 경우에 대해 서브쿼리를 사용하여 필터링을 적용합니다.
    if clean_text(params.get("product_ven_cd")):
        add_filter(clauses, "Physic_Cd.Rd04_Ven_Cd = %(product_ven_cd)s")

    if like_value(params.get("product_ven_nm")):
        params["product_ven_nm_like"] = like_value(params.get("product_ven_nm"))
        add_filter(
            clauses,
            """EXISTS (
                SELECT 1
                FROM dbo.Rddbc030 AS Make_Ven
                WHERE Make_Ven.Rd03_Ven_Cd = Physic_Cd.Rd04_Ven_Cd
                  AND Make_Ven.Rd03_Ven_Nm LIKE %(product_ven_nm_like)s
            )""",
        )

    if like_value(params.get("product_group_nm")):
        params["product_group_nm_like"] = like_value(params.get("product_group_nm"))
        add_filter(
            clauses,
            """EXISTS (
                SELECT 1
                FROM dbo.Rddbc010 AS Physic_Group_Nm
                WHERE Physic_Group_Nm.Rd01_Gcode = Physic_Cd.Rd04_Physic_Group_Gcode
                  AND Physic_Group_Nm.Rd01_Tcode = Physic_Cd.Rd04_Physic_Group
                  AND Physic_Group_Nm.Rd01_Hnm LIKE %(product_group_nm_like)s
            )""",
        )

    if like_value(params.get("product_di_nm")):
        params["product_di_nm_like"] = like_value(params.get("product_di_nm"))
        add_filter(
            clauses,
            """EXISTS (
                SELECT 1
                FROM dbo.Rddbc010 AS Physic_Di_Nm
                WHERE Physic_Di_Nm.Rd01_Gcode = Physic_Cd.Rd04_Physic_Di_Gcode
                  AND Physic_Di_Nm.Rd01_Tcode = Physic_Cd.Rd04_Physic_Di
                  AND Physic_Di_Nm.Rd01_Hnm LIKE %(product_di_nm_like)s
            )""",
        )

    if like_value(params.get("product_class_nm")):
        params["product_class_nm_like"] = like_value(params.get("product_class_nm"))
        add_filter(
            clauses,
            """EXISTS (
                SELECT 1
                FROM dbo.Rddbc010 AS Physic_Gu_Nm
                WHERE Physic_Gu_Nm.Rd01_Gcode = Physic_Cd.Rd04_Physic_Gu_Gcode
                  AND Physic_Gu_Nm.Rd01_Tcode = Physic_Cd.Rd04_Physic_Gu
                  AND Physic_Gu_Nm.Rd01_Hnm LIKE %(product_class_nm_like)s
            )""",
        )



#   재고위치 필터는 재고코드와 재고명으로 구성되며, 재고코드 필터는 단순히 Rd11_Stock_Cd 컬럼과 비교하는 방식으로 적용되고, 재고명 필터는 Rddbc010 테이블과 조인하여 Rd01_Hnm 컬럼을 LIKE 연산자로 비교하는 방식으로 적용됩니다.
    if clean_text(params.get("stock_cd")):
        add_filter(clauses, "In_Put.Rd11_Stock_Cd = %(stock_cd)s")

    if like_value(params.get("stock_nm")):
        params["stock_nm_like"] = like_value(params.get("stock_nm"))
        add_filter(clauses, "Stock_Cd.Rd01_Hnm LIKE %(stock_nm_like)s")
        
    if clean_text(params.get("only_mismatch_trans")).upper() in {"Y", "1", "TRUE"}:
        add_filter(
            clauses,
            """(
                T13.Rd13_Trans_Seq IS NULL
                OR COALESCE(TS.Sum_Fin_Supply_Price, 0) <> COALESCE(T13.Rd13_Supply_Price, 0)
                OR COALESCE(TS.Sum_Fin_Tax_Price, 0) <> COALESCE(T13.Rd13_Tax_Price, 0)
            )""",
        )

    if clean_text(params.get("only_mismatch_tax")).upper() in {"Y", "1", "TRUE"}:
        add_filter(
            clauses,
            """(
                T14.Rd14_Tax_Seq IS NULL
                OR COALESCE(TX.Sum_Fin_Supply_Price, 0) <> COALESCE(T14.Rd14_Supply_Price, 0)
                OR COALESCE(TX.Sum_Fin_Tax_Price, 0) <> COALESCE(T14.Rd14_Tax_Price, 0)
            )""",
        )
    if clean_text(params.get("add_cd")):
        add_filter(clauses, "In_Put.Rd11_Add_Cd = %(add_cd)s")

    if like_value(params.get("add_nm")):
        params["add_nm_like"] = like_value(params.get("add_nm"))
        add_filter(clauses, "Add_Cd.Rd06_User_Nm LIKE %(add_nm_like)s")

    if clean_text(params.get("mod_cd")):
        add_filter(clauses, "In_Put.Rd11_Mod_Cd = %(mod_cd)s")

    if like_value(params.get("mod_nm")):
        params["mod_nm_like"] = like_value(params.get("mod_nm"))
        add_filter(clauses, "Mod_Cd.Rd06_User_Nm LIKE %(mod_nm_like)s")

    if clean_text(params.get("add_date_from")):
        add_filter(clauses, "In_Put.Rd11_Add_Date >= %(add_date_from)s")

    if clean_text(params.get("add_date_to")):
        add_filter(clauses, "In_Put.Rd11_Add_Date <= %(add_date_to)s")

    if clean_text(params.get("mod_date_from")):
        add_filter(clauses, "In_Put.Rd11_Mod_Date >= %(mod_date_from)s")

    if clean_text(params.get("mod_date_to")):
        add_filter(clauses, "In_Put.Rd11_Mod_Date <= %(mod_date_to)s")
        
    return ("\n      AND " + "\n      AND ".join(clauses)) if clauses else ""


def get_rddbc110_df(params: Optional[Dict[str, Any]] = None):
    params = coalesce_params(params)
    params["top"] = _io_query_top(params, default=200)

    where_sql = _base_filters(params)

    sql = f"""
WITH trans_sum AS (
    SELECT
        Rd11_Trans_YyMmDd,
        Rd11_Ven_Cd,
        Rd11_Trans_Seq,
        SUM(COALESCE(Rd11_Fin_Supply_Price, Rd11_Supply_Price, 0)) AS Sum_Fin_Supply_Price,
        SUM(COALESCE(Rd11_Fin_Tax_Price, Rd11_Tax_Price, 0)) AS Sum_Fin_Tax_Price
    FROM dbo.Rddbc110
    WHERE NULLIF(LTRIM(RTRIM(Rd11_Trans_Seq)), '') IS NOT NULL
    GROUP BY Rd11_Trans_YyMmDd, Rd11_Ven_Cd, Rd11_Trans_Seq
),
tax_sum AS (
    SELECT
        Rd11_Tax_YyMmDd,
        Rd11_Ven_Cd,
        Rd11_Tax_Seq,
        SUM(COALESCE(Rd11_Fin_Supply_Price, Rd11_Supply_Price, 0)) AS Sum_Fin_Supply_Price,
        SUM(COALESCE(Rd11_Fin_Tax_Price, Rd11_Tax_Price, 0)) AS Sum_Fin_Tax_Price
    FROM dbo.Rddbc110
    WHERE Rd11_Tax_Di = '1'
      AND NULLIF(LTRIM(RTRIM(Rd11_Tax_Seq)), '') IS NOT NULL
    GROUP BY Rd11_Tax_YyMmDd, Rd11_Ven_Cd, Rd11_Tax_Seq
)
SELECT TOP (%(top)s)
    In_Put.Rd11_In_YyMmDd,
    In_Put.Rd11_Ven_Cd,
    Ven_Cd.Rd03_Ven_Nm AS 거래처명,
    In_Put.Rd11_In_Seq,
    In_Put.Rd11_Stock_Cd_Gcode,
    In_Put.Rd11_Stock_Cd,
    Stock_Cd.Rd01_Hnm AS 재고위치,
    In_Put.Rd11_Physic_Cd,
    Physic_Cd.Rd04_Physic_Nm AS 제품명,
    In_Put.Rd11_Io_Gu_Gcode,
    In_Put.Rd11_Io_Gu,
    Io_Gu.Rd01_Hnm AS 입출고구분,
    LEFT(In_Put.Rd11_Io_Gu, 1) AS Io_Gu_Prefix,
    In_Put.Rd11_Cost_Apply_Cd,
    Cost_Apply.Rd03_Ven_Nm AS 단가적용처명,
    In_Put.Rd11_Stock_Apply_Cd,
    Stock_Apply.Rd03_Ven_Nm AS 재고적용처명,
    In_Put.Rd11_Unit_Cost,
    In_Put.Rd11_Quantity,
    In_Put.Rd11_Oquantity,
    In_Put.Rd11_Supply_Price,
    In_Put.Rd11_Tax_Price,
    In_Put.Rd11_Fin_Unit_Cost,
    In_Put.Rd11_Fin_Supply_Price,
    In_Put.Rd11_Fin_Tax_Price,
    In_Put.Rd11_Product_No,
    In_Put.Rd11_Term_Date,
    In_Put.Rd11_Trans_Di,
    In_Put.Rd11_Trans_YyMmDd,
    In_Put.Rd11_Trans_Seq,
    TS.Sum_Fin_Supply_Price AS 거래명세서상세합_공급가액,
    TS.Sum_Fin_Tax_Price AS 거래명세서상세합_세액,
    T13.Rd13_Supply_Price AS 거래명세서헤더_공급가액,
    T13.Rd13_Tax_Price AS 거래명세서헤더_세액,
    CASE
        WHEN T13.Rd13_Trans_Seq IS NULL THEN NULL
        WHEN COALESCE(TS.Sum_Fin_Supply_Price, 0) = COALESCE(T13.Rd13_Supply_Price, 0)
         AND COALESCE(TS.Sum_Fin_Tax_Price, 0) = COALESCE(T13.Rd13_Tax_Price, 0) THEN 'Y'
        ELSE 'N'
    END AS 거래명세서금액일치,
    In_Put.Rd11_Tax_Di,
    In_Put.Rd11_Tax_YyMmDd,
    In_Put.Rd11_Tax_Seq,
    TX.Sum_Fin_Supply_Price AS 세금계산서상세합_공급가액,
    TX.Sum_Fin_Tax_Price AS 세금계산서상세합_세액,
    T14.Rd14_Supply_Price AS 세금계산서헤더_공급가액,
    T14.Rd14_Tax_Price AS 세금계산서헤더_세액,
    CASE
        WHEN T14.Rd14_Tax_Seq IS NULL THEN NULL
        WHEN COALESCE(TX.Sum_Fin_Supply_Price, 0) = COALESCE(T14.Rd14_Supply_Price, 0)
         AND COALESCE(TX.Sum_Fin_Tax_Price, 0) = COALESCE(T14.Rd14_Tax_Price, 0) THEN 'Y'
        ELSE 'N'
    END AS 세금계산서금액일치,
    In_Put.Rd11_Other,
    In_Put.Rd11_Insu_Price,
    In_Put.Rd11_Fixed_Flag,
    In_Put.Rd11_Record_Flag,
    In_Put.Rd11_Reform_Flag,
    In_Put.Rd11_Validation,
    In_Put.Rd11_Add_Date,
    In_Put.Rd11_Add_Cd,
    Add_Cd.Rd06_User_Nm AS 등록자,
    In_Put.Rd11_Mod_Date,
    In_Put.Rd11_Mod_Cd,
    Mod_Cd.Rd06_User_Nm AS 수정자,
    In_Put.Rd11_Dope_Flag,
    In_Put.Rd11_Unify_Ven_Cd,
    In_Put.Rd11_Link_Yymmdd,
    In_Put.Rd11_Link_Ven_Cd,
    In_Put.Rd11_Link_Seq,
    In_Put.Rd11_Validation_Qty,
    In_Put.Rd11_Add_Time,
    In_Put.Rd11_Mod_Time
FROM dbo.Rddbc110 AS In_Put
LEFT JOIN dbo.Rddbc060 AS Add_Cd
    ON In_Put.Rd11_Add_Cd = Add_Cd.Rd06_User_Cd
LEFT JOIN dbo.Rddbc060 AS Mod_Cd
    ON In_Put.Rd11_Mod_Cd = Mod_Cd.Rd06_User_Cd
LEFT JOIN dbo.Rddbc030 AS Ven_Cd
    ON In_Put.Rd11_Ven_Cd = Ven_Cd.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc040 AS Physic_Cd
    ON In_Put.Rd11_Physic_Cd = Physic_Cd.Rd04_Physic_Cd
LEFT JOIN dbo.Rddbc030 AS Cost_Apply
    ON In_Put.Rd11_Cost_Apply_Cd = Cost_Apply.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS Stock_Apply
    ON In_Put.Rd11_Stock_Apply_Cd = Stock_Apply.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc010 AS Stock_Cd
    ON In_Put.Rd11_Stock_Cd_Gcode = Stock_Cd.Rd01_Gcode
   AND In_Put.Rd11_Stock_Cd = Stock_Cd.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS Io_Gu
    ON In_Put.Rd11_Io_Gu_Gcode = Io_Gu.Rd01_Gcode
   AND In_Put.Rd11_Io_Gu = Io_Gu.Rd01_Tcode
LEFT JOIN trans_sum AS TS
    ON In_Put.Rd11_Trans_YyMmDd = TS.Rd11_Trans_YyMmDd
   AND In_Put.Rd11_Ven_Cd = TS.Rd11_Ven_Cd
   AND In_Put.Rd11_Trans_Seq = TS.Rd11_Trans_Seq
LEFT JOIN dbo.Rddbc130 AS T13
    ON In_Put.Rd11_Trans_Di = T13.Rd13_Trans_Di
   AND In_Put.Rd11_Trans_YyMmDd = T13.Rd13_Trans_YyMmDd
   AND In_Put.Rd11_Ven_Cd = T13.Rd13_Ven_Cd
   AND In_Put.Rd11_Trans_Seq = T13.Rd13_Trans_Seq
LEFT JOIN tax_sum AS TX
    ON In_Put.Rd11_Tax_YyMmDd = TX.Rd11_Tax_YyMmDd
   AND In_Put.Rd11_Ven_Cd = TX.Rd11_Ven_Cd
   AND In_Put.Rd11_Tax_Seq = TX.Rd11_Tax_Seq
LEFT JOIN dbo.Rddbc140 AS T14
    ON In_Put.Rd11_Tax_Di = T14.Rd14_Tax_Di
   AND In_Put.Rd11_Tax_YyMmDd = T14.Rd14_Tax_YyMmDd
   AND In_Put.Rd11_Ven_Cd = T14.Rd14_Ven_Cd
   AND In_Put.Rd11_Tax_Seq = T14.Rd14_Tax_Seq
WHERE 1 = 1
{where_sql}
ORDER BY In_Put.Rd11_In_YyMmDd , In_Put.Rd11_Ven_Cd, In_Put.Rd11_In_Seq
"""
    df = query_to_df(sql, params)

    return df

# 입고명세 조회
# 다양한 필터 조건을 적용하여 입고명세 데이터를 조회하고, 결과를 데이터프레임으로 반환하는 함수입니다. 
# 조회된 데이터의 행 수를 로그로 기록하며, 결과가 없는 경우에는 적절한 메시지를 포함한 페이로드를 반환합니다. 
# 데이터가 있는 경우에는 결과 페이로드를 빌드하여 반환합니다.
# 조회 조건에는 입고일자, 입고번호, 거래처코드, 거래처명, 제품코드, 제품명, 재고위치코드, 재고위치명, 입출고구분, 거래명세서 연동 여부, 세금계산서 연동 여부 등이 포함됩니다. 
# 또한, 제품 관련 필터로는 제품명, 제품군명, DI명, 제조사명, 제품분류명이 있으며, 각각의 경우에 대해 서브쿼리를 사용하여 필터링을 적용합니다. 
# 재고위치 필터는 재고코드와 재고명으로 구성되며, 재고코드 필터는 단순히 Rd11_Stock_Cd 컬럼과 비교하는 방식으로 적용되고, 재고명 필터는 Rddbc010 테이블과 조인하여 Rd01_Hnm 컬럼을 LIKE 연산자로 비교하는 방식으로 적용됩니다.
# 조회 결과에는 입고명세의 상세 정보뿐만 아니라, 거래명세서 및 세금계산서와의 금액 일치 여부도 포함되어 있습니다.  
def get_rddbc110_result(params: Optional[Dict[str, Any]] = None):
    params = coalesce_params(params)
    df = get_rddbc110_df(params)

    row_count = 0 if df is None else int(len(df))
    log.info("DBG get_rddbc110_result rows=%s", row_count)

    if row_count == 0:
        return {
            "table": TABLE,
            "title": "입고명세 조회",
            "action": "입고명세 조회",
            "params": params,
            "data": "해당 자료가 없습니다.",
            "message": "해당 자료가 없습니다.",
            "final": True,
            "meta": {"row_count": 0},
        }

    return build_result_payload(
        table=TABLE,
        title="입고명세 조회",
        action="입고명세 조회",
        params=params,
        df=df,
        message=f"입고명세 {row_count:,}건",
    )

def get_rddbc110_export_df(params: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    """
    입고명세 CSV/Excel 다운로드용 전체 상세 DataFrame.

    화면 조회 TOP 200과 분리한다.
    단, 무제한은 위험하므로 SIMS_EXPORT_MAX_ROWS 기본 100000건까지 허용한다.
    """
    qparams = coalesce_params(params)
    export_top = _io_export_top()

    qparams["top"] = export_top
    qparams["_max_top"] = export_top

    df = get_rddbc110_df(qparams)

    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame() if df is None else df

    try:
        payload = build_result_payload(
            table=TABLE,
            title="입고명세 조회",
            action="입고명세 조회",
            params=qparams,
            df=df,
            message=f"입고명세 다운로드 {len(df):,}건",
        )
        out = payload.get("df_display")
        if isinstance(out, pd.DataFrame):
            return out
    except Exception:
        log.exception("get_rddbc110_export_df label/apply failed")

    return df


def _analysis_records_from_section_df(df, section: str) -> list[dict]:
    if df is None or df.empty or "section" not in df.columns:
        return []

    out: list[dict] = []
    try:
        sub = df[df["section"].astype(str) == section]
    except Exception:
        return []

    for _, row in sub.iterrows():
        name = str(row.get("name") or "").strip()
        if not name:
            name = "(미지정)"

        def _num(key: str) -> float:
            try:
                v = row.get(key)
                if v is None:
                    return 0.0
                return float(v)
            except Exception:
                return 0.0

        out.append(
            {
                "name": name,
                "row_count": int(_num("row_count")),
                "qty_sum": _num("qty_sum"),
                "supply_sum": _num("supply_sum"),
                "tax_sum": _num("tax_sum"),
                "amount_sum": _num("amount_sum"),
            }
        )

    return out


def _analysis_scalar(row, key: str, default=0):
    try:
        v = row.get(key)
        if v is None:
            return default
        if isinstance(default, int):
            return int(float(v))
        return float(v)
    except Exception:
        return default


def get_rddbc110_analysis_summary(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    입고명세 LLM 분석용 전체 집계.

    중요:
    - 화면 조회 TOP 200과 분리한다.
    - 동일 조회조건 전체 기준으로 건수/수량/금액/거래처별/제품별/재고위치별 집계를 만든다.
    """
    qparams = coalesce_params(dict(params or {}))
    where_sql = _base_filters(qparams)

    sql = f"""
WITH trans_sum AS (
    SELECT
        Rd11_Trans_YyMmDd,
        Rd11_Ven_Cd,
        Rd11_Trans_Seq,
        SUM(COALESCE(Rd11_Fin_Supply_Price, Rd11_Supply_Price, 0)) AS Sum_Fin_Supply_Price,
        SUM(COALESCE(Rd11_Fin_Tax_Price, Rd11_Tax_Price, 0)) AS Sum_Fin_Tax_Price
    FROM dbo.Rddbc110
    WHERE NULLIF(LTRIM(RTRIM(Rd11_Trans_Seq)), '') IS NOT NULL
    GROUP BY Rd11_Trans_YyMmDd, Rd11_Ven_Cd, Rd11_Trans_Seq
),
tax_sum AS (
    SELECT
        Rd11_Tax_YyMmDd,
        Rd11_Ven_Cd,
        Rd11_Tax_Seq,
        SUM(COALESCE(Rd11_Fin_Supply_Price, Rd11_Supply_Price, 0)) AS Sum_Fin_Supply_Price,
        SUM(COALESCE(Rd11_Fin_Tax_Price, Rd11_Tax_Price, 0)) AS Sum_Fin_Tax_Price
    FROM dbo.Rddbc110
    WHERE Rd11_Tax_Di = '1'
      AND NULLIF(LTRIM(RTRIM(Rd11_Tax_Seq)), '') IS NOT NULL
    GROUP BY Rd11_Tax_YyMmDd, Rd11_Ven_Cd, Rd11_Tax_Seq
),
base AS (
    SELECT
        In_Put.Rd11_In_YyMmDd AS io_date,
        NULLIF(LTRIM(RTRIM(In_Put.Rd11_Ven_Cd)), '') AS vendor_cd,
        COALESCE(NULLIF(LTRIM(RTRIM(Ven_Cd.Rd03_Ven_Nm)), ''), '(미지정)') AS vendor_nm,
        NULLIF(LTRIM(RTRIM(In_Put.Rd11_Physic_Cd)), '') AS product_cd,
        COALESCE(NULLIF(LTRIM(RTRIM(Physic_Cd.Rd04_Physic_Nm)), ''), '(미지정)') AS product_nm,
        NULLIF(LTRIM(RTRIM(In_Put.Rd11_Stock_Cd)), '') AS stock_cd,
        COALESCE(NULLIF(LTRIM(RTRIM(Stock_Cd.Rd01_Hnm)), ''), '(미지정)') AS stock_nm,
        CAST(COALESCE(In_Put.Rd11_Quantity, 0) AS float) AS qty,
        CAST(COALESCE(In_Put.Rd11_Fin_Supply_Price, In_Put.Rd11_Supply_Price, 0) AS float) AS supply_amt,
        CAST(COALESCE(In_Put.Rd11_Fin_Tax_Price, In_Put.Rd11_Tax_Price, 0) AS float) AS tax_amt
    FROM dbo.Rddbc110 AS In_Put
    LEFT JOIN dbo.Rddbc060 AS Add_Cd
        ON In_Put.Rd11_Add_Cd = Add_Cd.Rd06_User_Cd
    LEFT JOIN dbo.Rddbc060 AS Mod_Cd
        ON In_Put.Rd11_Mod_Cd = Mod_Cd.Rd06_User_Cd
    LEFT JOIN dbo.Rddbc030 AS Ven_Cd
        ON In_Put.Rd11_Ven_Cd = Ven_Cd.Rd03_Ven_Cd
    LEFT JOIN dbo.Rddbc040 AS Physic_Cd
        ON In_Put.Rd11_Physic_Cd = Physic_Cd.Rd04_Physic_Cd
    LEFT JOIN dbo.Rddbc010 AS Stock_Cd
        ON In_Put.Rd11_Stock_Cd_Gcode = Stock_Cd.Rd01_Gcode
       AND In_Put.Rd11_Stock_Cd = Stock_Cd.Rd01_Tcode
    LEFT JOIN dbo.Rddbc010 AS Io_Gu
        ON In_Put.Rd11_Io_Gu_Gcode = Io_Gu.Rd01_Gcode
       AND In_Put.Rd11_Io_Gu = Io_Gu.Rd01_Tcode
    LEFT JOIN trans_sum AS TS
        ON In_Put.Rd11_Trans_YyMmDd = TS.Rd11_Trans_YyMmDd
       AND In_Put.Rd11_Ven_Cd = TS.Rd11_Ven_Cd
       AND In_Put.Rd11_Trans_Seq = TS.Rd11_Trans_Seq
    LEFT JOIN dbo.Rddbc130 AS T13
        ON In_Put.Rd11_Trans_Di = T13.Rd13_Trans_Di
       AND In_Put.Rd11_Trans_YyMmDd = T13.Rd13_Trans_YyMmDd
       AND In_Put.Rd11_Ven_Cd = T13.Rd13_Ven_Cd
       AND In_Put.Rd11_Trans_Seq = T13.Rd13_Trans_Seq
    LEFT JOIN tax_sum AS TX
        ON In_Put.Rd11_Tax_YyMmDd = TX.Rd11_Tax_YyMmDd
       AND In_Put.Rd11_Ven_Cd = TX.Rd11_Ven_Cd
       AND In_Put.Rd11_Tax_Seq = TX.Rd11_Tax_Seq
    LEFT JOIN dbo.Rddbc140 AS T14
        ON In_Put.Rd11_Tax_Di = T14.Rd14_Tax_Di
       AND In_Put.Rd11_Tax_YyMmDd = T14.Rd14_Tax_YyMmDd
       AND In_Put.Rd11_Ven_Cd = T14.Rd14_Ven_Cd
       AND In_Put.Rd11_Tax_Seq = T14.Rd14_Tax_Seq
    WHERE 1 = 1
    {where_sql}
),
grouped AS (
    SELECT
        'overall' AS section,
        '전체' AS name,
        COUNT_BIG(*) AS row_count,
        SUM(qty) AS qty_sum,
        SUM(supply_amt) AS supply_sum,
        SUM(tax_amt) AS tax_sum,
        SUM(supply_amt + tax_amt) AS amount_sum,
        COUNT(DISTINCT vendor_cd) AS vendor_count,
        COUNT(DISTINCT product_cd) AS product_count,
        COUNT(DISTINCT stock_cd) AS stock_location_count
    FROM base

    UNION ALL

    SELECT
        'top_purchase_vendors' AS section,
        vendor_nm AS name,
        COUNT_BIG(*) AS row_count,
        SUM(qty) AS qty_sum,
        SUM(supply_amt) AS supply_sum,
        SUM(tax_amt) AS tax_sum,
        SUM(supply_amt + tax_amt) AS amount_sum,
        CAST(NULL AS int) AS vendor_count,
        CAST(NULL AS int) AS product_count,
        CAST(NULL AS int) AS stock_location_count
    FROM base
    GROUP BY vendor_nm

    UNION ALL

    SELECT
        'top_products' AS section,
        product_nm AS name,
        COUNT_BIG(*) AS row_count,
        SUM(qty) AS qty_sum,
        SUM(supply_amt) AS supply_sum,
        SUM(tax_amt) AS tax_sum,
        SUM(supply_amt + tax_amt) AS amount_sum,
        CAST(NULL AS int) AS vendor_count,
        CAST(NULL AS int) AS product_count,
        CAST(NULL AS int) AS stock_location_count
    FROM base
    GROUP BY product_nm

    UNION ALL

    SELECT
        'top_stock_locations' AS section,
        stock_nm AS name,
        COUNT_BIG(*) AS row_count,
        SUM(qty) AS qty_sum,
        SUM(supply_amt) AS supply_sum,
        SUM(tax_amt) AS tax_sum,
        SUM(supply_amt + tax_amt) AS amount_sum,
        CAST(NULL AS int) AS vendor_count,
        CAST(NULL AS int) AS product_count,
        CAST(NULL AS int) AS stock_location_count
    FROM base
    GROUP BY stock_nm
),
ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY section
            ORDER BY amount_sum DESC, qty_sum DESC, row_count DESC
        ) AS rn
    FROM grouped
)
SELECT
    section,
    name,
    row_count,
    qty_sum,
    supply_sum,
    tax_sum,
    amount_sum,
    vendor_count,
    product_count,
    stock_location_count
FROM ranked
WHERE section = 'overall'
   OR rn <= 10
ORDER BY
    CASE section
        WHEN 'overall' THEN 0
        WHEN 'top_purchase_vendors' THEN 1
        WHEN 'top_products' THEN 2
        WHEN 'top_stock_locations' THEN 3
        ELSE 9
    END,
    rn
"""
    df = query_to_df(sql, qparams)
    if df is None or df.empty:
        return {
            "row_count_total": 0,
            "row_count": 0,
            "top_purchase_vendors": [],
            "top_vendors": [],
            "top_products": [],
            "top_stock_locations": [],
        }

    overall_df = df[df["section"].astype(str) == "overall"]
    if overall_df.empty:
        return {"row_count_total": 0, "row_count": 0}

    overall = overall_df.iloc[0]

    top_purchase_vendors = _analysis_records_from_section_df(df, "top_purchase_vendors")
    top_products = _analysis_records_from_section_df(df, "top_products")
    top_stock_locations = _analysis_records_from_section_df(df, "top_stock_locations")

    return {
        "row_count_total": _analysis_scalar(overall, "row_count", 0),
        "row_count": _analysis_scalar(overall, "row_count", 0),
        "qty_sum": _analysis_scalar(overall, "qty_sum", 0.0),
        "supply_sum": _analysis_scalar(overall, "supply_sum", 0.0),
        "tax_sum": _analysis_scalar(overall, "tax_sum", 0.0),
        "amount_sum": _analysis_scalar(overall, "amount_sum", 0.0),
        "vendor_count": _analysis_scalar(overall, "vendor_count", 0),
        "product_count": _analysis_scalar(overall, "product_count", 0),
        "stock_location_count": _analysis_scalar(overall, "stock_location_count", 0),
        "top_purchase_vendors": top_purchase_vendors,
        "top_vendors": top_purchase_vendors,
        "top_products": top_products,
        "top_stock_locations": top_stock_locations,
    }