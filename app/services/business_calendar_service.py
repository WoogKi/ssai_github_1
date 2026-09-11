"""Shared KST business-calendar helpers.

The legacy SQL helpers below remain temporarily backed by ERP holiday
overrides.  New Python consumers use the SSAI common-calendar authority and
receive an explicit unavailable status when its schema or annual load is not
ready; they must never fall back to weekday-only calculations silently.
"""

from __future__ import annotations

from dataclasses import dataclass
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
