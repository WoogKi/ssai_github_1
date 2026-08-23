from __future__ import annotations

import argparse
import copy
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot import calculate_payload_checksum, snapshot_key_from_payload
from app.services.sql_server_snapshot_repository import INSPECTION_QUERY_TIMEOUT_SECONDS, SqlServerSnapshotRepository
from tools.check_ssai_analytics_snapshot_repository import _Connection, _State, _payload, _validator


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _load_tool_module():
    path = ROOT / "tools" / "inspect_approve_dashboard_inventory_frequency_snapshot.py"
    spec = importlib.util.spec_from_file_location("snapshot_approval_tool_fixture", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("approval tool module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _expected_args(payload: dict, generation: int, preserve_generation: int) -> argparse.Namespace:
    summary = dict(payload["summary"])
    diagnostics = dict(payload["source_diagnostics"])
    grade_counts = dict(summary["grade_counts"])
    return argparse.Namespace(
        company_id=int(payload["company_id"].replace("C", "")),
        evaluation_month=str(payload["evaluation_month"]),
        stock_code=list(payload["scope"]["stock_codes"]),
        generation=generation,
        preserve_generation=preserve_generation,
        expected_checksum=str(payload["checksum"]),
        expected_product_count=int(summary["product_count"]),
        expected_normal_event_count=int(summary["normal_event_count"]),
        expected_source_row_count=int(diagnostics["source_row_count"]),
        expected_grade_counts={key: int(grade_counts.get(key) or 0) for key in ("A", "B", "C", "D", "E", "X")},
        approve=True,
        approved_by="fixture-approver",
        approval_reason="fixture post approval verification",
    )


def test_approval_postcheck_completes_and_preserves_draft() -> None:
    state = _State()
    connections: list[_Connection] = []

    def factory() -> _Connection:
        connection = _Connection(state)
        connections.append(connection)
        return connection

    repository = SqlServerSnapshotRepository(
        reader_connection_factory=factory,
        writer_connection_factory=factory,
        payload_validator=_validator,
    )
    preserved_payload = _payload(company="1")
    key = snapshot_key_from_payload(preserved_payload)
    repository.publish(key, preserved_payload, created_by="fixture-generator")
    candidate_payload = copy.deepcopy(preserved_payload)
    candidate_payload["source_watermark"] = "fixture-generation-2"
    normal_event_count = int(candidate_payload["summary"]["normal_event_count"])
    candidate_payload["source_diagnostics"] = {
        "source_row_count": normal_event_count,
        "normal_positive_accepted_row_count": normal_event_count,
        "normal_positive_duplicate_row_count": 0,
        "normal_positive_conflicting_row_count": 0,
        "normal_positive_missing_key_row_count": 0,
        "normal_positive_nonintegral_row_count": 0,
        "normal_nonpositive_row_count": 0,
        "return_positive_row_count": 0,
        "return_nonpositive_row_count": 0,
        "other_tcode_row_count": 0,
    }
    candidate_payload["checksum"] = calculate_payload_checksum(candidate_payload)
    candidate = repository.publish(key, candidate_payload, created_by="fixture-generator", force=True)
    _assert(candidate.generation_no == 2, "candidate must be generation 2")

    tool = _load_tool_module()
    output = tool.run_approval_workflow(_expected_args(candidate_payload, 2, 1), repository)
    expected_stages = {
        "pre_approval_inspect_generation",
        "pre_approval_verification",
        "preserve_generation_inspect",
        "approve_checked",
        "post_approval_inspect_generation",
        "post_approval_verification",
        "preserved_after_inspect_generation",
    }
    _assert(set(output["stage_elapsed_ms"]) == expected_stages, "all approval/post-check stages must finish once")
    _assert(output["approval"] == "ready" and output["approved_generation"] == 2, "candidate must publish")
    _assert(output["preserved_generation"]["generation_no"] == 1, "preserved generation must be reported")
    _assert(output["preserved_generation"]["manifest_status"] == "draft", "preserved generation remains draft")
    _assert(all(getattr(conn, "timeout", None) == INSPECTION_QUERY_TIMEOUT_SECONDS for conn in connections if conn.commits == 0), "inspection readers use bounded statement timeout")


def test_approval_without_preserved_draft_supersedes_published_generation() -> None:
    state = _State()
    factory = lambda: _Connection(state)
    repository = SqlServerSnapshotRepository(
        reader_connection_factory=factory,
        writer_connection_factory=factory,
        payload_validator=_validator,
    )
    published_payload = _payload(company="1")
    key = snapshot_key_from_payload(published_payload)
    first = repository.publish(key, published_payload, created_by="fixture-generator")
    repository.approve_checked(
        key,
        int(first.generation_no or 0),
        expected_checksum=str(published_payload["checksum"]),
        approved_by="fixture-approver",
        approval_reason="fixture initial approval",
    )
    candidate_payload = copy.deepcopy(published_payload)
    candidate_payload["source_watermark"] = "fixture-generation-2-no-preserve"
    normal_event_count = int(candidate_payload["summary"]["normal_event_count"])
    candidate_payload["source_diagnostics"] = {
        "source_row_count": normal_event_count,
        "normal_positive_accepted_row_count": normal_event_count,
        "normal_positive_duplicate_row_count": 0,
        "normal_positive_conflicting_row_count": 0,
        "normal_positive_missing_key_row_count": 0,
        "normal_positive_nonintegral_row_count": 0,
        "normal_nonpositive_row_count": 0,
        "return_positive_row_count": 0,
        "return_nonpositive_row_count": 0,
        "other_tcode_row_count": 0,
    }
    candidate_payload["checksum"] = calculate_payload_checksum(candidate_payload)
    candidate = repository.publish(key, candidate_payload, created_by="fixture-generator", force=True)
    _assert(candidate.generation_no == 2, "candidate must be generation 2")

    tool = _load_tool_module()
    output = tool.run_approval_workflow(_expected_args(candidate_payload, 2, 0), repository)
    expected_stages = {
        "pre_approval_inspect_generation",
        "pre_approval_verification",
        "approve_checked",
        "post_approval_inspect_generation",
        "post_approval_verification",
    }
    _assert(set(output["stage_elapsed_ms"]) == expected_stages, "no-preserve flow must skip only preserve checks")
    _assert("preserved_generation" not in output, "no-preserve output must not invent a preserved draft")
    _assert(output["approval"] == "ready" and output["approved_generation"] == 2, "draft 2 must publish")
    _assert(
        any(item["generation_no"] == 1 and item["status"] == "superseded" for item in state.manifests),
        "existing published generation must be superseded atomically",
    )
    _assert(
        any(item["generation_no"] == 2 and item["status"] == "published" and item["approval_status"] == "approved" for item in state.manifests),
        "approved draft must become the published generation",
    )

def main() -> int:
    tests = [test_approval_postcheck_completes_and_preserves_draft, test_approval_without_preserved_draft_supersedes_published_generation]
    for test in tests:
        test()
    print(f"PASS: snapshot approval post-check tests={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
