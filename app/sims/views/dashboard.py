# app/sims/views/dashboard.py
import streamlit as st
from datetime import date, timedelta
from app.db import query_templates as qt
from app.db.mssql_client import read_df
from app.services.utils import apply_labels

def _date_range_inputs():
    col1, col2 = st.columns(2)
    with col1:
        start = st.date_input("시작일", value=date.today().replace(day=1))
    with col2:
        end = st.date_input("종료일", value=date.today())
    return str(start), str(end)

def render_monthly_sales():
    st.subheader("📈 월별 매출")
    start, end = _date_range_inputs()
    try:
        df = read_df(qt.MONTHLY_SALES_SUMMARY, (start, end))
        st.dataframe(df, width="stretch", height=320)
        if "YearMonth" in df.columns and "SalesAmount" in df.columns:
            st.line_chart(df.set_index("YearMonth")["SalesAmount"])
    except Exception as e:
        st.warning(f"조회 오류: {e}")
        return
    # ✅ 채팅 전송용 payload
    try:
        return {
            "title": f"월별 매출 ({start} ~ {end})",
            "action": "월별 매출",
            "columns": list(df.columns),
            "df": df.to_dict(orient="records"),
        }
    except Exception:
        return
def render_inout_trend():
    st.subheader("📦 입출고 추이")
    start, end = _date_range_inputs()
    try:
        df = read_df(qt.INOUT_BY_DATE_RANGE, (start, end))
        st.dataframe(df, width="stretch", height=320)
        if {"IoDate","InQty","OutQty"}.issubset(df.columns):
            st.line_chart(df.set_index("IoDate")[["InQty","OutQty"]])
    except Exception as e:
        st.warning(f"조회 오류: {e}")
        return
    try:
        return {
             "title": f"입출고 추이 ({start} ~ {end})",
             "action": "입출고 추이",
             "columns": list(df.columns),
             "df": df.to_dict(orient="records"),
        }
    except Exception:
        return
# ---- Placeholders below (replace queries with your templates/services) ----
def render_ar_ap_summary():
    st.subheader("💸 AR/AP 요약")
    st.info("AR/AP 템플릿 연결 예정")

def render_inventory_turn():
    st.subheader("🔄 재고 회전(90일)")
    st.info("재고 회전 템플릿 연결 예정")

def render_top_inventory():
    st.subheader("📦 품목별 재고 상위 50")
    st.info("재고 상위 템플릿 연결 예정")

def render_low_stock():
    st.subheader("⚠️ 재고 부족(임계치)")
    st.info("재고 임계치 템플릿 연결 예정")

def render_recent_receipts():
    st.subheader("⬇️ 최근 입고 100")
    st.info("최근 입고 템플릿 연결 예정")

def render_recent_shipments():
    st.subheader("⬆️ 최근 출하 100")
    st.info("최근 출하 템플릿 연결 예정")

def render_daily_sales():
    st.subheader("📅 일자별 매출 합계")
    start = str(date.today() - timedelta(days=30))
    end = str(date.today())
    st.info(f"기간: {start} ~ {end} (템플릿 연결 예정)")

def render_customer_ranking():
    st.subheader("👥 고객 매출 랭킹 Top N")
    st.info("랭킹 템플릿 연결 예정")

def render_product_ranking():
    st.subheader("📦 제품 매출 랭킹 Top N")
    st.info("랭킹 템플릿 연결 예정")

def render_yoy():
    st.subheader("📊 전년동기 대비(YoY)")
    st.info("YoY 템플릿 연결 예정")

def render_mom():
    st.subheader("📊 전월대비(MoM)")
    st.info("MoM 템플릿 연결 예정")

def render_weekly_trend():
    st.subheader("📆 주간 추이(최근 12주)")
    st.info("주간 추이 템플릿 연결 예정")
