"""Offline focused gate for the shared-DB product classification authority."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.product_classification_contract import (  # noqa: E402
    ClassificationStatus,
    ProductClassification,
)
from app.services.ssai_product_classification_repository import (  # noqa: E402
    MAPPING_TABLE,
    ProductClassificationTargets,
    TARGET_PRODUCT_GROUP,
    TARGET_PRODUCT_OVERRIDE,
    create_draft_mapping,
    delete_draft_mapping,
    load_effective_classification_authority,
    shadow_classify_product,
    transition_mapping_status,
    update_draft_mapping,
)
from tools.ssai_add_product_classification_schema import (  # noqa: E402
    CREATE_LOOKUP_INDEX_SQL,
    CREATE_MAPPING_TABLE_SQL,
)


_ROW_FIELDS = (
    "mapping_id",
    "company_id",
    "target_type",
    "target_gcode",
    "target_code",
    "classification",
    "adjustment_only",
    "reason_code",
    "status",
    "effective_from",
    "effective_to",
    "mapping_version",
    "created_by",
    "created_at",
    "updated_by",
    "updated_at",
    "approved_by",
    "approved_at",
)


class _Cursor:
    def __init__(self, db: "_Db") -> None:
        self.db = db
        self.result: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def execute(self, sql: str, *params: Any) -> "_Cursor":
        normalized = " ".join(sql.split())
        self.result = []
        self.rowcount = 0
        if normalized.startswith("SELECT mapping_id"):
            company_id, as_of_from, as_of_to = params
            selected = [
                row
                for row in self.db.rows
                if row["company_id"] == company_id
                and row["status"] == "approved"
                and row["effective_from"] <= as_of_from
                and (row["effective_to"] is None or row["effective_to"] >= as_of_to)
            ]
            self.result = [tuple(row[field] for field in _ROW_FIELDS) for row in sorted(selected, key=lambda item: item["mapping_id"])]
        elif normalized.startswith(f"INSERT INTO dbo.{MAPPING_TABLE}"):
            mapping_id = self.db.next_id
            self.db.next_id += 1
            (
                company_id,
                target_type,
                target_gcode,
                target_code,
                classification,
                adjustment_only,
                reason_code,
                effective_from,
                effective_to,
                mapping_version,
                created_by,
                updated_by,
            ) = params
            self.db.rows.append(
                {
                    "mapping_id": mapping_id,
                    "company_id": company_id,
                    "target_type": target_type,
                    "target_gcode": target_gcode,
                    "target_code": target_code,
                    "classification": classification,
                    "adjustment_only": adjustment_only,
                    "reason_code": reason_code,
                    "status": "draft",
                    "effective_from": effective_from,
                    "effective_to": effective_to,
                    "mapping_version": mapping_version,
                    "created_by": created_by,
                    "created_at": "fixture-created",
                    "updated_by": updated_by,
                    "updated_at": "fixture-updated",
                    "approved_by": "",
                    "approved_at": "",
                }
            )
            self.result = [(mapping_id,)]
            self.rowcount = 1
        elif "SET classification = ?" in normalized:
            classification, adjustment_only, reason_code, effective_from, effective_to, version, actor, mapping_id = params
            row = self.db.find(mapping_id, status="draft")
            if row:
                row.update(
                    classification=classification,
                    adjustment_only=adjustment_only,
                    reason_code=reason_code,
                    effective_from=effective_from,
                    effective_to=effective_to,
                    mapping_version=version,
                    updated_by=actor,
                )
                self.rowcount = 1
        elif "SET status = ?" in normalized:
            target_status = str(params[0])
            if target_status == "approved":
                _, actor, approved_by, mapping_id, current_status = params
            else:
                _, actor, mapping_id, current_status = params
                approved_by = ""
            row = self.db.find(int(mapping_id), status=str(current_status))
            if row:
                row.update(status=target_status, updated_by=actor)
                if target_status == "approved":
                    row.update(approved_by=approved_by, approved_at="fixture-approved")
                self.rowcount = 1
        elif normalized.startswith(f"DELETE FROM dbo.{MAPPING_TABLE}"):
            mapping_id = int(params[0])
            row = self.db.find(mapping_id, status="draft")
            if row:
                self.db.rows.remove(row)
                self.rowcount = 1
        else:
            raise AssertionError(f"unexpected SQL: {normalized}")
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.result[0] if self.result else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.result)


class _Connection:
    def __init__(self, db: "_Db") -> None:
        self.db = db
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return _Cursor(self.db)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class _Db:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.next_id = 1
        self.connections: list[_Connection] = []

    @contextmanager
    def connect(self):
        connection = _Connection(self)
        self.connections.append(connection)
        yield connection

    def find(self, mapping_id: int, *, status: str) -> dict[str, Any] | None:
        return next((row for row in self.rows if row["mapping_id"] == mapping_id and row["status"] == status), None)


def _create(
    db: _Db,
    *,
    company_id: int,
    target_type: str,
    target_gcode: str,
    target_code: str,
    classification: ProductClassification,
    adjustment_only: bool = False,
    effective_from: date = date(2026, 1, 1),
    effective_to: date | None = None,
    version: str = "mapping-v1",
) -> int:
    return create_draft_mapping(
        company_id=company_id,
        target_type=target_type,
        target_gcode=target_gcode,
        target_code=target_code,
        classification=classification,
        adjustment_only=adjustment_only,
        reason_code="fixture",
        effective_from=effective_from,
        effective_to=effective_to,
        mapping_version=version,
        actor="fixture-user",
        connection_factory=db.connect,
    )


def _approve(db: _Db, mapping_id: int) -> None:
    transition_mapping_status(
        mapping_id=mapping_id,
        target_status="approved",
        actor="fixture-approver",
        connection_factory=db.connect,
    )


def main() -> int:
    tests = 0

    schema = CREATE_MAPPING_TABLE_SQL
    assert f"CREATE TABLE dbo.{MAPPING_TABLE}" in schema
    assert "SSAI_ANALYSIS_PROFILES" not in schema
    assert "product_override" in schema and "product_group" in schema and "product_flag" in schema
    assert "commercial" in schema and "financial_adjustment" in schema and "unknown" in schema
    assert "draft" in schema and "approved" in schema and "retired" in schema
    assert "effective_to IS NULL OR effective_to >= effective_from" in schema
    assert "UNIQUE" in schema and "mapping_version" in schema
    assert f"IX_{MAPPING_TABLE}_approved_effective" in CREATE_LOOKUP_INDEX_SQL
    tests += 1

    db = _Db()
    draft_id = _create(
        db,
        company_id=7,
        target_type=TARGET_PRODUCT_GROUP,
        target_gcode="0013",
        target_code="A001",
        classification=ProductClassification.FINANCIAL_ADJUSTMENT,
    )
    before = load_effective_classification_authority(
        company_id=7,
        product_code="00077",
        targets=ProductClassificationTargets(product_group_keys=("0013:A001",)),
        as_of=date(2026, 9, 12),
        connection_factory=db.connect,
    )
    assert before.status is ClassificationStatus.UNMAPPED
    update_draft_mapping(
        mapping_id=draft_id,
        classification=ProductClassification.FINANCIAL_ADJUSTMENT,
        adjustment_only=True,
        reason_code="approved-adjustment-only",
        effective_from=date(2026, 1, 1),
        effective_to=None,
        mapping_version="mapping-v2",
        actor="fixture-editor",
        connection_factory=db.connect,
    )
    _approve(db, draft_id)
    after = shadow_classify_product(
        company_id=7,
        product_code="00077",
        targets=ProductClassificationTargets(product_group_keys=("0013:A001",)),
        as_of=date(2026, 9, 12),
        connection_factory=db.connect,
    )
    assert after.result.classification is ProductClassification.FINANCIAL_ADJUSTMENT
    assert after.result.adjustment_only is True
    assert after.authority.mapping_versions == ("mapping-v2",)
    assert after.authority.policy_version == "product_classification_mapping_v1"
    assert after.authority.mappings[0].approved_by == "fixture-approver"
    tests += 1

    override_id = _create(
        db,
        company_id=7,
        target_type=TARGET_PRODUCT_OVERRIDE,
        target_gcode="",
        target_code="00077",
        classification=ProductClassification.MIXED,
        version="override-v1",
    )
    _approve(db, override_id)
    overridden = shadow_classify_product(
        company_id=7,
        product_code="00077",
        targets=ProductClassificationTargets(product_group_keys=("0013:A001",)),
        as_of=date(2026, 9, 12),
        connection_factory=db.connect,
    )
    assert overridden.result.classification is ProductClassification.MIXED
    assert overridden.result.authority_key == "00077"
    tests += 1

    conflict_id = _create(
        db,
        company_id=7,
        target_type=TARGET_PRODUCT_OVERRIDE,
        target_gcode="",
        target_code="00077",
        classification=ProductClassification.COMMERCIAL,
        version="override-v2",
    )
    _approve(db, conflict_id)
    conflict = shadow_classify_product(
        company_id=7,
        product_code="00077",
        targets=ProductClassificationTargets(product_group_keys=("0013:A001",)),
        as_of=date(2026, 9, 12),
        connection_factory=db.connect,
    )
    assert conflict.result.status is ClassificationStatus.CONFLICT
    assert conflict.result.classification is ProductClassification.UNKNOWN
    tests += 1

    company4 = shadow_classify_product(
        company_id=4,
        product_code="9998",
        product_name="반품 정리 조정 보정",
        targets=ProductClassificationTargets(product_group_keys=("0013:9998",)),
        as_of=date(2026, 9, 12),
        connection_factory=db.connect,
    )
    assert company4.authority.status is ClassificationStatus.UNMAPPED
    assert company4.result.classification is ProductClassification.UNKNOWN
    tests += 1

    future_id = _create(
        db,
        company_id=4,
        target_type=TARGET_PRODUCT_OVERRIDE,
        target_gcode="",
        target_code="00009",
        classification=ProductClassification.COMMERCIAL,
        effective_from=date(2027, 1, 1),
        version="future-v1",
    )
    _approve(db, future_id)
    future = load_effective_classification_authority(
        company_id=4,
        product_code="00009",
        as_of=date(2026, 9, 12),
        connection_factory=db.connect,
    )
    assert future.status is ClassificationStatus.UNMAPPED
    tests += 1

    retired_id = _create(
        db,
        company_id=4,
        target_type=TARGET_PRODUCT_OVERRIDE,
        target_gcode="",
        target_code="00010",
        classification=ProductClassification.MANAGEMENT_ONLY,
        adjustment_only=True,
        version="retired-v1",
    )
    _approve(db, retired_id)
    transition_mapping_status(
        mapping_id=retired_id,
        target_status="retired",
        actor="fixture-retirer",
        connection_factory=db.connect,
    )
    retired = load_effective_classification_authority(
        company_id=4,
        product_code="00010",
        as_of=date(2026, 9, 12),
        connection_factory=db.connect,
    )
    assert retired.status is ClassificationStatus.UNMAPPED
    tests += 1

    delete_id = _create(
        db,
        company_id=4,
        target_type=TARGET_PRODUCT_OVERRIDE,
        target_gcode="",
        target_code="00011",
        classification=ProductClassification.UNKNOWN,
        version="draft-delete-v1",
    )
    assert delete_draft_mapping(mapping_id=delete_id, connection_factory=db.connect)
    assert all(row["mapping_id"] != delete_id for row in db.rows)
    tests += 1

    unknown_id = _create(
        db,
        company_id=4,
        target_type=TARGET_PRODUCT_OVERRIDE,
        target_gcode="",
        target_code="00012",
        classification=ProductClassification.UNKNOWN,
        version="unknown-v1",
    )
    _approve(db, unknown_id)
    explicit_unknown = shadow_classify_product(
        company_id=4,
        product_code="00012",
        as_of=date(2026, 9, 12),
        connection_factory=db.connect,
    )
    assert explicit_unknown.result.status is ClassificationStatus.READY
    assert explicit_unknown.result.classification is ProductClassification.UNKNOWN
    assert explicit_unknown.result.product_code == "00012"
    tests += 1

    assert all(connection.commits + connection.rollbacks <= 1 for connection in db.connections)
    assert any(connection.commits == 1 for connection in db.connections)
    tests += 1

    print(f"PASS product classification DB authority: tests={tests} db_calls=0 runtime_integrations=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
