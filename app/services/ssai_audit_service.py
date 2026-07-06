# app/services/ssai_audit_service.py
#
# SS AI Phase 3
# 감사 로그 서비스
# create 2026/06/24

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pyodbc

from app.services.ssai_auth_service import connect_ssai_db


log = logging.getLogger("ssai.audit")


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, Decimal):
        return str(value)

    return str(value)


def _to_json_text(details: dict[str, Any] | list[Any] | str | None) -> str | None:
    if details is None:
        return None

    if isinstance(details, str):
        return details

    return json.dumps(
        details,
        ensure_ascii=False,
        default=_json_default,
    )


def _row_to_dict(cur: pyodbc.Cursor, row: Any) -> dict[str, Any] | None:
    if not row:
        return None

    columns = [col[0] for col in cur.description]
    return dict(zip(columns, row))


def _fetch_all_dicts(
    conn: pyodbc.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    cur = conn.cursor()
    rows = cur.execute(sql, *params).fetchall()
    columns = [col[0] for col in cur.description]
    return [dict(zip(columns, row)) for row in rows]


def log_audit_event(
    *,
    event_type: str,
    action_result: str = "SUCCESS",
    actor_user_id: int | None = None,
    actor_login_id: str | None = None,
    company_id: int | None = None,
    target_user_id: int | None = None,
    target_login_id: str | None = None,
    target_company_id: int | None = None,
    message: str | None = None,
    details: dict[str, Any] | list[Any] | str | None = None,
    request_id: str | None = None,
    session_id: str | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> int:
    """
    감사 로그를 기록하고 audit_id를 반환한다.

    이 함수는 실패 시 예외를 발생시킨다.
    화면/업무 로직에서는 safe_log_audit_event() 사용을 권장한다.
    """
    event_type = str(event_type or "").strip().upper()
    action_result = str(action_result or "SUCCESS").strip().upper()

    if not event_type:
        raise ValueError("event_type이 필요합니다.")

    if action_result not in {"SUCCESS", "FAIL", "ERROR", "DENIED", "INFO"}:
        action_result = "INFO"

    details_json = _to_json_text(details)

    sql = """
    INSERT INTO dbo.SSAI_AUDIT_LOGS (
        event_type,
        action_result,
        actor_user_id,
        actor_login_id,
        company_id,
        target_user_id,
        target_login_id,
        target_company_id,
        message,
        details_json,
        request_id,
        session_id,
        client_ip,
        user_agent,
        created_at
    )
    OUTPUT INSERTED.audit_id
    VALUES (
        ?,
        ?,
        ?,
        ?,
        ?,
        ?,
        ?,
        ?,
        ?,
        ?,
        ?,
        ?,
        ?,
        ?,
        SYSDATETIME()
    )
    """

    with connect_ssai_db() as conn:
        cur = conn.cursor()
        row = cur.execute(
            sql,
            event_type,
            action_result,
            int(actor_user_id) if actor_user_id is not None else None,
            str(actor_login_id or "").strip() or None,
            int(company_id) if company_id is not None else None,
            int(target_user_id) if target_user_id is not None else None,
            str(target_login_id or "").strip() or None,
            int(target_company_id) if target_company_id is not None else None,
            str(message or "").strip() or None,
            details_json,
            str(request_id or "").strip() or None,
            str(session_id or "").strip() or None,
            str(client_ip or "").strip() or None,
            str(user_agent or "").strip() or None,
        ).fetchone()

        conn.commit()

    return int(row[0])


def safe_log_audit_event(**kwargs: Any) -> int | None:
    """
    감사 로그 실패가 본 업무를 막지 않도록 하는 안전 래퍼.
    """
    try:
        return log_audit_event(**kwargs)
    except Exception:
        log.exception("[SSAI_AUDIT] log failed event_type=%s", kwargs.get("event_type"))
        return None


def list_audit_logs(
    *,
    top: int = 200,
    event_type: str | None = None,
    actor_user_id: int | None = None,
    company_id: int | None = None,
    target_user_id: int | None = None,
) -> list[dict[str, Any]]:
    """
    감사 로그 조회.
    나중에 관리자 화면의 로그 조회 탭에서 사용한다.
    """
    top = max(1, min(int(top or 200), 2000))

    where_parts: list[str] = []
    params: list[Any] = []

    if event_type:
        where_parts.append("event_type = ?")
        params.append(str(event_type).strip().upper())

    if actor_user_id is not None:
        where_parts.append("actor_user_id = ?")
        params.append(int(actor_user_id))

    if company_id is not None:
        where_parts.append("company_id = ?")
        params.append(int(company_id))

    if target_user_id is not None:
        where_parts.append("target_user_id = ?")
        params.append(int(target_user_id))

    where_sql = ""
    if where_parts:
        where_sql = "WHERE " + "\n  AND ".join(where_parts)

    sql = f"""
    SELECT TOP {top}
        audit_id,
        event_type,
        action_result,
        actor_user_id,
        actor_login_id,
        company_id,
        target_user_id,
        target_login_id,
        target_company_id,
        message,
        details_json,
        request_id,
        session_id,
        client_ip,
        user_agent,
        created_at
    FROM dbo.SSAI_AUDIT_LOGS
    {where_sql}
    ORDER BY audit_id DESC
    """

    with connect_ssai_db() as conn:
        return _fetch_all_dicts(conn, sql, tuple(params))


def get_audit_log(audit_id: int) -> dict[str, Any] | None:
    sql = """
    SELECT TOP 1
        audit_id,
        event_type,
        action_result,
        actor_user_id,
        actor_login_id,
        company_id,
        target_user_id,
        target_login_id,
        target_company_id,
        message,
        details_json,
        request_id,
        session_id,
        client_ip,
        user_agent,
        created_at
    FROM dbo.SSAI_AUDIT_LOGS
    WHERE audit_id = ?
    """

    with connect_ssai_db() as conn:
        cur = conn.cursor()
        row = cur.execute(sql, int(audit_id)).fetchone()
        return _row_to_dict(cur, row)