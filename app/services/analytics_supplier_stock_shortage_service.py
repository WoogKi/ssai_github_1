# app/services/analytics_supplier_stock_shortage_service.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, Optional

import pandas as pd

from app.services.analytics_sales_trend_service import (
    _apply_month_or_date_params,
    _apply_period_source_policy_params,
    _chunks,
    _clean_list_param,
    _fmt_analytics_query_summary,
    _fmt_counts_for_summary,
    _fmt_num_for_summary,
    _normalize_analytics_numeric_columns,
    _parse_yyyymm,
    _resolve_period_source_policy,
    _stock_current_cutoff_month,
    _stock_current_monthly_spec,
    _stock_shortage_source_labels,
    clean_text,
    coalesce_params,
    get_stock_shortage_df,
    query_to_df,
)
from app.services.rddbc_io_common import build_result_payload

log = logging.getLogger("ssai.sims.analytics_supplier_stock_shortage")

TABLE = "analytics_supplier_stock_shortage"
ACTION = "매입처별 재고부족 현황"

SUPPLIER_SUMMARY_COLUMNS = [
    "순번",
    "매입처코드",
    "매입처명",
    "관련제품수",
    "부족제품수",
    "재고없음제품수",
    "음수재고제품수",
    "매입처원본재고수량",
    "매입처원본재고금액",
    "음수재고수량",
    "음수재고금액",
    "최근6완료월매입금액",
    "전체완료월매입금액",
    "배정부족예상수량",
    "배정부족예상금액",
    "배정1개월부족수량",
    "배정1개월부족금액",
    "배정2개월부족수량",
    "배정2개월부족금액",
    "배정3개월부족수량",
    "배정3개월부족금액",
    "전체부족금액비중",
    "주요배분기준",
    "최고부족등급",
    "재고정합성",
    "배분정합성",
    "재고기준",
    "분석자료원",
    "기간구분",
]

SUPPLIER_DETAIL_COLUMNS = [
    "매입처코드",
    "매입처명",
    "제품코드",
    "제품명",
    "규격",
    "제조사명",
    "제품그룹명",
    "제품구분명",
    "제품분류명",
    "재고기준",
    "매입처원본재고수량",
    "매입처원본재고금액",
    "매입처재고음수여부",
    "매입처음수재고수량",
    "매입처음수재고금액",
    "최근6완료월매입수량",
    "최근6완료월매입금액",
    "전체완료월매입금액",
    "매입처입고누계수량",
    "매입처입고누계금액",
    "제품전체현재재고수량",
    "제품전체현재재고금액",
    "제품전체재고평가단가",
    "당월 현재출고수량",
    "당월 예상출고수량",
    "당월 잔여예상출고수량",
    "제품전체부족예상수량",
    "제품전체부족예상금액",
    "매입처배분율",
    "배정부족예상수량",
    "배정부족예상금액",
    "배정1개월부족수량",
    "배정1개월부족금액",
    "배정2개월부족수량",
    "배정2개월부족금액",
    "배정3개월부족수량",
    "배정3개월부족금액",
    "재고커버월수",
    "당월 재고충족률",
    "재고부족판정",
    "부족등급",
    "배분기준",
    "재고정합성",
    "배분정합성",
    "분석자료원",
    "기간구분",
]

GRADE_RANK = {
    "재고없음": 0,
    "즉시부족": 1,
    "부족": 2,
    "주의": 3,
    "정상": 4,
    "수요없음": 5,
}


_SUPPLIER_DETAIL_FALLBACK_CACHE: Dict[str, pd.DataFrame] = {}


def _stash_supplier_detail_df(detail: pd.DataFrame) -> str:
    key = f"supplier_stock_shortage_detail::{uuid.uuid4().hex}"
    if not isinstance(detail, pd.DataFrame):
        return key
    try:
        import streamlit as st  # type: ignore

        st.session_state.setdefault("__sims_supplier_stock_shortage_detail_tables", {})
        st.session_state["__sims_supplier_stock_shortage_detail_tables"][key] = detail
    except Exception:
        _SUPPLIER_DETAIL_FALLBACK_CACHE[key] = detail
    return key


def _num(value: Any, index: pd.Index | None = None) -> pd.Series:
    if isinstance(value, pd.Series):
        return pd.to_numeric(value, errors="coerce").fillna(0)
    if index is None:
        return pd.Series([pd.to_numeric(value, errors="coerce")]).fillna(0)
    return pd.Series(value, index=index).pipe(pd.to_numeric, errors="coerce").fillna(0)


def _sum_numeric(df: pd.DataFrame, col: str) -> float:
    if df is None or col not in df.columns:
        return 0.0
    return float(_num(df[col]).sum())


def _count_values(df: pd.DataFrame, col: str) -> Dict[str, int]:
    if df is None or df.empty or col not in df.columns:
        return {}
    vals = df[col].fillna("").astype(str).str.strip().replace("", "미지정")
    return {str(k): int(v) for k, v in vals.value_counts().items()}


def _source_params(params: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(params or {})
    # 매입처 조건은 제품 공식 부족값을 흔들지 않도록 마지막 요약 단계에서만 적용한다.
    out["buy_nm"] = ""
    out["buy_cd"] = ""
    return out


def load_product_shortage_base(params: Dict[str, Any]) -> pd.DataFrame:
    return get_stock_shortage_df(_source_params(params))


def _next_month(yyyymm: str) -> str:
    y = int(yyyymm[:4])
    m = int(yyyymm[4:6]) + 1
    if m > 12:
        y += 1
        m = 1
    return f"{y:04d}{m:02d}"


def _completed_months(params: Dict[str, Any], policy: Dict[str, Any]) -> list[str]:
    month_from = _parse_yyyymm(params.get("month_from") or str(params.get("date_from") or "")[:6])
    evaluation_month = _parse_yyyymm(policy.get("evaluation_month"))
    if not month_from or not evaluation_month:
        return []
    months: list[str] = []
    cur = month_from
    while cur < evaluation_month:
        months.append(cur)
        cur = _next_month(cur)
    return months


def _recent6_completed_months(params: Dict[str, Any], policy: Dict[str, Any]) -> list[str]:
    return _completed_months(params, policy)[-6:]


def load_supplier_product_stock(product_codes: list[str], params: Dict[str, Any]) -> pd.DataFrame:
    t0 = time.perf_counter()
    codes = sorted({str(x or "").strip() for x in product_codes if str(x or "").strip()})
    columns = [
        "제품코드",
        "매입처코드",
        "매입처명",
        "매입처원본재고수량",
        "매입처원본재고금액",
        "매입처입고누계수량",
        "매입처입고누계금액",
        "최근6완료월매입수량",
        "최근6완료월매입금액",
        "전체완료월매입금액",
    ]
    if not codes:
        return pd.DataFrame(columns=columns)

    stock_mode = str(params.get("stock_mode") or "real").strip()
    stock_spec = _stock_current_monthly_spec(stock_mode)
    policy = _resolve_period_source_policy(params)
    table = stock_spec["table"]
    pfx = stock_spec["prefix"]
    stock_cutoff = _stock_current_cutoff_month(params)
    recent6 = _recent6_completed_months(params, policy)
    completed = _completed_months(params, policy)
    recent6_from = recent6[0] if recent6 else "000000"
    recent6_to = recent6[-1] if recent6 else "000000"
    completed_from = completed[0] if completed else "000000"
    completed_to = completed[-1] if completed else "000000"

    real_mode = stock_mode == "real"
    in_qty_expr = (
        f"CAST(ISNULL(M.{pfx}_In_Quantity, 0) AS FLOAT) + CAST(ISNULL(M.{pfx}_In_Oquantity, 0) AS FLOAT)"
        if real_mode
        else f"CAST(ISNULL(M.{pfx}_In_Quantity, 0) AS FLOAT)"
    )
    out_qty_expr = (
        f"CAST(ISNULL(M.{pfx}_Out_Quantity, 0) AS FLOAT) + CAST(ISNULL(M.{pfx}_Out_Oquantity, 0) AS FLOAT)"
        if real_mode
        else f"CAST(ISNULL(M.{pfx}_Out_Quantity, 0) AS FLOAT)"
    )
    unit_expr = (
        f"CASE WHEN ABS({in_qty_expr}) > 0 "
        f"THEN CAST(ISNULL(M.{pfx}_In_Supply_Price, 0) AS FLOAT) / NULLIF({in_qty_expr}, 0) "
        "ELSE 0 END"
    )

    stock_codes = _clean_list_param(params.get("stock_cd_list"))
    if not stock_codes and clean_text(params.get("stock_cd")):
        stock_codes = [clean_text(params.get("stock_cd"))]

    frames: list[pd.DataFrame] = []
    for batch in _chunks(codes, 1600):
        bind: Dict[str, Any] = {
            "stock_cutoff": stock_cutoff,
            "recent6_from": recent6_from,
            "recent6_to": recent6_to,
            "completed_from": completed_from,
            "completed_to": completed_to,
        }
        code_names: list[str] = []
        for i, code in enumerate(batch):
            key = f"cd{i}"
            bind[key] = code
            code_names.append(f"%({key})s")

        stock_filter = ""
        if stock_codes:
            stock_names: list[str] = []
            for i, code in enumerate(stock_codes):
                key = f"stock_cd_{i}"
                bind[key] = code
                stock_names.append(f"%({key})s")
            stock_filter = f"\n      AND M.{pfx}_Stock_Cd IN ({', '.join(stock_names)})"

        sql = f"""
SELECT
    LTRIM(RTRIM(M.{pfx}_Physic_Cd)) AS [제품코드],
    COALESCE(NULLIF(LTRIM(RTRIM(M.{pfx}_Ven_Cd)), ''), '미지정') AS [매입처코드],
    COALESCE(NULLIF(LTRIM(RTRIM(BuyVen.Rd03_Ven_Nm)), ''), '미지정') AS [매입처명],
    SUM(CASE WHEN M.{pfx}_Stock_YyMm <= %(stock_cutoff)s THEN {in_qty_expr} - {out_qty_expr} ELSE 0 END) AS [매입처원본재고수량],
    SUM(CASE WHEN M.{pfx}_Stock_YyMm <= %(stock_cutoff)s THEN ({in_qty_expr} - {out_qty_expr}) * {unit_expr} ELSE 0 END) AS [매입처원본재고금액],
    SUM(CASE WHEN M.{pfx}_Stock_YyMm <= %(stock_cutoff)s THEN {in_qty_expr} ELSE 0 END) AS [매입처입고누계수량],
    SUM(CASE WHEN M.{pfx}_Stock_YyMm <= %(stock_cutoff)s THEN CAST(ISNULL(M.{pfx}_In_Supply_Price, 0) AS FLOAT) ELSE 0 END) AS [매입처입고누계금액],
    SUM(CASE WHEN M.{pfx}_Stock_YyMm BETWEEN %(recent6_from)s AND %(recent6_to)s AND CAST(ISNULL(M.{pfx}_In_Supply_Price, 0) AS FLOAT) > 0 THEN {in_qty_expr} ELSE 0 END) AS [최근6완료월매입수량],
    SUM(CASE WHEN M.{pfx}_Stock_YyMm BETWEEN %(recent6_from)s AND %(recent6_to)s AND CAST(ISNULL(M.{pfx}_In_Supply_Price, 0) AS FLOAT) > 0 THEN CAST(ISNULL(M.{pfx}_In_Supply_Price, 0) AS FLOAT) ELSE 0 END) AS [최근6완료월매입금액],
    SUM(CASE WHEN M.{pfx}_Stock_YyMm BETWEEN %(completed_from)s AND %(completed_to)s AND CAST(ISNULL(M.{pfx}_In_Supply_Price, 0) AS FLOAT) > 0 THEN CAST(ISNULL(M.{pfx}_In_Supply_Price, 0) AS FLOAT) ELSE 0 END) AS [전체완료월매입금액]
FROM {table} AS M WITH (NOLOCK)
LEFT JOIN dbo.Rddbc030 AS BuyVen WITH (NOLOCK)
    ON LTRIM(RTRIM(M.{pfx}_Ven_Cd)) = LTRIM(RTRIM(BuyVen.Rd03_Ven_Cd))
WHERE M.{pfx}_Physic_Cd IN ({", ".join(code_names)})
  AND M.{pfx}_Stock_YyMm <= %(stock_cutoff)s
  {stock_filter}
GROUP BY
    LTRIM(RTRIM(M.{pfx}_Physic_Cd)),
    COALESCE(NULLIF(LTRIM(RTRIM(M.{pfx}_Ven_Cd)), ''), '미지정'),
    COALESCE(NULLIF(LTRIM(RTRIM(BuyVen.Rd03_Ven_Nm)), ''), '미지정')
OPTION (RECOMPILE)
"""
        df = query_to_df(sql, bind)
        if isinstance(df, pd.DataFrame) and not df.empty:
            frames.append(df)

    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=columns)
    for col in columns:
        if col not in out.columns:
            out[col] = "" if col in {"제품코드", "매입처코드", "매입처명"} else 0
    for col in columns:
        if col not in {"제품코드", "매입처코드", "매입처명"}:
            out[col] = _num(out[col])

    elapsed = time.perf_counter() - t0
    neg = _num(out["매입처원본재고수량"]) < 0 if not out.empty else pd.Series(dtype=bool)
    log.info(
        "[analytics.supplier_stock_shortage.stock] product_count=%s supplier_product_rows=%s supplier_count=%s negative_stock_rows=%s negative_stock_products=%s negative_stock_suppliers=%s stock_mismatch_products=%s elapsed=%.3f",
        len(codes),
        len(out),
        int(out["매입처코드"].nunique()) if not out.empty else 0,
        int(neg.sum()) if not out.empty else 0,
        int(out.loc[neg, "제품코드"].nunique()) if not out.empty else 0,
        int(out.loc[neg, "매입처코드"].nunique()) if not out.empty else 0,
        0,
        elapsed,
    )
    return out


def _product_cols_from_base(base: pd.DataFrame) -> pd.DataFrame:
    rename = {
        "현재재고수량": "제품전체현재재고수량",
        "현재재고금액": "제품전체현재재고금액",
        "재고평가단가": "제품전체재고평가단가",
        "부족예상수량": "제품전체부족예상수량",
        "부족예상금액": "제품전체부족예상금액",
    }
    cols = [
        "제품코드",
        "제품명",
        "규격",
        "제조사명",
        "제품그룹명",
        "제품구분명",
        "제품분류명",
        "재고기준",
        "현재재고수량",
        "현재재고금액",
        "재고평가단가",
        "당월 현재출고수량",
        "당월 예상출고수량",
        "당월 잔여예상출고수량",
        "부족예상수량",
        "부족예상금액",
        "1개월부족수량",
        "2개월부족수량",
        "3개월부족수량",
        "재고커버월수",
        "당월 재고충족률",
        "재고부족판정",
        "부족등급",
    ]
    out = base[[c for c in cols if c in base.columns]].copy()
    out = out.rename(columns=rename)
    unit = _num(out.get("제품전체재고평가단가", 0), out.index).clip(lower=0)
    for n in ("1", "2", "3"):
        qcol = f"{n}개월부족수량"
        if qcol in out.columns:
            out[f"{n}개월부족금액"] = _num(out[qcol]) * unit
    return out


def _add_missing_supplier_rows(stock: pd.DataFrame, product_base: pd.DataFrame) -> pd.DataFrame:
    if product_base.empty:
        return stock
    existing = set(stock["제품코드"].astype(str)) if "제품코드" in stock.columns else set()
    missing_codes = [c for c in product_base["제품코드"].astype(str).tolist() if c not in existing]
    if not missing_codes:
        return stock
    rows = []
    for code in missing_codes:
        rows.append(
            {
                "제품코드": code,
                "매입처코드": "미지정",
                "매입처명": "미지정",
                "매입처원본재고수량": 0,
                "매입처원본재고금액": 0,
                "매입처입고누계수량": 0,
                "매입처입고누계금액": 0,
                "최근6완료월매입수량": 0,
                "최근6완료월매입금액": 0,
                "전체완료월매입금액": 0,
            }
        )
    return pd.concat([stock, pd.DataFrame(rows)], ignore_index=True)


def validate_supplier_allocation(detail: pd.DataFrame) -> pd.DataFrame:
    if detail is None or detail.empty:
        return detail
    detail["재고정합성"] = "일치"
    detail["배분정합성"] = "일치"
    for _, idx in detail.groupby("제품코드").groups.items():
        sub = detail.loc[idx]
        stock_qty_diff = float(sub["매입처원본재고수량"].sum() - sub["제품전체현재재고수량"].iloc[0])
        stock_amt_diff = float(sub["매입처원본재고금액"].sum() - sub["제품전체현재재고금액"].iloc[0])
        if abs(stock_qty_diff) > 0.01 or abs(stock_amt_diff) > 1:
            detail.loc[idx, "재고정합성"] = "불일치"

        alloc_rate_diff = float(sub["매입처배분율"].sum() - 1)
        alloc_amt_diff = float(sub["배정부족예상금액"].sum() - sub["제품전체부족예상금액"].iloc[0])
        if abs(alloc_rate_diff) > 1e-6 or abs(alloc_amt_diff) > 1:
            detail.loc[idx, "배분정합성"] = "불일치"
    return detail


def build_supplier_allocation_detail(product_shortage: pd.DataFrame, supplier_stock: pd.DataFrame) -> pd.DataFrame:
    product_base = _product_cols_from_base(product_shortage)
    stock = _add_missing_supplier_rows(supplier_stock.copy(), product_base)
    detail = stock.merge(product_base, on="제품코드", how="inner", validate="many_to_one")
    if detail.empty:
        return pd.DataFrame(columns=SUPPLIER_DETAIL_COLUMNS)

    numeric_cols = [
        "매입처원본재고수량",
        "매입처원본재고금액",
        "매입처입고누계수량",
        "매입처입고누계금액",
        "최근6완료월매입수량",
        "최근6완료월매입금액",
        "전체완료월매입금액",
        "제품전체현재재고수량",
        "제품전체현재재고금액",
        "제품전체재고평가단가",
        "제품전체부족예상수량",
        "제품전체부족예상금액",
        "1개월부족수량",
        "1개월부족금액",
        "2개월부족수량",
        "2개월부족금액",
        "3개월부족수량",
        "3개월부족금액",
    ]
    for col in numeric_cols:
        if col not in detail.columns:
            detail[col] = 0
        detail[col] = _num(detail[col])

    detail["매입처원본재고금액"] = (
        detail["매입처원본재고수량"] * detail["제품전체재고평가단가"]
    )

    detail["매입처재고음수여부"] = detail["매입처원본재고수량"] < 0
    detail["매입처음수재고수량"] = detail["매입처원본재고수량"].where(detail["매입처원본재고수량"] < 0, 0)
    detail["매입처음수재고금액"] = detail["매입처원본재고금액"].where(detail["매입처원본재고수량"] < 0, 0)

    detail["_basis_recent6"] = detail["최근6완료월매입금액"].clip(lower=0)
    detail["_basis_full"] = detail["전체완료월매입금액"].clip(lower=0)
    detail["_basis_stock_amt"] = detail["매입처원본재고금액"].clip(lower=0)
    detail["_basis_in_qty"] = detail["매입처입고누계수량"].clip(lower=0)

    basis_rows = []
    for _, sub in detail.groupby("제품코드", sort=False):
        idx = sub.index
        basis_col = ""
        basis_name = ""
        for col, name in [
            ("_basis_recent6", "최근6완료월매입금액"),
            ("_basis_full", "전체완료월매입금액"),
            ("_basis_stock_amt", "양수재고금액"),
            ("_basis_in_qty", "양수입고누계수량"),
        ]:
            if float(sub[col].sum()) > 0:
                basis_col = col
                basis_name = name
                break

        weights = pd.Series(0.0, index=idx)
        if basis_col:
            weights = sub[basis_col] / float(sub[basis_col].sum())
        else:
            unassigned = sub.index[sub["매입처코드"].astype(str).eq("미지정")]
            target = unassigned[0] if len(unassigned) else idx[0]
            weights.loc[target] = 1.0
            basis_name = "미지정"

        if len(weights) > 0:
            weights.iloc[-1] = weights.iloc[-1] + (1.0 - float(weights.sum()))
        basis_rows.append(pd.DataFrame({"_idx": idx, "매입처배분율": weights.values, "배분기준": basis_name}))

    basis_df = pd.concat(basis_rows, ignore_index=True).set_index("_idx")
    detail = detail.join(basis_df, how="left")
    detail["매입처배분율"] = _num(detail["매입처배분율"])

    for src, dst in [
        ("제품전체부족예상수량", "배정부족예상수량"),
        ("제품전체부족예상금액", "배정부족예상금액"),
        ("1개월부족수량", "배정1개월부족수량"),
        ("1개월부족금액", "배정1개월부족금액"),
        ("2개월부족수량", "배정2개월부족수량"),
        ("2개월부족금액", "배정2개월부족금액"),
        ("3개월부족수량", "배정3개월부족수량"),
        ("3개월부족금액", "배정3개월부족금액"),
    ]:
        detail[dst] = _num(detail[src], detail.index) * detail["매입처배분율"]

    validate_supplier_allocation(detail)
    for col in SUPPLIER_DETAIL_COLUMNS:
        if col not in detail.columns:
            detail[col] = ""
    return detail[SUPPLIER_DETAIL_COLUMNS].copy()


def _worst_grade(values: pd.Series) -> str:
    vals = [str(x or "").strip() for x in values.tolist() if str(x or "").strip()]
    if not vals:
        return "미분류"
    vals.sort(key=lambda x: GRADE_RANK.get(x, 99))
    return vals[0] or "미분류"


def _main_basis(values: pd.Series, amounts: pd.Series) -> str:
    work = pd.DataFrame({"basis": values.fillna("미지정").astype(str), "amount": _num(amounts)})
    if work.empty:
        return "미지정"
    grouped = work.groupby("basis", dropna=False)["amount"].sum().sort_values(ascending=False)
    return str(grouped.index[0]) if len(grouped) else "미지정"


def build_supplier_shortage_summary(detail: pd.DataFrame, params: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    if detail is None or detail.empty:
        return pd.DataFrame(columns=SUPPLIER_SUMMARY_COLUMNS)
    work = detail.copy()
    numeric_cols = [
        "매입처원본재고수량",
        "매입처원본재고금액",
        "매입처음수재고수량",
        "매입처음수재고금액",
        "최근6완료월매입금액",
        "전체완료월매입금액",
        "배정부족예상수량",
        "배정부족예상금액",
        "배정1개월부족수량",
        "배정1개월부족금액",
        "배정2개월부족수량",
        "배정2개월부족금액",
        "배정3개월부족수량",
        "배정3개월부족금액",
    ]
    for col in numeric_cols:
        work[col] = _num(work.get(col, 0), work.index)

    summary = (
        work.groupby(["매입처코드", "매입처명"], dropna=False)
        .apply(
            lambda g: pd.Series(
                {
                    "관련제품수": int(g["제품코드"].nunique()),
                    "부족제품수": int(g.loc[_num(g["제품전체부족예상금액"]) > 0, "제품코드"].nunique()),
                    "재고없음제품수": int(g.loc[g["부족등급"].astype(str).str.contains("재고없음", na=False), "제품코드"].nunique()),
                    "음수재고제품수": int(g.loc[_num(g["매입처원본재고수량"]) < 0, "제품코드"].nunique()),
                    "매입처원본재고수량": g["매입처원본재고수량"].sum(),
                    "매입처원본재고금액": g["매입처원본재고금액"].sum(),
                    "음수재고수량": g["매입처음수재고수량"].sum(),
                    "음수재고금액": g["매입처음수재고금액"].sum(),
                    "최근6완료월매입금액": g["최근6완료월매입금액"].sum(),
                    "전체완료월매입금액": g["전체완료월매입금액"].sum(),
                    "배정부족예상수량": g["배정부족예상수량"].sum(),
                    "배정부족예상금액": g["배정부족예상금액"].sum(),
                    "배정1개월부족수량": g["배정1개월부족수량"].sum(),
                    "배정1개월부족금액": g["배정1개월부족금액"].sum(),
                    "배정2개월부족수량": g["배정2개월부족수량"].sum(),
                    "배정2개월부족금액": g["배정2개월부족금액"].sum(),
                    "배정3개월부족수량": g["배정3개월부족수량"].sum(),
                    "배정3개월부족금액": g["배정3개월부족금액"].sum(),
                    "주요배분기준": _main_basis(g["배분기준"], g["배정부족예상금액"]),
                    "최고부족등급": _worst_grade(g["부족등급"]),
                    "재고정합성": "불일치" if (g["재고정합성"].astype(str) == "불일치").any() else "일치",
                    "배분정합성": "불일치" if (g["배분정합성"].astype(str) == "불일치").any() else "일치",
                    "재고기준": str(g["재고기준"].dropna().iloc[0]) if "재고기준" in g and not g["재고기준"].dropna().empty else "",
                    "분석자료원": str(g["분석자료원"].dropna().iloc[0]) if "분석자료원" in g and not g["분석자료원"].dropna().empty else "",
                    "기간구분": str(g["기간구분"].dropna().iloc[0]) if "기간구분" in g and not g["기간구분"].dropna().empty else "",
                }
            ),
            include_groups=False,
        )
        .reset_index()
    )

    total_alloc = float(summary["배정부족예상금액"].sum())
    summary["전체부족금액비중"] = (summary["배정부족예상금액"] / total_alloc * 100) if abs(total_alloc) >= 1e-12 else 0

    summary_before_vendor_filter_rows = int(len(summary))
    buy_nm = clean_text((params or {}).get("buy_nm"))
    if buy_nm:
        summary = summary[summary["매입처명"].fillna("").astype(str).str.contains(buy_nm, na=False)].copy()
    summary_after_vendor_filter_rows = int(len(summary))

    summary = summary.sort_values(
        ["배정부족예상금액", "배정1개월부족금액", "음수재고금액", "부족제품수", "매입처명"],
        ascending=[False, False, True, False, True],
    ).reset_index(drop=True)
    summary.insert(0, "순번", range(1, len(summary) + 1))

    for col in SUPPLIER_SUMMARY_COLUMNS:
        if col not in summary.columns:
            summary[col] = ""
    result = _normalize_analytics_numeric_columns(summary[SUPPLIER_SUMMARY_COLUMNS].copy())
    result.attrs.update({
        "supplier_summary_before_vendor_filter_rows": summary_before_vendor_filter_rows,
        "supplier_summary_after_vendor_filter_rows": summary_after_vendor_filter_rows,
        "supplier_vendor_filter_applied": bool(buy_nm),
    })
    return result


def _meta_from_frames(summary: pd.DataFrame, detail: pd.DataFrame, product_base: pd.DataFrame, params: Dict[str, Any]) -> Dict[str, Any]:
    total_alloc = _sum_numeric(summary, "배정부족예상금액")
    mismatch_stock = int(detail.loc[detail.get("재고정합성", "") == "불일치", "제품코드"].nunique()) if "제품코드" in detail.columns else 0
    mismatch_alloc = int(detail.loc[detail.get("배분정합성", "") == "불일치", "제품코드"].nunique()) if "제품코드" in detail.columns else 0
    unassigned_amount = 0.0
    if "매입처코드" in detail.columns and "배정부족예상금액" in detail.columns:
        unassigned_amount = float(detail.loc[detail["매입처코드"].astype(str) == "미지정", "배정부족예상금액"].sum())
    return {
        "row_count": int(len(summary)),
        "row_count_total": int(len(summary)),
        "supplier_count": int(summary["매입처코드"].nunique()) if "매입처코드" in summary.columns else 0,
        "product_count": int(product_base["제품코드"].nunique()) if "제품코드" in product_base.columns else 0,
        "supplier_product_rows": int(len(detail)),
        "shortage_product_count": int(product_base.loc[_num(product_base.get("부족예상금액", 0), product_base.index) > 0, "제품코드"].nunique()) if "제품코드" in product_base.columns else 0,
        "negative_supplier_count": int(summary.loc[_num(summary.get("음수재고제품수", 0), summary.index) > 0, "매입처코드"].nunique()) if "매입처코드" in summary.columns else 0,
        "negative_product_count": int(detail.loc[_num(detail.get("매입처원본재고수량", 0), detail.index) < 0, "제품코드"].nunique()) if "제품코드" in detail.columns else 0,
        "negative_stock_rows": int((_num(detail.get("매입처원본재고수량", 0), detail.index) < 0).sum()) if not detail.empty else 0,
        "total_product_stock_amount": _sum_numeric(product_base, "현재재고금액"),
        "total_product_shortage_amount": _sum_numeric(product_base, "부족예상금액"),
        "total_allocated_shortage_amount": total_alloc,
        "sum_expected_shortage_amt": total_alloc,
        "stock_consistency_mismatch_products": mismatch_stock,
        "allocation_consistency_mismatch_products": mismatch_alloc,
        "unassigned_allocated_shortage_amount": unassigned_amount,
        "supplier_shortage_distribution": dict(summary.set_index("매입처명")["배정부족예상금액"].sort_values(ascending=False).head(20)) if "매입처명" in summary.columns and "배정부족예상금액" in summary.columns else {},
        "allocation_basis_distribution": _count_values(detail, "배분기준"),
        "allocation_basis_amount_distribution": {str(k): float(v) for k, v in detail.groupby("배분기준")["배정부족예상금액"].sum().sort_values(ascending=False).items()} if "배분기준" in detail.columns and "배정부족예상금액" in detail.columns else {},
        "shortage_grade_distribution": _count_values(detail, "부족등급"),
        "stock_shortage_judge_counts": _count_values(detail, "재고부족판정"),
        "stock_consistency_distribution": _count_values(detail, "재고정합성"),
        "allocation_consistency_distribution": _count_values(detail, "배분정합성"),
        "stock_mode": str(params.get("stock_mode") or "real"),
    }


def get_supplier_stock_shortage_df(params: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    params = _apply_period_source_policy_params(_apply_month_or_date_params(coalesce_params(params)))
    t0 = time.perf_counter()
    stock_mode = str(params.get("stock_mode") or "real").strip()
    stock_spec = _stock_current_monthly_spec(stock_mode)
    policy = _resolve_period_source_policy(params)
    source_labels = _stock_shortage_source_labels(params, stock_mode=stock_mode)
    log.info(
        "[analytics.supplier_stock_shortage.source] stock_mode=%s stock_table=%s date_from=%s date_to=%s evaluation_mode=%s",
        stock_mode,
        stock_spec.get("source_table"),
        params.get("date_from"),
        params.get("date_to"),
        policy.get("evaluation_mode"),
    )

    t_base0 = time.perf_counter()
    product_base = load_product_shortage_base(params)
    t_base = time.perf_counter()
    if product_base is None or product_base.empty:
        out = pd.DataFrame(columns=SUPPLIER_SUMMARY_COLUMNS)
        out.attrs.update({"row_count": 0, "row_count_total": 0, "supplier_count": 0, "product_count": 0})
        return out

    product_codes = product_base["제품코드"].fillna("").astype(str).str.strip().tolist()
    supplier_stock = load_supplier_product_stock(product_codes, params)
    t_stock = time.perf_counter()
    detail = build_supplier_allocation_detail(product_base, supplier_stock)
    detail["분석자료원"] = source_labels["display_source"]
    detail["기간구분"] = source_labels.get("display_period_label") or source_labels.get("evaluation_mode") or ""
    t_alloc = time.perf_counter()
    summary = build_supplier_shortage_summary(detail, params)
    t_summary = time.perf_counter()
    meta = _meta_from_frames(summary, detail, product_base, params)
    meta.update(dict(summary.attrs or {}))
    supplier_detail_key = _stash_supplier_detail_df(detail)
    meta.update(
        {
            "supplier_detail_key": supplier_detail_key,
            "supplier_detail_rows": int(len(detail)),
            "excel_sheet_names": ["매입처별요약", "제품매입처상세"],
        }
    )
    summary.attrs.update(meta)

    log.info(
        "[analytics.supplier_stock_shortage.stage] demand_shortage_rows=%s current_stock_rows=%s "
        "shortage_product_count=%s allocation_rows=%s supplier_summary_before_vendor_filter_rows=%s "
        "supplier_summary_after_vendor_filter_rows=%s supplier_vendor_filter_applied=%s",
        int(len(product_base)),
        int(len(supplier_stock)),
        int(meta.get("shortage_product_count") or 0),
        int(len(detail)),
        int(meta.get("supplier_summary_before_vendor_filter_rows") or 0),
        int(meta.get("supplier_summary_after_vendor_filter_rows") or 0),
        bool(meta.get("supplier_vendor_filter_applied")),
    )

    alloc_basis = meta.get("allocation_basis_distribution") or {}
    log.info(
        "[analytics.supplier_stock_shortage.allocation] recent6_amount_products=%s full_period_amount_products=%s positive_stock_amount_products=%s positive_inbound_qty_products=%s primary_supplier_products=%s unassigned_products=%s allocation_mismatch_products=%s elapsed=%.3f",
        alloc_basis.get("최근6완료월매입금액", 0),
        alloc_basis.get("전체완료월매입금액", 0),
        alloc_basis.get("양수재고금액", 0),
        alloc_basis.get("양수입고누계수량", 0),
        0,
        alloc_basis.get("미지정", 0),
        meta.get("allocation_consistency_mismatch_products", 0),
        t_alloc - t_stock,
    )
    log.info(
        "[analytics.supplier_stock_shortage.perf] base_shortage_elapsed=%.3f supplier_stock_elapsed=%.3f allocation_elapsed=%.3f summary_elapsed=%.3f finish_elapsed=%.3f total_elapsed=%.3f",
        t_base - t_base0,
        t_stock - t_base,
        t_alloc - t_stock,
        t_summary - t_alloc,
        time.perf_counter() - t_summary,
        time.perf_counter() - t0,
    )
    return summary


def get_supplier_stock_shortage_result(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    params = _apply_month_or_date_params(coalesce_params(params))
    df = get_supplier_stock_shortage_df(params)
    row_count = int(len(df)) if isinstance(df, pd.DataFrame) else 0
    attrs = {
        k: v
        for k, v in dict(getattr(df, "attrs", {}) or {}).items()
        if not isinstance(v, (pd.DataFrame, pd.Series, bytes, bytearray))
    }
    source_label = "매입처별 재고부족 배분"
    query_summary = _fmt_analytics_query_summary(params, source_label)
    meta = {
        **attrs,
        "analytics": True,
        "analysis_type": "supplier_stock_shortage",
        "summary_type": "supplier_stock_shortage",
        "query_summary": query_summary,
        "condition": query_summary,
        "source_label": source_label,
        "summary_md": (
            f"매입처별 재고부족 현황: 조회조건 {query_summary} / "
            f"매입처수 {_fmt_num_for_summary(attrs.get('supplier_count'))} / "
            f"관련제품수 {_fmt_num_for_summary(attrs.get('product_count'))} / "
            f"배정부족예상금액 {_fmt_num_for_summary(attrs.get('total_allocated_shortage_amount'))} / "
            f"배분기준 {_fmt_counts_for_summary(attrs.get('allocation_basis_distribution') or {})}"
        ),
    }
    payload = build_result_payload(
        table=TABLE,
        title=ACTION,
        action=ACTION,
        params=params,
        df=df,
        message=f"{ACTION} {row_count:,}건",
    )
    payload.setdefault("meta", {})
    payload["meta"].update(meta)
    return payload
