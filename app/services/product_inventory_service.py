# app/services/product_inventory_service.py
# 제품재고장

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional

import logging
import os
import re
import time

import pandas as pd

from app.db.mssql_client import get_current_company_id
from app.services.dashboard_inventory_frequency_snapshot import (
    FREQUENCY_INSUFFICIENT_GRADE,
    frequency_rows_for_product_subset,
    scope_fingerprint,
)
from app.services.dashboard_inventory_frequency_snapshot_service import (
    read_approved_frequency_snapshot,
    resolve_dashboard_profile_stock_scope,
)
from app.services.ssai_snapshot_repository import SnapshotReadResult
from app.services.rddbc_io_common import (
    clean_text,
    add_unlabeled_name_like_filter,
    coalesce_params,
    normalize_top,
    query_to_df,
)

TABLE = "product_inventory"
TITLE = "제품재고현황 조회"
ACTION = "제품재고현황 조회"
log = logging.getLogger("ssai")

SQL_SERVER_PARAMETER_LIMIT = 2100
SQL_PARAMETER_SAFETY_MARGIN = 32


# -----------------------------------------------------------------------------
# 기본 유틸
# -----------------------------------------------------------------------------
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


def _to_num(sr: pd.Series) -> pd.Series:
    return pd.to_numeric(sr, errors="coerce").fillna(0)


def _safe_div(num: pd.Series | float, den: pd.Series | float) -> pd.Series | float:
    if isinstance(num, pd.Series) or isinstance(den, pd.Series):
        num_s = num if isinstance(num, pd.Series) else pd.Series([num] * len(den), index=den.index)  # type: ignore[arg-type]
        den_s = den if isinstance(den, pd.Series) else pd.Series([den] * len(num), index=num.index)  # type: ignore[arg-type]
        out = pd.Series(0.0, index=num_s.index)
        mask = den_s != 0
        out.loc[mask] = num_s.loc[mask] / den_s.loc[mask]
        return out
    return 0.0 if den == 0 else num / den


def _unit_price_from_amount_qty(amount: pd.Series, qty: pd.Series) -> pd.Series:
    """Return a unit price only when the period has an actual quantity basis."""
    amount_s = pd.to_numeric(amount, errors="coerce")
    qty_s = pd.to_numeric(qty, errors="coerce")
    out = pd.Series(float("nan"), index=qty_s.index, dtype="float64")
    has_quantity = qty_s.notna() & qty_s.ne(0)
    out.loc[has_quantity] = amount_s.loc[has_quantity] / qty_s.loc[has_quantity]
    return out


def _dc_rate_from_unit(insu_unit: pd.Series, unit_price: pd.Series) -> pd.Series:
    """Keep DC blank when the period has no valid unit-price evidence."""
    insu_s = pd.to_numeric(insu_unit, errors="coerce")
    unit_s = pd.to_numeric(unit_price, errors="coerce")
    out = pd.Series(float("nan"), index=insu_s.index, dtype="float64")
    valid = insu_s.notna() & insu_s.ne(0) & unit_s.notna()
    out.loc[valid] = ((insu_s.loc[valid] - unit_s.loc[valid]) * 100) / insu_s.loc[valid]
    return out


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


def _resolve_inventory_dates(params: Dict[str, Any]) -> tuple[str, str]:
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
        "real": "실재고",
        "book": "장부재고",
        "실재고": "실재고",
        "장부재고": "장부재고",
    }.get(text, clean_text(value) or "실재고")


def _group_basis_label(value: Any) -> str:
    text = clean_text(value).lower()
    return {
        "maker": "제조사",
        "order": "발주처",
        "purchase": "매입처",
        "stock": "재고위치",
        "제조사": "제조사",
        "발주처": "발주처",
        "매입처": "매입처",
        "재고위치": "재고위치",
    }.get(text, clean_text(value) or "제조사")


def _price_mode_label(value: Any) -> str:
    text = clean_text(value).lower()
    return {
        "avg": "총평균단가",
        "last": "최종매입가",
        "std": "기준가",
        "insu": "현보험약가",
        "cons": "계약단가",
        "총평균단가": "총평균단가",
        "최종매입가": "최종매입가",
        "기준가": "기준가",
        "현보험약가": "현보험약가",
        "계약단가": "계약단가",
    }.get(text, clean_text(value) or "총평균단가")

def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except Exception:
        return default

# params["top"]은 화면 표시용으로 사용하고, 실제 쿼리에서는 별도의 상한으로 제한한다.
def _inventory_fetch_top(params: Dict[str, Any]) -> int:
    """
    제품재고현황 실제 계산용 최대 건수.

    params["top"]은 화면 표시 건수로 사용하고,
    서비스 내부 계산은 별도 상한으로 가져온다.
    """
    raw = (
        params.get("fetch_top")
        or params.get("_max_top")
        or os.getenv("SIMS_INVENTORY_QUERY_MAX_ROWS")
        or os.getenv("SIMS_EXPORT_MAX_ROWS")
        or "50000"
    )

    return normalize_top(
        raw,
        default=50000,
        max_value=_env_int("SIMS_INVENTORY_QUERY_HARD_MAX_ROWS", 200000),
    )

def _build_inventory_query_summary(
    *,
    date_from: str,
    date_to: str,
    cfg: Dict[str, Any],
    work_params: Dict[str, Any],
    params: Dict[str, Any],
) -> str:
    bits = [
        f"기간 {_fmt_date_text(date_from)} ~ {_fmt_date_text(date_to)}",
        f"기준 {_stock_mode_label(cfg['stock_mode'])}",
        f"집계기준 {_group_basis_label(cfg['group_basis'])}",
        f"단가기준 {_price_mode_label(cfg['price_mode'])}",
    ]

    physic_cd = clean_text(params.get("physic_cd"))
    physic_nm = clean_text(params.get("physic_nm"))

    maker_cd = clean_text(params.get("maker_cd"))
    maker_nm = clean_text(params.get("maker_nm"))

    order_cd = clean_text(params.get("order_cd"))
    order_nm = clean_text(params.get("order_nm"))

    buy_cd = clean_text(params.get("buy_cd"))
    buy_nm = clean_text(params.get("buy_nm"))

    product_group_nm = clean_text(params.get("product_group_nm"))
    product_di_nm = clean_text(params.get("product_di_nm"))
    product_class_nm = clean_text(params.get("product_class_nm"))


    if physic_cd and physic_nm:
        bits.append(f"제품 {physic_cd} ({physic_nm})")
    elif physic_cd:
        bits.append(f"제품코드 {physic_cd}")
    elif physic_nm:
        bits.append(f"제품명 {physic_nm}")

    if product_group_nm:
        bits.append(f"제품그룹명 {product_group_nm}")
    if product_di_nm:
        bits.append(f"제품구분명 {product_di_nm}")
    if product_class_nm:
        bits.append(f"제품분류명 {product_class_nm}")

    if maker_cd and maker_nm:
        bits.append(f"제조사 {maker_cd} ({maker_nm})")
    elif maker_cd:
        bits.append(f"제조사코드 {maker_cd}")
    elif maker_nm:
        bits.append(f"제조사명 {maker_nm}")

    if order_cd and order_nm:
        bits.append(f"발주처 {order_cd} ({order_nm})")
    elif order_cd:
        bits.append(f"발주처코드 {order_cd}")
    elif order_nm:
        bits.append(f"발주처명 {order_nm}")

    if buy_cd and buy_nm:
        bits.append(f"매입처 {buy_cd} ({buy_nm})")
    elif buy_cd:
        bits.append(f"매입처코드 {buy_cd}")
    elif buy_nm:
        bits.append(f"매입처명 {buy_nm}")

    unlabeled_name = clean_text(params.get("nlq_unlabeled_name"))
    if unlabeled_name:
        bits.append(f"통합검색 {unlabeled_name}")

    stock_text = _stock_condition_text(work_params)

    if stock_text:
        bits.append(f"재고위치 {stock_text}")

    frequency_grade = _normalize_product_inventory_frequency_filter(params.get("frequency_grade"))
    if frequency_grade:
        bits.append(f"출고빈도 {frequency_grade}")


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


def _build_inventory_header_md(meta: Dict[str, Any]) -> str:
    # 현재고 조회는 위치별 상세 표 자체가 조건과 합계를 충분히 보여 준다.
    # 화면 상단의 별도 요약은 현재고 전용 계약에서 제외한다.
    if bool(meta.get("current_stock_query")):
        return ""

    current_stock_summary = meta.get("current_stock_summary") or {}
    if isinstance(current_stock_summary, dict):
        maker_name = clean_text(current_stock_summary.get("maker_name"))
        product_count = int(current_stock_summary.get("product_count") or 0)
        condition = f"제조사 {maker_name}" if maker_name else "검색 조건"
        return (
            f"현재고 조건: {condition}\n\n"
            "```text\n"
            f"제품수    전체 재고수량\n{product_count:,}        "
            f"{_fmt_header_num(meta.get('sum_stock_qty'))}\n```"
        )

    info = meta.get("product_info") or {}
    if not isinstance(info, dict):
        info = {}

    line1 = (
        "제품정보: "
        f"제품코드 {clean_text(info.get('제품코드'))} / "
        f"제품명 {clean_text(info.get('제품명'))} / "
        f"규격 {clean_text(info.get('규격'))} / "
        f"현보험약가 {_fmt_header_num(info.get('현보험약가'))} / "
        f"보험코드 {clean_text(info.get('보험코드'))} / "
        f"표준코드 {clean_text(info.get('표준코드'))} / "
        f"제조사명 {clean_text(info.get('제조사명'))} / "
        f"발주처명 {clean_text(info.get('발주처명'))} / "
        f"제품그룹명 {clean_text(info.get('제품그룹명'))} / "
        f"제품구분명 {clean_text(info.get('제품구분명'))} / "
        f"제품분류명 {clean_text(info.get('제품분류명'))} / "
        f"특수관리제품명 {clean_text(info.get('특수관리제품명'))}"
    )

    line2 = "이월수량    입고수량    출고수량    재고수량"
    line3 = (
        f"{_fmt_header_num(meta.get('sum_carry_qty'))}               "
        f"{_fmt_header_num(meta.get('sum_in_qty'))}                 "
        f"{_fmt_header_num(meta.get('sum_out_qty'))}               "
        f"{_fmt_header_num(meta.get('sum_stock_qty'))}"
    )

    return line1 + "\n```text\n" + line2 + "\n" + line3 + "\n```"


def _inventory_text_payload(
    *,
    message: str,
    params: Dict[str, Any],
    cfg: Optional[Dict[str, Any]] = None,
    date_from: str = "",
    date_to: str = "",
    work_params: Optional[Dict[str, Any]] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    work_params = dict(work_params or params or {})
    meta = dict(meta or {})

    # text payload에서도 화면 요약 카드가 nan으로 표시되지 않도록 기본 합계값 보정
    zero_defaults = {
        "detail_count": 0,
        "sum_carry_qty": 0.0,
        "sum_in_qty": 0.0,
        "sum_out_qty": 0.0,
        "sum_stock_qty": 0.0,
        "sum_stock_amt": 0.0,
        "sum_insu_amt": 0.0,
    }
    for k, v in zero_defaults.items():
        meta.setdefault(k, v)

    # 실제 0건 결과는 요약 수치를 숫자 0으로 고정한다. text payload를
    # 현재표/다운로드 원본으로 오인하지 않도록 표 데이터도 만들지 않는다.
    if meta.get("result_status") == "no_data":
        meta.update(zero_defaults)
        meta["row_count"] = 0
        meta["row_count_total"] = 0


    out_params = {
        **params,
        "date_from": date_from,
        "date_to": date_to,
    }

    if cfg:
        out_params.update(
            {
                "stock_mode": _stock_mode_label(cfg.get("stock_mode")),
                "group_basis": _group_basis_label(cfg.get("group_basis")),
                "price_mode": _price_mode_label(cfg.get("price_mode")),
                "stock_cds": _resolve_stock_codes(work_params),
            }
        )

        query_summary = _build_inventory_query_summary(
            date_from=date_from,
            date_to=date_to,
            cfg=cfg,
            work_params=work_params,
            params=params,
        )
        note = "조회건수: 0건"

#       @@@@@@@@@@@@@@@@@@@@@@@@@@ 
#        meta["query_summary"] = query_summary
#        meta["summary_md"] = "📊 " + note
#        meta["note"] = note
#       @@@@@@@@@@@@@@@@@@@@@@@@@@

        meta["query_summary"] = _build_inventory_query_summary(
            date_from=date_from,
            date_to=date_to,
            cfg=cfg,
            work_params=work_params,
            params=params,
        )
        meta["summary_md"] = "📊 " + note
        meta["note"] = note

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


def _round_money(sr: pd.Series) -> pd.Series:
    return pd.to_numeric(sr, errors="coerce").fillna(0).round(0)

_DISPLAY_NUMERIC_COLS_260 = {
    "이월수량", "이월단가", "이월DC율", "이월금액",
    "입고수량", "입고단가", "입고DC율", "입고금액",
    "출고수량", "출고단가", "출고DC율", "출고금액",
    "재고수량", "재고단가", "재고DC율", "재고금액",
    "현보험약가", "보험금액", "3개월 출고발생수",
}

_FREQUENCY_GRADE_COLUMN = "출고빈도등급"
_FREQUENCY_COUNT_COLUMN = "3개월 출고발생수"
_FREQUENCY_FILTER_ALL = "전체"
_FREQUENCY_FILTER_VALUES = ("A", "B", "C", "D", "E", "X", FREQUENCY_INSUFFICIENT_GRADE)


def _place_frequency_columns(df: pd.DataFrame) -> pd.DataFrame:
    columns = [
        column
        for column in df.columns
        if column not in {_FREQUENCY_GRADE_COLUMN, _FREQUENCY_COUNT_COLUMN}
    ]
    product_code_index = columns.index("제품코드") + 1
    columns[product_code_index:product_code_index] = [
        _FREQUENCY_GRADE_COLUMN,
        _FREQUENCY_COUNT_COLUMN,
    ]
    return df.loc[:, columns]


def _normalize_product_inventory_frequency_filter(value: Any) -> str:
    """Return the one canonical product-inventory frequency filter value."""
    normalized = clean_text(value)
    if normalized in {"", _FREQUENCY_FILTER_ALL}:
        return ""
    if normalized in _FREQUENCY_FILTER_VALUES:
        return normalized
    return ""


def filter_product_inventory_frequency_rows(df: pd.DataFrame, frequency_grade: Any) -> pd.DataFrame:
    """Filter already attached frequency values without recalculating a subset."""
    selected = _normalize_product_inventory_frequency_filter(frequency_grade)
    if not selected or _FREQUENCY_GRADE_COLUMN not in df.columns:
        return df
    return df.loc[df[_FREQUENCY_GRADE_COLUMN].fillna("").astype(str) == selected].copy()


def _inventory_frequency_context(params: Dict[str, Any], date_to: str) -> tuple[int | None, str, str]:
    """Return the company/evaluation key without letting request params cross companies."""
    context_company = get_current_company_id()
    requested_company = clean_text(params.get("company_id"))
    try:
        requested_id = int(requested_company) if requested_company else None
    except (TypeError, ValueError):
        return None, "", "company_id_invalid"
    if context_company is not None and requested_id is not None and int(context_company) != requested_id:
        return None, "", "company_context_mismatch"
    company_id = int(context_company) if context_company is not None else requested_id
    explicit_month = "".join(ch for ch in clean_text(params.get("evaluation_month")) if ch.isdigit())[:6]
    evaluation_month = explicit_month if len(explicit_month) == 6 else str(date_to or "")[:6]
    if company_id is None:
        return None, evaluation_month, "company_context_missing"
    if len(evaluation_month) != 6:
        return company_id, "", "evaluation_month_missing"
    return company_id, evaluation_month, ""


def attach_dashboard_frequency_snapshot(
    df: pd.DataFrame,
    *,
    params: Dict[str, Any],
    date_to: str,
    profile_scope_resolver: Callable[..., Any] = resolve_dashboard_profile_stock_scope,
    snapshot_reader: Callable[..., SnapshotReadResult] = read_approved_frequency_snapshot,
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    """Attach the Dashboard's approved frequency snapshot to inventory output only.

    The inventory query's selected stock filters remain untouched. The snapshot
    key always uses the company-saved Dashboard stock scope, so a subset view
    never re-ranks products or falls back to ERP activity rows.
    """
    out = df.copy()
    if "제품코드" not in out.columns:
        return out, {
            "frequency_snapshot_status": "missing",
            "frequency_snapshot_reason": "product_code_column_missing",
            "frequency_missing_product_count": 0,
            "frequency_additional_erp_source_call_count": 0,
        }

    product_codes = out["제품코드"].fillna("").astype(str).str.strip()
    detail_mask = product_codes.ne("")
    detail_product_codes = set(product_codes.loc[detail_mask])
    out[_FREQUENCY_GRADE_COLUMN] = ""
    out[_FREQUENCY_COUNT_COLUMN] = pd.Series(pd.NA, index=out.index, dtype="Int64")
    company_id, evaluation_month, context_reason = _inventory_frequency_context(params, date_to)
    base_meta: Dict[str, Any] = {
        "frequency_snapshot_company_id": company_id,
        "frequency_snapshot_evaluation_month": evaluation_month,
        "frequency_snapshot_status": "missing",
        "frequency_snapshot_reason": context_reason,
        "frequency_snapshot_scope_fingerprint": "",
        "frequency_snapshot_generation_no": None,
        "frequency_snapshot_checksum": "",
        "frequency_missing_product_count": len(detail_product_codes),
        "frequency_additional_erp_source_call_count": 0,
    }
    if not detail_mask.any() or context_reason:
        if detail_mask.any():
            out.loc[detail_mask, _FREQUENCY_GRADE_COLUMN] = FREQUENCY_INSUFFICIENT_GRADE
        return _place_frequency_columns(out), base_meta

    try:
        scope = profile_scope_resolver(company_id=company_id)
        snapshot = snapshot_reader(
            company_id=company_id,
            evaluation_month=evaluation_month,
            stock_codes=list(scope.stock_codes),
        )
    except Exception as exc:
        base_meta["frequency_snapshot_reason"] = f"frequency_snapshot_unavailable:{type(exc).__name__}"
        out.loc[detail_mask, _FREQUENCY_GRADE_COLUMN] = FREQUENCY_INSUFFICIENT_GRADE
        return _place_frequency_columns(out), base_meta

    base_meta.update(
        {
            "frequency_snapshot_status": str(snapshot.status or "missing"),
            "frequency_snapshot_reason": str(snapshot.reason or ""),
            "frequency_snapshot_scope": list(scope.stock_codes),
            "frequency_snapshot_scope_fingerprint": scope_fingerprint(scope.stock_codes),
            "frequency_snapshot_generation_no": snapshot.generation_no,
            "frequency_snapshot_checksum": str(snapshot.checksum or ""),
        }
    )
    frequency_rows = frequency_rows_for_product_subset(
        snapshot,
        product_codes.loc[detail_mask].drop_duplicates().tolist(),
    )
    frequency_frame = pd.DataFrame(frequency_rows)
    if frequency_frame.empty:
        grade_by_product = pd.Series(dtype="object")
        occurrence_by_product = pd.Series(dtype="float64")
    else:
        frequency_frame["product_code"] = frequency_frame["product_code"].fillna("").astype(str).str.strip()
        frequency_frame = frequency_frame.loc[frequency_frame["product_code"].ne("")]
        frequency_frame = frequency_frame.drop_duplicates(subset=["product_code"], keep="last")
        grade_by_product = frequency_frame.set_index("product_code")["frequency_grade"]
        occurrence_by_product = frequency_frame.set_index("product_code")["occurrence_count_3m"]

    attached_grades = product_codes.map(grade_by_product).fillna(FREQUENCY_INSUFFICIENT_GRADE).astype(str)
    attached_occurrences = pd.to_numeric(product_codes.map(occurrence_by_product), errors="coerce")
    attached_valid = (
        detail_mask
        & attached_grades.ne(FREQUENCY_INSUFFICIENT_GRADE)
        & attached_occurrences.notna()
    )
    out.loc[detail_mask, _FREQUENCY_GRADE_COLUMN] = attached_grades.loc[detail_mask]
    out.loc[attached_valid, _FREQUENCY_COUNT_COLUMN] = attached_occurrences.loc[attached_valid].astype("Int64")
    missing_mask = detail_mask & ~attached_valid
    base_meta["frequency_missing_product_count"] = int(product_codes.loc[missing_mask].nunique())
    return _place_frequency_columns(out), base_meta

def _finalize_display_df_260(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    for col in out.columns:
        if col in _DISPLAY_NUMERIC_COLS_260:
            # 0은 실제 수량/금액/단가일 수 있다. 거래 근거가 없는
            # 단가·DC는 집계 단계에서 이미 NA로 만들어 전달한다.
            out[col] = pd.to_numeric(out[col], errors="coerce")
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

def _clean_display_df_260(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    for col in _DISPLAY_NUMERIC_COLS_260:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    try:
        out = out.replace({None: "", "None": "", "nan": "", "<NA>": "", "NaT": ""})
        out = out.where(pd.notna(out), "")
    except Exception:
        pass

    return out



def _resolve_stock_mode(value: Any) -> str:
    text = clean_text(value).lower()
    if text in {"장부재고", "장부수불", "book", "ledger"}:
        return "book"
    return "real"


def _resolve_group_basis(value: Any) -> str:
    text = clean_text(value).lower()
    if text in {"재고위치", "stock", "warehouse"}:
        return "stock"
    if text in {"매입처", "purchase", "buy"}:
        return "purchase"
    if text in {"발주처", "order", "ord"}:
        return "order"
    return "maker"


def _resolve_price_mode(value: Any) -> str:
    text = clean_text(value).lower()

    # 현재 화면 노출 기준
    if text in {"최종매입가", "last", "latest"}:
        return "last"
    if text in {"기준가", "standard", "std", "otc"}:
        return "std"
    if text in {"현보험약가", "보험가", "insu", "insurance"}:
        return "insu"

    # 향후 계약단가(입고) 확장용
    if text in {"계약단가", "계약단가(입고)", "cons", "rcons"}:
        return "cons"

    # 기본 = 총평균단가
    return "avg"

def _stock_condition_text(params: Dict[str, Any]) -> str:
    names: list[str] = []

    raw_names = params.get("stock_names")
    if isinstance(raw_names, (list, tuple, set)):
        names.extend([clean_text(x) for x in raw_names if clean_text(x)])

    one_name = _stock_name_condition(params)
    if one_name:
        names.append(one_name)

    label_text = clean_text(params.get("stock_label_text"))
    if label_text and label_text != "전체":
        names.append(label_text)

    # 중복 제거
    clean_names: list[str] = []
    seen = set()
    for nm in names:
        if nm and nm not in seen:
            clean_names.append(nm)
            seen.add(nm)

    codes = _resolve_stock_codes(params)

    if clean_names and codes:
        return f"{', '.join(clean_names)} ({','.join(codes)})"
    if clean_names:
        return ", ".join(clean_names)
    if codes:
        return ",".join(codes)

    return ""


def _normalize_stock_codes(params: Dict[str, Any]) -> list[str]:
    raw = params.get("stock_cds")
    values: list[str] = []

    if isinstance(raw, (list, tuple, set)):
        values = [clean_text(x) for x in raw]
    else:
        one = clean_text(params.get("stock_cd_csv") or params.get("stock_cd"))
        if one:
            values = [clean_text(x) for x in str(one).split(",")]

    out: list[str] = []
    seen = set()
    for v in values:
        if v and v not in seen:
            out.append(v)
            seen.add(v)
    return out



def _stock_name_condition(params: Dict[str, Any]) -> str:
    return clean_text(
        params.get("stock_nm")
        or params.get("stock_name")
        or params.get("stock_location_nm")
    )


def _resolve_stock_codes(params: Dict[str, Any]) -> list[str]:
    """
    제품재고현황 재고위치 조건 정규화.

    우선순위:
    1. stock_cds / stock_cd_csv / stock_cd 코드 조건
    2. stock_nm 이름 조건을 Rddbc010(0018)에서 코드로 변환

    주의:
    - 이름 조건이 있는데 코드 변환에 실패하면 빈 리스트를 반환한다.
    - 호출부에서 이름 조건 실패를 notice/0건으로 처리해야 한다.
    """
    codes = _normalize_stock_codes(params)
    if codes:
        return codes

    stock_nm = _stock_name_condition(params)
    if not stock_nm:
        return []

    # User input may omit the ERP display-name prefix or spaces.  Normalize
    # only for matching; the displayed stock-location name remains unchanged.
    normalized_stock_nm = re.sub(r"[.\s]+", "", stock_nm)
    if normalized_stock_nm.isdigit() and len(normalized_stock_nm) <= 6:
        normalized_stock_nm = normalized_stock_nm.zfill(5)

    sql = """
SELECT
    LTRIM(RTRIM(Rd01_Tcode)) AS stock_cd
FROM dbo.Rddbc010
WHERE Rd01_Gcode = '0018'
  AND ISNULL(Rd01_Del_Flag, '') <> 'E'
  AND (
        LTRIM(RTRIM(Rd01_Tcode)) = %(stock_cd_exact)s
        OR REPLACE(REPLACE(LTRIM(RTRIM(ISNULL(Rd01_Hnm, ''))), '.', ''), ' ', '')
           LIKE %(stock_nm_normalized_like)s
      )
ORDER BY Rd01_Tcode
"""
    try:
        df = query_to_df(
            sql,
            {
                "stock_cd_exact": normalized_stock_nm,
                "stock_nm_normalized_like": f"%{normalized_stock_nm}%",
            },
        )
    except Exception:
        return []

    if not isinstance(df, pd.DataFrame) or df.empty or "stock_cd" not in df.columns:
        return []

    out: list[str] = []
    seen = set()
    for v in df["stock_cd"].fillna("").astype(str).str.strip().tolist():
        if v and v not in seen:
            out.append(v)
            seen.add(v)

    return out


def resolve_inventory_stock_codes(params: Dict[str, Any]) -> list[str]:
    """Resolve inventory stock-location codes from explicit codes or a name."""
    return _resolve_stock_codes(params)

def _stock_not_found_message(*, stock_name: str = "", stock_codes: list[str] | None = None) -> str:
    codes = [clean_text(x) for x in (stock_codes or []) if clean_text(x)]

    if stock_name:
        target = f"재고위치 '{stock_name}'"
    elif codes:
        target = f"재고위치코드 '{', '.join(codes)}'"
    else:
        target = "입력한 재고위치"

    return (
        f"{target}에 해당하는 등록 코드를 찾지 못했습니다.\n\n"
        "재고위치명과 재고위치코드는 코드마스터 Rddbc010의 그룹코드 0018 기준으로 조회합니다.\n"
        "코드마스터에 등록된 명칭/코드와 입력값이 다르면 조회되지 않습니다.\n"
        "예: '본사창고'가 아니라 '본사 창고'처럼 공백이 포함되어 등록되어 있을 수 있습니다.\n"
        "재고위치명을 확인하거나 등록된 재고위치코드로 다시 조회해 주세요."
    )


def _registered_stock_codes(codes: list[str]) -> list[str]:
    """
    입력된 재고위치코드가 코드마스터 Rddbc010 / Gcode=0018에 실제 등록되어 있는지 확인한다.

    주의:
    - 코드 자릿수는 업무코드의 Rd01_Col_Num 기준이므로 여기서 임의 보정하지 않는다.
    - 00001과 000001은 서로 다른 입력으로 본다.
    """
    values: list[str] = []
    seen = set()

    for c in codes or []:
        v = clean_text(c)
        if v and v not in seen:
            values.append(v)
            seen.add(v)

    if not values:
        return []

    sql_params: Dict[str, Any] = {}
    bind_keys: list[str] = []

    for i, code in enumerate(values):
        key = f"stock_code_{i}"
        sql_params[key] = code
        bind_keys.append(f"%({key})s")

    sql = f"""
SELECT
    LTRIM(RTRIM(Rd01_Tcode)) AS stock_cd
FROM dbo.Rddbc010
WHERE Rd01_Gcode = '0018'
  AND ISNULL(Rd01_Del_Flag, '') <> 'E'
  AND LTRIM(RTRIM(Rd01_Tcode)) IN ({", ".join(bind_keys)})
ORDER BY Rd01_Tcode
"""

    try:
        df = query_to_df(sql, sql_params)
    except Exception:
        return []

    if not isinstance(df, pd.DataFrame) or df.empty or "stock_cd" not in df.columns:
        return []

    out: list[str] = []
    seen2 = set()
    for v in df["stock_cd"].fillna("").astype(str).str.strip().tolist():
        if v and v not in seen2:
            out.append(v)
            seen2.add(v)

    return out


def _append_in_clause(where: list[str], sql_params: Dict[str, Any], field_expr: str, values: list[str], prefix: str) -> None:
    if not values:
        return
    bind_keys = []
    for i, val in enumerate(values):
        key = f"{prefix}_{i}"
        sql_params[key] = val
        bind_keys.append(f"%({key})s")
    where.append(f"{field_expr} IN ({', '.join(bind_keys)})")


def _bounded_product_scope_safe_limit(base_sql: str, *, bind_occurrences: int = 2) -> int:
    fixed_bind_count = len(re.findall(r"%\(([^)]+)\)s", base_sql))
    return max(
        0,
        (
            SQL_SERVER_PARAMETER_LIMIT
            - fixed_bind_count
            - SQL_PARAMETER_SAFETY_MARGIN
        ) // max(1, int(bind_occurrences)),
    )


def _resolve_explicit_product_name_scope(
    params: Dict[str, Any],
    *,
    safe_limit: int,
) -> tuple[list[str], Dict[str, Any]]:
    """Resolve the complete explicit product-name LIKE set within a safe SQL bound."""
    started = time.perf_counter()
    product_name = clean_text(params.get("physic_nm"))
    meta: Dict[str, Any] = {
        "product_name_scope": bool(product_name),
        "candidate_count": 0,
        "scope_applied": False,
        "safe_limit": max(0, int(safe_limit)),
        "fallback_reason": "",
        "candidate_elapsed_ms": 0.0,
    }
    if (
        not product_name
        or clean_text(params.get("physic_cd"))
        or clean_text(params.get("nlq_unlabeled_name"))
        or bool(params.get("current_stock_query"))
    ):
        meta["product_name_scope"] = False
        meta["fallback_reason"] = "not_explicit_product_name"
        return [], meta
    if safe_limit <= 0:
        meta["fallback_reason"] = "sql_parameter_limit"
        return [], meta

    sql = f"""
SELECT DISTINCT TOP ({safe_limit + 1})
    LTRIM(RTRIM(P.Rd04_Physic_Cd)) AS physic_cd
FROM dbo.Rddbc040 AS P
WHERE ISNULL(P.Rd04_Del_Flag, '') <> 'E'
  AND P.Rd04_Physic_Nm LIKE %(explicit_product_name_like)s
ORDER BY LTRIM(RTRIM(P.Rd04_Physic_Cd))
"""
    try:
        candidates = query_to_df(
            sql,
            {"explicit_product_name_like": f"%{product_name}%"},
        )
    except Exception:
        meta["fallback_reason"] = "candidate_query_failed"
        meta["candidate_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return [], meta

    if not isinstance(candidates, pd.DataFrame) or "physic_cd" not in candidates.columns:
        meta["fallback_reason"] = "candidate_result_unavailable"
        meta["candidate_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return [], meta

    product_codes = list(dict.fromkeys(
        clean_text(value)
        for value in candidates["physic_cd"].tolist()
        if clean_text(value)
    ))
    meta["candidate_count"] = len(product_codes)
    meta["candidate_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    if not product_codes:
        meta["fallback_reason"] = "no_candidates"
        return [], meta
    if len(product_codes) > safe_limit:
        meta["fallback_reason"] = "sql_parameter_limit"
        return [], meta

    meta["scope_applied"] = True
    return product_codes, meta


def _last_cost_product_scope_plan(
    params: Dict[str, Any],
    source_df: pd.DataFrame,
    base_sql: str,
) -> tuple[list[str], Dict[str, Any]]:
    """Bound an unlabeled last-cost lookup to products already in its source rows."""
    fixed_bind_count = len(re.findall(r"%\(([^)]+)\)s", base_sql))
    product_bind_occurrences = 2
    safe_limit = _bounded_product_scope_safe_limit(
        base_sql,
        bind_occurrences=product_bind_occurrences,
    )
    meta: Dict[str, Any] = {
        "applied": False,
        "product_code_count": 0,
        "safe_limit": safe_limit,
        "fixed_parameter_count": fixed_bind_count,
        "product_bind_occurrences": product_bind_occurrences,
        "fallback_reason": "",
    }

    if not clean_text(params.get("nlq_unlabeled_name")):
        meta["fallback_reason"] = "not_unlabeled"
        return [], meta
    if not isinstance(source_df, pd.DataFrame) or source_df.empty or "physic_cd" not in source_df.columns:
        meta["fallback_reason"] = "source_product_scope_unavailable"
        return [], meta

    product_codes = list(dict.fromkeys(
        clean_text(value)
        for value in source_df["physic_cd"].tolist()
        if clean_text(value)
    ))
    meta["product_code_count"] = len(product_codes)
    if not product_codes:
        meta["fallback_reason"] = "source_product_scope_empty"
        return [], meta
    if len(product_codes) > safe_limit:
        meta["fallback_reason"] = "sql_parameter_limit"
        return [], meta

    meta["applied"] = True
    return product_codes, meta


def _month_first(date_yyyymmdd: str) -> str:
    return date_yyyymmdd[:6] + "01"


def _prev_day(date_yyyymmdd: str) -> str:
    dt = datetime.strptime(date_yyyymmdd, "%Y%m%d")
    return (dt - timedelta(days=1)).strftime("%Y%m%d")


def _group_label(basis: str) -> str:
    return {
        "maker": "제조사",
        "order": "발주처",
        "purchase": "매입처",
        "stock": "재고위치",
    }.get(basis, "제조사")


def _settings(params: Dict[str, Any]) -> Dict[str, Any]:
    stock_mode = _resolve_stock_mode(params.get("stock_mode") or params.get("stock_kind"))
    group_basis = _resolve_group_basis(params.get("group_basis") or params.get("group_type"))
    price_mode = _resolve_price_mode(params.get("price_mode") or params.get("unit_basis"))

    if stock_mode == "real":
        month_table = "dbo.Rddbc210"
        month_alias = "Rd21"
        in_date_field = "T.Rd11_In_YyMmDd"
        out_date_field = "T.Rd12_Out_YyMmDd"
        in_qty_expr = "ISNULL(T.Rd11_Quantity, 0) + ISNULL(T.Rd11_Oquantity, 0)"
        out_qty_expr = "ISNULL(T.Rd12_Quantity, 0) + ISNULL(T.Rd12_Oquantity, 0)"
        in_exclude_prefix = ("2",)
        out_exclude_prefix = ("7",)
        month_in_qty_expr = "ISNULL(M.Rd21_In_Quantity, 0) + ISNULL(M.Rd21_In_Oquantity, 0)"
        month_out_qty_expr = "ISNULL(M.Rd21_Out_Quantity, 0) + ISNULL(M.Rd21_Out_Oquantity, 0)"
        month_in_amt_expr = "ISNULL(M.Rd21_In_Supply_Price, 0) + ISNULL(M.Rd21_In_Tax_Price, 0)"
        month_out_amt_expr = "ISNULL(M.Rd21_Out_Supply_Price, 0) + ISNULL(M.Rd21_Out_Tax_Price, 0)"
    else:
        month_table = "dbo.Rddbc220"
        month_alias = "Rd22"
        in_date_field = "T.Rd11_Trans_YyMmDd"
        out_date_field = "T.Rd12_Trans_YyMmDd"
        in_qty_expr = "ISNULL(T.Rd11_Quantity, 0)"
        out_qty_expr = "ISNULL(T.Rd12_Quantity, 0)"
        in_exclude_prefix = ("3",)
        out_exclude_prefix = ("8",)
        month_in_qty_expr = "ISNULL(M.Rd22_In_Quantity, 0)"
        month_out_qty_expr = "ISNULL(M.Rd22_Out_Quantity, 0)"
        month_in_amt_expr = "ISNULL(M.Rd22_In_Supply_Price, 0) + ISNULL(M.Rd22_In_Tax_Price, 0)"
        month_out_amt_expr = "ISNULL(M.Rd22_Out_Supply_Price, 0) + ISNULL(M.Rd22_Out_Tax_Price, 0)"

    return {
        "stock_mode": stock_mode,
        "group_basis": group_basis,
        "price_mode": price_mode,
        "month_table": month_table,
        "month_alias": month_alias,
        "in_date_field": in_date_field,
        "out_date_field": out_date_field,
        "in_qty_expr": in_qty_expr,
        "out_qty_expr": out_qty_expr,
        "month_in_qty_expr": month_in_qty_expr,
        "month_out_qty_expr": month_out_qty_expr,
        "month_in_amt_expr": month_in_amt_expr,
        "month_out_amt_expr": month_out_amt_expr,
        "in_exclude_prefix": in_exclude_prefix,
        "out_exclude_prefix": out_exclude_prefix,
        "current_stock_query": bool(params.get("current_stock_query")),
        "stock_location_name_map": {
            clean_text(code): clean_text(name)
            for code, name in dict(params.get("stock_location_name_map") or {}).items()
            if clean_text(code) and clean_text(name)
        },
    }


def _month_last(yyyymmdd: str) -> str:
    year = int(yyyymmdd[:4])
    month = int(yyyymmdd[4:6])
    next_month = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    return (next_month - timedelta(days=1)).strftime("%Y%m%d")


def resolve_product_inventory_source_path(params: Optional[Dict[str, Any]] = None) -> str:
    """Choose the monthly ledger or explicitly dated detail source path."""
    source_params = dict(params or {})
    if bool(source_params.get("current_stock_query")):
        return "date_exact"
    raw = clean_text(source_params.get("source_path") or source_params.get("inventory_source_path")).lower()
    if raw in {"monthly", "date_exact"}:
        return raw

    if not clean_text(source_params.get("date_from")) and not clean_text(source_params.get("date_to")) and not clean_text(source_params.get("month_from")) and not clean_text(source_params.get("month_to")):
        return "monthly"

    date_from, date_to = _resolve_inventory_dates(source_params)
    if date_from[:6] == date_to[:6] and date_from.endswith("01") and date_to == _month_last(date_from):
        return "monthly"
    return "date_exact"


def _inventory_predicate_mode(params: Dict[str, Any]) -> str:
    if clean_text(params.get("nlq_unlabeled_name")):
        return "unlabeled_like_or"
    if clean_text(params.get("maker_cd")):
        return "manufacturer_code"
    if clean_text(params.get("maker_nm")):
        return "manufacturer_like"
    if clean_text(params.get("physic_cd")):
        return "product_code"
    if params.get("_product_inventory_explicit_product_scope_applied"):
        return "product_code_scope"
    if clean_text(params.get("physic_nm")):
        return "product_like"
    if clean_text(params.get("ven_cd")) or clean_text(params.get("ven_nm")):
        return "vendor"
    return "standard"


def _prefix_not_in(field_expr: str, prefixes: tuple[str, ...]) -> str:
    if not prefixes:
        return "1 = 1"
    values = ", ".join(f"'{p}'" for p in prefixes)
    return f"LEFT({field_expr}, 1) NOT IN ({values})"


def _common_descriptor_sql(group_cd_expr: str, group_nm_expr: str) -> str:
    return f"""
        LTRIM(RTRIM({group_cd_expr})) AS group_cd,
        LTRIM(RTRIM({group_nm_expr})) AS group_nm,
        LTRIM(RTRIM(ISNULL(BuyVen.Rd03_Ven_Cd, ''))) AS buy_cd,
        LTRIM(RTRIM(ISNULL(BuyVen.Rd03_Ven_Nm, ''))) AS buy_nm,
        LTRIM(RTRIM(ISNULL(OrderVen.Rd03_Ven_Cd, ''))) AS order_cd,
        LTRIM(RTRIM(ISNULL(OrderVen.Rd03_Ven_Nm, ''))) AS order_nm,
        LTRIM(RTRIM(ISNULL(MakerVen.Rd03_Ven_Cd, ''))) AS maker_cd,
        LTRIM(RTRIM(ISNULL(MakerVen.Rd03_Ven_Nm, ''))) AS maker_nm,

        LTRIM(RTRIM(ISNULL(PG.Rd01_Hnm, ''))) AS product_group_nm,
        LTRIM(RTRIM(ISNULL(PD.Rd01_Hnm, ''))) AS product_di_nm,
        LTRIM(RTRIM(ISNULL(PF.Rd01_Hnm, ''))) AS product_class_nm,

        LTRIM(RTRIM(ISNULL(P.Rd04_Physic_Cd, ''))) AS physic_cd,
        LTRIM(RTRIM(ISNULL(P.Rd04_Physic_Nm, ''))) AS physic_nm,
        LTRIM(RTRIM(ISNULL(P.Rd04_Standard, ''))) AS standard,
        LTRIM(RTRIM(ISNULL(P.Rd04_Insu_Cd, ''))) AS kd_cd,
        LTRIM(RTRIM(ISNULL(P.Rd04_Old_Insu_Cd, ''))) AS edi_cd,
        LTRIM(RTRIM(ISNULL(StdCd.Rd046_Standard_Cd, ''))) AS std_cd,
        LTRIM(RTRIM(ISNULL(P.Rd04_Pack_Unit, ''))) AS pack_unit,
        CAST(ISNULL(P.Rd04_In_Unit_Cost, 0) AS decimal(18, 4)) AS master_unit_cost,
        ISNULL(P.Rd04_Insu_Date, '') AS insu_date,
        ISNULL(P.Rd04_Before_Insu_Date, '') AS before_insu_date,
        CAST(ISNULL(P.Rd04_Insu_Price, 0) AS decimal(18, 4)) AS insu_price,
        CAST(ISNULL(P.Rd04_Before_Insu_Price, 0) AS decimal(18, 4)) AS before_insu_price,
        CAST(ISNULL(P.Rd04_Acc_Unit, 0) AS decimal(18, 4)) AS acc_unit,
        ISNULL(P.Rd04_Physic_Tax, '') AS physic_tax,
        LTRIM(RTRIM(ISNULL(PhysicGu.Rd01_Hnm, ''))) AS special_manage_nm
    """


def _vendor_group_expr(basis: str, purchase_field: str, stock_field: str = "") -> tuple[str, str]:
    if basis == "stock" and stock_field:
        return stock_field, f"LTRIM(RTRIM(ISNULL({stock_field}, '')))"
    if basis == "purchase":
        return purchase_field, f"ISNULL(BuyVen.Rd03_Ven_Nm, {purchase_field})"
    if basis == "order":
        return "P.Rd04_OrVen_Cd", "ISNULL(OrderVen.Rd03_Ven_Nm, P.Rd04_OrVen_Cd)"
    return "P.Rd04_Ven_Cd", "ISNULL(MakerVen.Rd03_Ven_Nm, P.Rd04_Ven_Cd)"


def _apply_master_filters(
    where: list[str],
    sql_params: Dict[str, Any],
    params: Dict[str, Any],
    buy_field_expr: str,
) -> None:
    current_stock_scope = clean_text(params.get("current_stock_entity_scope"))
    current_stock_scoped = current_stock_scope in {
        "manufacturer", "product", "manufacturer_or_product", "manufacturer_and_product",
    }
    explicit_product_codes = list(dict.fromkeys(
        clean_text(value)
        for value in (params.get("_product_inventory_explicit_product_codes") or [])
        if clean_text(value)
    ))
    explicit_product_scope_applied = bool(
        params.get("_product_inventory_explicit_product_scope_applied")
        and explicit_product_codes
    )
    if clean_text(params.get("physic_cd")):
        where.append("P.Rd04_Physic_Cd = %(physic_cd)s")
    if explicit_product_scope_applied and not current_stock_scoped:
        _append_in_clause(
            where,
            sql_params,
            "P.Rd04_Physic_Cd",
            explicit_product_codes,
            "explicit_product",
        )
    elif clean_text(params.get("physic_nm")) and not current_stock_scoped:
        sql_params["physic_nm_like"] = f"%{clean_text(params.get('physic_nm'))}%"
        where.append("P.Rd04_Physic_Nm LIKE %(physic_nm_like)s")

    if current_stock_scoped:
        manufacturer_codes = list(dict.fromkeys(
            clean_text(value) for value in (params.get("current_stock_manufacturer_codes") or []) if clean_text(value)
        ))
        product_codes = list(dict.fromkeys(
            clean_text(value) for value in (params.get("current_stock_product_codes") or []) if clean_text(value)
        ))
        maker_where: list[str] = []
        product_where: list[str] = []
        if manufacturer_codes and params.get("current_stock_maker_filter_mode") == "code_in":
            entity_params: dict[str, Any] = {}
            _append_in_clause(maker_where, entity_params, "P.Rd04_Ven_Cd", manufacturer_codes, "current_stock_maker")
            sql_params.update(entity_params)
        elif clean_text(params.get("maker_nm")):
            sql_params["current_stock_maker_like"] = f"%{clean_text(params.get('maker_nm'))}%"
            maker_where.append("MakerVen.Rd03_Ven_Nm LIKE %(current_stock_maker_like)s")
        if product_codes and params.get("current_stock_product_filter_mode") == "code_in":
            entity_params = {}
            _append_in_clause(product_where, entity_params, "P.Rd04_Physic_Cd", product_codes, "current_stock_product")
            sql_params.update(entity_params)
        elif clean_text(params.get("physic_nm")):
            sql_params["current_stock_product_like"] = f"%{clean_text(params.get('physic_nm'))}%"
            product_where.append("P.Rd04_Physic_Nm LIKE %(current_stock_product_like)s")

        if current_stock_scope == "manufacturer_and_product":
            where.extend(maker_where + product_where)
        elif current_stock_scope == "manufacturer":
            where.extend(maker_where)
        elif current_stock_scope == "product":
            where.extend(product_where)
        elif maker_where or product_where:
            where.append("(" + " OR ".join(maker_where + product_where) + ")")
        elif clean_text(params.get("current_stock_entity_phrase")):
            sql_params["current_stock_entity_like"] = f"%{clean_text(params.get('current_stock_entity_phrase'))}%"
            where.append(
                "(MakerVen.Rd03_Ven_Nm LIKE %(current_stock_entity_like)s "
                "OR P.Rd04_Physic_Nm LIKE %(current_stock_entity_like)s)"
            )

    # 제품재고장의 거래처 라벨은 제품 마스터에 연결된 매입처/발주처
    # 이름만 대상으로 한다. 제품·제조사 조건과 섞지 않는다.
    if clean_text(params.get("ven_nm")):
        sql_params["ven_nm_like"] = f"%{clean_text(params.get('ven_nm'))}%"
        where.append(
            "(BuyVen.Rd03_Ven_Nm LIKE %(ven_nm_like)s "
            "OR OrderVen.Rd03_Ven_Nm LIKE %(ven_nm_like)s)"
        )

    if clean_text(params.get("maker_cd")):
        where.append("P.Rd04_Ven_Cd = %(maker_cd)s")
    if clean_text(params.get("maker_nm")) and not current_stock_scoped:
        sql_params["maker_nm_like"] = f"%{clean_text(params.get('maker_nm'))}%"
        where.append("MakerVen.Rd03_Ven_Nm LIKE %(maker_nm_like)s")

    if clean_text(params.get("order_cd")):
        where.append("P.Rd04_OrVen_Cd = %(order_cd)s")
    if clean_text(params.get("order_nm")):
        sql_params["order_nm_like"] = f"%{clean_text(params.get('order_nm'))}%"
        where.append("OrderVen.Rd03_Ven_Nm LIKE %(order_nm_like)s")

    if clean_text(params.get("buy_cd")):
        where.append(f"{buy_field_expr} = %(buy_cd)s")
    if clean_text(params.get("buy_nm")):
        sql_params["buy_nm_like"] = f"%{clean_text(params.get('buy_nm'))}%"
        where.append("BuyVen.Rd03_Ven_Nm LIKE %(buy_nm_like)s")

    add_unlabeled_name_like_filter(
        where,
        sql_params,
        vendor_name_exprs=("BuyVen.Rd03_Ven_Nm", "OrderVen.Rd03_Ven_Nm"),
        product_name_expr="P.Rd04_Physic_Nm",
        manufacturer_name_expr="MakerVen.Rd03_Ven_Nm",
    )

    if clean_text(params.get("product_group_nm")) and clean_text(params.get("product_group_nm")) != "전체":
        sql_params["product_group_nm_like"] = f"%{clean_text(params.get('product_group_nm'))}%"
        where.append("PG.Rd01_Hnm LIKE %(product_group_nm_like)s")

    if clean_text(params.get("product_di_nm")) and clean_text(params.get("product_di_nm")) != "전체":
        sql_params["product_di_nm_like"] = f"%{clean_text(params.get('product_di_nm'))}%"
        where.append("PD.Rd01_Hnm LIKE %(product_di_nm_like)s")

    if clean_text(params.get("product_class_nm")) and clean_text(params.get("product_class_nm")) != "전체":
        sql_params["product_class_nm_like"] = f"%{clean_text(params.get('product_class_nm'))}%"
        where.append("PF.Rd01_Hnm LIKE %(product_class_nm_like)s")

# -----------------------------------------------------------------------------
# SQL: 월집계 이월
# -----------------------------------------------------------------------------
def _build_month_carry_baseline_sql(params: Dict[str, Any], cfg: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    sql_params = dict(params)
    stock_codes = _normalize_stock_codes(params)

    month_table = cfg["month_table"]
    stock_field = f"M.{cfg['month_alias']}_Stock_Cd"
    group_cd_expr, group_nm_expr = _vendor_group_expr(
        cfg["group_basis"], f"M.{cfg['month_alias']}_Ven_Cd", stock_field
    )

    where = [
        "1 = 1",
        f"M.{cfg['month_alias']}_Stock_YyMm < %(base_month)s",
        f"NULLIF(LTRIM(RTRIM(M.{cfg['month_alias']}_Stock_YyMm)), '') IS NOT NULL",
        "ISNULL(P.Rd04_Del_Flag, '') <> 'E'",
    ]
    _append_in_clause(where, sql_params, stock_field, stock_codes, "carry_stock")
    unlabeled_name = clean_text(params.get("nlq_unlabeled_name"))
    master_params = dict(params)
    if unlabeled_name:
        # Preserve the shared four-role OR-LIKE contract and add equivalent
        # semi-joins as a redundant optimizer scope for the month aggregate.
        master_params["nlq_unlabeled_name"] = ""
    _apply_master_filters(where, sql_params, master_params, f"M.{cfg['month_alias']}_Ven_Cd")
    if unlabeled_name:
        sql_params["nlq_unlabeled_name_like"] = f"%{unlabeled_name}%"
        where.append(
            "(P.Rd04_Physic_Nm LIKE %(nlq_unlabeled_name_like)s "
            "OR EXISTS (SELECT 1 FROM dbo.Rddbc030 AS BuyFilter "
            f"WHERE BuyFilter.Rd03_Ven_Cd = M.{cfg['month_alias']}_Ven_Cd "
            "AND BuyFilter.Rd03_Ven_Nm LIKE %(nlq_unlabeled_name_like)s) "
            "OR EXISTS (SELECT 1 FROM dbo.Rddbc030 AS OrderFilter "
            "WHERE OrderFilter.Rd03_Ven_Cd = P.Rd04_OrVen_Cd "
            "AND OrderFilter.Rd03_Ven_Nm LIKE %(nlq_unlabeled_name_like)s) "
            "OR EXISTS (SELECT 1 FROM dbo.Rddbc030 AS MakerFilter "
            "WHERE MakerFilter.Rd03_Ven_Cd = P.Rd04_Ven_Cd "
            "AND MakerFilter.Rd03_Ven_Nm LIKE %(nlq_unlabeled_name_like)s))"
        )

    sql = f"""
SELECT
    {_common_descriptor_sql(group_cd_expr, group_nm_expr)},
    CAST(SUM({cfg['month_in_qty_expr']}) AS decimal(18, 4)) AS old_in_qty,
    CAST(SUM({cfg['month_in_amt_expr']}) AS decimal(18, 4)) AS old_in_amt,
    CAST(SUM({cfg['month_out_qty_expr']}) AS decimal(18, 4)) AS old_out_qty,
    CAST(0 AS decimal(18, 4)) AS now_in_qty,
    CAST(0 AS decimal(18, 4)) AS now_in_amt,
    CAST(0 AS decimal(18, 4)) AS now_out_qty,
    CAST(0 AS decimal(18, 4)) AS now_out_amt
FROM {month_table} AS M
LEFT JOIN dbo.Rddbc040 AS P
       ON M.{cfg['month_alias']}_Physic_Cd = P.Rd04_Physic_Cd

LEFT JOIN dbo.Rddbc010 AS PG
       ON P.Rd04_Physic_Group_Gcode = PG.Rd01_Gcode
      AND P.Rd04_Physic_Group = PG.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS PD
       ON P.Rd04_Physic_Di_Gcode = PD.Rd01_Gcode
      AND P.Rd04_Physic_Di = PD.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS PF
       ON P.Rd04_Physic_Flag_Gcode = PF.Rd01_Gcode
      AND P.Rd04_Physic_Flag = PF.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS PhysicGu
       ON P.Rd04_Physic_Gu_Gcode = PhysicGu.Rd01_Gcode
      AND P.Rd04_Physic_Gu = PhysicGu.Rd01_Tcode
      

LEFT JOIN dbo.Rddbc046 AS StdCd
       ON P.Rd04_Physic_Cd = StdCd.Rd046_Physic_Cd
LEFT JOIN dbo.Rddbc030 AS BuyVen
       ON M.{cfg['month_alias']}_Ven_Cd = BuyVen.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS MakerVen
       ON P.Rd04_Ven_Cd = MakerVen.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS OrderVen
       ON P.Rd04_OrVen_Cd = OrderVen.Rd03_Ven_Cd
WHERE {" AND ".join(where)}
GROUP BY
    {group_cd_expr},
    {group_nm_expr},
    BuyVen.Rd03_Ven_Cd, BuyVen.Rd03_Ven_Nm,
    OrderVen.Rd03_Ven_Cd, OrderVen.Rd03_Ven_Nm,
    MakerVen.Rd03_Ven_Cd, MakerVen.Rd03_Ven_Nm,
    P.Rd04_Physic_Cd, P.Rd04_Physic_Nm, P.Rd04_Standard,
    P.Rd04_Insu_Cd, P.Rd04_Old_Insu_Cd, StdCd.Rd046_Standard_Cd,
    P.Rd04_Pack_Unit, P.Rd04_In_Unit_Cost,
    P.Rd04_Insu_Date, P.Rd04_Before_Insu_Date, P.Rd04_Insu_Price,
    P.Rd04_Before_Insu_Price, P.Rd04_Acc_Unit, P.Rd04_Physic_Tax,
    PG.Rd01_Hnm, PD.Rd01_Hnm, PF.Rd01_Hnm,
    PhysicGu.Rd01_Hnm 
HAVING
    SUM({cfg['month_in_qty_expr']}) <> 0
    OR SUM({cfg['month_in_amt_expr']}) <> 0
    OR SUM({cfg['month_out_qty_expr']}) <> 0
"""
    return sql, sql_params


def _month_carry_has_master_filter(params: Dict[str, Any]) -> bool:
    """Keep the established master-filter SQL when its aliases are required."""
    text_filters = (
        "physic_cd", "physic_nm", "ven_nm", "maker_cd", "maker_nm",
        "order_cd", "order_nm", "buy_cd", "buy_nm", "product_group_nm",
        "product_di_nm", "product_class_nm", "nlq_unlabeled_name",
        "current_stock_entity_scope", "current_stock_entity_phrase",
    )
    if any(clean_text(params.get(key)) not in {"", "전체"} for key in text_filters):
        return True
    if bool(params.get("_product_inventory_explicit_product_scope_applied")):
        return True
    return bool(
        params.get("current_stock_manufacturer_codes")
        or params.get("current_stock_product_codes")
    )


def _build_month_carry_monthagg_sql(params: Dict[str, Any], cfg: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    """Aggregate monthly movements before expanding display-only master rows."""
    sql_params = dict(params)
    stock_codes = _normalize_stock_codes(params)
    month_table = cfg["month_table"]
    month_alias = cfg["month_alias"]
    stock_field = f"M.{month_alias}_Stock_Cd"
    group_cd_expr, group_nm_expr = _vendor_group_expr(
        cfg["group_basis"], "A.month_ven_cd", "A.month_stock_cd"
    )
    where = [
        f"M.{month_alias}_Stock_YyMm < %(base_month)s",
        f"NULLIF(LTRIM(RTRIM(M.{month_alias}_Stock_YyMm)), '') IS NOT NULL",
        "ISNULL(PFilter.Rd04_Del_Flag, '') <> 'E'",
    ]
    _append_in_clause(where, sql_params, stock_field, stock_codes, "carry_stock")

    sql = f"""
WITH MonthAgg AS (
    SELECT
        M.{month_alias}_Physic_Cd AS month_physic_cd,
        M.{month_alias}_Stock_Cd AS month_stock_cd,
        M.{month_alias}_Ven_Cd AS month_ven_cd,
        SUM({cfg['month_in_qty_expr']}) AS old_in_qty,
        SUM({cfg['month_in_amt_expr']}) AS old_in_amt,
        SUM({cfg['month_out_qty_expr']}) AS old_out_qty
    FROM {month_table} AS M
    LEFT JOIN dbo.Rddbc040 AS PFilter
           ON M.{month_alias}_Physic_Cd = PFilter.Rd04_Physic_Cd
    WHERE {' AND '.join(where)}
    GROUP BY
        M.{month_alias}_Physic_Cd,
        M.{month_alias}_Stock_Cd,
        M.{month_alias}_Ven_Cd
    HAVING
        SUM({cfg['month_in_qty_expr']}) <> 0
        OR SUM({cfg['month_in_amt_expr']}) <> 0
        OR SUM({cfg['month_out_qty_expr']}) <> 0
)
SELECT
    {_common_descriptor_sql(group_cd_expr, group_nm_expr)},
    CAST(SUM(A.old_in_qty) AS decimal(18, 4)) AS old_in_qty,
    CAST(SUM(A.old_in_amt) AS decimal(18, 4)) AS old_in_amt,
    CAST(SUM(A.old_out_qty) AS decimal(18, 4)) AS old_out_qty,
    CAST(0 AS decimal(18, 4)) AS now_in_qty,
    CAST(0 AS decimal(18, 4)) AS now_in_amt,
    CAST(0 AS decimal(18, 4)) AS now_out_qty,
    CAST(0 AS decimal(18, 4)) AS now_out_amt
FROM MonthAgg AS A
LEFT JOIN dbo.Rddbc040 AS P
       ON A.month_physic_cd = P.Rd04_Physic_Cd
LEFT JOIN dbo.Rddbc010 AS PG
       ON P.Rd04_Physic_Group_Gcode = PG.Rd01_Gcode
      AND P.Rd04_Physic_Group = PG.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS PD
       ON P.Rd04_Physic_Di_Gcode = PD.Rd01_Gcode
      AND P.Rd04_Physic_Di = PD.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS PF
       ON P.Rd04_Physic_Flag_Gcode = PF.Rd01_Gcode
      AND P.Rd04_Physic_Flag = PF.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS PhysicGu
       ON P.Rd04_Physic_Gu_Gcode = PhysicGu.Rd01_Gcode
      AND P.Rd04_Physic_Gu = PhysicGu.Rd01_Tcode
LEFT JOIN dbo.Rddbc046 AS StdCd
       ON P.Rd04_Physic_Cd = StdCd.Rd046_Physic_Cd
LEFT JOIN dbo.Rddbc030 AS BuyVen
       ON A.month_ven_cd = BuyVen.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS MakerVen
       ON P.Rd04_Ven_Cd = MakerVen.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS OrderVen
       ON P.Rd04_OrVen_Cd = OrderVen.Rd03_Ven_Cd
GROUP BY
    {group_cd_expr},
    {group_nm_expr},
    BuyVen.Rd03_Ven_Cd, BuyVen.Rd03_Ven_Nm,
    OrderVen.Rd03_Ven_Cd, OrderVen.Rd03_Ven_Nm,
    MakerVen.Rd03_Ven_Cd, MakerVen.Rd03_Ven_Nm,
    P.Rd04_Physic_Cd, P.Rd04_Physic_Nm, P.Rd04_Standard,
    P.Rd04_Insu_Cd, P.Rd04_Old_Insu_Cd, StdCd.Rd046_Standard_Cd,
    P.Rd04_Pack_Unit, P.Rd04_In_Unit_Cost,
    P.Rd04_Insu_Date, P.Rd04_Before_Insu_Date, P.Rd04_Insu_Price,
    P.Rd04_Before_Insu_Price, P.Rd04_Acc_Unit, P.Rd04_Physic_Tax,
    PG.Rd01_Hnm, PD.Rd01_Hnm, PF.Rd01_Hnm, PhysicGu.Rd01_Hnm
HAVING
    SUM(A.old_in_qty) <> 0
    OR SUM(A.old_in_amt) <> 0
    OR SUM(A.old_out_qty) <> 0
"""
    return sql, sql_params


def _build_month_carry_sql(params: Dict[str, Any], cfg: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    """Build month carry without changing established master-filter semantics."""
    if _month_carry_has_master_filter(params):
        return _build_month_carry_baseline_sql(params, cfg)
    return _build_month_carry_monthagg_sql(params, cfg)


def _build_month_period_sql(params: Dict[str, Any], cfg: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    """Build the selected whole-month ledger branch without detail tables."""
    sql_params = dict(params)
    stock_codes = _normalize_stock_codes(params)
    month_table = cfg["month_table"]
    month_alias = cfg["month_alias"]
    stock_field = f"M.{month_alias}_Stock_Cd"
    group_cd_expr, group_nm_expr = _vendor_group_expr(
        cfg["group_basis"], f"M.{month_alias}_Ven_Cd", stock_field
    )
    month_from = clean_text(params.get("month_from") or params.get("date_from"))[:6]
    month_to = clean_text(params.get("month_to") or params.get("date_to"))[:6]
    if not month_from or not month_to:
        raise ValueError("월집계 조회의 기준월이 없습니다.")
    sql_params["month_from"] = month_from
    sql_params["month_to"] = month_to

    where = [
        "1 = 1",
        f"LEFT(M.{month_alias}_Stock_YyMm, 6) >= %(month_from)s",
        f"LEFT(M.{month_alias}_Stock_YyMm, 6) <= %(month_to)s",
        f"NULLIF(LTRIM(RTRIM(M.{month_alias}_Stock_YyMm)), '') IS NOT NULL",
        "ISNULL(P.Rd04_Del_Flag, '') <> 'E'",
    ]
    _append_in_clause(where, sql_params, stock_field, stock_codes, "period_stock")
    _apply_master_filters(where, sql_params, params, f"M.{month_alias}_Ven_Cd")

    sql = f"""
SELECT
    {_common_descriptor_sql(group_cd_expr, group_nm_expr)},
    CAST(0 AS decimal(18, 4)) AS old_in_qty,
    CAST(0 AS decimal(18, 4)) AS old_in_amt,
    CAST(0 AS decimal(18, 4)) AS old_out_qty,
    CAST(SUM({cfg['month_in_qty_expr']}) AS decimal(18, 4)) AS now_in_qty,
    CAST(SUM({cfg['month_in_amt_expr']}) AS decimal(18, 4)) AS now_in_amt,
    CAST(SUM({cfg['month_out_qty_expr']}) AS decimal(18, 4)) AS now_out_qty,
    CAST(SUM({cfg['month_out_amt_expr']}) AS decimal(18, 4)) AS now_out_amt
FROM {month_table} AS M
LEFT JOIN dbo.Rddbc040 AS P
       ON M.{month_alias}_Physic_Cd = P.Rd04_Physic_Cd

LEFT JOIN dbo.Rddbc010 AS PG
       ON P.Rd04_Physic_Group_Gcode = PG.Rd01_Gcode
      AND P.Rd04_Physic_Group = PG.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS PD
       ON P.Rd04_Physic_Di_Gcode = PD.Rd01_Gcode
      AND P.Rd04_Physic_Di = PD.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS PF
       ON P.Rd04_Physic_Flag_Gcode = PF.Rd01_Gcode
      AND P.Rd04_Physic_Flag = PF.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS PhysicGu
       ON P.Rd04_Physic_Gu_Gcode = PhysicGu.Rd01_Gcode
      AND P.Rd04_Physic_Gu = PhysicGu.Rd01_Tcode
LEFT JOIN dbo.Rddbc046 AS StdCd
       ON P.Rd04_Physic_Cd = StdCd.Rd046_Physic_Cd
LEFT JOIN dbo.Rddbc030 AS BuyVen
       ON M.{month_alias}_Ven_Cd = BuyVen.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS MakerVen
       ON P.Rd04_Ven_Cd = MakerVen.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS OrderVen
       ON P.Rd04_OrVen_Cd = OrderVen.Rd03_Ven_Cd
WHERE {" AND ".join(where)}
GROUP BY
    {group_cd_expr},
    {group_nm_expr},
    BuyVen.Rd03_Ven_Cd, BuyVen.Rd03_Ven_Nm,
    OrderVen.Rd03_Ven_Cd, OrderVen.Rd03_Ven_Nm,
    MakerVen.Rd03_Ven_Cd, MakerVen.Rd03_Ven_Nm,
    P.Rd04_Physic_Cd, P.Rd04_Physic_Nm, P.Rd04_Standard,
    P.Rd04_Insu_Cd, P.Rd04_Old_Insu_Cd, StdCd.Rd046_Standard_Cd,
    P.Rd04_Pack_Unit, P.Rd04_In_Unit_Cost,
    P.Rd04_Insu_Date, P.Rd04_Before_Insu_Date, P.Rd04_Insu_Price,
    P.Rd04_Before_Insu_Price, P.Rd04_Acc_Unit, P.Rd04_Physic_Tax,
    PG.Rd01_Hnm, PD.Rd01_Hnm, PF.Rd01_Hnm, PhysicGu.Rd01_Hnm
HAVING
    SUM({cfg['month_in_qty_expr']}) <> 0
    OR SUM({cfg['month_in_amt_expr']}) <> 0
    OR SUM({cfg['month_out_qty_expr']}) <> 0
    OR SUM({cfg['month_out_amt_expr']}) <> 0
"""
    return sql, sql_params


# -----------------------------------------------------------------------------
# SQL: 상세(부분 이월 / 기간 입출고)
# -----------------------------------------------------------------------------
def _build_detail_sql(direction: str, params: Dict[str, Any], cfg: Dict[str, Any], date_from: str, date_to: str, bucket: str) -> tuple[str, Dict[str, Any]]:
    """
    bucket:
      - carry : 당월 1일 ~ 시작일 전일
      - period: 시작일 ~ 종료일
    """
    sql_params = dict(params)
    stock_codes = _normalize_stock_codes(params)

    if direction == "in":
        table = "dbo.Rddbc110"
        date_field = cfg["in_date_field"]
        qty_expr = cfg["in_qty_expr"]
        amount_expr = "ISNULL(T.Rd11_Supply_Price, 0) + ISNULL(T.Rd11_Tax_Price, 0)"
        stock_field = "T.Rd11_Stock_Cd"
        buy_field = "T.Rd11_Ven_Cd"
        seq_field = "T.Rd11_In_Seq"
        io_field = "T.Rd11_Io_Gu"
        in_old = "SUM_QTY"
        where = [
            "1 = 1",
            f"NULLIF(LTRIM(RTRIM({date_field})), '') IS NOT NULL",
            _prefix_not_in(io_field, cfg["in_exclude_prefix"]),
            "ISNULL(P.Rd04_Del_Flag, '') <> 'E'",
        ]
    else:
        table = "dbo.Rddbc120"
        date_field = cfg["out_date_field"]
        qty_expr = cfg["out_qty_expr"]
        amount_expr = "ISNULL(T.Rd12_Supply_Price, 0) + ISNULL(T.Rd12_Tax_Price, 0)"
        stock_field = "T.Rd12_Stock_Cd"
        buy_field = "T.Rd12_In_Ven_Cd"
        seq_field = "T.Rd12_Out_Seq"
        io_field = "T.Rd12_Io_Gu"
        in_old = "SUM_QTY"
        where = [
            "1 = 1",
            f"NULLIF(LTRIM(RTRIM({date_field})), '') IS NOT NULL",
            _prefix_not_in(io_field, cfg["out_exclude_prefix"]),
            "ISNULL(P.Rd04_Del_Flag, '') <> 'E'",
        ]

    sql_params["bucket_from"] = date_from
    sql_params["bucket_to"] = date_to
    where.append(f"{date_field} >= %(bucket_from)s")
    where.append(f"{date_field} <= %(bucket_to)s")

    _append_in_clause(where, sql_params, stock_field, stock_codes, f"{direction}_{bucket}_stock")
    _apply_master_filters(where, sql_params, params, buy_field)

    group_cd_expr, group_nm_expr = _vendor_group_expr(cfg["group_basis"], buy_field, stock_field)

    if bucket == "carry":
        old_in_qty_sql = f"CAST(SUM({qty_expr}) AS decimal(18, 4))" if direction == "in" else "CAST(0 AS decimal(18, 4))"
        old_in_amt_sql = f"CAST(SUM({amount_expr}) AS decimal(18, 4))" if direction == "in" else "CAST(0 AS decimal(18, 4))"
        old_out_qty_sql = f"CAST(SUM({qty_expr}) AS decimal(18, 4))" if direction == "out" else "CAST(0 AS decimal(18, 4))"
        now_in_qty_sql = "CAST(0 AS decimal(18, 4))"
        now_in_amt_sql = "CAST(0 AS decimal(18, 4))"
        now_out_qty_sql = "CAST(0 AS decimal(18, 4))"
        now_out_amt_sql = "CAST(0 AS decimal(18, 4))"
    else:
        old_in_qty_sql = "CAST(0 AS decimal(18, 4))"
        old_in_amt_sql = "CAST(0 AS decimal(18, 4))"
        old_out_qty_sql = "CAST(0 AS decimal(18, 4))"
        now_in_qty_sql = f"CAST(SUM({qty_expr}) AS decimal(18, 4))" if direction == "in" else "CAST(0 AS decimal(18, 4))"
        now_in_amt_sql = f"CAST(SUM({amount_expr}) AS decimal(18, 4))" if direction == "in" else "CAST(0 AS decimal(18, 4))"
        now_out_qty_sql = f"CAST(SUM({qty_expr}) AS decimal(18, 4))" if direction == "out" else "CAST(0 AS decimal(18, 4))"
        now_out_amt_sql = f"CAST(SUM({amount_expr}) AS decimal(18, 4))" if direction == "out" else "CAST(0 AS decimal(18, 4))"

    sql = f"""
SELECT
    {_common_descriptor_sql(group_cd_expr, group_nm_expr)},
    {old_in_qty_sql} AS old_in_qty,
    {old_in_amt_sql} AS old_in_amt,
    {old_out_qty_sql} AS old_out_qty,
    {now_in_qty_sql} AS now_in_qty,
    {now_in_amt_sql} AS now_in_amt,
    {now_out_qty_sql} AS now_out_qty,
    {now_out_amt_sql} AS now_out_amt
FROM {table} AS T
LEFT JOIN dbo.Rddbc040 AS P
       ON {"T.Rd11_Physic_Cd = P.Rd04_Physic_Cd" if direction == "in" else "T.Rd12_Physic_Cd = P.Rd04_Physic_Cd"}

LEFT JOIN dbo.Rddbc010 AS PG
       ON P.Rd04_Physic_Group_Gcode = PG.Rd01_Gcode
      AND P.Rd04_Physic_Group = PG.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS PD
       ON P.Rd04_Physic_Di_Gcode = PD.Rd01_Gcode
      AND P.Rd04_Physic_Di = PD.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS PF
       ON P.Rd04_Physic_Flag_Gcode = PF.Rd01_Gcode
      AND P.Rd04_Physic_Flag = PF.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS PhysicGu
       ON P.Rd04_Physic_Gu_Gcode = PhysicGu.Rd01_Gcode
      AND P.Rd04_Physic_Gu = PhysicGu.Rd01_Tcode

LEFT JOIN dbo.Rddbc046 AS StdCd
       ON P.Rd04_Physic_Cd = StdCd.Rd046_Physic_Cd
LEFT JOIN dbo.Rddbc030 AS BuyVen
       ON {buy_field} = BuyVen.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS MakerVen
       ON P.Rd04_Ven_Cd = MakerVen.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc030 AS OrderVen
       ON P.Rd04_OrVen_Cd = OrderVen.Rd03_Ven_Cd
WHERE {" AND ".join(where)}
GROUP BY
    {group_cd_expr},
    {group_nm_expr},
    BuyVen.Rd03_Ven_Cd, BuyVen.Rd03_Ven_Nm,
    OrderVen.Rd03_Ven_Cd, OrderVen.Rd03_Ven_Nm,
    MakerVen.Rd03_Ven_Cd, MakerVen.Rd03_Ven_Nm,
    P.Rd04_Physic_Cd, P.Rd04_Physic_Nm, P.Rd04_Standard,
    P.Rd04_Insu_Cd, P.Rd04_Old_Insu_Cd, StdCd.Rd046_Standard_Cd,
    P.Rd04_Pack_Unit, P.Rd04_In_Unit_Cost,
    P.Rd04_Insu_Date, P.Rd04_Before_Insu_Date, P.Rd04_Insu_Price,
    P.Rd04_Before_Insu_Price, P.Rd04_Acc_Unit, P.Rd04_Physic_Tax,
    PG.Rd01_Hnm, PD.Rd01_Hnm, PF.Rd01_Hnm,
    PhysicGu.Rd01_Hnm
HAVING
    SUM({qty_expr}) <> 0
    OR SUM({amount_expr}) <> 0
"""
    return sql, sql_params



def _build_last_cost_sql(
    params: Dict[str, Any],
    cfg: Dict[str, Any],
    *,
    product_scope: Optional[list[str]] = None,
) -> tuple[str, Dict[str, Any]]:
    """
    문서 기준:
    - 최종매입가 = 재고위치와 무관한 제품 기준 최종 매입단가
    - 매입이 전혀 없는 제품은 최종출고건의 입고/출고단가를 fallback 으로 사용 가능
      (현재는 IN_UNIT 우선 fallback)
    """
    sql_params = dict(params)

    in_where = [
        "1 = 1",
        "NULLIF(LTRIM(RTRIM(T.Rd11_In_YyMmDd)), '') IS NOT NULL",
        "T.Rd11_In_YyMmDd <= %(date_to)s",
        "LEFT(LTRIM(RTRIM(T.Rd11_Io_Gu)), 1) <> '3'",      # 미결입고 제외
        "LTRIM(RTRIM(T.Rd11_Io_Gu)) NOT IN ('402', '404')",  # 이동매입/이동매입정리 제외
        "ISNULL(P.Rd04_Del_Flag, '') <> 'E'",
    ]
    _apply_master_filters(in_where, sql_params, params, "T.Rd11_Ven_Cd")
    _append_in_clause(
        in_where,
        sql_params,
        "T.Rd11_Physic_Cd",
        list(product_scope or []),
        "last_cost_product",
    )

    out_where = [
        "1 = 1",
        "NULLIF(LTRIM(RTRIM(T.Rd12_Out_YyMmDd)), '') IS NOT NULL",
        "T.Rd12_Out_YyMmDd <= %(date_to)s",
        "LEFT(LTRIM(RTRIM(T.Rd12_Io_Gu)), 1) <> '8'",      # 미결출고 제외
        "LTRIM(RTRIM(T.Rd12_Io_Gu)) NOT IN ('902', '904')",  # 이동매출/이동매출정리 제외
        "ISNULL(P.Rd04_Del_Flag, '') <> 'E'",
    ]
    _apply_master_filters(out_where, sql_params, params, "T.Rd12_In_Ven_Cd")
    _append_in_clause(
        out_where,
        sql_params,
        "T.Rd12_Physic_Cd",
        list(product_scope or []),
        "last_cost_product",
    )

    sql = f"""
WITH Base AS (
    -- 1순위: 최종 매입단가
    SELECT
        LTRIM(RTRIM(T.Rd11_Physic_Cd)) AS physic_cd,
        CAST(ISNULL(T.Rd11_Unit_Cost, 0) AS decimal(18,4)) AS unit_cost,
        T.Rd11_In_YyMmDd AS ymd,
        T.Rd11_In_Seq AS seq_no,
        0 AS src_priority
    FROM dbo.Rddbc110 AS T
    LEFT JOIN dbo.Rddbc040 AS P
           ON T.Rd11_Physic_Cd = P.Rd04_Physic_Cd
    LEFT JOIN dbo.Rddbc010 AS PG
        ON P.Rd04_Physic_Group_Gcode = PG.Rd01_Gcode
        AND P.Rd04_Physic_Group = PG.Rd01_Tcode
    LEFT JOIN dbo.Rddbc010 AS PD
        ON P.Rd04_Physic_Di_Gcode = PD.Rd01_Gcode
        AND P.Rd04_Physic_Di = PD.Rd01_Tcode
    LEFT JOIN dbo.Rddbc010 AS PF
        ON P.Rd04_Physic_Flag_Gcode = PF.Rd01_Gcode
        AND P.Rd04_Physic_Flag = PF.Rd01_Tcode

    LEFT JOIN dbo.Rddbc030 AS BuyVen
           ON T.Rd11_Ven_Cd = BuyVen.Rd03_Ven_Cd
    LEFT JOIN dbo.Rddbc030 AS MakerVen
           ON P.Rd04_Ven_Cd = MakerVen.Rd03_Ven_Cd
    LEFT JOIN dbo.Rddbc030 AS OrderVen
           ON P.Rd04_OrVen_Cd = OrderVen.Rd03_Ven_Cd
    WHERE {" AND ".join(in_where)}

    UNION ALL

    -- 2순위 fallback: 매입이 없는 제품은 최종 출고건의 입고단가 우선 사용
    SELECT
        LTRIM(RTRIM(T.Rd12_Physic_Cd)) AS physic_cd,
        CAST(
            CASE
                WHEN ISNULL(T.Rd12_In_Unit_Cost, 0) <> 0 THEN T.Rd12_In_Unit_Cost
                ELSE ISNULL(T.Rd12_Unit_Cost, 0)
            END
            AS decimal(18,4)
        ) AS unit_cost,
        T.Rd12_Out_YyMmDd AS ymd,
        T.Rd12_Out_Seq AS seq_no,
        1 AS src_priority
    FROM dbo.Rddbc120 AS T
    LEFT JOIN dbo.Rddbc040 AS P
           ON T.Rd12_Physic_Cd = P.Rd04_Physic_Cd
    LEFT JOIN dbo.Rddbc010 AS PG
        ON P.Rd04_Physic_Group_Gcode = PG.Rd01_Gcode
        AND P.Rd04_Physic_Group = PG.Rd01_Tcode
    LEFT JOIN dbo.Rddbc010 AS PD
        ON P.Rd04_Physic_Di_Gcode = PD.Rd01_Gcode
        AND P.Rd04_Physic_Di = PD.Rd01_Tcode
    LEFT JOIN dbo.Rddbc010 AS PF
        ON P.Rd04_Physic_Flag_Gcode = PF.Rd01_Gcode
        AND P.Rd04_Physic_Flag = PF.Rd01_Tcode
    LEFT JOIN dbo.Rddbc030 AS BuyVen
           ON T.Rd12_In_Ven_Cd = BuyVen.Rd03_Ven_Cd
    LEFT JOIN dbo.Rddbc030 AS MakerVen
           ON P.Rd04_Ven_Cd = MakerVen.Rd03_Ven_Cd
    LEFT JOIN dbo.Rddbc030 AS OrderVen
           ON P.Rd04_OrVen_Cd = OrderVen.Rd03_Ven_Cd
    WHERE {" AND ".join(out_where)}
),
Ranked AS (
    SELECT
        physic_cd,
        unit_cost,
        ROW_NUMBER() OVER (
            PARTITION BY physic_cd
            ORDER BY src_priority ASC, ymd DESC, seq_no DESC
        ) AS rn
    FROM Base
    WHERE unit_cost <> 0
)
SELECT
    physic_cd,
    unit_cost AS last_unit_cost
FROM Ranked
WHERE rn = 1
"""
    return sql, sql_params

# -----------------------------------------------------------------------------
# SQL: 최종매입가
# -----------------------------------------------------------------------------
def _build_last_in_unit_sql(params: Dict[str, Any], cfg: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    sql_params = dict(params)
    stock_codes = _normalize_stock_codes(params)

    where = [
        "1 = 1",
        "NULLIF(LTRIM(RTRIM(T.Rd11_In_YyMmDd)), '') IS NOT NULL",
        "T.Rd11_In_YyMmDd <= %(date_to)s",
        _prefix_not_in("T.Rd11_Io_Gu", cfg["in_exclude_prefix"]),
        "ISNULL(P.Rd04_Del_Flag, '') <> 'E'",
    ]
    _append_in_clause(where, sql_params, "T.Rd11_Stock_Cd", stock_codes, "last_stock")
    _apply_master_filters(where, sql_params, params, "T.Rd11_Ven_Cd")

    group_cd_expr, group_nm_expr = _vendor_group_expr(
        cfg["group_basis"], "T.Rd11_Ven_Cd", "T.Rd11_Stock_Cd"
    )

    sql = f"""
WITH X AS (
    SELECT
        LTRIM(RTRIM({group_cd_expr})) AS group_cd,
        LTRIM(RTRIM(ISNULL(P.Rd04_Physic_Cd, ''))) AS physic_cd,
        CAST(ISNULL(T.Rd11_Unit_Cost, 0) AS decimal(18, 4)) AS last_in_unit,
        ROW_NUMBER() OVER (
            PARTITION BY LTRIM(RTRIM({group_cd_expr})), LTRIM(RTRIM(ISNULL(P.Rd04_Physic_Cd, '')))
            ORDER BY T.Rd11_In_YyMmDd DESC, T.Rd11_In_Seq DESC
        ) AS rn
    FROM dbo.Rddbc110 AS T
    LEFT JOIN dbo.Rddbc040 AS P
           ON T.Rd11_Physic_Cd = P.Rd04_Physic_Cd
    LEFT JOIN dbo.Rddbc030 AS BuyVen
           ON T.Rd11_Ven_Cd = BuyVen.Rd03_Ven_Cd
    LEFT JOIN dbo.Rddbc030 AS MakerVen
           ON P.Rd04_Ven_Cd = MakerVen.Rd03_Ven_Cd
    LEFT JOIN dbo.Rddbc030 AS OrderVen
           ON P.Rd04_OrVen_Cd = OrderVen.Rd03_Ven_Cd
    WHERE {" AND ".join(where)}
)
SELECT group_cd, physic_cd, last_in_unit
FROM X
WHERE rn = 1
"""
    return sql, sql_params


# -----------------------------------------------------------------------------
# 쿼리 실행
# -----------------------------------------------------------------------------
def _query_df_safe(sql: str, params: Dict[str, Any]) -> pd.DataFrame:
    try:
        df = query_to_df(sql, params)
        if isinstance(df, pd.DataFrame):
            return df
        return pd.DataFrame()
    except Exception as e:
        raise RuntimeError(str(e)) from e


def _collect_source_df(params: Dict[str, Any], cfg: Dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    date_from = params["date_from"]
    date_to = params["date_to"]
    base_month = date_from[:6]

    sql_params = dict(params)
    sql_params["base_month"] = base_month

    frames: list[pd.DataFrame] = []
    current_stock_query = bool(cfg.get("current_stock_query"))
    query_perf: dict[str, dict[str, Any]] = {}
    source_path = resolve_product_inventory_source_path(params)
    params["source_path"] = source_path
    predicate_mode = (
        clean_text(params.get("current_stock_predicate_mode"))
        if current_stock_query
        else _inventory_predicate_mode(params)
    ) or "standard"

    def _fetch(stage: str, sql: str, sql_params: Dict[str, Any]) -> pd.DataFrame:
        started = time.perf_counter()
        df = _query_df_safe(sql, sql_params)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        query_perf[stage] = {
            "rows": int(len(df)),
            "sql_fetch_ms": elapsed_ms,
            "predicate_mode": predicate_mode,
        }
        if current_stock_query:
            query_perf[stage].update({
                "manufacturer_code_count": int(len(params.get("current_stock_manufacturer_codes") or [])),
                "product_code_count": int(len(params.get("current_stock_product_codes") or [])),
            })
            log.info(
                "[current_stock.perf] stage=%s rows=%s sql_fetch_ms=%s predicate_mode=%s manufacturer_code_count=%s product_code_count=%s code_in_used=%s fallback_to_like_or=%s fallback_reason=%s",
                stage,
                int(len(df)),
                elapsed_ms,
                predicate_mode,
                int(len(params.get("current_stock_manufacturer_codes") or [])),
                int(len(params.get("current_stock_product_codes") or [])),
                bool(params.get("current_stock_code_in_used")),
                bool(params.get("current_stock_fallback_to_like_or")),
                clean_text(params.get("current_stock_fallback_reason")),
            )
        else:
            log.info(
                "[product_inventory.perf] stage=sql sql_stage=%s rows=%s elapsed_ms=%s predicate_mode=%s",
                stage,
                int(len(df)),
                elapsed_ms,
                predicate_mode,
            )
        return df

    if source_path == "monthly" and cfg.get("price_mode") == "last":
        raise ValueError("월집계 조회에서는 최종매입가 기준을 사용할 수 없습니다. 일자 범위를 지정해 조회해 주세요.")

    month_sql, month_params = _build_month_carry_sql(sql_params, cfg)
    frames.append(_fetch("month_carry", month_sql, month_params))

    if source_path == "monthly":
        period_sql, period_params = _build_month_period_sql(sql_params, cfg)
        frames.append(_fetch("month_period", period_sql, period_params))
        frames = [df for df in frames if isinstance(df, pd.DataFrame) and not df.empty]
        params["_product_inventory_last_cost_scope"] = {
            "applied": False,
            "product_code_count": 0,
            "safe_limit": 0,
            "fixed_parameter_count": 0,
            "fallback_reason": "price_mode_not_last",
        }
        query_perf["last_cost"] = {
            "rows": 0,
            "sql_fetch_ms": 0.0,
            "predicate_mode": predicate_mode,
            "skipped": True,
            "skip_reason": "price_mode_not_last",
        }
        params["_product_inventory_query_perf"] = query_perf
        log.info(
            "[product_inventory.source] source_path=monthly month_table=%s detail_query_count=0",
            cfg["month_table"],
        )
        if not frames:
            return pd.DataFrame(), pd.DataFrame()
        return pd.concat(frames, ignore_index=True, sort=False), pd.DataFrame()

    first_day = _month_first(date_from)
    prev_day = _prev_day(date_from)
    if first_day <= prev_day:
        in_sql, in_params = _build_detail_sql("in", sql_params, cfg, first_day, prev_day, "carry")
        out_sql, out_params = _build_detail_sql("out", sql_params, cfg, first_day, prev_day, "carry")
        frames.append(_fetch("carry_in", in_sql, in_params))
        frames.append(_fetch("carry_out", out_sql, out_params))

    in_sql, in_params = _build_detail_sql("in", sql_params, cfg, date_from, date_to, "period")
    out_sql, out_params = _build_detail_sql("out", sql_params, cfg, date_from, date_to, "period")
    frames.append(_fetch("period_in", in_sql, in_params))
    frames.append(_fetch("period_out", out_sql, out_params))
    log.info(
        "[product_inventory.source] source_path=date_exact month_table=%s detail_query_count=%s",
        cfg["month_table"],
        4 if first_day <= prev_day else 2,
    )

    frames = [df for df in frames if isinstance(df, pd.DataFrame) and not df.empty]
    if not frames:
        return pd.DataFrame(), pd.DataFrame()

    all_df = pd.concat(frames, ignore_index=True, sort=False)
    params["_product_inventory_query_perf"] = query_perf

    if current_stock_query:
        params["_current_stock_query_perf"] = query_perf
        return all_df, pd.DataFrame()

    if cfg.get("price_mode") != "last":
        scope_meta = {
            "applied": False,
            "product_code_count": 0,
            "safe_limit": 0,
            "fixed_parameter_count": 0,
            "fallback_reason": "price_mode_not_last",
        }
        params["_product_inventory_last_cost_scope"] = scope_meta
        query_perf["last_cost"] = {
            "rows": 0,
            "sql_fetch_ms": 0.0,
            "predicate_mode": predicate_mode,
            "skipped": True,
            "skip_reason": "price_mode_not_last",
        }
        log.info(
            "[product_inventory.perf] stage=last_cost skipped=True reason=price_mode_not_last price_mode=%s",
            cfg.get("price_mode"),
        )
        return all_df, pd.DataFrame()

    base_last_sql, _ = _build_last_cost_sql(sql_params, cfg)
    product_scope, scope_meta = _last_cost_product_scope_plan(
        sql_params,
        all_df,
        base_last_sql,
    )
    params["_product_inventory_last_cost_scope"] = scope_meta
    log.info(
        "[product_inventory.perf] stage=last_cost_scope applied=%s product_code_count=%s "
        "safe_limit=%s fixed_parameter_count=%s fallback_reason=%s",
        bool(scope_meta.get("applied")),
        int(scope_meta.get("product_code_count") or 0),
        int(scope_meta.get("safe_limit") or 0),
        int(scope_meta.get("fixed_parameter_count") or 0),
        clean_text(scope_meta.get("fallback_reason")),
    )
    last_sql, last_params = _build_last_cost_sql(
        sql_params,
        cfg,
        product_scope=product_scope,
    )
    last_df = _fetch("last_cost", last_sql, last_params)

    return all_df, last_df

# -----------------------------------------------------------------------------
# 계산
# -----------------------------------------------------------------------------
def _calc_current_insu_unit(df: pd.DataFrame, asof_yyyymmdd: str) -> pd.Series:
    if df.empty:
        return pd.Series(dtype="float64")

    insu_date = df["insu_date"].fillna("").astype(str).str.replace(r"\D", "", regex=True)
    insu_price = _to_num(df["insu_price"])
    before_price = _to_num(df["before_insu_price"])
    acc_unit = _to_num(df["acc_unit"])
    acc_unit = acc_unit.where(acc_unit > 0, 1)

    use_before = (insu_date.str.len() == 8) & (insu_date > asof_yyyymmdd) & (before_price > 0)
    base_price = insu_price.copy()
    base_price.loc[use_before] = before_price.loc[use_before]

    return (base_price * acc_unit).round(4)


def _single_descriptor_pair(values: pd.Series) -> tuple[str, str]:
    """Return one exact descriptor pair, or blanks when a group has many."""
    pairs = {
        (clean_text(code), clean_text(name))
        for code, name in values.tolist()
        if clean_text(code) or clean_text(name)
    }
    if len(pairs) != 1:
        return "", ""
    return next(iter(pairs))


def _prepare_grouped_df(
    src_df: pd.DataFrame,
    last_df: pd.DataFrame,
    cfg: Dict[str, Any],
    params: Dict[str, Any],
    perf: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    if src_df is None or src_df.empty:
        return pd.DataFrame()

    perf = perf if isinstance(perf, dict) else {}
    prepare_started = time.perf_counter()
    work = src_df.copy()

    num_cols = [
        "old_in_qty", "old_in_amt", "old_out_qty",
        "now_in_qty", "now_in_amt", "now_out_qty", "now_out_amt",
    ]
    for c in num_cols:
        if c not in work.columns:
            work[c] = 0

    text_cols = [
        "group_cd", "group_nm",
        "buy_cd", "buy_nm",
        "order_cd", "order_nm",
        "maker_cd", "maker_nm",
        "physic_cd", "physic_nm", "standard",
        "kd_cd", "edi_cd", "std_cd", "pack_unit",
        "insu_date", "before_insu_date", "physic_tax",


    ]
    for c in text_cols:
        if c not in work.columns:
            work[c] = ""
        work[c] = work[c].fillna("").astype(str).str.strip()

    value_cols = [
        "master_unit_cost", "insu_price", "before_insu_price", "acc_unit",
    ]
    for c in value_cols:
        if c not in work.columns:
            work[c] = 0
        work[c] = pd.to_numeric(work[c], errors="coerce").fillna(0)
    perf["normalize_ms"] = round((time.perf_counter() - prepare_started) * 1000, 1)

    # 핵심:
    # 제품재고현황은 "선택한 집계기준 + 제품" 단위로 누적되어야 함
    # 선택하지 않은 거래처 축(buy/order/maker)은 그룹키에서 제외
    group_key = [
        "group_cd", "group_nm",
        "physic_cd", "physic_nm", "standard",
        "kd_cd", "edi_cd", "std_cd", "pack_unit",
        "master_unit_cost", "insu_date", "before_insu_date",
        "insu_price", "before_insu_price", "acc_unit", "physic_tax",
    ]

    agg_map = {c: "sum" for c in num_cols}
    work["_buy_descriptor_pair"] = list(zip(work["buy_cd"], work["buy_nm"]))
    buy_descriptors = (
        work.groupby(group_key, dropna=False, as_index=False)["_buy_descriptor_pair"]
        .agg(_single_descriptor_pair)
        .copy()
    )

    agg_map.update({
        "order_cd": "first",
        "order_nm": "first",
        "maker_cd": "first",
        "maker_nm": "first",
        "product_group_nm": "first",
        "product_di_nm": "first",
        "product_class_nm": "first",
        "special_manage_nm": "first",        
    })

    aggregate_started = time.perf_counter()
    grp = (
        work.groupby(group_key, dropna=False, as_index=False)
        .agg(agg_map)
        .copy()
    )
    grp = grp.merge(buy_descriptors, on=group_key, how="left", validate="one_to_one")
    grp[["buy_cd", "buy_nm"]] = pd.DataFrame(
        grp.pop("_buy_descriptor_pair").tolist(),
        index=grp.index,
    )
    perf["group_aggregate_ms"] = round((time.perf_counter() - aggregate_started) * 1000, 1)
    perf["master_merge_ms"] = 0.0
    perf["master_merge_mode"] = "sql_join_in_source_queries"

    # 선택한 집계기준과 group_cd/group_nm 을 다시 명확히 맞춤
    basis = cfg["group_basis"]
    if basis == "purchase":
        grp["buy_cd"] = grp["group_cd"]
        grp["buy_nm"] = grp["group_nm"]
    elif basis == "order":
        grp["order_cd"] = grp["group_cd"]
        grp["order_nm"] = grp["group_nm"]
    elif basis == "maker":
        grp["maker_cd"] = grp["group_cd"]
        grp["maker_nm"] = grp["group_nm"]

    grp["carry_qty"] = _to_num(grp["old_in_qty"]) - _to_num(grp["old_out_qty"])
    grp["stock_qty"] = _to_num(grp["carry_qty"]) + _to_num(grp["now_in_qty"]) - _to_num(grp["now_out_qty"])

    if bool(cfg.get("current_stock_query")):
        current_calc_started = time.perf_counter()
        grp["curr_insu_unit"] = _calc_current_insu_unit(grp, params["date_to"])
        grp["insu_amt"] = _round_money(_to_num(grp["stock_qty"]) * _to_num(grp["curr_insu_unit"]))
        keep_mask = (
            (_to_num(grp["carry_qty"]) != 0)
            | (_to_num(grp["now_in_qty"]) != 0)
            | (_to_num(grp["now_out_qty"]) != 0)
            | (_to_num(grp["stock_qty"]) != 0)
        )
        result = grp.loc[keep_mask].sort_values(
            ["group_nm", "physic_nm", "physic_cd"],
            ascending=[True, True, True],
            kind="stable",
        ).reset_index(drop=True)
        perf["current_stock_calc_ms"] = round((time.perf_counter() - current_calc_started) * 1000, 1)
        perf["total_group_prepare_ms"] = round((time.perf_counter() - prepare_started) * 1000, 1)
        return result

    last_cost_merge_started = time.perf_counter()
    if last_df is not None and not last_df.empty:
        last_df = last_df.copy()
        last_df["physic_cd"] = last_df["physic_cd"].fillna("").astype(str).str.strip()
        grp = grp.merge(
            last_df[["physic_cd", "last_unit_cost"]],
            on=["physic_cd"],
            how="left",
        )
    else:
        grp["last_unit_cost"] = 0
    perf["last_cost_merge_ms"] = round((time.perf_counter() - last_cost_merge_started) * 1000, 1)

    unit_calc_started = time.perf_counter()
    grp["last_unit_cost"] = _to_num(grp.get("last_unit_cost", 0))
    grp["master_unit_cost"] = _to_num(grp["master_unit_cost"])
    grp["curr_insu_unit"] = _calc_current_insu_unit(grp, params["date_to"])

    price_mode = cfg["price_mode"]

    # 문서 기준:
    # avg  = 총평균단가
    # last = 제품 기준 최종매입가
    # std  = 제품마스터 기준가
    # insu = 현보험약가(현재 화면 유지)
    # cons = 계약단가(입고) -> RD07 스키마 수령 후 연결 예정
    if price_mode == "last":
        grp["carry_unit"] = grp["last_unit_cost"]
        grp["stock_unit"] = grp["last_unit_cost"]
    elif price_mode == "std":
        grp["carry_unit"] = grp["master_unit_cost"]
        grp["stock_unit"] = grp["master_unit_cost"]
    elif price_mode == "insu":
        grp["carry_unit"] = grp["curr_insu_unit"]
        grp["stock_unit"] = grp["curr_insu_unit"]
    elif price_mode == "cons":
        # RD07 계열 스키마 미업로드 상태.
        # 현재는 오동작 방지를 위해 기준가 fallback.
        grp["carry_unit"] = grp["master_unit_cost"]
        grp["stock_unit"] = grp["master_unit_cost"]
    else:  # avg
        grp["carry_unit"] = _unit_price_from_amount_qty(grp["old_in_amt"], grp["old_in_qty"])
        grp["stock_unit"] = _unit_price_from_amount_qty(
            grp["old_in_amt"] + grp["now_in_amt"],
            grp["old_in_qty"] + grp["now_in_qty"],
        )

    grp["in_unit"] = _unit_price_from_amount_qty(grp["now_in_amt"], grp["now_in_qty"])
    grp["out_unit"] = _unit_price_from_amount_qty(grp["now_out_amt"], grp["now_out_qty"])
    perf["unit_calc_ms"] = round((time.perf_counter() - unit_calc_started) * 1000, 1)

    amount_calc_started = time.perf_counter()
    grp["carry_amt"] = _round_money(_to_num(grp["carry_qty"]) * _to_num(grp["carry_unit"]))
    grp["in_amt"] = _round_money(_to_num(grp["now_in_amt"]))
    grp["out_amt"] = _round_money(_to_num(grp["now_out_amt"]))
    grp["stock_amt"] = _round_money(_to_num(grp["stock_qty"]) * _to_num(grp["stock_unit"]))
    grp["insu_amt"] = _round_money(_to_num(grp["stock_qty"]) * _to_num(grp["curr_insu_unit"]))
    perf["amount_calc_ms"] = round((time.perf_counter() - amount_calc_started) * 1000, 1)

    dc_calc_started = time.perf_counter()
    grp["carry_dc"] = _dc_rate_from_unit(grp["curr_insu_unit"], grp["carry_unit"])
    grp["in_dc"] = _dc_rate_from_unit(grp["curr_insu_unit"], grp["in_unit"])
    grp["out_dc"] = _dc_rate_from_unit(grp["curr_insu_unit"], grp["out_unit"])
    grp["stock_dc"] = _dc_rate_from_unit(grp["curr_insu_unit"], grp["stock_unit"])
    perf["dc_calc_ms"] = round((time.perf_counter() - dc_calc_started) * 1000, 1)

    finalize_started = time.perf_counter()
    keep_mask = (
        (_to_num(grp["carry_qty"]) != 0)
        | (_to_num(grp["now_in_qty"]) != 0)
        | (_to_num(grp["now_out_qty"]) != 0)
        | (_to_num(grp["stock_qty"]) != 0)
        | (_to_num(grp["carry_amt"]) != 0)
        | (_to_num(grp["in_amt"]) != 0)
        | (_to_num(grp["out_amt"]) != 0)
        | (_to_num(grp["stock_amt"]) != 0)
    )
    grp = grp.loc[keep_mask].copy()

    grp = grp.sort_values(
        ["group_nm", "physic_nm", "physic_cd"],
        ascending=[True, True, True],
        kind="stable",
    ).reset_index(drop=True)

    # params["top"]은 화면 표시 건수이므로 여기서 자르면 안 된다.
    # 실제 계산/다운로드/현재표 기준은 별도 fetch_top 상한으로 제한한다.
    fetch_top = _inventory_fetch_top(params)
    if fetch_top and len(grp) > fetch_top:
        grp = grp.head(fetch_top).copy()
    perf["group_finalize_ms"] = round((time.perf_counter() - finalize_started) * 1000, 1)
    perf["total_group_prepare_ms"] = round((time.perf_counter() - prepare_started) * 1000, 1)
    return grp
    
def _final_display_df(
    grp: pd.DataFrame,
    cfg: Dict[str, Any],
    perf: Optional[Dict[str, Any]] = None,
    frequency_params: Optional[Dict[str, Any]] = None,
    frequency_date_to: str = "",
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    if bool(cfg.get("current_stock_query")):
        return _final_current_stock_display_df(grp, cfg)

    perf = perf if isinstance(perf, dict) else {}
    display_started = time.perf_counter()

    group_label = _group_label(cfg["group_basis"])
    current_stock_query = False
    location_columns = [group_label]

    if grp is None or grp.empty:
        cols = [
            *location_columns, "제품명", "규격", "제품코드", "KD코드", "EDI코드",
            "이월수량", "이월단가", "이월DC율", "이월금액",
            "입고수량", "입고단가", "입고DC율", "입고금액",
            "출고수량", "출고단가", "출고DC율", "출고금액",
            "재고수량", "재고단가", "재고DC율", "재고금액",
            "현보험약가", "보험금액", "표준코드",
            "제품그룹명", "제품구분명", "제품분류명",
            "매입처코드", "매입처명",
            "발주처코드", "발주처명",
            "제조사코드", "제조사명",
            "포장단위",
        ]
        return pd.DataFrame(columns=cols), {
            "row_count": 0,
            "detail_count": 0,
            "sum_carry_qty": 0.0,
            "sum_in_qty": 0.0,
            "sum_out_qty": 0.0,
            "sum_stock_qty": 0.0,
            "sum_stock_amt": 0.0,
            "sum_insu_amt": 0.0,
            "group_label": group_label,
        }

    work = grp.copy()

    first_row = {}
    try:
        if not work.empty:
            first_row = work.iloc[0].to_dict()
    except Exception:
        first_row = {}

    product_info = {
        "제품코드": clean_text(first_row.get("physic_cd")),
        "제품명": clean_text(first_row.get("physic_nm")),
        "규격": clean_text(first_row.get("standard")),
        "현보험약가": float(pd.to_numeric(first_row.get("curr_insu_unit"), errors="coerce") or 0.0),
        "보험코드": clean_text(first_row.get("kd_cd")),
        "표준코드": clean_text(first_row.get("std_cd")),
        "제조사명": clean_text(first_row.get("maker_nm")),
        "발주처명": clean_text(first_row.get("order_nm")),
        "제품그룹명": clean_text(first_row.get("product_group_nm")),
        "제품구분명": clean_text(first_row.get("product_di_nm")),
        "제품분류명": clean_text(first_row.get("product_class_nm")),
        "특수관리제품명": clean_text(first_row.get("special_manage_nm")),
    }

    if current_stock_query:
        location_names = dict(cfg.get("stock_location_name_map") or {})
        work["재고위치코드"] = work["group_cd"].fillna("").astype(str).str.strip()
        work["재고위치명"] = work["재고위치코드"].map(location_names).fillna(work["group_nm"])
    else:
        work[group_label] = work["group_nm"]
    work["제품명"] = work["physic_nm"]
    work["규격"] = work["standard"]
    work["제품코드"] = work["physic_cd"]
    work["KD코드"] = work["kd_cd"]
    work["EDI코드"] = work["edi_cd"]

    work["이월수량"] = _to_num(work["carry_qty"])
    work["이월단가"] = pd.to_numeric(work["carry_unit"], errors="coerce").round(2)
    work["이월DC율"] = pd.to_numeric(work["carry_dc"], errors="coerce").round(2)
    work["이월금액"] = _round_money(work["carry_amt"])

    work["입고수량"] = _to_num(work["now_in_qty"])
    work["입고단가"] = pd.to_numeric(work["in_unit"], errors="coerce").round(2)
    work["입고DC율"] = pd.to_numeric(work["in_dc"], errors="coerce").round(2)
    work["입고금액"] = _round_money(work["in_amt"])

    work["출고수량"] = _to_num(work["now_out_qty"])
    work["출고단가"] = pd.to_numeric(work["out_unit"], errors="coerce").round(2)
    work["출고DC율"] = pd.to_numeric(work["out_dc"], errors="coerce").round(2)
    work["출고금액"] = _round_money(work["out_amt"])

    work["재고수량"] = _to_num(work["stock_qty"])
    work["재고단가"] = pd.to_numeric(work["stock_unit"], errors="coerce").round(2)
    work["재고DC율"] = pd.to_numeric(work["stock_dc"], errors="coerce").round(2)
    work["재고금액"] = _round_money(work["stock_amt"])

    work["현보험약가"] = _to_num(work["curr_insu_unit"]).round(2)
    work["보험금액"] = _round_money(work["insu_amt"])

    work["표준코드"] = work["std_cd"]

    work["제품그룹명"] = work["product_group_nm"]
    work["제품구분명"] = work["product_di_nm"]
    work["제품분류명"] = work["product_class_nm"]

    work["매입처코드"] = work["buy_cd"]
    work["매입처명"] = work["buy_nm"]
    work["발주처코드"] = work["order_cd"]
    work["발주처명"] = work["order_nm"]
    work["제조사코드"] = work["maker_cd"]
    work["제조사명"] = work["maker_nm"]
    work["포장단위"] = work["pack_unit"]


    cols = [
        *location_columns, "제품명", "규격", "제품코드", "KD코드", "EDI코드",
        "이월수량", "이월단가", "이월DC율", "이월금액",
        "입고수량", "입고단가", "입고DC율", "입고금액",
        "출고수량", "출고단가", "출고DC율", "출고금액",
        "재고수량", "재고단가", "재고DC율", "재고금액",

        "현보험약가", "보험금액", "표준코드",
        "제품그룹명", "제품구분명", "제품분류명",
        "매입처코드", "매입처명",
        "발주처코드", "발주처명",
        "제조사코드", "제조사명",
        "포장단위",

    ]
    out = work[cols].copy()
    frequency_meta: Dict[str, Any] = {}
    if frequency_params is not None:
        frequency_attach_started = time.perf_counter()
        out, frequency_meta = attach_dashboard_frequency_snapshot(
            out,
            params=frequency_params,
            date_to=frequency_date_to,
        )
        perf["frequency_attach_ms"] = round((time.perf_counter() - frequency_attach_started) * 1000, 1)
        perf["frequency_rows_before_filter"] = int(len(out))
        frequency_filter_started = time.perf_counter()
        out = filter_product_inventory_frequency_rows(
            out,
            frequency_params.get("frequency_grade"),
        )
        perf["frequency_filter_ms"] = round((time.perf_counter() - frequency_filter_started) * 1000, 1)
        perf["frequency_rows_after_filter"] = int(len(out))
    perf["final_display_frame_ms"] = round((time.perf_counter() - display_started) * 1000, 1)

    if out.empty:
        return out, {
            "row_count": 0,
            "detail_count": 0,
            "sum_carry_qty": 0.0,
            "sum_in_qty": 0.0,
            "sum_out_qty": 0.0,
            "sum_stock_qty": 0.0,
            "sum_stock_amt": 0.0,
            "sum_insu_amt": 0.0,
            "group_label": group_label,
            "product_info": {},
            **frequency_meta,
        }

    # 합계는 상세행 기준으로 먼저 계산
    total_started = time.perf_counter()
    sum_carry_qty = float(pd.to_numeric(out["이월수량"], errors="coerce").fillna(0).sum())
    sum_in_qty = float(pd.to_numeric(out["입고수량"], errors="coerce").fillna(0).sum())
    sum_out_qty = float(pd.to_numeric(out["출고수량"], errors="coerce").fillna(0).sum())
    sum_stock_qty = float(pd.to_numeric(out["재고수량"], errors="coerce").fillna(0).sum())
    sum_stock_amt = float(pd.to_numeric(out["재고금액"], errors="coerce").fillna(0).sum())
    sum_insu_amt = float(pd.to_numeric(out["보험금액"], errors="coerce").fillna(0).sum())

    total_row = {
        c: (None if c in _DISPLAY_NUMERIC_COLS_260 else "")
        for c in cols
    }

    total_row["재고위치명" if current_stock_query else group_label] = "합계"
    total_row["이월수량"] = sum_carry_qty
    total_row["이월금액"] = float(pd.to_numeric(out["이월금액"], errors="coerce").fillna(0).sum())
    total_row["입고수량"] = sum_in_qty
    total_row["입고금액"] = float(pd.to_numeric(out["입고금액"], errors="coerce").fillna(0).sum())
    total_row["출고수량"] = sum_out_qty
    total_row["출고금액"] = float(pd.to_numeric(out["출고금액"], errors="coerce").fillna(0).sum())
    total_row["재고수량"] = sum_stock_qty
    total_row["재고금액"] = sum_stock_amt
    total_row["보험금액"] = sum_insu_amt


    detail_count = int(len(out))
    perf["subtotal_ms"] = 0.0
    perf["subtotal_rows"] = 0
    total_frame = out.iloc[[0]].copy()
    total_frame.index = [0]
    for column, value in total_row.items():
        total_frame.at[0, column] = float("nan") if value is None else value
    if _FREQUENCY_GRADE_COLUMN in total_frame.columns:
        total_frame.at[0, _FREQUENCY_GRADE_COLUMN] = ""
    if _FREQUENCY_COUNT_COLUMN in total_frame.columns:
        total_frame.at[0, _FREQUENCY_COUNT_COLUMN] = pd.NA
    out = pd.concat([out, total_frame], ignore_index=True)
    out = _finalize_display_df_260(out)
    perf["final_total_ms"] = round((time.perf_counter() - total_started) * 1000, 1)
    perf["total_display_ms"] = round((time.perf_counter() - display_started) * 1000, 1)

    meta = {
        "row_count": int(len(out)),
        "detail_count": detail_count,
        "sum_carry_qty": sum_carry_qty,
        "sum_in_qty": sum_in_qty,
        "sum_out_qty": sum_out_qty,
        "sum_stock_qty": sum_stock_qty,
        "sum_stock_amt": sum_stock_amt,
        "sum_insu_amt": sum_insu_amt,
        "group_label": group_label,
        "product_info": product_info,        
        **frequency_meta,
    }
    return out, meta


_CURRENT_STOCK_DISPLAY_COLUMNS = [
    "순번", "제품코드", "제품명", "규격",
    "재고위치명", "재고수량", "현보험약가", "보험금액", "표준코드",
    "제품그룹명", "제품구분명", "제품분류명",
    "발주처코드", "발주처명", "제조사코드",
    "재고위치코드", "KD코드", "EDI코드", "제조사명", "포장단위",
]


def _build_current_stock_table_frames(
    grp: pd.DataFrame,
    cfg: Dict[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame, Dict[str, Any]]:
    """Build separate current-stock source and display frames."""
    if grp is None or grp.empty:
        empty = pd.DataFrame(columns=_CURRENT_STOCK_DISPLAY_COLUMNS)
        return empty, empty.copy(), {
            "row_count": 0,
            "detail_count": 0,
            "sum_carry_qty": 0.0,
            "sum_in_qty": 0.0,
            "sum_out_qty": 0.0,
            "sum_stock_qty": 0.0,
            "sum_stock_amt": 0.0,
            "sum_insu_amt": 0.0,
            "group_label": "재고위치",
            "product_info": {},
            "current_stock_summary": {"product_count": 0, "maker_name": ""},
        }

    work = grp.copy()
    location_names = dict(cfg.get("stock_location_name_map") or {})
    work["재고위치코드"] = work["group_cd"].fillna("").astype(str).str.strip()
    work["재고위치명"] = work["재고위치코드"].map(location_names).fillna(work["group_nm"])

    aliases = {
        "제품코드": "physic_cd", "제품명": "physic_nm", "규격": "standard",
        "KD코드": "kd_cd", "EDI코드": "edi_cd", "표준코드": "std_cd",
        "제품그룹명": "product_group_nm", "제품구분명": "product_di_nm",
        "제품분류명": "product_class_nm", "발주처코드": "order_cd",
        "발주처명": "order_nm", "제조사코드": "maker_cd", "제조사명": "maker_nm",
        "포장단위": "pack_unit",
    }
    for display_col, source_col in aliases.items():
        work[display_col] = work[source_col]
    work["재고수량"] = _to_num(work["stock_qty"])
    work["현보험약가"] = _to_num(work["curr_insu_unit"]).round(2)
    work["보험금액"] = _round_money(work["insu_amt"])

    product_key_columns = ["제품코드", "제품명", "규격"]
    work["_현재고제품키"] = (
        work[product_key_columns]
        .fillna("")
        .astype(str)
        .agg("\x1f".join, axis=1)
    )
    product_keys = work["_현재고제품키"].drop_duplicates().tolist()
    product_serials = {key: index + 1 for index, key in enumerate(product_keys)}
    work["순번"] = work["_현재고제품키"].map(product_serials).astype(int)

    detail_count = int(len(work))
    sum_stock_qty = float(pd.to_numeric(work["재고수량"], errors="coerce").fillna(0).sum())
    sum_insu_amt = float(pd.to_numeric(work["보험금액"], errors="coerce").fillna(0).sum())

    display_parts: list[pd.DataFrame] = []
    source_parts: list[pd.DataFrame] = []
    for _, product_rows in work.groupby("_현재고제품키", sort=False, dropna=False):
        source_details = product_rows[_CURRENT_STOCK_DISPLAY_COLUMNS].copy()
        source_parts.append(source_details)
        details = source_details.copy()
        if len(details) > 1:
            sequence_column = _CURRENT_STOCK_DISPLAY_COLUMNS[0]
            location_name_column = _CURRENT_STOCK_DISPLAY_COLUMNS[4]
            location_code_column = _CURRENT_STOCK_DISPLAY_COLUMNS[15]
            repeated_text_columns = [
                column for column in _CURRENT_STOCK_DISPLAY_COLUMNS
                if column not in _DISPLAY_NUMERIC_COLS_260
                and column not in {sequence_column, location_name_column, location_code_column}
            ]
            details.loc[details.index[1:], repeated_text_columns] = ""
            details[sequence_column] = pd.to_numeric(details[sequence_column], errors="coerce").astype("Int64")
            details.loc[details.index[1:], sequence_column] = pd.NA
            details.loc[details.index[1:], ["현보험약가", "보험금액"]] = float("nan")
        display_parts.append(details)
        if len(product_rows) <= 1:
            continue

        first = source_details.iloc[0]
        source_subtotal = {column: first.get(column, "") for column in _CURRENT_STOCK_DISPLAY_COLUMNS}
        source_subtotal["재고위치코드"] = ""
        source_subtotal["재고위치명"] = "제품 합계"
        source_subtotal["재고수량"] = float(pd.to_numeric(product_rows["재고수량"], errors="coerce").fillna(0).sum())
        source_subtotal["보험금액"] = float(pd.to_numeric(product_rows["보험금액"], errors="coerce").fillna(0).sum())
        source_parts.append(pd.DataFrame([source_subtotal], columns=_CURRENT_STOCK_DISPLAY_COLUMNS))
        subtotal = {column: "" for column in _CURRENT_STOCK_DISPLAY_COLUMNS}
        # 제품 합계는 위치별 상세를 닫는 행이다. 화면용 copy에서만
        # 제품정보를 반복하지 않고 위치명과 집계 수치만 남긴다.
        subtotal["재고위치명"] = "제품 합계"
        subtotal["재고수량"] = float(pd.to_numeric(product_rows["재고수량"], errors="coerce").fillna(0).sum())
        subtotal["보험금액"] = float(pd.to_numeric(product_rows["보험금액"], errors="coerce").fillna(0).sum())
        display_parts.append(pd.DataFrame([subtotal], columns=_CURRENT_STOCK_DISPLAY_COLUMNS))

    out = _finalize_display_df_260(pd.concat(display_parts, ignore_index=True))
    source_out = _finalize_display_df_260(pd.concat(source_parts, ignore_index=True))

    product_count = int(len(product_keys))
    product_info = {} if product_count > 1 else {
        "제품코드": clean_text(work.iloc[0].get("제품코드")),
        "제품명": clean_text(work.iloc[0].get("제품명")),
        "규격": clean_text(work.iloc[0].get("규격")),
        "현보험약가": float(pd.to_numeric(work.iloc[0].get("현보험약가"), errors="coerce") or 0.0),
        "보험코드": clean_text(work.iloc[0].get("KD코드")),
        "표준코드": clean_text(work.iloc[0].get("표준코드")),
        "제조사명": clean_text(work.iloc[0].get("제조사명")),
        "발주처명": clean_text(work.iloc[0].get("발주처명")),
        "제품그룹명": clean_text(work.iloc[0].get("제품그룹명")),
        "제품구분명": clean_text(work.iloc[0].get("제품구분명")),
        "제품분류명": clean_text(work.iloc[0].get("제품분류명")),
    }
    return out, source_out, {
        "row_count": int(len(out)),
        "detail_count": detail_count,
        "sum_carry_qty": 0.0,
        "sum_in_qty": 0.0,
        "sum_out_qty": 0.0,
        "sum_stock_qty": sum_stock_qty,
        "sum_stock_amt": 0.0,
        "sum_insu_amt": sum_insu_amt,
        "group_label": "재고위치",
        "product_info": product_info,
        "current_stock_query": True,
    }


def _final_current_stock_display_df(
    grp: pd.DataFrame,
    cfg: Dict[str, Any],
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    """Return the current-stock display copy for existing display-only callers."""
    display_df, _source_df, meta = _build_current_stock_table_frames(grp, cfg)
    return display_df, meta


def _filter_current_stock_frequency_rows(
    grp: pd.DataFrame,
    *,
    params: Dict[str, Any],
    date_to: str,
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    """Filter current-stock facts with the same approved snapshot boundary."""
    selected = _normalize_product_inventory_frequency_filter(params.get("frequency_grade"))
    if not selected or grp is None or grp.empty:
        return grp, {}

    frequency_frame = pd.DataFrame({"제품코드": grp["physic_cd"]}, index=grp.index)
    attached, meta = attach_dashboard_frequency_snapshot(
        frequency_frame,
        params=params,
        date_to=date_to,
    )
    attached = filter_product_inventory_frequency_rows(attached, selected)
    return grp.loc[attached.index].copy(), meta

# -----------------------------------------------------------------------------
# 외부 공개 함수
# -----------------------------------------------------------------------------
def get_product_inventory_df(params: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    params = coalesce_params(params)
    cfg = _settings(params)

    date_from, date_to = _resolve_inventory_dates(params)

    work_params = dict(params)
    work_params["date_from"] = date_from
    work_params["date_to"] = date_to
    source_path = resolve_product_inventory_source_path(params)
    work_params["source_path"] = source_path
    if source_path == "monthly":
        month_from = clean_text(work_params.get("month_from") or date_from)[:6]
        month_to = clean_text(work_params.get("month_to") or date_to)[:6]
        work_params["month_from"] = month_from
        work_params["month_to"] = month_to
        work_params["date_from"] = f"{month_from}01"
        work_params["date_to"] = _month_last(f"{month_to}01")

    stock_codes_before = _normalize_stock_codes(work_params)

    if stock_codes_before:
        registered_codes = _registered_stock_codes(stock_codes_before)
        if not registered_codes:
            return pd.DataFrame()

        work_params["stock_cds"] = registered_codes
        if len(registered_codes) == 1:
            work_params["stock_cd"] = registered_codes[0]
    else:
        resolved_stock_cds = _resolve_stock_codes(work_params)
        if resolved_stock_cds:
            work_params["stock_cds"] = resolved_stock_cds
            if len(resolved_stock_cds) == 1:
                work_params["stock_cd"] = resolved_stock_cds[0]
        elif _stock_name_condition(work_params):
            return pd.DataFrame()
        
    src_df, last_df = _collect_source_df(work_params, cfg)

    grp = _prepare_grouped_df(src_df, last_df, cfg, work_params)
    df_display, _ = _final_display_df(grp, cfg)
    return df_display


def get_product_inventory_result(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    request_started = time.perf_counter()
    params = coalesce_params(params)
    cfg = _settings(params)

    try:
        filter_prepare_started = time.perf_counter()
        date_from, date_to = _resolve_inventory_dates(params)

        work_params = dict(params)
        work_params["date_from"] = date_from
        work_params["date_to"] = date_to
        source_path = resolve_product_inventory_source_path(params)
        work_params["source_path"] = source_path
        if source_path == "monthly":
            month_from = clean_text(work_params.get("month_from") or date_from)[:6]
            month_to = clean_text(work_params.get("month_to") or date_to)[:6]
            work_params["month_from"] = month_from
            work_params["month_to"] = month_to
            work_params["date_from"] = f"{month_from}01"
            work_params["date_to"] = _month_last(f"{month_to}01")
            date_from, date_to = work_params["date_from"], work_params["date_to"]

        stock_name = _stock_name_condition(work_params)
        stock_codes_before = _normalize_stock_codes(work_params)

        # 1) 사용자가 재고위치코드를 직접 입력한 경우:
        #    코드마스터에 등록된 코드인지 먼저 확인한다.
        if stock_codes_before:
            registered_codes = _registered_stock_codes(stock_codes_before)

            if not registered_codes:
                return _inventory_text_payload(
                    message=_stock_not_found_message(stock_codes=stock_codes_before),
                    params=params,
                    cfg=cfg,
                    date_from=date_from,
                    date_to=date_to,
                    work_params=work_params,
                    meta={
                        "result_status": "no_data",
                        "row_count": 0,
                        "row_count_total": 0,
                    },
                )

            work_params["stock_cds"] = registered_codes
            if len(registered_codes) == 1:
                work_params["stock_cd"] = registered_codes[0]

        # 2) 사용자가 재고위치명을 입력한 경우:
        #    이름으로 코드를 찾고, 없으면 등록 코드 없음 메시지를 낸다.
        else:
            resolved_stock_cds = _resolve_stock_codes(work_params)

            if resolved_stock_cds:
                work_params["stock_cds"] = resolved_stock_cds
                if len(resolved_stock_cds) == 1:
                    work_params["stock_cd"] = resolved_stock_cds[0]
            elif stock_name:
                return _inventory_text_payload(
                    message=_stock_not_found_message(stock_name=stock_name),
                    params=params,
                    cfg=cfg,
                    date_from=date_from,
                    date_to=date_to,
                    work_params=work_params,
                    meta={
                        "result_status": "no_data",
                        "row_count": 0,
                        "row_count_total": 0,
                    },
                )

        product_scope_meta: Dict[str, Any] = {
            "product_name_scope": False,
            "candidate_count": 0,
            "scope_applied": False,
            "safe_limit": 0,
            "fallback_reason": "not_explicit_product_name",
            "candidate_elapsed_ms": 0.0,
        }
        if (
            clean_text(work_params.get("physic_nm"))
            and not clean_text(work_params.get("physic_cd"))
            and not clean_text(work_params.get("nlq_unlabeled_name"))
            and not bool(cfg.get("current_stock_query"))
        ):
            last_cost_base_sql, _ = _build_last_cost_sql(work_params, cfg)
            safe_limit = _bounded_product_scope_safe_limit(
                last_cost_base_sql,
                bind_occurrences=2,
            )
            product_codes, product_scope_meta = _resolve_explicit_product_name_scope(
                work_params,
                safe_limit=safe_limit,
            )
            if product_scope_meta.get("scope_applied"):
                work_params["_product_inventory_explicit_product_codes"] = product_codes
                work_params["_product_inventory_explicit_product_scope_applied"] = True
        work_params["_product_inventory_product_name_scope"] = product_scope_meta
        log.info(
            "[product_inventory.perf] stage=product_name_scope product_name_scope=%s "
            "candidate_count=%s scope_applied=%s safe_limit=%s fallback_reason=%s "
            "candidate_elapsed_ms=%s predicate_mode=%s",
            bool(product_scope_meta.get("product_name_scope")),
            int(product_scope_meta.get("candidate_count") or 0),
            bool(product_scope_meta.get("scope_applied")),
            int(product_scope_meta.get("safe_limit") or 0),
            clean_text(product_scope_meta.get("fallback_reason")),
            product_scope_meta.get("candidate_elapsed_ms", 0),
            _inventory_predicate_mode(work_params),
        )

        if product_scope_meta.get("fallback_reason") == "no_candidates":
            return _inventory_text_payload(
                message="해당 조회조건의 자료가 없습니다.",
                params=params,
                cfg=cfg,
                date_from=date_from,
                date_to=date_to,
                work_params=work_params,
                meta={
                    "result_status": "no_data",
                    "row_count": 0,
                    "row_count_total": 0,
                    "product_name_scope": product_scope_meta,
                },
            )

        filter_prepare_ms = round((time.perf_counter() - filter_prepare_started) * 1000, 1)
        service_started = time.perf_counter()
        src_df, last_df = _collect_source_df(work_params, cfg)
        source_elapsed_ms = round((time.perf_counter() - service_started) * 1000, 1)
        group_started = time.perf_counter()
        group_perf: Dict[str, Any] = {}
        grp = _prepare_grouped_df(src_df, last_df, cfg, work_params, perf=group_perf)
        group_elapsed_ms = round((time.perf_counter() - group_started) * 1000, 1)
        if bool(cfg.get("current_stock_query")):
            frequency_filter_started = time.perf_counter()
            grp, frequency_meta = _filter_current_stock_frequency_rows(
                grp,
                params=work_params,
                date_to=date_to,
            )
            frequency_filter_ms = round((time.perf_counter() - frequency_filter_started) * 1000, 1)
            frame_started = time.perf_counter()
            df_display, df_full, meta = _build_current_stock_table_frames(grp, cfg)
            frame_elapsed_ms = round((time.perf_counter() - frame_started) * 1000, 1)
            meta.update(frequency_meta)
            meta["frequency_filter_ms"] = frequency_filter_ms
            meta["current_stock_query_perf"] = dict(work_params.get("_current_stock_query_perf") or {})
            meta["current_stock_source_elapsed_ms"] = source_elapsed_ms
            meta["current_stock_group_elapsed_ms"] = group_elapsed_ms
            meta["current_stock_frame_elapsed_ms"] = frame_elapsed_ms
            meta["current_stock_service_elapsed_ms"] = round((time.perf_counter() - service_started) * 1000, 1)
            log.info(
                "[current_stock.perf] stage=service_complete source_elapsed_ms=%s group_elapsed_ms=%s frequency_filter_ms=%s frame_elapsed_ms=%s total_elapsed_ms=%s detail_rows=%s predicate_mode=%s",
                source_elapsed_ms,
                group_elapsed_ms,
                frequency_filter_ms,
                frame_elapsed_ms,
                meta["current_stock_service_elapsed_ms"],
                int(meta.get("detail_count", 0) or 0),
                clean_text(work_params.get("current_stock_predicate_mode")) or "standard",
            )
        else:
            display_perf: Dict[str, Any] = {}
            df_display, meta = _final_display_df(
                grp,
                cfg,
                perf=display_perf,
                frequency_params=params,
                frequency_date_to=date_to,
            )
            df_full = df_display
            service_total_ms = round((time.perf_counter() - request_started) * 1000, 1)
            query_perf = dict(work_params.get("_product_inventory_query_perf") or {})
            perf_meta = {
                "source_path": source_path,
                "predicate_mode": _inventory_predicate_mode(work_params),
                "filter_prepare_ms": filter_prepare_ms,
                "source_elapsed_ms": source_elapsed_ms,
                "sql": query_perf,
                "group": group_perf,
                "display": display_perf,
                "last_cost_scope": dict(work_params.get("_product_inventory_last_cost_scope") or {}),
                "product_name_scope": dict(work_params.get("_product_inventory_product_name_scope") or {}),
                "service_total_ms": service_total_ms,
            }
            meta["product_inventory_perf"] = perf_meta
            log.info(
                "[product_inventory.perf] stage=service_complete source_path=%s predicate_mode=%s "
                "filter_prepare_ms=%s source_elapsed_ms=%s normalize_ms=%s "
                "group_aggregate_ms=%s master_merge_ms=%s last_cost_merge_ms=%s "
                "unit_calc_ms=%s amount_calc_ms=%s dc_calc_ms=%s group_finalize_ms=%s "
                "frequency_attach_ms=%s frequency_filter_ms=%s frequency_rows_before_filter=%s "
                "frequency_rows_after_filter=%s final_display_frame_ms=%s subtotal_ms=%s subtotal_rows=%s final_total_ms=%s "
                "service_total_ms=%s source_rows=%s result_rows=%s",
                source_path,
                perf_meta["predicate_mode"],
                filter_prepare_ms,
                source_elapsed_ms,
                group_perf.get("normalize_ms", 0),
                group_perf.get("group_aggregate_ms", 0),
                group_perf.get("master_merge_ms", 0),
                group_perf.get("last_cost_merge_ms", 0),
                group_perf.get("unit_calc_ms", 0),
                group_perf.get("amount_calc_ms", 0),
                group_perf.get("dc_calc_ms", 0),
                group_perf.get("group_finalize_ms", 0),
                display_perf.get("frequency_attach_ms", 0),
                display_perf.get("frequency_filter_ms", 0),
                display_perf.get("frequency_rows_before_filter", 0),
                display_perf.get("frequency_rows_after_filter", 0),
                display_perf.get("final_display_frame_ms", 0),
                display_perf.get("subtotal_ms", 0),
                display_perf.get("subtotal_rows", 0),
                display_perf.get("final_total_ms", 0),
                service_total_ms,
                int(len(src_df)),
                int(len(df_display)),
            )

        detail_count = int(meta.get("detail_count", 0) or 0)

        if df_display is None or df_display.empty or detail_count <= 0:
            return _inventory_text_payload(
                message="해당 조회조건의 자료가 없습니다.",
                params=params,
                cfg=cfg,
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

        query_summary = _build_inventory_query_summary(
            date_from=date_from,
            date_to=date_to,
            cfg=cfg,
            work_params=work_params,
            params=params,
        )


        payload: Dict[str, Any] = {
            "title": TITLE,
            "action": ACTION,
            "type": "table",
            "params": {
                **params,
                "date_from": date_from,
                "date_to": date_to,
                "stock_mode": _stock_mode_label(cfg["stock_mode"]),
                "group_basis": _group_basis_label(cfg["group_basis"]),
                "price_mode": _price_mode_label(cfg["price_mode"]),
                "stock_cds": _resolve_stock_codes(work_params),
            },
            "columns": list(df_display.columns),
            "df": df_full,
            "df_display": df_display,
            "records": df_display.to_dict(orient="records"),
            "final": True,
            "message": f"제품재고현황 {detail_count:,}건",
            "meta": {
                **meta,
                "result_status": "success",
                "query_summary": query_summary,
                "summary_md": _build_inventory_header_md(meta),
                "note": _build_inventory_header_md(meta),
            },
        }

        return payload

    except ValueError as e:
        return _inventory_text_payload(
            message=f"조회조건이 부족합니다. {str(e)}",
            params=params,
            cfg=cfg,
        )
    except Exception:
        return _inventory_text_payload(
            message="제품재고현황 조회 중 오류가 발생했습니다. 조회조건을 다시 확인해 주세요.",
            params=params,
            cfg=cfg,
        )
