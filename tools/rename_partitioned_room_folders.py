#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Dry-run/apply tool to rename partitioned chat room folders.

The tool is intentionally conservative:
- Dry-run is the default.
- The legacy user_{id}_chat_rooms.json file is read-only input.
- Backup partition folders are read-only input.
- --apply updates only the active partitioned user_{id} folder.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


FORBIDDEN_WIN_CHARS_RE = re.compile(r'[<>:"/\\|?*]+')
ROOM_NAME_DATE_RE = re.compile(
    r"^\s*(\d{4})-(\d{2})-(\d{2})[ T_]+(\d{1,2})[:\-_](\d{2})(?:[:\-_]\d{2})?\s+(.+?)\s*$"
)


def safe_title_slug(title: Any, limit: int = 48) -> str:
    text = str(title or "new chat").strip()
    text = FORBIDDEN_WIN_CHARS_RE.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    text = text.replace(" ", "_")
    text = re.sub(r"_+", "_", text).strip("._ ")
    return (text[:limit].strip("._ ") or "new_chat")


def split_room_name_datetime_prefix(name: Any) -> tuple[str | None, str]:
    text = str(name or "").strip()
    m = ROOM_NAME_DATE_RE.match(text)
    if not m:
        return None, text
    date_part = f"{m.group(1)}-{m.group(2)}-{m.group(3)}_{int(m.group(4)):02d}-{m.group(5)}"
    title = str(m.group(6) or "").strip()
    return date_part, title


def short_room_id(room_id: Any, length: int = 8) -> str:
    s = re.sub(r"[^0-9A-Za-z]+", "", str(room_id or ""))
    return (s[:length] or uuid.uuid4().hex[:length]).lower()


def readable_dirname_for_name(room: dict[str, Any], name: str | None = None) -> str:
    rid = str(room.get("id") or uuid.uuid4())
    raw_name = str(name if name is not None else (room.get("name") or room.get("title") or "new chat")).strip()
    date_part, title_text = split_room_name_datetime_prefix(raw_name)
    if not date_part:
        created = str(room.get("created_at") or "")
        digits = re.sub(r"[^0-9]", "", created)[:12]
        if len(digits) >= 12:
            date_part = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}_{digits[8:10]}-{digits[10:12]}"
        else:
            date_part = datetime.now().strftime("%Y-%m-%d_%H-%M")
        title_text = raw_name
    return f"{date_part}_{safe_title_slug(title_text or raw_name)}__{short_room_id(rid)}"[:150].rstrip(" .")


def readable_dirname(room: dict[str, Any]) -> str:
    return readable_dirname_for_name(room)


def safe_uuid_dirname(room_id: Any) -> str:
    s = str(room_id or "").strip() or str(uuid.uuid4())
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", s)[:120] or str(uuid.uuid4())


def rel_messages(dirname: str) -> str:
    return str(Path("rooms") / dirname / "messages.jsonl")


def room_dir_from_meta(root: Path, room: dict[str, Any]) -> Path:
    rel = str(room.get("relative_path") or "").strip()
    if rel:
        p = Path(rel)
        if not p.is_absolute() and ".." not in p.parts:
            if p.name == "messages.jsonl":
                p = p.parent
            candidate = (root / p).resolve()
            try:
                candidate.relative_to(root.resolve())
                return candidate
            except Exception:
                pass
    return root / "rooms" / safe_uuid_dirname(room.get("id"))


def load_rooms(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rooms_path = root / "rooms.json"
    doc = json.loads(rooms_path.read_text(encoding="utf-8"))
    rooms = doc.get("rooms") if isinstance(doc, dict) else doc
    if not isinstance(rooms, list):
        raise ValueError("rooms.json does not contain a rooms list")
    return doc if isinstance(doc, dict) else {"rooms": rooms}, rooms


def _iter_legacy_rooms(doc: Any) -> list[dict[str, Any]]:
    if isinstance(doc, dict):
        rooms = doc.get("rooms") or doc.get("chat_rooms") or []
    elif isinstance(doc, list):
        rooms = doc
    else:
        rooms = []
    return [r for r in rooms if isinstance(r, dict)]


def load_original_room_names(chat_root: Path, user_id: str) -> dict[str, dict[str, str]]:
    originals: dict[str, dict[str, str]] = {}

    def add_source(path: Path, source: str) -> None:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        for room in _iter_legacy_rooms(doc):
            rid = str(room.get("id") or "").strip()
            name = str(room.get("name") or room.get("title") or "").strip()
            if rid and name and rid not in originals:
                originals[rid] = {"name": name, "source": source}

    add_source(chat_root / f"user_{user_id}_chat_rooms.json", "legacy_json")

    for candidate in sorted(chat_root.glob(f"user_{user_id}*backup*")):
        rooms_json = candidate / "rooms.json"
        if rooms_json.exists():
            add_source(rooms_json, f"partition_backup:{candidate.name}")

    return originals


def build_plan(root: Path, originals: dict[str, dict[str, str]] | None = None) -> list[dict[str, Any]]:
    _, rooms = load_rooms(root)
    originals = originals or {}
    used: set[str] = set()
    plan: list[dict[str, Any]] = []
    for room in rooms:
        if not isinstance(room, dict):
            continue
        rid = str(room.get("id") or "")
        if not rid:
            continue
        current_name = str(room.get("name") or room.get("title") or "")
        original = originals.get(rid) or {}
        original_name = str(original.get("name") or "")
        proposed_name = original_name if original_name and original_name != current_name else current_name
        old_dir = room_dir_from_meta(root, room)
        target_name = readable_dirname_for_name(room, proposed_name)
        base = target_name
        suffix = 1
        while target_name.lower() in used:
            suffix += 1
            target_name = f"{base}_{suffix}"
        used.add(target_name.lower())
        new_dir = root / "rooms" / target_name
        needs_title_restore = bool(original_name and original_name != current_name)
        needs_rename = old_dir.name != target_name
        exists_conflict = new_dir.exists() and old_dir.resolve() != new_dir.resolve()
        too_long = len(str(new_dir)) >= 240
        duplicated_date = bool(re.search(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}_\d{4}-\d{2}-\d{2}", target_name))
        plan.append(
            {
                "room_id": rid,
                "current_name": current_name,
                "original_name": original_name,
                "proposed_name": proposed_name,
                "source": str(original.get("source") or ""),
                "old_dir": old_dir,
                "new_dir": new_dir,
                "new_relative_path": rel_messages(target_name),
                "needs_title_restore": needs_title_restore,
                "needs_rename": needs_rename,
                "exists_conflict": exists_conflict,
                "too_long": too_long,
                "duplicated_date": duplicated_date,
            }
        )
    return plan


def write_rooms_index(root: Path, rooms: list[dict[str, Any]]) -> None:
    rows = []
    for room in rooms:
        rid = str(room.get("id") or "")
        rel = str(room.get("relative_path") or rel_messages(safe_uuid_dirname(rid)))
        msg_path = root / rel
        try:
            size = int(msg_path.stat().st_size) if msg_path.exists() else 0
        except Exception:
            size = 0
        rows.append(
            {
                "room_id": rid,
                "room_name": str(room.get("name") or room.get("title") or ""),
                "created_at": str(room.get("created_at") or ""),
                "updated_at": str(room.get("updated_at") or ""),
                "company_id": str(room.get("company_id") or ""),
                "message_count": int(room.get("message_count") or 0),
                "messages_file_bytes": size,
                "messages_file_mb": f"{size / (1024 * 1024):.2f}",
                "relative_path": rel,
            }
        )
    tmp = root / f"rooms_index.{uuid.uuid4().hex}.tmp"
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["room_id", "relative_path"])
        writer.writeheader()
        writer.writerows(rows)
    os.replace(str(tmp), str(root / "rooms_index.csv"))


def apply_plan(root: Path, plan: list[dict[str, Any]]) -> None:
    doc, rooms = load_rooms(root)
    backup = root.parent / f"{root.name}.folder_rename_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    renamed: list[tuple[Path, Path]] = []
    try:
        if backup.exists():
            raise RuntimeError(f"backup path already exists: {backup}")
        shutil.copytree(root, backup)
        for item in plan:
            if not item["needs_rename"]:
                continue
            if item["exists_conflict"] or item["too_long"] or item["duplicated_date"]:
                raise RuntimeError(f"unsafe target for room {item['room_id']}: {item['new_dir']}")
            old_dir: Path = item["old_dir"]
            new_dir: Path = item["new_dir"]
            if not old_dir.exists():
                raise RuntimeError(f"source folder not found: {old_dir}")
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(old_dir), str(new_dir))
            renamed.append((new_dir, old_dir))
            room_json = new_dir / "room.json"
            if room_json.exists():
                room_doc = json.loads(room_json.read_text(encoding="utf-8"))
                if isinstance(room_doc, dict):
                    room_doc["name"] = item["proposed_name"]
                    room_doc["relative_path"] = item["new_relative_path"]
                    room_json.write_text(json.dumps(room_doc, ensure_ascii=False, indent=2), encoding="utf-8")
        by_id = {item["room_id"]: item for item in plan}
        for room in rooms:
            rid = str(room.get("id") or "")
            item = by_id.get(rid)
            if item:
                room["name"] = item["proposed_name"]
                room["relative_path"] = item["new_relative_path"]
        doc["rooms"] = rooms
        tmp = root / f"rooms.{uuid.uuid4().hex}.tmp"
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(root / "rooms.json"))
        write_rooms_index(root, rooms)
        print(f"APPLY_OK backup={backup}")
    except Exception:
        for new_dir, old_dir in reversed(renamed):
            try:
                if new_dir.exists() and not old_dir.exists():
                    os.replace(str(new_dir), str(old_dir))
            except Exception:
                pass
        print(f"APPLY_FAILED rollback_attempted=True backup={backup}", file=sys.stderr)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--chat-root", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    chat_root = Path(args.chat_root)
    root = chat_root / f"user_{args.user_id}"
    originals = load_original_room_names(chat_root, str(args.user_id))
    plan = build_plan(root, originals)
    targets = [item for item in plan if item["needs_rename"] or item["needs_title_restore"]]
    print(f"ROOT={root}")
    print(f"MODE={'apply' if args.apply else 'dry-run'}")
    print(f"ROOMS={len(plan)} RENAME_TARGETS={len([i for i in plan if i['needs_rename']])} TITLE_RESTORE_TARGETS={len([i for i in plan if i['needs_title_restore']])}")
    for item in plan:
        print(
            "ROOM "
            f"id={item['room_id']} "
            f"rename={item['needs_rename']} "
            f"title_restore={item['needs_title_restore']} "
            f"conflict={item['exists_conflict']} "
            f"too_long={item['too_long']} "
            f"duplicated_date={item['duplicated_date']} "
            f"source={item['source']} "
            f"current_name={item['current_name']} "
            f"original_name={item['original_name']} "
            f"proposed_name={item['proposed_name']} "
            f"old={item['old_dir']} "
            f"new={item['new_dir']}"
        )
    if any(item["exists_conflict"] or item["too_long"] or item["duplicated_date"] for item in plan):
        print("VALIDATION=FAIL")
        return 2
    print("VALIDATION=OK")
    if args.apply:
        apply_plan(root, plan)
    else:
        print(
            "DRY_RUN_ONLY apply_command=python tools/rename_partitioned_room_folders.py "
            f"--user-id {args.user_id} --chat-root {args.chat_root} --apply"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
