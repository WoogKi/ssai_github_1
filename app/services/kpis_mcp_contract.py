"""KPIS catalog and offline authorization preflight; no transport or credentials.

Metadata follows the repository's v2.1 plan, not a live API certification.
The session provider must be owned by SIMS, never supplied by a model/client.
Even a successful preflight is NOT permission to execute an external call.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Callable

from app.services.mcp_adapter import (
    MCP_DEFAULT_TIMEOUT_S, MCP_MAX_TIMEOUT_S, MCP_EXTERNAL_RESOURCE_READ,
)
from app.services.ssai_permission_policy import get_required_permission


@dataclass(frozen=True)
class Service:
    code: str
    name: str
    auth: str | None
    path: str | None
    response_kind: str = "json"
    read_only: bool = True
    method: str | None = "POST"


_SERVICES = (
    Service("MA117", "의약품센터 공지사항 조회", "API_KEY", "/v1/services/getMsupNtfcItemInq.do"),
    Service("MA101", "표준코드마스터 정보 조회", "API_KEY", "/v1/services/getMsupStdCdInfo.do"),
    Service("MA102", "거래처 정보 조회", "API_KEY", "/v1/services/getMsupBscqInfo.do"),
    Service("MA116", "분기별 적기보고율 조회", "JWT", "/v1/services/getTimelyBriefRateListQtInfo.do"),
    Service("MA110", "공급내역 집계 조회", "API_KEY", "/v1/services/getMsupBriefSummary01Info.do"),
    Service("MA103", "RFID Tag 정보 조회", "API_KEY", "/v1/services/getMsupRfidInfo.do"),
    Service("MA104", "묶음번호_최소박스 조회", "API_KEY", "/v1/services/getMsupBnoInfo.do"),
    Service("MA105", "묶음번호_일련번호 조회", "API_KEY", "/v1/services/getMsupSnoInfo.do"),
    Service("MA106", "묶음번호_일련번호상세조회", "API_KEY", "/v1/services/getMsupSnoDtlInfo.do"),
    Service("MA107", "RFID Tag 다건 정보 조회", "API_KEY", "/v1/services/getMsupRfidMultiInfo.do"),
    Service("MA109", "EB 반송 마스터 조회", "API_KEY", "/v1/services/getRiskMsupInfo.do"),
    Service("MA112", "공급내역 보고 결과 정보 조회", "JWT", "/v1/services/getSplyDescBriefRstInfo.do"),
    Service("MA114", "공급내역 반송요청 결과 정보 조회", "JWT", "/v1/services/getSplyRetnReqRstInfo.do"),
    Service("MA115", "입고내역 정보 조회(갑지)", "JWT", "/v1/services/getWrhngInfo.do"),
    Service("MA119", "입고내역 정보 조회(을지)", "JWT", "/v1/services/getWrhngDtfrInfo.do", "paged_json"),
    Service("MA118", "표준코드 마스터 조회(전체)", "API_KEY", "/v1/services/getDownloadMsupStdCdInfoStreamJson.do", "gzip"),
    # Save endpoints are intentionally absent from routable metadata.
    Service("MA111", "공급내역 보고 정보 저장", None, None, read_only=False, method=None),
    Service("MA113", "반송신청 정보 저장", None, None, read_only=False, method=None),
)
SERVICES = MappingProxyType({s.code: s for s in _SERVICES})
READ_SERVICES = tuple(s.code for s in _SERVICES if s.read_only)
FIRST_PRIORITY = READ_SERVICES[:5]
MODES = frozenset({"MOCK", "TEST", "PRODUCTION"})


@dataclass(frozen=True)
class Request:
    service: str
    mode: str
    company_id: int
    user_id: str
    session_epoch: int
    timeout_s: int = MCP_DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class Session:
    company_id: int
    user_id: str
    epoch: int
    permissions: frozenset[str]
    service_modes: frozenset[tuple[str, str]]
    test_authorized: bool
    expires_at: datetime


@dataclass(frozen=True)
class CompanyApproval:
    company_id: int
    service: str
    mode: str
    enabled: bool
    expires_at: datetime | None
    evidence_id: str
    authorization_basis: str = "EXPIRY"


SSAI_ACTIVE_COMPANY = "SSAI_COMPANIES.is_active"


def _future(value: object, now: datetime) -> bool:
    return (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None and value > now)


def preflight(request: Request, *, load_session: Callable[[], Session | None],
              approval: CompanyApproval | None, feature_enabled: bool = False,
              explicit_request: bool = False, now: datetime | None = None) -> dict:
    return _preflight(request, load_session=load_session, approval=approval,
                      feature_enabled=feature_enabled, explicit_request=explicit_request, now=now)


def erp_direct_preflight(request: Request, *, load_session: Callable[[], Session | None],
                         approval: CompanyApproval | None, feature_enabled: bool = False,
                         explicit_request: bool = False, now: datetime | None = None) -> dict:
    """Server-selected ERP path only; never selected by an LLM/request parameter."""
    result = _preflight(request, load_session=load_session, approval=approval,
                        feature_enabled=feature_enabled, explicit_request=explicit_request,
                        now=now, erp_direct=True)
    if result["status"] == "PREFLIGHT_ONLY" and (
            approval.authorization_basis != SSAI_ACTIVE_COMPANY):
        return dict(result, status="DENIED", reason_code="company_approval_required")
    return result


def _preflight(request: Request, *, load_session, approval, feature_enabled=False,
               explicit_request=False, now=None, erp_direct=False) -> dict:
    """Check a server-owned snapshot; do not issue capabilities, grant roles or call APIs.

    CompanyApproval must come from a trusted approval store. Persistence and live
    session revocation wiring remain deployment gates, not implemented here.
    """
    result = dict(status="DENIED", reason_code="invalid_request", service="", mode="",
                  runtime_ready=False, physical_call_count=0, retry_count=0)
    if (not isinstance(request, Request) or type(request.service) is not str
            or type(request.mode) is not str):
        return result
    if erp_direct and (request.mode != "PRODUCTION" or request.service not in {"MA117", "MA101", "MA102", "MA110", "MA109"}):
        return dict(result, reason_code="erp_direct_service_not_supported")
    spec = SERVICES.get(request.service)
    if spec is not None:
        result["service"] = spec.code
    if request.mode in MODES:
        result["mode"] = request.mode
    def denied(reason):
        return dict(result, reason_code=reason)
    if feature_enabled is not True:
        return denied("feature_disabled")
    if explicit_request is not True:
        return denied("explicit_request_required")
    if spec is None or not spec.read_only:
        return denied("unsupported_service")
    if (request.mode not in MODES or type(request.company_id) is not int or request.company_id <= 0
            or type(request.user_id) is not str or not 1 <= len(request.user_id) <= 128
            or type(request.session_epoch) is not int or request.session_epoch <= 0
            or type(request.timeout_s) is not int or not 1 <= request.timeout_s <= MCP_MAX_TIMEOUT_S):
        return result
    now = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return denied("invalid_clock")
    try:
        session = load_session()
    except Exception:
        return denied("session_unavailable")
    if (not isinstance(session, Session) or type(session.company_id) is not int
            or type(session.epoch) is not int
            or (session.company_id, session.user_id, session.epoch)
            != (request.company_id, request.user_id, request.session_epoch)
            or not _future(session.expires_at, now)):
        return denied("stale_or_mismatched_session")
    required = get_required_permission(special="mcp_external_resource")
    if (not isinstance(session.permissions, frozenset) or
            (not erp_direct and (required != MCP_EXTERNAL_RESOURCE_READ or required not in session.permissions))):
        return denied("permission_denied")
    if (not isinstance(session.service_modes, frozenset)
            or (request.service, request.mode) not in session.service_modes):
        return denied("service_mode_denied")
    if request.mode in {"MOCK", "TEST"} and session.test_authorized is not True:
        return denied("test_permission_required")
    active_company = (isinstance(approval, CompanyApproval)
                      and approval.authorization_basis == SSAI_ACTIVE_COMPANY
                      and approval.evidence_id == SSAI_ACTIVE_COMPANY
                      and approval.expires_at is None
                      and request.mode == "PRODUCTION"
                      and request.service in {"MA117", "MA101", "MA102", "MA110", "MA109"})
    dated_approval = (isinstance(approval, CompanyApproval)
                      and approval.authorization_basis == "EXPIRY"
                      and _future(approval.expires_at, now))
    if (not isinstance(approval, CompanyApproval) or type(approval.company_id) is not int
            or (approval.company_id, approval.service, approval.mode)
            != (request.company_id, request.service, request.mode)
            or approval.enabled is not True or not (active_company or dated_approval)
            or type(approval.evidence_id) is not str or not approval.evidence_id.strip()):
        return denied("company_approval_required")
    # This is deliberately not an executable grant. No real transport is wired.
    return dict(result, status="PREFLIGHT_ONLY", reason_code="transport_and_credentials_not_bound")
