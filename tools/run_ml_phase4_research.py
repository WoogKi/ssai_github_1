"""Run a clearly labelled posthoc forecast on a verified DB3 extract."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from app.ml.integration import ml_storage_root
from app.ml.research import create_research_dataset
from app.services.ssai_analysis_profile_service import load_dashboard_profile_checked
from tools.run_ml_phase1_offline import run_fixture


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    try:
        profile = load_dashboard_profile_checked(company_id=3)
        if profile.status != "ready" or not profile.profile:
            raise ValueError("company 3 saved Dashboard profile unavailable")
        profile_bytes = json.dumps(profile.profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        root = ml_storage_root()
        dataset = create_research_dataset(
            args.source.resolve(), root,
            saved_io_codes=list(profile.profile.get("io_gu_list") or []),
            profile_fingerprint=hashlib.sha256(profile_bytes).hexdigest(),
        )
        summary = run_fixture(dataset / "fixture.json", root / "runs", run_id=dataset.name)
        forecasts = json.loads((root / "runs" / dataset.name / "predictions.json").read_text(encoding="utf-8"))
        product_codes = sorted({row["product_code"] for row in forecasts})
        aliases = {code: f"P{index + 1}" for index, code in enumerate(product_codes)}
        observations = [
            {"as_of": row["as_of"], "product": aliases[row["product_code"]], "method": row["method"],
             "status": row["status"], "actual": row["actual_quantity"], "predicted": row["predicted_quantity"]}
            for row in forecasts
        ]
        print(json.dumps({"dataset_version": dataset.name, "evaluation_grade": "research_posthoc",
                          "source_rows": summary["source_row_count"], "model_metrics": summary["models"],
                          "observations": observations}, ensure_ascii=False))
    except Exception as exc:
        parser.exit(1, f"ML research run stopped: {type(exc).__name__}\n")


if __name__ == "__main__":
    main()
