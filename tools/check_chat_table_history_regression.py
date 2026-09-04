# -*- coding: utf-8 -*-
"""Focused in-memory regression for canonical SIMS table history retention."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _table(message_id: str, table_key: str, action: str) -> dict:
    return {
        "id": message_id,
        "type": "table",
        "role": "assistant",
        "action": action,
        "title": action,
        "meta": {"kind": "table", "table_key": table_key, "action": action},
    }


def main() -> int:
    chat = importlib.import_module("app.ui.chat_middleware")
    first = _table("message-stock", "sims_stock_a", "현재고 조회")
    second = _table("message-codes", "sims_codes_b", "코드명 검색")
    room = {"history": [first], "messages": [first]}
    state = {
        "__chat_history": [first],
        "sims_tables": {"sims_stock_a": object(), "sims_codes_b": object()},
        "current_room": room,
        "__sims_current_table_source_key": "sims_codes_b",
        "__sims_current_table_source_action": "코드명 검색",
    }

    with patch.object(chat.st, "session_state", state):
        chat._prune_old_sims_table_history(new_table_key="sims_codes_b", new_item=second)

    failures: list[str] = []
    for channel, items in (
        ("session", state["__chat_history"]),
        ("room.history", room["history"]),
        ("room.messages", room["messages"]),
    ):
        keys = [str((item.get("meta") or {}).get("table_key") or "") for item in items if isinstance(item, dict)]
        if "sims_stock_a" not in keys:
            failures.append(f"{channel} lost sims_stock_a: {keys!r}")

    if "sims_stock_a" not in state.get("sims_tables", {}):
        failures.append("display payload for sims_stock_a was pruned")
    if state.get("__sims_current_table_source_key") != "sims_codes_b":
        failures.append("current-table pointer changed")

    if failures:
        print("RESULT: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("RESULT: OK")
    print("history retains prior assistant table; current-table remains latest only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
