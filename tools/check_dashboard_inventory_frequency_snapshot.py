from __future__ import annotations

import copy
import random
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot import (  # noqa: E402
    ALGORITHM_VERSION,
    FREQUENCY_INSUFFICIENT_GRADE,
    SCHEMA_VERSION,
    SNAPSHOT_TYPE,
    SnapshotContractError,
    assign_frequency_grades,
    build_frequency_snapshot_payload,
    calculate_payload_checksum,
    completed_month_basis,
    evaluate_frequency_snapshot,
    frequency_rows_for_universe,
    is_normal_outbound_tcode,
    snapshot_key_from_payload,
    validate_frequency_snapshot_payload,
)
from app.services.ssai_snapshot_repository import (  # noqa: E402
    SNAPSHOT_STATUS_CORRUPT,
    SNAPSHOT_STATUS_MISSING,
    SNAPSHOT_STATUS_READY,
    SNAPSHOT_STATUS_STALE,
    SNAPSHOT_STATUS_VERSION_MISMATCH,
    SnapshotKey,
    SnapshotPublishResult,
    SnapshotReadResult,
    SnapshotRepository,
)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _row(
    seq: int,
    *,
    outbound_date: str = "20251010",
    vendor: str = "00001",
    tcode: str = "501",
    product: str = "P1",
    stock: str = "00001",
    quantity: int = 1,
    oquantity: int = 0,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "outbound_date": outbound_date,
        "vendor_code": vendor,
        "outbound_seq": seq,
        "io_gu_gcode": "0012",
        "io_tcode": tcode,
        "product_code": product,
        "stock_code": stock,
        "quantity": quantity,
        "oquantity": oquantity,
        **extra,
    }


def _build(rows: list[Mapping[str, Any]], **overrides: Any) -> dict[str, Any]:
    args = {
        "company_id": "TEST_COMPANY",
        "evaluation_month": "202601",
        "rows": rows,
        "product_codes": ["P1", "P2", "P3"],
        "stock_codes": ["00001"],
        "source_watermark": "fixture-20251231",
        "source_watermark_status": "verified",
    }
    args.update(overrides)
    return build_frequency_snapshot_payload(**args)


def test_tcode_and_event_contract() -> None:
    _assert(is_normal_outbound_tcode("12", "500"), "normalized 0012/500 must be accepted")
    _assert(is_normal_outbound_tcode("0012", 599), "599 must be accepted")
    _assert(not is_normal_outbound_tcode("0012", "5"), "005 must not be treated as 5xx")
    _assert(not is_normal_outbound_tcode("0012", "600"), "6xx return must be excluded")
    _assert(not is_normal_outbound_tcode("0013", "500"), "wrong group must be excluded")

    rows = [
        _row(1, fixed_flag="anything", record_flag="anything"),
        _row(2, tcode="599", oquantity=2),
        _row(3, tcode="600", quantity=100),
        _row(4, quantity=0),
        _row(5, quantity=-1),
    ]
    payload = _build(rows)
    _assert(payload["summary"]["normal_event_count"] == 2, "only positive 5xx events count")
    _assert(payload["monthly_activity"][0]["outbound_quantity"] == 4, "quantities must add")
    _assert(payload["source_contract"]["flag_exclusion_fields"] == [], "flags must not filter")


def test_month_scope_and_deduplication() -> None:
    basis = completed_month_basis("202601")
    _assert(basis.months == ("202510", "202511", "202512"), "prior three completed months")
    _assert(basis.basis_from == "20251001" and basis.basis_to == "20251231", "basis dates")
    duplicate = _row(1)
    payload = _build(
        [
            duplicate,
            dict(duplicate),
            _row(2, outbound_date="20251101", stock="00002"),
            _row(3, outbound_date="20260101"),
        ]
    )
    _assert(payload["summary"]["normal_event_count"] == 1, "dedupe and stock/month scope")
    _assert(payload["monthly_activity"][0]["occurrence_count"] == 1, "one occurrence")

    conflicting = dict(duplicate, product_code="P2")
    try:
        _build([duplicate, conflicting])
    except SnapshotContractError:
        pass
    else:
        raise AssertionError("conflicting duplicate event must fail closed")


def test_grade_bands_and_zero() -> None:
    rows = assign_frequency_grades(
        {"P1": 2, **{f"P{index}": 1 for index in range(2, 10)}},
        [f"P{index}" for index in range(1, 10)],
    )
    grades = {row["product_code"]: row["frequency_grade"] for row in rows}
    _assert(grades["P1"] == "A", "first group starts in A")
    _assert(all(grades[f"P{index}"] == "B" for index in range(2, 10)), "tie group is not split")

    tied = assign_frequency_grades({"P1": 1, "P2": 1, "P3": 1}, ["P1", "P2", "P3"])
    _assert({row["frequency_grade"] for row in tied} == {"A"}, "large tie remains one grade")
    zero = assign_frequency_grades({}, ["P1", "P2"])
    _assert({row["frequency_grade"] for row in zero} == {"X"}, "valid zero occurrences are X")


def test_payload_determinism_and_fail_closed() -> None:
    rows = [_row(1), _row(2, outbound_date="20251110", product="P2")]
    first = _build(rows)
    shuffled = list(rows)
    random.Random(7).shuffle(shuffled)
    second = _build(shuffled)
    _assert(first == second, "same logical input must produce identical payload/checksum")

    key = snapshot_key_from_payload(first)
    ready = evaluate_frequency_snapshot(first, expected_key=key)
    _assert(ready.status == SNAPSHOT_STATUS_READY, "valid snapshot must be ready")

    cases: list[tuple[str, SnapshotReadResult]] = []
    cases.append((SNAPSHOT_STATUS_MISSING, evaluate_frequency_snapshot(None, expected_key=key)))
    stale_key = SnapshotKey(
        company_id=key.company_id,
        snapshot_type=key.snapshot_type,
        evaluation_month="202602",
        scope_fingerprint=key.scope_fingerprint,
        schema_version=key.schema_version,
        algorithm_version=key.algorithm_version,
    )
    cases.append((SNAPSHOT_STATUS_STALE, evaluate_frequency_snapshot(first, expected_key=stale_key)))
    corrupt = copy.deepcopy(first)
    corrupt["summary"]["normal_event_count"] = 999
    cases.append((SNAPSHOT_STATUS_CORRUPT, evaluate_frequency_snapshot(corrupt, expected_key=key)))
    old_version = copy.deepcopy(first)
    old_version["schema_version"] = "0.9"
    cases.append(
        (SNAPSHOT_STATUS_VERSION_MISMATCH, evaluate_frequency_snapshot(old_version, expected_key=key))
    )
    for expected_status, result in cases:
        _assert(result.status == expected_status, f"expected {expected_status}, got {result.status}")
        unavailable = frequency_rows_for_universe(result, ["P1", "P2"])
        _assert(
            {row["frequency_grade"] for row in unavailable} == {FREQUENCY_INSUFFICIENT_GRADE},
            f"{expected_status} must not become X",
        )

    unverified = _build(rows, source_watermark_status="unverified")
    unverified_result = validate_frequency_snapshot_payload(
        unverified, expected_key=snapshot_key_from_payload(unverified)
    )
    _assert(
        unverified_result.status == SNAPSHOT_STATUS_READY,
        "watermark provenance and human approval/read status must remain separate",
    )


def _rechecksum(payload: dict[str, Any]) -> dict[str, Any]:
    payload["checksum"] = calculate_payload_checksum(payload)
    return payload


def test_payload_internal_corruption() -> None:
    payload = _build([_row(1), _row(2, outbound_date="20251110", product="P2")])
    key = snapshot_key_from_payload(payload)
    corruptions: list[tuple[str, dict[str, Any]]] = []

    duplicate_product = copy.deepcopy(payload)
    duplicate_product["product_frequency"].append(copy.deepcopy(duplicate_product["product_frequency"][0]))
    corruptions.append(("duplicate product", _rechecksum(duplicate_product)))

    duplicate_monthly = copy.deepcopy(payload)
    duplicate_monthly["monthly_activity"].append(copy.deepcopy(duplicate_monthly["monthly_activity"][0]))
    corruptions.append(("duplicate monthly grain", _rechecksum(duplicate_monthly)))

    negative = copy.deepcopy(payload)
    negative["monthly_activity"][0]["outbound_quantity"] = -1
    corruptions.append(("negative aggregate", _rechecksum(negative)))

    summary = copy.deepcopy(payload)
    summary["summary"]["normal_event_count"] += 1
    corruptions.append(("summary mismatch", _rechecksum(summary)))

    grades = copy.deepcopy(payload)
    grades["product_frequency"][0]["frequency_grade"] = "E"
    grades["summary"]["grade_counts"] = {"A": 0, "B": 0, "C": 0, "D": 0, "E": 1, "X": 2}
    corruptions.append(("grade mismatch", _rechecksum(grades)))

    for label, corrupted in corruptions:
        result = validate_frequency_snapshot_payload(corrupted, expected_key=key)
        _assert(result.status == SNAPSHOT_STATUS_CORRUPT, f"{label} must be corrupt")


class _FixtureRepository:
    def publish(
        self,
        key: SnapshotKey,
        payload: Mapping[str, Any],
        *,
        created_by: str,
        force: bool = False,
    ) -> SnapshotPublishResult:
        return SnapshotPublishResult(status=SNAPSHOT_STATUS_READY, generation_no=1)

    def read(self, key: SnapshotKey) -> SnapshotReadResult:
        return SnapshotReadResult(status=SNAPSHOT_STATUS_MISSING)

    def status(self, key: SnapshotKey) -> str:
        return SNAPSHOT_STATUS_MISSING

    def approve(
        self,
        key: SnapshotKey,
        generation_no: int,
        *,
        approved_by: str,
        approval_reason: str,
    ) -> SnapshotPublishResult:
        return SnapshotPublishResult(status=SNAPSHOT_STATUS_READY, generation_no=generation_no)

    def invalidate(self, key: SnapshotKey, *, reason: str, invalidated_by: str) -> None:
        return None

    def replace(
        self,
        key: SnapshotKey,
        payload: Mapping[str, Any],
        *,
        created_by: str,
    ) -> SnapshotPublishResult:
        return SnapshotPublishResult(status=SNAPSHOT_STATUS_READY, generation_no=2)


def test_repository_boundary() -> None:
    _assert(isinstance(_FixtureRepository(), SnapshotRepository), "repository Protocol must be portable")
    _assert(SNAPSHOT_TYPE and SCHEMA_VERSION and ALGORITHM_VERSION, "version contract is required")


def main() -> int:
    tests = (
        test_tcode_and_event_contract,
        test_month_scope_and_deduplication,
        test_grade_bands_and_zero,
        test_payload_determinism_and_fail_closed,
        test_payload_internal_corruption,
        test_repository_boundary,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS dashboard inventory frequency snapshot contract ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
