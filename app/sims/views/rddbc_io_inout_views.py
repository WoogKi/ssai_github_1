# app/sims/views/rddbc_io_inout_views.py

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

import pandas as pd
import streamlit as st
from datetime import date, datetime
from app.db.mssql_client import read_df


from app.services.rddbc110_service import get_rddbc110_result, get_rddbc110_export_df
from app.services.rddbc120_service import get_rddbc120_result, get_rddbc120_export_df

from app.sims.views.rddbc_io_shared import (
    _IO110_LOCAL_FILTER_ALIAS_MAP,
    _IO120_LOCAL_FILTER_ALIAS_MAP,
    _apply_product_input_sync_if_pending,
    _apply_product_pick,
    _apply_vendor_input_sync_if_pending,
    _apply_vendor_pick,
    _audit_inputs,
    _checkbox_yn,
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
    _render_date_input_with_week,
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



def _find_first_existing_col(df: pd.DataFrame, names: list[str]) -> str:
    for name in names:
        if name in df.columns:
            return name
    return ""

def _display_top_from_params(params: Dict[str, Any], default: int = 200) -> int:
    try:
        top = int(params.get("display_top") or params.get("top") or default)
        return top if top > 0 else default
    except Exception:
        return default

def _fmt_ymd_for_condition(v: Any) -> str:
    s = str(v or "").strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    if len(digits) >= 6:
        return f"{digits[:4]}-{digits[4:6]}"
    return s


def _build_inout_condition_text(title: str, params: Dict[str, Any], *, display_top: int | None = None) -> str:
    title_s = str(title or "")
    parts: list[str] = []

    date_from = _fmt_ymd_for_condition(params.get("date_from"))
    date_to = _fmt_ymd_for_condition(params.get("date_to"))

    if date_from or date_to:
        if date_from and date_to:
            parts.append(f"기간 {date_from} ~ {date_to}")
        elif date_from:
            parts.append(f"기간 {date_from} 이후")
        elif date_to:
            parts.append(f"기간 {date_to} 이전")

    if str(params.get("only_mismatch_trans") or "").upper() in {"Y", "1", "TRUE"}:
        parts.append("거래명세서 불일치만")

    if str(params.get("only_mismatch_tax") or "").upper() in {"Y", "1", "TRUE"}:
        parts.append("세금계산서 불일치만")

    stock_label = str(params.get("stock_label_text") or "").strip()
    if stock_label:
        parts.append(f"재고위치 {stock_label}")

    ven_nm = str(params.get("ven_nm") or "").strip()
    if ven_nm:
        parts.append(f"거래처 {ven_nm}")

    physic_nm = str(params.get("physic_nm") or "").strip()
    if physic_nm:
        parts.append(f"제품 {physic_nm}")

    maker_nm = str(params.get("product_ven_nm") or "").strip()
    if maker_nm:
        parts.append(f"제조사 {maker_nm}")

    # display_top은 내부 화면 표시 제한값이므로 조회조건에는 노출하지 않는다.
    return " / ".join(parts) if parts else "전체"


def _attach_inout_full_df(
    *,
    payload: Dict[str, Any],
    title: str,
    final_params: Dict[str, Any],
    requested_filters: Dict[str, str],
    alias_map: Dict[str, list[str]],
    contains_keys: set[str],
    stock_code_candidates: list[str],
    stock_cds: list[str],
    display_top: int,
) -> Dict[str, Any]:
    """
    입고/출고 명세:
    - 화면 표시: display_top 건
    - 원본/다운로드/LLM/현재표: export 전체 DF
    """
    if not isinstance(payload, dict):
        return payload

    try:
        is_in_validation = "검증" in str(title) and "입고" in str(title)
        is_out_validation = "검증" in str(title) and "출고" in str(title)

        if title == "입고명세 조회" or is_in_validation:
            export_df = get_rddbc110_export_df(final_params)
        elif title == "출고명세 조회" or is_out_validation:
            export_df = get_rddbc120_export_df(final_params)
        else:
            return payload

        if not isinstance(export_df, pd.DataFrame) or export_df.empty:
            return payload

        export_payload = {
            "title": title,
            "action": title,
            "params": dict(final_params),
            "df": export_df,
            "df_display": export_df,
            "records": export_df.to_dict(orient="records"),
            "columns": list(export_df.columns),
            "meta": {
                "row_count": int(len(export_df)),
                "row_count_total": int(len(export_df)),
            },
            "final": True,
        }

        export_payload = _finalize_io_payload(
            payload=export_payload,
            requested_filters=requested_filters,
            final_params=final_params,
            title=title,
            alias_map=alias_map,
            contains_keys=contains_keys,
            stock_code_candidates=stock_code_candidates,
            stock_code_key="stock_cd",
            stock_name_key="stock_nm",
        )

        if len(stock_cds) > 1:
            export_payload = _filter_payload_by_stock_codes(
                payload=export_payload,
                stock_codes=stock_cds,
                alias_map=alias_map,
                title=title,
                final_params=final_params,
            )

        full_df = export_payload.get("df_display")
        if not isinstance(full_df, pd.DataFrame) or full_df.empty:
            full_df = export_payload.get("df")

        if not isinstance(full_df, pd.DataFrame) or full_df.empty:
            return payload

        # 여기서는 조회된 전체 결과를 넘긴다.
        # 실제 채팅/패널 화면 표시 제한은 chat_middleware/sims_panel 공통 로직이 담당한다.
        display_df = full_df.copy()

        payload["df"] = full_df
        payload["df_display"] = display_df
        payload["records"] = display_df.to_dict(orient="records")
        payload["columns"] = list(display_df.columns)

        meta = payload.setdefault("meta", {})
        meta["row_count"] = int(len(full_df))
        meta["display_row_count"] = int(len(display_df))
        meta["row_count_total"] = int(len(full_df))
        meta["analysis_row_count"] = int(len(full_df))
        meta["row_count_total_for_analysis"] = int(len(full_df))
        meta["download_row_count"] = int(len(full_df))
        meta["detail_count"] = int(len(full_df))
        meta["display_top"] = int(display_top)

        params = payload.setdefault("params", {})
        params["조회상한"] = int(display_top)
        params.setdefault("top", int(display_top))

        payload["message"] = f"{title.replace(' 조회', '')} {len(full_df):,}건"
        payload["data"] = payload.get("data") or ""

        log_msg = (
            f"[io.inout.full] action={title} "
            f"display_rows={len(display_df)} full_rows={len(full_df)} display_top={display_top}"
        )
        try:
            import logging
            logging.getLogger("ssai").info(log_msg)
        except Exception:
            pass

    except Exception:
        try:
            import logging
            logging.getLogger("ssai").exception("[io.inout.full] attach full df failed action=%s", title)
        except Exception:
            pass

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
        return payload

    code_aliases = alias_map.get("stock_cd", [])
    row_count = None

    df = payload.get("df")
    df_display = payload.get("df_display")
    records = payload.get("records")

    def _filter_df(src: pd.DataFrame | None) -> pd.DataFrame | None:
        if src is None or src.empty:
            return src
        code_col = _find_first_existing_col(src, code_aliases)
        if not code_col:
            return src
        s = src[code_col].fillna("").astype(str).str.strip()
        return src.loc[s.isin(stock_codes)].copy()

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

    return payload


def _default_stock_codes(defaults: Dict[str, Any]) -> list[str]:
    stock_cds = defaults.get("stock_cds")
    if isinstance(stock_cds, list):
        return [str(x).strip() for x in stock_cds if str(x).strip()]

    stock_cd = str(defaults.get("stock_cd", "")).strip()
    return [stock_cd] if stock_cd else []


def _build_seq_inputs(
    *,
    prefix: str,
    defaults: Dict[str, Any],
    io_kind: str,
) -> Dict[str, str]:
    c1, c2, c3 = st.columns(3)

    if io_kind == "in":
        with c1:
            io_seq = _txt(f"{prefix}_in_seq", "입고순번", str(defaults.get("in_seq", "")))
        io_seq_key = "in_seq"
    else:
        with c1:
            io_seq = _txt(f"{prefix}_out_seq", "출고순번", str(defaults.get("out_seq", "")))
        io_seq_key = "out_seq"

    with c2:
        trans_seq = _txt(f"{prefix}_trans_seq", "거래명세서순번", str(defaults.get("trans_seq", "")))
    with c3:
        tax_seq = _txt(f"{prefix}_tax_seq", "세금계산서순번", str(defaults.get("tax_seq", "")))

    return {
        io_seq_key: io_seq,
        "trans_seq": trans_seq,
        "tax_seq": tax_seq,
    }

def _render_inout_form(
    *,
    prefix: str,
    form_key: str,
    caption_text: str,
    defaults: Dict[str, Any],
    io_kind: str,
) -> Dict[str, Any]:
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

    @st.cache_data(ttl=300, show_spinner=False)
    def _search_maker_candidates(keyword: str) -> list[dict[str, str]]:
        kw = str(keyword or "").strip()
        if not kw:
            return []

        sql = """
SELECT TOP 100
    LTRIM(RTRIM(Rd03_Ven_Cd)) AS ven_cd,
    LTRIM(RTRIM(Rd03_Ven_Nm)) AS ven_nm
FROM dbo.Rddbc030 WITH (NOLOCK)
WHERE Rd03_Ven_Cd >= '10001'
  AND Rd03_Ven_Cd <= '18999'
  AND LTRIM(RTRIM(Rd03_Ven_Nm)) LIKE ?
ORDER BY LTRIM(RTRIM(Rd03_Ven_Nm)), LTRIM(RTRIM(Rd03_Ven_Cd))
""".strip()

        try:
            df = read_df(sql, (f"%{kw}%",))
        except Exception:
            return []

        if df is None or df.empty:
            return []

        out: list[dict[str, str]] = []
        for _, row in df.iterrows():
            ven_cd = str(row.get("ven_cd") or "").strip()
            ven_nm = str(row.get("ven_nm") or "").strip()
            if not ven_cd and not ven_nm:
                continue
            out.append({"ven_cd": ven_cd, "ven_nm": ven_nm})
        return out

    def _pick_index(options: list[str], value: Any) -> int:
        v = str(value or "").strip()
        if not v:
            return 0
        try:
            return options.index(v)
        except ValueError:
            return 0

    def _parse_yyyymmdd(value: Any):
        s = str(value or "").strip()
        digits = "".join(ch for ch in s if ch.isdigit())
        if len(digits) != 8:
            return None
        try:
            return datetime.strptime(digits, "%Y%m%d").date()
        except Exception:
            return None

    def _to_yyyymmdd(value: Any) -> str:
        if isinstance(value, datetime):
            return value.strftime("%Y%m%d")
        if isinstance(value, date):
            return value.strftime("%Y%m%d")
        return ""

    today = date.today()
    month_first = today.replace(day=1)

    default_date_from = _parse_yyyymmdd(defaults.get("date_from")) or month_first
    default_date_to = _parse_yyyymmdd(defaults.get("date_to")) or today

    group_options = _load_code_name_options("0013")
    di_options = _load_code_name_options("0004")
    class_options = _load_code_name_options("0028")

    if io_kind == "in":
        io_gu_options = [
            ("전체", ""),
            ("정상입고", "0"),
            ("입고반품", "1"),
            ("장부입고", "2"),
            ("미결입고", "3"),
            ("기타입고", "4"),
        ]
    else:
        io_gu_options = [
            ("전체", ""),
            ("정상출고", "5"),
            ("출고반품", "6"),
            ("장부출고", "7"),
            ("미결출고", "8"),
            ("기타출고", "9"),
        ]

    io_gu_prefix_default = str(defaults.get("io_gu_prefix", "")).strip()
    io_gu_labels = [label for label, _ in io_gu_options]
    io_gu_label_to_value = {label: value for label, value in io_gu_options}

    default_io_gu_idx = 0
    if io_gu_prefix_default:
        for i, (_, value) in enumerate(io_gu_options):
            if value == io_gu_prefix_default:
                default_io_gu_idx = i
                break

    maker_prefix = f"{prefix}_maker"
    maker_candidates_key = f"{maker_prefix}_candidates"
    maker_selected_cd_key = f"{maker_prefix}_selected_cd"
    maker_selected_nm_key = f"{maker_prefix}_selected_nm"
    maker_pick_key = f"{maker_prefix}_candidate_pick"

    maker_pick_reset_pending_key = f"{maker_prefix}_candidate_pick_reset_pending"

    # 공용 거래처 sync 패턴 사용
    _apply_vendor_input_sync_if_pending(maker_prefix)
    _maybe_reset_vendor_candidate_state(maker_prefix)

    selected_maker_cd = str(
        st.session_state.get(maker_selected_cd_key, defaults.get("product_ven_cd", ""))
    ).strip()
    selected_maker_nm = str(
        st.session_state.get(maker_selected_nm_key, defaults.get("product_ven_nm", ""))
    ).strip()

    maker_candidates = st.session_state.get(maker_candidates_key, [])
    if not isinstance(maker_candidates, list):
        maker_candidates = []

    maker_vendor_search = False
    maker_choice = "선택하세요"

    if st.session_state.get(maker_pick_reset_pending_key):
        st.session_state.pop(maker_pick_key, None)
        st.session_state[maker_pick_reset_pending_key] = False

    with st.form(form_key, clear_on_submit=False, enter_to_submit=False):
        st.caption(caption_text)

        # 1줄: 시작일자 / 종료일자 / 입고(출고)순번 / 거래명세서순번 / 입출고구분 / 세금계산서순번 / 재고위치
        c1, c2, c3, c4, c5, c6, c7 = st.columns([1.15, 1.15, 0.9, 1.0, 1.0, 1.0, 2.0])

        with c1:
            date_from_value = _render_date_input_with_week(
                key=f"{prefix}_date_from",
                label="시작일자",
                value=default_date_from,
            )

        with c2:
            date_to_value = _render_date_input_with_week(
                key=f"{prefix}_date_to",
                label="종료일자",
                value=default_date_to,
            )

        if io_kind == "in":
            with c3:
                io_seq = _txt(f"{prefix}_in_seq", "입고순번", str(defaults.get("in_seq", "")))
            io_seq_key = "in_seq"
        else:
            with c3:
                io_seq = _txt(f"{prefix}_out_seq", "출고순번", str(defaults.get("out_seq", "")))
            io_seq_key = "out_seq"

        with c4:
            io_gu_label = st.selectbox(
                "입출고구분",
                options=io_gu_labels,
                index=default_io_gu_idx,
                key=f"{prefix}_io_gu_prefix",
            )
            io_gu_prefix = io_gu_label_to_value.get(io_gu_label, "")
        with c5:
            trans_seq = _txt(f"{prefix}_trans_seq", "거래명세서순번", str(defaults.get("trans_seq", "")))
        with c6:
            tax_seq = _txt(f"{prefix}_tax_seq", "세금계산서순번", str(defaults.get("tax_seq", "")))
        with c7:
            stock_info = _render_stock_multiselect(
                prefix,
                label="재고위치",
                default_codes=_default_stock_codes(defaults),
            )


        ven_cd, ven_nm, vendor_search = _render_vendor_candidate_row(prefix)
        physic_cd, physic_nm, product_search = _render_product_candidate_row(prefix)

        # 2줄: 제약사 / 제약사 후보선택 / 제품그룹명 / 제품구분명 / 제품분류명
        c7, c7b, c8, c9, c10 = st.columns([2.0, 1.5, 1.0, 1.0, 1.0])

        with c7:
            mc1, mc2 = st.columns([4, 1])
            with mc1:
                maker_ven_nm_key = f"{maker_prefix}_ven_nm"
                if maker_ven_nm_key in st.session_state:
                    product_ven_nm = st.text_input(
                        "제약사",
                        key=maker_ven_nm_key,
                    )
                else:
                    product_ven_nm = st.text_input(
                        "제약사",
                        value=selected_maker_nm,
                        key=maker_ven_nm_key,
                    )
            with mc2:
                st.caption("")
                maker_vendor_search = st.form_submit_button(
                    "제약사 후보",
                    width="stretch",
                )

        with c7b:
            maker_options = ["선택하세요"] + [
                f"{row.get('ven_cd', '').strip()} | {row.get('ven_nm', '').strip()}"
                for row in maker_candidates
                if str(row.get("ven_cd", "")).strip() or str(row.get("ven_nm", "")).strip()
            ]

            default_maker_idx = 0
            if selected_maker_cd and selected_maker_nm:
                target = f"{selected_maker_cd} | {selected_maker_nm}"
                try:
                    default_maker_idx = maker_options.index(target)
                except ValueError:
                    default_maker_idx = 0

            maker_choice = st.selectbox(
                "제약사 후보선택",
                options=maker_options,
                index=default_maker_idx,
                key=maker_pick_key,
            )

        with c8:
            group_name_sel = st.selectbox(
                "제품그룹명",
                options=group_options,
                index=_pick_index(group_options, defaults.get("product_group_nm", "")),
                key=f"{prefix}_product_group_nm",
            )

        with c9:
            di_name_sel = st.selectbox(
                "제품구분명",
                options=di_options,
                index=_pick_index(di_options, defaults.get("product_di_nm", "")),
                key=f"{prefix}_product_di_nm",
            )

        with c10:
            class_name_sel = st.selectbox(
                "제품분류명",
                options=class_options,
                index=_pick_index(class_options, defaults.get("product_class_nm", "")),
                key=f"{prefix}_product_class_nm",
            )

        # 3줄: 조회건수 / 거래명세서 불일치 / 세금계산서 불일치
        c11, c12, c13 = st.columns([1.0, 1.2, 1.2])
        with c11:
            top = _top_value(f"{prefix}_top", int(defaults.get("top", 200)))
        with c12:
            only_mismatch_trans = _checkbox_yn(
                f"{prefix}_only_mismatch_trans",
                "거래명세서 불일치만 조회",
            )
        with c13:
            only_mismatch_tax = _checkbox_yn(
                f"{prefix}_only_mismatch_tax",
                "세금계산서 불일치만 조회",
            )

        chosen_maker_cd = ""
        chosen_maker_nm = str(product_ven_nm or "").strip()

        if maker_choice != "선택하세요":
            try:
                chosen_maker_cd, chosen_maker_nm = [x.strip() for x in maker_choice.split("|", 1)]
            except ValueError:
                chosen_maker_cd, chosen_maker_nm = "", str(product_ven_nm or "").strip()

        p = {
            "date_from": _to_yyyymmdd(date_from_value),
            "date_to": _to_yyyymmdd(date_to_value),
            io_seq_key: io_seq,
            "trans_seq": trans_seq,
            "tax_seq": tax_seq,
            "top": top,
            "ven_cd": ven_cd,
            "io_gu_prefix": io_gu_prefix,
            "ven_nm": ven_nm,
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
            "only_mismatch_trans": only_mismatch_trans,
            "only_mismatch_tax": only_mismatch_tax,
        }
        p.update(_audit_inputs(prefix))

        submitted = st.form_submit_button(
            "조회",
            type="primary",
            width="stretch",
            on_click=_trigger_panel_run,
        )



    if maker_vendor_search:
        found = _search_maker_candidates(product_ven_nm)
        st.session_state[maker_candidates_key] = found

        # 새로 후보검색하면 이전 선택은 반드시 초기화
        st.session_state[maker_selected_cd_key] = ""
        st.session_state[maker_selected_nm_key] = ""
        st.session_state[maker_pick_reset_pending_key] = True

        _rerun_panel_for_inner_submit()

    if maker_choice != "선택하세요":
        try:
            picked_cd, picked_nm = [x.strip() for x in maker_choice.split("|", 1)]
        except ValueError:
            picked_cd, picked_nm = "", ""
        st.session_state[maker_selected_cd_key] = picked_cd
        st.session_state[maker_selected_nm_key] = picked_nm

    return {
        "params": p,
        "submitted": submitted,
        "vendor_search": vendor_search,
        "product_search": product_search,
    }


def _run_inout_submit(
    *,
    title: str,
    payload_key: str,
    prefix: str,
    defaults: Dict[str, Any],
    p: Dict[str, Any],
    seq_keys: list[str],
    alias_map: Dict[str, list[str]],
    service_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
) -> Dict[str, Any]:
    payload_fresh_key = f"{payload_key}_fresh"

    def _text_payload(message: str, final: bool = False) -> Dict[str, Any]:
        msg = str(message or "").strip()
        payload = {
            "title": title,
            "action": title,
            "params": dict(p),
            "data": msg,
            "message": msg,
            "df": pd.DataFrame(),
            "df_display": pd.DataFrame(),
            "meta": {
                "row_count": 0,
            },
            "final": final,
        }
        st.session_state[payload_key] = payload
        st.session_state[payload_fresh_key] = True
        return payload

    if _needs_vendor_pick(prefix, p):
        return _text_payload("거래처 후보를 선택한 뒤 [조회] 버튼을 누르세요.", final=False)

    if _needs_product_pick(prefix, p):
        return _text_payload("제품 후보를 선택한 뒤 [조회] 버튼을 누르세요.", final=False)

    maker_prefix = f"{prefix}_maker"
    maker_candidates_key = f"{maker_prefix}_candidates"
    maker_pick_key = f"{maker_prefix}_candidate_pick"
    maker_selected_cd_key = f"{maker_prefix}_selected_cd"
    maker_selected_nm_key = f"{maker_prefix}_selected_nm"
    maker_input_key = f"{maker_prefix}_ven_nm"

    raw_ven_cd = str(p.get("ven_cd", "")).strip()
    raw_ven_nm = str(p.get("ven_nm", "")).strip()
    raw_physic_cd = str(p.get("physic_cd", "")).strip()
    raw_physic_nm = str(p.get("physic_nm", "")).strip()
    raw_maker_nm = str(p.get("product_ven_nm", "")).strip()

    maker_candidates = st.session_state.get(maker_candidates_key, [])
    if not isinstance(maker_candidates, list):
        maker_candidates = []

    maker_choice = str(st.session_state.get(maker_pick_key, "선택하세요") or "선택하세요").strip()
    current_maker_input_nm = str(st.session_state.get(maker_input_key, raw_maker_nm)).strip()

    if current_maker_input_nm and maker_candidates and maker_choice == "선택하세요":
        return _text_payload("제약사 후보를 선택한 뒤 [조회] 버튼을 누르세요.", final=False)

    if maker_choice != "선택하세요":
        try:
            picked_maker_cd, picked_maker_nm = [x.strip() for x in maker_choice.split("|", 1)]
        except ValueError:
            picked_maker_cd, picked_maker_nm = "", ""

        if picked_maker_cd or picked_maker_nm:
            p["product_ven_cd"] = picked_maker_cd
            p["product_ven_nm"] = picked_maker_nm

    p = _apply_vendor_pick(prefix, p)
    p = _apply_product_pick(prefix, p)

    stock_cds = [str(x).strip() for x in p.get("stock_cds", []) if str(x).strip()]

    requested_filters = {
        "ven_cd": str(p.get("ven_cd", "")).strip(),
        "ven_nm": str(p.get("ven_nm", "")).strip(),
        "physic_cd": str(p.get("physic_cd", "")).strip(),
        "physic_nm": str(p.get("physic_nm", "")).strip(),
        "product_ven_cd": str(p.get("product_ven_cd", "")).strip(),
        "product_ven_nm": str(p.get("product_ven_nm", "")).strip(),
        "product_group_nm": str(p.get("product_group_nm", "")).strip(),
        "product_di_nm": str(p.get("product_di_nm", "")).strip(),
        "product_class_nm": str(p.get("product_class_nm", "")).strip(),
        "stock_cd": str(p.get("stock_cd", "")).strip(),
        "stock_nm": str(p.get("stock_nm", "")).strip(),
    }
    for key in seq_keys:
        requested_filters[key] = str(p.get(key, "")).strip()

    service_p, stock_code_candidates = _prepare_io_service_params(
        base_params=p,
        requested_filters=requested_filters,
        seq_keys=seq_keys,
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

    _clear_payload_key(payload_key)

    payload = service_fn(final_params)
    payload = _finalize_io_payload(
        payload=payload,
        requested_filters=requested_filters,
        final_params=final_params,
        title=title,
        alias_map=alias_map,
        contains_keys={"ven_nm", "physic_nm", "product_ven_nm", "stock_nm"},
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

    # 화면 조회건수와 전체 원본 DF를 분리한다.
    # 화면은 사용자가 입력한 top만 표시하고,
    # LLM/다운로드/현재표 후속분석은 전체 export DF를 기준으로 한다.
    display_top = _display_top_from_params(p, default=200)

    payload = _attach_inout_full_df(
        payload=payload,
        title=title,
        final_params=final_params,
        requested_filters=requested_filters,
        alias_map=alias_map,
        contains_keys={"ven_nm", "physic_nm", "product_ven_nm", "stock_nm"},
        stock_code_candidates=stock_code_candidates,
        stock_cds=stock_cds,
        display_top=display_top,
    )

    if isinstance(payload, dict):
        try:
            cond_text = _build_inout_condition_text(
                title,
                final_params,
                display_top=display_top,
            )

            meta = payload.setdefault("meta", {})
            meta["query_summary"] = cond_text
            meta["condition"] = cond_text
            meta["display_top"] = int(display_top)

            payload.setdefault("params", {}).setdefault("display_top", int(display_top))

            if "검증" in str(title):
                meta["analysis_type"] = "io_validation"
                meta["validation_action"] = str(title)
        except Exception:
            pass

    if isinstance(payload, dict):
        data_text = str(payload.get("data", "") or "").strip()
        msg_text = str(payload.get("message", "") or "").strip()
        if data_text and not msg_text:
            payload["message"] = data_text
        if (
            not data_text
            and not msg_text
            and isinstance(payload.get("meta"), dict)
            and int(payload.get("meta", {}).get("row_count", 0) or 0) == 0
        ):
            payload["data"] = "조회 결과가 없습니다."
            payload["message"] = "조회 결과가 없습니다."
            payload["final"] = True

    st.session_state[payload_key] = payload
    st.session_state[payload_fresh_key] = True

    st.session_state[f"{prefix}_vendor_reset_pending"] = True
    st.session_state[f"{prefix}_product_reset_pending"] = True

    vendor_sync_needed = (final_ven_cd != raw_ven_cd) or (final_ven_nm != raw_ven_nm)
    product_sync_needed = (final_physic_cd != raw_physic_cd) or (final_physic_nm != raw_physic_nm)

    if vendor_sync_needed:
        _queue_vendor_input_sync(prefix, final_ven_cd, final_ven_nm)

    if product_sync_needed:
        _queue_product_input_sync(prefix, final_physic_cd, final_physic_nm)

    maker_sync_needed = False
    if final_maker_cd or final_maker_nm:
        st.session_state[maker_selected_cd_key] = final_maker_cd
        st.session_state[maker_selected_nm_key] = final_maker_nm

        maker_sync_needed = bool(final_maker_nm) and (current_maker_input_nm != final_maker_nm)

        if maker_sync_needed:
            _queue_vendor_input_sync(maker_prefix, final_maker_cd, final_maker_nm)

    if vendor_sync_needed or product_sync_needed or maker_sync_needed:
        _rerun_panel_for_inner_submit()

    return payload

def _view_inout(
    *,
    title: str,
    payload_key: str,
    prefix: str,
    form_key: str,
    caption_text: str,
    vendor_scope: str,
    io_kind: str,
    seq_keys: list[str],
    alias_map: Dict[str, list[str]],
    service_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
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
        elif "없습니다" in msg:
            st.info(msg)
        else:
            st.info(msg)

    _apply_vendor_input_sync_if_pending(prefix)
    _apply_product_input_sync_if_pending(prefix)

    _maybe_reset_vendor_candidate_state(prefix)
    _maybe_reset_product_candidate_state(prefix)

    form_state = _render_inout_form(
        prefix=prefix,
        form_key=form_key,
        caption_text=caption_text,
        defaults=defaults,
        io_kind=io_kind,
    )

    p = form_state["params"]
    submitted = bool(form_state["submitted"])
    vendor_search = bool(form_state["vendor_search"])
    product_search = bool(form_state["product_search"])

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

    _handle_candidate_search(
        prefix=prefix,
        p=p,
        vendor_search=vendor_search,
        product_search=product_search,
        vendor_scope=vendor_scope,
    )

    if submitted:
        payload = _run_inout_submit(
            title=title,
            payload_key=payload_key,
            prefix=prefix,
            defaults=defaults,
            p=p,
            seq_keys=seq_keys,
            alias_map=alias_map,
            service_fn=service_fn,
        )
        _show_text_payload(payload)
        return payload

    prompt_payload = {
        "title": title,
        "action": title,
        "params": {},
        "data": "조회 조건을 입력한 뒤 [조회] 버튼을 누르세요.",
        "message": "조회 조건을 입력한 뒤 [조회] 버튼을 누르세요.",
        "final": False,
    }

    if payload_key not in st.session_state:
        _show_text_payload(prompt_payload)
        return prompt_payload

    payload_fresh_key = f"{payload_key}_fresh"

    # 후보검색/내부 rerun에서는
    # '방금 조회 버튼으로 만든 payload'만 1회 보여주고,
    # 나머지는 이전 조회결과를 다시 재생하지 않는다.
    if bool(st.session_state.get("__sims_inner_submit", False)):
        if st.session_state.pop(payload_fresh_key, False):
            payload = st.session_state[payload_key]
            _show_text_payload(payload)
            return payload
        _show_text_payload(prompt_payload)
        return prompt_payload

    payload = st.session_state[payload_key]
    _show_text_payload(payload)
    return payload

# ==========================================================================
# view_rddbc110
# ==========================================================================
def view_rddbc110(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _view_inout(
        title="입고명세 조회",
        payload_key="__io110_last_payload",
        prefix="__io110",
        form_key="__io110_form",
        caption_text="조회조건 · 입고명세",
        vendor_scope="purchase",
        io_kind="in",
        seq_keys=["in_seq", "trans_seq", "tax_seq"],
        alias_map=_IO110_LOCAL_FILTER_ALIAS_MAP,
        service_fn=get_rddbc110_result,
        params=params,
    )


# ------------------------------------------------------------
# view_rddbc120
# ------------------------------------------------------------
def view_rddbc120(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _view_inout(
        title="출고명세 조회",
        payload_key="__io120_last_payload",
        prefix="__io120",
        form_key="__io120_form",
        caption_text="조회조건 · 출고명세",
        vendor_scope="sales",
        io_kind="out",
        seq_keys=["out_seq", "trans_seq", "tax_seq"],
        alias_map=_IO120_LOCAL_FILTER_ALIAS_MAP,
        service_fn=get_rddbc120_result,
        params=params,
    )
