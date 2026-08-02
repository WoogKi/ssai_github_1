# app/services/rddbc120_service.py
# 출고명세 조회

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

import pandas as pd


from app.services.rddbc_io_common import (
    add_filter,
    add_unlabeled_name_like_filter,
    build_result_payload,    
    apply_labels_safe,
    clean_text,
    coalesce_params,
    like_value,
    make_date_filters,
    normalize_top,
    query_to_df,
)


TABLE = "rddbc120"
log = logging.getLogger("ssai.sims.rddbc120")

# 환경변수에서 정수값을 읽어오는 유틸리티 함수입니다.
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


# 출고명세 조회
# 다양한 필터 조건을 적용하여 출고명세 데이터를 조회하는 함수입니다.
def _base_filters(params: Dict[str, Any], *, include_validation_filters: bool = True) -> str:
    clauses: list[str] = []

    # 출고일자 기간 필터
    clauses += make_date_filters("Out_Put.Rd12_Out_YyMmDd", params)

    if clean_text(params.get("out_seq")):
        add_filter(clauses, "Out_Put.Rd12_Out_Seq = %(out_seq)s")

    if clean_text(params.get("ven_cd")):
        add_filter(clauses, "Out_Put.Rd12_Ven_Cd = %(ven_cd)s")
    if like_value(params.get("ven_nm")):
        params["ven_nm_like"] = like_value(params.get("ven_nm"))
        add_filter(clauses, "Ven_Cd.Rd03_Ven_Nm LIKE %(ven_nm_like)s")

    if clean_text(params.get("physic_cd")):
        add_filter(clauses, "Out_Put.Rd12_Physic_Cd = %(physic_cd)s")
    if like_value(params.get("physic_nm")):
        params["physic_nm_like"] = like_value(params.get("physic_nm"))
        add_filter(clauses, "Physic_Cd.Rd04_Physic_Nm LIKE %(physic_nm_like)s")

    # 제약사 / 제품그룹 / 제품구분 / 제품분류
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

    add_unlabeled_name_like_filter(
        clauses,
        params,
        vendor_name_exprs=("Ven_Cd.Rd03_Ven_Nm",),
        product_name_expr="Physic_Cd.Rd04_Physic_Nm",
        manufacturer_predicate=(
            "EXISTS (SELECT 1 FROM dbo.Rddbc030 AS Make_Ven_Nlq "
            "WHERE Make_Ven_Nlq.Rd03_Ven_Cd = Physic_Cd.Rd04_Ven_Cd "
            "AND Make_Ven_Nlq.Rd03_Ven_Nm LIKE %(nlq_unlabeled_name_like)s)"
        ),
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
        add_filter(clauses, "Out_Put.Rd12_Stock_Cd = %(stock_cd)s")
    if like_value(params.get("stock_nm")):
        params["stock_nm_like"] = like_value(params.get("stock_nm"))
        add_filter(clauses, "Stock_Cd.Rd01_Hnm LIKE %(stock_nm_like)s")

    if clean_text(params.get("io_gu_prefix")):
        add_filter(clauses, "LEFT(Out_Put.Rd12_Io_Gu, 1) = %(io_gu_prefix)s")

    if clean_text(params.get("trans_seq")):
        add_filter(clauses, "Out_Put.Rd12_Trans_Seq = %(trans_seq)s")

    trans_link = clean_text(params.get("trans_link"))
    if trans_link == "Y":
        add_filter(clauses, "NULLIF(LTRIM(RTRIM(Out_Put.Rd12_Trans_Seq)), '') IS NOT NULL")
    elif trans_link == "N":
        add_filter(clauses, "NULLIF(LTRIM(RTRIM(Out_Put.Rd12_Trans_Seq)), '') IS NULL")

    if clean_text(params.get("tax_seq")):
        add_filter(clauses, "Out_Put.Rd12_Tax_Seq = %(tax_seq)s")

    tax_link = clean_text(params.get("tax_link"))
    if tax_link == "Y":
        add_filter(clauses, "Out_Put.Rd12_Tax_Di = '3'")
    elif tax_link == "N":
        add_filter(clauses, "(Out_Put.Rd12_Tax_Di IS NULL OR Out_Put.Rd12_Tax_Di <> '3')")

    if clean_text(params.get("buy_cd")):
        add_filter(clauses, "Out_Put.Rd12_In_Ven_Cd = %(buy_cd)s")
    if like_value(params.get("buy_nm")):
        params["buy_nm_like"] = like_value(params.get("buy_nm"))
        add_filter(clauses, "In_Ven_Cd.Rd03_Ven_Nm LIKE %(buy_nm_like)s")

    if clean_text(params.get("sales_man")):
        add_filter(clauses, "Out_Put.Rd12_Sales_Man = %(sales_man)s")
    if like_value(params.get("sales_man_nm")):
        params["sales_man_nm_like"] = like_value(params.get("sales_man_nm"))
        add_filter(clauses, "Sales_Man.Rd06_User_Nm LIKE %(sales_man_nm_like)s")

    if clean_text(params.get("real_ven_cd")):
        add_filter(clauses, "Out_Put.Rd12_Real_Ven_Cd = %(real_ven_cd)s")
    if like_value(params.get("real_ven_nm")):
        params["real_ven_nm_like"] = like_value(params.get("real_ven_nm"))
        add_filter(clauses, "Real_Ven_Cd.Rd03_Ven_Nm LIKE %(real_ven_nm_like)s")

    if clean_text(params.get("add_cd")):
        add_filter(clauses, "Out_Put.Rd12_Add_Cd = %(add_cd)s")
    if like_value(params.get("add_nm")):
        params["add_nm_like"] = like_value(params.get("add_nm"))
        add_filter(clauses, "Add_Cd.Rd06_User_Nm LIKE %(add_nm_like)s")

    if clean_text(params.get("mod_cd")):
        add_filter(clauses, "Out_Put.Rd12_Mod_Cd = %(mod_cd)s")
    if like_value(params.get("mod_nm")):
        params["mod_nm_like"] = like_value(params.get("mod_nm"))
        add_filter(clauses, "Mod_Cd.Rd06_User_Nm LIKE %(mod_nm_like)s")

    if clean_text(params.get("add_date_from")):
        add_filter(clauses, "Out_Put.Rd12_Add_Date >= %(add_date_from)s")
    if clean_text(params.get("add_date_to")):
        add_filter(clauses, "Out_Put.Rd12_Add_Date <= %(add_date_to)s")
    if clean_text(params.get("mod_date_from")):
        add_filter(clauses, "Out_Put.Rd12_Mod_Date >= %(mod_date_from)s")
    if clean_text(params.get("mod_date_to")):
        add_filter(clauses, "Out_Put.Rd12_Mod_Date <= %(mod_date_to)s")

    if include_validation_filters and clean_text(params.get("only_mismatch_trans")).upper() in {"Y", "1", "TRUE"}:
        add_filter(
            clauses,
            """(
                T13.Rd13_Trans_Seq IS NULL
                OR COALESCE(TS.Sum_Fin_Supply_Price, 0) <> COALESCE(T13.Rd13_Supply_Price, 0)
                OR COALESCE(TS.Sum_Fin_Tax_Price, 0) <> COALESCE(T13.Rd13_Tax_Price, 0)
            )""",
        )

    if include_validation_filters and clean_text(params.get("only_mismatch_tax")).upper() in {"Y", "1", "TRUE"}:
        add_filter(
            clauses,
            """(
                T14.Rd14_Tax_Seq IS NULL
                OR COALESCE(TX.Sum_Fin_Supply_Price, 0) <> COALESCE(T14.Rd14_Supply_Price, 0)
                OR COALESCE(TX.Sum_Fin_Tax_Price, 0) <> COALESCE(T14.Rd14_Tax_Price, 0)
            )""",
        )

    return ("\n      AND " + "\n      AND ".join(clauses)) if clauses else ""


def _validation_mismatch_filters(params: Dict[str, Any]) -> str:
    """Return validation predicates after the selected-row joins are available."""
    clauses: list[str] = []
    if clean_text(params.get("only_mismatch_trans")).upper() in {"Y", "1", "TRUE"}:
        clauses.append(
            "(T13.Rd13_Trans_Seq IS NULL "
            "OR COALESCE(TS.Sum_Fin_Supply_Price, 0) <> COALESCE(T13.Rd13_Supply_Price, 0) "
            "OR COALESCE(TS.Sum_Fin_Tax_Price, 0) <> COALESCE(T13.Rd13_Tax_Price, 0))"
        )
    if clean_text(params.get("only_mismatch_tax")).upper() in {"Y", "1", "TRUE"}:
        clauses.append(
            "(T14.Rd14_Tax_Seq IS NULL "
            "OR COALESCE(TX.Sum_Fin_Supply_Price, 0) <> COALESCE(T14.Rd14_Supply_Price, 0) "
            "OR COALESCE(TX.Sum_Fin_Tax_Price, 0) <> COALESCE(T14.Rd14_Tax_Price, 0))"
        )
    return ("\nWHERE " + "\n  AND ".join(clauses)) if clauses else ""

# 출고명세 조회 결과를 데이터프레임으로 반환하는 함수입니다.
# 다양한 조인과 집계를 포함하는 SQL 쿼리를 실행하여, 조회된 데이터를 판다스 데이터프레임으로 변환하여 반환합니다.
def _validation_targets(params: Dict[str, Any]) -> tuple[bool, bool]:
    """Return the validation sides explicitly requested by the caller."""
    enabled = {"Y", "1", "TRUE"}
    return (
        clean_text(params.get("validation_trans")).upper() in enabled
        or clean_text(params.get("only_mismatch_trans")).upper() in enabled,
        clean_text(params.get("validation_tax")).upper() in enabled
        or clean_text(params.get("only_mismatch_tax")).upper() in enabled,
    )


def _validation_join_sql(params: Dict[str, Any]) -> str:
    """Return only the requested validation joins for the summary query."""
    use_trans, use_tax = _validation_targets(params)
    joins: list[str] = []
    if use_trans:
        joins.append("""
OUTER APPLY (
    SELECT
        SUM(COALESCE(Trans_Put.Rd12_Fin_Supply_Price, Trans_Put.Rd12_Supply_Price, 0)) AS Sum_Fin_Supply_Price,
        SUM(COALESCE(Trans_Put.Rd12_Fin_Tax_Price, Trans_Put.Rd12_Tax_Price, 0)) AS Sum_Fin_Tax_Price
    FROM dbo.Rddbc120 AS Trans_Put
    WHERE Trans_Put.Rd12_Trans_Di = Out_Put.Rd12_Trans_Di
      AND Trans_Put.Rd12_Trans_YyMmDd = Out_Put.Rd12_Trans_YyMmDd
      AND Trans_Put.Rd12_Ven_Cd = Out_Put.Rd12_Ven_Cd
      AND Trans_Put.Rd12_Trans_Seq = Out_Put.Rd12_Trans_Seq
      AND NULLIF(LTRIM(RTRIM(Trans_Put.Rd12_Trans_Seq)), '') IS NOT NULL
) AS TS
LEFT JOIN dbo.Rddbc130 AS T13
    ON Out_Put.Rd12_Trans_Di = T13.Rd13_Trans_Di
   AND Out_Put.Rd12_Trans_YyMmDd = T13.Rd13_Trans_YyMmDd
   AND Out_Put.Rd12_Ven_Cd = T13.Rd13_Ven_Cd
   AND Out_Put.Rd12_Trans_Seq = T13.Rd13_Trans_Seq
""")
    if use_tax:
        joins.append("""
OUTER APPLY (
    SELECT
        SUM(COALESCE(Tax_Put.Rd12_Fin_Supply_Price, Tax_Put.Rd12_Supply_Price, 0)) AS Sum_Fin_Supply_Price,
        SUM(COALESCE(Tax_Put.Rd12_Fin_Tax_Price, Tax_Put.Rd12_Tax_Price, 0)) AS Sum_Fin_Tax_Price
    FROM dbo.Rddbc120 AS Tax_Put
    WHERE Tax_Put.Rd12_Tax_Di = Out_Put.Rd12_Tax_Di
      AND Tax_Put.Rd12_Tax_YyMmDd = Out_Put.Rd12_Tax_YyMmDd
      AND Tax_Put.Rd12_Ven_Cd = Out_Put.Rd12_Ven_Cd
      AND Tax_Put.Rd12_Tax_Seq = Out_Put.Rd12_Tax_Seq
      AND NULLIF(LTRIM(RTRIM(Tax_Put.Rd12_Tax_Seq)), '') IS NOT NULL
) AS TX
LEFT JOIN dbo.Rddbc140 AS T14
    ON Out_Put.Rd12_Tax_Di = T14.Rd14_Tax_Di
   AND Out_Put.Rd12_Tax_YyMmDd = T14.Rd14_Tax_YyMmDd
   AND Out_Put.Rd12_Ven_Cd = T14.Rd14_Ven_Cd
   AND Out_Put.Rd12_Tax_Seq = T14.Rd14_Tax_Seq
""")
    return "\n".join(joins)


def _validation_ctes_for(
    source_cte: str,
    prefix: str,
    *,
    use_trans: bool,
    use_tax: bool,
) -> str:
    """Aggregate validation amounts only for keys in ``source_cte``.

    The former per-row OUTER APPLY scanned Rddbc120 once for each displayed
    row.  This CTE keeps the same key and amount contract, while restricting
    both aggregate scans to distinct transaction/tax keys from the supplied CTE.
    """
    ctes: list[str] = []
    if use_trans:
        ctes.append(f"""
{prefix}TransKeys AS (
    SELECT DISTINCT
        Rd12_Trans_Di,
        Rd12_Trans_YyMmDd,
        Rd12_Ven_Cd,
        Rd12_Trans_Seq
    FROM {source_cte}
    WHERE NULLIF(LTRIM(RTRIM(Rd12_Trans_Seq)), '') IS NOT NULL
),
{prefix}TransSums AS (
    SELECT
        Trans_Put.Rd12_Trans_Di,
        Trans_Put.Rd12_Trans_YyMmDd,
        Trans_Put.Rd12_Ven_Cd,
        Trans_Put.Rd12_Trans_Seq,
        SUM(COALESCE(Trans_Put.Rd12_Fin_Supply_Price, Trans_Put.Rd12_Supply_Price, 0)) AS Sum_Fin_Supply_Price,
        SUM(COALESCE(Trans_Put.Rd12_Fin_Tax_Price, Trans_Put.Rd12_Tax_Price, 0)) AS Sum_Fin_Tax_Price
    FROM dbo.Rddbc120 AS Trans_Put
    INNER JOIN {prefix}TransKeys AS K
        ON Trans_Put.Rd12_Trans_Di = K.Rd12_Trans_Di
       AND Trans_Put.Rd12_Trans_YyMmDd = K.Rd12_Trans_YyMmDd
       AND Trans_Put.Rd12_Ven_Cd = K.Rd12_Ven_Cd
       AND Trans_Put.Rd12_Trans_Seq = K.Rd12_Trans_Seq
    WHERE NULLIF(LTRIM(RTRIM(Trans_Put.Rd12_Trans_Seq)), '') IS NOT NULL
    GROUP BY Trans_Put.Rd12_Trans_Di, Trans_Put.Rd12_Trans_YyMmDd, Trans_Put.Rd12_Ven_Cd, Trans_Put.Rd12_Trans_Seq
)
""")
    if use_tax:
        ctes.append(f"""
{prefix}TaxKeys AS (
    SELECT DISTINCT
        Rd12_Tax_Di,
        Rd12_Tax_YyMmDd,
        Rd12_Ven_Cd,
        Rd12_Tax_Seq
    FROM {source_cte}
    WHERE NULLIF(LTRIM(RTRIM(Rd12_Tax_Seq)), '') IS NOT NULL
),
{prefix}TaxSums AS (
    SELECT
        Tax_Put.Rd12_Tax_Di,
        Tax_Put.Rd12_Tax_YyMmDd,
        Tax_Put.Rd12_Ven_Cd,
        Tax_Put.Rd12_Tax_Seq,
        SUM(COALESCE(Tax_Put.Rd12_Fin_Supply_Price, Tax_Put.Rd12_Supply_Price, 0)) AS Sum_Fin_Supply_Price,
        SUM(COALESCE(Tax_Put.Rd12_Fin_Tax_Price, Tax_Put.Rd12_Tax_Price, 0)) AS Sum_Fin_Tax_Price
    FROM dbo.Rddbc120 AS Tax_Put
    INNER JOIN {prefix}TaxKeys AS K
        ON Tax_Put.Rd12_Tax_Di = K.Rd12_Tax_Di
       AND Tax_Put.Rd12_Tax_YyMmDd = K.Rd12_Tax_YyMmDd
       AND Tax_Put.Rd12_Ven_Cd = K.Rd12_Ven_Cd
       AND Tax_Put.Rd12_Tax_Seq = K.Rd12_Tax_Seq
    WHERE NULLIF(LTRIM(RTRIM(Tax_Put.Rd12_Tax_Seq)), '') IS NOT NULL
    GROUP BY Tax_Put.Rd12_Tax_Di, Tax_Put.Rd12_Tax_YyMmDd, Tax_Put.Rd12_Ven_Cd, Tax_Put.Rd12_Tax_Seq
)
""")
    return ",\n".join(cte.strip() for cte in ctes)


def _selected_validation_ctes(params: Dict[str, Any]) -> str:
    use_trans, use_tax = _validation_targets(params)
    return _validation_ctes_for(
        "SelectedRows",
        "Selected",
        use_trans=use_trans,
        use_tax=use_tax,
    )


def _selected_validation_join_sql(params: Dict[str, Any]) -> str:
    """Join only the validation aggregates selected for the detail result."""
    use_trans, use_tax = _validation_targets(params)
    joins: list[str] = []
    if use_trans:
        joins.append("""
LEFT JOIN SelectedTransSums AS TS
    ON Out_Put.Rd12_Trans_Di = TS.Rd12_Trans_Di
   AND Out_Put.Rd12_Trans_YyMmDd = TS.Rd12_Trans_YyMmDd
   AND Out_Put.Rd12_Ven_Cd = TS.Rd12_Ven_Cd
   AND Out_Put.Rd12_Trans_Seq = TS.Rd12_Trans_Seq
LEFT JOIN dbo.Rddbc130 AS T13
    ON Out_Put.Rd12_Trans_Di = T13.Rd13_Trans_Di
   AND Out_Put.Rd12_Trans_YyMmDd = T13.Rd13_Trans_YyMmDd
   AND Out_Put.Rd12_Ven_Cd = T13.Rd13_Ven_Cd
   AND Out_Put.Rd12_Trans_Seq = T13.Rd13_Trans_Seq
""")
    if use_tax:
        joins.append("""
LEFT JOIN SelectedTaxSums AS TX
    ON Out_Put.Rd12_Tax_Di = TX.Rd12_Tax_Di
   AND Out_Put.Rd12_Tax_YyMmDd = TX.Rd12_Tax_YyMmDd
   AND Out_Put.Rd12_Ven_Cd = TX.Rd12_Ven_Cd
   AND Out_Put.Rd12_Tax_Seq = TX.Rd12_Tax_Seq
LEFT JOIN dbo.Rddbc140 AS T14
    ON Out_Put.Rd12_Tax_Di = T14.Rd14_Tax_Di
   AND Out_Put.Rd12_Tax_YyMmDd = T14.Rd14_Tax_YyMmDd
   AND Out_Put.Rd12_Ven_Cd = T14.Rd14_Ven_Cd
   AND Out_Put.Rd12_Tax_Seq = T14.Rd14_Tax_Seq
""")
    return "\n".join(joins)


def _needs_validation_join(params: Dict[str, Any]) -> bool:
    return any(
        clean_text(params.get(key)).upper() in {"Y", "1", "TRUE"}
        for key in ("only_mismatch_trans", "only_mismatch_tax")
    )


def _requires_validation_projection(params: Dict[str, Any]) -> bool:
    """Return whether the current user explicitly requested validation data.

    Action names can originate from routing metadata or retrieved context, so
    they are deliberately not a validation trigger.  The router passes this
    flag only after it has confirmed both an outbound SIMS action and a signal
    in the current raw user question.  Mismatch flags remain a narrow
    compatibility path for direct panel/service callers.
    """
    return any(_validation_targets(params))


def _mismatch_selected_rows_ctes(
    where_sql: str,
    validation_where_sql: str,
    params: Dict[str, Any],
) -> str:
    """Build the mismatch-only path before applying the display/export TOP."""
    use_trans, use_tax = _validation_targets(params)
    validation_ctes = _validation_ctes_for(
        "BaseRows",
        "Base",
        use_trans=use_trans,
        use_tax=use_tax,
    )
    validation_joins: list[str] = []
    if use_trans:
        validation_joins.append("""
    LEFT JOIN BaseTransSums AS TS
        ON Out_Put.Rd12_Trans_Di = TS.Rd12_Trans_Di
       AND Out_Put.Rd12_Trans_YyMmDd = TS.Rd12_Trans_YyMmDd
       AND Out_Put.Rd12_Ven_Cd = TS.Rd12_Ven_Cd
       AND Out_Put.Rd12_Trans_Seq = TS.Rd12_Trans_Seq
    LEFT JOIN dbo.Rddbc130 AS T13
        ON Out_Put.Rd12_Trans_Di = T13.Rd13_Trans_Di
       AND Out_Put.Rd12_Trans_YyMmDd = T13.Rd13_Trans_YyMmDd
       AND Out_Put.Rd12_Ven_Cd = T13.Rd13_Ven_Cd
       AND Out_Put.Rd12_Trans_Seq = T13.Rd13_Trans_Seq
""")
    if use_tax:
        validation_joins.append("""
    LEFT JOIN BaseTaxSums AS TX
        ON Out_Put.Rd12_Tax_Di = TX.Rd12_Tax_Di
       AND Out_Put.Rd12_Tax_YyMmDd = TX.Rd12_Tax_YyMmDd
       AND Out_Put.Rd12_Ven_Cd = TX.Rd12_Ven_Cd
       AND Out_Put.Rd12_Tax_Seq = TX.Rd12_Tax_Seq
    LEFT JOIN dbo.Rddbc140 AS T14
        ON Out_Put.Rd12_Tax_Di = T14.Rd14_Tax_Di
       AND Out_Put.Rd12_Tax_YyMmDd = T14.Rd14_Tax_YyMmDd
       AND Out_Put.Rd12_Ven_Cd = T14.Rd14_Ven_Cd
       AND Out_Put.Rd12_Tax_Seq = T14.Rd14_Tax_Seq
""")
    return f"""
BaseRows AS (
    SELECT Out_Put.*
    FROM dbo.Rddbc120 AS Out_Put
    LEFT JOIN dbo.Rddbc060 AS Add_Cd
        ON Out_Put.Rd12_Add_Cd = Add_Cd.Rd06_User_Cd
    LEFT JOIN dbo.Rddbc060 AS Mod_Cd
        ON Out_Put.Rd12_Mod_Cd = Mod_Cd.Rd06_User_Cd
    LEFT JOIN dbo.Rddbc030 AS Ven_Cd
        ON Out_Put.Rd12_Ven_Cd = Ven_Cd.Rd03_Ven_Cd
    LEFT JOIN dbo.Rddbc030 AS In_Ven_Cd
        ON Out_Put.Rd12_In_Ven_Cd = In_Ven_Cd.Rd03_Ven_Cd
    LEFT JOIN dbo.Rddbc030 AS Real_Ven_Cd
        ON Out_Put.Rd12_Real_Ven_Cd = Real_Ven_Cd.Rd03_Ven_Cd
    LEFT JOIN dbo.Rddbc040 AS Physic_Cd
        ON Out_Put.Rd12_Physic_Cd = Physic_Cd.Rd04_Physic_Cd
    LEFT JOIN dbo.Rddbc010 AS Stock_Cd
        ON Out_Put.Rd12_Stock_Cd_Gcode = Stock_Cd.Rd01_Gcode
       AND Out_Put.Rd12_Stock_Cd = Stock_Cd.Rd01_Tcode
    LEFT JOIN dbo.Rddbc060 AS Sales_Man
        ON Out_Put.Rd12_Sales_Man = Sales_Man.Rd06_User_Cd
    WHERE 1 = 1
    {where_sql}
),
{validation_ctes},
MismatchRows AS (
    SELECT Out_Put.*
    FROM BaseRows AS Out_Put
    {''.join(validation_joins)}
    {validation_where_sql}
),
SelectedRows AS (
    SELECT TOP (%(top)s) Out_Put.*
    FROM MismatchRows AS Out_Put
    ORDER BY Out_Put.Rd12_Out_YyMmDd, Out_Put.Rd12_Ven_Cd, Out_Put.Rd12_Out_Seq
)
"""


def get_rddbc120_df(params: Optional[Dict[str, Any]] = None):
    started = time.perf_counter()
    params = coalesce_params(params)
    params["top"] = _io_query_top(params, default=200)

    where_sql = _base_filters(params, include_validation_filters=False)
    validation_where_sql = _validation_mismatch_filters(params)
    if _needs_validation_join(params):
        selected_rows_ctes = _mismatch_selected_rows_ctes(
            where_sql,
            validation_where_sql,
            params,
        )
        final_validation_where_sql = ""
    else:
        selected_rows_ctes = f"""
SelectedRows AS (
    SELECT TOP (%(top)s) Out_Put.*
    FROM dbo.Rddbc120 AS Out_Put
    LEFT JOIN dbo.Rddbc060 AS Add_Cd
        ON Out_Put.Rd12_Add_Cd = Add_Cd.Rd06_User_Cd
    LEFT JOIN dbo.Rddbc060 AS Mod_Cd
        ON Out_Put.Rd12_Mod_Cd = Mod_Cd.Rd06_User_Cd
    LEFT JOIN dbo.Rddbc030 AS Ven_Cd
        ON Out_Put.Rd12_Ven_Cd = Ven_Cd.Rd03_Ven_Cd
    LEFT JOIN dbo.Rddbc030 AS In_Ven_Cd
        ON Out_Put.Rd12_In_Ven_Cd = In_Ven_Cd.Rd03_Ven_Cd
    LEFT JOIN dbo.Rddbc030 AS Real_Ven_Cd
        ON Out_Put.Rd12_Real_Ven_Cd = Real_Ven_Cd.Rd03_Ven_Cd
    LEFT JOIN dbo.Rddbc040 AS Physic_Cd
        ON Out_Put.Rd12_Physic_Cd = Physic_Cd.Rd04_Physic_Cd
    LEFT JOIN dbo.Rddbc010 AS Stock_Cd
        ON Out_Put.Rd12_Stock_Cd_Gcode = Stock_Cd.Rd01_Gcode
       AND Out_Put.Rd12_Stock_Cd = Stock_Cd.Rd01_Tcode
    LEFT JOIN dbo.Rddbc060 AS Sales_Man
        ON Out_Put.Rd12_Sales_Man = Sales_Man.Rd06_User_Cd
    WHERE 1 = 1
    {where_sql}
    ORDER BY Out_Put.Rd12_Out_YyMmDd, Out_Put.Rd12_Ven_Cd, Out_Put.Rd12_Out_Seq
)
"""
        final_validation_where_sql = validation_where_sql

    validation_projection = _requires_validation_projection(params)
    use_trans, use_tax = _validation_targets(params)
    validation_ctes_sql = f",\n{_selected_validation_ctes(params)}" if validation_projection else ""
    validation_select_sql = """
    Out_Put.Rd12_Trans_Di,
    Out_Put.Rd12_Trans_YyMmDd,
    Out_Put.Rd12_Trans_Seq,
    TS.Sum_Fin_Supply_Price AS 거래명세서상세합_공급가액,
    TS.Sum_Fin_Tax_Price AS 거래명세서상세합_세액,
    T13.Rd13_Trans_Di AS 거래명세서헤더구분,
    T13.Rd13_Supply_Price AS 거래명세서헤더_공급가액,
    T13.Rd13_Tax_Price AS 거래명세서헤더_세액,
    CASE
        WHEN T13.Rd13_Trans_Seq IS NULL THEN NULL
        WHEN COALESCE(TS.Sum_Fin_Supply_Price, 0) = COALESCE(T13.Rd13_Supply_Price, 0)
         AND COALESCE(TS.Sum_Fin_Tax_Price, 0) = COALESCE(T13.Rd13_Tax_Price, 0) THEN 'Y'
        ELSE 'N'
    END AS 거래명세서금액일치,
    Out_Put.Rd12_Tax_Di,
    Out_Put.Rd12_Tax_YyMmDd,
    Out_Put.Rd12_Tax_Seq,
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
""" if validation_projection else ""
    if validation_projection:
        # Keep the six native link fields in their historic detail positions.
        # The derived validation columns are optional and must not determine
        # whether a normal outbound result exposes its source fields.
        trans_validation_select_sql, tax_validation_select_sql = validation_select_sql.split(
            "    Out_Put.Rd12_Tax_Di,",
            1,
        )
        trans_validation_select_sql = trans_validation_select_sql.replace(
            "    Out_Put.Rd12_Trans_Di,\n"
            "    Out_Put.Rd12_Trans_YyMmDd,\n"
            "    Out_Put.Rd12_Trans_Seq,\n",
            "",
            1,
        )
        trans_validation_select_sql = (
            trans_validation_select_sql.rstrip().rstrip(",") + ",\n"
            if use_trans
            else ""
        )
        tax_validation_select_sql = tax_validation_select_sql.replace(
            "\n    Out_Put.Rd12_Tax_YyMmDd,\n"
            "    Out_Put.Rd12_Tax_Seq,\n",
            "",
            1,
        )
        tax_validation_select_sql = (
            tax_validation_select_sql.rstrip().rstrip(",") + ",\n"
            if use_tax
            else ""
        )
    else:
        trans_validation_select_sql = ""
        tax_validation_select_sql = ""
    validation_join_sql = _selected_validation_join_sql(params) if validation_projection else ""

    sql = f"""
WITH {selected_rows_ctes}{validation_ctes_sql}
SELECT
    Out_Put.Rd12_Out_YyMmDd,
    Out_Put.Rd12_Ven_Cd,
    Ven_Cd.Rd03_Ven_Nm AS 거래처명,
    Out_Put.Rd12_Out_Seq,
    Out_Put.Rd12_In_Ven_Cd,
    In_Ven_Cd.Rd03_Ven_Nm AS 매입처명,
    Out_Put.Rd12_Stock_Cd_Gcode,
    Out_Put.Rd12_Stock_Cd,
    Stock_Cd.Rd01_Hnm AS 재고위치,
    Out_Put.Rd12_Physic_Cd,
    Physic_Cd.Rd04_Physic_Nm AS 제품명,
    Out_Put.Rd12_Real_Ven_Cd,
    Real_Ven_Cd.Rd03_Ven_Nm AS 실납처명,
    Out_Put.Rd12_Io_Gu_Gcode,
    Out_Put.Rd12_Io_Gu,
    Io_Gu.Rd01_Hnm AS 입출고구분,
    LEFT(Out_Put.Rd12_Io_Gu, 1) AS Io_Gu_Prefix,
    Out_Put.Rd12_Cost_Apply_Cd,
    Cost_Apply.Rd03_Ven_Nm AS 단가적용처명,
    Out_Put.Rd12_Stock_Apply_Cd,
    Stock_Apply.Rd03_Ven_Nm AS 재고적용처명,
    Out_Put.Rd12_Unit_Cost,
    Out_Put.Rd12_Quantity,
    Out_Put.Rd12_Oquantity,
    Out_Put.Rd12_Supply_Price,
    Out_Put.Rd12_Tax_Price,
    Out_Put.Rd12_Fin_Unit_Cost,
    Out_Put.Rd12_Fin_Supply_Price,
    Out_Put.Rd12_Fin_Tax_Price,
    Out_Put.Rd12_Product_No,
    Out_Put.Rd12_Term_Date,
    Out_Put.Rd12_Sales_Man,
    Sales_Man.Rd06_User_Nm AS 영업사원명,
    Out_Put.Rd12_Sales_Unit,
    Out_Put.Rd12_Sales_Supply_Price,
    Out_Put.Rd12_Sales_Tax_Price,
    Out_Put.Rd12_Trans_Di,
    Out_Put.Rd12_Trans_YyMmDd,
    Out_Put.Rd12_Trans_Seq,
{trans_validation_select_sql}
    Out_Put.Rd12_Tax_Di,
    Out_Put.Rd12_Tax_YyMmDd,
    Out_Put.Rd12_Tax_Seq,
{tax_validation_select_sql}
    Out_Put.Rd12_Dc_Amt,
    Out_Put.Rd12_Other,
    Out_Put.Rd12_Insu_Price,
    Out_Put.Rd12_Fixed_Flag,
    Out_Put.Rd12_Record_Flag,
    Out_Put.Rd12_Reform_Flag,
    Out_Put.Rd12_Validation,
    Out_Put.Rd12_Add_Date,
    Out_Put.Rd12_Add_Cd,
    Add_Cd.Rd06_User_Nm AS 등록자,
    Out_Put.Rd12_Mod_Date,
    Out_Put.Rd12_Mod_Cd,
    Mod_Cd.Rd06_User_Nm AS 수정자,
    Out_Put.Rd12_Dope_Flag,
    Out_Put.Rd12_Position_Num,
    Out_Put.Rd12_Real_Sales_Man,
    Out_Put.Rd12_Validation_Qty,
    Out_Put.Rd12_Prod_Date,
    Out_Put.Rd12_Add_Time,
    Out_Put.Rd12_Mod_Time
FROM SelectedRows AS Out_Put
LEFT JOIN dbo.Rddbc060 AS Add_Cd
    ON Out_Put.Rd12_Add_Cd = Add_Cd.Rd06_User_Cd
LEFT JOIN dbo.Rddbc060 AS Mod_Cd
    ON Out_Put.Rd12_Mod_Cd = Mod_Cd.Rd06_User_Cd
LEFT JOIN dbo.Rddbc030 AS Ven_Cd
    ON Out_Put.Rd12_Ven_Cd = Ven_Cd.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS In_Ven_Cd
    ON Out_Put.Rd12_In_Ven_Cd = In_Ven_Cd.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS Real_Ven_Cd
    ON Out_Put.Rd12_Real_Ven_Cd = Real_Ven_Cd.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc040 AS Physic_Cd
    ON Out_Put.Rd12_Physic_Cd = Physic_Cd.Rd04_Physic_Cd
LEFT JOIN dbo.Rddbc030 AS Cost_Apply
    ON Out_Put.Rd12_Cost_Apply_Cd = Cost_Apply.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS Stock_Apply
    ON Out_Put.Rd12_Stock_Apply_Cd = Stock_Apply.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc010 AS Stock_Cd
    ON Out_Put.Rd12_Stock_Cd_Gcode = Stock_Cd.Rd01_Gcode
   AND Out_Put.Rd12_Stock_Cd = Stock_Cd.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS Io_Gu
    ON Out_Put.Rd12_Io_Gu_Gcode = Io_Gu.Rd01_Gcode
   AND Out_Put.Rd12_Io_Gu = Io_Gu.Rd01_Tcode
LEFT JOIN dbo.Rddbc060 AS Sales_Man
    ON Out_Put.Rd12_Sales_Man = Sales_Man.Rd06_User_Cd
{validation_join_sql}
{final_validation_where_sql}
ORDER BY Out_Put.Rd12_Out_YyMmDd , Out_Put.Rd12_Ven_Cd, Out_Put.Rd12_Out_Seq 
"""
    df = query_to_df(sql, params)
    log.info(
        "[io.detail.perf] action=출고명세 조회 stage=service_query "
        "result_rows=%s elapsed_ms=%s top=%s condition_type_count=%s",
        0 if df is None else len(df),
        int((time.perf_counter() - started) * 1000),
        params.get("top"),
        sum(bool(clean_text(params.get(key))) for key in (
            "date_from", "date_to", "ven_cd", "ven_nm", "physic_cd", "physic_nm",
            "product_ven_cd", "product_ven_nm", "stock_cd", "stock_nm",
        )),
    )

    return df

# 입고명세 조회 결과를 처리하여 최종 페이로드를 반환하는 함수입니다.
# 조회된 데이터의 행 수를 로그로 기록하며, 결과가 없는 경우에는 적절한 메시지를 포함한 페이로드를 반환합니다.
def get_rddbc120_result(params: Optional[Dict[str, Any]] = None):
    params = coalesce_params(params)
    df = get_rddbc120_df(params)

    row_count = 0 if df is None else int(len(df))
    log.info("DBG get_rddbc120_result rows=%s", row_count)

    if row_count == 0:
        return {
            "table": TABLE,
            "title": "출고명세 조회",
            "action": "출고명세 조회",
            "params": params,
            "data": "해당 자료가 없습니다.",
            "final": True,
            "meta": {"row_count": 0},
        }

    return build_result_payload(
        table=TABLE,
        title="출고명세 조회",
        action="출고명세 조회",
        params=params,
        df=df,
        message=f"출고명세 {row_count:,}건",
    )

def get_rddbc120_export_df(params: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    """
    출고명세 CSV/Excel 다운로드용 전체 상세 DataFrame.

    화면 조회 TOP 200과 분리한다.
    단, 무제한은 위험하므로 SIMS_EXPORT_MAX_ROWS 기본 100000건까지 허용한다.
    """
    qparams = coalesce_params(params)
    export_top = _io_export_top()

    qparams["top"] = export_top
    qparams["_max_top"] = export_top

    df = get_rddbc120_df(qparams)

    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame() if df is None else df

    try:
        payload = build_result_payload(
            table=TABLE,
            title="출고명세 조회",
            action="출고명세 조회",
            params=qparams,
            df=df,
            message=f"출고명세 다운로드 {len(df):,}건",
        )
        out = payload.get("df_display")
        if isinstance(out, pd.DataFrame):
            return out
    except Exception:
        log.exception("get_rddbc120_export_df label/apply failed")

    return df


# 출고명세 조회 결과를 분석하여 섹션별 집계를 생성하는 함수입니다.
# 섹션은 거래처명, 실납처명, 제품명, 영업사원명 등으로 구분할 수 있으며, 각 섹션별로 건수, 수량 합계, 공급가액 합계, 세액 합계, 금액 합계 등을 계산하여 리스트 형태로 반환합니다. 
# 입력 데이터프레임이 None이거나 비어있거나, 섹션 컬럼이 없는 경우에는 빈 리스트를 반환합니다.
# 섹션별 집계는 분석 결과에서 자주 사용되는 형태이므로, 이를 생성하는 공통 함수를 만들어 재사용할 수 있도록 합니다.
# 분석 결과에서 섹션별 집계를 생성하는 함수는 입력 데이터프레임과 섹션 이름을 받아서, 해당 섹션에 해당하는 행들을 필터링한 후, 각 행에 대해 건수, 수량 합계, 공급가액 합계, 세액 합계, 금액 합계 등을 계산하여 리스트 형태로 반환합니다.
# 섹션별 집계 결과는 분석 결과에서 자주 사용되는 형태이므로, 이를 생성하는 공통 함수를 만들어 재사용할 수 있도록 합니다.
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


def get_rddbc120_analysis_summary(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    출고명세 LLM 분석용 전체 집계.

    중요:
    - 화면 조회 TOP 200과 분리한다.
    - 동일 조회조건 전체 기준으로 건수/수량/금액/매출처/실납처/제품/영업사원 집계를 만든다.
    """
    qparams = coalesce_params(dict(params or {}))
    where_sql = _base_filters(qparams)
    validation_join_sql = _validation_join_sql(qparams) if _requires_validation_projection(qparams) else ""

    sql = f"""
WITH base AS (
    SELECT
        Out_Put.Rd12_Out_YyMmDd AS io_date,
        NULLIF(LTRIM(RTRIM(Out_Put.Rd12_Ven_Cd)), '') AS vendor_cd,
        COALESCE(NULLIF(LTRIM(RTRIM(Ven_Cd.Rd03_Ven_Nm)), ''), '(미지정)') AS vendor_nm,
        NULLIF(LTRIM(RTRIM(Out_Put.Rd12_Real_Ven_Cd)), '') AS real_vendor_cd,
        COALESCE(NULLIF(LTRIM(RTRIM(Real_Ven_Cd.Rd03_Ven_Nm)), ''), '(미지정)') AS real_vendor_nm,
        NULLIF(LTRIM(RTRIM(Out_Put.Rd12_In_Ven_Cd)), '') AS buy_vendor_cd,
        COALESCE(NULLIF(LTRIM(RTRIM(In_Ven_Cd.Rd03_Ven_Nm)), ''), '(미지정)') AS buy_vendor_nm,
        NULLIF(LTRIM(RTRIM(Out_Put.Rd12_Physic_Cd)), '') AS product_cd,
        COALESCE(NULLIF(LTRIM(RTRIM(Physic_Cd.Rd04_Physic_Nm)), ''), '(미지정)') AS product_nm,
        NULLIF(LTRIM(RTRIM(Out_Put.Rd12_Stock_Cd)), '') AS stock_cd,
        COALESCE(NULLIF(LTRIM(RTRIM(Stock_Cd.Rd01_Hnm)), ''), '(미지정)') AS stock_nm,
        NULLIF(LTRIM(RTRIM(Out_Put.Rd12_Sales_Man)), '') AS sales_staff_cd,
        COALESCE(NULLIF(LTRIM(RTRIM(Sales_Man.Rd06_User_Nm)), ''), '(미지정)') AS sales_staff_nm,
        CAST(COALESCE(Out_Put.Rd12_Quantity, 0) AS float) AS qty,
        CAST(COALESCE(Out_Put.Rd12_Fin_Supply_Price, Out_Put.Rd12_Supply_Price, 0) AS float) AS supply_amt,
        CAST(COALESCE(Out_Put.Rd12_Fin_Tax_Price, Out_Put.Rd12_Tax_Price, 0) AS float) AS tax_amt
    FROM dbo.Rddbc120 AS Out_Put
    LEFT JOIN dbo.Rddbc060 AS Add_Cd
        ON Out_Put.Rd12_Add_Cd = Add_Cd.Rd06_User_Cd
    LEFT JOIN dbo.Rddbc060 AS Mod_Cd
        ON Out_Put.Rd12_Mod_Cd = Mod_Cd.Rd06_User_Cd
    LEFT JOIN dbo.Rddbc030 AS Ven_Cd
        ON Out_Put.Rd12_Ven_Cd = Ven_Cd.Rd03_Ven_Cd
    LEFT JOIN dbo.Rddbc030 AS In_Ven_Cd
        ON Out_Put.Rd12_In_Ven_Cd = In_Ven_Cd.Rd03_Ven_Cd
    LEFT JOIN dbo.Rddbc030 AS Real_Ven_Cd
        ON Out_Put.Rd12_Real_Ven_Cd = Real_Ven_Cd.Rd03_Ven_Cd
    LEFT JOIN dbo.Rddbc040 AS Physic_Cd
        ON Out_Put.Rd12_Physic_Cd = Physic_Cd.Rd04_Physic_Cd
    LEFT JOIN dbo.Rddbc010 AS Stock_Cd
        ON Out_Put.Rd12_Stock_Cd_Gcode = Stock_Cd.Rd01_Gcode
       AND Out_Put.Rd12_Stock_Cd = Stock_Cd.Rd01_Tcode
    LEFT JOIN dbo.Rddbc010 AS Io_Gu
        ON Out_Put.Rd12_Io_Gu_Gcode = Io_Gu.Rd01_Gcode
       AND Out_Put.Rd12_Io_Gu = Io_Gu.Rd01_Tcode
    LEFT JOIN dbo.Rddbc060 AS Sales_Man
        ON Out_Put.Rd12_Sales_Man = Sales_Man.Rd06_User_Cd
    {validation_join_sql}
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
        COUNT(DISTINCT stock_cd) AS stock_location_count,
        COUNT(DISTINCT sales_staff_cd) AS staff_count
    FROM base

    UNION ALL

    SELECT 'top_sales_vendors', vendor_nm, COUNT_BIG(*), SUM(qty), SUM(supply_amt), SUM(tax_amt), SUM(supply_amt + tax_amt),
           CAST(NULL AS int), CAST(NULL AS int), CAST(NULL AS int), CAST(NULL AS int)
    FROM base
    GROUP BY vendor_nm

    UNION ALL

    SELECT 'top_real_vendors', real_vendor_nm, COUNT_BIG(*), SUM(qty), SUM(supply_amt), SUM(tax_amt), SUM(supply_amt + tax_amt),
           CAST(NULL AS int), CAST(NULL AS int), CAST(NULL AS int), CAST(NULL AS int)
    FROM base
    GROUP BY real_vendor_nm

    UNION ALL

    SELECT 'top_buy_vendors', buy_vendor_nm, COUNT_BIG(*), SUM(qty), SUM(supply_amt), SUM(tax_amt), SUM(supply_amt + tax_amt),
           CAST(NULL AS int), CAST(NULL AS int), CAST(NULL AS int), CAST(NULL AS int)
    FROM base
    GROUP BY buy_vendor_nm

    UNION ALL

    SELECT 'top_products', product_nm, COUNT_BIG(*), SUM(qty), SUM(supply_amt), SUM(tax_amt), SUM(supply_amt + tax_amt),
           CAST(NULL AS int), CAST(NULL AS int), CAST(NULL AS int), CAST(NULL AS int)
    FROM base
    GROUP BY product_nm

    UNION ALL

    SELECT 'top_sales_staff', sales_staff_nm, COUNT_BIG(*), SUM(qty), SUM(supply_amt), SUM(tax_amt), SUM(supply_amt + tax_amt),
           CAST(NULL AS int), CAST(NULL AS int), CAST(NULL AS int), CAST(NULL AS int)
    FROM base
    GROUP BY sales_staff_nm

    UNION ALL

    SELECT 'top_stock_locations', stock_nm, COUNT_BIG(*), SUM(qty), SUM(supply_amt), SUM(tax_amt), SUM(supply_amt + tax_amt),
           CAST(NULL AS int), CAST(NULL AS int), CAST(NULL AS int), CAST(NULL AS int)
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
    stock_location_count,
    staff_count
FROM ranked
WHERE section = 'overall'
   OR rn <= 10
ORDER BY
    CASE section
        WHEN 'overall' THEN 0
        WHEN 'top_sales_vendors' THEN 1
        WHEN 'top_real_vendors' THEN 2
        WHEN 'top_buy_vendors' THEN 3
        WHEN 'top_products' THEN 4
        WHEN 'top_sales_staff' THEN 5
        WHEN 'top_stock_locations' THEN 6
        ELSE 9
    END,
    rn
"""
    df = query_to_df(sql, qparams)
    if df is None or df.empty:
        return {
            "row_count_total": 0,
            "row_count": 0,
            "top_sales_vendors": [],
            "top_vendors": [],
            "top_real_vendors": [],
            "top_buy_vendors": [],
            "top_products": [],
            "top_sales_staff": [],
            "top_stock_locations": [],
        }

    overall_df = df[df["section"].astype(str) == "overall"]
    if overall_df.empty:
        return {"row_count_total": 0, "row_count": 0}

    overall = overall_df.iloc[0]

    top_sales_vendors = _analysis_records_from_section_df(df, "top_sales_vendors")

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
        "staff_count": _analysis_scalar(overall, "staff_count", 0),
        "top_sales_vendors": top_sales_vendors,
        "top_vendors": top_sales_vendors,
        "top_real_vendors": _analysis_records_from_section_df(df, "top_real_vendors"),
        "top_buy_vendors": _analysis_records_from_section_df(df, "top_buy_vendors"),
        "top_products": _analysis_records_from_section_df(df, "top_products"),
        "top_sales_staff": _analysis_records_from_section_df(df, "top_sales_staff"),
        "top_stock_locations": _analysis_records_from_section_df(df, "top_stock_locations"),
    }
