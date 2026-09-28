"""Shared in-memory date dimensions for current-table follow-up results."""

from __future__ import annotations

import pandas as pd


_WEEKDAY_LABELS = {
    0: "월요일",
    1: "화요일",
    2: "수요일",
    3: "목요일",
    4: "금요일",
    5: "토요일",
    6: "일요일",
}


def derive_current_table_time_grouping(values: pd.Series, grouping: str) -> pd.Series:
    """Return a display-safe date dimension without modifying the source table."""
    normalized = (
        values.fillna("")
        .astype("string")
        .str.replace(r"\D", "", regex=True)
        .str.slice(0, 8)
    )
    dates = pd.to_datetime(normalized, format="%Y%m%d", errors="coerce")
    if grouping == "month":
        return dates.dt.strftime("%Y-%m").astype("string").fillna("")
    if grouping == "weekday":
        return dates.dt.dayofweek.map(_WEEKDAY_LABELS).astype("string").fillna("")
    if grouping == "day":
        return dates.dt.strftime("%Y-%m-%d").astype("string").fillna("")
    if grouping == "ymd":
        return dates.dt.strftime("%Y%m%d").astype("string").fillna("")
    return pd.Series("", index=values.index, dtype="string")
