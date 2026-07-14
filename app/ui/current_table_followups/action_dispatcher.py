# app/ui/current_table_followups/action_dispatcher.py
# 현재표 후속분석 action kind 매핑 및 dispatcher
# Create 2026-06-07
# =========================================================
# 현재표 후속분석 action kind 매핑
# =========================================================
# 입고↔세금계산서 검증       → validation
# 출고↔세금계산서 검증       → validation
# 입고↔거래명세서 검증       → validation
# 출고↔거래명세서 검증       → validation
# 실재고월집계 조회          → monthly_stock
# 장부재고월집계 조회        → monthly_stock
# 제품재고현황 조회          → inventory
# 제품수불현황 조회          → stock_ledger
# 출고명세 조회              → sales_detail
# 입고명세 조회              → purchase_detail
# 세금계산서 공통 조회       → tax_doc
# 거래명세서 공통 조회       → trans_doc
# 품목별매출추세분석         → analytics_kpi
# 품목별매출추세요약표       → analytics_kpi
# 품목별매출예상"            → analytics_kpi
# 제약사별매출추세분석       → analytics_kpi
# 제약사별매출추세분석요약표 → analytics_kpi
# 목별재고부족현황"          → analytics_kpi
# 현재표 generic/마스터류 후속분석 -> generic
#
# 원칙:
# - 현재표 질문은 질문 단어보다 원본 action을 먼저 본다.
# - action kind별 전용 handler가 먼저 처리한다.
# - 처리하지 못한 경우에만 기존 legacy 현재표 후속분석 블록으로 내려간다.
# - 안정화 후 legacy 블록을 action별 모듈로 이동/삭제한다.
# =========================================================

from __future__ import annotations

import re
from typing import Any, Callable

import pandas as pd

from app.ui.current_table_followups.monthly_stock import handle_monthly_stock_followup
from app.ui.current_table_followups.validation import handle_validation_followup
from app.ui.current_table_followups.inventory import handle_inventory_followup
from app.ui.current_table_followups.stock_ledger import handle_stock_ledger_followup
from app.ui.current_table_followups.sales_detail import handle_sales_detail_followup
from app.ui.current_table_followups.purchase_detail import handle_purchase_detail_followup
from app.ui.current_table_followups.tax_doc import handle_tax_doc_followup
from app.ui.current_table_followups.trans_doc import handle_trans_doc_followup
from app.ui.current_table_followups.analytics_kpi import handle_analytics_kpi_followup
from app.ui.current_table_followups.generic import (
    handle_generic_followup,
    handle_common_column_filter_followup,
    handle_common_column_group_followup,
)


def detect_current_table_kind(source_action: str) -> str:
    s = re.sub(r"\s+", "", str(source_action or ""))

    if "검증" in s:
        return "validation"

    if "실재고월집계" in s or "장부재고월집계" in s:
        return "monthly_stock"

    if "제품재고현황" in s or "제품재고" in s:
        return "inventory"

    if "제품수불현황" in s or "제품수불" in s:
        return "stock_ledger"

    if "출고명세" in s:
        return "sales_detail"

    if "입고명세" in s:
        return "purchase_detail"

    if "세금계산서" in s:
        return "tax_doc"

    if "거래명세서" in s:
        return "trans_doc"

    if (
        "품목별매출추세분석" in s
        or "품목별매출추세요약표" in s
        or "품목별매출예상" in s
        or "제약사별매출추세분석" in s
        or "제약사별매출추세분석요약표" in s
        or "제조사별매출추세분석" in s
        or "제조사별매출추세분석요약표" in s
        or "품목별재고부족현황" in s
        or "??????????" in s
    ):
        return "analytics_kpi"

    return "generic"



def _current_followup_kind_label(kind: str, source_action: str) -> str:
    label_map = {
        "validation": "검증",
        "monthly_stock": "월집계",
        "inventory": "제품재고현황",
        "stock_ledger": "제품수불현황",
        "sales_detail": "출고명세",
        "purchase_detail": "입고명세",
        "tax_doc": "세금계산서 공통",
        "trans_doc": "거래명세서 공통",
        "analytics_kpi": "분석/KPI",
    }
    return label_map.get(kind, str(source_action or "현재표"))


def _current_followup_hint(kind: str) -> str:
    hint_map = {
        "validation": (
            "검증 현재표에서는 불일치/일치/거래처별/제품별/월별 검증 요약처럼 "
            "검증 결과에 포함된 컬럼 기준의 후속분석을 사용해 주세요."
        ),
        "monthly_stock": (
            "월집계 현재표에서는 월별/제품별/재고위치별/입고수량/출고수량/재고수량 "
            "기준 후속분석을 사용해 주세요."
        ),
        "inventory": (
            "제품재고현황 현재표에서는 제품별/제조사별/재고위치별/재고수량/0 이하/0 이상 "
            "기준 후속분석을 사용해 주세요."
        ),
        "stock_ledger": (
            "제품수불현황 현재표에서는 일자별/월별/입고수량/출고수량/재고수량 "
            "기준 후속분석을 사용해 주세요."
        ),
        "sales_detail": (
            "출고명세 현재표에서는 거래처별/제품별/영업사원별/일자별/월별 매출금액, "
            "출고수량, 매출횟수 후속분석을 사용해 주세요. 매입/입고 분석은 입고명세 조회 후 실행해야 합니다."
        ),
        "purchase_detail": (
            "입고명세 현재표에서는 거래처별/매입처별/제품별/일자별/월별 매입금액, "
            "입고수량, 입고횟수 후속분석을 사용해 주세요. 매출/출고 분석은 출고명세 조회 후 실행해야 합니다."
        ),
        "tax_doc": (
            "세금계산서 공통 현재표에서는 거래처별/일자별/월별/세금계산서구분별 계산서금액 "
            "후속분석을 사용해 주세요. 제품별 분석은 입고명세 또는 출고명세 조회 후 실행해야 합니다."
        ),
        "trans_doc": (
            "거래명세서 공통 현재표에서는 거래처별/일자별/월별/거래명세서구분별 거래금액 "
            "후속분석을 사용해 주세요. 제품별/입고수량/출고수량 분석은 입고명세 또는 출고명세 조회 후 실행해야 합니다."
        ),
        "analytics_kpi": (
            "분석/KPI 현재표에서는 추세판정별/예상등급별/부족등급별/제조사별/거래처별 등 "
            "현재 분석표에 포함된 컬럼 기준의 후속분석을 사용해 주세요."
        ),
    }
    return hint_map.get(kind, "현재표 원본 action에 맞는 후속분석 질문을 사용해 주세요.")


def _push_dispatch_notice(
    *,
    helpers: dict[str, Callable[..., Any]],
    title: str,
    action: str,
    message: str,
    query_summary: str,
    source_query: str,
) -> bool:
    push_notice = helpers.get("push_notice")
    if not callable(push_notice):
        return False
    return bool(
        push_notice(
            title=title,
            action=action,
            message=message,
            query_summary=query_summary,
            source_query=source_query,
        )
    )


def _known_action_handlers() -> dict[str, Callable[..., Any]]:
    return {
        "validation": handle_validation_followup,
        "monthly_stock": handle_monthly_stock_followup,
        "inventory": handle_inventory_followup,
        "stock_ledger": handle_stock_ledger_followup,
        "sales_detail": handle_sales_detail_followup,
        "purchase_detail": handle_purchase_detail_followup,
        "tax_doc": handle_tax_doc_followup,
        "trans_doc": handle_trans_doc_followup,
        "analytics_kpi": handle_analytics_kpi_followup,
    }


def handle_current_table_followup_by_action(
    *,
    df: pd.DataFrame,
    query: str,
    top_n: int,
    table_key: str,
    source_action: str,
    helpers: dict[str, Callable[..., Any]],
    log: Any,
) -> bool:
    """
    현재표 후속질문을 원본 action 기준으로 먼저 처리한다.

    True  = action 전용 handler가 처리 완료 또는 notice 표시 완료
    False = generic/legacy 처리 가능

    중요:
    - source_action으로 kind가 확정된 경우에는 다른 action의 legacy 로직으로 넘기지 않는다.
    - handler가 False를 반환하거나 예외가 나도 사용자에게 notice를 표시해서
      질문에 답 없이 끝나는 일을 막는다.
    """
    kind = detect_current_table_kind(source_action)

    if kind == "generic":
        # generic/마스터류는 기존 전용 요약을 먼저 시도하고,
        # 처리하지 못한 경우에만 공통 컬럼 필터를 적용한다.
        try:
            handled = bool(
                handle_generic_followup(
                    df=df,
                    query=query,
                    top_n=top_n,
                    table_key=table_key,
                    source_action=source_action,
                    helpers=helpers,
                    log=log,
                )
            )
            if handled:
                return True
        except Exception:
            try:
                log.exception("[chat.followup_table] generic handler failed")
            except Exception:
                pass

        try:
            handled = bool(
                handle_common_column_group_followup(
                    df=df,
                    query=query,
                    top_n=top_n,
                    table_key=table_key,
                    source_action=source_action,
                    helpers=helpers,
                    log=log,
                )
            )
            if handled:
                return True
        except Exception:
            try:
                log.exception("[chat.followup_table] common column group failed")
            except Exception:
                pass

        try:
            return bool(
                handle_common_column_filter_followup(
                    df=df,
                    query=query,
                    top_n=top_n,
                    table_key=table_key,
                    source_action=source_action,
                    helpers=helpers,
                    log=log,
                )
            )
        except Exception:
            try:
                log.exception("[chat.followup_table] common column filter failed")
            except Exception:
                pass
            return False

    try:
        log.info(
            "[chat.followup.dispatch] kind=%s source_action=%r query=%r rows=%s table_key=%s",
            kind,
            source_action,
            query,
            len(df) if isinstance(df, pd.DataFrame) else -1,
            table_key,
        )
    except Exception:
        pass

    handlers = _known_action_handlers()
    handler = handlers.get(kind)

    # 아직 action 전용 모듈이 없는 generic만 기존 legacy 현재표 후속분석으로 내려간다.
    if handler is None:
        return False

    label = _current_followup_kind_label(kind, source_action)

    # "현재표 <컬럼명> <값> 상세히" 형태는 action별 미지원 안내보다 먼저
    # 실제 현재표 df.columns 기반 공통 필터 상세표로 처리한다.
    try:
        if handle_common_column_filter_followup(
            df=df,
            query=query,
            top_n=top_n,
            table_key=table_key,
            source_action=source_action,
            helpers=helpers,
            log=log,
        ):
            return True
    except Exception:
        try:
            log.exception("[chat.followup_table] common column filter failed kind=%s", kind)
        except Exception:
            pass

    # "현재표 추세판정 집계"처럼 현재 DataFrame의 실제 컬럼 기준으로
    # 처리할 수 있는 그룹 요청은 분석/KPI 전용 handler의 제품/기준월 필수
    # 안내보다 먼저 공통 집계로 처리한다.
    try:
        if handle_common_column_group_followup(
            df=df,
            query=query,
            top_n=top_n,
            table_key=table_key,
            source_action=source_action,
            helpers=helpers,
            log=log,
        ):
            return True
    except Exception:
        try:
            log.exception("[chat.followup_table] common column group failed kind=%s", kind)
        except Exception:
            pass

    try:
        handled = bool(
            handler(
                df=df,
                query=query,
                top_n=top_n,
                table_key=table_key,
                source_action=source_action,
                helpers=helpers,
                log=log,
            )
        )
    except Exception as e:
        try:
            log.exception("[chat.followup_table] %s handler failed", kind)
        except Exception:
            pass

        return _push_dispatch_notice(
            helpers=helpers,
            title=f"현재표 {label} 후속분석 오류",
            action=f"현재표 {label} 후속분석 오류",
            message=(
                f"현재표 원본은 [{source_action}]입니다.\n"
                "해당 action 전용 후속분석 처리 중 오류가 발생했습니다.\n"
                "질문에 답이 없이 끝나지 않도록 오류 메시지로 표시합니다.\n\n"
                f"질문: {query}\n"
                f"오류: {type(e).__name__}: {e}"
            ),
            query_summary=f"현재표 / {label} 후속분석 오류 / 원본={source_action}",
            source_query=query,
        )

    if handled:
        return True

    # action 전용 handler가 처리하지 못한 경우에도,
    # "현재표 <컬럼명> <값> 상세히" 형태는 모든 현재표에서 공통 필터로 처리한다.
    try:
        if handle_common_column_filter_followup(
            df=df,
            query=query,
            top_n=top_n,
            table_key=table_key,
            source_action=source_action,
            helpers=helpers,
            log=log,
        ):
            return True
    except Exception:
        try:
            log.exception("[chat.followup_table] common column filter failed kind=%s", kind)
        except Exception:
            pass

    # action kind가 확정된 현재표 질문은 legacy로 넘기지 않는다.
    # 예: 입고명세 현재표에서 '매출거래처별 매출금액'을 물으면
    #     매입표로 오답을 만들지 말고 명확한 notice를 낸다.
    return _push_dispatch_notice(
        helpers=helpers,
        title=f"현재표 {label} 후속분석 미지원",
        action=f"현재표 {label} 후속분석 미지원",
        message=(
            f"현재표 원본은 [{source_action}]입니다.\n"
            "이 질문은 현재표 원본 action의 전용 후속분석에서 처리하지 못했습니다.\n"
            "다른 action의 분석으로 임의 변환하지 않고 중단합니다.\n\n"
            f"질문: {query}\n\n"
            f"{_current_followup_hint(kind)}"
        ),
        query_summary=f"현재표 / {label} 후속분석 미지원 / 원본={source_action}",
        source_query=query,
    )
