from __future__ import annotations

import re
import time
import logging
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

import pandas as pd
from sqlalchemy import text

from app.db.mssql_client import get_current_company_id, get_engine, set_current_company_id
from app.services.dashboard_inventory_frequency_snapshot import (
    ALGORITHM_VERSION,
    SCHEMA_VERSION,
    SNAPSHOT_TYPE,
    SnapshotContractError,
    build_frequency_snapshot_payload_from_aggregates,
    completed_month_basis,
    scope_fingerprint,
    snapshot_key_from_payload,
    validate_frequency_snapshot_payload,
)
from app.services.ssai_snapshot_repository import (
    SNAPSHOT_STATUS_MISSING,
    SNAPSHOT_STATUS_STALE,
    SnapshotKey,
    SnapshotReadResult,
)
from app.services.sql_server_snapshot_repository import SqlServerSnapshotRepository


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
    erp_sql_call_count: int = 2
    analytics_write_plan: str = "draft manifest 1 + immutable payload 1; approval/publish 0"


@dataclass(frozen=True)
class DashboardProfileStockScope:
    """Exact stored Dashboard stock scope accepted for a snapshot operation."""

    company_id: int
    stock_codes: tuple[str, ...]
    profile_status: str
    scope_source: str


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

    stored_scope = normalize_stock_scope(
        normalize_company_default_conditions(profile).get("stock_cd_list")
    )
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
    )


def build_frequency_snapshot_plan(*, company_id: Any, evaluation_month: Any, stock_codes: Sequence[Any] | None) -> FrequencySnapshotPlan:
    try:
        normalized_company = int(company_id)
    except (TypeError, ValueError) as exc:
        raise SnapshotContractError("company_id must be an existing numeric company id") from exc
    if normalized_company <= 0:
        raise SnapshotContractError("company_id must be positive")
    basis = completed_month_basis(evaluation_month)
    return FrequencySnapshotPlan(
        company_id=normalized_company,
        evaluation_month=basis.evaluation_month,
        basis_from=basis.basis_from,
        basis_to=basis.basis_to,
        basis_months=basis.months,
        stock_codes=normalize_stock_scope(stock_codes),
    )


def frequency_snapshot_key(plan: FrequencySnapshotPlan) -> SnapshotKey:
    """Return the immutable repository key for one Dashboard frequency scope."""
    return SnapshotKey(
        company_id=str(plan.company_id),
        snapshot_type=SNAPSHOT_TYPE,
        evaluation_month=plan.evaluation_month,
        scope_fingerprint=scope_fingerprint(plan.stock_codes),
        schema_version=SCHEMA_VERSION,
        algorithm_version=ALGORITHM_VERSION,
    )


_FREQUENCY_READ_CACHE_TTL_SECONDS = 300.0
_frequency_read_cache: dict[SnapshotKey, tuple[float, SnapshotReadResult]] = {}


def _snapshot_exception_code(exc: Exception) -> str:
    """Return only an exception class and optional SQLSTATE, never its text."""
    sqlstate = ""
    args = getattr(exc, "args", ()) or ()
    if args and isinstance(args[0], str):
        candidate = args[0].strip().upper()
        if len(candidate) == 5 and candidate.isalnum():
            sqlstate = candidate
    return f"{type(exc).__name__}{':' + sqlstate if sqlstate else ''}"


def read_approved_frequency_snapshot(
    *,
    company_id: Any,
    evaluation_month: Any,
    stock_codes: Sequence[Any] | None,
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
        )
    except SnapshotContractError as exc:
        return SnapshotReadResult(status=SNAPSHOT_STATUS_MISSING, reason=str(exc))
    key = frequency_snapshot_key(plan)
    now = time.monotonic()
    cached = _frequency_read_cache.get(key)
    cache_age_ms = int((now - cached[0]) * 1000) if cached else 0
    log.info(
        "[dashboard.snapshot.reader] stage=key_cache_lookup company_id=%s evaluation_month=%s stock_code_count=%s scope_fingerprint=%s schema_version=%s algorithm_version=%s cache_hit=%s cache_age_ms=%s cached_status=%s",
        key.company_id,
        key.evaluation_month,
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
        repo = repository or SqlServerSnapshotRepository(
            payload_validator=lambda candidate, expected_key: validate_frequency_snapshot_payload(
                candidate,
                expected_key=expected_key,
            )
        )
        result = repo.read(key)
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
        _frequency_read_cache[key] = (now, result)
    elif repository is None:
        _frequency_read_cache.pop(key, None)
    log.info(
        "[dashboard.snapshot.reader] stage=reader_total company_id=%s evaluation_month=%s stock_code_count=%s scope_fingerprint=%s status=%s generation_no=%s payload_bytes=%s reason_code=%s elapsed_ms=%s",
        key.company_id,
        key.evaluation_month,
        len(plan.stock_codes),
        key.scope_fingerprint[:12],
        result.status,
        result.generation_no if result.generation_no is not None else "",
        0,
        "none" if result.status == "ready" else str(result.status),
        int((time.perf_counter() - started) * 1000),
    )
    return result


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


def _aggregate_sql(base_rows_sql: str) -> str:
    return f"""
WITH BaseRows AS (
    {base_rows_sql}
), Classified AS (
    SELECT *,
        CASE WHEN io_gcode = '0012' AND LEN(io_tcode) = 3
                  AND io_tcode NOT LIKE '%[^0-9]%'
                  AND TRY_CONVERT(int, io_tcode) BETWEEN 500 AND 599 THEN 1 ELSE 0 END AS is_normal,
        CASE WHEN io_gcode = '0012' AND LEN(io_tcode) = 3
                  AND io_tcode NOT LIKE '%[^0-9]%'
                  AND TRY_CONVERT(int, io_tcode) BETWEEN 600 AND 699 THEN 1 ELSE 0 END AS is_return
    FROM BaseRows
), NormalPositive AS (
    SELECT * FROM Classified WHERE is_normal = 1 AND outbound_quantity > 0
), ValidNormalPositive AS (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY outbound_date, vendor_code, outbound_seq, product_code, stock_code, outbound_quantity
        ORDER BY outbound_date
    ) AS exact_duplicate_rank
    FROM NormalPositive
    WHERE NULLIF(outbound_date, '') IS NOT NULL AND NULLIF(vendor_code, '') IS NOT NULL
      AND NULLIF(outbound_seq, '') IS NOT NULL AND NULLIF(product_code, '') IS NOT NULL
      AND NULLIF(stock_code, '') IS NOT NULL
      AND outbound_quantity = FLOOR(outbound_quantity)
), UniqueNormalEvents AS (
    SELECT * FROM ValidNormalPositive WHERE exact_duplicate_rank = 1
), EventMappingCounts AS (
    SELECT outbound_date, vendor_code, outbound_seq, COUNT_BIG(*) AS mapping_count
    FROM UniqueNormalEvents
    GROUP BY outbound_date, vendor_code, outbound_seq
), AcceptedEvents AS (
    SELECT E.*
    FROM UniqueNormalEvents AS E
    INNER JOIN EventMappingCounts AS M
        ON M.outbound_date = E.outbound_date AND M.vendor_code = E.vendor_code AND M.outbound_seq = E.outbound_seq
    WHERE M.mapping_count = 1
), Monthly AS (
    SELECT LEFT(E.outbound_date, 6) AS [month], E.product_code, E.stock_code,
        COUNT_BIG(*) AS occurrence_count, SUM(E.outbound_quantity) AS outbound_quantity,
        COUNT(DISTINCT E.outbound_date) AS outbound_day_count
    FROM AcceptedEvents AS E
    GROUP BY LEFT(E.outbound_date, 6), E.product_code, E.stock_code
), Diagnostics AS (
    SELECT
        (SELECT COUNT_BIG(*) FROM BaseRows) AS source_row_count,
        (SELECT COUNT_BIG(*) FROM AcceptedEvents) AS normal_positive_accepted_row_count,
        (SELECT COUNT_BIG(*) FROM ValidNormalPositive WHERE exact_duplicate_rank > 1) AS normal_positive_duplicate_row_count,
        (SELECT COUNT_BIG(*) FROM UniqueNormalEvents AS E INNER JOIN EventMappingCounts AS M
          ON M.outbound_date = E.outbound_date AND M.vendor_code = E.vendor_code AND M.outbound_seq = E.outbound_seq
          WHERE M.mapping_count > 1) AS normal_positive_conflicting_row_count,
        (SELECT COUNT_BIG(*) FROM NormalPositive WHERE NULLIF(outbound_date, '') IS NULL OR NULLIF(vendor_code, '') IS NULL OR NULLIF(outbound_seq, '') IS NULL OR NULLIF(product_code, '') IS NULL OR NULLIF(stock_code, '') IS NULL) AS normal_positive_missing_key_row_count,
        (SELECT COUNT_BIG(*) FROM NormalPositive WHERE NULLIF(outbound_date, '') IS NOT NULL AND NULLIF(vendor_code, '') IS NOT NULL AND NULLIF(outbound_seq, '') IS NOT NULL AND NULLIF(product_code, '') IS NOT NULL AND NULLIF(stock_code, '') IS NOT NULL AND outbound_quantity <> FLOOR(outbound_quantity)) AS normal_positive_nonintegral_row_count,
        (SELECT COUNT_BIG(*) FROM Classified WHERE is_normal = 1 AND outbound_quantity <= 0) AS normal_nonpositive_row_count,
        (SELECT COUNT_BIG(*) FROM Classified WHERE is_return = 1 AND outbound_quantity > 0) AS return_positive_row_count,
        (SELECT COUNT_BIG(*) FROM Classified WHERE is_return = 1 AND outbound_quantity <= 0) AS return_nonpositive_row_count,
        (SELECT COUNT_BIG(*) FROM Classified WHERE is_normal = 0 AND is_return = 0) AS other_tcode_row_count,
        (SELECT COUNT_BIG(*) FROM NormalPositive) AS normal_positive_row_count,
        (SELECT COUNT_BIG(*) FROM AcceptedEvents) AS distinct_normal_event_count,
        (SELECT COUNT_BIG(*) FROM EventMappingCounts WHERE mapping_count > 1) AS conflicting_event_count
)
SELECT 'monthly' AS row_kind, M.[month], M.product_code, M.stock_code,
    M.occurrence_count, M.outbound_quantity, M.outbound_day_count,
    D.source_row_count, D.normal_positive_accepted_row_count, D.normal_positive_duplicate_row_count,
    D.normal_positive_conflicting_row_count, D.normal_positive_missing_key_row_count,
    D.normal_positive_nonintegral_row_count, D.normal_nonpositive_row_count,
    D.return_positive_row_count, D.return_nonpositive_row_count, D.other_tcode_row_count,
    D.normal_positive_row_count, D.distinct_normal_event_count, D.conflicting_event_count
FROM Monthly AS M CROSS JOIN Diagnostics AS D
UNION ALL
SELECT 'summary', '', '', '', 0, 0, 0,
    D.source_row_count, D.normal_positive_accepted_row_count, D.normal_positive_duplicate_row_count,
    D.normal_positive_conflicting_row_count, D.normal_positive_missing_key_row_count,
    D.normal_positive_nonintegral_row_count, D.normal_nonpositive_row_count,
    D.return_positive_row_count, D.return_nonpositive_row_count, D.other_tcode_row_count,
    D.normal_positive_row_count, D.distinct_normal_event_count, D.conflicting_event_count
FROM Diagnostics AS D
ORDER BY row_kind, [month], product_code, stock_code
""".strip()


def outbound_monthly_aggregate_sql(plan: FrequencySnapshotPlan) -> tuple[str, dict[str, Any]]:
    binds: dict[str, Any] = {"basis_from": plan.basis_from, "basis_to": plan.basis_to}
    stock_clause = ""
    if plan.stock_codes:
        names: list[str] = []
        for index, code in enumerate(plan.stock_codes):
            key = f"stock_{index}"
            binds[key] = code
            names.append(f":{key}")
        stock_clause = "AND LTRIM(RTRIM(O.Rd12_Stock_Cd_Gcode)) = '0018'\n      AND LTRIM(RTRIM(O.Rd12_Stock_Cd)) IN (" + ", ".join(names) + ")"
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
WHERE O.Rd12_Out_YyMmDd >= :basis_from AND O.Rd12_Out_YyMmDd <= :basis_to
  {stock_clause}
""".strip()
    return _aggregate_sql(base), binds


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


def generate_frequency_snapshot_draft(*, plan: FrequencySnapshotPlan, created_by: str, timeout_seconds: int = 120, query_executor: QueryExecutor | None = None, repository: Any | None = None, force: bool = False, progress_reporter: ProgressReporter | None = None) -> dict[str, Any]:
    actor = str(created_by or "").strip()
    if not actor:
        raise SnapshotContractError("created_by is required")
    query = query_executor or _query_company_df
    report = progress_reporter or (lambda _message: None)
    universe_sql, universe_binds = product_universe_sql()
    report("제품 조회 중")
    universe_df = query(plan.company_id, universe_sql, universe_binds, timeout_seconds)
    if not isinstance(universe_df, pd.DataFrame) or "product_code" not in universe_df.columns:
        raise SnapshotContractError("product universe query returned an invalid shape")
    product_codes = sorted({str(value).strip() for value in universe_df["product_code"].tolist() if str(value).strip()})
    if not product_codes:
        raise SnapshotContractError("official Rddbc040 product universe is empty")
    aggregate_sql, aggregate_binds = outbound_monthly_aggregate_sql(plan)
    report("출고 집계 중")
    monthly_rows, diagnostics = _aggregate_result(query(plan.company_id, aggregate_sql, aggregate_binds, timeout_seconds))
    report("등급 계산 중")
    payload = build_frequency_snapshot_payload_from_aggregates(
        company_id=plan.company_id, evaluation_month=plan.evaluation_month, monthly_rows=monthly_rows,
        product_codes=product_codes, stock_codes=plan.stock_codes, source_watermark=None,
        source_watermark_status="unverified", source_diagnostics=diagnostics,
    )
    key = snapshot_key_from_payload(payload)
    repo = repository or SqlServerSnapshotRepository(
        payload_validator=lambda candidate, expected_key: validate_frequency_snapshot_payload(candidate, expected_key=expected_key)
    )
    report("draft 저장 중")
    draft = repo.publish(key, payload, created_by=actor, force=bool(force))
    blocked = repo.read(key)
    if blocked.status != "unapproved":
        raise SnapshotContractError("draft generation was readable before manual approval")
    return {"plan": plan, "payload": payload, "draft": draft, "read_status": blocked.status}
