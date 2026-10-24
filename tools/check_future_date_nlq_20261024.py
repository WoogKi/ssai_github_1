"""DB-free date-range contract through the real NLQ router and mocked IO services."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.io_nlq import has_unconsumed_detail_condition, resolve_io_nlq
from app.services import rddbc110_service, rddbc120_service
from tools.check_nlq_runtime_intent_contract import _run_router


def check_range(question: str, action: str, first: str, last: str,
                *, explicit: bool = True) -> None:
    parsed = resolve_io_nlq(question, today=date(2026, 9, 27))
    assert parsed and parsed["action"] == action, (question, parsed)
    params = parsed["params"]
    expected = {"date_from": first, "date_to": last,
                "month_from": first[:6], "month_to": last[:6]}
    if explicit:
        assert all(params.get(key) == value for key, value in expected.items()), (question, params)
    else:
        assert not any(params.get(key) for key in expected), (question, params)
    assert not has_unconsumed_detail_condition(question, action, params), question
    handled, calls, sent = _run_router(question)
    assert handled and len(calls) == 1 and len(sent) == 1, (question, calls, sent)
    actual = calls[0]
    if not explicit:
        assert actual.get("date_from") == actual.get("date_to"), (question, actual)
        first = last = actual["date_from"]
        expected = {"date_from": first, "date_to": last,
                    "month_from": first[:6], "month_to": last[:6]}
    assert all(actual.get(key) == value for key, value in expected.items()), (question, actual)
    bound = dict(actual)
    if action == "입고명세 조회":
        bound["top"] = rddbc110_service._io_query_top(bound)
        sql = rddbc110_service._base_filters(bound)
        column = "In_Put.Rd11_In_YyMmDd"
    else:
        bound["top"] = rddbc120_service._io_query_top(bound)
        sql = rddbc120_service._base_filters(bound)
        column = "Out_Put.Rd12_Out_YyMmDd"
    assert f"{column} >= %(date_from)s" in sql, (question, sql)
    assert f"{column} <= %(date_to)s" in sql, (question, sql)
    assert bound["date_to"] == last and 0 < bound["top"] <= 30000, bound


def check_blocked(question: str) -> None:
    parsed = resolve_io_nlq(question, today=date(2026, 9, 27))
    assert parsed and parsed["action"] == "출고명세 조회", (question, parsed)
    assert has_unconsumed_detail_condition(question, parsed["action"], parsed["params"]), question
    handled, calls, sent = _run_router(question)
    assert handled and not calls and len(sent) == 1, (question, calls, sent)
    assert (sent[0]["payload"].get("meta") or {}).get("result_status") == "input_required"


def main() -> None:
    for question, action, end in (
        ("출고명세 20261025 ~ 90001231", "출고명세 조회", "90001231"),
        ("매출현황 20261025 ~ 90001231", "출고명세 조회", "90001231"),
        ("출고명세 20261025 ~ 99991231", "출고명세 조회", "99991231"),
        ("입고명세 조회 20261025 ~ 90001231", "입고명세 조회", "90001231"),
    ):
        check_range(question, action, "20261025", end)
    check_range("출고명세 20261025 ~ 20261231", "출고명세 조회",
                "20261025", "20261231")
    check_range("매출현황 202610 ~ 900012", "출고명세 조회",
                "20261001", "90001231")
    check_range("출고명세 202610", "출고명세 조회", "20261001", "20261031")
    check_range("출고명세 2026", "출고명세 조회", "20260101", "20261231")
    check_range("어제 출고현황", "출고명세 조회", "20260926", "20260926")
    check_range("출고명세", "출고명세 조회", "20260927", "20260927", explicit=False)
    for question in (
        "출고명세 20261025 ~ 90001331",
        "출고명세 20261025 ~ 90000230",
        "출고명세 20261325 ~ 90001231",
        "출고명세 20261231 ~ 20261025",
    ):
        check_blocked(question)
    print("PASS future date NLQ parser/router/service/SQL and invalid-range guard")


if __name__ == "__main__":
    main()
