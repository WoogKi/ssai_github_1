"""Pure route-decision records for already-resolved SIMS tool routes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping


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
_DECISION_MODES = frozenset({"deterministic", "llm_summary", "llm_stream", "none"})
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
        "sql",
        "bind",
        "session",
    }
)


def _safe_text(value: Any, *, limit: int = 160) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text if text and len(text) <= limit else ""


def _safe_arguments(values: Mapping[str, Any] | None) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for raw_key, value in (values or {}).items():
        key = _safe_text(raw_key)
        if not key or any(token in key.lower() for token in _FORBIDDEN_KEY_TOKENS):
            continue
        if value is None or isinstance(value, (str, int, float, bool)):
            safe[key] = value
    return safe


@dataclass(frozen=True)
class ToolRouteDecision:
    """A JSON-safe record of a route selected by existing application logic."""

    kind: str
    decision_mode: str
    action: str
    canonical_action: str
    safe_arguments: Mapping[str, Any]
    reason_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = {
            "kind": self.kind,
            "decision_mode": self.decision_mode,
            "action": self.action,
            "canonical_action": self.canonical_action,
            "safe_arguments": dict(self.safe_arguments),
            "reason_code": self.reason_code,
        }
        validate_tool_route_decision(value)
        return value


def build_datetime_tool_route_decision(answer: Any) -> ToolRouteDecision:
    """Record an existing DateTimeToolAnswer after the resolver has succeeded."""
    intent = _safe_text(getattr(answer, "intent", ""))
    timezone_name = _safe_text(getattr(answer, "timezone_name", ""))
    if not intent or not timezone_name:
        raise ValueError("datetime tool route decision requires a resolved answer")
    decision = ToolRouteDecision(
        kind="datetime_tool",
        decision_mode="deterministic",
        action=intent,
        canonical_action=intent,
        safe_arguments={"timezone_name": timezone_name},
        reason_code="resolved",
    )
    decision.to_dict()
    return decision


def build_knowledge_rag_tool_route_decision(
    *,
    technical_detail_mode: Any,
    reason_code: Any,
) -> ToolRouteDecision:
    """Record a completed Knowledge route without evaluating authorization."""
    if not isinstance(technical_detail_mode, bool):
        raise ValueError("Knowledge tool route decision requires a resolved technical mode")
    reason = _safe_text(reason_code)
    if not reason:
        raise ValueError("Knowledge tool route decision requires a completed reason code")
    decision = ToolRouteDecision(
        kind="knowledge_rag",
        decision_mode="deterministic",
        action="knowledge_chat",
        canonical_action="knowledge_chat",
        safe_arguments={"technical_detail_mode": technical_detail_mode},
        reason_code=reason,
    )
    decision.to_dict()
    return decision


def build_web_latest_tool_route_decision(response: Any) -> ToolRouteDecision:
    """Record an existing WebSearchResponse after the search has completed."""
    status = _safe_text(getattr(response, "status", ""))
    period = getattr(response, "period", None)
    period_kind = _safe_text(getattr(period, "kind", ""))
    reason = _safe_text(getattr(response, "reason_code", "")) or status
    if not status or not period_kind or not reason:
        raise ValueError("Web tool route decision requires a completed search response")
    decision = ToolRouteDecision(
        kind="web_latest",
        decision_mode="deterministic",
        action="web_search",
        canonical_action="web_search",
        safe_arguments={"period_kind": period_kind},
        reason_code=reason,
    )
    decision.to_dict()
    return decision


def build_mcp_external_resource_tool_route_decision(response: Any) -> ToolRouteDecision:
    """Record a completed MCP resource request; never execute it."""
    status = _safe_text(getattr(response, "status", ""))
    resource_id = _safe_text(getattr(response, "resource_id", ""))
    reason = _safe_text(getattr(response, "reason_code", "")) or status
    if not status or not resource_id or not reason:
        raise ValueError("MCP tool route decision requires a completed response")
    decision = ToolRouteDecision(
        kind="mcp_external_resource",
        decision_mode="deterministic",
        action="mcp_resource_read",
        canonical_action="mcp_resource_read",
        safe_arguments={"resource_id": resource_id},
        reason_code=reason,
    )
    decision.to_dict()
    return decision


def _internal_handler_kind(canonical_action: str) -> tuple[str, str]:
    """Return existing action-inventory ownership without importing runtime handlers."""
    try:
        from app.sims.nlq.action_inventory import implemented_actions

        spec = next(
            (item for item in implemented_actions() if item.canonical_action == canonical_action),
            None,
        )
    except Exception:
        return "", ""
    if spec is None:
        return "", ""
    return _safe_text(spec.handler_kind), _safe_text(spec.handler_target)


def _internal_route_kind(
    *,
    canonical_action: str,
    meta: Mapping[str, Any],
) -> str:
    """Map completed handler ownership to a public route kind; never parse input."""
    if bool(meta.get("current_table_followup")):
        return "current_table"

    handler_kind, handler_target = _internal_handler_kind(canonical_action)
    if handler_kind in {"io_service", "io_alias"}:
        return "io"
    if handler_kind == "analytics" or bool(meta.get("analysis_nlq") or meta.get("analytics")):
        return "analytics"
    if handler_target == "app.sims.nlq.nlq_vendors.try_handle_vendors_nlq":
        return "vendor_master"
    if bool(meta.get("nlq") or meta.get("master_nlq") or meta.get("nlq_query")):
        return "sims_nlq"
    return ""


def build_sims_internal_tool_route_decision(
    payload: Mapping[str, Any] | None,
) -> ToolRouteDecision | None:
    """Record a completed internal SIMS route from final action/meta ownership only.

    Returning ``None`` means the final payload has no explicit SIMS ownership
    metadata.  Callers then preserve the existing Phase 1 envelope behavior.
    """
    item = payload if isinstance(payload, Mapping) else {}
    meta_value = item.get("meta")
    meta = meta_value if isinstance(meta_value, Mapping) else {}
    action = _safe_text(item.get("action") or meta.get("action"))
    canonical_action = _safe_text(meta.get("canonical_action")) or action
    if not canonical_action:
        return None

    kind = _internal_route_kind(canonical_action=canonical_action, meta=meta)
    if not kind:
        return None

    reason = (
        _safe_text(meta.get("reason_code"))
        or _safe_text(meta.get("result_status"))
        or _safe_text(item.get("result_status"))
        or _safe_text(meta.get("execution_status"))
    )
    if not reason:
        return None

    params_value = item.get("params")
    params = params_value if isinstance(params_value, Mapping) else {}
    safe_arguments: dict[str, Any] = {
        "has_conditions": bool(params),
        "condition_key_count": len(_safe_arguments(params)),
    }
    if kind == "current_table":
        safe_arguments["is_current_table_followup"] = True
    for key in ("product_scope_count", "supplier_scope_count", "stock_scope_count"):
        value = meta.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            safe_arguments[key] = value

    decision = ToolRouteDecision(
        kind=kind,
        decision_mode="deterministic",
        action=action or canonical_action,
        canonical_action=canonical_action,
        safe_arguments=safe_arguments,
        reason_code=reason,
    )
    decision.to_dict()
    return decision


def validate_tool_route_decision(value: Mapping[str, Any] | ToolRouteDecision | None) -> None:
    """Validate only the derived decision; it never evaluates or changes routing."""
    raw = value.to_dict() if isinstance(value, ToolRouteDecision) else value
    if not isinstance(raw, Mapping):
        raise ValueError("tool route decision must be a mapping")
    required = {"kind", "decision_mode", "action", "canonical_action", "safe_arguments", "reason_code"}
    if set(raw) != required:
        raise ValueError("tool route decision keys are invalid")
    if raw.get("kind") not in _ROUTE_KINDS:
        raise ValueError("tool route decision kind is invalid")
    if raw.get("decision_mode") not in _DECISION_MODES:
        raise ValueError("tool route decision mode is invalid")
    for field in ("action", "canonical_action", "reason_code"):
        text = raw.get(field)
        if not isinstance(text, str) or (field != "reason_code" and not _safe_text(text)):
            raise ValueError("tool route decision text is invalid")
        if any(token in text.lower() for token in _FORBIDDEN_KEY_TOKENS):
            raise ValueError("tool route decision contains a forbidden field")
    if not isinstance(raw.get("safe_arguments"), Mapping):
        raise ValueError("tool route decision arguments are invalid")
    for key, item in raw["safe_arguments"].items():
        if not isinstance(key, str) or any(token in key.lower() for token in _FORBIDDEN_KEY_TOKENS):
            raise ValueError("tool route decision contains a forbidden field")
        if item is not None and not isinstance(item, (str, int, float, bool)):
            raise ValueError("tool route decision contains a non-JSON-safe value")
    try:
        json.dumps(raw, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("tool route decision is not JSON-safe") from exc
