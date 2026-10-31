"""Sequential initial monthly IO Draft build and independent inspection.

No approval is performed. Database work requires an explicit --execute scope.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot_service import (  # noqa: E402
    build_frequency_snapshot_plan, resolve_dashboard_profile_stock_scope,
)
from app.services.monthly_io_components import (  # noqa: E402
    MONTHLY_IO_ALGORITHM_VERSION, MONTHLY_IO_SCHEMA_VERSION,
)
from app.services.saved_io_scope import resolve_saved_io_scope  # noqa: E402
from app.services.ssai_analytics_snapshot_migration import MIGRATIONS  # noqa: E402
from app.services.ssai_analytics_target_resolver import connect_company_analytics_db  # noqa: E402
from app.services.ssai_company_admin_service import list_companies  # noqa: E402
from app.services.snapshot_operation_history import analytics_target_hash  # noqa: E402


EVALUATION_MONTH = "202610"
MONTHS = tuple(f"2026{month:02d}" for month in range(3, 10))
PRIORITY_IDS = (4, 7, 9, 10, 11, 12, 13)
PROTECTED_IDS = (1, 2, 3, 6, 40)
KST = timezone(timedelta(hours=9))
COMPANY8_START = datetime(2026, 11, 1, 14, tzinfo=KST)


def parse_ids(value: str) -> tuple[int, ...]:
    parts = tuple(part.strip() for part in value.split(","))
    if not parts or any(not part.isdecimal() or int(part) <= 0 for part in parts):
        raise ValueError("company IDs must be positive comma-separated integers")
    ids = tuple(int(part) for part in parts)
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate company ID")
    return ids


def validate_scope(ids: tuple[int, ...], *, now: datetime) -> None:
    if not ids or set(ids) & set(PROTECTED_IDS):
        raise ValueError("scope includes a protected or missing company")
    if 8 in ids and now.astimezone(KST) < COMPANY8_START:
        raise ValueError("company 8 monthly work is blocked until 2026-11-01 14:00 KST")


def load_company_metadata(company_id: int, *, timeout: int = 10) -> dict[str, Any]:
    scope = resolve_dashboard_profile_stock_scope(company_id=company_id)
    plan = build_frequency_snapshot_plan(
        company_id=company_id, evaluation_month=EVALUATION_MONTH,
        stock_codes=scope.stock_codes, product_group_codes=scope.product_group_codes,
        product_di_codes=scope.product_di_codes, product_class_codes=scope.product_class_codes,
        io_gu_codes=scope.io_gu_codes, stock_mode=scope.stock_mode,
    )
    io_scope = resolve_saved_io_scope(plan.io_gu_codes, confirmed_trade_roles=True)
    if io_scope.unclassified_codes or (company_id == 13 and len(plan.io_gu_codes) != 45):
        raise ValueError("stored Saved IO scope is incomplete or changed")
    scope_key = io_scope.fingerprint(plan.profile_fingerprint)
    conn = connect_company_analytics_db(company_id, "reader", timeout=timeout)
    try:
        cursor = conn.cursor()
        identity = cursor.execute("SELECT DB_NAME()").fetchone()
        if identity is None or str(identity[0]).upper() != "SSAI_ANALYTICS":
            raise ValueError("Analytics database identity mismatch")
        ledger = cursor.execute(
            "SELECT TOP (20) migration_id,migration_checksum FROM snapshot.schema_migrations"
        ).fetchall()
        recorded = {str(row[0]): str(row[1]) for row in ledger}
        if (len(recorded) != len(ledger) or
                any(recorded.get(migration.migration_id) != migration.checksum
                    for migration in MIGRATIONS)):
            raise ValueError("Migration 001-011 ledger is missing or differs from code")
        marks = ",".join("?" for _ in MONTHS)
        rows = cursor.execute(
            f"""SELECT TOP (100) basis_month,manifest_id,generation_no,status,
                       approval_status,checksum,product_count
                FROM snapshot.frequency_month_manifest
                WHERE company_id=? AND profile_fingerprint=? AND scope_fingerprint=?
                  AND schema_version=? AND algorithm_version=?
                  AND basis_month IN ({marks})
                  AND status IN ('draft','published')
                ORDER BY basis_month,generation_no DESC""",
            str(company_id), plan.profile_fingerprint, scope_key,
            MONTHLY_IO_SCHEMA_VERSION, MONTHLY_IO_ALGORITHM_VERSION, *MONTHS,
        ).fetchall()
        if len(rows) >= 100:
            raise ValueError("monthly Manifest metadata exceeds bounded inspection")
        approved = tuple(sorted({str(row[0]) for row in rows
                                 if str(row[3]) == "published" and str(row[4]) == "approved"}))
        pending = tuple(sorted({str(row[0]) for row in rows
                                if str(row[3]) == "draft" and str(row[4]) == "pending"}))
        return {
            "company_id": company_id, "target_hash": analytics_target_hash(company_id),
            "profile_fingerprint": plan.profile_fingerprint,
            "scope_fingerprint": scope_key, "saved_io_count": len(plan.io_gu_codes),
            "approved_months": approved, "pending_months": pending,
        }
    finally:
        conn.close()


def _launch(command: list[str], stdout_path: Path, stderr_path: Path,
            timeout_seconds: int) -> tuple[int, float]:
    started = time.perf_counter()
    with stdout_path.open("w", encoding="utf-8") as stdout, \
            stderr_path.open("w", encoding="utf-8") as stderr:
        try:
            finished = subprocess.run(command, cwd=ROOT, stdout=stdout, stderr=stderr,
                                      timeout=timeout_seconds, check=False)
            return finished.returncode, round((time.perf_counter() - started) * 1000, 3)
        except subprocess.TimeoutExpired:
            raise RuntimeError("child process exceeded the overall time limit") from None


def _last_json(path: Path) -> dict[str, Any]:
    last = ""
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                last = line
    if not last:
        raise ValueError("child stdout has no result")
    payload = json.loads(last)
    if not isinstance(payload, dict):
        raise ValueError("child stdout result is not an object")
    return payload


def _assert_exact(payload: dict[str, Any]) -> None:
    price = payload["price_exact"]
    outbound = payload["r120_exact"]
    if (price["price_mismatch_count"] != 0 or
            outbound["normal_monthly_mismatch_count"] != 0 or
            outbound["return_product_mismatch_count"] != 0):
        raise ValueError("source Exact mismatch")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, sort_keys=True, indent=2)
        output.write("\n")


def run_sequence(
    *, company_ids: tuple[int, ...], operator_id: str, approval_reference: str,
    output_root: Path, timeout_seconds: int = 120, process_timeout_seconds: int = 7200,
    now: datetime | None = None,
    registry_loader: Callable[..., list[dict[str, Any]]] = list_companies,
    metadata_loader: Callable[..., dict[str, Any]] = load_company_metadata,
    launcher: Callable[[list[str], Path, Path, int], tuple[int, float]] = _launch,
) -> dict[str, Any]:
    validate_scope(company_ids, now=now or datetime.now(KST))
    if not operator_id.strip() or not approval_reference.strip():
        raise ValueError("operator identity and execution approval reference are required")
    if not 1 <= timeout_seconds <= 120 or process_timeout_seconds < timeout_seconds:
        raise ValueError("invalid SQL or process timeout")
    output_root = output_root.resolve()
    if output_root.is_relative_to(ROOT):
        raise ValueError("customer-work logs and approval pins must stay outside Git repository")
    registry = registry_loader(include_inactive=True, top=2000)
    if len(registry) >= 2000:
        raise ValueError("company registry exceeds bounded inspection")
    by_id = {int(row["company_id"]): row for row in registry}
    if len(by_id) != len(registry) or any(cid not in by_id or by_id[cid].get("is_active") != 1
                                             for cid in company_ids):
        raise ValueError("requested company is missing, inactive or has unknown active status")
    run_dir = output_root / f"monthly-initial-{datetime.now(KST):%Y%m%d-%H%M%S}-{uuid4().hex[:8]}"
    run_dir.mkdir(parents=True, exist_ok=False)
    summary: dict[str, Any] = {
        "evaluation_month": EVALUATION_MONTH, "approval_reference": approval_reference,
        "operator_id": operator_id, "run_dir": str(run_dir),
        "registry_verified": True,
        "active_company_ids": sorted(cid for cid, row in by_id.items() if row.get("is_active") == 1),
        "selected_company_ids": list(company_ids), "companies": [], "status": "running",
    }
    summary_path = run_dir / "run_summary.json"
    for cid in company_ids:
        result: dict[str, Any] = {"company_id": cid, "stage": "preflight", "status": "running"}
        summary["companies"].append(result)
        try:
            print(json.dumps({"company_id": cid, "stage": "preflight"}), flush=True)
            validate_scope((cid,), now=datetime.now(KST))
            metadata = metadata_loader(cid, timeout=10)
            if metadata["company_id"] != cid:
                raise ValueError("preflight company identity mismatch")
            result["preflight"] = metadata
            if set(metadata["approved_months"]) == set(MONTHS):
                result.update(stage="complete", status="approved_manifests_skipped_not_revalidated")
                _write_json(run_dir / f"company_{cid}_skipped.json", result)
                print(json.dumps({"company_id": cid, "status": result["status"]}), flush=True)
                continue
            if cid == 13 and metadata["saved_io_count"] != 45:
                raise ValueError("company 13 Saved IO scope is not the stored 45 codes")
            result["stage"] = "build_draft"
            print(json.dumps({"company_id": cid, "stage": result["stage"]}), flush=True)
            build_stdout = run_dir / f"company_{cid}_build.stdout.log"
            build_stderr = run_dir / f"company_{cid}_build.stderr.log"
            code, elapsed = launcher(
                [sys.executable, "-u", str(ROOT / "tools" / "build_monthly_io_company.py"),
                 "--company-id", str(cid), "--evaluation-month", EVALUATION_MONTH,
                 "--apply", "--timeout-seconds", str(timeout_seconds),
                 "--created-by", operator_id],
                build_stdout, build_stderr, process_timeout_seconds,
            )
            result["build_elapsed_ms"] = elapsed
            if code != 0:
                raise RuntimeError("monthly Draft builder returned nonzero exit status")
            built = _last_json(build_stdout)
            if built.get("ok") is not True or built.get("company_id") != cid or \
                    built.get("evaluation_month") != EVALUATION_MONTH:
                raise ValueError("monthly Draft builder result identity or success mismatch")
            _assert_exact(built)
            monthly = built["monthly"]
            if set(monthly) != set(MONTHS):
                raise ValueError("monthly Draft month coverage mismatch")
            pins = []
            for month in MONTHS:
                row = monthly[month]
                if row["status"] not in {"draft_pending_manual_approval", "reused_source_checked"}:
                    raise ValueError("monthly Draft has unexpected status")
                if not row.get("saved") or row.get("fact_count", -1) < 0:
                    raise ValueError("monthly Draft has missing save or count")
                pins.append({"basis_month": month, "manifest_id": row["manifest_id"],
                             "generation_no": row["generation_no"], "checksum": row["checksum"]})
            result.update(product_count=built["product_count"], fact_count=built["fact_count"],
                          source_select_count=built["erp_select_count"],
                          monthly=monthly, stage="independent_exact")
            print(json.dumps({"company_id": cid, "stage": result["stage"]}), flush=True)
            candidate_path = run_dir / f"company_{cid}_candidate_unverified.json"
            _write_json(candidate_path, {"company_id": cid, "evaluation_month": EVALUATION_MONTH,
                                         "drafts": pins})
            inspect_stdout = run_dir / f"company_{cid}_inspect.stdout.log"
            inspect_stderr = run_dir / f"company_{cid}_inspect.stderr.log"
            code, elapsed = launcher(
                [sys.executable, "-u", str(ROOT / "tools" / "approve_monthly_io_company.py"),
                 "--company-id", str(cid), "--evaluation-month", EVALUATION_MONTH,
                 "--initial-batch-file", str(candidate_path), "--approved-by", operator_id,
                 "--approval-reason", "independent inspection only; approval not executed",
                 "--timeout-seconds", str(timeout_seconds)],
                inspect_stdout, inspect_stderr, process_timeout_seconds,
            )
            result["inspect_elapsed_ms"] = elapsed
            if code != 0:
                raise RuntimeError("independent Exact inspector returned nonzero exit status")
            inspected = _last_json(inspect_stdout)
            if inspected.get("company_id") != cid or inspected.get("evaluation_month") != EVALUATION_MONTH:
                raise ValueError("independent Exact result identity mismatch")
            _assert_exact(inspected)
            checked = inspected["months"]
            if (len(checked) != len(MONTHS) or
                    {(row["basis_month"], row["manifest_id"], row["generation_no"], row["checksum"])
                     for row in checked} !=
                    {(row["basis_month"], row["manifest_id"], row["generation_no"], row["checksum"])
                     for row in pins} or
                    any(row["status"] not in {"inspected_draft", "already_approved"}
                        for row in checked)):
                raise ValueError("independent Exact pins or statuses differ")
            approval_path = run_dir / f"company_{cid}_approval_batch.json"
            _write_json(approval_path, {"company_id": cid, "evaluation_month": EVALUATION_MONTH,
                                        "drafts": pins})
            result.update(stage="complete", status="inspected_pending_manual_approval",
                          approval_batch_file=str(approval_path), inspected_months=checked)
            _write_json(run_dir / f"company_{cid}_result.json", result)
            print(json.dumps({"company_id": cid, "status": result["status"],
                              "product_count": result["product_count"],
                              "fact_count": result["fact_count"]}), flush=True)
        except Exception as exc:
            result.update(status="failed", error_type=type(exc).__name__)
            summary["status"] = "stopped"
            summary["failed_company_id"] = cid
            summary["failed_stage"] = result["stage"]
            _write_json(run_dir / f"company_{cid}_failed.json", result)
            _write_json(summary_path, summary)
            print(json.dumps({"company_id": cid, "status": "failed",
                              "stage": result["stage"],
                              "error_type": type(exc).__name__}), file=sys.stderr, flush=True)
            return summary
    summary["status"] = (
        "completed_with_unverified_skips"
        if any(row["status"] == "approved_manifests_skipped_not_revalidated"
               for row in summary["companies"])
        else "inspected_no_approval"
    )
    _write_json(summary_path, summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="perform DB-backed Draft and Exact work")
    parser.add_argument("--company-ids", default="")
    parser.add_argument("--operator-id", default="")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--process-timeout-seconds", type=int, default=7200)
    args = parser.parse_args()
    if not args.execute:
        if args.company_ids or args.operator_id or args.approval_reference or args.output_root:
            parser.error("execution arguments require --execute")
        print(json.dumps({"mode": "offline_plan", "evaluation_month": EVALUATION_MONTH,
                          "months": MONTHS, "priority_ids": PRIORITY_IDS,
                          "protected_ids": PROTECTED_IDS, "company8_not_before": COMPANY8_START.isoformat(),
                          "database_verified": False}, ensure_ascii=False))
        return 0
    if not args.company_ids or not args.output_root:
        parser.error("--execute requires --company-ids and --output-root")
    try:
        result = run_sequence(
            company_ids=parse_ids(args.company_ids), operator_id=args.operator_id,
            approval_reference=args.approval_reference, output_root=args.output_root,
            timeout_seconds=args.timeout_seconds,
            process_timeout_seconds=args.process_timeout_seconds,
        )
    except Exception as exc:
        print(json.dumps({"status": "failed_before_start", "error_type": type(exc).__name__}))
        return 1
    print(json.dumps({"status": result["status"], "run_dir": result["run_dir"],
                      "companies": len(result["companies"]),
                      "failed_company_id": result.get("failed_company_id"),
                      "failed_stage": result.get("failed_stage")}, ensure_ascii=False))
    return 0 if result["status"] in {"inspected_no_approval",
                                      "completed_with_unverified_skips"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
