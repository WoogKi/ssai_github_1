"""Offline gate for the order-only R046 management-product boundary."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.erp_table_nlq import resolve_registered_erp_table_nlq
from app.services.order_calculation_service import _master, assemble_result
from app.services.product_master_filter_contract import (
    add_named_management_only_exclusion,
    is_management_only_standard_code,
)
from app.services.dashboard_inventory_frequency_snapshot import FrequencyProjectionReadResult
from app.services.snapshot_product_information_service import get_snapshot_product_information_result


VALID = "8801234567890"


def test_policy_boundary() -> None:
    def excluded(di: str, standard: str, representative: str) -> bool:
        return is_management_only_standard_code(standard, representative)

    for di in ("", "0", "1", "3", "4", "D", "B"):
        assert excluded(di, VALID, VALID)
        assert not excluded(di, VALID, "8801234567891")
        assert not excluded(di, "", "")
        assert not excluded(di, "880BAD", "880BAD")
        assert not excluded(di, "9901234567890", "9901234567890")


def test_order_master_sql() -> None:
    captured: list[tuple[str, tuple]] = []

    def query(sql: str, binds: tuple):
        captured.append((sql, binds))
        return pd.DataFrame(columns=["제품코드"])

    with patch("app.services.order_calculation_service.query_to_df", side_effect=query):
        _master({})
        _master({"physic_cd": "63431"})
    assert len(captured) == 2
    for sql, binds in captured:
        assert sql.count("NOT EXISTS (SELECT 1 FROM dbo.Rddbc046 AS ManagementStd") == 1
        assert "P.Rd04_Physic_Di" in sql
        assert "ManagementStd.Rd046_Standard_Cd" in sql
        assert "ManagementStd.Rd046_Main_Standard_Cd" in sql
        assert "LIKE '880%'" in sql and "NOT LIKE '%[^0-9]%'" in sql
        management = sql.split("NOT EXISTS (SELECT 1 FROM dbo.Rddbc046 AS ManagementStd", 1)[1]
        assert "P.Rd04_Physic_Di" not in management
    assert "P.Rd04_Physic_Cd = ?" not in captured[0][0]
    assert "P.Rd04_Physic_Cd = ?" in captured[1][0]
    assert "63431" not in captured[1][0] and "63431" in captured[1][1]
    existing_consumer: list[str] = []
    add_named_management_only_exclusion(
        existing_consumer, product_code_expression="Physic_Cd.Rd04_Physic_Cd",
    )
    assert len(existing_consumer) == 1
    assert "Physic_Di" not in existing_consumer[0]


def test_full_single_nlq_snapshot_bridge() -> None:
    captured: list[tuple[str, tuple]] = []
    selected = ("63431", "63432", "63433")

    def query(sql, binds):
        captured.append((sql, binds))
        rows = [
            {"제품코드": code, "제품명": "fixture", "제약사": "fixture", "규격": "10T"}
            for code in selected[1:]
        ]
        return pd.DataFrame(rows).loc[
            lambda frame: frame["제품코드"].eq(binds[-1]) if binds and binds[-1] in selected else frame["제품코드"].ne("")
        ].reset_index(drop=True)

    def projection(**kwargs):
        codes = [kwargs["product_codes"]] if isinstance(kwargs.get("product_codes"), str) else kwargs.get("product_codes")
        rows = tuple({"product_code": code, "frequency_grade": "A"}
                     for code in selected if not codes or code in codes)
        return FrequencyProjectionReadResult(
            status="ready", rows=rows, manifest_id=1, generation_no=1,
            checksum="a" * 64, authority_status="ready", resolution_status="exact_match",
            contract_version="2.1",
        )

    nlq = resolve_registered_erp_table_nlq(
        "제품코드 63431 조회구분 전체 발주계산", today=date(2026, 10, 4),
    )
    assert nlq and nlq["params"]["physic_cd"] == "63431"
    assert nlq["params"]["_display_context"] == "chat"
    assert nlq["params"]["query_mode"] == "전체"
    scope = SimpleNamespace(stock_codes=(), product_group_codes=(), product_di_codes=(),
                            product_class_codes=(), stock_mode="real", io_gu_codes=())
    with patch("app.services.snapshot_product_information_service.get_current_company_id", return_value=4), \
         patch("app.services.order_calculation_service.query_to_df", side_effect=query):
        for code, expected in (("", {"63432", "63433"}), ("63431", set()),
                               ("63432", {"63432"})):
            result = get_snapshot_product_information_result(
                {"company_id": 4, "evaluation_month": "202610", "physic_cd": code},
                profile_resolver=lambda **_: scope, projection_reader=projection,
                master_loader=_master, apply_viewer_projection=False,
            )
            assert result["meta"]["snapshot_status"] == "ready"
            observed = set(result.get("df", pd.DataFrame()).get("제품코드", []))
            assert observed == expected, (code, observed, expected)
            if not code:
                base = result["df"]
                sources = {
                    "snapshot": result,
                    "base": base,
                    "demand": pd.DataFrame({
                        "제품코드": list(expected), "현재재고수량": [0] * len(expected),
                        "당월 예상출고수량": [30] * len(expected),
                        "수요예상기준": ["최근3개월평균수요수량"] * len(expected),
                    }),
                    "suppliers": pd.DataFrame({
                        "product_code": list(expected),
                        "recent_inbound_vendor_code": ["00100"] * len(expected),
                        "recent_inbound_vendor_name": ["fixture"] * len(expected),
                    }),
                    "pending": pd.DataFrame(), "order_history": pd.DataFrame(),
                    "order_history_complete": True, "calendar_status": "ready",
                    "business_dates": [date(2026, 10, day) for day in (7, 8, 9, 12, 13)],
                }
                assembled = assemble_result(
                    {"company_id": 4, "order_date": "2026-10-06", "safety_days": 3,
                     "target_days": 5, "closing_day": 25}, sources,
                )
                assert set(assembled["제품코드"]) == expected
    assert len(captured) == 3


if __name__ == "__main__":
    test_policy_boundary()
    test_order_master_sql()
    test_full_single_nlq_snapshot_bridge()
    print("Rddbc046 order exclusion: PASS (offline, DB=0, LLM=0)")
