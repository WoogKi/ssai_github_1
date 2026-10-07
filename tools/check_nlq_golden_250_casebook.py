"""Offline contract audit for the immutable Golden-250 NLQ export.

This gate deliberately audits only casebook/oracle/harness reachability.  It
does not open an ERP connection and never treats historical workbook results
as fresh PASS evidence.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.sims.nlq.action_inventory import implemented_actions


def _read_casebook(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError("Golden casebook cases missing")
    return [dict(item) for item in cases]


def _normalized(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def audit(casebook: Path, *, id_prefix: str = "G250", expected_count: int = 250) -> list[dict[str, str]]:
    cases = _read_casebook(casebook)
    known_actions = {spec.canonical_action for spec in implemented_actions()}
    expected_ids = {f"{id_prefix}-{index:04d}" for index in range(1, expected_count + 1)}
    actual_ids = {str(case.get("golden_case_id") or "") for case in cases}
    if expected_count < 1 or len(cases) != expected_count or actual_ids != expected_ids:
        raise AssertionError(f"Golden IDs are not exactly {id_prefix}-0001..{expected_count:04d}")
    pairs = {(_normalized(case.get("parent_question_raw")), _normalized(case.get("question_raw"))) for case in cases}
    if len(pairs) != expected_count:
        raise AssertionError("Golden normalized parent/question has duplicates")
    rows: list[dict[str, str]] = []
    valid_modes = {
        "DIRECT_TABLE", "DIRECT_STRUCTURED", "CURRENT_TABLE_DETERMINISTIC",
        "CURRENT_TABLE_LLM", "DIRECT_LLM", "CLARIFICATION", "EXPECTED_BLOCK", "DATA_DEPENDENT", "SOURCE_REVIEW",
    }
    valid_compatibility = {"EXACT", "DECLARED_ALIAS", "SCREEN_TO_RUNTIME_COMPATIBLE", "UNDECLARED", "NOT_APPLICABLE"}
    for case in cases:
        expected = str(case.get("expected_action_raw") or "")
        resolved = str(case.get("resolved_expected_action") or "")
        mode = str(case.get("execution_mode") or "")
        status = str(case.get("compatibility_status") or "")
        reasons: list[str] = []
        if not str(case.get("question_raw") or ""):
            reasons.append("blank_question")
        if not expected:
            reasons.append("blank_expected_action")
        if status not in valid_compatibility:
            reasons.append("unknown_compatibility")
        if status == "UNDECLARED":
            reasons.append("oracle_review_expected_action")
        # A screen-to-runtime contract is intentionally validated by the
        # official compatibility registry, not by an inventory literal.
        if resolved and resolved not in known_actions and status not in {"SCREEN_TO_RUNTIME_COMPATIBLE", "DECLARED_ALIAS"}:
            reasons.append("resolved_action_not_registered")
        if mode not in valid_modes:
            reasons.append("unknown_execution_mode")
        if mode.startswith("CURRENT_TABLE") and not str(case.get("parent_question_raw") or ""):
            reasons.append("current_table_missing_parent")
        if str(case.get("parent_question_raw") or "") and not mode.startswith("CURRENT_TABLE"):
            reasons.append("parent_mode_inconsistent")
        result = "PASS" if not reasons else ("ORACLE_REVIEW" if any("oracle" in reason for reason in reasons) else "SOURCE_REVIEW")
        rows.append({
            "golden_case_id": str(case["golden_case_id"]), "source_seq": str(case["source_seq"]),
            "question_raw": str(case["question_raw"]), "parent_question_raw": str(case.get("parent_question_raw") or ""),
            "expected_action_raw": expected, "resolved_expected_action": resolved,
            "compatibility_status": status, "execution_mode": mode,
            "parent_required": "Y" if str(case.get("parent_question_raw") or "") else "N",
            "handler_reachability": "registered" if resolved in known_actions else "screen_contract",
            "semantic_axis_a_prescription": "Y" if any(token in case["question_raw"] for token in ("전문", "일반", "ETC", "OTC")) else "",
            "semantic_axis_b_insurance": "Y" if any(token in case["question_raw"] for token in ("보험", "비보험")) else "",
            "offline_result": result, "reasons": "|".join(reasons),
        })
    return rows


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--casebook", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--id-prefix", default="G250")
    parser.add_argument("--expected-count", type=int, default=250)
    args = parser.parse_args()
    rows = audit(args.casebook, id_prefix=args.id_prefix, expected_count=args.expected_count)
    _write_csv(args.output, rows)
    summary = {key: sum(row["offline_result"] == key for row in rows) for key in ("PASS", "ORACLE_REVIEW", "HARNESS_REVIEW", "SOURCE_REVIEW")}
    print(json.dumps({"rows": len(rows), **summary}, ensure_ascii=False))
    return 0 if summary["ORACLE_REVIEW"] == 0 and summary["SOURCE_REVIEW"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
