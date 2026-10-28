"""Run a bounded research-grade CPU model without ERP or external API access."""

from __future__ import annotations

import argparse
import json
from datetime import datetime

from app.ml.integration import ml_storage_root
from app.ml.learning import train_research


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--estimators", type=int, default=40)
    args = parser.parse_args()
    version = args.dataset_version
    if not version.startswith("db3-research-") or not all(c.isalnum() or c in "._-" for c in version):
        parser.error("only a named DB3 research dataset is supported")
    root = ml_storage_root()
    run_id = args.run_id or f"ml-phase5-{datetime.now():%Y%m%d%H%M%S}"
    result = train_research(root / "datasets" / version / "fixture.json", root,
                            run_id=run_id, estimators=args.estimators,
                            cancel_check=lambda: (root / "runs" / run_id / "cancel.request").exists())
    print(json.dumps({key: result[key] for key in ("run_id", "dataset_version", "evaluation_grade",
                                                     "train_samples", "evaluation_samples", "metrics",
                                                     "training_ms", "forecast_ms")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
