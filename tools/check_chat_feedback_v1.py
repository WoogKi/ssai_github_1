from __future__ import annotations

import copy
import json
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.chat_feedback_service import (  # noqa: E402
    CHAT_FEEDBACK_REVIEW,
    DISLIKE_REASON_CODES,
    ChatFeedbackAccessError,
    ChatFeedbackError,
    aggregate_chat_feedback,
    bounded_plain_text,
    bounded_visible_answer_summary,
    list_chat_feedback_review_events,
    read_chat_feedback,
    update_chat_feedback,
)
from app.ui.chat_message_controls import (  # noqa: E402
    bounded_copy_text,
    claim_chat_message_controls,
    is_feedback_eligible_message,
    reset_chat_message_control_registry,
)


def _message(*, message_type: str = "chat", **extra) -> dict:
    meta = dict(extra.pop("meta", {}) or {})
    meta.setdefault("message_type", message_type)
    return {
        "id": str(uuid.uuid4()),
        "role": "assistant",
        "content": "안전한 답변 SECRET-DO-NOT-STORE",
        "time": "2026-09-03 10:00:00",
        "user_id": 7,
        "company_id": 4,
        "meta": meta,
        **extra,
    }


def _room(messages: list[dict]) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "user_id": 7,
        "company_id": 4,
        "messages": messages,
        "history": [],
    }


def _expect_error(error_type, fn, reason: str) -> None:
    try:
        fn()
    except error_type:
        return
    raise AssertionError(reason)


def main() -> None:
    now = datetime(2026, 9, 3, 3, 0, tzinfo=timezone.utc)
    message = _message(
        meta={"message_type": "chat", "request_id": "req-1", "result_status": "success"},
    )
    question = {
        "id": str(uuid.uuid4()),
        "role": "user",
        "content": "오늘 재고 현황을 알려줘",
        "time": "2026-09-03 09:59:00",
    }
    room = _room([question, message])
    room_before = copy.deepcopy(room)

    with tempfile.TemporaryDirectory(prefix="chat-feedback-v1-") as temp_dir:
        root = Path(temp_dir)

        # Read paths are genuinely read-only and do not create an empty store.
        assert read_chat_feedback(
            room=room,
            message=message,
            user_id=7,
            company_id=4,
            storage_root=root,
        ) is None
        assert list(root.rglob("*")) == []

        created = update_chat_feedback(
            room=room,
            message=message,
            user_id=7,
            company_id=4,
            feedback="like",
            now=now,
            storage_root=root,
        )
        assert created["status"] == "created" and created["write_count"] == 1
        assert created["record"]["revision"] == 1
        assert created["record"]["record_kind"] == "review_signal"
        assert created["record"]["message_id"] == message["id"]
        assert created["record"]["request_id"] == "req-1"
        assert created["record"]["retention_days"] == 180

        unchanged = update_chat_feedback(
            room=room,
            message=message,
            user_id=7,
            company_id=4,
            feedback="like",
            now=now + timedelta(minutes=1),
            storage_root=root,
        )
        assert unchanged["status"] == "unchanged" and unchanged["write_count"] == 0
        assert unchanged["record"]["revision"] == 1

        changed = update_chat_feedback(
            room=room,
            message=message,
            user_id=7,
            company_id=4,
            feedback="dislike",
            now=now + timedelta(minutes=2),
            storage_root=root,
        )
        assert changed["status"] == "changed" and changed["record"]["revision"] == 2
        assert changed["record"]["reason_code"] is None
        assert changed["review_log_write_count"] == 1
        review_events = list_chat_feedback_review_events(
            reviewer_user_type="SSART_ADMIN",
            reviewer_user_grade="SUPER",
            storage_root=root,
            now=now,
        )
        assert len(review_events) == 1
        first_event = review_events[0]
        assert first_event["event_kind"] == "dislike_created"
        assert first_event["question_text"] == "오늘 재고 현황을 알려줘"
        assert first_event["feedback"] == "dislike" and first_event["reason_code"] is None
        assert first_event["previous_feedback"] == "like"
        assert first_event["review_state"] == "unreviewed"
        assert first_event["promoted_to_regression"] is False
        assert "SECRET-DO-NOT-STORE" not in json.dumps(first_event, ensure_ascii=False)

        same_dislike = update_chat_feedback(
            room=room,
            message=message,
            user_id=7,
            company_id=4,
            feedback="dislike",
            now=now + timedelta(minutes=2, seconds=30),
            storage_root=root,
        )
        assert same_dislike["status"] == "unchanged" and same_dislike["write_count"] == 0
        assert len(list_chat_feedback_review_events(
            reviewer_user_type="SSART_ADMIN",
            reviewer_user_grade="SUPER",
            storage_root=root,
            now=now,
        )) == 1

        reason_added = update_chat_feedback(
            room=room,
            message=message,
            user_id=7,
            company_id=4,
            feedback="dislike",
            reason_code="too_slow",
            now=now + timedelta(minutes=3),
            storage_root=root,
        )
        assert reason_added["status"] == "changed" and reason_added["record"]["revision"] == 3
        assert reason_added["record"]["reason_code"] == "too_slow"
        assert reason_added["review_log_write_count"] == 1

        cleared = update_chat_feedback(
            room=room,
            message=message,
            user_id=7,
            company_id=4,
            feedback=None,
            now=now + timedelta(minutes=4),
            storage_root=root,
        )
        assert cleared["status"] == "cleared" and cleared["record"]["revision"] == 4
        assert cleared["record"]["state"] == "cleared" and cleared["record"]["feedback"] is None
        assert cleared["review_log_write_count"] == 1
        assert room == room_before

        raw_record = Path(cleared["path"]).read_text(encoding="utf-8")
        assert "SECRET-DO-NOT-STORE" not in raw_record
        assert "안전한 답변" not in raw_record
        assert "content_hash" in raw_record
        review_events = list_chat_feedback_review_events(
            reviewer_user_type="SSART_ADMIN",
            reviewer_user_grade="SUPER",
            storage_root=root,
            now=now,
        )
        assert [event["event_kind"] for event in reversed(review_events)] == [
            "dislike_created", "dislike_reason_changed", "dislike_cleared",
        ]
        _expect_error(
            ChatFeedbackAccessError,
            lambda: list_chat_feedback_review_events(
                reviewer_user_type="SSART_USER",
                reviewer_user_grade="STAFF",
                storage_root=root,
                now=now,
            ),
            "review queue must remain admin-only",
        )

        _expect_error(
            ChatFeedbackError,
            lambda: update_chat_feedback(
                room=room,
                message=message,
                user_id=7,
                company_id=4,
                feedback="like",
                reason_code="too_slow",
                storage_root=root,
            ),
            "like reason must remain null",
        )
        _expect_error(
            ChatFeedbackError,
            lambda: update_chat_feedback(
                room=room,
                message=message,
                user_id=7,
                company_id=4,
                feedback=None,
                reason_code="too_slow",
                storage_root=root,
            ),
            "clear reason must remain null",
        )
        for reason in DISLIKE_REASON_CODES:
            assert reason and " " not in reason

        for bad_scope in (
            {"user_id": 8, "company_id": 4},
            {"user_id": 7, "company_id": 5},
        ):
            _expect_error(
                ChatFeedbackAccessError,
                lambda bad_scope=bad_scope: read_chat_feedback(
                    room=room,
                    message=message,
                    storage_root=root,
                    **bad_scope,
                ),
                "cross-scope read must fail closed",
            )

        synthetic = _message()
        _expect_error(
            ChatFeedbackAccessError,
            lambda: update_chat_feedback(
                room=room,
                message=synthetic,
                user_id=7,
                company_id=4,
                feedback="like",
                storage_root=root,
            ),
            "non-canonical assistant UUID must be rejected",
        )

        routes = {
            "generic_llm": _message(message_type="chat"),
            "knowledge_rag": _message(message_type="knowledge_answer"),
            "web_latest": _message(message_type="web_search"),
            "mcp_external_resource": _message(message_type="mcp_external_resource"),
            "sims_nlq": _message(message_type="table", action="입고명세 조회"),
            "dashboard": _message(message_type="dashboard_lite", action="Dashboard Lite v0.1"),
        }
        room["messages"].extend(routes.values())
        state: dict = {}
        reset_chat_message_control_registry(state)
        for route_name, route_message in routes.items():
            assert claim_chat_message_controls(state, route_message), route_name
            assert not claim_chat_message_controls(state, route_message), route_name
        user_message = {**_message(), "role": "user"}
        system_message = _message(message_type="system_notice")
        assert not is_feedback_eligible_message(user_message)
        assert not is_feedback_eligible_message(system_message)

        active_like = routes["knowledge_rag"]
        active_dislike = routes["sims_nlq"]
        active_dislike["data"] = "RAW-TABLE-SECRET-MUST-NOT-STORE"
        active_dislike["meta"].update({
            "row_count_total": 12,
            "query_summary": "회사 4 / 오늘",
            "summary_md": "회사 4 / 오늘",
            "route": "io",
            "nlq_trace_request_id": "trace-active-dislike",
            "result_status": "no_data",
        })
        room["messages"].insert(room["messages"].index(active_dislike), {
            "id": str(uuid.uuid4()),
            "role": "user",
            "content": "오늘 입고명세를 확인해줘",
        })
        update_chat_feedback(
            room=room,
            message=active_like,
            user_id=7,
            company_id=4,
            feedback="like",
            now=now,
            storage_root=root,
        )
        active_dislike_write = update_chat_feedback(
            room=room,
            message=active_dislike,
            user_id=7,
            company_id=4,
            feedback="dislike",
            reason_code="inconvenient_table",
            now=now,
            storage_root=root,
        )
        assert active_dislike_write["record"]["route"] == "sims_nlq"
        assert active_dislike_write["record"]["route_kind"] == "sims_nlq"
        assert active_dislike_write["record"]["request_id"] == "trace-active-dislike"
        assert active_dislike_write["record"]["result_status"] == "no_data"
        structured_events = list_chat_feedback_review_events(
            reviewer_user_type="SSART_ADMIN",
            reviewer_user_grade="SUPER",
            storage_root=root,
            now=now,
        )
        structured_event = next(event for event in structured_events if event["message_id"] == active_dislike["id"])
        assert structured_event["question_text"] == "오늘 입고명세를 확인해줘"
        assert "입고명세 조회" in structured_event["answer_summary"]
        assert "결과 건수: 12건" in structured_event["answer_summary"]
        assert structured_event["answer_summary"].count("회사 4 / 오늘") == 1
        assert structured_event["route_kind"] == "sims_nlq"
        assert structured_event["request_id"] == "trace-active-dislike"
        assert structured_event["result_status"] == "no_data"
        assert "RAW-TABLE-SECRET-MUST-NOT-STORE" not in json.dumps(structured_event, ensure_ascii=False)
        assert bounded_visible_answer_summary(active_dislike) == bounded_copy_text(active_dislike)
        rows = aggregate_chat_feedback(
            reviewer_user_type="SSART_ADMIN",
            reviewer_user_grade="SUPER",
            storage_root=root,
            now=now,
        )
        assert sum(row["feedback_count"] for row in rows) == 2
        assert sum(row["like_count"] for row in rows) == 1
        assert sum(row["dislike_count"] for row in rows) == 1
        assert any(row["route"] == "knowledge_rag" for row in rows)
        assert any(row["route"] == "sims_nlq" for row in rows)
        assert all("user_id" not in row and "message_id" not in row for row in rows)
        assert aggregate_chat_feedback(
            reviewer_user_type="SSART_USER",
            reviewer_user_grade="STAFF",
            permission_codes=[CHAT_FEEDBACK_REVIEW],
            storage_root=root,
            now=now,
        ) == rows
        _expect_error(
            ChatFeedbackAccessError,
            lambda: aggregate_chat_feedback(
                reviewer_user_type="SSART_USER",
                reviewer_user_grade="STAFF",
                storage_root=root,
                now=now,
            ),
            "non-reviewer aggregate must fail closed",
        )

        expired = routes["web_latest"]
        update_chat_feedback(
            room=room,
            message=expired,
            user_id=7,
            company_id=4,
            feedback="dislike",
            now=now - timedelta(days=181),
            storage_root=root,
        )
        rows_after_expired = aggregate_chat_feedback(
            reviewer_user_type="SSART_ADMIN",
            reviewer_user_grade="SUPER",
            storage_root=root,
            now=now,
        )
        assert sum(row["feedback_count"] for row in rows_after_expired) == 2
        assert all(
            event["message_id"] != expired["id"]
            for event in list_chat_feedback_review_events(
                reviewer_user_type="SSART_ADMIN",
                reviewer_user_grade="SUPER",
                storage_root=root,
                now=now,
            )
        )

        long_message = {**message, "content": "<script>secret()</script>" + ("가" * 20_000)}
        copied = bounded_plain_text(long_message)
        assert len(copied) == 12_000 and "script" not in copied and "secret()" not in copied

        nlq_copy = bounded_copy_text(
            {
                "id": str(uuid.uuid4()),
                "role": "assistant",
                "type": "table",
                "title": "거래처 목록",
                "content": "거래처 목록",
                "action": "거래처 목록",
                "data": "RAW-DATAFRAME-MUST-NOT-COPY",
                "meta": {
                    "row_count_total": 12,
                    "query_summary": "회사 4 / 기준일 2026-09-03",
                    "summary_md": "거래처 목록을 조회했습니다.",
                },
            }
        )
        assert "거래처 목록" in nlq_copy and "결과 건수: 12건" in nlq_copy
        assert "조회조건:" in nlq_copy and "RAW-DATAFRAME-MUST-NOT-COPY" not in nlq_copy
        assert "안전한 답변" in bounded_copy_text(message)
        assert "SECRET-DO-NOT-STORE" not in bounded_copy_text(message)

    main_source = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    middleware_source = (ROOT / "app" / "ui" / "chat_middleware.py").read_text(encoding="utf-8")
    assert "reset_chat_message_control_registry(st.session_state)" in main_source
    assert "_render_assistant_message_controls(m, room=current_room)" in main_source
    assert "render_chat_message_controls(" in middleware_source
    controls_source = (ROOT / "app" / "ui" / "chat_message_controls.py").read_text(encoding="utf-8")
    assert "@st.fragment" in controls_source
    assert "st.rerun()" not in controls_source
    assert "사유 선택" in controls_source
    assert "with st.popover" not in controls_source
    assert "options=reason_options" in controls_source
    assert "material-symbols-rounded" not in controls_source
    assert "<svg" not in controls_source
    assert "position:absolute" in controls_source
    assert 'type="secondary"' in controls_source
    assert "div.st-key-__chat_feedback_like_" in controls_source
    assert "background:#e8f1ff" in controls_source
    assert "color:#3b82f6" in controls_source
    assert "background:#fdebec" in controls_source
    assert "color:#ef4444" in controls_source
    assert "border:0 !important" in controls_source
    assert "font-size:20px !important" in controls_source
    assert "width=116" in controls_source
    assert "min-height:28px !important;height:28px !important" in controls_source
    assert "✓ 저장됨" in controls_source
    assert '"check"' not in controls_source
    assert "[0.42, 0.42, 0.38, 1.52, 0.72, 20]" in controls_source
    assert '"nlq_trace_request_id"' in main_source
    assert '"result_status"' in main_source
    assert '"feedback"' not in main_source[main_source.index("_CHAT_PARTITION_MESSAGE_ALLOW_KEYS"):][:1200]
    assert "feedback_id" not in json.dumps(room, ensure_ascii=False)

    print("PASS chat feedback V1")
    print("routes=6 controls_once=PASS")
    print("idempotency_revision_tombstone=PASS")
    print("scope_isolation_sensitive_persistence=PASS")
    print("aggregate_retention_permission=PASS")
    print("dislike_review_queue=PASS")


if __name__ == "__main__":
    main()
