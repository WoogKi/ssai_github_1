# app/services/product_flow_service.py
# 제품수불현황 조회

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import pandas as pd

from app.services.rddbc_io_common import (
    clean_text,
    coalesce_params,
    normalize_top,
    query_to_df,
)
from app.services.product_inventory_service import resolve_inventory_stock_codes

import logging

log = logging.getLogger("ssai")

TABLE = "product_flow"
TITLE = "제품수불현황 조회"
ACTION = "제품수불현황 조회"

def _norm_yyyymmdd(value: Any, default: str = "") -> str:
    text = clean_text(value)
    if not text:
        return default
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) == 8:
        return digits
    if len(digits) == 6:
        return digits + "01"
    return default


def _fmt_date_text(value: Any) -> str:
    text = clean_text(value)
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) == 8:
        return f"{digits[:4]}/{digits[4:6]}/{digits[6:8]}"
    return text

def _digits_only(value: Any) -> str:
    return "".join(ch for ch in clean_text(value) if ch.isdigit())


def _last_day_of_month(yyyymm: str) -> str:
    first = datetime.strptime(yyyymm + "01", "%Y%m%d")
    if first.month == 12:
        next_first = first.replace(year=first.year + 1, month=1, day=1)
    else:
        next_first = first.replace(month=first.month + 1, day=1)
    return (next_first - timedelta(days=1)).strftime("%Y%m%d")


def _resolve_flow_dates(params: Dict[str, Any]) -> tuple[str, str]:
    raw_from = _digits_only(params.get("date_from"))
    raw_to = _digits_only(params.get("date_to"))
    month_from = _digits_only(params.get("month_from"))
    month_to = _digits_only(params.get("month_to"))
    today = datetime.now().strftime("%Y%m%d")

    if not raw_from and month_from:
        raw_from = month_from
    if not raw_to and month_to:
        raw_to = month_to

    if not raw_from and not raw_to:
        return today[:6] + "01", today

    if not raw_from:
        if len(raw_to) == 6:
            return raw_to + "01", _last_day_of_month(raw_to)
        if len(raw_to) == 8:
            return raw_to[:6] + "01", raw_to

    if not raw_to:
        if len(raw_from) == 6:
            return raw_from + "01", _last_day_of_month(raw_from)
        if len(raw_from) == 8:
            return raw_from[:6] + "01", raw_from

    if len(raw_from) == 6:
        date_from = raw_from + "01"
    else:
        date_from = _norm_yyyymmdd(raw_from)

    if len(raw_to) == 6:
        date_to = _last_day_of_month(raw_to)
    else:
        date_to = _norm_yyyymmdd(raw_to)

    if not date_from:
        date_from = today[:6] + "01"
    if not date_to:
        date_to = today

    return date_from, date_to


def _stock_mode_label(value: Any) -> str:
    text = clean_text(value).lower()
    return {
        "real": "실수불",
        "book": "장부수불",
        "실수불": "실수불",
        "장부수불": "장부수불",
    }.get(text, clean_text(value) or "실수불")


def _date_basis_label(value: Any) -> str:
    text = clean_text(value).lower()
    return {
        "io": "입출고일자",
        "trans": "명세서일자",
        "입출고일자": "입출고일자",
        "명세서일자": "명세서일자",
    }.get(text, clean_text(value) or "입출고일자")


def _flow_scope_label(value: Any) -> str:
    text = clean_text(value).lower()
    return {
        "all": "전체",
        "in": "매입",
        "out": "매출",
        "전체": "전체",
        "매입": "매입",
        "매출": "매출",
    }.get(text, clean_text(value) or "전체")


def _build_flow_query_summary(
    *,
    date_from: str,
    date_to: str,
    settings: Dict[str, Any],
    work_params: Dict[str, Any],
    params: Dict[str, Any],
) -> str:
    bits = [
        f"기간 {_fmt_date_text(date_from)} ~ {_fmt_date_text(date_to)}",
        f"기준 {_stock_mode_label(settings['stock_mode'])}",
        f"범위 {_flow_scope_label(settings['flow_scope'])}",
        f"기준일자 {_date_basis_label(settings['date_basis'])}",
    ]

    product_info = work_params.get("__product_info__") or {}
    if not isinstance(product_info, dict):
        product_info = {}

    physic_cd = clean_text(params.get("physic_cd") or product_info.get("제품코드"))
    physic_nm = clean_text(params.get("physic_nm") or product_info.get("제품명"))
    maker_nm = clean_text(product_info.get("제조사명"))
    order_nm = clean_text(product_info.get("발주처명"))
    product_group_nm = clean_text(product_info.get("제품그룹명"))
    product_class_nm = clean_text(product_info.get("제품분류명"))
    stock_cds = _normalize_stock_codes(work_params)

    if physic_cd and physic_nm:
        bits.append(f"제품 {physic_cd} ({physic_nm})")
    elif physic_cd:
        bits.append(f"제품코드 {physic_cd}")
    elif physic_nm:
        bits.append(f"제품명 {physic_nm}")

    if product_group_nm:
        bits.append(f"제품그룹명 {product_group_nm}")
    if product_class_nm:
        bits.append(f"제품분류명 {product_class_nm}")
    if maker_nm:
        bits.append(f"제조사명 {maker_nm}")
    if order_nm:
        bits.append(f"발주처명 {order_nm}")

    if stock_cds:
        bits.append("재고위치 " + ",".join(stock_cds))

    return " / ".join(bits)

def _fmt_header_num(value: Any) -> str:
    try:
        n = float(pd.to_numeric(value, errors="coerce"))
    except Exception:
        return "0"
    if abs(n) < 1e-12:
        return "0"
    if n.is_integer():
        return f"{int(n):,}"
    return f"{n:,.2f}".rstrip("0").rstrip(".")


def _build_flow_header_md(meta: Dict[str, Any]) -> str:
    info = meta.get("product_info") or {}
    if not isinstance(info, dict):
        info = {}

    line1 = (
        "제품정보: "
        f"제품코드 {clean_text(info.get('제품코드'))} / "
        f"제품명 {clean_text(info.get('제품명'))} / "
        f"규격 {clean_text(info.get('규격'))} / "
        f"최종보험가 {_fmt_header_num(info.get('최종보험가'))} / "
        f"보험코드 {clean_text(info.get('보험코드'))} / "
        f"표준코드 {clean_text(info.get('표준코드'))} / "
        f"제조사명 {clean_text(info.get('제조사명'))} / "
        f"발주처명 {clean_text(info.get('발주처명'))} / "
        f"제품그룹명 {clean_text(info.get('제품그룹명'))} / "
        f"제품분류명 {clean_text(info.get('제품분류명'))} / "
        f"특수관리제품명 {clean_text(info.get('특수관리제품명'))}"
    )

    line2 = "이월재고    입고수량    출고수량    재고수량"
    line3 = (
        f"{_fmt_header_num(meta.get('carry_qty'))}               "
        f"{_fmt_header_num(meta.get('in_qty'))}                 "
        f"{_fmt_header_num(meta.get('out_qty'))}               "
        f"{_fmt_header_num(meta.get('stock_qty'))}"
    )

    return line1 + "\n```text\n" + line2 + "\n" + line3 + "\n```"

def _flow_text_payload(
    *,
    message: str,
    params: Dict[str, Any],
    settings: Optional[Dict[str, Any]] = None,
    date_from: str = "",
    date_to: str = "",
    work_params: Optional[Dict[str, Any]] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    work_params = dict(work_params or params or {})
    meta = dict(meta or {})

    out_params = {
        **params,
        "date_from": date_from,
        "date_to": date_to,
    }

    if settings:
        out_params.update(
            {
                "stock_mode": _stock_mode_label(settings.get("stock_mode")),
                "date_basis": _date_basis_label(settings.get("date_basis")),
                "flow_scope": _flow_scope_label(settings.get("flow_scope")),
                "stock_cds": _normalize_stock_codes(work_params),
            }
        )
        query_summary = _build_flow_query_summary(
            date_from=date_from,
            date_to=date_to,
            settings=settings,
            work_params=work_params,
            params=params,
        )
        meta["query_summary"] = query_summary
        if meta.get("product_info"):
            meta["summary_md"] = _build_flow_header_md(meta)
            meta["note"] = _build_flow_header_md(meta)


    meta.setdefault("row_count", 0)
    meta.setdefault("row_count_total", 0)

    return {
        "title": TITLE,
        "action": ACTION,
        "type": "text",
        "final": True,
        "params": out_params,
        "data": message,
        "message": message,
        "meta": meta,
    }

def _to_number_series(sr: pd.Series) -> pd.Series:
    return pd.to_numeric(sr, errors="coerce").fillna(0)

_DISPLAY_NUMERIC_COLS_250 = {
    "매입단가", "매출단가", "보험가", "할인율",
    "입고수량", "출고수량", "할증", "재고수량",
    "공급가액", "부가세", "합계금액",
}


_PRODUCT_FLOW_INTEGER_ID_COLS = {"명세서번호"}
_PRODUCT_FLOW_TEXT_ID_COLS = {"제조번호", "검수확인"}


def _product_flow_integer_id_series(sr: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(sr, errors="coerce")
    numeric = numeric.mask(numeric.abs() < 1e-12)
    return numeric.round(0).astype("Int64")


def _product_flow_text_id_series(sr: pd.Series) -> pd.Series:
    work = sr.where(sr.notna(), "")
    return work.astype(str).str.strip()


def _finalize_display_df_250(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    for col in out.columns:
        if col in _PRODUCT_FLOW_INTEGER_ID_COLS:
            out[col] = _product_flow_integer_id_series(out[col])
        elif col in _PRODUCT_FLOW_TEXT_ID_COLS:
            out[col] = _product_flow_text_id_series(out[col])
        elif col in _DISPLAY_NUMERIC_COLS_250:
            s = pd.to_numeric(out[col], errors="coerce")
            s = s.mask(s.abs() < 1e-12)
            out[col] = s
        else:
            out[col] = (
                out[col]
                .fillna("")
                .astype(str)
                .str.strip()
                .replace({
                    "None": "",
                    "none": "",
                    "nan": "",
                    "NaN": "",
                    "<NA>": "",
                    "NaT": "",
                    "nat": "",
                    "NULL": "",
                    "null": "",
                })
            )
    return out


def _concat_product_flow_frames(
    frames: list[pd.DataFrame],
    *,
    columns: list[str] | None = None,
    ignore_index: bool = True,
    sort: bool = False,
) -> pd.DataFrame:
    """Concat product-flow frames without letting empty/all-NA columns decide dtypes."""
    ordered_cols: list[Any] = list(columns or [])
    if not ordered_cols:
        seen_cols: set[Any] = set()
        for df in frames:
            if not isinstance(df, pd.DataFrame):
                continue
            for col in df.columns:
                if col not in seen_cols:
                    ordered_cols.append(col)
                    seen_cols.add(col)

    concat_inputs: list[pd.DataFrame] = []
    for df in frames:
        if not isinstance(df, pd.DataFrame) or df.empty:
            continue
        work = df.copy()
        all_na_cols = [col for col in work.columns if work[col].isna().all()]
        if all_na_cols:
            work = work.drop(columns=all_na_cols)
        concat_inputs.append(work)

    if not concat_inputs:
        return pd.DataFrame(columns=ordered_cols)

    out = pd.concat(concat_inputs, ignore_index=ignore_index, sort=sort)
    if ordered_cols:
        out = out.reindex(columns=ordered_cols)
    return out


def _clean_display_df_250(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    for col in _DISPLAY_NUMERIC_COLS_250:
        if col in out.columns:
            s = pd.to_numeric(out[col], errors="coerce")
            s = s.mask(s.abs() < 1e-12)
            out[col] = s

    try:
        out = out.replace({None: "", "None": "", "nan": "", "<NA>": "", "NaT": ""})
        out = out.where(pd.notna(out), "")
    except Exception:
        pass

    return out

def _resolve_stock_mode(value: Any) -> str:
    text = clean_text(value).lower()
    if text in {"장부수불", "장부재고", "book", "books", "book_stock", "ledger"}:
        return "book"
    return "real"


def _resolve_date_basis(value: Any, stock_mode: str) -> str:
    if stock_mode == "book":
        return "trans"
    text = clean_text(value).lower()
    if text in {"명세서일자", "명세서", "trans", "statement"}:
        return "trans"
    return "io"


def _resolve_flow_scope(value: Any) -> str:
    text = clean_text(value).lower()
    if text in {"매입", "purchase", "in", "input"}:
        return "purchase"
    if text in {"매출", "sales", "out", "output"}:
        return "sales"
    return "all"


def _mode_settings(params: Dict[str, Any]) -> Dict[str, Any]:
    stock_mode = _resolve_stock_mode(
        params.get("stock_mode")
        or params.get("flow_kind")
        or params.get("stock_kind")
        or params.get("book_real")
    )
    date_basis = _resolve_date_basis(
        params.get("date_basis") or params.get("basis"),
        stock_mode,
    )
    flow_scope = _resolve_flow_scope(params.get("flow_scope") or params.get("io_scope"))

    if stock_mode == "real":
        include_bonus = True
        carry_table = "dbo.Rddbc210"
        carry_prefix_in = ("0", "1", "3", "4")
        carry_prefix_out = ("5", "6", "8", "9")
        if date_basis == "io":
            in_exclude = ("2",)
            out_exclude = ("7",)
        else:
            in_exclude = ("2", "3")
            out_exclude = ("7", "8")
    else:
        include_bonus = False
        carry_table = "dbo.Rddbc220"
        carry_prefix_in = ("0", "1", "2", "4")
        carry_prefix_out = ("5", "6", "7", "9")
        in_exclude = ("3",)
        out_exclude = ("8",)

    return {
        "stock_mode": stock_mode,
        "date_basis": date_basis,
        "flow_scope": flow_scope,
        "include_bonus": include_bonus,
        "carry_table": carry_table,
        "carry_prefix_in": carry_prefix_in,
        "carry_prefix_out": carry_prefix_out,
        "in_exclude": in_exclude,
        "out_exclude": out_exclude,
    }


def _sql_in_prefix_not(prefix_expr: str, prefixes: tuple[str, ...]) -> str:
    if not prefixes:
        return "1 = 1"
    values = ", ".join(f"'{p}'" for p in prefixes)
    return f"LEFT({prefix_expr}, 1) NOT IN ({values})"


def _sql_in_prefix_yes(prefix_expr: str, prefixes: tuple[str, ...]) -> str:
    if not prefixes:
        return "1 = 1"
    values = ", ".join(f"'{p}'" for p in prefixes)
    return f"LEFT({prefix_expr}, 1) IN ({values})"


def _detail_date_field(direction: str, date_basis: str) -> str:
    if direction == "in":
        return "T.Rd11_In_YyMmDd" if date_basis == "io" else "T.Rd11_Trans_YyMmDd"
    return "T.Rd12_Out_YyMmDd" if date_basis == "io" else "T.Rd12_Trans_YyMmDd"


def _build_detail_sql(direction: str, params: Dict[str, Any], settings: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    sql_params = dict(params)
    sql_params["top"] = normalize_top(params.get("top", 20000), default=20000, max_value=50000)

    stock_codes = _normalize_stock_codes(params)
    date_field = _detail_date_field(direction, settings["date_basis"])
    where: list[str] = ["1 = 1", f"NULLIF(LTRIM(RTRIM({date_field})), '') IS NOT NULL"]

    if clean_text(params.get("date_from")):
        where.append(f"{date_field} >= %(date_from)s")
    if clean_text(params.get("date_to")):
        where.append(f"{date_field} <= %(date_to)s")

    if direction == "in":
        where.append(_sql_in_prefix_not("T.Rd11_Io_Gu", settings["in_exclude"]))

        if clean_text(params.get("physic_cd")):
            where.append("T.Rd11_Physic_Cd = %(physic_cd)s")

        _append_stock_code_in_clause(where, sql_params, "T.Rd11_Stock_Cd", stock_codes, "in")

        sql = f"""
SELECT TOP (%(top)s)
    'IN' AS 내부방향,
    ISNULL(IoGu.Rd01_Hnm, T.Rd11_Io_Gu) AS [구분],
    T.Rd11_In_YyMmDd AS [입출고일자],
    T.Rd11_In_Seq AS [번호],
    T.Rd11_Trans_YyMmDd AS [명세서일자],
    T.Rd11_Trans_Seq AS [명세서번호],
    T.Rd11_Ven_Cd AS [코드],
    ISNULL(V.Rd03_Ven_Nm, T.Rd11_Ven_Cd) AS [거래처명],
    CAST(ISNULL(T.Rd11_Unit_Cost, 0) AS decimal(18, 2)) AS [매입단가],
    CAST(NULL AS decimal(18, 2)) AS [매출단가],
    CAST(ISNULL(T.Rd11_Insu_Price, 0) AS decimal(18, 2)) AS [보험가],
    CAST(
        CASE
            WHEN ISNULL(T.Rd11_Insu_Price, 0) = 0 THEN 0
            ELSE ROUND((1 - (ISNULL(T.Rd11_Unit_Cost, 0) / NULLIF(T.Rd11_Insu_Price, 0))) * 100, 2)
        END AS decimal(18, 2)
    ) AS [할인율],
    CAST(ISNULL(T.Rd11_Quantity, 0) AS decimal(18, 2)) AS [입고수량],
    CAST(0 AS decimal(18, 2)) AS [출고수량],
    CAST(CASE WHEN %(include_bonus_sql)s = 1 THEN ISNULL(T.Rd11_Oquantity, 0) ELSE 0 END AS decimal(18, 2)) AS [할증],
    CAST(
        ISNULL(T.Rd11_Quantity, 0)
        + CASE WHEN %(include_bonus_sql)s = 1 THEN ISNULL(T.Rd11_Oquantity, 0) ELSE 0 END
        AS decimal(18, 2)
    ) AS [재고증감],
    CAST(ISNULL(T.Rd11_Supply_Price, 0) AS decimal(18, 2)) AS [공급가액],
    CAST(ISNULL(T.Rd11_Tax_Price, 0) AS decimal(18, 2)) AS [부가세],
    CAST(ISNULL(T.Rd11_Supply_Price, 0) + ISNULL(T.Rd11_Tax_Price, 0) AS decimal(18, 2)) AS [합계금액],
    ISNULL(VSales.Rd06_User_Nm, '') AS [영업사원],
    ISNULL(T.Rd11_Other, '') AS [적요],
    ISNULL(T13.Rd13_Other, '') AS [비고],
    ISNULL(T.Rd11_Product_No, '') AS [제조번호],
    ISNULL(T.Rd11_Term_Date, '') AS [유효기한],
    ISNULL(T.RD11_Prod_Date, '') AS [제조년월],
    ISNULL(CostApply.Rd03_Ven_Nm, T.Rd11_Cost_Apply_Cd) AS [단가적용거래처],
    ISNULL(StockApply.Rd03_Ven_Nm, T.Rd11_Stock_Apply_Cd) AS [재고적용거래처],
    CASE
        WHEN NULLIF(LTRIM(RTRIM(T.Rd11_Tax_Seq)), '') IS NULL THEN ''
        ELSE CONCAT(ISNULL(T.Rd11_Tax_YyMmDd, ''), '-', ISNULL(T.Rd11_Tax_Seq, ''))
    END AS [세금계산서],
    ISNULL(StockCd.Rd01_Hnm, T.Rd11_Stock_Cd) AS [재고위치],
    '' AS [실납처코드],
    '' AS [실납품처],
    '' AS [실납처사업자번호],
    ISNULL(V.Rd03_Address, '') AS [주소],
    ISNULL(V.Rd03_Ven_Nm, T.Rd11_Ven_Cd) AS [매입처],
    ISNULL(T.Rd11_Validation, '') AS [검수확인],
    ISNULL(V.Rd03_Ven_Num, '') AS [사업자번호],
    ISNULL(V.Rd03_Owner_Nm, '') AS [대표자명],
    ISNULL(V.Rd03_Phone, '') AS [전화번호],
    ISNULL(V.Rd03_Zip_Code, '') AS [우편번호],

    ISNULL(T.Rd11_Add_Time, T.Rd11_Add_Date) AS [등록일자],
    ISNULL(AddU.Rd06_User_Nm, '') AS [등록자],
    ISNULL(T.Rd11_Mod_Time, T.Rd11_Mod_Date) AS [수정일자],
    ISNULL(ModU.Rd06_User_Nm, '') AS [수정자],
    ISNULL(VKind.Rd01_Hnm, '') AS [거래처종류],
    T.Rd11_In_YyMmDd AS [정렬일자],
    T.Rd11_In_Seq AS [정렬순번]
FROM dbo.Rddbc110 AS T
LEFT JOIN dbo.Rddbc030 AS V
    ON T.Rd11_Ven_Cd = V.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS CostApply
    ON T.Rd11_Cost_Apply_Cd = CostApply.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS StockApply
    ON T.Rd11_Stock_Apply_Cd = StockApply.Rd03_Ven_Cd

LEFT JOIN dbo.Rddbc010 AS StockCd
    ON T.Rd11_Stock_Cd_Gcode = StockCd.Rd01_Gcode
   AND T.Rd11_Stock_Cd = StockCd.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS IoGu
    ON T.Rd11_Io_Gu_Gcode = IoGu.Rd01_Gcode
   AND T.Rd11_Io_Gu = IoGu.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS VKind
    ON V.Rd03_Ven_Kind_Gcode = VKind.Rd01_Gcode
   AND V.Rd03_Ven_Kind = VKind.Rd01_Tcode
LEFT JOIN dbo.Rddbc060 AS VSales
    ON V.Rd03_Sales_Man = VSales.Rd06_User_Cd
LEFT JOIN dbo.Rddbc060 AS AddU
    ON T.Rd11_Add_Cd = AddU.Rd06_User_Cd
LEFT JOIN dbo.Rddbc060 AS ModU
    ON T.Rd11_Mod_Cd = ModU.Rd06_User_Cd
LEFT JOIN dbo.Rddbc130 AS T13
    ON T.Rd11_Trans_Di = T13.Rd13_Trans_Di
   AND T.Rd11_Trans_YyMmDd = T13.Rd13_Trans_YyMmDd
   AND T.Rd11_Ven_Cd = T13.Rd13_Ven_Cd
   AND T.Rd11_Trans_Seq = T13.Rd13_Trans_Seq
WHERE {" AND ".join(where)}
ORDER BY T.Rd11_In_YyMmDd, T.Rd11_In_Seq
"""
    else:
        where.append(_sql_in_prefix_not("T.Rd12_Io_Gu", settings["out_exclude"]))

        if clean_text(params.get("physic_cd")):
            where.append("T.Rd12_Physic_Cd = %(physic_cd)s")

        _append_stock_code_in_clause(where, sql_params, "T.Rd12_Stock_Cd", stock_codes, "out")

        sql = f"""
SELECT TOP (%(top)s)
    'OUT' AS 내부방향,
    ISNULL(IoGu.Rd01_Hnm, T.Rd12_Io_Gu) AS [구분],
    T.Rd12_Out_YyMmDd AS [입출고일자],
    T.Rd12_Out_Seq AS [번호],
    T.Rd12_Trans_YyMmDd AS [명세서일자],
    T.Rd12_Trans_Seq AS [명세서번호],
    T.Rd12_Ven_Cd AS [코드],
    ISNULL(V.Rd03_Ven_Nm, T.Rd12_Ven_Cd) AS [거래처명],
    CAST(ISNULL(T.Rd12_In_Unit_Cost, 0) AS decimal(18, 2)) AS [매입단가],
    CAST(ISNULL(T.Rd12_Unit_Cost, 0) AS decimal(18, 2)) AS [매출단가],
    CAST(ISNULL(T.Rd12_Insu_Price, 0) AS decimal(18, 2)) AS [보험가],
    CAST(0 AS decimal(18, 2)) AS [할인율],
    CAST(0 AS decimal(18, 2)) AS [입고수량],
    CAST(ISNULL(T.Rd12_Quantity, 0) AS decimal(18, 2)) AS [출고수량],
    CAST(CASE WHEN %(include_bonus_sql)s = 1 THEN ISNULL(T.Rd12_Oquantity, 0) ELSE 0 END AS decimal(18, 2)) AS [할증],
    CAST(
        0
        - ISNULL(T.Rd12_Quantity, 0)
        - CASE WHEN %(include_bonus_sql)s = 1 THEN ISNULL(T.Rd12_Oquantity, 0) ELSE 0 END
        AS decimal(18, 2)
    ) AS [재고증감],
    CAST(ISNULL(T.Rd12_Supply_Price, 0) AS decimal(18, 2)) AS [공급가액],
    CAST(ISNULL(T.Rd12_Tax_Price, 0) AS decimal(18, 2)) AS [부가세],
    CAST(ISNULL(T.Rd12_Supply_Price, 0) + ISNULL(T.Rd12_Tax_Price, 0) AS decimal(18, 2)) AS [합계금액],
    ISNULL(RowSales.Rd06_User_Nm, ISNULL(VSales.Rd06_User_Nm, '')) AS [영업사원],
    ISNULL(T.Rd12_Other, '') AS [적요],
    ISNULL(T13.Rd13_Other, '') AS [비고],
    ISNULL(T.Rd12_Product_No, '') AS [제조번호],
    ISNULL(T.Rd12_Term_Date, '') AS [유효기한],
    ISNULL(T.RD12_Prod_Date, '') AS [제조년월],
    ISNULL(CostApply.Rd03_Ven_Nm, T.Rd12_Cost_Apply_Cd) AS [단가적용거래처],
    ISNULL(StockApply.Rd03_Ven_Nm, T.Rd12_Stock_Apply_Cd) AS [재고적용거래처],
    CASE
        WHEN NULLIF(LTRIM(RTRIM(T.Rd12_Tax_Seq)), '') IS NULL THEN ''
        ELSE CONCAT(ISNULL(T.Rd12_Tax_YyMmDd, ''), '-', ISNULL(T.Rd12_Tax_Seq, ''))
    END AS [세금계산서],
    ISNULL(StockCd.Rd01_Hnm, T.Rd12_Stock_Cd) AS [재고위치],
    ISNULL(T.Rd12_Real_Ven_Cd, '') AS [실납처코드],
    ISNULL(RealV.Rd03_Ven_Nm, '') AS [실납품처],
    ISNULL(RealV.Rd03_Ven_Num, '') AS [실납처사업자번호],
    ISNULL(V.Rd03_Address, '') AS [주소],
    ISNULL(InVen.Rd03_Ven_Nm, T.Rd12_In_Ven_Cd) AS [매입처],
    ISNULL(T.Rd12_Validation, '') AS [검수확인],
    ISNULL(V.Rd03_Ven_Num, '') AS [사업자번호],
    ISNULL(V.Rd03_Owner_Nm, '') AS [대표자명],
    ISNULL(V.Rd03_Phone, '') AS [전화번호],
    ISNULL(V.Rd03_Zip_Code, '') AS [우편번호],

    ISNULL(T.Rd12_Add_Time, T.Rd12_Add_Date) AS [등록일자],
    ISNULL(AddU.Rd06_User_Nm, '') AS [등록자],
    ISNULL(T.Rd12_Mod_Time, T.Rd12_Mod_Date) AS [수정일자],
    ISNULL(ModU.Rd06_User_Nm, '') AS [수정자],
    ISNULL(VKind.Rd01_Hnm, '') AS [거래처종류],
    T.Rd12_Out_YyMmDd AS [정렬일자],
    T.Rd12_Out_Seq AS [정렬순번]
FROM dbo.Rddbc120 AS T
LEFT JOIN dbo.Rddbc030 AS V
    ON T.Rd12_Ven_Cd = V.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS InVen
    ON T.Rd12_In_Ven_Cd = InVen.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS RealV
    ON T.Rd12_Real_Ven_Cd = RealV.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS CostApply
    ON T.Rd12_Cost_Apply_Cd = CostApply.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS StockApply
    ON T.Rd12_Stock_Apply_Cd = StockApply.Rd03_Ven_Cd

LEFT JOIN dbo.Rddbc010 AS StockCd
    ON T.Rd12_Stock_Cd_Gcode = StockCd.Rd01_Gcode
   AND T.Rd12_Stock_Cd = StockCd.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS IoGu
    ON T.Rd12_Io_Gu_Gcode = IoGu.Rd01_Gcode
   AND T.Rd12_Io_Gu = IoGu.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS VKind
    ON V.Rd03_Ven_Kind_Gcode = VKind.Rd01_Gcode
   AND V.Rd03_Ven_Kind = VKind.Rd01_Tcode
LEFT JOIN dbo.Rddbc060 AS RowSales
    ON T.Rd12_Sales_Man = RowSales.Rd06_User_Cd
LEFT JOIN dbo.Rddbc060 AS VSales
    ON V.Rd03_Sales_Man = VSales.Rd06_User_Cd
LEFT JOIN dbo.Rddbc060 AS AddU
    ON T.Rd12_Add_Cd = AddU.Rd06_User_Cd
LEFT JOIN dbo.Rddbc060 AS ModU
    ON T.Rd12_Mod_Cd = ModU.Rd06_User_Cd
LEFT JOIN dbo.Rddbc130 AS T13
    ON T.Rd12_Trans_Di = T13.Rd13_Trans_Di
   AND T.Rd12_Trans_YyMmDd = T13.Rd13_Trans_YyMmDd
   AND T.Rd12_Ven_Cd = T13.Rd13_Ven_Cd
   AND T.Rd12_Trans_Seq = T13.Rd13_Trans_Seq
WHERE {" AND ".join(where)}
ORDER BY T.Rd12_Out_YyMmDd, T.Rd12_Out_Seq
"""
    sql_params["include_bonus_sql"] = 1 if settings["include_bonus"] else 0
    return sql, sql_params

def _get_detail_df(params: Dict[str, Any], settings: Dict[str, Any]) -> pd.DataFrame:
    frames = []

    if settings["flow_scope"] in {"all", "purchase"}:
        in_sql, in_params = _build_detail_sql("in", params, settings)
        frames.append(query_to_df(in_sql, in_params))

    if settings["flow_scope"] in {"all", "sales"}:
        out_sql, out_params = _build_detail_sql("out", params, settings)
        frames.append(query_to_df(out_sql, out_params))

    frames = [df for df in frames if isinstance(df, pd.DataFrame) and not df.empty]
    if not frames:
        return pd.DataFrame()

    return _concat_product_flow_frames(frames, ignore_index=True, sort=False)


def _build_month_carry_sql(params: Dict[str, Any], settings: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    base_month = clean_text(params.get("base_month"))
    sql_params = dict(params)
    sql_params["base_month"] = base_month
    stock_codes = _normalize_stock_codes(params)

    qty_expr = (
        "(ISNULL(M.Rd21_In_Quantity, 0) + ISNULL(M.Rd21_In_Oquantity, 0)) - "
        "(ISNULL(M.Rd21_Out_Quantity, 0) + ISNULL(M.Rd21_Out_Oquantity, 0))"
        if settings["stock_mode"] == "real"
        else "(ISNULL(M.Rd22_In_Quantity, 0)) - (ISNULL(M.Rd22_Out_Quantity, 0))"
    )

    month_field = "M.Rd21_Stock_YyMm" if settings["stock_mode"] == "real" else "M.Rd22_Stock_YyMm"
    physic_field = "M.Rd21_Physic_Cd" if settings["stock_mode"] == "real" else "M.Rd22_Physic_Cd"
    stock_field = "M.Rd21_Stock_Cd" if settings["stock_mode"] == "real" else "M.Rd22_Stock_Cd"
    stock_g_field = "M.Rd21_Stock_Cd_Gcode" if settings["stock_mode"] == "real" else "M.Rd22_Stock_Cd_Gcode"
    io_field = "M.Rd21_Io_Gu" if settings["stock_mode"] == "real" else "M.Rd22_Io_Gu"

    where = [
        "1 = 1",
        f"{month_field} < %(base_month)s",
        f"NULLIF(LTRIM(RTRIM({month_field})), '') IS NOT NULL",
    ]

    allowed = settings["carry_prefix_in"] + settings["carry_prefix_out"]
    where.append(_sql_in_prefix_yes(io_field, allowed))

    if clean_text(params.get("physic_cd")):
        where.append(f"{physic_field} = %(physic_cd)s")

    _append_stock_code_in_clause(where, sql_params, stock_field, stock_codes, "carry")

    sql = f"""
SELECT
    CAST(ISNULL(SUM({qty_expr}), 0) AS decimal(18, 2)) AS carry_qty
FROM {settings['carry_table']} AS M
WHERE {" AND ".join(where)}
"""
    return sql, sql_params

def _get_month_carry_qty(params: Dict[str, Any], settings: Dict[str, Any]) -> float:
    sql, sql_params = _build_month_carry_sql(params, settings)
    df = query_to_df(sql, sql_params)
    if df is None or df.empty:
        return 0.0
    try:
        return float(pd.to_numeric(df.iloc[0]["carry_qty"], errors="coerce") or 0.0)
    except Exception:
        return 0.0


def _get_current_month_carry_qty(params: Dict[str, Any], settings: Dict[str, Any], date_from: str) -> float:
    dt = datetime.strptime(date_from, "%Y%m%d")
    month_first = dt.replace(day=1).strftime("%Y%m%d")
    prev_day = (dt - timedelta(days=1)).strftime("%Y%m%d")

    if month_first > prev_day:
        return 0.0

    carry_params = dict(params)
    carry_params["date_from"] = month_first
    carry_params["date_to"] = prev_day
    carry_params["top"] = 50000

    df = _get_detail_df(carry_params, settings)
    if df is None or df.empty:
        return 0.0

    return float(_to_number_series(df["재고증감"]).sum())

def _get_product_master_info(params: Dict[str, Any]) -> Dict[str, Any]:
    physic_cd = clean_text(params.get("physic_cd"))
    if not physic_cd:
        return {}

    sql = """
SELECT TOP 1
    RTRIM(P.Rd04_Physic_Cd) AS [제품코드],
    RTRIM(P.Rd04_Physic_Nm) AS [제품명],
    RTRIM(P.Rd04_Standard) AS [규격],
    RTRIM(P.Rd04_Pack_Unit) AS [포장단위],
    CAST(
        ISNULL(P.Rd04_Insu_Price, 0) *
        CASE WHEN ISNULL(P.Rd04_Acc_Unit, 0) = 0 THEN 1 ELSE P.Rd04_Acc_Unit END
        AS decimal(18, 2)
    ) AS [최종보험가],
    RTRIM(P.Rd04_Insu_Cd) AS [보험코드],
    RTRIM(ISNULL(P046.Rd046_Standard_Cd, '')) AS [표준코드],
    RTRIM(ISNULL(MakerVen.Rd03_Ven_Nm, '')) AS [제조사명],
    RTRIM(ISNULL(OrVen.Rd03_Ven_Nm, '')) AS [발주처명],
    RTRIM(ISNULL(PG.Rd01_Hnm, '')) AS [제품그룹명],
    RTRIM(ISNULL(PT.Rd01_Hnm, '')) AS [제품분류명],
    RTRIM(ISNULL(Physic_Gu.Rd01_Hnm, '')) AS [특수관리제품명]
FROM dbo.Rddbc040 AS P
LEFT JOIN dbo.Rddbc046 AS P046
    ON P.Rd04_Physic_Cd = P046.Rd046_Physic_Cd
LEFT JOIN dbo.Rddbc030 AS MakerVen
    ON P.Rd04_Ven_Cd = MakerVen.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS OrVen
    ON P.Rd04_OrVen_Cd = OrVen.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc010 AS PG
    ON P.Rd04_Physic_Group_Gcode = PG.Rd01_Gcode
   AND P.Rd04_Physic_Group = PG.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS PT
    ON P.Rd04_Physic_Tax_Gcode = PT.Rd01_Gcode
   AND P.Rd04_Physic_Tax = PT.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS Physic_Gu
    ON P.Rd04_Physic_Gu_Gcode = Physic_Gu.Rd01_Gcode
   AND P.Rd04_Physic_Gu = Physic_Gu.Rd01_Tcode   
WHERE P.Rd04_Physic_Cd = %(physic_cd)s
"""

    df = query_to_df(sql, {"physic_cd": physic_cd})
    if df is None or df.empty:
        return {}

    row = df.iloc[0].to_dict()
    out: Dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, str):
            out[str(k)] = v.strip()
        else:
            out[str(k)] = v
    return out

def _prepare_display_df(
    df: pd.DataFrame,
    carry_qty: float,
    settings: Dict[str, Any],
    params: Dict[str, Any],
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    display_cols = [
        "순번", "구분", "입출고일자", "번호", "명세서일자", "명세서번호", "코드", "거래처명",
        "매입단가", "매출단가", "보험가", "할인율", "입고수량", "출고수량", "할증", "재고수량",
        "공급가액", "부가세", "합계금액", "영업사원", "적요", "비고", "제조번호", "유효기한",
        "제조년월", "단가적용거래처", "재고적용거래처", "세금계산서", "재고위치", "실납처코드",
        "실납품처", "실납처사업자번호", "주소", "매입처", "검수확인", "사업자번호", "대표자명",
        "전화번호", "우편번호", "등록일자", "등록자", "수정일자", "수정자", "거래처종류",
    ]

    stock_names = params.get("stock_names") or []
    if not isinstance(stock_names, list):
        stock_names = []

    fallback_product_cd = clean_text(params.get("physic_cd"))
    fallback_product_nm = clean_text(params.get("physic_nm"))
    fallback_stock_nm = ", ".join([str(x).strip() for x in stock_names if str(x).strip()])

    raw_master_info = params.get("__product_info__")
    master_info = raw_master_info if isinstance(raw_master_info, dict) else {}

    def _blank_row() -> Dict[str, Any]:
        return {
            col: (None if col in _DISPLAY_NUMERIC_COLS_250 else "")
            for col in display_cols
        }

    def _safe_num(value: Any) -> float:
        try:
            v = float(pd.to_numeric(value, errors="coerce"))
            if abs(v) < 1e-12:
                return 0.0
            return v
        except Exception:
            return 0.0

    def _build_product_info(source_row: Dict[str, Any] | None = None) -> Dict[str, Any]:
        src = source_row or {}

        def _pick_text(key: str, fallback: str = "") -> str:
            v = master_info.get(key, src.get(key, fallback))
            return str(v or "").strip()

        def _pick_num(key: str, fallback: float = 0.0) -> float:
            v = master_info.get(key, src.get(key, fallback))
            try:
                n = float(pd.to_numeric(v, errors="coerce"))
                if abs(n) < 1e-12:
                    return 0.0
                return n
            except Exception:
                return 0.0

        return {
            "제품코드": _pick_text("제품코드", fallback_product_cd),
            "제품명": _pick_text("제품명", fallback_product_nm),
            "규격": _pick_text("규격"),
            "포장단위": _pick_text("포장단위"),
            "최종보험가": _pick_num("최종보험가", 0.0),
            "보험코드": _pick_text("보험코드"),
            "표준코드": _pick_text("표준코드"),
            "제조사명": _pick_text("제조사명"),
            "발주처명": _pick_text("발주처명"),
            "제품그룹명": _pick_text("제품그룹명"),
            "제품분류명": _pick_text("제품분류명"),
            "특수관리제품명": _pick_text("특수관리제품명"),
        }

    def _make_carry_row(source_row: Dict[str, Any] | None = None) -> Dict[str, Any]:
        src = source_row or {}
        row = _blank_row()
        row["입출고일자"] = "이월재고"
        row["재고수량"] = carry_qty
        row["재고위치"] = str(src.get("재고위치", "")).strip() or fallback_stock_nm
        return row

    if df is None or df.empty:
        product_info = _build_product_info()
        carry_row = _make_carry_row()

        out = pd.DataFrame([carry_row], columns=display_cols)
        out = _finalize_display_df_250(out)

        meta = {
            "product_info": product_info,
            "carry_qty": float(carry_qty),
            "in_qty": 0.0,
            "out_qty": 0.0,
            "stock_qty": float(carry_qty),
            "row_count": int(len(out)),
            "stock_mode": settings["stock_mode"],
            "date_basis": settings["date_basis"],
            "flow_scope": settings["flow_scope"],
        }
        return out, meta

    work = df.copy()

    for col in display_cols:
        if col not in work.columns:
            work[col] = None if col in _DISPLAY_NUMERIC_COLS_250 else ""

    work["정렬일자"] = work["정렬일자"].astype(str)
    work["정렬순번"] = _to_number_series(work["정렬순번"])
    work["재고증감"] = _to_number_series(work["재고증감"])
    work["입고수량"] = _to_number_series(work["입고수량"])
    work["출고수량"] = _to_number_series(work["출고수량"])
    work["할증"] = _to_number_series(work["할증"])
    work["공급가액"] = _to_number_series(work["공급가액"])
    work["부가세"] = _to_number_series(work["부가세"])
    work["합계금액"] = _to_number_series(work["합계금액"])

    work = work.sort_values(
        ["정렬일자", "정렬순번", "내부방향"],
        ascending=[True, True, True],
    ).reset_index(drop=True)

    work["재고수량"] = carry_qty + work["재고증감"].cumsum()
    work["순번"] = range(1, len(work) + 1)

    for col in ("입출고일자", "명세서일자"):
        work[col] = work[col].map(_fmt_date_text)

    first_src = {}
    try:
        first_src = work.iloc[0].to_dict()
    except Exception:
        first_src = {}

    product_info = _build_product_info(first_src)
    carry_row = _make_carry_row(first_src)

    out = _concat_product_flow_frames(
        [
            pd.DataFrame([carry_row], columns=display_cols),
            work[display_cols],
        ],
        ignore_index=True,
        columns=display_cols,
    )
    out = _finalize_display_df_250(out)

    meta = {
        "product_info": product_info,
        "carry_qty": float(carry_qty),
        "in_qty": float(work["입고수량"].sum() + work.loc[work["내부방향"] == "IN", "할증"].sum()),
        "out_qty": float(work["출고수량"].sum() + work.loc[work["내부방향"] == "OUT", "할증"].sum()),
        "stock_qty": float(carry_qty + work["재고증감"].sum()),
        "row_count": int(len(out)),
        "stock_mode": settings["stock_mode"],
        "date_basis": settings["date_basis"],
        "flow_scope": settings["flow_scope"],
    }
    return out, meta


def get_product_flow_df(params: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    params = coalesce_params(params)
    settings = _mode_settings(params)

    date_from, date_to = _resolve_flow_dates(params)    

    physic_cd = clean_text(params.get("physic_cd"))
    if not physic_cd:
        physic_nm = clean_text(params.get("physic_nm"))
        if physic_nm:
            cand_df = _find_product_candidates_by_name(physic_nm, top=20)
            log.info(
                "[product_flow] product candidate search physic_nm=%r rows=%s",
                physic_nm,
                0 if cand_df is None else len(cand_df),
            )            
            if len(cand_df) == 1:
                params = dict(params)
                params["physic_cd"] = clean_text(cand_df.iloc[0].get("제품코드"))
                params["physic_nm"] = clean_text(cand_df.iloc[0].get("제품명"))
                physic_cd = clean_text(params.get("physic_cd"))
            elif cand_df.empty:
                raise ValueError(f"제품명 '{physic_nm}' 후보가 없습니다.")
            else:
                raise ValueError(
                    f"제품명 '{physic_nm}' 후보가 {len(cand_df):,}건입니다. 제품코드로 다시 조회해 주세요."
                )

    if not physic_cd:
        raise ValueError("제품수불현황은 제품코드 1개를 반드시 지정해야 합니다.")

    work_params = dict(params)
    work_params["date_from"] = date_from
    work_params["date_to"] = date_to
    work_params["base_month"] = date_from[:6]
    work_params["top"] = normalize_top(params.get("top", 20000), default=20000, max_value=50000)

    work_params["__product_info__"] = _get_product_master_info(work_params)

    month_carry = _get_month_carry_qty(work_params, settings)
    current_month_carry = _get_current_month_carry_qty(work_params, settings, date_from)
    carry_qty = month_carry + current_month_carry

    df_detail = _get_detail_df(work_params, settings)
    df_display, _ = _prepare_display_df(df_detail, carry_qty, settings, work_params)

    return df_display

    log.info(
        "[product_flow] product candidate search physic_nm=%r rows=%s",
        physic_nm,
        0 if cand_df is None else len(cand_df),
    )


def _normalize_stock_codes(params: Dict[str, Any]) -> list[str]:
    raw = params.get("stock_cds")
    values: list[str] = []

    if isinstance(raw, (list, tuple, set)):
        values = [clean_text(x) for x in raw]
    else:
        raw_csv = clean_text(params.get("stock_cd_csv") or params.get("stock_cd"))
        if raw_csv:
            values = [clean_text(x) for x in str(raw_csv).split(",")]

    out: list[str] = []
    seen = set()
    for v in values:
        if v and v not in seen:
            out.append(v)
            seen.add(v)
    return out

def _find_product_candidates_by_name(name: str, top: int = 20) -> pd.DataFrame:
    kw = clean_text(name)
    if not kw:
        return pd.DataFrame()

    sql = """
SELECT TOP (%(top)s)
    LTRIM(RTRIM(P.Rd04_Physic_Cd)) AS 제품코드,
    LTRIM(RTRIM(P.Rd04_Physic_Nm)) AS 제품명,
    LTRIM(RTRIM(ISNULL(Maker.Rd03_Ven_Nm, ''))) AS 제조사명,
    LTRIM(RTRIM(ISNULL(PG.Rd01_Hnm, ''))) AS 제품그룹명,
    LTRIM(RTRIM(ISNULL(PD.Rd01_Hnm, ''))) AS 제품구분명
FROM dbo.Rddbc040 AS P
LEFT JOIN dbo.Rddbc030 AS Maker
       ON P.Rd04_Ven_Cd = Maker.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc010 AS PG
       ON P.Rd04_Physic_Group_Gcode = PG.Rd01_Gcode
      AND P.Rd04_Physic_Group = PG.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS PD
       ON P.Rd04_Physic_Di_Gcode = PD.Rd01_Gcode
      AND P.Rd04_Physic_Di = PD.Rd01_Tcode
WHERE ISNULL(P.Rd04_Del_Flag, '') <> 'E'
  AND P.Rd04_Physic_Nm LIKE %(physic_nm_like)s
ORDER BY
    CASE WHEN P.Rd04_Physic_Nm = %(physic_nm_exact)s THEN 0 ELSE 1 END,
    P.Rd04_Physic_Nm,
    P.Rd04_Physic_Cd
"""
    try:
        df = query_to_df(
            sql,
            {
                "top": int(top),
                "physic_nm_like": f"%{kw}%",
                "physic_nm_exact": kw,
            },
        )
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _build_product_candidate_rows(cand_df: pd.DataFrame, top: int = 20) -> list[dict]:
    if cand_df is None or cand_df.empty:
        return []

    rows: list[dict] = []
    for i, (_, row) in enumerate(cand_df.head(top).iterrows(), start=1):
        rows.append(
            {
                "번호": i,
                "제품코드": clean_text(row.get("제품코드")),
                "제품명": clean_text(row.get("제품명")),
                "제조사명": clean_text(row.get("제조사명")),
                "제품그룹명": clean_text(row.get("제품그룹명")),
                "제품구분명": clean_text(row.get("제품구분명")),
            }
        )
    return rows


def _build_product_candidate_message(physic_nm: str, cand_df: pd.DataFrame) -> str:
    if cand_df is None or cand_df.empty:
        return (
            f"제품명 '{physic_nm}' 후보가 없습니다.\n"
            f"제품명 일부를 조금 더 길게 입력하거나 제품코드로 조회해 주세요."
        )

    rows = _build_product_candidate_rows(cand_df, top=20)
    total = int(len(cand_df))

    lines = [
        f"제품명 '{physic_nm}' 후보 {total:,}건입니다.",
        "원하는 번호를 입력해 주세요. 예: 1번 / 첫번째 제품",
        "",
    ]

    for row in rows:
        tail = " / ".join(
            [x for x in [row["제조사명"], row["제품그룹명"], row["제품구분명"]] if x]
        )
        if tail:
            lines.append(f'{row["번호"]}. {row["제품코드"]} | {row["제품명"]} | {tail}')
        else:
            lines.append(f'{row["번호"]}. {row["제품코드"]} | {row["제품명"]}')

    if total > len(rows):
        lines.append(f"... 외 {total - len(rows)}건")

    return "\n".join(lines)

def _flow_candidate_table_payload(
    *,
    physic_nm: str,
    cand_df: pd.DataFrame,
    candidate_rows: list[dict],
    params: Dict[str, Any],
    settings: Dict[str, Any],
    date_from: str,
    date_to: str,
) -> Dict[str, Any]:
    """
    제품수불현황 제품명 후보가 여러 건일 때,
    텍스트가 아니라 후보표로 보여주기 위한 payload.
    """
    df_candidates = pd.DataFrame(candidate_rows)

    if df_candidates is None or df_candidates.empty:
        return _flow_text_payload(
            message=_build_product_candidate_message(physic_nm, cand_df),
            params=params,
            settings=settings,
            date_from=date_from,
            date_to=date_to,
            meta={
                "result_status": "no_data",
                "pending_product_candidates": [],
                "pending_product_action": ACTION,
                "pending_product_params": {
                    **params,
                    "physic_cd": "",
                    "physic_nm": physic_nm,

                    # 후보 선택 후 "1번"으로 실제 조회할 때도
                    # 최초 조회 시 적용된 기본기간/기준값이 유지되어야 한다.
                    "date_from": date_from,
                    "date_to": date_to,
                    "stock_mode": settings.get("stock_mode"),
                    "date_basis": settings.get("date_basis"),
                    "flow_scope": settings.get("flow_scope"),
                },
            },
        )

    message = (
        f"제품명 '{physic_nm}' 후보 {len(candidate_rows):,}건입니다.\n"
        "원하는 번호를 입력해 주세요. 예: 1번 / 첫번째 제품"
    )

    meta = {
        "result_status": "candidate_required",
        "row_count": int(len(df_candidates)),
        "row_count_total": int(len(cand_df)) if isinstance(cand_df, pd.DataFrame) else int(len(df_candidates)),
        "query_summary": f"제품명 후보검색 {physic_nm}",
        "condition": f"제품명 후보검색 {physic_nm}",
        "summary_md": message,
        "pending_product_candidates": candidate_rows,
        "pending_product_action": ACTION,
        "pending_product_params": {
            **params,
            "physic_cd": "",
            "physic_nm": physic_nm,
        },
        "candidate_table": True,
    }

    return {
        "title": "제품수불현황 제품 후보",
        "action": ACTION,
        "type": "table",
        "final": True,
        "params": {
            **params,
            "date_from": date_from,
            "date_to": date_to,
            "stock_mode": _stock_mode_label(settings["stock_mode"]),
            "date_basis": _date_basis_label(settings["date_basis"]),
            "flow_scope": _flow_scope_label(settings["flow_scope"]),
        },
        "message": message,
        "df": df_candidates,
        "df_display": df_candidates,
        "columns": list(df_candidates.columns),
        "records": df_candidates.to_dict(orient="records"),
        "meta": meta,
    }

def _build_product_name_candidate_message(physic_nm: str, cand_df: pd.DataFrame) -> str:
    if cand_df is None or cand_df.empty:
        return (
            f"제품명 '{physic_nm}' 후보가 없습니다.\n"
            f"제품명 일부를 조금 더 길게 입력하거나 제품코드로 조회해 주세요."
        )

    total = int(len(cand_df))
    lines = [
        f"제품명 '{physic_nm}' 후보가 {total:,}건 검색되었습니다.",
        "아래 제품코드 중 하나로 다시 조회해 주세요.",
        "",
    ]

    show_n = min(total, 15)
    for _, row in cand_df.head(show_n).iterrows():
        code = clean_text(row.get("제품코드"))
        name = clean_text(row.get("제품명"))
        maker = clean_text(row.get("제조사명"))
        group_nm = clean_text(row.get("제품그룹명"))
        di_nm = clean_text(row.get("제품구분명"))

        tail = " / ".join([x for x in [maker, group_nm, di_nm] if x])
        if tail:
            lines.append(f"- {code} | {name} | {tail}")
        else:
            lines.append(f"- {code} | {name}")

    if total > show_n:
        lines.append(f"... 외 {total - show_n}건")

    lines.append("")
    lines.append(f"예: 제품수불현황 제품 00052 조회")

    return "\n".join(lines)

def _append_stock_code_in_clause(
    where: list[str],
    sql_params: Dict[str, Any],
    field_expr: str,
    stock_codes: list[str],
    prefix: str,
) -> None:
    if not stock_codes:
        return

    bind_keys = []
    for i, code in enumerate(stock_codes):
        key = f"{prefix}_stock_cd_{i}"
        sql_params[key] = code
        bind_keys.append(f"%({key})s")

    where.append(f"{field_expr} IN ({', '.join(bind_keys)})")

def get_product_flow_result(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    params = coalesce_params(params)
    settings = _mode_settings(params)

    # NLQ 외 직접 호출도 제품 조건 없이 후보/수불 SQL로 진행하지 않는다.
    if not clean_text(params.get("physic_cd")) and not clean_text(params.get("physic_nm")):
        return _flow_text_payload(
            message="제품수불현황은 제품 1개를 먼저 지정해 주세요. 예: 제품수불현황 제품명 우루사 조회",
            params=params,
            meta={
                "input_required": True,
                "result_status": "input_required",
                "row_count": 0,
                "row_count_total": 0,
            },
        )

    try:
        date_from, date_to = _resolve_flow_dates(params)

        physic_cd = clean_text(params.get("physic_cd"))
        if not physic_cd:
            physic_nm = clean_text(params.get("physic_nm"))

            if physic_nm:
                cand_df = _find_product_candidates_by_name(physic_nm, top=20)

                log.info(
                    "[product_flow] product candidate search physic_nm=%r rows=%s",
                    physic_nm,
                    0 if cand_df is None else len(cand_df),
                )

                if len(cand_df) == 1:
                    params = dict(params)
                    params["physic_cd"] = clean_text(cand_df.iloc[0].get("제품코드"))
                    params["physic_nm"] = clean_text(cand_df.iloc[0].get("제품명"))
                    physic_cd = clean_text(params.get("physic_cd"))
                else:
                    candidate_rows = _build_product_candidate_rows(cand_df, top=20)

                    log.info(
                        "[product_flow] product candidate payload physic_nm=%r candidate_rows=%s",
                        physic_nm,
                        len(candidate_rows),
                    )

                    return _flow_candidate_table_payload(
                        physic_nm=physic_nm,
                        cand_df=cand_df,
                        candidate_rows=candidate_rows,
                        params=params,
                        settings=settings,
                        date_from=date_from,
                        date_to=date_to,
                    )
                
            if not physic_cd:
                return _flow_text_payload(
                    message="제품수불현황은 제품 1개를 먼저 지정해 주세요. 예: 제품수불현황 제품명 우루사 조회",
                    params=params,
                    settings=settings,
                    date_from=date_from,
                    date_to=date_to,
                    meta={
                        "input_required": True,
                        "result_status": "input_required",
                        "row_count": 0,
                        "row_count_total": 0,
                    },
                )

        work_params = dict(params)
        work_params["date_from"] = date_from
        work_params["date_to"] = date_to
        work_params["base_month"] = date_from[:6]
        work_params["top"] = normalize_top(params.get("top", 20000), default=20000, max_value=50000)

        # Reuse the current-stock location resolver.  A named location must
        # never silently degrade to an unfiltered product-flow query.
        requested_stock_name = clean_text(
            work_params.get("stock_nm")
            or work_params.get("stock_name")
            or work_params.get("stock_location_nm")
        )
        resolved_stock_codes = resolve_inventory_stock_codes(work_params)
        if resolved_stock_codes:
            work_params["stock_cds"] = resolved_stock_codes
            if len(resolved_stock_codes) == 1:
                work_params["stock_cd"] = resolved_stock_codes[0]
        elif requested_stock_name:
            return _flow_text_payload(
                message="입력한 재고위치에 해당하는 코드가 없습니다. 재고위치명 또는 코드를 확인해 주세요.",
                params=params,
                settings=settings,
                date_from=date_from,
                date_to=date_to,
                work_params=work_params,
                meta={
                    "result_status": "no_data",
                    "row_count": 0,
                    "row_count_total": 0,
                    "tableless_result": True,
                },
            )

        work_params["__product_info__"] = _get_product_master_info(work_params)

        month_carry = _get_month_carry_qty(work_params, settings)
        current_month_carry = _get_current_month_carry_qty(work_params, settings, date_from)
        carry_qty = month_carry + current_month_carry

        df_detail = _get_detail_df(work_params, settings)
        df_display, meta = _prepare_display_df(df_detail, carry_qty, settings, work_params)

        if df_display is None or df_display.empty:
            return _flow_text_payload(
                message="조회 결과가 없습니다.",
                params=params,
                settings=settings,
                date_from=date_from,
                date_to=date_to,
                work_params=work_params,
                meta={
                    **dict(meta or {}),
                    "result_status": "no_data",
                    "row_count": 0,
                    "row_count_total": 0,
                },
            )

        detail_count = len(df_display)
        try:
            if (
                isinstance(df_display, pd.DataFrame)
                and not df_display.empty
                and "입출고일자" in df_display.columns
                and str(df_display.iloc[0]["입출고일자"]).strip() == "이월재고"
            ):
                detail_count -= 1
        except Exception:
            pass

        query_summary = _build_flow_query_summary(
            date_from=date_from,
            date_to=date_to,
            settings=settings,
            work_params=work_params,
            params=params,
        )
        count_note = f"조회건수: {max(detail_count, 0):,}건"

        payload: Dict[str, Any] = {
            "title": TITLE,
            "action": ACTION,
            "type": "table",
            "params": {
                **params,
                "date_from": date_from,
                "date_to": date_to,
                "stock_mode": _stock_mode_label(settings["stock_mode"]),
                "date_basis": _date_basis_label(settings["date_basis"]),
                "flow_scope": _flow_scope_label(settings["flow_scope"]),
                "stock_cds": _normalize_stock_codes(work_params),
            },
            "columns": list(df_display.columns),
            "df": df_display,
            "df_display": df_display,
            "records": df_display.to_dict(orient="records"),
            "final": True,
            "message": f"제품수불현황 {max(detail_count, 0):,}건",
            "meta": {
                **meta,
                "result_status": "success",
                "query_summary": _build_flow_query_summary(
                    date_from=date_from,
                    date_to=date_to,
                    settings=settings,
                    work_params=work_params,
                    params=params,
                ),
                "summary_md": _build_flow_header_md(meta),
                "note": _build_flow_header_md(meta),
            },
        }

        return payload

    except ValueError as e:
        return _flow_text_payload(
            message=f"조회조건이 부족합니다. {str(e)}",
            params=params,
            settings=settings,
        )
    except Exception:
        return _flow_text_payload(
            message="제품수불현황 조회 중 오류가 발생했습니다. 조회조건을 다시 확인해 주세요.",
            params=params,
            settings=settings,
        )

