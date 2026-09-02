"""Fixture regression for manual Project Source Knowledge approval and freshness CLI."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.knowledge_document_service import KnowledgeDocumentRepository
from app.services.knowledge_role_policy import KNOWLEDGE_ROLE_PERMISSION_MATRIX
from project_source_knowledge_cli import (
    PlanValidationError,
    apply_plan,
    current_head,
    freshness_report,
    validate_plan,
)


ROLE_PERMISSIONS = KNOWLEDGE_ROLE_PERMISSION_MATRIX


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "--", "app/services/example.py")
    _git(root, "commit", "-m", message)
    return current_head(root)


def _plan(plan_dir: Path, head: str, **overrides) -> Path:
    item = {
        "path": "app/services/example.py",
        "symbol": "sample",
        "source_kind": "PROJECT_SOURCE",
        "source_revision": head,
        "knowledge_classification": "GENERAL",
        "scope": "GLOBAL",
        "version": 1,
        "search_aliases": ["예제 함수"],
    }
    item.update(overrides)
    plan = plan_dir / f"plan-{len(list(plan_dir.glob('plan-*.json')))}.json"
    plan.write_text(json.dumps({"items": [item]}, ensure_ascii=False), encoding="utf-8")
    return plan


def _expect_validation(plan: Path, root: Path, head: str) -> None:
    try:
        validate_plan(plan, repo_root=root, head=head)
    except PlanValidationError:
        return
    raise AssertionError("invalid approval plan must fail closed")


def _resolver(role_code: str):
    def resolve(*, actor_user_id: int, selected_company_id: int):
        assert actor_user_id == 1 and selected_company_id == 4
        return ROLE_PERMISSIONS[role_code]
    return resolve


def _apply(items, manifest_root: Path, role_code: str, repo_root: Path = ROOT):
    return apply_plan(
        items,
        manifest_root=manifest_root,
        actor_user_id=1,
        selected_company_id=4,
        permission_resolver=_resolver(role_code),
        repo_root=repo_root,
    )


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="project-source-cli-root-"))
    manifest_root = Path(tempfile.mkdtemp(prefix="project-source-cli-manifest-"))
    results: dict[str, bool] = {}
    try:
        _git(root, "init")
        _git(root, "config", "user.email", "fixture@example.invalid")
        _git(root, "config", "user.name", "Fixture")
        source_path = root / "app/services/example.py"
        first_source = "def sample():\n    return 1\n\n\ndef other():\n    return 2\n"
        _write(source_path, first_source)
        head_v1 = _commit(root, "fixture v1")

        valid = _plan(root, head_v1)
        items = validate_plan(valid, repo_root=root, head=head_v1)
        results["normal_commit_object_approval_input"] = (
            len(items) == 1
            and items[0].source_key == "project-source:app/services/example.py#sample"
            and "return 1" in items[0].content
        )
        conflict_item = validate_plan(
            _plan(
                root, head_v1, version=9,
                conflict_group_id="policy.cli-input", conflict_confirmed=True,
            ),
            repo_root=root, head=head_v1,
        )[0]
        results["conflict_metadata_cli_normalized"] = (
            conflict_item.conflict_group_id == "policy.cli-input"
            and conflict_item.conflict_confirmed
        )
        _expect_validation(
            _plan(root, head_v1, version=10, conflict_confirmed=True), root, head_v1
        )
        results["malformed_conflict_metadata_cli_denied"] = True

        _expect_validation(_plan(root, head_v1, path="../outside.py"), root, head_v1)
        results["outside_path_denied"] = True
        _expect_validation(_plan(root, head_v1, path="app/services/missing.py"), root, head_v1)
        results["missing_path_denied"] = True
        _expect_validation(_plan(root, head_v1, symbol="missing_symbol"), root, head_v1)
        results["missing_symbol_denied"] = True
        _expect_validation(_plan(root, "b" * 40), root, head_v1)
        results["head_mismatch_denied"] = True
        _expect_validation(_plan(root, "short"), root, head_v1)
        results["malformed_revision_denied"] = True

        _write(root / "app/services/untracked.py", "def sample():\n    return 1\n")
        _expect_validation(_plan(root, head_v1, path="app/services/untracked.py"), root, head_v1)
        results["untracked_source_denied"] = True

        apply_result = _apply(items, manifest_root, "SYSTEM_ADMIN", root)
        results["normal_apply_and_approve"] = apply_result[0]["approval_status"] == "APPROVED"
        before = (manifest_root / "manifest.json").read_bytes()
        report = freshness_report(manifest_root=manifest_root, repo_root=root, head=head_v1)
        after = (manifest_root / "manifest.json").read_bytes()
        results["freshness_current_git_object_read_only"] = (
            report[0]["status"] == "CURRENT"
            and report[0]["worktree_matches_head"]
            and before == after
        )

        # A modified worktree cannot impersonate the named commit during approval.
        _write(source_path, "def sample():\n    return 99\n\n\ndef other():\n    return 2\n")
        _expect_validation(valid, root, head_v1)
        results["modified_worktree_validate_denied"] = True
        try:
            _apply(items, manifest_root, "SYSTEM_ADMIN", root)
        except PlanValidationError:
            results["modified_worktree_apply_denied"] = True
        else:
            results["modified_worktree_apply_denied"] = False
        stale = freshness_report(manifest_root=manifest_root, repo_root=root, head=head_v1)
        results["modified_worktree_freshness_stale"] = (
            stale[0]["status"] == "STALE" and not stale[0]["worktree_matches_head"]
        )
        _write(source_path, first_source)

        # Same version may not silently replace source metadata/content.
        changed_alias_plan = _plan(root, head_v1, search_aliases=["다른 별칭"])
        try:
            _apply(validate_plan(changed_alias_plan, repo_root=root, head=head_v1), manifest_root, "SYSTEM_ADMIN", root)
        except ValueError as exc:
            results["same_version_change_denied"] = "version already has different" in str(exc)
        else:
            results["same_version_change_denied"] = False

        second_source = "def sample():\n    return 2\n\n\ndef other():\n    return 3\n"
        _write(source_path, second_source)
        head_v2 = _commit(root, "fixture v2")
        old_head_report = freshness_report(manifest_root=manifest_root, repo_root=root, head=head_v2)
        results["head_change_stale"] = old_head_report[0]["status"] == "STALE" and old_head_report[0]["worktree_matches_head"]
        v2 = _plan(root, head_v2, version=2, search_aliases=["예제 함수 v2"])
        _apply(validate_plan(v2, repo_root=root, head=head_v2), manifest_root, "SYSTEM_ADMIN", root)
        manifest = KnowledgeDocumentRepository(root=manifest_root)._read_manifest()
        source_versions = sorted((item.version, item.status) for item in manifest if item.source_key.endswith("#sample"))
        results["new_version_supersedes_after_approval"] = source_versions == [(1, "SUPERSEDED"), (2, "ACTIVE")]

        # Service-boundary metadata must bind exactly to the artifact header and body.
        current_item = validate_plan(v2, repo_root=root, head=head_v2)[0]

        def trusted_error(
            *,
            content: str = current_item.content,
            source_key: str = current_item.source_key,
            source_revision: str = current_item.source_revision,
            source_content_hash: str = current_item.source_content_hash,
        ) -> str:
            forged_root = Path(tempfile.mkdtemp(prefix="project-source-forged-"))
            try:
                KnowledgeDocumentRepository(root=forged_root)._register_text_trusted(
                    source_name=current_item.source_name,
                    source_key=source_key,
                    content=content,
                    scope="GLOBAL",
                    version=1,
                    knowledge_classification="GENERAL",
                    source_kind="PROJECT_SOURCE",
                    source_revision=source_revision,
                    source_content_hash=source_content_hash,
                )
            except ValueError as exc:
                return str(exc)
            finally:
                shutil.rmtree(forged_root, ignore_errors=True)
            raise AssertionError("forged PROJECT_SOURCE metadata must fail closed")

        results["artifact_header_commit_mismatch_denied"] = (
            "commit" in trusted_error(
                content=current_item.content.replace(
                    f"commit: {current_item.source_revision}", "commit: " + "0" * 40
                )
            )
        )
        path_error = trusted_error(
            content=current_item.content.replace(
                "path: app/services/example.py", "path: app/services/other.py"
            )
        )
        symbol_error = trusted_error(
            content=current_item.content.replace("symbol: sample", "symbol: other")
        )
        results["artifact_header_path_symbol_source_key_mismatch_denied"] = (
            "source_key" in path_error and "source_key" in symbol_error
        )
        results["artifact_header_hash_body_mismatch_denied"] = (
            "content hash" in trusted_error(
                content=current_item.content.replace(
                    f"sha256: {current_item.source_content_hash}", "sha256: " + "0" * 64
                )
            )
        )
        results["source_content_hash_body_mismatch_denied"] = (
            "content hash" in trusted_error(source_content_hash="0" * 64)
        )
        results["artifact_invalid_line_range_denied"] = (
            "header metadata" in trusted_error(
                content=current_item.content.replace("lines: 1-2", "lines: 4-3")
            )
        )

        # Explicit ERP role mapping: system global, manager selected-company, all other listed roles deny.
        erp_global = _plan(root, head_v2, symbol="other", version=1, knowledge_classification="ERP_DB_INTERNAL")
        _apply(validate_plan(erp_global, repo_root=root, head=head_v2), manifest_root, "SYSTEM_ADMIN", root)
        erp_company = _plan(
            root, head_v2, symbol="other", version=1, knowledge_classification="ERP_DB_INTERNAL",
            scope="COMPANY", company_id=4,
        )
        _apply(validate_plan(erp_company, repo_root=root, head=head_v2), manifest_root, "SSART_MANAGER", root)
        deny_apply = True
        for role_code in ("SSART_STAFF", "WHOLESALE_MANAGER", "WHOLESALE_STAFF", "WHOLESALE_READONLY"):
            try:
                _apply(validate_plan(erp_company, repo_root=root, head=head_v2), manifest_root, role_code, root)
            except (PlanValidationError, PermissionError):
                continue
            deny_apply = False
        results["erp_non_manager_approval_denied"] = deny_apply

        repo = KnowledgeDocumentRepository(root=manifest_root)
        protected = {item.content_hash for item in repo._read_manifest() if item.knowledge_classification == "ERP_DB_INTERNAL"}
        original_read = repo._read_artifact
        reads: list[str] = []
        repo._read_artifact = lambda content_hash: (reads.append(content_hash), original_read(content_hash))[1]  # type: ignore[method-assign]
        try:
            system_packet = repo.retrieve(query="other", current_user_id=1, current_company_id=4, permission_codes=ROLE_PERMISSIONS["SYSTEM_ADMIN"], technical_detail_mode=True)
            manager_packet = repo.retrieve(query="other", current_user_id=2, current_company_id=4, permission_codes=ROLE_PERMISSIONS["SSART_MANAGER"], technical_detail_mode=True)
            before_denied = len(reads)
            denied_packets = [
                repo.retrieve(query="other", current_user_id=3, current_company_id=4, permission_codes=ROLE_PERMISSIONS[role])
                for role in ("SSART_STAFF", "WHOLESALE_MANAGER", "WHOLESALE_STAFF", "WHOLESALE_READONLY")
            ]
            denied_reads = set(reads[before_denied:])
        finally:
            repo._read_artifact = original_read  # type: ignore[method-assign]
        results["erp_system_admin_allow"] = bool(system_packet.citations)
        results["erp_ssart_manager_allow"] = bool(manager_packet.citations)
        results["erp_denied_roles_no_leak"] = (
            all(not packet.citations and not packet.text for packet in denied_packets)
            and not (protected & denied_reads)
        )

        # A deleted/renamed symbol in the next HEAD remains MISSING_SYMBOL; never auto-reconnect.
        _write(source_path, "def renamed():\n    return 3\n")
        head_v3 = _commit(root, "fixture renamed")
        missing = freshness_report(manifest_root=manifest_root, repo_root=root, head=head_v3)
        results["deleted_or_renamed_symbol_missing"] = any(row["status"] == "MISSING_SYMBOL" for row in missing)

        # A malformed historical row is visible as INVALID to reviewers and never repaired.
        manifest_path = manifest_root / "manifest.json"
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        invalid_row = dict(raw_manifest["documents"][-1])
        invalid_row["document_id"] = "invalid-fixture-row"
        invalid_row["source_revision"] = "not-a-commit"
        raw_manifest["documents"].append(invalid_row)
        manifest_path.write_text(json.dumps(raw_manifest), encoding="utf-8")
        invalid = freshness_report(manifest_root=manifest_root, repo_root=root, head=head_v3)
        results["invalid_manifest_row_reported"] = any(
            row["source_key"] == invalid_row["source_key"] and row["status"] == "INVALID" for row in invalid
        )

        ok = all(results.values())
        print(json.dumps({"ok": ok, "case_count": len(results), "results": results, "db_write_count": 0}, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(manifest_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
