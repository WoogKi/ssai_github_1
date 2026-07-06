# app/ui/ssai_company_admin.py
#
# SS AI Phase 3
# 회원사 ERP DB 관리 화면
#
# 기능:
# - 회원사 ERP DB 목록 조회
# - 저장된 회사 DB 접속 테스트
# - ERP DB 신규 등록
# - ERP DB 수정
# - 활성 / 비활성 전환

from __future__ import annotations

import logging
from typing import Any

import pandas as pd
import streamlit as st

from app.services.ssai_company_admin_service import (
    list_companies,
    list_customer_candidates,
    set_company_active,
    test_company_connection,
    test_saved_company_connection,
    test_sims_admin_auth,
    upsert_company,
)
from app.ui.ssai_login import get_current_user, has_permission
from app.services.ssai_audit_service import safe_log_audit_event

log = logging.getLogger("ssai")


SESSION_COMPANY_ADMIN_FLASH = "__ssai_company_admin_flash"

FORM_COMPANY_CODE = "__ssai_company_form_company_code"
FORM_COMPANY_NAME = "__ssai_company_form_company_name"
FORM_COMPANY_TYPE = "__ssai_company_form_company_type"
FORM_CUSTOMER_CODE = "__ssai_company_form_customer_code"
FORM_CUSTOMER_NAME = "__ssai_company_form_customer_name"
FORM_DB_USAGE_TYPE = "__ssai_company_form_db_usage_type"
FORM_ERP_COMPANY_NAME = "__ssai_company_form_erp_company_name"
FORM_DB_SERVER = "__ssai_company_form_db_server"
FORM_DB_PORT = "__ssai_company_form_db_port"
FORM_DB_NAME = "__ssai_company_form_db_name"
FORM_DB_USER = "__ssai_company_form_db_user"
FORM_DB_PASSWORD = "__ssai_company_form_db_password"
FORM_SIMS_ADMIN_PASSWORD = "__ssai_company_form_sims_admin_password"
FORM_DB_DRIVER = "__ssai_company_form_db_driver"
FORM_TRUST_CERT = "__ssai_company_form_trust_cert"
FORM_IS_TEST = "__ssai_company_form_is_test"
FORM_IS_ACTIVE = "__ssai_company_form_is_active"

SESSION_COMPANY_ADMIN_SELECTED_CODE = "__ssai_company_admin_selected_code"
SESSION_COMPANY_ADMIN_PENDING_FORM_VALUES = "__ssai_company_admin_pending_form_values"

def _can_manage_erp_db() -> bool:
    return bool(
        has_permission("COMPANY_MANAGE")
        or has_permission("SIMS_DB_MANAGE")
        or has_permission("USER_MANAGE_ALL")
    )


def _set_flash(message: str, level: str = "success") -> None:
    st.session_state[SESSION_COMPANY_ADMIN_FLASH] = {
        "message": str(message or ""),
        "level": str(level or "success"),
    }

def _safe_log_company_admin_audit(
    *,
    event_type: str,
    action_result: str,
    company_code: str = "",
    company_id: int | None = None,
    message: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    """
    회원사 ERP DB 관리 화면 감사 로그.

    감사 로그 기록 실패가 회원사 ERP DB 관리 기능을 막으면 안 되므로
    예외는 warning만 남기고 계속 진행한다.
    """
    try:
        user = get_current_user()

        safe_log_audit_event(
            event_type=event_type,
            action_result=action_result,
            actor_user_id=int(user.user_id) if user and getattr(user, "user_id", None) is not None else None,
            actor_login_id=str(user.login_id) if user and getattr(user, "login_id", None) else None,
            company_id=int(company_id) if company_id is not None else None,
            target_company_id=int(company_id) if company_id is not None else None,
            message=message,
            details={
                "company_code": company_code,
                **(details or {}),
            },
        )
    except Exception as e:
        log.warning(
            "[company_admin.audit] failed event_type=%s company=%s reason=%s",
            event_type,
            company_code,
            e,
        )


def _render_flash() -> None:
    flash = st.session_state.pop(SESSION_COMPANY_ADMIN_FLASH, None)

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


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return bool(value)

    return str(value or "").strip().lower() in ("1", "true", "y", "yes", "on")


def _ensure_form_defaults() -> None:
    defaults = {
        FORM_COMPANY_CODE: "",
        FORM_COMPANY_NAME: "",
        FORM_COMPANY_TYPE: "TEST",
        FORM_CUSTOMER_CODE: "",
        FORM_CUSTOMER_NAME: "",
        FORM_DB_USAGE_TYPE: "MAIN",
        FORM_ERP_COMPANY_NAME: "",
        FORM_DB_SERVER: "",
        FORM_DB_PORT: "",
        FORM_DB_NAME: "",
        FORM_DB_USER: "",
        FORM_DB_PASSWORD: "",
        FORM_SIMS_ADMIN_PASSWORD: "",
        FORM_DB_DRIVER: "ODBC Driver 18 for SQL Server",
        FORM_TRUST_CERT: True,
        FORM_IS_TEST: True,
        FORM_IS_ACTIVE: True,
    }

    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def _clear_form() -> None:
    st.session_state[FORM_COMPANY_CODE] = ""
    st.session_state[FORM_COMPANY_NAME] = ""
    st.session_state[FORM_COMPANY_TYPE] = "TEST"
    st.session_state[FORM_CUSTOMER_CODE] = ""
    st.session_state[FORM_CUSTOMER_NAME] = ""
    st.session_state[FORM_DB_USAGE_TYPE] = "MAIN"
    st.session_state[FORM_ERP_COMPANY_NAME] = ""
    st.session_state[FORM_DB_SERVER] = ""
    st.session_state[FORM_DB_PORT] = ""
    st.session_state[FORM_DB_NAME] = ""
    st.session_state[FORM_DB_USER] = ""
    st.session_state[FORM_DB_PASSWORD] = ""
    st.session_state[FORM_SIMS_ADMIN_PASSWORD] = ""
    st.session_state[FORM_DB_DRIVER] = "ODBC Driver 18 for SQL Server"
    st.session_state[FORM_TRUST_CERT] = True
    st.session_state[FORM_IS_TEST] = True
    st.session_state[FORM_IS_ACTIVE] = True


def _load_company_to_form(company: dict[str, Any]) -> None:
    st.session_state[FORM_COMPANY_CODE] = str(company.get("company_code") or "")
    st.session_state[FORM_COMPANY_NAME] = str(company.get("company_name") or "")
    st.session_state[FORM_COMPANY_TYPE] = str(company.get("company_type") or "TEST")
    st.session_state[FORM_CUSTOMER_CODE] = str(company.get("customer_code") or company.get("company_code") or "")
    st.session_state[FORM_CUSTOMER_NAME] = str(company.get("customer_name") or company.get("company_name") or "")
    st.session_state[FORM_DB_USAGE_TYPE] = str(company.get("db_usage_type") or "MAIN")
    st.session_state[FORM_ERP_COMPANY_NAME] = str(company.get("erp_company_name") or "")
    st.session_state[FORM_DB_SERVER] = str(company.get("db_server") or "")
    st.session_state[FORM_DB_PORT] = "" if company.get("db_port") is None else str(company.get("db_port"))
    st.session_state[FORM_DB_NAME] = str(company.get("db_name") or "")
    st.session_state[FORM_DB_USER] = str(company.get("db_user") or "")
    st.session_state[FORM_DB_PASSWORD] = ""
    st.session_state[FORM_SIMS_ADMIN_PASSWORD] = ""
    st.session_state[FORM_DB_DRIVER] = str(company.get("db_driver") or "ODBC Driver 18 for SQL Server")
    st.session_state[FORM_TRUST_CERT] = _as_bool(company.get("trust_server_certificate")) if "trust_server_certificate" in company else True
    st.session_state[FORM_IS_TEST] = _as_bool(company.get("is_test_company"))
    st.session_state[FORM_IS_ACTIVE] = _as_bool(company.get("is_active"))


def _company_label(company: dict[str, Any]) -> str:
    active_text = "사용" if _as_bool(company.get("is_active")) else "중지"
    member_code = str(company.get("customer_code") or company.get("company_code") or "").strip()
    member_name = str(
        company.get("customer_name")
        or company.get("erp_company_name")
        or company.get("company_name")
        or ""
    ).strip()
    erp_db_code = str(company.get("company_code") or "").strip()
    erp_db_name = str(company.get("company_name") or "").strip()
    db_name = str(company.get("db_name") or "").strip()

    return (
        f"{member_name or '-'} ({member_code or '-'}) / "
        f"{erp_db_code} / {erp_db_name or '-'} / "
        f"{db_name or '-'} / {active_text}"
    )

def _extract_selected_rows_from_grid(event: Any) -> list[int]:
    """
    Streamlit dataframe 행 선택 결과를 버전 차이와 관계없이 안전하게 읽는다.
    최신 버전: event.selection.rows
    일부 버전: dict 형태
    """
    if event is None:
        return []

    try:
        rows = getattr(getattr(event, "selection", None), "rows", None)
        if rows is not None:
            return list(rows)
    except Exception:
        pass

    if isinstance(event, dict):
        selection = event.get("selection") or {}
        rows = selection.get("rows") or []
        return list(rows)

    return []


def _render_company_list_tab() -> None:
    st.subheader("회원사 ERP DB 목록 / 접속 테스트")

    refresh_col, _ = st.columns([1, 4])

    with refresh_col:
        if st.button("목록 새로고침", use_container_width=True, key="__ssai_company_admin_refresh_list"):
            st.rerun()

    try:
        companies = list_companies(include_inactive=True)
    except Exception as e:
        log.exception("[company_admin] list_companies failed")
        st.error(f"회원사 ERP DB 목록을 불러오지 못했습니다: {type(e).__name__}: {e}")
        return

    c1, c2 = st.columns(2)
    c1.metric("등록 ERP DB 수", len(companies))
    c2.metric("활성 ERP DB 수", sum(1 for c in companies if _as_bool(c.get("is_active"))))

    if not companies:
        st.info("등록된 ERP DB가 없습니다.")
        return

    df = pd.DataFrame(companies)

    display_cols = [
        "company_id",
        "customer_code",
        "customer_name",
        "company_code",
        "company_name",
        "company_type",
        "db_usage_type",
        "erp_company_name",
        "db_server",
        "db_port",
        "db_name",
        "db_user",
        "db_driver",
        "is_test_company",
        "is_active",
        "updated_at",
    ]
    display_cols = [c for c in display_cols if c in df.columns]

    display_df = df[display_cols].copy().reset_index(drop=True)

    st.caption("표에서 회원사 ERP DB 행을 클릭하면 아래 작업 대상이 자동 선택됩니다.")

    selected_rows: list[int] = []

    try:
        grid_event = st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True,
            key="__ssai_company_admin_db_grid",
            on_select="rerun",
            selection_mode="single-row",
            column_config={
                "company_id": "ID",
                "customer_code": "회원사코드",
                "customer_name": "회원사명",
                "company_code": "DB등록코드",
                "company_name": "DB표시명",
                "company_type": "소속/운영 구분",
                "db_usage_type": "DB용도",
                "erp_company_name": "ERP내부회사명",
                "db_server": "DB 서버",
                "db_port": "DB 포트",
                "db_name": "DB명",
                "db_user": "DB 사용자",
                "db_driver": "ODBC Driver",
                "is_test_company": "테스트 DB",
                "is_active": "활성",
                "updated_at": "수정일시",
            },
        )
        selected_rows = _extract_selected_rows_from_grid(grid_event)

    except TypeError:
        st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "company_id": "ID",
                "customer_code": "회원사코드",
                "customer_name": "회원사명",
                "company_code": "DB등록코드",
                "company_name": "DB표시명",
                "company_type": "소속/운영 구분",
                "db_usage_type": "DB용도",
                "erp_company_name": "ERP내부회사명",
                "db_server": "DB 서버",
                "db_port": "DB 포트",
                "db_name": "DB명",
                "db_user": "DB 사용자",
                "db_driver": "ODBC Driver",
                "is_test_company": "테스트 DB",
                "is_active": "활성",
                "updated_at": "수정일시",
            },
        )
        st.info("현재 Streamlit 버전에서는 표 클릭 선택이 지원되지 않습니다. 아래 선택박스를 사용하세요.")

    if selected_rows:
        selected_row_index = int(selected_rows[0])

        if 0 <= selected_row_index < len(companies):
            clicked_company = companies[selected_row_index]
            clicked_code = str(clicked_company.get("company_code") or "").strip()

            if clicked_code:
                st.session_state[SESSION_COMPANY_ADMIN_SELECTED_CODE] = clicked_code

    st.divider()
    st.markdown("#### 선택 회원사 ERP DB 작업")

    labels = [_company_label(c) for c in companies]
    code_to_index = {
        str(c.get("company_code") or "").strip(): i
        for i, c in enumerate(companies)
    }

    selected_code = str(st.session_state.get(SESSION_COMPANY_ADMIN_SELECTED_CODE) or "").strip()
    default_index = code_to_index.get(selected_code, 0)

    selected_label = st.selectbox(
        "작업할 ERP DB",
        options=labels,
        index=default_index,
        key="__ssai_company_admin_selected_company",
    )

    selected_company = companies[labels.index(selected_label)]
    company_code = str(selected_company.get("company_code") or "").strip()
    is_active = _as_bool(selected_company.get("is_active"))

    if company_code:
        st.session_state[SESSION_COMPANY_ADMIN_SELECTED_CODE] = company_code

    st.caption(
        f"현재 선택 회원사 ERP DB: {company_code} / "
        f"{selected_company.get('company_name')} / "
        f"회원사: {selected_company.get('customer_code') or '-'} / {selected_company.get('customer_name') or '-'} / "
        f"{selected_company.get('db_server')}:{selected_company.get('db_port')} / "
        f"{selected_company.get('db_name')}"
    )

    b1, b2, b3, b4 = st.columns(4)

    with b1:
        if st.button("선택 회원사 ERP DB 접속 테스트", use_container_width=True, key="__ssai_company_admin_test_saved"):
            try:
                result = test_saved_company_connection(company_code)

                if result.get("ok"):
                    st.success("선택 회원사 ERP DB 접속 테스트 성공")
                else:
                    st.error("선택 회원사 ERP DB 접속 테스트 실패")

                _safe_log_company_admin_audit(
                    event_type="ERP_DB_CONNECTION_TEST",
                    action_result="SUCCESS" if result.get("ok") else "FAILURE",
                    company_code=company_code,
                    company_id=selected_company.get("company_id"),
                    message="ERP DB 접속 테스트",
                    details={
                        "result_ok": bool(result.get("ok")),
                        "db_name": result.get("db_name") or selected_company.get("db_name"),
                        "db_server": result.get("db_server") or selected_company.get("db_server"),
                        "db_port": result.get("db_port") or selected_company.get("db_port"),
                        "error_type": result.get("error_type"),
                        "message": result.get("message"),
                    },
                )

                st.json(result)

            except Exception as e:
                log.exception("[company_admin] test_saved failed company=%s", company_code)
                st.error(f"접속 테스트 중 오류가 발생했습니다: {type(e).__name__}: {e}")

    with b2:
        if st.button("등록/수정 폼에 불러오기", use_container_width=True, key="__ssai_company_admin_load_form"):
            _load_company_to_form(selected_company)
            st.success(f"등록/수정 폼에 불러왔습니다: {company_code}")
            st.caption("두 번째 탭인 '회원사 ERP DB 등록 / 수정'에서 수정 후 저장하세요.")

    with b3:
        if st.button(
            "선택 회원사 회원사 ERP DB 비활성화",
            use_container_width=True,
            disabled=not is_active,
            key="__ssai_company_admin_deactivate",
        ):
            try:
                set_company_active(company_code=company_code, is_active=False)

                _safe_log_company_admin_audit(
                    event_type="ERP_DB_DEACTIVATE",
                    action_result="SUCCESS",
                    company_code=company_code,
                    company_id=selected_company.get("company_id"),
                    message="회원사 ERP DB 비활성화",
                    details={
                        "company_code": company_code,
                        "is_active": False,
                    },
                )

                _set_flash(f"회원사 ERP DB 비활성화 완료: {company_code}", "warning")
                st.rerun()

            except Exception as e:
                log.exception("[company_admin] deactivate failed company=%s", company_code)
                st.error(f"비활성화 중 오류가 발생했습니다: {type(e).__name__}: {e}")

    with b4:
        if st.button(
            "선택 회원사 회원사 ERP DB 활성화",
            type="primary",
            use_container_width=True,
            disabled=is_active,
            key="__ssai_company_admin_activate",
        ):
            try:
                set_company_active(company_code=company_code, is_active=True)

                _safe_log_company_admin_audit(
                    event_type="ERP_DB_ACTIVATE",
                    action_result="SUCCESS",
                    company_code=company_code,
                    company_id=selected_company.get("company_id"),
                    message="회원사 ERP DB 활성화",
                    details={
                        "company_code": company_code,
                        "is_active": True,
                    },
                )

                _set_flash(f"회원사 ERP DB 활성화 완료: {company_code}", "success")
                st.rerun()

            except Exception as e:
                log.exception("[company_admin] activate failed company=%s", company_code)
                st.error(f"활성화 중 오류가 발생했습니다: {type(e).__name__}: {e}")


def _render_company_form_tab() -> None:
    st.subheader("회원사 ERP DB 등록 / 수정")
    st.caption(
        "회원사 ERP DB 접속정보를 등록/수정합니다. "
        "입력값 접속 테스트를 실행하면 ERP DB 내부 회사명을 조회하여 "
        "ERP DB 표시명에 자동 반영합니다. "
        "DB 비밀번호는 저장 시 암호화됩니다. "
        "기존 ERP DB 수정 시 비밀번호를 비워두면 기존 암호화 비밀번호를 유지합니다."
    )
    
    _ensure_form_defaults()

    pending_values = st.session_state.pop(SESSION_COMPANY_ADMIN_PENDING_FORM_VALUES, None)

    if isinstance(pending_values, dict):
        for key, value in pending_values.items():
            st.session_state[key] = value


    if st.button("신규 등록용으로 폼 초기화", use_container_width=True, key="__ssai_company_admin_clear_form"):
        _clear_form()
        st.rerun()

    type_options = ["TEST", "WHOLESALE", "SSART"]
    current_type = str(st.session_state.get(FORM_COMPANY_TYPE) or "TEST").upper()
    type_index = type_options.index(current_type) if current_type in type_options else 0

    usage_options = ["MAIN", "TEST", "BACKUP", "ANALYSIS", "TRAINING", "ETC"]
    current_usage = str(st.session_state.get(FORM_DB_USAGE_TYPE) or "MAIN").upper()
    usage_index = usage_options.index(current_usage) if current_usage in usage_options else 0

    with st.form("ssai_company_admin_form", clear_on_submit=False):
        c1, c2, c3 = st.columns([1, 2, 1])

        with c1:
            customer_code = st.text_input(
                "회원사코드",
                key=FORM_CUSTOMER_CODE,
                placeholder="예: C00010 또는 TEST_SIMS03",
                help="SSAI 회원사 코드입니다. 같은 회원사의 여러 ERP DB는 같은 회원사코드를 사용할 수 있습니다.",
            )

        with c2:
            customer_name = st.text_input(
                "회원사명",
                key=FORM_CUSTOMER_NAME,
                placeholder="예: ○○약품",
                help="사업자등록번호는 저장하지 않고, 회원사명 기준으로 관리자가 매핑합니다.",
            )

        with c3:
            db_usage_type = st.selectbox(
                "DB용도",
                options=usage_options,
                index=usage_index,
                key=FORM_DB_USAGE_TYPE,
                help="MAIN=운영, TEST=테스트, BACKUP=백업, ANALYSIS=분석, TRAINING=교육",
            )

        c1b, c2b, c3b = st.columns([1, 2, 1])

        with c1b:
            company_code = st.text_input(
                "DB등록코드",
                key=FORM_COMPANY_CODE,
                placeholder="예: C00010_MAIN",
                help="SS AI에서 ERP DB를 식별하는 고유 코드입니다. 내부적으로 company_code에 저장됩니다.",
            )

        with c2b:
            company_name = st.text_input(
                "DB표시명",
                key=FORM_COMPANY_NAME,
                placeholder="예: ○○약품 운영 ERP DB",
                help="화면에 표시할 ERP DB 이름입니다. 내부적으로 company_name에 저장됩니다.",
            )

        with c3b:
            company_type = st.selectbox(
                "소속/운영 구분",
                options=type_options,
                index=type_index,
                key=FORM_COMPANY_TYPE,
                help=(
                    "TEST=개발/테스트 ERP DB, "
                    "WHOLESALE=회원사 도매업체 ERP DB, "
                    "SSART=신성아트 내부/관리용 ERP DB"
                ),
            )

        erp_company_name = st.text_input(
            "ERP내부회사명",
            key=FORM_ERP_COMPANY_NAME,
            placeholder="접속 테스트 또는 SIMS admin 인증 후 자동 반영",
            help="ERP DB에서 읽은 회사명입니다. 자동 조회 실패 시 관리자가 직접 입력할 수 있습니다.",
        )

        c4, c5 = st.columns([2, 1])

        with c4:
            db_server = st.text_input(
                "DB 서버",
                key=FORM_DB_SERVER,
                placeholder="예: 211.119.134.252",
            )

        with c5:
            db_port = st.text_input(
                "DB 포트",
                key=FORM_DB_PORT,
                placeholder="예: 9060",
            )

        c6, c7 = st.columns(2)

        with c6:
            db_name = st.text_input(
                "DB명",
                key=FORM_DB_NAME,
                placeholder="예: srds30db_ai_test",
            )

        with c7:
            db_user = st.text_input(
                "DB 사용자",
                key=FORM_DB_USER,
                placeholder="예: etc_ai",
            )

        db_password = st.text_input(
            "DB 비밀번호",
            type="password",
            key=FORM_DB_PASSWORD,
            help="신규 등록 시 필수입니다. 기존 회사 수정 시 비워두면 기존 비밀번호를 유지합니다.",
        )

        sims_admin_password = st.text_input(
            "SIMS admin 비밀번호",
            type="password",
            key=FORM_SIMS_ADMIN_PASSWORD,
            help="SIMS ID는 admin 고정입니다. 이 비밀번호는 인증에만 사용하고 저장하지 않습니다.",
        )

        db_driver = st.text_input(
            "ODBC Driver",
            key=FORM_DB_DRIVER,
        )

        c8, c9, c10 = st.columns(3)

        with c8:
            trust_server_certificate = st.checkbox(
                "TrustServerCertificate",
                key=FORM_TRUST_CERT,
            )

        with c9:
            is_test_company = st.checkbox(
                "테스트 DB",
                key=FORM_IS_TEST,
            )

        with c10:
            is_active = st.checkbox(
                "활성",
                key=FORM_IS_ACTIVE,
            )

        test_sql_clicked = st.form_submit_button(
            "SQL DB 접속 테스트",
            use_container_width=True,
        )

        test_sims_admin_clicked = st.form_submit_button(
            "SIMS admin 인증 / 회사명 읽기",
            use_container_width=True,
        )

        save_clicked = st.form_submit_button(
            "저장",
            type="primary",
            use_container_width=True,
        )


    candidate_keyword = str(erp_company_name or customer_name or company_name or "").strip()
    if candidate_keyword:
        try:
            candidates = list_customer_candidates(candidate_keyword)
        except Exception as e:
            candidates = []
            st.warning(f"기존 회원사 후보 조회 실패: {type(e).__name__}: {e}")

        if candidates:
            st.markdown("#### 기존 회원사 후보")
            st.caption("사업자등록번호는 저장하지 않습니다. 회원사명 기준 후보만 보여주고, 최종 선택/수정은 관리자가 합니다.")
            st.dataframe(
                pd.DataFrame(candidates),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "customer_code": "회원사코드",
                    "customer_name": "회원사명",
                    "db_count": "등록 DB 수",
                    "last_updated_at": "최근수정일시",
                },
            )

    if test_sql_clicked:
        if not db_password:
            st.warning("SQL DB 접속 테스트는 DB 비밀번호 입력이 필요합니다.")
            return

        try:
            result = test_company_connection(
                db_server=db_server,
                db_port=db_port,
                db_name=db_name,
                db_user=db_user,
                db_password=db_password,
                db_driver=db_driver,
                trust_server_certificate=trust_server_certificate,
            )

            if result.get("ok"):
                suggested_name = str(result.get("suggested_company_name") or "").strip()
                erp_company_name = str(result.get("erp_company_name") or "").strip()

                pending_values = {}

                if erp_company_name:
                    pending_values[FORM_ERP_COMPANY_NAME] = erp_company_name

                    if not str(customer_name or "").strip():
                        pending_values[FORM_CUSTOMER_NAME] = erp_company_name

                if suggested_name and not str(company_name or "").strip():
                    pending_values[FORM_COMPANY_NAME] = suggested_name

                if pending_values:
                    st.session_state[SESSION_COMPANY_ADMIN_PENDING_FORM_VALUES] = pending_values
                    st.success("SQL DB 접속 테스트 성공 / ERP 회사명 정보를 폼에 반영합니다.")
                    st.info(f"ERP DB 내부 회사명: {erp_company_name or '-'}")
                    st.json(result)
                    st.rerun()
                else:
                    st.success("SQL DB 접속 테스트 성공")
                    st.warning("ERP DB 내부 회사명을 찾지 못했습니다. ERP내부회사명을 직접 입력하세요.")
                    st.json(result)
            else:
                st.error("SQL DB 접속 테스트 실패")
                st.json(result)

        except Exception as e:
            log.exception("[company_admin] SQL test input failed")
            st.error(f"SQL DB 접속 테스트 중 오류가 발생했습니다: {type(e).__name__}: {e}")

    if test_sims_admin_clicked:
        if not db_password:
            st.warning("SIMS admin 인증은 DB 비밀번호 입력이 필요합니다.")
            return

        if not sims_admin_password:
            st.warning("SIMS admin 비밀번호를 입력하세요.")
            return

        try:
            result = test_sims_admin_auth(
                db_server=db_server,
                db_port=db_port,
                db_name=db_name,
                db_user=db_user,
                db_password=db_password,
                sims_admin_password=sims_admin_password,
                db_driver=db_driver,
                trust_server_certificate=trust_server_certificate,
            )

            if result.get("ok"):
                suggested_name = str(result.get("suggested_company_name") or "").strip()
                erp_name = str(result.get("erp_company_name") or "").strip()

                pending_values = {}
                if erp_name:
                    pending_values[FORM_ERP_COMPANY_NAME] = erp_name

                    if not str(customer_name or "").strip():
                        pending_values[FORM_CUSTOMER_NAME] = erp_name

                if suggested_name and not str(company_name or "").strip():
                    pending_values[FORM_COMPANY_NAME] = suggested_name

                if pending_values:
                    st.session_state[SESSION_COMPANY_ADMIN_PENDING_FORM_VALUES] = pending_values

                st.success("SIMS admin 인증 성공")
                st.info(
                    f"ERP 내부 회사명: {erp_name or '-'} / "
                    f"admin 사용자명: {result.get('sims_admin_user_name') or '-'}"
                )
                st.json(result)

                if pending_values:
                    st.rerun()
            else:
                st.error("SIMS admin 인증 실패")
                st.json(result)

        except Exception as e:
            log.exception("[company_admin] SIMS admin auth failed")
            st.error(f"SIMS admin 인증 중 오류가 발생했습니다: {type(e).__name__}: {e}")

    if save_clicked:
        try:
            result = upsert_company(
                company_code=company_code,
                company_name=company_name,
                company_type=company_type,
                db_server=db_server,
                customer_code=customer_code,
                customer_name=customer_name,
                db_usage_type=db_usage_type,
                erp_company_name=erp_company_name,
                db_port=db_port,
                db_name=db_name,
                db_user=db_user,
                db_password=db_password,
                db_driver=db_driver,
                trust_server_certificate=trust_server_certificate,
                is_test_company=is_test_company,
                is_active=is_active,
                require_connection_test=True,
            )

            action = result.get("action")
            saved_company = result.get("company") or {}

            _safe_log_company_admin_audit(
                event_type="ERP_DB_SAVE",
                action_result="SUCCESS",
                company_code=company_code,
                company_id=saved_company.get("company_id"),
                message="회원사 ERP DB 등록/수정 저장",
                details={
                    "action": action,
                    "company_code": company_code,
                    "company_name": company_name,
                    "customer_code": customer_code,
                    "customer_name": customer_name,
                    "db_usage_type": db_usage_type,
                    "db_server": db_server,
                    "db_port": db_port,
                    "db_name": db_name,
                    "db_user": db_user,
                    "db_driver": db_driver,
                    "is_test_company": bool(is_test_company),
                    "is_active": bool(is_active),
                    "connection_test_ok": bool((result.get("connection_test") or {}).get("ok")),
                },
            )

            _set_flash(
                f"회원사 ERP DB 저장 완료: {company_code} / {action}. 목록에서 다시 확인하세요.",
                "success",
            )
            st.rerun()
            
        except Exception as e:
            log.exception("[company_admin] save failed company=%s", company_code)
            st.error(f"회원사 ERP DB 저장 중 오류가 발생했습니다: {type(e).__name__}: {e}")


def render_company_admin_page() -> None:
    st.title("🗄️ 회원사 ERP DB 관리")
    st.caption("SS AI에서 사용할 회원사 ERP DB 접속정보를 등록하고 관리합니다.")
    _render_flash()

    current_user = get_current_user()

    if not _can_manage_erp_db():
        st.error("회원사 ERP DB 관리 권한이 없습니다.")
        return

    with st.container(border=True):
        st.write("**현재 관리자**")
        if current_user:
            st.write(f"{current_user.user_name} / {current_user.login_id} / {current_user.user_type}")
        else:
            st.write("-")

    tab_list, tab_form = st.tabs([
        "회원사 ERP DB 목록 / 접속 테스트",
        "회원사 ERP DB 등록 / 수정",
    ])

    with tab_list:
        _render_company_list_tab()

    with tab_form:
        _render_company_form_tab()