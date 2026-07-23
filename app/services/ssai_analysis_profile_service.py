"""Company-scoped Dashboard condition profile storage.

The SSAI management DB schema is installed by the explicit migration tool; this
runtime service never creates tables implicitly.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from app.services.ssai_auth_service import connect_ssai_db

log = logging.getLogger("ssai.analysis_profile")

PROFILE_PERMISSION = "ANALYSIS_PROFILE_MANAGE"
_PROFILE_KEYS = (
    "stock_mode", "stock_cd_list", "vendor_group_list", "vendor_kind_list",
    "product_group_list", "product_di_list", "product_class_list", "io_gu_list",
    "major_purchase_vendor_days", "risk_analysis_days", "overstock_inactive_days",
    "readiness_warning_pct", "risk_quick_view_count", "amount_display_unit",
)


def profile_conditions_for_storage(params: Mapping[str, Any]) -> dict[str, Any]:
    """Return the allow-listed, non-date Dashboard condition object."""
    source = dict(params or {})
    out: dict[str, Any] = {}
    for key in _PROFILE_KEYS:
        value = source.get(key)
        if isinstance(value, (list, tuple, set)):
            out[key] = sorted({str(item).strip() for item in value if str(item).strip()})
        elif value is not None:
            out[key] = value
    return out


def _profile_log_summary(profile: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a non-sensitive summary suitable for runtime logging."""
    data = dict(profile or {})
    io_values = [str(value).strip() for value in data.get("io_gu_list") or [] if str(value).strip()]
    return {
        "condition_keys": ",".join(sorted(str(key) for key in data.keys())),
        "io_gu_count": len(io_values),
        "io_gu_sample": ",".join(io_values[:3]),
    }


def load_dashboard_profile(*, company_id: int) -> dict[str, Any] | None:
    try:
        with connect_ssai_db() as conn:
            row = conn.cursor().execute(
                """
                SELECT TOP 1 profile_json
                FROM dbo.SSAI_ANALYSIS_PROFILES
                WHERE company_id = ?
                """,
                int(company_id),
            ).fetchone()
    except Exception as exc:
        log.warning("[analysis_profile.load] company_id=%s loaded=False error_type=%s", company_id, type(exc).__name__)
        return None
    if not row:
        log.info("[analysis_profile.load] company_id=%s profile_found=False", company_id)
        return None
    try:
        value = json.loads(str(row[0] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    summary = _profile_log_summary(value)
    log.info(
        "[analysis_profile.load] company_id=%s profile_found=True condition_keys=%s io_gu_count=%s io_gu_sample=%s",
        company_id, summary["condition_keys"], summary["io_gu_count"], summary["io_gu_sample"],
    )
    return value


def save_dashboard_profile(
    *,
    company_id: int,
    params: Mapping[str, Any],
    actor_user_id: int | None = None,
) -> str:
    conditions = profile_conditions_for_storage(params)
    payload = json.dumps(conditions, ensure_ascii=False, sort_keys=True)
    with connect_ssai_db() as conn:
        cur = conn.cursor()
        existing = cur.execute(
            "SELECT TOP 1 profile_id FROM dbo.SSAI_ANALYSIS_PROFILES WHERE company_id = ?",
            int(company_id),
        ).fetchone()
        if existing:
            cur.execute(
                """UPDATE dbo.SSAI_ANALYSIS_PROFILES
                   SET profile_json = ?, user_id = COALESCE(?, user_id), updated_at = SYSDATETIME()
                   WHERE company_id = ?""",
                payload, actor_user_id, int(company_id),
            )
            action = "updated"
        else:
            cur.execute(
                """INSERT INTO dbo.SSAI_ANALYSIS_PROFILES
                   (user_id, company_id, profile_json, created_at, updated_at)
                   VALUES (?, ?, ?, SYSDATETIME(), SYSDATETIME())""",
                int(actor_user_id or 0), int(company_id), payload,
            )
            action = "inserted"
        conn.commit()
    summary = _profile_log_summary(conditions)
    log.info(
        "[analysis_profile.save] actor_user_id=%s company_id=%s action=%s condition_keys=%s io_gu_count=%s io_gu_sample=%s",
        actor_user_id, company_id, action, summary["condition_keys"], summary["io_gu_count"], summary["io_gu_sample"],
    )
    return action
