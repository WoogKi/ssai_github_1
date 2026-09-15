"""Filesystem-only regression for the PROJECT_SOURCE retirement command."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from app.services.knowledge_document_service import (  # noqa: E402
    DOCUMENT_RETIRED,
    KnowledgeDocumentRepository,
    SOURCE_KIND_DOCUMENT,
    SOURCE_KIND_PROJECT_SOURCE,
)
from app.services.knowledge_role_policy import KNOWLEDGE_ROLE_PERMISSION_MATRIX  # noqa: E402
import project_source_knowledge_cli as cli  # noqa: E402
from project_source_knowledge_cli import (  # noqa: E402
    PlanValidationError,
    authorize_retire,
    preview_retire,
    retire_project_source,
)


REVISION = "a" * 40


def _resolver(role_code: str):
    def resolve(*, actor_user_id: int, selected_company_id: int):
        assert actor_user_id == 1
        assert selected_company_id == 7
        return KNOWLEDGE_ROLE_PERMISSION_MATRIX[role_code]

    return resolve


def _project_source(
    repo: KnowledgeDocumentRepository,
    *,
    version: int = 1,
    symbol: str = "sample",
):
    source_key = f"project-source:app/services/example.py#{symbol}"
    body = f"def {symbol}():\n    return 1"
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    content = (
        f"# path: app/services/example.py | symbol: {symbol} | lines: 1-2 "
        f"| commit: {REVISION} | sha256: {body_hash}\n\n"
        f"```python\n{body}\n```"
    )
    source, _ = repo._register_text_trusted(
        source_name="project_source_example.py",
        source_key=source_key,
        content=content,
        scope="GLOBAL",
        version=version,
        knowledge_classification="GENERAL",
        source_kind=SOURCE_KIND_PROJECT_SOURCE,
        source_revision=REVISION,
        source_content_hash=body_hash,
    )
    return repo._approve_trusted(document_id=source.document_id)


def _document(repo: KnowledgeDocumentRepository):
    source, _ = repo._register_text_trusted(
        source_name="guide.md",
        source_key="document:guide",
        content="# Guide\n\ntext",
        scope="GLOBAL",
        version=1,
        knowledge_classification="GENERAL",
        source_kind=SOURCE_KIND_DOCUMENT,
    )
    return repo._approve_trusted(document_id=source.document_id)


def _expect_failure(callback, expected: type[Exception]) -> bool:
    try:
        callback()
    except expected:
        return True
    return False


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="project-source-retire-"))
    results: dict[str, bool] = {}
    try:
        repo = KnowledgeDocumentRepository(root=root)
        target = _project_source(repo)
        other_source = _project_source(repo, symbol="other")
        document = _document(repo)
        manifest_path = root / "manifest.json"

        before = manifest_path.read_bytes()
        preview, _ = authorize_retire(
            manifest_root=root,
            document_id=target.document_id,
            version=target.version,
            actor_user_id=1,
            selected_company_id=7,
            permission_resolver=_resolver("SYSTEM_ADMIN"),
        )
        after = manifest_path.read_bytes()
        results["preview_write_count_zero"] = (
            preview.document_id == target.document_id and before == after
        )

        results["document_kind_denied"] = _expect_failure(
            lambda: preview_retire(
                manifest_root=root,
                document_id=document.document_id,
                version=document.version,
            ),
            PlanValidationError,
        )

        results["source_kind_mismatch_denied"] = _expect_failure(
            lambda: preview_retire(
                manifest_root=root,
                document_id=document.document_id,
                version=document.version,
            ),
            PlanValidationError,
        )

        results["wrong_document_id_denied"] = _expect_failure(
            lambda: preview_retire(
                manifest_root=root,
                document_id="missing-document-id",
                version=target.version,
            ),
            ValueError,
        )
        results["wrong_version_denied"] = _expect_failure(
            lambda: preview_retire(
                manifest_root=root,
                document_id=target.document_id,
                version=target.version + 1,
            ),
            PlanValidationError,
        )
        results["permission_denied"] = _expect_failure(
            lambda: authorize_retire(
                manifest_root=root,
                document_id=target.document_id,
                version=target.version,
                actor_user_id=1,
                selected_company_id=7,
                permission_resolver=_resolver("SSART_STAFF"),
            ),
            PermissionError,
        )
        results["bad_manifest_root_denied"] = _expect_failure(
            lambda: preview_retire(
                manifest_root=root / "missing",
                document_id=target.document_id,
                version=target.version,
            ),
            ValueError,
        )

        pending_root = root / "pending"
        pending_repo = KnowledgeDocumentRepository(root=pending_root)
        pending = _project_source(pending_repo)
        pending_repo._write_manifest([
            replace(pending, approval_status="PENDING", approved_at=None)
        ])
        results["pending_approval_denied"] = _expect_failure(
            lambda: preview_retire(
                manifest_root=pending_root,
                document_id=pending.document_id,
                version=pending.version,
            ),
            PlanValidationError,
        )

        artifact_path = root / "artifacts" / f"{target.content_hash}.json"
        artifact_before = artifact_path.read_bytes()
        apply_result = retire_project_source(
            manifest_root=root,
            document_id=target.document_id,
            version=target.version,
            actor_user_id=1,
            selected_company_id=7,
            permission_resolver=_resolver("SYSTEM_ADMIN"),
        )
        final = {item.document_id: item for item in repo._read_manifest()}
        results["apply_active_to_retired"] = (
            apply_result["retired"] is True
            and final[target.document_id].status == DOCUMENT_RETIRED
        )
        results["identity_approval_and_artifact_preserved"] = (
            final[target.document_id].document_id == target.document_id
            and final[target.document_id].source_key == target.source_key
            and final[target.document_id].version == target.version
            and final[target.document_id].approval_status == target.approval_status
            and final[target.document_id].approved_at == target.approved_at
            and artifact_path.read_bytes() == artifact_before
        )
        results["other_project_source_versions_unchanged"] = all(
            item.status == other_source.status
            and item.approval_status == other_source.approval_status
            and item.content_hash == other_source.content_hash
            for item in repo._read_manifest()
            if item.document_id == other_source.document_id
        )
        results["document_corpus_unchanged"] = (
            final[document.document_id].status == document.status
            and final[document.document_id].content_hash == document.content_hash
        )
        results["already_retired_denied"] = _expect_failure(
            lambda: preview_retire(
                manifest_root=root,
                document_id=target.document_id,
                version=target.version,
            ),
            PlanValidationError,
        )

        superseded_root = root / "superseded"
        superseded_repo = KnowledgeDocumentRepository(root=superseded_root)
        superseded = _project_source(superseded_repo, version=1)
        _project_source(superseded_repo, version=2)
        results["superseded_denied"] = _expect_failure(
            lambda: preview_retire(
                manifest_root=superseded_root,
                document_id=superseded.document_id,
                version=superseded.version,
            ),
            PlanValidationError,
        )

        # Exercise the existing validate/apply/freshness paths without running Git.
        legacy_repo = root / "legacy-repo"
        legacy_source = legacy_repo / "app/services/example.py"
        legacy_source.parent.mkdir(parents=True)
        legacy_source.write_text("def sample():\n    return 1\n", encoding="utf-8")
        legacy_plan = legacy_repo / "plan.json"
        legacy_plan.write_text(json.dumps({"items": [{
            "path": "app/services/example.py",
            "symbol": "sample",
            "source_kind": "PROJECT_SOURCE",
            "source_revision": REVISION,
            "knowledge_classification": "GENERAL",
            "scope": "GLOBAL",
            "version": 1,
            "search_aliases": ["예제 함수"],
        }]}, ensure_ascii=False), encoding="utf-8")
        original_head = cli.current_head
        original_show = cli._git_show_source
        original_worktree = cli._worktree_matches_revision
        try:
            cli.current_head = lambda repo_root=ROOT: REVISION
            cli._git_show_source = lambda **kwargs: legacy_source.read_text(encoding="utf-8")
            cli._worktree_matches_revision = lambda **kwargs: True
            legacy_items = cli.validate_plan(
                legacy_plan,
                repo_root=legacy_repo,
                head=REVISION,
            )
            legacy_manifest = root / "legacy-manifest"
            legacy_apply = cli.apply_plan(
                legacy_items,
                manifest_root=legacy_manifest,
                actor_user_id=1,
                selected_company_id=7,
                permission_resolver=_resolver("SYSTEM_ADMIN"),
                repo_root=legacy_repo,
            )
            legacy_freshness = cli.freshness_report(
                manifest_root=legacy_manifest,
                repo_root=legacy_repo,
                head=REVISION,
            )
        finally:
            cli.current_head = original_head
            cli._git_show_source = original_show
            cli._worktree_matches_revision = original_worktree
        results["existing_validate_apply_freshness_regression"] = (
            len(legacy_items) == 1
            and legacy_apply[0]["approval_status"] == "APPROVED"
            and legacy_freshness[0]["status"] == "CURRENT"
        )

        ok = all(results.values())
        print(json.dumps({
            "ok": ok,
            "case_count": len(results),
            "results": results,
            "operating_write_count": 0,
        }, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
