"""Create and Exact-check monthly I/O evidence before manual Snapshot approval."""

from __future__ import annotations

import argparse
import calendar
import getpass
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.mssql_client import get_current_company_id, get_engine, set_current_company_id  # noqa: E402
from app.services.dashboard_inventory_frequency_snapshot_service import (  # noqa: E402
    _query_company_chunks, build_frequency_snapshot_plan, product_statistics_raw_event_stream_sql,
    resolve_dashboard_profile_stock_scope,
)
from app.services.monthly_frequency_aggregate import product_lifecycle_sql  # noqa: E402
from app.services.monthly_io_components import (  # noqa: E402
    MonthlyIoFact, r110_facts, r110_historical_tail_sql, r110_monthly_base_sql,
    r110_monthly_price_sql, r120_facts_from_raw_chunks, reconstruct_purchase_prices,
    monthly_product_universe_sql,
)
from app.services.saved_io_scope import resolve_saved_io_scope  # noqa: E402
from app.services.sql_server_monthly_io_repository import (  # noqa: E402
    MonthlyIoMaterialization, SqlServerMonthlyIoRepository,
)
from app.services.ssai_analytics_target_resolver import connect_company_analytics_db  # noqa: E402


def _require_closed_source_months(months: tuple[str, ...], *, today: date) -> None:
    if any(month >= today.strftime("%Y%m") for month in months):
        raise ValueError("a new monthly draft requires a fully closed source month")


def _verified_kst_day(value: Any) -> str:
    if isinstance(value, datetime):
        verified = value
    elif isinstance(value, str) and value.strip():
        try:
            verified = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError("approved product source verification time is invalid") from exc
    else:
        raise ValueError("approved product source verification time is missing")
    if verified.tzinfo is None:
        verified = verified.replace(tzinfo=timezone.utc)
    return verified.astimezone(timezone(timedelta(hours=9))).strftime("%Y%m%d")


def _verify_approved_universe_reuse(
    *, company_id: int, current_rows: pd.DataFrame, existing: dict[str, dict[str, Any]],
    profile_fingerprint: str, scope_fingerprint: str, stock_codes: tuple[str, ...],
    source_first_dates: Callable[
        [tuple[str, ...], str, str], dict[str, dict[tuple[str, str], str | None]]
    ],
) -> dict[str, Any]:
    """Prove add-only monthly scope changes without touching approved generations."""
    registration: dict[str, str] = {}
    for row in current_rows.itertuples(index=False):
        code = "" if pd.isna(row.product_code) else str(row.product_code).strip()
        added = "" if pd.isna(row.product_add_date) else str(row.product_add_date).strip()
        if not code or (code in registration and registration[code] != added):
            raise ValueError("current product authority contains duplicate or empty codes")
        registration[code] = added
    if not registration:
        raise ValueError("stored profile product universe is empty")
    current_codes = tuple(sorted(registration))
    proof: dict[str, Any] = {"changed_months": [], "new_product_count": 0,
                             "historical_source_select_count": 0, "month_proofs": {}}
    candidates: set[str] = set()
    excluded_by_month: dict[str, set[str]] = {}
    for month, stored in sorted(existing.items()):
        fingerprint = MonthlyIoMaterialization(
            company_id, month, profile_fingerprint, scope_fingerprint,
            current_codes, stock_codes, (),
        ).product_universe_fingerprint
        if (fingerprint == stored["product_universe_fingerprint"]
                and len(current_codes) == stored["product_count"]):
            proof["month_proofs"][month] = {
                "stored_product_count": stored["product_count"],
                "reconstructed_product_count": len(current_codes),
                "stored_fingerprint": stored["product_universe_fingerprint"],
                "reconstructed_fingerprint": fingerprint,
                "fingerprint_match": True, "post_verification_candidate_count": 0,
            }
            continue
        verified_day = _verified_kst_day(stored.get("source_verified_at"))
        late_codes = {
            code for code, added in registration.items()
            if len(added) == 8 and added.isdigit() and added > verified_day
            and _valid_yyyymmdd(added)
        }
        earlier_codes = tuple(sorted(set(current_codes) - late_codes))
        earlier_fingerprint = MonthlyIoMaterialization(
            company_id, month, profile_fingerprint, scope_fingerprint,
            earlier_codes, stock_codes, (),
        ).product_universe_fingerprint
        if (len(earlier_codes) != stored["product_count"]
                or earlier_fingerprint != stored["product_universe_fingerprint"]):
            raise ValueError("product universe changed beyond proven new registrations; explicit rebuild required")
        candidates.update(late_codes)
        excluded_by_month[month] = late_codes
        proof["changed_months"].append(month)
        proof["month_proofs"][month] = {
            "source_verified_kst_date": verified_day,
            "stored_product_count": stored["product_count"],
            "reconstructed_product_count": len(earlier_codes),
            "stored_fingerprint": stored["product_universe_fingerprint"],
            "reconstructed_fingerprint": earlier_fingerprint,
            "fingerprint_match": True,
            "post_verification_candidate_count": len(late_codes),
        }
    if candidates:
        proof["new_product_count"] = len(candidates)
        first_day = f"{min(excluded_by_month)}01"
        latest_month = max(excluded_by_month)
        next_index = int(latest_month[:4]) * 12 + int(latest_month[4:])
        cutoff = f"{next_index // 12:04d}{next_index % 12 + 1:02d}01"
        first_dates = source_first_dates(tuple(sorted(candidates)), first_day, cutoff)
        if set(first_dates) != {"R110", "R120"}:
            raise ValueError("new product source evidence is incomplete")
        for source in ("R110", "R120"):
            for (code, month), first_date in first_dates[source].items():
                if (code not in candidates or not isinstance(month, str)
                        or not isinstance(first_date, str) or len(first_date) != 8
                        or not _valid_yyyymmdd(first_date) or first_date[:6] != month
                        or not first_day <= first_date < cutoff):
                    raise ValueError("new product source evidence is invalid")
                if code in excluded_by_month.get(month, set()):
                    raise ValueError("new product has historical source rows; explicit rebuild required")
    return proof


def _valid_yyyymmdd(value: str) -> bool:
    try:
        date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:]}")
    except ValueError:
        return False
    return True


def _new_product_source_first_dates(
    company_id: int, codes: tuple[str, ...], first_day: str, cutoff: str, timeout: int,
) -> tuple[dict[str, dict[tuple[str, str], str | None]], int]:
    result: dict[str, dict[tuple[str, str], str | None]] = {"R110": {}, "R120": {}}
    selects = 0
    for start in range(0, len(codes), 200):
        batch = codes[start:start + 200]
        binds: dict[str, Any] = {f"product_{index}": code for index, code in enumerate(batch)}
        binds["first_day"] = first_day
        binds["cutoff"] = cutoff
        names = ",".join(f":product_{index}" for index in range(len(batch)))
        for source, table, product, day in (
            ("R110", "Rddbc110", "Rd11_Physic_Cd", "Rd11_In_YyMmDd"),
            ("R120", "Rddbc120", "Rd12_Physic_Cd", "Rd12_Out_YyMmDd"),
        ):
            rows = _query_decimal(
                company_id,
                f"SELECT LTRIM(RTRIM({product})) AS product_code, "
                f"LEFT({day},6) AS basis_month, MIN({day}) AS first_date "
                f"FROM dbo.{table} WHERE LTRIM(RTRIM({product})) IN ({names}) "
                f"AND {day} >= :first_day AND {day} < :cutoff "
                f"GROUP BY LTRIM(RTRIM({product})), LEFT({day},6)",
                binds, timeout,
            )
            selects += 1
            for row in rows.itertuples(index=False):
                key = (str(row.product_code).strip(), str(row.basis_month).strip())
                if key in result[source]:
                    raise ValueError("duplicate new product source evidence")
                result[source][key] = str(row.first_date).strip()
    return result, selects


def _save_or_resume_monthly_draft(repository: SqlServerMonthlyIoRepository,
                                  item: MonthlyIoMaterialization, *, created_by: str) -> dict[str, Any]:
    pending = repository.read_pending_exact(item)
    resumed = pending is not None
    stored = pending or repository.save_draft(item, created_by=created_by)
    checked = repository.read_generation(item, stored.generation_no)
    if checked.materialization.checksum != item.checksum:
        raise AssertionError("incremental draft readback checksum mismatch")
    return {
        "status": "draft_pending_manual_approval", "saved": True,
        "resumed_draft": resumed, "manifest_id": stored.manifest_id,
        "generation_no": stored.generation_no,
    }


def _query_decimal(company_id: int, sql: str, binds: dict[str, Any], timeout: int) -> pd.DataFrame:
    previous = get_current_company_id()
    set_current_company_id(company_id)
    try:
        with get_engine().connect() as connection:
            raw = getattr(connection.connection, "driver_connection", connection.connection)
            raw.timeout = timeout
            return pd.read_sql_query(text(sql), connection, params=binds, coerce_float=False)
    finally:
        set_current_company_id(previous)


def _decimal(value: Any) -> Decimal | None:
    if value is None or pd.isna(value):
        return None
    return Decimal(str(value))


def _assert_exact_prices(direct: pd.DataFrame, reconstructed: dict[str, dict[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    differences: list[str] = []
    details: list[dict[str, str]] = []
    direct_only = 0
    for row in direct.itertuples(index=False):
        product = str(row.product_code).strip()
        if product in seen:
            raise AssertionError("direct lifecycle product rows are duplicated")
        seen.add(product)
        cached = reconstructed.get(product)
        if cached is None:
            if _decimal(row.avg_purchase_unit_cost) is not None:
                direct_only += 1
            continue
        expected = (
            _decimal(row.avg_purchase_unit_cost), str(row.purchase_price_basis_month or ""),
            str(row.purchase_price_basis_kind or ""), int(row.excluded_purchase_trade_count or 0),
            _decimal(row.excluded_purchase_quantity) or Decimal(0),
            _decimal(row.excluded_purchase_amount) or Decimal(0),
        )
        actual = (
            cached["unit_price"], cached["basis_month"], cached["basis_kind"],
            cached["excluded_trade_count"], cached["excluded_quantity"], cached["excluded_amount"],
        )
        if expected != actual:
            differences.append(product)
            if len(details) < 5:
                details.append({"product_code": product, "expected": str(expected), "actual": str(actual)})
    if set(reconstructed) - seen:
        raise AssertionError("monthly price contains products outside direct scope")
    return {"cached_price_products": len(reconstructed), "direct_only_price_products": direct_only,
            "price_mismatch_count": len(differences), "price_mismatch_sample": differences[:10],
            "price_mismatch_details": details}


def _assert_exact_r120(facts: tuple[MonthlyIoFact, ...], projection: tuple[Any, ...],
                       *, product_codes: set[str] | None = None) -> dict[str, int]:
    totals: dict[tuple[str, str, str], list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
    returns: dict[str, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0), Decimal(0)])
    for fact in facts:
        if fact.source_kind != "R120":
            continue
        if product_codes is not None and fact.product_code not in product_codes:
            raise AssertionError("monthly R120 fact escaped the stored product scope")
        key = (fact.basis_month, fact.product_code, fact.stock_code)
        totals[key][0] += fact.normal_event_count
        totals[key][1] += fact.normal_event_quantity
        returns[fact.product_code][0] += fact.return_event_count
        returns[fact.product_code][1] += fact.return_quantity
        returns[fact.product_code][2] += fact.return_supply_amount
    normal_mismatch = 0
    direct_keys: set[tuple[str, str, str]] = set()
    for row in projection[0]:
        if product_codes is not None and str(row["product_code"]) not in product_codes:
            continue
        key = (str(row["month"]), str(row["product_code"]), str(row["stock_code"]))
        direct_keys.add(key)
        actual = totals.get(key, [Decimal(0), Decimal(0)])
        if actual != [Decimal(str(row["occurrence_count"])), Decimal(str(row["outbound_quantity"]))]:
            normal_mismatch += 1
    normal_mismatch += sum(1 for key in set(totals) - direct_keys if totals[key] != [Decimal(0), Decimal(0)])
    return_mismatch = 0
    direct_returns = {
        str(product): expected for product, expected in projection[5].items()
        if product_codes is None or str(product) in product_codes
    }
    for product, expected in direct_returns.items():
        actual = returns.get(product, [Decimal(0), Decimal(0), Decimal(0)])
        if actual != [Decimal(str(expected["event_count"])), Decimal(str(expected["quantity"])),
                      Decimal(str(expected["supply_amount"]))]:
            return_mismatch += 1
    return_mismatch += sum(1 for product in set(returns) - set(direct_returns)
                           if returns[product] != [Decimal(0), Decimal(0), Decimal(0)])
    return {"normal_monthly_mismatch_count": normal_mismatch,
            "return_product_mismatch_count": return_mismatch,
            "normal_monthly_group_count": len(direct_keys)}


def run(company_id: int, evaluation_month: str, *, apply: bool, timeout: int,
        created_by: str, event_callback: Callable[..., None] | None = None,
        evidence_callback: Callable[[MonthlyIoMaterialization], None] | None = None) -> dict[str, Any]:
    def progress(stage: str) -> None:
        print(json.dumps({"stage": stage, "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}), flush=True)

    scope = resolve_dashboard_profile_stock_scope(company_id=company_id)
    plan = build_frequency_snapshot_plan(
        company_id=company_id, evaluation_month=evaluation_month,
        stock_codes=scope.stock_codes, product_group_codes=scope.product_group_codes,
        product_di_codes=scope.product_di_codes, product_class_codes=scope.product_class_codes,
        io_gu_codes=scope.io_gu_codes, stock_mode=scope.stock_mode,
    )
    io_scope = resolve_saved_io_scope(plan.io_gu_codes, confirmed_trade_roles=True)
    if io_scope.unclassified_codes:
        raise ValueError("stored I/O roles are not fully classified")
    scope_key = io_scope.fingerprint(plan.profile_fingerprint)
    started = time.perf_counter()
    metrics: dict[str, Any] = {"company_id": company_id, "evaluation_month": evaluation_month,
                               "stock_count": len(plan.stock_codes), "io_count": len(plan.io_gu_codes),
                               "erp_select_count": 0, "erp_queries_ms": {}, "monthly": {}}

    from datetime import datetime
    from app.services.dashboard_inventory_frequency_snapshot_service import _month_start_months_before
    lifecycle_sql, lifecycle_binds = product_lifecycle_sql(
        stock_codes=plan.stock_codes, cutoff_date=plan.basis_to,
        basis_from=plan.basis_from, basis_to=plan.basis_to, stock_mode=plan.stock_mode,
        product_group_codes=plan.product_group_codes, product_di_codes=plan.product_di_codes,
        product_class_codes=plan.product_class_codes,
        price_lookback_from=_month_start_months_before(plan.basis_from, 9),
        include_first_outbound=False, snapshot_projection_only=True,
        exclude_management_only=True, io_scope=io_scope, average_purchase_price=True,
    )
    before = time.perf_counter()
    progress("direct_source1_start")
    direct = _query_decimal(company_id, lifecycle_sql, lifecycle_binds, timeout)
    metrics["erp_select_count"] += 1
    metrics["erp_queries_ms"]["direct_source1"] = round((time.perf_counter() - before) * 1000, 3)
    product_codes = tuple(sorted({str(value).strip() for value in direct.product_code if str(value).strip()}))
    if not product_codes:
        raise ValueError("stored profile product scope is empty")
    metrics["product_count"] = len(product_codes)
    progress("direct_source1_done")
    all_facts: list[MonthlyIoFact] = []

    # The six-month seed and the prior tail are independently bounded reads.
    first_six = _month_start_months_before(plan.basis_from, 3)[:6]
    historical_month = _month_start_months_before(plan.basis_from, 4)[:6]
    for label, builder in (
        ("r110_base", lambda: r110_monthly_base_sql(plan, io_scope, first_month=first_six)),
        ("r110_price", lambda: r110_monthly_price_sql(plan, io_scope, first_month=first_six)),
        ("r110_history", lambda: r110_historical_tail_sql(plan, io_scope, historical_month=historical_month)),
    ):
        sql, binds = builder()
        before = time.perf_counter()
        progress(f"{label}_start")
        frame = _query_decimal(company_id, sql, binds, timeout)
        metrics["erp_select_count"] += 1
        metrics["erp_queries_ms"][label] = round((time.perf_counter() - before) * 1000, 3)
        metrics[f"{label}_rows"] = len(frame)
        progress(f"{label}_done")
        if label == "r110_base":
            base = frame
        elif label == "r110_price":
            prices = frame
        else:
            historical = frame
    all_facts.extend(r110_facts(base, prices))
    all_facts.extend(r110_facts(pd.DataFrame(), historical))
    price = reconstruct_purchase_prices(
        all_facts, three_month_from=plan.basis_from[:6], six_month_from=first_six,
        basis_month=plan.basis_to[:6], product_codes=product_codes,
    )
    metrics["price_exact"] = _assert_exact_prices(direct, price)
    if metrics["price_exact"]["price_mismatch_count"]:
        print(json.dumps({"stage": "price_exact_failed", "metrics": metrics}, ensure_ascii=False, default=str), flush=True)
        raise AssertionError("monthly R110 price differs from direct calculation")
    progress("price_exact_pass")

    sql, binds = product_statistics_raw_event_stream_sql(plan, io_scope=io_scope,
                                                         include_original_supply=True,
                                                         stream_from=plan.basis_from)
    before = time.perf_counter()
    progress("r120_stream_start")
    projection: dict[str, Any] = {}
    profile_products = set(product_codes)
    r120 = r120_facts_from_raw_chunks(
        _query_company_chunks(company_id, sql, binds, timeout),
        plan=plan, io_scope=io_scope, product_codes=profile_products,
        projection_result=projection,
    )
    metrics["erp_select_count"] += 1
    metrics["erp_queries_ms"]["r120_stream_and_python"] = round((time.perf_counter() - before) * 1000, 3)
    progress("r120_stream_done")
    all_facts.extend(r120)
    metrics["r120_outside_profile"] = {
        "normal_monthly_groups": sum(
            str(row["product_code"]) not in profile_products
            for row in projection["result"][0]
        ),
        "return_products": sum(
            str(product) not in profile_products
            for product in projection["result"][5]
        ),
    }
    metrics["r120_exact"] = _assert_exact_r120(r120, projection["result"], product_codes=profile_products)
    if (metrics["r120_exact"]["normal_monthly_mismatch_count"] or
            metrics["r120_exact"]["return_product_mismatch_count"]):
        print(json.dumps({"stage": "exact_failed", "metrics": metrics}, ensure_ascii=False, default=str), flush=True)
        raise AssertionError("monthly I/O projection differs from direct calculation")
    progress("exact_pass")

    repository = None
    if apply:
        repository = SqlServerMonthlyIoRepository(
            lambda: connect_company_analytics_db(company_id, "reader", timeout=timeout),
            lambda: connect_company_analytics_db(company_id, "writer", timeout=timeout),
        )
    by_month: dict[str, list[MonthlyIoFact]] = defaultdict(list)
    for fact in all_facts:
        by_month[fact.basis_month].append(fact)
    first_index = int(first_six[:4]) * 12 + int(first_six[4:]) - 1
    required_months = {
        f"{index // 12:04d}{index % 12 + 1:02d}"
        for index in range(first_index, first_index + 6)
    }
    months = sorted(set(by_month) | required_months | {historical_month})
    if apply:
        _require_closed_source_months(tuple(months), today=date.today())
    for month in months:
        item = MonthlyIoMaterialization(
            company_id, month, plan.profile_fingerprint, scope_key,
            product_codes, plan.stock_codes, tuple(by_month.get(month, ())),
        )
        item.validate()
        if evidence_callback is not None:
            evidence_callback(item)
        metrics["monthly"][month] = {
            "status": "component_validated_draft_ready", "fact_count": len(item.facts),
            "checksum": item.checksum, "saved": False,
        }
        if repository is not None:
            try:
                existing = repository.read_current(item)
            except LookupError:
                existing = None
            if existing is not None:
                if existing.materialization.checksum != item.checksum:
                    raise AssertionError("approved monthly evidence changed; explicit regeneration review required")
                metrics["monthly"][month].update({
                    "status": "reused_source_checked", "saved": True,
                    "manifest_id": existing.manifest_id,
                    "generation_no": existing.generation_no,
                    "source_verified_at": str(existing.source_verified_at),
                })
                continue
            saved = _save_or_resume_monthly_draft(repository, item, created_by=created_by)
            metrics["monthly"][month].update(saved)
            if event_callback:
                event_callback(month, saved["manifest_id"], saved["generation_no"],
                               item.checksum, len(item.facts), "pending")
            progress(f"monthly_{month}_draft_saved")
    metrics["fact_count"] = len(all_facts)
    metrics["total_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return metrics


def run_incremental(company_id: int, evaluation_month: str, *, apply: bool, plan_only: bool,
                    timeout: int, created_by: str,
                    event_callback: Callable[..., None] | None = None,
                    evidence_callback: Callable[[MonthlyIoMaterialization], None] | None = None) -> dict[str, Any]:
    from app.services.dashboard_inventory_frequency_snapshot_service import _month_start_months_before

    scope = resolve_dashboard_profile_stock_scope(company_id=company_id)
    plan = build_frequency_snapshot_plan(
        company_id=company_id, evaluation_month=evaluation_month,
        stock_codes=scope.stock_codes, product_group_codes=scope.product_group_codes,
        product_di_codes=scope.product_di_codes, product_class_codes=scope.product_class_codes,
        io_gu_codes=scope.io_gu_codes, stock_mode=scope.stock_mode,
    )
    io_scope = resolve_saved_io_scope(plan.io_gu_codes, confirmed_trade_roles=True)
    if io_scope.unclassified_codes:
        raise ValueError("stored I/O roles are not fully classified")
    scope_key = io_scope.fingerprint(plan.profile_fingerprint)
    repository = SqlServerMonthlyIoRepository(
        lambda: connect_company_analytics_db(company_id, "reader", timeout=timeout),
        lambda: connect_company_analytics_db(company_id, "writer", timeout=timeout),
    )
    first_six = _month_start_months_before(plan.basis_from, 3)[:6]
    first_index = int(first_six[:4]) * 12 + int(first_six[4:]) - 1
    purchase_months = tuple(
        f"{index // 12:04d}{index % 12 + 1:02d}"
        for index in range(first_index, first_index + 6)
    )
    historical_months = repository.list_approved_months_before(
        company_id=company_id, before_month=first_six,
        profile_fingerprint=plan.profile_fingerprint, scope_fingerprint=scope_key,
    )
    if not historical_months:
        raise ValueError("approved historical price seed is missing; use the full initial build")
    first_historical = int(historical_months[0][:4]) * 12 + int(historical_months[0][4:]) - 1
    expected_historical = tuple(
        f"{index // 12:04d}{index % 12 + 1:02d}"
        for index in range(first_historical, first_index)
    )
    if historical_months != expected_historical:
        raise ValueError("approved historical price months are incomplete")
    required_months = historical_months + purchase_months
    existing: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for month in required_months:
        try:
            existing[month] = repository.read_approved_manifest_for_scope(
                company_id=company_id, basis_month=month,
                profile_fingerprint=plan.profile_fingerprint, scope_fingerprint=scope_key,
                stock_codes=plan.stock_codes,
            )
        except LookupError:
            missing.append(month)
    if not existing:
        raise ValueError("approved monthly product universe evidence is missing")
    result: dict[str, Any] = {
        "company_id": company_id, "evaluation_month": evaluation_month,
        "reused_months": tuple(existing), "missing_months": tuple(missing),
        "reuse_status": "approved_without_source_revalidation",
        "last_source_verified_at": {
            month: str(item["source_verified_at"]) for month, item in existing.items()
        },
        "erp_select_count": 0, "new_months": {},
    }
    if plan_only or not missing:
        return result
    if any(month not in purchase_months for month in missing):
        raise ValueError("historical seed must be explicitly rebuilt and Exact checked")
    if apply:
        _require_closed_source_months(tuple(missing), today=date.today())
    product_sql, product_binds = monthly_product_universe_sql(plan, include_add_date=True)
    master = _query_decimal(company_id, product_sql, product_binds, timeout)
    result["erp_select_count"] += 1
    product_codes = tuple(sorted({str(value).strip() for value in master.product_code if str(value).strip()}))
    if not product_codes:
        raise ValueError("stored profile product universe is empty")
    source_selects = 0

    def first_dates(
        codes: tuple[str, ...], first_day: str, cutoff: str,
    ) -> dict[str, dict[tuple[str, str], str | None]]:
        nonlocal source_selects
        found, selects = _new_product_source_first_dates(company_id, codes, first_day, cutoff, timeout)
        source_selects += selects
        return found

    universe_proof = _verify_approved_universe_reuse(
        company_id=company_id, current_rows=master, existing=existing,
        profile_fingerprint=plan.profile_fingerprint, scope_fingerprint=scope_key,
        stock_codes=plan.stock_codes, source_first_dates=first_dates,
    )
    universe_proof["historical_source_select_count"] = source_selects
    result["erp_select_count"] += source_selects
    result["product_universe_reuse_proof"] = universe_proof
    if universe_proof["changed_months"]:
        result["reuse_status"] = "approved_with_new_product_source_absence_verified"
    for month in missing:
        month_end = f"{month}{calendar.monthrange(int(month[:4]), int(month[4:]))[1]:02d}"
        month_plan = replace(plan, basis_from=f"{month}01", basis_to=month_end,
                             basis_months=(month, month, month))
        base_sql, base_binds = r110_monthly_base_sql(month_plan, io_scope, first_month=month, last_month=month)
        price_sql, price_binds = r110_monthly_price_sql(month_plan, io_scope, first_month=month, last_month=month)
        base = _query_decimal(company_id, base_sql, base_binds, timeout)
        prices = _query_decimal(company_id, price_sql, price_binds, timeout)
        result["erp_select_count"] += 2
        facts = list(r110_facts(base, prices))
        if month in plan.basis_months:
            stream_sql, stream_binds = product_statistics_raw_event_stream_sql(
                month_plan, io_scope=io_scope, include_original_supply=True, stream_from=f"{month}01",
            )
            projection: dict[str, Any] = {}
            r120 = r120_facts_from_raw_chunks(
                _query_company_chunks(company_id, stream_sql, stream_binds, timeout),
                plan=month_plan, io_scope=io_scope, product_codes=set(product_codes),
                projection_result=projection,
            )
            result["erp_select_count"] += 1
            check = _assert_exact_r120(r120, projection["result"], product_codes=set(product_codes))
            if check["normal_monthly_mismatch_count"] or check["return_product_mismatch_count"]:
                raise AssertionError("incremental R120 projection differs from direct calculation")
            facts.extend(r120)
        item = MonthlyIoMaterialization(
            company_id, month, plan.profile_fingerprint, scope_key,
            product_codes, plan.stock_codes, tuple(facts),
        )
        item.validate()
        if evidence_callback is not None:
            evidence_callback(item)
        result["new_months"][month] = {
            "status": "component_validated_draft_ready", "fact_count": len(facts), "checksum": item.checksum,
            "saved": False, "empty_source_month": not bool(facts),
        }
        if apply:
            result["new_months"][month].update(
                _save_or_resume_monthly_draft(repository, item, created_by=created_by)
            )
            if event_callback:
                saved = result["new_months"][month]
                event_callback(month, saved["manifest_id"], saved["generation_no"],
                               item.checksum, len(item.facts), "draft")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company-id", type=int, required=True)
    parser.add_argument("--evaluation-month", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--created-by", default=getpass.getuser())
    parser.add_argument("--incremental", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    from app.services.snapshot_operation_history import OperationEvent, record_event, utc_now
    from uuid import uuid4

    run_id = str(uuid4())
    started_at = utc_now()
    started = time.perf_counter()

    def monthly_event(month: str, manifest_id: int, generation_no: int,
                      checksum: str, fact_count: int, approval_status: str) -> None:
        record_event(OperationEvent(
            args.company_id, "monthly_build", "month_committed", "stage_complete",
            run_id=run_id, started_at=started_at, finished_at=utc_now(),
            evaluation_month=args.evaluation_month, basis_month=month,
            manifest_id=manifest_id, generation_no=generation_no,
            fact_count=fact_count, whole_checksum=checksum,
            approval_status=approval_status,
        ))

    try:
        if args.plan_only and not args.incremental:
            parser.error("--plan-only requires --incremental")
        if args.apply and not args.plan_only:
            record_event(OperationEvent(args.company_id, "monthly_build", "source_start", "started",
                                        run_id=run_id, started_at=started_at,
                                        evaluation_month=args.evaluation_month))
        if args.incremental:
            result = run_incremental(
                args.company_id, args.evaluation_month, apply=args.apply,
                plan_only=args.plan_only, timeout=args.timeout_seconds, created_by=args.created_by,
                event_callback=monthly_event if args.apply and not args.plan_only else None,
            )
        else:
            result = run(args.company_id, args.evaluation_month, apply=args.apply,
                         timeout=args.timeout_seconds, created_by=args.created_by,
                         event_callback=monthly_event if args.apply else None)
        if args.apply and not args.plan_only:
            reused_months = result.get("reused_months") or tuple(
                month for month, item in result.get("monthly", {}).items()
                if str(item.get("status", "")).startswith("reused")
            )
            record_event(OperationEvent(
                args.company_id, "monthly_build", "run_complete", "succeeded",
                run_id=run_id, started_at=started_at, finished_at=utc_now(),
                evaluation_month=args.evaluation_month,
                total_ms=round((time.perf_counter() - started) * 1000, 3),
                product_count=result.get("product_count"), fact_count=result.get("fact_count"),
                details={"source_call_count": result.get("erp_select_count"),
                         "reused_months": list(reused_months)},
            ))
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        if args.apply and not args.plan_only:
            record_event(OperationEvent(
                args.company_id, "monthly_build", "run_failed", "failed",
                run_id=run_id, started_at=started_at, finished_at=utc_now(),
                evaluation_month=args.evaluation_month,
                total_ms=round((time.perf_counter() - started) * 1000, 3),
                error_code=type(exc).__name__,
            ))
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
