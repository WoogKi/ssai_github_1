"""One-shot ownership for explicit Panel query submissions (not display caches)."""

from typing import Any, MutableMapping
from uuid import uuid4


def record_panel_submission(state: MutableMapping[str, Any]) -> None:
    selected = state.get("__sims_selected") or {}
    state["__sims_pending_submission"] = {
        "id": uuid4().hex,
        "action": str(selected.get("action") or "").strip(),
    }


def consume_panel_submission(state: MutableMapping[str, Any], action: str) -> str:
    event = state.pop("__sims_pending_submission", None)
    if not isinstance(event, dict) or event.get("action") != str(action).strip():
        return ""
    return str(event.get("id") or "")
