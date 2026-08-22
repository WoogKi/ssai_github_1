"""Offline regression for the dry-run-first DOCUMENT management CLI."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.knowledge_document_service import DOCUMENT_RETIRED, KnowledgeDocumentRepository
from tools.knowledge_document_manage_cli import (
    PlanValidationError,
    apply_plan,
    authorize_retire,
    retire_document,
    validate_plan,
)


GLOBAL = ("KNOWLEDGE_GLOBAL_MANAGE",)
ERP = ("KNOWLEDGE_GLOBAL_MANAGE", "KNOWLEDGE_ERP_DB_READ")


def _resolver(permissions):
    return lambda **_: permissions


def _write_plan(root: Path, *, classification: str = "GENERAL", scope: str = "GLOBAL", version: int = 1) -> Path:
    content = root / ("erp.md" if classification == "ERP_DB_INTERNAL" else "general.md")
    content.write_text("# UI Smoke\nK-SMOKE document body\n", encoding="utf-8")
    plan = root / "plan.json"
    plan.write_text(json.dumps({"items": [{
        "source_name": content.name,
        "source_key": f"smoke-{classification.lower()}-{scope.lower()}",
        "content_file": content.name,
        "source_kind": "DOCUMENT",
        "scope": scope,
        "company_id": 4 if scope == "COMPANY" else None,
        "version": version,
        "knowledge_classification": classification,
        "search_aliases": ["UI Smoke document"],
    }]}, ensure_ascii=False), encoding="utf-8")
    return plan


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="knowledge-document-cli-"))
    try:
        manifest = root / "manifest"
        general_plan = _write_plan(root)
        general = validate_plan(general_plan)
        assert len(general) == 1
        applied = apply_plan(
            general, manifest_root=manifest, actor_user_id=1, selected_company_id=4,
            permission_resolver=_resolver(GLOBAL),
        )
        assert applied[0]["created"] and applied[0]["status"] == "ACTIVE"
        again = apply_plan(
            general, manifest_root=manifest, actor_user_id=1, selected_company_id=4,
            permission_resolver=_resolver(GLOBAL),
        )
        assert not again[0]["created"] and again[0]["status"] == "ACTIVE"

        document_id = applied[0]["document_id"]
        preview, _ = authorize_retire(
            manifest_root=manifest, document_id=document_id, version=1,
            actor_user_id=1, selected_company_id=4, permission_resolver=_resolver(GLOBAL),
        )
        assert preview.document_id == document_id
        retired = retire_document(
            manifest_root=manifest, document_id=document_id, version=1,
            actor_user_id=1, selected_company_id=4, permission_resolver=_resolver(GLOBAL),
        )
        assert retired["retired"] and retired["status"] == DOCUMENT_RETIRED
        retired_again = retire_document(
            manifest_root=manifest, document_id=document_id, version=1,
            actor_user_id=1, selected_company_id=4, permission_resolver=_resolver(GLOBAL),
        )
        assert not retired_again["retired"] and retired_again["status"] == DOCUMENT_RETIRED
        assert KnowledgeDocumentRepository(root=manifest).retrieve(
            query="K-SMOKE", current_user_id=1, current_company_id=4, permission_codes=["RAG_USE"]
        ).reason_code == "no_authorized_match"

        try:
            authorize_retire(
                manifest_root=manifest, document_id=document_id, version=2,
                actor_user_id=1, selected_company_id=4, permission_resolver=_resolver(GLOBAL),
            )
        except PlanValidationError:
            pass
        else:
            raise AssertionError("wrong retire version was accepted")

        erp_root = root / "erp"
        erp_root.mkdir()
        erp_plan = _write_plan(erp_root, classification="ERP_DB_INTERNAL")
        erp = validate_plan(erp_plan)
        try:
            apply_plan(
                erp, manifest_root=erp_root / "manifest", actor_user_id=1, selected_company_id=4,
                permission_resolver=_resolver(GLOBAL),
            )
        except PermissionError:
            pass
        else:
            raise AssertionError("ERP document was approved without ERP read permission")
        assert not (erp_root / "manifest" / "manifest.json").exists()
        erp_applied = apply_plan(
            erp, manifest_root=erp_root / "manifest", actor_user_id=1, selected_company_id=4,
            permission_resolver=_resolver(ERP),
        )
        assert erp_applied[0]["knowledge_classification"] == "ERP_DB_INTERNAL"
        try:
            authorize_retire(
                manifest_root=erp_root / "manifest", document_id=erp_applied[0]["document_id"], version=1,
                actor_user_id=1, selected_company_id=4, permission_resolver=_resolver(GLOBAL),
            )
        except PermissionError:
            pass
        else:
            raise AssertionError("ERP document retirement preview accepted without ERP read permission")
        assert KnowledgeDocumentRepository(root=erp_root / "manifest")._read_manifest()[0].status == "ACTIVE"

        invalid = root / "invalid.json"
        invalid.write_text(json.dumps({"items": [{
            "source_name": "bad.md", "source_key": "bad", "content_file": "general.md",
            "source_kind": "PROJECT_SOURCE", "scope": "GLOBAL", "version": 1,
            "knowledge_classification": "GENERAL",
        }]}), encoding="utf-8")
        try:
            validate_plan(invalid)
        except PlanValidationError:
            pass
        else:
            raise AssertionError("non-DOCUMENT plan was accepted")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("RESULT OK tests=15 db_write=0 manifest_apply=temporary_fixture_only")


if __name__ == "__main__":
    main()
