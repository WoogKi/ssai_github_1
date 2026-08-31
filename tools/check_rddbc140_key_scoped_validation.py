"""Focused regression for Rddbc140 key-scoped validation and NLQ reuse."""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import io_nlq, rddbc140_service
from app.services.io_nlq import _extract_unlabeled_entity_phrase, resolve_io_nlq
from app.sims.views.rddbc_io_shared import _prepare_io_display_df
from app.ui.sims_table_display import resolve_sims_excel_number_format


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _fixture() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Rd14_Tax_Di": ["1", "1", "3"],
            "Rd14_Tax_YyMmDd": ["20260831"] * 3,
            "Rd14_Ven_Cd": ["A", "B", "C"],
            "Rd14_Tax_Seq": [1, 2, 3],
            "Rd14_Supply_Price": [100, 200, 300],
            "Rd14_Tax_Price": [10, 20, 30],
            "Rd14_Tot_Amt": [110, 220, 330],
            "상세합계일치": ["N", None, "Y"],
            "거래처명": ["가", "나", "다"],
        }
    )


def main() -> int:
    captured: list[str] = []

    def _query(sql: str, _params: dict) -> pd.DataFrame:
        captured.append(str(sql))
        return _fixture()

    with patch.object(rddbc140_service, "query_to_df", side_effect=_query):
        mismatches = rddbc140_service.get_rddbc140_df(
            {"date_from": "20260831", "date_to": "20260831", "only_mismatch": "Y"}
        )

    sql = captured[0]
    _assert("FilteredBooks AS" in sql and "FilteredTaxKeys AS" in sql, "Rddbc140 driving key CTE missing")
    _assert("SELECT TOP (%(top)s) Tax_Books.*" in sql, "display TOP must constrain FilteredBooks")
    _assert("INNER JOIN FilteredTaxKeys AS Keys" in sql, "detail aggregate must join filtered keys")
    _assert("In_Put.Rd11_Tax_Di = Keys.Tax_Di" in sql and "Out_Put.Rd12_Tax_Di = Keys.Tax_Di" in sql, "native four-key join missing")
    _assert("LTRIM(RTRIM(Rd11_Tax_Seq))" not in sql and "LTRIM(RTRIM(Rd12_Tax_Seq))" not in sql, "Tax_Seq string conversion remains")
    _assert(list(mismatches["상세합계일치"]) == ["N"], "only_mismatch must exclude detail-missing rows")

    summary = rddbc140_service.summarize_rddbc140_df(_fixture())
    _assert(summary["mismatch_count"] == 1, "summary must count only explicit N")
    _assert(summary["detail_missing_count"] == 1, "summary must retain detail-missing separately")

    for validation_query in (
        "오늘 세금계산서 부적합자료 전체",
        "오늘 세금계산서 공통 부적합조회",
        "세금계산서 부적합자료 202608",
    ):
        parsed = resolve_io_nlq(validation_query)
        _assert(parsed and parsed["action"] == "세금계산서 공통 조회", "tax validation action must be deterministic")
        _assert(parsed["params"].get("validation_requested") is True, "tax validation intent must be attached before entity resolution")
        _assert(parsed["params"].get("only_mismatch") == "Y", "tax invalid query must request only explicit mismatches")
        _assert(
            _extract_unlabeled_entity_phrase(validation_query, "세금계산서 공통 조회") == "",
            "tax validation syntax must not become an unlabeled entity",
        )
        with patch.object(io_nlq, "_lookup_unlabeled_io_entity_candidates") as lookup:
            entity_result = io_nlq.resolve_unlabeled_io_entity_condition(
                validation_query,
                action="세금계산서 공통 조회",
                params=parsed["params"],
            )
        _assert(entity_result["status"] == "not_applicable", "tax validation must skip entity resolution")
        lookup.assert_not_called()

    identifiers = _prepare_io_display_df(pd.DataFrame({
        "세금계산서구분": [3.0],
        "세금계산서순번": [476.0],
        "Rd14_Tax_Seq": [476.0],
        "Rd14_Slip_Seq": [7.0],
        "공급가액": [1234.5],
    }), add_row_no=False)
    _assert(str(identifiers["세금계산서구분"].dtype) == "Int64", "tax division must remain numeric integer")
    _assert(str(identifiers["세금계산서순번"].dtype) == "Int64", "tax sequence must remain numeric integer")
    for col in ("세금계산서구분", "세금계산서순번", "Rd14_Tax_Seq", "Rd14_Slip_Seq"):
        _assert(resolve_sims_excel_number_format(col) == "#,##0", f"{col} must use integer Excel format")
    _assert(resolve_sims_excel_number_format("공급가액") == "#,##0", "amount format contract changed")

    router_path = Path("app/sims/nlq/nlq_router.py")
    tree = ast.parse(router_path.read_text(encoding="utf-8"))
    target = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_ensure_io_summary_meta")
    calls = [
        node for node in ast.walk(target)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_ensure_tax_doc_llm_summary"
    ]
    _assert(len(calls) == 1, "tax summary helper must run once per request")
    router_text = router_path.read_text(encoding="utf-8")
    _assert('payload["df_full"] = full_source_df' in router_text, "full source must be attached for chat reuse")

    print("RESULT: OK - Rddbc140 filtered keys, mismatch semantics, and one-request reuse")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
