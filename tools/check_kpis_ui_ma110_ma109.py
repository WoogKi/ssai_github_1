"""DB-free KPIS UI, MA110 shape diagnostics and MA109 contract checks."""
import json
import csv
from io import BytesIO, StringIO
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from openpyxl import load_workbook
from app.services.external_api_management import ApiManagementError, ERP_KEY_CODES
from app.services.kpis_rest_client import OUTPUTS, validate_query
from app.ui import kpis_erp_query as ui, chat_middleware as chat
from tools.check_kpis_query_ui import QueryUI


class QueryChanges(unittest.TestCase):
    def setUp(self):
        self.fixture = QueryUI(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f

    def response(self, data, service='MA110', query=None):
        return self.f.call(request=replace(self.f.req, service=service),
                           query=query or {'splyDd': '20261124'},
                           handler=lambda req: httpx.Response(200, json=data))

    def test_ma102_combos_send_only_code(self):
        with self.fixture.screen(service='MA102') as (page, factory, push):
            self.assertEqual(next(v for v in page.selectbox if v.label == '거래처 구분').options, list(ui.CODE_OPTIONS['msupSplyFrmCd'].values()))
            self.assertEqual(len(page.text_input), 3)
            next(v for v in page.text_input if v.label == '거래처 사업자번호').set_value('0000000001')
            for code in ('5', '6', '8', '9'):
                next(v for v in page.selectbox if v.label == '거래처 구분').set_value(code).run(timeout=20)
                next(v for v in page.button if v.label == 'KPIS 조회').click().run(timeout=20)
                self.assertFalse(page.error)
                self.assertEqual(json.loads(self.f.sent[-1].content)['msupSplyFrmCd'], code)
            self.assertEqual(push.call_count, 4)
            page.run(timeout=20)
            self.assertEqual(factory.call_count, 4)

    def test_ma110_default_null_and_all_codes(self):
        with self.fixture.screen(service='MA110') as (page, factory, push):
            self.assertEqual(next(v for v in page.selectbox if v.label == '공급 구분').value, None)
            self.assertEqual(next(v for v in page.selectbox if v.label == '공급 구분').options, list(ui.CODE_OPTIONS['splyTpCd'].values()))
            page.date_input[0].set_value('2026-11-24')
            for code in (None, '1', '2', '3', '4'):
                next(v for v in page.selectbox if v.label == '공급 구분').set_value(code)
                next(v for v in page.button if v.label == 'KPIS 조회').click().run(timeout=20)
                self.assertFalse(page.error)
                body = json.loads(self.f.sent[-1].content)
                self.assertIn('splyTpCd', body)
                self.assertEqual(body['splyTpCd'], code)
                self.assertEqual(body['aplHbin'], body['splyHbin'])
            self.assertEqual(push.call_count, 5)
            page.run(timeout=20)
            self.assertEqual(factory.call_count, 5)

    def test_ma110_invalid_null_other_field_and_empty_code_block(self):
        for extra in ({'splyTpCd': ''}, {'splyTpCd': '5'}, {'splyTpCd': 1}, {'tkrHbin': None}):
            with self.assertRaises(ApiManagementError):
                validate_query('MA110', {'splyDd': '20261124', **extra})
        self.assertIsNone(validate_query('MA110', {'splyDd': '20261124'})['splyTpCd'])

    def test_ma110_shape_failures_remain_denied_with_safe_metadata(self):
        key = 'msupBriefSummary01List'
        cases = [({}, 'ma110_rows_missing'), ({key: None}, 'ma110_rows_null'),
                 ({key: {'msupSplyQty': '10'}}, 'ma110_rows_type'), ({key: 'private-data'}, 'ma110_rows_type'),
                 ({key: [{'msupSplyQty': '1'}] * 1001}, 'ma110_rows_limit'),
                 ({key: [None]}, 'response_row'), ({key: [{'msupSplyQty': []}]}, 'response_field')]
        for payload, reason in cases:
            with self.subTest(reason=reason), self.assertLogs('ssai.kpis', level='INFO') as logged:
                result = self.response({'result': 'success', 'apiKey': 'private-data', **payload})
            self.assertEqual(result['reason_code'], reason)
            self.assertEqual(result['status'], 'DENIED')
            self.assertEqual(result['rows'], [])
            self.assertNotIn('private-data', str(logged.output))
            self.assertEqual(result['retry_count'], 0)
            self.assertEqual(self.f.events[-1]['result'], reason)

    def test_ma110_array_empty_and_limit_boundary_and_provider_error(self):
        key = 'msupBriefSummary01List'
        for rows in ([], [{'msupSplyQty': '4', 'msupSplyAmt': '741000', 'rcvCnt': '3'}],
                     [{'msupSplyQty': '1'}] * 1000):
            result = self.response({'result': 'success', key: rows})
            self.assertEqual(result['status'], 'SUCCESS')
            self.assertEqual(result['rows'], rows)
        result = self.response({'result': 'fail', key: None, 'resultMessage': 'private-data'})
        self.assertEqual(result['reason_code'], 'provider_rejected')
        self.assertNotIn('private-data', str(result))
        with self.assertLogs('ssai.kpis', level='INFO') as logged:
            self.response({'result': 'private-data', key: []})
        self.assertNotIn('private-data', str(logged.output))

    def test_ma101_name_and_full_fields(self):
        self.assertEqual(ui.KPIS_ACTIONS['표준코드 이력조회 (MA101)'], 'MA101')
        self.assertNotIn('표준코드마스터 조회 (MA101)', ui.KPIS_ACTIONS)
        with self.fixture.screen(service='MA101') as (page, _, push):
            self.assertEqual(page.subheader[0].value, '표준코드 이력조회 (MA101)')
            page.text_input[0].set_value('0000000000001')
            page.button[0].click().run(timeout=20)
            payload = push.call_args.args[0]
            self.assertEqual(payload['title'], 'KPIS 표준코드 이력조회 (MA101)')
            self.assertEqual(list(payload['df'].columns)[:18], list(ui.STANDARD_COLUMNS.values()))
            self.assertEqual(len(payload['df'].columns), 26)

    def test_notice_hook_after_download_actions_once(self):
        source = Path('app/ui/chat_middleware.py').read_text(encoding='utf-8')
        hook = source.index('render_notice_detail(data,')
        actions = source.rfind('_render_sims_result_actions_lazy(', 0, hook)
        self.assertGreater(actions, source.index('# 다운로드 (CSV / XLSX)'))
        self.assertEqual(source.count('render_notice_detail(data,'), 1)

    def test_ma109_mapping_request_and_all_response_fields(self):
        self.assertEqual(ERP_KEY_CODES['MA109'], '000009')
        row = {name: 'synthetic' for name in OUTPUTS['MA109'][1]}
        row['msupStdCd'] = '0000000000001'
        row['rpayCmmEndDd'] = ''
        result = self.response({'result': 'success', 'riskMsupList': [row, row]}, 'MA109', {'rpayCmmYr': '2026'})
        self.assertEqual(result['rows'], [row, row])
        self.f.key.assert_called_once_with(8, 'MA109')
        push = Mock(return_value={})
        with patch.object(ui, 'load_current_api_session', return_value=self.f.actor):
            self.assertTrue(ui.publish_result(result, {'rpayCmmYr': '2026'}, (8, '42', 12345), push=push))
        frame = push.call_args.args[0]['df']
        self.assertEqual(frame.shape, (2, 10))
        self.assertEqual(set(ui.RECALL_COLUMNS), OUTPUTS['MA109'][1])
        self.assertEqual(frame.iloc[0]['표준코드'], '0000000000001')
        normalized = chat._normalize_result_for_chat(push.call_args.args[0])
        self.assertEqual(normalized['meta']['row_count_total'], 2)
        csv_file, excel_file = chat._make_table_downloads(normalized['df'])
        self.assertEqual(list(csv.DictReader(StringIO(csv_file.getvalue().decode('utf-8-sig')))), frame.to_dict('records'))
        book = load_workbook(BytesIO(excel_file.getvalue()), read_only=True)
        try:
            values = list(book.active.values)
            self.assertEqual(values[0], tuple(ui.RECALL_COLUMNS.values()))
            self.assertEqual(len(values), 3)
            self.assertEqual(values[1][0], '0000000000001')
        finally:
            book.close()
        self.assertEqual(self.response({'result': 'success', 'riskMsupList': []}, 'MA109', {'rpayCmmYr': '2026'})['rows'], [])

    def test_ma109_query_validation(self):
        for query in ({}, {'rpayCmmYr': '26'}, {'rpayCmmYr': '0000'}, {'rpayCmmStaDd': '20261101'},
                      {'rpayCmmStaDd': '20260230', 'rpayCmmEndDd': '20260301'},
                      {'rpayCmmStaDd': '20261124', 'rpayCmmEndDd': '20261101'}):
            with self.assertRaises(ApiManagementError):
                validate_query('MA109', query)
        self.assertEqual(validate_query('MA109', {'rpayCmmYr': '2026'}), {'rpayCmmYr': '2026'})

    def test_ma109_year_and_date_form_explicit_only(self):
        with self.fixture.screen(service='MA109') as (page, factory, push):
            page.radio[0].set_value('회수명령년').run(timeout=20)
            page.text_input[0].set_value('2026').run(timeout=20)
            factory.assert_not_called()
            page.button[0].click().run(timeout=20)
            self.assertFalse(page.error)
            self.assertEqual(json.loads(self.f.sent[-1].content)['rpayCmmYr'], '2026')
            page.radio[0].set_value('회수명령 기간').run(timeout=20)
            self.assertEqual(len(page.date_input), 2)
            page.date_input[0].set_value('2026-11-01')
            page.date_input[1].set_value('2026-11-24')
            page.button[0].click().run(timeout=20)
            body = json.loads(self.f.sent[-1].content)
            self.assertNotIn('rpayCmmYr', body)
            self.assertEqual(body['rpayCmmStaDd'], '20261101')
            self.assertEqual(body['rpayCmmEndDd'], '20261124')
            self.assertEqual(push.call_count, 2)
            page.run(timeout=20)
            self.assertEqual(factory.call_count, 2)

    def test_ma109_company_change_after_key_blocks(self):
        def switch(*args):
            self.f.actor = replace(self.f.actor, company_id=4, epoch=99)
            return 'synthetic-only'
        self.f.key.side_effect = switch
        result = self.response({'result': 'success', 'riskMsupList': []}, 'MA109', {'rpayCmmYr': '2026'})
        self.assertEqual(result['status'], 'DENIED')
        self.assertEqual(result['physical_call_count'], 0)

    def test_ma109_wrong_shape_and_credential_echo_denied(self):
        for rows, reason in ((None, 'ma109_rows_null'), ({}, 'ma109_rows_type'),
                             ([{'msupMnfNo': 'synthetic-erp-key-only'}], 'credential_in_response')):
            result = self.response({'result': 'success', 'riskMsupList': rows}, 'MA109', {'rpayCmmYr': '2026'})
            self.assertEqual(result['reason_code'], reason)
            self.assertNotIn('synthetic-erp-key-only', json.dumps(result))

    def test_ma110_metadata_has_only_approved_fields(self):
        with self.assertLogs('ssai.kpis', level='INFO') as logged:
            self.response({'result': 'success', 'msupBriefSummary01List': [],
                           'apiKey': 'private-key', 'aplHbin': 'private-number'})
        message = logged.records[0].getMessage()
        self.assertEqual(message, '[kpis.ma110.response] result=success list_present=True list_type=list list_length=0 over_limit=False')


if __name__ == '__main__':
    unittest.main()
