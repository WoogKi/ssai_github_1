"""Offline contracts for the production-path DB preflight tool."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
import importlib.util
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("company_preflight", ROOT / "tools" / "check_company_db_connection.py")
assert SPEC and SPEC.loader
tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tool
SPEC.loader.exec_module(tool)


def main() -> int:
    source = (ROOT / "tools" / "check_company_db_connection.py").read_text(encoding="utf-8")
    failures: list[str] = []
    if "pyodbc.connect" in source or "Driver={" in source or "Server=" in source:
        failures.append("tool must not build or open a direct DB connection")
    if any(token in source.upper() for token in ("INSERT ", "UPDATE ", "DELETE ", "MERGE ", "CREATE ", "ALTER ", "DROP ")):
        failures.append("tool contains a write or DDL SQL token")
    if "db.query_to_df(\"SELECT 1 AS connection_ok\")" not in source:
        failures.append("tool must use the production dataframe helper for SELECT 1")
    if "load_project_env()" not in source:
        failures.append("tool must load the standard project environment")
    expected_exit_codes = {
        "environment": 1,
        "config_db": 2,
        "company_record": 3,
        "erp_engine": 4,
        "erp_select1": 5,
    }
    for stage, expected in expected_exit_codes.items():
        if tool._exit_code(tool.PreflightReport(3, "fixture", "fixture", True, error_stage=stage)) != expected:
            failures.append(f"exit-code contract changed for {stage}")

    context: list[int | None] = []
    current = {"value": None}
    config = SimpleNamespace(db_driver="ODBC Driver Fixture")
    def set_company(value):
        context.append(value)
        current["value"] = value
    def get_engine():
        tool.auth.get_company_db_config(current["value"])
        return object()
    with patch.object(tool.db, "set_current_company_id", side_effect=set_company), \
         patch.object(tool.db, "get_current_company_id", side_effect=lambda: current["value"]), \
         patch.object(tool.db, "get_engine", side_effect=get_engine), \
         patch.object(tool.db, "read_only_request", side_effect=lambda **_: nullcontext({"queries": []})), \
         patch.object(tool.db, "query_to_df", return_value=pd.DataFrame({"connection_ok": [1]})) as select, \
         patch.object(tool.auth, "get_company_db_config", return_value=config) as resolver:
        code, report = tool.run_preflight(3, timeout_seconds=30)
    if code != 0 or report.error_stage or report.steps.get("erp_select1", tool.Step()).status != "PASS":
        failures.append(f"success contract failed: code={code}, report={report}")
    if resolver.call_args_list != [((3,), {})] or select.call_args_list != [(("SELECT 1 AS connection_ok",), {})]:
        failures.append("production resolver or one read-only SELECT contract changed")
    if context != [3, None]:
        failures.append(f"company context restore contract changed: {context}")

    current = {"value": None}
    config_failure_context: list[int | None] = []
    with patch.object(tool.db, "set_current_company_id", side_effect=lambda value: (config_failure_context.append(value), current.__setitem__("value", value))[-1]), \
         patch.object(tool.db, "get_current_company_id", side_effect=lambda: current["value"]), \
         patch.object(tool.db, "get_engine", side_effect=lambda: tool.auth.get_company_db_config(3)), \
         patch.object(tool.auth, "get_company_db_config", side_effect=RuntimeError("config unavailable")):
        code, report = tool.run_preflight(3, timeout_seconds=30)
    if code != tool.EXIT_CONFIG_DB or report.steps.get("erp_engine", tool.Step()).status != "NOT_RUN":
        failures.append(f"config failure must stop before ERP engine: code={code}, report={report}")
    if config_failure_context != [3, None]:
        failures.append(f"config failure context restore changed: {config_failure_context}")

    if failures:
        print("Company DB preflight contract: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Company DB preflight contract: PASS")
    print("- production resolver/engine/helper path, one SELECT, no direct connection/write SQL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
