# app/ui/chat_middleware.py (unified, safe drop-in)
# VERSION = "chat_middleware/2025-11-16T-fixed"
# -*- coding: utf-8 -*-
#VERSION = "vendors/2025-12-14-001"
#VERSION = "vendors/2026-05-03-001"
#VERSION = "vendors/2026-05-03-002"
from __future__ import annotations
from typing import Any, Dict, Optional, Iterable, Tuple, List
import os
import io, json, hashlib, logging
import uuid
import streamlit as st
import pandas as pd
import re
try:
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
except Exception:
    ILLEGAL_CHARACTERS_RE = re.compile(r"[\x00-\x08\x0B-\x0C\x0E-\x1F]")
    
from app.sims.views.rddbc_io_shared import (
    _prepare_io_display_df,
    _build_io_display_styler,
)

from app.ui.sims_table_display import (
    build_sims_table_display_config,
    normalize_display_df_for_streamlit,
)

import datetime as dt
import time

log = logging.getLogger("ssai")


def _safe_log_value(value: Any, limit: int = 120) -> str:
    try:
        s = str(value if value is not None else "").replace("\n", " ").replace("\r", " ").strip()
    except Exception:
        s = ""
    if len(s) > limit:
        return s[:limit].rstrip() + "..."
    return s


def _chat_runtime_log_context(room: Optional[Dict[str, Any]] = None, **extra: Any) -> Dict[str, Any]:
    """
    chat_middleware는 메인과 로그인 모듈 사이에 있으므로,
    import cycle을 피하려고 로그인 정보는 함수 내부에서 lazy import한다.
    """
    try:
        from app.ui.ssai_login import get_current_user, get_selected_company
        user = get_current_user()
        company = get_selected_company()
    except Exception:
        user = None
        company = None

    if room is None:
        try:
            room = _get_current_room_from_session()
        except Exception:
            room = None

    ctx: Dict[str, Any] = {
        "user_id": getattr(user, "user_id", None),
        "login_id": getattr(user, "login_id", None),
        "user_type": getattr(user, "user_type", None),
        "user_grade": getattr(user, "user_grade", None),
        "company_id": None,
        "company_name": "",
        "db_name": "",
        "room_id": "",
    }

    if isinstance(company, dict):
        ctx.update(
            {
                "company_id": company.get("company_id"),
                "company_name": company.get("company_name"),
                "db_name": company.get("db_name"),
            }
        )

    if isinstance(room, dict):
        ctx["room_id"] = room.get("id") or ""

    ctx.update(extra)
    return ctx


def _chat_runtime_log_kv(room: Optional[Dict[str, Any]] = None, **extra: Any) -> str:
    ctx = _chat_runtime_log_context(room, **extra)
    order = [
        "user_id",
        "login_id",
        "user_type",
        "user_grade",
        "company_id",
        "company_name",
        "db_name",
        "room_id",
    ]
    order += [k for k in ctx.keys() if k not in order]

    return " ".join(
        f"{key}={_safe_log_value(ctx.get(key))}"
        for key in order
        if ctx.get(key) not in (None, "")
    )



def _chat_log_info_once(key: str, msg: str, *args: Any) -> None:
    """
    채팅 렌더 rerun에서 같은 INFO 로그가 반복되는 것을 줄인다.

    - 첫 발생은 INFO로 남긴다.
    - 같은 key의 반복 발생은 DEBUG로 낮춘다.
    - 기능 동작은 바꾸지 않고 로그 노이즈만 줄인다.
    """
    try:
        skey = str(key or "").strip()
        if not skey:
            log.info(msg, *args)
            return

        ss = st.session_state
        seen = ss.get("__chat_info_log_once_seen")
        if not isinstance(seen, dict):
            seen = {}

        if skey in seen:
            log.debug(msg, *args)
            return

        log.info(msg, *args)
        seen[skey] = time.time()

        # 장시간 사용 시 session_state가 무한히 커지지 않도록 오래된 key를 정리한다.
        if len(seen) > 700:
            try:
                old_keys = sorted(seen, key=lambda k: float(seen.get(k) or 0))[:200]
                for old_key in old_keys:
                    seen.pop(old_key, None)
            except Exception:
                pass

        ss["__chat_info_log_once_seen"] = seen

    except Exception:
        # 로그 억제 유틸 자체가 실패해도 기존 INFO 로그는 유지한다.
        log.info(msg, *args)



# ---------------------------------------------------------------------
# Company stamp guard
# ---------------------------------------------------------------------
def _chat_current_company_sig() -> tuple[str, str]:
    try:
        from app.ui.ssai_login import get_selected_company
        company = get_selected_company()
    except Exception:
        company = None

    if not isinstance(company, dict):
        return "", ""

    return (
        str(company.get("company_id") or "").strip(),
        str(company.get("db_name") or "").strip(),
    )


def _chat_payload_company_sig(payload: Dict[str, Any] | None) -> tuple[str, str]:
    if not isinstance(payload, dict):
        return "", ""

    try:
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        return (
            str(meta.get("_ssai_company_id") or payload.get("_ssai_company_id") or "").strip(),
            str(meta.get("_ssai_db_name") or payload.get("_ssai_db_name") or "").strip(),
        )
    except Exception:
        return "", ""


def _chat_payload_matches_current_company(payload: Dict[str, Any] | None) -> bool:
    payload_company_id, payload_db_name = _chat_payload_company_sig(payload)

    # stamp 없는 legacy payload는 여기서 막지 않는다.
    # 회사 변경 루틴에서 legacy cache를 제거한다.
    if not payload_company_id and not payload_db_name:
        return True

    current_company_id, current_db_name = _chat_current_company_sig()

    if payload_company_id and current_company_id and payload_company_id != current_company_id:
        return False

    if payload_db_name and current_db_name and payload_db_name != current_db_name:
        return False

    return True


# ---------------------------------------------------------------------
# DataFrame 안전 추출/직렬화 유틸
# ---------------------------------------------------------------------
def _pick_df_from_payload(payload: Dict[str, Any]) -> Optional[pd.DataFrame]:
    """payload 내 df/df_display/data/records/meta(df)에서 DataFrame을 안전하게 추출한다."""
    try:
        v = payload.get("df_display")
        if isinstance(v, pd.DataFrame):
            return v

        v = payload.get("df")
        if isinstance(v, pd.DataFrame):
            return v

        v = payload.get("data")
        if isinstance(v, pd.DataFrame):
            return v

        # 1) top-level records/columns 우선 복원
        records = payload.get("records")
        columns = payload.get("columns")
        if isinstance(records, list) and isinstance(columns, list):
            try:
                return pd.DataFrame.from_records(records, columns=columns)
            except Exception:
                try:
                    return pd.DataFrame(records)
                except Exception:
                    pass

        # 2) meta 내부 records/columns 복원
        meta = payload.get("meta") or {}
        records = meta.get("df")
        columns = meta.get("columns")
        if isinstance(records, list) and isinstance(columns, list):
            try:
                return pd.DataFrame.from_records(records, columns=columns)
            except Exception:
                try:
                    return pd.DataFrame(records)
                except Exception:
                    pass
    except Exception:
        pass
    return None

# DataFrame 내 문자열에서 Excel에서 사용할 수 없는 문자를 제거한다.
# (엑셀로 다운로드할 때 오류 방지용)
# openpyxl의 ILLEGAL_CHARACTERS_RE를 사용하되, openpyxl이 설치되어 있지 않은 경우에도 최소한의 대체 패턴을 제공한다.
def _sanitize_excel_value(v):
    if v is None:
        return v
    if isinstance(v, str):
        return ILLEGAL_CHARACTERS_RE.sub("", v)
    return v

# DataFrame 전체에 적용하는 함수
# - 컬럼명과 문자열 값 모두에서 Excel에서 사용할 수 없는 문자를 제거한다.
# - openpyxl이 설치되어 있지 않은 경우에도 최소한의 대체 패턴을 사용한다.
# - 이 함수는 다운로드 직전에 호출하는 것을 권장한다 (화면 렌더링용 df_display에는 원본 데이터를 유지하는 것이 좋음).
# - 이 함수는 원본 DataFrame을 수정하지 않고, sanitized된 복사본을 반환한다.
def _sanitize_dataframe_for_excel(df: pd.DataFrame) -> pd.DataFrame:
    if df is None:
        return df

    df = _drop_sensitive_columns(df)
    out = df.copy()

    try:
        out.columns = [
            ILLEGAL_CHARACTERS_RE.sub("", c) if isinstance(c, str) else c
            for c in out.columns
        ]
    except Exception:
        pass

    for col in out.columns:
        try:
            if out[col].dtype == "object":
                out[col] = out[col].map(_sanitize_excel_value)
        except Exception:
            pass

    return out


# ---------------------------------------------------------------------
# Security: 표/다운로드/LLM 컨텍스트 민감 컬럼 방어막
# ---------------------------------------------------------------------
_SENSITIVE_COLUMN_EXACT = {
    "비밀번호",
    "주민번호",
    "Rd06_Password",
    "Rd06_Password_ENCrypt",
    "Rd06_Jumin",
    "Rd06_SMS_PW",
    "Rd06_POL_PW",
    "Rd06_Work_PWD",
}

_SENSITIVE_COLUMN_TOKENS = (
    "password",
    "passwd",
    "pwd",
    "비밀번호",
    "jumin",
    "주민",
    "sms_pw",
    "pol_pw",
    "work_pwd",
)


def _is_sensitive_column(col: object) -> bool:
    s = str(col or "").strip()
    s_lower = s.lower()
    if not s:
        return False
    if s in _SENSITIVE_COLUMN_EXACT:
        return True

    low = s.lower().replace(" ", "").replace("-", "_")
    return any(token in low for token in _SENSITIVE_COLUMN_TOKENS)


def _drop_sensitive_columns(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame):
        return df

    try:
        drop_cols = [c for c in df.columns if _is_sensitive_column(c)]
        if drop_cols:
            return df.drop(columns=drop_cols, errors="ignore")
    except Exception:
        pass

    return df
# SIMS 분석 결과로 보이는 payload인지 판정하는 함수
# - action/analysis_type/summary_type 키와 값을 활용해서 판정한다.
# - SIMS 분석 결과로 보이는 경우, 이후에 분석/KPI 등급 컬럼 스타일링이나 숫자 포맷팅 등을 적용할 때 참고할 수 있다.
def _chat_is_analysis_payload(item: Dict[str, Any], meta: Dict[str, Any], title: str = "") -> bool:
    action_name = str(item.get("action") or meta.get("action") or title or "").strip()
    analysis_type = str(meta.get("analysis_type") or "").strip()
    summary_type = str(meta.get("summary_type") or "").strip()

    return (
        action_name in {
            "품목별 매출 추세 분석",
            "품목별 매출 추세 요약표",
            "품목별 매출 예상",
            "품목별 재고부족현황",
        }
        or analysis_type in {"sales_trend", "sales_forecast", "stock_shortage"}
        or summary_type in {"product_summary", "product_forecast", "product_stock_shortage"}
    )


def _chat_parse_num(value: Any) -> float | None:
    if value is None:
        return None

    s = str(value).strip()
    if s in {"", "None", "nan", "NaN", "<NA>", "NaT"}:
        return None

    s = (
        s.replace(",", "")
        .replace("원", "")
        .replace("개", "")
        .replace("건", "")
        .replace("개월", "")
        .replace("%", "")
        .strip()
    )

    if not s:
        return None

    try:
        return float(s)
    except Exception:
        return None

# 분석/KPI 등급 컬럼 스타일링 함수
# - 등급 값에 따라 글자색과 굵기를 반환한다.
def _chat_grade_cell_style(value: Any) -> str:
    v = str(value or "").strip()

    if v in {"증가", "신규/증가", "상승예상", "정상"}:
        return "color:#166534; font-weight:800;"
    if v in {"감소", "감소예상", "2개월내 부족주의", "3개월내 부족주의", "3개월내 부족"}:
        return "color:#9a3412; font-weight:800;"
    if v in {"안정", "안정예상"}:
        return "color:#1d4ed8; font-weight:800;"
    if v in {"반품주의", "재고없음", "1개월내 부족"}:
        return "color:#be123c; font-weight:800;"
    if v in {"자료부족", "수요관찰", "재고없음/수요없음"}:
        return "color:#475569; font-weight:800;"
    if v == "신규확인":
        return "color:#6d28d9; font-weight:800;"
    if any(token in v for token in ("부족", "위험", "주의")):
        return "color:#be123c; font-weight:800;"
    if any(token in v for token in ("정상", "충분", "양호")):
        return "color:#166534; font-weight:800;"

    return ""
# 분석/KPI 등급 컬럼이 있는 DataFrame 스타일러에 스타일을 적용하는 함수
# - 분석/KPI 등급 컬럼이 있는 경우, 해당 셀에 글자색과 굵기를 적용한다.
# - 이 함수는 _build_io_display_styler()로 만든 기본 스타일러 위에 추가로 적용하는 것을 권장한다.
def _apply_chat_analysis_grade_style(styler, df: pd.DataFrame):
    """
    공통 IO 스타일러 위에 분석/KPI 등급 컬럼 글자색만 추가한다.
    숫자 포맷/정렬/음수 처리는 _build_io_display_styler()에 맡긴다.
    """
    if df is None or df.empty:
        return styler

    style_df = pd.DataFrame("", index=df.index, columns=df.columns)

    for grade_col in ["추세판정", "예상등급", "부족등급", "재고부족", "재고등급", "판정", "판정결과"]:
        if grade_col not in df.columns:
            continue

        for idx in df.index:
            extra = _chat_grade_cell_style(df.loc[idx, grade_col])
            if extra:
                style_df.loc[idx, grade_col] = extra

    try:
        return styler.apply(lambda _: style_df, axis=None)
    except Exception:
        return styler


# 채팅 표에서 Styler 대신 빠른 st.dataframe 렌더를 쓸지 판단하는 함수
# - 셀 수가 너무 많으면 Styler 렌더링이 너무 느려질 수 있으므로, cell 수 기준으로 빠른 렌더링 여부를 판단한다.
def _chat_is_large_table_for_fast_render(df: pd.DataFrame) -> bool:
    """채팅 표에서 Styler 대신 빠른 st.dataframe 렌더를 쓸지 판단."""
    if df is None or df.empty:
        return False

    try:
        cells = int(len(df)) * int(len(df.columns))
    except Exception:
        return False

    threshold = int(os.getenv("SIMS_CHAT_FAST_TABLE_CELL_THRESHOLD", os.getenv("SIMS_FAST_TABLE_CELL_THRESHOLD", "6000")))
    return cells >= threshold


def _chat_is_nlq_table_meta(meta: Dict[str, Any] | None) -> bool:
    if not isinstance(meta, dict):
        return False
    return bool(
        meta.get("nlq")
        or meta.get("analysis_nlq")
        or meta.get("master_nlq")
        or meta.get("io_nlq")
        or meta.get("nlq_query")
    )


def _chat_is_stock_io_action(action_name: str) -> bool:
    s = str(action_name or "").strip()
    return any(w in s for w in ("제품수불현황", "제품재고현황", "제품수불", "제품재고"))


def _chat_log_stock_table_render(
    *,
    action_name: str,
    rows: int,
    cols: int,
    fast: bool,
) -> None:
    try:
        if fast:
            log.info(
                "[stock.table] fast table mode action=%s rows=%s cols=%s cells=%s",
                action_name,
                rows,
                cols,
                rows * cols,
            )
        else:
            log.info(
                "[stock.table] small table style enabled action=%s rows=%s cols=%s",
                action_name,
                rows,
                cols,
            )
    except Exception:
        pass


def _chat_log_nlq_table_render(
    *,
    action_name: str,
    table_key: str,
    rows: int,
    cols: int,
    fast: bool,
) -> None:
    try:
        log.info(
            "[chat.nlq.table] render action=%s rows=%s table_key=%s small=%s fast=%s",
            action_name,
            rows,
            table_key,
            not fast,
            fast,
        )
        if fast:
            log.info(
                "[chat.nlq.table] fast table mode action=%s rows=%s cols=%s cells=%s",
                action_name,
                rows,
                cols,
                rows * cols,
            )
        else:
            log.info(
                "[chat.nlq.table] small table style enabled action=%s rows=%s cols=%s",
                action_name,
                rows,
                cols,
            )
    except Exception:
        pass


def _render_nlq_table_meta_caption(meta: Dict[str, Any]) -> None:
    if not _chat_is_nlq_table_meta(meta):
        return

    try:
        nlq_query = str(
            meta.get("nlq_query")
            or meta.get("question")
            or meta.get("user_query")
            or ""
        ).strip()
        table_key = str(meta.get("table_key") or "").strip()

        if nlq_query:
            st.caption(f"NLQ 질문: {nlq_query}")
        if table_key:
            st.caption(f"table_key: {table_key}")
            st.caption("현재표 후속질문 가능")
    except Exception:
        pass


def _chat_clean_display_none_values(df: pd.DataFrame) -> pd.DataFrame:
    """
    채팅 표 화면 표시용 None/NaN 정리.

    - 실제 None/NaN은 빈칸으로 표시
    - 문자열 "None", "nan", "<NA>", "NaT", "NULL"도 빈칸 처리
    - 값이 있는 셀은 건드리지 않음
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df

    out = df.copy()

    blank_tokens = {
        "None", "none", "NONE",
        "nan", "NaN", "NAN",
        "<NA>", "NaT",
        "NULL", "null",
    }

    for col in out.columns:
        try:
            if str(col).strip() == "순번":
                continue

            s = out[col].astype("object")
            s = s.where(pd.notna(s), "")
            s = s.map(lambda v: "" if str(v).strip() in blank_tokens else v)
            out[col] = s
        except Exception:
            pass

    return out


def _chat_drop_number_config_for_blank_numeric_cols(
    df: pd.DataFrame,
    column_config: dict | None,
) -> dict | None:
    """
    빈칸이 섞인 숫자형 컬럼은 NumberColumn 설정을 제거한다.

    이유:
    - 값은 빈칸으로 정리했더라도 NumberColumn이 붙으면
      Streamlit이 빈칸을 None처럼 표시하는 경우가 있다.
    - 컬럼 폭/고정은 일부 약해질 수 있지만 None 표시 제거가 우선이다.
    """
    if not isinstance(column_config, dict) or not column_config:
        return column_config

    if not isinstance(df, pd.DataFrame) or df.empty:
        return column_config

    cfg = dict(column_config)

    for col in list(df.columns):
        if col not in cfg:
            continue

        try:
            has_blank = df[col].astype("object").map(lambda v: str(v).strip() == "").any()
        except Exception:
            has_blank = False

        if not has_blank:
            continue

        try:
            is_numeric_like = _chat_is_fast_numeric_column(df, col)
        except Exception:
            is_numeric_like = False

        if is_numeric_like:
            cfg.pop(col, None)

    return cfg


def _chat_is_fast_numeric_column(df: pd.DataFrame, col: str) -> bool:
    """
    채팅 빠른 표 모드에서 숫자 처리할 컬럼 판정.

    주의:
    - 제품코드/거래처코드/보험코드/번호/ID는 숫자처럼 보여도 문자 유지
    - 이름/명칭/일자/등급도 문자 유지
    """
    s = str(col or "").strip()
    s_lower = s.lower()

    if s in {"순번", "조회순번"}:
        return True

    exclude_words = [
        "코드",
        "stock_cd",
        "buy_cd",
        "physic_cd",
        "ven_cd",
        "_cd",
        "ID",
        "번호",
        "일자",
        "날짜",
        "일시",
        "시간",
        "명",
        "이름",
        "재고기준",
        "자료원",
        "추세판정",
        "예상등급",
        "부족등급",
        "예상기준",
    ]
    if any(w in s or w in s_lower for w in exclude_words):
        return False

    include_words = [
        "수량",
        "금액",
        "단가",
        "커버월수",
        "평균",
        "매출",
        "공급가액",
        "세액",
        "율",
        "건수",
        "품목수",
        "거래처수",
        "매입처수",
        "재고적용처수",
        "분석월수",
        "매출발생월수",
        "부족",
        "필요",
    ]

    if any(w in s for w in include_words):
        return True

    try:
        return pd.api.types.is_numeric_dtype(df[col])
    except Exception:
        return False


def _chat_fast_display_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    채팅 화면 표시용 빠른 DataFrame.

    중요:
    - 숫자 컬럼은 빠르게 반올림한다.
    - 단, 원본이 None/NaN/빈칸/"None"인 값은 다시 None으로 보이지 않게 빈칸으로 유지한다.
    """
    out = _chat_clean_display_none_values(df)

    blank_tokens = {
        "", "None", "none", "NONE",
        "nan", "NaN", "NAN",
        "<NA>", "NaT",
        "NULL", "null",
    }

    for col in out.columns:
        s = str(col or "").strip()

        if not _chat_is_fast_numeric_column(out, col):
            continue

        try:
            raw_obj = out[col].astype("object")

            blank_mask = raw_obj.isna() | raw_obj.map(
                lambda v: str(v).strip() in blank_tokens
            )

            num = pd.to_numeric(
                raw_obj.astype(str).str.replace(",", "", regex=False),
                errors="coerce",
            )

            if (
                s in {"순번", "조회순번"}
                or s.endswith("건수")
                or "품목수" in s
                or "거래처수" in s
                or "매입처수" in s
                or "재고적용처수" in s
                or "금액" in s
                or "매출" in s
                or "공급가액" in s
                or "세액" in s
                or "가격" in s
            ):
                converted = num.round(0).astype("object")
            else:
                converted = num.round(2).astype("object")

            converted.loc[blank_mask] = ""

            out[col] = converted

        except Exception:
            pass

    return out

def _chat_fast_column_config(df: pd.DataFrame) -> dict:
    """채팅 빠른 표용 column_config."""
    cfg: dict = {}

    for col in df.columns:
        s = str(col or "").strip()

        if not _chat_is_fast_numeric_column(df, col):
            continue

        # 빈칸이 섞인 숫자형 컬럼은 NumberColumn을 적용하지 않는다.
        # NumberColumn을 적용하면 빈칸이 Streamlit에서 None처럼 보일 수 있다.
        try:
            has_blank = df[col].astype("object").map(lambda v: str(v).strip() == "").any()
            if has_blank:
                continue
        except Exception:
            pass

        if (
            s in {"순번", "조회순번"}
            or s.endswith("건수")
            or "품목수" in s
            or "거래처수" in s
            or "매입처수" in s
            or "재고적용처수" in s
            or "금액" in s
            or "매출" in s
            or "공급가액" in s
            or "세액" in s
            or "가격" in s
        ):
            cfg[col] = st.column_config.NumberColumn(
                s,
                format="localized",
                step=1,
            )
        else:
            cfg[col] = st.column_config.NumberColumn(
                s,
                format="localized",
                step=0.01,
            )

    return cfg


def _render_chat_fast_dataframe(
    df: pd.DataFrame,
    *,
    height: int = 520,
    action_name: str = "",
    meta: Dict[str, Any] | None = None,
) -> None:
    """
    채팅 영역 빠른 표 렌더링.

    분석/KPI 큰 표도 공용 column_config를 사용해서
    좌측 고정 칼럼/컬럼폭/숫자 표시를 유지한다.
    """
    view_df = normalize_display_df_for_streamlit(_chat_fast_display_df(df))

    try:
        view_df, column_config, table_width, table_height = build_sims_table_display_config(
            view_df,
            action_name=action_name,
            meta=meta or {},
            add_row_no=False,
            row_no_name="순번",
            enable_pinning=True,
            max_pinned_cols=5,
            min_width=720,
            max_width=1650,
            min_height=170,
            max_height=height,
            row_height=32,
        )

        view_df = _chat_clean_display_none_values(view_df)
        column_config = _chat_drop_number_config_for_blank_numeric_cols(view_df, column_config)

        st.dataframe(
            view_df,
            use_container_width=True,
            hide_index=True,
            height=table_height,
            column_config=column_config if column_config else None,
        )
    except Exception:
        log.exception("[chat] fast common table render failed")
        cfg = _chat_fast_column_config(view_df)
        st.dataframe(
            view_df,
            use_container_width=True,
            hide_index=True,
            height=height,
            column_config=cfg if cfg else None,
        )


#   정리대상 2026/05/19 이후 
def _apply_chat_table_profile(df: pd.DataFrame, meta: Dict[str, Any]) -> tuple[pd.DataFrame, dict]:
    """
    채팅 표 전용 표시 프로필.
    현재는 도로명주소만 먼저 적용한다.
    """
    if not isinstance(df, pd.DataFrame):
        return df, {}

    profile = str((meta or {}).get("table_profile") or "").strip()
    domain = str((meta or {}).get("domain") or "").strip()
    action = str((meta or {}).get("action") or "").strip()

    is_road_address = (
        profile == "road_address"
        or domain == "road_address"
        or action == "도로명주소 조회"
    )

    if not is_road_address:
        return df, {}

    out = df.copy()

    # 혹시 순번이 없으면 보강
    if "순번" not in out.columns and "조회순번" not in out.columns:
        out.insert(0, "순번", range(1, len(out) + 1))

    desired_cols = [
        "순번",
        "도로명코드",
        "도로명코드상세번호",
        "시도명",
        "시구군명",
        "법정읍면동명",
        "도로명",
        "도로명(영문)",
        "동여부",
        "지번본번",
    ]

    use_cols = [c for c in desired_cols if c in out.columns]
    extra_cols = [c for c in out.columns if c not in use_cols]
    out = out[use_cols + extra_cols]

    column_config = {}

    if "순번" in out.columns:
        column_config["순번"] = st.column_config.NumberColumn(
            "순번",
            format="localized",
            step=1,
            width="small",
        )

    for col in [
        "도로명코드",
        "도로명코드상세번호",
        "시도명",
        "시구군명",
        "동여부",
        "지번본번",
    ]:
        if col in out.columns:
            column_config[col] = st.column_config.TextColumn(col, width="small")

    for col in ["법정읍면동명", "도로명", "도로명(영문)"]:
        if col in out.columns:
            column_config[col] = st.column_config.TextColumn(col, width="medium")

    return out, column_config

# 분석/KPI 등급 컬럼 숫자 포맷팅 함수
# - 숫자 값에서 단위(원/개/건/개월/%)를 제거하고, 천 단위 구분 쉼표와 소수점 2자리까지 포맷팅한다.
def _chat_header_num(value: Any, unit: str = "") -> str:
    n = _chat_parse_num(value)
    if n is None:
        text = str(value or "").strip()
    else:
        if abs(n - int(n)) < 1e-9:
            text = f"{int(n):,}"
        else:
            text = f"{n:,.2f}".rstrip("0").rstrip(".")

    if not text:
        text = "-"

    return f"{text}{unit}" if unit and text != "-" else text

# 분석 결과 헤더를 렌더링하는 함수
# - meta에서 분석 유형(analysis_type)과 관련 통계를 읽어서, 적절한 제목과 주요 지표들을 화면 상단에 렌더링한다.
# - 이 함수는 채팅 답변의 본문을 렌더링하기 전에 호출하는 것을 권장한다.
def _chat_metric_card(label: str, value: Any, unit: str = "", bg: str = "#f8fafc", border: str = "#dbe4ee") -> None:
    """
    채팅창 SIMS 분석/KPI 요약 카드.

    패널에서 쓰던 상세 요약 카드 UX를 채팅창으로 옮긴 버전이다.
    패널은 입력 전용으로 두고, 결과 요약은 채팅 메시지 안에서만 렌더한다.
    """
    value_text = _chat_header_num(value, unit)

    html = (
        f'<div style="background:{bg}; border:1px solid {border}; border-radius:10px; '
        f'padding:9px 12px; min-height:42px; display:flex; align-items:center; '
        f'justify-content:space-between; gap:10px; margin-bottom:6px;">'
        f'<span style="font-size:13px; color:#64748b; font-weight:600; white-space:nowrap;">{label}</span>'
        f'<span style="font-size:16px; font-weight:750; color:#1f2937; line-height:1.2; '
        f'text-align:right; white-space:nowrap;">{value_text}</span>'
        f'</div>'
    )
    st.markdown(html, unsafe_allow_html=True)


def _chat_source_mode_label(source_mode: Any) -> str:
    return {
        "auto": "자동",
        "monthly_book": "월집계-장부재고",
        "monthly_real": "월집계-실재고",
        "detail": "출고상세",
    }.get(str(source_mode or ""), str(source_mode or ""))


def _chat_color_for_trend(label: str) -> tuple[str, str]:
    label = str(label or "").strip()
    if label in {"증가", "신규/증가"}:
        return "#ecfdf3", "#b7ebc6"
    if label in {"감소"}:
        return "#fff7ed", "#fed7aa"
    if label in {"반품주의"}:
        return "#fff1f2", "#fecdd3"
    if label in {"자료부족"}:
        return "#f8fafc", "#cbd5e1"
    if label in {"안정"}:
        return "#eff6ff", "#bfdbfe"
    return "#f8fafc", "#dbe4ee"


def _chat_color_for_forecast(label: str) -> tuple[str, str]:
    label = str(label or "").strip()
    if label == "상승예상":
        return "#ecfdf3", "#b7ebc6"
    if label == "감소예상":
        return "#fff7ed", "#fed7aa"
    if label == "안정예상":
        return "#eff6ff", "#bfdbfe"
    if label == "신규확인":
        return "#f5f3ff", "#ddd6fe"
    if label == "반품주의":
        return "#fff1f2", "#fecdd3"
    if label == "자료부족":
        return "#f8fafc", "#cbd5e1"
    return "#f8fafc", "#dbe4ee"


def _chat_color_for_shortage(label: str) -> tuple[str, str]:
    label = str(label or "").strip()
    if label in {"재고없음", "1개월내 부족"}:
        return "#fff1f2", "#fecdd3"
    if label in {"2개월내 부족주의", "3개월내 부족주의", "3개월내 부족"}:
        return "#fff7ed", "#fed7aa"
    if label == "정상":
        return "#ecfdf3", "#b7ebc6"
    if label in {"수요관찰", "재고없음/수요없음"}:
        return "#f8fafc", "#cbd5e1"
    return "#f8fafc", "#dbe4ee"


def _render_chat_count_card_group(title: str, counts: Dict[str, Any], order: list[str], color_fn) -> None:
    """패널에 있던 등급별 제품수 카드 그룹을 채팅창에 그대로 표시한다."""
    if not isinstance(counts, dict) or not counts:
        return

    keys = [k for k in order if k in counts]
    keys += [k for k in counts.keys() if k not in keys]
    if not keys:
        return

    st.markdown(f"### {title}")
    for start in range(0, len(keys), 6):
        row_keys = keys[start:start + 6]
        cols = st.columns(len(row_keys))
        for i, k in enumerate(row_keys):
            bg, border = color_fn(k)
            with cols[i]:
                _chat_metric_card(str(k), counts.get(k, 0), "개", bg=bg, border=border)


# 분석 결과 헤더를 렌더링하는 함수
# - meta에서 분석 유형(analysis_type)과 관련 통계를 읽어서, 적절한 제목과 주요 지표들을 화면 상단에 렌더링한다.
# - 패널 상세 요약은 이 함수로 옮겨 채팅창에서만 표시한다.
def _render_chat_analysis_header(meta: Dict[str, Any]) -> None:
    if not isinstance(meta, dict):
        return

    analysis_type = str(meta.get("analysis_type") or "").strip()
    summary_type = str(meta.get("summary_type") or "").strip()
    is_forecast = analysis_type == "sales_forecast" or summary_type == "product_forecast"
    source_label = str(meta.get("source_label") or _chat_source_mode_label(meta.get("source_mode") or ""))

    if analysis_type == "stock_shortage":
        st.markdown("### 재고부족요약")

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            _chat_metric_card("품목수", meta.get("product_count"), "개", bg="#eff6ff", border="#bfdbfe")
        with c2:
            _chat_metric_card("부족품목수", meta.get("shortage_item_count"), "개", bg="#fff1f2", border="#fecdd3")
        with c3:
            _chat_metric_card("현재재고수량", meta.get("sum_current_stock_qty"), "개", bg="#f0fdf4", border="#bbf7d0")
        with c4:
            _chat_metric_card("현재재고금액", meta.get("sum_current_stock_amt"), "원", bg="#f8fafc", border="#dbe4ee")

        c5, c6, c7, c8 = st.columns(4)
        with c5:
            _chat_metric_card("1개월부족수량", meta.get("sum_shortage_1m_qty"), "개", bg="#fff1f2", border="#fecdd3")
        with c6:
            _chat_metric_card("2개월부족수량", meta.get("sum_shortage_2m_qty"), "개", bg="#fff7ed", border="#fed7aa")
        with c7:
            _chat_metric_card("3개월부족수량", meta.get("sum_shortage_3m_qty"), "개", bg="#fff7ed", border="#fed7aa")
        with c8:
            _chat_metric_card("재고기준", meta.get("stock_label") or meta.get("stock_mode"), "", bg="#f5f3ff", border="#ddd6fe")

        stock_source_label = str(meta.get("stock_source_label") or "")
        c9, c10, c11 = st.columns(3)
        with c9:
            _chat_metric_card("자료원", source_label, "", bg="#f8fafc", border="#dbe4ee")
        with c10:
            _chat_metric_card("현재고원천", stock_source_label or meta.get("stock_label"), "", bg="#f8fafc", border="#dbe4ee")
        with c11:
            _chat_metric_card("조회건수", meta.get("row_count_total") or meta.get("row_count"), "건", bg="#f8fafc", border="#dbe4ee")

        _render_chat_count_card_group(
            "부족등급별 제품수",
            meta.get("shortage_grade_counts") or {},
            [
                "재고없음",
                "1개월내 부족",
                "2개월내 부족주의",
                "3개월내 부족주의",
                "3개월내 부족",
                "정상",
                "수요관찰",
                "재고없음/수요없음",
                "미분류",
            ],
            _chat_color_for_shortage,
        )
        return

    if analysis_type in {"sales_forecast", "sales_trend"} or is_forecast:
        if is_forecast:
            st.markdown("### 매출예상요약")

            c1, c2, c3, c4 = st.columns(4)
            with c1:
                _chat_metric_card("총매출액", meta.get("sum_sales_amt"), "원", bg="#f8fafc", border="#dbe4ee")
            with c2:
                _chat_metric_card("다음월예상매출", meta.get("sum_next_month_forecast_amt"), "원", bg="#fff7ed", border="#fed7aa")
            with c3:
                _chat_metric_card("3개월예상매출", meta.get("sum_3month_forecast_amt"), "원", bg="#fff7ed", border="#fed7aa")
            with c4:
                _chat_metric_card("6개월예상매출", meta.get("sum_6month_forecast_amt"), "원", bg="#fff7ed", border="#fed7aa")

            c5, c6, c7, c8, c9 = st.columns(5)
            with c5:
                _chat_metric_card("출고수량", meta.get("sum_qty"), "개", bg="#f0fdf4", border="#bbf7d0")
            with c6:
                _chat_metric_card("품목수", meta.get("product_count"), "개", bg="#eff6ff", border="#bfdbfe")
            with c7:
                _chat_metric_card(str(meta.get("customer_count_label") or "거래처수"), meta.get("customer_count"), "개", bg="#eff6ff", border="#bfdbfe")
            with c8:
                _chat_metric_card("분석월수", meta.get("month_count"), "개월", bg="#f5f3ff", border="#ddd6fe")
            with c9:
                _chat_metric_card("자료원", source_label, "", bg="#f8fafc", border="#dbe4ee")
        else:
            st.markdown("### 매출추세요약")

            c1, c2, c3, c4 = st.columns(4)
            with c1:
                _chat_metric_card("총매출액", meta.get("sum_sales_amt"), "원", bg="#f8fafc", border="#dbe4ee")
            with c2:
                _chat_metric_card("매출공급가액", meta.get("sum_supply_amt"), "원", bg="#f8fafc", border="#dbe4ee")
            with c3:
                _chat_metric_card("매출세액", meta.get("sum_tax_amt"), "원", bg="#f8fafc", border="#dbe4ee")
            with c4:
                _chat_metric_card("출고수량", meta.get("sum_qty"), "개", bg="#f0fdf4", border="#bbf7d0")

            c5, c6, c7, c8 = st.columns(4)
            with c5:
                _chat_metric_card("품목수", meta.get("product_count"), "개", bg="#eff6ff", border="#bfdbfe")
            with c6:
                _chat_metric_card(str(meta.get("customer_count_label") or "거래처수"), meta.get("customer_count"), "개", bg="#eff6ff", border="#bfdbfe")
            with c7:
                _chat_metric_card("분석월수", meta.get("month_count"), "개월", bg="#f5f3ff", border="#ddd6fe")
            with c8:
                _chat_metric_card("자료원", source_label, "", bg="#f8fafc", border="#dbe4ee")

        _render_chat_count_card_group(
            "추세판정별 제품수",
            meta.get("trend_judge_counts") or {},
            ["증가", "감소", "안정", "반품주의", "신규/증가", "자료부족", "미분류"],
            _chat_color_for_trend,
        )
        _render_chat_count_card_group(
            "예상등급별 제품수",
            meta.get("forecast_grade_counts") or {},
            ["상승예상", "감소예상", "안정예상", "신규확인", "반품주의", "자료부족", "미분류"],
            _chat_color_for_forecast,
        )
        return

# DataFrame이 포함된 payload를 JSON 직렬화 가능하도록 변환한다.
# - payload 내 df/df_display/data/records/meta(df)에서 DataFrame을 찾아서,
#   컬럼명과 데이터를 JSON 직렬화 가능한 형태로 변환한다.
# - 이 함수는 채팅 히스토리에 저장하기 전에 호출하는 것을 권장한다 (실제 렌더링용 payload에는 DataFrame을 유지하는 것이 좋음).
# - 이 함수는 payload를 직접 수정한다 (in-place).
# - DataFrame이 없는 경우나, JSON 직렬화가 이미 가능한 경우에는 payload를 그대로 유지한다.
def _ensure_table_json_safe(payload: Dict[str, Any]) -> None:
    """테이블 payload가 JSON 직렬화 가능하도록 meta(df, columns)를 보장하고 DF 객체를 제거한다."""
    try:
        if payload.get("type") != "table":
            return
        df_ui = _pick_df_from_payload(payload)
        if not isinstance(df_ui, pd.DataFrame):
            return

        df_ui = _drop_sensitive_columns(df_ui)

        meta = dict(payload.get("meta") or {})
        meta["columns"] = list(df_ui.columns)
        meta["df"] = df_ui.to_dict(orient="records")
        meta.setdefault("row_count", int(len(df_ui)))
        payload["meta"] = meta

        for k in ("df", "df_display", "data"):
            if isinstance(payload.get(k), pd.DataFrame):
                payload.pop(k, None)
    except Exception:
        log.exception("[chat] ensure table json-safe failed")

# ---------------------------------------------------------------------
# 렌더 타겟(이번 턴 답변 아래 컨테이너 등)
# - NLQ/백엔드 푸시가 발생해도 표/버블이 화면 상단으로 튀지 않게,
#   '지정된 컨테이너' 안에서 렌더하도록 한다.
# - Streamlit DeltaGenerator(컨테이너)는 런타임 객체이므로,
#   메인(UI)에서 매 rerun마다 1회 set_chat_render_target(...) 해주는 것을 권장.
# ---------------------------------------------------------------------
_CHAT_RENDER_TARGET = None  # type: ignore

def set_chat_render_target(area: Any) -> None:
    """채팅/테이블 즉시렌더의 출력 위치를 지정한다."""
    global _CHAT_RENDER_TARGET
    _CHAT_RENDER_TARGET = area

def clear_chat_render_target() -> None:
    global _CHAT_RENDER_TARGET
    _CHAT_RENDER_TARGET = None

# ---------------------------------------------------------------------
# DataFrame 안전 선택 유틸
# - pandas.DataFrame는 bool 문맥에서 ValueError를 발생시키므로
#   'A or B' 같은 표현을 쓰지 말고 타입 체크로 선택한다.
# ---------------------------------------------------------------------
def _pick_df(payload: dict, prefer=("df_display", "df")):
    for k in prefer:
        v = payload.get(k)
        if isinstance(v, pd.DataFrame):
            return v
    return None

# ---------------------------------------------------------------------
# Backward/Forward compatibility
# - sims_panel.py / chat_bridge.py가 import 하는 이름을 보장한다.
# ---------------------------------------------------------------------

# SIMS LLM 컨텍스트용 키/제한
KEY_SIMS_CTX = "__sims_ctx"
# ✅ 2번 정책(전부 보여주기): LLM 컨텍스트 JSON에 넣을 최대 행 수
# 0 이면 제한 없음. 단, 너무 큰 테이블 보호를 위해 _shrink_df_for_llm에서 HARD_CAP 적용.
# ✅ 툴콜 붙이기 전 임시 운영: LLM 컨텍스트 JSON에 넣을 최대 행 수(기본 300)
# - 너무 크게 주면 토큰 폭발(수십만 tokens) 발생
# - .env에서 SIMS_CTX_MAX_ROWS=200 처럼 조정 가능
def _get_int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default

SIMS_CTX_MAX_ROWS = int(os.getenv("SIMS_CTX_MAX_ROWS", "300"))  # LLM 컨텍스트 JSON에 넣을 최대 행 수

# ---------------------------------------------------------------------
# 렌더 앵커(UX)
# - SIMS 표/카드가 코드 실행 위치(상단)로 '튀는' 현상을 줄이기 위해
#   메인(UI)에서 지정한 컨테이너(anchor) 내부로 결과를 렌더링할 수 있게 한다.
# - 앵커가 없으면 기존처럼 현재 위치에 렌더링(하위호환).
# ---------------------------------------------------------------------
def _chat_display_max_rows(default: int = 200) -> int:
    return max(0, _get_int_env("SIMS_CHAT_DISPLAY_MAX_ROWS", default))


def _limit_chat_display_df(df: pd.DataFrame, *, limit: int | None = None) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame):
        return df

    out = normalize_display_df_for_streamlit(df)
    row_limit = _chat_display_max_rows() if limit is None else int(limit or 0)
    if row_limit > 0 and len(out) > row_limit:
        return out.head(row_limit).copy()
    return out


def _apply_chat_display_limit_to_payload(payload: Dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        return

    df_full = payload.get("df")
    df_display = payload.get("df_display")
    if not isinstance(df_display, pd.DataFrame):
        if isinstance(payload.get("data"), pd.DataFrame):
            df_display = payload.get("data")
        elif isinstance(df_full, pd.DataFrame):
            df_display = df_full

    if not isinstance(df_display, pd.DataFrame):
        return

    limited = _limit_chat_display_df(df_display)
    payload["df_display"] = limited
    payload["data"] = limited
    payload["columns"] = list(limited.columns)
    payload["records"] = limited.to_dict(orient="records")

    meta = payload.setdefault("meta", {})
    full_rows = int(len(df_full)) if isinstance(df_full, pd.DataFrame) else int(len(df_display))
    meta["display_row_count"] = int(len(limited))
    meta.setdefault("row_count", full_rows)
    meta.setdefault("row_count_total", full_rows)
    meta.setdefault("download_row_count", full_rows)


def set_chat_render_anchor(anchor) -> None:
    """SIMS 결과 렌더링을 고정할 컨테이너(예: st.container())를 지정."""
    try:
        st.session_state["__chat_render_anchor"] = anchor
    except Exception:
        pass


def _get_chat_render_anchor():
    try:
        return st.session_state.get("__chat_render_anchor")
    except Exception:
        return None

# ──────────────────────────────────────────────────────────────────────────────
# 내부 유틸
# ──────────────────────────────────────────────────────────────────────────────
def _sig(obj: Any) -> str:
    """푸시 중복 방지용 서명."""
    try:
        if isinstance(obj, pd.DataFrame):
            # 내용 + 컬럼명 + shape 기준
            h = hashlib.sha256()
            h.update(str(list(obj.columns)).encode("utf-8"))
            h.update(str(obj.shape).encode("utf-8"))
            # 큰 DF는 샘플만(헤더+앞 50행) 서명
            sample = obj.head(50).to_csv(index=False)
            h.update(sample.encode("utf-8"))
            return "DF:" + h.hexdigest()
        return hashlib.sha256(
            json.dumps(obj, default=str, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
    except Exception:
        return "RAW:" + hashlib.sha256(str(obj).encode("utf-8")).hexdigest()

def _shrink_df_for_llm(df: pd.DataFrame) -> pd.DataFrame:
    """
    LLM 컨텍스트용으로 DataFrame 크기를 줄인다.
    - 행 수를 SIMS_CTX_MAX_ROWS 로 제한 (0이면 무제한)
    - 문자열 값은 strip() 해서 CHAR 패딩/공백을 제거 (조인키/코드 비교 품질 향상)    
    """
    if not isinstance(df, pd.DataFrame):
        return df

    out = df
    try:
        # 문자열 패딩 제거 (원본 DF를 건드리지 않도록 copy)
        obj_cols = [c for c in out.columns if out[c].dtype == "object"]
        if obj_cols:
            out = out.copy()
            for c in obj_cols:
                out[c] = out[c].map(lambda x: x.strip() if isinstance(x, str) else x)
    except Exception:
        # 어떤 문제가 나도 원본 df는 그대로 반환
        out = df

    try:
        max_rows = int(SIMS_CTX_MAX_ROWS or 0)
        if max_rows > 0 and len(out) > max_rows:
            out = out.head(max_rows)
    except Exception:
        pass

    return out

# SIMS 뷰 함수가 반환한 결과가 '최종 결과'인지 판정하는 함수
# - SIMS 패널에서 사용하는 표 구조(df/df_display/records+columns)가 있으면 최종 결과로 간주한다.
# - SIMS 패널에서 명시적으로 is_final=True 를 줄 수도 있다.
# - 최종 결과가 아니면, 예를 들어 "중간 결과"나 "진행 상황" 같은 것으로 간주할 수 있다.
def _is_final_result(result: Any) -> bool:
    """
    SIMS 뷰 함수가 반환한 값이 '최종 결과'인지 판정.
    - df / df_display / records 를 포함한 dict나 DataFrame이면 최종 결과로 간주.
    """
    if result is None:
        return False

    # 순수 DataFrame 은 최종 결과
    if isinstance(result, pd.DataFrame):
        return True

    if isinstance(result, dict):
        # 뷰에서 명시적으로 is_final=True 를 줄 수도 있음
        if result.get("is_final") is True:
            return True

        # SIMS 패널에서 사용하는 표 구조라면 최종 결과로 본다
        if (
            isinstance(result.get("df"), pd.DataFrame)
            or isinstance(result.get("df_display"), pd.DataFrame)
            or ("records" in result and "columns" in result)
        ):
            return True

    return False

# SIMS 뷰 함수가 반환한 결과를 채팅 컨텍스트에 올릴 표준 payload로 변환하는 함수
# - SIMS 패널에서 사용하는 표 구조(df/df_display/records+columns)가 있으면, 이를 활용해서 "type":"table" payload로 래핑한다.
# - 그렇지 않으면, "type":"object" payload로 래핑한다.
def _normalize_result_for_chat(result: Any) -> Dict[str, Any]:
    """
    채팅 컨텍스트에 올릴 표준 payload로 변환.
    반환 스키마:
      {"type":"table|text|object", "title":str, "data":..., "meta":{...}}
    """
    # 0) SIMS 패널 payload(dict)에 df/df_display/records가 있는 경우 우선 처리
    if isinstance(result, dict) and (
        isinstance(result.get("df"), pd.DataFrame)
        or isinstance(result.get("df_display"), pd.DataFrame)
        or ("records" in result and "columns" in result)
    ):
        payload = dict(result)  # 원본 보존
        title = payload.get("title") or payload.get("action") or "SIMS 결과"

        df_disp = payload.get("df_display")
        df_full = payload.get("df")

        # 1) df_full 확보: df_display, records/columns 에서 재구성
        if not isinstance(df_full, pd.DataFrame):
            if isinstance(df_disp, pd.DataFrame):
                df_full = df_disp
            else:
                records = payload.get("records")
                cols = payload.get("columns")
                if isinstance(records, list) and records:
                    try:
                        if isinstance(cols, list) and cols:
                            df_full = pd.DataFrame(records, columns=cols)
                        else:
                            df_full = pd.DataFrame.from_records(records)
                    except Exception:
                        df_full = None

        # 2) DataFrame 이 있다면 df/df_display 를 유지하면서 table payload 로 래핑
        if isinstance(df_full, pd.DataFrame):
            df_full = _drop_sensitive_columns(df_full)
            if isinstance(df_disp, pd.DataFrame):
                df_disp = _drop_sensitive_columns(df_disp)

            if not isinstance(df_disp, pd.DataFrame) or df_disp.empty:
                df_disp = df_full
            df_disp = _limit_chat_display_df(df_disp)

            sample_df = df_disp
            if SIMS_CTX_MAX_ROWS and len(sample_df) > SIMS_CTX_MAX_ROWS:
                sample_df = sample_df.head(SIMS_CTX_MAX_ROWS)

            meta = payload.get("meta") or {}
            payload["type"] = "table"
            payload["title"] = title
            payload["data"] = df_disp
            payload["meta"] = meta
            payload["df"] = df_full          # ★ 컨텍스트용으로 계속 유지
            payload["df_display"] = df_disp  # ★ 화면/컨텍스트 공용

            try:
                row_count = int(payload.get("count") or len(df_full))
            except Exception:
                row_count = len(df_full)
            meta.setdefault("row_count", row_count)
            meta["display_row_count"] = int(len(df_disp))
            meta.setdefault("row_count_total", row_count)
            meta.setdefault("download_row_count", row_count)
            meta.setdefault("columns", [str(c) for c in sample_df.columns])
            meta.setdefault("source", "SIMS")
            if payload.get("action"):
                meta.setdefault("action", payload.get("action"))

            return payload

        # 3) DataFrame 을 만들지 못하면 그냥 객체로 래핑
        meta = payload.get("meta") or {}
        payload["meta"] = meta
        return {
            "type": "object",
            "title": title,
            "data": payload,
            "meta": meta,
        }

    # 1) 이미 래핑된 형식
    if isinstance(result, dict) and "data" in result:
        payload = dict(result)
        payload.setdefault("type", "object")
        payload.setdefault("title", payload.get("action") or "SIMS 결과")
        payload.setdefault("meta", {})
        return payload

    # 2) 기타 단순 타입들
    if isinstance(result, str):
        return {"type": "text", "title": "결과", "data": result, "meta": {}}
    if isinstance(result, (int, float, bool)):
        return {"type": "text", "title": "값", "data": str(result), "meta": {}}

    # 마지막 fallback
    return {"type": "object", "title": "객체", "data": result, "meta": {}}


# ---------------------------------------------------------------------
# LLM 분석 전용 컨텍스트 builder
# ---------------------------------------------------------------------
def _llm_json_safe_value(v: Any) -> Any:
    """LLM 분석 컨텍스트용 JSON-safe 값 변환."""
    try:
        if v is None:
            return None
        if isinstance(v, float) and pd.isna(v):
            return None
        if not isinstance(v, (list, dict, tuple, set)) and pd.isna(v):
            return None
    except Exception:
        pass

    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float, bool)):
        return v

    try:
        if hasattr(v, "item"):
            return v.item()
    except Exception:
        pass

    try:
        return str(v).strip()
    except Exception:
        return None


def _llm_num_series(df: pd.DataFrame, col: str) -> pd.Series:
    """문자/콤마/단위가 섞인 숫자 컬럼을 float Series로 변환."""
    if not isinstance(df, pd.DataFrame) or col not in df.columns:
        return pd.Series([0.0] * (len(df) if isinstance(df, pd.DataFrame) else 0), dtype="float64")

    return df[col].map(lambda x: _chat_parse_num(x) or 0.0)


def _llm_records_from_df(
    df: pd.DataFrame,
    cols: List[str],
    limit: int = 30,
) -> List[Dict[str, Any]]:
    """LLM 분석용 record 추출. 지정 컬럼만, 지정 건수만."""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []

    use_cols = [c for c in cols if c in df.columns]
    if not use_cols:
        use_cols = list(df.columns[:20])

    out: List[Dict[str, Any]] = []
    try:
        for _, row in df[use_cols].head(limit).iterrows():
            out.append({c: _llm_json_safe_value(row.get(c)) for c in use_cols})
    except Exception:
        return []

    return out


def _llm_value_counts(df: pd.DataFrame, col: str, limit: int = 50) -> Dict[str, int]:
    if not isinstance(df, pd.DataFrame) or col not in df.columns:
        return {}

    try:
        s = df[col].dropna().astype(str).str.strip()
        s = s[s != ""]
        return {str(k): int(v) for k, v in s.value_counts().head(limit).to_dict().items()}
    except Exception:
        return {}

# LLM 분석 컨텍스트용 숫자 변환 함수
# - 문자열에 섞인 콤마/단위 제거 후 숫자로 변환. 변환 불가하면 0으로 처리.
# - LLM 분석에서 숫자 집계/비교를 할 때, 원본 문자열이 섞여 있으면 품질이 떨어질 수 있으므로, 최대한 숫자 형태로 변환해서 제공한다.
# - 예외가 나도 0으로 처리해서 분석이 중단되지 않도록 한다.
def _analysis_to_num(sr: pd.Series) -> pd.Series:
    """분석 컨텍스트 집계용 숫자 변환."""
    try:
        return pd.to_numeric(
            sr.astype(str).str.replace(",", "", regex=False),
            errors="coerce",
        ).fillna(0)
    except Exception:
        return pd.Series([0] * len(sr), index=sr.index)


def _analysis_safe_text(v: Any) -> str:
    try:
        if pd.isna(v):
            return ""
    except Exception:
        pass
    s = str(v or "").strip()
    if s in {"None", "nan", "NaN", "<NA>", "NaT"}:
        return ""
    return s


def _df_top_records(
    df: pd.DataFrame,
    *,
    sort_col: str,
    cols: list[str],
    n: int = 10,
    ascending: bool = False,
) -> list[dict]:
    """LLM 컨텍스트용 TOP 목록 생성."""
    if not isinstance(df, pd.DataFrame) or df.empty or sort_col not in df.columns:
        return []

    use_cols = [c for c in cols if c in df.columns]
    if sort_col not in use_cols:
        use_cols.append(sort_col)

    tmp = df.copy()
    tmp[sort_col] = _analysis_to_num(tmp[sort_col])

    try:
        tmp = tmp.sort_values(sort_col, ascending=ascending).head(n)
    except Exception:
        tmp = tmp.head(n)

    out = []
    for _, row in tmp[use_cols].iterrows():
        rec = {}
        for c in use_cols:
            v = row.get(c)
            if isinstance(v, (int, float)):
                rec[c] = float(v)
            else:
                rec[c] = _analysis_safe_text(v)
        out.append(rec)

    return out


def _group_summary_records(
    df: pd.DataFrame,
    *,
    group_col: str,
    numeric_cols: list[str],
    n: int = 10,
    sort_col: str | None = None,
) -> list[dict]:
    """제조사/제품그룹/등급별 전체 집계 생성."""
    if not isinstance(df, pd.DataFrame) or df.empty or group_col not in df.columns:
        return []

    tmp = df.copy()
    tmp[group_col] = tmp[group_col].map(_analysis_safe_text).replace("", "미지정")

    agg_dict = {}
    for c in numeric_cols:
        if c in tmp.columns:
            tmp[c] = _analysis_to_num(tmp[c])
            agg_dict[c] = "sum"

    if not agg_dict:
        grouped = tmp.groupby(group_col, dropna=False).size().reset_index(name="건수")
    else:
        grouped = tmp.groupby(group_col, dropna=False).agg(agg_dict).reset_index()
        grouped.insert(1, "건수", tmp.groupby(group_col, dropna=False).size().values)

    sort_target = sort_col if sort_col in grouped.columns else None
    if sort_target:
        grouped = grouped.sort_values(sort_target, ascending=False)
    elif "건수" in grouped.columns:
        grouped = grouped.sort_values("건수", ascending=False)

    grouped = grouped.head(n)

    out = []
    for _, row in grouped.iterrows():
        rec = {}
        for c in grouped.columns:
            v = row.get(c)
            if isinstance(v, (int, float)):
                rec[c] = float(v)
            else:
                rec[c] = _analysis_safe_text(v)
        out.append(rec)

    return out

# 품목별 재고부족현황 분석 컨텍스트 빌더
# - 원본 df에서 전체 집계 + 등급분포 + 위험품목 상세 샘플을 추출해서 LLM 분석 컨텍스트로 만든다.
# - 화면용 styled table과는 분리해서, LLM에는 필요한 정보만 JSON-safe하게 제공한다.
def _build_stock_shortage_analysis_ctx(
    base_df: pd.DataFrame,
    *,
    action_name: str,
    params: Dict[str, Any],
    meta: Dict[str, Any],
) -> Dict[str, Any]:
    """
    품목별 재고부족현황 전용 LLM 분석 컨텍스트.

    원칙:
    - 화면용 styled table과 분리
    - 전체 원본 df 기준으로 집계
    - LLM에는 전체 원본 974x97을 그대로 주지 않고,
      전체 집계 + 등급분포 + 위험품목 상세 샘플을 제공
    """
    df = base_df if isinstance(base_df, pd.DataFrame) else pd.DataFrame()
    if df.empty:
        return {}

    row_count = int(len(df))
    col_count = int(len(df.columns))

    product_cols = [
        "순번",
        "제품코드",
        "제품명",
        "규격",
        "제조사명",
        "제품그룹명",
        "제품구분명",
        "제품분류명",
        "재고기준",
        "현재재고수량",
        "현재재고금액",
        "최근3개월평균수량",
        "최근6개월평균수량",
        "월평균출고수량",
        "예상기준월수량",
        "재고커버월수",
        "1개월필요수량",
        "1개월부족수량",
        "2개월필요수량",
        "2개월부족수량",
        "3개월필요수량",
        "3개월부족수량",
        "부족등급",
        "예상등급",
        "추세판정",
    ]

    sum_current_stock_qty = float(_llm_num_series(df, "현재재고수량").sum())
    sum_current_stock_amt = float(_llm_num_series(df, "현재재고금액").sum())
    sum_shortage_1m_qty = float(_llm_num_series(df, "1개월부족수량").sum())
    sum_shortage_2m_qty = float(_llm_num_series(df, "2개월부족수량").sum())
    sum_shortage_3m_qty = float(_llm_num_series(df, "3개월부족수량").sum())

    shortage_grade_counts = (
        meta.get("shortage_grade_counts")
        if isinstance(meta.get("shortage_grade_counts"), dict)
        else _llm_value_counts(df, "부족등급")
    )
    forecast_grade_counts = (
        meta.get("forecast_grade_counts")
        if isinstance(meta.get("forecast_grade_counts"), dict)
        else _llm_value_counts(df, "예상등급")
    )
    trend_judge_counts = (
        meta.get("trend_judge_counts")
        if isinstance(meta.get("trend_judge_counts"), dict)
        else _llm_value_counts(df, "추세판정")
    )

    shortage_grades = {
        "재고없음",
        "1개월내 부족",
        "2개월내 부족",
        "2개월내 부족주의",
        "3개월내 부족",
        "3개월내 부족주의",
    }

    shortage_item_count = 0
    if "부족등급" in df.columns:
        try:
            shortage_item_count = int(
                df["부족등급"].astype(str).str.strip().isin(shortage_grades).sum()
            )
        except Exception:
            shortage_item_count = 0

    work = df.copy()
    grade_priority = {
        "재고없음": 1,
        "1개월내 부족": 2,
        "2개월내 부족": 3,
        "2개월내 부족주의": 4,
        "3개월내 부족": 5,
        "3개월내 부족주의": 6,
        "수요관찰": 7,
        "재고없음/수요없음": 8,
        "정상": 99,
    }

    if "부족등급" in work.columns:
        work["_llm_risk_priority"] = work["부족등급"].map(
            lambda x: grade_priority.get(str(x or "").strip(), 90)
        )
    else:
        work["_llm_risk_priority"] = 90

    work["_llm_shortage_3m"] = _llm_num_series(work, "3개월부족수량")
    work["_llm_shortage_1m"] = _llm_num_series(work, "1개월부족수량")
    work["_llm_negative_stock"] = _llm_num_series(work, "현재재고수량").map(
        lambda x: abs(x) if x < 0 else 0
    )

    try:
        work = work.sort_values(
            by=[
                "_llm_risk_priority",
                "_llm_shortage_3m",
                "_llm_shortage_1m",
                "_llm_negative_stock",
            ],
            ascending=[True, False, False, False],
        )
    except Exception:
        pass

    risk_work = work.drop(
        columns=[
            "_llm_risk_priority",
            "_llm_shortage_3m",
            "_llm_shortage_1m",
            "_llm_negative_stock",
        ],
        errors="ignore",
    )

    risk_products_top = _llm_records_from_df(risk_work, product_cols, limit=50)

    grade_samples: Dict[str, List[Dict[str, Any]]] = {}
    if "부족등급" in df.columns:
        for grade in [
            "재고없음",
            "1개월내 부족",
            "2개월내 부족",
            "3개월내 부족",
            "3개월내 부족주의",
            "수요관찰",
            "정상",
            "재고없음/수요없음",
        ]:
            try:
                sub = df[df["부족등급"].astype(str).str.strip() == grade]
                if not sub.empty:
                    grade_samples[grade] = _llm_records_from_df(sub, product_cols, limit=8)
            except Exception:
                pass

    # 재고 기준/테이블 힌트 정리
    # LLM이 source_mode=auto만 보고 Rddbc220 같은 테이블명을 추정하지 않도록 명시한다.
    stock_mode_raw = (
        params.get("stock_mode")
        or meta.get("stock_mode")
        or meta.get("stock_label")
        or ""
    )
    stock_mode_text = str(stock_mode_raw or "").strip().lower()

    if stock_mode_text in {"real", "실재고", "actual"}:
        stock_basis_label = "실재고 기준"
        source_table_hint = "Rddbc210"
        source_table_label = "실재고월집계(Rddbc210)"
    elif stock_mode_text in {"book", "장부재고", "ledger"}:
        stock_basis_label = "장부재고 기준"
        source_table_hint = "Rddbc220"
        source_table_label = "장부재고월집계(Rddbc220)"
    else:
        stock_basis_label = str(meta.get("stock_label") or stock_mode_raw or "재고 기준 미확정")
        source_table_hint = ""
        source_table_label = ""

    # 재고 기준/테이블 힌트 정리
    # LLM이 source_mode=auto만 보고 Rddbc220 같은 테이블명을 추정하지 않도록 명시한다.
    stock_mode_raw = (
        params.get("stock_mode")
        or meta.get("stock_mode")
        or meta.get("stock_label")
        or ""
    )
    stock_mode_text = str(stock_mode_raw or "").strip().lower()

    if stock_mode_text in {"real", "실재고", "actual"}:
        stock_basis_label = "실재고 기준"
        source_table_hint = "Rddbc210"
        source_table_label = "실재고월집계(Rddbc210)"
    elif stock_mode_text in {"book", "장부재고", "ledger"}:
        stock_basis_label = "장부재고 기준"
        source_table_hint = "Rddbc220"
        source_table_label = "장부재고월집계(Rddbc220)"
    else:
        stock_basis_label = str(meta.get("stock_label") or stock_mode_raw or "재고 기준 미확정")
        source_table_hint = ""
        source_table_label = ""

    summary = {
        "product_count": int(meta.get("product_count") or row_count),
        "shortage_item_count": int(meta.get("shortage_item_count") or shortage_item_count),
        "sum_current_stock_qty": _llm_json_safe_value(meta.get("sum_current_stock_qty") or sum_current_stock_qty),
        "sum_current_stock_amt": _llm_json_safe_value(meta.get("sum_current_stock_amt") or sum_current_stock_amt),
        "sum_shortage_1m_qty": _llm_json_safe_value(meta.get("sum_shortage_1m_qty") or sum_shortage_1m_qty),
        "sum_shortage_2m_qty": _llm_json_safe_value(meta.get("sum_shortage_2m_qty") or sum_shortage_2m_qty),
        "sum_shortage_3m_qty": _llm_json_safe_value(meta.get("sum_shortage_3m_qty") or sum_shortage_3m_qty),
        "stock_label": meta.get("stock_label") or meta.get("stock_mode") or params.get("stock_mode"),

        # 추가적으로 meta에서 summary로 옮겨 담는 값들 (LLM 분석에서 summary를 우선 참고하도록)
        "stock_mode": stock_mode_raw,
        "stock_basis_label": stock_basis_label,
        "source_table": source_table_hint,
        "source_table_label": source_table_label,

    }

    # ------------------------------------------------------------
    # 전체 원본 DataFrame 기준 추가 집계
    # LLM이 대표 샘플만 보지 않고 전체표 구조를 파악하도록 보강한다.
    # ------------------------------------------------------------
    whole_numeric_cols = [
        "현재재고수량",
        "현재재고금액",
        "1개월부족수량",
        "2개월부족수량",
        "3개월부족수량",
        "월평균출고수량",
        "최근3개월평균수량",
        "최근6개월평균수량",
        "재고커버월수",
    ]

    main_cols = [
        "제품코드",
        "제품명",
        "규격",
        "제조사명",
        "제품그룹명",
        "제품구분명",
        "제품분류명",
        "현재재고수량",
        "현재재고금액",
        "월평균출고수량",
        "1개월부족수량",
        "2개월부족수량",
        "3개월부족수량",
        "부족등급",
        "예상등급",
        "추세판정",
    ]

    whole_table_profile = {
        "row_count": row_count,
        "column_count": col_count,
        "shortage_grade_summary": _group_summary_records(
            df,
            group_col="부족등급",
            numeric_cols=whole_numeric_cols,
            n=20,
            sort_col="건수",
        ),
        "maker_shortage_top": _group_summary_records(
            df,
            group_col="제조사명",
            numeric_cols=["1개월부족수량", "2개월부족수량", "3개월부족수량", "현재재고수량", "현재재고금액"],
            n=15,
            sort_col="1개월부족수량",
        ),
        "product_group_shortage_top": _group_summary_records(
            df,
            group_col="제품그룹명",
            numeric_cols=["1개월부족수량", "2개월부족수량", "3개월부족수량", "현재재고수량", "현재재고금액"],
            n=15,
            sort_col="1개월부족수량",
        ),
        "product_type_shortage_top": _group_summary_records(
            df,
            group_col="제품구분명",
            numeric_cols=["1개월부족수량", "2개월부족수량", "3개월부족수량", "현재재고수량", "현재재고금액"],
            n=15,
            sort_col="1개월부족수량",
        ),
        "negative_stock_top": _df_top_records(
            df[df["현재재고수량"].map(lambda x: float(_analysis_to_num(pd.Series([x])).iloc[0])) < 0] if "현재재고수량" in df.columns else df.iloc[0:0],
            sort_col="현재재고수량",
            cols=main_cols,
            n=15,
            ascending=True,
        ),
        "shortage_1m_top": _df_top_records(
            df,
            sort_col="1개월부족수량",
            cols=main_cols,
            n=20,
            ascending=False,
        ),
        "shortage_2m_top": _df_top_records(
            df,
            sort_col="2개월부족수량",
            cols=main_cols,
            n=15,
            ascending=False,
        ),
        "shortage_3m_top": _df_top_records(
            df,
            sort_col="3개월부족수량",
            cols=main_cols,
            n=15,
            ascending=False,
        ),
        "current_stock_amt_low_top": _df_top_records(
            df,
            sort_col="현재재고금액",
            cols=main_cols,
            n=10,
            ascending=True,
        ),
        "current_stock_amt_high_top": _df_top_records(
            df,
            sort_col="현재재고금액",
            cols=main_cols,
            n=10,
            ascending=False,
        ),
    }

    analysis_text = (
        "SIMS_ANALYSIS_CONTEXT_V1\n"
        "중요: 아래 내용은 최신 SIMS 조회 결과 1건만을 대상으로 만든 LLM 분석용 컨텍스트입니다.\n"
        "이전 대화의 표/이전 SIMS 결과는 분석 대상에서 제외하세요.\n"
        "이 컨텍스트의 summary와 *_counts는 전체 원본 DataFrame 기준 집계입니다. 샘플 데이터라고 표현하지 마세요.\n"
        f"- action: {action_name}\n"
        f"- rows: {row_count}\n"
        f"- cols: {col_count}\n"
        f"- 품목수: {summary.get('product_count')}\n"
        f"- 부족품목수: {summary.get('shortage_item_count')}\n"
        f"- 현재재고수량합계: {summary.get('sum_current_stock_qty')}\n"
        f"- 1개월부족수량합계: {summary.get('sum_shortage_1m_qty')}\n"
        f"- 2개월부족수량합계: {summary.get('sum_shortage_2m_qty')}\n"
        f"- 3개월부족수량합계: {summary.get('sum_shortage_3m_qty')}\n"
        f"- 부족등급분포: {shortage_grade_counts}\n"
        f"- 재고기준: {summary.get('stock_basis_label')}\n"
        f"- 기준테이블: {summary.get('source_table_label') or '명시 안 됨'}\n"
        "답변은 전체 집계(summary/counts)를 우선 근거로 하고, 품목 설명은 risk_products_top과 grade_samples를 사용하세요.\n"
    )

    return {
        "kind": "SIMS_ANALYSIS_CONTEXT_V1",
        "analysis_target": "latest_sims_result_only",
        "analysis_key": str(uuid.uuid4()),
        "action": action_name,
        "params": params,
        "row_count": row_count,
        "column_count": col_count,
        "columns": [str(c) for c in df.columns],
        "summary": summary,
        "shortage_grade_counts": shortage_grade_counts,
        "forecast_grade_counts": forecast_grade_counts,
        "trend_judge_counts": trend_judge_counts,
        "risk_products_top": risk_products_top,
        "grade_samples": grade_samples,
        "whole_table_profile": whole_table_profile,

        "stock_basis_label": stock_basis_label,
        "source_table": source_table_hint,
        "source_table_label": source_table_label,

        "llm_rules": [
            "이 컨텍스트는 최신 SIMS 조회 결과 1건만 대상으로 한다.",
            "이전 표, 이전 조회 결과, 이전 답변은 분석 근거로 사용하지 않는다.",
            "summary와 whole_table_profile의 전체 집계를 우선 근거로 답한다.",
            "sample_records는 대표 목록일 수 있으므로 전체 건수/합계 판단에는 사용하지 않는다.",
            "부족등급, 예상등급, 추세판정, 현재재고수량, 부족수량을 중심으로 분석한다.",
            "내부 key 이름은 답변에 노출하지 않는다.",
        ],

        "analysis_text": analysis_text,
    }

def _sims_ctx_find_col(
    df: pd.DataFrame,
    *,
    exact: tuple[str, ...] = (),
    include_any: tuple[str, ...] = (),
    exclude_any: tuple[str, ...] = (),
) -> str | None:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None

    cols = [str(c) for c in df.columns]

    for target in exact:
        for c in cols:
            if c.strip() == target:
                return c

    for c in cols:
        s = c.strip()
        if include_any and not any(w in s for w in include_any):
            continue
        if exclude_any and any(w in s for w in exclude_any):
            continue
        return c

    return None


def _sims_ctx_num_series(df: pd.DataFrame, col: str | None) -> pd.Series:
    if not col or col not in df.columns:
        return pd.Series([0] * len(df), index=df.index, dtype="float64")

    s = df[col]

    if pd.api.types.is_numeric_dtype(s):
        return pd.to_numeric(s, errors="coerce").fillna(0)

    return pd.to_numeric(
        s.astype(str)
        .str.replace(",", "", regex=False)
        .str.replace("원", "", regex=False)
        .str.replace("개", "", regex=False)
        .str.strip(),
        errors="coerce",
    ).fillna(0)


def _sims_ctx_to_records(df: pd.DataFrame, limit: int = 20) -> list[dict]:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []

    out = df.head(limit).copy()

    for col in out.columns:
        try:
            if pd.api.types.is_float_dtype(out[col]):
                out[col] = out[col].round(2)
        except Exception:
            pass

    out = out.where(pd.notna(out), None)
    return out.to_dict(orient="records")

# SIMS 분석 컨텍스트의 업무 용어 보정
# - LLM이 출고/매출/입고/매입 같은 용어를 정확히 구분해서 쓰도록 유도하기 위한 보정값을 제공한다.
def _sims_business_terms(action_name: str) -> dict:
    """
    LLM 분석 컨텍스트 업무 용어 보정.
    내부 key 이름은 sales_*를 유지하더라도, 답변 용어는 action에 맞게 쓰게 한다.
    """
    action = str(action_name or "")

    if "제품수불현황" in action or "제품수불" in action:
        return {
            "flow_label": "제품수불",
            "amount_label": "수불금액",
            "vendor_label": "거래처",
            "qty_label": "수불수량",
            "avoid_words": ["매출액", "매출실적", "판매량", "인기 제품", "부진 제품"],
            "preferred_words": ["제품수불", "입고수량", "출고수량", "재고수량", "재고증감", "수불금액"],
        }

    if "실재고월집계" in action or "장부재고월집계" in action or "월집계" in action:
        return {
            "flow_label": "월집계",
            "amount_label": "입출고공급가액",
            "vendor_label": "거래처",
            "qty_label": "입출고수량",
            "avoid_words": ["매출 TOP", "판매량", "인기 제품", "부진 제품"],
            "preferred_words": ["월집계", "입고수량", "출고수량", "입고공급가액", "출고공급가액"],
        }


    if "제품재고현황" in action or "제품재고" in action:
        return {
            "flow_label": "제품재고",
            "amount_label": "재고금액",
            "vendor_label": "제조사",
            "qty_label": "재고수량",
            "avoid_words": ["매출액", "매출실적", "판매량"],
            "preferred_words": ["제품재고", "이월수량", "입고수량", "출고수량", "재고수량", "재고부족"],
        }

    if "검증" in action:
        return {
            "flow_label": "검증",
            "amount_label": "차이금액",
            "vendor_label": "거래처",
            "qty_label": "수량",
            "avoid_words": ["매출 TOP", "판매량", "인기 제품", "부진 제품"],
            "preferred_words": [
                "검증",
                "불일치",
                "누락",
                "공급가액 차이",
                "세액 차이",
                "거래처별 불일치",
                "제품별 불일치",
            ],
        }



    if "입고명세" in action:
        return {
            "flow_label": "입고",
            "amount_label": "입고금액",
            "vendor_label": "매입처",
            "qty_label": "입고수량",
            "avoid_words": ["매출", "매출금액", "매출액", "판매"],
            "preferred_words": ["입고", "매입", "입고금액", "매입처"],
        }

    if "출고명세" in action:
        return {
            "flow_label": "출고",
            "amount_label": "매출금액",
            "vendor_label": "매출처",
            "qty_label": "출고수량",
            "avoid_words": [],
            "preferred_words": ["출고", "매출", "매출금액", "매출처"],
        }

    if "거래명세서" in action:
        return {
            "flow_label": "거래명세서",
            "amount_label": "거래금액",
            "vendor_label": "거래처",
            "qty_label": "수량",
            "avoid_words": ["매출", "매출금액", "매출액", "판매", "판매량", "인기 제품", "부진 제품"],
            "preferred_words": ["거래명세서", "거래금액", "거래처", "매입분", "매출분", "거래명세서구분"],
        }

    if "세금계산서" in action:
        return {
            "flow_label": "세금계산서",
            "amount_label": "계산서금액",
            "vendor_label": "거래처",
            "qty_label": "수량",
            "avoid_words": ["판매", "판매량", "인기 제품", "부진 제품"],
            "preferred_words": ["세금계산서", "계산서금액", "거래처", "매입", "매출"],
        }

    return {
        "flow_label": "분석",
        "amount_label": "금액",
        "vendor_label": "거래처",
        "qty_label": "수량",
        "avoid_words": [],
        "preferred_words": [],
    }


def _apply_business_terms_to_sales_profiles(
    sales_time_profile: dict,
    sales_group_profile: dict,
    terms: dict,
) -> tuple[dict, dict]:
    """
    기존 sales_* profile의 내부 구조는 유지하되,
    records 안의 표시 컬럼명만 업무 용어에 맞게 바꾼다.
    """
    amount_label = str(terms.get("amount_label") or "금액")
    qty_label = str(terms.get("qty_label") or "수량")
    vendor_label = str(terms.get("vendor_label") or "거래처")

    def _rename_record_keys(rows: Any, *, vendor: bool = False) -> Any:
        if not isinstance(rows, list):
            return rows

        new_rows = []
        for r in rows:
            if not isinstance(r, dict):
                new_rows.append(r)
                continue

            nr = dict(r)

            if "매출금액" in nr and amount_label != "매출금액":
                nr[amount_label] = nr.pop("매출금액")

            if "수량" in nr and qty_label != "수량":
                nr[qty_label] = nr.pop("수량")

            if vendor and "거래처명" in nr and vendor_label != "거래처명":
                nr[vendor_label] = nr.pop("거래처명")

            new_rows.append(nr)

        return new_rows

    stp = dict(sales_time_profile or {})
    sgp = dict(sales_group_profile or {})

    for key in (
        "daily_sales_top",
        "daily_sales_top1",
        "monthly_sales",
        "monthly_sales_top",
        "weekday_sales",
        "weekday_sales_top",
    ):
        stp[key] = _rename_record_keys(stp.get(key), vendor=False)

    for key in (
        "product_sales_top",
        "product_quantity_top",
        "staff_sales",
    ):
        sgp[key] = _rename_record_keys(sgp.get(key), vendor=False)

    for key in (
        "vendor_sales_top",
        "vendor_quantity_top",
    ):
        sgp[key] = _rename_record_keys(sgp.get(key), vendor=True)

    stp["business_terms"] = terms
    stp["amount_label"] = amount_label
    stp["qty_label"] = qty_label

    sgp["business_terms"] = terms
    sgp["amount_label"] = amount_label
    sgp["qty_label"] = qty_label
    sgp["vendor_label"] = vendor_label

    return stp, sgp



# 출고/매출 상세 DF에서 일자별·월별·요일별 매출 집계를 만든다.
# LLM이 원본 8만 행을 직접 읽지 않고도 날짜 질문에 답할 수 있게 하는 용도.
def _build_sims_sales_time_profile(df: pd.DataFrame, terms: dict | None = None) -> dict:
    """
    일자별·월별·요일별 금액 집계.

    내부 key 이름은 sales_time_profile로 유지하지만,
    결과 컬럼명은 action별 업무 용어를 사용한다.
    예:
    - 입고명세: 입고금액
    - 출고명세: 매출금액
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return {}

    terms = terms or {}
    amount_label = str(terms.get("amount_label") or "금액")
    qty_label = str(terms.get("qty_label") or "수량")

    date_col = _sims_ctx_find_col(
        df,
        exact=("출고일자", "매출일자", "입고일자", "일자", "거래일자", "전표일자"),
        include_any=("일자", "날짜"),
        exclude_any=("등록", "수정", "보험", "유효", "마감"),
    )

    amount_col = _sims_ctx_find_col(
        df,
        exact=("합계금액", "매출금액", "출고금액", "입고금액", "공급가액", "입고공급가액", "출고공급가액"),
        include_any=("합계금액", "매출", "출고금액", "입고금액", "공급가액"),
        exclude_any=("단가", "율", "세액"),
    )

    qty_col = _sims_ctx_find_col(
        df,
        exact=("수량", "출고수량", "입고수량", "매출수량"),
        include_any=("수량",),
        exclude_any=("재고", "이월", "현재", "부족"),
    )

    # 세금계산서의 수량1, 수량2... 는 반복 상세 필드이므로
    # 일반 수량 분석 컬럼으로 사용하지 않는다.
    if qty_col and re.fullmatch(r"수량\d+", str(qty_col).strip()):
        qty_col = None

    try:
        log.info(
            "[SIMS_SALES_TIME_COLS] date_col=%s amount_col=%s qty_col=%s amount_label=%s qty_label=%s columns_head=%s",
            date_col,
            amount_col,
            qty_col,
            amount_label,
            qty_label,
            list(df.columns)[:30],
        )
    except Exception:
        pass

    if not date_col or not amount_col:
        return {
            "available": False,
            "reason": "일자 컬럼 또는 금액 컬럼을 찾지 못했습니다.",
            "date_col": date_col,
            "amount_col": amount_col,
            "qty_col": qty_col,
            "amount_label": amount_label,
            "qty_label": qty_label,
            "business_terms": terms,
        }

    raw_date = (
        df[date_col]
        .astype(str)
        .str.replace(r"\D", "", regex=True)
        .str[:8]
    )
    dt_s = pd.to_datetime(raw_date, format="%Y%m%d", errors="coerce")

    work = pd.DataFrame(
        {
            "_dt": dt_s,
            "_amount": _sims_ctx_num_series(df, amount_col),
            "_qty": _sims_ctx_num_series(df, qty_col),
        }
    )
    work = work.dropna(subset=["_dt"])

    if work.empty:
        return {
            "available": False,
            "reason": "일자 변환 결과가 없습니다.",
            "date_col": date_col,
            "amount_col": amount_col,
            "qty_col": qty_col,
            "amount_label": amount_label,
            "qty_label": qty_label,
            "business_terms": terms,
        }

    weekday_names = ["월", "화", "수", "목", "금", "토", "일"]

    work["일자"] = work["_dt"].dt.strftime("%Y-%m-%d")
    work["월"] = work["_dt"].dt.strftime("%Y-%m")
    work["요일번호"] = work["_dt"].dt.weekday
    work["요일"] = work["요일번호"].map(lambda x: weekday_names[int(x)] if pd.notna(x) else "")

    daily = (
        work.groupby(["일자", "요일"], dropna=False)
        .agg(
            건수=("_amount", "size"),
            **{qty_label: ("_qty", "sum")},
            **{amount_label: ("_amount", "sum")},
        )
        .reset_index()
        .sort_values(amount_label, ascending=False)
    )

    monthly = (
        work.groupby("월", dropna=False)
        .agg(
            건수=("_amount", "size"),
            **{qty_label: ("_qty", "sum")},
            **{amount_label: ("_amount", "sum")},
        )
        .reset_index()
        .sort_values("월", ascending=True)
    )

    weekday = (
        work.groupby(["요일번호", "요일"], dropna=False)
        .agg(
            건수=("_amount", "size"),
            **{qty_label: ("_qty", "sum")},
            **{amount_label: ("_amount", "sum")},
        )
        .reset_index()
        .sort_values("요일번호", ascending=True)
        .drop(columns=["요일번호"])
    )

    def _safe_amount_sort(_df: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(_df, pd.DataFrame) or _df.empty:
            return _df

        for cand in (amount_label, "입고금액", "매출금액", "금액", "합계금액", "공급가액"):
            if cand in _df.columns:
                return _df.sort_values(cand, ascending=False)

        return _df

    return {
        "available": True,
        "date_col": date_col,
        "amount_col": amount_col,
        "qty_col": qty_col,
        "amount_label": amount_label,
        "qty_label": qty_label,
        "business_terms": terms,
        "daily_sales_top": _sims_ctx_to_records(daily, 20),
        "daily_sales_top1": _sims_ctx_to_records(daily, 1),
        "monthly_sales": _sims_ctx_to_records(monthly, 120),
        "monthly_sales_top": _sims_ctx_to_records(
            _safe_amount_sort(monthly),
            20,
        ),
        "weekday_sales": _sims_ctx_to_records(weekday, 7),
        "weekday_sales_top": _sims_ctx_to_records(
            _safe_amount_sort(weekday),
            7,
        ),
    }


def _build_sims_sales_group_profile(df: pd.DataFrame, terms: dict | None = None) -> dict:
    """
    제품/거래처/영업사원 기준 금액·수량 TOP 집계.

    내부 key 이름은 sales_group_profile로 유지하지만,
    결과 컬럼명은 action별 업무 용어를 사용한다.
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return {}

    terms = terms or {}
    amount_label = str(terms.get("amount_label") or "금액")
    qty_label = str(terms.get("qty_label") or "수량")
    vendor_label = str(terms.get("vendor_label") or "거래처")

    amount_col = _sims_ctx_find_col(
        df,
        exact=("합계금액", "매출금액", "출고금액", "입고금액", "공급가액", "입고공급가액", "출고공급가액"),
        include_any=("합계금액", "매출", "출고금액", "입고금액", "공급가액"),
        exclude_any=("단가", "율", "세액"),
    )
    qty_col = _sims_ctx_find_col(
        df,
        exact=("수량", "출고수량", "입고수량", "매출수량"),
        include_any=("수량",),
        exclude_any=("재고", "이월", "현재", "부족"),
    )

    # 세금계산서의 수량1, 수량2... 는 반복 상세 필드이므로
    # 일반 수량 분석 컬럼으로 사용하지 않는다.
    if qty_col and re.fullmatch(r"수량\d+", str(qty_col).strip()):
        qty_col = None

    product_col = _sims_ctx_find_col(
        df,
        exact=("제품명", "품목명", "상품명"),
        include_any=("제품명", "품목명", "상품명"),
        exclude_any=("코드", "번호", "분류", "구분"),
    )
    vendor_col = _sims_ctx_find_col(
        df,
        exact=("거래처명", "매출처명", "매입처명", "실납처명", "납품처명"),
        include_any=("거래처", "매출처", "매입처", "실납처", "납품처"),
        exclude_any=("코드", "번호", "분류", "구분"),
    )
    staff_col = _sims_ctx_find_col(
        df,
        exact=("영업사원명", "영업담당자명", "담당자명", "사원명"),
        include_any=("영업사원", "영업담당", "담당자", "사원"),
        exclude_any=("코드", "번호", "ID", "아이디"),
    )

    work = pd.DataFrame(index=df.index)
    work["_amount"] = _sims_ctx_num_series(df, amount_col)
    work["_qty"] = _sims_ctx_num_series(df, qty_col)

    def _group_top(group_col: str | None, label: str, sort_by: str) -> list[dict]:
        if not group_col or group_col not in df.columns:
            return []

        tmp = work.copy()
        tmp[label] = df[group_col].astype(str).str.strip()
        tmp = tmp[tmp[label] != ""]

        if tmp.empty:
            return []

        grouped = (
            tmp.groupby(label, dropna=False)
            .agg(
                건수=("_amount", "size"),
                **{qty_label: ("_qty", "sum")},
                **{amount_label: ("_amount", "sum")},
            )
            .reset_index()
        )

        sort_col = qty_label if sort_by == "qty" else amount_label
        grouped = grouped.sort_values(sort_col, ascending=False)
        return _sims_ctx_to_records(grouped, 20)

    return {
        "amount_col": amount_col,
        "qty_col": qty_col,
        "product_col": product_col,
        "vendor_col": vendor_col,
        "staff_col": staff_col,
        "amount_label": amount_label,
        "qty_label": qty_label,
        "vendor_label": vendor_label,
        "business_terms": terms,
        "product_sales_top": _group_top(product_col, "제품명", "amount"),
        "product_quantity_top": _group_top(product_col, "제품명", "qty"),
        "vendor_sales_top": _group_top(vendor_col, vendor_label, "amount"),
        "vendor_quantity_top": _group_top(vendor_col, vendor_label, "qty"),
        "staff_sales": _group_top(staff_col, "영업사원명", "amount"),
    }

def _build_sims_doc_division_profile(df: pd.DataFrame, action_name: str = "") -> dict:
    """
    거래명세서/세금계산서용 구분별 요약.

    거래명세서 공통:
    - 거래명세서구분명 기준 매입분/매출분 거래금액
    세금계산서 공통:
    - 계산서구분/매입매출 구분이 있으면 같은 방식으로 집계
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return {}

    action = str(action_name or "")

    if not any(w in action for w in ("거래명세서", "세금계산서")):
        return {}

    div_col = _sims_ctx_find_col(
        df,
        exact=(
            "거래명세서구분명",
            "거래명세서구분",
            "세금계산서구분명",
            "세금계산서구분",
            "계산서구분명",
            "계산서구분",
            "매입매출구분명",
            "매입매출구분",
        ),
        include_any=("구분", "매입매출"),
        exclude_any=("코드", "번호", "순번"),
    )

    amount_col = _sims_ctx_find_col(
        df,
        exact=("합계금액", "거래금액", "계산서금액", "공급가액"),
        include_any=("합계금액", "거래금액", "계산서금액", "공급가액"),
        exclude_any=("상세합", "단가", "율"),
    )

    supply_col = _sims_ctx_find_col(
        df,
        exact=("공급가액",),
        include_any=("공급가액",),
        exclude_any=("상세합", "단가", "율"),
    )

    tax_col = _sims_ctx_find_col(
        df,
        exact=("세액",),
        include_any=("세액",),
        exclude_any=("상세합", "단가", "율"),
    )

    if not div_col or not amount_col:
        return {
            "available": False,
            "reason": "거래명세서/세금계산서 구분 컬럼 또는 금액 컬럼을 찾지 못했습니다.",
            "division_col": div_col,
            "amount_col": amount_col,
        }

    work = pd.DataFrame({
        "구분": df[div_col].astype(str).str.strip(),
        "_amount": _sims_ctx_num_series(df, amount_col),
        "_supply": _sims_ctx_num_series(df, supply_col) if supply_col else 0,
        "_tax": _sims_ctx_num_series(df, tax_col) if tax_col else 0,
    })

    work = work[work["구분"] != ""]

    if work.empty:
        return {
            "available": False,
            "reason": "구분 값이 비어 있습니다.",
            "division_col": div_col,
            "amount_col": amount_col,
        }

    amount_label = "계산서금액" if "세금계산서" in action else "거래금액"

    grouped = (
        work.groupby("구분", dropna=False)
        .agg(
            건수=("_amount", "size"),
            공급가액=("_supply", "sum"),
            세액=("_tax", "sum"),
            **{amount_label: ("_amount", "sum")},
        )
        .reset_index()
        .sort_values(amount_label, ascending=False)
    )

    total_amount = float(grouped[amount_label].sum()) if amount_label in grouped.columns else 0.0
    if total_amount:
        grouped["비율"] = grouped[amount_label] / total_amount * 100.0

    return {
        "available": True,
        "division_col": div_col,
        "amount_col": amount_col,
        "amount_label": amount_label,
        "division_summary": _sims_ctx_to_records(grouped, 20),
    }

def _build_sims_analysis_context_from_df(
    base_df: pd.DataFrame,
    *,
    result: Dict[str, Any],
    action_name: str,
    params: Dict[str, Any],
    meta: Dict[str, Any],
) -> Dict[str, Any]:
    """화면용 표와 분리된 LLM 분석 전용 컨텍스트 생성."""
    if not isinstance(base_df, pd.DataFrame) or base_df.empty:
        return {}

    action = str(action_name or meta.get("action") or result.get("title") or "").strip()
    analysis_type = str(meta.get("analysis_type") or "").strip()

    if action == "품목별 재고부족현황" or analysis_type == "stock_shortage":
        return _build_stock_shortage_analysis_ctx(
            base_df,
            action_name=action,
            params=params,
            meta=meta,
        )

    fallback_cols = list(base_df.columns[:25])

    # 입고명세/출고명세는 nlq_router에서 만든 전체 집계 meta를 우선 사용한다.
    detail_summary = {}
    if isinstance(meta.get("in_detail_summary"), dict):
        detail_summary = dict(meta.get("in_detail_summary") or {})
    elif isinstance(meta.get("out_detail_summary"), dict):
        detail_summary = dict(meta.get("out_detail_summary") or {})
    elif isinstance(meta.get("trans_doc_summary"), dict):
        detail_summary = dict(meta.get("trans_doc_summary") or {})
    elif isinstance(meta.get("tax_doc_summary"), dict):
        detail_summary = dict(meta.get("tax_doc_summary") or {})
    elif isinstance(meta.get("monthly_stock_detail_summary"), dict):
        detail_summary = dict(meta.get("monthly_stock_detail_summary") or {})
    elif isinstance(meta.get("inventory_summary"), dict):
        detail_summary = dict(meta.get("inventory_summary") or {})



    llm_summary_md = str(meta.get("llm_summary_md") or meta.get("summary_md") or "").strip()

    analysis_row_count = int(
        detail_summary.get("row_count_total")
        or detail_summary.get("row_count")
        or meta.get("analysis_row_count")
        or len(base_df)
        or 0
    )

    display_row_count = int(
        detail_summary.get("display_row_count")
        or meta.get("display_row_count")
        or len(base_df)
        or 0
    )

    business_terms = _sims_business_terms(action)

    sales_time_profile = _build_sims_sales_time_profile(base_df, business_terms)
    sales_group_profile = _build_sims_sales_group_profile(base_df, business_terms)

    sales_time_profile, sales_group_profile = _apply_business_terms_to_sales_profiles(
        sales_time_profile,
        sales_group_profile,
        business_terms,
    )

    doc_division_profile = _build_sims_doc_division_profile(base_df, action)

    # 거래명세서/세금계산서는 sales_*라는 내부 key 이름이 LLM 답변에 노출되기 쉽다.
    # 그래서 문서계열 조회에서는 사용자용 doc_* profile로 복사하고,
    # sales_* profile은 빈 dict로 내려보낸다.
    is_doc_action = any(w in str(action) for w in ("거래명세서", "세금계산서"))
    is_stock_action = any(w in str(action) for w in ("제품수불현황", "제품재고현황", "제품수불", "제품재고"))

    doc_time_profile = {}
    doc_counterparty_profile = {}

    sales_time_profile_out = sales_time_profile
    sales_group_profile_out = sales_group_profile

    if is_doc_action or is_stock_action:
        amount_label = str(business_terms.get("amount_label") or "금액")
        vendor_label = str(business_terms.get("vendor_label") or "거래처")

        doc_time_profile = {
            "available": bool((sales_time_profile or {}).get("available")),
            "date_col": (sales_time_profile or {}).get("date_col"),
            "amount_col": (sales_time_profile or {}).get("amount_col"),
            "amount_label": amount_label,
            "daily_amount_top": (sales_time_profile or {}).get("daily_sales_top") or [],
            "daily_amount_top1": (sales_time_profile or {}).get("daily_sales_top1") or [],
            "monthly_amount": (sales_time_profile or {}).get("monthly_sales") or [],
            "monthly_amount_top": (sales_time_profile or {}).get("monthly_sales_top") or [],
            "weekday_amount": (sales_time_profile or {}).get("weekday_sales") or [],
            "weekday_amount_top": (sales_time_profile or {}).get("weekday_sales_top") or [],
        }

        doc_counterparty_profile = {
            "amount_label": amount_label,
            "vendor_label": vendor_label,
            "vendor_amount_top": (sales_group_profile or {}).get("vendor_sales_top") or [],
            "vendor_quantity_top": (sales_group_profile or {}).get("vendor_quantity_top") or [],
            "staff_amount": (sales_group_profile or {}).get("staff_sales") or [],
        }

        # 중요: 문서계열에서는 sales_* key를 비워서 LLM이 내부명 그대로 답하지 못하게 한다.
        sales_time_profile_out = {}
        sales_group_profile_out = {}


#   임시로그
    try:
        log.info(
            "[SIMS_ANALYSIS_PROFILE] action=%s flow=%s amount_label=%s division_available=%s time_available=%s daily_top=%s monthly=%s product_qty_top=%s product_sales_top=%s",
            action,
            business_terms.get("flow_label"),
            business_terms.get("amount_label"),
            bool((doc_division_profile or {}).get("available")),
            bool((sales_time_profile or {}).get("available")),
            len((sales_time_profile or {}).get("daily_sales_top") or []),
            len((sales_time_profile or {}).get("monthly_sales") or []),
            len((sales_group_profile or {}).get("product_quantity_top") or []),
            len((sales_group_profile or {}).get("product_sales_top") or []),
        )

    except Exception:
        pass
#   임시로그

    keep_meta_keys = {
        "summary_md",
        "llm_summary_md",
        "message",
        "query_summary",
        "condition",
        "trend_judge_counts",
        "forecast_grade_counts",
        "shortage_grade_counts",
        "analysis_type",
        "source_mode",
        "stock_mode",
        "in_detail_summary",
        "out_detail_summary",
        "inventory_summary",
        "inventory_target_scope",
        "zero_stock_count",
        "zero_or_negative_stock_count",
        "negative_stock_count",
        "qty_sum",
        "supply_sum",
        "tax_sum",
        "amount_sum",
        "vendor_count",
        "product_count",
        "stock_location_count",
        "staff_count",
        "display_row_count",
        "analysis_row_count",
        "row_count_total_for_analysis",
        "field_notes",
        "trans_doc_summary",
        "dc_sum",
        "mismatch_count",
        "tax_doc_summary",
        "detail_missing_count",
        "accounting_count",
        "monthly_stock_detail_summary",
        "stock_basis",
        "stock_apply_count",
        "sum_in_qty",
        "sum_in_bonus_qty",
        "sum_in_supply_amt",
        "sum_in_tax_amt",
        "sum_out_qty",
        "sum_out_bonus_qty",
        "sum_out_supply_amt",
        "sum_out_tax_amt",
    }

    analysis_text = (
        "SIMS_ANALYSIS_CONTEXT_V1\n"
        "최신 SIMS 조회 결과 1건만 대상으로 만든 LLM 분석용 컨텍스트입니다.\n"
        "중요: 화면 표는 일부 행만 표시될 수 있습니다.\n"
        "입고명세/출고명세/거래명세서/세금계산서/월집계는 detail_summary와 llm_summary_md의 "
        "전체 집계를 우선 근거로 답하세요.\n"
        f"- action: {action}\n"
        f"- rows_display: {display_row_count}\n"
        f"- rows_for_analysis: {analysis_row_count}\n"
        f"- cols: {len(base_df.columns)}\n"
    )

    analysis_text += (
        f"- 업무흐름: {business_terms.get('flow_label')}\n"
        f"- 금액표현: {business_terms.get('amount_label')}\n"
        f"- 거래처표현: {business_terms.get('vendor_label')}\n"
        f"- 수량표현: {business_terms.get('qty_label')}\n"
    )
    
    if "거래명세서" in str(action) or "세금계산서" in str(action):
        has_product_col = any(
            str(c).strip() in ("제품명", "제품코드", "품목명", "상품명")
            for c in base_df.columns
        )
        analysis_text += (
            f"- 제품컬럼존재: {'Y' if has_product_col else 'N'}\n"
            "- 제품컬럼존재가 N이면 제품별/품목별/판매량 분석을 제안하지 마세요.\n"
            "- 거래명세서 공통은 거래처별, 거래명세서구분별, 일자별 거래금액 분석을 우선 제안하세요.\n"
            "- 세금계산서 공통은 거래처별, 세금계산서구분별, 일자별 계산서금액 분석을 우선 제안하세요.\n"
            "- 수량 합계가 0이거나 수량 컬럼이 제품 반복필드 일부이면 수량 기반 분석을 하지 마세요.\n"
        )    

    if "거래명세서" in str(action) or "세금계산서" in str(action):
        if (doc_division_profile or {}).get("available"):
            analysis_text += (
                "- 거래명세서/세금계산서 구분별 요약이 포함되어 있습니다.\n"
                "- 답변에는 매입분/매출분 또는 구분별 거래금액 차이를 반드시 포함하세요.\n"
            )
        else:
            analysis_text += (
                "- 거래명세서/세금계산서 구분별 요약을 만들 수 없습니다.\n"
            )


    if llm_summary_md:
        analysis_text += "\n[LLM 요약/전체 집계]\n" + llm_summary_md + "\n"

    return {
        "kind": "SIMS_ANALYSIS_CONTEXT_V1",
        "analysis_target": "latest_sims_result_only",
        "analysis_key": str(uuid.uuid4()),
        "action": action,
        "params": params,
        "row_count": analysis_row_count,
        "display_row_count": display_row_count,
        "column_count": int(len(base_df.columns)),
        "columns": [str(c) for c in base_df.columns],
        "meta": {
            k: v
            for k, v in meta.items()
            if k in keep_meta_keys
        },

        "detail_summary": detail_summary,
        "llm_summary_md": llm_summary_md,
        
        "business_terms": business_terms,
        "doc_division_profile": doc_division_profile,
        "doc_time_profile": doc_time_profile,
        "doc_counterparty_profile": doc_counterparty_profile,
        "sales_time_profile": sales_time_profile_out,
        "sales_group_profile": sales_group_profile_out,
        "sample_records": _llm_records_from_df(base_df, fallback_cols, limit=30),

        "llm_rules": [
            "이 컨텍스트는 최신 SIMS 조회 결과 1건만 대상으로 한다.",
            "이전 표, 이전 조회 결과, 이전 답변은 분석 근거로 사용하지 않는다.",
            "입고명세/출고명세/거래명세서/세금계산서는 detail_summary와 llm_summary_md를 우선 근거로 답한다.",
            "business_terms가 있으면 답변 용어는 반드시 business_terms의 amount_label, vendor_label, flow_label을 사용한다.",
            "action이 입고명세 조회이면 '매출', '매출금액', '매출액'이라고 쓰지 말고 '입고', '매입', '입고금액', '매입처'라고 표현한다.",
            "action이 출고명세 조회이면 '출고', '매출', '매출금액', '매출처'라고 표현한다.",
            "action이 거래명세서 공통 조회이면 '매출', '판매량', '인기 제품', '부진 제품'이라고 단정하지 말고 '거래명세서', '거래금액', '거래처', '매입분/매출분' 중심으로 표현한다.",
            "거래명세서 공통 조회에서 제품명/제품코드 컬럼이 없으면 제품별 분석이나 제품별 판매량 분석을 제안하지 않는다.",
            "거래명세서 공통 조회의 현재 컬럼에 제품명/제품코드가 없으면 '품목별', '제품별', '판매량', '인기 제품', '부진 제품' 분석을 제안하지 않는다.",
            "거래명세서 공통 조회에서는 '매출 추이'라고 단정하지 말고, 매출분 조건이 명시된 경우에만 '매출분 거래금액 추이'라고 표현한다.",
            "action이 거래명세서 공통 조회이면 구분별 요약을 확인해 매입분/매출분 또는 거래명세서구분별 거래금액 요약을 반드시 포함한다.",
            "거래명세서 공통 조회에서 제품명/제품코드 컬럼이 없으면 제품별/품목별/판매량 분석을 제안하지 말고 거래처별, 거래명세서구분별, 일자별 거래금액 분석을 제안한다.",
            "action이 세금계산서 공통 조회이면 '세금계산서', '계산서금액', '거래처', '매입/매출' 중심으로 표현한다.",
            "action이 세금계산서 공통 조회이면 내부 영문 key 이름이나 내부 프로필명을 답변에 노출하지 않는다.",
            "세금계산서 공통 조회에서 표준 제품명/제품코드 컬럼이 없으면 제품별/품목별/판매량 분석을 제안하지 말고 거래처별, 세금계산서구분별, 일자별 계산서금액 분석을 제안한다.",
            "세금계산서 공통 조회에서는 수량 컬럼이 있어도 수량 합계가 0이면 수량 기반 분석을 하지 않는다.",
            "sample_records는 대표 목록일 수 있으므로 전체 건수/합계 판단에는 사용하지 않는다.",
            "화면표시건수와 row_count가 다르면, 화면에는 일부만 표시되고 분석은 전체 조회조건 기준이라고 설명한다.",
            "내부 key 이름은 답변에 노출하지 않는다.",
        ],


        "analysis_text": analysis_text,
    }

# SIMS 뷰 함수가 반환한 결과를 채팅 컨텍스트에 올릴 표준 컨테이너로 변환하는 함수
# - 위 _normalize_result_for_chat()로 표준 payload로 변환한 후, LLM이 소비하기 좋은 JSON 컨테이너 + 텍스트로 변환한다.
# - 세션 상태에 '최신 1개 컨텍스트'를 저장한다.
# - 반환값은 컨텍스트 패키지(dict)로, 내부에 원본 payload/result 뿐만 아니라 LLM용 JSON 컨테이너와 텍스트 설명이 포함된다.
def _build_sims_context_from_result(
    result: Dict[str, Any],
    action_name: str,
    params: Dict[str, Any],
    df_llm: pd.DataFrame,
) -> Dict[str, Any]:
    """
    SIMS 결과(DataFrame)를 LLM이 소비하기 좋은 JSON 컨테이너 + 텍스트로 변환하고,
    세션 상태에 '최신 1개 컨텍스트'를 저장한다.
    """
    ss = st.session_state

    # 0) 기본 값
    cols = [str(c) for c in df_llm.columns]
    now = dt.datetime.now()

    # 🔹 원본 meta 중 LLM 분석에 필요한 요약/집계 정보는 보존한다.
    # - 예전에는 TopN/source 정도만 넘겨서 summary_md, *_counts 등이 LLM에 전달되지 않았다.
    # - 큰 원본(df/records/columns)은 제외하고, JSON 직렬화 가능한 작은 meta만 보존한다.
    original_meta = dict(result.get("meta") or {})
    meta: Dict[str, Any] = {}

    def _is_json_safe_meta_value(v: Any) -> bool:
        if isinstance(v, pd.DataFrame):
            return False
        if callable(v):
            return False
        try:
            json.dumps(v, ensure_ascii=False, default=str)
            return True
        except Exception:
            return False

    drop_meta_keys = {
        "df",
        "df_display",
        "data",
        "records",
        "columns",
        "html",
        "style",
        "styler",
    }

    for k, v in original_meta.items():
        sk = str(k)
        if sk in drop_meta_keys:
            continue

        # 내부 제어 플래그는 LLM에 불필요하다.
        # 단, source/action/summary/summary_md/counts 등 일반 meta는 보존된다.
        if sk.startswith("_"):
            continue

        if not _is_json_safe_meta_value(v):
            continue

        # 긴 문자열은 요약/프롬프트 오염 방지를 위해 제한한다.
        if isinstance(v, str) and len(v) > 6000:
            v = v[:6000] + "\n...(meta 문자열 길이 제한으로 이후 생략)"

        # 너무 큰 dict/list는 대표 요약만 보존한다.
        if isinstance(v, (dict, list, tuple)):
            try:
                raw = json.dumps(v, ensure_ascii=False, default=str)
                if len(raw) > 12000:
                    v = {
                        "_omitted": True,
                        "reason": "meta value too large",
                        "length": len(raw),
                    }
            except Exception:
                continue

        meta[sk] = v

    meta.setdefault("source", original_meta.get("source") or "SIMS")

    #  === 1-1) 전체 DataFrame 확보 (집계/요약용, 주로 로그/텍스트 컨텍스트 용도) ===
    base_df = result.get("df")
    if not isinstance(base_df, pd.DataFrame):
        base_df = df_llm

    # 🔴 total_rows 를 '전체 DF' 기준으로 계산 (df_llm 길이 X)
    if isinstance(base_df, pd.DataFrame):
        try:
            total_rows = int(result.get("count") or len(base_df))
        except Exception:
            total_rows = len(base_df)
    else:
        # 그래도 없으면 df_llm 기준으로라도 계산
        try:
            total_rows = int(result.get("count") or len(df_llm))
        except Exception:
            total_rows = len(df_llm)

    # === 1-2) 부서 컬럼 찾아보기 (meta 우선, 없으면 컬럼명으로 추론) ===
    dept_col = meta.get("부서컬럼")
    if not dept_col and isinstance(base_df, pd.DataFrame):
        for cand in ["부서명", "부서", "부서 명"]:
            if cand in base_df.columns:
                dept_col = cand
                break
        # 그래도 없으면 코드라도 사용
        if not dept_col:
            for cand in ["부서 상세코드", "부서코드"]:
                if cand in base_df.columns:
                    dept_col = cand
                    break

    include_dept_summary = str(action_name or "").strip() == "부서별 사용자 수"


    # === 1-3) (선택) 부서별 인원 수 집계 (전체 DF 기준, LLM JSON이 아닌 로그/텍스트용) ===
    dept_counts_df = None
    if include_dept_summary and dept_col and isinstance(base_df, pd.DataFrame) and dept_col in base_df.columns:
        try:
            dept_counts_df = (
                base_df.groupby(dept_col, dropna=False)
                .size()
                .reset_index(name="인원수")
            )
        except Exception:
            log.exception("[SIMS_CTX] dept aggregation failed")

    # 🔹 LLM 에 넘길 샘플 행 수는 별도로 제한 (토큰/속도 최적화)
    #    → 전체 건수(total_rows)는 base_df 기준이므로 df_llm을 줄여도 전체 통계에는 영향 없음
    if isinstance(df_llm, pd.DataFrame):
        pass
#        df_llm = df_llm.head(80)  # 필요시 50/100 등으로 조정 가능

    # 🔹 meta 에 전체/샘플 정보 명시적으로 기록
    meta.setdefault("action", action_name)
    meta.setdefault("params", params)
    meta.setdefault("generated_at", now.isoformat(timespec="seconds"))

    # ✅ 여기서만은 '강제 덮어쓰기'가 맞음 (예전 잘못된 값/키 무시)
    meta["row_count_total"] = int(total_rows)                  # 전체 행 수
    meta.setdefault("row_count", int(total_rows))              # 없으면 전체로
    meta["sample_rows"] = int(len(df_llm))                     # 실제 LLM 샘플 길이

    # 2) 2) LLM 용 JSON 컨테이너 (records는 위에서 head()로 제한된 df_llm 기반)
    data_container: Dict[str, Any] = {
        "columns": cols,
        "records": df_llm.to_dict(orient="records"),
        "meta": meta,   # 🔴 위에서 만든 meta 그대로 사용
    }

    # 🔹 (선택) 거래처/영업사원 등 추가 집계도 JSON 안에 넣어둔다.
    #     - records는 샘플일 수 있으므로, "전체 집계"가 필요한 질문은 여기 aggregations를 우선 사용하도록 유도
    try:
        cols2 = list(map(str, base_df.columns))

        def _clean_series(col: str):
            s = base_df[col]
            # dropna → str → strip → 빈값 제거 (NaN이 "nan" 문자열로 변하는 것 방지)
            s2 = s.dropna().astype(str).str.strip()
            return s2[s2 != ""]

        # 1) 영업사원 집계 (거래처 마스터에서 자주 필요)
        sales_name_col = next((c for c in cols2 if "영업사원명" in c), None)
        sales_code_col = next((c for c in cols2 if "영업사원코드" in c), None)
        if sales_name_col or sales_code_col:
            col_for_count = sales_code_col or sales_name_col
            s_sales = _clean_series(col_for_count)
            sales_distinct = int(s_sales.nunique()) if len(s_sales) else 0
            top20 = s_sales.value_counts().head(20).to_dict() if len(s_sales) else {}
            data_container.setdefault("aggregations", {})["sales_reps"] = {
                "column": col_for_count,
                "name_column": sales_name_col,
                "code_column": sales_code_col,
                "distinct_count": sales_distinct,
                "top20": top20,
            }

        # 2) 거래처 분류(종류/등급/그룹) 집계
        kind_col = next((c for c in cols2 if c == "거래처종류명" or ("거래처" in c and "종류" in c and "명" in c)), None)
        rank_col = next((c for c in cols2 if c == "거래처등급명" or ("거래처" in c and "등급" in c and "명" in c)), None)
        group_col = next((c for c in cols2 if c == "거래처그룹명" or ("거래처" in c and "그룹" in c and "명" in c)), None)

        def _attach_counts(key: str, col: str | None):
            if not col:
                return
            s = _clean_series(col)
            data_container.setdefault("aggregations", {})[key] = {
                "column": col,
                "distinct_count": int(s.nunique()) if len(s) else 0,
                "top20": s.value_counts().head(20).to_dict() if len(s) else {},
            }

        _attach_counts("vendor_kind", kind_col)
        _attach_counts("vendor_rank", rank_col)
        _attach_counts("vendor_group", group_col)

    except Exception:
        log.exception("[sims.ctx] build aggregations failed")

    #     - LLM 에게는 "records 는 샘플, aggregations.by_department.rows 는 전체 집계" 라고 안내할 예정
    if include_dept_summary and dept_counts_df is not None and dept_col:
        data_container.setdefault("aggregations", {})
        data_container["aggregations"]["by_department"] = {
            "column": dept_col,
            "rows": dept_counts_df.to_dict(orient="records"),
        }
        
    # (옵션) 디버그용 로그: meta 에 뭐가 들어갔는지 확인
    try:
        log.info(
            "[SIMS_CTX_DEBUG] meta.row_count=%r, row_count_total=%r, sample_rows=%r",
            meta.get("row_count"),
            meta.get("row_count_total"),
            meta.get("sample_rows"),
        )
    except Exception:
        pass

    # 3) 텍스트 버전 구성
    # 3-1) 샘플 CSV (상위 20행)
    sample_rows = min(len(df_llm), 20)
    csv_buf = io.StringIO()
    df_llm.head(sample_rows).to_csv(csv_buf, index=False)
    csv_text = csv_buf.getvalue().strip()

    # 3--2) 부서별 인원수 요약 텍스트 (전체 DF 기준, 사람/로그용 설명)
    dept_summary_text = ""
    if dept_counts_df is not None and not dept_counts_df.empty:
        dept_buf = io.StringIO()
        dept_counts_df.to_csv(dept_buf, index=False)
        dept_summary_text = (
            f"\n\n[부서별 인원 수 요약 (전체 {total_rows}행 기준)]\n"
            f"{dept_buf.getvalue().strip()}"
        )

    # 3-3) 최종 컨텍스트 텍스트
    header = (
        "SIMS ERP 데이터 컨텍스트입니다.\n"
        f"- action = {action_name}\n"
        f"- 샘플 행 수(sample_rows) = {len(df_llm)}\n"
    )

    ctx_text = header
    if dept_summary_text:
        ctx_text += dept_summary_text

    ctx_text += "\n\n[상위 행 CSV 샘플]\n" + csv_text

    # 3-4) LLM 분석 전용 컨텍스트 생성
    # 화면용 표와 분리해서, LLM은 최신 분석용 컨텍스트만 보게 한다.

    prev_analysis_ctx = ss.get("__sims_analysis_ctx")

    analysis_ctx: Dict[str, Any] = {}
    try:
        analysis_ctx = _build_sims_analysis_context_from_df(
            base_df,
            result=result,
            action_name=action_name,
            params=params,
            meta=meta,
        )
    

        if analysis_ctx:
            is_current_table_followup = bool(meta.get("current_table_followup"))
            source_table_key = str(meta.get("source_table_key") or "").strip()
            table_key = str(meta.get("table_key") or ss.get("__sims_last_table_key") or "").strip()

            analysis_ctx["current_table_followup"] = is_current_table_followup
            analysis_ctx["source_table_key"] = source_table_key
            analysis_ctx["table_key"] = table_key

            # 현재표 후속 파생표(TOP/상세표)가 만들어져도,
            # LLM이 "현재표 ..." 후속분석을 할 때 볼 원본 분석 컨텍스트는 따로 유지한다.
            if is_current_table_followup:
                if (
                    source_table_key
                    and not isinstance(ss.get("__sims_current_table_source_analysis_ctx"), dict)
                    and isinstance(prev_analysis_ctx, dict)
                ):
                    ss["__sims_current_table_source_analysis_ctx"] = prev_analysis_ctx
            else:
                ss["__sims_current_table_source_analysis_ctx"] = analysis_ctx

            data_container = analysis_ctx
            ctx_text = str(analysis_ctx.get("analysis_text") or ctx_text)
            log.info(
                "[SIMS_ANALYSIS_CTX_UPDATED] action=%s rows=%s cols=%s risk_top=%s current_followup=%s source_key=%s",
                action_name,
                analysis_ctx.get("row_count"),
                analysis_ctx.get("column_count"),
                len(analysis_ctx.get("risk_products_top") or []),
                is_current_table_followup,
                source_table_key,
            )


    except Exception:
        log.exception("[SIMS_ANALYSIS_CTX] build failed")

    ctx_pack: Dict[str, Any] = {
        "text": ctx_text,
        "data": data_container,
        "action": action_name,
        "params": params,
        "ts": now.timestamp(),
    }

    # 4) 세션에 저장 — dict 그대로 유지
    try:
        ss["__sims_ctx"] = ctx_pack
        ss["__sims_context"] = ctx_pack
        ss["__sims_context_obj"] = ctx_pack
        ss[KEY_SIMS_CTX] = ctx_pack
        ss["__sims_context_text"] = ctx_text

        if analysis_ctx:
            ss["__sims_analysis_ctx"] = analysis_ctx
            ss["__sims_latest_analysis_key"] = analysis_ctx.get("analysis_key")

        ss.pop("__sims_ctx_hash", None)
        ss["__sims_ctx_dirty"] = True
        ss["__chat_has_new"] = True
    except Exception:
        pass

    # 5) 로그
    try:
        preview = ctx_text.replace("\n", " ⏎ ")
        if len(preview) > 220:
            preview = preview[:220] + "…"
        log.info(
            "[SIMS_CTX_UPDATED] action=%s rows=%s cols=%s preview=%s",
            action_name, total_rows, len(cols), preview,
        )
    except Exception:
        pass

    return ctx_pack

# SIMS 결과를 채팅 컨텍스트에 올릴 때, DataFrame을 CSV/XLSX로 다운로드할 수 있게 변환하는 함수
# - DataFrame을 CSV/XLSX로 변환해서, io.BytesIO 객체로 반환한다.
# - 이 함수는 다운로드 직전에 호출하는 것을 권장한다 (화면 렌더링용 df_display에는 원본 데이터를 유지하는 것이 좋음).
def _make_table_downloads(df: pd.DataFrame) -> Tuple[io.BytesIO, io.BytesIO]:
    csv_buf = io.BytesIO()
    xlsx_buf = io.BytesIO()

    # CSV는 Excel에서 바로 열 수 있도록 UTF-8 + BOM으로 인코딩
    csv_text = df.to_csv(index=False)           # 문자열로 한 번 뽑고
    csv_buf.write(csv_text.encode("utf-8-sig")) # BOM 포함 UTF-8로 인코딩
    csv_buf.seek(0)

    with pd.ExcelWriter(xlsx_buf, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="SIMS")
    xlsx_buf.seek(0)
    return csv_buf, xlsx_buf

def _safe_int_for_download(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(str(value).replace(",", "").strip()))
    except Exception:
        return default


def _download_params_from_item(item: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
    """
    다운로드용 조회 파라미터 추출.
    item.params를 우선 사용하고, 없으면 meta.params를 사용한다.
    """
    params = item.get("params")
    if not isinstance(params, dict):
        params = meta.get("params") if isinstance(meta.get("params"), dict) else {}

    out = dict(params or {})

    # 화면용 top=200이 들어있어도 export 함수에서 다시 덮어쓰지만,
    # 혼동 방지를 위해 여기서 제거한다.
    out.pop("top", None)

    # 내부/렌더링 전용 키 제거
    for k in list(out.keys()):
        sk = str(k)
        if sk.startswith("__") or sk.startswith("_push"):
            out.pop(k, None)

    return out


def _expected_analysis_row_count(meta: Dict[str, Any], fallback_rows: int) -> int:
    """
    LLM 전체 집계에서 계산된 전체 건수 추출.
    """
    if not isinstance(meta, dict):
        return fallback_rows

    direct = (
        meta.get("row_count_total_for_analysis")
        or meta.get("analysis_row_count")
        or meta.get("row_count_total")
        or meta.get("row_count")
    )
    n = _safe_int_for_download(direct, 0)
    if n > 0:
        return n

    for key in (
        "in_detail_summary",
        "out_detail_summary",
        "trans_doc_summary",
        "tax_doc_summary",
        "monthly_stock_detail_summary",
    ):

        d = meta.get(key)
        if isinstance(d, dict):
            n = _safe_int_for_download(d.get("row_count_total") or d.get("row_count"), 0)
            if n > 0:
                return n

    return fallback_rows

def _pick_payload_full_df_for_download(
    item: Dict[str, Any],
    meta: Dict[str, Any],
) -> Optional[pd.DataFrame]:
    """
    CSV/Excel 다운로드 전용 전체 DataFrame 선택.

    화면 표시용은 df_display/sims_tables를 쓰더라도,
    다운로드는 payload["df"] 또는 session_state["sims_export_tables"]의 전체 DataFrame을 우선 사용한다.
    """
    try:
        for k in ("df", "df_full", "download_df", "export_df"):
            v = item.get(k)
            if isinstance(v, pd.DataFrame) and not v.empty:
                return v

        for k in ("df_full", "download_df", "export_df"):
            v = meta.get(k)
            if isinstance(v, pd.DataFrame) and not v.empty:
                return v

        table_key = str(meta.get("table_key") or item.get("table_key") or "").strip()
        if table_key:
            export_tables = st.session_state.get("sims_export_tables")
            if isinstance(export_tables, dict):
                v = export_tables.get(table_key)
                if isinstance(v, pd.DataFrame) and not v.empty:
                    return v

            export_tables2 = st.session_state.get("__sims_export_tables_by_key")
            if isinstance(export_tables2, dict):
                v = export_tables2.get(table_key)
                if isinstance(v, pd.DataFrame) and not v.empty:
                    return v

    except Exception:
        pass

    return None

def _get_full_download_df_for_sims_item(
    item: Dict[str, Any],
    meta: Dict[str, Any],
    display_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    CSV/Excel 다운로드용 DataFrame.

    원칙:
    - 화면 표시는 df_display를 사용할 수 있다.
    - 다운로드는 payload["df"] 전체 DataFrame이 있으면 그것을 우선 사용한다.
    - 입고/출고/거래명세서/세금계산서는 기존처럼 필요 시 export 재조회한다.
    """
    if not isinstance(display_df, pd.DataFrame) or display_df.empty:
        return display_df

    action = str(item.get("action") or meta.get("action") or item.get("title") or "").strip()

    export_actions = {
        "입고명세 조회",
        "출고명세 조회",
        "거래명세서 공통 조회",
        "세금계산서 공통 조회",
        "실재고월집계 조회",
        "장부재고월집계 조회",
    }

    is_in_validation_action = "검증" in action and "입고" in action
    is_out_validation_action = "검증" in action and "출고" in action
    is_validation_action = is_in_validation_action or is_out_validation_action
    is_exportable_action = action in export_actions or is_validation_action

    display_rows = int(len(display_df))

    expected_rows = _expected_analysis_row_count(meta, display_rows)

    table_key = str(meta.get("table_key") or item.get("table_key") or "").strip()

    def _stash_export_df_by_table_key(export_df: pd.DataFrame) -> None:
        """
        전체 다운로드 DF를 현재표 후속표 생성에서 바로 찾을 수 있도록
        cache_key뿐 아니라 table_key 기준 저장소에도 보관한다.
        """
        if not table_key:
            return
        if not isinstance(export_df, pd.DataFrame) or export_df.empty:
            return

        try:
            st.session_state.setdefault("sims_export_tables", {})
            st.session_state.setdefault("__sims_export_tables_by_key", {})

            st.session_state["sims_export_tables"][table_key] = export_df
            st.session_state["__sims_export_tables_by_key"][table_key] = export_df

            meta["download_table_key"] = table_key
            meta["download_row_count"] = int(len(export_df))
            meta["display_row_count"] = int(display_rows)
            item["meta"] = meta

            log.info(
                "[chat] stash full export by table_key table_key=%s rows=%s display_rows=%s action=%s",
                table_key,
                len(export_df),
                display_rows,
                action,
            )
        except Exception:
            log.exception("[chat] stash full export by table_key failed")

    full_df = _pick_payload_full_df_for_download(item, meta)


    if isinstance(full_df, pd.DataFrame) and not full_df.empty:
        try:
            full_rows = int(len(full_df))

            try:
                params_for_cap = item.get("params") or meta.get("params") or {}
                query_cap = int(
                    meta.get("fetch_limit")
                    or meta.get("display_top")
                    or meta.get("top")
                    or (params_for_cap.get("_max_top") if isinstance(params_for_cap, dict) else 0)
                    or (params_for_cap.get("display_top") if isinstance(params_for_cap, dict) else 0)
                    or (params_for_cap.get("top") if isinstance(params_for_cap, dict) else 0)
                    or os.getenv("SIMS_PANEL_DISPLAY_MAX_ROWS", "0")
                    or 0
                )
            except Exception:
                query_cap = 0

            hit_query_cap = bool(query_cap > 0 and full_rows >= query_cap)

            # payload 안의 df가 이미 기대 전체건수를 만족하면 재조회하지 않는다.
            # 단, 검증 조회가 조회상한에 딱 걸린 경우에는 더 있을 수 있으므로 export 재조회 여지를 둔다.
            if (not is_exportable_action) or (
                full_rows >= expected_rows and not (is_validation_action and hit_query_cap)
            ) or (
                (not is_validation_action) and expected_rows <= display_rows
            ):
                _chat_log_info_once(
                    f"download_payload_full::{table_key or action}::{full_rows}::{display_rows}::{expected_rows}",
                    "[chat] download uses payload full df action=%s rows=%s display_rows=%s expected_rows=%s",
                    action,
                    full_rows,
                    display_rows,
                    expected_rows,
                )
                return full_df

            _chat_log_info_once(
                f"payload_display_only::{table_key or action}::{full_rows}::{display_rows}::{expected_rows}",
                "[chat] payload df is display-only; prepare export action=%s payload_rows=%s display_rows=%s expected_rows=%s",
                action,
                full_rows,
                display_rows,
                expected_rows,
            )

        except Exception:
            # 판단 실패 시 기존 동작 유지
            return full_df

    if not is_exportable_action:
        return display_df

    # 전체건수가 화면건수와 같으면 굳이 재조회하지 않는다.
    # 검증 화면도 조회상한에 걸리지 않았다면 재조회하지 않는다.
    if expected_rows <= display_rows:
        try:
            params_for_cap = item.get("params") or meta.get("params") or {}
            query_cap = int(
                meta.get("fetch_limit")
                or meta.get("display_top")
                or meta.get("top")
                or (params_for_cap.get("_max_top") if isinstance(params_for_cap, dict) else 0)
                or (params_for_cap.get("display_top") if isinstance(params_for_cap, dict) else 0)
                or (params_for_cap.get("top") if isinstance(params_for_cap, dict) else 0)
                or os.getenv("SIMS_PANEL_DISPLAY_MAX_ROWS", "0")
                or 0
            )
        except Exception:
            query_cap = 0

        if not is_validation_action or not (query_cap > 0 and display_rows >= query_cap):
            return display_df

    params = _download_params_from_item(item, meta)

    # 검증 export는 화면표시 TOP 제한을 제거하고 전체 불일치 기준으로 재조회한다.
    if is_validation_action:
        params.pop("top", None)
        params.pop("display_top", None)
        params.pop("_max_top", None)

    try:
        cache_src = {
            "action": action,
            "params": params,
            "expected_rows": expected_rows,
        }
        cache_key = hashlib.sha256(
            json.dumps(
                cache_src,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()

        st.session_state.setdefault("__sims_export_tables", {})
        cached = st.session_state["__sims_export_tables"].get(cache_key)

        if isinstance(cached, pd.DataFrame) and not cached.empty:
            _stash_export_df_by_table_key(cached)
            return cached

        if action == "입고명세 조회":
            from app.services.rddbc110_service import get_rddbc110_export_df

            export_df = get_rddbc110_export_df(params)

        elif action == "출고명세 조회":
            from app.services.rddbc120_service import get_rddbc120_export_df

            export_df = get_rddbc120_export_df(params)

        elif action == "거래명세서 공통 조회":
            from app.services.rddbc130_service import get_rddbc130_export_df

            export_df = get_rddbc130_export_df(params)

        elif action == "세금계산서 공통 조회":
            from app.services.rddbc140_service import get_rddbc140_export_df

            export_df = get_rddbc140_export_df(params)

        elif action == "실재고월집계 조회":
            from app.services.rddbc210_service import get_rddbc210_export_df

            export_df = get_rddbc210_export_df(params)

        elif action == "장부재고월집계 조회":
            from app.services.rddbc220_service import get_rddbc220_export_df

            export_df = get_rddbc220_export_df(params)

        elif is_in_validation_action:
            from app.services.rddbc110_service import get_rddbc110_export_df

            export_df = get_rddbc110_export_df(params)

        elif is_out_validation_action:
            from app.services.rddbc120_service import get_rddbc120_export_df

            export_df = get_rddbc120_export_df(params)


        else:
            export_df = display_df

        if isinstance(export_df, pd.DataFrame) and not export_df.empty:
            st.session_state["__sims_export_tables"][cache_key] = export_df
            _stash_export_df_by_table_key(export_df)

            log.info(
                "[chat] full download df prepared action=%s display_rows=%s export_rows=%s expected_rows=%s",
                action,
                display_rows,
                len(export_df),
                expected_rows,
            )
            return export_df
        
    except Exception:
        log.exception("[chat] full download df build failed action=%s", action)

    return display_df

# SIMS 결과를 채팅 컨텍스트에 올릴 때, 현재 선택된 채팅방 객체를 session_state에서 찾아 반환하는 함수
# - SIMS 표를 pending 1회 렌더가 아니라 채팅방 history에 고정하기 위해 사용한다.
# - 반환값은 room dict 또는 None (현재 방이 없거나 찾을 수 없는 경우)이다.
# - 이 함수는 SIMS 결과를 채팅방 history에 고정할 때마다 호출하는 것을 권장한다 (예: SIMS 패널에서 LLM 분석 버튼을 눌렀을 때).
# - SIMS 결과가 채팅방 history에 고정되면, 이후 NLQ/백엔드 푸시가 있어도 해당 결과는 유지되고 새 결과는 그 아래에 쌓이게 된다.
def _get_current_room_from_session() -> Optional[Dict[str, Any]]:
    """
    현재 선택된 채팅방 객체를 session_state.chat_rooms에서 찾는다.
    SIMS 표를 pending 1회 렌더가 아니라 채팅방 history에 고정하기 위해 사용한다.
    """
    try:
        rooms = st.session_state.get("chat_rooms") or []
        room_id = st.session_state.get("current_room")
        if not room_id:
            return None

        for room in rooms:
            if isinstance(room, dict) and room.get("id") == room_id:
                return room
    except Exception:
        pass

    return None

def clear_product_candidate_tables_from_chat() -> None:
    """
    제품 후보 선택 취소 시, 기존 후보표를 채팅 history/pending/session table에서 제거한다.

    이유:
    - 취소 메시지는 push되지만, 기존 후보표가 current_room.history에 남아 있으면
      다음 rerun에서 후보표가 다시 렌더되어 취소 메시지가 안 보이는 것처럼 보인다.
    """
    ss = st.session_state
    remove_table_keys: set[str] = set()

    def _is_candidate_item(item: Any) -> bool:
        if not isinstance(item, dict):
            return False

        meta = item.get("meta") or {}
        if bool(meta.get("candidate_table")):
            tk = str(meta.get("table_key") or "").strip()
            if tk:
                remove_table_keys.add(tk)
            return True

        # 제품 후보표 계열 보조 판정
        if meta.get("pending_product_candidates") or meta.get("pending_product_action"):
            tk = str(meta.get("table_key") or "").strip()
            if tk:
                remove_table_keys.add(tk)
            return True

        title = str(item.get("title") or item.get("action") or meta.get("action") or "")
        if "제품 후보" in title:
            tk = str(meta.get("table_key") or "").strip()
            if tk:
                remove_table_keys.add(tk)
            return True

        return False

    def _filter_items(items: Any) -> list:
        if not isinstance(items, list):
            return []
        return [x for x in items if not _is_candidate_item(x)]

    for key in ("__chat_pending_items", "__chat_pending_render", "__chat_history"):
        try:
            ss[key] = _filter_items(ss.get(key) or [])
        except Exception:
            pass

    try:
        room_obj = _get_current_room_from_session()
        if isinstance(room_obj, dict):
            room_obj["history"] = _filter_items(room_obj.get("history") or [])
    except Exception:
        log.exception("[chat] clear candidate tables from room history failed")

    try:
        tables = ss.get("sims_tables")
        if isinstance(tables, dict) and remove_table_keys:
            for tk in remove_table_keys:
                tables.pop(tk, None)
    except Exception:
        log.exception("[chat] clear candidate table data failed")

    try:
        log.info(
            "[chat] cleared product candidate tables removed_keys=%s",
            len(remove_table_keys),
        )
    except Exception:
        pass

# SIMS 분석 rerun에서는 SIMS 표를 light 모드로 표시해서 렌더링 속도를 개선한다.
# - rerun 시, SIMS 표는 채팅방 history에 고정된 상태이므로, 
# SIMS 패널에서는 light 모드로 빠르게 렌더링하고, 
# LLM 분석에서는 전체 컨텍스트를 활용해서 심층 분석하도록 유도한다.
# - light 모드에서는 표 스타일링/포맷팅을 최소화해서 렌더링 속도를 개선한다.
def _queue_sims_llm_analysis(prompt: Optional[str] = None) -> None:
    """
    fallback용 LLM 분석 큐.

    원칙:
    - fragment runner가 있으면 이 함수는 사용하지 않는다.
    - fallback으로 전체 rerun이 필요할 때만 사용한다.
    - 표를 감추는 light mode는 사용하지 않는다.
    """
    st.session_state["__sims_auto_user_input"] = (
        prompt
        or "현재 조회 결과를 핵심 요약, 주요 수치, 주의할 점, 다음 조회 제안 순서로 분석해줘"
    )
    st.session_state["__did_user_input"] = True
    st.rerun()

# LLM 분석 버튼 클릭 시, 전체 앱을 rerun하지 않고, 해당 fragment 영역에서만 LLM 분석을 실행하고 답변을 표시하는 fragment.
# - fragment runner가 준비되지 않은 경우에는 fallback으로 전체 rerun을 한다.
# - 이 fragment는 SIMS 표/요약/조회조건을 건드리지 않고, LLM 분석과 답변 표시만 담당한다.
@st.fragment
def _render_sims_llm_analysis_fragment(key_suffix: str, prompt: str) -> None:
    """
    LLM 분석 버튼 전용 fragment.

    목적:
    - 버튼 클릭 시 전체 앱을 rerun하지 않는다.
    - 이미 표시된 SIMS 표/요약/조회조건을 건드리지 않는다.
    - 이 fragment 영역에서만 LLM 분석을 실행하고 답변을 표시한다.
    """
    runner = st.session_state.get("__sims_llm_analysis_runner")

    if st.button(
        "LLM 분석",
        key=f"sims_llm_analysis_fragment_btn_{key_suffix}",
        use_container_width=True,
    ):
        if not callable(runner):
            st.warning("LLM 분석 실행기가 아직 준비되지 않았습니다. 기존 방식으로 실행합니다.")
            _queue_sims_llm_analysis(prompt)
            return

        with st.container(border=True):
            st.caption("LLM 분석 결과")
            try:
                runner(prompt)
            except Exception:
                log.exception("[chat] fragment LLM analysis failed")
                st.error("LLM 분석 중 오류가 발생했습니다.")


@st.fragment
def _render_sims_result_actions_fragment(
    *,
    key_suffix: str,
    csv_bytes: bytes,
    csv_name: str,
    excel_bytes: bytes,
    xlsx_name: str,
    prompt: str,
) -> None:
    """
    SIMS 결과 하단 액션 영역.

    목표:
    - CSV / EXCEL / LLM 분석 버튼은 3개 컬럼으로 한 줄에 표시
    - LLM 분석 결과는 버튼 아래 전체 폭으로 표시
    - fragment 내부에서만 rerun되어 기존 표 화면은 건드리지 않음
    """
    c1, c2, c3 = st.columns(3)

    with c1:
        st.download_button(
            "CSV 저장",
            data=csv_bytes,
            file_name=csv_name,
            mime="text/csv",
            key=f"sims_csv_{key_suffix}",
            use_container_width=True,
        )

    with c2:
        st.download_button(
            "EXCEL 저장",
            data=excel_bytes,
            file_name=xlsx_name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"sims_xlsx_{key_suffix}",
            use_container_width=True,
        )

    with c3:
        run_llm = st.button(
            "LLM 분석",
            key=f"sims_llm_analysis_fragment_btn_{key_suffix}",
            use_container_width=True,
        )

    # 중요: columns 밖이므로 결과는 전체 폭으로 표시된다.
    # border container를 쓰지 않아 일반 채팅 응답처럼 자연스럽게 표시된다.
    if run_llm:
        runner = st.session_state.get("__sims_llm_analysis_runner")

        if not callable(runner):
            st.warning("LLM 분석 실행기가 아직 준비되지 않았습니다.")
            return

        try:
            runner(prompt)
        except Exception:
            log.exception("[chat] fragment LLM analysis failed")
            st.error("LLM 분석 중 오류가 발생했습니다.")

def _build_sims_detail_analysis_prompt(
    *,
    action_name: str,
    display_rows: int,
    download_rows: int,
    expected_rows: int,
) -> str:
    action_name = str(action_name or "SIMS 조회 결과").strip()


    if "제품수불현황" in action_name or "제품수불" in action_name:
        return f"""
현재 SIMS 조회 결과 [{action_name}]를 전체 조회조건 기준으로 분석해줘.

중요 원칙:
- 화면 표시 {display_rows:,}건은 일부일 수 있고, 전체 분석 기준은 {expected_rows:,}건이다.
- 제품수불현황은 보통 제품 1개 기준의 입고/출고/재고 흐름이다.
- 답변 용어는 반드시 제품수불, 입고수량, 출고수량, 재고수량, 재고증감, 수불금액 중심으로 사용해라.
- '매출액', '매출실적', '판매량', '인기 제품', '부진 제품' 중심으로 표현하지 마라.
- 단일 제품 조회이면 제품 간 순위처럼 말하지 말고, 해당 제품의 기간 내 수불 흐름이라고 설명해라.
- 월별 분석을 제안할 때는 월별 입고수량/출고수량/재고증감을 중심으로 제안해라.
- 다음 조회 제안에서 내부 영문 key 이름이나 내부 프로필명을 절대 쓰지 마라.
- 다음 조회 제안에서 '매출 추이'라고 쓰지 말고 '수불 추이', '입고/출고수량 추이', '재고수량 변화'라고 표현해라.
- 단일 제품 수불현황이면 '제품별 분석'을 제안하지 말고, '월별 입고/출고수량', '거래처별 입출고수량', '재고수량 변화'를 제안해라.
- 재고부족 위험 분석은 제품재고현황 또는 재고부족 분석 화면에서 수행하라고 안내하고, 내부 key 이름은 쓰지 마라.
- 내부 영문 key 이름이나 내부 프로필명은 답변에 노출하지 마라.

분석 형식:
① 핵심 요약
② 주요 수치
③ 주의/확인할 점
④ 다음 조회 제안
"""


    if "실재고월집계" in action_name or "장부재고월집계" in action_name or "월집계" in action_name:
        return f"""
현재 SIMS 조회 결과 [{action_name}]를 전체 조회조건 기준으로 분석해줘.

중요 원칙:
- 화면 표시 {display_rows:,}건은 일부일 수 있고, 전체 분석 기준은 {expected_rows:,}건이다.
- 답변 용어는 반드시 월집계, 입고수량, 출고수량, 입고공급가액, 출고공급가액, 입고세액, 출고세액 중심으로 사용해라.
- '매출 TOP', '제품별 매출', '판매량', '인기 제품', '부진 제품' 중심으로 표현하지 마라.
- 금액은 절대 억/조/천억 단위로 환산하지 마라.
- 답변 전체에서 '약 00억', '약 00조', '천억원', '조 원' 같은 환산 표현을 사용하지 마라.
- 공급가액, 세액, 합계금액은 반드시 원 단위 숫자 그대로 쉼표를 붙여 표기해라.
- 예: 28,927,304,358원은 "28,927,304,358원"으로만 표기해라. "약 28조", "약 289억"처럼 바꾸지 마라.
- "세금 포함 총액"이라는 표현을 쓰지 마라. 세액이면 "세액 합계", 공급가액이면 "공급가액 합계"라고 표현해라.
- 월집계 기본 분석에서는 사용자가 명시적으로 요청하지 않은 제품 TOP, 상위 제품 목록을 임의로 만들지 마라.
- 다음 조회 제안은 월별 입고/출고수량, 제품별 입고수량 TOP, 제품별 출고수량 TOP, 거래처별 입고/출고수량 중심으로 제안해라.
- 제품별 현재 재고수량 TOP은 제품재고현황 조회 후 실행하라고 안내해라.
- 수익성, 원가, 이익률, 품목별 수익성 분석은 제안하지 마라.
- 재고 부족 위험 품목은 제품재고현황 또는 재고부족 분석 화면에서 확인하라고 안내해라.
- 내부 영문 key 이름이나 내부 프로필명은 절대 답변에 노출하지 마라.

분석 형식:
① 핵심 요약
② 주요 수치
③ 주의/확인할 점
④ 다음 조회 제안
"""


    if "제품재고현황" in action_name or "제품재고" in action_name:
        return f"""
현재 SIMS 조회 결과 [{action_name}]를 전체 조회조건 기준으로 분석해줘.

중요 원칙:
- 화면 표시 {display_rows:,}건은 일부일 수 있고, 전체 분석 기준은 {expected_rows:,}건이다.
- 답변 용어는 반드시 제품재고, 이월수량, 입고수량, 출고수량, 재고수량, 재고부족 중심으로 사용해라.
- '매출액', '매출실적', '판매량' 중심으로 표현하지 마라.
- 재고수량 0 이하/0 이상/부족 가능 품목은 재고관리 관점으로 설명해라.
- 내부 영문 key 이름이나 내부 프로필명은 답변에 노출하지 마라.

분석 형식:
① 핵심 요약
② 주요 수치
③ 주의/확인할 점
④ 다음 조회 제안
"""

    if "검증" in action_name:
        return f"""
현재 SIMS 조회 결과 [{action_name}]를 전체 조회조건 기준으로 검증 분석해줘.

중요 원칙:
- 화면 표시 {display_rows:,}건은 일부일 수 있고, 전체 분석 기준은 {expected_rows:,}건이다.
- 이 화면은 일반 입고/출고/세금계산서 분석이 아니라 검증 화면이다.
- 답변 용어는 반드시 검증, 불일치, 누락, 공급가액 차이, 세액 차이, 거래처별 불일치, 제품별 불일치 중심으로 사용해라.
- 계산서금액 TOP, 매출 TOP, 판매량, 인기 제품, 부진 제품 중심으로 표현하지 마라.
- 세금계산서/거래명세서 일반 조회처럼 날짜별 계산서금액만 요약하지 마라.
- 불일치 사유가 명확한 컬럼이 있으면 그 컬럼을 우선 설명해라.
- 차이금액/공급가액차이/세액차이 컬럼이 있으면 차이 합계와 상위 거래처/제품을 우선 설명해라.
- 차이 컬럼이 없으면 현재표의 공급가액, 세액, 합계금액, 상세합계일치, 거래명세서/세금계산서 연결 여부를 기준으로 설명해라.
- 내부 영문 key 이름이나 내부 프로필명은 절대 답변에 노출하지 마라.
- 금액은 원 단위 숫자 그대로 쉼표를 붙여 표기해라.

분석 형식:
① 핵심 요약
② 주요 수치
③ 주의/확인할 점
④ 다음 조회 제안

다음 조회 제안은 아래 유형 중심으로 작성해라:
- 현재표 거래처별 불일치 분석
- 현재표 제품별 불일치 분석
- 현재표 월별 불일치 분석
- 특정 거래처/제품의 불일치 상세 확인
"""


    if "세금계산서" in action_name:
        return f"""
현재 SIMS 조회 결과 [{action_name}]를 전체 조회조건 기준으로 분석해줘.

중요 원칙:
- 화면 표시 {display_rows:,}건은 일부일 수 있고, 전체 분석 기준은 {expected_rows:,}건이다.
- 답변 용어는 반드시 세금계산서, 계산서금액, 거래처, 매입/매출, 공급가액, 세액, 합계금액 중심으로 사용해라.
- 내부 영문 key 이름이나 내부 프로필명은 절대 답변에 노출하지 마라.
- 세금계산서 공통에서는 표준 제품명/제품코드 컬럼이 없는 경우 제품별/품목별/판매량/인기 제품/부진 제품 분석을 제안하지 마라.
- 제품1, 제품2, 수량1, 수량2 같은 반복 상세 필드는 표준 제품/수량 컬럼으로 보지 마라.
- 수량 합계가 0이거나 수량 컬럼이 반복 상세 필드이면 수량 기반 분석을 하지 마라.
- 매입 데이터가 없거나 극히 적으면 “매입 자료가 거의 없으므로 매입 분석은 제한적”이라고만 설명해라.
- 다음 조회 제안은 세금계산서 공통 조회, 현재표 거래처별 계산서금액, 세금계산서구분별 계산서금액, 일자별 계산서금액 중심으로 제안해라.
- 제품별/품목별/판매량 분석 제안은 하지 마라.
- 다음 조회 제안에서 '품목', '제품', '판매량', '인기/부진 품목'이라는 표현을 사용하지 마라.
- '거래처별 매출'이라고 쓰지 말고 '거래처별 계산서금액' 또는 '거래처별 매출 계산서금액'이라고 표현해라.

분석 형식:
① 핵심 요약
② 주요 수치
③ 주의/확인할 점
④ 다음 조회 제안
"""


    if "거래명세서" in action_name:
        return f"""
현재 SIMS 조회 결과 [{action_name}]를 전체 조회조건 기준으로 분석해줘.

중요 원칙:
- 화면 표시 {display_rows:,}건은 일부일 수 있고, 전체 분석 기준은 {expected_rows:,}건이다.
- 답변 용어는 반드시 거래명세서, 거래금액, 거래처, 매입분/매출분, 공급가액, 세액, 합계금액 중심으로 사용해라.
- 내부 영문 key 이름이나 내부 프로필명은 절대 답변에 노출하지 마라.
- 제품명/제품코드 컬럼이 없는 거래명세서 공통에서는 제품별/품목별/판매량/인기 제품/부진 제품 분석을 제안하지 마라.
- 다음 조회 제안은 거래명세서구분별 거래금액, 거래처별 거래금액, 일자별 거래금액 중심으로 제안해라.

분석 형식:
① 핵심 요약
② 주요 수치
③ 주의/확인할 점
④ 다음 조회 제안
"""


# "현재표" 후속분석은, 전체 기준은 현재표 후속분석 결과 전체 건수이지 화면 표시 건수가 아니다.
# - 화면 표시 건수는 일부일 수 있다.
    if action_name.startswith("현재표"):
        return f"""
현재표 후속분석 결과 [{action_name}]를 간단히 분석해줘.

중요 원칙:
- 전체 기준은 현재표 후속분석 결과 전체 {download_rows:,}건이다.
- 화면 표시 {display_rows:,}건은 일부일 수 있다.
- 표 전체를 나열하지 말고 상위 5~10개와 특징만 요약해라.
- 내부 key 이름은 노출하지 마라.
- 답변은 1,200자 이내로 끝내라.
- 불필요하게 긴 다음 조회 제안은 하지 마라.

분석 형식:
① 핵심 요약
② 상위 항목 특징
③ 주의/확인할 점
④ 다음에 볼 만한 현재표 후속질문 2개
"""

# SIMS 결과가 대형표인 경우, 처음에는 CSV/XLSX bytes를 만들지 않고 [다운로드 준비] 버튼만 표시하는 lazy 버전 액션 영역.
# - 작은 표: 기존처럼 CSV/EXCEL/LLM 버튼 즉시 표시
# - 큰 표: 처음에는 [다운로드 준비] + [LLM 분석]만 표시
# - [다운로드 준비]를 누른 뒤에만 CSV/XLSX bytes 생성
def _get_sims_download_lazy_threshold_rows() -> int:
    """
    대형표 다운로드 lazy 기준 행 수.
    기본 5,000건 이상이면 CSV/XLSX bytes를 즉시 만들지 않는다.

    .env 예:
    SIMS_DOWNLOAD_LAZY_ROW_THRESHOLD=5000
    """
    try:
        return max(0, int(os.getenv("SIMS_DOWNLOAD_LAZY_ROW_THRESHOLD", "5000")))
    except Exception:
        return 5000

# SIMS 결과가 대형표인 경우, 처음에는 CSV/XLSX bytes를 만들지 않고 [다운로드 준비] 버튼만 표시하는 lazy 버전 액션 영역.
# - 작은 표: 기존처럼 CSV/EXCEL/LLM 버튼 즉시 표시
# - 큰 표: 처음에는 [다운로드 준비] + [LLM 분석]만 표시
# - [다운로드 준비]를 누른 뒤에만 CSV/XLSX bytes 생성
def _render_sims_result_actions_lazy(
    *,
    key_suffix: str,
    download_df: pd.DataFrame,
    csv_name: str,
    xlsx_name: str,
    prompt: str,
    expected_rows: int | None = None,
    display_rows: int | None = None,
) -> None:    

    """
    채팅 SIMS 결과 하단 액션 영역 lazy 버전.

    - 작은 표: 기존처럼 CSV/EXCEL/LLM 버튼 즉시 표시
    - 큰 표: 처음에는 [다운로드 준비] + [LLM 분석]만 표시
    - [다운로드 준비]를 누른 뒤에만 CSV/XLSX bytes 생성
    """
    if not isinstance(download_df, pd.DataFrame) or download_df.empty:
        c1, c2, c3 = st.columns(3)
        with c1:
            st.caption("다운로드할 표가 없습니다.")
        with c3:
            run_llm = st.button(
                "LLM 분석",
                key=f"sims_llm_analysis_btn_lazy_empty_{key_suffix}",
                use_container_width=True,
            )

        if run_llm:
            runner = st.session_state.get("__sims_llm_analysis_runner")
            log.info("[chat] LLM analysis clicked runner=%s key=%s", callable(runner), key_suffix)

            if not callable(runner):
                st.warning("LLM 분석 실행기가 아직 준비되지 않았습니다.")
                return

            try:
                runner(prompt)
            except Exception:
                log.exception("[chat] LLM analysis failed")
                st.error("LLM 분석 중 오류가 발생했습니다.")

        return

    threshold_rows = _get_sims_download_lazy_threshold_rows()
    row_count = int(len(download_df))
    col_count = int(len(download_df.columns))

    try:
        expected_rows_int = int(expected_rows or row_count)
    except Exception:
        expected_rows_int = row_count

    try:
        display_rows_int = int(display_rows or row_count)
    except Exception:
        display_rows_int = row_count

    # 중요:
    # download_df가 아직 화면 표시용 200건이어도,
    # expected_rows가 9,615건이면 대형표로 판단해야 한다.
    lazy_basis_rows = max(row_count, expected_rows_int)

    is_large_download = threshold_rows > 0 and lazy_basis_rows >= threshold_rows

    ready_key = f"__sims_download_ready::{key_suffix}"
    bytes_key = f"__sims_download_bytes::{key_suffix}"

    ss = st.session_state
    is_ready = bool(ss.get(ready_key))

    if is_large_download and not is_ready:
        if expected_rows_int > row_count:
            st.caption(
                f"대형표 다운로드: 전체 예상 {expected_rows_int:,}건 × {col_count:,}열입니다. "
                f"현재 화면 표시는 {display_rows_int:,}건입니다. "
                "속도를 위해 전체 export와 CSV/EXCEL 파일은 [다운로드 준비]를 누른 뒤 생성합니다."
            )
        else:
            st.caption(
                f"대형표 다운로드: {row_count:,}건 × {col_count:,}열입니다. "
                "속도를 위해 CSV/EXCEL 파일은 [다운로드 준비]를 누른 뒤 생성합니다."
            )

        c1, c2, c3 = st.columns(3)

        with c1:
            if st.button(
                "다운로드 준비",
                key=f"sims_prepare_download_{key_suffix}",
                use_container_width=True,
            ):
                ss[ready_key] = True
                st.rerun()

        with c2:
            st.caption("CSV/EXCEL 버튼은 준비 후 표시됩니다.")

        with c3:
            run_llm = st.button(
                "LLM 분석",
                key=f"sims_llm_analysis_btn_lazy_{key_suffix}",
                use_container_width=True,
            )

        if run_llm:
            runner = ss.get("__sims_llm_analysis_runner")
            log.info("[chat] LLM analysis clicked runner=%s key=%s", callable(runner), key_suffix)

            if not callable(runner):
                st.warning("LLM 분석 실행기가 아직 준비되지 않았습니다.")
                return

            try:
                runner(prompt)
            except Exception:
                log.exception("[chat] LLM analysis failed")
                st.error("LLM 분석 중 오류가 발생했습니다.")

        return

    cached = ss.get(bytes_key)
    col_sig = tuple(str(c) for c in download_df.columns)

    if (
        isinstance(cached, dict)
        and cached.get("rows") == row_count
        and cached.get("cols") == col_count
        and tuple(cached.get("col_sig") or []) == col_sig
        and isinstance(cached.get("csv_bytes"), (bytes, bytearray))
        and isinstance(cached.get("excel_bytes"), (bytes, bytearray))
    ):
        csv_bytes = cached["csv_bytes"]
        excel_bytes = cached["excel_bytes"]
        log.debug(
            "[chat] download bytes cache hit key=%s rows=%s cols=%s",
            key_suffix,
            row_count,
            col_count,
        )
    else:
        t0 = time.perf_counter()

        excel_download_df = _sanitize_dataframe_for_excel(download_df)

        csv_bytes = excel_download_df.to_csv(index=False).encode("utf-8-sig")

        bio = io.BytesIO()
        with pd.ExcelWriter(bio, engine="openpyxl") as writer:
            excel_download_df.to_excel(writer, index=False, sheet_name="SIMS")
        excel_bytes = bio.getvalue()

        ss[bytes_key] = {
            "rows": row_count,
            "cols": col_count,
            "col_sig": col_sig,
            "csv_bytes": csv_bytes,
            "excel_bytes": excel_bytes,
        }

        log.info(
            "[chat] download bytes prepared lazy=%s rows=%s cols=%s %.3fs",
            is_large_download,
            row_count,
            col_count,
            time.perf_counter() - t0,
        )

    _render_sims_result_actions_fragment(
        key_suffix=key_suffix,
        csv_bytes=csv_bytes,
        csv_name=csv_name,
        excel_bytes=excel_bytes,
        xlsx_name=xlsx_name,
        prompt=prompt,
    )


# SIMS 결과 카드 하단 액션 버튼 직접 렌더링.
# - fragment 렌더 이슈를 피하기 위한 안정판.
def _render_sims_result_actions_plain(
    *,
    key_suffix: str,
    csv_bytes: bytes,
    csv_name: str,
    excel_bytes: bytes,
    xlsx_name: str,
    prompt: str,
) -> None:
    """
    SIMS 결과 하단 액션 버튼 직접 렌더링.
    fragment 렌더 이슈를 피하기 위한 안정판.
    """
    _chat_log_info_once(
        f"render_action_buttons::{key_suffix}",
        "[chat] render action buttons key=%s csv=%s xlsx=%s",
        key_suffix,
        csv_name,
        xlsx_name,
    )

    c1, c2, c3 = st.columns(3)

    with c1:
        st.download_button(
            "CSV 저장",
            data=csv_bytes,
            file_name=csv_name,
            mime="text/csv",
            key=f"sims_csv_{key_suffix}",
            use_container_width=True,
        )

    with c2:
        st.download_button(
            "EXCEL 저장",
            data=excel_bytes,
            file_name=xlsx_name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"sims_xlsx_{key_suffix}",
            use_container_width=True,
        )

    with c3:
        run_llm = st.button(
            "LLM 분석",
            key=f"sims_llm_analysis_btn_{key_suffix}",
            use_container_width=True,
        )

    if run_llm:
        runner = st.session_state.get("__sims_llm_analysis_runner")
        log.info("[chat] LLM analysis clicked runner=%s key=%s", callable(runner), key_suffix)

        if not callable(runner):
            st.warning("LLM 분석 실행기가 아직 준비되지 않았습니다.")
            return

        try:
            runner(prompt)
        except Exception:
            log.exception("[chat] LLM analysis failed")
            st.error("LLM 분석 중 오류가 발생했습니다.")

# LLM 분석 버튼 클릭 시, fragment runner가 준비되지 않은 경우에 대비한 fallback 함수.
# - 이 함수는 전체 앱을 rerun해서 LLM 분석을 실행하는 기존 방식이다.
def _render_sims_llm_analysis_button(key_suffix: str, prompt: Optional[str] = None) -> None:
    """
    SIMS 결과 카드 하단에 LLM 분석 버튼을 표시한다.

    Streamlit fragment를 사용해서 버튼 클릭 시 전체 화면을 rerun하지 않고,
    버튼/분석 결과 영역만 갱신한다.
    """
    _render_sims_llm_analysis_fragment(
        key_suffix=key_suffix,
        prompt=(
            prompt
            or "현재 조회 결과를 핵심 요약, 주요 수치, 주의할 점, 다음 조회 제안 순서로 분석해줘"
        ),
    )

# ──────────────────────────────────────────────────────────────────────────────
# 퍼블릭 API
# ──────────────────────────────────────────────────────────────────────────────
def wire_chat_context(room: Optional[Dict[str, Any]] = None) -> None:
    """
    채팅 화면에서 사용할 공용 상태를 초기화/연결.
    여러 번 호출해도 안전(idempotent).
    """
    ss = st.session_state
    ss.setdefault("__chat_inbox", [])        # 외부에서 밀어넣는 메시지 큐
    ss.setdefault("__chat_history", [])      # 렌더된 메시지들
    # ✅ "이번 런에서 아래(pending_area)에서 1회 렌더"할 항목 큐
    ss.setdefault("__chat_pending_items", []) 
    ss.setdefault("__sims_last_push_sig", None)
    ss.setdefault("__sims_selected_action", None)
    ss.setdefault("__sims_push_count", 0)
    # ❌ room dict에 inbox/history(DF 포함)를 꽂으면 save_chat_rooms JSON dump에서 터짐
    #    (room은 “저장 객체”, session_state는 “런타임 객체”로 분리 유지)
    # if room is not None:
    #     room.setdefault("inbox", ss["__chat_inbox"])
    #     room.setdefault("history", ss["__chat_history"])

# 내부 유틸리티 함수 (사용자 메시지/LLM 답변과 달리,
# 채팅 history에 저장된 SIMS 표 아이템을 식별하고 관리하는 함수)
def _is_sims_table_history_item(item: Any) -> bool:
    """
    채팅 history에 저장된 SIMS 표 아이템인지 판단한다.
    일반 LLM 답변/사용자 메시지는 제거하지 않는다.
    """
    if not isinstance(item, dict):
        return False

    meta = item.get("meta") or {}

    return (
        item.get("type") == "table"
        or meta.get("kind") == "table"
        or bool(meta.get("table_key"))
    )


def _coerce_sims_display_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{2}:\d{2}:\d{2}", text):
        return text
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%H:%M:%S")
    except Exception:
        pass
    m = re.search(r"(\d{2}:\d{2}:\d{2})", text)
    return m.group(1) if m else text[:8]


def _coerce_sims_result_datetime(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass

    m = re.search(
        r"(\d{4})[-/.]?(\d{2})[-/.]?(\d{2})[ T]+(\d{2}):(\d{2}):(\d{2})",
        text,
    )
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}:{m.group(6)}"

    return ""


def _sims_result_datetime_text(item: Dict[str, Any], meta: Dict[str, Any]) -> str:
    for value in (
        meta.get("created_at"),
        item.get("time"),
        meta.get("time"),
        meta.get("ts"),
        meta.get("timestamp"),
    ):
        text = _coerce_sims_result_datetime(value)
        if text:
            return text
    return ""

# SIMS 표 유지 정책
# - reference: 사용자가 계속 보면서 판단해야 하는 기준표
# - drilldown : 기준표를 보면서 추가 확인하는 보조조회표
REFERENCE_TABLE_ACTIONS = {
    "품목별 매출 추세 분석",
    "품목별 매출 추세 요약표",
    "품목별 매출 예상",
    "품목별 재고부족현황",
    "제품재고현황 조회",
    "제품재고장",
}

DRILLDOWN_TABLE_KEEP_DEFAULT = 5


def _get_drilldown_table_keep_limit() -> int:
    try:
        return max(1, int(os.getenv("SIMS_DRILLDOWN_TABLE_KEEP", str(DRILLDOWN_TABLE_KEEP_DEFAULT))))
    except Exception:
        return DRILLDOWN_TABLE_KEEP_DEFAULT


def _sims_table_role_from_action(action_name: Any, meta: Optional[Dict[str, Any]] = None) -> str:
    """
    SIMS 표를 기준표(reference) / 보조조회표(drilldown)로 구분한다.

    reference:
      - 재고부족/매출예상/매출추세/제품재고장처럼 계속 보면서 판단해야 하는 표

    drilldown:
      - 입고/출고/수불/제품/거래처처럼 기준표를 보며 추가 확인하는 표
    """
    meta = meta or {}

    role = str(meta.get("table_role") or "").strip().lower()
    if role in {"reference", "drilldown"}:
        return role

    action = str(action_name or meta.get("action") or meta.get("title") or "").strip()
    analysis_type = str(meta.get("analysis_type") or "").strip()
    summary_type = str(meta.get("summary_type") or "").strip()

    if (
        action in REFERENCE_TABLE_ACTIONS
        or "제품재고장" in action
        or analysis_type in {"sales_trend", "sales_forecast", "stock_shortage"}
        or summary_type in {"product_summary", "product_forecast", "product_stock_shortage"}
    ):
        return "reference"

    return "drilldown"


def _sims_table_role_from_item(item: Dict[str, Any]) -> str:
    meta = item.get("meta") or {}
    action_name = (
        item.get("action")
        or item.get("title")
        or meta.get("action")
        or meta.get("title")
        or ""
    )
    return _sims_table_role_from_action(action_name, meta)


def _sims_table_key_from_item(item: Dict[str, Any]) -> str:
    meta = item.get("meta") or {}
    return str(meta.get("table_key") or "").strip()

def _old_sims_table_force_key(item: Dict[str, Any], meta: Dict[str, Any], uid: str = "") -> str:
    raw = (
        str(meta.get("table_key") or "")
        or str(item.get("id") or "")
        or str(item.get("seq") or "")
        or str(uid or "")
        or "unknown"
    )
    safe = re.sub(r"[^0-9A-Za-z가-힣_\-:]+", "_", raw)
    return f"__sims_force_render_old_table::{safe}"


def _is_current_sims_table_item(item: Dict[str, Any], meta: Dict[str, Any]) -> bool:
    """
    현재 새로 조회한 SIMS 표인지 판정.
    현재표만 무거운 full dataframe 렌더를 수행하고,
    이전 표는 lightweight placeholder로 둔다.
    """
    if not isinstance(item, dict):
        return False

    # 후보표는 항상 즉시 보여야 한다.
    if bool(meta.get("candidate_table")):
        return True

    item_id = str(item.get("id") or "").strip()
    item_table_key = str(meta.get("table_key") or item.get("table_key") or "").strip()

    last_id = str(st.session_state.get("__sims_last_msg_id") or "").strip()
    last_table_key = str(st.session_state.get("__sims_last_table_key") or "").strip()

    if last_id and item_id and item_id == last_id:
        return True

    if last_table_key and item_table_key and item_table_key == last_table_key:
        return True

    return False


def _should_full_render_sims_table(item: Dict[str, Any], meta: Dict[str, Any], uid: str = "") -> bool:
    """
    full dataframe render 여부.
    - 현재 새 조회표: full render
    - 기준표(reference): 유지
    - 사용자가 '이전 표 다시 표시'를 누른 표: full render
    - 그 외 이전 drilldown 표: placeholder
    """
    if _is_current_sims_table_item(item, meta):
        return True

    try:
        role = _sims_table_role_from_item(item)
        render_old_reference = str(
            os.getenv("SIMS_RENDER_OLD_REFERENCE_TABLES", "0")
        ).strip().lower() in {"1", "true", "yes", "y", "on"}

        # 이전 기준표도 기본은 접는다.
        # 현재표만 펼치고, 이전 표는 사용자가 [이전 표 다시 표시]를 눌렀을 때만 렌더한다.
        if role == "reference" and render_old_reference:
            return True
    except Exception:
        pass

    force_key = _old_sims_table_force_key(item, meta, uid)
    if bool(st.session_state.get(force_key)):
        return True

    return False


def _render_old_sims_table_placeholder(
    item: Dict[str, Any],
    meta: Dict[str, Any],
    data: pd.DataFrame,
    uid: str,
) -> None:
    """
    이전 SIMS 표는 rerun 때마다 무거운 dataframe을 다시 그리지 않고
    요약만 표시한다.
    """
    action_name = str(item.get("action") or meta.get("action") or item.get("title") or "SIMS 결과").strip()

    try:
        row_count = int(
            meta.get("row_count_total")
            or meta.get("download_row_count")
            or meta.get("row_count")
            or len(data)
            or 0
        )
    except Exception:
        row_count = len(data) if isinstance(data, pd.DataFrame) else 0

    try:
        display_rows = int(meta.get("display_row_count") or len(data) or 0)
    except Exception:
        display_rows = len(data) if isinstance(data, pd.DataFrame) else 0

    try:
        col_count = int(meta.get("column_count") or (len(data.columns) if isinstance(data, pd.DataFrame) else 0))
    except Exception:
        col_count = 0

    st.caption(
        f"이전 조회표: {action_name} / 전체 {row_count:,}건"
        + (f" / 표시 {display_rows:,}건" if display_rows and display_rows != row_count else "")
        + (f" / {col_count:,}열" if col_count else "")
        + " — 속도를 위해 표 렌더링을 생략했습니다."
    )

    force_key = _old_sims_table_force_key(item, meta, uid)

    if st.button(
        "이전 표 다시 표시",
        key=f"show_old_sims_table_{uid}",
        use_container_width=False,
    ):
        st.session_state[force_key] = True
        st.rerun()


# SIMS 표 history 유지 정책 함수
# - 기준표는 계속 유지한다 (사용자가 보면서 판단해야 하므로)
# - 보조조회표는 최근 N개만 유지한다 (화면에 너무 많은 표가 남아 있으면 렌더링이 느려지므로)
# - 일반 LLM 답변/사용자 메시지는 유지한다 (표가 아니므로 렌더링 부담이 적음)
def _prune_old_sims_table_history(
    new_table_key: Optional[str] = None,
    new_item: Optional[Dict[str, Any]] = None,
) -> None:
    """
    SIMS 표 history 유지 정책.

    기존 정책:
      - 최신 SIMS 표 1개만 유지

    변경 정책:
      - 기준표(reference)는 유지
      - 보조조회표(drilldown)는 최근 N개만 유지
      - 일반 LLM 답변/사용자 메시지는 유지
      - session_state["sims_tables"]에서는 실제로 화면에 남는 table_key만 유지

    이유:
      - 사용자는 재고부족/재고장 같은 기준표를 보면서
        제품수불/제품코드/거래처코드를 이어서 조회해야 한다.
      - 하지만 모든 보조표를 무제한 유지하면 rerun 렌더링이 느려진다.
    """
    ss = st.session_state
    keep_limit = _get_drilldown_table_keep_limit()

    # 새로 추가될 표가 보조표라면, 기존 보조표는 keep_limit - 1개만 남겨서
    # append 후 전체 보조표가 keep_limit개가 되게 한다.
    new_is_drilldown = False
    try:
        if isinstance(new_item, dict) and _is_sims_table_history_item(new_item):
            new_is_drilldown = _sims_table_role_from_item(new_item) != "reference"
    except Exception:
        new_is_drilldown = False

    existing_drilldown_keep = max(keep_limit - 1, 0) if new_is_drilldown else keep_limit

    keep_table_keys: set[str] = set()
    if new_table_key:
        keep_table_keys.add(str(new_table_key))

    def _prune_list(items: list[Any]) -> list[Any]:
        table_items: list[Dict[str, Any]] = []
        normal_items: list[Any] = []

        for x in items:
            if not _is_sims_table_history_item(x):
                normal_items.append(x)
                continue

            if isinstance(x, dict):
                table_items.append(x)

        # 기준표는 전부 유지
        reference_ids: set[int] = set()
        drilldown_items: list[Dict[str, Any]] = []

        for x in table_items:
            role = _sims_table_role_from_item(x)
            if role == "reference":
                reference_ids.add(id(x))
                tk = _sims_table_key_from_item(x)
                if tk:
                    keep_table_keys.add(tk)
            else:
                drilldown_items.append(x)

        # 보조표는 최근 N개 유지
        keep_drilldown = drilldown_items[-existing_drilldown_keep:] if existing_drilldown_keep > 0 else []
        keep_drilldown_ids = {id(x) for x in keep_drilldown}

        for x in keep_drilldown:
            tk = _sims_table_key_from_item(x)
            if tk:
                keep_table_keys.add(tk)

        out: list[Any] = []
        for x in items:
            if not _is_sims_table_history_item(x):
                out.append(x)
                continue

            if isinstance(x, dict):
                if id(x) in reference_ids or id(x) in keep_drilldown_ids:
                    out.append(x)

        return out

    # 1) session 전역 chat history 정리
    try:
        old_history = list(ss.get("__chat_history") or [])
        ss["__chat_history"] = _prune_list(old_history)
    except Exception:
        log.exception("[chat] prune __chat_history sims tables failed")

    # 2) 현재 room history 정리
    try:
        room_obj = _get_current_room_from_session()
        if isinstance(room_obj, dict):
            old_room_history = list(room_obj.get("history") or [])
            room_obj["history"] = _prune_list(old_room_history)
    except Exception:
        log.exception("[chat] prune current_room.history sims tables failed")

    # 3) session_state에 보관된 DataFrame 정리
    #    화면 history에 남아 있는 table_key + 새 table_key만 유지한다.
    try:
        tables = ss.get("sims_tables")
        if isinstance(tables, dict):
            ss["sims_tables"] = {
                k: v for k, v in tables.items()
                if str(k) in keep_table_keys
            }
    except Exception:
        log.exception("[chat] prune sims_tables by table policy failed")

    try:
        log.debug(
            "[chat] prune sims tables policy reference+drilldown keep_limit=%s keep_keys=%s",
            keep_limit,
            len(keep_table_keys),
        )
    except Exception:
        pass

# 채팅 컨텍스트가 필요한 곳에서 이 함수를 호출해서 세션 상태를 보장한다.
# 예: NLQ/백엔드 푸시, 채팅 패널 렌더 루프 등.
def drain_inbox_to_chat(room: Optional[Dict[str, Any]] = None) -> None:
    """
    인박스에 쌓인 항목(테이블/텍스트/오브젝트)을 채팅 히스토리로 이동.

    ✅ 여기서는 "즉시 렌더"하지 않는다.
    - 이유: 이 함수가 채팅 렌더 루프보다 먼저 호출되면(예: NLQ/백엔드 푸시),
      결과가 화면 상단으로 "튀어" 보일 수 있음.
    - 채팅 UI는 메인에서 render_pending_chat_items(...)로 일관된 순서로 출력한다.

    또한 채팅룸을 JSON으로 저장할 수 있도록, 히스토리에는 DataFrame을 직접 넣지 않고
    (records/columns 형태로) 안전하게 보관한다.
    """
    wire_chat_context(room)  # 안전 호출

    ss = st.session_state
    ss.setdefault("__chat_inbox", [])
    ss.setdefault("__chat_history", [])
    ss.setdefault("__chat_pending_items", [])

    inbox = list(ss["__chat_inbox"])
    if not inbox:
        return

    # DataFrame이 JSON 직렬화 문제를 일으키지 않도록, 저장용 아이템에서 DataFrame 제거/변환
    # - 이번 런에 바로 보여줄 pending 아이템은 원본 그대로 둔다 (DataFrame 포함 가능).
    # - 장기 보관용 히스토리 아이템은 DataFrame 제거/변환해서 저장한다.
    # - 이 함수는 채팅방 history에 아이템을 고정하기 전에 호출하는 것을 권장한다 (예: SIMS 패널에서 LLM 분석 버튼을 눌렀을 때).
    # - SIMS 결과가 채팅방 history에 고정되면, 이후 NLQ/백엔드 푸시가 있어도 해당 결과는 유지되고 새 결과는 그 아래에 쌓이게 된다.
    def _history_safe_item(item: Dict[str, Any]) -> Dict[str, Any]:
        """
        DataFrame/비직렬화 객체를 제거한 저장용 아이템.

        중요:
        - 표는 current_room["history"]에 고정되어야 후속 LLM 답변 후에도 사라지지 않는다.
        - 큰 DataFrame 원본은 room JSON에 저장하지 않고, meta.table_key만 저장한다.
        """
        safe = dict(item)

        if safe.get("type") == "table":
            meta = dict(safe.get("meta") or {})

            # table_key가 있으면 session_state["sims_tables"]에서 다시 그릴 수 있다.
            if meta.get("table_key"):
                meta["kind"] = "table"
                meta.setdefault("table_role", _sims_table_role_from_action(
                    safe.get("action") or safe.get("title") or meta.get("action"),
                    meta,
                ))

                # JSON 저장/화면 재렌더에 불필요한 대용량 데이터 제거
                meta.pop("df", None)
                meta.pop("columns", None)

                safe["meta"] = meta
                safe.setdefault("content", safe.get("title") or f"📊 {meta.get('action') or 'SIMS 결과'}")

                for k in ("data", "df", "df_display", "records", "columns"):
                    safe.pop(k, None)

                return safe

                # text/object SIMS payload는 일반 채팅 history 렌더에서 content를 사용한다.
                # content가 없으면 push는 성공해도 화면에는 빈 assistant bubble처럼 보일 수 있다.
                if safe.get("type") in {"text", "object"}:
                    text_content = str(
                        safe.get("content")
                        or safe.get("message")
                        or safe.get("data")
                        or (safe.get("meta") or {}).get("summary_md")
                        or safe.get("title")
                        or ""
                    ).strip()

                    if text_content:
                        safe["content"] = text_content
                        safe.setdefault("role", "assistant")


            # table_key가 없는 예전 payload는 최소한 records/columns로 저장
            df = safe.get("data")
            if not isinstance(df, pd.DataFrame):
                df = safe.get("df_display")
            if not isinstance(df, pd.DataFrame):
                df = safe.get("df")

            if isinstance(df, pd.DataFrame):
                safe["columns"] = list(df.columns)
                safe["records"] = df.to_dict(orient="records")

            for k in ("data", "df", "df_display"):
                safe.pop(k, None)

        return safe
    
    moved = 0
    while ss["__chat_inbox"]:

        item = ss["__chat_inbox"].pop(0)
        _ensure_table_json_safe(item)

        # (A) 이번 런에서 바로 보여줄 pending
        ss["__chat_pending_items"].append(item)

        # (B) 장기 보관/재렌더용 히스토리
        safe_item = _history_safe_item(item)

        # SIMS 표는 최신 1개만 유지한다.
        # 이전 SIMS 표까지 매 rerun마다 렌더링하면 974행 x 97열 같은 큰 표에서 매우 느려진다.
        try:
            if _is_sims_table_history_item(safe_item):
                _prune_old_sims_table_history(
                    new_table_key=(safe_item.get("meta") or {}).get("table_key"),
                    new_item=safe_item,
                )
        except Exception:
            log.exception("[chat] prune old sims table before append failed")

        ss["__chat_history"].append(safe_item)


        # (C) 현재 채팅방 history에도 고정
        #     이것이 없으면 후속 LLM 답변 rerun 때 SIMS 표가 사라진다.
        try:
            room_obj = room or _get_current_room_from_session()
            if isinstance(room_obj, dict):
                room_obj.setdefault("history", [])

                safe_id = safe_item.get("id")
                exists = False
                if safe_id:
                    exists = any(
                        isinstance(x, dict) and x.get("id") == safe_id
                        for x in room_obj.get("history", [])
                    )

                if not exists:
                    room_obj["history"].append(safe_item)
        except Exception:
            log.exception("[chat] persist sims item into current_room.history failed")

        moved += 1

    if moved:
        log.info(
            "[chat.drain] %s moved=%d history_count=%s pending_count=%s",
            _chat_runtime_log_kv(room),
            moved,
            len(ss.get("__chat_history") or []),
            len(ss.get("__chat_pending_items") or []),
        )

    # (C) room/state 동기화: 이번 런에 drained 된 결과가 즉시 반영되도록
    #     마지막에 1회 호출한다.
    wire_chat_context(room)

def render_pending_chat_items(area, room: Optional[Dict[str, Any]] = None) -> None:
    """이번 런에서 생성된(혹은 남아있는) pending 채팅 아이템을 지정 영역에 1회 렌더."""
    wire_chat_context(room)
    ss = st.session_state

    pending = list((ss.get("__chat_pending_items") or ss.get("__chat_pending_render") or []))

    # 이미 현재 rerun에서 history 영역에 렌더된 메시지는 pending에서 한 번 더 그리지 않는다.
    # SIMS 표를 current_room.history에 고정하면, 첫 rerun에서 history + pending 이중 렌더가 생길 수 있다.
    try:
        rendered_ids = set(ss.get("__chat_rendered_ids_this_run") or [])
        if rendered_ids:
            pending = [
                item for item in pending
                if not item.get("id") or item.get("id") not in rendered_ids
            ]
    except Exception:
        pass

    if not pending:
        ss["__chat_pending_items"] = []
        ss["__chat_pending_render"] = []
        return

    # pending은 "한 번만" 보여주고 비운다.
    ss["__chat_pending_items"] = []
    ss["__chat_pending_render"] = []

    log.info("[chat.render.pending] %s pending=%d", _chat_runtime_log_kv(room), len(pending))

    with area:
        for item in pending:
            try:
                _render_chat_item(item, target=area)
            except Exception:
                log.exception("[chat] render pending item failed")

def wssz(result: Any, action: Optional[str] = None) -> None:
    """
    SIMS 최종 결과를 채팅 히스토리로 1회 푸시하고,
    LLM용 SIMS 컨텍스트(JSON)도 동시에 갱신한다.
    """
    wire_chat_context()
    ss = st.session_state

    # 새 SIMS 조회 결과가 들어올 때는 화면 표를 정상 전체 렌더링한다.
    # LLM 분석 버튼 때문에 light 모드였던 상태를 새 조회 시 초기화한다.
    ss["__sims_table_render_mode"] = "full"
    ss.pop("__sims_table_light_reason", None)

    # 1) 결과를 dict payload 로 정규화
    try:
        if isinstance(result, dict):
            payload = _normalize_result_for_chat(result)
        else:
            payload = _normalize_result_for_chat(result)
    except Exception:
        log.exception("[chat] normalize result failed")
        return

    # 2) 액션명/타이틀 보강
    meta = dict(payload.get("meta") or {})
    if action:
        meta["action"] = action
    action_name = (
        action
        or meta.get("action")
        or payload.get("title")
        or "SIMS"
    )
    meta.setdefault("action", action_name)
    meta.setdefault("table_role", _sims_table_role_from_action(action_name, meta))
    payload["meta"] = meta

    if not _chat_payload_matches_current_company(payload):
        payload_company_id, payload_db_name = _chat_payload_company_sig(payload)
        current_company_id, current_db_name = _chat_current_company_sig()

        log.info(
            "[chat.sims.push] skip stale company payload action=%s payload_company_id=%s payload_db=%s current_company_id=%s current_db=%s",
            action_name,
            payload_company_id,
            payload_db_name,
            current_company_id,
            current_db_name,
        )
        return

    # 현재표 후속표는 새로 생성된 파생표를 화면 최신표로 보여주더라도,
    # 다음 "현재표 ... TOP/상세표"의 기준 DF는 원본 상세표/export DF로 유지한다.
    if bool(meta.get("current_table_followup")):
        source_table_key = str(
            meta.get("source_table_key")
            or ss.get("__sims_current_table_source_key")
            or ""
        ).strip()
        if source_table_key:
            meta["source_table_key"] = source_table_key
            ss["__sims_current_table_source_key"] = source_table_key

    payload["meta"] = meta
    payload.setdefault("title", action_name)


    # ✅ 모든 push payload에 고유 id 부여 (스크롤/중복판정 안정화)
    payload.setdefault("id", str(uuid.uuid4()))

    # ✅ NLQ 등에서 동일 조건 반복 호출 시에도 표 버블을 다시 띄우기 위한 플래그
    force_push = bool(meta.get("_force_push")) or bool(ss.pop("__sims_force_push", False))
    if force_push:
        meta.setdefault("_push_nonce", str(uuid.uuid4()))
        payload["meta"] = meta

    # 3) LLM용 SIMS 컨텍스트 갱신
    try:
        df = payload.get("df")
        df_display = payload.get("df_display")

        # 1) df / df_display / data / records/columns 순으로 최대한 DF 확보
        if not isinstance(df, pd.DataFrame):
            if isinstance(df_display, pd.DataFrame):
                df = df_display
            elif payload.get("type") == "table" and isinstance(payload.get("data"), pd.DataFrame):
                df = payload["data"]
                df_display = df
            elif "records" in payload and "columns" in payload:
                try:
                    df = pd.DataFrame.from_records(
                        payload["records"], columns=payload["columns"]
                    )
                    df_display = df
                except Exception:
                    df = None

        # ✅ table_key로 UI DF를 stash한 경우, payload에는 df/df_display가 없을 수 있음
        if not isinstance(df, pd.DataFrame) or df.empty:
            try:
                _mk = (payload.get("meta") or {}).get("table_key")
                if _mk and isinstance(ss.get("sims_tables"), dict):
                    _df2 = ss["sims_tables"].get(_mk)
                    if isinstance(_df2, pd.DataFrame) and not _df2.empty:
                        df = _df2
                        df_display = _df2
            except Exception:
                pass

        if isinstance(df, pd.DataFrame) and not df.empty:
            df = _drop_sensitive_columns(df)
            if isinstance(df_display, pd.DataFrame):
                df_display = _drop_sensitive_columns(df_display)

            if not isinstance(df_display, pd.DataFrame) or df_display.empty:
                df_display = df

            payload["df"] = df
            payload["df_display"] = df_display

            # meta/params 우선순위: meta > params > args
            params = payload.get("meta") or payload.get("params") or payload.get("args") or {}

            df_llm = _shrink_df_for_llm(df_display)

            _build_sims_context_from_result(
                payload,      # 정규화된 payload 기준
                action_name,
                params,
                df_llm,
            )
        else:
            log.info(
                "[chat] SIMS result has no DataFrame; skip context build (keys=%s)",
                list(payload.keys()),
            )

            # 0건/text 결과가 새로 들어온 경우,
            # 직전 표/current source/LLM 분석 컨텍스트를 모두 비운다.
            # 목적:
            # - "현재표 ..." 질문이 이전 표를 잡지 않게 함
            # - LLM fallback이 이전 SIMS_ANALYSIS_CONTEXT로 답하지 않게 함
            try:
                if not bool(meta.get("current_table_followup")):
                    ss.pop("__sims_last_table_key", None)
                    ss.pop("__sims_current_table_source_key", None)
                    ss.pop("__sims_current_table_source_action", None)
                    ss.pop("__sims_current_table_source_analysis_ctx", None)

                    for k in (
                        KEY_SIMS_CTX,
                        "__sims_ctx",
                        "__sims_ctx_hash",
                        "__sims_context",
                        "__sims_context_text",
                        "__sims_context_obj",
                        "__sims_analysis_ctx",
                        "__sims_latest_analysis_key",
                    ):
                        ss.pop(k, None)

                    ss["__sims_ctx_dirty"] = True
                    ss["__sims_last_table_action"] = str(action_name or "")

                    params = payload.get("params") or {}
                    meta = payload.get("meta") or {}

                    query_summary = str(
                        meta.get("query_summary")
                        or payload.get("query_summary")
                        or ""
                    ).strip()

                    if not query_summary:
                        month_from = str(params.get("month_from") or "").strip()
                        month_to = str(params.get("month_to") or "").strip()
                        date_from = str(params.get("date_from") or "").strip()
                        date_to = str(params.get("date_to") or "").strip()

                        if month_from and month_to:
                            query_summary = f"기간 {month_from}~{month_to}"
                        elif date_from and date_to:
                            query_summary = f"기간 {date_from}~{date_to}"

                    # 0건/검증 정상 결과는 채팅 메시지를 짧게 통일한다.
                    # 조회조건은 meta.query_summary로 렌더러가 caption에 1회 표시한다.
                    if "검증" in str(action_name or ""):
                        msg = "검증 결과 이상 자료가 없습니다."
                    else:
                        msg = "해당 조회조건의 자료가 없습니다."

                    payload["type"] = "text"
                    payload["title"] = f"{action_name} 결과 없음"
                    payload["action"] = str(action_name or "")
                    payload["message"] = msg
                    payload["data"] = msg
                    payload["content"] = msg
                    if query_summary:
                        meta["query_summary"] = query_summary
                        meta["condition"] = query_summary

                    meta["row_count"] = 0
                    meta["row_count_total"] = 0
                    meta["tableless_result"] = True
                    meta["current_table_cleared"] = True
                    payload["meta"] = meta
                    
                    log.info(
                        "[chat.tableless] cleared stale SIMS table/context %s action=%s",
                        _chat_runtime_log_kv(),
                        action_name,
                    )

            except Exception:
                log.exception("[chat] clear stale SIMS context after tableless result failed")

    except Exception:
        log.exception("[chat] build SIMS context failed")

    # 3.5) (옵션) 컨텍스트만 갱신하고 채팅 푸시는 생략
    # - NLQ(자연어 자동조회) 등에서 "표는 채팅버블로 별도 렌더"할 때,
    #   여기서 즉시 렌더/푸시를 하면 표가 화면 '맨 위'에 찍히는 부작용이 있음.
    # - __sims_silent_push 플래그가 True면, SIMS_CTX만 갱신하고 종료한다.
    if ss.pop("__sims_silent_push", False):
        log.debug("[chat.sims.silent_push] %s action=%s", _chat_runtime_log_kv(), action_name)
        return

    # 3.55) SIMS 표를 채팅방 history에서 다시 렌더할 수 있도록 session_state에 보관
    # - 화면용 df_display는 sims_tables에 저장
    # - 다운로드용 전체 df는 sims_export_tables에 별도 저장
    # - 채팅방 history에는 table_key만 저장하고, 실제 DF는 session_state에 보관한다.
    try:
        if isinstance(payload, dict) and payload.get("type") == "table":
            df_full_for_export = payload.get("df")
            df_display_for_ui = payload.get("df_display")

            if isinstance(df_full_for_export, pd.DataFrame):
                df_full_for_export = _drop_sensitive_columns(df_full_for_export)
            else:
                df_full_for_export = None

            if isinstance(df_display_for_ui, pd.DataFrame):
                df_display_for_ui = _drop_sensitive_columns(df_display_for_ui)

            if not isinstance(df_display_for_ui, pd.DataFrame):
                if isinstance(df_full_for_export, pd.DataFrame):
                    df_display_for_ui = df_full_for_export
                elif isinstance(payload.get("data"), pd.DataFrame):
                    df_display_for_ui = payload.get("data")
                else:
                    df_display_for_ui = None

            if isinstance(df_display_for_ui, pd.DataFrame) and not df_display_for_ui.empty:
                ss.setdefault("sims_tables", {})
                ss.setdefault("sims_export_tables", {})
                ss.setdefault("__sims_export_tables_by_key", {})

                meta = dict(payload.get("meta") or {})
                table_key = meta.get("table_key") or f"sims_{uuid.uuid4().hex[:8]}"

                # 현재표 후속표 기준 DF/source key 관리
                # - 일반 SIMS/IO 표: 이 표가 새로운 원본 기준표가 된다.
                # - 현재표 후속 파생표: 화면 최신표는 바뀌지만, 후속 집계 기준은 기존 원본 key를 유지한다.
                if bool(meta.get("current_table_followup")):
                    source_table_key = str(
                        meta.get("source_table_key")
                        or ss.get("__sims_current_table_source_key")
                        or ""
                    ).strip()
                    if source_table_key:
                        meta["source_table_key"] = source_table_key
                        ss["__sims_current_table_source_key"] = source_table_key
                else:
                    ss["__sims_current_table_source_key"] = str(table_key)
                    ss["__sims_current_table_source_action"] = str(action_name or "")

                # 화면 렌더용: 제한된 df_display
                ss["sims_tables"][table_key] = df_display_for_ui

                # 다운로드용: 전체 df 우선
                if isinstance(df_full_for_export, pd.DataFrame) and not df_full_for_export.empty:
                    ss["sims_export_tables"][table_key] = df_full_for_export
                    ss["__sims_export_tables_by_key"][table_key] = df_full_for_export
                    meta["download_table_key"] = table_key
                    meta["download_row_count"] = int(len(df_full_for_export))
                    meta["display_row_count"] = int(len(df_display_for_ui))

                    log.info(
                        "[chat.stash.export] %s table_key=%s rows=%s display_rows=%s action=%s",
                        _chat_runtime_log_kv(),
                        table_key,
                        len(df_full_for_export),
                        len(df_display_for_ui),
                        action_name,
                    )
                    if _chat_is_nlq_table_meta(meta):
                        log.info(
                            "[chat.nlq.table] stash export table_key=%s rows=%s",
                            table_key,
                            len(df_full_for_export),
                        )
                else:
                    meta["download_row_count"] = int(len(df_display_for_ui))
                    meta.setdefault("display_row_count", int(len(df_display_for_ui)))

                # ------------------------------------------------------------
                # 현재표 후속분석 전체 기준 보정
                # ------------------------------------------------------------
                # 화면표시는 df_display 일부일 수 있지만,
                # 현재표 후속분석은 전체 조회조건 기준이어야 한다.
                # 대형표 lazy 다운로드 때문에 _get_full_download_df_for_sims_item()가
                # 렌더 시점에 호출되지 않으면, 후속분석이 5,000건 화면표시분만 보게 된다.
                try:
                    action_for_export = str(
                        payload.get("action")
                        or meta.get("action")
                        or action_name
                        or payload.get("title")
                        or ""
                    ).strip()

                    display_rows_for_followup = int(len(df_display_for_ui))
                    expected_rows_for_followup = _expected_analysis_row_count(
                        meta,
                        display_rows_for_followup,
                    )

                    if expected_rows_for_followup > display_rows_for_followup:
                        item_for_export = {
                            "action": action_for_export,
                            "title": payload.get("title"),
                            "params": payload.get("params") or meta.get("params") or {},
                            "meta": meta,
                            "df_display": df_display_for_ui,
                        }

                        full_df_for_followup = _get_full_download_df_for_sims_item(
                            item_for_export,
                            meta,
                            df_display_for_ui,
                        )

                        if (
                            isinstance(full_df_for_followup, pd.DataFrame)
                            and not full_df_for_followup.empty
                            and len(full_df_for_followup) > display_rows_for_followup
                        ):
                            ss["sims_export_tables"][table_key] = full_df_for_followup
                            ss["__sims_export_tables_by_key"][table_key] = full_df_for_followup

                            meta["download_table_key"] = table_key
                            meta["download_row_count"] = int(len(full_df_for_followup))
                            meta["display_row_count"] = int(display_rows_for_followup)
                            meta["row_count_total_for_followup"] = int(len(full_df_for_followup))

                            # 후속질문 기준 source는 전체 DF가 묶인 원본 table_key 유지
                            ss["__sims_current_table_source_key"] = str(table_key)
                            ss["__sims_current_table_source_action"] = action_for_export

                            log.info(
                                "[chat.stash.followup] %s action=%s table_key=%s display_rows=%s full_rows=%s expected_rows=%s",
                                _chat_runtime_log_kv(),
                                action_for_export,
                                table_key,
                                display_rows_for_followup,
                                len(full_df_for_followup),
                                expected_rows_for_followup,
                            )

                except Exception:
                    log.exception("[chat] stash full df for current followup failed")


                ss["__sims_last_table_key"] = table_key
                ss["__sims_last_table_action"] = action_name

                meta["kind"] = "table"
                meta["table_key"] = table_key
                meta.setdefault(
                    "table_role",
                    _sims_table_role_from_action(
                        payload.get("action") or payload.get("title") or meta.get("action"),
                        meta,
                    ),
                )
                meta.setdefault("row_count", int(len(df_full_for_export) if isinstance(df_full_for_export, pd.DataFrame) else len(df_display_for_ui)))
                meta.setdefault("column_count", int(len(df_display_for_ui.columns)))

                if _chat_is_nlq_table_meta(meta):
                    try:
                        source_key_now = str(ss.get("__sims_current_table_source_key") or "").strip()
                        if source_key_now == str(table_key):
                            log.info(
                                "[chat.nlq.table] promoted current source action=%s table_key=%s rows=%s",
                                action_name,
                                table_key,
                                int(meta.get("download_row_count") or meta.get("row_count") or len(df_display_for_ui)),
                            )
                    except Exception:
                        pass

                if _chat_is_stock_io_action(action_name):
                    try:
                        source_key_now = str(ss.get("__sims_current_table_source_key") or "").strip()
                        if source_key_now == str(table_key):
                            log.info(
                                "[stock.table] promoted current source action=%s table_key=%s rows=%s",
                                action_name,
                                table_key,
                                int(meta.get("download_row_count") or meta.get("row_count") or len(df_display_for_ui)),
                            )
                    except Exception:
                        pass

                payload["meta"] = meta
                payload.setdefault("content", payload.get("title") or f"📊 {action_name}")

    except Exception:
        log.exception("[chat] stash sims table/export table before json-safe failed")


    # 3.56) JSON 저장/재렌더 안전화 (DataFrame → meta records/columns)
    _ensure_table_json_safe(payload)

    # 4) 중복 PUSH 방지 (NLQ는 동일 결과라도 표를 다시 띄울 수 있게 옵션 허용)
    meta = dict(payload.get("meta") or {})
    force_push = bool(meta.get("_force_push")) or bool(ss.pop("__sims_force_push", False))
    if force_push:
        meta.setdefault("_push_nonce", str(uuid.uuid4()))
        payload["meta"] = meta

    sig = _sig(payload)
    # (UX) 동일한 조건을 연달아 물어볼 때도 표를 다시 보고 싶을 수 있음.
    #      "즉시 중복(같은 스크립트 런/짧은 시간 내 중복)"만 막고, 그 외에는 허용.
    if not force_push:
        try:
            import time as _time
            _now = _time.time()
            _last = ss.get("__sims_last_push") or {}
            if _last.get("sig") == sig and (_now - float(_last.get("t") or 0)) < 0.35:
                log.debug(
                    "[chat.sims.skip_duplicate] %s sig=%s",
                    _chat_runtime_log_kv(),
                    sig[:8],
                )
                return
        except Exception:
            # 예외 시에는 기존 동작(중복 허용)으로 진행
            pass
    
    # 4-1) ✅ UI 렌더용 테이블은 session_state에 보관하고,
    #      채팅 히스토리에는 table_key만 남겨 JSON 저장/렌더를 안정화
    try:
        if isinstance(payload, dict) and payload.get('type') == 'table':
            # ✅ pandas.DataFrame 은 bool 평가가 불가하므로(or 사용 금지)
            # NOTE: pandas.DataFrame는 bool 평가가 불가하므로(or 사용 금지)
            _df_for_ui = payload.get('df_display')
            if _df_for_ui is None:
                _df_for_ui = payload.get('df')
            if isinstance(_df_for_ui, pd.DataFrame):
                _df_for_ui = _drop_sensitive_columns(_df_for_ui)
                ss.setdefault('sims_tables', {})
                payload.setdefault('meta', {})
                table_key = payload['meta'].get('table_key') or f"sims_{uuid.uuid4().hex[:8]}"
                ss['sims_tables'][table_key] = _df_for_ui
                payload['meta']['kind'] = 'table'
                payload['meta']['table_key'] = table_key
                payload.setdefault('content', payload.get('title') or f"📊 {action_name}")
                payload.pop('df', None)
                payload.pop('df_display', None)
    except Exception:
        log.exception('[chat] stash sims table failed')

    # 4-2) 최소 필드 보강(정렬/렌더)
    if isinstance(payload, dict):
        payload.setdefault('role', 'assistant')
        now_dt = dt.datetime.now()
        payload.setdefault('time', now_dt.strftime('%Y-%m-%d %H:%M:%S'))
        if payload.get('seq') is None:
            ss.setdefault('__seq', 0)
            ss['__seq'] += 1
            payload['seq'] = ss['__seq']
        try:
            meta_for_display = dict(payload.get("meta") or {})
            created_at = str(meta_for_display.get("created_at") or "").strip()
            if not created_at:
                created_at = now_dt.isoformat(timespec="seconds")
            meta_for_display["created_at"] = created_at
            meta_for_display.setdefault("display_time", _coerce_sims_display_time(created_at) or now_dt.strftime("%H:%M:%S"))
            meta_for_display.setdefault("result_seq", int(ss.get("__sims_push_count", 0)) + 1)
            payload["meta"] = meta_for_display
        except Exception:
            log.exception("[chat.sims.push] failed to attach display meta")

    # 5) 인박스에 넣고 drain
    ss.setdefault("__chat_inbox", [])
    ss["__chat_inbox"].append(payload)
    drain_inbox_to_chat()
    ss["__sims_last_push_sig"] = sig
    try:
        import time as _time
        ss["__sims_last_push"] = {"sig": sig, "t": _time.time()}
    except Exception:
        pass
    ss["__sims_push_count"] += 1

    # ✅ 표 버블로 스크롤(메인에서 pop해서 실행)
    # 메인 렌더는 <div id="jump-{message_id}"> 앵커를 만들기 때문에
    # __scroll_to_msg에도 반드시 "jump-" 접두어가 포함되어야 한다.
    try:
        _mid = payload.get("id") or (payload.get("meta") or {}).get("id")
        if _mid:
            ss["__sims_last_msg_id"] = _mid
            ss["__scroll_to_msg"] = f"jump-{_mid}"
        else:
            ss["__scroll_to_msg"] = f"jump-{payload.get('id')}"
    except Exception:
        ss["__scroll_to_msg"] = f"jump-{payload.get('id')}"

    # 기존 로그 + 디버그용 SIMS_PUSH 로그
    rows = cols = None  # ✅ 항상 기본값 선할당(예외 발생해도 UnboundLocal 방지)
    try:
        # ❗ pandas.DataFrame 에서는 "A or B"가 ValueError를 유발할 수 있어 안전하게 선택
        df_log = (
            payload.get("df") if isinstance(payload.get("df"), pd.DataFrame)
            else (payload.get("df_display") if isinstance(payload.get("df_display"), pd.DataFrame) else None)
        )
        if isinstance(df_log, pd.DataFrame):
            rows = int(df_log.shape[0])
            cols = int(df_log.shape[1])
    except Exception:
        pass

    try:
        meta_log = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        if rows is None:
            rows = (
                meta_log.get("row_count_total")
                or meta_log.get("download_row_count")
                or meta_log.get("row_count")
                or meta_log.get("display_row_count")
            )
        if cols is None:
            cols = meta_log.get("column_count")
    except Exception:
        pass

    log.info(
        "[chat.sims.push] %s action=%s rows=%s cols=%s sig=%s count=%d table_key=%s",
        _chat_runtime_log_kv(),
        action_name,
        rows,
        cols,
        sig[:8],
        ss["__sims_push_count"],
        (payload.get("meta") or {}).get("table_key") if isinstance(payload.get("meta"), dict) else "",
    )
    log.info(
        "[SIMS_PUSH] %s action=%s rows=%s cols=%s sig=%s count=%d",
        _chat_runtime_log_kv(),
        action_name,
        rows,
        cols,
        sig[:8],
        ss["__sims_push_count"],
    )

# 조회조건 텍스트 빌더: meta.query_summary > params 기반 텍스트 
# (meta.query_summary이 있으면 우선 사용, 없으면 params에서 날짜/제품/거래처 등 주요 조건을 뽑아서 조합)   
def _fmt_cond_date_text(value: Any) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())

    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    if len(digits) == 6:
        return f"{digits[:4]}-{digits[4:6]}"
    return text

def _trans_di_label(value: Any) -> str:
    mapping = {
        "1": "매입분",
        "3": "매출분",
    }
    v = str(value or "").strip()
    return mapping.get(v, v)


def _tax_di_label(value: Any) -> str:
    mapping = {
        "1": "매입",
        "2": "회계매입",
        "3": "매출",
        "4": "회계매출",
    }
    v = str(value or "").strip()
    return mapping.get(v, v)


def _io_prefix_label(value: Any) -> str:
    mapping = {
        "0": "정상입고",
        "1": "입고반품",
        "2": "장부입고",
        "3": "미결입고",
        "4": "기타입고",
        "5": "정상출고",
        "6": "출고반품",
        "7": "장부출고",
        "8": "미결출고",
        "9": "기타출고",
    }
    v = str(value or "").strip()
    return mapping.get(v, v)

def _stock_side_label(value: Any) -> str:
    v = str(value or "").strip().lower()
    if v in {"in", "입고", "매입"}:
        return "입고집계대상"
    if v in {"out", "출고", "매출"}:
        return "출고집계대상"
    return str(value or "").strip()

# =====================================================================
# 조회조건 텍스트 빌더: meta.query_summary > params 기반 텍스트
# (meta.query_summary이 있으면 우선 사용, 없으면 params에서 날짜/제품/거래처 등 주요 조건을 뽑아서 조합)
# =====================================================================
def _build_query_condition_text(item: Dict[str, Any]) -> str:

    params = dict(item.get("params") or {})
    meta = dict(item.get("meta") or {})

    query_summary = str(meta.get("query_summary") or "").strip()

    # 마스터 NLQ는 nlq_goods/nlq_vendors 쪽에서 만든 query_summary가
    # 가장 정확하다. params를 다시 해석하면 거래처명 + 법정읍면동명처럼
    # 불필요한 조건이 섞일 수 있으므로 우선 사용한다.
    if bool(meta.get("master_nlq")) and query_summary:
        return "조회조건: " + query_summary
    # 분석/KPI는 서비스에서 만든 query_summary가 가장 정확하다.
    # 예: 기간 / 분석자료원 / 추세판정
    if bool(meta.get("analytics")) and query_summary:
        return "조회조건: " + query_summary

    bits = []


    date_from = params.get("date_from")
    date_to = params.get("date_to")
    month_from = params.get("month_from")
    month_to = params.get("month_to")

    if date_from or date_to:
        if str(date_from or "") == str(date_to or "") and date_from:
            bits.append(f"기간 {_fmt_cond_date_text(date_from)}")
        else:
            bits.append(
                f"기간 {_fmt_cond_date_text(date_from)} ~ {_fmt_cond_date_text(date_to)}"
            )
    elif month_from or month_to:
        if str(month_from or "") == str(month_to or "") and month_from:
            bits.append(f"기준월 {_fmt_cond_date_text(month_from)}")
        else:
            bits.append(
                f"기준월 {_fmt_cond_date_text(month_from)} ~ {_fmt_cond_date_text(month_to)}"
            )

            
    source_mode = params.get("source_mode") or params.get("분석자료원")
    if source_mode:
        source_label = {
            "auto": "자동",
            "monthly_book": "월집계-장부재고",
            "monthly_real": "월집계-실재고",
            "detail": "출고상세",
        }.get(str(source_mode), str(source_mode))
        bits.append(f"분석자료원 {source_label}")    
    
    
    if params.get("physic_cd"):
        if params.get("physic_nm"):
            bits.append(f"제품 {params.get('physic_cd')} ({params.get('physic_nm')})")
        else:
            bits.append(f"제품 {params.get('physic_cd')}")
    elif params.get("physic_nm"):
        bits.append(f"제품명 {params.get('physic_nm')}")

    if params.get("ven_cd"):
        if params.get("ven_nm"):
            bits.append(f"거래처 {params.get('ven_cd')} ({params.get('ven_nm')})")
        else:
            bits.append(f"거래처 {params.get('ven_cd')}")
    elif params.get("ven_nm"):
        bits.append(f"거래처명 {params.get('ven_nm')}")

    # 거래처 마스터 NLQ 한글 params fallback
    vendor_param_map = [
        ("거래처코드", "거래처코드"),
        ("거래처명", "거래처명"),
        ("대표자명", "대표자명"),
        ("사업자등록번호", "사업자등록번호"),
        ("전화번호", "전화번호"),
        ("영업사원명", "영업사원명"),
        ("단가적용처명", "단가적용처명"),
        ("재고적용처명", "재고적용처명"),
        ("등록자", "등록자"),
        ("등록일자", "등록일자"),
        ("수정자", "수정자"),
        ("수정일자", "수정일자"),
    ]

    for key, label in vendor_param_map:
        v = str(params.get(key) or "").strip()
        if v:
            text = f"{label} {v}"
            if text not in bits:
                bits.append(text)

    if params.get("in_seq"):
        bits.append(f"입고순번 {params.get('in_seq')}")
    if params.get("out_seq"):
        bits.append(f"출고순번 {params.get('out_seq')}")
    if params.get("trans_seq"):
        bits.append(f"거래명세서순번 {params.get('trans_seq')}")
    if params.get("tax_seq"):
        bits.append(f"세금계산서순번 {params.get('tax_seq')}")

    if params.get("trans_di"):
        bits.append(f"거래명세서구분 {_trans_di_label(params.get('trans_di'))}")
    if params.get("tax_di"):
        bits.append(f"세금계산서구분 {_tax_di_label(params.get('tax_di'))}")

    if params.get("io_gu_prefix"):
        bits.append(f"입출고구분 {_io_prefix_label(params.get('io_gu_prefix'))}")

    if params.get("stock_side"):
        bits.append(f"집계방향 {_stock_side_label(params.get('stock_side'))}")

    has_mismatch_trans = str(params.get("only_mismatch_trans") or "").upper() in {"Y", "1", "TRUE"}
    has_mismatch_tax = str(params.get("only_mismatch_tax") or "").upper() in {"Y", "1", "TRUE"}
    has_mismatch_any = str(params.get("only_mismatch") or "").upper() in {"Y", "1", "TRUE"}

    if has_mismatch_trans:
        bits.append("거래명세서 불일치만")

    if has_mismatch_tax:
        bits.append("세금계산서 불일치만")

    if has_mismatch_any and not (has_mismatch_trans or has_mismatch_tax):
        bits.append("불일치만")


    if params.get("ven_group_nm"):
        bits.append(f"거래처그룹명 {params.get('ven_group_nm')}")
    if params.get("ven_kind_nm"):
        bits.append(f"거래처종류명 {params.get('ven_kind_nm')}")

    # 도로명주소 / 지역 조건
    sido_nm = (
        params.get("sido_nm")
        or params.get("road_sido_nm")
        or params.get("시도명")
    )
    if sido_nm:
        bits.append(f"시도명 {sido_nm}")

    gugun_nm = (
        params.get("gugun_nm")
        or params.get("road_gugun_nm")
        or params.get("시구군명")
    )
    if gugun_nm:
        bits.append(f"시구군명 {gugun_nm}")

    dong_nm = (
        params.get("dong_nm")
        or params.get("road_dong_nm")
        or params.get("법정읍면동명")
        or params.get("법정동명")
    )
    if dong_nm:
        bits.append(f"법정읍면동명 {dong_nm}")

    road_nm = (
        params.get("road_nm")
        or params.get("도로명")
    )
    if road_nm:
        bits.append(f"도로명 {road_nm}")

    road_addr_kw = (
        params.get("road_addr_kw")
        or params.get("road_address_kw")
        or params.get("도로명주소")
    )
    if road_addr_kw:
        bits.append(f"도로명주소 {road_addr_kw}")

    addr_kw = (
        params.get("addr_kw")
        or params.get("address_kw")
        or params.get("주소")
        or params.get("상세주소")        
    )
    if addr_kw:
        bits.append(f"주소 {addr_kw}")


    if params.get("maker_cd"):
        if params.get("maker_nm"):
            bits.append(f"제조사 {params.get('maker_cd')} ({params.get('maker_nm')})")
        else:
            bits.append(f"제조사 {params.get('maker_cd')}")
    elif params.get("maker_nm"):
        bits.append(f"제조사명 {params.get('maker_nm')}")

    if params.get("order_cd"):
        if params.get("order_nm"):
            bits.append(f"발주처 {params.get('order_cd')} ({params.get('order_nm')})")
        else:
            bits.append(f"발주처 {params.get('order_cd')}")
    elif params.get("order_nm"):
        bits.append(f"발주처명 {params.get('order_nm')}")

    if params.get("buy_cd"):
        if params.get("buy_nm"):
            bits.append(f"매입처 {params.get('buy_cd')} ({params.get('buy_nm')})")
        else:
            bits.append(f"매입처 {params.get('buy_cd')}")
    elif params.get("buy_nm"):
        bits.append(f"매입처명 {params.get('buy_nm')}")

    if params.get("real_ven_cd"):
        if params.get("real_ven_nm"):
            bits.append(f"실납처 {params.get('real_ven_cd')} ({params.get('real_ven_nm')})")
        else:
            bits.append(f"실납처 {params.get('real_ven_cd')}")
    elif params.get("real_ven_nm"):
        bits.append(f"실납처명 {params.get('real_ven_nm')}")

    if params.get("sales_man"):
        if params.get("sales_man_nm"):
            bits.append(f"영업사원 {params.get('sales_man')} ({params.get('sales_man_nm')})")
        else:
            bits.append(f"영업사원 {params.get('sales_man')}")
    elif params.get("sales_man_nm"):
        bits.append(f"영업사원명 {params.get('sales_man_nm')}")

    if params.get("product_group_nm"):
        bits.append(f"제품그룹명 {params.get('product_group_nm')}")
    if params.get("product_di_nm"):
        bits.append(f"제품구분명 {params.get('product_di_nm')}")
    if params.get("product_class_nm"):
        bits.append(f"제품분류명 {params.get('product_class_nm')}")

    if params.get("add_nm"):
        bits.append(f"등록자명 {params.get('add_nm')}")
    if params.get("mod_nm"):
        bits.append(f"수정자명 {params.get('mod_nm')}")

    if params.get("stock_mode"):
        bits.append(f"기준 {params.get('stock_mode')}")

    if params.get("flow_scope"):
        bits.append(f"범위 {params.get('flow_scope')}")

    if params.get("date_basis"):
        bits.append(f"기준일자 {params.get('date_basis')}")

    if params.get("group_basis"):
        bits.append(f"집계기준 {params.get('group_basis')}")

    if params.get("price_mode"):
        bits.append(f"단가기준 {params.get('price_mode')}")

    stock_cds = params.get("stock_cds")

    if isinstance(stock_cds, (list, tuple)) and stock_cds:
        bits.append("재고위치 " + ",".join(str(x) for x in stock_cds))
    elif params.get("stock_cd"):
        bits.append(f"재고위치 {params.get('stock_cd')}")
    elif params.get("stock_nm"):
        bits.append(f"재고위치명 {params.get('stock_nm')}")

    if params.get("stock_apply_cd"):
        bits.append(f"재고적용처 {params.get('stock_apply_cd')}")
    elif params.get("stock_apply_nm"):
        bits.append(f"재고적용처명 {params.get('stock_apply_nm')}")
    
    query_summary = str(meta.get("query_summary") or "").strip()
    if bits:
        return "조회조건: " + " / ".join(bits)
    if query_summary:
        return "조회조건: " + query_summary
    return ""

def _fmt_metric_num(value: Any) -> str:
    try:
        n = float(pd.to_numeric(value, errors="coerce"))
    except Exception:
        return "0"

    if abs(n) < 1e-12:
        return "0"
    if n.is_integer():
        return f"{int(n):,}"
    return f"{n:,.2f}".rstrip("0").rstrip(".")


def _is_product_flow_action(action_name: str) -> bool:
    return action_name == "제품수불현황 조회"


def _is_product_inventory_action(action_name: str) -> bool:
    return action_name == "제품재고현황 조회"

def _is_monthly_stock_action(action_name: str) -> bool:
    return action_name in {"실재고월집계 조회", "장부재고월집계 조회"}

def _is_sales_trend_action(action_name: str) -> bool:
    return action_name in {
        "품목별 매출 추세 분석",
        "품목별 매출 추세 요약표",
        "품목별 매출 예상",
        "품목별 재고부족현황",
    }


def _build_product_flow_info_caption(meta: Dict[str, Any]) -> str:
    info = meta.get("product_info") or {}
    if not isinstance(info, dict) or not info:
        return ""

    bits = [
        f"제품코드 {str(info.get('제품코드') or '').strip()}",
        f"제품명 {str(info.get('제품명') or '').strip()}",
        f"규격 {str(info.get('규격') or '').strip()}",
        f"최종보험가 {_fmt_metric_num(info.get('최종보험가'))}",
        f"보험코드 {str(info.get('보험코드') or '').strip()}",
        f"표준코드 {str(info.get('표준코드') or '').strip()}",
        f"제조사명 {str(info.get('제조사명') or '').strip()}",
        f"발주처명 {str(info.get('발주처명') or '').strip()}",
        f"제품그룹명 {str(info.get('제품그룹명') or '').strip()}",
    ]

    product_class_nm = str(info.get("제품분류명") or "").strip()
    if product_class_nm:
        bits.append(f"제품분류명 {product_class_nm}")

    special_nm = str(info.get("특수관리제품명") or "").strip()
    if special_nm:
        bits.append(f"특수관리제품명 {special_nm}")

    bits = [x for x in bits if not x.endswith(" ")]
    return "제품정보: " + " / ".join(bits)


def _build_product_inventory_info_caption(meta: Dict[str, Any], item: Dict[str, Any]) -> str:
    info = meta.get("product_info") or {}
    if not isinstance(info, dict) or not info:
        return ""

    bits = [
        f"제품코드 {str(info.get('제품코드') or '').strip()}",
        f"제품명 {str(info.get('제품명') or '').strip()}",
        f"규격 {str(info.get('규격') or '').strip()}",
        f"제조사명 {str(info.get('제조사명') or '').strip()}",
        f"발주처명 {str(info.get('발주처명') or '').strip()}",
        f"제품그룹명 {str(info.get('제품그룹명') or '').strip()}",
    ]

    product_di_nm = str(info.get("제품구분명") or "").strip()
    if product_di_nm:
        bits.append(f"제품구분명 {product_di_nm}")

    product_class_nm = str(info.get("제품분류명") or "").strip()
    if product_class_nm:
        bits.append(f"제품분류명 {product_class_nm}")

    special_nm = str(info.get("특수관리제품명") or "").strip()
    if special_nm:
        bits.append(f"특수관리제품명 {special_nm}")

    insu_cd = str(info.get("보험코드") or "").strip()
    if insu_cd:
        bits.append(f"보험코드 {insu_cd}")

    std_cd = str(info.get("표준코드") or "").strip()
    if std_cd:
        bits.append(f"표준코드 {std_cd}")

    bits.append(f"현보험약가 {_fmt_metric_num(info.get('현보험약가'))}")

    bits = [x for x in bits if not x.endswith(" ")]
    return "제품정보: " + " / ".join(bits)

def _render_product_flow_metrics(meta: Dict[str, Any]) -> None:
    labels = ["이월재고", "입고수량", "출고수량", "재고수량"]
    values = [
        _fmt_metric_num(meta.get("carry_qty")),
        _fmt_metric_num(meta.get("in_qty")),
        _fmt_metric_num(meta.get("out_qty")),
        _fmt_metric_num(meta.get("stock_qty")),
    ]

    cols = st.columns(4)
    for col, label, value in zip(cols, labels, values):
        with col:
            st.caption(label)
            st.markdown(f"### {value}")


def _render_product_inventory_metrics(meta: Dict[str, Any]) -> None:
    top_labels = ["이월수량", "입고수량", "출고수량"]
    top_values = [
        _fmt_metric_num(meta.get("sum_carry_qty")),
        _fmt_metric_num(meta.get("sum_in_qty")),
        _fmt_metric_num(meta.get("sum_out_qty")),
    ]

    bottom_labels = ["재고수량", "재고금액", "보험금액"]
    bottom_values = [
        _fmt_metric_num(meta.get("sum_stock_qty")),
        _fmt_metric_num(meta.get("sum_stock_amt")),
        _fmt_metric_num(meta.get("sum_insu_amt")),
    ]

    top_cols = st.columns(3)
    for col, label, value in zip(top_cols, top_labels, top_values):
        with col:
            st.caption(label)
            st.markdown(f"### {value}")

    bottom_cols = st.columns(3)
    for col, label, value in zip(bottom_cols, bottom_labels, bottom_values):
        with col:
            st.caption(label)
            st.markdown(f"### {value}")

def _render_monthly_stock_metrics(meta: Dict[str, Any]) -> None:
    if not meta.get("monthly_stock_summary"):
        return

    st.caption("월집계요약")

    top_labels = ["입고수량", "입고할증수량", "입고공급가액", "입고세액"]
    top_values = [
        _fmt_metric_num(meta.get("sum_in_qty")),
        _fmt_metric_num(meta.get("sum_in_bonus_qty")),
        _fmt_metric_num(meta.get("sum_in_supply_amt")),
        _fmt_metric_num(meta.get("sum_in_tax_amt")),
    ]

    bottom_labels = ["출고수량", "출고할증수량", "출고공급가액", "출고세액"]
    bottom_values = [
        _fmt_metric_num(meta.get("sum_out_qty")),
        _fmt_metric_num(meta.get("sum_out_bonus_qty")),
        _fmt_metric_num(meta.get("sum_out_supply_amt")),
        _fmt_metric_num(meta.get("sum_out_tax_amt")),
    ]

    top_cols = st.columns(4)
    for col, label, value in zip(top_cols, top_labels, top_values):
        with col:
            st.caption(label)
            st.markdown(f"### {value}")

    bottom_cols = st.columns(4)
    for col, label, value in zip(bottom_cols, bottom_labels, bottom_values):
        with col:
            st.caption(label)
            st.markdown(f"### {value}")

def _render_sales_trend_metrics(meta: Dict[str, Any]) -> None:
    """
    품목별 매출 추세 분석 상단 요약 헤더.

    예전에는 간단 숫자형으로만 표시했지만,
    채팅 SSOT 정책에서는 패널의 상세 카드 요약을 채팅창에 그대로 표시한다.
    """
    _render_chat_analysis_header(meta)


# ---------------------------------------------------------------------
# 채팅 결과 집계요약 fallback
# ---------------------------------------------------------------------
def _chat_summary_fmt_num(value: Any) -> str:
    n = _chat_parse_num(value)
    if n is None:
        s = str(value or "").strip()
        return s if s else "0"
    if abs(n - int(n)) < 1e-9:
        return f"{int(n):,}"
    return f"{n:,.2f}".rstrip("0").rstrip(".")


def _chat_summary_num_sum(df: pd.DataFrame, col: str) -> float:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or col not in df.columns:
            return 0.0
        return float(df[col].map(lambda x: _chat_parse_num(x) or 0.0).sum())
    except Exception:
        return 0.0


def _chat_summary_top_counts(df: pd.DataFrame, col: str, limit: int = 10) -> str:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or col not in df.columns:
            return ""
        s = df[col].fillna("").astype(str).str.strip()
        s = s[(s != "") & (~s.isin(["None", "nan", "NaN", "<NA>", "NaT"]))]
        if s.empty:
            return ""
        return ", ".join(f"{k} {int(v):,}건" for k, v in s.value_counts().head(limit).items())
    except Exception:
        return ""


def _chat_summary_records_line(label: str, rows: Any, *, value_key: str = "row_count", limit: int = 10) -> str:
    if not isinstance(rows, list) or not rows:
        return ""
    parts = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or row.get("제품명") or row.get("거래처명") or row.get("재고위치") or "").strip()
        if not name:
            name = "(미지정)"
        value = row.get(value_key)
        if value is None and value_key != "row_count":
            value = row.get("row_count")
        unit = "건" if value_key == "row_count" else ""
        parts.append(f"{name} {_chat_summary_fmt_num(value)}{unit}")
    if not parts:
        return ""
    return f"{label}: " + ", ".join(parts)


def _chat_summary_condition_text(meta: Dict[str, Any], item: Dict[str, Any]) -> str:
    try:
        qs = str(meta.get("query_summary") or meta.get("condition") or "").strip()
        if qs:
            return qs
        params = item.get("params") or meta.get("params") or {}
        if not isinstance(params, dict):
            return "전체"
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
            value = params.get(key)
            if isinstance(value, (list, tuple, set)):
                value = ", ".join(str(x).strip() for x in value if str(x).strip())
            value = str(value or "").strip()
            if value:
                parts.append(f"{label} {value}")
        return " / ".join(parts) if parts else "전체"
    except Exception:
        return "전체"


def _chat_is_io_summary_action(action_name: str) -> bool:
    action = str(action_name or "")
    return any(
        key in action
        for key in [
            "입고명세", "출고명세", "거래명세서", "세금계산서",
            "월집계", "제품수불", "제품재고", "검증",
        ]
    )


def _build_chat_fallback_summary_md(
    item: Dict[str, Any],
    meta: Dict[str, Any],
    data: Any,
    action_name: str,
) -> str:
    """
    일부 IO/재고 조회 payload가 summary_md 없이 표만 올라오는 경우를 위한
    채팅 표시용 정량 요약 fallback.

    - LLM 분석이 아니라 pandas/meta 기반의 결정적 요약이다.
    - 기존 summary_md가 있는 경우에는 이 함수가 사용되지 않는다.
    """
    if not _chat_is_io_summary_action(action_name):
        return ""
    if bool(meta.get("candidate_table")) or bool(meta.get("current_table_followup")):
        return ""
    if not isinstance(data, pd.DataFrame) or data.empty:
        return ""

    try:
        row_total = int(
            meta.get("download_row_count")
            or meta.get("row_count_total")
            or meta.get("row_count_loaded")
            or meta.get("row_count")
            or len(data)
            or 0
        )
    except Exception:
        row_total = len(data)

    try:
        row_display = int(meta.get("display_row_count") or len(data) or 0)
    except Exception:
        row_display = len(data)

    lines: list[str] = []
    title = str(action_name or item.get("title") or "SIMS 조회").strip() or "SIMS 조회"
    lines.append(f"{title} 집계 요약")
    lines.append("")
    lines.append("분석 기준: 조회된 전체 결과 기준")
    lines.append(f"조회조건: {_chat_summary_condition_text(meta, item)}")
    lines.append(f"전체 조회건수: {row_total:,}건")
    lines.append(f"화면 표시건수: {row_display:,}건")

    # 월집계 서비스는 전체 SQL 집계 결과를 monthly_stock_detail_summary에 보유한다.
    monthly_detail = meta.get("monthly_stock_detail_summary")
    if isinstance(monthly_detail, dict) and monthly_detail:
        numeric_pairs = [
            ("입고수량합계", monthly_detail.get("in_qty_sum") or meta.get("sum_in_qty")),
            ("입고공급가액합계", monthly_detail.get("in_supply_sum") or meta.get("sum_in_supply_amt")),
            ("입고세액합계", monthly_detail.get("in_tax_sum") or meta.get("sum_in_tax_amt")),
            ("출고수량합계", monthly_detail.get("out_qty_sum") or meta.get("sum_out_qty")),
            ("출고공급가액합계", monthly_detail.get("out_supply_sum") or meta.get("sum_out_supply_amt")),
            ("출고세액합계", monthly_detail.get("out_tax_sum") or meta.get("sum_out_tax_amt")),
        ]
        for label, value in numeric_pairs:
            if value not in (None, ""):
                lines.append(f"{label}: {_chat_summary_fmt_num(value)}")

        for label, key in [
            ("월별 상위", "by_month"),
            ("입출고구분별 상위", "by_io_type"),
            ("제품별 상위", "top_products"),
            ("거래처별 상위", "top_vendors"),
            ("재고위치별 상위", "top_stock_locations"),
        ]:
            line = _chat_summary_records_line(label, monthly_detail.get(key), value_key="row_count")
            if line:
                lines.append(line)

        lines.append("답변 규칙: 위 집계는 전체 조회결과 기준입니다.")
        return "\n".join(lines).strip()

    # 일반 IO/명세/검증/재고 표는 현재 payload DataFrame 기준으로 요약한다.
    numeric_cols = [
        "수량", "할증수량", "공급가액", "세액", "합계금액", "할인금액",
        "입고수량", "입고할증수량", "입고공급가액", "입고세액",
        "출고수량", "출고할증수량", "출고공급가액", "출고세액",
        "이월수량", "이월금액", "재고수량", "재고금액", "보험금액",
        "차이금액", "상세합_공급가액", "상세합_세액",
    ]
    added_numeric = 0
    for col in numeric_cols:
        if col in data.columns:
            total = _chat_summary_num_sum(data, col)
            if abs(total) > 1e-9:
                lines.append(f"{col} 합계: {_chat_summary_fmt_num(total)}")
                added_numeric += 1
            if added_numeric >= 12:
                break

    top_cols = [
        ("일자별 상위", "입고일자"),
        ("일자별 상위", "출고일자"),
        ("일자별 상위", "거래명세서일자"),
        ("일자별 상위", "세금계산서일자"),
        ("제품별 상위", "제품명"),
        ("거래처별 상위", "거래처명"),
        ("매입처별 상위", "매입처명"),
        ("재고위치별 상위", "재고위치"),
        ("입출고구분별 상위", "입출고구분"),
        ("명세서구분별 상위", "거래명세서구분명"),
        ("계산서구분별 상위", "세금계산서구분명"),
        ("매입매출구분별 상위", "매입매출구분"),
        ("제조사별 상위", "제조사"),
        ("제조사별 상위", "제조사명"),
    ]
    seen_labels: set[str] = set()
    for label, col in top_cols:
        if label in seen_labels and label.endswith("상위"):
            # 같은 label은 첫 매칭 컬럼만 사용한다.
            continue
        line = _chat_summary_top_counts(data, col)
        if line:
            lines.append(f"{label}: {line}")
            seen_labels.add(label)

    lines.append("답변 규칙: 위 집계는 조회된 전체 결과 기준입니다.")
    return "\n".join(lines).strip()


def _clean_chat_summary_text(summary_md: Any, cond_text: str = "") -> str:
    summary_text = str(summary_md or "").strip()
    if not summary_text:
        return ""

    if cond_text and summary_text.startswith("조회조건:"):
        lines = summary_text.splitlines()[1:]
        summary_text = "\n".join(lines).strip()

    try:
        cleaned_lines = []
        for line in summary_text.splitlines():
            s = str(line or "").strip()
            if not s:
                cleaned_lines.append(line)
                continue
            if (
                s.startswith("조회결과:")
                or s.startswith("조회 완료:")
                or s.startswith("조회완료:")
            ):
                continue
            cleaned_lines.append(line)
        summary_text = "\n".join(cleaned_lines).strip()
    except Exception:
        pass

    return summary_text


def _render_chat_summary_expander(summary_md: Any, cond_text: str = "") -> None:
    summary_text = _clean_chat_summary_text(summary_md, cond_text)
    if not summary_text:
        return
    with st.expander("집계 요약 펼쳐보기", expanded=False):
        st.markdown(summary_text)

# ──────────────────────────────────────────────────────────────────────────────
# 렌더링
# ──────────────────────────────────────────────────────────────────────────────
def _render_chat_item(item: Dict[str, Any], *, target=None) -> None:
    """채팅 아이템을 렌더링한다. target이 있으면 해당 컨테이너 안에 렌더."""
    _tgt = target if target is not None else globals().get("_CHAT_RENDER_TARGET", None)

    force_target = target is not None
    try:
        if force_target:
            st.session_state["__chat_render_force_target"] = True

        if _tgt is not None:
            with _tgt:
                _render_chat_item_body(item)
        else:
            _render_chat_item_body(item)
    finally:
        if force_target:
            st.session_state["__chat_render_force_target"] = False

# SIMS 표 렌더링 캐시 관리: _get_sims_table_render_cache / _get_sims_table_render_cache_key / _clear_other_sims_table_render_cache
# 목적: rerun 때마다 표 렌더링 준비/스타일링을 다시 수행하지 않도록, table_key 기준으로 최신 표 1개만 캐시한다.
def _get_sims_table_render_cache() -> Dict[str, Any]:
    """
    SIMS 표 렌더링 캐시.

    목적:
    - rerun 때마다 _prepare_io_display_df / _build_io_display_styler /
      _apply_chat_analysis_grade_style 를 다시 수행하지 않도록 한다.
    - table_key 기준으로 최신 표 1개만 캐시한다.
    """
    ss = st.session_state
    cache = ss.setdefault("__sims_table_render_cache", {})
    if not isinstance(cache, dict):
        cache = {}
        ss["__sims_table_render_cache"] = cache
    return cache

# SIMS 표 렌더링 캐시 키 생성: table_key + action_name + mode + df shape
# table_key는 meta.table_key 우선, 없으면 item.id, seq 등으로 대체. action_name은 item.action/meta.action/title에서 추출.
# mode는 product_flow/product_inventory/monthly_stock/sales_trend 등 주요 액션 구분. df shape는 행/열 수로 간단히 표현.
def _get_sims_table_render_cache_key(
    item: Dict[str, Any],
    meta: Dict[str, Any],
    action_name: str,
    mode: str,
    df: pd.DataFrame,
) -> str:
    table_key = str(meta.get("table_key") or item.get("id") or "")
    shape_key = f"{len(df)}x{len(df.columns)}"
    return f"{table_key}:{action_name}:{mode}:{shape_key}"

# SIMS 표 렌더링 캐시 관리: 최신 table_key 외의 캐시 제거. 이전 표를 최신 1개만 남기는 정책과 맞춘다.
# table_key가 없으면 제거하지 않는다(안전장치). 예외 발생해도 제거 실패 로그만 남기고 무시한다.
def _clear_other_sims_table_render_cache(current_table_key: str) -> None:
    """
    최신 table_key 외의 렌더 캐시 제거.
    이전 표를 최신 1개만 남기는 정책과 맞춘다.
    """
    if not current_table_key:
        return

    try:
        cache = _get_sims_table_render_cache()
        remove_keys = [
            k for k in cache.keys()
            if not str(k).startswith(f"{current_table_key}:")
        ]
        for k in remove_keys:
            cache.pop(k, None)
    except Exception:
        log.exception("[chat] clear old sims render cache failed")


def _lookup_sims_table_payload_for_render(
    item: Dict[str, Any],
    meta: Dict[str, Any],
) -> tuple[Any, str]:
    """
    저장된 SIMS 표 메시지를 다시 렌더할 때 DataFrame payload를 찾는다.

    순서:
    1. 메시지 자체 data/df/df_display
    2. session_state 표시/다운로드 cache
    3. 현재표 followup source key
    4. records/columns fallback
    """
    data = item.get("data")
    if isinstance(data, pd.DataFrame):
        return data, "item.data"

    for key_name in ("df_display", "df"):
        cand = item.get(key_name)
        if isinstance(cand, pd.DataFrame):
            return cand, f"item.{key_name}"

    table_key = str((meta or {}).get("table_key") or item.get("table_key") or "").strip()
    download_key = str((meta or {}).get("download_table_key") or "").strip()
    source_key = str(
        (meta or {}).get("source_table_key")
        or (meta or {}).get("source_key")
        or ""
    ).strip()

    ss = st.session_state
    search_keys: list[tuple[str, str]] = []
    for label, key in (
        ("table_key", table_key),
        ("download_table_key", download_key),
        ("source_key", source_key),
    ):
        if key and (label, key) not in search_keys:
            search_keys.append((label, key))

    stores = (
        ("sims_tables", ss.get("sims_tables")),
        ("sims_export_tables", ss.get("sims_export_tables")),
        ("__sims_export_tables_by_key", ss.get("__sims_export_tables_by_key")),
    )

    for key_label, key in search_keys:
        for store_name, store in stores:
            try:
                if isinstance(store, dict):
                    cand = store.get(key)
                    if isinstance(cand, pd.DataFrame):
                        return cand, f"{store_name}.{key_label}"
            except Exception:
                continue

    try:
        recs = item.get("records")
        cols = item.get("columns")
        if recs is not None and cols is not None:
            return pd.DataFrame.from_records(recs, columns=cols), "item.records"
    except Exception:
        pass

    try:
        recs = (meta or {}).get("records") or (meta or {}).get("df")
        cols = (meta or {}).get("columns")
        if recs is not None and cols is not None:
            return pd.DataFrame.from_records(recs, columns=cols), "meta.records"
    except Exception:
        pass

    return None, ""


def _sims_table_meta_row_count(meta: Dict[str, Any], data: Any = None) -> tuple[int, int, int]:
    full_rows = _safe_int_for_download(
        meta.get("download_row_count")
        or meta.get("row_count_total_for_followup")
        or meta.get("row_count_total")
        or meta.get("row_count_loaded")
        or meta.get("row_count")
        or meta.get("rows")
        or (len(data) if isinstance(data, pd.DataFrame) else 0),
        0,
    )
    display_rows = _safe_int_for_download(
        meta.get("display_row_count")
        or meta.get("display_rows")
        or (len(data) if isinstance(data, pd.DataFrame) else full_rows)
        or 0,
        0,
    )
    expected_rows = _safe_int_for_download(
        meta.get("expected_rows")
        or meta.get("db_total_count")
        or meta.get("total_count")
        or full_rows,
        0,
    )
    return full_rows, display_rows, expected_rows


def _sims_render_dedupe_key(item: Dict[str, Any], meta: Dict[str, Any], data: Any = None) -> str:
    """한 rerun 화면 안에서 같은 SIMS 결과 카드가 두 번 그려지는 것을 막기 위한 key."""
    if not isinstance(item, dict):
        return ""

    item_type = str(item.get("type") or "").strip().lower()
    is_sims_like = (
        item_type in {"table", "text", "object"}
        and (
            bool(meta.get("kind") == "table")
            or bool(meta.get("source") == "SIMS")
            or bool(meta.get("analytics"))
            or bool(meta.get("current_table_followup"))
            or bool(item.get("action"))
            or bool(item.get("title"))
        )
    )
    if not is_sims_like:
        return ""

    msg_id = str(item.get("id") or meta.get("id") or "").strip()
    table_key = str(meta.get("table_key") or item.get("table_key") or "").strip()
    if msg_id:
        return f"id:{msg_id}"
    if table_key:
        return f"table:{table_key}"

    action = str(item.get("action") or meta.get("action") or item.get("title") or "").strip()
    full_rows, display_rows, expected_rows = _sims_table_meta_row_count(meta, data)
    created_at = str(
        meta.get("created_at")
        or item.get("time")
        or meta.get("time")
        or meta.get("ts")
        or meta.get("timestamp")
        or ""
    ).strip()
    sig = str(meta.get("sig") or item.get("sig") or "").strip()
    return f"sig:{sig}:{action}:{full_rows}:{display_rows}:{expected_rows}:{created_at}"


def _should_render_sims_message_once(item: Dict[str, Any], meta: Dict[str, Any], data: Any = None) -> bool:
    key = _sims_render_dedupe_key(item, meta, data)
    if not key:
        return True

    table_key = str(meta.get("table_key") or item.get("table_key") or "").strip()
    action = str(item.get("action") or meta.get("action") or item.get("title") or "").strip()
    full_rows, display_rows, expected_rows = _sims_table_meta_row_count(meta, data)
    created_at = str(
        meta.get("created_at")
        or item.get("time")
        or meta.get("time")
        or meta.get("ts")
        or meta.get("timestamp")
        or ""
    ).strip()
    sig = str(meta.get("sig") or item.get("sig") or "").strip()

    candidate_keys = [key]
    if table_key:
        candidate_keys.append(f"table:{table_key}")
    if action:
        candidate_keys.append(f"shape:{sig}:{action}:{full_rows}:{display_rows}:{expected_rows}:{created_at}")

    ss = st.session_state
    rendered = ss.setdefault("__chat_rendered_sims_keys_this_run", set())
    if not isinstance(rendered, set):
        rendered = set(rendered or [])
        ss["__chat_rendered_sims_keys_this_run"] = rendered

    matched_key = next((k for k in candidate_keys if k in rendered), "")
    if matched_key:
        try:
            log.info(
                "[chat.render.dedupe] skip duplicate sims message key=%s action=%s table_key=%s",
                matched_key,
                action,
                table_key,
            )
        except Exception:
            pass
        return False

    for k in candidate_keys:
        if k:
            rendered.add(k)
    try:
        log.info(
            "[chat.render.dedupe] render sims message key=%s action=%s table_key=%s",
            key,
            action,
            table_key,
        )
    except Exception:
        pass
    return True


def _render_expired_sims_table_fallback(item: Dict[str, Any], meta: Dict[str, Any]) -> None:
    """DataFrame payload가 사라진 과거 SIMS 표를 요약 카드로 렌더한다."""
    action_name = str(
        item.get("action")
        or meta.get("action")
        or item.get("title")
        or item.get("content")
        or "SIMS 결과"
    ).strip() or "SIMS 결과"
    table_key = str(meta.get("table_key") or item.get("table_key") or "").strip()
    source_key = str(meta.get("source_table_key") or meta.get("source_key") or "").strip()
    result_time_text = _sims_result_datetime_text(item, meta)
    cond_text = _build_query_condition_text(item)
    if not cond_text:
        query_summary = str(meta.get("query_summary") or "").strip()
        if query_summary:
            cond_text = f"조회조건: {query_summary}"
    full_rows, display_rows, expected_rows = _sims_table_meta_row_count(meta)

    st.markdown(f"---\n##### {action_name}")
    if result_time_text:
        time_label = "처리시각" if bool(meta.get("current_table_followup")) else "조회시각"
        st.caption(f"{time_label}: {result_time_text}")
    else:
        st.caption("조회시각: 저장된 메타에 시간 정보가 없습니다.")

    if cond_text:
        st.caption(cond_text)

    row_bits: list[str] = []
    if expected_rows and expected_rows != full_rows:
        row_bits.append(f"expected_rows={expected_rows:,}")
    if full_rows:
        row_bits.append(f"rows={full_rows:,}")
    if display_rows:
        row_bits.append(f"display_rows={display_rows:,}")
    if row_bits:
        st.caption(" / ".join(row_bits))

    key_bits = []
    if table_key:
        key_bits.append(f"table_key={table_key}")
    if source_key:
        key_bits.append(f"source_key={source_key}")
    source_action = str(meta.get("source_action") or meta.get("source_table_action") or "").strip()
    if source_action:
        key_bits.append(f"source_action={source_action}")
    if key_bits:
        st.caption(" / ".join(key_bits))

    st.info(
        "표 데이터는 현재 세션에서 만료되어 다시 펼칠 수 없습니다.\n\n"
        "단, 당시 조회 요약은 아래에 남아 있습니다.\n"
        "필요하면 같은 조회를 다시 실행해 주세요."
    )

    summary_md = meta.get("summary_md") or meta.get("summary") or ""
    if isinstance(summary_md, str) and summary_md.strip():
        _render_chat_summary_expander(summary_md, cond_text)

    try:
        log.info(
            "[chat.table.render] payload expired fallback table_key=%s action=%s has_meta=%s",
            table_key,
            action_name,
            bool(meta),
        )
        log.info(
            "[chat.table.render] expired table did not clear current source table_key=%s",
            table_key,
        )
    except Exception:
        pass


# 채팅 아이템 본문 렌더링. anchor/busy 체크 후 렌더링.
# anchor가 있으면 그 안에 렌더링하고, busy 플래그로 중복 렌더링 방지.
def _render_chat_item_body(item: Dict[str, Any]) -> None:
    # 명시적 target 렌더 중이면 anchor가 가로채지 않게 한다.
    try:
        force_target = bool(st.session_state.get("__chat_render_force_target", False))
    except Exception:
        force_target = False

    anchor = None if force_target else _get_chat_render_anchor()

    try:
        busy = bool(st.session_state.get("__chat_render_anchor_busy", False))
    except Exception:
        busy = False

    if anchor is not None and not busy:
        try:
            st.session_state["__chat_render_anchor_busy"] = True
            with anchor:
                _render_chat_item(item)
        finally:
            try:
                st.session_state["__chat_render_anchor_busy"] = False
            except Exception:
                pass
        return

    """
    표준 payload를 화면에 표시.
    SIMS 결과/테이블 1건 렌더
    """

    t = str(item.get("type") or "").strip().lower()
    title = item.get("title") or ""
    meta = item.get("meta") or {}
    data = item.get("data")

    if not _should_render_sims_message_once(item, meta, data):
        return

    # 저장된 히스토리(DF 제거)에서도 표를 다시 그릴 수 있게 records/columns 지원

    if t == "table" and not isinstance(data, pd.DataFrame):
        data, _payload_source = _lookup_sims_table_payload_for_render(item, meta)
        if isinstance(data, pd.DataFrame):
            try:
                log.info(
                    "[chat.table.render] payload found table_key=%s action=%s rows=%s",
                    str(meta.get("table_key") or item.get("table_key") or ""),
                    str(item.get("action") or meta.get("action") or title or ""),
                    len(data),
                )
            except Exception:
                pass
        else:
            with st.chat_message((item.get("role") or "assistant").lower() if (item.get("role") or "assistant").lower() in ("assistant", "user") else "assistant"):
                _render_expired_sims_table_fallback(item, meta)
            return

    if isinstance(data, pd.DataFrame):
        data = normalize_display_df_for_streamlit(data)

    role = (item.get("role") or "assistant").lower()
    if role not in ("assistant", "user"):
        role = "assistant"

    # 동일 라벨(다운로드 버튼 등) 충돌 방지용 uid
    # ✅ 충돌 방지 강화:
    # - 같은 액션/같은 행수/같은 ts 조합이 같은 rerun에 2번 렌더되면 key 충돌이 나기 쉬움
    # - item.id / item.seq / meta.table_key 등 "고유값"을 uid에 포함시킨다.
    _uid_src = "|".join([
        str(item.get("id") or ""),
        str(item.get("seq") or ""),
        str(meta.get("table_key") or ""),
        str(meta.get("ts") or meta.get("timestamp") or ""),
        str(meta.get("action") or title)[:80],
        str(len(data) if isinstance(data, pd.DataFrame) else 0),
    ])
    uid = hashlib.md5(_uid_src.encode("utf-8", "ignore")).hexdigest()[:12]
    # ✅ 같은 uid 충돌 방지용 suffix(아이템 고유값)
    # item id/seq가 있으면 반드시 포함해서 다운로드 버튼 key가 중복되지 않게 한다.
    _uniq = f"{item.get('id','')}_{item.get('seq','')}"
    if _uniq == "_":
        _uniq = str(meta.get("ts") or meta.get("timestamp") or "")
    uid2 = f"{uid}_{_uniq}"

    # ✅ 채팅 흐름 안으로 렌더(상단으로 튀는 체감 완화)
    with st.chat_message(role):
        # SIMS 결과/현재표 후속표가 일반 LLM 분석 답변과 섞여 보이지 않도록
        # 각 SIMS 아이템 시작부에 구분 헤더를 명확히 표시한다.
        action_name_for_header = str(item.get("action") or meta.get("action") or title or "").strip()
        is_sims_table_or_text = t in {"table", "text", "object"}

        if is_sims_table_or_text:

            header_title = action_name_for_header or str(title or "결과").strip() or "결과"
            is_current_followup = bool(meta.get("current_table_followup"))
            result_time_text = _sims_result_datetime_text(item, meta)

            st.markdown(f"---\n##### {header_title}")

            try:
                db_total_rows = _safe_int_for_download(
                    meta.get("db_total_count")
                    or meta.get("total_count")
                    or meta.get("matched_row_count")
                    or meta.get("matched_total_count")
                    or 0,
                    0,
                )

                header_loaded_rows = _safe_int_for_download(
                    meta.get("download_row_count")
                    or meta.get("row_count_loaded")
                    or meta.get("row_count_total")
                    or meta.get("row_count")
                    or (len(data) if isinstance(data, pd.DataFrame) else 0)
                    or 0,
                    0,
                )

                header_display_rows = _safe_int_for_download(
                    meta.get("display_row_count")
                    or (len(data) if isinstance(data, pd.DataFrame) else header_loaded_rows)
                    or 0,
                    0,
                )

                if is_current_followup:
                    source_action = str(
                        meta.get("source_action")
                        or meta.get("source_table_action")
                        or meta.get("source_title")
                        or ""
                    ).strip()
                    source_rows = _safe_int_for_download(
                        meta.get("source_rows")
                        or meta.get("source_row_count")
                        or meta.get("row_count_total_for_followup")
                        or 0,
                        0,
                    )
                    if source_action or source_rows:
                        source_text = source_action or "원본 현재표"
                        if source_rows:
                            st.caption(f"원본: {source_text} / {source_rows:,}건")
                        else:
                            st.caption(f"원본: {source_text}")

                if db_total_rows and header_loaded_rows and db_total_rows > header_loaded_rows:
                    st.caption(
                        f"결과: 조건 전체 {db_total_rows:,}건 중 {header_loaded_rows:,}건 조회, "
                        f"화면 {header_display_rows:,}건 표시"
                    )
                elif header_loaded_rows > 0:
                    if header_display_rows and header_display_rows < header_loaded_rows:
                        st.caption(f"결과: 조회 {header_loaded_rows:,}건 중 화면 {header_display_rows:,}건 표시")
                    else:
                        st.caption(f"결과: {header_loaded_rows:,}건")

                if result_time_text:
                    time_label = "처리시각" if is_current_followup else "조회시각"
                    st.caption(f"{time_label}: {result_time_text}")
            except Exception:
                pass

        elif title:
            st.markdown(f"**{title}**")


        # 현재표 후속계산 text 결과는 표 렌더러/다운로드/meta expander를 타지 않고
        # 일반 assistant 답변처럼 표시한다.
        if bool(meta.get("render_as_text")):
            cond_text = _build_query_condition_text(item)

            try:
                meta_query_summary = str(meta.get("query_summary") or "").strip()
                if meta_query_summary:
                    cond_text = f"조회조건: {meta_query_summary}"
            except Exception:
                pass

            if cond_text:
                st.caption(cond_text)

            try:
                action_name_for_count = str(item.get("action") or meta.get("action") or title or "").strip()
                if "검증" in action_name_for_count:
                    row_count = int(
                        meta.get("row_count_total")
                        or meta.get("download_row_count")
                        or meta.get("row_count")
                        or (len(data) if isinstance(data, pd.DataFrame) else 0)
                        or 0
                    )
                    display_count = int(
                        meta.get("display_row_count")
                        or (len(data) if isinstance(data, pd.DataFrame) else row_count)
                        or 0
                    )

                    if row_count > 0:
                        if row_count > display_count:
                            st.caption(f"조회결과: 전체 {row_count:,}건 중 화면 {display_count:,}건 표시")
                        else:
                            st.caption(f"조회결과: {row_count:,}건")
            except Exception:
                pass

            msg = (
                item.get("message")
                or item.get("content")
                or item.get("data")
                or ""
            )
            if msg:
                st.markdown(str(msg))

            ts = (
                item.get("time")
                or meta.get("time")
                or meta.get("ts")
                or meta.get("timestamp")
                or ""
            )
            if ts and not _sims_result_datetime_text(item, meta):
                st.caption(str(ts))

            return

        action_name = str(item.get("action") or meta.get("action") or title or "").strip()
        is_product_flow = _is_product_flow_action(action_name)
        is_product_inventory = _is_product_inventory_action(action_name)
        is_monthly_stock = _is_monthly_stock_action(action_name)
        is_sales_trend = (
            _is_sales_trend_action(action_name)
            or meta.get("analysis_type") in {"sales_trend", "sales_forecast", "stock_shortage"}
            or meta.get("summary_type") in {"product_summary", "product_forecast", "product_stock_shortage"}
        )

        cond_text = _build_query_condition_text(item)

        # IO/NLQ는 meta.query_summary가 가장 완전한 조회조건이다.
        # 예: 기간 + 기준월 + 최근 1개월 자동적용
        try:
            meta_query_summary = str(meta.get("query_summary") or "").strip()
            if meta_query_summary:
                cond_text = f"조회조건: {meta_query_summary}"
        except Exception:
            pass

        if cond_text:
            st.caption(cond_text)

        _render_nlq_table_meta_caption(meta)

        if bool(meta.get("candidate_table")):
            # 제품수불현황 후보표 안내
            guide_text = (
                meta.get("summary_md")
                or item.get("message")
                or item.get("data")
                or ""
            )
            guide_text = str(guide_text or "").strip()

            if not guide_text:
                guide_text = "제품 후보 목록입니다. 원하는 번호를 채팅창에 입력해 주세요. 예: 1번 / 첫번째 제품"

            # st.info는 여백이 커서 표와 따로 노는 느낌이 있으므로 compact 안내문으로 표시
            st.markdown(
                f"""
                <div style="
                    background:#eef6ff;
                    border:1px solid #d5e8ff;
                    border-radius:8px;
                    padding:8px 12px;
                    margin:4px 0 8px 0;
                    font-size:0.92rem;
                    line-height:1.45;
                ">
                {guide_text.replace(chr(10), "<br>")}
                </div>
                """,
                unsafe_allow_html=True,
            )
        elif is_product_flow:
            product_info_text = _build_product_flow_info_caption(meta)
            if product_info_text:
                st.caption(product_info_text)

            _render_product_flow_metrics(meta)
            _render_chat_summary_expander(
                meta.get("summary_md") or meta.get("summary") or _build_chat_fallback_summary_md(item, meta, data, action_name),
                cond_text,
            )

        elif is_product_inventory:            
            product_info_text = _build_product_inventory_info_caption(meta, item)
            if product_info_text:
                st.caption(product_info_text)

            _render_product_inventory_metrics(meta)
            _render_chat_summary_expander(
                meta.get("summary_md") or meta.get("summary") or _build_chat_fallback_summary_md(item, meta, data, action_name),
                cond_text,
            )

        elif is_monthly_stock:
            _render_monthly_stock_metrics(meta)
            _render_chat_summary_expander(
                meta.get("summary_md") or meta.get("summary") or _build_chat_fallback_summary_md(item, meta, data, action_name),
                cond_text,
            )

        elif is_sales_trend:
            _render_sales_trend_metrics(meta)
            _render_chat_summary_expander(
                meta.get("summary_md") or meta.get("summary") or _build_chat_fallback_summary_md(item, meta, data, action_name),
                cond_text,
            )

        else:
            if _chat_is_analysis_payload(item, meta, title):
                _render_chat_analysis_header(meta)
                _render_chat_summary_expander(
                    meta.get("summary_md") or meta.get("summary") or _build_chat_fallback_summary_md(item, meta, data, action_name),
                    cond_text,
                )
            else:
                summary_md = meta.get("summary_md") or meta.get("summary") or _build_chat_fallback_summary_md(item, meta, data, action_name)

                if isinstance(summary_md, str) and summary_md.strip():
                    summary_text = summary_md.strip()

                    # 위에서 조회조건을 caption으로 이미 표시했으면,
                    # summary_md의 첫 줄 조회조건은 중복 표시하지 않는다.
                    if cond_text and summary_text.startswith("조회조건:"):
                        lines = summary_text.splitlines()
                        lines = lines[1:]
                        summary_text = "\n".join(lines).strip()

                    # 조회건수/표시건수는 위 헤더에서 이미 표시한다.
                    # summary_md 안의 첫 줄 또는 중간 줄에 같은 의미의 "조회결과:"가 있으면
                    # 중복 표시되므로 제거한다.
                    try:
                        cleaned_lines = []
                        for line in summary_text.splitlines():
                            s = str(line or "").strip()

                            if not s:
                                cleaned_lines.append(line)
                                continue

                            # 예:
                            # - 조회결과: 20,000건 (표시는 상위 5,000건)
                            # - 조회결과: 전체 20,000건 중 화면 5,000건 표시
                            # - 조회 완료: 20,000건
                            if (
                                s.startswith("조회결과:")
                                or s.startswith("조회 완료:")
                                or s.startswith("조회완료:")
                            ):
                                continue

                            cleaned_lines.append(line)

                        summary_text = "\n".join(cleaned_lines).strip()
                    except Exception:
                        pass

                    if summary_text:
                        action_for_summary = str(
                            item.get("action")
                            or meta.get("action")
                            or title
                            or ""
                        ).strip()

                        # 마스터 조회의 summary_md는 표 위에 길게 나오면
                        # LLM 분석이 먼저 나온 것처럼 보인다.
                        # 따라서 마스터 요약은 접기 처리하고, 표를 바로 볼 수 있게 한다.
                        master_summary_actions = {
                            "거래처 목록",
                            "거래처 상세",
                            "사용자목록 + 부서명",
                            "부서별 사용자 수",
                            "부서별사용자수",
                            "그룹코드조회",
                            "그룹별 코드 조회",
                            "코드명 검색",
                            "제품코드 목록",
                            "제품코드 상세",
                            "제품코드목록",
                            "제품코드상세",
                            "도로명주소 조회",
                            "입고명세 조회",
                            "출고명세 조회",
                            "거래명세서 공통 조회",
                            "세금계산서 공통 조회",
                            "실재고월집계 조회",
                            "장부재고월집계 조회",
                            "입고↔거래명세서 검증",
                            "입고↔세금계산서 검증",
                            "출고↔거래명세서 검증",
                            "출고↔세금계산서 검증",
                            "제품수불현황 조회",
                            "제품재고현황 조회",
                        }

                        is_master_summary = (
                            action_for_summary in master_summary_actions
                            or bool(meta.get("master_nlq"))
                            or str(meta.get("domain") or "").strip() in {
                                "vendors",
                                "vendor",
                                "users",
                                "codes",
                                "goods",
                                "road_address",
                            }
                        )

                        if is_master_summary and not bool(meta.get("current_table_followup")):
                            with st.expander("집계 요약 펼쳐보기", expanded=False):
                                st.markdown(summary_text)
                        else:
                            st.markdown(summary_text)


            debug_meta = str(os.getenv("SSAI_DEBUG_META", "false")).strip().lower() in {
                "1", "true", "yes", "y", "on"
            }

            if meta and debug_meta and not bool(meta.get("hide_meta_expander")):
                with st.expander("meta", expanded=False):
                    st.json(meta)


        if t == "table" and not isinstance(data, pd.DataFrame):
            # data가 DF로 안 들어오는 경우(meta에 저장된 records/columns)로 복구
            try:
                _m = item.get("meta") or {}
                if isinstance(_m.get("df"), list) and isinstance(_m.get("columns"), list):
                    try:
                        data = pd.DataFrame.from_records(_m["df"], columns=_m["columns"])
                    except Exception:
                        data = pd.DataFrame(_m["df"])
            except Exception:
                pass

        # 표 렌더링
        # - data가 DataFrame이면 data로, 아니면 df_display로 렌더 시도
        # - 둘 다 아니면 일반 텍스트/JSON 렌더링으로 fallback
        if t == "table" and isinstance(data, pd.DataFrame):

            # 이전 drilldown 표는 매 rerun마다 다시 그리지 않는다.
            # 다운로드 df 준비도 무겁기 때문에 current/reference/강제표시일 때만 수행한다.
            if not _should_full_render_sims_table(item, meta, uid2):
                _render_old_sims_table_placeholder(item, meta, data, uid2)
                return

            # 다운로드용 전체 DF는 대형표일 경우 [다운로드 준비] 전에는 만들지 않는다.
            # 기존에는 여기서 _get_full_download_df_for_sims_item()가 먼저 실행되어
            # 화면 표시 200건짜리 NLQ 결과도 전체 9,615건 export 재조회가 발생했다.
            try:
                display_rows_initial = int(len(data)) if isinstance(data, pd.DataFrame) else 0
            except Exception:
                display_rows_initial = 0

            try:
                expected_rows_initial = _expected_analysis_row_count(meta, display_rows_initial)
            except Exception:
                expected_rows_initial = display_rows_initial

            try:
                threshold_rows = _get_sims_download_lazy_threshold_rows()
            except Exception:
                threshold_rows = 5000

            download_ready_key = f"__sims_download_ready::{uid2}"

            defer_full_export = (
                threshold_rows > 0
                and expected_rows_initial >= threshold_rows
                and expected_rows_initial > display_rows_initial
                and not bool(st.session_state.get(download_ready_key))
            )

            if defer_full_export:
                raw_download_df = data
                _chat_log_info_once(
                    f"defer_full_download::{uid2}::{display_rows_initial}::{expected_rows_initial}",
                    "[chat] defer full download df until prepare action=%s display_rows=%s expected_rows=%s key=%s",
                    item.get("action") or meta.get("action") or title,
                    display_rows_initial,
                    expected_rows_initial,
                    uid2,
                )
            else:
                raw_download_df = _get_full_download_df_for_sims_item(item, meta, data)


            # 표 렌더링 캐시 준비
            # 반드시 data가 DataFrame으로 확정된 뒤 여기서 선언해야 한다.
            table_key_for_cache = str(meta.get("table_key") or item.get("id") or "")

            try:
                _clear_other_sims_table_render_cache(table_key_for_cache)
            except Exception:
                log.exception("[chat] clear old sims table render cache failed")

            render_cache = _get_sims_table_render_cache()

            action_name = str(item.get("action") or meta.get("action") or title or "").strip()
            is_nlq_table = _chat_is_nlq_table_meta(meta)
            is_stock_io_table = _chat_is_stock_io_action(action_name)
            nlq_table_key = str(meta.get("table_key") or item.get("table_key") or item.get("id") or "").strip()

            # 후보 선택표는 실제 제품수불현황 표가 아니다.
            # 후보표는 6컬럼 정도의 선택용 목록이므로 전체 폭을 채우지 않고 compact하게 렌더한다.
            # CSV / Excel / LLM 분석 버튼은 후보표에는 붙이지 않는다.
            if bool(meta.get("candidate_table")):
                try:
                    candidate_df = data.copy()

                    # product_flow_service.py에서 이미 '번호' 컬럼을 만들지만,
                    # 혹시 없을 때만 보조로 추가한다.
                    if "번호" not in candidate_df.columns and "순번" not in candidate_df.columns:
                        candidate_df.insert(0, "번호", range(1, len(candidate_df) + 1))

                    # 후보표 컬럼 순서 고정
                    preferred_cols = [
                        "번호",
                        "제품코드",
                        "제품명",
                        "제조사명",
                        "제품그룹명",
                        "제품구분명",
                        "제품분류명",
                    ]
                    use_cols = [c for c in preferred_cols if c in candidate_df.columns]
                    extra_cols = [c for c in candidate_df.columns if c not in use_cols]
                    candidate_df = candidate_df[use_cols + extra_cols]

                    # 코드 컬럼은 앞자리 0 보존
                    for code_col in ["제품코드", "보험코드", "표준코드", "제조사코드"]:
                        if code_col in candidate_df.columns:
                            candidate_df[code_col] = candidate_df[code_col].astype(str).str.strip()

                    column_config = {}

                    if "번호" in candidate_df.columns:
                        column_config["번호"] = st.column_config.NumberColumn(
                            "번호",
                            format="localized",
                            step=1,
                            width=70,
                        )

                    if "제품코드" in candidate_df.columns:
                        column_config["제품코드"] = st.column_config.TextColumn(
                            "제품코드",
                            width=90,
                        )

                    if "제품명" in candidate_df.columns:
                        column_config["제품명"] = st.column_config.TextColumn(
                            "제품명",
                            width=460,
                        )

                    if "제조사명" in candidate_df.columns:
                        column_config["제조사명"] = st.column_config.TextColumn(
                            "제조사명",
                            width=150,
                        )

                    if "제품그룹명" in candidate_df.columns:
                        column_config["제품그룹명"] = st.column_config.TextColumn(
                            "제품그룹명",
                            width=110,
                        )

                    if "제품구분명" in candidate_df.columns:
                        column_config["제품구분명"] = st.column_config.TextColumn(
                            "제품구분명",
                            width=110,
                        )

                    if "제품분류명" in candidate_df.columns:
                        column_config["제품분류명"] = st.column_config.TextColumn(
                            "제품분류명",
                            width=110,
                        )

                    st.dataframe(
                        candidate_df,
                        use_container_width=False,
                        hide_index=True,
                        height=min(460, 80 + 30 * max(len(candidate_df), 1)),
                        column_config=column_config if column_config else None,
                    )

                    log.debug(
                        "[chat] candidate compact table render rows=%s cols=%s",
                        len(candidate_df),
                        len(candidate_df.columns),
                    )

                except Exception:
                    log.exception("[chat] candidate compact table render failed")
                    st.dataframe(
                        data,
                        use_container_width=False,
                        hide_index=True,
                        height=min(460, 80 + 30 * max(len(data), 1)),
                    )

                return

            is_io_table = any(
                k in action_name
                for k in (
                    "입고", "출고", "거래명세서", "세금계산서",
                    "실재고", "장부재고", "제품수불현황", "제품재고현황", "검증",
                )
            )

            if is_io_table:
                try:
                    if (is_nlq_table or is_stock_io_table) and _chat_is_large_table_for_fast_render(data):
                        if is_stock_io_table:
                            _chat_log_stock_table_render(
                                action_name=action_name,
                                rows=int(len(data)),
                                cols=int(len(data.columns)),
                                fast=True,
                            )
                        if is_nlq_table:
                            _chat_log_nlq_table_render(
                                action_name=action_name,
                                table_key=nlq_table_key,
                                rows=int(len(data)),
                                cols=int(len(data.columns)),
                                fast=True,
                            )
                        _render_chat_fast_dataframe(
                            data.copy(),
                            height=520,
                            action_name=action_name,
                            meta=meta,
                        )
                        raise StopIteration
                    # IO/NLQ 기본표도 sims_table_display.py 공용 설정을 사용한다.
                    # 목적:
                    # - 좌측 고정 pinned 적용
                    # - 컬럼 폭/높이 계산 통일
                    # - 채팅 현재표 후속표와 동일한 표 렌더 규칙 사용
                    render_df = _prepare_io_display_df(data, add_row_no=True)

                    view_df, column_config, table_width, table_height = build_sims_table_display_config(
                        render_df,
                        action_name=action_name,
                        meta=meta,
                        add_row_no=False,      # _prepare_io_display_df에서 이미 순번 보강
                        row_no_name="순번",
                        enable_pinning=True,
                        max_pinned_cols=4,
                        min_width=720,
                        max_width=1650,
                        min_height=170,
                        max_height=520,
                        row_height=32,
                    )

                    view_df = _chat_clean_display_none_values(view_df)
                    column_config = _chat_drop_number_config_for_blank_numeric_cols(view_df, column_config)

                    log.debug(
                        "[chat] io common table render action=%s rows=%s cols=%s natural_width=%s height=%s pinned=%s",
                        action_name,
                        len(view_df),
                        len(view_df.columns),
                        table_width,
                        table_height,
                        True,
                    )

                    if is_nlq_table:
                        _chat_log_nlq_table_render(
                            action_name=action_name,
                            table_key=nlq_table_key,
                            rows=int(len(view_df)),
                            cols=int(len(view_df.columns)),
                            fast=False,
                        )
                    if is_stock_io_table:
                        _chat_log_stock_table_render(
                            action_name=action_name,
                            rows=int(len(view_df)),
                            cols=int(len(view_df.columns)),
                            fast=False,
                        )

                    rendered_with_style = False
                    if is_nlq_table or is_stock_io_table:
                        try:
                            styled_view = _build_io_display_styler(view_df, add_row_no=False, band_size=5)
                            styled_view = _apply_chat_analysis_grade_style(styled_view, view_df)
                            st.dataframe(
                                styled_view,
                                use_container_width=True,
                                hide_index=True,
                                height=table_height,
                                column_config=column_config if column_config else None,
                            )
                            rendered_with_style = True
                        except Exception:
                            log.debug("[chat] io small table styler skipped", exc_info=True)

                    if not rendered_with_style:
                        st.dataframe(
                            view_df,
                            use_container_width=True,
                            hide_index=True,
                            height=table_height,
                            column_config=column_config if column_config else None,
                        )

                except StopIteration:
                    pass
                except Exception:
                    log.exception("[chat] io common table render failed")
                    try:
                        render_df = _prepare_io_display_df(data, add_row_no=True)
                    except Exception:
                        render_df = data

                    render_df = _chat_clean_display_none_values(render_df)                        

                    st.dataframe(
                        render_df,
                        use_container_width=True,
                        hide_index=True,
                        height=520,
                    )


            else:
                view_df = data.copy()

                is_analysis_table = _chat_is_analysis_payload(item, meta, title)

                if is_analysis_table:
                    try:
                        render_df = data.copy()

                        if "순번" not in render_df.columns and "조회순번" not in render_df.columns:
                            render_df.insert(0, "순번", range(1, len(render_df) + 1))

                        is_large_analysis_table = _chat_is_large_table_for_fast_render(render_df)
                        if is_nlq_table:
                            _chat_log_nlq_table_render(
                                action_name=action_name,
                                table_key=nlq_table_key,
                                rows=int(len(render_df)),
                                cols=int(len(render_df.columns)),
                                fast=bool(is_large_analysis_table),
                            )
                        if is_large_analysis_table:
                            st.caption("빠른 표 모드: 채팅 분석/KPI 큰 표는 속도를 위해 셀 색상/굵은 글씨 서식을 생략합니다.")

                        # 분석/KPI 표는 좌측 고정 컬럼이 우선이다.
                        # Pandas Styler 경로는 Streamlit pinned column_config가 적용되지 않는 경우가 있어,
                        # 기본 렌더도 공용 DataFrame renderer로 통일한다.
                        # 단, 분석/KPI 원표는 이미 서비스에서 순번/요약 컬럼을 구성하므로
                        # IO 전용 display_df 변환을 다시 적용하지 않는다. (요약표/컬럼 차이 방지)
                        if is_large_analysis_table:
                            _render_chat_fast_dataframe(
                                render_df.copy(),
                                height=520,
                                action_name=action_name,
                                meta=meta,
                            )
                        else:
                            view_df, column_config, table_width, table_height = build_sims_table_display_config(
                                normalize_display_df_for_streamlit(render_df.copy()),
                                action_name=action_name,
                                meta=meta,
                                add_row_no=False,
                                row_no_name="순번",
                                enable_pinning=True,
                                max_pinned_cols=5,
                                min_width=720,
                                max_width=1650,
                                min_height=170,
                                max_height=520,
                                row_height=32,
                            )
                            view_df = _chat_clean_display_none_values(view_df)
                            column_config = _chat_drop_number_config_for_blank_numeric_cols(view_df, column_config)
                            try:
                                styled_view = _build_io_display_styler(view_df, add_row_no=False, band_size=5)
                                styled_view = _apply_chat_analysis_grade_style(styled_view, view_df)
                                st.dataframe(
                                    styled_view,
                                    use_container_width=True,
                                    hide_index=True,
                                    height=table_height,
                                    column_config=column_config if column_config else None,
                                )
                            except Exception:
                                log.debug("[chat] small analysis table styler skipped", exc_info=True)
                                st.dataframe(
                                    view_df,
                                    use_container_width=True,
                                    hide_index=True,
                                    height=table_height,
                                    column_config=column_config if column_config else None,
                                )

                    except Exception:
                        log.exception("[chat] analysis table fast/styler render failed")
                        try:
                            fallback_df, column_config, _table_width, table_height = build_sims_table_display_config(
                                normalize_display_df_for_streamlit(data.copy()),
                                action_name=action_name,
                                meta=meta,
                                add_row_no=False,
                                row_no_name="순번",
                                enable_pinning=True,
                                max_pinned_cols=5,
                                min_width=720,
                                max_width=1650,
                                min_height=170,
                                max_height=520,
                                row_height=32,
                            )
                            st.dataframe(
                                fallback_df,
                                use_container_width=True,
                                hide_index=True,
                                height=table_height,
                                column_config=column_config if column_config else None,
                            )
                        except Exception:
                            st.dataframe(
                                normalize_display_df_for_streamlit(data),
                                use_container_width=True,
                                hide_index=True,
                                height=520,
                            )

                else:
                    try:
                        if is_nlq_table and _chat_is_large_table_for_fast_render(view_df):
                            _chat_log_nlq_table_render(
                                action_name=action_name,
                                table_key=nlq_table_key,
                                rows=int(len(view_df)),
                                cols=int(len(view_df.columns)),
                                fast=True,
                            )
                            _render_chat_fast_dataframe(
                                view_df.copy(),
                                height=520,
                                action_name=action_name,
                                meta=meta,
                            )
                            raise StopIteration

                        view_df, column_config, table_width, table_height = build_sims_table_display_config(
                            view_df,
                            action_name=action_name,
                            meta=meta,
                            add_row_no=True,
                            row_no_name="순번",
                            enable_pinning=True,
                            min_width=720,
                            max_width=1650,
                            min_height=170,
                            max_height=520,
                            row_height=32,
                        )

                        view_df = _chat_clean_display_none_values(view_df)
                        column_config = _chat_drop_number_config_for_blank_numeric_cols(view_df, column_config)

#                        log.info(
                        log.debug(
                            "[chat] common table render action=%s rows=%s cols=%s natural_width=%s height=%s pinned=%s container_width=True",
                            action_name,
                            len(view_df),
                            len(view_df.columns),
                            table_width,
                            table_height,
                            True,
                        )

                        if is_nlq_table:
                            _chat_log_nlq_table_render(
                                action_name=action_name,
                                table_key=nlq_table_key,
                                rows=int(len(view_df)),
                                cols=int(len(view_df.columns)),
                                fast=False,
                            )

                        rendered_with_style = False
                        if is_nlq_table:
                            try:
                                styled_view = _build_io_display_styler(view_df, add_row_no=False, band_size=5)
                                styled_view = _apply_chat_analysis_grade_style(styled_view, view_df)
                                st.dataframe(
                                    styled_view,
                                    use_container_width=True,
                                    hide_index=True,
                                    height=table_height,
                                    column_config=column_config if column_config else None,
                                )
                                rendered_with_style = True
                            except Exception:
                                log.debug("[chat] nlq common small table styler skipped", exc_info=True)

                        if not rendered_with_style:
                            st.dataframe(
                                view_df,
                                use_container_width=True,
                                hide_index=True,
                                height=table_height,
                                column_config=column_config if column_config else None,
                            )

                    except StopIteration:
                        pass
                    except Exception:
                        log.exception("[chat] common table render failed")
                        fallback_df = data.copy()
                        if "조회순번" not in fallback_df.columns and "순번" not in fallback_df.columns:
                            fallback_df.insert(0, "순번", range(1, len(fallback_df) + 1))

                        fallback_df = _chat_clean_display_none_values(fallback_df)

                        st.dataframe(
                            fallback_df,
                            use_container_width=True,
                            hide_index=True,
                            height=520,
                        )

            # 다운로드 (CSV / XLSX)
            try:
                action = item.get("action") or meta.get("action")
                base_name = action or title or "SIMS_RESULT"
                safe_base = re.sub(r"[^\w가-힣\-]+", "_", str(base_name)).strip("_")
                ts_str = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                csv_name = f"{safe_base}_{ts_str}.csv"
                xlsx_name = f"{safe_base}_{ts_str}.xlsx"

                display_rows_for_download = int(len(data)) if isinstance(data, pd.DataFrame) else 0
                download_rows = int(len(raw_download_df)) if isinstance(raw_download_df, pd.DataFrame) else 0
                expected_rows = _expected_analysis_row_count(meta, display_rows_for_download)

                if expected_rows > download_rows and bool(locals().get("defer_full_export", False)):
                    st.caption(
                        f"CSV/EXCEL 다운로드 기준: 전체 조회조건 {expected_rows:,}건 "
                        f"(현재 화면 표시 {display_rows_for_download:,}건, "
                        "[다운로드 준비] 후 전체 export 생성)"
                    )
                elif download_rows > display_rows_for_download:
                    st.caption(
                        f"CSV/EXCEL 다운로드 기준: 전체 조회조건 {download_rows:,}건 "
                        f"(화면 표시 {display_rows_for_download:,}건)"
                    )
                elif expected_rows > download_rows:
                    st.caption(
                        f"CSV/EXCEL 다운로드 기준: {download_rows:,}건 "
                        f"(전체 예상 {expected_rows:,}건, 다운로드 최대건수 설정 확인 필요)"
                    )

                prompt = _build_sims_detail_analysis_prompt(
                    action_name=str(action or action_name or title or ""),
                    display_rows=display_rows_for_download,
                    download_rows=download_rows,
                    expected_rows=expected_rows,
                )

                _render_sims_result_actions_lazy(
                    key_suffix=uid2,
                    download_df=raw_download_df,
                    csv_name=csv_name,
                    xlsx_name=xlsx_name,
                    prompt=prompt,
                    expected_rows=expected_rows,
                    display_rows=display_rows_for_download,
                )

            except Exception:
                log.exception("[chat] download buttons render failed")

            return

        # 안내/빈결과 메시지는 type 값보다 message 존재 여부를 우선해서 렌더한다.
        msg_text = str(item.get("message") or "").strip()

        if t == "text":
            text_to_show = msg_text or str(data or "").strip()
            if text_to_show in {"해당 자료 없습니다.", "해당 자료가 없습니다.", "조회 결과가 없습니다."}:
                text_to_show = "해당 조회조건의 자료가 없습니다."
            st.info(text_to_show or "해당 조회조건의 자료가 없습니다.")
            return

        # type이 달라도 message가 있으면 안내 메시지로 렌더
        if msg_text and not isinstance(data, pd.DataFrame):
            st.info(msg_text)
            return

        if t == "object":
            if isinstance(data, (dict, list)):
                st.json(data)
            elif data is not None and str(data).strip():
                st.info(str(data).strip())
            else:
                st.info("표시할 내용이 없습니다.")
            return

        # fallback
        if isinstance(data, (dict, list)):
            st.json(data)
        elif data is not None and str(data).strip():
            st.info(str(data).strip())
        else:
            st.info("표시할 내용이 없습니다.")

# ──────────────────────────────────────────────────────────────────────────────
# (선택) 컨트롤 섹션: SIMS 컨텍스트 표시/리셋 버튼
# ──────────────────────────────────────────────────────────────────────────────
def render_sims_context_controls() -> None:
    """
    상단/사이드바 어디서든 호출 가능. 상태만 만지고 UI에 영향 최소화.
    """
    wire_chat_context()
    ss = st.session_state

    ns = str(
        ss.get("__sims_widget_ns")
        or ss.get("__sims_form_id")
        or ss.get("__sims_run_seq")
        or "0"
    )

    with st.expander("SIMS 컨텍스트", expanded=False):
        st.caption("최근 푸시 상태")
        st.write({"push_count": ss.get("__sims_push_count", 0)})

        def _clear_sims_only() -> None:
            # ✅ 채팅 히스토리/인박스는 건드리지 말고 SIMS 컨텍스트만 제거
            ss["__sims_last_push_sig"] = None
            ss["__sims_push_count"] = 0
            for k in (
                KEY_SIMS_CTX,
                "__sims_ctx", "__sims_ctx_hash", "__sims_ctx_dirty",
                "__sims_context", "__sims_context_text", "__sims_context_obj",
                "__sims_analysis_ctx", "__sims_latest_analysis_key",
            ):
                ss.pop(k, None)

        if st.button(
            "컨텍스트 리셋",
            use_container_width=True,
            key=f"__sims_ctx_reset_btn::{ns}",
        ):
            _clear_sims_only()
            st.success("SIMS 컨텍스트만 초기화했습니다.")
            
# ---------------------------------------------------------------------
# SSOT 호환: 외부(sims_panel/chat_bridge)에서 기대하는 이름을
# 실제 푸시 함수(wssz)로 확정 매핑
# ---------------------------------------------------------------------
push_sims_result_to_chat = wssz

def render_sims_chat_item(item: Dict[str, Any]) -> None:
    """
    Lmstudio_SSAI_chat_main.py의 저장된 SIMS table history 렌더용 공개 wrapper.

    기존 _render_chat_item_body()를 그대로 사용해서
    조회조건 / 헤더 / summary_md / 분석 스타일 / CSV / EXCEL / LLM 분석 버튼을 유지한다.
    """
    prev_force = bool(st.session_state.get("__chat_render_force_target", False))
    st.session_state["__chat_render_force_target"] = True

    try:
        _render_chat_item_body(item)
    finally:
        st.session_state["__chat_render_force_target"] = prev_force


__all__ = [
    "set_chat_render_anchor",
    "wire_chat_context",
    "drain_inbox_to_chat",
    "push_sims_result_to_chat",
    "render_sims_context_controls",
    "set_chat_render_target",
    "clear_chat_render_target",
    "render_pending_chat_items",
    "render_sims_chat_item",
    "clear_product_candidate_tables_from_chat",
]
