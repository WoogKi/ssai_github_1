"""Read-only allocation of the existing order calculation, never ERP registration."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import logging
import time
from typing import Any

import pandas as pd

from app.db.mssql_client import get_current_company_id, query_to_df, read_only_request
from app.services import order_calculation_service as base
from app.services.erp_table_query_service import registered_query_limits
from app.services.order_calculation_contract import amounts
from app.services.saved_io_scope import resolve_saved_io_scope

ACTION = "병원발주계산"
TABLE = "hospital_order_calculation"
SELECTION = "allocation_stock_apply_codes"
POLICY = "hospital_allocation_v2_customer_code_first_contract_first_residual_50001"
SCOPED_POLICY = "hospital_allocation_selected_v1_no_automatic_residual"
RESIDUAL_STOCK_APPLY_CODE = "50001"
MAX_SELECTED_GROUPS = 20
REFERENCE_ATTR = "hospital_reference_records"
UNALLOCATED_ATTR = "hospital_unallocated_by_product"
RESIDUAL_TEMPLATES_ATTR = "hospital_residual_templates"
ORIGINAL_ATTR = "hospital_original_allocation_records"
ALLOCATION_KEY = (*base.ROW_KEY, "배분 재고적용처코드")
MASTER_COLUMNS = ("code", "name", "stock_apply", "cost_apply")


class AllocationError(ValueError):
    """A safe, non-sensitive reason that prevents a partial order proposal."""


class SourceContractError(AllocationError):
    def __init__(self, reason: str, source: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.source = source


class MasterError(AllocationError):
    def __init__(self, reason: str, message: str, *, query_failed: bool = False):
        super().__init__(message)
        self.reason = reason
        self.query_failed = query_failed


def _company(company: Any) -> int:
    current = get_current_company_id()
    if current is None or company is None or int(current) != int(company):
        raise AllocationError("현재 회사와 병원발주계산 회사가 일치하지 않습니다.")
    return int(current)


def _text(value: Any) -> str:
    return "" if value is None or pd.isna(value) else str(value).strip()


def _quantity(value: Any, *, nonnegative: bool = False) -> Decimal:
    number = base.decimal_or_none(value)
    if number is None or not number.is_finite() or number != number.to_integral_value():
        raise AllocationError("수량이 누락되었거나 유한한 정수가 아닙니다.")
    if nonnegative and number < 0:
        raise AllocationError("실제 발주수량은 음수일 수 없습니다.")
    return number


def _parent_quantity(value: Any, row_number: int) -> Decimal:
    if value is None or value is pd.NA or isinstance(value, str) and not value.strip():
        reason = 'missing'
    else:
        try:
            number = Decimal(str(value))
            reason = ('non_finite' if not number.is_finite() else
                      'fractional' if number != number.to_integral_value() else
                      'negative' if number < 0 else '')
        except (InvalidOperation, ValueError, TypeError):
            reason = 'not_numeric'
    if reason:
        raise SourceContractError(f'parent_quantity_{reason}', 'parent',
                                  f'원계산 결과 {row_number}행의 실제 발주수량이 유효하지 않습니다 ({reason}). '
                                  '해당 행의 계산상태와 수량 원천을 확인하세요.')
    return number


def normalize_selection(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise AllocationError("배분 재고적용처는 코드 목록으로 지정하세요.")
    return tuple(sorted(set(v.strip() for v in value)))


def validate_master(frame: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or not set(MASTER_COLUMNS).issubset(frame):
        raise MasterError('master_shape_invalid', "거래처 마스터 조회 결과의 필수 컬럼이 없습니다. 데이터 없음이 아닌 조회 계약 오류입니다.")
    work = frame.loc[:, MASTER_COLUMNS].copy()
    for column in MASTER_COLUMNS:
        work[column] = work[column].map(_text)
    if work.empty or work.code.eq("").any():
        raise MasterError('master_identity_missing', "거래처 마스터가 비어 있거나 식별키가 누락되었습니다.")
    if work.code.duplicated().any():
        raise MasterError('master_identity_duplicate', "거래처 마스터 식별키가 중복됩니다. 임의로 첫 행을 사용하지 않았습니다.")
    return work.sort_values("code", kind="stable")


def _master_scope(master: pd.DataFrame, selected=(), *, relevant: set[str] | None = None):
    """Shared identity contract for the panel, NLQ preflight and allocation."""
    work = validate_master(master)
    residual = RESIDUAL_STOCK_APPLY_CODE
    if residual not in set(work.code):
        raise MasterError('residual_missing', "현재 회사 R030에 기본 잔여 배정처코드 50001이 없습니다. 다른 코드로 대체하지 않았습니다.")
    available = set(work.stock_apply) - {""}
    if residual not in available:
        raise MasterError('residual_members_missing', "현재 회사 R030에서 기본 잔여 배정처코드 50001의 재고적용처 관계를 확인하지 못했습니다.")
    chosen = set(normalize_selection(selected))
    if chosen - available:
        raise AllocationError("선택한 배분 재고적용처가 현재 회사의 관계 목록에 없습니다.")
    targets = chosen or (available if relevant is None else set(relevant))
    missing = (targets | {residual}) - set(work.code)
    if missing:
        raise MasterError('stock_identity_missing',
            f"현재 회사 R030의 재고적용처 참조 {len(missing)}개에 대응하는 거래처 마스터가 없습니다. ERP의 재고적용처 코드 관계를 확인하세요. 임의 제외·대체하지 않았습니다.")
    return work, targets, residual


def resolve_groups(master: pd.DataFrame, selected=(), *, relevant: set[str] | None = None) -> tuple[dict[str, dict], str]:
    work, targets, residual = _master_scope(master, selected, relevant=relevant)
    identities = work.set_index("code")
    groups = {}
    for code in sorted(targets | {residual}):
        members = work.loc[work.stock_apply.eq(code)]
        if members.empty:
            raise MasterError('residual_members_missing', "잔여 배정처의 단가적용처 관계를 확인해야 합니다.")
        # User-approved policy: first CUSTOMER, not smallest cost code or cheapest contract.
        first = members.iloc[0]
        cost = first.cost_apply
        if not cost or cost not in identities.index:
            raise MasterError('first_cost_identity_missing', "첫 거래처의 단가적용처가 공백이거나 등록되지 않았습니다.")
        groups[code] = {"name": identities.at[code, "name"], "cost_apply": cost,
                        "selected": code in targets}
    return groups, residual


def _totals(frame: pd.DataFrame, products: set[str], master: pd.DataFrame,
            *, source: str, company: int | None = None) -> dict:
    if source not in ('sales', 'orders'):
        raise SourceContractError('source_invalid', source, "원천 집계 종류를 확인해야 합니다.")
    required = {"product", "stock_apply", "quantity", "bonus", "source_rows"}
    if not isinstance(frame, pd.DataFrame) or not required.issubset(frame):
        raise SourceContractError('shape_invalid', source, "제품별 적용처 집계 자료가 불완전합니다.")
    work = validate_master(master)
    identities = set(work.code)
    referenced = set(work.stock_apply) - {""}
    result = {}
    for row in frame.to_dict("records"):
        if source == 'sales' and _quantity(row.get('missing_customer_rows', 0), nonnegative=True) > 0:
            raise SourceContractError('sales_customer_missing', source, "전월 매출처의 거래처 마스터가 없습니다.")
        key = (_text(row["product"]), _text(row["stock_apply"]))
        reason = ('product_out_of_scope' if key[0] not in products else
                  'stock_apply_blank' if not key[1] else
                  'stock_identity_missing' if key[1] not in identities else
                  'stock_reference_missing' if source == 'sales' and key[1] not in referenced else
                  'normalized_key_duplicate' if key in result else '')
        if reason:
            if company is not None and reason in ('stock_identity_missing', 'stock_reference_missing'):
                source_groups = {_text(value) for value in frame['stock_apply']}
                logging.getLogger('ssai').warning(
                    '[hospital.source.reference] company_id=%s source=%s stage=validation reason=%s '
                    'source_group_count=%s master_group_count=%s missing_identity_count=%s '
                    'unreferenced_group_count=%s relevant_product_count=%s',
                    company, {'sales': 'R120', 'orders': 'R170_R180'}.get(source, source), reason,
                    len(source_groups), len(referenced), len(source_groups - identities),
                    len((source_groups & identities) - referenced), len(products))
            raise SourceContractError(reason, source, "집계 제품/재고적용처가 누락·중복되었거나 조회범위를 벗어났습니다.")
        if _quantity(row["source_rows"], nonnegative=True) <= 0:
            raise SourceContractError('source_rows_invalid', source, "집계 원천 행수가 유효하지 않습니다.")
        _quantity(row["bonus"])
        if row.get("ambiguous_locations", 0):
            raise SourceContractError('ambiguous_locations', source, "발주 재고위치의 복수 매핑을 확인해야 합니다.")
        result[key] = _quantity(row["quantity"])
    return result


def _allocation_groups(master: pd.DataFrame, selected, previous: dict, placed: dict):
    """Keep order-only R180 identities without making them new allocation groups."""
    work = validate_master(master)
    referenced = set(work.stock_apply) - {""}
    historical = {group for _, group in placed if group not in referenced}
    sales_groups = {group for _, group in previous}
    chosen = set(normalize_selection(selected))
    reference_only = {group for _, group in placed if group in historical or
                      (group not in sales_groups and group != RESIDUAL_STOCK_APPLY_CODE
                       and group not in chosen)}
    for (product, group), quantity in placed.items():
        if group in reference_only and quantity < 0:
            raise SourceContractError('historical_order_negative', 'orders',
                                      "기발주 전용 그룹의 음수 수량은 별도 확인이 필요합니다.")
    relevant = {group for _, group in (*previous, *placed)} - reference_only
    groups, residual = resolve_groups(work, selected, relevant=relevant)
    return groups, residual, reference_only, historical


def allocate(parents: pd.DataFrame, *, company: int, master: pd.DataFrame,
             sales: pd.DataFrame, orders: pd.DataFrame, contracts: dict,
             selected=(), limit: int = 100000) -> pd.DataFrame:
    required = set(base.ROW_KEY) | {"실제 발주수량", "발주단가"}
    if not required.issubset(parents) or parents.duplicated(list(base.ROW_KEY)).any():
        raise AllocationError("제품별 원계산 식별키가 누락되거나 중복되었습니다.")
    if not parents["회사"].eq(company).all() or parents["제품코드"].map(_text).eq("").any():
        raise AllocationError("제품별 원계산의 회사/제품을 확인해야 합니다.")
    if parents["제품코드"].duplicated().any() or parents["발주일자"].nunique() > 1:
        raise AllocationError("부모 제품 또는 계산 기준일이 중복되었습니다.")
    products = set(parents["제품코드"].map(_text))
    previous = _totals(sales, products, master, source='sales', company=company)
    placed = _totals(orders, products, master, source='orders', company=company)
    selected = normalize_selection(selected)
    groups, residual, reference_only, _ = _allocation_groups(master, selected, previous, placed)
    eligible = ({product for (product, code), quantity in previous.items()
                 if code in selected and quantity > 0} if selected else products)
    identities = validate_master(master).set_index('code')
    for code in reference_only:
        groups[code] = {"name": identities.at[code, 'name'], "cost_apply": "",
                        "selected": False, "historical": True}
    active_by_product: dict[str, set[str]] = {}
    for product, code in previous.keys() | placed.keys():
        active_by_product.setdefault(product, set()).add(code)
    result = []
    unallocated = {}
    residual_templates = {}
    for row_number, parent in enumerate(parents.to_dict("records"), start=1):
        product = _text(parent["제품코드"])
        if product not in eligible:
            continue
        if parent.get("계산상태") == "수요/재고 사용자확인":
            review_value = parent["실제 발주수량"]
            if review_value is None or review_value is pd.NA or _parent_quantity(review_value, row_number) == 0:
                continue
            raise AllocationError("사용자 확인 제품에 확정되지 않은 양수 발주수량이 있습니다.")
        total = _parent_quantity(parent["실제 발주수량"], row_number)
        if total == 0:
            if selected:
                unallocated[product] = Decimal(0)
            continue
        fallback = base.decimal_or_none(parent["발주단가"])
        if fallback is not None and (not fallback.is_finite() or fallback < 0):
            raise AllocationError("원계산 발주단가가 유효하지 않습니다.")
        details = {}
        active_codes = (active_by_product.get(product, set()) & groups.keys()) | {residual}
        for code in sorted(active_codes):
            group = groups[code]
            price = None if group.get('historical') else contracts.get((product, group["cost_apply"]))
            if price is not None:
                price = base.decimal_or_none(price)
                if price is None or not price.is_finite() or price <= 0:
                    raise AllocationError("유효한 양수 계약단가가 아닙니다.")
            last = previous.get((product, code), Decimal(0))
            current = placed.get((product, code), Decimal(0))
            details[code] = {"last": last, "current": current, "need": last - current,
                             "contract": price, "price": None if group.get('historical') else
                             (price if price is not None else fallback),
                             "allocated": Decimal(0), "residual": Decimal(0)}
        priority = sorted((c for c in details if groups[c]["selected"] and (selected or c != residual)),
                          key=lambda c: ("재고적용" in groups[c]["name"],
                                         details[c]["contract"] is None,
                                         details[c]["contract"] or Decimal(0), c))
        remaining = total
        for code in priority:
            detail = details[code]
            detail["allocated"] = min(remaining, max(Decimal(0), detail["need"]))
            remaining -= detail["allocated"]
        if selected:
            unallocated[product] = remaining
        else:
            details[residual]["allocated"] += remaining
            details[residual]["residual"] = remaining
        display_order = priority + ([] if selected or residual in priority else [residual]) + sorted(reference_only & details.keys())
        start = len(result)
        for code in display_order:
            detail = details[code]
            group = groups[code]
            row = {k: parent.get(k) for k in (*base.ROW_KEY, "제품명", "규격", "발주처", "발주단위")}
            row.update({"실제 발주수량": detail["allocated"], "배분 재고적용처코드": code,
                        "배분 재고적용처": group["name"], "단가적용처코드": group["cost_apply"],
                        "지난달매출수량": detail["last"], "이번달발주수량": detail["current"],
                        "현재발주수량": detail["need"], "잔여배정수량": detail["residual"],
                        "선택외 잔여배정": not group["selected"] and code == residual,
                        "발주단가": detail["price"],
                        "단가출처": "과거 기발주 참고" if group.get('historical') else
                        ("계약단가" if detail["contract"] is not None else "기존 발주 계산 단가"),
                        "단가판정": "해당없음" if group.get('historical') else
                        ("미확정" if detail["price"] is None else "확정"),
                        "배분정책": SCOPED_POLICY if selected else POLICY})
            row.update(amounts(detail["allocated"], detail["price"]))
            if code == residual and not selected:
                residual_templates[product] = row.copy()
            if detail["allocated"] == 0 and detail["need"] >= 0 and not (total == 0 and code == residual):
                continue
            result.append(row)
            if len(result) > limit:
                raise AllocationError("병원 배분 결과가 전체 원본 안전 한도를 초과했습니다.")
        if selected and not any(row['실제 발주수량'] > 0 for row in result[start:]):
            del result[start:]
        allocated = sum((r["실제 발주수량"] for r in result[start:]), Decimal(0))
        if allocated + unallocated.get(product, Decimal(0)) != total:
            raise AllocationError("제품별 원계산과 실제 배분 합계가 일치하지 않습니다.")
    frame = pd.DataFrame(result)
    frame.attrs[UNALLOCATED_ATTR] = unallocated
    frame.attrs[RESIDUAL_TEMPLATES_ATTR] = residual_templates
    frame.attrs['hospital_eligible_products'] = eligible
    if not frame.empty and frame.duplicated(list(ALLOCATION_KEY)).any():
        raise AllocationError("배분 식별키가 중복되었습니다.")
    return frame


def load_master(company: int, *, timeout_seconds: int = 120) -> pd.DataFrame:
    _company(company)
    limit = registered_query_limits({}).source_top
    try:
        with read_only_request(timeout_seconds=timeout_seconds):
            frame = query_to_df(f"""SELECT TOP {limit + 1}
            RTRIM(Rd03_Ven_Cd) AS code, RTRIM(Rd03_Ven_Nm) AS name,
            RTRIM(Rd03_Stock_Apply_Cd) AS stock_apply, RTRIM(Rd03_Cost_Apply_Cd) AS cost_apply
            FROM dbo.Rddbc030 ORDER BY Rd03_Ven_Cd""")
            if len(frame) > limit:
                frame = query_to_df(_compact_master_sql(limit))
    except Exception as exc:
        # SQLSTATE only determines the category; raw driver/connection text is never exposed.
        timeout = isinstance(exc, TimeoutError) or type(exc).__name__ == 'TimeoutError' or any(
            state in str(exc) for state in ('HYT00', 'HYT01'))
        denied = any(state in str(exc) for state in ('28000', '42501', '(229)'))
        reason = 'master_query_timeout' if timeout else 'master_permission_denied' if denied else 'master_query_failed'
        logging.getLogger('ssai').warning('[hospital.master] company_id=%s reason=%s error_type=%s',
                                         company, reason, type(exc).__name__)
        message = ("재고적용처 마스터 조회 시간이 초과되었습니다. 데이터 없음으로 판단하지 않았습니다." if timeout else
                   "재고적용처 마스터 DB 인증·조회 권한이 거부되었습니다. 권한을 확인하세요." if denied else
                   "재고적용처 마스터 조회에 실패했습니다. 연결·권한을 확인하세요. 데이터 없음으로 판단하지 않았습니다.")
        raise MasterError(reason, message, query_failed=True) from exc
    _company(company)
    if len(frame) > limit:
        raise MasterError('master_limit_exceeded', "거래처 관계 조회가 안전 한도를 초과했습니다. 부분 자료로 계산하지 않았습니다.")
    if 'master_rows' in frame:
        required = {'master_rows', 'unique_codes', 'blank_codes', 'reference_codes'}
        if frame.empty or not required.issubset(frame) or frame[list(required)].isna().any().any():
            raise MasterError('master_compact_invalid', "거래처 관계 축약 조회의 완전성을 확인하지 못했습니다.")
        stats = frame.iloc[0]
        if any(frame[key].nunique() != 1 for key in required):
            raise MasterError('master_compact_invalid', "거래처 관계 축약 조회의 원천 집계가 일치하지 않습니다.")
        if int(stats.master_rows) != int(stats.unique_codes) or int(stats.blank_codes) != 0:
            raise MasterError('master_identity_duplicate', "거래처 마스터 식별키가 누락되거나 중복됩니다.")
        work = validate_master(frame)
        if len(set(work.stock_apply) - {''}) != int(stats.reference_codes):
            raise MasterError('master_compact_invalid', "재고적용처 참조 종류가 원천 집계와 일치하지 않습니다.")
        logging.getLogger('ssai').info('[hospital.master.compact] company_id=%s master_rows=%s projected_rows=%s reference_codes=%s',
                                      company, int(stats.master_rows), len(work), int(stats.reference_codes))
        return work
    return validate_master(frame)


def _compact_master_sql(limit: int) -> str:
    # The first customer and every referenced identity are retained; global counts detect hidden duplicates.
    return f"""WITH Master AS (
        SELECT LTRIM(RTRIM(Rd03_Ven_Cd)) COLLATE Latin1_General_100_BIN2 AS code,
               LTRIM(RTRIM(Rd03_Ven_Nm)) AS name,
               LTRIM(RTRIM(Rd03_Stock_Apply_Cd)) COLLATE Latin1_General_100_BIN2 AS stock_apply,
               LTRIM(RTRIM(Rd03_Cost_Apply_Cd)) COLLATE Latin1_General_100_BIN2 AS cost_apply
        FROM dbo.Rddbc030
    ), FirstMember AS (
        SELECT code, stock_apply, cost_apply,
               ROW_NUMBER() OVER (PARTITION BY stock_apply ORDER BY code) AS member_rank
        FROM Master WHERE stock_apply IS NOT NULL AND stock_apply <> ''
    ), Needed AS (
        SELECT code FROM FirstMember WHERE member_rank = 1
        UNION SELECT stock_apply FROM FirstMember WHERE member_rank = 1
        UNION SELECT cost_apply FROM FirstMember WHERE member_rank = 1 AND cost_apply <> ''
        UNION SELECT code FROM Master WHERE code = '{RESIDUAL_STOCK_APPLY_CODE}'
    ), Counts AS (
        SELECT COUNT_BIG(*) AS master_rows, COUNT_BIG(DISTINCT code) AS unique_codes,
               SUM(CASE WHEN code IS NULL OR code = '' THEN CONVERT(bigint, 1) ELSE 0 END) AS blank_codes,
               COUNT_BIG(DISTINCT CASE WHEN stock_apply <> '' THEN stock_apply END) AS reference_codes
        FROM Master
    )
    SELECT TOP {limit + 1} M.code, M.name, M.stock_apply, M.cost_apply,
           C.master_rows, C.unique_codes, C.blank_codes, C.reference_codes
    FROM Master AS M CROSS JOIN Counts AS C
    WHERE M.code IN (SELECT code FROM Needed)
    ORDER BY M.code"""


def group_options(company: int, *, timeout_seconds: int = 120) -> dict[str, str]:
    master = load_master(company, timeout_seconds=timeout_seconds)
    return _master_options(master)


def _master_options(master: pd.DataFrame) -> dict[str, str]:
    work, _, _ = _master_scope(master, relevant=set())
    targets = (set(work.stock_apply) - {""}) & set(work.code)
    identities = dict(zip(work.code, work.name))
    return {code: identities[code] for code in sorted(targets)}


def build_aggregate_queries(params: dict, products: list[str], reference: date, limit: int):
    from app.services import rddbc170_rddbc180_order_service as order
    stocks = list(params.get("stock_cd_list") or [])
    io = resolve_saved_io_scope(params.get("io_gu_list") or []).outbound_codes
    if not stocks or not io or not products:
        raise AllocationError("원계산의 재고위치/정상출고/제품 범위가 비어 있습니다.")
    month = reference.replace(day=1)
    previous = (month - timedelta(days=1)).replace(day=1)
    # Match the existing bounded-IN / full-authority-post-filter approach.
    scoped = len(products) <= 1000
    product_sql = " AND O.Rd12_Physic_Cd IN (" + ",".join("?" for _ in products) + ")" if scoped else ""
    sales_sql = f"""SELECT TOP {limit + 1} RTRIM(O.Rd12_Physic_Cd) AS product,
        RTRIM(V.Rd03_Stock_Apply_Cd) AS stock_apply,
        SUM(O.Rd12_Quantity) AS quantity, SUM(O.Rd12_Oquantity) AS bonus, COUNT_BIG(*) AS source_rows,
        SUM(CASE WHEN V.Rd03_Ven_Cd IS NULL THEN 1 ELSE 0 END) AS missing_customer_rows
        FROM dbo.Rddbc120 AS O
        LEFT JOIN dbo.Rddbc030 AS V ON V.Rd03_Ven_Cd = O.Rd12_Ven_Cd
        WHERE O.Rd12_Out_YyMmDd >= ? AND O.Rd12_Out_YyMmDd < ?
          AND O.Rd12_Io_Gu_Gcode = '0012'
          AND O.Rd12_Io_Gu IN ({','.join('?' for _ in io)})
          AND O.Rd12_Stock_Cd_Gcode = '0018'
          AND O.Rd12_Stock_Cd IN ({','.join('?' for _ in stocks)}) {product_sql}
        GROUP BY O.Rd12_Physic_Cd, V.Rd03_Stock_Apply_Cd
        ORDER BY O.Rd12_Physic_Cd, V.Rd03_Stock_Apply_Cd"""
    sales_values = [previous.strftime('%Y%m%d'), month.strftime('%Y%m%d'), *io, *stocks,
                    *(products if scoped else [])]
    clauses = [order._NORMAL_ORDER_PREDICATE, "D.Rd18_Or_Di IN ('1','2','3')",
               "H.Rd17_Or_YyMmDd >= ?", "H.Rd17_Or_YyMmDd <= ?"]
    values = [month.strftime('%Y%m%d'), reference.strftime('%Y%m%d')]
    order._append_order_stock_filter(clauses, values, params)
    if scoped:
        clauses.append("D.Rd18_Physic_Cd IN (" + ",".join("?" for _ in products) + ")")
        values.extend(products)
    orders_sql = f"""SELECT TOP {limit + 1} RTRIM(D.Rd18_Physic_Cd) AS product,
        RTRIM(D.Rd18_Stock_Apply_Cd) AS stock_apply,
        SUM(D.Rd18_Quantity) AS quantity, SUM(D.Rd18_Oquantity) AS bonus, COUNT_BIG(*) AS source_rows,
        SUM(CASE WHEN OrderLocation.match_count > 1 THEN 1 ELSE 0 END) AS ambiguous_locations
        {order._ORDER_PRICE_HISTORY_MINIMAL_JOINS}
        {order._ORDER_STOCK_APPLIES}
        WHERE {' AND '.join(clauses)}
        GROUP BY D.Rd18_Physic_Cd, D.Rd18_Stock_Apply_Cd
        ORDER BY D.Rd18_Physic_Cd, D.Rd18_Stock_Apply_Cd"""
    orders_sql, values = order._bind_saved_order_locations(orders_sql, values, params)
    if max(len(sales_values), len(values)) > 2068:
        raise AllocationError("원천 조회의 SQL 파라미터 안전 한도를 초과했습니다.")
    return ((sales_sql, sales_values), (orders_sql, values))


def load_allocation_sources(params: dict, parents: pd.DataFrame, reference: date, selected,
                            *, master: pd.DataFrame | None = None) -> dict:
    from app.services.rddbc070_service import get_rddbc070_current_df
    company = _company(params.get("company_id"))
    started = time.perf_counter()
    master = load_master(company) if master is None else master
    products = sorted(set(parents["제품코드"].map(_text)))
    limits = registered_query_limits(params)
    frames = []
    query_ms = 0.0
    with read_only_request(timeout_seconds=120):
        for sql, values in build_aggregate_queries(params, products, reference, limits.source_top):
            _company(company)
            query_started = time.perf_counter()
            frame = query_to_df(sql, tuple(values))
            query_ms += (time.perf_counter() - query_started) * 1000
            if len(frame) > limits.source_top:
                raise AllocationError("당월/전월 집계가 안전 한도를 초과해 부분 계산하지 않았습니다.")
            frame = frame.loc[frame['product'].map(_text).isin(products)].copy()
            frames.append(frame)
        previous = _totals(frames[0], set(products), master, source='sales', company=company)
        placed = _totals(frames[1], set(products), master, source='orders', company=company)
        groups, residual, _, historical = _allocation_groups(master, selected, previous, placed)
        validated_ms = (time.perf_counter() - started) * 1000
        unused_orphans = (set(master.stock_apply) - {""}) - set(master.code)
        if unused_orphans:
            logging.getLogger('ssai').warning(
                '[hospital.master.unused_orphans] company_id=%s codes=%s', company, len(unused_orphans))
        orderable_products = set()
        for row_number, parent in enumerate(parents.to_dict('records'), start=1):
            value = parent['실제 발주수량']
            if parent.get('계산상태') == '수요/재고 사용자확인' and (value is None or value is pd.NA):
                continue
            quantity = _parent_quantity(value, row_number)
            if parent.get('계산상태') == '수요/재고 사용자확인':
                if quantity > 0:
                    raise AllocationError('사용자 확인 제품에 확정되지 않은 양수 발주수량이 있습니다.')
                continue
            if quantity > 0:
                orderable_products.add(_text(parent['제품코드']))
        by_cost: dict[str, set[str]] = (
            {groups[residual]['cost_apply']: set(orderable_products)} if orderable_products else {})
        for product, group in previous.keys() | placed.keys():
            if (product in orderable_products and group in groups and
                    previous.get((product, group), 0) - placed.get((product, group), 0) > 0):
                by_cost.setdefault(groups[group]['cost_apply'], set()).add(product)
        contracts = {}
        contract_rows = 0
        for cost, codes in sorted(by_cost.items()):
            _company(company)
            query = base.build_contract_price_params({"cost_apply_cd": cost,
                "order_date": reference.isoformat(), "order_product_code_list": sorted(codes)})
            frame = get_rddbc070_current_df(query)
            contract_rows += len(frame)
            if frame.attrs.get('full_source_limit_hit') or len(frame) >= limits.source_top or contract_rows > limits.export_limit_rows:
                raise AllocationError("계약단가 원천의 완전성/누적 안전 한도를 확인해야 합니다.")
            if frame.empty:
                continue
            if frame.duplicated(["단가적용거래처", "제품코드"]).any():
                raise AllocationError("제품별 유효 계약단가가 중복되었습니다.")
            contracts.update(base._contract_purchase_price_map(frame))
    logging.getLogger('ssai').info(
        '[hospital.perf] company_id=%s stage=source_load sql_ms=%.1f validation_ms=%.1f total_ms=%.1f '
        'parent_products=%s orderable_products=%s cost_groups=%s contract_rows=%s',
        company, query_ms, validated_ms - query_ms, (time.perf_counter() - started) * 1000,
        len(products), len(orderable_products), len(by_cost), contract_rows)
    _company(company)
    return {"master": master, "sales": frames[0], "orders": frames[1], "contracts": contracts,
            "audit": {"sales_rows": int(frames[0].source_rows.sum()),
                      "order_rows": int(frames[1].source_rows.sum()),
                      "sales_quantity": str(sum(previous.values(), Decimal(0))),
                      "order_quantity": str(sum(placed.values(), Decimal(0))),
                      "sales_bonus": str(sum((_quantity(v) for v in frames[0].bonus), Decimal(0))),
                      "order_bonus": str(sum((_quantity(v) for v in frames[1].bonus), Decimal(0))),
                      "historical_order_groups": len(historical),
                      "historical_order_quantity": str(sum((quantity for (product, group), quantity in placed.items()
                                                              if group in historical), Decimal(0))),
                      "contract_rows": contract_rows,
                      "unused_orphan_codes": len(unused_orphans)}}


def _notice(params: dict, message: str, *, skipped: bool = False) -> dict:
    return {"action": ACTION, "title": ACTION, "table": TABLE, "params": dict(params),
            "data": message, "message": message, "final": True,
            "meta": {"result_status": "input_required", "tableless_result": True,
                     "service_call_skipped": skipped, "row_count": 0, "row_count_total": 0}}


def validate_parent_stock_application(params: dict) -> dict:
    """Validate the submitted pair before the parent can load calculation sources."""
    company = _company(params.get('company_id'))
    q = dict(params)
    code, name = (_text(q.get('stock_apply_' + suffix)) for suffix in ('cd', 'nm'))
    if name and not code:
        raise AllocationError("부모 발주 계산 재고적용처코드가 없습니다. 정확한 코드를 입력하거나 코드와 명칭을 모두 비워 기존 기본값을 사용하세요. 배분 대상은 병원별 배분 재고적용처에서 선택하세요.")
    q.update(stock_apply_cd=code, stock_apply_nm=name)
    q = base.apply_application_defaults(q)
    code = q['stock_apply_cd']
    with read_only_request(timeout_seconds=15):
        identities = base.load_application_names([code])
    _company(company)
    if (not {'code', 'name'}.issubset(identities)
            or len(identities) != 1
            or _text(identities.iloc[0]['code']) != code
            or not _text(identities.iloc[0]['name'])):
        raise AllocationError("현재 회사에서 부모 재고적용처코드와 명칭을 유일하게 확인하지 못했습니다. 코드를 확인하세요. 자동 대체하지 않았습니다.")
    canonical_name = _text(identities.iloc[0]['name'])
    if name and name != canonical_name:
        raise AllocationError("부모 재고적용처코드와 명칭이 일치하지 않습니다. 정확한 조합으로 수정하거나 명칭을 비우고 코드만 입력하세요. 자동 변경하지 않았습니다.")
    q['stock_apply_nm'] = canonical_name
    return q


def get_hospital_order_calculation_result(params=None, *, source_loader=base.load_sources,
                                          allocation_loader=load_allocation_sources):
    started = time.perf_counter()
    q = dict(params or {})
    if q.get('_display_context') == 'chat':
        from app.services.ssai_permission_policy import get_required_permission
        from app.ui.ssai_login import has_permission
        if not has_permission(get_required_permission(action=ACTION)):
            result = _notice(q, "병원발주계산 조회 권한이 없습니다.", skipped=True)
            result['meta']['result_status'] = 'permission_denied'
            return result
        if q.get('_hospital_nlq_error'):
            return _notice(q, q['_hospital_nlq_error'], skipped=True)
    elif q.get('_display_context') != 'panel':
        return _notice(q, "병원발주계산 실행 경로를 확인하세요.", skipped=True)
    company = _company(q.get('company_id', get_current_company_id()))
    q['company_id'] = company
    selected = ()
    try:
        selected = normalize_selection(q.pop(SELECTION, None))
        q = validate_parent_stock_application(q)
        master = load_master(company, timeout_seconds=15)
        if q.get('_display_context') == 'chat':
            names = q.pop('_allocation_stock_apply_names', [])
            if selected or names:
                options = _master_options(master)
                resolved = list(selected)
                if set(selected) - set(options):
                    raise AllocationError("배분 재고적용처코드를 현재 회사에서 확인하지 못했습니다.")
                for name in names:
                    matches = [code for code, label in options.items() if label == name]
                    if not matches:
                        matches = [code for code, label in options.items() if name in label]
                    if len(matches) > MAX_SELECTED_GROUPS:
                        candidates = ', '.join(f"{code} {options[code]}" for code in matches[:5])
                        remaining = f" 외 {len(matches) - 5}개" if len(matches) > 5 else ""
                        raise AllocationError(
                            f"배분 재고적용처 '{name}'에 해당하는 그룹이 {len(matches)}개입니다. "
                            f"후보: {candidates}{remaining}. 범위가 넓으므로 명칭을 좁히거나 코드를 지정하세요.")
                    if not matches:
                        work = validate_master(master)
                        customers = work.loc[work.name.eq(name)]
                        if customers.empty:
                            customers = work.loc[work.name.str.contains(name, regex=False)]
                        if not customers.empty:
                            raise AllocationError(
                                f"'{name}'은 현재 회사 R030 거래처명으로 확인되지만 배분 재고적용처 그룹명은 아닙니다. "
                                "거래처의 재고적용처로 자동 변환하지 않았습니다. 그룹 코드 또는 명칭을 확인하세요.")
                        raise AllocationError(
                            f"'{name}'에 해당하는 현재 회사 배분 재고적용처 그룹을 찾지 못했습니다. "
                            "정확한 그룹 코드 또는 명칭을 확인하세요.")
                    resolved.extend(matches)
                selected = normalize_selection(resolved)
                if len(selected) > MAX_SELECTED_GROUPS:
                    raise AllocationError(f"배분 재고적용처가 {len(selected)}개 선택되었습니다. 범위를 좁혀 {MAX_SELECTED_GROUPS}개 이하로 지정하세요.")
        preflight_groups, residual = resolve_groups(master, selected, relevant=set())
        if q.get('_display_context') == 'chat':
            from app.services.order_stock_location_scope import prepare_order_stock_scope
            q, stock_error, _ = prepare_order_stock_scope(q, saved_only=True)
            if stock_error:
                raise AllocationError("재고위치를 유일하게 확인하지 못했습니다. 현재 회사의 저장 재고위치와 정확한 코드·명칭을 확인하세요.")
        _company(company)
    except MasterError as exc:
        logging.getLogger('ssai').info('[hospital.master.preflight] company_id=%s reason=%s parent_calls=0',
                                      company, exc.reason)
        result = _notice({**q, SELECTION: list(selected)}, str(exc), skipped=True)
        result['meta'].update(master_reason=exc.reason,
                              result_status='query_error' if exc.query_failed else 'input_required')
        return result
    except AllocationError as exc:
        return _notice({**q, SELECTION: list(selected)}, str(exc), skipped=True)
    captured = {}

    def capture(query):
        sources = source_loader(query)
        captured.update(sources.get('source_params') or query)
        return sources

    parent_payload = base.get_order_calculation_result(q, source_loader=capture)
    logging.getLogger('ssai').info('[hospital.perf] company_id=%s stage=parent elapsed_ms=%.1f parent_status=%s',
                                  company, (time.perf_counter() - started) * 1000,
                                  parent_payload.get('meta', {}).get('result_status'))
    if parent_payload.get('meta', {}).get('result_status') != 'success':
        return {**parent_payload, 'action': ACTION, 'title': ACTION, 'table': TABLE,
                'meta': {**parent_payload.get('meta', {}), 'order_calculation_editable': False}}
    parents = parent_payload.get('df')
    if not isinstance(parents, pd.DataFrame) or parents.empty:
        return {**parent_payload, 'action': ACTION, 'title': ACTION, 'table': TABLE,
                'meta': {**parent_payload.get('meta', {}), 'order_calculation_editable': False}}
    reference = date.fromisoformat(parent_payload['meta']['order_header_metrics']['effective_business_date'])
    # The wrapper source loader must not bypass the parent's saved-location preparation.
    captured['company_id'] = company
    stage = 'source_load'
    try:
        _company(company)
        if allocation_loader is load_allocation_sources:
            sources = allocation_loader(captured, parents, reference, selected, master=master)
        else:
            sources = allocation_loader(captured, parents, reference, selected)
        stage = 'allocation'
        allocation_started = time.perf_counter()
        frame = allocate(parents, company=company, selected=selected,
                         limit=registered_query_limits(q).export_limit_rows,
                         **{k: sources[k] for k in ('master', 'sales', 'orders', 'contracts')})
        logging.getLogger('ssai').info('[hospital.perf] company_id=%s stage=allocation parent_rows=%s allocation_rows=%s elapsed_ms=%.1f',
                                      company, len(parents), len(frame), (time.perf_counter() - allocation_started) * 1000)
    except AllocationError as exc:
        result = _notice({**q, SELECTION: list(selected)}, str(exc))
        reason = getattr(exc, 'reason', 'allocation_validation_failed')
        logging.getLogger('ssai').warning('[hospital.allocation] company_id=%s stage=%s reason=%s',
                                         company, stage, reason)
        result['meta'].update(allocation_stage=stage, allocation_reason=reason)
        if isinstance(exc, SourceContractError):
            result['meta'].update(source_contract_source=exc.source, source_contract_reason=exc.reason)
        return result
    _company(company)
    assembly_started = time.perf_counter()
    parent_rows = parents.to_dict('records')
    review_products = {_text(row['제품코드']) for row in parent_rows
                       if row.get('계산상태') == '수요/재고 사용자확인'}
    if selected and (frame.empty or not frame['실제 발주수량'].gt(0).any()):
        review_count = len(review_products & frame.attrs.get('hospital_eligible_products', set()))
        result = _notice({**q, SELECTION: list(selected)},
                         (f"선택 범위의 사용자 확인 제품 {review_count}개는 수량 미확정으로 신규 배분하지 않았습니다."
                          if review_count else "선택한 재고적용처의 전월 매출 제품 중 추가 발주 대상이 없습니다."))
        result['meta']['result_status'] = 'input_required' if review_count else 'no_data'
        return result
    if frame.empty:
        return _notice({**q, SELECTION: list(selected)},
                       f"사용자 확인 제품 {len(review_products)}개의 발주수량이 미확정이라 배분하지 않았습니다.")
    # Session-cached frame metadata, not chat history or current-table quantities.
    unallocated = frame.attrs.get(UNALLOCATED_ATTR, {})
    selected_products = frame.attrs.get('hospital_eligible_products', set())
    parent_records = [row for row in parent_rows
                      if not selected or _text(row['제품코드']) in selected_products]
    review_count = sum(row.get('계산상태') == '수요/재고 사용자확인' for row in parent_records)
    no_order_count = sum(row.get('계산상태') != '수요/재고 사용자확인' and
                         row.get('실제 발주수량') == 0 for row in parent_records)
    if selected:
        for row in parent_records:
            row['미배분수량'] = (None if row.get('계산상태') == '수요/재고 사용자확인'
                              else unallocated.get(_text(row['제품코드']), Decimal(0)))
    frame.attrs[REFERENCE_ATTR] = parent_records
    frame.attrs[ORIGINAL_ATTR] = frame.to_dict('records')
    display = pd.DataFrame(frame, copy=False).head(300).copy()
    total = sum(frame['실제 발주수량'], Decimal(0))
    selected_display = (preflight_groups[selected[0]]['name'] if len(selected) == 1 else
                        f'{len(selected)}개 선택' if selected else '전체')
    selected_details = ', '.join(f"{code} {preflight_groups[code]['name']}" for code in selected)
    residual_display = f"{preflight_groups[residual]['name']} ({residual})"
    unallocated_total = sum(unallocated.values(), Decimal(0))
    scope_label = '전월 매출 제품' if selected else '원계산 제품'
    summary = (f"병원발주계산: 재고적용처 {selected_display} / {scope_label} {len(parent_records):,}개 / "
               f"실제 배분 {len(frame):,}행 / 전체 배분수량 {total:,}. "
               + ("" if selected else f"잔여 배정처 {residual_display}. ")
               + (f"사용자 확인 제품 {review_count:,}개는 배분하지 않고 참고표에 보존했습니다. " if review_count else "")
               + (f"발주 불필요 제품 {no_order_count:,}개는 참고표에 보존했습니다. " if no_order_count else "")
               + (f"미배분수량 {unallocated_total:,}. " if selected else "") +
               "제품 첫 행은 참고용이며 실제 합계·현재표·CSV는 배분행 기준입니다. "
               "Excel은 실제배분과 참고표 시트를 분리합니다. ERP에 등록되지 않습니다.")
    parent_params = parent_payload.get('params') or {}
    stock_codes = captured.get('stock_cd_list') or []
    if isinstance(stock_codes, str):
        stock_codes = [stock_codes]
    if not stock_codes and captured.get('stock_cd'):
        stock_codes = [captured['stock_cd']]
    stock_location_codes = ', '.join(str(code) for code in stock_codes)
    location_names = captured.get('_order_stock_display_names') or q.get('_order_stock_display_names') or {}
    stock_location_details = ', '.join(
        f"{location_names[code]} ({code})" if code in location_names else f"명칭 미확인 ({code})"
        for code in stock_codes
    )
    stock_location = (location_names.get(stock_codes[0], '명칭 미확인') if len(stock_codes) == 1 else
                      f'{len(stock_codes)}개 선택' if stock_codes else '확인 필요')
    meta = {"result_status": "success", "row_count_total": len(frame), "row_count": len(display),
            "full_source_row_count": len(frame), "download_row_count": len(frame),
            "full_source_ready": True, "hospital_order_calculation": True,
            "order_calculation_editable": False,
            "allocation_policy": SCOPED_POLICY if selected else POLICY,
            "allocation_selected": bool(selected), "unallocated_quantity": str(unallocated_total),
            "allocation_quantity": str(total), "allocation_audit": sources.get('audit', {}),
            "parent_review_count": review_count,
            "parent_no_order_count": no_order_count,
            "parent_product_count": len(parent_records), "summary_md": summary, "llm_summary_md": summary,
            "order_header_metrics": dict(parent_payload['meta'].get('order_header_metrics') or {}),
            "order_display_conditions": {
                "stock_location": stock_location,
                "stock_location_codes": stock_location_codes,
                "stock_location_details": stock_location_details,
                "cost_application": _text(parent_params.get('cost_apply_cd')) or '확인 필요',
                "stock_application": selected_display,
                "stock_application_details": selected_details,
            },
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "query_summary": parent_payload['meta']['query_summary'] +
                " | 배분 재고적용처 " + (','.join(selected) or '전체') +
                (" | 선택 범위만 배분, 나머지 미배분" if selected else " | 선택외 기본 잔여처 배정 허용"),
            "company_id": company}
    logging.getLogger('ssai').info('[hospital.perf] company_id=%s stage=result_assembly elapsed_ms=%.1f total_ms=%.1f parent_rows=%s review_rows=%s allocation_rows=%s',
                                  company, (time.perf_counter() - assembly_started) * 1000,
                                  (time.perf_counter() - started) * 1000, len(parents), review_count, len(frame))
    return {"action": ACTION, "title": ACTION, "table": TABLE, "params": {**q, SELECTION: list(selected)},
            "df": frame, "df_display": display, "records": display.to_dict('records'),
            "columns": list(display), "data": summary, "message": summary, "meta": meta, "final": True}


def get_hospital_order_calculation_export_df(params=None):
    import streamlit as st
    q = dict(params or {})
    key = _text(q.get('table_key') or q.get('download_table_key'))
    frame = st.session_state.get('sims_export_tables', {}).get(key)
    if not key or not isinstance(frame, pd.DataFrame) or '배분정책' not in frame:
        raise AllocationError("병원발주계산 배분 원본이 없습니다. 다시 조회하세요.")
    _company(q.get('company_id', get_current_company_id()))
    if (not frame['회사'].eq(get_current_company_id()).all() or
            frame['배분정책'].nunique() != 1 or
            not frame['배분정책'].isin((POLICY, SCOPED_POLICY)).all()):
        raise AllocationError("병원발주계산 다운로드 회사/정책이 일치하지 않습니다.")
    return frame.copy()
