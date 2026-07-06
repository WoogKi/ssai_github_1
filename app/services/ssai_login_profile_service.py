# app/services/ssai_login_profile_service.py
#
# SS AI Phase 3
# 로그인 후 SIMS 사용자 프로필 조회 서비스
# - SSAI DB에는 실사용자명/직책/부서를 저장하지 않는다.
# - 연결된 SIMS DB Rddbc060/Rddbc010에서 조회한다.

from __future__ import annotations

from typing import Any

from app.services.ssai_auth_service import connect_company_db


def _clean(value: Any) -> str:
    return str(value or "").strip()


def get_sims_login_profile(
    *,
    company_id: int,
    sims_user_id: str,
) -> dict[str, Any] | None:
    """
    연결된 SIMS DB에서 로그인 사용자 업무 프로필을 조회한다.

    기준:
    - Rddbc060.Rd06_User_ID 또는 Rd06_User_Cd
    - 직책/부서/지역/재고위치는 Rddbc010 코드명 조인
    """
    sims_user_id = _clean(sims_user_id)
    if not sims_user_id:
        return None

    sql = """
    SELECT TOP 1
        RTRIM(LTRIM(U.Rd06_User_Cd)) AS sims_user_cd,
        RTRIM(LTRIM(U.Rd06_User_ID)) AS sims_user_id,
        RTRIM(LTRIM(U.Rd06_User_Nm)) AS sims_user_name,

        RTRIM(LTRIM(U.Rd06_Department_Gcode)) AS department_gcode,
        RTRIM(LTRIM(U.Rd06_Department)) AS department_code,
        RTRIM(LTRIM(ISNULL(Dept.Rd01_Hnm, U.Rd06_Department))) AS department_name,

        RTRIM(LTRIM(U.Rd06_Duty_Gcode)) AS duty_gcode,
        RTRIM(LTRIM(U.Rd06_Duty)) AS duty_code,
        RTRIM(LTRIM(ISNULL(Duty.Rd01_Hnm, U.Rd06_Duty))) AS duty_name,

        RTRIM(LTRIM(U.Rd06_District_Gcode)) AS district_gcode,
        RTRIM(LTRIM(U.Rd06_District)) AS district_code,
        RTRIM(LTRIM(ISNULL(District.Rd01_Hnm, U.Rd06_District))) AS district_name,

        RTRIM(LTRIM(U.Rd06_Stock_Cd_Gcode)) AS stock_gcode,
        RTRIM(LTRIM(U.Rd06_Stock_Cd)) AS stock_code,
        RTRIM(LTRIM(ISNULL(Stock.Rd01_Hnm, U.Rd06_Stock_Cd))) AS stock_name,

        RTRIM(LTRIM(U.Rd06_Del_Flag)) AS sims_del_flag
    FROM dbo.Rddbc060 U
    LEFT JOIN dbo.Rddbc010 Dept
        ON Dept.Rd01_Gcode = U.Rd06_Department_Gcode
       AND Dept.Rd01_Tcode = U.Rd06_Department
    LEFT JOIN dbo.Rddbc010 Duty
        ON Duty.Rd01_Gcode = U.Rd06_Duty_Gcode
       AND Duty.Rd01_Tcode = U.Rd06_Duty
    LEFT JOIN dbo.Rddbc010 District
        ON District.Rd01_Gcode = U.Rd06_District_Gcode
       AND District.Rd01_Tcode = U.Rd06_District
    LEFT JOIN dbo.Rddbc010 Stock
        ON Stock.Rd01_Gcode = U.Rd06_Stock_Cd_Gcode
       AND Stock.Rd01_Tcode = U.Rd06_Stock_Cd
    WHERE RTRIM(LTRIM(U.Rd06_User_ID)) = ?
       OR RTRIM(LTRIM(U.Rd06_User_Cd)) = ?
    """

    with connect_company_db(int(company_id)) as conn:
        cur = conn.cursor()
        row = cur.execute(sql, sims_user_id, sims_user_id).fetchone()

        if not row:
            return None

        columns = [col[0] for col in cur.description]
        return dict(zip(columns, row))


def build_login_profile(
    *,
    user: Any,
    company: dict[str, Any],
) -> dict[str, Any]:
    """
    로그인 화면/메인 화면에서 사용할 업무 프로필.

    SSAI_USERS의 nickname은 화면 별칭이고,
    실제 업무 사용자명/직책/부서는 SIMS DB에서 가져온다.
    """
    company_id = int(company.get("company_id") or 0)
    company_name = _clean(company.get("company_name"))
    company_code = _clean(company.get("company_code"))
    db_name = _clean(company.get("db_name"))

    sims_user_id = _clean(getattr(user, "sims_user_id", "") or "")

    profile: dict[str, Any] = {
        "company_id": company_id,
        "company_code": company_code,
        "company_name": company_name,
        "db_name": db_name,

        "user_id": getattr(user, "user_id", None),
        "login_id": _clean(getattr(user, "login_id", "")),
        "nickname": _clean(getattr(user, "nickname", "")),
        "user_type": _clean(getattr(user, "user_type", "")),
        "user_grade": _clean(getattr(user, "user_grade", "")),
        "sims_user_id": sims_user_id,

        "sims_user_cd": "",
        "sims_user_name": "",
        "department_name": "",
        "duty_name": "",
        "district_name": "",
        "stock_name": "",
        "profile_source": "SSAI_FALLBACK",
    }

    if not company_id or not sims_user_id:
        return profile

    try:
        sims_profile = get_sims_login_profile(
            company_id=company_id,
            sims_user_id=sims_user_id,
        )
    except Exception as e:
        profile["profile_error"] = f"{type(e).__name__}: {e}"
        return profile

    if not sims_profile:
        profile["profile_error"] = "SIMS_USER_PROFILE_NOT_FOUND"
        return profile

    profile.update(
        {
            "sims_user_cd": _clean(sims_profile.get("sims_user_cd")),
            "sims_user_id": _clean(sims_profile.get("sims_user_id")) or sims_user_id,
            "sims_user_name": _clean(sims_profile.get("sims_user_name")),
            "department_name": _clean(sims_profile.get("department_name")),
            "duty_name": _clean(sims_profile.get("duty_name")),
            "district_name": _clean(sims_profile.get("district_name")),
            "stock_name": _clean(sims_profile.get("stock_name")),
            "sims_del_flag": _clean(sims_profile.get("sims_del_flag")),
            "profile_source": "SIMS_Rddbc060",
        }
    )

    return profile