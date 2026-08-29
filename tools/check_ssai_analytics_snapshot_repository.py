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
    FREQUENCY_PROJECTION_GRADES,
    build_relational_frequency_snapshot_from_aggregates,
    build_frequency_projection,
    build_frequency_snapshot_payload,
    snapshot_key_from_payload,
    validate_frequency_snapshot_payload,
)
from app.services.ssai_analytics_snapshot_migration import (  # noqa: E402
    MIGRATION_001_SQL,
    MIGRATION_002_SQL,
    MIGRATIONS,
    SnapshotMigration,
    SnapshotMigrationError,
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
    SnapshotReadResult,
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
        self.frequency_products: dict[int, list[dict[str, Any]]] = {}
        self.frequency_projections: dict[int, list[dict[str, Any]]] = {}
        self.sql_calls: list[str] = []
        self.next_id = 1


class _Cursor:
    def __init__(self, state: _State) -> None:
        self.state = state
        self.row: tuple[Any, ...] | None = None
        self.rows: list[tuple[Any, ...]] = []

    @staticmethod
    def _matches(item: dict[str, Any], values: tuple[Any, ...], *, include_version: bool = True) -> bool:
        names = ["company_id", "snapshot_type", "evaluation_month", "scope_fingerprint"]
        if include_version:
            names += ["schema_version", "algorithm_version"]
        return all(str(item[name]) == str(value) for name, value in zip(names, values))

    def execute(self, sql: str, *params: Any) -> "_Cursor":
        self.state.sql_calls.append(sql)
        self.row = None
        self.rows = []
        if "snapshot.operating.resolve_latest" in sql:
            candidates = [
                item for item in self.state.manifests
                if str(item["company_id"]) == str(params[0])
                and str(item["snapshot_type"]) == str(params[1])
                and str(item["scope_fingerprint"]) == str(params[2])
                and str(item["schema_version"]) == str(params[3])
                and str(item["algorithm_version"]) == str(params[4])
                and item["status"] == "published"
                and item["approval_status"] == "approved"
                and str(item["basis_to"]) <= str(params[5])
                and str(item.get("source_watermark_status") or "") in {"verified", "unverified"}
            ]
            if candidates:
                item = max(candidates, key=lambda value: (value["basis_to"], value["evaluation_month"], value["generation_no"]))
                self.row = (item["evaluation_month"],)
        elif "storage_representation" in sql and "FROM snapshot.manifest" in sql and "snapshot.read.published" not in sql and "snapshot.approve.load" not in sql and "snapshot.projection.manifest" not in sql:
            rows = [
                m for m in self.state.manifests
                if self._matches(m, params) and m["status"] == "published" and m["approval_status"] == "approved"
            ]
            if rows:
                item = max(rows, key=lambda value: value["generation_no"])
                self.row = (
                    item["manifest_id"], item["generation_no"], item["checksum"], item["approval_status"],
                    item["approved_at"], item["approved_by"], item["approval_reason"], "legacy_json_v1",
                )
        elif "snapshot.publish.latest" in sql:
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
        elif "snapshot.publish.frequency_product" in sql:
            self.state.frequency_products.setdefault(int(params[0]), []).append(
                {"product_code": params[1], "occurrence_count_3m": params[2], "frequency_grade": params[3], "data_status": params[4], "row_checksum": params[5]}
            )
        elif "snapshot.publish.frequency_projection" in sql:
            self.state.frequency_projections.setdefault(int(params[0]), []).append(
                {"frequency_grade": params[1], "expected_product_count": params[2], "projection_checksum": params[3]}
            )
        elif "snapshot.inspect.generation" in sql:
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
                    "legacy_json_v1", payload["payload_json"], payload["storage_checksum"], payload["payload_size"],
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
                    payload["payload_json"],
                    payload["storage_checksum"], payload["payload_size"],
                )
        elif "snapshot.projection.manifest" in sql:
            rows = [m for m in self.state.manifests if self._matches(m, params) and m["status"] == "published" and m["approval_status"] == "approved"]
            if rows:
                item = max(rows, key=lambda value: value["generation_no"])
                self.row = (item["manifest_id"], item["generation_no"], item["checksum"], item["item_count"], "legacy_json_v1", 1)
        elif "snapshot.projection.headers" in sql:
            self.rows = [
                (row["frequency_grade"], row["expected_product_count"], row["projection_checksum"])
                for row in sorted(self.state.frequency_projections.get(int(params[0]), []), key=lambda row: row["frequency_grade"])
            ]
        elif "snapshot.projection.product_presence" in sql:
            if self.state.frequency_products.get(int(params[0])):
                self.row = (int(params[0]),)
        elif "snapshot.projection.product_count" in sql:
            self.row = (len(self.state.frequency_products.get(int(params[0]), [])),)
        elif "snapshot.projection.rows_by_grade" in sql:
            self.rows = [
                (row["product_code"], row["occurrence_count_3m"], row["frequency_grade"], row["data_status"], row["row_checksum"])
                for row in sorted(self.state.frequency_products.get(int(params[0]), []), key=lambda row: row["product_code"])
                if row["frequency_grade"] == params[1]
            ]
        elif "snapshot.projection.rows_all" in sql:
            self.rows = [
                (row["product_code"], row["occurrence_count_3m"], row["frequency_grade"], row["data_status"], row["row_checksum"])
                for row in sorted(self.state.frequency_products.get(int(params[0]), []), key=lambda row: (row["frequency_grade"], row["product_code"]))
            ]
        elif "snapshot.projection.rows_subset" in sql:
            requested = set(params[1:])
            self.rows = [
                (row["product_code"], row["occurrence_count_3m"], row["frequency_grade"], row["data_status"], row["row_checksum"])
                for row in sorted(self.state.frequency_products.get(int(params[0]), []), key=lambda row: row["product_code"])
                if row["product_code"] in requested
            ]
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

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def executemany(self, sql: str, params_seq: list[tuple[Any, ...]]) -> "_Cursor":
        for params in params_seq:
            self.execute(sql, *params)
        return self


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


class _RelationalWriteCursor:
    def __init__(self) -> None:
        self.row: tuple[Any, ...] | None = None
        self.executemany_sql: list[str] = []

    def execute(self, sql: str, *_params: Any) -> "_RelationalWriteCursor":
        self.row = (1,) if "snapshot.publish.relational.manifest" in sql else None
        return self

    def executemany(self, sql: str, params_seq: list[tuple[Any, ...]]) -> "_RelationalWriteCursor":
        if not params_seq:
            raise AssertionError("empty executemany must not be called")
        self.executemany_sql.append(sql)
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row


class _RelationalWriteConnection:
    def __init__(self) -> None:
        self.cursor_instance = _RelationalWriteCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _RelationalWriteCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        return None


def test_relational_empty_monthly_activity_is_a_valid_noop_write() -> None:
    diagnostics = {
        "diagnostic_contract_version": 2,
        "source_row_count": 0,
        "normal_positive_accepted_row_count": 0,
        "normal_positive_duplicate_row_count": 0,
        "normal_positive_conflicting_row_count": 0,
        "normal_positive_missing_key_row_count": 0,
        "normal_positive_nonintegral_row_count": 0,
        "normal_nonpositive_row_count": 0,
        "return_positive_row_count": 0,
        "return_nonpositive_row_count": 0,
        "other_tcode_row_count": 0,
        "normal_positive_row_count": 0,
        "distinct_normal_event_count": 0,
        "conflicting_event_count": 0,
        "ignored_product_event_count": 0,
    }
    snapshot = build_relational_frequency_snapshot_from_aggregates(
        company_id=2,
        evaluation_month="202609",
        stock_codes=["00001"],
        product_codes=["P1", "P2"],
        monthly_rows=[],
        source_diagnostics=diagnostics,
    )
    connection = _RelationalWriteConnection()
    repository = SqlServerSnapshotRepository(
        reader_connection_factory=lambda: connection,
        writer_connection_factory=lambda: connection,
    )
    result = repository.publish_relational(snapshot, created_by="fixture")
    _assert(result.status == "draft" and result.generation_no == 1, "empty monthly activity still saves a draft")
    _assert(connection.commits == 1 and connection.rollbacks == 0, "empty monthly activity commits normally")
    _assert(
        not any("snapshot.frequency_monthly_activity" in sql for sql in connection.cursor_instance.executemany_sql),
        "empty monthly activity must skip executemany",
    )
    _assert(
        any("snapshot.frequency_product" in sql for sql in connection.cursor_instance.executemany_sql)
        and any("snapshot.frequency_projection" in sql for sql in connection.cursor_instance.executemany_sql),
        "product and projection relational rows remain stored",
    )


def test_latest_eligible_operating_key_excludes_future_basis_and_mismatches() -> None:
    state = _State()
    key = SnapshotKey("1", "dashboard_inventory_outbound_frequency", "202608", "a" * 64, "1.0", "outbound_frequency_v1")
    common = {
        "company_id": key.company_id, "snapshot_type": key.snapshot_type,
        "scope_fingerprint": key.scope_fingerprint, "schema_version": key.schema_version,
        "algorithm_version": key.algorithm_version, "status": "published",
        "approval_status": "approved", "source_watermark_status": "unverified",
    }
    state.manifests.extend([
        {**common, "manifest_id": 1, "evaluation_month": "202608", "basis_from": "20250501", "basis_to": "20250731", "generation_no": 1},
        {**common, "manifest_id": 2, "evaluation_month": "202609", "basis_from": "20250601", "basis_to": "20260831", "generation_no": 1},
        {**common, "manifest_id": 3, "evaluation_month": "202610", "basis_from": "20250701", "basis_to": "20260930", "generation_no": 1, "scope_fingerprint": "b" * 64},
        {**common, "manifest_id": 4, "evaluation_month": "202609", "basis_from": "20250601", "basis_to": "20260831", "generation_no": 2, "approval_status": "pending"},
    ])
    repository = SqlServerSnapshotRepository(reader_connection_factory=lambda: _Connection(state), writer_connection_factory=lambda: _Connection(state))
    august = repository.resolve_latest_eligible_key(key, available_through="20260829")
    september = repository.resolve_latest_eligible_key(key, available_through="20260901")
    _assert(august is not None and august.evaluation_month == "202608", "future basis must not be selected before it completes")
    _assert(september is not None and september.evaluation_month == "202609", "completed future evaluation becomes the newest operating read")


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
    _assert(
        repository.read_frequency_projection(key).status == SNAPSHOT_STATUS_UNAPPROVED,
        "draft projection read must fail closed",
    )
    inspected = repository.inspect_generation(key, 1)
    _assert(
        inspected.status == SNAPSHOT_STATUS_UNAPPROVED and inspected.payload == first_payload,
        "exact draft inspection must validate without publishing",
    )
    try:
        repository.approve_checked(
            key, 1, expected_checksum="0" * 64, approved_by="fixture-approver", approval_reason="wrong checksum"
        )
    except ValueError:
        pass
    else:
        raise AssertionError("approval must bind the expected checksum")
    _assert(repository.read(key).status == SNAPSHOT_STATUS_UNAPPROVED, "checksum mismatch must not publish draft")

    approved = repository.approve_checked(
        key, 1, expected_checksum=str(first_payload["checksum"]),
        approved_by="fixture-approver", approval_reason="fixture source range reviewed"
    )
    ready = repository.read(key)
    _assert(approved.status == SNAPSHOT_STATUS_READY and ready.usable, "approved generation is readable")
    _assert(
        ready.payload == first_payload,
        "plain NVARCHAR published transport must restore the exact canonical payload before validation",
    )
    published_read_sql = [sql for sql in state.sql_calls if "snapshot.read.published" in sql]
    _assert(published_read_sql, "published legacy read SQL must execute")
    _assert(
        all("COMPRESS(" not in sql.upper() for sql in published_read_sql),
        "published legacy read must remain compatible with SQL Server 2008 without COMPRESS",
    )
    inspected_ready = repository.inspect_generation(key, 1)
    _assert(
        inspected_ready.status == SNAPSHOT_STATUS_READY
        and inspected_ready.manifest_status == "published"
        and inspected_ready.approval_status == "approved"
        and inspected_ready.generation_no == 1,
        "post-approval exact inspection must report actual published state",
    )
    _assert(
        ready.approved_by == "fixture-approver" and ready.approval_reason,
        "approval provenance must be retained",
    )
    _assert(
        ready.payload and ready.payload["source_watermark_status"] == "unverified",
        "manual approval does not rewrite watermark provenance",
    )
    projection_a = repository.read_frequency_projection(key, frequency_grade="A")
    _assert(projection_a.usable and [row["product_code"] for row in projection_a.rows] == ["P1"], "approved A projection must be exact")
    projection_subset = repository.read_frequency_projection(key, product_codes=("P2",))
    _assert(projection_subset.usable and projection_subset.rows[0]["frequency_grade"] == "X", "subset projection must preserve X rows")
    active_manifest_id = int(ready.manifest_id or 0)
    saved_projection_rows = copy.deepcopy(state.frequency_products[active_manifest_id])
    saved_projection_headers = copy.deepcopy(state.frequency_projections[active_manifest_id])
    state.frequency_products.pop(active_manifest_id)
    state.frequency_projections.pop(active_manifest_id)
    _assert(repository.read_frequency_projection(key).status == "legacy", "projection absence must retain legacy compatibility")
    state.frequency_products[active_manifest_id] = saved_projection_rows
    state.frequency_projections[active_manifest_id] = saved_projection_headers
    original_projection_row = dict(state.frequency_products[active_manifest_id][0])
    state.frequency_products[active_manifest_id][0]["frequency_grade"] = "B"
    _assert(repository.read_frequency_projection(key, frequency_grade="A").status == SNAPSHOT_STATUS_CORRUPT, "substituted projection row must fail closed")
    state.frequency_products[active_manifest_id][0] = original_projection_row
    state.frequency_products[active_manifest_id] = [
        row for row in state.frequency_products[active_manifest_id] if row["product_code"] != "P1"
    ]
    _assert(repository.read_frequency_projection(key, frequency_grade="A").status == SNAPSHOT_STATUS_CORRUPT, "missing projection row must fail closed")
    state.frequency_products[active_manifest_id].append(original_projection_row)

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

    invalid_repository = SqlServerSnapshotRepository(
        reader_connection_factory=factory,
        writer_connection_factory=factory,
        payload_validator=lambda _payload, _key: SnapshotReadResult(
            status=SNAPSHOT_STATUS_CORRUPT,
            reason="fixture validator rejection",
        ),
    )
    _assert(
        invalid_repository.read(key).status == SNAPSHOT_STATUS_CORRUPT,
        "validator result status must fail closed instead of returning ready",
    )

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
        if "SET ANSI_NULLS ON" in sql:
            if not self.conn.ignore_session_set:
                self.conn.session_values = (1, 1, 1, 1, 1, 1, 0)
        elif "SESSIONPROPERTY('ANSI_NULLS')" in sql:
            self.row = self.conn.session_values
        elif "SELECT migration_checksum" in sql:
            checksum = self.conn.ledger.get(str(params[0]))
            self.row = (checksum,) if checksum else None
        elif "INSERT INTO snapshot.schema_migrations" in sql:
            self.conn.ledger[str(params[0])] = str(params[1])
        elif sql == self.conn.fail_sql:
            raise RuntimeError("fixture migration failure")
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row

    def close(self) -> None:
        return None


class _MigrationConnection:
    def __init__(self, *, fail_sql: str = "", ignore_session_set: bool = False) -> None:
        self.ledger: dict[str, str] = {}
        self.fail_sql = fail_sql
        self.ignore_session_set = ignore_session_set
        self.session_values = (1, 1, 1, 0, 1, 1, 0)
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
    _assert(first["applied"] == [migration.migration_id for migration in MIGRATIONS], "first migrations apply")
    _assert(second["skipped"] == [migration.migration_id for migration in MIGRATIONS], "second migrations are no-op")
    _assert(first["session_options"]["ARITHABORT"] == 1, "migration session enables ARITHABORT")

    bad_session_conn = _MigrationConnection(ignore_session_set=True)
    try:
        apply_snapshot_migrations(bad_session_conn, applied_by="fixture")
    except SnapshotMigrationError as exc:
        _assert(exc.migration_id == "__session_options__", "invalid session fails before DDL")
    else:
        raise AssertionError("invalid migration session must fail closed")
    _assert(bad_session_conn.rollbacks == 1 and not bad_session_conn.ledger, "invalid session performs no DDL")

    failing = SnapshotMigration("fixture_failure", "RAISE_FIXTURE")
    failed_conn = _MigrationConnection(fail_sql=failing.sql)
    try:
        apply_snapshot_migrations(failed_conn, applied_by="fixture", migrations=(failing,))
    except SnapshotMigrationError as exc:
        _assert(exc.migration_id == "fixture_failure", "failure reports exact migration id")
    else:
        raise AssertionError("migration failure must propagate")
    _assert(failed_conn.rollbacks == 1 and failed_conn.commits == 0, "migration failure rolls back")
    _assert("ssai_snapshot_reader" in MIGRATION_001_SQL, "reader DB role must be created")
    _assert("ssai_snapshot_writer" in MIGRATION_001_SQL, "writer DB role must be created")
    _assert("GRANT INSERT ON OBJECT::snapshot.payload" in MIGRATION_001_SQL, "payload is insert-only")
    _assert("GRANT UPDATE ON OBJECT::snapshot.payload" not in MIGRATION_001_SQL, "payload update is forbidden")
    _assert("SSAI_COMPANIES" not in MIGRATION_001_SQL and "Rddbc" not in MIGRATION_001_SQL, "no cross-DB dependency")
    _assert("CREATE DATABASE" not in MIGRATION_001_SQL.upper(), "migration must not create a database")
    _assert("snapshot.frequency_product" in MIGRATION_002_SQL and "snapshot.frequency_projection" in MIGRATION_002_SQL, "projection tables must be migrated")
    _assert("GRANT INSERT ON OBJECT::snapshot.frequency_product" in MIGRATION_002_SQL, "projection writer insert grant required")


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
        test_relational_empty_monthly_activity_is_a_valid_noop_write,
        test_latest_eligible_operating_key_excludes_future_basis_and_mismatches,
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
