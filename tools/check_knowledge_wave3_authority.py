from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.knowledge_document_manage_cli import validate_plan


def main() -> int:
    paths = [
        "docs/03_runbook/SIMS_AI_MASTER_INVENTORY_USER_GUIDE",
        "docs/02_design/SIMS_MASTER_PRICE_QUERY_CONTRACT",
        "docs/02_design/PRODUCT_INVENTORY_LEDGER_CONTRACT",
        "docs/02_design/SNAPSHOT_PRODUCT_INFORMATION_V21_CONTRACT",
    ]
    keys: set[str] = set()
    results = []
    for index, name in enumerate(paths):
        source = ROOT / (name + ".md")
        plan = ROOT / (name + ".knowledge.json")
        items = validate_plan(plan)
        assert len(items) == 1
        item = items[0]
        assert item.version == 1 and item.scope == "GLOBAL"
        assert item.company_id is None and item.user_id is None
        assert item.knowledge_classification == ("GENERAL" if index == 0 else "ERP_DB_INTERNAL")
        assert item.source_key not in keys
        keys.add(item.source_key)
        text = source.read_text(encoding="utf-8")
        assert not re.search(r"[\u3040-\u30ff]", text)
        for target in re.findall(r"\]\(([^)]+)\)", text):
            assert (source.parent / target.split("#", 1)[0]).exists(), target
        results.append({"source_key": item.source_key, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "content_sha256": hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
                        "aliases": len(item.search_aliases), "validate": "PASS"})
    master = (ROOT / (paths[1] + ".md")).read_text(encoding="utf-8")
    assert "동률" in master and "시간순 최신" in master
    ledger = (ROOT / (paths[2] + ".md")).read_text(encoding="utf-8")
    assert "price_mode='cons'" in ledger and "대체 경로" in ledger
    snapshot = (ROOT / (paths[3] + ".md")).read_text(encoding="utf-8")
    assert "2-statement" in snapshot and "source_call_count=3" in snapshot
    assert "unknown_lifecycle" in snapshot and "invalid_lifecycle" in snapshot
    for plan in (ROOT / "docs").rglob("*.knowledge.json"):
        if str(plan).endswith(tuple(name.split("/")[-1] + ".knowledge.json" for name in paths)):
            continue
        if "90_archive" in plan.parts:
            continue
        for item in json.loads(plan.read_text(encoding="utf-8-sig")).get("items", []):
            assert item.get("source_key") not in keys, plan
    for name in ("docs/README.md", "docs/04_test_results/KNOWLEDGE_WAVE3_AUTHORITY_REVIEW_20260914.md"):
        source = ROOT / name
        for target in re.findall(r"\]\(([^)]+)\)", source.read_text(encoding="utf-8")):
            if "://" not in target:
                assert (source.parent / target.split("#", 1)[0]).exists(), target
    print(json.dumps({"status": "PASS", "operating_write_count": 0, "items": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
