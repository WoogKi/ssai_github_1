"""Offline Gate for the R110 inbound-detail clustered-date access contract."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import rddbc110_service


def _strip_date_predicates(sql: str) -> str:
    return "\n".join(
        line for line in str(sql).splitlines()
        if not (
            "Rd11_In_YyMmDd" in line
            and ("%(date_from)s" in line or "%(date_to)s" in line)
        )
    )


def main() -> int:
    captured: list[tuple[str, dict[str, object]]] = []
    fixture = pd.DataFrame(
        [{"Rd11_In_YyMmDd": "20261001", "Rd11_Ven_Cd": "V1", "Rd11_In_Seq": 1}]
    )

    def _capture(sql: str, params: dict[str, object]) -> pd.DataFrame:
        captured.append((str(sql), dict(params)))
        return fixture.copy()

    with patch.object(rddbc110_service, "query_to_df", _capture):
        exact = rddbc110_service.get_rddbc110_df(
            {"date_from": "20261001", "date_to": "20261001", "top": 200}
        )
    assert len(captured) == 1
    exact_sql, exact_params = captured.pop()
    pd.testing.assert_frame_equal(exact, fixture, check_dtype=True)
    assert exact_params["date_from"] == exact_params["date_to"] == "20261001"
    assert "In_Put.Rd11_In_YyMmDd = %(date_from)s" in exact_sql
    assert "Key_Row.Rd11_In_YyMmDd = %(date_from)s" in exact_sql
    assert "LTRIM(RTRIM(In_Put.Rd11_In_YyMmDd))" not in exact_sql
    assert "LTRIM(RTRIM(Key_Row.Rd11_In_YyMmDd))" not in exact_sql
    assert exact_sql.index("FROM dbo.Rddbc110 AS In_Put") < exact_sql.index("LEFT JOIN dbo.Rddbc040 AS Physic_Cd")
    assert "ORDER BY In_Put.Rd11_In_YyMmDd , In_Put.Rd11_Ven_Cd, In_Put.Rd11_In_Seq" in exact_sql

    with patch.object(rddbc110_service, "query_to_df", _capture):
        ranged = rddbc110_service.get_rddbc110_df(
            {"date_from": "20261001", "date_to": "20261002", "top": 200}
        )
    assert len(captured) == 1
    range_sql, _ = captured.pop()
    pd.testing.assert_frame_equal(ranged, fixture, check_dtype=True)
    assert "In_Put.Rd11_In_YyMmDd >= %(date_from)s" in range_sql
    assert "In_Put.Rd11_In_YyMmDd <= %(date_to)s" in range_sql
    assert "Key_Row.Rd11_In_YyMmDd >= %(date_from)s" in range_sql
    assert "Key_Row.Rd11_In_YyMmDd <= %(date_to)s" in range_sql
    assert _strip_date_predicates(exact_sql) == _strip_date_predicates(range_sql)

    print("PASS: inbound detail R110 date-first access / result equality / source_call_count=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
