"""Manual, fail-closed Project Source Knowledge approval and freshness CLI.

This tool approves only a human-selected ``path#symbol`` from a named Git
commit. It never auto-discovers, auto-approves, or bulk-indexes source.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.knowledge_document_service import (  # noqa: E402
    KnowledgeDocumentRepository,
    SOURCE_KIND_PROJECT_SOURCE,
    normalize_conflict_metadata,
)
from app.services.knowledge_scope_policy import (  # noqa: E402
    KnowledgeClassification,
    validate_document_classification,
    validate_document_scope,
)
from app.services.ssai_auth_service import connect_ssai_db, get_user_permissions  # noqa: E402


_REVISION = re.compile(r"^[0-9a-f]{40}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_PREFIX = "project-source:"
ERP_READ_PERMISSION = "KNOWLEDGE_ERP_DB_READ"
WORKTREE_MISMATCH_MESSAGE = "승인 대상 source가 commit revision과 다릅니다. 먼저 commit 또는 변경 취소 후 다시 검증하세요."


class PlanValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectSourceApproval:
    relative_path: str
    symbol: str
    source_revision: str
    source_content_hash: str
    content: str
    source_key: str
    source_name: str
    scope: str
    company_id: int | None
    user_id: int | None
    version: int
    knowledge_classification: str
    search_aliases: tuple[str, ...]
    conflict_group_id: str
    conflict_confirmed: bool


def _git(repo_root: Path, args: list[str], *, text: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo_root, check=True, capture_output=True, text=text
    )


def current_head(repo_root: Path = ROOT) -> str:
    revision = _git(repo_root, ["rev-parse", "HEAD"], text=True).stdout.strip().lower()
    if not _REVISION.fullmatch(revision):
        raise PlanValidationError("current repository HEAD is not a full commit hash")
    return revision


def _positive_int(value: Any, *, field: str, required: bool = False) -> int | None:
    if value is None:
        if required:
            raise PlanValidationError(f"{field} is required")
        return None
    if isinstance(value, bool):
        raise PlanValidationError(f"{field} must be a positive integer")
    text = str(value).strip()
    if not text.isdigit() or int(text) <= 0:
        raise PlanValidationError(f"{field} must be a positive integer")
    return int(text)


def _normalize_aliases(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise PlanValidationError("search_aliases must be a JSON array of strings")
    aliases: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            raise PlanValidationError("search_aliases entries must be strings")
        alias = " ".join(raw.split())
        if not alias:
            continue
        if len(alias) > 160:
            raise PlanValidationError("search alias is too long")
        if alias not in aliases:
            aliases.append(alias)
    return tuple(aliases)


def _safe_relative_path(
    raw: Any,
    *,
    repo_root: Path,
    require_worktree_file: bool,
) -> tuple[str, Path]:
    if not isinstance(raw, str) or not raw.strip():
        raise PlanValidationError("path is required")
    text = raw.strip().replace("\\", "/")
    relative = PurePosixPath(text)
    if relative.is_absolute() or ".." in relative.parts or text.startswith("/"):
        raise PlanValidationError("path must be repository-relative")
    candidate = (repo_root / Path(*relative.parts)).resolve()
    try:
        resolved_relative = candidate.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise PlanValidationError("path resolves outside repository") from exc
    if candidate.suffix.lower() != ".py":
        raise PlanValidationError("project source path must be a Python file")
    if require_worktree_file and not candidate.is_file():
        raise PlanValidationError(WORKTREE_MISMATCH_MESSAGE)
    return resolved_relative.as_posix(), candidate


def _git_show_source(*, repo_root: Path, revision: str, relative_path: str) -> str:
    try:
        raw = _git(repo_root, ["show", "--no-textconv", f"{revision}:{relative_path}"]).stdout
        return raw.decode("utf-8")
    except (subprocess.CalledProcessError, UnicodeDecodeError) as exc:
        raise PlanValidationError("commit revision에 승인 대상 source가 없습니다.") from exc


def _worktree_matches_revision(*, repo_root: Path, revision: str, relative_path: str) -> bool:
    candidate = repo_root / relative_path
    if not candidate.is_file():
        return False
    try:
        tracked = _git(repo_root, ["ls-files", "--error-unmatch", "--", relative_path])
        if not tracked.stdout.strip():
            return False
        status = _git(repo_root, ["status", "--porcelain=v1", "--untracked-files=all", "--", relative_path], text=True)
        return status.stdout == ""
    except subprocess.CalledProcessError:
        return False


def _find_symbol(tree: ast.Module, symbol: str) -> ast.AST:
    parts = symbol.split(".")
    if len(parts) == 1:
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol:
                return node
    elif len(parts) == 2:
        class_name, method_name = parts
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method_name:
                        return child
    raise PlanValidationError("selected source symbol was not found")


def extract_project_symbol(
    *,
    source_text: str,
    relative_path: str,
    symbol: str,
    revision: str,
) -> tuple[str, str]:
    try:
        tree = ast.parse(source_text, filename=relative_path)
    except SyntaxError as exc:
        raise PlanValidationError("commit revision source cannot be parsed") from exc
    if not isinstance(symbol, str) or not symbol.strip():
        raise PlanValidationError("symbol is required")
    node = _find_symbol(tree, symbol.strip())
    decorators = getattr(node, "decorator_list", ())
    start_line = min([getattr(node, "lineno", 1), *(getattr(item, "lineno", 1) for item in decorators)])
    end_line = int(getattr(node, "end_lineno", start_line))
    lines = source_text.splitlines()
    symbol_text = "\n".join(lines[start_line - 1 : end_line]).rstrip()
    if not symbol_text:
        raise PlanValidationError("selected source symbol is empty")
    source_hash = hashlib.sha256(symbol_text.encode("utf-8")).hexdigest()
    rendered = (
        f"# path: {relative_path} | symbol: {symbol.strip()} | lines: {start_line}-{end_line} "
        f"| commit: {revision} | sha256: {source_hash}\n\n"
        f"```python\n{symbol_text}\n```"
    )
    return rendered, source_hash


def _load_plan(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanValidationError("approval plan must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list) or not payload["items"]:
        raise PlanValidationError("approval plan requires a non-empty items array")
    if not all(isinstance(item, dict) for item in payload["items"]):
        raise PlanValidationError("approval plan items must be objects")
    return payload["items"]


def validate_plan(
    plan_path: Path,
    *,
    repo_root: Path = ROOT,
    head: str | None = None,
) -> tuple[ProjectSourceApproval, ...]:
    revision = (head or current_head(repo_root)).lower()
    if not _REVISION.fullmatch(revision):
        raise PlanValidationError("current HEAD must be a full 40-character commit hash")
    validated: list[ProjectSourceApproval] = []
    seen_keys: set[tuple[str, str, int]] = set()
    for raw in _load_plan(plan_path):
        if str(raw.get("source_kind") or "").strip().upper() != SOURCE_KIND_PROJECT_SOURCE:
            raise PlanValidationError("source_kind must be PROJECT_SOURCE")
        source_revision = str(raw.get("source_revision") or "").strip().lower()
        if not _REVISION.fullmatch(source_revision):
            raise PlanValidationError("source_revision must be a full 40-character commit hash")
        if source_revision != revision:
            raise PlanValidationError("source_revision does not match current HEAD")
        relative_path, _ = _safe_relative_path(
            raw.get("path"), repo_root=repo_root, require_worktree_file=True
        )
        if not _worktree_matches_revision(
            repo_root=repo_root, revision=revision, relative_path=relative_path
        ):
            raise PlanValidationError(WORKTREE_MISMATCH_MESSAGE)
        symbol = str(raw.get("symbol") or "").strip()
        source_text = _git_show_source(
            repo_root=repo_root, revision=source_revision, relative_path=relative_path
        )
        rendered, source_hash = extract_project_symbol(
            source_text=source_text,
            relative_path=relative_path,
            symbol=symbol,
            revision=source_revision,
        )
        scope = str(raw.get("scope") or "").strip().upper()
        company_id = _positive_int(raw.get("company_id"), field="company_id")
        user_id = _positive_int(raw.get("user_id"), field="user_id")
        try:
            version = int(raw.get("version"))
        except (TypeError, ValueError) as exc:
            raise PlanValidationError("version must be a positive integer") from exc
        if version <= 0:
            raise PlanValidationError("version must be a positive integer")
        try:
            conflict_group_id, conflict_confirmed = normalize_conflict_metadata(
                raw.get("conflict_group_id", ""), raw.get("conflict_confirmed", False)
            )
        except ValueError as exc:
            raise PlanValidationError(str(exc)) from exc
        classification = str(raw.get("knowledge_classification") or "").strip().upper()
        policy_document = {
            "scope": scope,
            "company_id": company_id,
            "user_id": user_id,
            "status": "ACTIVE",
            "knowledge_classification": classification,
        }
        if not validate_document_scope(policy_document).allowed:
            raise PlanValidationError("scope owner metadata is invalid")
        if not validate_document_classification(policy_document).allowed:
            raise PlanValidationError("knowledge_classification is invalid")
        source_key = f"{_SOURCE_PREFIX}{relative_path}#{symbol}"
        key = (source_key, scope, version)
        if key in seen_keys:
            raise PlanValidationError("approval plan has a duplicate source_key/scope/version")
        seen_keys.add(key)
        validated.append(
            ProjectSourceApproval(
                relative_path=relative_path,
                symbol=symbol,
                source_revision=source_revision,
                source_content_hash=source_hash,
                content=rendered,
                source_key=source_key,
                source_name=f"project_source_{hashlib.sha256(source_key.encode('utf-8')).hexdigest()[:16]}.py",
                scope=scope,
                company_id=company_id,
                user_id=user_id,
                version=version,
                knowledge_classification=classification,
                search_aliases=_normalize_aliases(raw.get("search_aliases")),
                conflict_group_id=conflict_group_id,
                conflict_confirmed=conflict_confirmed,
            )
        )
    return tuple(validated)


def resolve_actor_permissions(*, actor_user_id: int, selected_company_id: int) -> tuple[str, ...]:
    conn = connect_ssai_db()
    try:
        return tuple(get_user_permissions(conn, user_id=actor_user_id, company_id=selected_company_id))
    finally:
        conn.close()


def apply_plan(
    items: tuple[ProjectSourceApproval, ...],
    *,
    manifest_root: Path,
    actor_user_id: int,
    selected_company_id: int,
    permission_resolver: Callable[..., Iterable[str]] = resolve_actor_permissions,
    repo_root: Path = ROOT,
) -> list[dict[str, Any]]:
    if len(items) != 1:
        raise PlanValidationError("--apply accepts exactly one approved path#symbol item per execution")
    permissions = tuple(permission_resolver(actor_user_id=actor_user_id, selected_company_id=selected_company_id))
    item = items[0]
    if item.source_revision != current_head(repo_root) or not _worktree_matches_revision(
        repo_root=repo_root,
        revision=item.source_revision,
        relative_path=item.relative_path,
    ):
        raise PlanValidationError(WORKTREE_MISMATCH_MESSAGE)
    source_text = _git_show_source(
        repo_root=repo_root,
        revision=item.source_revision,
        relative_path=item.relative_path,
    )
    rendered, source_hash = extract_project_symbol(
        source_text=source_text,
        relative_path=item.relative_path,
        symbol=item.symbol,
        revision=item.source_revision,
    )
    if item.source_content_hash != source_hash or item.content != rendered:
        raise PlanValidationError("승인 계획의 source artifact가 commit revision과 다릅니다.")
    if item.knowledge_classification == KnowledgeClassification.ERP_DB_INTERNAL and ERP_READ_PERMISSION not in permissions:
        raise PlanValidationError("ERP_DB_INTERNAL approval requires KNOWLEDGE_ERP_DB_READ")
    repo = KnowledgeDocumentRepository(root=manifest_root)
    source, created = repo.register_text_checked(
        source_name=item.source_name,
        source_key=item.source_key,
        content=item.content,
        scope=item.scope,
        company_id=item.company_id,
        user_id=item.user_id,
        version=item.version,
        knowledge_classification=item.knowledge_classification,
        source_kind=SOURCE_KIND_PROJECT_SOURCE,
        source_revision=item.source_revision,
        source_content_hash=item.source_content_hash,
        search_aliases=item.search_aliases,
        conflict_group_id=item.conflict_group_id,
        conflict_confirmed=item.conflict_confirmed,
        current_company_id=selected_company_id,
        permission_codes=permissions,
    )
    approved = repo.approve_checked(
        document_id=source.document_id,
        current_company_id=selected_company_id,
        permission_codes=permissions,
    )
    return [{
        "source_key": approved.source_key,
        "version": approved.version,
        "created": created,
        "approval_status": approved.approval_status,
        "status": approved.status,
    }]


def _parse_source_key(source_key: str) -> tuple[str, str]:
    if not source_key.startswith(_SOURCE_PREFIX):
        raise PlanValidationError("invalid PROJECT_SOURCE source_key")
    raw = source_key[len(_SOURCE_PREFIX):]
    relative_path, delimiter, symbol = raw.rpartition("#")
    if delimiter != "#" or not relative_path or not symbol:
        raise PlanValidationError("invalid PROJECT_SOURCE source_key")
    return relative_path, symbol


def freshness_report(*, manifest_root: Path, repo_root: Path = ROOT, head: str | None = None) -> list[dict[str, Any]]:
    """Read PROJECT_SOURCE freshness without repairing a malformed manifest row."""
    current_revision = (head or current_head(repo_root)).lower()
    manifest_path = manifest_root / "manifest.json"
    if not manifest_path.exists():
        return []
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        documents = payload.get("documents")
        if not isinstance(documents, list):
            raise ValueError("documents is not a list")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PlanValidationError("Knowledge manifest cannot be read for freshness") from exc

    rows: list[dict[str, Any]] = []
    for raw in documents:
        if not isinstance(raw, dict):
            continue
        source_key = str(raw.get("source_key") or "")
        source_kind = str(raw.get("source_kind") or "").strip().upper()
        if source_kind != SOURCE_KIND_PROJECT_SOURCE and not source_key.startswith(_SOURCE_PREFIX):
            continue
        revision = str(raw.get("source_revision") or "").strip().lower()
        content_hash = str(raw.get("source_content_hash") or "").strip().lower()
        row: dict[str, Any] = {
            "source_key": source_key,
            "version": raw.get("version"),
            "approved_source_revision": revision,
            "current_head": current_revision,
            "current_source_content_hash_matches": False,
            "worktree_matches_head": False,
            "status": "INVALID",
        }
        try:
            if source_kind != SOURCE_KIND_PROJECT_SOURCE:
                raise PlanValidationError("invalid PROJECT_SOURCE kind")
            if not _REVISION.fullmatch(revision) or not _HASH.fullmatch(content_hash):
                raise PlanValidationError("invalid PROJECT_SOURCE freshness metadata")
            relative_path, _ = _safe_relative_path(
                _parse_source_key(source_key)[0], repo_root=repo_root, require_worktree_file=False
            )
            symbol = _parse_source_key(source_key)[1]
            source_text = _git_show_source(
                repo_root=repo_root, revision=current_revision, relative_path=relative_path
            )
            _, current_hash = extract_project_symbol(
                source_text=source_text,
                relative_path=relative_path,
                symbol=symbol,
                revision=current_revision,
            )
            row["current_source_content_hash_matches"] = current_hash == content_hash
            row["worktree_matches_head"] = _worktree_matches_revision(
                repo_root=repo_root, revision=current_revision, relative_path=relative_path
            )
            row["status"] = (
                "CURRENT"
                if revision == current_revision
                and current_hash == content_hash
                and row["worktree_matches_head"]
                else "STALE"
            )
        except PlanValidationError as exc:
            message = str(exc)
            row["status"] = "MISSING_SYMBOL" if "source" in message or "symbol" in message or "path" in message else "INVALID"
        rows.append(row)
    return rows


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="Manual Project Source Knowledge approval and freshness report.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate", help="Validate a human-authored approval plan without writing a manifest.")
    validate_parser.add_argument("--plan", type=Path, required=True)
    apply_parser = subparsers.add_parser("apply", help="Register and approve exactly one validated source symbol.")
    apply_parser.add_argument("--plan", type=Path, required=True)
    apply_parser.add_argument("--manifest-root", type=Path, required=True)
    apply_parser.add_argument("--actor-user-id", type=int, required=True)
    apply_parser.add_argument("--selected-company-id", type=int, required=True)
    freshness_parser = subparsers.add_parser("freshness", help="Read-only PROJECT_SOURCE freshness report.")
    freshness_parser.add_argument("--manifest-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "validate":
            items = validate_plan(args.plan)
            _print({"mode": "validate", "write_count": 0, "items": [
                {"source_key": item.source_key, "version": item.version, "source_revision": item.source_revision,
                 "source_content_hash": item.source_content_hash, "knowledge_classification": item.knowledge_classification,
                 "scope": item.scope, "search_aliases": list(item.search_aliases), "conflict_group_id": item.conflict_group_id, "conflict_confirmed": item.conflict_confirmed} for item in items
            ]})
        elif args.command == "apply":
            actor_user_id = _positive_int(args.actor_user_id, field="actor_user_id", required=True)
            selected_company_id = _positive_int(args.selected_company_id, field="selected_company_id", required=True)
            items = validate_plan(args.plan)
            result = apply_plan(
                items,
                manifest_root=args.manifest_root,
                actor_user_id=actor_user_id,
                selected_company_id=selected_company_id,
            )
            _print({"mode": "apply", "items": result})
        else:
            _print({"mode": "freshness", "write_count": 0, "items": freshness_report(manifest_root=args.manifest_root)})
    except (PlanValidationError, PermissionError, ValueError, OSError, subprocess.SubprocessError) as exc:
        _print({"ok": False, "error_type": type(exc).__name__, "reason": str(exc)})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
