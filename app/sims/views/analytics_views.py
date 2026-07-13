# app/sims/views/analytics_views.py
# -*- coding: utf-8 -*-
# 카테고리
#    분석/KPI
# 작업선택
#    품목별 매출 추세 분석
#    품목별 매출 예상
#    품목별 재고부족현황

from __future__ import annotations

from typing import Any, Dict
import datetime as dt
import logging
import os
import pandas as pd
from app.db.mssql_client import read_df

import streamlit as st

from app.sims.views.rddbc_io_shared import _week_label_52
from app.services.analytics_sales_trend_service import (
    get_sales_trend_result,
    get_sales_trend_summary_result,
    get_sales_forecast_result,
    get_stock_shortage_result,
)
from app.services.analytics_manufacturer_sales_trend_service import (
    get_manufacturer_sales_trend_result,
    get_manufacturer_sales_trend_summary_result,
)
from app.services.analytics_customer_sales_forecast_service import (
    get_customer_sales_forecast_result,
)

log = logging.getLogger("ssai")


SHORTAGE_GRADE_OPTIONS = [
    "전체",
    "재고없음",
    "1개월내 부족",
    "2개월내 부족주의",
    "2개월내 부족",
    "3개월내 부족주의",
    "3개월내 부족",
    "정상",
    "수요관찰",
    "재고없음/수요없음",
]


def _ns() -> str:
    return str(st.session_state.get("__sims_widget_ns", "0"))


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _yyyymm_today() -> str:
    return dt.date.today().strftime("%Y%m")


def _yyyymm_start_of_year() -> str:
    return dt.date.today().strftime("%Y") + "01"

def _analytics_max_rows(default: int = 30000) -> int:
    """
    분석/KPI 조회 공통 상한.
    새 env를 만들지 않고 기존 SIMS_PANEL_DISPLAY_MAX_ROWS /
    SIMS_CHAT_DISPLAY_MAX_ROWS 값을 사용한다.
    """
    try:
        raw = (
            os.getenv("SIMS_PANEL_DISPLAY_MAX_ROWS")
            or os.getenv("SIMS_CHAT_DISPLAY_MAX_ROWS")
            or str(default)
        )
        v = int(str(raw or default).strip())
    except Exception:
        v = int(default)
    if v < 1:
        v = int(default)
    return v


# Rddbc010에서 업무코드 옵션을 가져오는 함수입니다. gcode에 해당하는 코드들을 불러와서 selectbox 옵션으로 사용할 수 있도록 리스트로 반환합니다.
@st.cache_data(ttl=300, show_spinner=False)
def _load_code_options(gcode: str) -> list[dict[str, str]]:
    """
    Rddbc010 업무코드 옵션.
    반환: [{"code": "", "name": "전체", "label": "전체"}, ...]
    """
    try:
        sql = """
        SELECT
            LTRIM(RTRIM(Rd01_Tcode)) AS code,
            LTRIM(RTRIM(Rd01_Hnm)) AS name
        FROM dbo.Rddbc010 WITH (NOLOCK)
        WHERE Rd01_Gcode = ?
          AND ISNULL(Rd01_Del_Flag, '') <> 'Y'
        ORDER BY Rd01_Tcode
        """
        df = read_df(sql, (gcode,))
    except Exception:
        log.exception("[analytics.views] failed to load code options gcode=%s", gcode)
        df = pd.DataFrame()

    opts = [{"code": "", "name": "", "label": "전체"}]

    if df is not None and not df.empty:
        for _, r in df.iterrows():
            code = str(r.get("code") or "").strip()
            name = str(r.get("name") or "").strip()
            if not code and not name:
                continue
            label = f"{code} - {name}" if name else code
            opts.append({"code": code, "name": name, "label": label})

    return opts

# Rddbc010에서 gcode에 해당하는 코드 옵션을 selectbox로 보여주고 선택된 옵션의 code/name/label을 반환하는 함수입니다.
# 예를 들어 gcode가 "SOME_CODE"라면 Rddbc010에서 Rd01_Gcode가 "SOME_CODE"인 레코드들을 불러와서 selectbox로 보여주고, 사용자가 선택한 옵션의 code/name/label을 딕셔너리로 반환합니다. 선택된 옵션이 없으면 {"code": "", "name": "", "label": "전체"}를 반환합니다.
# selectbox의 key는 함수 호출 시 전달되는 key 매개변수로 설정하여, 동일한 gcode라도 다른 selectbox로 사용할 수 있도록 합니다.
def _select_code_option(label: str, gcode: str, key: str) -> dict[str, str]:
    opts = _load_code_options(gcode)
    labels = [x["label"] for x in opts]

    selected_label = st.selectbox(
        label,
        labels,
        index=0,
        key=key,
    )

    for opt in opts:
        if opt["label"] == selected_label:
            return opt

    return {"code": "", "name": "", "label": "전체"}


def _select_code_options(label: str, gcode: str, key: str) -> list[dict[str, str]]:
    opts = _load_code_options(gcode)
    selectable = [x for x in opts if str(x.get("code") or "").strip()]
    labels = [x["label"] for x in selectable]

    selected_labels = st.multiselect(
        label,
        labels,
        default=[],
        key=key,
    )

    selected = [opt for opt in selectable if opt["label"] in set(selected_labels)]
    return selected


def _selected_codes(options: list[dict[str, str]]) -> list[str]:
    return [str(x.get("code") or "").strip() for x in options if str(x.get("code") or "").strip()]


def _selected_names(options: list[dict[str, str]]) -> list[str]:
    return [str(x.get("name") or "").strip() for x in options if str(x.get("name") or "").strip()]


def _join_selected_names(options: list[dict[str, str]]) -> str:
    return ", ".join(_selected_names(options))

# 날짜 값을 입력받아 YYYYMMDD 형식의 문자열로 반환하는 함수입니다. 입력값이 날짜 객체인 경우 strftime을 사용하여 변환하고, 그렇지 않은 경우 입력값에서 숫자만 추출하여 8자리 이상이면 앞 8자리까지 반환합니다. 유효한 날짜 형식이 아닌 경우 빈 문자열을 반환합니다.
# 예를 들어 입력값이 datetime.date(2025, 1, 1)이라면 "20250101"을 반환하고, 입력값이 "2025-01-01"이라면 "20250101"을 반환하며, 입력값이 "2025/01/01"이라면 "20250101"을 반환합니다. 입력값이 "invalid date"라면 ""를 반환합니다.

def _date_to_yyyymmdd(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y%m%d")
    s = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(s) >= 8:
        return s[:8]
    return ""

# 날짜 값을 입력받아 YYYYMM 형식의 문자열로 반환하는 함수입니다. 입력값이 날짜 객체인 경우 strftime을 사용하여 변환하고, 그렇지 않은 경우 입력값에서 숫자만 추출하여 6자리 이상이면 앞 6자리까지 반환합니다. 유효한 날짜 형식이 아닌 경우 빈 문자열을 반환합니다.
# 예를 들어 입력값이 datetime.date(2025, 1, 1)이라면 "202501"을 반환하고, 입력값이 "2025-01-01"이라면 "202501"을 반환하며, 입력값이 "2025/01/01"이라면 "202501"을 반환합니다. 입력값이 "invalid date"라면 ""를 반환합니다.  

def _date_to_yyyymm(value: Any) -> str:
    s = _date_to_yyyymmdd(value)
    if len(s) >= 6:
        return s[:6]
    return ""

# 올해 1월 1일 날짜 객체를 반환하는 함수입니다. 예를 들어 오늘이 2025년 8월 15일이라면 datetime.date(2025, 1, 1)을 반환합니다.
def _default_start_date() -> dt.date:
    today = dt.date.today()
    return dt.date(today.year, 1, 1)

# 오늘 날짜 객체를 반환하는 함수입니다. 예를 들어 오늘이 2025년 8월 15일이라면 datetime.date(2025, 8, 15)을 반환합니다.
def _default_end_date() -> dt.date:
    return dt.date.today()


def _render_date_input_with_week(label: str, value: dt.date, key: str) -> dt.date:
    d = st.date_input(
        label,
        value=value,
        format="YYYY-MM-DD",
        key=key,
    )
    st.caption(_week_label_52(d))
    return d


# 숫자 값을 입력받아 천 단위 구분 쉼표가 있는 문자열로 반환하는 함수입니다. 입력값이 정수인 경우 쉼표를 추가하여 반환하고, 실수인 경우 소수점 둘째 자리까지 표시하여 쉼표를 추가하여 반환합니다. 입력값이 숫자가 아닌 경우 "0"을 반환합니다.
# 예를 들어 입력값이 1234567이라면 "1,234,567"을 반환하고, 입력값이 12345.6789이라면 "12,345.68"을 반환하며, 입력값이 "not a number"라면 "0"을 반환합니다.
def _fmt_num(value: Any) -> str:
    try:
        v = float(value or 0)
        if v.is_integer():
            return f"{int(v):,}"
        return f"{v:,.2f}"
    except Exception:
        return "0"

def _metric_value(value: Any, unit: str = "", *, decimals: int | None = None) -> str:
    """
    숫자는 천단위 포맷, 문자는 그대로 표시.
    예: 112,080원 / 17개 / 5개월 / 월집계-장부재고(Rddbc220)
    """
    if decimals is None and unit == "원":
        decimals = 0

    if value is None:
        text = ""
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            text = ""
        else:
            try:
                n = float(s.replace(",", ""))
                if decimals is not None:
                    text = f"{n:,.{decimals}f}"
                elif n.is_integer():
                    text = f"{int(n):,}"
                else:
                    text = f"{n:,.2f}"
            except Exception:
                text = s
    else:
        if decimals is not None:
            try:
                text = f"{float(value or 0):,.{decimals}f}"
            except Exception:
                text = "0"
        else:
            text = _fmt_num(value)

    if not text:
        text = "-"

    if unit and text != "-":
        return f"{text}{unit}"

    return text

def _metric_card(label: str, value: Any, unit: str = "", bg: str = "#f8fafc", border: str = "#dbe4ee", decimals: int | None = None) -> None:
    """
    요약 헤더용 카드.
    한 줄 표시: 총매출액 : 112,080원
    """
    value_text = _metric_value(value, unit, decimals=decimals)

    html = (
        f'<div style="background:{bg}; border:1px solid {border}; border-radius:10px; '
        f'padding:9px 12px; min-height:42px; display:flex; align-items:center; '
        f'justify-content:space-between; gap:10px;">'
        f'<span style="font-size:13px; color:#64748b; font-weight:600; white-space:nowrap;">{label}</span>'
        f'<span style="font-size:16px; font-weight:750; color:#1f2937; line-height:1.2; '
        f'text-align:right; white-space:nowrap;">{value_text}</span>'
        f'</div>'
    )

    st.markdown(html, unsafe_allow_html=True)


def _count_card(label: str, count: Any, bg: str, border: str) -> None:
    _metric_card(label, count, "개", bg=bg, border=border)


def _color_for_trend(label: str) -> tuple[str, str]:
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


def _color_for_forecast(label: str) -> tuple[str, str]:
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

def _color_for_shortage(label: str) -> tuple[str, str]:
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


def _render_count_card_group(title: str, counts: Dict[str, Any], order: list[str], color_fn) -> None:
    """
    추세판정별/예상등급별 제품수 표시.
    예: 감소예상 604개 / 반품주의 277개 / 자료부족 92개
    """
    if not isinstance(counts, dict) or not counts:
        return

    keys = [k for k in order if k in counts]
    keys += [k for k in counts.keys() if k not in keys]

    st.markdown(f"### {title}")

    # 한 줄에 너무 많이 몰리지 않도록 6개씩 표시
    for start in range(0, len(keys), 6):
        row_keys = keys[start:start + 6]
        cols = st.columns(len(row_keys))

        for i, k in enumerate(row_keys):
            bg, border = color_fn(k)
            with cols[i]:
                _metric_card(str(k), counts.get(k, 0), "개", bg=bg, border=border)


def _fmt_yyyymm(value: Any) -> str:
    s = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(s) >= 6:
        return f"{s[:4]}-{s[4:6]}"
    return str(value or "").strip()


def _source_mode_label(source_mode: str) -> str:
    return {
        "auto": "자동",
        "monthly_book": "월집계-장부재고",
        "monthly_real": "월집계-실재고",
        "detail": "출고상세",
    }.get(str(source_mode or ""), str(source_mode or ""))

def _panel_result_target_chat_enabled() -> bool:
    """
    SIMS 패널 결과 표시 정책 확인.

    SIMS_PANEL_RESULT_TARGET=chat 계열이면:
    - 분석/KPI view 함수는 조회조건 UI와 payload 생성만 담당한다.
    - 매출예상요약/매출추세요약/재고부족요약은 패널에 직접 그리지 않는다.
    - 같은 meta를 chat_middleware가 채팅 메시지 안에서 렌더링한다.
    """
    try:
        import os
        target = str(os.getenv("SIMS_PANEL_RESULT_TARGET", "chat") or "chat").strip().lower()
        return target in {"chat", "chat_only", "chat-only", "1", "true", "yes", "y", "on"}
    except Exception:
        return True


def _render_inline_analysis_header_enabled() -> bool:
    """
    분석/KPI view 내부 요약 직접 렌더 허용 여부.

    장기 정책:
    - 패널은 조회조건 입력/조회 실행 전용
    - 결과 요약/표/다운로드는 채팅창 전용

    따라서 chat-only 모드에서는 view 함수가 매출예상요약 등을 직접 그리지 않는다.
    """
    return not _panel_result_target_chat_enabled()


# 매출 추세 분석의 조회조건과 메타 정보를 받아서 패널 상단에 보여줄 조회조건 요약 문자열을 생성하는 함수입니다.
# 예를 들어 source_mode가 "auto"이고 month_from이 "202501"이고 month_to가 "202512"이면 "분석자료원 자동 / 기준월 2025-01 ~ 2025-12"와 같은 문자열을 반환합니다.
#  제품코드, 제품명, 제조사명 등 다른 조건들도 포함하여 반환합니다.         
def _build_sales_trend_query_condition(params: Dict[str, Any], meta: Dict[str, Any] | None = None) -> str:
    meta = meta or {}
    bits: list[str] = []

    source_mode = params.get("source_mode") or meta.get("source_mode") or ""
    if source_mode:
        bits.append(f"분석자료원 {_source_mode_label(source_mode)}")

    df = _clean_text(params.get("date_from"))
    dt_to = _clean_text(params.get("date_to"))

    if df or dt_to:
        def _fmt_yyyymmdd(v: Any) -> str:
            s = "".join(ch for ch in str(v or "") if ch.isdigit())
            if len(s) >= 8:
                return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
            return str(v or "").strip()

        if df and dt_to and df != dt_to:
            bits.append(f"기간 {_fmt_yyyymmdd(df)} ~ {_fmt_yyyymmdd(dt_to)}")
        elif df:
            bits.append(f"기간 {_fmt_yyyymmdd(df)}")
        elif dt_to:
            bits.append(f"기간 {_fmt_yyyymmdd(dt_to)}")
    else:
        mf = _clean_text(params.get("month_from"))
        mt = _clean_text(params.get("month_to"))

        if mf and mt and mf != mt:
            bits.append(f"기준월 {_fmt_yyyymm(mf)} ~ {_fmt_yyyymm(mt)}")
        elif mf:
            bits.append(f"기준월 {_fmt_yyyymm(mf)}")
        elif mt:
            bits.append(f"기준월 {_fmt_yyyymm(mt)}")

    if _clean_text(params.get("physic_cd")):
        bits.append(f"제품 {params.get('physic_cd')}")
    if _clean_text(params.get("physic_nm")):
        bits.append(f"제품명 {params.get('physic_nm')}")
    if _clean_text(params.get("product_ven_nm")):
        bits.append(f"제조사명 {params.get('product_ven_nm')}")
    if _clean_text(params.get("product_group_nm")):
        bits.append(f"제품그룹명 {params.get('product_group_nm')}")
    product_di_names = params.get("product_di_nm_list")
    if isinstance(product_di_names, list) and product_di_names:
        bits.append(f"제품구분명 {', '.join(str(x) for x in product_di_names if str(x).strip())}")
    elif _clean_text(params.get("product_di_nm")):
        bits.append(f"제품구분명 {params.get('product_di_nm')}")
    product_class_names = params.get("product_class_nm_list")
    if isinstance(product_class_names, list) and product_class_names:
        bits.append(f"제품분류명 {', '.join(str(x) for x in product_class_names if str(x).strip())}")
    elif _clean_text(params.get("product_class_nm")):
        bits.append(f"제품분류명 {params.get('product_class_nm')}")
    stock_names = params.get("stock_nm_list")
    if isinstance(stock_names, list) and stock_names:
        bits.append(f"재고위치 {', '.join(str(x) for x in stock_names if str(x).strip())}")
    elif _clean_text(params.get("stock_nm")):
        bits.append(f"재고위치 {params.get('stock_nm')}")
    if _clean_text(params.get("ven_nm")):
        bits.append(f"거래처명 {params.get('ven_nm')}")
    if _clean_text(params.get("buy_nm")):
        bits.append(f"매입처명 {params.get('buy_nm')}")
    if _clean_text(params.get("sales_man_nm")):
        bits.append(f"영업사원명 {params.get('sales_man_nm')}")
    if _clean_text(params.get("sido_nm")):
        bits.append(f"시도명 {params.get('sido_nm')}")
    if _clean_text(params.get("gugun_nm")):
        bits.append(f"시구군명 {params.get('gugun_nm')}")
    if _clean_text(params.get("road_nm")):
        bits.append(f"도로명 {params.get('road_nm')}")
    if _clean_text(params.get("trend_judge")):
        bits.append(f"추세판정 {params.get('trend_judge')}")
    if _clean_text(params.get("shortage_grade")):
        bits.append(f"부족등급 {params.get('shortage_grade')}")

    # top은 내부 조회 상한/표시 정책용 값이므로 조회조건 문구에는 노출하지 않는다.
    return " / ".join(bits)

# 매출 추세 분석의 조회조건과 메타 정보를 받아서 패널 상단에 보여줄 조회조건 요약 딕셔너리를 생성하는 함수입니다. 
# 예를 들어 source_mode가 "auto"이고 month_from이 "202501"이고 month_to가 "202512"이면 {"분석자료원": "자동", "기준월": "2025-01 ~ 2025-12"}와 같은 딕셔너리를 반환합니다.
# 제품코드, 제품명, 제조사명 등 다른 조건들도 포함하여 반환합니다.
def _build_sales_trend_display_params(params: Dict[str, Any], meta: Dict[str, Any] | None = None) -> Dict[str, Any]:
    meta = meta or {}
    out: Dict[str, Any] = {}

    source_mode = params.get("source_mode") or meta.get("source_mode") or ""
    if source_mode:
        out["분석자료원"] = _source_mode_label(source_mode)

    df = _clean_text(params.get("date_from"))
    dt_to = _clean_text(params.get("date_to"))

    if df or dt_to:
        def _fmt_yyyymmdd(v: Any) -> str:
            s = "".join(ch for ch in str(v or "") if ch.isdigit())
            if len(s) >= 8:
                return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
            return str(v or "").strip()

        if df and dt_to and df != dt_to:
            out["기간"] = f"{_fmt_yyyymmdd(df)} ~ {_fmt_yyyymmdd(dt_to)}"
        elif df:
            out["기간"] = _fmt_yyyymmdd(df)
        elif dt_to:
            out["기간"] = _fmt_yyyymmdd(dt_to)
    else:
        mf = _clean_text(params.get("month_from"))
        mt = _clean_text(params.get("month_to"))
        if mf and mt and mf != mt:
            out["기준월"] = f"{_fmt_yyyymm(mf)} ~ {_fmt_yyyymm(mt)}"
        elif mf:
            out["기준월"] = _fmt_yyyymm(mf)
        elif mt:
            out["기준월"] = _fmt_yyyymm(mt)

    mapping = [
        ("physic_cd", "제품코드"),
        ("physic_nm", "제품명"),
        ("product_ven_nm", "제조사명"),
        ("product_group_nm", "제품그룹명"),
        ("product_di_nm", "제품구분명"),
        ("product_class_nm", "제품분류명"),
        ("ven_cd", "매출처코드"),
        ("ven_nm", "거래처명"),
        ("buy_nm", "매입처명"),
        ("sales_man_nm", "영업사원명"),
        ("sido_nm", "시도명"),
        ("gugun_nm", "시구군명"),
        ("road_nm", "도로명"),
        ("trend_judge", "추세판정"),        
        ("forecast_grade", "예상등급"),
        ("shortage_grade", "부족등급"),
        ("top", "Top"),
    ]

    for key, label in mapping:
        v = _clean_text(params.get(key))
        if v:
            out[label] = v

    list_mapping = [
        ("product_di_nm_list", "제품구분명"),
        ("product_class_nm_list", "제품분류명"),
        ("stock_nm_list", "재고위치"),
    ]
    for key, label in list_mapping:
        vals = params.get(key)
        if isinstance(vals, list):
            text = ", ".join(str(x).strip() for x in vals if str(x).strip())
            if text:
                out[label] = text

    return out


def _render_sales_trend_panel_header(meta: Dict[str, Any], query_condition: str) -> None:
    if not isinstance(meta, dict):
        meta = {}

    analysis_type = str(meta.get("analysis_type") or "").strip()
    summary_type = str(meta.get("summary_type") or "").strip()
    is_forecast = analysis_type == "sales_forecast" or summary_type == "product_forecast"

    source_label = str(meta.get("source_label") or _source_mode_label(meta.get("source_mode") or ""))

    current_progress = meta.get("current_month_progress_pct")
    if current_progress is None:
        current_expected = float(meta.get("sum_current_month_expected_amt") or 0)
        current_sales = float(meta.get("sum_current_month_sales_amt") or 0)
        current_progress = (current_sales / current_expected * 100) if abs(current_expected) >= 1e-12 else 0

    if query_condition:
        st.caption(f"조회조건: {query_condition}")

    if is_forecast:
        st.markdown("### 당월 매출예상 요약")

        c1, c2, c3, c4, c5 = st.columns(5)
        with c1:
            _metric_card("완료월평균매출", meta.get("avg_completed_month_sales_amt"), "원", bg="#f8fafc", border="#dbe4ee")
        with c2:
            _metric_card("당월 현재매출", meta.get("sum_current_month_sales_amt"), "원", bg="#f8fafc", border="#dbe4ee")
        with c3:
            _metric_card("당월 예상매출", meta.get("sum_current_month_expected_amt"), "원", bg="#fff7ed", border="#fed7aa")
        with c4:
            _metric_card("당월 잔여예상", meta.get("sum_current_month_remaining_expected_amt"), "원", bg="#fff7ed", border="#fed7aa")
        with c5:
            _metric_card("당월 진척률", current_progress, "%", bg="#f0fdf4", border="#bbf7d0", decimals=2)

        st.markdown("### 중장기 예상")

        f1, f2, f3, f4 = st.columns(4)
        with f1:
            _metric_card("총매출액", meta.get("sum_sales_amt"), "원", bg="#f8fafc", border="#dbe4ee")
        with f2:
            _metric_card("다음월예상매출", meta.get("sum_next_month_forecast_amt"), "원", bg="#fff7ed", border="#fed7aa")
        with f3:
            _metric_card("3개월예상매출", meta.get("sum_3month_forecast_amt"), "원", bg="#fff7ed", border="#fed7aa")
        with f4:
            _metric_card("6개월예상매출", meta.get("sum_6month_forecast_amt"), "원", bg="#fff7ed", border="#fed7aa")

        c5, c6, c7, c8, c9, c10 = st.columns(6)
        with c5:
            _metric_card("출고수량", meta.get("sum_qty"), "개", bg="#f0fdf4", border="#bbf7d0")
        with c6:
            _metric_card("품목수", meta.get("product_count"), "개", bg="#eff6ff", border="#bfdbfe")
        with c7:
            _metric_card(str(meta.get("customer_count_label") or "거래처수"), meta.get("customer_count"), "개", bg="#eff6ff", border="#bfdbfe")
        with c8:
            _metric_card("분석월수", meta.get("month_count"), "개월", bg="#f5f3ff", border="#ddd6fe")
        with c9:
            _metric_card("완료월수", meta.get("completed_month_count"), "개월", bg="#f5f3ff", border="#ddd6fe")
        with c10:
            _metric_card("자료원", source_label, "", bg="#f8fafc", border="#dbe4ee")

    else:
        st.markdown("### 매출추세요약")

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            _metric_card("총매출액", meta.get("sum_sales_amt"), "원", bg="#f8fafc", border="#dbe4ee")
        with c2:
            _metric_card("매출공급가액", meta.get("sum_supply_amt"), "원", bg="#f8fafc", border="#dbe4ee")
        with c3:
            _metric_card("매출세액", meta.get("sum_tax_amt"), "원", bg="#f8fafc", border="#dbe4ee")
        with c4:
            _metric_card("출고수량", meta.get("sum_qty"), "개", bg="#f0fdf4", border="#bbf7d0")

        c5, c6, c7, c8, c9 = st.columns(5)
        with c5:
            _metric_card("품목수", meta.get("product_count"), "개", bg="#eff6ff", border="#bfdbfe")
        with c6:
            _metric_card(str(meta.get("customer_count_label") or "거래처수"), meta.get("customer_count"), "개", bg="#eff6ff", border="#bfdbfe")
        with c7:
            _metric_card("분석월수", meta.get("month_count"), "개월", bg="#f5f3ff", border="#ddd6fe")
        with c8:
            _metric_card("완료월수", meta.get("completed_month_count"), "개월", bg="#f5f3ff", border="#ddd6fe")
        with c9:
            _metric_card("자료원", source_label, "", bg="#f8fafc", border="#dbe4ee")

    if not is_forecast:
        st.markdown("### 당월 진행 요약")

        c10, c11, c12, c13, c14, c15 = st.columns(6)
        with c10:
            _metric_card("완료월수", meta.get("completed_month_count"), "개월", bg="#f5f3ff", border="#ddd6fe")
        with c11:
            _metric_card("완료월평균매출", meta.get("avg_completed_month_sales_amt"), "원", bg="#f8fafc", border="#dbe4ee")
        with c12:
            _metric_card("당월 현재매출", meta.get("sum_current_month_sales_amt"), "원", bg="#f8fafc", border="#dbe4ee")
        with c13:
            _metric_card("당월 예상매출", meta.get("sum_current_month_expected_amt"), "원", bg="#fff7ed", border="#fed7aa")
        with c14:
            _metric_card("당월 잔여예상", meta.get("sum_current_month_remaining_expected_amt"), "원", bg="#fff7ed", border="#fed7aa")
        with c15:
            _metric_card("당월 진척률", current_progress, "%", bg="#f0fdf4", border="#bbf7d0", decimals=2)

    trend_counts = meta.get("trend_judge_counts") or {}
    _render_count_card_group(
        "추세판정별 제품수",
        trend_counts,
        ["증가", "감소", "안정", "반품주의", "신규/증가", "자료부족", "미분류"],
        _color_for_trend,
    )

    forecast_counts = meta.get("forecast_grade_counts") or {}
    _render_count_card_group(
        "예상등급별 제품수",
        forecast_counts,
        ["상승예상", "감소예상", "안정예상", "신규확인", "반품주의", "자료부족", "미분류"],
        _color_for_forecast,
    )

def _render_stock_shortage_panel_header(meta: Dict[str, Any], query_condition: str) -> None:
    if not isinstance(meta, dict):
        meta = {}

    source_label = str(meta.get("source_label") or _source_mode_label(meta.get("source_mode") or ""))
    stock_label = str(meta.get("stock_label") or "장부재고")

    if query_condition:
        st.caption(f"조회조건: {query_condition}")

    st.markdown("### 재고부족요약")

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        _metric_card("품목수", meta.get("product_count"), "개", bg="#eff6ff", border="#bfdbfe")
    with c2:
        _metric_card("부족품목수", meta.get("shortage_item_count"), "개", bg="#fff1f2", border="#fecdd3")
    with c3:
        _metric_card("현재재고수량", meta.get("sum_current_stock_qty"), "개", bg="#f0fdf4", border="#bbf7d0")
    with c4:
        _metric_card("현재재고금액", meta.get("sum_current_stock_amt"), "원", bg="#f8fafc", border="#dbe4ee")

    c5, c6, c7, c8 = st.columns(4)
    with c5:
        _metric_card("평가월 예상수요", meta.get("sum_current_month_expected_out_qty"), "개", bg="#f5f3ff", border="#ddd6fe", decimals=2)
    with c6:
        _metric_card("평가월 실제수요", meta.get("sum_current_month_out_qty"), "개", bg="#eff6ff", border="#bfdbfe", decimals=2)
    with c7:
        _metric_card("평가월 잔여예상수요", meta.get("sum_current_month_remaining_out_qty"), "개", bg="#fff7ed", border="#fed7aa", decimals=2)
    with c8:
        _metric_card("평가월 수요진척률", meta.get("current_month_demand_progress_pct"), "%", bg="#f0fdf4", border="#bbf7d0", decimals=2)

    c9, c10, c11, c12 = st.columns(4)
    with c9:
        _metric_card("부족예상수량", meta.get("sum_expected_shortage_qty"), "개", bg="#fff1f2", border="#fecdd3", decimals=2)
    with c10:
        _metric_card("부족예상금액", meta.get("sum_expected_shortage_amt"), "원", bg="#fff1f2", border="#fecdd3")
    with c11:
        _metric_card("재고충족률", meta.get("overall_stock_fill_rate"), "%", bg="#f8fafc", border="#dbe4ee", decimals=2)
    with c12:
        _metric_card("재고기준", stock_label, "", bg="#f5f3ff", border="#ddd6fe")

    stock_source_label = str(meta.get("stock_source_label") or "")

    c13, c14, c15 = st.columns(3)
    with c13:
        _metric_card("1개월부족수량", meta.get("sum_shortage_1m_qty"), "개", bg="#fff1f2", border="#fecdd3", decimals=2)
    with c14:
        _metric_card("2개월부족수량", meta.get("sum_shortage_2m_qty"), "개", bg="#fff7ed", border="#fed7aa", decimals=2)
    with c15:
        _metric_card("3개월부족수량", meta.get("sum_shortage_3m_qty"), "개", bg="#fff7ed", border="#fed7aa", decimals=2)

    try:
        row_count_text = f"{int(meta.get('row_count_total') or meta.get('row_count') or 0):,}"
    except Exception:
        row_count_text = str(meta.get("row_count_total") or meta.get("row_count") or 0)
    st.caption(
        f"자료원: {source_label} / 현재고원천: {stock_source_label or stock_label} / "
        f"현재고기준월: {meta.get('stock_cutoff_month') or ''} / "
        f"조회건수: {row_count_text}건"
    )

    _render_count_card_group(
        "재고부족판정별 제품수",
        meta.get("stock_shortage_judge_counts") or meta.get("shortage_grade_counts") or {},
        [
            "부족",
            "주의",
            "적정",
            "수요없음",
            "미분류",
        ],
        _color_for_shortage,
    )


def _manufacturer_progress_title(meta: Dict[str, Any]) -> str:
    return str(meta.get("current_progress_title") or ("당월 진행 요약" if meta.get("evaluation_mode") == "current_monthly" else "평가월 진행 요약"))


def _render_manufacturer_sales_summary_header(meta: Dict[str, Any], query_condition: str) -> None:
    if not isinstance(meta, dict):
        meta = {}
    show_extended = (
        meta.get("analysis_type") == "manufacturer_sales_trend_summary"
        or meta.get("summary_type") == "manufacturer_trend_summary"
    )
    if query_condition:
        st.caption(f"조회조건: {query_condition}")

    st.markdown("### 매출추세요약")
    if meta.get("period_caption"):
        st.caption(str(meta.get("period_caption")))
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        _metric_card("총매출액", meta.get("sum_sales_amt"), "원", bg="#f8fafc", border="#dbe4ee")
    with c2:
        _metric_card("매출공급가액", meta.get("sum_supply_amt"), "원", bg="#f8fafc", border="#dbe4ee")
    with c3:
        _metric_card("매출세액", meta.get("sum_tax_amt"), "원", bg="#f8fafc", border="#dbe4ee")
    with c4:
        _metric_card("제약사수", meta.get("manufacturer_count") or meta.get("group_count"), "개", bg="#eff6ff", border="#bfdbfe")

    c5, c6, c7, c8 = st.columns(4)
    with c5:
        _metric_card("제품수", meta.get("product_count"), "개", bg="#eff6ff", border="#bfdbfe")
    with c6:
        _metric_card("매입처수", meta.get("buy_vendor_count") or meta.get("purchase_vendor_count"), "개", bg="#eff6ff", border="#bfdbfe")
    with c7:
        _metric_card("분석월수", meta.get("month_count"), "개월", bg="#f5f3ff", border="#ddd6fe")
    with c8:
        _metric_card("자료원", meta.get("source_label") or "-", "", bg="#f8fafc", border="#dbe4ee")

    if not show_extended:
        st.caption(
            f"평가월: {meta.get('evaluation_month') or '-'} / "
            f"자료원: {meta.get('source_label') or '-'} / "
            f"조회건수: {meta.get('row_count_total') or meta.get('row_count') or 0}건"
        )
        return

    st.markdown(f"### {_manufacturer_progress_title(meta)}")
    p1, p2, p3, p4, p5, p6 = st.columns(6)
    with p1:
        _metric_card("완료월수", meta.get("completed_month_count"), "개월", bg="#f5f3ff", border="#ddd6fe")
    with p2:
        _metric_card("완료월평균매출", meta.get("avg_completed_month_sales_amt"), "원", bg="#f8fafc", border="#dbe4ee")
    with p3:
        _metric_card(str(meta.get("current_sales_label") or "당월 현재매출"), meta.get("sum_current_month_sales_amt"), "원", bg="#f8fafc", border="#dbe4ee")
    with p4:
        _metric_card(str(meta.get("current_expected_label") or "당월 예상매출"), meta.get("sum_current_month_expected_amt"), "원", bg="#fff7ed", border="#fed7aa")
    with p5:
        _metric_card(str(meta.get("current_remaining_label") or "당월 잔여예상"), meta.get("sum_current_month_remaining_expected_amt"), "원", bg="#fff7ed", border="#fed7aa")
    with p6:
        _metric_card(str(meta.get("current_progress_label") or "당월 진척률"), meta.get("current_month_progress_pct"), "%", bg="#f0fdf4", border="#bbf7d0", decimals=2)

    trend_counts = meta.get("trend_judge_counts") or {}
    st.markdown("### 추세판정별 제약사수")
    j1, j2, j3, j4 = st.columns(4)
    with j1:
        _metric_card("증가", trend_counts.get("증가", 0), "개", bg="#ecfdf5", border="#bbf7d0")
    with j2:
        _metric_card("감소", trend_counts.get("감소", 0), "개", bg="#fff1f2", border="#fecdd3")
    with j3:
        _metric_card("안정", trend_counts.get("안정", 0), "개", bg="#eff6ff", border="#bfdbfe")
    with j4:
        _metric_card("자료부족", trend_counts.get("자료부족", 0), "개", bg="#f8fafc", border="#dbe4ee")

    st.caption(
        f"평가월: {meta.get('evaluation_month') or '-'} / "
        f"자료원: {meta.get('source_label') or '-'} / "
        f"조회건수: {meta.get('row_count_total') or meta.get('row_count') or 0}건"
    )


def _render_manufacturer_sales_trend_panel_header(meta: Dict[str, Any], query_condition: str) -> None:
    _render_manufacturer_sales_summary_header(meta, query_condition)


def _render_manufacturer_sales_trend_summary_panel_header(meta: Dict[str, Any], query_condition: str) -> None:
    _render_manufacturer_sales_summary_header(meta, query_condition)


def _render_customer_sales_forecast_panel_header(meta: Dict[str, Any], query_condition: str) -> None:
    if not isinstance(meta, dict):
        meta = {}
    if query_condition:
        st.caption(f"조회조건: {query_condition}")

    current_progress = meta.get("current_month_progress_pct")
    if current_progress is None:
        expected = float(meta.get("sum_current_month_expected_amt") or 0)
        actual = float(meta.get("sum_current_month_sales_amt") or 0)
        current_progress = (actual / expected * 100) if abs(expected) >= 1e-12 else 0

    st.markdown(f"### {meta.get('current_progress_title') or '당월 매출예상 요약'}")
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        _metric_card("완료월평균매출", meta.get("avg_completed_month_sales_amt"), "원", bg="#f8fafc", border="#dbe4ee")
    with c2:
        _metric_card(str(meta.get("current_sales_label") or "당월 현재매출"), meta.get("sum_current_month_sales_amt"), "원", bg="#f8fafc", border="#dbe4ee")
    with c3:
        _metric_card(str(meta.get("current_expected_label") or "당월 예상매출"), meta.get("sum_current_month_expected_amt"), "원", bg="#fff7ed", border="#fed7aa")
    with c4:
        _metric_card(str(meta.get("current_remaining_label") or "당월 잔여예상"), meta.get("sum_current_month_remaining_expected_amt"), "원", bg="#fff7ed", border="#fed7aa")
    with c5:
        _metric_card(str(meta.get("current_progress_label") or "당월 진척률"), current_progress, "%", bg="#f0fdf4", border="#bbf7d0", decimals=2)

    st.markdown("### 중장기 예상")
    f1, f2, f3, f4 = st.columns(4)
    with f1:
        _metric_card("총매출액", meta.get("sum_sales_amt"), "원", bg="#f8fafc", border="#dbe4ee")
    with f2:
        _metric_card("다음월예상매출", meta.get("sum_next_month_forecast_amt"), "원", bg="#fff7ed", border="#fed7aa")
    with f3:
        _metric_card("3개월예상매출", meta.get("sum_3month_forecast_amt"), "원", bg="#fff7ed", border="#fed7aa")
    with f4:
        _metric_card("6개월예상매출", meta.get("sum_6month_forecast_amt"), "원", bg="#fff7ed", border="#fed7aa")

    c6, c7, c8, c9 = st.columns(4)
    with c6:
        _metric_card("매출처수", meta.get("customer_count"), "개", bg="#eff6ff", border="#bfdbfe")
    with c7:
        _metric_card("영업사원수", meta.get("salesperson_count"), "명", bg="#eff6ff", border="#bfdbfe")
    with c8:
        _metric_card("지역수", meta.get("region_count"), "개", bg="#eff6ff", border="#bfdbfe")
    with c9:
        _metric_card("분석월수", meta.get("month_count"), "개월", bg="#f5f3ff", border="#ddd6fe")
    _metric_card("자료원", meta.get("source_label") or "-", "", bg="#f8fafc", border="#dbe4ee")

    _render_count_card_group(
        "추세판정별 매출처수",
        meta.get("trend_judge_counts") or {},
        ["증가", "감소", "안정", "자료부족"],
        _color_for_trend,
    )
    _render_count_card_group(
        "예상등급별 매출처수",
        meta.get("forecast_grade_counts") or {},
        ["상승예상", "감소예상", "안정예상", "신규확인", "반품주의", "자료부족", "미분류"],
        _color_for_forecast,
    )


def render_sales_trend_analysis() -> Dict[str, Any]:
    st.subheader("품목별 매출 추세 분석")
    st.caption("Rddbc120 출고명세 기준으로 월별 품목 매출 추세를 분석합니다.")

    ns = _ns()

    with st.form(
        key=f"__analytics_sales_trend_form__{ns}",
        clear_on_submit=False,
        enter_to_submit=False,
    ):
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            source_label = st.selectbox(
                "분석자료원",
                [
                    "자동",
                    "월집계-장부재고",
                    "월집계-실재고",
                    "출고상세",
                ],
                index=0,
                key=f"__analytics_sales_trend_source__{ns}",
            )

        with c2:
            date_from = _render_date_input_with_week(
                "시작일자",
                _default_start_date(),
                f"__analytics_sales_trend_date_from__{ns}",
            )
        with c3:
            date_to = _render_date_input_with_week(
                "종료일자",
                _default_end_date(),
                f"__analytics_sales_trend_date_to__{ns}",
            )
        with c4:
            trend_judge = st.selectbox(
                "추세판정",
                ["전체", "감소", "안정", "증가", "신규/증가", "자료부족", "반품주의"],
                index=0,
                key=f"__analytics_sales_trend_judge__{ns}",
            )
        top = _analytics_max_rows()

        c4, c5, c6 = st.columns(3)
        with c4:
            physic_cd = st.text_input(
                "제품코드",
                value="",
                key=f"__analytics_sales_trend_physic_cd__{ns}",
            )
        with c5:
            physic_nm = st.text_input(
                "제품명",
                value="",
                key=f"__analytics_sales_trend_physic_nm__{ns}",
            )
        with c6:
            product_ven_nm = st.text_input(
                "제조사명",
                value="",
                key=f"__analytics_sales_trend_product_ven_nm__{ns}",
            )

        c7, c8, c9, c10 = st.columns(4)
        with c7:
            product_group_opt = _select_code_option(
                "제품그룹명",
                "0013",
                key=f"__analytics_sales_trend_product_group__{ns}",
            )
        with c8:
            product_di_opts = _select_code_options(
                "제품구분명",
                "0004",
                key=f"__analytics_sales_trend_product_di__{ns}",
            )
        with c9:
            product_class_opts = _select_code_options(
                "제품분류명",
                "0028",
                key=f"__analytics_sales_trend_product_class__{ns}",
            )
        with c10:
            stock_opts = _select_code_options(
                "재고위치",
                "0018",
                key=f"__analytics_sales_trend_stock__{ns}",
            )

        c11, c12, c13 = st.columns(3)
        with c11:
            ven_nm = st.text_input(
                "거래처명",
                value="",
                key=f"__analytics_sales_trend_ven_nm__{ns}",
            )
        with c12:
            buy_nm = st.text_input(
                "매입처명",
                value="",
                key=f"__analytics_sales_trend_buy_nm__{ns}",
            )
        with c13:
            sales_man_nm = st.text_input(
                "영업사원명",
                value="",
                key=f"__analytics_sales_trend_sales_man_nm__{ns}",
            )

        c14, c15, c16 = st.columns(3)
        with c14:
            sido_nm = st.text_input(
                "시도명",
                value="",
                key=f"__analytics_sales_trend_sido_nm__{ns}",
                placeholder="예: 서울",
            )
        with c15:
            gugun_nm = st.text_input(
                "시구군명",
                value="",
                key=f"__analytics_sales_trend_gugun_nm__{ns}",
                placeholder="예: 강남",
            )
        with c16:
            road_nm = st.text_input(
                "도로명",
                value="",
                key=f"__analytics_sales_trend_road_nm__{ns}",
                placeholder="예: 테헤란로",
            )

        submitted = st.form_submit_button("조회", type="primary", use_container_width=True)

    if not submitted:
        return {
            "final": False,
            "type": "text",
            "title": "품목별 매출 추세 분석",
            "data": "[조회] 버튼을 눌러 실행하세요.",
        }


    source_mode = {
        "자동": "auto",
        "월집계-장부재고": "monthly_book",
        "월집계-실재고": "monthly_real",
        "출고상세": "detail",
    }.get(source_label, "auto")

    date_from_text = _date_to_yyyymmdd(date_from)
    date_to_text = _date_to_yyyymmdd(date_to)

    params = {
        "source_mode": source_mode,

        "date_from": date_from_text,
        "date_to": date_to_text,
        "month_from": _date_to_yyyymm(date_from),
        "month_to": _date_to_yyyymm(date_to),

        "physic_cd": _clean_text(physic_cd),
        "physic_nm": _clean_text(physic_nm),
        "product_ven_nm": _clean_text(product_ven_nm),

        "product_group": product_group_opt.get("code", ""),
        "product_group_nm": product_group_opt.get("name", ""),
        "product_di": "",
        "product_di_nm": "",
        "product_di_list": _selected_codes(product_di_opts),
        "product_di_nm_list": _selected_names(product_di_opts),
        "product_class": "",
        "product_class_nm": "",
        "product_class_list": _selected_codes(product_class_opts),
        "product_class_nm_list": _selected_names(product_class_opts),
        "stock_cd": "",
        "stock_nm": "",
        "stock_cd_list": _selected_codes(stock_opts),
        "stock_nm_list": _selected_names(stock_opts),

        "ven_nm": _clean_text(ven_nm),
        "buy_nm": _clean_text(buy_nm),
        "sales_man_nm": _clean_text(sales_man_nm),
        "sido_nm": _clean_text(sido_nm),
        "gugun_nm": _clean_text(gugun_nm),
        "road_nm": _clean_text(road_nm),
        "trend_judge": "" if trend_judge == "전체" else trend_judge,

        "top": int(top),
    }

    try:

        result = get_sales_trend_result(params)

        meta = dict(result.get("meta") or {})
        meta.setdefault("analytics", True)
        meta.setdefault("analysis_type", "sales_trend")
        meta.setdefault("sales_trend_summary", True)

        query_condition = _build_sales_trend_query_condition(params, meta)
        if query_condition:
            meta["query_summary"] = query_condition
            meta["condition"] = query_condition

        # 패널 기본 조회조건이 source_mode/month_from 식으로 나오지 않도록 한글 params로 교체
        result["params_raw"] = params
        result["params"] = _build_sales_trend_display_params(params, meta)

        source_label = str(meta.get("source_label") or _source_mode_label(params.get("source_mode")))

        if not _clean_text(meta.get("summary_md")):
            source_label = str(meta.get("source_label") or _source_mode_label(params.get("source_mode")))
            meta["summary_md"] = (
                f"매출추세요약: "
                f"조회조건 {query_condition} / "
                f"총매출액 {_fmt_num(meta.get('sum_sales_amt'))} / "
                f"출고수량 {_fmt_num(meta.get('sum_qty'))} / "
                f"품목수 {_fmt_num(meta.get('product_count'))} / "
                f"거래처수 {_fmt_num(meta.get('customer_count'))} / "
                f"분석월수 {_fmt_num(meta.get('month_count'))} / "
                f"자료원 {source_label}"
            )

        result["meta"] = meta

        # chat-only 모드에서는 매출추세요약/매출예상요약을 패널에 직접 그리지 않는다.
        # 같은 meta는 채팅 메시지 렌더러(chat_middleware)가 표시한다.
        if (
            _render_inline_analysis_header_enabled()
            and int(meta.get("row_count_total") or meta.get("row_count") or 0) > 0
        ):
            _render_sales_trend_panel_header(meta, query_condition)

        return result

    except Exception as e:
        log.exception("[analytics.views] sales trend failed")
        return {
            "final": True,
            "type": "text",
            "title": "품목별 매출 추세 분석 오류",
            "action": "품목별 매출 추세 분석",
            "params": _build_sales_trend_display_params(params, {}),
            "params_raw": params,
            "data": str(e),
            "message": str(e),
            "meta": {
                "row_count": 0,
                "row_count_total": 0,
                "analytics": True,
                "analysis_type": "sales_trend",
            },
        }
# 매출 추세 분석 결과에서 품목별 매출 추세 요약표를 보여주는 함수입니다. 
# 매출 추세 분석과 유사한 조회조건을 입력받아서, 품목별 매출 추세 분석 결과에서 제품 1줄 단위로 요약된 데이터를 반환합니다. 
# 이 데이터는 SIMS 패널에서 표 형태로 보여질 수 있습니다.

def render_sales_trend_summary_analysis() -> Dict[str, Any]:
    st.subheader("품목별 매출 추세 요약표")
    st.caption("품목별 매출 추세 분석 결과를 제품 1줄 단위로 요약합니다.")

    ns = _ns()

    with st.form(
        key=f"__analytics_sales_trend_summary_form__{ns}",
        clear_on_submit=False,
        enter_to_submit=False,
    ):


        c1, c2, c3, c4 = st.columns(4)
        with c1:
            source_label = st.selectbox(
                "분석자료원",
                [
                    "자동",
                    "월집계-장부재고",
                    "월집계-실재고",
                    "출고상세",
                ],
                index=0,
                key=f"__analytics_sales_trend_summary_source__{ns}",
            )
        with c2:
            date_from = _render_date_input_with_week(
                "시작일자",
                _default_start_date(),
                f"__analytics_sales_trend_summary_date_from__{ns}",
            )
        with c3:
            date_to = _render_date_input_with_week(
                "종료일자",
                _default_end_date(),
                f"__analytics_sales_trend_summary_date_to__{ns}",
            )
        with c4:
            trend_judge = st.selectbox(
                "추세판정",
                ["전체", "감소", "안정", "증가", "신규/증가", "자료부족", "반품주의"],
                index=0,
                key=f"__analytics_sales_trend_summary_judge__{ns}",
            )
        top = _analytics_max_rows()


        c5, c6, c7 = st.columns(3)
        with c5:
            physic_cd = st.text_input(
                "제품코드",
                value="",
                key=f"__analytics_sales_trend_summary_physic_cd__{ns}",
            )
        with c6:
            physic_nm = st.text_input(
                "제품명",
                value="",
                key=f"__analytics_sales_trend_summary_physic_nm__{ns}",
            )
        with c7:
            product_ven_nm = st.text_input(
                "제조사명",
                value="",
                key=f"__analytics_sales_trend_summary_product_ven_nm__{ns}",
            )


        c8, c9, c10, c11 = st.columns(4)
        with c8:
            product_group_opt = _select_code_option(
                "제품그룹명",
                "0013",
                key=f"__analytics_sales_trend_summary_product_group__{ns}",
            )
        with c9:
            product_di_opts = _select_code_options(
                "제품구분명",
                "0004",
                key=f"__analytics_sales_trend_summary_product_di__{ns}",
            )
        with c10:
            product_class_opts = _select_code_options(
                "제품분류명",
                "0028",
                key=f"__analytics_sales_trend_summary_product_class__{ns}",
            )
        with c11:
            stock_opts = _select_code_options(
                "재고위치",
                "0018",
                key=f"__analytics_sales_trend_summary_stock__{ns}",
            )


        c12, c13, c14 = st.columns(3)
        with c12:
            ven_nm = st.text_input(
                "거래처명",
                value="",
                key=f"__analytics_sales_trend_summary_ven_nm__{ns}",
            )
        with c13:
            buy_nm = st.text_input(
                "매입처명",
                value="",
                key=f"__analytics_sales_trend_summary_buy_nm__{ns}",
            )
        with c14:
            sales_man_nm = st.text_input(
                "영업사원명",
                value="",
                key=f"__analytics_sales_trend_summary_sales_man_nm__{ns}",
            )

        c15, c16, c17 = st.columns(3)
        with c15:
            sido_nm = st.text_input(
                "시도명",
                value="",
                key=f"__analytics_sales_trend_summary_sido_nm__{ns}",
                placeholder="예: 서울",
            )
        with c16:
            gugun_nm = st.text_input(
                "시구군명",
                value="",
                key=f"__analytics_sales_trend_summary_gugun_nm__{ns}",
                placeholder="예: 강남",
            )
        with c17:
            road_nm = st.text_input(
                "도로명",
                value="",
                key=f"__analytics_sales_trend_summary_road_nm__{ns}",
                placeholder="예: 테헤란로",
            )

        submitted = st.form_submit_button("조회", type="primary", use_container_width=True)

    if not submitted:
        return {
            "final": False,
            "type": "text",
            "title": "품목별 매출 추세 요약표",
            "data": "[조회] 버튼을 눌러 실행하세요.",
        }

    source_mode = {
        "자동": "auto",
        "월집계-장부재고": "monthly_book",
        "월집계-실재고": "monthly_real",
        "출고상세": "detail",
    }.get(source_label, "auto")

    date_from_text = _date_to_yyyymmdd(date_from)
    date_to_text = _date_to_yyyymmdd(date_to)

    params = {
        "source_mode": source_mode,

        "date_from": date_from_text,
        "date_to": date_to_text,
        "month_from": _date_to_yyyymm(date_from),
        "month_to": _date_to_yyyymm(date_to),

        "physic_cd": _clean_text(physic_cd),
        "physic_nm": _clean_text(physic_nm),
        "product_ven_nm": _clean_text(product_ven_nm),

        "product_group": product_group_opt.get("code", ""),
        "product_group_nm": product_group_opt.get("name", ""),
        "product_di": "",
        "product_di_nm": "",
        "product_di_list": _selected_codes(product_di_opts),
        "product_di_nm_list": _selected_names(product_di_opts),
        "product_class": "",
        "product_class_nm": "",
        "product_class_list": _selected_codes(product_class_opts),
        "product_class_nm_list": _selected_names(product_class_opts),
        "stock_cd": "",
        "stock_nm": "",
        "stock_cd_list": _selected_codes(stock_opts),
        "stock_nm_list": _selected_names(stock_opts),

        "ven_nm": _clean_text(ven_nm),
        "buy_nm": _clean_text(buy_nm),
        "sales_man_nm": _clean_text(sales_man_nm),
        "sido_nm": _clean_text(sido_nm),
        "gugun_nm": _clean_text(gugun_nm),
        "road_nm": _clean_text(road_nm),
        "trend_judge": "" if trend_judge == "전체" else trend_judge,        
        "top": int(top),
    }

    try:
        result = get_sales_trend_summary_result(params)

        meta = dict(result.get("meta") or {})
        meta.setdefault("analytics", True)
        meta.setdefault("analysis_type", "sales_trend")
        meta.setdefault("sales_trend_summary", True)
        meta.setdefault("summary_type", "product_summary")

        query_condition = _build_sales_trend_query_condition(params, meta)
        if query_condition:
            meta["query_summary"] = query_condition
            meta["condition"] = query_condition

        result["params_raw"] = params
        result["params"] = _build_sales_trend_display_params(params, meta)

        source_label2 = str(meta.get("source_label") or _source_mode_label(params.get("source_mode")))

        if not _clean_text(meta.get("summary_md")):
            source_label = str(meta.get("source_label") or _source_mode_label(params.get("source_mode")))
            meta["summary_md"] = (
                f"매출추세요약: "
                f"조회조건 {query_condition} / "
                f"총매출액 {_fmt_num(meta.get('sum_sales_amt'))} / "
                f"출고수량 {_fmt_num(meta.get('sum_qty'))} / "
                f"품목수 {_fmt_num(meta.get('product_count'))} / "
                f"거래처수 {_fmt_num(meta.get('customer_count'))} / "
                f"분석월수 {_fmt_num(meta.get('month_count'))} / "
                f"자료원 {source_label}"
            )

        result["meta"] = meta

        if (
            _render_inline_analysis_header_enabled()
            and int(meta.get("row_count_total") or meta.get("row_count") or 0) > 0
        ):
            _render_sales_trend_panel_header(meta, query_condition)

        return result

    except Exception as e:
        log.exception("[analytics.views] sales trend summary failed")
        return {
            "final": True,
            "type": "text",
            "title": "품목별 매출 추세 요약표 오류",
            "action": "품목별 매출 추세 요약표",
            "params": _build_sales_trend_display_params(params, {}),
            "params_raw": params,
            "data": str(e),
            "message": str(e),
            "meta": {
                "row_count": 0,
                "row_count_total": 0,
                "analytics": True,
                "analysis_type": "sales_trend",
                "sales_trend_summary": True,
                "summary_type": "product_summary",
            },
        }

def render_sales_forecast_analysis() -> Dict[str, Any]:
    st.subheader("품목별 매출 예상")
    st.caption("품목별 매출 추세 요약표를 기반으로 다음월/3개월/6개월 예상 매출을 계산합니다.")

    ns = _ns()

    with st.form(
        key=f"__analytics_sales_forecast_form__{ns}",
        clear_on_submit=False,
        enter_to_submit=False,
    ):

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            source_label = st.selectbox(
                "분석자료원",
                [
                    "자동",
                    "월집계-장부재고",
                    "월집계-실재고",
                    "출고상세",
                ],
                index=0,
                key=f"__analytics_sales_forecast_source__{ns}",
            )
        with c2:
            date_from = _render_date_input_with_week(
                "시작일자",
                _default_start_date(),
                f"__analytics_sales_forecast_date_from__{ns}",
            )
        with c3:
            date_to = _render_date_input_with_week(
                "종료일자",
                _default_end_date(),
                f"__analytics_sales_forecast_date_to__{ns}",
            )
        with c4:
            trend_judge = st.selectbox(
                "추세판정",
                ["전체", "감소", "안정", "증가", "신규/증가", "자료부족", "반품주의"],
                index=0,
                key=f"__analytics_sales_forecast_judge__{ns}",
            )
        top = _analytics_max_rows()

        c5, c6, c7 = st.columns(3)
        with c5:
            physic_cd = st.text_input(
                "제품코드",
                value="",
                key=f"__analytics_sales_forecast_physic_cd__{ns}",
            )
        with c6:
            physic_nm = st.text_input(
                "제품명",
                value="",
                key=f"__analytics_sales_forecast_physic_nm__{ns}",
            )
        with c7:
            product_ven_nm = st.text_input(
                "제조사명",
                value="",
                key=f"__analytics_sales_forecast_product_ven_nm__{ns}",
            )

        c8, c9, c10, c11 = st.columns(4)
        with c8:
            product_group_opt = _select_code_option(
                "제품그룹명",
                "0013",
                key=f"__analytics_sales_forecast_product_group__{ns}",
            )
        with c9:
            product_di_opts = _select_code_options(
                "제품구분명",
                "0004",
                key=f"__analytics_sales_forecast_product_di__{ns}",
            )
        with c10:
            product_class_opts = _select_code_options(
                "제품분류명",
                "0028",
                key=f"__analytics_sales_forecast_product_class__{ns}",
            )
        with c11:
            stock_opts = _select_code_options(
                "재고위치",
                "0018",
                key=f"__analytics_sales_forecast_stock__{ns}",
            )

        c12, c13, c14 = st.columns(3)
        with c12:
            ven_nm = st.text_input(
                "거래처명",
                value="",
                key=f"__analytics_sales_forecast_ven_nm__{ns}",
            )
        with c13:
            buy_nm = st.text_input(
                "매입처명",
                value="",
                key=f"__analytics_sales_forecast_buy_nm__{ns}",
            )
        with c14:
            sales_man_nm = st.text_input(
                "영업사원명",
                value="",
                key=f"__analytics_sales_forecast_sales_man_nm__{ns}",
            )

        c15, c16, c17 = st.columns(3)
        with c15:
            sido_nm = st.text_input(
                "시도명",
                value="",
                key=f"__analytics_sales_forecast_sido_nm__{ns}",
                placeholder="예: 서울",
            )
        with c16:
            gugun_nm = st.text_input(
                "시구군명",
                value="",
                key=f"__analytics_sales_forecast_gugun_nm__{ns}",
                placeholder="예: 강남",
            )
        with c17:
            road_nm = st.text_input(
                "도로명",
                value="",
                key=f"__analytics_sales_forecast_road_nm__{ns}",
                placeholder="예: 테헤란로",
            )

        submitted = st.form_submit_button("조회", type="primary", use_container_width=True)

    if not submitted:
        return {
            "final": False,
            "type": "text",
            "title": "품목별 매출 예상",
            "data": "[조회] 버튼을 눌러 실행하세요.",
        }

    source_mode = {
        "자동": "auto",
        "월집계-장부재고": "monthly_book",
        "월집계-실재고": "monthly_real",
        "출고상세": "detail",
    }.get(source_label, "auto")

    date_from_text = _date_to_yyyymmdd(date_from)
    date_to_text = _date_to_yyyymmdd(date_to)

    params = {
        "source_mode": source_mode,
        "date_from": date_from_text,
        "date_to": date_to_text,
        "month_from": _date_to_yyyymm(date_from),
        "month_to": _date_to_yyyymm(date_to),
        "physic_cd": _clean_text(physic_cd),
        "physic_nm": _clean_text(physic_nm),
        "product_ven_nm": _clean_text(product_ven_nm),
        "product_group": product_group_opt.get("code", ""),
        "product_group_nm": product_group_opt.get("name", ""),
        "product_di": "",
        "product_di_nm": "",
        "product_di_list": _selected_codes(product_di_opts),
        "product_di_nm_list": _selected_names(product_di_opts),
        "product_class": "",
        "product_class_nm": "",
        "product_class_list": _selected_codes(product_class_opts),
        "product_class_nm_list": _selected_names(product_class_opts),
        "stock_cd": "",
        "stock_nm": "",
        "stock_cd_list": _selected_codes(stock_opts),
        "stock_nm_list": _selected_names(stock_opts),
        "ven_nm": _clean_text(ven_nm),
        "buy_nm": _clean_text(buy_nm),
        "sales_man_nm": _clean_text(sales_man_nm),
        "sido_nm": _clean_text(sido_nm),
        "gugun_nm": _clean_text(gugun_nm),
        "road_nm": _clean_text(road_nm),
        "trend_judge": "" if trend_judge == "전체" else trend_judge,        
        "top": int(top),
    }

    try:
        result = get_sales_forecast_result(params)

        meta = dict(result.get("meta") or {})
        meta.setdefault("analytics", True)
        meta.setdefault("analysis_type", "sales_forecast")
        meta.setdefault("sales_trend_summary", True)
        meta.setdefault("summary_type", "product_forecast")

        query_condition = _build_sales_trend_query_condition(params, meta)
        if query_condition:
            meta["query_summary"] = query_condition
            meta["condition"] = query_condition

        result["params_raw"] = params
        result["params"] = _build_sales_trend_display_params(params, meta)

        result["meta"] = meta

        if (
            _render_inline_analysis_header_enabled()
            and int(meta.get("row_count_total") or meta.get("row_count") or 0) > 0
        ):
            _render_sales_trend_panel_header(meta, query_condition)

        return result

    except Exception as e:
        log.exception("[analytics.views] sales forecast failed")
        return {
            "final": True,
            "type": "text",
            "title": "품목별 매출 예상 오류",
            "action": "품목별 매출 예상",
            "params": _build_sales_trend_display_params(params, {}),
            "params_raw": params,
            "data": str(e),
            "message": str(e),
            "meta": {
                "row_count": 0,
                "row_count_total": 0,
                "analytics": True,
                "analysis_type": "sales_forecast",
                "sales_trend_summary": True,
                "summary_type": "product_forecast",
            },
        }


def render_customer_sales_forecast_analysis() -> Dict[str, Any]:
    st.subheader("매출처별 매출 예상")
    st.caption("Rddbc130 출고 거래명세서 원장매출을 기준으로 매출처별 다음월/3개월/6개월 예상 매출을 계산합니다.")

    ns = _ns()

    with st.form(
        key=f"__analytics_customer_sales_forecast_form__{ns}",
        clear_on_submit=False,
        enter_to_submit=False,
    ):
        c1, c2, c3 = st.columns(3)
        with c1:
            date_from = _render_date_input_with_week(
                "시작일자",
                _default_start_date(),
                f"__analytics_customer_sales_forecast_date_from__{ns}",
            )
        with c2:
            date_to = _render_date_input_with_week(
                "종료일자",
                _default_end_date(),
                f"__analytics_customer_sales_forecast_date_to__{ns}",
            )
        with c3:
            trend_judge = st.selectbox(
                "추세판정",
                ["전체", "감소", "안정", "증가", "자료부족", "반품주의", "신규/증가"],
                index=0,
                key=f"__analytics_customer_sales_forecast_judge__{ns}",
            )

        top = _analytics_max_rows()

        c4, c5, c6, c7 = st.columns(4)
        with c4:
            ven_cd = st.text_input(
                "매출처코드",
                value="",
                key=f"__analytics_customer_sales_forecast_ven_cd__{ns}",
            )
        with c5:
            ven_nm = st.text_input(
                "매출처명",
                value="",
                key=f"__analytics_customer_sales_forecast_ven_nm__{ns}",
            )
        with c6:
            sales_man_nm = st.text_input(
                "영업사원명",
                value="",
                key=f"__analytics_customer_sales_forecast_sales_man_nm__{ns}",
            )
        with c7:
            forecast_grade = st.selectbox(
                "예상등급",
                ["전체", "상승예상", "감소예상", "안정예상", "신규확인", "반품주의", "자료부족"],
                index=0,
                key=f"__analytics_customer_sales_forecast_grade__{ns}",
            )

        c8, c9, c10 = st.columns(3)
        with c8:
            sido_nm = st.text_input(
                "시도명",
                value="",
                key=f"__analytics_customer_sales_forecast_sido_nm__{ns}",
                placeholder="예: 서울",
            )
        with c9:
            gugun_nm = st.text_input(
                "시구군명",
                value="",
                key=f"__analytics_customer_sales_forecast_gugun_nm__{ns}",
                placeholder="예: 강남",
            )
        with c10:
            road_nm = st.text_input(
                "도로명",
                value="",
                key=f"__analytics_customer_sales_forecast_road_nm__{ns}",
                placeholder="예: 테헤란로",
            )

        submitted = st.form_submit_button("조회", type="primary", use_container_width=True)

    if not submitted:
        return {
            "final": False,
            "type": "text",
            "title": "매출처별 매출 예상",
            "data": "[조회] 버튼을 눌러 실행하세요.",
        }

    date_from_text = _date_to_yyyymmdd(date_from)
    date_to_text = _date_to_yyyymmdd(date_to)
    params = {
        "source_mode": "customer_monthly",
        "date_from": date_from_text,
        "date_to": date_to_text,
        "month_from": _date_to_yyyymm(date_from),
        "month_to": _date_to_yyyymm(date_to),
        "ven_cd": _clean_text(ven_cd),
        "ven_nm": _clean_text(ven_nm),
        "sales_man_nm": _clean_text(sales_man_nm),
        "sido_nm": _clean_text(sido_nm),
        "gugun_nm": _clean_text(gugun_nm),
        "road_nm": _clean_text(road_nm),
        "trend_judge": "" if trend_judge == "전체" else trend_judge,
        "forecast_grade": "" if forecast_grade == "전체" else forecast_grade,
        "top": int(top),
    }

    try:
        result = get_customer_sales_forecast_result(params)
        meta = dict(result.get("meta") or {})
        meta.setdefault("analytics", True)
        meta.setdefault("analysis_type", "customer_sales_forecast")
        meta.setdefault("summary_type", "customer_forecast")
        meta.setdefault("group_type", "customer")
        query_condition = _build_sales_trend_query_condition(params, meta)
        if query_condition:
            meta["query_summary"] = query_condition
            meta["condition"] = query_condition
        result["params_raw"] = params
        result["params"] = _build_sales_trend_display_params(params, meta)
        result["meta"] = meta
        if (
            _render_inline_analysis_header_enabled()
            and int(meta.get("row_count_total") or meta.get("row_count") or 0) > 0
        ):
            _render_customer_sales_forecast_panel_header(meta, query_condition)
        return result
    except Exception as e:
        log.exception("[analytics.views] customer sales forecast failed")
        return {
            "final": True,
            "type": "text",
            "title": "매출처별 매출 예상 오류",
            "action": "매출처별 매출 예상",
            "params": _build_sales_trend_display_params(params, {}),
            "params_raw": params,
            "data": str(e),
            "message": str(e),
            "meta": {
                "row_count": 0,
                "row_count_total": 0,
                "analytics": True,
                "analysis_type": "customer_sales_forecast",
                "summary_type": "customer_forecast",
            },
        }


def _render_manufacturer_sales_trend_form(action_key: str) -> tuple[bool, Dict[str, Any]]:
    ns = _ns()

    with st.form(
        key=f"__analytics_{action_key}_form__{ns}",
        clear_on_submit=False,
        enter_to_submit=False,
    ):
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            source_label = st.selectbox(
                "분석자료원",
                ["자동", "월집계-장부재고", "월집계-실재고", "출고상세"],
                index=0,
                key=f"__analytics_{action_key}_source__{ns}",
            )
        with c2:
            date_from = _render_date_input_with_week(
                "시작일자",
                _default_start_date(),
                f"__analytics_{action_key}_date_from__{ns}",
            )
        with c3:
            date_to = _render_date_input_with_week(
                "종료일자",
                _default_end_date(),
                f"__analytics_{action_key}_date_to__{ns}",
            )
        with c4:
            trend_judge = st.selectbox(
                "추세판정",
                ["전체", "감소", "안정", "증가", "신규/증가", "자료부족", "반품주의"],
                index=0,
                key=f"__analytics_{action_key}_judge__{ns}",
            )
        top = _analytics_max_rows()

        c5, c6, c7 = st.columns(3)
        with c5:
            product_ven_nm = st.text_input(
                "제조사명",
                value="",
                key=f"__analytics_{action_key}_product_ven_nm__{ns}",
            )
        with c6:
            buy_nm = st.text_input(
                "매입처명",
                value="",
                key=f"__analytics_{action_key}_buy_nm__{ns}",
            )
        with c7:
            ven_nm = st.text_input(
                "거래처명",
                value="",
                key=f"__analytics_{action_key}_ven_nm__{ns}",
            )

        c8, c9, c10, c11 = st.columns(4)
        with c8:
            product_group_opt = _select_code_option(
                "제품그룹명",
                "0013",
                key=f"__analytics_{action_key}_product_group__{ns}",
            )
        with c9:
            product_di_opts = _select_code_options(
                "제품구분명",
                "0004",
                key=f"__analytics_{action_key}_product_di__{ns}",
            )
        with c10:
            product_class_opts = _select_code_options(
                "제품분류명",
                "0028",
                key=f"__analytics_{action_key}_product_class__{ns}",
            )
        with c11:
            stock_opts = _select_code_options(
                "재고위치",
                "0018",
                key=f"__analytics_{action_key}_stock__{ns}",
            )

        c12, c13, c14 = st.columns(3)
        with c12:
            sido_nm = st.text_input(
                "시도명",
                value="",
                key=f"__analytics_{action_key}_sido_nm__{ns}",
                placeholder="예: 서울",
            )
        with c13:
            gugun_nm = st.text_input(
                "시구군명",
                value="",
                key=f"__analytics_{action_key}_gugun_nm__{ns}",
                placeholder="예: 강남",
            )
        with c14:
            road_nm = st.text_input(
                "도로명",
                value="",
                key=f"__analytics_{action_key}_road_nm__{ns}",
                placeholder="예: 테헤란로",
            )

        submitted = st.form_submit_button("조회", type="primary", use_container_width=True)

    source_mode = {
        "자동": "auto",
        "월집계-장부재고": "monthly_book",
        "월집계-실재고": "monthly_real",
        "출고상세": "detail",
    }.get(source_label, "auto")

    date_from_text = _date_to_yyyymmdd(date_from)
    date_to_text = _date_to_yyyymmdd(date_to)
    params = {
        "source_mode": source_mode,
        "date_from": date_from_text,
        "date_to": date_to_text,
        "month_from": _date_to_yyyymm(date_from),
        "month_to": _date_to_yyyymm(date_to),
        "product_ven_nm": _clean_text(product_ven_nm),
        "product_group": product_group_opt.get("code", ""),
        "product_group_nm": product_group_opt.get("name", ""),
        "product_di": "",
        "product_di_nm": "",
        "product_di_list": _selected_codes(product_di_opts),
        "product_di_nm_list": _selected_names(product_di_opts),
        "product_class": "",
        "product_class_nm": "",
        "product_class_list": _selected_codes(product_class_opts),
        "product_class_nm_list": _selected_names(product_class_opts),
        "stock_cd": "",
        "stock_nm": "",
        "stock_cd_list": _selected_codes(stock_opts),
        "stock_nm_list": _selected_names(stock_opts),
        "ven_nm": _clean_text(ven_nm),
        "buy_nm": _clean_text(buy_nm),
        "sido_nm": _clean_text(sido_nm),
        "gugun_nm": _clean_text(gugun_nm),
        "road_nm": _clean_text(road_nm),
        "trend_judge": "" if trend_judge == "전체" else trend_judge,
        "top": int(top),
    }
    return submitted, params


def _finish_manufacturer_sales_trend_result(
    result: Dict[str, Any],
    params: Dict[str, Any],
    *,
    analysis_type: str,
    summary_type: str,
    header_func,
) -> Dict[str, Any]:
    meta = dict(result.get("meta") or {})
    meta.setdefault("analytics", True)
    meta.setdefault("analysis_type", analysis_type)
    meta.setdefault("summary_type", summary_type)
    meta.setdefault("group_type", "manufacturer")

    query_condition = _build_sales_trend_query_condition(params, meta)
    if query_condition:
        meta["query_summary"] = query_condition
        meta["condition"] = query_condition

    result["params_raw"] = params
    result["params"] = _build_sales_trend_display_params(params, meta)
    result["meta"] = meta

    if (
        _render_inline_analysis_header_enabled()
        and int(meta.get("row_count_total") or meta.get("row_count") or 0) > 0
    ):
        header_func(meta, query_condition)

    return result


def render_manufacturer_sales_trend_analysis() -> Dict[str, Any]:
    st.subheader("제약사별 매출 추세 분석")
    st.caption("월별 제품 매출 원자료를 제약사+월 단위로 집계하여 제약사별 매출 흐름과 월시점 추세를 분석합니다.")

    submitted, params = _render_manufacturer_sales_trend_form("manufacturer_sales_trend")
    if not submitted:
        return {
            "final": False,
            "type": "text",
            "title": "제약사별 매출 추세 분석",
            "data": "[조회] 버튼을 눌러 실행하세요.",
        }

    try:
        result = get_manufacturer_sales_trend_result(params)
        return _finish_manufacturer_sales_trend_result(
            result,
            params,
            analysis_type="manufacturer_sales_trend",
            summary_type="manufacturer_trend_detail",
            header_func=_render_manufacturer_sales_trend_panel_header,
        )
    except Exception as e:
        log.exception("[analytics.views] manufacturer sales trend failed")
        return {
            "final": True,
            "type": "text",
            "title": "제약사별 매출 추세 분석 오류",
            "action": "제약사별 매출 추세 분석",
            "params": _build_sales_trend_display_params(params, {}),
            "params_raw": params,
            "data": str(e),
            "message": str(e),
            "meta": {
                "row_count": 0,
                "row_count_total": 0,
                "analytics": True,
                "analysis_type": "manufacturer_sales_trend",
                "summary_type": "manufacturer_trend_detail",
            },
        }


def render_manufacturer_sales_trend_summary_analysis() -> Dict[str, Any]:
    st.subheader("제약사별 매출 추세 분석 요약표")
    st.caption("제약사별 월 매출을 한 행으로 요약하고 월별 매출과 최종 추세판정을 표시합니다.")

    submitted, params = _render_manufacturer_sales_trend_form("manufacturer_sales_trend_summary")
    if not submitted:
        return {
            "final": False,
            "type": "text",
            "title": "제약사별 매출 추세 분석 요약표",
            "data": "[조회] 버튼을 눌러 실행하세요.",
        }

    try:
        result = get_manufacturer_sales_trend_summary_result(params)
        return _finish_manufacturer_sales_trend_result(
            result,
            params,
            analysis_type="manufacturer_sales_trend_summary",
            summary_type="manufacturer_trend_summary",
            header_func=_render_manufacturer_sales_trend_summary_panel_header,
        )
    except Exception as e:
        log.exception("[analytics.views] manufacturer sales trend summary failed")
        return {
            "final": True,
            "type": "text",
            "title": "제약사별 매출 추세 분석 요약표 오류",
            "action": "제약사별 매출 추세 분석 요약표",
            "params": _build_sales_trend_display_params(params, {}),
            "params_raw": params,
            "data": str(e),
            "message": str(e),
            "meta": {
                "row_count": 0,
                "row_count_total": 0,
                "analytics": True,
                "analysis_type": "manufacturer_sales_trend_summary",
                "summary_type": "manufacturer_trend_summary",
            },
        }


def render_stock_shortage_analysis() -> Dict[str, Any]:
    st.subheader("품목별 재고부족현황")
    st.caption("품목별 출고 추세와 현재재고를 비교하여 부족 가능 품목을 계산합니다.")

    ns = _ns()

    with st.form(
        key=f"__analytics_stock_shortage_form__{ns}",
        clear_on_submit=False,
        enter_to_submit=False,
    ):


        c1, c2, c3, c4 = st.columns(4)
        with c1:
            source_label = st.selectbox(
                "분석자료원",
                ["자동", "월집계-장부재고", "월집계-실재고", "출고상세"],
                index=0,
                key=f"__analytics_stock_shortage_source__{ns}",
            )
        with c2:
            date_from = _render_date_input_with_week(
                "시작일자",
                _default_start_date(),
                f"__analytics_stock_shortage_date_from__{ns}",
            )
        with c3:
            date_to = _render_date_input_with_week(
                "종료일자",
                _default_end_date(),
                f"__analytics_stock_shortage_date_to__{ns}",
            )
        with c4:
            shortage_grade = st.selectbox(
                "부족등급",
                SHORTAGE_GRADE_OPTIONS,
                index=0,
                key=f"__analytics_stock_shortage_grade__{ns}",
            )
        top = _analytics_max_rows()

        c_stock, c6, c7, c8 = st.columns(4)
        with c_stock:
            stock_label = st.selectbox(
                "재고기준",
                ["장부재고", "실재고"],
                index=0,
                key=f"__analytics_stock_shortage_stock_mode__{ns}",
            )
        with c6:
            physic_cd = st.text_input("제품코드", value="", key=f"__analytics_stock_shortage_physic_cd__{ns}")
        with c7:
            physic_nm = st.text_input("제품명", value="", key=f"__analytics_stock_shortage_physic_nm__{ns}")
        with c8:
            product_ven_nm = st.text_input("제조사명", value="", key=f"__analytics_stock_shortage_product_ven_nm__{ns}")

        c9, c10, c11, c12 = st.columns(4)
        with c9:
            product_group_opt = _select_code_option(
                "제품그룹명",
                "0013",
                key=f"__analytics_stock_shortage_product_group__{ns}",
            )
        with c10:
            product_di_opts = _select_code_options(
                "제품구분명",
                "0004",
                key=f"__analytics_stock_shortage_product_di__{ns}",
            )
        with c11:
            product_class_opts = _select_code_options(
                "제품분류명",
                "0028",
                key=f"__analytics_stock_shortage_product_class__{ns}",
            )
        with c12:
            stock_opts = _select_code_options(
                "재고위치",
                "0018",
                key=f"__analytics_stock_shortage_stock__{ns}",
            )

        c13, c14, c15 = st.columns(3)
        with c13:
            ven_nm = st.text_input("거래처명", value="", key=f"__analytics_stock_shortage_ven_nm__{ns}")
        with c14:
            buy_nm = st.text_input("매입처명", value="", key=f"__analytics_stock_shortage_buy_nm__{ns}")
        with c15:
            sales_man_nm = st.text_input("영업사원명", value="", key=f"__analytics_stock_shortage_sales_man_nm__{ns}")

        submitted = st.form_submit_button("조회", type="primary", use_container_width=True)

    if not submitted:
        return {
            "final": False,
            "type": "text",
            "title": "품목별 재고부족현황",
            "data": "[조회] 버튼을 눌러 실행하세요.",
        }

    source_mode = {
        "자동": "auto",
        "월집계-장부재고": "monthly_book",
        "월집계-실재고": "monthly_real",
        "출고상세": "detail",
    }.get(source_label, "auto")

    stock_mode = {
        "장부재고": "book",
        "실재고": "real",
    }.get(stock_label, "book")

    date_from_text = _date_to_yyyymmdd(date_from)
    date_to_text = _date_to_yyyymmdd(date_to)

    params = {
        "source_mode": source_mode,
        "stock_mode": stock_mode,
        "date_from": date_from_text,
        "date_to": date_to_text,
        "month_from": _date_to_yyyymm(date_from),
        "month_to": _date_to_yyyymm(date_to),
        "physic_cd": _clean_text(physic_cd),
        "physic_nm": _clean_text(physic_nm),
        "product_ven_nm": _clean_text(product_ven_nm),
        "product_group": product_group_opt.get("code", ""),
        "product_group_nm": product_group_opt.get("name", ""),
        "product_di": "",
        "product_di_nm": "",
        "product_di_list": _selected_codes(product_di_opts),
        "product_di_nm_list": _selected_names(product_di_opts),
        "product_class": "",
        "product_class_nm": "",
        "product_class_list": _selected_codes(product_class_opts),
        "product_class_nm_list": _selected_names(product_class_opts),
        "stock_cd": "",
        "stock_nm": "",
        "stock_cd_list": _selected_codes(stock_opts),
        "stock_nm_list": _selected_names(stock_opts),
        "ven_nm": _clean_text(ven_nm),
        "buy_nm": _clean_text(buy_nm),
        "sales_man_nm": _clean_text(sales_man_nm),
        "shortage_grade": "" if shortage_grade == "전체" else shortage_grade,
        "top": int(top),
    }

    try:
        result = get_stock_shortage_result(params)

        meta = dict(result.get("meta") or {})
        meta.setdefault("analytics", True)
        meta.setdefault("analysis_type", "stock_shortage")
        meta.setdefault("summary_type", "product_stock_shortage")

        query_condition = _build_sales_trend_query_condition(params, meta)
        if query_condition:
            query_condition = f"{query_condition} / 재고기준 {stock_label}"
            meta["query_summary"] = query_condition
            meta["condition"] = query_condition

        result["params_raw"] = params
        result["params"] = _build_sales_trend_display_params(params, meta)
        result["params"]["재고기준"] = stock_label
        result["meta"] = meta

        if (
            _render_inline_analysis_header_enabled()
            and int(meta.get("row_count_total") or meta.get("row_count") or 0) > 0
        ):
            _render_stock_shortage_panel_header(meta, query_condition)

        return result

    except Exception as e:
        log.exception("[analytics.views] stock shortage failed")
        return {
            "final": True,
            "type": "text",
            "title": "품목별 재고부족현황 오류",
            "action": "품목별 재고부족현황",
            "params": _build_sales_trend_display_params(params, {}),
            "params_raw": params,
            "data": str(e),
            "message": str(e),
            "meta": {
                "row_count": 0,
                "row_count_total": 0,
                "analytics": True,
                "analysis_type": "stock_shortage",
                "summary_type": "product_stock_shortage",
            },
        }
