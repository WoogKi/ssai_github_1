"""Dry-run-first manual approval and retirement CLI for DOCUMENT Knowledge."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any, Callable, Iterable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.knowledge_document_service import (  # noqa: E402
    DOCUMENT_RETIRED,
    KnowledgeDocumentRepository,
    SOURCE_KIND_DOCUMENT,
)
from app.services.knowledge_scope_policy import (  # noqa: E402
    KnowledgeClassification,
    can_manage_document,
    validate_document_classification,
    validate_document_scope,
)
from app.services.ssai_auth_service import connect_ssai_db, get_user_permissions  # noqa: E402


ERP_READ_PERMISSION = "KNOWLEDGE_ERP_DB_READ"
_ALLOWED_SUFFIXES = {".md", ".txt"}


class PlanValidationError(ValueError):
    pass


@dataclass(frozen=True)
class DocumentApproval:
    source_name: str
    source_key: str
    content: str
    scope: str
    company_id: int | None
    user_id: int | None
    version: int
    knowledge_classification: str
    search_aliases: tuple[str, ...]


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


def _load_plan(plan_path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanValidationError("approval plan must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list) or not payload["items"]:
        raise PlanValidationError("approval plan requires a non-empty items array")
    if not all(isinstance(item, dict) for item in payload["items"]):
        raise PlanValidationError("approval plan items must be objects")
    return payload["items"]


def _read_content_file(raw_path: Any, *, plan_path: Path) -> str:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise PlanValidationError("content_file is required")
    path = Path(raw_path.strip())
    if not path.is_absolute():
        path = plan_path.parent / path
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise PlanValidationError("content_file does not exist") from exc
    if not path.is_file() or path.suffix.lower() not in _ALLOWED_SUFFIXES:
        raise PlanValidationError("content_file must be an existing UTF-8 .md or .txt file")
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PlanValidationError("content_file must be UTF-8 text") from exc
    if not content.strip():
        raise PlanValidationError("content_file must not be empty")
    return content


def validate_plan(plan_path: Path) -> tuple[DocumentApproval, ...]:
    validated: list[DocumentApproval] = []
    seen: set[tuple[str, str, int | None, int | None, int]] = set()
    for raw in _load_plan(plan_path):
        if str(raw.get("source_kind") or "").strip().upper() != SOURCE_KIND_DOCUMENT:
            raise PlanValidationError("source_kind must be DOCUMENT")
        source_name = str(raw.get("source_name") or "").strip()
        source_key = str(raw.get("source_key") or "").strip()
        if not source_name or not source_key:
            raise PlanValidationError("source_name and source_key are required")
        scope = str(raw.get("scope") or "").strip().upper()
        company_id = _positive_int(raw.get("company_id"), field="company_id")
        user_id = _positive_int(raw.get("user_id"), field="user_id")
        try:
            version = int(raw.get("version"))
        except (TypeError, ValueError) as exc:
            raise PlanValidationError("version must be a positive integer") from exc
        if version <= 0:
            raise PlanValidationError("version must be a positive integer")
        classification = str(raw.get("knowledge_classification") or "").strip().upper()
        policy_document = {
            "scope": scope,
            "company_id": company_id,
            "user_id": user_id,
            "status": "ACTIVE",
            "knowledge_classification": classification,
            "source_kind": SOURCE_KIND_DOCUMENT,
        }
        if not validate_document_scope(policy_document).allowed:
            raise PlanValidationError("scope owner metadata is invalid")
        if not validate_document_classification(policy_document).allowed:
            raise PlanValidationError("knowledge_classification is invalid")
        key = (source_key, scope, company_id, user_id, version)
        if key in seen:
            raise PlanValidationError("approval plan has a duplicate source_key/scope/owner/version")
        seen.add(key)
        validated.append(DocumentApproval(
            source_name=source_name,
            source_key=source_key,
            content=_read_content_file(raw.get("content_file"), plan_path=plan_path),
            scope=scope,
            company_id=company_id,
            user_id=user_id,
            version=version,
            knowledge_classification=classification,
            search_aliases=_normalize_aliases(raw.get("search_aliases")),
        ))
    return tuple(validated)


def resolve_actor_permissions(*, actor_user_id: int, selected_company_id: int) -> tuple[str, ...]:
    conn = connect_ssai_db()
    try:
        return tuple(get_user_permissions(conn, user_id=actor_user_id, company_id=selected_company_id))
    finally:
        conn.close()


def _require_erp_read(item: DocumentApproval, permissions: Iterable[str]) -> None:
    if item.knowledge_classification == KnowledgeClassification.ERP_DB_INTERNAL and ERP_READ_PERMISSION not in set(permissions):
        raise PermissionError("ERP_DB_INTERNAL approval or retirement requires KNOWLEDGE_ERP_DB_READ")


def _audit(source: Any, *, created: bool | None = None, retired: bool | None = None) -> dict[str, Any]:
    result = {
        "document_id": source.document_id,
        "source_key": source.source_key,
        "source_kind": source.source_kind,
        "scope": source.scope,
        "company_id": source.company_id,
        "user_id": source.user_id,
        "version": source.version,
        "knowledge_classification": source.knowledge_classification,
        "approval_status": source.approval_status,
        "status": source.status,
    }
    if created is not None:
        result["created"] = created
    if retired is not None:
        result["retired"] = retired
    return result


def apply_plan(
    items: tuple[DocumentApproval, ...],
    *,
    manifest_root: Path,
    actor_user_id: int,
    selected_company_id: int,
    permission_resolver: Callable[..., Iterable[str]] = resolve_actor_permissions,
) -> list[dict[str, Any]]:
    if len(items) != 1:
        raise PlanValidationError("apply accepts exactly one DOCUMENT item per execution")
    item = items[0]
    permissions = tuple(permission_resolver(actor_user_id=actor_user_id, selected_company_id=selected_company_id))
    _require_erp_read(item, permissions)
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
        source_kind=SOURCE_KIND_DOCUMENT,
        search_aliases=item.search_aliases,
        current_company_id=selected_company_id,
        permission_codes=permissions,
    )
    approved = repo.approve_checked(
        document_id=source.document_id,
        current_company_id=selected_company_id,
        permission_codes=permissions,
    )
    return [_audit(approved, created=created)]


def preview_retire(*, manifest_root: Path, document_id: str, version: int) -> Any:
    repo = KnowledgeDocumentRepository(root=manifest_root)
    target = repo._find_document(repo._read_manifest(), document_id)
    if target.source_kind != SOURCE_KIND_DOCUMENT:
        raise PlanValidationError("retire CLI accepts DOCUMENT only")
    if target.version != version:
        raise PlanValidationError("document_id/version does not identify one exact document")
    return target


def authorize_retire(
    *,
    manifest_root: Path,
    document_id: str,
    version: int,
    actor_user_id: int,
    selected_company_id: int,
    permission_resolver: Callable[..., Iterable[str]] = resolve_actor_permissions,
) -> tuple[Any, tuple[str, ...]]:
    permissions = tuple(permission_resolver(actor_user_id=actor_user_id, selected_company_id=selected_company_id))
    target = preview_retire(manifest_root=manifest_root, document_id=document_id, version=version)
    _require_erp_read(DocumentApproval(
        source_name=target.source_name, source_key=target.source_key, content="", scope=target.scope,
        company_id=target.company_id, user_id=target.user_id, version=target.version,
        knowledge_classification=target.knowledge_classification, search_aliases=target.search_aliases,
    ), permissions)
    decision = can_manage_document(
        document=target.policy_document(),
        current_company_id=selected_company_id,
        permission_codes=permissions,
    )
    if not decision.allowed:
        raise PermissionError(f"DOCUMENT retirement denied: {decision.reason_code}")
    return target, permissions


def retire_document(
    *,
    manifest_root: Path,
    document_id: str,
    version: int,
    actor_user_id: int,
    selected_company_id: int,
    permission_resolver: Callable[..., Iterable[str]] = resolve_actor_permissions,
) -> dict[str, Any]:
    _, permissions = authorize_retire(
        manifest_root=manifest_root, document_id=document_id, version=version,
        actor_user_id=actor_user_id, selected_company_id=selected_company_id,
        permission_resolver=permission_resolver,
    )
    retired, changed = KnowledgeDocumentRepository(root=manifest_root).retire_checked(
        document_id=document_id,
        version=version,
        current_company_id=selected_company_id,
        permission_codes=permissions,
    )
    return _audit(retired, retired=changed)

def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="Manual DOCUMENT Knowledge approval and retirement (dry-run first).")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Validate a human-authored DOCUMENT plan without writing.")
    validate.add_argument("--plan", type=Path, required=True)
    apply = commands.add_parser("apply", help="Register and approve one validated DOCUMENT item.")
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--manifest-root", type=Path, required=True)
    apply.add_argument("--actor-user-id", type=int, required=True)
    apply.add_argument("--selected-company-id", type=int, required=True)
    retire = commands.add_parser("retire", help="Preview, then optionally retire one exact DOCUMENT version.")
    retire.add_argument("--manifest-root", type=Path, required=True)
    retire.add_argument("--document-id", required=True)
    retire.add_argument("--version", type=int, required=True)
    retire.add_argument("--actor-user-id", type=int, required=True)
    retire.add_argument("--selected-company-id", type=int, required=True)
    retire.add_argument("--apply", action="store_true", help="Perform the retirement after the default preview was reviewed.")
    args = parser.parse_args()
    try:
        if args.command == "validate":
            items = validate_plan(args.plan)
            _print({"mode": "validate", "write_count": 0, "items": [
                {"source_key": item.source_key, "scope": item.scope, "company_id": item.company_id,
                 "user_id": item.user_id, "version": item.version,
                 "knowledge_classification": item.knowledge_classification,
                 "search_aliases": list(item.search_aliases)} for item in items
            ]})
        elif args.command == "apply":
            actor = _positive_int(args.actor_user_id, field="actor_user_id", required=True)
            company = _positive_int(args.selected_company_id, field="selected_company_id", required=True)
            _print({"mode": "apply", "items": apply_plan(
                validate_plan(args.plan), manifest_root=args.manifest_root,
                actor_user_id=actor, selected_company_id=company,
            )})
        else:
            actor = _positive_int(args.actor_user_id, field="actor_user_id", required=True)
            company = _positive_int(args.selected_company_id, field="selected_company_id", required=True)
            preview, _ = authorize_retire(
                manifest_root=args.manifest_root, document_id=str(args.document_id), version=int(args.version),
                actor_user_id=actor, selected_company_id=company,
            )
            if not args.apply:
                _print({"mode": "retire_preview", "write_count": 0, "item": _audit(preview)})
            else:
                _print({"mode": "retire_apply", "item": retire_document(
                    manifest_root=args.manifest_root, document_id=str(args.document_id), version=int(args.version),
                    actor_user_id=actor, selected_company_id=company,
                )})
    except (PlanValidationError, PermissionError, ValueError, OSError) as exc:
        _print({"ok": False, "error_type": type(exc).__name__, "reason": str(exc)})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
