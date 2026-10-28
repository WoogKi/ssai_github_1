"""DB-free synthetic scaling probe for sparse daily data and backtest."""

from __future__ import annotations

import gc
import json
import tracemalloc
from datetime import date, timedelta
from time import perf_counter

from app.ml.baselines import backtest_models
from app.ml.phase1 import OfflineCalendar, build_daily, prepare_sources


AS_OF = date(2026, 9, 30)
START = AS_OF - timedelta(days=149)
END = AS_OF + timedelta(days=30)
COMPLETE = tuple(START + timedelta(days=offset) for offset in range((END - START).days + 1))


def _measure(call):
    gc.collect()
    tracemalloc.start()
    started = perf_counter()
    result = call()
    elapsed = perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return result, round(elapsed, 4), round(peak / (1024 * 1024), 3)


def _source(product_count: int):
    products = [
        {"company_id": 3, "Rd04_Physic_Cd": f"P{index:06d}", "Rd04_Add_Date": "20200101"}
        for index in range(product_count)
    ]
    rows = []
    for index in range(0, product_count, 10):
        day = AS_OF - timedelta(days=index % 60)
        rows.append({
            "company_id": 3, "Rd12_Out_YyMmDd": day.strftime("%Y%m%d"),
            "Rd12_Ven_Cd": "V", "Rd12_Out_Seq": str(index + 1),
            "Rd12_Physic_Cd": f"P{index:06d}", "Rd12_Io_Gu": "501",
            "Rd12_Quantity": "2", "Rd12_Oquantity": "0", "Rd12_Stock_Cd_Gcode": "S",
            "Rd12_Stock_Cd": "00001", "Rd12_Stock_Apply_Cd": "A", "Rd12_Cost_Apply_Cd": "B",
            "Rd12_Trans_Di": "3", "Rd12_Trans_YyMmDd": day.strftime("%Y%m%d"),
            "Rd12_Trans_Seq": str(index + 1),
            "Rd12_Add_Time": "20260101000000", "Rd12_Mod_Time": "20260101000000",
        })
    return prepare_sources(
        company_id=3, outbound_rows=rows, document_rows=[], vendor_rows=[],
        product_rows=products, standard_rows=[], code_rows=[], saved_io_codes=["501"],
    )


def measure(product_count: int) -> dict[str, object]:
    prepared = _source(product_count)
    daily, daily_seconds, daily_peak_mib = _measure(
        lambda: build_daily(prepared, company_id=3, date_from=START, date_to=END, complete_dates=COMPLETE),
    )
    calendar = OfflineCalendar(date(2026, 9, 1), END, frozenset(), date(2026, 1, 1), "synthetic-v1")
    forecasts, backtest_seconds, backtest_peak_mib = _measure(
        lambda: backtest_models(
            daily, company_id=3, product_codes=prepared.product_codes,
            as_of_dates=(AS_OF - timedelta(days=2), AS_OF - timedelta(days=1), AS_OF),
            calendar=calendar, timestamp_policy_confirmed=True,
            methods=("moving_average", "ewma"),
        ),
    )
    dense, dense_seconds, dense_peak_mib = _measure(
        lambda: {
            (3, product, day): daily.points[3, product, day]
            for product in prepared.product_codes for day in COMPLETE
        },
    )
    return {
        "products": product_count, "days": len(COMPLETE), "source_rows": prepared.source_row_count,
        "forecast_rows": len(forecasts), "observed_daily_points": len(daily.points),
        "dense_daily_points": len(dense), "sparse_build_seconds": daily_seconds,
        "sparse_build_peak_mib": daily_peak_mib, "backtest_seconds": backtest_seconds,
        "backtest_peak_mib": backtest_peak_mib, "dense_reference_seconds": dense_seconds,
        "dense_reference_peak_mib": dense_peak_mib,
        "forecast_checksum": str(sum((row.predicted_quantity or 0 for row in forecasts))),
    }


if __name__ == "__main__":
    print(json.dumps([measure(80), measure(800)], indent=2, ensure_ascii=False))
