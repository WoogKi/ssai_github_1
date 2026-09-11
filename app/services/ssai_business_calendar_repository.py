"""Shared-SSAI-DB authority for official Korean public holidays.

The table is installed only by the explicit schema tool.  Reads report an
explicit unavailable state when either the schema or required yearly load
metadata is absent; consumers must not silently substitute weekday-only logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from typing import Any, Callable, Iterable, Sequence

from app.services.ssai_auth_service import connect_ssai_db
from app.services.ssai_korean_holiday_openapi import KASI_SPECIAL_DAY_SOURCE, OfficialSpecialDay


HOLIDAY_TABLE = "SSAI_KOREAN_HOLIDAYS"
HOLIDAY_LOAD_TABLE = "SSAI_KOREAN_HOLIDAY_LOADS"


@dataclass(frozen=True)
class CalendarAuthorityRead:
    status: str
    holiday_dates: frozenset[str] = frozenset()
    missing_years: tuple[int, ...] = ()
    reason_code: str = ""
    authority: str = "ssai_common_calendar"


@dataclass(frozen=True)
class HolidaySyncResult:
    years: tuple[int, ...]
    inserted: int
    updated: int
    deleted: int
    source: str
    source_version: str


ConnectionFactory = Callable[[], Any]


def _yyyymmdd(value: date) -> str:
    return value.strftime("%Y%m%d")


def initial_calendar_years(*, base_date: date) -> tuple[int, ...]:
    """Return the required five-year initial authority window in KST."""
    return tuple(range(int(base_date.year) - 3, int(base_date.year) + 2))


def _table_exists(conn: Any, table_name: str) -> bool:
    row = conn.cursor().execute("SELECT OBJECT_ID(?, N'U')", f"dbo.{table_name}").fetchone()
    return bool(row and row[0])


def _years_between(start_date: date, end_date: date) -> tuple[int, ...]:
    lower, upper = sorted((int(start_date.year), int(end_date.year)))
    return tuple(range(lower, upper + 1))


def load_official_holidays(
    *,
    start_date: date,
    end_date: date,
    connection_factory: ConnectionFactory = connect_ssai_db,
    source: str = KASI_SPECIAL_DAY_SOURCE,
) -> CalendarAuthorityRead:
    """Load one range only when the common DB has verified annual coverage."""
    required_years = _years_between(start_date, end_date)
    try:
        with connection_factory() as conn:
            if not _table_exists(conn, HOLIDAY_TABLE) or not _table_exists(conn, HOLIDAY_LOAD_TABLE):
                return CalendarAuthorityRead(status="unavailable", reason_code="calendar_schema_missing")
            placeholders = ", ".join("?" for _ in required_years)
            coverage_rows = conn.cursor().execute(
                f"""
                SELECT calendar_year
                FROM dbo.{HOLIDAY_LOAD_TABLE}
                WHERE source = ? AND load_status = 'ready' AND calendar_year IN ({placeholders})
                """,
                source,
                *required_years,
            ).fetchall()
            loaded_years = {int(row[0]) for row in coverage_rows}
            missing_years = tuple(year for year in required_years if year not in loaded_years)
            if missing_years:
                return CalendarAuthorityRead(
                    status="unavailable",
                    missing_years=missing_years,
                    reason_code="calendar_year_not_loaded",
                )
            rows = conn.cursor().execute(
                f"""
                SELECT holiday_date
                FROM dbo.{HOLIDAY_TABLE}
                WHERE source = ?
                  AND is_holiday = 1
                  AND holiday_date >= ? AND holiday_date <= ?
                """,
                source,
                _yyyymmdd(start_date),
                _yyyymmdd(end_date),
            ).fetchall()
    except Exception as exc:
        return CalendarAuthorityRead(status="unavailable", reason_code=f"calendar_db_unavailable:{type(exc).__name__}")
    return CalendarAuthorityRead(status="ready", holiday_dates=frozenset(str(row[0]).strip() for row in rows))


def _checksum(records: Sequence[OfficialSpecialDay]) -> str:
    canonical = "\n".join(
        f"{row.holiday_date}|{row.source_sequence}|{row.holiday_name}|{row.holiday_type}|{int(row.is_holiday)}"
        for row in sorted(records, key=lambda item: (item.holiday_date, item.source_sequence))
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _official_records(records: Iterable[OfficialSpecialDay], *, year: int) -> tuple[OfficialSpecialDay, ...]:
    values: dict[tuple[str, str], OfficialSpecialDay] = {}
    for row in records:
        if not row.is_holiday or not str(row.holiday_date).startswith(f"{int(year):04d}"):
            continue
        if not row.source_sequence:
            raise RuntimeError("kasi_special_day_sequence_missing")
        values[(row.holiday_date, row.source_sequence)] = row
    return tuple(values[key] for key in sorted(values))


def upsert_official_holiday_years(
    *,
    records_by_year: dict[int, Sequence[OfficialSpecialDay]],
    source_version: str,
    connection_factory: ConnectionFactory = connect_ssai_db,
    source: str = KASI_SPECIAL_DAY_SOURCE,
) -> HolidaySyncResult:
    """Transactionally reconcile official holiday rows and their annual coverage.

    Existing rows for each supplied year are updated in place, newly published
    dates are inserted, and withdrawn official dates from the same authority
    are removed.  Re-running the same payload is therefore idempotent.
    """
    years = tuple(sorted(int(year) for year in records_by_year))
    inserted = updated = deleted = 0
    with connection_factory() as conn:
        try:
            if not _table_exists(conn, HOLIDAY_TABLE) or not _table_exists(conn, HOLIDAY_LOAD_TABLE):
                raise RuntimeError("calendar_schema_missing")
            cur = conn.cursor()
            for year in years:
                rows = _official_records(records_by_year[year], year=year)
                incoming_keys = {(row.holiday_date, row.source_sequence) for row in rows}
                existing_rows = cur.execute(
                    f"""
                    SELECT holiday_date, source_sequence, holiday_name, holiday_type, is_holiday, source_version
                    FROM dbo.{HOLIDAY_TABLE}
                    WHERE source = ? AND holiday_date >= ? AND holiday_date <= ?
                    """,
                    source,
                    f"{year:04d}0101",
                    f"{year:04d}1231",
                ).fetchall()
                existing = {(str(row[0]).strip(), str(row[1]).strip()): row for row in existing_rows}
                for row in rows:
                    current = existing.get((row.holiday_date, row.source_sequence))
                    values = (row.holiday_name, row.holiday_type, 1, source_version, source, row.holiday_date, row.source_sequence)
                    if current:
                        unchanged = (
                            str(current[2] or "") == row.holiday_name
                            and str(current[3] or "") == row.holiday_type
                            and bool(current[4]) is True
                            and str(current[5] or "") == source_version
                        )
                        if unchanged:
                            continue
                        cur.execute(
                            f"""UPDATE dbo.{HOLIDAY_TABLE}
                                SET holiday_name = ?, holiday_type = ?, is_holiday = ?, source_version = ?,
                                    updated_at = SYSUTCDATETIME()
                                WHERE source = ? AND holiday_date = ? AND source_sequence = ?""",
                            *values,
                        )
                        updated += 1
                    else:
                        cur.execute(
                            f"""INSERT INTO dbo.{HOLIDAY_TABLE}
                                (holiday_date, source_sequence, holiday_name, holiday_type, is_holiday, source, source_version, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, SYSUTCDATETIME())""",
                            row.holiday_date,
                            row.source_sequence,
                            row.holiday_name,
                            row.holiday_type,
                            1,
                            source,
                            source_version,
                        )
                        inserted += 1
                for holiday_date, source_sequence in sorted(set(existing) - incoming_keys):
                    cur.execute(
                        f"DELETE FROM dbo.{HOLIDAY_TABLE} WHERE source = ? AND holiday_date = ? AND source_sequence = ?",
                        source,
                        holiday_date,
                        source_sequence,
                    )
                    deleted += 1
                load_row = cur.execute(
                    f"SELECT calendar_year FROM dbo.{HOLIDAY_LOAD_TABLE} WHERE source = ? AND calendar_year = ?",
                    source,
                    year,
                ).fetchone()
                params = (source_version, len(rows), _checksum(rows), source, year)
                if load_row:
                    cur.execute(
                        f"""UPDATE dbo.{HOLIDAY_LOAD_TABLE}
                            SET source_version = ?, holiday_count = ?, payload_checksum = ?, load_status = 'ready',
                                loaded_at = SYSUTCDATETIME(), updated_at = SYSUTCDATETIME()
                            WHERE source = ? AND calendar_year = ?""",
                        *params,
                    )
                else:
                    cur.execute(
                        f"""INSERT INTO dbo.{HOLIDAY_LOAD_TABLE}
                            (source, calendar_year, source_version, holiday_count, payload_checksum, load_status, loaded_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, 'ready', SYSUTCDATETIME(), SYSUTCDATETIME())""",
                        source,
                        year,
                        source_version,
                        len(rows),
                        _checksum(rows),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return HolidaySyncResult(years=years, inserted=inserted, updated=updated, deleted=deleted, source=source, source_version=source_version)
