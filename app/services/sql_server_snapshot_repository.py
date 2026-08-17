from __future__ import annotations

import hashlib
import gzip
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from app.services.ssai_analytics_db import connect_analytics_db
from app.services.ssai_snapshot_repository import (
    SNAPSHOT_STATUS_CORRUPT,
    SNAPSHOT_STATUS_MISSING,
    SNAPSHOT_STATUS_READY,
    SNAPSHOT_STATUS_STALE,
    SNAPSHOT_STATUS_UNAPPROVED,
    SNAPSHOT_STATUS_VERSION_MISMATCH,
    SnapshotKey,
    SnapshotPublishResult,
    SnapshotReadResult,
)


PayloadValidator = Callable[[Mapping[str, Any], SnapshotKey], SnapshotReadResult]
log = logging.getLogger("ssai.sims.dashboard_snapshot")


@dataclass(frozen=True)
class SnapshotGenerationInspection:
    """Exact-generation, read-only integrity view used before an explicit manual approval."""

    status: str
    manifest_status: str = ""
    approval_status: str = ""
    payload: Mapping[str, Any] | None = None
    reason: str = ""
    manifest_id: int | None = None
    generation_no: int | None = None
    checksum: str = ""
    storage_checksum: str = ""
    payload_size: int = 0


def _canonical_payload(payload: Mapping[str, Any]) -> tuple[str, bytes]:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text, text.encode("utf-8")


def _storage_checksum(payload_bytes: bytes) -> str:
    return hashlib.sha256(payload_bytes).hexdigest()


def _decode_compressed_payload(value: Any) -> str:
    """Decode SQL Server COMPRESS(VARBINARY(NVARCHAR)) without changing payload bytes."""
    if isinstance(value, str):
        # In-memory repository fixtures model the logical value, not SQL transport bytes.
        return value
    try:
        utf16_bytes = gzip.decompress(bytes(value))
        return utf16_bytes.decode("utf-16le")
    except (OSError, TypeError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("compressed snapshot payload cannot be decoded") from exc


def _key_values(key: SnapshotKey) -> tuple[str, str, str, str, str, str]:
    return (
        key.company_id,
        key.snapshot_type,
        key.evaluation_month,
        key.scope_fingerprint,
        key.schema_version,
        key.algorithm_version,
    )


def _snapshot_exception_code(exc: Exception) -> str:
    """Expose only an exception class and optional SQLSTATE in runtime logs."""
    sqlstate = ""
    args = getattr(exc, "args", ()) or ()
    if args and isinstance(args[0], str):
        candidate = args[0].strip().upper()
        if len(candidate) == 5 and candidate.isalnum():
            sqlstate = candidate
    return f"{type(exc).__name__}{':' + sqlstate if sqlstate else ''}"


def _log_read_stage(
    key: SnapshotKey,
    stage: str,
    *,
    elapsed_ms: int,
    status: str = "",
    generation_no: int | None = None,
    payload_bytes: int = 0,
    reason_code: str = "",
) -> None:
    log.info(
        "[dashboard.snapshot.repository] stage=%s company_id=%s evaluation_month=%s scope_fingerprint=%s schema_version=%s algorithm_version=%s status=%s generation_no=%s payload_bytes=%s reason_code=%s elapsed_ms=%s",
        stage,
        key.company_id,
        key.evaluation_month,
        key.scope_fingerprint[:12],
        key.schema_version,
        key.algorithm_version,
        status,
        generation_no if generation_no is not None else "",
        payload_bytes,
        reason_code,
        elapsed_ms,
    )


def _validate_actor(value: str, *, field: str) -> str:
    actor = str(value or "").strip()
    if not actor:
        raise ValueError(f"{field} is required")
    return actor


class SqlServerSnapshotRepository:
    """Generation-only SQL Server backend for approved shared snapshots."""

    def __init__(
        self,
        *,
        reader_connection_factory: Callable[[], Any] | None = None,
        writer_connection_factory: Callable[[], Any] | None = None,
        payload_validator: PayloadValidator | None = None,
    ) -> None:
        self._reader_connection_factory = reader_connection_factory or (
            lambda: connect_analytics_db("reader")
        )
        self._writer_connection_factory = writer_connection_factory or (
            lambda: connect_analytics_db("writer")
        )
        self._payload_validator = payload_validator

    def _validate_payload(self, payload: Mapping[str, Any], key: SnapshotKey) -> None:
        scope = payload.get("scope")
        if not isinstance(scope, Mapping):
            raise ValueError("snapshot scope is missing")
        actual = SnapshotKey(
            company_id=str(payload.get("company_id") or ""),
            snapshot_type=str(payload.get("snapshot_type") or ""),
            evaluation_month=str(payload.get("evaluation_month") or ""),
            scope_fingerprint=str(scope.get("fingerprint") or ""),
            schema_version=str(payload.get("schema_version") or ""),
            algorithm_version=str(payload.get("algorithm_version") or ""),
        )
        if actual != key:
            raise ValueError("payload key does not match repository key")
        checksum = str(payload.get("checksum") or "")
        if len(checksum) != 64:
            raise ValueError("payload checksum is missing")
        if self._payload_validator is not None:
            result = self._payload_validator(payload, key)
            if not result.usable:
                raise ValueError(f"payload validation failed: {result.status}: {result.reason}")

    def publish(
        self,
        key: SnapshotKey,
        payload: Mapping[str, Any],
        *,
        created_by: str,
        force: bool = False,
    ) -> SnapshotPublishResult:
        actor = _validate_actor(created_by, field="created_by")
        self._validate_payload(payload, key)
        payload_json, payload_bytes = _canonical_payload(payload)
        storage_checksum = _storage_checksum(payload_bytes)
        contract_checksum = str(payload.get("checksum"))
        summary = payload.get("summary")
        item_count = int(summary.get("product_count") or 0) if isinstance(summary, Mapping) else 0
        basis_from = str(payload.get("basis_from") or "")
        basis_to = str(payload.get("basis_to") or "")
        source_fingerprint = str(payload.get("source_fingerprint") or "")
        if len(source_fingerprint) != 64 or len(basis_from) != 8 or len(basis_to) != 8:
            raise ValueError("payload source provenance is incomplete")

        conn = self._writer_connection_factory()
        try:
            cursor = conn.cursor()
            current = cursor.execute(
                """
                /* snapshot.publish.latest */
                SELECT TOP 1 manifest_id, generation_no, checksum, status, approval_status
                FROM snapshot.manifest WITH (UPDLOCK, HOLDLOCK)
                WHERE company_id=? AND snapshot_type=? AND evaluation_month=?
                  AND scope_fingerprint=? AND schema_version=? AND algorithm_version=?
                ORDER BY generation_no DESC
                """,
                *_key_values(key),
            ).fetchone()
            if (
                current
                and str(current[2]) == contract_checksum
                and str(current[3]) in {"draft", "published"}
                and not force
            ):
                conn.commit()
                return SnapshotPublishResult(
                    status=str(current[3]),
                    generation_no=int(current[1]),
                    checksum=contract_checksum,
                    no_op=True,
                    manifest_id=int(current[0]),
                    approval_status=str(current[4]),
                )
            generation_no = int(current[1]) + 1 if current else 1
            inserted = cursor.execute(
                """
                /* snapshot.publish.manifest */
                INSERT INTO snapshot.manifest (
                    company_id, snapshot_type, evaluation_month, basis_from, basis_to,
                    scope_fingerprint, schema_version, algorithm_version, generation_no,
                    status, approval_status, source_watermark, source_watermark_status,
                    source_fingerprint, item_count, payload_size, checksum, created_by
                )
                OUTPUT INSERTED.manifest_id
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', 'pending', ?, ?, ?, ?, ?, ?, ?)
                """,
                key.company_id,
                key.snapshot_type,
                key.evaluation_month,
                basis_from,
                basis_to,
                key.scope_fingerprint,
                key.schema_version,
                key.algorithm_version,
                generation_no,
                payload.get("source_watermark"),
                str(payload.get("source_watermark_status") or "unverified"),
                source_fingerprint,
                item_count,
                len(payload_bytes),
                contract_checksum,
                actor,
            ).fetchone()
            manifest_id = int(inserted[0])
            cursor.execute(
                """
                /* snapshot.publish.payload */
                INSERT INTO snapshot.payload
                    (manifest_id, payload_json, storage_checksum, payload_size)
                VALUES (?, ?, ?, ?)
                """,
                manifest_id,
                payload_json,
                storage_checksum,
                len(payload_bytes),
            )
            conn.commit()
            return SnapshotPublishResult(
                status="draft",
                generation_no=generation_no,
                checksum=contract_checksum,
                manifest_id=manifest_id,
                approval_status="pending",
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def approve(
        self,
        key: SnapshotKey,
        generation_no: int,
        *,
        approved_by: str,
        approval_reason: str,
    ) -> SnapshotPublishResult:
        return self._approve(
            key,
            generation_no,
            approved_by=approved_by,
            approval_reason=approval_reason,
            expected_checksum="",
        )

    def approve_checked(
        self,
        key: SnapshotKey,
        generation_no: int,
        *,
        expected_checksum: str,
        approved_by: str,
        approval_reason: str,
    ) -> SnapshotPublishResult:
        checksum = str(expected_checksum or "").strip().lower()
        if len(checksum) != 64 or any(ch not in "0123456789abcdef" for ch in checksum):
            raise ValueError("expected_checksum must be a SHA-256 checksum")
        return self._approve(
            key,
            generation_no,
            approved_by=approved_by,
            approval_reason=approval_reason,
            expected_checksum=checksum,
        )

    def _approve(
        self,
        key: SnapshotKey,
        generation_no: int,
        *,
        approved_by: str,
        approval_reason: str,
        expected_checksum: str,
    ) -> SnapshotPublishResult:
        actor = _validate_actor(approved_by, field="approved_by")
        reason = _validate_actor(approval_reason, field="approval_reason")
        conn = self._writer_connection_factory()
        try:
            cursor = conn.cursor()
            row = cursor.execute(
                """
                /* snapshot.approve.load */
                SELECT m.manifest_id, m.status, m.approval_status, m.checksum,
                       p.payload_json, p.storage_checksum, p.payload_size
                FROM snapshot.manifest m WITH (UPDLOCK, HOLDLOCK)
                INNER JOIN snapshot.payload p WITH (UPDLOCK, HOLDLOCK)
                    ON p.manifest_id=m.manifest_id
                WHERE m.company_id=? AND m.snapshot_type=? AND m.evaluation_month=?
                  AND m.scope_fingerprint=? AND m.schema_version=? AND m.algorithm_version=?
                  AND m.generation_no=?
                """,
                *_key_values(key),
                int(generation_no),
            ).fetchone()
            if not row:
                raise LookupError("snapshot generation not found")
            if str(row[1]) != "draft" or str(row[2]) != "pending":
                raise ValueError("only a pending draft generation can be approved")
            if expected_checksum and str(row[3]).lower() != expected_checksum:
                raise ValueError("expected checksum does not match draft generation")
            payload_json = str(row[4])
            payload_bytes = payload_json.encode("utf-8")
            if len(payload_bytes) != int(row[6]) or _storage_checksum(payload_bytes) != str(row[5]):
                raise ValueError("stored payload is corrupt")
            payload = json.loads(payload_json)
            if str(payload.get("checksum") or "") != str(row[3]):
                raise ValueError("manifest and payload checksum do not match")
            self._validate_payload(payload, key)
            manifest_id = int(row[0])
            cursor.execute(
                """
                /* snapshot.approve.supersede */
                UPDATE snapshot.manifest
                SET status='superseded', superseded_at=SYSUTCDATETIME()
                WHERE company_id=? AND snapshot_type=? AND evaluation_month=?
                  AND scope_fingerprint=? AND schema_version=? AND algorithm_version=?
                  AND status='published' AND manifest_id<>?
                """,
                *_key_values(key),
                manifest_id,
            )
            cursor.execute(
                """
                /* snapshot.approve.publish */
                UPDATE snapshot.manifest
                SET status='published', approval_status='approved',
                    approved_at=SYSUTCDATETIME(), approved_by=?, approval_reason=?,
                    published_at=SYSUTCDATETIME()
                WHERE manifest_id=? AND status='draft' AND approval_status='pending'
                """,
                actor,
                reason,
                manifest_id,
            )
            conn.commit()
            return SnapshotPublishResult(
                status=SNAPSHOT_STATUS_READY,
                generation_no=int(generation_no),
                checksum=str(row[3]),
                manifest_id=manifest_id,
                approval_status="approved",
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def inspect_generation(self, key: SnapshotKey, generation_no: int) -> SnapshotGenerationInspection:
        """Read exactly one generation and prove storage and contract integrity without changing it."""
        conn = self._reader_connection_factory()
        try:
            cursor = conn.cursor()
            row = cursor.execute(
                """
                /* snapshot.inspect.generation */
                SELECT m.manifest_id, m.status, m.approval_status, m.checksum,
                       p.payload_json, p.storage_checksum, p.payload_size
                FROM snapshot.manifest m
                INNER JOIN snapshot.payload p ON p.manifest_id=m.manifest_id
                WHERE m.company_id=? AND m.snapshot_type=? AND m.evaluation_month=?
                  AND m.scope_fingerprint=? AND m.schema_version=? AND m.algorithm_version=?
                  AND m.generation_no=?
                """,
                *_key_values(key),
                int(generation_no),
            ).fetchone()
            if not row:
                return SnapshotGenerationInspection(status=SNAPSHOT_STATUS_MISSING, generation_no=int(generation_no))
            payload_json = str(row[4])
            payload_bytes = payload_json.encode("utf-8")
            if len(payload_bytes) != int(row[6]) or _storage_checksum(payload_bytes) != str(row[5]):
                return SnapshotGenerationInspection(
                    status=SNAPSHOT_STATUS_CORRUPT,
                    manifest_status=str(row[1]), approval_status=str(row[2]),
                    manifest_id=int(row[0]), generation_no=int(generation_no), checksum=str(row[3]),
                    storage_checksum=str(row[5]), payload_size=int(row[6]), reason="storage checksum mismatch",
                )
            try:
                payload = json.loads(payload_json)
                self._validate_payload(payload, key)
            except Exception as exc:
                return SnapshotGenerationInspection(
                    status=SNAPSHOT_STATUS_CORRUPT,
                    manifest_status=str(row[1]), approval_status=str(row[2]),
                    manifest_id=int(row[0]), generation_no=int(generation_no), checksum=str(row[3]),
                    storage_checksum=str(row[5]), payload_size=int(row[6]), reason=str(exc),
                )
            if str(payload.get("checksum") or "") != str(row[3]):
                return SnapshotGenerationInspection(
                    status=SNAPSHOT_STATUS_CORRUPT,
                    manifest_status=str(row[1]), approval_status=str(row[2]),
                    manifest_id=int(row[0]), generation_no=int(generation_no), checksum=str(row[3]),
                    storage_checksum=str(row[5]), payload_size=int(row[6]), reason="manifest checksum mismatch",
                )
            status = SNAPSHOT_STATUS_READY if str(row[1]) == "published" and str(row[2]) == "approved" else SNAPSHOT_STATUS_UNAPPROVED
            return SnapshotGenerationInspection(
                status=status, manifest_status=str(row[1]), approval_status=str(row[2]), payload=payload,
                manifest_id=int(row[0]), generation_no=int(generation_no), checksum=str(row[3]),
                storage_checksum=str(row[5]), payload_size=int(row[6]),
            )
        finally:
            conn.close()

    def read(self, key: SnapshotKey) -> SnapshotReadResult:
        total_started = time.perf_counter()
        conn = None
        payload_size = 0
        generation_no: int | None = None

        def _finish(result: SnapshotReadResult) -> SnapshotReadResult:
            _log_read_stage(
                key,
                "repository_total",
                elapsed_ms=int((time.perf_counter() - total_started) * 1000),
                status=result.status,
                generation_no=result.generation_no,
                payload_bytes=payload_size,
                reason_code="none" if result.status == SNAPSHOT_STATUS_READY else str(result.status),
            )
            return result

        try:
            started = time.perf_counter()
            conn = self._reader_connection_factory()
            _log_read_stage(key, "db_connection", elapsed_ms=int((time.perf_counter() - started) * 1000))
            cursor = conn.cursor()
            started = time.perf_counter()
            statement = cursor.execute(
                """
                /* snapshot.read.published */
                SELECT TOP 1 m.manifest_id, m.generation_no, m.checksum,
                       m.approval_status, m.approved_at, m.approved_by, m.approval_reason,
                       COMPRESS(CONVERT(VARBINARY(MAX), p.payload_json)) AS payload_compressed,
                       p.storage_checksum, p.payload_size
                FROM snapshot.manifest m
                INNER JOIN snapshot.payload p ON p.manifest_id=m.manifest_id
                WHERE m.company_id=? AND m.snapshot_type=? AND m.evaluation_month=?
                  AND m.scope_fingerprint=? AND m.schema_version=? AND m.algorithm_version=?
                  AND m.status='published' AND m.approval_status='approved'
                ORDER BY m.generation_no DESC
                """,
                *_key_values(key),
            )
            _log_read_stage(key, "published_sql_execute", elapsed_ms=int((time.perf_counter() - started) * 1000))
            started = time.perf_counter()
            row = statement.fetchone()
            transport_payload_size = len(bytes(row[7])) if row and not isinstance(row[7], str) else 0
            _log_read_stage(
                key,
                "published_sql_fetch",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                generation_no=int(row[1]) if row else None,
                payload_bytes=transport_payload_size,
            )
            if not row:
                return _finish(self._unavailable_status(cursor, key))
            generation_no = int(row[1])
            started = time.perf_counter()
            payload_json = _decode_compressed_payload(row[7])
            payload_bytes = payload_json.encode("utf-8")
            payload_size = len(payload_bytes)
            _log_read_stage(key, "payload_byte_conversion", elapsed_ms=int((time.perf_counter() - started) * 1000), generation_no=generation_no, payload_bytes=payload_size)
            started = time.perf_counter()
            storage_valid = payload_size == int(row[9]) and _storage_checksum(payload_bytes) == str(row[8])
            _log_read_stage(key, "storage_checksum", elapsed_ms=int((time.perf_counter() - started) * 1000), generation_no=generation_no, payload_bytes=payload_size, status="valid" if storage_valid else "corrupt")
            if not storage_valid:
                return _finish(SnapshotReadResult(status=SNAPSHOT_STATUS_CORRUPT, reason="storage checksum mismatch"))
            try:
                started = time.perf_counter()
                payload = json.loads(payload_json)
                _log_read_stage(key, "json_decode", elapsed_ms=int((time.perf_counter() - started) * 1000), generation_no=generation_no, payload_bytes=payload_size)
                started = time.perf_counter()
                if str(payload.get("checksum") or "") != str(row[2]):
                    return _finish(SnapshotReadResult(status=SNAPSHOT_STATUS_CORRUPT, reason="manifest checksum mismatch"))
                self._validate_payload(payload, key)
                _log_read_stage(key, "contract_checksum_validation", elapsed_ms=int((time.perf_counter() - started) * 1000), generation_no=generation_no, payload_bytes=payload_size, status="valid")
            except Exception as exc:
                _log_read_stage(key, "contract_checksum_validation", elapsed_ms=int((time.perf_counter() - started) * 1000), generation_no=generation_no, payload_bytes=payload_size, status="corrupt", reason_code=_snapshot_exception_code(exc))
                return _finish(SnapshotReadResult(status=SNAPSHOT_STATUS_CORRUPT, reason=str(exc)))
            return _finish(SnapshotReadResult(
                status=SNAPSHOT_STATUS_READY,
                payload=payload,
                manifest_id=int(row[0]),
                generation_no=int(row[1]),
                checksum=str(row[2]),
                approval_status=str(row[3]),
                approved_at=str(row[4] or ""),
                approved_by=str(row[5] or ""),
                approval_reason=str(row[6] or ""),
            ))
        except Exception as exc:
            _log_read_stage(
                key,
                "repository_error",
                elapsed_ms=int((time.perf_counter() - total_started) * 1000),
                status="error",
                generation_no=generation_no,
                payload_bytes=payload_size,
                reason_code=_snapshot_exception_code(exc),
            )
            raise
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _unavailable_status(cursor: Any, key: SnapshotKey) -> SnapshotReadResult:
        exact = cursor.execute(
            """
            /* snapshot.read.exact */
            SELECT TOP 1 status, approval_status
            FROM snapshot.manifest
            WHERE company_id=? AND snapshot_type=? AND evaluation_month=?
              AND scope_fingerprint=? AND schema_version=? AND algorithm_version=?
            ORDER BY generation_no DESC
            """,
            *_key_values(key),
        ).fetchone()
        if exact:
            if str(exact[0]) == "draft" or str(exact[1]) != "approved":
                return SnapshotReadResult(status=SNAPSHOT_STATUS_UNAPPROVED, reason="snapshot is not approved")
            return SnapshotReadResult(status=SNAPSHOT_STATUS_STALE, reason=f"snapshot status is {exact[0]}")
        other_version = cursor.execute(
            """
            /* snapshot.read.other_version */
            SELECT TOP 1 manifest_id
            FROM snapshot.manifest
            WHERE company_id=? AND snapshot_type=? AND evaluation_month=?
              AND scope_fingerprint=? AND status='published' AND approval_status='approved'
            """,
            key.company_id,
            key.snapshot_type,
            key.evaluation_month,
            key.scope_fingerprint,
        ).fetchone()
        if other_version:
            return SnapshotReadResult(
                status=SNAPSHOT_STATUS_VERSION_MISMATCH,
                reason="approved snapshot exists with another version",
            )
        return SnapshotReadResult(status=SNAPSHOT_STATUS_MISSING, reason="snapshot does not exist")

    def status(self, key: SnapshotKey) -> str:
        return self.read(key).status

    def invalidate(self, key: SnapshotKey, *, reason: str, invalidated_by: str) -> None:
        actor = _validate_actor(invalidated_by, field="invalidated_by")
        failure_reason = _validate_actor(reason, field="reason")
        conn = self._writer_connection_factory()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                /* snapshot.invalidate */
                UPDATE snapshot.manifest
                SET status='invalidated', invalidated_at=SYSUTCDATETIME(),
                    invalidated_by=?, failure_reason=?
                WHERE company_id=? AND snapshot_type=? AND evaluation_month=?
                  AND scope_fingerprint=? AND schema_version=? AND algorithm_version=?
                  AND status='published'
                """,
                actor,
                failure_reason,
                *_key_values(key),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def replace(
        self,
        key: SnapshotKey,
        payload: Mapping[str, Any],
        *,
        created_by: str,
    ) -> SnapshotPublishResult:
        return self.publish(key, payload, created_by=created_by, force=True)

    def new_generation(
        self,
        key: SnapshotKey,
        payload: Mapping[str, Any],
        *,
        created_by: str,
    ) -> SnapshotPublishResult:
        return self.replace(key, payload, created_by=created_by)
