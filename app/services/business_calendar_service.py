"""Shared KST business-calendar helpers backed by ERP holiday overrides."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Mapping


KST = timezone(timedelta(hours=9))


def kst_today() -> date:
    return datetime.now(KST).date()


def _yyyymmdd(value: date) -> str:
    return value.strftime("%Y%m%d")


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
