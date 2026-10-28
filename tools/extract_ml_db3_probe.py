"""Explicit company-3-only, bounded, read-only ML probe.

Raw sample files must be written outside the Git checkout. No retry or write SQL.
"""

from __future__ import annotations

import argparse
import json
import traceback
from pathlib import Path

import pyodbc

from app.ml.dataset_contract import write_probe_dataset
from app.ml.db3_probe import run_db3_probe
from app.services.ssai_auth_service import build_company_conn_str, get_company_db_config
from tools.run_ml_phase1_offline import run_fixture


REPO_ROOT = Path(__file__).resolve().parents[1]


def validate_destination(path: Path) -> Path:
    result = path.resolve()
    if result == REPO_ROOT or result.is_relative_to(REPO_ROOT):
        raise ValueError("customer data must not be written inside the Git checkout")
    return result


def execute_probe(*, company_id: int, output_root: Path, approval_reference: str) -> dict[str, object]:
    if company_id != 3 or not approval_reference.strip():
        raise ValueError("company 3 and explicit approval reference are required")
    root = validate_destination(output_root)
    cfg = get_company_db_config(3)
    if cfg.company_id != 3 or not cfg.db_name:
        raise ValueError("company 3 connection target is not verified")
    query_count = 0

    with pyodbc.connect(build_company_conn_str(cfg), timeout=10, autocommit=True) as connection:
        connection.timeout = 120
        def query(sql: str, params: tuple[object, ...], cap: int) -> list[dict[str, object]]:
            nonlocal query_count
            cursor = connection.cursor()
            cursor.execute(sql, *params)
            names = [column[0] for column in cursor.description]
            rows = [dict(zip(names, row)) for row in cursor.fetchmany(cap + 1)]
            query_count += 1
            if len(rows) > cap:
                raise ValueError("bounded SELECT result exceeded cap")
            return rows

        result = run_db3_probe(query)
    dataset_dir = root / result["fixture"]["dataset_version"]
    manifest = write_probe_dataset(
        dataset_dir, fixture=result["fixture"], daily_r120=result["daily_r120"],
        daily_r130=result["daily_r130"], approval_reference=approval_reference,
        query_durations_ms=result["query_durations_ms"], probe_quality=result["quality"],
    )
    offline = run_fixture(dataset_dir / "fixture.json", root / "runs", run_id=result["fixture"]["dataset_version"])
    if offline["source_row_count"] != manifest["table_counts"]["r120_sample_rows"]:
        raise ValueError("offline sample count differs from extracted sample")
    return {
        "query_count": query_count, "query_durations_ms": result["query_durations_ms"],
        "dataset_status": manifest["dataset_status"],
        "r120_daily_groups": manifest["table_counts"]["r120_daily_groups"],
        "r130_daily_groups": manifest["table_counts"]["r130_daily_groups"],
        "r120_sample_rows": manifest["table_counts"]["r120_sample_rows"],
        "sample_products": result["quality"]["products_in_sample"],
        "negative_5xx_rows": result["quality"]["negative_5xx_rows"],
        "document_match_counts": result["quality"]["document_match_counts"],
        "offline_models": offline["models"],
        "source_max_observed_date": manifest["source_max_observed_date"],
        "last_complete_date": manifest["last_complete_date"],
        "dataset_path": str(dataset_dir),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-read-only", action="store_true")
    parser.add_argument("--company-id", type=int, required=True)
    parser.add_argument("--approval-reference", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    if not args.live_read_only:
        parser.error("--live-read-only is required before any DB access")
    try:
        result = execute_probe(company_id=args.company_id, output_root=args.output_root,
                               approval_reference=args.approval_reference)
    except Exception as exc:
        frames = [(Path(frame.filename).name, frame.lineno) for frame in traceback.extract_tb(exc.__traceback__)
                  if Path(frame.filename).name in {"extract_ml_db3_probe.py", "db3_probe.py", "dataset_contract.py"}]
        parser.exit(1, f"DB3 probe stopped: {type(exc).__name__} at {frames[-1] if frames else 'dependency'}\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
