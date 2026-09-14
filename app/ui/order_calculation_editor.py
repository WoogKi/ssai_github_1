"""Chat-owned actual-quantity editing, without queries or Panel promotion."""

from decimal import Decimal, InvalidOperation
import logging
import pandas as pd
import streamlit as st

from app.services.order_calculation_contract import amounts
from app.services.order_calculation_service import ROW_KEY, decimal_or_none


def apply_actual_edits(full: pd.DataFrame, visible: pd.DataFrame, changes: dict) -> pd.DataFrame:
    result = full.copy()
    if result.duplicated(list(ROW_KEY)).any():
        raise ValueError("수정행 식별 key가 중복됩니다.")
    for position, change in changes.items():
        if set(change) - {"실제 발주수량"}:
            raise ValueError("실제 발주수량만 수정할 수 있습니다.")
        if not 0 <= int(position) < len(visible):
            raise ValueError("수정행 범위를 확인하세요.")
        source = visible.iloc[int(position)]
        mask = pd.Series(True, index=result.index)
        for key in ROW_KEY:
            mask &= result[key].eq(source[key])
        if mask.sum() != 1:
            raise ValueError("수정행을 원본에서 식별할 수 없습니다.")
        if decimal_or_none(source["추천 발주수량"]) is None:
            raise ValueError("수요/재고 근거를 먼저 확인하세요.")
        try:
            value = Decimal(str(change["실제 발주수량"]).strip())
        except (InvalidOperation, KeyError) as exc:
            raise ValueError("실제 발주수량을 숫자로 입력하세요.") from exc
        calculated = amounts(value, decimal_or_none(source["발주단가"]))
        result.loc[mask, "실제 발주수량"] = value
        result.loc[mask, "추천대비수정수량"] = value - decimal_or_none(source["추천 발주수량"])
        for column, amount in calculated.items():
            result.loc[mask, column] = amount
    return result


def render_actual_quantity_editor(item: dict, meta: dict, *, download_uid: str):
    ss = st.session_state
    key = str(meta.get("table_key") or item.get("table_key") or "")
    if not key or ss.get("__sims_current_table_source_key") != key:
        return
    from app.db.mssql_client import get_current_company_id
    full = ss.get("sims_export_tables", {}).get(key)
    if not isinstance(full, pd.DataFrame) or full.empty:
        return
    if not full["회사"].eq(get_current_company_id()).all():
        return
    page_count = (len(full) + 299) // 300
    page = st.selectbox("결과 페이지", range(1, page_count + 1), key=f"order_page::{key}") if page_count > 1 else 1
    visible = full.iloc[(page - 1) * 300:page * 300].copy()
    existing_display = ss.get("sims_tables", {}).get(key)
    display_columns = list(existing_display.columns) if isinstance(existing_display, pd.DataFrame) else list(full.columns)
    revision = int(meta.get("quantity_edit_revision", 0))
    widget_key = f"order_actual::{key}::{page}"
    columns = [c for c in display_columns if c in visible]
    from app.ui.sims_table_display import build_sims_table_display_config
    display_source = visible[columns].copy()
    raw = pd.to_numeric(display_source['계산 발주수량'], errors='coerce')
    display_source.loc[raw.le(0), '계산 발주수량'] = None
    editor, column_config, _, height = build_sims_table_display_config(
        display_source, action_name="발주 계산", meta=meta, add_row_no=False, native_numeric_cells=True)
    raw_config = column_config.get("계산 발주수량") or {}
    if raw_config.get("type_config", {}).get("type") == "number":
        raw_config["type_config"]["format"] = "%.2f"
    editor["실제 발주수량"] = pd.array([
        None if v is None or pd.isna(v) else int(v) for v in visible["실제 발주수량"]], dtype="Int64")
    actual_config = column_config.get("실제 발주수량") or {}
    column_config["실제 발주수량"] = st.column_config.NumberColumn(
        "실제 발주수량 (수정)", min_value=0, step=1, format="%d",
        width=actual_config.get("width"), pinned=actual_config.get("pinned"), alignment="right",
        help="정수를 입력하고 Enter 또는 다른 셀로 이동하면 수량과 금액이 반영됩니다.")

    def commit():
        state = ss.get(widget_key, {})
        try:
            if ss.get("__sims_current_table_source_key") != key or not full["회사"].eq(get_current_company_id()).all():
                raise ValueError("회사가 변경되었거나 현재표 소유권이 바뀌어 수정을 적용하지 않았습니다.")
            current = ss.get("sims_export_tables", {}).get(key)
            updated = apply_actual_edits(current, visible, state.get("edited_rows", {}))
        except ValueError as exc:
            ss[f"order_edit_error::{key}"] = str(exc)
            return
        ss.pop(f"order_edit_error::{key}", None)
        display = updated.head(300).loc[:, display_columns].copy()
        for store in ("sims_export_tables", "__sims_export_tables_by_key"):
            ss.setdefault(store, {})[key] = updated
        ss.setdefault("sims_tables", {})[key] = display
        item.update(df=updated, df_display=display, records=display.to_dict("records"))
        if isinstance(item.get("data"), pd.DataFrame):
            item["data"] = display
        meta["quantity_edit_revision"] = revision + 1
        item["meta"] = meta
        from app.ui.chat_middleware import _build_sims_context_from_result
        _build_sims_context_from_result(item, "발주 계산", dict(item.get("params") or {}), updated)
        for cache_key in list(ss.get("__sims_table_render_cache", {})):
            if str(cache_key).startswith(key + ":"):
                ss["__sims_table_render_cache"].pop(cache_key, None)
        for cache_key in (f"__sims_download_ready::{download_uid}", f"__sims_download_bytes::{download_uid}"):
            ss.pop(cache_key, None)
        logging.getLogger("ssai").info("[order_calculation.edit] company_id=%s table_key=%s edited_rows=%s revision=%s",
            get_current_company_id(), key, len(state.get("edited_rows", {})), revision + 1)

    st.data_editor(editor, key=widget_key, on_change=commit, hide_index=True,
        placeholder=" ",
        disabled=[c for c in editor if c != "실제 발주수량"], column_config=column_config,
        num_rows="fixed", height=height, width="stretch")
    error = ss.get(f"order_edit_error::{key}")
    if error:
        st.error(error)
    return True
