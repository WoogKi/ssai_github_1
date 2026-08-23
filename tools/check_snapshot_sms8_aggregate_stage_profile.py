from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot_service import build_frequency_snapshot_plan, outbound_base_rows_sql  # noqa: E402
from tools import profile_dashboard_inventory_frequency_snapshot_outbound as profiler  # noqa: E402


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    plan = build_frequency_snapshot_plan(
        company_id=8,
        evaluation_month="202608",
        stock_codes=["00001", "00008", "00013"],
    )
    base_sql, binds = outbound_base_rows_sql(plan)
    stages = profiler._stage_queries(base_sql)
    expected = (
        "base_rows",
        "classified",
        "normal_positive_validation",
        "exact_row_group",
        "event_grain",
        "monthly_day_rollup",
    )
    _assert(tuple(name for name, _sql in stages) == expected, "stage order changed")
    _assert(binds["basis_from"] == "20260501" and binds["basis_to"] == "20260731", "basis mismatch")
    _assert("O.Rd12_Stock_Cd_Gcode = '0018'" in base_sql, "base stock group must be native-column")
    _assert("O.Rd12_Stock_Cd IN (:stock_0, :stock_1, :stock_2)" in base_sql, "base stock scope missing")
    _assert("TRY_CONVERT" not in "\n".join(sql for _name, sql in stages), "legacy SQL function leaked")
    for name, sql in stages:
        upper = sql.upper()
        _assert("INSERT " not in upper and "UPDATE " not in upper and "DELETE " not in upper, f"{name} is not read-only")
        _assert("CREATE " not in upper and "SELECT INTO" not in upper, f"{name} must not create objects")
    class _Connection:
        connection = SimpleNamespace(driver_connection=SimpleNamespace(timeout=0))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Engine:
        def connect(self):
            return _Connection()

    stream_frame = pd.DataFrame([
        {"row_kind": "event", "outbound_date": "20260701", "product_code": "P1", "stock_code": "00001", "outbound_quantity": 2, "mapping_count": 1, "exact_duplicate_row_count": 0},
        {"row_kind": "diagnostics", "outbound_date": "", "product_code": "", "stock_code": "", "outbound_quantity": None, "mapping_count": None, "exact_duplicate_row_count": None,
         "source_row_count": 1, "normal_positive_row_count": 1, "normal_positive_missing_key_row_count": 0,
         "normal_positive_nonintegral_row_count": 0, "normal_nonpositive_row_count": 0,
         "return_positive_row_count": 0, "return_nonpositive_row_count": 0, "other_tcode_row_count": 0},
    ])
    originals = (profiler.get_engine, profiler.get_current_company_id, profiler.set_current_company_id, profiler.pd.read_sql_query)
    try:
        profiler.get_engine = lambda: _Engine()
        profiler.get_current_company_id = lambda: None
        profiler.set_current_company_id = lambda _company_id: None
        profiler.pd.read_sql_query = lambda *_args, **_kwargs: iter([stream_frame])
        stream_result = profiler.profile(
            company_id=8, evaluation_month="202608", stock_codes=["00001", "00008", "00013"],
            timeout_seconds=120, event_stream_only=True,
        )
    finally:
        profiler.get_engine, profiler.get_current_company_id, profiler.set_current_company_id, profiler.pd.read_sql_query = originals
    _assert(stream_result["connection_status"] == "ok", "event-stream static connection path failed")
    _assert(stream_result["stages"][0]["status"] == "ok", "event-stream static execution failed")
    _assert(stream_result["stages"][0]["metrics"]["normal_positive_accepted_row_count"] == 1, "event-stream diagnostics changed")
    print("PASS snapshot aggregate stage profile static contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
