from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.chat_composer_submission import (  # noqa: E402
    PENDING_ATTACHMENT_SUBMISSION_KEY,
    queue_attachment_submission_event,
    select_composer_submission,
)


@dataclass
class Submission:
    text: str = ""
    files: list[str] = field(default_factory=list)
    audio: object | None = None


def _assert_one_shot_binary_submission() -> None:
    state: dict[str, object] = {}
    context = {"room_id": "room-a", "company_id": "3", "user_id": "7"}
    submission = Submission(text="분석해줘", files=["large.xlsx"])
    queue_attachment_submission_event(
        state,
        submission=submission,
        event_id="event-1",
        context=context,
    )

    selected, event_id, status = select_composer_submission(
        state,
        widget_submission=submission,
        context=context,
    )
    assert selected is submission
    assert event_id == "event-1"
    assert status == "ready"
    assert PENDING_ATTACHMENT_SUBMISSION_KEY not in state

    # The widget can retain the old upload across the post-analysis rerun.
    selected, _, status = select_composer_submission(
        state,
        widget_submission=submission,
        context=context,
    )
    assert selected is None
    assert status == "missing_binary_ignored"


def _assert_explicit_reupload_is_new_event() -> None:
    state: dict[str, object] = {}
    context = {"room_id": "room-a", "company_id": "3", "user_id": "7"}
    same_file = Submission(files=["large.xlsx"])
    consumed_ids: list[str] = []
    for event_id in ("event-1", "event-2"):
        queue_attachment_submission_event(
            state,
            submission=same_file,
            event_id=event_id,
            context=context,
        )
        selected, selected_id, status = select_composer_submission(
            state,
            widget_submission=same_file,
            context=context,
        )
        assert selected is same_file and status == "ready"
        consumed_ids.append(selected_id)
    assert consumed_ids == ["event-1", "event-2"]


def _assert_batch_audio_text_and_context_contracts() -> None:
    context = {"room_id": "room-a", "company_id": "3", "user_id": "7"}
    state: dict[str, object] = {}
    batch = Submission(files=["a.xlsx", "b.pdf"])
    queue_attachment_submission_event(
        state,
        submission=batch,
        event_id="batch-1",
        context=context,
    )
    selected, _, status = select_composer_submission(
        state,
        widget_submission=batch,
        context=context,
    )
    assert selected is batch and status == "ready" and len(selected.files) == 2

    audio = Submission(audio=object())
    queue_attachment_submission_event(
        state,
        submission=audio,
        event_id="audio-1",
        context=context,
    )
    selected, _, status = select_composer_submission(
        state,
        widget_submission=audio,
        context=context,
    )
    assert selected is audio and status == "ready"

    selected, _, status = select_composer_submission(
        state,
        widget_submission="일반 질문",
        context=context,
    )
    assert selected == "일반 질문" and status == "missing"

    queue_attachment_submission_event(
        state,
        submission=batch,
        event_id="stale-1",
        context=context,
    )
    selected, _, status = select_composer_submission(
        state,
        widget_submission=batch,
        context={**context, "company_id": "4"},
    )
    assert selected is None and status == "stale_context_binary_ignored"
    assert PENDING_ATTACHMENT_SUBMISSION_KEY not in state


def main() -> int:
    _assert_one_shot_binary_submission()
    _assert_explicit_reupload_is_new_event()
    _assert_batch_audio_text_and_context_contracts()
    print("attachment single-submission gate: PASS (4/4)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
