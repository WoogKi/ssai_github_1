# app/ui/ssai_login.py
#
# 사용자 로그인
# create 2026/06/22

from __future__ import annotations

import os
import logging
import time

import streamlit as st

from app.services.ssai_auth_service import (
    AuthUser,
    authenticate_ssai_password,
    authenticate_wholesale_sims_password,
    get_active_companies_for_user,
    get_sims_user_for_login,
    verify_sims_plain_password,
)

from app.services.ssai_user_admin_service import (
    create_signup_request,
)

from app.services.ssai_login_profile_service import (
    build_login_profile,
)

from app.services.ssai_storage_service import (
    ensure_user_storage_dirs,
)

SESSION_AUTH_USER = "ssai_auth_user"
SESSION_AUTH_PERMISSIONS = "ssai_auth_permissions"
SESSION_COMPANY = "ssai_selected_company"
SESSION_COMPANY_PICK_MODE = "ssai_company_pick_mode"
SESSION_SIGNUP_OPEN = "__ssai_signup_open"
SESSION_SIGNUP_FLASH = "__ssai_signup_flash"
SESSION_PENDING_WHOLESALE_AUTH = "__ssai_pending_wholesale_auth"
SESSION_LOGIN_FLASH = "__ssai_login_flash"
SESSION_LOGIN_PROFILE = "__ssai_login_profile"
SESSION_LOGIN_GREETING = "__ssai_login_greeting"
SESSION_LOGIN_GREETING_SIG = "__ssai_login_greeting_sig"
SESSION_COMPANY_CHANGE_NOTICE = "__ssai_company_change_notice"


log = logging.getLogger("ssai")



def _safe_log_value(value, limit: int = 120) -> str:
    try:
        s = str(value if value is not None else "").replace("\n", " ").replace("\r", " ").strip()
    except Exception:
        s = ""
    if len(s) > limit:
        return s[:limit].rstrip() + "..."
    return s


def _login_log_context(
    user: AuthUser | None = None,
    company: dict | None = None,
    **extra,
) -> dict:
    """
    인증/회원사 선택 로그용 비식별 컨텍스트.
    """
    try:
        user = user or st.session_state.get(SESSION_AUTH_USER)
    except Exception:
        user = user

    try:
        company = company if company is not None else st.session_state.get(SESSION_COMPANY)
    except Exception:
        company = company

    ctx = {
        "user_id": getattr(user, "user_id", None),
        "user_type": getattr(user, "user_type", None),
        "user_grade": getattr(user, "user_grade", None),
        "company_id": None,
    }

    if isinstance(company, dict):
        ctx.update(
            {
                "company_id": company.get("company_id"),
            }
        )

    blocked_keys = {
        "api_key", "company_name", "connection_string", "db_name", "dsn", "login_id",
        "password", "path", "server", "user", "username",
    }
    ctx.update({key: value for key, value in extra.items() if str(key).lower() not in blocked_keys})
    return ctx


def _login_log_kv(
    user: AuthUser | None = None,
    company: dict | None = None,
    **extra,
) -> str:
    ctx = _login_log_context(user=user, company=company, **extra)
    order = [
        "user_id",
        "user_type",
        "user_grade",
        "company_id",
    ]
    order += [k for k in ctx.keys() if k not in order]

    return " ".join(
        f"{key}={_safe_log_value(ctx.get(key))}"
        for key in order
        if ctx.get(key) not in (None, "")
    )


def _clear_company_dependent_state() -> None:
    """
    회사 변경 시 이전 회사 기준 캐시/현재표/SIMS 결과를 정리한다.

    이유:
    - rddbc060_service 등 여러 서비스가 st.cache_data를 사용한다.
    - 회사 DB가 바뀌어도 함수 인자가 같으면 이전 회사 결과가 재사용될 수 있다.
    """
    try:
        st.cache_data.clear()
    except Exception:
        pass

    try:
        from app.sims.views.dashboard_lite import clear_dashboard_lite_session_state

        clear_dashboard_lite_session_state(st.session_state)
    except Exception:
        log.warning("[auth.company] dashboard state clear failed error_type=DashboardStateClearError")

    # SIMS 현재표 / 분석 컨텍스트 / 다운로드 캐시 정리
    keys_to_clear = [
        "__sims_context",
        "__sims_context_text",
        "__sims_context_note",
        "__sims_ctx",
        "__sims_context_obj",
        "__sims_analysis_ctx",
        "__sims_current_table_source_analysis_ctx",

        "__sims_result",
        "__sims_last_push_sig",
        "__sims_last_push",

        "__sims_last_table_key",
        "__sims_last_table_action",
        "__sims_current_table_source_key",
        "__sims_current_table_source_action",

        "__sims_flash",
        "__sims_flash_close",
        "__sims_flash_csv",
        "__sims_flash_xlsx",

        "__sims_last_render_run_seq",
        "__sims_rendered",
        "__sims_was_final",
        "__sims_panel_active",
        "__sims_force_open",
        "__sims_run_flag",
        "__sims_inner_submit",
    ]

    for key in keys_to_clear:
        st.session_state.pop(key, None)

    # 테이블 저장소 정리
    for key in [
        "sims_tables",
        "sims_export_tables",
        "__sims_export_tables_by_key",
    ]:
        value = st.session_state.get(key)
        if isinstance(value, dict):
            value.clear()

    # 회사 변경 후에는 사용자가 다시 SIMS 작업을 열도록 초기화
    st.session_state["__sims_open"] = False
    st.session_state["__sims_open_ui"] = False
    st.session_state["__sims_panel_active"] = False

def is_logged_in() -> bool:
    return SESSION_AUTH_USER in st.session_state and st.session_state[SESSION_AUTH_USER] is not None


def get_current_user() -> AuthUser | None:
    return st.session_state.get(SESSION_AUTH_USER)


def get_current_permissions() -> list[str]:
    return st.session_state.get(SESSION_AUTH_PERMISSIONS, [])


def has_permission(permission_code: str) -> bool:
    return permission_code in get_current_permissions()


def require_permission(permission_code: str, *, show_error: bool = True) -> bool:
    """
    현재 로그인 사용자가 특정 권한을 가지고 있는지 확인한다.

    사용 예:
        if not require_permission("MASTER_READ"):
            st.stop()
    """
    if has_permission(permission_code):
        return True

    if show_error:
        st.error(f"이 기능을 사용할 권한이 없습니다. 필요 권한: {permission_code}")

    return False

def _display_user_name(user: AuthUser | None) -> str:
    """
    화면 표시용 사용자명.

    SS AI는 사용자 실명을 관리하지 않는다.
    우선순위:
    1. nickname
    2. user_name 호환 컬럼
    3. login_id
    """
    if not user:
        return ""

    nickname = str(user.nickname or "").strip()
    if nickname:
        return nickname

    user_name = str(user.user_name or "").strip()
    if user_name:
        return user_name

    return str(user.login_id or "").strip()


def _can_change_member_erp_db(user: AuthUser | None, companies: list[dict]) -> bool:
    """
    회원사 / ERP DB 변경 버튼 표시 여부.

    정책:
    - 신성아트컴 계정은 전체 ERP DB 접근 가능 권한이 있으면 변경 가능
    - 회원사 관리자는 자기에게 연결된 ERP DB가 2개 이상일 때만 변경 가능
    - 회원사 일반 사용자/조회전용은 변경 불가
    """
    if not user:
        return False

    if len(companies or []) <= 1:
        return False

    user_type = str(user.user_type or "").strip().upper()

    if user_type in {"SSART_ADMIN", "SSART_USER"}:
        return True

    if user_type == "WHOLESALE_ADMIN":
        return True

    if has_permission("USER_MANAGE_COMPANY"):
        return True

    return False


def _can_show_physical_db_name(user: AuthUser | None) -> bool:
    """
    물리 DB명 표시 여부.

    정책:
    - 신성아트컴 관리자급은 표시
    - 신성아트컴 일반 사용자, 회원사 사용자는 숨김
    """
    if not user:
        return False

    user_type = str(user.user_type or "").strip().upper()

    if user_type == "SSART_ADMIN":
        return True

    return bool(
        has_permission("SIMS_DB_MANAGE")
        or has_permission("USER_MANAGE_ALL")
    )

def _set_login_flash(message: str, level: str = "info") -> None:
    st.session_state[SESSION_LOGIN_FLASH] = {
        "message": str(message or ""),
        "level": str(level or "info"),
    }


def _render_login_flash() -> None:
    flash = st.session_state.pop(SESSION_LOGIN_FLASH, None)

    if not isinstance(flash, dict):
        return

    message = flash.get("message") or ""
    level = flash.get("level") or "info"

    if not message:
        return

    if level == "success":
        st.success(message)
    elif level == "error":
        st.error(message)
    elif level == "warning":
        st.warning(message)
    else:
        st.info(message)

def _clear_pending_wholesale_auth() -> None:
    st.session_state.pop(SESSION_PENDING_WHOLESALE_AUTH, None)


def _complete_login(result) -> bool:
    """
    AuthResult를 세션에 반영하고 로그인 완료 처리.
    """
    if not result.success or result.user is None:
        log.warning(
            "[auth.login] failed reason_present=%s",
            bool(getattr(result, "fail_reason", "")),
        )
        st.error(f"로그인 실패: {result.fail_reason}")
        return False

    st.session_state[SESSION_AUTH_USER] = result.user
    st.session_state[SESSION_AUTH_PERMISSIONS] = result.permissions or []

    log.info(
        "[auth.login] success %s permissions=%s",
        _login_log_kv(user=result.user),
        len(result.permissions or []),
    )
    st.session_state[SESSION_COMPANY_PICK_MODE] = False
    _clear_pending_wholesale_auth()

    _set_login_flash("로그인 성공", "success")
    st.rerun()

    return True

def get_selected_company() -> dict | None:
    return st.session_state.get(SESSION_COMPANY)


def _is_ssart_user(user: AuthUser | None) -> bool:
    if not user:
        return False
    return user.user_type in ("SSART_ADMIN", "SSART_USER")


def _is_wholesale_user(user: AuthUser | None) -> bool:
    if not user:
        return False
    return user.user_type in ("WHOLESALE_ADMIN", "WHOLESALE_USER")

def _company_id_of(company: dict | None) -> str:
    if not isinstance(company, dict):
        return ""
    return str(company.get("company_id") or "").strip()


def _company_name_of(company: dict | None) -> str:
    if not isinstance(company, dict):
        return ""
    return str(company.get("company_name") or "").strip()


def _member_code_of(company: dict | None) -> str:
    if not isinstance(company, dict):
        return ""

    return str(
        company.get("customer_code")
        or company.get("company_code")
        or ""
    ).strip()


def _member_name_of(company: dict | None) -> str:
    if not isinstance(company, dict):
        return ""

    return str(
        company.get("customer_name")
        or company.get("erp_company_name")
        or company.get("company_name")
        or ""
    ).strip()


def _erp_db_display_name_of(company: dict | None) -> str:
    if not isinstance(company, dict):
        return ""

    return str(company.get("company_name") or "").strip()


def _db_name_of(company: dict | None) -> str:
    if not isinstance(company, dict):
        return ""

    return str(company.get("db_name") or "").strip()


def _company_selector_label(company: dict) -> str:
    member_name = _member_name_of(company) or "-"
    member_code = _member_code_of(company) or "-"
    erp_db_name = _erp_db_display_name_of(company) or "-"
    db_usage_type = str(company.get("db_usage_type") or company.get("company_type") or "").strip()
    middle = f"{member_name} ({member_code}) / ERP DB: {erp_db_name}"
    if db_usage_type:
        middle += f" / {db_usage_type}"

    return middle


def _after_company_selected(
    company: dict,
    *,
    previous_company: dict | None = None,
    manual_change: bool = False,
) -> None:
    """
    회원사 ERP DB 선택/자동 선택 후 공통 처리.

    - 선택 회원사 ERP DB 세션 저장
    - 사용자별 저장소 폴더 생성
    - SIMS DB에서 로그인 프로필 조회
    - 로그인 인사말 재생성 플래그 초기화
    - 수동 회사 변경이면 안내 메시지 큐 등록
    """
    user = get_current_user()

    st.session_state[SESSION_COMPANY] = company

    if user:
        company_id = company.get("company_id")

        try:
            ensure_user_storage_dirs(
                company_id=company_id,
                user_id=user.user_id,
            )
        except Exception as e:
            st.session_state["__ssai_storage_error"] = f"{type(e).__name__}: {e}"

        try:
            profile = build_login_profile(
                user=user,
                company=company,
            )
            st.session_state[SESSION_LOGIN_PROFILE] = profile
        except Exception as e:
            st.session_state[SESSION_LOGIN_PROFILE] = {
                "company_id": company.get("company_id"),
                "company_name": company.get("company_name"),
                "user_id": user.user_id,
                "login_id": user.login_id,
                "nickname": user.nickname,
                "user_type": user.user_type,
                "user_grade": user.user_grade,
                "sims_user_id": user.sims_user_id,
                "profile_source": "ERROR",
                "profile_error": f"{type(e).__name__}: {e}",
            }

    # 회사/프로필이 바뀌면 화면 인사말은 다시 생성한다.
    st.session_state.pop(SESSION_LOGIN_GREETING, None)
    st.session_state.pop(SESSION_LOGIN_GREETING_SIG, None)

    # 수동 회사 변경이면 채팅방에 남길 안내 메시지를 메인에 큐잉한다.
    if (
        manual_change
        and previous_company
        and _company_id_of(previous_company) != _company_id_of(company)
    ):
        st.session_state[SESSION_COMPANY_CHANGE_NOTICE] = {
            "old_company_id": previous_company.get("company_id"),
            "old_company_name": previous_company.get("company_name"),
            "old_db_name": previous_company.get("db_name"),
            "new_company_id": company.get("company_id"),
            "new_company_name": company.get("company_name"),
            "new_db_name": company.get("db_name"),
        }

    log.info(
        "[auth.company] selected %s manual_change=%s previous_company_id=%s",
        _login_log_kv(user=user, company=company),
        bool(manual_change),
        _company_id_of(previous_company),
    )

def _find_default_company(user: AuthUser, companies: list[dict]) -> dict | None:
    if not companies:
        return None

    # 1순위: 사용자의 default_company_id
    if user.default_company_id:
        for c in companies:
            if int(c.get("company_id") or 0) == int(user.default_company_id):
                return c

    # 2순위: .env 지정 데모 회사
    default_demo_code = os.getenv("SSAI_DEFAULT_DEMO_COMPANY_CODE", "").strip()
    if default_demo_code:
        for c in companies:
            if str(c.get("company_code") or "").strip() == default_demo_code:
                return c

    # 3순위: 테스트 회사 중 첫 번째
    for c in companies:
        if bool(c.get("is_test_company")):
            return c

    # 4순위: 전체 첫 번째
    return companies[0]


def _auto_select_company_if_needed() -> bool:
    user = get_current_user()
    if not user:
        return False

    if get_selected_company() is not None:
        return True

    companies = get_active_companies_for_user(user)

    if not companies:
        log.warning("[auth.company] no accessible company %s", _login_log_kv(user=user))
        st.error("접근 가능한 회사가 없습니다.")
        return False

    # 신성아트컴 사용자는 기본 데모 DB 자동 선택
    if _is_ssart_user(user):
        company = _find_default_company(user, companies)
        if not company:
            st.error("기본 데모 회사를 선택할 수 없습니다.")
            return False

        _clear_company_dependent_state()
        _after_company_selected(company, previous_company=None, manual_change=False)
        return True

    # 도매 사용자는 자기 회사 자동 선택
    if _is_wholesale_user(user):
        company = _find_default_company(user, companies)
        if not company:
            st.error("사용자에게 지정된 회사가 없습니다.")
            return False

        _clear_company_dependent_state()
        _after_company_selected(company, previous_company=None, manual_change=False)
        return True

    return False


def logout() -> None:
    log.info("[auth.logout] %s", _login_log_kv())

    for key in [
        SESSION_AUTH_USER,
        SESSION_AUTH_PERMISSIONS,
        SESSION_COMPANY,
        SESSION_COMPANY_PICK_MODE,
        SESSION_PENDING_WHOLESALE_AUTH,
        SESSION_LOGIN_PROFILE,
        SESSION_LOGIN_GREETING,
        SESSION_LOGIN_GREETING_SIG,
        SESSION_COMPANY_CHANGE_NOTICE,
        "__ssai_company_change_sims_password",
        "__ssai_clear_company_change_sims_password",
    ]:        

        st.session_state.pop(key, None)

    _clear_company_dependent_state()

    # 로그인 사용자 변경 시 이전 사용자의 채팅방 세션을 반드시 제거한다.
    # 그렇지 않으면 admin → 도매 사용자 전환 시 admin 채팅방이 계속 보일 수 있다.
    for key in [
        "chat_rooms",
        "current_room",
        "__chat_owner_user_id",
        "__seq",
        "__queue_ai",
        "__an_busy",
        "__an_job",
        "__an_cancel",
        "__deferred_current_table_followup",
        "__sims_auto_user_input",
    ]:
        st.session_state.pop(key, None)

    st.rerun()


def render_logout_box() -> None:
    user = get_current_user()
    company = get_selected_company()

    if not user:
        return

    try:
        selectable_companies = get_active_companies_for_user(user)
    except Exception as e:
        selectable_companies = []
        log.warning(
            "[auth.company] sidebar company list failed user_id=%s error_type=%s",
            getattr(user, "user_id", None),
            type(e).__name__,
        )

    with st.sidebar:
        st.markdown("### 로그인 정보")
        st.write(f"사용자: **{_display_user_name(user)}**")
        st.write(f"ID: `{user.login_id}`")
        st.write(f"구분: `{user.user_type}`")
        st.write(f"등급: `{user.user_grade}`")

        if company:
            st.markdown("### 사용 회원사")
            st.write(f"회원사: **{_member_name_of(company) or '-'}**")
            st.write(f"ERP DB: **{_erp_db_display_name_of(company) or '-'}**")

        if _can_change_member_erp_db(user, selectable_companies):
            if st.button("회원사 / ERP DB 변경", width="stretch"):
                st.session_state[SESSION_COMPANY_PICK_MODE] = True
                st.rerun()

        if st.button("로그아웃", width="stretch"):
            logout()

def render_company_selector() -> bool:
    user = get_current_user()
    if not user:
        return False

    password_key = "__ssai_company_change_sims_password"
    clear_password_key = "__ssai_clear_company_change_sims_password"

    # 성공 또는 취소한 이전 실행에서 요청한 비밀번호 정리를
    # 위젯이 생성되기 전에 처리한다.
    if st.session_state.pop(clear_password_key, False):
        st.session_state.pop(password_key, None)

    companies = get_active_companies_for_user(user)

    if not companies:
        log.warning("[auth.company] selector no accessible company %s", _login_log_kv(user=user))
        st.error("접근 가능한 회사가 없습니다.")
        return False

    st.title("회원사 / ERP DB 선택")
    st.caption(
        "사용할 회원사 ERP DB를 선택한 뒤 SIMS Password를 입력하고 "
        "Enter를 누르거나 [이 회원사 ERP DB로 접속] 버튼을 누르세요."
    )


    current_company = get_selected_company()

    labels: list[str] = []
    label_to_company: dict[str, dict] = {}

    for c in companies:
        label = _company_selector_label(c)
        labels.append(label)
        label_to_company[label] = c

    default_index = 0
    if current_company:
        current_id = int(current_company.get("company_id") or 0)
        for i, label in enumerate(labels):
            c = label_to_company[label]
            if int(c.get("company_id") or 0) == current_id:
                default_index = i
                break
    else:
        default_company = _find_default_company(user, companies)
        if default_company:
            default_id = int(default_company.get("company_id") or 0)
            for i, label in enumerate(labels):
                c = label_to_company[label]
                if int(c.get("company_id") or 0) == default_id:
                    default_index = i
                    break

    selected_label = st.selectbox(
        "회원사 ERP DB",
        labels,
        index=default_index,
    )

    selected_company = label_to_company[selected_label]

    st.info(
        f"선택 예정 회원사: {_member_name_of(selected_company)} / "
        f"ERP DB: {_erp_db_display_name_of(selected_company)}"
    )

    sims_change_password = ""
    company_change_submitted = False
    company_change_cancelled = False

    # 모든 사용자는 자신의 SIMS 사용자 ID로 선택한 ERP DB의 비밀번호를 확인한다.
    sims_user_id_for_change = str(user.sims_user_id or "").strip()

    # 신성아트컴 사용자는 SIMS 사용자 ID가 없을 때만 기존 admin 기본값을 사용한다.
    if _is_ssart_user(user) and not sims_user_id_for_change:
        sims_user_id_for_change = "admin"

    if not sims_user_id_for_change:
        st.error(
            "내 정보에 SIMS 사용자 ID가 등록되어 있지 않습니다. "
            "관리자에게 SIMS 사용자 ID 등록을 요청하세요."
        )
        return False

    st.warning(
        "회원사/ERP DB 변경은 선택한 ERP DB의 "
        "SIMS Password 확인 후 적용합니다."
    )
    st.caption(f"확인할 SIMS 사용자 ID: `{sims_user_id_for_change}`")

    # 모든 사용자가 Password 입력 후 Enter 또는 버튼으로 제출
    with st.form(
        "__ssai_company_change_form",
        clear_on_submit=False,
        enter_to_submit=True,
    ):
        sims_change_password = st.text_input(
            "SIMS Password 확인",
            type="password",
            key=password_key,
        )

        col1, col2 = st.columns([1, 1])

        with col1:
            company_change_submitted = st.form_submit_button(
                "이 회원사 ERP DB로 접속",
                type="primary",
                width="stretch",
            )

        with col2:
            if current_company is not None:
                company_change_cancelled = st.form_submit_button(
                    "취소",
                    width="stretch",
                )


    if company_change_cancelled:
        st.session_state[clear_password_key] = True
        st.session_state[SESSION_COMPANY_PICK_MODE] = False
        st.rerun()

    if company_change_submitted:
        current_company = get_selected_company()

        current_id = None
        if isinstance(current_company, dict):
            current_id = current_company.get("company_id")

        selected_id = selected_company.get("company_id")


        if not sims_change_password:
            st.error("회원사/ERP DB 변경을 위해 SIMS Password를 입력하세요.")
            return False

        try:
            sims_user = get_sims_user_for_login(
                company_id=int(selected_id),
                sims_user_id=sims_user_id_for_change,
            )
        except Exception as e:
            log.warning(
                "[auth.company] sims password check failed company_id=%s error_type=%s",
                selected_id,
                type(e).__name__,
            )
            st.error(f"SIMS 사용자 확인 중 오류가 발생했습니다: {type(e).__name__}: {e}")
            return False

        if not sims_user:
            st.error(
                "선택한 회원사 ERP DB에서 현재 사용자의 SIMS 사용자 ID를 찾지 못했습니다. "
                "내 정보의 SIMS 사용자 ID를 확인하세요."
            )
            return False

        sims_del_flag = str(sims_user.get("sims_del_flag") or "").strip().upper()
        if sims_del_flag == "E":
            st.error("선택한 회원사 ERP DB의 SIMS 사용자가 삭제/비활성 상태입니다.")
            return False

        if not verify_sims_plain_password(
            sims_change_password,
            str(sims_user.get("sims_password") or ""),
        ):
            st.error("SIMS Password가 일치하지 않습니다.")
            return False

        if str(current_id or "") != str(selected_id or ""):
            _clear_company_dependent_state()

        _after_company_selected(
            selected_company,
            previous_company=current_company if isinstance(current_company, dict) else None,
            manual_change=True,
        )

        st.session_state[clear_password_key] = True
        st.session_state[SESSION_COMPANY_PICK_MODE] = False
        st.success("회원사/ERP DB 선택 완료")
        st.rerun()

    # 회원사 ERP DB 선택 중에는 메인 화면으로 진행하지 않음
    return False

def render_signup_request_box() -> None:
    """
    로그인 화면 하단 사용자 가입 신청 UI.

    상태 정책:
    - 기본은 닫힘
    - 중복 ID / 입력 오류 / 신청 오류 발생 시 열린 상태 유지
    - 가입 신청 성공 시 닫힘

    Phase 3 원칙:
    - 사용자 실명은 받지 않는다.
    - 사용자 Nickname만 관리한다.
    - 연락처는 승인/운영 연락용으로 필수 입력한다.
    - 신청 회원사명은 자유 입력한다. 공개 가입 화면에서 ERP DB 목록은 노출하지 않는다.
    - SIMS 비밀번호는 가입 신청 단계에서 받지 않는다.
    """
    flash = st.session_state.pop(SESSION_SIGNUP_FLASH, None)

    if isinstance(flash, dict):
        level = flash.get("level", "info")
        message = flash.get("message", "")

        if message:
            if level == "success":
                st.success(message)
            elif level == "error":
                st.error(message)
            elif level == "warning":
                st.warning(message)
            else:
                st.info(message)

    signup_open = bool(st.session_state.get(SESSION_SIGNUP_OPEN, False))

    with st.expander("사용자 가입 신청", expanded=signup_open):
        st.caption(
            "사용자는 가입 신청 후 신성아트컴 관리자 승인을 받아야 합니다. "
            "사용자 실명은 저장하지 않고 Nickname만 관리합니다. "
            "SIMS 비밀번호는 가입 신청 단계에서 입력하지 않습니다."
        )

        with st.form("ssai_signup_form", clear_on_submit=False):
            signup_login_id = st.text_input(
                "로그인 ID",
                key="__ssai_signup_login_id",
                placeholder="예: hong01",
            )

            signup_password = st.text_input(
                "로그인 Password",
                type="password",
                key="__ssai_signup_password",
                help="SS AI 로그인용 비밀번호입니다.",
            )

            signup_password_confirm = st.text_input(
                "Password 확인",
                type="password",
                key="__ssai_signup_password_confirm",
            )

            signup_nickname = st.text_input(
                "사용자 Nickname",
                key="__ssai_signup_nickname",
                placeholder="예: 홍길동 담당자, 김부장, 약품관리자 등",
                help="실명이 아니라 SS AI 화면 표시용 별칭입니다.",
            )

            signup_phone = st.text_input(
                "연락처",
                key="__ssai_signup_phone",
                placeholder="예: 010-1234-5678",
                help="승인 확인과 운영 연락을 위한 필수 항목입니다.",
            )

            requested_company_name = st.text_input(
                "신청 회원사명",
                key="__ssai_signup_company_name",
                placeholder="예: ○○약품",
                help="ERP DB 목록은 공개하지 않습니다. 회원사명은 자유 입력 후 관리자가 매핑합니다.",
            )

            signup_sims_user_id = st.text_input(
                "SIMS 사용자 ID",
                key="__ssai_signup_sims_user_id",
                placeholder="예: admin 또는 본인 SIMS ID",
            )

            agree = st.checkbox(
                "가입 신청 후 관리자 승인 전에는 로그인할 수 없습니다.",
                key="__ssai_signup_agree",
            )

            submitted = st.form_submit_button(
                "가입 신청",
                type="primary",
                width="stretch",
            )

        if not submitted:
            return

        if not agree:
            st.session_state[SESSION_SIGNUP_OPEN] = True
            st.session_state[SESSION_SIGNUP_FLASH] = {
                "level": "warning",
                "message": "가입 신청 전 확인 문구에 체크해 주세요.",
            }
            st.rerun()

        try:
            result = create_signup_request(
                login_id=signup_login_id,
                password=signup_password,
                password_confirm=signup_password_confirm,
                nickname=signup_nickname,
                phone=signup_phone,
                requested_company_name=requested_company_name,
                sims_user_id=signup_sims_user_id,
            )

            user = result.get("user") or {}

            st.session_state[SESSION_SIGNUP_OPEN] = False
            st.session_state[SESSION_SIGNUP_FLASH] = {
                "level": "success",
                "message": (
                    "가입 신청이 접수되었습니다. "
                    f"신청 ID: {user.get('login_id')}, "
                    f"상태: {user.get('approval_status')}, "
                    f"신청 회원사: {user.get('requested_company_name')}"
                ),
            }
            st.rerun()

        except Exception as e:
            st.session_state[SESSION_SIGNUP_OPEN] = True
            st.session_state[SESSION_SIGNUP_FLASH] = {
                "level": "error",
                "message": f"가입 신청 중 오류가 발생했습니다: {type(e).__name__}: {e}",
            }
            st.rerun()

def render_login_form() -> bool:
    _render_login_flash()

    pending_auth = st.session_state.get(SESSION_PENDING_WHOLESALE_AUTH)

    if isinstance(pending_auth, dict):
        st.caption("도매 사용자는 SS AI Password 인증 후 SIMS Password 인증이 필요합니다.")

        with st.container(border=True):
            st.markdown("### 2단계: SIMS Password 인증")
            st.write(f"로그인 ID: `{pending_auth.get('login_id')}`")
            st.write(f"사용자: **{pending_auth.get('display_name') or pending_auth.get('login_id')}**")
            st.caption("회원사 ERP에 등록된 SIMS 비밀번호를 입력하세요.")

            with st.form("ssai_sims_password_form", clear_on_submit=False):
                sims_password = st.text_input(
                    "SIMS Password",
                    type="password",
                    key="__ssai_login_sims_password",
                )

                c1, c2 = st.columns(2)

                with c1:
                    sims_submitted = st.form_submit_button(
                        "SIMS 인증 후 로그인",
                        type="primary",
                        width="stretch",
                    )

                with c2:
                    cancel_submitted = st.form_submit_button(
                        "처음으로",
                        width="stretch",
                    )

        if cancel_submitted:
            log.info(
                "[auth.login] wholesale sims auth cancelled user_id=%s",
                _safe_log_value(pending_auth.get("user_id")),
            )
            _clear_pending_wholesale_auth()
            _set_login_flash("SS AI 로그인을 처음부터 다시 진행합니다.", "info")
            st.rerun()

        if not sims_submitted:
            return False

        if not sims_password:
            st.error("SIMS Password를 입력하세요.")
            return False

        result = authenticate_wholesale_sims_password(
            str(pending_auth.get("login_id") or "").strip(),
            sims_password,
        )

        if not result.success:
            log.warning(
                "[auth.login] wholesale sims auth failed user_id=%s reason_present=%s",
                _safe_log_value(pending_auth.get("user_id")),
                bool(result.fail_reason),
            )
            st.error(f"SIMS 인증 실패: {result.fail_reason}")
            return False

        log.info(
            "[auth.login] wholesale sims auth success user_id=%s",
            _safe_log_value(pending_auth.get("user_id")),
        )
        return _complete_login(result)

    st.caption("1단계: SS AI 로그인 ID와 SS AI Password를 입력하세요.")

    with st.form("ssai_login_form", clear_on_submit=False):
        login_id = st.text_input("로그인 ID", value="", placeholder="아이디를 입력하세요")
        password = st.text_input("SS AI Password", type="password")
        submitted = st.form_submit_button("다음", width="stretch")

    # 로그인 버튼을 누르지 않아도 가입 신청 UI는 항상 보여야 한다.
    st.divider()
    render_signup_request_box()

    if not submitted:
        return False

    login_id = str(login_id or "").strip()

    if not login_id:
        st.error("로그인 ID를 입력하세요.")
        return False

    if not password:
        st.error("SS AI Password를 입력하세요.")
        return False

    auth_started_at = time.perf_counter()
    st.session_state["__auth_login_submit_started_at"] = auth_started_at
    result = authenticate_ssai_password(login_id, password)
    st.session_state["__auth_login_authenticate_elapsed"] = max(0.0, time.perf_counter() - auth_started_at)

    if not result.success:
        log.warning(
            "[auth.login] ssai auth failed reason_present=%s",
            bool(result.fail_reason),
        )
        st.error(f"SS AI 인증 실패: {result.fail_reason}")
        return False

    if result.user is None:
        log.warning("[auth.login] user is none")
        st.error("로그인 사용자 정보를 읽지 못했습니다.")
        return False

    if result.user.user_type in ("SSART_ADMIN", "SSART_USER"):
        return _complete_login(result)

    if result.user.user_type in ("WHOLESALE_ADMIN", "WHOLESALE_USER"):
        st.session_state[SESSION_PENDING_WHOLESALE_AUTH] = {
            "login_id": result.user.login_id,
            "user_id": result.user.user_id,
            "user_type": result.user.user_type,
            "user_grade": result.user.user_grade,
            "display_name": _display_user_name(result.user),
        }

        log.info(
            "[auth.login] ssai auth success; waiting sims password %s",
            _login_log_kv(user=result.user),
        )
        _set_login_flash("SS AI 인증 성공. SIMS Password를 입력하세요.", "success")
        st.rerun()
        return False

    log.warning("[auth.login] unknown user_type %s", _login_log_kv(user=result.user))
    st.error(f"알 수 없는 사용자 구분입니다: {result.user.user_type}")
    return False


def require_login() -> bool:
    # 1. 로그인 전에는 로그인 화면만 표시
    if not is_logged_in():
        render_login_form()
        return False

    # 2. 회사 변경 모드가 아닐 때, 선택 회사가 없으면 먼저 자동 선택
    if not st.session_state.get(SESSION_COMPANY_PICK_MODE):
        if get_selected_company() is None:
            if not _auto_select_company_if_needed():
                render_logout_box()
                render_company_selector()
                return False

    # 3. 로그인 후 sidebar 로그인 정보 표시
    render_logout_box()

    # 4. 회원사 / ERP DB 변경 모드일 때는 선택 화면에 머문다
    if st.session_state.get(SESSION_COMPANY_PICK_MODE):
        render_company_selector()
        return False

    # 5. 회원사 ERP DB 선택까지 끝났을 때만 메인 진입
    return True
