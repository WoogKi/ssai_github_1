from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot import (  # noqa: E402
    build_frequency_snapshot_payload,
    snapshot_key_from_payload,
    validate_frequency_snapshot_payload,
)
from app.services.ssai_analytics_snapshot_migration import (  # noqa: E402
    MIGRATION_001_SQL,
    MIGRATIONS,
    SnapshotMigration,
    apply_snapshot_migrations,
)
from app.services import ssai_analytics_db as analytics_db  # noqa: E402
from app.services.ssai_snapshot_repository import (  # noqa: E402
    SNAPSHOT_STATUS_CORRUPT,
    SNAPSHOT_STATUS_MISSING,
    SNAPSHOT_STATUS_READY,
    SNAPSHOT_STATUS_STALE,
    SNAPSHOT_STATUS_UNAPPROVED,
    SNAPSHOT_STATUS_VERSION_MISMATCH,
    SnapshotKey,
)
from app.services.sql_server_snapshot_repository import SqlServerSnapshotRepository  # noqa: E402


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _payload(company: str = "C1", month: str = "202601", stock: str = "00001") -> dict[str, Any]:
    return build_frequency_snapshot_payload(
        company_id=company,
        evaluation_month=month,
        product_codes=["P1", "P2"],
        stock_codes=[stock],
        source_watermark_status="unverified",
        rows=[
            {
                "outbound_date": "20251010",
                "vendor_code": "V1",
                "outbound_seq": 1,
                "io_gu_gcode": "0012",
                "io_tcode": "501",
                "product_code": "P1",
                "stock_code": stock,
                "quantity": 2,
                "oquantity": 0,
            }
        ],
    )


def _validator(payload: Any, key: Any) -> Any:
    return validate_frequency_snapshot_payload(payload, expected_key=key)


class _State:
    def __init__(self) -> None:
        self.manifests: list[dict[str, Any]] = []
        self.payloads: dict[int, dict[str, Any]] = {}
        self.next_id = 1


class _Cursor:
    def __init__(self, state: _State) -> None:
        self.state = state
        self.row: tuple[Any, ...] | None = None

    @staticmethod
    def _matches(item: dict[str, Any], values: tuple[Any, ...], *, include_version: bool = True) -> bool:
        names = ["company_id", "snapshot_type", "evaluation_month", "scope_fingerprint"]
        if include_version:
            names += ["schema_version", "algorithm_version"]
        return all(str(item[name]) == str(value) for name, value in zip(names, values))

    def execute(self, sql: str, *params: Any) -> "_Cursor":
        self.row = None
        if "snapshot.publish.latest" in sql:
            rows = [m for m in self.state.manifests if self._matches(m, params)]
            if rows:
                item = max(rows, key=lambda value: value["generation_no"])
                self.row = (
                    item["manifest_id"], item["generation_no"], item["checksum"],
                    item["status"], item["approval_status"],
                )
        elif "snapshot.publish.manifest" in sql:
            manifest_id = self.state.next_id
            self.state.next_id += 1
            self.state.manifests.append(
                {
                    "manifest_id": manifest_id,
                    "company_id": params[0], "snapshot_type": params[1],
                    "evaluation_month": params[2], "basis_from": params[3], "basis_to": params[4],
                    "scope_fingerprint": params[5], "schema_version": params[6],
                    "algorithm_version": params[7], "generation_no": int(params[8]),
                    "status": "draft", "approval_status": "pending",
                    "source_watermark": params[9], "source_watermark_status": params[10],
                    "source_fingerprint": params[11], "item_count": int(params[12]),
                    "payload_size": int(params[13]), "checksum": params[14], "created_by": params[15],
                    "approved_at": "", "approved_by": "", "approval_reason": "",
                }
            )
            self.row = (manifest_id,)
        elif "snapshot.publish.payload" in sql:
            self.state.payloads[int(params[0])] = {
                "payload_json": params[1], "storage_checksum": params[2], "payload_size": int(params[3])
            }
        elif "snapshot.approve.load" in sql:
            candidates = [
                m for m in self.state.manifests
                if self._matches(m, params[:6]) and m["generation_no"] == int(params[6])
            ]
            if candidates:
                item = candidates[0]
                payload = self.state.payloads[item["manifest_id"]]
                self.row = (
                    item["manifest_id"], item["status"], item["approval_status"], item["checksum"],
                    payload["payload_json"], payload["storage_checksum"], payload["payload_size"],
                )
        elif "snapshot.approve.supersede" in sql:
            for item in self.state.manifests:
                if self._matches(item, params[:6]) and item["status"] == "published" and item["manifest_id"] != int(params[6]):
                    item["status"] = "superseded"
        elif "snapshot.approve.publish" in sql:
            for item in self.state.manifests:
                if item["manifest_id"] == int(params[2]) and item["status"] == "draft":
                    item.update(
                        status="published", approval_status="approved", approved_at="fixture-time",
                        approved_by=params[0], approval_reason=params[1]
                    )
        elif "snapshot.read.published" in sql:
            rows = [
                m for m in self.state.manifests
                if self._matches(m, params) and m["status"] == "published" and m["approval_status"] == "approved"
            ]
            if rows:
                item = max(rows, key=lambda value: value["generation_no"])
                payload = self.state.payloads[item["manifest_id"]]
                self.row = (
                    item["manifest_id"], item["generation_no"], item["checksum"], item["approval_status"],
                    item["approved_at"], item["approved_by"], item["approval_reason"],
                    payload["payload_json"], payload["storage_checksum"], payload["payload_size"],
                )
        elif "snapshot.read.exact" in sql:
            rows = [m for m in self.state.manifests if self._matches(m, params)]
            if rows:
                item = max(rows, key=lambda value: value["generation_no"])
                self.row = (item["status"], item["approval_status"])
        elif "snapshot.read.other_version" in sql:
            rows = [
                m for m in self.state.manifests
                if self._matches(m, params, include_version=False)
                and m["status"] == "published" and m["approval_status"] == "approved"
            ]
            if rows:
                self.row = (rows[0]["manifest_id"],)
        elif "snapshot.invalidate" in sql:
            for item in self.state.manifests:
                if self._matches(item, params[2:]) and item["status"] == "published":
                    item.update(status="invalidated", invalidated_by=params[0], failure_reason=params[1])
        else:
            raise AssertionError(f"unhandled repository SQL: {sql[:80]!r}")
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row


class _Connection:
    def __init__(self, state: _State) -> None:
        self.state = state
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self.state)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        return None


def test_repository_lifecycle_and_isolation() -> None:
    state = _State()
    factory = lambda: _Connection(state)
    repository = SqlServerSnapshotRepository(
        reader_connection_factory=factory,
        writer_connection_factory=factory,
        payload_validator=_validator,
    )
    first_payload = _payload()
    key = snapshot_key_from_payload(first_payload)
    draft = repository.publish(key, first_payload, created_by="fixture-generator")
    _assert(draft.status == "draft" and draft.generation_no == 1, "publish creates draft generation")
    _assert(repository.read(key).status == SNAPSHOT_STATUS_UNAPPROVED, "draft read must fail closed")

    approved = repository.approve(
        key, 1, approved_by="fixture-approver", approval_reason="fixture source range reviewed"
    )
    ready = repository.read(key)
    _assert(approved.status == SNAPSHOT_STATUS_READY and ready.usable, "approved generation is readable")
    _assert(
        ready.approved_by == "fixture-approver" and ready.approval_reason,
        "approval provenance must be retained",
    )
    _assert(
        ready.payload and ready.payload["source_watermark_status"] == "unverified",
        "manual approval does not rewrite watermark provenance",
    )

    second_payload = copy.deepcopy(first_payload)
    second_payload["source_watermark"] = "manual-range-2"
    from app.services.dashboard_inventory_frequency_snapshot import calculate_payload_checksum
    second_payload["checksum"] = calculate_payload_checksum(second_payload)
    second = repository.replace(key, second_payload, created_by="fixture-generator")
    _assert(second.generation_no == 2 and repository.read(key).generation_no == 1, "draft does not replace active")
    repository.approve(key, 2, approved_by="fixture-approver", approval_reason="replacement reviewed")
    _assert(repository.read(key).generation_no == 2, "approval atomically activates new generation")
    _assert(
        any(item["generation_no"] == 1 and item["status"] == "superseded" for item in state.manifests),
        "previous generation must be preserved",
    )

    other_payload = _payload(company="C2")
    other_key = snapshot_key_from_payload(other_payload)
    _assert(repository.read(other_key).status != SNAPSHOT_STATUS_READY, "company isolation")
    month_key = SnapshotKey(
        company_id=key.company_id,
        snapshot_type=key.snapshot_type,
        evaluation_month="202602",
        scope_fingerprint=key.scope_fingerprint,
        schema_version=key.schema_version,
        algorithm_version=key.algorithm_version,
    )
    scope_key = SnapshotKey(
        company_id=key.company_id,
        snapshot_type=key.snapshot_type,
        evaluation_month=key.evaluation_month,
        scope_fingerprint="f" * 64,
        schema_version=key.schema_version,
        algorithm_version=key.algorithm_version,
    )
    version_key = SnapshotKey(
        company_id=key.company_id,
        snapshot_type=key.snapshot_type,
        evaluation_month=key.evaluation_month,
        scope_fingerprint=key.scope_fingerprint,
        schema_version="2.0",
        algorithm_version=key.algorithm_version,
    )
    _assert(repository.read(month_key).status == SNAPSHOT_STATUS_MISSING, "month isolation")
    _assert(repository.read(scope_key).status == SNAPSHOT_STATUS_MISSING, "scope isolation")
    _assert(repository.read(version_key).status == SNAPSHOT_STATUS_VERSION_MISMATCH, "version isolation")

    active_manifest_id = int(repository.read(key).manifest_id or 0)
    original_storage_checksum = state.payloads[active_manifest_id]["storage_checksum"]
    state.payloads[active_manifest_id]["storage_checksum"] = "0" * 64
    _assert(repository.read(key).status == SNAPSHOT_STATUS_CORRUPT, "storage corruption fails closed")
    state.payloads[active_manifest_id]["storage_checksum"] = original_storage_checksum
    repository.invalidate(key, reason="fixture invalidation", invalidated_by="fixture-operator")
    _assert(repository.read(key).status == SNAPSHOT_STATUS_STALE, "invalidated snapshot must fail closed")
    republished = repository.publish(key, second_payload, created_by="fixture-generator")
    _assert(
        republished.generation_no == 3 and not republished.no_op,
        "invalidated/superseded payload must create a new generation",
    )


class _MigrationCursor:
    def __init__(self, conn: "_MigrationConnection") -> None:
        self.conn = conn
        self.row: tuple[Any, ...] | None = None

    def execute(self, sql: str, *params: Any) -> "_MigrationCursor":
        self.row = None
        if "SELECT migration_checksum" in sql:
            checksum = self.conn.ledger.get(str(params[0]))
            self.row = (checksum,) if checksum else None
        elif "INSERT INTO snapshot.schema_migrations" in sql:
            self.conn.ledger[str(params[0])] = str(params[1])
        elif sql == self.conn.fail_sql:
            raise RuntimeError("fixture migration failure")
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row


class _MigrationConnection:
    def __init__(self, *, fail_sql: str = "") -> None:
        self.ledger: dict[str, str] = {}
        self.fail_sql = fail_sql
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _MigrationCursor:
        return _MigrationCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_migration_idempotency_and_rollback() -> None:
    conn = _MigrationConnection()
    first = apply_snapshot_migrations(conn, applied_by="fixture")
    second = apply_snapshot_migrations(conn, applied_by="fixture")
    _assert(first["applied"] == [MIGRATIONS[0].migration_id], "first migration applies")
    _assert(second["skipped"] == [MIGRATIONS[0].migration_id], "second migration is no-op")

    failing = SnapshotMigration("fixture_failure", "RAISE_FIXTURE")
    failed_conn = _MigrationConnection(fail_sql=failing.sql)
    try:
        apply_snapshot_migrations(failed_conn, applied_by="fixture", migrations=(failing,))
    except RuntimeError:
        pass
    else:
        raise AssertionError("migration failure must propagate")
    _assert(failed_conn.rollbacks == 1 and failed_conn.commits == 0, "migration failure rolls back")
    _assert("ssai_snapshot_reader" in MIGRATION_001_SQL, "reader DB role must be created")
    _assert("ssai_snapshot_writer" in MIGRATION_001_SQL, "writer DB role must be created")
    _assert("GRANT INSERT ON OBJECT::snapshot.payload" in MIGRATION_001_SQL, "payload is insert-only")
    _assert("GRANT UPDATE ON OBJECT::snapshot.payload" not in MIGRATION_001_SQL, "payload update is forbidden")
    _assert("SSAI_COMPANIES" not in MIGRATION_001_SQL and "Rddbc" not in MIGRATION_001_SQL, "no cross-DB dependency")
    _assert("CREATE DATABASE" not in MIGRATION_001_SQL.upper(), "migration must not create a database")


def test_analytics_connector_fail_closed() -> None:
    member_only = {
        "SSAI_DB_SERVER": "member-server",
        "SSAI_DB_USER": "member-user",
        "SSAI_DB_PASSWORD": "member-password",
        "SSAI_DB_DRIVER": "ODBC Driver 18 for SQL Server",
        "SSAI_DB_NAME": "MEMBER_DB_MUST_NOT_BE_USED",
    }
    with patch.object(analytics_db, "load_dotenv", return_value=member_only), patch.dict(
        os.environ, {}, clear=True
    ):
        reader_from_shared = analytics_db.load_analytics_db_settings("reader")
        writer_from_shared = analytics_db.load_analytics_db_settings("writer")
        migration_from_shared = analytics_db.load_analytics_db_settings("migration")
    _assert(
        {reader_from_shared.database, writer_from_shared.database, migration_from_shared.database}
        == {"SSAI_ANALYTICS"},
        "shared credentials must never reuse the member DB name",
    )
    _assert(
        reader_from_shared.user == writer_from_shared.user == migration_from_shared.user == "member-user",
        "existing SSAI credentials may serve all roles initially",
    )
    with patch.object(analytics_db, "load_dotenv", return_value=member_only), patch.dict(
        os.environ, {}, clear=True
    ), patch.object(analytics_db.pyodbc, "connect", return_value=object()) as connect_mock:
        analytics_db.connect_analytics_db("reader")
    connection_string = str(connect_mock.call_args.args[0])
    _assert("DATABASE=SSAI_ANALYTICS;" in connection_string, "connector target is fixed")
    _assert("MEMBER_DB_MUST_NOT_BE_USED" not in connection_string, "member DB name is never used")

    analytics = {
        "SSAI_ANALYTICS_DB_SERVER": "analytics-server",
        "SSAI_ANALYTICS_DB_NAME": "SSAI_ANALYTICS",
        "SSAI_ANALYTICS_DB_READER_USER": "reader-user",
        "SSAI_ANALYTICS_DB_READER_PASSWORD": "reader-password",
        "SSAI_ANALYTICS_DB_WRITER_USER": "writer-user",
        "SSAI_ANALYTICS_DB_WRITER_PASSWORD": "writer-password",
        "SSAI_ANALYTICS_DB_MIGRATION_USER": "migration-user",
        "SSAI_ANALYTICS_DB_MIGRATION_PASSWORD": "migration-password",
    }
    with patch.object(analytics_db, "load_dotenv", return_value=analytics), patch.dict(
        os.environ, {}, clear=True
    ):
        reader = analytics_db.load_analytics_db_settings("reader")
        writer = analytics_db.load_analytics_db_settings("writer")
        migration = analytics_db.load_analytics_db_settings("migration")
    _assert(reader.user != writer.user and reader.role != writer.role, "reader/writer settings are separate")
    _assert(migration.user not in {reader.user, writer.user}, "migration credentials are separate")

    wrong_database = dict(member_only, SSAI_ANALYTICS_DB_NAME="NOT_SSAI_ANALYTICS")
    with patch.object(analytics_db, "load_dotenv", return_value=wrong_database), patch.dict(
        os.environ, {}, clear=True
    ):
        try:
            analytics_db.load_analytics_db_settings("reader")
        except analytics_db.AnalyticsDbConfigurationError:
            pass
        else:
            raise AssertionError("a non-SSAI_ANALYTICS target must fail closed")


def main() -> int:
    tests = (
        test_repository_lifecycle_and_isolation,
        test_migration_idempotency_and_rollback,
        test_analytics_connector_fail_closed,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS SSAI analytics snapshot repository ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
