"""Read-only admin view of Snapshot and monthly aggregate operation history."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pandas as pd
import streamlit as st

from app.services.snapshot_operation_history_admin import (
    KST, build_history_catalog, query_admin_history, require_history_admin, to_kst,
)
from app.ui.ssai_login import get_current_user, get_selected_company


log = logging.getLogger("ssai.snapshot.history.ui")
_KINDS = {
    "전체": None,
    "Snapshot 생성": "snapshot_generate",
    "월별 집계": "monthly_build",
    "검증": "inspect",
    "승인·게시": "approve_publish",
    "삭제": "cleanup",
}
_STATUSES = {"전체": None, "진행": "started", "단계 완료": "stage_complete",
             "성공": "succeeded", "실패": "failed", "건너뜀": "skipped"}
_RESULT_KEY = "__snapshot_history_query_result"


_STAGES = {"cleanup_committed": "삭제 완료", "draft_committed": "초안 저장",
           "approval_committed": "승인·게시 완료", "monthly_committed": "월별 집계 저장",
           "inspection_complete": "검증 완료"}
_SERVER_STATUSES = {"ready": "정상", "migration_011_missing": "이력 Migration 011 미적용",
                    "no_active_anchor": "연결 가능한 활성 회사 없음",
                    "connection_or_query_failed": "연결 또는 조회 실패"}
_TIMING_LABELS = {
    "pre_approval_inspect_generation": "승인 전 Snapshot 검사",
    "pre_approval_verification": "승인 전 Exact 검증",
    "preserve_generation_inspect": "보존 대상 검사",
    "post_approval_inspect_generation": "승인 후 Snapshot 검사",
    "post_approval_verification": "승인 후 Exact 검증",
    "preserved_after_inspect_generation": "보존 대상 사후 검사",
}


def _label(options: dict[str, str | None], value: str | None) -> str:
    return next((label for label, code in options.items() if code == value), "기록 없음")


def _present(value: Any, suffix: str = "") -> str:
    if value is None or value == "":
        return "기록 없음"
    if isinstance(value, (int, float, Decimal)):
        return f"{value:,}{suffix}"
    return f"{value}{suffix}"


def _seconds(value: Any) -> str:
    return f"{float(value) / 1000:,.2f}초" if value is not None else "기록 없음"


def _decoded(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def _grade_summary(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return "기록 없음"
    return ", ".join(f"{grade} {count:,}개" for grade, count in sorted(value.items())
                    if isinstance(grade, str) and isinstance(count, int)) or "기록 없음"


def _cleanup_ids(row: dict[str, Any]) -> tuple[str, str]:
    details = _decoded(row.get("details_json"), {})
    details = details if isinstance(details, dict) else {}
    deleted = _decoded(row.get("deleted_manifest_ids"), [])
    deleted = deleted if isinstance(deleted, list) else []
    snapshot_ids = details.get("deleted_snapshot_ids") or ([] if details.get("deleted_monthly_ids") else deleted)
    monthly_ids = details.get("deleted_monthly_ids") or []
    return (", ".join(map(str, snapshot_ids)) or "없음",
            ", ".join(map(str, monthly_ids)) or "없음")


def _result_boundary(actor: Any, current_id: int, catalog: Any) -> tuple[Any, ...]:
    return (getattr(actor, "user_id", None), getattr(actor, "login_id", None),
            getattr(actor, "user_type", None), getattr(actor, "user_grade", None),
            getattr(actor, "approval_status", None), getattr(actor, "is_active", None),
            current_id, tuple((server.key, server.anchor_company_id, server.company_ids)
                              for server in catalog.servers),
            tuple(sorted(catalog.company_servers.items())),
            tuple(sorted(catalog.company_names.items())),
            tuple(sorted(catalog.company_statuses.items())))


def _history_report(row: dict[str, Any], company_name: str) -> list[tuple[str, str]]:
    kind = row["operation_kind"]
    details = _decoded(row.get("details_json"), {})
    if not isinstance(details, dict):
        details = {}
    manifest = (f"{row['manifest_id']}/{row['generation_no']}"
                if row.get("manifest_id") is not None and row.get("generation_no") is not None
                else "기록 없음")
    report = [("회사", f"{company_name} ({row['company_id']})"),
              ("ERP 서버", str(row.get("server_label") or "기록 없음")),
              ("작업", _label(_KINDS, kind)),
              ("결과", _label(_STATUSES, row.get("result_status"))),
              ("단계", _STAGES.get(row.get("stage"), "기록 없음")),
              ("사후 등록일시" if row.get("origin") == "backfill" else "실행일시",
               to_kst(row.get("event_at")))]
    if kind == "snapshot_generate":
        report.extend([("평가월", _present(row.get("evaluation_month"))),
                       ("제품 수", _present(row.get("product_count"), "개")),
                       ("Snapshot Manifest / Generation", manifest),
                       ("등급 분포", _grade_summary(details.get("grade_counts"))),
                       ("Whole Checksum", _present(row.get("whole_checksum")))])
    elif kind == "monthly_build":
        reused = details.get("reused_months")
        report.extend([("기준월", _present(row.get("basis_month"))),
                       ("생성·재사용", "승인 월 재사용" if reused else "신규 생성"),
                       ("Fact 행수", _present(row.get("fact_count"), "행")),
                       ("월별 Manifest / Generation", manifest)])
    elif kind == "approve_publish":
        report.extend([("평가월", _present(row.get("evaluation_month"))),
                       ("Snapshot Manifest / Generation", manifest),
                       ("승인자", _present(row.get("approved_by"))),
                       ("승인 단계 완료시각", to_kst(row.get("finished_at"))),
                       ("승인 결과", "승인·게시" if row.get("approval_status") == "approved"
                        else "기록 없음")])
    elif kind == "cleanup":
        snapshot_ids, monthly_ids = _cleanup_ids(row)
        report.extend([("삭제 Snapshot Manifest", snapshot_ids),
                       ("삭제 월별 Manifest", monthly_ids),
                       ("삭제행수", _present(row.get("deleted_rows"), "행")),
                       ("예약 공간 감소", f"{float(row['reserved_delta_kib']) / 1024:,.3f} MiB"
                        if row.get("reserved_delta_kib") is not None else "기록 없음"),
                       ("정리 대상 Manifest / Generation", "위 삭제 목록" if row.get("origin") == "backfill" else manifest),
                       ("보존 Snapshot", manifest if row.get("origin") == "backfill"
                        else "별도 Reader 현황에서 확인"),
                       ("기록 출처", "사후 등록" if row.get("origin") == "backfill" else "실행 기록")])
    else:
        report.extend([("평가월", _present(row.get("evaluation_month"))),
                       ("Manifest / Generation", manifest),
                       ("Exact 결과", _present(row.get("exact_status")))])
    if kind in {"snapshot_generate", "monthly_build", "inspect", "approve_publish"}:
        if kind in {"snapshot_generate", "monthly_build"}:
            report.extend([("원천 조회시간", _seconds(row.get("source_ms"))),
                           ("계산시간", _seconds(row.get("calculation_ms"))),
                           ("저장시간", _seconds(row.get("storage_ms")))])
        timings = details.get("stage_timings_ms")
        if isinstance(timings, dict):
            report.extend((_TIMING_LABELS[name], _seconds(duration)) for name, duration in timings.items()
                          if name in _TIMING_LABELS)
    report.append(("전체 처리시간", _seconds(row.get("total_ms"))))
    if row.get("result_status") == "failed":
        report.extend([("실패 단계", _STAGES.get(row.get("stage"), _present(row.get("stage")))),
                       ("오류 원인", _present(row.get("error_code"))),
                       ("처리 상태", "실패 기록, 재처리 여부 별도 확인")])
    return report


def _detail_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    return value


def render_snapshot_operation_history_page() -> None:
    st.title("집계 작업 이력")
    actor = get_current_user()
    try:
        require_history_admin(actor)
    except PermissionError:
        st.session_state.pop(_RESULT_KEY, None)
        st.error("최고 관리자 권한이 필요합니다.")
        return
    try:
        catalog = build_history_catalog(actor)
    except Exception as exc:
        st.session_state.pop(_RESULT_KEY, None)
        log.error("[snapshot.history.ui] catalog failed type=%s", type(exc).__name__)
        st.error("등록된 ERP 서버와 회사 정보를 읽지 못했습니다.")
        return
    if not catalog.servers:
        st.session_state.pop(_RESULT_KEY, None)
        st.info("등록된 ERP 서버가 없습니다.")
        return
    selected = get_selected_company()
    current_id = int((selected or {}).get("company_id") or 0)
    boundary = _result_boundary(actor, current_id, catalog)
    cached = st.session_state.get(_RESULT_KEY)
    if not isinstance(cached, dict) or cached.get("boundary") != boundary:
        st.session_state.pop(_RESULT_KEY, None)
        cached = None
    server_labels = {server.key: server.label for server in catalog.servers}
    with st.form("__snapshot_history_filters"):
        server_col, company_col = st.columns(2)
        with server_col:
            server_mode = st.selectbox("ERP 서버", ("전체", "현재 서버", "개별 서버"))
            individual_key = (st.selectbox("서버 선택", tuple(server_labels),
                                           format_func=lambda key: server_labels[key])
                              if server_mode == "개별 서버" else None)
        if server_mode == "전체":
            servers = tuple(server_labels)
        elif server_mode == "현재 서버":
            current_key = catalog.company_servers.get(current_id)
            servers = (current_key,) if current_key else ()
        else:
            servers = (individual_key,) if individual_key else ()
        available_ids = tuple(sorted(company_id for company_id, key in catalog.company_servers.items()
                                     if key in servers))
        with company_col:
            company_mode = st.selectbox("회사 범위", ("전체", "단일", "다중"))
            label_company = lambda cid: (f"{catalog.company_names[cid]} ({cid}) · 등록 "
                                         f"{catalog.company_statuses.get(cid, '확인 불가')}")
            if company_mode == "단일":
                choices = (0,) + available_ids
                single_id = st.selectbox("회사 선택", choices,
                                         format_func=lambda cid: "선택하세요" if cid == 0 else label_company(cid))
                selected_ids = (single_id,) if single_id else ()
            elif company_mode == "다중":
                selected_ids = tuple(st.multiselect("회사 선택", available_ids, format_func=label_company))
            else:
                selected_ids = None
        start_col, end_col, kind_col, status_col = st.columns(4)
        with start_col:
            from_date = st.date_input("시작일 (KST)", value=datetime.now(KST).date() - timedelta(days=30))
        with end_col:
            to_date = st.date_input("종료일 (KST)", value=datetime.now(KST).date())
        with kind_col:
            kind_label = st.selectbox("작업 종류", tuple(_KINDS))
        with status_col:
            status_label = st.selectbox("결과", tuple(_STATUSES))
        month_col, limit_col = st.columns([2, 1])
        with month_col:
            month = st.text_input("평가월", max_chars=6)
        with limit_col:
            limit = st.selectbox("서버별 건수", (25, 50, 100), index=1)
        submitted = st.form_submit_button("조회")
    filters = (servers, selected_ids, from_date, to_date, _KINDS[kind_label],
               _STATUSES[status_label], month or None, limit)
    if submitted:
        st.session_state.pop(_RESULT_KEY, None)
        if not servers:
            st.warning("현재 회사에 등록된 ERP 서버가 없습니다. 서버를 직접 선택해 주세요.")
            return
        if selected_ids == ():
            st.warning("회사를 선택해 주세요.")
            return
        try:
            result = query_admin_history(actor, catalog, server_keys=servers,
                                         company_ids=selected_ids, from_date=from_date, to_date=to_date,
                                         kind=_KINDS[kind_label], status=_STATUSES[status_label],
                                         evaluation_month=month or None, per_server_limit=limit)
        except (ValueError, PermissionError) as exc:
            st.error(f"조회 조건을 확인해 주세요. ({type(exc).__name__})")
            return
        st.session_state[_RESULT_KEY] = {"boundary": boundary, "filters": filters, "result": result}
    elif cached is not None and cached.get("filters") == filters:
        result = cached["result"]
    else:
        st.session_state.pop(_RESULT_KEY, None)
        return
    statuses = [row["status"] for row in result["servers"]]
    ready = statuses.count("ready")
    missing = statuses.count("migration_011_missing")
    no_anchor = statuses.count("no_active_anchor")
    failed = len(statuses) - ready - missing - no_anchor
    summary_text = (f"조회 완료 {ready}개 서버 / Migration 미적용 {missing}개 / "
                    f"활성 연결 기준 없음 {no_anchor}개 / 연결·권한 오류 {failed}개")
    (st.warning if failed or no_anchor else st.info)(summary_text)
    server_rows = [{"ERP 서버": row["server"], "상태": _SERVER_STATUSES.get(row["status"], "조회 오류"),
                    "조회시간(초)": round(row["elapsed_ms"] / 1000, 2),
                    "이력 건수": row["event_count"],
                    "오류 유형": row.get("error_type") or "없음"} for row in result["servers"]]
    with st.expander("서버별 상세 상태"):
        st.dataframe(pd.DataFrame(server_rows), width="stretch", hide_index=True)
        for warning in catalog.warnings:
            st.warning(warning)
    events = result["events"]
    metrics = st.columns(3)
    metrics[0].metric("조회된 작업", f"{len(events):,}건")
    metrics[1].metric("조회된 실패", f"{sum(row['result_status'] == 'failed' for row in events):,}건")
    metrics[2].metric("최근 처리시간", _seconds(next(
        (row["total_ms"] for row in events if row.get("total_ms") is not None), None)))
    snapshots = {int(row["company_id"]): row for row in result["snapshots"]}
    monthly = {int(row["company_id"]): row for row in result["monthly"]}
    names = result["company_names"]
    summary_ids = sorted(set(snapshots) | set(monthly))
    if summary_ids:
        summary = [{"회사": f"{names.get(cid, f'회사 {cid}')} ({cid})",
                    "회사 등록 상태": catalog.company_statuses.get(cid, "확인 불가"),
                    "현재 승인 Snapshot": (f"{snapshots[cid]['manifest_id']}/{snapshots[cid]['generation_no']}"
                                         if cid in snapshots else "없음"),
                    "최신 월별 Fact": (f"{monthly[cid]['basis_month']} · {monthly[cid]['fact_count']:,}행"
                                    if cid in monthly else "없음")}
                   for cid in summary_ids]
        st.subheader("집계 현황")
        st.dataframe(pd.DataFrame(summary), width="stretch", hide_index=True)
    if not events:
        st.info("조회 조건에 해당하는 작업이 없습니다.")
        return
    st.subheader("작업 목록")
    def _preserved_snapshot(row: dict[str, Any]) -> str:
        current = snapshots.get(int(row["company_id"]))
        return (f"{current['manifest_id']}/{current['generation_no']}" if current else "기록 없음")

    display = pd.DataFrame([{
        "기록일시 (KST)": to_kst(row["event_at"]),
        "ERP 서버": row["server_label"],
        "회사": f"{names.get(int(row['company_id']), '회사')} ({row['company_id']})",
        "작업": _label(_KINDS, row["operation_kind"]),
        "작업 Manifest / Generation": (f"{row['manifest_id']}/{row['generation_no']}"
                                       if row["operation_kind"] != "cleanup" and row["manifest_id"] is not None
                                       and row["generation_no"] is not None else "해당 없음"),
        "삭제 Snapshot Manifest": _cleanup_ids(row)[0] if row["operation_kind"] == "cleanup" else "해당 없음",
        "삭제 월별 Manifest": _cleanup_ids(row)[1] if row["operation_kind"] == "cleanup" else "해당 없음",
        "현재 보존 Snapshot": _preserved_snapshot(row) if row["operation_kind"] == "cleanup" else "해당 없음",
        "처리행수": (row["deleted_rows"] if row["operation_kind"] == "cleanup"
                 else row["fact_count"] if row["fact_count"] is not None else row["product_count"]),
        "소요시간": _seconds(row["total_ms"]),
        "결과": _label(_STATUSES, row["result_status"]),
    } for row in events])
    st.dataframe(display, width="stretch", hide_index=True)
    selected_event = st.selectbox("상세 작업", events,
                                  format_func=lambda row: f"{to_kst(row['event_at'])} · "
                                  f"{names.get(int(row['company_id']), '회사')} · "
                                  f"{_label(_KINDS, row['operation_kind'])} · #{row['event_id']}")
    if selected_event:
        st.subheader("작업 상세")
        for label, value in _history_report(selected_event,
                                            names.get(int(selected_event["company_id"]), "회사")):
            st.write(f"**{label}**  {value}")
        with st.expander("기술 상세정보"):
            detail = {key: _detail_value(value) for key, value in selected_event.items()}
            detail["deleted_manifest_ids"] = _decoded(selected_event.get("deleted_manifest_ids"), [])
            detail["details"] = _decoded(selected_event.get("details_json"), {})
            detail.pop("details_json", None)
            st.json(detail)
