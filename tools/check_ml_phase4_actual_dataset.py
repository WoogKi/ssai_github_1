"""Read-only independent checks of one DB3 research dataset and its predictions."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from app.services.saved_io_scope import resolve_saved_io_scope


def check(dataset: Path, run: Path) -> dict:
    manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
    raw = (dataset / "fixture.json").read_bytes()
    if (manifest.get("company_id") != 3 or manifest.get("dataset_status") != "RESEARCH_POSTHOC"
            or manifest.get("sha256_by_file", {}).get("fixture.json") != hashlib.sha256(raw).hexdigest()):
        raise ValueError("research dataset identity or checksum mismatch")
    fixture = json.loads(raw)
    predictions = json.loads((run / "predictions.json").read_text(encoding="utf-8"))
    scope = resolve_saved_io_scope(fixture["saved_io_codes"], confirmed_trade_roles=True)
    rows = fixture["outbound_rows"]
    if (len(rows) != int(manifest["metrics"]["source_rows"])
            or sum((Decimal(str(row["Rd12_Quantity"])) for row in rows), Decimal(0))
            != Decimal(manifest["metrics"]["source_quantity"])):
        raise ValueError("R120 source rows or quantity changed")
    product_codes = {str(row["Rd12_Physic_Cd"]).strip() for row in rows}
    if len(product_codes) != int(manifest["metrics"]["selected_products"]):
        raise ValueError("product count changed")
    checked = withheld = 0
    for item in predictions:
        if item["evaluation_grade"] != "research_posthoc" or int(item["horizon_days"]) != 30:
            raise ValueError("prediction grade or horizon mismatch")
        as_of = date.fromisoformat(item["as_of"])
        end = (as_of + timedelta(days=30)).strftime("%Y%m%d")
        cutoff = as_of.strftime("%Y%m%d")
        product = item["product_code"]
        history = [row for row in rows if str(row["Rd12_Physic_Cd"]).strip() == product
                   and str(row["Rd12_Io_Gu"]).strip() in scope.outbound_codes
                   and cutoff >= str(row["Rd12_Out_YyMmDd"]).strip()]
        late = any(str(row.get(field) or "")[:10].replace("-", "") > cutoff
                   for row in history for field in ("Rd12_Add_Time", "Rd12_Mod_Time"))
        if item["status"] != "ready":
            withheld += 1
            continue
        if late:
            raise ValueError("ready prediction includes late-registered or modified history")
        actual = sum((Decimal(str(row["Rd12_Quantity"])) for row in rows
                      if str(row["Rd12_Physic_Cd"]).strip() == product
                      and str(row["Rd12_Io_Gu"]).strip() in scope.outbound_codes
                      and cutoff < str(row["Rd12_Out_YyMmDd"]).strip() <= end), Decimal(0))
        if actual != Decimal(item["actual_quantity"]):
            raise ValueError("30-calendar-day label differs from raw R120")
        checked += 1
    return {"company_id": 3, "source_rows": len(rows), "products": len(product_codes),
            "checked_ready_model_rows": checked, "withheld_model_rows": withheld,
            "evaluation_grade": "research_posthoc"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(check(args.dataset, args.run), ensure_ascii=False))


if __name__ == "__main__":
    main()
