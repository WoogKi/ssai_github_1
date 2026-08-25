"""Static and fixture gate for same-source manufacturer reconstruction equality."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.check_dashboard_sales_purchase_db_multigrain_equality import dashboard_probe_contract
from tools.check_dashboard_sales_purchase_manufacturer_actual_equality import (
    compare_manufacturer_frames,
    build_same_source_manufacturer_equality_sql,
    run_same_source_reconstruction,
)
from tools.check_dashboard_sales_purchase_manufacturer_vendor_relation_gate import _sales_fixture


def main() -> int:
    params, contract = dashboard_probe_contract()
    sql, bind, plan = build_same_source_manufacturer_equality_sql(params)
    assert plan["logical_source_call_count"] == 1
    assert plan["physical_query_count"] == 1
    assert plan["comparison_boundary"].startswith("one SQL result materialized")
    assert contract["period_source_policy"]["use_hybrid"] is False
    assert "CanonicalSales" in sql
    assert "GROUP BY 기준월, 제품코드, 매입처코드" in sql
    assert "UNION" not in sql
    assert bind["stage_stock_cd_0"] == "00001"
    legacy, reconstructed, mismatches = run_same_source_reconstruction(_sales_fixture())
    assert not mismatches
    assert compare_manufacturer_frames(legacy, reconstructed) == []
    print("PASS: dashboard same-source manufacturer reconstruction equality gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
