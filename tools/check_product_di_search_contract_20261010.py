"""DB-free positive and negative checks for labelled and unlabelled DI search."""

from __future__ import annotations

import logging
from pathlib import Path
import sys
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.io_nlq import resolve_io_nlq
from app.services.product_master_filter_contract import (
    append_product_master_filter_clauses,
    build_product_master_enrichment_sql,
    classify_product_prescription_semantic,
)
from app.sims.nlq import nlq_goods


def _sql(filters: dict[str, str]) -> tuple[str, list[object]]:
    _joins, expressions = build_product_master_enrichment_sql(product_alias="P")
    clauses: list[str] = []
    binds: list[object] = []
    append_product_master_filter_clauses(clauses, binds, filters, expressions=expressions)
    return " ".join(clauses), binds


def main() -> int:
    for term in ("보험", "전문의약품", "의료기기"):
        query = f"제품구분 {term} 제품정보 조회"
        params = (resolve_io_nlq(query) or {}).get("params") or {}
        assert params.get("product_di_nm") == term, (query, params)
        sql, binds = _sql(params)
        assert "PD.Rd01_Hnm LIKE ?" in sql and f"%{term}%" in binds, (query, sql, binds)
        assert "Rd04_Physic_Di" not in sql, (query, sql)

    expected = {
        "보험": ("insurance_product", ("N'0'", "N'4'"), "N'%|보험|%'"),
        "비보험": ("non_insurance", ("N'5'", "N'7'"), "N'%|비보험|%'"),
        "보험약": ("insurance", ("N'1'", "N'3'"), None),
        "비보험약": ("non_insurance_drug", ("N'5'", "N'7'"), None),
    }
    for term, (group, codes, name_token) in expected.items():
        params = (resolve_io_nlq(f"{term} 제품정보 조회") or {}).get("params") or {}
        assert params.get("product_di_semantic_group") == group, (term, params)
        assert not params.get("product_di_nm") and not params.get("nlq_unlabeled_name"), (term, params)
        sql, binds = _sql(params)
        assert all(code in sql for code in codes) and not binds, (term, sql, binds)
        assert bool(name_token and name_token in sql) == bool(name_token), (term, sql)
        if name_token is None:
            assert "PD.Rd01_Hnm" not in sql, (term, sql)
        else:
            assert "NOT LIKE" in sql and "LIKE N'%[^0-9]%'" in sql, (term, sql)
    for term, semantic in (("전문약", "prescription"), ("일반약", "otc")):
        params = (resolve_io_nlq(f"{term} 제품정보 조회") or {}).get("params") or {}
        assert params.get("product_prescription_semantic") == semantic, (term, params)
        sql, _binds = _sql(params)
        assert "LIKE N'%[^0-9]%'" in sql and "CompanyProductDi.Rd01_Hnm" in sql, (term, sql)
    assert classify_product_prescription_semantic("4", "", "", "일반의약품") == ""

    calls: list[dict[str, object]] = []
    with (
        patch.object(nlq_goods, "search_goods_full", side_effect=lambda **kw: calls.append(kw) or pd.DataFrame()),
        patch.object(nlq_goods, "push_sims_result_to_chat", return_value=None),
    ):
        for query, key, value in (
            ("제품구분 보험 제품조회", "di_name_kw", "보험"),
            ("보험 제품조회", "product_di_semantic_group", "insurance_product"),
            ("비보험약 제품조회", "product_di_semantic_group", "non_insurance_drug"),
        ):
            calls.clear()
            handled = nlq_goods.try_handle_goods_nlq(
                query, room={"room_id": "fixture", "messages": []}, session_state={},
                make_ts=lambda: "fixture", next_seq=lambda: 1,
                logger=logging.getLogger("product-di-search-contract"),
            )
            assert handled and len(calls) == 1, (query, calls)
            assert calls[0].get(key) == value and not calls[0].get("keyword"), (query, calls)
    print("PRODUCT DI SEARCH CONTRACT PASS: labels, code axes, A-Z fallback, and goods router")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
