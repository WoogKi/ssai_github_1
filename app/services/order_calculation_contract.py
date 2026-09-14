"""Pure ordering calculation; dates and demand are supplied by authorities."""

from __future__ import annotations

from dataclasses import dataclass
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Mapping, Sequence


@dataclass(frozen=True)
class OrderConditions:
    reference_date: date
    safety_days: int = 3
    target_days: int = 15
    closing_day: int = 25

    def __post_init__(self):
        if self.safety_days < 0 or self.target_days < 0:
            raise ValueError("재고일수는 음수일 수 없습니다.")
        if not 0 <= self.closing_day <= 31:
            raise ValueError("마감일은 0~31일입니다.")
        if self.closing_day:
            # Do not silently clamp a nonexistent date to month-end.
            date(self.reference_date.year, self.reference_date.month, self.closing_day)


def horizon_dates(conditions: OrderConditions, business_dates: Sequence[date]) -> tuple[date, ...]:
    future = sorted(set(d for d in business_dates if d > conditions.reference_date))
    before_close = conditions.closing_day and conditions.reference_date.day < conditions.closing_day
    if before_close:
        future = [d for d in future if (d.year, d.month) ==
                  (conditions.reference_date.year, conditions.reference_date.month)]
    return tuple(future[:conditions.target_days])


def demand_for_dates(
    dates: Sequence[date], *, reference_date: date,
    business_dates: Sequence[date], monthly_forecast: Mapping[str, Decimal],
    month_day_counts: Mapping[str, int] | None = None,
    date_month_counts: Mapping[str, int] | None = None,
) -> tuple[Decimal | None, tuple[str, ...]]:
    """Allocate the unchanged monthly forecast over that month's full calendar."""
    total = Decimal(0)
    missing = set()
    counts = date_month_counts if date_month_counts is not None else Counter(day.strftime("%Y%m") for day in dates)
    for month, count in sorted(counts.items()):
        plan = monthly_forecast.get(month)
        denominator = (month_day_counts.get(month, 0) if month_day_counts is not None else
                       len({value for value in business_dates if value.strftime("%Y%m") == month}))
        if plan is None or denominator <= 0:
            missing.add(month)
        else:
            # Avoid rounding a recurring daily fraction before multiplication.
            total += max(Decimal(0), plan) * count / Decimal(denominator)
    return (None if missing else total), tuple(sorted(missing))


def recommend_quantity(raw: Decimal, unit: Decimal | None, *, increasing: bool) -> Decimal:
    if raw <= 0:
        return Decimal(0)
    confirmed_unit = unit is not None and unit > 0
    if not confirmed_unit:
        unit = Decimal(1)
    if not unit.is_finite() or unit != unit.to_integral_value():
        raise ValueError("발주단위는 양의 정수여야 합니다.")
    rounding = ROUND_CEILING if increasing else ROUND_FLOOR
    result = (raw / unit).to_integral_value(rounding=rounding) * unit
    return unit if confirmed_unit and result == 0 else result


def infer_order_unit(history: Sequence[Mapping]) -> tuple[Decimal | None, str]:
    """Require observed repetition, not a synthetic greatest common divisor."""
    if not history:
        return None, "발주이력 없음"
    try:
        quantities = [Decimal(str(row['발주수량'])) for row in history]
    except (InvalidOperation, ValueError, KeyError):
        return None, "발주수량 자료부족 사용자확인"
    if any(not q.is_finite() or q <= 0 or q != q.to_integral_value() for q in quantities):
        return None, "비정상/소수 발주수량 사용자확인"
    if len({row['발주일자'] for row in history}) < 3:
        return None, "반복 발주일 부족"
    candidate = min(quantities)
    if len({row['발주일자'] for row, q in zip(history, quantities) if q == candidate}) < 2:
        return None, "최소 발주수량 반복 부족"
    if any(q % candidate for q in quantities):
        return None, "불규칙 발주수량 사용자확인"
    return candidate, "최근1개월 3개 이상 발주일/최소수량 반복/정수배 일관"


def amounts(actual: Decimal, unit_price: Decimal | None) -> dict:
    if not actual.is_finite() or actual < 0 or actual != actual.to_integral_value():
        raise ValueError("실제 발주수량은 유한한 0 이상의 정수여야 합니다.")
    if unit_price is None:
        return {"발주공급가액": None, "발주세액": None, "발주금액(부가세포함)": None}
    if not unit_price.is_finite() or unit_price < 0:
        raise ValueError("발주단가를 확인하세요.")
    supply = actual * unit_price
    tax = supply * Decimal("0.10")
    # Preserve exact amounts; no unapproved settlement rounding policy.
    return {"발주공급가액": supply, "발주세액": tax, "발주금액(부가세포함)": supply + tax}


def calculate_quantities(*, stock: Decimal, pending: Decimal | None,
                         safety_demand: Decimal | None, horizon_demand: Decimal | None,
                         unit: Decimal | None = None, increasing: bool = False) -> dict:
    trigger = None if safety_demand is None else stock <= safety_demand
    raw = None if horizon_demand is None or pending is None else horizon_demand - stock - pending
    recommended = None if trigger is None or raw is None else (
        recommend_quantity(raw, unit, increasing=increasing) if trigger else Decimal(0))
    return {"발주trigger": trigger, "계산 발주수량": raw,
            "추천 발주수량": recommended, "실제 발주수량": recommended}
