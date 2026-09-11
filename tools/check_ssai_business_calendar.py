"""Focused fixture gate for the shared Korean public-holiday authority."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.business_calendar_service import is_business_day, recent_business_days
from app.services.ssai_business_calendar_repository import CalendarAuthorityRead, initial_calendar_years, upsert_official_holiday_years
from app.services.ssai_korean_holiday_openapi import OfficialSpecialDay, fetch_special_days_for_month, holiday_api_settings, parse_special_day_xml


class _Row(tuple):
    pass


class _Cursor:
    def __init__(self, conn: "_MemoryConnection") -> None:
        self.conn = conn
        self.rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, *params: object) -> "_Cursor":
        text = " ".join(sql.split()).upper()
        self.rows = []
        if "SELECT OBJECT_ID" in text:
            self.rows = [(1,)]
        elif "SELECT HOLIDAY_DATE, SOURCE_SEQUENCE" in text:
            year = str(params[1])[:4]
            self.rows = [tuple(value) for key, value in self.conn.holidays.items() if key[0].startswith(year)]
        elif "UPDATE DBO.SSAI_KOREAN_HOLIDAYS" in text:
            name, kind, flag, version, _source, holiday_date, source_sequence = params
            self.conn.holidays[(str(holiday_date), str(source_sequence))] = (holiday_date, source_sequence, name, kind, flag, version)
        elif "INSERT INTO DBO.SSAI_KOREAN_HOLIDAYS" in text:
            if self.conn.fail_insert:
                raise RuntimeError("fixture_insert_failure")
            holiday_date, source_sequence, name, kind, flag, _source, version = params
            self.conn.holidays[(str(holiday_date), str(source_sequence))] = (holiday_date, source_sequence, name, kind, flag, version)
        elif "DELETE FROM DBO.SSAI_KOREAN_HOLIDAYS" in text:
            _source, holiday_date, source_sequence = params
            self.conn.holidays.pop((str(holiday_date), str(source_sequence)), None)
        elif "SELECT CALENDAR_YEAR FROM DBO.SSAI_KOREAN_HOLIDAY_LOADS" in text:
            source, year = str(params[0]), int(params[1])
            self.rows = [(year,)] if (source, year) in self.conn.loads else []
        elif "UPDATE DBO.SSAI_KOREAN_HOLIDAY_LOADS" in text:
            version, count, checksum, source, year = params
            self.conn.loads[(str(source), int(year))] = (version, count, checksum)
        elif "INSERT INTO DBO.SSAI_KOREAN_HOLIDAY_LOADS" in text:
            source, year, version, count, checksum = params
            self.conn.loads[(str(source), int(year))] = (version, count, checksum)
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.rows)


class _MemoryConnection:
    def __init__(self, *, fail_insert: bool = False) -> None:
        self.holidays: dict[tuple[str, str], tuple[Any, ...]] = {}
        self.loads: dict[tuple[str, int], tuple[Any, ...]] = {}
        self.fail_insert = fail_insert
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self) -> "_MemoryConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _calendar_loader(*, start_date: date, end_date: date) -> CalendarAuthorityRead:
    del start_date, end_date
    return CalendarAuthorityRead(status="ready", holiday_dates=frozenset({"20260914", "20261001"}))


def _unavailable_loader(*, start_date: date, end_date: date) -> CalendarAuthorityRead:
    del start_date, end_date
    return CalendarAuthorityRead(status="unavailable", reason_code="calendar_year_not_loaded")


def main() -> None:
    payload = """
    <response><header><resultCode>00</resultCode></header><body><items>
      <item><seq>1</seq><dateKind>01</dateKind><dateName>일반 공휴일</dateName><isHoliday>Y</isHoliday><locdate>20260914</locdate></item>
      <item><seq>2</seq><dateKind>01</dateKind><dateName>같은 날짜 공휴일</dateName><isHoliday>Y</isHoliday><locdate>20260914</locdate></item>
      <item><seq>3</seq><dateKind>01</dateKind><dateName>대체공휴일</dateName><isHoliday>Y</isHoliday><locdate>20261001</locdate></item>
      <item><seq>4</seq><dateKind>02</dateKind><dateName>기념일</dateName><isHoliday>N</isHoliday><locdate>20261002</locdate></item>
    </items></body></response>
    """.encode("utf-8")
    parsed = parse_special_day_xml(payload)
    assert len(parsed) == 4
    assert [item.is_holiday for item in parsed] == [True, True, True, False]
    assert parsed[3].holiday_date == "20261002"  # string contract, including zeroes
    assert parsed[1].source_sequence == "2"
    assert holiday_api_settings(environ={"SSAI_HOLIDAY_API_SERVICE_KEY": ""}).service_key is None
    captured: dict[str, str] = {}
    class _Response:
        def __enter__(self) -> "_Response":
            return self
        def __exit__(self, *_args: object) -> None:
            return None
        def read(self) -> bytes:
            return payload
    def _opener(url: str, *, timeout: int) -> _Response:
        captured["url"] = url
        assert timeout == 3
        return _Response()
    fetch_special_days_for_month(
        year=2026,
        month=9,
        settings=holiday_api_settings(environ={"SSAI_HOLIDAY_API_SERVICE_KEY": "abc%2Bdef%3D"}),
        timeout_seconds=3,
        opener=_opener,
    )
    assert "serviceKey=abc%2Bdef%3D" in captured["url"] and "%252B" not in captured["url"]
    assert initial_calendar_years(base_date=date(2026, 9, 9)) == (2023, 2024, 2025, 2026, 2027)

    assert is_business_day(date(2026, 9, 7), calendar_loader=_calendar_loader).is_business_day is True
    assert is_business_day(date(2026, 9, 5), calendar_loader=_calendar_loader).is_business_day is False
    assert is_business_day(date(2026, 9, 6), calendar_loader=_calendar_loader).is_business_day is False
    assert is_business_day(date(2026, 9, 14), calendar_loader=_calendar_loader).is_business_day is False
    assert is_business_day(date(2026, 10, 1), calendar_loader=_calendar_loader).is_business_day is False
    assert is_business_day(date(2026, 9, 7), calendar_loader=_unavailable_loader).status == "unavailable"
    recent = recent_business_days(base_date=date(2026, 9, 15), count=4, calendar_loader=_calendar_loader)
    assert recent.status == "ready"
    assert recent.dates == ("20260915", "20260911", "20260910", "20260909")

    conn = _MemoryConnection()
    rows = (parsed[0], parsed[1], parsed[2])
    first = upsert_official_holiday_years(records_by_year={2026: rows}, source_version="fixture-v1", connection_factory=lambda: conn)
    assert (first.inserted, first.updated, first.deleted) == (3, 0, 0)
    assert len([key for key in conn.holidays if key[0] == "20260914"]) == 2
    assert conn.loads[next(iter(conn.loads))][1] == 3  # raw official-holiday row count, not distinct dates
    second = upsert_official_holiday_years(records_by_year={2026: rows}, source_version="fixture-v1", connection_factory=lambda: conn)
    assert (second.inserted, second.updated, second.deleted) == (0, 0, 0)
    changed = OfficialSpecialDay("20260914", "1", "변경된 공휴일명", "01", True)
    third = upsert_official_holiday_years(records_by_year={2026: (changed, parsed[1], parsed[2])}, source_version="fixture-v2", connection_factory=lambda: conn)
    assert third.updated == 3 and conn.holidays[("20260914", "1")][2] == "변경된 공휴일명"
    failing = _MemoryConnection(fail_insert=True)
    try:
        upsert_official_holiday_years(records_by_year={2026: (parsed[0],)}, source_version="fixture", connection_factory=lambda: failing)
    except RuntimeError:
        pass
    else:
        raise AssertionError("fixture write failure must propagate")
    assert failing.rollbacks == 1
    print("PASS: ssai_business_calendar")


if __name__ == "__main__":
    main()
