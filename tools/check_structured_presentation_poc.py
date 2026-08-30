"""Focused no-DB checks for the strict JSON Schema presentation PoC."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.structured_presentation_poc import (  # noqa: E402
    FEATURE_FLAG,
    MAX_KEY_POINT_CHARS,
    MAX_NOTICE_CHARS,
    MAX_PRESENTATION_SUMMARY_CHARS,
    PRESENTATION_MAX_TOKENS,
    PRESENTATION_SCHEMA,
    build_analytics_presentation_facts,
    maybe_create_structured_presentation,
)


def _signature(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _payload(*, status: str = "success") -> dict:
    return {
        "type": "table",
        "action": "제조사별 매출 분석",
        "params": {"month_from": "202601", "month_to": "202606"},
        "meta": {
            "nlq": True,
            "analysis_nlq": True,
            "result_status": status,
            "execution_status": status,
            "source_call_count": 2,
            "table_key": "result-table",
            "summary_md": "확정된 제조사별 매출 집계입니다.",
            "row_count": 5,
            "row_count_total": 5,
        },
    }


class _FakeCreate:
    def __init__(self, response: object | None = None, error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        return self.response


class _FakeClient:
    def __init__(self, create: _FakeCreate) -> None:
        self.create_api = create
        self.with_options_calls: list[dict] = []
        self.chat = SimpleNamespace(completions=create)

    def with_options(self, **kwargs: object) -> "_FakeClient":
        self.with_options_calls.append(dict(kwargs))
        return self


def _response(content: object, *, finish_reason: str = "stop") -> object:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)]
    )


def _enabled_env() -> dict[str, str]:
    return {FEATURE_FLAG: "1", "LLM_MODEL_DEFAULT": "fixture-model", "LLM_TIMEOUT_S": "90"}


def _assert_unchanged(payload: dict, runner) -> dict:
    before = _signature(payload)
    result = runner()
    if _signature(payload) != before:
        raise AssertionError("legacy payload was mutated")
    return result


def check_valid_schema() -> None:
    create = _FakeCreate(_response('{"summary":"확정 결과 요약","key_points":["집계 5건"],"notices":[]}'))
    client = _FakeClient(create)
    payload = _payload()
    result = _assert_unchanged(
        payload,
        lambda: maybe_create_structured_presentation(payload, environ=_enabled_env(), client_factory=lambda: client),
    )
    if result.get("status") != "ready" or result.get("presentation", {}).get("summary") != "확정 결과 요약":
        raise AssertionError("valid strict schema response was not accepted")
    if result.get("finish_reason") != "stop" or result.get("content_type") != "str":
        raise AssertionError("safe response diagnostics were not retained")
    if len(create.calls) != 1 or client.with_options_calls != [{"timeout": 90, "max_retries": 0}]:
        raise AssertionError("presentation call count or retry contract changed")
    request = create.calls[0]
    if request.get("stream") is not False or request.get("response_format") != {"type": "json_schema", "json_schema": PRESENTATION_SCHEMA}:
        raise AssertionError("strict JSON Schema non-stream request changed")
    if request.get("max_tokens") != PRESENTATION_MAX_TOKENS:
        raise AssertionError("presentation token ceiling changed")
    schema = PRESENTATION_SCHEMA["schema"]["properties"]
    if (
        schema["summary"].get("maxLength") != MAX_PRESENTATION_SUMMARY_CHARS
        or schema["key_points"]["items"].get("maxLength") != MAX_KEY_POINT_CHARS
        or schema["notices"]["items"].get("maxLength") != MAX_NOTICE_CHARS
    ):
        raise AssertionError("presentation schema does not bound output length")
    if "tools" in request or "tool_choice" in request:
        raise AssertionError("presentation request exposed tool calling")
    prompt = str(request["messages"][-1]["content"])
    if "params" in prompt or "source_call_count" in prompt or "table_key" in prompt:
        raise AssertionError("prompt contains non-presentation execution data")


def check_failure_isolated() -> None:
    expected_failures = {
        '{"summary":"ok","key_points":[],"notices":[],"extra":"x"}': ("schema_validation", "schema_validation_failed"),
        "```json\nnot-json\n```": ("json_parse", "json_parse_failed"),
    }
    for content, (expected_stage, expected_reason) in expected_failures.items():
        create = _FakeCreate(_response(content))
        client = _FakeClient(create)
        payload = _payload()
        result = _assert_unchanged(
            payload,
            lambda: maybe_create_structured_presentation(payload, environ=_enabled_env(), client_factory=lambda: client),
        )
        if (
            result.get("status") != "failed"
            or result.get("failure_stage") != expected_stage
            or result.get("reason_code") != expected_reason
            or result.get("exception_class") not in {"ValueError", "JSONDecodeError"}
            or result.get("response_present") is not True
            or result.get("content_present") is not True
            or len(create.calls) != 1
        ):
            raise AssertionError("invalid presentation was not isolated")
        if expected_stage == "json_parse" and result.get("content_has_code_fence") is not True:
            raise AssertionError("parse failure omitted safe structural diagnostics")

    create = _FakeCreate(_response('{"summary":"truncated"', finish_reason="length"))
    client = _FakeClient(create)
    payload = _payload()
    result = _assert_unchanged(
        payload,
        lambda: maybe_create_structured_presentation(payload, environ=_enabled_env(), client_factory=lambda: client),
    )
    if (
        result.get("status") != "failed"
        or result.get("reason_code") != "output_truncated"
        or result.get("failure_stage") != "json_parse"
        or result.get("finish_reason") != "length"
        or result.get("content_starts_json_object") is not True
        or result.get("content_ends_json_object") is not False
        or result.get("retry_count") != 0
        or len(create.calls) != 1
    ):
        raise AssertionError("truncated JSON was not safely isolated")

    create = _FakeCreate(error=TimeoutError("fixture"))
    client = _FakeClient(create)
    payload = _payload()
    result = _assert_unchanged(
        payload,
        lambda: maybe_create_structured_presentation(payload, environ=_enabled_env(), client_factory=lambda: client),
    )
    if (
        result.get("status") != "failed"
        or result.get("failure_stage") != "request"
        or result.get("exception_class") != "TimeoutError"
        or result.get("retry_count") != 0
        or len(create.calls) != 1
    ):
        raise AssertionError("timeout failure retried or changed the legacy result")

    create = _FakeCreate(_response(""))
    client = _FakeClient(create)
    payload = _payload()
    result = _assert_unchanged(
        payload,
        lambda: maybe_create_structured_presentation(payload, environ=_enabled_env(), client_factory=lambda: client),
    )
    if (
        result.get("status") != "failed"
        or result.get("failure_stage") != "response_shape"
        or result.get("reason_code") != "response_shape_invalid"
        or result.get("response_present") is not True
        or result.get("content_present") is not False
    ):
        raise AssertionError("empty response shape was not classified safely")


def check_eligibility_and_business_invariance() -> None:
    payload = _payload(status="no_data")
    if build_analytics_presentation_facts(payload) is not None:
        raise AssertionError("non-success result became presentation eligible")
    payload = _payload()
    payload["meta"].pop("result_status")
    payload["meta"]["execution_status"] = "success"
    if build_analytics_presentation_facts(payload) is not None:
        raise AssertionError("non-final execution status became presentation eligible")
    payload = _payload()
    payload["meta"]["current_table_followup"] = True
    if build_analytics_presentation_facts(payload) is not None:
        raise AssertionError("current-table follow-up became presentation eligible")

    create = _FakeCreate(_response('{"summary":"요약","key_points":[],"notices":[]}'))
    client = _FakeClient(create)
    payload = _payload()
    before = copy.deepcopy(payload)
    disabled = maybe_create_structured_presentation(payload, environ={}, client_factory=lambda: client)
    if disabled.get("reason_code") != "feature_disabled" or create.calls:
        raise AssertionError("disabled feature called LM Studio")
    if payload != before:
        raise AssertionError("disabled feature mutated legacy payload")


def check_runtime_boundary() -> None:
    source = (ROOT / "app" / "ui" / "chat_middleware.py").read_text(encoding="utf-8")
    start = source.index("def wssz(")
    block = source[start:]
    attach = block.index("_attach_sims_response_timing(payload, ss)")
    overlay = block.index("maybe_create_structured_presentation(payload)")
    inbox = block.index('ss.setdefault("__chat_inbox", [])', overlay)
    if not (attach < overlay < inbox):
        raise AssertionError("presentation was not derived after final business payload and before chat delivery")
    if 'payload["structured_presentation"]' in block or 'meta["structured_presentation"]' in block:
        raise AssertionError("presentation changed the persisted legacy payload shape")


def main() -> int:
    check_valid_schema()
    check_failure_isolated()
    check_eligibility_and_business_invariance()
    check_runtime_boundary()
    print("STRUCTURED_PRESENTATION_POC_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
