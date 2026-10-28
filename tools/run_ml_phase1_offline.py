"""Run the phase-1 forecast on an approved offline JSON fixture only.

The input supplies explicit complete dates, approved code mappings, and an
offline official-calendar snapshot. It never opens an ERP or Analytics DB.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Callable

from app.ml.baselines import backtest_models, evaluate
from app.ml.phase1 import OfflineCalendar, build_daily, prepare_sources
from app.ml.run_state import RunState


def _date(raw: str) -> date:
    return date.fromisoformat(raw)


class RunCancelled(Exception):
    pass


def run_fixture(source: Path, runs_root: Path, *, run_id: str, progress_every: int = 500,
                progress_interval_s: float = 0.5,
                cancel_check: Callable[[], bool] | None = None) -> dict[str, object]:
    if progress_every <= 0 or progress_interval_s <= 0:
        raise ValueError("invalid progress cadence")
    source_bytes = source.read_bytes()
    payload = json.loads(source_bytes)
    if payload.get("schema_version") != "ml_phase1_fixture_v1":
        raise ValueError("unsupported fixture schema")
    manifest_path = source.parent / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (manifest.get("sha256_by_file", {}).get(source.name) != hashlib.sha256(source_bytes).hexdigest()
                or manifest.get("dataset_version") != payload.get("dataset_version")
                or manifest.get("company_id") != payload.get("company_id")
                or manifest.get("dataset_status") not in {"REVIEW_REQUIRED", "READY"}):
            raise ValueError("dataset manifest or checksum mismatch")
    company_id = int(payload["company_id"])
    dataset_version = str(payload["dataset_version"])
    run = RunState(runs_root, run_id=run_id, dataset_version=dataset_version)
    stage = "prepare"
    done_count = 0
    stage_total: int | None = None
    try:
        run.update(stage=stage, event="START", done=0)
        prepared = prepare_sources(
            company_id=company_id, outbound_rows=payload["outbound_rows"],
            document_rows=payload["document_rows"], vendor_rows=payload["vendor_rows"],
            product_rows=payload["product_rows"], standard_rows=payload["standard_rows"],
            code_rows=payload["code_rows"], saved_io_codes=payload["saved_io_codes"],
            channel_by_io=payload.get("channel_by_io"),
            valid_care_provider_codes=payload.get("valid_care_provider_codes", ()),
            supplier_type_by_label=payload.get("supplier_type_by_label"),
        )
        run.update(stage=stage, event="DONE", done=prepared.source_row_count, total=prepared.source_row_count)
        stage = "daily"
        run.update(stage=stage, event="START", done=0)
        daily = build_daily(
            prepared, company_id=company_id, date_from=_date(payload["date_from"]),
            date_to=_date(payload["date_to"]),
            complete_dates=(_date(day) for day in payload["complete_dates"]),
        )
        run.update(stage=stage, event="DONE", done=prepared.source_row_count, total=prepared.source_row_count)
        calendar_data = payload.get("calendar")
        calendar = (OfflineCalendar(
            _date(calendar_data["start_date"]), _date(calendar_data["end_date"]),
            frozenset(calendar_data["holiday_dates"]), _date(calendar_data["known_as_of"]),
            calendar_data["version"],
        ) if calendar_data is not None else None)
        stage = "forecast"
        done_count = 0
        as_of_dates = [_date(day) for day in payload["as_of_dates"]]
        methods = ("moving_average", "ewma")
        total = len(set(as_of_dates)) * len(prepared.product_codes) * len(methods)
        stage_total = total
        run.update(stage=stage, event="START", done=0, total=total)
        last_reported = 0
        last_time = time.monotonic()

        def report_progress(done: int) -> None:
            nonlocal last_reported, last_time, done_count
            done_count = done
            now = time.monotonic()
            if done < total and (done - last_reported >= progress_every or now - last_time >= progress_interval_s):
                run.update(stage=stage, event="PROGRESS", done=done, total=total)
                last_reported = done
                last_time = now
            if cancel_check is not None and cancel_check():
                raise RunCancelled()

        forecasts = backtest_models(
            daily, company_id=company_id, product_codes=prepared.product_codes,
            as_of_dates=as_of_dates,
            calendar=calendar, timestamp_policy_confirmed=payload.get("timestamp_policy_confirmed") is True,
            methods=methods, alpha=Decimal(str(payload.get("ewma_alpha", "0.2"))),
            on_complete=report_progress,
        )
        run.update(stage=stage, event="DONE", done=len(forecasts), total=len(forecasts))
        (run.path / "predictions.json").write_text(
            json.dumps([{key: value.isoformat() if isinstance(value, date) else str(value) if isinstance(value, Decimal) else value
                         for key, value in asdict(row).items()} for row in forecasts], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        report = {
            "run_id": run_id, "dataset_version": dataset_version,
            "company_id": company_id, "metric_policy_version": daily.metric_policy_version,
            "source_row_count": prepared.source_row_count,
            "source_quantity": str(prepared.source_quantity),
            "document_match_counts": prepared.document_match_counts,
            "models": {method: {key: str(value) if isinstance(value, Decimal) else value
                                 for key, value in asdict(evaluate(row for row in forecasts if row.method == method)).items()}
                       for method in methods},
            "not_operationally_approved": True,
        }
        (run.path / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        run.update(stage="complete", event="DONE", done=1, total=1)
        return report
    except (RunCancelled, KeyboardInterrupt):
        run.update(stage=stage, event="CANCEL", done=done_count, total=stage_total)
        raise
    except Exception as exc:
        run.update(stage=stage, event="ERROR", done=done_count, total=stage_total,
                   error_code=type(exc).__name__.upper())
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    result = run_fixture(args.input, args.runs_root, run_id=args.run_id)
    print(json.dumps({"run_id": result["run_id"], "source_row_count": result["source_row_count"],
                      "run_path": str(args.runs_root / args.run_id)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
