"""Fixture regression for deterministic knowledge scope authorization."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.knowledge_scope_policy import (  # noqa: E402
    KNOWLEDGE_COMPANY_MANAGE,
    KNOWLEDGE_ERP_DB_READ,
    KNOWLEDGE_GLOBAL_MANAGE,
    KNOWLEDGE_PROJECT_SOURCE_READ,
    can_manage_document,
    can_read_document,
)
from app.services.knowledge_role_policy import KNOWLEDGE_ROLE_PERMISSION_MATRIX  # noqa: E402


def _document(
    scope: str,
    *,
    company_id: int | None = None,
    user_id: int | None = None,
    status: str = "ACTIVE",
    classification: object = "GENERAL",
    source_kind: object = "DOCUMENT",
) -> dict:
    return {
        "scope": scope,
        "company_id": company_id,
        "user_id": user_id,
        "status": status,
        "knowledge_classification": classification,
        "source_kind": source_kind,
    }


def _assert(decision, allowed: bool, reason: str) -> None:
    assert decision.allowed is allowed, decision
    assert decision.reason_code == reason, decision


def main() -> None:
    rag = ["RAG_USE"]
    _assert(can_read_document(document=_document("GLOBAL"), current_user_id=11, current_company_id=4, permission_codes=rag), True, "global_active")
    _assert(can_read_document(document=_document("GLOBAL"), current_user_id=11, current_company_id=4, permission_codes=[]), False, "missing_rag_use")
    _assert(can_read_document(document=_document("COMPANY", company_id=4), current_user_id=11, current_company_id=4, permission_codes=rag), True, "company_match")
    _assert(can_read_document(document=_document("COMPANY", company_id=4), current_user_id=11, current_company_id=6, permission_codes=rag), False, "company_mismatch")
    _assert(can_read_document(document=_document("USER", company_id=4, user_id=11), current_user_id=11, current_company_id=4, permission_codes=rag), True, "user_match")
    _assert(can_read_document(document=_document("USER", company_id=4, user_id=11), current_user_id=12, current_company_id=4, permission_codes=rag), False, "user_mismatch")
    _assert(can_read_document(document=_document("COMPANY", company_id=4, status="DISABLED"), current_user_id=11, current_company_id=4, permission_codes=rag), False, "document_not_active")
    _assert(can_read_document(document=_document("GLOBAL", company_id=4), current_user_id=11, current_company_id=4, permission_codes=rag), False, "global_scope_has_owner")
    _assert(can_read_document(document=_document("USER", company_id=4), current_user_id=11, current_company_id=4, permission_codes=rag), False, "user_scope_missing_owner")
    _assert(can_read_document(document=_document("COMPANY", company_id=4), current_user_id=11, current_company_id=4, permission_codes=["RAG_USE", "SUPER"]), True, "company_match")
    _assert(can_read_document(document=_document("GLOBAL", company_id="abc"), current_user_id=11, current_company_id=4, permission_codes=rag), False, "global_scope_has_owner")
    _assert(can_read_document(document=_document("GLOBAL", user_id="abc"), current_user_id=11, current_company_id=4, permission_codes=rag), False, "global_scope_has_owner")
    _assert(can_read_document(document=_document("GLOBAL", company_id="   "), current_user_id=11, current_company_id=4, permission_codes=rag), False, "global_scope_has_owner")
    _assert(can_read_document(document=_document("COMPANY", company_id="abc"), current_user_id=11, current_company_id=4, permission_codes=rag), False, "company_scope_invalid_company")
    _assert(can_read_document(document=_document("USER", company_id="abc", user_id=11), current_user_id=11, current_company_id=4, permission_codes=rag), False, "user_scope_invalid_owner")
    _assert(can_read_document(document=_document("USER", company_id=4, user_id="abc"), current_user_id=11, current_company_id=4, permission_codes=rag), False, "user_scope_invalid_owner")
    _assert(can_read_document(document=_document("GLOBAL", company_id=-1), current_user_id=11, current_company_id=4, permission_codes=rag), False, "global_scope_has_owner")
    _assert(can_read_document(document=_document("GLOBAL", user_id=0), current_user_id=11, current_company_id=4, permission_codes=rag), False, "global_scope_has_owner")
    _assert(can_read_document(document=_document("COMPANY", company_id=0), current_user_id=11, current_company_id=4, permission_codes=rag), False, "company_scope_invalid_company")
    _assert(can_read_document(document=_document("USER", company_id=4, user_id=-1), current_user_id=11, current_company_id=4, permission_codes=rag), False, "user_scope_invalid_owner")

    project_source = _document("GLOBAL", source_kind="PROJECT_SOURCE")
    _assert(can_read_document(document=project_source, current_user_id=11, current_company_id=4, permission_codes=["RAG_USE", KNOWLEDGE_PROJECT_SOURCE_READ]), False, "technical_detail_mode_required")
    _assert(can_read_document(document=project_source, current_user_id=11, current_company_id=4, permission_codes=["RAG_USE"], technical_detail_mode=True), False, "missing_project_source_read")
    _assert(can_read_document(document=project_source, current_user_id=11, current_company_id=4, permission_codes=["RAG_USE", KNOWLEDGE_PROJECT_SOURCE_READ], technical_detail_mode=True), True, "global_active")
    _assert(can_read_document(document=_document("GLOBAL", source_kind="SOURCE"), current_user_id=11, current_company_id=4, permission_codes=rag), False, "invalid_source_kind")
    _assert(can_read_document(document=_document("GLOBAL"), current_user_id=11, current_company_id=4, permission_codes=rag, technical_detail_mode="true"), False, "invalid_technical_detail_mode")

    erp_document = _document("GLOBAL", classification="ERP_DB_INTERNAL")
    role_permissions = KNOWLEDGE_ROLE_PERMISSION_MATRIX
    for role_code in ("SYSTEM_ADMIN", "SSART_MANAGER"):
        _assert(can_read_document(document=erp_document, current_user_id=11, current_company_id=4, permission_codes=role_permissions[role_code]), False, "technical_detail_mode_required")
        _assert(can_read_document(document=erp_document, current_user_id=11, current_company_id=4, permission_codes=role_permissions[role_code], technical_detail_mode=True), True, "global_active")
    _assert(can_read_document(document=erp_document, current_user_id=11, current_company_id=4, permission_codes=["RAG_USE", KNOWLEDGE_ERP_DB_READ], technical_detail_mode=True), True, "global_active")
    _assert(can_read_document(document=erp_document, current_user_id=11, current_company_id=4, permission_codes=["RAG_USE", KNOWLEDGE_PROJECT_SOURCE_READ], technical_detail_mode=True), False, "missing_erp_db_read")
    for role_code in ("SSART_STAFF", "WHOLESALE_MANAGER", "WHOLESALE_STAFF"):
        _assert(can_read_document(document=erp_document, current_user_id=11, current_company_id=4, permission_codes=role_permissions[role_code], technical_detail_mode=True), False, "missing_erp_db_read")
    _assert(can_read_document(document=erp_document, current_user_id=11, current_company_id=4, permission_codes=role_permissions["WHOLESALE_READONLY"]), False, "missing_rag_use")
    erp_project_source = _document("GLOBAL", classification="ERP_DB_INTERNAL", source_kind="PROJECT_SOURCE")
    _assert(can_read_document(document=erp_project_source, current_user_id=11, current_company_id=4, permission_codes=["RAG_USE", KNOWLEDGE_ERP_DB_READ], technical_detail_mode=True), False, "missing_project_source_read")
    _assert(can_read_document(document=erp_project_source, current_user_id=11, current_company_id=4, permission_codes=["RAG_USE", KNOWLEDGE_PROJECT_SOURCE_READ], technical_detail_mode=True), False, "missing_erp_db_read")
    _assert(can_read_document(document=erp_project_source, current_user_id=11, current_company_id=4, permission_codes=["RAG_USE", KNOWLEDGE_PROJECT_SOURCE_READ, KNOWLEDGE_ERP_DB_READ], technical_detail_mode=True), True, "global_active")
    company_erp = _document("COMPANY", company_id=4, classification="ERP_DB_INTERNAL")
    _assert(can_read_document(document=company_erp, current_user_id=11, current_company_id=4, permission_codes=role_permissions["SYSTEM_ADMIN"], technical_detail_mode=True), True, "company_match")
    _assert(can_read_document(document=company_erp, current_user_id=11, current_company_id=6, permission_codes=role_permissions["SYSTEM_ADMIN"], technical_detail_mode=True), False, "company_mismatch")
    for malformed in (None, "", "SECRET", 1, True):
        _assert(can_read_document(document=_document("GLOBAL", classification=malformed), current_user_id=11, current_company_id=4, permission_codes=role_permissions["SYSTEM_ADMIN"], technical_detail_mode=True), False, "invalid_knowledge_classification")

    _assert(can_manage_document(document=_document("GLOBAL"), current_company_id=4, permission_codes=[KNOWLEDGE_GLOBAL_MANAGE]), True, "global_manage_allowed")
    _assert(can_manage_document(document=_document("GLOBAL"), current_company_id=4, permission_codes=["USER_MANAGE_ALL"]), False, "missing_global_manage")
    _assert(can_manage_document(document=_document("COMPANY", company_id=4), current_company_id=4, permission_codes=[KNOWLEDGE_COMPANY_MANAGE]), True, "company_manage_allowed")
    _assert(can_manage_document(document=_document("COMPANY", company_id=4), current_company_id=6, permission_codes=[KNOWLEDGE_COMPANY_MANAGE]), False, "company_mismatch")
    _assert(can_manage_document(document=_document("USER", company_id=4, user_id=11), current_company_id=4, permission_codes=["UPLOAD_FILE"]), False, "user_scope_management_not_defined")
    _assert(can_manage_document(document=_document("GLOBAL", classification="ERP_DB_INTERNAL"), current_company_id=4, permission_codes=[KNOWLEDGE_GLOBAL_MANAGE]), True, "global_manage_allowed")
    _assert(can_manage_document(document=_document("GLOBAL", classification="INVALID"), current_company_id=4, permission_codes=[KNOWLEDGE_GLOBAL_MANAGE]), False, "invalid_knowledge_classification")
    print("RESULT OK tests=49")


if __name__ == "__main__":
    main()
