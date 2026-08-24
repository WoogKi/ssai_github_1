from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable


SNAPSHOT_STATUS_READY = "ready"
SNAPSHOT_STATUS_MISSING = "missing"
SNAPSHOT_STATUS_STALE = "stale"
SNAPSHOT_STATUS_CORRUPT = "corrupt"
SNAPSHOT_STATUS_VERSION_MISMATCH = "version_mismatch"
SNAPSHOT_STATUS_WATERMARK_UNVERIFIED = "watermark_unverified"
SNAPSHOT_STATUS_UNAPPROVED = "unapproved"

SNAPSHOT_UNUSABLE_STATUSES = frozenset(
    {
        SNAPSHOT_STATUS_MISSING,
        SNAPSHOT_STATUS_STALE,
        SNAPSHOT_STATUS_CORRUPT,
        SNAPSHOT_STATUS_VERSION_MISMATCH,
        SNAPSHOT_STATUS_WATERMARK_UNVERIFIED,
        SNAPSHOT_STATUS_UNAPPROVED,
    }
)


@dataclass(frozen=True)
class SnapshotKey:
    company_id: str
    snapshot_type: str
    evaluation_month: str
    scope_fingerprint: str
    schema_version: str
    algorithm_version: str


@dataclass(frozen=True)
class SnapshotReadResult:
    status: str
    payload: Mapping[str, Any] | None = None
    reason: str = ""
    manifest_id: int | None = None
    generation_no: int | None = None
    checksum: str = ""
    approval_status: str = ""
    approved_at: str = ""
    approved_by: str = ""
    approval_reason: str = ""
    representation: str = "legacy_json_v1"

    @property
    def usable(self) -> bool:
        return self.status == SNAPSHOT_STATUS_READY and (
            self.payload is not None or self.representation == "relational_frequency_v1"
        )


@dataclass(frozen=True)
class SnapshotPublishResult:
    status: str
    generation_no: int | None = None
    checksum: str = ""
    no_op: bool = False
    manifest_id: int | None = None
    approval_status: str = ""


@runtime_checkable
class SnapshotRepository(Protocol):
    """Storage-neutral boundary for a future shared snapshot repository."""

    def publish(
        self,
        key: SnapshotKey,
        payload: Mapping[str, Any],
        *,
        created_by: str,
        force: bool = False,
    ) -> SnapshotPublishResult: ...

    def approve(
        self,
        key: SnapshotKey,
        generation_no: int,
        *,
        approved_by: str,
        approval_reason: str,
    ) -> SnapshotPublishResult: ...

    def read(self, key: SnapshotKey) -> SnapshotReadResult: ...

    def status(self, key: SnapshotKey) -> str: ...

    def invalidate(self, key: SnapshotKey, *, reason: str, invalidated_by: str) -> None: ...

    def replace(
        self,
        key: SnapshotKey,
        payload: Mapping[str, Any],
        *,
        created_by: str,
    ) -> SnapshotPublishResult: ...
