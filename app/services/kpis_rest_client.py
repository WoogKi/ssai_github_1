"""Bounded KPIS REST transport, owned by SIMS, not by the LLM.

Wire contracts: local integrated guide (2026-03-30), pages 5/14/16/22/47-49.
No network on import, automatic token refresh, HTTP retry or Native MCP fallback.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import replace
from datetime import datetime, timezone
from types import MappingProxyType

import httpx

from app.services.external_api_management import ApiManagementError, ERP_KEY_CODES
from app.services.kpis_mcp_contract import CompanyApproval, SERVICES, preflight

log = logging.getLogger("ssai.kpis")
ROW_LIMITS = MappingProxyType({"MA109": 5000})


def response_row_limit(service):
    return ROW_LIMITS.get(service, 1000)

BASES = MappingProxyType({"TEST": "http://devopenapi.kpis.or.kr", "PRODUCTION": "https://newopenapi.kpis.or.kr"})
INPUTS = {
    "MA117": {"srchStaDd": 8, "srchEndDd": 8},
    "MA101": {"msupStdCd": 13},
    "MA102": {"msupSplyFrmCd": 2, "tkrHbin": 10, "tkrpNm": 400, "ykiho": 8},
    "MA116": {"rcvYr": 4, "rcvQt": 2, "owcpOtcTpCd": 2},
    "MA110": {"splyTpCd": 1, "splyDd": 8, "tkrHbin": 10, "ykiho": 8},
    "MA109": {"rpayCmmYr": 4, "rpayCmmStaDd": 8, "rpayCmmEndDd": 8},
}
OUTPUTS = {
    "MA117": ("msupNtfcItemList", {"brdOrd", "brdTtl", "brdDtlTxt", "wrtpNm", "wrtYmd", "fileYn"}),
    "MA101": ("msupStdCdList", {"stdCd", "korMgdsNm", "adptFrDt", "adptToDt", "tranTkovrAdptFrDt", "tranTkovrAdptToDt", "packMgdsTotQty", "upCost", "payType", "sftMsup", "spcGnlCd",
                               "wlprLprcRadt", "itemStdCd", "cnclDt", "snoOmssYnCd", "snoOmssRsCd", "msupDctOftTpCd", "crtrPrdCd3"}),
    "MA102": ("msupBscqList", {"bscqRegYn", "bscqNm", "cloDd"}),
    "MA110": ("msupBriefSummary01List", {"msupSplyQty", "msupSplyAmt", "rcvCnt"}),
    "MA109": ("riskMsupList", {"msupStdCd", "msupMnfNo", "mfdsItmCrtrCd", "rpayCmmDt", "regDt", "mnfDt",
                              "rpayRspnpBizrno", "rpayRspnpFirmNm", "riskGrdDivCdNm", "rpayCmmEndDd"}),
    "MA116": ("timelyBriefRateListQtInfoVoList", {"hiraRcvQt", "hiraRcvYm", "owcpOtcTpCd", "termWnBriefCnt", "briefMissMsupSplyQty", "msupSplyQty", "timelyBriefRate"}),
}


def validate_query(service, query):
    if service not in INPUTS or not isinstance(query, dict) or set(query) - INPUTS[service].keys():
        raise ApiManagementError("invalid_query")
    query = dict(query)
    if service in {"MA102", "MA110"} and query.get("tkrHbin"):
        # Same accepted forms as login own_identity; do not strip arbitrary characters.
        number = query["tkrHbin"]
        if not isinstance(number, str) or not re.fullmatch(r"[0-9]{10}|[0-9]{3}-[0-9]{2}-[0-9]{5}", number):
            raise ApiManagementError("invalid_counterparty_number")
        query["tkrHbin"] = number.replace("-", "")
    if service == "MA102":
        # Integrated guide p15: recipient registration number is required.
        number = query.get("tkrHbin")
        if number in (None, ""):
            raise ApiManagementError("ma102_counterparty_required")
        if not isinstance(number, str) or len(number) != 10 or not number.isascii() or not number.isdigit():
            raise ApiManagementError("invalid_counterparty_number")
        institution = query.get("ykiho")
        if institution and (not isinstance(institution, str) or len(institution) != 8
                            or not institution.isascii() or not institution.isdigit()):
            raise ApiManagementError("invalid_institution_code")
    for name, value in query.items():
        if service == "MA110" and name == "splyTpCd" and value is None:
            continue
        if not isinstance(value, str) or len(value) > INPUTS[service][name] or any(ord(c) < 32 for c in value):
            raise ApiManagementError("invalid_query")
        if value and name != "tkrpNm" and (not value.isascii() or not value.isdigit()):
            raise ApiManagementError("invalid_query")
    for key in ("srchStaDd", "srchEndDd", "splyDd", "rpayCmmStaDd", "rpayCmmEndDd"):
        if key in query:
            try:
                if len(query[key]) != 8:
                    raise ValueError()
                datetime.strptime(query[key], "%Y%m%d")
            except ValueError:
                raise ApiManagementError("invalid_date") from None
    required = {"MA117": {"srchStaDd", "srchEndDd"}, "MA101": {"msupStdCd"},
                "MA102": {"msupSplyFrmCd"}, "MA110": {"splyDd"}, "MA116": {"rcvYr", "rcvQt"}, "MA109": set()}[service]
    if any(not query.get(k) for k in required):
        raise ApiManagementError("missing_query")
    if service == "MA117" and query["srchStaDd"] > query["srchEndDd"]:
        raise ApiManagementError("invalid_date_range")
    if service == "MA101" and len(query["msupStdCd"]) != 13:
        raise ApiManagementError("invalid_standard_code")
    if service == "MA102" and query["msupSplyFrmCd"] not in {"5", "6", "8", "9"}:
        raise ApiManagementError("invalid_supply_form")
    if service == "MA110" and query.get("splyTpCd") not in {None, "1", "2", "3", "4"}:
        raise ApiManagementError("invalid_supply_type")
    if service == "MA109":
        year, start, end = (query.get(k) for k in ("rpayCmmYr", "rpayCmmStaDd", "rpayCmmEndDd"))
        if year and (len(year) != 4 or int(year) == 0):
            raise ApiManagementError("invalid_recall_year")
        if not year and not (start and end):
            raise ApiManagementError("missing_query")
        if bool(start) != bool(end):
            raise ApiManagementError("missing_query")
        if start and start > end:
            raise ApiManagementError("invalid_date_range")
    if service == "MA116" and (len(query["rcvYr"]) != 4 or query["rcvQt"] not in {"1", "2", "3", "4"}
                                or query.get("owcpOtcTpCd", "") not in {"", "01", "02"}):
        raise ApiManagementError("invalid_quarter")
    checked = dict(query)
    if service == "MA110":
        checked.setdefault("splyTpCd", None)
    return checked


class KpisClient:
    def __init__(self, store, load_session, *, enabled=False, test_http_approved=False, transport=None,
                 erp_key_loader=None):
        self.store, self.load_session = store, load_session
        self.enabled, self.test_http_approved = enabled, test_http_approved
        self.transport = transport
        self.erp_key_loader = erp_key_loader
        # Policy belongs to the server-owned access adapter, never to query input.
        self.preflight = getattr(store, "preflight", preflight)

    def call(self, request, query, *, explicit=False):
        started, calls, credential_reads, actor = time.monotonic(), 0, 0, None
        stage, http_status, received_bytes, error_type = "preflight", None, 0, "none"
        result = dict(status="DENIED", reason_code="not_authorized", rows=[], physical_call_count=0,
                      retry_count=0, elapsed_ms=0)
        try:
            # Check all request types/permissions before accessing a credential store.
            gate = self.preflight(request, load_session=self.load_session, approval=None,
                             feature_enabled=self.enabled, explicit_request=explicit)
            if gate["reason_code"] not in {"service_mode_denied", "company_approval_required"}:
                raise ApiManagementError(gate["reason_code"])
            if request.service not in INPUTS or request.mode not in BASES:
                raise ApiManagementError("unsupported_service")
            stage = "query_validation"
            payload = validate_query(request.service, query)
            stage = "profile"
            actor = self.load_session()
            profile = self.store.execution_profile(actor, request)
            actor = replace(actor, service_modes=frozenset({(request.service, request.mode)}))
            approval = CompanyApproval(request.company_id, request.service, request.mode,
                                       profile["enabled"], datetime.fromisoformat(profile["expires"]) if profile["expires"] is not None else None,
                                       profile["evidence"], profile.get("authorization_basis", "EXPIRY"))
            gate = self.preflight(request, load_session=lambda: actor, approval=approval,
                             feature_enabled=self.enabled, explicit_request=explicit)
            if gate["status"] != "PREFLIGHT_ONLY":
                raise ApiManagementError(gate["reason_code"])
            if request.mode == "TEST" and self.test_http_approved is not True:
                raise ApiManagementError("test_plain_http_not_approved")

            def fresh():
                current = self.load_session()
                latest = self.store.execution_profile(current, request)
                current = replace(current, service_modes=actor.service_modes)
                decision = self.preflight(request, load_session=lambda: current, approval=approval,
                                     feature_enabled=self.enabled, explicit_request=explicit)
                if decision["status"] != "PREFLIGHT_ONLY" or latest != profile:
                    raise ApiManagementError("authorization_changed")

            credentials = profile["credentials"]
            names = {"client_id", "client_secret"} if SERVICES[request.service].auth == "JWT" else {"apiKey"}
            if credentials.get("source") == "ERP_RDDBC0A4":
                if (request.mode != "PRODUCTION" or self.erp_key_loader is None
                        or credentials != {"source": "ERP_RDDBC0A4", "tcode": ERP_KEY_CODES.get(request.service)}):
                    raise ApiManagementError("erp_key_read_not_approved")
                # All authorization and explicit-live gates above precede ERP access.
                self.store.record_event(request.company_id, request.service, request.mode, actor, "ERP_KEY_READ")
                fresh()
                credential_reads += 1
                stage = "erp_key_read"
                credentials = {"apiKey": self.erp_key_loader(request.company_id, request.service)}
                if not isinstance(credentials["apiKey"], str) or not credentials["apiKey"]:
                    raise ApiManagementError("erp_key_invalid")
            if set(credentials) != names:
                raise ApiManagementError("credentials_missing")
            secrets = [*credentials.values(), profile["applicant"]]
            payload["aplHbin"] = profile["applicant"]
            if request.service in {"MA102", "MA110"}:
                payload["splyHbin"] = profile["applicant"]

            # Audit must be available before a request is sent.
            self.store.record_event(request.company_id, request.service, request.mode, actor, "CALL_START")
            stage = "http_setup"
            transport = self.transport if self.transport is not None else httpx.HTTPTransport(retries=0)
            with httpx.Client(transport=transport, timeout=request.timeout_s, trust_env=False,
                              follow_redirects=False) as client:
                def post(path, body, headers=None):
                    nonlocal calls, stage, http_status, received_bytes
                    stage = "authorize_before_http"
                    fresh()
                    calls += 1
                    stage, http_status, received_bytes = "http_exchange", None, 0
                    with client.stream("POST", BASES[request.mode] + path, json=body, headers=headers) as response:
                        stage, http_status = "response_headers", response.status_code
                        if http_status in {401, 403}:
                            raise ApiManagementError("http_auth_error")
                        if response.status_code != 200:
                            raise ApiManagementError("http_error")
                        if "json" not in response.headers.get("content-type", "").lower():
                            raise ApiManagementError("response_type")
                        if response.headers.get("content-encoding", "identity").lower() != "identity":
                            raise ApiManagementError("compressed_response_not_supported")
                        raw = bytearray()
                        stage = "response_read"
                        for chunk in response.iter_bytes(chunk_size=65536):
                            received_bytes += len(chunk)
                            if len(raw) + len(chunk) > 2 * 1024 * 1024:
                                raise ApiManagementError("response_limit")
                            if time.monotonic() - started > request.timeout_s * 2:
                                raise ApiManagementError("elapsed_limit")
                            raw.extend(chunk)
                        stage = "response_json"
                        value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                        if not isinstance(value, dict):
                            raise ApiManagementError("response_schema")
                        return value

                headers = {"Accept-Encoding": "identity"}
                if names == {"client_id", "client_secret"}:
                    token_result = post("/v1/auth/jwt", credentials, headers)
                    token = token_result.get("jwt")
                    if (token_result.get("status") != "success" or not isinstance(token, str)
                            or not 1 <= len(token) <= 8192 or not token.isascii()
                            or any(c.isspace() or ord(c) < 32 for c in token)):
                        raise ApiManagementError("jwt_rejected")
                    secrets.append(token)
                    headers["Authorization"] = "Bearer " + token
                else:
                    payload["apiKey"] = credentials["apiKey"]
                data = post(SERVICES[request.service].path, payload, headers)
            stage = "authorize_after_http"
            fresh()
            stage = "response_contract"
            list_key, fields = OUTPUTS[request.service]
            row_limit = response_row_limit(request.service)
            if request.service in {"MA109", "MA110"}:
                rows_value = data.get(list_key)
                result_value = data.get("result")
                safe_result = result_value if isinstance(result_value, str) and result_value in {"success", "fail", "error"} else "other"
                # Only shape metadata: never response text, identity, credentials or rows.
                log.info("[kpis.%s.response] result=%s list_present=%s list_type=%s list_length=%s over_limit=%s",
                         request.service.lower(), safe_result, list_key in data, type(rows_value).__name__,
                         len(rows_value) if isinstance(rows_value, list) else None,
                         isinstance(rows_value, list) and len(rows_value) > row_limit)
            if data.get("result") != "success":
                if request.service == "MA102":
                    # Match only the documented message; never log provider text or codes.
                    reason = ("ma102_counterparty_required" if data.get("resultMessage") ==
                              "공급받은자사업자등록번호 은/는 필수 조회 조건입니다." else "provider_rejected")
                    value = data.get("result")
                    safe_result = value if isinstance(value, str) and value in {"fail", "error"} else "other"
                    log.info("[kpis.ma102.response] result=%s reason_code=%s", safe_result, reason)
                    raise ApiManagementError(reason)
                raise ApiManagementError("provider_rejected")
            rows = data.get(list_key)
            if request.service in {"MA109", "MA110"}:
                # Keep the documented array contract; distinguish failures without logging data.
                if list_key not in data:
                    raise ApiManagementError(request.service.lower() + "_rows_missing")
                if rows is None:
                    raise ApiManagementError(request.service.lower() + "_rows_null")
                if not isinstance(rows, list):
                    raise ApiManagementError(request.service.lower() + "_rows_type")
                if len(rows) > row_limit:
                    raise ApiManagementError(request.service.lower() + "_rows_limit")
            if not isinstance(rows, list) or len(rows) > row_limit:
                raise ApiManagementError("response_rows")
            clean = []
            stage = "response_fields"
            for row in rows:
                if not isinstance(row, dict):
                    raise ApiManagementError("response_row")
                safe = {k: v for k, v in row.items() if k in fields}
                if not safe or any(v is not None and type(v) not in {str, int, float, bool} for v in safe.values()):
                    raise ApiManagementError("response_field")
                json.dumps(safe, ensure_ascii=False, allow_nan=False)
                if any(isinstance(v, str) and len(v) > 4096 for v in safe.values()):
                    raise ApiManagementError("response_field_limit")
                if any(isinstance(v, str) and secret in v for v in safe.values() for secret in secrets):
                    raise ApiManagementError("credential_in_response")
                clean.append(safe)
            result.update(status="SUCCESS", reason_code="ok", rows=clean, company_id=request.company_id,
                          service=request.service, mode=request.mode, source="KPIS",
                          retrieved_at=datetime.now(timezone.utc).isoformat())
        except ApiManagementError as exc:
            error_type = "ApiManagementError"
            result.update(reason_code=str(exc))
        except httpx.TimeoutException:
            error_type = "TimeoutException"
            result.update(reason_code="timeout")
        except httpx.ConnectError:
            error_type = "ConnectError"
            result.update(reason_code="connection_error")
        except httpx.DecodingError:
            error_type = "DecodingError"
            result.update(reason_code="response_decoding_error")
        except httpx.ProtocolError:
            error_type = "ProtocolError"
            result.update(reason_code="protocol_error")
        except httpx.NetworkError:
            error_type = "NetworkError"
            result.update(reason_code="network_error")
        except httpx.RequestError:
            error_type = "RequestError"
            result.update(reason_code="transport_error")
        except Exception as exc:
            # No exception text/traceback: JSON errors include the original response document.
            error_type = next((name for kind, name in ((json.JSONDecodeError, "JSONDecodeError"),
                (UnicodeError, "UnicodeError"), (TypeError, "TypeError"), (ValueError, "ValueError"),
                (KeyError, "KeyError"), (RuntimeError, "RuntimeError")) if isinstance(exc, kind)), "OtherException")
            reason = "internal_or_transport_error"
            if stage == "response_json" and isinstance(exc, (ValueError, UnicodeError)):
                reason = "response_encoding_error" if isinstance(exc, UnicodeError) else "response_json_invalid"
            elif stage == "response_fields" and isinstance(exc, ValueError):
                reason = "response_field"
            result.update(reason_code=reason)
        result.update(physical_call_count=calls, credential_read_count=credential_reads,
                      elapsed_ms=int((time.monotonic() - started) * 1000))
        if actor is not None:
            try:
                self.store.record_event(actor.company_id, request.service, request.mode, actor,
                                        "CALL_END", result["reason_code"], calls)
            except Exception:
                result.update(status="DENIED", reason_code="audit_unavailable", rows=[])
                stage, error_type = "audit_end", "AuditError"
        if result["status"] != "SUCCESS":
            # Static stage/type plus numeric metadata only; audit retains the existing reason_code.
            try:
                service = request.service if request.service in INPUTS else "other"
                log.warning("[kpis.rest.failure] service=%s stage=%s error_type=%s http_status=%s response_bytes=%s calls=%s",
                            service, stage, error_type, http_status, received_bytes, calls)
            except Exception:
                pass
        return result
