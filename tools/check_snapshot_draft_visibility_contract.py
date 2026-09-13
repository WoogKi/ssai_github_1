from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot_service import (
    SnapshotGenerationInProgressError,
    build_frequency_snapshot_plan,
    frequency_snapshot_generation_guard,
    generate_frequency_snapshot_draft,
)
from app.services.dashboard_inventory_frequency_snapshot import (
    EXTENDED_RELATIONAL_FREQUENCY_REPRESENTATION,
    RelationalFrequencySnapshot,
)
from app.services.sql_server_snapshot_repository import SnapshotGenerationInspection
from app.services.ssai_snapshot_repository import SnapshotPublishResult, SnapshotReadResult
from app.services.product_classification_contract import ClassificationStatus
from app.services.ssai_product_classification_repository import ProductClassificationAuthorityRead


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _classification_authority(**_kwargs: Any) -> ProductClassificationAuthorityRead:
    return ProductClassificationAuthorityRead(status=ClassificationStatus.READY)


class _PublishedPlusDraftRepository:
    def __init__(self, *, no_op: bool = False) -> None:
        self.key: Any = None
        self.payload: Any = None
        self.relational_snapshot: RelationalFrequencySnapshot | None = None
        self.last_inspection: SnapshotGenerationInspection | None = None
        self.inspect_calls = 0
        self.read_calls = 0
        self.no_op = no_op

    def publish(self, key: Any, payload: Any, *, created_by: str, force: bool = False) -> SnapshotPublishResult:
        self.key = key
        self.payload = payload
        return SnapshotPublishResult(
            status="draft",
            generation_no=2,
            checksum=str(payload["checksum"]),
            manifest_id=22,
            approval_status="pending",
        )

    def publish_relational(
        self,
        snapshot: RelationalFrequencySnapshot,
        *,
        created_by: str,
        force: bool = False,
    ) -> SnapshotPublishResult:
        self.key = snapshot.key
        self.payload = None
        self.relational_snapshot = snapshot
        return SnapshotPublishResult(
            status="published" if self.no_op else "draft",
            generation_no=2,
            checksum=snapshot.checksum,
            no_op=self.no_op,
            manifest_id=22,
            approval_status="approved" if self.no_op else "pending",
        )

    def inspect_generation(self, key: Any, generation_no: int) -> SnapshotGenerationInspection:
        self.inspect_calls += 1
        if key != self.key or generation_no != 2:
            return SnapshotGenerationInspection(status="missing", generation_no=generation_no)
        inspection = SnapshotGenerationInspection(
            status="ready" if self.no_op else "unapproved",
            manifest_status="published" if self.no_op else "draft",
            approval_status="approved" if self.no_op else "pending",
            representation=EXTENDED_RELATIONAL_FREQUENCY_REPRESENTATION,
            relational_snapshot=self.relational_snapshot,
            manifest_id=22,
            generation_no=2,
            checksum=str(self.relational_snapshot.checksum),
        )
        self.last_inspection = inspection
        return inspection

    def read(self, key: Any) -> SnapshotReadResult:
        self.read_calls += 1
        _assert(key == self.key, "operating read must use the same key")
        return SnapshotReadResult(
            status="ready",
            generation_no=2 if self.no_op else 1,
            checksum=str(self.relational_snapshot.checksum) if self.no_op else "published-generation-1",
            approval_status="approved",
        )


def _diagnostics() -> dict[str, int]:
    return {
        "source_row_count": 2,
        "normal_positive_accepted_row_count": 2,
        "normal_positive_duplicate_row_count": 0,
        "normal_positive_conflicting_row_count": 0,
        "normal_positive_missing_key_row_count": 0,
        "normal_positive_nonintegral_row_count": 0,
        "normal_nonpositive_row_count": 0,
        "return_positive_row_count": 0,
        "return_nonpositive_row_count": 0,
        "other_tcode_row_count": 0,
        "normal_positive_row_count": 2,
        "distinct_normal_event_count": 2,
        "conflicting_event_count": 0,
    }


def _generation_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    lifecycle = pd.DataFrame({
        "product_code": ["P1", "P2"],
        "first_normal_inbound_month": ["202501", None],
        "current_stock_present": [1, 0],
        "basis_inbound_present": [0, 1],
    })
    empty_diagnostics = {key: None for key in _diagnostics()}
    stream = pd.DataFrame([
        {"row_kind": "event", "outbound_date": "20251001", "vendor_code": "00007", "product_code": "P1", "stock_code": "00001", "outbound_quantity": 2, "paid_quantity": 2, "mapping_count": 1, "exact_duplicate_row_count": 0, **empty_diagnostics},
        {"row_kind": "event", "outbound_date": "20251002", "vendor_code": "00007", "product_code": "P1", "stock_code": "00001", "outbound_quantity": 1, "paid_quantity": 1, "mapping_count": 1, "exact_duplicate_row_count": 0, **empty_diagnostics},
        {"row_kind": "diagnostics", "outbound_date": "", "vendor_code": "", "product_code": "", "stock_code": "", "outbound_quantity": None, "mapping_count": None, "exact_duplicate_row_count": None, **_diagnostics()},
    ])
    return lifecycle, stream


def test_published_operating_read_and_new_draft_are_distinct() -> None:
    plan = build_frequency_snapshot_plan(company_id=4, evaluation_month="202601", stock_codes=["00001"])
    lifecycle, stream = _generation_frames()

    def query(_company_id: int, sql: str, _params: Any, _timeout: int) -> pd.DataFrame:
        return lifecycle.copy() if "FirstInbound" in sql else stream.copy()

    repository = _PublishedPlusDraftRepository()
    result = generate_frequency_snapshot_draft(
        plan=plan,
        created_by="fixture",
        query_executor=query,
        repository=repository,
        classification_authority_loader=_classification_authority,
    )
    _assert(result["draft"].generation_no == 2, "draft generation must remain generation 2")
    _assert(result["draft_inspection_status"] == "unapproved", "exact generation inspect must expose the draft for approval")
    _assert(repository.last_inspection is not None, "draft workflow must inspect the exact generation")
    _assert(repository.last_inspection.representation == EXTENDED_RELATIONAL_FREQUENCY_REPRESENTATION, "draft inspect must preserve v2 relational representation")
    _assert(repository.last_inspection.relational_snapshot is not None, "draft inspect must return relational authority")
    _assert(result["read_status"] == "ready", "operating read must retain published generation 1")
    _assert(repository.inspect_calls == 1 and repository.read_calls == 1, "exact inspect and operating read each run once")


def test_identical_approved_generation_is_noop_not_a_new_draft_error() -> None:
    plan = build_frequency_snapshot_plan(company_id=4, evaluation_month="202601", stock_codes=["00001"])
    lifecycle, stream = _generation_frames()

    def query(_company_id: int, sql: str, _params: Any, _timeout: int) -> pd.DataFrame:
        return lifecycle.copy() if "FirstInbound" in sql else stream.copy()

    result = generate_frequency_snapshot_draft(
        plan=plan,
        created_by="fixture",
        query_executor=query,
        repository=_PublishedPlusDraftRepository(no_op=True),
        classification_authority_loader=_classification_authority,
    )
    _assert(result["draft"].no_op, "identical approved generation must remain a repository no-op")
    _assert(result["read_status"] == "ready" and result["draft_inspection_status"] == "ready", "approved no-op remains the operating generation")


def test_same_run_key_is_blocked_before_second_generation_starts() -> None:
    plan = build_frequency_snapshot_plan(company_id=4, evaluation_month="202601", stock_codes=["00001"])
    with tempfile.TemporaryDirectory() as root:
        with frequency_snapshot_generation_guard(plan, lock_root=Path(root)):
            try:
                with frequency_snapshot_generation_guard(plan, lock_root=Path(root)):
                    raise AssertionError("duplicate run-key lock unexpectedly acquired")
            except SnapshotGenerationInProgressError:
                pass
            else:
                raise AssertionError("duplicate run-key must fail closed")
        with frequency_snapshot_generation_guard(plan, lock_root=Path(root)):
            pass


def main() -> int:
    test_published_operating_read_and_new_draft_are_distinct()
    test_identical_approved_generation_is_noop_not_a_new_draft_error()
    test_same_run_key_is_blocked_before_second_generation_starts()
    print("PASS: snapshot draft visibility contract tests=3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
