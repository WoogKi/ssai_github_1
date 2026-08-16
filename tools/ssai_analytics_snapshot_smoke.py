from __future__ import annotations

import argparse
import copy
import getpass
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot import (  # noqa: E402
    build_frequency_snapshot_payload,
    calculate_payload_checksum,
    snapshot_key_from_payload,
    validate_frequency_snapshot_payload,
)
from app.services.sql_server_snapshot_repository import SqlServerSnapshotRepository  # noqa: E402


def _fixture_payload(*, company_id: str, evaluation_month: str) -> dict[str, object]:
    year = int(evaluation_month[:4])
    month = int(evaluation_month[4:])
    ordinal = year * 12 + month - 1 - 3
    basis_year, basis_zero_month = divmod(ordinal, 12)
    outbound_date = f"{basis_year:04d}{basis_zero_month + 1:02d}01"
    return build_frequency_snapshot_payload(
        company_id=company_id,
        evaluation_month=evaluation_month,
        product_codes=["SMOKE-P1", "SMOKE-P2"],
        stock_codes=["SMOKE-STOCK"],
        source_watermark=None,
        source_watermark_status="unverified",
        rows=[
            {
                "outbound_date": outbound_date,
                "vendor_code": "SMOKE-VENDOR",
                "outbound_seq": 1,
                "io_gu_gcode": "0012",
                "io_tcode": "501",
                "product_code": "SMOKE-P1",
                "stock_code": "SMOKE-STOCK",
                "quantity": 1,
                "oquantity": 0,
            }
        ],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="SSAI_ANALYTICS repository lifecycle smoke")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--evaluation-month", required=True)
    parser.add_argument("--company-id", default="__SSAI_SNAPSHOT_SMOKE__")
    parser.add_argument("--actor", default="")
    args = parser.parse_args()
    actor = str(args.actor or getpass.getuser() or "").strip()
    plan = {
        "apply": bool(args.apply),
        "company_id": args.company_id,
        "evaluation_month": args.evaluation_month,
        "operations": ["draft generation A", "approve/read", "draft generation B", "approve/read", "invalidate"],
        "expected_writes": {"manifest_insert": 2, "payload_insert": 2, "approval_update": 2, "invalidate_update": 1},
        "cleanup": "published payloads are immutable; final generation remains invalidated for audit",
    }
    if not args.apply:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    try:
        payload1 = _fixture_payload(
            company_id=str(args.company_id), evaluation_month=str(args.evaluation_month)
        )
        key = snapshot_key_from_payload(payload1)
        repository = SqlServerSnapshotRepository(
            payload_validator=lambda payload, expected_key: validate_frequency_snapshot_payload(
                payload, expected_key=expected_key
            )
        )
        draft1 = repository.publish(key, payload1, created_by=actor)
        blocked_status = repository.read(key).status
        approved1 = repository.approve(
            key,
            int(draft1.generation_no or 0),
            approved_by=actor,
            approval_reason="manual smoke approval: source range and fingerprint reviewed",
        )
        ready1 = repository.read(key)
        generation1 = int(draft1.generation_no or 0)

        payload2 = copy.deepcopy(payload1)
        payload2["source_watermark"] = "manual-smoke-generation-2"
        payload2["checksum"] = calculate_payload_checksum(payload2)
        draft2 = repository.replace(key, payload2, created_by=actor)
        approved2 = repository.approve(
            key,
            int(draft2.generation_no or 0),
            approved_by=actor,
            approval_reason="manual smoke replacement approval",
        )
        ready2 = repository.read(key)
        generation2 = int(draft2.generation_no or 0)
        repository.invalidate(key, reason="manual smoke completed", invalidated_by=actor)
        final_status = repository.read(key).status
        if not (
            blocked_status == "unapproved"
            and approved1.status == "ready"
            and ready1.generation_no == generation1
            and approved2.status == "ready"
            and generation2 == generation1 + 1
            and ready2.generation_no == generation2
            and final_status == "stale"
        ):
            raise AssertionError("repository lifecycle smoke contract mismatch")
    except Exception as exc:
        plan.update({"ok": False, "error_type": type(exc).__name__})
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 1
    plan.update(
        {
            "ok": True,
            "draft_read_status": blocked_status,
            "first_approved_generation": ready1.generation_no,
            "replacement_approved_generation": ready2.generation_no,
            "final_status": final_status,
        }
    )
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
