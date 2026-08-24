from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any


MIGRATION_SESSION_OPTIONS = (
    ("ANSI_NULLS", 1),
    ("ANSI_PADDING", 1),
    ("ANSI_WARNINGS", 1),
    ("ARITHABORT", 1),
    ("CONCAT_NULL_YIELDS_NULL", 1),
    ("QUOTED_IDENTIFIER", 1),
    ("NUMERIC_ROUNDABORT", 0),
)
MIGRATION_SESSION_SET_SQL = """
SET ANSI_NULLS ON;
SET ANSI_PADDING ON;
SET ANSI_WARNINGS ON;
SET ARITHABORT ON;
SET CONCAT_NULL_YIELDS_NULL ON;
SET QUOTED_IDENTIFIER ON;
SET NUMERIC_ROUNDABORT OFF;
"""
MIGRATION_SESSION_PROPERTIES_SQL = """
SELECT
    CAST(SESSIONPROPERTY('ANSI_NULLS') AS int),
    CAST(SESSIONPROPERTY('ANSI_PADDING') AS int),
    CAST(SESSIONPROPERTY('ANSI_WARNINGS') AS int),
    CAST(SESSIONPROPERTY('ARITHABORT') AS int),
    CAST(SESSIONPROPERTY('CONCAT_NULL_YIELDS_NULL') AS int),
    CAST(SESSIONPROPERTY('QUOTED_IDENTIFIER') AS int),
    CAST(SESSIONPROPERTY('NUMERIC_ROUNDABORT') AS int)
"""


class SnapshotMigrationError(RuntimeError):
    """Migration failure with its exact transactional stage preserved."""

    def __init__(self, migration_id: str, cause: Exception) -> None:
        self.migration_id = str(migration_id)
        self.cause = cause
        super().__init__(f"snapshot migration failed at {self.migration_id}: {type(cause).__name__}")

@dataclass(frozen=True)
class SnapshotMigration:
    migration_id: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


ENSURE_LEDGER_SQL = """
IF SCHEMA_ID(N'snapshot') IS NULL EXEC(N'CREATE SCHEMA snapshot');
IF OBJECT_ID(N'snapshot.schema_migrations', N'U') IS NULL
BEGIN
    CREATE TABLE snapshot.schema_migrations (
        migration_id NVARCHAR(100) NOT NULL PRIMARY KEY,
        migration_checksum CHAR(64) NOT NULL,
        applied_at DATETIME2(3) NOT NULL CONSTRAINT DF_snapshot_migrations_applied_at DEFAULT SYSUTCDATETIME(),
        applied_by NVARCHAR(128) NOT NULL
    );
END;
"""


MIGRATION_001_SQL = """
CREATE TABLE snapshot.manifest (
    manifest_id BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_snapshot_manifest PRIMARY KEY,
    company_id NVARCHAR(128) NOT NULL,
    snapshot_type NVARCHAR(100) NOT NULL,
    evaluation_month CHAR(6) NOT NULL,
    basis_from CHAR(8) NOT NULL,
    basis_to CHAR(8) NOT NULL,
    scope_fingerprint CHAR(64) NOT NULL,
    schema_version NVARCHAR(32) NOT NULL,
    algorithm_version NVARCHAR(64) NOT NULL,
    generation_no INT NOT NULL,
    status VARCHAR(20) NOT NULL,
    approval_status VARCHAR(20) NOT NULL,
    source_watermark NVARCHAR(256) NULL,
    source_watermark_status VARCHAR(24) NOT NULL,
    source_fingerprint CHAR(64) NOT NULL,
    item_count BIGINT NOT NULL,
    payload_size BIGINT NOT NULL,
    checksum CHAR(64) NOT NULL,
    created_at DATETIME2(3) NOT NULL CONSTRAINT DF_snapshot_manifest_created_at DEFAULT SYSUTCDATETIME(),
    created_by NVARCHAR(128) NOT NULL,
    approved_at DATETIME2(3) NULL,
    approved_by NVARCHAR(128) NULL,
    approval_reason NVARCHAR(500) NULL,
    published_at DATETIME2(3) NULL,
    superseded_at DATETIME2(3) NULL,
    invalidated_at DATETIME2(3) NULL,
    invalidated_by NVARCHAR(128) NULL,
    failure_reason NVARCHAR(1000) NULL,
    CONSTRAINT UQ_snapshot_manifest_generation UNIQUE (
        company_id, snapshot_type, evaluation_month, scope_fingerprint,
        schema_version, algorithm_version, generation_no
    ),
    CONSTRAINT CK_snapshot_manifest_status CHECK (
        status IN ('draft','published','superseded','invalidated','failed')
    ),
    CONSTRAINT CK_snapshot_manifest_approval CHECK (
        approval_status IN ('pending','approved','rejected')
    ),
    CONSTRAINT CK_snapshot_manifest_counts CHECK (
        generation_no > 0 AND item_count >= 0 AND payload_size > 0
    )
);

CREATE UNIQUE INDEX UX_snapshot_manifest_one_published
ON snapshot.manifest (
    company_id, snapshot_type, evaluation_month, scope_fingerprint,
    schema_version, algorithm_version
)
WHERE status = 'published';

CREATE INDEX IX_snapshot_manifest_lookup
ON snapshot.manifest (
    company_id, snapshot_type, evaluation_month, scope_fingerprint,
    schema_version, algorithm_version, status, generation_no DESC
)
INCLUDE (manifest_id, approval_status, checksum, payload_size, approved_at, approved_by);

CREATE INDEX IX_snapshot_manifest_operations
ON snapshot.manifest (status, created_at, company_id, evaluation_month);

CREATE TABLE snapshot.payload (
    manifest_id BIGINT NOT NULL CONSTRAINT PK_snapshot_payload PRIMARY KEY,
    payload_json NVARCHAR(MAX) NOT NULL,
    storage_checksum CHAR(64) NOT NULL,
    payload_size BIGINT NOT NULL,
    created_at DATETIME2(3) NOT NULL CONSTRAINT DF_snapshot_payload_created_at DEFAULT SYSUTCDATETIME(),
    CONSTRAINT FK_snapshot_payload_manifest FOREIGN KEY (manifest_id)
        REFERENCES snapshot.manifest(manifest_id),
    CONSTRAINT CK_snapshot_payload_size CHECK (payload_size > 0)
);

IF DATABASE_PRINCIPAL_ID(N'ssai_snapshot_reader') IS NULL
    CREATE ROLE [ssai_snapshot_reader] AUTHORIZATION [dbo];
IF DATABASE_PRINCIPAL_ID(N'ssai_snapshot_writer') IS NULL
    CREATE ROLE [ssai_snapshot_writer] AUTHORIZATION [dbo];

GRANT SELECT ON SCHEMA::snapshot TO [ssai_snapshot_reader];
GRANT SELECT ON SCHEMA::snapshot TO [ssai_snapshot_writer];
GRANT INSERT, UPDATE ON OBJECT::snapshot.manifest TO [ssai_snapshot_writer];
GRANT INSERT ON OBJECT::snapshot.payload TO [ssai_snapshot_writer];
"""


MIGRATION_002_SQL = """
CREATE TABLE snapshot.frequency_product (
    manifest_id BIGINT NOT NULL,
    product_code NVARCHAR(128) NOT NULL,
    occurrence_count_3m BIGINT NOT NULL,
    frequency_grade CHAR(1) NOT NULL,
    data_status VARCHAR(20) NOT NULL,
    row_checksum CHAR(64) NOT NULL,
    CONSTRAINT PK_snapshot_frequency_product PRIMARY KEY (manifest_id, product_code),
    CONSTRAINT FK_snapshot_frequency_product_manifest FOREIGN KEY (manifest_id)
        REFERENCES snapshot.manifest(manifest_id),
    CONSTRAINT CK_snapshot_frequency_product_count CHECK (occurrence_count_3m >= 0),
    CONSTRAINT CK_snapshot_frequency_product_grade CHECK (frequency_grade IN ('A','B','C','D','E','X')),
    CONSTRAINT CK_snapshot_frequency_product_status CHECK (data_status = 'ready')
);

CREATE INDEX IX_snapshot_frequency_product_grade
ON snapshot.frequency_product (manifest_id, frequency_grade, product_code)
INCLUDE (occurrence_count_3m, data_status, row_checksum);

CREATE TABLE snapshot.frequency_projection (
    manifest_id BIGINT NOT NULL,
    frequency_grade CHAR(1) NOT NULL,
    expected_product_count BIGINT NOT NULL,
    projection_checksum CHAR(64) NOT NULL,
    CONSTRAINT PK_snapshot_frequency_projection PRIMARY KEY (manifest_id, frequency_grade),
    CONSTRAINT FK_snapshot_frequency_projection_manifest FOREIGN KEY (manifest_id)
        REFERENCES snapshot.manifest(manifest_id),
    CONSTRAINT CK_snapshot_frequency_projection_grade CHECK (frequency_grade IN ('A','B','C','D','E','X')),
    CONSTRAINT CK_snapshot_frequency_projection_count CHECK (expected_product_count >= 0)
);

GRANT SELECT ON OBJECT::snapshot.frequency_product TO [ssai_snapshot_reader];
GRANT SELECT ON OBJECT::snapshot.frequency_projection TO [ssai_snapshot_reader];
GRANT SELECT ON OBJECT::snapshot.frequency_product TO [ssai_snapshot_writer];
GRANT SELECT ON OBJECT::snapshot.frequency_projection TO [ssai_snapshot_writer];
GRANT INSERT ON OBJECT::snapshot.frequency_product TO [ssai_snapshot_writer];
GRANT INSERT ON OBJECT::snapshot.frequency_projection TO [ssai_snapshot_writer];
"""


MIGRATION_003_SQL = """
ALTER TABLE snapshot.manifest ADD storage_representation VARCHAR(32) NOT NULL
    CONSTRAINT DF_snapshot_manifest_representation DEFAULT 'legacy_json_v1';
ALTER TABLE snapshot.manifest ADD scope_mode VARCHAR(12) NULL;
ALTER TABLE snapshot.manifest ADD fingerprint_contract_version INT NULL;
ALTER TABLE snapshot.manifest ADD fingerprint_mode VARCHAR(32) NULL;

-- SQL Server compiles a batch against its pre-ALTER schema.  Keep these
-- constraints in a separately compiled statement because they use the new
-- storage_representation column added above.
EXEC(N'
ALTER TABLE snapshot.manifest DROP CONSTRAINT CK_snapshot_manifest_counts;
ALTER TABLE snapshot.manifest ADD CONSTRAINT CK_snapshot_manifest_counts CHECK (
    generation_no > 0 AND item_count >= 0 AND payload_size >= 0
    AND ((storage_representation = ''legacy_json_v1'' AND payload_size > 0)
      OR (storage_representation = ''relational_frequency_v1'' AND payload_size = 0))
);
ALTER TABLE snapshot.manifest ADD CONSTRAINT CK_snapshot_manifest_representation CHECK (
    storage_representation IN (''legacy_json_v1'', ''relational_frequency_v1'')
);
');

CREATE TABLE snapshot.frequency_monthly_activity (
    manifest_id BIGINT NOT NULL,
    month CHAR(6) NOT NULL,
    product_code NVARCHAR(128) NOT NULL,
    stock_code NVARCHAR(128) NOT NULL,
    occurrence_count BIGINT NOT NULL,
    outbound_quantity BIGINT NOT NULL,
    outbound_day_count BIGINT NOT NULL,
    row_checksum CHAR(64) NOT NULL,
    CONSTRAINT PK_snapshot_frequency_monthly_activity PRIMARY KEY (manifest_id, month, product_code, stock_code),
    CONSTRAINT FK_snapshot_frequency_monthly_activity_manifest FOREIGN KEY (manifest_id)
        REFERENCES snapshot.manifest(manifest_id),
    CONSTRAINT CK_snapshot_frequency_monthly_activity_values CHECK (
        occurrence_count > 0 AND outbound_quantity > 0 AND outbound_day_count > 0 AND outbound_day_count <= occurrence_count
    )
);

CREATE TABLE snapshot.frequency_scope_stock (
    manifest_id BIGINT NOT NULL,
    stock_code NVARCHAR(128) NOT NULL,
    CONSTRAINT PK_snapshot_frequency_scope_stock PRIMARY KEY (manifest_id, stock_code),
    CONSTRAINT FK_snapshot_frequency_scope_stock_manifest FOREIGN KEY (manifest_id)
        REFERENCES snapshot.manifest(manifest_id)
);

CREATE TABLE snapshot.frequency_source_contract (
    manifest_id BIGINT NOT NULL CONSTRAINT PK_snapshot_frequency_source_contract PRIMARY KEY,
    source_table NVARCHAR(128) NOT NULL,
    io_gu_gcode CHAR(4) NOT NULL,
    normal_tcode_from CHAR(3) NOT NULL,
    normal_tcode_to CHAR(3) NOT NULL,
    event_key_fields NVARCHAR(256) NOT NULL,
    positive_quantity_expression NVARCHAR(256) NOT NULL,
    return_tcode_from CHAR(3) NOT NULL,
    return_tcode_to CHAR(3) NOT NULL,
    returns_are_netted BIT NOT NULL,
    flag_exclusion_fields NVARCHAR(256) NOT NULL,
    non_exclusion_flag_fields NVARCHAR(512) NOT NULL,
    universe_mode NVARCHAR(128) NOT NULL,
    dashboard_product_filters NVARCHAR(128) NOT NULL,
    include_rd04_del_flag_e BIT NOT NULL,
    fingerprint_contract_version INT NOT NULL,
    fingerprint_mode NVARCHAR(64) NOT NULL,
    contract_checksum CHAR(64) NOT NULL,
    CONSTRAINT FK_snapshot_frequency_source_contract_manifest FOREIGN KEY (manifest_id)
        REFERENCES snapshot.manifest(manifest_id)
);

CREATE TABLE snapshot.frequency_source_diagnostics (
    manifest_id BIGINT NOT NULL CONSTRAINT PK_snapshot_frequency_source_diagnostics PRIMARY KEY,
    diagnostic_contract_version INT NOT NULL,
    source_row_count BIGINT NOT NULL,
    normal_positive_accepted_row_count BIGINT NOT NULL,
    normal_positive_duplicate_row_count BIGINT NOT NULL,
    normal_positive_conflicting_row_count BIGINT NOT NULL,
    normal_positive_missing_key_row_count BIGINT NOT NULL,
    normal_positive_nonintegral_row_count BIGINT NOT NULL,
    normal_nonpositive_row_count BIGINT NOT NULL,
    return_positive_row_count BIGINT NOT NULL,
    return_nonpositive_row_count BIGINT NOT NULL,
    other_tcode_row_count BIGINT NOT NULL,
    normal_positive_row_count BIGINT NOT NULL,
    distinct_normal_event_count BIGINT NOT NULL,
    conflicting_event_count BIGINT NOT NULL,
    ignored_product_event_count BIGINT NOT NULL,
    row_checksum CHAR(64) NOT NULL,
    CONSTRAINT FK_snapshot_frequency_source_diagnostics_manifest FOREIGN KEY (manifest_id)
        REFERENCES snapshot.manifest(manifest_id)
);

CREATE INDEX IX_snapshot_frequency_monthly_activity_product
ON snapshot.frequency_monthly_activity (manifest_id, product_code, month, stock_code);

GRANT SELECT ON OBJECT::snapshot.frequency_monthly_activity TO [ssai_snapshot_reader];
GRANT SELECT ON OBJECT::snapshot.frequency_scope_stock TO [ssai_snapshot_reader];
GRANT SELECT ON OBJECT::snapshot.frequency_source_contract TO [ssai_snapshot_reader];
GRANT SELECT ON OBJECT::snapshot.frequency_source_diagnostics TO [ssai_snapshot_reader];
GRANT SELECT, INSERT ON OBJECT::snapshot.frequency_monthly_activity TO [ssai_snapshot_writer];
GRANT SELECT, INSERT ON OBJECT::snapshot.frequency_scope_stock TO [ssai_snapshot_writer];
GRANT SELECT, INSERT ON OBJECT::snapshot.frequency_source_contract TO [ssai_snapshot_writer];
GRANT SELECT, INSERT ON OBJECT::snapshot.frequency_source_diagnostics TO [ssai_snapshot_writer];
"""


MIGRATIONS = (
    SnapshotMigration("001_snapshot_manifest_payload", MIGRATION_001_SQL),
    SnapshotMigration("002_snapshot_frequency_projection", MIGRATION_002_SQL),
    SnapshotMigration("003_snapshot_relational_frequency_authority", MIGRATION_003_SQL),
)


def configure_migration_session(cursor: Any) -> dict[str, int]:
    """Set and prove the session options required by filtered-index DDL."""
    cursor.execute(MIGRATION_SESSION_SET_SQL)
    row = cursor.execute(MIGRATION_SESSION_PROPERTIES_SQL).fetchone()
    if not row or len(row) != len(MIGRATION_SESSION_OPTIONS):
        raise RuntimeError("migration session options could not be verified")
    actual = {
        name: int(value)
        for (name, _expected), value in zip(MIGRATION_SESSION_OPTIONS, row)
    }
    expected = dict(MIGRATION_SESSION_OPTIONS)
    if actual != expected:
        raise RuntimeError(
            "migration session options mismatch: "
            + ", ".join(f"{name}={actual.get(name)!r}" for name in expected)
        )
    return actual


def apply_snapshot_migrations(
    conn: Any,
    *,
    applied_by: str,
    migrations: tuple[SnapshotMigration, ...] = MIGRATIONS,
) -> dict[str, Any]:
    actor = str(applied_by or "").strip()
    if not actor:
        raise ValueError("applied_by is required")
    if hasattr(conn, "autocommit"):
        conn.autocommit = False
    applied: list[str] = []
    skipped: list[str] = []
    cursor = None
    current_migration_id = "__session_options__"
    try:
        cursor = conn.cursor()
        session_options = configure_migration_session(cursor)
        current_migration_id = "__ensure_ledger__"
        cursor.execute(ENSURE_LEDGER_SQL)
        for migration in migrations:
            current_migration_id = migration.migration_id
            row = cursor.execute(
                "SELECT migration_checksum FROM snapshot.schema_migrations WITH (UPDLOCK, HOLDLOCK) WHERE migration_id = ?",
                migration.migration_id,
            ).fetchone()
            if row:
                if str(row[0]) != migration.checksum:
                    raise RuntimeError(f"migration checksum mismatch: {migration.migration_id}")
                skipped.append(migration.migration_id)
                continue
            cursor.execute(migration.sql)
            cursor.execute(
                "INSERT INTO snapshot.schema_migrations (migration_id, migration_checksum, applied_by) VALUES (?, ?, ?)",
                migration.migration_id,
                migration.checksum,
                actor,
            )
            applied.append(migration.migration_id)
        current_migration_id = "__commit__"
        conn.commit()
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        if isinstance(exc, SnapshotMigrationError):
            raise
        raise SnapshotMigrationError(current_migration_id, exc) from exc
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
    return {"applied": applied, "skipped": skipped, "session_options": session_options}


def inspect_snapshot_schema(conn: Any) -> dict[str, Any]:
    cursor = conn.cursor()
    database_row = cursor.execute(
        "SELECT DB_NAME(), "
        "CAST(SERVERPROPERTY('MachineName') AS nvarchar(128)), "
        "CAST(SERVERPROPERTY('ServerName') AS nvarchar(128))"
    ).fetchone()
    actual_database = str(database_row[0] or "") if database_row else ""
    if actual_database != "SSAI_ANALYTICS":
        raise RuntimeError("analytics connection target mismatch")
    tables = [
        str(row[0])
        for row in cursor.execute(
            """
            SELECT t.name
            FROM sys.tables t
            INNER JOIN sys.schemas s ON s.schema_id=t.schema_id
            WHERE s.name=N'snapshot'
            ORDER BY t.name
            """
        ).fetchall()
    ]
    indexes = [
        str(row[0])
        for row in cursor.execute(
            """
            SELECT i.name
            FROM sys.indexes i
            INNER JOIN sys.tables t ON t.object_id=i.object_id
            INNER JOIN sys.schemas s ON s.schema_id=t.schema_id
            WHERE s.name=N'snapshot' AND i.name IS NOT NULL
              AND i.is_primary_key=0 AND i.is_unique_constraint=0
            ORDER BY i.name
            """
        ).fetchall()
    ]
    roles = [
        str(row[0])
        for row in cursor.execute(
            """
            SELECT name FROM sys.database_principals
            WHERE type='R' AND name IN (N'ssai_snapshot_reader', N'ssai_snapshot_writer')
            ORDER BY name
            """
        ).fetchall()
    ]
    permission_row = cursor.execute(
        "SELECT "
        "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE SCHEMA'), "
        "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE TABLE'), "
        "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'ALTER ANY ROLE'), "
        "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CONTROL')"
    ).fetchone()
    result = {
        "database": actual_database,
        "server": {
            "machine_name": str(database_row[1] or "") if database_row else "",
            "server_name": str(database_row[2] or "") if database_row else "",
        },
        "schema": "snapshot",
        "tables": tables,
        "indexes": indexes,
        "roles": roles,
        "migration_permissions": {
            "create_schema": bool(permission_row[0]) if permission_row else False,
            "create_table": bool(permission_row[1]) if permission_row else False,
            "alter_any_role": bool(permission_row[2]) if permission_row else False,
            "control_database": bool(permission_row[3]) if permission_row else False,
        },
    }
    if "schema_migrations" not in tables:
        return {
            **result,
            "migration_count": 0,
            "snapshot_schema_ready": False,
            "smoke": None,
        }
    migration_row = cursor.execute(
        "SELECT COUNT_BIG(*) FROM snapshot.schema_migrations"
    ).fetchone()
    smoke_row = cursor.execute(
        """
        SELECT COUNT_BIG(*),
               SUM(CASE WHEN status='draft' THEN 1 ELSE 0 END),
               SUM(CASE WHEN status='published' THEN 1 ELSE 0 END),
               SUM(CASE WHEN status='superseded' THEN 1 ELSE 0 END),
               SUM(CASE WHEN status='invalidated' THEN 1 ELSE 0 END),
               SUM(CASE WHEN approval_status='approved' THEN 1 ELSE 0 END),
               MIN(generation_no), MAX(generation_no)
        FROM snapshot.manifest
        WHERE company_id=N'__SSAI_SNAPSHOT_SMOKE__'
        """
    ).fetchone()
    payload_row = cursor.execute(
        """
        SELECT COUNT_BIG(*)
        FROM snapshot.payload p
        INNER JOIN snapshot.manifest m ON m.manifest_id=p.manifest_id
        WHERE m.company_id=N'__SSAI_SNAPSHOT_SMOKE__'
        """
    ).fetchone()
    return {
        **result,
        "migration_count": int(migration_row[0] or 0) if migration_row else 0,
        "snapshot_schema_ready": True,
        "smoke": {
            "manifest_count": int(smoke_row[0] or 0) if smoke_row else 0,
            "draft_count": int(smoke_row[1] or 0) if smoke_row else 0,
            "published_count": int(smoke_row[2] or 0) if smoke_row else 0,
            "superseded_count": int(smoke_row[3] or 0) if smoke_row else 0,
            "invalidated_count": int(smoke_row[4] or 0) if smoke_row else 0,
            "approved_count": int(smoke_row[5] or 0) if smoke_row else 0,
            "min_generation": int(smoke_row[6] or 0) if smoke_row else 0,
            "max_generation": int(smoke_row[7] or 0) if smoke_row else 0,
            "payload_count": int(payload_row[0] or 0) if payload_row else 0,
        },
    }
