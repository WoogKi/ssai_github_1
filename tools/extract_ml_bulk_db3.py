"""Extract bounded DB3 R120/R130 month-product batches into immutable ML storage."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Iterator

import pyodbc

from app.ml.bulk_dataset import Batch, build_bulk_dataset
from app.ml.integration import ml_storage_root
from app.ml.db3_probe import SAMPLE_R120, SAMPLE_R130
from app.services.ssai_analysis_profile_service import load_dashboard_profile_checked
from app.services.ssai_auth_service import build_company_conn_str, get_company_db_config


class Db3Source:
    def __init__(self, connection: pyodbc.Connection) -> None:
        self.connection = connection
        self.query_count = 0

    def rows(self, sql: str, params: tuple[Any, ...]) -> Iterator[dict[str, Any]]:
        if not sql.lstrip().upper().startswith("SELECT"):
            raise ValueError("DB3 source is read-only")
        self.query_count += 1
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql, *params)
            names = [item[0] for item in cursor.description]
            while True:
                chunk = cursor.fetchmany(500)
                if not chunk:
                    break
                for item in chunk:
                    yield dict(zip(names, item))
        finally:
            cursor.close()

    def product_rows(self) -> Iterator[dict[str, Any]]:
        for row in self.rows(
            "SELECT Rd04_Physic_Cd, Rd04_Use_Gu, Rd04_Add_Date "
            "FROM dbo.Rddbc040 ORDER BY Rd04_Physic_Cd", (),
        ):
            yield {**row, "company_id": 3}

    def totals(self, batch: Batch) -> dict[str, Any]:
        marks = ", ".join("?" for _ in batch.product_codes)
        sql = f"""
SELECT COUNT_BIG(*) AS row_count,
       SUM(CAST(ISNULL(Rd12_Quantity, 0) AS decimal(38,4))) AS quantity,
       SUM(CAST(ISNULL(Rd12_Oquantity, 0) AS decimal(38,4))) AS bonus_quantity
FROM dbo.Rddbc120
WHERE Rd12_Out_YyMmDd >= ? AND Rd12_Out_YyMmDd < ?
  AND Rd12_Physic_Cd IN ({marks})
  AND (Rd12_Io_Gu LIKE '5%' OR Rd12_Io_Gu LIKE '6%')
"""
        result = list(self.rows(sql, (batch.date_from, batch.date_to_exclusive, *batch.product_codes)))
        if len(result) != 1:
            raise ValueError("DB3 R120 aggregate did not return one row")
        row = result[0]
        return {"row_count": int(row["row_count"]), "quantity": row["quantity"] or 0,
                "bonus_quantity": row["bonus_quantity"] or 0}

    def outbound_rows(self, batch: Batch) -> Iterator[dict[str, Any]]:
        marks = ", ".join("?" for _ in batch.product_codes)
        yield from self.rows(
            SAMPLE_R120.format(product_marks=marks, row_limit=10001),
            (batch.date_from, batch.date_to_exclusive, *batch.product_codes),
        )

    def document_rows(self, batch: Batch) -> Iterator[dict[str, Any]]:
        marks = ", ".join("?" for _ in batch.product_codes)
        yield from self.rows(
            SAMPLE_R130.format(product_marks=marks, row_limit=10001),
            (batch.date_from, batch.date_to_exclusive, *batch.product_codes),
        )


def _prior_codes(root: Path, version: str) -> set[str]:
    if not version.startswith("db3-research-") or not version.replace("-", "").isalnum():
        raise ValueError("invalid prior DB3 research version")
    source = root / "datasets" / version
    raw = (source / "fixture.json").read_bytes()
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("company_id") != 3 or manifest.get("dataset_version") != version
            or manifest.get("sha256_by_file", {}).get("fixture.json") != hashlib.sha256(raw).hexdigest()):
        raise ValueError("prior DB3 cohort is not verified")
    return {row["Rd04_Physic_Cd"].strip() for row in json.loads(raw)["product_rows"]}


def _fixed_cohort(root: Path, version: str, trusted_manifest_sha256: str) -> tuple[tuple[str, ...], dict[str, Any]]:
    if not version.startswith("db3-bulk-") or not version.replace("-", "").isalnum():
        raise ValueError("invalid fixed DB3 cohort version")
    if len(trusted_manifest_sha256) != 64 or any(char not in "0123456789abcdef" for char in trusted_manifest_sha256):
        raise ValueError("fixed cohort requires a trusted SHA256")
    source = root / "datasets" / version
    manifest_path = source / "manifest.json"
    master_path = source / "products.jsonl"
    if source.is_symlink() or manifest_path.is_symlink() or master_path.is_symlink():
        raise ValueError("fixed cohort cannot use linked files")
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != trusted_manifest_sha256:
        raise ValueError("fixed cohort manifest checksum mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("schema_version") != "ssai_ml_bulk_supply_v0.1"
            or manifest.get("dataset_version") != version or manifest.get("company_id") != 3
            or manifest.get("source_db_alias") != "DB3" or manifest.get("dataset_status") != "COMPLETE"
            or manifest.get("evaluation_grade") != "RESEARCH_POSTHOC"
            or hashlib.sha256(master_path.read_bytes()).hexdigest() != manifest.get("master_sha256")):
        raise ValueError("fixed cohort contract or product checksum mismatch")
    codes = []
    with master_path.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            code = row.get("Rd04_Physic_Cd")
            if row.get("company_id") != 3 or not isinstance(code, str) or not code.strip():
                raise ValueError("fixed cohort product identity mismatch")
            codes.append(code.strip())
    if not codes or len(codes) != manifest.get("product_count") or len(codes) != len(set(codes)):
        raise ValueError("fixed cohort product count or uniqueness mismatch")
    return tuple(codes), manifest


def _select_fixed_products(all_products: list[dict[str, Any]], codes: tuple[str, ...]) -> list[dict[str, Any]]:
    by_code: dict[str, dict[str, Any]] = {}
    for row in all_products:
        code = str(row["Rd04_Physic_Cd"]).strip()
        if code in by_code:
            raise ValueError("duplicate R040 product code")
        by_code[code] = row
    if not set(codes).issubset(by_code):
        raise ValueError("fixed cohort product is missing from R040")
    return [by_code[code] for code in codes]


def run(*, dataset_version: str, date_from: date, date_to: date,
        group_size: int, product_limit: int | None, include_dataset_version: str | None,
        resume: bool, max_batches: int | None, inventory_only: bool = False,
        fixed_cohort_version: str | None = None,
        fixed_cohort_manifest_sha256: str | None = None) -> dict[str, Any]:
    if not dataset_version.startswith("db3-bulk-") or not dataset_version.replace("-", "").isalnum():
        raise ValueError("invalid DB3 bulk version")
    root = ml_storage_root()
    started = perf_counter()
    if bool(fixed_cohort_version) != bool(fixed_cohort_manifest_sha256):
        raise ValueError("fixed cohort version and trusted checksum must be supplied together")
    if fixed_cohort_version and (product_limit is not None or include_dataset_version or inventory_only):
        raise ValueError("fixed cohort cannot be combined with pilot selection or inventory mode")
    fixed_codes, fixed_manifest = (
        _fixed_cohort(root, fixed_cohort_version, fixed_cohort_manifest_sha256)
        if fixed_cohort_version and fixed_cohort_manifest_sha256 else ((), None)
    )
    profile = load_dashboard_profile_checked(company_id=3)
    if profile.status != "ready" or not profile.profile:
        raise ValueError("company 3 saved Dashboard profile unavailable")
    fingerprint = hashlib.sha256(json.dumps(
        profile.profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    if fixed_manifest is not None and fixed_manifest.get("profile_fingerprint") != fingerprint:
        raise ValueError("saved Dashboard profile differs from the fixed cohort")
    cfg = get_company_db_config(3)
    if cfg.company_id != 3:
        raise ValueError("ERP connection is not company 3")
    with pyodbc.connect(build_company_conn_str(cfg), timeout=10, autocommit=True) as connection:
        connection.timeout = 120
        source = Db3Source(connection)
        all_products = list(source.product_rows())
        active = [row for row in all_products if str(row.get("Rd04_Use_Gu") or "").strip() == "0"]
        if inventory_only:
            report_path = root / "diagnostics" / (dataset_version + "-r040.json")
            report_path.parent.mkdir(parents=True, exist_ok=True)
            with report_path.open("x", encoding="utf-8") as stream:
                json.dump({"company_id": 3, "source_db_alias": "DB3", "products": all_products},
                          stream, ensure_ascii=False, default=str)
            return {"all_master_products": len(all_products), "all_active_products": len(active),
                    "all_stopped_products": len(all_products) - len(active),
                    "inventory_file_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
                    "erp_select_count": source.query_count,
                    "elapsed_ms": int((perf_counter() - started) * 1000)}
        if fixed_manifest is not None:
            selected = _select_fixed_products(all_products, fixed_codes)
        elif product_limit is not None:
            if not 1 <= product_limit <= 300:
                raise ValueError("pilot is limited to at most 300 products")
            prior = _prior_codes(root, include_dataset_version) if include_dataset_version else set()
            if not prior.issubset({row["Rd04_Physic_Cd"].strip() for row in all_products}):
                raise ValueError("prior cohort is missing from R040")
            if len(prior) > product_limit:
                raise ValueError("prior cohort exceeds pilot limit")
            selected = [row for row in all_products if row["Rd04_Physic_Cd"].strip() in prior]
            selected += [row for row in active if row["Rd04_Physic_Cd"].strip() not in prior][:product_limit - len(selected)]
        else:
            selected = all_products
        summary = build_bulk_dataset(
            root, dataset_version=dataset_version, source=source, master_rows=selected,
            saved_io_codes=profile.profile.get("io_gu_list") or (), profile_fingerprint=fingerprint,
            calendar_version="UNAVAILABLE_ASOF_EXTRACTION", date_from=date_from, date_to=date_to,
            group_size=group_size, resume=resume, max_batches=max_batches,
        )
        return {**summary, "all_master_products": len(all_products), "all_active_products": len(active),
                "all_stopped_products": len(all_products) - len(active), "erp_select_count": source.query_count,
                "elapsed_ms": int((perf_counter() - started) * 1000)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-read-only", action="store_true")
    parser.add_argument("--company-id", type=int, required=True)
    parser.add_argument("--dataset-version", default="db3-bulk-" + datetime.now().strftime("%Y%m%d%H%M%S"))
    parser.add_argument("--date-from", default="2018-01-01")
    parser.add_argument("--date-to", default="2026-08-30")
    parser.add_argument("--group-size", type=int, default=100)
    parser.add_argument("--product-limit", type=int)
    parser.add_argument("--include-dataset-version")
    parser.add_argument("--fixed-cohort-version")
    parser.add_argument("--fixed-cohort-manifest-sha256")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-batches", type=int)
    parser.add_argument("--inventory-only", action="store_true")
    args = parser.parse_args()
    if not args.live_read_only or args.company_id != 3:
        parser.error("explicit company 3 read-only scope required")
    result = run(
        dataset_version=args.dataset_version, date_from=date.fromisoformat(args.date_from),
        date_to=date.fromisoformat(args.date_to), group_size=args.group_size,
        product_limit=args.product_limit, include_dataset_version=args.include_dataset_version,
        resume=args.resume, max_batches=args.max_batches, inventory_only=args.inventory_only,
        fixed_cohort_version=args.fixed_cohort_version,
        fixed_cohort_manifest_sha256=args.fixed_cohort_manifest_sha256,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
