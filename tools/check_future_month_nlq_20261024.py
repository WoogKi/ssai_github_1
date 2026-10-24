"""DB-free regression for an explicit far-future outbound end month."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.io_nlq import (
    extract_params,
    has_unconsumed_detail_condition,
    resolve_io_nlq,
    strip_nlq_period_tokens_for_entity_residual,
)
from app.services.rddbc120_service import _base_filters, _io_query_top
from tools.check_nlq_runtime_intent_contract import _run_router


def check(question: str, start_month: str, end_month: str,
          start_day: str, end_day: str, *, today: date = date(2026, 10, 24)) -> None:
    parsed = resolve_io_nlq(question, today=today)
    assert parsed and parsed["action"] == "출고명세 조회", (question, parsed)
    params = parsed["params"]
    for key, expected in (("month_from", start_month), ("month_to", end_month),
                          ("date_from", start_day), ("date_to", end_day)):
        assert params.get(key) == expected, (question, key, params)
    assert not has_unconsumed_detail_condition(question, parsed["action"], params), question
    handled, calls, sent = _run_router(question)
    assert handled and len(calls) == 1 and len(sent) == 1, (question, calls, sent)
    actual = calls[0]
    assert all(actual.get(key) == params[key] for key in
               ("month_from", "month_to", "date_from", "date_to")), (question, actual)
    sql_params = dict(actual)
    sql_params["top"] = _io_query_top(sql_params)
    where = _base_filters(sql_params)
    assert "Out_Put.Rd12_Out_YyMmDd >= %(date_from)s" in where, where
    assert "Out_Put.Rd12_Out_YyMmDd <= %(date_to)s" in where, where
    assert sql_params["date_to"] == end_day and 0 < sql_params["top"] <= 30000


def main() -> None:
    check("매출현황 202610 ~ 900012", "202610", "900012", "20261001", "90001231")
    check("출고명세 202610 ~ 900012", "202610", "900012", "20261001", "90001231")
    check("매출현황 202610~999912", "202610", "999912", "20261001", "99991231")
    check("매출현황 202610 ~ 202612", "202610", "202612", "20261001", "20261231")
    check("매출현황 20261001 ~ 20261231", "202610", "202612", "20261001", "20261231")
    check("어제 출고현황", "202609", "202609", "20260926", "20260926",
          today=date(2026, 9, 27))
    formatted = resolve_io_nlq("매출현황 2026-10 ~ 2026-12", today=date(2026, 10, 24))
    assert formatted and formatted["params"]["month_to"] == "202612"
    for invalid in ("202610 ~ 900013", "202610 ~ 900000", "202610 ~ 900099",
                    "202613 ~ 900012"):
        question = "매출현황 " + invalid
        params = extract_params(question, today=date(2026, 10, 24))
        parsed = resolve_io_nlq(question, today=date(2026, 10, 24))
        assert parsed and has_unconsumed_detail_condition(question, parsed["action"], parsed["params"]), question
    assert extract_params("매출현황 900012", today=date(2026, 10, 24)).get("month_to") != "900012"
    assert "900012" not in strip_nlq_period_tokens_for_entity_residual(
        "매출현황 202610 ~ 900012")
    print("PASS future month parser/router/service boundary")


if __name__ == "__main__":
    main()
