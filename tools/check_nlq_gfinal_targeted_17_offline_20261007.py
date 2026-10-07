"""Offline regression for the targeted Current Table evidence oracle."""

from __future__ import annotations

import pandas as pd
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.check_nlq_regression_harness_alignment_20261006 import _followup_result_contract


def main() -> int:
    weekday = _followup_result_contract(
        "현재표 요일별 집계", {"df": pd.DataFrame({"요일": ["월요일", "화요일"], "건수": [2, 1]})},
        "발주조회",
    )
    assert weekday["rank_contract_pass"] and "요일" in weekday["result_columns"]

    amount = {"df": pd.DataFrame({"거래처명": ["A", "B"], "거래금액": [200, 100]})}
    purchase = _followup_result_contract(
        "현재표 거래처별 매입금액 TOP 20", amount, "거래명세서 공통 조회",
    )
    assert purchase["rank_contract_pass"] and purchase["metric_order_pass"]
    assert not _followup_result_contract(
        "현재표 거래처별 매입금액 TOP 20", amount, "발주조회",
    )["rank_contract_pass"]

    monthly = _followup_result_contract(
        "현재표 월별 매입금액 TOP 10",
        {"df": pd.DataFrame({"월": ["2026-09", "2026-08"], "거래금액": [200, 100]})},
        "거래명세서 공통 조회",
    )
    assert monthly["rank_contract_pass"] and monthly["metric_order_pass"]
    print("PASS targeted17 offline oracle 4/4")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
