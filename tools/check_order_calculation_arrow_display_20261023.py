"""DB-free Arrow and edit regression for restored order-calculation tables."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys

import pandas as pd
import pyarrow as pa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.order_calculation_service import get_order_calculation_result
from app.ui import order_calculation_editor as editor
from tools.check_order_calculation_contract import fixture


def test_display_types():
    for values in (["10", 20, None], ["10", 20, "미확인"]):
        source = pd.DataFrame({"제품코드": ["00001", "00002", "00003"], "발주단위": values})
        saved = source.copy(deep=True)
        for editing in (False, True):
            view, config, _ = editor._order_table_display_config(source, editing=editing)
            pa.Table.from_pandas(view, preserve_index=False)
            pa.Table.from_pandas(editor._style_order_table(view, editing=editing).data, preserve_index=False)
            assert config["발주단위"]["alignment"] == "right"
            assert view["발주단위"].iloc[0] not in (None, "")
            assert view["발주단위"].iloc[1] not in (None, "")
        pd.testing.assert_frame_equal(source, saved)


def test_restored_table_and_edit():
    params, sources = fixture()
    with patch("app.services.order_calculation_service.get_current_company_id", return_value=7):
        result = get_order_calculation_result({**params, "query_mode": "전체"}, source_loader=lambda query: sources)
    full = pd.concat([result["df"]] * 3, ignore_index=True)
    full["제품코드"] = ["00001", "00002", "00003"]
    full["발주단위"] = pd.Series(["10", 20, None], dtype="object")
    original = full.copy(deep=True)
    columns = list(result["df_display"].columns)
    state = {
        "sims_export_tables": {"previous": full},
        "sims_tables": {"previous": full.loc[:, columns].copy()},
        "__sims_current_table_source_key": "newer",
    }
    shown = []
    edited = []
    real_column_config = editor.st.column_config
    fake_st = SimpleNamespace(
        session_state=state, column_config=real_column_config,
        dataframe=lambda styled, **kwargs: shown.append((styled.data.copy(), kwargs)),
        data_editor=lambda styled, **kwargs: edited.append((styled.data.copy(), kwargs)),
        error=lambda message: None,
    )
    with patch.object(editor, "st", fake_st), \
         patch("app.db.mssql_client.get_current_company_id", return_value=7), \
         patch("app.ui.chat_middleware._build_sims_context_from_result"):
        assert editor.render_actual_quantity_editor({}, {"table_key": "previous"}, download_uid="previous")
        assert len(shown) == 1 and not edited
        pa.Table.from_pandas(shown[0][0], preserve_index=False)
        pd.testing.assert_frame_equal(state["sims_export_tables"]["previous"], original)

        state["__sims_current_table_source_key"] = "previous"
        assert editor.render_actual_quantity_editor({}, {"table_key": "previous"}, download_uid="previous")
        assert len(edited) == 1
        pa.Table.from_pandas(edited[0][0], preserve_index=False)
        assert edited[0][1]["disabled"] == [column for column in edited[0][0] if column != "실제 발주수량"]
        state[edited[0][1]["key"]] = {"edited_rows": {0: {"실제 발주수량": "55"}}}
        edited[0][1]["on_change"]()
        updated = state["sims_export_tables"]["previous"]
        assert updated.iloc[0]["실제 발주수량"] == 55
        assert updated.iloc[0]["발주단위"] == original.iloc[0]["발주단위"]
        assert updated.iloc[0]["발주금액(부가세포함)"] != original.iloc[0]["발주금액(부가세포함)"]
        assert list(updated.columns) == list(original.columns)


if __name__ == "__main__":
    test_display_types()
    test_restored_table_and_edit()
    print("ORDER_ARROW_DISPLAY_PASS")
