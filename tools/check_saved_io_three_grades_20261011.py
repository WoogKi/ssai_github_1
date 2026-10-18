"""Offline checks for the opt-in, non-persisting Saved I/O grade calculation."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot import (  # noqa: E402
    PRODUCT_STATISTICS_ALGORITHM_VERSION,
    SAVED_IO_FULL_SHADOW_ALGORITHM_VERSION,
    SAVED_IO_FUTURE_PROFIT_ALGORITHM_VERSION,
    SAVED_IO_AVG_PURCHASE_ALGORITHM_VERSION,
    SnapshotContractError,
    validate_relational_frequency_snapshot,
)
from app.services.dashboard_inventory_frequency_snapshot_service import (  # noqa: E402
    _generate_frequency_snapshot_draft_locked,
    build_frequency_snapshot_plan,
    calculate_saved_io_three_grades_shadow,
    frequency_snapshot_read_keys,
    selected_io_snapshot_key,
)
from app.services.product_classification_contract import ClassificationStatus  # noqa: E402
from app.services.saved_io_scope import resolve_saved_io_scope  # noqa: E402
from app.services.monthly_frequency_aggregate import product_lifecycle_sql  # noqa: E402
from app.services.ssai_product_classification_repository import ProductClassificationAuthorityRead  # noqa: E402
from app.services.sql_server_snapshot_repository import SqlServerSnapshotRepository  # noqa: E402


def _authority(**_kwargs: Any) -> ProductClassificationAuthorityRead:
    return ProductClassificationAuthorityRead(status=ClassificationStatus.READY)


def test_three_grades_without_repository_access() -> None:
    codes = ("001", "002", "008", "090", "580", "590", "690")
    plan = build_frequency_snapshot_plan(
        company_id=13, evaluation_month="202610", stock_codes=("00001", "00012"),
        io_gu_codes=codes,
    )
    lifecycle = pd.DataFrame([
        {"product_code": "P1", "first_normal_inbound_month": "202608",
         "current_stock_present": 1, "basis_inbound_present": 1,
         "avg_purchase_unit_cost": 40, "purchase_price_basis_month": "202609",
         "representative_vendor_code": "BUY1", "purchase_cost_apply_code": "PC1"},
        {"product_code": "P2", "first_normal_inbound_month": None,
         "current_stock_present": 1, "basis_inbound_present": 0,
         "avg_purchase_unit_cost": None, "purchase_price_basis_month": None,
         "representative_vendor_code": "", "purchase_cost_apply_code": ""},
    ])
    raw = pd.DataFrame([
        {"outbound_date": "20260901", "vendor_code": "V1", "outbound_seq": "1",
         "product_code": "P1", "stock_code": "00001", "io_gcode": "0012", "io_tcode": code,
         "cost_apply_code": "C1",
         "outbound_quantity": quantity, "paid_quantity": quantity,
         "supply_amount": amount}
        for code, quantity, amount in (("580", 2, 200), ("590", 3, 300), ("690", -1, -30))
    ])
    calls: list[tuple[str, dict[str, Any]]] = []

    def query(company_id: int, sql: str, binds: dict[str, Any], timeout: int) -> pd.DataFrame:
        assert company_id == 13 and timeout == 120
        calls.append((sql, binds))
        return lifecycle.copy() if "FirstInbound" in sql else raw.copy()

    with patch(
        "app.services.dashboard_inventory_frequency_snapshot_service._company_snapshot_repository",
        side_effect=AssertionError("Shadow must not contact the Snapshot repository"),
    ):
        result = calculate_saved_io_three_grades_shadow(
            plan=plan, created_by="offline-fixture", query_executor=query,
            classification_authority_loader=_authority,
        )
    snapshot = result["relational_snapshot"]
    validate_relational_frequency_snapshot(snapshot)
    assert len(calls) == 2 and "PurchaseCandidates" in calls[0][0]
    assert all("TRY_CONVERT" not in sql.upper() for sql, _ in calls)
    assert "ORDER BY I.Rd11_In_YyMmDd DESC, I.Rd11_In_Seq DESC" in calls[0][0]
    assert calls[0][0].count("I.Rd11_In_Seq DESC") == 2
    assert "L0.purchase_date IS NULL AND LEN(P.product_code) < 5" in calls[0][0]
    assert "I.Rd11_Cost_Apply_Cd" not in calls[0][0]
    assert "ROW_NUMBER() OVER (" in calls[0][0]
    assert "RC.cost_apply_code = LTRIM(RTRIM(I.Rd11_Cost_Apply_Cd))" not in calls[0][0]
    assert "V.Rd03_Cost_Apply_Cd" not in calls[0][0]
    assert "GROUP BY LTRIM(RTRIM(I.Rd11_Physic_Cd)), LEFT(LTRIM(RTRIM(I.Rd11_In_YyMmDd)), 6)" not in calls[0][0]
    legacy_sql, _ = product_lifecycle_sql(stock_codes=plan.stock_codes, cutoff_date="20261010")
    assert "Rd11_Cost_Apply_Cd" not in legacy_sql
    assert {value for key, value in calls[0][1].items() if key.startswith("lifecycle_price_io_")} == {
        "001", "002", "008", "090"
    }
    assert {value for key, value in calls[1][1].items() if key.startswith("statistics_io_")} == {
        "580", "590", "690"
    }
    assert result["operating_eligible"] is False and result["price_authority"] == "shadow_candidate_only"
    assert "draft" not in result and snapshot.key.algorithm_version == SAVED_IO_AVG_PURCHASE_ALGORITHM_VERSION
    assert snapshot.key == selected_io_snapshot_key(plan, resolve_saved_io_scope(codes, confirmed_trade_roles=True))
    assert frequency_snapshot_read_keys(plan)[0] == snapshot.key
    assert frequency_snapshot_read_keys(plan)[1].algorithm_version == SAVED_IO_FUTURE_PROFIT_ALGORITHM_VERSION
    assert frequency_snapshot_read_keys(plan)[3].algorithm_version == PRODUCT_STATISTICS_ALGORITHM_VERSION
    rows = {row["product_code"]: row for row in snapshot.frequency_products}
    assert len(rows) == 2 and rows["P1"]["frequency_grade"] == "F"
    assert rows["P1"]["occurrence_count_3m"] == 2
    assert rows["P1"]["profit_grade"] == "A" and rows["P1"]["contribution_grade"] == "A"
    assert rows["P1"]["return_qty_3m"] == 1
    assert rows["P2"]["profit_grade"] == rows["P2"]["contribution_grade"] == "unavailable"
    source2 = result["source_metrics"]["source2"]
    assert result["source_metrics"]["saved_io_roles"]["normal_outbound"] == ("580", "590")
    assert source2["normal_event_count_by_io"] == {"580": 1, "590": 1}
    assert source2["return_source_quantity_by_io"] == {"690": -1}
    assert source2["return_source_amount_by_io"] == {"690": -30}
    repository = SqlServerSnapshotRepository(
        reader_connection_factory=lambda: (_ for _ in ()).throw(AssertionError("DB read")),
        writer_connection_factory=lambda: (_ for _ in ()).throw(AssertionError("DB write")),
    )
    try:
        repository.publish_relational(snapshot, created_by="offline-fixture")
    except SnapshotContractError as exc:
        assert "cannot be persisted" in str(exc)
    else:
        raise AssertionError("candidate Snapshot reached the repository writer")
    try:
        repository.approve_checked(
            snapshot.key, 1, expected_checksum=snapshot.checksum,
            approved_by="offline-fixture", approval_reason="test",
        )
    except SnapshotContractError as exc:
        assert "cannot be approved" in str(exc)
    else:
        raise AssertionError("candidate Snapshot reached approval")


def test_candidate_draft_write_is_rejected_before_query() -> None:
    codes = ("001", "008", "580")
    plan = build_frequency_snapshot_plan(company_id=13, evaluation_month="202610", stock_codes=("00001",), io_gu_codes=codes)
    scope = resolve_saved_io_scope(codes, confirmed_trade_roles=True)

    try:
        calculate_saved_io_three_grades_shadow(plan=plan, created_by="offline-fixture")
    except SnapshotContractError as exc:
        assert "explicit execution approval" in str(exc)
    else:
        raise AssertionError("live Shadow could start without explicit approval")

    def forbidden_query(*_args: Any) -> pd.DataFrame:
        raise AssertionError("candidate draft must fail before ERP access")

    try:
        _generate_frequency_snapshot_draft_locked(
            plan=plan, created_by="offline-fixture", query_executor=forbidden_query,
            io_scope=scope,
        )
    except SnapshotContractError as exc:
        assert "cannot be written" in str(exc)
    else:
        raise AssertionError("candidate price entered the draft writer")


if __name__ == "__main__":
    test_three_grades_without_repository_access()
    test_candidate_draft_write_is_rejected_before_query()
    print("saved_io_three_grades: 2/2 PASS")
