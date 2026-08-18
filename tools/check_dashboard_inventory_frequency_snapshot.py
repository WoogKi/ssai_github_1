from __future__ import annotations

import copy
import inspect
import random
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot import (  # noqa: E402
    ALGORITHM_VERSION,
    FREQUENCY_INSUFFICIENT_GRADE,
    SCHEMA_VERSION,
    SNAPSHOT_TYPE,
    SnapshotContractError,
    assign_frequency_grades,
    build_frequency_snapshot_payload,
    calculate_payload_checksum,
    completed_month_basis,
    evaluate_frequency_snapshot,
    frequency_rows_for_universe,
    is_normal_outbound_tcode,
    snapshot_key_from_payload,
    validate_frequency_snapshot_payload,
)
from app.services.ssai_snapshot_repository import (  # noqa: E402
    SNAPSHOT_STATUS_CORRUPT,
    SNAPSHOT_STATUS_MISSING,
    SNAPSHOT_STATUS_READY,
    SNAPSHOT_STATUS_STALE,
    SNAPSHOT_STATUS_VERSION_MISMATCH,
    SnapshotKey,
    SnapshotPublishResult,
    SnapshotReadResult,
    SnapshotRepository,
)
from app.services.dashboard_inventory_frequency_snapshot_service import (  # noqa: E402
    DashboardProfileStockScope,
    resolve_dashboard_profile_stock_scope,
)
from app.services import product_inventory_service  # noqa: E402
from app.services.product_inventory_service import (  # noqa: E402
    attach_dashboard_frequency_snapshot,
    filter_product_inventory_frequency_rows,
)
from app.services.ssai_analysis_profile_service import DashboardProfileLoadResult  # noqa: E402


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _row(
    seq: int,
    *,
    outbound_date: str = "20251010",
    vendor: str = "00001",
    tcode: str = "501",
    product: str = "P1",
    stock: str = "00001",
    quantity: int = 1,
    oquantity: int = 0,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "outbound_date": outbound_date,
        "vendor_code": vendor,
        "outbound_seq": seq,
        "io_gu_gcode": "0012",
        "io_tcode": tcode,
        "product_code": product,
        "stock_code": stock,
        "quantity": quantity,
        "oquantity": oquantity,
        **extra,
    }


def _build(rows: list[Mapping[str, Any]], **overrides: Any) -> dict[str, Any]:
    args = {
        "company_id": "TEST_COMPANY",
        "evaluation_month": "202601",
        "rows": rows,
        "product_codes": ["P1", "P2", "P3"],
        "stock_codes": ["00001"],
        "source_watermark": "fixture-20251231",
        "source_watermark_status": "verified",
    }
    args.update(overrides)
    return build_frequency_snapshot_payload(**args)


def test_tcode_and_event_contract() -> None:
    _assert(is_normal_outbound_tcode("12", "500"), "normalized 0012/500 must be accepted")
    _assert(is_normal_outbound_tcode("0012", 599), "599 must be accepted")
    _assert(not is_normal_outbound_tcode("0012", "5"), "005 must not be treated as 5xx")
    _assert(not is_normal_outbound_tcode("0012", "600"), "6xx return must be excluded")
    _assert(not is_normal_outbound_tcode("0013", "500"), "wrong group must be excluded")

    rows = [
        _row(1, fixed_flag="anything", record_flag="anything"),
        _row(2, tcode="599", oquantity=2),
        _row(3, tcode="600", quantity=100),
        _row(4, quantity=0),
        _row(5, quantity=-1),
    ]
    payload = _build(rows)
    _assert(payload["summary"]["normal_event_count"] == 2, "only positive 5xx events count")
    _assert(payload["monthly_activity"][0]["outbound_quantity"] == 4, "quantities must add")
    _assert(payload["source_contract"]["flag_exclusion_fields"] == [], "flags must not filter")


def test_month_scope_and_deduplication() -> None:
    basis = completed_month_basis("202601")
    _assert(basis.months == ("202510", "202511", "202512"), "prior three completed months")
    _assert(basis.basis_from == "20251001" and basis.basis_to == "20251231", "basis dates")
    duplicate = _row(1)
    payload = _build(
        [
            duplicate,
            dict(duplicate),
            _row(2, outbound_date="20251101", stock="00002"),
            _row(3, outbound_date="20260101"),
        ]
    )
    _assert(payload["summary"]["normal_event_count"] == 1, "dedupe and stock/month scope")
    _assert(payload["monthly_activity"][0]["occurrence_count"] == 1, "one occurrence")

    conflicting = dict(duplicate, product_code="P2")
    try:
        _build([duplicate, conflicting])
    except SnapshotContractError:
        pass
    else:
        raise AssertionError("conflicting duplicate event must fail closed")


def test_grade_bands_and_zero() -> None:
    rows = assign_frequency_grades(
        {"P1": 2, **{f"P{index}": 1 for index in range(2, 10)}},
        [f"P{index}" for index in range(1, 10)],
    )
    grades = {row["product_code"]: row["frequency_grade"] for row in rows}
    _assert(grades["P1"] == "A", "first group starts in A")
    _assert(all(grades[f"P{index}"] == "B" for index in range(2, 10)), "tie group is not split")

    tied = assign_frequency_grades({"P1": 1, "P2": 1, "P3": 1}, ["P1", "P2", "P3"])
    _assert({row["frequency_grade"] for row in tied} == {"A"}, "large tie remains one grade")
    zero = assign_frequency_grades({}, ["P1", "P2"])
    _assert({row["frequency_grade"] for row in zero} == {"X"}, "valid zero occurrences are X")


def test_payload_determinism_and_fail_closed() -> None:
    rows = [_row(1), _row(2, outbound_date="20251110", product="P2")]
    first = _build(rows)
    shuffled = list(rows)
    random.Random(7).shuffle(shuffled)
    second = _build(shuffled)
    _assert(first == second, "same logical input must produce identical payload/checksum")

    key = snapshot_key_from_payload(first)
    ready = evaluate_frequency_snapshot(first, expected_key=key)
    _assert(ready.status == SNAPSHOT_STATUS_READY, "valid snapshot must be ready")

    cases: list[tuple[str, SnapshotReadResult]] = []
    cases.append((SNAPSHOT_STATUS_MISSING, evaluate_frequency_snapshot(None, expected_key=key)))
    stale_key = SnapshotKey(
        company_id=key.company_id,
        snapshot_type=key.snapshot_type,
        evaluation_month="202602",
        scope_fingerprint=key.scope_fingerprint,
        schema_version=key.schema_version,
        algorithm_version=key.algorithm_version,
    )
    cases.append((SNAPSHOT_STATUS_STALE, evaluate_frequency_snapshot(first, expected_key=stale_key)))
    corrupt = copy.deepcopy(first)
    corrupt["summary"]["normal_event_count"] = 999
    cases.append((SNAPSHOT_STATUS_CORRUPT, evaluate_frequency_snapshot(corrupt, expected_key=key)))
    old_version = copy.deepcopy(first)
    old_version["schema_version"] = "0.9"
    cases.append(
        (SNAPSHOT_STATUS_VERSION_MISMATCH, evaluate_frequency_snapshot(old_version, expected_key=key))
    )
    for expected_status, result in cases:
        _assert(result.status == expected_status, f"expected {expected_status}, got {result.status}")
        unavailable = frequency_rows_for_universe(result, ["P1", "P2"])
        _assert(
            {row["frequency_grade"] for row in unavailable} == {FREQUENCY_INSUFFICIENT_GRADE},
            f"{expected_status} must not become X",
        )

    unverified = _build(rows, source_watermark_status="unverified")
    unverified_result = validate_frequency_snapshot_payload(
        unverified, expected_key=snapshot_key_from_payload(unverified)
    )
    _assert(
        unverified_result.status == SNAPSHOT_STATUS_READY,
        "watermark provenance and human approval/read status must remain separate",
    )


def _rechecksum(payload: dict[str, Any]) -> dict[str, Any]:
    payload["checksum"] = calculate_payload_checksum(payload)
    return payload


def test_payload_internal_corruption() -> None:
    payload = _build([_row(1), _row(2, outbound_date="20251110", product="P2")])
    key = snapshot_key_from_payload(payload)
    corruptions: list[tuple[str, dict[str, Any]]] = []

    duplicate_product = copy.deepcopy(payload)
    duplicate_product["product_frequency"].append(copy.deepcopy(duplicate_product["product_frequency"][0]))
    corruptions.append(("duplicate product", _rechecksum(duplicate_product)))

    duplicate_monthly = copy.deepcopy(payload)
    duplicate_monthly["monthly_activity"].append(copy.deepcopy(duplicate_monthly["monthly_activity"][0]))
    corruptions.append(("duplicate monthly grain", _rechecksum(duplicate_monthly)))

    negative = copy.deepcopy(payload)
    negative["monthly_activity"][0]["outbound_quantity"] = -1
    corruptions.append(("negative aggregate", _rechecksum(negative)))

    summary = copy.deepcopy(payload)
    summary["summary"]["normal_event_count"] += 1
    corruptions.append(("summary mismatch", _rechecksum(summary)))

    grades = copy.deepcopy(payload)
    grades["product_frequency"][0]["frequency_grade"] = "E"
    grades["summary"]["grade_counts"] = {"A": 0, "B": 0, "C": 0, "D": 0, "E": 1, "X": 2}
    corruptions.append(("grade mismatch", _rechecksum(grades)))

    for label, corrupted in corruptions:
        result = validate_frequency_snapshot_payload(corrupted, expected_key=key)
        _assert(result.status == SNAPSHOT_STATUS_CORRUPT, f"{label} must be corrupt")


class _FixtureRepository:
    def publish(
        self,
        key: SnapshotKey,
        payload: Mapping[str, Any],
        *,
        created_by: str,
        force: bool = False,
    ) -> SnapshotPublishResult:
        return SnapshotPublishResult(status=SNAPSHOT_STATUS_READY, generation_no=1)

    def read(self, key: SnapshotKey) -> SnapshotReadResult:
        return SnapshotReadResult(status=SNAPSHOT_STATUS_MISSING)

    def status(self, key: SnapshotKey) -> str:
        return SNAPSHOT_STATUS_MISSING

    def approve(
        self,
        key: SnapshotKey,
        generation_no: int,
        *,
        approved_by: str,
        approval_reason: str,
    ) -> SnapshotPublishResult:
        return SnapshotPublishResult(status=SNAPSHOT_STATUS_READY, generation_no=generation_no)

    def invalidate(self, key: SnapshotKey, *, reason: str, invalidated_by: str) -> None:
        return None

    def replace(
        self,
        key: SnapshotKey,
        payload: Mapping[str, Any],
        *,
        created_by: str,
    ) -> SnapshotPublishResult:
        return SnapshotPublishResult(status=SNAPSHOT_STATUS_READY, generation_no=2)


def test_repository_boundary() -> None:
    _assert(isinstance(_FixtureRepository(), SnapshotRepository), "repository Protocol must be portable")
    _assert(SNAPSHOT_TYPE and SCHEMA_VERSION and ALGORITHM_VERSION, "version contract is required")


def test_dashboard_profile_stock_scope_contract() -> None:
    profiles = {
        6: DashboardProfileLoadResult(status="ready", profile={"stock_cd_list": ["00001"]}, company_id=6),
        4: DashboardProfileLoadResult(status="ready", profile={"stock_cd_list": ["00901", "00001", "00247"]}, company_id=4),
        7: DashboardProfileLoadResult(status="missing", reason_code="profile_missing"),
        8: DashboardProfileLoadResult(status="ready", profile={"stock_cd_list": []}),
        9: DashboardProfileLoadResult(status="unavailable", reason_code="profile_db_unavailable:OperationalError"),
        10: DashboardProfileLoadResult(status="ready", profile={"stock_cd_list": ["00001"]}, company_id=4),
    }
    loader = lambda company_id: profiles[company_id]
    company6 = resolve_dashboard_profile_stock_scope(company_id=6, profile_loader=loader)
    company4 = resolve_dashboard_profile_stock_scope(company_id=4, profile_loader=loader)
    _assert(company6.stock_codes == ("00001",), "company 6 must keep its own saved stock scope")
    _assert(company4.stock_codes == ("00001", "00247", "00901"), "company 4 scope must remain deterministic")
    verified_manual = resolve_dashboard_profile_stock_scope(
        company_id=6, manual_stock_codes=["00001"], profile_loader=loader,
    )
    _assert(verified_manual.scope_source == "manual_verified", "matching manual scope may remain an override")
    for company_id, expected_code in ((6, "dashboard_profile_stock_scope_mismatch"), (7, "dashboard_profile_missing"), (8, "dashboard_profile_stock_scope_empty"), (9, "dashboard_profile_unavailable"), (10, "dashboard_profile_company_mismatch")):
        try:
            resolve_dashboard_profile_stock_scope(
                company_id=company_id,
                manual_stock_codes=["99999"] if company_id == 6 else None,
                profile_loader=loader,
            )
        except SnapshotContractError as exc:
            _assert(expected_code in str(exc), f"{company_id} expected {expected_code}, got {exc}")
        else:
            raise AssertionError(f"{company_id} must fail closed")


def test_product_inventory_snapshot_attachment() -> None:
    payload = _build([_row(1), _row(2, outbound_date="20251110", product="P2")])
    ready = validate_frequency_snapshot_payload(payload, expected_key=snapshot_key_from_payload(payload))
    expected = {row["product_code"]: row for row in payload["product_frequency"]}
    calls: list[dict[str, Any]] = []

    def _scope(**kwargs: Any) -> DashboardProfileStockScope:
        _assert(kwargs == {"company_id": 6}, "stored profile scope must use the selected company only")
        return DashboardProfileStockScope(6, ("00001",), "ready", "dashboard_profile")

    def _reader(**kwargs: Any) -> SnapshotReadResult:
        calls.append(dict(kwargs))
        return SnapshotReadResult(status=SNAPSHOT_STATUS_READY, payload=payload, generation_no=3, checksum=payload["checksum"])

    source = __import__("pandas").DataFrame(
        {"제품코드": ["P1", "P1", "P2", "P4", "P4", ""], "재고수량": [1, 2, 3, 4, 5, 6]}
    )
    attached, meta = attach_dashboard_frequency_snapshot(
        source,
        params={"company_id": 6, "stock_cds": ["00999"]},
        date_to="20260115",
        profile_scope_resolver=_scope,
        snapshot_reader=_reader,
    )
    _assert(calls == [{"company_id": 6, "evaluation_month": "202601", "stock_codes": ["00001"]}], "reader must use profile scope, not table filter")
    _assert(meta["frequency_snapshot_status"] == "ready" and meta["frequency_additional_erp_source_call_count"] == 0, "ready attachment keeps ERP calls at zero")
    _assert(
        list(attached.columns[:4]) == ["제품코드", "출고빈도등급", "3개월 출고발생수", "재고수량"],
        "frequency columns must remain beside the shared product key",
    )
    _assert(attached.loc[0, "출고빈도등급"] == expected["P1"]["frequency_grade"], "P1 grade must match Dashboard snapshot")
    _assert(attached.loc[1, "출고빈도등급"] == expected["P1"]["frequency_grade"], "duplicate P1 rows must keep the same snapshot grade")
    _assert(int(attached.loc[2, "3개월 출고발생수"]) == expected["P2"]["occurrence_count_3m"], "P2 count must match Dashboard snapshot")
    _assert(attached.loc[3, "출고빈도등급"] == FREQUENCY_INSUFFICIENT_GRADE, "product outside snapshot must fail closed")
    _assert(meta["frequency_missing_product_count"] == 1, "missing count must use unique product codes, not result rows")
    _assert(attached.loc[5, "출고빈도등급"] == "" and __import__("pandas").isna(attached.loc[5, "3개월 출고발생수"]), "total row must remain blank")

    for status in (SNAPSHOT_STATUS_MISSING, "unapproved", SNAPSHOT_STATUS_STALE, SNAPSHOT_STATUS_CORRUPT):
        unavailable, unavailable_meta = attach_dashboard_frequency_snapshot(
            source,
            params={"company_id": 6},
            date_to="20260115",
            profile_scope_resolver=_scope,
            snapshot_reader=lambda **_kwargs: SnapshotReadResult(status=status),
        )
        _assert(unavailable_meta["frequency_snapshot_status"] == status, f"{status} status must be retained")
        _assert(set(unavailable.loc[:4, "출고빈도등급"]) == {FREQUENCY_INSUFFICIENT_GRADE}, f"{status} must fail closed")
        _assert(unavailable_meta["frequency_missing_product_count"] == 3, f"{status} must count unique products only")

    mismatch, mismatch_meta = attach_dashboard_frequency_snapshot(
        source,
        params={"company_id": 6},
        date_to="20260115",
        profile_scope_resolver=lambda **_kwargs: (_ for _ in ()).throw(SnapshotContractError("scope mismatch")),
        snapshot_reader=_reader,
    )
    _assert(mismatch_meta["frequency_snapshot_status"] == "missing", "profile/key mismatch must fail closed")
    _assert(set(mismatch.loc[:4, "출고빈도등급"]) == {FREQUENCY_INSUFFICIENT_GRADE}, "mismatch must not become X")
    _assert(mismatch_meta["frequency_missing_product_count"] == 3, "profile mismatch must count unique products only")

    original_company_reader = product_inventory_service.get_current_company_id
    product_inventory_service.get_current_company_id = lambda: 6
    try:
        context_mismatch, context_meta = attach_dashboard_frequency_snapshot(
            source,
            params={"company_id": 7},
            date_to="20260115",
            profile_scope_resolver=_scope,
            snapshot_reader=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("reader must not run")),
        )
    finally:
        product_inventory_service.get_current_company_id = original_company_reader
    _assert(context_meta["frequency_snapshot_reason"] == "company_context_mismatch", "company mismatch must fail closed before snapshot read")
    _assert(set(context_mismatch.loc[:4, "출고빈도등급"]) == {FREQUENCY_INSUFFICIENT_GRADE}, "company mismatch must not reuse another company's snapshot")
    _assert(context_meta["frequency_missing_product_count"] == 3, "company mismatch must count unique products only")

    filter_source = __import__("pandas").DataFrame({
        "제품코드": ["A1", "A1", "B1", "C1", "D1", "E1", "X1", "M1"],
        "출고빈도등급": ["A", "A", "B", "C", "D", "E", "X", FREQUENCY_INSUFFICIENT_GRADE],
    })
    expected_rows = {"A": 2, "B": 1, "C": 1, "D": 1, "E": 1, "X": 1, FREQUENCY_INSUFFICIENT_GRADE: 1}
    for grade, expected_count in expected_rows.items():
        filtered = filter_product_inventory_frequency_rows(filter_source, grade)
        _assert(len(filtered) == expected_count, f"{grade} must filter only attached snapshot grades")
    _assert(
        len(filter_product_inventory_frequency_rows(filter_source, "전체")) == len(filter_source),
        "all frequency filter must preserve existing rows",
    )


def test_product_inventory_frequency_filter_totals_and_summary() -> None:
    """The attached grade narrows detail, totals, current-table, and Excel together."""
    rows = []
    for product_code, maker, stock_qty in (("P1", "제조사A", 2), ("P1", "제조사B", 3), ("P2", "제조사C", 97)):
        rows.append({
            "group_nm": maker,
            "physic_cd": product_code,
            "physic_nm": product_code + " 제품",
            "standard": "규격",
            "kd_cd": "KD",
            "edi_cd": "EDI",
            "carry_qty": 0,
            "carry_unit": 0,
            "carry_dc": 0,
            "carry_amt": 0,
            "now_in_qty": 0,
            "in_unit": 0,
            "in_dc": 0,
            "in_amt": 0,
            "now_out_qty": 0,
            "out_unit": 0,
            "out_dc": 0,
            "out_amt": 0,
            "stock_qty": stock_qty,
            "stock_unit": 0,
            "stock_dc": 0,
            "stock_amt": stock_qty * 10,
            "curr_insu_unit": 0,
            "insu_amt": stock_qty * 10,
            "std_cd": "STD",
            "product_group_nm": "그룹",
            "product_di_nm": "구분",
            "product_class_nm": "분류",
            "buy_cd": "BUY",
            "buy_nm": "매입처",
            "order_cd": "ORDER",
            "order_nm": "발주처",
            "maker_cd": "MAKER",
            "maker_nm": maker,
            "pack_unit": "1",
            "special_manage_nm": "",
        })
    source = __import__("pandas").DataFrame(rows)
    original_attach = product_inventory_service.attach_dashboard_frequency_snapshot

    def _attach_fixture(df: Any, **_kwargs: Any) -> tuple[Any, dict[str, Any]]:
        out = df.copy()
        out["출고빈도등급"] = out["제품코드"].map({"P1": "A", "P2": "B"})
        out["3개월 출고발생수"] = out["제품코드"].map({"P1": 8, "P2": 1}).astype("Int64")
        return product_inventory_service._place_frequency_columns(out), {
            "frequency_snapshot_status": "ready",
            "frequency_additional_erp_source_call_count": 0,
        }

    product_inventory_service.attach_dashboard_frequency_snapshot = _attach_fixture
    try:
        displayed, meta = product_inventory_service._final_display_df(
            source,
            {"group_basis": "maker"},
            frequency_params={"frequency_grade": "A"},
            frequency_date_to="20260131",
        )
    finally:
        product_inventory_service.attach_dashboard_frequency_snapshot = original_attach

    details = displayed.loc[displayed["제조사"] != "합계"]
    total = displayed.loc[displayed["제조사"] == "합계"].iloc[0]
    _assert(len(details) == 2 and set(details["제품코드"]) == {"P1"}, "frequency filter must retain only attached grade rows")
    _assert(meta["detail_count"] == 2 and meta["sum_stock_qty"] == 5.0, "result count and summary must use filtered details")
    _assert(float(total["재고수량"]) == 5.0 and float(total["재고금액"]) == 50.0, "total row must be rebuilt after filtering")
    _assert(set(details["출고빈도등급"]) == {"A"}, "duplicate product rows must keep their attached grade")
    _assert(total["출고빈도등급"] == "", "summary row must not become a frequency detail")
    summary = product_inventory_service._build_inventory_query_summary(
        date_from="20260101",
        date_to="20260131",
        cfg={"stock_mode": "real", "group_basis": "maker", "price_mode": "purchase"},
        work_params={},
        params={"frequency_grade": "E"},
    )
    _assert("출고빈도 E" in summary, "NLQ and UI query summary must expose the canonical frequency filter")
    attach_source = inspect.getsource(product_inventory_service.attach_dashboard_frequency_snapshot)
    _assert(
        "for index in out.index[detail_mask]" not in attach_source
        and "product_codes.map(" in attach_source,
        "frequency attachment must map snapshot rows without per-result-row mutation",
    )


def main() -> int:
    tests = (
        test_tcode_and_event_contract,
        test_month_scope_and_deduplication,
        test_grade_bands_and_zero,
        test_payload_determinism_and_fail_closed,
        test_payload_internal_corruption,
        test_repository_boundary,
        test_dashboard_profile_stock_scope_contract,
        test_product_inventory_snapshot_attachment,
        test_product_inventory_frequency_filter_totals_and_summary,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS dashboard inventory frequency snapshot contract ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
