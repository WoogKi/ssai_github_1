"""Production-path, read-only DB preflight for SIMS actual diagnostics.

Run this before any company DB actual.  It intentionally uses the same company
resolver and engine builder as SIMS services; never build a connection string
or call a DB driver directly from this tool.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
import sys
import time
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.db.mssql_client as db
import app.services.ssai_auth_service as auth
from app.utils.env_config import load_project_env


EXIT_OK = 0
EXIT_ENVIRONMENT = 1
EXIT_CONFIG_DB = 2
EXIT_COMPANY_RECORD = 3
EXIT_ERP_ENGINE = 4
EXIT_ERP_SELECT = 5


@dataclass
class Step:
    status: str = "NOT_RUN"
    elapsed_ms: int = 0


@dataclass
class PreflightReport:
    company_id: int
    python_executable: str
    project_root: str
    venv_active: bool
    steps: dict[str, Step] = field(default_factory=dict)
    error_stage: str = ""
    error_class: str = ""
    sqlstate: str = ""
    error_detail: str = ""
    driver: str = ""
    total_elapsed_ms: int = 0


def _step(report: PreflightReport, name: str, status: str, started: float) -> None:
    report.steps[name] = Step(status=status, elapsed_ms=round((time.perf_counter() - started) * 1000))


def _sqlstate(exc: BaseException) -> str:
    for item in getattr(exc, "args", ()):
        if isinstance(item, tuple) and item and isinstance(item[0], str):
            return item[0]
        if isinstance(item, str) and len(item) == 5 and item.isalnum():
            return item
    return ""


def _safe_error_detail(exc: BaseException) -> str:
    """Keep a concise diagnostic without leaking endpoints or credentials."""
    import re

    detail = " ".join(str(exc).split())
    detail = re.sub(r"(?i)(password|pwd|uid|user|server|database)=([^;\s]+)", r"\1=<redacted>", detail)
    detail = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b", "<endpoint>", detail)
    return detail[:300]


def _is_company_record_error(exc: BaseException) -> bool:
    text = str(exc)
    return "SSAI_COMPANIES" in text and ("없습니다" in text or "비활성" in text or "암호화" in text)


@contextmanager
def _tracked_company_resolver(report: PreflightReport) -> Iterator[None]:
    """Record production resolver stages without changing its behavior."""
    original = auth.get_company_db_config

    def tracked(company_id: int):
        started = time.perf_counter()
        try:
            config = original(company_id)
        except Exception as exc:
            if _is_company_record_error(exc):
                _step(report, "config_db", "PASS", started)
                _step(report, "company_record", "FAIL", started)
                report.error_stage = "company_record"
            else:
                _step(report, "config_db", "FAIL", started)
                report.error_stage = "config_db"
            report.error_class = type(exc).__name__
            report.sqlstate = _sqlstate(exc)
            report.error_detail = _safe_error_detail(exc)
            raise
        _step(report, "config_db", "PASS", started)
        _step(report, "company_record", "PASS", started)
        report.driver = str(getattr(config, "db_driver", "") or "")
        return config

    auth.get_company_db_config = tracked
    try:
        yield
    finally:
        auth.get_company_db_config = original


def run_preflight(company_id: int, timeout_seconds: int = 30) -> tuple[int, PreflightReport]:
    started = time.perf_counter()
    load_project_env()
    executable = Path(sys.executable).resolve()
    report = PreflightReport(
        company_id=int(company_id),
        python_executable=str(executable),
        project_root=str(ROOT),
        venv_active=ROOT.joinpath("venv").resolve() in executable.parents,
    )
    previous_company_id = db.get_current_company_id()
    try:
        environment_started = time.perf_counter()
        if not ROOT.exists() or not executable.exists():
            raise RuntimeError("project root or Python executable is unavailable")
        _step(report, "environment", "PASS", environment_started)

        context_started = time.perf_counter()
        db.set_current_company_id(company_id)
        if db.get_current_company_id() != int(company_id):
            raise RuntimeError("company context was not applied")
        _step(report, "context", "PASS", context_started)

        with _tracked_company_resolver(report):
            engine_started = time.perf_counter()
            try:
                db.get_engine()
            except Exception as exc:
                if not report.error_stage:
                    report.error_stage = "erp_engine"
                    report.error_class = type(exc).__name__
                    report.sqlstate = _sqlstate(exc)
                    report.error_detail = _safe_error_detail(exc)
                    _step(report, "erp_engine", "FAIL", engine_started)
                return _exit_code(report), report
            _step(report, "erp_engine", "PASS", engine_started)

            select_started = time.perf_counter()
            try:
                with db.read_only_request(timeout_seconds=timeout_seconds):
                    frame = db.query_to_df("SELECT 1 AS connection_ok")
                if len(frame) != 1 or int(frame.iloc[0]["connection_ok"]) != 1:
                    raise RuntimeError("unexpected SELECT 1 result")
            except Exception as exc:
                report.error_stage = "erp_select1"
                report.error_class = type(exc).__name__
                report.sqlstate = _sqlstate(exc)
                report.error_detail = _safe_error_detail(exc)
                _step(report, "erp_select1", "FAIL", select_started)
                return EXIT_ERP_SELECT, report
            _step(report, "erp_select1", "PASS", select_started)
    except Exception as exc:
        report.error_stage = report.error_stage or "environment"
        report.error_class = type(exc).__name__
        report.sqlstate = _sqlstate(exc)
        report.error_detail = _safe_error_detail(exc)
        _step(report, report.error_stage, "FAIL", started)
        return _exit_code(report), report
    finally:
        db.set_current_company_id(previous_company_id)
        report.total_elapsed_ms = round((time.perf_counter() - started) * 1000)
    return EXIT_OK, report


def _exit_code(report: PreflightReport) -> int:
    return {
        "environment": EXIT_ENVIRONMENT,
        "config_db": EXIT_CONFIG_DB,
        "company_record": EXIT_COMPANY_RECORD,
        "erp_engine": EXIT_ERP_ENGINE,
        "erp_select1": EXIT_ERP_SELECT,
    }.get(report.error_stage, EXIT_ENVIRONMENT)


def print_report(exit_code: int, report: PreflightReport, *, debug: bool = False) -> None:
    print("[DB PREFLIGHT]")
    print(f"company_id={report.company_id}")
    print(f"python={'project_venv' if report.venv_active else 'non_project_venv'}")
    print(f"project_root={report.project_root}")
    for name in ("environment", "context", "config_db", "company_record", "erp_engine", "erp_select1"):
        step = report.steps.get(name, Step())
        print(f"{name}={step.status} elapsed_ms={step.elapsed_ms}")
    if report.driver:
        print(f"driver={report.driver}")
    if exit_code:
        print(f"stage={report.error_stage}")
        print(f"status=FAIL")
        print(f"error_class={report.error_class}")
        if report.sqlstate:
            print(f"sqlstate={report.sqlstate}")
        if debug and report.error_detail:
            print(f"error_detail={report.error_detail}")
    else:
        print("status=PASS")
    print(f"exit_code={exit_code}")
    print(f"elapsed_ms={report.total_elapsed_ms}")


def main() -> int:
    parser = argparse.ArgumentParser(description="SIMS production-path read-only DB preflight")
    parser.add_argument("--company-id", type=int, required=True)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    exit_code, report = run_preflight(args.company_id, max(1, int(args.timeout)))
    print_report(exit_code, report, debug=args.debug)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
