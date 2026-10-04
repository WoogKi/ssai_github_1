"""Pure ordering calculation; dates and demand are supplied by authorities."""

from __future__ import annotations

from calendar import monthrange
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
    @property
    def effective_closing_day(self) -> int:
        """Return the calendar-safe settlement boundary for the reference month."""
        if not self.closing_day:
            return 0
        return min(self.closing_day, monthrange(self.reference_date.year, self.reference_date.month)[1])


def horizon_dates(conditions: OrderConditions, business_dates: Sequence[date]) -> tuple[date, ...]:
    future = sorted(set(d for d in business_dates if d > conditions.reference_date))
    before_close = closing_day_relation(conditions) == "BEFORE"
    if before_close:
        future = [d for d in future if (d.year, d.month) ==
                  (conditions.reference_date.year, conditions.reference_date.month)]
    return tuple(future[:conditions.target_days])


def closing_day_relation(conditions: OrderConditions) -> str:
    """Classify the calendar-day payment boundary without shifting non-business days."""
    if not conditions.closing_day:
        return "CASH"
    effective_day = conditions.effective_closing_day
    if conditions.reference_date.day < effective_day:
        return "BEFORE"
    if conditions.reference_date.day == effective_day:
        return "ON"
    return "AFTER"


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


def quantity_adjustment(raw: Decimal, unit: Decimal | None, *, increasing: bool,
                        price: Decimal | None = None,
                        default_unit_allowed: bool = True) -> dict:
    """Choose one applied adjustment policy after the final unit price is known."""
    unused = {"추천 발주수량": Decimal(0), "수량조정정책": "미적용",
              "수량조정단위": None, "최소수량 적용": False,
              "기본 조정단위 적용": False, "고가 조정단위 적용": False,
              "수량정수화": "미적용"}
    if raw <= 0:
        return unused
    if unit is not None and (not unit.is_finite() or unit <= 0 or unit != unit.to_integral_value()):
        raise ValueError("발주단위는 양의 정수여야 합니다.")
    high_price = (default_unit_allowed and price is not None and price.is_finite()
                  and price >= Decimal(1000000))
    if high_price:
        adjustment_unit, policy = Decimal(1), "고가 조정단위1"
    elif unit is not None:
        adjustment_unit, policy = unit, "확정 이력단위"
    elif default_unit_allowed:
        adjustment_unit, policy = Decimal(10), "기본 조정단위10"
    else:
        adjustment_unit, policy = Decimal(1), "이력 완전성 확인"
    rounded = (raw / adjustment_unit).to_integral_value(
        rounding=ROUND_CEILING if increasing else ROUND_FLOOR
    ) * adjustment_unit
    minimum_applied = not increasing and rounded == 0 and policy != "이력 완전성 확인"
    recommended = adjustment_unit if minimum_applied else rounded
    return {"추천 발주수량": recommended, "수량조정정책": policy,
            "수량조정단위": adjustment_unit if policy != "이력 완전성 확인" else None,
            "최소수량 적용": minimum_applied,
            "기본 조정단위 적용": policy == "기본 조정단위10",
            "고가 조정단위 적용": policy == "고가 조정단위1",
            "수량정수화": "올림" if increasing else "내림"}


def recommend_quantity(raw: Decimal, unit: Decimal | None, *, increasing: bool,
                       default_unit_allowed: bool = True,
                       price: Decimal | None = None) -> Decimal:
    return quantity_adjustment(raw, unit, increasing=increasing, price=price,
                               default_unit_allowed=default_unit_allowed)["추천 발주수량"]


def infer_order_unit(history: Sequence[Mapping], *, period_label: str = "최근1개월") -> tuple[Decimal | None, str]:
    """Require observed repetition, not a synthetic greatest common divisor."""
    if not history:
        return None, "단위 산정기간 내 발주이력 없음"
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
    return candidate, f"{period_label} 3개 이상 발주일/최소수량 반복/정수배 일관"


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
                         unit: Decimal | None = None, increasing: bool = False,
                         default_unit_allowed: bool = True,
                         price: Decimal | None = None) -> dict:
    trigger = None if safety_demand is None else stock <= safety_demand
    raw = None if horizon_demand is None or pending is None else horizon_demand - stock - pending
    if trigger is None or raw is None:
        return {"발주trigger": trigger, "계산 발주수량": raw,
                "추천 발주수량": None, "실제 발주수량": None,
                "수량조정정책": "미적용", "수량조정단위": None,
                "최소수량 적용": False, "기본 조정단위 적용": False,
                "고가 조정단위 적용": False, "수량정수화": "미적용"}
    adjustment = quantity_adjustment(raw if trigger else Decimal(0), unit,
                                     increasing=increasing, price=price,
                                     default_unit_allowed=default_unit_allowed)
    return {"발주trigger": trigger, "계산 발주수량": raw,
            **adjustment, "실제 발주수량": adjustment["추천 발주수량"]}


def demand_trend_adjustment(
    *, recent_3m_avg: Decimal | None, previous_3m_avg: Decimal | None,
    completed_months: int, frequency_grade: str,
) -> tuple[Decimal | None, Decimal, str]:
    """Apply the approved deadband/half-rate/cap policy to demand only."""
    grade = str(frequency_grade or "").strip().upper()
    if grade == "F":
        return None, Decimal(0), "frequency_F_new_product_excluded"
    if grade == "X":
        return None, Decimal(0), "frequency_X_no_recent_outbound_excluded"
    if completed_months < 6 or recent_3m_avg is None or previous_3m_avg is None:
        return None, Decimal(0), "insufficient_six_completed_months"
    if previous_3m_avg <= 0:
        return None, Decimal(0), "previous_3m_non_positive"
    rate = recent_3m_avg / previous_3m_avg - Decimal(1)
    # Compare the source quantities directly so a repeating monthly average
    # cannot push an exact 10% boundary outside the deadband after division.
    if abs(recent_3m_avg - previous_3m_avg) <= previous_3m_avg * Decimal("0.10"):
        return rate, Decimal(0), "deadband"
    adjustment = max(Decimal("-0.30"), min(Decimal("0.30"), rate * Decimal("0.50")))
    return rate, adjustment, "trend_half_capped"
