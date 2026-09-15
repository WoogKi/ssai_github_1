"""Offline gate for explicit SIMS Panel form submission and result promotion."""

from __future__ import annotations

import ast
from pathlib import Path
import sys

from streamlit.testing.v1 import AppTest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


EXPECTED_EXECUTION_SUBMITS = {
    "app/sims/views/users.py": 3,
    "app/sims/views/codes.py": 4,
    "app/sims/views/vendors.py": 2,
    "app/sims/views/goods.py": 2,
    "app/sims/views/road_address.py": 1,
    "app/sims/views/analytics_views.py": 7,
}


def _fixture_app() -> None:
    import pandas as pd
    import streamlit as st

    from app.sims.views.rddbc_io_shared import _trigger_panel_run
    from app.ui import sims_panel as panel

    def fixture_view():
        with st.form("fixture-users"):
            submitted = st.form_submit_button(
                "조회",
                type="primary",
                on_click=_trigger_panel_run,
            )
        if not submitted:
            return {
                "final": False,
                "type": "text",
                "title": "사용자목록 + 부서명",
            }

        frame = pd.DataFrame(
            {
                "사용자코드": [f"U{index:03d}" for index in range(319)],
                "부서명": ["영업부"] * 319,
            }
        )
        return {
            "final": True,
            "type": "table",
            "title": "사용자목록 + 부서명",
            "action": "사용자목록 + 부서명",
            "df": frame,
            "df_display": frame,
            "meta": {
                "row_count": 319,
                "row_count_total": 319,
                "display_row_count": 319,
            },
        }

    panel._CATEGORIES = {
        "사용자": {
            "actions": {
                "사용자목록 + 부서명": fixture_view,
            }
        }
    }
    panel.render_sims_context_controls = lambda: None
    panel.render_sims_main(
        {
            "category": "사용자",
            "action": "사용자목록 + 부서명",
        }
    )


def _assert_execution_submit_callbacks() -> int:
    checked = 0
    for relative_path, expected_count in EXPECTED_EXECUTION_SUBMITS.items():
        source_path = ROOT / relative_path
        tree = ast.parse(source_path.read_text(encoding="utf-8-sig"), filename=str(source_path))
        execution_submits = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not isinstance(function, ast.Attribute) or function.attr != "form_submit_button":
                continue
            label = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else ""
            if label not in {"조회", "집계"}:
                continue
            execution_submits.append(node)

        assert len(execution_submits) == expected_count, (
            relative_path,
            len(execution_submits),
            expected_count,
        )
        for node in execution_submits:
            callback = next((item.value for item in node.keywords if item.arg == "on_click"), None)
            assert isinstance(callback, ast.Name) and callback.id == "_trigger_panel_run", (
                relative_path,
                node.lineno,
            )
            checked += 1
    return checked


def main() -> int:
    callback_count = _assert_execution_submit_callbacks()

    app = AppTest.from_function(_fixture_app)
    app.run(timeout=20)
    assert len(app.button) == 1
    app.button[0].click().run(timeout=20)

    state = app.session_state
    assert state["__sims_was_final"] is True
    stored = state["__sims_last_final_payload_for_chat"]
    assert len(stored["df"]) == 319
    assert stored["meta"]["_panel_submission_id"]
    assert stored["meta"]["_panel_source_sig"] == stored["meta"]["_panel_submission_id"]
    assert state["__sims_panel_source_promoted_sig"] == stored["meta"]["_panel_source_sig"]

    submission_id = stored["meta"]["_panel_submission_id"]
    app.run(timeout=20)
    assert app.session_state["__sims_was_final"] is False
    remembered = app.session_state["__sims_last_final_payload_for_chat"]
    assert remembered["meta"]["_panel_submission_id"] == submission_id

    print(
        "PASS SIMS panel submission contract: "
        f"callbacks={callback_count} rows=319 was_final=True chat_payload=True "
        "current_source=True cached_rerun_duplicate=False"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
