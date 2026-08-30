"""Focused no-DB regression checks for sims.response.v1."""

from __future__ import annotations

import copy
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.structured_response_contract import (  # noqa: E402
    SCHEMA_VERSION,
    build_structured_response_envelope,
    validate_structured_response_envelope,
)
from app.services.structured_tool_routing import (  # noqa: E402
    build_datetime_tool_route_decision,
    build_knowledge_rag_tool_route_decision,
    build_sims_internal_tool_route_decision,
    build_web_latest_tool_route_decision,
    validate_tool_route_decision,
)
from app.services.datetime_tool import resolve_datetime_question  # noqa: E402
from app.services.web_search_service import WebSearchPeriod, WebSearchResponse, WebSearchResult  # noqa: E402


def _signature(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _assert_legacy_unchanged(payload: dict, **kwargs: object) -> dict:
    before = _signature(payload)
    result = build_structured_response_envelope(payload, **kwargs)
    if _signature(payload) != before:
        raise AssertionError("legacy payload was mutated")
    validate_structured_response_envelope(result)
    if result["schema_version"] != SCHEMA_VERSION:
        raise AssertionError("schema version mismatch")
    return result


def _sims_payload(status: str, *, source_calls: int = 2) -> dict:
    return {
        "type": "table",
        "title": "출고명세 조회",
        "action": "출고명세 조회",
        "params": {"date_from": "20250101", "date_to": "20250131"},
        "meta": {
            "nlq": True,
            "route": "io",
            "canonical_action": "출고명세 조회",
            "result_status": status,
            "row_count": 10,
            "row_count_total": 15,
            "source_call_count": source_calls,
            "nlq_trace_request_id": "safe-request-id",
            "table_key": "result-table",
        },
    }


def check_sims_statuses() -> None:
    for status in ("success", "no_data", "input_required", "candidate_required", "resolution_unavailable", "timeout", "routing_error"):
        payload = _sims_payload(status)
        envelope = _assert_legacy_unchanged(payload)
        if envelope["execution"]["result_status"] != status:
            raise AssertionError(f"status changed: {status}")
        if envelope["execution"]["source_call_count"] != 2:
            raise AssertionError("source_call_count was recalculated")


def check_current_table_provenance() -> None:
    payload = _sims_payload("success", source_calls=0)
    payload["meta"].update(
        {
            "current_table_followup": True,
            "table_key": "derived-table",
            "source_table_key": "parent-source-table",
            "source_action": "출고명세 조회",
            "execution_status": "success",
        }
    )
    envelope = _assert_legacy_unchanged(payload)
    table = envelope["result"]["table"]
    if table["table_key"] != "derived-table" or table["source_table_key"] != "parent-source-table":
        raise AssertionError("current-table provenance changed")
    if envelope["execution"]["execution_status"] != "success":
        raise AssertionError("existing execution_status was lost")


def check_knowledge_security() -> None:
    ready = {
        "type": "knowledge_answer",
        "title": "Knowledge 답변",
        "knowledge_evidence": {
            "citations": [{"identifier": "citation-1", "document_id": "doc-1", "source_name": "approved.txt", "source_kind": "DOCUMENT"}],
            "conflict_notices": [{"conflict_group_id": "group-1", "message": "확인 필요"}],
        },
        "meta": {"message_type": "knowledge_answer"},
    }
    authorized = _assert_legacy_unchanged(copy.deepcopy(ready), authorized_knowledge_evidence=True)
    if authorized["evidence"]["kind"] != "knowledge" or authorized["evidence"]["citation_count"] != 1:
        raise AssertionError("authorized Knowledge evidence missing")
    denied = _assert_legacy_unchanged(copy.deepcopy(ready), authorized_knowledge_evidence=False)
    if denied["evidence"]["citation_count"] != 0 or denied["evidence"]["citations"]:
        raise AssertionError("unauthorized Knowledge evidence leaked")
    no_evidence = _assert_legacy_unchanged({"type": "knowledge_answer", "meta": {"message_type": "knowledge_answer"}})
    if no_evidence["evidence"]["citation_count"] != 0:
        raise AssertionError("empty Knowledge evidence leaked")


def check_knowledge_tool_route_decision() -> None:
    ready = {
        "type": "knowledge_answer",
        "knowledge_evidence": {
            "citations": [
                {
                    "identifier": "citation-1",
                    "document_id": "doc-1",
                    "source_name": "approved.txt",
                    "source_kind": "DOCUMENT",
                    "version": 1,
                }
            ],
            "conflict_notices": [{"conflict_group_id": "group-1", "message": "확인 필요"}],
        },
        "meta": {"message_type": "knowledge_answer", "reason_code": "ready"},
    }
    authorized_decision = build_knowledge_rag_tool_route_decision(
        technical_detail_mode=True,
        reason_code="ready",
    )
    validate_tool_route_decision(authorized_decision)
    authorized = _assert_legacy_unchanged(
        ready,
        authorized_knowledge_evidence=True,
        tool_route_decision=authorized_decision,
    )
    if (
        authorized["route"]["kind"] != "knowledge_rag"
        or authorized["route"]["decision_mode"] != "deterministic"
        or authorized["action"] != {"raw": "knowledge_chat", "canonical": "knowledge_chat"}
        or authorized["execution"]["reason_code"] != "ready"
        or authorized["evidence"]["citation_count"] != 1
    ):
        raise AssertionError("authorized Knowledge route decision was not mapped")
    if authorized["arguments"]["tool"] != {"technical_detail_mode": True}:
        raise AssertionError("Knowledge route decision copied unsafe arguments")

    for reason_code in ("missing_rag_use", "no_authorized_match"):
        denied = _assert_legacy_unchanged(
            {"type": "knowledge_answer", "meta": {"message_type": "knowledge_answer", "reason_code": reason_code}},
            tool_route_decision=build_knowledge_rag_tool_route_decision(
                technical_detail_mode=False,
                reason_code=reason_code,
            ),
        )
        evidence = denied["evidence"]
        if (
            denied["execution"]["reason_code"] != reason_code
            or evidence["citation_count"] != 0
            or evidence["citations"]
            or evidence["source_count"] != 0
            or evidence["kind"] != "none"
        ):
            raise AssertionError("denied or no-evidence Knowledge route leaked evidence")


def check_knowledge_runtime_trace_boundary() -> None:
    source = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    start = source.index("def _run_explicit_knowledge_chat(")
    end = source.index("\ndef _run_web_search_chat(", start)
    block = source[start:end]
    retrieve = block.index("repository.retrieve_for_chat(")
    decision = block.index("build_knowledge_rag_tool_route_decision(")
    ready_boundary = block.index("build_knowledge_answer_message(")
    authorized_envelope = block.index("authorized_knowledge_evidence=True")
    if not (retrieve < decision < ready_boundary < authorized_envelope):
        raise AssertionError("Knowledge trace crossed the authorization/evidence boundary")
    if "message[\"structured_response\"]" in block or "knowledge_evidence\"][\"structured" in block:
        raise AssertionError("Knowledge trace changed the persisted message shape")


def check_web_tool_route_decision() -> None:
    reference_at = datetime(2026, 8, 30, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    period = WebSearchPeriod("today", reference_at.date(), reference_at.date(), "pd")
    result = WebSearchResult(
        title="External source",
        url="https://example.test/news",
        source="example.test",
        snippet="fixture",
        published_at="2026-08-30",
    )
    ready_response = WebSearchResponse("ready", "latest question", reference_at, period, results=(result,))
    ready = _assert_legacy_unchanged(
        {
            "type": "text",
            "content": "legacy web answer",
            "meta": {
                "web_search": True,
                "status": "ready",
                "sources": [
                    {
                        "title": result.title,
                        "url": result.url,
                        "source": result.source,
                        "published_at": result.published_at,
                    }
                ],
            },
        },
        tool_route_decision=build_web_latest_tool_route_decision(ready_response),
    )
    if (
        ready["route"]["kind"] != "web_latest"
        or ready["route"]["decision_mode"] != "deterministic"
        or ready["action"] != {"raw": "web_search", "canonical": "web_search"}
        or ready["execution"]["result_status"] != "ready"
        or ready["execution"]["reason_code"] != "ready"
        or ready["evidence"]["kind"] != "web"
        or ready["evidence"]["source_count"] != 1
        or ready["evidence"].get("citations") != []
    ):
        raise AssertionError("ready Web route decision was not mapped")
    if ready["arguments"]["tool"] != {"period_kind": "today"}:
        raise AssertionError("Web route decision copied unsafe arguments")

    zero_source_response = WebSearchResponse("ready", "latest question", reference_at, period)
    zero_source = _assert_legacy_unchanged(
        {"type": "text", "meta": {"web_search": True, "status": "ready", "sources": []}},
        tool_route_decision=build_web_latest_tool_route_decision(zero_source_response),
    )
    if zero_source["evidence"]["kind"] != "web" or zero_source["evidence"]["source_count"] != 0:
        raise AssertionError("zero-source Web response created fake evidence")

    failed_response = WebSearchResponse(
        "failed",
        "latest question",
        reference_at,
        period,
        reason_code="search_failed",
    )
    failed = _assert_legacy_unchanged(
        {"type": "text", "meta": {"web_search": True, "status": "failed", "reason_code": "search_failed"}},
        tool_route_decision=build_web_latest_tool_route_decision(failed_response),
    )
    if (
        failed["execution"]["result_status"] != "failed"
        or failed["execution"]["reason_code"] != "search_failed"
        or failed["evidence"]["kind"] != "web"
        or failed["evidence"]["source_count"] != 0
        or failed["evidence"].get("citations") != []
    ):
        raise AssertionError("failed Web response changed status or created fake evidence")


def check_web_runtime_trace_boundary() -> None:
    source = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    start = source.index("def _run_web_search_chat(")
    end = source.index("\ndef _consume_company_change_notice(", start)
    block = source[start:end]
    search = block.index("response = search_web(route)")
    failed_decision = block.index("build_web_latest_tool_route_decision(response)")
    summary = block.index("llm_response = call_chat_protected(")
    ready_decision = block.rindex("build_web_latest_tool_route_decision(response)")
    ready_trace = block.rindex("web_envelope = build_structured_response_envelope(message")
    if not (search < failed_decision < summary < ready_decision < ready_trace):
        raise AssertionError("Web trace crossed the search or LLM summary boundary")
    if "message[\"structured_response\"]" in block:
        raise AssertionError("Web trace changed the persisted message shape")


def check_datetime_and_web() -> None:
    datetime_payload = {"type": "text", "action": "current_time", "message_type": "datetime_tool", "meta": {}}
    datetime_envelope = _assert_legacy_unchanged(datetime_payload)
    if datetime_envelope["route"]["kind"] != "datetime_tool" or datetime_envelope["execution"]["source_call_count"] != 0:
        raise AssertionError("datetime contract changed")
    web_payload = {"type": "text", "meta": {"web_search": True, "sources": [{"title": "source", "url": "https://example.test", "source": "test"}]}}
    web_envelope = _assert_legacy_unchanged(web_payload)
    if web_envelope["evidence"]["kind"] != "web" or web_envelope["evidence"]["source_count"] != 1:
        raise AssertionError("web evidence contract changed")


def check_datetime_tool_route_decision() -> None:
    cases = ("오늘 날짜", "현재 몇 시", "2026-08-30 요일")
    for question in cases:
        answer = resolve_datetime_question(question)
        if answer is None:
            raise AssertionError(f"datetime resolver declined: {question}")
        decision = build_datetime_tool_route_decision(answer)
        validate_tool_route_decision(decision)
        legacy = {
            "type": "text",
            "content": answer.text,
            "message_type": "datetime_tool",
            "meta": {},
        }
        envelope = _assert_legacy_unchanged(legacy, tool_route_decision=decision)
        if envelope["route"]["kind"] != "datetime_tool" or envelope["route"]["decision_mode"] != "deterministic":
            raise AssertionError("datetime decision was not mapped")
        if envelope["action"]["raw"] != answer.intent or envelope["action"]["canonical"] != answer.intent:
            raise AssertionError("datetime action changed")
        if envelope["execution"]["source_call_count"] != 0:
            raise AssertionError("datetime source call count changed")
    if resolve_datetime_question("입고현황 20260828") is not None:
        raise AssertionError("non-date question created a datetime decision")


def check_sims_internal_tool_route_decisions() -> None:
    cases = (
        (
            "sims_nlq",
            {
                "action": "그룹코드조회",
                "params": {"group_code": "A"},
                "meta": {"nlq": True, "result_status": "success", "source_call_count": 1},
            },
        ),
        (
            "analytics",
            {
                "action": "매출처별 매출 예상",
                "params": {"month_from": "202601"},
                "meta": {"nlq": True, "analysis_nlq": True, "result_status": "success"},
            },
        ),
        (
            "io",
            {
                "action": "출고명세 조회",
                "params": {"date_from": "20260801"},
                "meta": {"nlq": True, "result_status": "no_data", "source_call_count": 2},
            },
        ),
        (
            "vendor_master",
            {
                "action": "거래처 목록",
                "params": {"ven_nm": "약국"},
                "meta": {"nlq": True, "result_status": "success"},
            },
        ),
        (
            "current_table",
            {
                "action": "현재표 제조사별 집계",
                "params": {"group_column": "제조사명"},
                "meta": {
                    "nlq": True,
                    "current_table_followup": True,
                    "result_status": "column_unavailable",
                    "table_key": "derived-table",
                    "source_table_key": "parent-source-table",
                    "source_action": "출고명세 조회",
                },
            },
        ),
    )
    for expected_kind, payload in cases:
        decision = build_sims_internal_tool_route_decision(payload)
        if decision is None:
            raise AssertionError(f"missing SIMS decision: {expected_kind}")
        validate_tool_route_decision(decision)
        envelope = _assert_legacy_unchanged(payload, tool_route_decision=decision)
        if envelope["route"]["kind"] != expected_kind:
            raise AssertionError(f"SIMS route changed: expected={expected_kind} actual={envelope['route']['kind']}")
        if envelope["execution"]["result_status"] != payload["meta"]["result_status"]:
            raise AssertionError("SIMS result_status was reinterpreted")
        if expected_kind == "io" and envelope["execution"]["source_call_count"] != 2:
            raise AssertionError("IO source_call_count was recalculated")
        if expected_kind == "current_table":
            table = envelope["result"]["table"]
            if table["table_key"] != "derived-table" or table["source_table_key"] != "parent-source-table":
                raise AssertionError("current-table source provenance changed")

    generic_payload = {"action": "미확정", "meta": {"result_status": "success"}}
    if build_sims_internal_tool_route_decision(generic_payload) is not None:
        raise AssertionError("unowned payload created a SIMS route decision")


def check_sims_internal_runtime_trace_boundary() -> None:
    source = (ROOT / "app" / "ui" / "chat_middleware.py").read_text(encoding="utf-8")
    start = source.index("def wssz(")
    block = source[start:]
    attach = block.index("_attach_sims_response_timing(payload, ss)")
    decision = block.index("build_sims_internal_tool_route_decision(payload)")
    envelope = block.index("build_structured_response_envelope(\n            payload,\n            tool_route_decision=tool_route_decision,")
    inbox = block.index('ss.setdefault("__chat_inbox", [])', envelope)
    if not (attach < decision < envelope < inbox):
        raise AssertionError("SIMS internal trace was not derived after the final payload boundary")


def check_rejects_forbidden_or_non_json_data() -> None:
    envelope = build_structured_response_envelope(_sims_payload("success"))
    envelope["arguments"]["connection_string"] = "not-allowed"
    try:
        validate_structured_response_envelope(envelope)
    except ValueError:
        return
    raise AssertionError("validator accepted forbidden data")


def check_sensitive_legacy_params_are_not_mapped() -> None:
    payload = _sims_payload("success")
    payload["params"]["password"] = "never-copy"
    envelope = _assert_legacy_unchanged(payload)
    if "password" in envelope["arguments"]["conditions"]["parameter_keys"]:
        raise AssertionError("sensitive legacy parameter key was mapped")


def main() -> int:
    check_sims_statuses()
    check_current_table_provenance()
    check_knowledge_security()
    check_knowledge_tool_route_decision()
    check_knowledge_runtime_trace_boundary()
    check_web_tool_route_decision()
    check_web_runtime_trace_boundary()
    check_datetime_and_web()
    check_datetime_tool_route_decision()
    check_sims_internal_tool_route_decisions()
    check_sims_internal_runtime_trace_boundary()
    check_rejects_forbidden_or_non_json_data()
    check_sensitive_legacy_params_are_not_mapped()
    print("STRUCTURED_RESPONSE_CONTRACT_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
