from __future__ import annotations

import gc
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import product_inventory_service as inventory


def _row(
    product: str,
    location: str,
    *,
    stock_qty: float = 1,
    insu_amt: float = 100,
    blank: bool = False,
) -> dict[str, Any]:
    return {
        "group_cd": location,
        "group_nm": f"위치 {location}",
        "physic_cd": product,
        "physic_nm": "" if blank else f"제품 {product}",
        "standard": "" if blank else "30T",
        "kd_cd": "" if blank else f"KD{product}",
        "edi_cd": "" if blank else f"EDI{product}",
        "std_cd": "" if blank else "8801234567890",
        "product_group_nm": "" if blank else "제품그룹",
        "product_di_nm": "" if blank else "제품구분",
        "product_class_nm": "" if blank else "제품분류",
        "order_cd": "" if blank else "ORDER",
        "order_nm": "" if blank else "발주처",
        "maker_cd": "" if blank else "MAKER",
        "maker_nm": "" if blank else "제조사",
        "pack_unit": "" if blank else "EA",
        "stock_qty": stock_qty,
        "curr_insu_unit": 100,
        "insu_amt": insu_amt,
    }


def _reference_frames(
    grp: pd.DataFrame,
    cfg: dict[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Reference implementation retained from the pre-vectorization contract."""
    if grp is None or grp.empty:
        empty = pd.DataFrame(columns=inventory._CURRENT_STOCK_DISPLAY_COLUMNS)
        return empty, empty.copy(), {
            "row_count": 0,
            "detail_count": 0,
            "sum_carry_qty": 0.0,
            "sum_in_qty": 0.0,
            "sum_out_qty": 0.0,
            "sum_stock_qty": 0.0,
            "sum_stock_amt": 0.0,
            "sum_insu_amt": 0.0,
            "group_label": "재고위치",
            "product_info": {},
            "current_stock_summary": {"product_count": 0, "maker_name": ""},
        }

    work = grp.copy()
    location_names = dict(cfg.get("stock_location_name_map") or {})
    work["재고위치코드"] = work["group_cd"].fillna("").astype(str).str.strip()
    work["재고위치명"] = work["재고위치코드"].map(location_names).fillna(work["group_nm"])
    aliases = {
        "제품코드": "physic_cd", "제품명": "physic_nm", "규격": "standard",
        "KD코드": "kd_cd", "EDI코드": "edi_cd", "표준코드": "std_cd",
        "제품그룹명": "product_group_nm", "제품구분명": "product_di_nm",
        "제품분류명": "product_class_nm", "발주처코드": "order_cd",
        "발주처명": "order_nm", "제조사코드": "maker_cd", "제조사명": "maker_nm",
        "포장단위": "pack_unit",
    }
    for display_col, source_col in aliases.items():
        work[display_col] = work[source_col]
    work["재고수량"] = inventory._to_num(work["stock_qty"])
    work["현보험약가"] = inventory._to_num(work["curr_insu_unit"]).round(2)
    work["보험금액"] = inventory._round_money(work["insu_amt"])
    if inventory._FREQUENCY_GRADE_COLUMN in work.columns:
        work["출고빈도"] = work[inventory._FREQUENCY_GRADE_COLUMN].fillna("").astype(str).str.strip()
    if inventory._FREQUENCY_COUNT_COLUMN in work.columns:
        work["출고횟수"] = pd.to_numeric(work[inventory._FREQUENCY_COUNT_COLUMN], errors="coerce")
    if inventory._PROFIT_GRADE_COLUMN in work.columns:
        work[inventory._PROFIT_GRADE_COLUMN] = work[inventory._PROFIT_GRADE_COLUMN].fillna("").astype(str).str.strip()
    if inventory._CONTRIBUTION_GRADE_COLUMN in work.columns:
        work[inventory._CONTRIBUTION_GRADE_COLUMN] = work[inventory._CONTRIBUTION_GRADE_COLUMN].fillna("").astype(str).str.strip()

    display_columns = inventory._current_stock_display_columns(work)
    product_key_columns = ["제품코드", "제품명", "규격"]
    work["_현재고제품키"] = (
        work[product_key_columns].fillna("").astype(str).agg("\x1f".join, axis=1)
    )
    product_keys = work["_현재고제품키"].drop_duplicates().tolist()
    product_serials = {key: index + 1 for index, key in enumerate(product_keys)}
    work["순번"] = work["_현재고제품키"].map(product_serials).astype(int)

    detail_count = int(len(work))
    sum_stock_qty = float(pd.to_numeric(work["재고수량"], errors="coerce").fillna(0).sum())
    sum_insu_amt = float(pd.to_numeric(work["보험금액"], errors="coerce").fillna(0).sum())
    display_parts: list[pd.DataFrame] = []
    source_parts: list[pd.DataFrame] = []
    for _, product_rows in work.groupby("_현재고제품키", sort=False, dropna=False):
        source_details = product_rows[display_columns].copy()
        source_parts.append(source_details)
        details = source_details.copy()
        if len(details) > 1:
            repeated_text_columns = [
                column for column in display_columns
                if column not in inventory._DISPLAY_NUMERIC_COLS_260
                and column not in {"순번", "재고위치명", "재고위치코드"}
            ]
            details.loc[details.index[1:], repeated_text_columns] = ""
            details["순번"] = pd.to_numeric(details["순번"], errors="coerce").astype("Int64")
            details.loc[details.index[1:], "순번"] = pd.NA
            details.loc[details.index[1:], ["현보험약가", "보험금액"]] = float("nan")
        display_parts.append(details)
        if len(product_rows) <= 1:
            continue
        first = source_details.iloc[0]
        source_subtotal = {column: first.get(column, "") for column in display_columns}
        source_subtotal["재고위치코드"] = ""
        source_subtotal["재고위치명"] = "제품 합계"
        source_subtotal["재고수량"] = float(pd.to_numeric(product_rows["재고수량"], errors="coerce").fillna(0).sum())
        source_subtotal["보험금액"] = float(pd.to_numeric(product_rows["보험금액"], errors="coerce").fillna(0).sum())
        source_parts.append(pd.DataFrame([source_subtotal], columns=display_columns))
        subtotal = {column: "" for column in display_columns}
        subtotal["재고위치명"] = "제품 합계"
        subtotal["재고수량"] = source_subtotal["재고수량"]
        subtotal["보험금액"] = source_subtotal["보험금액"]
        display_parts.append(pd.DataFrame([subtotal], columns=display_columns))

    out = inventory._finalize_display_df_260(pd.concat(display_parts, ignore_index=True))
    source_out = inventory._finalize_display_df_260(pd.concat(source_parts, ignore_index=True))
    product_count = int(len(product_keys))
    product_info = {} if product_count > 1 else {
        "제품코드": inventory.clean_text(work.iloc[0].get("제품코드")),
        "제품명": inventory.clean_text(work.iloc[0].get("제품명")),
        "규격": inventory.clean_text(work.iloc[0].get("규격")),
        "현보험약가": float(pd.to_numeric(work.iloc[0].get("현보험약가"), errors="coerce") or 0.0),
        "보험코드": inventory.clean_text(work.iloc[0].get("KD코드")),
        "표준코드": inventory.clean_text(work.iloc[0].get("표준코드")),
        "제조사명": inventory.clean_text(work.iloc[0].get("제조사명")),
        "발주처명": inventory.clean_text(work.iloc[0].get("발주처명")),
        "제품그룹명": inventory.clean_text(work.iloc[0].get("제품그룹명")),
        "제품구분명": inventory.clean_text(work.iloc[0].get("제품구분명")),
        "제품분류명": inventory.clean_text(work.iloc[0].get("제품분류명")),
    }
    return out, source_out, {
        "row_count": int(len(out)),
        "detail_count": detail_count,
        "sum_carry_qty": 0.0,
        "sum_in_qty": 0.0,
        "sum_out_qty": 0.0,
        "sum_stock_qty": sum_stock_qty,
        "sum_stock_amt": 0.0,
        "sum_insu_amt": sum_insu_amt,
        "group_label": "재고위치",
        "product_info": product_info,
        "current_stock_query": True,
    }


def _assert_equivalent(frame: pd.DataFrame, name: str) -> None:
    cfg = {
        "stock_location_name_map": {
            "00001": "본사 창고", "00002": "지점 창고", "00003": "보조 창고",
        }
    }
    expected_display, expected_source, expected_meta = _reference_frames(frame, cfg)
    actual_display, actual_source, actual_meta = inventory._build_current_stock_table_frames(frame, cfg)
    pd.testing.assert_frame_equal(actual_display, expected_display, check_dtype=True)
    pd.testing.assert_frame_equal(actual_source, expected_source, check_dtype=True)
    for key in (
        "row_count", "detail_count", "sum_stock_qty", "sum_insu_amt", "product_info",
    ):
        if actual_meta.get(key) != expected_meta.get(key):
            raise AssertionError(f"{name}: meta mismatch for {key}: {actual_meta.get(key)!r}")


def _correctness_checks() -> None:
    fixtures = {
        "single product/location": pd.DataFrame([_row("00001", "00001")]),
        "single product/multiple locations": pd.DataFrame([
            _row("00001", "00001", stock_qty=-2, insu_amt=-200),
            _row("00001", "00002", stock_qty=0, insu_amt=0),
            _row("00001", "00003", stock_qty=3, insu_amt=300),
        ]),
        "mixed products/locations/blanks": pd.DataFrame([
            _row("00002", "00002", stock_qty=0, insu_amt=0),
            _row("00001", "00001", stock_qty=-1, insu_amt=-100),
            _row("00002", "00001", stock_qty=2, insu_amt=200),
            _row("00003", "00003", stock_qty=5, insu_amt=500, blank=True),
            _row("00001", "00003", stock_qty=1, insu_amt=100),
        ]),
    }
    grade_frame = pd.DataFrame([
        _row("00004", "00001"), _row("00004", "00002"), _row("00005", "00003"),
    ])
    grade_frame[inventory._FREQUENCY_GRADE_COLUMN] = ["A", "A", "F"]
    grade_frame[inventory._FREQUENCY_COUNT_COLUMN] = [10, 10, 0]
    grade_frame[inventory._PROFIT_GRADE_COLUMN] = ["B", "B", "C"]
    grade_frame[inventory._CONTRIBUTION_GRADE_COLUMN] = ["A", "A", "D"]
    fixtures["snapshot grades"] = grade_frame
    for name, frame in fixtures.items():
        _assert_equivalent(frame, name)


def _large_fixture(product_count: int = 4000, locations_per_product: int = 3) -> pd.DataFrame:
    rows = [
        _row(
            f"{product_index:05d}",
            f"{location_index + 1:05d}",
            stock_qty=((product_index + location_index) % 11) - 5,
            insu_amt=(((product_index + location_index) % 11) - 5) * 100,
            blank=(product_index % 97 == 0),
        )
        for product_index in range(product_count)
        for location_index in range(locations_per_product)
    ]
    frame = pd.DataFrame(rows)
    frame[inventory._FREQUENCY_GRADE_COLUMN] = "A"
    frame[inventory._FREQUENCY_COUNT_COLUMN] = 12
    frame[inventory._PROFIT_GRADE_COLUMN] = "B"
    frame[inventory._CONTRIBUTION_GRADE_COLUMN] = "C"
    return frame


def _median_ms(func, frame: pd.DataFrame, cfg: dict[str, Any], repeats: int) -> float:
    samples: list[float] = []
    for _ in range(repeats):
        gc.collect()
        started = time.perf_counter()
        func(frame, cfg)
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples)


def main() -> None:
    _correctness_checks()
    cfg = {"stock_location_name_map": {}}
    warmup = _large_fixture(product_count=20)
    _reference_frames(warmup, cfg)
    inventory._build_current_stock_table_frames(warmup, cfg)

    large = _large_fixture()
    reference_ms = _median_ms(_reference_frames, large, cfg, repeats=3)
    optimized_ms = _median_ms(
        inventory._build_current_stock_table_frames, large, cfg, repeats=5
    )
    improvement_pct = (1.0 - optimized_ms / reference_ms) * 100.0
    if improvement_pct < 50.0:
        raise AssertionError(
            f"performance improvement below 50%: reference={reference_ms:.1f}ms "
            f"optimized={optimized_ms:.1f}ms improvement={improvement_pct:.1f}%"
        )

    print("PASS: current-stock frame exact equivalence")
    print(f"fixture_rows={len(large)} product_count=4000")
    print(f"reference_median_ms={reference_ms:.1f}")
    print(f"optimized_median_ms={optimized_ms:.1f}")
    print(f"improvement_pct={improvement_pct:.1f}")


if __name__ == "__main__":
    main()
