"""DB-free checks for the 12 closed-month Snapshot cleanup boundary."""
from __future__ import annotations

from datetime import date
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import cleanup_snapshot_history as cleanup


class Cursor:
    def __init__(self, *, evaluation_month: str, latest_id: int | None) -> None:
        self.evaluation_month = evaluation_month
        self.latest_id = latest_id
        self.sql = ""
        self.params: tuple[object, ...] = ()

    def execute(self, sql: str, *params: object) -> "Cursor":
        self.sql, self.params = sql, params
        return self

    def fetchone(self) -> tuple[object, ...] | None:
        if self.sql.startswith("SELECT company_id"):
            return ("3", "superseded", "approved", "a" * 64, 1,
                    "frequency", self.evaluation_month, "b" * 64, "2.1", "algorithm", "c" * 64)
        if self.sql.startswith("SELECT TOP 1 manifest_id"):
            return (self.latest_id,) if self.latest_id is not None else None
        return None

    def fetchval(self) -> int:
        return 0


class Connection:
    def __init__(self, cursor: Cursor) -> None:
        self._cursor = cursor
        self.commits = 0

    def cursor(self) -> Cursor:
        return self._cursor

    def close(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def commit(self) -> None:
        self.commits += 1


def check_case(evaluation_month: str, latest_id: int | None, *, allowed: bool) -> None:
    conn = Connection(Cursor(evaluation_month=evaluation_month, latest_id=latest_id))
    with patch.object(cleanup, "connect_company_analytics_db", return_value=conn):
        try:
            result = cleanup.cleanup_one(
                company_id=3, kind="snapshot", manifest_id=16,
                expected_checksum="a" * 64, expected_rows=1,
                expected_generation=1, apply=False,
            )
        except ValueError as exc:
            assert not allowed and "last approved snapshot" in str(exc)
        else:
            assert allowed and result["manifest_id"] == 16 and not result["applied"]
    assert conn.commits == 0


def main() -> int:
    assert cleanup._recent_closed_evaluation_month("202511", today=date(2026, 10, 26))
    assert not cleanup._recent_closed_evaluation_month("202510", today=date(2026, 10, 26))
    assert cleanup._recent_closed_evaluation_month("202610", today=date(2026, 10, 26))
    assert cleanup._recent_closed_evaluation_month("202701", today=date(2026, 12, 1))
    for value in ("202613", "2026", "garbage"):
        try:
            cleanup._recent_closed_evaluation_month(value, today=date(2026, 10, 26))
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid evaluation month accepted: {value}")
    check_case("202610", 16, allowed=False)
    check_case("202610", None, allowed=False)
    check_case("202610", 24, allowed=True)
    check_case("202510", None, allowed=True)
    cursor = Cursor(evaluation_month="202610", latest_id=24)
    cleanup._protect_recent_approved_snapshot(
        cursor, company_id=3, manifest_id=16,
        evaluation_month="202610", approval_status="approved", apply=True,
    )
    assert "WITH (UPDLOCK,HOLDLOCK)" in cursor.sql
    assert cursor.params == ("3", "202610")
    print("PASS 12 closed-month retention: latest approved protected, old generation eligible, dry-run writes 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
