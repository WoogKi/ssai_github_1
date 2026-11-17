"""Opt-in company RF40 research supply using the verified DB3 bulk batch engine."""

from __future__ import annotations

import argparse
import hashlib
import json
import signal
from contextlib import closing
from datetime import date
from pathlib import Path
from time import monotonic

import pyodbc

from app.ml.bulk_dataset import build_bulk_dataset, load_frozen_active_master
from app.services.ssai_analysis_profile_service import load_dashboard_profile_checked
from app.services.ssai_auth_service import build_company_conn_str, get_company_db_config
from app.services.saved_io_scope import resolve_saved_io_scope
from tools.extract_ml_bulk_db3 import Db3Source, _source_identity_sha256


SOURCE_DB = {4: 3, 7: 7, 8: 8}
CLONE_AS_OF = date(2026, 11, 3)


def identity(company_id: int, start: date, end: date) -> tuple[int, date | None]:
    if company_id not in SOURCE_DB or start.day != 1 or end < start:
        raise ValueError("invalid company or date scope")
    clone = CLONE_AS_OF if company_id == 4 else None
    if clone is not None and end > clone:
        raise ValueError("company 4 source period exceeds the DB3 clone date")
    if end > date.today():
        raise ValueError("source period extends into the future")
    return SOURCE_DB[company_id], clone


def extract(args) -> dict:
    source_company_id, clone = identity(args.company, args.date_from, args.date_to)
    if not args.approval_reference or not args.live_read_only:
        raise ValueError("live extraction requires explicit approval and read-only mode")
    if args.max_batches < 1 or not 1 <= args.query_timeout_s <= 120 or args.max_runtime_s < args.query_timeout_s:
        raise ValueError("invalid batch, SQL timeout, or runtime budget")
    version = args.dataset_version
    root = args.output_root
    profile = load_dashboard_profile_checked(company_id=args.company)
    if profile.status != "ready" or not profile.profile:
        raise ValueError("company Saved IO profile is unavailable")
    saved_io = profile.profile.get("io_gu_list") or ()
    scope = resolve_saved_io_scope(saved_io, confirmed_trade_roles=True)
    if "502" not in scope.outbound_codes:
        raise ValueError("company RF40 requires normal IO 502 in the approved profile")
    fingerprint = hashlib.sha256(json.dumps(
        profile.profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    cfg = get_company_db_config(source_company_id)
    if cfg.company_id != source_company_id:
        raise ValueError("ERP DB configuration differs from approved source mapping")
    started = monotonic()
    stop = False
    sql_elapsed_ms = 0
    sql_selects = 0
    current_batch = None

    def progress(event: str, details: dict) -> None:
        nonlocal sql_elapsed_ms, sql_selects, current_batch
        if event == "stage" and details.get("batch_id"):
            current_batch = details["batch_id"]
        if event == "source_select":
            sql_selects += 1
        elif event == "source_sql_elapsed":
            elapsed = int(details["elapsed_ms"])
            sql_elapsed_ms += elapsed
            print(json.dumps({"event": "sql_elapsed", "batch_id": current_batch,
                              "elapsed_ms": elapsed}), flush=True)
        elif event == "batch_complete":
            print(json.dumps({"event": event, "batch_id": details["batch_id"],
                              "rows": details["rows"], "elapsed_ms": details["elapsed_ms"]}), flush=True)

    def request_stop(_signum, _frame):
        nonlocal stop
        stop = True

    previous = signal.signal(signal.SIGINT, request_stop)
    try:
        with closing(pyodbc.connect(build_company_conn_str(cfg), timeout=10, autocommit=True)) as connection:
            connection.timeout = args.query_timeout_s
            source = Db3Source(connection, progress=progress, company_id=args.company)
            source_sha = _source_identity_sha256(source, cfg)
            session_rows = list(source.rows("SELECT @@SPID AS session_id", ()))
            if len(session_rows) != 1 or not isinstance(session_rows[0].get("session_id"), int):
                raise ValueError("ERP session identity is unavailable")
            session_id = session_rows[0]["session_id"]
            print(json.dumps({"event": "erp_session", "company_id": args.company,
                              "session_id": session_id}), flush=True)
            target = root / "datasets" / version
            current_batch = "product_master"
            masters = (load_frozen_active_master(target) if args.resume
                       else tuple(source.active_product_rows()))
            result = build_bulk_dataset(
                root, dataset_version=version, source=source, master_rows=masters,
                saved_io_codes=saved_io,
                profile_fingerprint=fingerprint, calendar_version=args.calendar_version,
                date_from=args.date_from, date_to=args.date_to, group_size=100,
                resume=args.resume, max_batches=args.max_batches, active_only=True,
                source_identity_sha256=source_sha, coalesce_groups=3,
                company_id=args.company, source_company_id=source_company_id, clone_as_of=clone,
                stop_requested=lambda: stop or monotonic() - started >= args.max_runtime_s,
                progress=progress,
            )
            return {**result, "company_id": args.company, "source_company_id": source_company_id,
                    "erp_session_id": session_id,
                    "erp_select_count": source.query_count, "sql_select_count": sql_selects,
                    "sql_elapsed_ms": sql_elapsed_ms,
                    "elapsed_s": round(monotonic() - started, 2)}
    finally:
        signal.signal(signal.SIGINT, previous)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--extract", action="store_true")
    parser.add_argument("--company", type=int, choices=tuple(SOURCE_DB), required=True)
    parser.add_argument("--date-from", type=date.fromisoformat, required=True)
    parser.add_argument("--date-to", type=date.fromisoformat, required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--calendar-version", required=True)
    parser.add_argument("--max-batches", type=int, default=300)
    parser.add_argument("--query-timeout-s", type=int, default=60)
    parser.add_argument("--max-runtime-s", type=int, default=7200)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--live-read-only", action="store_true")
    parser.add_argument("--approval-reference")
    args = parser.parse_args()
    source_id, clone = identity(args.company, args.date_from, args.date_to)
    if args.dataset_version.startswith(f"company{args.company}-rf40-") is False:
        parser.error("dataset version does not encode the selected company")
    if args.check or args.plan:
        report = {"company_id": args.company, "source_company_id": source_id,
                  "clone_as_of": clone.isoformat() if clone else None,
                  "date_from": args.date_from.isoformat(), "date_to": args.date_to.isoformat(),
                  "dataset_version": args.dataset_version, "database_access": False,
                  "batch_strategy": "month x 100 products; up to three adjacent groups per SELECT",
                  "query_timeout_s": args.query_timeout_s, "max_runtime_s": args.max_runtime_s,
                  "max_batches": args.max_batches}
        print(json.dumps(report, ensure_ascii=False))
        return
    if not args.extract:
        parser.error("no execution mode")
    print(json.dumps(extract(args), ensure_ascii=False))


if __name__ == "__main__":
    main()
