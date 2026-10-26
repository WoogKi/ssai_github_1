"""Read-only company-scoped monthly I/O work history from existing manifests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.ssai_analytics_target_resolver import connect_company_analytics_db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company-id", type=int, required=True)
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()
    conn = connect_company_analytics_db(args.company_id, "reader", timeout=30)
    try:
        cursor = conn.cursor()
        rows = cursor.execute(
            """SELECT TOP (?) basis_month,generation_no,status,approval_status,
                      source_row_count,event_count,fact_count,created_at,approved_at,
                      source_verified_at,created_by,approved_by,manifest_id
               FROM snapshot.frequency_month_manifest
               WHERE company_id=? AND schema_version=N'1.2'
                 AND algorithm_version=N'monthly_io_components_v2'
               ORDER BY created_at DESC,manifest_id DESC""",
            max(1, min(200, args.limit)), str(args.company_id),
        ).fetchall()
        for row in rows:
            print(json.dumps({
                "company_id": args.company_id, "month": str(row[0]), "generation": int(row[1]),
                "status": str(row[2]), "approval_status": str(row[3]),
                "source_rows": int(row[4]), "accepted_events": int(row[5]),
                "fact_rows": int(row[6]), "created_at_utc": str(row[7]),
                "approved_at_utc": str(row[8]) if row[8] else None,
                "last_source_verified_at_utc": str(row[9]) if row[9] else None,
                "created_by": str(row[10]), "approved_by": str(row[11]) if row[11] else None,
                "manifest_id": int(row[12]), "duration_ms": None,
            }, ensure_ascii=False))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
