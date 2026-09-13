"""Shared KST business-calendar helpers.

The legacy SQL helpers below remain temporarily backed by ERP holiday
overrides.  New Python consumers use the SSAI common-calendar authority and
receive an explicit unavailable status when its schema or annual load is not
ready; they must never fall back to weekday-only calculations silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from app.services.ssai_business_calendar_repository import CalendarAuthorityRead, load_official_holidays


KST = timezone(timedelta(hours=9))


def kst_today() -> date:
    return datetime.now(KST).date()


def _yyyymmdd(value: date) -> str:
    return value.strftime("%Y%m%d")


@dataclass(frozen=True)
class BusinessDayResult:
    status: str
    is_business_day: bool | None = None
    reason_code: str = ""
    authority: str = "ssai_common_calendar"


@dataclass(frozen=True)
class RecentBusinessDaysResult:
    status: str
    dates: tuple[str, ...] = ()
    reason_code: str = ""
    authority: str = "ssai_common_calendar"


@dataclass(frozen=True)
class BusinessDayMonthContext:
    evaluation_date: str
    evaluation_month: str
    calendar_days_total: int
    elapsed_calendar_days: int
    calendar_progress_ratio: float
    business_days_total: int | None = None
    elapsed_business_days: int | None = None
    business_day_progress_ratio: float | None = None
    authority_status: str = "unavailable"
    reason: str = ""
    authority: str = "ssai_common_calendar"


CalendarLoader = Callable[..., CalendarAuthorityRead]


def _official_calendar(
    *,
    start_date: date,
    end_date: date,
    calendar_loader: CalendarLoader,
) -> CalendarAuthorityRead:
    return calendar_loader(start_date=start_date, end_date=end_date)


def is_business_day(
    target_date: date,
    *,
    calendar_loader: CalendarLoader = load_official_holidays,
) -> BusinessDayResult:
    """Determine one Korean business day from the loaded common authority."""
    authority = _official_calendar(start_date=target_date, end_date=target_date, calendar_loader=calendar_loader)
    if authority.status != "ready":
        return BusinessDayResult(status="unavailable", reason_code=authority.reason_code, authority=authority.authority)
    value = target_date.weekday() < 5 and _yyyymmdd(target_date) not in authority.holiday_dates
    return BusinessDayResult(status="ready", is_business_day=value, authority=authority.authority)


def recent_business_days(
    *,
    base_date: date | None = None,
    count: int = 4,
    include_base: bool = True,
    calendar_loader: CalendarLoader = load_official_holidays,
    lookback_days: int = 62,
) -> RecentBusinessDaysResult:
    """Resolve recent KST business days from persisted official holidays only.

    ``lookback_days`` is a bounded authority window, not a weekday fallback.
    If it cannot yield the requested number of days, callers receive an
    explicit unavailable result.
    """
    required = max(1, int(count))
    anchor = base_date or kst_today()
    start = anchor if include_base else anchor - timedelta(days=1)
    window = max(required + 14, int(lookback_days))
    lower = start - timedelta(days=window - 1)
    authority = _official_calendar(start_date=lower, end_date=start, calendar_loader=calendar_loader)
    if authority.status != "ready":
        return RecentBusinessDaysResult(status="unavailable", reason_code=authority.reason_code, authority=authority.authority)
    dates: list[str] = []
    cursor = start
    while cursor >= lower and len(dates) < required:
        key = _yyyymmdd(cursor)
        if cursor.weekday() < 5 and key not in authority.holiday_dates:
            dates.append(key)
        cursor -= timedelta(days=1)
    if len(dates) != required:
        return RecentBusinessDaysResult(
            status="unavailable",
            reason_code="calendar_lookback_insufficient",
            authority=authority.authority,
        )
    return RecentBusinessDaysResult(status="ready", dates=tuple(dates), authority=authority.authority)


def business_day_month_context(
    *,
    evaluation_date: date | None = None,
    calendar_loader: CalendarLoader = load_official_holidays,
) -> BusinessDayMonthContext:
    """Build one reusable month-progress context from the persisted authority."""
    target = evaluation_date or kst_today()
    total_calendar_days = monthrange(target.year, target.month)[1]
    month_start = target.replace(day=1)
    month_end = target.replace(day=total_calendar_days)
    elapsed_calendar_days = target.day
    base = {
        "evaluation_date": _yyyymmdd(target),
        "evaluation_month": target.strftime("%Y%m"),
        "calendar_days_total": total_calendar_days,
        "elapsed_calendar_days": elapsed_calendar_days,
        "calendar_progress_ratio": elapsed_calendar_days / total_calendar_days,
    }
    authority = _official_calendar(
        start_date=month_start,
        end_date=month_end,
        calendar_loader=calendar_loader,
    )
    if authority.status != "ready":
        return BusinessDayMonthContext(
            **base,
            authority_status="unavailable",
            reason=authority.reason_code,
            authority=authority.authority,
        )

    business_dates = [
        month_start + timedelta(days=offset)
        for offset in range(total_calendar_days)
        if (month_start + timedelta(days=offset)).weekday() < 5
        and _yyyymmdd(month_start + timedelta(days=offset)) not in authority.holiday_dates
    ]
    elapsed_business_days = sum(1 for value in business_dates if value <= target)
    business_days_total = len(business_dates)
    if business_days_total <= 0:
        return BusinessDayMonthContext(
            **base,
            authority_status="unavailable",
            reason="calendar_month_has_no_business_days",
            authority=authority.authority,
        )
    return BusinessDayMonthContext(
        **base,
        business_days_total=business_days_total,
        elapsed_business_days=elapsed_business_days,
        business_day_progress_ratio=elapsed_business_days / business_days_total,
        authority_status="ready",
        authority=authority.authority,
    )


def recent_business_dates(
    *,
    today: date,
    count: int = 3,
    overrides: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Resolve recent dates for fixtures and non-SQL consumers.

    ``work`` overrides weekends and ``holiday`` excludes weekdays.
    """
    required = max(1, int(count))
    override_map = {str(key): str(value).strip().lower() for key, value in (overrides or {}).items()}
    result: list[str] = []
    cursor = today
    while len(result) < required:
        key = _yyyymmdd(cursor)
        flag = override_map.get(key, "")
        if flag == "work" or (cursor.weekday() < 5 and flag != "holiday"):
            result.append(key)
        cursor -= timedelta(days=1)
    return tuple(result)


@dataclass(frozen=True)
class BusinessCalendarSql:
    cte_sql: str
    params: tuple[object, ...]
    authority: str = "dbo.WB_Holiday:holiday/work"


def build_recent_business_days_cte(
    *,
    today: date | None = None,
    count: int = 3,
    lookback_days: int = 21,
) -> BusinessCalendarSql:
    """Build a parameter-bound SQL Server 2008 compatible business-day CTE.

    It is designed to be prepended to the owning source SELECT so calendar and
    business data are obtained in one physical query.
    """
    anchor = today or kst_today()
    required = max(1, min(int(count), 31))
    window = max(required + 7, min(int(lookback_days), 62))
    rows = [(anchor - timedelta(days=offset), (anchor - timedelta(days=offset)).weekday()) for offset in range(window)]
    selects = ["SELECT ? AS business_date, ? AS weekday_no"]
    selects.extend("UNION ALL SELECT ?, ?" for _ in rows[1:])
    params: list[object] = []
    for candidate, weekday_no in rows:
        params.extend((_yyyymmdd(candidate), int(weekday_no)))
    cte = f"""
WITH BusinessCalendarCandidates AS (
    {' '.join(selects)}
), RecentBusinessDates AS (
    SELECT TOP {required} C.business_date
    FROM BusinessCalendarCandidates AS C
    WHERE
        EXISTS (
            SELECT 1 FROM dbo.WB_Holiday AS W
            WHERE W.WBday_Target_Date = C.business_date
              AND W.WBday_Flag = 'work'
        )
        OR (
            C.weekday_no < 5
            AND NOT EXISTS (
                SELECT 1 FROM dbo.WB_Holiday AS H
                WHERE H.WBday_Target_Date = C.business_date
                  AND H.WBday_Flag = 'holiday'
            )
        )
    ORDER BY C.business_date DESC
)
""".strip()
    return BusinessCalendarSql(cte_sql=cte, params=tuple(params))
