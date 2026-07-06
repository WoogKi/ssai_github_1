# app/sims/views/rddbc_io_shared.py

from __future__ import annotations

from typing import Any, Dict, Optional
import datetime as dt
import os
import pandas as pd
from pandas.api.types import is_datetime64_any_dtype, is_numeric_dtype

import streamlit as st

from app.services import rddbc010_service as C01
from app.services.rddbc_io_common import query_to_df
from app.services.rddbc040_service import search_goods_full
from app.services.utils import apply_labels

import logging
log = logging.getLogger("ssai")


# ---------------------------------------------------------------------
# 공통 helper
# ---------------------------------------------------------------------
def _trigger_panel_run() -> None:
    ss = st.session_state
    ss["__sims_run_flag"] = True
    ss["__sims_inner_submit"] = True
    ss["__sims_run_seq"] = int(ss.get("__sims_run_seq", 0)) + 1


def _trigger_panel_inner_submit() -> None:
    ss = st.session_state
    ss["__sims_run_flag"] = False   # 핵심: 후보검색은 실행이 아니다
    ss["__sims_inner_submit"] = True
    ss["__sims_panel_active"] = True
    ss["__sims_run_seq"] = int(ss.get("__sims_run_seq", 0)) + 1

def _txt(key: str, label: str, value: str = "") -> str:
    return st.text_input(label, value=value, key=key).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default)) or default).strip())
    except Exception:
        return int(default)


def _panel_display_max_rows(default: int = 30000) -> int:
    """
    조회 화면 공통 상한.
    별도 env를 만들지 않고 기존 표시 제한 env를 그대로 사용한다.
    - 1순위: SIMS_PANEL_DISPLAY_MAX_ROWS
    - 2순위: SIMS_CHAT_DISPLAY_MAX_ROWS
    """
    v = _env_int(
        "SIMS_PANEL_DISPLAY_MAX_ROWS",
        _env_int("SIMS_CHAT_DISPLAY_MAX_ROWS", default),
    )
    return max(1, int(v))


def _top_value(key: str, default: int) -> int:
    """
    예전에는 화면에 '조회건수' 입력칸을 렌더링했다.
    실무 조회에서는 Top N을 사용자가 고르는 의미가 약하므로
    입력칸은 숨기고 공통 표시 제한 env 값을 사용한다.
    key/default 인자는 기존 호출부 호환용으로만 유지한다.
    """
    try:
        fallback = int(default) if int(default) > 0 else 30000
    except Exception:
        fallback = 30000
    return _panel_display_max_rows(fallback)

# 월집계 조회는 기본적으로 충분히 크게 가져오도록 별도 상한 설정
# (월집계는 화면에 다 표시하기보다는 엑셀로 내려받아서 보는 경우가 많아서, 화면 표시용 상한과 분리하는 게 낫다고 판단)
def _monthly_fetch_top_value() -> int:
    """
    월집계 화면 조회 상한.

    월집계도 별도 Top 입력/별도 MAX를 쓰지 않고
    기존 공통 표시 제한 env(SIMS_PANEL_DISPLAY_MAX_ROWS)를 그대로 사용한다.
    다운로드/현재표 후속분석용 전체 DF는 export 경로에서 별도로 보강한다.
    """
    return _panel_display_max_rows(30000)

def _checkbox_yn(key: str, label: str, value: bool = False, disabled: bool = False) -> str:
    return "Y" if st.checkbox(label, value=value, key=key, disabled=disabled) else ""

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


def _as_date(value: Any, fallback: dt.date) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value

    text = _clean_text(value)
    digits = "".join(ch for ch in text if ch.isdigit())
    try:
        if len(digits) == 8:
            return dt.datetime.strptime(digits, "%Y%m%d").date()
        if len(digits) == 6:
            return dt.datetime.strptime(digits + "01", "%Y%m%d").date()
    except Exception:
        pass
    return fallback


def _week_label_52(value: Any) -> str:
    text = _clean_text(value)
    if isinstance(value, dt.date):
        d = value
    else:
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) != 8:
            return ""
        try:
            d = dt.datetime.strptime(digits, "%Y%m%d").date()
        except Exception:
            return ""

    weekday_map = ["월", "화", "수", "목", "금", "토", "일"]
    week_no = ((d.timetuple().tm_yday - 1) // 7) + 1
    if week_no < 1:
        week_no = 1
    if week_no > 52:
        week_no = 52

    return f"{weekday_map[d.weekday()]} / {week_no}주"


def _render_date_input_with_week(
    key: str,
    label: str,
    value: dt.date,
) -> dt.date:
    d = st.date_input(
        label,
        value=value,
        format="YYYY-MM-DD",
        key=key,
    )
    st.caption(_week_label_52(d))
    return d


def _render_io_date_range(
    prefix: str,
    date_from_label: str = "시작일자",
    date_to_label: str = "종료일자",
    default_from: Optional[Any] = None,
    default_to: Optional[Any] = None,
) -> Dict[str, Any]:
    today = dt.date.today()
    from_date = _as_date(default_from, today - dt.timedelta(days=30))
    to_date = _as_date(default_to, today)

    c1, c2 = st.columns(2)
    with c1:
        date_from = _render_date_input_with_week(
            f"{prefix}_date_from",
            date_from_label,
            from_date,
        )
    with c2:
        date_to = _render_date_input_with_week(
            f"{prefix}_date_to",
            date_to_label,
            to_date,
        )

    return {
        "date_from": date_from.strftime("%Y%m%d"),
        "date_to": date_to.strftime("%Y%m%d"),
        "date_from_obj": date_from,
        "date_to_obj": date_to,
        "date_from_week_label": _week_label_52(date_from),
        "date_to_week_label": _week_label_52(date_to),
    }


def _load_stock_code_options(gcode: str = _STOCK_GCODE) -> list[tuple[str, str, str]]:
    try:
        fn = getattr(C01, "list_by_group", None)
        if callable(fn):
            df = _ensure_df(fn(gcode=gcode, top=2000))
        else:
            df = _ensure_df(C01.search_rows(gcode=gcode, top=2000, only_active=True))
    except Exception:
        return [("전체", "", "전체")]

    if df.empty:
        return [("전체", "", "전체")]

    tcol = _pick_col(df, ["Rd01_Tcode", "항목코드", "상세코드"])
    ncol = _pick_col(df, ["Rd01_Hnm", "한글명", "코드명"])
    if not tcol or not ncol:
        return [("전체", "", "전체")]

    work = df[[tcol, ncol]].copy()
    work[tcol] = _norm_series(work[tcol])
    work[ncol] = _norm_series(work[ncol])
    work = work[(work[tcol] != "") & (work[ncol] != "")]
    work = work.drop_duplicates(subset=[tcol, ncol]).sort_values([tcol], kind="stable")

    options: list[tuple[str, str, str]] = [("전체", "", "전체")]
    for _, row in work.iterrows():
        code = str(row[tcol]).strip()
        name = str(row[ncol]).strip()
        options.append((f"{name} ({code})", code, name))
    return options


def _render_stock_multiselect(
    prefix: str,
    label: str = "재고위치",
    default_codes: Optional[list[str]] = None,
    gcode: str = _STOCK_GCODE,
) -> Dict[str, Any]:
    options = _load_stock_code_options(gcode=gcode)

    label_to_code = {label_: code for label_, code, _ in options}
    label_to_name = {label_: name for label_, _, name in options}
    code_to_label = {code: label_ for label_, code, _ in options if code}

    default_codes = [str(x).strip() for x in (default_codes or []) if str(x).strip()]
    default_labels = [code_to_label[c] for c in default_codes if c in code_to_label]

    selected_labels = st.multiselect(
        label,
        options=[label_ for label_, _, _ in options],
        default=default_labels,
        key=f"{prefix}_stock_labels",
        help="선택하지 않거나 '전체'만 선택하면 전체 조회입니다. 여러 재고위치를 동시에 선택할 수 있습니다.",
    )

    picked_all = ("전체" in selected_labels) or (len(selected_labels) == 0)

    stock_cds = [] if picked_all else [
        label_to_code[label_]
        for label_ in selected_labels
        if label_to_code.get(label_)
    ]
    stock_names = [] if picked_all else [
        label_to_name[label_]
        for label_ in selected_labels
        if label_to_name.get(label_)
    ]

    return {
        "stock_cds": stock_cds,
        "stock_names": stock_names,
        "stock_cd": stock_cds[0] if len(stock_cds) == 1 else "",
        "stock_nm": "" if picked_all else ", ".join(stock_names),
        "stock_label_text": "전체" if picked_all else ", ".join(selected_labels),
    }

def _date_range_inputs(prefix: str) -> Dict[str, Any]:
    return _render_io_date_range(prefix)

def _month_range_inputs(prefix: str) -> Dict[str, Any]:
    c1, c2 = st.columns(2)
    with c1:
        month_from = _txt(f"{prefix}_month_from", "시작월(YYYYMM)")
    with c2:
        month_to = _txt(f"{prefix}_month_to", "종료월(YYYYMM)")
    return {"month_from": month_from, "month_to": month_to}


def _audit_inputs(prefix: str) -> Dict[str, Any]:
    params: Dict[str, Any] = {}

    with st.expander("고급 조회조건"):
        c1, c2 = st.columns(2)
        with c1:
            params["add_cd"] = _txt(f"{prefix}_add_cd", "등록자코드")
        with c2:
            params["add_nm"] = _txt(f"{prefix}_add_nm", "등록자명")

        c3, c4 = st.columns(2)
        with c3:
            params["mod_cd"] = _txt(f"{prefix}_mod_cd", "수정자코드")
        with c4:
            params["mod_nm"] = _txt(f"{prefix}_mod_nm", "수정자명")

        c5, c6 = st.columns(2)
        with c5:
            params["add_date_from"] = _txt(f"{prefix}_add_date_from", "등록일자 From(YYYYMMDD)")
        with c6:
            params["add_date_to"] = _txt(f"{prefix}_add_date_to", "등록일자 To(YYYYMMDD)")

        c7, c8 = st.columns(2)
        with c7:
            params["mod_date_from"] = _txt(f"{prefix}_mod_date_from", "수정일자 From(YYYYMMDD)")
        with c8:
            params["mod_date_to"] = _txt(f"{prefix}_mod_date_to", "수정일자 To(YYYYMMDD)")

    return params


def _product_inputs(prefix: str) -> Dict[str, Any]:
    c1, c2 = st.columns(2)
    with c1:
        physic_cd = _txt(f"{prefix}_physic_cd", "제품코드")
    with c2:
        physic_nm = _txt(f"{prefix}_physic_nm", "제품명")

    c3, c4 = st.columns(2)
    with c3:
        stock_cd = _txt(f"{prefix}_stock_cd", "재고위치코드")
    with c4:
        stock_nm = _txt(f"{prefix}_stock_nm", "재고위치명")

    return {
        "physic_cd": physic_cd,
        "physic_nm": physic_nm,
        "stock_cd": stock_cd,
        "stock_nm": stock_nm,
    }

def _product_inputs_with_stock_multiselect(
    prefix: str,
    default_stock_codes: Optional[list[str]] = None,
) -> Dict[str, Any]:
    c1, c2 = st.columns(2)
    with c1:
        physic_cd = _txt(f"{prefix}_physic_cd", "제품코드")
    with c2:
        physic_nm = _txt(f"{prefix}_physic_nm", "제품명")

    stock_info = _render_stock_multiselect(
        prefix=prefix,
        label="재고위치",
        default_codes=default_stock_codes,
    )

    return {
        "physic_cd": physic_cd,
        "physic_nm": physic_nm,
        **stock_info,
    }

def _queue_vendor_input_sync(prefix: str, ven_cd: str, ven_nm: str) -> None:
    st.session_state[f"{prefix}_vendor_sync_pending"] = {
        "ven_cd": (ven_cd or "").strip(),
        "ven_nm": (ven_nm or "").strip(),
    }


def _apply_vendor_input_sync_if_pending(prefix: str) -> None:
    pending = st.session_state.pop(f"{prefix}_vendor_sync_pending", None)
    if not pending:
        return

    # 위젯 생성 전 단계에서만 입력칸 값 동기화
    st.session_state[f"{prefix}_ven_cd"] = pending.get("ven_cd", "")
    st.session_state[f"{prefix}_ven_nm"] = pending.get("ven_nm", "")


# ---------------------------------------------------------------------
# 제품 후보검색 helper
# ---------------------------------------------------------------------
def _lookup_product_candidates(name_text: str, limit: int = 30) -> list[tuple[str, str]]:
    name_text = (name_text or "").strip()
    if not name_text:
        return []

    df = search_goods_full(
        top=limit,
        keyword=name_text,
        only_use=True,
    )
    if df is None or df.empty:
        return []

    df = apply_labels(df, "rddbc040")

    out: list[tuple[str, str]] = []
    if "제품코드" not in df.columns or "제품명" not in df.columns:
        return out

    for _, row in df.iterrows():
        cd = str(row.get("제품코드", "")).strip()
        nm = str(row.get("제품명", "")).strip()
        if cd:
            out.append((cd, nm))
    return out


def _clear_product_candidate_state(prefix: str) -> None:
    for k in [
        f"{prefix}_product_candidates",
        f"{prefix}_product_pick",
        f"{prefix}_product_msg",
        f"{prefix}_product_lookup_name",
        f"{prefix}_product_reset_pending",
    ]:
        st.session_state.pop(k, None)


def _maybe_reset_product_candidate_state(prefix: str) -> None:
    if st.session_state.get(f"{prefix}_product_reset_pending"):
        _clear_product_candidate_state(prefix)


def _store_product_candidates(prefix: str, physic_nm: str) -> None:
    physic_nm = (physic_nm or "").strip()
    _clear_product_candidate_state(prefix)

    _queue_product_input_sync(prefix, "", physic_nm)

    st.session_state[f"{prefix}_product_candidates"] = []
    st.session_state[f"{prefix}_product_pick"] = ""
    st.session_state[f"{prefix}_product_lookup_name"] = physic_nm
    st.session_state[f"{prefix}_product_reset_pending"] = False

    if not physic_nm:
        st.session_state[f"{prefix}_product_msg"] = "제품명을 입력하세요."
        return

    rows = _lookup_product_candidates(physic_nm)
    st.session_state[f"{prefix}_product_candidates"] = rows

    if rows:
        st.session_state[f"{prefix}_product_pick"] = ""
        st.session_state[f"{prefix}_product_msg"] = f"제품 후보 {len(rows)}건. 후보선택에서 선택하세요."
    else:
        st.session_state[f"{prefix}_product_pick"] = ""
        st.session_state[f"{prefix}_product_msg"] = "제품 후보가 없습니다."


def _needs_product_pick(prefix: str, params: Dict[str, Any]) -> bool:
    p = dict(params)

    current_cd = str(p.get("physic_cd", "")).strip()
    current_nm = str(p.get("physic_nm", "")).strip()
    lookup_nm = str(st.session_state.get(f"{prefix}_product_lookup_name", "")).strip()
    raw = str(st.session_state.get(f"{prefix}_product_pick", "")).strip()
    rows = st.session_state.get(f"{prefix}_product_candidates") or []

    if current_cd:
        return False
    if not rows:
        return False
    if not lookup_nm or current_nm != lookup_nm:
        return False

    return not (raw and " | " in raw)


def _apply_product_pick(prefix: str, params: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(params)

    current_cd = str(p.get("physic_cd", "")).strip()
    current_nm = str(p.get("physic_nm", "")).strip()
    lookup_nm = str(st.session_state.get(f"{prefix}_product_lookup_name", "")).strip()
    raw = st.session_state.get(f"{prefix}_product_pick", "")

    if current_cd:
        return p
    if lookup_nm and current_nm != lookup_nm:
        return p

    if raw and " | " in raw:
        cd, nm = raw.split(" | ", 1)
        p["physic_cd"] = cd.strip()
        p["physic_nm"] = nm.strip()

    return p


def _queue_product_input_sync(prefix: str, physic_cd: str, physic_nm: str) -> None:
    st.session_state[f"{prefix}_product_sync_pending"] = {
        "physic_cd": (physic_cd or "").strip(),
        "physic_nm": (physic_nm or "").strip(),
    }


def _apply_product_input_sync_if_pending(prefix: str) -> None:
    pending = st.session_state.pop(f"{prefix}_product_sync_pending", None)
    if not pending:
        return

    st.session_state[f"{prefix}_physic_cd"] = pending.get("physic_cd", "")
    st.session_state[f"{prefix}_physic_nm"] = pending.get("physic_nm", "")


# ---------------------------------------------------------------------
# 시퀀스 / 기본 파라미터 helper
# ---------------------------------------------------------------------
def _seq_inputs_110(prefix: str) -> Dict[str, Any]:
    c1, c2, c3 = st.columns(3)
    with c1:
        in_seq = _txt(f"{prefix}_in_seq", "입고순번")
    with c2:
        trans_seq = _txt(f"{prefix}_trans_seq", "거래명세서순번")
    with c3:
        tax_seq = _txt(f"{prefix}_tax_seq", "세금계산서순번")
    return {"in_seq": in_seq, "trans_seq": trans_seq, "tax_seq": tax_seq}


def _seq_inputs_120(prefix: str) -> Dict[str, Any]:
    c1, c2, c3 = st.columns(3)
    with c1:
        out_seq = _txt(f"{prefix}_out_seq", "출고순번")
    with c2:
        trans_seq = _txt(f"{prefix}_trans_seq", "거래명세서순번")
    with c3:
        tax_seq = _txt(f"{prefix}_tax_seq", "세금계산서순번")
    return {"out_seq": out_seq, "trans_seq": trans_seq, "tax_seq": tax_seq}


def _base_params_110(prefix: str, default_top: int = 200) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    params.update(_date_range_inputs(prefix))
    params.update(_product_inputs(prefix))
    params.update(_seq_inputs_110(prefix))
    params["top"] = _top_value(f"{prefix}_top", default_top)
    return params


def _base_params_120(prefix: str, default_top: int = 200) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    params.update(_date_range_inputs(prefix))
    params.update(_product_inputs(prefix))
    params.update(_seq_inputs_120(prefix))
    params["top"] = _top_value(f"{prefix}_top", default_top)
    return params


def _base_params_130(prefix: str, default_top: int = 200) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    params.update(_date_range_inputs(prefix))

    c1, c2 = st.columns(2)
    with c1:
        params["ven_cd"] = _txt(f"{prefix}_ven_cd", "거래처코드")
    with c2:
        params["ven_nm"] = _txt(f"{prefix}_ven_nm", "거래처명")

    c3, c4 = st.columns(2)
    with c3:
        params["trans_di"] = _txt(f"{prefix}_trans_di", "거래명세서구분")
    with c4:
        params["trans_seq"] = _txt(f"{prefix}_trans_seq", "거래명세서순번")

    params["top"] = _top_value(f"{prefix}_top", default_top)
    return params


def _base_params_140(prefix: str, default_top: int = 200) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    params.update(_date_range_inputs(prefix))

    c1, c2 = st.columns(2)
    with c1:
        params["ven_cd"] = _txt(f"{prefix}_ven_cd", "거래처코드")
    with c2:
        params["ven_nm"] = _txt(f"{prefix}_ven_nm", "거래처명")

    c3, c4 = st.columns(2)
    with c3:
        params["tax_di"] = _txt(f"{prefix}_tax_di", "세금계산서구분")
    with c4:
        params["tax_seq"] = _txt(f"{prefix}_tax_seq", "세금계산서순번")

    params["top"] = _top_value(f"{prefix}_top", default_top)
    return params


def _base_params_210(prefix: str, default_top: int = 200) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    params.update(_month_range_inputs(prefix))

    c1, c2 = st.columns(2)
    with c1:
        params["ven_cd"] = _txt(f"{prefix}_ven_cd", "거래처코드")
    with c2:
        params["ven_nm"] = _txt(f"{prefix}_ven_nm", "거래처명")

    params.update(_product_inputs(prefix))

    # 화면 표시 건수
    display_top = _top_value(f"{prefix}_top", default_top)
    params["display_top"] = display_top

    # 실제 DB 조회 건수는 별도 상한 사용
    fetch_top = _monthly_fetch_top_value()
    params["top"] = fetch_top
    params["_max_top"] = fetch_top

#   임시
    log.info(
        "[io.monthly.params] action=real display_top=%s fetch_top=%s top=%s max_top=%s",
        display_top,
        fetch_top,
        params.get("top"),
        params.get("_max_top"),
    )
#   임시

    return params

def _base_params_220(prefix: str, default_top: int = 200) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    params.update(_month_range_inputs(prefix))

    c1, c2 = st.columns(2)
    with c1:
        params["ven_cd"] = _txt(f"{prefix}_ven_cd", "거래처코드")
    with c2:
        params["ven_nm"] = _txt(f"{prefix}_ven_nm", "거래처명")

    params.update(_product_inputs(prefix))

    # 화면 표시 건수
    display_top = _top_value(f"{prefix}_top", default_top)
    params["display_top"] = display_top

    # 실제 DB 조회 건수는 별도 상한 사용
    fetch_top = _monthly_fetch_top_value()
    params["top"] = fetch_top
    params["_max_top"] = fetch_top

#   임시
    log.info(
        "[io.monthly.params] action=book display_top=%s fetch_top=%s top=%s max_top=%s",
        display_top,
        fetch_top,
        params.get("top"),
        params.get("_max_top"),
    )
#   임시
    return params

# ---------------------------------------------------------------------
# 거래처 후보검색 helper
# ---------------------------------------------------------------------
def _vendor_scope_where_sql(scope: str) -> str:
    """
    scope:
      - all              : 전체 거래처
      - maker            : 제약사                    10001 ~ 18999
      - purchase         : 매입처 전체               00001 ~ 3ZZZZ
      - account_purchase : 회계매입처                40000 ~ 4ZZZZ
      - sales            : 매출처 전체               50000 ~ 8ZZZZ
      - account_sales    : 회계매출처                90001 ~ 9ZZZZ
      - cost_apply       : 단가적용처(매출처 전체)   50000 ~ 8ZZZZ
      - stock_apply      : 재고적용처(매출처 전체)   50000 ~ 8ZZZZ
    """
    s = str(scope or "all").strip().lower()

    if s == "maker":
        return "RTRIM(Rd03_Ven_Cd) BETWEEN '10001' AND '18999'"

    if s == "purchase":
        return "RTRIM(Rd03_Ven_Cd) BETWEEN '00001' AND '3ZZZZ'"

    if s == "account_purchase":
        return "RTRIM(Rd03_Ven_Cd) BETWEEN '40000' AND '4ZZZZ'"

    if s in {"sales", "cost_apply", "stock_apply"}:
        return "RTRIM(Rd03_Ven_Cd) BETWEEN '50000' AND '8ZZZZ'"

    if s == "account_sales":
        return "RTRIM(Rd03_Ven_Cd) BETWEEN '90001' AND '9ZZZZ'"

    return "1=1"


def _lookup_vendor_candidates(name_text: str, scope: str = "all", limit: int = 30) -> list[tuple[str, str]]:
    name_text = (name_text or "").strip()
    if not name_text:
        return []

    scope_where = _vendor_scope_where_sql(scope)

    sql = f"""
    SELECT TOP (%(top)s)
        RTRIM(Rd03_Ven_Cd) AS ven_cd,
        RTRIM(Rd03_Ven_Nm) AS ven_nm
    FROM dbo.Rddbc030
    WHERE ({scope_where})
      AND RTRIM(Rd03_Ven_Nm) LIKE %(ven_nm_like)s
    ORDER BY
        CASE WHEN RTRIM(Rd03_Ven_Nm) = %(ven_nm_exact)s THEN 0 ELSE 1 END,
        RTRIM(Rd03_Ven_Nm),
        RTRIM(Rd03_Ven_Cd)
    """

    df = query_to_df(
        sql,
        {
            "top": limit,
            "ven_nm_like": f"%{name_text}%",
            "ven_nm_exact": name_text,
        },
    )
    if df is None or len(df) == 0:
        return []

    out: list[tuple[str, str]] = []
    for _, row in df.iterrows():
        cd = str(row.get("ven_cd", "")).strip()
        nm = str(row.get("ven_nm", "")).strip()
        if cd:
            out.append((cd, nm))
    return out



def _needs_vendor_pick(prefix: str, params: Dict[str, Any]) -> bool:
    p = dict(params)

    current_cd = str(p.get("ven_cd", "")).strip()
    current_nm = str(p.get("ven_nm", "")).strip()
    lookup_nm = str(st.session_state.get(f"{prefix}_vendor_lookup_name", "")).strip()
    raw = str(st.session_state.get(f"{prefix}_vendor_pick", "")).strip()
    rows = st.session_state.get(f"{prefix}_vendor_candidates") or []

    # 거래처코드를 직접 입력한 경우는 후보선택 불필요
    if current_cd:
        return False

    # 후보가 없으면 불필요
    if not rows:
        return False

    # 현재 입력명이 후보검색 당시 이름과 다르면 예전 후보는 무효
    if not lookup_nm or current_nm != lookup_nm:
        return False

    # 후보가 있는데 아직 선택 안 했으면 선택 필요
    return not (raw and " | " in raw)


def _clear_vendor_candidate_state(prefix: str) -> None:
    for k in [
        f"{prefix}_vendor_candidates",
        f"{prefix}_vendor_pick",
        f"{prefix}_vendor_msg",
        f"{prefix}_vendor_lookup_name",
        f"{prefix}_vendor_reset_pending",
    ]:
        st.session_state.pop(k, None)


def _maybe_reset_vendor_candidate_state(prefix: str) -> None:
    if st.session_state.get(f"{prefix}_vendor_reset_pending"):
        _clear_vendor_candidate_state(prefix)


def _store_vendor_candidates(prefix: str, ven_nm: str, scope: str = "all") -> None:
    ven_nm = (ven_nm or "").strip()
    _clear_vendor_candidate_state(prefix)

    _queue_vendor_input_sync(prefix, "", ven_nm)

    st.session_state[f"{prefix}_vendor_candidates"] = []
    st.session_state[f"{prefix}_vendor_pick"] = ""
    st.session_state[f"{prefix}_vendor_msg"] = ""
    st.session_state[f"{prefix}_vendor_lookup_name"] = ven_nm
    st.session_state[f"{prefix}_vendor_lookup_scope"] = scope
    st.session_state[f"{prefix}_vendor_reset_pending"] = False

    if not ven_nm:
        st.session_state[f"{prefix}_vendor_msg"] = "거래처명을 입력하세요."
        return

    rows = _lookup_vendor_candidates(ven_nm, scope=scope)
    st.session_state[f"{prefix}_vendor_candidates"] = rows

    if rows:
        st.session_state[f"{prefix}_vendor_pick"] = ""
        st.session_state[f"{prefix}_vendor_msg"] = f"거래처 후보 {len(rows)}건. 후보선택에서 선택하세요."
    else:
        st.session_state[f"{prefix}_vendor_pick"] = ""
        st.session_state[f"{prefix}_vendor_msg"] = "거래처 후보가 없습니다."

        
def _apply_vendor_pick(prefix: str, params: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(params)

    current_cd = str(p.get("ven_cd", "")).strip()
    current_nm = str(p.get("ven_nm", "")).strip()
    lookup_nm = str(st.session_state.get(f"{prefix}_vendor_lookup_name", "")).strip()
    raw = st.session_state.get(f"{prefix}_vendor_pick", "")

    # 코드를 직접 입력한 경우에는 후보선택 무시
    if current_cd:
        return p

    # 후보검색 당시 이름과 현재 입력 이름이 다르면, 예전 후보선택은 무효
    if lookup_nm and current_nm != lookup_nm:
        return p

    if raw and " | " in raw:
        cd, nm = raw.split(" | ", 1)
        p["ven_cd"] = cd.strip()
        p["ven_nm"] = nm.strip()

    return p


def _empty_result_payload(title: str, params: Dict[str, Any], msg: str) -> Dict[str, Any]:
    action_text = str(title or "").strip()

    default_msg = (
        "검증 결과 이상 자료가 없습니다."
        if "검증" in action_text
        else "해당 조회조건의 자료가 없습니다."
    )

    message = str(msg or "").strip()
    if (
        not message
        or message in {"", "해당 조회조건의 자료가 없습니다.", "해당 조회조건의 자료가 없습니다."}
    ):
        message = default_msg

    def _condition_text_from_params(p: Dict[str, Any]) -> str:
        try:
            labels = {
                "date_from": "시작일자", "date_to": "종료일자",
                "month_from": "시작월", "month_to": "종료월",
                "physic_cd": "제품코드", "physic_nm": "제품명",
                "ven_cd": "거래처코드", "ven_nm": "거래처명",
                "stock_cd": "재고위치", "stock_nm": "재고위치명",
                "stock_label_text": "재고위치",
                "trans_di": "거래명세서구분", "tax_di": "세금계산서구분",
            }
            parts = []
            for key, label in labels.items():
                value = p.get(key)
                if isinstance(value, (list, tuple, set)):
                    value = ", ".join(str(x).strip() for x in value if str(x).strip())
                value = str(value or "").strip()
                if value:
                    parts.append(f"{label} {value}")
            return " / ".join(parts) if parts else "전체"
        except Exception:
            return "전체"

    condition_text = _condition_text_from_params(params or {})

    return {
        "title": title,
        "action": title,
        "params": params,
        "data": message,
        "message": message,
        "type": "text",
        "final": True,
        "meta": {
            "action": title,
            "empty_result": True,
            "row_count": 0,
            "row_count_total": 0,
            "display_row_count": 0,
            "download_row_count": 0,
            "condition": condition_text,
            "query_summary": condition_text,
            "message": message,
            "_force_push": True,
        },
    }

#@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
_IO_ROWNO_CANDIDATES = ["조회순번", "순번", "No", "NO", "no", "번호"]

_IO_CODE_NAME_HINTS = [
    "코드", "_cd", "cd_", "_code", "code_",
    "gcode", "tcode",
    "vendor_cd", "ven_cd", "physic_cd", "user_cd",
]

_IO_DATE_NAME_HINTS = [
    "일자", "날짜", "date", "yymm", "yyyymm", "yyyymmdd",
    "년월", "기간", "유효기한", "만료", "expiry", "expire",
]

_IO_DECIMAL_NAME_HINTS = [
    "단가", "dc율", "할인율", "dc_rate", "discount_rate",
    "rate", "ratio", "percent", "퍼센트", "비율",
    "unit_cost", "unit_rate", "cost_rate", "insu_price",
]

_IO_NUMERIC_NAME_HINTS = [
    "수량", "금액", "단가", "dc율", "할인율", "보험약가", "보험가", "가격",
]

_IO_TEXTISH_NAME_HINTS = [
    "거래처", "처명", "사원", "적요", "비고", "주소", "전화", "우편",
    "대표자", "사업자", "세금계산서", "명세서", "매입처",
    "실납품처", "실납처", "재고위치", "제품명", "규격", "포장단위",
    "제조번호",
]


def _normalize_col_key(name: Any) -> str:
    return (
        str(name or "")
        .strip()
        .lower()
        .replace(" ", "")
        .replace("-", "")
        .replace("__", "_")
    )


def _has_name_hint(col_name: str, hints: list[str]) -> bool:
    key = _normalize_col_key(col_name)
    return any(h.lower().replace(" ", "") in key for h in hints)


def _looks_code_col(col_name: str) -> bool:
    key = _normalize_col_key(col_name)

    # 코드/식별자 성격 컬럼
    if _has_name_hint(col_name, _IO_CODE_NAME_HINTS):
        return True

    # 입출고구분앞자리 같은 파생 문자 코드 컬럼
    if "앞자리" in str(col_name):
        return True

    # 접두/접미 한 자리 구분값도 문자로 유지
    if key.endswith("prefix") or key.endswith("front"):
        return True

    return False


def _looks_date_col(col_name: str) -> bool:
    return _has_name_hint(col_name, _IO_DATE_NAME_HINTS)


def _looks_textish_label_col(col_name: str) -> bool:
    return _has_name_hint(col_name, _IO_TEXTISH_NAME_HINTS)


def _looks_decimal_col(col_name: str) -> bool:
    if _looks_code_col(col_name) or _looks_textish_label_col(col_name):
        return False
    return _has_name_hint(col_name, _IO_DECIMAL_NAME_HINTS)


def _looks_numeric_metric_col(col_name: str) -> bool:
    if _looks_code_col(col_name) or _looks_date_col(col_name):
        return False
    if _looks_textish_label_col(col_name):
        return False
    return _has_name_hint(col_name, _IO_NUMERIC_NAME_HINTS)


def _normalize_zero_numeric_series(sr: pd.Series) -> pd.Series:
    return sr.apply(
        lambda v: 0 if (pd.notna(v) and abs(float(v)) < 1e-12) else v
    )


def _has_row_number_col(df: pd.DataFrame) -> bool:
    return any(col in df.columns for col in _IO_ROWNO_CANDIDATES)


def _clean_object_series(sr: pd.Series) -> pd.Series:
    text = sr.fillna("").astype(str).str.strip()
    return text.replace(
        {
            "None": "",
            "none": "",
            "nan": "",
            "NaN": "",
            "<NA>": "",
            "NaT": "",
            "nat": "",
            "NULL": "",
            "null": "",
        }
    )


def _normalize_code_cell(value: Any) -> str:
    if pd.isna(value):
        return ""

    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m-%d")

    text = str(value).strip()
    if text in {"", "None", "none", "nan", "NaN", "<NA>", "NaT", "nat", "NULL", "null"}:
        return ""

    text = text.replace(",", "")

    if "." in text:
        left, right = text.split(".", 1)
        if right.isdigit() and set(right) <= {"0"}:
            text = left

    return text.strip()


def _series_to_code_text(sr: pd.Series) -> pd.Series:
    return sr.apply(_normalize_code_cell).astype(str)


def _normalize_date_cell(value: Any) -> str:
    if pd.isna(value):
        return ""

    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m-%d")

    text = str(value).strip()
    if text in {"", "None", "none", "nan", "NaN", "<NA>", "NaT", "nat", "NULL", "null"}:
        return ""

    digits = "".join(ch for ch in text if ch.isdigit())

    if digits in {"0", "00", "0000", "000000", "00000000"}:
        return ""

    if len(digits) == 8:
        try:
            d = dt.datetime.strptime(digits, "%Y%m%d").date()
            if d.year <= 1901:
                return ""
            return d.strftime("%Y-%m-%d")
        except Exception:
            return ""

    if len(digits) == 6:
        yyyy = digits[:4]
        mm = digits[4:6]
        if yyyy == "0000" or mm == "00":
            return ""
        return f"{yyyy}-{mm}"

    if len(digits) == 4:
        if digits == "0000":
            return ""
        return digits

    return text


def _series_to_date_text(sr: pd.Series) -> pd.Series:
    return sr.apply(_normalize_date_cell).astype(str)


def _maybe_to_numeric(sr: pd.Series, col_name: str):
    if _looks_code_col(col_name):
        return _series_to_code_text(sr)

    if _looks_date_col(col_name):
        return _series_to_date_text(sr)

    if is_datetime64_any_dtype(sr):
        return sr

    if is_numeric_dtype(sr):
        return _normalize_zero_numeric_series(sr)

    text = _clean_object_series(sr)
    if text.empty:
        return text

    probe = (
        text
        .str.replace(",", "", regex=False)
        .str.replace("%", "", regex=False)
        .str.replace(r"^\((.*)\)$", r"-\1", regex=True)
    )

    numeric = pd.to_numeric(probe, errors="coerce")
    numeric = _normalize_zero_numeric_series(numeric)

    non_blank = text != ""

    if int(non_blank.sum()) == 0:
        return text

    if _looks_numeric_metric_col(col_name):
        return numeric

    ratio = float(numeric[non_blank].notna().mean())
    return numeric if ratio >= 0.8 else text


def _prepare_io_display_df(
    df: Any,
    *,
    add_row_no: bool = True,
) -> pd.DataFrame:
    work = _ensure_df(df)
    if work.empty:
        return work

    work = work.reset_index(drop=True)

    def _looks_datetime_display_col(col_name: str) -> bool:
        raw = str(col_name or "").strip()
        key = _normalize_col_key(col_name)
        return (
            ("시간" in raw)
            or key.endswith("time")
            or ("_time" in key)
            or ("datetime" in key)
            or ("timestamp" in key)
        )

    for col in list(work.columns):
        col_name = str(col)
        sr = work[col]

        if _looks_code_col(col_name):
            work[col] = _series_to_code_text(sr)
            continue

        if is_datetime64_any_dtype(sr):
            if _looks_datetime_display_col(col_name):
                work[col] = sr.dt.strftime("%Y-%m-%d %H:%M:%S").fillna("")
            else:
                work[col] = sr.dt.strftime("%Y-%m-%d").fillna("")
            continue

        if _looks_date_col(col_name):
            work[col] = _series_to_date_text(sr)
            continue

        work[col] = _maybe_to_numeric(sr, col_name)

    object_cols = work.select_dtypes(include=["object", "string"]).columns
    for col in object_cols:
        work[col] = _clean_object_series(work[col])

    numeric_cols = work.select_dtypes(include=["number"]).columns
    for col in numeric_cols:
        s = pd.to_numeric(work[col], errors="coerce")
        s = _normalize_zero_numeric_series(s)
        work[col] = s.fillna(0)

    if add_row_no and not _has_row_number_col(work):
        work.insert(0, "조회순번", range(1, len(work) + 1))

    return work

def _build_io_format_map(df: pd.DataFrame) -> Dict[str, Any]:
    fmt: Dict[str, Any] = {}

    def _fmt_seq(v: Any) -> str:
        if pd.isna(v):
            return ""

        text = str(v).strip()
        if not text:
            return ""

        try:
            n = float(text)
        except Exception:
            return text

        if abs(n) < 1e-12:
            return ""

        return f"{int(n)}"

    def _fmt_decimal(v: Any) -> str:
        if pd.isna(v):
            return ""
        n = float(v)
        if abs(n) < 1e-12:
            return ""
        return f"{n:,.2f}"

    def _fmt_integer(v: Any) -> str:
        if pd.isna(v):
            return ""
        n = float(v)
        if abs(n) < 1e-12:
            return ""
        return f"{n:,.0f}"

    for col in df.columns:
        col_name = str(col)
        sr = df[col]

        if col_name in _IO_ROWNO_CANDIDATES:
            fmt[col] = _fmt_seq
            continue

        if _looks_code_col(col_name):
            continue
        if not is_numeric_dtype(sr):
            continue

        if _looks_decimal_col(col_name):
            fmt[col] = _fmt_decimal
        else:
            fmt[col] = _fmt_integer

    return fmt

#   @@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
def _build_io_alignment_df(df: pd.DataFrame) -> pd.DataFrame:
    style_df = pd.DataFrame("", index=df.index, columns=df.columns)

    for col in df.columns:
        col_name = str(col)

        if _looks_code_col(col_name) or _looks_date_col(col_name):
            align = "center"
        elif is_numeric_dtype(df[col]):
            align = "right"
        else:
            align = "left"

        style_df[col] = f"text-align: {align};"

    return style_df


def _build_io_negative_df(df: pd.DataFrame) -> pd.DataFrame:
    style_df = pd.DataFrame("", index=df.index, columns=df.columns)

    for col in df.columns:
        col_name = str(col)
        if _looks_code_col(col_name):
            continue
        if not is_numeric_dtype(df[col]):
            continue

        style_df[col] = df[col].apply(
            lambda v: "color: #d92d20; font-weight: 600;"
            if pd.notna(v) and float(v) < 0
            else ""
        )

    return style_df


def _build_io_banding_df(df: pd.DataFrame, band_size: int = 5) -> pd.DataFrame:
    style_df = pd.DataFrame("", index=df.index, columns=df.columns)

    if band_size <= 0:
        return style_df

    for i in range(len(df)):
        if (i // band_size) % 2 == 1:
            style_df.iloc[i, :] = "background-color: #fafafa;"

    return style_df


def _build_io_display_styler(
    df: Any,
    *,
    add_row_no: bool = True,
    band_size: int = 5,
):
    work = _prepare_io_display_df(df, add_row_no=add_row_no)

    styler = work.style.format(_build_io_format_map(work), na_rep="")
    styler = styler.apply(lambda _: _build_io_alignment_df(work), axis=None)
    styler = styler.apply(lambda _: _build_io_negative_df(work), axis=None)
    styler = styler.apply(lambda _: _build_io_banding_df(work, band_size=band_size), axis=None)
    styler = styler.set_properties(**{"white-space": "nowrap"})

    return styler


def _render_io_dataframe(
    df: Any,
    *,
    key: str,
    add_row_no: bool = True,
    band_size: int = 5,
    use_container_width: bool = True,
    hide_index: bool = True,
    height: Optional[int] = None,
) -> pd.DataFrame:
    work = _prepare_io_display_df(df, add_row_no=add_row_no)

    st.dataframe(
        _build_io_display_styler(work, add_row_no=False, band_size=band_size),
        use_container_width=use_container_width,
        hide_index=hide_index,
        height=height,
        key=key,
    )

    return work

# ---------------------------------------------------------------------
# 입출고 공통 로컬 필터 / payload 후처리 helper
# ---------------------------------------------------------------------
_IO110_LOCAL_FILTER_ALIAS_MAP: Dict[str, list[str]] = {
    "in_seq": [
        "입고순번", "입고순서", "입고번호",
        "Rd11_In_Seq", "IN_SEQ", "in_seq",
    ],
    "trans_seq": [
        "거래명세서순번", "거래명세순번", "명세서순번",
        "Rd11_Trans_Seq", "TRANS_SEQ", "trans_seq",
    ],
    "tax_seq": [
        "세금계산서순번", "세금계산순번", "계산서순번",
        "Rd11_Tax_Seq", "TAX_SEQ", "tax_seq",
    ],
    "stock_cd": [
        "재고위치코드", "재고코드", "위치코드", "창고코드", "로케이션코드",
        "Rd11_Stock_Cd", "STOCK_CD", "stock_cd",
        "Loc_Cd", "LOC_CD", "loc_cd",
        "Location_Cd", "LOCATION_CD", "location_cd",
    ],
    "stock_nm": [
        "재고위치명", "재고위치", "위치명", "창고명", "창고", "보관위치", "로케이션",
        "STOCK_NM", "stock_nm",
        "Loc_Nm", "LOC_NM", "loc_nm",
        "Location_Nm", "LOCATION_NM", "location_nm",
    ],
}

_IO120_LOCAL_FILTER_ALIAS_MAP: Dict[str, list[str]] = {
    "out_seq": [
        "출고순번", "출고순서", "출고번호",
        "Rd12_Out_Seq", "OUT_SEQ", "out_seq",
    ],
    "trans_seq": [
        "거래명세서순번", "거래명세순번", "명세서순번",
        "Rd12_Trans_Seq", "TRANS_SEQ", "trans_seq",
    ],
    "tax_seq": [
        "세금계산서순번", "세금계산순번", "계산서순번",
        "Rd12_Tax_Seq", "TAX_SEQ", "tax_seq",
    ],
    "stock_cd": [
        "재고위치코드", "재고코드", "위치코드", "창고코드", "로케이션코드",
        "Rd12_Stock_Cd", "STOCK_CD", "stock_cd",
        "Loc_Cd", "LOC_CD", "loc_cd",
        "Location_Cd", "LOCATION_CD", "location_cd",
    ],
    "stock_nm": [
        "재고위치명", "재고위치", "위치명", "창고명", "창고", "보관위치", "로케이션",
        "STOCK_NM", "stock_nm",
        "Loc_Nm", "LOC_NM", "loc_nm",
        "Location_Nm", "LOCATION_NM", "location_nm",
    ],
}


def _find_first_existing_col(df: pd.DataFrame, names: list[str]) -> str:
    for name in names:
        if name in df.columns:
            return name
    return ""


def _filter_df_by_alias_map(
    df: pd.DataFrame | None,
    filters: Dict[str, str],
    alias_map: Dict[str, list[str]],
    contains_keys: set[str] | None = None,
) -> pd.DataFrame | None:
    if df is None or df.empty:
        return df

    out = df.copy()
    contains_keys = contains_keys or set()

    for filter_key, aliases in alias_map.items():
        wanted = str(filters.get(filter_key, "")).strip()
        if not wanted:
            continue

        col = _find_first_existing_col(out, aliases)
        if not col:
            continue

        s = out[col].astype(str).str.strip()

        if filter_key in contains_keys:
            mask = s.str.upper().str.contains(wanted.upper(), regex=False, na=False)
        else:
            mask = s == wanted

        out = out.loc[mask].copy()
        if out.empty:
            break

    return out


def _apply_local_filters_to_payload(
    payload: Dict[str, Any],
    filters: Dict[str, str],
    final_params: Dict[str, Any],
    title: str,
    alias_map: Dict[str, list[str]],
    contains_keys: set[str] | None = None,
) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return payload

    has_any_filter = any(str(filters.get(k, "")).strip() for k in alias_map.keys())
    if not has_any_filter:
        return payload

    df = payload.get("df")
    df_display = payload.get("df_display")
    records = payload.get("records")

    row_count = None

    if isinstance(df, pd.DataFrame):
        original_df = df
        filtered_df = _filter_df_by_alias_map(
            original_df,
            filters,
            alias_map,
            contains_keys=contains_keys,
        )
        payload["df"] = filtered_df

        if isinstance(df_display, pd.DataFrame):
            try:
                if filtered_df is not None and len(df_display) == len(original_df):
                    payload["df_display"] = df_display.loc[filtered_df.index].copy()
                else:
                    payload["df_display"] = _filter_df_by_alias_map(
                        df_display,
                        filters,
                        alias_map,
                        contains_keys=contains_keys,
                    )
            except Exception:
                payload["df_display"] = _filter_df_by_alias_map(
                    df_display,
                    filters,
                    alias_map,
                    contains_keys=contains_keys,
                )

        row_count = 0 if filtered_df is None else int(len(filtered_df))

    elif isinstance(df_display, pd.DataFrame):
        filtered_display = _filter_df_by_alias_map(
            df_display,
            filters,
            alias_map,
            contains_keys=contains_keys,
        )
        payload["df_display"] = filtered_display
        row_count = 0 if filtered_display is None else int(len(filtered_display))

    elif isinstance(records, list):
        rows_df = pd.DataFrame(records)
        filtered_rows_df = _filter_df_by_alias_map(
            rows_df,
            filters,
            alias_map,
            contains_keys=contains_keys,
        )
        filtered_records = [] if filtered_rows_df is None else filtered_rows_df.to_dict(orient="records")
        payload["records"] = filtered_records

        if isinstance(payload.get("columns"), list):
            payload["columns"] = list(filtered_rows_df.columns) if filtered_rows_df is not None else []

        row_count = len(filtered_records)

    if row_count is None:
        return payload

    if isinstance(payload.get("meta"), dict):
        payload["meta"]["row_count"] = row_count

    if row_count == 0:
        return _empty_result_payload(title, final_params, "")

    return payload


def _payload_is_empty(payload: Dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return True

    df_display = payload.get("df_display")
    if isinstance(df_display, pd.DataFrame):
        return df_display.empty

    df = payload.get("df")
    if isinstance(df, pd.DataFrame):
        return df.empty

    records = payload.get("records")
    if isinstance(records, list):
        return len(records) == 0

    data = payload.get("data")
    if isinstance(data, str) and "조회 결과가 없습니다" in data:
        return True

    return False

# ---------------------------------------------------------------------
# 입출고 화면 공통 rerun / payload helper
# ---------------------------------------------------------------------
def _prepare_panel_rerun_for_inner_submit() -> None:
    st.session_state["__sims_run_flag"] = False
    st.session_state["__sims_inner_submit"] = True
    st.session_state["__sims_panel_active"] = True
    st.session_state["__sims_last_render_run_seq"] = -1


def _rerun_panel_for_inner_submit() -> None:
    _prepare_panel_rerun_for_inner_submit()
    st.rerun()


def _clear_payload_key(payload_key: str) -> None:
    st.session_state.pop(payload_key, None)

# ---------------------------------------------------------------------
# 재고위치 코드 조회 / payload fallback helper
# ---------------------------------------------------------------------
def _lookup_stock_codes_by_name(name_text: str, limit: int = 50) -> list[str]:
    name_text = (name_text or "").strip()
    if not name_text:
        return []

    sql = """
    SELECT TOP (%(top)s)
        RTRIM(Rd01_Gcode) AS stock_gcode,
        RTRIM(Rd01_Tcode) AS stock_cd,
        RTRIM(Rd01_Hnm)   AS stock_nm
    FROM dbo.Rddbc010
    WHERE RTRIM(Rd01_Hnm) LIKE %(name_like)s
    ORDER BY
        CASE WHEN RTRIM(Rd01_Hnm) = %(name_exact)s THEN 0 ELSE 1 END,
        CASE WHEN RTRIM(Rd01_Gcode) = '0018' THEN 0 ELSE 1 END,
        RTRIM(Rd01_Hnm),
        RTRIM(Rd01_Tcode)
    """
    df = query_to_df(
        sql,
        {
            "top": limit,
            "name_like": f"%{name_text}%",
            "name_exact": name_text,
        },
    )
    if df is None or df.empty:
        return []

    out: list[str] = []
    for _, row in df.iterrows():
        cd = str(row.get("stock_cd", "")).strip()
        if cd and cd not in out:
            out.append(cd)
    return out


def _apply_code_candidate_fallback(
    payload: Dict[str, Any],
    code_candidates: list[str],
    final_params: Dict[str, Any],
    title: str,
    code_aliases: list[str],
    name_aliases: list[str],
) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    if not code_candidates:
        return payload

    def _filter_df(df: pd.DataFrame | None) -> pd.DataFrame | None:
        if df is None or df.empty:
            return df

        name_col = _find_first_existing_col(df, name_aliases)
        code_col = _find_first_existing_col(df, code_aliases)

        # 이름 컬럼이 있으면 앞 단계 contains 필터가 이미 처리했으므로 그대로 둔다.
        if name_col:
            return df

        if not code_col:
            return df

        s = df[code_col].astype(str).str.strip()
        return df.loc[s.isin(code_candidates)].copy()

    row_count = None

    df = payload.get("df")
    df_display = payload.get("df_display")
    records = payload.get("records")

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
        return _empty_result_payload(title, final_params, "")

    return payload

# ---------------------------------------------------------------------
# 입출고 공통 service param 보정 / payload 후처리 helper
# ---------------------------------------------------------------------
def _prepare_io_service_params(
    base_params: Dict[str, Any],
    requested_filters: Dict[str, str],
    seq_keys: list[str],
    stock_name_key: str = "stock_nm",
    stock_code_key: str = "stock_cd",
    base_top: int = 3000,
    stock_name_top: int = 10000,
) -> tuple[Dict[str, Any], list[str]]:
    service_p = dict(base_params)

    if any(str(requested_filters.get(k, "")).strip() for k in requested_filters.keys()):
        try:
            service_p["top"] = max(int(service_p.get("top", 200)), base_top)
        except Exception:
            service_p["top"] = base_top

    stock_code_candidates: list[str] = []

    stock_nm = str(requested_filters.get(stock_name_key, "")).strip()
    stock_cd = str(requested_filters.get(stock_code_key, "")).strip()

    # 멀티선택 화면 보정:
    # base_params 에는 stock_nm 이 "창고A, 창고B" 형태로 남아 있을 수 있다.
    # 이 값이 service query 로 넘어가면 오히려 조회가 막히므로,
    # 멀티선택(2건 이상)일 때는 service 단계의 stock 필터를 비우고
    # 화면 후처리(payload 후필터)에서 stock_cds 로 걸러낸다.
    raw_stock_cds = base_params.get("stock_cds", [])
    if isinstance(raw_stock_cds, list):
        multi_stock_cds = [str(x).strip() for x in raw_stock_cds if str(x).strip()]
    else:
        multi_stock_cds = []

    if len(multi_stock_cds) > 1:
        service_p[stock_code_key] = ""
        service_p[stock_name_key] = ""

        try:
            service_p["top"] = max(int(service_p.get("top", 200)), stock_name_top)
        except Exception:
            service_p["top"] = stock_name_top

    elif stock_nm and not stock_cd:
        stock_code_candidates = _lookup_stock_codes_by_name(stock_nm)
        service_p[stock_code_key] = ""
        service_p[stock_name_key] = ""

        try:
            service_p["top"] = max(int(service_p.get("top", 200)), stock_name_top)
        except Exception:
            service_p["top"] = stock_name_top

    if any(str(requested_filters.get(k, "")).strip() for k in seq_keys):
        service_p["date_from"] = ""
        service_p["date_to"] = ""

    return service_p, stock_code_candidates

def _finalize_io_payload(
    payload: Dict[str, Any],
    requested_filters: Dict[str, str],
    final_params: Dict[str, Any],
    title: str,
    alias_map: Dict[str, list[str]],
    contains_keys: set[str] | None = None,
    stock_code_candidates: list[str] | None = None,
    stock_code_key: str = "stock_cd",
    stock_name_key: str = "stock_nm",
) -> Dict[str, Any]:
    local_filters = dict(requested_filters)

    stock_nm = str(requested_filters.get(stock_name_key, "")).strip()
    stock_cd = str(requested_filters.get(stock_code_key, "")).strip()

    # 재고위치명만 입력한 경우:
    # service 단계에서는 이름으로 후보코드를 찾고 넓게 조회했으므로,
    # 로컬 1차 필터에서는 stock_nm 을 다시 걸지 않는다.
    # 이후 code candidate fallback 으로 stock_cd 기준 후처리한다.
    if stock_code_candidates and stock_nm and not stock_cd:
        local_filters[stock_name_key] = ""

    payload = _apply_local_filters_to_payload(
        payload=payload,
        filters=local_filters,
        final_params=final_params,
        title=title,
        alias_map=alias_map,
        contains_keys=contains_keys,
    )

    if stock_code_candidates:
        payload = _apply_code_candidate_fallback(
            payload=payload,
            code_candidates=stock_code_candidates,
            final_params=final_params,
            title=title,
            code_aliases=alias_map[stock_code_key],
            name_aliases=alias_map[stock_name_key],
        )

    if _payload_is_empty(payload):
        payload = _empty_result_payload(title, final_params, "")

    return payload

# ---------------------------------------------------------------------
# 입출고 화면 공통 후보검색 row 렌더 helper
# ---------------------------------------------------------------------
def _render_vendor_candidate_row(
    prefix: str,
    code_label: str = "거래처코드",
    name_label: str = "거래처명",
    search_label: str = "후보검색",
    pick_label: str = "거래처 후보선택",
) -> tuple[str, str, bool]:
    c1, c2, c3, c4 = st.columns([2, 4, 1.4, 4])
    with c1:
        ven_cd = _txt(f"{prefix}_ven_cd", code_label)
    with c2:
        ven_nm = _txt(f"{prefix}_ven_nm", name_label)
    with c3:
        vendor_search = st.form_submit_button(
            search_label,
            use_container_width=True,
            on_click=_trigger_panel_inner_submit,
        )
    with c4:
        vendor_rows = st.session_state.get(f"{prefix}_vendor_candidates", []) or []
        vendor_options = [""] + [f"{cd} | {nm}" for cd, nm in vendor_rows]
        st.selectbox(
            pick_label,
            options=vendor_options,
            key=f"{prefix}_vendor_pick",
        )

    vendor_msg = st.session_state.get(f"{prefix}_vendor_msg", "")
    if vendor_msg:
        st.caption(vendor_msg)

    return ven_cd, ven_nm, vendor_search


def _render_product_candidate_row(
    prefix: str,
    code_label: str = "제품코드",
    name_label: str = "제품명",
    search_label: str = "제품 후보검색",
    pick_label: str = "제품 후보선택",
) -> tuple[str, str, bool]:
    c1, c2, c3, c4 = st.columns([2, 4, 1.4, 4])
    with c1:
        physic_cd = _txt(f"{prefix}_physic_cd", code_label)
    with c2:
        physic_nm = _txt(f"{prefix}_physic_nm", name_label)
    with c3:
        product_search = st.form_submit_button(
            search_label,
            use_container_width=True,
            on_click=_trigger_panel_inner_submit,
        )
    with c4:
        product_rows = st.session_state.get(f"{prefix}_product_candidates", []) or []
        product_options = [""] + [f"{cd} | {nm}" for cd, nm in product_rows]
        st.selectbox(
            pick_label,
            options=product_options,
            key=f"{prefix}_product_pick",
        )

    product_msg = st.session_state.get(f"{prefix}_product_msg", "")
    if product_msg:
        st.caption(product_msg)

    return physic_cd, physic_nm, product_search



