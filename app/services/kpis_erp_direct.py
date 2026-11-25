"""ERP-only credential reference, using the existing REST and session boundaries.

Business identity comes from the existing login SELECT, not environment policy.
No local secret store, ERP credential cache, or JWT fallback is used here.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from app.services.external_api_management import (
    ApiManagementError, ERP_KEY_CODES, load_current_api_session, load_erp_api_key,
)
from app.services.ssai_login_profile_service import ERP_IDENTITY_KEY, ErpOwnIdentity, IDENTITY_STATUSES
from app.services.kpis_mcp_contract import SSAI_ACTIVE_COMPANY, erp_direct_preflight
from app.services.kpis_rest_client import KpisClient

DIRECT_SERVICES = ("MA117", "MA101", "MA102", "MA110", "MA109")
log = logging.getLogger(__name__)


def load_direct_approval(company, service):
    """Read login-owned identity only; fresh central authorization is done by the client."""
    from app.ui import ssai_login as login
    reason = "company_service_denied"
    try:
        user, selected = login.get_current_user(), login.get_selected_company()
        state = login.st.session_state
        if (type(company) is not int or company <= 0 or service not in DIRECT_SERVICES
                or user is None or not isinstance(selected, dict)
                or selected.get("company_id") != company or state.get(login.SESSION_COMPANY_PICK_MODE)):
            raise ApiManagementError("erp_direct_company_service_denied")
        if selected.get("is_active") not in (True, 1):
            reason = "company_inactive"
            raise ApiManagementError("erp_direct_company_inactive")
        identity = state.get(ERP_IDENTITY_KEY)
        reason = "session_identity_missing"
        if (not isinstance(identity, ErpOwnIdentity) or identity.company_id != company
                or identity.user_id != str(user.user_id)):
            if isinstance(identity, ErpOwnIdentity):
                reason = "session_binding_mismatch"
            raise ApiManagementError("erp_identity_unavailable")
        if identity.status != "verified":
            reason = identity.status if identity.status in IDENTITY_STATUSES else "identity_status_invalid"
            raise ApiManagementError("erp_identity_not_verified")
        if (len(identity.applicant) != 10 or not identity.applicant.isascii()
                or not identity.applicant.isdigit() or identity.applicant == "0" * 10):
            reason = "invalid_number"
            raise ApiManagementError("erp_identity_not_verified")
        # is_active has no company expiry date in the existing central contract.
        return dict(company_id=company, user_id=identity.user_id, applicant=identity.applicant, enabled=True,
                    expires_at=None, evidence_id=SSAI_ACTIVE_COMPANY, authorization_basis=SSAI_ACTIVE_COMPANY)
    except ApiManagementError:
        log.warning("[kpis.identity] stage=session_consume reason=%s", reason)
        raise
    except Exception as exc:
        log.warning("[kpis.identity] stage=session_consume reason=validation_error error_type=%s", type(exc).__name__)
        raise ApiManagementError("erp_identity_unavailable") from None


def append_safe_audit(event):
    # Reuse the configured application log directory, not an API/Secret database.
    from app.utils.env_config import config_path_any, read_project_env_file
    try:
        app_log = config_path_any(("LOG_FILE", "SIMS_LOG_FILE"), environ=read_project_env_file())
        path = app_log.with_name(app_log.name + ".kpis.jsonl")
        if path.is_symlink() or not path.parent.is_dir():
            raise ValueError()
        line = json.dumps(event, ensure_ascii=True, allow_nan=False) + "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
    except Exception:
        raise ApiManagementError("audit_unavailable") from None


class ErpDirectAccess:
    """Profile/audit interface for KpisClient, without pretending to be a DB."""
    preflight = staticmethod(erp_direct_preflight)

    def __init__(self, approval_loader=None, audit=append_safe_audit):
        self.approval_loader, self.audit = approval_loader or load_direct_approval, audit
        self.request_id = uuid4().hex

    def execution_profile(self, actor, request):
        if request.service not in DIRECT_SERVICES or request.mode != "PRODUCTION":
            raise ApiManagementError("erp_direct_service_not_supported")
        gate = self.preflight(request, load_session=lambda: actor, approval=None,
                              feature_enabled=True, explicit_request=True)
        if gate["reason_code"] not in {"service_mode_denied", "company_approval_required"}:
            raise ApiManagementError(gate["reason_code"])
        entry = self.approval_loader(request.company_id, request.service)
        if entry["user_id"] != actor.user_id:
            raise ApiManagementError("stale_session")
        return dict(scope=[request.company_id, request.service, request.mode],
                    applicant=entry["applicant"], enabled=entry["enabled"],
                    expires=entry["expires_at"], evidence=entry["evidence_id"],
                    authorization_basis=entry["authorization_basis"],
                    credentials={"source": "ERP_RDDBC0A4", "tcode": ERP_KEY_CODES[request.service]})

    def record_event(self, company, service, mode, actor, action, result="OK", calls=0):
        self.audit(dict(at=datetime.now(timezone.utc).isoformat(), request_id=self.request_id,
                        company_id=company, service=service, mode=mode, user_id=actor.user_id,
                        action=action, result=result, calls=calls))


def configured_direct_client():
    # 일반 ERP 조회는 기존 로그인/회사/자사정보로 인가한다. 관리자용 클라이언트 정책은 별개다.
    return KpisClient(ErpDirectAccess(), load_current_api_session,
                      enabled=True, erp_key_loader=load_erp_api_key)
