#!/usr/bin/env python3
"""Offline contract checks for append-only NLQ feedback logging."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.nlq_feedback_log_service import (
    append_nlq_feedback_event,
    latest_feedback_events,
    resolve_nlq_feedback_log_path,
)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="nlq_feedback_") as temp_dir:
        root = Path(temp_dir)
        case_path = root / "nlq_cases.jsonl"
        environ = {"SIMS_NLQ_CASE_LOG_FILE": str(case_path)}
        feedback_path = resolve_nlq_feedback_log_path(environ=environ)
        _assert(feedback_path == root / "nlq_feedback.jsonl", "feedback path must be sibling of case log")

        common = {
            "request_id": "request-1",
            "assistant_message_id": "message-1",
            "runtime_context": {"company_id": 4, "user_id": "user-1", "room_id": "room-1"},
            "environ": environ,
        }
        first = append_nlq_feedback_event(
            **common,
            feedback="like",
            occurred_at="2026-08-26T10:00:00.000+09:00",
        )
        second = append_nlq_feedback_event(
            **common,
            feedback="dislike",
            reason="result_wrong",
            note="fixture note",
            occurred_at="2026-08-26T10:01:00.000+09:00",
        )
        records = [json.loads(line) for line in feedback_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        _assert(len(records) == 2, "feedback changes must append rather than overwrite")
        _assert(first["feedback_id"] != second["feedback_id"], "feedback event ids must be unique")
        _assert(all("df" not in record and "result" not in record for record in records), "feedback must not store result data")
        _assert(records[1]["company_id"] == "4" and records[1]["user_id"] == "user-1", "runtime scope missing")
        latest = latest_feedback_events(records)
        _assert(latest[("request-1", "message-1")]["feedback"] == "dislike", "latest event selection failed")

        for field, value in (("feedback", "neutral"), ("reason", "invalid")):
            try:
                append_nlq_feedback_event(**common, feedback="like" if field == "reason" else value, reason=value if field == "reason" else "")
            except ValueError:
                continue
            raise AssertionError(f"invalid {field} was accepted")

    print("RESULT: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
