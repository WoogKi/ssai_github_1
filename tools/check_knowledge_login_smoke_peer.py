"""Compare saved read-only reports; never connect to or modify operating storage."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.check_knowledge_final_readonly_smoke import active_identity

KEY = "project-source:app/services/ssai_storage_service.py#get_user_file_path"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("local", type=Path)
    parser.add_argument("peer", type=Path)
    args = parser.parse_args()
    reports = [json.loads(p.read_text(encoding="utf-8-sig")) for p in (args.local, args.peer)]
    counts = [Counter(c["name"] for c in r["checks"]) for r in reports]
    difference = {name: dict(local=counts[0][name], peer=counts[1][name])
                  for name in sorted(set(counts[0]) | set(counts[1]))
                  if counts[0][name] != counts[1][name]}
    history_only = set(difference).issubset({"artifact_checksum", "inactive_policy_denied"})
    equal = active_identity(reports[0]) == active_identity(reports[1])
    passed = equal and history_only and all(r["status"] == "PASS" and
        r["operating_write_count"] == 0 and all(c["status"] == "PASS" for c in r["checks"])
        for r in reports)
    print(json.dumps(dict(status="PASS" if passed else "REVIEW", active_equal=equal,
        history_only_count_difference=history_only, difference=difference,
        check_counts=[len(r["checks"]) for r in reports],
        servers=[dict(manifest_root=r["manifest_root"], total_versions=r["total_versions"],
            active_count=r["active_count"], status_counts=r["status_counts"],
            project_source_v9=[s for s in r["active"] if s["source_key"] == KEY and s["version"] == 9])
            for r in reports], operating_write_count=0), ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
