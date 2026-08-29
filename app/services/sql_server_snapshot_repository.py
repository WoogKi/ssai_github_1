from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from app.services.dashboard_inventory_frequency_snapshot import (
    FREQUENCY_PROJECTION_GRADES,
    FrequencyProjectionReadResult,
    RELATIONAL_FREQUENCY_REPRESENTATION,
    RelationalFrequencySnapshot,
    SnapshotContractError,
    build_frequency_projection,
    build_relational_frequency_projection,
    relational_row_checksum,
    validate_relational_frequency_snapshot,
    validate_relational_frequency_projection,
    validate_frequency_projection,
)
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
INSPECTION_QUERY_TIMEOUT_SECONDS = 30
PROJECTION_SUBSET_SAFE_LIMIT = 2000


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
    representation: str = ""
    relational_snapshot: RelationalFrequencySnapshot | None = None


def _canonical_payload(payload: Mapping[str, Any]) -> tuple[str, bytes]:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text, text.encode("utf-8")


def _storage_checksum(payload_bytes: bytes) -> str:
    return hashlib.sha256(payload_bytes).hexdigest()


def _key_values(key: SnapshotKey) -> tuple[str, str, str, str, str, str]:
    return (
        key.company_id,
        key.snapshot_type,
        key.evaluation_month,
        key.scope_fingerprint,
        key.schema_version,
        key.algorithm_version,
    )


def _contract_checksum_values(values: tuple[Any, ...]) -> tuple[Any, ...]:
    """Restore SQL BIT fields to the writer's canonical 0/1 checksum form."""
    return tuple(int(value) if isinstance(value, bool) else value for value in values)


def _snapshot_exception_code(exc: Exception) -> str:
    """Expose only an exception class and optional SQLSTATE in runtime logs."""
    sqlstate = ""
    args = getattr(exc, "args", ()) or ()
    if args and isinstance(args[0], str):
        candidate = args[0].strip().upper()
        if len(candidate) == 5 and candidate.isalnum():
            sqlstate = candidate
    return f"{type(exc).__name__}{':' + sqlstate if sqlstate else ''}"


def _projection_schema_is_absent(exc: Exception) -> bool:
    """Only a missing migration is legacy compatibility; other errors are corrupt."""
    values = [str(value) for value in (getattr(exc, "args", ()) or ())]
    text = " ".join(values).upper()
    return "42S02" in text or (
        "INVALID OBJECT NAME" in text
        and ("SNAPSHOT.FREQUENCY_PRODUCT" in text or "SNAPSHOT.FREQUENCY_PROJECTION" in text)
    )


def _representation_column_is_absent(exc: Exception) -> bool:
    """M003 is optional for pre-existing legacy-only analytics databases."""
    values = [str(value) for value in (getattr(exc, "args", ()) or ())]
    text = " ".join(values).upper()
    return "42S22" in text or (
        "INVALID COLUMN NAME" in text and "STORAGE_REPRESENTATION" in text
    )


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
        reader_connection_factory: Callable[[], Any],
        writer_connection_factory: Callable[[], Any],
        payload_validator: PayloadValidator | None = None,
    ) -> None:
        self._reader_connection_factory = reader_connection_factory
        self._writer_connection_factory = writer_connection_factory
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
        projection_rows, projection_headers = build_frequency_projection(payload)
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
            cursor.executemany(
                """
                /* snapshot.publish.frequency_product */
                INSERT INTO snapshot.frequency_product (
                    manifest_id, product_code, occurrence_count_3m,
                    frequency_grade, data_status, row_checksum
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        manifest_id, row["product_code"], row["occurrence_count_3m"],
                        row["frequency_grade"], row["data_status"], row["row_checksum"],
                    )
                    for row in projection_rows
                ],
            )
            cursor.executemany(
                """
                /* snapshot.publish.frequency_projection */
                INSERT INTO snapshot.frequency_projection (
                    manifest_id, frequency_grade, expected_product_count, projection_checksum
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        manifest_id, header["frequency_grade"],
                        header["expected_product_count"], header["projection_checksum"],
                    )
                    for header in projection_headers
                ],
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

    def publish_relational(
        self,
        snapshot: RelationalFrequencySnapshot,
        *,
        created_by: str,
        force: bool = False,
    ) -> SnapshotPublishResult:
        """Store one complete payload-less relational authority set as a draft."""
        actor = _validate_actor(created_by, field="created_by")
        validate_relational_frequency_snapshot(snapshot)
        projection_rows, projection_headers = build_relational_frequency_projection(snapshot)
        key = snapshot.key
        conn = self._writer_connection_factory()
        try:
            cursor = conn.cursor()
            current = cursor.execute(
                """
                /* snapshot.publish.relational.latest */
                SELECT TOP 1 manifest_id, generation_no, checksum, status, approval_status
                FROM snapshot.manifest WITH (UPDLOCK, HOLDLOCK)
                WHERE company_id=? AND snapshot_type=? AND evaluation_month=?
                  AND scope_fingerprint=? AND schema_version=? AND algorithm_version=?
                ORDER BY generation_no DESC
                """,
                *_key_values(key),
            ).fetchone()
            if current and str(current[2]) == snapshot.checksum and str(current[3]) in {"draft", "published"} and not force:
                conn.commit()
                return SnapshotPublishResult(str(current[3]), int(current[1]), snapshot.checksum, True, int(current[0]), str(current[4]))
            generation_no = int(current[1]) + 1 if current else 1
            inserted = cursor.execute(
                """
                /* snapshot.publish.relational.manifest */
                INSERT INTO snapshot.manifest (
                    company_id, snapshot_type, evaluation_month, basis_from, basis_to,
                    scope_fingerprint, schema_version, algorithm_version, generation_no,
                    status, approval_status, source_watermark, source_watermark_status,
                    source_fingerprint, item_count, payload_size, checksum, created_by,
                    storage_representation, scope_mode, fingerprint_contract_version, fingerprint_mode
                )
                OUTPUT INSERTED.manifest_id
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', 'pending', ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
                """,
                key.company_id, key.snapshot_type, key.evaluation_month, snapshot.basis_from, snapshot.basis_to,
                key.scope_fingerprint, key.schema_version, key.algorithm_version, generation_no,
                snapshot.source_watermark, snapshot.source_watermark_status, snapshot.source_fingerprint,
                snapshot.item_count, snapshot.checksum, actor, RELATIONAL_FREQUENCY_REPRESENTATION,
                snapshot.scope_mode, int(snapshot.source_contract.get("fingerprint_contract_version") or 0), str(snapshot.source_contract.get("fingerprint_mode") or ""),
            ).fetchone()
            manifest_id = int(inserted[0])
            if snapshot.stock_codes:
                cursor.executemany(
                    "INSERT INTO snapshot.frequency_scope_stock (manifest_id, stock_code) VALUES (?, ?)",
                    [(manifest_id, code) for code in snapshot.stock_codes],
                )
            product_columns = ("product_code", "occurrence_count_3m", "frequency_grade", "data_status")
            cursor.executemany(
                """INSERT INTO snapshot.frequency_product (manifest_id, product_code, occurrence_count_3m, frequency_grade, data_status, row_checksum)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (manifest_id, row["product_code"], row["occurrence_count_3m"], row["frequency_grade"], row["data_status"], relational_row_checksum("frequency_product", product_columns, tuple(row[column] for column in product_columns)))
                    for row in snapshot.frequency_products
                ],
            )
            monthly_columns = ("month", "product_code", "stock_code", "occurrence_count", "outbound_quantity", "outbound_day_count")
            monthly_values = [
                (manifest_id, *(row[column] for column in monthly_columns), relational_row_checksum("frequency_monthly_activity", monthly_columns, tuple(row[column] for column in monthly_columns)))
                for row in snapshot.monthly_activity
            ]
            # A valid product universe can have no positive outbound events.
            # pyodbc rejects executemany([]), while the native relational
            # contract represents that state with X-grade product rows only.
            if monthly_values:
                cursor.executemany(
                    """INSERT INTO snapshot.frequency_monthly_activity (manifest_id, month, product_code, stock_code, occurrence_count, outbound_quantity, outbound_day_count, row_checksum)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    monthly_values,
                )
            contract = snapshot.source_contract
            contract_columns = ("source_table", "io_gu_gcode", "normal_tcode_from", "normal_tcode_to", "event_key_fields", "positive_quantity_expression", "return_tcode_from", "return_tcode_to", "returns_are_netted", "flag_exclusion_fields", "non_exclusion_flag_fields", "universe_mode", "dashboard_product_filters", "include_rd04_del_flag_e", "fingerprint_contract_version", "fingerprint_mode")
            contract_values = (
                contract.get("table"), contract.get("io_gu_gcode"), contract.get("normal_tcode_from"), contract.get("normal_tcode_to"),
                "|".join(contract.get("event_key_fields") or ()), contract.get("positive_quantity_expression"), contract.get("return_tcode_from"), contract.get("return_tcode_to"),
                int(bool(contract.get("returns_are_netted"))), "|".join(contract.get("flag_exclusion_fields") or ()), "|".join(contract.get("non_exclusion_flag_fields") or ()),
                contract.get("universe_mode"), contract.get("dashboard_product_filters"), int(bool(contract.get("include_rd04_del_flag_e"))), contract.get("fingerprint_contract_version"), contract.get("fingerprint_mode"),
            )
            cursor.execute(
                f"INSERT INTO snapshot.frequency_source_contract (manifest_id, {', '.join(contract_columns)}, contract_checksum) VALUES (?, {', '.join('?' for _ in contract_columns)}, ?)",
                manifest_id, *contract_values, relational_row_checksum("frequency_source_contract", contract_columns, contract_values),
            )
            diagnostics = snapshot.source_diagnostics
            diagnostic_columns = ("diagnostic_contract_version", "source_row_count", "normal_positive_accepted_row_count", "normal_positive_duplicate_row_count", "normal_positive_conflicting_row_count", "normal_positive_missing_key_row_count", "normal_positive_nonintegral_row_count", "normal_nonpositive_row_count", "return_positive_row_count", "return_nonpositive_row_count", "other_tcode_row_count", "normal_positive_row_count", "distinct_normal_event_count", "conflicting_event_count", "ignored_product_event_count")
            diagnostic_values = tuple(int(diagnostics.get(column) or 0) for column in diagnostic_columns)
            cursor.execute(
                f"INSERT INTO snapshot.frequency_source_diagnostics (manifest_id, {', '.join(diagnostic_columns)}, row_checksum) VALUES (?, {', '.join('?' for _ in diagnostic_columns)}, ?)",
                manifest_id, *diagnostic_values, relational_row_checksum("frequency_source_diagnostics", diagnostic_columns, diagnostic_values),
            )
            cursor.executemany(
                "INSERT INTO snapshot.frequency_projection (manifest_id, frequency_grade, expected_product_count, projection_checksum) VALUES (?, ?, ?, ?)",
                [(manifest_id, row["frequency_grade"], row["expected_product_count"], row["projection_checksum"]) for row in projection_headers],
            )
            conn.commit()
            return SnapshotPublishResult("draft", generation_no, snapshot.checksum, False, manifest_id, "pending")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

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
                       m.storage_representation, p.payload_json, p.storage_checksum, p.payload_size
                FROM snapshot.manifest m WITH (UPDLOCK, HOLDLOCK)
                LEFT JOIN snapshot.payload p WITH (UPDLOCK, HOLDLOCK)
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
            representation = str(row[4] or "")
            if representation == RELATIONAL_FREQUENCY_REPRESENTATION:
                snapshot, _manifest = self._load_relational_snapshot(cursor, key, int(generation_no))
                if snapshot is None:
                    raise ValueError("relational snapshot generation is missing")
                validate_relational_frequency_snapshot(snapshot)
            elif representation != "legacy_json_v1":
                raise ValueError("snapshot representation is invalid")
            else:
                payload_json = str(row[5])
                payload_bytes = payload_json.encode("utf-8")
                if len(payload_bytes) != int(row[7]) or _storage_checksum(payload_bytes) != str(row[6]):
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
        # Representation dispatch is explicit: a native generation never enters
        # the legacy payload_json inspection path.
        probe_conn = self._reader_connection_factory()
        try:
            probe_conn.timeout = INSPECTION_QUERY_TIMEOUT_SECONDS
            probe = probe_conn.cursor().execute(
                """SELECT storage_representation FROM snapshot.manifest
                   WHERE company_id=? AND snapshot_type=? AND evaluation_month=?
                     AND scope_fingerprint=? AND schema_version=? AND algorithm_version=? AND generation_no=?""",
                *_key_values(key), int(generation_no),
            ).fetchone()
            if probe and str(probe[0] or "") == RELATIONAL_FREQUENCY_REPRESENTATION:
                return self._inspect_relational_generation(key, int(generation_no))
        except Exception as exc:
            if not _representation_column_is_absent(exc):
                raise
        finally:
            probe_conn.close()
        total_started = time.perf_counter()
        conn = None
        payload_size = 0
        try:
            started = time.perf_counter()
            conn = self._reader_connection_factory()
            # pyodbc exposes statement timeout on the connection, not the cursor.
            # This bounds a blocked post-approval reader without adding a retry path.
            conn.timeout = INSPECTION_QUERY_TIMEOUT_SECONDS
            _log_read_stage(
                key,
                "inspect_db_connection",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                generation_no=int(generation_no),
            )
            cursor = conn.cursor()
            started = time.perf_counter()
            statement = cursor.execute(
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
            )
            _log_read_stage(
                key,
                "inspect_sql_execute",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                generation_no=int(generation_no),
            )
            started = time.perf_counter()
            row = statement.fetchone()
            _log_read_stage(
                key,
                "inspect_sql_fetch",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                generation_no=int(generation_no),
            )
            if not row:
                return SnapshotGenerationInspection(status=SNAPSHOT_STATUS_MISSING, generation_no=int(generation_no))
            started = time.perf_counter()
            payload_json = str(row[4])
            payload_bytes = payload_json.encode("utf-8")
            payload_size = len(payload_bytes)
            _log_read_stage(
                key,
                "inspect_payload_byte_conversion",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                generation_no=int(generation_no),
                payload_bytes=payload_size,
            )
            started = time.perf_counter()
            storage_valid = payload_size == int(row[6]) and _storage_checksum(payload_bytes) == str(row[5])
            _log_read_stage(
                key,
                "inspect_storage_checksum",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                generation_no=int(generation_no),
                payload_bytes=payload_size,
                status="valid" if storage_valid else "corrupt",
            )
            if not storage_valid:
                return SnapshotGenerationInspection(
                    status=SNAPSHOT_STATUS_CORRUPT,
                    manifest_status=str(row[1]), approval_status=str(row[2]),
                    manifest_id=int(row[0]), generation_no=int(generation_no), checksum=str(row[3]),
                    storage_checksum=str(row[5]), payload_size=int(row[6]), reason="storage checksum mismatch",
                )
            try:
                started = time.perf_counter()
                payload = json.loads(payload_json)
                _log_read_stage(
                    key,
                    "inspect_json_decode",
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    generation_no=int(generation_no),
                    payload_bytes=payload_size,
                )
                started = time.perf_counter()
                self._validate_payload(payload, key)
                _log_read_stage(
                    key,
                    "inspect_contract_validation",
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    generation_no=int(generation_no),
                    payload_bytes=payload_size,
                    status="valid",
                )
            except Exception as exc:
                _log_read_stage(
                    key,
                    "inspect_contract_validation",
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    generation_no=int(generation_no),
                    payload_bytes=payload_size,
                    status="corrupt",
                    reason_code=_snapshot_exception_code(exc),
                )
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
        except Exception as exc:
            _log_read_stage(
                key,
                "inspect_error",
                elapsed_ms=int((time.perf_counter() - total_started) * 1000),
                generation_no=int(generation_no),
                payload_bytes=payload_size,
                status="error",
                reason_code=_snapshot_exception_code(exc),
            )
            raise
        finally:
            if conn is not None:
                conn.close()

    def _inspect_relational_generation(self, key: SnapshotKey, generation_no: int) -> SnapshotGenerationInspection:
        conn = self._reader_connection_factory()
        try:
            conn.timeout = INSPECTION_QUERY_TIMEOUT_SECONDS
            cursor = conn.cursor()
            snapshot, manifest = self._load_relational_snapshot(cursor, key, generation_no)
            if snapshot is None or manifest is None:
                return SnapshotGenerationInspection(status=SNAPSHOT_STATUS_MISSING, generation_no=generation_no, representation=RELATIONAL_FREQUENCY_REPRESENTATION)
            validate_relational_frequency_snapshot(snapshot)
            status = SNAPSHOT_STATUS_READY if manifest[1] == "published" and manifest[2] == "approved" else SNAPSHOT_STATUS_UNAPPROVED
            return SnapshotGenerationInspection(
                status=status, manifest_status=str(manifest[1]), approval_status=str(manifest[2]),
                reason="", manifest_id=int(manifest[0]), generation_no=generation_no, checksum=snapshot.checksum,
                representation=RELATIONAL_FREQUENCY_REPRESENTATION, relational_snapshot=snapshot,
            )
        except Exception as exc:
            return SnapshotGenerationInspection(status=SNAPSHOT_STATUS_CORRUPT, generation_no=generation_no, representation=RELATIONAL_FREQUENCY_REPRESENTATION, reason=str(exc))
        finally:
            conn.close()

    @staticmethod
    def _load_relational_snapshot(cursor: Any, key: SnapshotKey, generation_no: int) -> tuple[RelationalFrequencySnapshot | None, tuple[Any, ...] | None]:
        manifest = cursor.execute(
            """SELECT manifest_id, status, approval_status, checksum, basis_from, basis_to,
                      scope_mode, source_watermark, source_watermark_status, source_fingerprint,
                      fingerprint_contract_version, fingerprint_mode, item_count, storage_representation
                 FROM snapshot.manifest
                 WHERE company_id=? AND snapshot_type=? AND evaluation_month=?
                   AND scope_fingerprint=? AND schema_version=? AND algorithm_version=? AND generation_no=?""",
            *_key_values(key), int(generation_no),
        ).fetchone()
        if not manifest:
            return None, None
        if str(manifest[13] or "") != RELATIONAL_FREQUENCY_REPRESENTATION:
            raise ValueError("snapshot representation is not relational")
        manifest_id = int(manifest[0])
        scope_rows = cursor.execute("SELECT stock_code FROM snapshot.frequency_scope_stock WHERE manifest_id=? ORDER BY stock_code", manifest_id).fetchall()
        contract = cursor.execute(
            """SELECT source_table, io_gu_gcode, normal_tcode_from, normal_tcode_to, event_key_fields,
                      positive_quantity_expression, return_tcode_from, return_tcode_to, returns_are_netted,
                      flag_exclusion_fields, non_exclusion_flag_fields, universe_mode, dashboard_product_filters,
                      include_rd04_del_flag_e, fingerprint_contract_version, fingerprint_mode, contract_checksum
                 FROM snapshot.frequency_source_contract WHERE manifest_id=?""",
            manifest_id,
        ).fetchone()
        diagnostics = cursor.execute(
            """SELECT diagnostic_contract_version, source_row_count, normal_positive_accepted_row_count,
                      normal_positive_duplicate_row_count, normal_positive_conflicting_row_count,
                      normal_positive_missing_key_row_count, normal_positive_nonintegral_row_count,
                      normal_nonpositive_row_count, return_positive_row_count, return_nonpositive_row_count,
                      other_tcode_row_count, normal_positive_row_count, distinct_normal_event_count,
                      conflicting_event_count, ignored_product_event_count, row_checksum
                 FROM snapshot.frequency_source_diagnostics WHERE manifest_id=?""",
            manifest_id,
        ).fetchone()
        if not contract or not diagnostics:
            raise ValueError("relational authority metadata is incomplete")
        products = cursor.execute(
            "SELECT product_code, occurrence_count_3m, frequency_grade, data_status, row_checksum FROM snapshot.frequency_product WHERE manifest_id=? ORDER BY product_code",
            manifest_id,
        ).fetchall()
        projection_headers = cursor.execute(
            "SELECT frequency_grade, expected_product_count, projection_checksum "
            "FROM snapshot.frequency_projection WHERE manifest_id=? ORDER BY frequency_grade",
            manifest_id,
        ).fetchall()
        monthly = cursor.execute(
            "SELECT month, product_code, stock_code, occurrence_count, outbound_quantity, outbound_day_count, row_checksum FROM snapshot.frequency_monthly_activity WHERE manifest_id=? ORDER BY month, product_code, stock_code",
            manifest_id,
        ).fetchall()
        contract_columns = ("source_table", "io_gu_gcode", "normal_tcode_from", "normal_tcode_to", "event_key_fields", "positive_quantity_expression", "return_tcode_from", "return_tcode_to", "returns_are_netted", "flag_exclusion_fields", "non_exclusion_flag_fields", "universe_mode", "dashboard_product_filters", "include_rd04_del_flag_e", "fingerprint_contract_version", "fingerprint_mode")
        contract_values = tuple(contract[index] for index in range(len(contract_columns)))
        # SQL BIT values come back from pyodbc as bool, while the writer
        # canonically stores those contract fields as 0/1 integers.
        if relational_row_checksum(
            "frequency_source_contract", contract_columns, _contract_checksum_values(contract_values)
        ) != str(contract[16]):
            raise ValueError("relational source contract checksum mismatch")
        diagnostic_columns = ("diagnostic_contract_version", "source_row_count", "normal_positive_accepted_row_count", "normal_positive_duplicate_row_count", "normal_positive_conflicting_row_count", "normal_positive_missing_key_row_count", "normal_positive_nonintegral_row_count", "normal_nonpositive_row_count", "return_positive_row_count", "return_nonpositive_row_count", "other_tcode_row_count", "normal_positive_row_count", "distinct_normal_event_count", "conflicting_event_count", "ignored_product_event_count")
        diagnostic_values = tuple(diagnostics[index] for index in range(len(diagnostic_columns)))
        if relational_row_checksum("frequency_source_diagnostics", diagnostic_columns, diagnostic_values) != str(diagnostics[15]):
            raise ValueError("relational diagnostics checksum mismatch")
        product_columns = ("product_code", "occurrence_count_3m", "frequency_grade", "data_status")
        product_rows: list[dict[str, Any]] = []
        for row in products:
            values = tuple(row[index] for index in range(4))
            if relational_row_checksum("frequency_product", product_columns, values) != str(row[4]):
                raise ValueError("relational product row checksum mismatch")
            product_rows.append({**dict(zip(product_columns, values)), "row_checksum": str(row[4])})
        monthly_columns = ("month", "product_code", "stock_code", "occurrence_count", "outbound_quantity", "outbound_day_count")
        monthly_rows: list[dict[str, Any]] = []
        for row in monthly:
            values = tuple(row[index] for index in range(6))
            if relational_row_checksum("frequency_monthly_activity", monthly_columns, values) != str(row[6]):
                raise ValueError("relational monthly row checksum mismatch")
            monthly_rows.append(dict(zip(monthly_columns, values)))
        source_contract = dict(zip(("table", "io_gu_gcode", "normal_tcode_from", "normal_tcode_to", "event_key_fields", "positive_quantity_expression", "return_tcode_from", "return_tcode_to", "returns_are_netted", "flag_exclusion_fields", "non_exclusion_flag_fields", "universe_mode", "dashboard_product_filters", "include_rd04_del_flag_e", "fingerprint_contract_version", "fingerprint_mode"), contract_values))
        for field in ("event_key_fields", "flag_exclusion_fields", "non_exclusion_flag_fields"):
            source_contract[field] = tuple(filter(None, str(source_contract[field] or "").split("|")))
        source_diagnostics = dict(zip(diagnostic_columns, diagnostic_values))
        snapshot = RelationalFrequencySnapshot(
            key, str(manifest[4]), str(manifest[5]), str(manifest[6]), tuple(str(row[0]) for row in scope_rows),
            None if manifest[7] is None else str(manifest[7]), str(manifest[8]), str(manifest[9]), source_contract,
            source_diagnostics, tuple(monthly_rows), tuple(product_rows), str(manifest[3]),
        )
        if int(manifest[12]) != snapshot.item_count:
            raise ValueError("relational item count mismatch")
        headers = [
            {
                "frequency_grade": row[0],
                "expected_product_count": row[1],
                "projection_checksum": row[2],
            }
            for row in projection_headers
        ]
        validate_relational_frequency_projection(
            rows=product_rows,
            headers=headers,
            require_complete=True,
        )
        return snapshot, tuple(manifest)

    def read_frequency_projection(
        self,
        key: SnapshotKey,
        *,
        product_codes: tuple[str, ...] = (),
        frequency_grade: str = "",
    ) -> FrequencyProjectionReadResult:
        """Read one approved derived projection without reading payload_json."""
        requested = tuple(sorted({str(code or "").strip() for code in product_codes if str(code or "").strip()}))
        grade = str(frequency_grade or "").strip()
        if grade and grade not in FREQUENCY_PROJECTION_GRADES:
            return FrequencyProjectionReadResult(status=SNAPSHOT_STATUS_CORRUPT, reason="projection grade is invalid")
        conn = None
        try:
            conn = self._reader_connection_factory()
            cursor = conn.cursor()
            manifest = cursor.execute(
                """
                /* snapshot.projection.manifest */
                SELECT TOP 1 m.manifest_id, m.generation_no, m.checksum, m.item_count,
                       m.storage_representation,
                       CASE WHEN EXISTS (SELECT 1 FROM snapshot.payload p WHERE p.manifest_id=m.manifest_id)
                            THEN 1 ELSE 0 END AS payload_exists
                FROM snapshot.manifest m
                WHERE m.company_id=? AND m.snapshot_type=? AND m.evaluation_month=?
                  AND m.scope_fingerprint=? AND m.schema_version=? AND m.algorithm_version=?
                  AND m.status='published' AND m.approval_status='approved'
                ORDER BY m.generation_no DESC
                """,
                *_key_values(key),
            ).fetchone()
            if not manifest:
                unavailable = self._unavailable_status(cursor, key)
                return FrequencyProjectionReadResult(status=unavailable.status, reason=unavailable.reason)
            manifest_id, generation_no, checksum, item_count, representation, payload_exists = int(manifest[0]), int(manifest[1]), str(manifest[2]), int(manifest[3]), str(manifest[4] or ""), bool(manifest[5])
            if representation not in {"legacy_json_v1", RELATIONAL_FREQUENCY_REPRESENTATION}:
                return FrequencyProjectionReadResult(status=SNAPSHOT_STATUS_CORRUPT, reason="snapshot representation is invalid", manifest_id=manifest_id, generation_no=generation_no, checksum=checksum)
            if representation == RELATIONAL_FREQUENCY_REPRESENTATION and payload_exists:
                return FrequencyProjectionReadResult(status=SNAPSHOT_STATUS_CORRUPT, reason="relational generation must not have payload_json", manifest_id=manifest_id, generation_no=generation_no, checksum=checksum)
            if representation == "legacy_json_v1" and not payload_exists:
                return FrequencyProjectionReadResult(status=SNAPSHOT_STATUS_CORRUPT, reason="legacy generation is missing payload_json", manifest_id=manifest_id, generation_no=generation_no, checksum=checksum)
            header_rows = cursor.execute(
                """
                /* snapshot.projection.headers */
                SELECT frequency_grade, expected_product_count, projection_checksum
                FROM snapshot.frequency_projection WHERE manifest_id=? ORDER BY frequency_grade
                """,
                manifest_id,
            ).fetchall()
            if not header_rows:
                product_exists = cursor.execute(
                    """
                    /* snapshot.projection.product_presence */
                    SELECT TOP 1 manifest_id FROM snapshot.frequency_product WHERE manifest_id=?
                    """,
                    manifest_id,
                ).fetchone()
                if product_exists:
                    return FrequencyProjectionReadResult(status=SNAPSHOT_STATUS_CORRUPT, reason="projection headers are missing", manifest_id=manifest_id, generation_no=generation_no, checksum=checksum)
                return FrequencyProjectionReadResult(status="legacy", reason="projection is absent for this generation", manifest_id=manifest_id, generation_no=generation_no, checksum=checksum)
            headers = [
                {"frequency_grade": row[0], "expected_product_count": row[1], "projection_checksum": row[2]}
                for row in header_rows
            ]
            if sum(int(header["expected_product_count"]) for header in headers) != item_count:
                return FrequencyProjectionReadResult(status=SNAPSHOT_STATUS_CORRUPT, reason="projection header product count does not match manifest", manifest_id=manifest_id, generation_no=generation_no, checksum=checksum)
            product_count_row = cursor.execute(
                """
                /* snapshot.projection.product_count */
                SELECT COUNT_BIG(*) FROM snapshot.frequency_product WHERE manifest_id=?
                """,
                manifest_id,
            ).fetchone()
            if not product_count_row or int(product_count_row[0]) != item_count:
                return FrequencyProjectionReadResult(status=SNAPSHOT_STATUS_CORRUPT, reason="projection product count does not match manifest", manifest_id=manifest_id, generation_no=generation_no, checksum=checksum)

            query_all = bool(grade) or not requested or len(requested) > PROJECTION_SUBSET_SAFE_LIMIT
            if grade:
                statement = cursor.execute(
                    """
                    /* snapshot.projection.rows_by_grade */
                    SELECT product_code, occurrence_count_3m, frequency_grade, data_status, row_checksum
                    FROM snapshot.frequency_product
                    WHERE manifest_id=? AND frequency_grade=? ORDER BY product_code
                    """,
                    manifest_id, grade,
                )
            elif query_all:
                statement = cursor.execute(
                    """
                    /* snapshot.projection.rows_all */
                    SELECT product_code, occurrence_count_3m, frequency_grade, data_status, row_checksum
                    FROM snapshot.frequency_product WHERE manifest_id=? ORDER BY frequency_grade, product_code
                    """,
                    manifest_id,
                )
            else:
                placeholders = ", ".join("?" for _ in requested)
                statement = cursor.execute(
                    f"""
                    /* snapshot.projection.rows_subset */
                    SELECT product_code, occurrence_count_3m, frequency_grade, data_status, row_checksum
                    FROM snapshot.frequency_product
                    WHERE manifest_id=? AND product_code IN ({placeholders}) ORDER BY product_code
                    """,
                    manifest_id, *requested,
                )
            rows = [
                {"product_code": row[0], "occurrence_count_3m": row[1], "frequency_grade": row[2], "data_status": row[3], "row_checksum": row[4]}
                for row in statement.fetchall()
            ]
            try:
                if representation == RELATIONAL_FREQUENCY_REPRESENTATION:
                    validated = validate_relational_frequency_projection(
                        rows=rows, headers=headers, required_grade=grade,
                        require_complete=query_all and not bool(grade),
                    )
                else:
                    validated = validate_frequency_projection(
                        manifest_checksum=checksum, rows=rows, headers=headers,
                        required_grade=grade, require_complete=query_all and not bool(grade),
                    )
            except SnapshotContractError as exc:
                return FrequencyProjectionReadResult(status=SNAPSHOT_STATUS_CORRUPT, reason=str(exc), manifest_id=manifest_id, generation_no=generation_no, checksum=checksum)
            if requested and query_all:
                requested_set = set(requested)
                validated = tuple(row for row in validated if str(row["product_code"]) in requested_set)
            return FrequencyProjectionReadResult(status=SNAPSHOT_STATUS_READY, rows=validated, manifest_id=manifest_id, generation_no=generation_no, checksum=checksum)
        except Exception as exc:
            if _representation_column_is_absent(exc):
                # M003 is not yet installed: preserve the established legacy
                # full-payload reader rather than guessing a representation.
                return FrequencyProjectionReadResult(
                    status="legacy",
                    reason="relational representation migration is not applied",
                )
            if _projection_schema_is_absent(exc):
                return FrequencyProjectionReadResult(status="legacy", reason="projection migration is not applied")
            return FrequencyProjectionReadResult(status=SNAPSHOT_STATUS_CORRUPT, reason=_snapshot_exception_code(exc))
        finally:
            if conn is not None:
                conn.close()

    def resolve_latest_eligible_key(
        self,
        key: SnapshotKey,
        *,
        available_through: str,
    ) -> SnapshotKey | None:
        """Resolve the newest completed approved generation for an operating read.

        ``evaluation_month`` remains part of immutable generation identity.  It
        is intentionally omitted only from this selection predicate so a
        Dashboard read can keep using the newest completed basis rather than a
        pre-created future evaluation month.
        """
        as_of = str(available_through or "").strip()
        if len(as_of) != 8 or not as_of.isdecimal():
            raise ValueError("available_through must be YYYYMMDD")
        conn = None
        try:
            conn = self._reader_connection_factory()
            row = conn.cursor().execute(
                """
                /* snapshot.operating.resolve_latest */
                SELECT TOP 1 m.evaluation_month
                FROM snapshot.manifest AS m
                WHERE m.company_id=? AND m.snapshot_type=?
                  AND m.scope_fingerprint=? AND m.schema_version=? AND m.algorithm_version=?
                  AND m.status='published' AND m.approval_status='approved'
                  AND m.basis_to <= ?
                  AND m.source_watermark_status IN ('verified', 'unverified')
                ORDER BY m.basis_to DESC, m.evaluation_month DESC, m.generation_no DESC
                """,
                key.company_id,
                key.snapshot_type,
                key.scope_fingerprint,
                key.schema_version,
                key.algorithm_version,
                as_of,
            ).fetchone()
            if not row:
                return None
            return SnapshotKey(
                company_id=key.company_id,
                snapshot_type=key.snapshot_type,
                evaluation_month=str(row[0]),
                scope_fingerprint=key.scope_fingerprint,
                schema_version=key.schema_version,
                algorithm_version=key.algorithm_version,
            )
        finally:
            if conn is not None:
                conn.close()

    def read(self, key: SnapshotKey) -> SnapshotReadResult:
        # Native generations have no snapshot.payload row. Keep the legacy JSON
        # transport isolated instead of treating a missing payload as unavailable.
        representation_conn = self._reader_connection_factory()
        try:
            representation_cursor = representation_conn.cursor()
            representation_row = representation_cursor.execute(
                """SELECT TOP 1 manifest_id, generation_no, checksum, approval_status, approved_at, approved_by, approval_reason, storage_representation
                   FROM snapshot.manifest
                   WHERE company_id=? AND snapshot_type=? AND evaluation_month=?
                     AND scope_fingerprint=? AND schema_version=? AND algorithm_version=?
                     AND status='published' AND approval_status='approved'
                   ORDER BY generation_no DESC""",
                *_key_values(key),
            ).fetchone()
            if representation_row and str(representation_row[7] or "") == RELATIONAL_FREQUENCY_REPRESENTATION:
                return SnapshotReadResult(
                    status=SNAPSHOT_STATUS_READY, manifest_id=int(representation_row[0]), generation_no=int(representation_row[1]),
                    checksum=str(representation_row[2]), approval_status=str(representation_row[3]), approved_at=str(representation_row[4] or ""),
                    approved_by=str(representation_row[5] or ""), approval_reason=str(representation_row[6] or ""),
                    representation=RELATIONAL_FREQUENCY_REPRESENTATION,
                )
        except Exception as exc:
            if not _representation_column_is_absent(exc):
                raise
        finally:
            representation_conn.close()
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
                       p.payload_json,
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
            transport_payload_size = len(str(row[7]).encode("utf-8")) if row else 0
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
            payload_json = str(row[7])
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
