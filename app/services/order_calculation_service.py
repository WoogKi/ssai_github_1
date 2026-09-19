"""Read-only source adapter and full-result assembly for ordering calculation."""

from __future__ import annotations

from datetime import date, timedelta, datetime
from calendar import monthrange
from decimal import Decimal, InvalidOperation
import logging
import time
from typing import Any, Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from collections import Counter

import pandas as pd

from app.db.mssql_client import get_current_company_id, query_to_df, read_only_request
from app.services.order_calculation_contract import (
    OrderConditions, amounts, calculate_quantities, demand_for_dates, demand_trend_adjustment,
    horizon_dates, infer_order_unit,
)

ACTION = "발주 계산"
TABLE = "order_calculation"
ROW_KEY = ("회사", "발주일자", "발주처코드", "제품코드")
log = logging.getLogger("ssai")
_source_measurement = ContextVar('order_source_measurement', default=None)


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
        append_product_master_filter_clauses, build_product_master_enrichment_sql,
    )
    joins, expressions = build_product_master_enrichment_sql(product_alias="P", include_audit_price=False)
    clauses, binds = [], []
    append_product_master_filter_clauses(clauses, binds, params, expressions=expressions)
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


def build_contract_price_params(params: dict) -> dict:
    # Order/forecast periods must not become a contract-start-date filter.
    query = {k: v for k, v in params.items() if k not in
             ('date_from', 'date_to', 'month_from', 'month_to')}
    query.update(ven_cd=params.get('cost_apply_cd'),
                 ven_nm=None if params.get('cost_apply_cd') else params.get('cost_apply_nm'),
                 as_of=date.fromisoformat(params['order_date']).strftime('%Y%m%d'))
    return query


def load_sources(params: dict) -> dict:
    from app.services.snapshot_product_information_service import get_snapshot_product_information_result
    from app.services.dashboard_inventory_frequency_snapshot_service import resolve_dashboard_profile_stock_scope
    from app.services.analytics_sales_trend_service import get_stock_shortage_df
    from app.services.dashboard_inbound_facts_service import get_dashboard_inbound_facts
    from app.services.rddbc170_rddbc180_order_service import get_expected_inbound_product_totals
    from app.services.rddbc230_service import get_rddbc230_result
    from app.services.business_calendar_service import business_day_month_context
    from app.services.ssai_business_calendar_repository import load_official_holidays
    from app.services.ssai_analysis_profile_service import load_dashboard_profile_checked, normalize_company_default_conditions

    reference = date.fromisoformat(params["order_date"])
    timings = {}
    def call(stage, function, *a, **kw):
        measurement = _source_measurement.get()
        before = len(measurement['queries']) if measurement is not None else 0
        with _phase(timings, stage, params['company_id']):
            value = function(*a, **kw)
        queries = measurement['queries'][before:] if measurement is not None else []
        for record in queries:
            if record.get('tables') == ['Rddbc010']:
                timings['code_names_sql'] = record.get('elapsed_ms', 0)
        sql_ms = sum(r.get('elapsed_ms', 0) for r in queries)
        timings[stage + '_sql'] = sql_ms
        timings[stage + '_python_other'] = max(0, timings[stage] - sql_ms)
        log.info('[order_calculation.perf] company_id=%s stage=%s calls=%s sql_ms=%s python_other_ms=%s',
                 params['company_id'], stage, len(queries), sql_ms, timings[stage + '_python_other'])
        return value
    codes = [params.get(field + "_cd") for field in ("cost_apply", "stock_apply") if params.get(field + "_cd")]
    if codes:
        applications = call('application_codes', query_to_df,
            "SELECT RTRIM(Rd03_Ven_Cd) AS code, RTRIM(Rd03_Ven_Nm) AS name FROM dbo.Rddbc030 WHERE Rd03_Ven_Cd IN (" + ','.join('?' for _ in codes) + ')', tuple(codes))
        names = dict(zip(applications['code'], applications['name']))
        for field in ("cost_apply", "stock_apply"):
            code = params.get(field + "_cd")
            if code and code not in names:
                raise ValueError(f"회사 {params['company_id']}에 적용처 코드 {code}가 없습니다. 대체하지 않았습니다.")
            if code:
                params[field + "_nm"] = names[code]
    def master(query):
        return call('master', _master, query)
    snapshot = call('snapshot_master', get_snapshot_product_information_result,
        {**params, "evaluation_month": reference.strftime("%Y%m")}, master_loader=master,
        apply_viewer_projection=False)
    if snapshot.get("meta", {}).get("snapshot_status") != "ready":
        return {"snapshot": snapshot}
    scope = resolve_dashboard_profile_stock_scope(company_id=params["company_id"])
    if params.get("stock_cd") and params["stock_cd"] not in scope.stock_codes:
        raise ValueError("저장된 재고범위 밖의 재고위치는 계산할 수 없습니다.")
    source_params = {**params, "evaluation_month": reference.strftime("%Y%m"),
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
    frame = snapshot.get("df", pd.DataFrame())
    if frame.empty:
        return {"snapshot": snapshot, "base": frame}
    # No new forecast model; the existing service supplies stock and quantity forecast together.
    # Saved classification is R040 Tax scope, not the legacy Physic_Gu filter.
    demand_params = build_demand_params(source_params, scope)
    demand = call('forecast_stock', get_stock_shortage_df, demand_params, product_universe_df=frame[["제품코드"]])
    for key in ('stock_sql_ms', 'stock_aggregate_ms', 'stock_shortage_build_ms', 'stock_shortage_total_ms'):
        timings[key] = demand.attrs.get(key, 0)
    from app.services.analytics_sales_trend_service import get_sales_trend_detail_df
    from app.services.rddbc170_rddbc180_order_service import get_order_df, normalize_order_params
    current_details = call('current_customer_source', get_sales_trend_detail_df, {**demand_params,
        'date_from': reference.replace(day=1).strftime('%Y%m%d'),
        'date_to': reference.strftime('%Y%m%d')})
    processing_started = time.perf_counter()
    customer_counts = (current_details.assign(제품코드=current_details['제품코드'].astype(str).str.strip(),
        거래처코드=current_details['거래처코드'].map(lambda v: None if pd.isna(v) else str(v).strip() or None))
        .groupby('제품코드')['거래처코드'].nunique().to_dict()) if not current_details.empty else {}
    timings['current_customer_mapping'] = (time.perf_counter() - processing_started) * 1000
    history_params = build_pending_params(source_params)
    history_params.update({'date_from': (pd.Timestamp(reference) - pd.DateOffset(months=1)).strftime('%Y%m%d'),
                          'date_to': reference.strftime('%Y%m%d'),
                          'status_codes': ['1', '2', '3']})
    order_history = call('order_history_source', get_order_df, history_params, mode='order')
    order_history_complete = len(order_history) < normalize_order_params(history_params, mode='order')['top']
    if not order_history.empty and order_history.duplicated(['발주일자', '발주거래처코드', '발주순번', '상세순번']).any():
        order_history_complete = False
    profile = load_dashboard_profile_checked(company_id=params["company_id"])
    defaults = normalize_company_default_conditions(profile.profile)
    suppliers = call('representative_vendor', get_dashboard_inbound_facts, source_params, data_cutoff_date=reference.strftime("%Y%m%d"),
        vendor_lookback_days=int(defaults.get("major_purchase_vendor_days", 90)))
    pending_params = build_pending_params(source_params)
    pending = call('pending_four_business_days', get_expected_inbound_product_totals, pending_params)
    prices = call('prices_code_names', get_rddbc230_result, source_params)
    price_frame = prices.get("df")
    if isinstance(price_frame, pd.DataFrame) and not price_frame.empty:
        prices["df"] = price_frame.loc[price_frame["재고위치"].astype(str).str.strip().isin(scope.stock_codes)].copy()
    contracts = {}
    if params.get("cost_apply_cd") or params.get("cost_apply_nm"):
        from app.services.rddbc070_service import get_rddbc070_current_result
        contracts = call('contract_prices', get_rddbc070_current_result, build_contract_price_params(source_params))
    end = reference + timedelta(days=max(120, int(params["target_days"]) * 4 + 62))
    end = end.replace(day=monthrange(end.year, end.month)[1])
    authority = call('official_calendar', load_official_holidays, start_date=reference.replace(day=1), end_date=end)
    business_dates = []
    if authority.status == "ready":
        cursor = reference.replace(day=1)
        while cursor <= end:
            if cursor.weekday() < 5 and cursor.strftime("%Y%m%d") not in authority.holiday_dates:
                business_dates.append(cursor)
            cursor += timedelta(days=1)
    context = business_day_month_context(evaluation_date=reference)
    return {"snapshot": snapshot, "base": frame, "demand": demand,
            "suppliers": suppliers, "pending": pending, "prices": prices, "contracts": contracts,
            "business_dates": business_dates, "calendar_status": authority.status,
            "elapsed_days": context.elapsed_business_days,
            "normal_outbound_verified": True,
            "source_params": source_params, "current_customer_counts": customer_counts,
            "order_history": order_history, "order_history_complete": order_history_complete,
            "performance_ms": timings,
            "order_history_period": [history_params['date_from'], history_params['date_to']]}


def _index(frame: pd.DataFrame, column="제품코드") -> dict:
    if frame is None or frame.empty or column not in frame:
        return {}
    if frame[column].duplicated().any():
        raise ValueError(f"원천 제품 grain 충돌: {column}")
    return {str(row[column]).strip(): row for row in frame.to_dict("records")}


def assemble_result(params: Mapping[str, Any], sources: dict) -> pd.DataFrame:
    assembly_started = time.perf_counter()
    timings = sources.setdefault('performance_ms', {})
    reference = date.fromisoformat(params["order_date"])
    conditions = OrderConditions(reference, int(params["safety_days"]),
                                 int(params["target_days"]), int(params["closing_day"]))
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
    price_payload = sources.get("prices", {})
    prices = price_payload.get("df", pd.DataFrame())
    price_truncated = price_payload.get("meta", {}).get("full_source_limit_hit", False)
    history_groups = _history_index(sources.get('order_history', pd.DataFrame()))
    price_groups = _groups(prices, ['제품코드', '매입처코드'])
    contract_groups = _groups(sources.get('contracts', {}).get('df', pd.DataFrame()), ['제품코드'])
    empty = pd.DataFrame()
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
        order_staff_filter = str(params.get("order_staff_nm") or "").strip()
        pharma_staff_filter = str(params.get("pharma_staff_nm") or "").strip()
        if order_staff_filter and order_staff_filter != order_staff_code and order_staff_filter not in order_staff_name:
            continue
        if pharma_staff_filter and pharma_staff_filter != pharma_staff_code and pharma_staff_filter not in pharma_staff_name:
            continue
        if params.get("order_vendor_cd") and params["order_vendor_cd"] != vendor_code:
            continue
        if params.get("order_vendor_nm") and params["order_vendor_nm"] not in vendor_name:
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
        if adjusted_plan is not None and adjusted_plan > 0 and basis and "부족" not in basis:
            plans[reference.strftime("%Y%m")] = adjusted_plan
        # Explicit future plans may be supplied by an existing adapter; never extend current plan.
        plans.update(sources.get("future_plans", {}).get(code, {}))
        hd, missing = demand_for_dates(horizon, reference_date=reference,
                                      business_dates=dates, monthly_forecast=plans,
                                      month_day_counts=month_day_counts, date_month_counts=horizon_month_counts)
        sd, safety_missing = demand_for_dates(safety, reference_date=reference,
                                             business_dates=dates, monthly_forecast=plans,
                                             month_day_counts=month_day_counts, date_month_counts=safety_month_counts)
        elapsed = sources.get("elapsed_days")
        daily = adjusted_plan / Decimal(month_days) if reference.strftime("%Y%m") in plans and month_days else None
        forecast_basis = basis
        basis = "예측기반" if reference.strftime("%Y%m") in plans else "수요근거 없음"
        fallback_reason = trend_reason
        if not plans and sources.get("normal_outbound_verified") and current_actual is not None and current_actual > 0 and elapsed:
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
        price_source = "단가없음"
        basis_selected = "real"
        blocked = False
        if params.get("cost_apply_cd") or params.get("cost_apply_nm"):
            contract_payload = sources.get("contracts", {})
            contracts = contract_payload.get("df", pd.DataFrame())
            if not basis_selected or contract_payload.get("meta", {}).get("full_source_limit_hit"):
                price_source, blocked = "계약단가 사용자확인", True
            elif not contracts.empty:
                candidates = contract_groups.get((code,), empty)
                if len(candidates) == 1:
                    column = {"real": "실입고단가", "book": "장부입고단가", "otc": "otc 입고단가"}[basis_selected]
                    price = decimal_or_none(candidates.iloc[0].get(column))
                    if price is not None and price > 0:
                        price_source = "단가적용처 계약단가"
                    else:
                        price, price_source, blocked = None, "종료/무효 계약단가 사용자확인", True
                elif len(candidates) > 1:
                    price_source, blocked = "복수 계약단가 사용자확인", True
        if price is None and not blocked and not prices.empty and not price_truncated:
            matched = price_groups.get((code, vendor_code), empty)
            if len(matched) and basis_selected in ("real", "book"):
                column = "실입고단가" if basis_selected == "real" else "장부입고단가"
                unique = {decimal_or_none(value) for value in matched[column]}
                if len(unique) == 1 and None not in unique:
                    price = unique.pop()
                    price_source = "대표매입처 최종매입가"
                else:
                    price_source, blocked = "복수 최종매입가 사용자확인", True
            elif len(matched):
                price_source, blocked = "최종매입가 basis 사용자확인", True
        if price is None and not blocked and not price_truncated:
            column = {"real": "제품등록실단가", "book": "제품등록장부단가"}.get(basis_selected)
            price = decimal_or_none(basic.get(column)) if column else None
            price_source = "제품등록단가" if price is not None else "단가없음/basis 사용자확인"
        elif price is None and price_truncated:
            price_source = "최종매입가 원천한도 사용자확인"
        if price is not None and price <= 0:
            price, price_source = None, "0/음수 단가 사용자확인"
        price_ms += (time.perf_counter() - price_started) * 1000
        row = {**basic, **quantity, "회사": params["company_id"], "발주일자": params["order_date"],
               "발주처코드": vendor_code, "발주처": vendor_name, "대표매입처출처": supplier.get("recent_inbound_vendor_source"),
               "재고수량": stock, "입고예정수량": pending_qty,
               "안전재고 기준수량": sd, "안전재고일수": conditions.safety_days,
               "적정재고일수": conditions.target_days, "결제일자": conditions.closing_day,
               "적용 horizon 영업일수": len(horizon), "horizon 예정수량": hd,
               "적용 필요 영업일수": len(horizon), "적용 필요예정수량": hd,
               "당월 정상출고수량": current_actual, "수요근거": basis,
               "월 기준 예상수량": adjusted_plan if basis == "예측기반" else None,
               "예측 산출근거": forecast_basis,
               "해당 월 전체 영업일수": month_days if calendar_ready else None,
               "경과 영업일수": elapsed, "기준 1영업일 예상수량": daily,
               "horizon 필요예정수량": hd,
               "수요 사용자확인월": ",".join(sorted(set(missing + safety_missing))),
               "발주단위": unit, "추세": "증가" if increasing else "유지/감소/증가근거 부족", "최근 발주횟수": len(selected_history),
               "당월 출고 거래처수": sources.get('current_customer_counts', {}).get(code, 0) if 'current_customer_counts' in sources else None,
               "발주단위 근거": unit_reason,
               "조달주의": '' if unit is not None else unit_reason, "발주단가": price, "단가출처": price_source,
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
    if params.get("only_needed") and not result.empty:
        result = result.loc[result["계산상태"].eq("발주해당")].copy()
    if params.get("query_mode") == "확인 필요" and not result.empty:
        result = result.loc[result["계산상태"].str.contains("확인") | result["발주단가"].isna() | result["발주처코드"].eq("")].copy()
    timings['only_needed_filter'] = (time.perf_counter() - filter_started) * 1000
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
            return {**snapshot, "action": ACTION, "title": ACTION, "table": TABLE}
        frame = assemble_result(q, sources)
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
    frame = frame.loc[:, [c for c in export_order if c in frame]]
    projection_started = time.perf_counter()
    display = frame.head(300).loc[:, [c for c in primary if c in frame]].copy()
    sources.setdefault('performance_ms', {})['display_projection'] = (time.perf_counter() - projection_started) * 1000
    summary = f"결과: 발주 계산 {len(frame):,}건 | 화면 {len(display):,}건 | Excel/CSV 전체 {len(frame):,}건\n수량은 추천이며 ERP 등록되지 않습니다."
    reference = date.fromisoformat(q["order_date"])
    calendar_dates = sources.get("business_dates", [])
    calendar_ready = sources.get("calendar_status") == "ready"
    month_days = len({d for d in calendar_dates if (d.year, d.month) == (reference.year, reference.month)})
    conditions = OrderConditions(reference, int(q["safety_days"]), int(q["target_days"]), int(q["closing_day"]))
    needed_days = len(horizon_dates(conditions, calendar_dates))
    mode = q.get('query_mode') or ('발주해당자료만' if q.get('only_needed') else '전체')
    payment = f"결제일 {conditions.closing_day}일" if conditions.closing_day else "결제일 현금/당일결제"
    query_summary = (f"발주일 {q['order_date']} | 조회구분 {mode}"
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
    calculation_basis = (f"계산기준: {reference.month}월 영업일 {month_days}일 | 적용 필요 {needed_days}영업일"
                         if calendar_ready else "계산기준: 공식 영업일 자료 확인 필요")
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
            "order_demand_contract": "monthly_forecast_full_business_days_v1",
            "order_demand_trend_contract": "completed_recent3_vs_previous3_deadband10_half_cap30_before_stock_pending_unit_v1",
            "summary_md": summary, "llm_summary_md": summary + "\n계산/추천/실제수량은 독립입니다. 단가와 수요 확인 필요 상태를 정상값으로 해석하지 마세요. ERP 발주 확정 또는 회계 확정손익이 아닙니다.",
            "order_calculation_editable": True, "query_summary": query_summary,
            "calculation_basis": calculation_basis + " | 입고예정 최근 4영업일",
            "order_header_metrics": {"needed_days": needed_days if calendar_ready else None,
                                     "query_mode": mode, "inbound_business_days": 4,
                                     "order_date": q["order_date"], "safety_days": conditions.safety_days,
                                     "target_days": conditions.target_days, "closing_day": conditions.closing_day,
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
            "settlement_rounding_status": "정산 반올림 미적용(원 계산 보존)", "full_source_ready": True}
    log.info("[order_calculation.query] company_id=%s rows=%s snapshot=%s source_call_count=%s elapsed_ms=%s",
             current, len(frame), meta.get("snapshot_manifest_id"), meta["source_call_count"],
             round((time.perf_counter() - started) * 1000, 1))
    payload = {"table": TABLE, "action": ACTION, "title": ACTION, "params": q, "df": frame,
            "df_display": display, "records": display.to_dict("records"), "columns": list(display),
            "data": summary, "message": summary, "meta": meta, "final": True}
    from app.services.snapshot_product_information_service import project_product_information_payload_for_viewer
    return project_product_information_payload_for_viewer(payload)


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
