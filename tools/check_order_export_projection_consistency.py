"""Offline regression for equal-row display/full order export selection."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ui import chat_middleware, sims_panel


def main() -> int:
    display = pd.DataFrame({f"표시{i}": [i] for i in range(30)})
    full = display.copy()
    for i in range(49):
        full[f"내부근거{i}"] = i
    key = "order-export-equal-row-fixture"
    state = {
        "__sims_current_table_source_key": key,
        "__sims_last_table_key": key,
        "__sims_export_tables_by_key": {key: full},
        "sims_export_tables": {key: full},
        "__sims_form_id": "fixture",
        "__sims_run_seq": 1,
    }
    captured: dict = {}

    def capture_panel(**kwargs):
        captured["panel"] = kwargs["download_df"].copy()

    fake_st = SimpleNamespace(session_state=state, caption=lambda *args, **kwargs: None)
    with patch.object(sims_panel, "st", fake_st), patch.object(
        sims_panel, "apply_sims_export_projection", side_effect=lambda frame: frame.copy()
    ), patch.object(sims_panel, "_render_panel_result_actions_lazy", side_effect=capture_panel):
        sims_panel._render_downloads(display, "발주 계산", df_full=display, action_name="발주 계산")

    panel_df = captured.get("panel")
    assert isinstance(panel_df, pd.DataFrame)
    assert panel_df.shape == (1, 79), panel_df.shape

    item = {"action": "발주 계산", "table_key": key}
    meta = {
        "action": "발주 계산",
        "table_key": key,
        "download_table_key": key,
        "download_row_count": 1,
        "download_column_count": 79,
    }
    with patch.object(chat_middleware, "st", fake_st):
        resolved = chat_middleware._resolve_payload_full_download_source(
            item,
            meta,
            display_df=display,
        )
    assert resolved.get("source_status") == "complete_display", resolved
    assert resolved.get("df").shape == (1, 79)

    state["sims_export_tables"] = {key: display}
    state["__sims_export_tables_by_key"] = {key: display}
    with patch.object(chat_middleware, "st", fake_st):
        incomplete = chat_middleware._resolve_payload_full_download_source(
            item,
            meta,
            display_df=display,
        )
    assert incomplete.get("df") is None, incomplete
    assert incomplete.get("provenance_status") == "column_projection_incomplete", incomplete

    print("PASS: equal-row richer export=79 columns; display-only=30 columns rejected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
