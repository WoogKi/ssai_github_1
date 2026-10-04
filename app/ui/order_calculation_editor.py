"""Chat-owned actual-quantity editing, without queries or Panel promotion."""

from decimal import Decimal, InvalidOperation
import logging
import pandas as pd
from pandas.io.formats.style import Styler
import streamlit as st

from app.services.order_calculation_contract import amounts
from app.services.order_calculation_service import ROW_KEY, _order_reason, decimal_or_none
from app.ui.sims_table_display import (
    _format_numeric_null_columns_for_display,
    _infer_width_px,
    _make_column_config,
    prepare_sims_table_display_df,
)


_ORDER_VIEW_LABELS = {
    "출고빈도등급": "등급", "추세판정": "추세", "계산 발주수량": "계산수량",
    "추천 발주수량": "추천수량", "실제 발주수량": "발주수량", "입고예정수량": "입고예정",
    "재고수량": "현재재고", "당월 정상출고수량": "당월출고",
    "최근3개월평균": "최근3개월", "이전3개월평균": "이전3개월",
}
_ORDER_VIEW_WIDTHS = {
    "제품코드": 82, "제품명": 230, "규격": 80, "발주처": 155,
    "출고빈도등급": 62, "추세판정": 92, "발주단위": 74,
    "계산 발주수량": 86, "추천 발주수량": 86, "실제 발주수량": 86,
    "입고예정수량": 84, "재고수량": 84, "당월 정상출고수량": 86,
}


_PAGE_SIZE = 300
_ORDER_QUANTITY_COLORS = {
    "계산 발주수량": "#edf3fb",
    "추천 발주수량": "#e8f4ee",
    "실제 발주수량": "#d8f0e2",
}
_ORDER_STOCK_COLOR = "#f5f3e8"
_ORDER_STOCK_COLUMNS = ("입고예정수량", "재고수량", "당월 정상출고수량")


def _order_table_display_config(source: pd.DataFrame, *, editing: bool) -> tuple[pd.DataFrame, dict, int]:
    view = prepare_sims_table_display_df(source, action_name="발주 계산")
    formatted = set()
    if not editing:
        view, formatted = _format_numeric_null_columns_for_display(view)
    config = {}
    for column in view.columns:
        width = _ORDER_VIEW_WIDTHS.get(column, _infer_width_px(view, column))
        cell = _make_column_config(
            view, column, width=width, pinned=column in {"제품코드", "제품명", "규격"},
            force_text=column in formatted, align_numbers=editing,
        )
        cell["label"] = _ORDER_VIEW_LABELS.get(column, column)
        if editing and column == "계산 발주수량" and cell.get("type_config", {}).get("type") == "number":
            cell["type_config"]["format"] = "%.2f"
        config[column] = cell
    height = min(520, max(170, 32 * (len(view) + 1) + 42))
    return view, config, height


def _style_order_table(view: pd.DataFrame, *, editing: bool) -> Styler:
    styled = view.style
    for column, color in _ORDER_QUANTITY_COLORS.items():
        if column in view and (not editing or column != "실제 발주수량"):
            styled = styled.set_properties(subset=[column], **{"background-color": color})
    stock_columns = [column for column in _ORDER_STOCK_COLUMNS if column in view]
    if stock_columns:
        styled = styled.set_properties(subset=stock_columns, **{"background-color": _ORDER_STOCK_COLOR})
    return styled


def _log_order_table_render(*, path: str, mode: str, renderer: str, page: int, page_count: int,
                            full: pd.DataFrame, visible: pd.DataFrame, editable: bool) -> None:
    logging.getLogger("ssai").info(
        "[order_calculation.table_render] render_path=%s mode=%s renderer=%s page=%s page_count=%s rows_on_page=%s full_rows=%s cols=%s editable=%s",
        path, mode, renderer, page, page_count, len(visible), len(full), len(visible.columns), editable,
    )


def apply_actual_edits(full: pd.DataFrame, visible: pd.DataFrame, changes: dict) -> pd.DataFrame:
    if not changes:
        return full
    result = full.copy()
    changed = False
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
        if decimal_or_none(result.loc[mask, "실제 발주수량"].iloc[0]) == value:
            continue
        calculated = amounts(value, decimal_or_none(source["발주단가"]))
        changed = True
        result.loc[mask, "실제 발주수량"] = value
        result.loc[mask, "추천대비수정수량"] = value - decimal_or_none(source["추천 발주수량"])
        for column, amount in calculated.items():
            result.loc[mask, column] = amount
        edited = result.loc[mask].iloc[0]
        result.loc[mask, "발주사유/계산근거"] = _order_reason(
            edited, int(edited.get("적용 horizon 영업일수") or 0),
        )
    return result if changed else full


def render_actual_quantity_editor(item: dict, meta: dict, *, download_uid: str):
    ss = st.session_state
    key = str(meta.get("table_key") or item.get("table_key") or "")
    if not key:
        return
    from app.db.mssql_client import get_current_company_id
    full = ss.get("sims_export_tables", {}).get(key)
    if not isinstance(full, pd.DataFrame):
        full = ss.get("__sims_export_tables_by_key", {}).get(key)
    if not isinstance(full, pd.DataFrame) or full.empty:
        return
    if not full["회사"].eq(get_current_company_id()).all():
        return
    page_count = (len(full) + _PAGE_SIZE - 1) // _PAGE_SIZE
    page_key = f"order_page::{key}"
    ss[page_key] = min(max(int(ss.get(page_key, 1)), 1), page_count)
    if page_count > 1:
        page = st.selectbox(
            "페이지", range(1, page_count + 1), key=page_key,
            format_func=lambda number: f"{number} / {page_count}",
            width=120,
        )
    else:
        page = 1
    visible = full.iloc[(page - 1) * _PAGE_SIZE:page * _PAGE_SIZE].copy()
    existing_display = ss.get("sims_tables", {}).get(key)
    display_columns = list(existing_display.columns) if isinstance(existing_display, pd.DataFrame) else list(full.columns)
    widget_key = f"order_actual::{key}::{page}"
    columns = [c for c in display_columns if c in visible]
    display_source = visible[columns].copy()
    raw = pd.to_numeric(display_source['계산 발주수량'], errors='coerce')
    display_source.loc[raw.le(0), '계산 발주수량'] = None
    render_path = str(ss.get("__sims_table_render_path") or "chat")
    is_current = ss.get("__sims_current_table_source_key") == key
    if not is_current:
        view, column_config, height = _order_table_display_config(display_source, editing=False)
        _log_order_table_render(path=render_path, mode="view", renderer="st.dataframe", page=page,
                                page_count=page_count, full=full, visible=view, editable=False)
        st.dataframe(_style_order_table(view, editing=False), hide_index=True, width="stretch", height=height,
                     column_config=column_config)
        return True
    editor, column_config, height = _order_table_display_config(display_source, editing=True)
    editor["실제 발주수량"] = pd.array([
        None if v is None or pd.isna(v) else int(v) for v in visible["실제 발주수량"]], dtype="Int64")
    actual_config = column_config.get("실제 발주수량") or {}
    column_config["실제 발주수량"] = st.column_config.NumberColumn(
        "🟩 발주수량", min_value=0, step=1, format="%d",
        width=actual_config.get("width"), pinned=actual_config.get("pinned"), alignment="right",
        help="정수를 입력하고 Enter 또는 다른 셀로 이동하면 수량과 금액이 반영됩니다.")

    def commit():
        state = ss.get(widget_key, {})
        try:
            if ss.get("__sims_current_table_source_key") != key or not full["회사"].eq(get_current_company_id()).all():
                raise ValueError("회사가 변경되었거나 현재표 소유권이 바뀌어 수정을 적용하지 않았습니다.")
            current = ss.get("sims_export_tables", {}).get(key)
            if not isinstance(current, pd.DataFrame) or not current["회사"].eq(get_current_company_id()).all():
                raise ValueError("현재 회사의 발주계산 원본을 확인할 수 없습니다.")
            updated = apply_actual_edits(current, visible, state.get("edited_rows", {}))
        except ValueError as exc:
            ss[f"order_edit_error::{key}"] = str(exc)
            return
        ss.pop(f"order_edit_error::{key}", None)
        if updated is current:
            return
        display = updated.head(_PAGE_SIZE).loc[:, display_columns].copy()
        for store in ("sims_export_tables", "__sims_export_tables_by_key"):
            ss.setdefault(store, {})[key] = updated
        ss.setdefault("sims_tables", {})[key] = display
        item.update(df=updated, df_display=display, records=display.to_dict("records"))
        if isinstance(item.get("data"), pd.DataFrame):
            item["data"] = display
        meta["quantity_edit_revision"] = int(meta.get("quantity_edit_revision", 0)) + 1
        item["meta"] = meta
        from app.ui.chat_middleware import _build_sims_context_from_result
        _build_sims_context_from_result(item, "발주 계산", dict(item.get("params") or {}), updated)
        for cache_key in list(ss.get("__sims_table_render_cache", {})):
            if str(cache_key).startswith(key + ":"):
                ss["__sims_table_render_cache"].pop(cache_key, None)
        for cache_key in (f"__sims_download_ready::{download_uid}", f"__sims_download_bytes::{download_uid}"):
            ss.pop(cache_key, None)
        logging.getLogger("ssai").info("[order_calculation.edit] company_id=%s table_key=%s edited_rows=%s revision=%s",
            get_current_company_id(), key, len(state.get("edited_rows", {})), meta["quantity_edit_revision"])

    _log_order_table_render(path=render_path, mode="edit", renderer="st.data_editor", page=page,
                            page_count=page_count, full=full, visible=editor, editable=True)
    st.data_editor(_style_order_table(editor, editing=True), key=widget_key, on_change=commit, hide_index=True,
        placeholder=" ",
        disabled=[c for c in editor if c != "실제 발주수량"], column_config=column_config,
        num_rows="fixed", height=height, width="stretch")
    error = ss.get(f"order_edit_error::{key}")
    if error:
        st.error(error)
    return True
