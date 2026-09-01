"""Pure, append-only mapping for the SIMS structured response envelope."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

from app.services.structured_tool_routing import ToolRouteDecision, validate_tool_route_decision


SCHEMA_VERSION = "sims.response.v1"

_ROUTE_KINDS = frozenset(
    {
        "sims_nlq",
        "analytics",
        "io",
        "vendor_master",
        "current_table",
        "knowledge_rag",
        "datetime_tool",
        "web_latest",
        "generic_llm",
        "attachment",
        "mcp_external_resource",
    }
)
_FORBIDDEN_KEY_TOKENS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "api_key",
        "connection_string",
        "connstr",
        "dsn",
        "raw_sql",
        "source_query",
        "bind",
        "session_state",
    }
)
_SAFE_CITATION_FIELDS = (
    "identifier",
    "document_id",
    "source_name",
    "source_kind",
    "version",
    "source_location",
    "artifact_hash",
    "source_hash",
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _safe_number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _safe_code(value: Any) -> str:
    text = _text(value)
    if not text or len(text) > 160:
        return ""
    return text


def _safe_parameter_keys(params: Mapping[str, Any]) -> list[str]:
    keys: list[str] = []
    for raw_key in params:
        key = _safe_code(raw_key)
        if not key or any(token in key.lower() for token in _FORBIDDEN_KEY_TOKENS):
            continue
        keys.append(key)
    return sorted(set(keys))


def _infer_route_kind(payload: Mapping[str, Any], meta: Mapping[str, Any], route_kind: str | None) -> str:
    explicit = _text(route_kind)
    if explicit in _ROUTE_KINDS:
        return explicit
    if bool(meta.get("current_table_followup")):
        return "current_table"
    if _mapping(payload.get("knowledge_evidence")) or _text(payload.get("type")) == "knowledge_answer":
        return "knowledge_rag"
    if _text(meta.get("message_type") or payload.get("message_type")) == "datetime_tool":
        return "datetime_tool"
    if bool(meta.get("web_search")):
        return "web_latest"
    candidate = _text(meta.get("route")).lower()
    aliases = {
        "vendors": "vendor_master",
        "vendor": "vendor_master",
        "dashboard": "analytics",
    }
    candidate = aliases.get(candidate, candidate)
    if candidate in _ROUTE_KINDS:
        return candidate
    if bool(meta.get("analytics")):
        return "analytics"
    if bool(meta.get("nlq")):
        return "sims_nlq"
    return "generic_llm"


def _decision_mode(route_kind: str, meta: Mapping[str, Any]) -> str:
    if route_kind == "datetime_tool":
        return "none"
    if route_kind in {"knowledge_rag", "web_latest"}:
        return "llm_summary"
    if route_kind == "generic_llm" or bool(meta.get("llm_analysis")):
        return "llm_stream"
    return "deterministic"


def _safe_citations(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, (list, tuple)):
        return []
    citations: list[dict[str, Any]] = []
    for item in raw:
        row = _mapping(item)
        safe = {key: row[key] for key in _SAFE_CITATION_FIELDS if key in row and _safe_scalar(row[key])}
        if safe:
            citations.append(safe)
    return citations


def _safe_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _safe_web_sources(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, (list, tuple)):
        return []
    sources: list[dict[str, Any]] = []
    for item in raw:
        row = _mapping(item)
        safe = {
            key: row[key]
            for key in ("title", "url", "source", "published_at")
            if key in row and _safe_scalar(row[key])
        }
        if safe:
            sources.append(safe)
    return sources


def _safe_mcp_resources(raw: Any) -> list[dict[str, Any]]:
    row = _mapping(raw)
    safe = {
        key: row[key]
        for key in ("resource_id", "title", "source_uri", "retrieved_at")
        if key in row and _safe_scalar(row[key])
    }
    return [safe] if safe else []


def build_structured_response_envelope(
    legacy: Mapping[str, Any] | None,
    *,
    route_kind: str | None = None,
    authorized_knowledge_evidence: bool = False,
    tool_route_decision: ToolRouteDecision | None = None,
) -> dict[str, Any]:
    """Build a JSON-safe envelope without mutating or executing legacy behavior."""
    payload = _mapping(legacy)
    meta = _mapping(payload.get("meta"))
    decision = tool_route_decision.to_dict() if tool_route_decision is not None else {}
    if decision:
        validate_tool_route_decision(decision)
    kind = _safe_code(decision.get("kind")) or _infer_route_kind(payload, meta, route_kind)
    raw_action = _safe_code(decision.get("action")) or _safe_code(payload.get("action") or meta.get("action") or payload.get("title"))
    canonical_action = _safe_code(decision.get("canonical_action")) or _safe_code(meta.get("canonical_action")) or raw_action
    params = _mapping(payload.get("params"))
    table_key = _safe_code(meta.get("table_key"))
    source_table_key = _safe_code(meta.get("source_table_key"))

    result_status = _safe_code(meta.get("result_status") or payload.get("result_status"))
    if not result_status and kind == "web_latest":
        result_status = _safe_code(meta.get("status"))
    execution_status = _safe_code(meta.get("execution_status") or payload.get("execution_status"))
    source_call_count = _safe_number(meta.get("source_call_count"))
    if kind == "datetime_tool" and source_call_count is None:
        source_call_count = 0

    evidence: dict[str, Any] = {
        "kind": "none",
        "citation_count": 0,
        "citations": [],
        "conflict_notices": [],
        "source_count": 0,
    }
    if kind == "knowledge_rag" and authorized_knowledge_evidence:
        snapshot = _mapping(payload.get("knowledge_evidence"))
        citations = _safe_citations(snapshot.get("citations"))
        notices = [
            {key: row[key] for key in ("conflict_group_id", "message") if key in row and _safe_scalar(row[key])}
            for item in snapshot.get("conflict_notices", []) if isinstance(item, Mapping)
            for row in [_mapping(item)]
        ]
        evidence.update(
            {
                "kind": "knowledge",
                "citation_count": len(citations),
                "citations": citations,
                "conflict_notices": [notice for notice in notices if notice],
                "source_count": len(citations),
            }
        )
    elif kind == "web_latest":
        sources = _safe_web_sources(meta.get("sources"))
        evidence.update({"kind": "web", "source_count": len(sources), "sources": sources})
    elif kind == "mcp_external_resource":
        resources = _safe_mcp_resources(meta.get("mcp_resource"))
        evidence.update({"kind": "mcp", "source_count": len(resources), "sources": resources})

    envelope = {
        "schema_version": SCHEMA_VERSION,
        "request": {
            "request_id": _safe_code(meta.get("request_id")),
            "input_kind": "followup" if bool(meta.get("current_table_followup")) else "chat_text",
            "trace_request_id": _safe_code(meta.get("nlq_trace_request_id")),
        },
        "route": {
            "kind": kind,
            "decision_mode": _safe_code(decision.get("decision_mode")) or _decision_mode(kind, meta),
            "handler": _safe_code(meta.get("handler_target") or meta.get("handler_kind")),
        },
        "action": {"raw": raw_action, "canonical": canonical_action},
        "arguments": {
            "conditions": {
                "present": bool(params),
                "query_summary_present": bool(_text(meta.get("query_summary") or meta.get("condition"))),
                "parameter_keys": _safe_parameter_keys(params),
            },
            "tool": dict(decision.get("safe_arguments") or {}),
            "scope": {
                key: _safe_number(meta.get(key))
                for key in ("product_scope_count", "supplier_scope_count", "stock_scope_count")
                if _safe_number(meta.get(key)) is not None
            },
        },
        "execution": {
            "result_status": result_status or None,
            "execution_status": execution_status or None,
            "reason_code": _safe_code(meta.get("reason_code")) or _safe_code(decision.get("reason_code")) or None,
            "error": {
                "class": _safe_code(meta.get("error_class") or meta.get("exception_class")) or None,
                "code": _safe_code(meta.get("error_code") or meta.get("safe_error_code")) or None,
            },
            "source_call_count": source_call_count,
            "elapsed_ms": _safe_number(meta.get("elapsed_ms") or meta.get("total_elapsed_ms")),
        },
        "result": {
            "row_count": _safe_number(meta.get("row_count")),
            "row_count_total": _safe_number(meta.get("row_count_total")),
            "candidate_count": _safe_number(meta.get("candidate_count")),
            "table": {
                "table_key": table_key or None,
                "source_table_key": source_table_key or None,
                "source_action": _safe_code(meta.get("source_action")) or None,
                "is_current_table_followup": bool(meta.get("current_table_followup")),
            },
        },
        "evidence": evidence,
        "presentation": {
            "message_type": _safe_code(meta.get("message_type") or payload.get("message_type") or payload.get("type")) or None,
            "title": _safe_code(payload.get("title")) or None,
            "summary_available": bool(_text(meta.get("summary_md") or payload.get("message") or payload.get("content"))),
            "table_available": _text(payload.get("type")) == "table" or bool(table_key),
            "download_available": bool(_safe_code(meta.get("download_table_key"))),
        },
    }
    validate_structured_response_envelope(envelope)
    return envelope


def validate_structured_response_envelope(envelope: Mapping[str, Any] | None) -> None:
    """Validate only the derived envelope; never validate or alter legacy input."""
    value = _mapping(envelope)
    required = {"schema_version", "request", "route", "action", "arguments", "execution", "result", "evidence", "presentation"}
    if set(value) != required:
        raise ValueError("structured response envelope keys are invalid")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("structured response schema version is invalid")
    route = _mapping(value.get("route"))
    if route.get("kind") not in _ROUTE_KINDS:
        raise ValueError("structured response route kind is invalid")
    if not isinstance(_mapping(value.get("execution")).get("source_call_count"), (int, float, type(None))):
        raise ValueError("structured response source call count is invalid")
    if _mapping(value.get("evidence")).get("kind") not in {"none", "knowledge", "web", "mcp"}:
        raise ValueError("structured response evidence kind is invalid")
    _validate_json_safe(value)


def _validate_json_safe(value: Any, *, key_path: tuple[str, ...] = ()) -> None:
    if key_path and any(token in key_path[-1].lower() for token in _FORBIDDEN_KEY_TOKENS):
        raise ValueError("structured response contains a forbidden field")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("structured response key must be a string")
            _validate_json_safe(item, key_path=(*key_path, key))
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_safe(item, key_path=key_path)
        return
    if not _safe_scalar(value):
        raise ValueError("structured response contains a non-JSON-safe value")
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("structured response contains a non-JSON-safe value") from exc
