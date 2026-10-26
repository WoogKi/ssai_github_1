"""Authorized, server-deduplicated read model for Snapshot operation history."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as clock_time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.services.ssai_analytics_target_resolver import (
    connect_company_analytics_db,
    normalize_sql_server_identity,
)
from app.services.ssai_company_admin_service import list_companies


KST = ZoneInfo("Asia/Seoul")
MAX_SERVER_ROWS = 100
MAX_REGISTRY_ROWS = 2000


@dataclass(frozen=True)
class HistoryServer:
    key: str
    label: str
    anchor_company_id: int | None
    company_ids: tuple[int, ...]


@dataclass(frozen=True)
class HistoryCatalog:
    servers: tuple[HistoryServer, ...]
    company_names: dict[int, str]
    company_servers: dict[int, str]
    warnings: tuple[str, ...]
    company_statuses: dict[int, str] = field(default_factory=dict)


def _registration_status(value: Any) -> str:
    if value is True or value == 1 or value == "1":
        return "활성"
    if value is False or value == 0 or value == "0":
        return "비활성"
    return "확인 불가"


def require_history_admin(actor: Any) -> None:
    if not (actor and str(getattr(actor, "user_type", "")).strip().upper() == "SSART_ADMIN"
            and str(getattr(actor, "user_grade", "")).strip().upper() == "SUPER"):
        raise PermissionError("Snapshot operation history requires a super administrator")


def build_history_catalog(actor: Any) -> HistoryCatalog:
    require_history_admin(actor)
    registered = list_companies(include_inactive=True, top=MAX_REGISTRY_ROWS)
    if len(registered) >= MAX_REGISTRY_ROWS:
        raise RuntimeError("company registry exceeds the bounded admin catalog")
    groups: dict[str, list[dict[str, Any]]] = {}
    company_names: dict[int, str] = {}
    company_servers: dict[int, str] = {}
    company_statuses: dict[int, str] = {}
    warnings: list[str] = []
    for row in registered:
        company_id = int(row["company_id"])
        name = str(row.get("company_name") or "").strip()
        if not name:
            warnings.append(f"회사 {company_id}: 등록된 회사명이 없습니다.")
            name = f"회사 {company_id}"
        company_names[company_id] = name
        company_statuses[company_id] = _registration_status(row.get("is_active"))
        try:
            identity = normalize_sql_server_identity(row.get("db_server"), row.get("db_port"))
        except Exception:
            warnings.append(f"회사 {company_id}: ERP 서버 연결 대상이 없습니다.")
            continue
        key = hashlib.sha256(f"company-db-server:{identity}".encode("utf-8")).hexdigest()
        if company_id in company_servers:
            raise RuntimeError("duplicate company ID in registry")
        company_servers[company_id] = key
        groups.setdefault(key, []).append(row)
    servers: list[HistoryServer] = []
    for ordinal, key in enumerate(sorted(groups), start=1):
        rows = groups[key]
        active = tuple(sorted(int(row["company_id"]) for row in rows
                              if company_statuses[int(row["company_id"])] == "활성"))
        all_ids = tuple(sorted(int(row["company_id"]) for row in rows))
        preview = ", ".join(company_names[company_id] for company_id in all_ids[:2])
        extra = f" 외 {len(all_ids) - 2}곳" if len(all_ids) > 2 else ""
        label = f"ERP 서버 {ordinal} · {preview}{extra}"
        servers.append(HistoryServer(key, label, active[0] if active else None, all_ids))
    return HistoryCatalog(tuple(servers), company_names, company_servers, tuple(warnings),
                          company_statuses)


def kst_day_bounds(from_date: date, to_date: date) -> tuple[datetime, datetime]:
    if from_date > to_date or (to_date - from_date).days > 366:
        raise ValueError("history date range must be within 366 days")
    start = datetime.combine(from_date, clock_time.min, tzinfo=KST)
    end = datetime.combine(to_date + timedelta(days=1), clock_time.min, tzinfo=KST)
    return (start.astimezone(timezone.utc).replace(tzinfo=None),
            end.astimezone(timezone.utc).replace(tzinfo=None))


def to_kst(value: datetime | None) -> str:
    if value is None:
        return "기록 없음"
    return value.replace(tzinfo=timezone.utc).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")


def _rows(cursor: Any, sql: str, *parameters: Any) -> list[dict[str, Any]]:
    records = cursor.execute(sql, *parameters).fetchall()
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, record)) for record in records]


def query_admin_history(
    actor: Any, catalog: HistoryCatalog, *, server_keys: tuple[str, ...],
    company_ids: tuple[int, ...] | None, from_date: date, to_date: date,
    kind: str | None = None, status: str | None = None,
    evaluation_month: str | None = None, per_server_limit: int = 100,
) -> dict[str, Any]:
    require_history_admin(actor)
    valid_servers = {server.key: server for server in catalog.servers}
    if not server_keys or len(set(server_keys)) != len(server_keys) or set(server_keys) - valid_servers.keys():
        raise ValueError("invalid or duplicate history server selection")
    if company_ids is not None:
        if not company_ids or len(set(company_ids)) != len(company_ids):
            raise ValueError("invalid history company selection")
        if any(catalog.company_servers.get(int(company_id)) not in server_keys for company_id in company_ids):
            raise PermissionError("company does not belong to selected ERP server")
    if kind not in {None, "snapshot_generate", "monthly_build", "inspect", "approve_publish", "cleanup"}:
        raise ValueError("invalid operation type")
    if status not in {None, "started", "stage_complete", "succeeded", "failed", "skipped"}:
        raise ValueError("invalid operation status")
    if evaluation_month is not None and (len(evaluation_month) != 6 or not evaluation_month.isdigit()):
        raise ValueError("invalid evaluation month")
    start_utc, end_utc = kst_day_bounds(from_date, to_date)
    top = min(MAX_SERVER_ROWS, max(1, int(per_server_limit)))
    events: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    monthly: list[dict[str, Any]] = []
    server_results: list[dict[str, Any]] = []
    for key in server_keys:
        server = valid_servers[key]
        selected_ids = tuple(company_id for company_id in server.company_ids
                             if company_ids is None or company_id in company_ids)
        if not selected_ids:
            continue
        if server.anchor_company_id is None:
            server_results.append({"server": server.label, "status": "no_active_anchor",
                                   "elapsed_ms": 0, "event_count": 0})
            continue
        started = time.perf_counter()
        connection = None
        try:
            connection = connect_company_analytics_db(server.anchor_company_id, "reader", timeout=10)
            cursor = connection.cursor()
            if cursor.execute("SELECT OBJECT_ID(N'snapshot.operation_event',N'U')").fetchval() is None:
                server_results.append({"server": server.label, "status": "migration_011_missing",
                                       "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                                       "event_count": 0})
                continue
            placeholders = ",".join("?" for _ in selected_ids)
            ids = tuple(str(company_id) for company_id in selected_ids)
            found = _rows(cursor, f"""SELECT TOP (?) event_id,company_id,analytics_target_hash,run_id,
                       operation_kind,stage,result_status,origin,event_at,operation_date,
                       started_at,finished_at,evaluation_month,basis_month,manifest_id,
                       generation_no,profile_fingerprint,product_count,fact_count,
                       source_row_count,exact_status,whole_checksum,source_ms,calculation_ms,
                       storage_ms,total_ms,approved_by,approval_status,deleted_manifest_ids,
                       deleted_rows,reserved_delta_kib,error_code,source_report,details_json
                    FROM snapshot.operation_event
                    WHERE analytics_target_hash=? AND company_id IN ({placeholders})
                      AND event_at>=? AND event_at<?
                      AND (? IS NULL OR operation_kind=?)
                      AND (? IS NULL OR result_status=?)
                      AND (? IS NULL OR evaluation_month=?)
                    ORDER BY event_at DESC,event_id DESC""",
                          top, key, *ids, start_utc, end_utc, kind, kind, status, status,
                          evaluation_month, evaluation_month)
            if any(int(row["company_id"]) not in selected_ids or row["analytics_target_hash"] != key
                   for row in found):
                raise RuntimeError("history company/server boundary mismatch")
            for row in found:
                row["server_label"] = server.label
            server_snapshots = _rows(cursor, f"""WITH ranked AS (
                SELECT company_id,manifest_id,generation_no,evaluation_month,item_count,checksum,
                       ROW_NUMBER() OVER(PARTITION BY company_id
                       ORDER BY published_at DESC,manifest_id DESC) AS rn
                FROM snapshot.manifest WHERE company_id IN ({placeholders})
                  AND status='published' AND approval_status='approved')
                SELECT company_id,manifest_id,generation_no,evaluation_month,item_count,checksum
                FROM ranked WHERE rn=1""", *ids)
            server_monthly = _rows(cursor, f"""WITH ranked AS (
                SELECT company_id,manifest_id,generation_no,basis_month,fact_count,
                       ROW_NUMBER() OVER(PARTITION BY company_id
                       ORDER BY basis_month DESC,published_at DESC,manifest_id DESC) AS rn
                FROM snapshot.frequency_month_manifest WHERE company_id IN ({placeholders})
                  AND status='published' AND approval_status='approved')
                SELECT company_id,manifest_id,generation_no,basis_month,fact_count
                FROM ranked WHERE rn=1""", *ids)
            if any(int(row["company_id"]) not in selected_ids
                   for row in (*server_snapshots, *server_monthly)):
                raise RuntimeError("summary company/server boundary mismatch")
            events.extend(found)
            snapshots.extend(server_snapshots)
            monthly.extend(server_monthly)
            server_results.append({"server": server.label, "status": "ready",
                                   "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                                   "event_count": len(found)})
        except Exception as exc:
            server_results.append({"server": server.label, "status": "connection_or_query_failed",
                                   "error_type": type(exc).__name__,
                                   "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                                   "event_count": 0})
        finally:
            if connection is not None:
                connection.close()
    events.sort(key=lambda row: (row["event_at"], int(row["event_id"])), reverse=True)
    return {"events": events, "snapshots": snapshots, "monthly": monthly,
            "servers": server_results, "company_names": catalog.company_names}
