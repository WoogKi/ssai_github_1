# tools/ssai_login_ui_test.py
#
# login 테스트용 Streamlit 파일 생성
#
# Create 2026/06/22

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ui.ssai_login import (  # noqa: E402
    get_current_permissions,
    get_current_user,
    get_selected_company,
    has_permission,
    require_login,
)


st.set_page_config(
    page_title="SS AI 로그인 테스트",
    layout="wide",
)

if not require_login():
    st.stop()

user = get_current_user()
company = get_selected_company()
permissions = get_current_permissions()

st.title("SS AI 로그인 테스트 완료")

st.subheader("사용자 정보")
st.json(
    {
        "user_id": user.user_id if user else None,
        "login_id": user.login_id if user else None,
        "user_name": user.user_name if user else None,
        "user_type": user.user_type if user else None,
        "user_grade": user.user_grade if user else None,
    }
)

st.subheader("선택 회사")
st.json(company)

st.subheader("권한")
st.write(f"권한 수: {len(permissions)}")
st.write(permissions)

st.subheader("권한 체크 예시")
st.write(f"MASTER_READ: {has_permission('MASTER_READ')}")
st.write(f"EXPORT_EXCEL: {has_permission('EXPORT_EXCEL')}")
st.write(f"SIMS_DB_MANAGE: {has_permission('SIMS_DB_MANAGE')}")