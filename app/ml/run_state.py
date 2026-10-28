"""DB-free, per-run progress files for a future isolated worker."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


KST = timezone(timedelta(hours=9))


class RunState:
    def __init__(self, root: Path, *, run_id: str, dataset_version: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
            raise ValueError("invalid run_id")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", dataset_version):
            raise ValueError("invalid dataset_version")
        self.path = root / run_id
        self.path.mkdir(parents=True, exist_ok=False)
        self.run_id = run_id
        self.dataset_version = dataset_version
        self.started = time.monotonic()

    def update(self, *, stage: str, event: str, done: int, total: int | None = None,
               error_code: str | None = None) -> dict[str, object]:
        if event not in {"START", "PROGRESS", "DONE", "ERROR", "CANCEL"}:
            raise ValueError("invalid event")
        if not re.fullmatch(r"[a-z_]{1,50}", stage):
            raise ValueError("invalid stage")
        if done < 0 or (total is not None and (total < 0 or done > total)):
            raise ValueError("invalid progress")
        if error_code is not None and not re.fullmatch(r"[A-Z_]{1,60}", error_code):
            raise ValueError("invalid error code")
        elapsed_ms = int((time.monotonic() - self.started) * 1000)
        record: dict[str, object] = {
            "timestamp_kst": datetime.now(KST).isoformat(), "run_id": self.run_id,
            "dataset_version": self.dataset_version, "stage": stage, "event": event,
            "completed_count": done, "total_count": total,
            "progress_percent": round(done * 100 / total, 2) if total else None,
            "duration_ms": elapsed_ms, "error_code": error_code,
        }
        status_path = self.path / "status.json"
        tmp_path = self.path / "status.json.tmp"
        tmp_path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        for attempt in range(3):
            try:
                os.replace(tmp_path, status_path)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.02)
        with (self.path / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return record
