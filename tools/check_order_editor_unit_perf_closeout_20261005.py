"""Offline editor revision and bounded unit-source production-branch gates."""

from __future__ import annotations

from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import order_calculation_service as calculation
from app.services import rddbc170_rddbc180_order_service as orders
from app.ui import order_calculation_editor as editor
from app.ui.current_table_followups.analysis_facts import build_whole_table_facts
from app.ui.sims_table_display import apply_sims_export_projection
from tools.check_order_calculation_contract import fixture


def check_editor_revision() -> None:
    params, sources = fixture()
    with patch("app.services.order_calculation_service.get_current_company_id", return_value=7):
        result = calculation.get_order_calculation_result(params, source_loader=lambda _q: sources)
    full = result["df"].copy()
    key = "order-edit-fixture"
    uid = "order-download-fixture"
    meta = {**result["meta"], "table_key": key, "quantity_edit_revision": 0}
    item = {"params": params, "meta": meta, "df": full,
            "df_display": full[result["df_display"].columns].copy()}
    state = {
        "sims_export_tables": {key: full},
        "sims_tables": {key: item["df_display"]},
        "__sims_current_table_source_key": key,
        f"__sims_download_ready::{uid}": True,
        f"__sims_download_bytes::{uid}": {"excel_bytes": b"old"},
    }
    captured = {}
    original_columns = editor.st.column_config
    fake = SimpleNamespace(session_state=state, column_config=original_columns,
                           data_editor=lambda _df, **kwargs: captured.update(kwargs),
                           error=lambda *_a: None)
    with ExitStack() as stack:
        stack.enter_context(patch("sqlalchemy.engine.Engine.connect", side_effect=AssertionError("DB forbidden")))
        stack.enter_context(patch("pyodbc.connect", side_effect=AssertionError("DB forbidden")))
        stack.enter_context(patch.object(editor, "st", fake))
        stack.enter_context(patch("app.db.mssql_client.get_current_company_id", return_value=7))
        context = stack.enter_context(patch("app.ui.chat_middleware._build_sims_context_from_result"))

        def render_and_commit(changes: dict | None) -> None:
            assert editor.render_actual_quantity_editor(item, meta, download_uid=uid)
            state[captured["key"]] = {"edited_rows": changes or {}}
            captured["on_change"]()

        render_and_commit({0: {"실제 발주수량": "20"}})
        assert meta["quantity_edit_revision"] == 1 and context.call_count == 1
        assert f"__sims_download_ready::{uid}" not in state
        assert f"__sims_download_bytes::{uid}" not in state
        updated = state["sims_export_tables"][key]
        assert updated.iloc[0]["실제 발주수량"] == 20
        assert updated.iloc[0]["추천 발주수량"] == full.iloc[0]["추천 발주수량"]
        exported = apply_sims_export_projection(updated)
        assert exported.iloc[0]["실제 발주수량"] == 20
        assert exported.iloc[0]["발주금액(부가세포함)"] == updated.iloc[0]["발주금액(부가세포함)"]
        facts = build_whole_table_facts(updated, action="발주 계산", query="왜 이 수량인가")
        assert facts is not None

        prepared = {"excel_bytes": b"new"}
        state[f"__sims_download_ready::{uid}"] = True
        state[f"__sims_download_bytes::{uid}"] = prepared
        render_and_commit({})
        assert meta["quantity_edit_revision"] == 1 and context.call_count == 1
        assert state[f"__sims_download_bytes::{uid}"] is prepared
        render_and_commit({0: {"실제 발주수량": "20"}})
        assert meta["quantity_edit_revision"] == 1 and context.call_count == 1
        assert state[f"__sims_download_bytes::{uid}"] is prepared
        assert editor.render_actual_quantity_editor(item, meta, download_uid=uid)
        assert meta["quantity_edit_revision"] == 1 and state[f"__sims_download_bytes::{uid}"] is prepared
        render_and_commit({0: {"실제 발주수량": "7"}})
        assert meta["quantity_edit_revision"] == 2 and context.call_count == 2
        assert f"__sims_download_ready::{uid}" not in state
        assert state["sims_export_tables"][key].iloc[0]["실제 발주수량"] == 7


def check_unit_complete_scope() -> None:
    params = {
        "date_from": "20260705", "date_to": "20261005",
        "status_codes": ["1", "2", "3"], "_order_unit_history_minimal": True,
        "_order_unit_selected_scope": True,
        "order_unit_product_code_list": ["12345", "A&B"],
        "product_prescription_semantic": "prescription",
    }
    observed = []
    raw = pd.DataFrame([
        {"제품코드": "12345", "발주일자": "20260920", "발주거래처코드": "V",
         "발주순번": 1, "상세순번": 1, "발주수량": Decimal(5)},
        {"제품코드": "A&B", "발주일자": "20260921", "발주거래처코드": "V",
         "발주순번": 2, "상세순번": 1, "발주수량": Decimal(12)},
    ])
    with patch("sqlalchemy.engine.Engine.connect", side_effect=AssertionError("DB forbidden")), \
         patch("pyodbc.connect", side_effect=AssertionError("DB forbidden")), \
         patch.object(orders, "execute_bound_select", side_effect=lambda sql, binds: (
             observed.append((sql, binds)) or raw.copy()
         )):
        result = orders.get_order_df(params, mode="order")
    assert len(observed) == 1
    sql, binds = observed[0]
    assert "SELECT TOP" not in sql and "Rddbc040" not in sql
    assert ".nodes('/codes/c')" in sql and "Rd18_Or_Di IN (?,?,?)" in sql
    assert "A&amp;B" in binds[-1] and len(binds) == 6
    assert result.attrs["order_unit_history_complete_scope"] is True
    assert calculation._unit_history_completeness(result, recent_start="20260905", top=2) == (True, True)
    duplicate = pd.concat([result, result.iloc[[0]]], ignore_index=True)
    duplicate.attrs.update(result.attrs)
    assert calculation._unit_history_completeness(duplicate, recent_start="20260905", top=2) == (False, False)
    capped = result.copy()
    capped.attrs.clear()
    assert calculation._unit_history_completeness(capped, recent_start="20260905", top=2)[1] is False


if __name__ == "__main__":
    check_editor_revision()
    check_unit_complete_scope()
    print("PASS editor idempotency/download revision and scoped unit SQL; DB attempts=0")
