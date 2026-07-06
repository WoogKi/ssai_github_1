# app/sims/views/master_advanced_filters.py
# -*- coding: utf-8 -*-
# 공용 화일 2026.05.15 신규 생성
# 등록자
# 등록일자 시작/종료
# 수정자
# 수정일자 시작/종료
# 조회조건 문구 생성
# 전체 row_count / 화면 display_row_count 분리
# LLM용 전체 집계요약
# Excel/CSV 전체 다운로드
#
from __future__ import annotations

from typing import Any, Sequence
import datetime as dt

import pandas as pd
import streamlit as st


def clean_text(value: Any) -> str:
    s = str(value or "").strip()
    if s in {"None", "nan", "NaN", "<NA>", "NaT"}:
        return ""
    return s


def date_to_yyyymmdd(value: Any) -> str:
    if isinstance(value, dt.datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, dt.date):
        return value.strftime("%Y%m%d")

    s = clean_text(value)
    digits = "".join(ch for ch in s if ch.isdigit())

    if len(digits) == 8:
        return digits
    return ""


def parse_yyyymmdd_or_none(value: Any):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) != 8:
        return None
    try:
        return dt.datetime.strptime(digits, "%Y%m%d").date()
    except Exception:
        return None


def render_master_audit_filter(
    *,
    prefix: str,
    ns: str,
    expanded: bool = False,
    add_user_label: str = "등록자",
    add_date_from_label: str = "등록일자 시작",
    add_date_to_label: str = "등록일자 종료",
    mod_user_label: str = "수정자",
    mod_date_from_label: str = "수정일자 시작",
    mod_date_to_label: str = "수정일자 종료",
) -> dict[str, Any]:
    """
    마스터 화면 공통 고급조회 이력조건 UI.

    반환값:
    - add_user_nm
    - add_date_from
    - add_date_to
    - mod_user_nm
    - mod_date_from
    - mod_date_to

    날짜는 YYYYMMDD 문자열로 반환한다.
    """
    with st.expander("고급조회", expanded=expanded):
        a1, a2, a3 = st.columns(3)
        with a1:
            add_user_nm = st.text_input(
                add_user_label,
                value="",
                key=f"__{prefix}_add_user_nm__{ns}",
                placeholder="예: 관리자",
            )
        with a2:
            add_date_from_raw = st.date_input(
                add_date_from_label,
                value=None,
                key=f"__{prefix}_add_date_from__{ns}",
            )
        with a3:
            add_date_to_raw = st.date_input(
                add_date_to_label,
                value=None,
                key=f"__{prefix}_add_date_to__{ns}",
            )

        m1, m2, m3 = st.columns(3)
        with m1:
            mod_user_nm = st.text_input(
                mod_user_label,
                value="",
                key=f"__{prefix}_mod_user_nm__{ns}",
                placeholder="예: 관리자",
            )
        with m2:
            mod_date_from_raw = st.date_input(
                mod_date_from_label,
                value=None,
                key=f"__{prefix}_mod_date_from__{ns}",
            )
        with m3:
            mod_date_to_raw = st.date_input(
                mod_date_to_label,
                value=None,
                key=f"__{prefix}_mod_date_to__{ns}",
            )

    return {
        "add_user_nm": clean_text(add_user_nm),
        "add_date_from": date_to_yyyymmdd(add_date_from_raw),
        "add_date_to": date_to_yyyymmdd(add_date_to_raw),
        "mod_user_nm": clean_text(mod_user_nm),
        "mod_date_from": date_to_yyyymmdd(mod_date_from_raw),
        "mod_date_to": date_to_yyyymmdd(mod_date_to_raw),
    }


def append_condition(parts: list[str], label: str, value: Any) -> None:
    s = clean_text(value)
    if s and s != "전체":
        parts.append(f"{label} {s}")


def append_date_range_condition(
    parts: list[str],
    label: str,
    date_from: Any,
    date_to: Any,
) -> None:
    a = clean_text(date_from)
    b = clean_text(date_to)

    if a and b:
        parts.append(f"{label} {a}~{b}" if a != b else f"{label} {a}")
    elif a:
        parts.append(f"{label} {a}~")
    elif b:
        parts.append(f"{label} ~{b}")


def build_master_query_condition(
    params: dict[str, Any],
    *,
    total: int,
    display_count: int,
    field_specs: Sequence[tuple],
    active_key: str = "사용중만",
    active_text: str = "사용구분 사용중",
) -> str:
    """
    마스터 화면/NLQ 공통 조회조건 문구 생성.

    field_specs 예:
    [
        ("text", "거래처코드구분", "거래처코드구분"),
        ("text", "거래처명", "거래처명"),
        ("date_range", "등록일자", "등록일자From", "등록일자To"),
    ]

    내부 표현(scope, True, TopN)은 넣지 않는다.
    """
    parts: list[str] = []

    for spec in field_specs:
        if not spec:
            continue

        kind = str(spec[0] or "").strip()

        if kind == "text" and len(spec) >= 3:
            label = str(spec[1])
            key = str(spec[2])
            append_condition(parts, label, params.get(key))

        elif kind == "date_range" and len(spec) >= 4:
            label = str(spec[1])
            from_key = str(spec[2])
            to_key = str(spec[3])
            append_date_range_condition(
                parts,
                label,
                params.get(from_key, ""),
                params.get(to_key, ""),
            )

    if bool(params.get(active_key)):
        parts.append(active_text)

    condition = " / ".join(parts) if parts else "전체"

    if int(total) > int(display_count):
        return f"{condition}\n조회결과: {int(total):,}건 (표시는 상위 {int(display_count):,}건)"
    return f"{condition}\n조회결과: {int(total):,}건"


def norm_series(series: pd.Series) -> pd.Series:
    try:
        s = series.fillna("").astype(str).str.strip()
        return s[(s != "") & (s != "전체")]
    except Exception:
        return pd.Series(dtype="object")


def top_count_records(df: pd.DataFrame, col: str, top_n: int = 10) -> list[dict[str, Any]]:
    if df is None or df.empty or col not in df.columns:
        return []

    s = norm_series(df[col])
    if s.empty:
        return []

    vc = s.value_counts(dropna=True).head(top_n)
    return [{"name": str(idx), "count": int(cnt)} for idx, cnt in vc.items()]


def year_count_records(df: pd.DataFrame, col: str, top_n: int = 10) -> list[dict[str, Any]]:
    if df is None or df.empty or col not in df.columns:
        return []

    s = norm_series(df[col])
    if s.empty:
        return []

    years = (
        s.str.replace("-", "", regex=False)
        .str.extract(r"^([0-9]{4})", expand=False)
        .fillna("")
        .astype(str)
        .str.strip()
    )
    years = years[years != ""]

    if years.empty:
        return []

    vc = years.value_counts(dropna=True).head(top_n)
    return [{"year": str(idx), "count": int(cnt)} for idx, cnt in vc.items()]


def fmt_top_records(items: list[dict[str, Any]], name_key: str = "name") -> str:
    if not items:
        return "없음"

    out: list[str] = []
    for x in items:
        name = clean_text(x.get(name_key))
        if not name:
            continue
        try:
            cnt = int(x.get("count", 0))
        except Exception:
            cnt = 0
        out.append(f"{name} {cnt:,}건")

    return ", ".join(out) if out else "없음"


def build_master_llm_summary(
    df: pd.DataFrame,
    *,
    master_name: str,
    query_condition: str,
    total: int,
    display_count: int,
    count_specs: Sequence[tuple[str, str, str]],
    year_specs: Sequence[tuple[str, str, str]] | None = None,
    top_n: int = 10,
    answer_rule: str | None = None,
) -> dict[str, Any]:
    """
    마스터 LLM 분석용 전체 집계 요약 생성.

    count_specs 예:
    [
        ("거래처그룹별 상위", "거래처그룹명", "group_top"),
        ("거래처종류별 상위", "거래처종류명", "kind_top"),
    ]

    year_specs 예:
    [
        ("등록연도별 상위", "등록일자", "add_year_top"),
    ]
    """
    df0 = df if isinstance(df, pd.DataFrame) else pd.DataFrame()
    year_specs = year_specs or []

    condition_text = clean_text(query_condition).replace("\n", " / ")

    result: dict[str, Any] = {
        "basis": "전체 조회결과 기준",
        "total_count": int(total),
        "display_count": int(display_count),
    }

    lines: list[str] = [
        f"{master_name} 전체 집계 요약",
        f"- 분석 기준: 전체 조회결과 기준",
        f"- 조회조건: {condition_text or '전체'}",
        f"- 전체 조회건수: {int(total):,}건",
        f"- 화면 표시건수: {int(display_count):,}건",
    ]

    for label, col, key in count_specs:
        items = top_count_records(df0, col, top_n=top_n)
        result[key] = items
        lines.append(f"- {label}: {fmt_top_records(items)}")

    for label, col, key in year_specs:
        items = year_count_records(df0, col, top_n=top_n)
        result[key] = items
        lines.append(f"- {label}: {fmt_top_records(items, name_key='year')}")

    if answer_rule:
        lines.append(f"- 답변 규칙: {answer_rule}")
    else:
        lines.append("- 답변 규칙: 화면에는 일부만 표시될 수 있지만, 위 집계는 전체 조회결과 기준입니다.")

    llm_summary_md = "\n".join(lines)
    result["llm_summary_md"] = llm_summary_md

    return result