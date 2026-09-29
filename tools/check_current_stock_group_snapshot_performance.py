from __future__ import annotations

import ast
import gc
import statistics
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import product_inventory_service as inventory


def _head_reference(function_name: str) -> Callable[..., Any]:
    """Load the task-start implementation as an isolated equivalence oracle."""
    result = subprocess.run(
        ["git", "show", "HEAD:app/services/product_inventory_service.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    tree = ast.parse(result.stdout)
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )
    namespace = dict(vars(inventory))
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<reference>", "exec"), namespace)
    return namespace[function_name]


REFERENCE_PREPARE = _head_reference("_prepare_grouped_df")
REFERENCE_ATTACH = _head_reference("attach_dashboard_frequency_snapshot")
REFERENCE_FILTER = _head_reference("_filter_current_stock_frequency_rows")


def _source_frame(product_count: int, movements_per_group: int) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for product_index in range(product_count):
        product_code = f"P{product_index:05d}"
        for movement_index in range(movements_per_group):
            # One stable inventory group per product; purchase descriptors vary
            # so the descriptor-pair blanking contract is exercised.
            rows.append({
                "group_cd": f"S{product_index % 7:02d}",
                "group_nm": "" if product_index % 113 == 0 else f"재고위치 {product_index % 7}",
                "buy_cd": "" if movement_index % 5 == 0 else f"B{movement_index % 3}",
                "buy_nm": "" if movement_index % 5 == 0 else f"매입처 {movement_index % 3}",
                "order_cd": f"O{product_index % 11}",
                "order_nm": f"발주처 {product_index % 11}",
                "maker_cd": f"M{product_index % 17}",
                "maker_nm": f"제조사 {product_index % 17}",
                "physic_cd": product_code,
                "physic_nm": "" if product_index % 127 == 0 else f"제품 {product_index}",
                "standard": "" if product_index % 131 == 0 else f"{(product_index % 5 + 1) * 10}T",
                "kd_cd": f"KD{product_index:05d}",
                "edi_cd": f"EDI{product_index:05d}",
                "std_cd": f"880{product_index:010d}",
                "pack_unit": "EA",
                "insu_date": "20261001" if product_index % 9 == 0 else "20260101",
                "before_insu_date": "20250101",
                "physic_tax": "과세" if product_index % 2 else "면세",
                "master_unit_cost": 100 + product_index % 23,
                "insu_price": 120 + product_index % 19,
                "before_insu_price": 110 + product_index % 17,
                "acc_unit": 1 if product_index % 7 else 2,
                "old_in_qty": (movement_index % 5) - 2,
                "old_in_amt": ((movement_index % 5) - 2) * 100,
                "old_out_qty": movement_index % 3,
                "now_in_qty": (movement_index % 7) - 3,
                "now_in_amt": ((movement_index % 7) - 3) * 120,
                "now_out_qty": movement_index % 4,
                "now_out_amt": (movement_index % 4) * 90,
                "product_group_nm": "" if product_index % 101 == 0 else f"그룹 {product_index % 4}",
                "product_di_nm": f"구분 {product_index % 3}",
                "product_class_nm": f"분류 {product_index % 5}",
                "special_manage_nm": "" if product_index % 29 else "특수",
            })
    return pd.DataFrame(rows)


def _assert_prepare_equal(source: pd.DataFrame, cfg: dict[str, Any], params: dict[str, Any]) -> None:
    reference_perf: dict[str, Any] = {}
    optimized_perf: dict[str, Any] = {}
    reference = REFERENCE_PREPARE(source, pd.DataFrame(), cfg, params, perf=reference_perf)
    optimized = inventory._prepare_grouped_df(source, pd.DataFrame(), cfg, params, perf=optimized_perf)
    pd.testing.assert_frame_equal(reference, optimized, check_dtype=True)
    if not source.empty:
        for key in (
            "input_copy_ms", "normalize_text_ms", "normalize_numeric_ms", "group_key_prepare_ms",
            "group_aggregate_ms", "descriptor_finalize_ms", "stock_calc_ms", "unit_calc_ms",
            "amount_calc_ms", "group_finalize_ms", "total_group_prepare_ms",
        ):
            if key not in optimized_perf:
                raise AssertionError(f"missing group perf field: {key}")


def _median_ms(function: Callable[[], Any], repeats: int = 3) -> float:
    samples: list[float] = []
    for _ in range(repeats):
        gc.collect()
        started = time.perf_counter()
        function()
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples)


def _projection_rows(product_count: int) -> tuple[dict[str, Any], ...]:
    grades = ("A", "B", "C", "D", "E", "F", "X")
    return tuple({
        "product_code": f"P{index:05d}",
        "frequency_grade": grades[index % len(grades)],
        "occurrence_count_3m": None if index % 13 == 0 else index % 31,
        "profit_grade": "unavailable" if index % 17 == 0 else grades[index % len(grades)],
        "contribution_grade": "unavailable" if index % 19 == 0 else grades[(index + 2) % len(grades)],
    } for index in range(product_count))


def _projection_reader(rows: tuple[dict[str, Any], ...]) -> Callable[..., Any]:
    return lambda **_kwargs: inventory.FrequencyProjectionReadResult(
        status="ready",
        rows=rows,
        generation_no=7,
        checksum="fixture",
    )


def _scope_resolver(**_kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(
        stock_codes=("00001",),
        product_group_codes=(),
        product_di_codes=(),
        product_class_codes=(),
        stock_mode="book",
    )


def _assert_snapshot_equal(frame: pd.DataFrame, params: dict[str, Any], rows: tuple[dict[str, Any], ...]) -> None:
    kwargs = {
        "params": params,
        "date_to": "20260930",
        "profile_scope_resolver": _scope_resolver,
        "projection_reader": _projection_reader(rows),
    }
    reference, reference_meta = REFERENCE_ATTACH(frame, **kwargs)
    optimized, optimized_meta = inventory.attach_dashboard_frequency_snapshot(frame, **kwargs)
    pd.testing.assert_frame_equal(reference, optimized, check_dtype=True)
    for key in (
        "frequency_product_code_normalize_ms", "frequency_projection_read_ms",
        "frequency_projection_frame_build_ms", "frequency_attach_map_ms", "frequency_attach_total_ms",
    ):
        if key not in optimized_meta:
            raise AssertionError(f"missing snapshot perf field: {key}")
    if reference_meta["frequency_missing_product_count"] != optimized_meta["frequency_missing_product_count"]:
        raise AssertionError("snapshot missing-product count changed")


def _assert_snapshot_filter_equal(rows: tuple[dict[str, Any], ...], params: dict[str, Any]) -> None:
    grouped = pd.DataFrame({
        "physic_cd": ["P00000", "P00001", "P00001", "P00013", "", "P99999"],
        "stock_qty": [1, 2, -2, 0, 3, 4],
    })

    def _reference_attach(frame: pd.DataFrame, **kwargs: Any) -> tuple[pd.DataFrame, dict[str, Any]]:
        return REFERENCE_ATTACH(
            frame,
            **kwargs,
            profile_scope_resolver=_scope_resolver,
            projection_reader=_projection_reader(rows),
        )

    def _optimized_attach(frame: pd.DataFrame, **kwargs: Any) -> tuple[pd.DataFrame, dict[str, Any]]:
        return original_optimized_attach(
            frame,
            **kwargs,
            profile_scope_resolver=_scope_resolver,
            projection_reader=_projection_reader(rows),
        )

    reference_globals = REFERENCE_FILTER.__globals__
    original_reference_attach = reference_globals["attach_dashboard_frequency_snapshot"]
    original_optimized_attach = inventory.attach_dashboard_frequency_snapshot
    try:
        reference_globals["attach_dashboard_frequency_snapshot"] = _reference_attach
        inventory.attach_dashboard_frequency_snapshot = _optimized_attach
        reference, _reference_meta = REFERENCE_FILTER(grouped, params=params, date_to="20260930")
        optimized, _optimized_meta = inventory._filter_current_stock_frequency_rows(
            grouped,
            params=params,
            date_to="20260930",
        )
    finally:
        reference_globals["attach_dashboard_frequency_snapshot"] = original_reference_attach
        inventory.attach_dashboard_frequency_snapshot = original_optimized_attach
    pd.testing.assert_frame_equal(reference, optimized, check_dtype=True)


def main() -> None:
    current_cfg = {"group_basis": "stock", "price_mode": "avg", "current_stock_query": True}
    current_params = {"date_to": "20260930"}
    normal_cfg = {"group_basis": "stock", "price_mode": "avg", "current_stock_query": False}

    for source, cfg in (
        (_source_frame(0, 1), current_cfg),
        (_source_frame(1, 1), current_cfg),
        (_source_frame(4, 3), current_cfg),
        (_source_frame(4, 3), normal_cfg),
    ):
        _assert_prepare_equal(source, cfg, current_params)

    large_source = _source_frame(12_000, 13)
    REFERENCE_PREPARE(large_source.head(100), pd.DataFrame(), current_cfg, current_params)
    inventory._prepare_grouped_df(large_source.head(100), pd.DataFrame(), current_cfg, current_params)
    reference_group_ms = _median_ms(
        lambda: REFERENCE_PREPARE(large_source, pd.DataFrame(), current_cfg, current_params),
    )
    optimized_group_ms = _median_ms(
        lambda: inventory._prepare_grouped_df(large_source, pd.DataFrame(), current_cfg, current_params),
    )
    group_improvement = (1.0 - optimized_group_ms / reference_group_ms) * 100.0
    if group_improvement <= 0.0:
        stage_perf: dict[str, Any] = {}
        inventory._prepare_grouped_df(large_source, pd.DataFrame(), current_cfg, current_params, perf=stage_perf)
        raise AssertionError(
            f"group optimization regressed: reference={reference_group_ms:.1f}ms "
            f"optimized={optimized_group_ms:.1f}ms improvement={group_improvement:.1f}% perf={stage_perf}"
        )

    snapshot_rows = _projection_rows(20_000)
    snapshot_codes = [f"P{index % 12_000:05d}" for index in range(16_000)]
    snapshot_codes.extend(["", "P99999", "P00013"])
    snapshot_frame = pd.DataFrame({"제품코드": snapshot_codes})
    base_snapshot_params = {"company_id": "1", "frequency_grade": ""}
    _assert_snapshot_equal(snapshot_frame, base_snapshot_params, snapshot_rows)
    _assert_snapshot_equal(snapshot_frame, {**base_snapshot_params, "frequency_grade": "A"}, snapshot_rows)
    _assert_snapshot_equal(snapshot_frame, {**base_snapshot_params, "frequency_grade": "F"}, snapshot_rows)
    _assert_snapshot_filter_equal(snapshot_rows, base_snapshot_params)
    _assert_snapshot_filter_equal(snapshot_rows, {**base_snapshot_params, "frequency_grade": "A"})
    _assert_snapshot_filter_equal(snapshot_rows, {**base_snapshot_params, "frequency_grade": "F"})

    snapshot_kwargs = {
        "params": base_snapshot_params,
        "date_to": "20260930",
        "profile_scope_resolver": _scope_resolver,
        "projection_reader": _projection_reader(snapshot_rows),
    }
    REFERENCE_ATTACH(snapshot_frame.head(100), **snapshot_kwargs)
    inventory.attach_dashboard_frequency_snapshot(snapshot_frame.head(100), **snapshot_kwargs)
    reference_snapshot_ms = _median_ms(lambda: REFERENCE_ATTACH(snapshot_frame, **snapshot_kwargs))
    optimized_snapshot_ms = _median_ms(lambda: inventory.attach_dashboard_frequency_snapshot(snapshot_frame, **snapshot_kwargs))
    snapshot_improvement = (1.0 - optimized_snapshot_ms / reference_snapshot_ms) * 100.0
    if snapshot_improvement <= 0.0:
        _snapshot_out, snapshot_perf = inventory.attach_dashboard_frequency_snapshot(snapshot_frame, **snapshot_kwargs)
        raise AssertionError(
            f"snapshot optimization regressed: reference={reference_snapshot_ms:.1f}ms "
            f"optimized={optimized_snapshot_ms:.1f}ms improvement={snapshot_improvement:.1f}% perf={snapshot_perf}"
        )

    print("PASS: current-stock group and snapshot exact equivalence")
    print(f"group_input_rows={len(large_source)} group_result_rows=12000")
    print(f"group_reference_median_ms={reference_group_ms:.1f}")
    print(f"group_optimized_median_ms={optimized_group_ms:.1f}")
    print(f"group_improvement_pct={group_improvement:.1f}")
    print(f"snapshot_projection_rows={len(snapshot_rows)} snapshot_current_rows={len(snapshot_frame)}")
    print(f"snapshot_reference_median_ms={reference_snapshot_ms:.1f}")
    print(f"snapshot_optimized_median_ms={optimized_snapshot_ms:.1f}")
    print(f"snapshot_improvement_pct={snapshot_improvement:.1f}")


if __name__ == "__main__":
    main()
