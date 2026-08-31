"""In-memory equality guard for the Rddbc130 filtered-key aggregate scope."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path


KEY_FIELDS = ("trans_di", "trans_date", "vendor_cd", "trans_seq")


def _key(row: dict) -> tuple:
    return tuple(row[field] for field in KEY_FIELDS)


def _aggregate(rows: list[dict], *, apply_legacy_sequence_filter: bool) -> dict[tuple, tuple[float, float]]:
    totals: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for row in rows:
        if apply_legacy_sequence_filter and not str(row["trans_seq"]).strip():
            continue
        bucket = totals[_key(row)]
        bucket[0] += float(row.get("supply") or 0)
        bucket[1] += float(row.get("tax") or 0)
    return {key: (values[0], values[1]) for key, values in totals.items()}


def _project(
    docs: list[dict],
    inbound: list[dict],
    outbound: list[dict],
    *,
    scoped: bool,
    apply_legacy_sequence_filter: bool,
    aggregate_shape: str,
) -> list[dict]:
    _assert(aggregate_shape in {"cte", "outer_apply"}, f"unexpected aggregate shape: {aggregate_shape}")
    doc_keys = {_key(row) for row in docs}
    if scoped:
        inbound = [row for row in inbound if _key(row) in doc_keys]
        outbound = [row for row in outbound if _key(row) in doc_keys]

    in_sum = _aggregate(inbound, apply_legacy_sequence_filter=apply_legacy_sequence_filter)
    out_sum = _aggregate(outbound, apply_legacy_sequence_filter=apply_legacy_sequence_filter)
    result: list[dict] = []
    for doc in docs:
        key = _key(doc)
        detail = in_sum.get(key) or out_sum.get(key)
        detail_kind = "입고" if key in in_sum else "출고" if key in out_sum else "기타"
        detail_match = None
        if detail is not None:
            detail_match = "Y" if detail == (doc["supply"], doc["tax"]) else "N"
        result.append(
            {
                "key": key,
                "거래명세서구분명": detail_kind,
                "공급가액": doc["supply"],
                "세액": doc["tax"],
                "합계금액": doc["total"],
                "할인금액": doc["discount"],
                "상세합계일치": detail_match,
            }
        )
    return sorted(result, key=lambda row: (row["key"][1], row["key"][2], row["key"][3]), reverse=True)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    docs = [
        {"trans_di": "1", "trans_date": "20260831", "vendor_cd": "A", "trans_seq": 1, "supply": 100.0, "tax": 10.0, "total": 110.0, "discount": 1.0},
        {"trans_di": "3", "trans_date": "20260831", "vendor_cd": "B", "trans_seq": 2, "supply": 200.0, "tax": 20.0, "total": 220.0, "discount": 0.0},
        {"trans_di": "9", "trans_date": "20260830", "vendor_cd": "C", "trans_seq": 3, "supply": 30.0, "tax": 3.0, "total": 33.0, "discount": 2.0},
        {"trans_di": "1", "trans_date": "20260829", "vendor_cd": "D", "trans_seq": 4, "supply": 50.0, "tax": 5.0, "total": 55.0, "discount": 0.0},
    ]
    inbound = [
        {"trans_di": "1", "trans_date": "20260831", "vendor_cd": "A", "trans_seq": 1, "supply": 100.0, "tax": 10.0},
        {"trans_di": "9", "trans_date": "20260830", "vendor_cd": "C", "trans_seq": 3, "supply": 30.0, "tax": 3.0},
        {"trans_di": "1", "trans_date": "20260829", "vendor_cd": "D", "trans_seq": 4, "supply": 49.0, "tax": 5.0},
        {"trans_di": "1", "trans_date": "20240101", "vendor_cd": "Z", "trans_seq": 999, "supply": 999.0, "tax": 99.0},
    ]
    outbound = [
        {"trans_di": "3", "trans_date": "20260831", "vendor_cd": "B", "trans_seq": 2, "supply": 200.0, "tax": 20.0},
        {"trans_di": "9", "trans_date": "20260830", "vendor_cd": "C", "trans_seq": 3, "supply": 999.0, "tax": 99.0},
        {"trans_di": "3", "trans_date": "20240101", "vendor_cd": "Y", "trans_seq": 998, "supply": 998.0, "tax": 98.0},
    ]

    legacy = _project(docs, inbound, outbound, scoped=False, apply_legacy_sequence_filter=True, aggregate_shape="cte")
    filtered_key = _project(docs, inbound, outbound, scoped=True, apply_legacy_sequence_filter=True, aggregate_shape="cte")
    no_trim = _project(docs, inbound, outbound, scoped=True, apply_legacy_sequence_filter=False, aggregate_shape="cte")
    proposed = _project(docs, inbound, outbound, scoped=True, apply_legacy_sequence_filter=False, aggregate_shape="outer_apply")
    _assert(legacy == filtered_key == no_trim == proposed, "legacy/filtered-key/no-trim/outer-apply changed rows, key order, amount, or match status")
    by_key = {row["key"]: row for row in proposed}
    _assert(by_key[("1", "20260829", "D", 4)]["상세합계일치"] == "N", "mismatch document contract changed")
    _assert(by_key[("3", "20260831", "B", 2)]["거래명세서구분명"] == "출고", "outbound-only document contract changed")
    _assert(by_key[("9", "20260830", "C", 3)]["거래명세서구분명"] == "입고", "both-detail document keeps inbound precedence")

    source = Path("app/services/rddbc130_service.py").read_text(encoding="utf-8")
    _assert("FilteredBooks AS" in source, "FilteredBooks CTE missing")
    _assert("FilteredTransactionKeys AS" in source, "FilteredTransactionKeys CTE missing")
    _assert(source.count("INNER JOIN FilteredTransactionKeys AS Keys") == 3, "validation helper and validation analysis aggregates must remain key scoped")
    _assert("def _base_document_sql" in source, "ordinary header-only query helper missing")
    _assert("OUTER APPLY (" not in source, "unsuccessful outer-apply experiment must not remain in production SQL")
    _assert("NULLIF(LTRIM(RTRIM(In_Put.Rd11_Trans_Seq)), '') IS NOT NULL" not in source, "inbound Trans_Seq conversion predicate remains")
    _assert("NULLIF(LTRIM(RTRIM(Out_Put.Rd12_Trans_Seq)), '') IS NOT NULL" not in source, "outbound Trans_Seq conversion predicate remains")

    print("RESULT: OK")
    print("legacy/filtered-key/no-trim/outer-apply exact equality: rows, key order, direction, amounts, discount, match status")


if __name__ == "__main__":
    main()
