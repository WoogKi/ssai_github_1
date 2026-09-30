"""Offline regression for Dashboard NLQ phrase whitespace normalization."""

from __future__ import annotations

import logging
from pathlib import Path
import sys
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    from app.sims.nlq import nlq_router as router
    from app.services import dashboard_lite_facts, product_supplier_scope_service
    from app.services import ssai_analysis_profile_service
    from app.ui import ssai_login

    action = "SIMS 일일점검"
    bare_variants = (
        "SIMS 일일점검",
        "SIMS  일일점검",
        "SIMS   일일점검",
        "SIMS일일점검",
        "SIMS\t일일점검",
        "SIMS\u00a0일일점검",
        "오늘의 경영점검",
        "오늘의  경영점검",
        "오늘의경영점검",
        "SIMS 운영점검",
        "SIMS  운영점검",
    )
    supplier_variants = (
        ("SIMS 일일점검 한미", "한미", "manufacturer"),
        ("SIMS  일일점검  한미", "한미", "manufacturer"),
        ("SIMS 일일점검 제약사 한미", "한미", "manufacturer"),
        ("SIMS 운영점검 발주처 종근당", "종근당", "order_vendor"),
    )

    assert all(router._resolve_dashboard_nlq_action(query) == action for query in bare_variants)
    assert all(router._dashboard_nlq_residual(query) == "" for query in bare_variants)
    assert router._extract_dashboard_nlq_conditions("오늘의 경영점검 담당자 김")[0]["담당자"] == "김"

    supplier_calls: list[tuple[str, str]] = []

    def resolve_supplier(text: str, *, mode: str):
        supplier_calls.append((text, mode))
        return [{"code": "10047", "name": text}]

    with (
        patch.object(ssai_login, "get_selected_company", return_value={"company_id": 3}),
        patch.object(ssai_analysis_profile_service, "load_dashboard_profile", return_value={}),
        patch.object(dashboard_lite_facts, "default_dashboard_lite_scope", return_value={}),
        patch.object(dashboard_lite_facts, "normalize_dashboard_lite_params", side_effect=lambda value: value),
        patch.object(product_supplier_scope_service, "resolve_supplier_vendor_codes", side_effect=resolve_supplier),
    ):
        for query in bare_variants:
            params, notice = router._build_dashboard_nlq_params(query, session_state={}, logger=logging.getLogger(__name__))
            assert notice is None, query
            assert not (params.get("manufacturer_codes") or params.get("order_vendor_codes")), query
        assert supplier_calls == [], supplier_calls

        for query, expected_supplier, expected_mode in supplier_variants:
            params, notice = router._build_dashboard_nlq_params(query, session_state={}, logger=logging.getLogger(__name__))
            assert notice is None, query
            assert params.get("product_supplier_scope_mode") == expected_mode, query
            expected_key = "manufacturer_codes" if expected_mode == "manufacturer" else "order_vendor_codes"
            assert params.get(expected_key) == ["10047"], query
            assert supplier_calls[-1] == (expected_supplier, expected_mode), supplier_calls[-1]

    print("PASS dashboard NLQ whitespace normalization; DB connection attempts=0")


if __name__ == "__main__":
    main()
