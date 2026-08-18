"""Synthetic regression for fail-closed company ownership of chat rooms."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.chat_company_scope_policy import (  # noqa: E402
    room_company_id,
    room_matches_company,
    rooms_for_company,
)


def _room(room_id: str, company_id) -> dict:
    return {"id": room_id, "company_id": company_id, "messages": []}


def main() -> None:
    company4 = _room("room-4", 4)
    company6 = _room("room-6", "6")
    legacy = {"id": "legacy-no-company", "messages": [{"company_id": 4}]}
    malformed = _room("malformed", "company-4")
    rooms = [company4, company6, legacy, malformed]

    assert [room["id"] for room in rooms_for_company(rooms, 4)] == ["room-4"]
    assert [room["id"] for room in rooms_for_company(rooms, 6)] == ["room-6"]
    assert room_matches_company(company4, 6) is False
    assert room_matches_company(company6, 6) is True
    assert room_matches_company(legacy, 4) is False
    assert room_company_id(legacy) == ""
    assert room_company_id(malformed) == ""
    assert room_matches_company(company4, 0) is False
    assert room_matches_company(company4, " ") is False

    main_source = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    assert "def _ensure_current_company_chat_scope()" in main_source
    assert "def _clear_company_scoped_chat_runtime" in main_source
    assert "def _build_current_room_compact_context" in main_source
    assert "reason=cross_company_or_unscoped" in main_source
    assert "mode=deterministic reason=company_change llm_call_count=0" in main_source
    assert "if key not in {\"company_id\", \"company_name\", \"db_name\"}" in main_source
    print("RESULT OK tests=15")


if __name__ == "__main__":
    main()
