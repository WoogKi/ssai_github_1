"""Notice display and standard-code contracts using synthetic data only."""
from copy import deepcopy
from dataclasses import replace
from io import BytesIO, StringIO
import csv
import json
import unittest
from unittest.mock import Mock, patch

import httpx
from openpyxl import load_workbook

from app.services.kpis_rest_client import OUTPUTS
from app.ui import kpis_erp_query as ui, chat_middleware as chat
from app.ui.kpis_result_display import notice_plain_text
from tools import check_kpis_erp_direct as direct_checks
from tools import check_kpis_query_ui as ui_checks


class NoticeTextChecks(unittest.TestCase):
    def test_blocks_entities_and_linebreaks(self):
        self.assertEqual(notice_plain_text('<p style="color:red">첫 줄<br>둘째 &amp; 셋째</p><div>다음</div>'),
                         '첫 줄\n둘째 & 셋째\n\n다음')
        self.assertEqual(notice_plain_text('원문\r\n둘째\n\n셋째&nbsp;줄'), '원문\n둘째\n\n셋째 줄')
        self.assertEqual(notice_plain_text('<table><tr><td>A</td><td>B</td></tr><tr><td>C</td></tr></table>'),
                         'A\tB\n\n\tC')

    def test_executable_and_styles_removed(self):
        for tag in ('script', 'style', 'iframe', 'object', 'svg', 'math', 'template', 'head'):
            with self.subTest(tag=tag):
                value = f'<{tag}>HIDDEN</{tag}><p onclick="HIDDEN">내용</p><!--HIDDEN-->'
                self.assertEqual(notice_plain_text(value), '내용')
        self.assertEqual(notice_plain_text('<img src=x onerror="HIDDEN"><a href="javascript:HIDDEN">링크</a>'), '링크')
        self.assertEqual(notice_plain_text('<embed src="HIDDEN"><p>다음 공지</p>'), '다음 공지')

    def test_encoded_nested_and_malformed_markup(self):
        self.assertEqual(notice_plain_text('&amp;lt;script&amp;gt;HIDDEN&amp;lt;/script&amp;gt;&lt;p&gt;공지&lt;/p&gt;'), '공지')
        self.assertEqual(notice_plain_text('앞<script>HIDDEN'), '앞')
        self.assertEqual(notice_plain_text('앞<script/>HIDDEN'), '앞')
        self.assertEqual(notice_plain_text('<p>공지<br/>다음'), '공지\n다음')
        self.assertEqual(notice_plain_text('수량 2 < 5 및 8 > 3'), '수량 2 < 5 및 8 > 3')

    def test_non_strings_empty_and_plain(self):
        for value in (None, 0, '', '한글 원문'):
            self.assertEqual(notice_plain_text(value), value)


class ServiceChecks(unittest.TestCase):
    def setUp(self):
        self.f = direct_checks.DirectChecks(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def result(self, service, rows):
        f = self.f
        query = {'msupStdCd': '0000000000001'} if service == 'MA101' else f.query
        def handler(request):
            f.sent.append(request)
            return httpx.Response(200, json={'result': 'success', OUTPUTS[service][0]: rows})
        return f.call(request=replace(f.req, service=service), query=query, handler=handler), query

    def publish(self, result, query):
        push = Mock(return_value={})
        self.assertTrue(ui.publish_result(result, query, (8, '42', 12345),
                                         load_session=lambda: self.f.actor, push=push))
        push.assert_called_once()
        return chat._normalize_result_for_chat(push.call_args.args[0])

    def test_notice_projection_and_common_exports_do_not_mutate_result(self):
        body = '<style>HIDDEN</style><p>첫 줄<br>다음 &amp; 마지막</p><script>HIDDEN</script>'
        result, query = self.result('MA117', [{'brdTtl': '공지 제목', 'brdDtlTxt': body}])
        original = deepcopy(result)
        payload = self.publish(result, query)
        self.assertEqual(result, original)
        expected = '첫 줄\n다음 & 마지막'
        self.assertEqual(payload['df'].iloc[0]['내용'], expected)
        self.assertEqual(payload['df'].iloc[0]['제목'], '공지 제목')
        with patch('app.ui.ssai_login.get_selected_company', return_value={'company_id': 8}):
            self.assertTrue(chat._chat_payload_matches_current_company(payload))
        with patch('app.ui.ssai_login.get_selected_company', return_value={'company_id': 4}):
            self.assertFalse(chat._chat_payload_matches_current_company(payload))
        csv_file, excel_file = chat._make_table_downloads(payload['df'])
        rows = list(csv.DictReader(StringIO(csv_file.getvalue().decode('utf-8-sig'))))
        self.assertEqual(rows[0]['내용'], expected)
        workbook = load_workbook(BytesIO(excel_file.getvalue()), read_only=True)
        try:
            values = list(workbook.active.values)
            self.assertEqual(values[1][values[0].index('내용')], expected)
        finally:
            workbook.close()

    def test_ma101_official_18_fields_request_and_leading_zero(self):
        fields = ('stdCd korMgdsNm adptFrDt adptToDt tranTkovrAdptFrDt tranTkovrAdptToDt '
                  'packMgdsTotQty upCost payType sftMsup spcGnlCd wlprLprcRadt itemStdCd cnclDt '
                  'snoOmssYnCd snoOmssRsCd msupDctOftTpCd crtrPrdCd3').split()
        self.assertEqual(OUTPUTS['MA101'], ('msupStdCdList', set(fields)))
        self.assertEqual(list(ui.STANDARD_COLUMNS), fields)
        row = {field: str(index) for index, field in enumerate(fields)}
        row.update(stdCd='0000000000001', korMgdsNm='<b>다른 업무 원문</b>', upCost='1234.50', unknown='ignored')
        result, query = self.result('MA101', [row])
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertEqual(set(result['rows'][0]), set(fields))
        request = self.f.sent[0]
        self.assertEqual(request.method, 'POST')
        self.assertEqual(str(request.url), 'https://newopenapi.kpis.or.kr/v1/services/getMsupStdCdInfo.do')
        body = json.loads(request.content)
        self.assertEqual(set(body), {'msupStdCd', 'aplHbin', 'apiKey'})
        self.assertEqual(body['msupStdCd'], '0000000000001')
        self.assertEqual(body['aplHbin'], self.f.entry['applicant'])
        self.f.key.assert_called_once_with(8, 'MA101')
        original = deepcopy(result)
        frame = self.publish(result, query)['df']
        self.assertEqual(result, original)
        self.assertEqual(list(frame), list(ui.STANDARD_COLUMNS.values()))
        self.assertEqual(frame.iloc[0]['표준코드'], '0000000000001')
        self.assertEqual(frame.iloc[0]['표준코드명칭'], '<b>다른 업무 원문</b>')
        self.assertEqual(frame.iloc[0]['상한가'], '1234.50')
        csv_file, excel_file = chat._make_table_downloads(frame)
        exported = list(csv.DictReader(StringIO(csv_file.getvalue().decode('utf-8-sig'))))
        self.assertEqual(exported[0]['표준코드'], '0000000000001')
        workbook = load_workbook(BytesIO(excel_file.getvalue()), read_only=True)
        try:
            values = list(workbook.active.values)
            self.assertEqual(values[0], tuple(ui.STANDARD_COLUMNS.values()))
            self.assertEqual(values[1][0], '0000000000001')
            self.assertEqual(values[1][7], '1234.50')
        finally:
            workbook.close()

    def test_zero_rows_preserve_headers_and_single_chat(self):
        for service, columns in (('MA101', ui.STANDARD_COLUMNS), ('MA117', ui.NOTICE_COLUMNS)):
            with self.subTest(service=service):
                result, query = self.result(service, [])
                self.assertEqual(result['status'], 'SUCCESS')
                payload = self.publish(result, query)
                self.assertTrue(payload['df'].empty)
                self.assertEqual(list(payload['df']), list(columns.values()))
                self.assertEqual(payload['meta']['row_count_total'], 0)

    def test_ma101_invalid_code_never_reads_key(self):
        for value in ('', '123', '0' * 14, 'A' * 13, '１' * 13):
            result = self.f.call(request=replace(self.f.req, service='MA101'), query={'msupStdCd': value})
            self.assertEqual(result['status'], 'DENIED')
        self.f.key.assert_not_called()
        self.assertFalse(self.f.sent)

    def test_ma101_errors_are_bounded_no_retry_no_secret(self):
        cases = [(httpx.Response(200, json={'result': 'failure', 'resultMessage': 'synthetic-erp-key-only'}), 'provider_rejected'),
                 (httpx.Response(500, text='synthetic-erp-key-only'), 'http_error'),
                 (httpx.Response(200, json={'result': 'success', 'msupStdCdList': {}}), 'response_rows')]
        for response, reason in cases:
            with self.subTest(reason=reason):
                result = self.f.call(request=replace(self.f.req, service='MA101'),
                                     query={'msupStdCd': '0000000000001'}, handler=lambda _: response)
                self.assertEqual(result['status'], 'DENIED')
                self.assertEqual(result['reason_code'], reason)
                self.assertEqual((result['physical_call_count'], result['retry_count']), (1, 0))
                self.assertEqual(result['rows'], [])
                self.assertNotIn('synthetic-erp-key-only', json.dumps(result))
                self.assertNotIn('synthetic-erp-key-only', json.dumps(self.f.events))

    def test_ma101_company_switch_during_key_read_blocks(self):
        def key(company, service):
            self.f.actor = replace(self.f.actor, company_id=4, epoch=99)
            return 'synthetic-key'
        self.f.key.side_effect = key
        result = self.f.call(request=replace(self.f.req, service='MA101'), query={'msupStdCd': '0000000000001'})
        self.assertEqual((result['status'], result['physical_call_count']), ('DENIED', 0))
        self.assertFalse(self.f.sent)

    def test_ma101_ui_only_explicit_submit_once(self):
        fixture = ui_checks.QueryUI(methodName='runTest')
        fixture.f = self.f
        with fixture.screen(service='MA101') as (page, factory, push):
            self.assertFalse(page.date_input)
            self.assertFalse(page.checkbox)
            self.assertEqual(len(page.text_input), 1)
            self.assertIn('13자리', page.text_input[0].label)
            page.text_input[0].set_value('123')
            page.button[0].click().run(timeout=20)
            self.assertTrue(page.error)
            factory.assert_not_called()
            page.text_input[0].set_value('0000000000001').run(timeout=20)
            factory.assert_not_called()
            page.button[0].click().run(timeout=20)
            self.assertFalse(page.exception)
            self.assertFalse(page.error)
            page.run(timeout=20)
            self.assertEqual(len(self.f.sent), 1)
            self.f.key.assert_called_once_with(8, 'MA101')
            factory.assert_called_once()
            push.assert_called_once()
            self.assertFalse(page.dataframe)


if __name__ == '__main__':
    unittest.main()
