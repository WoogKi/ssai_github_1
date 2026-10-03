"""Offline equivalence gate for the Dashboard exact-product stock strategy."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from unittest.mock import patch

import pandas as pd
from pandas.testing import assert_frame_equal


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services import analytics_sales_trend_service as stock_service


PROFILE = {"dashboard_product_group_list": ["0013:9998"]}
STOCK_CODES = ["00001", "00002"]


def _value_row(code: str, mode: str) -> dict[str, object]:
    qty = float(int(code[1:]) if code[1:].isdigit() else 1)
    unit = 10.0
    prefix = "실" if mode == "real" else "장부"
    return {
        "제품코드": code,
        f"{prefix}재고수량": qty,
        f"{prefix}재고평가단가": unit,
        f"{prefix}재고금액": qty * unit,
    }


def _fake_query_factory(calls: list[tuple[str, dict[str, object]]], mode: str):
    def _fake_query(sql: str, bind: dict[str, object]) -> pd.DataFrame:
        sql_text = str(sql)
        bind_copy = dict(bind)
        calls.append((sql_text, bind_copy))
        if "FROM dbo.Rddbc110 AS T" in sql_text and "FROM dbo.Rddbc120 AS T" in sql_text:
            codes = sorted(str(value) for key, value in bind_copy.items() if key.startswith("cd"))
            return pd.DataFrame(
                {
                    "제품코드": codes,
                    "당월입고수량": [2.0] * len(codes),
                    "당월출고수량": [1.0] * len(codes),
                    "당월재고증감수량": [1.0] * len(codes),
                }
            )
        if "P_SCOPE" in sql_text:
            codes = ["P001", "P002", "OUTSIDE"]
        else:
            codes = sorted(str(value) for key, value in bind_copy.items() if key.startswith("cd"))
        return pd.DataFrame([_value_row(code, mode) for code in codes])

    return _fake_query


def _load(
    codes: list[str],
    *,
    mode: str,
    batch_size: int,
    date_to: str = "20260930",
    policy_date: str = "20260930",
) -> tuple[pd.DataFrame, list[tuple[str, dict[str, object]]]]:
    calls: list[tuple[str, dict[str, object]]] = []
    with patch.dict(os.environ, {"SIMS_STOCK_QUERY_BATCH_SIZE": str(batch_size)}):
        with patch.object(stock_service, "query_to_df", side_effect=_fake_query_factory(calls, mode)):
            frame = stock_service._load_product_current_stock(
                codes,
                stock_mode=mode,
                month_to="202609",
                date_to=date_to,
                policy_date=policy_date,
                stock_cd_list=STOCK_CODES,
                product_scope_params=PROFILE,
            )
    return frame, calls


def _public_values(frame: pd.DataFrame, mode: str) -> pd.DataFrame:
    prefix = "실" if mode == "real" else "장부"
    columns = ["제품코드", f"{prefix}재고수량", f"{prefix}재고평가단가", f"{prefix}재고금액"]
    return frame.loc[:, columns].sort_values("제품코드", kind="stable").reset_index(drop=True)


def _small_exact_gate() -> None:
    codes = [f"P{i:03d}" for i in range(1, 36)]
    frame, calls = _load(codes, mode="real", batch_size=1600)
    assert len(calls) == 1
    sql, bind = calls[0]
    assert "M.Rd21_Physic_Cd IN" in sql
    assert "P_SCOPE" not in sql
    assert sum(key.startswith("cd") for key in bind) == 35
    assert bind["stock_cd_0"] == "00001" and bind["stock_cd_1"] == "00002"
    assert frame.attrs["stock_query_mode"] == "exact_product_single_batch"
    assert frame.attrs["stock_query_batches"] == 1
    assert frame.attrs["stock_profile_scope_applied"] is False
    assert set(frame["제품코드"]) == set(codes)


def _equivalence_gate() -> None:
    codes = ["P001", "P002"]
    for mode in ("real", "book"):
        reference, reference_calls = _load(codes, mode=mode, batch_size=1)
        candidate, candidate_calls = _load(codes, mode=mode, batch_size=1600)
        assert len(reference_calls) == len(candidate_calls) == 1
        assert reference.attrs["stock_query_mode"] == "profile_scope_single_query"
        assert candidate.attrs["stock_query_mode"] == "exact_product_single_batch"
        assert_frame_equal(_public_values(reference, mode), _public_values(candidate, mode), check_dtype=True)


def _empty_and_large_gate() -> None:
    empty, empty_calls = _load([], mode="real", batch_size=10)
    assert empty.empty and empty_calls == []

    codes = [f"P{i:03d}" for i in range(1, 12)]
    large, calls = _load(codes, mode="real", batch_size=10)
    assert len(calls) == 1
    assert "P_SCOPE" in calls[0][0]
    assert not any(key.startswith("cd") for key in calls[0][1])
    assert large.attrs["stock_query_mode"] == "profile_scope_single_query"
    assert large.attrs["stock_query_batches"] == 1


def _hybrid_gate() -> None:
    codes = ["P001", "P002"]
    for mode in ("real", "book"):
        reference, reference_calls = _load(
            codes, mode=mode, batch_size=1, date_to="20260818", policy_date="20260918",
        )
        candidate, candidate_calls = _load(
            codes, mode=mode, batch_size=1600, date_to="20260818", policy_date="20260918",
        )
        assert len(reference_calls) == len(candidate_calls) == 2
        assert_frame_equal(_public_values(reference, mode), _public_values(candidate, mode), check_dtype=True)


def main() -> int:
    _small_exact_gate()
    _equivalence_gate()
    _empty_and_large_gate()
    _hybrid_gate()
    source = Path(stock_service.__file__).read_text(encoding="utf-8")
    loader = source[source.index("def _load_product_current_stock("):source.index("def _month_qty_columns(")]
    assert "retry" not in loader.lower()
    print("dashboard stock exact scope: PASS")
    print("small exact/profile equivalence: PASS")
    print("real/book/location/hybrid/empty/large: PASS")
    print("physical query increase: 0")
    print("retry: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
