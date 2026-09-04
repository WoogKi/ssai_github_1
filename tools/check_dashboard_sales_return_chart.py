"""Offline contract gate for the Dashboard sales-return chart series."""
from __future__ import annotations

import re
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_lite_facts import (
    _build_sales_facts,
    _filter_sales_source_for_dashboard,
    _monthly_sales_actuals_from_source,
    _monthly_sales_returns_from_source,
)
from app.sims.views.dashboard_lite import _build_sales_bar_chart, build_dashboard_lite_chat_snapshot


def _assert_legacy_monthly_projection_preserves_sales_returns() -> None:
    """Exercise the legacy exact-selected projection without a DB connection."""
    import app.services.analytics_sales_trend_service as sales_trend

    raw = pd.DataFrame([{
        "기준월": "202603",
        "제품코드": "fixture-product",
        "매입처코드": "fixture-vendor",
        "재고적용처코드": "fixture-stock-vendor",
        "출고수량": 10.0,
        "출고할증수량": 0.0,
        "매출공급가액": 900.0,
        "매출세액": 90.0,
        "매출합계": 990.0,
        "매출반품금액": 125.0,
        "집계건수": 1,
        "매입처수": 1,
        "분석자료원": "월집계-장부재고(Rddbc220)",
    }])
    product_master = pd.DataFrame([{
        "제품코드": "fixture-product",
        "제품명": "fixture",
        "규격": "",
        "제조사코드": "",
        "제조사명": "",
        "제품그룹Gcode": "",
        "제품그룹코드": "",
        "제품그룹명": "",
        "제품구분Gcode": "",
        "제품구분코드": "",
        "제품구분명": "",
        "제품분류Gcode": "",
        "제품분류코드": "",
        "제품분류명": "",
    }])
    vendor_master = pd.DataFrame([{"거래처코드": "fixture-vendor", "거래처명": "fixture"}])
    original_query = sales_trend.query_to_df
    original_product = sales_trend._load_monthly_product_master_for_codes
    original_vendor = sales_trend._load_monthly_vendor_names_for_codes
    from app.services import dashboard_narrow_sales_candidate_service as narrow_candidate

    original_candidate_gate = narrow_candidate.can_use_dashboard_narrow_sales_candidate
    sales_trend.query_to_df = lambda _sql, _params: raw.copy()
    sales_trend._load_monthly_product_master_for_codes = lambda _codes: product_master.copy()
    sales_trend._load_monthly_vendor_names_for_codes = lambda _codes: vendor_master.copy()
    narrow_candidate.can_use_dashboard_narrow_sales_candidate = lambda _params: (False, "legacy_fixture")
    params = {
        "company_id": "fixture",
        "month_from": "202603",
        "month_to": "202603",
        "date_from": "20260301",
        "date_to": "20260331",
        "evaluation_month": "202603",
        "io_gu_list": ["501", "601"],
        "_require_company_io": True,
    }
    try:
        projected = sales_trend._get_sales_trend_monthly_df_fast(
            params,
            source_mode="monthly_book",
        )
        bundle_raw = raw.assign(_dashboard_source_kind="sales")
        sales_trend.query_to_df = lambda _sql, _params: bundle_raw.copy()
        bundle = sales_trend.get_dashboard_sales_source_bundle(params)
    finally:
        sales_trend.query_to_df = original_query
        sales_trend._load_monthly_product_master_for_codes = original_product
        sales_trend._load_monthly_vendor_names_for_codes = original_vendor
        narrow_candidate.can_use_dashboard_narrow_sales_candidate = original_candidate_gate

    assert "매출반품금액" in projected.columns
    assert float(projected.iloc[0]["매출반품금액"]) == 125.0
    assert _monthly_sales_returns_from_source(projected) == [
        {"period": "2026-03", "period_sort": "202603", "magnitude": 125.0}
    ]
    bundle_sales = bundle["sales_df"]
    assert "매출반품금액" in bundle_sales.columns
    legacy_returns = _monthly_sales_returns_from_source(bundle_sales)
    assert legacy_returns == [
        {"period": "2026-03", "period_sort": "202603", "magnitude": 125.0}
    ]
    legacy_facts = _build_sales_facts(
        {"df": pd.DataFrame([{"제약사명": "fixture", "2026-03 매출": 990.0}]), "meta": {"evaluation_month": "202603"}},
        history_sales_returns=legacy_returns,
        evaluation_month="202603",
        policy_date="20260331",
        today=date(2026, 3, 31),
    )
    assert any(
        row.get("kind") == "매출반품" and row.get("value") == -125.0 and row.get("return_magnitude") == 125.0
        for row in legacy_facts["chart_rows"]
    )


def _dataset_rows(spec: dict) -> list[dict]:
    rows: list[dict] = []
    for values in (spec.get("datasets") or {}).values():
        if isinstance(values, list):
            rows.extend(item for item in values if isinstance(item, dict))
    return rows


def _chart_data_rows(spec: dict, chart: dict) -> list[dict]:
    data_name = str((chart.get("data") or {}).get("name") or "")
    rows = (spec.get("datasets") or {}).get(data_name) or []
    return [row for row in rows if isinstance(row, dict)]


def _first_bar_layer(chart: dict) -> dict:
    mark = chart.get("mark") or {}
    if mark.get("type") == "bar":
        return chart
    for layer in chart.get("layer") or []:
        if isinstance(layer, dict):
            found = _first_bar_layer(layer)
            if found:
                return found
    return {}


def _assert_contract_violation(callback: Callable[[], object]) -> None:
    try:
        callback()
    except ValueError as exc:
        assert "sales-return magnitude" in str(exc)
        return
    raise AssertionError("negative sales-return magnitude was not rejected")


def _return_stage_stats(df: pd.DataFrame) -> dict[str, float | int]:
    """Keep the source-to-chart magnitude boundary observable in this offline gate."""
    values = pd.to_numeric(df["매출반품금액"], errors="raise")
    return {
        "rows": len(values),
        "min": float(values.min()) if not values.empty else 0.0,
        "max": float(values.max()) if not values.empty else 0.0,
        "negative_rows": int(values.lt(0).sum()),
    }


def _assert_full_and_narrow_return_stages() -> None:
    """Full/hybrid and narrow scopes must deliver magnitudes before facts run."""
    import app.services.analytics_sales_trend_service as sales_trend

    monthly = pd.DataFrame([
        {"기준월": "202603", "매출반품금액": 100.0, "매출합계": 900.0, "제품코드": "p1"},
        {"기준월": "202604", "매출반품금액": 200.0, "매출합계": 800.0, "제품코드": "p1"},
        {"기준월": "202609", "매출반품금액": 999.0, "매출합계": 700.0, "제품코드": "p1"},
    ])
    detail = pd.DataFrame([
        {"기준월": "202609", "매출반품금액": 30.0, "매출합계": 600.0, "제품코드": "p1"},
    ])
    original_detail = sales_trend.get_sales_trend_detail_df
    sales_trend.get_sales_trend_detail_df = lambda _params: detail.copy()
    try:
        full_hybrid = sales_trend._apply_monthly_current_detail_mix(
            monthly,
            {"date_to": "20260930"},
            source_mode="monthly_book",
            source_policy={"use_hybrid": True, "effective_date_to": "20260930"},
        )
    finally:
        sales_trend.get_sales_trend_detail_df = original_detail

    params = {"product_group_list": ["fixture-group"]}
    full_hybrid["제품그룹Gcode"] = "0013"
    full_hybrid["제품그룹코드"] = "fixture-group"
    full_hybrid["제품그룹명"] = "fixture"
    filtered = _filter_sales_source_for_dashboard(full_hybrid, params)
    ranged = filtered.loc[filtered["기준월"].between("202603", "202609")].copy()
    narrow = pd.concat([monthly.iloc[:2], detail], ignore_index=True)

    stages = {
        "full_hybrid_source_projection": full_hybrid,
        "full_hybrid_product_filter": filtered,
        "full_hybrid_range_slice": ranged,
        "full_hybrid_history_aggregate_input": ranged,
        "narrow_source_projection": narrow,
        "narrow_history_aggregate_input": narrow,
    }
    for stage, frame in stages.items():
        stats = _return_stage_stats(frame)
        assert stats["negative_rows"] == 0, f"{stage}: {stats}"
        assert stats["min"] >= 0, f"{stage}: {stats}"

    full_returns = _monthly_sales_returns_from_source(ranged)
    assert full_returns == [
        {"period": "2026-03", "period_sort": "202603", "magnitude": 100.0},
        {"period": "2026-04", "period_sort": "202604", "magnitude": 200.0},
        {"period": "2026-09", "period_sort": "202609", "magnitude": 30.0},
    ]
    assert _monthly_sales_returns_from_source(narrow) == full_returns


def main() -> int:
    # The three representations already contain signed net sales and the
    # positive return magnitude.  The Dashboard may only reuse these facts.
    detail = pd.DataFrame([
        {"기준월": "202601", "매출합계": 100.0, "매출반품금액": 0.0},
        {"기준월": "202602", "매출합계": 75.0, "매출반품금액": 25.0},
        {"기준월": "202603", "매출합계": -20.0, "매출반품금액": 20.0},
    ])
    monthly = detail.copy()
    narrow_monthly = detail.copy()
    expected_returns = [
        {"period": "2026-01", "period_sort": "202601", "magnitude": 0.0},
        {"period": "2026-02", "period_sort": "202602", "magnitude": 25.0},
        {"period": "2026-03", "period_sort": "202603", "magnitude": 20.0},
    ]
    assert _monthly_sales_returns_from_source(detail) == expected_returns
    assert _monthly_sales_returns_from_source(monthly) == expected_returns
    assert _monthly_sales_returns_from_source(narrow_monthly) == expected_returns
    _assert_contract_violation(lambda: _monthly_sales_returns_from_source(pd.DataFrame([
            {"기준월": "202601", "매출합계": 10.0, "매출반품금액": -1.0},
        ])))
    assert _monthly_sales_actuals_from_source(detail) == _monthly_sales_actuals_from_source(monthly)
    assert _monthly_sales_actuals_from_source(detail) == _monthly_sales_actuals_from_source(narrow_monthly)
    _assert_legacy_monthly_projection_preserves_sales_returns()
    _assert_full_and_narrow_return_stages()

    summary = pd.DataFrame([{
        "제약사명": "fixture",
        "2026-01 매출": 100.0,
        "2026-02 매출": 75.0,
        "2026-03 매출": -20.0,
        "당월 현재매출": 40.0,
        "당월 예상매출": 60.0,
    }])
    baseline = _build_sales_facts(
        {"df": summary, "meta": {"evaluation_month": "202604"}},
        evaluation_month="202604",
        policy_date="20260415",
        today=date(2026, 4, 15),
    )
    with_returns = _build_sales_facts(
        {"df": summary, "meta": {"evaluation_month": "202604"}},
        history_sales_returns=expected_returns,
        evaluation_month="202604",
        policy_date="20260415",
        today=date(2026, 4, 15),
    )
    baseline_actuals = {
        row["period_sort"]: row["value"]
        for row in baseline["chart_rows"]
        if row["kind"] in {"완료월 실제", "당월 현재(부분월)"}
    }
    returned_actuals = {
        row["period_sort"]: row["value"]
        for row in with_returns["chart_rows"]
        if row["kind"] in {"완료월 실제", "당월 현재(부분월)"}
    }
    assert baseline_actuals == returned_actuals
    returns = {
        row["period_sort"]: (row["value"], row["return_magnitude"])
        for row in with_returns["chart_rows"]
        if row["kind"] == "매출반품"
    }
    assert returns == {
        "202601": (0.0, 0.0),
        "202602": (-25.0, 25.0),
        "202603": (-20.0, 20.0),
        "202604": (0.0, 0.0),
    }
    _assert_contract_violation(lambda: _build_sales_facts(
            {"df": summary, "meta": {"evaluation_month": "202604"}},
            history_sales_returns=[{"period": "2026-01", "period_sort": "202601", "magnitude": -1.0}],
            evaluation_month="202604",
            policy_date="20260415",
            today=date(2026, 4, 15),
        ))

    spec = _build_sales_bar_chart({"sales": with_returns}).to_dict()
    rendered_returns = [row for row in _dataset_rows(spec) if row.get("series") == "매출반품"]
    assert any(row.get("display_value") == -25.0 and row.get("return_magnitude") == 25.0 for row in rendered_returns)
    assert any(row.get("display_value") == -20.0 and row.get("return_magnitude") == 20.0 for row in rendered_returns)
    # Sales and returns use asymmetrical visual areas but meet at the same zero.
    # The lower area retains its actual negative domain, so a small return bar
    # is not compressed by the substantially larger top sales scale.
    assert spec.get("resolve", {}).get("scale") == {"x": "shared", "y": "independent"}
    return_panel = (spec.get("vconcat") or [{}, {}])[1]
    assert return_panel.get("height") == 118
    return_bar = (return_panel.get("layer") or [{}])[0]
    return_y = (return_bar.get("encoding") or {}).get("y") or {}
    return_domain = (return_y.get("scale") or {}).get("domain") or []
    assert len(return_domain) == 2 and float(return_domain[0]) < 0 and float(return_domain[1]) == 0.0
    assert return_y.get("field") == "display_value"
    assert (return_y.get("axis") or {}).get("labelExpr") == "format(datum.value, ',.0f')"
    return_tooltips = (return_bar.get("encoding") or {}).get("tooltip") or []
    assert any(item.get("field") == "return_magnitude" for item in return_tooltips)
    assert any(item.get("field") == "return_rate_pct" for item in return_tooltips)
    return_offset = (return_bar.get("encoding") or {}).get("xOffset") or {}
    assert return_offset.get("field") == "return_anchor"
    assert (return_bar.get("encoding") or {}).get("x", {}).get("bandPosition") == 0.5
    assert return_offset.get("bandPosition") == 0.5
    return_bar_rows = _chart_data_rows(spec, return_bar)
    return_rate_by_period = {row.get("period_sort"): row for row in return_bar_rows}
    assert all(row.get("return_anchor") == "실제매출" for row in return_bar_rows)
    assert return_rate_by_period["202602"]["return_rate_label"] == "33.3%"
    assert return_rate_by_period["202603"]["return_rate_label"] == "100.0%"
    assert return_rate_by_period["202601"]["return_rate_label"] == "0.0%"
    assert return_rate_by_period["202602"]["return_label_placement"] == "inside"
    assert return_rate_by_period["202602"]["label_y"] == return_rate_by_period["202602"]["display_value"] / 2.0
    assert return_rate_by_period["202602"]["display_value"] < return_rate_by_period["202602"]["label_y"] < 0.0
    return_layers = return_panel.get("layer") or []
    assert (return_layers[2].get("mark") or {}).get("color") == "#111827"
    assert (return_layers[2].get("mark") or {}).get("fontSize") == 12
    assert (return_layers[3].get("mark") or {}).get("color") == "#111827"
    assert (return_layers[3].get("mark") or {}).get("fontSize") == 11
    inside_label_y = (return_layers[2].get("encoding") or {}).get("y") or {}
    outside_label_y = (return_layers[3].get("encoding") or {}).get("y") or {}
    assert inside_label_y.get("field") == "display_value"
    assert outside_label_y.get("field") == "display_value"
    assert (return_panel.get("resolve") or {}).get("scale") == {"x": "shared", "y": "shared"}
    for label_layer in (return_layers[2], return_layers[3]):
        label_encoding = label_layer.get("encoding") or {}
        assert label_encoding.get("x") == (return_bar.get("encoding") or {}).get("x")
        assert label_encoding.get("xOffset") == (return_bar.get("encoding") or {}).get("xOffset")
        assert (label_layer.get("mark") or {}).get("align") == "center"
    inside_label_rows = _chart_data_rows(spec, return_layers[2])
    assert any(
        row.get("return_rate_label") == "33.3%"
        and row.get("display_value") == row.get("label_y")
        and -25.0 < row.get("display_value", 0.0) < 0.0
        for row in inside_label_rows
    )
    outside_label_rows = _chart_data_rows(spec, return_layers[3])
    assert all(row.get("return_anchor") == "실제매출" for row in outside_label_rows)
    top_bar = _first_bar_layer((spec.get("vconcat") or [{}])[0])
    assert "size" not in (top_bar.get("mark") or {})
    assert "size" not in (return_bar.get("mark") or {})
    top_offset = (top_bar.get("encoding") or {}).get("xOffset") or {}
    assert return_offset.get("scale") == top_offset.get("scale")

    alignment_periods = [f"20260{month}" for month in range(3, 10)]
    alignment_rows: list[dict] = []
    for month_index, period_sort in enumerate(alignment_periods, start=1):
        period = f"{period_sort[:4]}-{period_sort[4:6]}"
        alignment_rows.extend([
            {
                "period": period,
                "period_sort": period_sort,
                "kind": "완료월 실제",
                "value": 0.0 if period_sort == "202604" else 1_000.0,
            },
            {
                "period": period,
                "period_sort": period_sort,
                "kind": "매출반품",
                "value": -float(month_index),
                "return_magnitude": float(month_index),
            },
        ])
    alignment_spec = _build_sales_bar_chart({"sales": {"chart_rows": alignment_rows}}).to_dict()
    alignment_return_panel = (alignment_spec.get("vconcat") or [{}, {}])[1]
    alignment_return_bar = (alignment_return_panel.get("layer") or [{}])[0]
    alignment_return_rows = _chart_data_rows(alignment_spec, alignment_return_bar)
    assert len(alignment_return_rows) == 7
    assert all(row.get("series") == "매출반품" for row in alignment_return_rows)
    assert all(row.get("return_anchor") == "실제매출" for row in alignment_return_rows)
    assert {row.get("period_sort") for row in alignment_return_rows} == set(alignment_periods)
    assert next(row for row in alignment_return_rows if row["period_sort"] == "202603")["return_rate_label"] == "0.1%"
    assert next(row for row in alignment_return_rows if row["period_sort"] == "202603")["return_label_placement"] == "outside"
    assert next(row for row in alignment_return_rows if row["period_sort"] == "202604")["return_rate_label"] == ""
    assert next(row for row in alignment_return_rows if row["period_sort"] == "202609")["return_label_placement"] == "inside"
    alignment_top_bar = _first_bar_layer((alignment_spec.get("vconcat") or [{}])[0])
    top_x = (alignment_top_bar.get("encoding") or {}).get("x") or {}
    return_x = (alignment_return_bar.get("encoding") or {}).get("x") or {}
    assert top_x.get("field") == return_x.get("field") == "display_period"
    assert top_x.get("sort") == return_x.get("sort")
    alignment_offset = (alignment_return_bar.get("encoding") or {}).get("xOffset") or {}
    assert alignment_offset.get("field") == "return_anchor"
    assert alignment_offset.get("scale") == (alignment_top_bar.get("encoding") or {}).get("xOffset", {}).get("scale")
    invalid_chart_sales = dict(with_returns)
    invalid_chart_sales["chart_rows"] = [
        dict(row, return_magnitude=-1.0)
        if row.get("kind") == "매출반품" else dict(row)
        for row in with_returns["chart_rows"]
    ]
    _assert_contract_violation(lambda: _build_sales_bar_chart({"sales": invalid_chart_sales}))

    snapshot = build_dashboard_lite_chat_snapshot({"facts": {"sales": with_returns}})
    snapshot_returns = [row for row in snapshot["facts"]["sales"]["chart_rows"] if row.get("kind") == "매출반품"]
    assert snapshot_returns == [row for row in with_returns["chart_rows"] if row.get("kind") == "매출반품"]

    analytics_source = (ROOT / "app" / "services" / "analytics_sales_trend_service.py").read_text(encoding="utf-8")
    narrow_source = (ROOT / "app" / "services" / "dashboard_narrow_sales_candidate_service.py").read_text(encoding="utf-8")
    assert analytics_source.count("AS 매출반품금액") >= 2
    assert "AS [매출반품금액]" in analytics_source
    assert analytics_source.count('"매출반품금액",\n        "집계건수",') >= 1
    assert '"매출합계", "매출반품금액", "집계건수", "매입처수", "분석자료원"' in analytics_source
    assert "LEFT(Out_Put.Rd12_Io_Gu, 1) = '6'" in analytics_source
    assert "SUM(매출반품금액) AS 매출반품금액" in narrow_source
    # Source rows may use either sign. Every representation establishes the
    # positive return magnitude in SQL before Dashboard facts validate it.
    assert len(re.findall(
        r"CASE WHEN LEFT\(M\.\{p\}_Io_Gu, 1\) = '6'\s+THEN ABS\(",
        analytics_source,
    )) >= 2
    assert re.search(
        r"CASE WHEN LEFT\(\{a\}\.\{p\}_Io_Gu, 1\) = '6'\s+THEN ABS\(",
        analytics_source,
    )
    assert re.search(
        r"CASE WHEN LEFT\(Out_Put\.Rd12_Io_Gu, 1\) = '6'\s+THEN ABS\(",
        analytics_source,
    )
    assert "CASE WHEN LEFT({p}_Io_Gu, 1) = '6' THEN ABS(" in narrow_source
    print("PASS: dashboard sales-return chart contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
