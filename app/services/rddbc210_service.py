# app/services/rddbc210_service.py

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
    make_month_filters,
    normalize_top,
    query_to_df,
)


TABLE = "rddbc210"
log = logging.getLogger("ssai.sims.rddbc210")
REAL_ALLOWED_PREFIX = ("0", "1", "3", "4", "5", "6", "8", "9")

def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except Exception:
        return default


def _monthly_query_top(params: Dict[str, Any], *, default: int = 200) -> int:
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

    # 월집계는 화면의 오래된 top 값(예: 10,000)이 params에 남아 있어도
    # 공통 표시 제한 env(SIMS_PANEL_DISPLAY_MAX_ROWS)를 실제 조회 상한으로 사용한다.
    # 다운로드/export 경로에서 _max_top=SIMS_EXPORT_MAX_ROWS를 넘기면 그 값을 사용한다.
    try:
        return max(1, int(max_top))
    except Exception:
        return int(default)


def _monthly_export_top() -> int:
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
                "in_qty_sum": _num("in_qty_sum"),
                "in_bonus_qty_sum": _num("in_bonus_qty_sum"),
                "in_supply_sum": _num("in_supply_sum"),
                "in_tax_sum": _num("in_tax_sum"),
                "out_qty_sum": _num("out_qty_sum"),
                "out_bonus_qty_sum": _num("out_bonus_qty_sum"),
                "out_supply_sum": _num("out_supply_sum"),
                "out_tax_sum": _num("out_tax_sum"),
                "total_qty_sum": _num("total_qty_sum"),
                "total_supply_sum": _num("total_supply_sum"),
                "total_tax_sum": _num("total_tax_sum"),
            }
        )

    return out


def _num_sum(df, col: str) -> float:
    try:
        if df is None or df.empty or col not in df.columns:
            return 0
        return float(pd.to_numeric(df[col], errors="coerce").fillna(0).sum())
    except Exception:
        return 0

# 날짜/월 입력값 보정. 숫자 8자리 이상이면 YYYY-MM-DD, 6자리 이상이면 YYYY-MM 으로 보정해서 필터에 활용한다.
def _fmt_ymd(v: Any) -> str:
    s = str(v or "").strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    if len(digits) >= 6:
        return f"{digits[:4]}-{digits[4:6]}"
    return s

def _fmt_ym(v: Any) -> str:
    s = str(v or "").strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 6:
        return f"{digits[:4]}-{digits[4:6]}"
    return s

def _monthly_condition_text(params: Dict[str, Any], *, basis_label: str) -> str:
    month_from = _fmt_ym(params.get("month_from") or params.get("month_from6"))
    month_to = _fmt_ym(params.get("month_to") or params.get("month_to6"))

    date_from = _fmt_ymd(params.get("date_from"))
    date_to = _fmt_ymd(params.get("date_to"))

    move_type = str(params.get("move_type") or basis_label or "").strip()
    trans_type = str(params.get("trans_type") or "전체").strip()
    stock_label = str(params.get("stock_label_text") or "전체").strip()

    # 조회조건 표시 보강:
    # SQL 필터에는 제품/거래처 조건이 적용되어도 기존 condition_text에는
    # 기준월/기준/입출고구분/재고위치만 표시되어 회귀 테스트와 사용자 화면에서
    # 실제 필터 조건이 누락되어 보였다.
    def _add_condition(label: str, *keys: str) -> str:
        for key in keys:
            value = str(params.get(key) or "").strip()
            if value:
                return f"{label} {value}"
        return ""

    extra_conditions = [
        _add_condition("제품", "physic_cd"),
        _add_condition("제품명", "physic_nm"),
        _add_condition("거래처", "ven_cd"),
        _add_condition("거래처명", "ven_nm"),
        _add_condition("재고적용처", "stock_apply_cd"),
        _add_condition("재고적용처명", "stock_apply_nm"),
        _add_condition("제조사", "product_ven_cd"),
        _add_condition("제조사명", "product_ven_nm"),
        _add_condition("제품그룹명", "product_group_nm"),
        _add_condition("제품구분명", "product_di_nm"),
        _add_condition("제품분류명", "product_class_nm"),
        _add_condition("거래처그룹명", "ven_group_nm"),
        _add_condition("거래처종류명", "ven_kind_nm"),
    ]

    parts = []

    # 월집계는 month_from/month_to가 있으면 기준월을 우선 표시한다.
    if month_from or month_to:
        if month_from and month_to:
            parts.append(f"기준월 {month_from} ~ {month_to}")
        elif month_from:
            parts.append(f"기준월 {month_from} 이후")
        elif month_to:
            parts.append(f"기준월 {month_to} 이전")
    elif date_from or date_to:
        if date_from and date_to:
            parts.append(f"기간 {date_from} ~ {date_to}")
        elif date_from:
            parts.append(f"기간 {date_from} 이후")
        elif date_to:
            parts.append(f"기간 {date_to} 이전")

    if move_type:
        parts.append(f"기준 {move_type}")

    if trans_type:
        parts.append(f"입출고구분 {trans_type}")

    for cond in extra_conditions:
        if cond:
            parts.append(cond)
 
    if stock_label:
        parts.append(f"재고위치 {stock_label}")

    return " / ".join(parts) if parts else "전체"

def _add_month6_filters(clauses: list[str], column_sql: str, params: Dict[str, str]) -> None:
    month_from = clean_text(params.get("month_from"))
    month_to = clean_text(params.get("month_to"))

    # 화면/기존 호출에서 date_from/date_to 로 들어온 경우 월로 보정
    if not month_from and clean_text(params.get("date_from")):
        month_from = clean_text(params.get("date_from"))[:6]
        params["month_from"] = month_from

    if not month_to and clean_text(params.get("date_to")):
        month_to = clean_text(params.get("date_to"))[:6]
        params["month_to"] = month_to

    if month_from:
        params["month_from6"] = month_from[:6]
        add_filter(clauses, f"LEFT({column_sql}, 6) >= %(month_from6)s")

    if month_to:
        params["month_to6"] = month_to[:6]
        add_filter(clauses, f"LEFT({column_sql}, 6) <= %(month_to6)s")


def _base_filters(params: Dict[str, str]) -> str:
    clauses: list[str] = []

    _add_month6_filters(clauses, "Real_Stock.Rd21_Stock_YyMm", params)

    if clean_text(params.get("physic_cd")):
        add_filter(clauses, "Real_Stock.Rd21_Physic_Cd = %(physic_cd)s")

    if like_value(params.get("physic_nm")):
        params["physic_nm_like"] = like_value(params.get("physic_nm"))
        add_filter(clauses, "Physic_Cd.Rd04_Physic_Nm LIKE %(physic_nm_like)s")

    if clean_text(params.get("ven_cd")):
        add_filter(clauses, "Real_Stock.Rd21_Ven_Cd = %(ven_cd)s")

    if like_value(params.get("ven_nm")):
        params["ven_nm_like"] = like_value(params.get("ven_nm"))
        add_filter(clauses, "Ven_Cd.Rd03_Ven_Nm LIKE %(ven_nm_like)s")

    if clean_text(params.get("stock_cd")):
        stock_cd = clean_text(params.get("stock_cd"))
        params["stock_cd"] = stock_cd
        params["stock_cd_trim"] = stock_cd.lstrip("0") or "0"
        add_filter(
            clauses,
            """(
                LTRIM(RTRIM(Real_Stock.Rd21_Stock_Cd)) = %(stock_cd)s
                OR LTRIM(RTRIM(Real_Stock.Rd21_Stock_Cd)) = %(stock_cd_trim)s
                OR RIGHT('00000' + LTRIM(RTRIM(Real_Stock.Rd21_Stock_Cd)), 6) = RIGHT('00000' + %(stock_cd)s, 6)
            )"""
        )

    if clean_text(params.get("stock_apply_cd")):
        add_filter(clauses, "Real_Stock.Rd21_Stock_Apply_Cd = %(stock_apply_cd)s")

    if like_value(params.get("stock_apply_nm")):
        params["stock_apply_nm_like"] = like_value(params.get("stock_apply_nm"))
        add_filter(clauses, "Stock_Apply.Rd03_Ven_Nm LIKE %(stock_apply_nm_like)s")

    if clean_text(params.get("io_gu_prefix")):
        add_filter(clauses, "LEFT(Real_Stock.Rd21_Io_Gu, 1) = %(io_gu_prefix)s")

    stock_side = clean_text(params.get("stock_side")).lower()
    if stock_side in ("in", "입고", "매입"):
        add_filter(clauses, "LEFT(Real_Stock.Rd21_Io_Gu, 1) IN ('0','1','3','4')")
    elif stock_side in ("out", "출고", "매출"):
        add_filter(clauses, "LEFT(Real_Stock.Rd21_Io_Gu, 1) IN ('5','6','8','9')")

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
                WHERE V.Rd03_Ven_Cd = Real_Stock.Rd21_Ven_Cd
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
                WHERE V.Rd03_Ven_Cd = Real_Stock.Rd21_Ven_Cd
                  AND K.Rd01_Hnm LIKE %(ven_kind_nm_like)s
            )""",
        )

    # 제약사코드
    if clean_text(params.get("product_ven_cd")):
        add_filter(clauses, "Physic_Cd.Rd04_Ven_Cd = %(product_ven_cd)s")

    # 제약사명
    if like_value(params.get("product_ven_nm")):
        params["product_ven_nm_like"] = like_value(params.get("product_ven_nm"))
        add_filter(
            clauses,
            """EXISTS (
                SELECT 1
                FROM dbo.Rddbc030 AS M
                WHERE M.Rd03_Ven_Cd = Physic_Cd.Rd04_Ven_Cd
                  AND M.Rd03_Ven_Nm LIKE %(product_ven_nm_like)s
            )""",
        )

    # 제품그룹명
    if like_value(params.get("product_group_nm")):
        params["product_group_nm_like"] = like_value(params.get("product_group_nm"))
        add_filter(
            clauses,
            """EXISTS (
                SELECT 1
                FROM dbo.Rddbc010 AS PG
                WHERE PG.Rd01_Gcode = Physic_Cd.Rd04_Physic_Group_Gcode
                  AND PG.Rd01_Tcode = Physic_Cd.Rd04_Physic_Group
                  AND PG.Rd01_Hnm LIKE %(product_group_nm_like)s
            )""",
        )

    # 제품구분명
    if like_value(params.get("product_di_nm")):
        params["product_di_nm_like"] = like_value(params.get("product_di_nm"))
        add_filter(
            clauses,
            """EXISTS (
                SELECT 1
                FROM dbo.Rddbc010 AS PD
                WHERE PD.Rd01_Gcode = Physic_Cd.Rd04_Physic_Di_Gcode
                  AND PD.Rd01_Tcode = Physic_Cd.Rd04_Physic_Di
                  AND PD.Rd01_Hnm LIKE %(product_di_nm_like)s
            )""",
        )

    # 제품분류명
    if like_value(params.get("product_class_nm")):
        params["product_class_nm_like"] = like_value(params.get("product_class_nm"))
        add_filter(
            clauses,
            """EXISTS (
                SELECT 1
                FROM dbo.Rddbc010 AS PF
                WHERE PF.Rd01_Gcode = Physic_Cd.Rd04_Physic_Flag_Gcode
                  AND PF.Rd01_Tcode = Physic_Cd.Rd04_Physic_Flag
                  AND PF.Rd01_Hnm LIKE %(product_class_nm_like)s
            )""",
        )

    return ("\n      AND " + "\n      AND ".join(clauses)) if clauses else ""


def get_rddbc210_df(params: Optional[Dict[str, str]] = None):
    params = coalesce_params(params)
    params["top"] = _monthly_query_top(params, default=200)

    allowed = ", ".join(f"'{x}'" for x in REAL_ALLOWED_PREFIX)
    where_sql = _base_filters(params)

    sql = f"""
SELECT TOP (%(top)s)
    Real_Stock.Rd21_Stock_YyMm AS 재고년월,
    Real_Stock.Rd21_Physic_Cd AS 제품코드,
    Physic_Cd.Rd04_Physic_Nm AS 제품명,
    Real_Stock.Rd21_Stock_Cd AS 재고위치코드,
    Stock_Cd.Rd01_Hnm AS 재고위치,
    Real_Stock.Rd21_Ven_Cd AS 거래처코드,
    Ven_Cd.Rd03_Ven_Nm AS 거래처명,
    Real_Stock.Rd21_Stock_Apply_Cd AS 재고적용처코드,
    Stock_Apply.Rd03_Ven_Nm AS 재고적용처명,
    Real_Stock.Rd21_Io_Gu AS 입출고구분코드,
    Io_Gu.Rd01_Hnm AS 입출고구분,
    LEFT(Real_Stock.Rd21_Io_Gu, 1) AS 입출고구분앞자리,
    CASE
        WHEN LEFT(Real_Stock.Rd21_Io_Gu, 1) IN ('0','1','3','4') THEN '실재고 입고집계대상'
        WHEN LEFT(Real_Stock.Rd21_Io_Gu, 1) IN ('5','6','8','9') THEN '실재고 출고집계대상'
        ELSE '실재고 규칙외'
    END AS 집계규칙판정,
    CASE WHEN LEFT(Real_Stock.Rd21_Io_Gu, 1) IN ({allowed}) THEN 'Y' ELSE 'N' END AS 허용구분여부,
    Real_Stock.Rd21_In_Quantity AS 입고수량,
    Real_Stock.Rd21_In_Oquantity AS 입고할증수량,
    Real_Stock.Rd21_In_Supply_Price AS 입고공급가액,
    Real_Stock.Rd21_In_Tax_Price AS 입고세액,
    Real_Stock.Rd21_Out_Quantity AS 출고수량,
    Real_Stock.Rd21_Out_Oquantity AS 출고할증수량,
    Real_Stock.Rd21_Out_Supply_Price AS 출고공급가액,
    Real_Stock.Rd21_Out_Tax_Price AS 출고세액

FROM dbo.Rddbc210 AS Real_Stock
LEFT JOIN dbo.Rddbc040 AS Physic_Cd
    ON Real_Stock.Rd21_Physic_Cd = Physic_Cd.Rd04_Physic_Cd
LEFT JOIN dbo.Rddbc010 AS Stock_Cd
    ON Real_Stock.Rd21_Stock_Cd_Gcode = Stock_Cd.Rd01_Gcode
   AND Real_Stock.Rd21_Stock_Cd = Stock_Cd.Rd01_Tcode
LEFT JOIN dbo.Rddbc030 AS Ven_Cd
    ON Real_Stock.Rd21_Ven_Cd = Ven_Cd.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS Stock_Apply
    ON Real_Stock.Rd21_Stock_Apply_Cd = Stock_Apply.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc010 AS Io_Gu
    ON Real_Stock.Rd21_Io_Gu_Gcode = Io_Gu.Rd01_Gcode
   AND Real_Stock.Rd21_Io_Gu = Io_Gu.Rd01_Tcode
WHERE 1 = 1
{where_sql}
ORDER BY Real_Stock.Rd21_Stock_YyMm DESC, Real_Stock.Rd21_Physic_Cd, Real_Stock.Rd21_Ven_Cd
"""
    return query_to_df(sql, params)


def get_rddbc210_result(params: Optional[Dict[str, Any]] = None):
    params = coalesce_params(params)
    df = get_rddbc210_df(params)
    row_count = len(df) if df is not None else 0

    log.info("DBG get_rddbc210_result rows=%s", row_count)

    if df is None or df.empty:
        cond_text = _monthly_condition_text(params, basis_label="실재고")
        return {
            "title": "실재고월집계 조회",
            "action": "실재고월집계 조회",
            "params": params,
            "final": True,
            "type": "text",
            "data": "해당 조회조건의 자료가 없습니다.",
            "message": "해당 조회조건의 자료가 없습니다.",
            "meta": {
                "row_count": 0,
                "row_count_total": 0,
                "display_row_count": 0,
                "download_row_count": 0,
                "empty_result": True,
                "condition": cond_text,
                "query_summary": cond_text,
            },
        }

    payload = build_result_payload(
        table=TABLE,
        title="실재고월집계 조회",
        action="실재고월집계 조회",
        params=params,
        df=df,
        message=f"실재고월집계 {row_count:,}건",
    )

    cond_text = _monthly_condition_text(params, basis_label="실재고")

    meta = dict(payload.get("meta") or {})

    try:
        analysis_params = dict(params)
        analysis_params.pop("top", None)
        analysis_params.pop("_max_top", None)
        analysis_summary = get_rddbc210_analysis_summary(analysis_params)
    except Exception:
        log.exception("get_rddbc210_analysis_summary failed")
        analysis_summary = {}

    display_row_count = row_count
    total_row_count = int(
        analysis_summary.get("row_count_total")
        or analysis_summary.get("row_count")
        or row_count
        or 0
    )

    condition_text = _monthly_condition_text(params, basis_label="실재고")

    meta.update({
        "monthly_stock_summary": True,
        "monthly_stock_detail_summary": analysis_summary,

        "row_count": display_row_count,
        "display_row_count": display_row_count,
        "row_count_total": total_row_count,
        "analysis_row_count": total_row_count,
        "row_count_total_for_analysis": total_row_count,

        "condition": condition_text,
        "query_summary": condition_text,
        "stock_basis": "실재고",

        "sum_in_qty": _num_sum(df, "입고수량"),
        "sum_in_bonus_qty": _num_sum(df, "입고할증수량"),
        "sum_in_supply_amt": _num_sum(df, "입고공급가액"),
        "sum_in_tax_amt": _num_sum(df, "입고세액"),
        "sum_out_qty": _num_sum(df, "출고수량"),
        "sum_out_bonus_qty": _num_sum(df, "출고할증수량"),
        "sum_out_supply_amt": _num_sum(df, "출고공급가액"),
        "sum_out_tax_amt": _num_sum(df, "출고세액"),
    })

    def _fmt(v):
        try:
            n = float(v or 0)
            if abs(n - int(n)) < 1e-9:
                return f"{int(n):,}"
            return f"{n:,.2f}".rstrip("0").rstrip(".")
        except Exception:
            return str(v or "0")

    def _records_line(label: str, rows, key: str = "row_count") -> str:
        if not isinstance(rows, list) or not rows:
            return ""
        parts = []
        for r in rows[:10]:
            if not isinstance(r, dict):
                continue
            name = str(r.get("name") or "(미지정)").strip() or "(미지정)"
            val = r.get(key)
            if val is None and key != "row_count":
                val = r.get("row_count")
            unit = "건" if key == "row_count" else ""
            parts.append(f"{name} {_fmt(val)}{unit}")
        return f"{label}: " + ", ".join(parts) if parts else ""

    summary_lines = [
        f"실재고월집계 조회 집계 요약",
        "",
        "분석 기준: 전체 조회결과 기준",
        f"조회조건: {condition_text}",
        f"전체 조회건수: {total_row_count:,}건",
        f"화면 표시건수: {display_row_count:,}건",
        f"입고수량합계: {_fmt(analysis_summary.get('in_qty_sum') or meta.get('sum_in_qty'))}",
        f"입고공급가액합계: {_fmt(analysis_summary.get('in_supply_sum') or meta.get('sum_in_supply_amt'))}",
        f"입고세액합계: {_fmt(analysis_summary.get('in_tax_sum') or meta.get('sum_in_tax_amt'))}",
        f"출고수량합계: {_fmt(analysis_summary.get('out_qty_sum') or meta.get('sum_out_qty'))}",
        f"출고공급가액합계: {_fmt(analysis_summary.get('out_supply_sum') or meta.get('sum_out_supply_amt'))}",
        f"출고세액합계: {_fmt(analysis_summary.get('out_tax_sum') or meta.get('sum_out_tax_amt'))}",
    ]
    for _label, _key in [
        ("월별 상위", "by_month"),
        ("입출고구분별 상위", "by_io_type"),
        ("제품별 상위", "top_products"),
        ("거래처별 상위", "top_vendors"),
        ("재고위치별 상위", "top_stock_locations"),
    ]:
        _line = _records_line(_label, analysis_summary.get(_key), "row_count")
        if _line:
            summary_lines.append(_line)
    summary_lines.append("답변 규칙: 위 집계는 전체 조회결과 기준입니다.")
    meta["summary_md"] = "\n".join(summary_lines).strip()
    meta["llm_summary_md"] = meta.get("summary_md")

    payload["meta"] = meta

    if total_row_count > display_row_count:
        payload["message"] = f"조회결과: 전체 {total_row_count:,}건 중 화면 {display_row_count:,}건 표시"
    else:
        payload["message"] = f"조회결과: {display_row_count:,}건"

    return payload

def get_rddbc210_export_df(params: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    """
    실재고월집계 CSV/Excel 다운로드용 전체 상세 DataFrame.

    화면 조회 TOP 200과 분리한다.
    """
    qparams = coalesce_params(params)
    export_top = _monthly_export_top()

    qparams["top"] = export_top
    qparams["_max_top"] = export_top

    df = get_rddbc210_df(qparams)

    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame() if df is None else df

    try:
        payload = build_result_payload(
            table=TABLE,
            title="실재고월집계 조회",
            action="실재고월집계 조회",
            params=qparams,
            df=df,
            message=f"실재고월집계 다운로드 {len(df):,}건",
        )
        out = payload.get("df_display")
        if isinstance(out, pd.DataFrame):
            return out
    except Exception:
        log.exception("get_rddbc210_export_df label/apply failed")

    return df


def get_rddbc210_analysis_summary(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    실재고월집계 LLM 분석용 전체 집계.

    화면 조회 TOP 200과 분리한다.
    동일 조회조건 전체 기준으로 월/제품/거래처/재고위치/입출고구분별 집계를 만든다.
    """
    qparams = coalesce_params(dict(params or {}))
    where_sql = _base_filters(qparams)

    sql = f"""
WITH base AS (
    SELECT
        Real_Stock.Rd21_Stock_YyMm AS stock_yyyymm,
        NULLIF(LTRIM(RTRIM(Real_Stock.Rd21_Physic_Cd)), '') AS product_cd,
        COALESCE(NULLIF(LTRIM(RTRIM(Physic_Cd.Rd04_Physic_Nm)), ''), '(미지정)') AS product_nm,
        NULLIF(LTRIM(RTRIM(Real_Stock.Rd21_Stock_Cd)), '') AS stock_cd,
        COALESCE(NULLIF(LTRIM(RTRIM(Stock_Cd.Rd01_Hnm)), ''), '(미지정)') AS stock_nm,
        NULLIF(LTRIM(RTRIM(Real_Stock.Rd21_Ven_Cd)), '') AS vendor_cd,
        COALESCE(NULLIF(LTRIM(RTRIM(Ven_Cd.Rd03_Ven_Nm)), ''), '(미지정)') AS vendor_nm,
        NULLIF(LTRIM(RTRIM(Real_Stock.Rd21_Stock_Apply_Cd)), '') AS stock_apply_cd,
        COALESCE(NULLIF(LTRIM(RTRIM(Stock_Apply.Rd03_Ven_Nm)), ''), '(미지정)') AS stock_apply_nm,
        NULLIF(LTRIM(RTRIM(Real_Stock.Rd21_Io_Gu)), '') AS io_gu,
        COALESCE(NULLIF(LTRIM(RTRIM(Io_Gu.Rd01_Hnm)), ''), '(미지정)') AS io_gu_nm,
        CASE
            WHEN LEFT(Real_Stock.Rd21_Io_Gu, 1) IN ('0','1','3','4') THEN '입고집계대상'
            WHEN LEFT(Real_Stock.Rd21_Io_Gu, 1) IN ('5','6','8','9') THEN '출고집계대상'
            ELSE '규칙외'
        END AS side_nm,
        CAST(COALESCE(Real_Stock.Rd21_In_Quantity, 0) AS float) AS in_qty,
        CAST(COALESCE(Real_Stock.Rd21_In_Oquantity, 0) AS float) AS in_bonus_qty,
        CAST(COALESCE(Real_Stock.Rd21_In_Supply_Price, 0) AS float) AS in_supply,
        CAST(COALESCE(Real_Stock.Rd21_In_Tax_Price, 0) AS float) AS in_tax,
        CAST(COALESCE(Real_Stock.Rd21_Out_Quantity, 0) AS float) AS out_qty,
        CAST(COALESCE(Real_Stock.Rd21_Out_Oquantity, 0) AS float) AS out_bonus_qty,
        CAST(COALESCE(Real_Stock.Rd21_Out_Supply_Price, 0) AS float) AS out_supply,
        CAST(COALESCE(Real_Stock.Rd21_Out_Tax_Price, 0) AS float) AS out_tax
    FROM dbo.Rddbc210 AS Real_Stock
    LEFT JOIN dbo.Rddbc040 AS Physic_Cd
        ON Real_Stock.Rd21_Physic_Cd = Physic_Cd.Rd04_Physic_Cd
    LEFT JOIN dbo.Rddbc010 AS Stock_Cd
        ON Real_Stock.Rd21_Stock_Cd_Gcode = Stock_Cd.Rd01_Gcode
       AND Real_Stock.Rd21_Stock_Cd = Stock_Cd.Rd01_Tcode
    LEFT JOIN dbo.Rddbc030 AS Ven_Cd
        ON Real_Stock.Rd21_Ven_Cd = Ven_Cd.Rd03_Ven_Cd
    LEFT JOIN dbo.Rddbc030 AS Stock_Apply
        ON Real_Stock.Rd21_Stock_Apply_Cd = Stock_Apply.Rd03_Ven_Cd
    LEFT JOIN dbo.Rddbc010 AS Io_Gu
        ON Real_Stock.Rd21_Io_Gu_Gcode = Io_Gu.Rd01_Gcode
       AND Real_Stock.Rd21_Io_Gu = Io_Gu.Rd01_Tcode
    WHERE 1 = 1
    {where_sql}
),
grouped AS (
    SELECT
        'overall' AS section,
        '전체' AS name,
        COUNT_BIG(*) AS row_count,
        SUM(in_qty) AS in_qty_sum,
        SUM(in_bonus_qty) AS in_bonus_qty_sum,
        SUM(in_supply) AS in_supply_sum,
        SUM(in_tax) AS in_tax_sum,
        SUM(out_qty) AS out_qty_sum,
        SUM(out_bonus_qty) AS out_bonus_qty_sum,
        SUM(out_supply) AS out_supply_sum,
        SUM(out_tax) AS out_tax_sum,
        SUM(in_qty + out_qty) AS total_qty_sum,
        SUM(in_supply + out_supply) AS total_supply_sum,
        SUM(in_tax + out_tax) AS total_tax_sum,
        COUNT(DISTINCT product_cd) AS product_count,
        COUNT(DISTINCT vendor_cd) AS vendor_count,
        COUNT(DISTINCT stock_cd) AS stock_location_count,
        COUNT(DISTINCT stock_apply_cd) AS stock_apply_count
    FROM base

    UNION ALL

    SELECT
        'by_month',
        stock_yyyymm,
        COUNT_BIG(*),
        SUM(in_qty),
        SUM(in_bonus_qty),
        SUM(in_supply),
        SUM(in_tax),
        SUM(out_qty),
        SUM(out_bonus_qty),
        SUM(out_supply),
        SUM(out_tax),
        SUM(in_qty + out_qty),
        SUM(in_supply + out_supply),
        SUM(in_tax + out_tax),
        CAST(NULL AS int), CAST(NULL AS int), CAST(NULL AS int), CAST(NULL AS int)
    FROM base
    GROUP BY stock_yyyymm

    UNION ALL

    SELECT
        'top_products',
        product_nm,
        COUNT_BIG(*),
        SUM(in_qty),
        SUM(in_bonus_qty),
        SUM(in_supply),
        SUM(in_tax),
        SUM(out_qty),
        SUM(out_bonus_qty),
        SUM(out_supply),
        SUM(out_tax),
        SUM(in_qty + out_qty),
        SUM(in_supply + out_supply),
        SUM(in_tax + out_tax),
        CAST(NULL AS int), CAST(NULL AS int), CAST(NULL AS int), CAST(NULL AS int)
    FROM base
    GROUP BY product_nm

    UNION ALL

    SELECT
        'top_vendors',
        vendor_nm,
        COUNT_BIG(*),
        SUM(in_qty),
        SUM(in_bonus_qty),
        SUM(in_supply),
        SUM(in_tax),
        SUM(out_qty),
        SUM(out_bonus_qty),
        SUM(out_supply),
        SUM(out_tax),
        SUM(in_qty + out_qty),
        SUM(in_supply + out_supply),
        SUM(in_tax + out_tax),
        CAST(NULL AS int), CAST(NULL AS int), CAST(NULL AS int), CAST(NULL AS int)
    FROM base
    GROUP BY vendor_nm

    UNION ALL

    SELECT
        'top_stock_locations',
        stock_nm,
        COUNT_BIG(*),
        SUM(in_qty),
        SUM(in_bonus_qty),
        SUM(in_supply),
        SUM(in_tax),
        SUM(out_qty),
        SUM(out_bonus_qty),
        SUM(out_supply),
        SUM(out_tax),
        SUM(in_qty + out_qty),
        SUM(in_supply + out_supply),
        SUM(in_tax + out_tax),
        CAST(NULL AS int), CAST(NULL AS int), CAST(NULL AS int), CAST(NULL AS int)
    FROM base
    GROUP BY stock_nm

    UNION ALL

    SELECT
        'by_io_type',
        io_gu_nm,
        COUNT_BIG(*),
        SUM(in_qty),
        SUM(in_bonus_qty),
        SUM(in_supply),
        SUM(in_tax),
        SUM(out_qty),
        SUM(out_bonus_qty),
        SUM(out_supply),
        SUM(out_tax),
        SUM(in_qty + out_qty),
        SUM(in_supply + out_supply),
        SUM(in_tax + out_tax),
        CAST(NULL AS int), CAST(NULL AS int), CAST(NULL AS int), CAST(NULL AS int)
    FROM base
    GROUP BY io_gu_nm

    UNION ALL

    SELECT
        'by_side',
        side_nm,
        COUNT_BIG(*),
        SUM(in_qty),
        SUM(in_bonus_qty),
        SUM(in_supply),
        SUM(in_tax),
        SUM(out_qty),
        SUM(out_bonus_qty),
        SUM(out_supply),
        SUM(out_tax),
        SUM(in_qty + out_qty),
        SUM(in_supply + out_supply),
        SUM(in_tax + out_tax),
        CAST(NULL AS int), CAST(NULL AS int), CAST(NULL AS int), CAST(NULL AS int)
    FROM base
    GROUP BY side_nm
),
ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY section
            ORDER BY total_supply_sum DESC, total_qty_sum DESC, row_count DESC
        ) AS rn
    FROM grouped
)
SELECT
    section,
    name,
    row_count,
    in_qty_sum,
    in_bonus_qty_sum,
    in_supply_sum,
    in_tax_sum,
    out_qty_sum,
    out_bonus_qty_sum,
    out_supply_sum,
    out_tax_sum,
    total_qty_sum,
    total_supply_sum,
    total_tax_sum,
    product_count,
    vendor_count,
    stock_location_count,
    stock_apply_count
FROM ranked
WHERE section = 'overall'
   OR rn <= 10
ORDER BY
    CASE section
        WHEN 'overall' THEN 0
        WHEN 'by_month' THEN 1
        WHEN 'by_side' THEN 2
        WHEN 'by_io_type' THEN 3
        WHEN 'top_products' THEN 4
        WHEN 'top_vendors' THEN 5
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
            "stock_basis": "실재고",
            "by_month": [],
            "by_side": [],
            "by_io_type": [],
            "top_products": [],
            "top_vendors": [],
            "top_stock_locations": [],
        }

    overall_df = df[df["section"].astype(str) == "overall"]
    if overall_df.empty:
        return {"row_count_total": 0, "row_count": 0, "stock_basis": "실재고"}

    overall = overall_df.iloc[0]

    return {
        "row_count_total": _analysis_scalar(overall, "row_count", 0),
        "row_count": _analysis_scalar(overall, "row_count", 0),
        "stock_basis": "실재고",
        "in_qty_sum": _analysis_scalar(overall, "in_qty_sum", 0.0),
        "in_bonus_qty_sum": _analysis_scalar(overall, "in_bonus_qty_sum", 0.0),
        "in_supply_sum": _analysis_scalar(overall, "in_supply_sum", 0.0),
        "in_tax_sum": _analysis_scalar(overall, "in_tax_sum", 0.0),
        "out_qty_sum": _analysis_scalar(overall, "out_qty_sum", 0.0),
        "out_bonus_qty_sum": _analysis_scalar(overall, "out_bonus_qty_sum", 0.0),
        "out_supply_sum": _analysis_scalar(overall, "out_supply_sum", 0.0),
        "out_tax_sum": _analysis_scalar(overall, "out_tax_sum", 0.0),
        "total_qty_sum": _analysis_scalar(overall, "total_qty_sum", 0.0),
        "total_supply_sum": _analysis_scalar(overall, "total_supply_sum", 0.0),
        "total_tax_sum": _analysis_scalar(overall, "total_tax_sum", 0.0),
        "product_count": _analysis_scalar(overall, "product_count", 0),
        "vendor_count": _analysis_scalar(overall, "vendor_count", 0),
        "stock_location_count": _analysis_scalar(overall, "stock_location_count", 0),
        "stock_apply_count": _analysis_scalar(overall, "stock_apply_count", 0),
        "by_month": _analysis_records_from_section_df(df, "by_month"),
        "by_side": _analysis_records_from_section_df(df, "by_side"),
        "by_io_type": _analysis_records_from_section_df(df, "by_io_type"),
        "top_products": _analysis_records_from_section_df(df, "top_products"),
        "top_vendors": _analysis_records_from_section_df(df, "top_vendors"),
        "top_stock_locations": _analysis_records_from_section_df(df, "top_stock_locations"),
    }
