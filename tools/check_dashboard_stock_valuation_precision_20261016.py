"""Offline regression for Dashboard valuation prices and shortage amounts."""

from __future__ import annotations

from math import isclose
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_lite_facts import _build_inventory_facts


def main() -> int:
    source = pd.DataFrame([
        {
            "제품코드": "31890", "제품명": "precision_a", "현재재고수량": 0,
            "당월 잔여예상출고수량": 0.02, "당월 예상출고수량": 0.02,
            "부족예상수량": 0.02, "부족예상금액": 1455.12,
            "재고평가단가": 78576.511,
        },
        {
            "제품코드": "29691", "제품명": "precision_b", "현재재고수량": 0,
            "당월 잔여예상출고수량": 0.70, "당월 예상출고수량": 0.70,
            "부족예상수량": 0.70, "부족예상금액": 120444.12,
            "재고평가단가": 173150.951,
        },
        {
            "제품코드": "NEG", "제품명": "negative_price", "현재재고수량": 0,
            "당월 잔여예상출고수량": 1, "당월 예상출고수량": 1,
            "부족예상수량": 1, "부족예상금액": 0,
            "재고평가단가": -10,
        },
        {
            "제품코드": "FALLBACK", "제품명": "missing_price", "현재재고수량": 0,
            "당월 잔여예상출고수량": 2, "당월 예상출고수량": 2,
            "부족예상수량": 2, "부족예상금액": 20,
        },
    ])
    facts = _build_inventory_facts(
        {"df": source}, evaluation_month="202610", policy_date="20261015",
        source_call_count=3,
    )
    rows = {row["product_code"]: row for row in facts["readiness_rows"]}
    for code, unit_price, shortage_qty in (
        ("31890", 78576.511, 0.02),
        ("29691", 173150.951, 0.70),
        ("NEG", 0.0, 1.0),
        ("FALLBACK", 10.0, 2.0),
    ):
        row = rows[code]
        assert isclose(row["stock_valuation_unit_price"], unit_price, rel_tol=0, abs_tol=1e-9), code
        assert isclose(row["shortage_amt"], unit_price * shortage_qty, rel_tol=0, abs_tol=1e-6), code
        assert isclose(row["위험보정부족예상금액"], unit_price * row["위험보정부족예상수량"], rel_tol=0, abs_tol=1e-6), code
    assert len(rows) == len(source)
    print("dashboard stock valuation precision: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
