from __future__ import annotations

import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot import (  # noqa: E402
    _canonical_relational_projection_row,
    _relational_projection_digest,
    _relational_projection_digest_from_canonical_rows,
    build_relational_frequency_projection,
    frequency_product_columns,
)
from tools.check_snapshot_product_statistics_extension import _snapshot  # noqa: E402


ROW_COUNT = 4096
REPETITIONS = 12


def _synthetic_rows() -> tuple[object, list[dict[str, object]], list[dict[str, object]]]:
    snapshot = _snapshot()
    rows, _headers = build_relational_frequency_projection(snapshot)
    raw_rows: list[dict[str, object]] = []
    for index in range(ROW_COUNT):
        source = dict(rows[index % len(rows)])
        source["product_code"] = f"BENCH{index:05d}"
        raw_rows.append(source)
    canonical_rows = [
        _canonical_relational_projection_row(row, key=snapshot.key) for row in raw_rows
    ]
    return snapshot.key, raw_rows, canonical_rows


def _grade_groups(rows: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    groups: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        groups.setdefault(str(row["frequency_grade"]), []).append(row)
    return groups


def _old_digest_pass(groups: dict[str, list[dict[str, object]]], key: object) -> dict[str, str]:
    return {
        grade: _relational_projection_digest(rows, key=key)
        for grade, rows in groups.items()
    }


def _canonical_reuse_digest_pass(
    groups: dict[str, list[dict[str, object]]], columns: tuple[str, ...]
) -> dict[str, str]:
    return {
        grade: _relational_projection_digest_from_canonical_rows(rows, columns=columns)
        for grade, rows in groups.items()
    }


def _elapsed_ms(action) -> float:
    started = time.perf_counter()
    action()
    return (time.perf_counter() - started) * 1000


def main() -> int:
    key, raw_rows, canonical_rows = _synthetic_rows()
    columns = frequency_product_columns(key)
    old_groups = _grade_groups(raw_rows)
    canonical_groups = _grade_groups(canonical_rows)
    old_digests = _old_digest_pass(old_groups, key)
    new_digests = _canonical_reuse_digest_pass(canonical_groups, columns)
    if old_digests != new_digests:
        raise AssertionError("raw and canonical-reuse digests differ")

    old_ms = _elapsed_ms(
        lambda: [
            _old_digest_pass(old_groups, key)
            for _ in range(REPETITIONS)
        ]
    )
    new_ms = _elapsed_ms(
        lambda: [
            _canonical_reuse_digest_pass(canonical_groups, columns)
            for _ in range(REPETITIONS)
        ]
    )
    improvement = (1 - new_ms / old_ms) * 100 if old_ms else 0.0
    print(
        "PASS snapshot canonicalization reuse microbenchmark "
        f"rows={ROW_COUNT} repetitions={REPETITIONS} "
        f"old_ms={old_ms:.3f} new_ms={new_ms:.3f} improvement_pct={improvement:.2f} "
        "digest_equality=exact"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
