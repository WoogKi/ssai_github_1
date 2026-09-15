"""Offline Wave 5 source, alias, link and effective-policy fixtures."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.knowledge_document_manage_cli import validate_plan
from app.services.knowledge_scope_policy import can_read_document
from app.services.knowledge_role_policy import KNOWLEDGE_ROLE_PERMISSION_MATRIX

PLANS = (
    "docs/03_runbook/RUNBOOK_SIMSAI.knowledge.json",
    "docs/03_runbook/RUNBOOK_2HO_OPERATION_CHECK.knowledge.json",
    "docs/02_design/KNOWLEDGE_ACCESS_POLICY_CONTRACT.knowledge.json",
)
EXPECTED_VERSIONS = {
    "docs/03_runbook/RUNBOOK_SIMSAI.knowledge.json": 2,
    "docs/03_runbook/RUNBOOK_2HO_OPERATION_CHECK.knowledge.json": 1,
    "docs/02_design/KNOWLEDGE_ACCESS_POLICY_CONTRACT.knowledge.json": 1,
}


def main() -> int:
    keys = set()
    rows = []
    permission_assertions = links = 0
    for name in PLANS:
        plan = ROOT / name
        items = validate_plan(plan)
        assert len(items) == 1
        item = items[0]
        assert (
            item.version,
            item.scope,
            item.company_id,
            item.user_id,
            item.knowledge_classification,
        ) == (EXPECTED_VERSIONS[name], "GLOBAL", None, None, "ERP_DB_INTERNAL")
        assert item.source_key not in keys
        keys.add(item.source_key)
        assert len(set(item.search_aliases)) == len(item.search_aliases)
        raw = json.loads(plan.read_text(encoding="utf-8"))["items"][0]
        source = plan.parent / raw["content_file"]
        assert source.name == item.source_name
        assert not re.search(r"[\u3040-\u30ff]", item.content)
        for target in re.findall(r"\]\(([^)]+)\)", item.content):
            if "://" in target:
                continue
            assert (source.parent / target.split("#", 1)[0]).exists(), target
            links += 1
        doc = dict(source_kind="DOCUMENT", scope=item.scope, company_id=item.company_id, user_id=None,
                   status="ACTIVE", knowledge_classification=item.knowledge_classification)
        for company in (7, 4):
            for role, permissions in KNOWLEDGE_ROLE_PERMISSION_MATRIX.items():
                for technical in (True, False):
                    expected = role in ("SYSTEM_ADMIN", "SSART_MANAGER") and technical
                    decision = can_read_document(document=doc, current_user_id=1, current_company_id=company,
                        permission_codes=permissions, technical_detail_mode=technical)
                    assert decision.allowed == expected
                    permission_assertions += 1
        for permissions in ((), ("RAG_USE",), ("KNOWLEDGE_ERP_DB_READ",)):
            assert not can_read_document(document=doc, current_user_id=1, current_company_id=7,
                permission_codes=permissions, technical_detail_mode=True).allowed
            permission_assertions += 1
        rows.append(dict(plan=name, source_key=item.source_key, aliases=len(item.search_aliases),
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            content_sha256=hashlib.sha256(item.content.encode("utf-8")).hexdigest(), validate="PASS"))
    for plan in (ROOT / "docs").rglob("*.knowledge.json"):
        if plan.relative_to(ROOT).as_posix() in PLANS or "90_archive" in plan.parts:
            continue
        for item in json.loads(plan.read_text(encoding="utf-8-sig")).get("items", []):
            assert item.get("source_key") not in keys, plan
    print(json.dumps(dict(status="PASS", permission_assertions=permission_assertions, links=links,
                         operating_write_count=0, items=rows), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
