#!/usr/bin/env python3
"""Self-contained regression checks for extract_nlq_review_cases.py."""

from __future__ import annotations

import csv
import json
import tempfile
from pathlib import Path

from extract_nlq_review_cases import (
    CASE_COLUMNS,
    classify_record,
    extract_review_cases,
)


def _record(
    request_id: str,
    question: str,
    *,
    status: str = "success",
    total_rows: int | None = 1,
    **updates,
) -> dict:
    record = {
        "occurred_at": "2026-08-05T10:00:00+09:00",
        "logged_at": "2026-08-05T10:00:01+09:00",
        "schema_version": "2.0",
        "request_id": request_id,
        "company_id": 4,
        "room_id": "room-a",
        "question": question,
        "normalized_question": question,
        "route": "analytics",
        "action": "품목별 매출 추세 분석",
        "canonical_action": "품목별 매출 추세 분석",
        "result_status": status,
        "total_rows": total_rows,
        "display_rows": total_rows,
        "full_source_rows": total_rows,
        "table_created": status == "success" and bool(total_rows),
        "source_call_count": 0,
        "notice_codes": [],
        "consistency_flags": {},
    }
    record.update(updates)
    return record


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _assert_issue(record: dict, issue_code: str) -> None:
    issues, _schema_gaps, _flags = classify_record(record)
    if issue_code not in issues:
        raise AssertionError(f"expected {issue_code!r}, got {issues!r} for {record!r}")


def _assert_schema_gap(record: dict, schema_gap_code: str) -> None:
    issues, schema_gaps, _flags = classify_record(record)
    if issues or schema_gap_code not in schema_gaps:
        raise AssertionError(
            f"expected schema-gap-only {schema_gap_code!r}, got issues={issues!r}, gaps={schema_gaps!r}"
        )


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="check_nlq_review_") as temp_dir:
        root = Path(temp_dir)
        source = root / "fixture.jsonl"
        output = root / "out"
        records = [
            _record("ok", "정상 성공 사례", requested_metric="sales", requested_grouping="product", result_metric="sales", result_grain="product", execution_status="success"),
            _record("column", "컬럼 없음", status="column_unavailable", total_rows=0, table_created=False),
            _record("unsupported", "미지원 요청", status="unsupported", total_rows=0, table_created=False),
            _record("no-data", "자료 없음", status="no_data", total_rows=0, table_created=False),
            _record("grain", "그룹 불일치", requested_metric="sales", requested_grouping="product", result_metric="sales", result_grain="manufacturer", execution_status="success"),
            _record("metric", "지표 불일치", requested_metric="sales", requested_grouping="product", result_metric="quantity", result_grain="product", execution_status="success"),
            _record("status", "상태 불일치", status="no_data", total_rows=3, table_created=True),
            _record("fallback", "대체 실행", issue_codes=["fallback_substitution"], resolved_action="다른 작업", execution_status="fail", table_created=False),
            _record("missing", "메타 누락", requested_metric="sales", result_metric="sales", execution_status="success"),
            _record("silent", "명시적 무응답", status="silent_response", total_rows=0, table_created=False, notice_codes=["silent_response"]),
            _record("duplicate-1", "한글 중복 질문", status="unsupported", total_rows=0, table_created=False),
            _record("duplicate-2", "한글   중복 질문", status="unsupported", total_rows=0, table_created=False),
            _record("schema-gap", "과거 스키마 정상", consistency_flags={"display_source_status_missing": True, "result_status_derived": True}),
            _record("general-false", "20260801 출고명세 조회", route="sims", action="출고명세 조회", canonical_action="출고명세 조회", table_created=False),
            _record("current-table-false", "현재표 제품별 매출 분석", requested_metric="sales", requested_grouping="product", result_metric="sales", result_grain="product", execution_status="success", table_created=False),
            _record("fixture-old", "계약 중복 질문", requested_metric="sales", requested_grouping="product", result_metric="quantity", result_grain="product", execution_status="success"),
            _record("fixture-new", "계약 중복 질문", requested_metric="sales", requested_grouping="product", result_metric="stock", result_grain="product", execution_status="success", occurred_at="2026-08-05T11:00:00+09:00"),
            _record("formula", "=SUM(1,1)", status="unsupported", total_rows=0, table_created=False, action="+DDE", canonical_action="+DDE"),
        ]
        with source.open("w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.write("{malformed json\n")

        _assert_issue(records[1], "column_unavailable")
        _assert_issue(records[2], "unsupported")
        _assert_issue(records[3], "no_data")
        _assert_issue(records[4], "grain_mismatch")
        _assert_issue(records[5], "metric_mismatch")
        _assert_issue(records[6], "status_inconsistent")
        _assert_issue(records[7], "fallback_substitution")
        _assert_issue(records[8], "metadata_missing")
        _assert_issue(records[9], "silent_response")
        _assert_schema_gap(records[12], "display_source_status_missing")
        _assert_issue(records[14], "status_inconsistent")
        ok_issues, _ok_schema_gaps, _ = classify_record(records[0])
        if ok_issues:
            raise AssertionError(f"normal success unexpectedly selected: {ok_issues!r}")
        general_issues, _general_gaps, _ = classify_record(records[13])
        if general_issues:
            raise AssertionError(f"general lookup table_created=False false positive: {general_issues!r}")

        stats = extract_review_cases([source], output)
        if stats["valid_record_count"] != len(records) or stats["malformed_count"] != 1:
            raise AssertionError(f"stream stats mismatch: {stats!r}")
        cases = _read_csv(output / "nlq_review_cases.csv")
        if any(row["request_id"] == "ok" for row in cases):
            raise AssertionError("normal success must not be selected without --include-all")
        if any(row["request_id"] in {"schema-gap", "general-false"} for row in cases):
            raise AssertionError("schema-gap-only/general success must not be selected by default")
        dedup = _read_csv(output / "nlq_review_questions_dedup.csv")
        duplicate = [row for row in dedup if row["normalized_question"] == "한글 중복 질문"]
        if len(duplicate) != 1 or duplicate[0]["occurrence_count"] != "2":
            raise AssertionError(f"duplicate normalization mismatch: {duplicate!r}")
        fixtures = _read_csv(output / "nlq_review_fixture_candidates.csv")
        if not fixtures or not all(row["recommended_regression"] for row in fixtures):
            raise AssertionError("fixture recommendations missing")
        if any(row["question"] == "메타 누락" for row in fixtures):
            raise AssertionError("metadata-only case must not become a fixture candidate")
        contract_fixtures = [row for row in fixtures if row["question"] == "계약 중복 질문"]
        if len(contract_fixtures) != 1 or contract_fixtures[0]["observed_metric"] != "stock":
            raise AssertionError(f"fixture latest representative mismatch: {contract_fixtures!r}")
        formula_cases = [row for row in cases if row["request_id"] == "formula"]
        if (
            len(formula_cases) != 1
            or formula_cases[0]["question"] != "'=SUM(1,1)"
            or formula_cases[0]["canonical_action"] != "'+DDE"
        ):
            raise AssertionError(f"CSV formula protection mismatch: {formula_cases!r}")
        for csv_path in output.glob("*.csv"):
            if not csv_path.read_bytes().startswith(b"\xef\xbb\xbf"):
                raise AssertionError(f"UTF-8 BOM missing: {csv_path}")
        if "한글" not in (output / "nlq_review_cases.csv").read_text(encoding="utf-8-sig"):
            raise AssertionError("Korean CSV content missing")
        if tuple(cases[0].keys()) != CASE_COLUMNS:
            raise AssertionError("review CSV header mismatch")

        include_all_out = root / "include_all"
        include_all = extract_review_cases([source], include_all_out, include_all=True)
        if include_all["review_case_count"] != len(records):
            raise AssertionError(f"--include-all mismatch: {include_all!r}")
        include_all_cases = _read_csv(include_all_out / "nlq_review_cases.csv")
        schema_gap_rows = [row for row in include_all_cases if row["request_id"] == "schema-gap"]
        if len(schema_gap_rows) != 1 or "display_source_status_missing" not in schema_gap_rows[0]["schema_gap_codes"]:
            raise AssertionError(f"--include-all schema gap missing: {schema_gap_rows!r}")

        empty_source = root / "empty.jsonl"
        empty_source.write_text("", encoding="utf-8")
        empty_out = root / "empty_out"
        empty_stats = extract_review_cases([empty_source], empty_out)
        if empty_stats["review_case_count"] != 0:
            raise AssertionError(f"empty input mismatch: {empty_stats!r}")
        for csv_path in empty_out.glob("*.csv"):
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                if len(list(csv.reader(handle))) != 1:
                    raise AssertionError(f"empty CSV must contain header only: {csv_path}")

        try:
            extract_review_cases([root / "missing.jsonl"], root / "missing_out")
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("missing input must raise FileNotFoundError")


def main() -> int:
    run()
    print("RESULT: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
