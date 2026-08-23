from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot_service import (
    build_frequency_snapshot_plan,
    generate_frequency_snapshot_draft,
)
from app.services.sql_server_snapshot_repository import SnapshotGenerationInspection
from app.services.ssai_snapshot_repository import SnapshotPublishResult, SnapshotReadResult


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class _PublishedPlusDraftRepository:
    def __init__(self) -> None:
        self.key: Any = None
        self.payload: Any = None
        self.inspect_calls = 0
        self.read_calls = 0

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

    def inspect_generation(self, key: Any, generation_no: int) -> SnapshotGenerationInspection:
        self.inspect_calls += 1
        if key != self.key or generation_no != 2:
            return SnapshotGenerationInspection(status="missing", generation_no=generation_no)
        return SnapshotGenerationInspection(
            status="unapproved",
            manifest_status="draft",
            approval_status="pending",
            payload=self.payload,
            manifest_id=22,
            generation_no=2,
            checksum=str(self.payload["checksum"]),
        )

    def read(self, key: Any) -> SnapshotReadResult:
        self.read_calls += 1
        _assert(key == self.key, "operating read must use the same key")
        return SnapshotReadResult(
            status="ready",
            generation_no=1,
            checksum="published-generation-1",
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


def test_published_operating_read_and_new_draft_are_distinct() -> None:
    plan = build_frequency_snapshot_plan(company_id=4, evaluation_month="202601", stock_codes=["00001"])
    aggregate = pd.DataFrame(
        [
            {
                "row_kind": "monthly", "month": "202510", "product_code": "P1", "stock_code": "00001",
                "occurrence_count": 2, "outbound_quantity": 3, "outbound_day_count": 2,
                **_diagnostics(),
            },
            {
                "row_kind": "summary", "month": "", "product_code": "", "stock_code": "",
                "occurrence_count": 0, "outbound_quantity": 0, "outbound_day_count": 0,
                **_diagnostics(),
            },
        ]
    )

    def query(_company_id: int, sql: str, _params: Any, _timeout: int) -> pd.DataFrame:
        return pd.DataFrame({"product_code": ["P1", "P2"]}) if "Rddbc040" in sql else aggregate.copy()

    repository = _PublishedPlusDraftRepository()
    result = generate_frequency_snapshot_draft(
        plan=plan,
        created_by="fixture",
        query_executor=query,
        repository=repository,
    )
    _assert(result["draft"].generation_no == 2, "draft generation must remain generation 2")
    _assert(result["draft_inspection_status"] == "unapproved", "exact generation inspect must expose the draft for approval")
    _assert(result["read_status"] == "ready", "operating read must retain published generation 1")
    _assert(repository.inspect_calls == 1 and repository.read_calls == 1, "exact inspect and operating read each run once")


def main() -> int:
    test_published_operating_read_and_new_draft_are_distinct()
    print("PASS: snapshot draft visibility contract tests=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())