# app/services/rddbc140_service.py
# 세금계산서
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


TABLE = "rddbc140"
log = logging.getLogger("ssai.sims.rddbc140")

def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except Exception:
        return default


def _tax_query_top(params: Dict[str, Any], *, default: int = 200) -> int:
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


def _tax_export_top() -> int:
    """
    CSV/Excel 다운로드용 최대 건수.
    필요하면 .env에서 SIMS_EXPORT_MAX_ROWS=200000 처럼 조정.
    """
    return _env_int("SIMS_EXPORT_MAX_ROWS", 100000)


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
                "mismatch_count": int(_num("mismatch_count")),
                "detail_missing_count": int(_num("detail_missing_count")),
                "accounting_count": int(_num("accounting_count")),
            }
        )

    return out


def _base_filters(params: Dict[str, str]) -> str:
    clauses: list[str] = []

    clauses += make_date_filters("Tax_Books.Rd14_Tax_YyMmDd", params)

    if clean_text(params.get("tax_di")):
        add_filter(clauses, "Tax_Books.Rd14_Tax_Di = %(tax_di)s")

    if clean_text(params.get("ven_cd")):
        add_filter(clauses, "Tax_Books.Rd14_Ven_Cd = %(ven_cd)s")

    if like_value(params.get("ven_nm")):
        params["ven_nm_like"] = like_value(params.get("ven_nm"))
        add_filter(clauses, "Ven_Cd.Rd03_Ven_Nm LIKE %(ven_nm_like)s")

    if clean_text(params.get("tax_seq")):
        add_filter(clauses, "Tax_Books.Rd14_Tax_Seq = %(tax_seq)s")

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
                WHERE V.Rd03_Ven_Cd = Tax_Books.Rd14_Ven_Cd
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
                WHERE V.Rd03_Ven_Cd = Tax_Books.Rd14_Ven_Cd
                  AND K.Rd01_Hnm LIKE %(ven_kind_nm_like)s
            )""",
        )

    # 입출고구분 앞자리
    io_gu_prefix = clean_text(params.get("io_gu_prefix"))

    # 매입 / 회계매입 -> 입고계열
    if io_gu_prefix in {"0", "1", "2", "3", "4"}:
        add_filter(
            clauses,
            """EXISTS (
                SELECT 1
                FROM dbo.Rddbc110 AS D
                WHERE D.Rd11_Tax_Di = Tax_Books.Rd14_Tax_Di
                  AND D.Rd11_Tax_YyMmDd = Tax_Books.Rd14_Tax_YyMmDd
                  AND D.Rd11_Ven_Cd = Tax_Books.Rd14_Ven_Cd
                  AND D.Rd11_Tax_Seq = Tax_Books.Rd14_Tax_Seq
                  AND LEFT(D.Rd11_Io_Gu, 1) = %(io_gu_prefix)s
            )""",
        )

    # 매출 / 회계매출 -> 출고계열
    elif io_gu_prefix in {"5", "6", "7", "8", "9"}:
        add_filter(
            clauses,
            """EXISTS (
                SELECT 1
                FROM dbo.Rddbc120 AS D
                WHERE D.Rd12_Tax_Di = Tax_Books.Rd14_Tax_Di
                  AND D.Rd12_Tax_YyMmDd = Tax_Books.Rd14_Tax_YyMmDd
                  AND D.Rd12_Ven_Cd = Tax_Books.Rd14_Ven_Cd
                  AND D.Rd12_Tax_Seq = Tax_Books.Rd14_Tax_Seq
                  AND LEFT(D.Rd12_Io_Gu, 1) = %(io_gu_prefix)s
            )""",
        )

    if clean_text(params.get("add_cd")):
        add_filter(clauses, "Tax_Books.Rd14_Add_Cd = %(add_cd)s")

    if like_value(params.get("add_nm")):
        params["add_nm_like"] = like_value(params.get("add_nm"))
        add_filter(clauses, "Add_Cd.Rd06_User_Nm LIKE %(add_nm_like)s")

    if clean_text(params.get("mod_cd")):
        add_filter(clauses, "Tax_Books.Rd14_Mod_Cd = %(mod_cd)s")

    if like_value(params.get("mod_nm")):
        params["mod_nm_like"] = like_value(params.get("mod_nm"))
        add_filter(clauses, "Mod_Cd.Rd06_User_Nm LIKE %(mod_nm_like)s")

    if clean_text(params.get("add_date_from")):
        add_filter(clauses, "Tax_Books.Rd14_Add_Date >= %(add_date_from)s")

    if clean_text(params.get("add_date_to")):
        add_filter(clauses, "Tax_Books.Rd14_Add_Date <= %(add_date_to)s")

    if clean_text(params.get("mod_date_from")):
        add_filter(clauses, "Tax_Books.Rd14_Mod_Date >= %(mod_date_from)s")

    if clean_text(params.get("mod_date_to")):
        add_filter(clauses, "Tax_Books.Rd14_Mod_Date <= %(mod_date_to)s")

    return ("\n      AND " + "\n      AND ".join(clauses)) if clauses else ""


def get_rddbc140_df(params: Optional[Dict[str, str]] = None):
    params = coalesce_params(params)
    params["top"] = _tax_query_top(params, default=200)
    where_sql = _base_filters(params)

    sql = f"""
WITH in_sum AS (
    SELECT
        Rd11_Tax_Di AS Tax_Di,
        Rd11_Tax_YyMmDd AS Tax_YyMmDd,
        Rd11_Ven_Cd AS Ven_Cd,
        Rd11_Tax_Seq AS Tax_Seq,
        SUM(COALESCE(Rd11_Fin_Supply_Price, Rd11_Supply_Price, 0)) AS Sum_Supply,
        SUM(COALESCE(Rd11_Fin_Tax_Price, Rd11_Tax_Price, 0)) AS Sum_Tax
    FROM dbo.Rddbc110
    WHERE NULLIF(LTRIM(RTRIM(Rd11_Tax_Seq)), '') IS NOT NULL
    GROUP BY Rd11_Tax_Di, Rd11_Tax_YyMmDd, Rd11_Ven_Cd, Rd11_Tax_Seq
),
out_sum AS (
    SELECT
        Rd12_Tax_Di AS Tax_Di,
        Rd12_Tax_YyMmDd AS Tax_YyMmDd,
        Rd12_Ven_Cd AS Ven_Cd,
        Rd12_Tax_Seq AS Tax_Seq,
        SUM(COALESCE(Rd12_Fin_Supply_Price, Rd12_Supply_Price, 0)) AS Sum_Supply,
        SUM(COALESCE(Rd12_Fin_Tax_Price, Rd12_Tax_Price, 0)) AS Sum_Tax
    FROM dbo.Rddbc120
    WHERE NULLIF(LTRIM(RTRIM(Rd12_Tax_Seq)), '') IS NOT NULL
    GROUP BY Rd12_Tax_Di, Rd12_Tax_YyMmDd, Rd12_Ven_Cd, Rd12_Tax_Seq
)
SELECT TOP (%(top)s)
    Tax_Books.Rd14_Tax_Di,
    CASE
        WHEN Tax_Books.Rd14_Tax_Di = '1' THEN '매입'
        WHEN Tax_Books.Rd14_Tax_Di = '2' THEN '회계매입'
        WHEN Tax_Books.Rd14_Tax_Di = '3' THEN '매출'
        WHEN Tax_Books.Rd14_Tax_Di = '4' THEN '회계매출'
        ELSE '기타'
    END AS 매입매출구분,

    Tax_Books.Rd14_Tax_YyMmDd,
    Tax_Books.Rd14_Ven_Cd,
    Ven_Cd.Rd03_Ven_Nm AS 거래처명,
    Tax_Books.Rd14_Tax_Seq,

    Tax_Books.Rd14_Tax_Di_Gcode,
    Tax_Di.Rd01_Hnm AS 세금계산서구분명,

    Tax_Books.Rd14_Ven_Num,
    Tax_Books.Rd14_Supply_Price,
    Tax_Books.Rd14_Tax_Price,
    Tax_Books.Rd14_Tot_Amt,

    CASE
        WHEN I.Tax_Di IS NOT NULL THEN I.Sum_Supply
        WHEN O.Tax_Di IS NOT NULL THEN O.Sum_Supply
        ELSE NULL
    END AS 상세합_공급가액,
    CASE
        WHEN I.Tax_Di IS NOT NULL THEN I.Sum_Tax
        WHEN O.Tax_Di IS NOT NULL THEN O.Sum_Tax
        ELSE NULL
    END AS 상세합_세액,

    CASE
        WHEN I.Tax_Di IS NOT NULL
            AND COALESCE(I.Sum_Supply, 0) = COALESCE(Tax_Books.Rd14_Supply_Price, 0)
            AND COALESCE(I.Sum_Tax, 0) = COALESCE(Tax_Books.Rd14_Tax_Price, 0)
            THEN 'Y'
        WHEN O.Tax_Di IS NOT NULL
            AND COALESCE(O.Sum_Supply, 0) = COALESCE(Tax_Books.Rd14_Supply_Price, 0)
            AND COALESCE(O.Sum_Tax, 0) = COALESCE(Tax_Books.Rd14_Tax_Price, 0)
            THEN 'Y'
        WHEN I.Tax_Di IS NOT NULL OR O.Tax_Di IS NOT NULL THEN 'N'
        ELSE NULL
    END AS 상세합계일치,

    Tax_Books.Rd14_Space_Cnt,

    Tax_Books.Rd14_Ym1,
    Tax_Books.Rd14_Item1,
    Tax_Books.Rd14_Standard1,
    Tax_Books.Rd14_Qty1,
    Tax_Books.Rd14_Unit_Cost1,
    Tax_Books.Rd14_Supply_Price1,
    Tax_Books.Rd14_Tax_Price1,

    Tax_Books.Rd14_Ym2,
    Tax_Books.Rd14_Item2,
    Tax_Books.Rd14_Standard2,
    Tax_Books.Rd14_Qty2,
    Tax_Books.Rd14_Unit_Cost2,
    Tax_Books.Rd14_Supply_Price2,
    Tax_Books.Rd14_Tax_Price2,

    Tax_Books.Rd14_Ym3,
    Tax_Books.Rd14_Item3,
    Tax_Books.Rd14_Standard3,
    Tax_Books.Rd14_Qty3,
    Tax_Books.Rd14_Unit_Cost3,
    Tax_Books.Rd14_Supply_Price3,
    Tax_Books.Rd14_Tax_Price3,

    Tax_Books.Rd14_Ym4,
    Tax_Books.Rd14_Item4,
    Tax_Books.Rd14_Standard4,
    Tax_Books.Rd14_Qty4,
    Tax_Books.Rd14_Unit_Cost4,
    Tax_Books.Rd14_Supply_Price4,
    Tax_Books.Rd14_Tax_Price4,

    Tax_Books.Rd14_Cash_Amt,
    Tax_Books.Rd14_Note_Amt,
    Tax_Books.Rd14_Bill_Amt,
    Tax_Books.Rd14_Credit_Amt,

    Tax_Books.Rd14_Tax_Gu_Gcode,
    Tax_Books.Rd14_Tax_Gu,
    Tax_Books.Rd14_Process_Gu,

    Tax_Books.Rd14_Slip_Gubun,
    Tax_Books.Rd14_Slip_YyMmDd,
    Tax_Books.Rd14_Slip_Yy,
    Tax_Books.Rd14_Slip_Mm,
    Tax_Books.Rd14_Slip_Dd,
    Tax_Books.Rd14_Slip_No,
    Tax_Books.Rd14_Slip_Seq,

    Tax_Books.Rd14_Add_Date,
    Tax_Books.Rd14_Add_Cd,
    Add_Cd.Rd06_User_Nm AS 등록자명,

    Tax_Books.Rd14_Mod_Date,
    Tax_Books.Rd14_Mod_Cd,
    Mod_Cd.Rd06_User_Nm AS 수정자명,

    Tax_Books.Rd14_Print_Seq,
    Tax_Books.Rd14_Tax_Pair,
    Tax_Books.Rd14_Move_Seq,
    Tax_Books.Rd14_Move_Date,
    Tax_Books.Rd14_Credit_No,
    Tax_Books.Rd14_E_Tax_Gu,
    Tax_Books.Rd14_Remark,
    Tax_Books.Rd14_Tax_Bill_Gcode,
    Tax_Books.Rd14_Tax_Bill,
    Tax_Books.Rd14_Mod_Gu,
    Tax_Books.Rd14_Print_Other,
    Tax_Books.Rd14_Report_Date,
    Tax_Books.Rd14_Ven_Num_Sub,
    Tax_Books.Rd14_Sup_Ven_Num_Sub,
    Tax_Books.Rd14_Tax_Sub_Di_Gcode,
    Tax_Books.Rd14_Tax_Sub_Di

FROM dbo.Rddbc140 AS Tax_Books
LEFT JOIN dbo.Rddbc060 AS Add_Cd
    ON Tax_Books.Rd14_Add_Cd = Add_Cd.Rd06_User_Cd
LEFT JOIN dbo.Rddbc060 AS Mod_Cd
    ON Tax_Books.Rd14_Mod_Cd = Mod_Cd.Rd06_User_Cd
LEFT JOIN dbo.Rddbc030 AS Ven_Cd
    ON Tax_Books.Rd14_Ven_Cd = Ven_Cd.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc010 AS Tax_Di
    ON Tax_Books.Rd14_Tax_Di_Gcode = Tax_Di.Rd01_Gcode
   AND Tax_Books.Rd14_Tax_Di       = Tax_Di.Rd01_Tcode
LEFT JOIN in_sum AS I
    ON Tax_Books.Rd14_Tax_Di       = '1'
   AND Tax_Books.Rd14_Tax_YyMmDd   = I.Tax_YyMmDd
   AND Tax_Books.Rd14_Ven_Cd       = I.Ven_Cd
   AND Tax_Books.Rd14_Tax_Seq      = I.Tax_Seq
LEFT JOIN out_sum AS O
    ON Tax_Books.Rd14_Tax_Di       = '3'
   AND Tax_Books.Rd14_Tax_YyMmDd   = O.Tax_YyMmDd
   AND Tax_Books.Rd14_Ven_Cd       = O.Ven_Cd
   AND Tax_Books.Rd14_Tax_Seq      = O.Tax_Seq
WHERE 1 = 1
{where_sql}
ORDER BY Tax_Books.Rd14_Tax_YyMmDd DESC, Tax_Books.Rd14_Ven_Cd, Tax_Books.Rd14_Tax_Seq DESC
"""
    df = query_to_df(sql, params)

    if clean_text(params.get("only_mismatch")).upper() in {"Y", "1", "TRUE"}:
        df = df[df["상세합계일치"].fillna("N") != "Y"]

    return df


def get_rddbc140_result(params: Optional[Dict[str, Any]] = None):
    params = coalesce_params(params)
    df = get_rddbc140_df(params)

    row_count = 0 if df is None else int(len(df))
    log.info("DBG get_rddbc140_result rows=%s", row_count)

    if row_count == 0:
        return {
            "table": TABLE,
            "title": "세금계산서 공통 조회",
            "action": "세금계산서 공통 조회",
            "params": params,
            "data": "조회 결과가 없습니다.",
            "message": "조회 결과가 없습니다.",
            "final": True,
            "meta": {"row_count": 0},
        }

    return build_result_payload(
        table=TABLE,
        title="세금계산서 공통 조회",
        action="세금계산서 공통 조회",
        params=params,
        df=df,
        message=f"세금계산서 공통 {row_count:,}건",
    )

def get_rddbc140_export_df(params: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    """
    세금계산서 공통 CSV/Excel 다운로드용 전체 상세 DataFrame.

    화면 조회 TOP 200과 분리한다.
    단, 무제한은 위험하므로 SIMS_EXPORT_MAX_ROWS 기본 100000건까지 허용한다.
    """
    qparams = coalesce_params(params)
    export_top = _tax_export_top()

    qparams["top"] = export_top
    qparams["_max_top"] = export_top

    df = get_rddbc140_df(qparams)

    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame() if df is None else df

    try:
        payload = build_result_payload(
            table=TABLE,
            title="세금계산서 공통 조회",
            action="세금계산서 공통 조회",
            params=qparams,
            df=df,
            message=f"세금계산서 공통 다운로드 {len(df):,}건",
        )
        out = payload.get("df_display")
        if isinstance(out, pd.DataFrame):
            return out
    except Exception:
        log.exception("get_rddbc140_export_df label/apply failed")

    return df

def get_rddbc140_analysis_summary(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    세금계산서 공통 LLM 분석용 전체 집계.

    화면 조회 TOP 200과 분리한다.
    동일 조회조건 전체 기준으로 건수/금액/거래처별/구분별/상세합계 상태별 집계를 만든다.
    """
    qparams = coalesce_params(dict(params or {}))
    where_sql = _base_filters(qparams)

    only_mismatch = clean_text(qparams.get("only_mismatch")).upper() in {"Y", "1", "TRUE"}
    mismatch_where = "WHERE detail_match = 'N'" if only_mismatch else ""

    sql = f"""
WITH in_sum AS (
    SELECT
        Rd11_Tax_Di AS Tax_Di,
        Rd11_Tax_YyMmDd AS Tax_YyMmDd,
        Rd11_Ven_Cd AS Ven_Cd,
        Rd11_Tax_Seq AS Tax_Seq,
        SUM(COALESCE(Rd11_Fin_Supply_Price, Rd11_Supply_Price, 0)) AS Sum_Supply,
        SUM(COALESCE(Rd11_Fin_Tax_Price, Rd11_Tax_Price, 0)) AS Sum_Tax
    FROM dbo.Rddbc110
    WHERE NULLIF(LTRIM(RTRIM(Rd11_Tax_Seq)), '') IS NOT NULL
    GROUP BY Rd11_Tax_Di, Rd11_Tax_YyMmDd, Rd11_Ven_Cd, Rd11_Tax_Seq
),
out_sum AS (
    SELECT
        Rd12_Tax_Di AS Tax_Di,
        Rd12_Tax_YyMmDd AS Tax_YyMmDd,
        Rd12_Ven_Cd AS Ven_Cd,
        Rd12_Tax_Seq AS Tax_Seq,
        SUM(COALESCE(Rd12_Fin_Supply_Price, Rd12_Supply_Price, 0)) AS Sum_Supply,
        SUM(COALESCE(Rd12_Fin_Tax_Price, Rd12_Tax_Price, 0)) AS Sum_Tax
    FROM dbo.Rddbc120
    WHERE NULLIF(LTRIM(RTRIM(Rd12_Tax_Seq)), '') IS NOT NULL
    GROUP BY Rd12_Tax_Di, Rd12_Tax_YyMmDd, Rd12_Ven_Cd, Rd12_Tax_Seq
),
base AS (
    SELECT
        Tax_Books.Rd14_Tax_Di AS tax_di,
        CASE
            WHEN Tax_Books.Rd14_Tax_Di = '1' THEN '매입'
            WHEN Tax_Books.Rd14_Tax_Di = '2' THEN '회계매입'
            WHEN Tax_Books.Rd14_Tax_Di = '3' THEN '매출'
            WHEN Tax_Books.Rd14_Tax_Di = '4' THEN '회계매출'
            ELSE COALESCE(NULLIF(LTRIM(RTRIM(Tax_Books.Rd14_Tax_Di)), ''), '기타')
        END AS tax_di_nm,
        Tax_Books.Rd14_Tax_YyMmDd AS tax_date,
        NULLIF(LTRIM(RTRIM(Tax_Books.Rd14_Ven_Cd)), '') AS vendor_cd,
        COALESCE(NULLIF(LTRIM(RTRIM(Ven_Cd.Rd03_Ven_Nm)), ''), '(미지정)') AS vendor_nm,
        COALESCE(NULLIF(LTRIM(RTRIM(Tax_Di.Rd01_Hnm)), ''), '(미지정)') AS tax_di_code_nm,
        CAST(COALESCE(Tax_Books.Rd14_Supply_Price, 0) AS float) AS supply_amt,
        CAST(COALESCE(Tax_Books.Rd14_Tax_Price, 0) AS float) AS tax_amt,
        CAST(COALESCE(Tax_Books.Rd14_Tot_Amt, 0) AS float) AS amount_amt,
        CASE
            WHEN I.Tax_Di IS NOT NULL THEN I.Sum_Supply
            WHEN O.Tax_Di IS NOT NULL THEN O.Sum_Supply
            ELSE NULL
        END AS detail_supply_amt,
        CASE
            WHEN I.Tax_Di IS NOT NULL THEN I.Sum_Tax
            WHEN O.Tax_Di IS NOT NULL THEN O.Sum_Tax
            ELSE NULL
        END AS detail_tax_amt,
        CASE
            WHEN I.Tax_Di IS NOT NULL
                AND COALESCE(I.Sum_Supply, 0) = COALESCE(Tax_Books.Rd14_Supply_Price, 0)
                AND COALESCE(I.Sum_Tax, 0) = COALESCE(Tax_Books.Rd14_Tax_Price, 0)
                THEN 'Y'
            WHEN O.Tax_Di IS NOT NULL
                AND COALESCE(O.Sum_Supply, 0) = COALESCE(Tax_Books.Rd14_Supply_Price, 0)
                AND COALESCE(O.Sum_Tax, 0) = COALESCE(Tax_Books.Rd14_Tax_Price, 0)
                THEN 'Y'
            WHEN I.Tax_Di IS NOT NULL OR O.Tax_Di IS NOT NULL THEN 'N'
            ELSE NULL
        END AS detail_match,
        CASE
            WHEN Tax_Books.Rd14_Tax_Di IN ('2', '4') THEN '회계분 상세연결없음'
            WHEN I.Tax_Di IS NOT NULL OR O.Tax_Di IS NOT NULL THEN
                CASE
                    WHEN
                        (
                            I.Tax_Di IS NOT NULL
                            AND COALESCE(I.Sum_Supply, 0) = COALESCE(Tax_Books.Rd14_Supply_Price, 0)
                            AND COALESCE(I.Sum_Tax, 0) = COALESCE(Tax_Books.Rd14_Tax_Price, 0)
                        )
                        OR
                        (
                            O.Tax_Di IS NOT NULL
                            AND COALESCE(O.Sum_Supply, 0) = COALESCE(Tax_Books.Rd14_Supply_Price, 0)
                            AND COALESCE(O.Sum_Tax, 0) = COALESCE(Tax_Books.Rd14_Tax_Price, 0)
                        )
                    THEN '상세합계 일치'
                    ELSE '상세합계 불일치'
                END
            ELSE '상세 없음'
        END AS detail_status_nm
    FROM dbo.Rddbc140 AS Tax_Books
    LEFT JOIN dbo.Rddbc060 AS Add_Cd
        ON Tax_Books.Rd14_Add_Cd = Add_Cd.Rd06_User_Cd
    LEFT JOIN dbo.Rddbc060 AS Mod_Cd
        ON Tax_Books.Rd14_Mod_Cd = Mod_Cd.Rd06_User_Cd
    LEFT JOIN dbo.Rddbc030 AS Ven_Cd
        ON Tax_Books.Rd14_Ven_Cd = Ven_Cd.Rd03_Ven_Cd
    LEFT JOIN dbo.Rddbc010 AS Tax_Di
        ON Tax_Books.Rd14_Tax_Di_Gcode = Tax_Di.Rd01_Gcode
       AND Tax_Books.Rd14_Tax_Di       = Tax_Di.Rd01_Tcode
    LEFT JOIN in_sum AS I
        ON Tax_Books.Rd14_Tax_Di       = '1'
       AND Tax_Books.Rd14_Tax_YyMmDd   = I.Tax_YyMmDd
       AND Tax_Books.Rd14_Ven_Cd       = I.Ven_Cd
       AND Tax_Books.Rd14_Tax_Seq      = I.Tax_Seq
    LEFT JOIN out_sum AS O
        ON Tax_Books.Rd14_Tax_Di       = '3'
       AND Tax_Books.Rd14_Tax_YyMmDd   = O.Tax_YyMmDd
       AND Tax_Books.Rd14_Ven_Cd       = O.Ven_Cd
       AND Tax_Books.Rd14_Tax_Seq      = O.Tax_Seq
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
        SUM(CASE WHEN detail_match = 'N' THEN 1 ELSE 0 END) AS mismatch_count,
        SUM(CASE WHEN tax_di IN ('1', '3') AND detail_match IS NULL THEN 1 ELSE 0 END) AS detail_missing_count,
        SUM(CASE WHEN tax_di IN ('2', '4') THEN 1 ELSE 0 END) AS accounting_count,
        COUNT(DISTINCT vendor_cd) AS vendor_count
    FROM filtered

    UNION ALL

    SELECT
        'by_tax_type',
        tax_di_nm,
        COUNT_BIG(*),
        SUM(supply_amt),
        SUM(tax_amt),
        SUM(amount_amt),
        SUM(CASE WHEN detail_match = 'N' THEN 1 ELSE 0 END),
        SUM(CASE WHEN tax_di IN ('1', '3') AND detail_match IS NULL THEN 1 ELSE 0 END),
        SUM(CASE WHEN tax_di IN ('2', '4') THEN 1 ELSE 0 END),
        CAST(NULL AS int)
    FROM filtered
    GROUP BY tax_di_nm

    UNION ALL

    SELECT
        'top_vendors',
        vendor_nm,
        COUNT_BIG(*),
        SUM(supply_amt),
        SUM(tax_amt),
        SUM(amount_amt),
        SUM(CASE WHEN detail_match = 'N' THEN 1 ELSE 0 END),
        SUM(CASE WHEN tax_di IN ('1', '3') AND detail_match IS NULL THEN 1 ELSE 0 END),
        SUM(CASE WHEN tax_di IN ('2', '4') THEN 1 ELSE 0 END),
        CAST(NULL AS int)
    FROM filtered
    GROUP BY vendor_nm

    UNION ALL

    SELECT
        'by_match_status',
        detail_status_nm,
        COUNT_BIG(*),
        SUM(supply_amt),
        SUM(tax_amt),
        SUM(amount_amt),
        SUM(CASE WHEN detail_match = 'N' THEN 1 ELSE 0 END),
        SUM(CASE WHEN tax_di IN ('1', '3') AND detail_match IS NULL THEN 1 ELSE 0 END),
        SUM(CASE WHEN tax_di IN ('2', '4') THEN 1 ELSE 0 END),
        CAST(NULL AS int)
    FROM filtered
    GROUP BY detail_status_nm
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
    mismatch_count,
    detail_missing_count,
    accounting_count,
    vendor_count
FROM ranked
WHERE section = 'overall'
   OR rn <= 10
ORDER BY
    CASE section
        WHEN 'overall' THEN 0
        WHEN 'by_tax_type' THEN 1
        WHEN 'top_vendors' THEN 2
        WHEN 'by_match_status' THEN 3
        ELSE 9
    END,
    rn
"""
    df = query_to_df(sql, qparams)
    if df is None or df.empty:
        return {
            "row_count_total": 0,
            "row_count": 0,
            "by_tax_type": [],
            "top_vendors": [],
            "by_match_status": [],
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
        "mismatch_count": _analysis_scalar(overall, "mismatch_count", 0),
        "detail_missing_count": _analysis_scalar(overall, "detail_missing_count", 0),
        "accounting_count": _analysis_scalar(overall, "accounting_count", 0),
        "vendor_count": _analysis_scalar(overall, "vendor_count", 0),
        "by_tax_type": _analysis_records_from_section_df(df, "by_tax_type"),
        "top_vendors": _analysis_records_from_section_df(df, "top_vendors"),
        "by_match_status": _analysis_records_from_section_df(df, "by_match_status"),
    }



