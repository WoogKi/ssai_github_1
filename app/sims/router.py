# app/sims/router.py
# VERSION = "chat_middleware/2025-11-01T-v1"

from app.sims.config import CATEGORIES, ACTIONS_BY_CATEGORY
from app.sims.views import dashboard, users, codes
from app.sims.views import goods
from app.sims.views import rddbc_io_views
import streamlit as st
import logging
log = logging.getLogger("ssai")


def all_categories():
    return list(CATEGORIES)

def actions_of(category: str):
    return ACTIONS_BY_CATEGORY.get(category, [])

def _canon_action(s: str) -> str:
    """
    액션 라벨을 표준형으로 정규화:
    - 앞뒤 공백 제거
    - 연속 공백 1개로 축약
    - '사용자목록 + 부서명' 같이 공백 빠진 케이스 보정
    """
    if not isinstance(s, str):
        return ""
    s0 = " ".join(s.strip().split())  # 연속 공백 축약
    # 개별 alias 보정(필요시 확장)
    aliases = {
        "사용자목록 + 부서명": "사용자 목록 + 부서명",
        "부서별사용자수": "부서별 사용자 수",
        "그룹코드조회": "그룹별 코드 조회",
        "그룹 별 코드 조회": "그룹별 코드 조회",
        "코드명검색": "코드명 검색",
        "제품코드목록": "제품코드 목록",
        "제품코드상세": "제품코드 상세",        
        "입고명세조회": "입고명세 조회",
        "출고명세조회": "출고명세 조회",
        "거래명세서공통조회": "거래명세서 공통 조회",
        "세금계산서공통조회": "세금계산서 공통 조회",
        "실재고월집계조회": "실재고월집계 조회",
        "장부재고월집계조회": "장부재고월집계 조회",
        "입고거래명세서검증": "입고↔거래명세서 검증",
        "입고세금계산서검증": "입고↔세금계산서 검증",
        "출고거래명세서검증": "출고↔거래명세서 검증",
        "출고세금계산서검증": "출고↔세금계산서 검증",
    }
    return aliases.get(s0, s0)

def run_sims_action(action: str):
    a = _canon_action(action)
    log.info("[router] in: %r -> canon: %r", action, a)
    try:
        # Dashboard
        if a == "월별 매출":
            return dashboard.render_monthly_sales()
        elif a == "입출고 추이":
            return dashboard.render_inout_trend()
        elif a == "AR/AP 요약":
            return dashboard.render_ar_ap_summary()
        elif a == "재고 회전(90일)":
            return dashboard.render_inventory_turn()

        # Users
        elif a == "사용자 목록 + 부서명":
            return users.render_user_list_with_dept()
        elif a == "부서별 사용자 수":
            return users.render_user_count_by_dept()
        elif a == "최근 입사자":
            return users.render_recent_hires()

        # Codes
        elif a == "그룹별 코드 조회":
            return codes.render_codes_by_group()
        elif a == "코드명 검색":
            return codes.render_search_codes()
        elif a == "코드 사용처 추적(예시)":
            return codes.render_code_usage_example()

        # Goods (Rddbc040)
        elif a == "제품코드 목록":
            return rddbc_io_views.view_rddbc040()
        elif a == "제품코드 상세":
            return goods.view_goods_detail()
        
        # RDDBC IO / 거래명세서 / 세금계산서 / 월집계
        elif a == "입고명세 조회":
            return rddbc_io_views.view_rddbc110()
        elif a == "출고명세 조회":
            return rddbc_io_views.view_rddbc120()
        elif a == "거래명세서 공통 조회":
            return rddbc_io_views.view_rddbc130()
        elif a == "세금계산서 공통 조회":
            return rddbc_io_views.view_rddbc140()
        elif a == "실재고월집계 조회":
            return rddbc_io_views.view_rddbc210()
        elif a == "장부재고월집계 조회":
            return rddbc_io_views.view_rddbc220()
        elif a == "입고↔거래명세서 검증":
            return rddbc_io_views.view_rddbc110_trans_check()
        elif a == "입고↔세금계산서 검증":
            return rddbc_io_views.view_rddbc110_tax_check()
        elif a == "출고↔거래명세서 검증":
            return rddbc_io_views.view_rddbc120_trans_check()
        elif a == "출고↔세금계산서 검증":
            return rddbc_io_views.view_rddbc120_tax_check()
        
        # Inventory / Sales KPI (placeholders)
        elif a == "품목별 재고 상위 50":
            return dashboard.render_top_inventory()
        elif a == "재고 부족(임계치)":
            return dashboard.render_low_stock()
        elif a == "최근 입고 100":
            return dashboard.render_recent_receipts()
        elif a == "최근 출하 100":
            return dashboard.render_recent_shipments()
        elif a == "일자별 매출 합계":
            return dashboard.render_daily_sales()
        elif a == "고객 매출 랭킹 Top N":
            return dashboard.render_customer_ranking()
        elif a == "제품 매출 랭킹 Top N":
            return dashboard.render_product_ranking()
        elif a == "전년동기 대비(YoY)":
            return dashboard.render_yoy()
        elif a == "전월대비(MoM)":
            return dashboard.render_mom()
        elif a == "주간 추이(최근 12주)":
            return dashboard.render_weekly_trend()
        else:
            log.warning("[router] miss: %r (canon=%r) — no match", action, a)
            st.info(f"미정의 작업: {action}")
            return {"title": "SIMS 실행", "text": f"미정의 작업: {action}", "action": action}
    except Exception as e:
        log.exception("[router] exception on %r (canon=%r): %s", action, a, e)
        st.error(f"실행 오류: {e}")
        return {"title": "SIMS 실행 오류", "text": str(e), "action": action}
