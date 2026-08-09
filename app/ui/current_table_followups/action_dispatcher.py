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


def normalize_current_table_action(value: Any) -> str:
    """현재표 action 비교용 정규화. 의미 있는 문구는 바꾸지 않고 공백 차이만 흡수한다."""
    return re.sub(r"\s+", " ", str(value or "").strip())


def _strip_current_table_referents(compact: str) -> str:
    """Remove words that only point at the current table before intent checks."""
    out = str(compact or "")
    referents = ("현재표", "현재조회결과", "현재결과")
    suffixes = ("에서", "으로", "로", "를", "을", "만", "의", "가", "는", "은")
    for referent in referents:
        for suffix in suffixes:
            out = out.replace(referent + suffix, "")
        out = out.replace(referent, "")
    return out


def current_table_analysis_query_matches(stored_query: Any, current_query: Any) -> bool:
    """Return True only when the one-shot analysis context belongs to this request."""
    stored = re.sub(r"\s+", " ", str(stored_query or "").strip())
    current = re.sub(r"\s+", " ", str(current_query or "").strip())
    return bool(stored and current and stored == current)


def _is_current_table_analysis_ctx_expired(ctx: Any) -> bool:
    """Reuse existing expired/payload_expired style flags when they are present."""
    if not isinstance(ctx, dict):
        return False

    def _truthy_expired(value: Any) -> bool:
        if value is True:
            return True
        if value is False or value is None:
            return False
        if isinstance(value, (int, float)):
            return value == 1
        normalized = str(value).strip().lower()
        if normalized in ("true", "1", "yes", "expired"):
            return True
        if normalized in ("", "false", "0", "no", "none", "null"):
            return False
        return False

    for key in ("expired", "payload_expired", "is_expired", "data_expired", "table_expired"):
        if _truthy_expired(ctx.get(key)):
            return True
    status = str(ctx.get("status") or ctx.get("payload_status") or "").strip().lower()
    return status == "expired"


def classify_current_table_followup_intent(query: str) -> str:
    """
    현재표 후속질문을 표 생성 요청과 일반 LLM 분석 요청으로 분리한다.

    반환값:
    - dataframe_table: pandas handler가 처리해야 하는 명시적 표/집계/필터 요청
    - llm_analysis: current table-scoped context로 LLM 분석해야 하는 서술형 요청
    - empty: 비어 있는 요청
    """
    text = str(query or "").strip()
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return "empty"

    intent_compact = _strip_current_table_referents(compact)

    numeric_condition_re = re.compile(
        r"\d[\d,]*(?:\.\d+)?(?:만원|억원|천원|원|개|건|%|퍼센트|박스|병|정|EA|ea)?(?:이상|이하|초과|미만)"
    )
    if numeric_condition_re.search(intent_compact):
        return "dataframe_table"

    explicit_table_markers = (
        "표로",
        "표를",
        "표만",
        "테이블",
        "목록",
        "리스트",
        "상세표",
        "TOP",
        "top",
        "상위",
        "하위",
        "정렬",
        "필터",
        "조건",
        "같음",
        "동일",
        "=",
        "불일치목록",
        "요약표",
        "집계표",
    )
    if any(marker in intent_compact for marker in explicit_table_markers):
        return "dataframe_table"

    current_table_summary_dimensions = (
        "추세판정별",
        "제조사별",
        "제조사명별",
        "예상등급별",
        "제품그룹별",
        "제품구분별",
        "제품분류별",
        "판정결과별",
    )
    current_table_summary_requests = ("요약", "집계", "분석", "매출")
    if (
        any(dimension in intent_compact for dimension in current_table_summary_dimensions)
        and any(request in intent_compact for request in current_table_summary_requests)
    ):
        return "dataframe_table"

    dimension_markers = (
        "거래처별",
        "제품별",
        "품목별",
        "매입처별",
        "매출처별",
        "제조사별",
        "재고위치별",
        "영업사원별",
        "담당자별",
        "월별",
        "일자별",
        "날짜별",
        "요일별",
        "기간별",
        "연도별",
    )
    aggregate_markers = ("집계", "집계해", "합계", "건수", "금액", "수량")
    if any(dim in intent_compact for dim in dimension_markers) and any(agg in intent_compact for agg in aggregate_markers):
        return "dataframe_table"

    analysis_phrase_markers = (
        "이상항목",
        "문제점",
        "주의사항",
        "확인할점",
        "특징",
        "경향",
    )
    if any(marker in intent_compact for marker in analysis_phrase_markers):
        return "llm_analysis"

    analysis_markers = (
        "분석",
        "요약",
        "요약해",
        "특징",
        "경향",
        "이상항목",
        "문제점",
        "주의사항",
        "확인할점",
        "알려줘",
        "설명",
        "의견",
    )
    if any(marker in intent_compact for marker in analysis_markers):
        return "llm_analysis"

    return "dataframe_table"


def current_table_analysis_ctx_mismatch(
    ctx: Any,
    table_key: str = "",
    action: str = "",
) -> str:
    """current source와 analysis context가 일치하지 않으면 사유를 반환한다."""
    if not isinstance(ctx, dict) or ctx.get("kind") != "SIMS_ANALYSIS_CONTEXT_V1":
        return "missing_context"
    if _is_current_table_analysis_ctx_expired(ctx):
        return "expired_context"

    expected_table_key = str(table_key or "").strip()
    selected_table_key = str(ctx.get("table_key") or ctx.get("source_table_key") or "").strip()
    if expected_table_key and selected_table_key != expected_table_key:
        return "table_key_mismatch" if selected_table_key else "context_table_key_missing"

    expected_action = normalize_current_table_action(action)
    selected_action = normalize_current_table_action(ctx.get("action"))
    if expected_action and not selected_action:
        return "context_action_missing"
    if expected_action and selected_action and expected_action != selected_action:
        return "action_mismatch"

    return ""


def select_current_table_analysis_context(session_state: Any) -> tuple[dict[str, Any] | None, str, str]:
    """
    현재 source table_key/action에 정확히 연결된 analysis context만 선택한다.

    반환값: (context, source, reason)
    - context가 None이면 source는 빈 문자열이고 reason에 차단 사유가 들어간다.
    - 후보는 __sims_analysis_ctx_by_table_key와 __sims_current_table_source_analysis_ctx만 사용한다.
    """
    try:
        table_key = str(session_state.get("__sims_current_table_source_key") or "").strip()
        action = str(session_state.get("__sims_current_table_source_action") or "").strip()
    except Exception:
        return None, "", "invalid_session_state"

    if not table_key:
        return None, "", "missing_current_table_key"
    if not action:
        return None, "", "missing_current_action"

    candidates: list[tuple[str, Any]] = []
    try:
        cache = session_state.get("__sims_analysis_ctx_by_table_key")
        if isinstance(cache, dict):
            candidates.append(("cache", cache.get(table_key)))
    except Exception:
        pass

    for source_name, key in (
        ("current_source", "__sims_current_table_source_analysis_ctx"),
    ):
        try:
            candidates.append((source_name, session_state.get(key)))
        except Exception:
            pass

    last_reason = "missing_context"
    for source_name, ctx in candidates:
        reason = current_table_analysis_ctx_mismatch(ctx, table_key, action)
        if not reason and isinstance(ctx, dict):
            return dict(ctx), source_name, ""
        if reason != "missing_context":
            last_reason = reason

    return None, "", last_reason


def detect_current_table_kind(source_action: str) -> str:
    s = re.sub(r"\s+", "", str(source_action or ""))

    if "검증" in s:
        return "validation"

    if "실재고월집계" in s or "장부재고월집계" in s:
        return "monthly_stock"

    if "제품재고현황" in s or "제품재고" in s or "현재고조회" in s:
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

    if s.startswith("현재표") and any(
        marker in s
        for marker in ("매출", "예상", "재고부족", "부족등급", "추세판정", "판정결과")
    ):
        return "analytics_kpi"

    return "generic"


_CURRENT_TABLE_DIMENSION_SPECS: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("product", "제품", ("제품별", "품목별"), ("제품명", "품목명", "상품명", "제품코드", "품목코드", "상품코드")),
    ("manufacturer", "제조사", ("제조사별", "제약사별", "제조사명별", "제약사명별", "제조사분석"), ("제조사명", "제조사", "제약사명", "제약사")),
    ("product_group", "제품그룹", ("제품그룹별",), ("제품그룹명", "제품그룹")),
    ("product_category", "제품구분", ("제품구분별",), ("제품구분명", "제품구분")),
    ("product_class", "제품분류", ("제품분류별",), ("제품분류명", "제품분류")),
    ("forecast_grade", "예상등급", ("예상등급별",), ("예상등급",)),
    ("trend_judgement", "추세판정", ("추세판정별",), ("추세판정",)),
    ("judgement_result", "판정결과", ("판정결과별",), ("판정결과",)),
)

_CURRENT_TABLE_METRIC_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "sales",
        "매출액",
        (
            "매출합계",
            "총매출액",
            "총매출공급가액",
            "매출공급가액",
            "매출금액",
            "매출액",
            "월시점 실제매출",
        ),
    ),
    (
        "shortage",
        "부족등급 또는 부족수량 또는 현재재고수량",
        (
            "부족등급",
            "1개월부족수량",
            "2개월부족수량",
            "3개월부족수량",
            "부족예상수량",
            "배정부족예상수량",
            "현재재고수량",
        ),
    ),
    (
        "forecast_sales",
        "예상매출",
        (
            "당월 예상매출",
            "월시점 예상매출",
            "평가월 예상매출",
            "예상매출",
        ),
    ),
)

_CURRENT_TABLE_METRIC_GROUPING_SUPPORT: dict[str, frozenset[str]] = {
    "sales": frozenset(
        {
            "product",
            "manufacturer",
            "product_group",
            "product_category",
            "product_class",
            "forecast_grade",
            "trend_judgement",
            "judgement_result",
        }
    ),
    "shortage": frozenset(
        {
            "product",
            "manufacturer",
            "product_group",
            "product_category",
            "product_class",
            "forecast_grade",
        }
    ),
    "forecast_sales": frozenset(
        {
            "product",
            "manufacturer",
            "product_group",
            "product_category",
            "product_class",
            "forecast_grade",
            "trend_judgement",
            "judgement_result",
        }
    ),
}


def _requested_current_table_dimensions(query: str) -> list[tuple[str, str, tuple[str, ...]]]:
    compact = re.sub(r"\s+", "", str(query or ""))
    requested: list[tuple[int, tuple[str, str, tuple[str, ...]]]] = []
    for key, label, phrases, aliases in _CURRENT_TABLE_DIMENSION_SPECS:
        positions = [compact.find(phrase) for phrase in phrases if compact.find(phrase) >= 0]
        if positions:
            requested.append((min(positions), (key, label, aliases)))
    return [item for _position, item in sorted(requested, key=lambda entry: entry[0])]


def _current_table_dimension_columns(df: pd.DataFrame, aliases: tuple[str, ...]) -> list[str]:
    normalized_aliases = {re.sub(r"\s+", "", alias) for alias in aliases}
    return [
        str(column)
        for column in df.columns
        if re.sub(r"\s+", "", str(column)) in normalized_aliases
    ]


def _current_table_metric_columns(df: pd.DataFrame, metric: str) -> list[str]:
    for key, _label, aliases in _CURRENT_TABLE_METRIC_SPECS:
        if key == metric:
            return _current_table_dimension_columns(df, aliases)
    return []


def _current_table_metric_label(metric: str) -> str:
    for key, label, _aliases in _CURRENT_TABLE_METRIC_SPECS:
        if key == metric:
            return label
    return ""


def _current_table_requested_metrics(query: str) -> list[str]:
    compact = re.sub(r"\s+", "", str(query or ""))
    metrics: list[str] = []
    if "예상매출" in compact:
        metrics.append("forecast_sales")
    elif "매출" in compact:
        metrics.append("sales")
    if "부족" in compact:
        metrics.append("shortage")
    # 재고부족은 shortage 단일 metric이다. 단순 재고는 별도 공식 metric이
    # 아직 없으므로 stock으로 보존한 뒤 capability에서 명시적으로 차단한다.
    # `재고수량`은 제품수불/재고표의 기존 수량 열 이름이다. 독립 재고
    # metric 요청으로 오인하면 영업사원별 재고수량 TOP 같은 정상 집계를 막는다.
    stock_quantity_terms = ("재고수량", "현재재고수량", "최종재고수량")
    if (
        "재고" in compact
        and "부족" not in compact
        and not any(term in compact for term in stock_quantity_terms)
    ):
        metrics.append("stock")
    return metrics


def _is_sales_trend_current_table(source_action: str) -> bool:
    return "매출추세" in re.sub(r"\s+", "", str(source_action or ""))


def _current_table_source_metric_hint(
    source_action: str,
    df: pd.DataFrame | None = None,
    source_meta: dict[str, Any] | None = None,
) -> str:
    """Infer a metric only from an unambiguous source action and real metric column."""
    if isinstance(source_meta, dict):
        persisted_metric = str(
            source_meta.get("result_metric") or source_meta.get("source_metric") or ""
        ).strip()
        if persisted_metric in _CURRENT_TABLE_METRIC_GROUPING_SUPPORT:
            if df is None or _current_table_metric_columns(df, persisted_metric):
                return persisted_metric

    compact = re.sub(r"\s+", "", str(source_action or ""))
    if "재고부족" in compact:
        candidate = "shortage"
    elif "예상매출" in compact:
        candidate = "forecast_sales"
    elif "매출추세" in compact or ("현재표" in compact and "매출" in compact):
        candidate = "sales"
    else:
        return ""
    if df is not None and not _current_table_metric_columns(df, candidate):
        return ""
    return candidate


def _is_product_top_request(query: str) -> bool:
    compact = re.sub(r"\s+", "", str(query or ""))
    return (
        any(word in compact for word in ("제품", "품목"))
        and any(marker in compact for marker in ("TOP", "top", "상위"))
    )


def _current_table_followup_intent(
    query: str,
    source_action: str = "",
    df: pd.DataFrame | None = None,
    source_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requested_dimensions = _requested_current_table_dimensions(query)
    metrics = _current_table_requested_metrics(query)
    source_metric = _current_table_source_metric_hint(source_action, df, source_meta)
    if not metrics and source_metric and (
        _is_product_top_request(query) or len(requested_dimensions) == 1
    ):
        # 질문이 지표를 생략한 경우에도 원본 action과 실제 컬럼이 함께 하나의
        # 공식 지표를 확정할 때만 이를 재사용한다.
        metrics.append(source_metric)
    groupings = [key for key, _label, _aliases in requested_dimensions]
    if not groupings and len(metrics) == 1:
        inferred_grouping = _current_table_requested_grouping(query, metrics[0], source_action)
        if inferred_grouping:
            groupings.append(inferred_grouping)
    return {
        "requested_metrics": metrics,
        "requested_groupings": groupings,
        "requested_metric": metrics[0] if len(metrics) == 1 else "",
        "requested_grouping": groupings[0] if len(groupings) == 1 else "",
        "requested_dimensions": requested_dimensions,
    }


def _current_table_requested_grouping(query: str, metric: str, source_action: str = "") -> str:
    requested = _requested_current_table_dimensions(query)
    if len(requested) == 1:
        return requested[0][0]

    # "재고부족 품목 TOP"은 문장상 "품목별"이 생략됐어도 제품 단위
    # 부족 순위를 요구한다. 일반 제품 단어만으로 차원을 추정하지 않는다.
    compact = re.sub(r"\s+", "", str(query or ""))
    if metric == "shortage" and any(word in compact for word in ("제품", "품목")):
        return "product"
    if metric == "shortage" and any(word in compact for word in ("TOP", "top", "상위")):
        return "product"
    if metric == "sales" and _is_sales_trend_current_table(source_action) and _is_product_top_request(query):
        return "product"
    return ""


def _looks_like_formatted_amount(value: Any) -> bool:
    """Detect a money-formatted value in a product-name output without flagging normal codes/names."""
    if value is None or pd.isna(value):
        return False
    text = str(value).strip()
    return bool(re.fullmatch(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}", text))


def _current_table_result_contract_error(
    df: Any,
    capability: dict[str, Any],
) -> str:
    """Return a stable reason when a metric/grouping result cannot be delivered safely."""
    if not capability.get("requires_result_contract"):
        return ""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return ""

    grouping = str(capability.get("requested_grouping") or "")
    metric = str(capability.get("requested_metric") or "")
    dimension_aliases = next(
        (aliases for key, _label, _phrases, aliases in _CURRENT_TABLE_DIMENSION_SPECS if key == grouping),
        (),
    )
    if not _current_table_dimension_columns(df, dimension_aliases):
        return "result_grouping_missing"
    if not _current_table_metric_columns(df, metric):
        return "result_metric_missing"

    if grouping == "product":
        product_code_cols = _current_table_dimension_columns(df, ("제품코드", "품목코드", "상품코드"))
        product_name_cols = _current_table_dimension_columns(df, ("제품명", "품목명", "상품명"))
        if not product_code_cols and not product_name_cols:
            return "result_product_missing"
        product_key = product_code_cols[0] if product_code_cols else product_name_cols[0]
        if df[product_key].astype(str).str.strip().duplicated().any():
            return "result_product_grain_mismatch"
        if product_name_cols and df[product_name_cols[0]].map(_looks_like_formatted_amount).any():
            return "result_product_name_metric_contamination"
        if product_code_cols and product_name_cols:
            mapping_counts = (
                df[[product_code_cols[0], product_name_cols[0]]]
                .dropna(subset=[product_code_cols[0]])
                .groupby(product_code_cols[0], dropna=False)[product_name_cols[0]]
                .nunique(dropna=True)
            )
            if (mapping_counts > 1).any():
                return "result_product_code_name_mapping_mismatch"
    return ""


def _current_table_followup_capability(
    *,
    df: pd.DataFrame,
    query: str,
    source_action: str,
    kind: str,
    source_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    intent = _current_table_followup_intent(query, source_action, df, source_meta)
    metrics = intent["requested_metrics"]
    groupings = intent["requested_groupings"]
    metric = intent["requested_metric"]
    grouping = intent["requested_grouping"]
    requested = intent["requested_dimensions"]
    missing_columns: list[str] = []
    available_columns: list[str] = []
    for _, label, aliases in requested:
        matches = _current_table_dimension_columns(df, aliases)
        if matches:
            available_columns.extend(matches)
        else:
            missing_columns.append(label)

    metric_columns = _current_table_metric_columns(df, metric)
    base = {
        "requested_metrics": metrics,
        "requested_groupings": groupings,
        "requested_metric": metric,
        "requested_grouping": grouping,
        "missing_columns": missing_columns,
        "available_columns": available_columns,
        "metric_columns": metric_columns,
        "issue_codes": [],
    }
    compact_query = re.sub(r"\s+", "", str(query or ""))
    if (
        any(token in compact_query for token in ("판정결과", "추세판정"))
        and any(token in compact_query for token in ("TOP", "top", "상위"))
        and not grouping
    ):
        return {
            **base,
            "status": "unsupported",
            "requires_result_contract": False,
            "issue_codes": ["filter_grouping_required"],
        }
    if len(metrics) > 1 or len(groupings) > 1:
        return {
            **base,
            "status": "unsupported",
            "requires_result_contract": False,
            "issue_codes": ["multiple_metric_or_grouping_unsupported"],
        }
    if metric == "stock" and not (kind == "inventory" and grouping == "product"):
        return {
            **base,
            "status": "unsupported",
            "requires_result_contract": False,
            "issue_codes": ["stock_metric_undefined"],
        }
    # A requested metric must be present even when the question does not name a
    # grouping. Otherwise a generic TOP path could silently rank another value.
    metric_missing = bool(metric and not metric_columns)
    if missing_columns:
        return {
            **base,
            "status": "column_unavailable",
            "requires_result_contract": bool(metric and grouping),
        }

    if metric_missing:
        return {
            **base,
            "status": "column_unavailable",
            "missing_columns": [_current_table_metric_label(metric)],
            "metric_columns": [],
            "requires_result_contract": True,
        }

    if requested and not any(
        df[column].notna().astype(bool).any()
        and df[column].astype(str).str.strip().replace({"nan": "", "None": "", "<NA>": ""}).ne("").any()
        for column in available_columns
    ):
        return {
            **base,
            "status": "no_data",
            "requires_result_contract": bool(metric and grouping),
        }

    if (
        kind == "analytics_kpi"
        and metric
        and grouping
        and grouping not in _CURRENT_TABLE_METRIC_GROUPING_SUPPORT.get(metric, frozenset())
    ):
        return {
            **base,
            "status": "unsupported",
            "requires_result_contract": True,
        }

    if kind in {"sales_detail", "purchase_detail"} and len(requested) > 1:
        return {
            **base,
            "status": "unsupported",
            "requested_grouping": "",
            "requires_result_contract": False,
        }

    return {
        **base,
        "status": "success",
        "requires_result_contract": bool(metric and grouping),
    }


def _current_table_exact_filter_value(df: pd.DataFrame, column: str, query: str) -> str:
    """Return one exact, maximal judgement value mentioned by the user."""
    compact = re.sub(r"\s+", "", str(query or ""))
    values = [
        str(value).strip()
        for value in df[column].dropna().astype("string").tolist()
        if str(value).strip() and str(value).strip().lower() not in {"nan", "none", "<na>"}
    ]
    unique_values = list(dict.fromkeys(values))
    matches = [value for value in unique_values if re.sub(r"\s+", "", value) in compact]
    maximal = [
        value for value in matches
        if not any(
            value != other
            and re.sub(r"\s+", "", value) in re.sub(r"\s+", "", other)
            for other in matches
        )
    ]
    return maximal[0] if len(maximal) == 1 else ""


def _has_unlabeled_judgement_value(query: str) -> bool:
    compact = re.sub(r"\s+", "", str(query or ""))
    return any(
        re.sub(r"\s+", "", value) in compact
        for value in ("감소", "안정", "증가", "신규/증가", "자료부족", "반품주의")
    )


def _current_table_product_top_filter(
    *,
    df: pd.DataFrame,
    query: str,
    source_action: str,
    source_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply a canonical trend-judgement filter before an analytics product TOP."""
    result = {
        "df": df,
        "status": "success",
        "filter_column": "",
        "filter_value": "",
        "missing_columns": [],
        "issue_codes": [],
    }
    if (
        _current_table_source_metric_hint(source_action, df, source_meta) != "sales"
        or not _is_product_top_request(query)
    ):
        return result

    compact = re.sub(r"\s+", "", str(query or ""))

    explicit_columns = []
    explicit_label = ""
    if "판정결과" in compact:
        explicit_columns = _current_table_dimension_columns(df, ("판정결과",))
        explicit_label = "판정결과"
    elif "추세판정" in compact:
        explicit_columns = _current_table_dimension_columns(df, ("추세판정",))
        explicit_label = "추세판정"

    if explicit_label and not explicit_columns:
        return {
            **result,
            "status": "column_unavailable",
            "missing_columns": [explicit_label],
            "issue_codes": ["filter_column_missing"],
        }

    judgement_columns = explicit_columns
    if not explicit_label:
        judgement_columns = [
            column
            for column in _current_table_dimension_columns(df, ("추세판정", "판정결과"))
            if _current_table_exact_filter_value(df, column, query)
        ]
        if not judgement_columns:
            if not _has_unlabeled_judgement_value(query):
                return result
            return {
                **result,
                "status": "unsupported",
                "issue_codes": ["filter_value_not_found"],
            }
    if len(judgement_columns) != 1:
        return {
            **result,
            "status": "unsupported" if judgement_columns else "column_unavailable",
            "missing_columns": ([] if judgement_columns else [explicit_label or "판정 컬럼"]),
            "issue_codes": ["filter_column_ambiguous" if judgement_columns else "filter_column_missing"],
        }

    column = judgement_columns[0]
    filter_value = _current_table_exact_filter_value(df, column, query)
    if not filter_value:
        return {
            **result,
            "status": "unsupported",
            "issue_codes": ["filter_value_ambiguous_or_missing"],
        }
    filtered = df[df[column].astype("string").str.strip().eq(filter_value)].copy()
    return {
        **result,
        "df": filtered,
        "status": "no_data" if filtered.empty else "success",
        "filter_column": column,
        "filter_value": filter_value,
    }



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
    extra_meta: dict[str, Any] | None = None,
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
            extra_meta=extra_meta,
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
    source_meta: dict[str, Any] | None = None,
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
    if (
        kind == "generic"
        and isinstance(source_meta, dict)
        and str(source_meta.get("result_metric") or "").strip()
        in _CURRENT_TABLE_METRIC_GROUPING_SUPPORT
    ):
        kind = "analytics_kpi"
    filter_result = _current_table_product_top_filter(
        df=df,
        query=query,
        source_action=source_action,
        source_meta=source_meta,
    )
    dispatch_df = filter_result["df"]
    dispatch_query = str(query or "")
    if filter_result["filter_column"]:
        # The generic analytics handler must not rediscover the already-applied
        # source filter as a second, differently named column filter.
        for token in ("추세판정", "판정결과", filter_result["filter_value"]):
            dispatch_query = dispatch_query.replace(str(token), " ")
    capability = _current_table_followup_capability(
        df=dispatch_df,
        query=query,
        source_action=source_action,
        kind=kind,
        source_meta=source_meta,
    )
    if filter_result["status"] != "success":
        capability = {
            **capability,
            "status": filter_result["status"],
            "missing_columns": filter_result["missing_columns"],
            "issue_codes": filter_result["issue_codes"],
        }
    capability_meta = {
        "source_action": source_action,
        "source_call_count": 0,
        "requested_metrics": capability["requested_metrics"],
        "requested_groupings": capability["requested_groupings"],
        "requested_metric": capability["requested_metric"],
        "requested_grouping": capability["requested_grouping"],
        "missing_columns": capability["missing_columns"],
        "issue_codes": capability["issue_codes"],
        "filter_column": filter_result["filter_column"] or str(
            (source_meta or {}).get("filter_column") or ""
        ).strip(),
        "filter_value": filter_result["filter_value"] or str(
            (source_meta or {}).get("filter_value") or ""
        ).strip(),
    }

    def _push_table_with_capability(**kwargs: Any) -> bool:
        extra_meta = dict(kwargs.pop("extra_meta", {}) or {})
        contract_error = _current_table_result_contract_error(kwargs.get("df"), capability)
        if contract_error:
            return _push_notice_with_capability(
                title="현재표 결과 계약 불일치",
                action="현재표 결과 계약 불일치",
                message=(
                    "현재표 후속분석 결과가 요청한 지표와 집계 단위를 충족하지 않아 표를 만들지 않았습니다.\n"
                    "다른 지표나 집계 결과로 대신 조회하지 않았습니다."
                ),
                query_summary=f"현재표 / 결과 계약 불일치 / 원본={source_action}",
                source_query=query,
                source_table_key=table_key,
                source_rows=len(df),
                extra_meta={
                    "execution_status": "routing_error",
                    "result_status": "routing_error",
                    "consistency_flags": ["result_contract_mismatch"],
                    "issue_codes": ["result_contract_mismatch", contract_error],
                },
            )
        extra_meta.setdefault("execution_status", "success")
        extra_meta.setdefault("result_status", "success")
        extra_meta.setdefault("result_metric", capability["requested_metric"])
        extra_meta.setdefault("result_grain", capability["requested_grouping"])
        extra_meta.setdefault(
            "intent_validation_status",
            "pass" if capability["requires_result_contract"] else "not_checked",
        )
        extra_meta.setdefault("table_created", True)
        extra_meta.setdefault("issue_codes", [])
        for key, value in capability_meta.items():
            extra_meta.setdefault(key, value)
        if filter_result["filter_column"]:
            kwargs["source_query"] = query
            title = str(kwargs.get("title") or "")
            if title.startswith("현재표 제품별"):
                kwargs["title"] = f"{filter_result['filter_column']} ‘{filter_result['filter_value']}’{title[3:]}"
                kwargs["action"] = kwargs["title"]
        kwargs.setdefault("source_table_key", table_key)
        kwargs.setdefault("source_rows", len(df))
        return bool(helpers["push_table"](**kwargs, extra_meta=extra_meta))

    def _push_notice_with_capability(**kwargs: Any) -> bool:
        extra_meta = dict(kwargs.pop("extra_meta", {}) or {})
        status = str(extra_meta.get("execution_status") or capability["status"] or "unsupported")
        extra_meta.setdefault("execution_status", status)
        extra_meta.setdefault("result_status", status)
        extra_meta.setdefault("result_metric", "")
        extra_meta.setdefault("result_grain", "")
        extra_meta.setdefault("intent_validation_status", "not_checked")
        extra_meta.setdefault("table_created", False)
        extra_meta.setdefault("issue_codes", capability["issue_codes"])
        for key, value in capability_meta.items():
            extra_meta.setdefault(key, value)
        kwargs.setdefault("source_table_key", table_key)
        kwargs.setdefault("source_rows", len(df))
        return bool(helpers["push_notice"](**kwargs, extra_meta=extra_meta))

    dispatch_helpers = dict(helpers)
    dispatch_helpers["push_table"] = _push_table_with_capability
    dispatch_helpers["push_notice"] = _push_notice_with_capability
    handlers = _known_action_handlers()
    handler = handlers.get(kind)
    normalized_query = re.sub(r"\s+", "", str(query or ""))

    # Current-stock product quantity TOP has a fixed product-identifying
    # output contract.  Let the inventory handler consume it before the
    # generic column grouper can reduce the result to quantity-only rows.
    inventory_product_stock_top = (
        kind == "inventory"
        and "재고수량" in normalized_query
        and any(marker in normalized_query for marker in ("제품별", "품목별"))
        and any(marker in normalized_query for marker in ("TOP", "top", "상위"))
    )
    if inventory_product_stock_top:
        try:
            if handler(
                df=df,
                query=query,
                top_n=top_n,
                table_key=table_key,
                source_action=source_action,
                helpers=dispatch_helpers,
                log=log,
            ):
                return True
        except Exception:
            try:
                log.exception("[chat.followup_table] inventory product stock TOP handler failed")
            except Exception:
                pass

    if capability["status"] == "column_unavailable":
        labels = ", ".join(capability["missing_columns"])
        requested_label = " / ".join(
            value
            for value in (
                _current_table_metric_label(capability["requested_metric"]),
                next(
                    (
                        label
                        for key, label, _phrases, _aliases in _CURRENT_TABLE_DIMENSION_SPECS
                        if key == capability["requested_grouping"]
                    ),
                    "",
                ),
            )
            if value
        )
        shortage_hint = ""
        if capability["requested_metric"] == "shortage":
            shortage_hint = (
                "\n먼저 `품목별 재고부족현황 2026 조회`를 실행한 뒤, "
                "새 결과표에서 부족등급별 요약 또는 재고부족 품목 TOP을 요청해 주세요."
            )
        return _push_dispatch_notice(
            helpers=dispatch_helpers,
            title="현재표 컬럼 부족",
            action="현재표 컬럼 부족",
            message=(
                f"현재표에 {labels} 컬럼이 없어 요청한 {requested_label or '집계'}를 만들 수 없습니다.\n"
                f"다른 집계 결과로 대신 조회하지 않았습니다.{shortage_hint}"
            ),
            query_summary=f"현재표 / 컬럼 부족 / 원본={source_action}",
            source_query=query,
            extra_meta={"execution_status": "column_unavailable", "result_status": "column_unavailable"},
        )

    if capability["status"] == "unsupported":
        return _push_dispatch_notice(
            helpers=dispatch_helpers,
            title="현재표 후속분석 미지원",
            action="현재표 후속분석 미지원",
            message=(
                f"현재표 원본 [{source_action}]에서는 요청한 복합 집계를 지원하지 않습니다.\n"
                "다른 집계 결과로 대신 조회하지 않았습니다."
            ),
            query_summary=f"현재표 / 미지원 / 원본={source_action}",
            source_query=query,
            extra_meta={"execution_status": "unsupported", "result_status": "unsupported"},
        )

    if capability["status"] == "no_data":
        return _push_dispatch_notice(
            helpers=dispatch_helpers,
            title="현재표 계산 결과 없음",
            action="현재표 계산 결과 없음",
            message="현재표의 요청 차원으로 계산한 결과가 없습니다.",
            query_summary=f"현재표 / 결과 없음 / 원본={source_action}",
            source_query=query,
            extra_meta={"execution_status": "no_data", "result_status": "no_data"},
        )

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
                    helpers=dispatch_helpers,
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
                    helpers=dispatch_helpers,
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
                    helpers=dispatch_helpers,
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
    normalized_query = re.sub(r"\s+", "", str(query or ""))
    requires_manufacturer_dimension = kind == "analytics_kpi" and any(
        marker in normalized_query
        for marker in ("제조사별", "제조사명별", "제조사분석")
    )
    has_manufacturer_dimension = any(
        "제조사" in str(column) or "제약사" in str(column)
        for column in df.columns
    )
    if requires_manufacturer_dimension and not has_manufacturer_dimension:
        return _push_dispatch_notice(
            helpers=dispatch_helpers,
            title="현재표 제조사별 분석 불가",
            action="현재표 제조사별 분석 불가",
            message="현재표에서 제조사명 또는 제약사명 컬럼을 찾지 못했습니다.",
            query_summary=f"현재표 / 제조사별 분석 불가 / 전체 {len(df):,}건 기준",
            source_query=query,
        )

    # 매출/재고부족처럼 지표와 집계 단위가 함께 명시된 분석/KPI 질문은
    # 제품별 매출/재고부족 TOP은 원본 행을 집계해야 하므로 일반 컬럼 TOP보다
    # action 전용 집계를 먼저 실행한다. 기존 제조사·추세판정 공통 집계는 유지한다.
    requires_metric_grouping_handler = (
        kind == "analytics_kpi"
        and capability["requires_result_contract"]
        and capability["requested_grouping"] in {"product", "manufacturer"}
        and capability["requested_metric"] in {"sales", "shortage"}
    )
    if requires_metric_grouping_handler:
        try:
            if handler(
                df=dispatch_df,
                query=dispatch_query,
                top_n=top_n,
                table_key=table_key,
                source_action=source_action,
                helpers=dispatch_helpers,
                log=log,
            ):
                return True
        except Exception:
            try:
                log.exception("[chat.followup_table] analytics metric/grouping handler failed")
            except Exception:
                pass
        return _push_dispatch_notice(
            helpers=dispatch_helpers,
            title="현재표 후속분석 미지원",
            action="현재표 후속분석 미지원",
            message=(
                "현재표에서 요청한 지표와 집계 단위를 처리하지 못했습니다.\n"
                "다른 지표나 집계 결과로 대신 조회하지 않았습니다."
            ),
            query_summary=f"현재표 / 미지원 / 원본={source_action}",
            source_query=query,
            extra_meta={
                "execution_status": "unsupported",
                "result_status": "unsupported",
                "consistency_flags": ["requested_metric_grouping_handler_unavailable"],
            },
        )

    # "현재표 <컬럼명> <값> 상세히" 형태는 action별 미지원 안내보다 먼저
    # 실제 현재표 df.columns 기반 공통 필터 상세표로 처리한다.
    try:
        if handle_common_column_filter_followup(
            df=df,
            query=query,
            top_n=top_n,
            table_key=table_key,
            source_action=source_action,
            helpers=dispatch_helpers,
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
            helpers=dispatch_helpers,
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
                helpers=dispatch_helpers,
                log=log,
            )
        )
    except Exception as e:
        try:
            log.exception("[chat.followup_table] %s handler failed", kind)
        except Exception:
            pass

        return _push_dispatch_notice(
            helpers=dispatch_helpers,
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

    # action 전용 handler가 처리하지 못한 차원별 집계/TOP은 공통 group handler로 먼저 처리한다.
    # 예: 제품수불현황 현재표에서 "영업사원별 TOP 20"은 영업사원 컬럼 자체의 row TOP이 아니라
    #     영업사원별 집계 결과를 TOP N으로 잘라야 한다.
    try:
        if handle_common_column_group_followup(
            df=df,
            query=query,
            top_n=top_n,
            table_key=table_key,
            source_action=source_action,
            helpers=dispatch_helpers,
            log=log,
        ):
            return True
    except Exception:
        try:
            log.exception("[chat.followup_table] common column group failed kind=%s", kind)
        except Exception:
            pass

    # action 전용 handler가 처리하지 못한 경우에도,
    # "현재표 <컬럼명> <값> 상세히" 형태는 모든 현재표에서 공통 필터로 처리한다.
    try:
        if handle_common_column_filter_followup(
            df=df,
            query=query,
            top_n=top_n,
            table_key=table_key,
            source_action=source_action,
            helpers=dispatch_helpers,
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
        helpers=dispatch_helpers,
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
