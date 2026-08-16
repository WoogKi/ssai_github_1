from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

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


MIGRATIONS = (SnapshotMigration("001_snapshot_manifest_payload", MIGRATION_001_SQL),)


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
    try:
        cursor = conn.cursor()
        cursor.execute(ENSURE_LEDGER_SQL)
        for migration in migrations:
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
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"applied": applied, "skipped": skipped}


def inspect_snapshot_schema(conn: Any) -> dict[str, Any]:
    cursor = conn.cursor()
    database_row = cursor.execute("SELECT DB_NAME()").fetchone()
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
        "database": actual_database,
        "schema": "snapshot",
        "tables": tables,
        "indexes": indexes,
        "roles": roles,
        "migration_count": int(migration_row[0] or 0) if migration_row else 0,
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
