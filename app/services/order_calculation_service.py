"""Read-only source adapter and full-result assembly for ordering calculation."""

from __future__ import annotations

from datetime import date, timedelta, datetime
from calendar import monthrange
from decimal import Decimal, InvalidOperation
import hashlib
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from collections import Counter

import pandas as pd

from app.db.mssql_client import get_current_company_id, query_to_df, read_only_request
from app.services.order_calculation_contract import (
    OrderConditions, amounts, calculate_quantities, closing_day_relation, demand_for_dates, demand_trend_adjustment,
    horizon_dates, infer_order_unit,
)

ACTION = "발주 계산"
TABLE = "order_calculation"
ROW_KEY = ("회사", "발주일자", "발주처코드", "제품코드")
log = logging.getLogger("ssai")
_source_measurement = ContextVar('order_source_measurement', default=None)
_CANONICAL_RESULT_COLUMNS = (
    ("제품코드", "제품코드"),
    ("발주담당자", "발주담당자"),
    ("제약담당자", "제약담당자"),
    ("대표매입처코드", "발주처코드"),
    ("단가적용처", "단가적용처"),
    ("재고적용처", "재고적용처"),
    ("예상수요", "적용 필요예정수량"),
    ("현재고", "재고수량"),
    ("미입고수량", "입고예정수량"),
    ("권장발주수량", "추천 발주수량"),
    ("발주단가", "발주단가"),
    ("발주금액", "발주금액(부가세포함)"),
)
_EVIDENCE_EXCLUDED_PARAM_TOKENS = ("password", "비밀번호", "jumin", "주민", "credential")
_EVIDENCE_STAGE_LABELS = {
    "snapshot_master": "snapshot/master", "master": "master", "representative_vendor": "representative_vendor",
    "representative_vendor_scope_authority": "representative_vendor_scope_authority",
    "early_scope": "early_scope", "forecast_stock": "forecast/stock", "current_customer_source": "current_customer",
    "order_history_unit_source": "order_history_unit",
    "order_history_3m_source": "order_history_3m", "order_history_1y_source": "order_history_1y",
    "pending_four_business_days": "pending",
    "contract_prices": "contract_price", "final_assembly": "final_assembly",
}


def order_calculation_performance_summary(timings: Mapping[str, Any] | None) -> dict[str, float]:
    """Return stable, user-facing phase totals without changing calculation flow."""
    values = dict(timings or {})

    def elapsed(key: str) -> float:
        try:
            return round(float(values.get(key) or 0), 3)
        except (TypeError, ValueError):
            return 0.0

    return {
        "snapshot_ms": elapsed("snapshot_master"),
        "representative_vendor_ms": elapsed("representative_vendor"),
        "forecast_stock_ms": elapsed("forecast_stock"),
        "current_customer_ms": elapsed("current_customer_source") + elapsed("current_customer_mapping"),
        "history_ms": (
            elapsed("order_history_unit_source")
            + elapsed("order_history_3m_source")
            + elapsed("order_history_1y_source")
        ),
        "pending_ms": elapsed("pending_four_business_days"),
        "r230_ms": elapsed("prices_code_names"),
        "contract_ms": elapsed("contract_prices"),
        "post_source_assembly_ms": elapsed("post_source_total"),
    }


def _normalized_product_codes(frame: Any, column: str = "제품코드") -> list[str]:
    if not isinstance(frame, pd.DataFrame) or column not in frame:
        return []
    return sorted({str(value).strip() for value in frame[column].fillna("") if str(value).strip()})


def _canonical_scalar(value: Any) -> Any:
    if value is None or (not isinstance(value, (list, dict, tuple)) and pd.isna(value)):
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, float):
        return format(value, ".15g")
    if isinstance(value, (str, int, bool)):
        return value.strip() if isinstance(value, str) else value
    return str(value).strip()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_evidence_params(params: Mapping[str, Any]) -> dict[str, Any]:
    safe = {}
    for key, value in params.items():
        key_text = str(key)
        key_lower = key_text.lower()
        if key_text.startswith("_") or any(token in key_lower or token in key_text for token in _EVIDENCE_EXCLUDED_PARAM_TOKENS):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[key_text] = _canonical_scalar(value)
        elif isinstance(value, (list, tuple)):
            safe[key_text] = [_canonical_scalar(item) for item in value]
    return safe


def _safe_evidence_question(value: Any) -> str:
    question = str(value or "").strip()
    lowered = question.lower()
    return "[redacted]" if any(token in lowered or token in question for token in _EVIDENCE_EXCLUDED_PARAM_TOKENS) else question


def _scope_evidence(params: Mapping[str, Any]) -> dict[str, str]:
    scopes = []
    for key, kind in (("order_staff_nm", "order_staff"), ("pharma_staff_nm", "pharma_staff")):
        value = str(params.get(key) or "").strip()
        if value:
            scopes.append((kind, value))
    return {
        "kind": "+".join(kind for kind, _ in scopes) or "none",
        "value": "+".join(value for _, value in scopes),
    }


def _canonical_result_evidence(frame: Any) -> dict[str, Any]:
    records = []
    if isinstance(frame, pd.DataFrame):
        for source in frame.to_dict("records"):
            records.append({label: _canonical_scalar(source.get(column)) for label, column in _CANONICAL_RESULT_COLUMNS})
    records.sort(key=lambda row: tuple(str(row.get(key) or "") for key, _ in _CANONICAL_RESULT_COLUMNS))
    return {
        "columns": [label for label, _ in _CANONICAL_RESULT_COLUMNS],
        "row_count": len(records),
        "sha256": hashlib.sha256(_canonical_json(records).encode("utf-8")).hexdigest(),
        "rows": records,
    }


def _snapshot_evidence(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    meta = dict(snapshot.get("meta") or {}) if isinstance(snapshot, Mapping) else {}
    return {
        "status": str(meta.get("snapshot_status") or ""),
        "reason": str(meta.get("snapshot_reason") or ""),
        "manifest_id": meta.get("snapshot_manifest_id", meta.get("manifest_id")),
        "generation_no": meta.get("snapshot_generation_no", meta.get("generation_no")),
        "evaluation_month": str((snapshot.get("params") or {}).get("evaluation_month") or "") if isinstance(snapshot, Mapping) else "",
    }


def build_manual_validation_evidence(
    *,
    params: Mapping[str, Any],
    sources: Mapping[str, Any] | None,
    result_frame: Any,
    measurement: Mapping[str, Any] | None,
    elapsed_ms: float,
    result_status: str,
    error: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    source_data = dict(sources or {})
    candidate_codes = list(source_data.get("diagnostic_candidate_product_codes") or _normalized_product_codes(source_data.get("base")))
    scoped_codes = list(source_data.get("diagnostic_scoped_product_codes") or candidate_codes)
    final_codes = _normalized_product_codes(result_frame)
    sets = {}
    for name, codes in (("candidate_product_codes", candidate_codes), ("scoped_product_codes", scoped_codes), ("final_result_product_codes", final_codes)):
        normalized = sorted({str(code).strip() for code in codes if str(code).strip()})
        sets[name] = {"count": len(normalized), "sha256": hashlib.sha256(_canonical_json(normalized).encode("utf-8")).hexdigest(), "codes": normalized}
    snapshot = source_data.get("snapshot", {})
    snapshot_detail = _snapshot_evidence(snapshot)
    if not snapshot_detail["evaluation_month"]:
        snapshot_detail["evaluation_month"] = str(params.get("order_date") or "")[:7].replace("-", "")
    return {
        "schema_version": "order_calculation_manual_validation_v1",
        "company_id": params.get("company_id"),
        "question": _safe_evidence_question(params.get("_original_question")),
        "parsed_params": _safe_evidence_params(params),
        "scope": _scope_evidence(params),
        "order_date": str(params.get("order_date") or ""),
        "evaluation_month": str(params.get("order_date") or "")[:7].replace("-", ""),
        "result_status": result_status,
        "result_rows": len(result_frame) if isinstance(result_frame, pd.DataFrame) else 0,
        "source_call_count": len((measurement or {}).get("queries") or []),
        "total_elapsed_ms": round(float(elapsed_ms), 1),
        "product_sets": sets,
        "canonical_result": _canonical_result_evidence(result_frame),
        "stages": dict(source_data.get("diagnostic_stage_evidence") or {}),
        "snapshot": snapshot_detail,
        "error": dict(error or {}),
    }


def write_manual_validation_evidence(params: Mapping[str, Any], evidence: Mapping[str, Any]) -> str:
    """Persist a safe comparison artifact without letting diagnostics affect the query."""
    root = str(params.get("_order_calculation_diagnostic_dir") or os.getenv("ORDER_CALC_DIAGNOSTIC_DIR") or "logs/order_calculation_diagnostics")
    try:
        artifact_dir = Path(root)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        digest = str(evidence.get("canonical_result", {}).get("sha256") or "noresult")[:12]
        company = str(evidence.get("company_id") or "unknown")
        filename = f"order_calculation_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_company{company}_{digest}.json"
        path = artifact_dir / filename
        path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return str(path)
    except Exception as exc:
        log.warning("[order.calc.perf] diagnostic_artifact_write_failed=%s", type(exc).__name__)
        return ""


def apply_application_defaults(params):
    q = dict(params)
    for field, code in (("cost_apply", "50002"), ("stock_apply", "50001")):
        if not str(q.get(field + "_cd") or "").strip() and not str(q.get(field + "_nm") or "").strip():
            q[field + "_cd"] = code
    explicit_mode = str(q.get("query_mode") or "").strip()
    if explicit_mode:
        q["query_mode"] = explicit_mode
    elif "only_needed" in q:
        q["query_mode"] = "발주해당자료만" if bool(q.get("only_needed")) else "전체"
    else:
        q["query_mode"] = "발주해당자료만"
    q["only_needed"] = q["query_mode"] == "발주해당자료만"
    return q


@contextmanager
def _phase(timings, stage, company_id):
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        timings[stage] = timings.get(stage, 0) + elapsed
        log.info("[order_calculation.perf] company_id=%s stage=%s elapsed_ms=%s",
                 company_id, stage, elapsed)


def _groups(frame, columns):
    if frame is None or frame.empty:
        return {}
    prepared = frame.copy()
    for column in columns:
        prepared[column] = prepared[column].astype(str).str.strip()
    return {key: group for key, group in prepared.groupby(columns, sort=False)}


def _history_index(frame):
    if frame is None or frame.empty:
        return {}
    grouped = {}
    for row in frame[['제품코드', '발주거래처코드', '발주일자', '발주수량']].to_dict('records'):
        key = (str(row['제품코드']).strip(), str(row['발주거래처코드']).strip())
        grouped.setdefault(key, []).append(row)
    return grouped


def _positive_decimal(value: Any) -> Decimal | None:
    amount = decimal_or_none(value)
    return amount if amount is not None and amount > 0 else None


def _latest_order_price_by_window(
    frame: pd.DataFrame,
    *,
    reference: date,
    months: int,
) -> dict[tuple[str, str], Decimal | None]:
    """Select one deterministic positive R170/R180 unit cost per product/application."""
    return _latest_order_prices_by_windows(
        frame, reference=reference, months=(months,)
    ).get(months, {})


def _latest_order_prices_by_windows(
    frame: pd.DataFrame,
    *,
    reference: date,
    months: tuple[int, ...],
) -> dict[int, dict[tuple[str, str], Decimal | None]]:
    """Derive deterministic order-price maps from one normalized, sorted history frame."""
    required = {
        "제품코드", "단가적용처코드", "발주일자", "발주거래처코드",
        "발주순번", "상세순번", "단가",
    }
    windows = tuple(sorted({int(month) for month in months if int(month) > 0}))
    if not windows:
        return {}
    empty = {month: {} for month in windows}
    if not isinstance(frame, pd.DataFrame) or frame.empty or not required.issubset(frame.columns):
        return empty
    earliest_start = (pd.Timestamp(reference) - pd.DateOffset(months=max(windows))).strftime("%Y%m%d")
    optional = {"발주상태코드"} if "발주상태코드" in frame else set()
    work = frame.loc[:, list(required | optional)].copy()
    for column in ("제품코드", "단가적용처코드", "발주일자", "발주거래처코드"):
        work[column] = work[column].fillna("").astype(str).str.strip()
    work["__price"] = work["단가"].map(_positive_decimal)
    work = work.loc[
        work["제품코드"].ne("")
        & work["단가적용처코드"].ne("")
        & work["발주일자"].str.fullmatch(r"\d{8}", na=False)
        & work["발주일자"].between(earliest_start, reference.strftime("%Y%m%d"))
        & work["__price"].notna()
    ].copy()
    if "발주상태코드" in work:
        work = work.loc[work["발주상태코드"].fillna("").astype(str).str.strip().isin({"1", "2", "3"})].copy()
    if work.empty:
        return empty
    grain = ["발주일자", "발주거래처코드", "발주순번", "상세순번"]
    work = work.sort_values(
        ["발주일자", "발주거래처코드", "발주순번", "상세순번"],
        ascending=[False, True, True, True],
        kind="stable",
    )
    maps: dict[int, dict[tuple[str, str], Decimal | None]] = {}
    for month in windows:
        start = (pd.Timestamp(reference) - pd.DateOffset(months=month)).strftime("%Y%m%d")
        window = work.loc[work["발주일자"].ge(start)].copy()
        duplicated = window.duplicated(grain, keep=False)
        ambiguous_keys = set(zip(window.loc[duplicated, "제품코드"], window.loc[duplicated, "단가적용처코드"]))
        selected = window.drop_duplicates(["제품코드", "단가적용처코드"], keep="first")
        prices = {
            (str(row["제품코드"]), str(row["단가적용처코드"])): row["__price"]
            for row in selected.to_dict("records")
        }
        for key in ambiguous_keys:
            prices[key] = None
        maps[month] = prices
    return maps


def _first_positive_price(row: Mapping[str, Any], columns: tuple[str, ...]) -> Decimal | None:
    for column in columns:
        price = _positive_decimal(row.get(column))
        if price is not None:
            return price
    return None


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None or pd.isna(value):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def _master(params):
    from app.services.product_master_filter_contract import (
        add_named_management_only_exclusion,
        append_product_master_filter_clauses,
        build_product_master_enrichment_sql,
    )
    joins, expressions = build_product_master_enrichment_sql(product_alias="P", include_audit_price=False)
    clauses, binds = [], []
    append_product_master_filter_clauses(clauses, binds, params, expressions=expressions)
    add_named_management_only_exclusion(
        clauses,
        product_code_expression="P.Rd04_Physic_Cd",
    )
    if params.get("physic_nm"):
        clauses.append("P.Rd04_Physic_Nm LIKE ?")
        binds.append(f"%{params['physic_nm']}%")
    if params.get("maker_cd"):
        clauses.append("P.Rd04_Ven_Cd = ?")
        binds.append(params["maker_cd"])
    return query_to_df(f"""SELECT RTRIM(P.Rd04_Physic_Cd) AS [제품코드],
        P.Rd04_Physic_Nm AS [제품명], PV.Rd03_Ven_Nm AS [제약사],
        P.Rd04_Standard AS [규격], P.Rd04_In_Real_Unit_Cost AS [제품등록실단가],
        P.Rd04_In_Acc_Unit_Cost AS [제품등록장부단가]
        FROM dbo.Rddbc040 P WITH (NOLOCK) {joins}
        WHERE {' AND '.join(clauses) if clauses else '1=1'}""", tuple(binds))


def build_demand_params(source_params: dict, scope) -> dict:
    demand_params = dict(source_params)
    for key in ("product_group", "product_di", "product_class"):
        demand_params["dashboard_" + key + "_list"] = demand_params.pop(key + "_list", [])
    demand_params["product_ven_nm"] = source_params.get("maker_nm", "")
    demand_params["product_ven_cd"] = source_params.get("maker_cd", "")
    demand_params["io_gu_list"] = [c for c in scope.io_gu_codes if len(str(c)) == 3 and "500" <= str(c) <= "599"]
    if not demand_params["io_gu_list"]:
        raise ValueError("저장조건에 정상출고 수요 코드가 없습니다.")
    demand_params["_order_product_month_forecast"] = True
    return demand_params


def build_pending_params(source_params: dict) -> dict:
    pending = {**source_params, "_today": source_params["policy_date"],
               "include_blank_stock_cd": True}
    pending.pop("date_to", None)
    pending.pop("date_from", None)
    # Order-vendor filters belong to the selected representative supplier, not
    # to outstanding orders already placed with potentially different vendors.
    pending.pop("order_vendor_cd", None)
    pending.pop("order_vendor_nm", None)
    return pending


def build_order_price_history_params(source_params: dict, *, reference: date) -> dict:
    """Keep price-history authority to product/application, not the requesting staff."""
    history = build_pending_params(source_params)
    # Staff scope selects the products to calculate.  Historical purchase prices
    # remain valid for the selected product/application even when an earlier
    # document was entered by a different staff member.
    history.pop("order_staff_nm", None)
    history.pop("pharma_staff_nm", None)
    history.pop("stock_apply_cd", None)
    history.pop("stock_apply_nm", None)
    history.pop("stock_cd", None)
    history.pop("stock_nm", None)
    history.pop("stock_cd_list", None)
    history.update({
        "date_from": (pd.Timestamp(reference) - pd.DateOffset(months=12)).strftime("%Y%m%d"),
        "date_to": reference.strftime("%Y%m%d"),
        "status_codes": ["1", "2", "3"],
        "_order_price_history": True,
    })
    return history


def build_order_unit_history_params(source_params: dict, *, reference: date) -> dict:
    """Preserve the established one-month R170/R180 source for order-unit inference."""
    history = build_pending_params(source_params)
    # Staff scope selects the products being calculated.  The historical
    # ordering unit remains valid regardless of which staff member entered an
    # earlier order, exactly like the price-history authority below.
    history.pop("order_staff_nm", None)
    history.pop("pharma_staff_nm", None)
    history.update({
        "date_from": (pd.Timestamp(reference) - pd.DateOffset(months=1)).strftime("%Y%m%d"),
        "date_to": reference.strftime("%Y%m%d"),
        "status_codes": ["1", "2", "3"],
        "_order_unit_history_minimal": True,
    })
    return history


def build_order_price_history_window_params(
    source_params: dict,
    *,
    date_from: date,
    date_to: date,
) -> dict:
    """Build one R170/R180-only history query; product scope is applied in Python."""
    history = build_order_price_history_params(source_params, reference=date_to)
    history.update({
        "date_from": date_from.strftime("%Y%m%d"),
        "date_to": date_to.strftime("%Y%m%d"),
        "_order_price_history_minimal": True,
    })
    return history


def _filter_order_price_history_products(frame: pd.DataFrame, product_codes: list[str]) -> pd.DataFrame:
    """Apply the already-authoritative selected product universe after the minimal SQL read."""
    if not isinstance(frame, pd.DataFrame) or frame.empty or "제품코드" not in frame:
        return frame
    selected = {str(code).strip() for code in product_codes if str(code).strip()}
    if not selected:
        return frame.iloc[0:0].copy()
    normalized_codes = frame["제품코드"].fillna("").astype(str).str.strip()
    return frame.loc[normalized_codes.isin(selected)].copy()


def _contract_purchase_price_map(frame: pd.DataFrame) -> dict[tuple[str, str], Decimal]:
    """Return only unambiguous positive R070 purchase prices by product/application."""
    result: dict[tuple[str, str], Decimal] = {}
    for (application, product), candidates in _groups(frame, ["단가적용거래처", "제품코드"]).items():
        if len(candidates) != 1:
            continue
        price = _first_positive_price(candidates.iloc[0], ("실입고단가", "장부입고단가", "otc 입고단가"))
        if price is not None:
            result[(str(product).strip(), str(application).strip())] = price
    return result


def _unresolved_order_price_codes(
    product_codes: list[str],
    *,
    cost_apply_code: str,
    contract_prices: Mapping[tuple[str, str], Decimal],
    order_price_3m: Mapping[tuple[str, str], Decimal | None],
) -> list[str]:
    """Return only products that still need the older one-year price window."""
    if not cost_apply_code:
        return []
    return [
        code for code in product_codes
        if (code, cost_apply_code) not in contract_prices
        and order_price_3m.get((code, cost_apply_code)) is None
    ]


def build_contract_price_params(params: dict) -> dict:
    # Order/forecast periods must not become a contract-start-date filter.
    query = {k: v for k, v in params.items() if k not in
             ('date_from', 'date_to', 'month_from', 'month_to')}
    query.update(ven_cd=params.get('cost_apply_cd'),
                 ven_nm=None if params.get('cost_apply_cd') else params.get('cost_apply_nm'),
                 as_of=date.fromisoformat(params['order_date']).strftime('%Y%m%d'))
    query['_order_contract_projection_only'] = True
    return query


def load_sources(params: dict) -> dict:
    from app.services.snapshot_product_information_service import get_snapshot_product_information_result
    from app.services.dashboard_inventory_frequency_snapshot_service import resolve_dashboard_profile_stock_scope
    from app.services.analytics_sales_trend_service import get_stock_shortage_df
    from app.services.dashboard_inbound_facts_service import (
        get_dashboard_inbound_facts, get_dashboard_inbound_scope_authority,
    )
    from app.services.rddbc170_rddbc180_order_service import get_expected_inbound_product_totals
    from app.services.business_calendar_service import business_day_month_context, next_business_day_on_or_after
    from app.services.ssai_business_calendar_repository import load_official_holidays
    from app.services.ssai_analysis_profile_service import load_dashboard_profile_checked, normalize_company_default_conditions

    input_reference = date.fromisoformat(params["order_date"])
    effective_business_day = next_business_day_on_or_after(input_reference)
    reference = (
        date.fromisoformat(effective_business_day.effective_date)
        if effective_business_day.status == "ready" else input_reference
    )
    source_request = {**params, "order_date": reference.isoformat()}
    timings = {}
    stage_evidence: dict[str, dict[str, Any]] = {}
    def call(stage, function, *a, input_rows: int | None = None, **kw):
        measurement = _source_measurement.get()
        before = len(measurement['queries']) if measurement is not None else 0
        with _phase(timings, stage, params['company_id']):
            value = function(*a, **kw)
        queries = measurement['queries'][before:] if measurement is not None else []
        physical_queries = [
            {
                "query_index": before + index + 1,
                "tables": [
                    name for name in str(record.get("tables") or "").split(",") if name
                ],
                "rows": record.get("rows"),
                "elapsed_ms": record.get("elapsed_ms"),
                "status": record.get("status"),
            }
            for index, record in enumerate(queries)
        ]
        for record in queries:
            if record.get('tables') == ['Rddbc010']:
                timings['code_names_sql'] = record.get('elapsed_ms', 0)
        sql_ms = sum(r.get('elapsed_ms', 0) for r in queries)
        timings[stage + '_sql'] = sql_ms
        timings[stage + '_python_other'] = max(0, timings[stage] - sql_ms)
        result_frame = value.get("df") if isinstance(value, Mapping) else value
        stage_evidence[stage] = {
            "label": _EVIDENCE_STAGE_LABELS.get(stage, stage),
            "elapsed_ms": round(timings[stage], 3),
            "sql_elapsed_ms": round(sql_ms, 3),
            "python_elapsed_ms": round(timings[stage + '_python_other'], 3),
            "input_rows": input_rows,
            "rows": len(result_frame) if isinstance(result_frame, pd.DataFrame) else None,
            "source_call_count": len(queries),
            "physical_queries": physical_queries,
        }
        log.info('[order_calculation.perf] company_id=%s stage=%s calls=%s sql_ms=%s python_other_ms=%s',
                 params['company_id'], stage, len(queries), sql_ms, timings[stage + '_python_other'])
        if physical_queries:
            log.info(
                '[order_calculation.query] company_id=%s stage=%s physical_queries=%s',
                params['company_id'], stage, physical_queries,
            )
        return value
    codes = [params.get(field + "_cd") for field in ("cost_apply", "stock_apply") if params.get(field + "_cd")]
    if codes:
        applications = call('application_codes', query_to_df,
            "SELECT RTRIM(Rd03_Ven_Cd) AS code, RTRIM(Rd03_Ven_Nm) AS name FROM dbo.Rddbc030 WHERE Rd03_Ven_Cd IN (" + ','.join('?' for _ in codes) + ')', tuple(codes), input_rows=len(codes))
        names = dict(zip(applications['code'], applications['name']))
        for field in ("cost_apply", "stock_apply"):
            code = params.get(field + "_cd")
            if code and code not in names:
                raise ValueError(f"회사 {params['company_id']}에 적용처 코드 {code}가 없습니다. 대체하지 않았습니다.")
            if code:
                params[field + "_nm"] = names[code]
    scope = resolve_dashboard_profile_stock_scope(company_id=params["company_id"])
    if params.get("stock_cd") and params["stock_cd"] not in scope.stock_codes:
        raise ValueError("저장된 재고범위 밖의 재고위치는 계산할 수 없습니다.")
    source_params = {**source_request, "evaluation_month": reference.strftime("%Y%m"),
                     "policy_date": reference.strftime("%Y%m%d"), "today": reference.strftime("%Y%m%d"),
                     "date_to": reference.strftime("%Y%m%d"), "stock_cd_list": list(scope.stock_codes),
                     "product_group_list": list(scope.product_group_codes),
                     "product_di_list": list(scope.product_di_codes),
                     "product_class_list": list(scope.product_class_codes), "stock_mode": scope.stock_mode}
    source_params["io_gu_list"] = list(scope.io_gu_codes)
    source_params["_require_company_io"] = True
    for field in ("cost_apply", "stock_apply"):
        if source_params.get(field + "_cd"):
            source_params.pop(field + "_nm", None)
    def master(query):
        return call('master', _master, query)
    snapshot = call('snapshot_master', get_snapshot_product_information_result,
        {**source_request, "evaluation_month": reference.strftime("%Y%m")}, master_loader=master,
        apply_viewer_projection=False, materialize_presentation=False, input_rows=0)
    if snapshot.get("meta", {}).get("snapshot_status") != "ready":
        return {"snapshot": snapshot}
    frame = snapshot.get("df", pd.DataFrame())
    if frame.empty:
        return {"snapshot": snapshot, "base": frame}
    candidate_codes = _normalized_product_codes(frame)
    profile = load_dashboard_profile_checked(company_id=params["company_id"])
    defaults = normalize_company_default_conditions(profile.profile)
    vendor_lookback_days = int(defaults.get("major_purchase_vendor_days", 90))
    scope_started = time.perf_counter()
    scope_authority = None
    if order_scope_active(params):
        scope_authority = call(
            'representative_vendor_scope_authority', get_dashboard_inbound_scope_authority,
            source_params, data_cutoff_date=reference.strftime("%Y%m%d"),
            vendor_lookback_days=vendor_lookback_days, input_rows=len(candidate_codes),
        )
        frame = filter_base_by_order_scope(frame, scope_authority, params)
    selected_codes = _normalized_product_codes(frame)
    timings['representative_vendor_scope_filter'] = (time.perf_counter() - scope_started) * 1000
    stage_evidence['early_scope'] = {
        "label": _EVIDENCE_STAGE_LABELS['early_scope'],
        "elapsed_ms": round(timings['representative_vendor_scope_filter'], 3),
        "input_rows": len(candidate_codes), "rows": len(selected_codes), "source_call_count": 0,
    }
    if not selected_codes:
        return {
            "snapshot": snapshot, "base": frame, "demand": pd.DataFrame(),
            "suppliers": scope_authority if isinstance(scope_authority, pd.DataFrame) else pd.DataFrame(),
            "pending": pd.DataFrame(), "prices": {"df": pd.DataFrame(), "meta": {}}, "contracts": {},
            "business_dates": [], "calendar_status": "not_needed", "elapsed_days": 0,
            "normal_outbound_verified": True, "source_params": source_params,
            "current_customer_counts": {}, "order_history": pd.DataFrame(), "order_history_complete": True,
            "performance_ms": timings, "order_history_period": [],
            "diagnostic_candidate_product_codes": candidate_codes,
            "diagnostic_scoped_product_codes": selected_codes,
            "diagnostic_stage_evidence": stage_evidence,
            "input_order_date": input_reference.isoformat(),
            "effective_business_date": reference.isoformat(),
            "business_date_shifted": effective_business_day.shifted,
            "business_date_shift_days": effective_business_day.shift_days,
            "effective_business_date_status": effective_business_day.status,
            "business_date_authority": effective_business_day.authority,
        }
    inbound_params = {**source_params, "inbound_product_code_list": selected_codes}
    if order_scope_active(params):
        # The product-grain compact authority already resolved the winning
        # R110 vendor and both staff roles.  Re-reading 365-day detail after
        # early scope only recreates the same order fields at much higher cost.
        selected_set = set(selected_codes)
        supplier_started = time.perf_counter()
        suppliers = scope_authority.loc[
            scope_authority["product_code"].fillna("").astype(str).str.strip().isin(selected_set)
        ].copy()
        suppliers.attrs.update(dict(getattr(scope_authority, "attrs", {}) or {}))
        suppliers.attrs["inbound_product_scope_count"] = len(selected_codes)
        suppliers.attrs["inbound_product_scope_sql_mode"] = "reused_profile_compact_authority"
        suppliers.attrs["inbound_product_scope_bind_count"] = 0
        timings['representative_vendor'] = (time.perf_counter() - supplier_started) * 1000
        stage_evidence['representative_vendor'] = {
            "label": _EVIDENCE_STAGE_LABELS['representative_vendor'],
            "elapsed_ms": round(timings['representative_vendor'], 3),
            "rows": len(suppliers),
            "source_call_count": 0,
        }
    else:
        suppliers = call('representative_vendor', get_dashboard_inbound_scope_authority, inbound_params,
            data_cutoff_date=reference.strftime("%Y%m%d"),
            vendor_lookback_days=vendor_lookback_days, input_rows=len(selected_codes))
    if isinstance(suppliers, pd.DataFrame):
        stage_evidence['representative_vendor'].update({
            "input_rows": len(candidate_codes),
            "source_rows": int(suppliers.attrs.get("inbound_source_rows", 0)),
            "query_elapsed_ms": int(suppliers.attrs.get("inbound_query_elapsed_ms", 0)),
            "product_scope_count": int(suppliers.attrs.get("inbound_product_scope_count", 0)),
            "scope_sql_mode": suppliers.attrs.get("inbound_product_scope_sql_mode", "none"),
            "scope_bind_count": int(suppliers.attrs.get("inbound_product_scope_bind_count", 0)),
        })
    if order_scope_active(params) and _normalized_product_codes(suppliers, "product_code") != selected_codes:
        raise ValueError("대표매입처 compact authority와 선택 제품 집합이 일치하지 않습니다.")
    if order_scope_active(params):
        source_params["order_product_code_list"] = selected_codes
    timings['representative_vendor_scope_product_count'] = len(selected_codes)
    # No new forecast model; the existing service supplies stock and quantity forecast together.
    # Saved classification is R040 Tax scope, not the legacy Physic_Gu filter.
    demand_params = build_demand_params(source_params, scope)
    demand = call('forecast_stock', get_stock_shortage_df, demand_params, product_universe_df=frame[["제품코드"]], input_rows=len(selected_codes))
    for key in (
        'stock_sql_ms', 'stock_aggregate_ms', 'stock_shortage_build_ms', 'stock_shortage_total_ms',
        'forecast_source_rows', 'forecast_source_sql_ms', 'forecast_source_query_count',
    ):
        timings[key] = demand.attrs.get(key, 0)
    timings['forecast_source_representation'] = demand.attrs.get('forecast_source_representation', 'legacy')
    from app.services.analytics_sales_trend_service import get_outbound_customer_counts_df
    from app.services.rddbc170_rddbc180_order_service import get_order_df, normalize_order_params
    current_customer_counts = call('current_customer_source', get_outbound_customer_counts_df, {**demand_params,
        'date_from': reference.replace(day=1).strftime('%Y%m%d'),
        'date_to': reference.strftime('%Y%m%d')}, input_rows=len(selected_codes))
    processing_started = time.perf_counter()
    customer_counts = (
        current_customer_counts.set_index('제품코드')['거래처수'].astype(int).to_dict()
        if not current_customer_counts.empty else {}
    )
    timings['current_customer_mapping'] = (time.perf_counter() - processing_started) * 1000
    contracts = {}
    if params.get("cost_apply_cd") or params.get("cost_apply_nm"):
        from app.services.rddbc070_service import get_rddbc070_current_result
        contracts = call('contract_prices', get_rddbc070_current_result, build_contract_price_params(source_params), input_rows=len(selected_codes))
        contract_profile = dict(
            (contracts.get("df").attrs.get("order_contract_price_profile") or {})
            if isinstance(contracts, Mapping) and isinstance(contracts.get("df"), pd.DataFrame) else {}
        )
        if contract_profile:
            timings.update({
                f"contract_prices_{key}": value
                for key, value in contract_profile.items()
                if isinstance(value, (int, float))
            })
            contract_detail_ms = {
                "contract_prices_sql": contract_profile.get("r070_sql_fetch_ms", 0),
                "contract_prices_normalize": contract_profile.get("result_dataframe_normalize_ms", 0),
                "contract_prices_dedupe": contract_profile.get("latest_duplicate_handling_ms", 0),
                # R070 has no independent code/name or cost-apply join. Those
                # remain in the existing merge_mapping phase after all sources load.
                "contract_prices_map_build": 0.0,
                "contract_prices_merge": 0.0,
                "contract_prices_finalize": contract_profile.get("final_price_dataframe_ms", 0),
            }
            timings.update(contract_detail_ms)
            stage_evidence["contract_prices"].update({
                "r070_profile": contract_profile,
                "unique_key_count": contract_profile.get("unique_key_count"),
                "duplicate_count": contract_profile.get("latest_duplicate_count"),
                "dataframe_shape": contract_profile.get("output_shape"),
                "detail_elapsed_ms": contract_detail_ms,
            })
            log.info(
                "[order_calculation.perf] company_id=%s stage=contract_prices_detail %s",
                params["company_id"],
                contract_detail_ms,
            )
    contract_prices = _contract_purchase_price_map(contracts.get("df", pd.DataFrame()))
    cost_apply_code = str(params.get("cost_apply_cd") or "").strip()
    unit_history_params = build_order_unit_history_params(source_params, reference=reference)
    order_history_unit = call(
        'order_history_unit_source', get_order_df, unit_history_params,
        mode='order', input_rows=len(selected_codes),
    )
    order_history_unit_complete = len(order_history_unit) < normalize_order_params(
        unit_history_params, mode='order'
    )['top']
    if (
        not order_history_unit.empty
        and order_history_unit.duplicated(['발주일자', '발주거래처코드', '발주순번', '상세순번']).any()
    ):
        order_history_unit_complete = False
    if isinstance(order_history_unit, pd.DataFrame):
        stage_evidence['order_history_unit_source'].update({
            'query_mode': order_history_unit.attrs.get('order_history_query_mode', 'registered_order_display'),
            'dataframe_shape': list(order_history_unit.shape),
        })
    unresolved_after_contract = _unresolved_order_price_codes(
        selected_codes,
        cost_apply_code=cost_apply_code,
        contract_prices=contract_prices,
        order_price_3m={},
    )
    three_month_start = (pd.Timestamp(reference) - pd.DateOffset(months=3)).date()
    history_3m_params = build_order_price_history_window_params(
        source_params,
        date_from=three_month_start,
        date_to=reference,
    )
    history_3m_params['order_price_product_code_list'] = unresolved_after_contract
    order_history_3m_source = (
        call(
            'order_history_3m_source', get_order_df, history_3m_params,
            mode='order', input_rows=len(unresolved_after_contract),
        )
        if unresolved_after_contract else pd.DataFrame()
    )
    history_scope_started = time.perf_counter()
    order_history_3m = _filter_order_price_history_products(order_history_3m_source, selected_codes)
    timings['order_history_3m_scope_filter'] = (time.perf_counter() - history_scope_started) * 1000
    stage_evidence.setdefault('order_history_3m_source', {
        'label': _EVIDENCE_STAGE_LABELS['order_history_3m_source'],
        'elapsed_ms': 0.0, 'rows': 0, 'source_call_count': 0,
    }).update({
        'source_rows': len(order_history_3m_source), 'scoped_rows': len(order_history_3m),
        'selected_product_count': len(selected_codes), 'unresolved_after_contract': len(unresolved_after_contract),
        'query_mode': order_history_3m_source.attrs.get('order_history_query_mode', 'skipped_no_unresolved_contract'),
        'scope_bind_count': int(order_history_3m_source.attrs.get('order_price_scope_bind_count', 0)),
        'python_other_ms': round(
            timings.get('order_history_3m_source_python_other', 0) + timings['order_history_3m_scope_filter'], 3
        ),
    })
    order_price_3m = _latest_order_prices_by_windows(
        order_history_3m, reference=reference, months=(3,),
    )[3]
    unresolved_price_codes = _unresolved_order_price_codes(
        selected_codes,
        cost_apply_code=cost_apply_code,
        contract_prices=contract_prices,
        order_price_3m=order_price_3m,
    )
    log.info(
        '[order_calculation.perf] company_id=%s stage=order_price_history_scope selected_product_count=%s '
        'unresolved_after_contract=%s unresolved_after_3m=%s',
        params['company_id'], len(selected_codes), len(unresolved_after_contract), len(unresolved_price_codes),
    )
    order_history_1y = pd.DataFrame()
    order_history_1y_source = pd.DataFrame()
    order_price_1y: dict[tuple[str, str], Decimal | None] = {}
    history_1y_params = None
    if unresolved_price_codes:
        history_1y_params = build_order_price_history_window_params(
            source_params,
            date_from=(pd.Timestamp(reference) - pd.DateOffset(months=12)).date(),
            date_to=three_month_start - timedelta(days=1),
        )
        history_1y_params['order_price_product_code_list'] = unresolved_price_codes
        order_history_1y_source = call(
            'order_history_1y_source', get_order_df, history_1y_params,
            mode='order', input_rows=len(unresolved_price_codes),
        )
        history_scope_started = time.perf_counter()
        order_history_1y = _filter_order_price_history_products(order_history_1y_source, unresolved_price_codes)
        timings['order_history_1y_scope_filter'] = (time.perf_counter() - history_scope_started) * 1000
        stage_evidence['order_history_1y_source'].update({
            'source_rows': len(order_history_1y_source), 'scoped_rows': len(order_history_1y),
            'selected_product_count': len(selected_codes),
            'unresolved_after_contract_and_3m': len(unresolved_price_codes),
            'query_mode': order_history_1y_source.attrs.get('order_history_query_mode', 'registered_order_display'),
            'scope_bind_count': int(order_history_1y_source.attrs.get('order_price_scope_bind_count', 0)),
            'python_other_ms': round(
                timings['order_history_1y_source_python_other'] + timings['order_history_1y_scope_filter'], 3
            ),
        })
        order_price_1y = _latest_order_prices_by_windows(
            order_history_1y, reference=reference, months=(12,),
        )[12]
    order_history_parts = [
        frame
        for frame in (order_history_3m, order_history_1y)
        if isinstance(frame, pd.DataFrame) and not frame.empty
    ]
    order_history = (
        pd.concat(order_history_parts, ignore_index=True)
        if order_history_parts
        else pd.DataFrame()
    )
    history_params = history_1y_params or history_3m_params
    history_source_frames = (order_history_3m_source, order_history_1y_source)
    order_price_history_complete = all(
        len(frame) < normalize_order_params(query, mode='order')['top']
        for frame, query in ((order_history_3m_source, history_3m_params), (order_history_1y_source, history_1y_params))
        if query is not None
    )
    if any(not frame.empty and frame.duplicated(['발주일자', '발주거래처코드', '발주순번', '상세순번']).any() for frame in history_source_frames):
        order_price_history_complete = False
    pending_params = build_pending_params(source_params)
    pending = call('pending_four_business_days', get_expected_inbound_product_totals, pending_params, input_rows=len(selected_codes))
    end = reference + timedelta(days=max(120, int(params["target_days"]) * 4 + 62))
    end = end.replace(day=monthrange(end.year, end.month)[1])
    authority = call('official_calendar', load_official_holidays, start_date=reference.replace(day=1), end_date=end, input_rows=0)
    business_dates = []
    if authority.status == "ready":
        cursor = reference.replace(day=1)
        while cursor <= end:
            if cursor.weekday() < 5 and cursor.strftime("%Y%m%d") not in authority.holiday_dates:
                business_dates.append(cursor)
            cursor += timedelta(days=1)
    context = business_day_month_context(evaluation_date=reference)
    calendar_status = authority.status if effective_business_day.status == "ready" else "unavailable"
    return {"snapshot": snapshot, "base": frame, "demand": demand,
            "suppliers": suppliers, "pending": pending, "contracts": contracts,
            "business_dates": business_dates, "calendar_status": calendar_status,
            "elapsed_days": context.elapsed_business_days,
            "input_order_date": input_reference.isoformat(),
            "effective_business_date": reference.isoformat(),
            "business_date_shifted": effective_business_day.shifted,
            "business_date_shift_days": effective_business_day.shift_days,
            "effective_business_date_status": effective_business_day.status,
            "business_date_authority": effective_business_day.authority,
            "normal_outbound_verified": True,
            "source_params": source_params, "current_customer_counts": customer_counts,
            "order_history": order_history, "order_history_3m": order_history_3m,
            "order_history_1y": order_history_1y,
            "order_history_unit": order_history_unit,
            "order_history_complete": order_history_unit_complete,
            "order_history_price_complete": order_price_history_complete,
            "order_history_price_truncated": not order_price_history_complete,
            "order_price_3m": order_price_3m, "order_price_1y": order_price_1y,
            "contract_purchase_prices": contract_prices,
            "performance_ms": timings,
            "order_history_period": [history_params['date_from'], history_params['date_to']],
            "order_history_3m_period": [history_3m_params['date_from'], history_3m_params['date_to']],
            "order_history_1y_period": ([history_1y_params['date_from'], history_1y_params['date_to']]
                                        if history_1y_params else []),
            "order_history_unit_period": [unit_history_params['date_from'], unit_history_params['date_to']],
            "diagnostic_candidate_product_codes": candidate_codes,
            "diagnostic_scoped_product_codes": selected_codes,
            "diagnostic_stage_evidence": stage_evidence}


def _index(frame: pd.DataFrame, column="제품코드") -> dict:
    if frame is None or frame.empty or column not in frame:
        return {}
    if frame[column].duplicated().any():
        raise ValueError(f"원천 제품 grain 충돌: {column}")
    return {str(row[column]).strip(): row for row in frame.to_dict("records")}


def filter_base_by_order_scope(
    base: pd.DataFrame,
    suppliers: pd.DataFrame,
    params: Mapping[str, Any],
) -> pd.DataFrame:
    """Apply the final order staff/vendor predicates before expensive sources.

    ``suppliers`` is already the authoritative, one-row-per-product result of
    representative inbound-vendor selection.  Keeping this predicate shared
    with result assembly prevents a staff query from calculating demand and
    prices for products that can never appear in its result.
    """
    if not isinstance(base, pd.DataFrame) or base.empty:
        return base
    if not order_scope_active(params):
        return base
    if not isinstance(suppliers, pd.DataFrame) or suppliers.empty or "product_code" not in suppliers:
        suppliers = pd.DataFrame(columns=["product_code"])
    elif suppliers["product_code"].duplicated().any():
        # Keep the existing source-grain contract from _index(): a duplicate
        # product row is an authority error, never a row-order tie-break.
        raise ValueError("원천 제품 grain 충돌: product_code")

    # Representative-vendor facts have already been calculated from the
    # unscoped R110 source.  Use only the authority columns needed for this
    # final predicate instead of materializing every Snapshot/facts row as a
    # Python dictionary.
    def scope_text(value: Any) -> str:
        return str(value or "").strip()

    authority_columns = {
        "recent_inbound_vendor_staff_code": "_order_staff_code",
        "recent_inbound_vendor_staff_name": "_order_staff_name",
        "manufacturer_staff_code": "_pharma_staff_code",
        "manufacturer_staff_name": "_pharma_staff_name",
        "recent_inbound_vendor_code": "_vendor_code",
        "recent_inbound_vendor_name": "_vendor_name",
    }
    supplier_columns = set(suppliers.columns)
    supplier_view = suppliers.reindex(columns=["product_code", *authority_columns]).copy()
    for source in authority_columns:
        if source not in supplier_columns:
            supplier_view[source] = ""
    supplier_view["_product_code"] = supplier_view["product_code"].map(scope_text)
    # _index() used a dict comprehension and consequently retained the last
    # normalized key if differently formatted source keys collided.
    supplier_view = supplier_view.drop_duplicates("_product_code", keep="last").set_index("_product_code")
    for source, target in authority_columns.items():
        supplier_view[target] = supplier_view[source].map(scope_text)

    base_codes = base["제품코드"].map(scope_text) if "제품코드" in base else pd.Series("", index=base.index)
    aligned = supplier_view.reindex(base_codes)
    aligned.index = base.index
    mask = pd.Series(True, index=base.index)
    order_filter = scope_text(params.get("order_staff_nm"))
    pharma_filter = scope_text(params.get("pharma_staff_nm"))
    order_vendor_code = params.get("order_vendor_cd")
    order_vendor_name = params.get("order_vendor_nm")
    if order_filter:
        mask &= aligned["_order_staff_code"].eq(order_filter) | aligned["_order_staff_name"].str.contains(order_filter, regex=False, na=False)
    if pharma_filter:
        mask &= aligned["_pharma_staff_code"].eq(pharma_filter) | aligned["_pharma_staff_name"].str.contains(pharma_filter, regex=False, na=False)
    if order_vendor_code:
        mask &= aligned["_vendor_code"].eq(order_vendor_code)
    if order_vendor_name:
        mask &= aligned["_vendor_name"].str.contains(order_vendor_name, regex=False, na=False)
    # The former ``DataFrame(rows, ...)`` construction also reset the index.
    # Preserve that caller-visible frame contract while avoiding record copies.
    return base.loc[mask].reset_index(drop=True).copy()


def order_scope_active(params: Mapping[str, Any]) -> bool:
    return any(str(params.get(key) or "").strip() for key in (
        "order_staff_nm", "pharma_staff_nm", "order_vendor_cd", "order_vendor_nm",
    ))


def order_scope_row_matches(params: Mapping[str, Any], supplier: Mapping[str, Any]) -> bool:
    """Return the authoritative post-representative-vendor scope decision."""
    order_code = str(supplier.get("recent_inbound_vendor_staff_code") or "").strip()
    order_name = str(supplier.get("recent_inbound_vendor_staff_name") or "").strip()
    pharma_code = str(supplier.get("manufacturer_staff_code") or "").strip()
    pharma_name = str(supplier.get("manufacturer_staff_name") or "").strip()
    vendor_code = str(supplier.get("recent_inbound_vendor_code") or "").strip()
    vendor_name = str(supplier.get("recent_inbound_vendor_name") or "").strip()
    order_filter = str(params.get("order_staff_nm") or "").strip()
    pharma_filter = str(params.get("pharma_staff_nm") or "").strip()
    if order_filter and order_filter != order_code and order_filter not in order_name:
        return False
    if pharma_filter and pharma_filter != pharma_code and pharma_filter not in pharma_name:
        return False
    if params.get("order_vendor_cd") and params["order_vendor_cd"] != vendor_code:
        return False
    if params.get("order_vendor_nm") and params["order_vendor_nm"] not in vendor_name:
        return False
    return True


def assemble_result(params: Mapping[str, Any], sources: dict) -> pd.DataFrame:
    assembly_started = time.perf_counter()
    timings = sources.setdefault('performance_ms', {})
    input_order_date = str(params["order_date"])
    effective_order_date = str(sources.get("effective_business_date") or input_order_date)
    reference = date.fromisoformat(effective_order_date)
    conditions = OrderConditions(reference, int(params["safety_days"]),
                                 int(params["target_days"]), int(params["closing_day"]))
    next_month_date = (
        date(reference.year + 1, 1, 1)
        if reference.month == 12 else date(reference.year, reference.month + 1, 1)
    )
    next_month_key = next_month_date.strftime("%Y%m")
    closing_relation = closing_day_relation(conditions)
    closing_cycle_next_month_policy = closing_relation in {"ON", "AFTER"}
    dates = sources.get("business_dates", [])
    horizon = horizon_dates(conditions, dates)
    safety = sorted(d for d in dates if d > reference)[:conditions.safety_days]
    month_day_counts = Counter(d.strftime('%Y%m') for d in set(dates))
    horizon_month_counts = Counter(d.strftime('%Y%m') for d in horizon)
    safety_month_counts = Counter(d.strftime('%Y%m') for d in safety)
    mapping_started = time.perf_counter()
    demand_rows = _index(sources.get("demand", pd.DataFrame()))
    vendors = _index(sources.get("suppliers", pd.DataFrame()), "product_code")
    pending = _index(sources.get("pending", pd.DataFrame()))
    history_groups = _history_index(sources.get('order_history_unit', pd.DataFrame()))
    order_price_3m = sources.get('order_price_3m')
    order_price_1y = sources.get('order_price_1y')
    if order_price_3m is None or order_price_1y is None:
        order_price_maps = _latest_order_prices_by_windows(
            sources.get('order_history', pd.DataFrame()), reference=reference, months=(3, 12),
        )
        order_price_3m = order_price_maps[3]
        order_price_1y = order_price_maps[12]
    contract_purchase_prices = sources.get('contract_purchase_prices')
    if contract_purchase_prices is None:
        contract_purchase_prices = _contract_purchase_price_map(sources.get('contracts', {}).get('df', pd.DataFrame()))
    timings['merge_mapping'] = (time.perf_counter() - mapping_started) * 1000
    month_days = len({d for d in dates if (d.year, d.month) == (reference.year, reference.month)})
    unit_ms = 0
    demand_ms = price_ms = quantity_ms = 0
    output = []
    for basic in sources.get("base", pd.DataFrame()).to_dict("records"):
        code = str(basic["제품코드"]).strip()
        demand = demand_rows.get(code, {})
        supplier = vendors.get(code, {})
        vendor_code = str(supplier.get("recent_inbound_vendor_code") or "").strip()
        vendor_name = str(supplier.get("recent_inbound_vendor_name") or "").strip()
        order_staff_code = str(supplier.get("recent_inbound_vendor_staff_code") or "").strip()
        order_staff_name = str(supplier.get("recent_inbound_vendor_staff_name") or "").strip()
        pharma_staff_code = str(supplier.get("manufacturer_staff_code") or "").strip()
        pharma_staff_name = str(supplier.get("manufacturer_staff_name") or "").strip()
        if not order_scope_row_matches(params, supplier):
            continue
        demand_started = time.perf_counter()
        stock = decimal_or_none(demand.get("현재재고수량"))
        if stock is None:
            stock = decimal_or_none(demand.get("실재고수량"))
        if params.get("stock_apply_nm") and not params.get("stock_apply_cd"):
            # A name-only application cannot safely constrain stock by an exact code.
            stock = None
        forecast_plan = decimal_or_none(demand.get("당월 예상출고수량"))
        base_plan = decimal_or_none(demand.get("예상기준월수량"))
        if base_plan is None:
            base_plan = forecast_plan
        basis = str(demand.get("수요예상기준") or "")
        current_actual = decimal_or_none(demand.get("당월 현재출고수량"))
        recent_3m_avg = decimal_or_none(demand.get("최근3개월평균수요수량"))
        if recent_3m_avg is None:
            recent_3m_avg = decimal_or_none(demand.get("최근3개월평균출고수량"))
        previous_3m_avg = decimal_or_none(demand.get("직전3개월평균수요수량"))
        completed_months = int(decimal_or_none(demand.get("완료월수")) or 0)
        frequency_grade = str(basic.get("출고빈도등급") or demand.get("출고빈도등급") or "").strip().upper()
        trend_rate, trend_adjustment, trend_reason = demand_trend_adjustment(
            recent_3m_avg=recent_3m_avg,
            previous_3m_avg=previous_3m_avg,
            completed_months=completed_months,
            frequency_grade=frequency_grade,
        )
        adjusted_plan = base_plan * (Decimal(1) + trend_adjustment) if base_plan is not None else None
        plans = {}
        forecast_ready = bool(
            adjusted_plan is not None and adjusted_plan > 0 and basis and "부족" not in basis
        )
        if forecast_ready:
            plans[next_month_key if closing_cycle_next_month_policy else reference.strftime("%Y%m")] = adjusted_plan
        # Explicit future plans may be supplied by an existing adapter; never extend current plan.
        plans.update(sources.get("future_plans", {}).get(code, {}))
        next_month_plan = decimal_or_none(plans.get(next_month_key))
        next_month_business_days = int(month_day_counts.get(next_month_key, 0))
        next_month_daily = (
            next_month_plan / Decimal(next_month_business_days)
            if closing_cycle_next_month_policy and next_month_plan is not None and next_month_business_days > 0
            else None
        )
        if closing_cycle_next_month_policy:
            hd = next_month_daily * Decimal(conditions.target_days) if next_month_daily is not None else None
            sd = next_month_daily * Decimal(conditions.safety_days) if next_month_daily is not None else None
            missing = () if next_month_daily is not None else (next_month_key,)
            safety_missing = missing
        else:
            hd, missing = demand_for_dates(horizon, reference_date=reference,
                                          business_dates=dates, monthly_forecast=plans,
                                          month_day_counts=month_day_counts, date_month_counts=horizon_month_counts)
            sd, safety_missing = demand_for_dates(safety, reference_date=reference,
                                                 business_dates=dates, monthly_forecast=plans,
                                                 month_day_counts=month_day_counts, date_month_counts=safety_month_counts)
        elapsed = sources.get("elapsed_days")
        daily = (
            next_month_daily if closing_cycle_next_month_policy
            else adjusted_plan / Decimal(month_days) if reference.strftime("%Y%m") in plans and month_days else None
        )
        forecast_basis = basis
        basis = (
            "다음달 예측기반" if closing_cycle_next_month_policy and next_month_daily is not None
            else "예측기반" if reference.strftime("%Y%m") in plans
            else "수요근거 없음"
        )
        fallback_reason = trend_reason
        if (not closing_cycle_next_month_policy and not plans and sources.get("normal_outbound_verified")
                and current_actual is not None and current_actual > 0 and elapsed):
            daily = current_actual / Decimal(elapsed)
            # Actual-pace fallback is allowed only within the known current month.
            hd = daily * len(horizon) if all(d.month == reference.month for d in horizon) else None
            sd = daily * len(safety) if all(d.month == reference.month for d in safety) else None
            basis = "실적기반"
            fallback_reason = "current_month_actual_pace"
            missing = tuple(m for m in missing if m != reference.strftime("%Y%m"))
            safety_missing = tuple(m for m in safety_missing if m != reference.strftime("%Y%m"))
        calendar_ready = sources.get("calendar_status") == "ready"
        if not calendar_ready or len(safety) < conditions.safety_days:
            hd = sd = None
            daily = None
        pending_qty = decimal_or_none(pending.get(code, {}).get("입고예정수량", 0))
        increasing = trend_adjustment > 0
        demand_ms += (time.perf_counter() - demand_started) * 1000
        unit_started = time.perf_counter()
        selected_history = history_groups.get((code, vendor_code), [])
        unit, unit_reason = infer_order_unit(selected_history) if vendor_code else (None, '대표매입처 사용자확인')
        if not sources.get('order_history_complete', False):
            unit, unit_reason = None, '발주이력 완전성 사용자확인'
        unit_ms += (time.perf_counter() - unit_started) * 1000
        quantity_started = time.perf_counter()
        quantity = calculate_quantities(stock=stock if stock is not None else Decimal(0),
                                       pending=pending_qty, safety_demand=sd if stock is not None else None,
                                       horizon_demand=hd, unit=unit, increasing=increasing)
        quantity_ms += (time.perf_counter() - quantity_started) * 1000
        price_started = time.perf_counter()
        price = None
        price_source = "미확정"
        price_decision = "REVIEW"
        price_key = (code, str(params.get("cost_apply_cd") or "").strip())
        if params.get("cost_apply_cd") or params.get("cost_apply_nm"):
            contract_payload = sources.get("contracts", {})
            if contract_payload.get("meta", {}).get("full_source_limit_hit"):
                price_source = "미확정"
            else:
                price = contract_purchase_prices.get(price_key)
                if price is not None:
                    price_source, price_decision = "계약단가", "확정"
        if price is None and not sources.get("order_history_price_truncated", False):
            price = order_price_3m.get(price_key)
            if price is not None:
                price_source, price_decision = "최근3개월발주단가", "확정"
            else:
                price = order_price_1y.get(price_key)
                if price is not None:
                    price_source, price_decision = "최근1년발주단가", "확정"
        if price is None:
            price_source, price_decision = "미확정", "REVIEW"
        price_ms += (time.perf_counter() - price_started) * 1000
        row = {**basic, **quantity, "회사": params["company_id"], "발주일자": effective_order_date,
               "입력 발주일자": input_order_date,
               "영업일 보정 여부": bool(sources.get("business_date_shifted", False)),
               "영업일 보정 일수": int(sources.get("business_date_shift_days") or 0),
               "발주처코드": vendor_code, "발주처": vendor_name, "대표매입처출처": supplier.get("recent_inbound_vendor_source"),
               "재고수량": stock, "입고예정수량": pending_qty,
               "안전재고 기준수량": sd, "안전재고일수": conditions.safety_days,
               "적정재고일수": conditions.target_days, "결제일자": conditions.closing_day,
               "적용 horizon 영업일수": len(horizon), "horizon 예정수량": hd,
               "적용 필요 영업일수": len(horizon), "적용 필요예정수량": hd,
               "당월 정상출고수량": current_actual, "수요근거": basis,
               "월 기준 예상수량": adjusted_plan if basis in ("예측기반", "다음달 예측기반") else None,
               "다음달 예상수요월": next_month_key if closing_cycle_next_month_policy else "",
               "다음달 예상수요수량": next_month_plan if closing_cycle_next_month_policy else None,
               "다음달 예상 일수요": next_month_daily,
               "다음달 영업일수": next_month_business_days if closing_cycle_next_month_policy else None,
               "결제일 다음달 수요정책": closing_cycle_next_month_policy,
               "결제일 관계": closing_relation,
               "수요 authority 월": next_month_key if closing_cycle_next_month_policy else reference.strftime("%Y%m"),
               "예측 산출근거": forecast_basis,
               "해당 월 전체 영업일수": month_days if calendar_ready else None,
               "경과 영업일수": elapsed, "기준 1영업일 예상수량": daily,
               "horizon 필요예정수량": hd,
               "수요 사용자확인월": ",".join(sorted(set(missing + safety_missing))),
               "발주단위": unit, "추세": "증가" if increasing else "유지/감소/증가근거 부족", "최근 발주횟수": len(selected_history),
               "당월 출고 거래처수": sources.get('current_customer_counts', {}).get(code, 0) if 'current_customer_counts' in sources else None,
               "발주단위 근거": unit_reason,
               "조달주의": '' if unit is not None else unit_reason, "발주단가": price, "단가출처": price_source,
               "단가판정": price_decision,
               "재고적용처코드": params.get("stock_apply_cd", ""), "재고적용처": params.get("stock_apply_nm", ""),
               "단가적용처코드": params.get("cost_apply_cd", ""), "단가적용처": params.get("cost_apply_nm", ""),
               "발주담당자코드": order_staff_code, "발주담당자": order_staff_name,
               "제약담당자코드": pharma_staff_code, "제약담당자": pharma_staff_name,
               "매입거래처수": int(supplier.get("recent_inbound_vendor_count_90") or 0),
               "base_demand_qty": base_plan, "recent_3m_avg": recent_3m_avg,
               "previous_3m_avg": previous_3m_avg, "trend_rate": trend_rate,
               "trend_adjustment": trend_adjustment, "adjusted_demand_qty": adjusted_plan,
               "raw_order_qty": quantity.get("계산 발주수량"),
               "final_recommended_qty": quantity.get("추천 발주수량"),
               "fallback_reason": fallback_reason}
        row["추천대비수정수량"] = Decimal(0) if quantity["추천 발주수량"] is not None else None
        actual = quantity["실제 발주수량"]
        row.update(amounts(actual if actual is not None else Decimal(0), price) if actual is not None else amounts(Decimal(0), None))
        row["계산상태"] = "수요/재고 사용자확인" if actual is None else (
            "발주해당" if actual > 0 else "발주 필요 없음")
        row["발주사유/계산근거"] = f"{row['계산상태']} / 안전기준 {sd} / 적용 필요 {len(horizon)}영업일 {hd} / 현재고 {stock} / 입고예정 {pending_qty}"
        if unit is not None and quantity['발주trigger'] and quantity['계산 발주수량'] is not None and 0 < quantity['계산 발주수량'] < unit:
            row['조달주의'] = '최소 발주단위 1회분 적용/원 필요수량보다 큰 추천수량 확인'
        if not vendor_code:
            row["조달주의"] += " / 대표매입처 사용자확인"
        if params.get("stock_apply_nm") and not params.get("stock_apply_cd"):
            row["조달주의"] += " / 재고적용처별 현재고 authority 확인 필요"
        output.append(row)
    build_started = time.perf_counter()
    result = pd.DataFrame(output)
    timings['full_result_build'] = (time.perf_counter() - build_started) * 1000
    if not result.empty and result.duplicated(list(ROW_KEY)).any():
        raise ValueError("발주계산 결과 grain 충돌")
    filter_started = time.perf_counter()
    status_counts = (
        {str(key): int(value) for key, value in result["계산상태"].value_counts(dropna=False).items()}
        if not result.empty and "계산상태" in result else {}
    )
    before_rows = len(result)
    filter_diagnostics = {
        "total_product_rows": before_rows,
        "demand_evidence_rows": int(result["적용 필요예정수량"].notna().sum()) if "적용 필요예정수량" in result else 0,
        "demand_evidence_definition": "full_horizon_demand_ready",
        "next_month_forecast_ready_rows": int(result["다음달 예상 일수요"].notna().sum()) if "다음달 예상 일수요" in result else 0,
        "at_or_below_safety_stock_rows": int(
            (
                pd.to_numeric(result["재고수량"], errors="coerce")
                <= pd.to_numeric(result["안전재고 기준수량"], errors="coerce")
            ).fillna(False).sum()
        ) if not result.empty and {"재고수량", "안전재고 기준수량"}.issubset(result.columns) else 0,
        "calculated_order_positive_rows": int((pd.to_numeric(result["계산 발주수량"], errors="coerce") > 0).sum()) if "계산 발주수량" in result else 0,
        "recommended_order_positive_rows": int((pd.to_numeric(result["추천 발주수량"], errors="coerce") > 0).sum()) if "추천 발주수량" in result else 0,
        "expected_inbound_positive_rows": int((pd.to_numeric(result["입고예정수량"], errors="coerce") > 0).sum()) if "입고예정수량" in result else 0,
        "calculation_status_counts": status_counts,
        "only_needed_before_rows": before_rows,
    }
    if params.get("only_needed") and not result.empty:
        result = result.loc[result["계산상태"].eq("발주해당")].copy()
    if params.get("query_mode") == "확인 필요" and not result.empty:
        result = result.loc[result["계산상태"].str.contains("확인") | result["발주단가"].isna() | result["발주처코드"].eq("")].copy()
    timings['only_needed_filter'] = (time.perf_counter() - filter_started) * 1000
    filter_diagnostics["only_needed_after_rows"] = len(result)
    filter_diagnostics["excluded_by_only_needed_rows"] = before_rows - len(result) if params.get("only_needed") else 0
    filter_diagnostics["exclusion_reason_counts"] = {
        "needs_confirmation": int(status_counts.get("수요/재고 사용자확인", 0)),
        "no_order_required": int(status_counts.get("발주 필요 없음", 0)),
    }
    result.attrs["order_filter_diagnostics"] = filter_diagnostics
    log.info(
        "[order_calculation.filter_diagnostics] company_id=%s query_mode=%s diagnostics=%s",
        params["company_id"], params.get("query_mode"), filter_diagnostics,
    )
    timings['unit_inference'] = unit_ms
    timings['product_demand_calculation'] = demand_ms
    timings['product_quantity_calculation'] = quantity_ms
    timings['product_price_mapping'] = price_ms
    timings['assembly_total'] = (time.perf_counter() - assembly_started) * 1000
    log.info('[order_calculation.perf] company_id=%s stages_ms=%s', params['company_id'], timings)
    return result.reset_index(drop=True)


def get_order_calculation_result(params=None, *, source_loader: Callable = load_sources):
    started = time.perf_counter()
    request_started_at = datetime.now().isoformat(timespec="seconds")
    request_started_monotonic = time.monotonic()
    q = apply_application_defaults({"safety_days": 3, "target_days": 15, "closing_day": 25, **dict(params or {})})
    if q.pop("_ambiguous_staff_role", False):
        message = "담당자 역할을 지정해 주세요. 발주담당자 또는 제약담당자로 조회할 수 있습니다."
        return {
            "table": TABLE, "action": ACTION, "title": ACTION, "params": q,
            "data": message, "message": message, "records": [], "columns": [], "final": True,
            "meta": {"result_status": "input_required", "input_required": True,
                     "service_call_skipped": True, "source_call_count": 0,
                     "row_count": 0, "row_count_total": 0, "tableless_result": True},
        }
    mode = q.get("query_mode") or ("발주해당자료만" if q.get("only_needed") else "전체")
    if mode not in ("전체", "발주해당자료만", "확인 필요"):
        raise ValueError("발주 계산 조회구분을 확인하세요.")
    q["query_mode"] = mode
    q["only_needed"] = mode == "발주해당자료만"
    from app.services.business_calendar_service import kst_today
    q.setdefault("order_date", kst_today().isoformat())
    current = get_current_company_id()
    if current is None or (q.get("company_id") is not None and int(q["company_id"]) != int(current)):
        raise ValueError("현재 회사와 발주계산 회사가 일치하지 않습니다.")
    q["company_id"] = int(current)
    OrderConditions(date.fromisoformat(q["order_date"]), int(q["safety_days"]), int(q["target_days"]), int(q["closing_day"]))
    with read_only_request(timeout_seconds=120) as measurement:
        token = _source_measurement.set(measurement)
        try:
            source_query = {k: v for k, v in q.items() if k not in ("query_mode", "only_needed")}
            sources = source_loader(source_query)
            for field in ("cost_apply", "stock_apply"):
                if source_query.get(field + "_nm"):
                    q[field + "_nm"] = source_query[field + "_nm"]
        finally:
            _source_measurement.reset(token)
        snapshot = sources.get("snapshot", {})
        if snapshot.get("meta", {}).get("snapshot_status") != "ready":
            snapshot_meta = dict(snapshot.get("meta") or {})
            reason = str(snapshot_meta.get("snapshot_reason") or "")
            error_class = reason.split(":", 1)[1] if ":" in reason else reason or "SnapshotUnavailable"
            error_stage = (
                "snapshot_authority" if reason.startswith("snapshot_authority_unavailable")
                else "snapshot_master" if reason == "product_master_unavailable"
                else "snapshot_validation"
            )
            evidence = build_manual_validation_evidence(
                params=q, sources=sources, result_frame=pd.DataFrame(), measurement=measurement,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                result_status=str(snapshot_meta.get("result_status") or "query_error"),
                error={
                    "error_stage": error_stage,
                    "exception_class": error_class,
                    "exception_message": str(snapshot.get("message") or ""),
                },
            )
            artifact_path = write_manual_validation_evidence(q, evidence)
            snapshot_meta.update({
                "source_call_count": len(measurement["queries"]),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                "manual_validation_evidence": evidence,
                "manual_validation_artifact_path": artifact_path,
            })
            log.info(
                "[order.calc.perf] company_id=%s scope=%s scope_value=%s scoped_products=%s result_rows=0 source_calls=%s elapsed_ms=%s canonical_sha256=%s status=%s",
                q["company_id"], evidence["scope"]["kind"], evidence["scope"]["value"],
                evidence["product_sets"]["scoped_product_codes"]["count"], len(measurement["queries"]),
                evidence["total_elapsed_ms"], evidence["canonical_result"]["sha256"], evidence["result_status"],
            )
            return {**snapshot, "action": ACTION, "title": ACTION, "table": TABLE, "meta": snapshot_meta}
        post_source_started = time.perf_counter()
        frame = assemble_result(q, sources)
        order_filter_diagnostics = dict(frame.attrs.get("order_filter_diagnostics") or {})
    timings = sources.setdefault("performance_ms", {})
    primary = ["제품코드", "제품명", "규격", "추세", "계산 발주수량", "추천 발주수량", "실제 발주수량",
               "재고수량", "입고예정수량", "발주처", "발주담당자", "제약담당자", "매입거래처수",
               "발주단가", "발주금액(부가세포함)",
               "월 기준 예상수량", "안전재고 기준수량", "적용 필요예정수량",
               "수요근거", "기준 1영업일 예상수량", "발주단위",
               "당월 출고 거래처수", "3개월출고거래처수", "당월 정상출고수량", "3개월출고수량", "품목기여등급", "품목손익등급", "출고빈도등급",
               "제약사", "계산상태", "단가출처", "조달주의"]
    detail = ["기준 1영업일 예상수량", "당월 정상출고수량", "당월 출고 거래처수", "3개월출고수량",
              "3개월출고거래처수", "매입거래처수", "발주담당자코드", "발주담당자",
              "제약담당자코드", "제약담당자",
              "발주단위", "발주단위 근거", "단가출처",
              "단가적용처코드", "단가적용처", "재고적용처코드", "재고적용처", "계산상태", "조달주의",
              "품목기여등급", "품목손익등급", "출고빈도등급"]
    # Keep the compact business header deterministic even when optional 규격 is absent.
    export_order = list(dict.fromkeys(primary[:16] + detail + list(frame.columns)))
    result_projection_started = time.perf_counter()
    frame = frame.loc[:, [c for c in export_order if c in frame]]
    timings["result_projection"] = (time.perf_counter() - result_projection_started) * 1000
    projection_started = time.perf_counter()
    display = frame.head(300).loc[:, [c for c in primary if c in frame]].copy()
    timings['display_projection'] = (time.perf_counter() - projection_started) * 1000
    summary = f"결과: 발주 계산 {len(frame):,}건 | 화면 {len(display):,}건 | Excel/CSV 전체 {len(frame):,}건\n수량은 추천이며 ERP 등록되지 않습니다."
    if frame.empty and int(order_filter_diagnostics.get("only_needed_before_rows") or 0) > 0:
        exclusion = dict(order_filter_diagnostics.get("exclusion_reason_counts") or {})
        summary += (
            f"\n필터 전 {int(order_filter_diagnostics['only_needed_before_rows']):,}건: "
            f"확인 필요 {int(exclusion.get('needs_confirmation') or 0):,}건, "
            f"발주 필요 없음 {int(exclusion.get('no_order_required') or 0):,}건. "
            "'확인 필요' 조회로 자료 부족 품목을 확인하세요."
        )
    input_order_date = q["order_date"]
    effective_order_date = str(sources.get("effective_business_date") or input_order_date)
    reference = date.fromisoformat(effective_order_date)
    calendar_dates = sources.get("business_dates", [])
    calendar_ready = sources.get("calendar_status") == "ready"
    month_days = len({d for d in calendar_dates if (d.year, d.month) == (reference.year, reference.month)})
    conditions = OrderConditions(reference, int(q["safety_days"]), int(q["target_days"]), int(q["closing_day"]))
    needed_days = len(horizon_dates(conditions, calendar_dates))
    mode = q.get('query_mode') or ('발주해당자료만' if q.get('only_needed') else '전체')
    payment = (
        f"결제일 {conditions.closing_day}일"
        + (
            f"(적용 {conditions.effective_closing_day}일)"
            if conditions.closing_day != conditions.effective_closing_day else ""
        )
        if conditions.closing_day else "결제일 현금/당일결제"
    )
    query_summary = (f"입력 발주일 {input_order_date}"
                     + (f" | 유효 발주일 {effective_order_date}" if effective_order_date != input_order_date else "")
                     + f" | 조회구분 {mode}"
                     f" | 단가적용처코드 {q.get('cost_apply_cd') or ''}"
                     f" | 단가적용처명 {q.get('cost_apply_nm') or ''}"
                     f" | 재고적용처코드 {q.get('stock_apply_cd') or ''}"
                     f" | 재고적용처명 {q.get('stock_apply_nm') or ''}"
                     f" | 안전재고 {conditions.safety_days}일 | 적정재고 {conditions.target_days}일 | {payment}")
    for field, label in (("physic_cd", "제품코드"),
                         ("physic_nm", "제품명"), ("order_vendor_nm", "발주처"),
                         ("order_staff_nm", "발주담당자"), ("pharma_staff_nm", "제약담당자"),
                         ("maker_cd", "제약사코드"), ("order_vendor_cd", "발주처코드"),
                         ("product_keyword", "제품 키워드"), ("insu_cd", "보험코드"),
                         ("maker_nm", "제약사"),
                         ("stock_cd", "재고위치코드"), ("stock_nm", "재고위치")):
        if q.get(field):
            query_summary += f" | {label} {q[field]}"
    closing_relation = closing_day_relation(conditions)
    next_cycle_policy = closing_relation in {"ON", "AFTER"}
    next_month = (reference.replace(day=1) + timedelta(days=32)).replace(day=1).strftime("%Y%m")
    calculation_basis = (
        f"계산기준: {payment} | {next_month} 예상수요 | "
        f"안전 {conditions.safety_days}영업일 | 목표 {conditions.target_days}영업일"
        if calendar_ready and next_cycle_policy else
        f"계산기준: {reference.month}월 영업일 {month_days}일 | 적용 필요 {needed_days}영업일"
        if calendar_ready else "계산기준: 공식 영업일 자료 확인 필요"
    )
    summary = calculation_basis + " | 입고예정 최근 4영업일\n\n" + summary
    meta = {**snapshot.get("meta", {}), "result_status": "success", "row_count_total": len(frame),
            "row_count": len(display), "full_source_row_count": len(frame), "download_row_count": len(frame),
            "source_call_count": len(measurement["queries"]), "source_queries": measurement["queries"],
            "performance_ms": dict(sources.get('performance_ms', {})),
            "source_grain": "+".join(ROW_KEY), "source_mode": "read_only_order_calculation",
            "expected_inbound_source": dict(sources.get("pending", pd.DataFrame()).attrs),
            "current_outbound_customer_authority": "Analytics R120 정상출고 상세/당월/제품별 distinct 거래처코드",
            "outbound_customer_3m_authority": "approved profile-exact Snapshot 2.1",
            "order_unit_authority": "R170/R180 최근1개월 대표매입처/반복발주 v1",
            "order_history_period": sources.get('order_history_period'),
            "order_history_unit_period": sources.get('order_history_unit_period'),
            "order_price_authority": "R170/R180 정상상태(1,2,3)/단가적용처+제품/최근3개월 후 최근1년/latest order grain",
            "order_demand_contract": "monthly_forecast_full_business_days_v1",
            "closing_day_demand_contract": "before_current_month_on_after_next_month_product_quantity_forecast_v2",
            "order_demand_trend_contract": "completed_recent3_vs_previous3_deadband10_half_cap30_before_stock_pending_unit_v1",
            "summary_md": summary, "llm_summary_md": summary + "\n계산/추천/실제수량은 독립입니다. 단가와 수요 확인 필요 상태를 정상값으로 해석하지 마세요. ERP 발주 확정 또는 회계 확정손익이 아닙니다.",
            "order_calculation_editable": True, "query_summary": query_summary,
            "calculation_basis": calculation_basis + " | 입고예정 최근 4영업일",
            "order_header_metrics": {"needed_days": needed_days if calendar_ready else None,
                                     "query_mode": mode, "inbound_business_days": 4,
                                     "order_date": input_order_date, "safety_days": conditions.safety_days,
                                     "target_days": conditions.target_days, "closing_day": conditions.closing_day,
                                     "effective_closing_day": conditions.effective_closing_day,
                                     "effective_business_date": effective_order_date,
                                     "business_date_shifted": bool(sources.get("business_date_shifted", False)),
                                     "business_date_shift_days": int(sources.get("business_date_shift_days") or 0),
                                     "business_date_shift_policy": "next_official_business_day_on_or_after",
                                     "business_date_authority": sources.get("business_date_authority", "ssai_common_calendar"),
                                     "closing_relation": closing_relation,
                                     "demand_authority_month": next_month if next_cycle_policy else reference.strftime("%Y%m"),
                                     "maker_nm": q.get("maker_nm", "")},
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "elapsed_authority": "order_calculation_request_to_result_ready",
            "request_started_at": request_started_at,
            "request_started_monotonic": request_started_monotonic,
            "purchase_customer_3m_authority": "R110 정상입고 / 기준일 포함 최근 90일 / 제품별 DISTINCT 매입거래처코드",
            "order_staff_authority": "최종 발주처코드 -> R030 Sales_Man -> R060 사용자명",
            "pharma_staff_authority": "제품마스터 제약사코드(R040 Rd04_Ven_Cd) -> R030 Sales_Man -> R060 사용자명",
            "order_staff_resolution": {
                "missing_order_vendor": int(frame.get("발주처코드", pd.Series(dtype="object")).fillna("").astype(str).str.strip().eq("").sum()),
                "missing_sales_man": int((frame.get("발주처코드", pd.Series(dtype="object")).fillna("").astype(str).str.strip().ne("") & frame.get("발주담당자코드", pd.Series(dtype="object")).fillna("").astype(str).str.strip().eq("")).sum()),
                "unmatched_user": int((frame.get("발주담당자코드", pd.Series(dtype="object")).fillna("").astype(str).str.strip().ne("") & frame.get("발주담당자", pd.Series(dtype="object")).fillna("").astype(str).str.strip().eq("")).sum()),
            },
             "order_filter_diagnostics": order_filter_diagnostics,
             "settlement_rounding_status": "정산 반올림 미적용(원 계산 보존)", "full_source_ready": True}
    timings["post_source_result_ready"] = (time.perf_counter() - post_source_started) * 1000
    timings["post_source_total"] = (time.perf_counter() - post_source_started) * 1000
    evidence = build_manual_validation_evidence(
        params=q, sources=sources, result_frame=frame, measurement=measurement,
        elapsed_ms=(time.perf_counter() - started) * 1000, result_status="success",
    )
    evidence["stages"]["final_assembly"] = {
        "label": _EVIDENCE_STAGE_LABELS["final_assembly"],
        "elapsed_ms": round(float(sources.get("performance_ms", {}).get("assembly_total", 0)), 3),
        "rows": len(frame), "source_call_count": 0,
    }
    evidence["stages"]["result_projection"] = {
        "label": "result_projection",
        "elapsed_ms": round(float(timings.get("result_projection", 0)), 3),
        "rows": len(frame), "source_call_count": 0,
    }
    evidence["stages"]["display_projection"] = {
        "label": "display_projection",
        "elapsed_ms": round(float(timings.get("display_projection", 0)), 3),
        "rows": len(display), "source_call_count": 0,
    }
    evidence["stages"]["post_source_assembly"] = {
        "label": "post_source_assembly",
        "elapsed_ms": round(float(timings.get("post_source_total", 0)), 3),
        "rows": len(frame), "source_call_count": 0,
    }
    perf_summary = order_calculation_performance_summary(timings)
    artifact_path = write_manual_validation_evidence(q, evidence)
    meta["manual_validation_evidence"] = evidence
    meta["manual_validation_artifact_path"] = artifact_path
    log.info("[order_calculation.query] company_id=%s rows=%s snapshot=%s source_call_count=%s elapsed_ms=%s",
              current, len(frame), meta.get("snapshot_manifest_id"), meta["source_call_count"],
              round((time.perf_counter() - started) * 1000, 1))
    log.info(
        "[order.calc.perf] company_id=%s scope=%s scope_value=%s scoped_products=%s result_rows=%s source_calls=%s "
        "snapshot_ms=%s representative_vendor_ms=%s forecast_stock_ms=%s current_customer_ms=%s history_ms=%s "
        "pending_ms=%s r230_ms=%s contract_ms=%s post_source_assembly_ms=%s total_ms=%s canonical_sha256=%s status=success",
        current, evidence["scope"]["kind"], evidence["scope"]["value"],
        evidence["product_sets"]["scoped_product_codes"]["count"], len(frame), meta["source_call_count"],
        perf_summary["snapshot_ms"], perf_summary["representative_vendor_ms"], perf_summary["forecast_stock_ms"],
        perf_summary["current_customer_ms"], perf_summary["history_ms"], perf_summary["pending_ms"],
        perf_summary["r230_ms"], perf_summary["contract_ms"], perf_summary["post_source_assembly_ms"],
        evidence["total_elapsed_ms"], evidence["canonical_result"]["sha256"],
    )
    payload = {"table": TABLE, "action": ACTION, "title": ACTION, "params": q, "df": frame,
            "df_display": display, "records": display.to_dict("records"), "columns": list(display),
            "data": summary, "message": summary, "meta": meta, "final": True}
    from app.services.snapshot_product_information_service import project_product_information_payload_for_viewer
    viewer_projection_started = time.perf_counter()
    projected_payload = project_product_information_payload_for_viewer(payload)
    timings["viewer_projection"] = (time.perf_counter() - viewer_projection_started) * 1000
    meta["performance_ms"] = dict(timings)
    return projected_payload


def get_order_calculation_export_df(params=None):
    """Never regenerate an ordering calculation to prepare a download."""
    import streamlit as st
    query = dict(params or {})
    key = str(query.get("table_key") or query.get("download_table_key") or "")
    frame = st.session_state.get("sims_export_tables", {}).get(key)
    company = get_current_company_id()
    if not key or not isinstance(frame, pd.DataFrame) or company is None:
        raise ValueError("발주 계산 전체 원본이 없습니다. 새 조회를 제출하세요.")
    if "회사" not in frame or not frame["회사"].eq(company).all():
        raise ValueError("발주 계산 다운로드 회사가 일치하지 않습니다.")
    return frame.copy()
