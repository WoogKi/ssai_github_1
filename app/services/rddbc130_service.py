# app/services/rddbc130_service.py
# 거래명세서 공통 조회를 위한 SQL WHERE 절을 생성하는 함수입니다.

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


TABLE = "rddbc130"
log = logging.getLogger("ssai.sims.rddbc130")

def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except Exception:
        return default


def _doc_query_top(params: Dict[str, Any], *, default: int = 200) -> int:
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


def _doc_export_top() -> int:
    """
    CSV/Excel 다운로드용 최대 건수.
    필요하면 .env에서 SIMS_EXPORT_MAX_ROWS=200000 처럼 조정.
    """
    return _env_int("SIMS_EXPORT_MAX_ROWS", 100000)


def _analysis_records_from_section_df(df, section: str) -> list[dict]:
    if df is None or df.empty or "section" not in df.columns:
        return []

    out: list[dict] = []
    try:
        sub = df[df["section"].astype(str) == section]
    except Exception:
        return []

    for _, row in sub.iterrows():
        name = str(row.get("name") or "").strip() or "(미지정)"

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
                "supply_sum": _num("supply_sum"),
                "tax_sum": _num("tax_sum"),
                "amount_sum": _num("amount_sum"),
                "dc_sum": _num("dc_sum"),
                "mismatch_count": int(_num("mismatch_count")),
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

# 거래명세서 공통 조회를 위한 SQL WHERE 절을 생성하는 함수입니다.
# 다양한 필터 조건을 적용하여, 거래명세서 데이터를 조회할 때 필요한 SQL WHERE 절을 동적으로 생성합니다.
def _base_filters(params: Dict[str, str]) -> str:
    clauses: list[str] = []

    clauses += make_date_filters("Trans_Books.Rd13_Trans_YyMmDd", params)

    if clean_text(params.get("trans_di")):
        add_filter(clauses, "Trans_Books.Rd13_Trans_Di = %(trans_di)s")

    if clean_text(params.get("ven_cd")):
        add_filter(clauses, "Trans_Books.Rd13_Ven_Cd = %(ven_cd)s")

    if like_value(params.get("ven_nm")):
        params["ven_nm_like"] = like_value(params.get("ven_nm"))
        add_filter(clauses, "Ven_Cd.Rd03_Ven_Nm LIKE %(ven_nm_like)s")

    if clean_text(params.get("trans_seq")):
        add_filter(clauses, "Trans_Books.Rd13_Trans_Seq = %(trans_seq)s")

    # 거래처그룹명
    if like_value(params.get("ven_group_nm")):
        params["ven_group_nm_like"] = like_value(params.get("ven_group_nm"))
        add_filter(
            clauses,
            """EXISTS (
                SELECT 1
                FROM dbo.Rddbc030 AS V
                INNER JOIN dbo.Rddbc010 AS G
                    ON V.Rd03_Ven_Group_Gcode = G.Rd01_Gcode
                   AND V.Rd03_Ven_Group = G.Rd01_Tcode
                WHERE V.Rd03_Ven_Cd = Trans_Books.Rd13_Ven_Cd
                  AND G.Rd01_Hnm LIKE %(ven_group_nm_like)s
            )""",
        )

    # 거래처종류명
    if like_value(params.get("ven_kind_nm")):
        params["ven_kind_nm_like"] = like_value(params.get("ven_kind_nm"))
        add_filter(
            clauses,
            """EXISTS (
                SELECT 1
                FROM dbo.Rddbc030 AS V
                INNER JOIN dbo.Rddbc010 AS K
                    ON V.Rd03_Ven_Kind_Gcode = K.Rd01_Gcode
                   AND V.Rd03_Ven_Kind = K.Rd01_Tcode
                WHERE V.Rd03_Ven_Cd = Trans_Books.Rd13_Ven_Cd
                  AND K.Rd01_Hnm LIKE %(ven_kind_nm_like)s
            )""",
        )

    # 입출고구분 앞자리
    io_gu_prefix = clean_text(params.get("io_gu_prefix"))
    if io_gu_prefix in {"0", "1", "2", "3", "4"}:
        add_filter(
            clauses,
            """EXISTS (
                SELECT 1
                FROM dbo.Rddbc110 AS D
                WHERE D.Rd11_Trans_Di = Trans_Books.Rd13_Trans_Di
                  AND D.Rd11_Trans_YyMmDd = Trans_Books.Rd13_Trans_YyMmDd
                  AND D.Rd11_Ven_Cd = Trans_Books.Rd13_Ven_Cd
                  AND D.Rd11_Trans_Seq = Trans_Books.Rd13_Trans_Seq
                  AND LEFT(D.Rd11_Io_Gu, 1) = %(io_gu_prefix)s
            )""",
        )
    elif io_gu_prefix in {"5", "6", "7", "8", "9"}:
        add_filter(
            clauses,
            """EXISTS (
                SELECT 1
                FROM dbo.Rddbc120 AS D
                WHERE D.Rd12_Trans_Di = Trans_Books.Rd13_Trans_Di
                  AND D.Rd12_Trans_YyMmDd = Trans_Books.Rd13_Trans_YyMmDd
                  AND D.Rd12_Ven_Cd = Trans_Books.Rd13_Ven_Cd
                  AND D.Rd12_Trans_Seq = Trans_Books.Rd13_Trans_Seq
                  AND LEFT(D.Rd12_Io_Gu, 1) = %(io_gu_prefix)s
            )""",
        )

    if clean_text(params.get("add_cd")):
        add_filter(clauses, "Trans_Books.Rd13_Add_Cd = %(add_cd)s")

    if like_value(params.get("add_nm")):
        params["add_nm_like"] = like_value(params.get("add_nm"))
        add_filter(clauses, "Add_Cd.Rd06_User_Nm LIKE %(add_nm_like)s")

    if clean_text(params.get("mod_cd")):
        add_filter(clauses, "Trans_Books.Rd13_Mod_Cd = %(mod_cd)s")

    if like_value(params.get("mod_nm")):
        params["mod_nm_like"] = like_value(params.get("mod_nm"))
        add_filter(clauses, "Mod_Cd.Rd06_User_Nm LIKE %(mod_nm_like)s")

    if clean_text(params.get("add_date_from")):
        add_filter(clauses, "Trans_Books.Rd13_Add_Date >= %(add_date_from)s")

    if clean_text(params.get("add_date_to")):
        add_filter(clauses, "Trans_Books.Rd13_Add_Date <= %(add_date_to)s")

    if clean_text(params.get("mod_date_from")):
        add_filter(clauses, "Trans_Books.Rd13_Mod_Date >= %(mod_date_from)s")

    if clean_text(params.get("mod_date_to")):
        add_filter(clauses, "Trans_Books.Rd13_Mod_Date <= %(mod_date_to)s")

    return ("\n      AND " + "\n      AND ".join(clauses)) if clauses else ""



def get_rddbc130_df(params: Optional[Dict[str, str]] = None):
    params = coalesce_params(params)
    params["top"] = _doc_query_top(params, default=200)

    where_sql = _base_filters(params)

    sql = f"""
WITH in_sum AS (
    SELECT
        Rd11_Trans_Di AS Trans_Di,
        Rd11_Trans_YyMmDd AS Trans_YyMmDd,
        Rd11_Ven_Cd AS Ven_Cd,
        Rd11_Trans_Seq AS Trans_Seq,
        SUM(COALESCE(Rd11_Fin_Supply_Price, Rd11_Supply_Price, 0)) AS Sum_Supply,
        SUM(COALESCE(Rd11_Fin_Tax_Price, Rd11_Tax_Price, 0)) AS Sum_Tax
    FROM dbo.Rddbc110
    WHERE NULLIF(LTRIM(RTRIM(Rd11_Trans_Seq)), '') IS NOT NULL
    GROUP BY Rd11_Trans_Di, Rd11_Trans_YyMmDd, Rd11_Ven_Cd, Rd11_Trans_Seq
),
out_sum AS (
    SELECT
        Rd12_Trans_Di AS Trans_Di,
        Rd12_Trans_YyMmDd AS Trans_YyMmDd,
        Rd12_Ven_Cd AS Ven_Cd,
        Rd12_Trans_Seq AS Trans_Seq,
        SUM(COALESCE(Rd12_Fin_Supply_Price, Rd12_Supply_Price, 0)) AS Sum_Supply,
        SUM(COALESCE(Rd12_Fin_Tax_Price, Rd12_Tax_Price, 0)) AS Sum_Tax
    FROM dbo.Rddbc120
    WHERE NULLIF(LTRIM(RTRIM(Rd12_Trans_Seq)), '') IS NOT NULL
    GROUP BY Rd12_Trans_Di, Rd12_Trans_YyMmDd, Rd12_Ven_Cd, Rd12_Trans_Seq
)
SELECT TOP (%(top)s)
    Trans_Books.Rd13_Trans_Di,
    CASE
        WHEN I.Trans_Di IS NOT NULL THEN '입고'
        WHEN O.Trans_Di IS NOT NULL THEN '출고'
        ELSE '기타'
    END AS 거래명세서구분명,
    Trans_Books.Rd13_Trans_YyMmDd,
    Trans_Books.Rd13_Ven_Cd,
    Ven_Cd.Rd03_Ven_Nm AS 거래처명,
    Trans_Books.Rd13_Trans_Seq,
    Trans_Books.Rd13_Supply_Price,
    Trans_Books.Rd13_Tax_Price,
    Trans_Books.Rd13_Tot_Amt,
    Trans_Books.Rd13_Dc_Amt,
    CASE
        WHEN I.Trans_Di IS NOT NULL THEN I.Sum_Supply
        WHEN O.Trans_Di IS NOT NULL THEN O.Sum_Supply
        ELSE NULL
    END AS 상세합_공급가액,
    CASE
        WHEN I.Trans_Di IS NOT NULL THEN I.Sum_Tax
        WHEN O.Trans_Di IS NOT NULL THEN O.Sum_Tax
        ELSE NULL
    END AS 상세합_세액,
    CASE
        WHEN I.Trans_Di IS NOT NULL
            AND COALESCE(I.Sum_Supply, 0) = COALESCE(Trans_Books.Rd13_Supply_Price, 0)
            AND COALESCE(I.Sum_Tax, 0) = COALESCE(Trans_Books.Rd13_Tax_Price, 0)
            THEN 'Y'
        WHEN O.Trans_Di IS NOT NULL
            AND COALESCE(O.Sum_Supply, 0) = COALESCE(Trans_Books.Rd13_Supply_Price, 0)
            AND COALESCE(O.Sum_Tax, 0) = COALESCE(Trans_Books.Rd13_Tax_Price, 0)
            THEN 'Y'
        WHEN I.Trans_Di IS NOT NULL OR O.Trans_Di IS NOT NULL THEN 'N'
        ELSE NULL
    END AS 상세합계일치,
    Trans_Books.Rd13_Slip_Gubun,
    Trans_Books.Rd13_Slip_YyMmDd,
    Trans_Books.Rd13_Slip_No,
    Trans_Books.Rd13_Slip_Seq,
    Trans_Books.Rd13_Other,
    Trans_Books.Rd13_Confirm_Check,
    Trans_Books.Rd13_Add_Date,
    Trans_Books.Rd13_Add_Cd,
    Add_Cd.Rd06_User_Nm AS 등록자명,
    Trans_Books.Rd13_Mod_Date,
    Trans_Books.Rd13_Mod_Cd,
    Mod_Cd.Rd06_User_Nm AS 수정자명,
    Trans_Books.Rd13_Delivery_Di_Gcode,
    Trans_Books.Rd13_Delivery_Di,
    Delivery_Di.Rd01_Hnm AS 배송구분,
    Trans_Books.Rd13_Print_Seq,
    Trans_Books.Rd13_Confirm_Check2,
    Trans_Books.Rd13_Confirm_Check3,
    Trans_Books.Rd13_Delivery_Pill,
    Trans_Books.Rd13_Delivery_Aque,
    Trans_Books.Rd13_Delivery_Refriger,
    Trans_Books.Rd13_Delivery_Change,
    Trans_Books.Rd13_Delivery_PrinterSeq,
    Trans_Books.Rd13_Delivery_Confirm,
    Trans_Books.Rd13_Sales_Man_Seq,
    Trans_Books.Rd13_Picking_PrinterSeq,
    Trans_Books.Rd13_Delivery_Degree,
    Trans_Books.Rd13_Add_Time,
    Trans_Books.Rd13_Mod_Time,
    Trans_Books.Rd13_PICKING_PRINTERSEQ_CS,
    Trans_Books.Rd13_LABEL_PRINT_CNT
FROM dbo.Rddbc130 AS Trans_Books
LEFT JOIN dbo.Rddbc060 AS Add_Cd
    ON Trans_Books.Rd13_Add_Cd = Add_Cd.Rd06_User_Cd
LEFT JOIN dbo.Rddbc060 AS Mod_Cd
    ON Trans_Books.Rd13_Mod_Cd = Mod_Cd.Rd06_User_Cd
LEFT JOIN dbo.Rddbc030 AS Ven_Cd
    ON Trans_Books.Rd13_Ven_Cd = Ven_Cd.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc010 AS Delivery_Di
    ON Trans_Books.Rd13_Delivery_Di_Gcode = Delivery_Di.Rd01_Gcode
   AND Trans_Books.Rd13_Delivery_Di = Delivery_Di.Rd01_Tcode
LEFT JOIN in_sum AS I
    ON Trans_Books.Rd13_Trans_Di = I.Trans_Di
   AND Trans_Books.Rd13_Trans_YyMmDd = I.Trans_YyMmDd
   AND Trans_Books.Rd13_Ven_Cd = I.Ven_Cd
   AND Trans_Books.Rd13_Trans_Seq = I.Trans_Seq
LEFT JOIN out_sum AS O
    ON Trans_Books.Rd13_Trans_Di = O.Trans_Di
   AND Trans_Books.Rd13_Trans_YyMmDd = O.Trans_YyMmDd
   AND Trans_Books.Rd13_Ven_Cd = O.Ven_Cd
   AND Trans_Books.Rd13_Trans_Seq = O.Trans_Seq
WHERE 1 = 1
{where_sql}
ORDER BY Trans_Books.Rd13_Trans_YyMmDd DESC, Trans_Books.Rd13_Ven_Cd, Trans_Books.Rd13_Trans_Seq DESC
"""
    df = query_to_df(sql, params)

    if clean_text(params.get("only_mismatch")).upper() in {"Y", "1", "TRUE"}:
        df = df[df["상세합계일치"].fillna("N") != "Y"]

    return df

# 거래명세서 공통 조회 결과를 반환하는 함수입니다.
# 입력된 필터 조건에 따라 거래명세서 데이터를 조회하고, 결과를 데이터프레임으로 반환합니다. 조회된 데이터가 없을 경우, 적절한 메시지와 함께 결과를 반환합니다.
def get_rddbc130_result(params: Optional[Dict[str, Any]] = None):
    params = coalesce_params(params)
    df = get_rddbc130_df(params)

    row_count = 0 if df is None else int(len(df))
    log.info("DBG get_rddbc130_result rows=%s", row_count)

    if row_count == 0:
        return {
            "table": TABLE,
            "title": "거래명세서 공통 조회",
            "action": "거래명세서 공통 조회",
            "params": params,
            "data": "조회 결과가 없습니다.",
            "message": "조회 결과가 없습니다.",
            "final": True,
            "meta": {"row_count": 0},
        }

    return build_result_payload(
        table=TABLE,
        title="거래명세서 공통 조회",
        action="거래명세서 공통 조회",
        params=params,
        df=df,
        message=f"거래명세서 공통 {row_count:,}건",
    )

# 거래명세서 공통 조회 결과를 CSV/Excel 다운로드용 데이터프레임으로 반환하는 함수입니다.
def get_rddbc130_export_df(params: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    """
    거래명세서 공통 CSV/Excel 다운로드용 전체 상세 DataFrame.

    화면 조회 TOP 200과 분리한다.
    단, 무제한은 위험하므로 SIMS_EXPORT_MAX_ROWS 기본 100000건까지 허용한다.
    """
    qparams = coalesce_params(params)
    export_top = _doc_export_top()

    qparams["top"] = export_top
    qparams["_max_top"] = export_top

    df = get_rddbc130_df(qparams)

    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame() if df is None else df

    try:
        payload = build_result_payload(
            table=TABLE,
            title="거래명세서 공통 조회",
            action="거래명세서 공통 조회",
            params=qparams,
            df=df,
            message=f"거래명세서 공통 다운로드 {len(df):,}건",
        )
        out = payload.get("df_display")
        if isinstance(out, pd.DataFrame):
            return out
    except Exception:
        log.exception("get_rddbc130_export_df label/apply failed")

    return df

# 거래명세서 공통 LLM 분석용 전체 집계 결과를 반환하는 함수입니다.
def get_rddbc130_analysis_summary(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    거래명세서 공통 LLM 분석용 전체 집계.

    화면 조회 TOP 200과 분리한다.
    동일 조회조건 전체 기준으로 건수/금액/거래처별/구분별/일치여부별 집계를 만든다.
    """
    qparams = coalesce_params(dict(params or {}))
    where_sql = _base_filters(qparams)

    only_mismatch = clean_text(qparams.get("only_mismatch")).upper() in {"Y", "1", "TRUE"}
    mismatch_where = "WHERE COALESCE(detail_match, 'N') <> 'Y'" if only_mismatch else ""

    sql = f"""
WITH in_sum AS (
    SELECT
        Rd11_Trans_Di AS Trans_Di,
        Rd11_Trans_YyMmDd AS Trans_YyMmDd,
        Rd11_Ven_Cd AS Ven_Cd,
        Rd11_Trans_Seq AS Trans_Seq,
        SUM(COALESCE(Rd11_Fin_Supply_Price, Rd11_Supply_Price, 0)) AS Sum_Supply,
        SUM(COALESCE(Rd11_Fin_Tax_Price, Rd11_Tax_Price, 0)) AS Sum_Tax
    FROM dbo.Rddbc110
    WHERE NULLIF(LTRIM(RTRIM(Rd11_Trans_Seq)), '') IS NOT NULL
    GROUP BY Rd11_Trans_Di, Rd11_Trans_YyMmDd, Rd11_Ven_Cd, Rd11_Trans_Seq
),
out_sum AS (
    SELECT
        Rd12_Trans_Di AS Trans_Di,
        Rd12_Trans_YyMmDd AS Trans_YyMmDd,
        Rd12_Ven_Cd AS Ven_Cd,
        Rd12_Trans_Seq AS Trans_Seq,
        SUM(COALESCE(Rd12_Fin_Supply_Price, Rd12_Supply_Price, 0)) AS Sum_Supply,
        SUM(COALESCE(Rd12_Fin_Tax_Price, Rd12_Tax_Price, 0)) AS Sum_Tax
    FROM dbo.Rddbc120
    WHERE NULLIF(LTRIM(RTRIM(Rd12_Trans_Seq)), '') IS NOT NULL
    GROUP BY Rd12_Trans_Di, Rd12_Trans_YyMmDd, Rd12_Ven_Cd, Rd12_Trans_Seq
),
base AS (
    SELECT
        Trans_Books.Rd13_Trans_Di AS trans_di,
        CASE
            WHEN Trans_Books.Rd13_Trans_Di = '1' THEN '매입분'
            WHEN Trans_Books.Rd13_Trans_Di = '3' THEN '매출분'
            ELSE COALESCE(NULLIF(LTRIM(RTRIM(Trans_Books.Rd13_Trans_Di)), ''), '기타')
        END AS trans_di_nm,
        Trans_Books.Rd13_Trans_YyMmDd AS trans_date,
        NULLIF(LTRIM(RTRIM(Trans_Books.Rd13_Ven_Cd)), '') AS vendor_cd,
        COALESCE(NULLIF(LTRIM(RTRIM(Ven_Cd.Rd03_Ven_Nm)), ''), '(미지정)') AS vendor_nm,
        COALESCE(NULLIF(LTRIM(RTRIM(Delivery_Di.Rd01_Hnm)), ''), '(미지정)') AS delivery_nm,
        CAST(COALESCE(Trans_Books.Rd13_Supply_Price, 0) AS float) AS supply_amt,
        CAST(COALESCE(Trans_Books.Rd13_Tax_Price, 0) AS float) AS tax_amt,
        CAST(COALESCE(Trans_Books.Rd13_Tot_Amt, 0) AS float) AS amount_amt,
        CAST(COALESCE(Trans_Books.Rd13_Dc_Amt, 0) AS float) AS dc_amt,
        CASE
            WHEN I.Trans_Di IS NOT NULL THEN I.Sum_Supply
            WHEN O.Trans_Di IS NOT NULL THEN O.Sum_Supply
            ELSE NULL
        END AS detail_supply_amt,
        CASE
            WHEN I.Trans_Di IS NOT NULL THEN I.Sum_Tax
            WHEN O.Trans_Di IS NOT NULL THEN O.Sum_Tax
            ELSE NULL
        END AS detail_tax_amt,
        CASE
            WHEN I.Trans_Di IS NOT NULL
                AND COALESCE(I.Sum_Supply, 0) = COALESCE(Trans_Books.Rd13_Supply_Price, 0)
                AND COALESCE(I.Sum_Tax, 0) = COALESCE(Trans_Books.Rd13_Tax_Price, 0)
                THEN 'Y'
            WHEN O.Trans_Di IS NOT NULL
                AND COALESCE(O.Sum_Supply, 0) = COALESCE(Trans_Books.Rd13_Supply_Price, 0)
                AND COALESCE(O.Sum_Tax, 0) = COALESCE(Trans_Books.Rd13_Tax_Price, 0)
                THEN 'Y'
            WHEN I.Trans_Di IS NOT NULL OR O.Trans_Di IS NOT NULL THEN 'N'
            ELSE NULL
        END AS detail_match
    FROM dbo.Rddbc130 AS Trans_Books
    LEFT JOIN dbo.Rddbc060 AS Add_Cd
        ON Trans_Books.Rd13_Add_Cd = Add_Cd.Rd06_User_Cd
    LEFT JOIN dbo.Rddbc060 AS Mod_Cd
        ON Trans_Books.Rd13_Mod_Cd = Mod_Cd.Rd06_User_Cd
    LEFT JOIN dbo.Rddbc030 AS Ven_Cd
        ON Trans_Books.Rd13_Ven_Cd = Ven_Cd.Rd03_Ven_Cd
    LEFT JOIN dbo.Rddbc010 AS Delivery_Di
        ON Trans_Books.Rd13_Delivery_Di_Gcode = Delivery_Di.Rd01_Gcode
       AND Trans_Books.Rd13_Delivery_Di = Delivery_Di.Rd01_Tcode
    LEFT JOIN in_sum AS I
        ON Trans_Books.Rd13_Trans_Di = I.Trans_Di
       AND Trans_Books.Rd13_Trans_YyMmDd = I.Trans_YyMmDd
       AND Trans_Books.Rd13_Ven_Cd = I.Ven_Cd
       AND Trans_Books.Rd13_Trans_Seq = I.Trans_Seq
    LEFT JOIN out_sum AS O
        ON Trans_Books.Rd13_Trans_Di = O.Trans_Di
       AND Trans_Books.Rd13_Trans_YyMmDd = O.Trans_YyMmDd
       AND Trans_Books.Rd13_Ven_Cd = O.Ven_Cd
       AND Trans_Books.Rd13_Trans_Seq = O.Trans_Seq
    WHERE 1 = 1
    {where_sql}
),
filtered AS (
    SELECT *
    FROM base
    {mismatch_where}
),
grouped AS (
    SELECT
        'overall' AS section,
        '전체' AS name,
        COUNT_BIG(*) AS row_count,
        SUM(supply_amt) AS supply_sum,
        SUM(tax_amt) AS tax_sum,
        SUM(amount_amt) AS amount_sum,
        SUM(dc_amt) AS dc_sum,
        SUM(CASE WHEN COALESCE(detail_match, 'N') <> 'Y' THEN 1 ELSE 0 END) AS mismatch_count,
        COUNT(DISTINCT vendor_cd) AS vendor_count
    FROM filtered

    UNION ALL

    SELECT
        'by_trans_type',
        trans_di_nm,
        COUNT_BIG(*),
        SUM(supply_amt),
        SUM(tax_amt),
        SUM(amount_amt),
        SUM(dc_amt),
        SUM(CASE WHEN COALESCE(detail_match, 'N') <> 'Y' THEN 1 ELSE 0 END),
        CAST(NULL AS int)
    FROM filtered
    GROUP BY trans_di_nm

    UNION ALL

    SELECT
        'top_vendors',
        vendor_nm,
        COUNT_BIG(*),
        SUM(supply_amt),
        SUM(tax_amt),
        SUM(amount_amt),
        SUM(dc_amt),
        SUM(CASE WHEN COALESCE(detail_match, 'N') <> 'Y' THEN 1 ELSE 0 END),
        CAST(NULL AS int)
    FROM filtered
    GROUP BY vendor_nm

    UNION ALL

    SELECT
        'by_match_status',
        CASE
            WHEN detail_match = 'Y' THEN '상세합계 일치'
            WHEN detail_match = 'N' THEN '상세합계 불일치'
            ELSE '상세 없음'
        END,
        COUNT_BIG(*),
        SUM(supply_amt),
        SUM(tax_amt),
        SUM(amount_amt),
        SUM(dc_amt),
        SUM(CASE WHEN COALESCE(detail_match, 'N') <> 'Y' THEN 1 ELSE 0 END),
        CAST(NULL AS int)
    FROM filtered
    GROUP BY
        CASE
            WHEN detail_match = 'Y' THEN '상세합계 일치'
            WHEN detail_match = 'N' THEN '상세합계 불일치'
            ELSE '상세 없음'
        END

    UNION ALL

    SELECT
        'by_delivery',
        delivery_nm,
        COUNT_BIG(*),
        SUM(supply_amt),
        SUM(tax_amt),
        SUM(amount_amt),
        SUM(dc_amt),
        SUM(CASE WHEN COALESCE(detail_match, 'N') <> 'Y' THEN 1 ELSE 0 END),
        CAST(NULL AS int)
    FROM filtered
    GROUP BY delivery_nm
),
ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY section
            ORDER BY amount_sum DESC, row_count DESC
        ) AS rn
    FROM grouped
)
SELECT
    section,
    name,
    row_count,
    supply_sum,
    tax_sum,
    amount_sum,
    dc_sum,
    mismatch_count,
    vendor_count
FROM ranked
WHERE section = 'overall'
   OR rn <= 10
ORDER BY
    CASE section
        WHEN 'overall' THEN 0
        WHEN 'by_trans_type' THEN 1
        WHEN 'top_vendors' THEN 2
        WHEN 'by_match_status' THEN 3
        WHEN 'by_delivery' THEN 4
        ELSE 9
    END,
    rn
"""
    df = query_to_df(sql, qparams)
    if df is None or df.empty:
        return {
            "row_count_total": 0,
            "row_count": 0,
            "top_vendors": [],
            "by_trans_type": [],
            "by_match_status": [],
            "by_delivery": [],
        }

    overall_df = df[df["section"].astype(str) == "overall"]
    if overall_df.empty:
        return {"row_count_total": 0, "row_count": 0}

    overall = overall_df.iloc[0]

    return {
        "row_count_total": _analysis_scalar(overall, "row_count", 0),
        "row_count": _analysis_scalar(overall, "row_count", 0),
        "supply_sum": _analysis_scalar(overall, "supply_sum", 0.0),
        "tax_sum": _analysis_scalar(overall, "tax_sum", 0.0),
        "amount_sum": _analysis_scalar(overall, "amount_sum", 0.0),
        "dc_sum": _analysis_scalar(overall, "dc_sum", 0.0),
        "mismatch_count": _analysis_scalar(overall, "mismatch_count", 0),
        "vendor_count": _analysis_scalar(overall, "vendor_count", 0),
        "by_trans_type": _analysis_records_from_section_df(df, "by_trans_type"),
        "top_vendors": _analysis_records_from_section_df(df, "top_vendors"),
        "by_match_status": _analysis_records_from_section_df(df, "by_match_status"),
        "by_delivery": _analysis_records_from_section_df(df, "by_delivery"),
    }