"""Offline official-document links, approval plan and security classification gate."""
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.knowledge_document_manage_cli import validate_plan
from app.services.knowledge_scope_policy import can_read_document

DOCUMENTS = (
    'docs/README.md',
    'docs/02_design/ORDER_CALCULATION_PHASE1_BUSINESS_CONTRACT.md',
    'docs/02_design/SIMS_AI_NLQ_기간정책_공식기준.md',
    'docs/02_design/SIMS_NLQ_CURRENT_STOCK_CURRENT_TABLE_CONTRACT.md',
    'docs/03_runbook/SIMS_AI_업무질문_사용_예시.md',
    'docs/03_runbook/RUNBOOK_SIMSAI.md',
    'docs/04_test_results/ORDER_CALCULATION_PHASE1_CLOSEOUT_20260914.md',
    'docs/00_roadmap/SIMS_AI_PLATFORM_ROADMAP_20260903.md',
    'docs/00_roadmap/SIMS_AI_PLATFORM_EXPECTED_SCHEDULE_20260903.md',
)

def main():
    checked = 0
    for name in DOCUMENTS:
        path = ROOT / name
        text = path.read_text(encoding='utf-8-sig')
        for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)', text):
            target = target.strip('<>')
            if re.match(r'[A-Za-z]+://', target):
                continue
            relative, _, anchor = unquote(target).partition('#')
            destination = (path.parent / relative).resolve() if relative else path
            assert destination.exists(), (name, target)
            if anchor and destination.suffix == '.md':
                headings = re.findall(r'^#+\s+(.+)$', destination.read_text(encoding='utf-8-sig'), re.M)
                slugs = [re.sub(r'[^\w -]', '', h.lower()).replace(' ', '-') for h in headings]
                assert anchor in slugs, (name, target)
            checked += 1
    plan_path = ROOT / 'docs/03_runbook/ORDER_CALCULATION_PHASE1.knowledge.json'
    catalog = json.loads(plan_path.read_text(encoding='utf-8'))
    assert catalog['items'] == [] and catalog['status'] == 'INDEX_ONLY_NOT_APPLICABLE'
    references = catalog['external_registration_plans']
    order_items = tuple(item for relative in references[2:]
                        for item in validate_plan(plan_path.parent / relative))
    guide_path = plan_path.parent / 'SIMS_AI_업무질문_사용_예시.knowledge.json'
    guide_items = validate_plan(guide_path)
    stock_path = plan_path.parent / '../02_design/SIMS_NLQ_CURRENT_STOCK_CURRENT_TABLE_CONTRACT.knowledge.json'
    stock_items = validate_plan(stock_path)
    assert len(order_items) == 3 and len(guide_items) == 1 and len(stock_items) == 1
    assert references == [guide_path.name, '../02_design/' + stock_path.name,
                          '../02_design/ORDER_CALCULATION_PHASE1_BUSINESS_CONTRACT.knowledge.json',
                          '../02_design/SIMS_AI_NLQ_기간정책_공식기준.knowledge.json',
                          '../04_test_results/ORDER_CALCULATION_PHASE1_CLOSEOUT_20260914.knowledge.json']
    assert guide_items[0].version == 3 and len(guide_items[0].search_aliases) == 27
    assert len(set(guide_items[0].search_aliases)) == 27
    items = guide_items + stock_items + order_items
    assert len(items) == 5 and len({item.source_key for item in items}) == 5
    for item in items:
        assert '90_archive' not in item.content and 'probe_v2' not in item.source_key
        doc = {'source_kind': 'DOCUMENT', 'scope': item.scope, 'company_id': item.company_id,
               'user_id': item.user_id, 'status': 'ACTIVE',
               'knowledge_classification': item.knowledge_classification}
        def access(permissions, company=7, technical=False):
            return can_read_document(document=doc, current_user_id=1, current_company_id=company,
                permission_codes=permissions, technical_detail_mode=technical).allowed
        assert not access(())
        if item.knowledge_classification == 'GENERAL':
            assert access(('RAG_USE',))
            assert not re.search(r'\bDB\b|\bSQL\b|Rddbc\d+|Rd\d+_|source_call_count', item.content, re.I)
            assert '2,798.19' not in item.content and '12,233.46' not in item.content
        else:
            assert not access(('RAG_USE',))
            assert not access(('RAG_USE', 'KNOWLEDGE_COMPANY_MANAGE'), technical=True)
            assert access(('RAG_USE', 'KNOWLEDGE_ERP_DB_READ'), technical=True)
            if item.scope == 'COMPANY':
                assert not access(('RAG_USE', 'KNOWLEDGE_ERP_DB_READ'), company=4, technical=True)
    contract = (ROOT / DOCUMENTS[1]).read_text(encoding='utf-8')
    assert all(value not in contract for value in ('64063', '83315', '23976', '2,798.19', '12,233.46', '9,200', '3,240', '3240'))
    order = next(item for item in order_items if item.source_key == 'document:order-calculation-phase1-business-contract')
    assert (order.version, order.scope, order.company_id) == (2, 'GLOBAL', None)
    assert '7283865dcf8709ec9673404b0321adfe208c65be' in contract
    assert 'build_contract_price_params' in contract
    print(json.dumps({'gate': 'PASS', 'documents': len(DOCUMENTS), 'local_links': checked,
        'rag_candidates': len(items), 'security_classification': 'PASS', 'operating_rag_writes': 0}, ensure_ascii=False))

if __name__ == '__main__':
    main()
