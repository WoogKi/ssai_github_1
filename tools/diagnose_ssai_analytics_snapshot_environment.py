"""Read-only preflight for company ERP and SSAI_ANALYTICS access.

Run this only from the Windows SIMS environment that can reach the SSAI
management DB. It intentionally performs one configuration read and at most
one ERP plus one SSAI_ANALYTICS connection per target company.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import pyodbc


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.ssai_analytics_db import ANALYTICS_DATABASE_NAME  # noqa: E402
from app.services.ssai_auth_service import (  # noqa: E402
    build_company_conn_str,
    get_company_db_config,
)


TARGET_SMS_IDS = (1, 2, 4, 6, 7, 8)
DEFAULT_OUTPUT_DIR = Path(
    r"C:\New\Python_Project\_codex_diffs\LmStudion_project1\20260819"
)


@dataclass
class DiagnosticResult:
    sms: int
    started_at: str = ""
    finished_at: str = ""
    elapsed_seconds: float = 0.0
    erp_connect: str = "NOT_RUN"
    analytics_exists: str = "NOT_RUN"
    analytics_select: str = "NOT_RUN"
    verdict: str = "STOP"
    note: str = ""


def _safe_error(exc: BaseException) -> str:
    """Return diagnostics without connection strings, server names, or messages."""
    sqlstate = ""
    native_code = ""
    for arg in getattr(exc, "args", ()):
        if not isinstance(arg, tuple):
            continue
        if not sqlstate and arg:
            sqlstate = str(arg[0] or "").strip()
        if not native_code and len(arg) >= 3:
            native_code = str(arg[2] or "").strip()
    parts = [type(exc).__name__]
    if sqlstate:
        parts.append(f"SQLSTATE={sqlstate}")
    if native_code:
        parts.append(f"native={native_code}")
    return " ".join(parts)


def _finalize(result: DiagnosticResult) -> DiagnosticResult:
    if result.erp_connect == "PASS" and result.analytics_select == "PASS":
        result.verdict = "PASS"
    elif result.erp_connect == "NOT_RUN":
        result.verdict = "STOP"
    else:
        result.verdict = "FAIL"
    return result


def diagnose_company(company_id: int) -> DiagnosticResult:
    """Inspect one configured company without retrying or writing to either DB."""
    started_at = datetime.now()
    started_clock = perf_counter()
    result = DiagnosticResult(
        sms=int(company_id),
        started_at=started_at.isoformat(timespec="seconds"),
    )
    try:
        try:
            config = get_company_db_config(int(company_id))
        except Exception as exc:
            result.note = f"company_config_read_failed:{_safe_error(exc)}"
            return _finalize(result)

        try:
            with pyodbc.connect(build_company_conn_str(config), timeout=10, autocommit=True) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                cursor.fetchone()
                result.erp_connect = "PASS"
                exists = cursor.execute(
                    "SELECT CASE WHEN DB_ID(N'SSAI_ANALYTICS') IS NULL THEN 0 ELSE 1 END"
                ).fetchone()
                result.analytics_exists = "PASS" if bool(exists[0]) else "MISSING"
        except Exception as exc:
            result.note = f"erp_select_failed:{_safe_error(exc)}"
            return _finalize(result)

        if result.analytics_exists != "PASS":
            result.note = "analytics_database_missing"
            return _finalize(result)

        try:
            analytics_config = replace(config, db_name=ANALYTICS_DATABASE_NAME)
            with pyodbc.connect(build_company_conn_str(analytics_config), timeout=10, autocommit=True) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                cursor.fetchone()
                result.analytics_select = "PASS"
        except Exception as exc:
            result.note = f"analytics_select_failed:{_safe_error(exc)}"
        return _finalize(result)
    finally:
        result.finished_at = datetime.now().isoformat(timespec="seconds")
        result.elapsed_seconds = round(perf_counter() - started_clock, 3)
def _render_table(results: list[DiagnosticResult]) -> str:
    rows = [
        "SMS | 시작시간 | 종료시간 | 소요초 | ERP 접속 | SSAI_ANALYTICS 존재 | SSAI_ANALYTICS SELECT | 최종판정 | 비고",
        "--- | --- | --- | ---: | --- | --- | --- | --- | ---",
    ]
    rows.extend(
        f"{item.sms} | {item.started_at} | {item.finished_at} | {item.elapsed_seconds:.3f} | "
        f"{item.erp_connect} | {item.analytics_exists} | {item.analytics_select} | "
        f"{item.verdict} | {item.note or '-'}"
        for item in results
    )
    return "\n".join(rows)


def _render_timing_summary(*, started_at: str, finished_at: str, elapsed_seconds: float) -> str:
    return (
        f"전체 시작시간: {started_at}\n"
        f"전체 종료시간: {finished_at}\n"
        f"총 소요초: {elapsed_seconds:.3f}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only ERP/SSAI_ANALYTICS preflight for SMS 1,2,4,6,7,8"
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="JSON/TXT result directory; no database or application files are written.",
    )
    args = parser.parse_args()

    overall_started_at = datetime.now()
    overall_started_clock = perf_counter()
    results = [diagnose_company(company_id) for company_id in TARGET_SMS_IDS]
    overall_finished_at = datetime.now()
    overall_elapsed_seconds = round(perf_counter() - overall_started_clock, 3)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"ssai_analytics_snapshot_preflight_{stamp}.json"
    txt_path = output_dir / f"ssai_analytics_snapshot_preflight_{stamp}.txt"
    payload: dict[str, Any] = {
        "executed_at": overall_finished_at.isoformat(timespec="seconds"),
        "overall_started_at": overall_started_at.isoformat(timespec="seconds"),
        "overall_finished_at": overall_finished_at.isoformat(timespec="seconds"),
        "overall_elapsed_seconds": overall_elapsed_seconds,
        "read_only": True,
        "targets": [asdict(result) for result in results],
        "pass_count": sum(result.verdict == "PASS" for result in results),
        "target_count": len(results),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    table = _render_table(results)
    timing_summary = _render_timing_summary(
        started_at=payload["overall_started_at"],
        finished_at=payload["overall_finished_at"],
        elapsed_seconds=payload["overall_elapsed_seconds"],
    )
    txt_path.write_text(table + "\n\n" + timing_summary + "\n", encoding="utf-8")

    print(table)
    print(timing_summary)
    print(f"JSON={json_path}")
    print(f"TXT={txt_path}")
    return 0 if all(result.verdict == "PASS" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
