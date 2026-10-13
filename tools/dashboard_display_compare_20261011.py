"""Typed comparison of dashboard source, display, and Excel cells."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
import re
from typing import Any

import openpyxl
import pandas as pd

from app.services.dashboard_risk_detail_export import build_dashboard_inventory_detail_excel_bytes
from app.sims.views.dashboard_lite import _integrated_inventory_detail_display_frame


FIELDS = (
    "제품코드", "규격", "현재재고금액", "재고평가단가", "최근 정상 입고일",
    "최근입고 대표매입처코드", "최근입고 대표매입처명",
)
NUMERIC_FIELDS = {"현재재고금액", "재고평가단가"}
DATE_FIELDS = {"최근 정상 입고일"}


def _missing(value: Any) -> bool:
    if value is None or isinstance(value, str) and not value.strip():
        return True
    try:
        result = pd.isna(value)
        return result is pd.NA or bool(result)
    except (TypeError, ValueError):
        return False


def _value(value: Any, column: str) -> Any:
    if _missing(value):
        return None
    if column in DATE_FIELDS:
        if isinstance(value, (date, datetime, pd.Timestamp)):
            return value.strftime("%Y-%m-%d")
        digits = re.sub(r"\D", "", str(value).strip())
        if len(digits) == 8:
            try:
                return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8])).isoformat()
            except ValueError:
                pass
    if column in NUMERIC_FIELDS:
        try:
            number = Decimal(str(value).replace(",", ""))
            return number if number.is_finite() else str(value)
        except Exception:
            return str(value)
    return str(value)


def _equal(left: Any, right: Any, column: str) -> bool:
    a, b = _value(left, column), _value(right, column)
    if isinstance(a, Decimal) and isinstance(b, Decimal):
        # Excel/openpyxl can pass through float; allow only sub-micro unit noise.
        return abs(a - b) <= Decimal("0.0000001")
    return a == b


def _evidence(value: Any) -> dict[str, str]:
    if _missing(value):
        rendered = "<missing>"
    else:
        rendered = repr(value)
    return {"value": rendered[:160], "type": type(value).__name__}


def compare_dashboard_sample(frame: pd.DataFrame) -> dict[str, Any]:
    """Compare semantic cells; retain format-only differences as evidence."""
    if frame.empty or not frame["제품코드"].is_unique:
        raise ValueError("sample must have unique product codes")
    screen = _integrated_inventory_detail_display_frame(frame, len(frame))
    excel_bytes, export_info = build_dashboard_inventory_detail_excel_bytes(frame, [])
    sheet = openpyxl.load_workbook(BytesIO(excel_bytes), read_only=True, data_only=True).active
    headers = [cell.value for cell in sheet[1]]
    missing_columns = [field for field in FIELDS if field not in frame or field not in screen or field not in headers]
    if missing_columns:
        return {"status": "FAIL", "missing_columns": missing_columns, "mismatches": [], "format_only": []}
    mismatches: list[dict[str, Any]] = []
    format_only: list[dict[str, Any]] = []
    for offset, (_, raw) in enumerate(frame.iterrows()):
        shown = screen.iloc[offset]
        for column in FIELDS:
            workbook_value = sheet.cell(offset + 2, headers.index(column) + 1).value
            raw_value, display_value = raw[column], shown[column]
            semantically_equal = _equal(raw_value, display_value, column) and _equal(raw_value, workbook_value, column)
            representation_equal = repr(raw_value) == repr(display_value) == repr(workbook_value)
            if semantically_equal and representation_equal:
                continue
            evidence = {
                "product_code": str(raw["제품코드"]), "column": column,
                "source": _evidence(raw_value), "screen": _evidence(display_value),
                "excel": _evidence(workbook_value),
                "classification": "display_format_difference" if semantically_equal else "value_difference",
            }
            (format_only if semantically_equal else mismatches).append(evidence)
    return {
        "status": "PASS" if not mismatches else "FAIL",
        "rows": len(frame), "excel_rows": export_info["export_rows"],
        "mismatches": mismatches, "format_only": format_only,
        "screen_columns": list(screen.columns), "excel_columns": headers,
    }
