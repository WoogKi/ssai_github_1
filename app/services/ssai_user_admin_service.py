# app/services/ssai_user_admin_service.py
#
# SS AI Phase 3
# 사용자 가입 신청 / 승인 / 회사 연결 / 역할 연결 서비스
# create 2026/06/23

from __future__ import annotations

import base64
import hashlib
import os
from typing import Any

import pyodbc

from app.services.ssai_auth_service import connect_ssai_db, get_user_permissions
from app.services.ssai_audit_service import safe_log_audit_event
from app.services.ssai_storage_service import ensure_user_storage_dirs


def _row_to_dict(cursor: pyodbc.Cursor, row) -> dict[str, Any] | None:
    if not row:
        return None

    columns = [col[0] for col in cursor.description]
    return dict(zip(columns, row))


def _fetch_one_dict(
    conn: pyodbc.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> dict[str, Any] | None:
    cur = conn.cursor()
    row = cur.execute(sql, *params).fetchone()
    return _row_to_dict(cur, row)


def _fetch_all_dicts(
    conn: pyodbc.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    cur = conn.cursor()
    rows = cur.execute(sql, *params).fetchall()
    columns = [col[0] for col in cur.description]

    return [dict(zip(columns, row)) for row in rows]

def _is_super_admin_user_in_conn(
    conn: pyodbc.Connection,
    user_id: int,
) -> bool:
    """
    신성아트컴 최고 관리자 여부.

    정책:
    - SSART_ADMIN + SUPER 만 최고관리자로 본다.
    - 최고관리자만 신성아트컴 관리자/사용자 정책을 변경할 수 있다.
    """
    row = _fetch_one_dict(
        conn,
        """
        SELECT TOP 1
            user_id,
            user_type,
            user_grade,
            approval_status,
            is_active
        FROM dbo.SSAI_USERS
        WHERE user_id = ?
        """,
        (int(user_id),),
    )

    if not row:
        return False

    return bool(
        str(row.get("user_type") or "").strip().upper() == "SSART_ADMIN"
        and str(row.get("user_grade") or "").strip().upper() == "SUPER"
        and str(row.get("approval_status") or "").strip().upper() == "APPROVED"
        and bool(row.get("is_active"))
    )

def _is_ssart_manager_user_in_conn(
    conn: pyodbc.Connection,
    user_id: int,
) -> bool:
    """
    신성아트컴 관리자 여부.
    SUPER와 MANAGER 모두 포함한다.
    """
    row = _fetch_one_dict(
        conn,
        """
        SELECT TOP 1
            user_id,
            user_type,
            user_grade,
            approval_status,
            is_active
        FROM dbo.SSAI_USERS
        WHERE user_id = ?
        """,
        (int(user_id),),
    )

    if not row:
        return False

    return bool(
        str(row.get("user_type") or "").strip().upper() == "SSART_ADMIN"
        and str(row.get("user_grade") or "").strip().upper() in ("SUPER", "MANAGER")
        and str(row.get("approval_status") or "").strip().upper() == "APPROVED"
        and bool(row.get("is_active"))
    )



def _safe_ensure_user_storage_dirs(
    *,
    company_id: int,
    user_id: int,
) -> dict[str, Any]:
    """
    사용자별 저장 폴더를 생성한다.

    폴더 생성 실패가 승인/권한 변경 업무를 막지 않도록 결과만 반환한다.
    """
    try:
        return ensure_user_storage_dirs(
            company_id=int(company_id),
            user_id=int(user_id),
        )
    except Exception as e:
        return {
            "ok": False,
            "error_type": type(e).__name__,
            "error_message": str(e),
        }

VALID_USER_TYPES = {
    "SSART_ADMIN",
    "SSART_USER",
    "WHOLESALE_ADMIN",
    "WHOLESALE_USER",
}

VALID_USER_GRADES = {
    "SUPER",
    "MANAGER",
    "STAFF",
    "READONLY",
}

VALID_APPROVAL_STATUSES = {
    "PENDING",
    "APPROVED",
    "REJECTED",
    "SUSPENDED",
}

PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 390000


def _hash_ssai_password(password: str) -> str:
    """
    SS AI 로그인 비밀번호 해시 생성.

    app.services.ssai_auth_service.verify_password()와 같은 포맷을 사용한다.
    포맷: pbkdf2_sha256$iterations$salt$digest
    """
    password = str(password or "")

    if not password:
        raise ValueError("password가 필요합니다.")

    salt = base64.urlsafe_b64encode(os.urandom(18)).decode("utf-8").rstrip("=")
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PASSWORD_ITERATIONS,
    )
    digest_b64 = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")

    return f"{PASSWORD_ALGORITHM}${PASSWORD_ITERATIONS}${salt}${digest_b64}"


def _mask_phone(phone: str) -> str:
    """감사 로그/목록 보조 표시용 연락처 마스킹."""
    text = str(phone or "").strip()

    if not text:
        return ""

    digits = "".join(ch for ch in text if ch.isdigit())

    if len(digits) >= 8:
        return f"{digits[:3]}-****-{digits[-4:]}"

    if len(text) <= 4:
        return "*" * len(text)

    return f"{text[:2]}****{text[-2:]}"


def _normalize_user_type(user_type: str) -> str:
    value = str(user_type or "").strip().upper()

    if value not in VALID_USER_TYPES:
        raise ValueError(
            f"허용되지 않은 사용자 종류입니다. user_type={user_type}, "
            f"allowed={sorted(VALID_USER_TYPES)}"
        )

    return value


def _normalize_user_grade(user_grade: str) -> str:
    value = str(user_grade or "").strip().upper()

    if value not in VALID_USER_GRADES:
        raise ValueError(
            f"허용되지 않은 사용자 등급입니다. user_grade={user_grade}, "
            f"allowed={sorted(VALID_USER_GRADES)}"
        )

    return value


def _default_role_code_for_user_type(user_type: str) -> str:
    """
    사용자 종류 기준 기본 역할 코드.
    실제 역할 존재 여부는 _get_role_id_by_code()에서 검증한다.
    """
    user_type = _normalize_user_type(user_type)

    if user_type == "SSART_ADMIN":
        return "SSART_MANAGER"

    if user_type == "SSART_USER":
        return "SSART_STAFF"

    if user_type == "WHOLESALE_ADMIN":
        return "WHOLESALE_MANAGER"

    return "WHOLESALE_STAFF"


def _default_grade_for_user_type_and_role(
    *,
    user_type: str,
    role_code: str,
) -> str:
    """
    사용자 종류/역할 기준 기본 등급.

    정책:
    - SUPER는 최고관리자 admin 전용이다.
    - 일반 신성아트컴 관리자는 MANAGER이다.
    """
    user_type = _normalize_user_type(user_type)
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


def _role_company_id_for_user_type(
    *,
    user_type: str,
    role_code: str,
    company_id: int,
) -> int | None:
    """
    역할 범위 결정.

    - 신성아트컴 역할은 전역 역할로 둔다. company_id = NULL
    - 도매 역할은 회사 단위 역할로 둔다. company_id = 선택 회사
    """
    user_type = _normalize_user_type(user_type)
    role_code = str(role_code or "").strip().upper()

    if user_type in {"SSART_ADMIN", "SSART_USER"}:
        return None

    if role_code in {"SYSTEM_ADMIN", "SSART_MANAGER"} or role_code.startswith("SSART_"):
        return None

    return int(company_id)

def _allowed_role_codes_for_user_type(user_type: str) -> set[str]:
    """
    사용자 종류별 허용 역할.
    user_type과 role_code가 어긋나는 것을 방지한다.
    """
    user_type = _normalize_user_type(user_type)

    if user_type == "SSART_ADMIN":
        return {"SSART_MANAGER"}

    if user_type == "SSART_USER":
        return {"SSART_STAFF"}

    if user_type == "WHOLESALE_ADMIN":
        return {"WHOLESALE_MANAGER"}

    if user_type == "WHOLESALE_USER":
        return {"WHOLESALE_STAFF", "WHOLESALE_READONLY"}

    return set()


def _normalize_role_code_for_user_type(
    *,
    user_type: str,
    role_code: str,
) -> str:
    """
    user_type에 맞지 않는 role_code가 들어오면 기본 역할로 보정한다.
    """
    user_type = _normalize_user_type(user_type)
    role_code = str(role_code or "").strip().upper()

    default_role_code = _default_role_code_for_user_type(user_type)

    if not role_code:
        return default_role_code

    allowed = _allowed_role_codes_for_user_type(user_type)

    if role_code not in allowed:
        return default_role_code

    return role_code


def get_user_by_login_id(login_id: str) -> dict[str, Any] | None:
    login_id = str(login_id or "").strip()

    if not login_id:
        return None

    sql = """
    SELECT TOP 1
        user_id,
        login_id,
        user_name,
        nickname,
        phone,
        user_type,
        user_grade,
        requested_company_name,
        default_company_id,
        sims_user_id,
        approval_status,
        is_active,
        created_at,
        updated_at
    FROM dbo.SSAI_USERS
    WHERE login_id = ?
    """

    with connect_ssai_db() as conn:
        return _fetch_one_dict(conn, sql, (login_id,))


def create_signup_request(
    *,
    login_id: str,
    password: str = "",
    password_confirm: str = "",
    nickname: str = "",
    phone: str = "",
    requested_company_name: str,
    sims_user_id: str,
    user_name: str = "",
) -> dict[str, Any]:
    """
    사용자 가입 신청.

    Phase 3 원칙:
    - SS AI 로그인 ID / 비밀번호를 가입 신청 단계에서 등록한다.
    - 사용자 실명은 SS AI에 저장하지 않는다.
    - nickname만 사용자별칭으로 관리한다.
    - phone은 승인/운영 연락처로 저장한다.
    - user_name 컬럼은 기존 DB NOT NULL / 기존 코드 호환용으로만 사용한다.
      실제 실명 저장 용도로 사용하지 않고 nickname 또는 login_id를 넣는다.
    - 가입 신청 단계에서는 default_company_id를 비워 둔다.
    - approval_status = PENDING
    - is_active = 0
    - 승인 후 회사 연결과 역할 부여를 한다.
    """
    login_id = str(login_id or "").strip()
    password = str(password or "")
    password_confirm = str(password_confirm or "")
    nickname = str(nickname or "").strip()
    phone = str(phone or "").strip()
    requested_company_name = str(requested_company_name or "").strip()
    sims_user_id = str(sims_user_id or "").strip()

    if not login_id:
        raise ValueError("login_id가 필요합니다.")

    if not password:
        raise ValueError("SS AI 로그인 비밀번호가 필요합니다.")

    if len(password) < 4:
        raise ValueError("SS AI 로그인 비밀번호는 4자리 이상 입력하세요.")

    if password_confirm and password != password_confirm:
        raise ValueError("비밀번호와 비밀번호 확인이 일치하지 않습니다.")

    if not nickname:
        raise ValueError("사용자 Nickname이 필요합니다.")

    if not phone:
        raise ValueError("연락처가 필요합니다.")

    if not requested_company_name:
        raise ValueError("requested_company_name이 필요합니다.")

    if not sims_user_id:
        raise ValueError("sims_user_id가 필요합니다.")

    # user_name은 기존 스키마 호환용이다. 실명 저장 용도로 사용하지 않는다.
    user_name_compat = nickname or str(user_name or "").strip() or login_id
    password_hash = _hash_ssai_password(password)

    with connect_ssai_db() as conn:
        cur = conn.cursor()

        existing = _fetch_one_dict(
            conn,
            """
            SELECT TOP 1
                user_id,
                login_id,
                approval_status,
                is_active
            FROM dbo.SSAI_USERS
            WHERE login_id = ?
            """,
            (login_id,),
        )

        if existing:
            existing_status = str(existing.get("approval_status") or "").strip().upper()
            existing_active = bool(existing.get("is_active"))

            if existing_status == "APPROVED" or existing_active:
                raise ValueError(
                    f"이미 등록되어 사용 중인 로그인 ID입니다. login_id={login_id}"
                )

            cur.execute(
                """
                UPDATE dbo.SSAI_USERS
                SET
                    password_hash = ?,
                    user_name = ?,
                    nickname = ?,
                    phone = ?,
                    user_type = N'WHOLESALE_USER',
                    user_grade = N'STAFF',
                    requested_company_name = ?,
                    default_company_id = NULL,
                    sims_user_id = ?,
                    approval_status = N'PENDING',
                    is_active = 0,
                    updated_at = SYSDATETIME()
                WHERE login_id = ?
                """,
                password_hash,
                user_name_compat,
                nickname,
                phone,
                requested_company_name,
                sims_user_id,
                login_id,
            )
        else:
            cur.execute(
                """
                INSERT INTO dbo.SSAI_USERS (
                    login_id,
                    password_hash,
                    user_name,
                    nickname,
                    phone,
                    user_type,
                    user_grade,
                    requested_company_name,
                    default_company_id,
                    sims_user_id,
                    approval_status,
                    is_active,
                    created_at,
                    updated_at
                )
                VALUES (
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    N'WHOLESALE_USER',
                    N'STAFF',
                    ?,
                    NULL,
                    ?,
                    N'PENDING',
                    0,
                    SYSDATETIME(),
                    SYSDATETIME()
                )
                """,
                login_id,
                password_hash,
                user_name_compat,
                nickname,
                phone,
                requested_company_name,
                sims_user_id,
            )

        conn.commit()

    user = get_user_by_login_id(login_id)

    safe_log_audit_event(
        event_type="SIGNUP_REQUEST",
        action_result="SUCCESS",
        target_user_id=int(user["user_id"]) if user and user.get("user_id") is not None else None,
        target_login_id=login_id,
        message="가입 신청 접수",
        details={
            "login_id": login_id,
            "nickname": nickname,
            "requested_company_name": requested_company_name,
            "sims_user_id": sims_user_id,
            "phone_masked": _mask_phone(phone),
            "approval_status": user.get("approval_status") if user else None,
            "user_type": user.get("user_type") if user else None,
            "user_name_policy": "user_name is compatibility-only; nickname is managed display name",
        },
    )

    return {
        "ok": True,
        "action": "signup_request_created",
        "user": user,
    }

def list_pending_users(top: int = 100) -> list[dict[str, Any]]:
    """
    승인 대기 사용자 목록.
    """
    top = max(1, min(int(top or 100), 1000))

    sql = f"""
    SELECT TOP {top}
        user_id,
        login_id,
        user_name,
        nickname,
        phone,
        user_type,
        user_grade,
        requested_company_name,
        default_company_id,
        sims_user_id,
        approval_status,
        is_active,
        created_at,
        updated_at
    FROM dbo.SSAI_USERS
    WHERE approval_status IN (N'PENDING', N'SUSPENDED')
    ORDER BY
        CASE
            WHEN approval_status = N'PENDING' THEN 0
            WHEN approval_status = N'SUSPENDED' THEN 1
            ELSE 9
        END,
        created_at DESC,
        user_id DESC
        """

    with connect_ssai_db() as conn:
        return _fetch_all_dicts(conn, sql)


def list_active_companies(top: int = 200) -> list[dict[str, Any]]:
    top = max(1, min(int(top or 200), 1000))

    sql = f"""
    SELECT TOP {top}
        company_id,
        company_code,
        company_name,
        company_type,
        db_name,
        is_test_company,
        is_active
    FROM dbo.SSAI_COMPANIES
    WHERE is_active = 1
    ORDER BY is_test_company DESC, company_id
    """

    with connect_ssai_db() as conn:
        return _fetch_all_dicts(conn, sql)

def list_active_roles(top: int = 200) -> list[dict[str, Any]]:
    """
    활성 역할 목록.
    다음 승인/사용자 정책 UI에서 역할 선택용으로 사용한다.
    """
    top = max(1, min(int(top or 200), 1000))

    sql = f"""
    SELECT TOP {top}
        role_id,
        role_code,
        role_name,
        is_active
    FROM dbo.SSAI_ROLES
    WHERE is_active = 1
    ORDER BY role_code
    """

    with connect_ssai_db() as conn:
        return _fetch_all_dicts(conn, sql)

def _get_company_id_by_code(
    conn: pyodbc.Connection,
    company_code: str,
) -> int:
    company_code = str(company_code or "").strip()

    row = _fetch_one_dict(
        conn,
        """
        SELECT TOP 1
            company_id
        FROM dbo.SSAI_COMPANIES
        WHERE company_code = ?
          AND is_active = 1
        """,
        (company_code,),
    )

    if not row:
        raise ValueError(f"활성 회사를 찾지 못했습니다. company_code={company_code}")

    return int(row["company_id"])

def _get_active_company_by_id(
    conn: pyodbc.Connection,
    company_id: int,
) -> dict[str, Any]:
    """
    활성 회사 정보를 company_id로 조회한다.
    """
    row = _fetch_one_dict(
        conn,
        """
        SELECT TOP 1
            company_id,
            company_code,
            company_name,
            company_type,
            db_name,
            is_active
        FROM dbo.SSAI_COMPANIES
        WHERE company_id = ?
          AND is_active = 1
        """,
        (int(company_id),),
    )

    if not row:
        raise ValueError(f"활성 회사를 찾지 못했습니다. company_id={company_id}")

    return row

def _get_role_id_by_code(
    conn: pyodbc.Connection,
    role_code: str,
) -> int:
    role_code = str(role_code or "").strip()

    row = _fetch_one_dict(
        conn,
        """
        SELECT TOP 1
            role_id
        FROM dbo.SSAI_ROLES
        WHERE role_code = ?
          AND is_active = 1
        """,
        (role_code,),
    )

    if not row:
        raise ValueError(f"활성 역할을 찾지 못했습니다. role_code={role_code}")

    return int(row["role_id"])


def assign_user_company(
    *,
    user_id: int,
    company_id: int,
    is_default: bool = True,
) -> None:
    """
    사용자-회사 연결.
    """
    with connect_ssai_db() as conn:
        _assign_user_company_in_conn(
            conn,
            user_id=int(user_id),
            company_id=int(company_id),
            is_default=bool(is_default),
        )
        conn.commit()


def _assign_user_company_in_conn(
    conn: pyodbc.Connection,
    *,
    user_id: int,
    company_id: int,
    is_default: bool = True,
) -> None:
    cur = conn.cursor()

    if is_default:
        cur.execute(
            """
            UPDATE dbo.SSAI_USER_COMPANIES
            SET is_default = 0
            WHERE user_id = ?
            """,
            int(user_id),
        )

    exists = _fetch_one_dict(
        conn,
        """
        SELECT TOP 1
            user_id,
            company_id
        FROM dbo.SSAI_USER_COMPANIES
        WHERE user_id = ?
          AND company_id = ?
        """,
        (int(user_id), int(company_id)),
    )

    if exists:
        cur.execute(
            """
            UPDATE dbo.SSAI_USER_COMPANIES
            SET
                is_default = ?,
                is_active = 1
            WHERE user_id = ?
              AND company_id = ?
            """,
            1 if is_default else 0,
            int(user_id),
            int(company_id),
        )
    else:
        cur.execute(
            """
            INSERT INTO dbo.SSAI_USER_COMPANIES (
                user_id,
                company_id,
                is_default,
                is_active,
                created_at
            )
            VALUES (
                ?,
                ?,
                ?,
                1,
                SYSDATETIME()
            )
            """,
            int(user_id),
            int(company_id),
            1 if is_default else 0,
        )


def assign_user_role(
    *,
    user_id: int,
    role_code: str,
    company_id: int | None = None,
) -> None:
    """
    사용자 역할 연결.
    """
    with connect_ssai_db() as conn:
        role_id = _get_role_id_by_code(conn, role_code)

        _assign_user_role_in_conn(
            conn,
            user_id=int(user_id),
            role_id=role_id,
            company_id=company_id,
        )

        conn.commit()


def _assign_user_role_in_conn(
    conn: pyodbc.Connection,
    *,
    user_id: int,
    role_id: int,
    company_id: int | None,
) -> None:
    cur = conn.cursor()

    if company_id is None:
        exists = _fetch_one_dict(
            conn,
            """
            SELECT TOP 1
                user_id,
                role_id,
                company_id
            FROM dbo.SSAI_USER_ROLES
            WHERE user_id = ?
              AND role_id = ?
              AND company_id IS NULL
            """,
            (int(user_id), int(role_id)),
        )
    else:
        exists = _fetch_one_dict(
            conn,
            """
            SELECT TOP 1
                user_id,
                role_id,
                company_id
            FROM dbo.SSAI_USER_ROLES
            WHERE user_id = ?
              AND role_id = ?
              AND company_id = ?
            """,
            (int(user_id), int(role_id), int(company_id)),
        )

    if exists:
        if company_id is None:
            cur.execute(
                """
                UPDATE dbo.SSAI_USER_ROLES
                SET is_active = 1
                WHERE user_id = ?
                  AND role_id = ?
                  AND company_id IS NULL
                """,
                int(user_id),
                int(role_id),
            )
        else:
            cur.execute(
                """
                UPDATE dbo.SSAI_USER_ROLES
                SET is_active = 1
                WHERE user_id = ?
                  AND role_id = ?
                  AND company_id = ?
                """,
                int(user_id),
                int(role_id),
                int(company_id),
            )
    else:
        cur.execute(
            """
            INSERT INTO dbo.SSAI_USER_ROLES (
                user_id,
                company_id,
                role_id,
                is_active,
                created_at
            )
            VALUES (
                ?,
                ?,
                ?,
                1,
                SYSDATETIME()
            )
            """,
            int(user_id),
            int(company_id) if company_id is not None else None,
            int(role_id),
        )


def approve_user(
    *,
    login_id: str,
    company_code: str,
    role_code: str = "",
    user_grade: str = "",
    user_type: str = "WHOLESALE_USER",
    sims_user_id: str | None = None,
    actor_user_id: int | None = None,
    actor_login_id: str | None = None,
) -> dict[str, Any]:
    """
    가입 신청 사용자 승인.

    처리:
    - 사용자 종류(user_type)를 승인자가 결정할 수 있다.
    - 사용자 등급(user_grade)을 승인자가 결정할 수 있다.
    - 선택 ERP DB를 기본 DB로 연결한다.
    - 사용자 종류/역할에 따라 전역 역할 또는 회사 역할을 부여한다.

    DB 제약조건:
    - user_type: SSART_ADMIN / SSART_USER / WHOLESALE_ADMIN / WHOLESALE_USER
    - user_grade: SUPER / MANAGER / STAFF / READONLY
    - approval_status: PENDING / APPROVED / REJECTED / SUSPENDED
    """
    login_id = str(login_id or "").strip()
    company_code = str(company_code or "").strip()

    user_type = _normalize_user_type(user_type)

    role_code = _normalize_role_code_for_user_type(
        user_type=user_type,
        role_code=role_code,
    )

    user_grade = _default_grade_for_user_type_and_role(
        user_type=user_type,
        role_code=role_code,
    )

    if not login_id:
        raise ValueError("login_id가 필요합니다.")

    if not company_code:
        raise ValueError("company_code가 필요합니다.")

    with connect_ssai_db() as conn:
        user = _fetch_one_dict(
            conn,
            """
            SELECT TOP 1
                user_id,
                login_id,
                user_name,
                user_type,
                user_grade,
                requested_company_name,
                sims_user_id,
                approval_status,
                is_active
            FROM dbo.SSAI_USERS
            WHERE login_id = ?
            """,
            (login_id,),
        )

        if not user:
            raise ValueError(f"사용자를 찾지 못했습니다. login_id={login_id}")

        user_id = int(user["user_id"])
        company_id = _get_company_id_by_code(conn, company_code)
        role_id = _get_role_id_by_code(conn, role_code)

        role_company_id = _role_company_id_for_user_type(
            user_type=user_type,
            role_code=role_code,
            company_id=company_id,
        )

        sims_user_id_to_save = (
            str(sims_user_id).strip()
            if sims_user_id is not None
            else str(user.get("sims_user_id") or "").strip()
        )

        cur = conn.cursor()

        # 기존 연결/역할은 승인 정책 기준으로 재정리한다.
        cur.execute(
            """
            UPDATE dbo.SSAI_USER_COMPANIES
            SET
                is_default = 0,
                is_active = 0
            WHERE user_id = ?
            """,
            user_id,
        )

        cur.execute(
            """
            UPDATE dbo.SSAI_USER_ROLES
            SET is_active = 0
            WHERE user_id = ?
            """,
            user_id,
        )

        cur.execute(
            """
            UPDATE dbo.SSAI_USERS
            SET
                user_type = ?,
                user_grade = ?,
                default_company_id = ?,
                sims_user_id = ?,
                approval_status = N'APPROVED',
                is_active = 1,
                updated_at = SYSDATETIME()
            WHERE user_id = ?
            """,
            user_type,
            user_grade,
            company_id,
            sims_user_id_to_save,
            user_id,
        )

        _assign_user_company_in_conn(
            conn,
            user_id=user_id,
            company_id=company_id,
            is_default=True,
        )

        _assign_user_role_in_conn(
            conn,
            user_id=user_id,
            role_id=role_id,
            company_id=role_company_id,
        )

        conn.commit()

    approved_user = get_user_by_login_id(login_id)

    storage_result = _safe_ensure_user_storage_dirs(
        company_id=company_id,
        user_id=user_id,
    )

    safe_log_audit_event(
        event_type="USER_APPROVE",
        action_result="SUCCESS",
        actor_user_id=actor_user_id,
        actor_login_id=actor_login_id,
        company_id=company_id,
        target_user_id=user_id,
        target_login_id=login_id,
        target_company_id=company_id,
        message="가입 신청 승인",
        details={
            "login_id": login_id,
            "company_code": company_code,
            "company_id": company_id,
            "user_type": user_type,
            "role_code": role_code,
            "role_company_id": role_company_id,
            "user_grade": user_grade,
            "sims_user_id": sims_user_id_to_save,
            "storage_result": storage_result,
        },
    )

    return {
        "ok": True,
        "action": "user_approved",
        "user": approved_user,
        "storage_result": storage_result,
    }

def reject_user(
    *,
    login_id: str,
    actor_user_id: int | None = None,
    actor_login_id: str | None = None,
) -> dict[str, Any]:
    """
    가입 신청 거절.

    현재 테이블에 reject_reason 컬럼이 없으므로 상태만 REJECTED로 변경한다.
    """
    login_id = str(login_id or "").strip()

    if not login_id:
        raise ValueError("login_id가 필요합니다.")

    with connect_ssai_db() as conn:
        target_user = _fetch_one_dict(
            conn,
            """
            SELECT TOP 1
                user_id,
                login_id,
                user_name,
                requested_company_name,
                sims_user_id,
                approval_status
            FROM dbo.SSAI_USERS
            WHERE login_id = ?
            """,
            (login_id,),
        )

        cur = conn.cursor()
        cur.execute(
            """
            UPDATE dbo.SSAI_USERS
            SET
                approval_status = N'REJECTED',
                is_active = 0,
                updated_at = SYSDATETIME()
            WHERE login_id = ?
            """,
            login_id,
        )

        if cur.rowcount <= 0:
            raise ValueError(f"사용자를 찾지 못했습니다. login_id={login_id}")

        conn.commit()

    rejected_user = get_user_by_login_id(login_id)

    safe_log_audit_event(
        event_type="USER_REJECT",
        action_result="SUCCESS",
        actor_user_id=actor_user_id,
        actor_login_id=actor_login_id,
        target_user_id=int(target_user["user_id"]) if target_user and target_user.get("user_id") is not None else None,
        target_login_id=login_id,
        message="가입 신청 거절",
        details={
            "login_id": login_id,
            "before": target_user,
            "after": rejected_user,
        },
    )

    return {
        "ok": True,
        "action": "user_rejected",
        "user": rejected_user,
    }

def revoke_user_approval(
    *,
    target_login_id: str,
    actor_user_id: int | None = None,
    actor_login_id: str | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """
    승인 취소 / 권한 회수.

    DB 제약조건상 REVOKED는 사용할 수 없으므로 SUSPENDED를 사용한다.

    처리:
    - SSAI_USERS.approval_status = SUSPENDED
    - SSAI_USERS.is_active = 0
    - SSAI_USERS.default_company_id = NULL
    - SSAI_USER_COMPANIES 전체 비활성
    - SSAI_USER_ROLES 전체 비활성
    - 감사 로그 USER_APPROVAL_REVOKE 기록
    """
    target_login_id = str(target_login_id or "").strip()
    reason = str(reason or "").strip()

    if not target_login_id:
        raise ValueError("target_login_id가 필요합니다.")

    with connect_ssai_db() as conn:
        target_user = _fetch_one_dict(
            conn,
            """
            SELECT TOP 1
                user_id,
                login_id,
                user_name,
                user_type,
                user_grade,
                default_company_id,
                sims_user_id,
                approval_status,
                is_active
            FROM dbo.SSAI_USERS
            WHERE login_id = ?
            """,
            (target_login_id,),
        )

        if not target_user:
            raise ValueError(f"대상 사용자를 찾지 못했습니다. target_login_id={target_login_id}")

        target_user_id = int(target_user["user_id"])

        if actor_user_id is not None and target_user_id == int(actor_user_id):
            raise PermissionError("자기 자신의 승인은 이 화면에서 취소할 수 없습니다.")

        before_state = get_user_company_roles(target_login_id)

        cur = conn.cursor()

        cur.execute(
            """
            UPDATE dbo.SSAI_USER_COMPANIES
            SET
                is_default = 0,
                is_active = 0
            WHERE user_id = ?
            """,
            target_user_id,
        )

        cur.execute(
            """
            UPDATE dbo.SSAI_USER_ROLES
            SET is_active = 0
            WHERE user_id = ?
            """,
            target_user_id,
        )

        cur.execute(
            """
            UPDATE dbo.SSAI_USERS
            SET
                default_company_id = NULL,
                approval_status = N'SUSPENDED',
                is_active = 0,
                updated_at = SYSDATETIME()
            WHERE user_id = ?
            """,
            target_user_id,
        )

        conn.commit()

    after_state = get_user_company_roles(target_login_id)

    safe_log_audit_event(
        event_type="USER_APPROVAL_REVOKE",
        action_result="SUCCESS",
        actor_user_id=actor_user_id,
        actor_login_id=actor_login_id,
        target_user_id=target_user_id,
        target_login_id=target_login_id,
        message="사용자 승인 취소 / 권한 회수",
        details={
            "target_login_id": target_login_id,
            "reason": reason,
            "before": before_state,
            "after": after_state,
        },
    )

    return {
        "ok": True,
        "action": "user_approval_revoked",
        "target_login_id": target_login_id,
        "user": get_user_by_login_id(target_login_id),
        "before": before_state,
        "after": after_state,
    }



def get_user_company_roles(login_id: str) -> dict[str, Any]:
    """
    사용자 승인/연결 결과 확인용.
    """
    login_id = str(login_id or "").strip()

    with connect_ssai_db() as conn:
        user = _fetch_one_dict(
            conn,
            """
            SELECT TOP 1
                user_id,
                login_id,
                user_name,
                user_type,
                user_grade,
                requested_company_name,
                default_company_id,
                sims_user_id,
                approval_status,
                is_active
            FROM dbo.SSAI_USERS
            WHERE login_id = ?
            """,
            (login_id,),
        )

        if not user:
            return {
                "user": None,
                "companies": [],
                "roles": [],
            }

        user_id = int(user["user_id"])

        companies = _fetch_all_dicts(
            conn,
            """
            SELECT
                c.company_id,
                c.company_code,
                c.company_name,
                c.db_name,
                uc.is_default,
                uc.is_active
            FROM dbo.SSAI_USER_COMPANIES uc
            JOIN dbo.SSAI_COMPANIES c
                ON c.company_id = uc.company_id
            WHERE uc.user_id = ?
            ORDER BY uc.is_default DESC, c.company_id
            """,
            (user_id,),
        )

        roles = _fetch_all_dicts(
            conn,
            """
            SELECT
                c.company_code,
                r.role_code,
                r.role_name,
                ur.is_active
            FROM dbo.SSAI_USER_ROLES ur
            JOIN dbo.SSAI_ROLES r
                ON r.role_id = ur.role_id
            LEFT JOIN dbo.SSAI_COMPANIES c
                ON c.company_id = ur.company_id
            WHERE ur.user_id = ?
            ORDER BY c.company_code, r.role_code
            """,
            (user_id,),
        )

    return {
        "user": user,
        "companies": companies,
        "roles": roles,
    }

def _get_user_active_company_ids(
    conn: pyodbc.Connection,
    *,
    user_id: int,
) -> list[int]:
    """
    사용자가 연결된 활성 회사 ID 목록.
    """
    rows = _fetch_all_dicts(
        conn,
        """
        SELECT
            uc.company_id
        FROM dbo.SSAI_USER_COMPANIES uc
        JOIN dbo.SSAI_COMPANIES c
            ON c.company_id = uc.company_id
        WHERE uc.user_id = ?
          AND uc.is_active = 1
          AND c.is_active = 1
        ORDER BY uc.is_default DESC, uc.company_id
        """,
        (int(user_id),),
    )

    return [int(r["company_id"]) for r in rows]


def list_managed_company_users(
    *,
    manager_user_id: int,
    allow_all_companies: bool = False,
    company_id: int | None = None,
    include_inactive: bool = True,
    top: int = 500,
) -> list[dict[str, Any]]:
    """
    관리자 권한 범위에 따른 사용자 목록 조회.

    정책:
    - allow_all_companies=True
      → 신성아트컴 관리자용. 전체 회사 사용자 조회 가능.
    - allow_all_companies=False
      → 도매 관리자용. manager_user_id가 연결된 회사 사용자만 조회 가능.
    """
    top = max(1, min(int(top or 500), 2000))

    with connect_ssai_db() as conn:
        allowed_company_ids: list[int] = []

        if not allow_all_companies:
            allowed_company_ids = _get_user_active_company_ids(
                conn,
                user_id=int(manager_user_id),
            )

            if not allowed_company_ids:
                return []

            if company_id is not None and int(company_id) not in allowed_company_ids:
                raise PermissionError(
                    f"해당 회사 사용자 목록을 조회할 권한이 없습니다. company_id={company_id}"
                )

        params: list[Any] = []
        where_parts: list[str] = []

        if company_id is not None:
            where_parts.append("c.company_id = ?")
            params.append(int(company_id))
        elif not allow_all_companies:
            placeholders = ",".join("?" for _ in allowed_company_ids)
            where_parts.append(f"c.company_id IN ({placeholders})")
            params.extend(allowed_company_ids)

        if not include_inactive:
            where_parts.append("u.is_active = 1")
            where_parts.append("uc.is_active = 1")
            where_parts.append("c.is_active = 1")
            
        where_sql = ""
        if where_parts:
            where_sql = "WHERE " + "\n  AND ".join(where_parts)

        sql = f"""
        SELECT TOP {top}
            u.user_id,
            u.login_id,
            u.user_name,
            u.nickname,
            u.phone,
            u.user_type,
            u.user_grade,
            u.requested_company_name,
            u.default_company_id,
            u.sims_user_id,
            u.approval_status,
            u.is_active,
            c.company_id,
            c.company_code,
            c.company_name,
            c.db_name,
            uc.is_default,
            uc.is_active AS user_company_is_active,
            STRING_AGG(r.role_code, ', ') AS role_codes,
            STRING_AGG(r.role_name, ', ') AS role_names,
            STRING_AGG(CASE WHEN ur.is_active = 1 THEN r.role_code END, ', ') AS active_role_codes,
            STRING_AGG(CASE WHEN ur.is_active = 1 THEN r.role_name END, ', ') AS active_role_names,
            MAX(CASE WHEN ur.is_active = 1 THEN 1 ELSE 0 END) AS has_active_role,
            u.created_at,
            u.updated_at,
            u.last_login_at
        FROM dbo.SSAI_USER_COMPANIES uc
        JOIN dbo.SSAI_USERS u
            ON u.user_id = uc.user_id
        JOIN dbo.SSAI_COMPANIES c
            ON c.company_id = uc.company_id
        LEFT JOIN dbo.SSAI_USER_ROLES ur
            ON ur.user_id = u.user_id
           AND (
                ur.company_id = c.company_id
                OR ur.company_id IS NULL
           )
        LEFT JOIN dbo.SSAI_ROLES r
            ON r.role_id = ur.role_id
        {where_sql}
        GROUP BY
            u.user_id,
            u.login_id,
            u.user_name,
            u.nickname,
            u.phone,
            u.user_type,
            u.user_grade,
            u.requested_company_name,
            u.default_company_id,
            u.sims_user_id,
            u.approval_status,
            u.is_active,
            c.company_id,
            c.company_code,
            c.company_name,
            c.db_name,
            uc.is_default,
            uc.is_active,
            u.created_at,
            u.updated_at,
            u.last_login_at
        ORDER BY
            c.company_code,
            u.user_type,
            u.user_grade,
            u.login_id
        """

        return _fetch_all_dicts(conn, sql, tuple(params))


def get_manageable_companies(
    *,
    manager_user_id: int,
    allow_all_companies: bool = False,
) -> list[dict[str, Any]]:
    """
    현재 관리자가 관리 가능한 회사 목록.

    - 신성아트컴 관리자: 전체 활성 회사
    - 도매 관리자: 본인에게 연결된 활성 회사
    """
    with connect_ssai_db() as conn:
        if allow_all_companies:
            return _fetch_all_dicts(
                conn,
                """
                SELECT
                    company_id,
                    company_code,
                    company_name,
                    company_type,
                    db_name,
                    is_test_company,
                    is_active
                FROM dbo.SSAI_COMPANIES
                WHERE is_active = 1
                ORDER BY is_test_company DESC, company_id
                """,
            )

        return _fetch_all_dicts(
            conn,
            """
            SELECT
                c.company_id,
                c.company_code,
                c.company_name,
                c.company_type,
                c.db_name,
                c.is_test_company,
                c.is_active,
                uc.is_default
            FROM dbo.SSAI_USER_COMPANIES uc
            JOIN dbo.SSAI_COMPANIES c
                ON c.company_id = uc.company_id
            WHERE uc.user_id = ?
              AND uc.is_active = 1
              AND c.is_active = 1
            ORDER BY uc.is_default DESC, c.company_id
            """,
            (int(manager_user_id),),
        )

def _assert_manager_can_manage_company(
    conn: pyodbc.Connection,
    *,
    manager_user_id: int,
    company_id: int,
    allow_all_companies: bool = False,
) -> None:
    """
    현재 관리자가 company_id를 관리할 수 있는지 확인한다.
    """
    if allow_all_companies:
        return

    allowed_company_ids = _get_user_active_company_ids(
        conn,
        user_id=int(manager_user_id),
    )

    if int(company_id) not in allowed_company_ids:
        raise PermissionError(
            f"해당 회사를 관리할 권한이 없습니다. company_id={company_id}"
        )


def _get_user_by_id_or_login(
    conn: pyodbc.Connection,
    *,
    target_user_id: int | None = None,
    target_login_id: str | None = None,
) -> dict[str, Any]:
    """
    user_id 또는 login_id로 대상 사용자를 찾는다.
    """
    if target_user_id:
        row = _fetch_one_dict(
            conn,
            """
            SELECT TOP 1
                user_id,
                login_id,
                user_name,
                user_type,
                user_grade,
                default_company_id,
                approval_status,
                is_active
            FROM dbo.SSAI_USERS
            WHERE user_id = ?
            """,
            (int(target_user_id),),
        )
    else:
        login_id = str(target_login_id or "").strip()
        row = _fetch_one_dict(
            conn,
            """
            SELECT TOP 1
                user_id,
                login_id,
                user_name,
                user_type,
                user_grade,
                default_company_id,
                approval_status,
                is_active
            FROM dbo.SSAI_USERS
            WHERE login_id = ?
            """,
            (login_id,),
        )

    if not row:
        raise ValueError("대상 사용자를 찾지 못했습니다.")

    return row


def _assert_user_belongs_to_company(
    conn: pyodbc.Connection,
    *,
    user_id: int,
    company_id: int,
) -> None:
    """
    대상 사용자가 해당 회사에 연결되어 있는지 확인한다.
    """
    row = _fetch_one_dict(
        conn,
        """
        SELECT TOP 1
            user_id,
            company_id
        FROM dbo.SSAI_USER_COMPANIES
        WHERE user_id = ?
          AND company_id = ?
        """,
        (int(user_id), int(company_id)),
    )

    if not row:
        raise PermissionError(
            f"대상 사용자가 해당 회사에 연결되어 있지 않습니다. user_id={user_id}, company_id={company_id}"
        )


KNOWLEDGE_EFFECTIVE_PERMISSION_CODES = (
    "RAG_USE",
    "KNOWLEDGE_PROJECT_SOURCE_READ",
    "KNOWLEDGE_ERP_DB_READ",
    "KNOWLEDGE_GLOBAL_MANAGE",
    "KNOWLEDGE_COMPANY_MANAGE",
)


def get_managed_user_knowledge_permissions(
    *,
    manager_user_id: int,
    target_login_id: str,
    company_id: int,
    allow_all_companies: bool = False,
) -> dict[str, Any]:
    """관리 가능한 회사 범위 안에서 대상 사용자의 Knowledge 실효 권한만 조회한다.

    역할 변경과 별개인 read-only readback 용도다. 대상 사용자/회사 소속과
    관리자 회사 범위를 먼저 확인해 다른 회사의 권한이 섞여 보이지 않게 한다.
    """
    normalized_login_id = str(target_login_id or "").strip()
    normalized_company_id = int(company_id or 0)

    if not normalized_login_id:
        raise ValueError("대상 로그인 ID가 필요합니다.")
    if normalized_company_id <= 0:
        raise ValueError("유효한 회사 ID가 필요합니다.")

    with connect_ssai_db() as conn:
        _assert_manager_can_manage_company(
            conn,
            manager_user_id=int(manager_user_id),
            company_id=normalized_company_id,
            allow_all_companies=allow_all_companies,
        )
        target_user = _get_user_by_id_or_login(
            conn,
            target_login_id=normalized_login_id,
        )
        target_user_id = int(target_user["user_id"])
        _assert_user_belongs_to_company(
            conn,
            user_id=target_user_id,
            company_id=normalized_company_id,
        )

        active_roles = _fetch_all_dicts(
            conn,
            """
            SELECT
                r.role_code,
                r.role_name
            FROM dbo.SSAI_USER_ROLES ur
            JOIN dbo.SSAI_ROLES r
                ON r.role_id = ur.role_id
            WHERE ur.user_id = ?
              AND ur.is_active = 1
              AND r.is_active = 1
              AND (
                    ur.company_id IS NULL
                    OR ur.company_id = ?
                  )
            ORDER BY r.role_code
            """,
            (target_user_id, normalized_company_id),
        )
        effective_permission_codes = set(
            get_user_permissions(
                conn,
                user_id=target_user_id,
                company_id=normalized_company_id,
            )
        )

    return {
        "target_login_id": str(target_user.get("login_id") or normalized_login_id),
        "target_user_id": target_user_id,
        "company_id": normalized_company_id,
        "roles": [
            {
                "role_code": str(role.get("role_code") or ""),
                "role_name": str(role.get("role_name") or ""),
            }
            for role in active_roles
        ],
        "effective_permissions": [
            {
                "permission_code": permission_code,
                "allowed": permission_code in effective_permission_codes,
            }
            for permission_code in KNOWLEDGE_EFFECTIVE_PERMISSION_CODES
        ],
    }


def _grade_for_wholesale_role(role_code: str) -> str:
    role_code = str(role_code or "").strip()

    if role_code == "WHOLESALE_MANAGER":
        return "MANAGER"

    if role_code == "WHOLESALE_READONLY":
        return "READONLY"

    return "STAFF"

def _role_code_for_wholesale_grade(user_grade: str) -> str:
    """
    user_grade 기준 기본 도매 역할 코드.
    재사용 처리 시 기존 등급에 맞는 역할 1개만 다시 활성화하기 위해 사용한다.
    """
    user_grade = str(user_grade or "").strip().upper()

    if user_grade == "MANAGER":
        return "WHOLESALE_MANAGER"

    if user_grade == "READONLY":
        return "WHOLESALE_READONLY"

    return "WHOLESALE_STAFF"

def set_company_user_access_active(
    *,
    manager_user_id: int,
    target_login_id: str,
    company_id: int,
    is_active: bool,
    allow_all_companies: bool = False,
) -> dict[str, Any]:
    """
    회사 사용자 사용/중지 처리.

    정책:
    - 도매 관리자는 자기 회사 사용자만 처리 가능
    - 회사 연결(SSAI_USER_COMPANIES)을 활성/비활성 처리
    - 회사 역할(SSAI_USER_ROLES)도 같이 활성/비활성 처리
    - 비활성 후 남은 활성 회사가 없으면 SSAI_USERS.is_active = 0
    """
    target_login_id = str(target_login_id or "").strip()

    if not target_login_id:
        raise ValueError("target_login_id가 필요합니다.")

    with connect_ssai_db() as conn:
        _assert_manager_can_manage_company(
            conn,
            manager_user_id=int(manager_user_id),
            company_id=int(company_id),
            allow_all_companies=allow_all_companies,
        )

        target_user = _get_user_by_id_or_login(
            conn,
            target_login_id=target_login_id,
        )

        target_user_id = int(target_user["user_id"])

        if target_user_id == int(manager_user_id):
            raise PermissionError("자기 자신의 사용 상태는 이 화면에서 변경할 수 없습니다.")        

        _assert_user_belongs_to_company(
            conn,
            user_id=target_user_id,
            company_id=int(company_id),
        )

        cur = conn.cursor()

        cur.execute(
            """
            UPDATE dbo.SSAI_USER_COMPANIES
            SET is_active = ?
            WHERE user_id = ?
              AND company_id = ?
            """,
            1 if is_active else 0,
            target_user_id,
            int(company_id),
        )

        cur.execute(
            """
            UPDATE dbo.SSAI_USER_ROLES
            SET is_active = ?
            WHERE user_id = ?
              AND company_id = ?
            """,
            1 if is_active else 0,
            target_user_id,
            int(company_id),
        )

        if is_active:
            # 재사용 시 모든 과거 역할을 되살리면 안 된다.
            # 현재 user_grade에 맞는 역할 1개만 활성화한다.
            role_code_to_enable = _role_code_for_wholesale_grade(
                str(target_user.get("user_grade") or "")
            )
            role_id_to_enable = _get_role_id_by_code(conn, role_code_to_enable)

            cur.execute(
                """
                UPDATE dbo.SSAI_USER_ROLES
                SET is_active = 0
                WHERE user_id = ?
                  AND company_id = ?
                """,
                target_user_id,
                int(company_id),
            )

            _assign_user_role_in_conn(
                conn,
                user_id=target_user_id,
                role_id=role_id_to_enable,
                company_id=int(company_id),
            )

            cur.execute(
                """
                UPDATE dbo.SSAI_USERS
                SET
                    is_active = 1,
                    approval_status = N'APPROVED',
                    user_grade = ?,
                    updated_at = SYSDATETIME()
                WHERE user_id = ?
                """,
                _grade_for_wholesale_role(role_code_to_enable),
                target_user_id,
            )

        else:
            active_company_count_row = _fetch_one_dict(
                conn,
                """
                SELECT COUNT(*) AS active_company_count
                FROM dbo.SSAI_USER_COMPANIES
                WHERE user_id = ?
                  AND is_active = 1
                """,
                (target_user_id,),
            )

            active_company_count = int(active_company_count_row["active_company_count"] or 0)

            if active_company_count <= 0:
                cur.execute(
                    """
                    UPDATE dbo.SSAI_USERS
                    SET
                        is_active = 0,
                        updated_at = SYSDATETIME()
                    WHERE user_id = ?
                    """,
                    target_user_id,
                )
            else:
                cur.execute(
                    """
                    UPDATE dbo.SSAI_USERS
                    SET updated_at = SYSDATETIME()
                    WHERE user_id = ?
                    """,
                    target_user_id,
                )

        conn.commit()

    storage_result: dict[str, Any] | None = None
    if is_active:
        storage_result = _safe_ensure_user_storage_dirs(
            company_id=int(company_id),
            user_id=target_user_id,
        )

    state = get_user_company_roles(target_login_id)

    safe_log_audit_event(
        event_type="USER_ACCESS_ENABLE" if is_active else "USER_ACCESS_DISABLE",
        action_result="SUCCESS",
        actor_user_id=int(manager_user_id),
        company_id=int(company_id),
        target_user_id=target_user_id,
        target_login_id=target_login_id,
        target_company_id=int(company_id),
        message="회사 사용자 사용 상태 변경",
        details={
            "target_login_id": target_login_id,
            "company_id": int(company_id),
            "is_active": bool(is_active),
            "allow_all_companies": bool(allow_all_companies),
            "storage_result": storage_result,
        },
    )

    return {
        "ok": True,
        "action": "company_user_access_updated",
        "target_login_id": target_login_id,
        "company_id": int(company_id),
        "is_active": bool(is_active),
        "storage_result": storage_result,
        "state": state,
    }


def change_company_user_role(
    *,
    manager_user_id: int,
    target_login_id: str,
    company_id: int,
    role_code: str,
    allow_all_companies: bool = False,
) -> dict[str, Any]:
    """
    회사 사용자 역할 변경.

    정책:
    - 도매 관리자는 자기 회사 사용자만 처리 가능
    - 회사 단위 역할만 변경
    - 기존 회사 역할은 비활성화 후 새 역할 활성화
    """
    target_login_id = str(target_login_id or "").strip()
    role_code = str(role_code or "").strip()

    if not target_login_id:
        raise ValueError("target_login_id가 필요합니다.")

    allowed_roles = {
        "WHOLESALE_STAFF",
        "WHOLESALE_MANAGER",
        "WHOLESALE_READONLY",
    }

    if role_code not in allowed_roles:
        raise ValueError(f"허용되지 않은 역할입니다. role_code={role_code}")

    with connect_ssai_db() as conn:
        _assert_manager_can_manage_company(
            conn,
            manager_user_id=int(manager_user_id),
            company_id=int(company_id),
            allow_all_companies=allow_all_companies,
        )

        target_user = _get_user_by_id_or_login(
            conn,
            target_login_id=target_login_id,
        )

        target_user_id = int(target_user["user_id"])

        if target_user_id == int(manager_user_id):
            raise PermissionError("자기 자신의 역할은 이 화면에서 변경할 수 없습니다.")

        _assert_user_belongs_to_company(
            conn,
            user_id=target_user_id,
            company_id=int(company_id),
        )

        role_id = _get_role_id_by_code(conn, role_code)
        user_grade = _grade_for_wholesale_role(role_code)

        cur = conn.cursor()

        # 기존 회사 역할 비활성화
        cur.execute(
            """
            UPDATE dbo.SSAI_USER_ROLES
            SET is_active = 0
            WHERE user_id = ?
              AND company_id = ?
            """,
            target_user_id,
            int(company_id),
        )

        # 새 역할 활성화/생성
        _assign_user_role_in_conn(
            conn,
            user_id=target_user_id,
            role_id=role_id,
            company_id=int(company_id),
        )

        cur.execute(
            """
            UPDATE dbo.SSAI_USERS
            SET
                user_type = N'WHOLESALE_USER',
                user_grade = ?,
                approval_status = N'APPROVED',
                is_active = 1,
                updated_at = SYSDATETIME()
            WHERE user_id = ?
            """,
            user_grade,
            target_user_id,
        )

        conn.commit()

    state = get_user_company_roles(target_login_id)

    safe_log_audit_event(
        event_type="USER_ROLE_CHANGE",
        action_result="SUCCESS",
        actor_user_id=int(manager_user_id),
        company_id=int(company_id),
        target_user_id=target_user_id,
        target_login_id=target_login_id,
        target_company_id=int(company_id),
        message="회사 사용자 역할 변경",
        details={
            "target_login_id": target_login_id,
            "company_id": int(company_id),
            "role_code": role_code,
            "user_grade": user_grade,
            "allow_all_companies": bool(allow_all_companies),
        },
    )

    return {
        "ok": True,
        "action": "company_user_role_changed",
        "target_login_id": target_login_id,
        "company_id": int(company_id),
        "role_code": role_code,
        "user_grade": user_grade,
        "state": state,
    }

def update_user_policy(
    *,
    manager_user_id: int,
    target_login_id: str,
    company_id: int,
    user_type: str,
    role_code: str = "",
    user_grade: str = "",
    sims_user_id: str | None = None,
    actor_login_id: str | None = None,
    allow_all_companies: bool = False,
) -> dict[str, Any]:
    """
    기존 사용자 정책 변경.

    처리 범위:
    - 사용자 종류 변경
    - 사용자 등급 변경
    - SIMS 사용자 ID 변경
    - 기본 ERP DB 변경
    - 기존 회사 연결/역할 비활성화 후 새 정책으로 재연결
    - 감사 로그 USER_POLICY_UPDATE 기록
    """
    target_login_id = str(target_login_id or "").strip()
    user_type = _normalize_user_type(user_type)

    # user_type 기준으로 role_code를 강제 보정한다.
    # 예: SSART_USER + SSART_MANAGER 같은 조합 방지
    role_code = _normalize_role_code_for_user_type(
        user_type=user_type,
        role_code=role_code,
    )

    # user_grade는 화면 입력값보다 정책을 우선한다.
    # 예: SSART_USER는 항상 STAFF, SSART_ADMIN은 항상 MANAGER
    user_grade = _default_grade_for_user_type_and_role(
        user_type=user_type,
        role_code=role_code,
    )

    if not target_login_id:
        raise ValueError("target_login_id가 필요합니다.")

    if role_code == "SYSTEM_ADMIN":
        raise PermissionError("SYSTEM_ADMIN 역할은 이 화면에서 부여할 수 없습니다.")

    if not allow_all_companies and user_type in {"SSART_ADMIN", "SSART_USER"}:
        raise PermissionError("도매 관리자는 신성아트컴 사용자로 변경할 수 없습니다.")

    with connect_ssai_db() as conn:
        _assert_manager_can_manage_company(
            conn,
            manager_user_id=int(manager_user_id),
            company_id=int(company_id),
            allow_all_companies=allow_all_companies,
        )

        company = _get_active_company_by_id(
            conn,
            company_id=int(company_id),
        )

        target_user = _fetch_one_dict(
            conn,
            """
            SELECT TOP 1
                user_id,
                login_id,
                user_name,
                user_type,
                user_grade,
                default_company_id,
                sims_user_id,
                approval_status,
                is_active
            FROM dbo.SSAI_USERS
            WHERE login_id = ?
            """,
            (target_login_id,),
        )

        if not target_user:
            raise ValueError(f"대상 사용자를 찾지 못했습니다. target_login_id={target_login_id}")

        target_user_id = int(target_user["user_id"])

        if target_user_id == int(manager_user_id):
            raise PermissionError("자기 자신의 사용자 정책은 이 화면에서 변경할 수 없습니다.")

        manager_is_super_admin = _is_super_admin_user_in_conn(
            conn,
            int(manager_user_id),
        )

        manager_is_ssart_manager = _is_ssart_manager_user_in_conn(
            conn,
            int(manager_user_id),
        )

        target_current_user_type = str(target_user.get("user_type") or "").strip().upper()
        target_current_user_grade = str(target_user.get("user_grade") or "").strip().upper()

        # 최고관리자 계정은 보호한다.
        # 현재는 admin 1개만 SUPER로 두고, 이 화면에서는 SUPER 계정 변경/생성을 막는다.
        if target_current_user_type == "SSART_ADMIN" and target_current_user_grade == "SUPER":
            raise PermissionError(
                "신성아트컴 최고관리자 계정은 이 화면에서 정책 변경할 수 없습니다."
            )

        # 신성관리자 계정 정책 변경 또는 신성관리자 승격은 SUPER만 가능하다.
        if (
            target_current_user_type == "SSART_ADMIN"
            or user_type == "SSART_ADMIN"
        ) and not manager_is_super_admin:
            raise PermissionError(
                "신성아트컴 관리자 계정 정책 변경은 최고관리자만 처리할 수 있습니다."
            )

        # 신성사용자 정책 변경은 신성관리자 이상이면 가능하다.
        if (
            target_current_user_type == "SSART_USER"
            or user_type == "SSART_USER"
        ) and not manager_is_ssart_manager:
            raise PermissionError(
                "신성아트컴 사용자 정책 변경은 신성관리자 이상만 처리할 수 있습니다."
            )

        # 이 화면에서는 새로운 SUPER 계정을 만들지 않는다.
        if user_type == "SSART_ADMIN" and user_grade == "SUPER":
            raise PermissionError(
                "신성아트컴 최고관리자 계정 생성/변경은 별도 최고관리자 관리 기능에서 처리하세요."
            )
        
        before_state = get_user_company_roles(target_login_id)

        role_id = _get_role_id_by_code(conn, role_code)

        role_company_id = _role_company_id_for_user_type(
            user_type=user_type,
            role_code=role_code,
            company_id=int(company_id),
        )

        sims_user_id_to_save = (
            str(sims_user_id).strip()
            if sims_user_id is not None
            else str(target_user.get("sims_user_id") or "").strip()
        )

        cur = conn.cursor()

        # 기존 회사 연결은 모두 비활성화하고 새 기본 회사만 활성화한다.
        cur.execute(
            """
            UPDATE dbo.SSAI_USER_COMPANIES
            SET
                is_default = 0,
                is_active = 0
            WHERE user_id = ?
            """,
            target_user_id,
        )

        # 기존 역할은 모두 비활성화하고 새 역할만 활성화한다.
        cur.execute(
            """
            UPDATE dbo.SSAI_USER_ROLES
            SET is_active = 0
            WHERE user_id = ?
            """,
            target_user_id,
        )

        cur.execute(
            """
            UPDATE dbo.SSAI_USERS
            SET
                user_type = ?,
                user_grade = ?,
                default_company_id = ?,
                sims_user_id = ?,
                approval_status = N'APPROVED',
                is_active = 1,
                updated_at = SYSDATETIME()
            WHERE user_id = ?
            """,
            user_type,
            user_grade,
            int(company_id),
            sims_user_id_to_save,
            target_user_id,
        )

        _assign_user_company_in_conn(
            conn,
            user_id=target_user_id,
            company_id=int(company_id),
            is_default=True,
        )

        _assign_user_role_in_conn(
            conn,
            user_id=target_user_id,
            role_id=role_id,
            company_id=role_company_id,
        )

        conn.commit()

    storage_result = _safe_ensure_user_storage_dirs(
        company_id=int(company_id),
        user_id=target_user_id,
    )

    after_state = get_user_company_roles(target_login_id)

    safe_log_audit_event(
        event_type="USER_POLICY_UPDATE",
        action_result="SUCCESS",
        actor_user_id=int(manager_user_id),
        actor_login_id=actor_login_id,
        company_id=int(company_id),
        target_user_id=target_user_id,
        target_login_id=target_login_id,
        target_company_id=int(company_id),
        message="기존 사용자 정책 변경",
        details={
            "target_login_id": target_login_id,
            "company": company,
            "user_type": user_type,
            "user_grade": user_grade,
            "role_code": role_code,
            "role_company_id": role_company_id,
            "sims_user_id": sims_user_id_to_save,
            "allow_all_companies": bool(allow_all_companies),
            "storage_result": storage_result,
            "before": before_state,
            "after": after_state,
        },
    )

    return {
        "ok": True,
        "action": "user_policy_updated",
        "target_login_id": target_login_id,
        "company_id": int(company_id),
        "user_type": user_type,
        "user_grade": user_grade,
        "role_code": role_code,
        "role_company_id": role_company_id,
        "sims_user_id": sims_user_id_to_save,
        "storage_result": storage_result,
        "state": after_state,
    }




