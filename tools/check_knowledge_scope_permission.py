"""Fixture regression for deterministic knowledge scope authorization."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.knowledge_scope_policy import (  # noqa: E402
    KNOWLEDGE_COMPANY_MANAGE,
    KNOWLEDGE_GLOBAL_MANAGE,
    can_manage_document,
    can_read_document,
)


def _document(scope: str, *, company_id: int | None = None, user_id: int | None = None, status: str = "ACTIVE") -> dict:
    return {"scope": scope, "company_id": company_id, "user_id": user_id, "status": status}


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

    _assert(can_manage_document(document=_document("GLOBAL"), current_company_id=4, permission_codes=[KNOWLEDGE_GLOBAL_MANAGE]), True, "global_manage_allowed")
    _assert(can_manage_document(document=_document("GLOBAL"), current_company_id=4, permission_codes=["USER_MANAGE_ALL"]), False, "missing_global_manage")
    _assert(can_manage_document(document=_document("COMPANY", company_id=4), current_company_id=4, permission_codes=[KNOWLEDGE_COMPANY_MANAGE]), True, "company_manage_allowed")
    _assert(can_manage_document(document=_document("COMPANY", company_id=4), current_company_id=6, permission_codes=[KNOWLEDGE_COMPANY_MANAGE]), False, "company_mismatch")
    _assert(can_manage_document(document=_document("USER", company_id=4, user_id=11), current_company_id=4, permission_codes=["UPLOAD_FILE"]), False, "user_scope_management_not_defined")
    print("RESULT OK tests=25")


if __name__ == "__main__":
    main()
