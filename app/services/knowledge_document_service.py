"""Small, filesystem-backed Document Artifact / RAG proof of concept.

This service deliberately does not import the Streamlit attachment screen.
Only documents registered through this explicit Knowledge manifest may be
retrieved; a user's chat attachment remains a chat attachment.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import unicodedata
import uuid
from typing import Any, Iterable, Mapping

from app.services.knowledge_scope_policy import (
    KnowledgeClassification,
    can_manage_document,
    can_read_document,
    validate_document_classification,
    validate_document_scope,
)
from app.services.ssai_storage_service import get_storage_root, make_safe_filename


MANIFEST_VERSION = 1
ARTIFACT_VERSION = 1
APPROVAL_APPROVED = "APPROVED"
APPROVAL_PENDING = "PENDING"
DOCUMENT_ACTIVE = "ACTIVE"
DOCUMENT_SUPERSEDED = "SUPERSEDED"
DOCUMENT_RETIRED = "RETIRED"
SOURCE_KIND_DOCUMENT = "DOCUMENT"
SOURCE_KIND_PROJECT_SOURCE = "PROJECT_SOURCE"
_SOURCE_KINDS = {SOURCE_KIND_DOCUMENT, SOURCE_KIND_PROJECT_SOURCE}
_SOURCE_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SOURCE_CONTENT_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CONFLICT_GROUP_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_SUPPORTED_SUFFIXES = {".txt", ".md", ".markdown", ".py"}
_LEXICAL_TOKEN_PATTERN = re.compile(r"[0-9a-zA-Z가-힣]+")
_PROJECT_SOURCE_ARTIFACT_PATTERN = re.compile(
    r"^# path: (?P<path>[^|]+) \| symbol: (?P<symbol>[^|]+) \| lines: (?P<start>\d+)-(?P<end>\d+) \| commit: (?P<commit>[0-9a-f]{40}) \| sha256: (?P<hash>[0-9a-f]{64})\n\n```python\n(?P<body>.*)\n```$",
    re.DOTALL,
)


@dataclass(frozen=True)
class DocumentSource:
    document_id: str
    source_name: str
    source_key: str
    content_hash: str
    scope: str
    company_id: int | None
    user_id: int | None
    version: int
    status: str
    approval_status: str
    created_at: str
    knowledge_classification: str = KnowledgeClassification.GENERAL
    approved_at: str | None = None
    source_kind: str = SOURCE_KIND_DOCUMENT
    source_revision: str = ""
    source_content_hash: str = ""
    search_aliases: tuple[str, ...] = ()
    conflict_group_id: str = ""
    conflict_confirmed: bool = False

    def policy_document(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "company_id": self.company_id,
            "user_id": self.user_id,
            "status": self.status,
            "knowledge_classification": self.knowledge_classification,
            "source_kind": self.source_kind,
        }


@dataclass(frozen=True)
class ExtractionArtifact:
    content_hash: str
    extractor_version: int
    normalized_text: str
    sections: tuple[dict[str, str], ...]
    source_content_hash: str = ""
    extractor_kind: str = "text"


@dataclass(frozen=True)
class ContextCitation:
    document_id: str
    source_name: str
    version: int
    section_id: str
    section_title: str
    source_kind: str = SOURCE_KIND_DOCUMENT
    source_revision: str = ""
    artifact_content_hash: str = ""
    source_content_hash: str = ""
    extractor_kind: str = "text"
    extractor_version: int = ARTIFACT_VERSION
    source_location: str = ""
    ocr_used: bool = False

    @property
    def label(self) -> str:
        details = []
        if self.source_location:
            details.append(self.source_location)
        if self.extractor_kind != "text":
            details.append(f"{self.extractor_kind} v{self.extractor_version}")
        if self.ocr_used:
            details.append("OCR")
        suffix = f" · {' / '.join(details)}" if details else ""
        return f"[{self.source_name} v{self.version} §{self.section_title}{suffix}]"

    @property
    def identifier(self) -> str:
        return f"{self.document_id}:{self.section_id}"


@dataclass(frozen=True)
class ContextConflictNotice:
    conflict_group_id: str
    document_citation_ids: tuple[str, ...]
    project_source_citation_ids: tuple[str, ...]
    message: str = "공식 문서와 현재 구현 source의 근거가 다릅니다. 두 citation을 구분해 확인하세요."


@dataclass(frozen=True)
class ContextPacket:
    text: str
    citations: tuple[ContextCitation, ...]
    reason_code: str
    candidate_count: int
    conflict_notices: tuple[ContextConflictNotice, ...] = ()


@dataclass(frozen=True)
class KnowledgeChatRequestContext:
    """Immutable identity captured once before Knowledge retrieval begins."""

    user_id: int | None
    company_id: int | None
    permission_codes: tuple[str, ...] | None
    room_owner_user_id: int | None
    room_company_id: int | None
    technical_detail_mode: bool = False


@dataclass(frozen=True)
class KnowledgeChatAuthorization:
    allowed: bool
    reason_code: str
    citations: tuple[ContextCitation, ...] = ()
    conflict_notices: tuple[ContextConflictNotice, ...] = ()


@dataclass(frozen=True)
class KnowledgeEvidenceSnapshot:
    """JSON-safe evidence identity persisted beside one Knowledge answer."""

    user_id: int
    company_id: int
    room_owner_user_id: int
    room_company_id: int
    technical_detail_mode: bool
    answer_hash: str
    citations: tuple[ContextCitation, ...]
    conflict_notices: tuple[ContextConflictNotice, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "company_id": self.company_id,
            "room_owner_user_id": self.room_owner_user_id,
            "room_company_id": self.room_company_id,
            "technical_detail_mode": self.technical_detail_mode,
            "answer_hash": self.answer_hash,
            "citations": [asdict(item) for item in self.citations],
            "conflict_notices": [asdict(item) for item in self.conflict_notices],
        }


class KnowledgeManagementDenied(PermissionError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = str(reason_code or "knowledge_management_denied")
        super().__init__(f"Knowledge management denied: {self.reason_code}")


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _positive_id(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    text = str(value).strip()
    if not text or not text.isdigit() or int(text) <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return int(text)


def _chat_context_positive_id(value: Any) -> int | None:
    try:
        return _positive_id(value, field="chat context id")
    except ValueError:
        return None


def _normalize_permission_codes(values: Iterable[str] | None) -> tuple[str, ...] | None:
    if values is None or isinstance(values, (str, bytes)):
        return None
    try:
        normalized = tuple(
            dict.fromkeys(
                str(value).strip().upper()
                for value in values
                if isinstance(value, str) and str(value).strip()
            )
        )
    except TypeError:
        return None
    return normalized


def build_knowledge_chat_request_context(
    *,
    user_id: Any,
    company_id: Any,
    permission_codes: Iterable[str] | None,
    room_owner_user_id: Any,
    room_company_id: Any,
    technical_detail_mode: Any = False,
) -> KnowledgeChatRequestContext:
    """Capture one request identity without consulting mutable session state again."""
    return KnowledgeChatRequestContext(
        user_id=_chat_context_positive_id(user_id),
        company_id=_chat_context_positive_id(company_id),
        permission_codes=_normalize_permission_codes(permission_codes),
        room_owner_user_id=_chat_context_positive_id(room_owner_user_id),
        room_company_id=_chat_context_positive_id(room_company_id),
        technical_detail_mode=technical_detail_mode if isinstance(technical_detail_mode, bool) else False,
    )


def authorize_knowledge_chat_request(context: KnowledgeChatRequestContext) -> KnowledgeChatAuthorization:
    """Fail closed before retrieval or an LLM context can be constructed."""
    if not isinstance(context, KnowledgeChatRequestContext):
        return KnowledgeChatAuthorization(False, "invalid_request_context")
    if not isinstance(context.technical_detail_mode, bool):
        return KnowledgeChatAuthorization(False, "invalid_technical_detail_mode")
    if not context.user_id or not context.company_id:
        return KnowledgeChatAuthorization(False, "missing_current_user_or_company")
    if not context.room_owner_user_id or not context.room_company_id:
        return KnowledgeChatAuthorization(False, "missing_room_owner_or_company")
    if context.user_id != context.room_owner_user_id:
        return KnowledgeChatAuthorization(False, "room_owner_user_mismatch")
    if context.company_id != context.room_company_id:
        return KnowledgeChatAuthorization(False, "room_company_mismatch")
    if context.permission_codes is None:
        return KnowledgeChatAuthorization(False, "missing_current_permissions")
    if "RAG_USE" not in context.permission_codes:
        return KnowledgeChatAuthorization(False, "missing_rag_use")
    return KnowledgeChatAuthorization(True, "ready")


def _normalize_source_kind(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    if normalized not in _SOURCE_KINDS:
        raise ValueError("invalid Knowledge source kind")
    return normalized


def _normalize_source_revision(value: Any, *, source_kind: str) -> str:
    revision = str(value or "").strip().lower()
    if source_kind == SOURCE_KIND_DOCUMENT:
        if revision:
            raise ValueError("document Knowledge source revision must be blank")
        return ""
    if not _SOURCE_REVISION_PATTERN.fullmatch(revision):
        raise ValueError("project source revision must be a full commit hash")
    return revision

def _normalize_source_content_hash(value: Any, *, source_kind: str) -> str:
    content_hash = str(value or "").strip().lower()
    if source_kind == SOURCE_KIND_DOCUMENT:
        if content_hash:
            raise ValueError("document Knowledge source content hash must be blank")
        return ""
    if not _SOURCE_CONTENT_HASH_PATTERN.fullmatch(content_hash):
        raise ValueError("project source content hash must be a SHA-256 hash")
    return content_hash


def normalize_conflict_metadata(
    conflict_group_id: Any,
    conflict_confirmed: Any,
) -> tuple[str, bool]:
    if not isinstance(conflict_group_id, str):
        raise ValueError("conflict_group_id must be a string")
    group_id = conflict_group_id.strip().lower()
    if group_id and not _CONFLICT_GROUP_ID_PATTERN.fullmatch(group_id):
        raise ValueError("conflict_group_id is invalid")
    if not isinstance(conflict_confirmed, bool):
        raise ValueError("conflict_confirmed must be a boolean")
    if conflict_confirmed and not group_id:
        raise ValueError("conflict_confirmed requires conflict_group_id")
    return group_id, conflict_confirmed


def _normalize_search_aliases(values: Iterable[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        raise ValueError("Knowledge search aliases must be an iterable of strings")
    aliases: list[str] = []
    for raw in values:
        if not isinstance(raw, str):
            raise ValueError("Knowledge search alias must be a string")
        alias = _normalize_text(raw).strip()
        if not alias:
            continue
        if len(alias) > 160:
            raise ValueError("Knowledge search alias is too long")
        if alias not in aliases:
            aliases.append(alias)
    return tuple(aliases)

def _normalize_text(raw: str) -> str:
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.strip()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _lexical_parts(value: str) -> tuple[tuple[str, ...], str]:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    tokens = tuple(_LEXICAL_TOKEN_PATTERN.findall(normalized))
    return tokens, "".join(tokens)


def _sectionize(text: str) -> tuple[dict[str, str], ...]:
    """Keep Markdown headings as provenance boundaries without parsing tables."""
    parts: list[dict[str, str]] = []
    # Artifacts dedupe by content hash across scopes.  The source name belongs
    # only to its manifest/citation, never to the shared artifact payload.
    title = "본문"
    body: list[str] = []

    def flush() -> None:
        normalized = "\n".join(body).strip()
        if normalized:
            parts.append({"section_id": f"S{len(parts) + 1}", "title": title, "text": normalized})

    for line in text.split("\n"):
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if heading:
            flush()
            body.clear()
            title = heading.group(1).strip() or title
        else:
            body.append(line)
    flush()
    return tuple(parts)


def build_extraction_artifact(
    *,
    sections: Iterable[Mapping[str, Any]],
    extractor_kind: str,
    source_content_hash: str = "",
) -> ExtractionArtifact:
    """Build provenance from already extracted text without opening the source."""
    kind = str(extractor_kind or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,79}", kind):
        raise ValueError("Knowledge extractor kind is invalid")
    raw_hash = str(source_content_hash or "").strip().lower()
    if raw_hash and not _SOURCE_CONTENT_HASH_PATTERN.fullmatch(raw_hash):
        raise ValueError("Knowledge source content hash is invalid")

    normalized_sections: list[dict[str, str]] = []
    for index, raw in enumerate(sections, start=1):
        text = _normalize_text(str(raw.get("text") or ""))
        if not text:
            continue
        section_id = str(raw.get("section_id") or f"S{index}").strip()
        title = str(raw.get("title") or "본문").strip()
        if not section_id or not title:
            raise ValueError("Knowledge artifact section metadata is invalid")
        section = {"section_id": section_id, "title": title, "text": text}
        for key in ("location_type", "location_label", "page", "paragraph", "table", "row", "ocr_used"):
            value = raw.get(key)
            if value is not None and str(value).strip():
                section[key] = str(value).strip()
        normalized_sections.append(section)
    if not normalized_sections:
        raise ValueError("Knowledge source is empty")
    normalized_text = _normalize_text("\n\n".join(section["text"] for section in normalized_sections))
    return ExtractionArtifact(
        content_hash=_sha256(normalized_text),
        extractor_version=ARTIFACT_VERSION,
        normalized_text=normalized_text,
        sections=tuple(normalized_sections),
        source_content_hash=raw_hash,
        extractor_kind=kind,
    )


def _validate_project_source_artifact(
    *,
    artifact: ExtractionArtifact,
    source_key: str,
    source_revision: str,
    source_content_hash: str,
) -> None:
    """Bind PROJECT_SOURCE request metadata to its fenced symbol provenance."""
    match = _PROJECT_SOURCE_ARTIFACT_PATTERN.fullmatch(artifact.normalized_text)
    if match is None:
        raise ValueError("project source artifact provenance is invalid")

    header_path = match.group("path").strip()
    header_symbol = match.group("symbol").strip()
    header_commit = match.group("commit")
    header_hash = match.group("hash")
    start_line = int(match.group("start"))
    end_line = int(match.group("end"))
    if not header_path or not header_symbol or start_line <= 0 or end_line < start_line:
        raise ValueError("project source artifact header metadata is invalid")
    if source_key != f"project-source:{header_path}#{header_symbol}":
        raise ValueError("project source artifact source_key does not match header")
    if header_commit != source_revision:
        raise ValueError("project source artifact commit does not match source_revision")

    body_hash = _sha256(match.group("body"))
    if body_hash != header_hash or body_hash != source_content_hash:
        raise ValueError("project source content hash does not match artifact symbol")


def extract_text_artifact(*, source_name: str, content: str | bytes) -> ExtractionArtifact:
    """Create a full-text TXT/Markdown artifact, never a UI preview string."""
    suffix = Path(source_name).suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        raise ValueError("only TXT, Markdown, or approved Python source representations are supported")
    if isinstance(content, bytes):
        for encoding in ("utf-8-sig", "cp949"):
            try:
                content = content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("text source encoding is unsupported")
    normalized = _normalize_text(content)
    if not normalized:
        raise ValueError("Knowledge source is empty")
    return ExtractionArtifact(
        content_hash=_sha256(normalized),
        extractor_version=ARTIFACT_VERSION,
        normalized_text=normalized,
        sections=_sectionize(normalized),
    )


class KnowledgeDocumentRepository:
    """Manifest and immutable text artifacts under the configured shared storage root."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        source_repo_root: Path | None = None,
    ) -> None:
        self._root = Path(root) if root is not None else get_storage_root() / "knowledge_poc"
        self._source_repo_root = Path(source_repo_root).resolve() if source_repo_root is not None else None

    def _project_source_is_current(self, source: DocumentSource) -> bool:
        """Fail closed for persisted PROJECT_SOURCE evidence without artifact IO."""
        if source.source_kind != SOURCE_KIND_PROJECT_SOURCE or self._source_repo_root is None:
            return False
        prefix = "project-source:"
        if not source.source_key.startswith(prefix):
            return False
        relative_path, separator, symbol = source.source_key[len(prefix):].rpartition("#")
        path = Path(*relative_path.split("/"))
        if (
            separator != "#"
            or not symbol
            or not relative_path
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            return False
        try:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self._source_repo_root,
                capture_output=True,
                check=True,
                timeout=5,
            ).stdout.decode("ascii").strip().lower()
            if head != source.source_revision:
                return False
            subprocess.run(
                ["git", "show", f"HEAD:{relative_path}"],
                cwd=self._source_repo_root,
                capture_output=True,
                check=True,
                timeout=5,
            )
            worktree_path = (self._source_repo_root / path).resolve()
            if self._source_repo_root not in worktree_path.parents or not worktree_path.is_file():
                return False
            return subprocess.run(
                ["git", "diff", "--quiet", "HEAD", "--", relative_path],
                cwd=self._source_repo_root,
                capture_output=True,
                timeout=5,
            ).returncode == 0
        except (OSError, subprocess.SubprocessError, UnicodeError):
            return False

    @property
    def manifest_path(self) -> Path:
        return self._root / "manifest.json"

    @property
    def artifact_dir(self) -> Path:
        return self._root / "artifacts"

    def _read_manifest(self) -> list[DocumentSource]:
        if not self.manifest_path.exists():
            return []
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if int(payload.get("manifest_version") or 0) != MANIFEST_VERSION:
                raise ValueError("manifest version mismatch")
            documents: list[DocumentSource] = []
            for row in payload.get("documents", []):
                normalized = dict(row)
                normalized["source_kind"] = _normalize_source_kind(normalized.get("source_kind", SOURCE_KIND_DOCUMENT))
                normalized["source_revision"] = _normalize_source_revision(
                    normalized.get("source_revision", ""), source_kind=normalized["source_kind"]
                )
                normalized["source_content_hash"] = _normalize_source_content_hash(
                    normalized.get("source_content_hash", ""), source_kind=normalized["source_kind"]
                )
                normalized["search_aliases"] = _normalize_search_aliases(normalized.get("search_aliases"))
                (
                    normalized["conflict_group_id"],
                    normalized["conflict_confirmed"],
                ) = normalize_conflict_metadata(
                    normalized.get("conflict_group_id", ""),
                    normalized.get("conflict_confirmed", False),
                )
                documents.append(DocumentSource(**normalized))
            return documents
        except Exception as exc:
            raise ValueError("Knowledge manifest is corrupt") from exc

    @staticmethod
    def _atomic_json_write(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            temp.replace(path)
        finally:
            if temp.exists():
                temp.unlink(missing_ok=True)

    def _write_manifest(self, documents: Iterable[DocumentSource]) -> None:
        self._atomic_json_write(
            self.manifest_path,
            {"manifest_version": MANIFEST_VERSION, "documents": [asdict(item) for item in documents]},
        )

    def _artifact_path(self, content_hash: str) -> Path:
        return self.artifact_dir / f"{content_hash}.json"

    def _write_artifact_once(self, artifact: ExtractionArtifact) -> None:
        path = self._artifact_path(artifact.content_hash)
        if path.exists():
            return
        self._atomic_json_write(path, asdict(artifact))

    def register_text_checked(
        self,
        *,
        source_name: str,
        source_key: str,
        content: str | bytes,
        scope: str,
        current_company_id: int | None,
        permission_codes: Iterable[str] | None,
        company_id: int | None = None,
        user_id: int | None = None,
        version: int = 1,
        knowledge_classification: str = KnowledgeClassification.GENERAL,
        source_kind: str = SOURCE_KIND_DOCUMENT,
        source_revision: str = "",
        source_content_hash: str = "",
        search_aliases: Iterable[str] | None = None,
        conflict_group_id: str = "",
        conflict_confirmed: bool = False,
    ) -> tuple[DocumentSource, bool]:
        """Authorize metadata before decoding content or touching storage."""
        decision = can_manage_document(
            document={
                "scope": scope,
                "company_id": company_id,
                "user_id": user_id,
                "status": DOCUMENT_ACTIVE,
                "knowledge_classification": knowledge_classification,
            },
            current_company_id=current_company_id,
            permission_codes=permission_codes,
        )
        if not decision.allowed:
            raise KnowledgeManagementDenied(decision.reason_code)
        return self._register_text_trusted(
            source_name=source_name,
            source_key=source_key,
            content=content,
            scope=scope,
            company_id=company_id,
            user_id=user_id,
            version=version,
            knowledge_classification=knowledge_classification,
            source_kind=source_kind,
            source_revision=source_revision,
            source_content_hash=source_content_hash,
            search_aliases=search_aliases,
            conflict_group_id=conflict_group_id,
            conflict_confirmed=conflict_confirmed,
        )

    def register_artifact_checked(
        self,
        *,
        source_name: str,
        source_key: str,
        artifact: ExtractionArtifact,
        scope: str,
        current_company_id: int | None,
        permission_codes: Iterable[str] | None,
        company_id: int | None = None,
        user_id: int | None = None,
        version: int = 1,
        knowledge_classification: str = KnowledgeClassification.GENERAL,
    ) -> tuple[DocumentSource, bool]:
        """Explicitly register a previously extracted artifact after the manage gate."""
        decision = can_manage_document(
            document={
                "scope": scope,
                "company_id": company_id,
                "user_id": user_id,
                "status": DOCUMENT_ACTIVE,
                "knowledge_classification": knowledge_classification,
            },
            current_company_id=current_company_id,
            permission_codes=permission_codes,
        )
        if not decision.allowed:
            raise KnowledgeManagementDenied(decision.reason_code)
        return self._register_text_trusted(
            source_name=source_name,
            source_key=source_key,
            content=artifact.normalized_text,
            artifact=artifact,
            scope=scope,
            company_id=company_id,
            user_id=user_id,
            version=version,
            knowledge_classification=knowledge_classification,
        )

    def _register_text_trusted(
        self,
        *,
        source_name: str,
        source_key: str,
        content: str | bytes,
        artifact: ExtractionArtifact | None = None,
        scope: str,
        company_id: int | None = None,
        user_id: int | None = None,
        version: int = 1,
        knowledge_classification: str = KnowledgeClassification.GENERAL,
        source_kind: str = SOURCE_KIND_DOCUMENT,
        source_revision: str = "",
        source_content_hash: str = "",
        search_aliases: Iterable[str] | None = None,
        conflict_group_id: str = "",
        conflict_confirmed: bool = False,
    ) -> tuple[DocumentSource, bool]:
        """Internal/fixture boundary after authorization has already succeeded."""
        safe_name = make_safe_filename(source_name, default="knowledge.txt")
        artifact = artifact or extract_text_artifact(source_name=safe_name, content=content)
        if (
            not isinstance(artifact, ExtractionArtifact)
            or artifact.extractor_version != ARTIFACT_VERSION
            or not artifact.normalized_text
            or _sha256(artifact.normalized_text) != artifact.content_hash
        ):
            raise ValueError("Knowledge artifact integrity mismatch")
        try:
            version = int(version)
        except (TypeError, ValueError) as exc:
            raise ValueError("version must be a positive integer") from exc
        if version <= 0:
            raise ValueError("version must be a positive integer")
        normalized_source_kind = _normalize_source_kind(source_kind)
        normalized_conflict_group_id, normalized_conflict_confirmed = normalize_conflict_metadata(
            conflict_group_id, conflict_confirmed
        )
        source = DocumentSource(
            document_id=str(uuid.uuid4()),
            source_name=safe_name,
            source_key=str(source_key or "").strip(),
            content_hash=artifact.content_hash,
            scope=str(scope or "").strip().upper(),
            company_id=_positive_id(company_id, field="company_id"),
            user_id=_positive_id(user_id, field="user_id"),
            version=version,
            status=DOCUMENT_ACTIVE,
            approval_status=APPROVAL_PENDING,
            created_at=_utc_now(),
            knowledge_classification=str(knowledge_classification or "").strip().upper(),
            source_kind=normalized_source_kind,
            source_revision=_normalize_source_revision(
                source_revision, source_kind=normalized_source_kind
            ),
            source_content_hash=_normalize_source_content_hash(
                source_content_hash, source_kind=normalized_source_kind
            ),
            search_aliases=_normalize_search_aliases(search_aliases),
            conflict_group_id=normalized_conflict_group_id,
            conflict_confirmed=normalized_conflict_confirmed,
        )
        if source.source_kind == SOURCE_KIND_PROJECT_SOURCE:
            _validate_project_source_artifact(
                artifact=artifact,
                source_key=source.source_key,
                source_revision=source.source_revision,
                source_content_hash=source.source_content_hash,
            )
        if not source.source_key:
            raise ValueError("source_key is required")
        decision = validate_document_scope(source.policy_document())
        if not decision.allowed:
            raise ValueError(f"invalid Knowledge scope: {decision.reason_code}")
        classification_decision = validate_document_classification(source.policy_document())
        if not classification_decision.allowed:
            raise ValueError(f"invalid Knowledge classification: {classification_decision.reason_code}")

        documents = self._read_manifest()
        for current in documents:
            same_logical_source = (
                current.source_key == source.source_key
                and current.scope == source.scope
                and current.company_id == source.company_id
                and current.user_id == source.user_id
            )
            if same_logical_source and current.version == source.version:
                if (
                    current.content_hash != source.content_hash
                    or current.knowledge_classification != source.knowledge_classification
                    or current.source_kind != source.source_kind
                    or current.source_revision != source.source_revision
                    or current.source_content_hash != source.source_content_hash
                    or current.search_aliases != source.search_aliases
                    or current.conflict_group_id != source.conflict_group_id
                    or current.conflict_confirmed != source.conflict_confirmed
                ):
                    raise ValueError("Knowledge source version already has different content or metadata")
                return current, False
        self._write_artifact_once(artifact)
        documents.append(source)
        self._write_manifest(documents)
        return source, True

    @staticmethod
    def _find_document(documents: Iterable[DocumentSource], document_id: str) -> DocumentSource:
        target = next((source for source in documents if source.document_id == document_id), None)
        if target is None:
            raise ValueError("Knowledge document was not found")
        return target

    def approve_checked(
        self,
        *,
        document_id: str,
        current_company_id: int | None,
        permission_codes: Iterable[str] | None,
    ) -> DocumentSource:
        """Authorize manifest metadata before approval or supersede mutation."""
        documents = self._read_manifest()
        target = self._find_document(documents, document_id)
        decision = can_manage_document(
            document=target.policy_document(),
            current_company_id=current_company_id,
            permission_codes=permission_codes,
        )
        if not decision.allowed:
            raise KnowledgeManagementDenied(decision.reason_code)
        return self._approve_target(documents, target)

    def retire_checked(
        self,
        *,
        document_id: str,
        version: int,
        current_company_id: int | None,
        permission_codes: Iterable[str] | None,
    ) -> tuple[DocumentSource, bool]:
        """Retire one exact active document after the same management check."""
        documents = self._read_manifest()
        target = self._find_document(documents, document_id)
        if int(version) != target.version:
            raise ValueError("Knowledge document version does not match retire request")
        decision = can_manage_document(
            document=target.policy_document(),
            current_company_id=current_company_id,
            permission_codes=permission_codes,
        )
        if not decision.allowed:
            raise KnowledgeManagementDenied(decision.reason_code)
        if target.status == DOCUMENT_RETIRED:
            return target, False
        if target.status != DOCUMENT_ACTIVE:
            raise ValueError("Only an active Knowledge document can be retired")
        retired = DocumentSource(**{**asdict(target), "status": DOCUMENT_RETIRED})
        self._write_manifest([
            retired if source.document_id == retired.document_id else source
            for source in documents
        ])
        return retired, True

    def _approve_trusted(self, *, document_id: str) -> DocumentSource:
        """Internal/fixture boundary after authorization has already succeeded."""
        documents = self._read_manifest()
        target = self._find_document(documents, document_id)
        return self._approve_target(documents, target)

    def _approve_target(
        self,
        documents: list[DocumentSource],
        target: DocumentSource,
    ) -> DocumentSource:
        if target.approval_status == APPROVAL_APPROVED and target.status == DOCUMENT_ACTIVE:
            return target

        same_source_approved = [
            source
            for source in documents
            if source.document_id != target.document_id
            and source.source_key == target.source_key
            and source.scope == target.scope
            and source.company_id == target.company_id
            and source.user_id == target.user_id
            and source.status == DOCUMENT_ACTIVE
            and source.approval_status == APPROVAL_APPROVED
        ]
        if same_source_approved and target.version <= max(source.version for source in same_source_approved):
            raise ValueError("Knowledge document version downgrade is not allowed")
        if target.status != DOCUMENT_ACTIVE:
            raise ValueError("Knowledge document is not active")

        approved = DocumentSource(
            **{**asdict(target), "approval_status": APPROVAL_APPROVED, "approved_at": _utc_now()}
        )
        rewritten = [approved if source.document_id == approved.document_id else source for source in documents]
        rewritten = [
            DocumentSource(**{**asdict(source), "status": DOCUMENT_SUPERSEDED})
            if source.document_id != approved.document_id
            and source.source_key == approved.source_key
            and source.scope == approved.scope
            and source.company_id == approved.company_id
            and source.user_id == approved.user_id
            and source.status == DOCUMENT_ACTIVE
            else source
            for source in rewritten
        ]
        self._write_manifest(rewritten)
        return approved

    def _read_artifact(self, content_hash: str) -> ExtractionArtifact:
        try:
            raw = json.loads(self._artifact_path(content_hash).read_text(encoding="utf-8"))
            artifact = ExtractionArtifact(
                content_hash=str(raw["content_hash"]),
                extractor_version=int(raw["extractor_version"]),
                normalized_text=str(raw["normalized_text"]),
                sections=tuple(raw["sections"]),
                source_content_hash=str(raw.get("source_content_hash") or ""),
                extractor_kind=str(raw.get("extractor_kind") or "text"),
            )
        except Exception as exc:
            raise ValueError("Knowledge artifact is corrupt") from exc
        if artifact.extractor_version != ARTIFACT_VERSION or _sha256(artifact.normalized_text) != content_hash:
            raise ValueError("Knowledge artifact integrity mismatch")
        return artifact

    @staticmethod
    def _score(query: str, source: DocumentSource, section: dict[str, str]) -> int:
        query_terms, compact_query = _lexical_parts(query)
        if not query_terms:
            return 0
        haystack_tokens, compact_haystack = _lexical_parts(
            " ".join((source.source_name, source.source_key, " ".join(source.search_aliases), section["title"], section["text"]))
        )
        unique_terms = tuple(dict.fromkeys(query_terms))
        score = 0
        for term in unique_terms:
            if term in haystack_tokens:
                score += 4
            elif any(term in token for token in haystack_tokens):
                score += 2
            elif term in compact_haystack:
                score += 1
            else:
                return 0
        if compact_query and compact_query in compact_haystack:
            score += 3
        return score

    def retrieve_for_chat(
        self,
        *,
        query: str,
        request_context: KnowledgeChatRequestContext,
        max_chars: int = 6000,
    ) -> ContextPacket:
        """Use the immutable chat request context before any artifact is read."""
        decision = authorize_knowledge_chat_request(request_context)
        if not decision.allowed:
            return ContextPacket(
                text="",
                citations=(),
                reason_code=decision.reason_code,
                candidate_count=0,
            )
        return self.retrieve(
            query=query,
            current_user_id=request_context.user_id,
            current_company_id=request_context.company_id,
            permission_codes=request_context.permission_codes,
            max_chars=max_chars,
            technical_detail_mode=request_context.technical_detail_mode,
        )

    def authorize_evidence_snapshot(
        self,
        *,
        snapshot: KnowledgeEvidenceSnapshot,
        request_context: KnowledgeChatRequestContext,
    ) -> KnowledgeChatAuthorization:
        """Re-authorize a saved answer without retrieval, LLM work, or artifact IO."""
        current = authorize_knowledge_chat_request(request_context)
        if not current.allowed:
            return current
        if not isinstance(snapshot, KnowledgeEvidenceSnapshot):
            return KnowledgeChatAuthorization(False, "invalid_evidence_snapshot")
        if not snapshot.citations:
            return KnowledgeChatAuthorization(False, "evidence_missing_citations")
        if (
            snapshot.user_id != request_context.user_id
            or snapshot.company_id != request_context.company_id
            or snapshot.room_owner_user_id != request_context.room_owner_user_id
            or snapshot.room_company_id != request_context.room_company_id
        ):
            return KnowledgeChatAuthorization(False, "evidence_request_context_mismatch")

        try:
            documents = {source.document_id: source for source in self._read_manifest()}
        except ValueError:
            return KnowledgeChatAuthorization(False, "knowledge_manifest_unavailable")

        for citation in snapshot.citations:
            source = documents.get(citation.document_id)
            if source is None:
                return KnowledgeChatAuthorization(False, "evidence_document_missing")
            if (
                source.source_name != citation.source_name
                or source.version != citation.version
                or source.source_kind != citation.source_kind
                or source.source_revision != citation.source_revision
                or source.approval_status != APPROVAL_APPROVED
                or source.status != DOCUMENT_ACTIVE
            ):
                return KnowledgeChatAuthorization(False, "evidence_document_mismatch")
            if source.source_kind == SOURCE_KIND_PROJECT_SOURCE and not self._project_source_is_current(source):
                return KnowledgeChatAuthorization(False, "evidence_project_source_stale")
            decision = can_read_document(
                document=source.policy_document(),
                current_user_id=request_context.user_id,
                current_company_id=request_context.company_id,
                permission_codes=request_context.permission_codes,
                technical_detail_mode=snapshot.technical_detail_mode,
            )
            if not decision.allowed:
                return KnowledgeChatAuthorization(False, decision.reason_code)

        allowed_ids = {citation.identifier for citation in snapshot.citations}
        sources_by_id = {source.document_id: source for source in documents.values()}
        notices: list[ContextConflictNotice] = []
        for notice in snapshot.conflict_notices:
            document_ids = set(notice.document_citation_ids)
            source_ids = set(notice.project_source_citation_ids)
            if not document_ids or not source_ids:
                continue
            if not (document_ids | source_ids).issubset(allowed_ids):
                continue
            related = [
                source
                for citation in snapshot.citations
                for source in [sources_by_id.get(citation.document_id)]
                if source is not None and citation.identifier in document_ids | source_ids
            ]
            if (
                len(related) != len(document_ids | source_ids)
                or any(
                    not source.conflict_confirmed
                    or source.conflict_group_id != notice.conflict_group_id
                    for source in related
                )
                or not any(source.source_kind == SOURCE_KIND_DOCUMENT for source in related)
                or not any(source.source_kind == SOURCE_KIND_PROJECT_SOURCE for source in related)
            ):
                continue
            notices.append(notice)

        return KnowledgeChatAuthorization(
            True,
            "ready",
            citations=snapshot.citations,
            conflict_notices=tuple(notices),
        )

    def retrieve(
        self,
        *,
        query: str,
        current_user_id: int | None,
        current_company_id: int | None,
        permission_codes: Iterable[str] | None,
        max_chars: int = 6000,
        technical_detail_mode: bool = False,
    ) -> ContextPacket:
        """Read artifact bytes only after approved status and scope/permission approval."""
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        candidates: list[tuple[int, DocumentSource, dict[str, str]]] = []
        for source in self._read_manifest():
            if source.approval_status != APPROVAL_APPROVED:
                continue
            decision = can_read_document(
                document=source.policy_document(),
                current_user_id=current_user_id,
                current_company_id=current_company_id,
                permission_codes=permission_codes,
                technical_detail_mode=technical_detail_mode,
            )
            if not decision.allowed:
                continue
            # Operating Chat supplies source_repo_root. Never read a stale
            # PROJECT_SOURCE artifact into that request's LLM context.
            if (
                source.source_kind == SOURCE_KIND_PROJECT_SOURCE
                and self._source_repo_root is not None
                and not self._project_source_is_current(source)
            ):
                continue
            artifact = self._read_artifact(source.content_hash)
            for raw_section in artifact.sections:
                section = dict(raw_section)
                section["_artifact_content_hash"] = artifact.content_hash
                section["_source_content_hash"] = artifact.source_content_hash
                section["_extractor_kind"] = artifact.extractor_kind
                section["_extractor_version"] = str(artifact.extractor_version)
                score = self._score(query, source, section)
                if score:
                    candidates.append((score, source, section))
        candidates.sort(key=lambda row: (-row[0], row[1].source_name, row[2]["section_id"]))
        chunks: list[str] = []
        citations: list[ContextCitation] = []
        selected: list[tuple[DocumentSource, ContextCitation]] = []
        total = 0
        for _, source, section in candidates:
            citation = ContextCitation(
                document_id=source.document_id,
                source_name=source.source_name,
                version=source.version,
                section_id=section["section_id"],
                section_title=section["title"],
                source_kind=source.source_kind,
                source_revision=source.source_revision,
                artifact_content_hash=str(section.get("_artifact_content_hash") or ""),
                source_content_hash=str(section.get("_source_content_hash") or ""),
                extractor_kind=str(section.get("_extractor_kind") or "text"),
                extractor_version=int(section.get("_extractor_version") or ARTIFACT_VERSION),
                source_location=str(section.get("location_label") or ""),
                ocr_used=str(section.get("ocr_used") or "").lower() == "true",
            )
            rendered = f"{citation.label}\n{section['text']}"
            remaining = max_chars - total
            if remaining <= 0:
                break
            if len(rendered) > remaining:
                content_limit = remaining - len(citation.label) - 1
                if content_limit <= 0:
                    break
                rendered = f"{citation.label}\n{section['text'][:content_limit].rstrip()}"
            if not rendered:
                break
            chunks.append(rendered)
            citations.append(citation)
            selected.append((source, citation))
            total += len(rendered) + 2

        conflict_groups: dict[str, dict[str, list[str]]] = {}
        for source, citation in selected:
            if not source.conflict_confirmed or not source.conflict_group_id:
                continue
            by_kind = conflict_groups.setdefault(source.conflict_group_id, {})
            by_kind.setdefault(source.source_kind, []).append(citation.identifier)
        conflict_notices = tuple(
            ContextConflictNotice(
                conflict_group_id=group_id,
                document_citation_ids=tuple(by_kind[SOURCE_KIND_DOCUMENT]),
                project_source_citation_ids=tuple(by_kind[SOURCE_KIND_PROJECT_SOURCE]),
            )
            for group_id, by_kind in sorted(conflict_groups.items())
            if by_kind.get(SOURCE_KIND_DOCUMENT)
            and by_kind.get(SOURCE_KIND_PROJECT_SOURCE)
        )
        return ContextPacket(
            text="\n\n".join(chunks),
            citations=tuple(citations),
            reason_code="ready" if chunks else "no_authorized_match",
            candidate_count=len(candidates),
            conflict_notices=conflict_notices,
        )
