from __future__ import annotations

import calendar
from dataclasses import dataclass
from typing import Any, Callable

from app.services.dashboard_inventory_frequency_snapshot import (
    SnapshotContractError,
    relational_row_checksum,
)
from app.services.monthly_frequency_aggregate import (
    MONTHLY_FREQUENCY_ALGORITHM_VERSION,
    MONTHLY_FREQUENCY_SCHEMA_VERSION,
    PRODUCT_LIFECYCLE_ALGORITHM_VERSION,
    PRODUCT_LIFECYCLE_SCHEMA_VERSION,
    MonthlyFrequencyMaterialization,
    ProductLifecycleAuthority,
    build_monthly_frequency_materialization,
    build_product_lifecycle_authority,
)


DIAGNOSTIC_COLUMNS = (
    "diagnostic_contract_version", "source_row_count",
    "normal_positive_accepted_row_count", "normal_positive_duplicate_row_count",
    "normal_positive_conflicting_row_count", "normal_positive_missing_key_row_count",
    "normal_positive_nonintegral_row_count", "normal_nonpositive_row_count",
    "return_positive_row_count", "return_nonpositive_row_count", "other_tcode_row_count",
    "normal_positive_row_count", "distinct_normal_event_count", "conflicting_event_count",
    "ignored_product_event_count",
)


@dataclass(frozen=True)
class MonthlyFrequencyPublishResult:
    status: str
    approval_status: str
    generation_no: int
    manifest_id: int
    checksum: str
    no_op: bool = False


@dataclass(frozen=True)
class MonthlyFrequencyReadResult:
    status: str
    approval_status: str
    generation_no: int
    manifest_id: int
    materialization: MonthlyFrequencyMaterialization


@dataclass(frozen=True)
class ProductLifecyclePublishResult:
    status: str
    approval_status: str
    generation_no: int
    manifest_id: int
    checksum: str
    no_op: bool = False


@dataclass(frozen=True)
class ProductLifecycleReadResult:
    status: str
    approval_status: str
    generation_no: int
    manifest_id: int
    authority: ProductLifecycleAuthority


def _enable_fast_executemany(cursor: Any) -> None:
    if hasattr(cursor, "fast_executemany"):
        cursor.fast_executemany = True


def _month_bounds(month: str) -> tuple[str, str]:
    year = int(month[:4])
    month_number = int(month[4:])
    return f"{month}01", f"{month}{calendar.monthrange(year, month_number)[1]:02d}"


class SqlServerMonthlyFrequencyRepository:
    """Independent immutable monthly frequency generations; not used by runtime readers."""

    def __init__(
        self,
        *,
        reader_connection_factory: Callable[[], Any],
        writer_connection_factory: Callable[[], Any],
    ) -> None:
        self._reader_connection_factory = reader_connection_factory
        self._writer_connection_factory = writer_connection_factory

    @staticmethod
    def _identity(materialization: MonthlyFrequencyMaterialization) -> tuple[str, str, str, str, str]:
        return (
            materialization.company_id,
            materialization.basis_month,
            materialization.scope_fingerprint,
            materialization.monthly_schema_version,
            materialization.monthly_algorithm_version,
        )

    @staticmethod
    def _validate_for_storage(materialization: MonthlyFrequencyMaterialization) -> None:
        uses_authority = materialization.lifecycle_authority_manifest_id is not None
        if (
            materialization.monthly_schema_version != MONTHLY_FREQUENCY_SCHEMA_VERSION
            or materialization.monthly_algorithm_version != MONTHLY_FREQUENCY_ALGORITHM_VERSION
            or len(materialization.scope_fingerprint) != 64
            or len(materialization.product_universe_fingerprint) != 64
            or len(materialization.source_fingerprint) != 64
            or len(materialization.checksum) != 64
            or (
                uses_authority
                and (
                    int(materialization.lifecycle_authority_manifest_id or 0) <= 0
                    or len(materialization.lifecycle_authority_checksum) != 64
                    or bool(materialization.lifecycle)
                )
            )
            or (
                not uses_authority
                and (
                    bool(materialization.lifecycle_authority_checksum)
                    or len(materialization.lifecycle) != len(materialization.product_codes)
                    or {row.product_code for row in materialization.lifecycle} != set(materialization.product_codes)
                )
            )
        ):
            raise SnapshotContractError("monthly materialization is incomplete for persistence")

    def publish(
        self,
        materialization: MonthlyFrequencyMaterialization,
        *,
        created_by: str,
        force: bool = False,
    ) -> MonthlyFrequencyPublishResult:
        self._validate_for_storage(materialization)
        actor = str(created_by or "").strip()
        if not actor:
            raise ValueError("created_by is required")
        basis_from, basis_to = _month_bounds(materialization.basis_month)
        conn = self._writer_connection_factory()
        try:
            cursor = conn.cursor()
            latest = cursor.execute(
                """SELECT TOP 1 manifest_id, generation_no, checksum, status, approval_status
                   FROM snapshot.frequency_month_manifest WITH (UPDLOCK, HOLDLOCK)
                   WHERE company_id=? AND basis_month=? AND scope_fingerprint=?
                     AND schema_version=? AND algorithm_version=?
                   ORDER BY generation_no DESC""",
                *self._identity(materialization),
            ).fetchone()
            if latest and str(latest[2]) == materialization.checksum and str(latest[3]) in {"draft", "published"} and not force:
                conn.commit()
                return MonthlyFrequencyPublishResult(
                    str(latest[3]), str(latest[4]), int(latest[1]), int(latest[0]),
                    materialization.checksum, True,
                )
            generation = int(latest[1]) + 1 if latest else 1
            diagnostics = materialization.source_diagnostics
            inserted = cursor.execute(
                """INSERT INTO snapshot.frequency_month_manifest (
                       company_id, basis_month, basis_from, basis_to, scope_fingerprint,
                       product_universe_fingerprint, product_count, schema_version, algorithm_version,
                       generation_no, status, approval_status, source_watermark, source_watermark_status,
                       source_fingerprint, source_row_count, event_count, fact_count, lifecycle_count,
                       checksum, created_by, lifecycle_manifest_id, lifecycle_authority_checksum)
                   OUTPUT INSERTED.manifest_id
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                materialization.company_id, materialization.basis_month, basis_from, basis_to,
                materialization.scope_fingerprint, materialization.product_universe_fingerprint,
                len(materialization.product_codes), materialization.monthly_schema_version,
                materialization.monthly_algorithm_version, generation, materialization.source_watermark,
                materialization.source_watermark_status, materialization.source_fingerprint,
                int(diagnostics.get("source_row_count") or 0),
                int(diagnostics.get("distinct_normal_event_count") or 0), len(materialization.facts),
                len(materialization.lifecycle), materialization.checksum, actor,
                materialization.lifecycle_authority_manifest_id,
                materialization.lifecycle_authority_checksum or None,
            ).fetchone()
            manifest_id = int(inserted[0])
            if materialization.stock_codes:
                cursor.executemany(
                    "INSERT INTO snapshot.frequency_month_scope_stock (manifest_id, stock_code) VALUES (?, ?)",
                    [(manifest_id, code) for code in materialization.stock_codes],
                )
            fact_columns = ("product_code", "stock_code", "occurrence_count", "outbound_quantity", "outbound_day_count")
            fact_values = []
            for fact in materialization.facts:
                values = (fact.product_code, fact.stock_code, fact.occurrence_count, fact.outbound_quantity, fact.outbound_day_count)
                fact_values.append((manifest_id, *values, relational_row_checksum("frequency_month_fact", fact_columns, values)))
            if fact_values:
                _enable_fast_executemany(cursor)
                cursor.executemany(
                    """INSERT INTO snapshot.frequency_month_fact
                       (manifest_id, product_code, stock_code, occurrence_count, outbound_quantity, outbound_day_count, row_checksum)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    fact_values,
                )
            diagnostic_values = tuple(int(diagnostics.get(column) or 0) for column in DIAGNOSTIC_COLUMNS)
            cursor.execute(
                f"INSERT INTO snapshot.frequency_month_source_diagnostics (manifest_id, {', '.join(DIAGNOSTIC_COLUMNS)}, row_checksum) VALUES (?, {', '.join('?' for _ in DIAGNOSTIC_COLUMNS)}, ?)",
                manifest_id, *diagnostic_values,
                relational_row_checksum("frequency_month_source_diagnostics", DIAGNOSTIC_COLUMNS, diagnostic_values),
            )
            lifecycle_columns = (
                "product_code", "product_registered_date", "first_normal_inbound_date",
                "first_normal_inbound_month", "first_outbound_date", "lifecycle_status",
            )
            if materialization.lifecycle:
                _enable_fast_executemany(cursor)
                cursor.executemany(
                    """INSERT INTO snapshot.frequency_month_lifecycle
                       (manifest_id, product_code, product_registered_date, first_normal_inbound_date,
                        first_normal_inbound_month, first_outbound_date, lifecycle_status, row_checksum)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (
                            manifest_id, *(values := (
                                row.product_code, row.product_registered_date, row.first_normal_inbound_date,
                                row.first_normal_inbound_month, row.first_outbound_date, row.lifecycle_status,
                            )), relational_row_checksum("frequency_month_lifecycle", lifecycle_columns, values),
                        )
                        for row in materialization.lifecycle
                    ],
                )
            conn.commit()
            return MonthlyFrequencyPublishResult("draft", "pending", generation, manifest_id, materialization.checksum)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def approve_checked(
        self,
        materialization: MonthlyFrequencyMaterialization,
        generation_no: int,
        *,
        expected_checksum: str,
        approved_by: str,
        approval_reason: str,
    ) -> MonthlyFrequencyPublishResult:
        actor = str(approved_by or "").strip()
        reason = str(approval_reason or "").strip()
        checksum = str(expected_checksum or "").strip().lower()
        if not actor or not reason or checksum != materialization.checksum:
            raise ValueError("approval actor, reason, and exact checksum are required")
        conn = self._writer_connection_factory()
        try:
            cursor = conn.cursor()
            row = cursor.execute(
                """SELECT manifest_id, status, approval_status, checksum
                   FROM snapshot.frequency_month_manifest WITH (UPDLOCK, HOLDLOCK)
                   WHERE company_id=? AND basis_month=? AND scope_fingerprint=?
                     AND schema_version=? AND algorithm_version=? AND generation_no=?""",
                *self._identity(materialization), int(generation_no),
            ).fetchone()
            if not row or str(row[1]) != "draft" or str(row[2]) != "pending" or str(row[3]) != checksum:
                raise ValueError("only the exact pending monthly generation can be approved")
            stored = self._load(cursor, materialization, int(generation_no))
            if stored.materialization != materialization:
                raise ValueError("stored monthly generation failed exact integrity validation")
            manifest_id = int(row[0])
            cursor.execute(
                """UPDATE snapshot.frequency_month_manifest
                   SET status='superseded', superseded_at=SYSUTCDATETIME()
                   WHERE company_id=? AND basis_month=? AND scope_fingerprint=?
                     AND schema_version=? AND algorithm_version=? AND status='published'
                     AND manifest_id<>?""",
                *self._identity(materialization), manifest_id,
            )
            cursor.execute(
                """UPDATE snapshot.frequency_month_manifest
                   SET status='published', approval_status='approved', approved_at=SYSUTCDATETIME(),
                       approved_by=?, approval_reason=?, published_at=SYSUTCDATETIME()
                   WHERE manifest_id=?""",
                actor, reason, manifest_id,
            )
            conn.commit()
            return MonthlyFrequencyPublishResult("published", "approved", int(generation_no), manifest_id, checksum)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def read_current(self, reference: MonthlyFrequencyMaterialization) -> MonthlyFrequencyReadResult:
        conn = self._reader_connection_factory()
        try:
            cursor = conn.cursor()
            row = cursor.execute(
                """SELECT TOP 1 generation_no FROM snapshot.frequency_month_manifest
                   WHERE company_id=? AND basis_month=? AND scope_fingerprint=?
                     AND schema_version=? AND algorithm_version=?
                     AND status='published' AND approval_status='approved'
                   ORDER BY generation_no DESC""",
                *self._identity(reference),
            ).fetchone()
            if not row:
                raise LookupError("approved monthly materialization is missing")
            return self._load(cursor, reference, int(row[0]))
        finally:
            conn.close()

    def read_generation(
        self,
        reference: MonthlyFrequencyMaterialization,
        generation_no: int,
    ) -> MonthlyFrequencyReadResult:
        conn = self._reader_connection_factory()
        try:
            return self._load(conn.cursor(), reference, int(generation_no))
        finally:
            conn.close()

    def _load(self, cursor: Any, reference: MonthlyFrequencyMaterialization, generation_no: int) -> MonthlyFrequencyReadResult:
        manifest = cursor.execute(
            """SELECT manifest_id, status, approval_status, source_watermark, source_watermark_status,
                      source_fingerprint, product_universe_fingerprint, product_count, fact_count,
                      lifecycle_count, checksum, lifecycle_manifest_id, lifecycle_authority_checksum
               FROM snapshot.frequency_month_manifest
               WHERE company_id=? AND basis_month=? AND scope_fingerprint=?
                 AND schema_version=? AND algorithm_version=? AND generation_no=?""",
            *self._identity(reference), int(generation_no),
        ).fetchone()
        if not manifest:
            raise LookupError("monthly generation not found")
        manifest_id = int(manifest[0])
        stocks = tuple(str(row[0]) for row in cursor.execute(
            "SELECT stock_code FROM snapshot.frequency_month_scope_stock WHERE manifest_id=? ORDER BY stock_code",
            manifest_id,
        ).fetchall())
        fact_db_rows = cursor.execute(
                """SELECT product_code, stock_code, occurrence_count, outbound_quantity, outbound_day_count, row_checksum
                   FROM snapshot.frequency_month_fact WHERE manifest_id=? ORDER BY product_code, stock_code""",
                manifest_id,
            ).fetchall()
        fact_columns = ("product_code", "stock_code", "occurrence_count", "outbound_quantity", "outbound_day_count")
        if any(relational_row_checksum("frequency_month_fact", fact_columns, tuple(row[:5])) != str(row[5]) for row in fact_db_rows):
            raise SnapshotContractError("stored monthly fact row checksum mismatch")
        fact_rows = [
            {
                "month": reference.basis_month, "product_code": str(row[0]), "stock_code": str(row[1]),
                "occurrence_count": int(row[2]), "outbound_quantity": int(row[3]), "outbound_day_count": int(row[4]),
            }
            for row in fact_db_rows
        ]
        diagnostic_row = cursor.execute(
            f"SELECT {', '.join(DIAGNOSTIC_COLUMNS)}, row_checksum FROM snapshot.frequency_month_source_diagnostics WHERE manifest_id=?",
            manifest_id,
        ).fetchone()
        if not diagnostic_row or relational_row_checksum("frequency_month_source_diagnostics", DIAGNOSTIC_COLUMNS, tuple(diagnostic_row[:-1])) != str(diagnostic_row[-1]):
            raise SnapshotContractError("stored monthly diagnostics row checksum mismatch")
        lifecycle_manifest_id = int(manifest[11]) if manifest[11] is not None else None
        lifecycle_authority_checksum = str(manifest[12] or "")
        lifecycle_rows: list[dict[str, Any]] = []
        if lifecycle_manifest_id is None:
            lifecycle_db_rows = cursor.execute(
                    """SELECT product_code, product_registered_date, first_normal_inbound_date,
                              first_normal_inbound_month, first_outbound_date, lifecycle_status, row_checksum
                       FROM snapshot.frequency_month_lifecycle WHERE manifest_id=? ORDER BY product_code""",
                    manifest_id,
                ).fetchall()
            lifecycle_columns = (
                "product_code", "product_registered_date", "first_normal_inbound_date",
                "first_normal_inbound_month", "first_outbound_date", "lifecycle_status",
            )
            if any(relational_row_checksum("frequency_month_lifecycle", lifecycle_columns, tuple(row[:6])) != str(row[6]) for row in lifecycle_db_rows):
                raise SnapshotContractError("stored monthly lifecycle row checksum mismatch")
            lifecycle_rows = [
                {
                    "product_code": str(row[0]), "product_registered_date": row[1],
                    "first_normal_inbound_date": row[2], "first_normal_inbound_month": row[3],
                    "first_outbound_date": row[4],
                }
                for row in lifecycle_db_rows
            ]
            products = tuple(row["product_code"] for row in lifecycle_rows)
        else:
            authority = cursor.execute(
                """SELECT company_id, scope_fingerprint, product_universe_fingerprint, product_count,
                          status, approval_status, checksum
                   FROM snapshot.frequency_lifecycle_manifest WHERE manifest_id=?""",
                lifecycle_manifest_id,
            ).fetchone()
            if (
                not authority
                or str(authority[0]) != reference.company_id
                or str(authority[1]) != reference.scope_fingerprint
                or str(authority[2]) != str(manifest[6])
                or int(authority[3]) != int(manifest[7])
                or str(authority[4]) != "published"
                or str(authority[5]) != "approved"
                or str(authority[6]) != lifecycle_authority_checksum
            ):
                raise SnapshotContractError("monthly lifecycle authority is not eligible")
            products = reference.product_codes
            if len(products) != int(manifest[7]) or reference.product_universe_fingerprint != str(manifest[6]):
                raise SnapshotContractError("monthly reference product universe does not match lifecycle authority")
        rebuilt = build_monthly_frequency_materialization(
            company_id=reference.company_id, basis_month=reference.basis_month, stock_codes=stocks,
            monthly_rows=fact_rows,
            source_diagnostics=dict(zip(DIAGNOSTIC_COLUMNS, diagnostic_row[:-1])),
            source_watermark=manifest[3], source_watermark_status=str(manifest[4]),
            product_codes=products, lifecycle_rows=lifecycle_rows,
            lifecycle_authority_manifest_id=lifecycle_manifest_id,
            lifecycle_authority_checksum=lifecycle_authority_checksum,
        )
        integrity = {
            "source_fingerprint": rebuilt.source_fingerprint == str(manifest[5]),
            "product_universe_fingerprint": rebuilt.product_universe_fingerprint == str(manifest[6]),
            "product_count": len(products) == int(manifest[7]),
            "fact_count": len(rebuilt.facts) == int(manifest[8]),
            "lifecycle_count": len(rebuilt.lifecycle) == int(manifest[9]),
            "checksum": rebuilt.checksum == str(manifest[10]),
        }
        if not all(integrity.values()):
            failed = ",".join(key for key, ok in integrity.items() if not ok)
            raise SnapshotContractError(
                f"stored monthly materialization integrity mismatch: {failed}; "
                f"source={rebuilt.source_fingerprint}/{str(manifest[5])}; "
                f"checksum={rebuilt.checksum}/{str(manifest[10])}"
            )
        return MonthlyFrequencyReadResult(
            str(manifest[1]), str(manifest[2]), int(generation_no), manifest_id, rebuilt,
        )


class SqlServerProductLifecycleRepository:
    """Immutable product lifecycle authority shared by approved monthly generations."""

    def __init__(
        self,
        *,
        reader_connection_factory: Callable[[], Any],
        writer_connection_factory: Callable[[], Any],
    ) -> None:
        self._reader_connection_factory = reader_connection_factory
        self._writer_connection_factory = writer_connection_factory

    @staticmethod
    def _identity(authority: ProductLifecycleAuthority) -> tuple[str, str, str, str]:
        return (
            authority.company_id,
            authority.scope_fingerprint,
            authority.schema_version,
            authority.algorithm_version,
        )

    @staticmethod
    def _validate(authority: ProductLifecycleAuthority) -> None:
        if (
            authority.schema_version != PRODUCT_LIFECYCLE_SCHEMA_VERSION
            or authority.algorithm_version != PRODUCT_LIFECYCLE_ALGORITHM_VERSION
            or len(authority.scope_fingerprint) != 64
            or len(authority.product_universe_fingerprint) != 64
            or len(authority.source_fingerprint) != 64
            or len(authority.checksum) != 64
            or len(authority.lifecycle) != len(authority.product_codes)
            or {row.product_code for row in authority.lifecycle} != set(authority.product_codes)
        ):
            raise SnapshotContractError("product lifecycle authority is incomplete")

    def publish(
        self,
        authority: ProductLifecycleAuthority,
        *,
        created_by: str,
        force: bool = False,
    ) -> ProductLifecyclePublishResult:
        self._validate(authority)
        actor = str(created_by or "").strip()
        if not actor:
            raise ValueError("created_by is required")
        conn = self._writer_connection_factory()
        try:
            cursor = conn.cursor()
            latest = cursor.execute(
                """SELECT TOP 1 manifest_id, generation_no, checksum, status, approval_status
                   FROM snapshot.frequency_lifecycle_manifest WITH (UPDLOCK, HOLDLOCK)
                   WHERE company_id=? AND scope_fingerprint=? AND schema_version=? AND algorithm_version=?
                   ORDER BY generation_no DESC""",
                *self._identity(authority),
            ).fetchone()
            if latest and str(latest[2]) == authority.checksum and str(latest[3]) in {"draft", "published"} and not force:
                conn.commit()
                return ProductLifecyclePublishResult(
                    str(latest[3]), str(latest[4]), int(latest[1]), int(latest[0]), authority.checksum, True,
                )
            generation = int(latest[1]) + 1 if latest else 1
            review_count = sum(1 for row in authority.lifecycle if row.review_required)
            insufficient_count = sum(1 for row in authority.lifecycle if row.quality_status == "insufficient")
            inserted = cursor.execute(
                """INSERT INTO snapshot.frequency_lifecycle_manifest (
                       company_id, scope_fingerprint, product_universe_fingerprint, product_count,
                       schema_version, algorithm_version, generation_no, status, approval_status,
                       source_watermark, source_watermark_status, source_fingerprint,
                       review_required_count, insufficient_count, checksum, created_by)
                   OUTPUT INSERTED.manifest_id
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', 'pending', ?, ?, ?, ?, ?, ?, ?)""",
                authority.company_id, authority.scope_fingerprint, authority.product_universe_fingerprint,
                len(authority.product_codes), authority.schema_version, authority.algorithm_version,
                generation, authority.source_watermark, authority.source_watermark_status,
                authority.source_fingerprint, review_count, insufficient_count, authority.checksum, actor,
            ).fetchone()
            manifest_id = int(inserted[0])
            if authority.stock_codes:
                cursor.executemany(
                    "INSERT INTO snapshot.frequency_lifecycle_scope_stock (manifest_id, stock_code) VALUES (?, ?)",
                    [(manifest_id, code) for code in authority.stock_codes],
                )
            columns = (
                "product_code", "product_registered_date", "first_normal_inbound_date",
                "first_normal_inbound_month", "first_outbound_date", "registered_after_inbound",
                "outbound_before_inbound", "long_registration_to_inbound_gap",
                "registration_to_inbound_days", "review_required", "quality_status",
            )
            values = []
            for row in authority.lifecycle:
                row_values = (
                    row.product_code, row.product_registered_date, row.first_normal_inbound_date,
                    row.first_normal_inbound_month, row.first_outbound_date, row.registered_after_inbound,
                    row.outbound_before_inbound, row.long_registration_to_inbound_gap,
                    row.registration_to_inbound_days, row.review_required, row.quality_status,
                )
                values.append((manifest_id, *row_values, relational_row_checksum(
                    "frequency_lifecycle_product", columns, row_values,
                )))
            _enable_fast_executemany(cursor)
            cursor.executemany(
                """INSERT INTO snapshot.frequency_lifecycle_product (
                       manifest_id, product_code, product_registered_date, first_normal_inbound_date,
                       first_normal_inbound_month, first_outbound_date, registered_after_inbound,
                       outbound_before_inbound, long_registration_to_inbound_gap,
                       registration_to_inbound_days, review_required, quality_status, row_checksum)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
            conn.commit()
            return ProductLifecyclePublishResult("draft", "pending", generation, manifest_id, authority.checksum)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def approve_checked(
        self,
        authority: ProductLifecycleAuthority,
        generation_no: int,
        *,
        expected_checksum: str,
        approved_by: str,
        approval_reason: str,
    ) -> ProductLifecyclePublishResult:
        actor = str(approved_by or "").strip()
        reason = str(approval_reason or "").strip()
        checksum = str(expected_checksum or "").strip().lower()
        if not actor or not reason or checksum != authority.checksum:
            raise ValueError("approval actor, reason, and exact checksum are required")
        conn = self._writer_connection_factory()
        try:
            cursor = conn.cursor()
            row = cursor.execute(
                """SELECT manifest_id, status, approval_status, checksum
                   FROM snapshot.frequency_lifecycle_manifest WITH (UPDLOCK, HOLDLOCK)
                   WHERE company_id=? AND scope_fingerprint=? AND schema_version=?
                     AND algorithm_version=? AND generation_no=?""",
                *self._identity(authority), int(generation_no),
            ).fetchone()
            if not row or str(row[1]) != "draft" or str(row[2]) != "pending" or str(row[3]) != checksum:
                raise ValueError("only the exact pending lifecycle generation can be approved")
            stored = self._load(cursor, authority, int(generation_no))
            if stored.authority != authority:
                raise ValueError("stored lifecycle generation failed exact integrity validation")
            manifest_id = int(row[0])
            cursor.execute(
                """UPDATE snapshot.frequency_lifecycle_manifest
                   SET status='superseded', superseded_at=SYSUTCDATETIME()
                   WHERE company_id=? AND scope_fingerprint=? AND schema_version=?
                     AND algorithm_version=? AND status='published' AND manifest_id<>?""",
                *self._identity(authority), manifest_id,
            )
            cursor.execute(
                """UPDATE snapshot.frequency_lifecycle_manifest
                   SET status='published', approval_status='approved', approved_at=SYSUTCDATETIME(),
                       approved_by=?, approval_reason=?, published_at=SYSUTCDATETIME()
                   WHERE manifest_id=?""",
                actor, reason, manifest_id,
            )
            conn.commit()
            return ProductLifecyclePublishResult("published", "approved", int(generation_no), manifest_id, checksum)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def read_current(self, reference: ProductLifecycleAuthority) -> ProductLifecycleReadResult:
        conn = self._reader_connection_factory()
        try:
            cursor = conn.cursor()
            row = cursor.execute(
                """SELECT TOP 1 generation_no FROM snapshot.frequency_lifecycle_manifest
                   WHERE company_id=? AND scope_fingerprint=? AND schema_version=? AND algorithm_version=?
                     AND status='published' AND approval_status='approved'
                   ORDER BY generation_no DESC""",
                *self._identity(reference),
            ).fetchone()
            if not row:
                raise LookupError("approved lifecycle authority is missing")
            return self._load(cursor, reference, int(row[0]))
        finally:
            conn.close()

    def _load(
        self,
        cursor: Any,
        reference: ProductLifecycleAuthority,
        generation_no: int,
    ) -> ProductLifecycleReadResult:
        manifest = cursor.execute(
            """SELECT manifest_id, status, approval_status, source_watermark, source_watermark_status,
                      source_fingerprint, product_universe_fingerprint, product_count,
                      review_required_count, insufficient_count, checksum
               FROM snapshot.frequency_lifecycle_manifest
               WHERE company_id=? AND scope_fingerprint=? AND schema_version=?
                 AND algorithm_version=? AND generation_no=?""",
            *self._identity(reference), int(generation_no),
        ).fetchone()
        if not manifest:
            raise LookupError("lifecycle generation not found")
        manifest_id = int(manifest[0])
        stocks = tuple(str(row[0]) for row in cursor.execute(
            "SELECT stock_code FROM snapshot.frequency_lifecycle_scope_stock WHERE manifest_id=? ORDER BY stock_code",
            manifest_id,
        ).fetchall())
        rows = cursor.execute(
            """SELECT product_code, product_registered_date, first_normal_inbound_date,
                      first_normal_inbound_month, first_outbound_date, registered_after_inbound,
                      outbound_before_inbound, long_registration_to_inbound_gap,
                      registration_to_inbound_days, review_required, quality_status, row_checksum
               FROM snapshot.frequency_lifecycle_product WHERE manifest_id=? ORDER BY product_code""",
            manifest_id,
        ).fetchall()
        columns = (
            "product_code", "product_registered_date", "first_normal_inbound_date",
            "first_normal_inbound_month", "first_outbound_date", "registered_after_inbound",
            "outbound_before_inbound", "long_registration_to_inbound_gap",
            "registration_to_inbound_days", "review_required", "quality_status",
        )
        if any(relational_row_checksum("frequency_lifecycle_product", columns, tuple(row[:11])) != str(row[11]) for row in rows):
            raise SnapshotContractError("stored lifecycle row checksum mismatch")
        lifecycle_rows = [
            {
                "product_code": str(row[0]),
                "product_registered_date": row[1],
                "first_normal_inbound_date": row[2],
                "first_outbound_date": row[4],
            }
            for row in rows
        ]
        products = tuple(row["product_code"] for row in lifecycle_rows)
        rebuilt = build_product_lifecycle_authority(
            company_id=reference.company_id,
            stock_codes=stocks,
            product_codes=products,
            lifecycle_rows=lifecycle_rows,
            source_watermark=manifest[3],
            source_watermark_status=str(manifest[4]),
        )
        integrity = {
            "source_fingerprint": rebuilt.source_fingerprint == str(manifest[5]),
            "product_universe_fingerprint": rebuilt.product_universe_fingerprint == str(manifest[6]),
            "product_count": len(products) == int(manifest[7]),
            "review_required_count": sum(1 for row in rebuilt.lifecycle if row.review_required) == int(manifest[8]),
            "insufficient_count": sum(1 for row in rebuilt.lifecycle if row.quality_status == "insufficient") == int(manifest[9]),
            "checksum": rebuilt.checksum == str(manifest[10]),
        }
        if not all(integrity.values()):
            raise SnapshotContractError(
                "stored lifecycle authority integrity mismatch: "
                + ",".join(key for key, ok in integrity.items() if not ok)
            )
        return ProductLifecycleReadResult(
            str(manifest[1]), str(manifest[2]), int(generation_no), manifest_id, rebuilt,
        )
