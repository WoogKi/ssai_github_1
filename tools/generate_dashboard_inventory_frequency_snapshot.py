from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot_service import (  # noqa: E402
    build_frequency_snapshot_plan,
    generate_frequency_snapshot_draft,
)


def _plan_json(plan: Any, *, apply: bool, timeout_seconds: int, force: bool) -> dict[str, Any]:
    return {
        "mode": "apply" if apply else "dry-run",
        "company_id": plan.company_id,
        "evaluation_month": plan.evaluation_month,
        "basis_months": list(plan.basis_months),
        "basis_from": plan.basis_from,
        "basis_to": plan.basis_to,
        "stock_scope": {
            "mode": "selected" if plan.stock_codes else "all",
            "count": len(plan.stock_codes),
            "stock_codes": list(plan.stock_codes),
        },
        "erp_read_plan": {
            "sql_call_count": plan.erp_sql_call_count,
            "queries": ["Rddbc040 product universe", "Rddbc120 monthly outbound aggregate"],
            "timeout_seconds_each": timeout_seconds,
            "retry_count": 0,
        },
        "analytics_write_plan": plan.analytics_write_plan,
        "force_new_generation": bool(force),
        "manual_approval": "required; this command never approves or publishes",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate an unapproved Dashboard outbound-frequency snapshot draft"
    )
    parser.add_argument("--company-id", required=True, type=int)
    parser.add_argument("--evaluation-month", required=True)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--stock-code", action="append", default=[])
    scope.add_argument("--all-stock-locations", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--created-by", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    plan = build_frequency_snapshot_plan(
        company_id=args.company_id,
        evaluation_month=args.evaluation_month,
        stock_codes=[] if args.all_stock_locations else args.stock_code,
    )
    output = _plan_json(
        plan,
        apply=bool(args.apply),
        timeout_seconds=max(1, int(args.timeout_seconds)),
        force=bool(args.force),
    )
    if not args.apply:
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    actor = str(args.created_by or getpass.getuser() or "").strip()
    try:
        result = generate_frequency_snapshot_draft(
            plan=plan,
            created_by=actor,
            timeout_seconds=max(1, int(args.timeout_seconds)),
            force=bool(args.force),
            progress_reporter=lambda message: print(f"[진행] {message}"),
        )
        payload = result["payload"]
        draft = result["draft"]
        summary = dict(payload.get("summary") or {})
        output.update(
            {
                "ok": True,
                "product_count": summary.get("product_count"),
                "normal_event_count": summary.get("normal_event_count"),
                "ignored_product_event_count": summary.get("ignored_product_event_count"),
                "grade_counts": summary.get("grade_counts"),
                "excluded_counts": payload.get("source_diagnostics"),
                "checksum": payload.get("checksum"),
                "generation_no": draft.generation_no,
                "manifest_id": draft.manifest_id,
                "draft_status": draft.status,
                "approval_status": draft.approval_status,
                "read_status": result["read_status"],
                "no_op": draft.no_op,
            }
        )
    except Exception as exc:
        output.update({"ok": False, "error_type": type(exc).__name__, "error": str(exc)})
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
