"""Offline grade-matrix and inventory-detail filter checks."""
from __future__ import annotations

from pathlib import Path
import ast
import json
import math
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_lite_facts import _attach_inventory_status_and_frequency
from app.services.ssai_snapshot_repository import SnapshotReadResult
from app.sims.views.dashboard_lite import (
    _INVENTORY_DETAIL_COLUMNS,
    _build_integrated_inventory_detail_frame,
    _build_profit_contribution_grade_heatmap,
    _filter_integrated_inventory_detail_rows,
    build_dashboard_lite_chat_snapshot,
)


def main() -> None:
    products = [
        {
            "product_code": code,
            "product_name": code,
            "inventory_current_stock_present": True,
            "evaluation_expected_demand_present": True,
            "current_stock_qty": 10,
            "evaluation_expected_demand_qty": 10,
        }
        for code in ("P1", "P2", "P3", "P4", "P5", "P6")
    ]
    grades = (
        ("P1", "A", "A", "ready"),
        ("P2", "X", "X", "ready"),
        ("P4", "unavailable", "unavailable", "unavailable"),
        ("P5", "unavailable", "unavailable", "stale"),
        ("P6", "A", "unavailable", "unavailable"),
    )
    projection = tuple({
        "product_code": code, "frequency_grade": "A", "profit_grade": profit,
        "contribution_grade": contribution, "profitability_status": status,
        "data_status": "ready", "row_status": "ready",
    } for code, profit, contribution, status in grades)
    result = _attach_inventory_status_and_frequency(
        products, frequency_snapshot=SnapshotReadResult(status="ready"),
        frequency_rows=projection, monthly_business_days=20,
    )
    summary = result["summary"]
    matrix = summary["profit_contribution_grade_matrix"]
    assert len(matrix) == 6 and all(len(values) == 6 for values in matrix.values())
    assert matrix["A"]["A"] == matrix["X"]["X"] == 1
    assert sum(sum(values.values()) for values in matrix.values()) == 2
    assert summary["profit_grade_counts"]["등급자료 부족"] == 3
    assert summary["contribution_grade_counts"]["등급자료 부족"] == 4
    assert (summary["grade_missing_both_count"], summary["grade_missing_profit_only_count"], summary["grade_missing_contribution_only_count"]) == (3, 0, 1)
    assert summary["grade_missing_primary_counts"] == {
        "product_not_in_projection": 1, "stale": 1, "unavailable": 2,
        "excluded_adjustment_only": 0, "other": 0,
    }
    chart = _build_profit_contribution_grade_heatmap({"inventory": {"inventory_status_summary": summary}})
    assert chart is not None
    spec = chart.to_dict()
    heat_rows = next(rows for rows in spec["datasets"].values() if len(rows) == 36)
    assert sum(row["count"] for row in heat_rows) == 2
    assert all(math.isclose(row["visual_intensity"], row["count"] ** 0.38) for row in heat_rows)
    color = spec["layer"][0]["encoding"]["color"]
    assert color["field"] == "visual_intensity" and color["scale"]["domain"] == [0, 0.2, 0.4, 0.6, 0.8, 1]
    assert color["scale"]["range"] == ["#f5f7fa", "#e7edf4", "#b9cadb", "#7899b8", "#4f759b", "#355d83"]
    comparison_matrix = {grade: dict(counts) for grade, counts in matrix.items()}
    comparison_matrix["B"]["B"] = 4
    comparison_chart = _build_profit_contribution_grade_heatmap({
        "inventory": {"inventory_status_summary": {"profit_contribution_grade_matrix": comparison_matrix, "total_product_count": 10}}
    })
    comparison_rows = next(rows for rows in comparison_chart.to_dict()["datasets"].values() if len(rows) == 36)
    assert math.isclose(
        next(row for row in comparison_rows if row["profit"] == "A" and row["contribution"] == "A")["visual_intensity"],
        (1 / 4) ** 0.38,
    )
    assert next(row for row in comparison_rows if row["profit"] == "B" and row["contribution"] == "B")["count"] == 4
    published_counts = (
        (68, 30, 28, 50, 26, 21),
        (82, 54, 45, 81, 94, 26),
        (756, 657, 646, 1205, 1166, 83),
        (1260, 780, 774, 1484, 1744, 98),
        (239, 156, 186, 376, 582, 14),
        (0, 0, 0, 0, 0, 158),
    )
    published_matrix = {
        profit: dict(zip("ABCDEX", counts))
        for profit, counts in zip("ABCDEX", published_counts)
    }
    published_chart = _build_profit_contribution_grade_heatmap({
        "inventory": {"inventory_status_summary": {"profit_contribution_grade_matrix": published_matrix, "total_product_count": 15354}}
    })
    published_rows = next(rows for rows in published_chart.to_dict()["datasets"].values() if len(rows) == 36)
    assert sum(row["count"] for row in published_rows) == 12969
    assert 15354 - sum(row["count"] for row in published_rows) == 2385
    assert next(row for row in published_rows if row["profit"] == "D" and row["contribution"] == "E")["count"] == 1744
    source_tree = ast.parse((ROOT / "app/Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8"))
    partition_names = {"_minimal_dashboard_partition_snapshot", "_partition_message_payload", "_json_sanitize"}
    partition_nodes = [node for node in source_tree.body if isinstance(node, ast.FunctionDef) and node.name in partition_names]
    partition_ns = {
        "Any": Any, "math": math,
        "_CHAT_PARTITION_MESSAGE_ALLOW_KEYS": {"id", "type", "meta"},
        "_CHAT_PARTITION_META_ALLOW_KEYS": {"dashboard_cache", "room_id", "dashboard_event_id"},
        "_CHAT_PARTITION_TEXT_LIMIT": 20000,
        "_clip_partition_text": lambda value, _limit: value,
        "_compact_partition_value": lambda value: value,
        "log": type("Log", (), {"info": staticmethod(lambda *_a, **_k: None), "warning": staticmethod(lambda *_a, **_k: None)})(),
    }
    exec(compile(ast.Module(body=partition_nodes, type_ignores=[]), "dashboard_partition", "exec"), partition_ns)
    published_summary = {
        "profit_contribution_grade_matrix": published_matrix,
        "profit_grade_counts": {grade: sum(published_matrix[grade].values()) for grade in "ABCDEX"},
        "contribution_grade_counts": {grade: sum(published_matrix[profit][grade] for profit in "ABCDEX") for grade in "ABCDEX"},
        "total_product_count": 15354,
        "grade_missing_primary_counts": {"stale": 1005, "product_not_in_projection": 723, "unavailable": 657},
    }
    source_cache = build_dashboard_lite_chat_snapshot({"facts": {"inventory": {"inventory_status_summary": published_summary}}, "params": {}})
    message = {"id": "dashboard-1", "type": "dashboard_lite", "meta": {"room_id": "room-1", "dashboard_event_id": "dashboard-1", "dashboard_cache": source_cache}}
    partition = partition_ns["_partition_message_payload"]
    saved = partition(message)
    serialized = json.dumps(saved, ensure_ascii=False)
    assert len(serialized.encode("utf-8")) < 65536
    restored_message = partition(json.loads(serialized))
    restored_cache = restored_message["meta"]["dashboard_cache"]
    restored_summary = restored_cache["facts"]["inventory"]["inventory_status_summary"]
    assert restored_summary["profit_contribution_grade_matrix"] == published_matrix
    assert sum(sum(row.values()) for row in restored_summary["profit_contribution_grade_matrix"].values()) == 12969
    assert restored_summary["profit_grade_counts"] == published_summary["profit_grade_counts"]
    assert restored_summary["contribution_grade_counts"] == published_summary["contribution_grade_counts"]
    assert restored_summary["grade_missing_primary_counts"] == published_summary["grade_missing_primary_counts"]
    for grade in "ABCDEX":
        assert sum(restored_summary["profit_contribution_grade_matrix"][grade].values()) == restored_summary["profit_grade_counts"][grade]
        assert sum(restored_summary["profit_contribution_grade_matrix"][profit][grade] for profit in "ABCDEX") == restored_summary["contribution_grade_counts"][grade]
    restored_chart = _build_profit_contribution_grade_heatmap(restored_cache["facts"])
    restored_rows = next(rows for rows in restored_chart.to_dict()["datasets"].values() if len(rows) == 36)
    assert [(row["profit"], row["contribution"], row["count"], row["percent"]) for row in restored_rows] == [
        (row["profit"], row["contribution"], row["count"], row["percent"]) for row in published_rows
    ]
    for legacy_matrix in (None, "legacy", {"A": "legacy"}, {"A": {"A": 1}}):
        legacy_cache = build_dashboard_lite_chat_snapshot({"facts": {"inventory": {"inventory_status_summary": {"profit_contribution_grade_matrix": legacy_matrix}}}})
        legacy_message = {**message, "meta": {**message["meta"], "dashboard_cache": legacy_cache}}
        legacy_restored = partition(json.loads(json.dumps(partition(legacy_message), ensure_ascii=False)))
        assert _build_profit_contribution_grade_heatmap(legacy_restored["meta"]["dashboard_cache"]["facts"]) is None
    compact = build_dashboard_lite_chat_snapshot({"facts": {"inventory": {"inventory_status_summary": summary}}, "params": {}})
    restored = compact["facts"]["inventory"]["inventory_status_summary"]
    assert restored["profit_contribution_grade_matrix"] == matrix
    assert restored["grade_missing_primary_counts"] == summary["grade_missing_primary_counts"]
    for legacy_matrix in ({**matrix, "A": "legacy"}, "legacy", None, {"A": {"A": 1}}):
        legacy_summary = {**summary, "profit_contribution_grade_matrix": legacy_matrix}
        saved = build_dashboard_lite_chat_snapshot({"facts": {"inventory": {"inventory_status_summary": legacy_summary}}, "params": {}})
        saved_facts = saved["facts"]
        assert _build_profit_contribution_grade_heatmap(saved_facts) is None
        assert saved_facts["inventory"]["inventory_status_summary"]["profit_contribution_grade_matrix"] == legacy_matrix

    frame = _build_integrated_inventory_detail_frame({"inventory_status_detail_rows": result["detail_rows"]})
    assert "품목손익등급" in frame and "품목기여등급" in frame
    assert "품목손익등급" in _INVENTORY_DETAIL_COLUMNS and "품목기여등급" in _INVENTORY_DETAIL_COLUMNS
    base = dict(inventory_status="전체", frequency_grade="전체", risk_filter="전체", vendor_key="전체", search_text="")
    assert list(_filter_integrated_inventory_detail_rows(frame, **base, profit_grade="A", contribution_grade="A")["제품코드"]) == ["P1"]
    assert list(_filter_integrated_inventory_detail_rows(frame, **base, profit_grade="X")["제품코드"]) == ["P2"]
    assert list(_filter_integrated_inventory_detail_rows(frame, **base, contribution_grade="등급자료 부족")["제품코드"]) == ["P3", "P4", "P5", "P6"]
    assert list(_filter_integrated_inventory_detail_rows(frame, **{**base, "search_text": "P5"}, profit_grade="등급자료 부족")["제품코드"]) == ["P5"]
    print("손익·기여 heatmap 및 재고상세 필터: 통과")


if __name__ == "__main__":
    main()
