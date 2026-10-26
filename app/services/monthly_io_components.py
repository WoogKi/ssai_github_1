"""Profile-scoped, immutable monthly I/O evidence for Snapshot price reuse.

This module never changes the operating Snapshot algorithm. Source1 and Source2
remain the authority for any component that is not exactly represented here.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP, localcontext
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

from app.services.dashboard_inventory_frequency_snapshot import SnapshotContractError
from app.services.saved_io_scope import SavedIoScope, bind_io_codes
from app.services.dashboard_inventory_frequency_snapshot_service import (
    FrequencySnapshotPlan,
    _aggregate_product_statistics_event_chunks,
    _product_dimension_scope_sql,
    _python_product_statistics_projection_chunks,
)
from app.services.product_master_filter_contract import add_named_management_only_exclusion


MONTHLY_IO_SCHEMA_VERSION = "1.2"
MONTHLY_IO_ALGORITHM_VERSION = "monthly_io_components_v2"
ZERO = Decimal("0.000000")


@dataclass(frozen=True)
class MonthlyIoFact:
    basis_month: str
    source_kind: str
    row_kind: str
    product_code: str
    stock_code: str
    io_code: str
    bucket_unit_price: Decimal
    raw_product_code: str
    source_row_count: int = 0
    normal_event_count: int = 0
    return_event_count: int = 0
    eligible_price_row_count: int = 0
    source_paid_quantity: Decimal = ZERO
    source_bonus_quantity: Decimal = ZERO
    source_original_supply_amount: Decimal = ZERO
    source_final_supply_amount: Decimal = ZERO
    normal_event_quantity: Decimal = ZERO
    return_quantity: Decimal = ZERO
    return_supply_amount: Decimal = ZERO
    eligible_paid_quantity: Decimal = ZERO
    eligible_supply_amount: Decimal = ZERO
    last_purchase_date: str | None = None
    last_purchase_seq: int | None = None
    last_vendor_code: str | None = None
    last_tie_count: int | None = None
    last_tie_conflict: bool = False

    def key(self) -> tuple[Any, ...]:
        return (
            self.basis_month, self.source_kind, self.row_kind, self.product_code,
            self.stock_code, self.io_code, self.bucket_unit_price, self.raw_product_code,
        )


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise SnapshotContractError(f"monthly IO {field} is not Decimal") from exc
    if not result.is_finite():
        raise SnapshotContractError(f"monthly IO {field} is not finite")
    return result


def _identity(fact: MonthlyIoFact) -> str:
    values = {key: (str(value) if isinstance(value, Decimal) else value)
              for key, value in vars(fact).items()}
    return json.dumps(values, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def fact_checksum(fact: MonthlyIoFact) -> str:
    return hashlib.sha256(_identity(fact).encode("utf-8")).hexdigest()


def whole_checksum(facts: Iterable[MonthlyIoFact]) -> str:
    ordered = sorted(facts, key=lambda item: item.key())
    if len({item.key() for item in ordered}) != len(ordered):
        raise SnapshotContractError("monthly IO fact keys are duplicated")
    return hashlib.sha256("\n".join(fact_checksum(item) for item in ordered).encode("ascii")).hexdigest()


def _r110_scope(plan: FrequencySnapshotPlan, io_scope: SavedIoScope, binds: dict[str, Any]) -> str:
    if not io_scope.normal_inbound_codes or not plan.stock_codes:
        raise SnapshotContractError("monthly R110 requires stored inbound and stock scope")
    join, dimensions = _product_dimension_scope_sql(
        plan, binds, product_alias="P", source_alias="I", source_product_column="Rd11_Physic_Cd",
    )
    if not join:
        join = "INNER JOIN dbo.Rddbc040 AS P ON P.Rd04_Physic_Cd = I.Rd11_Physic_Cd"
    stock_names = []
    for index, code in enumerate(plan.stock_codes):
        name = f"monthly_r110_stock_{index}"
        binds[name] = code
        stock_names.append(f":{name}")
    io_codes = (*io_scope.normal_inbound_codes, *io_scope.inbound_return_codes)
    io_clause = bind_io_codes("I.Rd11_Io_Gu", io_codes, binds, prefix="monthly_r110_io")
    filters = [dimensions.removeprefix("AND ").strip()] if dimensions else []
    add_named_management_only_exclusion(
        filters, product_code_expression="P.Rd04_Physic_Cd",
        product_di_code_expression="P.Rd04_Physic_Di",
    )
    dimensions_sql = "\n  AND ".join(filters)
    return f"""{join}
WHERE I.Rd11_Io_Gu_Gcode = '0012' AND {io_clause}
  AND I.Rd11_Stock_Cd_Gcode = '0018' AND I.Rd11_Stock_Cd IN ({', '.join(stock_names)})
  AND I.Rd11_In_YyMmDd BETWEEN :monthly_from AND :monthly_to
  AND {dimensions_sql}"""


def monthly_product_universe_sql(plan: FrequencySnapshotPlan) -> tuple[str, dict[str, Any]]:
    """Read only the stored-profile product authority needed for an incremental month."""
    binds: dict[str, Any] = {}
    _, dimensions = _product_dimension_scope_sql(plan, binds, product_alias="P")
    filters = [dimensions.removeprefix("AND ").strip()] if dimensions else []
    add_named_management_only_exclusion(
        filters, product_code_expression="P.Rd04_Physic_Cd",
        product_di_code_expression="P.Rd04_Physic_Di",
    )
    where = "\n  AND " + "\n  AND ".join(filters) if filters else ""
    sql = f"""SELECT DISTINCT LTRIM(RTRIM(P.Rd04_Physic_Cd)) AS product_code
FROM dbo.Rddbc040 AS P
WHERE P.Rd04_Physic_Cd <> ''{where}
ORDER BY product_code"""
    return sql, binds


def r110_monthly_base_sql(
    plan: FrequencySnapshotPlan, io_scope: SavedIoScope, *, first_month: str,
    last_month: str | None = None,
) -> tuple[str, dict[str, Any]]:
    from calendar import monthrange
    end = last_month or plan.basis_to[:6]
    binds: dict[str, Any] = {"monthly_from": f"{first_month}01",
                             "monthly_to": f"{end}{monthrange(int(end[:4]), int(end[4:]))[1]:02d}"}
    scope = _r110_scope(plan, io_scope, binds)
    sql = f"""SELECT LEFT(I.Rd11_In_YyMmDd, 6) AS basis_month,
       LTRIM(RTRIM(I.Rd11_Physic_Cd)) AS product_code,
       LTRIM(RTRIM(I.Rd11_Stock_Cd)) AS stock_code,
       LTRIM(RTRIM(I.Rd11_Io_Gu)) AS io_code,
       COUNT_BIG(*) AS source_row_count,
       SUM(CAST(COALESCE(I.Rd11_Quantity, 0) AS decimal(38,6))) AS source_paid_quantity,
       SUM(CAST(COALESCE(I.Rd11_Oquantity, 0) AS decimal(38,6))) AS source_bonus_quantity,
       SUM(CAST(COALESCE(I.Rd11_Supply_Price, 0) AS decimal(38,6))) AS source_original_supply_amount,
       SUM(CAST(COALESCE(I.Rd11_Fin_Supply_Price, I.Rd11_Supply_Price, 0) AS decimal(38,6))) AS source_final_supply_amount
FROM dbo.Rddbc110 AS I
{scope}
GROUP BY LEFT(I.Rd11_In_YyMmDd, 6), LTRIM(RTRIM(I.Rd11_Physic_Cd)),
         LTRIM(RTRIM(I.Rd11_Stock_Cd)), LTRIM(RTRIM(I.Rd11_Io_Gu))
ORDER BY basis_month, product_code, stock_code, io_code"""
    return sql, binds


def r110_monthly_price_sql(
    plan: FrequencySnapshotPlan, io_scope: SavedIoScope, *, first_month: str,
    last_month: str | None = None,
) -> tuple[str, dict[str, Any]]:
    from calendar import monthrange
    end = last_month or plan.basis_to[:6]
    binds: dict[str, Any] = {"monthly_from": f"{first_month}01",
                             "monthly_to": f"{end}{monthrange(int(end[:4]), int(end[4:]))[1]:02d}"}
    scope = _r110_scope(plan, io_scope, binds)
    price_clause = bind_io_codes("I.Rd11_Io_Gu", io_scope.price_codes, binds, prefix="monthly_price_io")
    sql = f"""WITH Candidate AS (
    SELECT LEFT(I.Rd11_In_YyMmDd, 6) AS basis_month,
           LTRIM(RTRIM(I.Rd11_Physic_Cd)) AS product_code,
           RTRIM(I.Rd11_Physic_Cd) AS raw_product_code,
           I.Rd11_In_YyMmDd AS purchase_date,
           I.Rd11_In_Seq AS purchase_seq,
           RTRIM(I.Rd11_Ven_Cd) AS vendor_code,
           CAST(I.Rd11_Quantity AS decimal(28,6)) AS paid_quantity,
           CAST(COALESCE(I.Rd11_Fin_Supply_Price, I.Rd11_Supply_Price) AS decimal(28,6)) AS supply_amount,
           CAST(CAST(COALESCE(I.Rd11_Fin_Supply_Price, I.Rd11_Supply_Price) AS decimal(28,6)) /
                NULLIF(CAST(I.Rd11_Quantity AS decimal(28,6)), 0) AS decimal(28,6)) AS unit_price
    FROM dbo.Rddbc110 AS I
    {scope}
      AND {price_clause}
      AND I.Rd11_Quantity > 0
      AND COALESCE(I.Rd11_Fin_Supply_Price, I.Rd11_Supply_Price, 0) > 0
), Bucket AS (
    SELECT basis_month, product_code, unit_price, COUNT_BIG(*) AS trade_count,
           SUM(paid_quantity) AS paid_quantity, SUM(supply_amount) AS supply_amount
    FROM Candidate GROUP BY basis_month, product_code, unit_price
), RankedTail AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY basis_month, raw_product_code
               ORDER BY purchase_date DESC, purchase_seq DESC, vendor_code DESC) AS tail_rank,
           COUNT_BIG(*) OVER (PARTITION BY basis_month, raw_product_code,
               purchase_date, purchase_seq, vendor_code) AS tie_count,
           MIN(paid_quantity) OVER (PARTITION BY basis_month, raw_product_code,
               purchase_date, purchase_seq, vendor_code) AS min_tie_quantity,
           MAX(paid_quantity) OVER (PARTITION BY basis_month, raw_product_code,
               purchase_date, purchase_seq, vendor_code) AS max_tie_quantity,
           MIN(supply_amount) OVER (PARTITION BY basis_month, raw_product_code,
               purchase_date, purchase_seq, vendor_code) AS min_tie_amount,
           MAX(supply_amount) OVER (PARTITION BY basis_month, raw_product_code,
               purchase_date, purchase_seq, vendor_code) AS max_tie_amount
    FROM Candidate
)
SELECT 'PRICE_BUCKET' AS row_kind, basis_month, product_code,
       CAST('' AS nvarchar(128)) AS raw_product_code, unit_price, trade_count,
       paid_quantity, supply_amount, CAST(NULL AS char(8)) AS purchase_date,
       CAST(NULL AS int) AS purchase_seq, CAST(NULL AS nvarchar(128)) AS vendor_code,
       CAST(NULL AS bigint) AS tie_count, CAST(0 AS bit) AS tie_conflict
FROM Bucket
UNION ALL
SELECT 'PRICE_TAIL', basis_month, product_code, raw_product_code,
       CAST(-1 AS decimal(28,6)), CAST(1 AS bigint), paid_quantity, supply_amount,
       purchase_date, purchase_seq, vendor_code, tie_count,
       CAST(CASE WHEN min_tie_quantity <> max_tie_quantity OR min_tie_amount <> max_tie_amount
            THEN 1 ELSE 0 END AS bit)
FROM RankedTail WHERE tail_rank=1
ORDER BY basis_month, product_code, row_kind, unit_price"""
    return sql, binds


def r110_historical_tail_sql(
    plan: FrequencySnapshotPlan, io_scope: SavedIoScope, *, historical_month: str,
) -> tuple[str, dict[str, Any]]:
    """Seed one pre-window latest eligible price per raw product key."""
    binds: dict[str, Any] = {
        "monthly_from": "19000101", "monthly_to": f"{historical_month}31",
        "historical_month": historical_month,
    }
    scope = _r110_scope(plan, io_scope, binds)
    price_clause = bind_io_codes("I.Rd11_Io_Gu", io_scope.price_codes, binds, prefix="historical_price_io")
    sql = f"""WITH Eligible AS (
    SELECT LTRIM(RTRIM(I.Rd11_Physic_Cd)) AS product_code,
           RTRIM(I.Rd11_Physic_Cd) AS raw_product_code,
           I.Rd11_In_YyMmDd AS purchase_date, I.Rd11_In_Seq AS purchase_seq,
           RTRIM(I.Rd11_Ven_Cd) AS vendor_code,
           CAST(I.Rd11_Quantity AS decimal(28,6)) AS paid_quantity,
           CAST(COALESCE(I.Rd11_Fin_Supply_Price,I.Rd11_Supply_Price) AS decimal(28,6)) AS supply_amount,
           ROW_NUMBER() OVER (PARTITION BY RTRIM(I.Rd11_Physic_Cd)
             ORDER BY I.Rd11_In_YyMmDd DESC,I.Rd11_In_Seq DESC,I.Rd11_Ven_Cd DESC) AS price_rank
    FROM dbo.Rddbc110 AS I
    {scope}
      AND {price_clause} AND I.Rd11_Quantity > 0
      AND COALESCE(I.Rd11_Fin_Supply_Price,I.Rd11_Supply_Price,0) > 0
)
SELECT 'PRICE_TAIL' AS row_kind, :historical_month AS basis_month, product_code,
       raw_product_code, CAST(-1 AS decimal(28,6)) AS unit_price,
       CAST(1 AS bigint) AS trade_count, paid_quantity, supply_amount,
       purchase_date,purchase_seq,vendor_code,CAST(1 AS bigint) AS tie_count,
       CAST(0 AS bit) AS tie_conflict
FROM Eligible WHERE price_rank=1
ORDER BY product_code,raw_product_code"""
    return sql, binds


def r110_facts(base: pd.DataFrame, prices: pd.DataFrame) -> tuple[MonthlyIoFact, ...]:
    output: list[MonthlyIoFact] = []
    for row in base.itertuples(index=False):
        output.append(MonthlyIoFact(
            basis_month=str(row.basis_month), source_kind="R110", row_kind="BASE",
            product_code=str(row.product_code), stock_code=str(row.stock_code), io_code=str(row.io_code),
            bucket_unit_price=Decimal(-1), raw_product_code="",
            source_row_count=int(row.source_row_count),
            source_paid_quantity=_decimal(row.source_paid_quantity, field="source_paid_quantity"),
            source_bonus_quantity=_decimal(row.source_bonus_quantity, field="source_bonus_quantity"),
            source_original_supply_amount=_decimal(row.source_original_supply_amount, field="source_original_supply_amount"),
            source_final_supply_amount=_decimal(row.source_final_supply_amount, field="source_final_supply_amount"),
        ))
    for row in prices.itertuples(index=False):
        kind = str(row.row_kind)
        if kind not in {"PRICE_BUCKET", "PRICE_TAIL"}:
            raise SnapshotContractError("R110 monthly price row kind is invalid")
        conflict = bool(row.tie_conflict) if kind == "PRICE_TAIL" else False
        output.append(MonthlyIoFact(
            basis_month=str(row.basis_month), source_kind="R110", row_kind=kind,
            product_code=str(row.product_code), stock_code="", io_code="000",
            bucket_unit_price=_decimal(row.unit_price, field="unit_price"),
            raw_product_code=str(row.raw_product_code or "") if kind == "PRICE_TAIL" else "",
            eligible_price_row_count=int(row.trade_count),
            eligible_paid_quantity=ZERO if conflict else _decimal(row.paid_quantity, field="paid_quantity"),
            eligible_supply_amount=ZERO if conflict else _decimal(row.supply_amount, field="supply_amount"),
            last_purchase_date=str(row.purchase_date) if kind == "PRICE_TAIL" else None,
            last_purchase_seq=int(row.purchase_seq) if kind == "PRICE_TAIL" else None,
            last_vendor_code=str(row.vendor_code) if kind == "PRICE_TAIL" else None,
            last_tie_count=int(row.tie_count) if kind == "PRICE_TAIL" else None,
            last_tie_conflict=conflict,
        ))
    whole_checksum(output)
    return tuple(output)


def r120_facts_from_raw_chunks(
    chunks: Iterable[pd.DataFrame], *, plan: FrequencySnapshotPlan,
    io_scope: SavedIoScope, product_codes: set[str],
    projection_result: dict[str, Any] | None = None,
) -> tuple[MonthlyIoFact, ...]:
    """Reuse the operating slip classifier; retain only monthly aggregates."""
    if not io_scope.outbound_codes or not io_scope.outbound_return_codes:
        raise SnapshotContractError("monthly R120 requires both saved outbound roles")
    selected = set(io_scope.outbound_codes) | set(io_scope.outbound_return_codes)
    totals: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    def entry(key: tuple[str, str, str, str]) -> dict[str, Any]:
        return totals.setdefault(key, {
            "source_row_count": 0, "normal_event_count": 0, "return_event_count": 0,
            "source_paid_quantity": ZERO, "source_bonus_quantity": ZERO,
            "source_original_supply_amount": ZERO, "source_final_supply_amount": ZERO,
            "normal_event_quantity": ZERO, "return_quantity": ZERO,
            "return_supply_amount": ZERO,
        })

    def on_raw(row: Mapping[str, Any]) -> None:
        outbound_date = str(row.get("outbound_date") or "").strip()
        product_code = str(row.get("product_code") or "").strip()
        stock_code = str(row.get("stock_code") or "").strip()
        io_code = str(row.get("io_tcode") or "").strip()
        if (not plan.basis_from <= outbound_date <= plan.basis_to
                or product_code not in product_codes or io_code not in selected):
            return
        if not stock_code:
            raise SnapshotContractError("monthly R120 scoped row has no stock code")
        key = (outbound_date[:6], product_code, stock_code, io_code)
        current = entry(key)
        current["source_row_count"] += 1
        paid = _decimal(row.get("paid_quantity"), field="R120 paid_quantity")
        total = _decimal(row.get("outbound_quantity"), field="R120 outbound_quantity")
        current["source_paid_quantity"] += paid
        current["source_bonus_quantity"] += total - paid
        current["source_original_supply_amount"] += _decimal(
            row.get("original_supply_amount"), field="R120 original_supply_amount",
        )
        current["source_final_supply_amount"] += _decimal(row.get("supply_amount"), field="R120 supply_amount")

    def on_event(row: Mapping[str, Any]) -> None:
        product_code = str(row["product_code"])
        if product_code not in product_codes:
            return
        key = (str(row["month"]), product_code, str(row["stock_code"]), str(row["io_code"]))
        if key not in totals:
            raise SnapshotContractError("accepted R120 event has no scoped source row")
        current = entry(key)
        if row["kind"] == "normal":
            current["normal_event_count"] += 1
            current["normal_event_quantity"] += _decimal(row["quantity"], field="normal quantity")
        elif row["kind"] == "return":
            current["return_event_count"] += 1
            current["return_quantity"] += _decimal(row["quantity"], field="return quantity")
            current["return_supply_amount"] += _decimal(row["supply_amount"], field="return amount")
        else:
            raise SnapshotContractError("monthly R120 observer event kind is invalid")

    # Consume all chunks; the operating projection emits the same event,
    # return and diagnostics rows, but only the optional observers retain BASE.
    projections = _python_product_statistics_projection_chunks(
        chunks, basis_from=plan.basis_from, io_scope=io_scope,
        raw_row_observer=on_raw, accepted_event_observer=on_event,
    )
    if projection_result is None:
        for _ in projections:
            pass
    else:
        projection_result["result"] = _aggregate_product_statistics_event_chunks(projections)
    facts = tuple(MonthlyIoFact(
        basis_month=month, source_kind="R120", row_kind="BASE", product_code=product,
        stock_code=stock, io_code=io, bucket_unit_price=Decimal(-1), raw_product_code="",
        **values,
    ) for (month, product, stock, io), values in sorted(totals.items()))
    whole_checksum(facts)
    return facts


def reconstruct_purchase_prices(
    facts: Iterable[MonthlyIoFact], *, three_month_from: str, six_month_from: str,
    basis_month: str, product_codes: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Recreate the operating per-trade median/exclusion from exact price buckets."""
    buckets: dict[str, list[MonthlyIoFact]] = defaultdict(list)
    tails: dict[str, list[MonthlyIoFact]] = defaultdict(list)
    for fact in facts:
        if fact.source_kind != "R110":
            continue
        if fact.row_kind == "PRICE_BUCKET" and six_month_from <= fact.basis_month <= basis_month:
            buckets[fact.product_code].append(fact)
        elif fact.row_kind == "PRICE_TAIL" and fact.basis_month < six_month_from:
            tails[fact.raw_product_code].append(fact)

    output: dict[str, dict[str, Any]] = {}
    for product in product_codes:
        available = buckets.get(product, ())
        recent = [fact for fact in available if fact.basis_month >= three_month_from]
        selected = recent or list(available)
        if selected:
            expanded_count = sum(fact.eligible_price_row_count for fact in selected)
            if expanded_count <= 0:
                raise SnapshotContractError("monthly price bucket has no trades")
            ordered = sorted(selected, key=lambda item: item.bucket_unit_price)
            middle = ((expanded_count + 1) // 2, (expanded_count + 2) // 2)
            prices: list[Decimal] = []
            position = 0
            for fact in ordered:
                next_position = position + fact.eligible_price_row_count
                for rank in middle:
                    if position < rank <= next_position:
                        prices.append(fact.bucket_unit_price)
                position = next_position
            center = ((prices[0] + prices[1]) / 2).quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_UP,
            )
            total_quantity = sum((f.eligible_paid_quantity for f in selected), ZERO)
            center_facts = [f for f in selected if center * Decimal("0.8") <= f.bucket_unit_price <= center * Decimal("1.2")]
            extreme_facts = [f for f in selected if f.bucket_unit_price > 2 and
                             (f.bucket_unit_price >= center * 5 or f.bucket_unit_price <= center / 5)]
            center_count = sum(f.eligible_price_row_count for f in center_facts)
            center_quantity = sum((f.eligible_paid_quantity for f in center_facts), ZERO)
            extreme_count = sum(f.eligible_price_row_count for f in extreme_facts)
            extreme_quantity = sum((f.eligible_paid_quantity for f in extreme_facts), ZERO)
            exclude = (
                expanded_count >= 4 and center_count >= 3
                and center_count * 4 >= expanded_count * 3
                and center_quantity * 5 >= total_quantity * 4
                and center > 2 and extreme_count * 4 <= expanded_count
                and extreme_quantity * 5 <= total_quantity
            )
            excluded = {id(f) for f in extreme_facts} if exclude else set()
            accepted = [f for f in selected if id(f) not in excluded]
            paid = sum((f.eligible_paid_quantity for f in accepted), ZERO)
            amount = sum((f.eligible_supply_amount for f in accepted), ZERO)
            excluded_rows = [f for f in selected if id(f) in excluded]
            if paid <= 0:
                raise SnapshotContractError("monthly price exclusion removed all purchases")
            output[product] = {
                "unit_price": (amount / paid).quantize(Decimal("0.000001"), rounding=ROUND_DOWN).quantize(
                    Decimal("0.0000000001")),
                "basis_month": max(f.basis_month for f in accepted),
                "basis_kind": "three_month" if recent else "six_month",
                "excluded_trade_count": sum(f.eligible_price_row_count for f in excluded_rows),
                "excluded_quantity": sum((f.eligible_paid_quantity for f in excluded_rows), ZERO),
                "excluded_amount": sum((f.eligible_supply_amount for f in excluded_rows), ZERO),
                "original_quantity": total_quantity,
                "original_amount": sum((f.eligible_supply_amount for f in selected), ZERO),
                "center_price": center.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP),
            }
            continue

        def latest(raw_code: str) -> MonthlyIoFact | None:
            eligible = [f for f in tails.get(raw_code, ()) if not f.last_tie_conflict]
            return max(eligible, key=lambda f: (f.last_purchase_date or "", f.last_purchase_seq or -1,
                                                 f.last_vendor_code or ""), default=None)

        tail = latest(product)
        if tail is None and len(product) < 5:
            tail = latest(product.rjust(5))
        if tail is not None:
            output[product] = {
                "unit_price": (tail.eligible_supply_amount / tail.eligible_paid_quantity).quantize(
                    Decimal("0.000001"), rounding=ROUND_DOWN).quantize(Decimal("0.0000000001")),
                "basis_month": (tail.last_purchase_date or "")[:6],
                "basis_kind": "historical",
                "excluded_trade_count": 0, "excluded_quantity": ZERO,
                "excluded_amount": ZERO, "original_quantity": ZERO,
                "original_amount": ZERO, "center_price": None,
            }
    return output
