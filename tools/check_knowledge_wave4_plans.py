"""File-only Wave 4 plan, source identity and authorization fixtures."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.knowledge_document_manage_cli import validate_plan
from app.services.knowledge_scope_policy import can_read_document
from app.services.knowledge_role_policy import KNOWLEDGE_ROLE_PERMISSION_MATRIX

PLANS = (
    "docs/02_design/ORDER_CALCULATION_PHASE1_BUSINESS_CONTRACT.knowledge.json",
    "docs/02_design/SIMS_AI_NLQ_기간정책_공식기준.knowledge.json",
    "docs/04_test_results/ORDER_CALCULATION_PHASE1_CLOSEOUT_20260914.knowledge.json",
)


def main() -> int:
    keys = set()
    rows = []
    assertions = 0
    for index, name in enumerate(PLANS):
        path = ROOT / name
        items = validate_plan(path)
        assert len(items) == 1
        item = items[0]
        assert item.version == (2 if index == 0 else 1) and item.user_id is None
        assert item.knowledge_classification == "ERP_DB_INTERNAL"
        assert (item.scope, item.company_id) == (("COMPANY", 7) if index == 2 else ("GLOBAL", None))
        assert item.source_key not in keys
        keys.add(item.source_key)
        assert len(set(item.search_aliases)) == len(item.search_aliases)
        doc = dict(source_kind="DOCUMENT", scope=item.scope, company_id=item.company_id,
                   user_id=None, status="ACTIVE", knowledge_classification=item.knowledge_classification)
        for role in ("SYSTEM_ADMIN", "SSART_MANAGER", "SSART_STAFF", "WHOLESALE_MANAGER", "WHOLESALE_STAFF"):
            for company in (7, 4):
                for technical in (True, False):
                    decision = can_read_document(document=doc, current_user_id=1, current_company_id=company,
                        permission_codes=KNOWLEDGE_ROLE_PERMISSION_MATRIX[role], technical_detail_mode=technical)
                    expected = role in ("SYSTEM_ADMIN", "SSART_MANAGER") and technical and (index != 2 or company == 7)
                    assert decision.allowed == expected, (name, role, company, technical, decision)
                    assertions += 1
        for permissions in ((), ("KNOWLEDGE_ERP_DB_READ",), ("RAG_USE",)):
            assert not can_read_document(document=doc, current_user_id=1, current_company_id=7,
                permission_codes=permissions, technical_detail_mode=True).allowed
            assertions += 1
        raw = json.loads(path.read_text(encoding="utf-8"))["items"][0]
        source = path.parent / raw["content_file"]
        relative = source.relative_to(ROOT).as_posix()
        head = subprocess.run(["git", "show", "HEAD:" + relative], cwd=ROOT, capture_output=True, check=True).stdout
        head_content = head.decode("utf-8").replace("\r\n", "\n")
        matches_head = item.content.replace("\r\n", "\n") == head_content
        if index != 0:
            assert matches_head, relative
        assert "3,240" not in item.content and "3240" not in item.content
        if index == 2:
            assert all(value in item.content for value in ("64063", "83315", "23976", "2,798.19", "12,233.46", "9,200"))
        elif index == 0:
            assert all(value not in item.content for value in ("64063", "83315", "23976", "2,798.19", "12,233.46", "9,200"))
        rows.append(dict(plan=name, source_key=item.source_key, scope=item.scope, company_id=item.company_id,
            classification=item.knowledge_classification, version=item.version, aliases=len(item.search_aliases),
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            content_sha256=hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
            head_content_matches=matches_head, validate="PASS"))
    for path in (ROOT / "docs").rglob("*.knowledge.json"):
        if path.relative_to(ROOT).as_posix() in PLANS or "90_archive" in path.parts:
            continue
        for item in json.loads(path.read_text(encoding="utf-8-sig")).get("items", []):
            assert item.get("source_key") not in keys, path
    print(json.dumps(dict(status="PASS", permission_assertions=assertions, operating_write_count=0, items=rows),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
