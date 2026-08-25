"""Static contract checks for the read-only narrow purchase_facts stage profiler."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_narrow_sales_candidate_service import _queries
from tools.check_dashboard_purchase_facts_stage_profiler import (
    COMPANY8_DEFAULT_PRODUCT_GROUPS,
    build_purchase_facts_stage_sql,
    company8_purchase_stage_contract,
    validate_final_purchase_facts,
)


def main() -> int:
    params, contract = company8_purchase_stage_contract()
    assert contract["default_product_group_scope"] == COMPANY8_DEFAULT_PRODUCT_GROUPS
    queries, plan = build_purchase_facts_stage_sql(params)
    production_queries, _meta = _queries(params)
    production_sql, production_bind = production_queries["purchase_facts"]
    assert list(queries) == ["filtered_sales_products", "purchase_grouped", "final_purchase_facts"]
    assert queries["final_purchase_facts"] == (production_sql, production_bind)
    assert "FilteredProducts AS" in queries["filtered_sales_products"][0]
    assert "FilteredSalesProducts AS" in queries["filtered_sales_products"][0]
    assert "PurchaseGrouped AS" not in queries["filtered_sales_products"][0]
    assert "PurchaseGrouped AS" in queries["purchase_grouped"][0]
    assert "Classified AS" not in queries["purchase_grouped"][0]
    assert plan["final_sql_is_exact_production_sql"] is True

    rows = [
        {"projection_kind": "purchase_month_total", "기준월": "202607", "매입금액": 100, "purchase_source_rows": 0, "purchase_positive_rows": 0, "purchase_nonpositive_rows": 0, "purchase_unclassified_rows": 0, "missing_product_code_rows": 0, "missing_month_rows": 0, "invalid_numeric_rows": 0, "other_excluded_rows": 0},
        {"projection_kind": "purchase_diagnostics", "기준월": "", "매입금액": 0, "purchase_source_rows": 3, "purchase_positive_rows": 2, "purchase_nonpositive_rows": 1, "purchase_unclassified_rows": 0, "missing_product_code_rows": 0, "missing_month_rows": 0, "invalid_numeric_rows": 0, "other_excluded_rows": 0},
    ]
    result = validate_final_purchase_facts(pd.DataFrame(rows))
    assert result["contract_shape"] == "PASS"
    assert result["final_month_total_row_count"] == 1
    assert result["diagnostics"]["purchase_source_rows"] == 3
    print("PASS: purchase_facts stage profiler preserves the exact production final SQL and contract shape")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
