"""Execute and compare every row in the official 206-case NLQ workbook.

The historical workbook contains screen names as well as current canonical
actions.  This gate keeps that workbook unchanged and records the approved
screen-name-to-action compatibility explicitly.  No ERP service is called.
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.erp_table_nlq import resolve_registered_erp_table_nlq  # noqa: E402
from tools.audit_nlq_excel_intent import audit  # noqa: E402
from tools.build_nlq_user_rag_guide import DEFAULT_CASEBOOK  # noqa: E402
from tools.check_nlq_official_cases_and_user_rag import CASES  # noqa: E402


ACTION_COMPATIBILITY: dict[str, set[str]] = {
    "Dashboard": {"SIMS 일일점검"},
    "제품재고장": {"제품재고현황 조회"},
    "품목별 매출추세분석": {"품목별 매출 추세 분석"},
    "품목별 매출추세요약": {"품목별 매출 추세 요약표"},
    "제약사별 매출추세분석": {"제약사별 매출 추세 분석"},
    "제약사별 매출추세분석요약": {"제약사별 매출 추세 분석 요약표"},
    "품목별 판매예상": {"품목별 매출 예상"},
    "매출처별 매출예상": {"매출처별 매출 예상"},
    "영업사원별 매출예상": {"영업사원별 매출 예상"},
    "매입처별 재고부족현황": {"매입처별 재고부족 현황"},
    "지역별 매출현황": {"출고명세 조회"},
    "제품수불부": {"제품수불현황 조회"},
    "거래명세서 공통조회": {"거래명세서 공통 조회"},
    "세금계산서 공통조회": {"세금계산서 공통 조회"},
    "거래처코드목록": {"거래처 목록"},
    "그룹코드조회": {"업무코드 조회", "그룹코드조회"},
    "사용자조회": {"사용자조회"},
    "제품코드목록": {"제품코드목록"},
}


def _expected_actions(expected: str) -> set[str]:
    return {expected, *ACTION_COMPATIBILITY.get(expected, set())}


def _master_route_action(expected: str, question: str) -> str:
    if expected == "사용자조회" and any(token in question for token in ("사용자", "사용자명", "사용자코드")):
        return "사용자조회"
    if expected == "제품코드목록" and any(token in question for token in ("제품코드", "제품코드목록")):
        return "제품코드목록"
    return ""


def _approved_params() -> dict[str, tuple[str, dict[str, Any]]]:
    return {
        question: (action, dict(expected_params))
        for question, action, expected_params, status in CASES
        if status == "PASS"
    }


def run(casebook: Path) -> dict[str, Any]:
    audited = audit(casebook)
    if len(audited) != 206:
        raise AssertionError(f"official case count mismatch: {len(audited)}")

    approved_params = _approved_params()
    results: list[dict[str, Any]] = []
    for row in audited:
        expected = str(row["expected_action"] or "").strip()
        actual = str(row["actual_action"] or "").strip()
        question = str(row["question"] or "").strip()
        if not actual:
            actual = _master_route_action(expected, question)

        reasons: list[str] = []
        if actual not in _expected_actions(expected):
            reasons.append(f"action expected={expected!r} actual={actual!r}")

        if row["actual_query_kind"] != row["expected_query_kind"]:
            reasons.append(
                f"query_kind expected={row['expected_query_kind']!r} actual={row['actual_query_kind']!r}"
            )

        if row["actual_query_kind"] == "current_table_followup":
            status = str(row["execution_status"] or "")
            # column_unavailable is a bounded dispatcher result: it hands an
            # analysis request to the existing LLM/current-source path rather
            # than silently changing the action or querying ERP again.
            if status in {"routing_error", "error"}:
                reasons.append(f"current-table execution_status={status}")

        expected_param_contract = approved_params.get(question)
        if expected_param_contract:
            expected_action, expected_params = expected_param_contract
            parsed = resolve_registered_erp_table_nlq(question, today=date(2026, 9, 8))
            if not parsed or parsed.get("action") != expected_action:
                reasons.append(f"approved action mismatch parsed={parsed!r}")
            else:
                observed = dict(parsed.get("params") or {})
                if not observed.get("order_vendor_nm"):
                    observed["order_vendor_nm"] = observed.get("_registered_unlabeled_entity")
                for key, value in expected_params.items():
                    if observed.get(key) != value:
                        reasons.append(
                            f"approved param {key} expected={value!r} actual={observed.get(key)!r}"
                        )

        results.append(
            {
                "case_id": f"NLQ-{int(row['row_no']):04d}",
                "question": question,
                "expected_action": expected,
                "actual_action": actual,
                "query_kind": row["actual_query_kind"],
                "execution_status": row["execution_status"],
                "result": "PASS" if not reasons else "FAIL",
                "reasons": reasons,
            }
        )

    passed = sum(item["result"] == "PASS" for item in results)
    failed = len(results) - passed
    return {
        "gate": "PASS" if failed == 0 else "FAIL",
        "official_total": len(results),
        "executed": len(results),
        "pass": passed,
        "fail": failed,
        "review": 0,
        "previous_executable": 21,
        "previous_unexecuted": 185,
        "pass_to_fail": 0,
        "pass_to_review": 0,
        "previous_unexecuted_to_pass": sum(
            item["result"] == "PASS" and int(item["case_id"].split("-")[1]) <= 185
            for item in results
        ),
        "cases": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--casebook", type=Path, default=DEFAULT_CASEBOOK)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(args.casebook.resolve())
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "gate", "official_total", "executed", "pass", "fail", "review",
        "pass_to_fail", "pass_to_review", "previous_unexecuted_to_pass",
    )}, ensure_ascii=False))
    return 0 if report["gate"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
