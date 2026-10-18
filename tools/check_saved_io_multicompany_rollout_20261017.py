"""DB-free contract checks for the company-profile Saved I/O Snapshot path."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot import SnapshotContractError  # noqa: E402
from app.services.dashboard_inventory_frequency_snapshot_service import (  # noqa: E402
    DashboardProfileStockScope,
    _generate_frequency_snapshot_draft_locked,
    _plan_with_current_saved_io,
    build_frequency_snapshot_plan,
    frequency_snapshot_key,
    frequency_snapshot_read_keys,
    read_approved_frequency_snapshot,
    selected_io_snapshot_key,
    validate_saved_io_snapshot_profile_key,
)
from app.services.saved_io_scope import resolve_saved_io_scope  # noqa: E402
from app.services.sql_server_snapshot_repository import SqlServerSnapshotRepository  # noqa: E402
from app.services.ssai_snapshot_repository import SnapshotReadResult  # noqa: E402
from tools.inspect_approve_dashboard_inventory_frequency_snapshot import run_approval_workflow  # noqa: E402


def test_company_keys_and_profile_isolation() -> None:
    codes = ("001", "008", "101", "501", "580", "590", "601")
    scope = resolve_saved_io_scope(codes, confirmed_trade_roles=True)
    report = scope.snapshot_role_report()
    assert set().union(*(set(values) for values in report.values())) == set(codes)
    assert sum(map(len, report.values())) == len(codes)
    assert report["inbound_return_not_price"] == ("101",)
    assert report["outbound_return_not_frequency"] == ("601",)
    assert report["normal_outbound"] == ("501", "580", "590")

    for company in (3, 4, 13):
        plan = build_frequency_snapshot_plan(
            company_id=company, evaluation_month="202610", stock_codes=("00001",),
            io_gu_codes=codes,
        )
        assert frequency_snapshot_read_keys(plan)[0] == selected_io_snapshot_key(plan, scope)
        assert frequency_snapshot_read_keys(plan)[3] == frequency_snapshot_key(plan, product_statistics=True)
    other_scope = resolve_saved_io_scope(("001", "501", "601"), confirmed_trade_roles=True)
    plan3 = build_frequency_snapshot_plan(company_id=3, evaluation_month="202610", stock_codes=("00001",), io_gu_codes=codes)
    plan4 = build_frequency_snapshot_plan(company_id=4, evaluation_month="202610", stock_codes=("00001",), io_gu_codes=other_scope.all_codes)
    assert selected_io_snapshot_key(plan3, scope).profile_fingerprint != selected_io_snapshot_key(plan4, other_scope).profile_fingerprint

    without_io = build_frequency_snapshot_plan(company_id=4, evaluation_month="202610", stock_codes=("00001",))
    stored = DashboardProfileStockScope(
        company_id=4, stock_codes=("00001",), profile_status="ready",
        scope_source="dashboard_profile", io_gu_codes=codes,
    )
    with patch("app.services.dashboard_inventory_frequency_snapshot_service.resolve_dashboard_profile_stock_scope", return_value=stored):
        current = _plan_with_current_saved_io(without_io)
    assert current.io_gu_codes == codes
    assert frequency_snapshot_read_keys(current)[0] == selected_io_snapshot_key(current, scope)
    with patch("app.services.dashboard_inventory_frequency_snapshot_service.resolve_dashboard_profile_stock_scope", return_value=stored):
        wrong_stock = build_frequency_snapshot_plan(company_id=4, evaluation_month="202610", stock_codes=("00002",))
        assert _plan_with_current_saved_io(wrong_stock).io_gu_codes == ()


def test_unconfirmed_roles_fail_before_source_calls() -> None:
    codes = ("001", "201", "301", "401", "501", "601", "701", "801", "901")
    scope = resolve_saved_io_scope(codes, confirmed_trade_roles=True)
    report = scope.snapshot_role_report()
    assert report["other_r110_role_unconfirmed"] == ("201", "301", "401")
    assert report["other_r120_role_unconfirmed"] == ("701", "801", "901")
    assert set().union(*(set(values) for values in report.values())) == set(codes)
    plan = build_frequency_snapshot_plan(company_id=4, evaluation_month="202610", stock_codes=("00001",), io_gu_codes=codes)
    assert frequency_snapshot_read_keys(plan)[0] == frequency_snapshot_key(plan, product_statistics=True)

    def forbidden_query(*_args: object) -> None:
        raise AssertionError("unconfirmed roles reached ERP")

    try:
        _generate_frequency_snapshot_draft_locked(
            plan=plan, created_by="offline", query_executor=forbidden_query,
            io_scope=scope, saved_io_full=True,
        )
    except SnapshotContractError as exc:
        assert "201" in str(exc) and "901" in str(exc)
    else:
        raise AssertionError("unconfirmed Saved I/O roles were accepted")


def test_approved_reader_priority_and_legacy_fallback() -> None:
    stored = DashboardProfileStockScope(
        company_id=4, stock_codes=("00001",), profile_status="ready",
        scope_source="dashboard_profile", io_gu_codes=("001", "501"),
    )
    expected = build_frequency_snapshot_plan(
        company_id=4, evaluation_month="202610", stock_codes=("00001",),
        io_gu_codes=stored.io_gu_codes,
    )
    saved, future_profit, old_saved_io, legacy = frequency_snapshot_read_keys(expected)[:4]

    class Repository:
        def __init__(self, available: object) -> None:
            self.available = available
            self.requested: list[object] = []

        def resolve_latest_eligible_key(self, key: object, *, available_through: str) -> object | None:
            assert available_through == "20261017"
            self.requested.append(key)
            return key if key == self.available else None

        def read(self, key: object) -> SnapshotReadResult:
            assert key == self.available
            return SnapshotReadResult(status="ready", generation_no=1)

    for available, expected_attempts in (
        (saved, [saved]), (future_profit, [saved, future_profit]),
        (old_saved_io, [saved, future_profit, old_saved_io]),
        (legacy, [saved, future_profit, old_saved_io, legacy]),
    ):
        repo = Repository(available)
        with patch("app.services.dashboard_inventory_frequency_snapshot_service.resolve_dashboard_profile_stock_scope", return_value=stored), patch(
            "app.services.dashboard_inventory_frequency_snapshot_service._company_snapshot_repository", return_value=repo
        ):
            result = read_approved_frequency_snapshot(
                company_id=4, evaluation_month="202610", stock_codes=("00001",),
                as_of_date="20261017",
            )
        assert result.status == "ready"
        assert repo.requested == expected_attempts


def test_approval_preflight_uses_selected_company_profile() -> None:
    stored = DashboardProfileStockScope(
        company_id=4, stock_codes=("00001",), profile_status="ready",
        scope_source="dashboard_profile", io_gu_codes=("001", "501"),
    )
    plan = build_frequency_snapshot_plan(
        company_id=4, evaluation_month="202610", stock_codes=stored.stock_codes,
        io_gu_codes=stored.io_gu_codes,
    )
    key = selected_io_snapshot_key(plan, resolve_saved_io_scope(stored.io_gu_codes, confirmed_trade_roles=True))
    args = SimpleNamespace(
        company_id=4, contract_version="2.1", stock_code=["00001"],
        evaluation_month="202610", expected_profile_fingerprint=key.profile_fingerprint,
        saved_io_full=True, expected_checksum="0" * 64, generation=1,
    )

    class Repository:
        def inspect_generation(self, inspected_key: object, generation: int) -> None:
            assert inspected_key == key and generation == 1
            raise RuntimeError("offline inspection reached")

    with patch("tools.inspect_approve_dashboard_inventory_frequency_snapshot.resolve_dashboard_profile_stock_scope", return_value=stored):
        try:
            run_approval_workflow(args, Repository())
        except RuntimeError as exc:
            assert str(exc) == "offline inspection reached"
        else:
            raise AssertionError("company 4 approval did not reach exact inspection")


def test_repository_direct_calls_verify_current_profile() -> None:
    stored = DashboardProfileStockScope(
        company_id=4, stock_codes=("00001",), profile_status="ready",
        scope_source="dashboard_profile", io_gu_codes=("001", "501"),
    )
    plan = build_frequency_snapshot_plan(
        company_id=4, evaluation_month="202610", stock_codes=stored.stock_codes,
        io_gu_codes=stored.io_gu_codes,
    )
    valid = selected_io_snapshot_key(plan, resolve_saved_io_scope(stored.io_gu_codes, confirmed_trade_roles=True))
    wrong_plan = build_frequency_snapshot_plan(
        company_id=4, evaluation_month="202610", stock_codes=stored.stock_codes,
        io_gu_codes=("001", "008", "501"),
    )
    wrong = selected_io_snapshot_key(
        wrong_plan, resolve_saved_io_scope(wrong_plan.io_gu_codes, confirmed_trade_roles=True),
    )
    repo = SqlServerSnapshotRepository(
        reader_connection_factory=lambda: (_ for _ in ()).throw(AssertionError("reader connected")),
        writer_connection_factory=lambda: (_ for _ in ()).throw(AssertionError("writer connected")),
    )
    with patch("app.services.dashboard_inventory_frequency_snapshot_service.resolve_dashboard_profile_stock_scope", return_value=stored):
        for operation in (
            lambda: repo.publish_relational(SimpleNamespace(key=wrong), created_by="offline", saved_io_verified=True),
            lambda: repo.approve_checked(
                wrong, 1, expected_checksum="0" * 64, approved_by="offline",
                approval_reason="offline", saved_io_verified=True,
            ),
        ):
            try:
                operation()
            except SnapshotContractError as exc:
                assert "current company profile" in str(exc)
            else:
                raise AssertionError("direct Repository call bypassed the stored profile")
        validate_saved_io_snapshot_profile_key(valid)


if __name__ == "__main__":
    test_company_keys_and_profile_isolation()
    test_unconfirmed_roles_fail_before_source_calls()
    test_approved_reader_priority_and_legacy_fallback()
    test_approval_preflight_uses_selected_company_profile()
    test_repository_direct_calls_verify_current_profile()
    print("saved_io_multicompany_rollout: 5/5 PASS (offline)")
