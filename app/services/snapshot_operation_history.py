"""Append-only, company-scoped Snapshot operation history."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from app.services.ssai_analytics_target_resolver import (
    connect_company_analytics_db,
    resolve_analytics_target,
)


log = logging.getLogger("ssai.snapshot.history")
_STATUS = {"started", "stage_complete", "succeeded", "failed", "skipped"}
_ORIGIN = {"runtime", "backfill"}
_KINDS = {"snapshot_generate", "monthly_build", "inspect", "approve_publish", "cleanup"}
_DETAIL_KEYS = {"stage_timings_ms", "source_call_count", "reused_months", "monthly_manifest_ids",
                "deleted_snapshot_ids", "deleted_monthly_ids", "no_op", "source_verified",
                "grade_counts"}
_COLUMNS = (
    "event_key", "company_id", "analytics_target_hash", "run_id", "operation_kind",
    "stage", "result_status", "origin", "operation_date", "started_at", "finished_at",
    "evaluation_month", "basis_month", "manifest_id", "generation_no", "profile_fingerprint",
    "product_count", "fact_count", "source_row_count", "exact_status", "whole_checksum",
    "source_ms", "calculation_ms", "storage_ms", "total_ms", "approved_by",
    "approval_status", "deleted_manifest_ids", "deleted_rows", "reserved_delta_kib",
    "error_code", "source_report", "details_json",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class OperationEvent:
    company_id: int
    operation_kind: str
    stage: str
    result_status: str
    run_id: str = field(default_factory=lambda: str(uuid4()))
    origin: str = "runtime"
    operation_date: date | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    evaluation_month: str | None = None
    basis_month: str | None = None
    manifest_id: int | None = None
    generation_no: int | None = None
    profile_fingerprint: str | None = None
    product_count: int | None = None
    fact_count: int | None = None
    source_row_count: int | None = None
    exact_status: str | None = None
    whole_checksum: str | None = None
    source_ms: Decimal | float | None = None
    calculation_ms: Decimal | float | None = None
    storage_ms: Decimal | float | None = None
    total_ms: Decimal | float | None = None
    approved_by: str | None = None
    approval_status: str | None = None
    deleted_manifest_ids: tuple[int, ...] = ()
    deleted_rows: int | None = None
    reserved_delta_kib: int | None = None
    error_code: str | None = None
    source_report: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    event_key: str | None = None


def _date_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _validated_values(event: OperationEvent, target_hash: str) -> tuple[Any, ...]:
    if event.company_id <= 0 or event.operation_kind not in _KINDS or event.result_status not in _STATUS:
        raise ValueError("invalid operation history identity")
    if event.origin not in _ORIGIN or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", event.stage):
        raise ValueError("invalid operation history stage")
    run_id = str(UUID(event.run_id))
    if event.operation_date and not isinstance(event.operation_date, date):
        raise ValueError("invalid operation date")
    for month in (event.evaluation_month, event.basis_month):
        if month is not None and not re.fullmatch(r"\d{6}", month):
            raise ValueError("invalid operation month")
    for digest in (event.profile_fingerprint, event.whole_checksum):
        if digest is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise ValueError("invalid operation checksum/fingerprint")
    for value in (event.product_count, event.fact_count, event.source_row_count,
                  event.deleted_rows, event.reserved_delta_kib):
        if value is not None and (not isinstance(value, int) or value < 0):
            raise ValueError("negative or nonintegral operation count")
    if event.error_code and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", event.error_code):
        raise ValueError("unsafe operation error code")
    if event.source_report and not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", event.source_report):
        raise ValueError("unsafe operation report reference")
    if set(event.details) - _DETAIL_KEYS:
        raise ValueError("unsupported operation detail key")
    if any(not isinstance(value, (str, int, float, bool, list, tuple, dict, type(None)))
           for value in event.details.values()):
        raise ValueError("unsupported operation detail value")
    details_json = json.dumps(event.details, ensure_ascii=True, separators=(",", ":")) if event.details else None
    if details_json and len(details_json) > 2000:
        raise ValueError("operation details exceed limit")
    manifest_ids = json.dumps(event.deleted_manifest_ids, separators=(",", ":")) if event.deleted_manifest_ids else None
    if any(not isinstance(value, int) or value <= 0 for value in event.deleted_manifest_ids):
        raise ValueError("invalid deleted manifest identifier")
    if manifest_ids and len(manifest_ids) > 400:
        raise ValueError("too many deleted manifest identifiers")
    key = event.event_key or hashlib.sha256(
        f"{event.company_id}:{run_id}:{event.operation_kind}:{event.stage}:{event.result_status}:"
        f"{event.manifest_id or ''}:{event.basis_month or ''}".encode("ascii")
    ).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", key) or not re.fullmatch(r"[0-9a-f]{64}", target_hash):
        raise ValueError("invalid operation event key")
    values = (
        key, str(event.company_id), target_hash, run_id, event.operation_kind,
        event.stage, event.result_status, event.origin, event.operation_date,
        _date_time(event.started_at), _date_time(event.finished_at), event.evaluation_month,
        event.basis_month, event.manifest_id, event.generation_no, event.profile_fingerprint,
        event.product_count, event.fact_count, event.source_row_count, event.exact_status,
        event.whole_checksum, event.source_ms, event.calculation_ms, event.storage_ms,
        event.total_ms, event.approved_by, event.approval_status, manifest_ids,
        event.deleted_rows, event.reserved_delta_kib, event.error_code, event.source_report,
        details_json,
    )
    if len(values) != len(_COLUMNS):
        raise AssertionError("operation history column contract mismatch")
    return values


def analytics_target_hash(company_id: int) -> str:
    target = resolve_analytics_target(company_id, "writer")
    return hashlib.sha256(target.target_id.encode("utf-8")).hexdigest()


def insert_event_on_cursor(cursor: Any, event: OperationEvent, *, target_hash: str) -> bool:
    """Use the caller's transaction for operations that must be atomic with history."""
    values = _validated_values(event, target_hash)
    columns = ",".join(_COLUMNS)
    parameters = ",".join("?" for _ in _COLUMNS)
    cursor.execute(
        f"INSERT INTO snapshot.operation_event ({columns}) "
        f"SELECT {parameters} WHERE NOT EXISTS ("
        "SELECT 1 FROM snapshot.operation_event WITH (UPDLOCK,HOLDLOCK) WHERE event_key=?)",
        *values, values[0],
    )
    return cursor.rowcount == 1


def record_event(event: OperationEvent, *, require_store: bool = False) -> bool:
    """Append only after a prior operation committed; never invent its outcome."""
    connection = None
    try:
        target_hash = analytics_target_hash(event.company_id)
        connection = connect_company_analytics_db(event.company_id, "writer", timeout=30)
        cursor = connection.cursor()
        if cursor.execute("SELECT OBJECT_ID(N'snapshot.operation_event',N'U')").fetchval() is None:
            if require_store:
                raise RuntimeError("Snapshot operation history migration is missing")
            return False
        inserted = insert_event_on_cursor(cursor, event, target_hash=target_hash)
        connection.commit()
        return inserted
    except Exception as exc:
        if connection is not None:
            connection.rollback()
        if require_store:
            raise
        log.warning("[snapshot.history] event write failed company=%s kind=%s stage=%s type=%s",
                    event.company_id, event.operation_kind, event.stage, type(exc).__name__)
        return False
    finally:
        if connection is not None:
            connection.close()


def list_history_company_ids(anchor_company_id: int) -> tuple[int, ...]:
    connection = connect_company_analytics_db(anchor_company_id, "reader", timeout=30)
    try:
        rows = connection.cursor().execute(
            """SELECT company_id FROM snapshot.operation_event
               UNION SELECT company_id FROM snapshot.manifest
               UNION SELECT company_id FROM snapshot.frequency_month_manifest"""
        ).fetchall()
        return tuple(sorted(int(row[0]) for row in rows if str(row[0]).isdigit()))
    finally:
        connection.close()


def query_events(anchor_company_id: int, *, company_id: int | None,
                 from_date: date, to_date: date, kind: str | None = None,
                 status: str | None = None, evaluation_month: str | None = None,
                 limit: int = 100) -> list[dict[str, Any]]:
    if from_date > to_date or (kind is not None and kind not in _KINDS):
        raise ValueError("invalid history filters")
    if status is not None and status not in _STATUS:
        raise ValueError("invalid history status")
    if evaluation_month is not None and not re.fullmatch(r"\d{6}", evaluation_month):
        raise ValueError("invalid evaluation month")
    top = max(1, min(int(limit), 200))
    connection = connect_company_analytics_db(anchor_company_id, "reader", timeout=30)
    try:
        cursor = connection.cursor()
        rows = cursor.execute(
            """SELECT TOP (?) event_id,company_id,analytics_target_hash,run_id,operation_kind,stage,result_status,
                      origin,event_at,operation_date,started_at,finished_at,evaluation_month,
                      basis_month,manifest_id,generation_no,profile_fingerprint,product_count,
                      fact_count,source_row_count,exact_status,whole_checksum,source_ms,
                      calculation_ms,storage_ms,total_ms,approved_by,approval_status,
                      deleted_manifest_ids,deleted_rows,reserved_delta_kib,error_code,
                      source_report,details_json
               FROM snapshot.operation_event
               WHERE event_at>=? AND event_at<DATEADD(day,1,?)
                 AND (? IS NULL OR company_id=?)
                 AND (? IS NULL OR operation_kind=?)
                 AND (? IS NULL OR result_status=?)
                 AND (? IS NULL OR evaluation_month=?)
               ORDER BY event_at DESC,event_id DESC""",
            top, from_date, to_date, str(company_id) if company_id else None,
            str(company_id) if company_id else None, kind, kind, status, status,
            evaluation_month, evaluation_month,
        ).fetchall()
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in rows]
    finally:
        connection.close()


def read_history_summary(anchor_company_id: int, company_id: int) -> dict[str, Any]:
    connection = connect_company_analytics_db(anchor_company_id, "reader", timeout=30)
    try:
        cursor = connection.cursor()
        snapshot = cursor.execute(
            """SELECT TOP 1 manifest_id,generation_no,evaluation_month,item_count,checksum
               FROM snapshot.manifest WHERE company_id=? AND status='published'
                 AND approval_status='approved' ORDER BY published_at DESC,manifest_id DESC""",
            str(company_id),
        ).fetchone()
        monthly = cursor.execute(
            """SELECT TOP 1 manifest_id,generation_no,basis_month,fact_count
               FROM snapshot.frequency_month_manifest WHERE company_id=?
                 AND status='published' AND approval_status='approved'
               ORDER BY basis_month DESC,published_at DESC,manifest_id DESC""",
            str(company_id),
        ).fetchone()
        event = cursor.execute(
            """SELECT TOP 1 event_at,operation_kind,result_status FROM snapshot.operation_event
               WHERE company_id=? ORDER BY event_at DESC,event_id DESC""", str(company_id),
        ).fetchone()
        duration = cursor.execute(
            """SELECT TOP 1 total_ms FROM snapshot.operation_event
               WHERE company_id=? AND total_ms IS NOT NULL
               ORDER BY event_at DESC,event_id DESC""", str(company_id),
        ).fetchone()
        failures = int(cursor.execute(
            """SELECT COUNT_BIG(*) FROM snapshot.operation_event
               WHERE company_id=? AND result_status='failed'
                 AND event_at>=DATEADD(day,-30,SYSUTCDATETIME())""", str(company_id),
        ).fetchval())
        return {"snapshot": tuple(snapshot) if snapshot else None,
                "monthly": tuple(monthly) if monthly else None,
                "recent_event": tuple(event) if event else None,
                "last_duration_ms": duration[0] if duration else None,
                "failures_30d": failures}
    finally:
        connection.close()
