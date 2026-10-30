"""Bounded, resumable DB3 R120 research extraction; never certifies source completeness."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Iterable, Mapping

from app.ml.phase1 import prepare_sources
from app.services.saved_io_scope import resolve_saved_io_scope


KST = timezone(timedelta(hours=9))
MAX_BATCH_ROWS = 10000
MAX_BATCH_BYTES = 32 * 1024 * 1024
MAX_GROUP_PRODUCTS = 300
FIRST_DAY = date(2018, 1, 1)
LAST_DAY = date(2026, 8, 30)


@dataclass(frozen=True)
class Batch:
    batch_id: str
    date_from: str
    date_to_exclusive: str
    product_codes: tuple[str, ...]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")) + "\n"


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _month_batches(products: tuple[str, ...], start: date, end: date, group_size: int) -> tuple[Batch, ...]:
    if not 1 <= group_size <= MAX_GROUP_PRODUCTS or start < FIRST_DAY or end > LAST_DAY or end < start:
        raise ValueError("invalid DB3 bulk scope")
    if start.day != 1 or ((end + timedelta(days=1)).day != 1 and end != LAST_DAY):
        raise ValueError("bulk windows must include whole calendar months")
    batches = []
    month = start
    while month <= end:
        next_month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        for offset in range(0, len(products), group_size):
            group = offset // group_size
            batch_end = min(next_month, end + timedelta(days=1))
            batches.append(Batch(f"{month:%Y%m}-g{group:04d}", month.strftime("%Y%m%d"),
                                 batch_end.strftime("%Y%m%d"), products[offset:offset + group_size]))
        month = next_month
    return tuple(batches)


def _load_master(rows: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    products: dict[str, dict[str, Any]] = {}
    for raw in rows:
        code = raw.get("Rd04_Physic_Cd")
        if not isinstance(code, str) or not code.strip() or code.strip() in products:
            raise ValueError("R040 product code is missing, non-string or duplicate")
        row = json.loads(json.dumps(dict(raw), default=str))
        if int(row.get("company_id", 0)) != 3:
            raise ValueError("R040 company boundary mismatch")
        products[code.strip()] = row
    if not products:
        raise ValueError("R040 master is empty")
    return tuple(products[code] for code in sorted(products))


def _write_rows(path: Path, rows: Iterable[Mapping[str, Any]], *, company_id: int,
                row_cap: int = MAX_BATCH_ROWS) -> tuple[int, Decimal, Decimal]:
    count = 0
    size = 0
    quantity = bonus = Decimal(0)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        for source in rows:
            row = dict(source)
            if row.get("company_id", company_id) != company_id:
                raise ValueError("batch company boundary mismatch")
            row["company_id"] = company_id
            count += 1
            if count > row_cap:
                raise ValueError("R120/R130 batch exceeds row cap; use a smaller group")
            if "Rd12_Quantity" in row:
                quantity += Decimal(str(row["Rd12_Quantity"]))
                bonus += Decimal(str(row.get("Rd12_Oquantity") or 0))
            line = _json(row)
            size += len(line.encode("utf-8"))
            if size > MAX_BATCH_BYTES:
                raise ValueError("batch exceeds file-size cap; use a smaller group")
            stream.write(line)
    return count, quantity, bonus


def _read_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


def _batch_dir(root: Path, batch: Batch) -> Path:
    return root / "batches" / batch.batch_id


def _whole_digest(root: Path, batches: Iterable[Batch]) -> str:
    digest = hashlib.sha256()
    digest.update(_sha(root / "plan.json").encode("ascii"))
    digest.update(_sha(root / "products.jsonl").encode("ascii"))
    for batch in batches:
        digest.update(batch.batch_id.encode("ascii"))
        digest.update(_sha(_batch_dir(root, batch) / "manifest.json").encode("ascii"))
    return digest.hexdigest()


def _role_totals(rows: Iterable[Mapping[str, Any]], codes: Iterable[str]) -> dict[str, Any]:
    scope = resolve_saved_io_scope(codes, confirmed_trade_roles=True)
    source_rows = list(rows)
    result: dict[str, Any] = {}
    for name, role_codes in (("normal", scope.outbound_codes), ("return", scope.outbound_return_codes),
                             ("other", tuple(code for code in {str(row["Rd12_Io_Gu"]).strip() for row in source_rows}
                                             if code not in (*scope.outbound_codes, *scope.outbound_return_codes)))):
        selected = [row for row in source_rows if str(row["Rd12_Io_Gu"]).strip() in role_codes]
        result[name + "_rows"] = len(selected)
        result[name + "_quantity"] = str(sum((Decimal(str(row["Rd12_Quantity"])) for row in selected), Decimal(0)))
        if name == "normal":
            result["negative_normal_rows"] = sum(Decimal(str(row["Rd12_Quantity"])) < 0 for row in selected)
    return result


def verify_batch(root: Path, batch: Batch) -> dict[str, Any]:
    folder = _batch_dir(root, batch)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("batch_id") != batch.batch_id or manifest.get("status") != "VERIFIED"
            or manifest.get("date_from") != batch.date_from
            or manifest.get("date_to_exclusive") != batch.date_to_exclusive
            or manifest.get("product_count") != len(batch.product_codes)):
        raise ValueError("batch identity or status mismatch")
    for name in ("r120.jsonl", "r130.jsonl"):
        if _sha(folder / name) != manifest["sha256_by_file"][name]:
            raise ValueError("batch file checksum mismatch")
    count = 0
    qty = bonus = Decimal(0)
    source_keys = set()
    outbound = _read_rows(folder / "r120.jsonl")
    for row in outbound:
        code = row["Rd12_Physic_Cd"]
        day = row["Rd12_Out_YyMmDd"]
        if (int(row["company_id"]) != 3 or not isinstance(code, str)
                or code.strip() not in batch.product_codes or not batch.date_from <= day < batch.date_to_exclusive):
            raise ValueError("R120 batch scope mismatch")
        key = (day, row["Rd12_Ven_Cd"], row["Rd12_Out_Seq"])
        if key in source_keys:
            raise ValueError("duplicate R120 source PK")
        source_keys.add(key)
        count += 1
        qty += Decimal(str(row["Rd12_Quantity"]))
        bonus += Decimal(str(row.get("Rd12_Oquantity") or 0))
    headers = _read_rows(folder / "r130.jsonl")
    header_keys = set()
    for row in headers:
        if int(row["company_id"]) != 3 or int(row["header_count"]) != 1 or row["Rd13_Trans_Di"] != "3":
            raise ValueError("R130 header count, direction or company mismatch")
        key = tuple(str(row[field]).strip() for field in (
            "Rd13_Trans_Di", "Rd13_Trans_YyMmDd", "Rd13_Ven_Cd", "Rd13_Trans_Seq"))
        if key in header_keys:
            raise ValueError("duplicate R130 document key")
        header_keys.add(key)
    if (count != manifest["r120_rows"] or str(qty) != manifest["r120_quantity"]
            or str(bonus) != manifest["r120_bonus"] or len(headers) != manifest["r130_headers"]):
        raise ValueError("batch count or quantity mismatch")
    if "role_totals" in manifest:
        plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
        if manifest["role_totals"] != _role_totals(outbound, plan["saved_io_codes"]):
            raise ValueError("batch saved I/O role totals differ")
    return manifest


def _save_batch(
    root: Path, batch: Batch, source: Any, masters: Mapping[str, Mapping[str, Any]],
    saved_io_codes: tuple[str, ...],
) -> dict[str, Any]:
    folder = _batch_dir(root, batch)
    if folder.exists():
        raise FileExistsError("incomplete batch exists; inspect it before an explicit resume")
    folder.mkdir(parents=True, exist_ok=False)
    started = perf_counter()
    stage = "source_totals"
    try:
        expected = source.totals(batch)
        if int(expected["row_count"]) > MAX_BATCH_ROWS:
            raise ValueError("R120 batch exceeds row cap; use a smaller group")
        stage = "r120_fetch"
        count, qty, bonus = _write_rows(folder / "r120.jsonl", source.outbound_rows(batch), company_id=3)
        stage = "r130_fetch"
        headers, _, _ = _write_rows(folder / "r130.jsonl", source.document_rows(batch), company_id=3)
        stage = "independent_exact"
        if (count != int(expected["row_count"]) or qty != Decimal(str(expected["quantity"]))
                or bonus != Decimal(str(expected["bonus_quantity"]))):
            raise ValueError("R120 batch differs from independent SQL totals")
        outbound = _read_rows(folder / "r120.jsonl")
        docs = _read_rows(folder / "r130.jsonl")
        stage = "prepare_sources"
        prepared = prepare_sources(
            company_id=3, outbound_rows=outbound, document_rows=docs, vendor_rows=(),
            product_rows=(masters[code] for code in batch.product_codes), standard_rows=(),
            code_rows=(), saved_io_codes=saved_io_codes,
        )
        if prepared.source_row_count != count or prepared.source_quantity != qty or prepared.source_bonus_quantity != bonus:
            raise ValueError("prepared batch differs from original R120")
        manifest = {
            "batch_id": batch.batch_id, "status": "VERIFIED", "company_id": 3,
            "date_from": batch.date_from, "date_to_exclusive": batch.date_to_exclusive,
            "product_count": len(batch.product_codes), "r120_rows": count,
            "r120_quantity": str(qty), "r120_bonus": str(bonus), "r130_headers": headers,
            "role_totals": _role_totals(outbound, saved_io_codes),
            "document_match_counts": dict(prepared.document_match_counts),
            "source_query_count": 3, "row_cap": MAX_BATCH_ROWS, "file_cap_bytes": MAX_BATCH_BYTES,
            "verified_at_kst": datetime.now(KST).isoformat(),
            "elapsed_ms": int((perf_counter() - started) * 1000),
            "sha256_by_file": {name: _sha(folder / name) for name in ("r120.jsonl", "r130.jsonl")},
        }
        (folder / "manifest.json").write_text(_json(manifest), encoding="utf-8")
        stage = "readback"
        verify_batch(root, batch)
        return manifest
    except Exception as exc:
        (folder / "error.json").write_text(_json({
            "batch_id": batch.batch_id, "status": "FAILED", "failed_stage": stage,
            "error_type": type(exc).__name__,
            "reason": str(exc)[:160] if isinstance(exc, ValueError) else "inspect local execution log",
        }), encoding="utf-8")
        raise


def build_bulk_dataset(
    root: Path, *, dataset_version: str, source: Any, master_rows: Iterable[Mapping[str, Any]],
    saved_io_codes: Iterable[str], profile_fingerprint: str, calendar_version: str,
    date_from: date = FIRST_DAY, date_to: date = LAST_DAY, group_size: int = 100,
    resume: bool = False, max_batches: int | None = None,
) -> dict[str, Any]:
    if not dataset_version.startswith("db3-bulk-") or not dataset_version.replace("-", "").isalnum():
        raise ValueError("invalid DB3 bulk dataset version")
    if not profile_fingerprint or not calendar_version or (max_batches is not None and max_batches < 1):
        raise ValueError("missing DB3 profile/Calendar or invalid batch limit")
    scope = resolve_saved_io_scope(saved_io_codes, confirmed_trade_roles=True)
    masters = _load_master(master_rows)
    codes = tuple(row["Rd04_Physic_Cd"].strip() for row in masters)
    batches = _month_batches(codes, date_from, date_to, group_size)
    target = root / "datasets" / dataset_version
    specification = {
        "schema_version": "ml_db3_bulk_v1", "dataset_version": dataset_version,
        "company_id": 3, "source_db_alias": "DB3", "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(), "group_size": group_size,
        "profile_fingerprint": profile_fingerprint, "saved_io_codes": list(scope.all_codes),
        "calendar_version": calendar_version, "dataset_status": "BUILDING",
        "evaluation_grade": "research_posthoc", "last_complete_date": None,
    }
    if resume:
        old = json.loads((target / "plan.json").read_text(encoding="utf-8"))
        if old != specification:
            raise ValueError("resume plan differs from original scope")
        master_file = target / "products.jsonl"
        if _sha(master_file) != (target / "products.sha256").read_text(encoding="ascii").strip():
            raise ValueError("product master checksum mismatch")
        if tuple(_read_rows(master_file)) != masters:
            raise ValueError("product master changed since extraction plan")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir(parents=True, exist_ok=False)
        (target / "plan.json").write_text(_json(specification), encoding="utf-8")
        _write_rows(target / "products.jsonl", masters, company_id=3, row_cap=100000)
        (target / "products.sha256").write_text(_sha(target / "products.jsonl") + "\n", encoding="ascii")
    master_index = {row["Rd04_Physic_Cd"].strip(): row for row in masters}
    complete = skipped = 0
    for batch in batches:
        if max_batches is not None and complete >= max_batches:
            break
        folder = _batch_dir(target, batch)
        if folder.exists():
            if resume and ((folder / "error.json").exists() or not (folder / "manifest.json").exists()):
                failed_root = target / "failed"
                failed_root.mkdir(exist_ok=True)
                attempt = 1
                failed = failed_root / batch.batch_id
                while failed.exists():
                    attempt += 1
                    failed = failed_root / f"{batch.batch_id}-attempt{attempt}"
                folder.rename(failed)
                folder = _batch_dir(target, batch)
            else:
                verify_batch(target, batch)
                skipped += 1
                continue
        _save_batch(target, batch, source, master_index, scope.all_codes)
        complete += 1
    result = verify_bulk_dataset(target, require_complete=False)
    finished = result["verified_batches"] == len(batches)
    if finished and result["verified_batches"] == len(batches):
        manifest = {**specification, "dataset_status": "RESEARCH_POSTHOC",
                    "batch_count": len(batches), "source_rows": result["source_rows"],
                    "source_quantity": result["source_quantity"], "source_bonus": result["source_bonus"],
                    "master_sha256": _sha(target / "products.jsonl"),
                    "whole_sha256": _whole_digest(target, batches)}
        manifest_path = target / "manifest.json"
        if manifest_path.exists():
            old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if any(old_manifest.get(key) != value for key, value in manifest.items()
                   if key != "whole_sha256" or key in old_manifest):
                raise ValueError("existing complete manifest differs")
        if not manifest_path.exists():
            manifest_path.write_text(_json(manifest), encoding="utf-8")
    return {**result, "new_batches": complete, "reused_batches": skipped,
            "planned_batches": len(batches), "dataset_status": "RESEARCH_POSTHOC" if finished else "BUILDING"}


def verify_bulk_dataset(target: Path, *, require_complete: bool = True) -> dict[str, Any]:
    plan = json.loads((target / "plan.json").read_text(encoding="utf-8"))
    if plan.get("company_id") != 3 or plan.get("source_db_alias") != "DB3":
        raise ValueError("bulk dataset company or source mismatch")
    master = target / "products.jsonl"
    if _sha(master) != (target / "products.sha256").read_text(encoding="ascii").strip():
        raise ValueError("product master checksum mismatch")
    products = tuple(row["Rd04_Physic_Cd"].strip() for row in _read_rows(master))
    batches = _month_batches(products, date.fromisoformat(plan["date_from"]),
                             date.fromisoformat(plan["date_to"]), int(plan["group_size"]))
    actual = {path.name for path in (target / "batches").iterdir() if path.is_dir()} if (target / "batches").exists() else set()
    if actual - {batch.batch_id for batch in batches}:
        raise ValueError("unexpected batch directory outside extraction plan")
    rows = 0
    qty = bonus = Decimal(0)
    verified = 0
    for batch in batches:
        folder = _batch_dir(target, batch)
        if folder.exists():
            meta = verify_batch(target, batch)
            rows += meta["r120_rows"]
            qty += Decimal(meta["r120_quantity"])
            bonus += Decimal(meta["r120_bonus"])
            verified += 1
    if require_complete:
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        if (verified != len(batches) or manifest.get("dataset_status") != "RESEARCH_POSTHOC"
                or manifest.get("batch_count") != len(batches) or manifest.get("source_rows") != rows
                or Decimal(manifest["source_quantity"]) != qty or Decimal(manifest["source_bonus"]) != bonus
                or manifest.get("master_sha256") != _sha(master)):
            raise ValueError("bulk dataset is incomplete or totals differ")
        for field in ("dataset_version", "company_id", "source_db_alias", "date_from", "date_to",
                      "group_size", "profile_fingerprint", "saved_io_codes", "calendar_version"):
            if manifest.get(field) != plan.get(field):
                raise ValueError("bulk dataset contract differs from extraction plan")
        if "whole_sha256" in manifest and manifest["whole_sha256"] != _whole_digest(target, batches):
            raise ValueError("bulk dataset whole checksum mismatch")
    return {"verified_batches": verified, "planned_batches": len(batches),
            "source_rows": rows, "source_quantity": str(qty), "source_bonus": str(bonus),
            "master_products": len(products),
            "active_products": sum(str(row.get("Rd04_Use_Gu") or "").strip() == "0" for row in _read_rows(master))}


def materialize_research_cohort(
    bulk_dataset: Path, target_root: Path, *, dataset_version: str,
    product_codes: tuple[str, ...], as_of_dates: tuple[str, ...], calendar: Mapping[str, Any],
    date_from: date | None = None, date_to: date | None = None,
) -> Path:
    """Feed the unchanged small-fixture engines from verified shards, within their old cap."""
    verify_bulk_dataset(bulk_dataset)
    if not dataset_version.startswith("db3-research-") or not dataset_version.replace("-", "").isalnum():
        raise ValueError("invalid bounded research version")
    if not 1 <= len(product_codes) <= 12 or len(set(product_codes)) != len(product_codes):
        raise ValueError("bounded training adapter accepts 1..12 unique products")
    plan = json.loads((bulk_dataset / "plan.json").read_text(encoding="utf-8"))
    start = date_from or date.fromisoformat(plan["date_from"])
    end = date_to or date.fromisoformat(plan["date_to"])
    if (start < date.fromisoformat(plan["date_from"]) or end > date.fromisoformat(plan["date_to"])
            or calendar.get("status") != "ready" or calendar["start_date"] > start.isoformat()
            or calendar["end_date"] < end.isoformat()):
        raise ValueError("offline Calendar does not cover requested source period")
    cutoffs = tuple(date.fromisoformat(value) for value in as_of_dates)
    if any(day - timedelta(days=89) < start or day + timedelta(days=30) > end for day in cutoffs):
        raise ValueError("research as-of range is outside verified shards")
    masters = {row["Rd04_Physic_Cd"].strip(): row for row in _read_rows(bulk_dataset / "products.jsonl")}
    if not set(product_codes).issubset(masters):
        raise ValueError("cohort product is absent from R040 master")
    batches = _month_batches(tuple(sorted(masters)), start, end, int(plan["group_size"]))
    selected = set(product_codes)
    outbound = []
    documents: dict[tuple[str, ...], dict[str, Any]] = {}
    seen = set()
    for batch in batches:
        if not selected.intersection(batch.product_codes):
            continue
        folder = _batch_dir(bulk_dataset, batch)
        for row in _read_rows(folder / "r120.jsonl"):
            if row["Rd12_Physic_Cd"].strip() not in selected:
                continue
            key = (row["Rd12_Out_YyMmDd"], row["Rd12_Ven_Cd"], row["Rd12_Out_Seq"])
            if key in seen:
                raise ValueError("R120 source PK duplicated across verified shards")
            seen.add(key)
            outbound.append(row)
            if len(outbound) > 30000:
                raise ValueError("bounded research adapter exceeds legacy row cap")
        for row in _read_rows(folder / "r130.jsonl"):
            key = tuple(str(row[field]).strip() for field in (
                "Rd13_Trans_Di", "Rd13_Trans_YyMmDd", "Rd13_Ven_Cd", "Rd13_Trans_Seq"))
            previous = documents.setdefault(key, row)
            if previous != row:
                raise ValueError("R130 document changed between shards")
    prepared = prepare_sources(
        company_id=3, outbound_rows=outbound, document_rows=documents.values(), vendor_rows=(),
        product_rows=(masters[code] for code in product_codes), standard_rows=(), code_rows=(),
        saved_io_codes=plan["saved_io_codes"],
    )
    if prepared.source_row_count != len(outbound):
        raise ValueError("bounded research source count differs")
    dates = [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]
    payload = {
        "schema_version": "ml_phase1_fixture_v1", "dataset_version": dataset_version,
        "company_id": 3, "date_from": start.isoformat(), "date_to": end.isoformat(),
        "outbound_rows": outbound, "document_rows": list(documents.values()),
        "vendor_rows": [], "product_rows": [masters[code] for code in product_codes],
        "standard_rows": [], "code_rows": [], "saved_io_codes": plan["saved_io_codes"],
        "complete_dates": dates, "as_of_dates": list(as_of_dates),
        "timestamp_policy_confirmed": True, "calendar_asof_required": False,
        "calendar": {key: calendar[key] for key in (
            "start_date", "end_date", "holiday_dates", "known_as_of", "version")},
    }
    target = target_root / "datasets" / dataset_version
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=False)
    raw = _json(payload).encode("utf-8")
    (target / "fixture.json").write_bytes(raw)
    (target / "manifest.json").write_text(_json({
        "company_id": 3, "dataset_version": dataset_version, "dataset_status": "RESEARCH_POSTHOC",
        "evaluation_grade": "research_posthoc", "not_operationally_approved": True,
        "parent_bulk_version": plan["dataset_version"],
        "parent_bulk_manifest_sha256": _sha(bulk_dataset / "manifest.json"),
        "source_rows": len(outbound), "source_quantity": str(prepared.source_quantity),
        "sha256_by_file": {"fixture.json": hashlib.sha256(raw).hexdigest()},
    }), encoding="utf-8")
    return target
