"""Explicit company-3-only batch extractor and research posthoc runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import tracemalloc
from pathlib import Path
from time import perf_counter

import pyodbc

from app.ml.integration import ml_storage_root
from app.ml.phase4_extract import extract_research_fixture, save_research_dataset
from app.services.ssai_analysis_profile_service import load_dashboard_profile_checked
from app.services.ssai_auth_service import build_company_conn_str, get_company_db_config
from tools.run_ml_phase1_offline import run_fixture


def run(calendar_dataset: Path) -> dict:
    root = ml_storage_root()
    manifest = json.loads((calendar_dataset / "manifest.json").read_text(encoding="utf-8"))
    fixture_bytes = (calendar_dataset / "fixture.json").read_bytes()
    if (manifest.get("company_id") != 3 or manifest.get("calendar_loaded_asof") is None
            or manifest.get("sha256_by_file", {}).get("fixture.json") != hashlib.sha256(fixture_bytes).hexdigest()):
        raise ValueError("saved DB3 Calendar dataset is invalid")
    calendar = json.loads(fixture_bytes)["calendar"]
    calendar["status"] = "ready"
    profile = load_dashboard_profile_checked(company_id=3)
    if profile.status != "ready" or not profile.profile:
        raise ValueError("company 3 Dashboard profile unavailable")
    profile_fingerprint = hashlib.sha256(json.dumps(
        profile.profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    cfg = get_company_db_config(3)
    if cfg.company_id != 3:
        raise ValueError("ERP connection company mismatch")
    started = perf_counter()
    tracemalloc.start()
    with pyodbc.connect(build_company_conn_str(cfg), timeout=10, autocommit=True) as conn:
        conn.timeout = 120

        def query(sql: str, params: tuple[object, ...], cap: int) -> list[dict]:
            cursor = conn.cursor()
            cursor.execute(sql, *params)
            columns = [column[0] for column in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchmany(cap + 1)]
            if len(rows) > cap:
                raise ValueError("bounded product batch exceeded cap")
            return rows

        fixture, metrics = extract_research_fixture(
            query, company_id=3, saved_io_codes=list(profile.profile.get("io_gu_list") or []),
            calendar=calendar, profile_fingerprint=profile_fingerprint,
        )
    extraction_ms = int((perf_counter() - started) * 1000)
    target = save_research_dataset(root, fixture, metrics)
    summary = run_fixture(target / "fixture.json", root / "runs", run_id=target.name)
    elapsed_ms = int((perf_counter() - started) * 1000)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    forecasts = json.loads((root / "runs" / target.name / "predictions.json").read_text(encoding="utf-8"))
    product_codes = sorted({row["product_code"] for row in forecasts})
    aliases = {code: f"P{index + 1}" for index, code in enumerate(product_codes)}
    observations = [
        {"as_of": row["as_of"], "product": aliases[row["product_code"]], "method": row["method"],
         "status": row["status"], "actual": row["actual_quantity"], "predicted": row["predicted_quantity"],
         "evaluation_grade": row["evaluation_grade"]}
        for row in forecasts
    ]
    report = {
        "dataset_version": target.name, "company_id": 3, "evaluation_grade": "research_posthoc",
        "source_rows": metrics["source_rows"], "source_quantity": metrics["source_quantity"],
        "source_bonus_quantity": metrics["source_bonus_quantity"], "selected_products": metrics["selected_products"],
        "category_counts": metrics["category_counts"], "negative_normal_rows": metrics["negative_normal_rows"],
        "document_match_counts": metrics["document_match_counts"],
        "query_count": metrics["query_count"], "query_durations_ms": metrics["query_durations_ms"],
        "extraction_ms": extraction_ms, "total_ms": elapsed_ms, "peak_python_bytes": peak,
        "model_metrics": summary["models"], "observations": observations,
    }
    out = root / "diagnostics" / (target.name + "-summary.json")
    if out.exists():
        raise FileExistsError("research summary already exists")
    out.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return {key: report[key] for key in report if key not in {"observations", "query_durations_ms"}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-read-only", action="store_true")
    parser.add_argument("--company-id", type=int, required=True)
    parser.add_argument("--calendar-dataset", type=Path, required=True)
    args = parser.parse_args()
    if not args.live_read_only or args.company_id != 3:
        parser.error("explicit company 3 read-only scope required")
    try:
        print(json.dumps(run(args.calendar_dataset.resolve()), ensure_ascii=False))
    except Exception as exc:
        parser.exit(1, f"ML phase4 DB3 extraction stopped: {type(exc).__name__}\n")


if __name__ == "__main__":
    main()
