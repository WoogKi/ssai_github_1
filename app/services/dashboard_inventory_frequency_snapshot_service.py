from __future__ import annotations

import calendar
import hashlib
import os
import re
import tempfile
import time
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import pandas as pd
from sqlalchemy import text
from app.db.sql_utils import sql_safe_int

from app.db.mssql_client import get_current_company_id, get_engine, set_current_company_id
from app.services.business_calendar_service import kst_today
from app.services.dashboard_inventory_frequency_snapshot import (
    ALGORITHM_VERSION,
    CUMULATIVE_PRODUCT_STATISTICS_ALGORITHM_VERSION,
    EXTENDED_ALGORITHM_VERSION,
    EXTENDED_RELATIONAL_FREQUENCY_REPRESENTATION,
    EXTENDED_SCHEMA_VERSION,
    LEGACY_PRODUCT_STATISTICS_ALGORITHM_VERSION,
    PRODUCT_STATISTICS_ALGORITHM_VERSION,
    SAVED_IO_FULL_SHADOW_ALGORITHM_VERSION,
    SAVED_IO_FUTURE_PROFIT_ALGORITHM_VERSION,
    SAVED_IO_AVG_PURCHASE_ALGORITHM_VERSION,
    PRODUCT_STATISTICS_SCHEMA_VERSION,
    FrequencyProjectionReadResult,
    RelationalFrequencySnapshot,
    SCHEMA_VERSION,
    SNAPSHOT_TYPE,
    SnapshotContractError,
    build_relational_frequency_snapshot_from_aggregates,
    build_extended_relational_frequency_snapshot_from_aggregates,
    build_product_statistics_relational_snapshot_from_aggregates,
    completed_month_basis,
    dashboard_profile_fingerprint,
    scope_fingerprint,
)
from app.services.monthly_frequency_aggregate import product_lifecycle_sql
from app.services.saved_io_scope import FULL_POLICY_VERSION, SavedIoScope, bind_io_codes, resolve_saved_io_scope
from app.services.product_classification_contract import ClassificationStatus
from app.services.ssai_product_classification_repository import (
    ProductClassificationTargets,
    classify_product_from_loaded_authority,
    load_effective_company_classification_authority,
)
from app.services.ssai_snapshot_repository import (
    SNAPSHOT_STATUS_CORRUPT,
    SNAPSHOT_STATUS_MISSING,
    SNAPSHOT_STATUS_STALE,
    SnapshotKey,
    SnapshotReadResult,
)
from app.services.sql_server_snapshot_repository import SqlServerSnapshotRepository
from app.services.ssai_analytics_target_resolver import connect_company_analytics_db


QueryExecutor = Callable[[int, str, Mapping[str, Any], int], pd.DataFrame]
ProgressReporter = Callable[[str], None]
log = logging.getLogger("ssai.sims.dashboard_snapshot")
ROW_PARTITION_FIELDS = (
    "normal_positive_accepted_row_count",
    "normal_positive_duplicate_row_count",
    "normal_positive_conflicting_row_count",
    "normal_positive_missing_key_row_count",
    "normal_positive_nonintegral_row_count",
    "normal_nonpositive_row_count",
    "return_positive_row_count",
    "return_nonpositive_row_count",
    "other_tcode_row_count",
)
DIAGNOSTIC_FIELDS = (
    "source_row_count",
    *ROW_PARTITION_FIELDS,
    "normal_positive_row_count",
    "distinct_normal_event_count",
    "conflicting_event_count",
)


@dataclass(frozen=True)
class FrequencySnapshotPlan:
    company_id: int
    evaluation_month: str
    basis_from: str
    basis_to: str
    basis_months: tuple[str, str, str]
    stock_codes: tuple[str, ...]
    product_group_codes: tuple[str, ...] = ()
    product_di_codes: tuple[str, ...] = ()
    product_class_codes: tuple[str, ...] = ()
    io_gu_codes: tuple[str, ...] = ()
    stock_mode: str = "real"
    profile_fingerprint: str = ""
    erp_sql_call_count: int = 2
    analytics_write_plan: str = "v2 draft manifest + immutable relational rows; approval/publish 0"


@dataclass(frozen=True)
class FrequencyMonthSourcePlan:
    company_id: int
    basis_month: str
    basis_from: str
    basis_to: str
    stock_codes: tuple[str, ...]
    erp_sql_call_count: int = 1


class SnapshotGenerationInProgressError(SnapshotContractError):
    """Fail closed when an identical snapshot generation is already running."""


@dataclass(frozen=True)
class DashboardProfileStockScope:
    """Exact stored Dashboard stock scope accepted for a snapshot operation."""

    company_id: int
    stock_codes: tuple[str, ...]
    profile_status: str
    scope_source: str
    product_group_codes: tuple[str, ...] = ()
    product_di_codes: tuple[str, ...] = ()
    product_class_codes: tuple[str, ...] = ()
    io_gu_codes: tuple[str, ...] = ()
    stock_mode: str = "real"


def normalize_stock_scope(stock_codes: Sequence[Any] | None) -> tuple[str, ...]:
    return tuple(sorted({str(code).strip() for code in stock_codes or () if str(code).strip()}))


def resolve_dashboard_profile_stock_scope(
    *,
    company_id: Any,
    manual_stock_codes: Sequence[Any] | None = None,
    profile_loader: Callable[[int], Any] | None = None,
) -> DashboardProfileStockScope:
    """Resolve the same saved Dashboard stock scope for CLI snapshot operations.

    ``None`` means use the stored scope.  An explicit sequence is an operator
    override and is accepted only when it exactly equals that stored scope.
    """
    try:
        normalized_company = int(company_id)
    except (TypeError, ValueError) as exc:
        raise SnapshotContractError("dashboard_profile_company_invalid") from exc
    if normalized_company <= 0:
        raise SnapshotContractError("dashboard_profile_company_invalid")

    if profile_loader is None:
        from app.services.ssai_analysis_profile_service import load_dashboard_profile_checked

        profile_loader = lambda value: load_dashboard_profile_checked(company_id=value)
    result = profile_loader(normalized_company)
    status = str(getattr(result, "status", "unavailable") or "unavailable")
    profile = getattr(result, "profile", None)
    reason_code = str(getattr(result, "reason_code", "") or "")
    result_company_id = getattr(result, "company_id", normalized_company)
    if result_company_id is not None:
        try:
            if int(result_company_id) != normalized_company:
                raise SnapshotContractError("dashboard_profile_company_mismatch")
        except (TypeError, ValueError) as exc:
            raise SnapshotContractError("dashboard_profile_company_mismatch") from exc
    if status != "ready" or not isinstance(profile, Mapping):
        raise SnapshotContractError(f"dashboard_profile_{status}:{reason_code or 'profile_unavailable'}")

    from app.services.ssai_analysis_profile_service import normalize_company_default_conditions

    normalized_profile = normalize_company_default_conditions(profile)
    stored_scope = normalize_stock_scope(normalized_profile.get("stock_cd_list"))
    if not stored_scope:
        raise SnapshotContractError("dashboard_profile_stock_scope_empty")
    if manual_stock_codes is not None:
        manual_scope = normalize_stock_scope(manual_stock_codes)
        if manual_scope != stored_scope:
            raise SnapshotContractError("dashboard_profile_stock_scope_mismatch")
        source = "manual_verified"
    else:
        source = "dashboard_profile"
    return DashboardProfileStockScope(
        company_id=normalized_company,
        stock_codes=stored_scope,
        profile_status=status,
        scope_source=source,
        product_group_codes=tuple(normalized_profile.get("product_group_list") or ()),
        product_di_codes=tuple(normalized_profile.get("product_di_list") or ()),
        product_class_codes=tuple(normalized_profile.get("product_class_list") or ()),
        io_gu_codes=tuple(normalized_profile.get("io_gu_list") or ()),
        stock_mode=str(normalized_profile.get("stock_mode") or "real").strip(),
    )


def build_frequency_snapshot_plan(
    *,
    company_id: Any,
    evaluation_month: Any,
    stock_codes: Sequence[Any] | None,
    product_group_codes: Sequence[Any] | None = None,
    product_di_codes: Sequence[Any] | None = None,
    product_class_codes: Sequence[Any] | None = None,
    io_gu_codes: Sequence[Any] | None = None,
    stock_mode: Any = "real",
) -> FrequencySnapshotPlan:
    try:
        normalized_company = int(company_id)
    except (TypeError, ValueError) as exc:
        raise SnapshotContractError("company_id must be an existing numeric company id") from exc
    if normalized_company <= 0:
        raise SnapshotContractError("company_id must be positive")
    normalized_stock_mode = str(stock_mode or "real").strip()
    if normalized_stock_mode not in {"real", "book"}:
        raise SnapshotContractError("stock_mode must be real or book")
    basis = completed_month_basis(evaluation_month)
    normalized_stock_codes = normalize_stock_scope(stock_codes)
    normalized_product_groups = normalize_stock_scope(product_group_codes)
    normalized_product_di = normalize_stock_scope(product_di_codes)
    normalized_product_class = normalize_stock_scope(product_class_codes)
    return FrequencySnapshotPlan(
        company_id=normalized_company,
        evaluation_month=basis.evaluation_month,
        basis_from=basis.basis_from,
        basis_to=basis.basis_to,
        basis_months=basis.months,
        stock_codes=normalized_stock_codes,
        product_group_codes=normalized_product_groups,
        product_di_codes=normalized_product_di,
        product_class_codes=normalized_product_class,
        io_gu_codes=normalize_stock_scope(io_gu_codes),
        stock_mode=normalized_stock_mode,
        profile_fingerprint=dashboard_profile_fingerprint(
            stock_codes=normalized_stock_codes,
            product_group_codes=normalized_product_groups,
            product_di_codes=normalized_product_di,
            product_class_codes=normalized_product_class,
            stock_mode=normalized_stock_mode,
        ),
    )


def build_frequency_month_source_plan(
    *,
    company_id: Any,
    basis_month: Any,
    stock_codes: Sequence[Any] | None,
) -> FrequencyMonthSourcePlan:
    """Build one completed-month source boundary without changing Snapshot identity."""
    try:
        normalized_company = int(company_id)
    except (TypeError, ValueError) as exc:
        raise SnapshotContractError("company_id must be an existing numeric company id") from exc
    month = str(basis_month or "").strip()
    if normalized_company <= 0 or len(month) != 6 or not month.isdigit():
        raise SnapshotContractError("monthly frequency source plan is invalid")
    year = int(month[:4])
    month_number = int(month[4:])
    if year < 1900 or not 1 <= month_number <= 12:
        raise SnapshotContractError("monthly frequency source plan is invalid")
    month_end = calendar.monthrange(year, month_number)[1]
    return FrequencyMonthSourcePlan(
        company_id=normalized_company,
        basis_month=month,
        basis_from=f"{month}01",
        basis_to=f"{month}{month_end:02d}",
        stock_codes=normalize_stock_scope(stock_codes),
    )


def _month_start_months_before(yyyymmdd: str, months_before: int) -> str:
    value = str(yyyymmdd or "").strip()
    if len(value) != 8 or not value.isdigit() or months_before < 0:
        raise SnapshotContractError("price lookback boundary is invalid")
    month_index = int(value[:4]) * 12 + int(value[4:6]) - 1 - months_before
    return f"{month_index // 12:04d}{month_index % 12 + 1:02d}01"


def _price_status(evaluation_month: str, basis_month: Any) -> str:
    text_value = str(basis_month or "").strip()
    if len(text_value) != 6 or not text_value.isdigit():
        return "unavailable"
    evaluation_index = int(evaluation_month[:4]) * 12 + int(evaluation_month[4:]) - 1
    basis_index = int(text_value[:4]) * 12 + int(text_value[4:]) - 1
    age = evaluation_index - basis_index
    if age < 1 or age > 12:
        return "unavailable"
    return "stale" if age > 6 else "ready"


def frequency_snapshot_key(
    plan: FrequencySnapshotPlan, *, extended: bool = False, product_statistics: bool = False
) -> SnapshotKey:
    """Return the immutable repository key for one Dashboard frequency scope."""
    return SnapshotKey(
        company_id=str(plan.company_id),
        snapshot_type=SNAPSHOT_TYPE,
        evaluation_month=plan.evaluation_month,
        scope_fingerprint=scope_fingerprint(plan.stock_codes),
        schema_version=(PRODUCT_STATISTICS_SCHEMA_VERSION if product_statistics else EXTENDED_SCHEMA_VERSION if extended else SCHEMA_VERSION),
        algorithm_version=(PRODUCT_STATISTICS_ALGORITHM_VERSION if product_statistics else EXTENDED_ALGORITHM_VERSION if extended else ALGORITHM_VERSION),
        profile_fingerprint=plan.profile_fingerprint if extended or product_statistics else "",
    )


def frequency_snapshot_read_keys(plan: FrequencySnapshotPlan) -> tuple[SnapshotKey, ...]:
    """Prefer an approved saved-I/O generation, retaining legacy fallbacks."""
    legacy = (
        frequency_snapshot_key(plan, product_statistics=True),
        frequency_snapshot_key(plan, extended=True),
        frequency_snapshot_key(plan),
    )
    if plan.io_gu_codes:
        scope = resolve_saved_io_scope(plan.io_gu_codes, confirmed_trade_roles=True)
        if not scope.unclassified_codes:
            old_saved_io_key = replace(
                selected_io_snapshot_key(plan, scope),
                algorithm_version=SAVED_IO_FULL_SHADOW_ALGORITHM_VERSION,
            )
            future_profit_key = replace(
                selected_io_snapshot_key(plan, scope),
                algorithm_version=SAVED_IO_FUTURE_PROFIT_ALGORITHM_VERSION,
            )
            return (selected_io_snapshot_key(plan, scope), future_profit_key, old_saved_io_key, *legacy)
    return legacy


def selected_io_snapshot_key(plan: FrequencySnapshotPlan, io_scope: SavedIoScope) -> SnapshotKey:
    """Identify the saved-I/O calculation separately from approved v2.1 keys."""
    _require_plan_io_scope(plan, io_scope)
    legacy = frequency_snapshot_key(plan, product_statistics=True)
    return replace(
        legacy,
        algorithm_version=SAVED_IO_AVG_PURCHASE_ALGORITHM_VERSION,
        profile_fingerprint=io_scope.fingerprint(plan.profile_fingerprint),
    )


def validate_saved_io_snapshot_profile_key(key: SnapshotKey) -> None:
    """Reject direct Repository writes using a key outside the current company profile."""
    stored = resolve_dashboard_profile_stock_scope(company_id=key.company_id)
    plan = build_frequency_snapshot_plan(
        company_id=stored.company_id, evaluation_month=key.evaluation_month,
        stock_codes=stored.stock_codes, product_group_codes=stored.product_group_codes,
        product_di_codes=stored.product_di_codes, product_class_codes=stored.product_class_codes,
        io_gu_codes=stored.io_gu_codes, stock_mode=stored.stock_mode,
    )
    io_scope = resolve_saved_io_scope(plan.io_gu_codes, confirmed_trade_roles=True)
    if io_scope.unclassified_codes or key != selected_io_snapshot_key(plan, io_scope):
        raise SnapshotContractError("Saved I/O key differs from the current company profile")


def _plan_with_current_saved_io(plan: FrequencySnapshotPlan) -> FrequencySnapshotPlan:
    """Use each company's saved I/O only when the remaining profile scope matches."""
    try:
        stored = resolve_dashboard_profile_stock_scope(company_id=plan.company_id)
    except Exception:
        return plan
    if (
        stored.stock_codes != plan.stock_codes
        or tuple(sorted(stored.product_group_codes)) != plan.product_group_codes
        or tuple(sorted(stored.product_di_codes)) != plan.product_di_codes
        or tuple(sorted(stored.product_class_codes)) != plan.product_class_codes
        or stored.stock_mode != plan.stock_mode
    ):
        return plan
    return replace(plan, io_gu_codes=stored.io_gu_codes)


def _require_plan_io_scope(plan: FrequencySnapshotPlan, io_scope: SavedIoScope) -> None:
    if io_scope.all_codes != resolve_saved_io_scope(plan.io_gu_codes).all_codes:
        raise SnapshotContractError("selected I/O scope differs from the saved plan")


@contextmanager
def frequency_snapshot_generation_guard(
    plan: FrequencySnapshotPlan,
    *,
    lock_root: Path | None = None,
    key: SnapshotKey | None = None,
) -> Iterable[SnapshotKey]:
    """Serialize one company/month/scope generation across local CLI processes.

    A stale lock intentionally remains fail-closed for an operator to inspect;
    this prevents a crashed run from silently overlapping a later retry.
    """
    key = key or frequency_snapshot_key(plan, product_statistics=True)
    digest_input = "|".join(
        (
            key.company_id,
            key.snapshot_type,
            key.evaluation_month,
            key.scope_fingerprint,
            key.schema_version,
            key.algorithm_version,
            key.profile_fingerprint,
        )
    )
    lock_name = f"frequency_snapshot_{hashlib.sha256(digest_input.encode('utf-8')).hexdigest()}.lock"
    root = lock_root or Path(tempfile.gettempdir()) / "ssai_snapshot_generation_locks"
    root.mkdir(parents=True, exist_ok=True)
    path = root / lock_name
    try:
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise SnapshotGenerationInProgressError(
            "snapshot generation is already in progress for the same company, evaluation month, and scope"
        ) from exc
    try:
        os.write(descriptor, b"ssai snapshot generation lock\n")
        yield key
    finally:
        os.close(descriptor)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


_FREQUENCY_READ_CACHE_TTL_SECONDS = 300.0
_frequency_read_cache: dict[tuple[SnapshotKey, str], tuple[float, SnapshotReadResult]] = {}


def _company_snapshot_repository(company_id: int) -> SqlServerSnapshotRepository:
    """Bind the common repository to one explicit same-server analytics target."""
    return SqlServerSnapshotRepository(
        reader_connection_factory=lambda: connect_company_analytics_db(company_id, "reader"),
        writer_connection_factory=lambda: connect_company_analytics_db(company_id, "writer"),
    )


def _snapshot_exception_code(exc: Exception) -> str:
    """Return only an exception class and optional SQLSTATE, never its text."""
    sqlstate = ""
    args = getattr(exc, "args", ()) or ()
    if args and isinstance(args[0], str):
        candidate = args[0].strip().upper()
        if len(candidate) == 5 and candidate.isalnum():
            sqlstate = candidate
    return f"{type(exc).__name__}{':' + sqlstate if sqlstate else ''}"


def _operating_as_of_date(value: Any) -> str:
    """Normalize the Dashboard policy date used to exclude incomplete bases."""
    text = str(value or "").strip()
    if not text:
        return date.today().strftime("%Y%m%d")
    if len(text) != 8 or not text.isdecimal():
        raise SnapshotContractError("snapshot operating as_of_date must be YYYYMMDD")
    return text


def _resolve_operating_key(repo: Any, requested_key: SnapshotKey, as_of_date: str) -> SnapshotKey | None:
    resolver = getattr(repo, "resolve_latest_eligible_key", None)
    if not callable(resolver):
        # Test doubles that predate the operating-read contract retain their
        # exact-key behavior; the production repository always implements it.
        return requested_key
    resolved = resolver(requested_key, available_through=as_of_date)
    if resolved is not None or requested_key.algorithm_version != PRODUCT_STATISTICS_ALGORITHM_VERSION:
        return resolved
    for version in (
        CUMULATIVE_PRODUCT_STATISTICS_ALGORITHM_VERSION,
        LEGACY_PRODUCT_STATISTICS_ALGORITHM_VERSION,
    ):
        resolved = resolver(replace(requested_key, algorithm_version=version), available_through=as_of_date)
        if resolved is not None:
            return resolved
    return None


def _classify_projection_unavailable(repo: Any, key: SnapshotKey) -> tuple[str, str]:
    classifier = getattr(repo, "classify_frequency_resolution", None)
    if not callable(classifier):
        return "missing", "no_approved_snapshot"
    try:
        reason = str(classifier(key) or "no_approved_snapshot")
    except Exception:
        return "missing", "authority_unavailable"
    authority = "version_mismatch" if reason == "version_mismatch" else "missing"
    return authority, reason


def read_approved_frequency_snapshot(
    *,
    company_id: Any,
    evaluation_month: Any,
    stock_codes: Sequence[Any] | None,
    product_group_codes: Sequence[Any] | None = None,
    product_di_codes: Sequence[Any] | None = None,
    product_class_codes: Sequence[Any] | None = None,
    stock_mode: Any = "real",
    as_of_date: Any = None,
    repository: Any | None = None,
) -> SnapshotReadResult:
    """Read only the approved snapshot, with a short process-local cache.

    This boundary is deliberately separate from ERP reads.  A missing or
    unavailable shared snapshot stays fail-closed so Dashboard facts can show
    frequency data as insufficient without attempting a live Rddbc120 rebuild.
    """
    try:
        plan = build_frequency_snapshot_plan(
            company_id=company_id,
            evaluation_month=evaluation_month,
            stock_codes=stock_codes,
            product_group_codes=product_group_codes,
            product_di_codes=product_di_codes,
            product_class_codes=product_class_codes,
            stock_mode=stock_mode,
        )
    except SnapshotContractError as exc:
        return SnapshotReadResult(status=SNAPSHOT_STATUS_MISSING, reason=str(exc))
    if repository is None:
        plan = _plan_with_current_saved_io(plan)
    keys = frequency_snapshot_read_keys(plan)
    try:
        as_of = _operating_as_of_date(as_of_date)
    except SnapshotContractError as exc:
        return SnapshotReadResult(status=SNAPSHOT_STATUS_MISSING, reason=str(exc))
    try:
        repo = repository or _company_snapshot_repository(int(plan.company_id))
        operating_key = next(
            (resolved for candidate in keys if (resolved := _resolve_operating_key(repo, candidate, as_of)) is not None),
            None,
        )
    except Exception as exc:
        reason_code = _snapshot_exception_code(exc)
        return SnapshotReadResult(status=SNAPSHOT_STATUS_STALE, reason=f"snapshot operating lookup unavailable: {reason_code}")
    if operating_key is None:
        _authority_status, resolution_status = _classify_projection_unavailable(repo, keys[0])
        return SnapshotReadResult(status=SNAPSHOT_STATUS_MISSING, reason=resolution_status)
    key = operating_key
    cache_key = (key, as_of)
    now = time.monotonic()
    cached = _frequency_read_cache.get(cache_key)
    cache_age_ms = int((now - cached[0]) * 1000) if cached else 0
    log.info(
        "[dashboard.snapshot.reader] stage=key_cache_lookup company_id=%s evaluation_month=%s stock_code_count=%s scope_fingerprint=%s schema_version=%s algorithm_version=%s cache_hit=%s cache_age_ms=%s cached_status=%s",
        operating_key.company_id,
        operating_key.evaluation_month,
        len(plan.stock_codes),
        key.scope_fingerprint[:12],
        key.schema_version,
        key.algorithm_version,
        bool(repository is None and cached and cached[1].status == "ready" and now - cached[0] < _FREQUENCY_READ_CACHE_TTL_SECONDS),
        cache_age_ms,
        str(cached[1].status) if cached else "",
    )
    if repository is None and cached and cached[1].status == "ready" and now - cached[0] < _FREQUENCY_READ_CACHE_TTL_SECONDS:
        return cached[1]
    started = time.perf_counter()
    try:
        result = repo.read(operating_key)
    except Exception as exc:
        reason_code = _snapshot_exception_code(exc)
        result = SnapshotReadResult(status=SNAPSHOT_STATUS_STALE, reason=f"snapshot read unavailable: {reason_code}")
        log.warning(
            "[dashboard.snapshot.reader] stage=repository_error company_id=%s evaluation_month=%s stock_code_count=%s scope_fingerprint=%s status=%s reason_code=%s elapsed_ms=%s",
            key.company_id,
            key.evaluation_month,
            len(plan.stock_codes),
            key.scope_fingerprint[:12],
            SNAPSHOT_STATUS_STALE,
            reason_code,
            int((time.perf_counter() - started) * 1000),
        )
    if repository is None and result.status == "ready":
        _frequency_read_cache[cache_key] = (now, result)
    elif repository is None:
        _frequency_read_cache.pop(cache_key, None)
    log.info(
        "[dashboard.snapshot.reader] stage=reader_total company_id=%s evaluation_month=%s stock_code_count=%s scope_fingerprint=%s status=%s generation_no=%s payload_bytes=%s reason_code=%s elapsed_ms=%s",
        operating_key.company_id,
        operating_key.evaluation_month,
        len(plan.stock_codes),
        key.scope_fingerprint[:12],
        result.status,
        result.generation_no if result.generation_no is not None else "",
        0,
        "none" if result.status == "ready" else str(result.status),
        int((time.perf_counter() - started) * 1000),
    )
    return result


def read_approved_frequency_projection(
    *,
    company_id: Any,
    evaluation_month: Any,
    stock_codes: Sequence[Any] | None,
    product_group_codes: Sequence[Any] | None = None,
    product_di_codes: Sequence[Any] | None = None,
    product_class_codes: Sequence[Any] | None = None,
    stock_mode: Any = "real",
    product_codes: Sequence[Any] | None = None,
    frequency_grade: str = "",
    as_of_date: Any = None,
    repository: Any | None = None,
) -> FrequencyProjectionReadResult:
    """Read only the approved derived product-frequency rows for one exact key.

    ``legacy`` is a representation-availability result, not an authorization
    result. Callers may use the established full-payload path only for that
    case; a malformed projection remains corrupt and fail-closed.
    """
    try:
        plan = build_frequency_snapshot_plan(
            company_id=company_id,
            evaluation_month=evaluation_month,
            stock_codes=stock_codes,
            product_group_codes=product_group_codes,
            product_di_codes=product_di_codes,
            product_class_codes=product_class_codes,
            stock_mode=stock_mode,
        )
        if repository is None:
            plan = _plan_with_current_saved_io(plan)
        keys = frequency_snapshot_read_keys(plan)
        as_of = _operating_as_of_date(as_of_date)
    except SnapshotContractError as exc:
        return FrequencyProjectionReadResult(status=SNAPSHOT_STATUS_MISSING, reason=str(exc))
    repo = repository or _company_snapshot_repository(int(plan.company_id))
    try:
        operating_key = next(
            (resolved for candidate in keys if (resolved := _resolve_operating_key(repo, candidate, as_of)) is not None),
            None,
        )
    except Exception as exc:
        return FrequencyProjectionReadResult(status=SNAPSHOT_STATUS_CORRUPT, reason=_snapshot_exception_code(exc))
    if operating_key is None:
        authority_status, resolution_status = _classify_projection_unavailable(repo, keys[0])
        return FrequencyProjectionReadResult(
            status=SNAPSHOT_STATUS_MISSING,
            reason=resolution_status,
            authority_status=authority_status,
            resolution_status=resolution_status,
            contract_version=EXTENDED_SCHEMA_VERSION,
        )
    reader = getattr(repo, "read_frequency_projection", None)
    if not callable(reader):
        return FrequencyProjectionReadResult(status="legacy", reason="projection reader is unavailable")
    try:
        result = reader(
            operating_key,
            product_codes=tuple(str(code or "").strip() for code in product_codes or () if str(code or "").strip()),
            frequency_grade=str(frequency_grade or "").strip(),
        )
        if result.status == "ready":
            return FrequencyProjectionReadResult(
                status=result.status, rows=result.rows, reason=result.reason,
                manifest_id=result.manifest_id, generation_no=result.generation_no,
                checksum=result.checksum, authority_status="ready",
                resolution_status="exact_match",
                contract_version=operating_key.schema_version,
                resolved_evaluation_month=operating_key.evaluation_month,
            )
        return result
    except Exception as exc:
        return FrequencyProjectionReadResult(status=SNAPSHOT_STATUS_CORRUPT, reason=_snapshot_exception_code(exc))


def clear_frequency_snapshot_read_cache() -> None:
    """Clear process-local reads after an operator publishes a new snapshot."""
    _frequency_read_cache.clear()


def product_universe_sql() -> tuple[str, dict[str, Any]]:
    """Frequency baseline: all Rddbc040 products; Dashboard display filters apply after grading."""
    return (
        """
SELECT DISTINCT LTRIM(RTRIM(P.Rd04_Physic_Cd)) AS product_code
FROM dbo.Rddbc040 AS P
WHERE NULLIF(LTRIM(RTRIM(P.Rd04_Physic_Cd)), '') IS NOT NULL
ORDER BY product_code
""".strip(),
        {},
    )


def _product_dimension_scope_sql(
    plan: FrequencySnapshotPlan,
    binds: dict[str, Any],
    *,
    product_alias: str,
    source_alias: str = "O",
    source_product_column: str = "Rd12_Physic_Cd",
) -> tuple[str, str]:
    """Bind the stored Dashboard Gcode:Tcode product scope without coercion."""
    clauses: list[str] = []
    specs = (
        (plan.product_group_codes, "0013", "Rd04_Physic_Group_Gcode", "Rd04_Physic_Group", "group"),
        (plan.product_di_codes, "0004", "Rd04_Physic_Di_Gcode", "Rd04_Physic_Di", "di"),
        (plan.product_class_codes, "0031", "Rd04_Physic_Tax_Gcode", "Rd04_Physic_Tax", "class"),
    )
    for values, expected_gcode, gcode_field, tcode_field, prefix in specs:
        checks: list[str] = []
        for index, raw in enumerate(values):
            gcode, separator, tcode = str(raw or "").strip().partition(":")
            if not separator or gcode != expected_gcode or not tcode:
                continue
            gkey, tkey = f"outbound_{prefix}_g_{index}", f"outbound_{prefix}_t_{index}"
            binds[gkey], binds[tkey] = gcode, tcode
            checks.append(
                f"({product_alias}.{gcode_field} = :{gkey} AND {product_alias}.{tcode_field} = :{tkey})"
            )
        if checks:
            clauses.append("(" + " OR ".join(checks) + ")")
    if not clauses:
        return "", ""
    return (
        f"INNER JOIN dbo.Rddbc040 AS {product_alias} ON {product_alias}.Rd04_Physic_Cd = {source_alias}.{source_product_column}",
        "AND " + "\n  AND ".join(clauses),
    )


def _aggregate_sql(base_rows_sql: str) -> str:
    """Aggregate outbound events once at exact-row, event, day, then month grain."""
    io_tcode_number = sql_safe_int("io_tcode")
    return f"""
WITH BaseRows AS (
    {base_rows_sql}
), Classified AS (
    SELECT *,
        CASE WHEN io_gcode = '0012' AND LEN(io_tcode) = 3
                  AND io_tcode NOT LIKE '%[^0-9]%'
                  AND {io_tcode_number} BETWEEN 500 AND 599 THEN 1 ELSE 0 END AS is_normal,
        CASE WHEN io_gcode = '0012' AND LEN(io_tcode) = 3
                  AND io_tcode NOT LIKE '%[^0-9]%'
                  AND {io_tcode_number} BETWEEN 600 AND 699 THEN 1 ELSE 0 END AS is_return
    FROM BaseRows
), NormalPositive AS (
    SELECT * FROM Classified WHERE is_normal = 1 AND outbound_quantity > 0
), ExactRows AS (
    SELECT outbound_date, vendor_code, outbound_seq, product_code, stock_code, outbound_quantity,
           COUNT_BIG(*) AS exact_duplicate_count
    FROM NormalPositive
    WHERE NULLIF(outbound_date, '') IS NOT NULL AND NULLIF(vendor_code, '') IS NOT NULL
      AND NULLIF(outbound_seq, '') IS NOT NULL AND NULLIF(product_code, '') IS NOT NULL
      AND NULLIF(stock_code, '') IS NOT NULL
      AND outbound_quantity = FLOOR(outbound_quantity)
    GROUP BY outbound_date, vendor_code, outbound_seq, product_code, stock_code, outbound_quantity
), EventGrain AS (
    SELECT outbound_date, vendor_code, outbound_seq,
           COUNT_BIG(*) AS mapping_count,
           MAX(product_code) AS product_code,
           MAX(stock_code) AS stock_code,
           MAX(outbound_quantity) AS outbound_quantity,
           SUM(exact_duplicate_count - 1) AS exact_duplicate_row_count
    FROM ExactRows
    GROUP BY outbound_date, vendor_code, outbound_seq
), EventAnnotated AS (
    SELECT *,
           COALESCE(SUM(exact_duplicate_row_count) OVER (), 0) AS normal_positive_duplicate_row_count,
           COALESCE(SUM(CASE WHEN mapping_count > 1 THEN mapping_count ELSE 0 END) OVER (), 0) AS normal_positive_conflicting_row_count,
           COALESCE(SUM(CASE WHEN mapping_count > 1 THEN 1 ELSE 0 END) OVER (), 0) AS conflicting_event_count,
           COALESCE(SUM(CASE WHEN mapping_count = 1 THEN 1 ELSE 0 END) OVER (), 0) AS accepted_row_count
    FROM EventGrain
), BaseDiagnostics AS (
    SELECT
        COUNT_BIG(*) AS source_row_count,
        COALESCE(SUM(CASE WHEN is_normal = 1 AND outbound_quantity > 0 THEN 1 ELSE 0 END), 0) AS normal_positive_row_count,
        COALESCE(SUM(CASE WHEN is_normal = 1 AND outbound_quantity > 0
            AND (NULLIF(outbound_date, '') IS NULL OR NULLIF(vendor_code, '') IS NULL
                OR NULLIF(outbound_seq, '') IS NULL OR NULLIF(product_code, '') IS NULL
                OR NULLIF(stock_code, '') IS NULL) THEN 1 ELSE 0 END), 0) AS normal_positive_missing_key_row_count,
        COALESCE(SUM(CASE WHEN is_normal = 1 AND outbound_quantity > 0
            AND NULLIF(outbound_date, '') IS NOT NULL AND NULLIF(vendor_code, '') IS NOT NULL
            AND NULLIF(outbound_seq, '') IS NOT NULL AND NULLIF(product_code, '') IS NOT NULL
            AND NULLIF(stock_code, '') IS NOT NULL AND outbound_quantity <> FLOOR(outbound_quantity)
            THEN 1 ELSE 0 END), 0) AS normal_positive_nonintegral_row_count,
        COALESCE(SUM(CASE WHEN is_normal = 1 AND outbound_quantity <= 0 THEN 1 ELSE 0 END), 0) AS normal_nonpositive_row_count,
        COALESCE(SUM(CASE WHEN is_return = 1 AND outbound_quantity > 0 THEN 1 ELSE 0 END), 0) AS return_positive_row_count,
        COALESCE(SUM(CASE WHEN is_return = 1 AND outbound_quantity <= 0 THEN 1 ELSE 0 END), 0) AS return_nonpositive_row_count,
        COALESCE(SUM(CASE WHEN is_normal = 0 AND is_return = 0 THEN 1 ELSE 0 END), 0) AS other_tcode_row_count
    FROM Classified
), MonthlyDays AS (
    SELECT LEFT(E.outbound_date, 6) AS [month], E.product_code, E.stock_code, E.outbound_date,
           COUNT_BIG(*) AS occurrence_count, SUM(E.outbound_quantity) AS outbound_quantity,
           MAX(E.normal_positive_duplicate_row_count) AS normal_positive_duplicate_row_count,
           MAX(E.normal_positive_conflicting_row_count) AS normal_positive_conflicting_row_count,
           MAX(E.conflicting_event_count) AS conflicting_event_count,
           MAX(E.accepted_row_count) AS accepted_row_count,
           MAX(B.source_row_count) AS source_row_count,
           MAX(B.normal_positive_row_count) AS normal_positive_row_count,
           MAX(B.normal_positive_missing_key_row_count) AS normal_positive_missing_key_row_count,
           MAX(B.normal_positive_nonintegral_row_count) AS normal_positive_nonintegral_row_count,
           MAX(B.normal_nonpositive_row_count) AS normal_nonpositive_row_count,
           MAX(B.return_positive_row_count) AS return_positive_row_count,
           MAX(B.return_nonpositive_row_count) AS return_nonpositive_row_count,
           MAX(B.other_tcode_row_count) AS other_tcode_row_count
    FROM EventAnnotated AS E
    CROSS JOIN BaseDiagnostics AS B
    WHERE E.mapping_count = 1
    GROUP BY LEFT(E.outbound_date, 6), E.product_code, E.stock_code, E.outbound_date
), MonthlyAndSummary AS (
    SELECT
        CASE WHEN GROUPING([month]) = 1 THEN 'summary' ELSE 'monthly' END AS row_kind,
        CASE WHEN GROUPING([month]) = 1 THEN '' ELSE [month] END AS [month],
        CASE WHEN GROUPING([month]) = 1 THEN '' ELSE product_code END AS product_code,
        CASE WHEN GROUPING([month]) = 1 THEN '' ELSE stock_code END AS stock_code,
        CASE WHEN GROUPING([month]) = 1 THEN 0 ELSE SUM(occurrence_count) END AS occurrence_count,
        CASE WHEN GROUPING([month]) = 1 THEN 0 ELSE SUM(outbound_quantity) END AS outbound_quantity,
        CASE WHEN GROUPING([month]) = 1 THEN 0 ELSE COUNT_BIG(*) END AS outbound_day_count,
        MAX(source_row_count) AS source_row_count,
        MAX(accepted_row_count) AS normal_positive_accepted_row_count,
        MAX(normal_positive_duplicate_row_count) AS normal_positive_duplicate_row_count,
        MAX(normal_positive_conflicting_row_count) AS normal_positive_conflicting_row_count,
        MAX(normal_positive_missing_key_row_count) AS normal_positive_missing_key_row_count,
        MAX(normal_positive_nonintegral_row_count) AS normal_positive_nonintegral_row_count,
        MAX(normal_nonpositive_row_count) AS normal_nonpositive_row_count,
        MAX(return_positive_row_count) AS return_positive_row_count,
        MAX(return_nonpositive_row_count) AS return_nonpositive_row_count,
        MAX(other_tcode_row_count) AS other_tcode_row_count,
        MAX(normal_positive_row_count) AS normal_positive_row_count,
        MAX(accepted_row_count) AS distinct_normal_event_count,
        MAX(conflicting_event_count) AS conflicting_event_count
    FROM MonthlyDays
    GROUP BY GROUPING SETS (([month], product_code, stock_code), ())
), EventFallbackDiagnostics AS (
    SELECT
        COALESCE(SUM(exact_duplicate_row_count), 0) AS normal_positive_duplicate_row_count,
        COALESCE(SUM(CASE WHEN mapping_count > 1 THEN mapping_count ELSE 0 END), 0) AS normal_positive_conflicting_row_count,
        COALESCE(SUM(CASE WHEN mapping_count > 1 THEN 1 ELSE 0 END), 0) AS conflicting_event_count,
        COALESCE(SUM(CASE WHEN mapping_count = 1 THEN 1 ELSE 0 END), 0) AS accepted_row_count
    FROM EventGrain
), EmptyAcceptedSummary AS (
    SELECT 'summary' AS row_kind, '' AS [month], '' AS product_code, '' AS stock_code,
           0 AS occurrence_count, 0 AS outbound_quantity, 0 AS outbound_day_count,
           B.source_row_count, E.accepted_row_count AS normal_positive_accepted_row_count,
           E.normal_positive_duplicate_row_count, E.normal_positive_conflicting_row_count,
           B.normal_positive_missing_key_row_count, B.normal_positive_nonintegral_row_count,
           B.normal_nonpositive_row_count, B.return_positive_row_count, B.return_nonpositive_row_count,
           B.other_tcode_row_count, B.normal_positive_row_count, E.accepted_row_count AS distinct_normal_event_count,
           E.conflicting_event_count
    FROM BaseDiagnostics AS B
    CROSS JOIN EventFallbackDiagnostics AS E
    WHERE NOT EXISTS (SELECT 1 FROM EventAnnotated WHERE mapping_count = 1)
)
SELECT * FROM MonthlyAndSummary
UNION ALL
SELECT * FROM EmptyAcceptedSummary
ORDER BY row_kind, [month], product_code, stock_code
""".strip()

def outbound_base_rows_sql(
    plan: FrequencySnapshotPlan | FrequencyMonthSourcePlan,
) -> tuple[str, dict[str, Any]]:
    """Build the shared Rddbc120 source for aggregate and read-only profiling."""
    binds: dict[str, Any] = {"basis_from": plan.basis_from, "basis_to": plan.basis_to}
    stock_clause = ""
    if plan.stock_codes:
        names: list[str] = []
        for index, code in enumerate(plan.stock_codes):
            key = f"stock_{index}"
            binds[key] = code
            names.append(f":{key}")
        # SQL Server character equality already ignores trailing blanks. Keep the
        # predicate on native columns so the ERP date/stock index remains usable.
        stock_clause = "AND O.Rd12_Stock_Cd_Gcode = '0018'\n      AND O.Rd12_Stock_Cd IN (" + ", ".join(names) + ")"
    product_join, product_clause = (
        _product_dimension_scope_sql(plan, binds, product_alias="P")
        if isinstance(plan, FrequencySnapshotPlan)
        else ("", "")
    )
    base = f"""
SELECT LTRIM(RTRIM(O.Rd12_Out_YyMmDd)) AS outbound_date,
    LTRIM(RTRIM(O.Rd12_Ven_Cd)) AS vendor_code,
    LTRIM(RTRIM(CONVERT(varchar(100), O.Rd12_Out_Seq))) AS outbound_seq,
    LTRIM(RTRIM(O.Rd12_Physic_Cd)) AS product_code,
    LTRIM(RTRIM(O.Rd12_Stock_Cd)) AS stock_code,
    LTRIM(RTRIM(O.Rd12_Io_Gu_Gcode)) AS io_gcode,
    LTRIM(RTRIM(O.Rd12_Io_Gu)) AS io_tcode,
    CAST(COALESCE(O.Rd12_Quantity, 0) + COALESCE(O.Rd12_Oquantity, 0) AS decimal(38, 6)) AS outbound_quantity
FROM dbo.Rddbc120 AS O
{product_join}
WHERE O.Rd12_Out_YyMmDd >= :basis_from AND O.Rd12_Out_YyMmDd <= :basis_to
  {stock_clause}
  {product_clause}
""".strip()
    return base, binds


def outbound_monthly_aggregate_sql(
    plan: FrequencySnapshotPlan | FrequencyMonthSourcePlan,
) -> tuple[str, dict[str, Any]]:
    base, binds = outbound_base_rows_sql(plan)
    return _aggregate_sql(base), binds


def outbound_event_grain_stream_sql(
    plan: FrequencySnapshotPlan | FrequencyMonthSourcePlan,
) -> tuple[str, dict[str, Any]]:
    """Return event-grain rows plus one base-diagnostics row for bounded local rollup."""
    base_rows_sql, binds = outbound_base_rows_sql(plan)
    io_tcode_number = sql_safe_int("io_tcode")
    return f"""
WITH BaseRows AS (
    {base_rows_sql}
), Classified AS (
    SELECT *,
        CASE WHEN io_gcode = '0012' AND LEN(io_tcode) = 3
                  AND io_tcode NOT LIKE '%[^0-9]%'
                  AND {io_tcode_number} BETWEEN 500 AND 599 THEN 1 ELSE 0 END AS is_normal,
        CASE WHEN io_gcode = '0012' AND LEN(io_tcode) = 3
                  AND io_tcode NOT LIKE '%[^0-9]%'
                  AND {io_tcode_number} BETWEEN 600 AND 699 THEN 1 ELSE 0 END AS is_return
    FROM BaseRows
), NormalPositive AS (
    SELECT * FROM Classified WHERE is_normal = 1 AND outbound_quantity > 0
), ExactRows AS (
    SELECT outbound_date, vendor_code, outbound_seq, product_code, stock_code, outbound_quantity,
           COUNT_BIG(*) AS exact_duplicate_count
    FROM NormalPositive
    WHERE NULLIF(outbound_date, '') IS NOT NULL AND NULLIF(vendor_code, '') IS NOT NULL
      AND NULLIF(outbound_seq, '') IS NOT NULL AND NULLIF(product_code, '') IS NOT NULL
      AND NULLIF(stock_code, '') IS NOT NULL
      AND outbound_quantity = FLOOR(outbound_quantity)
    GROUP BY outbound_date, vendor_code, outbound_seq, product_code, stock_code, outbound_quantity
), EventGrain AS (
    SELECT outbound_date, vendor_code, outbound_seq,
           COUNT_BIG(*) AS mapping_count,
           MAX(product_code) AS product_code,
           MAX(stock_code) AS stock_code,
           MAX(outbound_quantity) AS outbound_quantity,
           SUM(exact_duplicate_count - 1) AS exact_duplicate_row_count
    FROM ExactRows
    GROUP BY outbound_date, vendor_code, outbound_seq
), BaseDiagnostics AS (
    SELECT
        COUNT_BIG(*) AS source_row_count,
        COALESCE(SUM(CASE WHEN is_normal = 1 AND outbound_quantity > 0 THEN 1 ELSE 0 END), 0) AS normal_positive_row_count,
        COALESCE(SUM(CASE WHEN is_normal = 1 AND outbound_quantity > 0
            AND (NULLIF(outbound_date, '') IS NULL OR NULLIF(vendor_code, '') IS NULL
                OR NULLIF(outbound_seq, '') IS NULL OR NULLIF(product_code, '') IS NULL
                OR NULLIF(stock_code, '') IS NULL) THEN 1 ELSE 0 END), 0) AS normal_positive_missing_key_row_count,
        COALESCE(SUM(CASE WHEN is_normal = 1 AND outbound_quantity > 0
            AND NULLIF(outbound_date, '') IS NOT NULL AND NULLIF(vendor_code, '') IS NOT NULL
            AND NULLIF(outbound_seq, '') IS NOT NULL AND NULLIF(product_code, '') IS NOT NULL
            AND NULLIF(stock_code, '') IS NOT NULL AND outbound_quantity <> FLOOR(outbound_quantity)
            THEN 1 ELSE 0 END), 0) AS normal_positive_nonintegral_row_count,
        COALESCE(SUM(CASE WHEN is_normal = 1 AND outbound_quantity <= 0 THEN 1 ELSE 0 END), 0) AS normal_nonpositive_row_count,
        COALESCE(SUM(CASE WHEN is_return = 1 AND outbound_quantity > 0 THEN 1 ELSE 0 END), 0) AS return_positive_row_count,
        COALESCE(SUM(CASE WHEN is_return = 1 AND outbound_quantity <= 0 THEN 1 ELSE 0 END), 0) AS return_nonpositive_row_count,
        COALESCE(SUM(CASE WHEN is_normal = 0 AND is_return = 0 THEN 1 ELSE 0 END), 0) AS other_tcode_row_count
    FROM Classified
)
SELECT 'event' AS row_kind, E.outbound_date, E.vendor_code, E.product_code, E.stock_code,
       E.outbound_quantity, E.mapping_count, E.exact_duplicate_row_count,
       CAST(NULL AS bigint) AS source_row_count,
       CAST(NULL AS bigint) AS normal_positive_row_count,
       CAST(NULL AS bigint) AS normal_positive_missing_key_row_count,
       CAST(NULL AS bigint) AS normal_positive_nonintegral_row_count,
       CAST(NULL AS bigint) AS normal_nonpositive_row_count,
       CAST(NULL AS bigint) AS return_positive_row_count,
       CAST(NULL AS bigint) AS return_nonpositive_row_count,
       CAST(NULL AS bigint) AS other_tcode_row_count
FROM EventGrain AS E
UNION ALL
SELECT 'diagnostics', '', '', '', '', CAST(NULL AS decimal(38, 6)), CAST(NULL AS bigint), CAST(NULL AS bigint),
       B.source_row_count, B.normal_positive_row_count, B.normal_positive_missing_key_row_count,
       B.normal_positive_nonintegral_row_count, B.normal_nonpositive_row_count,
       B.return_positive_row_count, B.return_nonpositive_row_count, B.other_tcode_row_count
FROM BaseDiagnostics AS B
""".strip(), binds


def product_statistics_event_stream_sql(
    plan: FrequencySnapshotPlan,
) -> tuple[str, dict[str, Any]]:
    """Return 3-month event statistics and 12-month sales price in one ERP statement."""
    binds: dict[str, Any] = {
        "basis_from": plan.basis_from,
        "basis_to": plan.basis_to,
        "price_lookback_from": _month_start_months_before(plan.basis_from, 9),
    }
    stock_clause = ""
    if plan.stock_codes:
        names = []
        for index, code in enumerate(plan.stock_codes):
            key = f"statistics_stock_{index}"
            binds[key] = code
            names.append(f":{key}")
        stock_clause = "AND O.Rd12_Stock_Cd_Gcode='0018' AND O.Rd12_Stock_Cd IN (" + ", ".join(names) + ")"
    product_join, product_clause = _product_dimension_scope_sql(plan, binds, product_alias="P")
    return f"""
WITH BaseRows AS (
    SELECT LTRIM(RTRIM(O.Rd12_Out_YyMmDd)) AS outbound_date,
           LTRIM(RTRIM(O.Rd12_Ven_Cd)) AS vendor_code,
           LTRIM(RTRIM(CONVERT(varchar(100), O.Rd12_Out_Seq))) AS outbound_seq,
           LTRIM(RTRIM(O.Rd12_Physic_Cd)) AS product_code,
           LTRIM(RTRIM(O.Rd12_Stock_Cd)) AS stock_code,
           LTRIM(RTRIM(O.Rd12_Io_Gu_Gcode)) AS io_gcode,
           LTRIM(RTRIM(O.Rd12_Io_Gu)) AS io_tcode,
           CAST(COALESCE(O.Rd12_Quantity,0)+COALESCE(O.Rd12_Oquantity,0) AS decimal(38,6)) AS outbound_quantity,
           CAST(COALESCE(O.Rd12_Quantity,0) AS decimal(38,6)) AS paid_quantity,
           CAST(COALESCE(O.Rd12_Fin_Supply_Price,O.Rd12_Supply_Price,0) AS decimal(38,6)) AS supply_amount
    FROM dbo.Rddbc120 AS O
    {product_join}
    WHERE O.Rd12_Out_YyMmDd >= :price_lookback_from AND O.Rd12_Out_YyMmDd <= :basis_to
      {stock_clause}
      {product_clause}
), Classified AS (
    SELECT *,
      CASE WHEN io_gcode='0012' AND LEN(io_tcode)=3 AND io_tcode NOT LIKE '%[^0-9]%'
             AND LEFT(io_tcode,1)='5' THEN 1 ELSE 0 END AS is_normal,
      CASE WHEN io_gcode='0012' AND LEN(io_tcode)=3 AND io_tcode NOT LIKE '%[^0-9]%'
             AND LEFT(io_tcode,1)='6' THEN 1 ELSE 0 END AS is_return
    FROM BaseRows
), NormalExact AS (
    SELECT outbound_date,vendor_code,outbound_seq,product_code,stock_code,outbound_quantity,
           MAX(paid_quantity) AS paid_quantity,MAX(supply_amount) AS supply_amount,
           COUNT_BIG(*) AS duplicate_count
    FROM Classified
    WHERE is_normal=1 AND outbound_quantity>0
      AND NULLIF(outbound_date,'') IS NOT NULL AND NULLIF(vendor_code,'') IS NOT NULL
      AND NULLIF(outbound_seq,'') IS NOT NULL AND NULLIF(product_code,'') IS NOT NULL AND NULLIF(stock_code,'') IS NOT NULL
      AND outbound_quantity=FLOOR(outbound_quantity)
    GROUP BY outbound_date,vendor_code,outbound_seq,product_code,stock_code,outbound_quantity
), NormalEvents AS (
    SELECT outbound_date,vendor_code,outbound_seq,COUNT_BIG(*) AS mapping_count,
           MAX(product_code) AS product_code,MAX(stock_code) AS stock_code,
           MAX(outbound_quantity) AS outbound_quantity,MAX(paid_quantity) AS paid_quantity,
           MAX(supply_amount) AS supply_amount,SUM(duplicate_count-1) AS exact_duplicate_row_count
    FROM NormalExact GROUP BY outbound_date,vendor_code,outbound_seq
), SalesPriceMonthly AS (
    SELECT product_code,LEFT(outbound_date,6) AS basis_month,
           SUM(paid_quantity) AS paid_quantity,SUM(supply_amount) AS supply_amount
    FROM NormalEvents
    WHERE mapping_count=1 AND paid_quantity>0 AND supply_amount>0
    GROUP BY product_code,LEFT(outbound_date,6)
), LatestSalesPrice AS (
    SELECT *,ROW_NUMBER() OVER(PARTITION BY product_code ORDER BY basis_month DESC) AS price_rank
    FROM SalesPriceMonthly WHERE paid_quantity>0
), ReturnExact AS (
    SELECT outbound_date,vendor_code,outbound_seq,product_code,stock_code,outbound_quantity,supply_amount,
           COUNT_BIG(*) AS duplicate_count
    FROM Classified
    WHERE is_return=1 AND outbound_date>=:basis_from AND outbound_quantity<0 AND supply_amount<0
      AND NULLIF(outbound_date,'') IS NOT NULL AND NULLIF(vendor_code,'') IS NOT NULL
      AND NULLIF(outbound_seq,'') IS NOT NULL AND NULLIF(product_code,'') IS NOT NULL AND NULLIF(stock_code,'') IS NOT NULL
      AND outbound_quantity=FLOOR(outbound_quantity)
    GROUP BY outbound_date,vendor_code,outbound_seq,product_code,stock_code,outbound_quantity,supply_amount
), ReturnEvents AS (
    SELECT outbound_date,vendor_code,outbound_seq,COUNT_BIG(*) AS mapping_count,
           MAX(product_code) AS product_code,MAX(-outbound_quantity) AS return_quantity,
           MAX(-supply_amount) AS return_supply_amount
    FROM ReturnExact GROUP BY outbound_date,vendor_code,outbound_seq
), ReturnProduct AS (
    SELECT product_code,COUNT_BIG(*) AS return_event_count,
           SUM(return_quantity) AS return_quantity,SUM(return_supply_amount) AS return_supply_amount
    FROM ReturnEvents WHERE mapping_count=1 GROUP BY product_code
), Diagnostics AS (
    SELECT COUNT_BIG(*) AS source_row_count,
      COALESCE(SUM(CASE WHEN is_normal=1 AND outbound_date>=:basis_from AND outbound_quantity>0 THEN 1 ELSE 0 END),0) AS normal_positive_row_count,
      COALESCE(SUM(CASE WHEN is_normal=1 AND outbound_date>=:basis_from AND outbound_quantity>0 AND
        (NULLIF(outbound_date,'') IS NULL OR NULLIF(vendor_code,'') IS NULL OR NULLIF(outbound_seq,'') IS NULL
         OR NULLIF(product_code,'') IS NULL OR NULLIF(stock_code,'') IS NULL) THEN 1 ELSE 0 END),0) AS normal_positive_missing_key_row_count,
      COALESCE(SUM(CASE WHEN is_normal=1 AND outbound_date>=:basis_from AND outbound_quantity>0 AND
        NULLIF(outbound_date,'') IS NOT NULL AND NULLIF(vendor_code,'') IS NOT NULL
        AND NULLIF(outbound_seq,'') IS NOT NULL AND NULLIF(product_code,'') IS NOT NULL AND NULLIF(stock_code,'') IS NOT NULL
        AND outbound_quantity<>FLOOR(outbound_quantity) THEN 1 ELSE 0 END),0) AS normal_positive_nonintegral_row_count,
      COALESCE(SUM(CASE WHEN is_normal=1 AND outbound_date>=:basis_from AND outbound_quantity<=0 THEN 1 ELSE 0 END),0) AS normal_nonpositive_row_count,
      COALESCE(SUM(CASE WHEN is_return=1 AND outbound_date>=:basis_from AND outbound_quantity>0 THEN 1 ELSE 0 END),0) AS return_positive_row_count,
      COALESCE(SUM(CASE WHEN is_return=1 AND outbound_date>=:basis_from AND outbound_quantity<=0 THEN 1 ELSE 0 END),0) AS return_nonpositive_row_count,
      COALESCE(SUM(CASE WHEN is_normal=0 AND is_return=0 AND outbound_date>=:basis_from THEN 1 ELSE 0 END),0) AS other_tcode_row_count
    FROM Classified WHERE outbound_date>=:basis_from
)
SELECT 'event' row_kind,E.outbound_date,E.vendor_code,E.product_code,E.stock_code,E.outbound_quantity,E.paid_quantity,
       E.mapping_count,E.exact_duplicate_row_count,
       CAST(NULL AS varchar(6)) basis_month,CAST(NULL AS decimal(38,10)) unit_price,
       CAST(NULL AS bigint) return_event_count,CAST(NULL AS decimal(38,6)) return_quantity,CAST(NULL AS decimal(38,6)) return_supply_amount,
       CAST(NULL AS bigint) source_row_count,CAST(NULL AS bigint) normal_positive_row_count,
       CAST(NULL AS bigint) normal_nonpositive_row_count,CAST(NULL AS bigint) return_positive_row_count,
       CAST(NULL AS bigint) return_nonpositive_row_count,CAST(NULL AS bigint) other_tcode_row_count,
       CAST(NULL AS bigint) normal_positive_missing_key_row_count,CAST(NULL AS bigint) normal_positive_nonintegral_row_count
FROM NormalEvents E WHERE E.outbound_date>=:basis_from
UNION ALL
SELECT 'sales_price','', '',P.product_code,'',NULL,NULL,NULL,NULL,P.basis_month,
       CAST(P.supply_amount/P.paid_quantity AS decimal(38,10)),NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL
FROM LatestSalesPrice P WHERE P.price_rank=1
UNION ALL
SELECT 'return_stats','', '',R.product_code,'',NULL,NULL,NULL,NULL,NULL,NULL,R.return_event_count,R.return_quantity,R.return_supply_amount,
       NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL FROM ReturnProduct R
UNION ALL
SELECT 'diagnostics','', '', '', '',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,
       D.source_row_count,D.normal_positive_row_count,D.normal_nonpositive_row_count,D.return_positive_row_count,D.return_nonpositive_row_count,D.other_tcode_row_count,
       D.normal_positive_missing_key_row_count,D.normal_positive_nonintegral_row_count
FROM Diagnostics D
ORDER BY row_kind,product_code,outbound_date
""".strip(), binds


_PRODUCT_STATISTICS_STREAM_COLUMNS = (
    "row_kind", "outbound_date", "vendor_code", "product_code", "stock_code",
    "outbound_quantity", "paid_quantity", "mapping_count", "exact_duplicate_row_count",
    "cost_apply_code", "contract_supply_amount",
    "basis_month", "unit_price", "return_event_count", "return_quantity", "return_supply_amount",
    "source_row_count", "normal_positive_row_count", "normal_nonpositive_row_count",
    "return_positive_row_count", "return_nonpositive_row_count", "other_tcode_row_count",
    "normal_positive_missing_key_row_count", "normal_positive_nonintegral_row_count",
)


def product_statistics_raw_event_stream_sql(
    plan: FrequencySnapshotPlan,
    *,
    io_scope: SavedIoScope | None = None,
    include_original_supply: bool = False,
    stream_from: str | None = None,
    ordered: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Return one ordered, profile-scoped R120 stream for Python aggregation."""
    if io_scope is not None:
        _require_plan_io_scope(plan, io_scope)
    binds: dict[str, Any] = {
        "basis_from": plan.basis_from,
        "basis_to": plan.basis_to,
        "price_lookback_from": stream_from or _month_start_months_before(plan.basis_from, 9),
    }
    stock_clause = ""
    if plan.stock_codes:
        names = []
        for index, code in enumerate(plan.stock_codes):
            key = f"statistics_stock_{index}"
            binds[key] = code
            names.append(f":{key}")
        stock_clause = "AND O.Rd12_Stock_Cd_Gcode='0018' AND O.Rd12_Stock_Cd IN (" + ", ".join(names) + ")"
    selected_io_clause = (
        "AND O.Rd12_Io_Gu_Gcode='0012' AND "
        + bind_io_codes(
            "O.Rd12_Io_Gu", (*io_scope.outbound_codes, *io_scope.outbound_return_codes),
            binds, prefix="statistics_io",
        ) if io_scope is not None else ""
    )
    product_join, product_clause = _product_dimension_scope_sql(plan, binds, product_alias="P")
    return f"""
SELECT LTRIM(RTRIM(O.Rd12_Out_YyMmDd)) AS outbound_date,
       LTRIM(RTRIM(O.Rd12_Ven_Cd)) AS vendor_code,
       LTRIM(RTRIM(CONVERT(varchar(100), O.Rd12_Out_Seq))) AS outbound_seq,
       LTRIM(RTRIM(O.Rd12_Physic_Cd)) AS product_code,
       LTRIM(RTRIM(O.Rd12_Stock_Cd)) AS stock_code,
       LTRIM(RTRIM(O.Rd12_Io_Gu_Gcode)) AS io_gcode,
       LTRIM(RTRIM(O.Rd12_Io_Gu)) AS io_tcode,
       LTRIM(RTRIM(O.Rd12_Cost_Apply_Cd)) AS cost_apply_code,
       CAST(COALESCE(O.Rd12_Quantity,0)+COALESCE(O.Rd12_Oquantity,0) AS decimal(38,6)) AS outbound_quantity,
       CAST(COALESCE(O.Rd12_Quantity,0) AS decimal(38,6)) AS paid_quantity,
       CAST(COALESCE(O.Rd12_Fin_Supply_Price,O.Rd12_Supply_Price,0) AS decimal(38,6)) AS supply_amount
       {', CAST(COALESCE(O.Rd12_Supply_Price,0) AS decimal(38,6)) AS original_supply_amount' if include_original_supply else ''}
FROM dbo.Rddbc120 AS O
{product_join}
WHERE O.Rd12_Out_YyMmDd >= :price_lookback_from AND O.Rd12_Out_YyMmDd <= :basis_to
  {stock_clause}
  {selected_io_clause}
  {product_clause}
{"ORDER BY O.Rd12_Out_YyMmDd, O.Rd12_Ven_Cd, O.Rd12_Out_Seq" + (", O.Rd12_Io_Gu" if io_scope is not None and io_scope.policy_version == FULL_POLICY_VERSION else "") if ordered else ""}
""".strip(), binds


def product_statistics_compact_event_stream_sql(
    plan: FrequencySnapshotPlan, *, io_scope: SavedIoScope,
) -> tuple[str, dict[str, Any]]:
    """Keep recent slip rows exact; send only deduplicated monthly price evidence for older rows."""
    raw_sql, raw_binds = product_statistics_raw_event_stream_sql(plan, io_scope=io_scope, ordered=False)
    binds = dict(raw_binds)
    normal_clause = bind_io_codes("H.io_tcode", io_scope.outbound_codes, binds, prefix="historical_normal_io")
    sql = f"""WITH Raw AS (
{raw_sql}
), HistoricalExact AS (
    SELECT H.outbound_date,H.vendor_code,H.outbound_seq,H.io_tcode,
           H.product_code,H.stock_code,H.outbound_quantity,
           MAX(H.paid_quantity) AS paid_quantity,
           MAX(H.supply_amount) AS supply_amount
    FROM Raw AS H
    WHERE H.outbound_date < :basis_from AND H.io_gcode='0012' AND {normal_clause}
      AND H.outbound_quantity > 0 AND H.outbound_quantity=FLOOR(H.outbound_quantity)
      AND H.outbound_date <> '' AND H.vendor_code <> '' AND H.outbound_seq <> ''
      AND H.product_code <> '' AND H.stock_code <> ''
    GROUP BY H.outbound_date,H.vendor_code,H.outbound_seq,H.io_tcode,
             H.product_code,H.stock_code,H.outbound_quantity
), HistoricalSlip AS (
    SELECT outbound_date,vendor_code,outbound_seq,io_tcode,
           COUNT_BIG(*) AS mapping_count,MAX(product_code) AS product_code,
           MAX(paid_quantity) AS paid_quantity,MAX(supply_amount) AS supply_amount
    FROM HistoricalExact
    GROUP BY outbound_date,vendor_code,outbound_seq,io_tcode
), HistoricalMonthly AS (
    SELECT product_code,LEFT(outbound_date,6) AS basis_month,
           SUM(paid_quantity) AS paid_quantity,SUM(supply_amount) AS supply_amount
    FROM HistoricalSlip
    WHERE mapping_count=1 AND paid_quantity > 0 AND supply_amount > 0
    GROUP BY product_code,LEFT(outbound_date,6)
)
SELECT CONCAT(basis_month,'01') AS outbound_date,
       CAST('' AS varchar(100)) AS vendor_code,CAST('' AS varchar(100)) AS outbound_seq,
       product_code,CAST('' AS varchar(100)) AS stock_code,
       CAST('0012' AS varchar(4)) AS io_gcode,CAST('000' AS varchar(3)) AS io_tcode,
       CAST('' AS varchar(100)) AS cost_apply_code,
       CAST(0 AS decimal(38,6)) AS outbound_quantity,
       paid_quantity,supply_amount,CAST('historical_price' AS varchar(20)) AS stream_row_kind
FROM HistoricalMonthly
UNION ALL
SELECT outbound_date,vendor_code,outbound_seq,product_code,stock_code,
       io_gcode,io_tcode,cost_apply_code,outbound_quantity,paid_quantity,supply_amount,
       CAST('raw' AS varchar(20)) AS stream_row_kind
FROM Raw WHERE outbound_date >= :basis_from
ORDER BY outbound_date,vendor_code,outbound_seq,io_tcode"""
    return sql, binds

def _fixture_decimal(value: Any, *, field: str) -> Decimal:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise SnapshotContractError(f"SQL fixture {field} is required")
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise SnapshotContractError(f"SQL fixture {field} must be numeric") from exc


def _fixture_base_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Map raw-event fixture names to the exact BaseRows contract used by ERP SQL."""
    if "outbound_quantity" in row:
        outbound_quantity = _fixture_decimal(row.get("outbound_quantity"), field="outbound_quantity")
    else:
        outbound_quantity = _fixture_decimal(row.get("quantity"), field="quantity") + _fixture_decimal(
            row.get("oquantity", 0), field="oquantity"
        )
    return {
        "outbound_date": row.get("outbound_date"),
        "vendor_code": row.get("vendor_code"),
        "outbound_seq": row.get("outbound_seq"),
        "product_code": row.get("product_code"),
        "stock_code": row.get("stock_code"),
        "io_gcode": row.get("io_gcode", row.get("io_gu_gcode")),
        "io_tcode": row.get("io_tcode"),
        "outbound_quantity": outbound_quantity,
    }


def outbound_values_fixture_sql(rows: Iterable[Mapping[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Build a typed SQL Server VALUES CTE from raw-event fixture rows only."""
    binds: dict[str, Any] = {}
    value_rows: list[str] = []
    fields = ("outbound_date", "vendor_code", "outbound_seq", "product_code", "stock_code", "io_gcode", "io_tcode", "outbound_quantity")
    for index, row in enumerate(rows):
        base_row = _fixture_base_row(row)
        names: list[str] = []
        for field in fields:
            key = f"fixture_{index}_{field}"
            binds[key] = base_row[field]
            sql_type = "decimal(38, 6)" if field == "outbound_quantity" else "varchar(100)"
            names.append(f"CAST(:{key} AS {sql_type})")
        value_rows.append("(" + ", ".join(names) + ")")
    if not value_rows:
        raise SnapshotContractError("SQL equivalence fixture rows are required")
    base = "SELECT V.outbound_date, V.vendor_code, V.outbound_seq, V.product_code, V.stock_code, V.io_gcode, V.io_tcode, V.outbound_quantity FROM (VALUES\n    " + ",\n    ".join(value_rows) + "\n) AS V(outbound_date, vendor_code, outbound_seq, product_code, stock_code, io_gcode, io_tcode, outbound_quantity)"
    return _aggregate_sql(base), binds


def _query_company_df(company_id: int, sql: str, params: Mapping[str, Any], timeout_seconds: int) -> pd.DataFrame:
    previous_company_id = get_current_company_id()
    set_current_company_id(company_id)
    try:
        with get_engine().connect() as conn:
            raw = getattr(conn.connection, "driver_connection", conn.connection)
            if hasattr(raw, "timeout"):
                raw.timeout = max(1, int(timeout_seconds))
            return pd.read_sql_query(text(sql), conn, params=dict(params))
    finally:
        set_current_company_id(previous_company_id)


def _query_company_chunks(
    company_id: int,
    sql: str,
    params: Mapping[str, Any],
    timeout_seconds: int,
    *,
    chunk_rows: int = 50000,
    metrics: dict[str, Any] | None = None,
    use_driver_cursor: bool = False,
) -> Iterable[pd.DataFrame]:
    """Read one ERP statement incrementally without retaining its full event result."""
    previous_company_id = get_current_company_id()
    set_current_company_id(company_id)
    try:
        with get_engine().connect() as conn:
            raw = getattr(conn.connection, "driver_connection", conn.connection)
            if hasattr(raw, "timeout"):
                raw.timeout = max(1, int(timeout_seconds))
            query_started = time.perf_counter()
            cursor = None
            if use_driver_cursor:
                compiled = text(sql).compile(dialect=conn.dialect)
                names = compiled.positiontup
                if not names or any(name not in params for name in names):
                    raise SnapshotContractError("outbound stream bind compilation is incomplete")
                cursor = raw.cursor()
                cursor.arraysize = min(max(1, int(chunk_rows)), 10000)
                cursor.execute(str(compiled), *(params[name] for name in names))
                columns = [str(item[0]) for item in cursor.description]
                chunks = None
            else:
                chunks = pd.read_sql_query(
                    text(sql), conn, params=dict(params), chunksize=max(1, int(chunk_rows)), coerce_float=False
                )
            query_finished = time.perf_counter()
            fetched_rows = 0
            fetch_ms = 0.0
            try:
                iterator = iter(chunks) if chunks is not None else None
                while True:
                    fetch_started = time.perf_counter()
                    if cursor is not None:
                        batch = cursor.fetchmany(max(1, int(chunk_rows)))
                        if not batch:
                            fetch_ms += (time.perf_counter() - fetch_started) * 1000
                            break
                        chunk = pd.DataFrame.from_records(batch, columns=columns, coerce_float=False)
                    else:
                        try:
                            chunk = next(iterator)
                        except StopIteration:
                            fetch_ms += (time.perf_counter() - fetch_started) * 1000
                            break
                    fetch_ms += (time.perf_counter() - fetch_started) * 1000
                    if not isinstance(chunk, pd.DataFrame):
                        raise SnapshotContractError("outbound event stream returned an invalid chunk")
                    fetched_rows += len(chunk)
                    yield chunk
            finally:
                if cursor is not None:
                    cursor.close()
            if metrics is not None:
                metrics.update({
                    "db_query_ms": round((query_finished - query_started) * 1000, 3),
                    "fetch_rows": fetched_rows,
                    "fetch_ms": round(fetch_ms, 3),
                })
    finally:
        set_current_company_id(previous_company_id)


def _stream_text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _stream_decimal(value: Any, *, field: str, nullable: bool = False) -> Decimal | None:
    if value is None or pd.isna(value):
        if nullable:
            return None
        value = 0
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise SnapshotContractError(f"raw outbound stream {field} must be numeric") from exc
    if not number.is_finite():
        raise SnapshotContractError(f"raw outbound stream {field} must be finite")
    quantum = Decimal("0.000001")
    try:
        with localcontext() as context:
            context.prec = 96
            context.rounding = ROUND_HALF_UP
            canonical = number.quantize(quantum)
    except InvalidOperation as exc:
        raise SnapshotContractError(f"raw outbound stream {field} exceeds decimal(38,6)") from exc
    if canonical.is_zero():
        canonical = Decimal(0).quantize(quantum)
    if canonical and canonical.adjusted() >= 32:
        raise SnapshotContractError(f"raw outbound stream {field} exceeds decimal(38,6)")
    return canonical


def _sales_unit_price(supply_amount: Decimal, paid_quantity: Decimal) -> Decimal:
    """Match SQL Server decimal(38,6) division followed by decimal(38,10) cast."""
    try:
        with localcontext() as context:
            context.prec = 96
            context.rounding = ROUND_HALF_UP
            intermediate = (supply_amount / paid_quantity).quantize(Decimal("0.000001"))
            result = intermediate.quantize(Decimal("0.0000000001"))
    except (InvalidOperation, ZeroDivisionError) as exc:
        raise SnapshotContractError("sales price exceeds decimal(38,10)") from exc
    if result.is_zero():
        result = Decimal(0).quantize(Decimal("0.0000000001"))
    if result and result.adjusted() >= 28:
        raise SnapshotContractError("sales price exceeds decimal(38,10)")
    return result


def _python_product_statistics_projection_chunks(
    chunks: Iterable[pd.DataFrame],
    *,
    basis_from: str,
    output_chunk_rows: int = 50000,
    metrics: dict[str, Any] | None = None,
    io_scope: SavedIoScope | None = None,
    raw_row_observer: Callable[[Mapping[str, Any]], None] | None = None,
    accepted_event_observer: Callable[[Mapping[str, Any]], None] | None = None,
) -> Iterable[pd.DataFrame]:
    """Convert one ordered raw R120 stream to the legacy 22-column projection."""
    required = {
        "outbound_date", "vendor_code", "outbound_seq", "product_code", "stock_code",
        "io_gcode", "io_tcode", "outbound_quantity", "paid_quantity", "supply_amount",
    }
    started = time.perf_counter()
    output_rows: list[dict[str, Any]] = []
    sales_monthly: dict[tuple[str, str], list[Decimal]] = {}
    return_products: dict[str, list[Any]] = {}
    diagnostics = {
        "source_row_count": 0,
        "normal_positive_row_count": 0,
        "normal_positive_missing_key_row_count": 0,
        "normal_positive_nonintegral_row_count": 0,
        "normal_nonpositive_row_count": 0,
        "return_positive_row_count": 0,
        "return_nonpositive_row_count": 0,
        "other_tcode_row_count": 0,
    }
    preserve_io_role = io_scope is not None and io_scope.policy_version == FULL_POLICY_VERSION
    current_slip: tuple[str, ...] | None = None
    normal_exact: dict[tuple[str, str, Decimal], list[Any]] = {}
    return_exact: dict[tuple[str, str, Decimal, Decimal], int] = {}

    def projection(**values: Any) -> dict[str, Any]:
        return {column: values.get(column) for column in _PRODUCT_STATISTICS_STREAM_COLUMNS}

    def flush_slip() -> None:
        nonlocal normal_exact, return_exact
        if current_slip is None:
            return
        outbound_date, vendor_code, _outbound_seq = current_slip[:3]
        if normal_exact:
            mapping_count = len(normal_exact)
            product_code = max(key[0] for key in normal_exact)
            stock_code = max(key[1] for key in normal_exact)
            outbound_quantity = max(key[2] for key in normal_exact)
            paid_quantity = max(value[0] for value in normal_exact.values())
            supply_amount = max(value[1] for value in normal_exact.values())
            exact_duplicates = sum(int(value[2]) - 1 for value in normal_exact.values())
            if mapping_count == 1 and paid_quantity > 0 and supply_amount > 0:
                price_key = (product_code, outbound_date[:6])
                price = sales_monthly.setdefault(price_key, [Decimal(0), Decimal(0)])
                price[0] += paid_quantity
                price[1] += supply_amount
            if mapping_count == 1 and accepted_event_observer is not None and outbound_date >= basis_from:
                accepted_event_observer({
                    "kind": "normal", "month": outbound_date[:6], "product_code": product_code,
                    "stock_code": stock_code, "io_code": current_slip[3] if preserve_io_role else "",
                    "quantity": outbound_quantity, "paid_quantity": paid_quantity,
                    "supply_amount": supply_amount,
                })
            if outbound_date >= basis_from:
                if preserve_io_role and metrics is not None and mapping_count == 1:
                    io_tcode = current_slip[3]
                    events = metrics.setdefault("normal_event_count_by_io", {})
                    quantities = metrics.setdefault("normal_outbound_quantity_by_io", {})
                    amounts = metrics.setdefault("normal_supply_amount_by_io", {})
                    events[io_tcode] = int(events.get(io_tcode, 0)) + 1
                    quantities[io_tcode] = int(quantities.get(io_tcode, 0)) + int(outbound_quantity)
                    amounts[io_tcode] = amounts.get(io_tcode, Decimal(0)) + supply_amount
                output_rows.append(projection(
                    row_kind="event", outbound_date=outbound_date, vendor_code=vendor_code,
                    product_code=product_code, stock_code=stock_code,
                    outbound_quantity=outbound_quantity, paid_quantity=paid_quantity,
                    mapping_count=mapping_count, exact_duplicate_row_count=exact_duplicates,
                ))
        if return_exact:
            mapping_count = len(return_exact)
            if mapping_count == 1:
                key = next(iter(return_exact))
                product_code, _stock_code, outbound_quantity, supply_amount = key
                current = return_products.setdefault(product_code, [0, Decimal(0), Decimal(0)])
                current[0] += 1
                current[1] += -outbound_quantity
                current[2] += -supply_amount
                if accepted_event_observer is not None:
                    accepted_event_observer({
                        "kind": "return", "month": outbound_date[:6], "product_code": product_code,
                        "stock_code": key[1], "io_code": current_slip[3] if preserve_io_role else "",
                        "quantity": -outbound_quantity, "paid_quantity": Decimal(0),
                        "supply_amount": -supply_amount,
                    })
                if preserve_io_role and metrics is not None:
                    io_tcode = current_slip[3]
                    returns = metrics.setdefault("return_event_count_by_io", {})
                    quantities = metrics.setdefault("return_source_quantity_by_io", {})
                    amounts = metrics.setdefault("return_source_amount_by_io", {})
                    returns[io_tcode] = int(returns.get(io_tcode, 0)) + 1
                    quantities[io_tcode] = quantities.get(io_tcode, Decimal(0)) + outbound_quantity
                    amounts[io_tcode] = amounts.get(io_tcode, Decimal(0)) + supply_amount
        normal_exact = {}
        return_exact = {}

    for chunk in chunks:
        if not isinstance(chunk, pd.DataFrame) or not required.issubset(chunk.columns):
            raise SnapshotContractError("raw outbound event stream columns are invalid")
        for record in chunk.to_dict("records"):
            if record.get("stream_row_kind") == "historical_price":
                key = (_stream_text(record.get("product_code")), _stream_text(record.get("outbound_date"))[:6])
                current = sales_monthly.setdefault(key, [Decimal(0), Decimal(0)])
                current[0] += _stream_decimal(record.get("paid_quantity"), field="paid_quantity")
                current[1] += _stream_decimal(record.get("supply_amount"), field="supply_amount")
                if metrics is not None:
                    metrics["historical_price_group_count"] = int(metrics.get("historical_price_group_count", 0)) + 1
                continue
            if raw_row_observer is not None:
                raw_row_observer(record)
            outbound_date = _stream_text(record.get("outbound_date"))
            vendor_code = _stream_text(record.get("vendor_code"))
            outbound_seq = _stream_text(record.get("outbound_seq"))
            product_code = _stream_text(record.get("product_code"))
            stock_code = _stream_text(record.get("stock_code"))
            io_gcode = _stream_text(record.get("io_gcode"))
            io_tcode = _stream_text(record.get("io_tcode"))
            outbound_quantity = _stream_decimal(record.get("outbound_quantity"), field="outbound_quantity")
            paid_quantity = _stream_decimal(record.get("paid_quantity"), field="paid_quantity")
            supply_amount = _stream_decimal(record.get("supply_amount"), field="supply_amount")
            cost_apply_code = _stream_text(record.get("cost_apply_code"))
            slip = (outbound_date, vendor_code, outbound_seq, io_tcode) if preserve_io_role else (
                outbound_date, vendor_code, outbound_seq,
            )
            if current_slip is None:
                current_slip = slip
            elif slip != current_slip:
                flush_slip()
                current_slip = slip
                if len(output_rows) >= max(1, int(output_chunk_rows)):
                    yield pd.DataFrame.from_records(output_rows, columns=_PRODUCT_STATISTICS_STREAM_COLUMNS)
                    output_rows = []

            ascii_digits = len(io_tcode) == 3 and io_tcode.isascii() and io_tcode.isdigit()
            is_normal = io_gcode == "0012" and ascii_digits and (
                io_tcode in io_scope.outbound_codes if preserve_io_role else io_tcode[0] == "5"
            )
            is_return = io_gcode == "0012" and ascii_digits and (
                io_tcode in io_scope.outbound_return_codes if preserve_io_role else io_tcode[0] == "6"
            )
            if outbound_date >= basis_from:
                diagnostics["source_row_count"] += 1
                if is_normal and outbound_quantity > 0:
                    diagnostics["normal_positive_row_count"] += 1
                    missing_key = not all((outbound_date, vendor_code, outbound_seq, product_code, stock_code))
                    if missing_key:
                        diagnostics["normal_positive_missing_key_row_count"] += 1
                    elif outbound_quantity != outbound_quantity.to_integral_value():
                        diagnostics["normal_positive_nonintegral_row_count"] += 1
                elif is_normal:
                    diagnostics["normal_nonpositive_row_count"] += 1
                elif is_return and outbound_quantity > 0:
                    diagnostics["return_positive_row_count"] += 1
                elif is_return:
                    diagnostics["return_nonpositive_row_count"] += 1
                else:
                    diagnostics["other_tcode_row_count"] += 1

            complete_key = all((outbound_date, vendor_code, outbound_seq, product_code, stock_code))
            integral_quantity = outbound_quantity == outbound_quantity.to_integral_value()
            if is_normal and outbound_quantity > 0 and complete_key and integral_quantity:
                exact_key = (product_code, stock_code, outbound_quantity)
                exact = normal_exact.get(exact_key)
                if exact is None:
                    normal_exact[exact_key] = [paid_quantity, supply_amount, 1, cost_apply_code]
                else:
                    exact[0] = max(exact[0], paid_quantity)
                    exact[1] = max(exact[1], supply_amount)
                    exact[2] += 1
                    if exact[3] != cost_apply_code:
                        exact[3] = ""
            if (
                is_return and outbound_date >= basis_from and outbound_quantity < 0 and supply_amount < 0
                and complete_key and integral_quantity
            ):
                exact_key = (product_code, stock_code, outbound_quantity, supply_amount)
                return_exact[exact_key] = return_exact.get(exact_key, 0) + 1

    flush_slip()
    if output_rows:
        yield pd.DataFrame.from_records(output_rows, columns=_PRODUCT_STATISTICS_STREAM_COLUMNS)

    tail_rows = [projection(row_kind="diagnostics", **diagnostics)]
    latest_prices: dict[str, tuple[str, Decimal, Decimal]] = {}
    for (product_code, basis_month), (paid_quantity, supply_amount) in sales_monthly.items():
        previous = latest_prices.get(product_code)
        if previous is None or basis_month > previous[0]:
            latest_prices[product_code] = (basis_month, paid_quantity, supply_amount)
    for product_code, (basis_month, paid_quantity, supply_amount) in sorted(latest_prices.items()):
        tail_rows.append(projection(
            row_kind="sales_price", outbound_date="", vendor_code="", product_code=product_code,
            stock_code="", basis_month=basis_month,
            unit_price=_sales_unit_price(supply_amount, paid_quantity),
        ))
    for product_code, (event_count, quantity, supply_amount) in sorted(return_products.items()):
        tail_rows.append(projection(
            row_kind="return_stats", outbound_date="", vendor_code="", product_code=product_code,
            stock_code="", return_event_count=event_count, return_quantity=quantity,
            return_supply_amount=supply_amount,
        ))
    yield pd.DataFrame.from_records(tail_rows, columns=_PRODUCT_STATISTICS_STREAM_COLUMNS)
    if metrics is not None:
        total_ms = (time.perf_counter() - started) * 1000
        metrics["python_aggregate_ms"] = round(max(0.0, total_ms - float(metrics.get("fetch_ms") or 0)), 3)


def _event_grain_int(value: Any, *, field: str) -> int:
    if value is None or pd.isna(value):
        raise SnapshotContractError(f"outbound event stream {field} is required")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise SnapshotContractError(f"outbound event stream {field} must be numeric") from exc
    if number != number.to_integral_value():
        raise SnapshotContractError(f"outbound event stream {field} must be integral")
    return int(number)


def _aggregate_extended_event_grain_chunks(
    chunks: Iterable[pd.DataFrame],
    *,
    paid_quantities: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, int], dict[str, int]]:
    """Rebuild monthly activity from a single chunked exact event stream.

    The SQL result already suppresses exact duplicates and marks conflicting
    event keys.  Keeping only one integer day bitmap per month/product/stock
    bounds local state by output grain rather than ERP event row count.
    """
    monthly: dict[tuple[str, str, str], list[int]] = {}
    product_days: dict[str, set[str]] = {}
    product_customers: dict[str, set[str]] = {}
    diagnostics: dict[str, int] | None = None
    duplicate_rows = 0
    conflicting_rows = 0
    conflicting_events = 0
    accepted_rows = 0
    event_rows = 0
    required = {
        "row_kind", "outbound_date", "vendor_code", "product_code", "stock_code", "outbound_quantity",
        "mapping_count", "exact_duplicate_row_count", *ROW_PARTITION_FIELDS[3:], "source_row_count", "normal_positive_row_count",
    }
    for chunk in chunks:
        if not isinstance(chunk, pd.DataFrame) or not required.issubset(chunk.columns):
            raise SnapshotContractError("outbound event stream columns are invalid")
        for record in chunk.to_dict("records"):
            kind = str(record.get("row_kind") or "").strip()
            if kind == "diagnostics":
                if diagnostics is not None:
                    raise SnapshotContractError("outbound event stream has duplicate diagnostics")
                diagnostics = {
                    "source_row_count": _event_grain_int(record.get("source_row_count"), field="source_row_count"),
                    "normal_positive_row_count": _event_grain_int(record.get("normal_positive_row_count"), field="normal_positive_row_count"),
                    "normal_positive_missing_key_row_count": _event_grain_int(record.get("normal_positive_missing_key_row_count"), field="normal_positive_missing_key_row_count"),
                    "normal_positive_nonintegral_row_count": _event_grain_int(record.get("normal_positive_nonintegral_row_count"), field="normal_positive_nonintegral_row_count"),
                    "normal_nonpositive_row_count": _event_grain_int(record.get("normal_nonpositive_row_count"), field="normal_nonpositive_row_count"),
                    "return_positive_row_count": _event_grain_int(record.get("return_positive_row_count"), field="return_positive_row_count"),
                    "return_nonpositive_row_count": _event_grain_int(record.get("return_nonpositive_row_count"), field="return_nonpositive_row_count"),
                    "other_tcode_row_count": _event_grain_int(record.get("other_tcode_row_count"), field="other_tcode_row_count"),
                }
                continue
            if kind != "event":
                raise SnapshotContractError("outbound event stream row kind is invalid")
            event_rows += 1
            mapping_count = _event_grain_int(record.get("mapping_count"), field="mapping_count")
            exact_duplicates = _event_grain_int(record.get("exact_duplicate_row_count"), field="exact_duplicate_row_count")
            if mapping_count < 1 or exact_duplicates < 0:
                raise SnapshotContractError("outbound event stream count is invalid")
            if paid_quantities is not None and mapping_count == 1:
                code = str(record.get("product_code") or "").strip()
                try:
                    paid_quantity = Decimal(str(record.get("paid_quantity") or 0).strip())
                except (InvalidOperation, ValueError):
                    paid_quantity = Decimal(0)
                if paid_quantity > 0 and paid_quantity == paid_quantity.to_integral_value():
                    paid_quantities[code] += int(paid_quantity)
            duplicate_rows += exact_duplicates
            if mapping_count > 1:
                conflicting_rows += mapping_count
                conflicting_events += 1
                continue
            outbound_date = str(record.get("outbound_date") or "").strip()
            product_code = str(record.get("product_code") or "").strip()
            stock_code = str(record.get("stock_code") or "").strip()
            vendor_code = str(record.get("vendor_code") or "").strip()
            if len(outbound_date) != 8 or not outbound_date.isdigit() or not vendor_code or not product_code or not stock_code:
                raise SnapshotContractError("outbound event stream accepted row is invalid")
            quantity = _event_grain_int(record.get("outbound_quantity"), field="outbound_quantity")
            day_of_month = int(outbound_date[6:])
            if quantity <= 0 or day_of_month < 1 or day_of_month > 31:
                raise SnapshotContractError("outbound event stream accepted row is invalid")
            accepted_rows += 1
            key = (outbound_date[:6], product_code, stock_code)
            current = monthly.setdefault(key, [0, 0, 0])
            current[0] += 1
            current[1] += quantity
            current[2] |= 1 << (day_of_month - 1)
            product_days.setdefault(product_code, set()).add(outbound_date)
            product_customers.setdefault(product_code, set()).add(vendor_code)
    if diagnostics is None:
        raise SnapshotContractError("outbound event stream returned no diagnostics")
    diagnostics.update({
        "normal_positive_accepted_row_count": accepted_rows,
        "normal_positive_duplicate_row_count": duplicate_rows,
        "normal_positive_conflicting_row_count": conflicting_rows,
        "distinct_normal_event_count": accepted_rows,
        "conflicting_event_count": conflicting_events,
    })
    frame_rows = [
        {
            "row_kind": "monthly", "month": month, "product_code": product_code, "stock_code": stock_code,
            "occurrence_count": values[0], "outbound_quantity": values[1], "outbound_day_count": values[2].bit_count(),
            **diagnostics,
        }
        for (month, product_code, stock_code), values in sorted(monthly.items())
    ]
    frame_rows.append({
        "row_kind": "summary", "month": "", "product_code": "", "stock_code": "",
        "occurrence_count": 0, "outbound_quantity": 0, "outbound_day_count": 0, **diagnostics,
    })
    monthly_rows, final_diagnostics = _aggregate_result(pd.DataFrame(frame_rows))
    return (
        monthly_rows,
        final_diagnostics,
        {code: len(values) for code, values in product_days.items()},
        {code: len(values) for code, values in product_customers.items()},
    )


def _aggregate_product_statistics_event_chunks(
    chunks: Iterable[pd.DataFrame],
) -> tuple[
    list[dict[str, Any]], dict[str, int], dict[str, int], dict[str, int],
    dict[str, int], dict[str, dict[str, Any]], dict[str, dict[str, Any]],
]:
    """Split one multi-projection R120 stream without issuing another ERP query."""
    paid_quantities: dict[str, int] = defaultdict(int)
    return_statistics: dict[str, dict[str, Any]] = {}
    sales_prices: dict[str, dict[str, Any]] = {}
    def frequency_chunks() -> Iterable[pd.DataFrame]:
        for chunk in chunks:
            if not isinstance(chunk, pd.DataFrame) or "row_kind" not in chunk.columns:
                raise SnapshotContractError("product statistics event stream is invalid")
            kinds = chunk["row_kind"].fillna("").astype(str)
            frequency_mask = kinds.isin(("event", "diagnostics"))
            for record in chunk.loc[~frequency_mask].to_dict("records"):
                kind = str(record.get("row_kind") or "")
                code = str(record.get("product_code") or "").strip()
                if not code:
                    raise SnapshotContractError("product statistics row has no product code")
                if kind == "sales_price":
                    if code in sales_prices:
                        raise SnapshotContractError("sales price projection is duplicated")
                    sales_prices[code] = {
                        "unit_price": record.get("unit_price"),
                        "basis_month": str(record.get("basis_month") or ""),
                    }
                elif kind == "return_stats":
                    if code in return_statistics:
                        raise SnapshotContractError("return projection is duplicated")
                    return_statistics[code] = {
                        "event_count": _event_grain_int(record.get("return_event_count"), field="return_event_count"),
                        "quantity": _event_grain_int(record.get("return_quantity"), field="return_quantity"),
                        "supply_amount": record.get("return_supply_amount"),
                    }
                else:
                    raise SnapshotContractError("product statistics row kind is invalid")
            frequency = chunk if frequency_mask.all() else chunk.loc[frequency_mask]
            if not frequency.empty:
                yield frequency

    monthly, diagnostics, days, customers = _aggregate_extended_event_grain_chunks(
        frequency_chunks(), paid_quantities=paid_quantities
    )
    return monthly, diagnostics, days, customers, dict(paid_quantities), return_statistics, sales_prices


def _aggregate_event_grain_chunks(chunks: Iterable[pd.DataFrame]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    def _legacy_compatible() -> Iterable[pd.DataFrame]:
        for chunk in chunks:
            if isinstance(chunk, pd.DataFrame) and "vendor_code" not in chunk.columns:
                chunk = chunk.copy()
                chunk["vendor_code"] = "__legacy_fixture__"
            yield chunk

    monthly_rows, diagnostics, _day_counts, _customer_counts = _aggregate_extended_event_grain_chunks(_legacy_compatible())
    return monthly_rows, diagnostics


def _select_snapshot_product_universe(
    lifecycle_df: pd.DataFrame,
    monthly_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[str], dict[str, int]]:
    """Select profile-scoped products with stock or basis-period movement evidence."""
    required = {"product_code", "current_stock_present", "basis_inbound_present"}
    if not isinstance(lifecycle_df, pd.DataFrame) or not required.issubset(lifecycle_df.columns):
        raise SnapshotContractError("product universe evidence query returned an invalid shape")
    # Universe 판정에는 세 증거 열만 필요하므로 전체 lifecycle frame을 복사하지 않는다.
    work = lifecycle_df.loc[:, ["product_code", "current_stock_present", "basis_inbound_present"]].copy()
    work["product_code"] = work["product_code"].fillna("").astype(str).str.strip()
    work = work.loc[work["product_code"].ne("")].drop_duplicates("product_code", keep="first")
    stock_products = set(
        work.loc[pd.to_numeric(work["current_stock_present"], errors="coerce").fillna(0).astype(int).eq(1), "product_code"]
    )
    inbound_products = set(
        work.loc[pd.to_numeric(work["basis_inbound_present"], errors="coerce").fillna(0).astype(int).eq(1), "product_code"]
    )
    outbound_products = {
        str(row.get("product_code") or "").strip()
        for row in monthly_rows
        if str(row.get("product_code") or "").strip()
    }
    profile_products = set(work["product_code"])
    eligible = sorted(profile_products & (stock_products | inbound_products | outbound_products))
    return eligible, {
        "profile_product_count": len(profile_products),
        "current_stock_product_count": len(stock_products),
        "basis_inbound_product_count": len(inbound_products),
        "basis_outbound_product_count": len(outbound_products),
        "eligible_product_count": len(eligible),
    }


def _filter_product_statistics_to_universe(
    monthly_rows: Sequence[Mapping[str, Any]],
    diagnostics: Mapping[str, Any],
    product_code_set: set[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep final-universe events while retaining their source-side exclusion count."""
    accepted = int(diagnostics.get("normal_positive_accepted_row_count") or 0)
    total = sum(int(row.get("occurrence_count") or 0) for row in monthly_rows)
    if total != accepted:
        raise SnapshotContractError("accepted normal events do not match monthly activity")
    kept = [dict(row) for row in monthly_rows if str(row.get("product_code") or "").strip() in product_code_set]
    final_count = sum(int(row.get("occurrence_count") or 0) for row in kept)
    ignored = total - final_count
    if ignored < 0 or ignored > accepted or accepted - ignored != final_count:
        raise SnapshotContractError("ignored product event reconciliation is invalid")
    updated = dict(diagnostics)
    updated["ignored_product_event_count"] = ignored
    return kept, updated


def query_sqlserver_fixture(sql: str, binds: Mapping[str, Any], *, timeout_seconds: int = 30) -> pd.DataFrame:
    """Execute a parameterized VALUES CTE against SQL Server only for test equivalence."""
    from app.services.ssai_analytics_db import connect_analytics_db

    names = re.findall(r":([A-Za-z0-9_]+)", sql)
    conn = connect_analytics_db("reader", timeout=max(1, int(timeout_seconds)))
    try:
        conn.timeout = max(1, int(timeout_seconds))
        cursor = conn.cursor()
        cursor.execute(re.sub(r":[A-Za-z0-9_]+", "?", sql), tuple(binds[name] for name in names))
        return pd.DataFrame.from_records(cursor.fetchall(), columns=[str(item[0]) for item in cursor.description or ()])
    finally:
        conn.close()


def _aggregate_result(frame: pd.DataFrame) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not isinstance(frame, pd.DataFrame) or frame.empty or "row_kind" not in frame.columns:
        raise SnapshotContractError("outbound aggregate query returned no diagnostics")
    summary_rows = frame.loc[frame["row_kind"].astype(str).eq("summary")]
    if len(summary_rows) != 1:
        raise SnapshotContractError("outbound aggregate diagnostics row is invalid")
    diagnostics = {field: int(summary_rows.iloc[0].get(field) or 0) for field in DIAGNOSTIC_FIELDS}
    if sum(diagnostics[field] for field in ROW_PARTITION_FIELDS) != diagnostics["source_row_count"]:
        raise SnapshotContractError("outbound source row partition does not reconcile")
    if diagnostics["normal_positive_row_count"] != sum(diagnostics[field] for field in ROW_PARTITION_FIELDS[:5]):
        raise SnapshotContractError("normal positive row partition does not reconcile")
    if diagnostics["normal_positive_missing_key_row_count"]:
        raise SnapshotContractError("normal outbound contains incomplete event keys")
    if diagnostics["normal_positive_nonintegral_row_count"]:
        raise SnapshotContractError("normal outbound contains non-integral quantities")
    if diagnostics["conflicting_event_count"]:
        raise SnapshotContractError("normal outbound event key maps to conflicting product rows")
    diagnostics["diagnostic_contract_version"] = 2
    monthly = frame.loc[frame["row_kind"].astype(str).eq("monthly")]
    rows = monthly[["month", "product_code", "stock_code", "occurrence_count", "outbound_quantity", "outbound_day_count"]].to_dict("records")
    return rows, diagnostics


def _load_approved_monthly_purchase_prices(
    plan: FrequencySnapshotPlan, io_scope: SavedIoScope,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Use only approved, checksum-checked monthly evidence; do not requery ERP."""
    from app.services.monthly_io_components import reconstruct_purchase_prices
    from app.services.sql_server_monthly_io_repository import SqlServerMonthlyIoRepository

    started = time.perf_counter()
    scope_key = io_scope.fingerprint(plan.profile_fingerprint)
    repository = SqlServerMonthlyIoRepository(
        lambda: connect_company_analytics_db(plan.company_id, "reader"),
        lambda: connect_company_analytics_db(plan.company_id, "writer"),
    )
    first_six = _month_start_months_before(plan.basis_from, 3)[:6]
    first_index = int(first_six[:4]) * 12 + int(first_six[4:]) - 1
    months = tuple(f"{index // 12:04d}{index % 12 + 1:02d}" for index in range(first_index, first_index + 6))
    facts = []
    provenance = []
    fact_read_metrics: dict[str, Any] = {}
    try:
        historical_months = repository.list_approved_months_before(
            company_id=plan.company_id, before_month=first_six,
            profile_fingerprint=plan.profile_fingerprint, scope_fingerprint=scope_key,
        )
        if not historical_months:
            raise LookupError("approved historical price seed is missing")
        first_historical = int(historical_months[0][:4]) * 12 + int(historical_months[0][4:]) - 1
        expected_historical = tuple(
            f"{index // 12:04d}{index % 12 + 1:02d}"
            for index in range(first_historical, first_index)
        )
        if historical_months != expected_historical:
            raise LookupError("approved historical price months are incomplete")
        stored_months = repository.read_price_period_for_scope(
            company_id=plan.company_id, months=historical_months + months,
            first_bucket_month=first_six,
            profile_fingerprint=plan.profile_fingerprint, scope_fingerprint=scope_key,
            stock_codes=plan.stock_codes, timings_ms=fact_read_metrics,
        )
        for stored in stored_months:
            facts.extend(stored.facts)
            provenance.append({"month": stored.basis_month, "manifest_id": stored.manifest_id,
                               "source_verified_at": str(stored.source_verified_at)})
    except Exception as exc:
        detail = getattr(exc, "name", "") if isinstance(exc, NameError) else ""
        log.warning("[dashboard.snapshot.monthly_price] status=direct_fallback reason=%s symbol=%s",
                    type(exc).__name__, detail)
        return {}, {"status": "direct_fallback", "reason": type(exc).__name__, "reused_months": []}
    candidates = {fact.product_code for fact in facts
                  if fact.source_kind == "R110" and fact.row_kind == "PRICE_BUCKET"}
    candidates.update(fact.product_code for fact in facts
                      if fact.source_kind == "R110" and fact.row_kind == "PRICE_TAIL")
    try:
        prices = reconstruct_purchase_prices(
            facts, three_month_from=plan.basis_from[:6], six_month_from=first_six,
            basis_month=plan.basis_to[:6], product_codes=candidates,
        )
    except Exception as exc:
        log.warning("[dashboard.snapshot.monthly_price] status=direct_fallback reason=%s", type(exc).__name__)
        return {}, {"status": "direct_fallback", "reason": type(exc).__name__, "reused_months": []}
    sql_membership = {
        fact.product_code for fact in facts
        if fact.source_kind == "R110"
        and fact.row_kind in {"PRICE_BUCKET", "PRICE_TAIL"}
        and fact.last_tie_conflict == 0
    }
    if sql_membership != set(prices):
        log.warning("[dashboard.snapshot.monthly_price] status=direct_fallback reason=membership_mismatch")
        return {}, {"status": "direct_fallback", "reason": "membership_mismatch", "reused_months": []}
    return prices, {"status": "approved_reused_without_source_revalidation",
                    "reused_months": provenance, "cached_product_count": len(prices),
                    "erp_source_calls_added": 0,
                    "fact_read_metrics": fact_read_metrics,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}


def _overlay_monthly_purchase_prices(
    lifecycle_df: pd.DataFrame, prices: Mapping[str, Mapping[str, Any]],
) -> pd.DataFrame:
    updated = lifecycle_df.copy()
    values = {
        "avg_purchase_unit_cost": "unit_price",
        "purchase_price_basis_month": "basis_month",
        "purchase_price_basis_kind": "basis_kind",
        "excluded_purchase_trade_count": "excluded_trade_count",
        "excluded_purchase_quantity": "excluded_quantity",
        "excluded_purchase_amount": "excluded_amount",
        "original_purchase_quantity": "original_quantity",
        "original_purchase_amount": "original_amount",
        "purchase_center_price": "center_price",
    }
    for index, product in updated["product_code"].items():
        cached = prices.get(str(product).strip())
        if cached is None:
            continue
        direct = updated.at[index, "avg_purchase_unit_cost"]
        if direct is not None and not pd.isna(direct):
            raise SnapshotContractError("monthly price cache did not bypass direct price computation")
        for column, field in values.items():
            updated.at[index, column] = cached[field]
    return updated


def _generate_frequency_snapshot_draft_locked(
    *, plan: FrequencySnapshotPlan, created_by: str, timeout_seconds: int = 120,
    query_executor: QueryExecutor | None = None, repository: Any | None = None,
    force: bool = False, progress_reporter: ProgressReporter | None = None,
    classification_authority_loader: Callable[..., Any] = load_effective_company_classification_authority,
    io_scope: SavedIoScope | None = None, shadow_only: bool = False,
    saved_io_full: bool = False,
    monthly_reuse: bool = True,
) -> dict[str, Any]:
    actor = str(created_by or "").strip()
    if not actor:
        raise SnapshotContractError("created_by is required")
    use_saved_io = shadow_only or saved_io_full
    if use_saved_io:
        if io_scope is None or io_scope.policy_version != FULL_POLICY_VERSION:
            raise SnapshotContractError("saved I/O calculation requires the full saved I/O policy")
        _require_plan_io_scope(plan, io_scope)
        if io_scope.unclassified_codes:
            raise SnapshotContractError(
                "saved I/O roles are unconfirmed: " + ", ".join(io_scope.unclassified_codes)
            )
        if force:
            raise SnapshotContractError("saved I/O generation cannot be forced")
    elif io_scope is not None:
        raise SnapshotContractError("saved I/O candidates cannot be written as a Snapshot draft")
    query = query_executor or _query_company_df
    report = progress_reporter or (lambda _message: None)
    evaluation_last_day = date(
        int(plan.evaluation_month[:4]),
        int(plan.evaluation_month[4:]),
        calendar.monthrange(int(plan.evaluation_month[:4]), int(plan.evaluation_month[4:]))[1],
    )
    lifecycle_cutoff = min(evaluation_last_day, kst_today()).strftime("%Y%m%d")
    def load_source2() -> tuple[tuple[Any, ...], dict[str, Any]]:
        aggregate_sql, aggregate_binds = (
            product_statistics_compact_event_stream_sql(plan, io_scope=io_scope)
            if saved_io_full else product_statistics_raw_event_stream_sql(plan, io_scope=io_scope)
        )
        metrics: dict[str, Any] = {}
        started = time.perf_counter()
        result = _aggregate_product_statistics_event_chunks(
            _python_product_statistics_projection_chunks(
                _query_company_chunks(
                    plan.company_id, aggregate_sql, aggregate_binds, timeout_seconds,
                    metrics=metrics, use_driver_cursor=True,
                ),
                basis_from=plan.basis_from, metrics=metrics, io_scope=io_scope,
            ),
        )
        metrics["total_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return result, metrics

    monthly_price_cache: dict[str, dict[str, Any]] = {}
    monthly_cache_metrics: dict[str, Any] = {"status": "direct", "reused_months": []}
    source2_executor: ThreadPoolExecutor | None = None
    source2_future: Any | None = None
    if saved_io_full and monthly_reuse and query_executor is None:
        source2_executor = ThreadPoolExecutor(max_workers=1)
        source2_future = source2_executor.submit(load_source2)
        monthly_price_cache, monthly_cache_metrics = _load_approved_monthly_purchase_prices(plan, io_scope)
        report(
            "monthly_price_cache status=%s months=%s elapsed_ms=%s" % (
                monthly_cache_metrics.get("status"),
                len(monthly_cache_metrics.get("reused_months", ())),
                monthly_cache_metrics.get("elapsed_ms"),
            )
        )
    lifecycle_sql, lifecycle_binds = product_lifecycle_sql(
        stock_codes=plan.stock_codes,
        cutoff_date=lifecycle_cutoff,
        basis_from=plan.basis_from,
        basis_to=plan.basis_to,
        stock_mode=plan.stock_mode,
        product_group_codes=plan.product_group_codes,
        product_di_codes=plan.product_di_codes,
        product_class_codes=plan.product_class_codes,
        price_lookback_from=_month_start_months_before(plan.basis_from, 9),
        include_first_outbound=False,
        force_hash_join=plan.company_id == 8,
        snapshot_projection_only=True,
        exclude_management_only=True,
        seek_char5_monthly_stock=plan.company_id in (12, 13) and plan.stock_mode == "real",
        io_scope=io_scope,
        average_purchase_price=use_saved_io,
        precomputed_purchase_prices=bool(monthly_price_cache),
    )
    report("제품 및 최초 정상 입고 조회 중")
    source1_started = time.perf_counter()
    try:
        lifecycle_df = query(plan.company_id, lifecycle_sql, lifecycle_binds, timeout_seconds)
        source1_elapsed_ms = round((time.perf_counter() - source1_started) * 1000, 3)
    except BaseException:
        if source2_executor is not None:
            source2_executor.shutdown(wait=True)
        raise
    source_metrics: dict[str, Any] = {
        "source1_ms": source1_elapsed_ms,
    }
    report("source1_complete elapsed_ms=%s" % source_metrics["source1_ms"])
    if saved_io_full:
        source_metrics["monthly_purchase_cache"] = monthly_cache_metrics
    if io_scope is not None:
        source_metrics["saved_io_roles"] = io_scope.snapshot_role_report()
    if not isinstance(lifecycle_df, pd.DataFrame) or "product_code" not in lifecycle_df.columns:
        raise SnapshotContractError("product lifecycle query returned an invalid shape")
    if use_saved_io and not {"representative_vendor_code", "purchase_cost_apply_code"}.issubset(lifecycle_df.columns):
        raise SnapshotContractError("representative purchase vendor evidence is missing")
    if monthly_price_cache:
        lifecycle_df = _overlay_monthly_purchase_prices(lifecycle_df, monthly_price_cache)
    if source2_future is None:
        report("출고 집계 중")
    if query_executor is None:
        try:
            source2_result, source2_metrics = (
                source2_future.result() if source2_future is not None else load_source2()
            )
        finally:
            if source2_executor is not None:
                source2_executor.shutdown(wait=True)
        monthly_rows, diagnostics, product_day_counts, product_customer_counts, outbound_paid_quantities, return_statistics, sales_prices = source2_result
        source_metrics["source2"] = dict(source2_metrics)
        log.info(
            "[dashboard.snapshot.source2_python] company_id=%s evaluation_month=%s db_query_ms=%s fetch_rows=%s fetch_ms=%s python_aggregate_ms=%s total_ms=%s source_calls=1 retry=0 timeout_s=%s",
            plan.company_id,
            plan.evaluation_month,
            source2_metrics.get("db_query_ms"),
            source2_metrics.get("fetch_rows"),
            source2_metrics.get("fetch_ms"),
            source2_metrics.get("python_aggregate_ms"),
            source2_metrics.get("total_ms"),
            timeout_seconds,
        )
        report(
            "source2_complete db_query_ms=%s fetch_rows=%s fetch_ms=%s python_aggregate_ms=%s total_ms=%s" % (
                source2_metrics.get("db_query_ms"), source2_metrics.get("fetch_rows"),
                source2_metrics.get("fetch_ms"), source2_metrics.get("python_aggregate_ms"),
                source2_metrics.get("total_ms"),
            )
        )
    else:
        aggregate_sql, aggregate_binds = (
            product_statistics_raw_event_stream_sql(plan, io_scope=io_scope)
            if use_saved_io else product_statistics_event_stream_sql(plan)
        )
        source2_started = time.perf_counter()
        source2_frame = query(plan.company_id, aggregate_sql, aggregate_binds, timeout_seconds)
        source2_metrics = {} if use_saved_io else None
        source2_chunks = (
            _python_product_statistics_projection_chunks(
                (source2_frame,), basis_from=plan.basis_from, io_scope=io_scope,
                metrics=source2_metrics,
            ) if use_saved_io else (source2_frame,)
        )
        monthly_rows, diagnostics, product_day_counts, product_customer_counts, outbound_paid_quantities, return_statistics, sales_prices = _aggregate_product_statistics_event_chunks(
            source2_chunks,
        )
        source_metrics["source2"] = {
            **(source2_metrics or {}),
            "total_ms": round((time.perf_counter() - source2_started) * 1000, 3),
        }
    product_codes, universe_diagnostics = _select_snapshot_product_universe(lifecycle_df, monthly_rows)
    if not product_codes:
        raise SnapshotContractError("dashboard profile product universe is empty")
    product_code_set = set(product_codes)
    monthly_rows, diagnostics = _filter_product_statistics_to_universe(
        monthly_rows, diagnostics, product_code_set
    )
    product_day_counts = {code: value for code, value in product_day_counts.items() if code in product_code_set}
    product_customer_counts = {code: value for code, value in product_customer_counts.items() if code in product_code_set}
    outbound_paid_quantities = {code: value for code, value in outbound_paid_quantities.items() if code in product_code_set}
    return_statistics = {code: value for code, value in return_statistics.items() if code in product_code_set}
    sales_prices = {code: value for code, value in sales_prices.items() if code in product_code_set}
    for price in sales_prices.values():
        price["status"] = _price_status(plan.evaluation_month, price.get("basis_month"))
    first_inbound_months: dict[str, Any] = {}
    purchase_prices: dict[str, dict[str, Any]] = {}
    excluded_purchase_products: dict[str, dict[str, Any]] = {}
    purchase_basis_counts = {"three_month": 0, "six_month": 0, "historical": 0, "unavailable": 0}
    classification_authority = classification_authority_loader(
        company_id=plan.company_id,
        as_of=date(int(lifecycle_cutoff[:4]), int(lifecycle_cutoff[4:6]), int(lifecycle_cutoff[6:])),
    )
    adjustment_only_products: set[str] = set()
    profitability_unavailable_products: set[str] = set()
    for row in lifecycle_df.itertuples(index=False):
        code = str(getattr(row, "product_code", "") or "").strip()
        if code not in product_code_set:
            continue
        purchase_price_basis_month = getattr(row, "purchase_price_basis_month", None)
        first_inbound_months[code] = getattr(row, "first_normal_inbound_month", None)
        purchase_prices[code] = {
            "unit_price": getattr(row, "avg_purchase_unit_cost", None),
            "basis_month": purchase_price_basis_month,
            "status": (
                "ready" if purchase_price_basis_month and getattr(row, "avg_purchase_unit_cost", None) is not None
                else "unavailable"
            ) if use_saved_io else _price_status(plan.evaluation_month, purchase_price_basis_month),
        }
        if use_saved_io:
            excluded = int(getattr(row, "excluded_purchase_trade_count", 0) or 0)
            if excluded:
                excluded_purchase_products[code] = {
                    "trade_count": excluded,
                    "quantity": str(getattr(row, "excluded_purchase_quantity", 0) or 0),
                    "amount": str(getattr(row, "excluded_purchase_amount", 0) or 0),
                    "center_price": str(getattr(row, "purchase_center_price", 0) or 0),
                    "before": str(
                        (Decimal(str(getattr(row, "original_purchase_amount"))) /
                         Decimal(str(getattr(row, "original_purchase_quantity"))))
                        if getattr(row, "original_purchase_quantity", None) else 0
                    ),
                    "after": str(getattr(row, "avg_purchase_unit_cost", 0) or 0),
                }
            basis_kind = str(getattr(row, "purchase_price_basis_kind", "") or "")
            if basis_kind not in purchase_basis_counts:
                basis_kind = "unavailable"
            purchase_basis_counts[basis_kind] += 1
        targets = ProductClassificationTargets(
            product_group_keys=tuple(value for value in (str(getattr(row, "product_group_key", "") or "").strip(),) if value and not value.endswith(":")),
            product_di_keys=tuple(value for value in (str(getattr(row, "product_di_key", "") or "").strip(),) if value and not value.endswith(":")),
            product_class_keys=tuple(value for value in (str(getattr(row, "product_class_key", "") or "").strip(),) if value and not value.endswith(":")),
        )
        classification = classify_product_from_loaded_authority(
            authority=classification_authority,
            company_id=plan.company_id,
            product_code=code,
            targets=targets,
        )
        if classification.status == ClassificationStatus.READY and classification.adjustment_only:
            adjustment_only_products.add(code)
        elif classification.status in {
            ClassificationStatus.UNAVAILABLE, ClassificationStatus.CONFLICT, ClassificationStatus.CORRUPT
        }:
            profitability_unavailable_products.add(code)
    if use_saved_io:
        source_metrics["average_purchase_price_coverage"] = purchase_basis_counts
        source_metrics["excluded_purchase_trades"] = {
            "product_count": len(excluded_purchase_products),
            "trade_count": sum(value["trade_count"] for value in excluded_purchase_products.values()),
            "by_product": excluded_purchase_products,
        }
    log.info(
        "[dashboard.snapshot.product_universe] company_id=%s evaluation_month=%s profile_products=%s current_stock_products=%s basis_inbound_products=%s basis_outbound_products=%s eligible_products=%s product_group_filters=%s product_di_filters=%s product_class_filters=%s stock_mode=%s",
        plan.company_id,
        plan.evaluation_month,
        universe_diagnostics["profile_product_count"],
        universe_diagnostics["current_stock_product_count"],
        universe_diagnostics["basis_inbound_product_count"],
        universe_diagnostics["basis_outbound_product_count"],
        universe_diagnostics["eligible_product_count"],
        len(plan.product_group_codes),
        len(plan.product_di_codes),
        len(plan.product_class_codes),
        plan.stock_mode,
    )
    report("등급 계산 중")
    relational_snapshot = build_product_statistics_relational_snapshot_from_aggregates(
        company_id=plan.company_id, evaluation_month=plan.evaluation_month, monthly_rows=monthly_rows,
        product_codes=product_codes, product_day_counts=product_day_counts,
        product_customer_counts=product_customer_counts,
        first_normal_inbound_months=first_inbound_months,
        outbound_paid_quantities=outbound_paid_quantities,
        return_statistics=return_statistics,
        purchase_prices=purchase_prices,
        sales_prices=sales_prices,
        adjustment_only_products=adjustment_only_products,
        profitability_unavailable_products=profitability_unavailable_products,
        stock_codes=plan.stock_codes, source_watermark=None,
        source_watermark_status="unverified", source_diagnostics=diagnostics,
        profile_fingerprint=(selected_io_snapshot_key(plan, io_scope).profile_fingerprint
                             if use_saved_io else plan.profile_fingerprint),
        algorithm_version=(SAVED_IO_AVG_PURCHASE_ALGORITHM_VERSION if use_saved_io
                           else PRODUCT_STATISTICS_ALGORITHM_VERSION),
    )
    key = relational_snapshot.key
    if use_saved_io and key != selected_io_snapshot_key(plan, io_scope):
        raise SnapshotContractError("saved I/O key does not match the stored profile")
    if shadow_only:
        return {
            "plan": plan,
            "relational_snapshot": relational_snapshot,
            "source_metrics": source_metrics,
            "operating_eligible": False,
            "price_authority": "shadow_candidate_only",
        }
    repo = repository or _company_snapshot_repository(int(plan.company_id))
    report("snapshot_built products=%s checksum=%s" % (relational_snapshot.item_count, relational_snapshot.checksum))
    report("draft 저장 중")
    publish_relational = getattr(repo, "publish_relational", None)
    if not callable(publish_relational):
        raise SnapshotContractError("repository does not support relational frequency snapshots")
    if saved_io_full:
        draft = publish_relational(relational_snapshot, created_by=actor, force=False, saved_io_verified=True)
    else:
        draft = publish_relational(relational_snapshot, created_by=actor, force=bool(force))
    draft_generation_no = int(draft.generation_no or 0)
    draft_inspection = repo.inspect_generation(key, draft_generation_no)
    operating_read = repo.read(key)
    if draft.no_op:
        if (
            draft_generation_no > 0
            and draft_inspection.status == "ready"
            and draft_inspection.manifest_status == "published"
            and draft_inspection.approval_status == "approved"
            and draft_inspection.generation_no == draft_generation_no
            and draft_inspection.checksum.lower() == relational_snapshot.checksum.lower()
            and draft_inspection.representation == EXTENDED_RELATIONAL_FREQUENCY_REPRESENTATION
            and draft_inspection.relational_snapshot is not None
            and operating_read.status == "ready"
            and operating_read.generation_no == draft_generation_no
        ):
            return {
                "plan": plan,
                "relational_snapshot": relational_snapshot,
                "draft": draft,
                "read_status": operating_read.status,
                "draft_inspection_status": draft_inspection.status,
                "source_metrics": source_metrics,
            }
        raise SnapshotContractError("identical snapshot already exists but is not an approved operating generation")
    if (
        draft_generation_no <= 0
        or draft_inspection.status != "unapproved"
        or draft_inspection.manifest_status != "draft"
        or draft_inspection.approval_status != "pending"
        or draft_inspection.generation_no != draft_generation_no
        or draft_inspection.checksum.lower() != relational_snapshot.checksum.lower()
        or draft_inspection.representation != EXTENDED_RELATIONAL_FREQUENCY_REPRESENTATION
        or draft_inspection.relational_snapshot is None
    ):
        raise SnapshotContractError("draft generation exact inspection failed before manual approval")
    if operating_read.status == "ready" and operating_read.generation_no == draft_generation_no:
        raise SnapshotContractError("draft generation was exposed through the operating read before manual approval")
    if operating_read.status not in {"ready", "unapproved"}:
        raise SnapshotContractError(f"operating snapshot read failed after draft save: {operating_read.status}")
    return {
        "plan": plan,
        "relational_snapshot": relational_snapshot,
        "draft": draft,
        "read_status": operating_read.status,
        "draft_inspection_status": draft_inspection.status,
        "source_metrics": source_metrics,
    }


def generate_frequency_snapshot_draft(*, plan: FrequencySnapshotPlan, created_by: str, timeout_seconds: int = 120, query_executor: QueryExecutor | None = None, repository: Any | None = None, force: bool = False, progress_reporter: ProgressReporter | None = None, classification_authority_loader: Callable[..., Any] = load_effective_company_classification_authority, saved_io_full: bool = False, monthly_reuse: bool = True) -> dict[str, Any]:
    """Generate one exact snapshot draft while preventing an overlapping run."""
    require_unified_snapshot_algorithm(plan.evaluation_month, saved_io_full=saved_io_full)
    io_scope = resolve_saved_io_scope(plan.io_gu_codes, confirmed_trade_roles=True) if saved_io_full else None
    key = selected_io_snapshot_key(plan, io_scope) if io_scope is not None else None
    with frequency_snapshot_generation_guard(plan, key=key):
        return _generate_frequency_snapshot_draft_locked(
            plan=plan,
            created_by=created_by,
            timeout_seconds=timeout_seconds,
            query_executor=query_executor,
            repository=repository,
            force=force,
            progress_reporter=progress_reporter,
            classification_authority_loader=classification_authority_loader,
            io_scope=io_scope, saved_io_full=saved_io_full,
            monthly_reuse=monthly_reuse,
        )


def require_unified_snapshot_algorithm(evaluation_month: Any, *, saved_io_full: bool) -> None:
    """Keep new operating months on the approved average-purchase algorithm."""
    month = str(evaluation_month or "").strip()
    if len(month) == 6 and month.isdigit() and month >= "202611" and not saved_io_full:
        raise SnapshotContractError("202611 onward Snapshot generation requires --dashboard-profile --saved-io-full")


def calculate_saved_io_three_grades_shadow(
    *, plan: FrequencySnapshotPlan, created_by: str, timeout_seconds: int = 120,
    query_executor: QueryExecutor | None = None, progress_reporter: ProgressReporter | None = None,
    classification_authority_loader: Callable[..., Any] = load_effective_company_classification_authority,
    execute: bool = False,
) -> dict[str, Any]:
    """Reuse the two-source grade calculation without creating or publishing a Snapshot."""
    if query_executor is None and not execute:
        raise SnapshotContractError("live Saved I/O Shadow requires explicit execution approval")
    io_scope = resolve_saved_io_scope(plan.io_gu_codes, confirmed_trade_roles=True)
    key = selected_io_snapshot_key(plan, io_scope)
    with frequency_snapshot_generation_guard(plan, key=key):
        return _generate_frequency_snapshot_draft_locked(
            plan=plan, created_by=created_by, timeout_seconds=timeout_seconds,
            query_executor=query_executor, progress_reporter=progress_reporter,
            classification_authority_loader=classification_authority_loader,
            io_scope=io_scope, shadow_only=True,
        )
