# app/sims/views/rddbc_io_check_views.py

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

import pandas as pd
import streamlit as st

from app.services.rddbc110_service import get_rddbc110_result
from app.services.rddbc120_service import get_rddbc120_result
from app.sims.views.rddbc_io_shared import (
    _IO110_LOCAL_FILTER_ALIAS_MAP,
    _IO120_LOCAL_FILTER_ALIAS_MAP,
    _apply_product_input_sync_if_pending,
    _apply_product_pick,
    _apply_vendor_input_sync_if_pending,
    _apply_vendor_pick,
    _audit_inputs,
    _clear_payload_key,
    _empty_result_payload,
    _finalize_io_payload,
    _maybe_reset_product_candidate_state,
    _maybe_reset_vendor_candidate_state,
    _needs_product_pick,
    _needs_vendor_pick,
    _prepare_io_service_params,
    _queue_product_input_sync,
    _queue_vendor_input_sync,
    _render_io_date_range,
    _render_product_candidate_row,
    _render_stock_multiselect,
    _render_vendor_candidate_row,
    _rerun_panel_for_inner_submit,
    _store_product_candidates,
    _store_vendor_candidates,
    _top_value,
    _trigger_panel_run,
    _txt,
)


def _normalize_col_name(value: Any) -> str:
    return (
        str(value or "")
        .strip()
        .lower()
        .replace(" ", "")
        .replace("_", "")
        .replace("-", "")
        .replace("(", "")
        .replace(")", "")
    )


def _normalize_code_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    text = text.replace(",", "")

    # 1.0 / 10.000 같은 표시 보정
    if "." in text:
        left, right = text.split(".", 1)
        if left.lstrip("-").isdigit() and right.isdigit() and set(right) <= {"0"}:
            text = left

    return text.strip()


def _normalize_name_value(value: Any) -> str:
    return str(value or "").strip()


def _find_first_existing_col(df: pd.DataFrame, names: list[str]) -> str:
    for name in names:
        if name in df.columns:
            return name

    norm_map = {_normalize_col_name(col): col for col in df.columns}
    for name in names:
        found = norm_map.get(_normalize_col_name(name))
        if found:
            return found

    return ""


def _force_payload_title(payload: Dict[str, Any], title: str) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return payload

    payload["title"] = title
    payload["action"] = title

    params = payload.get("params")
    if not isinstance(params, dict):
        payload["params"] = {}
    return payload


def _filter_payload_by_stock_codes(
    payload: Dict[str, Any],
    stock_codes: list[str],
    alias_map: Dict[str, list[str]],
    title: str,
    final_params: Dict[str, Any],
) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    if not stock_codes:
        return _force_payload_title(payload, title)

    code_aliases = alias_map.get("stock_cd", [])
    name_aliases = alias_map.get("stock_nm", [])
    selected_codes = {_normalize_code_value(x) for x in stock_codes if _normalize_code_value(x)}
    selected_names = {
        _normalize_name_value(x)
        for x in (final_params.get("stock_names") or [])
        if _normalize_name_value(x)
    }

    row_count = None

    df = payload.get("df")
    df_display = payload.get("df_display")
    records = payload.get("records")

    def _filter_df(src: pd.DataFrame | None) -> pd.DataFrame | None:
        if src is None or src.empty:
            return src

        code_col = _find_first_existing_col(src, code_aliases)
        name_col = _find_first_existing_col(src, name_aliases)

        if not code_col and not name_col:
            return src

        mask = None

        if code_col:
            code_series = src[code_col].apply(_normalize_code_value)
            code_mask = code_series.isin(selected_codes)
            mask = code_mask if mask is None else (mask | code_mask)

        if name_col and selected_names:
            name_series = src[name_col].apply(_normalize_name_value)
            name_mask = name_series.isin(selected_names)
            mask = name_mask if mask is None else (mask | name_mask)

        if mask is None:
            return src

        return src.loc[mask].copy()

    if isinstance(df, pd.DataFrame):
        original_df = df
        filtered_df = _filter_df(original_df)
        payload["df"] = filtered_df

        if isinstance(df_display, pd.DataFrame):
            try:
                if filtered_df is not None and len(df_display) == len(original_df):
                    payload["df_display"] = df_display.loc[filtered_df.index].copy()
                else:
                    payload["df_display"] = _filter_df(df_display)
            except Exception:
                payload["df_display"] = _filter_df(df_display)

        row_count = 0 if filtered_df is None else int(len(filtered_df))

    elif isinstance(df_display, pd.DataFrame):
        filtered_display = _filter_df(df_display)
        payload["df_display"] = filtered_display
        row_count = 0 if filtered_display is None else int(len(filtered_display))

    elif isinstance(records, list):
        rows_df = pd.DataFrame(records)
        filtered_rows_df = _filter_df(rows_df)
        filtered_records = [] if filtered_rows_df is None else filtered_rows_df.to_dict(orient="records")
        payload["records"] = filtered_records

        if isinstance(payload.get("columns"), list):
            payload["columns"] = list(filtered_rows_df.columns) if filtered_rows_df is not None else []

        row_count = len(filtered_records)

    if isinstance(payload.get("meta"), dict) and row_count is not None:
        payload["meta"]["row_count"] = row_count

    if row_count == 0:
        return _empty_result_payload(title, final_params, "검증 결과 이상 자료가 없습니다.")

    return _force_payload_title(payload, title)


def _default_stock_codes(defaults: Dict[str, Any]) -> list[str]:
    stock_cds = defaults.get("stock_cds")
    if isinstance(stock_cds, list):
        return [str(x).strip() for x in stock_cds if str(x).strip()]

    stock_cd = str(defaults.get("stock_cd", "")).strip()
    return [stock_cd] if stock_cd else []


def _build_check_seq_inputs(
    *,
    prefix: str,
    defaults: Dict[str, Any],
    seq_key: str,
    seq_label: str,
) -> Dict[str, str]:
    c1, c2, c3 = st.columns(3)

    with c1:
        main_seq = _txt(f"{prefix}_{seq_key}", seq_label, str(defaults.get(seq_key, "")))
    with c2:
        trans_seq = _txt(f"{prefix}_trans_seq", "거래명세서순번", str(defaults.get("trans_seq", "")))
    with c3:
        tax_seq = _txt(f"{prefix}_tax_seq", "세금계산서순번", str(defaults.get("tax_seq", "")))

    return {
        seq_key: main_seq,
        "trans_seq": trans_seq,
        "tax_seq": tax_seq,
    }


def _render_check_form(
    *,
    prefix: str,
    form_key: str,
    caption_text: str,
    defaults: Dict[str, Any],
    seq_key: str,
    seq_label: str,
    mismatch_key: str,
) -> Dict[str, Any]:
    import datetime as dt

    def _parse_yyyymmdd(value: Any) -> dt.date | None:
        s = str(value or "").strip()
        digits = "".join(ch for ch in s if ch.isdigit())
        if len(digits) != 8:
            return None
        try:
            return dt.datetime.strptime(digits, "%Y%m%d").date()
        except Exception:
            return None

    def _to_yyyymmdd(value: Any) -> str:
        if isinstance(value, dt.datetime):
            return value.strftime("%Y%m%d")
        if isinstance(value, dt.date):
            return value.strftime("%Y%m%d")
        return ""

    def _week_label_52(value: dt.date) -> str:
        weekday_map = ["월", "화", "수", "목", "금", "토", "일"]
        week_no = ((value.timetuple().tm_yday - 1) // 7) + 1
        if week_no < 1:
            week_no = 1
        if week_no > 52:
            week_no = 52
        return f"{weekday_map[value.weekday()]} / {week_no}주"

    today = dt.date.today()
    month_first = today.replace(day=1)

    default_from_date = _parse_yyyymmdd(defaults.get("date_from")) or month_first
    default_to_date = _parse_yyyymmdd(defaults.get("date_to")) or today

    vendor_candidates_key = f"{prefix}_vendor_candidates"
    vendor_pick_key = f"{prefix}_vendor_pick"
    product_candidates_key = f"{prefix}_product_candidates"
    product_pick_key = f"{prefix}_product_pick"

    vendor_rows = st.session_state.get(vendor_candidates_key, []) or []
    if not isinstance(vendor_rows, list):
        vendor_rows = []
    vendor_options = [""] + [f"{cd} | {nm}" for cd, nm in vendor_rows]

    product_rows = st.session_state.get(product_candidates_key, []) or []
    if not isinstance(product_rows, list):
        product_rows = []
    product_options = [""] + [f"{cd} | {nm}" for cd, nm in product_rows]

    with st.form(form_key, clear_on_submit=False, enter_to_submit=False):
        st.caption(caption_text)

        # 1줄: 시작일자 / 종료일자 / 메인순번 / 거래명세서순번 / 세금계산서순번 / 재고위치 / 조회건수
        c1, c2, c3, c4, c5, c6, c7 = st.columns([1.15, 1.15, 1.0, 1.0, 1.0, 2.3, 0.8])

        with c1:
            date_from_value = st.date_input(
                "시작일자",
                value=default_from_date,
                key=f"{prefix}_date_from",
            )
            st.caption(_week_label_52(date_from_value))

        with c2:
            date_to_value = st.date_input(
                "종료일자",
                value=default_to_date,
                key=f"{prefix}_date_to",
            )
            st.caption(_week_label_52(date_to_value))

        with c3:
            main_seq = _txt(f"{prefix}_{seq_key}", seq_label, str(defaults.get(seq_key, "")))

        with c4:
            trans_seq = _txt(f"{prefix}_trans_seq", "거래명세서순번", str(defaults.get("trans_seq", "")))

        with c5:
            tax_seq = _txt(f"{prefix}_tax_seq", "세금계산서순번", str(defaults.get("tax_seq", "")))

        with c6:
            stock_info = _render_stock_multiselect(
                prefix,
                label="재고위치",
                default_codes=_default_stock_codes(defaults),
            )

        with c7:
            top = _top_value(f"{prefix}_top", int(defaults.get("top", 200)))

        # 2줄: 거래처코드 / 거래처명 / 후보검색 / 거래처 후보선택
        c8, c9, c10, c11 = st.columns([1.3, 2.9, 1.0, 3.0])

        with c8:
            ven_cd_key = f"{prefix}_ven_cd"
            if ven_cd_key in st.session_state:
                ven_cd = st.text_input("거래처코드", key=ven_cd_key).strip()
            else:
                ven_cd = st.text_input(
                    "거래처코드",
                    value=str(defaults.get("ven_cd", "")),
                    key=ven_cd_key,
                ).strip()

        with c9:
            ven_nm_key = f"{prefix}_ven_nm"
            if ven_nm_key in st.session_state:
                ven_nm = st.text_input("거래처명", key=ven_nm_key).strip()
            else:
                ven_nm = st.text_input(
                    "거래처명",
                    value=str(defaults.get("ven_nm", "")),
                    key=ven_nm_key,
                ).strip()

        with c10:
            vendor_search = st.form_submit_button(
                "거래처 후보검색",
                width="stretch",
            )

        with c11:
            st.selectbox(
                "거래처 후보선택",
                options=vendor_options,
                key=vendor_pick_key,
            )

        vendor_msg = str(st.session_state.get(f"{prefix}_vendor_msg", "") or "").strip()
        if vendor_msg:
            st.caption(vendor_msg)

        # 3줄: 제품코드 / 제품명 / 제품 후보검색 / 제품 후보선택
        c12, c13, c14, c15 = st.columns([1.3, 2.9, 1.0, 3.0])

        with c12:
            physic_cd_key = f"{prefix}_physic_cd"
            if physic_cd_key in st.session_state:
                physic_cd = st.text_input("제품코드", key=physic_cd_key).strip()
            else:
                physic_cd = st.text_input(
                    "제품코드",
                    value=str(defaults.get("physic_cd", "")),
                    key=physic_cd_key,
                ).strip()

        with c13:
            physic_nm_key = f"{prefix}_physic_nm"
            if physic_nm_key in st.session_state:
                physic_nm = st.text_input("제품명", key=physic_nm_key).strip()
            else:
                physic_nm = st.text_input(
                    "제품명",
                    value=str(defaults.get("physic_nm", "")),
                    key=physic_nm_key,
                ).strip()

        with c14:
            product_search = st.form_submit_button(
                "제품 후보검색",
                width="stretch",
            )

        with c15:
            st.selectbox(
                "제품 후보선택",
                options=product_options,
                key=product_pick_key,
            )

        product_msg = str(st.session_state.get(f"{prefix}_product_msg", "") or "").strip()
        if product_msg:
            st.caption(product_msg)

        p = {
            "date_from": _to_yyyymmdd(date_from_value),
            "date_to": _to_yyyymmdd(date_to_value),
            "top": top,
            "ven_cd": ven_cd,
            "ven_nm": ven_nm,
            "physic_cd": physic_cd,
            "physic_nm": physic_nm,
            "stock_cd": stock_info["stock_cd"],
            "stock_nm": stock_info["stock_nm"],
            "stock_cds": stock_info["stock_cds"],
            "stock_names": stock_info["stock_names"],
            "stock_label_text": stock_info["stock_label_text"],
            seq_key: main_seq,
            "trans_seq": trans_seq,
            "tax_seq": tax_seq,
        }
        p.update(_audit_inputs(prefix))

        # 검증 화면은 불일치만 강제
        p[mismatch_key] = "Y"

        submitted = st.form_submit_button(
            "조회",
            type="primary",
            width="stretch",
            on_click=_trigger_panel_run,
        )

    return {
        "params": p,
        "submitted": submitted,
        "vendor_search": vendor_search,
        "product_search": product_search,
    }


def _handle_candidate_search(
    *,
    prefix: str,
    p: Dict[str, Any],
    vendor_search: bool,
    product_search: bool,
    vendor_scope: str,
) -> None:
    if vendor_search:
        _store_vendor_candidates(prefix, p.get("ven_nm", ""), scope=vendor_scope)
        _rerun_panel_for_inner_submit()

    if product_search:
        _store_product_candidates(prefix, p.get("physic_nm", ""))
        _rerun_panel_for_inner_submit()


def _run_check_submit(
    *,
    title: str,
    payload_key: str,
    prefix: str,
    defaults: Dict[str, Any],
    p: Dict[str, Any],
    mismatch_key: str,
    seq_key: str,
    alias_map: Dict[str, list[str]],
    service_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
) -> Dict[str, Any]:
    if _needs_vendor_pick(prefix, p):
        return {
            "title": title,
            "action": title,
            "params": {},
            "data": "거래처 후보를 선택한 뒤 [조회] 버튼을 누르세요.",
            "message": "거래처 후보를 선택한 뒤 [조회] 버튼을 누르세요.",
            "final": False,
        }

    if _needs_product_pick(prefix, p):
        return {
            "title": title,
            "action": title,
            "params": {},
            "data": "제품 후보를 선택한 뒤 [조회] 버튼을 누르세요.",
            "message": "제품 후보를 선택한 뒤 [조회] 버튼을 누르세요.",
            "final": False,
        }

    raw_ven_cd = str(p.get("ven_cd", "")).strip()
    raw_ven_nm = str(p.get("ven_nm", "")).strip()
    raw_physic_cd = str(p.get("physic_cd", "")).strip()
    raw_physic_nm = str(p.get("physic_nm", "")).strip()

    p = _apply_vendor_pick(prefix, p)
    p = _apply_product_pick(prefix, p)

    stock_cds = [str(x).strip() for x in p.get("stock_cds", []) if str(x).strip()]

    requested_filters = {
        "ven_cd": str(p.get("ven_cd", "")).strip(),
        "ven_nm": str(p.get("ven_nm", "")).strip(),
        "physic_cd": str(p.get("physic_cd", "")).strip(),
        "physic_nm": str(p.get("physic_nm", "")).strip(),
        seq_key: str(p.get(seq_key, "")).strip(),
        "trans_seq": str(p.get("trans_seq", "")).strip(),
        "tax_seq": str(p.get("tax_seq", "")).strip(),
        "stock_cd": str(p.get("stock_cd", "")).strip(),
        "stock_nm": "",
    }

    service_p, stock_code_candidates = _prepare_io_service_params(
        base_params=p,
        requested_filters=requested_filters,
        seq_keys=[seq_key, "trans_seq", "tax_seq"],
        stock_name_key="stock_nm",
        stock_code_key="stock_cd",
        base_top=3000,
        stock_name_top=10000,
    )

    if len(stock_cds) > 1:
        try:
            service_p["top"] = max(int(service_p.get("top", 200)), 10000)
        except Exception:
            service_p["top"] = 10000

    service_p[mismatch_key] = "Y"

    final_ven_cd = str(p.get("ven_cd", "")).strip()
    final_ven_nm = str(p.get("ven_nm", "")).strip()
    final_physic_cd = str(p.get("physic_cd", "")).strip()
    final_physic_nm = str(p.get("physic_nm", "")).strip()

    final_params = dict(defaults)
    final_params.update(service_p)
    final_params["stock_cds"] = stock_cds
    final_params["stock_names"] = p.get("stock_names", [])
    final_params["stock_label_text"] = p.get("stock_label_text", "전체")

    _clear_payload_key(payload_key)

    payload = service_fn(final_params)
    payload = _finalize_io_payload(
        payload=payload,
        requested_filters=requested_filters,
        final_params=final_params,
        title=title,
        alias_map=alias_map,
        contains_keys={"ven_nm", "physic_nm", "stock_nm"},
        stock_code_candidates=stock_code_candidates,
        stock_code_key="stock_cd",
        stock_name_key="stock_nm",
    )

    if len(stock_cds) > 1:
        payload = _filter_payload_by_stock_codes(
            payload=payload,
            stock_codes=stock_cds,
            alias_map=alias_map,
            title=title,
            final_params=final_params,
        )
    else:
        payload = _force_payload_title(payload, title)

    st.session_state[payload_key] = payload

    st.session_state[f"{prefix}_vendor_reset_pending"] = True
    st.session_state[f"{prefix}_product_reset_pending"] = True

    vendor_sync_needed = (final_ven_cd != raw_ven_cd) or (final_ven_nm != raw_ven_nm)
    product_sync_needed = (final_physic_cd != raw_physic_cd) or (final_physic_nm != raw_physic_nm)

    if vendor_sync_needed:
        _queue_vendor_input_sync(prefix, final_ven_cd, final_ven_nm)

    if product_sync_needed:
        _queue_product_input_sync(prefix, final_physic_cd, final_physic_nm)

    if vendor_sync_needed or product_sync_needed:
        _rerun_panel_for_inner_submit()

    return payload


def _view_rddbc_check_common(
    *,
    title: str,
    payload_key: str,
    form_key: str,
    caption_text: str,
    prefix: str,
    service_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
    mismatch_key: str,
    seq_key: str,
    seq_label: str,
    vendor_scope: str,
    alias_map: Dict[str, list[str]],
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    defaults = dict(params or {})

    def _show_text_payload(payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return

        msg = str(payload.get("message") or payload.get("data") or "").strip()
        if not msg:
            return

        df_display = payload.get("df_display")
        if isinstance(df_display, pd.DataFrame) and not df_display.empty:
            return

        df = payload.get("df")
        if isinstance(df, pd.DataFrame) and not df.empty:
            return

        records = payload.get("records")
        if isinstance(records, list) and len(records) > 0:
            return

        if "후보를 선택" in msg:
            st.warning(msg)
        elif "없습니다" in msg or "없읍니다" in msg or "0건" in msg:
            st.info(msg)
        else:
            st.info(msg)

    _apply_vendor_input_sync_if_pending(prefix)
    _apply_product_input_sync_if_pending(prefix)

    _maybe_reset_vendor_candidate_state(prefix)
    _maybe_reset_product_candidate_state(prefix)

    form_state = _render_check_form(
        prefix=prefix,
        form_key=form_key,
        caption_text=caption_text,
        defaults=defaults,
        seq_key=seq_key,
        seq_label=seq_label,
        mismatch_key=mismatch_key,
    )

    p = form_state["params"]
    submitted = bool(form_state["submitted"])
    vendor_search = bool(form_state["vendor_search"])
    product_search = bool(form_state["product_search"])

    _handle_candidate_search(
        prefix=prefix,
        p=p,
        vendor_search=vendor_search,
        product_search=product_search,
        vendor_scope=vendor_scope,
    )

    if submitted:
        payload = _run_check_submit(
            title=title,
            payload_key=payload_key,
            prefix=prefix,
            defaults=defaults,
            p=p,
            mismatch_key=mismatch_key,
            seq_key=seq_key,
            alias_map=alias_map,
            service_fn=service_fn,
        )

        msg = str(payload.get("message") or payload.get("data") or "").strip()

        df_display = payload.get("df_display")
        has_df_display = isinstance(df_display, pd.DataFrame) and not df_display.empty

        df = payload.get("df")
        has_df = isinstance(df, pd.DataFrame) and not df.empty

        records = payload.get("records")
        has_records = isinstance(records, list) and len(records) > 0

        has_visible_rows = has_df_display or has_df or has_records

        if msg and not has_visible_rows:
            _show_text_payload(payload)

        return payload

    if payload_key not in st.session_state:
        return {
            "title": title,
            "action": title,
            "params": {},
            "data": "조회 조건을 입력한 뒤 [조회] 버튼을 누르세요.",
            "message": "조회 조건을 입력한 뒤 [조회] 버튼을 누르세요.",
            "final": False,
        }

    payload = _force_payload_title(st.session_state[payload_key], title)
    _show_text_payload(payload)
    return payload


def view_rddbc110_trans_check(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _view_rddbc_check_common(
        title="입고↔거래명세서 검증",
        payload_key="__io110_tc_last_payload",
        form_key="__io110_tc_form",
        caption_text="조회조건 · 입고↔거래명세서 검증",
        prefix="__io110_tc",
        service_fn=get_rddbc110_result,
        mismatch_key="only_mismatch_trans",
        seq_key="in_seq",
        seq_label="입고순번",
        vendor_scope="purchase",
        alias_map=_IO110_LOCAL_FILTER_ALIAS_MAP,
        params=params,
    )


def view_rddbc110_tax_check(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _view_rddbc_check_common(
        title="입고↔세금계산서 검증",
        payload_key="__io110_tx_last_payload",
        form_key="__io110_tx_form",
        caption_text="조회조건 · 입고↔세금계산서 검증",
        prefix="__io110_tx",
        service_fn=get_rddbc110_result,
        mismatch_key="only_mismatch_tax",
        seq_key="in_seq",
        seq_label="입고순번",
        vendor_scope="purchase",
        alias_map=_IO110_LOCAL_FILTER_ALIAS_MAP,
        params=params,
    )


def view_rddbc120_trans_check(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _view_rddbc_check_common(
        title="출고↔거래명세서 검증",
        payload_key="__io120_tc_last_payload",
        form_key="__io120_tc_form",
        caption_text="조회조건 · 출고↔거래명세서 검증",
        prefix="__io120_tc",
        service_fn=get_rddbc120_result,
        mismatch_key="only_mismatch_trans",
        seq_key="out_seq",
        seq_label="출고순번",
        vendor_scope="sales",
        alias_map=_IO120_LOCAL_FILTER_ALIAS_MAP,
        params=params,
    )


def view_rddbc120_tax_check(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _view_rddbc_check_common(
        title="출고↔세금계산서 검증",
        payload_key="__io120_tx_last_payload",
        form_key="__io120_tx_form",
        caption_text="조회조건 · 출고↔세금계산서 검증",
        prefix="__io120_tx",
        service_fn=get_rddbc120_result,
        mismatch_key="only_mismatch_tax",
        seq_key="out_seq",
        seq_label="출고순번",
        vendor_scope="sales",
        alias_map=_IO120_LOCAL_FILTER_ALIAS_MAP,
        params=params,
    )