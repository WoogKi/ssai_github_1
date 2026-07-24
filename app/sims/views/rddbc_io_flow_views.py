# app/sims/views/rddbc_io_flow_views.py
# 제품수불 현황 용

from __future__ import annotations

from typing import Any, Dict, Optional
import datetime as dt

import pandas as pd
import streamlit as st

from app.services import rddbc010_service as C01
from app.services.product_flow_service import get_product_flow_result
from app.sims.views.rddbc_io_shared import (
    _apply_product_input_sync_if_pending,
    _apply_product_pick,
    _clear_payload_key,
    _finalize_io_payload,
    _maybe_reset_product_candidate_state,
    _needs_product_pick,
    _queue_product_input_sync,
    _render_product_candidate_row,
    _rerun_panel_for_inner_submit,
    _store_product_candidates,
    _top_value,
    _trigger_panel_run,
)

_STOCK_GCODE = "0018"

_FLOW_LOCAL_FILTER_ALIAS_MAP: Dict[str, list[str]] = {
    "physic_cd": ["제품코드", "PHYSIC_CD", "physic_cd"],
    "physic_nm": ["제품명", "PHYSIC_NM", "physic_nm"],
}


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


def _load_stock_options() -> list[tuple[str, str]]:
    """
    재고위치 업무코드 옵션
    - Rddbc110 기본 재고위치 Gcode = 0018
    """
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


def view_product_flow(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from app.services.rddbc_io_common import query_to_df
    from app.sims.views.rddbc_io_shared import (
        _apply_vendor_input_sync_if_pending,
        _apply_vendor_pick,
        _maybe_reset_vendor_candidate_state,
        _needs_vendor_pick,
        _queue_vendor_input_sync,
        _store_vendor_candidates,
    )

    title = "제품수불현황 조회"
    payload_key = "__io250_last_payload"
    prefix = "__io250"
    maker_prefix = f"{prefix}_maker"

    defaults = dict(params or {})

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

    def _pick_index(options: list[str], value: Any) -> int:
        v = _clean_text(value)
        if not v:
            return 0
        try:
            return options.index(v)
        except ValueError:
            return 0

    def _week_label_52(value: dt.date) -> str:
        weekday_map = ["월", "화", "수", "목", "금", "토", "일"]
        week_no = ((value.timetuple().tm_yday - 1) // 7) + 1
        if week_no < 1:
            week_no = 1
        if week_no > 52:
            week_no = 52
        return f"{weekday_map[value.weekday()]} / {week_no}주"

    def _product_search_sig(
        physic_nm: str,
        maker_nm: str,
        group_nm: str,
        di_nm: str,
        class_nm: str,
    ) -> str:
        return "||".join(
            [
                _clean_text(physic_nm),
                _clean_text(maker_nm),
                _clean_text(group_nm),
                _clean_text(di_nm),
                _clean_text(class_nm),
            ]
        )

    def _clear_product_candidate_state_ext() -> None:
        for k in [
            f"{prefix}_product_candidates",
            f"{prefix}_product_pick",
            f"{prefix}_product_msg",
            f"{prefix}_product_lookup_name",
            f"{prefix}_product_lookup_sig",
            f"{prefix}_product_reset_pending",
        ]:
            st.session_state.pop(k, None)

    def _store_product_candidates_ext(
        *,
        physic_nm: str,
        maker_nm: str,
        group_nm: str,
        di_nm: str,
        class_nm: str,
    ) -> None:
        _clear_product_candidate_state_ext()

        physic_nm = _clean_text(physic_nm)
        maker_nm = _clean_text(maker_nm)
        group_nm = "" if _clean_text(group_nm) == "전체" else _clean_text(group_nm)
        di_nm = "" if _clean_text(di_nm) == "전체" else _clean_text(di_nm)
        class_nm = "" if _clean_text(class_nm) == "전체" else _clean_text(class_nm)

        search_sig = _product_search_sig(physic_nm, maker_nm, group_nm, di_nm, class_nm)
        st.session_state[f"{prefix}_product_candidates"] = []
        st.session_state[f"{prefix}_product_pick"] = ""
        st.session_state[f"{prefix}_product_lookup_name"] = physic_nm
        st.session_state[f"{prefix}_product_lookup_sig"] = search_sig
        st.session_state[f"{prefix}_product_reset_pending"] = False

        if not any([physic_nm, maker_nm, group_nm, di_nm, class_nm]):
            st.session_state[f"{prefix}_product_msg"] = "제품명 또는 제조사/제품분류 조건을 입력하세요."
            return

        sql_params: Dict[str, Any] = {"top": 50}
        where = ["1 = 1"]

        if physic_nm:
            sql_params["physic_nm_like"] = f"%{physic_nm}%"
            where.append("P.Rd04_Physic_Nm LIKE %(physic_nm_like)s")

        if maker_nm:
            sql_params["maker_nm_like"] = f"%{maker_nm}%"
            where.append("M.Rd03_Ven_Nm LIKE %(maker_nm_like)s")

        if group_nm:
            sql_params["group_nm_like"] = f"%{group_nm}%"
            where.append("PG.Rd01_Hnm LIKE %(group_nm_like)s")

        if di_nm:
            sql_params["di_nm_like"] = f"%{di_nm}%"
            where.append("PD.Rd01_Hnm LIKE %(di_nm_like)s")

        if class_nm:
            sql_params["class_nm_like"] = f"%{class_nm}%"
            where.append("PF.Rd01_Hnm LIKE %(class_nm_like)s")

        sql = f"""
SELECT TOP (%(top)s)
    RTRIM(P.Rd04_Physic_Cd) AS physic_cd,
    RTRIM(P.Rd04_Physic_Nm) AS physic_nm
FROM dbo.Rddbc040 AS P
LEFT JOIN dbo.Rddbc030 AS M
    ON P.Rd04_Ven_Cd = M.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc010 AS PG
    ON P.Rd04_Physic_Group_Gcode = PG.Rd01_Gcode
   AND P.Rd04_Physic_Group = PG.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS PD
    ON P.Rd04_Physic_Di_Gcode = PD.Rd01_Gcode
   AND P.Rd04_Physic_Di = PD.Rd01_Tcode
LEFT JOIN dbo.Rddbc010 AS PF
    ON P.Rd04_Physic_Flag_Gcode = PF.Rd01_Gcode
   AND P.Rd04_Physic_Flag = PF.Rd01_Tcode
WHERE {" AND ".join(where)}
ORDER BY
    CASE WHEN RTRIM(P.Rd04_Physic_Nm) = %(physic_nm_exact)s THEN 0 ELSE 1 END,
    RTRIM(P.Rd04_Physic_Nm),
    RTRIM(P.Rd04_Physic_Cd)
"""
        sql_params["physic_nm_exact"] = physic_nm

        try:
            df = query_to_df(sql, sql_params)
        except Exception:
            df = pd.DataFrame()

        rows: list[tuple[str, str]] = []
        if isinstance(df, pd.DataFrame) and not df.empty:
            for _, row in df.iterrows():
                cd = str(row.get("physic_cd", "")).strip()
                nm = str(row.get("physic_nm", "")).strip()
                if cd:
                    rows.append((cd, nm))

        st.session_state[f"{prefix}_product_candidates"] = rows

        if rows:
            st.session_state[f"{prefix}_product_msg"] = f"제품 후보 {len(rows)}건. 후보선택에서 선택하세요."
        else:
            st.session_state[f"{prefix}_product_msg"] = "제품 후보가 없습니다."

    def _needs_product_pick_ext(p: Dict[str, Any]) -> bool:
        current_cd = _clean_text(p.get("physic_cd"))
        if current_cd:
            return False

        rows = st.session_state.get(f"{prefix}_product_candidates") or []
        if not rows:
            return False

        raw_pick = _clean_text(st.session_state.get(f"{prefix}_product_pick", ""))
        if raw_pick and " | " in raw_pick:
            return False

        current_sig = _product_search_sig(
            _clean_text(p.get("physic_nm")),
            _clean_text(p.get("product_ven_nm")),
            _clean_text(p.get("product_group_nm")),
            _clean_text(p.get("product_di_nm")),
            _clean_text(p.get("product_class_nm")),
        )
        lookup_sig = _clean_text(st.session_state.get(f"{prefix}_product_lookup_sig", ""))

        if not lookup_sig:
            return False

        return current_sig == lookup_sig

    def _apply_product_pick_ext(p: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(p)

        if _clean_text(p.get("physic_cd")):
            return p

        current_sig = _product_search_sig(
            _clean_text(p.get("physic_nm")),
            _clean_text(p.get("product_ven_nm")),
            _clean_text(p.get("product_group_nm")),
            _clean_text(p.get("product_di_nm")),
            _clean_text(p.get("product_class_nm")),
        )
        lookup_sig = _clean_text(st.session_state.get(f"{prefix}_product_lookup_sig", ""))
        if lookup_sig and current_sig != lookup_sig:
            return p

        raw = _clean_text(st.session_state.get(f"{prefix}_product_pick", ""))
        if raw and " | " in raw:
            cd, nm = raw.split(" | ", 1)
            p["physic_cd"] = cd.strip()
            p["physic_nm"] = nm.strip()

        return p

    _apply_vendor_input_sync_if_pending(prefix)
    _apply_product_input_sync_if_pending(prefix)
    _apply_vendor_input_sync_if_pending(maker_prefix)

    _maybe_reset_vendor_candidate_state(prefix)
    _maybe_reset_product_candidate_state(prefix)
    _maybe_reset_vendor_candidate_state(maker_prefix)

    stock_mode_default = _clean_text(defaults.get("stock_mode") or "실수불") or "실수불"
    flow_scope_default = _clean_text(defaults.get("flow_scope") or "전체") or "전체"
    date_basis_default = _clean_text(defaults.get("date_basis") or "입출고일자") or "입출고일자"

    stock_options = _load_stock_options()
    stock_label_to_code = {label: code for label, code in stock_options}
    stock_code_to_label = {code: label for label, code in stock_options if code}

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

    ven_group_options = _load_code_name_options("0019")
    ven_kind_options = _load_code_name_options("0009")
    product_group_options = _load_code_name_options("0013")
    product_di_options = _load_code_name_options("0004")
    product_class_options = _load_code_name_options("0001")

    selected_maker_cd = _clean_text(
        st.session_state.get(f"{maker_prefix}_ven_cd", defaults.get("product_ven_cd", ""))
    )
    selected_maker_nm = _clean_text(
        st.session_state.get(f"{maker_prefix}_ven_nm", defaults.get("product_ven_nm", ""))
    )

    maker_rows = st.session_state.get(f"{maker_prefix}_vendor_candidates", []) or []
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

#   @@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
    with st.form("__io250_form", clear_on_submit=False, enter_to_submit=False):
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

        st.caption("조회조건 · 제품수불현황")
        st.caption("제품수불현황은 제품 1개를 먼저 지정해 주세요.")

        # 1줄: 기본 조회 조건
        c1, c2, c3, c4, c5, c6, c7 = st.columns(
            [0.9, 0.9, 1.0, 1.15, 1.15, 2.2, 0.8]
        )

        with c1:
            stock_mode = st.selectbox(
                "수불구분",
                options=["실수불", "장부수불"],
                index=0 if stock_mode_default != "장부수불" else 1,
                key=f"{prefix}_stock_mode",
            )

        with c2:
            flow_scope = st.selectbox(
                "조회범위",
                options=["전체", "매입", "매출"],
                index={"전체": 0, "매입": 1, "매출": 2}.get(flow_scope_default, 0),
                key=f"{prefix}_flow_scope",
            )

        with c3:
            if stock_mode == "장부수불":
                date_basis_options = ["명세서일자"]
                date_basis_index = 0
            else:
                date_basis_options = ["입출고일자", "명세서일자"]
                date_basis_index = 0 if date_basis_default != "명세서일자" else 1

            date_basis = st.selectbox(
                "기준일자",
                options=date_basis_options,
                index=date_basis_index,
                key=f"{prefix}_date_basis",
            )

        with c4:
            date_from = st.date_input(
                "시작일자",
                value=date_from_default,
                format="YYYY-MM-DD",
                key=f"{prefix}_date_from",
            )
            st.caption(_week_label_52(date_from))

        with c5:
            date_to = st.date_input(
                "종료일자",
                value=date_to_default,
                format="YYYY-MM-DD",
                key=f"{prefix}_date_to",
            )
            st.caption(_week_label_52(date_to))

        with c6:
            selected_stock_labels = st.multiselect(
                "재고위치",
                options=[label for label, _ in stock_options],
                default=default_stock_labels,
                key=f"{prefix}_stock_labels",
                help="전체는 선택하지 않거나, '전체'만 선택하면 됩니다. 여러 재고위치를 동시에 선택할 수 있습니다.",
            )

        with c7:
            top = _top_value(
                f"{prefix}_top",
                int(defaults.get("top", 20000)),
            )

        # 2줄: 제품 관련
        c8, c9, c10, c11, c12, c13, c14, c15, c16, c17 = st.columns(
            [1.0, 2.0, 0.9, 2.0, 1.8, 0.9, 1.8, 1.0, 1.0, 1.0]
        )

        with c8:
            physic_cd_key = f"{prefix}_physic_cd"
            if physic_cd_key in st.session_state:
                physic_cd = st.text_input("제품코드", key=physic_cd_key).strip()
            else:
                physic_cd = st.text_input(
                    "제품코드",
                    value=_clean_text(defaults.get("physic_cd")),
                    key=physic_cd_key,
                ).strip()

        with c9:
            physic_nm_key = f"{prefix}_physic_nm"
            if physic_nm_key in st.session_state:
                physic_nm = st.text_input("제품명", key=physic_nm_key).strip()
            else:
                physic_nm = st.text_input(
                    "제품명",
                    value=_clean_text(defaults.get("physic_nm")),
                    key=physic_nm_key,
                ).strip()

        with c10:
            product_search = st.form_submit_button(
                "제품\n후보",
                width="stretch",
            )

        with c11:
            product_rows = st.session_state.get(f"{prefix}_product_candidates", []) or []
            product_options = [""] + [f"{cd} | {nm}" for cd, nm in product_rows]
            st.selectbox(
                "제품 후보선택",
                options=product_options,
                key=f"{prefix}_product_pick",
            )

        with c12:
            maker_ven_nm_key = f"{maker_prefix}_ven_nm"
            if maker_ven_nm_key in st.session_state:
                product_ven_nm = st.text_input("제조사명", key=maker_ven_nm_key).strip()
            else:
                product_ven_nm = st.text_input(
                    "제조사명",
                    value=selected_maker_nm,
                    key=maker_ven_nm_key,
                ).strip()

        with c13:
            maker_vendor_search = st.form_submit_button(
                "제조사\n후보",
                width="stretch",
            )

        with c14:
            maker_choice = st.selectbox(
                "제조사 후보선택",
                options=maker_options,
                index=default_maker_idx,
                key=f"{maker_prefix}_vendor_pick",
            )

        with c15:
            product_group_nm = st.selectbox(
                "제품그룹명",
                options=product_group_options,
                index=_pick_index(product_group_options, defaults.get("product_group_nm", "")),
                key=f"{prefix}_product_group_nm",
            )

        with c16:
            product_di_nm = st.selectbox(
                "제품구분명",
                options=product_di_options,
                index=_pick_index(product_di_options, defaults.get("product_di_nm", "")),
                key=f"{prefix}_product_di_nm",
            )

        with c17:
            product_class_nm = st.selectbox(
                "제품분류명",
                options=product_class_options,
                index=_pick_index(product_class_options, defaults.get("product_class_nm", "")),
                key=f"{prefix}_product_class_nm",
            )

        product_msg = _clean_text(st.session_state.get(f"{prefix}_product_msg", ""))
        if product_msg:
            st.caption(product_msg)

        maker_msg = _clean_text(st.session_state.get(f"{maker_prefix}_vendor_msg", ""))
        if maker_msg:
            st.caption(maker_msg)

        # 3줄: 거래처 관련
        c18, c19, c20, c21, c22, c23 = st.columns(
            [1.0, 2.2, 0.9, 2.2, 1.2, 1.2]
        )

        with c18:
            ven_cd_key = f"{prefix}_ven_cd"
            if ven_cd_key in st.session_state:
                ven_cd = st.text_input("거래처코드", key=ven_cd_key).strip()
            else:
                ven_cd = st.text_input(
                    "거래처코드",
                    value=_clean_text(defaults.get("ven_cd")),
                    key=ven_cd_key,
                ).strip()

        with c19:
            ven_nm_key = f"{prefix}_ven_nm"
            if ven_nm_key in st.session_state:
                ven_nm = st.text_input("거래처명", key=ven_nm_key).strip()
            else:
                ven_nm = st.text_input(
                    "거래처명",
                    value=_clean_text(defaults.get("ven_nm")),
                    key=ven_nm_key,
                ).strip()

        with c20:
            vendor_search = st.form_submit_button(
                "거래처\n후보",
                width="stretch",
            )

        with c21:
            vendor_rows = st.session_state.get(f"{prefix}_vendor_candidates", []) or []
            vendor_options = [""] + [f"{cd} | {nm}" for cd, nm in vendor_rows]
            st.selectbox(
                "거래처 후보선택",
                options=vendor_options,
                key=f"{prefix}_vendor_pick",
            )

        with c22:
            ven_group_nm = st.selectbox(
                "거래처그룹",
                options=ven_group_options,
                index=_pick_index(ven_group_options, defaults.get("ven_group_nm", "")),
                key=f"{prefix}_ven_group_nm",
            )

        with c23:
            ven_kind_nm = st.selectbox(
                "거래처종류",
                options=ven_kind_options,
                index=_pick_index(ven_kind_options, defaults.get("ven_kind_nm", "")),
                key=f"{prefix}_ven_kind_nm",
            )

        vendor_msg = _clean_text(st.session_state.get(f"{prefix}_vendor_msg", ""))
        if vendor_msg:
            st.caption(vendor_msg)

        submitted = st.form_submit_button(
            "조회",
            type="primary",
            width="stretch",
            on_click=_trigger_panel_run,
        )

#   @@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
    if vendor_search:
        _store_vendor_candidates(prefix, ven_nm, scope="all")
        _rerun_panel_for_inner_submit()

    if product_search:
        _store_product_candidates_ext(
            physic_nm=physic_nm,
            maker_nm=product_ven_nm,
            group_nm=product_group_nm,
            di_nm=product_di_nm,
            class_nm=product_class_nm,
        )
        _rerun_panel_for_inner_submit()

    if maker_vendor_search:
        _store_vendor_candidates(maker_prefix, product_ven_nm, scope="maker")
        _rerun_panel_for_inner_submit()

    if submitted:
        picked_all = ("전체" in selected_stock_labels) or (len(selected_stock_labels) == 0)
        stock_cds = [] if picked_all else [
            stock_label_to_code[label]
            for label in selected_stock_labels
            if stock_label_to_code.get(label)
        ]

        p = {
            "date_from": date_from.strftime("%Y%m%d"),
            "date_to": date_to.strftime("%Y%m%d"),
            "stock_mode": _clean_text(stock_mode),
            "flow_scope": _clean_text(flow_scope),
            "date_basis": _clean_text(date_basis),
            "ven_cd": _clean_text(ven_cd),
            "ven_nm": _clean_text(ven_nm),
            "ven_group_nm": "" if _clean_text(ven_group_nm) == "전체" else _clean_text(ven_group_nm),
            "ven_kind_nm": "" if _clean_text(ven_kind_nm) == "전체" else _clean_text(ven_kind_nm),
            "physic_cd": _clean_text(physic_cd),
            "physic_nm": _clean_text(physic_nm),
            "product_ven_cd": _clean_text(defaults.get("product_ven_cd")),
            "product_ven_nm": _clean_text(product_ven_nm),
            "product_group_nm": "" if _clean_text(product_group_nm) == "전체" else _clean_text(product_group_nm),
            "product_di_nm": "" if _clean_text(product_di_nm) == "전체" else _clean_text(product_di_nm),
            "product_class_nm": "" if _clean_text(product_class_nm) == "전체" else _clean_text(product_class_nm),
            "stock_cds": stock_cds,
            "stock_cd": stock_cds[0] if len(stock_cds) == 1 else "",
            "top": int(top),
        }

        # 현재 입력칸의 원래 값
        raw_ven_cd = _clean_text(st.session_state.get(f"{prefix}_ven_cd", p.get("ven_cd", "")))
        raw_ven_nm = _clean_text(st.session_state.get(f"{prefix}_ven_nm", p.get("ven_nm", "")))
        raw_physic_cd = _clean_text(st.session_state.get(f"{prefix}_physic_cd", p.get("physic_cd", "")))
        raw_physic_nm = _clean_text(st.session_state.get(f"{prefix}_physic_nm", p.get("physic_nm", "")))
        raw_maker_cd = _clean_text(st.session_state.get(f"{maker_prefix}_ven_cd", p.get("product_ven_cd", "")))
        raw_maker_nm = _clean_text(st.session_state.get(f"{maker_prefix}_ven_nm", p.get("product_ven_nm", "")))

        # 1) 후보선택값을 먼저 p에 강제 반영
        vendor_pick_raw = _clean_text(st.session_state.get(f"{prefix}_vendor_pick", ""))
        if vendor_pick_raw and " | " in vendor_pick_raw:
            try:
                vcd, vnm = vendor_pick_raw.split(" | ", 1)
                p["ven_cd"] = vcd.strip()
                p["ven_nm"] = vnm.strip()
            except Exception:
                pass

        maker_pick_raw = _clean_text(st.session_state.get(f"{maker_prefix}_vendor_pick", ""))
        if maker_pick_raw and " | " in maker_pick_raw:
            try:
                mcd, mnm = maker_pick_raw.split(" | ", 1)
                p["product_ven_cd"] = mcd.strip()
                p["product_ven_nm"] = mnm.strip()
            except Exception:
                pass

        product_pick_raw = _clean_text(st.session_state.get(f"{prefix}_product_pick", ""))
        if product_pick_raw and " | " in product_pick_raw:
            try:
                pcd, pnm = product_pick_raw.split(" | ", 1)
                p["physic_cd"] = pcd.strip()
                p["physic_nm"] = pnm.strip()
            except Exception:
                pass

        # 2) 거래처 후보 미선택 경고
        if _needs_vendor_pick(prefix, p):
            msg = "거래처 후보를 선택한 뒤 [조회] 버튼을 누르세요."
            st.warning(msg)
            return {
                "title": title,
                "action": title,
                "params": {},
                "data": msg,
                "message": msg,
                "final": False,
            }

        # 3) 제조사 후보 미선택 경고
        maker_current_nm = _clean_text(st.session_state.get(f"{maker_prefix}_ven_nm", p.get("product_ven_nm", "")))
        maker_lookup_nm = _clean_text(st.session_state.get(f"{maker_prefix}_vendor_lookup_name", ""))
        maker_rows2 = st.session_state.get(f"{maker_prefix}_vendor_candidates", []) or []
        if not isinstance(maker_rows2, list):
            maker_rows2 = []

        if maker_rows2 and maker_lookup_nm and maker_current_nm == maker_lookup_nm and not (maker_pick_raw and " | " in maker_pick_raw):
            msg = "제조사 후보를 선택한 뒤 [조회] 버튼을 누르세요."
            st.warning(msg)
            return {
                "title": title,
                "action": title,
                "params": {},
                "data": msg,
                "message": msg,
                "final": False,
            }

        # 4) 제품 후보 미선택 경고
        if _needs_product_pick_ext(p):
            msg = "제품 후보를 선택한 뒤 [조회] 버튼을 누르세요."
            st.warning(msg)
            return {
                "title": title,
                "action": title,
                "params": {},
                "data": msg,
                "message": msg,
                "final": False,
            }

        # 기존 helper도 한 번 더 적용
        p = _apply_vendor_pick(prefix, p)
        p = _apply_product_pick_ext(p)

        final_ven_cd = _clean_text(p.get("ven_cd"))
        final_ven_nm = _clean_text(p.get("ven_nm"))
        final_physic_cd = _clean_text(p.get("physic_cd"))
        final_physic_nm = _clean_text(p.get("physic_nm"))
        final_maker_cd = _clean_text(p.get("product_ven_cd"))
        final_maker_nm = _clean_text(p.get("product_ven_nm"))

        if not final_physic_cd:
            msg = "제품수불현황은 제품 1개를 먼저 지정해 주세요."
            st.warning(msg)
            return {
                "title": title,
                "action": title,
                "params": {},
                "data": msg,
                "message": msg,
                "final": False,
            }

        final_params = dict(defaults)
        final_params.update(p)
        final_params["stock_names"] = [] if picked_all else selected_stock_labels

        _clear_payload_key(payload_key)

        payload = get_product_flow_result(final_params)
        st.session_state[payload_key] = payload

        st.session_state[f"{prefix}_product_reset_pending"] = True
        st.session_state[f"{prefix}_vendor_reset_pending"] = True
        st.session_state[f"{maker_prefix}_vendor_reset_pending"] = True

        vendor_sync_needed = (final_ven_cd != raw_ven_cd) or (final_ven_nm != raw_ven_nm)
        product_sync_needed = (final_physic_cd != raw_physic_cd) or (final_physic_nm != raw_physic_nm)
        maker_sync_needed = (final_maker_cd != raw_maker_cd) or (final_maker_nm != raw_maker_nm)

        if vendor_sync_needed:
            _queue_vendor_input_sync(prefix, final_ven_cd, final_ven_nm)

        if product_sync_needed:
            _queue_product_input_sync(prefix, final_physic_cd, final_physic_nm)

        if maker_sync_needed:
            _queue_vendor_input_sync(maker_prefix, final_maker_cd, final_maker_nm)

        if vendor_sync_needed or product_sync_needed or maker_sync_needed:
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

    return st.session_state[payload_key]




