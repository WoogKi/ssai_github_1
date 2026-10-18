"""Offline guard for the Saved I/O purchase-price contract and company-4 evidence."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.monthly_frequency_aggregate import product_lifecycle_sql  # noqa: E402
from app.services.saved_io_scope import resolve_saved_io_scope  # noqa: E402


def check_sql_contract() -> None:
    selected = ("001", "002", "051", "052", "053", "090", "093", "095", "101", "102", "501")
    scope = resolve_saved_io_scope(selected, confirmed_trade_roles=True)
    assert scope.price_codes == selected[:8]
    assert scope.inbound_return_codes == ("101", "102")
    sql, binds = product_lifecycle_sql(
        stock_codes=("00001", "00247", "00901"), cutoff_date="20260930",
        basis_from="20260701", basis_to="20260930", price_lookback_from="20251001",
        exclude_management_only=True, io_scope=scope,
    )
    price_binds = {value for key, value in binds.items() if key.startswith("lifecycle_price_io_")}
    assert price_binds == set(scope.price_codes)
    assert not price_binds.intersection(scope.inbound_return_codes)
    assert binds["price_lookback_from"] == "20251001"
    assert {binds[f"lifecycle_stock_{i}"] for i in range(3)} == {"00001", "00247", "00901"}
    assert "COALESCE(I.Rd11_Quantity, 0) > 0" in sql
    assert "COALESCE(I.Rd11_Fin_Supply_Price, I.Rd11_Supply_Price, 0) > 0" in sql
    assert "NULLIF(RC.cost_apply_code, '') IS NOT NULL" in sql
    assert "RC.vendor_code = LTRIM(RTRIM(I.Rd11_Ven_Cd))" in sql
    assert "V.Rd03_Cost_Apply_Cd" in sql
    assert "ROW_NUMBER() OVER (" in sql and "I.Rd11_In_Seq DESC" in sql
    assert "Rddbc046 AS ManagementStd" in sql
    assert "dbo.Rddbc210" in sql and "M.Rd21_Io_Gu_Gcode = '0012'" in sql


def check_company4_evidence(path: Path, base_path: Path | None = None) -> None:
    evidence = json.loads(path.read_text(encoding="utf-8"))
    scope, summary = evidence["scope"], evidence["summary"]
    assert scope["company_id"] == 4 and scope["old_manifest"] == 11 and scope["draft_manifest"] == 12
    assert len(evidence["price_changes"]) == summary["changed_price_products"] == 2672
    assert summary["single_valid_6m"] + summary["multiple_valid_6m"] + summary["no_valid_6m"] == 2672
    assert summary["price_spread_90pct_products"] == 4
    assert summary["selected_same_key_negative_products"] == 5
    assert summary["no_latest_12m"] == 1
    assert len(evidence["review_products"]) == summary["review_needed_products"] == 10
    assert all(item["decision"] == "추가 확인" for item in evidence["review_products"])
    assert {item["product_code"] for item in evidence["review_products"] if item["price_spread_90pct"]} == {
        "06056", "06157", "36863", "37650"
    }
    missing = next(item for item in evidence["price_changes"] if item["product_code"] == "38372")
    assert missing["new_price"] is None and missing["new_price_status"] == "unavailable"
    assert missing["valid_candidate_count_6m"] == 0
    assert all(item["selected_io_code"] in scope["normal_inbound_price_codes"]
               for item in evidence["price_changes"] if item["selected_io_code"] is not None)
    assert all(item["old_price"] is not None or item["new_price"] is not None
               for item in evidence["price_changes"])
    if base_path is not None:
        base = json.loads(base_path.read_text(encoding="utf-8"))
        row = next(item for item in base["changes"] if item["product_code"] == "38372")
        assert row["new_price"] is None and row["new_price_status"] == "unavailable"
        assert row["profit_grade"] == row["contribution_grade"] == "unavailable"
        assert row["frequency_grade"] == "E"


if __name__ == "__main__":
    check_sql_contract()
    if len(sys.argv) in (2, 3):
        check_company4_evidence(Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) == 3 else None)
    elif len(sys.argv) != 1:
        raise SystemExit("usage: check_saved_io_purchase_price_contract_20261018.py [review.json [draft-compare.json]]")
    print("saved_io_purchase_price_contract: PASS")
