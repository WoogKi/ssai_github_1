"""Offline routing and production-dispatch parity checks; no ERP access."""
from datetime import date
from pathlib import Path
from unittest.mock import patch
import json
import logging
import sys
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.erp_table_nlq import resolve_registered_erp_table_nlq, is_order_calculation_request
from app.services.io_nlq import resolve_io_nlq
from app.sims.nlq.nlq_router import _try_handle_io_nlq, resolve_new_sims_nlq_candidate
from app.services.order_calculation_service import get_order_calculation_result
from tools.check_order_calculation_contract import fixture


def run():
    mode_cases = [
        ('한림 안전재고 5일 적정재고 20일 마감일자 25일 조회구분 해당 발주 계산', '한림', True),
        ('환인 조회구분 발주해당자료만 발주 계산', '환인', True),
        ('환인 조회구분 전체 발주 계산', '환인', False),
        ('제품코드 64063 발주할 것만 계산', None, True),
    ]
    for text, maker, needed in mode_cases:
        for resolver in (resolve_registered_erp_table_nlq, resolve_io_nlq):
            result = resolver(text)
            p = result['params']
            assert result['action'] == '발주 계산'
            assert p['only_needed'] == needed
            assert p['query_mode'] == ('발주해당자료만' if needed else '전체')
            if maker:
                assert p['_registered_unlabeled_entity'] == maker
            else:
                assert p['physic_cd'] == '64063'
        if maker == '한림':
            assert (p['safety_days'], p['target_days'], p['closing_day']) == (5, 20, 25)
        assert resolve_new_sims_nlq_candidate(text)['action'] == '발주 계산'
        print(json.dumps({'text': text, **result}, ensure_ascii=False))
    for expression in ('조회구분 해당', '조회구분 발주해당', '조회구분 발주해당자료만',
                       '발주해당자료만', '발주할 것만', '발주 대상만'):
        p = resolve_registered_erp_table_nlq(f'환인 {expression} 발주 계산')['params']
        assert p['_registered_unlabeled_entity'] == '환인' and p['only_needed']
    print('PASS query mode phrases 4/4; aliases 6/6')
    cases = []
    ordinary = ['발주 조회', '발주 내역', '발주 현황', '발주 보여줘', '환인 발주 보여줘',
                '제품코드 27656 발주 조회', '제약사 중외제약 발주 보여줘']
    for text in ordinary:
        assert not is_order_calculation_request(text)
        for resolver in (resolve_registered_erp_table_nlq, resolve_io_nlq):
            assert resolver(text)['action'] == '발주조회', text
        cases.append({'text': text, 'action': '발주조회'})
    calculations = [
        ('환인 발주 계산', 3, 15, 25),
        ('환인 안전재고 3일 적정재고 15일 결제일 25일 발주 계산', 3, 15, 25),
        ('한림 안전재고 5일 적정재고 20일 마감일자 25일 발주 계산', 5, 20, 25),
        ('제품코드 64063 발주 수량 계산', 3, 15, 25),
        ('제조사 환인 안전재고일수 4영업일 적정재고일수 18영업일 결제일자 0 발주 계산', 4, 18, 0),
        ('제품명 로바스로정 권장 발주량', 3, 15, 25),
        ('키워드 플래리스 추천 발주량', 3, 15, 25),
        ('제약사 환인 마감일 20 발주할 수량', 3, 15, 20),
        ('제품코드 64063 조회구분 발주해당자료만 단가적용처코드 12345 재고적용처코드 23456 발주 계산', 3, 15, 25),
        ('제약사 한림 조회구분 확인 필요 발주 계산', 3, 15, 25),
    ]
    for text, safety, target, closing in calculations:
        result = resolve_registered_erp_table_nlq(text, today=date(2026, 9, 14))
        p = result['params']
        assert result['action'] == '발주 계산'
        assert (p['safety_days'], p['target_days'], p['closing_day']) == (safety, target, closing)
        assert resolve_io_nlq(text)['action'] == '발주 계산'
        assert resolve_new_sims_nlq_candidate(text)['action'] == '발주 계산'
        if '12345' in text:
            assert p['cost_apply_cd'] == '12345' and p['stock_apply_cd'] == '23456'
            assert p['query_mode'] == '발주해당자료만' and p['only_needed']
        else:
            assert p['cost_apply_cd'] == '50002' and p['stock_apply_cd'] == '50001'
        if text.startswith('제조사'):
            assert p['maker_nm'] == '환인'
        if text.startswith('제품명'):
            assert p['physic_nm'] == '로바스로정'
        if text.startswith('키워드'):
            assert p['product_keyword'] == '플래리스'
        if text.startswith('한림'):
            assert p['_registered_unlabeled_entity'] == '한림'
        if text.startswith('환인'):
            assert p['_registered_unlabeled_entity'] == '환인'
        if '확인 필요' in text:
            assert p['query_mode'] == '확인 필요'
        cases.append({'text': text, **result})
    # Dispatch through the real router and service, using the same captured authority fixture.
    for text in ('제약사 환인 발주 계산',
                 '제약사 환인 안전재고 3일 적정재고 15일 결제일 25일 발주 계산',
                 '제약사 한림 안전재고 5일 적정재고 20일 마감일자 25일 발주 계산',
                 '제품코드 64063 발주 수량 계산'):
        _, sources = fixture()
        sent, requests = [], []
        def service(q):
            requests.append(q)
            return get_order_calculation_result(q, source_loader=lambda p: sources)
        with patch('app.services.order_calculation_service.get_current_company_id', return_value=7), \
             patch('app.services.order_calculation_service.get_order_calculation_result', side_effect=service), \
             patch('app.ui.chat_middleware.push_sims_result_to_chat', side_effect=lambda p, a: sent.append(p) or p['meta']):
            assert _try_handle_io_nlq(text, room={}, session_state={}, make_ts=lambda: 'fixture',
                next_seq=lambda: 1, logger=logging.getLogger('fixture'))
        assert len(requests) == len(sent) == 1
        with patch('app.services.order_calculation_service.get_current_company_id', return_value=7):
            panel = get_order_calculation_result(requests[0], source_loader=lambda p: sources)
        pd.testing.assert_frame_equal(sent[0]['df'], panel['df'])
        assert sent[0]['meta']['source_call_count'] == 0
    print(json.dumps(cases, ensure_ascii=False, indent=2))
    from app.services import io_nlq
    for action in ('발주 계산', '발주조회'):
        with patch.object(io_nlq, '_lookup_transaction_vendor_candidates', return_value={'candidates': []}), \
             patch('app.services.product_supplier_scope_service.resolve_supplier_vendor_codes', return_value=[{'code': '10001', 'name': '테스트제약주식회사'}]), \
             patch.object(io_nlq, '_resolve_single_product_code_by_name_with_error', return_value=(None, None)):
            resolved = io_nlq.resolve_unlabeled_io_entity_condition('테스트 발주 계산', action=action,
                params={}, residual_phrase='테스트')
            assert resolved['status'] == 'resolved' and resolved['params']['maker_nm'] == '테스트'
        with patch.object(io_nlq, '_lookup_transaction_vendor_candidates', return_value={'candidates': []}), \
             patch('app.services.product_supplier_scope_service.resolve_supplier_vendor_codes', return_value=[{'code': '10001', 'name': '테스트제약'}, {'code': '10002', 'name': '테스트약품'}]), \
             patch.object(io_nlq, '_resolve_single_product_code_by_name_with_error', return_value=(None, None)):
            assert io_nlq.resolve_unlabeled_io_entity_condition('테스트 발주 계산', action=action,
                params={}, residual_phrase='테스트')['status'] == 'candidate_required'
    sent = []
    with patch.object(io_nlq, '_lookup_transaction_vendor_candidates', return_value={'candidates': [{'match_type': 'transaction_vendor', 'match_code': '00001', 'match_value': '테스트'}]}), \
         patch('app.services.product_supplier_scope_service.resolve_supplier_vendor_codes', return_value=[{'code': '10001', 'name': '테스트제약'}]), \
         patch.object(io_nlq, '_resolve_single_product_code_by_name_with_error', return_value=(None, None)):
        assert io_nlq.resolve_unlabeled_io_entity_condition('테스트 발주 계산', action='발주 계산',
            params={}, residual_phrase='테스트')['status'] == 'candidate_required'
    with patch('app.services.io_nlq.resolve_unlabeled_io_entity_condition', side_effect=lambda txt, **kw: {'status': 'not_found', 'params': kw['params']}), \
         patch('app.ui.chat_middleware.push_sims_result_to_chat', side_effect=lambda p, a: sent.append(p) or p['meta']):
        assert _try_handle_io_nlq('가상회사 안전재고 5일 적정재고 20일 마감일 0 발주 계산',
            room={}, session_state={}, make_ts=lambda: 'fixture', next_seq=lambda: 1, logger=logging.getLogger('fixture'))
    assert all(value in sent[0]['meta']['query_summary'] for value in ('안전재고 5일', '적정재고 20일', '결제/마감일 0일', '50002', '50001'))
    print('PASS NLQ cases 17/17; production dispatch parity 4/4; ERP calls 0')
    print('PASS manufacturer partial/ambiguous 4/4; no_data conditions 1/1')
    print('PASS vendor/manufacturer collision 1/1')


if __name__ == '__main__':
    run()
