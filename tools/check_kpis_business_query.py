"""DB-free KPIS task forms, ERP candidates and full-history projection."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from io import BytesIO, StringIO
import csv
import json
import unittest
from unittest.mock import Mock, patch

import httpx
import pandas as pd
from openpyxl import load_workbook
from streamlit.testing.v1 import AppTest

from app.services import rddbc040_service as goods
from app.services.external_api_management import ApiManagementError
from app.services.ssai_login_profile_service import ERP_IDENTITY_KEY
from app.ui import kpis_erp_query as ui, chat_middleware as chat, ssai_login as login
from app.ui.kpis_result_display import attachment_label
from tools import check_kpis_query_ui as old_ui


def history(code):
    row = dict.fromkeys(ui.STANDARD_COLUMNS, '')
    row.update(stdCd=code, korMgdsNm='합성 제품', adptFrDt='20200101', adptToDt='20221231',
               upCost='123.40', cnclDt='20260101', tranTkovrAdptFrDt='20230201', tranTkovrAdptToDt='')
    return [row, dict(row, adptFrDt='20230101', adptToDt='', upCost='456.70'), dict(row)]


class BusinessChecks(unittest.TestCase):
    def setUp(self):
        self.fixture = old_ui.QueryUI(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f
        self.scope = (8, '42', 12345)
        self.candidates = pd.DataFrame([
            ('00001', '합성 하나', '0000000000001'),
            ('00001', '합성 하나', '0000000000002'),
            ('00002', '합성 둘', '0000000000002'),
            ('00003', '합성 셋', None),
        ], columns=['제품코드', '제품명', '표준코드'])

    def history_handler(self, request):
        self.f.sent.append(request)
        code = json.loads(request.content)['msupStdCd']
        return httpx.Response(200, json={'result': 'success', 'msupStdCdList': history(code)})

    def assert_history_payload(self, payload, expected):
        normalized = chat._normalize_result_for_chat(payload)
        frame = normalized['df']
        self.assertEqual(list(frame)[:18], list(ui.STANDARD_COLUMNS.values()))
        actual = frame[list(ui.STANDARD_COLUMNS.values())].rename(columns={v: k for k, v in ui.STANDARD_COLUMNS.items()}).to_dict('records')
        self.assertEqual(actual, expected)
        self.assertEqual(normalized['meta']['row_count_total'], len(expected))
        self.assertEqual(normalized['meta']['kpis_history_scope'], '전체 변경 이력')
        self.assertIn('전체 변경 이력', normalized['meta']['query_summary'])
        csv_file, excel_file = chat._make_table_downloads(frame)
        csv_rows = list(csv.DictReader(StringIO(csv_file.getvalue().decode('utf-8-sig'))))
        self.assertEqual([{k: row[k] for k in ui.STANDARD_COLUMNS.values()} for row in csv_rows],
                         frame[list(ui.STANDARD_COLUMNS.values())].to_dict('records'))
        book = load_workbook(BytesIO(excel_file.getvalue()), read_only=True)
        try:
            rows = list(book.active.values)
            self.assertEqual(rows[0], tuple(frame.columns))
            self.assertEqual([tuple('' if v is None else v for v in row) for row in rows[1:]],
                             list(frame.itertuples(index=False, name=None)))
        finally:
            book.close()

    def test_three_modes_identical_18_fields_and_all_history(self):
        for mode, term in (('표준코드 직접 입력', '0000000000001'), ('ERP 제품 검색', '합성'), ('ERP 제품 검색', '00001')):
            self.f.sent.clear()
            self.f.state.clear()
            self.f.state.update({login.SESSION_AUTH_USER: type('User', (), {'user_id': 42})(),
                                 login.SESSION_COMPANY: self.f.company, ERP_IDENTITY_KEY: self.f.identity})
            with self.subTest(mode=mode), patch.object(self.f, 'handler', self.history_handler), patch.object(
                    goods, 'search_standard_code_candidates', return_value=self.candidates) as lookup:
                with self.fixture.screen(service='MA101') as (page, factory, push):
                    self.assertFalse(page.selectbox)
                    page.radio[0].set_value(mode).run(timeout=20)
                    page.text_input[0].set_value(term).run(timeout=20)
                    lookup.assert_not_called()
                    factory.assert_not_called()
                    if mode != '표준코드 직접 입력':
                        next(b for b in page.button if b.label == '제품 검색').click().run(timeout=20)
                        self.assertFalse(page.exception)
                        lookup.assert_called_once_with(8, **({'keyword': term} if term == '합성' else {'physic_cd': term}))
                        page.multiselect[0].set_value(['00001']).run(timeout=20)
                        self.assertEqual(page.multiselect[1].options, ['0000000000001', '0000000000002'])
                        self.assertEqual(page.multiselect[1].value, ['0000000000001', '0000000000002'])
                        page.multiselect[1].set_value(['0000000000001']).run(timeout=20)
                    next(b for b in page.button if b.label == 'KPIS 조회').click().run(timeout=20)
                    self.assertFalse(page.exception)
                    self.assertFalse(page.error)
                    self.assertEqual(len(self.f.sent), 1)
                    push.assert_called_once()
                    self.assert_history_payload(push.call_args.args[0], history('0000000000001'))
                    page.run(timeout=20)
                    push.assert_called_once()
                    self.assertEqual(len(self.f.sent), 1)
                    if mode == '표준코드 직접 입력':
                        lookup.assert_not_called()

    def test_multi_products_and_codes_no_representative_no_history_dedup(self):
        with patch.object(self.f, 'handler', self.history_handler), patch.object(
                goods, 'search_standard_code_candidates', return_value=self.candidates):
            with self.fixture.screen(service='MA101') as (page, factory, push):
                page.radio[0].set_value('ERP 제품 검색').run(timeout=20)
                page.text_input[0].set_value('합성').run(timeout=20)
                next(b for b in page.button if b.label == '제품 검색').click().run(timeout=20)
                page.multiselect[0].set_value(['00001', '00002']).run(timeout=20)
                self.assertEqual(page.multiselect[1].value, ['0000000000001', '0000000000002'])
                next(b for b in page.button if b.label == 'KPIS 조회').click().run(timeout=20)
                self.assertFalse(page.exception)
                self.assertEqual(len(self.f.sent), 2)
                push.assert_called_once()
                self.assert_history_payload(push.call_args.args[0], history('0000000000001') + history('0000000000002'))

    def test_zero_candidates_and_no_standard_mapping(self):
        with patch.object(goods, 'search_standard_code_candidates', return_value=self.candidates.iloc[:0]) as lookup:
            with self.fixture.screen(service='MA101') as (page, factory, _):
                page.radio[0].set_value('ERP 제품 검색').run(timeout=20)
                page.text_input[0].set_value('합성').run(timeout=20)
                next(b for b in page.button if b.label == '제품 검색').click().run(timeout=20)
                self.assertTrue(any('검색 결과가 없습니다' in x.value for x in page.info))
                lookup.return_value = self.candidates.iloc[3:]
                next(b for b in page.button if b.label == '제품 검색').click().run(timeout=20)
                page.multiselect[0].set_value(['00003']).run(timeout=20)
                self.assertTrue(page.warning)
                self.assertEqual(page.multiselect[1].options, [])
                factory.assert_not_called()

    def test_search_change_company_switch_and_revocation_discard_candidates(self):
        with patch.object(goods, 'search_standard_code_candidates', return_value=self.candidates) as lookup:
            with self.fixture.screen(service='MA101') as (page, factory, _):
                page.radio[0].set_value('ERP 제품 검색').run(timeout=20)
                page.text_input[0].set_value('합성').run(timeout=20)
                next(b for b in page.button if b.label == '제품 검색').click().run(timeout=20)
                self.assertTrue(page.multiselect)
                page.text_input[0].set_value('다른 검색').run(timeout=20)
                self.assertFalse(page.multiselect)
                lookup.assert_called_once()
                self.f.actor = replace(self.f.actor, company_id=4, epoch=999)
                self.f.state[login.SESSION_COMPANY] = dict(company_id=4, is_active=True)
                self.f.state[ERP_IDENTITY_KEY] = replace(self.f.identity, company_id=4)
                page.run(timeout=20)
                self.assertFalse(page.multiselect)
                self.assertEqual(page.text_input[0].value, '')
                self.f.actor = None
                page.run(timeout=20)
                self.assertFalse(page.button)
                self.assertTrue(page.error)
                factory.assert_not_called()

    def test_search_switch_during_query_never_stores_result(self):
        def switch(*args, **kwargs):
            self.f.actor = replace(self.f.actor, epoch=99)
            return self.candidates
        with patch.object(goods, 'search_standard_code_candidates', side_effect=switch):
            with self.fixture.screen(service='MA101') as (page, factory, _):
                page.radio[0].set_value('ERP 제품 검색').run(timeout=20)
                page.text_input[0].set_value('00001').run(timeout=20)
                next(b for b in page.button if b.label == '제품 검색').click().run(timeout=20)
                self.assertTrue(page.error)
                self.assertFalse(page.multiselect)
                factory.assert_not_called()

    def test_sequential_failure_stops_remaining_and_no_partial_publish(self):
        requests = [{'msupStdCd': f'{i:013d}'} for i in (1, 2, 3)]
        client = Mock()
        client.call.side_effect = [dict(status='SUCCESS', rows=history('0000000000001'), physical_call_count=1),
                                   dict(status='DENIED', reason_code='timeout', rows=[])]
        with patch.object(ui, 'load_current_api_session', return_value=self.f.actor), patch.object(
                ui, 'configured_direct_client', return_value=client):
            result = ui._call_selected('MA101', requests, self.scope)
        self.assertEqual(client.call.call_count, 2)
        self.assertEqual(result['status'], 'DENIED')
        self.assertEqual(result['rows'], [])

    def test_all_inputs_validate_before_any_key_or_http(self):
        with patch.object(ui, 'configured_direct_client') as client:
            for queries in ([], [{'msupStdCd': '0000000000001'}] * 21,
                            [{'msupStdCd': '0000000000001'}, {'msupStdCd': 'bad'}]):
                with self.assertRaises(ApiManagementError):
                    ui._call_selected('MA101', queries, self.scope)
            client.assert_not_called()

    def test_real_chat_stash_keeps_all_history_current_table_and_export(self):
        with patch.object(ui, 'load_current_api_session', return_value=self.f.actor), patch.object(
                self.f, 'handler', self.history_handler):
            result = self.f.call(request=replace(self.f.req, service='MA101'), query={'msupStdCd': '0000000000001'})
            original = deepcopy(result)
            with patch.object(chat, 'drain_inbox_to_chat'), patch.object(chat, '_append_finalized_nlq_case'), patch.object(
                    chat, 'maybe_create_structured_presentation', return_value={'status': 'skipped'}):
                self.assertTrue(ui.publish_result(result, {'msupStdCd': '0000000000001'}, self.scope,
                                                 load_session=lambda: self.f.actor, push=chat.wssz))
            self.assertEqual(result, original)
            state = self.f.state
            source_key = state['__sims_current_table_source_key']
            display = state['sims_tables'][source_key]
            export = state['sims_export_tables'][source_key]
            pd.testing.assert_frame_equal(display, export)
            self.assertEqual(len(export), 3)
            self.assertEqual(list(export), list(ui.STANDARD_COLUMNS.values()))
            self.assertEqual(export.rename(columns={v: k for k, v in ui.STANDARD_COLUMNS.items()}).to_dict('records'), history('0000000000001'))
            self.assertEqual(len(state['__chat_inbox']), 1)

    def test_ma102_ma110_conditions_and_ma116_disabled(self):
        for service, count in [('MA102', 3), ('MA110', 2), ('MA116', 3)]:
            with self.subTest(service=service), self.fixture.screen(service=service) as (page, factory, _):
                self.assertFalse(any(s.label == '서비스' for s in page.selectbox))
                self.assertEqual(len(page.text_input), count)
                if service == 'MA116':
                    self.assertTrue(page.button[0].disabled)
                    page.button[0].click().run(timeout=20)
                    self.assertTrue(page.info)
                factory.assert_not_called()


class CandidateSqlChecks(unittest.TestCase):
    @contextmanager
    def db(self, rows):
        connection, cursor = Mock(), Mock()
        connection.cursor.return_value = cursor
        cursor.fetchmany.return_value = rows
        with patch('app.services.ssai_auth_service.connect_company_db', return_value=connection) as connect:
            yield connect, connection, cursor

    def test_company_bound_like_and_exact_code_sql_mapping(self):
        for kwargs, expression, value in [({'keyword': "제품' OR 1=1"}, 'LIKE ?', "%제품' OR 1=1%"),
                                           ({'physic_cd': '00001'}, '= ?', '00001')]:
            with self.subTest(kwargs=kwargs), self.db([('00001', '합성', '0000000000001', '10mg', '합성제약'), ('00001', '합성', '0000000000002', '10mg', '합성제약')]) as (connect, conn, cur):
                result = goods.search_standard_code_candidates(8, **kwargs)
                connect.assert_called_once_with(8)
                sql, *params = cur.execute.call_args.args
                self.assertIn(expression, sql)
                self.assertEqual(params, [value])
                self.assertNotIn(value, sql)
                self.assertIn('TOP (201)', sql)
                self.assertIn('LEFT JOIN dbo.Rddbc046', sql)
                self.assertIn('= S.Rd046_Physic_Cd', sql)
                self.assertNotIn('TOP (1)', sql)
                self.assertEqual(len(result), 2)
                self.assertEqual(conn.timeout, 10)
                cur.close.assert_called_once()
                conn.close.assert_called_once()

    def test_empty_wildcard_and_oversize_queries_never_connect(self):
        with patch('app.services.ssai_auth_service.connect_company_db') as connect:
            for kwargs in ({}, {'keyword': '%_'}, {'keyword': 'a' * 101}, {'physic_cd': '123456'},
                           {'keyword': '제품', 'physic_cd': '00001'}):
                with self.assertRaises(ValueError):
                    goods.search_standard_code_candidates(8, **kwargs)
            connect.assert_not_called()

    def test_excess_rows_return_first_200_with_explicit_partial_metadata(self):
        with self.db([('00001', '합성', '0000000000001', '10mg', '합성제약')] * 201) as (_, conn, cur):
            result = goods.search_standard_code_candidates(8, keyword='합성')
            self.assertEqual(len(result), 200)
            self.assertTrue(result.attrs['kpis_candidates_partial'])
            cur.fetchmany.assert_called_once_with(201)
            cur.close.assert_called_once()
            conn.close.assert_called_once()

    def test_sql_error_closes_without_retry(self):
        with self.db([]) as (_, conn, cur):
            cur.execute.side_effect = RuntimeError('synthetic')
            with self.assertRaises(RuntimeError):
                goods.search_standard_code_candidates(8, physic_cd='00001')
            cur.execute.assert_called_once()
            cur.close.assert_called_once()
            conn.close.assert_called_once()


class NoticeDetailChecks(unittest.TestCase):
    def test_attachment_label(self):
        self.assertEqual([attachment_label(v) for v in ['Y', 'N', None, '?']], ['있음', '없음', '미확인', '미확인'])

    def test_full_notice_selection_no_fetch_no_html_execution(self):
        source = """
import pandas as pd
from app.ui.kpis_result_display import render_notice_detail
df = pd.DataFrame([{'공지번호':'1','제목':'합성 첫 공지','내용':'첫 줄\\n'+('전체 본문 '*500),'첨부여부':'있음'},
                   {'공지번호':'2','제목':'합성 둘째','내용':'<script>HIDDEN</script><p>둘째<br>마지막</p>','첨부여부':'없음'}])
render_notice_detail(df, key='synthetic')
"""
        with patch('app.services.ssai_auth_service.connect_company_db', side_effect=AssertionError('no_db')), patch(
                'app.services.kpis_rest_client.KpisClient.call', side_effect=AssertionError('no_api')):
            page = AppTest.from_string(source).run(timeout=20)
            self.assertFalse(page.exception)
            self.assertFalse(page.text)
            page.selectbox[0].set_value(0).run(timeout=20)
            self.assertEqual(page.text[1].value.count('전체 본문'), 500)
            self.assertTrue(page.text[1].value.startswith('첫 줄\n'))
            self.assertFalse(page.dataframe)
            page.selectbox[0].set_value(1).run(timeout=20)
            self.assertEqual(page.text[1].value, '둘째\n마지막')
            self.assertNotIn('HIDDEN', page.text[1].value)
            self.assertIn('없음', page.caption[0].value)

    def test_chat_hook_is_company_scoped_and_keeps_single_table(self):
        source = """
import pandas as pd
from app.ui import chat_middleware as chat
data = pd.DataFrame([{'공지번호':'1','제목':'합성 공지','내용':'전체 내용\\n둘째 줄','첨부여부':'있음'}])
item = {'type':'table','id':'synthetic-notice','title':'KPIS 공지','action':'KPIS 공지','data':data,
        'meta':{'source':'KPIS','service':'MA117','_ssai_company_id':8,'row_count_total':1}}
chat._render_chat_item_body(item)
"""
        with patch.object(chat, '_should_full_render_sims_table', return_value=True), patch.object(
                chat, '_should_render_sims_message_once', return_value=True), patch.object(
                chat, '_get_chat_render_anchor', return_value=None), patch('app.ui.ssai_login.get_selected_company',
                return_value={'company_id': 8}) as selected, patch('app.services.ssai_auth_service.connect_company_db',
                side_effect=AssertionError('no_db')), patch('app.services.kpis_rest_client.KpisClient.call',
                side_effect=AssertionError('no_api')):
            page = AppTest.from_string(source).run(timeout=20)
            self.assertFalse(page.exception)
            pick = next(x for x in page.selectbox if x.label == '공지번호 / 제목')
            pick.set_value(0).run(timeout=20)
            self.assertTrue(any(x.value == '전체 내용\n둘째 줄' for x in page.text))
            self.assertEqual(len(page.dataframe), 1)
            selected.return_value = {'company_id': 4}
            page.run(timeout=20)
            self.assertFalse(any(x.label == '공지번호 / 제목' for x in page.selectbox))


if __name__ == '__main__':
    unittest.main()
