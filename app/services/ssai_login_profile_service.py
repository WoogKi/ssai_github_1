# app/services/ssai_login_profile_service.py
#
# SS AI Phase 3
# 로그인 후 SIMS 사용자 프로필 조회 서비스
# - SSAI DB에는 실사용자명/직책/부서를 저장하지 않는다.
# - 연결된 SIMS DB Rddbc060/Rddbc010에서 조회한다.

from __future__ import annotations

from typing import Any
from dataclasses import dataclass, field
import logging
import re

from app.services.ssai_auth_service import connect_company_db


ERP_IDENTITY_KEY = "__ssai_erp_own_identity"
log = logging.getLogger(__name__)
IDENTITY_STATUSES = frozenset({"verified", "unavailable", "query_failed", "contract_invalid",
                               "missing", "ambiguous", "vendor_missing", "vendor_ambiguous", "invalid_number"})


@dataclass(frozen=True)
class ErpOwnIdentity:
    company_id: int
    user_id: str
    status: str
    applicant: str = field(default="", repr=False)


def own_identity(company_id, user_id, row):
    """Accept one ERP self-vendor only; never select a representative duplicate."""
    count = row.get("own_company_rows")
    if type(count) is not int or count < 0:
        return ErpOwnIdentity(company_id, str(user_id), "contract_invalid")
    if count != 1:
        return ErpOwnIdentity(company_id, str(user_id), "missing" if count == 0 else "ambiguous")
    vendors = row.get("own_vendor_rows")
    if type(vendors) is not int or vendors < 0:
        return ErpOwnIdentity(company_id, str(user_id), "contract_invalid")
    if vendors != 1:
        return ErpOwnIdentity(company_id, str(user_id), "vendor_missing" if vendors == 0 else "vendor_ambiguous")
    raw = row.get("own_company_number")
    value = raw.strip() if isinstance(raw, str) else ""
    if not re.fullmatch(r"[0-9]{10}|[0-9]{3}-[0-9]{2}-[0-9]{5}", value):
        return ErpOwnIdentity(company_id, str(user_id), "invalid_number")
    value = value.replace("-", "")
    if value == "0" * 10:
        return ErpOwnIdentity(company_id, str(user_id), "invalid_number")
    return ErpOwnIdentity(company_id, str(user_id), "verified", value)


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
    - ERP 사용자 미연결/미존재여도 자사정보는 같은 SELECT에서 별도로 반환
    """
    sims_user_id = _clean(sims_user_id)

    sql = """
    SELECT TOP 1
        CASE WHEN U.Rd06_User_Cd IS NULL THEN 0 ELSE 1 END AS sims_profile_found,
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

        RTRIM(LTRIM(U.Rd06_Del_Flag)) AS sims_del_flag,
        OwnSetting.own_company_rows,
        OwnCompany.own_vendor_rows,
        OwnCompany.own_company_number
    FROM (SELECT 1 AS login_scope) LoginScope
    OUTER APPLY (
        SELECT TOP 1
            Rd06_User_Cd, Rd06_User_ID, Rd06_User_Nm,
            Rd06_Department_Gcode, Rd06_Department, Rd06_Duty_Gcode, Rd06_Duty,
            Rd06_District_Gcode, Rd06_District, Rd06_Stock_Cd_Gcode, Rd06_Stock_Cd,
            Rd06_Del_Flag
        FROM dbo.Rddbc060
        WHERE ? <> '' AND (RTRIM(LTRIM(Rd06_User_ID)) = ?
                            OR RTRIM(LTRIM(Rd06_User_Cd)) = ?)
    ) U
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
    OUTER APPLY (
        SELECT COUNT_BIG(*) AS own_company_rows,
               MIN(C.Rd0A_Value) AS own_vendor_code
        FROM dbo.Rddbc0A0 C
        WHERE C.Rd0A_Gcode = '0003' AND C.Rd0A_Tcode = '000001'
    ) OwnSetting
    OUTER APPLY (
        SELECT COUNT_BIG(*) AS own_vendor_rows,
               MIN(LTRIM(RTRIM(V.Rd03_Ven_Num))) AS own_company_number
        FROM dbo.Rddbc030 V
        WHERE OwnSetting.own_company_rows = 1
          AND V.Rd03_Ven_Cd = OwnSetting.own_vendor_code
    ) OwnCompany
    """

    with connect_company_db(int(company_id)) as conn:
        cur = conn.cursor()
        row = cur.execute(sql, sims_user_id, sims_user_id, sims_user_id).fetchone()

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
        ERP_IDENTITY_KEY: ErpOwnIdentity(company_id, str(getattr(user, "user_id", "")), "unavailable"),
    }

    if not company_id:
        log.warning("[kpis.identity] stage=profile reason=company_missing")
        return profile

    try:
        sims_profile = get_sims_login_profile(
            company_id=company_id,
            sims_user_id=sims_user_id,
        )
    except Exception as e:
        profile["profile_error"] = f"{type(e).__name__}: ERP_PROFILE_UNAVAILABLE"
        profile[ERP_IDENTITY_KEY] = ErpOwnIdentity(company_id, str(getattr(user, "user_id", "")), "query_failed")
        log.warning("[kpis.identity] stage=profile_query reason=query_failed error_type=%s", type(e).__name__)
        return profile

    if not sims_profile:
        profile["profile_error"] = "SIMS_USER_PROFILE_NOT_FOUND"
        log.warning("[kpis.identity] stage=profile_query reason=result_missing")
        return profile

    identity = own_identity(company_id, getattr(user, "user_id", ""), sims_profile)
    profile[ERP_IDENTITY_KEY] = identity
    log.info("[kpis.identity] stage=identity_build reason=%s", identity.status)
    # Company identity must not depend on the optional ERP employee profile.
    if sims_profile.get("sims_profile_found") != 1:
        profile["profile_error"] = "SIMS_USER_PROFILE_NOT_FOUND"
        log.info("[kpis.identity] stage=profile reason=%s",
                 "sims_user_missing" if sims_user_id else "sims_user_unbound")
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
