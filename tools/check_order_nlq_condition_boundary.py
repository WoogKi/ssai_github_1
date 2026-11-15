"""DB-free order and hospital calculation condition-boundary regressions."""

from contextlib import nullcontext
from datetime import date
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.io_nlq import resolve_io_nlq
from app.services.order_stock_location_scope import extract_order_stock_terms


class OrderConditionBoundaryTests(unittest.TestCase):
    def parse(self, question):
        with patch('app.db.mssql_client.read_df', side_effect=AssertionError('ERP forbidden')):
            result = resolve_io_nlq(question, today=date(2026, 11, 15))
        self.assertIsNotNone(result, question)
        return result['params']

    def assert_full_conditions(self, question, *, allocation=None):
        params = self.parse(question)
        self.assertEqual(params['stock_nm_list'], ['본사', '오토'])
        self.assertEqual(params['stock_cd_list'], [])
        self.assertFalse(params['_order_stock_terms_invalid'])
        self.assertEqual((params['safety_days'], params['target_days'], params['closing_day']),
                         (10, 20, 30))
        self.assertFalse(params.get('_unconsumed_order_condition'))
        if allocation is not None:
            self.assertEqual(params['allocation_stock_apply_codes'], allocation)
            self.assertFalse(params['_hospital_nlq_error'])

    def test_existing_order_calculation_aliases_and_order(self):
        basic = self.parse('발주계산 재고위치 본사 + 오토')
        self.assertEqual(basic['stock_nm_list'], ['본사', '오토'])
        self.assertEqual((basic['safety_days'], basic['target_days'], basic['closing_day']),
                         (3, 15, 25))
        for question in (
            '발주계산 재고위치 본사 + 오토 안전재고 10일 적정재고 20일 결재일자 30일',
            '발주계산 결제일 30일 적정재고 20일 재고위치 본사 + 오토 안전재고 10일',
            '발주 계산 안전재고 10일 결제일자 30일 재고위치 본사 + 오토 적정재고 20일',
        ):
            with self.subTest(question=question):
                self.assert_full_conditions(question)

    def test_hospital_calculation_uses_same_boundaries(self):
        for question, codes in (
            ('병원발주계산 재고적용처 50592 재고위치 본사 + 오토 안전재고 10일 '
             '적정재고 20일 결재일자 30일', ['50592']),
            ('병원발주계산 재고위치 본사 + 오토 재고적용처 50592 + 53831 '
             '안전재고 10일 적정재고 20일 결제일 30일', ['50592', '53831']),
        ):
            with self.subTest(question=question):
                self.assert_full_conditions(question, allocation=codes)
        basic = self.parse('병원발주계산 재고적용처 50592 재고위치 본사')
        self.assertEqual(basic['stock_nm_list'], ['본사'])
        self.assertEqual(basic['allocation_stock_apply_codes'], ['50592'])

    def test_stock_location_extraction_stops_at_each_following_condition(self):
        for suffix in ('안전재고 10일', '적정재고 20일', '결재일자 30일',
                       '결제일자 30일', '결제일 30일', '마감일 30일'):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    extract_order_stock_terms('재고위치 본사 + 오토 ' + suffix),
                    ([], ['본사', '오토']),
                )

    def test_tableless_input_notice_is_rendered_once(self):
        from app.ui import chat_middleware as chat

        for action in ('발주 계산', '병원발주계산'):
            with self.subTest(action=action):
                notice = '재고위치를 확인하세요.'
                item = {'type': 'text', 'action': action, 'title': action,
                        'role': 'assistant', 'message': notice, 'data': notice,
                        'meta': {'result_status': 'input_required',
                                 'service_call_skipped': True}}
                fake_st = MagicMock()
                fake_st.session_state = {}
                fake_st.chat_message.return_value = nullcontext()
                with patch.object(chat, 'st', fake_st), \
                     patch.object(chat, '_get_chat_render_anchor', return_value=None), \
                     patch.object(chat, '_should_render_sims_message_once', return_value=True), \
                     patch.object(chat, '_render_sims_result_header_view'), \
                     patch.object(chat, '_render_structured_presentation_poc'):
                    chat._render_chat_item_body(item)
                fake_st.info.assert_called_once_with(notice)
                self.assertFalse(any(notice in str(call.args[0])
                                     for call in fake_st.markdown.call_args_list))


if __name__ == '__main__':
    unittest.main()
