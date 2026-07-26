"""Company-scoped Dashboard condition profile storage.

The SSAI management DB schema is installed by the explicit migration tool; this
runtime service never creates tables implicitly.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Mapping, MutableMapping

from app.services.ssai_auth_service import connect_ssai_db

log = logging.getLogger("ssai.analysis_profile")

PROFILE_PERMISSION = "ANALYSIS_PROFILE_MANAGE"
_PROFILE_KEYS = (
    "stock_mode", "stock_cd_list", "vendor_group_list", "vendor_kind_list",
    "product_group_list", "product_di_list", "product_class_list", "io_gu_list",
    "major_purchase_vendor_days", "risk_analysis_days", "overstock_inactive_days",
    "readiness_warning_pct", "risk_quick_view_count", "amount_display_unit",
)

# Company Default may only contain these shared analysis inputs.  Keep the
# allow-list here so Dashboard, KPI, and NLQ never need to parse profile JSON
# or duplicate their own merge rules.
COMPANY_DEFAULT_KEYS = _PROFILE_KEYS
_CODE_LIST_KEYS = {
    "stock_cd_list",
    "vendor_group_list",
    "vendor_kind_list",
    "product_group_list",
    "product_di_list",
    "product_class_list",
    "io_gu_list",
}


def normalize_business_code(value: Any) -> str:
    """Keep ERP business codes as trimmed strings without numeric coercion."""
    if not isinstance(value, str):
        return ""
    return value.strip()


def normalize_business_code_pair(value: Any) -> str:
    """Return one exact ``Gcode:Tcode`` string, preserving leading zeroes."""
    pair = normalize_business_code(value)
    gcode, separator, tcode = pair.partition(":")
    gcode = normalize_business_code(gcode)
    tcode = normalize_business_code(tcode)
    return f"{gcode}:{tcode}" if separator and gcode and tcode else ""


def invalidate_analysis_profile_cache(
    session_state: MutableMapping[str, Any],
    company_id: int | str | None = None,
) -> None:
    """Evict one company's shared analysis-profile cache, or all entries."""
    cache = session_state.get("__analysis_profile_company_cache")
    if not isinstance(cache, dict):
        return
    if company_id is None:
        cache.clear()
        return
    company_key = str(company_id).strip()
    cache.pop(company_key, None)
    try:
        cache.pop(int(company_key), None)
    except (TypeError, ValueError):
        pass


def mark_analysis_profile_saved(
    session_state: MutableMapping[str, Any],
    company_id: int | str,
) -> int:
    """Invalidate one company and advance its in-session Default generation."""
    company_key = str(company_id).strip()
    invalidate_analysis_profile_cache(session_state, company_id=company_key)
    generation_key = f"__analysis_profile_generation::{company_key}"
    try:
        generation = int(session_state.get(generation_key) or 0) + 1
    except (TypeError, ValueError):
        generation = 1
    session_state[generation_key] = generation
    return generation


def get_analysis_profile_generation(
    session_state: Mapping[str, Any],
    company_id: int | str,
) -> int:
    """Return the current in-session Default generation for one company."""
    try:
        return max(0, int(session_state.get(f"__analysis_profile_generation::{str(company_id).strip()}") or 0))
    except (TypeError, ValueError):
        return 0


def profile_conditions_for_storage(params: Mapping[str, Any]) -> dict[str, Any]:
    """Return the allow-listed, non-date Dashboard condition object."""
    source = dict(params or {})
    out: dict[str, Any] = {}
    for key in _PROFILE_KEYS:
        value = source.get(key)
        if isinstance(value, (list, tuple, set)):
            out[key] = sorted({normalize_business_code(item) for item in value if normalize_business_code(item)})
        elif value is not None:
            out[key] = value
    return out


def normalize_company_default_conditions(profile: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a copy of the supported company Default fields only.

    Dates and Dashboard-only manufacturer input are intentionally absent from
    ``_PROFILE_KEYS``.  Code lists are made deterministic without mutating the
    management-DB payload passed by the caller.
    """
    source = dict(profile or {})
    normalized: dict[str, Any] = {}
    for key in COMPANY_DEFAULT_KEYS:
        if key not in source:
            continue
        value = source.get(key)
        if key in _CODE_LIST_KEYS:
            values = value if isinstance(value, (list, tuple, set)) else [value]
            normalized[key] = sorted({normalize_business_code(item) for item in values if normalize_business_code(item)})
        elif value is not None:
            normalized[key] = value
    return normalized


def normalize_analytics_multi_code_filter(
    selected_codes: Any,
    available_codes: Any,
    selected_pairs: Any = None,
    expected_gcode: str | None = None,
) -> dict[str, Any]:
    """Normalize a KPI/NLQ multi-code selection against its real option set."""
    def _values(raw: Any) -> list[str]:
        items = raw if isinstance(raw, (list, tuple, set)) else [raw]
        return list(dict.fromkeys(
            normalize_business_code(item) for item in items if normalize_business_code(item)
        ))

    selected = _values(selected_codes)
    available = _values(available_codes)
    pairs = _values(selected_pairs)
    selected_set = set(selected)
    available_set = set(available)
    expected = normalize_business_code(expected_gcode)
    pair_gcodes = {
        normalize_business_code(pair.rsplit(":", 1)[0])
        for pair in pairs if ":" in pair and normalize_business_code(pair.rsplit(":", 1)[0])
    }
    pair_gcode_matches = not pairs or not expected or pair_gcodes == {expected}
    is_full_selection = bool(available_set) and selected_set == available_set and pair_gcode_matches
    return {
        "effective_codes": [] if is_full_selection else selected,
        "effective_pairs": [] if is_full_selection else pairs,
        "is_full_selection": is_full_selection,
        "is_empty_selection": not selected_set,
        "selected_count": len(selected_set),
        "available_count": len(available_set),
        "pair_gcode_matches": pair_gcode_matches,
    }


def build_company_default_adapter(
    profile: Mapping[str, Any] | None,
    *,
    supported_keys: set[str] | tuple[str, ...] | list[str],
    explicit: Mapping[str, Any] | None = None,
    explicit_keys: set[str] | tuple[str, ...] | list[str] | None = None,
    clear_keys: set[str] | tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    """Merge one company Default with explicit conditions for one target.

    Explicit keys win even when their value is an empty list.  This represents
    an NLQ phrase such as ``전체 창고`` and prevents the Default from being
    reapplied.  Unsupported Default keys are reported for diagnostics only and
    are never forced into a target screen.
    """
    normalized = normalize_company_default_conditions(profile)
    supported = {str(key) for key in supported_keys}
    explicit_data = dict(explicit or {})
    explicit_set = {str(key) for key in (explicit_keys or ())}
    cleared = {str(key) for key in (clear_keys or ())}

    defaults = {key: value for key, value in normalized.items() if key in supported}
    unsupported = sorted(key for key in normalized if key not in supported)
    effective = dict(defaults)
    sources = {key: "default" for key in defaults}

    for key in supported:
        if key not in explicit_set and key not in cleared:
            continue
        if key in cleared:
            effective[key] = [] if key in _CODE_LIST_KEYS else ""
            sources[key] = "explicit_clear"
        else:
            effective[key] = explicit_data.get(key)
            sources[key] = "explicit"

    return {
        "defaults": defaults,
        "effective": effective,
        "sources": sources,
        "unsupported_default_keys": unsupported,
        "profile_found": bool(normalized),
        "applied_default_count": sum(1 for value in sources.values() if value == "default"),
        "explicit_override_count": sum(1 for value in sources.values() if value == "explicit"),
        "explicit_clear_count": sum(1 for value in sources.values() if value == "explicit_clear"),
    }


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
