"""Offline contracts for chat composer dispatch and normal-render log levels."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
MIDDLEWARE = (ROOT / "app" / "ui" / "chat_middleware.py").read_text(encoding="utf-8")
PANEL = (ROOT / "app" / "ui" / "sims_panel.py").read_text(encoding="utf-8")


def require(source: str, token: str, label: str) -> None:
    if token not in source:
        raise AssertionError(label)


def main() -> None:
    require(MAIN, "def _queue_chat_composer_submission", "composer callback is required")
    require(MAIN, "on_submit=_queue_chat_composer_submission", "chat_input must use the callback")
    require(MAIN, 'st.session_state["__sims_auto_user_input"] = composer_text', "text must reach the existing input dispatcher")
    require(MAIN, "queue_attachment_submission_event(", "file/audio submission must be queued as an event")
    require(MAIN, "select_composer_submission(", "pending file/audio event must be consumed once")
    composer_block = MAIN[MAIN.index("composer_submission = st.chat_input("):MAIN.index("if recorded_audio is not None:")]
    if "elif composer_text:\n        # chat_input returns" in composer_block:
        raise AssertionError("text composer must not force a second rerun")
    require(MAIN, '"[ui.rerun.summary]', "one INFO rerun summary is required")
    for token in (
        "[chat.render.dedupe]",
        "[chat.history.table_skip]",
        "[old_table.render]",
        "[old_table.prune]",
        "[chat.nlq.table]",
    ):
        index = MIDDLEWARE.index(token)
        if "log.debug(" not in MIDDLEWARE[max(0, index - 100):index]:
            raise AssertionError(f"{token} must be DEBUG")
    require(MAIN, 'log.debug(\n        "[chat.room.selector]', "room selector detail must be DEBUG")
    require(MAIN, 'log.debug(\n                "[ui.event_to_rerun]', "event detail must be DEBUG")
    require(MAIN, 'log.debug(\n                "[ui.script_path.perf]', "script path detail must be DEBUG")
    require(PANEL, 'log.debug(\n                    "[dashboard.event_link]', "dashboard event-link detail must be DEBUG")
    print("PASS: chat composer callback and normal-render logging contract")


if __name__ == "__main__":
    main()
