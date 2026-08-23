"""Offline timing contracts for the read-only snapshot preflight tool."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tools" / "diagnose_ssai_analytics_snapshot_environment.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("snapshot_preflight_timing", SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError("snapshot preflight module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Cursor:
    def execute(self, _query: str):
        return self

    def fetchone(self):
        return (1,)


class _Connection:
    def cursor(self):
        return _Cursor()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def main() -> None:
    module = _load_module()
    module.get_company_db_config = lambda company_id: {"company_id": company_id}
    module.build_company_conn_str = lambda _config: "fixture"
    module.replace = lambda config, **_kwargs: config
    module.pyodbc.connect = lambda *_args, **_kwargs: _Connection()
    module.TARGET_SMS_IDS = (1, 2, 4, 6, 7, 8)

    original_argv = sys.argv
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sys.argv = [str(SOURCE), "--output-dir", tmp_dir]
            console = io.StringIO()
            with contextlib.redirect_stdout(console):
                assert module.main() == 0
            output_dir = Path(tmp_dir)
            payload = json.loads(next(output_dir.glob("*.json")).read_text(encoding="utf-8"))
            text = next(output_dir.glob("*.txt")).read_text(encoding="utf-8")
            rendered = console.getvalue()
    finally:
        sys.argv = original_argv

    assert payload["target_count"] == 6
    assert payload["pass_count"] == 6
    assert payload["overall_elapsed_seconds"] >= 0
    assert payload["overall_started_at"]
    assert payload["overall_finished_at"]
    assert "전체 시작시간:" in text and "전체 종료시간:" in text and "총 소요초:" in text
    assert "시작시간 | 종료시간 | 소요초" in text
    assert "전체 시작시간:" in rendered and "전체 종료시간:" in rendered and "총 소요초:" in rendered
    for item in payload["targets"]:
        assert item["verdict"] == "PASS"
        assert item["elapsed_seconds"] >= 0
        assert item["started_at"] and item["finished_at"]
        row = f"{item['sms']} | {item['started_at']} | {item['finished_at']} | {item['elapsed_seconds']:.3f}"
        assert row in text and row in rendered
    print("RESULT OK tests=16 targets=6 timing_json_txt_console=consistent db_calls=fixture_only")


if __name__ == "__main__":
    main()