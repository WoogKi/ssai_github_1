"""Regression check for deterministic Rddbc120 summary ranking."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "app" / "services" / "rddbc120_service.py"


def rank(rows: list[dict[str, object]]) -> list[str]:
    ordered = sorted(
        rows,
        key=lambda row: (
            -float(row["amount_sum"]),
            -float(row["qty_sum"]),
            -int(row["row_count"]),
            str(row["name"]),
        ),
    )
    return [str(row["name"]) for row in ordered]


def main() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    assert re.search(
        r"ORDER BY amount_sum DESC, qty_sum DESC, row_count DESC, name ASC",
        source,
    ), "ROW_NUMBER ranking must use name only as the deterministic final tie-breaker"

    rows = [
        {"name": "zeta", "amount_sum": 100, "qty_sum": 10, "row_count": 2},
        {"name": "alpha", "amount_sum": 100, "qty_sum": 10, "row_count": 2},
        {"name": "middle", "amount_sum": 100, "qty_sum": 10, "row_count": 2},
        {"name": "higher", "amount_sum": 101, "qty_sum": 1, "row_count": 1},
    ]

    expected = ["higher", "alpha", "middle", "zeta"]
    assert rank(rows) == expected
    assert rank(list(reversed(rows))) == expected
    print("PASS: rddbc120 deterministic summary tie ordering")


if __name__ == "__main__":
    main()
