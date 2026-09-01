"""Focused JSON persistence regression for pandas/numpy temporal values."""
from __future__ import annotations

import ast
import datetime as dt
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


def _load_sanitizer():
    source = Path("app/Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "_json_sanitize")
    namespace = {"math": math}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "_json_sanitize_fixture", "exec"), namespace)
    return namespace["_json_sanitize"]


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    sanitize = _load_sanitizer()
    stamp = pd.Timestamp("2026-08-31 16:12:14")
    room = {
        "created_at": stamp,
        "meta": {"updated_at": stamp, "items": [stamp, {"nested": stamp}]},
        "frame": pd.DataFrame({"when": [stamp, pd.NaT], "amount": [np.int64(7), np.nan]}),
        "tuple": (dt.date(2026, 8, 31), np.float64(1.5), pd.NaT),
    }

    sanitized = sanitize(room)
    serialized = json.dumps(sanitized, ensure_ascii=False, indent=2)
    restored = json.loads(serialized)

    _assert(restored["created_at"] == "2026-08-31T16:12:14", "top-level Timestamp format changed")
    _assert(restored["meta"]["items"][0] == "2026-08-31T16:12:14", "list Timestamp escaped sanitizer")
    _assert(restored["meta"]["items"][1]["nested"] == "2026-08-31T16:12:14", "nested Timestamp escaped sanitizer")
    _assert(restored["frame"]["records"][0]["when"] == "2026-08-31T16:12:14", "DataFrame Timestamp escaped sanitizer")
    _assert(restored["frame"]["records"][1]["when"] is None, "NaT must persist as null")
    _assert(restored["frame"]["records"][1]["amount"] is None, "NaN must persist as null")
    _assert(restored["tuple"][0] == "2026-08-31" and restored["tuple"][2] is None, "date/tuple contract changed")

    # Same representation used by the all-room ZIP writer.
    zip_room_json = json.dumps(sanitize({"name": "fixture", "messages": [room]}), ensure_ascii=False, indent=2)
    _assert("2026-08-31T16:12:14" in zip_room_json, "ZIP room serialization regression")
    print("RESULT: OK - Timestamp/NaT/NaN recursive room and ZIP serialization")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
