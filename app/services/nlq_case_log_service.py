"""Safe append-only NLQ case logging at the final chat delivery boundary."""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import re
import threading
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Optional

from app.utils.env_config import config_path_any


_CASE_LOG_ENV = "SIMS_NLQ_CASE_LOG_FILE"
_WRITE_LOCK = threading.Lock()
_DEDUP_SESSION_KEY = "__sims_nlq_case_logged_request_ids"
_KST = dt.timezone(dt.timedelta(hours=9))
_FINAL_STATUSES = frozenset(
    {
        "success",
        "no_data",
        "input_required",
        "candidate_required",
        "unsupported",
        "routing_error",
        "not_found",
        "resolution_unavailable",
        "error",
        "unknown",
    }
)
_SAFE_CONDITION_KEYS = frozenset(
    {
        "date_from", "date_to", "month_from", "month_to", "ven_cd", "ven_cds",
        "product_cd", "product_cds", "physic_cd", "physic_cds", "maker_cd", "maker_cds",
        "product_ven_cd", "product_ven_cds", "order_cd", "order_cds", "sales_man",
        "stock_cd", "stock_cds", "stock_cd_list", "io_gu", "io_gu_list", "product_di",
        "product_di_list", "dashboard_product_di_list", "product_class", "product_class_list",
        "dashboard_product_class_list", "source_mode", "stock_mode", "date_basis", "flow_scope",
        "trans_di", "tax_di", "only_mismatch_trans", "only_mismatch_tax",
        "product_supplier_scope_mode", "ven_nm", "physic_nm", "maker_nm", "product_ven_nm",
        "order_nm", "sales_man_nm", "region_nm", "stock_nm", "nlq_unlabeled_name",
    }
)
_CANONICAL_CONDITION_ALIASES: dict[str, tuple[str, ...]] = {
    "transaction_vendor_name": ("ven_nm",),
    "transaction_vendor_codes": ("ven_cd", "ven_cds"),
    "product_name": ("physic_nm",),
    "product_codes": ("physic_cd", "physic_cds", "product_cd", "product_cds"),
    "manufacturer_name": ("maker_nm", "product_ven_nm"),
    "manufacturer_codes": ("maker_cd", "maker_cds", "product_ven_cd", "product_ven_cds"),
    "ordering_vendor_name": ("order_nm",),
    "ordering_vendor_codes": ("order_cd", "order_cds"),
    "sales_person_name": ("sales_man_nm",),
    "sales_person_codes": ("sales_man",),
    "unlabeled_name": ("nlq_unlabeled_name",),
    "stock_codes": ("stock_cd", "stock_cds", "stock_cd_list"),
    "io_codes": ("io_gu", "io_gu_list"),
    "product_type_codes": ("product_di", "product_di_list", "dashboard_product_di_list"),
    "product_tax_codes": ("product_class", "product_class_list", "dashboard_product_class_list"),
    "date_from": ("date_from",),
    "date_to": ("date_to",),
    "month_from": ("month_from",),
    "month_to": ("month_to",),
    "stock_mode": ("stock_mode",),
    "source_mode": ("source_mode",),
}
_CODE_CONDITION_KEYS = frozenset(
    {
        "transaction_vendor_codes", "product_codes", "manufacturer_codes", "ordering_vendor_codes",
        "sales_person_codes", "stock_codes", "io_codes", "product_type_codes", "product_tax_codes",
    }
)


def _safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return None


def _safe_value(value: Any) -> Any:
    scalar = _safe_scalar(value)
    if scalar is not None or value is None:
        return scalar
    if isinstance(value, (list, tuple, set)):
        return [item for item in (_safe_scalar(item) for item in value) if item is not None]
    return None


def _safe_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return None


def _first_int(*values: Any) -> Optional[int]:
    for value in values:
        result = _safe_int(value)
        if result is not None:
            return result
    return None


def _compact_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _safe_conditions(params: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in params.items():
        normalized_key = str(key or "").strip().lower()
        if normalized_key not in _SAFE_CONDITION_KEYS:
            continue
        safe_value = _safe_value(value)
        if safe_value not in (None, "", []):
            result[normalized_key] = safe_value
    return result


def _as_text_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = [value]
    return [str(item).strip() for item in values if str(item or "").strip()]


def _canonical_conditions(params: Mapping[str, Any]) -> dict[str, Any]:
    """Map already-final query parameters to stable, non-internal case-log keys."""
    safe_params = _safe_conditions(params)
    result: dict[str, Any] = {}
    for output_key, source_keys in _CANONICAL_CONDITION_ALIASES.items():
        values: list[str] = []
        for source_key in source_keys:
            values.extend(_as_text_values(safe_params.get(source_key)))
        values = list(dict.fromkeys(values))
        if not values:
            continue
        if output_key in _CODE_CONDITION_KEYS:
            result[output_key] = values
        else:
            result[output_key] = values[0]
    return result


def _format_iso_kst(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        try:
            parsed = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_KST)
    return parsed.astimezone(_KST).isoformat(timespec="milliseconds")


def _now_kst() -> str:
    return dt.datetime.now(_KST).isoformat(timespec="milliseconds")


def resolve_nlq_case_log_path(*, environ: Mapping[str, str] | None = None) -> Path:
    """Use the configured case path or the existing app.log path resolver."""
    source = environ if environ is not None else os.environ
    configured = str(source.get(_CASE_LOG_ENV) or "").strip()
    if configured:
        return Path(configured)
    app_log_path = config_path_any(("LOG_FILE", "SIMS_LOG_FILE"), environ=source)
    return app_log_path.parent / "nlq_cases.jsonl"


def _resolved_conditions(meta: Mapping[str, Any], conditions: Mapping[str, Any]) -> dict[str, Any]:
    resolved_codes: list[str] = []
    for key in _CODE_CONDITION_KEYS:
        value = conditions.get(key)
        if isinstance(value, list):
            resolved_codes.extend(str(item) for item in value if str(item).strip())
    kinds = meta.get("resolved_entity_types")
    if not isinstance(kinds, (list, tuple, set)):
        one_kind = str(meta.get("resolved_kind") or "").strip()
        kinds = [one_kind] if one_kind else []
    normalized_codes = list(dict.fromkeys(code for code in resolved_codes if code))
    return {
        "resolver_status": str(meta.get("entity_resolution_status") or "") or None,
        "resolved_entity_types": [str(kind) for kind in kinds if str(kind).strip()][:5],
        "resolved_code_count": len(normalized_codes),
        "resolved_code_samples": normalized_codes[:5],
    }


def _format_date_label(value: Any, *, monthly: bool = False) -> str:
    text = str(value or "").strip()
    if monthly and re.fullmatch(r"\d{6}", text):
        return f"{text[:4]}-{text[4:]}"
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def _period_policy_label(policy: Mapping[str, Any]) -> str:
    if bool(policy.get("explicit_period_present")):
        return "명시기간"
    if not bool(policy.get("auto_applied")):
        return ""
    default_policy = str(policy.get("default_policy") or "").strip()
    labels = {
        "recent_1day": "기본기간 최근 1일",
        "recent_1month": "기본기간 최근 1개월",
        "recent_7days": "제품수불 최근 7일",
        "inventory_current_month": "기본기간 현재월",
        "inventory_month": "재고 기준월",
    }
    return labels.get(default_policy, "기본기간 자동적용")


def _condition_summary(
    conditions: Mapping[str, Any],
    period_policy: Mapping[str, Any],
    *,
    search_mode: str,
) -> str:
    parts: list[str] = []
    date_from = _format_date_label(conditions.get("date_from"))
    date_to = _format_date_label(conditions.get("date_to"))
    if date_from or date_to:
        parts.append(f"기간 {date_from or date_to}" + (f" ~ {date_to}" if date_from and date_to and date_from != date_to else ""))
    elif conditions.get("month_from") or conditions.get("month_to"):
        month_from = _format_date_label(conditions.get("month_from"), monthly=True)
        month_to = _format_date_label(conditions.get("month_to"), monthly=True)
        parts.append(f"기간 {month_from or month_to}" + (f" ~ {month_to}" if month_from and month_to and month_from != month_to else ""))
    labels = (
        ("transaction_vendor_name", "거래처명"), ("product_name", "제품명"),
        ("manufacturer_name", "제조사명"), ("ordering_vendor_name", "발주처명"),
        ("sales_person_name", "영업사원명"), ("unlabeled_name", "무라벨명"),
    )
    for key, label in labels:
        value = _compact_text(conditions.get(key))
        if value:
            parts.append(f"{label} {value}")
    if search_mode == "unlabeled_or":
        parts.append("거래처·제품·제조사 OR")
    policy_label = _period_policy_label(period_policy)
    if policy_label:
        parts.append(policy_label)
    return " / ".join(parts) if parts else "조건 없음"


def _interpretation(meta: Mapping[str, Any], conditions: Mapping[str, Any]) -> dict[str, Any]:
    policy = meta.get("period_policy") if isinstance(meta.get("period_policy"), Mapping) else {}
    extracted = dict(conditions)
    search_mode = str(meta.get("search_mode") or "").strip()
    raw_fields = meta.get("search_fields")
    field_map = {
        "ven_nm": "transaction_vendor", "physic_nm": "product", "maker_nm": "manufacturer",
        "product_ven_nm": "manufacturer", "order_nm": "ordering_vendor", "sales_man_nm": "sales_person",
    }
    search_fields = [field_map.get(str(item), str(item)) for item in raw_fields if str(item).strip()] if isinstance(raw_fields, (list, tuple, set)) else []
    if search_mode == "unlabeled_or":
        search_fields = ["transaction_vendor", "product", "manufacturer"]
    search_fields = list(dict.fromkeys(search_fields))[:8]
    resolved = _resolved_conditions(meta, conditions)
    missing_fields: list[str] = []
    if "parsed_action" not in meta:
        missing_fields.append("parsed_action")
    if "canonical_action" not in meta:
        missing_fields.append("canonical_action")
    if not isinstance(meta.get("period_policy"), Mapping):
        missing_fields.extend(("action_class", "period_policy"))
    if "search_mode" not in meta:
        missing_fields.append("search_mode")
    if "entity_resolution_status" not in meta:
        missing_fields.append("resolved_conditions.resolver_status")
    return {
        "parsed_action": str(meta.get("parsed_action") or ""),
        "canonical_action": str(meta.get("canonical_action") or ""),
        "action_class": str(policy.get("action_class") or meta.get("action_class") or ""),
        "explicit_period_present": bool(policy.get("explicit_period_present")),
        "period_policy": str(policy.get("default_policy") or ""),
        "period_reason": str(policy.get("policy_reason") or ""),
        "period_auto_applied": bool(policy.get("auto_applied")),
        "search_mode": search_mode,
        "search_fields": search_fields,
        "condition_summary": _condition_summary(conditions, policy, search_mode=search_mode),
        "extracted_conditions": extracted,
        "resolved_conditions": resolved,
        "missing_fields": missing_fields,
    }


def _result_status(meta: Mapping[str, Any], *, total_rows: int | None, candidate_count: int | None) -> tuple[str, str, str]:
    raw = str(meta.get("result_status") or "").strip()
    if raw in _FINAL_STATUSES:
        return raw, raw, "payload"
    if str(meta.get("error_class") or "").strip() or str(meta.get("error_code") or "").strip():
        return raw, "error", "derived"
    if candidate_count and candidate_count > 0:
        return raw, "candidate_required", "derived"
    if total_rows is not None and total_rows > 0:
        return raw, "success", "derived"
    if total_rows == 0:
        return raw, "no_data", "derived"
    return raw, "unknown", "derived"


def _source_status(meta: Mapping[str, Any], key: str) -> str | None:
    value = str(meta.get(key) or "").strip().lower()
    return value if value in {"queried", "cache", "not_required", "unavailable"} else None


def append_nlq_case_record(
    payload: Mapping[str, Any],
    session_state: MutableMapping[str, Any],
    *,
    runtime_context: Optional[Mapping[str, Any]] = None,
    question: Any = "",
    normalized_question: Any = "",
    logger: Optional[logging.Logger] = None,
) -> bool:
    """Append one final safe NLQ case record; logging never affects chat delivery."""
    meta = payload.get("meta") if isinstance(payload.get("meta"), Mapping) else {}
    if not bool(meta.get("nlq")):
        return False
    request_id = str(meta.get("nlq_trace_request_id") or payload.get("id") or "").strip()
    if not request_id:
        return False
    logged_ids = session_state.get(_DEDUP_SESSION_KEY)
    if not isinstance(logged_ids, set):
        logged_ids = set(logged_ids or [])
        session_state[_DEDUP_SESSION_KEY] = logged_ids
    if request_id in logged_ids:
        return False
    logged_ids.add(request_id)

    params = payload.get("params") if isinstance(payload.get("params"), Mapping) else {}
    conditions = _canonical_conditions(params)
    raw_status_hint = str(meta.get("result_status") or "").strip()
    is_early_status = raw_status_hint in {"input_required", "candidate_required", "resolution_unavailable"}
    full_source_rows = None if is_early_status else _first_int(meta.get("download_row_count"), meta.get("full_source_row_count"))
    display_rows = None if is_early_status else _first_int(meta.get("display_row_count"), meta.get("row_count"))
    total_rows = full_source_rows if full_source_rows is not None else display_rows
    candidate_count = _first_int(meta.get("candidate_count"))
    raw_result_status, result_status, result_status_source = _result_status(
        meta, total_rows=total_rows, candidate_count=candidate_count
    )
    action = str(meta.get("action") or payload.get("action") or payload.get("title") or "").strip()
    interpretation = _interpretation(meta, conditions)
    elapsed_ms = _first_int(meta.get("elapsed_ms"))
    runtime = dict(runtime_context or {})
    notice_codes = meta.get("notice_codes")
    if not isinstance(notice_codes, (list, tuple, set)):
        notice_codes = []
    notice_codes = list(dict.fromkeys(str(code) for code in notice_codes if str(code).strip()))[:8]
    occurred_at = _format_iso_kst(meta.get("request_started_at"))
    completed_at = _format_iso_kst(meta.get("response_completed_at"))
    source_call_count = _first_int(meta.get("source_call_count"))

    record = {
        "occurred_at": occurred_at,
        "completed_at": completed_at,
        "logged_at": _now_kst(),
        "schema_version": "2.0",
        "request_id": request_id,
        "company_id": runtime.get("company_id"),
        "room_id": str(runtime.get("room_id") or ""),
        "question": _compact_text(question or meta.get("nlq_query")),
        "normalized_question": _compact_text(normalized_question or meta.get("nlq_query")),
        "route": str(meta.get("route") or ("analytics" if meta.get("analysis_nlq") else "sims")),
        "action": action,
        "canonical_action": str(meta.get("canonical_action") or ""),
        "interpretation": interpretation,
        "period_policy": str(interpretation.get("period_policy") or ""),
        "period_auto_applied": bool(interpretation.get("period_auto_applied")),
        "raw_result_status": raw_result_status,
        "result_status": result_status,
        "result_status_source": result_status_source,
        "stage": "delivery_finalized",
        "conditions": conditions,
        "manufacturer_code": str((conditions.get("manufacturer_codes") or [""])[0] or ""),
        "manufacturer_name": str(conditions.get("manufacturer_name") or ""),
        "stock_mode": str(conditions.get("stock_mode") or ""),
        "source_mode": str(conditions.get("source_mode") or ""),
        "final_date_from": str(conditions.get("date_from") or ""),
        "final_date_to": str(conditions.get("date_to") or ""),
        "notice_codes": notice_codes,
        "answer_title": str(payload.get("title") or action),
        "total_rows": total_rows,
        "display_rows": display_rows,
        "full_source_rows": full_source_rows,
        "source_call_count": source_call_count,
        "cache_used": meta.get("cache_used") if isinstance(meta.get("cache_used"), bool) else None,
        "display_source_status": _source_status(meta, "display_source_status"),
        "full_source_status": _source_status(meta, "full_source_status"),
        "elapsed_ms": elapsed_ms,
        "total_elapsed_ms": _first_int(meta.get("total_elapsed_ms"), elapsed_ms),
        "error_class": str(meta.get("error_class") or ""),
        "sql_error_number": _first_int(meta.get("sql_error_number")),
        "error_code": str(meta.get("error_code") or ""),
        "candidate_count": candidate_count,
        "requested_metric": str(meta.get("requested_metric") or ""),
        "requested_grouping": str(meta.get("requested_grouping") or ""),
        "resolved_action": str(meta.get("resolved_action") or ""),
        "execution_status": str(meta.get("execution_status") or ""),
        "intent_validation_status": str(meta.get("intent_validation_status") or ""),
        "entity_query": str(meta.get("entity_query") or "")[:128],
        "entity_resolution_scope": str(meta.get("entity_resolution_scope") or ""),
        "entity_lookup_call_count": _first_int(meta.get("entity_lookup_call_count")),
        "candidate_count_total": _first_int(meta.get("candidate_count_total")),
        "compatible_candidate_count": _first_int(meta.get("compatible_candidate_count")),
        "resolved_entity_role": str(meta.get("resolved_entity_role") or ""),
        "resolved_entity_code": str(meta.get("resolved_entity_code") or ""),
        "resolved_entity_name": str(meta.get("resolved_entity_name") or "")[:128],
        "llm_explanation_used": bool(meta.get("llm_explanation_used")),
        "llm_explanation_status": str(meta.get("llm_explanation_status") or "")[:64],
        "intent_consistency_flags": [
            str(flag) for flag in (meta.get("consistency_flags") or [])
            if str(flag).strip()
        ] if isinstance(meta.get("consistency_flags"), (list, tuple, set)) else [],
        "review_status": "pending",
        "review_note": "",
        "consistency_flags": {
            "occurred_at_missing": occurred_at is None,
            "raw_result_status_missing": not bool(raw_result_status),
            "result_status_derived": result_status_source == "derived",
            "result_status_unknown": result_status == "unknown",
            "total_rows_missing": total_rows is None,
            "rows_present_but_not_found": result_status == "not_found" and bool((total_rows or 0) > 0),
            "success_with_zero_rows": result_status == "success" and total_rows == 0,
            "no_data_with_positive_rows": result_status == "no_data" and bool((total_rows or 0) > 0),
            "rows_present_but_entity_not_found_notice": bool((total_rows or 0) > 0 and "entity_not_found" in notice_codes),
            "full_source_less_than_display": bool(full_source_rows is not None and display_rows is not None and full_source_rows < display_rows),
            "total_rows_less_than_display": bool(total_rows is not None and display_rows is not None and total_rows < display_rows),
            "total_rows_mismatch_full_source": bool(total_rows is not None and full_source_rows is not None and total_rows != full_source_rows),
            "source_call_count_missing": source_call_count is None,
            "display_source_status_missing": _source_status(meta, "display_source_status") is None,
            "full_source_status_missing": _source_status(meta, "full_source_status") is None,
            "elapsed_missing": elapsed_ms is None,
            "action_missing": not bool(action),
            "intent_validation_failed": str(meta.get("intent_validation_status") or "") == "fail",
            "interpretation_missing_fields": interpretation["missing_fields"],
        },
    }
    try:
        path = resolve_nlq_case_log_path()
        with _WRITE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        if logger is not None:
            logger.info("[nlq.case] request_id=%s action=%s result_status=%s total_rows=%s", request_id, action, result_status, total_rows if total_rows is not None else "")
        return True
    except Exception as exc:
        if logger is not None:
            logger.warning("[nlq.case] append_failed request_id=%s error_class=%s", request_id, type(exc).__name__)
        return False
