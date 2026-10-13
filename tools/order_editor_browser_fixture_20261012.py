"""DB-free browser fixture for the 284-row order editor boundary."""

from unittest.mock import patch

import pandas as pd
import streamlit as st

from app.db.mssql_client import set_current_company_id
from app.services.order_calculation_service import get_order_calculation_result
from app.ui.order_calculation_editor import render_actual_quantity_editor
from tools.check_order_calculation_contract import fixture


set_current_company_id(7)
st.session_state["ssai_selected_company"] = {"company_id": 7}
params, sources = fixture()
with patch("app.services.order_calculation_service.get_current_company_id", return_value=7):
    result = get_order_calculation_result(params, source_loader=lambda _params: sources)

count = st.radio("Rows", (284, 301), horizontal=True)
full = pd.concat([result["df"]] * count, ignore_index=True)
full["제품코드"] = [f"{index:05d}" for index in range(count)]
columns = list(result["df_display"].columns)
if st.session_state.get("fixture_rows") != count:
    st.session_state["sims_export_tables"] = {"browser-fixture": full}
    st.session_state["sims_tables"] = {"browser-fixture": full.head(300)[columns].copy()}
    st.session_state["__sims_current_table_source_key"] = "browser-fixture"
    st.session_state["fixture_rows"] = count

st.caption(f"Offline editor fixture: {count} rows x {len(columns)} columns")
render_actual_quantity_editor({}, {"table_key": "browser-fixture"}, download_uid="browser-fixture")
saved = st.session_state["sims_export_tables"]["browser-fixture"].iloc[0]
st.caption(
    f"First row: actual={saved['실제 발주수량']}, "
    f"amount={saved['발주금액(부가세포함)']}"
)
