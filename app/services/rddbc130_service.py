# app/services/rddbc130_service.py
# 거래명세서 공통 조회를 위한 SQL WHERE 절을 생성하는 함수입니다.

from __future__ import annotations

import logging
import os
import time
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

VALIDATION_SCOPE_CURRENT_RESULT = "current_result"
VALIDATION_SCOPE_FULL_RANGE = "full_range"

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


def _filtered_transaction_scope_ctes(where_sql: str) -> str:
    """Scope detail aggregates to the already-filtered transaction documents."""
    return f"""
FilteredBooks AS (
    SELECT Trans_Books.*
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
    WHERE 1 = 1
    {where_sql}
),
FilteredTransactionKeys AS (
    SELECT DISTINCT
        Rd13_Trans_Di AS Trans_Di,
        Rd13_Trans_YyMmDd AS Trans_YyMmDd,
        Rd13_Ven_Cd AS Ven_Cd,
        Rd13_Trans_Seq AS Trans_Seq
    FROM FilteredBooks
)
"""


def _is_validation_requested(params: Dict[str, Any]) -> bool:
    """Return the explicit transaction-document validation intent only."""
    return any(
        clean_text(params.get(key)).upper() in {"Y", "1", "TRUE"}
        for key in ("validation_requested", "only_mismatch", "only_mismatch_trans")
    )


def _validation_scope(params: Dict[str, Any]) -> str:
    """Return the explicit validation scope without widening a current result."""
    if clean_text(params.get("validation_scope")) == VALIDATION_SCOPE_FULL_RANGE:
        return VALIDATION_SCOPE_FULL_RANGE
    return VALIDATION_SCOPE_CURRENT_RESULT


def _current_result_transaction_scope_ctes(where_sql: str) -> str:
    """Build keys for exactly the same TOP result shown by the header query."""
    return f"""
CurrentResultBooks AS (
    SELECT TOP (%(top)s)
        Trans_Books.Rd13_Trans_Di,
        Trans_Books.Rd13_Trans_YyMmDd,
        Trans_Books.Rd13_Ven_Cd,
        Trans_Books.Rd13_Trans_Seq
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
    WHERE 1 = 1
    {where_sql}
    ORDER BY Trans_Books.Rd13_Trans_YyMmDd DESC, Trans_Books.Rd13_Ven_Cd, Trans_Books.Rd13_Trans_Seq DESC
),
FilteredTransactionKeys AS (
    SELECT DISTINCT
        Rd13_Trans_Di AS Trans_Di,
        Rd13_Trans_YyMmDd AS Trans_YyMmDd,
        Rd13_Ven_Cd AS Ven_Cd,
        Rd13_Trans_Seq AS Trans_Seq
    FROM CurrentResultBooks
)
"""


def _validation_scope_ctes(where_sql: str, *, scope: str) -> str:
    if scope == VALIDATION_SCOPE_FULL_RANGE:
        return _filtered_transaction_scope_ctes(where_sql)
    return _current_result_transaction_scope_ctes(where_sql)


def _base_document_sql(where_sql: str, *, include_top: bool = True) -> str:
    """Build the header-only query used by ordinary transaction-document views."""
    top_sql = "TOP (%(top)s)" if include_top else ""
    return f"""
SELECT {top_sql}
    Trans_Books.Rd13_Trans_Di,
    CASE
        WHEN Trans_Books.Rd13_Trans_Di = '1' THEN '입고'
        WHEN Trans_Books.Rd13_Trans_Di = '3' THEN '출고'
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
WHERE 1 = 1
{where_sql}
ORDER BY Trans_Books.Rd13_Trans_YyMmDd DESC, Trans_Books.Rd13_Ven_Cd, Trans_Books.Rd13_Trans_Seq DESC
"""


def _validation_aggregate_sql(where_sql: str, *, side: str, scope: str) -> str:
    """Return one native-key, filtered-document detail aggregate query."""
    if side == "inbound":
        table, alias, prefix = "dbo.Rddbc110", "In_Put", "Rd11"
    elif side == "outbound":
        table, alias, prefix = "dbo.Rddbc120", "Out_Put", "Rd12"
    else:
        raise ValueError(f"unsupported validation side: {side}")

    scope_ctes = _validation_scope_ctes(where_sql, scope=scope)
    return f"""
WITH {scope_ctes}
SELECT
    {alias}.{prefix}_Trans_Di AS Trans_Di,
    {alias}.{prefix}_Trans_YyMmDd AS Trans_YyMmDd,
    {alias}.{prefix}_Ven_Cd AS Ven_Cd,
    {alias}.{prefix}_Trans_Seq AS Trans_Seq,
    SUM(COALESCE({alias}.{prefix}_Fin_Supply_Price, {alias}.{prefix}_Supply_Price, 0)) AS Sum_Supply,
    SUM(COALESCE({alias}.{prefix}_Fin_Tax_Price, {alias}.{prefix}_Tax_Price, 0)) AS Sum_Tax,
    COUNT_BIG(*) AS Detail_Row_Count
FROM {table} AS {alias}
INNER JOIN FilteredTransactionKeys AS Keys
    ON {alias}.{prefix}_Trans_Di = Keys.Trans_Di
   AND {alias}.{prefix}_Trans_YyMmDd = Keys.Trans_YyMmDd
   AND {alias}.{prefix}_Ven_Cd = Keys.Ven_Cd
   AND {alias}.{prefix}_Trans_Seq = Keys.Trans_Seq
GROUP BY
    {alias}.{prefix}_Trans_Di,
    {alias}.{prefix}_Trans_YyMmDd,
    {alias}.{prefix}_Ven_Cd,
    {alias}.{prefix}_Trans_Seq
"""


def _merge_validation_aggregates(
    base_df: pd.DataFrame,
    inbound_df: Optional[pd.DataFrame],
    outbound_df: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """Attach key-scoped validation totals without changing the header result grain."""
    if base_df is None or base_df.empty:
        return pd.DataFrame() if base_df is None else base_df

    key_columns = ["Rd13_Trans_Di", "Rd13_Trans_YyMmDd", "Rd13_Ven_Cd", "Rd13_Trans_Seq"]
    aggregate_keys = ["Trans_Di", "Trans_YyMmDd", "Ven_Cd", "Trans_Seq"]

    def _merge_key(value: Any, *, sequence: bool) -> Any:
        if pd.isna(value):
            return pd.NA
        if sequence:
            numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
            return pd.NA if pd.isna(numeric) else int(numeric)
        # SQL native equality already selected the scoped detail rows. This is
        # only a pandas merge representation normalizer for fixed-char keys.
        return str(value).rstrip()

    merge_columns = ["__validation_key_di", "__validation_key_date", "__validation_key_vendor", "__validation_key_seq"]

    def _prepared(frame: Optional[pd.DataFrame], prefix: str) -> pd.DataFrame:
        if frame is None or frame.empty:
            return pd.DataFrame(columns=merge_columns + [f"{prefix}_present", f"{prefix}_supply", f"{prefix}_tax"])
        out = frame.loc[:, aggregate_keys + ["Sum_Supply", "Sum_Tax"]].copy()
        for raw_column, merge_column in zip(aggregate_keys, merge_columns):
            out[merge_column] = out[raw_column].map(
                lambda value, is_sequence=raw_column == "Trans_Seq": _merge_key(value, sequence=is_sequence)
            )
        out[f"{prefix}_present"] = True
        return out.loc[:, merge_columns + ["Sum_Supply", "Sum_Tax", f"{prefix}_present"]].rename(
            columns={"Sum_Supply": f"{prefix}_supply", "Sum_Tax": f"{prefix}_tax"}
        )

    base = base_df.copy()
    # Preserve header key values exactly as returned by Rddbc130.  SQL already
    # used native equality; these temporary columns only bridge fixed-char and
    # numeric representations when pandas merges the scoped aggregates.
    for raw_column, merge_column in zip(key_columns, merge_columns):
        base[merge_column] = base[raw_column].map(
            lambda value, is_sequence=raw_column == "Rd13_Trans_Seq": _merge_key(value, sequence=is_sequence)
        )

    merged = base.merge(
        _prepared(inbound_df, "_in"),
        how="left",
        left_on=merge_columns,
        right_on=merge_columns,
        sort=False,
    )
    merged = merged.merge(
        _prepared(outbound_df, "_out"),
        how="left",
        left_on=merge_columns,
        right_on=merge_columns,
        sort=False,
    )

    inbound_present = merged["_in_present"].eq(True)
    outbound_present = merged["_out_present"].eq(True)
    detail_present = inbound_present | outbound_present
    merged["상세합_공급가액"] = merged["_out_supply"].where(~inbound_present, merged["_in_supply"])
    merged["상세합_세액"] = merged["_out_tax"].where(~inbound_present, merged["_in_tax"])
    amounts_match = (
        merged["상세합_공급가액"].fillna(0).eq(merged["Rd13_Supply_Price"].fillna(0))
        & merged["상세합_세액"].fillna(0).eq(merged["Rd13_Tax_Price"].fillna(0))
    )
    merged["상세합계일치"] = pd.NA
    merged.loc[detail_present & amounts_match, "상세합계일치"] = "Y"
    merged.loc[detail_present & ~amounts_match, "상세합계일치"] = "N"
    # A missing detail set is itself actionable for validation, but its sums
    # are undefined.  Keep the amounts null and expose the state explicitly.
    merged.loc[~detail_present, "상세합계일치"] = "상세없음"
    merged.loc[inbound_present, "거래명세서구분명"] = "입고"
    merged.loc[~inbound_present & outbound_present, "거래명세서구분명"] = "출고"
    return merged.drop(
        columns=merge_columns + ["_in_present", "_in_supply", "_in_tax", "_out_present", "_out_supply", "_out_tax"],
        errors="ignore",
    )


def _current_source_validation_keys(df: pd.DataFrame) -> pd.DataFrame:
    """Extract native Rddbc130 keys from an already stashed current table."""
    aliases = (
        ("Rd13_Trans_Di", "거래명세서구분"),
        ("Rd13_Trans_YyMmDd", "거래명세서일자"),
        ("Rd13_Ven_Cd", "거래처코드"),
        ("Rd13_Trans_Seq", "거래명세서순번"),
    )
    selected: list[str] = []
    for candidates in aliases:
        column = next((name for name in candidates if name in df.columns), None)
        if not column:
            return pd.DataFrame(columns=["Trans_Di", "Trans_YyMmDd", "Ven_Cd", "Trans_Seq"])
        selected.append(column)

    keys = df.loc[:, selected].copy()
    keys.columns = ["Trans_Di", "Trans_YyMmDd", "Ven_Cd", "Trans_Seq"]
    for column in ("Trans_Di", "Trans_YyMmDd", "Ven_Cd"):
        keys[column] = keys[column].map(lambda value: "" if pd.isna(value) else str(value).rstrip())
    keys["Trans_Seq"] = pd.to_numeric(keys["Trans_Seq"], errors="coerce")
    keys = keys[
        keys["Trans_Di"].ne("")
        & keys["Trans_YyMmDd"].ne("")
        & keys["Ven_Cd"].ne("")
        & keys["Trans_Seq"].notna()
    ].copy()
    if keys.empty:
        return keys
    keys["Trans_Seq"] = keys["Trans_Seq"].astype("int64")
    return keys.drop_duplicates(ignore_index=True)


def _current_source_validation_aggregate_sql(
    keys: pd.DataFrame,
    *,
    side: str,
    batch_index: int,
) -> tuple[str, dict[str, Any]]:
    """Aggregate one detail side for supplied Rddbc130 keys without rereading Rddbc130."""
    if side == "inbound":
        table, alias, prefix = "dbo.Rddbc110", "In_Put", "Rd11"
    elif side == "outbound":
        table, alias, prefix = "dbo.Rddbc120", "Out_Put", "Rd12"
    else:
        raise ValueError(f"unsupported validation side: {side}")

    values: list[str] = []
    params: dict[str, Any] = {}
    for row_index, row in enumerate(keys.itertuples(index=False)):
        names = {
            "di": f"current_key_{batch_index}_{row_index}_di",
            "date": f"current_key_{batch_index}_{row_index}_date",
            "ven": f"current_key_{batch_index}_{row_index}_ven",
            "seq": f"current_key_{batch_index}_{row_index}_seq",
        }
        params.update(
            {
                names["di"]: row.Trans_Di,
                names["date"]: row.Trans_YyMmDd,
                names["ven"]: row.Ven_Cd,
                names["seq"]: int(row.Trans_Seq),
            }
        )
        values.append(
            "(%({di})s, %({date})s, %({ven})s, %({seq})s)".format(**names)
        )

    sql = f"""
WITH CurrentResultKeys (Trans_Di, Trans_YyMmDd, Ven_Cd, Trans_Seq) AS (
    SELECT Source.Trans_Di, Source.Trans_YyMmDd, Source.Ven_Cd, Source.Trans_Seq
    FROM (VALUES
        {",\n        ".join(values)}
    ) AS Source (Trans_Di, Trans_YyMmDd, Ven_Cd, Trans_Seq)
)
SELECT
    {alias}.{prefix}_Trans_Di AS Trans_Di,
    {alias}.{prefix}_Trans_YyMmDd AS Trans_YyMmDd,
    {alias}.{prefix}_Ven_Cd AS Ven_Cd,
    {alias}.{prefix}_Trans_Seq AS Trans_Seq,
    SUM(COALESCE({alias}.{prefix}_Fin_Supply_Price, {alias}.{prefix}_Supply_Price, 0)) AS Sum_Supply,
    SUM(COALESCE({alias}.{prefix}_Fin_Tax_Price, {alias}.{prefix}_Tax_Price, 0)) AS Sum_Tax,
    COUNT_BIG(*) AS Detail_Row_Count
FROM {table} AS {alias}
INNER JOIN CurrentResultKeys AS Keys
    ON {alias}.{prefix}_Trans_Di = Keys.Trans_Di
   AND {alias}.{prefix}_Trans_YyMmDd = Keys.Trans_YyMmDd
   AND {alias}.{prefix}_Ven_Cd = Keys.Ven_Cd
   AND {alias}.{prefix}_Trans_Seq = Keys.Trans_Seq
GROUP BY
    {alias}.{prefix}_Trans_Di,
    {alias}.{prefix}_Trans_YyMmDd,
    {alias}.{prefix}_Ven_Cd,
    {alias}.{prefix}_Trans_Seq
"""
    return sql, params


def validate_rddbc130_current_result_df(current_df: pd.DataFrame) -> pd.DataFrame:
    """Validate only the already-bound transaction-document rows.

    This intentionally never queries Rddbc130. It obtains the native document
    keys from the in-memory current table and reads only matching Rddbc110/120
    rows before applying the existing merge/comparison contract.
    """
    if not isinstance(current_df, pd.DataFrame) or current_df.empty:
        return pd.DataFrame() if current_df is None else current_df

    keys = _current_source_validation_keys(current_df)
    if keys.empty:
        raise ValueError("현재 거래명세서 표에서 검증 key를 찾지 못했습니다.")

    # Current-table storage normally holds the Korean display frame. Add only
    # temporary raw aliases required by the shared comparison helper, then
    # remove them again so the current-table presentation contract is unchanged.
    working = current_df.copy()
    added_columns: list[str] = []
    raw_aliases = {
        "Rd13_Trans_Di": ("거래명세서구분",),
        "Rd13_Trans_YyMmDd": ("거래명세서일자",),
        "Rd13_Ven_Cd": ("거래처코드",),
        "Rd13_Trans_Seq": ("거래명세서순번",),
        "Rd13_Supply_Price": ("공급가액",),
        "Rd13_Tax_Price": ("세액",),
    }
    for raw_name, candidates in raw_aliases.items():
        if raw_name in working.columns:
            continue
        source_name = next((name for name in candidates if name in working.columns), None)
        if not source_name:
            raise ValueError(f"현재 거래명세서 표에서 {raw_name} 비교 컬럼을 찾지 못했습니다.")
        working[raw_name] = working[source_name]
        added_columns.append(raw_name)

    # Four values per key stay under SQL Server's parameter ceiling per query.
    batch_size = 450
    aggregated: dict[str, list[pd.DataFrame]] = {"inbound": [], "outbound": []}
    for batch_index, start in enumerate(range(0, len(keys), batch_size)):
        batch = keys.iloc[start:start + batch_size]
        for side in ("inbound", "outbound"):
            sql, params = _current_source_validation_aggregate_sql(
                batch,
                side=side,
                batch_index=batch_index,
            )
            result = query_to_df(sql, params)
            if isinstance(result, pd.DataFrame) and not result.empty:
                aggregated[side].append(result)

    inbound_df = pd.concat(aggregated["inbound"], ignore_index=True) if aggregated["inbound"] else pd.DataFrame()
    outbound_df = pd.concat(aggregated["outbound"], ignore_index=True) if aggregated["outbound"] else pd.DataFrame()
    validated = _merge_validation_aggregates(working, inbound_df, outbound_df)
    log.info(
        "[io.detail.perf] action=거래명세서 공통 조회 stage=current_result_validation "
        "header_rows=%s validation_key_count=%s inbound_group_count=%s outbound_group_count=%s",
        len(current_df),
        len(keys),
        len(inbound_df),
        len(outbound_df),
    )
    return validated.drop(columns=added_columns, errors="ignore")


def get_rddbc130_df(params: Optional[Dict[str, str]] = None):
    params = coalesce_params(params)
    params["top"] = _doc_query_top(params, default=200)
    started = time.perf_counter()
    validation_requested = _is_validation_requested(params)
    validation_scope = _validation_scope(params) if validation_requested else None
    where_sql = _base_filters(params)

    base_started = time.perf_counter()
    df = query_to_df(
        _base_document_sql(
            where_sql,
            include_top=validation_scope != VALIDATION_SCOPE_FULL_RANGE,
        ),
        params,
    )
    header_key_count = 0
    if isinstance(df, pd.DataFrame) and not df.empty:
        header_key_count = int(
            df.loc[:, ["Rd13_Trans_Di", "Rd13_Trans_YyMmDd", "Rd13_Ven_Cd", "Rd13_Trans_Seq"]]
            .drop_duplicates()
            .shape[0]
        )
    log.info(
        "[io.detail.perf] action=거래명세서 공통 조회 stage=base_query validation_requested=%s validation_scope=%s header_rows=%s header_key_count=%s elapsed_ms=%s top=%s",
        validation_requested,
        validation_scope or "none",
        0 if df is None else len(df),
        header_key_count,
        int((time.perf_counter() - base_started) * 1000),
        params.get("top"),
    )

    if validation_requested and isinstance(df, pd.DataFrame) and not df.empty:
        log.info(
            "[io.detail.perf] action=거래명세서 공통 조회 stage=validation_key_scope validation_scope=%s header_rows=%s header_key_count=%s validation_key_count=%s",
            validation_scope,
            len(df),
            header_key_count,
            header_key_count,
        )
        detail_started = time.perf_counter()
        inbound_df = query_to_df(_validation_aggregate_sql(where_sql, side="inbound", scope=validation_scope), params)
        outbound_df = query_to_df(_validation_aggregate_sql(where_sql, side="outbound", scope=validation_scope), params)
        inbound_detail_rows = int(inbound_df["Detail_Row_Count"].sum()) if isinstance(inbound_df, pd.DataFrame) and "Detail_Row_Count" in inbound_df else 0
        outbound_detail_rows = int(outbound_df["Detail_Row_Count"].sum()) if isinstance(outbound_df, pd.DataFrame) and "Detail_Row_Count" in outbound_df else 0
        log.info(
            "[io.detail.perf] action=거래명세서 공통 조회 stage=validation_detail_query validation_scope=%s inbound_detail_rows=%s outbound_detail_rows=%s inbound_group_count=%s outbound_group_count=%s elapsed_ms=%s",
            validation_scope,
            inbound_detail_rows,
            outbound_detail_rows,
            0 if inbound_df is None else len(inbound_df),
            0 if outbound_df is None else len(outbound_df),
            int((time.perf_counter() - detail_started) * 1000),
        )
        aggregate_started = time.perf_counter()
        df = _merge_validation_aggregates(df, inbound_df, outbound_df)
        log.info(
            "[io.detail.perf] action=거래명세서 공통 조회 stage=validation_complete validation_scope=%s result_rows=%s elapsed_ms=%s",
            validation_scope,
            len(df),
            int((time.perf_counter() - aggregate_started) * 1000),
        )
        if clean_text(params.get("only_mismatch")).upper() in {"Y", "1", "TRUE"}:
            # "부적합" is a completed comparison with a mismatched amount.
            # Headers without a detail set remain visible in a normal validation
            # result as "상세없음", but are not fabricated as mismatches.
            df = df[df["상세합계일치"].eq("N")]

    log.info(
        "[io.detail.perf] action=거래명세서 공통 조회 stage=service_query validation_requested=%s validation_scope=%s result_rows=%s elapsed_ms=%s top=%s",
        validation_requested,
        validation_scope or "none",
        0 if df is None else len(df),
        int((time.perf_counter() - started) * 1000),
        params.get("top"),
    )
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


def _get_rddbc130_base_analysis_summary(qparams: Dict[str, Any], where_sql: str) -> Dict[str, Any]:
    """Return the full header-only summary used when validation was not requested."""
    sql = f"""
WITH base AS (
    SELECT
        CASE
            WHEN Trans_Books.Rd13_Trans_Di = '1' THEN '매입분'
            WHEN Trans_Books.Rd13_Trans_Di = '3' THEN '매출분'
            ELSE COALESCE(NULLIF(LTRIM(RTRIM(Trans_Books.Rd13_Trans_Di)), ''), '기타')
        END AS trans_di_nm,
        NULLIF(LTRIM(RTRIM(Trans_Books.Rd13_Ven_Cd)), '') AS vendor_cd,
        COALESCE(NULLIF(LTRIM(RTRIM(Ven_Cd.Rd03_Ven_Nm)), ''), '(미지정)') AS vendor_nm,
        COALESCE(NULLIF(LTRIM(RTRIM(Delivery_Di.Rd01_Hnm)), ''), '(미지정)') AS delivery_nm,
        CAST(COALESCE(Trans_Books.Rd13_Supply_Price, 0) AS float) AS supply_amt,
        CAST(COALESCE(Trans_Books.Rd13_Tax_Price, 0) AS float) AS tax_amt,
        CAST(COALESCE(Trans_Books.Rd13_Tot_Amt, 0) AS float) AS amount_amt,
        CAST(COALESCE(Trans_Books.Rd13_Dc_Amt, 0) AS float) AS dc_amt
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
    WHERE 1 = 1
    {where_sql}
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
        CAST(NULL AS bigint) AS mismatch_count,
        COUNT(DISTINCT vendor_cd) AS vendor_count
    FROM base
    UNION ALL
    SELECT 'by_trans_type', trans_di_nm, COUNT_BIG(*), SUM(supply_amt), SUM(tax_amt), SUM(amount_amt), SUM(dc_amt), CAST(NULL AS bigint), CAST(NULL AS int)
    FROM base GROUP BY trans_di_nm
    UNION ALL
    SELECT 'top_vendors', vendor_nm, COUNT_BIG(*), SUM(supply_amt), SUM(tax_amt), SUM(amount_amt), SUM(dc_amt), CAST(NULL AS bigint), CAST(NULL AS int)
    FROM base GROUP BY vendor_nm
    UNION ALL
    SELECT 'by_delivery', delivery_nm, COUNT_BIG(*), SUM(supply_amt), SUM(tax_amt), SUM(amount_amt), SUM(dc_amt), CAST(NULL AS bigint), CAST(NULL AS int)
    FROM base GROUP BY delivery_nm
),
ranked AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY section ORDER BY amount_sum DESC, row_count DESC) AS rn
    FROM grouped
)
SELECT section, name, row_count, supply_sum, tax_sum, amount_sum, dc_sum, mismatch_count, vendor_count
FROM ranked
WHERE section = 'overall' OR rn <= 10
ORDER BY CASE section WHEN 'overall' THEN 0 WHEN 'by_trans_type' THEN 1 WHEN 'top_vendors' THEN 2 WHEN 'by_delivery' THEN 4 ELSE 9 END, rn
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
            "validation_performed": False,
        }

    overall_df = df[df["section"].astype(str) == "overall"]
    if overall_df.empty:
        return {"row_count_total": 0, "row_count": 0, "validation_performed": False}
    overall = overall_df.iloc[0]
    return {
        "row_count_total": _analysis_scalar(overall, "row_count", 0),
        "row_count": _analysis_scalar(overall, "row_count", 0),
        "supply_sum": _analysis_scalar(overall, "supply_sum", 0.0),
        "tax_sum": _analysis_scalar(overall, "tax_sum", 0.0),
        "amount_sum": _analysis_scalar(overall, "amount_sum", 0.0),
        "dc_sum": _analysis_scalar(overall, "dc_sum", 0.0),
        "mismatch_count": None,
        "vendor_count": _analysis_scalar(overall, "vendor_count", 0),
        "by_trans_type": _analysis_records_from_section_df(df, "by_trans_type"),
        "top_vendors": _analysis_records_from_section_df(df, "top_vendors"),
        "by_match_status": [],
        "by_delivery": _analysis_records_from_section_df(df, "by_delivery"),
        "validation_performed": False,
    }


def _get_rddbc130_validation_analysis_summary(qparams: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize the already key-scoped full-range validation result in memory."""
    validation_params = dict(qparams)
    validation_params["validation_scope"] = VALIDATION_SCOPE_FULL_RANGE
    df = get_rddbc130_df(validation_params)
    if df is None or df.empty:
        return {
            "row_count_total": 0,
            "row_count": 0,
            "top_vendors": [],
            "by_trans_type": [],
            "by_match_status": [],
            "by_delivery": [],
            "validation_performed": True,
        }

    work = df.copy()
    work["_trans_type"] = work["거래명세서구분명"].replace({"입고": "매입분", "출고": "매출분"}).fillna("기타")
    work["_vendor"] = work.get("거래처명", pd.Series("(미지정)", index=work.index)).fillna("(미지정)")
    work["_delivery"] = work.get("배송구분", pd.Series("(미지정)", index=work.index)).fillna("(미지정)")
    work["_match"] = work.get("상세합계일치", pd.Series(pd.NA, index=work.index)).map(
        {"Y": "상세합계 일치", "N": "상세합계 불일치", "상세없음": "상세 없음"}
    ).fillna("상세 없음")
    for source, target in (
        ("Rd13_Supply_Price", "_supply"),
        ("Rd13_Tax_Price", "_tax"),
        ("Rd13_Tot_Amt", "_amount"),
        ("Rd13_Dc_Amt", "_dc"),
    ):
        work[target] = pd.to_numeric(work.get(source, 0), errors="coerce").fillna(0.0)

    def _records(group_column: str, *, limit: int = 10) -> list[dict]:
        grouped = (
            work.groupby(group_column, dropna=False)
            .agg(
                row_count=("_amount", "size"),
                supply_sum=("_supply", "sum"),
                tax_sum=("_tax", "sum"),
                amount_sum=("_amount", "sum"),
                dc_sum=("_dc", "sum"),
                mismatch_count=("상세합계일치", lambda values: int(values.fillna("N").ne("Y").sum())),
            )
            .reset_index()
            .rename(columns={group_column: "name"})
            .sort_values(["amount_sum", "row_count"], ascending=[False, False], kind="stable")
            .head(limit)
        )
        return grouped.to_dict(orient="records")

    return {
        "row_count_total": int(len(work)),
        "row_count": int(len(work)),
        "supply_sum": float(work["_supply"].sum()),
        "tax_sum": float(work["_tax"].sum()),
        "amount_sum": float(work["_amount"].sum()),
        "dc_sum": float(work["_dc"].sum()),
        "mismatch_count": int(work["상세합계일치"].fillna("N").ne("Y").sum()),
        "vendor_count": int(work["Rd13_Ven_Cd"].nunique(dropna=True)),
        "by_trans_type": _records("_trans_type"),
        "top_vendors": _records("_vendor"),
        "by_match_status": _records("_match"),
        "by_delivery": _records("_delivery"),
        "validation_performed": True,
    }


# 거래명세서 공통 LLM 분석용 전체 집계 결과를 반환하는 함수입니다.
def get_rddbc130_analysis_summary(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    거래명세서 공통 LLM 분석용 전체 집계.

    화면 조회 TOP 200과 분리한다.
    동일 조회조건 전체 기준으로 건수/금액/거래처별/구분별/일치여부별 집계를 만든다.
    """
    qparams = coalesce_params(dict(params or {}))
    where_sql = _base_filters(qparams)
    validation_requested = _is_validation_requested(qparams)
    if not validation_requested:
        return _get_rddbc130_base_analysis_summary(qparams, where_sql)
    # Explicit validation is full-range by contract. Build it via the same
    # header-driving key-scoped service path, not the old aggregate/final-join
    # SQL shape that allowed CTE reordering to dominate the request.
    return _get_rddbc130_validation_analysis_summary(qparams)

    # Historical SQL retained below only as an unreachable reference while the
    # existing diff is audited. It is intentionally not used by production.
    scope_ctes = _filtered_transaction_scope_ctes(where_sql)

    only_mismatch = clean_text(qparams.get("only_mismatch")).upper() in {"Y", "1", "TRUE"}
    mismatch_where = "WHERE COALESCE(detail_match, 'N') <> 'Y'" if only_mismatch else ""

    sql = f"""
WITH {scope_ctes},
in_sum AS (
    SELECT
        In_Put.Rd11_Trans_Di AS Trans_Di,
        In_Put.Rd11_Trans_YyMmDd AS Trans_YyMmDd,
        In_Put.Rd11_Ven_Cd AS Ven_Cd,
        In_Put.Rd11_Trans_Seq AS Trans_Seq,
        SUM(COALESCE(In_Put.Rd11_Fin_Supply_Price, In_Put.Rd11_Supply_Price, 0)) AS Sum_Supply,
        SUM(COALESCE(In_Put.Rd11_Fin_Tax_Price, In_Put.Rd11_Tax_Price, 0)) AS Sum_Tax
    FROM dbo.Rddbc110 AS In_Put
    INNER JOIN FilteredTransactionKeys AS Keys
        ON In_Put.Rd11_Trans_Di = Keys.Trans_Di
       AND In_Put.Rd11_Trans_YyMmDd = Keys.Trans_YyMmDd
       AND In_Put.Rd11_Ven_Cd = Keys.Ven_Cd
       AND In_Put.Rd11_Trans_Seq = Keys.Trans_Seq
    GROUP BY In_Put.Rd11_Trans_Di, In_Put.Rd11_Trans_YyMmDd, In_Put.Rd11_Ven_Cd, In_Put.Rd11_Trans_Seq
),
out_sum AS (
    SELECT
        Out_Put.Rd12_Trans_Di AS Trans_Di,
        Out_Put.Rd12_Trans_YyMmDd AS Trans_YyMmDd,
        Out_Put.Rd12_Ven_Cd AS Ven_Cd,
        Out_Put.Rd12_Trans_Seq AS Trans_Seq,
        SUM(COALESCE(Out_Put.Rd12_Fin_Supply_Price, Out_Put.Rd12_Supply_Price, 0)) AS Sum_Supply,
        SUM(COALESCE(Out_Put.Rd12_Fin_Tax_Price, Out_Put.Rd12_Tax_Price, 0)) AS Sum_Tax
    FROM dbo.Rddbc120 AS Out_Put
    INNER JOIN FilteredTransactionKeys AS Keys
        ON Out_Put.Rd12_Trans_Di = Keys.Trans_Di
       AND Out_Put.Rd12_Trans_YyMmDd = Keys.Trans_YyMmDd
       AND Out_Put.Rd12_Trans_Seq = Keys.Trans_Seq
       AND Out_Put.Rd12_Ven_Cd = Keys.Ven_Cd
    GROUP BY Out_Put.Rd12_Trans_Di, Out_Put.Rd12_Trans_YyMmDd, Out_Put.Rd12_Ven_Cd, Out_Put.Rd12_Trans_Seq
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
    FROM FilteredBooks AS Trans_Books
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
        "validation_performed": True,
    }
