"""DB-free guard for the 202611+ operating Snapshot algorithm."""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot import (  # noqa: E402
    PRODUCT_STATISTICS_ALGORITHM_VERSION,
    SAVED_IO_AVG_PURCHASE_ALGORITHM_VERSION,
    SnapshotContractError,
)
from app.services.dashboard_inventory_frequency_snapshot_service import (  # noqa: E402
    build_frequency_snapshot_plan,
    generate_frequency_snapshot_draft,
    require_unified_snapshot_algorithm,
    selected_io_snapshot_key,
)
from app.services.saved_io_scope import resolve_saved_io_scope  # noqa: E402
from tools import generate_dashboard_inventory_frequency_snapshot as generate_cli  # noqa: E402
from tools import inspect_approve_dashboard_inventory_frequency_snapshot as inspect_cli  # noqa: E402


def _plan(company_id: int, month: str):
    return build_frequency_snapshot_plan(
        company_id=company_id, evaluation_month=month,
        stock_codes=("00001",), io_gu_codes=("001", "501", "601"),
    )


def test_service_blocks_legacy_before_sources() -> None:
    for company_id in (3, 4, 7, 8, 40):
        for month in ("202611", "202612"):
            with patch(
                "app.services.dashboard_inventory_frequency_snapshot_service._generate_frequency_snapshot_draft_locked",
                side_effect=AssertionError("source must not be called"),
            ):
                try:
                    generate_frequency_snapshot_draft(plan=_plan(company_id, month), created_by="fixture")
                except SnapshotContractError as exc:
                    assert "saved-io-full" in str(exc)
                else:
                    raise AssertionError("legacy generation was not blocked")
    require_unified_snapshot_algorithm("202610", saved_io_full=False)
    require_unified_snapshot_algorithm("202611", saved_io_full=True)


def test_saved_io_key_is_shared_across_companies() -> None:
    for company_id in (3, 4, 7, 8, 40):
        plan = _plan(company_id, "202611")
        io_scope = resolve_saved_io_scope(plan.io_gu_codes, confirmed_trade_roles=True)
        key = selected_io_snapshot_key(plan, io_scope)
        assert key.algorithm_version == SAVED_IO_AVG_PURCHASE_ALGORITHM_VERSION
        assert key.schema_version == "2.1" and len(key.profile_fingerprint) == 64


def test_generation_cli_guard_and_dry_run() -> None:
    with patch.object(sys, "argv", [
        "generate", "--company-id", "4", "--evaluation-month", "202611", "--dashboard-profile",
    ]), patch.object(generate_cli, "resolve_dashboard_profile_stock_scope", side_effect=AssertionError("DB accessed")):
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                generate_cli.main()
            except SystemExit as exc:
                assert exc.code == 2
            else:
                raise AssertionError("CLI accepted a legacy plan")

    profile = SimpleNamespace(
        company_id=4, stock_codes=("00001",), product_group_codes=(), product_di_codes=(),
        product_class_codes=(), io_gu_codes=("001", "501", "601"), stock_mode="real",
        scope_source="dashboard_profile",
    )
    output = io.StringIO()
    with patch.object(sys, "argv", [
        "generate", "--company-id", "4", "--evaluation-month", "202611",
        "--dashboard-profile", "--saved-io-full",
    ]), patch.object(generate_cli, "resolve_dashboard_profile_stock_scope", return_value=profile), contextlib.redirect_stdout(output):
        assert generate_cli.main() == 0
    payload = json.loads(output.getvalue())
    assert payload["mode"] == "dry-run"
    assert payload["algorithm_version"] == SAVED_IO_AVG_PURCHASE_ALGORITHM_VERSION
    assert payload["erp_read_plan"]["sql_call_count"] == 2
    assert len(payload["product_scope"]["profile_fingerprint"]) == 64


def _inspection_args(month: str, *, approve: bool) -> list[str]:
    args = [
        "inspect", "--company-id", "4", "--evaluation-month", month,
        "--stock-code", "00001", "--generation", "1", "--expected-checksum", "a" * 64,
        "--expected-product-count", "1", "--expected-normal-event-count", "1",
        "--expected-source-row-count", "1", "--expected-grade-counts",
        "A=1,B=0,C=0,D=0,E=0,F=0,X=0", "--contract-version", "2.1",
        "--expected-profile-fingerprint", "b" * 64,
    ]
    if approve:
        args += ["--approve", "--approved-by", "fixture", "--approval-reason", "fixture"]
    return args


def test_approval_blocks_legacy_but_inspection_remains_available() -> None:
    output = io.StringIO()
    with patch.object(sys, "argv", _inspection_args("202611", approve=True)), patch.object(
        inspect_cli, "SqlServerSnapshotRepository", return_value=object()
    ), patch("app.services.snapshot_operation_history.record_event", return_value=True), contextlib.redirect_stdout(output):
        assert inspect_cli.main() == 1
    assert "saved-io-full" in output.getvalue()

    output = io.StringIO()
    with patch.object(sys, "argv", _inspection_args("202610", approve=False)), patch.object(
        inspect_cli, "SqlServerSnapshotRepository", return_value=object()
    ), patch("app.services.snapshot_operation_history.record_event", return_value=True
    ), patch.object(
        inspect_cli, "run_approval_workflow",
        side_effect=lambda args, _repo, **_kwargs: {"algorithm_version": inspect_cli._key(args).algorithm_version},
    ), contextlib.redirect_stdout(output):
        assert inspect_cli.main() == 0
    assert json.loads(output.getvalue())["algorithm_version"] == PRODUCT_STATISTICS_ALGORITHM_VERSION


def test_runbook_uses_the_unified_key() -> None:
    runbook = (ROOT / "docs/03_runbook/SNAPSHOT_202610_MONTH_END_RUNBOOK_202611.md").read_text(encoding="utf-8")
    commands = [line for line in runbook.splitlines() if "tools\\generate_dashboard_inventory_frequency_snapshot.py" in line or "tools\\inspect_approve_dashboard_inventory_frequency_snapshot.py" in line]
    assert len(commands) == 3
    assert all("--saved-io-full" in line for line in commands)
    assert SAVED_IO_AVG_PURCHASE_ALGORITHM_VERSION in runbook


def main() -> int:
    tests = (
        test_service_blocks_legacy_before_sources,
        test_saved_io_key_is_shared_across_companies,
        test_generation_cli_guard_and_dry_run,
        test_approval_blocks_legacy_but_inspection_remains_available,
        test_runbook_uses_the_unified_key,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS unified Snapshot version contract ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
