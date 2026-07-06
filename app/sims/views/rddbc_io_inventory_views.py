# app/sims/views/rddbc_io_inventory_views.py
# 2026/04/19

from __future__ import annotations

from typing import Any, Dict, Optional
import datetime as dt

import pandas as pd
import streamlit as st

from app.services import rddbc010_service as C01
from app.services.product_inventory_service import get_product_inventory_result
from app.sims.views.rddbc_io_shared import (
    _apply_product_input_sync_if_pending,
    _apply_product_pick,
    _apply_vendor_input_sync_if_pending,
    _apply_vendor_pick,
    _clear_payload_key,
    _render_date_input_with_week,
    _maybe_reset_product_candidate_state,
    _maybe_reset_vendor_candidate_state,
    _needs_product_pick,
    _needs_vendor_pick,
    _queue_product_input_sync,
    _queue_vendor_input_sync,
    _rerun_panel_for_inner_submit,
    _store_product_candidates,
    _store_vendor_candidates,
    _top_value,
    _trigger_panel_run,
)

_STOCK_GCODE = "0018"


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _ensure_df(obj: Any) -> pd.DataFrame:
    if obj is None:
        return pd.DataFrame()
    if isinstance(obj, pd.DataFrame):
        return obj.copy()
    try:
        return pd.DataFrame(obj)
    except Exception:
        return pd.DataFrame()


def _norm_series(sr: pd.Series) -> pd.Series:
    return (
        sr.fillna("")
        .astype(str)
        .replace({"None": "", "nan": "", "<NA>": ""})
        .str.strip()
    )


def _pick_col(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _pick_index(options: list[str], value: Any) -> int:
    v = _clean_text(value)
    if not v:
        return 0
    try:
        return options.index(v)
    except ValueError:
        return 0


def _ensure_select_value(key: str, options: list[str], default: str = "") -> None:
    current = _clean_text(st.session_state.get(key, default))
    if current not in options:
        st.session_state[key] = default if default in options else (options[0] if options else "")

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

    if "후보를 목록에서 선택" in msg:
        st.warning(msg)
    elif "없습니다" in msg or "없읍니다" in msg or "0건" in msg:
        st.info(msg)
    else:
        st.info(msg)

# 재고 위치 옵션 로드
# C01의 list_by_group이 있으면 그걸 쓰고, 없으면 search_rows로 대체. 실패하면 전체 하나만.
def _load_stock_options() -> list[tuple[str, str]]:
    try:
        fn = getattr(C01, "list_by_group", None)
        if callable(fn):
            df = _ensure_df(fn(gcode=_STOCK_GCODE, top=2000))
        else:
            df = _ensure_df(C01.search_rows(gcode=_STOCK_GCODE, top=2000, only_active=True))
    except Exception:
        return [("전체", "")]

    if df.empty:
        return [("전체", "")]

    tcol = _pick_col(df, ["Rd01_Tcode", "항목코드", "상세코드"])
    ncol = _pick_col(df, ["Rd01_Hnm", "한글명", "코드명"])
    if not tcol or not ncol:
        return [("전체", "")]

    work = df[[tcol, ncol]].copy()
    work[tcol] = _norm_series(work[tcol])
    work[ncol] = _norm_series(work[ncol])
    work = work[(work[tcol] != "") & (work[ncol] != "")]
    work = work.drop_duplicates(subset=[tcol, ncol]).sort_values([tcol], kind="stable")

    options: list[tuple[str, str]] = [("전체", "")]
    for _, row in work.iterrows():
        code = str(row[tcol]).strip()
        name = str(row[ncol]).strip()
        options.append((f"{name} ({code})", code))
    return options


def _load_code_name_options(gcode: str) -> list[str]:
    try:
        fn = getattr(C01, "list_by_group", None)
        if callable(fn):
            df = _ensure_df(fn(gcode=gcode, top=2000))
        else:
            df = _ensure_df(C01.search_rows(gcode=gcode, top=2000, only_active=True))
    except Exception:
        return ["전체"]

    if df.empty:
        return ["전체"]

    tcol = _pick_col(df, ["Rd01_Tcode", "항목코드", "상세코드"])
    ncol = _pick_col(df, ["Rd01_Hnm", "한글명", "코드명"])
    if not tcol or not ncol:
        return ["전체"]

    work = df[[tcol, ncol]].copy()
    work[tcol] = _norm_series(work[tcol])
    work[ncol] = _norm_series(work[ncol])
    work = work[(work[tcol] != "") & (work[ncol] != "")]
    work = work.drop_duplicates(subset=[ncol]).sort_values([ncol], kind="stable")

    out = ["전체"]
    seen = {"전체"}
    for _, row in work.iterrows():
        nm = str(row[ncol]).strip()
        if nm and nm not in seen:
            out.append(nm)
            seen.add(nm)
    return out

# 날짜 입력값을 가능한 한 유연하게 처리하는 함수. 숫자만 추출해서 8자리면 YYYYMMDD, 6자리면 YYYYMM로 해석. 실패하면 fallback 반환.
def _as_date(value: Any, fallback: dt.date) -> dt.date:
    text = _clean_text(value)
    digits = "".join(ch for ch in text if ch.isdigit())
    try:
        if len(digits) == 8:
            return dt.datetime.strptime(digits, "%Y%m%d").date()
        if len(digits) == 6:
            return dt.datetime.strptime(digits + "01", "%Y%m%d").date()
    except Exception:
        pass
    if isinstance(value, dt.date):
        return value
    return fallback


def _seed_text_input_defaults(prefix: str, defaults: Dict[str, Any]) -> None:
    seed_map = {
        f"{prefix}_physic_cd": defaults.get("physic_cd", ""),
        f"{prefix}_physic_nm": defaults.get("physic_nm", ""),
        f"{prefix}_maker_ven_cd": defaults.get("maker_cd", ""),
        f"{prefix}_maker_ven_nm": defaults.get("maker_nm", ""),
        f"{prefix}_order_ven_cd": defaults.get("order_cd", ""),
        f"{prefix}_order_ven_nm": defaults.get("order_nm", ""),
        f"{prefix}_buy_ven_cd": defaults.get("buy_cd", ""),
        f"{prefix}_buy_ven_nm": defaults.get("buy_nm", ""),
    }
    for k, v in seed_map.items():
        if k not in st.session_state and _clean_text(v):
            st.session_state[k] = _clean_text(v)


def view_product_inventory(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    title = "제품재고현황 조회"
    payload_key = "__io260_last_payload"
    prefix = "__io260"

    defaults = dict(params or {})

    maker_prefix = f"{prefix}_maker"
    order_prefix = f"{prefix}_order"
    buy_prefix = f"{prefix}_buy"

    _seed_text_input_defaults(prefix, defaults)

    _apply_product_input_sync_if_pending(prefix)
    _apply_vendor_input_sync_if_pending(maker_prefix)
    _apply_vendor_input_sync_if_pending(order_prefix)
    _apply_vendor_input_sync_if_pending(buy_prefix)

    _maybe_reset_product_candidate_state(prefix)
    _maybe_reset_vendor_candidate_state(maker_prefix)
    _maybe_reset_vendor_candidate_state(order_prefix)
    _maybe_reset_vendor_candidate_state(buy_prefix)

    stock_options = _load_stock_options()
    stock_label_to_code = {label: code for label, code in stock_options}
    stock_code_to_label = {code: label for label, code in stock_options if code}

    product_group_options = _load_code_name_options("0013")
    product_di_options = _load_code_name_options("0004")
    product_class_options = _load_code_name_options("0001")

    default_stock_codes: list[str] = []
    raw_stock_cds = defaults.get("stock_cds")
    if isinstance(raw_stock_cds, (list, tuple, set)):
        default_stock_codes = [str(x).strip() for x in raw_stock_cds if str(x).strip()]
    else:
        raw_one = _clean_text(defaults.get("stock_cd"))
        if raw_one:
            default_stock_codes = [raw_one]

    default_stock_labels = [
        stock_code_to_label[c]
        for c in default_stock_codes
        if c in stock_code_to_label
    ]

    date_from_default = _as_date(defaults.get("date_from"), dt.date.today() - dt.timedelta(days=30))
    date_to_default = _as_date(defaults.get("date_to"), dt.date.today())

    stock_mode_default = _clean_text(defaults.get("stock_mode") or "실재고")
    group_basis_default = _clean_text(defaults.get("group_basis") or "제조사")
    price_mode_default = _clean_text(defaults.get("price_mode") or "총평균단가")

    product_rows = st.session_state.get(f"{prefix}_product_candidates", []) or []
    if not isinstance(product_rows, list):
        product_rows = []
    product_options = [""] + [
        f"{cd} | {nm}"
        for cd, nm in product_rows
        if _clean_text(cd) or _clean_text(nm)
    ]

    maker_rows = st.session_state.get(f"{maker_prefix}_vendor_candidates", []) or []
    if not isinstance(maker_rows, list):
        maker_rows = []
    maker_options = [""] + [
        f"{cd} | {nm}"
        for cd, nm in maker_rows
        if _clean_text(cd) or _clean_text(nm)
    ]

    order_rows = st.session_state.get(f"{order_prefix}_vendor_candidates", []) or []
    if not isinstance(order_rows, list):
        order_rows = []
    order_options = [""] + [
        f"{cd} | {nm}"
        for cd, nm in order_rows
        if _clean_text(cd) or _clean_text(nm)
    ]

    buy_rows = st.session_state.get(f"{buy_prefix}_vendor_candidates", []) or []
    if not isinstance(buy_rows, list):
        buy_rows = []
    buy_options = [""] + [
        f"{cd} | {nm}"
        for cd, nm in buy_rows
        if _clean_text(cd) or _clean_text(nm)
    ]

    _ensure_select_value(f"{prefix}_product_pick", product_options, "")
    _ensure_select_value(f"{maker_prefix}_vendor_pick", maker_options, "")
    _ensure_select_value(f"{order_prefix}_vendor_pick", order_options, "")
    _ensure_select_value(f"{buy_prefix}_vendor_pick", buy_options, "")

    with st.form("__io260_form", clear_on_submit=False, enter_to_submit=False):
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

        st.caption("조회조건 · 제품재고현황")

        # 1라인
        c1, c2, c3, c4, c5, c6, c7 = st.columns([0.9, 1.0, 1.0, 1.15, 1.15, 2.2, 0.8])

        with c1:
            stock_mode = st.selectbox(
                "재고구분",
                options=["실재고", "장부재고"],
                index=0 if stock_mode_default != "장부재고" else 1,
                key=f"{prefix}_stock_mode",
            )

        with c2:
            group_basis = st.selectbox(
                "재고집계기준",
                options=["제조사", "발주처", "매입처"],
                index={"제조사": 0, "발주처": 1, "매입처": 2}.get(group_basis_default, 0),
                key=f"{prefix}_group_basis",
            )

        with c3:
            price_mode = st.selectbox(
                "재고단가기준",
                options=["총평균단가", "최종매입가", "기준가", "현보험약가"],
                index={"총평균단가": 0, "최종매입가": 1, "기준가": 2, "현보험약가": 3}.get(price_mode_default, 0),
                key=f"{prefix}_price_mode",
            )

        with c4:
            date_from = _render_date_input_with_week(
                key=f"{prefix}_date_from",
                label="시작일자",
                value=date_from_default,
            )

        with c5:
            date_to = _render_date_input_with_week(
                key=f"{prefix}_date_to",
                label="종료일자",
                value=date_to_default,
            )

        with c6:
            selected_stock_labels = st.multiselect(
                "재고위치",
                options=[label for label, _ in stock_options],
                default=default_stock_labels,
                key=f"{prefix}_stock_labels",
                help="전체는 선택하지 않거나 '전체'만 선택하면 됩니다.",
            )

        with c7:
            top = _top_value(f"{prefix}_top", int(defaults.get("top", 1000)))

        # 2라인
        c8, c9, c10, c11, c13, c14, c15 = st.columns([0.9, 1.8, 0.8, 1.9, 1.1, 1.1, 1.1])

        with c8:
            physic_cd = st.text_input(
                "제품코드",
                key=f"{prefix}_physic_cd",
            ).strip()

        with c9:
            physic_nm = st.text_input(
                "제품명",
                key=f"{prefix}_physic_nm",
            ).strip()

        with c10:
            product_search = st.form_submit_button(
                "제품\n후보",
                use_container_width=True,
            )

        with c11:
            st.selectbox(
                "제품 후보선택",
                options=product_options,
                key=f"{prefix}_product_pick",
            )

        with c13:
            product_group_nm = st.selectbox(
                "제품그룹명",
                options=product_group_options,
                index=_pick_index(
                    product_group_options,
                    st.session_state.get(f"{prefix}_product_group_nm", defaults.get("product_group_nm", "")),
                ),
                key=f"{prefix}_product_group_nm",
            )

        with c14:
            product_di_nm = st.selectbox(
                "제품구분명",
                options=product_di_options,
                index=_pick_index(
                    product_di_options,
                    st.session_state.get(f"{prefix}_product_di_nm", defaults.get("product_di_nm", "")),
                ),
                key=f"{prefix}_product_di_nm",
            )

        with c15:
            product_class_nm = st.selectbox(
                "제품분류명",
                options=product_class_options,
                index=_pick_index(
                    product_class_options,
                    st.session_state.get(f"{prefix}_product_class_nm", defaults.get("product_class_nm", "")),
                ),
                key=f"{prefix}_product_class_nm",
            )

        product_msg = _clean_text(st.session_state.get(f"{prefix}_product_msg", ""))
        if product_msg:
            st.caption(product_msg)

        # 3라인
        c16, c17, c18, c20, c21, c22, c24, c25, c26 = st.columns(
            [1.7, 0.8, 1.8, 1.7, 0.8, 1.8, 1.7, 0.8, 1.8]
        )

        with c16:
            maker_nm = st.text_input(
                "제조사명",
                key=f"{maker_prefix}_ven_nm",
            ).strip()

        with c17:
            maker_search = st.form_submit_button(
                "제조사\n후보",
                use_container_width=True,
            )

        with c18:
            st.selectbox(
                "제조사 후보선택",
                options=maker_options,
                key=f"{maker_prefix}_vendor_pick",
            )

        with c20:
            order_nm = st.text_input(
                "발주처명",
                key=f"{order_prefix}_ven_nm",
            ).strip()

        with c21:
            order_search = st.form_submit_button(
                "발주처\n후보",
                use_container_width=True,
            )

        with c22:
            st.selectbox(
                "발주처 후보선택",
                options=order_options,
                key=f"{order_prefix}_vendor_pick",
            )

        with c24:
            buy_nm = st.text_input(
                "매입처명",
                key=f"{buy_prefix}_ven_nm",
            ).strip()

        with c25:
            buy_search = st.form_submit_button(
                "매입처\n후보",
                use_container_width=True,
            )

        with c26:
            st.selectbox(
                "매입처 후보선택",
                options=buy_options,
                key=f"{buy_prefix}_vendor_pick",
            )

        maker_msg = _clean_text(st.session_state.get(f"{maker_prefix}_vendor_msg", ""))
        if maker_msg:
            st.caption(maker_msg)

        order_msg = _clean_text(st.session_state.get(f"{order_prefix}_vendor_msg", ""))
        if order_msg:
            st.caption(order_msg)

        buy_msg = _clean_text(st.session_state.get(f"{buy_prefix}_vendor_msg", ""))
        if buy_msg:
            st.caption(buy_msg)

        submitted = st.form_submit_button(
            "조회",
            type="primary",
            use_container_width=True,
            on_click=_trigger_panel_run,
        )

    # 후보검색 처리
    if product_search:
        _store_product_candidates(prefix, physic_nm)
        _rerun_panel_for_inner_submit()

    if maker_search:
        _store_vendor_candidates(maker_prefix, maker_nm, scope="maker")
        _rerun_panel_for_inner_submit()

    if order_search:
        _store_vendor_candidates(order_prefix, order_nm, scope="purchase")
        _rerun_panel_for_inner_submit()
    
    if buy_search:
        _store_vendor_candidates(buy_prefix, buy_nm, scope="purchase")
        _rerun_panel_for_inner_submit()

    if submitted:
        picked_all = ("전체" in selected_stock_labels) or (len(selected_stock_labels) == 0)
        stock_cds = [] if picked_all else [
            stock_label_to_code[label]
            for label in selected_stock_labels
            if stock_label_to_code.get(label)
        ]

        base_p = {
            "date_from": date_from.strftime("%Y%m%d"),
            "date_to": date_to.strftime("%Y%m%d"),
            "stock_mode": _clean_text(stock_mode),
            "group_basis": _clean_text(group_basis),
            "price_mode": _clean_text(price_mode),
            "physic_cd": _clean_text(physic_cd),
            "physic_nm": _clean_text(physic_nm),
            "product_group_nm": "" if _clean_text(product_group_nm) == "전체" else _clean_text(product_group_nm),
            "product_di_nm": "" if _clean_text(product_di_nm) == "전체" else _clean_text(product_di_nm),
            "product_class_nm": "" if _clean_text(product_class_nm) == "전체" else _clean_text(product_class_nm),
            "stock_cds": stock_cds,
            "stock_names": [] if picked_all else selected_stock_labels,
            "top": int(top),
        }

        maker_p = {
            "ven_cd": _clean_text(st.session_state.get(f"{maker_prefix}_ven_cd", defaults.get("maker_cd", ""))),
            "ven_nm": _clean_text(maker_nm),
        }
        order_p = {
            "ven_cd": _clean_text(st.session_state.get(f"{order_prefix}_ven_cd", defaults.get("order_cd", ""))),
            "ven_nm": _clean_text(order_nm),
        }
        buy_p = {
            "ven_cd": _clean_text(st.session_state.get(f"{buy_prefix}_ven_cd", defaults.get("buy_cd", ""))),
            "ven_nm": _clean_text(buy_nm),
        }

        if _needs_product_pick(prefix, base_p):
            msg = "제품 후보를 목록에서 선택한 뒤 [조회] 버튼을 누르세요."
            st.warning(msg)
            return {
                "title": title,
                "action": title,
                "params": {},
                "data": msg,
                "message": msg,
                "final": False,
            }

        if _needs_vendor_pick(maker_prefix, maker_p):
            msg = "제조사 후보를 목록에서 선택한 뒤 [조회] 버튼을 누르세요."
            st.warning(msg)
            return {
                "title": title,
                "action": title,
                "params": {},
                "data": msg,
                "message": msg,
                "final": False,
            }

        if _needs_vendor_pick(order_prefix, order_p):
            msg = "발주처 후보를 목록에서 선택한 뒤 [조회] 버튼을 누르세요."
            st.warning(msg)
            return {
                "title": title,
                "action": title,
                "params": {},
                "data": msg,
                "message": msg,
                "final": False,
            }

        if _needs_vendor_pick(buy_prefix, buy_p):
            msg = "매입처 후보를 목록에서 선택한 뒤 [조회] 버튼을 누르세요."
            st.warning(msg)
            return {
                "title": title,
                "action": title,
                "params": {},
                "data": msg,
                "message": msg,
                "final": False,
            }

        raw_physic_cd = _clean_text(base_p.get("physic_cd"))
        raw_physic_nm = _clean_text(base_p.get("physic_nm"))
        raw_maker_cd = _clean_text(maker_p.get("ven_cd"))
        raw_maker_nm = _clean_text(maker_p.get("ven_nm"))
        raw_order_cd = _clean_text(order_p.get("ven_cd"))
        raw_order_nm = _clean_text(order_p.get("ven_nm"))
        raw_buy_cd = _clean_text(buy_p.get("ven_cd"))
        raw_buy_nm = _clean_text(buy_p.get("ven_nm"))

        base_p = _apply_product_pick(prefix, base_p)
        maker_p = _apply_vendor_pick(maker_prefix, maker_p)
        order_p = _apply_vendor_pick(order_prefix, order_p)
        buy_p = _apply_vendor_pick(buy_prefix, buy_p)

        final_physic_cd = _clean_text(base_p.get("physic_cd"))
        final_physic_nm = _clean_text(base_p.get("physic_nm"))
        final_maker_cd = _clean_text(maker_p.get("ven_cd"))
        final_maker_nm = _clean_text(maker_p.get("ven_nm"))
        final_order_cd = _clean_text(order_p.get("ven_cd"))
        final_order_nm = _clean_text(order_p.get("ven_nm"))
        final_buy_cd = _clean_text(buy_p.get("ven_cd"))
        final_buy_nm = _clean_text(buy_p.get("ven_nm"))

        p = dict(base_p)
        p.update(
            {
                "maker_cd": final_maker_cd,
                "maker_nm": final_maker_nm,
                "order_cd": final_order_cd,
                "order_nm": final_order_nm,
                "buy_cd": final_buy_cd,
                "buy_nm": final_buy_nm,
            }
        )

        final_params = dict(defaults)
        final_params.update(p)

        _clear_payload_key(payload_key)

        payload = get_product_inventory_result(final_params)
        st.session_state[payload_key] = payload

        msg = str(payload.get("message") or payload.get("data") or "").strip()
        if msg and ("없습니다" in msg or "없읍니다" in msg or "0건" in msg):
            _show_text_payload(payload)

        if final_physic_cd != raw_physic_cd or final_physic_nm != raw_physic_nm:
            _queue_product_input_sync(prefix, final_physic_cd, final_physic_nm)

        if final_maker_cd != raw_maker_cd or final_maker_nm != raw_maker_nm:
            _queue_vendor_input_sync(maker_prefix, final_maker_cd, final_maker_nm)

        if final_order_cd != raw_order_cd or final_order_nm != raw_order_nm:
            _queue_vendor_input_sync(order_prefix, final_order_cd, final_order_nm)

        if final_buy_cd != raw_buy_cd or final_buy_nm != raw_buy_nm:
            _queue_vendor_input_sync(buy_prefix, final_buy_cd, final_buy_nm)

        st.session_state[f"{prefix}_product_reset_pending"] = True
        st.session_state[f"{maker_prefix}_vendor_reset_pending"] = True
        st.session_state[f"{order_prefix}_vendor_reset_pending"] = True
        st.session_state[f"{buy_prefix}_vendor_reset_pending"] = True

        if (
            final_physic_cd != raw_physic_cd or final_physic_nm != raw_physic_nm
            or final_maker_cd != raw_maker_cd or final_maker_nm != raw_maker_nm
            or final_order_cd != raw_order_cd or final_order_nm != raw_order_nm
            or final_buy_cd != raw_buy_cd or final_buy_nm != raw_buy_nm
        ):
            _rerun_panel_for_inner_submit()

        return payload

    if payload_key not in st.session_state:
        return {
            "title": title,
            "action": title,
            "params": {},
            "data": "조회조건을 입력한 뒤 [조회] 버튼을 눌러 주세요.",
            "final": False,
        }

    payload = st.session_state[payload_key]
    _show_text_payload(payload)
    return payload


view_rddbc260 = view_product_inventory