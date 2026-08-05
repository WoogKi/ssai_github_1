#!/usr/bin/env python3
"""Extract review candidates from SSAI NLQ JSONL logs without external calls."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import datetime, time, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


REVIEW_RESULT_STATUSES = {
    "column_unavailable",
    "unsupported",
    "no_data",
    "routing_error",
}

FIXTURE_ISSUE_CODES = REVIEW_RESULT_STATUSES | {
    "grain_mismatch",
    "metric_mismatch",
    "fallback_substitution",
    "status_inconsistent",
    "result_contract_mismatch",
    "silent_response",
}

SCHEMA_GAP_CODES = {
    "display_source_status_missing",
    "full_source_status_missing",
    "interpretation_missing_fields",
    "raw_result_status_missing",
    "result_status_derived",
    "occurred_at_missing",
    "elapsed_missing",
}

CASE_COLUMNS = (
    "source_file",
    "source_line",
    "occurred_at",
    "request_id",
    "company_id",
    "room_id",
    "question",
    "normalized_question",
    "canonical_action",
    "result_status",
    "issue_codes",
    "schema_gap_codes",
    "requested_metric",
    "requested_grouping",
    "result_metric",
    "result_grain",
    "total_rows",
    "display_rows",
    "full_source_rows",
    "table_created",
    "source_call_count",
    "notice_codes",
    "consistency_flags",
    "review_status",
    "review_note",
    "fixture_target",
    "raw_record_json",
)

SUMMARY_COLUMNS = (
    "issue_code",
    "case_count",
    "unique_question_count",
    "first_occurred_at",
    "last_occurred_at",
)

DEDUP_COLUMNS = (
    "normalized_question",
    "canonical_action",
    "issue_codes",
    "occurrence_count",
    "latest_request_id",
    "latest_result_status",
    "fixture_target",
)

FIXTURE_COLUMNS = (
    "question",
    "canonical_action",
    "expected_status",
    "expected_metric",
    "expected_grouping",
    "observed_metric",
    "observed_grain",
    "issue_codes",
    "recommended_regression",
)

_KST = timezone(timedelta(hours=9))


def _text(value: Any) -> str:
    return str(value or "").strip()


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return ""


def _list_values(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        return sorted(_text(key) for key, enabled in value.items() if bool(enabled) and _text(key))
    if isinstance(value, (list, tuple, set)):
        return sorted({_text(item) for item in value if _text(item)})
    text = _text(value)
    return [text] if text else []


def _active_consistency_flags(record: Mapping[str, Any]) -> list[str]:
    active = _list_values(record.get("consistency_flags"))
    active.extend(_list_values(record.get("intent_consistency_flags")))
    return sorted(set(active))


def _normalize_question(record: Mapping[str, Any]) -> str:
    value = _text(record.get("normalized_question") or record.get("question"))
    return re.sub(r"\s+", " ", value).strip()


def _canonical_action(record: Mapping[str, Any]) -> str:
    interpretation = record.get("interpretation")
    if not isinstance(interpretation, Mapping):
        interpretation = {}
    return _text(
        record.get("canonical_action")
        or record.get("resolved_action")
        or interpretation.get("canonical_action")
        or record.get("action")
    )


def _occurred_at(record: Mapping[str, Any]) -> str:
    return _text(record.get("occurred_at") or record.get("logged_at"))


def _parse_datetime(value: str, *, is_to: bool = False) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", text))
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid time value: {value!r}") from exc
    if date_only:
        parsed = datetime.combine(parsed.date(), time.max if is_to else time.min)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_KST)
    return parsed.astimezone(timezone.utc)


def _record_datetime(record: Mapping[str, Any]) -> datetime | None:
    value = _occurred_at(record)
    if not value:
        return None
    try:
        return _parse_datetime(value)
    except ValueError:
        return None


def _has_explicit_silent_response(record: Mapping[str, Any], issue_codes: Iterable[str]) -> bool:
    evidence = {
        _text(record.get("result_status")).lower(),
        _text(record.get("raw_result_status")).lower(),
        *(_text(code).lower() for code in issue_codes),
        *(_text(code).lower() for code in _list_values(record.get("notice_codes"))),
        *(_text(code).lower() for code in _active_consistency_flags(record)),
    }
    return bool({"silent_response", "response_missing", "empty_response"} & evidence)


def classify_record(record: Mapping[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """Return review issues, schema gaps, and all active consistency flags."""
    explicit_issues = set(_list_values(record.get("issue_codes")))
    flags = _active_consistency_flags(record)
    schema_gaps = (explicit_issues | set(flags)) & SCHEMA_GAP_CODES
    issues = (explicit_issues | set(flags)) - schema_gaps

    status = _text(record.get("result_status")).lower()
    if status in REVIEW_RESULT_STATUSES:
        issues.add(status)

    requested_metric = _text(record.get("requested_metric"))
    requested_grouping = _text(record.get("requested_grouping"))
    result_metric = _text(record.get("result_metric"))
    result_grain = _text(record.get("result_grain"))
    if requested_grouping and result_grain and requested_grouping != result_grain:
        issues.add("grain_mismatch")
    if requested_metric and result_metric and requested_metric != result_metric:
        issues.add("metric_mismatch")

    total_rows = _integer(record.get("total_rows"))
    display_rows = _integer(record.get("display_rows"))
    full_source_rows = _integer(record.get("full_source_rows"))
    observed_rows = total_rows
    if observed_rows is None:
        observed_rows = full_source_rows if full_source_rows is not None else display_rows
    table_created = record.get("table_created") if isinstance(record.get("table_created"), bool) else None
    notice_codes = set(_list_values(record.get("notice_codes")))
    current_table_scope = bool(
        "현재표" in f"{_canonical_action(record)} {_normalize_question(record)}"
        or _text(record.get("source_table_key"))
        or _text(record.get("source_action"))
    )
    metric_grouping_contract = any(
        _text(record.get(key))
        for key in ("requested_metric", "requested_grouping", "result_metric", "result_grain")
    )
    table_contract_scope = current_table_scope or metric_grouping_contract

    status_conflict = (
        (status == "success" and observed_rows == 0)
        or (status == "no_data" and bool((observed_rows or 0) > 0))
        or (
            table_contract_scope
            and status in {"column_unavailable", "unsupported", "no_data", "routing_error"}
            and table_created is True
        )
        or (table_contract_scope and status == "success" and table_created is False)
        or (bool((observed_rows or 0) > 0) and "entity_not_found" in notice_codes)
    )
    if status_conflict:
        issues.add("status_inconsistent")

    intent_status = _text(record.get("intent_validation_status")).lower()
    resolved_action = _text(record.get("resolved_action"))
    canonical_action = _canonical_action(record)
    explicit_contract_codes = set(_list_values(record.get("issue_codes"))) | set(flags)
    if any("fallback" in code.lower() or "substitution" in code.lower() for code in explicit_contract_codes):
        issues.add("fallback_substitution")
    elif intent_status == "fail" and resolved_action and canonical_action and resolved_action != canonical_action:
        issues.add("fallback_substitution")

    if any("result_contract_mismatch" in code for code in explicit_contract_codes):
        issues.add("result_contract_mismatch")

    missing_base = []
    if not _text(record.get("request_id")):
        missing_base.append("request_id")
    if not _normalize_question(record):
        missing_base.append("question")
    if not status:
        missing_base.append("result_status")
    if not canonical_action:
        missing_base.append("canonical_action")

    contract_present = any(
        value not in (None, "", [], {})
        for value in (
            record.get("requested_metric"),
            record.get("requested_grouping"),
            record.get("result_metric"),
            record.get("result_grain"),
            record.get("execution_status"),
        )
    )
    missing_contract = []
    if contract_present:
        for key in ("requested_metric", "requested_grouping", "execution_status"):
            if not _text(record.get(key)):
                missing_contract.append(key)
        if status == "success":
            for key in ("result_metric", "result_grain"):
                if not _text(record.get(key)):
                    missing_contract.append(key)
            if table_created is None:
                missing_contract.append("table_created")

    explicit_missing_flags = [
        flag for flag in flags
        if flag in {"occurred_at_missing", "total_rows_missing", "action_missing"}
        or (contract_present and flag == "source_call_count_missing")
    ]
    if missing_base or missing_contract or explicit_missing_flags:
        issues.add("metadata_missing")

    if _has_explicit_silent_response(record, issues):
        issues.add("silent_response")

    return sorted(issues), sorted(schema_gaps), flags


def recommend_fixture_target(record: Mapping[str, Any], issue_codes: Sequence[str]) -> str:
    route = _text(record.get("route")).lower()
    action = _canonical_action(record)
    combined = f"{action} {_normalize_question(record)}"
    issue_set = set(issue_codes)
    if issue_set & {"routing_error", "fallback_substitution", "result_contract_mismatch"}:
        return "ActionInventory"
    if route == "analytics" or _text(record.get("source_table_key")) or any(
        _text(record.get(key)) for key in ("requested_metric", "requested_grouping", "result_metric", "result_grain")
    ):
        return "Analytics_CurrentTable"
    if any(token in combined for token in ("입고", "출고", "수불", "재고", "주문", "발주")):
        return "IO"
    if any(token in combined for token in ("사용자", "거래처", "제품", "품목", "코드", "부서", "사원")):
        return "Master"
    return "ManualReview"


def _review_note(issue_codes: Sequence[str], schema_gap_codes: Sequence[str]) -> str:
    issue_set = set(issue_codes)
    if not issue_set and schema_gap_codes:
        return "과거 schema 또는 진단 필드 공백이며 기본 검토 대상에서는 제외됩니다."
    if issue_set and issue_set <= REVIEW_RESULT_STATUSES:
        return "정상 운영상태일 수 있으며 결함 여부는 사람이 확인해야 합니다."
    if "silent_response" in issue_set:
        return "로그에 명시된 무응답 근거가 있어 검토 대상으로 분류했습니다."
    return "자동 규칙으로 선별된 사례이며 결함 여부는 확정되지 않았습니다."


def _case_values(
    record: Mapping[str, Any],
    *,
    source_file: Path,
    source_line: int,
    raw_json: str,
    issue_codes: Sequence[str],
    schema_gap_codes: Sequence[str],
    consistency_flags: Sequence[str],
) -> dict[str, Any]:
    fixture_target = recommend_fixture_target(record, issue_codes)
    return {
        "source_file": str(source_file.resolve()),
        "source_line": source_line,
        "occurred_at": _occurred_at(record),
        "request_id": _text(record.get("request_id")),
        "company_id": _text(record.get("company_id")),
        "room_id": _text(record.get("room_id")),
        "question": _text(record.get("question")),
        "normalized_question": _normalize_question(record),
        "canonical_action": _canonical_action(record),
        "result_status": _text(record.get("result_status")),
        "issue_codes": ";".join(issue_codes),
        "schema_gap_codes": ";".join(schema_gap_codes),
        "requested_metric": _text(record.get("requested_metric")),
        "requested_grouping": _text(record.get("requested_grouping")),
        "result_metric": _text(record.get("result_metric")),
        "result_grain": _text(record.get("result_grain")),
        "total_rows": _integer(record.get("total_rows")),
        "display_rows": _integer(record.get("display_rows")),
        "full_source_rows": _integer(record.get("full_source_rows")),
        "table_created": _bool_text(record.get("table_created")),
        "source_call_count": _integer(record.get("source_call_count")),
        "notice_codes": ";".join(_list_values(record.get("notice_codes"))),
        "consistency_flags": ";".join(consistency_flags),
        "review_status": "pending",
        "review_note": _review_note(issue_codes, schema_gap_codes),
        "fixture_target": fixture_target,
        "raw_record_json": raw_json,
    }


def _open_csv(path: Path, columns: Sequence[str]):
    handle = path.open("w", encoding="utf-8-sig", newline="")
    writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
    writer.writeheader()
    return handle, writer


def _excel_safe(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _write_csv_row(writer: csv.DictWriter, values: Mapping[str, Any]) -> None:
    writer.writerow({key: _excel_safe(value) for key, value in values.items()})


def _create_schema(connection: sqlite3.Connection) -> None:
    column_sql = ",".join(f'"{column}" TEXT' for column in CASE_COLUMNS)
    connection.execute(
        f"CREATE TABLE cases (case_id INTEGER PRIMARY KEY, sort_time TEXT NOT NULL, fixture_candidate INTEGER NOT NULL, {column_sql})"
    )
    connection.execute("CREATE TABLE case_issues (case_id INTEGER NOT NULL, issue_code TEXT NOT NULL)")
    connection.execute("CREATE INDEX idx_cases_sort ON cases(sort_time, source_file, source_line, request_id)")
    connection.execute("CREATE INDEX idx_case_issues ON case_issues(issue_code, case_id)")


def _insert_case(connection: sqlite3.Connection, values: Mapping[str, Any], issue_codes: Sequence[str]) -> None:
    columns = list(CASE_COLUMNS)
    placeholders = ",".join("?" for _ in columns)
    sort_time = _text(values.get("occurred_at")) or "9999-12-31T23:59:59"
    fixture_candidate = int(bool(set(issue_codes) & FIXTURE_ISSUE_CODES))
    cursor = connection.execute(
        f"INSERT INTO cases (sort_time, fixture_candidate, {','.join(columns)}) VALUES (?, ?, {placeholders})",
        [sort_time, fixture_candidate, *("" if values.get(column) is None else values.get(column) for column in columns)],
    )
    case_id = int(cursor.lastrowid)
    connection.executemany(
        "INSERT INTO case_issues (case_id, issue_code) VALUES (?, ?)",
        [(case_id, code) for code in issue_codes],
    )


def _write_outputs(connection: sqlite3.Connection, output_dir: Path) -> dict[str, int]:
    case_path = output_dir / "nlq_review_cases.csv"
    handle, writer = _open_csv(case_path, CASE_COLUMNS)
    try:
        query = f"SELECT {','.join(CASE_COLUMNS)} FROM cases ORDER BY sort_time, source_file, CAST(source_line AS INTEGER), request_id"
        for row in connection.execute(query):
            _write_csv_row(writer, dict(zip(CASE_COLUMNS, row)))
    finally:
        handle.close()

    summary_path = output_dir / "nlq_review_issue_summary.csv"
    handle, writer = _open_csv(summary_path, SUMMARY_COLUMNS)
    issue_counts: dict[str, int] = {}
    try:
        query = """
            SELECT i.issue_code, COUNT(*), COUNT(DISTINCT c.normalized_question),
                   MIN(NULLIF(c.occurred_at, '')), MAX(NULLIF(c.occurred_at, ''))
              FROM case_issues i JOIN cases c ON c.case_id = i.case_id
             GROUP BY i.issue_code ORDER BY i.issue_code
        """
        for row in connection.execute(query):
            values = dict(zip(SUMMARY_COLUMNS, row))
            _write_csv_row(writer, values)
            issue_counts[str(row[0])] = int(row[1])
    finally:
        handle.close()

    dedup_path = output_dir / "nlq_review_questions_dedup.csv"
    handle, writer = _open_csv(dedup_path, DEDUP_COLUMNS)
    dedup_count = 0
    try:
        groups = connection.execute(
            """
            SELECT normalized_question, canonical_action, COUNT(*)
              FROM cases GROUP BY normalized_question, canonical_action
             ORDER BY normalized_question, canonical_action
            """
        )
        for normalized_question, canonical_action, occurrence_count in groups:
            issue_codes = [
                row[0] for row in connection.execute(
                    """
                    SELECT DISTINCT i.issue_code
                      FROM case_issues i JOIN cases c ON c.case_id=i.case_id
                     WHERE c.normalized_question=? AND c.canonical_action=?
                     ORDER BY i.issue_code
                    """,
                    (normalized_question, canonical_action),
                )
            ]
            latest = connection.execute(
                """
                SELECT request_id, result_status, fixture_target FROM cases
                 WHERE normalized_question=? AND canonical_action=?
                 ORDER BY sort_time DESC, CAST(source_line AS INTEGER) DESC, request_id DESC LIMIT 1
                """,
                (normalized_question, canonical_action),
            ).fetchone()
            _write_csv_row(writer, {
                "normalized_question": normalized_question,
                "canonical_action": canonical_action,
                "issue_codes": ";".join(issue_codes),
                "occurrence_count": occurrence_count,
                "latest_request_id": latest[0] if latest else "",
                "latest_result_status": latest[1] if latest else "",
                "fixture_target": latest[2] if latest else "ManualReview",
            })
            dedup_count += 1
    finally:
        handle.close()

    fixture_path = output_dir / "nlq_review_fixture_candidates.csv"
    handle, writer = _open_csv(fixture_path, FIXTURE_COLUMNS)
    fixture_count = 0
    try:
        query = """
            WITH ranked AS (
                SELECT question, canonical_action, result_status, requested_metric,
                       requested_grouping, result_metric, result_grain, issue_codes, fixture_target,
                       normalized_question, sort_time, source_line, request_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY normalized_question, canonical_action, result_status,
                                        requested_metric, requested_grouping
                           ORDER BY sort_time DESC, CAST(source_line AS INTEGER) DESC, request_id DESC
                       ) AS candidate_rank
                  FROM cases WHERE fixture_candidate=1
            )
            SELECT question, canonical_action, result_status, requested_metric,
                   requested_grouping, result_metric, result_grain, issue_codes, fixture_target
              FROM ranked WHERE candidate_rank=1
             ORDER BY fixture_target, canonical_action, normalized_question,
                      result_status, requested_metric, requested_grouping
        """
        for row in connection.execute(query):
            _write_csv_row(writer, dict(zip(FIXTURE_COLUMNS, row)))
            fixture_count += 1
    finally:
        handle.close()

    return {
        "review_case_count": int(connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0]),
        "dedup_question_count": dedup_count,
        "fixture_candidate_count": fixture_count,
        "fixture_raw_candidate_count": int(
            connection.execute("SELECT COUNT(*) FROM cases WHERE fixture_candidate=1").fetchone()[0]
        ),
        "issue_counts": issue_counts,
    }


def extract_review_cases(
    input_paths: Sequence[Path],
    output_dir: Path,
    *,
    from_time: str = "",
    to_time: str = "",
    company_id: str = "",
    room_id: str = "",
    include_all: bool = False,
) -> dict[str, Any]:
    paths = [Path(path) for path in input_paths]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError("input JSONL not found: " + ", ".join(missing))
    output_dir.mkdir(parents=True, exist_ok=True)
    from_dt = _parse_datetime(from_time) if from_time else None
    to_dt = _parse_datetime(to_time, is_to=True) if to_time else None

    stats: dict[str, Any] = {
        "input_files": len(paths),
        "read_line_count": 0,
        "valid_record_count": 0,
        "malformed_count": 0,
        "filtered_out_count": 0,
    }
    schema_gap_counts: Counter[str] = Counter()
    schema_gap_only_count = 0
    review_issue_record_count = 0
    with tempfile.TemporaryDirectory(prefix="nlq_review_") as temp_dir:
        connection = sqlite3.connect(str(Path(temp_dir) / "review.sqlite3"))
        try:
            _create_schema(connection)
            for source_path in paths:
                with source_path.open("r", encoding="utf-8-sig", errors="replace") as source:
                    for source_line, raw_line in enumerate(source, 1):
                        stats["read_line_count"] += 1
                        raw_json = raw_line.rstrip("\r\n")
                        if not raw_json.strip():
                            stats["malformed_count"] += 1
                            continue
                        try:
                            record = json.loads(raw_json)
                        except (json.JSONDecodeError, TypeError):
                            stats["malformed_count"] += 1
                            continue
                        if not isinstance(record, Mapping):
                            stats["malformed_count"] += 1
                            continue
                        stats["valid_record_count"] += 1

                        occurred = _record_datetime(record)
                        if from_dt is not None and (occurred is None or occurred < from_dt):
                            stats["filtered_out_count"] += 1
                            continue
                        if to_dt is not None and (occurred is None or occurred > to_dt):
                            stats["filtered_out_count"] += 1
                            continue
                        if company_id and _text(record.get("company_id")) != _text(company_id):
                            stats["filtered_out_count"] += 1
                            continue
                        if room_id and _text(record.get("room_id")) != _text(room_id):
                            stats["filtered_out_count"] += 1
                            continue

                        issue_codes, schema_gap_codes, consistency_flags = classify_record(record)
                        schema_gap_counts.update(schema_gap_codes)
                        if schema_gap_codes and not issue_codes:
                            schema_gap_only_count += 1
                        if issue_codes:
                            review_issue_record_count += 1
                        if not include_all and not issue_codes:
                            continue
                        values = _case_values(
                            record,
                            source_file=source_path,
                            source_line=source_line,
                            raw_json=raw_json,
                            issue_codes=issue_codes,
                            schema_gap_codes=schema_gap_codes,
                            consistency_flags=consistency_flags,
                        )
                        _insert_case(connection, values, issue_codes)
            connection.commit()
            stats.update(_write_outputs(connection, output_dir))
            stats["review_issue_record_count"] = review_issue_record_count
            stats["schema_gap_only_count"] = schema_gap_only_count
            stats["schema_gap_counts"] = dict(sorted(schema_gap_counts.items()))
            stats["top_review_cases"] = [
                {
                    "issue_codes": row[0],
                    "canonical_action": row[1],
                    "question": row[2],
                    "request_id": row[3],
                }
                for row in connection.execute(
                    """
                    SELECT issue_codes, canonical_action, question, request_id FROM cases
                     ORDER BY CASE WHEN result_status IN ('routing_error','unsupported','column_unavailable') THEN 0 ELSE 1 END,
                              sort_time DESC, source_file, CAST(source_line AS INTEGER)
                     LIMIT 20
                    """
                )
            ]
        finally:
            connection.close()
    return stats


def _flatten_inputs(values: Sequence[Sequence[str]]) -> list[Path]:
    return [Path(item) for group in values for item in group]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract NLQ cases that need human review")
    parser.add_argument(
        "--input",
        action="append",
        nargs="+",
        required=True,
        help="Input nlq_cases.jsonl path; repeat --input for multiple files",
    )
    parser.add_argument("--output-dir", required=True, help="Directory for four review CSV files")
    parser.add_argument("--from-time", default="", help="Inclusive ISO timestamp/date filter")
    parser.add_argument("--to-time", default="", help="Inclusive ISO timestamp/date filter")
    parser.add_argument("--company-id", default="", help="Exact company_id filter")
    parser.add_argument("--room-id", default="", help="Exact room_id filter")
    parser.add_argument("--include-all", action="store_true", help="Include records without review issues")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        stats = extract_review_cases(
            _flatten_inputs(args.input),
            Path(args.output_dir),
            from_time=args.from_time,
            to_time=args.to_time,
            company_id=args.company_id,
            room_id=args.room_id,
            include_all=bool(args.include_all),
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
