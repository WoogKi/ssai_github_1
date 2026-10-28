"""Read-only, low-overhead status monitor for an independent ML worker."""

from __future__ import annotations

import argparse
import json
import re
import time

from app.ml.integration import ml_storage_root


def read_status(run_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
        raise ValueError("invalid run_id")
    path = ml_storage_root() / "runs" / run_id / "status.json"
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("monitor interval must be at least one second")
    last = None
    try:
        while True:
            record = read_status(args.run_id)
            if record != last:
                print(json.dumps(record, ensure_ascii=False), flush=True)
                last = record
            if not args.watch or record.get("stage") == "complete" or record.get("event") in {"ERROR", "CANCEL"}:
                return
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
