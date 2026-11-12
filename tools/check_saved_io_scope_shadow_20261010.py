"""Offline gate for the opt-in saved I/O contract. No database access."""

from __future__ import annotations

import sys
import re
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inbound_facts_service import (  # noqa: E402
    _scope_authority_sql, _sql, build_dashboard_inbound_facts_frame, get_dashboard_inbound_facts,
)
from app.services.dashboard_inventory_frequency_snapshot import (  # noqa: E402
    SAVED_IO_AVG_PURCHASE_ALGORITHM_VERSION, SAVED_IO_FULL_SHADOW_ALGORITHM_VERSION,
    SAVED_IO_FUTURE_PROFIT_ALGORITHM_VERSION,
)
from app.services.dashboard_inventory_frequency_snapshot_service import (  # noqa: E402
    build_frequency_snapshot_plan, frequency_snapshot_key, frequency_snapshot_read_keys,
    product_statistics_raw_event_stream_sql, selected_io_snapshot_key,
)
from app.services.monthly_frequency_aggregate import (  # noqa: E402
    first_normal_inbound_month_sql, product_lifecycle_sql,
)
from app.services.saved_io_scope import resolve_saved_io_scope  # noqa: E402
from tools.snapshot_saved_io_shadow_sql_20261010 import (  # noqa: E402
    build_basis_outbound_presence_sql, build_code_summary_sql, build_exact_key_overlap_sql,
    build_pda_first_month_sql, build_price_comparison_sql,
)


COMPANY13_CODES = (
    "001", "002", "008", "011", "051", "052", "090", "093",
    "101", "102", "103", "111", "195",
    "501", "502", "506", "507", "508", "509", "511", "513", "514", "515", "516", "517",
    "519", "520", "551", "552", "553", "580", "590", "591", "596", "597", "598", "599",
    "601", "602", "611", "690", "691", "692", "696", "697",
)


def _bound_values(binds: dict, prefix: str) -> set[str]:
    return {value for key, value in binds.items() if key.startswith(prefix)}


def _expected_saved_io_read_keys(plan, scope) -> tuple:
    product_statistics = frequency_snapshot_key(plan, product_statistics=True)
    saved_io = replace(
        product_statistics,
        algorithm_version=SAVED_IO_AVG_PURCHASE_ALGORITHM_VERSION,
        profile_fingerprint=scope.fingerprint(plan.profile_fingerprint),
    )
    return (
        saved_io,
        replace(saved_io, algorithm_version=SAVED_IO_FUTURE_PROFIT_ALGORITHM_VERSION),
        replace(saved_io, algorithm_version=SAVED_IO_FULL_SHADOW_ALGORITHM_VERSION),
        product_statistics,
        frequency_snapshot_key(plan, extended=True),
        frequency_snapshot_key(plan),
    )


def test_classification() -> None:
    scope = resolve_saved_io_scope(f"0012:{code}" for code in reversed(COMPANY13_CODES))
    assert len(scope.all_codes) == 45
    assert tuple(map(len, (scope.inbound_codes, scope.inbound_return_codes, scope.outbound_codes, scope.outbound_return_codes))) == (8, 5, 24, 8)
    assert scope.normal_inbound_codes == ("001", "002")
    assert scope.price_codes == ("001", "002")
    assert scope.price_candidate_codes == ("008",)
    assert scope.unresolved_inbound_codes == ("011", "051", "052", "090", "093")
    assert scope.unclassified_codes == ()
    unclassified = resolve_saved_io_scope(("001", "0012:201", "301", "401", "501", "601", "701", "801", "901"))
    assert unclassified.unclassified_codes == ("201", "301", "401", "701", "801", "901")
    assert unclassified.unresolved_role_codes == unclassified.unclassified_codes
    assert len(unclassified.all_codes) == 9 and unclassified.normal_inbound_codes == ("001",)
    assert unclassified.all_codes == ("001", "201", "301", "401", "501", "601", "701", "801", "901")
    other_company = resolve_saved_io_scope(("001", "501", "601"))
    assert other_company.all_codes == ("001", "501", "601")
    assert other_company.fingerprint("profile") != scope.fingerprint("profile")
    pda = resolve_saved_io_scope(COMPANY13_CODES, include_pda_candidate=True)
    assert pda.normal_inbound_codes == pda.price_codes == ("001", "002", "008")
    assert pda.all_codes == scope.all_codes
    for invalid in (("1",), ("0011:001",), ()):
        try:
            resolve_saved_io_scope(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid saved scope accepted: {invalid}")


def test_snapshot_sql_and_identity() -> None:
    legacy = resolve_saved_io_scope(COMPANY13_CODES)
    pda = resolve_saved_io_scope(COMPANY13_CODES, include_pda_candidate=True)
    plan = build_frequency_snapshot_plan(
        company_id=13, evaluation_month="202610", stock_codes=("00001", "00012"),
        io_gu_codes=COMPANY13_CODES,
    )
    changed = build_frequency_snapshot_plan(
        company_id=13, evaluation_month="202610", stock_codes=("00001", "00012"),
        io_gu_codes=COMPANY13_CODES[:-1],
    )
    full_scope = resolve_saved_io_scope(plan.io_gu_codes, confirmed_trade_roles=True)
    changed_scope = resolve_saved_io_scope(changed.io_gu_codes, confirmed_trade_roles=True)
    read_keys = frequency_snapshot_read_keys(plan)
    changed_keys = frequency_snapshot_read_keys(changed)
    assert read_keys == _expected_saved_io_read_keys(plan, full_scope)
    assert changed_keys == _expected_saved_io_read_keys(changed, changed_scope)
    assert selected_io_snapshot_key(plan, full_scope) == read_keys[0]
    # All saved-I/O generations are profile-bound; only legacy fallbacks ignore I/O.
    assert all(before != after for before, after in zip(read_keys[:3], changed_keys[:3]))
    assert read_keys[3:] == changed_keys[3:]
    assert frequency_snapshot_read_keys(replace(plan, io_gu_codes=())) == read_keys[3:]
    assert selected_io_snapshot_key(plan, legacy) != frequency_snapshot_read_keys(plan)[0]
    assert selected_io_snapshot_key(plan, legacy) != selected_io_snapshot_key(plan, pda)
    assert selected_io_snapshot_key(plan, legacy) != selected_io_snapshot_key(changed, resolve_saved_io_scope(changed.io_gu_codes))

    legacy_sql, legacy_binds = product_lifecycle_sql(stock_codes=plan.stock_codes, cutoff_date="20261010")
    assert legacy_sql.count("I.Rd11_Io_Gu IN ('001', '002')") == 3
    assert not _bound_values(legacy_binds, "lifecycle_inbound_io")
    sql, binds = product_lifecycle_sql(stock_codes=plan.stock_codes, cutoff_date="20261010", io_scope=pda)
    assert sql.count("I.Rd11_Io_Gu IN (:lifecycle_inbound_io_0, :lifecycle_inbound_io_1, :lifecycle_inbound_io_2)") == 2
    assert "LTRIM(RTRIM(I.Rd11_Io_Gu)) IN" not in sql
    assert _bound_values(binds, "lifecycle_price_io") == {"001", "002", "008"}
    assert not ({"051", "052", "090", "093"} & _bound_values(binds, "lifecycle_price_io"))
    assert "SUM(CAST(I.Rd11_Quantity AS decimal(38, 6))) AS paid_quantity" in sql
    assert "SUM(CAST(COALESCE(I.Rd11_Fin_Supply_Price, I.Rd11_Supply_Price, 0) AS decimal(38, 6))) AS supply_amount" in sql
    assert "PP.supply_amount / PP.paid_quantity" in sql
    assert _bound_values(binds, "lifecycle_outbound_io") == set(pda.outbound_codes)
    assert "CurrentInbound AS" in sql and "MonthlyStock AS" in sql
    first_sql, first_binds = first_normal_inbound_month_sql(stock_codes=plan.stock_codes, cutoff_date="20261010", io_scope=pda)
    assert "first_inbound_io_2" in first_sql and _bound_values(first_binds, "first_inbound_io") == {"001", "002", "008"}

    raw_sql, raw_binds = product_statistics_raw_event_stream_sql(plan, io_scope=pda)
    assert _bound_values(raw_binds, "statistics_io") == set(pda.outbound_codes + pda.outbound_return_codes)
    assert "O.Rd12_Io_Gu_Gcode='0012'" in raw_sql
    default_sql, default_binds = product_statistics_raw_event_stream_sql(plan)
    assert not _bound_values(default_binds, "statistics_io")
    assert "O.Rd12_Io_Gu_Gcode='0012' AND LTRIM" not in default_sql
    mixed = resolve_saved_io_scope(("001", "201", "301", "401", "501", "601", "701", "801", "901"))
    mixed_plan = build_frequency_snapshot_plan(
        company_id=4, evaluation_month="202610", stock_codes=("00001",), io_gu_codes=mixed.all_codes,
    )
    assert frequency_snapshot_read_keys(mixed_plan) == (
        frequency_snapshot_key(mixed_plan, product_statistics=True),
        frequency_snapshot_key(mixed_plan, extended=True),
        frequency_snapshot_key(mixed_plan),
    )
    mixed_sql, mixed_binds = product_statistics_raw_event_stream_sql(mixed_plan, io_scope=mixed)
    assert "O.Rd12_Io_Gu IN" in mixed_sql
    assert _bound_values(mixed_binds, "statistics_io") == {"501", "601"}
    assert selected_io_snapshot_key(mixed_plan, mixed).profile_fingerprint != frequency_snapshot_read_keys(mixed_plan)[0].profile_fingerprint
    inbound_sql, inbound_binds = _sql({}, start_date="20260101", cutoff_date="20261010", io_scope=mixed)
    assert "I.Rd11_Io_Gu IN" in inbound_sql
    assert _bound_values(inbound_binds, "inbound_io") == {"001"}


def test_shadow_sql_builders() -> None:
    scope = resolve_saved_io_scope(COMPANY13_CODES)
    common = {"stock_codes": ("00001", "00012"), "date_from": "20251001", "date_to": "20260930"}
    statements = (
        build_code_summary_sql(scope, **common),
        build_price_comparison_sql(scope, **common),
        build_exact_key_overlap_sql(scope, **common),
        build_pda_first_month_sql(scope, stock_codes=common["stock_codes"], cutoff_date="20261010"),
        build_basis_outbound_presence_sql(
            scope, stock_codes=common["stock_codes"], date_from="20260701", date_to="20260930",
        ),
    )
    for sql, binds in statements:
        assert sql.lstrip().startswith(("SELECT", "WITH"))
        assert not re.search(r"\b(UPDATE|DELETE|INSERT|MERGE|CREATE|ALTER|DROP|TOP)\b", sql, re.I)
        assert set(re.findall(r":([A-Za-z][A-Za-z0-9_]*)", sql)) <= set(binds)
        assert ("I.Rd11_In_YyMmDd <= :shadow_to" in sql) or ("O.Rd12_Out_YyMmDd <= :outbound_to" in sql)
        assert ("I.Rd11_Stock_Cd IN" in sql) or ("O.Rd12_Stock_Cd IN" in sql)
        assert "LTRIM(RTRIM(I.Rd11_Io_Gu)) IN" not in sql
        assert "LTRIM(RTRIM(I.Rd11_Stock_Cd)) IN" not in sql
    code_sql, code_binds = statements[0]
    assert code_binds["summary_stock_0"] == "00001"
    assert _bound_values(code_binds, "summary_io") == set(scope.inbound_codes + scope.inbound_return_codes)
    price_sql, price_binds = statements[1]
    assert _bound_values(price_binds, "price_io") == {"001", "002", "008"}
    assert _bound_values(price_binds, "price_base") == {"001", "002"}
    assert "baseline_amount / baseline_paid" in price_sql and "candidate_amount / candidate_paid" in price_sql
    assert "legacy" not in code_sql.lower()
    overlap_sql, overlap_binds = statements[2]
    assert "Rd11_In_Seq" in overlap_sql and overlap_binds["overlap_pda"] == "008"
    first_sql, first_binds = statements[3]
    assert "GROUP BY LTRIM(RTRIM(I.Rd11_Physic_Cd))" in first_sql and first_binds["first_pda"] == "008"
    outbound_sql, outbound_binds = statements[4]
    assert "GROUP BY LTRIM(RTRIM(O.Rd12_Physic_Cd))" in outbound_sql
    assert _bound_values(outbound_binds, "outbound_io") == set(scope.outbound_codes)
    for invalid in ("20251301", "20260931"):
        try:
            build_code_summary_sql(scope, stock_codes=("00001",), date_from=invalid, date_to="20261010")
        except ValueError:
            pass
        else:
            raise AssertionError("invalid Shadow date accepted")


def test_dashboard_inbound() -> None:
    scope = resolve_saved_io_scope(COMPANY13_CODES, include_pda_candidate=True)
    sql, binds = _sql({"stock_cd_list": ["00001"]}, start_date="20260101", cutoff_date="20261010", io_scope=scope)
    assert _bound_values(binds, "inbound_io") == set(scope.normal_inbound_codes + scope.inbound_return_codes)
    assert "193" not in _bound_values(binds, "inbound_io")
    authority_sql, authority_binds = _scope_authority_sql({}, cutoff_date="20261010", vendor_lookback_days=90, io_scope=scope)
    assert "scope_inbound_io_2" in authority_sql
    assert _bound_values(authority_binds, "scope_inbound_io") == {"001", "002", "008"}
    columns = ["product_code", "inbound_date", "io_tcode", "quantity", "oquantity", "supply_price"]
    source = pd.DataFrame([
        ("A", "20261001", "001", 1, 0, 10),
        ("A", "20261002", "008", 2, 0, 20),
        ("A", "20261003", "103", 1, 0, 10),
        ("A", "20261004", "193", 10, 0, 100),
    ], columns=columns)
    legacy = build_dashboard_inbound_facts_frame(source, data_cutoff_date="20261010")
    shadow = build_dashboard_inbound_facts_frame(source, data_cutoff_date="20261010", io_scope=scope)
    assert legacy.iloc[0]["normal_inbound_positive_qty_365"] == 1
    assert shadow.iloc[0]["normal_inbound_positive_qty_365"] == 3
    assert shadow.iloc[0]["inbound_return_raw_qty_365"] == 1
    with patch("app.services.dashboard_inbound_facts_service.query_to_df", return_value=source) as query:
        facts = get_dashboard_inbound_facts({}, data_cutoff_date="20261010", io_scope=scope)
    assert query.call_count == 1 and facts.attrs["inbound_query_count"] == 1


if __name__ == "__main__":
    test_classification()
    test_snapshot_sql_and_identity()
    test_shadow_sql_builders()
    test_dashboard_inbound()
    print("saved_io_scope_shadow: 4/4 PASS (offline)")
