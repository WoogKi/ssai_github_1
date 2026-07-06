# app/sims/views/rddbc_io_doc_views.py

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

import streamlit as st
from datetime import date, datetime
from app.db.mssql_client import read_df

import pandas as pd

from app.services.rddbc130_service import get_rddbc130_result, get_rddbc130_export_df
from app.services.rddbc140_service import get_rddbc140_result, get_rddbc140_export_df

from app.sims.views.rddbc_io_shared import (
    _apply_vendor_input_sync_if_pending,
    _apply_vendor_pick,
    _audit_inputs,
    _clear_payload_key,
    _finalize_io_payload,
    _maybe_reset_vendor_candidate_state,
    _needs_vendor_pick,
    _prepare_io_service_params,
    _queue_vendor_input_sync,
    _render_io_date_range,
    _render_vendor_candidate_row,
    _rerun_panel_for_inner_submit,
    _store_vendor_candidates,
    _top_value,
    _trigger_panel_run,
    _txt,
)

_IO130_LOCAL_FILTER_ALIAS_MAP: Dict[str, list[str]] = {
    "ven_cd": [
        "거래처코드", "매입처코드", "매출처코드",
        "VEN_CD", "ven_cd",
        "Rd13_Ven_Cd", "RD13_VEN_CD",
    ],
    "ven_nm": [
        "거래처명", "매입처명", "매출처명",
        "VEN_NM", "ven_nm",
        "Rd13_Ven_Nm", "RD13_VEN_NM",
    ],
    "trans_di": [
        "거래명세서구분",
        "TRANS_DI", "trans_di",
        "Rd13_Trans_Di", "RD13_TRANS_DI",
    ],
    "trans_seq": [
        "거래명세서순번", "거래명세순번", "명세서순번",
        "TRANS_SEQ", "trans_seq",
        "Rd13_Trans_Seq", "RD13_TRANS_SEQ",
    ],
}

_IO140_LOCAL_FILTER_ALIAS_MAP: Dict[str, list[str]] = {
    "ven_cd": [
        "거래처코드", "매입처코드", "매출처코드",
        "VEN_CD", "ven_cd",
        "Rd14_Ven_Cd", "RD14_VEN_CD",
    ],
    "ven_nm": [
        "거래처명", "매입처명", "매출처명",
        "VEN_NM", "ven_nm",
        "Rd14_Ven_Nm", "RD14_VEN_NM",
    ],
    "tax_di": [
        "세금계산서구분",
        "TAX_DI", "tax_di",
        "Rd14_Tax_Di", "RD14_TAX_DI",
    ],
    "tax_seq": [
        "세금계산서순번", "세금계산순번", "계산서순번",
        "TAX_SEQ", "tax_seq",
        "Rd14_Tax_Seq", "RD14_TAX_SEQ",
    ],
}


def _resolve_doc_vendor_scope(di_key: str, di_value: str) -> str:
    v = str(di_value or "").strip()

    if di_key == "trans_di":
        if v == "1":
            return "purchase"
        if v == "3":
            return "sales"
        return "all"

    if di_key == "tax_di":
        if v == "1":
            return "purchase"
        if v == "2":
            return "account_purchase"
        if v == "3":
            return "sales"
        if v == "4":
            return "account_sales"
        return "all"

    return "all"


def _force_payload_title(payload: Dict[str, Any], title: str) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return payload

    payload["title"] = title
    payload["action"] = title

    if not isinstance(payload.get("params"), dict):
        payload["params"] = {}

    return payload

def _render_doc_form(
    *,
    prefix: str,
    form_key: str,
    caption_text: str,
    defaults: Dict[str, Any],
    di_key: str,
    di_label: str,
    seq_key: str,
    seq_label: str,
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

    def _week_label_52(value: Any) -> str:
        if isinstance(value, date):
            d = value
        else:
            s = str(value or "").strip()
            digits = "".join(ch for ch in s if ch.isdigit())
            if len(digits) != 8:
                return ""
            try:
                d = datetime.strptime(digits, "%Y%m%d").date()
            except Exception:
                return ""

        weekday_map = ["월", "화", "수", "목", "금", "토", "일"]
        week_no = ((d.timetuple().tm_yday - 1) // 7) + 1
        if week_no < 1:
            week_no = 1
        if week_no > 52:
            week_no = 52
        return f"{weekday_map[d.weekday()]} / {week_no}주"

    st.caption(caption_text)

    today = date.today()
    month_first = today.replace(day=1)

    default_date_from = _parse_yyyymmdd(defaults.get("date_from")) or month_first
    default_date_to = _parse_yyyymmdd(defaults.get("date_to")) or today

    ven_group_options = _load_code_name_options("0019")
    ven_kind_options = _load_code_name_options("0009")

    if di_key == "trans_di":
        di_labels = ["전체", "매입", "매출"]
        di_label_to_value = {
            "전체": "",
            "매입": "1",
            "매출": "3",
        }
    elif di_key == "tax_di":
        di_labels = ["전체", "매입", "회계매입", "매출", "회계매출"]
        di_label_to_value = {
            "전체": "",
            "매입": "1",
            "회계매입": "2",
            "매출": "3",
            "회계매출": "4",
        }
    else:
        di_labels = ["전체"]
        di_label_to_value = {"전체": ""}

    di_value_to_label = {v: k for k, v in di_label_to_value.items()}
    default_di_value = str(defaults.get(di_key, "")).strip()
    default_di_label = di_value_to_label.get(default_di_value, "전체")

    current_di_label = str(
        st.session_state.get(f"{prefix}_{di_key}", default_di_label)
    ).strip() or "전체"
    if current_di_label not in di_labels:
        current_di_label = "전체"

    current_di_value = di_label_to_value.get(current_di_label, "")

    io_gu_label_to_value = {
        "전체": "",
        "정상입고": "0",
        "입고반품": "1",
        "장부입고": "2",
        "미결입고": "3",
        "기타입고": "4",
        "정상출고": "5",
        "출고반품": "6",
        "장부출고": "7",
        "미결출고": "8",
        "기타출고": "9",
    }
    io_gu_value_to_label = {v: k for k, v in io_gu_label_to_value.items()}

    if di_key == "trans_di":
        if current_di_value == "1":
            io_gu_options = ["전체", "정상입고", "입고반품", "장부입고", "미결입고", "기타입고"]
        elif current_di_value == "3":
            io_gu_options = ["전체", "정상출고", "출고반품", "장부출고", "미결출고", "기타출고"]
        else:
            io_gu_options = [
                "전체",
                "정상입고", "입고반품", "장부입고", "미결입고", "기타입고",
                "정상출고", "출고반품", "장부출고", "미결출고", "기타출고",
            ]
    elif di_key == "tax_di":
        if current_di_value in {"1", "2"}:
            io_gu_options = ["전체", "정상입고", "입고반품", "장부입고", "미결입고", "기타입고"]
        elif current_di_value in {"3", "4"}:
            io_gu_options = ["전체", "정상출고", "출고반품", "장부출고", "미결출고", "기타출고"]
        else:
            io_gu_options = [
                "전체",
                "정상입고", "입고반품", "장부입고", "미결입고", "기타입고",
                "정상출고", "출고반품", "장부출고", "미결출고", "기타출고",
            ]
    else:
        io_gu_options = ["전체"]

    default_io_gu_label = str(defaults.get("io_gu_label", "")).strip()
    if not default_io_gu_label:
        default_io_gu_label = io_gu_value_to_label.get(
            str(defaults.get("io_gu_prefix", "")).strip(),
            "전체",
        )

    current_io_gu_label = str(
        st.session_state.get(f"{prefix}_io_gu_label", default_io_gu_label)
    ).strip() or "전체"
    if current_io_gu_label not in io_gu_options:
        current_io_gu_label = "전체"

    vendor_candidates_key = f"{prefix}_vendor_candidates"
    vendor_pick_key = f"{prefix}_vendor_pick"
    vendor_msg_key = f"{prefix}_vendor_msg"

    vendor_rows = st.session_state.get(vendor_candidates_key, []) or []
    if not isinstance(vendor_rows, list):
        vendor_rows = []

    vendor_options = ["선택하세요"] + [
        f"{cd} | {nm}"
        for cd, nm in vendor_rows
        if str(cd).strip() or str(nm).strip()
    ]

    current_vendor_pick = str(
        st.session_state.get(vendor_pick_key, "선택하세요") or "선택하세요"
    ).strip()
    if current_vendor_pick not in vendor_options:
        st.session_state[vendor_pick_key] = "선택하세요"

    with st.form(form_key, clear_on_submit=False, enter_to_submit=False):
        # 1줄 고정
        c1, c2, c3, c4, c5, c6 = st.columns([1.0, 1.0, 1.15, 1.15, 1.0, 0.8])

        with c1:
            di_label_live = st.selectbox(
                di_label,
                options=di_labels,
                index=_pick_index(di_labels, current_di_label),
                key=f"{prefix}_{di_key}",
            )
            di_value = di_label_to_value.get(di_label_live, "")

        with c2:
            io_gu_label = st.selectbox(
                "입출고구분",
                options=io_gu_options,
                index=_pick_index(io_gu_options, current_io_gu_label),
                key=f"{prefix}_io_gu_label",
            )
            io_gu_prefix = io_gu_label_to_value.get(io_gu_label, "")

        with c3:
            date_from_value = st.date_input(
                "시작일자",
                value=default_date_from,
                key=f"{prefix}_date_from",
            )
            st.caption(_week_label_52(date_from_value))

        with c4:
            date_to_value = st.date_input(
                "종료일자",
                value=default_date_to,
                key=f"{prefix}_date_to",
            )
            st.caption(_week_label_52(date_to_value))

        with c5:
            seq_value = _txt(
                f"{prefix}_{seq_key}",
                seq_label,
                str(defaults.get(seq_key, "")),
            )

        with c6:
            top = _top_value(f"{prefix}_top", int(defaults.get("top", 200)))

        c7, c8, c9, c10, c11, c12 = st.columns([1.4, 2.6, 1.0, 2.6, 1.6, 1.6])

        with c7:
            ven_cd = st.text_input(
                "거래처코드",
                value=str(st.session_state.get(f"{prefix}_ven_cd", defaults.get("ven_cd", ""))),
                key=f"{prefix}_ven_cd",
            ).strip()

        with c8:
            ven_nm = st.text_input(
                "거래처명",
                value=str(st.session_state.get(f"{prefix}_ven_nm", defaults.get("ven_nm", ""))),
                key=f"{prefix}_ven_nm",
            ).strip()

        with c9:
            vendor_search = st.form_submit_button(
                "후보검색",
                use_container_width=True,
            )

        with c10:
            st.selectbox(
                "거래처 후보선택",
                options=vendor_options,
                key=vendor_pick_key,
            )

        with c11:
            ven_group_nm_sel = st.selectbox(
                "거래처그룹",
                options=ven_group_options,
                index=_pick_index(
                    ven_group_options,
                    st.session_state.get(f"{prefix}_ven_group_nm", defaults.get("ven_group_nm", "")),
                ),
                key=f"{prefix}_ven_group_nm",
            )

        with c12:
            ven_kind_nm_sel = st.selectbox(
                "거래처종류",
                options=ven_kind_options,
                index=_pick_index(
                    ven_kind_options,
                    st.session_state.get(f"{prefix}_ven_kind_nm", defaults.get("ven_kind_nm", "")),
                ),
                key=f"{prefix}_ven_kind_nm",
            )

        vendor_msg = str(st.session_state.get(vendor_msg_key, "") or "").strip()
        if vendor_msg:
            st.caption(vendor_msg)

        submitted = st.form_submit_button(
            "조회",
            type="primary",
            use_container_width=True,
            on_click=_trigger_panel_run,
        )

    p = {
        "date_from": _to_yyyymmdd(date_from_value),
        "date_to": _to_yyyymmdd(date_to_value),
        "ven_cd": ven_cd,
        "ven_nm": ven_nm,
        di_key: di_value,
        seq_key: seq_value,
        "io_gu_prefix": io_gu_prefix,
        "io_gu_label": io_gu_label,
        "ven_group_nm": "" if ven_group_nm_sel == "전체" else str(ven_group_nm_sel).strip(),
        "ven_kind_nm": "" if ven_kind_nm_sel == "전체" else str(ven_kind_nm_sel).strip(),
        "top": top,
    }
    p.update(_audit_inputs(prefix))

    return {
        "params": p,
        "submitted": submitted,
        "vendor_search": vendor_search,
    }

def _display_top_from_params(params: Dict[str, Any], default: int = 200) -> int:
    try:
        top = int(params.get("display_top") or params.get("top") or default)
        return top if top > 0 else default
    except Exception:
        return default


def _attach_doc_full_df(
    *,
    payload: Dict[str, Any],
    title: str,
    final_params: Dict[str, Any],
    requested_filters: Dict[str, str],
    alias_map: Dict[str, list[str]],
    display_top: int,
) -> Dict[str, Any]:
    """
    거래명세서/세금계산서 공통:
    - 화면 표시: display_top 건
    - 원본/다운로드/LLM/현재표: export 전체 DF
    """
    if not isinstance(payload, dict):
        return payload

    try:
        if title == "거래명세서 공통 조회":
            export_df = get_rddbc130_export_df(final_params)
        elif title == "세금계산서 공통 조회":
            export_df = get_rddbc140_export_df(final_params)
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
            contains_keys={"ven_nm"},
            stock_code_candidates=[],
            stock_code_key="__unused_stock_cd__",
            stock_name_key="__unused_stock_nm__",
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

        try:
            import logging
            logging.getLogger("ssai").info(
                "[io.doc.full] action=%s display_rows=%s full_rows=%s display_top=%s",
                title,
                len(display_df),
                len(full_df),
                display_top,
            )
        except Exception:
            pass

    except Exception:
        try:
            import logging
            logging.getLogger("ssai").exception("[io.doc.full] attach full df failed action=%s", title)
        except Exception:
            pass

    return payload


#/ 조회버튼 눌렀을 때 실행되는 함수
def _run_doc_submit(
    *,
    title: str,
    payload_key: str,
    prefix: str,
    defaults: Dict[str, Any],
    p: Dict[str, Any],
    di_key: str,
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
            "final": False,
        }

    raw_ven_cd = str(p.get("ven_cd", "")).strip()
    raw_ven_nm = str(p.get("ven_nm", "")).strip()

    p = _apply_vendor_pick(prefix, p)

    requested_filters = {
        "ven_cd": str(p.get("ven_cd", "")).strip(),
        "ven_nm": str(p.get("ven_nm", "")).strip(),
        di_key: str(p.get(di_key, "")).strip(),
        seq_key: str(p.get(seq_key, "")).strip(),
    }

    service_p, _ = _prepare_io_service_params(
        base_params=p,
        requested_filters=requested_filters,
        seq_keys=[seq_key],
        stock_name_key="__unused_stock_nm__",
        stock_code_key="__unused_stock_cd__",
        base_top=3000,
        stock_name_top=10000,
    )

    final_ven_cd = str(p.get("ven_cd", "")).strip()
    final_ven_nm = str(p.get("ven_nm", "")).strip()

    final_params = dict(defaults)
    final_params.update(service_p)

    _clear_payload_key(payload_key)

    payload = service_fn(final_params)
    payload = _finalize_io_payload(
        payload=payload,
        requested_filters=requested_filters,
        final_params=final_params,
        title=title,
        alias_map=alias_map,
        contains_keys={"ven_nm"},
        stock_code_candidates=[],
        stock_code_key="__unused_stock_cd__",
        stock_name_key="__unused_stock_nm__",
    )
    payload = _force_payload_title(payload, title)

    # 화면 조회건수와 전체 원본 DF를 분리한다.
    display_top = _display_top_from_params(p, default=200)

    payload = _attach_doc_full_df(
        payload=payload,
        title=title,
        final_params=final_params,
        requested_filters=requested_filters,
        alias_map=alias_map,
        display_top=display_top,
    )    

    st.session_state[payload_key] = payload
    st.session_state[f"{prefix}_vendor_reset_pending"] = True

    vendor_sync_needed = (final_ven_cd != raw_ven_cd) or (final_ven_nm != raw_ven_nm)
    if vendor_sync_needed:
        _queue_vendor_input_sync(prefix, final_ven_cd, final_ven_nm)
        _rerun_panel_for_inner_submit()

    return payload


def _view_rddbc_doc_common(
    *,
    title: str,
    payload_key: str,
    prefix: str,
    di_key: str,
    di_label: str,
    seq_key: str,
    seq_label: str,
    service_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
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
        if hasattr(df_display, "empty") and not df_display.empty:
            return

        df = payload.get("df")
        if hasattr(df, "empty") and not df.empty:
            return

        records = payload.get("records")
        if isinstance(records, list) and len(records) > 0:
            return

        if "후보를 선택" in msg:
            st.warning(msg)
        elif "없습니다" in msg or "0건" in msg:
            st.info(msg)
        else:
            st.info(msg)

    _apply_vendor_input_sync_if_pending(prefix)
    _maybe_reset_vendor_candidate_state(prefix)

    form_state = _render_doc_form(
        prefix=prefix,
        form_key=f"{prefix}_form",
        caption_text=f"조회조건 · {title.replace(' 조회', '')}",
        defaults=defaults,
        di_key=di_key,
        di_label=di_label,
        seq_key=seq_key,
        seq_label=seq_label,
    )

    p = form_state["params"]
    submitted = bool(form_state["submitted"])
    vendor_search = bool(form_state["vendor_search"])

    # 후보검색은 여기서 직접 처리
    if vendor_search:
        vendor_scope = _resolve_doc_vendor_scope(di_key, p.get(di_key, ""))
        _store_vendor_candidates(prefix, p.get("ven_nm", ""), scope=vendor_scope)
        _rerun_panel_for_inner_submit()

    if submitted:
        payload = _run_doc_submit(
            title=title,
            payload_key=payload_key,
            prefix=prefix,
            defaults=defaults,
            p=p,
            di_key=di_key,
            seq_key=seq_key,
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
        return prompt_payload

    payload = _force_payload_title(st.session_state[payload_key], title)
    payload_params = payload.get("params", {}) if isinstance(payload.get("params"), dict) else {}

    current_di = str(p.get(di_key, "")).strip()
    current_io_prefix = str(p.get("io_gu_prefix", "")).strip()
    current_io_label = str(p.get("io_gu_label", "")).strip()

    last_di = str(payload_params.get(di_key, "")).strip()
    last_io_prefix = str(payload_params.get("io_gu_prefix", "")).strip()
    last_io_label = str(payload_params.get("io_gu_label", "")).strip()

    outer_selection_changed = (
        current_di != last_di
        or current_io_prefix != last_io_prefix
        or current_io_label != last_io_label
    )

    # form 밖 선택값만 바뀐 경우:
    # 이전 조회결과를 다시 재생하지도 않고,
    # 추가 안내문도 화면에 그리지 않음
    if outer_selection_changed:
        return {
            "title": title,
            "action": title,
            "params": {},
            "data": "",
            "message": "",
            "final": False,
        }
    _show_text_payload(payload)
    return payload



def view_rddbc130(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _view_rddbc_doc_common(
        title="거래명세서 공통 조회",
        payload_key="__io130_last_payload",
        prefix="__io130",
        di_key="trans_di",
        di_label="거래명세서구분",
        seq_key="trans_seq",
        seq_label="거래명세서순번",
        service_fn=get_rddbc130_result,
        alias_map=_IO130_LOCAL_FILTER_ALIAS_MAP,
        params=params,
    )


def view_rddbc140(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _view_rddbc_doc_common(
        title="세금계산서 공통 조회",
        payload_key="__io140_last_payload",
        prefix="__io140",
        di_key="tax_di",
        di_label="세금계산서구분",
        seq_key="tax_seq",
        seq_label="세금계산서순번",
        service_fn=get_rddbc140_result,
        alias_map=_IO140_LOCAL_FILTER_ALIAS_MAP,
        params=params,
    )
