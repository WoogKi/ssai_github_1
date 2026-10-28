"""Deterministic 30-calendar-day baselines and backtest metrics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Callable, Iterable

from app.ml.phase1 import DailyDataset, Example30, build_example, OfflineCalendar


@dataclass(frozen=True)
class Forecast:
    company_id: int
    product_code: str
    as_of: date
    horizon_days: int
    method: str
    predicted_quantity: Decimal | None
    actual_quantity: Decimal | None
    status: str
    metric_policy_version: str


def forecast_from_example(example: Example30, *, method: str, alpha: Decimal = Decimal("0.2"),
                          dataset: DailyDataset | None = None) -> Forecast:
    if method not in {"moving_average", "ewma"}:
        raise ValueError("unsupported baseline")
    if not Decimal(0) < alpha <= Decimal(1):
        raise ValueError("alpha must be in (0, 1]")
    status = example.feature_status
    predicted: Decimal | None = None
    if status == "ready" and example.calendar_status != "ready":
        status = "calendar_unavailable"
    if status == "ready" and method == "moving_average":
        predicted = example.qty_30d
    elif status == "ready":
        if dataset is None:
            raise ValueError("EWMA requires daily history")
        first = example.as_of - timedelta(days=89)
        points = [dataset.points.get((example.company_id, example.product_code, first + timedelta(days=i))) for i in range(90)]
        if any(point is None or point.state != "complete" for point in points):
            status = "history_incomplete"
        else:
            level = points[0].normal_quantity
            for point in points[1:]:
                level = alpha * point.normal_quantity + (Decimal(1) - alpha) * level
            predicted = level * Decimal(example.horizon_days)
    if predicted is not None and predicted < 0:
        predicted = None
        status = "negative_forecast_unresolved"
    return Forecast(
        example.company_id, example.product_code, example.as_of, example.horizon_days,
        method, predicted, example.actual_qty_30d, status, example.metric_policy_version,
    )


def backtest(
    dataset: DailyDataset, *, company_id: int, product_codes: Iterable[str], as_of_dates: Iterable[date],
    calendar: OfflineCalendar | None, timestamp_policy_confirmed: bool, method: str,
    alpha: Decimal = Decimal("0.2"), calendar_asof_required: bool = True,
) -> tuple[Forecast, ...]:
    return backtest_models(
        dataset, company_id=company_id, product_codes=product_codes, as_of_dates=as_of_dates,
        calendar=calendar, timestamp_policy_confirmed=timestamp_policy_confirmed,
        methods=(method,), alpha=alpha, calendar_asof_required=calendar_asof_required,
    )


def backtest_models(
    dataset: DailyDataset, *, company_id: int, product_codes: Iterable[str], as_of_dates: Iterable[date],
    calendar: OfflineCalendar | None, timestamp_policy_confirmed: bool, methods: tuple[str, ...],
    alpha: Decimal = Decimal("0.2"), on_complete: Callable[[int], None] | None = None,
    calendar_asof_required: bool = True,
) -> tuple[Forecast, ...]:
    results = []
    products = sorted(set(product_codes))
    for as_of in sorted(set(as_of_dates)):
        for product_code in products:
            example = build_example(
                dataset, company_id=company_id, product_code=product_code, as_of=as_of,
                calendar=calendar, timestamp_policy_confirmed=timestamp_policy_confirmed,
                calendar_asof_required=calendar_asof_required,
            )
            for method in methods:
                results.append(forecast_from_example(example, method=method, alpha=alpha, dataset=dataset))
                if on_complete is not None:
                    on_complete(len(results))
    return tuple(results)


@dataclass(frozen=True)
class Metrics:
    evaluated_count: int
    excluded_count: int
    wape: Decimal | None
    mae: Decimal | None
    bias: Decimal | None
    over_count: int
    under_count: int
    zero_actual_count: int


def evaluate(forecasts: Iterable[Forecast]) -> Metrics:
    rows = tuple(forecasts)
    valid = [row for row in rows if row.status == "ready" and row.actual_quantity is not None and row.predicted_quantity is not None]
    if any(row.horizon_days != 30 for row in valid):
        raise ValueError("mixed prediction horizon")
    if not valid:
        return Metrics(0, len(rows), None, None, None, 0, 0, 0)
    actual_total = sum((abs(row.actual_quantity) for row in valid), Decimal(0))
    errors = [row.predicted_quantity - row.actual_quantity for row in valid]
    absolute_error = sum((abs(error) for error in errors), Decimal(0))
    return Metrics(
        len(valid), len(rows) - len(valid),
        absolute_error / actual_total if actual_total else None,
        absolute_error / Decimal(len(valid)),
        sum(errors, Decimal(0)),
        sum(error > 0 for error in errors), sum(error < 0 for error in errors),
        sum(row.actual_quantity == 0 for row in valid),
    )
