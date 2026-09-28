"""Focused no-DB contract for expected-inbound product names and explicit months."""

from __future__ import annotations

from datetime import date
import inspect
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services import io_nlq
from app.services import erp_table_nlq
from app.services import rddbc170_rddbc180_order_service as expected_service


REFERENCE_DAY = date(2026, 9, 27)
CASES = (
    ("바레탄 입고예정조회 202609", "바레탄", "20260901", "20260930"),
    ("바레탄 입고예정 조회 202609", "바레탄", "20260901", "20260930"),
    ("202609 바레탄 입고예정조회", "바레탄", "20260901", "20260930"),
    ("입고예정조회 바레탄 202609", "바레탄", "20260901", "20260930"),
    ("이가탄 입고예정조회 202609", "이가탄", "20260901", "20260930"),
    ("아모잘탄 입고예정조회 202609", "아모잘탄", "20260901", "20260930"),
    ("제품명 바레탄 입고예정조회 202609", "바레탄", "20260901", "20260930"),
    ("제품명 바레탄 입고예정조회 202608~202609", "바레탄", "20260801", "20260930"),
    ("비타민2026정 입고예정조회 202609", "비타민2026정", "20260901", "20260930"),
    ("ABC202609정 입고예정조회 202608~202609", "ABC202609정", "20260801", "20260930"),
    ("2020정 입고예정조회 202609", "2020정", "20260901", "20260930"),
)

ENTITY_NAME_CASES = (
    ("제품명 비타민2026정 입고예정조회 202609", ("제품명",), "비타민2026정"),
    ("제품명 ABC202609정 입고예정조회 202608~202609", ("제품명",), "ABC202609정"),
    ("제품명 2020정 입고예정조회 202609", ("제품명",), "2020정"),
    ("제조사명 비타민2026제약 입고예정조회 202609", ("제조사명",), "비타민2026제약"),
    ("발주처명 ABC202609상사 입고예정조회 202609", ("발주처명",), "ABC202609상사"),
)


def _check(query: str, expected_name: str, expected_from: str, expected_to: str) -> list[str]:
    errors: list[str] = []
    parsed = io_nlq.resolve_io_nlq(query, today=REFERENCE_DAY) or {}
    action = str(parsed.get("action") or "")
    params = dict(parsed.get("params") or {})
    consumed = io_nlq._consume_io_action_text(query, action)
    residual_after_period = io_nlq.strip_nlq_period_tokens_for_entity_residual(consumed)
    residual = str(params.pop("_registered_unlabeled_entity", "") or "")
    resolved = io_nlq.resolve_unlabeled_io_entity_condition(
        query,
        action=action,
        params=params,
        residual_phrase=residual,
    )
    final_params, policy = io_nlq.apply_nlq_default_period_policy(
        dict(resolved.get("params") or {}),
        action,
        today=REFERENCE_DAY,
    )
    final_params["_expected_inbound_auto_period"] = bool(policy.get("auto_applied"))

    if action != "입고예정조회":
        errors.append(f"action={action!r}")
    if str(final_params.get("physic_nm") or "") != expected_name:
        errors.append(f"physic_nm={final_params.get('physic_nm')!r}")
    if final_params.get("date_from") != expected_from or final_params.get("date_to") != expected_to:
        errors.append(f"period={final_params.get('date_from')!r}~{final_params.get('date_to')!r}")
    if bool(policy.get("auto_applied")) or not bool(policy.get("explicit_period_present")):
        errors.append(f"policy={policy!r}")
    if expected_name not in residual_after_period:
        errors.append(
            f"action-consumed residual={consumed!r}, after_period={residual_after_period!r}"
        )
    service_params = expected_service.normalize_order_params(final_params, mode="expected")
    if (
        service_params.get("physic_nm") != expected_name
        or service_params.get("date_from") != expected_from
        or service_params.get("date_to") != expected_to
        or service_params.get("_expected_inbound_auto_period") is not False
        or service_params.get("_expected_inbound_explicit_period") is not True
    ):
        errors.append(f"service_params={service_params!r}")
    return errors


def main() -> int:
    failures = []
    for query, expected_name, expected_from, expected_to in CASES:
        errors = _check(query, expected_name, expected_from, expected_to)
        if errors:
            failures.append((query, errors))
            print(f"[FAIL] {query}: {'; '.join(errors)}")
        else:
            print(f"[PASS] {query}")

    for text, labels, expected_name in ENTITY_NAME_CASES:
        actual_name = erp_table_nlq._extract_name(text, labels)
        if actual_name != expected_name:
            failures.append((text, [f"labelled entity={actual_name!r}"]))
            print(f"[FAIL] labelled entity keeps embedded digits: {text}: {actual_name!r}")
        else:
            print(f"[PASS] labelled entity keeps embedded digits: {text}")

    service_source = inspect.getsource(expected_service)
    source_contract_ok = (
        '"source_call_count": 1' in service_source
        and 'bool(source.get("_expected_inbound_auto_period"))' in service_source
    )
    print(f"[{'PASS' if source_contract_ok else 'FAIL'}] expected-inbound service keeps one-source-call contract")
    if not source_contract_ok:
        failures.append(("source_call_count", ["expected service contract missing"]))

    total = len(CASES) + len(ENTITY_NAME_CASES) + 1
    print(f"RESULT: {'PASS' if not failures else 'FAIL'} ({total - len(failures)}/{total})")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
