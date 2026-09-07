"""Shared read-only query/result helpers for registered ERP table features."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable

import pandas as pd

from app.services.rddbc_io_common import build_result_payload, normalize_top


def execute_bound_select(sql: str, params: Iterable[Any]) -> pd.DataFrame:
    """Execute a SELECT with driver-bound values.

    SQL identifiers are owned by feature code. Only values are accepted here.
    """
    from app.db.mssql_client import query_to_df

    return query_to_df(str(sql), tuple(params))


def export_top() -> int:
    try:
        return max(1, int(os.getenv("SIMS_EXPORT_MAX_ROWS", "100000") or 100000))
    except Exception:
        return 100000


def _positive_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default)) or default))
    except Exception:
        return int(default)


@dataclass(frozen=True)
class RegisteredQueryLimits:
    display_top: int
    display_limit_rows: int
    display_limit_source: str
    requested_display_top: int | None
    source_top: int
    export_limit_rows: int
    source_safety_limit_rows: int
    source_limit_source: str


def registered_query_limits(
    params: dict[str, Any],
    *,
    display_context: str = "chat",
    source_limit_env: str = "SIMS_IO_QUERY_MAX_ROWS",
) -> RegisteredQueryLimits:
    """Resolve display and source limits without letting UI TOP widen source reads."""
    context = str(params.get("_display_context") or display_context).strip().lower()
    if context == "panel":
        display_env = "SIMS_PANEL_DISPLAY_MAX_ROWS"
        display_default = 2000
    else:
        display_env = "SIMS_CHAT_DISPLAY_MAX_ROWS"
        display_default = 2000
    display_limit = _positive_env(display_env, display_default)
    requested_raw = params.get("display_top", params.get("top"))
    requested: int | None = None
    if requested_raw not in (None, ""):
        requested = normalize_top(requested_raw, default=display_limit, max_value=display_limit)
    display_top = requested if requested is not None else display_limit

    export_limit = export_top()
    source_env = str(source_limit_env or "SIMS_IO_QUERY_MAX_ROWS").strip()
    if source_env not in {"SIMS_IO_QUERY_MAX_ROWS", "SIMS_EXPORT_MAX_ROWS"}:
        raise ValueError(f"지원하지 않는 source limit 환경계약입니다: {source_env}")
    if source_env == "SIMS_EXPORT_MAX_ROWS":
        source_safety_limit = export_limit
        source_top = export_limit
        source_limit_source = "SIMS_EXPORT_MAX_ROWS"
    else:
        source_safety_limit = _positive_env("SIMS_IO_QUERY_MAX_ROWS", 30000)
        source_top = source_safety_limit
        source_limit_source = "SIMS_IO_QUERY_MAX_ROWS"
    return RegisteredQueryLimits(
        display_top=display_top,
        display_limit_rows=display_limit,
        display_limit_source=display_env,
        requested_display_top=requested,
        source_top=source_top,
        export_limit_rows=export_limit,
        source_safety_limit_rows=source_safety_limit,
        source_limit_source=source_limit_source,
    )


def build_feature_result(
    *,
    table: str,
    title: str,
    params: dict[str, Any],
    df: pd.DataFrame,
    summary_md: str,
) -> dict[str, Any]:
    row_count = int(len(df)) if isinstance(df, pd.DataFrame) else 0
    if row_count == 0:
        return {
            "table": table,
            "title": title,
            "action": title,
            "params": dict(params),
            "data": "해당 자료가 없습니다.",
            "message": "해당 자료가 없습니다.",
            "final": True,
            "meta": {
                "row_count": 0,
                "row_count_total": 0,
                "result_status": "no_data",
                "source_call_count": 1,
                "summary_md": summary_md,
                "registered_erp_table": True,
                "semantic_styled_max_rows": 300,
            },
        }
    payload = build_result_payload(
        table=table,
        title=title,
        action=title,
        params=params,
        df=df,
        message=f"{title} {row_count:,}건",
    )
    meta = dict(payload.get("meta") or {})
    meta.update(
        {
            "row_count": row_count,
            "row_count_total": row_count,
            "result_status": "success",
            "source_call_count": 1,
            "summary_md": summary_md,
            "registered_erp_table": True,
            "semantic_styled_max_rows": 300,
        }
    )
    payload["meta"] = meta
    return payload
