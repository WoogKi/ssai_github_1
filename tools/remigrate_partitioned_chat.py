from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


CHANNELS = ("messages", "history", "sims_messages", "gen_messages")
MESSAGE_ALLOW_KEYS = {
    "id",
    "message_id",
    "seq",
    "role",
    "type",
    "kind",
    "channel",
    "content",
    "message",
    "title",
    "action",
    "time",
    "created_at",
    "timestamp",
    "datetime",
    "params",
    "table_key",
    "payload_id",
    "source_key",
    "source_action",
    "meta",
}
META_ALLOW_KEYS = {
    "id",
    "message_id",
    "kind",
    "type",
    "action",
    "title",
    "table_key",
    "payload_id",
    "source_key",
    "source_action",
    "source_table_key",
    "source_room_id",
    "company_id",
    "company_name",
    "db_name",
    "created_at",
    "display_time",
    "result_seq",
    "row_count",
    "rows",
    "full_rows",
    "display_rows",
    "expected_rows",
    "column_count",
    "columns",
    "summary_md",
    "summary",
    "query_summary",
    "condition_summary",
    "params",
    "nlq",
    "current_table_followup",
    "supplier_detail_key",
    "supplier_detail_rows",
    "excel_sheet_names",
}
DROP_KEYS = {
    "df",
    "df_display",
    "data",
    "records",
    "full_df",
    "display_df",
    "excel_bytes",
    "csv_bytes",
    "payload",
    "rows_data",
    "supplier_detail_df",
    "product_shortage_df",
}
TEXT_LIMIT = 20000
META_TEXT_LIMIT = 8000
RECORD_MAX_BYTES = 65536


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return len(str(type(value).__name__).encode("utf-8"))


def safe_room_dir(room_id: Any) -> str:
    raw = str(room_id or "").strip() or uuid.uuid4().hex
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in raw)
    return safe[:120] or uuid.uuid4().hex


def room_dir(root: Path, room_id: Any) -> Path:
    return root / "rooms" / safe_room_dir(room_id)


def messages_file(root: Path, room_id: Any) -> Path:
    return room_dir(root, room_id) / "messages.jsonl"


def _safe_relative_path(value: Any) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw.replace("\\", "/").lstrip("/")
    parts = Path(raw).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    return Path(*parts)


def room_dir_from_meta(root: Path, meta: dict[str, Any]) -> Path:
    rel = _safe_relative_path((meta or {}).get("relative_path"))
    if rel is not None:
        # Schema: relative_path may point either to the room directory or messages.jsonl.
        if rel.name.lower() == "messages.jsonl":
            return (root / rel).parent
        return root / rel
    return room_dir(root, (meta or {}).get("id"))


def messages_file_from_meta(root: Path, meta: dict[str, Any]) -> Path:
    rel = _safe_relative_path((meta or {}).get("relative_path"))
    if rel is not None:
        if rel.name.lower() == "messages.jsonl":
            return root / rel
        return root / rel / "messages.jsonl"
    return messages_file(root, (meta or {}).get("id"))


def messages_relative_path(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except Exception:
        return str(Path("rooms") / safe_room_dir(path.parent.name) / "messages.jsonl")


def clip_text(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit].rstrip() + "\n...[truncated]"
    return value


def compact_value(value: Any, *, text_limit: int = META_TEXT_LIMIT, depth: int = 0) -> Any:
    if depth > 4:
        return str(type(value).__name__)
    if isinstance(value, str):
        return clip_text(value, text_limit)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, child in value.items():
            sk = str(key)
            if sk in DROP_KEYS:
                continue
            if len(out) >= 80:
                out["__truncated_keys__"] = True
                break
            out[sk] = compact_value(child, text_limit=text_limit, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        if len(value) > 80:
            return [compact_value(v, text_limit=text_limit, depth=depth + 1) for v in value[:80]] + ["...[truncated]"]
        return [compact_value(v, text_limit=text_limit, depth=depth + 1) for v in value]
    return str(value)


def large_paths(value: Any, prefix: str = "$", out: list[tuple[str, int]] | None = None, depth: int = 0) -> list[tuple[str, int]]:
    if out is None:
        out = []
    if depth > 6:
        return out
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}"
            out.append((path, json_size(child)))
            large_paths(child, path, out, depth + 1)
    elif isinstance(value, list):
        out.append((prefix + "[]", json_size(value)))
        for idx, child in enumerate(value[:5]):
            large_paths(child, f"{prefix}[{idx}]", out, depth + 1)
    return out


def compact_message(message: dict[str, Any], removed_paths: dict[str, int]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in MESSAGE_ALLOW_KEYS:
        if key not in message:
            continue
        value = message.get(key)
        if key in {"content", "message"}:
            value = clip_text(value, TEXT_LIMIT)
        elif key == "meta" and isinstance(value, dict):
            meta_out: dict[str, Any] = {}
            for mk, mv in value.items():
                smk = str(mk)
                if smk in DROP_KEYS:
                    removed_paths[f"$.message.meta.{smk}"] = removed_paths.get(f"$.message.meta.{smk}", 0) + 1
                    continue
                if smk in META_ALLOW_KEYS:
                    meta_out[smk] = compact_value(mv)
            value = meta_out
        else:
            value = compact_value(value)
        out[key] = value
    return out


def message_time(message: dict[str, Any]) -> str:
    for key in ("created_at", "timestamp", "datetime", "time"):
        value = str(message.get(key) or "").strip()
        if value:
            return value
    return ""


def logical_key(message: dict[str, Any]) -> str:
    meta = message.get("meta") if isinstance(message.get("meta"), dict) else {}
    mid = str(message.get("id") or message.get("message_id") or meta.get("message_id") or "").strip()
    if mid:
        return "id:" + mid
    table_key = str(message.get("table_key") or meta.get("table_key") or "").strip()
    if table_key:
        return "table:" + table_key + ":" + str(message.get("action") or meta.get("action") or "")
    sig = {
        "role": message.get("role"),
        "time": message_time(message),
        "action": message.get("action") or meta.get("action"),
        "content": str(message.get("content") or message.get("message") or message.get("title") or "")[:500],
    }
    raw = json.dumps(sig, ensure_ascii=False, sort_keys=True, default=str)
    return "sig:" + hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()


def room_meta(room: dict[str, Any], message_count: int) -> dict[str, Any]:
    out = {
        str(k): compact_value(v)
        for k, v in room.items()
        if k not in CHANNELS and not str(k).startswith("__")
    }
    out.setdefault("id", str(room.get("id") or uuid.uuid4()))
    out.setdefault("name", str(room.get("name") or room.get("title") or "업무 대화"))
    out.setdefault("created_at", str(room.get("created_at") or ""))
    out.setdefault("updated_at", str(room.get("updated_at") or ""))
    out["message_count"] = int(message_count)
    return out


def collect_legacy_rooms(legacy_file: Path) -> list[dict[str, Any]]:
    obj = json.loads(legacy_file.read_text(encoding="utf-8"))
    rooms = obj.get("rooms") if isinstance(obj, dict) else obj
    return rooms if isinstance(rooms, list) else []


def iter_room_messages(room: dict[str, Any]):
    for channel in CHANNELS:
        for msg in room.get(channel) or []:
            if isinstance(msg, dict):
                yield channel, msg


def load_partition_records(partition_root: Path) -> tuple[dict[str, dict[str, Any]], int, int]:
    rooms_doc = partition_root / "rooms.json"
    if not rooms_doc.exists():
        return {}, 0, 0
    try:
        obj = json.loads(rooms_doc.read_text(encoding="utf-8"))
        metas = obj.get("rooms") if isinstance(obj, dict) else obj
    except Exception:
        metas = []
    result: dict[str, dict[str, Any]] = {}
    bad_lines = 0
    record_count = 0
    for meta in metas or []:
        if not isinstance(meta, dict):
            continue
        rid = str(meta.get("id") or "")
        if not rid:
            continue
        entry = result.setdefault(rid, {"meta": meta, "records": []})
        path = messages_file_from_meta(partition_root, meta)
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    bad_lines += 1
                    continue
                msg = rec.get("message") if isinstance(rec, dict) else None
                if not isinstance(msg, dict):
                    continue
                channels = rec.get("channels") if isinstance(rec.get("channels"), list) else [rec.get("channel") or "messages"]
                channels = [str(ch) for ch in channels if str(ch) in CHANNELS] or ["messages"]
                entry["records"].append((channels, msg))
                record_count += 1
    return result, record_count, bad_lines


def merge_rooms(legacy_rooms: list[dict[str, Any]], partition_records: dict[str, dict[str, Any]], removed_paths: dict[str, int]):
    merged: dict[str, dict[str, Any]] = {}
    duplicates = 0
    legacy_count = 0
    partition_added = 0

    def ensure_room(room_id: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        room = merged.setdefault(room_id, {"meta": dict(meta or {"id": room_id}), "messages": {}})
        if meta:
            room["meta"].update({k: v for k, v in meta.items() if k not in CHANNELS})
        return room

    for room in legacy_rooms:
        if not isinstance(room, dict):
            continue
        rid = str(room.get("id") or uuid.uuid4())
        target = ensure_room(rid, room)
        for channel, msg in iter_room_messages(room):
            key = logical_key(msg)
            if key in target["messages"]:
                if channel not in target["messages"][key]["channels"]:
                    target["messages"][key]["channels"].append(channel)
                duplicates += 1
                continue
            target["messages"][key] = {"channels": [channel], "message": msg}
            legacy_count += 1

    for rid, entry in partition_records.items():
        target = ensure_room(rid, entry.get("meta") if isinstance(entry.get("meta"), dict) else {"id": rid})
        for channels, msg in entry.get("records") or []:
            key = logical_key(msg)
            if key in target["messages"]:
                for channel in channels:
                    if channel not in target["messages"][key]["channels"]:
                        target["messages"][key]["channels"].append(channel)
                duplicates += 1
                continue
            target["messages"][key] = {"channels": channels, "message": msg}
            partition_added += 1

    # Force compact once here so verification reflects final schema.
    for room in merged.values():
        for item in room["messages"].values():
            item["message"] = compact_message(item["message"], removed_paths)

    return merged, legacy_count, partition_added, duplicates


def write_store(root: Path, merged: dict[str, dict[str, Any]], legacy_file: Path, current_room: str = "") -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    (root / "rooms").mkdir(parents=True, exist_ok=True)
    stats = {
        "room_count": 0,
        "message_count": 0,
        "max_record_bytes": 0,
        "total_messages_bytes": 0,
        "room_sizes": {},
    }
    meta_rooms: list[dict[str, Any]] = []
    for rid, room in merged.items():
        messages = room["messages"]
        meta = room_meta(room["meta"], len(messages))
        meta_rooms.append(meta)
        rdir = room_dir_from_meta(root, meta)
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "room.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        path = messages_file_from_meta(root, meta)
        meta["relative_path"] = messages_relative_path(root, path)
        rows = sorted(
            messages.items(),
            key=lambda kv: (
                message_time(kv[1]["message"]),
                int(kv[1]["message"].get("seq") or 0) if str(kv[1]["message"].get("seq") or "").isdigit() else 0,
                kv[0],
            ),
        )
        with path.open("w", encoding="utf-8", newline="\n") as f:
            for key, item in rows:
                rec = {
                    "version": 2,
                    "logical_key": key,
                    "channels": item["channels"],
                    "message": item["message"],
                }
                line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"), default=str)
                record_bytes = len(line.encode("utf-8"))
                if record_bytes > RECORD_MAX_BYTES:
                    # Final guard: compact text fields further, but keep metadata refs.
                    msg = dict(item["message"])
                    if isinstance(msg.get("content"), str):
                        msg["content"] = clip_text(msg["content"], 4000)
                    if isinstance(msg.get("message"), str):
                        msg["message"] = clip_text(msg["message"], 4000)
                    rec["message"] = msg
                    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"), default=str)
                    record_bytes = len(line.encode("utf-8"))
                f.write(line)
                f.write("\n")
                stats["message_count"] += 1
                stats["max_record_bytes"] = max(stats["max_record_bytes"], record_bytes)
            f.flush()
        file_bytes = path.stat().st_size
        stats["total_messages_bytes"] += file_bytes
        stats["room_sizes"][rid] = file_bytes
    rooms_doc = {
        "version": 2,
        "storage_mode": "partitioned",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "legacy_chat_file": str(legacy_file),
        "current_room": current_room,
        "rooms": meta_rooms,
    }
    (root / "rooms.json").write_text(json.dumps(rooms_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    write_rooms_index(root, meta_rooms)
    stats["room_count"] = len(meta_rooms)
    return stats


def write_rooms_index(root: Path, meta_rooms: list[dict[str, Any]]) -> None:
    path = root / "rooms_index.csv"
    tmp = root / f"rooms_index.{uuid.uuid4().hex}.tmp"
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "room_id",
                "room_name",
                "created_at",
                "updated_at",
                "company_id",
                "message_count",
                "messages_file_bytes",
                "messages_file_mb",
                "relative_path",
            ],
        )
        writer.writeheader()
        for meta in meta_rooms:
            rid = str(meta.get("id") or "")
            msg_path = messages_file_from_meta(root, meta)
            file_bytes = msg_path.stat().st_size if msg_path.exists() else 0
            writer.writerow(
                {
                    "room_id": rid,
                    "room_name": str(meta.get("name") or meta.get("title") or ""),
                    "created_at": str(meta.get("created_at") or ""),
                    "updated_at": str(meta.get("updated_at") or ""),
                    "company_id": str(meta.get("company_id") or ""),
                    "message_count": int(meta.get("message_count") or 0),
                    "messages_file_bytes": file_bytes,
                    "messages_file_mb": f"{file_bytes / (1024 * 1024):.2f}",
                    "relative_path": messages_relative_path(root, msg_path),
                }
            )
    os.replace(str(tmp), str(path))


def summarize_room_sizes(room_sizes: dict[str, int], top_n: int = 15) -> list[tuple[str, int]]:
    return sorted(room_sizes.items(), key=lambda row: row[1], reverse=True)[:top_n]


def run(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    chat_root = Path(args.chat_root).resolve()
    legacy_file = chat_root / f"user_{args.user_id}_chat_rooms.json"
    partition_root = chat_root / f"user_{args.user_id}"
    if not legacy_file.exists():
        print(f"ERROR legacy file not found: {legacy_file}", file=sys.stderr)
        return 2

    legacy_stat = legacy_file.stat()
    legacy_rooms = collect_legacy_rooms(legacy_file)
    partition_records, partition_record_count, bad_lines = load_partition_records(partition_root)
    removed_paths: dict[str, int] = {}
    merged, legacy_count, partition_added, duplicates = merge_rooms(legacy_rooms, partition_records, removed_paths)

    if args.apply:
        tmp_root = chat_root / f".user_{args.user_id}.remigrate.tmp_{uuid.uuid4().hex}"
    else:
        tmp_root = Path(tempfile.mkdtemp(prefix=f"user_{args.user_id}.remigrate.dryrun."))
    backup_root = chat_root / f"user_{args.user_id}_partition_backup_{now_stamp()}"
    try:
        stats = write_store(tmp_root, merged, legacy_file)
        elapsed = time.perf_counter() - started

        print("MODE", "apply" if args.apply else "dry-run")
        print("LEGACY_FILE", legacy_file)
        print("LEGACY_BYTES", legacy_stat.st_size)
        print("LEGACY_MTIME", datetime.fromtimestamp(legacy_stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"))
        print("LEGACY_ROOMS", len(legacy_rooms))
        print("LEGACY_MESSAGES", legacy_count)
        print("PARTITION_ROOT", partition_root)
        print("PARTITION_RECORDS_READ", partition_record_count)
        print("PARTITION_BAD_LINES", bad_lines)
        print("PARTITION_NEW_MESSAGES_AFTER_DEDUPE", partition_added)
        print("DEDUPLICATED_MESSAGES", duplicates)
        print("FINAL_ROOMS", stats["room_count"])
        print("FINAL_MESSAGES", stats["message_count"])
        print("FINAL_TOTAL_MESSAGES_BYTES", stats["total_messages_bytes"])
        print("FINAL_TOTAL_MESSAGES_MB", f"{stats['total_messages_bytes'] / (1024 * 1024):.2f}")
        print("MAX_RECORD_BYTES", stats["max_record_bytes"])
        print("REMOVED_LARGE_KEY_PATH_COUNTS", json.dumps(removed_paths, ensure_ascii=False, sort_keys=True))
        print("ROOM_SIZE_TOP")
        for rid, size in summarize_room_sizes(stats["room_sizes"]):
            print(" ", rid, size, f"{size / (1024 * 1024):.2f}MB")
        print("TEMP_ROOT", tmp_root)
        print("BACKUP_PATH", backup_root)
        print("ROLLBACK_COMMAND", f"Rename-Item -LiteralPath '{backup_root}' -NewName 'user_{args.user_id}'")
        print("ELAPSED", f"{elapsed:.3f}s")

        if not args.apply:
            if not args.keep_temp:
                shutil.rmtree(tmp_root, ignore_errors=True)
                print("DRY_RUN_TEMP_REMOVED", True)
            return 0

        if partition_root.exists():
            if backup_root.exists():
                raise RuntimeError(f"backup path already exists: {backup_root}")
            os.replace(str(partition_root), str(backup_root))
        try:
            os.replace(str(tmp_root), str(partition_root))
        except Exception:
            if partition_root.exists():
                shutil.rmtree(partition_root, ignore_errors=True)
            if backup_root.exists():
                os.replace(str(backup_root), str(partition_root))
            raise
        print("APPLIED", True)
        print("ACTIVE_ROOT", partition_root)
        return 0
    except Exception as exc:
        shutil.rmtree(tmp_root, ignore_errors=True)
        print("ERROR", type(exc).__name__, str(exc), file=sys.stderr)
        print("LEGACY_UNCHANGED", legacy_file, file=sys.stderr)
        print("PARTITION_UNCHANGED", partition_root, file=sys.stderr)
        return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safely rebuild partitioned SSAI chat storage without modifying legacy JSON.")
    parser.add_argument("--user-id", required=True, help="SSAI user id, e.g. 8")
    parser.add_argument("--chat-root", required=True, help="Chat storage root, e.g. C:\\SSAI_TEST_DATA\\chat")
    parser.add_argument("--apply", action="store_true", help="Apply after validation. Default is dry-run.")
    parser.add_argument("--keep-temp", action="store_true", help="Keep dry-run temporary output for inspection.")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
