"""DB-free NLQ -> registered dispatcher -> hospital service contracts."""
from contextlib import ExitStack
from datetime import date
import logging
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.io_nlq import resolve_io_nlq
from app.services import hospital_order_calculation_service as h
from app.sims.nlq.nlq_router import _try_handle_io_nlq, resolve_new_sims_nlq_candidate
from tools.check_hospital_order_calculation import parents, sources, totals, master

REAL_PARENT = h.base.get_order_calculation_result
REAL_GROUP_OPTIONS = h._master_options


def parsed(question):
    return resolve_io_nlq(question, today=date(2026, 11, 15))


class HospitalNlqTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('app.db.mssql_client.read_df', side_effect=AssertionError('ERP forbidden')))
        self.permission = self.stack.enter_context(patch('app.ui.ssai_login.has_permission', return_value=True))
        self.company = self.stack.enter_context(patch.object(h, 'get_current_company_id', return_value=3))
        self.master = self.stack.enter_context(patch.object(h, 'load_master', return_value=master()))
        self.options = self.stack.enter_context(patch.object(h, '_master_options', return_value={
            '70001': '단원병원', '70002': 'fixture B', '50001': '재고적용거래처'}))
        self.scope = self.stack.enter_context(patch(
            'app.services.order_stock_location_scope.prepare_order_stock_scope',
            side_effect=lambda q, **kw: ({**q, 'stock_cd_list': ['00001'],
                '_order_stock_scope_resolved': True, '_order_stock_scope_policy': 'saved'}, '', [])))
        self.identity = self.stack.enter_context(patch.object(h.base, 'load_application_names',
            side_effect=lambda codes: pd.DataFrame([{'code': c, 'name': 'parent fixture'} for c in codes])))
        self.parent = self.stack.enter_context(patch.object(h.base, 'get_order_calculation_result',
            return_value={'df': parents(), 'meta': {'result_status': 'success',
                'order_header_metrics': {'effective_business_date': '2026-11-16'}, 'query_summary': 'fixture'}}))
        self.allocation = Mock(return_value=sources())
        self.allocate = self.stack.enter_context(patch.object(h, 'allocate', wraps=h.allocate))

    def execute(self, question, **extra):
        return h.get_hospital_order_calculation_result(
            {**parsed(question)['params'], **extra}, allocation_loader=self.allocation)

    def route(self, question, *, outer=False):
        sent = []
        real = h.get_hospital_order_calculation_result
        with patch.object(h, 'get_hospital_order_calculation_result',
                          side_effect=lambda params: real(params, allocation_loader=self.allocation)), \
             patch('app.ui.chat_middleware.push_sims_result_to_chat',
                   side_effect=lambda p, a: sent.append(p) or p['meta']):
            from app.sims.nlq.nlq_router import try_handle_nlq
            route = try_handle_nlq if outer else _try_handle_io_nlq
            self.assertTrue(route(question, room={}, session_state={},
                make_ts=lambda: 'fixture', next_seq=lambda: 1, logger=logging.getLogger('fixture')))
        self.assertEqual(len(sent), 1)
        return sent[0]

    def assert_no_calculation(self):
        self.parent.assert_not_called()
        self.allocation.assert_not_called()
        self.allocate.assert_not_called()

    def test_default_aliases_and_new_query(self):
        for question in ('병원발주계산 해줘', '병원 발주 계산', '병원발주계산',
                         '병원별발주계산', '병원별 발주 계산'):
            self.assertEqual(parsed(question)['action'], h.ACTION)
            self.assertEqual(resolve_new_sims_nlq_candidate(question)['action'], h.ACTION)
            self.assertEqual(parsed(question)['params'][h.SELECTION], [])

    def test_product_code(self):
        result = self.execute('병원발주계산 제품코드 23397')
        self.assertEqual(result['params']['physic_cd'], '23397')
        self.assertEqual(self.parent.call_args.args[0]['physic_cd'], '23397')

    def test_single_and_multiple_codes(self):
        for value, expected in [('70001', ['70001']), ('70001, 70002', ['70001','70002'])]:
            self.assertEqual(self.execute('병원발주계산 재고적용처 ' + value)['params'][h.SELECTION], expected)

    def test_multi_names_codes_delimiters_and_location_scope(self):
        from app.services.order_stock_location_scope import resolve_order_stock_scope
        self.options.return_value = {'70001': '단원병원', '70002': '대자인', '50001': '재고적용거래처'}
        for value in ('단원병원 + 대자인', '단원병원, 대자인', '단원병원 + 70002',
                      '70001, 70002', '단원병원, 70001, 대자인'):
            parsed_params = parsed('병원발주계산 재고적용처 ' + value)['params']
            self.assertFalse(parsed_params['_hospital_nlq_error'], value)
            self.assertEqual(self.execute('병원발주계산 재고적용처 ' + value)['params'][h.SELECTION],
                             ['70001', '70002'])
        both = parsed('병원발주계산 재고위치 본사 + 오토 재고적용처 단원병원 + 대자인')['params']
        self.assertEqual(both['stock_nm_list'], ['본사', '오토'])
        self.assertEqual(both['_allocation_stock_apply_names'], ['단원병원', '대자인'])
        scope, error, _ = resolve_order_stock_scope(
            both, default_codes=['00001', '00901'],
            registered_names={'00001': '본사창고', '00901': '오토창고'}, saved_only=True)
        self.assertFalse(error)
        self.assertEqual(scope['stock_cd_list'], ['00001', '00901'])
        self.assertEqual(scope['_order_stock_display_names'], {'00001': '본사창고', '00901': '오토창고'})

    def test_partial_name_exact_first_and_multi_match(self):
        self.options.return_value = {'70001': '단원병원(매출)', '70002': '단원병원',
                                     '50001': '재고적용거래처'}
        self.assertEqual(self.execute('병원발주계산 재고적용처 단원병원')['params'][h.SELECTION], ['70002'])
        self.parent.reset_mock()
        self.options.return_value = {'70001': '단원병원(매출)', '70002': '단원병원(입원)',
                                     '50001': '재고적용거래처'}
        result = self.execute('병원발주계산 재고적용처 단원병원')
        self.assertEqual(result['meta']['result_status'], 'success')
        self.assertEqual(result['params'][h.SELECTION], ['70001', '70002'])
        self.parent.assert_called_once()

    def test_company4_group_names_and_ordinary_customer_are_distinct(self):
        names = {
            '50591': '(20.12.31폐업)단원병원(마약)',
            '50592': '단원병원(매출)',
            '52553': '(20.12.31폐업)단원병원(수액)',
            '53618': '(20.12.31폐업)단원병원(향정)',
            '53619': '(20.12.31폐업)단원병원(지하2층)',
            '53831': '대자인병원',
            '50001': '재고적용거래처',
        }
        rows = [(code, name, code, '80001') for code, name in names.items()]
        rows += [('61234', '대자인병원 옆 약국', '50001', '80001'),
                 ('80001', '단가적용처', '', '')]
        frame = pd.DataFrame(rows, columns=h.MASTER_COLUMNS)
        self.master.return_value = frame
        self.options.side_effect = REAL_GROUP_OPTIONS
        self.allocation.return_value = dict(
            master=frame, sales=totals([('P1', '50592', 100), ('P1', '53831', 50)]),
            orders=totals(), contracts={})

        matches = self.execute('병원발주계산 재고적용처 단원병원')
        self.assertEqual(matches['meta']['result_status'], 'success')
        self.assertEqual(matches['params'][h.SELECTION], ['50591', '50592', '52553', '53618', '53619'])
        self.parent.assert_called_once()
        self.parent.reset_mock()
        self.allocation.reset_mock()
        self.allocate.reset_mock()

        ordinary = self.execute('병원발주계산 재고적용처 대자인병원 옆 약국')
        self.assertEqual(ordinary['meta']['result_status'], 'input_required')
        self.assertIn('거래처명으로 확인되지만', ordinary['message'])
        self.assertIn('자동 변환하지 않았습니다', ordinary['message'])
        self.assert_no_calculation()

        cases = (
            ('병원발주계산 재고적용처 단원병원(매출)', ['50592']),
            ('병원발주계산 재고적용처 50592', ['50592']),
            ('병원발주계산 재고적용처 대자인', ['53831']),
            ('병원발주계산 재고적용처 단원병원(매출) + 대자인', ['50592', '53831']),
            ('병원발주계산 재고적용처 50592, 53831', ['50592', '53831']),
            ('병원발주계산 재고위치 본사 + 오토 재고적용처 단원병원(매출) + 대자인',
             ['50592', '53831']),
        )
        for question, expected in cases:
            self.parent.reset_mock()
            self.allocation.reset_mock()
            selected = self.execute(question)
            self.assertEqual(selected['meta']['result_status'], 'success', question)
            self.assertEqual(selected['params'][h.SELECTION], expected, question)
            self.parent.assert_called_once()
            self.allocation.assert_called_once()
            if '재고위치' in question:
                self.assertEqual(parsed(question)['params']['stock_nm_list'], ['본사', '오토'])
        self.assertEqual(selected['meta']['order_display_conditions']['stock_application'], '2개 선택')

    def test_selected_names_and_summary_use_current_group_identity(self):
        frame = master().copy()
        frame.loc[frame.code.eq('70001'), 'name'] = '단원병원(매출)'
        frame.loc[frame.code.eq('70002'), 'name'] = '대자인'
        self.master.return_value = frame
        self.options.return_value = {'70001': '단원병원(매출)', '70002': '대자인',
                                     '50001': '재고적용거래처'}
        result = self.execute('병원발주계산 재고적용처 단원병원 + 대자인')
        self.assertEqual(result['meta']['result_status'], 'success')
        display = result['meta']['order_display_conditions']
        self.assertEqual(display['stock_application'], '2개 선택')
        self.assertIn('70001 단원병원(매출)', display['stock_application_details'])
        self.assertIn('70002 대자인', display['stock_application_details'])
        self.assertIn('재고적용처 2개 선택', result['meta']['summary_md'])
        self.assertIn('미배분수량', result['meta']['summary_md'])
        self.assertEqual(result['df']['실제 발주수량'].sum() +
                         sum(result['df'].attrs[h.UNALLOCATED_ATTR].values()),
                         parents()['실제 발주수량'].sum())

    def test_multi_location_and_allocation_display_without_second_lookup(self):
        from app.services.order_stock_location_scope import resolve_order_stock_scope
        frame = master().copy()
        frame.loc[frame.code.eq('70001'), 'name'] = '단원병원'
        frame.loc[frame.code.eq('70002'), 'name'] = '대자인'
        self.master.return_value = frame
        self.options.return_value = {'70001': '단원병원', '70002': '대자인',
                                     '50001': '재고적용거래처'}
        self.scope.side_effect = lambda q, **kw: resolve_order_stock_scope(
            q, default_codes=['00001', '00901'],
            registered_names={'00001': '본사창고', '00901': '오토창고'}, saved_only=True)
        def parent(q, *, source_loader):
            source_loader(q)
            return {'df': parents(), 'params': q, 'meta': {'result_status': 'success',
                'order_header_metrics': {'effective_business_date': '2026-11-16'},
                'query_summary': 'fixture'}}
        self.parent.side_effect = parent
        params = parsed('병원발주계산 재고위치 본사 + 오토 재고적용처 단원병원 + 대자인')['params']
        result = h.get_hospital_order_calculation_result(
            params, source_loader=lambda q: {'source_params': q}, allocation_loader=self.allocation)
        self.assertEqual(result['meta']['result_status'], 'success')
        self.assertEqual(result['params'][h.SELECTION], ['70001', '70002'])
        self.assertEqual(self.parent.call_args.args[0]['stock_cd_list'], ['00001', '00901'])
        display = result['meta']['order_display_conditions']
        self.assertEqual(display['stock_location'], '2개 선택')
        self.assertEqual(display['stock_location_details'], '본사창고 (00001), 오토창고 (00901)')
        self.assertEqual(display['stock_application'], '2개 선택')
        self.assertEqual((self.parent.call_count, self.allocation.call_count, self.allocate.call_count), (1, 1, 1))

    def test_exact_name_prefix_and_label(self):
        for question in ('단원병원 병원발주계산 해줘', '병원발주계산 재고적용처 단원병원'):
            self.assertEqual(self.execute(question)['params'][h.SELECTION], ['70001'])
        self.master.assert_called_with(3, timeout_seconds=15)

    def test_all_and_omitted(self):
        for suffix in ('', ' 재고적용처 전체'):
            self.assertEqual(self.execute('병원발주계산' + suffix)['params'][h.SELECTION], [])
        self.options.assert_not_called()

    def test_parent_allocation_independence(self):
        q = '병원발주계산 부모 재고적용처코드 50001 배분 재고적용처 70001'
        result = self.execute(q)
        self.assertEqual(result['params']['stock_apply_cd'], '50001')
        self.assertEqual(result['params'][h.SELECTION], ['70001'])
        self.assertNotIn(h.SELECTION, self.parent.call_args.args[0])
        self.assertEqual(self.allocation.call_args.args[-1], ('70001',))

    def test_parent_parser_conditions_reused(self):
        for clause in ('제약사 삼진', '발주처 fixture', '발주담당자 fixture', '제약담당자 fixture',
                       '단가적용처코드 80001', '안전재고 5일 적정재고 20일 결제일 25일',
                       '조회구분 전체', '제품코드 23397'):
            baseline = parsed('발주 계산 ' + clause)['params']
            hospital = parsed('병원발주계산 ' + clause)['params']
            self.assertFalse(hospital['_hospital_nlq_error'], clause)
            for key, value in baseline.items():
                self.assertEqual(hospital[key], value, (clause, key))

    def test_warehouse_uses_saved_scope(self):
        self.execute('병원발주계산 본사창고 기준')
        self.assertEqual(self.scope.call_args.args[0]['stock_nm_list'], ['본사창고'])
        self.assertTrue(self.scope.call_args.kwargs['saved_only'])
        for question in ('본사창고 기준 병원발주계산',
                         '병원발주계산 재고적용처 70001 본사창고 기준'):
            self.assertFalse(parsed(question)['params']['_hospital_nlq_error'])
            self.assertEqual(parsed(question)['params']['stock_nm_list'], ['본사창고'])

    def test_unknown_duplicate_fuzzy_names(self):
        self.assertEqual(self.execute('병원발주계산 재고적용처 없는병원')['meta']['result_status'], 'input_required')
        self.assert_no_calculation()
        self.options.return_value = {'70001': 'fixture B', '70002': 'fixture B'}
        result = self.execute('병원발주계산 재고적용처 fixture B')
        self.assertEqual(result['params'][h.SELECTION], ['70001', '70002'])

    def test_broad_name_requires_narrower_scope_before_parent(self):
        self.options.return_value = {f'{i:05d}': f'단원병원 {i}' for i in range(21)}
        result = self.execute('병원발주계산 재고적용처 단원병원')
        self.assertEqual(result['meta']['result_status'], 'input_required')
        self.assertIn('21개', result['message'])
        self.assert_no_calculation()

    def test_unknown_code(self):
        self.assertEqual(self.execute('병원발주계산 재고적용처 12345')['meta']['result_status'], 'input_required')
        self.assert_no_calculation()

    def test_invalid_condition_zero_authority_calls(self):
        for question in ('병원발주계산 재고적용처', '병원발주계산 재고적용처 70001,',
                         '병원발주계산 재고적용처 전체 + 단원병원',
                         '병원발주계산 재고적용처 전체 배분 재고적용처 70001',
                         '병원발주계산 제품코드 23397 알수없는조건 12', '병원발주계산 담당자 신',
                         '병원발주계산 발주상태 3', '병원발주계산 납기일자 20261130'):
            self.assertEqual(self.execute(question)['meta']['result_status'], 'input_required', question)
        self.options.assert_not_called()
        self.scope.assert_not_called()
        self.identity.assert_not_called()
        self.assert_no_calculation()

    def test_permission_denied_zero_queries(self):
        self.permission.return_value = False
        result = self.route('단원병원 병원발주계산 해줘')
        self.assertEqual(result['meta']['result_status'], 'permission_denied')
        self.options.assert_not_called()
        self.scope.assert_not_called()
        self.identity.assert_not_called()
        self.assert_no_calculation()
        self.permission.assert_called_with('IO_READ')

    def test_company_mismatch_zero_queries(self):
        with self.assertRaises(h.AllocationError):
            self.execute('병원발주계산 재고적용처 70001', company_id=4)
        self.options.assert_not_called()
        self.assert_no_calculation()

    def test_company_change_during_resolution(self):
        self.company.side_effect = [3, 3, 4]
        self.assertEqual(self.execute('단원병원 병원발주계산')['meta']['result_status'], 'input_required')
        self.assert_no_calculation()

    def test_parent_identity_mismatch(self):
        result = self.execute('병원발주계산', stock_apply_cd='50001', stock_apply_nm='wrong')
        self.assertEqual(result['meta']['result_status'], 'input_required')
        self.assert_no_calculation()

    def test_parent_name_only(self):
        self.assertEqual(self.execute('병원발주계산', stock_apply_cd='', stock_apply_nm='orphan')['meta']['result_status'], 'input_required')
        self.assert_no_calculation()

    def test_missing_saved_warehouse(self):
        self.scope.side_effect = lambda q, **kw: (q, 'no_saved_locations', [])
        self.assertEqual(self.execute('병원발주계산')['meta']['result_status'], 'input_required')
        self.assert_no_calculation()

    def test_production_dispatch_once_and_special_result(self):
        result = self.route('병원발주계산 재고적용처 70001')
        self.assertEqual(result['meta']['result_status'], 'success')
        self.assertTrue(result['meta']['hospital_order_calculation'])
        self.assertFalse(result['meta']['order_calculation_editable'])
        self.assertEqual((self.parent.call_count, self.allocation.call_count, self.allocate.call_count), (1,1,1))
        self.assertEqual(result['df']['실제 발주수량'].sum() +
                         sum(result['df'].attrs[h.UNALLOCATED_ATTR].values()),
                         parents()['실제 발주수량'].sum())
        self.assertNotIn('50001', set(result['df']['배분 재고적용처코드']))
        self.assertIn(h.REFERENCE_ATTR, result['df'].attrs)

    def test_legacy_routes_unchanged(self):
        self.assertEqual(parsed('발주 계산')['action'], '발주 계산')
        self.assertEqual(parsed('발주 조회')['action'], '발주조회')
        self.assertEqual(parsed('발주 계산 재고적용처코드 70001')['params']['stock_apply_cd'], '70001')

    def test_before_after_condition_order(self):
        for clause in ('재고적용처 70001, 70002', '제품코드 23397', '제약사 삼진'):
            self.assertEqual(parsed(clause + ' 병원발주계산')['params'], parsed('병원발주계산 ' + clause)['params'])

    def test_outer_router_executes_hospital(self):
        result = self.route('병원발주계산 재고적용처 70001', outer=True)
        self.assertEqual(result['meta']['result_status'], 'success')
        self.assertEqual((self.parent.call_count, self.allocation.call_count), (1, 1))

    def test_real_parent_engine_once_and_same_quantity(self):
        from tools.check_order_calculation_contract import fixture
        q, data = fixture()
        q.update(_display_context='chat')
        self.company.return_value = 7
        with patch.object(h.base, 'get_current_company_id', return_value=7), \
             patch.object(h.base, 'get_order_calculation_result', wraps=REAL_PARENT) as engine:
            loader = Mock(return_value=data)
            allocations = Mock(return_value=dict(master=master(), sales=totals(), orders=totals(), contracts={}))
            result = h.get_hospital_order_calculation_result(q, source_loader=loader, allocation_loader=allocations)
            self.assertEqual(engine.call_count, 1)
            self.assertEqual(loader.call_count, 1)
            self.assertEqual(allocations.call_count, 1)
            self.assertEqual(result['meta']['result_status'], 'success')
            reference_total = sum(row['실제 발주수량'] for row in result['df'].attrs[h.REFERENCE_ATTR])
            self.assertEqual(result['df']['실제 발주수량'].sum(), reference_total)

    def test_nlq_delivery_current_table_and_cached_export(self):
        import streamlit as st
        from app.ui import chat_middleware as chat
        result = self.route('단원병원 병원발주계산')
        state = {'__sims_push_count': 0}
        with patch.object(st, 'session_state', state), \
             patch.object(chat, 'wire_chat_context'), patch.object(chat, 'drain_inbox_to_chat'), \
             patch.object(chat, '_append_finalized_nlq_case'), \
             patch.object(chat, 'maybe_create_structured_presentation', return_value={'status': 'skipped'}), \
             patch.object(chat, '_chat_current_company_sig', return_value=('3', '')):
            chat.push_sims_result_to_chat(result, h.ACTION)
            self.assertEqual(len(state['__chat_inbox']), 1)
            key = state['__sims_current_table_source_key']
            full = state['sims_export_tables'][key]
            self.assertIn(h.REFERENCE_ATTR, full.attrs)
            pd.testing.assert_frame_equal(h.get_hospital_order_calculation_export_df({'table_key': key}), full)
            self.assertEqual(full['실제 발주수량'].sum() +
                             sum(full.attrs[h.UNALLOCATED_ATTR].values()),
                             parents()['실제 발주수량'].sum())
            from app.ui.current_table_followups.generic import handle_common_column_group_followup
            followup = Mock(return_value=True)
            self.assertTrue(handle_common_column_group_followup(
                df=full, query='현재표 제품별 실제 발주수량 합계', top_n=20,
                table_key=key, source_action=h.ACTION,
                helpers={'push_table': followup}, log=logging.getLogger('fixture')))
            self.assertEqual(followup.call_args.kwargs['df']['실제 발주수량'].sum(),
                             full['실제 발주수량'].sum())
            pd.testing.assert_frame_equal(state['sims_export_tables'][key], full)
        self.assertEqual((self.parent.call_count, self.allocation.call_count, self.allocate.call_count), (1,1,1))


if __name__ == '__main__':
    unittest.main(verbosity=2)
