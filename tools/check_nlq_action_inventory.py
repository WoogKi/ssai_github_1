"""Static contract checks for the canonical SIMS NLQ action inventory.

This tool imports routing metadata only.  It never invokes a service handler,
opens Streamlit, or connects to an ERP database.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.sims.nlq.action_inventory import (  # noqa: E402
    CANONICAL_ACTIONS,
    DASHBOARD_ONLY,
    IMPLEMENTED,
    IO_VIEW_FALLBACK_TARGETS,
    all_panel_labels,
)
from app.services.io_nlq import get_nlq_period_action_class, resolve_io_nlq  # noqa: E402
from app.sims.nlq.nlq_router import resolve_new_sims_nlq_candidate  # noqa: E402


def _fail(message: str) -> None:
    raise AssertionError(message)


def _literal_categories() -> dict[str, list[str]]:
    """Read the panel registry without importing Streamlit UI code."""

    source_path = PROJECT_ROOT / "app" / "ui" / "sims_panel.py"
    module = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    categories_node: ast.Dict | None = None
    for node in module.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "_CATEGORIES" and isinstance(node.value, ast.Dict):
                categories_node = node.value
                break

    if categories_node is None:
        _fail("app/ui/sims_panel.py의 _CATEGORIES literal을 찾지 못했습니다.")

    categories: dict[str, list[str]] = {}
    for category_key, category_value in zip(categories_node.keys, categories_node.values):
        if not isinstance(category_key, ast.Constant) or not isinstance(category_key.value, str):
            _fail("_CATEGORIES category key가 문자열 literal이 아닙니다.")
        if not isinstance(category_value, ast.Dict):
            _fail(f"{category_key.value}: category value가 dict literal이 아닙니다.")

        actions_node: ast.Dict | None = None
        for field_key, field_value in zip(category_value.keys, category_value.values):
            if isinstance(field_key, ast.Constant) and field_key.value == "actions":
                if isinstance(field_value, ast.Dict):
                    actions_node = field_value
                break
        if actions_node is None:
            _fail(f"{category_key.value}: actions literal을 찾지 못했습니다.")

        labels: list[str] = []
        for action_key in actions_node.keys:
            if not isinstance(action_key, ast.Constant) or not isinstance(action_key.value, str):
                _fail(f"{category_key.value}: action label이 문자열 literal이 아닙니다.")
            labels.append(action_key.value)
        categories[category_key.value] = labels
    return categories


def _resolve_dotted_callable(target: str) -> Any:
    module_name, attribute = target.rsplit(".", 1)
    value = getattr(importlib.import_module(module_name), attribute, None)
    if not callable(value):
        _fail(f"callable target을 찾지 못했습니다: {target}")
    return value


def _check_panel_inventory() -> None:
    panel_categories = _literal_categories()
    panel_labels = [label for labels in panel_categories.values() for label in labels]
    inventory_labels = list(all_panel_labels())

    if len(panel_labels) != 39:
        _fail(f"panel label 수가 예상과 다릅니다: {len(panel_labels)} != 39")
    if len(CANONICAL_ACTIONS) != 33:
        _fail(f"canonical action 수가 예상과 다릅니다: {len(CANONICAL_ACTIONS)} != 33")
    if len(set(panel_labels)) != len(panel_labels):
        _fail("panel action label에 중복이 있습니다.")
    if len(set(inventory_labels)) != len(inventory_labels):
        _fail("canonical inventory label에 중복이 있습니다.")
    if set(panel_labels) != set(inventory_labels):
        _fail(
            "panel registry와 canonical inventory의 label이 다릅니다: "
            f"panel_only={sorted(set(panel_labels) - set(inventory_labels))}, "
            f"inventory_only={sorted(set(inventory_labels) - set(panel_labels))}"
        )

    category_by_label = {
        label: category for category, labels in panel_categories.items() for label in labels
    }
    for spec in CANONICAL_ACTIONS:
        for label in (spec.panel_action, *spec.label_aliases):
            if category_by_label.get(label) != spec.panel_category:
                _fail(f"{label}: panel category와 inventory category가 다릅니다.")

    alias_count = sum(len(spec.label_aliases) for spec in CANONICAL_ACTIONS)
    if alias_count != 6:
        _fail(f"panel alias 수가 예상과 다릅니다: {alias_count} != 6")


def _check_statuses() -> None:
    implemented = [spec for spec in CANONICAL_ACTIONS if spec.implementation_status == IMPLEMENTED]
    dashboard_only = [spec for spec in CANONICAL_ACTIONS if spec.implementation_status == DASHBOARD_ONLY]
    other = [
        spec
        for spec in CANONICAL_ACTIONS
        if spec.implementation_status not in {IMPLEMENTED, DASHBOARD_ONLY}
    ]
    if len(implemented) != 32 or len(dashboard_only) != 1 or other:
        _fail(
            "implementation status count 불일치: "
            f"implemented={len(implemented)}, dashboard_only={len(dashboard_only)}, other={len(other)}"
        )
    dashboard = dashboard_only[0]
    if dashboard.canonical_action != "Dashboard Lite v0.1" or not dashboard.dashboard_only_reason:
        _fail("Dashboard Lite v0.1의 dashboard-only 계약이 누락됐습니다.")


def _check_handler_coverage() -> None:
    from app.sims.nlq import nlq_router
    from app.sims.views import rddbc_io_views

    for spec in CANONICAL_ACTIONS:
        if spec.implementation_status != IMPLEMENTED:
            continue
        if spec.handler_kind == "analytics":
            handler = nlq_router._get_analytics_handler(spec.canonical_action)
            if not callable(handler):
                _fail(f"analytics handler를 찾지 못했습니다: {spec.canonical_action}")
        else:
            _resolve_dotted_callable(spec.handler_target)

    io_specs = {
        spec.canonical_action
        for spec in CANONICAL_ACTIONS
        if spec.handler_kind == "io_service"
    }
    if set(IO_VIEW_FALLBACK_TARGETS) != io_specs:
        _fail("IO fallback action set이 canonical IO action set과 다릅니다.")
    for action, target_name in IO_VIEW_FALLBACK_TARGETS.items():
        if not callable(getattr(rddbc_io_views, target_name, None)):
            _fail(f"IO fallback export를 찾지 못했습니다: {action} -> {target_name}")

    if IO_VIEW_FALLBACK_TARGETS.get("제품수불현황 조회") != "view_product_flow":
        _fail("제품수불현황 조회 fallback이 view_product_flow가 아닙니다.")
    if IO_VIEW_FALLBACK_TARGETS.get("제품재고현황 조회") != "view_product_inventory":
        _fail("제품재고현황 조회 fallback이 view_product_inventory가 아닙니다.")
    if hasattr(rddbc_io_views, "view_rddbc250") or hasattr(rddbc_io_views, "view_rddbc260"):
        _fail("aggregate IO view에 사용 금지 fallback alias가 노출됐습니다.")


def _check_product_flow_inventory_aliases() -> None:
    cases = (
        ("제품수불현황 조회", "제품수불현황 조회"),
        ("제품 수불 현황 조회", "제품수불현황 조회"),
        ("제품수불부 조회", "제품수불현황 조회"),
        ("제품코드 31768 제품수불현황 조회", "제품수불현황 조회"),
        ("제품재고현황 조회", "제품재고현황 조회"),
        ("제품 재고 현황 조회", "제품재고현황 조회"),
        ("제품재고장 조회", "제품재고현황 조회"),
        ("장부재고 제품재고현황 조회", "제품재고현황 조회"),
    )
    for query, expected_action in cases:
        parsed = resolve_io_nlq(query)
        if isinstance(parsed, dict) and str(parsed.get("action") or "") != expected_action:
            _fail(f"io parser action mismatch: {query} -> {parsed.get('action')}")
        routed = resolve_new_sims_nlq_candidate(query)
        if not isinstance(routed, dict) or str(routed.get("action") or "") != expected_action:
            _fail(f"new NLQ route action mismatch: {query} -> {routed}")


def _check_period_policy_classification() -> None:
    """Every implemented canonical action belongs to one NLQ period class."""
    expected = {
        "제품수불현황 조회": "single_entity_history",
        "제품재고현황 조회": "inventory_movement",
        "실재고월집계 조회": "inventory_snapshot",
        "장부재고월집계 조회": "inventory_snapshot",
    }
    for spec in CANONICAL_ACTIONS:
        if spec.implementation_status != IMPLEMENTED:
            continue
        actual = get_nlq_period_action_class(spec.canonical_action)
        if spec.handler_kind == "analytics":
            wanted = "aggregate_analysis"
        elif spec.handler_kind == "io_service":
            wanted = expected.get(spec.canonical_action, "list_detail")
        else:
            wanted = "other"
        if actual != wanted:
            _fail(
                f"period policy class mismatch: {spec.canonical_action}: {actual} != {wanted}"
            )


def main() -> int:
    checks = (
        ("panel registry / inventory coverage", _check_panel_inventory),
        ("implementation status contract", _check_statuses),
        ("handler and IO fallback callable coverage", _check_handler_coverage),
        ("product flow / inventory parser aliases", _check_product_flow_inventory_aliases),
        ("canonical NLQ period policy classification", _check_period_policy_classification),
    )
    failures: list[str] = []
    for label, check in checks:
        try:
            check()
            print(f"[OK] {label}")
        except Exception as exc:  # noqa: BLE001 - report all contract failures clearly
            failures.append(f"[FAIL] {label}: {type(exc).__name__}: {exc}")

    if failures:
        print("\n".join(failures))
        print(f"RESULT: FAIL ({len(failures)} checks)")
        return 1

    print(
        "RESULT: OK "
        f"(canonical_actions={len(CANONICAL_ACTIONS)}, panel_labels={len(all_panel_labels())}, "
        "implemented=32, dashboard_only=1, aliases=6)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
