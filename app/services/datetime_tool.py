"""Deterministic Korean operating-calendar answers for ordinary Chat."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re
from zoneinfo import ZoneInfo

OPERATING_TIMEZONE_NAME = "Asia/Seoul"
OPERATING_TIMEZONE = ZoneInfo(OPERATING_TIMEZONE_NAME)
_WEEKDAY_NAMES = ("월", "화", "수", "목", "금", "토", "일")

@dataclass(frozen=True)
class DateRange:
    start: date
    end: date

@dataclass(frozen=True)
class DateTimeToolAnswer:
    intent: str
    text: str
    reference_at: datetime
    timezone_name: str = OPERATING_TIMEZONE_NAME

def operating_now(*, now: datetime | None = None) -> datetime:
    """Return an aware operating timestamp; fixtures must provide an aware time."""
    if now is None:
        return datetime.now(OPERATING_TIMEZONE)
    if now.tzinfo is None:
        raise ValueError("Date/Time Tool reference time must be timezone-aware")
    return now.astimezone(OPERATING_TIMEZONE)

def week_range(reference: date, *, offset_weeks: int = 0) -> DateRange:
    start = reference - timedelta(days=reference.weekday()) + timedelta(weeks=offset_weeks)
    return DateRange(start=start, end=start + timedelta(days=6))

def month_range(reference: date, *, offset_months: int = 0) -> DateRange:
    month_index = reference.year * 12 + (reference.month - 1) + offset_months
    year, zero_month = divmod(month_index, 12)
    start = date(year, zero_month + 1, 1)
    next_year, next_zero_month = divmod(month_index + 1, 12)
    return DateRange(start=start, end=date(next_year, next_zero_month + 1, 1) - timedelta(days=1))

def _weekday(value: date) -> str:
    return _WEEKDAY_NAMES[value.weekday()]

def _reference_line(value: datetime) -> str:
    return f"기준 시각: {value:%Y-%m-%d %H:%M:%S} {value.tzname() or 'KST'} ({OPERATING_TIMEZONE_NAME})"

def _answer(intent: str, body: str, reference_at: datetime) -> DateTimeToolAnswer:
    return DateTimeToolAnswer(intent=intent, text=f"{body}\n\n{_reference_line(reference_at)}", reference_at=reference_at)

def _relative_day_answer(kind: str, reference_at: datetime) -> DateTimeToolAnswer:
    offsets = {"today": 0, "yesterday": -1, "tomorrow": 1}
    labels = {"today": "오늘", "yesterday": "어제", "tomorrow": "내일"}
    target = reference_at.date() + timedelta(days=offsets[kind])
    return _answer(kind, f"{labels[kind]} 날짜는 {target.year}년 {target.month}월 {target.day}일 ({_weekday(target)}요일)입니다.", reference_at)

def _relative_week_answer(direction: str, reference_at: datetime) -> DateTimeToolAnswer:
    offsets = {"this": 0, "last": -1, "next": 1}
    labels = {"this": "이번 주", "last": "지난 주", "next": "다음 주"}
    period = week_range(reference_at.date(), offset_weeks=offsets[direction])
    return _answer(f"{direction}_week", f"{labels[direction]} 기간은 {period.start:%Y-%m-%d} ({_weekday(period.start)}요일)부터 {period.end:%Y-%m-%d} ({_weekday(period.end)}요일)까지입니다.", reference_at)

def _relative_month_answer(direction: str, reference_at: datetime) -> DateTimeToolAnswer:
    offsets = {"this": 0, "last": -1, "next": 1}
    labels = {"this": "이번 달", "last": "지난 달", "next": "다음 달"}
    period = month_range(reference_at.date(), offset_months=offsets[direction])
    return _answer(f"{direction}_month", f"{labels[direction]} 기간은 {period.start:%Y-%m-%d}부터 {period.end:%Y-%m-%d}까지입니다.", reference_at)

def resolve_datetime_question(text: object, *, now: datetime | None = None) -> DateTimeToolAnswer | None:
    """Recognize only explicit calendar/time questions, otherwise leave Chat routing untouched."""
    question = re.sub(r"\s+", " ", str(text or "").strip())
    if not question:
        return None
    reference_at = operating_now(now=now)
    date_match = re.search(r"(?P<year>20\d{2})\s*(?:년|[-./])\s*(?P<month>\d{1,2})\s*(?:월|[-./])\s*(?P<day>\d{1,2})\s*일?", question)
    if date_match and "요일" in question:
        try:
            target = date(int(date_match.group("year")), int(date_match.group("month")), int(date_match.group("day")))
        except ValueError:
            return _answer("invalid_date", "입력한 날짜를 확인할 수 없습니다.", reference_at)
        return _answer("specific_weekday", f"{target.year}년 {target.month}월 {target.day}일은 {_weekday(target)}요일입니다.", reference_at)
    if re.search(r"(?:지금|현재)\s*(?:몇\s*시|시간|시각)", question):
        return _answer("current_time", f"현재 시각은 {reference_at.year}년 {reference_at.month}월 {reference_at.day}일 {reference_at:%H시 %M분 %S초}입니다.", reference_at)
    day_offsets = {"today": 0, "yesterday": -1, "tomorrow": 1}
    for kind, token in (("today", "오늘"), ("yesterday", "어제"), ("tomorrow", "내일")):
        if re.search(fr"{token}.*요일", question):
            target = reference_at.date() + timedelta(days=day_offsets[kind])
            return _answer(f"{kind}_weekday", f"{token}은 {_weekday(target)}요일입니다.", reference_at)
        if re.search(fr"{token}.*(?:날짜|몇\s*일|며칠)", question):
            return _relative_day_answer(kind, reference_at)
    week_match = re.search(r"(이번|지난|다음)\s*주", question)
    if week_match and (re.fullmatch(r"(이번|지난|다음)\s*주[?!。.]?", question) or re.search(r"기간|시작|종료|언제|날짜|며칠", question)):
        direction = {"이번": "this", "지난": "last", "다음": "next"}[week_match.group(1)]
        return _relative_week_answer(direction, reference_at)
    month_match = re.search(r"(이번|지난|다음)\s*달", question)
    if month_match and (re.fullmatch(r"(이번|지난|다음)\s*달[?!。.]?", question) or re.search(r"기간|시작|종료|언제|날짜|며칠", question)):
        direction = {"이번": "this", "지난": "last", "다음": "next"}[month_match.group(1)]
        return _relative_month_answer(direction, reference_at)
    if re.search(r"(?:오늘|현재|지금).*(?:날짜|몇\s*일|며칠)", question):
        return _relative_day_answer("today", reference_at)
    return None