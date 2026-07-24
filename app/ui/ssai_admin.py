# app/ui/ssai_admin.py
#
# SS AI Phase 3
# 관리자 화면 1차
# - 사용자 가입 신청 승인/거절
# - 회원사 ERP DB 연결
# - 역할 부여

from __future__ import annotations

import io
import json
import logging
import re
from datetime import date, timedelta
from typing import Any

import streamlit as st

from app.services.ssai_auth_service import connect_ssai_db
from app.services.ssai_user_admin_service import (
    approve_user,
    change_company_user_role,
    get_manageable_companies,
    get_user_company_roles,
    list_active_companies,
    list_active_roles,
    list_managed_company_users,
    list_pending_users,
    reject_user,
    revoke_user_approval,
    set_company_user_access_active,
    update_user_policy,
)


from app.ui.ssai_company_admin import render_company_admin_page

from app.ui.ssai_login import get_current_user, has_permission

log = logging.getLogger("ssai")


SESSION_ADMIN_PAGE = "__ssai_admin_page"
SESSION_ADMIN_FLASH = "__ssai_admin_flash"
SESSION_ADMIN_SUBTAB = "__ssai_admin_subtab"


def _safe_key(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"[^0-9A-Za-z가-힣_]+", "_", text).strip("_")
    return text or "item"


def _can_use_admin_page() -> bool:
    return (
        has_permission("USER_APPROVE")
        or has_permission("USER_MANAGE_ALL")
        or has_permission("USER_MANAGE_COMPANY")
        or has_permission("COMPANY_MANAGE")
        or has_permission("SIMS_DB_MANAGE")
    )

def _can_manage_all_companies() -> bool:
    user = get_current_user()
    if not user:
        return False

    return bool(
        user.user_type == "SSART_ADMIN"
        and (
            has_permission("USER_APPROVE")
            or has_permission("USER_MANAGE_ALL")
        )
    )

def _can_approve_signup_users() -> bool:
    """
    신규 가입 승인/재승인은 신성아트컴 관리자 계열만 허용한다.

    회원사 관리자는 자기 회원사 사용자 관리는 가능하지만,
    SSAI 신규 가입 신청 자체를 승인할 수 없다.
    """
    user = get_current_user()

    if not user:
        return False

    return bool(
        user.user_type in ("SSART_ADMIN", "SSART_USER")
        and (
            has_permission("USER_APPROVE")
            or has_permission("USER_MANAGE_ALL")
        )
    )

def _is_super_admin_user(user: Any | None) -> bool:
    """
    신성아트컴 최고 관리자 여부.
    - 자기 자신 변경은 별도 로직에서 이미 차단한다.
    - SUPER만 신성아트컴 관리자/사용자 정책을 변경할 수 있다.
    """
    if not user:
        return False

    return bool(
        str(getattr(user, "user_type", "") or "").strip().upper() == "SSART_ADMIN"
        and str(getattr(user, "user_grade", "") or "").strip().upper() == "SUPER"
    )

def _is_ssart_manager_user(user: Any | None) -> bool:
    """
    신성아트컴 관리자 여부.
    SUPER와 MANAGER 모두 포함한다.
    """
    if not user:
        return False

    return bool(
        str(getattr(user, "user_type", "") or "").strip().upper() == "SSART_ADMIN"
        and str(getattr(user, "user_grade", "") or "").strip().upper() in ("SUPER", "MANAGER")
    )

def _can_manage_company_users() -> bool:
    return bool(
        _can_manage_all_companies()
        or has_permission("USER_MANAGE_COMPANY")
    )

def _can_view_audit_logs() -> bool:
    """
    감사 로그는 민감 정보이므로 신성아트컴 관리자급만 조회한다.
    """
    return bool(
        _can_manage_all_companies()
        or has_permission("USER_MANAGE_ALL")
    )

def render_ssai_admin_sidebar() -> None:
    """
    사이드바 관리자 메뉴.

    권한 있는 사용자에게만 표시한다.
    """
    if not _can_use_admin_page():
        return

    with st.sidebar.expander("🛡️ 관리자 메뉴", expanded=False):
        admin_user_menu_label = (
            "회원 사용자 승인 관리"
            if _can_approve_signup_users()
            else "회원사 사용자 관리"
        )

        if st.button(admin_user_menu_label, width="stretch", key="__ssai_admin_user_approval_btn"):
            st.session_state[SESSION_ADMIN_PAGE] = "user_approval"

            if not _can_approve_signup_users():
                st.session_state[SESSION_ADMIN_SUBTAB] = "회사 사용자 관리"

            st.rerun()

        if (
            has_permission("COMPANY_MANAGE")
            or has_permission("SIMS_DB_MANAGE")
            or has_permission("USER_MANAGE_ALL")
        ):
            if st.button("회원사 ERP DB 관리", width="stretch", key="__ssai_admin_company_db_btn"):
                st.session_state[SESSION_ADMIN_PAGE] = "company_admin"
                st.rerun()

        if _can_view_audit_logs():
            if st.button("감사 로그 조회", width="stretch", key="__ssai_admin_audit_logs_btn"):
                st.session_state[SESSION_ADMIN_PAGE] = "audit_logs"
                st.rerun()

        if st.session_state.get(SESSION_ADMIN_PAGE):
            if st.button("일반 화면으로 돌아가기", width="stretch", key="__ssai_admin_back_btn"):
                st.session_state.pop(SESSION_ADMIN_PAGE, None)
                st.rerun()



def _find_default_company_index(companies: list[dict[str, Any]], requested_company_name: str) -> int:
    """
    신청 회원사명과 비슷한 회사를 기본 선택한다.
    """
    req = str(requested_company_name or "").strip().lower()

    if not companies:
        return 0

    if not req:
        return 0

    for i, c in enumerate(companies):
        code = str(c.get("company_code") or "").strip().lower()
        name = str(c.get("company_name") or "").strip().lower()

        if req == code or req == name:
            return i

    for i, c in enumerate(companies):
        code = str(c.get("company_code") or "").strip().lower()
        name = str(c.get("company_name") or "").strip().lower()

        if req in code or req in name or code in req or name in req:
            return i

    return 0


def _approval_company_label(company: dict[str, Any]) -> str:
    """
    승인/관리 화면에서 회원사와 ERP DB를 구분해서 보여주는 표시명.
    """
    member_code = str(company.get("customer_code") or company.get("company_code") or "").strip()
    member_name = str(
        company.get("customer_name")
        or company.get("erp_company_name")
        or company.get("company_name")
        or ""
    ).strip()
    erp_db_code = str(company.get("company_code") or "").strip()
    erp_db_name = str(company.get("company_name") or "").strip()
    db_usage_type = str(company.get("db_usage_type") or company.get("company_type") or "").strip()
    db_name = str(company.get("db_name") or "").strip()

    label = f"{member_name or '-'} ({member_code or '-'}) / ERP DB: {erp_db_name or erp_db_code or '-'}"

    if db_usage_type:
        label += f" / {db_usage_type}"

    if db_name:
        label += f" / DB: {db_name}"

    return label


def _role_grade_for_role(role_code: str) -> str:
    role_code = str(role_code or "").strip()

    if role_code == "WHOLESALE_MANAGER":
        return "MANAGER"

    if role_code == "WHOLESALE_READONLY":
        return "READONLY"

    return "STAFF"

def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return bool(value)

    return str(value or "").strip().lower() in ("1", "true", "y", "yes", "on")


def _admin_set_flash(message: str, level: str = "success") -> None:
    st.session_state[SESSION_ADMIN_FLASH] = {
        "message": str(message or ""),
        "level": str(level or "success"),
    }


def _admin_render_flash() -> None:
    flash = st.session_state.pop(SESSION_ADMIN_FLASH, None)

    if not isinstance(flash, dict):
        return

    message = flash.get("message") or ""
    level = flash.get("level") or "success"

    if not message:
        return

    if level == "error":
        st.error(message)
    elif level == "warning":
        st.warning(message)
    elif level == "info":
        st.info(message)
    else:
        st.success(message)

def _admin_keep_company_user_tab() -> None:
    st.session_state[SESSION_ADMIN_SUBTAB] = "회원사 사용자 관리"

def _admin_keep_pending_tab() -> None:
    st.session_state[SESSION_ADMIN_SUBTAB] = "승인 대기"

def _row_to_dict(cursor: Any, row: Any) -> dict[str, Any] | None:
    if not row:
        return None

    columns = [col[0] for col in cursor.description]
    return dict(zip(columns, row))

def _fetch_audit_event_types() -> list[str]:
    """
    감사 로그 화면의 이벤트 selectbox에 표시할 event_type 목록을 조회한다.
    """
    sql = """
    SELECT DISTINCT
        event_type
    FROM dbo.SSAI_AUDIT_LOGS
    WHERE event_type IS NOT NULL
      AND LTRIM(RTRIM(CONVERT(nvarchar(200), event_type))) <> ''
    ORDER BY event_type
    """

    with connect_ssai_db() as conn:
        cur = conn.cursor()
        rows = cur.execute(sql).fetchall()

    out: list[str] = []

    for row in rows:
        try:
            value = str(row[0] or "").strip()
        except Exception:
            value = ""

        if value and value not in out:
            out.append(value)

    return out


def _fetch_audit_logs(
    *,
    top: int = 100,
    event_type: str = "",
    action_result: str = "",
    keyword: str = "",
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict[str, Any]]:
    top = max(1, min(int(top or 100), 5000))

    where_parts: list[str] = ["1 = 1"]
    params: list[Any] = []

    event_type = str(event_type or "").strip()
    action_result = str(action_result or "").strip()
    keyword = str(keyword or "").strip()

    if event_type and event_type != "전체":
        where_parts.append("event_type = ?")
        params.append(event_type)

    if action_result and action_result != "전체":
        where_parts.append("action_result = ?")
        params.append(action_result)

    if date_from is not None:
        where_parts.append("created_at >= ?")
        params.append(date_from)

    if date_to is not None:
        # 종료일은 해당 날짜 전체를 포함하기 위해 다음날 00:00 미만으로 조회
        where_parts.append("created_at < ?")
        params.append(date_to + timedelta(days=1))

    if keyword:
        like_value = f"%{keyword}%"
        where_parts.append(
            """
            (
                actor_login_id LIKE ?
                OR target_login_id LIKE ?
                OR message LIKE ?
                OR event_type LIKE ?
                OR action_result LIKE ?
            )
            """
        )
        params.extend([
            like_value,
            like_value,
            like_value,
            like_value,
            like_value,
        ])

    where_sql = "\n      AND ".join(where_parts)

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
    WHERE {where_sql}
    ORDER BY audit_id DESC
    """

    with connect_ssai_db() as conn:
        cur = conn.cursor()
        rows = cur.execute(sql, *tuple(params)).fetchall()
        columns = [col[0] for col in cur.description]

    return [dict(zip(columns, row)) for row in rows]


def _parse_details_json(value: Any) -> Any:
    text = str(value or "").strip()

    if not text:
        return {}

    try:
        return json.loads(text)
    except Exception:
        return {
            "raw": text,
        }

def _make_audit_logs_excel_bytes(logs: list[dict[str, Any]]) -> bytes:
    """
    감사 로그 조회 결과를 Excel 파일 bytes로 만든다.
    """
    import pandas as pd

    rows: list[dict[str, Any]] = []

    for row in logs:
        excel_row = dict(row)

        # created_at은 Excel에서 보기 좋게 문자열화
        if excel_row.get("created_at") is not None:
            excel_row["created_at"] = str(excel_row.get("created_at"))

        # details_json은 JSON 문자열로 정리
        details = _parse_details_json(excel_row.get("details_json"))

        try:
            excel_row["details_json_pretty"] = json.dumps(
                details,
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        except Exception:
            excel_row["details_json_pretty"] = str(excel_row.get("details_json") or "")

        rows.append(excel_row)

    df = pd.DataFrame(rows)

    preferred_cols = [
        "audit_id",
        "created_at",
        "event_type",
        "action_result",
        "actor_user_id",
        "actor_login_id",
        "company_id",
        "target_user_id",
        "target_login_id",
        "target_company_id",
        "message",
        "details_json_pretty",
        "request_id",
        "session_id",
        "client_ip",
        "user_agent",
    ]

    existing_cols = [c for c in preferred_cols if c in df.columns]
    remaining_cols = [c for c in df.columns if c not in existing_cols]

    if existing_cols:
        df = df[existing_cols + remaining_cols]

    output = io.BytesIO()

    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, sheet_name="audit_logs", index=False)

        workbook = writer.book
        worksheet = writer.sheets["audit_logs"]

        header_format = workbook.add_format({
            "bold": True,
            "bg_color": "#D9EAF7",
            "border": 1,
        })

        text_wrap_format = workbook.add_format({
            "text_wrap": True,
            "valign": "top",
        })

        for col_idx, col_name in enumerate(df.columns):
            worksheet.write(0, col_idx, col_name, header_format)

            if col_name in ("message", "details_json", "details_json_pretty"):
                worksheet.set_column(col_idx, col_idx, 60, text_wrap_format)
            elif col_name in ("created_at",):
                worksheet.set_column(col_idx, col_idx, 22)
            elif col_name.endswith("_login_id"):
                worksheet.set_column(col_idx, col_idx, 20)
            else:
                worksheet.set_column(col_idx, col_idx, 16)

        worksheet.freeze_panes(1, 0)
        worksheet.autofilter(0, 0, max(len(df), 1), max(len(df.columns) - 1, 0))

    return output.getvalue()

def _active_role_code_from_user_row(row: dict[str, Any]) -> str:
    """
    회원사 사용자 목록 row에서 현재 활성 역할을 가져온다.
    """
    text = str(row.get("active_role_codes") or "").strip()

    if not text:
        text = str(row.get("role_codes") or "").strip()

    codes = [x.strip() for x in text.split(",") if x.strip()]

    priority = [
        "WHOLESALE_MANAGER",
        "WHOLESALE_STAFF",
        "WHOLESALE_READONLY",
    ]

    for code in priority:
        if code in codes:
            return code

    return codes[0] if codes else "WHOLESALE_STAFF"


def _role_label_options() -> dict[str, str]:
    return {
        "도매 사용자": "WHOLESALE_STAFF",
        "도매 관리자": "WHOLESALE_MANAGER",
        "도매 조회 전용 사용자": "WHOLESALE_READONLY",
    }

def _user_type_label_options() -> dict[str, str]:
    return {
        "신성아트컴 관리자": "SSART_ADMIN",
        "신성아트컴 사용자": "SSART_USER",
        "도매 관리자": "WHOLESALE_ADMIN",
        "도매 사용자": "WHOLESALE_USER",
    }


def _user_grade_label_options() -> dict[str, str]:
    return {
        "최고 관리자": "SUPER",
        "관리자": "MANAGER",
        "일반 사용자": "STAFF",
        "조회 전용": "READONLY",
    }


def _role_options_for_user_type(
    roles: list[dict[str, Any]],
    user_type: str,
) -> dict[str, str]:
    """
    사용자 종류별 선택 가능한 역할 목록.

    SYSTEM_ADMIN은 일반 승인 화면에서 만들지 않는다.
    시스템 전체 관리자는 DB에서 직접 관리하거나 별도 관리자 관리 화면에서 처리한다.
    """
    user_type = str(user_type or "").strip().upper()

    allowed_codes_by_type = {
        "SSART_ADMIN": ["SSART_MANAGER"],
        "SSART_USER": ["SSART_STAFF"],
        "WHOLESALE_ADMIN": ["WHOLESALE_MANAGER"],
        "WHOLESALE_USER": ["WHOLESALE_STAFF", "WHOLESALE_READONLY"],
    }

    fallback_labels = {
        "SSART_MANAGER": "신성아트컴 관리자",
        "SSART_STAFF": "신성아트컴 사용자",
        "WHOLESALE_MANAGER": "도매 관리자",
        "WHOLESALE_STAFF": "도매 사용자",
        "WHOLESALE_READONLY": "도매 조회 전용 사용자",
    }

    allowed_codes = allowed_codes_by_type.get(user_type, ["WHOLESALE_STAFF"])

    role_by_code = {
        str(r.get("role_code") or "").strip().upper(): r
        for r in roles
    }

    options: dict[str, str] = {}

    for code in allowed_codes:
        role = role_by_code.get(code)
        role_name = str(role.get("role_name") or "").strip() if role else ""
        label_name = role_name or fallback_labels.get(code, code)
        label = f"{label_name} ({code})"
        options[label] = code

    return options


def _grade_for_user_type_and_role(
    *,
    user_type: str,
    role_code: str,
) -> str:
    user_type = str(user_type or "").strip().upper()
    role_code = str(role_code or "").strip().upper()

    if user_type == "SSART_ADMIN":
        return "MANAGER"

    if user_type == "SSART_USER":
        return "STAFF"

    if user_type == "WHOLESALE_ADMIN":
        return "MANAGER"

    if role_code == "WHOLESALE_READONLY":
        return "READONLY"

    return "STAFF"

def _grade_label_index_for_value(value: str) -> int:
    value = str(value or "").strip().upper()
    grade_values = list(_user_grade_label_options().values())

    try:
        return grade_values.index(value)
    except ValueError:
        return 2

def _option_index_by_value(
    options: dict[str, str],
    value: str,
    default_index: int = 0,
) -> int:
    value = str(value or "").strip().upper()
    values = [str(v or "").strip().upper() for v in options.values()]

    try:
        return values.index(value)
    except ValueError:
        return default_index


def _first_role_code_from_user(user: dict[str, Any]) -> str:
    role_codes = str(
        user.get("active_role_codes")
        or user.get("role_codes")
        or ""
    ).strip()

    if not role_codes:
        return ""

    return role_codes.split(",")[0].strip().upper()

def _get_dataframe_selected_row_index(event: Any) -> int | None:
    """
    st.dataframe(on_select='rerun') 선택 행 index 추출.
    Streamlit 버전/반환 형태 차이를 방어적으로 처리한다.
    """
    if event is None:
        return None

    try:
        rows = event.selection.rows
        if rows:
            return int(rows[0])
    except Exception:
        pass

    try:
        rows = event.get("selection", {}).get("rows", [])
        if rows:
            return int(rows[0])
    except Exception:
        pass

    return None

def _render_pending_user_card(
    *,
    user: dict[str, Any],
    companies: list[dict[str, Any]],
    roles: list[dict[str, Any]],
    current_user: Any | None,
) -> None:


    login_id = str(user.get("login_id") or "").strip()
    member_nickname = str(user.get("nickname") or user.get("user_name") or "").strip()
    member_phone = str(user.get("phone") or "").strip()
    requested_company_name = str(user.get("requested_company_name") or "").strip()
    sims_user_id = str(user.get("sims_user_id") or "").strip()
    key_base = _safe_key(f"{user.get('user_id')}_{login_id}")

    member_display = member_nickname or login_id
    if member_phone:
        member_display = f"{member_display} / {member_phone}"

    title = f"{member_nickname or login_id} / {login_id}"

    with st.expander(title, expanded=True):
        c1, c2, c3 = st.columns(3)

        with c1:
            st.write("**회원 Nickname**")
            st.write(member_display or "-")
            st.caption(f"로그인 ID: {login_id}")

        with c2:
            st.write("**신청 회원사명**")
            st.write(requested_company_name or "-")
            st.caption(f"SIMS ID: {sims_user_id or '-'}")

        with c3:
            st.write("**상태**")
            st.write(user.get("approval_status") or "-")
            st.caption(f"신청일: {user.get('created_at') or '-'}")

        approval_status = str(user.get("approval_status") or "").strip().upper()

        if approval_status == "SUSPENDED":
            st.warning(
                "이 사용자는 승인 취소 / 권한 회수 상태입니다. "
                "다시 승인하면 선택한 사용자 종류, ERP DB, 역할로 재연결됩니다."
            )


        if not companies:
            st.error("활성 회원사 ERP DB가 없습니다. 먼저 SSAI_COMPANIES를 확인하세요.")
            return

        default_idx = _find_default_company_index(companies, requested_company_name)

        company_labels = [
            _approval_company_label(c)
            for c in companies
        ]

        selected_label = st.selectbox(
            "승인할 회원사 ERP DB 선택",
            options=company_labels,
            index=default_idx,
            key=f"__approve_company_{key_base}",
        )

        selected_company = companies[company_labels.index(selected_label)]
        selected_company_code = str(selected_company.get("company_code") or "").strip()

        user_type_options = _user_type_label_options()

        user_type_label = st.selectbox(
            "사용자 종류",
            options=list(user_type_options.keys()),
            index=3,
            key=f"__approve_user_type_{key_base}",
        )

        user_type = user_type_options[user_type_label]

        role_options = _role_options_for_user_type(
            roles=roles,
            user_type=user_type,
        )

        role_label = st.selectbox(
            "역할 선택",
            options=list(role_options.keys()),
            index=0,
            key=f"__approve_role_{key_base}",
        )

        role_code = role_options[role_label]

        default_grade = _grade_for_user_type_and_role(
            user_type=user_type,
            role_code=role_code,
        )

        grade_options = _user_grade_label_options()

        grade_label = st.selectbox(
            "사용자 등급",
            options=list(grade_options.keys()),
            index=_grade_label_index_for_value(default_grade),
            key=f"__approve_grade_{key_base}",
        )

        user_grade = grade_options[grade_label]

        if user_type in ("SSART_ADMIN", "SSART_USER"):
            approve_sims_user_id = st.text_input(
                "SIMS 사용자 ID",
                value=sims_user_id or "admin",
                key=f"__approve_sims_user_id_{key_base}",
                help=(
                    "신성아트컴 사용자도 회원사 ERP DB 접근/변경 시 "
                    "이 SIMS ID와 SIMS Password로 확인합니다."
                ),
            )

            st.info(
                "신성아트컴 내부 사용자로 승인합니다. "
                "승인 후 선택한 역할과 권한에 따라 회원사 지원/관리 기능을 사용할 수 있습니다."
            )
        else:
            approve_sims_user_id = st.text_input(
                "SIMS 사용자 ID",
                value=sims_user_id,
                key=f"__approve_sims_user_id_{key_base}",
                help="회원 사용자는 로그인 시 이 SIMS ID의 비밀번호로 검증합니다.",
            )

        b1, b2, b3 = st.columns([1, 1, 2])

        with b1:
            if st.button("승인", type="primary", width="stretch", key=f"__approve_btn_{key_base}"):
                try:
                    result = approve_user(
                        login_id=login_id,
                        company_code=selected_company_code,
                        role_code=role_code,
                        user_grade=user_grade,
                        user_type=user_type,
                        sims_user_id=approve_sims_user_id,
                        actor_user_id=current_user.user_id if current_user else None,
                        actor_login_id=current_user.login_id if current_user else None,
                    )

                    log.info(
                        "[admin.user] approved login_id=%s company=%s role=%s",
                        login_id,
                        selected_company_code,
                        role_code,
                    )
                    st.success(f"승인 완료: {login_id}")
                    st.json(result)
                    st.rerun()
                except Exception as e:
                    log.exception("[admin.user] approve failed login_id=%s", login_id)
                    st.error(f"승인 중 오류가 발생했습니다: {type(e).__name__}: {e}")

        with b2:
            if st.button("거절", width="stretch", key=f"__reject_btn_{key_base}"):
                try:
                    result = reject_user(
                        login_id=login_id,
                        actor_user_id=current_user.user_id if current_user else None,
                        actor_login_id=current_user.login_id if current_user else None,
                    )

                    log.info("[admin.user] rejected login_id=%s", login_id)
                    st.warning(f"거절 처리 완료: {login_id}")
                    st.json(result)
                    st.rerun()
                except Exception as e:
                    log.exception("[admin.user] reject failed login_id=%s", login_id)
                    st.error(f"거절 중 오류가 발생했습니다: {type(e).__name__}: {e}")

        with b3:
            st.caption(
                "승인하면 사용자는 선택 회원사/ERP DB에 연결되고, 선택 역할이 부여됩니다. "
                "회원 사용자는 이후 SIMS 비밀번호로 로그인합니다."
            )

def _render_user_policy_update_box(
    *,
    selected_user: dict[str, Any],
    manageable_companies: list[dict[str, Any]],
    roles: list[dict[str, Any]],
    current_user: Any,
    allow_all_companies: bool,
) -> None:
    target_login_id = str(selected_user.get("login_id") or "").strip()
    target_user_id = selected_user.get("user_id")
    current_user_id = getattr(current_user, "user_id", None)

    if not target_login_id:
        return

    if current_user_id is not None and int(target_user_id or 0) == int(current_user_id):
        st.info("자기 자신의 사용자 정책은 이 화면에서 변경하지 않습니다.")
        return

    with st.expander("🧩 사용자 정책 변경", expanded=False):
        st.caption(
            "기존 사용자의 사용자 종류, 등급, SIMS ID, 기본 회원사 ERP DB, 역할을 한 번에 재정리합니다."
        )

        if not manageable_companies:
            st.error("관리 가능한 회원사 ERP DB가 없습니다.")
            return

        if not roles:
            st.error("활성 역할 목록이 없습니다.")
            return

        current_user_type = str(selected_user.get("user_type") or "WHOLESALE_USER").strip().upper()

        current_user_grade = str(selected_user.get("user_grade") or "").strip().upper()
        actor_is_super_admin = _is_super_admin_user(current_user)
        actor_is_ssart_manager = _is_ssart_manager_user(current_user)

        # 최고 관리자 계정은 보호한다.
        # 단, 자기 자신 차단은 함수 상단에서 이미 처리한다.
        if current_user_type == "SSART_ADMIN" and current_user_grade == "SUPER":
            st.warning(
                "신성아트컴 최고 관리자 계정은 이 화면에서 정책 변경하지 않습니다. "
                "최고 관리자 계정 변경은 별도 최고관리자 관리 기능에서 처리하세요."
            )
            return

        # 신성관리자 계정 자체의 승격/강등/정책 변경은 SUPER만 허용한다.
        if current_user_type == "SSART_ADMIN" and not actor_is_super_admin:
            st.warning(
                "신성아트컴 관리자 계정 정책 변경은 최고관리자만 처리할 수 있습니다."
            )
            return

        # 신성사용자 정책 변경은 신성관리자 이상이면 허용한다.
        if current_user_type == "SSART_USER" and not actor_is_ssart_manager:
            st.warning(
                "신성아트컴 사용자 정책 변경은 신성관리자 이상만 처리할 수 있습니다."
            )
            return

        if allow_all_companies:
            if actor_is_super_admin:
                user_type_options = {
                    "신성아트컴 관리자": "SSART_ADMIN",
                    "신성아트컴 사용자": "SSART_USER",
                    "도매 관리자": "WHOLESALE_ADMIN",
                    "도매 사용자": "WHOLESALE_USER",
                }
            elif actor_is_ssart_manager and current_user_type == "SSART_USER":
                # 신성관리자는 신성사용자의 정책만 관리한다.
                # 신성사용자 → 신성관리자 승격은 SUPER 전용.
                user_type_options = {
                    "신성아트컴 사용자": "SSART_USER",
                }
            else:
                user_type_options = {
                    "도매 관리자": "WHOLESALE_ADMIN",
                    "도매 사용자": "WHOLESALE_USER",
                }
        else:
            user_type_options = {
                "도매 관리자": "WHOLESALE_ADMIN",
                "도매 사용자": "WHOLESALE_USER",
            }

        user_type_label = st.selectbox(
            "사용자 종류",
            options=list(user_type_options.keys()),
            index=_option_index_by_value(
                user_type_options,
                current_user_type,
                default_index=len(user_type_options) - 1,
            ),
            key=f"__policy_user_type_{target_login_id}",
        )

        policy_user_type = user_type_options[user_type_label]

        role_options = _role_options_for_user_type(
            roles=roles,
            user_type=policy_user_type,
        )

        current_role_code = _first_role_code_from_user(selected_user)

        role_label = st.selectbox(
            "역할",
            options=list(role_options.keys()),
            index=_option_index_by_value(
                role_options,
                current_role_code,
                default_index=0,
            ),
            key=f"__policy_role_{target_login_id}",
        )

        policy_role_code = role_options[role_label]

        policy_user_grade = _grade_for_user_type_and_role(
            user_type=policy_user_type,
            role_code=policy_role_code,
        )

        st.text_input(
            "사용자 등급",
            value=policy_user_grade,
            disabled=True,
            key=f"__policy_grade_{target_login_id}",
            help="사용자 종류와 역할에 따라 자동 결정됩니다. SUPER는 최고관리자 admin 전용입니다.",
        )
        
        company_options: dict[str, int] = {}

        for company in manageable_companies:
            company_id = int(company.get("company_id"))
            company_code = str(company.get("company_code") or "").strip()
            company_name = str(company.get("company_name") or "").strip()
            db_name = str(company.get("db_name") or "").strip()

            label = f"{company_name} ({company_code})"
            if db_name:
                label += f" / {db_name}"

            company_options[label] = company_id

        current_company_id = int(
            selected_user.get("company_id")
            or selected_user.get("default_company_id")
            or 0
        )

        company_values = list(company_options.values())

        try:
            company_index = company_values.index(current_company_id)
        except ValueError:
            company_index = 0

        company_label = st.selectbox(
            "기본 회원사 ERP DB",
            options=list(company_options.keys()),
            index=company_index,
            key=f"__policy_company_{target_login_id}",
        )

        policy_company_id = int(company_options[company_label])

        current_sims_user_id = str(selected_user.get("sims_user_id") or "").strip()

        if policy_user_type in ("SSART_ADMIN", "SSART_USER"):
            policy_sims_user_id = st.text_input(
                "SIMS 사용자 ID",
                value=current_sims_user_id or "admin",
                key=f"__policy_sims_user_id_{target_login_id}",
                help=(
                    "신성아트컴 사용자도 회원사 ERP DB 접근/변경 시 "
                    "이 SIMS ID와 SIMS Password로 확인합니다."
                ),
            )
        else:
            policy_sims_user_id = st.text_input(
                "SIMS 사용자 ID",
                value=current_sims_user_id,
                key=f"__policy_sims_user_id_{target_login_id}",
                help="회원 사용자는 로그인 시 이 SIMS ID의 비밀번호로 검증합니다.",
            )

        st.warning(
            "저장을 누르면 기존 회원사 ERP DB 연결과 역할을 비활성화한 뒤, "
            "선택한 정책으로 다시 연결합니다."
        )

        if st.button(
            "사용자 정책 저장",
            type="primary",
            width="stretch",
            key=f"__policy_save_{target_login_id}",
        ):
            try:
                result = update_user_policy(
                    manager_user_id=int(current_user.user_id),
                    target_login_id=target_login_id,
                    company_id=int(policy_company_id),
                    user_type=policy_user_type,
                    role_code=policy_role_code,
                    user_grade=policy_user_grade,
                    sims_user_id=policy_sims_user_id,
                    actor_login_id=current_user.login_id,
                    allow_all_companies=bool(allow_all_companies),
                )

                _admin_set_flash(
                    f"사용자 정책 변경 완료: {target_login_id}",
                    "success",
                )
                _admin_keep_company_user_tab()
                st.rerun()

            except PermissionError as e:
                # 업무 규칙에 따른 차단은 시스템 오류가 아니므로 ERROR/Traceback으로 남기지 않는다.
                log.warning(
                    "[admin.company_users] policy update blocked target=%s reason=%s",
                    target_login_id,
                    e,
                )
                st.warning(str(e))

            except Exception as e:
                log.exception("[admin.company_users] policy update failed target=%s", target_login_id)
                st.error(f"사용자 정책 변경 중 오류가 발생했습니다: {type(e).__name__}: {e}")

def render_user_approval_page() -> None:
    """
    회원 사용자 승인 관리 화면.
    """
    if _can_approve_signup_users():
        st.title("🛡️ 회원 사용자 승인 / 재승인 관리")
        st.caption("가입 신청 사용자 또는 승인 취소된 사용자를 승인하고 사용자 종류, 회원사 ERP DB, 역할을 연결합니다.")
    else:
        st.title("🛡️ 회원사 사용자 관리")
        st.caption("같은 회원사에 이미 승인된 사용자의 역할과 사용 상태를 관리합니다.")

    _admin_render_flash()

    current_user = get_current_user()

    can_approve_signup = _can_approve_signup_users()
    can_manage_company_users = _can_manage_company_users()    

    if not _can_use_admin_page():
        st.error("이 화면을 사용할 권한이 없습니다.")
        return

    with st.container(border=True):
        st.write("**현재 관리자**")
        if current_user:
            st.write(f"{current_user.user_name} / {current_user.login_id} / {current_user.user_type}")
        else:
            st.write("-")

    try:
        pending_users = list_pending_users(top=200) if can_approve_signup else []
        companies = list_active_companies(top=500)
        roles = list_active_roles(top=500)

    except Exception as e:

        log.exception("[admin.user] load page data failed")
        st.error(f"승인 관리 데이터를 불러오지 못했습니다: {type(e).__name__}: {e}")
        return

    t1, t2, t3 = st.columns(3)
    t1.metric("승인/재승인 대기", len(pending_users))    
    t2.metric("활성 회원사 ERP DB", len(companies))
    t3.metric("관리 기능", "사용 가능")

    if st.button("승인 대기 새로고침", width="stretch", key="__admin_pending_refresh_btn"):
        st.rerun()

    st.divider()

    if SESSION_ADMIN_SUBTAB not in st.session_state:
        st.session_state[SESSION_ADMIN_SUBTAB] = "승인 대기"

    subtab = st.session_state.get(SESSION_ADMIN_SUBTAB, "승인 대기")
    if subtab == "회사 사용자 관리":
        subtab = "회원사 사용자 관리"
        st.session_state[SESSION_ADMIN_SUBTAB] = subtab


    if not can_approve_signup:
        st.session_state[SESSION_ADMIN_SUBTAB] = "회사 사용자 관리"

    subtab = st.session_state.get(SESSION_ADMIN_SUBTAB, "승인 대기")

    if can_approve_signup:
        nav1, nav2 = st.columns(2)

        with nav1:
            if st.button(
                "승인 대기",
                type="primary" if subtab == "승인 대기" else "secondary",
                width="stretch",
                key="__admin_nav_pending",
            ):
                _admin_keep_pending_tab()
                st.rerun()

        with nav2:
            if st.button(
                "회원사 사용자 관리",
                type="primary" if subtab == "회사 사용자 관리" else "secondary",
                width="stretch",
                key="__admin_nav_company_users",
            ):
                _admin_keep_company_user_tab()
                st.rerun()
    else:
        st.info("회원사 관리자는 신규 가입 승인은 할 수 없으며, 같은 회원사에 이미 승인된 사용자만 관리할 수 있습니다.")


    subtab = st.session_state.get(SESSION_ADMIN_SUBTAB, "승인 대기")

    if subtab == "승인 대기" and not can_approve_signup:
        render_company_user_management_page()
        return

    if subtab == "승인 대기":
        if not pending_users:
            st.success("승인/재승인 대기 사용자가 없습니다.")
        else:
            st.subheader("승인/재승인 대기 사용자")

            for user in pending_users:
                _render_pending_user_card(
                    user=user,
                    companies=companies,
                    roles=roles,
                    current_user=current_user,
                )

    else:
        render_company_user_management_page()

def render_company_user_management_page() -> None:
    """
    회원사 사용자 관리 1차 화면.

    - SSART_ADMIN: 전체 회원사 사용자 조회
    - 도매 관리자: 자기 회원사 사용자만 조회
    - 이번 1차는 조회 전용
    """
    st.subheader("회원사 사용자 관리")
    st.caption("관리 권한 범위 내의 회원사 사용자만 조회합니다.")

    current_user = get_current_user()

    if not current_user:
        st.error("로그인 정보가 없습니다.")
        return

    if not _can_manage_company_users():
        st.error("회원사 사용자 관리 권한이 없습니다.")
        return

    allow_all_companies = _can_manage_all_companies()

    try:
        companies = get_manageable_companies(
            manager_user_id=current_user.user_id,
            allow_all_companies=allow_all_companies,
        )

        roles = list_active_roles(top=500)

    except Exception as e:
        log.exception("[admin.company_users] load companies failed")
        st.error(f"관리 가능한 회원사를 불러오지 못했습니다: {type(e).__name__}: {e}")
        return

    if not companies:
        st.info("관리 가능한 회원사가 없습니다.")
        return

    company_labels = [
        (
            f"{c.get('customer_code') or c.get('company_code')} / "
            f"{c.get('customer_name') or c.get('company_name')} / "
            f"ERP DB: {c.get('company_name')} / "
            f"DB: {c.get('db_name')}"
        )
        for c in companies
    ]

    all_company_label = "전체 / 관리 가능한 모든 회원사"

    if allow_all_companies:
        company_select_options = [all_company_label] + company_labels
    else:
        company_select_options = company_labels

    selected_label = st.selectbox(
        "회원사 ERP DB 선택",
        options=company_select_options,
        index=0,
        key="__admin_company_user_company_select",
    )

    if selected_label == all_company_label:
        selected_company = None
        selected_company_id = None
        selected_company_key = "all"
        selected_company_code = "전체"
    else:
        selected_company = companies[company_labels.index(selected_label)]
        selected_company_id = int(selected_company["company_id"])
        selected_company_key = str(selected_company_id)
        selected_company_code = str(selected_company.get("company_code") or "")

    col_filter, col_refresh = st.columns([4, 1])

    with col_filter:
        include_inactive = st.checkbox(
            "비활성 회원사 ERP DB 연결 포함",
            value=False,
            key="__admin_company_user_include_inactive",
        )

    with col_refresh:
        st.write("")
        if st.button(
            "현재 범위 재조회",
            width="stretch",
            key=f"__admin_company_user_refresh_{selected_company_key}",
        ):
            _admin_keep_company_user_tab()
            st.rerun()

    try:
        if selected_company_id is None:
            users = []

            for company in companies:
                company_id = int(company["company_id"])

                company_users = list_managed_company_users(
                    manager_user_id=current_user.user_id,
                    allow_all_companies=allow_all_companies,
                    company_id=company_id,
                    include_inactive=include_inactive,
                    top=1000,
                )

                users.extend(company_users)
        else:
            users = list_managed_company_users(
                manager_user_id=current_user.user_id,
                allow_all_companies=allow_all_companies,
                company_id=selected_company_id,
                include_inactive=include_inactive,
                top=1000,
            )

    except PermissionError as e:
        st.error(str(e))
        return
    except Exception as e:
        log.exception("[admin.company_users] load users failed")
        st.error(f"회원사 사용자 목록을 불러오지 못했습니다: {type(e).__name__}: {e}")
        return

    c1, c2, c3 = st.columns(3)
    c1.metric("선택 회원사/ERP DB", selected_company_code)
    c2.metric("사용자 수", len(users))
    c3.metric("권한 범위", "전체 회원사" if allow_all_companies else "내 회원사")

    if not users:
        st.info("조회된 사용자가 없습니다.")
        return

    import pandas as pd

    df = pd.DataFrame(users)

    display_cols = [
        "company_code",
        "login_id",
        "user_name",
        "user_type",
        "user_grade",
        "sims_user_id",
        "approval_status",
        "is_active",
        "user_company_is_active",
        "active_role_codes",
        "active_role_names",
        "last_login_at",
    ]

    display_cols = [c for c in display_cols if c in df.columns]

    table_df = df[display_cols].reset_index(drop=True)

    selected_row_index: int | None = None

    try:
        event = st.dataframe(
            table_df,
            width="stretch",
            hide_index=True,
            selection_mode="single-row",
            on_select="rerun",
            key=f"__admin_company_user_table_{selected_company_key}",
        )
        selected_row_index = _get_dataframe_selected_row_index(event)
        st.caption("사용자 행을 클릭하면 아래 사용자 작업 대상이 변경됩니다.")
    except TypeError:
        # Streamlit 구버전 호환용 fallback
        st.dataframe(
            table_df,
            width="stretch",
            hide_index=True,
        )
        st.caption("현재 Streamlit 버전에서는 표 행 선택을 지원하지 않아 아래 선택박스로 대상을 선택합니다.")

    user_labels = [
        (
            f"{u.get('login_id')} / {u.get('user_name')} / "
            f"{u.get('user_grade')} / "
            f"{'사용중' if _as_bool(u.get('user_company_is_active')) and _as_bool(u.get('is_active')) else '중지'}"
        )
        for u in users
    ]

    if selected_row_index is not None:
        selected_row_index = max(0, min(selected_row_index, len(users) - 1))
        st.session_state[f"__admin_company_user_selected_index_{selected_company_key}"] = selected_row_index
    else:
        selected_row_index = int(
            st.session_state.get(f"__admin_company_user_selected_index_{selected_company_key}", 0)
        )
        selected_row_index = max(0, min(selected_row_index, len(users) - 1))

    st.divider()
    st.markdown("#### 사용자 작업")

    # 표 선택이 안 되는 환경에서도 수동 선택 가능하게 유지
    selected_user_label = st.selectbox(
        "작업 대상 사용자",
        options=user_labels,
        index=selected_row_index,
        key=f"__admin_company_user_target_select_{selected_company_key}",
    )

    selected_user = users[user_labels.index(selected_user_label)]

    target_company_id = int(
        selected_user.get("company_id")
        or selected_company_id
        or 0
    )

    if target_company_id <= 0:
        st.error("작업 대상 사용자의 회사 ID를 확인할 수 없습니다.")
        return

    st.info(
        f"현재 작업 대상: "
        f"{selected_user.get('company_code') or '-'} / "
        f"{selected_user.get('login_id')} / "
        f"{selected_user.get('user_name')}"
    )

    target_login_id = str(selected_user.get("login_id") or "").strip()
    target_user_name = str(selected_user.get("user_name") or "").strip()
    target_is_active = bool(
        _as_bool(selected_user.get("is_active"))
        and _as_bool(selected_user.get("user_company_is_active"))
    )

    target_approval_status = str(selected_user.get("approval_status") or "").strip().upper()
    target_user_type = str(selected_user.get("user_type") or "").strip().upper()
    is_wholesale_target = target_user_type in ("WHOLESALE_ADMIN", "WHOLESALE_USER")

    is_self = bool(current_user and target_login_id == current_user.login_id)

    current_role_code = _active_role_code_from_user_row(selected_user)
    role_options = _role_label_options()

    role_labels = list(role_options.keys())
    role_codes = list(role_options.values())

    try:
        role_index = role_codes.index(current_role_code)
    except ValueError:
        role_index = 0

    col_role, col_state, col_revoke = st.columns(3)

    with col_role:
        st.write("**역할 변경**")

        new_role_label = st.selectbox(
            "새 역할",
            options=role_labels,
            index=role_index,
            key="__admin_company_user_role_select",
        )

        new_role_code = role_options[new_role_label]

        role_change_disabled = bool(
            is_self
            or not target_is_active
            or not is_wholesale_target
        )

        if st.button(
            "역할 변경 적용",
            type="primary",
            width="stretch",
            disabled=role_change_disabled,
            key="__admin_company_user_role_change_btn",
            help="자기 자신, 사용 중지 사용자, 신성아트컴 사용자는 이 빠른 역할 변경을 사용할 수 없습니다." if role_change_disabled else None,
        ):
            try:
                change_company_user_role(
                    manager_user_id=current_user.user_id,
                    target_login_id=target_login_id,
                    company_id=target_company_id,
                    role_code=new_role_code,
                    allow_all_companies=allow_all_companies,
                )

                _admin_set_flash(
                    f"역할 변경 완료: {target_login_id} → {new_role_label}",
                    "success",
                )
                _admin_keep_company_user_tab()
                st.rerun()

            except Exception as e:
                log.exception("[admin.company_users] role change failed target=%s", target_login_id)
                st.error(f"역할 변경 중 오류가 발생했습니다: {type(e).__name__}: {e}")




    with col_state:
        st.write("**사용 상태**")

        st.info(
            f"대상: {target_user_name or target_login_id}\n\n"
            f"현재 상태: {'사용중' if target_is_active else '사용 중지'}"
        )

        if target_is_active:
            if st.button(
                "사용 중지",
                width="stretch",
                disabled=is_self,
                key="__admin_company_user_disable_btn",
                help="자기 자신은 사용 중지할 수 없습니다." if is_self else None,
            ):
                try:
                    set_company_user_access_active(
                        manager_user_id=current_user.user_id,
                        target_login_id=target_login_id,
                        company_id=target_company_id,
                        is_active=False,
                        allow_all_companies=allow_all_companies,
                    )

                    _admin_set_flash(
                        f"사용 중지 완료: {target_login_id}",
                        "warning",
                    )
                    _admin_keep_company_user_tab()
                    st.rerun()

                except Exception as e:
                    log.exception("[admin.company_users] disable failed target=%s", target_login_id)
                    st.error(f"사용 중지 중 오류가 발생했습니다: {type(e).__name__}: {e}")

        else:
            if st.button(
                "재사용",
                type="primary",
                width="stretch",
                disabled=is_self,
                key="__admin_company_user_enable_btn",
                help="자기 자신은 이 화면에서 재사용 처리할 수 없습니다." if is_self else None,
            ):
                try:
                    set_company_user_access_active(
                        manager_user_id=current_user.user_id,
                        target_login_id=target_login_id,
                        company_id=target_company_id,
                        is_active=True,
                        allow_all_companies=allow_all_companies,
                    )

                    _admin_set_flash(
                        f"재사용 처리 완료: {target_login_id}",
                        "success",
                    )
                    _admin_keep_company_user_tab()
                    st.rerun()

                except Exception as e:
                    log.exception("[admin.company_users] enable failed target=%s", target_login_id)
                    st.error(f"재사용 처리 중 오류가 발생했습니다: {type(e).__name__}: {e}")


    with col_revoke:
        st.write("**승인 / 권한 회수**")

        st.warning(
            "승인 취소는 사용자의 모든 회원사 ERP DB 연결과 역할을 비활성화하고, "
            "상태를 SUSPENDED로 변경합니다."
        )

        revoke_disabled = bool(
            is_self
            or target_approval_status == "SUSPENDED"
        )

        if st.button(
            "승인 취소 / 권한 회수",
            width="stretch",
            disabled=revoke_disabled,
            key="__admin_company_user_revoke_btn",
            help="자기 자신이거나 이미 승인 취소된 사용자는 처리할 수 없습니다." if revoke_disabled else None,
        ):
            try:
                revoke_user_approval(
                    target_login_id=target_login_id,
                    actor_user_id=current_user.user_id,
                    actor_login_id=current_user.login_id,
                    reason="회원사 사용자 관리 화면에서 승인 취소 / 권한 회수",
                )

                _admin_set_flash(
                    f"승인 취소 / 권한 회수 완료: {target_login_id}",
                    "warning",
                )
                _admin_keep_company_user_tab()
                st.rerun()

            except Exception as e:
                log.exception("[admin.company_users] revoke failed target=%s", target_login_id)
                st.error(f"승인 취소 중 오류가 발생했습니다: {type(e).__name__}: {e}")

    _render_user_policy_update_box(
        selected_user=selected_user,
        manageable_companies=companies,
        roles=roles,
        current_user=current_user,
        allow_all_companies=allow_all_companies,
    )

    if is_self:
        st.caption("현재 로그인한 자기 자신의 역할/사용 상태는 이 화면에서 변경할 수 없습니다.")

    with st.expander("상세 JSON", expanded=False):
        st.json(users)

def render_user_lookup_debug() -> None:
    """
    승인 결과 확인용 간단 조회.
    운영 화면에서는 나중에 별도 사용자 관리 화면으로 분리 가능.
    """
    with st.expander("승인 결과 확인", expanded=False):
        login_id = st.text_input("확인할 로그인 ID", value="", key="__admin_lookup_login_id")

        if st.button("확인", width="stretch", key="__admin_lookup_btn"):
            if not login_id.strip():
                st.warning("로그인 ID를 입력하세요.")
                return

            try:
                result = get_user_company_roles(login_id.strip())
                st.json(result)
            except Exception as e:
                st.error(f"조회 중 오류가 발생했습니다: {type(e).__name__}: {e}")

def render_audit_log_page() -> None:
    """
    SS AI 감사 로그 조회 화면.
    """
    st.title("📜 감사 로그 조회")
    st.caption("가입 신청, 승인, 거절, 역할 변경, 사용 중지, 권한 회수, 사용자 정책 변경 이력을 조회합니다.")

    _admin_render_flash()

    current_user = get_current_user()

    if not current_user:
        st.error("로그인 정보가 없습니다.")
        return

    if not _can_view_audit_logs():
        st.error("감사 로그 조회 권한이 없습니다.")
        return

    try:
        event_types = _fetch_audit_event_types()
    except Exception as e:
        log.exception("[admin.audit] load event types failed")
        st.error(f"감사 로그 이벤트 목록을 불러오지 못했습니다: {type(e).__name__}: {e}")
        return

    with st.container(border=True):
        st.write("**조회 조건**")

        c1, c2, c3, c4 = st.columns([2, 2, 2, 2])

        today = date.today()
        default_from = today - timedelta(days=30)

        with c1:
            event_type = st.selectbox(
                "이벤트",
                options=["전체"] + event_types,
                index=0,
                key="__audit_event_type",
            )

        with c2:
            action_result = st.selectbox(
                "결과",
                options=[
                    "전체",
                    "SUCCESS",
                    "FAILURE",
                    "ERROR",
                ],
                index=0,
                key="__audit_action_result",
            )

        with c3:
            date_from = st.date_input(
                "시작일",
                value=default_from,
                key="__audit_date_from",
            )

        with c4:
            date_to = st.date_input(
                "종료일",
                value=today,
                key="__audit_date_to",
            )

        c5, c6 = st.columns([4, 1])

        with c5:
            keyword = st.text_input(
                "검색어",
                value="",
                key="__audit_keyword",
                help="actor_login_id, target_login_id, message, event_type에서 검색합니다.",
            )

        with c6:
            top = st.selectbox(
                "건수",
                options=[50, 100, 200, 500, 1000, 5000],
                index=1,
                key="__audit_top",
            )

        if date_from and date_to and date_from > date_to:
            st.error("시작일은 종료일보다 클 수 없습니다.")
            return

        if st.button(
            "감사 로그 재조회",
            type="primary",
            width="stretch",
            key="__audit_refresh_btn",
        ):
            st.rerun()

    try:
        logs = _fetch_audit_logs(
            top=int(top),
            event_type=event_type,
            action_result=action_result,
            keyword=keyword,
            date_from=date_from,
            date_to=date_to,
        )

    except Exception as e:
        log.exception("[admin.audit] load audit logs failed")
        st.error(f"감사 로그를 불러오지 못했습니다: {type(e).__name__}: {e}")
        return

    m1, m2 = st.columns([1, 3])

    with m1:
        st.metric("조회 건수", len(logs))

    with m2:
        if logs:
            excel_bytes = _make_audit_logs_excel_bytes(logs)

            st.download_button(
                "감사 로그 Excel 다운로드",
                data=excel_bytes,
                file_name=f"ssai_audit_logs_{date.today().strftime('%Y%m%d')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width="stretch",
                key="__audit_excel_download_btn",
            )

    if not logs:
        st.info("조회된 감사 로그가 없습니다.")
        return
    
    import pandas as pd

    df = pd.DataFrame(logs)

    display_cols = [
        "audit_id",
        "created_at",
        "event_type",
        "action_result",
        "actor_login_id",
        "company_id",
        "target_login_id",
        "target_company_id",
        "message",
    ]

    display_cols = [c for c in display_cols if c in df.columns]

    st.dataframe(
        df[display_cols],
        width="stretch",
        hide_index=True,
    )

    st.divider()

    audit_id_options = [
        int(row.get("audit_id"))
        for row in logs
        if row.get("audit_id") is not None
    ]

    selected_audit_id = st.selectbox(
        "상세 조회할 audit_id",
        options=audit_id_options,
        index=0,
        key="__audit_detail_id",
    )

    selected_row = None
    for row in logs:
        if int(row.get("audit_id")) == int(selected_audit_id):
            selected_row = row
            break

    if not selected_row:
        return

    with st.expander("감사 로그 상세", expanded=True):
        st.write("**기본 정보**")

        base_detail = {
            k: str(v) if k == "created_at" else v
            for k, v in selected_row.items()
            if k != "details_json"
        }

        st.json(base_detail)

        st.write("**details_json**")
        st.json(_parse_details_json(selected_row.get("details_json")))


def render_ssai_admin_page() -> bool:
    """
    현재 관리자 페이지가 활성화되어 있으면 렌더링하고 True 반환.
    아니면 False 반환.
    """
    page = st.session_state.get(SESSION_ADMIN_PAGE)

    if not page:
        return False

    if page == "user_approval":
        render_user_approval_page()
        render_user_lookup_debug()
        return True

    if page == "audit_logs":
        render_audit_log_page()
        return True

    if page == "company_admin":
        render_company_admin_page()
        return True

    st.warning(f"알 수 없는 관리자 페이지입니다: {page}")
    return True