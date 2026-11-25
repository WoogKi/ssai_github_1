"""Synthetic REST failures and bounded product candidates; no live DB/API."""
from contextlib import contextmanager
from dataclasses import replace
from io import StringIO
import json
import logging
import unittest
from unittest.mock import Mock, patch

import httpx
import pandas as pd

from app.services import kpis_rest_client as rest, rddbc040_service as goods
from app.ui import kpis_erp_query as ui
from app.utils.logging_setup import OnlyNamespace
from tools.check_kpis_query_ui import QueryUI


class RestErrorChecks(unittest.TestCase):
    def setUp(self):
        self.fixture = QueryUI(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f

    def response(self, response, service='MA101'):
        queries = {'MA101': {'msupStdCd': '0000000000001'}, 'MA102': {'msupSplyFrmCd': '5', 'tkrHbin': '0000000001'},
                   'MA109': {'rpayCmmYr': '2026'}, 'MA110': {'splyDd': '20261124'}}
        with self.assertLogs(rest.log, 'INFO') as logged:
            result = self.f.call(request=replace(self.f.req, service=service), query=queries[service],
                                 handler=lambda _: response)
        self.assertNotIn('PRIVATE-PAYLOAD', str(result) + str(logged.output) + str(self.f.events))
        return result, logged

    def test_four_services_invalid_json_previous_generic_path_classified(self):
        for service in ('MA101', 'MA102', 'MA109', 'MA110'):
            result, logs = self.response(httpx.Response(200, content=b'PRIVATE-PAYLOAD',
                headers={'Content-Type': 'application/json'}), service)
            self.assertEqual(result['reason_code'], 'response_json_invalid')
            self.assertEqual((result['physical_call_count'], result['retry_count']), (1, 0))
            self.assertIn('stage=response_json', logs.output[-1])
            self.assertIn('error_type=JSONDecodeError', logs.output[-1])
            self.assertIn('http_status=200', logs.output[-1])

    def test_connection_protocol_network_timeout_no_retry(self):
        cases = [(httpx.ConnectError, 'connection_error'), (httpx.ReadError, 'network_error'),
                 (httpx.RemoteProtocolError, 'protocol_error'), (httpx.ReadTimeout, 'timeout'),
                 (httpx.DecodingError, 'response_decoding_error')]
        for kind, reason in cases:
            calls = []
            def fail(request):
                calls.append(1)
                raise kind('PRIVATE-PAYLOAD', request=request)
            with self.assertLogs(rest.log, 'WARNING') as logs:
                result = self.f.call(handler=fail)
            self.assertEqual(result['reason_code'], reason)
            self.assertEqual(len(calls), 1)
            self.assertNotIn('PRIVATE-PAYLOAD', str(result) + str(logs.output))
            self.assertIn('stage=http_exchange', logs.output[0])

    def test_http_and_content_type_are_separate(self):
        for response, reason in [(httpx.Response(401), 'http_auth_error'), (httpx.Response(403), 'http_auth_error'),
                                 (httpx.Response(500), 'http_error'), (httpx.Response(200, text='PRIVATE-PAYLOAD'), 'response_type')]:
            result, logs = self.response(response)
            self.assertEqual(result['reason_code'], reason)
            self.assertIn('stage=response_headers', logs.output[-1])

    def test_json_encoding_schema_and_nonfinite_fail_closed(self):
        for content, reason in [(b'\xffPRIVATE-PAYLOAD', 'response_encoding_error'),
                                (b'[]', 'response_schema'), (b'{"x":NaN}', 'response_json_invalid'),
                                (b'{"result":"success","msupStdCdList":[{"upCost":1e999}]}', 'response_field')]:
            result, _ = self.response(httpx.Response(200, content=content, headers={'content-type': 'application/json'}))
            self.assertEqual(result['reason_code'], reason)
            self.assertEqual(result['rows'], [])

    def test_unknown_internal_exception_only_safe_stage_and_type(self):
        def fail(_):
            raise RuntimeError('PRIVATE-PAYLOAD')
        with self.assertLogs(rest.log, 'WARNING') as logs:
            result = self.f.call(handler=fail)
        self.assertEqual(result['reason_code'], 'internal_or_transport_error')
        self.assertIn('RuntimeError', logs.output[0])
        self.assertNotIn('PRIVATE-PAYLOAD', str(result) + str(logs.output))
        self.assertEqual(self.f.events[-1]['result'], result['reason_code'])

    def test_ma110_shape_and_limits_unchanged(self):
        key = 'msupBriefSummary01List'
        for payload, reason in [({}, 'ma110_rows_missing'), ({key: None}, 'ma110_rows_null'),
                               ({key: {}}, 'ma110_rows_type'),
                               ({key: [{'msupSplyQty': '1'}] * 1001}, 'ma110_rows_limit')]:
            result, logs = self.response(httpx.Response(200, json={'result': 'success', **payload}), 'MA110')
            self.assertEqual(result['reason_code'], reason)
            self.assertIn('[kpis.ma110.response]', logs.output[0])
            self.assertEqual(result['rows'], [])

    def test_existing_ssai_logger_receives_metadata_not_root(self):
        parent, root = logging.getLogger('ssai'), logging.getLogger()
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(OnlyNamespace('ssai'))
        previous = parent.handlers[:], parent.level, parent.propagate, root.level, rest.log.level
        try:
            parent.handlers = [handler]
            parent.setLevel(logging.INFO)
            parent.propagate = False
            root.setLevel(logging.WARNING)
            rest.log.setLevel(logging.NOTSET)
            logging.getLogger('app.services.kpis_rest_client').info('old-route-hidden')
            self.f.call(request=replace(self.f.req, service='MA110'), query={'splyDd': '20261124'},
                        handler=lambda _: httpx.Response(200, json={'result': 'success', 'msupBriefSummary01List': None}))
            text = stream.getvalue()
            self.assertIn('[kpis.ma110.response]', text)
            self.assertIn('[kpis.rest.failure]', text)
            self.assertNotIn('old-route-hidden', text)
            self.assertNotIn('synthetic-erp-key-only', text)
        finally:
            parent.handlers, level, parent.propagate, root_level, child_level = previous
            parent.setLevel(level)
            root.setLevel(root_level)
            rest.log.setLevel(child_level)
            handler.close()

    def test_messages_distinguish_failures_without_raw_exception(self):
        reasons = ['connection_error', 'timeout', 'http_auth_error', 'http_error', 'response_json_invalid',
                   'response_encoding_error', 'response_schema', 'erp_key_missing_or_duplicate', 'ma110_rows_null']
        self.assertEqual(len({ui.call_error_message(r) for r in reasons}), len(reasons))
        self.assertNotIn('PRIVATE-PAYLOAD', ui.call_error_message('PRIVATE-PAYLOAD'))

    @contextmanager
    def candidate_db(self, rows):
        conn, cur = Mock(), Mock()
        conn.cursor.return_value = cur
        cur.fetchmany.return_value = rows
        with patch('app.services.ssai_auth_service.connect_company_db', return_value=conn) as connect:
            yield connect, conn, cur

    def test_candidates_first_200_order_and_limit_no_count(self):
        rows = [(f'{i // 2:05d}', '합성', f'{i:013d}', '10mg', '제약') for i in range(201)]
        for count in (0, 199, 200, 201):
            with self.candidate_db(rows[:count]) as (connect, conn, cur):
                frame = goods.search_standard_code_candidates(8, keyword='합성')
                self.assertEqual(list(frame.itertuples(index=False, name=None)), rows[:min(count, 200)])
                self.assertEqual(frame.attrs['kpis_candidates_partial'], count > 200)
                sql = cur.execute.call_args.args[0]
                self.assertIn('TOP (201)', sql)
                self.assertIn('ORDER BY [제품코드], [표준코드]', sql)
                self.assertIn('S.Rd046_Standard_Cd', sql)
                self.assertNotIn('COUNT(', sql.upper())
                connect.assert_called_once_with(8)
                cur.execute.assert_called_once()
                cur.fetchmany.assert_called_once_with(201)
                cur.close.assert_called_once()
                conn.close.assert_called_once()

    def test_partial_candidates_warning_selection_and_changed_input_reset(self):
        frame = pd.DataFrame([('00001', '합성', '0000000000001'), ('00001', '합성', '0000000000002')],
                             columns=['제품코드', '제품명', '표준코드'])
        frame.attrs['kpis_candidates_partial'] = True
        with patch.object(goods, 'search_standard_code_candidates', return_value=frame) as lookup:
            with self.fixture.screen(service='MA101') as (page, factory, push):
                page.radio[0].set_value('ERP 제품 검색').run(timeout=20)
                page.text_input[0].set_value('합성').run(timeout=20)
                next(b for b in page.button if b.label == '제품 검색').click().run(timeout=20)
                self.assertTrue(any('전체 결과가 아니므로' in x.value for x in page.warning))
                page.multiselect[0].set_value(['00001']).run(timeout=20)
                page.multiselect[1].set_value(['0000000000001', '0000000000002']).run(timeout=20)
                self.assertEqual(len(page.multiselect[1].value), 2)
                factory.assert_not_called()
                page.text_input[0].set_value('다른 제품').run(timeout=20)
                self.assertFalse(page.multiselect)
                self.assertFalse(any('전체 결과가 아니므로' in x.value for x in page.warning))
                lookup.assert_called_once()
                push.assert_not_called()

    def test_ma102_new_name_old_alias_and_single_publish(self):
        self.assertEqual(ui.KPIS_ACTIONS['KPIS 거래처 조회 (MA102)'], 'MA102')
        self.assertIn('거래처 정보 조회 (MA102)', ui.KPIS_VIEWS)
        with self.fixture.screen(service='MA102') as (page, factory, push):
            self.assertEqual(page.subheader[0].value, 'KPIS 거래처 조회 (MA102)')
            next(v for v in page.text_input if v.label == '거래처 사업자번호').set_value('0000000001')
            next(v for v in page.button if v.label == 'KPIS 조회').click().run(timeout=20)
            self.assertFalse(page.error)
            self.assertEqual(push.call_args.args[0]['title'], 'KPIS 거래처 조회 (MA102)')
            self.assertEqual(json.loads(self.f.sent[-1].content)['msupSplyFrmCd'], '5')
            page.run(timeout=20)
            push.assert_called_once()
            factory.assert_called_once()


if __name__ == '__main__':
    unittest.main()
