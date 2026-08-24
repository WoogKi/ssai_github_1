from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot import (  # noqa: E402
    ALGORITHM_VERSION,
    SCHEMA_VERSION,
    SNAPSHOT_TYPE,
    scope_fingerprint,
)
from app.services.sql_server_snapshot_repository import SqlServerSnapshotRepository  # noqa: E402
from app.services.ssai_analytics_target_resolver import connect_company_analytics_db  # noqa: E402
from app.services.ssai_snapshot_repository import SnapshotKey  # noqa: E402


_GRADE_KEYS = ("A", "B", "C", "D", "E", "X")
_PARTITION_KEYS = (
    "normal_positive_accepted_row_count",
    "normal_positive_duplicate_row_count",
    "normal_positive_conflicting_row_count",
    "normal_positive_missing_key_row_count",
    "normal_positive_nonintegral_row_count",
    "normal_nonpositive_row_count",
    "return_positive_row_count",
    "return_nonpositive_row_count",
    "other_tcode_row_count",
)


def _parse_grade_counts(value: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in str(value or "").split(","):
        key, separator, raw = item.strip().partition("=")
        if not separator or key not in _GRADE_KEYS:
            raise ValueError("expected grade counts must be A=..,B=..,C=..,D=..,E=..,X=..")
        result[key] = int(raw)
    if tuple(sorted(result)) != tuple(sorted(_GRADE_KEYS)) or any(value < 0 for value in result.values()):
        raise ValueError("expected grade counts must contain every nonnegative A/B/C/D/E/X count")
    return result


def _key(args: argparse.Namespace) -> SnapshotKey:
    scope_codes = tuple(sorted({str(code).strip() for code in args.stock_code if str(code).strip()}))
    return SnapshotKey(
        company_id=str(args.company_id),
        snapshot_type=SNAPSHOT_TYPE,
        evaluation_month=str(args.evaluation_month),
        scope_fingerprint=scope_fingerprint(scope_codes),
        schema_version=SCHEMA_VERSION,
        algorithm_version=ALGORITHM_VERSION,
    )


def _verify_expected(payload: Mapping[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    summary = dict(payload.get("summary") or {})
    diagnostics = dict(payload.get("source_diagnostics") or {})
    grade_counts = dict(summary.get("grade_counts") or {})
    actual_grades = {key: int(grade_counts.get(key) or 0) for key in _GRADE_KEYS}
    partition_total = sum(int(diagnostics.get(key) or 0) for key in _PARTITION_KEYS)
    checks = {
        "product_count": int(summary.get("product_count") or 0) == args.expected_product_count,
        "normal_event_count": int(summary.get("normal_event_count") or 0) == args.expected_normal_event_count,
        "grade_counts": actual_grades == args.expected_grade_counts,
        "grade_count_total": sum(actual_grades.values()) == int(summary.get("product_count") or 0),
        "source_partition_total": partition_total == args.expected_source_row_count,
        "source_row_count": int(diagnostics.get("source_row_count") or 0) == args.expected_source_row_count,
        "accepted_normal": int(diagnostics.get("normal_positive_accepted_row_count") or 0)
        == int(summary.get("normal_event_count") or 0),
    }


def _inspection_authority(inspection: Any) -> Mapping[str, Any] | None:
    if isinstance(getattr(inspection, "payload", None), Mapping):
        return inspection.payload
    native = getattr(inspection, "relational_snapshot", None)
    if native is None:
        return None
    grade_counts = {key: 0 for key in _GRADE_KEYS}
    for row in native.frequency_products:
        grade = str(row.get("frequency_grade") or "")
        if grade in grade_counts:
            grade_counts[grade] += 1
    return {
        "summary": {
            "product_count": native.item_count,
            "normal_event_count": sum(int(row.get("occurrence_count") or 0) for row in native.monthly_activity),
            "grade_counts": grade_counts,
        },
        "source_diagnostics": native.source_diagnostics,
    }
    if not all(checks.values()):
        failed = ", ".join(key for key, value in checks.items() if not value)
        raise ValueError(f"approval expectation mismatch: {failed}")
    return {
        "product_count": summary.get("product_count"),
        "normal_event_count": summary.get("normal_event_count"),
        "grade_counts": actual_grades,
        "source_diagnostics": diagnostics,
        "checks": checks,
    }


def _output(inspection: Any, verification: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "integrity_status": inspection.status,
        "manifest_status": inspection.manifest_status,
        "approval_status": inspection.approval_status,
        "manifest_id": inspection.manifest_id,
        "generation_no": inspection.generation_no,
        "checksum": inspection.checksum,
        "storage_checksum_verified": bool(inspection.payload),
        "contract_checksum_verified": bool(_inspection_authority(inspection)),
        "payload_size": inspection.payload_size,
        "representation": getattr(inspection, "representation", "legacy_json_v1"),
        "verification": verification or {},
        "reason": inspection.reason,
    }


def _stage(
    timings: dict[str, int],
    name: str,
    *,
    generation_no: int,
    checksum: str,
    action: Any,
) -> Any:
    print(
        f"[snapshot.approval] stage={name} phase=start generation_no={generation_no} checksum={checksum[:12]}",
        flush=True,
    )
    started = time.perf_counter()
    try:
        result = action()
    except Exception:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        timings[name] = elapsed_ms
        print(
            f"[snapshot.approval] stage={name} phase=error generation_no={generation_no} "
            f"checksum={checksum[:12]} elapsed_ms={elapsed_ms}",
            flush=True,
        )
        raise
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    timings[name] = elapsed_ms
    print(
        f"[snapshot.approval] stage={name} phase=complete generation_no={generation_no} "
        f"checksum={checksum[:12]} elapsed_ms={elapsed_ms}",
        flush=True,
    )
    return result


def run_approval_workflow(args: argparse.Namespace, repository: Any) -> dict[str, Any]:
    """Run the inspect/approve contract once; every external stage is timed and has no retry."""
    key = _key(args)
    expected_checksum = str(args.expected_checksum).strip().lower()
    timings: dict[str, int] = {}
    inspection = _stage(
        timings,
        "pre_approval_inspect_generation",
        generation_no=args.generation,
        checksum=expected_checksum,
        action=lambda: repository.inspect_generation(key, args.generation),
    )
    authority = _inspection_authority(inspection)
    if inspection.status == "corrupt" or authority is None:
        raise ValueError(f"snapshot inspection failed: {inspection.status}: {inspection.reason}")
    if inspection.checksum.lower() != expected_checksum:
        raise ValueError("expected checksum does not match inspected generation")
    verification = _stage(
        timings,
        "pre_approval_verification",
        generation_no=args.generation,
        checksum=inspection.checksum,
        action=lambda: _verify_expected(authority, args),
    )
    output = _output(inspection, verification)
    preserved = None
    if args.preserve_generation:
        preserved = _stage(
            timings,
            "preserve_generation_inspect",
            generation_no=args.preserve_generation,
            checksum=expected_checksum,
            action=lambda: repository.inspect_generation(key, args.preserve_generation),
        )
        output["preserved_generation"] = {
            "generation_no": preserved.generation_no,
            "integrity_status": preserved.status,
            "manifest_status": preserved.manifest_status,
            "approval_status": preserved.approval_status,
            "checksum": preserved.checksum,
        }
    if not args.approve:
        output.update({"mode": "inspect", "approval": "not executed", "stage_elapsed_ms": timings})
        return output
    if inspection.status != "unapproved" or inspection.manifest_status != "draft" or inspection.approval_status != "pending":
        raise ValueError("only a draft/pending generation can be approved")
    if preserved is not None and (
        preserved.status != "unapproved"
        or preserved.manifest_status != "draft"
        or preserved.approval_status != "pending"
    ):
        raise ValueError("preserved generation must remain draft/pending before approval")
    _stage(
        timings,
        "approve_checked",
        generation_no=args.generation,
        checksum=expected_checksum,
        action=lambda: repository.approve_checked(
            key,
            args.generation,
            expected_checksum=args.expected_checksum,
            approved_by=args.approved_by,
            approval_reason=args.approval_reason,
        ),
    )
    post = _stage(
        timings,
        "post_approval_inspect_generation",
        generation_no=args.generation,
        checksum=expected_checksum,
        action=lambda: repository.inspect_generation(key, args.generation),
    )
    if (
        post.status != "ready"
        or post.manifest_status != "published"
        or post.approval_status != "approved"
        or post.generation_no != args.generation
        or post.checksum.lower() != expected_checksum
        or _inspection_authority(post) is None
    ):
        raise ValueError("post-approval inspection did not confirm the published generation")
    post_verification = _stage(
        timings,
        "post_approval_verification",
        generation_no=args.generation,
        checksum=post.checksum,
        action=lambda: _verify_expected(_inspection_authority(post) or {}, args),
    )
    preserved_after = None
    if args.preserve_generation:
        preserved_after = _stage(
            timings,
            "preserved_after_inspect_generation",
            generation_no=args.preserve_generation,
            checksum=expected_checksum,
            action=lambda: repository.inspect_generation(key, args.preserve_generation),
        )
        if (
            preserved_after.status != "unapproved"
            or preserved_after.manifest_status != "draft"
            or preserved_after.approval_status != "pending"
        ):
            raise ValueError("preserved generation changed during approval")
    final_output = _output(post, post_verification)
    if preserved_after is not None:
        final_output["preserved_generation"] = {
            "generation_no": preserved_after.generation_no,
            "integrity_status": preserved_after.status,
            "manifest_status": preserved_after.manifest_status,
            "approval_status": preserved_after.approval_status,
            "checksum": preserved_after.checksum,
        }
    final_output.update(
        {
            "mode": "approve",
            "approval": "ready",
            "approved_generation": args.generation,
            "stage_elapsed_ms": timings,
        }
    )
    return final_output


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect and explicitly approve one Dashboard frequency snapshot generation")
    parser.add_argument("--company-id", required=True, type=int)
    parser.add_argument("--evaluation-month", required=True)
    parser.add_argument("--stock-code", action="append", required=True)
    parser.add_argument("--generation", required=True, type=int)
    parser.add_argument("--preserve-generation", type=int, default=0)
    parser.add_argument("--expected-checksum", required=True)
    parser.add_argument("--expected-product-count", required=True, type=int)
    parser.add_argument("--expected-normal-event-count", required=True, type=int)
    parser.add_argument("--expected-source-row-count", required=True, type=int)
    parser.add_argument("--expected-grade-counts", required=True)
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--approved-by", default="")
    parser.add_argument("--approval-reason", default="")
    args = parser.parse_args()
    try:
        if args.company_id <= 0 or args.generation <= 0 or args.preserve_generation < 0:
            raise ValueError("company_id/generation must be positive and preserve_generation cannot be negative")
        args.expected_grade_counts = _parse_grade_counts(args.expected_grade_counts)
        if args.approve and (not str(args.approved_by).strip() or not str(args.approval_reason).strip()):
            raise ValueError("--approved-by and --approval-reason are required with --approve")
        output = run_approval_workflow(
            args,
            SqlServerSnapshotRepository(
                reader_connection_factory=lambda: connect_company_analytics_db(args.company_id, "reader"),
                writer_connection_factory=lambda: connect_company_analytics_db(args.company_id, "writer"),
            ),
        )
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
