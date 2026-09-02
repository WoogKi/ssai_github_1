"""Offline matrix regression for predefined Knowledge RBAC roles."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.knowledge_role_policy import (  # noqa: E402
    KNOWLEDGE_PERMISSION_CODES,
    KNOWLEDGE_ROLE_PERMISSION_MATRIX,
    PREDEFINED_KNOWLEDGE_ROLE_CODES,
    role_codes_granted,
)


def main() -> None:
    expected = {
        "SYSTEM_ADMIN": set(KNOWLEDGE_PERMISSION_CODES),
        "SSART_MANAGER": {
            "RAG_USE",
            "KNOWLEDGE_PROJECT_SOURCE_READ",
            "KNOWLEDGE_ERP_DB_READ",
            "KNOWLEDGE_COMPANY_MANAGE",
        },
        "SSART_STAFF": {"RAG_USE"},
        "WHOLESALE_MANAGER": {"RAG_USE", "KNOWLEDGE_COMPANY_MANAGE"},
        "WHOLESALE_STAFF": {"RAG_USE"},
        "WHOLESALE_READONLY": set(),
    }
    assert tuple(KNOWLEDGE_ROLE_PERMISSION_MATRIX) == PREDEFINED_KNOWLEDGE_ROLE_CODES
    for role_code, permissions in expected.items():
        assert KNOWLEDGE_ROLE_PERMISSION_MATRIX[role_code] == frozenset(permissions)

    assert role_codes_granted("KNOWLEDGE_GLOBAL_MANAGE") == ("SYSTEM_ADMIN",)
    assert role_codes_granted("KNOWLEDGE_COMPANY_MANAGE") == (
        "SYSTEM_ADMIN", "SSART_MANAGER", "WHOLESALE_MANAGER"
    )
    assert role_codes_granted("RAG_USE") == (
        "SYSTEM_ADMIN", "SSART_MANAGER", "SSART_STAFF",
        "WHOLESALE_MANAGER", "WHOLESALE_STAFF",
    )
    try:
        role_codes_granted("UNKNOWN")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown permission was accepted")
    print("RESULT OK tests=10 db_write_count=0")


if __name__ == "__main__":
    main()
