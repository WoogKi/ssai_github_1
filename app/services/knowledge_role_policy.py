"""Declared Knowledge permission policy for predefined RBAC roles.

Runtime authorization always uses effective permissions loaded from the SSAI
database. This module is intentionally declarative: it keeps seed planning and
offline policy gates aligned without granting an individual user anything.
"""

from __future__ import annotations


KNOWLEDGE_PERMISSION_CODES = (
    "RAG_USE",
    "KNOWLEDGE_PROJECT_SOURCE_READ",
    "KNOWLEDGE_ERP_DB_READ",
    "KNOWLEDGE_GLOBAL_MANAGE",
    "KNOWLEDGE_COMPANY_MANAGE",
)

# SSART_ADMIN/SUPER is the user identity for the SYSTEM_ADMIN RBAC role.
PREDEFINED_KNOWLEDGE_ROLE_CODES = (
    "SYSTEM_ADMIN",
    "SSART_MANAGER",
    "SSART_STAFF",
    "WHOLESALE_MANAGER",
    "WHOLESALE_STAFF",
    "WHOLESALE_READONLY",
)


KNOWLEDGE_ROLE_PERMISSION_MATRIX: dict[str, frozenset[str]] = {
    "SYSTEM_ADMIN": frozenset(KNOWLEDGE_PERMISSION_CODES),
    "SSART_MANAGER": frozenset(
        {
            "RAG_USE",
            "KNOWLEDGE_PROJECT_SOURCE_READ",
            "KNOWLEDGE_ERP_DB_READ",
            "KNOWLEDGE_COMPANY_MANAGE",
        }
    ),
    "SSART_STAFF": frozenset({"RAG_USE"}),
    "WHOLESALE_MANAGER": frozenset({"RAG_USE", "KNOWLEDGE_COMPANY_MANAGE"}),
    "WHOLESALE_STAFF": frozenset({"RAG_USE"}),
    "WHOLESALE_READONLY": frozenset(),
}


def role_codes_granted(permission_code: str) -> tuple[str, ...]:
    """Return predefined roles allowed for one declared Knowledge permission."""
    normalized = str(permission_code or "").strip()
    if normalized not in KNOWLEDGE_PERMISSION_CODES:
        raise ValueError("unknown Knowledge permission")
    return tuple(
        role_code
        for role_code in PREDEFINED_KNOWLEDGE_ROLE_CODES
        if normalized in KNOWLEDGE_ROLE_PERMISSION_MATRIX[role_code]
    )
