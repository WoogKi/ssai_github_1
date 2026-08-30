"""Focused no-DB regression for the structured-presentation retest fixes."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.structured_presentation_poc import (  # noqa: E402
    FEATURE_FLAG,
    build_analytics_presentation_facts,
    maybe_create_structured_presentation,
)
from app.sims.nlq.nlq_router import (  # noqa: E402
    _build_analytics_params,
    _finalize_analytics_result_status,
    _resolve_analytics_action,
)


class _NeverCalledClient:
    def __init__(self) -> None:
        self.calls = 0

    def with_options(self, **_kwargs):
        self.calls += 1
        raise AssertionError("an ineligible payload must not create an LLM client")


def _payload(status: str | None, *, row_count: int = 0) -> dict:
    meta = {
        "analysis_nlq": True,
        "execution_status": "success",
        "summary_md": "확정된 분석 요약",
        "row_count": row_count,
        "row_count_total": row_count,
        "source_call_count": 0,
    }
    if status is not None:
        meta["result_status"] = status
    return {"action": "품목별 매출 추세 분석", "meta": meta}


def check_product_grouping_aliases() -> None:
    for text in ("제품별 매출분석", "제품별 매출 분석", "품목별 매출분석", "품목별 매출 분석"):
        action = _resolve_analytics_action(text)
        if action != "품목별 매출 추세 분석":
            raise AssertionError(f"grouping alias did not resolve Analytics action: {text}")
        params = _build_analytics_params(text, action)
        if str(params.get("physic_nm") or "").strip() or str(params.get("physic_cd") or "").strip():
            raise AssertionError(f"grouping alias became a product filter: {text}")

    explicit_text = "제품명 테스트품 제품별 매출분석"
    explicit_params = _build_analytics_params(explicit_text, _resolve_analytics_action(explicit_text))
    if explicit_params.get("physic_nm") != "테스트품":
        raise AssertionError("explicit product condition was lost while removing the grouping alias")


def check_final_status_and_poc_eligibility() -> None:
    if _finalize_analytics_result_status({"row_count_total": 0}) != "no_data":
        raise AssertionError("zero-row Analytics result was not finalized as no_data")
    if _finalize_analytics_result_status({"row_count_total": 1}) != "success":
        raise AssertionError("non-empty Analytics result was not finalized as success")
    if _finalize_analytics_result_status({"result_status": "candidate_required", "row_count_total": 1}) != "candidate_required":
        raise AssertionError("explicit terminal Analytics status was overwritten")

    env = {FEATURE_FLAG: "1", "LLM_MODEL_DEFAULT": "fixture-model", "LLM_TIMEOUT_S": "90"}
    for status in ("no_data", "input_required", "candidate_required", "error", None):
        client = _NeverCalledClient()
        result = maybe_create_structured_presentation(
            _payload(status),
            environ=env,
            client_factory=lambda: client,
        )
        if result.get("status") != "skipped" or result.get("reason_code") != "not_eligible" or client.calls:
            raise AssertionError(f"non-success or non-final status invoked the PoC: {status!r}")

    followup = _payload("success", row_count=1)
    followup["meta"]["current_table_followup"] = True
    if build_analytics_presentation_facts(followup) is not None:
        raise AssertionError("current-table follow-up became eligible for the Analytics PoC")


def main() -> int:
    check_product_grouping_aliases()
    check_final_status_and_poc_eligibility()
    print("STRUCTURED_PRESENTATION_RETEST_FOLLOWUP_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
