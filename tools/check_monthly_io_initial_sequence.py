"""DB-free gate for the sequential initial monthly IO runner."""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import run_monthly_io_initial_sequence as runner  # noqa: E402


def _expect_error(call, error_type=ValueError) -> None:
    try:
        call()
    except error_type:
        return
    raise AssertionError(f"expected {error_type.__name__}")


def _metadata(company_id: int, *, approved: tuple[str, ...] = ()) -> dict:
    return {"company_id": company_id, "target_hash": "a" * 64,
            "profile_fingerprint": "b" * 64, "scope_fingerprint": "c" * 64,
            "saved_io_count": 45 if company_id == 13 else 35,
            "approved_months": approved, "pending_months": ()}


def _built(company_id: int) -> dict:
    return {"ok": True, "company_id": company_id, "evaluation_month": "202610",
            "price_exact": {"price_mismatch_count": 0},
            "r120_exact": {"normal_monthly_mismatch_count": 0,
                           "return_product_mismatch_count": 0},
            "product_count": 300, "fact_count": 700, "erp_select_count": 5,
            "monthly": {month: {"status": "draft_pending_manual_approval", "saved": True,
                                "manifest_id": 100 + index, "generation_no": 1,
                                "checksum": f"{index + 1:064x}", "fact_count": 100}
                        for index, month in enumerate(runner.MONTHS)}}


def _inspected(company_id: int) -> dict:
    return {"company_id": company_id, "evaluation_month": "202610",
            "price_exact": {"price_mismatch_count": 0},
            "r120_exact": {"normal_monthly_mismatch_count": 0,
                           "return_product_mismatch_count": 0},
            "months": [{"basis_month": month, "manifest_id": 100 + index,
                        "generation_no": 1, "checksum": f"{index + 1:064x}",
                        "status": "inspected_draft"}
                       for index, month in enumerate(runner.MONTHS)]}


def check_guards() -> None:
    assert runner.parse_ids("4,7,13") == (4, 7, 13)
    for value in ("", "4,4", "4,-7", "4,"):
        _expect_error(lambda value=value: runner.parse_ids(value))
    before = datetime(2026, 11, 1, 13, 59, tzinfo=runner.KST)
    after = datetime(2026, 11, 1, 14, tzinfo=runner.KST)
    for cid in runner.PROTECTED_IDS:
        _expect_error(lambda cid=cid: runner.validate_scope((cid,), now=after))
    _expect_error(lambda: runner.validate_scope((8,), now=before))
    runner.validate_scope((8,), now=after)


def check_no_db_default() -> None:
    with patch.object(runner, "list_companies", side_effect=AssertionError("DB touched")), \
            patch.object(runner, "connect_company_analytics_db", side_effect=AssertionError("DB touched")), \
            patch.object(sys, "argv", ["runner"]):
        assert runner.main() == 0


def check_sequential_happy_and_stop() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        calls = []

        def registry_loader(**_kwargs):
            return [{"company_id": 4, "is_active": 1},
                    {"company_id": 7, "is_active": 1},
                    {"company_id": 13, "is_active": 0}]

        def metadata_loader(cid, **_kwargs):
            return _metadata(cid)

        def launcher(command, stdout_path, stderr_path, _timeout):
            cid = int(command[command.index("--company-id") + 1])
            calls.append((cid, tuple(command)))
            assert "--approve" not in command
            assert "--timeout-seconds" in command
            if "build_monthly_io_company.py" in command[2]:
                payload = _built(cid)
                stdout_path.write_text('{"stage":"source_start"}\n' + json.dumps(payload) + "\n",
                                       encoding="utf-8")
            else:
                payload = _inspected(cid)
                stdout_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            stderr_path.write_text("Streamlit warning only\n", encoding="utf-8")
            return 0, 123.4

        outcome = runner.run_sequence(
            company_ids=(4, 7), operator_id="tester", approval_reference="approved-window",
            output_root=root, now=datetime(2026, 11, 2, tzinfo=runner.KST),
            registry_loader=registry_loader, metadata_loader=metadata_loader,
            launcher=launcher,
        )
        assert outcome["status"] == "inspected_no_approval"
        assert [cid for cid, _ in calls] == [4, 4, 7, 7]
        for row in outcome["companies"]:
            assert row["status"] == "inspected_pending_manual_approval"
            assert row["product_count"] == 300 and row["fact_count"] == 700
            assert row["build_elapsed_ms"] == row["inspect_elapsed_ms"] == 123.4
            pin = json.loads(Path(row["approval_batch_file"]).read_text(encoding="utf-8"))
            assert tuple(item["basis_month"] for item in pin["drafts"]) == runner.MONTHS
        assert Path(outcome["run_dir"], "run_summary.json").exists()

        _expect_error(lambda: runner.run_sequence(
            company_ids=(13,), operator_id="tester", approval_reference="approved-window",
            output_root=root, now=datetime(2026, 11, 2, tzinfo=runner.KST),
            registry_loader=registry_loader, metadata_loader=metadata_loader,
            launcher=launcher))
        _expect_error(lambda: runner.run_sequence(
            company_ids=(4,), operator_id="tester", approval_reference="approved-window",
            output_root=ROOT, now=datetime(2026, 11, 2, tzinfo=runner.KST),
            registry_loader=registry_loader, metadata_loader=metadata_loader,
            launcher=launcher))

        failed_calls = []

        def failing_launcher(command, stdout_path, stderr_path, _timeout):
            failed_calls.append(command)
            stdout_path.write_text('{"ok":false}\n', encoding="utf-8")
            stderr_path.write_text("SQL failed\n", encoding="utf-8")
            return 1, 20.0

        failed = runner.run_sequence(
            company_ids=(4, 7), operator_id="tester", approval_reference="approved-window",
            output_root=root, now=datetime(2026, 11, 2, tzinfo=runner.KST),
            registry_loader=registry_loader, metadata_loader=metadata_loader,
            launcher=failing_launcher)
        assert failed["status"] == "stopped" and failed["failed_company_id"] == 4
        assert failed["failed_stage"] == "build_draft" and len(failed_calls) == 1
        assert not list(Path(failed["run_dir"]).glob("*approval_batch.json"))
        assert Path(failed["run_dir"], "run_summary.json").exists()


def check_approved_skip_and_inspection_mismatch() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        registry = lambda **_kw: [{"company_id": 4, "is_active": 1}]

        def no_launch(*_args):
            raise AssertionError("approved months must not be rebuilt")

        skipped = runner.run_sequence(
            company_ids=(4,), operator_id="tester", approval_reference="approved-window",
            output_root=root, now=datetime(2026, 11, 2, tzinfo=runner.KST),
            registry_loader=registry,
            metadata_loader=lambda cid, **_kw: _metadata(cid, approved=runner.MONTHS),
            launcher=no_launch,
        )
        assert skipped["status"] == "completed_with_unverified_skips"
        assert skipped["companies"][0]["status"] == "approved_manifests_skipped_not_revalidated"

        calls = 0

        def mismatch_launch(command, stdout_path, stderr_path, _timeout):
            nonlocal calls
            calls += 1
            cid = int(command[command.index("--company-id") + 1])
            payload = _built(cid) if calls == 1 else _inspected(cid)
            if calls == 2:
                payload["months"][0]["checksum"] = "f" * 64
            stdout_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            stderr_path.write_text("", encoding="utf-8")
            return 0, 10.0

        mismatch = runner.run_sequence(
            company_ids=(4,), operator_id="tester", approval_reference="approved-window",
            output_root=root, now=datetime(2026, 11, 2, tzinfo=runner.KST),
            registry_loader=registry, metadata_loader=lambda cid, **_kw: _metadata(cid),
            launcher=mismatch_launch,
        )
        assert mismatch["status"] == "stopped" and mismatch["failed_stage"] == "independent_exact"
        assert not list(Path(mismatch["run_dir"]).glob("*approval_batch.json"))


def check_preflight_metadata() -> None:
    class Cursor:
        def __init__(self, *, bad_ledger=False):
            self.query = ""
            self.bad_ledger = bad_ledger

        def execute(self, sql, *_params):
            self.query = sql
            return self

        def fetchone(self):
            assert "DB_NAME()" in self.query
            return ("SSAI_ANALYTICS",)

        def fetchall(self):
            if "schema_migrations" in self.query:
                rows = [(m.migration_id, m.checksum) for m in runner.MIGRATIONS]
                return rows[:-1] if self.bad_ledger else rows
            return []

    class Connection:
        def __init__(self, *, bad_ledger=False):
            self.cur = Cursor(bad_ledger=bad_ledger)
            self.closed = False

        def cursor(self):
            return self.cur

        def close(self):
            self.closed = True

    scope = SimpleNamespace(stock_codes=("00001",), product_group_codes=(),
                            product_di_codes=(), product_class_codes=(),
                            io_gu_codes=tuple(str(n) for n in range(45)), stock_mode="real")
    plan = SimpleNamespace(profile_fingerprint="b" * 64, io_gu_codes=scope.io_gu_codes)
    io_scope = SimpleNamespace(unclassified_codes=(), fingerprint=lambda _value: "c" * 64)
    good = Connection()
    with patch.object(runner, "resolve_dashboard_profile_stock_scope", return_value=scope), \
            patch.object(runner, "build_frequency_snapshot_plan", return_value=plan), \
            patch.object(runner, "resolve_saved_io_scope", return_value=io_scope), \
            patch.object(runner, "connect_company_analytics_db", return_value=good), \
            patch.object(runner, "analytics_target_hash", return_value="a" * 64):
        result = runner.load_company_metadata(13)
        assert result["saved_io_count"] == 45 and not result["approved_months"]
        assert good.closed
    bad = Connection(bad_ledger=True)
    with patch.object(runner, "resolve_dashboard_profile_stock_scope", return_value=scope), \
            patch.object(runner, "build_frequency_snapshot_plan", return_value=plan), \
            patch.object(runner, "resolve_saved_io_scope", return_value=io_scope), \
            patch.object(runner, "connect_company_analytics_db", return_value=bad):
        _expect_error(lambda: runner.load_company_metadata(13))
        assert bad.closed


def check_stream_separation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        stdout, stderr = root / "out.log", root / "err.log"
        command = [sys.executable, "-u", "-c",
                   "import sys; print('RESULT'); print('WARNING', file=sys.stderr)"]
        code, elapsed = runner._launch(command, stdout, stderr, 10)
        assert code == 0 and elapsed >= 0
        assert stdout.read_text(encoding="utf-8").strip() == "RESULT"
        assert stderr.read_text(encoding="utf-8").strip() == "WARNING"


def main() -> int:
    with patch.object(runner, "connect_company_analytics_db",
                      side_effect=AssertionError("unmocked Analytics DB connection")):
        check_guards()
        check_no_db_default()
        check_sequential_happy_and_stop()
        check_approved_skip_and_inspection_mismatch()
        check_preflight_metadata()
        check_stream_separation()
    print("PASS monthly IO initial sequential runner DB-free gate (6 groups)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
