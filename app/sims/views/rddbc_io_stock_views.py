# app/sims/views/rddbc_io_stock_views.py

from __future__ import annotations

from typing import Any, Callable, Dict, Optional
import datetime as dt

import pandas as pd
import streamlit as st

from app.services.rddbc210_service import get_rddbc210_result
from app.services.rddbc220_service import get_rddbc220_result
from app.sims.views.rddbc_io_shared import (
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
)

_STOCK_LOCAL_FILTER_ALIAS_MAP: Dict[str, list[str]] = {
    "ven_cd": ["거래처코드", "매입처코드", "VEN_CD", "ven_cd"],
    "ven_nm": ["거래처명", "매입처명", "VEN_NM", "ven_nm"],
    "physic_cd": ["제품코드", "PHYSIC_CD", "physic_cd"],
    "physic_nm": ["제품명", "PHYSIC_NM", "physic_nm"],
    "stock_cd": [
        "재고위치코드",
        "재고위치 코드",
        "재고코드",
        "STOCK_CD",
        "stock_cd",
        "Rd21_Stock_Cd",
        "Rd22_Stock_Cd",
    ],
    "stock_nm": [
        "재고위치",
        "재고위치명",
        "STOCK_NM",
        "stock_nm",
        "Rd01_Hnm",
    ],
    "stock_yymm": [
        "재고년월",
        "재고 년월",
        "STOCK_YYMM",
        "stock_yymm",
        "Rd21_Stock_YyMm",
        "Rd22_Stock_YyMm",
    ],
}


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

    if not isinstance(payload.get("params"), dict):
        payload["params"] = {}

    return payload


def _default_stock_codes(defaults: Dict[str, Any]) -> list[str]:
    stock_cds = defaults.get("stock_cds")
    if isinstance(stock_cds, list):
        return [str(x).strip() for x in stock_cds if str(x).strip()]

    stock_cd = str(defaults.get("stock_cd", "")).strip()
    return [stock_cd] if stock_cd else []


def _month_default_to_date(value: Any, fallback: dt.date) -> dt.date:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())

    try:
        if len(digits) == 6:
            return dt.datetime.strptime(digits + "01", "%Y%m%d").date()
        if len(digits) == 8:
            return dt.datetime.strptime(digits, "%Y%m%d").date()
    except Exception:
        pass

    return fallback


def _month_from_date_text(value: str, fallback: str = "") -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 6:
        return digits[:6]
    return fallback


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
        return _empty_result_payload(title, final_params, "조회 결과가 없습니다.")

    return _force_payload_title(payload, title)


def _render_product_move_form(
    *,
    prefix: str,
    form_key: str,
    caption_text: str,
    defaults: Dict[str, Any],
) -> Dict[str, Any]:
    from app.db.mssql_client import read_df

    @st.cache_data(ttl=600, show_spinner=False)
    def _load_code_name_options(gcode: str) -> list[str]:
        sql = """
SELECT DISTINCT LTRIM(RTRIM(Rd01_Hnm)) AS code_name
FROM dbo.Rddbc010 WITH (NOLOCK)
WHERE Rd01_Gcode = ?
  AND ISNULL(Rd01_Hnm, '') <> ''
ORDER BY LTRIM(RTRIM(Rd01_Hnm))
""".strip()

        try:
            df_code = read_df(sql, (gcode,))
        except Exception:
            return ["전체"]

        if df_code is None or df_code.empty or "code_name" not in df_code.columns:
            return ["전체"]

        vals: list[str] = []
        seen: set[str] = set()
        for v in df_code["code_name"].tolist():
            s = str(v or "").strip()
            if not s or s in seen:
                continue
            seen.add(s)
            vals.append(s)

        return ["전체"] + vals

    def _pick_index(options: list[str], value: Any) -> int:
        v = str(value or "").strip()
        if not v:
            return 0
        try:
            return options.index(v)
        except ValueError:
            return 0

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

    ven_group_options = _load_code_name_options("0019")
    ven_kind_options = _load_code_name_options("0009")
    group_options = _load_code_name_options("0013")
    di_options = _load_code_name_options("0004")
    class_options = _load_code_name_options("0001")

    move_type_options = ["실수불", "장부수불"]
    trans_type_options = ["전체", "매입", "매출"]

    maker_prefix = f"{prefix}_maker"
    maker_candidates_key = f"{maker_prefix}_vendor_candidates"
    maker_pick_key = f"{maker_prefix}_vendor_pick"

    _apply_vendor_input_sync_if_pending(maker_prefix)
    _maybe_reset_vendor_candidate_state(maker_prefix)

    selected_maker_cd = str(
        st.session_state.get(f"{maker_prefix}_ven_cd", defaults.get("product_ven_cd", ""))
    ).strip()
    selected_maker_nm = str(
        st.session_state.get(f"{maker_prefix}_ven_nm", defaults.get("product_ven_nm", ""))
    ).strip()

    maker_rows = st.session_state.get(maker_candidates_key, []) or []
    if not isinstance(maker_rows, list):
        maker_rows = []

    maker_options = ["선택하세요"] + [
        f"{cd} | {nm}"
        for cd, nm in maker_rows
        if str(cd).strip() or str(nm).strip()
    ]

    default_maker_idx = 0
    if selected_maker_cd and selected_maker_nm:
        target = f"{selected_maker_cd} | {selected_maker_nm}"
        try:
            default_maker_idx = maker_options.index(target)
        except ValueError:
            default_maker_idx = 0

    with st.form(form_key, clear_on_submit=False, enter_to_submit=False):
        st.markdown(
            """
            <style>
            div[data-testid="stForm"] button p {
                white-space: pre-line;
                line-height: 1.15;
                text-align: center;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )

        st.caption(caption_text)

        # 1줄: 수불구분 / 구분 / 시작일자 / 종료일자 / 재고위치 / 거래처코드 / 거래처명 / 후보검색 / 거래처후보 / 거래처그룹 / 거래처종류
        c1, c2, c3, c4, c5, c6, c7, c8, c9, c10, c11 = st.columns(
            [0.9, 0.9, 1.15, 1.15, 1.8, 1.0, 1.8, 0.9, 1.9, 1.1, 1.1]
        )

        with c1:
            move_type = st.selectbox(
                "수불구분",
                options=move_type_options,
                index=_pick_index(move_type_options, defaults.get("move_type", "실수불")),
                key=f"{prefix}_move_type",
            )

        with c2:
            trans_type = st.selectbox(
                "구분",
                options=trans_type_options,
                index=_pick_index(trans_type_options, defaults.get("trans_type", "전체")),
                key=f"{prefix}_trans_type",
            )

        with c3:
            date_from_value = st.date_input(
                "시작일자",
                value=default_from_date,
                key=f"{prefix}_date_from",
            )
            st.caption(_week_label_52(date_from_value))

        with c4:
            date_to_value = st.date_input(
                "종료일자",
                value=default_to_date,
                key=f"{prefix}_date_to",
            )
            st.caption(_week_label_52(date_to_value))

        with c5:
            stock_info = _render_stock_multiselect(
                prefix,
                label="재고위치",
                default_codes=_default_stock_codes(defaults),
            )

        with c6:
            ven_cd_key = f"{prefix}_ven_cd"
            if ven_cd_key in st.session_state:
                ven_cd = st.text_input("거래처코드", key=ven_cd_key).strip()
            else:
                ven_cd = st.text_input(
                    "거래처코드",
                    value=str(defaults.get("ven_cd", "")),
                    key=ven_cd_key,
                ).strip()

        with c7:
            ven_nm_key = f"{prefix}_ven_nm"
            if ven_nm_key in st.session_state:
                ven_nm = st.text_input("거래처명", key=ven_nm_key).strip()
            else:
                ven_nm = st.text_input(
                    "거래처명",
                    value=str(defaults.get("ven_nm", "")),
                    key=ven_nm_key,
                ).strip()

        with c8:
            vendor_search = st.form_submit_button(
                "거래처\n후보",
                use_container_width=True,
            )

        with c9:
            vendor_rows = st.session_state.get(f"{prefix}_vendor_candidates", []) or []
            vendor_options = [""] + [f"{cd} | {nm}" for cd, nm in vendor_rows]
            st.selectbox(
                "거래처 후보선택",
                options=vendor_options,
                key=f"{prefix}_vendor_pick",
            )

        with c10:
            ven_group_nm_sel = st.selectbox(
                "거래처그룹",
                options=ven_group_options,
                index=_pick_index(ven_group_options, defaults.get("ven_group_nm", "")),
                key=f"{prefix}_ven_group_nm",
            )

        with c11:
            ven_kind_nm_sel = st.selectbox(
                "거래처종류",
                options=ven_kind_options,
                index=_pick_index(ven_kind_options, defaults.get("ven_kind_nm", "")),
                key=f"{prefix}_ven_kind_nm",
            )

        vendor_msg = str(st.session_state.get(f"{prefix}_vendor_msg", "") or "").strip()
        if vendor_msg:
            st.caption(vendor_msg)

        # 2줄: 제품코드 / 제품명 / 제품후보 / 제품후보선택 / 제조사명 / 제조사후보 / 제품그룹명 / 제품구분명 / 제품분류명
        c12, c13, c14, c15, c16, c17, c18, c19, c20 = st.columns(
            [1.0, 2.2, 0.9, 2.0, 2.0, 1.5, 1.0, 1.0, 1.0]
        )

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
                "제품\n후보",
                use_container_width=True,
            )

        with c15:
            product_rows = st.session_state.get(f"{prefix}_product_candidates", []) or []
            product_options = [""] + [f"{cd} | {nm}" for cd, nm in product_rows]
            st.selectbox(
                "제품 후보선택",
                options=product_options,
                key=f"{prefix}_product_pick",
            )

        with c16:
            maker_ven_nm_key = f"{maker_prefix}_ven_nm"
            if maker_ven_nm_key in st.session_state:
                product_ven_nm = st.text_input("제조사명", key=maker_ven_nm_key).strip()
            else:
                product_ven_nm = st.text_input(
                    "제조사명",
                    value=selected_maker_nm,
                    key=maker_ven_nm_key,
                ).strip()

        with c17:
            maker_vendor_search = st.form_submit_button(
                "제조사\n후보",
                use_container_width=True,
            )

        with c18:
            group_name_sel = st.selectbox(
                "제품그룹명",
                options=group_options,
                index=_pick_index(group_options, defaults.get("product_group_nm", "")),
                key=f"{prefix}_product_group_nm",
            )

        with c19:
            di_name_sel = st.selectbox(
                "제품구분명",
                options=di_options,
                index=_pick_index(di_options, defaults.get("product_di_nm", "")),
                key=f"{prefix}_product_di_nm",
            )

        with c20:
            class_name_sel = st.selectbox(
                "제품분류명",
                options=class_options,
                index=_pick_index(class_options, defaults.get("product_class_nm", "")),
                key=f"{prefix}_product_class_nm",
            )

        with st.container():
            c21, _ = st.columns([2.0, 8.0])
            with c21:
                maker_choice = st.selectbox(
                    "제조사 후보선택",
                    options=maker_options,
                    index=default_maker_idx,
                    key=maker_pick_key,
                )

        product_msg = str(st.session_state.get(f"{prefix}_product_msg", "") or "").strip()
        if product_msg:
            st.caption(product_msg)

        maker_msg = str(st.session_state.get(f"{maker_prefix}_vendor_msg", "") or "").strip()
        if maker_msg:
            st.caption(maker_msg)

        chosen_maker_cd = ""
        chosen_maker_nm = str(product_ven_nm or "").strip()
        if maker_choice != "선택하세요":
            try:
                chosen_maker_cd, chosen_maker_nm = [x.strip() for x in maker_choice.split("|", 1)]
            except ValueError:
                chosen_maker_cd, chosen_maker_nm = "", str(product_ven_nm or "").strip()

        p = {
            "move_type": str(move_type).strip(),
            "trans_type": str(trans_type).strip(),
            "date_from": _to_yyyymmdd(date_from_value),
            "date_to": _to_yyyymmdd(date_to_value),
            "ven_cd": ven_cd,
            "ven_nm": ven_nm,
            "ven_group_nm": "" if ven_group_nm_sel == "전체" else str(ven_group_nm_sel).strip(),
            "ven_kind_nm": "" if ven_kind_nm_sel == "전체" else str(ven_kind_nm_sel).strip(),
            "physic_cd": physic_cd,
            "physic_nm": physic_nm,
            "product_ven_cd": chosen_maker_cd,
            "product_ven_nm": chosen_maker_nm,
            "product_group_nm": "" if group_name_sel == "전체" else str(group_name_sel).strip(),
            "product_di_nm": "" if di_name_sel == "전체" else str(di_name_sel).strip(),
            "product_class_nm": "" if class_name_sel == "전체" else str(class_name_sel).strip(),
            "stock_cd": stock_info["stock_cd"],
            "stock_nm": stock_info["stock_nm"],
            "stock_cds": stock_info["stock_cds"],
            "stock_names": stock_info["stock_names"],
            "stock_label_text": stock_info["stock_label_text"],
            # 화면 조회 상한은 기존 공통 env를 사용한다.
            # SIMS_PANEL_DISPLAY_MAX_ROWS 없으면 SIMS_CHAT_DISPLAY_MAX_ROWS, 그것도 없으면 30,000건.
            "top": _top_value(f"{prefix}_top", 30000),
        }
        p.update(_audit_inputs(prefix))

        submitted = st.form_submit_button(
            "조회",
            type="primary",
            use_container_width=True,
            on_click=_trigger_panel_run,
        )

    return {
        "params": p,
        "submitted": submitted,
        "vendor_search": vendor_search,
        "product_search": product_search,
        "maker_vendor_search": maker_vendor_search,
    }

def _render_stock_form(
    *,
    prefix: str,
    form_key: str,
    caption_text: str,
    defaults: Dict[str, Any],
) -> Dict[str, Any]:
    """
    실재고월집계/장부재고월집계 조회용 form wrapper.

    현재 월집계 화면은 제품수불현황과 동일한 조건 입력 구조를 사용하므로
    기존 _render_product_move_form() 을 그대로 재사용한다.

    나중에 월집계 전용 화면 구성이 필요하면 이 함수 내부만 분리하면 된다.
    """
    return _render_product_move_form(
        prefix=prefix,
        form_key=form_key,
        caption_text=caption_text,
        defaults=defaults,
    )


def _view_product_move_status_common(
    *,
    title: str,
    payload_key: str,
    form_key: str,
    caption_text: str,
    prefix: str,
    service_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
    vendor_scope: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    defaults = dict(params or {})
    maker_prefix = f"{prefix}_maker"

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
    _apply_vendor_input_sync_if_pending(maker_prefix)

    _maybe_reset_vendor_candidate_state(prefix)
    _maybe_reset_product_candidate_state(prefix)
    _maybe_reset_vendor_candidate_state(maker_prefix)

    form_state = _render_product_move_form(
        prefix=prefix,
        form_key=form_key,
        caption_text=caption_text,
        defaults=defaults,
    )

    p = form_state["params"]
    submitted = bool(form_state["submitted"])
    vendor_search = bool(form_state["vendor_search"])
    product_search = bool(form_state["product_search"])
    maker_vendor_search = bool(form_state.get("maker_vendor_search", False))

    if vendor_search:
        _store_vendor_candidates(prefix, p.get("ven_nm", ""), scope=vendor_scope)
        _rerun_panel_for_inner_submit()

    if product_search:
        _store_product_candidates(prefix, p.get("physic_nm", ""))
        _rerun_panel_for_inner_submit()

    if maker_vendor_search:
        _store_vendor_candidates(maker_prefix, p.get("product_ven_nm", ""), scope="maker")
        _rerun_panel_for_inner_submit()

    if submitted:
        payload = _run_stock_submit(
            title=title,
            payload_key=payload_key,
            prefix=prefix,
            defaults=defaults,
            p=p,
            service_fn=service_fn,
        )

        msg = str(payload.get("message") or payload.get("data") or "").strip()
        if msg and ("없습니다" in msg or "없읍니다" in msg or "0건" in msg):
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


def _run_stock_submit(
    *,
    title: str,
    payload_key: str,
    prefix: str,
    defaults: Dict[str, Any],
    p: Dict[str, Any],
    service_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
) -> Dict[str, Any]:
    def _silent_block_payload() -> Dict[str, Any]:
        payload = {
            "title": title,
            "action": title,
            "params": dict(p),
            "data": "",
            "message": "",
            "df": pd.DataFrame(),
            "df_display": pd.DataFrame(),
            "meta": {"row_count": 0},
            "final": False,
        }
        st.session_state[payload_key] = payload
        return payload

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

    maker_prefix = f"{prefix}_maker"
    maker_candidates_key = f"{maker_prefix}_vendor_candidates"
    maker_pick_key = f"{maker_prefix}_vendor_pick"
    maker_input_key = f"{maker_prefix}_ven_nm"

    raw_ven_cd = str(p.get("ven_cd", "")).strip()
    raw_ven_nm = str(p.get("ven_nm", "")).strip()
    raw_physic_cd = str(p.get("physic_cd", "")).strip()
    raw_physic_nm = str(p.get("physic_nm", "")).strip()

    raw_maker_nm = str(p.get("product_ven_nm", "")).strip()
    current_maker_input_nm = str(st.session_state.get(maker_input_key, raw_maker_nm)).strip()
    maker_candidates = st.session_state.get(maker_candidates_key, []) or []
    if not isinstance(maker_candidates, list):
        maker_candidates = []

    maker_choice = str(st.session_state.get(maker_pick_key, "") or "").strip()

    # 원칙
    # - 제약사명만 입력 후 조회 => LIKE 검색 허용
    # - 제약후보 버튼을 눌러 후보가 뜬 상태 => 반드시 후보선택 필요
    if current_maker_input_nm and maker_candidates and not (maker_choice and " | " in maker_choice):
        st.session_state[f"{maker_prefix}_vendor_msg"] = ""

        payload = {
            "title": title,
            "action": title,
            "params": dict(p),
            "data": "제약사 후보를 선택한 뒤 [조회] 버튼을 누르세요.",
            "message": "제약사 후보를 선택한 뒤 [조회] 버튼을 누르세요.",
            "df": pd.DataFrame(),
            "df_display": pd.DataFrame(),
            "meta": {"row_count": 0},
            "final": False,
        }
        st.session_state[payload_key] = payload
        return payload
# 단, 제약사명이 입력되어 있고 후보가 존재하는 상황에서 제약사 후보를 선택하지 않은 경우에는 입력된 제약사명으로 조회를 시도할 수 있도록 허용     
    if maker_choice and " | " in maker_choice:
        st.session_state[f"{maker_prefix}_vendor_msg"] = ""
        try:
            maker_cd, maker_nm = maker_choice.split(" | ", 1)
            p["product_ven_cd"] = maker_cd.strip()
            p["product_ven_nm"] = maker_nm.strip()
        except Exception:
            pass

    p = _apply_vendor_pick(prefix, p)
    p = _apply_product_pick(prefix, p)

    stock_cds = [str(x).strip() for x in p.get("stock_cds", []) if str(x).strip()]

    requested_filters = {
        "ven_cd": str(p.get("ven_cd", "")).strip(),
        "ven_nm": str(p.get("ven_nm", "")).strip(),
        "ven_group_nm": str(p.get("ven_group_nm", "")).strip(),
        "ven_kind_nm": str(p.get("ven_kind_nm", "")).strip(),
        "physic_cd": str(p.get("physic_cd", "")).strip(),
        "physic_nm": str(p.get("physic_nm", "")).strip(),
        "product_ven_cd": str(p.get("product_ven_cd", "")).strip(),
        "product_ven_nm": str(p.get("product_ven_nm", "")).strip(),
        "product_group_nm": str(p.get("product_group_nm", "")).strip(),
        "product_di_nm": str(p.get("product_di_nm", "")).strip(),
        "product_class_nm": str(p.get("product_class_nm", "")).strip(),
        "stock_cd": str(p.get("stock_cd", "")).strip(),
        "stock_nm": "",
    }

    service_p, stock_code_candidates = _prepare_io_service_params(
        base_params=p,
        requested_filters=requested_filters,
        seq_keys=[],
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

    final_ven_cd = str(p.get("ven_cd", "")).strip()
    final_ven_nm = str(p.get("ven_nm", "")).strip()
    final_physic_cd = str(p.get("physic_cd", "")).strip()
    final_physic_nm = str(p.get("physic_nm", "")).strip()
    final_maker_cd = str(p.get("product_ven_cd", "")).strip()
    final_maker_nm = str(p.get("product_ven_nm", "")).strip()

    final_params = dict(defaults)
    final_params.update(service_p)
    final_params["stock_cds"] = stock_cds
    final_params["stock_names"] = p.get("stock_names", [])
    final_params["stock_label_text"] = p.get("stock_label_text", "전체")
    final_params["ven_group_nm"] = str(p.get("ven_group_nm", "")).strip()
    final_params["ven_kind_nm"] = str(p.get("ven_kind_nm", "")).strip()
    final_params["product_ven_cd"] = final_maker_cd
    final_params["product_ven_nm"] = final_maker_nm
    final_params["product_group_nm"] = str(p.get("product_group_nm", "")).strip()
    final_params["product_di_nm"] = str(p.get("product_di_nm", "")).strip()
    final_params["product_class_nm"] = str(p.get("product_class_nm", "")).strip()

    _clear_payload_key(payload_key)

    st.session_state[f"{maker_prefix}_vendor_msg"] = ""

    payload = service_fn(final_params)
    payload = _finalize_io_payload(
        payload=payload,
        requested_filters=requested_filters,
        final_params=final_params,
        title=title,
        alias_map=_STOCK_LOCAL_FILTER_ALIAS_MAP,
        contains_keys={"ven_nm", "physic_nm", "stock_nm"},
        stock_code_candidates=stock_code_candidates,
        stock_code_key="stock_cd",
        stock_name_key="stock_nm",
    )

    if len(stock_cds) > 1:
        payload = _filter_payload_by_stock_codes(
            payload=payload,
            stock_codes=stock_cds,
            alias_map=_STOCK_LOCAL_FILTER_ALIAS_MAP,
            title=title,
            final_params=final_params,
        )
    else:
        payload = _force_payload_title(payload, title)

    st.session_state[payload_key] = payload

    st.session_state[f"{prefix}_vendor_reset_pending"] = True
    st.session_state[f"{prefix}_product_reset_pending"] = True
    st.session_state[f"{maker_prefix}_vendor_reset_pending"] = True

    vendor_sync_needed = (final_ven_cd != raw_ven_cd) or (final_ven_nm != raw_ven_nm)
    product_sync_needed = (final_physic_cd != raw_physic_cd) or (final_physic_nm != raw_physic_nm)
    maker_sync_needed = bool(final_maker_nm) and (current_maker_input_nm != final_maker_nm)

    if vendor_sync_needed:
        _queue_vendor_input_sync(prefix, final_ven_cd, final_ven_nm)

    if product_sync_needed:
        _queue_product_input_sync(prefix, final_physic_cd, final_physic_nm)

    if final_maker_cd or final_maker_nm:
        _queue_vendor_input_sync(maker_prefix, final_maker_cd, final_maker_nm)

    if vendor_sync_needed or product_sync_needed or maker_sync_needed:
        _rerun_panel_for_inner_submit()

    return payload

def _view_rddbc_stock_common(
    *,
    title: str,
    payload_key: str,
    form_key: str,
    caption_text: str,
    prefix: str,
    service_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
    vendor_scope: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    defaults = dict(params or {})
    maker_prefix = f"{prefix}_maker"

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
    _apply_vendor_input_sync_if_pending(maker_prefix)

    _maybe_reset_vendor_candidate_state(prefix)
    _maybe_reset_product_candidate_state(prefix)
    _maybe_reset_vendor_candidate_state(maker_prefix)

    form_state = _render_stock_form(
        prefix=prefix,
        form_key=form_key,
        caption_text=caption_text,
        defaults=defaults,
    )

    p = form_state["params"]
    submitted = bool(form_state["submitted"])
    vendor_search = bool(form_state["vendor_search"])
    product_search = bool(form_state["product_search"])
    maker_vendor_search = bool(form_state.get("maker_vendor_search", False))

    if vendor_search:
        _store_vendor_candidates(prefix, p.get("ven_nm", ""), scope=vendor_scope)
        _rerun_panel_for_inner_submit()

    if product_search:
        _store_product_candidates(prefix, p.get("physic_nm", ""))
        _rerun_panel_for_inner_submit()

    if maker_vendor_search:
        _store_vendor_candidates(maker_prefix, p.get("product_ven_nm", ""), scope="maker")
        _rerun_panel_for_inner_submit()

    if submitted:
        payload = _run_stock_submit(
            title=title,
            payload_key=payload_key,
            prefix=prefix,
            defaults=defaults,
            p=p,
            service_fn=service_fn,
        )
        msg = str(payload.get("message") or payload.get("data") or "").strip()
        if msg and ("없습니다" in msg or "없읍니다" in msg or "0건" in msg):
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


def view_rddbc210(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _view_rddbc_stock_common(
        title="실재고월집계 조회",
        payload_key="__io210_last_payload",
        form_key="__io210_form",
        caption_text="조회조건 · 실재고월집계",
        prefix="__io210",
        service_fn=get_rddbc210_result,
        vendor_scope="purchase",
        params=params,
    )


def view_rddbc220(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _view_rddbc_stock_common(
        title="장부재고월집계 조회",
        payload_key="__io220_last_payload",
        form_key="__io220_form",
        caption_text="조회조건 · 장부재고월집계",
        prefix="__io220",
        service_fn=get_rddbc220_result,
        vendor_scope="purchase",
        params=params,
    )

def view_product_move_status(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from app.services.product_flow_service import get_product_flow_result

    return _view_product_move_status_common(
        title="제품수불현황 조회",
        payload_key="__product_flow_last_payload",
        form_key="__product_flow_form",
        caption_text="조회조건 · 제품수불현황",
        prefix="__product_flow",
        service_fn=get_product_flow_result,
        vendor_scope="purchase",
        params=params,
    )

