# app/ui/ssai_company_admin.py
#
# SS AI Phase 3
# ERP DB 관리 화면
#
# 기능:
# - 회사 DB 목록 조회
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
    set_company_active,
    test_company_connection,
    test_saved_company_connection,
    upsert_company,
)
from app.ui.ssai_login import get_current_user, has_permission

log = logging.getLogger("ssai")


SESSION_COMPANY_ADMIN_FLASH = "__ssai_company_admin_flash"

FORM_COMPANY_CODE = "__ssai_company_form_company_code"
FORM_COMPANY_NAME = "__ssai_company_form_company_name"
FORM_COMPANY_TYPE = "__ssai_company_form_company_type"
FORM_DB_SERVER = "__ssai_company_form_db_server"
FORM_DB_PORT = "__ssai_company_form_db_port"
FORM_DB_NAME = "__ssai_company_form_db_name"
FORM_DB_USER = "__ssai_company_form_db_user"
FORM_DB_PASSWORD = "__ssai_company_form_db_password"
FORM_DB_DRIVER = "__ssai_company_form_db_driver"
FORM_TRUST_CERT = "__ssai_company_form_trust_cert"
FORM_IS_TEST = "__ssai_company_form_is_test"
FORM_IS_ACTIVE = "__ssai_company_form_is_active"


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
        FORM_DB_SERVER: "",
        FORM_DB_PORT: "",
        FORM_DB_NAME: "",
        FORM_DB_USER: "",
        FORM_DB_PASSWORD: "",
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
    st.session_state[FORM_DB_SERVER] = ""
    st.session_state[FORM_DB_PORT] = ""
    st.session_state[FORM_DB_NAME] = ""
    st.session_state[FORM_DB_USER] = ""
    st.session_state[FORM_DB_PASSWORD] = ""
    st.session_state[FORM_DB_DRIVER] = "ODBC Driver 18 for SQL Server"
    st.session_state[FORM_TRUST_CERT] = True
    st.session_state[FORM_IS_TEST] = True
    st.session_state[FORM_IS_ACTIVE] = True


def _load_company_to_form(company: dict[str, Any]) -> None:
    st.session_state[FORM_COMPANY_CODE] = str(company.get("company_code") or "")
    st.session_state[FORM_COMPANY_NAME] = str(company.get("company_name") or "")
    st.session_state[FORM_COMPANY_TYPE] = str(company.get("company_type") or "TEST")
    st.session_state[FORM_DB_SERVER] = str(company.get("db_server") or "")
    st.session_state[FORM_DB_PORT] = "" if company.get("db_port") is None else str(company.get("db_port"))
    st.session_state[FORM_DB_NAME] = str(company.get("db_name") or "")
    st.session_state[FORM_DB_USER] = str(company.get("db_user") or "")
    st.session_state[FORM_DB_PASSWORD] = ""
    st.session_state[FORM_DB_DRIVER] = str(company.get("db_driver") or "ODBC Driver 18 for SQL Server")
    st.session_state[FORM_TRUST_CERT] = _as_bool(company.get("trust_server_certificate")) if "trust_server_certificate" in company else True
    st.session_state[FORM_IS_TEST] = _as_bool(company.get("is_test_company"))
    st.session_state[FORM_IS_ACTIVE] = _as_bool(company.get("is_active"))


def _company_label(company: dict[str, Any]) -> str:
    active_text = "사용" if _as_bool(company.get("is_active")) else "중지"

    return (
        f"{company.get('company_code')} / "
        f"{company.get('company_name')} / "
        f"{company.get('db_name')} / "
        f"{active_text}"
    )


def _render_company_list_tab() -> None:
    st.subheader("ERP DB 목록 / 접속 테스트")

    try:
        companies = list_companies(include_inactive=True)
    except Exception as e:
        log.exception("[company_admin] list_companies failed")
        st.error(f"회사 목록을 불러오지 못했습니다: {type(e).__name__}: {e}")
        return

    c1, c2 = st.columns(2)
    c1.metric("등록 회사 수", len(companies))
    c2.metric("활성 회사 수", sum(1 for c in companies if _as_bool(c.get("is_active"))))

    if not companies:
        st.info("등록된 ERP DB가 없습니다.")
        return

    df = pd.DataFrame(companies)

    display_cols = [
        "company_id",
        "company_code",
        "company_name",
        "company_type",
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

    st.dataframe(
        df[display_cols],
        use_container_width=True,
        hide_index=True,
    )

    st.divider()
    st.markdown("#### 선택 회사 작업")

    labels = [_company_label(c) for c in companies]

    selected_label = st.selectbox(
        "작업할 회사",
        options=labels,
        index=0,
        key="__ssai_company_admin_selected_company",
    )

    selected_company = companies[labels.index(selected_label)]
    company_code = str(selected_company.get("company_code") or "").strip()
    is_active = _as_bool(selected_company.get("is_active"))

    b1, b2, b3, b4 = st.columns(4)

    with b1:
        if st.button("접속 테스트", use_container_width=True, key="__ssai_company_admin_test_saved"):
            try:
                result = test_saved_company_connection(company_code)

                if result.get("ok"):
                    st.success("접속 테스트 성공")
                else:
                    st.error("접속 테스트 실패")

                st.json(result)

            except Exception as e:
                log.exception("[company_admin] test_saved failed company=%s", company_code)
                st.error(f"접속 테스트 중 오류가 발생했습니다: {type(e).__name__}: {e}")

    with b2:
        if st.button("수정 폼에 불러오기", use_container_width=True, key="__ssai_company_admin_load_form"):
            _load_company_to_form(selected_company)
            st.success(f"수정 폼에 불러왔습니다: {company_code}")
            st.caption("위쪽 탭의 'ERP DB 등록 / 수정'으로 이동해 수정 후 저장하세요.")

    with b3:
        if st.button(
            "비활성화",
            use_container_width=True,
            disabled=not is_active,
            key="__ssai_company_admin_deactivate",
        ):
            try:
                set_company_active(company_code=company_code, is_active=False)
                _set_flash(f"비활성화 완료: {company_code}", "warning")
                st.rerun()
            except Exception as e:
                log.exception("[company_admin] deactivate failed company=%s", company_code)
                st.error(f"비활성화 중 오류가 발생했습니다: {type(e).__name__}: {e}")

    with b4:
        if st.button(
            "활성화",
            type="primary",
            use_container_width=True,
            disabled=is_active,
            key="__ssai_company_admin_activate",
        ):
            try:
                set_company_active(company_code=company_code, is_active=True)
                _set_flash(f"활성화 완료: {company_code}", "success")
                st.rerun()
            except Exception as e:
                log.exception("[company_admin] activate failed company=%s", company_code)
                st.error(f"활성화 중 오류가 발생했습니다: {type(e).__name__}: {e}")


def _render_company_form_tab() -> None:
    st.subheader("ERP DB 등록 / 수정")
    st.caption(
        "DB 비밀번호는 저장 시 암호화됩니다. "
        "기존 회사 수정 시 비밀번호를 비워두면 기존 암호화 비밀번호를 유지합니다."
    )

    _ensure_form_defaults()

    if st.button("신규 등록용으로 폼 초기화", use_container_width=True, key="__ssai_company_admin_clear_form"):
        _clear_form()
        st.rerun()

    type_options = ["TEST", "WHOLESALE", "SSART"]
    current_type = str(st.session_state.get(FORM_COMPANY_TYPE) or "TEST").upper()
    type_index = type_options.index(current_type) if current_type in type_options else 0

    with st.form("ssai_company_admin_form", clear_on_submit=False):
        c1, c2, c3 = st.columns([1, 2, 1])

        with c1:
            company_code = st.text_input(
                "회사코드",
                key=FORM_COMPANY_CODE,
                placeholder="예: TEST_SIMS03",
            )

        with c2:
            company_name = st.text_input(
                "회사명",
                key=FORM_COMPANY_NAME,
                placeholder="예: ○○약품 ERP DB",
            )

        with c3:
            company_type = st.selectbox(
                "회사구분",
                options=type_options,
                index=type_index,
                key=FORM_COMPANY_TYPE,
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
                "테스트 회사",
                key=FORM_IS_TEST,
            )

        with c10:
            is_active = st.checkbox(
                "활성",
                key=FORM_IS_ACTIVE,
            )

        test_clicked = st.form_submit_button(
            "입력값 접속 테스트",
            use_container_width=True,
        )

        save_clicked = st.form_submit_button(
            "저장",
            type="primary",
            use_container_width=True,
        )

    if test_clicked:
        if not db_password:
            st.warning("입력값 접속 테스트는 DB 비밀번호 입력이 필요합니다.")
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
                st.success("입력값 접속 테스트 성공")
            else:
                st.error("입력값 접속 테스트 실패")

            st.json(result)

        except Exception as e:
            log.exception("[company_admin] test input failed")
            st.error(f"입력값 접속 테스트 중 오류가 발생했습니다: {type(e).__name__}: {e}")

    if save_clicked:
        try:
            result = upsert_company(
                company_code=company_code,
                company_name=company_name,
                company_type=company_type,
                db_server=db_server,
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
            st.success(f"ERP DB 저장 완료: {company_code} / {action}")
            st.json(result)

        except Exception as e:
            log.exception("[company_admin] save failed company=%s", company_code)
            st.error(f"ERP DB 저장 중 오류가 발생했습니다: {type(e).__name__}: {e}")


def render_company_admin_page() -> None:
    st.title("🗄️ ERP DB 관리")
    st.caption("SS AI에서 사용할 ERP 회사 DB 접속정보를 등록하고 관리합니다.")
    _render_flash()

    current_user = get_current_user()

    if not _can_manage_erp_db():
        st.error("ERP DB 관리 권한이 없습니다.")
        return

    with st.container(border=True):
        st.write("**현재 관리자**")
        if current_user:
            st.write(f"{current_user.user_name} / {current_user.login_id} / {current_user.user_type}")
        else:
            st.write("-")

    tab_list, tab_form = st.tabs([
        "ERP DB 목록 / 접속 테스트",
        "ERP DB 등록 / 수정",
    ])

    with tab_list:
        _render_company_list_tab()

    with tab_form:
        _render_company_form_tab()