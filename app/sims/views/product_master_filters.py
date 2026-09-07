"""Shared Streamlit controls for the R040 product-master filter contract."""

from __future__ import annotations

import logging
from typing import Any, Mapping

import streamlit as st

from app.db.mssql_client import read_df
from app.sims.views.master_advanced_filters import render_master_audit_filter


log = logging.getLogger("ssai")


@st.cache_data(ttl=600, show_spinner=False)
def product_code_name_options(gcode: str) -> list[str]:
    sql = """
SELECT DISTINCT LTRIM(RTRIM(Rd01_Hnm)) AS code_name
FROM dbo.Rddbc010 WITH (NOLOCK)
WHERE Rd01_Gcode = ?
  AND ISNULL(Rd01_Hnm, '') <> ''
ORDER BY LTRIM(RTRIM(Rd01_Hnm))
""".strip()
    try:
        df = read_df(sql, (str(gcode),))
    except Exception:
        log.exception("[product.master.filters] code option load failed gcode=%s", gcode)
        return ["전체"]
    if df is None or df.empty or "code_name" not in df.columns:
        return ["전체"]
    values = list(dict.fromkeys(str(value or "").strip() for value in df["code_name"].tolist()))
    return ["전체", *(value for value in values if value)]


def render_product_master_filters(
    *,
    prefix: str,
    ns: str,
    defaults: Mapping[str, Any] | None = None,
    only_use_default: bool,
    display_caption: str = "",
    include_price_filters: bool = True,
    include_audit_filters: bool = True,
    include_only_use: bool = True,
) -> dict[str, Any]:
    values = dict(defaults or {})
    group_options = product_code_name_options("0013")
    di_options = product_code_name_options("0004")
    class_options = product_code_name_options("0028")

    def selected(options: list[str], value: Any) -> int:
        text = str(value or "").strip()
        return options.index(text) if text in options else 0

    c1, c2, c3 = st.columns(3)
    with c1:
        physic_cd = st.text_input("제품코드(정확)", value=str(values.get("physic_cd") or ""), key=f"{prefix}_physic_cd_{ns}")
    with c2:
        keyword = st.text_input("키워드(제품명/제품코드/보험코드)", value=str(values.get("product_keyword") or values.get("physic_nm") or ""), key=f"{prefix}_keyword_{ns}")
    with c3:
        insu_cd = st.text_input("보험코드(정확)", value=str(values.get("insu_cd") or ""), key=f"{prefix}_insu_cd_{ns}")

    c4, c5, c6 = st.columns(3)
    with c4:
        maker_nm = st.text_input("제약사명 포함", value=str(values.get("maker_nm") or ""), key=f"{prefix}_maker_nm_{ns}")
    with c5:
        barcode = st.text_input("바코드(정확, 1~5)", value=str(values.get("barcode") or ""), key=f"{prefix}_barcode_{ns}")
    with c6:
        if include_price_filters:
            unit_price = st.text_input("단가(정확 또는 범위)", value=str(values.get("product_unit_price") or ""), key=f"{prefix}_unit_price_{ns}")
        else:
            unit_price = ""
            if display_caption:
                st.caption(display_caption)

    c7, c8, c9 = st.columns(3)
    with c7:
        group_nm = st.selectbox("제품그룹명", group_options, index=selected(group_options, values.get("product_group_nm")), key=f"{prefix}_group_nm_{ns}")
    with c8:
        di_nm = st.selectbox("구분명", di_options, index=selected(di_options, values.get("product_di_nm")), key=f"{prefix}_di_nm_{ns}")
    with c9:
        class_nm = st.selectbox("제품분류명", class_options, index=selected(class_options, values.get("product_class_nm")), key=f"{prefix}_class_nm_{ns}")

    c10, c11, c12 = st.columns(3)
    with c10:
        if include_price_filters:
            final_price_date = st.text_input("최종단가변경일자(YYYYMMDD 또는 범위)", value=str(values.get("product_final_price_date") or ""), key=f"{prefix}_final_price_date_{ns}")
        else:
            final_price_date = ""
    with c11:
        if include_only_use:
            only_use = st.checkbox("사용(Use_Gu=0)만", value=bool(values.get("product_only_use", only_use_default)), key=f"{prefix}_only_use_{ns}")
        else:
            only_use = False
    with c12:
        if display_caption and include_price_filters:
            st.caption(display_caption)

    audit = (
        render_master_audit_filter(prefix=prefix, ns=ns, expanded=False)
        if include_audit_filters
        else {
            "add_user_nm": "", "add_date_from": "", "add_date_to": "",
            "mod_user_nm": "", "mod_date_from": "", "mod_date_to": "",
        }
    )
    return {
        "physic_cd": str(physic_cd or "").strip(),
        "product_keyword": str(keyword or "").strip(),
        "insu_cd": str(insu_cd or "").strip(),
        "barcode": str(barcode or "").strip(),
        "maker_nm": str(maker_nm or "").strip(),
        "product_group_nm": "" if group_nm == "전체" else str(group_nm),
        "product_di_nm": "" if di_nm == "전체" else str(di_nm),
        "product_class_nm": "" if class_nm == "전체" else str(class_nm),
        "product_unit_price": str(unit_price or "").strip(),
        "product_final_price_date": str(final_price_date or "").strip(),
        "product_only_use": bool(only_use),
        "product_add_user_nm": audit["add_user_nm"],
        "product_add_date_from": audit["add_date_from"],
        "product_add_date_to": audit["add_date_to"],
        "product_mod_user_nm": audit["mod_user_nm"],
        "product_mod_date_from": audit["mod_date_from"],
        "product_mod_date_to": audit["mod_date_to"],
    }
