"""Immutable monthly R110/R120 evidence in the existing Analytics manifest."""

from __future__ import annotations

import hashlib
import gzip
import json
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Iterable

from app.services.dashboard_inventory_frequency_snapshot import SnapshotContractError
from app.services.monthly_io_components import (
    MONTHLY_IO_ALGORITHM_VERSION, MONTHLY_IO_SCHEMA_VERSION, MonthlyIoFact,
    fact_checksum, whole_checksum,
)


FACT_COLUMNS = (
    "source_kind", "row_kind", "product_code", "stock_code", "io_code",
    "bucket_unit_price", "raw_product_code", "source_row_count",
    "normal_event_count", "return_event_count", "eligible_price_row_count",
    "source_paid_quantity", "source_bonus_quantity", "source_original_supply_amount",
    "source_final_supply_amount", "normal_event_quantity", "return_quantity",
    "return_supply_amount", "eligible_paid_quantity", "eligible_supply_amount",
    "last_purchase_date", "last_purchase_seq", "last_vendor_code", "last_tie_count",
    "last_tie_conflict",
)


@dataclass(frozen=True)
class MonthlyIoMaterialization:
    company_id: int
    basis_month: str
    profile_fingerprint: str
    scope_fingerprint: str
    product_codes: tuple[str, ...]
    stock_codes: tuple[str, ...]
    facts: tuple[MonthlyIoFact, ...]

    @property
    def checksum(self) -> str:
        return whole_checksum(self.facts)

    @property
    def product_universe_fingerprint(self) -> str:
        payload = json.dumps(sorted(set(self.product_codes)), ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def validate(self) -> None:
        if (self.company_id <= 0 or len(self.basis_month) != 6 or not self.basis_month.isdigit()
                or len(self.profile_fingerprint) != 64 or len(self.scope_fingerprint) != 64
                or not self.product_codes or not self.stock_codes
                or len(set(self.product_codes)) != len(self.product_codes)):
            raise SnapshotContractError("monthly IO identity is incomplete")
        allowed = set(self.product_codes)
        for fact in self.facts:
            if (fact.basis_month != self.basis_month or fact.product_code not in allowed
                    or (fact.row_kind == "BASE" and fact.stock_code not in self.stock_codes)
                    or (fact.source_kind == "R110" and fact.row_kind not in {"BASE", "PRICE_BUCKET", "PRICE_TAIL"})
                    or (fact.source_kind == "R120" and fact.row_kind != "BASE")):
                raise SnapshotContractError("monthly IO fact escapes its scope")
        self.checksum


@dataclass(frozen=True)
class MonthlyIoStored:
    manifest_id: int
    generation_no: int
    status: str
    approval_status: str
    source_verified_at: datetime | None
    materialization: MonthlyIoMaterialization


@dataclass(frozen=True)
class MonthlyIoPriceEvidence:
    manifest_id: int
    basis_month: str
    source_verified_at: datetime | None
    facts: tuple[MonthlyIoFact, ...]


class SqlServerMonthlyIoRepository:
    def __init__(self, reader_connection_factory: Callable[[], Any], writer_connection_factory: Callable[[], Any]):
        self._read = reader_connection_factory
        self._write = writer_connection_factory

    @staticmethod
    def _identity(item: MonthlyIoMaterialization) -> tuple[Any, ...]:
        return (str(item.company_id), item.basis_month, item.scope_fingerprint,
                MONTHLY_IO_SCHEMA_VERSION, MONTHLY_IO_ALGORITHM_VERSION)

    def save_draft(self, item: MonthlyIoMaterialization, *, created_by: str) -> MonthlyIoStored:
        item.validate()
        actor = str(created_by or "").strip()
        if not actor:
            raise SnapshotContractError("monthly IO actor is required")
        conn = self._write()
        try:
            cursor = conn.cursor()
            latest = cursor.execute(
                """SELECT TOP 1 generation_no FROM snapshot.frequency_month_manifest WITH (UPDLOCK,HOLDLOCK)
                   WHERE company_id=? AND basis_month=? AND scope_fingerprint=?
                     AND schema_version=? AND algorithm_version=? ORDER BY generation_no DESC""",
                *self._identity(item),
            ).fetchone()
            generation = int(latest[0]) + 1 if latest else 1
            source_rows = sum(f.source_row_count for f in item.facts if f.row_kind == "BASE")
            events = sum(f.normal_event_count + f.return_event_count for f in item.facts if f.row_kind == "BASE")
            from calendar import monthrange
            year, month = int(item.basis_month[:4]), int(item.basis_month[4:])
            month_end = f"{item.basis_month}{monthrange(year, month)[1]:02d}"
            inserted = cursor.execute(
                """INSERT INTO snapshot.frequency_month_manifest (
                     company_id,basis_month,basis_from,basis_to,scope_fingerprint,
                     product_universe_fingerprint,product_count,schema_version,algorithm_version,
                     generation_no,status,approval_status,source_watermark,source_watermark_status,
                     source_fingerprint,source_row_count,event_count,fact_count,lifecycle_count,
                     checksum,created_by,profile_fingerprint,operating_schema_version,
                     operating_algorithm_version,source_verified_at)
                   OUTPUT INSERTED.manifest_id, INSERTED.source_verified_at
                   VALUES (?,?,?,?,?,?,?,?,?,?,'draft','pending',NULL,'verified_at_creation',
                           ?,?,?,?,0,?,?,?,?,?,SYSUTCDATETIME())""",
                str(item.company_id), item.basis_month, f"{item.basis_month}01", month_end,
                item.scope_fingerprint, item.product_universe_fingerprint, len(item.product_codes),
                MONTHLY_IO_SCHEMA_VERSION, MONTHLY_IO_ALGORITHM_VERSION, generation,
                item.checksum, source_rows, events, len(item.facts), item.checksum, actor,
                item.profile_fingerprint, "2.1",
                "outbound_frequency_product_v3+saved_io_avg_purchase_v1",
            ).fetchone()
            manifest_id = int(inserted[0])
            cursor.executemany(
                "INSERT INTO snapshot.frequency_month_scope_stock(manifest_id,stock_code) VALUES (?,?)",
                [(manifest_id, code) for code in item.stock_codes],
            )
            sql_columns = ("manifest_id", "company_id", "basis_month", "io_gcode", *FACT_COLUMNS, "row_checksum")
            placeholders = ",".join("?" for _ in sql_columns)
            statement = f"INSERT INTO snapshot.monthly_io_fact ({','.join(sql_columns)}) VALUES ({placeholders})"
            for start in range(0, len(item.facts), 5000):
                batch = item.facts[start:start + 5000]
                values = [
                    (manifest_id, str(item.company_id), item.basis_month, "0012",
                     *(getattr(fact, column) for column in FACT_COLUMNS), fact_checksum(fact))
                    for fact in batch
                ]
                if values:
                    if hasattr(cursor, "fast_executemany"):
                        cursor.fast_executemany = True
                    cursor.executemany(statement, values)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return MonthlyIoStored(manifest_id, generation, "draft", "pending", inserted[1], item)

    def read_pending_exact(self, reference: MonthlyIoMaterialization) -> MonthlyIoStored | None:
        conn = self._read()
        try:
            cursor = conn.cursor()
            row = cursor.execute(
                """SELECT TOP 1 generation_no FROM snapshot.frequency_month_manifest
                   WHERE company_id=? AND basis_month=? AND scope_fingerprint=?
                     AND schema_version=? AND algorithm_version=?
                     AND profile_fingerprint=? AND status='draft' AND approval_status='pending'
                     AND checksum=? ORDER BY generation_no DESC""",
                *self._identity(reference), reference.profile_fingerprint, reference.checksum,
            ).fetchone()
            return self._load(cursor, reference, int(row[0])) if row else None
        finally:
            conn.close()

    def read_generation(self, reference: MonthlyIoMaterialization, generation_no: int) -> MonthlyIoStored:
        conn = self._read()
        try:
            return self._load(conn.cursor(), reference, generation_no)
        finally:
            conn.close()

    def read_current(self, reference: MonthlyIoMaterialization) -> MonthlyIoStored:
        conn = self._read()
        try:
            cursor = conn.cursor()
            row = cursor.execute(
                """SELECT TOP 1 generation_no FROM snapshot.frequency_month_manifest
                   WHERE company_id=? AND basis_month=? AND scope_fingerprint=?
                     AND schema_version=? AND algorithm_version=?
                     AND profile_fingerprint=? AND status='published' AND approval_status='approved'
                   ORDER BY generation_no DESC""",
                *self._identity(reference), reference.profile_fingerprint,
            ).fetchone()
            if not row:
                raise LookupError("approved monthly IO generation is missing")
            return self._load(cursor, reference, int(row[0]))
        finally:
            conn.close()

    def read_current_for_scope(self, *, company_id: int, basis_month: str,
                               profile_fingerprint: str, scope_fingerprint: str,
                               stock_codes: tuple[str, ...]) -> MonthlyIoStored:
        """Validate an approved fact without re-reading ERP product master rows."""
        reference = MonthlyIoMaterialization(
            company_id, basis_month, profile_fingerprint, scope_fingerprint,
            (), stock_codes, (),
        )
        return self.read_current(reference)

    def read_approved_manifest_for_scope(self, *, company_id: int, basis_month: str,
                                         profile_fingerprint: str, scope_fingerprint: str,
                                         stock_codes: tuple[str, ...]) -> dict[str, Any]:
        """Resolve approved work without fetching any monthly Fact or ERP source rows."""
        reference = MonthlyIoMaterialization(
            company_id, basis_month, profile_fingerprint, scope_fingerprint, (), stock_codes, (),
        )
        conn = self._read()
        try:
            cursor = conn.cursor()
            row = cursor.execute(
                """SELECT TOP 1 manifest_id,generation_no,product_universe_fingerprint,
                          product_count,fact_count,checksum,source_verified_at
                   FROM snapshot.frequency_month_manifest
                   WHERE company_id=? AND basis_month=? AND scope_fingerprint=?
                     AND schema_version=? AND algorithm_version=? AND profile_fingerprint=?
                     AND status='published' AND approval_status='approved'
                   ORDER BY generation_no DESC""",
                *self._identity(reference), profile_fingerprint,
            ).fetchone()
            if row is None:
                raise LookupError("approved monthly IO generation is missing")
            stocks = tuple(str(value[0]) for value in cursor.execute(
                "SELECT stock_code FROM snapshot.frequency_month_scope_stock WHERE manifest_id=? ORDER BY stock_code",
                int(row[0]),
            ).fetchall())
            if stocks != tuple(sorted(stock_codes)):
                raise SnapshotContractError("monthly IO stock scope mismatch")
            return {
                "manifest_id": int(row[0]), "generation_no": int(row[1]),
                "product_universe_fingerprint": str(row[2]), "product_count": int(row[3]),
                "fact_count": int(row[4]), "checksum": str(row[5]),
                "source_verified_at": row[6],
            }
        finally:
            conn.close()

    def list_approved_months_before(self, *, company_id: int, before_month: str,
                                    profile_fingerprint: str, scope_fingerprint: str) -> tuple[str, ...]:
        conn = self._read()
        try:
            rows = conn.cursor().execute(
                """SELECT DISTINCT basis_month FROM snapshot.frequency_month_manifest
                   WHERE company_id=? AND basis_month<? AND profile_fingerprint=?
                     AND scope_fingerprint=? AND schema_version=? AND algorithm_version=?
                     AND status='published' AND approval_status='approved'
                   ORDER BY basis_month""",
                str(company_id), before_month, profile_fingerprint, scope_fingerprint,
                MONTHLY_IO_SCHEMA_VERSION, MONTHLY_IO_ALGORITHM_VERSION,
            ).fetchall()
            return tuple(str(row[0]) for row in rows)
        finally:
            conn.close()

    def read_price_period_for_scope(self, *, company_id: int, months: tuple[str, ...],
                                    first_bucket_month: str, profile_fingerprint: str,
                                    scope_fingerprint: str,
                                    stock_codes: tuple[str, ...],
                                    timings_ms: dict[str, Any] | None = None) -> tuple[MonthlyIoPriceEvidence, ...]:
        """Validate complete approved generations in one pass, returning only R110 prices."""
        if not months or len(months) != len(set(months)) or any(
            len(month) != 6 or not month.isdigit() for month in months
        ):
            raise SnapshotContractError("monthly IO price period is invalid")
        month_marks = ",".join("?" for _ in months)
        started = time.perf_counter()
        conn = self._read()
        try:
            cursor = conn.cursor()
            rows = cursor.execute(
                f"""SELECT manifest_id,basis_month,generation_no,fact_count,checksum,
                           source_verified_at,product_count
                    FROM snapshot.frequency_month_manifest
                    WHERE company_id=? AND basis_month IN ({month_marks})
                      AND profile_fingerprint=? AND scope_fingerprint=?
                      AND schema_version=? AND algorithm_version=?
                      AND status='published' AND approval_status='approved'
                    ORDER BY basis_month,generation_no DESC""",
                str(company_id), *months, profile_fingerprint, scope_fingerprint,
                MONTHLY_IO_SCHEMA_VERSION, MONTHLY_IO_ALGORITHM_VERSION,
            ).fetchall()
            selected: dict[str, Any] = {}
            for row in rows:
                selected.setdefault(str(row[1]), row)
            if set(selected) != set(months):
                raise LookupError("approved monthly IO price period is incomplete")
            ids = tuple(int(selected[month][0]) for month in months)
            id_marks = ",".join("?" for _ in ids)
            stocks: dict[int, list[str]] = {manifest_id: [] for manifest_id in ids}
            for manifest_id, code in cursor.execute(
                f"""SELECT manifest_id,stock_code FROM snapshot.frequency_month_scope_stock
                    WHERE manifest_id IN ({id_marks}) ORDER BY manifest_id,stock_code""", *ids,
            ).fetchall():
                stocks[int(manifest_id)].append(str(code))
            if any(tuple(stocks[manifest_id]) != tuple(sorted(stock_codes)) for manifest_id in ids):
                raise SnapshotContractError("monthly IO stock scope mismatch")
            if timings_ms is not None:
                timings_ms["manifest_scope_ms"] = round((time.perf_counter() - started) * 1000, 3)
            checksum_started = time.perf_counter()

            def read_digests(*, canonical_order: bool) -> dict[int, tuple[int, str | None]]:
                collation = " COLLATE Latin1_General_100_BIN2" if canonical_order else ""
                order_columns = ",".join(
                    name + collation for name in (
                        "source_kind", "row_kind", "product_code", "stock_code", "io_code",
                    )
                ) + ",bucket_unit_price,raw_product_code" + collation
                return {
                    int(row[0]): (int(row[1]), str(row[2]).lower() if row[2] is not None else None)
                    for row in cursor.execute(
                        f"""SELECT manifest_id,COUNT_BIG(*),
                        CONVERT(varchar(64), HASHBYTES('SHA2_256',
                          STRING_AGG(CAST(row_checksum AS varchar(max)), CHAR(10))
                          WITHIN GROUP (ORDER BY {order_columns})), 2)
                        FROM snapshot.monthly_io_fact
                        WHERE company_id=? AND manifest_id IN ({id_marks})
                        GROUP BY manifest_id""", str(company_id), *ids,
                    ).fetchall()
                }

            empty_digest = hashlib.sha256(b"").hexdigest()
            def matches_manifests(digests: dict[int, tuple[int, str | None]]) -> bool:
                return all(
                    digests.get(manifest_id, (0, empty_digest)) ==
                    (int(selected[month][3]), str(selected[month][4]).lower())
                    for month, manifest_id in zip(months, ids)
                )

            digests = read_digests(canonical_order=False)
            checksum_order = "index"
            if not matches_manifests(digests):
                digests = read_digests(canonical_order=True)
                checksum_order = "canonical_fallback"
                if not matches_manifests(digests):
                    raise SnapshotContractError("monthly IO stored whole checksum/count mismatch")
            if timings_ms is not None:
                timings_ms["whole_checksum_ms"] = round((time.perf_counter() - checksum_started) * 1000, 3)
                timings_ms["checksum_order"] = checksum_order
            price_started = time.perf_counter()
            recent_ids = tuple(ids[-3:])
            older_ids = tuple(manifest_id for month, manifest_id in zip(months, ids)
                              if first_bucket_month <= month < months[-3])
            historical_ids = tuple(manifest_id for month, manifest_id in zip(months, ids)
                                   if month < first_bucket_month)
            if len(older_ids) != 3 or not historical_ids:
                raise SnapshotContractError("monthly IO price window is incomplete")
            recent_marks = ",".join("?" for _ in recent_ids)
            older_marks = ",".join("?" for _ in older_ids)
            historical_marks = ",".join("?" for _ in historical_ids)
            six_ids = older_ids + recent_ids
            six_marks = ",".join("?" for _ in six_ids)
            cte = f"""WITH Recent AS (
                SELECT DISTINCT product_code FROM snapshot.monthly_io_fact
                WHERE company_id=? AND manifest_id IN ({recent_marks})
                  AND source_kind='R110' AND row_kind='PRICE_BUCKET'
            ), Six AS (
                SELECT DISTINCT product_code FROM snapshot.monthly_io_fact
                WHERE company_id=? AND manifest_id IN ({six_marks})
                  AND source_kind='R110' AND row_kind='PRICE_BUCKET'
            )"""
            selection = f"""P.company_id=? AND P.source_kind='R110' AND (
                (P.row_kind='PRICE_BUCKET' AND (P.manifest_id IN ({recent_marks}) OR
                  (P.manifest_id IN ({older_marks}) AND NOT EXISTS
                    (SELECT 1 FROM Recent AS R WHERE R.product_code=P.product_code))))
                OR (P.row_kind='PRICE_TAIL' AND P.manifest_id IN ({historical_marks})
                  AND NOT EXISTS (SELECT 1 FROM Six AS S
                    WHERE S.product_code=LTRIM(RTRIM(P.raw_product_code))))
            )"""
            price_binds = (
                str(company_id), *recent_ids, str(company_id), *six_ids,
                str(company_id), *recent_ids, *older_ids, *historical_ids,
            )
            zero_fields = (
                "source_row_count", "normal_event_count", "return_event_count",
                "source_paid_quantity", "source_bonus_quantity", "source_original_supply_amount",
                "source_final_supply_amount", "normal_event_quantity", "return_quantity",
                "return_supply_amount",
            )
            invalid = " OR ".join(f"P.{field}<>0" for field in zero_fields)
            expected_price_rows = int(cursor.execute(
                f"""{cte} SELECT COUNT_BIG(*) FROM snapshot.monthly_io_fact AS P
                    WHERE {selection}""", *price_binds,
            ).fetchone()[0])
            payload = cursor.execute(
                f"""{cte} SELECT COMPRESS((SELECT P.manifest_id AS i,P.basis_month AS m,
                    P.row_kind AS k,P.product_code AS p,P.bucket_unit_price AS u,
                    P.raw_product_code AS r,P.eligible_price_row_count AS c,
                    P.eligible_paid_quantity AS q,P.eligible_supply_amount AS a,
                    P.last_purchase_date AS d,P.last_purchase_seq AS s,
                    P.last_vendor_code AS v,P.last_tie_count AS t,
                    P.last_tie_conflict AS f,P.row_checksum AS h,
                    CASE WHEN P.stock_code<>N'' OR P.io_code<>'000' OR P.io_gcode<>'0012'
                      OR {invalid} THEN 1 ELSE 0 END AS z
                    FROM snapshot.monthly_io_fact AS P WHERE {selection}
                    FOR JSON PATH, INCLUDE_NULL_VALUES))""", *price_binds,
            ).fetchone()[0]
            if payload is None:
                raise SnapshotContractError("monthly IO price payload is missing")
            if timings_ms is not None:
                timings_ms["price_query_execute_ms"] = round((time.perf_counter() - price_started) * 1000, 3)
                timings_ms["price_payload_bytes"] = len(payload)
            try:
                price_rows = json.loads(gzip.decompress(bytes(payload)).decode("utf-16-le"),
                                        parse_float=Decimal)
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                raise SnapshotContractError("monthly IO price payload is invalid") from exc
            if not isinstance(price_rows, list) or len(price_rows) != expected_price_rows:
                raise SnapshotContractError("monthly IO price payload row count mismatch")
            id_to_month = dict(zip(ids, months))
            facts: dict[str, list[MonthlyIoFact]] = {month: [] for month in months}
            validate_started = time.perf_counter()
            for row in price_rows:
                if not isinstance(row, dict) or int(row.get("i", -1)) not in id_to_month:
                    raise SnapshotContractError("monthly IO price payload ownership mismatch")
                month = id_to_month[int(row["i"])]
                if str(row.get("m")) != month:
                    raise SnapshotContractError("monthly IO price payload month mismatch")
                if row.get("z") != 0:
                    raise SnapshotContractError("monthly IO price row has nondefault omitted fields")
                fact = MonthlyIoFact(
                    basis_month=month, source_kind="R110", row_kind=row["k"],
                    product_code=row["p"], stock_code="", io_code="000",
                    bucket_unit_price=row["u"], raw_product_code=row["r"],
                    eligible_price_row_count=row["c"], eligible_paid_quantity=row["q"],
                    eligible_supply_amount=row["a"], last_purchase_date=row["d"],
                    last_purchase_seq=row["s"], last_vendor_code=row["v"],
                    last_tie_count=row["t"], last_tie_conflict=row["f"],
                )
                if fact_checksum(fact) != row["h"]:
                    raise SnapshotContractError("monthly IO price row checksum mismatch")
                facts[month].append(fact)
            validate_elapsed = time.perf_counter() - validate_started
            if any(len({fact.product_code for fact in facts[month]}) > int(selected[month][6])
                   for month in months):
                raise SnapshotContractError("monthly IO price product count exceeds manifest")
            if timings_ms is not None:
                timings_ms["price_rows_ms"] = round((time.perf_counter() - price_started) * 1000, 3)
                timings_ms["price_row_count"] = sum(len(items) for items in facts.values())
                timings_ms["price_row_validation_ms"] = round(validate_elapsed * 1000, 3)
            return tuple(MonthlyIoPriceEvidence(ids[index], month, selected[month][5],
                                                tuple(facts[month]))
                         for index, month in enumerate(months))
        finally:
            conn.close()

    def read_price_current_for_scope(self, *, company_id: int, basis_month: str,
                                     profile_fingerprint: str, scope_fingerprint: str,
                                     stock_codes: tuple[str, ...], row_kind: str) -> MonthlyIoPriceEvidence:
        """Check the full stored digest, then materialize and rehash only needed R110 prices."""
        if row_kind not in {"PRICE_BUCKET", "PRICE_TAIL"}:
            raise SnapshotContractError("monthly price row kind is invalid")
        reference = MonthlyIoMaterialization(
            company_id, basis_month, profile_fingerprint, scope_fingerprint, (), stock_codes, (),
        )
        conn = self._read()
        try:
            cursor = conn.cursor()
            manifest = cursor.execute(
                """SELECT TOP 1 manifest_id,fact_count,checksum,source_verified_at,product_count
                   FROM snapshot.frequency_month_manifest
                   WHERE company_id=? AND basis_month=? AND scope_fingerprint=?
                     AND schema_version=? AND algorithm_version=? AND profile_fingerprint=?
                     AND status='published' AND approval_status='approved'
                   ORDER BY generation_no DESC""",
                *self._identity(reference), profile_fingerprint,
            ).fetchone()
            if not manifest:
                raise LookupError("approved monthly IO generation is missing")
            manifest_id = int(manifest[0])
            stocks = tuple(str(row[0]) for row in cursor.execute(
                "SELECT stock_code FROM snapshot.frequency_month_scope_stock WHERE manifest_id=? ORDER BY stock_code",
                manifest_id,
            ).fetchall())
            if stocks != tuple(sorted(stock_codes)):
                raise SnapshotContractError("monthly IO stock scope mismatch")
            digest_row = cursor.execute(
                """SELECT CONVERT(varchar(64), HASHBYTES('SHA2_256',
                            STRING_AGG(CAST(row_checksum AS varchar(max)), CHAR(10))
                              WITHIN GROUP (ORDER BY source_kind COLLATE Latin1_General_100_BIN2,
                            row_kind COLLATE Latin1_General_100_BIN2,
                            product_code COLLATE Latin1_General_100_BIN2,
                            stock_code COLLATE Latin1_General_100_BIN2,
                            io_code COLLATE Latin1_General_100_BIN2,
                            bucket_unit_price,
                            raw_product_code COLLATE Latin1_General_100_BIN2)), 2)
                   FROM snapshot.monthly_io_fact
                   WHERE manifest_id=? AND company_id=? AND basis_month=?""",
                manifest_id, str(company_id), basis_month,
            ).fetchone()
            fact_count = int(cursor.execute(
                "SELECT COUNT_BIG(*) FROM snapshot.monthly_io_fact WHERE manifest_id=? AND company_id=? AND basis_month=?",
                manifest_id, str(company_id), basis_month,
            ).fetchone()[0])
            digest = hashlib.sha256(b"").hexdigest() if fact_count == 0 else str(digest_row[0]).lower()
            if fact_count != int(manifest[1]) or digest != str(manifest[2]).lower():
                raise SnapshotContractError("monthly IO stored whole checksum/count mismatch")
            price_rows = cursor.execute(
                """SELECT source_kind,row_kind,product_code,stock_code,io_code,bucket_unit_price,
                          raw_product_code,source_row_count,normal_event_count,return_event_count,
                          eligible_price_row_count,source_paid_quantity,source_bonus_quantity,
                          source_original_supply_amount,source_final_supply_amount,
                          normal_event_quantity,return_quantity,return_supply_amount,
                          eligible_paid_quantity,eligible_supply_amount,last_purchase_date,
                          last_purchase_seq,last_vendor_code,last_tie_count,last_tie_conflict,row_checksum
                   FROM snapshot.monthly_io_fact
                   WHERE manifest_id=? AND company_id=? AND basis_month=?
                     AND source_kind='R110' AND row_kind=?""",
                manifest_id, str(company_id), basis_month, row_kind,
            )
            facts: list[MonthlyIoFact] = []
            while batch := price_rows.fetchmany(5000):
                for row in batch:
                    fact = MonthlyIoFact(
                        basis_month=basis_month, **dict(zip(FACT_COLUMNS, row[:-1])),
                    )
                    if str(row[-1]) != fact_checksum(fact):
                        raise SnapshotContractError("monthly IO price row checksum mismatch")
                    facts.append(fact)
            if len({fact.product_code for fact in facts}) > int(manifest[4]):
                raise SnapshotContractError("monthly IO price product count exceeds manifest")
            return MonthlyIoPriceEvidence(manifest_id, basis_month, manifest[3], tuple(facts))
        finally:
            conn.close()

    def _load(self, cursor: Any, reference: MonthlyIoMaterialization, generation_no: int) -> MonthlyIoStored:
        manifest = cursor.execute(
            """SELECT manifest_id,status,approval_status,profile_fingerprint,
                      product_universe_fingerprint,product_count,source_row_count,event_count,
                      fact_count,checksum,source_verified_at
               FROM snapshot.frequency_month_manifest
               WHERE company_id=? AND basis_month=? AND scope_fingerprint=?
                 AND schema_version=? AND algorithm_version=? AND generation_no=?""",
            *self._identity(reference), int(generation_no),
        ).fetchone()
        if not manifest or str(manifest[3]) != reference.profile_fingerprint:
            raise SnapshotContractError("monthly IO manifest/profile mismatch")
        manifest_id = int(manifest[0])
        stocks = tuple(str(row[0]) for row in cursor.execute(
            "SELECT stock_code FROM snapshot.frequency_month_scope_stock WHERE manifest_id=? ORDER BY stock_code",
            manifest_id,
        ).fetchall())
        if stocks != tuple(sorted(reference.stock_codes)):
            raise SnapshotContractError("monthly IO stock scope mismatch")
        rows = cursor.execute(
            """SELECT source_kind,row_kind,product_code,stock_code,io_code,bucket_unit_price,
                      raw_product_code,source_row_count,normal_event_count,return_event_count,
                      eligible_price_row_count,source_paid_quantity,source_bonus_quantity,
                      source_original_supply_amount,source_final_supply_amount,
                      normal_event_quantity,return_quantity,return_supply_amount,
                      eligible_paid_quantity,eligible_supply_amount,last_purchase_date,
                      last_purchase_seq,last_vendor_code,last_tie_count,last_tie_conflict,row_checksum
               FROM snapshot.monthly_io_fact WHERE manifest_id=? AND company_id=? AND basis_month=?""",
            manifest_id, str(reference.company_id), reference.basis_month,
        ).fetchall()
        facts: list[MonthlyIoFact] = []
        for row in rows:
            values = dict(zip(FACT_COLUMNS, row[:-1]))
            if values["row_kind"] == "BASE" and values["bucket_unit_price"] == -1:
                # The in-memory BASE sentinel predates SQL DECIMAL(28,6) scale padding.
                values["bucket_unit_price"] = Decimal(-1)
            fact = MonthlyIoFact(basis_month=reference.basis_month, **values)
            if str(row[-1]) != fact_checksum(fact):
                raise SnapshotContractError("monthly IO row checksum mismatch")
            facts.append(fact)
        inferred_codes = tuple(sorted({fact.product_code for fact in facts}))
        loaded = MonthlyIoMaterialization(
            reference.company_id, reference.basis_month, reference.profile_fingerprint,
            reference.scope_fingerprint, reference.product_codes or inferred_codes,
            reference.stock_codes,
            tuple(facts),
        )
        if loaded.product_codes:
            loaded.validate()
        if ((reference.product_codes and str(manifest[4]) != loaded.product_universe_fingerprint)
                or (reference.product_codes and int(manifest[5]) != len(reference.product_codes))
                or (not reference.product_codes and int(manifest[5]) < len(inferred_codes))
                or int(manifest[6]) != sum(f.source_row_count for f in facts if f.row_kind == "BASE")
                or int(manifest[7]) != sum(f.normal_event_count + f.return_event_count for f in facts if f.row_kind == "BASE")
                or int(manifest[8]) != len(facts) or str(manifest[9]) != loaded.checksum):
            raise SnapshotContractError("monthly IO manifest checksum/count mismatch")
        return MonthlyIoStored(manifest_id, int(generation_no), str(manifest[1]),
                               str(manifest[2]), manifest[10], loaded)

    def approve_checked(self, reference: MonthlyIoMaterialization, generation_no: int, *,
                        expected_checksum: str, approved_by: str, approval_reason: str,
                        exact_confirmed: bool, approval_event: Any | None = None,
                        target_hash: str | None = None) -> MonthlyIoStored:
        if not exact_confirmed or not approved_by.strip() or not approval_reason.strip():
            raise SnapshotContractError("monthly IO approval requires independent Exact evidence")
        stored = self.read_generation(reference, generation_no)
        if (stored.status != "draft" or stored.approval_status != "pending"
                or stored.materialization.checksum != expected_checksum):
            raise SnapshotContractError("monthly IO pending draft checksum mismatch")
        if approval_event is not None:
            if (not target_hash or approval_event.company_id != reference.company_id
                    or approval_event.basis_month != reference.basis_month
                    or approval_event.manifest_id != stored.manifest_id
                    or approval_event.generation_no != stored.generation_no
                    or approval_event.profile_fingerprint != reference.profile_fingerprint
                    or approval_event.whole_checksum != expected_checksum
                    or approval_event.approved_by != approved_by
                    or approval_event.exact_status != "passed"):
                raise SnapshotContractError("monthly IO approval history identity mismatch")
        conn = self._write()
        try:
            cursor = conn.cursor()
            if approval_event is not None:
                if cursor.execute("SELECT OBJECT_ID(N'snapshot.operation_event',N'U')").fetchone()[0] is None:
                    raise SnapshotContractError("monthly IO approval history migration is missing")
            cursor.execute(
                """UPDATE snapshot.frequency_month_manifest SET status='superseded',
                      superseded_at=SYSUTCDATETIME()
                   WHERE company_id=? AND basis_month=? AND scope_fingerprint=?
                     AND schema_version=? AND algorithm_version=?
                     AND profile_fingerprint=? AND status='published' AND manifest_id<>?""",
                *self._identity(reference), reference.profile_fingerprint, stored.manifest_id,
            )
            cursor.execute(
                """UPDATE snapshot.frequency_month_manifest SET status='published',
                      approval_status='approved',approved_at=SYSUTCDATETIME(),
                      approved_by=?,approval_reason=?,published_at=SYSUTCDATETIME()
                   WHERE company_id=? AND manifest_id=? AND status='draft'
                     AND approval_status='pending' AND checksum=?""",
                approved_by.strip(), approval_reason.strip(), str(reference.company_id),
                stored.manifest_id, expected_checksum,
            )
            if cursor.rowcount != 1:
                raise SnapshotContractError("monthly IO approval lost its pending draft")
            if approval_event is not None:
                from app.services.snapshot_operation_history import insert_event_on_cursor

                if not insert_event_on_cursor(cursor, approval_event, target_hash=target_hash):
                    raise SnapshotContractError("monthly IO approval history already exists")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return MonthlyIoStored(stored.manifest_id, stored.generation_no, "published", "approved",
                               stored.source_verified_at, stored.materialization)
