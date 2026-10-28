"""DB-free outbound demand preparation with explicit evidence boundaries."""

from __future__ import annotations

from collections import defaultdict
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any, Iterable, Mapping

from app.services.business_calendar_service import is_business_day
from app.services.saved_io_scope import resolve_saved_io_scope
from app.services.ssai_business_calendar_repository import CalendarAuthorityRead


METRIC_POLICY_VERSION = "r120_quantity_only_provisional_v1"
DOCUMENT_DIRECTION_OUTBOUND = "3"


def _code(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else ""


def _day(value: Any) -> date:
    if isinstance(value, datetime):
        raise ValueError("date field must not be a timestamp")
    if isinstance(value, date):
        return value
    raw = _code(value)
    if len(raw) == 8 and raw.isascii() and raw.isdigit():
        return datetime.strptime(raw, "%Y%m%d").date()
    if len(raw) == 10:
        return date.fromisoformat(raw)
    raise ValueError("invalid date field")


def _timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        raw = value.strip()
        if len(raw) == 14 and raw.isascii() and raw.isdigit():
            result = datetime.strptime(raw, "%Y%m%d%H%M%S")
        else:
            result = datetime.fromisoformat(raw)
    else:
        raise ValueError("invalid timestamp field")
    if result.tzinfo is not None:
        raise ValueError("timestamp timezone policy is unverified")
    return result


def _decimal(value: Any) -> Decimal:
    if value is None or value == "":
        raise ValueError("quantity or amount is missing")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid quantity") from exc
    if not result.is_finite():
        raise ValueError("non-finite quantity")
    return result


def _company(row: Mapping[str, Any], expected: int) -> None:
    if int(row.get("company_id", 0)) != expected:
        raise ValueError("company boundary mismatch")


@dataclass(frozen=True)
class Outbound:
    company_id: int
    out_date: date
    vendor_code: str
    out_seq: str
    product_code: str
    io_code: str
    quantity: Decimal
    bonus_quantity: Decimal
    stock_group_code: str
    stock_code: str
    stock_apply_code: str
    cost_apply_code: str
    trans_di: str
    trans_date: str
    trans_seq: str
    add_time: datetime | None
    mod_time: datetime | None
    raw: Mapping[str, Any]

    @property
    def source_key(self) -> tuple[int, date, str, str]:
        return self.company_id, self.out_date, self.vendor_code, self.out_seq

    @property
    def document_key(self) -> tuple[int, str, str, str, str]:
        return self.company_id, self.trans_di, self.trans_date, self.vendor_code, self.trans_seq


@dataclass(frozen=True)
class EnrichedOutbound:
    source: Outbound
    document_status: str
    document: Mapping[str, Any] | None
    vendor: Mapping[str, Any] | None
    product: Mapping[str, Any] | None
    standard_codes: tuple[str, ...]
    io_label: str
    channel: str
    care_provider_status: str
    supply_label: str
    supplier_type: str
    business_number: str


@dataclass(frozen=True)
class PreparedSource:
    rows: tuple[EnrichedOutbound, ...]
    product_codes: tuple[str, ...]
    normal_codes: tuple[str, ...]
    source_row_count: int
    source_quantity: Decimal
    source_bonus_quantity: Decimal
    document_match_counts: Mapping[str, int]
    product_registered_dates: Mapping[str, date | None]


def _unique_master(
    rows: Iterable[Mapping[str, Any]], *, company_id: int, field: str, name: str,
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        _company(row, company_id)
        if name == "product" and not isinstance(row.get(field), str):
            raise ValueError("product code must be a string to preserve leading zeroes")
        code = _code(row.get(field))
        if not code or code in result:
            raise ValueError(f"missing or duplicate {name} key")
        result[code] = dict(row)
    return result


def prepare_sources(
    *, company_id: int, outbound_rows: Iterable[Mapping[str, Any]],
    document_rows: Iterable[Mapping[str, Any]], vendor_rows: Iterable[Mapping[str, Any]],
    product_rows: Iterable[Mapping[str, Any]], standard_rows: Iterable[Mapping[str, Any]],
    code_rows: Iterable[Mapping[str, Any]], saved_io_codes: Iterable[str],
    channel_by_io: Mapping[str, str] | None = None,
    valid_care_provider_codes: Iterable[str] = (),
    supplier_type_by_label: Mapping[str, str] | None = None,
) -> PreparedSource:
    """Keep each R120 PK once; join R130 only on the four document fields."""
    if company_id <= 0:
        raise ValueError("company_id is required")
    scope = resolve_saved_io_scope(saved_io_codes, confirmed_trade_roles=True)
    vendors = _unique_master(vendor_rows, company_id=company_id, field="Rd03_Ven_Cd", name="vendor")
    products = _unique_master(product_rows, company_id=company_id, field="Rd04_Physic_Cd", name="product")
    registered_dates: dict[str, date | None] = {}
    for code, product in products.items():
        raw_date = product.get("Rd04_Add_Date")
        try:
            registered_dates[code] = _day(raw_date) if raw_date else None
        except (TypeError, ValueError):
            registered_dates[code] = None
    standards: dict[str, set[str]] = defaultdict(set)
    for row in standard_rows:
        _company(row, company_id)
        product_code = _code(row.get("Rd046_Physic_Cd"))
        if not product_code:
            raise ValueError("standard product key is missing")
        standard_code = _code(row.get("Rd046_Standard_Cd"))
        if standard_code:
            standards[product_code].add(standard_code)
    names: dict[tuple[str, str], str] = {}
    for row in code_rows:
        _company(row, company_id)
        key = _code(row.get("Rd01_Gcode")), _code(row.get("Rd01_Tcode"))
        if not all(key) or key in names:
            raise ValueError("missing or duplicate business code key")
        names[key] = _code(row.get("Rd01_Hnm"))
    documents: dict[tuple[int, str, str, str, str], Mapping[str, Any]] = {}
    for row in document_rows:
        _company(row, company_id)
        key = (
            company_id, _code(row.get("Rd13_Trans_Di")),
            _code(row.get("Rd13_Trans_YyMmDd")),
            _code(row.get("Rd13_Ven_Cd")), _code(row.get("Rd13_Trans_Seq")),
        )
        if not all(key[1:]) or key in documents:
            raise ValueError("missing or duplicate R130 document key")
        documents[key] = dict(row)
    allowed_care = {_code(value) for value in valid_care_provider_codes if _code(value)}
    channels = dict(channel_by_io or {})
    supplier_types = dict(supplier_type_by_label or {})
    if any(code not in scope.all_codes or value not in {"online", "offline"} for code, value in channels.items()):
        raise ValueError("channel map requires approved I/O codes and labels")
    source_keys: set[tuple[int, date, str, str]] = set()
    result: list[EnrichedOutbound] = []
    total = Decimal(0)
    bonus_total = Decimal(0)
    matches: dict[str, int] = defaultdict(int)
    for row in outbound_rows:
        _company(row, company_id)
        if not isinstance(row.get("Rd12_Physic_Cd"), str):
            raise ValueError("product code must be a string to preserve leading zeroes")
        source = Outbound(
            company_id=company_id,
            out_date=_day(row.get("Rd12_Out_YyMmDd")),
            vendor_code=_code(row.get("Rd12_Ven_Cd")),
            out_seq=_code(row.get("Rd12_Out_Seq")),
            product_code=_code(row.get("Rd12_Physic_Cd")),
            io_code=_code(row.get("Rd12_Io_Gu")),
            quantity=_decimal(row.get("Rd12_Quantity")),
            bonus_quantity=_decimal(row.get("Rd12_Oquantity") if row.get("Rd12_Oquantity") is not None else 0),
            stock_group_code=_code(row.get("Rd12_Stock_Cd_Gcode")),
            stock_code=_code(row.get("Rd12_Stock_Cd")),
            stock_apply_code=_code(row.get("Rd12_Stock_Apply_Cd")),
            cost_apply_code=_code(row.get("Rd12_Cost_Apply_Cd")),
            trans_di=_code(row.get("Rd12_Trans_Di")),
            trans_date=_code(row.get("Rd12_Trans_YyMmDd")),
            trans_seq=_code(row.get("Rd12_Trans_Seq")),
            add_time=_timestamp(row.get("Rd12_Add_Time")),
            mod_time=_timestamp(row.get("Rd12_Mod_Time")),
            raw=dict(row),
        )
        if not source.vendor_code or not source.out_seq or not source.product_code:
            raise ValueError("R120 source PK or product is missing")
        if source.source_key in source_keys:
            raise ValueError("duplicate R120 source PK")
        source_keys.add(source.source_key)
        if source.trans_di and source.trans_di != DOCUMENT_DIRECTION_OUTBOUND:
            status = "direction_mismatch"
        elif not source.trans_di or not source.trans_date or not source.trans_seq:
            status = "missing_key"
        elif source.document_key in documents:
            status = "matched"
        else:
            status = "not_linked"
        document = documents.get(source.document_key) if status == "matched" else None
        vendor = vendors.get(source.vendor_code)
        supply_label = ""
        care_status = "unknown"
        if vendor is not None:
            supply_label = names.get(
                (_code(vendor.get("Rd03_Supply_Cd_Gcode")), _code(vendor.get("Rd03_Supply_Cd"))), "",
            )
            if _code(vendor.get("Rd03_Other1")) in allowed_care:
                care_status = "care_provider"
        io_label = names.get(("0012", source.io_code), "")
        result.append(EnrichedOutbound(
            source=source, document_status=status, document=document,
            vendor=vendor, product=products.get(source.product_code),
            standard_codes=tuple(sorted(standards.get(source.product_code, ()))),
            io_label=io_label, channel=channels.get(source.io_code, "unmapped"),
            care_provider_status=care_status, supply_label=supply_label,
            supplier_type=supplier_types.get(supply_label, "review_needed"),
            business_number=_code(vendor.get("Rd03_Ven_Num")) if vendor is not None else "",
        ))
        matches[status] += 1
        total += source.quantity
        bonus_total += source.bonus_quantity
    if total != sum((item.source.quantity for item in result), Decimal(0)):
        raise ValueError("R120 join changed source count or quantity")
    return PreparedSource(
        rows=tuple(result), product_codes=tuple(sorted(set(products) | {item.source.product_code for item in result})),
        normal_codes=scope.outbound_codes, source_row_count=len(result),
        source_quantity=total, source_bonus_quantity=bonus_total,
        document_match_counts=dict(matches),
        product_registered_dates=registered_dates,
    )


def document_amount_once(prepared: PreparedSource, field: str) -> Decimal:
    """R130 amounts are document-level, never repeated per R120 detail row."""
    seen: set[tuple[int, str, str, str, str]] = set()
    total = Decimal(0)
    for row in prepared.rows:
        key = row.source.document_key
        if row.document is not None and key not in seen:
            if field not in row.document:
                raise ValueError("R130 amount field is missing")
            total += _decimal(row.document.get(field))
            seen.add(key)
    return total


def business_number_quantity(prepared: PreparedSource) -> Mapping[tuple[int, str], Decimal]:
    """Sum each R120 normal-outbound row once, even across vendor codes."""
    totals: dict[tuple[int, str], Decimal] = defaultdict(Decimal)
    for row in prepared.rows:
        if row.business_number and row.source.io_code in prepared.normal_codes:
            totals[(row.source.company_id, row.business_number)] += row.source.quantity
    return dict(totals)


@dataclass(frozen=True)
class DailyPoint:
    company_id: int
    product_code: str
    out_date: date
    normal_quantity: Decimal | None
    bonus_quantity: Decimal | None
    normal_row_count: int
    state: str
    product_existence_status: str
    negative_normal_row_count: int = 0


class SparseDailyPoints:
    """Materialize only observed days; synthesize verified zeroes on access."""

    def __init__(
        self, *, company_id: int, date_from: date, date_to: date, complete_dates: frozenset[date],
        product_codes: tuple[str, ...], registered_dates: Mapping[str, date | None],
        first_sale_dates: Mapping[str, date], observed: Mapping[tuple[int, str, date], DailyPoint],
    ) -> None:
        self.company_id = company_id
        self.date_from = date_from
        self.date_to = date_to
        self.complete_dates = complete_dates
        self.product_codes = frozenset(product_codes)
        self.registered_dates = registered_dates
        self.first_sale_dates = first_sale_dates
        self.observed = observed
        by_product: dict[str, list[DailyPoint]] = defaultdict(list)
        for point in observed.values():
            by_product[point.product_code].append(point)
        self.observed_by_product = {
            code: tuple(sorted(points, key=lambda point: point.out_date)) for code, points in by_product.items()
        }
        self.observed_dates_by_product = {
            code: tuple(point.out_date for point in points) for code, points in self.observed_by_product.items()
        }

    def existence(self, product_code: str, day: date) -> str:
        registered = self.registered_dates.get(product_code)
        first_sale = self.first_sale_dates.get(product_code)
        if first_sale is not None and first_sale <= day:
            return "sales_evidence_master_conflict" if registered is not None and registered > day else "sales_evidence"
        if registered is not None:
            return "registered_date_only" if registered <= day else "not_yet_registered"
        return "existence_unverified"

    def get(self, key: tuple[int, str, date], default: Any = None) -> DailyPoint | Any:
        company_id, product_code, day = key
        if company_id != self.company_id or product_code not in self.product_codes or not self.date_from <= day <= self.date_to:
            return default
        existence = self.existence(product_code, day)
        if existence in {"not_yet_registered", "existence_unverified"}:
            return DailyPoint(company_id, product_code, day, None, None, 0, existence, existence)
        if day not in self.complete_dates:
            return DailyPoint(company_id, product_code, day, None, None, 0, "incomplete", existence)
        return self.observed.get(key, DailyPoint(company_id, product_code, day, Decimal(0), Decimal(0), 0, "complete", existence))

    def __getitem__(self, key: tuple[int, str, date]) -> DailyPoint:
        result = self.get(key)
        if result is None:
            raise KeyError(key)
        return result

    def __len__(self) -> int:
        return len(self.observed)


@dataclass(frozen=True)
class DailyDataset:
    points: SparseDailyPoints
    scope_totals: Mapping[tuple[int, str, date, str, str, str, str], Decimal]
    rows: tuple[EnrichedOutbound, ...]
    normal_codes: tuple[str, ...]
    metric_policy_version: str
    rows_by_product: Mapping[str, tuple[Outbound, ...]]
    row_dates_by_product: Mapping[str, tuple[date, ...]]


def build_daily(
    prepared: PreparedSource, *, company_id: int, date_from: date, date_to: date,
    complete_dates: Iterable[date],
) -> DailyDataset:
    if date_to < date_from or any(row.source.company_id != company_id for row in prepared.rows):
        raise ValueError("invalid daily company or date scope")
    complete = set(complete_dates)
    if any(day < date_from or day > date_to for day in complete):
        raise ValueError("complete date outside dataset range")
    qty: dict[tuple[int, str, date], Decimal] = defaultdict(Decimal)
    bonus: dict[tuple[int, str, date], Decimal] = defaultdict(Decimal)
    counts: dict[tuple[int, str, date], int] = defaultdict(int)
    negatives: dict[tuple[int, str, date], int] = defaultdict(int)
    scoped: dict[tuple[int, str, date, str, str, str, str], Decimal] = defaultdict(Decimal)
    first_sale: dict[str, date] = {}
    by_product: dict[str, list[Outbound]] = defaultdict(list)
    for row in prepared.rows:
        item = row.source
        if not date_from <= item.out_date <= date_to:
            raise ValueError("R120 row outside dataset range")
        if item.io_code in prepared.normal_codes:
            by_product[item.product_code].append(item)
            if item.add_time is not None:
                evidence_day = max(item.out_date, item.add_time.date())
                first_sale[item.product_code] = min(first_sale.get(item.product_code, evidence_day), evidence_day)
        if item.io_code not in prepared.normal_codes or item.out_date not in complete:
            continue
        key = company_id, item.product_code, item.out_date
        qty[key] += item.quantity
        bonus[key] += item.bonus_quantity
        counts[key] += 1
        if item.quantity < 0:
            negatives[key] += 1
        scoped[(*key, item.stock_group_code, item.stock_code, item.stock_apply_code, item.cost_apply_code)] += item.quantity
    points = SparseDailyPoints(
        company_id=company_id, date_from=date_from, date_to=date_to, complete_dates=frozenset(complete),
        product_codes=prepared.product_codes, registered_dates=prepared.product_registered_dates,
        first_sale_dates=first_sale, observed={
            key: DailyPoint(*key, amount, bonus[key], counts[key], "complete",
                            ("sales_evidence_master_conflict" if prepared.product_registered_dates.get(key[1])
                             and prepared.product_registered_dates[key[1]] > key[2] else "sales_evidence"),
                            negatives[key])
            for key, amount in qty.items()
        },
    )
    if sum(scoped.values(), Decimal(0)) != sum(qty.values(), Decimal(0)):
        raise ValueError("scope aggregation changed normal quantity")
    sorted_rows = {code: tuple(sorted(rows, key=lambda item: item.out_date)) for code, rows in by_product.items()}
    return DailyDataset(
        points, dict(scoped), prepared.rows, prepared.normal_codes, METRIC_POLICY_VERSION,
        sorted_rows, {code: tuple(item.out_date for item in rows) for code, rows in sorted_rows.items()},
    )


@dataclass(frozen=True)
class OfflineCalendar:
    start_date: date
    end_date: date
    holiday_dates: frozenset[str]
    known_as_of: date
    version: str

    def loader(self, *, start_date: date, end_date: date) -> CalendarAuthorityRead:
        if start_date < self.start_date or end_date > self.end_date or not self.version:
            return CalendarAuthorityRead(status="unavailable", reason_code="calendar_coverage_missing")
        return CalendarAuthorityRead(status="ready", holiday_dates=self.holiday_dates)


@dataclass(frozen=True)
class Example30:
    company_id: int
    product_code: str
    as_of: date
    horizon_days: int
    qty_7d: Decimal | None
    qty_30d: Decimal | None
    qty_90d: Decimal | None
    active_days_30d: int | None
    business_days_next_30d: int | None
    first_business_day_of_month: date | None
    actual_qty_30d: Decimal | None
    feature_status: str
    label_status: str
    calendar_status: str
    metric_policy_version: str
    product_existence_status: str
    negative_normal_row_count_90d: int


def _window_summary(
    dataset: DailyDataset, company_id: int, product_code: str, start: date, end: date,
) -> tuple[Decimal, int, int] | None:
    points = dataset.points
    if (company_id != points.company_id or product_code not in points.product_codes
            or start < points.date_from or end > points.date_to
            or points.existence(product_code, start) in {"not_yet_registered", "existence_unverified"}):
        return None
    cursor = start
    while cursor <= end:
        if cursor not in points.complete_dates:
            return None
        cursor += timedelta(days=1)
    dates = points.observed_dates_by_product.get(product_code, ())
    observed = points.observed_by_product.get(product_code, ())
    selected = observed[bisect_left(dates, start):bisect_right(dates, end)]
    return (
        sum((point.normal_quantity for point in selected), Decimal(0)),
        sum(point.negative_normal_row_count for point in selected),
        sum(point.normal_quantity != 0 for point in selected),
    )


@lru_cache(maxsize=128)
def _calendar_context(calendar: OfflineCalendar, as_of: date) -> tuple[str, int | None, date | None]:
    if calendar.known_as_of > as_of:
        return "unavailable", None, None
    month_start = as_of.replace(day=1)
    end = as_of + timedelta(days=30)
    if calendar.start_date > month_start or calendar.end_date < end:
        return "unavailable", None, None
    checks = [
        is_business_day(month_start + timedelta(days=offset), calendar_loader=calendar.loader)
        for offset in range((end - month_start).days + 1)
    ]
    if not all(check.status == "ready" for check in checks):
        return "unavailable", None, None
    first = next(
        (month_start + timedelta(days=index) for index, check in enumerate(checks) if check.is_business_day), None,
    )
    next_days = sum(
        1 for index, check in enumerate(checks)
        if check.is_business_day and month_start + timedelta(days=index) > as_of
    )
    return "ready", next_days, first


def build_example(
    dataset: DailyDataset, *, company_id: int, product_code: str, as_of: date,
    calendar: OfflineCalendar | None, timestamp_policy_confirmed: bool,
    horizon_days: int = 30,
) -> Example30:
    if horizon_days != 30:
        raise ValueError("only the validated 30-day horizon is supported")
    windows = {
        days: _window_summary(dataset, company_id, product_code, as_of - timedelta(days=days - 1), as_of)
        for days in (7, 30, 90)
    }
    existence = dataset.points.existence(product_code, as_of)
    cutoff = datetime.combine(as_of, datetime.max.time())
    dates = dataset.row_dates_by_product.get(product_code, ())
    rows = dataset.rows_by_product.get(product_code, ())
    start_index = bisect_left(dates, as_of - timedelta(days=89))
    end_index = bisect_right(dates, as_of)
    source_rows = rows[start_index:end_index]
    negative_count = windows[90][1] if windows[90] is not None else 0
    if existence in {"not_yet_registered", "existence_unverified"}:
        feature_status = existence
    elif not timestamp_policy_confirmed:
        feature_status = "timestamp_policy_unverified"
    elif any(item.add_time is None or item.mod_time is None for item in source_rows):
        feature_status = "asof_timestamp_unavailable"
    elif any(item.add_time > cutoff or item.mod_time > cutoff for item in source_rows):
        feature_status = "asof_history_unavailable"
    elif windows[90] is None:
        feature_status = "history_incomplete"
    elif negative_count:
        feature_status = "negative_normal_quantity_unresolved"
    else:
        feature_status = "ready"
    label = _window_summary(dataset, company_id, product_code, as_of + timedelta(days=1), as_of + timedelta(days=30))
    label_status = "complete" if label is not None else "incomplete"
    if label_status == "complete" and label[1]:
        label_status = "negative_quantity_review"
    calendar_status, business_days, first_business = (
        _calendar_context(calendar, as_of) if calendar is not None else ("unavailable", None, None)
    )
    if feature_status == "ready":
        qty_7d = windows[7][0]
        qty_30d = windows[30][0]
        qty_90d = windows[90][0]
        active_days = windows[30][2]
    else:
        qty_7d = qty_30d = qty_90d = None
        active_days = None
    return Example30(
        company_id, product_code, as_of, horizon_days, qty_7d, qty_30d, qty_90d,
        active_days, business_days, first_business,
        label[0] if label_status == "complete" else None,
        feature_status, label_status, calendar_status, dataset.metric_policy_version,
        existence, negative_count,
    )
