"""KASI special-day OpenAPI client used only by calendar ingestion tools.

Runtime business-day checks never call this module's network functions.  The
official payload includes non-holiday special days, so callers must retain
only records whose public-holiday flag is affirmative.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Callable, Mapping
from urllib.parse import unquote, urlencode
from urllib.request import urlopen
from xml.etree import ElementTree

from app.utils.env_config import read_project_env_file


KASI_SPECIAL_DAY_SOURCE = "KASI_SPCDE_OPENAPI"
KASI_SPECIAL_DAY_ENDPOINT = "https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"
HOLIDAY_API_SERVICE_KEY_ENV = "SSAI_HOLIDAY_API_SERVICE_KEY"
HOLIDAY_API_ENDPOINT_ENV = "SSAI_HOLIDAY_API_ENDPOINT"


@dataclass(frozen=True)
class OfficialSpecialDay:
    holiday_date: str
    source_sequence: str
    holiday_name: str
    holiday_type: str
    is_holiday: bool


@dataclass(frozen=True)
class HolidayApiSettings:
    service_key: str | None
    endpoint: str


def holiday_api_settings(environ: Mapping[str, str] | None = None) -> HolidayApiSettings:
    """Resolve ingestion-only configuration without printing sensitive values."""
    values = dict(read_project_env_file())
    values.update(dict(os.environ if environ is None else environ))
    service_key = str(values.get(HOLIDAY_API_SERVICE_KEY_ENV) or "").strip() or None
    endpoint = str(values.get(HOLIDAY_API_ENDPOINT_ENV) or KASI_SPECIAL_DAY_ENDPOINT).strip()
    return HolidayApiSettings(service_key=service_key, endpoint=endpoint)


def _tag_name(element: ElementTree.Element) -> str:
    return str(element.tag).rsplit("}", 1)[-1]


def _item_value(item: ElementTree.Element, name: str) -> str:
    for child in item:
        if _tag_name(child) == name:
            return str(child.text or "").strip()
    return ""


def _valid_yyyymmdd(value: str) -> str:
    candidate = str(value or "").strip()
    if len(candidate) != 8 or not candidate.isdigit():
        return ""
    try:
        date(int(candidate[:4]), int(candidate[4:6]), int(candidate[6:]))
    except ValueError:
        return ""
    return candidate


def parse_special_day_xml(payload: bytes | str) -> tuple[OfficialSpecialDay, ...]:
    """Parse KASI special-day XML while preserving non-holiday rows for audit.

    KASI exposes ``isHoliday`` for public-institution holiday status.  Date
    names or special-day kinds alone are deliberately not interpreted as a
    holiday decision.
    """
    root = ElementTree.fromstring(payload)
    result_code = next((str(node.text or "").strip() for node in root.iter() if _tag_name(node) == "resultCode"), "")
    if result_code and result_code not in {"00", "0000"}:
        raise RuntimeError(f"kasi_special_day_api_error:{result_code}")

    rows: list[OfficialSpecialDay] = []
    for item in (node for node in root.iter() if _tag_name(node) == "item"):
        holiday_date = _valid_yyyymmdd(_item_value(item, "locdate"))
        if not holiday_date:
            continue
        flag = _item_value(item, "isHoliday").upper()
        rows.append(
            OfficialSpecialDay(
                holiday_date=holiday_date,
                source_sequence=_item_value(item, "seq"),
                holiday_name=_item_value(item, "dateName"),
                holiday_type=_item_value(item, "dateKind"),
                is_holiday=flag in {"Y", "1", "TRUE"},
            )
        )
    return tuple(rows)


def fetch_special_days_for_month(
    *,
    year: int,
    month: int,
    settings: HolidayApiSettings | None = None,
    timeout_seconds: int = 15,
    opener: Callable[..., object] = urlopen,
) -> tuple[OfficialSpecialDay, ...]:
    """Fetch one month for an explicit ingestion run, never for runtime reads."""
    configured = settings or holiday_api_settings()
    if not configured.service_key:
        raise RuntimeError("holiday_api_service_key_missing")
    if int(month) < 1 or int(month) > 12:
        raise ValueError("month must be between 1 and 12")
    query = urlencode({
        # data.go.kr keys are often copied in already percent-encoded form.
        # Normalize first so urlencode performs exactly one encoding pass.
        "serviceKey": unquote(configured.service_key),
        "solYear": f"{int(year):04d}",
        "solMonth": f"{int(month):02d}",
        "numOfRows": "100",
    })
    with opener(f"{configured.endpoint}?{query}", timeout=max(1, int(timeout_seconds))) as response:  # type: ignore[union-attr]
        payload = response.read()
    return parse_special_day_xml(payload)


def fetch_official_holidays_for_year(
    *,
    year: int,
    settings: HolidayApiSettings | None = None,
    timeout_seconds: int = 15,
    opener: Callable[..., object] = urlopen,
) -> tuple[OfficialSpecialDay, ...]:
    """Return only official public holidays, including official substitutes."""
    values: dict[tuple[str, str], OfficialSpecialDay] = {}
    for month in range(1, 13):
        for item in fetch_special_days_for_month(
            year=year,
            month=month,
            settings=settings,
            timeout_seconds=timeout_seconds,
            opener=opener,
        ):
            if item.is_holiday:
                if not item.source_sequence:
                    raise RuntimeError("kasi_special_day_sequence_missing")
                values[(item.holiday_date, item.source_sequence)] = item
    return tuple(values[key] for key in sorted(values))
