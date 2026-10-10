"""DB-free contract checks for the resumable Company4 GFINAL live runner."""

from __future__ import annotations

import csv
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.check_nlq_gfinal_248_full_live_20261007 import (
    _assert_parent_cache_contract,
    _current_table_classification,
    _load_existing_rows,
)
from tools.check_nlq_regression_harness_alignment_20261006 import _main_namespace


def main() -> int:
    cases = [
        {"source_seq": "171", "parent_question_raw": "거래명세서 공통조회  20260917"},
        {"source_seq": "172", "parent_question_raw": "거래명세서 공통조회  20260917"},
        {"source_seq": "173", "parent_question_raw": "거래명세서 공통조회  20260917"},
        {"source_seq": "174", "parent_question_raw": ""},
    ]
    _assert_parent_cache_contract(cases)

    detail = {
        "parent_status": "success", "followup_handled": True,
        "followup_status": "success", "source_binding_pass": True,
        "current_table_extra_erp_source_call": 0, "schema_pass": True,
        # This intentionally differs: it was the old false-positive path.
        "verdict": "FAIL",
    }
    if _current_table_classification(detail) != "PASS":
        raise AssertionError("successful parent/follow-up contract must not compare action labels")

    no_data_parent = {
        "parent_status": "no_data", "source_table_key": "",
        "followup_handled": False, "followup_status": "",
        "current_table_extra_erp_source_call": 0,
    }
    if _current_table_classification(no_data_parent) != "DATA_NO_RESULT_ALLOWED":
        raise AssertionError("an empty official parent cannot be called a capture defect")

    namespace = _main_namespace({}, lambda *_args, **_kwargs: None)
    source_actions = namespace["_ANALYTICS_KPI_SOURCE_ACTIONS"]
    if "품목별 매출 예상" not in source_actions:
        raise AssertionError("forecast parent must use the production implicit-followup path")
    normalized = namespace["_normalize_implicit_analytics_current_followup"](
        "예상등급 감소예상 상세히 보여줘"
    )
    if normalized != "현재표 예상등급 감소예상 상세히 보여줘":
        raise AssertionError(f"implicit Current Table normalization drifted: {normalized}")

    output = ROOT / ".tmp_onepass" / "runner_resume_fixture.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("golden_case_id", "source_seq"))
        writer.writeheader()
        writer.writerow({"golden_case_id": "GFINAL-0171", "source_seq": "171"})
    rows = _load_existing_rows(output, start_source_seq=172)
    if len(rows) != 1 or rows[0]["golden_case_id"] != "GFINAL-0171":
        raise AssertionError("resume must retain completed evidence once")
    print("PASS: parent cache, implicit follow-up, empty-parent classification, and no-overlap resume")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
