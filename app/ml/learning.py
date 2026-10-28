"""Research-only 30-day Random Forest training over verified offline datasets."""

from __future__ import annotations

import hashlib
import json
import os
import time
import tracemalloc
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable

from app.ml.baselines import Forecast, evaluate, forecast_from_example
from app.ml.phase1 import OfflineCalendar, build_daily, build_example, prepare_sources
from app.ml.run_state import RunState


class TrainingCancelled(Exception):
    pass


@contextmanager
def single_worker(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    lock = root / "training.lock"
    handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(handle, f"pid={os.getpid()}\n".encode("ascii"))
        yield
    finally:
        os.close(handle)
        lock.unlink()


def _load_fixture(path: Path) -> tuple[dict, dict]:
    raw = path.read_bytes()
    payload = json.loads(raw)
    manifest_path = path.parent / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("a checked dataset manifest is required")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (payload.get("schema_version") != "ml_phase1_fixture_v1"
            or payload.get("company_id") != manifest.get("company_id")
            or payload.get("dataset_version") != manifest.get("dataset_version")
            or manifest.get("sha256_by_file", {}).get(path.name) != hashlib.sha256(raw).hexdigest()
            or manifest.get("dataset_status") != "RESEARCH_POSTHOC"
            or manifest.get("evaluation_grade") != "research_posthoc"
            or payload.get("calendar_asof_required") is not False):
        raise ValueError("research dataset contract or checksum mismatch")
    return payload, manifest


def _features(example, *, rows, standard_codes: set[str], product: dict,
              calendar: OfflineCalendar, master_asof_verified: bool) -> dict[str, float | str]:
    as_of = example.as_of
    cutoff = datetime.combine(as_of, datetime.max.time())
    history = [row.source for row in rows if as_of - timedelta(days=89) <= row.source.out_date <= as_of]
    if any(row.add_time is None or row.mod_time is None or row.add_time > cutoff or row.mod_time > cutoff
           for row in history):
        raise ValueError("source history cannot be reproduced as of prediction date")
    returns = [row for row in history if row.io_code.startswith("6")]
    month_end = (as_of.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    days = [as_of + timedelta(days=offset) for offset in range(-3, 4)]
    holiday_days = {date.fromisoformat(day) for day in calendar.holiday_dates}
    nonbusiness = [day.weekday() >= 5 or day in holiday_days for day in days]
    run = longest = 0
    for flag in nonbusiness:
        run = run + 1 if flag else 0
        longest = max(longest, run)
    return {
        "company": str(example.company_id), "product": example.product_code,
        "product_group": str(product.get("Rd04_Physic_Cd_Gcode") or "unknown") if master_asof_verified else "unknown",
        "product_kind": str(product.get("Rd04_Physic_Gu") or "unknown") if master_asof_verified else "unknown",
        "standard_status": ("present" if example.product_code in standard_codes else "absent")
        if master_asof_verified else "unknown",
        "quantity_7d": float(example.qty_7d), "quantity_30d": float(example.qty_30d),
        "quantity_90d": float(example.qty_90d), "active_days_30d": float(example.active_days_30d),
        "return_quantity_90d": float(sum((row.quantity for row in returns), Decimal(0))),
        "return_rows_90d": float(len(returns)),
        "stock_locations_90d": float(len({row.stock_code for row in history})),
        "stock_apply_codes_90d": float(len({row.stock_apply_code for row in history})),
        "cost_apply_codes_90d": float(len({row.cost_apply_code for row in history})),
        "day_of_month": float(as_of.day), "days_to_month_end": float((month_end - as_of).days),
        "first_business_day": float(example.first_business_day_of_month == as_of),
        "business_days_next_30d": float(example.business_days_next_30d),
        "holiday_nearby": float(any(day in holiday_days for day in days)),
        "longest_nonbusiness_nearby": float(longest),
    }


def _metric_dict(rows: list[Forecast]) -> dict:
    return {key: str(value) if isinstance(value, Decimal) else value
            for key, value in asdict(evaluate(rows)).items()}


def train_research(
    source: Path, storage_root: Path, *, run_id: str,
    cancel_check: Callable[[], bool] | None = None, estimators: int = 40,
) -> dict:
    """Fit once on earlier non-overlapping labels; reserve latest date for final evaluation."""
    if estimators < 10 or estimators > 200 or estimators % 10:
        raise ValueError("estimators must be 10..200 in groups of 10")
    from joblib import dump, load
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.feature_extraction import DictVectorizer
    from threadpoolctl import threadpool_limits

    payload, manifest = _load_fixture(source)
    dates = sorted({date.fromisoformat(value) for value in payload["as_of_dates"]})
    if len(dates) < 2:
        raise ValueError("at least two distinct as-of dates are required")
    evaluation_date = dates[-1]
    training_dates = [day for day in dates[:-1] if day + timedelta(days=30) < evaluation_date]
    if not training_dates:
        raise ValueError("no non-overlapping training label before evaluation date")
    company_id = int(payload["company_id"])
    run_root = storage_root / "runs"
    model_root = storage_root / "models" / run_id
    with single_worker(storage_root / "runs"):
        run = RunState(run_root, run_id=run_id, dataset_version=payload["dataset_version"])
        stage = "prepare"
        done = 0
        total = None
        started = time.monotonic()
        cpu_started = time.process_time()
        tracemalloc.start()
        try:
            run.update(stage=stage, event="START", done=0)
            prepared = prepare_sources(
                company_id=company_id, outbound_rows=payload["outbound_rows"],
                document_rows=payload["document_rows"], vendor_rows=payload["vendor_rows"],
                product_rows=payload["product_rows"], standard_rows=payload["standard_rows"],
                code_rows=payload["code_rows"], saved_io_codes=payload["saved_io_codes"],
            )
            daily = build_daily(
                prepared, company_id=company_id,
                date_from=date.fromisoformat(payload["date_from"]),
                date_to=date.fromisoformat(payload["date_to"]),
                complete_dates=(date.fromisoformat(value) for value in payload["complete_dates"]),
            )
            calendar_data = payload["calendar"]
            calendar = OfflineCalendar(
                date.fromisoformat(calendar_data["start_date"]), date.fromisoformat(calendar_data["end_date"]),
                frozenset(calendar_data["holiday_dates"]), date.fromisoformat(calendar_data["known_as_of"]),
                calendar_data["version"],
            )
            products = {str(row["Rd04_Physic_Cd"]).strip(): row for row in payload["product_rows"]}
            standard = {str(row["Rd046_Physic_Cd"]).strip() for row in payload["standard_rows"]}
            by_product = {}
            for row in prepared.rows:
                by_product.setdefault(row.source.product_code, []).append(row)
            samples = []
            withheld = []
            all_dates = training_dates + [evaluation_date]
            total = len(all_dates) * len(prepared.product_codes)
            for as_of in all_dates:
                for code in prepared.product_codes:
                    if cancel_check is not None and cancel_check():
                        raise TrainingCancelled()
                    example = build_example(
                        daily, company_id=company_id, product_code=code, as_of=as_of,
                        calendar=calendar, timestamp_policy_confirmed=payload.get("timestamp_policy_confirmed") is True,
                        calendar_asof_required=False,
                    )
                    status = example.feature_status if example.feature_status != "ready" else example.label_status
                    if status == "complete" and example.calendar_status != "ready":
                        status = "calendar_unavailable"
                    if status == "complete":
                        try:
                            feature = _features(example, rows=by_product.get(code, ()),
                                                standard_codes=standard, product=products.get(code, {}), calendar=calendar,
                                                master_asof_verified=(manifest.get("master_features_asof_verified") is True
                                                                      and bool(manifest.get("master_features_asof_evidence"))))
                        except ValueError:
                            status = "asof_history_unavailable"
                    if status == "complete":
                        samples.append((as_of, code, example, feature))
                    else:
                        withheld.append({"as_of": as_of.isoformat(), "product_code": code, "reason": status})
                    done += 1
                run.update(stage=stage, event="PROGRESS", done=done, total=total)
            train = [row for row in samples if row[0] < evaluation_date]
            test = [row for row in samples if row[0] == evaluation_date]
            if len(train) < 2 or not test:
                raise ValueError("insufficient eligible train or evaluation samples")
            run.update(stage=stage, event="DONE", done=done, total=total)
            stage = "train_random_forest"
            done = 0
            total = estimators
            vectorizer = DictVectorizer(sparse=True)
            x_train = vectorizer.fit_transform([row[3] for row in train])
            y_train = [float(row[2].actual_qty_30d) for row in train]
            model = RandomForestRegressor(
                n_estimators=10, min_samples_leaf=2, max_depth=6, random_state=42,
                n_jobs=1, warm_start=True,
            )
            run.update(stage=stage, event="START", done=0, total=estimators)
            train_started = time.monotonic()
            with threadpool_limits(limits=1):
                for count in range(10, estimators + 1, 10):
                    if cancel_check is not None and cancel_check():
                        raise TrainingCancelled()
                    model.n_estimators = count
                    model.fit(x_train, y_train)
                    done = len(model.estimators_)
                    run.update(stage=stage, event="PROGRESS" if done < estimators else "DONE",
                               done=done, total=estimators)
            training_ms = int((time.monotonic() - train_started) * 1000)
            stage = "evaluate"
            done = 0
            total = len(test)
            run.update(stage=stage, event="START", done=0, total=total)
            x_test = vectorizer.transform([row[3] for row in test])
            forecast_started = time.monotonic()
            predictions = model.predict(x_test)
            forecast_ms = int((time.monotonic() - forecast_started) * 1000)
            ml_rows = []
            baselines = {"moving_average": [], "ewma": []}
            output = []
            for row, prediction in zip(test, predictions):
                if cancel_check is not None and cancel_check():
                    raise TrainingCancelled()
                as_of, code, example, _ = row
                quantity = Decimal(str(max(0.0, float(prediction))))
                ml_rows.append(Forecast(company_id, code, as_of, 30, "random_forest", quantity,
                                        example.actual_qty_30d, "ready", daily.metric_policy_version))
                baseline_values = {}
                for method in baselines:
                    forecast = forecast_from_example(example, method=method, dataset=daily)
                    baselines[method].append(forecast)
                    baseline_values[method] = str(forecast.predicted_quantity)
                output.append({"as_of": as_of.isoformat(), "product_code": code,
                               "actual": str(example.actual_qty_30d), "random_forest": str(quantity),
                               **baseline_values, "evaluation_grade": "research_posthoc"})
                done += 1
                run.update(stage=stage, event="PROGRESS" if done < total else "DONE", done=done, total=total)
            stage = "save"
            run.update(stage=stage, event="START", done=0, total=1)
            model_root.mkdir(parents=True, exist_ok=False)
            model_path = model_root / "random_forest.joblib"
            dump({"vectorizer": vectorizer, "model": model}, model_path, compress=3)
            model_hash = hashlib.sha256(model_path.read_bytes()).hexdigest()
            restored = load(model_path)
            if list(restored["model"].predict(restored["vectorizer"].transform([row[3] for row in test]))) != list(predictions):
                raise ValueError("model reload changed predictions")
            (model_root / "manifest.json").write_text(json.dumps({
                "dataset_version": payload["dataset_version"], "evaluation_grade": "research_posthoc",
                "source_sha256": manifest["sha256_by_file"][source.name],
                "model_sha256": model_hash, "train_dates": [day.isoformat() for day in training_dates],
                "evaluation_date": evaluation_date.isoformat(), "estimator_count": estimators,
                "not_operationally_approved": True,
            }, indent=2) + "\n", encoding="utf-8")
            (run.path / "predictions.json").write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
            _, peak_bytes = tracemalloc.get_traced_memory()
            report = {
                "run_id": run_id, "dataset_version": payload["dataset_version"],
                "evaluation_grade": "research_posthoc", "train_dates": [day.isoformat() for day in training_dates],
                "evaluation_date": evaluation_date.isoformat(), "train_samples": len(train),
                "evaluation_samples": len(test), "withheld": withheld,
                "metrics": {"random_forest": _metric_dict(ml_rows),
                            **{method: _metric_dict(rows) for method, rows in baselines.items()}},
                "training_ms": training_ms, "forecast_ms": forecast_ms,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "cpu_ms": int((time.process_time() - cpu_started) * 1000), "peak_python_bytes": peak_bytes,
                "model_sha256": model_hash, "not_operationally_approved": True,
            }
            (run.path / "summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            run.update(stage=stage, event="DONE", done=1, total=1)
            run.update(stage="complete", event="DONE", done=1, total=1)
            return report
        except (TrainingCancelled, KeyboardInterrupt):
            run.update(stage=stage, event="CANCEL", done=done, total=total)
            raise
        except Exception as exc:
            run.update(stage=stage, event="ERROR", done=done, total=total,
                       error_code=type(exc).__name__.upper())
            raise
        finally:
            tracemalloc.stop()
