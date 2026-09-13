"""Read-only product statistics backed by the approved Snapshot v2.1 projection."""

from __future__ import annotations

from datetime import datetime
import logging
import time
from typing import Any, Callable, Mapping, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from app.db.mssql_client import get_current_company_id
from app.services.product_master_filter_contract import (
    normalize_product_master_filters, append_product_master_filter_clauses,
    build_product_master_enrichment_sql,
)
from app.services.dashboard_inventory_frequency_snapshot import dashboard_profile_fingerprint
from app.services.dashboard_inventory_frequency_snapshot_service import (
    read_approved_frequency_projection,
    resolve_dashboard_profile_stock_scope,
)


ACTION = "제품정보 조회"
log = logging.getLogger("ssai")
TABLE = "snapshot.frequency_product"
_GRADES = frozenset(("A", "B", "C", "D", "E", "F", "X", "unavailable"))
_LIFECYCLE = frozenset(("new_product", "established_product", "unknown_lifecycle"))
STATUS_LABELS = {
    "new_product": "신규품목", "established_product": "기존품목",
    "unknown_lifecycle": "최초 입고정보 없음", "ready": "정상",
    "invalid_lifecycle": "최초 입고정보 확인 필요",
    "stale": "오래된 자료", "unavailable": "자료 부족",
    "no_normal_outbound": "최근 3개월 정상 출고 없음",
    "product_not_in_projection": "승인자료에 제품 없음",
    "excluded_adjustment_only": "금융조정 품목(등급 제외)",
}


def load_product_information_master(params: Mapping[str, Any]) -> pd.DataFrame:
    """One bound, read-only R040 enrichment query using the shared filter contract."""
    from app.db.mssql_client import get_conn

    joins, expressions = build_product_master_enrichment_sql(
        product_alias="P", include_audit_price=False,
    )
    clauses: list[str] = []
    values: list[Any] = []
    append_product_master_filter_clauses(clauses, values, params, expressions=expressions)
    if params.get("physic_nm"):
        clauses.append("P.Rd04_Physic_Nm LIKE ?")
        values.append(f"%{params['physic_nm']}%")
    sql = f"""
SELECT LTRIM(RTRIM(P.Rd04_Physic_Cd)) AS [제품코드],
       P.Rd04_Physic_Nm AS [제품명], PV.Rd03_Ven_Nm AS [제약사],
       P.Rd04_Standard AS [규격], P.Rd04_Insu_Cd AS [보험코드],
       PG.Rd01_Hnm AS [제품그룹명], PD.Rd01_Hnm AS [구분명],
       PC.Rd01_Hnm AS [제품분류명]
FROM dbo.Rddbc040 AS P WITH (NOLOCK)
{joins}
WHERE {' AND '.join(clauses) if clauses else '1 = 1'}
"""
    with get_conn() as conn:
        driver = conn.connection.driver_connection
        previous_timeout = driver.timeout
        driver.timeout = 30
        try:
            return pd.read_sql(sql, con=conn, params=tuple(values))
        finally:
            driver.timeout = previous_timeout

_COLUMN_LABELS = {
    "product_code": "제품코드",
    "frequency_grade": "출고빈도등급",
    "profit_grade": "손익등급",
    "contribution_grade": "기여도등급",
    "lifecycle_status": "제품수명주기",
    "first_normal_inbound_month": "최초정상입고월",
    "row_status": "출고자료상태",
    "occurrence_count_3m": "3개월출고발생수",
    "outbound_day_count_3m": "3개월출고일수",
    "outbound_customer_count_3m": "3개월출고거래처수",
    "outbound_qty_3m": "3개월출고수량",
    "outbound_paid_qty_3m": "3개월유상출고수량",
    "return_event_count_3m": "3개월반품건수",
    "return_qty_3m": "3개월반품수량",
    "return_supply_amount_3m": "3개월반품공급가액",
    "avg_purchase_unit_cost": "평균매입단가",
    "purchase_price_basis_month": "매입단가기준월",
    "purchase_price_status": "매입단가상태",
    "avg_sales_unit_price": "평균매출단가",
    "sales_price_basis_month": "매출단가기준월",
    "sales_price_status": "매출단가상태",
    "estimated_unit_profit": "추정단위손익",
    "estimated_profit_rate": "추정손익률",
    "estimated_contribution_amount": "추정기여금액",
    "profitability_status": "수익성상태",
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def normalize_product_information_params(params: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    out = dict(params or {})
    out.update(normalize_product_master_filters(out))
    out["physic_nm"] = _clean(out.get("physic_nm"))
    out["physic_cd"] = _clean(out.get("physic_cd"))
    for key in ("frequency_grade", "profit_grade", "contribution_grade"):
        value = _clean(out.get(key))
        out[key] = value if value in _GRADES else ""
    lifecycle = _clean(out.get("lifecycle_status"))
    out["lifecycle_status"] = lifecycle if lifecycle in _LIFECYCLE else ""
    try:
        out["top"] = min(2000, max(1, int(out.get("top") or 500)))
    except (TypeError, ValueError):
        out["top"] = 500
    return out


def _evaluation_month(params: Mapping[str, Any]) -> str:
    value = "".join(ch for ch in _clean(params.get("evaluation_month")) if ch.isdigit())[:6]
    return value if len(value) == 6 else datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m")


def _company_id(params: Mapping[str, Any]) -> int | None:
    current = get_current_company_id()
    requested = _clean(params.get("company_id"))
    try:
        requested_id = int(requested) if requested else None
    except (TypeError, ValueError):
        return None
    if current is not None and requested_id is not None and int(current) != requested_id:
        return None
    return int(current) if current is not None else requested_id


def _empty_or_error_payload(
    *, params: Mapping[str, Any], status: str, reason: str, message: str,
    snapshot_read_call_count: int = 1,
    source_call_count: int = 0,
) -> dict[str, Any]:
    return {
        "table": TABLE,
        "title": ACTION,
        "action": ACTION,
        "params": dict(params),
        "data": message,
        "message": message,
        "final": True,
        "meta": {
            "row_count": 0,
            "row_count_total": 0,
            "result_status": status,
            "snapshot_status": status,
            "snapshot_reason": reason,
            "source_call_count": source_call_count,
            "snapshot_read_call_count": snapshot_read_call_count,
            "source_grain": "product",
        },
    }


def _get_snapshot_product_information_result(
    params: Optional[Mapping[str, Any]] = None,
    *,
    profile_resolver: Callable[..., Any] = resolve_dashboard_profile_stock_scope,
    projection_reader: Callable[..., Any] = read_approved_frequency_projection,
    master_loader: Callable[..., pd.DataFrame] = load_product_information_master,
) -> dict[str, Any]:
    qparams = normalize_product_information_params(params)
    company_id = _company_id(qparams)
    if company_id is None:
        return _empty_or_error_payload(
            params=qparams,
            status="query_error",
            reason="company_context_missing_or_mismatch",
            message="회사 범위를 확인할 수 없어 제품정보를 조회하지 않았습니다.",
            snapshot_read_call_count=0,
        )
    evaluation_month = _evaluation_month(qparams)
    try:
        scope = profile_resolver(company_id=company_id)
        projection = projection_reader(
            company_id=company_id,
            evaluation_month=evaluation_month,
            stock_codes=list(scope.stock_codes),
            product_group_codes=list(scope.product_group_codes),
            product_di_codes=list(scope.product_di_codes),
            product_class_codes=list(scope.product_class_codes),
            stock_mode=scope.stock_mode,
            product_codes=[qparams["physic_cd"]] if qparams["physic_cd"] else None,
            frequency_grade=qparams["frequency_grade"],
        )
    except Exception as exc:
        return _empty_or_error_payload(
            params=qparams,
            status="query_error",
            reason=f"snapshot_authority_unavailable:{type(exc).__name__}",
            message="승인된 제품정보 Snapshot을 확인하지 못했습니다.",
        )
    if projection.status != "ready":
        return _empty_or_error_payload(
            params=qparams,
            status="query_error",
            reason=_clean(projection.reason) or _clean(projection.status),
            message="현재 저장조건과 일치하는 승인된 제품정보 Snapshot이 없습니다.",
        )

    snapshot_meta = {
        "snapshot_manifest_id": projection.manifest_id,
        "snapshot_generation_no": projection.generation_no,
        "snapshot_contract_version": projection.contract_version,
        "profile_fingerprint": dashboard_profile_fingerprint(
            stock_codes=scope.stock_codes, product_group_codes=scope.product_group_codes,
            product_di_codes=scope.product_di_codes, product_class_codes=scope.product_class_codes,
            stock_mode=scope.stock_mode,
        ),
    }
    frame = pd.DataFrame(list(projection.rows))
    master_read = not frame.empty
    try:
        master = master_loader(qparams) if not frame.empty else pd.DataFrame()
    except Exception:
        return _empty_or_error_payload(
            params=qparams, status="query_error", reason="product_master_unavailable",
            message="제품 기본정보를 확인하지 못했습니다.",
            source_call_count=1,
        )
    if not frame.empty:
        for key in ("profit_grade", "contribution_grade", "lifecycle_status"):
            if qparams[key] and key in frame.columns:
                frame = frame.loc[frame[key].fillna("").astype(str).eq(qparams[key])]
        available = [column for column in _COLUMN_LABELS if column in frame.columns]
        frame = frame.loc[:, available].rename(columns=_COLUMN_LABELS).reset_index(drop=True)
        frame = frame.merge(master, on="제품코드", how="inner", validate="one_to_one")
        leading = [key for key in ("제품코드", "제품명", "제약사", "규격") if key in frame]
        basic = [key for key in master.columns if key not in leading]
        frame = frame.loc[:, leading + [key for key in frame if key not in leading + basic] + basic]
        for column in ("제품수명주기", "출고자료상태", "매입단가상태", "매출단가상태", "수익성상태",
                       "출고빈도등급", "손익등급", "기여도등급"):
            if column in frame:
                frame[column] = frame[column].replace(STATUS_LABELS)
        for column in ("최초정상입고월", "매입단가기준월", "매출단가기준월"):
            if column in frame:
                frame[column] = frame[column].map(lambda value: "" if pd.isna(value) else str(value))
        frame.insert(0, "조회순번", range(1, len(frame) + 1))

    filters = []
    for key, label in (
        ("physic_cd", "제품코드"),
        ("physic_nm", "제품명"), ("product_keyword", "키워드"),
        ("insu_cd", "보험코드"), ("maker_nm", "제약사"), ("barcode", "바코드"),
        ("product_group_nm", "제품그룹"), ("product_di_nm", "구분"),
        ("product_class_nm", "제품분류"),
        ("frequency_grade", "출고빈도"),
        ("profit_grade", "손익등급"),
        ("contribution_grade", "기여도등급"),
        ("lifecycle_status", "제품수명주기"),
    ):
        if qparams[key]:
            filters.append(f"{label} {STATUS_LABELS.get(qparams[key], qparams[key])}")
    condition = " / ".join(filters) if filters else "전체"
    if frame.empty:
        payload = _empty_or_error_payload(
            params=qparams,
            status="no_data",
            reason="",
            message=f"해당 조건의 제품정보가 없습니다.\n\n조회조건: {condition}",
            source_call_count=1 if master_read else 0,
        )
        payload["meta"]["snapshot_status"] = "ready"
        payload["meta"].update(
            {"manifest_id": projection.manifest_id, "generation_no": projection.generation_no, **snapshot_meta}
        )
        return payload

    display = frame.head(qparams["top"]).copy()
    summary = f"조회조건: {condition}\n\n결과: {len(frame):,}건"
    grade_lines = []
    for column in ("출고빈도등급", "손익등급", "기여도등급", "제품수명주기"):
        if column in frame:
            counts = frame[column].fillna("자료 부족").value_counts().to_dict()
            grade_lines.append(f"{column}: " + ", ".join(f"{key} {value}개" for key, value in counts.items()))
    llm_summary = summary + "\n\n" + "\n".join(grade_lines) + (
        "\n출고빈도/손익/기여도 등급은 서로 독립입니다. F는 신규품목, X는 최근 정상출고 없음입니다."
        " 자료 부족을 E/X 또는 손실로 해석하지 마세요. 추정손익률 원자료는 비율(0.1=10%)입니다."
        " 추정단위손익/추정기여금액은 관리용 추정치이며 회계 확정손익이 아닙니다."
        " 전체 등급 분포는 위 집계를 근거로 하고 일부 표 샘플을 전체로 일반화하지 마세요."
    )
    return {
        "table": TABLE,
        "title": ACTION,
        "action": ACTION,
        "params": {**qparams, "company_id": company_id, "evaluation_month": evaluation_month},
        "df": frame,
        "df_display": display,
        "records": display.to_dict(orient="records"),
        "columns": list(display.columns),
        "data": summary,
        "message": summary,
        "final": True,
        "meta": {
            "row_count": len(display),
            "row_count_total": len(frame),
            "full_source_row_count": len(frame),
            "download_row_count": len(frame),
            "result_status": "success",
            "query_summary": condition,
            "condition": condition,
            "summary_md": summary,
            "llm_summary_md": llm_summary,
            "source_table": TABLE,
            "source_mode": "approved_snapshot_projection",
            "source_grain": "product",
            "source_call_count": 1,
            "snapshot_read_call_count": 1,
            "snapshot_status": "ready",
            "snapshot_reason": "",
            "snapshot_checksum": projection.checksum,
            **snapshot_meta,
            "semantic_styled_max_rows": 300,
        },
    }


def get_snapshot_product_information_result(
    params: Optional[Mapping[str, Any]] = None,
    *,
    profile_resolver: Callable[..., Any] = resolve_dashboard_profile_stock_scope,
    projection_reader: Callable[..., Any] = read_approved_frequency_projection,
    master_loader: Callable[..., pd.DataFrame] = load_product_information_master,
) -> dict[str, Any]:
    """Emit one safe provenance/performance record for each query execution."""
    started = time.perf_counter()
    payload = _get_snapshot_product_information_result(
        params, profile_resolver=profile_resolver,
        projection_reader=projection_reader, master_loader=master_loader,
    )
    meta = payload.get("meta") or {}
    query = payload.get("params") or {}
    filters = {key: str(query[key])[:120] for key in (
        "physic_cd", "physic_nm", "product_keyword", "insu_cd", "barcode", "maker_nm", "frequency_grade", "profit_grade",
        "contribution_grade", "lifecycle_status", "product_group_nm", "product_di_nm", "product_class_nm",
    ) if query.get(key)}
    log.info(
        "[product_information.query] company_id=%s action=%s filters=%s profile=%s schema=%s "
        "manifest=%s generation=%s filtered_rows=%s display_rows=%s snapshot_reads=%s "
        "erp_source_calls=%s elapsed_ms=%s result_status=%s reason=%s",
        _company_id(query), ACTION, filters, meta.get("profile_fingerprint", ""),
        meta.get("snapshot_contract_version", ""), meta.get("snapshot_manifest_id", meta.get("manifest_id")),
        meta.get("snapshot_generation_no", meta.get("generation_no")),
        meta.get("row_count_total", 0), meta.get("row_count", 0), meta.get("snapshot_read_call_count", 0),
        meta.get("source_call_count", 0), round((time.perf_counter() - started) * 1000, 1),
        meta.get("result_status", ""), meta.get("snapshot_reason", ""),
    )
    return payload


def get_snapshot_product_information_export_df(
    params: Optional[Mapping[str, Any]] = None,
) -> pd.DataFrame:
    payload = get_snapshot_product_information_result(params)
    frame = payload.get("df")
    return frame.copy() if isinstance(frame, pd.DataFrame) else pd.DataFrame()
