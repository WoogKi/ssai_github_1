"""Deterministic authorization rules for future knowledge retrieval.

This module deliberately has no database, Streamlit, or LLM dependency.  The
retrieval boundary must resolve the active user's permissions for the selected
company first, then pass those permissions here before any document is offered
to retrieval.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.services.ssai_permission_policy import get_required_permission


class KnowledgeScope(StrEnum):
    GLOBAL = "GLOBAL"
    COMPANY = "COMPANY"
    USER = "USER"


class KnowledgeDocumentStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    DISABLED = "DISABLED"


class KnowledgeClassification(StrEnum):
    GENERAL = "GENERAL"
    ERP_DB_INTERNAL = "ERP_DB_INTERNAL"


KNOWLEDGE_GLOBAL_MANAGE = "KNOWLEDGE_GLOBAL_MANAGE"
KNOWLEDGE_COMPANY_MANAGE = "KNOWLEDGE_COMPANY_MANAGE"
KNOWLEDGE_ERP_DB_READ = "KNOWLEDGE_ERP_DB_READ"
KNOWLEDGE_PROJECT_SOURCE_READ = "KNOWLEDGE_PROJECT_SOURCE_READ"

_SOURCE_KIND_DOCUMENT = "DOCUMENT"
_SOURCE_KIND_PROJECT_SOURCE = "PROJECT_SOURCE"


@dataclass(frozen=True)
class KnowledgeAccessDecision:
    allowed: bool
    reason_code: str


def _optional_positive_id(value: Any) -> tuple[int | None, bool]:
    """Return ``(id, valid)`` without turning malformed metadata into blank.

    A missing owner is valid only when the field is genuinely blank.  This is
    intentionally stricter than ``int(value)`` because GLOBAL documents must
    not become public when a corrupt company/user value is coerced to None.
    """
    if value is None or value == "":
        return None, True
    if isinstance(value, bool):
        return None, False
    if isinstance(value, int):
        return (value, value > 0)
    if isinstance(value, str):
        normalized = value.strip()
        if normalized and normalized.isdigit():
            result = int(normalized)
            return (result, result > 0)
    return None, False


def _permissions(permission_codes: Iterable[str] | None) -> set[str]:
    return {
        str(code).strip().upper()
        for code in (permission_codes or [])
        if str(code).strip()
    }


def _document_value(document: Mapping[str, Any], key: str) -> Any:
    return document.get(key)


def validate_document_scope(document: Mapping[str, Any]) -> KnowledgeAccessDecision:
    """Validate visibility metadata before using it for authorization.

    Invalid or ambiguous metadata is not normalized into a broader scope.
    """
    try:
        scope = KnowledgeScope(str(_document_value(document, "scope") or "").strip().upper())
    except ValueError:
        return KnowledgeAccessDecision(False, "invalid_scope")

    company_id, company_valid = _optional_positive_id(_document_value(document, "company_id"))
    user_id, user_valid = _optional_positive_id(_document_value(document, "user_id"))
    if scope is KnowledgeScope.GLOBAL:
        if not company_valid or not user_valid or company_id is not None or user_id is not None:
            return KnowledgeAccessDecision(False, "global_scope_has_owner")
    elif scope is KnowledgeScope.COMPANY:
        if not company_valid:
            return KnowledgeAccessDecision(False, "company_scope_invalid_company")
        if company_id is None:
            return KnowledgeAccessDecision(False, "company_scope_missing_company")
    else:
        if not company_valid or not user_valid:
            return KnowledgeAccessDecision(False, "user_scope_invalid_owner")
        if company_id is None or user_id is None:
            return KnowledgeAccessDecision(False, "user_scope_missing_owner")
    return KnowledgeAccessDecision(True, "valid")


def validate_document_classification(document: Mapping[str, Any]) -> KnowledgeAccessDecision:
    """Validate the security classification without broadening malformed values.

    Missing metadata means GENERAL only for manifests created before this field
    existed. New registration calls always supply the field explicitly.
    """
    if "knowledge_classification" not in document:
        return KnowledgeAccessDecision(True, "general_classification")
    raw = _document_value(document, "knowledge_classification")
    if not isinstance(raw, str):
        return KnowledgeAccessDecision(False, "invalid_knowledge_classification")
    try:
        classification = KnowledgeClassification(raw.strip().upper())
    except ValueError:
        return KnowledgeAccessDecision(False, "invalid_knowledge_classification")
    return KnowledgeAccessDecision(
        True,
        "erp_db_internal_classification"
        if classification is KnowledgeClassification.ERP_DB_INTERNAL
        else "general_classification",
    )


def _source_kind(document: Mapping[str, Any]) -> tuple[str | None, KnowledgeAccessDecision | None]:
    """Return the manifest source kind without broadening malformed metadata.

    Missing source_kind is retained as DOCUMENT for pre-Project-Source manifests.
    New manifests always persist a concrete value through DocumentSource.
    """
    raw = _document_value(document, "source_kind")
    if raw is None:
        return _SOURCE_KIND_DOCUMENT, None
    if not isinstance(raw, str):
        return None, KnowledgeAccessDecision(False, "invalid_source_kind")
    normalized = raw.strip().upper()
    if normalized not in {_SOURCE_KIND_DOCUMENT, _SOURCE_KIND_PROJECT_SOURCE}:
        return None, KnowledgeAccessDecision(False, "invalid_source_kind")
    return normalized, None

def can_read_document(
    *,
    document: Mapping[str, Any],
    current_user_id: int | None,
    current_company_id: int | None,
    permission_codes: Iterable[str] | None,
    technical_detail_mode: bool = False,
) -> KnowledgeAccessDecision:
    """Return a fail-closed decision for a future RAG retrieval candidate."""
    if not isinstance(technical_detail_mode, bool):
        return KnowledgeAccessDecision(False, "invalid_technical_detail_mode")
    if get_required_permission(special="rag") not in _permissions(permission_codes):
        return KnowledgeAccessDecision(False, "missing_rag_use")

    if str(_document_value(document, "status") or "").strip().upper() != KnowledgeDocumentStatus.ACTIVE:
        return KnowledgeAccessDecision(False, "document_not_active")

    valid = validate_document_scope(document)
    if not valid.allowed:
        return valid

    scope = KnowledgeScope(str(_document_value(document, "scope")).strip().upper())
    allowed_reason = "global_active"
    if scope is not KnowledgeScope.GLOBAL:
        company_id, _ = _optional_positive_id(_document_value(document, "company_id"))
        selected_company_id, selected_company_valid = _optional_positive_id(current_company_id)
        if not selected_company_valid or company_id != selected_company_id:
            return KnowledgeAccessDecision(False, "company_mismatch")
        allowed_reason = "company_match"
        if scope is KnowledgeScope.USER:
            document_user_id, _ = _optional_positive_id(_document_value(document, "user_id"))
            selected_user_id, selected_user_valid = _optional_positive_id(current_user_id)
            if not selected_user_valid or document_user_id != selected_user_id:
                return KnowledgeAccessDecision(False, "user_mismatch")
            allowed_reason = "user_match"

    classification = validate_document_classification(document)
    if not classification.allowed:
        return classification
    source_kind, source_kind_error = _source_kind(document)
    if source_kind_error is not None:
        return source_kind_error
    permissions = _permissions(permission_codes)
    requires_project_source_read = source_kind == _SOURCE_KIND_PROJECT_SOURCE
    requires_erp_db_read = classification.reason_code == "erp_db_internal_classification"
    requires_technical_detail = requires_project_source_read or requires_erp_db_read
    if requires_technical_detail and not technical_detail_mode:
        return KnowledgeAccessDecision(False, "technical_detail_mode_required")
    if requires_project_source_read and KNOWLEDGE_PROJECT_SOURCE_READ not in permissions:
        return KnowledgeAccessDecision(False, "missing_project_source_read")
    if requires_erp_db_read and KNOWLEDGE_ERP_DB_READ not in permissions:
        return KnowledgeAccessDecision(False, "missing_erp_db_read")
    return KnowledgeAccessDecision(True, allowed_reason)


def can_manage_document(
    *,
    document: Mapping[str, Any],
    current_company_id: int | None,
    permission_codes: Iterable[str] | None,
) -> KnowledgeAccessDecision:
    """Authorize only future GLOBAL/COMPANY knowledge administration.

    USER attachment lifecycle is intentionally left to its existing owner and
    retention policy; UPLOAD_FILE is not silently promoted to a generic
    document-management permission.
    """
    valid = validate_document_scope(document)
    if not valid.allowed:
        return valid
    classification = validate_document_classification(document)
    if not classification.allowed:
        return classification

    permissions = _permissions(permission_codes)
    scope = KnowledgeScope(str(_document_value(document, "scope")).strip().upper())
    if scope is KnowledgeScope.GLOBAL:
        return KnowledgeAccessDecision(
            KNOWLEDGE_GLOBAL_MANAGE in permissions,
            "global_manage_allowed" if KNOWLEDGE_GLOBAL_MANAGE in permissions else "missing_global_manage",
        )
    if scope is KnowledgeScope.COMPANY:
        document_company_id, _ = _optional_positive_id(_document_value(document, "company_id"))
        selected_company_id, selected_company_valid = _optional_positive_id(current_company_id)
        if not selected_company_valid or document_company_id != selected_company_id:
            return KnowledgeAccessDecision(False, "company_mismatch")
        return KnowledgeAccessDecision(
            KNOWLEDGE_COMPANY_MANAGE in permissions,
            "company_manage_allowed" if KNOWLEDGE_COMPANY_MANAGE in permissions else "missing_company_manage",
        )
    return KnowledgeAccessDecision(False, "user_scope_management_not_defined")
