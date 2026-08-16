from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot import (  # noqa: E402
    SnapshotContractError,
    build_frequency_snapshot_payload,
    build_frequency_snapshot_payload_from_aggregates,
    snapshot_key_from_payload,
    validate_frequency_snapshot_payload,
)
from app.services.dashboard_inventory_frequency_snapshot_service import (  # noqa: E402
    _aggregate_result,
    build_frequency_snapshot_plan,
    generate_frequency_snapshot_draft,
    outbound_monthly_aggregate_sql,
    outbound_values_fixture_sql,
    product_universe_sql,
    query_sqlserver_fixture,
)
from app.services.ssai_snapshot_repository import (  # noqa: E402
    SnapshotPublishResult,
    SnapshotReadResult,
)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _event(
    *,
    day: str,
    seq: int,
    product: str,
    stock: str = "00001",
    tcode: str = "500",
    quantity: int = 1,
    oquantity: int = 0,
) -> dict[str, object]:
    return {
        "outbound_date": day,
        "vendor_code": f"V{seq}",
        "outbound_seq": seq,
        "io_gu_gcode": "0012",
        "io_tcode": tcode,
        "product_code": product,
        "stock_code": stock,
        "quantity": quantity,
        "oquantity": oquantity,
    }


def _diagnostics(normal_event_count: int) -> dict[str, int]:
    return {
        "source_row_count": normal_event_count,
        "normal_positive_accepted_row_count": normal_event_count,
        "normal_positive_duplicate_row_count": 0,
        "normal_positive_conflicting_row_count": 0,
        "normal_positive_missing_key_row_count": 0,
        "normal_positive_nonintegral_row_count": 0,
        "normal_nonpositive_row_count": 0,
        "return_positive_row_count": 0,
        "return_nonpositive_row_count": 0,
        "other_tcode_row_count": 0,
        "normal_positive_row_count": normal_event_count,
        "distinct_normal_event_count": normal_event_count,
        "conflicting_event_count": 0,
        "diagnostic_contract_version": 2,
    }


def test_sql_contract() -> None:
    plan = build_frequency_snapshot_plan(
        company_id=4,
        evaluation_month="202601",
        stock_codes=["00247", "00001", "00001"],
    )
    _assert(plan.basis_months == ("202510", "202511", "202512"), "basis months mismatch")
    _assert(plan.stock_codes == ("00001", "00247"), "stock scope must be canonical")
    universe_sql, universe_binds = product_universe_sql()
    _assert("Rddbc040" in universe_sql and not universe_binds, "official universe query missing")
    _assert("Rd04_Del_Flag" not in universe_sql, "deleted-flag products must remain in universe")
    sql, binds = outbound_monthly_aggregate_sql(plan)
    compact = " ".join(sql.split())
    for fragment in (
        "Rd12_Out_YyMmDd >= :basis_from",
        "Rd12_Out_YyMmDd <= :basis_to",
        "io_gcode = '0012'",
        "BETWEEN 500 AND 599",
        "outbound_quantity > 0",
        "ROW_NUMBER() OVER",
        "return_nonpositive_row_count",
    ):
        _assert(fragment in compact, f"SQL contract missing: {fragment}")
    for forbidden in ("Rd12_Fixed_Flag", "Rd12_Record_Flag", "Rd12_Reform_Flag", "Rd12_Validation"):
        _assert(forbidden not in sql, f"flag must not exclude rows: {forbidden}")
    _assert(binds["basis_from"] == "20251001" and binds["basis_to"] == "20251231", "date binds mismatch")
    _assert({binds["stock_0"], binds["stock_1"]} == {"00001", "00247"}, "stock binds mismatch")


def test_values_fixture_base_row_mapping() -> None:
    sql, binds = outbound_values_fixture_sql([
        _event(day="20251001", seq=1, product="P1", quantity=2, oquantity=3),
    ])
    _assert(binds["fixture_0_io_gcode"] == "0012", "raw io_gu_gcode was not mapped")
    _assert(binds["fixture_0_outbound_quantity"] == Decimal("5"), "raw quantities were not combined")
    _assert("CAST(:fixture_0_outbound_quantity AS decimal(38, 6))" in sql, "quantity type is implicit")
    _assert("CAST(:fixture_0_io_gcode AS varchar(100))" in sql, "Tcode group type is implicit")


def test_python_aggregate_equivalence() -> None:
    rows = [
        _event(day="20251001", seq=1, product="P1", quantity=2, oquantity=1),
        _event(day="20251002", seq=2, product="P1", tcode="599", quantity=4),
        _event(day="20251103", seq=3, product="P2", stock="00247", quantity=5),
        _event(day="20251104", seq=4, product="P2", tcode="600", quantity=9),
        _event(day="20251205", seq=5, product="P3", quantity=0),
    ]
    event_payload = build_frequency_snapshot_payload(
        company_id=4,
        evaluation_month="202601",
        rows=rows,
        product_codes=["P1", "P2", "P3", "P4"],
        stock_codes=["00001", "00247"],
    )
    normal_count = int(event_payload["summary"]["normal_event_count"])
    aggregate_payload = build_frequency_snapshot_payload_from_aggregates(
        company_id=4,
        evaluation_month="202601",
        monthly_rows=event_payload["monthly_activity"],
        product_codes=["P1", "P2", "P3", "P4"],
        stock_codes=["00001", "00247"],
        source_diagnostics=_diagnostics(normal_count),
    )
    _assert(aggregate_payload["monthly_activity"] == event_payload["monthly_activity"], "monthly aggregate differs")
    _assert(aggregate_payload["product_frequency"] == event_payload["product_frequency"], "frequency grades differ")
    _assert(aggregate_payload["summary"] == event_payload["summary"], "summary differs")
    _assert(aggregate_payload["summary"]["grade_counts"]["X"] == 2, "zero-event products must be X")
    _assert(
        aggregate_payload["source_contract"]["fingerprint_mode"] == "monthly_aggregate_v1",
        "aggregate fingerprint mode missing",
    )
    read = validate_frequency_snapshot_payload(
        aggregate_payload, expected_key=snapshot_key_from_payload(aggregate_payload)
    )
    _assert(read.status == "ready", "aggregate payload validator rejected v2 diagnostics")


def _valid_sql_fixture_rows() -> list[dict[str, object]]:
    return [
        _event(day="20251001", seq=1, product="P1", quantity=2, oquantity=1),
        _event(day="20251001", seq=1, product="P1", quantity=2, oquantity=1),  # exact duplicate
        _event(day="20251002", seq=2, product="P1", tcode="599", quantity=4),
        _event(day="20251103", seq=3, product="P2", stock="00247", quantity=5),
        _event(day="20251104", seq=4, product="P2", tcode="600", quantity=9),
        _event(day="20251105", seq=5, product="P3", tcode="601", quantity=0),
        _event(day="20251205", seq=6, product="P3", quantity=0),
        _event(day="20251206", seq=7, product="P4", tcode="090", quantity=4),
    ]


def _payload_from_sql_fixture(rows: list[dict[str, object]]) -> dict[str, object]:
    sql, binds = outbound_values_fixture_sql(rows)
    monthly_rows, diagnostics = _aggregate_result(query_sqlserver_fixture(sql, binds))
    return build_frequency_snapshot_payload_from_aggregates(
        company_id=4,
        evaluation_month="202601",
        monthly_rows=monthly_rows,
        product_codes=["P1", "P2", "P3", "P4"],
        stock_codes=["00001", "00247"],
        source_diagnostics=diagnostics,
    )


def test_sqlserver_values_python_equivalence() -> None:
    """Directly compare a SQL Server VALUES CTE result against the raw-event helper."""
    rows = _valid_sql_fixture_rows()
    sql_payload = _payload_from_sql_fixture(rows)
    python_payload = build_frequency_snapshot_payload(
        company_id=4,
        evaluation_month="202601",
        rows=rows,
        product_codes=["P1", "P2", "P3", "P4"],
        stock_codes=["00001", "00247"],
    )
    _assert(sql_payload["monthly_activity"] == python_payload["monthly_activity"], "SQL/Python monthly result differs")
    _assert(sql_payload["product_frequency"] == python_payload["product_frequency"], "SQL/Python grades differ")
    diagnostics = sql_payload["source_diagnostics"]
    _assert(diagnostics["source_row_count"] == len(rows), "SQL diagnostics source total differs")
    _assert(diagnostics["normal_positive_duplicate_row_count"] == 1, "exact duplicate diagnostic differs")


def test_sql_diagnostics_fail_closed() -> None:
    invalid_rows = [
        _event(day="20251001", seq=1, product="P1"),
        {**_event(day="20251002", seq=2, product="P2"), "vendor_code": ""},
        {**_event(day="20251003", seq=3, product="P3"), "quantity": "1.5"},
        _event(day="20251004", seq=4, product="P4"),
        _event(day="20251004", seq=4, product="P5"),
    ]
    sql, binds = outbound_values_fixture_sql(invalid_rows)
    try:
        _aggregate_result(query_sqlserver_fixture(sql, binds))
    except SnapshotContractError:
        pass
    else:
        raise AssertionError("invalid SQL source diagnostics must fail closed")


class _DraftRepository:
    def __init__(self) -> None:
        self.published = None

    def publish(self, key, payload, *, created_by: str, force: bool = False):
        self.published = (key, payload, created_by, force)
        return SnapshotPublishResult(
            status="draft", generation_no=7, checksum=str(payload["checksum"]), manifest_id=17,
            approval_status="pending",
        )

    def read(self, key):
        return SnapshotReadResult(status="unapproved", generation_no=7, manifest_id=17)


def test_generation_boundary() -> None:
    plan = build_frequency_snapshot_plan(company_id=4, evaluation_month="202601", stock_codes=["00001"])
    aggregate = pd.DataFrame(
        [
            {
                "row_kind": "monthly", "month": "202510", "product_code": "P1", "stock_code": "00001",
                "occurrence_count": 2, "outbound_quantity": 3, "outbound_day_count": 2,
                **_diagnostics(2),
            },
            {
                "row_kind": "summary", "month": "", "product_code": "", "stock_code": "",
                "occurrence_count": 0, "outbound_quantity": 0, "outbound_day_count": 0,
                **_diagnostics(2),
            },
        ]
    )
    calls: list[str] = []

    def query(_company_id, sql, _params, timeout):
        calls.append(sql)
        _assert(_company_id == 4 and timeout == 33, "company/timeout contract mismatch")
        if "Rddbc040" in sql:
            return pd.DataFrame({"product_code": ["P1", "P2"]})
        return aggregate.copy()

    repo = _DraftRepository()
    progress: list[str] = []
    result = generate_frequency_snapshot_draft(
        plan=plan,
        created_by="fixture",
        timeout_seconds=33,
        query_executor=query,
        repository=repo,
        progress_reporter=progress.append,
    )
    _assert(len(calls) == 2, "generation must issue exactly two ERP reads")
    _assert(result["read_status"] == "unapproved", "draft must fail closed before approval")
    _assert(result["draft"].status == "draft", "generator must not approve/publish")
    _assert(result["payload"]["summary"]["grade_counts"]["X"] == 1, "universe X row missing")
    _assert(progress == ["제품 조회 중", "출고 집계 중", "등급 계산 중", "draft 저장 중"], "progress phases mismatch")


def test_diagnostics_fail_closed() -> None:
    frame = pd.DataFrame(
        [{"row_kind": "summary", "normal_positive_missing_key_row_count": 1, **_diagnostics(0)}]
    )
    frame.loc[0, "normal_positive_missing_key_row_count"] = 1
    try:
        _aggregate_result(frame)
    except SnapshotContractError:
        pass
    else:
        raise AssertionError("incomplete event key must fail closed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-sqlserver", action="store_true", help="run the direct SQL Server VALUES CTE equivalence")
    args = parser.parse_args()
    tests = (
        test_sql_contract,
        test_values_fixture_base_row_mapping,
        test_python_aggregate_equivalence,
        test_generation_boundary,
        test_diagnostics_fail_closed,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    if args.live_sqlserver:
        for test in (test_sqlserver_values_python_equivalence, test_sql_diagnostics_fail_closed):
            test()
            print(f"PASS {test.__name__}")
    print(f"RESULT OK tests={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
