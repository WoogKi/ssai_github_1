"""Offline contract and microbenchmark for the order Snapshot lightweight path."""

from __future__ import annotations

import hashlib
import json
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot import FrequencyProjectionReadResult  # noqa: E402
from app.services.snapshot_product_information_service import (  # noqa: E402
    _COLUMN_LABELS,
    get_snapshot_product_information_result,
)


def _scope(**_kwargs):
    return SimpleNamespace(
        stock_codes=("00001",), product_group_codes=(), product_di_codes=(),
        product_class_codes=(), stock_mode="real",
    )


def _inputs(row_count: int):
    rows = tuple(
        {
            "product_code": f"{index:05d}",
            "frequency_grade": ("A", "B", "C", "D", "E", "F")[index % 6],
            "profit_grade": ("A", "B", "C", "D", "E", "X")[index % 6],
            "contribution_grade": ("A", "B", "C", "D", "E", "X")[index % 6],
            "lifecycle_status": "new_product" if index % 11 == 0 else "established_product",
            "first_normal_inbound_month": "202501",
            "row_status": "ready",
            "occurrence_count_3m": index % 30,
            "outbound_day_count_3m": index % 20,
            "outbound_customer_count_3m": index % 13,
            "outbound_qty_3m": index,
            "outbound_paid_qty_3m": index,
            "return_event_count_3m": index % 3,
            "return_qty_3m": index % 5,
            "return_supply_amount_3m": Decimal("0.0000"),
            "avg_purchase_unit_cost": Decimal("1234.5678"),
            "purchase_price_basis_month": "202608",
            "purchase_price_status": "ready",
            "avg_sales_unit_price": Decimal("1500.0000"),
            "sales_price_basis_month": "202608",
            "sales_price_status": "ready",
            "estimated_unit_profit": Decimal("265.4322"),
            "estimated_profit_rate": Decimal("0.17695467"),
            "estimated_contribution_amount": Decimal("2654.3220"),
            "profitability_status": "ready",
            # Integrity-only columns must remain available to the repository contract,
            # while the product-information frame deliberately does not materialize them.
            "legacy_frequency_grade": "A",
            "lifecycle_reason": "fixture",
            "data_status": "ready",
        }
        for index in range(row_count)
    )
    projection = FrequencyProjectionReadResult(
        status="ready", rows=rows, manifest_id=91, generation_no=2,
        checksum="a" * 64, authority_status="ready",
        resolution_status="exact_match", contract_version="2.1",
    )
    master = pd.DataFrame({
        "제품코드": [f"{index:05d}" for index in range(row_count)],
        "제품명": [f"검증제품{index}" for index in range(row_count)],
        "제약사": [f"검증제약{index % 20}" for index in range(row_count)],
        "규격": ["10정"] * row_count,
        "보험코드": [f"{640000000 + index}" for index in range(row_count)],
        "제품그룹명": ["전문"] * row_count,
        "구분명": ["보험"] * row_count,
        "제품분류명": ["내복제"] * row_count,
    })
    return projection, master


def _digest(frame: pd.DataFrame) -> str:
    normalized = frame.astype(object).where(pd.notna(frame), None)
    payload = json.dumps(
        normalized.to_dict(orient="split"), ensure_ascii=False, default=str,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _legacy_snapshot_projection(rows) -> pd.DataFrame:
    frame = pd.DataFrame(list(rows))
    available = [column for column in _COLUMN_LABELS if column in frame.columns]
    return frame.loc[:, available].rename(columns=_COLUMN_LABELS).reset_index(drop=True)


def _new_snapshot_projection(rows) -> pd.DataFrame:
    columns = [column for column in _COLUMN_LABELS if rows and column in rows[0]]
    frame = pd.DataFrame.from_records(rows, columns=columns)
    frame.rename(columns=_COLUMN_LABELS, inplace=True)
    return frame


def _run(row_count: int, *, presentation: bool):
    projection, master = _inputs(row_count)
    started = time.perf_counter()
    with patch(
        "app.services.snapshot_product_information_service.get_current_company_id",
        return_value=3,
    ):
        result = get_snapshot_product_information_result(
            {"company_id": 3, "evaluation_month": "202609", "top": 300},
            profile_resolver=_scope,
            projection_reader=lambda **_kwargs: projection,
            master_loader=lambda _params: master,
            apply_viewer_projection=False,
            materialize_presentation=presentation,
        )
    return result, (time.perf_counter() - started) * 1000


def main() -> int:
    order_source = (ROOT / "app/services/order_calculation_service.py").read_text(encoding="utf-8")
    assert "materialize_presentation=False" in order_source
    for row_count in (10_000, 16_404):
        projection, _master = _inputs(row_count)
        old_projection = _legacy_snapshot_projection(projection.rows)
        new_projection = _new_snapshot_projection(projection.rows)
        pd.testing.assert_frame_equal(old_projection, new_projection, check_dtype=True, check_exact=True)
        old_projection_times = []
        new_projection_times = []
        for _ in range(3):
            started = time.perf_counter()
            _legacy_snapshot_projection(projection.rows)
            old_projection_times.append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            _new_snapshot_projection(projection.rows)
            new_projection_times.append((time.perf_counter() - started) * 1000)
        old_projection_ms = statistics.median(old_projection_times)
        new_projection_ms = statistics.median(new_projection_times)

        legacy, _ = _run(row_count, presentation=True)
        lightweight, _ = _run(row_count, presentation=False)
        pd.testing.assert_frame_equal(
            legacy["df"], lightweight["df"], check_dtype=True,
            check_exact=True, check_like=False,
        )
        assert legacy["meta"]["snapshot_checksum"] == lightweight["meta"]["snapshot_checksum"]
        assert legacy["meta"]["snapshot_manifest_id"] == lightweight["meta"]["snapshot_manifest_id"]
        assert legacy["meta"]["source_call_count"] == lightweight["meta"]["source_call_count"] == 1
        assert legacy["meta"]["snapshot_read_call_count"] == lightweight["meta"]["snapshot_read_call_count"] == 1
        assert lightweight["records"] == [] and lightweight["df_display"].empty

        legacy_times = []
        lightweight_times = []
        for _ in range(3):
            _, elapsed = _run(row_count, presentation=True)
            legacy_times.append(elapsed)
            _, elapsed = _run(row_count, presentation=False)
            lightweight_times.append(elapsed)
        legacy_ms = statistics.median(legacy_times)
        new_ms = statistics.median(lightweight_times)
        improvement = (1 - new_ms / legacy_ms) * 100 if legacy_ms else 0.0
        print(
            "PASS product Snapshot lightweight projection "
            f"rows={row_count} repetitions=3 legacy_ms={legacy_ms:.3f} "
            f"new_ms={new_ms:.3f} improvement_pct={improvement:.2f} "
            f"projection_old_ms={old_projection_ms:.3f} projection_new_ms={new_projection_ms:.3f} "
            f"frame_digest={_digest(lightweight['df'])} exact_frame_equality=PASS"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
