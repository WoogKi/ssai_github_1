"""Extract bounded DB3 R120/R130 month-product batches into immutable ML storage."""

from __future__ import annotations

import argparse
import hashlib
import json
import signal
import sys
from contextlib import ExitStack
from datetime import date, datetime
from pathlib import Path
from threading import Event, Lock, Thread
from time import perf_counter, process_time
from typing import Any, Callable, Iterator, TextIO

import pyodbc

from app.ml.bulk_dataset import Batch, ProgressCallback, build_bulk_dataset, load_frozen_active_master
from app.ml.integration import ml_storage_root
from app.ml.db3_probe import SAMPLE_R120, SAMPLE_R130
from app.services.ssai_analysis_profile_service import load_dashboard_profile_checked
from app.services.ssai_auth_service import build_company_conn_str, get_company_db_config
from app.services.sql_server_workload_monitor import SqlServerWorkloadMonitor, worker_process_metrics


class ProgressReporter:
    def __init__(self, *, interval: float = 10.0, stream: TextIO | None = None,
                 concise: bool = False) -> None:
        self.interval = interval
        self.concise = concise
        self.stream = stream if stream is not None else sys.stderr
        self.started = perf_counter()
        self.started_cpu = process_time()
        self.lock = Lock()
        self.stop = Event()
        self.halt = Event()
        self.thread = Thread(target=self._heartbeat, daemon=True)
        self.stage = "setup"
        self.batch = "-"
        self.planned: int | None = None
        self.existing_candidates = 0
        self.reused = 0
        self.completed = 0
        self.fetched = 0
        self.verified = 0
        self.source_select_count = 0
        self.source_client_sql_ms = 0
        self.batch_elapsed_total_ms = 0
        self.batch_elapsed_max_ms = 0
        self.monitor_select_setup_count = 0
        self.monitor_summary: dict[str, Any] | None = None
        self.final_state: str | None = None
        self.first_new_batch_started: float | None = None

    def __enter__(self) -> ProgressReporter:
        with self.lock:
            self._emit("RUNNING")
        self.thread.start()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, _exc: BaseException | None,
                 _tb: Any) -> None:
        try:
            if exc_type is not None:
                with self.lock:
                    self._emit("FAILED", error=exc_type.__name__)
        finally:
            self.stop.set()
            self.thread.join()
            with self.lock:
                monitor_selects = self.monitor_select_setup_count + (
                    self.monitor_summary or {}).get("monitor_select_attempts", 0)
                summary = {
                    "scope": "DB3_EXTRACTION_RUN",
                    "state": "FAILED" if exc_type else (self.final_state or "FINISHED"),
                    "source_select_attempts": self.source_select_count,
                    "monitor_select_attempts": monitor_selects,
                    "total_db_select_attempts": self.source_select_count + monitor_selects,
                    "source_cursor_execute_fetch_ms": self.source_client_sql_ms,
                    "fetched_rows": self.fetched, "verified_rows": self.verified,
                    "new_batches": self.completed,
                    "reused_batches": self.reused,
                    "planned_batches": self.planned,
                    "new_batch_avg_ms": round(self.batch_elapsed_total_ms / self.completed) if self.completed else None,
                    "new_batch_max_ms": self.batch_elapsed_max_ms if self.completed else None,
                    "elapsed_ms": round((perf_counter() - self.started) * 1000),
                    "blocking_observed_in_samples": (self.monitor_summary or {}).get("blocking_observed"),
                    "error_count": int(exc_type is not None),
                    "db3_monitor": self.monitor_summary or {"state": "DISABLED"},
                    "worker_process": worker_process_metrics(self.started_cpu),
                }
                print("[DB3] PERF_SUMMARY " + json.dumps(summary, ensure_ascii=False, sort_keys=True),
                      file=self.stream, flush=True)

    def _emit(self, event: str, **extra: Any) -> None:
        fields = {
            "state": "RUNNING" if event not in ("DONE", "PARTIAL", "FAILED") else event,
            "stage": self.stage, "batch": self.batch,
            "planned_batches": self.planned if self.planned is not None else "unknown",
            "existing_candidates": self.existing_candidates,
            "reused_verified": self.reused, "new_completed": self.completed,
            "fetched_rows": self.fetched, "verified_rows": self.verified,
            "elapsed_s": f"{perf_counter() - self.started:.1f}",
        }
        fields.update(extra)
        print("[DB3] " + event + " " + " ".join(f"{key}={value}" for key, value in fields.items()),
              file=self.stream, flush=True)

    def _heartbeat(self) -> None:
        while not self.stop.wait(self.interval):
            with self.lock:
                if self.concise:
                    remaining = max(0, self.planned - self.existing_candidates - self.completed) if self.planned is not None else None
                    eta = (round((perf_counter() - self.first_new_batch_started) * remaining / self.completed)
                           if self.first_new_batch_started is not None and self.completed and remaining is not None
                           else "unknown")
                    self._emit("PROGRESS_10M", eta_s=eta)
                else:
                    print(f"[DB3] HEARTBEAT RUNNING elapsed_s={perf_counter() - self.started:.1f} "
                          f"last_confirmed_rows={self.fetched}", file=self.stream, flush=True)

    def update(self, event: str, details: dict[str, Any]) -> None:
        with self.lock:
            if event == "stage":
                self.stage = str(details["stage"])
                if "batch_id" in details or self.stage == "dataset_verify":
                    self.batch = str(details.get("batch_id", "-"))
            elif event == "plan":
                self.planned = int(details["planned_batches"])
                self.existing_candidates = int(details["existing_candidates"])
                self._emit("PLAN", product_master_sha256=details["product_master_sha256"])
            elif event == "batch_start":
                self.batch = str(details["batch_id"])
                self.stage = "source_totals"
                if self.first_new_batch_started is None:
                    self.first_new_batch_started = perf_counter()
                if not self.concise:
                    self._emit("BATCH_START")
            elif event == "rows":
                previous = self.fetched
                self.fetched += int(details["count"])
                if not self.concise:
                    for boundary in range(previous // 10000 + 1, self.fetched // 10000 + 1):
                        self._emit("FETCH_10000", boundary_rows=boundary * 10000)
            elif event == "batch_reused":
                self.batch = str(details["batch_id"])
                self.stage = "reused_verify"
                self.reused += 1
                self.verified += int(details["rows"])
                if not self.concise and (self.reused % 10 == 0 or self.reused == self.existing_candidates):
                    self._emit("REUSED")
            elif event == "batch_complete":
                self.completed += 1
                self.verified += int(details["rows"])
                elapsed = int(details.get("elapsed_ms", 0))
                self.batch_elapsed_total_ms += elapsed
                self.batch_elapsed_max_ms = max(self.batch_elapsed_max_ms, elapsed)
                if not self.concise:
                    self._emit("BATCH_DONE", batch_rows=details["rows"])
            elif event == "product_drift":
                self._emit("PRODUCT_DRIFT", **details)
            elif event == "source_select":
                self.source_select_count += 1
            elif event == "source_sql_elapsed":
                self.source_client_sql_ms += int(details["elapsed_ms"])
            elif event == "monitor_setup_select":
                self.monitor_select_setup_count += 1
            elif event == "db_monitor_sample":
                self._emit("DB_MONITOR", samples=details["sample_count"],
                           request_active=details["request_active"], blocking=details["blocking"],
                           session_cpu_ms=details["session_cpu_ms"],
                           logical_reads=details["session_logical_reads"],
                           reads=details["session_reads"],
                           request_elapsed_ms=details["request_elapsed_ms"],
                           request_wait_ms=details["request_wait_ms"],
                           tempdb_net_kb=details["tempdb_net_kb"],
                           server_available_memory_kb=details["server_available_memory_kb"],
                           unavailable=",".join(f"{name}:{reason}" for name, reason in details["unavailable"].items()))
            elif event == "monitor_summary":
                self.monitor_summary = details
            elif event == "complete":
                self.stage = "complete"
                self.batch = "-"
                self.verified = int(details["verified_rows"])
                self.final_state = "DONE" if details["dataset_status"] == "RESEARCH_POSTHOC" else "PARTIAL"
                self._emit(self.final_state, verified_batches=details["verified_batches"])


class Db3Source:
    def __init__(self, connection: pyodbc.Connection, progress: ProgressCallback | None = None) -> None:
        self.connection = connection
        self.progress = progress
        self.query_count = 0

    def rows(self, sql: str, params: tuple[Any, ...]) -> Iterator[dict[str, Any]]:
        if not sql.lstrip().upper().startswith("SELECT"):
            raise ValueError("DB3 source is read-only")
        self.query_count += 1
        cursor_wait_ms = 0.0
        if self.progress is not None:
            self.progress("source_select", {})
        cursor = self.connection.cursor()
        try:
            started = perf_counter()
            try:
                cursor.execute(sql, *params)
            finally:
                cursor_wait_ms += (perf_counter() - started) * 1000
            names = [item[0] for item in cursor.description]
            while True:
                started = perf_counter()
                try:
                    chunk = cursor.fetchmany(500)
                finally:
                    cursor_wait_ms += (perf_counter() - started) * 1000
                if not chunk:
                    break
                for item in chunk:
                    yield dict(zip(names, item))
        finally:
            cursor.close()
            if self.progress is not None:
                self.progress("source_sql_elapsed", {"elapsed_ms": round(cursor_wait_ms)})


    def product_rows(self) -> Iterator[dict[str, Any]]:
        for row in self.rows(
            "SELECT Rd04_Physic_Cd, Rd04_Use_Gu, Rd04_Add_Date "
            "FROM dbo.Rddbc040 ORDER BY Rd04_Physic_Cd", (),
        ):
            yield {**row, "company_id": 3}

    def totals(self, batch: Batch) -> dict[str, Any]:
        marks = ", ".join("?" for _ in batch.product_codes)
        sql = f"""
SELECT COUNT_BIG(*) AS row_count,
       SUM(CAST(ISNULL(Rd12_Quantity, 0) AS decimal(38,4))) AS quantity,
       SUM(CAST(ISNULL(Rd12_Oquantity, 0) AS decimal(38,4))) AS bonus_quantity
FROM dbo.Rddbc120
WHERE Rd12_Out_YyMmDd >= ? AND Rd12_Out_YyMmDd < ?
  AND Rd12_Physic_Cd IN ({marks})
  AND (Rd12_Io_Gu LIKE '5%' OR Rd12_Io_Gu LIKE '6%')
"""
        result = list(self.rows(sql, (batch.date_from, batch.date_to_exclusive, *batch.product_codes)))
        if len(result) != 1:
            raise ValueError("DB3 R120 aggregate did not return one row")
        row = result[0]
        return {"row_count": int(row["row_count"]), "quantity": row["quantity"] or 0,
                "bonus_quantity": row["bonus_quantity"] or 0}

    def outbound_rows(self, batch: Batch) -> Iterator[dict[str, Any]]:
        marks = ", ".join("?" for _ in batch.product_codes)
        yield from self.rows(
            SAMPLE_R120.format(product_marks=marks, row_limit=10001),
            (batch.date_from, batch.date_to_exclusive, *batch.product_codes),
        )

    def document_rows(self, batch: Batch) -> Iterator[dict[str, Any]]:
        marks = ", ".join("?" for _ in batch.product_codes)
        yield from self.rows(
            SAMPLE_R130.format(product_marks=marks, row_limit=10001),
            (batch.date_from, batch.date_to_exclusive, *batch.product_codes),
        )


def _stop_monitor(monitor: SqlServerWorkloadMonitor, progress: ProgressCallback | None) -> None:
    monitor.stop()
    if progress is not None:
        progress("monitor_summary", monitor.summary())


def _prior_codes(root: Path, version: str) -> set[str]:
    if not version.startswith("db3-research-") or not version.replace("-", "").isalnum():
        raise ValueError("invalid prior DB3 research version")
    source = root / "datasets" / version
    raw = (source / "fixture.json").read_bytes()
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("company_id") != 3 or manifest.get("dataset_version") != version
            or manifest.get("sha256_by_file", {}).get("fixture.json") != hashlib.sha256(raw).hexdigest()):
        raise ValueError("prior DB3 cohort is not verified")
    return {row["Rd04_Physic_Cd"].strip() for row in json.loads(raw)["product_rows"]}


def _fixed_cohort(root: Path, version: str, trusted_manifest_sha256: str) -> tuple[tuple[str, ...], dict[str, Any]]:
    if not version.startswith("db3-bulk-") or not version.replace("-", "").isalnum():
        raise ValueError("invalid fixed DB3 cohort version")
    if len(trusted_manifest_sha256) != 64 or any(char not in "0123456789abcdef" for char in trusted_manifest_sha256):
        raise ValueError("fixed cohort requires a trusted SHA256")
    source = root / "datasets" / version
    manifest_path = source / "manifest.json"
    master_path = source / "products.jsonl"
    if source.is_symlink() or manifest_path.is_symlink() or master_path.is_symlink():
        raise ValueError("fixed cohort cannot use linked files")
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != trusted_manifest_sha256:
        raise ValueError("fixed cohort manifest checksum mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("schema_version") != "ssai_ml_bulk_supply_v0.1"
            or manifest.get("dataset_version") != version or manifest.get("company_id") != 3
            or manifest.get("source_db_alias") != "DB3" or manifest.get("dataset_status") != "COMPLETE"
            or manifest.get("evaluation_grade") != "RESEARCH_POSTHOC"
            or hashlib.sha256(master_path.read_bytes()).hexdigest() != manifest.get("master_sha256")):
        raise ValueError("fixed cohort contract or product checksum mismatch")
    codes = []
    with master_path.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            code = row.get("Rd04_Physic_Cd")
            if row.get("company_id") != 3 or not isinstance(code, str) or not code.strip():
                raise ValueError("fixed cohort product identity mismatch")
            codes.append(code.strip())
    if not codes or len(codes) != manifest.get("product_count") or len(codes) != len(set(codes)):
        raise ValueError("fixed cohort product count or uniqueness mismatch")
    return tuple(codes), manifest


def _select_fixed_products(all_products: list[dict[str, Any]], codes: tuple[str, ...]) -> list[dict[str, Any]]:
    by_code: dict[str, dict[str, Any]] = {}
    for row in all_products:
        code = str(row["Rd04_Physic_Cd"]).strip()
        if code in by_code:
            raise ValueError("duplicate R040 product code")
        by_code[code] = row
    if not set(codes).issubset(by_code):
        raise ValueError("fixed cohort product is missing from R040")
    return [by_code[code] for code in codes]


def _source_identity_sha256(source: Db3Source, cfg: Any) -> str:
    rows = list(source.rows(
        "SELECT DB_NAME() AS database_name, @@SERVERNAME AS server_name, "
        "CONVERT(varchar(36), database_guid) AS database_guid, "
        "CONVERT(varchar(36), recovery_fork_guid) AS recovery_fork_guid "
        "FROM sys.database_recovery_status WHERE database_id = DB_ID()", (),
    ))
    if len(rows) != 1 or not all(rows[0].get(name) for name in (
            "database_name", "server_name", "database_guid", "recovery_fork_guid")):
        raise ValueError("DB3 database identity is unavailable; active extraction cannot continue")
    if str(rows[0]["database_name"]).strip().casefold() != str(cfg.db_name).strip().casefold():
        raise ValueError("DB3 connection database differs from company configuration")
    identity = {
        "company_id": cfg.company_id,
        "configured_server": str(cfg.db_server).strip().casefold(),
        "configured_port": str(cfg.db_port),
        "configured_database": str(cfg.db_name).strip().casefold(),
        "actual_server": str(rows[0]["server_name"]).strip().casefold(),
        "database_guid": str(rows[0]["database_guid"]).strip().casefold(),
        "recovery_fork_guid": str(rows[0]["recovery_fork_guid"]).strip().casefold(),
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _product_drift(frozen: tuple[dict[str, Any], ...], current: list[dict[str, Any]]) -> dict[str, Any]:
    old = {row["Rd04_Physic_Cd"].strip(): row for row in frozen}
    now = {row["Rd04_Physic_Cd"].strip(): json.loads(json.dumps(row, default=str)) for row in current}
    if len(now) != len(current):
        raise ValueError("duplicate R040 active product code")
    added = set(now) - set(old)
    removed = set(old) - set(now)
    changed = {code for code in set(old) & set(now) if old[code] != now[code]}
    affected = sorted(added | removed | changed)
    def digest(codes: set[str] | list[str]) -> str:
        return hashlib.sha256(json.dumps(sorted(codes), ensure_ascii=False,
                                         separators=(",", ":")).encode()).hexdigest()
    return {
        "new_active": len(added), "no_longer_active": len(removed),
        "master_changed": len(changed), "affected_codes_sha256": digest(affected),
        "new_active_codes_sha256": digest(added),
        "no_longer_active_codes_sha256": digest(removed),
        "master_changed_codes_sha256": digest(changed),
    }


def run(*, dataset_version: str, date_from: date, date_to: date,
        group_size: int, product_limit: int | None, include_dataset_version: str | None,
        resume: bool, max_batches: int | None, inventory_only: bool = False,
        fixed_cohort_version: str | None = None,
        fixed_cohort_manifest_sha256: str | None = None,
        active_only: bool = False,
        monitor_db3: bool = False, monitor_interval_s: int = 60,
        monitor_max_samples: int = 120,
        monitor_approval_reference: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
        progress: ProgressCallback | None = None) -> dict[str, Any]:
    if not dataset_version.startswith("db3-bulk-") or not dataset_version.replace("-", "").isalnum():
        raise ValueError("invalid DB3 bulk version")
    root = ml_storage_root()
    started = perf_counter()
    if bool(fixed_cohort_version) != bool(fixed_cohort_manifest_sha256):
        raise ValueError("fixed cohort version and trusted checksum must be supplied together")
    if monitor_db3 and not monitor_approval_reference:
        raise ValueError("DB3 monitoring requires an explicit approval reference")
    if fixed_cohort_version and (product_limit is not None or include_dataset_version or inventory_only):
        raise ValueError("fixed cohort cannot be combined with pilot selection or inventory mode")
    if active_only and (fixed_cohort_version or product_limit is not None
                        or include_dataset_version or inventory_only):
        raise ValueError("active-only extraction cannot be combined with pilot or inventory selection")
    fixed_codes, fixed_manifest = (
        _fixed_cohort(root, fixed_cohort_version, fixed_cohort_manifest_sha256)
        if fixed_cohort_version and fixed_cohort_manifest_sha256 else ((), None)
    )
    if progress is not None:
        progress("stage", {"stage": "profile_load"})
    profile = load_dashboard_profile_checked(company_id=3)
    if profile.status != "ready" or not profile.profile:
        raise ValueError("company 3 saved Dashboard profile unavailable")
    fingerprint = hashlib.sha256(json.dumps(
        profile.profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    if fixed_manifest is not None and fixed_manifest.get("profile_fingerprint") != fingerprint:
        raise ValueError("saved Dashboard profile differs from the fixed cohort")
    if progress is not None:
        progress("stage", {"stage": "company_config"})
    cfg = get_company_db_config(3)
    if cfg.company_id != 3:
        raise ValueError("ERP connection is not company 3")
    if progress is not None:
        progress("stage", {"stage": "erp_connect"})
    connection_string = build_company_conn_str(cfg)
    with pyodbc.connect(connection_string, timeout=10, autocommit=True) as connection, ExitStack() as monitor_stack:
        connection.timeout = 120
        source = Db3Source(connection, progress=progress)
        source_identity = _source_identity_sha256(source, cfg) if active_only else None
        if monitor_db3:
            if not 60 <= monitor_interval_s <= 600 or not 1 <= monitor_max_samples <= 120:
                raise ValueError("DB3 monitor interval or sample cap out of bounds")
            if progress is not None:
                progress("monitor_setup_select", {})
            try:
                cursor = connection.cursor()
                try:
                    cursor.execute("SELECT @@SPID")
                    session_id = int(cursor.fetchone()[0])
                finally:
                    cursor.close()
                monitor = SqlServerWorkloadMonitor(
                    lambda: pyodbc.connect(connection_string, timeout=10, autocommit=True),
                    session_id=session_id, interval_s=monitor_interval_s,
                    max_samples=monitor_max_samples,
                    on_sample=(lambda sample: progress("db_monitor_sample", sample)) if progress else None,
                )
                monitor.start()
                monitor_stack.callback(_stop_monitor, monitor, progress)
            except Exception as exc:
                if progress is not None:
                    progress("monitor_summary", {"state": "UNAVAILABLE", "monitor_select_attempts": 0,
                                                 "unavailable": {"setup": type(exc).__name__}})
        if progress is not None:
            progress("stage", {"stage": "product_master"})
        all_products = list(source.product_rows())
        active = [row for row in all_products if str(row.get("Rd04_Use_Gu") or "").strip() == "0"]
        if inventory_only:
            report_path = root / "diagnostics" / (dataset_version + "-r040.json")
            report_path.parent.mkdir(parents=True, exist_ok=True)
            with report_path.open("x", encoding="utf-8") as stream:
                json.dump({"company_id": 3, "source_db_alias": "DB3", "products": all_products},
                          stream, ensure_ascii=False, default=str)
            return {"all_master_products": len(all_products), "all_active_products": len(active),
                    "all_stopped_products": len(all_products) - len(active),
                    "inventory_file_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
                    "erp_select_count": source.query_count,
                    "elapsed_ms": int((perf_counter() - started) * 1000)}
        if fixed_manifest is not None:
            selected = _select_fixed_products(all_products, fixed_codes)
        elif active_only:
            selected = (load_frozen_active_master(root / "datasets" / dataset_version)
                        if resume else active)
            if resume and progress is not None:
                progress("product_drift", _product_drift(selected, active))
        elif product_limit is not None:
            if not 1 <= product_limit <= 300:
                raise ValueError("pilot is limited to at most 300 products")
            prior = _prior_codes(root, include_dataset_version) if include_dataset_version else set()
            if not prior.issubset({row["Rd04_Physic_Cd"].strip() for row in all_products}):
                raise ValueError("prior cohort is missing from R040")
            if len(prior) > product_limit:
                raise ValueError("prior cohort exceeds pilot limit")
            selected = [row for row in all_products if row["Rd04_Physic_Cd"].strip() in prior]
            selected += [row for row in active if row["Rd04_Physic_Cd"].strip() not in prior][:product_limit - len(selected)]
        else:
            selected = all_products
        summary = build_bulk_dataset(
            root, dataset_version=dataset_version, source=source, master_rows=selected,
            saved_io_codes=profile.profile.get("io_gu_list") or (), profile_fingerprint=fingerprint,
            calendar_version="UNAVAILABLE_ASOF_EXTRACTION", date_from=date_from, date_to=date_to,
            group_size=group_size, resume=resume, max_batches=max_batches,
            active_only=active_only, source_identity_sha256=source_identity,
            stop_requested=stop_requested,
            progress=progress,
        )
        return {**summary, "all_master_products": len(all_products), "all_active_products": len(active),
                "all_stopped_products": len(all_products) - len(active), "erp_select_count": source.query_count,
                "elapsed_ms": int((perf_counter() - started) * 1000)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-read-only", action="store_true")
    parser.add_argument("--company-id", type=int, required=True)
    parser.add_argument("--dataset-version", default="db3-bulk-" + datetime.now().strftime("%Y%m%d%H%M%S"))
    parser.add_argument("--date-from", default="2018-01-01")
    parser.add_argument("--date-to", default="2026-08-30")
    parser.add_argument("--group-size", type=int)
    parser.add_argument("--active-only", action="store_true",
                        help="freeze all currently active R040 products in a new dataset")
    parser.add_argument("--product-limit", type=int)
    parser.add_argument("--include-dataset-version")
    parser.add_argument("--fixed-cohort-version")
    parser.add_argument("--fixed-cohort-manifest-sha256")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-batches", type=int)
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--monitor-db3", action="store_true",
                        help="opt-in bounded read-only DMV sampling for the DB3 extraction session")
    parser.add_argument("--monitor-approval-reference",
                        help="required approval reference for live DB3 DMV sampling")
    parser.add_argument("--monitor-interval-s", type=int, default=60)
    parser.add_argument("--monitor-max-samples", type=int, default=120)
    args = parser.parse_args()
    if not args.live_read_only or args.company_id != 3:
        parser.error("explicit company 3 read-only scope required")
    if args.monitor_db3 and not args.monitor_approval_reference:
        parser.error("DB3 monitoring requires an explicit approval reference")
    if args.monitor_db3 and (not 60 <= args.monitor_interval_s <= 600
                             or not 1 <= args.monitor_max_samples <= 120):
        parser.error("DB3 monitoring requires interval 60..600s and max samples 1..120")
    with ProgressReporter(interval=600.0 if args.active_only else 10.0,
                          concise=args.active_only) as reporter:
        previous_sigint = signal.getsignal(signal.SIGINT)
        if args.active_only:
            def request_stop(_signum: int, _frame: Any) -> None:
                if reporter.halt.is_set():
                    raise KeyboardInterrupt
                reporter.halt.set()
                print("[DB3] STOP_REQUESTED after_current_batch=1", file=sys.stderr, flush=True)
            signal.signal(signal.SIGINT, request_stop)
        try:
            result = run(
            dataset_version=args.dataset_version, date_from=date.fromisoformat(args.date_from),
            date_to=date.fromisoformat(args.date_to),
            group_size=args.group_size if args.group_size is not None else (300 if args.active_only else 100),
            product_limit=args.product_limit, include_dataset_version=args.include_dataset_version,
            resume=args.resume, max_batches=args.max_batches, inventory_only=args.inventory_only,
            fixed_cohort_version=args.fixed_cohort_version,
            fixed_cohort_manifest_sha256=args.fixed_cohort_manifest_sha256,
            active_only=args.active_only,
            monitor_db3=args.monitor_db3, monitor_interval_s=args.monitor_interval_s,
            monitor_max_samples=args.monitor_max_samples,
            monitor_approval_reference=args.monitor_approval_reference,
            stop_requested=reporter.halt.is_set if args.active_only else None,
            progress=reporter.update,
            )
        finally:
            if args.active_only:
                signal.signal(signal.SIGINT, previous_sigint)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
