# tools/ssai_streamlit_company_db_probe.py
#
# Streamlit session_state에서 선택한 회사가 실제 read_df()까지 전달되는지 확인용
#
# Create 2026/06/22

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.mssql_client import read_df  # noqa: E402
from app.ui.ssai_login import (  # noqa: E402
    get_selected_company,
    require_login,
)


st.set_page_config(
    page_title="SS AI 회사 DB 연결 확인",
    layout="wide",
)

if not require_login():
    st.stop()

company = get_selected_company()

st.title("SS AI 회사 DB 연결 확인")

st.subheader("선택 회사")
st.json(company)

st.subheader("현재 read_df() 접속 DB")

df_db = read_df("SELECT DB_NAME() AS current_db, SYSTEM_USER AS [system_user]")
df_count = read_df("SELECT COUNT(*) AS row_count FROM dbo.Rddbc060")

current_db = df_db.iloc[0]["current_db"]
system_user = df_db.iloc[0]["system_user"]
row_count = int(df_count.iloc[0]["row_count"])

st.success("read_df() 회사 DB 접속 성공")

st.write(f"현재 DB: **{current_db}**")
st.write(f"접속 사용자: **{system_user}**")
st.write(f"Rddbc060 건수: **{row_count:,}건**")

expected_db = company.get("db_name") if isinstance(company, dict) else None

if expected_db and str(current_db).lower() == str(expected_db).lower():
    st.success("선택 회사 DB와 read_df() 접속 DB가 일치합니다.")
else:
    st.error("선택 회사 DB와 read_df() 접속 DB가 다릅니다.")

st.divider()

st.caption("회사 변경은 왼쪽 사이드바의 [회사 변경] 버튼으로 테스트하세요.")