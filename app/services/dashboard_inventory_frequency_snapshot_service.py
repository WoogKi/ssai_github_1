from __future__ import annotations

import calendar
import hashlib
import os
import re
import tempfile
import time
import logging
from collections import defaultdict
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass
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
    EXTENDED_ALGORITHM_VERSION,
    EXTENDED_RELATIONAL_FREQUENCY_REPRESENTATION,
    EXTENDED_SCHEMA_VERSION,
    PRODUCT_STATISTICS_ALGORITHM_VERSION,
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


def frequency_snapshot_read_keys(plan: FrequencySnapshotPlan) -> tuple[SnapshotKey, SnapshotKey, SnapshotKey]:
    """Prefer product statistics, then lifecycle v2, without invalidating v1."""
    return (
        frequency_snapshot_key(plan, product_statistics=True),
        frequency_snapshot_key(plan, extended=True),
        frequency_snapshot_key(plan),
    )


@contextmanager
def frequency_snapshot_generation_guard(
    plan: FrequencySnapshotPlan,
    *,
    lock_root: Path | None = None,
) -> Iterable[SnapshotKey]:
    """Serialize one company/month/scope generation across local CLI processes.

    A stale lock intentionally remains fail-closed for an operator to inspect;
    this prevents a crashed run from silently overlapping a later retry.
    """
    key = frequency_snapshot_key(plan, product_statistics=True)
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
    return resolver(requested_key, available_through=as_of_date)


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
        f"INNER JOIN dbo.Rddbc040 AS {product_alias} ON {product_alias}.Rd04_Physic_Cd = O.Rd12_Physic_Cd",
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
    io_number = sql_safe_int("io_tcode")
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
             AND {io_number} BETWEEN 500 AND 599 THEN 1 ELSE 0 END AS is_normal,
      CASE WHEN io_gcode='0012' AND LEN(io_tcode)=3 AND io_tcode NOT LIKE '%[^0-9]%'
             AND {io_number} BETWEEN 600 AND 699 THEN 1 ELSE 0 END AS is_return
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
) -> Iterable[pd.DataFrame]:
    """Read one ERP statement incrementally without retaining its full event result."""
    previous_company_id = get_current_company_id()
    set_current_company_id(company_id)
    try:
        with get_engine().connect() as conn:
            raw = getattr(conn.connection, "driver_connection", conn.connection)
            if hasattr(raw, "timeout"):
                raw.timeout = max(1, int(timeout_seconds))
            chunks = pd.read_sql_query(text(sql), conn, params=dict(params), chunksize=max(1, int(chunk_rows)))
            for chunk in chunks:
                if not isinstance(chunk, pd.DataFrame):
                    raise SnapshotContractError("outbound event stream returned an invalid chunk")
                yield chunk
    finally:
        set_current_company_id(previous_company_id)


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
    frequency_chunks: list[pd.DataFrame] = []
    paid_quantities: dict[str, int] = defaultdict(int)
    return_statistics: dict[str, dict[str, Any]] = {}
    sales_prices: dict[str, dict[str, Any]] = {}
    for chunk in chunks:
        if not isinstance(chunk, pd.DataFrame) or "row_kind" not in chunk.columns:
            raise SnapshotContractError("product statistics event stream is invalid")
        kinds = chunk["row_kind"].fillna("").astype(str)
        frequency = chunk[kinds.isin(("event", "diagnostics"))].copy()
        if not frequency.empty:
            frequency_chunks.append(frequency)
        for record in chunk[~kinds.isin(("event", "diagnostics"))].to_dict("records"):
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
        for record in chunk[kinds == "event"].to_dict("records"):
            if _event_grain_int(record.get("mapping_count"), field="mapping_count") == 1:
                code = str(record.get("product_code") or "").strip()
                try:
                    paid_quantity = Decimal(str(record.get("paid_quantity") or 0).strip())
                except (InvalidOperation, ValueError):
                    paid_quantity = Decimal(0)
                if paid_quantity > 0 and paid_quantity == paid_quantity.to_integral_value():
                    paid_quantities[code] += int(paid_quantity)
    if not frequency_chunks:
        raise SnapshotContractError("product statistics stream returned no frequency rows")
    monthly, diagnostics, days, customers = _aggregate_extended_event_grain_chunks(frequency_chunks)
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
    work = lifecycle_df.copy()
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


def _generate_frequency_snapshot_draft_locked(*, plan: FrequencySnapshotPlan, created_by: str, timeout_seconds: int = 120, query_executor: QueryExecutor | None = None, repository: Any | None = None, force: bool = False, progress_reporter: ProgressReporter | None = None, classification_authority_loader: Callable[..., Any] = load_effective_company_classification_authority) -> dict[str, Any]:
    actor = str(created_by or "").strip()
    if not actor:
        raise SnapshotContractError("created_by is required")
    query = query_executor or _query_company_df
    report = progress_reporter or (lambda _message: None)
    evaluation_last_day = date(
        int(plan.evaluation_month[:4]),
        int(plan.evaluation_month[4:]),
        calendar.monthrange(int(plan.evaluation_month[:4]), int(plan.evaluation_month[4:]))[1],
    )
    lifecycle_cutoff = min(evaluation_last_day, kst_today()).strftime("%Y%m%d")
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
    )
    report("제품 및 최초 정상 입고 조회 중")
    lifecycle_df = query(plan.company_id, lifecycle_sql, lifecycle_binds, timeout_seconds)
    if not isinstance(lifecycle_df, pd.DataFrame) or "product_code" not in lifecycle_df.columns:
        raise SnapshotContractError("product lifecycle query returned an invalid shape")
    report("출고 집계 중")
    aggregate_sql, aggregate_binds = product_statistics_event_stream_sql(plan)
    if query_executor is None:
        monthly_rows, diagnostics, product_day_counts, product_customer_counts, outbound_paid_quantities, return_statistics, sales_prices = _aggregate_product_statistics_event_chunks(
            _query_company_chunks(plan.company_id, aggregate_sql, aggregate_binds, timeout_seconds)
        )
    else:
        monthly_rows, diagnostics, product_day_counts, product_customer_counts, outbound_paid_quantities, return_statistics, sales_prices = _aggregate_product_statistics_event_chunks(
            (query(plan.company_id, aggregate_sql, aggregate_binds, timeout_seconds),)
        )
    product_codes, universe_diagnostics = _select_snapshot_product_universe(lifecycle_df, monthly_rows)
    if not product_codes:
        raise SnapshotContractError("dashboard profile product universe is empty")
    product_code_set = set(product_codes)
    monthly_rows = [row for row in monthly_rows if str(row.get("product_code") or "").strip() in product_code_set]
    product_day_counts = {code: value for code, value in product_day_counts.items() if code in product_code_set}
    product_customer_counts = {code: value for code, value in product_customer_counts.items() if code in product_code_set}
    outbound_paid_quantities = {code: value for code, value in outbound_paid_quantities.items() if code in product_code_set}
    return_statistics = {code: value for code, value in return_statistics.items() if code in product_code_set}
    sales_prices = {code: value for code, value in sales_prices.items() if code in product_code_set}
    for price in sales_prices.values():
        price["status"] = _price_status(plan.evaluation_month, price.get("basis_month"))
    first_inbound_months = {
        str(row.get("product_code") or "").strip(): row.get("first_normal_inbound_month")
        for row in lifecycle_df.to_dict("records")
        if str(row.get("product_code") or "").strip() in product_code_set
    }
    purchase_prices = {
        str(row.get("product_code") or "").strip(): {
            "unit_price": row.get("avg_purchase_unit_cost"),
            "basis_month": row.get("purchase_price_basis_month"),
            "status": _price_status(plan.evaluation_month, row.get("purchase_price_basis_month")),
        }
        for row in lifecycle_df.to_dict("records")
        if str(row.get("product_code") or "").strip() in product_code_set
    }
    classification_authority = classification_authority_loader(
        company_id=plan.company_id,
        as_of=date(int(lifecycle_cutoff[:4]), int(lifecycle_cutoff[4:6]), int(lifecycle_cutoff[6:])),
    )
    adjustment_only_products: set[str] = set()
    profitability_unavailable_products: set[str] = set()
    for row in lifecycle_df.to_dict("records"):
        code = str(row.get("product_code") or "").strip()
        if code not in product_code_set:
            continue
        targets = ProductClassificationTargets(
            product_group_keys=tuple(value for value in (str(row.get("product_group_key") or "").strip(),) if value and not value.endswith(":")),
            product_di_keys=tuple(value for value in (str(row.get("product_di_key") or "").strip(),) if value and not value.endswith(":")),
            product_class_keys=tuple(value for value in (str(row.get("product_class_key") or "").strip(),) if value and not value.endswith(":")),
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
        profile_fingerprint=plan.profile_fingerprint,
    )
    key = relational_snapshot.key
    repo = repository or _company_snapshot_repository(int(plan.company_id))
    report("draft 저장 중")
    publish_relational = getattr(repo, "publish_relational", None)
    if not callable(publish_relational):
        raise SnapshotContractError("repository does not support relational frequency snapshots")
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
    }


def generate_frequency_snapshot_draft(*, plan: FrequencySnapshotPlan, created_by: str, timeout_seconds: int = 120, query_executor: QueryExecutor | None = None, repository: Any | None = None, force: bool = False, progress_reporter: ProgressReporter | None = None, classification_authority_loader: Callable[..., Any] = load_effective_company_classification_authority) -> dict[str, Any]:
    """Generate one exact snapshot draft while preventing an overlapping run."""
    with frequency_snapshot_generation_guard(plan):
        return _generate_frequency_snapshot_draft_locked(
            plan=plan,
            created_by=created_by,
            timeout_seconds=timeout_seconds,
            query_executor=query_executor,
            repository=repository,
            force=force,
            progress_reporter=progress_reporter,
            classification_authority_loader=classification_authority_loader,
        )
