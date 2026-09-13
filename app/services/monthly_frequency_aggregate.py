from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from app.services.dashboard_inventory_frequency_snapshot import (
    ALGORITHM_VERSION,
    RELATIONAL_DIAGNOSTIC_FIELDS,
    SCHEMA_VERSION,
    RelationalFrequencySnapshot,
    SnapshotContractError,
    build_relational_frequency_snapshot_from_aggregates,
    completed_month_basis,
    scope_fingerprint,
    validate_relational_frequency_snapshot,
)


MONTHLY_FREQUENCY_SCHEMA_VERSION = "1.0"
MONTHLY_FREQUENCY_ALGORITHM_VERSION = "outbound_frequency_month_v1"
PRODUCT_LIFECYCLE_SCHEMA_VERSION = "1.0"
PRODUCT_LIFECYCLE_ALGORITHM_VERSION = "product_lifecycle_v1"
OPERATING_ANALYSIS_WINDOW_MONTHS = 12
DEFAULT_RETENTION_MONTHS = 24
SUPPORTED_RETENTION_MONTHS = (24, 36)


@dataclass(frozen=True)
class MonthlyFrequencyFact:
    company_id: str
    scope_fingerprint: str
    basis_month: str
    product_code: str
    stock_code: str
    occurrence_count: int
    outbound_quantity: int
    outbound_day_count: int

    def as_snapshot_row(self) -> dict[str, Any]:
        return {
            "month": self.basis_month,
            "product_code": self.product_code,
            "stock_code": self.stock_code,
            "occurrence_count": self.occurrence_count,
            "outbound_quantity": self.outbound_quantity,
            "outbound_day_count": self.outbound_day_count,
        }


@dataclass(frozen=True)
class MonthlyProductLifecycle:
    product_code: str
    product_registered_date: str | None
    first_normal_inbound_date: str | None
    first_normal_inbound_month: str | None
    first_outbound_date: str | None
    lifecycle_status: str
    registered_after_inbound: bool = False
    outbound_before_inbound: bool = False
    long_registration_to_inbound_gap: bool = False
    registration_to_inbound_days: int | None = None
    review_required: bool = False
    quality_status: str = "ready"


@dataclass(frozen=True)
class ProductLifecycleAuthority:
    schema_version: str
    algorithm_version: str
    company_id: str
    stock_codes: tuple[str, ...]
    scope_fingerprint: str
    product_codes: tuple[str, ...]
    product_universe_fingerprint: str
    source_fingerprint: str
    source_watermark: str | None
    source_watermark_status: str
    lifecycle: tuple[MonthlyProductLifecycle, ...]
    checksum: str


@dataclass(frozen=True)
class SourceFreshnessAssessment:
    status: str
    evidence: str
    watermark_verified: bool
    reason: str


@dataclass(frozen=True)
class ProductLifecycleRefreshPlan:
    mode: str
    candidate_product_codes: tuple[str, ...]
    candidate_count: int
    requires_full_source_scan: bool
    write_required: bool
    reason: str


@dataclass(frozen=True)
class MonthlyFrequencyMaterialization:
    monthly_schema_version: str
    monthly_algorithm_version: str
    company_id: str
    basis_month: str
    stock_codes: tuple[str, ...]
    scope_fingerprint: str
    source_fingerprint: str
    source_watermark: str | None
    source_watermark_status: str
    source_diagnostics: Mapping[str, int]
    facts: tuple[MonthlyFrequencyFact, ...]
    product_codes: tuple[str, ...] = ()
    product_universe_fingerprint: str = ""
    lifecycle: tuple[MonthlyProductLifecycle, ...] = ()
    checksum: str = ""
    lifecycle_authority_manifest_id: int | None = None
    lifecycle_authority_checksum: str = ""

    @property
    def erp_source_call_count(self) -> int:
        return 1


@dataclass(frozen=True)
class MonthlyFrequencyAggregateWindow:
    monthly_schema_version: str
    monthly_algorithm_version: str
    company_id: str
    evaluation_month: str
    basis_months: tuple[str, str, str]
    stock_codes: tuple[str, ...]
    scope_fingerprint: str
    product_codes: tuple[str, ...]
    product_universe_fingerprint: str
    source_fingerprint: str
    source_watermark: str | None
    source_watermark_status: str
    source_contract: Mapping[str, Any]
    source_diagnostics: Mapping[str, Any]
    source_schema_version: str
    source_algorithm_version: str
    expected_snapshot_checksum: str
    facts: tuple[MonthlyFrequencyFact, ...]

    @property
    def erp_source_call_count(self) -> int:
        return 0


@dataclass(frozen=True)
class NewProductFrequencyProjection:
    product_code: str
    occurrence_count_3m: int
    legacy_frequency_grade: str
    frequency_grade: str
    first_normal_inbound_month: str | None
    lifecycle_status: str


def _product_universe_fingerprint(product_codes: tuple[str, ...]) -> str:
    encoded = "\n".join(product_codes).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _month_text(value: Any, *, field: str) -> str:
    month = str(value or "").strip()
    if len(month) != 6 or not month.isdigit() or not 1 <= int(month[4:]) <= 12:
        raise SnapshotContractError(f"{field} must be YYYYMM")
    return month


def _monthly_source_fingerprint(
    *,
    company_id: str,
    basis_month: str,
    scope_value: str,
    facts: Sequence[MonthlyFrequencyFact],
    diagnostics: Mapping[str, int],
) -> str:
    payload = {
        "algorithm_version": MONTHLY_FREQUENCY_ALGORITHM_VERSION,
        "basis_month": basis_month,
        "company_id": company_id,
        "diagnostics": {str(key): int(value) for key, value in sorted(diagnostics.items())},
        "facts": [fact.as_snapshot_row() for fact in sorted(
            facts,
            key=lambda item: (item.basis_month, item.product_code, item.stock_code),
        )],
        "schema_version": MONTHLY_FREQUENCY_SCHEMA_VERSION,
        "scope_fingerprint": scope_value,
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _date_text(value: Any, *, field: str) -> str | None:
    text = str(value or "").strip().replace("-", "").replace("/", "")
    if not text:
        return None
    if len(text) != 8 or not text.isdigit():
        raise SnapshotContractError(f"{field} must be YYYYMMDD")
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError as exc:
        raise SnapshotContractError(f"{field} must be a valid date") from exc
    return text


def _materialization_checksum(
    *,
    company_id: str,
    basis_month: str,
    scope_value: str,
    product_universe_fingerprint: str,
    source_fingerprint: str,
    source_watermark: str | None,
    source_watermark_status: str,
    facts: Sequence[MonthlyFrequencyFact],
    lifecycle: Sequence[MonthlyProductLifecycle],
    lifecycle_authority_checksum: str = "",
) -> str:
    payload = {
        "algorithm_version": MONTHLY_FREQUENCY_ALGORITHM_VERSION,
        "basis_month": basis_month,
        "company_id": company_id,
        "facts": [fact.as_snapshot_row() for fact in facts],
        # Preserve the Phase 3 inline-lifecycle checksum contract. Quality fields
        # belong to the independent lifecycle authority introduced in Phase 3.1.
        "lifecycle": [
            {
                "product_code": row.product_code,
                "product_registered_date": row.product_registered_date,
                "first_normal_inbound_date": row.first_normal_inbound_date,
                "first_normal_inbound_month": row.first_normal_inbound_month,
                "first_outbound_date": row.first_outbound_date,
                "lifecycle_status": row.lifecycle_status,
            }
            for row in lifecycle
        ],
        "product_universe_fingerprint": product_universe_fingerprint,
        "schema_version": MONTHLY_FREQUENCY_SCHEMA_VERSION,
        "scope_fingerprint": scope_value,
        "source_fingerprint": source_fingerprint,
        "source_watermark": source_watermark,
        "source_watermark_status": source_watermark_status,
    }
    if lifecycle_authority_checksum:
        payload["lifecycle_authority_checksum"] = lifecycle_authority_checksum
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _build_lifecycle_rows(
    *,
    product_codes: Sequence[str],
    lifecycle_rows: Iterable[Mapping[str, Any]],
) -> tuple[MonthlyProductLifecycle, ...]:
    products = set(product_codes)
    lifecycle: list[MonthlyProductLifecycle] = []
    seen: set[str] = set()
    for row in lifecycle_rows:
        product_code = str(row.get("product_code") or "").strip()
        if not product_code or product_code in seen or product_code not in products:
            raise SnapshotContractError("monthly lifecycle ownership is invalid")
        seen.add(product_code)
        try:
            registered = _date_text(row.get("product_registered_date"), field="product_registered_date")
            inbound = _date_text(row.get("first_normal_inbound_date"), field="first_normal_inbound_date")
            outbound = _date_text(row.get("first_outbound_date"), field="first_outbound_date")
        except SnapshotContractError:
            lifecycle.append(MonthlyProductLifecycle(
                product_code, None, None, None, None, "invalid",
                review_required=True, quality_status="invalid",
            ))
            continue
        registered_after_inbound = bool(registered and inbound and registered > inbound)
        outbound_before_inbound = bool(outbound and inbound and outbound < inbound)
        gap_days = None
        if registered and inbound:
            gap_days = (datetime.strptime(inbound, "%Y%m%d") - datetime.strptime(registered, "%Y%m%d")).days
        long_gap = bool(gap_days is not None and gap_days > 365)
        review_required = registered_after_inbound or outbound_before_inbound or long_gap
        lifecycle.append(MonthlyProductLifecycle(
            product_code=product_code,
            product_registered_date=registered,
            first_normal_inbound_date=inbound,
            first_normal_inbound_month=inbound[:6] if inbound else None,
            first_outbound_date=outbound,
            lifecycle_status="verified" if inbound else "insufficient",
            registered_after_inbound=registered_after_inbound,
            outbound_before_inbound=outbound_before_inbound,
            long_registration_to_inbound_gap=long_gap,
            registration_to_inbound_days=gap_days,
            review_required=review_required,
            quality_status=("review_required" if review_required else "ready") if inbound else "insufficient",
        ))
    return tuple(sorted(lifecycle, key=lambda item: item.product_code))


def build_product_lifecycle_authority(
    *,
    company_id: Any,
    stock_codes: Iterable[Any],
    product_codes: Iterable[Any],
    lifecycle_rows: Iterable[Mapping[str, Any]],
    source_watermark: Any = None,
    source_watermark_status: str = "unverified",
) -> ProductLifecycleAuthority:
    company = str(company_id or "").strip()
    if not company:
        raise SnapshotContractError("company_id is required")
    stocks = tuple(sorted({str(value or "").strip() for value in stock_codes if str(value or "").strip()}))
    products = tuple(sorted({str(value or "").strip() for value in product_codes if str(value or "").strip()}))
    if not products:
        raise SnapshotContractError("product lifecycle authority requires a product universe")
    watermark_status = str(source_watermark_status or "unverified")
    if watermark_status not in {"verified", "unverified"}:
        raise SnapshotContractError("lifecycle source watermark status is invalid")
    lifecycle = _build_lifecycle_rows(product_codes=products, lifecycle_rows=lifecycle_rows)
    if len(lifecycle) != len(products):
        raise SnapshotContractError("product lifecycle authority must cover the complete product universe")
    scope_value = scope_fingerprint(stocks)
    universe_fingerprint = _product_universe_fingerprint(products)
    source_payload = {
        "company_id": company,
        "lifecycle": [row.__dict__ for row in lifecycle],
        "scope_fingerprint": scope_value,
    }
    source_fingerprint = hashlib.sha256(json.dumps(
        source_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    watermark = None if source_watermark is None else str(source_watermark)
    checksum_payload = {
        "algorithm_version": PRODUCT_LIFECYCLE_ALGORITHM_VERSION,
        "company_id": company,
        "lifecycle": [row.__dict__ for row in lifecycle],
        "product_universe_fingerprint": universe_fingerprint,
        "schema_version": PRODUCT_LIFECYCLE_SCHEMA_VERSION,
        "scope_fingerprint": scope_value,
        "source_fingerprint": source_fingerprint,
        "source_watermark": watermark,
        "source_watermark_status": watermark_status,
    }
    checksum = hashlib.sha256(json.dumps(
        checksum_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return ProductLifecycleAuthority(
        schema_version=PRODUCT_LIFECYCLE_SCHEMA_VERSION,
        algorithm_version=PRODUCT_LIFECYCLE_ALGORITHM_VERSION,
        company_id=company,
        stock_codes=stocks,
        scope_fingerprint=scope_value,
        product_codes=products,
        product_universe_fingerprint=universe_fingerprint,
        source_fingerprint=source_fingerprint,
        source_watermark=watermark,
        source_watermark_status=watermark_status,
        lifecycle=lifecycle,
        checksum=checksum,
    )


def assess_source_freshness(
    *,
    stored_scope_fingerprint: str,
    expected_scope_fingerprint: str,
    stored_source_fingerprint: str,
    observed_source_fingerprint: str | None,
    stored_watermark: str | None,
    observed_watermark: str | None,
    stored_watermark_status: str,
    observed_watermark_status: str,
) -> SourceFreshnessAssessment:
    """Classify freshness without presenting an unverified watermark as proof."""
    if stored_scope_fingerprint != expected_scope_fingerprint:
        return SourceFreshnessAssessment("stale", "scope_mismatch", False, "scope fingerprint changed")
    stored_status = str(stored_watermark_status or "unverified")
    observed_status = str(observed_watermark_status or "unverified")
    if stored_status not in {"verified", "unverified"} or observed_status not in {"verified", "unverified"}:
        raise SnapshotContractError("source watermark status is invalid")
    if stored_status == "verified":
        if observed_status != "verified" or not stored_watermark or not observed_watermark:
            return SourceFreshnessAssessment(
                "unverified", "watermark_not_observed", False,
                "verified source watermark was not observed with the same contract",
            )
        if stored_watermark != observed_watermark:
            return SourceFreshnessAssessment("stale", "watermark_changed", True, "source watermark changed")
        if observed_source_fingerprint and observed_source_fingerprint != stored_source_fingerprint:
            return SourceFreshnessAssessment("stale", "fingerprint_changed", True, "source fingerprint changed")
        return SourceFreshnessAssessment("current", "verified_watermark", True, "verified source watermark matches")
    if not observed_source_fingerprint:
        return SourceFreshnessAssessment(
            "unverified", "fingerprint_not_observed", False,
            "source has no verified watermark and no current fingerprint was computed",
        )
    if observed_source_fingerprint != stored_source_fingerprint:
        return SourceFreshnessAssessment(
            "stale", "fingerprint_changed", False,
            "exact source fingerprint changed under an unverified watermark contract",
        )
    return SourceFreshnessAssessment(
        "current", "exact_fingerprint", False,
        "full deterministic source scan matches; watermark remains unverified",
    )


def plan_product_lifecycle_refresh(
    current: ProductLifecycleAuthority,
    observed: ProductLifecycleAuthority,
    *,
    complete_change_tracking: bool,
) -> ProductLifecycleRefreshPlan:
    """Plan immutable refresh; partial refresh requires complete source change tracking."""
    if (
        current.company_id != observed.company_id
        or current.scope_fingerprint != observed.scope_fingerprint
        or current.schema_version != observed.schema_version
        or current.algorithm_version != observed.algorithm_version
    ):
        raise SnapshotContractError("lifecycle refresh identity changed")
    current_rows = {row.product_code: row for row in current.lifecycle}
    observed_rows = {row.product_code: row for row in observed.lifecycle}
    candidates = tuple(sorted(
        code for code in set(current_rows) | set(observed_rows)
        if current_rows.get(code) != observed_rows.get(code)
    ))
    if not candidates and current.checksum == observed.checksum:
        return ProductLifecycleRefreshPlan(
            mode="reuse", candidate_product_codes=(), candidate_count=0,
            requires_full_source_scan=not complete_change_tracking, write_required=False,
            reason="approved lifecycle authority exactly matches observed source",
        )
    if complete_change_tracking:
        return ProductLifecycleRefreshPlan(
            mode="incremental_candidates", candidate_product_codes=candidates,
            candidate_count=len(candidates), requires_full_source_scan=False, write_required=True,
            reason="complete change tracking identified lifecycle candidates; publish a new immutable authority",
        )
    return ProductLifecycleRefreshPlan(
        mode="full_replace", candidate_product_codes=candidates,
        candidate_count=len(candidates), requires_full_source_scan=True, write_required=True,
        reason="source lacks complete change tracking; full scan is required before immutable replacement",
    )


def build_monthly_frequency_materialization(
    *,
    company_id: Any,
    basis_month: Any,
    stock_codes: Iterable[Any],
    monthly_rows: Iterable[Mapping[str, Any]],
    source_diagnostics: Mapping[str, Any],
    source_watermark: Any = None,
    source_watermark_status: str = "unverified",
    product_codes: Iterable[Any] = (),
    lifecycle_rows: Iterable[Mapping[str, Any]] = (),
    lifecycle_authority_manifest_id: int | None = None,
    lifecycle_authority_checksum: str = "",
) -> MonthlyFrequencyMaterialization:
    """Build one immutable month fact set from the canonical outbound aggregate."""
    company = str(company_id or "").strip()
    month = _month_text(basis_month, field="basis_month")
    if not company:
        raise SnapshotContractError("company_id is required")
    stocks = tuple(sorted({str(value or "").strip() for value in stock_codes if str(value or "").strip()}))
    scope_value = scope_fingerprint(stocks)
    facts: list[MonthlyFrequencyFact] = []
    seen: set[tuple[str, str]] = set()
    for row in monthly_rows:
        row_month = _month_text(row.get("month"), field="monthly fact month")
        product_code = str(row.get("product_code") or "").strip()
        stock_code = str(row.get("stock_code") or "").strip()
        try:
            occurrence_count = int(row.get("occurrence_count"))
            outbound_quantity = int(row.get("outbound_quantity"))
            outbound_day_count = int(row.get("outbound_day_count"))
        except (TypeError, ValueError) as exc:
            raise SnapshotContractError("monthly frequency fact aggregate is invalid") from exc
        key = (product_code, stock_code)
        if (
            row_month != month
            or not product_code
            or not stock_code
            or key in seen
            or (stocks and stock_code not in stocks)
            or occurrence_count <= 0
            or outbound_quantity <= 0
            or not 0 < outbound_day_count <= occurrence_count
        ):
            raise SnapshotContractError("monthly frequency fact aggregate is invalid")
        seen.add(key)
        facts.append(MonthlyFrequencyFact(
            company_id=company,
            scope_fingerprint=scope_value,
            basis_month=month,
            product_code=product_code,
            stock_code=stock_code,
            occurrence_count=occurrence_count,
            outbound_quantity=outbound_quantity,
            outbound_day_count=outbound_day_count,
        ))
    diagnostics: dict[str, int] = {}
    diagnostic_keys = (*RELATIONAL_DIAGNOSTIC_FIELDS, "diagnostic_contract_version")
    for key in diagnostic_keys:
        value = source_diagnostics.get(key, 0)
        try:
            normalized = int(value or 0)
        except (TypeError, ValueError) as exc:
            raise SnapshotContractError("monthly source diagnostics are invalid") from exc
        if normalized < 0:
            raise SnapshotContractError("monthly source diagnostics are invalid")
        diagnostics[str(key)] = normalized
    watermark_status = str(source_watermark_status or "unverified")
    if watermark_status not in {"verified", "unverified"}:
        raise SnapshotContractError("monthly source watermark status is invalid")
    fact_tuple = tuple(sorted(facts, key=lambda item: (item.product_code, item.stock_code)))
    products = tuple(sorted({str(value or "").strip() for value in product_codes if str(value or "").strip()}))
    if not products:
        products = tuple(sorted({fact.product_code for fact in fact_tuple}))
    lifecycle_tuple = _build_lifecycle_rows(product_codes=products, lifecycle_rows=lifecycle_rows)
    universe_fingerprint = _product_universe_fingerprint(products)
    watermark = None if source_watermark is None else str(source_watermark)
    source_fingerprint = _monthly_source_fingerprint(
        company_id=company,
        basis_month=month,
        scope_value=scope_value,
        facts=fact_tuple,
        diagnostics=diagnostics,
    )
    return MonthlyFrequencyMaterialization(
        monthly_schema_version=MONTHLY_FREQUENCY_SCHEMA_VERSION,
        monthly_algorithm_version=MONTHLY_FREQUENCY_ALGORITHM_VERSION,
        company_id=company,
        basis_month=month,
        stock_codes=stocks,
        scope_fingerprint=scope_value,
        source_fingerprint=source_fingerprint,
        source_watermark=watermark,
        source_watermark_status=watermark_status,
        source_diagnostics=diagnostics,
        facts=fact_tuple,
        product_codes=products,
        product_universe_fingerprint=universe_fingerprint,
        lifecycle=lifecycle_tuple,
        checksum=_materialization_checksum(
            company_id=company,
            basis_month=month,
            scope_value=scope_value,
            product_universe_fingerprint=universe_fingerprint,
            source_fingerprint=source_fingerprint,
            source_watermark=watermark,
            source_watermark_status=watermark_status,
            facts=fact_tuple,
            lifecycle=lifecycle_tuple,
            lifecycle_authority_checksum=str(lifecycle_authority_checksum or ""),
        ),
        lifecycle_authority_manifest_id=lifecycle_authority_manifest_id,
        lifecycle_authority_checksum=str(lifecycle_authority_checksum or ""),
    )


def extract_monthly_frequency_window(
    snapshot: RelationalFrequencySnapshot,
) -> MonthlyFrequencyAggregateWindow:
    """Expose trusted relational monthly rows without querying ERP again."""
    validate_relational_frequency_snapshot(snapshot)
    basis = completed_month_basis(snapshot.key.evaluation_month)
    product_codes = tuple(
        sorted(str(row.get("product_code") or "") for row in snapshot.frequency_products)
    )
    facts = tuple(
        MonthlyFrequencyFact(
            company_id=snapshot.key.company_id,
            scope_fingerprint=snapshot.key.scope_fingerprint,
            basis_month=str(row.get("month") or ""),
            product_code=str(row.get("product_code") or ""),
            stock_code=str(row.get("stock_code") or ""),
            occurrence_count=int(row.get("occurrence_count") or 0),
            outbound_quantity=int(row.get("outbound_quantity") or 0),
            outbound_day_count=int(row.get("outbound_day_count") or 0),
        )
        for row in snapshot.monthly_activity
    )
    return MonthlyFrequencyAggregateWindow(
        monthly_schema_version=MONTHLY_FREQUENCY_SCHEMA_VERSION,
        monthly_algorithm_version=MONTHLY_FREQUENCY_ALGORITHM_VERSION,
        company_id=snapshot.key.company_id,
        evaluation_month=snapshot.key.evaluation_month,
        basis_months=basis.months,
        stock_codes=snapshot.stock_codes,
        scope_fingerprint=snapshot.key.scope_fingerprint,
        product_codes=product_codes,
        product_universe_fingerprint=_product_universe_fingerprint(product_codes),
        source_fingerprint=snapshot.source_fingerprint,
        source_watermark=snapshot.source_watermark,
        source_watermark_status=snapshot.source_watermark_status,
        source_contract=dict(snapshot.source_contract),
        source_diagnostics=dict(snapshot.source_diagnostics),
        source_schema_version=snapshot.key.schema_version,
        source_algorithm_version=snapshot.key.algorithm_version,
        expected_snapshot_checksum=snapshot.checksum,
        facts=facts,
    )


def compose_frequency_window(
    reference: MonthlyFrequencyAggregateWindow,
    *,
    reused_months: Iterable[Any],
    reused_facts: Iterable[MonthlyFrequencyFact],
    materialized_month: MonthlyFrequencyMaterialization,
) -> MonthlyFrequencyAggregateWindow:
    """Compose a shadow three-month window while retaining reference provenance."""
    if (
        materialized_month.company_id != reference.company_id
        or materialized_month.stock_codes != reference.stock_codes
        or materialized_month.scope_fingerprint != reference.scope_fingerprint
        or materialized_month.basis_month not in reference.basis_months
        or materialized_month.monthly_schema_version != reference.monthly_schema_version
        or materialized_month.monthly_algorithm_version != reference.monthly_algorithm_version
    ):
        raise SnapshotContractError("monthly materialization does not belong to the reference window")
    expected_months = set(reference.basis_months)
    normalized_reused_months = {
        _month_text(value, field="reused month") for value in reused_months
    }
    if (
        materialized_month.basis_month in normalized_reused_months
        or normalized_reused_months | {materialized_month.basis_month} != expected_months
    ):
        raise SnapshotContractError("composed monthly frequency window is incomplete")
    reused_fact_tuple = tuple(reused_facts)
    reused_fact_set = set(reused_fact_tuple)
    facts = reused_fact_tuple + materialized_month.facts
    keys: set[tuple[str, str, str]] = set()
    for fact in facts:
        key = (fact.basis_month, fact.product_code, fact.stock_code)
        if (
            fact.company_id != reference.company_id
            or fact.scope_fingerprint != reference.scope_fingerprint
            or fact.basis_month not in expected_months
            or (
                fact in reused_fact_set
                and fact.basis_month not in normalized_reused_months
            )
            or fact.product_code not in reference.product_codes
            or fact.stock_code not in reference.stock_codes
            or key in keys
        ):
            raise SnapshotContractError("composed monthly frequency facts are invalid")
        keys.add(key)
    return replace(
        reference,
        facts=tuple(sorted(facts, key=lambda item: (item.basis_month, item.product_code, item.stock_code))),
    )


def first_normal_inbound_month_sql(
    *,
    stock_codes: Iterable[Any],
    cutoff_date: Any,
) -> tuple[str, dict[str, Any]]:
    """Return the canonical scope-bound first normal inbound month query."""
    cutoff = str(cutoff_date or "").strip()
    if len(cutoff) != 8 or not cutoff.isdigit():
        raise SnapshotContractError("first normal inbound cutoff must be YYYYMMDD")
    stocks = tuple(sorted({str(value or "").strip() for value in stock_codes if str(value or "").strip()}))
    binds: dict[str, Any] = {"cutoff_date": cutoff}
    stock_clause = ""
    if stocks:
        names: list[str] = []
        for index, code in enumerate(stocks):
            key = f"inbound_stock_{index}"
            binds[key] = code
            names.append(f":{key}")
        stock_clause = (
            "AND I.Rd11_Stock_Cd_Gcode = '0018'\n"
            "  AND I.Rd11_Stock_Cd IN (" + ", ".join(names) + ")"
        )
    sql = f"""
SELECT LTRIM(RTRIM(I.Rd11_Physic_Cd)) AS product_code,
       LEFT(MIN(LTRIM(RTRIM(I.Rd11_In_YyMmDd))), 6) AS first_normal_inbound_month
FROM dbo.Rddbc110 AS I
WHERE I.Rd11_Io_Gu_Gcode = '0012'
  AND I.Rd11_Io_Gu IN ('001', '002')
  AND COALESCE(I.Rd11_Quantity, 0) + COALESCE(I.Rd11_Oquantity, 0) > 0
  AND NULLIF(LTRIM(RTRIM(I.Rd11_Physic_Cd)), '') IS NOT NULL
  AND LEN(LTRIM(RTRIM(I.Rd11_In_YyMmDd))) = 8
  AND LTRIM(RTRIM(I.Rd11_In_YyMmDd)) NOT LIKE '%[^0-9]%'
  AND ISDATE(LTRIM(RTRIM(I.Rd11_In_YyMmDd))) = 1
  AND I.Rd11_In_YyMmDd <= :cutoff_date
  {stock_clause}
GROUP BY LTRIM(RTRIM(I.Rd11_Physic_Cd))
ORDER BY product_code
""".strip()
    return sql, binds


def product_lifecycle_sql(
    *,
    stock_codes: Iterable[Any],
    cutoff_date: Any,
    basis_from: Any = None,
    basis_to: Any = None,
    stock_mode: str = "real",
    product_group_codes: Iterable[Any] = (),
    product_di_codes: Iterable[Any] = (),
    product_class_codes: Iterable[Any] = (),
    price_lookback_from: Any = None,
) -> tuple[str, dict[str, Any]]:
    """Read profile-scoped lifecycle and product-universe evidence once."""
    cutoff = str(cutoff_date or "").strip()
    if len(cutoff) != 8 or not cutoff.isdigit():
        raise SnapshotContractError("lifecycle cutoff must be YYYYMMDD")
    basis_start = str(basis_from or f"{cutoff[:6]}01").strip()
    basis_end = str(basis_to or cutoff).strip()
    price_start = str(price_lookback_from or basis_start).strip()
    if (
        len(basis_start) != 8 or not basis_start.isdigit()
        or len(basis_end) != 8 or not basis_end.isdigit()
        or len(price_start) != 8 or not price_start.isdigit()
    ):
        raise SnapshotContractError("lifecycle basis must be YYYYMMDD")
    mode = str(stock_mode or "real").strip()
    if mode not in {"real", "book"}:
        raise SnapshotContractError("lifecycle stock_mode must be real or book")
    stocks = tuple(sorted({str(value or "").strip() for value in stock_codes if str(value or "").strip()}))
    cutoff_day = datetime.strptime(cutoff, "%Y%m%d")
    cutoff_month = cutoff[:6]
    next_month = (cutoff_day.replace(day=28) + timedelta(days=4)).replace(day=1)
    is_partial_month = cutoff_day.date() < (next_month - timedelta(days=1)).date()
    previous_day = cutoff_day.replace(day=1) - timedelta(days=1)
    stock_month_to = previous_day.strftime("%Y%m") if is_partial_month else cutoff_month
    binds: dict[str, Any] = {
        "cutoff_date": cutoff,
        "basis_from": basis_start,
        "basis_to": basis_end,
        "price_lookback_from": price_start,
        "stock_month_to": stock_month_to,
        "current_month_from": f"{cutoff_month}01",
        "use_current_detail": 1 if is_partial_month else 0,
    }
    inbound_stock = ""
    outbound_stock = ""
    monthly_stock = ""
    if stocks:
        names: list[str] = []
        for index, code in enumerate(stocks):
            key = f"lifecycle_stock_{index}"
            binds[key] = code
            names.append(f":{key}")
        values = ", ".join(names)
        inbound_stock = "AND I.Rd11_Stock_Cd_Gcode = '0018' AND I.Rd11_Stock_Cd IN (" + values + ")"
        outbound_stock = "AND O.Rd12_Stock_Cd_Gcode = '0018' AND O.Rd12_Stock_Cd IN (" + values + ")"
        monthly_stock = "AND M.{prefix}_Stock_Cd IN (" + values + ")"

    def _dimension_clause(values: Iterable[Any], *, expected_gcode: str, gcode_column: str, tcode_column: str, prefix: str) -> str:
        checks: list[str] = []
        for index, raw in enumerate(values):
            text = str(raw or "").strip()
            gcode, separator, tcode = text.partition(":")
            if not separator or gcode != expected_gcode or not tcode:
                continue
            gkey, tkey = f"{prefix}_g_{index}", f"{prefix}_t_{index}"
            binds[gkey], binds[tkey] = gcode, tcode
            checks.append(f"({gcode_column} = :{gkey} AND {tcode_column} = :{tkey})")
        return "(" + " OR ".join(checks) + ")" if checks else ""

    product_filters = [
        clause for clause in (
            _dimension_clause(product_group_codes, expected_gcode="0013", gcode_column="P.Rd04_Physic_Group_Gcode", tcode_column="P.Rd04_Physic_Group", prefix="group"),
            _dimension_clause(product_di_codes, expected_gcode="0004", gcode_column="P.Rd04_Physic_Di_Gcode", tcode_column="P.Rd04_Physic_Di", prefix="di"),
            _dimension_clause(product_class_codes, expected_gcode="0031", gcode_column="P.Rd04_Physic_Tax_Gcode", tcode_column="P.Rd04_Physic_Tax", prefix="class"),
        ) if clause
    ]
    product_filter_sql = "\n      AND " + "\n      AND ".join(product_filters) if product_filters else ""
    monthly_prefix = "Rd21" if mode == "real" else "Rd22"
    monthly_table = "dbo.Rddbc210" if mode == "real" else "dbo.Rddbc220"
    monthly_in_qty = f"COALESCE(M.{monthly_prefix}_In_Quantity, 0)"
    monthly_out_qty = f"COALESCE(M.{monthly_prefix}_Out_Quantity, 0)"
    detail_in_date = "I.Rd11_In_YyMmDd" if mode == "real" else "I.Rd11_Trans_YyMmDd"
    detail_out_date = "O.Rd12_Out_YyMmDd" if mode == "real" else "O.Rd12_Trans_YyMmDd"
    detail_in_qty = "COALESCE(I.Rd11_Quantity, 0)"
    detail_out_qty = "COALESCE(O.Rd12_Quantity, 0)"
    if mode == "real":
        monthly_in_qty += f" + COALESCE(M.{monthly_prefix}_In_Oquantity, 0)"
        monthly_out_qty += f" + COALESCE(M.{monthly_prefix}_Out_Oquantity, 0)"
        detail_in_qty += " + COALESCE(I.Rd11_Oquantity, 0)"
        detail_out_qty += " + COALESCE(O.Rd12_Oquantity, 0)"
    monthly_stock_sql = monthly_stock.format(prefix=monthly_prefix) if monthly_stock else ""
    sql = f"""
WITH ProductUniverse AS (
    SELECT LTRIM(RTRIM(P.Rd04_Physic_Cd)) AS product_code,
           CONCAT(LTRIM(RTRIM(P.Rd04_Physic_Group_Gcode)), ':', LTRIM(RTRIM(P.Rd04_Physic_Group))) AS product_group_key,
           CONCAT(LTRIM(RTRIM(P.Rd04_Physic_Di_Gcode)), ':', LTRIM(RTRIM(P.Rd04_Physic_Di))) AS product_di_key,
           CONCAT(LTRIM(RTRIM(P.Rd04_Physic_Tax_Gcode)), ':', LTRIM(RTRIM(P.Rd04_Physic_Tax))) AS product_class_key,
           CASE WHEN LEN(LTRIM(RTRIM(P.Rd04_Add_Date))) = 8
                     AND LTRIM(RTRIM(P.Rd04_Add_Date)) NOT LIKE '%[^0-9]%'
                     AND ISDATE(LTRIM(RTRIM(P.Rd04_Add_Date))) = 1
                THEN LTRIM(RTRIM(P.Rd04_Add_Date)) END AS product_registered_date
    FROM dbo.Rddbc040 AS P
    WHERE NULLIF(LTRIM(RTRIM(P.Rd04_Physic_Cd)), '') IS NOT NULL
      {product_filter_sql}
), FirstInbound AS (
    SELECT LTRIM(RTRIM(I.Rd11_Physic_Cd)) AS product_code,
           MIN(LTRIM(RTRIM(I.Rd11_In_YyMmDd))) AS first_normal_inbound_date
    FROM dbo.Rddbc110 AS I
    WHERE I.Rd11_Io_Gu_Gcode = '0012' AND I.Rd11_Io_Gu IN ('001', '002')
      AND COALESCE(I.Rd11_Quantity, 0) + COALESCE(I.Rd11_Oquantity, 0) > 0
      AND NULLIF(LTRIM(RTRIM(I.Rd11_Physic_Cd)), '') IS NOT NULL
      AND LEN(LTRIM(RTRIM(I.Rd11_In_YyMmDd))) = 8
      AND LTRIM(RTRIM(I.Rd11_In_YyMmDd)) NOT LIKE '%[^0-9]%'
      AND ISDATE(LTRIM(RTRIM(I.Rd11_In_YyMmDd))) = 1
      AND I.Rd11_In_YyMmDd <= :cutoff_date
      {inbound_stock}
    GROUP BY LTRIM(RTRIM(I.Rd11_Physic_Cd))
), BasisInbound AS (
    SELECT DISTINCT LTRIM(RTRIM(I.Rd11_Physic_Cd)) AS product_code
    FROM dbo.Rddbc110 AS I
    WHERE I.Rd11_Io_Gu_Gcode = '0012' AND I.Rd11_Io_Gu IN ('001', '002')
      AND COALESCE(I.Rd11_Quantity, 0) + COALESCE(I.Rd11_Oquantity, 0) > 0
      AND I.Rd11_In_YyMmDd >= :basis_from AND I.Rd11_In_YyMmDd <= :basis_to
      {inbound_stock}
), PurchasePriceMonthly AS (
    SELECT LTRIM(RTRIM(I.Rd11_Physic_Cd)) AS product_code,
           LEFT(LTRIM(RTRIM(I.Rd11_In_YyMmDd)), 6) AS basis_month,
           SUM(CAST(I.Rd11_Quantity AS decimal(38, 6))) AS paid_quantity,
           SUM(CAST(COALESCE(I.Rd11_Fin_Supply_Price, I.Rd11_Supply_Price, 0) AS decimal(38, 6))) AS supply_amount
    FROM dbo.Rddbc110 AS I
    WHERE I.Rd11_Io_Gu_Gcode = '0012' AND I.Rd11_Io_Gu IN ('001', '002')
      AND COALESCE(I.Rd11_Quantity, 0) > 0
      AND COALESCE(I.Rd11_Fin_Supply_Price, I.Rd11_Supply_Price, 0) > 0
      AND I.Rd11_In_YyMmDd >= :price_lookback_from AND I.Rd11_In_YyMmDd <= :basis_to
      {inbound_stock}
    GROUP BY LTRIM(RTRIM(I.Rd11_Physic_Cd)), LEFT(LTRIM(RTRIM(I.Rd11_In_YyMmDd)), 6)
), LatestPurchasePrice AS (
    SELECT product_code, basis_month, paid_quantity, supply_amount,
           ROW_NUMBER() OVER (PARTITION BY product_code ORDER BY basis_month DESC) AS price_rank
    FROM PurchasePriceMonthly
    WHERE paid_quantity > 0
), FirstOutbound AS (
    SELECT LTRIM(RTRIM(O.Rd12_Physic_Cd)) AS product_code,
           MIN(LTRIM(RTRIM(O.Rd12_Out_YyMmDd))) AS first_outbound_date
    FROM dbo.Rddbc120 AS O
    WHERE O.Rd12_Io_Gu_Gcode = '0012'
      AND LEN(LTRIM(RTRIM(O.Rd12_Io_Gu))) = 3
      AND LTRIM(RTRIM(O.Rd12_Io_Gu)) NOT LIKE '%[^0-9]%'
      AND CONVERT(int, CASE
            WHEN LEN(LTRIM(RTRIM(O.Rd12_Io_Gu))) = 3
             AND LTRIM(RTRIM(O.Rd12_Io_Gu)) NOT LIKE '%[^0-9]%'
            THEN LTRIM(RTRIM(O.Rd12_Io_Gu)) ELSE NULL END) BETWEEN 500 AND 599
      AND COALESCE(O.Rd12_Quantity, 0) + COALESCE(O.Rd12_Oquantity, 0) > 0
      AND NULLIF(LTRIM(RTRIM(O.Rd12_Physic_Cd)), '') IS NOT NULL
      AND LEN(LTRIM(RTRIM(O.Rd12_Out_YyMmDd))) = 8
      AND LTRIM(RTRIM(O.Rd12_Out_YyMmDd)) NOT LIKE '%[^0-9]%'
      AND ISDATE(LTRIM(RTRIM(O.Rd12_Out_YyMmDd))) = 1
      AND O.Rd12_Out_YyMmDd <= :cutoff_date
      {outbound_stock}
    GROUP BY LTRIM(RTRIM(O.Rd12_Physic_Cd))
), MonthlyStock AS (
    SELECT LTRIM(RTRIM(M.{monthly_prefix}_Physic_Cd)) AS product_code,
           SUM(({monthly_in_qty}) - ({monthly_out_qty})) AS stock_quantity
    FROM {monthly_table} AS M
    WHERE M.{monthly_prefix}_Stock_YyMm <= :stock_month_to
      AND M.{monthly_prefix}_Io_Gu_Gcode = '0012'
      {monthly_stock_sql}
    GROUP BY LTRIM(RTRIM(M.{monthly_prefix}_Physic_Cd))
), CurrentInbound AS (
    SELECT LTRIM(RTRIM(I.Rd11_Physic_Cd)) AS product_code, SUM({detail_in_qty}) AS quantity
    FROM dbo.Rddbc110 AS I
    WHERE :use_current_detail = 1 AND I.Rd11_Io_Gu_Gcode = '0012'
      AND {detail_in_date} >= :current_month_from AND {detail_in_date} <= :cutoff_date
      {inbound_stock}
    GROUP BY LTRIM(RTRIM(I.Rd11_Physic_Cd))
), CurrentOutbound AS (
    SELECT LTRIM(RTRIM(O.Rd12_Physic_Cd)) AS product_code, SUM({detail_out_qty}) AS quantity
    FROM dbo.Rddbc120 AS O
    WHERE :use_current_detail = 1 AND O.Rd12_Io_Gu_Gcode = '0012'
      AND {detail_out_date} >= :current_month_from AND {detail_out_date} <= :cutoff_date
      {outbound_stock}
    GROUP BY LTRIM(RTRIM(O.Rd12_Physic_Cd))
)
SELECT P.product_code, P.product_group_key, P.product_di_key, P.product_class_key,
       P.product_registered_date,
       I.first_normal_inbound_date,
       LEFT(I.first_normal_inbound_date, 6) AS first_normal_inbound_month,
       O.first_outbound_date,
       CASE WHEN COALESCE(S.stock_quantity, 0) + COALESCE(CI.quantity, 0) - COALESCE(CO.quantity, 0) <> 0 THEN 1 ELSE 0 END AS current_stock_present,
       CASE WHEN BI.product_code IS NULL THEN 0 ELSE 1 END AS basis_inbound_present,
       COUNT_BIG(*) OVER () AS profile_product_count
       ,CAST(CASE WHEN PP.paid_quantity > 0 THEN PP.supply_amount / PP.paid_quantity END AS decimal(38, 10)) AS avg_purchase_unit_cost
       ,PP.basis_month AS purchase_price_basis_month
FROM ProductUniverse AS P
LEFT JOIN FirstInbound AS I ON I.product_code=P.product_code
LEFT JOIN FirstOutbound AS O ON O.product_code=P.product_code
LEFT JOIN BasisInbound AS BI ON BI.product_code=P.product_code
LEFT JOIN MonthlyStock AS S ON S.product_code=P.product_code
LEFT JOIN CurrentInbound AS CI ON CI.product_code=P.product_code
LEFT JOIN CurrentOutbound AS CO ON CO.product_code=P.product_code
LEFT JOIN LatestPurchasePrice AS PP ON PP.product_code=P.product_code AND PP.price_rank=1
ORDER BY P.product_code
""".strip()
    return sql, binds


def compose_independent_monthly_window(
    reference: MonthlyFrequencyAggregateWindow,
    materializations: Iterable[MonthlyFrequencyMaterialization],
) -> MonthlyFrequencyAggregateWindow:
    """Compose three independently approved months and retain Snapshot provenance."""
    months = tuple(sorted(materializations, key=lambda item: item.basis_month))
    if tuple(item.basis_month for item in months) != reference.basis_months:
        raise SnapshotContractError("independent monthly window is incomplete")
    for item in months:
        if (
            item.company_id != reference.company_id
            or item.stock_codes != reference.stock_codes
            or item.scope_fingerprint != reference.scope_fingerprint
            or item.product_codes != reference.product_codes
            or item.product_universe_fingerprint != reference.product_universe_fingerprint
            or item.monthly_schema_version != reference.monthly_schema_version
            or item.monthly_algorithm_version != reference.monthly_algorithm_version
        ):
            raise SnapshotContractError("independent monthly materialization contract mismatch")
    diagnostic_keys = set(reference.source_diagnostics)
    combined_diagnostics: dict[str, int] = {}
    for key in diagnostic_keys:
        if key == "diagnostic_contract_version":
            values = {int(item.source_diagnostics.get(key) or 0) for item in months}
            if len(values) != 1:
                raise SnapshotContractError("monthly diagnostic contract versions differ")
            combined_diagnostics[key] = values.pop()
        else:
            combined_diagnostics[key] = sum(int(item.source_diagnostics.get(key) or 0) for item in months)
    facts = tuple(
        sorted(
            (fact for item in months for fact in item.facts),
            key=lambda fact: (fact.basis_month, fact.product_code, fact.stock_code),
        )
    )
    return replace(reference, facts=facts, source_diagnostics=combined_diagnostics)


def project_new_product_frequency(
    snapshot: RelationalFrequencySnapshot,
    *,
    first_normal_inbound_months: Mapping[str, Any],
    lifecycle_scope_fingerprint: str,
) -> tuple[NewProductFrequencyProjection, ...]:
    """Overlay F for a verified scope without mutating legacy A-E/X grades."""
    validate_relational_frequency_snapshot(snapshot)
    if str(lifecycle_scope_fingerprint or "") != snapshot.key.scope_fingerprint:
        raise SnapshotContractError("new-product lifecycle scope fingerprint mismatch")
    evaluation_month = _month_text(snapshot.key.evaluation_month, field="evaluation_month")
    evaluation_index = int(evaluation_month[:4]) * 12 + int(evaluation_month[4:]) - 1
    rows: list[NewProductFrequencyProjection] = []
    for source in snapshot.frequency_products:
        product_code = str(source.get("product_code") or "").strip()
        legacy_grade = str(source.get("frequency_grade") or "")
        first_value = first_normal_inbound_months.get(product_code)
        first_month: str | None = None
        lifecycle_status = "insufficient"
        projected_grade = legacy_grade
        if first_value not in (None, ""):
            try:
                first_month = _month_text(first_value, field="first_normal_inbound_month")
            except SnapshotContractError:
                lifecycle_status = "invalid"
            else:
                first_index = int(first_month[:4]) * 12 + int(first_month[4:]) - 1
                age_months = evaluation_index - first_index
                if age_months < 0:
                    lifecycle_status = "invalid_future"
                else:
                    lifecycle_status = "verified"
                    if age_months <= 2:
                        projected_grade = "F"
        rows.append(NewProductFrequencyProjection(
            product_code=product_code,
            occurrence_count_3m=int(source.get("occurrence_count_3m") or 0),
            legacy_frequency_grade=legacy_grade,
            frequency_grade=projected_grade,
            first_normal_inbound_month=first_month,
            lifecycle_status=lifecycle_status,
        ))
    return tuple(sorted(rows, key=lambda item: item.product_code))


def rebuild_frequency_snapshot_from_monthly_window(
    window: MonthlyFrequencyAggregateWindow,
) -> RelationalFrequencySnapshot:
    """Rebuild the existing three-month authority from persisted monthly facts."""
    basis = completed_month_basis(window.evaluation_month)
    if (
        window.monthly_schema_version != MONTHLY_FREQUENCY_SCHEMA_VERSION
        or window.monthly_algorithm_version != MONTHLY_FREQUENCY_ALGORITHM_VERSION
    ):
        raise SnapshotContractError("monthly aggregate contract version mismatch")
    if window.basis_months != basis.months:
        raise SnapshotContractError("monthly aggregate basis months do not match evaluation month")
    if window.source_schema_version != SCHEMA_VERSION or window.source_algorithm_version != ALGORITHM_VERSION:
        raise SnapshotContractError("monthly aggregate source contract version mismatch")
    if window.scope_fingerprint != scope_fingerprint(window.stock_codes):
        raise SnapshotContractError("monthly aggregate scope fingerprint mismatch")
    if window.product_universe_fingerprint != _product_universe_fingerprint(window.product_codes):
        raise SnapshotContractError("monthly aggregate product universe fingerprint mismatch")
    if any(
        fact.company_id != window.company_id
        or fact.scope_fingerprint != window.scope_fingerprint
        or fact.basis_month not in window.basis_months
        for fact in window.facts
    ):
        raise SnapshotContractError("monthly aggregate fact ownership mismatch")

    rebuilt = build_relational_frequency_snapshot_from_aggregates(
        company_id=window.company_id,
        evaluation_month=window.evaluation_month,
        monthly_rows=(fact.as_snapshot_row() for fact in window.facts),
        product_codes=window.product_codes,
        stock_codes=window.stock_codes,
        source_watermark=window.source_watermark,
        source_watermark_status=window.source_watermark_status,
        source_diagnostics=window.source_diagnostics,
    )
    if rebuilt.source_contract != window.source_contract:
        raise SnapshotContractError("monthly aggregate source contract mismatch")
    if rebuilt.source_fingerprint != window.source_fingerprint:
        raise SnapshotContractError("monthly aggregate source fingerprint mismatch")
    if rebuilt.checksum != window.expected_snapshot_checksum:
        raise SnapshotContractError("monthly aggregate snapshot checksum mismatch")
    return rebuilt
