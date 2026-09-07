"""Rddbc070 contract-price history and effective-date lookup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import logging
import re
from typing import Any, Optional

import pandas as pd

from app.services.erp_table_query_service import (
    build_feature_result,
    execute_bound_select,
    registered_query_limits,
)
from app.services.product_master_filter_contract import (
    append_product_master_filter_clauses,
    build_product_master_enrichment_sql,
    normalize_product_master_filters,
)


TABLE = "rddbc070"
HISTORY_ACTION = "계약단가 이력 조회"
CURRENT_ACTION = "최종 계약단가 조회"
log = logging.getLogger("ssai.sims.rddbc070")


_PRICE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("실입고단가", "실입고단가"),
    ("장부입고단가", "장부입고단가"),
    ("otc 입고단가", "otc 입고단가"),
    ("실줄고단가", "실줄고단가"),
    ("장부출고단가", "장부출고단가"),
    ("otc 출고단가", "otc 출고단가"),
)

_RESULT_COLUMN_ORDER: tuple[str, ...] = (
    "조회순번",
    "단가적용거래처", "단가적용처명",
    "제품코드", "제품명", "제약사", "제품그룹", "제품구분코드", "제품구분", "제품분류",
    "계약시작일자",
    "실입고단가", "장부입고단가", "otc 입고단가",
    "실줄고단가", "장부출고단가", "otc 출고단가",
    "보험단가", "계약여부", "계약상태", "종료단가",
)

_GARBAGE_CANDIDATE_SQL: tuple[str, ...] = (
    "LTRIM(RTRIM(C.Rd07_Cost_Apply_Cd)) <> ''",
    "LTRIM(RTRIM(C.Rd07_Start_Date)) <> '00000000'",
    "NOT ("
    "C.Rd07_In_Amt = 0 AND C.Rd07_In_Ramt = 0 AND C.Rd07_In_Oamt = 0 "
    "AND C.Rd07_Out_Amt = 0 AND C.Rd07_Out_Ramt = 0 AND C.Rd07_Out_Oamt = 0"
    ")",
)

PRODUCT_TYPE_LABELS: dict[str, str] = {
    "1": "일반약(보험)",
    "2": "수입약(보험)",
    "3": "전문약(보험)",
    "5": "일반비보험",
    "6": "수입약",
    "7": "소모품",
    "8": "원료약품",
}
INSURANCE_PRODUCT_TYPES = frozenset({"1", "2", "3"})
NON_INSURANCE_PRODUCT_TYPES = frozenset({"5", "6", "7", "8"})

_CONTRACT_PRICE_COLUMN = {
    ("inbound", "real"): "실입고단가",
    ("inbound", "book"): "장부입고단가",
    ("inbound", "otc"): "otc 입고단가",
    ("outbound", "real"): "실줄고단가",
    ("outbound", "book"): "장부출고단가",
    ("outbound", "otc"): "otc 출고단가",
}


@dataclass(frozen=True)
class ContractPriceLookupKey:
    cost_apply_cd: str
    physic_cd: str
    transaction_date: str


def _first_value(row: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        if isinstance(row, dict) and name in row:
            return row.get(name)
        try:
            if name in row:
                return row.get(name)
        except (TypeError, AttributeError):
            continue
    return None


def _transaction_lookup_key(row: Any, *, direction: str) -> ContractPriceLookupKey:
    if direction == "inbound":
        cost_names = ("Rd11_Cost_Apply_Cd", "단가적용처코드", "단가적용코드")
        product_names = ("Rd11_Physic_Cd", "제품코드")
        date_names = ("Rd11_In_YyMmDd", "매입일자", "입고일자")
    elif direction == "outbound":
        cost_names = ("Rd12_Cost_Apply_Cd", "단가적용처코드", "단가적용코드")
        product_names = ("Rd12_Physic_Cd", "제품코드")
        date_names = ("Rd12_Out_YyMmDd", "매출일자", "출고일자")
    else:
        raise ValueError("direction은 inbound 또는 outbound여야 합니다.")

    cost_apply_cd = _clean(_first_value(row, cost_names))
    physic_cd = _clean(_first_value(row, product_names))
    transaction_date = _date_value(
        _first_value(row, date_names), field="거래일자", required=True
    )
    if not cost_apply_cd or not physic_cd:
        raise ValueError("계약단가 선택에는 단가적용코드와 제품코드가 모두 필요합니다.")
    return ContractPriceLookupKey(cost_apply_cd, physic_cd, transaction_date)


def inbound_contract_lookup_key(row: Any) -> ContractPriceLookupKey:
    return _transaction_lookup_key(row, direction="inbound")


def outbound_contract_lookup_key(row: Any) -> ContractPriceLookupKey:
    return _transaction_lookup_key(row, direction="outbound")


def select_latest_effective_contract(
    rows: pd.DataFrame,
    key: ContractPriceLookupKey,
) -> Optional[dict[str, Any]]:
    """Select one exact contract without reviving an older zero-price row."""
    if not isinstance(rows, pd.DataFrame) or rows.empty:
        return None
    required = {
        "단가적용거래처", "제품코드", "계약시작일자",
        *(column for column, _label in _PRICE_COLUMNS),
    }
    if not required.issubset(rows.columns):
        raise ValueError("계약 후보에 식별키 컬럼이 없습니다.")

    work = rows.copy()
    cost = work["단가적용거래처"].astype(str).str.strip()
    product = work["제품코드"].astype(str).str.strip()
    start = work["계약시작일자"].astype(str).str.replace(r"[^0-9]", "", regex=True)
    numeric_prices = pd.DataFrame(
        {
            column: pd.to_numeric(work[column], errors="coerce")
            for column, _label in _PRICE_COLUMNS
        },
        index=work.index,
    )
    all_zero = numeric_prices.eq(0).all(axis=1)
    mask = (
        cost.ne("")
        & ~all_zero
        & start.ne("00000000")
        & cost.eq(key.cost_apply_cd)
        & product.eq(key.physic_cd)
        & start.le(key.transaction_date)
    )
    if "삭제 flag" in work.columns:
        mask &= ~work["삭제 flag"].astype(str).str.strip().str.upper().eq("E")
    candidates = work.loc[mask].copy()
    if candidates.empty:
        return None
    candidates["__contract_start"] = start.loc[candidates.index]
    latest = candidates.sort_values("__contract_start", ascending=False, kind="stable").iloc[0]
    return {k: v for k, v in latest.to_dict().items() if k != "__contract_start"}


def _nonnegative_decimal(value: Any, *, field: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field}이 유효한 숫자가 아닙니다.") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{field}은 0 이상이어야 합니다.")
    return amount


def resolve_contract_price(
    *,
    contract_row: Optional[dict[str, Any]],
    direction: str,
    price_basis: str,
    product_type: Any,
    transaction_insurance_price: Any = None,
    last_purchase_price: Any = None,
) -> dict[str, Any]:
    """Resolve a price from explicit as-of inputs; never reads current master state."""
    price_column = _CONTRACT_PRICE_COLUMN.get((str(direction), str(price_basis)))
    if not price_column:
        raise ValueError("지원하지 않는 거래방향 또는 단가 기준입니다.")

    if contract_row is not None:
        if price_column not in contract_row:
            return {"status": "invalid_contract", "amount": None, "source": "contract"}
        amount = _nonnegative_decimal(contract_row.get(price_column), field=price_column)
        if amount == 0:
            return {
                "status": "terminated",
                "amount": None,
                "source": "contract_zero",
                "price_column": price_column,
            }
        return {
            "status": "resolved",
            "amount": float(amount),
            "source": "contract",
            "price_column": price_column,
        }

    product_type_code = _clean(product_type)
    if product_type_code in INSURANCE_PRODUCT_TYPES:
        if transaction_insurance_price is None:
            return {"status": "input_required", "amount": None, "source": "insurance_as_of"}
        insurance_price = _nonnegative_decimal(
            transaction_insurance_price, field="거래시점 보험가"
        )
        if insurance_price == 0:
            return {"status": "no_price", "amount": None, "source": "insurance_as_of"}
        amount = insurance_price * (Decimal("0.95") if direction == "inbound" else Decimal("1"))
        return {
            "status": "resolved",
            "amount": float(amount),
            "source": "insurance_as_of",
            "product_type": product_type_code,
        }

    if product_type_code in NON_INSURANCE_PRODUCT_TYPES:
        if last_purchase_price is None:
            return {"status": "input_required", "amount": None, "source": "last_purchase"}
        last_price = _nonnegative_decimal(last_purchase_price, field="최종 매입단가")
        if last_price == 0:
            return {"status": "no_price", "amount": None, "source": "last_purchase"}
        return {
            "status": "resolved",
            "amount": float(last_price),
            "source": "last_purchase",
            "product_type": product_type_code,
        }

    return {"status": "unsupported_product_type", "amount": None, "source": "none"}


_PRODUCT_MASTER_JOINS, _PRODUCT_MASTER_EXPRESSIONS = build_product_master_enrichment_sql(
    product_alias="P"
)


_SELECT_COLUMNS = f"""
    LTRIM(RTRIM(CAST(C.Rd07_Cost_Apply_Cd AS VARCHAR(50)))) AS [단가적용거래처],
    LTRIM(RTRIM(ISNULL(V.Rd03_Ven_Nm, ''))) AS [단가적용처명],
    C.Rd07_Physic_Cd AS [제품코드],
    LTRIM(RTRIM(ISNULL(P.Rd04_Physic_Nm, ''))) AS [제품명],
    LTRIM(RTRIM(ISNULL({_PRODUCT_MASTER_EXPRESSIONS['maker_nm']}, ''))) AS [제약사],
    LTRIM(RTRIM(ISNULL({_PRODUCT_MASTER_EXPRESSIONS['product_group_nm']}, ''))) AS [제품그룹],
    LTRIM(RTRIM(ISNULL(P.Rd04_Physic_Di, ''))) AS [제품구분코드],
    LTRIM(RTRIM(ISNULL({_PRODUCT_MASTER_EXPRESSIONS['product_di_nm']}, ''))) AS [제품구분],
    LTRIM(RTRIM(ISNULL({_PRODUCT_MASTER_EXPRESSIONS['product_class_nm']}, ''))) AS [제품분류],
    C.Rd07_Start_Date AS [계약시작일자],
    C.Rd07_In_Amt AS [실입고단가],
    C.Rd07_In_Ramt AS [장부입고단가],
    C.Rd07_In_Oamt AS [otc 입고단가],
    C.Rd07_Out_Amt AS [실줄고단가],
    C.Rd07_Out_Ramt AS [장부출고단가],
    C.Rd07_Out_Oamt AS [otc 출고단가],
    C.Rd07_Insu_Price AS [보험단가],
    C.Rd07_Contract_YN AS [계약여부],
    C.Rd07_Sale_Physic_Cd AS [매출처 제품코드],
    C.Rd07_Sale_Unit AS [매출처 단위],
    C.Rd07_Sale_Standard AS [매출처 규격],
    C.Rd07_Acc_Rate AS [매출처 환산단위],
    C.Rd07_Sale_Physic_Nm AS [매출처 제품명],
    C.Rd07_Other AS [기타],
    C.Rd07_Del_Flag AS [삭제 flag],
    C.Rd07_Add_Date AS [등록일자],
    C.Rd07_Add_Cd AS [등록자],
    LTRIM(RTRIM(ISNULL(AU.Rd06_User_Nm, ''))) AS [등록자명],
    C.Rd07_Mod_Date AS [수정일자],
    C.Rd07_Mod_Cd AS [수정자],
    LTRIM(RTRIM(ISNULL(MU.Rd06_User_Nm, ''))) AS [수정자명],
    C.Rd07_Add_Time AS [등록일시],
    C.Rd07_Mod_Time AS [수정일시],
    C.Rd07_Web_Gu AS [WEB 게시여부],
    C.Rd07_Web_Limit_Quantity AS [WEB 매출 한도 수량],
    C.Rd07_Web_Limit_Percent AS [WEB 매출한도 재고 비율]
""".strip()


_JOINS = f"""
FROM dbo.Rddbc070 AS C WITH (NOLOCK)
LEFT JOIN dbo.Rddbc030 AS V WITH (NOLOCK)
    ON C.Rd07_Cost_Apply_Cd = V.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc040 AS P WITH (NOLOCK)
    ON C.Rd07_Physic_Cd = P.Rd04_Physic_Cd
LEFT JOIN dbo.Rddbc060 AS AU WITH (NOLOCK)
    ON C.Rd07_Add_Cd = AU.Rd06_User_Cd
LEFT JOIN dbo.Rddbc060 AS MU WITH (NOLOCK)
    ON C.Rd07_Mod_Cd = MU.Rd06_User_Cd
{_PRODUCT_MASTER_JOINS}
""".strip()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _date_value(value: Any, *, field: str, required: bool = False) -> str:
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y%m%d")
    digits = re.sub(r"[^0-9]", "", _clean(value))
    if not digits:
        if required:
            raise ValueError(f"{field}이 필요합니다.")
        return ""
    if len(digits) != 8:
        raise ValueError(f"{field}은 YYYYMMDD 형식이어야 합니다.")
    try:
        datetime.strptime(digits, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"{field}이 올바른 날짜가 아닙니다.") from exc
    return digits


def normalize_rddbc070_params(
    params: Optional[dict[str, Any]],
    *,
    mode: str,
) -> dict[str, Any]:
    out = dict(params or {})
    if _clean(out.get("contract_yn")) or _clean(out.get("del_flag")):
        raise ValueError("계약여부와 삭제 flag의 값 의미가 확정되지 않아 조건으로 사용할 수 없습니다.")
    out["date_from"] = _date_value(out.get("date_from"), field="계약시작일 시작")
    out["date_to"] = _date_value(out.get("date_to"), field="계약시작일 종료")
    if out["date_from"] and out["date_to"] and out["date_from"] > out["date_to"]:
        raise ValueError("계약시작일 시작이 종료보다 늦습니다.")
    if mode == "current":
        out["as_of"] = _date_value(
            out.get("as_of") or date.today(), field="기준일", required=True
        )
    else:
        out.pop("as_of", None)
    product_filters = normalize_product_master_filters(out)
    out.update(product_filters)
    limits = registered_query_limits(out)
    out["display_top"] = limits.display_top
    out["source_top"] = limits.source_top
    out["top"] = limits.source_top
    out["_limit_contract"] = limits
    out["mode"] = mode
    return out


def _entity_filters(params: dict[str, Any]) -> tuple[list[str], list[Any]]:
    clauses: list[str] = ["C.Rd07_Del_Flag <> 'E'", *_GARBAGE_CANDIDATE_SQL]
    values: list[Any] = []
    if _clean(params.get("ven_cd")):
        clauses.append("C.Rd07_Cost_Apply_Cd = ?")
        values.append(_clean(params["ven_cd"]))
    if _clean(params.get("ven_nm")):
        clauses.append("V.Rd03_Ven_Nm LIKE ?")
        values.append(f"%{_clean(params['ven_nm'])}%")
    if _clean(params.get("physic_nm")):
        clauses.append("P.Rd04_Physic_Nm LIKE ?")
        values.append(f"%{_clean(params['physic_nm'])}%")
    append_product_master_filter_clauses(
        clauses,
        values,
        params,
        expressions={**_PRODUCT_MASTER_EXPRESSIONS, "physic_cd": "C.Rd07_Physic_Cd"},
    )
    return clauses, values


def _date_filters(params: dict[str, Any], *, alias: str = "C") -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    if params.get("date_from"):
        clauses.append(f"{alias}.Rd07_Start_Date >= ?")
        values.append(params["date_from"])
    if params.get("date_to"):
        clauses.append(f"{alias}.Rd07_Start_Date <= ?")
        values.append(params["date_to"])
    return clauses, values


def _add_contract_status(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    out = df.copy()
    ended_labels: list[str] = []
    overall: list[str] = []
    for _, row in out.iterrows():
        ended: list[str] = []
        nonzero = 0
        for column, label in _PRICE_COLUMNS:
            value = pd.to_numeric(pd.Series([row.get(column)]), errors="coerce").iloc[0]
            if pd.isna(value):
                continue
            if float(value) == 0.0:
                ended.append(label)
            else:
                nonzero += 1
        ended_labels.append(", ".join(ended))
        if ended and nonzero == 0:
            overall.append("계약 종료(전체 단가 0)")
        elif ended:
            overall.append("일부 단가 0(해석 미확정)")
        else:
            overall.append("단가 있음")
    out["계약상태"] = overall
    out["종료단가"] = ended_labels
    return out


def _prepare_result_projection(df: pd.DataFrame) -> pd.DataFrame:
    """Add R070-derived display fields once to the canonical result frame."""
    out = _add_contract_status(df)
    if not isinstance(out, pd.DataFrame) or out.empty:
        return out
    if "조회순번" not in out.columns:
        out.insert(0, "조회순번", range(1, len(out) + 1))
    leading = [column for column in _RESULT_COLUMN_ORDER if column in out.columns]
    trailing = [column for column in out.columns if column not in leading]
    return out.loc[:, [*leading, *trailing]]


def get_rddbc070_history_df(params: Optional[dict[str, Any]] = None) -> pd.DataFrame:
    qparams = normalize_rddbc070_params(params, mode="history")
    clauses, values = _entity_filters(qparams)
    date_clauses, date_values = _date_filters(qparams)
    clauses.extend(date_clauses)
    values.extend(date_values)
    sql = f"""
SELECT TOP {int(qparams['top'])}
{_SELECT_COLUMNS}
{_JOINS}
WHERE {' AND '.join(clauses)}
ORDER BY C.Rd07_Start_Date DESC, C.Rd07_Cost_Apply_Cd, C.Rd07_Physic_Cd
""".strip()
    return _prepare_result_projection(execute_bound_select(sql, values))


def get_rddbc070_current_df(params: Optional[dict[str, Any]] = None) -> pd.DataFrame:
    qparams = normalize_rddbc070_params(params, mode="current")
    clauses, values = _entity_filters(qparams)
    clauses.append("C.Rd07_Start_Date <= ?")
    values.append(qparams["as_of"])
    outer_clauses = ["R.__rn = 1"]
    if qparams.get("date_from"):
        outer_clauses.append("R.[계약시작일자] >= ?")
        values.append(qparams["date_from"])
    if qparams.get("date_to"):
        outer_clauses.append("R.[계약시작일자] <= ?")
        values.append(qparams["date_to"])
    sql = f"""
WITH R AS (
    SELECT
{_SELECT_COLUMNS},
        ROW_NUMBER() OVER (
            PARTITION BY C.Rd07_Cost_Apply_Cd, C.Rd07_Physic_Cd
            ORDER BY C.Rd07_Start_Date DESC
        ) AS __rn
    {_JOINS}
    WHERE {' AND '.join(clauses)}
)
SELECT TOP {int(qparams['top'])}
    R.*
FROM R
WHERE {' AND '.join(outer_clauses)}
ORDER BY R.[단가적용거래처], R.[제품코드]
""".strip()
    df = execute_bound_select(sql, values)
    if isinstance(df, pd.DataFrame) and "__rn" in df.columns:
        df = df.drop(columns=["__rn"])
    return _prepare_result_projection(df)


def _query_summary(params: dict[str, Any], *, mode: str, row_count: int, df: pd.DataFrame) -> str:
    parts: list[str] = []
    if params.get("ven_cd") or params.get("ven_nm"):
        parts.append(f"단가적용거래처 {_clean(params.get('ven_cd')) or _clean(params.get('ven_nm'))}")
    if params.get("physic_cd") or params.get("physic_nm") or params.get("product_keyword"):
        parts.append(
            f"제품 {_clean(params.get('physic_cd')) or _clean(params.get('physic_nm')) or _clean(params.get('product_keyword'))}"
        )
    for key, label in (
        ("maker_nm", "제약사"),
        ("product_group_nm", "제품그룹"),
        ("product_di_nm", "제품구분"),
        ("product_di_semantic_group", "상위 제품구분"),
        ("product_class_nm", "제품분류"),
        ("insu_cd", "보험코드"),
        ("barcode", "바코드"),
        ("product_unit_price", "제품마스터 단가"),
    ):
        if params.get(key):
            parts.append(f"{label} {_clean(params.get(key))}")
    if params.get("date_from") or params.get("date_to"):
        parts.append(f"계약시작일 {_clean(params.get('date_from')) or '처음'}~{_clean(params.get('date_to')) or '마지막'}")
    if mode == "current":
        parts.append(f"기준일 {params.get('as_of')}")
    condition = " / ".join(parts) if parts else "전체"
    return (
        f"조회조건: {condition}\n\n"
        f"결과: {row_count:,}건\n\n"
        "최신 계약의 선택 단가가 0이면 과거 계약단가로 대체하지 않습니다."
    )


def _result(params: Optional[dict[str, Any]], *, mode: str, action: str) -> dict[str, Any]:
    qparams = normalize_rddbc070_params(params, mode=mode)
    limits = qparams.pop("_limit_contract")
    display_top = int(qparams["display_top"])
    df = get_rddbc070_current_df(qparams) if mode == "current" else get_rddbc070_history_df(qparams)
    summary = _query_summary(qparams, mode=mode, row_count=len(df), df=df)
    payload = build_feature_result(
        table=TABLE,
        title=action,
        params=qparams,
        df=df,
        summary_md=summary,
    )
    meta = dict(payload.get("meta") or {})
    display_df = df.head(display_top).copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    full_source_limit = int(limits.source_top)
    full_source_limit_hit = bool(
        isinstance(df, pd.DataFrame) and len(df) >= full_source_limit
    )
    payload["df"] = df
    payload["df_display"] = display_df
    payload["records"] = display_df.to_dict(orient="records")
    payload["columns"] = list(display_df.columns)
    meta.update(
        {
            "source_table": "Rddbc070",
            "source_mode": mode,
            "query_summary": summary.split("\n", 1)[0].replace("조회조건: ", ""),
            "condition": summary.split("\n", 1)[0].replace("조회조건: ", ""),
            "effective_date_strategy": "latest_start_date_lte_as_of" if mode == "current" else "history",
            "candidate_data_policy": "exclude_trim_empty_cost_apply_zero_start_date_and_all_six_prices_zero",
            "zero_price_contract": "partial_zero_preserved_selected_price_zero_no_revive",
            "required_result_columns": [
                "조회순번", "단가적용거래처", "제품코드", "계약시작일자",
                "제약사", "제품그룹", "제품구분코드", "제품구분", "제품분류", "보험단가"
            ],
            "row_number_source": "result_display_projection",
            "display_row_count": int(len(display_df)),
            "row_count": int(len(display_df)),
            "row_count_total": int(len(df)),
            "full_source_ready": True,
            "full_source_bounded": True,
            "full_source_limit_rows": full_source_limit,
            "full_source_limit_source": limits.source_limit_source,
            "export_limit_rows": int(limits.export_limit_rows),
            "source_safety_limit_rows": int(limits.source_safety_limit_rows),
            "full_source_limit_hit": full_source_limit_hit,
            "full_source_truncated": "unverified" if full_source_limit_hit else False,
            "display_top": display_top,
            "display_limit_rows": int(limits.display_limit_rows),
            "display_limit_source": limits.display_limit_source,
            "requested_display_top": limits.requested_display_top,
            "limit_policy": "common_display_export_safety",
        }
    )
    payload["meta"] = meta
    return payload


def get_rddbc070_history_result(params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return _result(params, mode="history", action=HISTORY_ACTION)


def get_rddbc070_current_result(params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return _result(params, mode="current", action=CURRENT_ACTION)


def _export(params: Optional[dict[str, Any]], *, mode: str) -> pd.DataFrame:
    qparams = dict(params or {})
    qparams.pop("top", None)
    qparams.pop("display_top", None)
    qparams["_display_context"] = "chat"
    return get_rddbc070_current_df(qparams) if mode == "current" else get_rddbc070_history_df(qparams)


def get_rddbc070_history_export_df(params: Optional[dict[str, Any]] = None) -> pd.DataFrame:
    return _export(params, mode="history")


def get_rddbc070_current_export_df(params: Optional[dict[str, Any]] = None) -> pd.DataFrame:
    return _export(params, mode="current")
