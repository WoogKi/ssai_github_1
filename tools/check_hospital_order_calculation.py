"""DB-free hospital allocation, source-query, routing and display contracts."""

from datetime import date
from contextlib import closing
from decimal import Decimal as D
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
import sqlite3
import sys
import unittest
import time

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services import hospital_order_calculation_service as h
from app.ui.hospital_order_calculation_display import build_display


def master():
    return pd.DataFrame([
        ('00100', 'fixture A', '70001', '80002'),
        ('00101', 'fixture B', '70001', '80001'),
        ('00200', 'fixture C', '70002', '80001'),
        ('00300', 'fixture D', '50001', '80001'),
        ('70001', 'group A', '', ''), ('70002', 'group B', '', ''),
        ('50001', '재고적용거래처', '', ''),
        ('80001', 'cost A', '', ''), ('80002', 'cost B', '', ''),
    ], columns=h.MASTER_COLUMNS)


def parents():
    return pd.DataFrame([{'회사': 3, '제품코드': f'P{i}', '제품명': f'fixture {i}', '규격': 'unit',
                         '발주일자': '2026-11-16', '발주처코드': 'V1', '발주처': 'fixture vendor',
                         '계산 발주수량': D(q), '추천 발주수량': D(q), '실제 발주수량': D(q),
                         '발주단위': 10, '입고예정수량': D(480 if i == 2 else 0),
                         '재고수량': D(100), '재고적용처': 'base group',
                         '발주단가': D(100), '단가출처': 'fixture'}
                        for i, q in enumerate((540, 460, 1600), 1)])


def totals(rows=()):
    return pd.DataFrame([{'product': p, 'stock_apply': g, 'quantity': D(q),
                          'bonus': D(0), 'source_rows': 2} for p, g, q in rows],
                        columns=['product', 'stock_apply', 'quantity', 'bonus', 'source_rows'])


def sources():
    return dict(master=master(), sales=totals([
        ('P1', '70001', 200), ('P1', '70002', 150),
        ('P2', '70001', 300), ('P2', '70002', 200),
        ('P3', '70001', 500), ('P3', '70002', 300)]),
        orders=totals([('P1', '70001', 100), ('P1', '70002', 100), ('P3', '70001', 300)]),
        contracts={(f'P{i}', '80002'): D(50) for i in range(1, 4)})


def large_display_fixture():
    original = pd.concat([parents().iloc[[0]]] * 1115, ignore_index=True)
    original['제품코드'] = [f'fixture-{i}' for i in range(1115)]
    original = pd.concat([original, pd.DataFrame({
        f'검증수량_{i}': [D(i)] * 1115 for i in range(100)
    })], axis=1)
    seed = h.allocate(parents(), company=3, **sources()).iloc[[0]]
    frame = pd.concat([seed] * 1789, ignore_index=True)
    frame['제품코드'] = [f'fixture-{i % 1115}' for i in range(1789)]
    frame.loc[1115:, '배분 재고적용처코드'] = '70002'
    original['실제 발주수량'] = [D(200 if i < 674 else 100) for i in range(1115)]
    original.loc[0, '입고예정수량'] = None
    frame.attrs[h.REFERENCE_ATTR] = original.to_dict('records')
    return original, frame


class HospitalTests(unittest.TestCase):
    def test_display_does_not_copy_parent_attrs_per_group(self):
        from copy import deepcopy
        class CountCopies(list):
            copies = 0
            def __deepcopy__(self, memo):
                type(self).copies += 1
                return deepcopy(list(self), memo)
        original = parents()
        frame = self.run_allocation()
        records = CountCopies(original.to_dict('records'))
        frame.attrs[h.REFERENCE_ATTR] = records
        result = build_display(original, frame)
        self.assertEqual(CountCopies.copies, 0)
        self.assertIs(frame.attrs[h.REFERENCE_ATTR], records)
        self.assertEqual(len(result), len(original) + len(frame))

    def test_large_display_and_excel_full_values(self):
        import pyarrow as pa
        import openpyxl
        from app.ui import hospital_order_calculation_display as display
        from app.ui import chat_middleware as chat
        original, frame = large_display_fixture()
        saved = pd.DataFrame(frame, copy=True)
        records = frame.attrs[h.REFERENCE_ATTR]
        started = time.perf_counter()
        shown = build_display(original, frame, max_rows=300)
        view, _, _ = display._order_table_display_config(shown, editing=False)
        pa.Table.from_pandas(view, preserve_index=False)
        display_seconds = time.perf_counter() - started
        self.assertEqual(len(shown), 300)
        started = time.perf_counter()
        with patch.object(h, 'get_current_company_id', return_value=3), \
             patch.object(h.base, 'get_order_calculation_result', side_effect=AssertionError('no parent recalc')), \
             patch.object(h, 'load_allocation_sources', side_effect=AssertionError('no allocation query')), \
             patch.object(h, 'allocate', side_effect=AssertionError('no allocation recalc')), \
             patch('app.services.snapshot_product_information_service.project_product_information_frame_for_viewer', side_effect=lambda df: df.copy()):
            csv, excel = chat._make_table_downloads(frame)
        excel_seconds = time.perf_counter() - started
        workbook = openpyxl.load_workbook(excel)
        self.assertEqual(workbook.sheetnames, ['실제배분', '참고표_합계제외'])
        actual, reference = workbook.worksheets
        self.assertEqual(actual.max_row - 1, 1789)
        self.assertEqual(reference.max_row - 1, 1789 + 1115)
        self.assertEqual(list(next(actual.values)), list(frame.columns))
        self.assertEqual(list(next(reference.values)), list(shown.columns))
        for sheet in (actual, reference):
            header = list(next(sheet.values))
            cells = [row[header.index('실제 발주수량')] for row in list(sheet.rows)[1:]]
            self.assertEqual(sum(D(str(c.value)) for c in cells if c.data_type == 'n' and c.value is not None), D(178900))
        actual_rows = list(actual.values)
        for column in ('발주단가', '발주금액(부가세포함)'):
            index = actual_rows[0].index(column)
            self.assertEqual(sum(D(str(row[index])) for row in actual_rows[1:]), sum(frame[column]))
        header = list(next(reference.values))
        self.assertIsNone(reference.cell(2, header.index('입고예정수량') + 1).value)
        self.assertEqual(len(pd.read_csv(csv)), 1789)
        pd.testing.assert_frame_equal(pd.DataFrame(frame), saved)
        self.assertIs(frame.attrs[h.REFERENCE_ATTR], records)
        print(f'HOSPITAL_BENCH display_arrow_300={display_seconds:.3f}s excel_1789={excel_seconds:.3f}s bytes={len(excel.getvalue())}')

    def test_allocation_page_boundaries_preserve_all_rows_and_parent_context(self):
        original, full = large_display_fixture()
        extra = full.iloc[:164].copy()
        extra['배분 재고적용처코드'] = '70003'
        all_rows = pd.concat([full, extra], ignore_index=True)
        for count, expected_pages in ((1, 1), (299, 1), (300, 1), (301, 2), (1944, 7), (1953, 7)):
            with self.subTest(count=count):
                source = all_rows.iloc[:count]
                page_count = (len(source) + 299) // 300
                self.assertEqual(page_count, expected_pages)
                seen = []
                for page in range(1, page_count + 1):
                    visible = source.iloc[(page - 1) * 300:page * 300]
                    shown = build_display(original, visible, max_rows=len(original) + len(visible))
                    self.assertEqual(shown['배분 재고적용처'].notna().sum(), len(visible))
                    self.assertTrue(shown.iloc[0]['제품코드'])
                    seen.extend(visible.index.tolist())
                self.assertEqual(seen, source.index.tolist())
                if count >= 1944:
                    self.assertEqual(len(source.iloc[1800:]), count - 1800)
        split = all_rows.iloc[[0] * 301].copy()
        split['배분 재고적용처코드'] = [f'fixture-{i}' for i in range(301)]
        next_page = build_display(original.iloc[:1], split.iloc[300:], max_rows=2)
        self.assertEqual(len(next_page), 2)
        self.assertEqual(next_page.iloc[0]['제품코드'], original.iloc[0]['제품코드'])
        self.assertEqual(next_page.iloc[1]['실제 발주수량'], split.iloc[300]['실제 발주수량'])

    def test_lazy_download_prepare_and_rerun_cache(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from app.ui import chat_middleware as chat
        from app.ui import hospital_order_calculation_display as display
        class Rerun(Exception):
            pass
        _, frame = large_display_fixture()
        state = {}
        fake_st = SimpleNamespace(session_state=state, columns=lambda n: [MagicMock() for _ in range(n)],
                                  caption=MagicMock(), button=MagicMock(return_value=True),
                                  rerun=MagicMock(side_effect=Rerun))
        kwargs = dict(key_suffix='fixture', table_key='fixture', download_df=frame,
                      csv_name='fixture.csv', xlsx_name='fixture.xlsx', prompt='', clicked_action=h.ACTION,
                      expected_rows=1789, display_rows=300)
        with patch.object(chat, 'st', fake_st), \
             patch.object(chat, '_select_sims_analysis_ctx_for_table', return_value=({}, 'fixture')), \
             patch.object(chat, '_get_sims_download_lazy_threshold_rows', return_value=1), \
             patch.object(chat, '_render_sims_result_actions_fragment') as rendered, \
             patch.object(h, 'get_current_company_id', return_value=3), \
             patch.object(h.base, 'get_order_calculation_result', side_effect=AssertionError('no parent recalc')), \
             patch.object(h, 'load_allocation_sources', side_effect=AssertionError('no allocation query')), \
             patch.object(h, 'allocate', side_effect=AssertionError('no allocation recalc')), \
             patch('app.services.snapshot_product_information_service.project_product_information_frame_for_viewer', side_effect=lambda df: df.copy()), \
             patch.object(display, 'write_hospital_excel_if_any', wraps=display.write_hospital_excel_if_any) as writer:
            with self.assertLogs('ssai', level='INFO') as logs:
                with self.assertRaises(Rerun):
                    chat._render_sims_result_actions_lazy(**kwargs)
                self.assertEqual(writer.call_count, 0)
                chat._render_sims_result_actions_lazy(**kwargs)
                chat._render_sims_result_actions_lazy(**kwargs)
            self.assertEqual(writer.call_count, 1)
            self.assertEqual(rendered.call_count, 2)
            self.assertTrue(any('requested' in line for line in logs.output))
            self.assertTrue(any('[chat.download.prepare] start' in line for line in logs.output))
            self.assertTrue(any('[chat.download.excel] complete' in line for line in logs.output))
            self.assertEqual(rendered.call_args_list[0].kwargs['excel_bytes'], rendered.call_args_list[1].kwargs['excel_bytes'])
            self.assertEqual(len(pd.read_csv(BytesIO(rendered.call_args.kwargs['csv_bytes']))), 1789)
            self.assertEqual(len(pd.read_excel(BytesIO(rendered.call_args.kwargs['excel_bytes']), sheet_name='실제배분')), 1789)

    def test_lazy_excel_failure_is_logged_without_error_payload(self):
        from types import SimpleNamespace
        from app.ui import chat_middleware as chat
        from app.ui import hospital_order_calculation_display as display
        frame = self.run_allocation()
        frame.attrs[h.REFERENCE_ATTR] = parents().to_dict('records')
        state = {'__sims_download_ready::failure': True}
        def fail_after_sheet(writer, *args, **kwargs):
            writer.book.create_sheet('fixture')
            raise ValueError('fixture-private-payload')
        with patch.object(chat, 'st', SimpleNamespace(session_state=state)), \
             patch.object(chat, '_select_sims_analysis_ctx_for_table', return_value=({}, 'fixture')), \
             patch.object(chat, '_render_sims_result_actions_fragment'), \
             patch.object(display, 'write_hospital_excel_if_any', side_effect=fail_after_sheet), \
             self.assertLogs('ssai', level='WARNING') as logs:
            chat._render_sims_result_actions_lazy(key_suffix='failure', download_df=frame,
                csv_name='fixture.csv', xlsx_name='fixture.xlsx', prompt='')
        self.assertIn('ValueError', '\n'.join(logs.output))
        self.assertNotIn('fixture-private-payload', '\n'.join(logs.output))
        self.assertIsNone(state['__sims_download_bytes::failure']['excel_bytes'])

    def run_allocation(self, **kwargs):
        data = sources()
        for key in tuple(data):
            if key in kwargs:
                data[key] = kwargs.pop(key)
        return h.allocate(kwargs.pop('parents', parents()), company=3, **data, **kwargs)

    def test_sample_three_totals_and_parent_immutable(self):
        original = parents()
        before = original.copy(deep=True)
        result = self.run_allocation(parents=original)
        self.assertEqual(result['실제 발주수량'].tolist(), list(map(D, (100, 50, 390, 300, 160, 200, 300, 1100))))
        self.assertEqual(result['실제 발주수량'].sum(), D(2600))
        pd.testing.assert_frame_equal(before, original)
        self.assertFalse(result.duplicated(list(h.ALLOCATION_KEY)).any())

    def test_first_customer_not_minimum_cost_code(self):
        groups, residual = h.resolve_groups(master().iloc[::-1], [])
        self.assertEqual(groups['70001']['cost_apply'], '80002')
        self.assertEqual(residual, '50001')

    def test_first_invalid_not_silently_skipped(self):
        for code in ('', 'unknown'):
            m = master(); m.loc[m.code.eq('00100'), 'cost_apply'] = code
            with self.assertRaises(h.AllocationError):
                self.run_allocation(master=m)

    def test_empty_single_multiple_selection_and_outside_residual(self):
        full = self.run_allocation(selected=[])
        self.assertEqual(full['실제 발주수량'].sum(), D(2600))
        single = self.run_allocation(selected=['70001'])
        self.assertEqual(set(single['배분 재고적용처코드']), {'70001'})
        self.assertEqual(single['실제 발주수량'].sum(), D(600))
        self.assertEqual(sum(single.attrs[h.UNALLOCATED_ATTR].values()), D(2000))
        multiple = self.run_allocation(selected=['70002', '70001', '70001'])
        self.assertEqual(multiple['실제 발주수량'].sum(), D(1110))
        self.assertNotIn('50001', set(multiple['배분 재고적용처코드']))
        with self.assertRaises(h.AllocationError):
            self.run_allocation(selected=['another-company'])

    def test_selected_sales_scope_and_need_zero_never_create_new_order(self):
        data = sources()
        data['sales'] = totals([('P1', '70001', 100), ('P2', '70002', 200)])
        data['orders'] = totals([('P1', '70001', 100)])
        result = h.allocate(parents(), company=3, **data, selected=('70001',))
        self.assertTrue(result.empty)
        self.assertEqual(result.attrs['hospital_eligible_products'], {'P1'})
        self.assertEqual(result.attrs[h.UNALLOCATED_ATTR], {'P1': D(540)})
        data['orders'] = totals()
        result = h.allocate(parents(), company=3, **data, selected=('70001',))
        self.assertEqual(set(result['제품코드']), {'P1'})
        self.assertEqual(set(result['배분 재고적용처코드']), {'70001'})
        self.assertEqual(result['실제 발주수량'].sum(), D(100))
        self.assertEqual(result.attrs[h.UNALLOCATED_ATTR], {'P1': D(440)})
        self.assertEqual(sum(result['실제 발주수량']), D(100))
        data['sales'] = totals([('P1', '50001', 50)])
        residual_selected = h.allocate(parents(), company=3, **data, selected=('50001',))
        self.assertEqual(residual_selected['배분 재고적용처코드'].tolist(), ['50001'])
        self.assertEqual(residual_selected['실제 발주수량'].sum(), D(50))
        self.assertFalse(residual_selected['선택외 잔여배정'].any())

    def test_selected_no_sales_returns_no_parent_table(self):
        original = parents()
        payload = {'df': original, 'meta': {'result_status': 'success',
            'order_header_metrics': {'effective_business_date': '2026-11-16'},
            'query_summary': 'fixture'}}
        with patch.object(h, 'get_current_company_id', return_value=3), \
             patch.object(h, 'load_master', return_value=master()), \
             patch.object(h, 'validate_parent_stock_application', side_effect=lambda q: q), \
             patch.object(h.base, 'get_order_calculation_result', return_value=payload) as parent:
            result = h.get_hospital_order_calculation_result(
                {'company_id': 3, '_display_context': 'panel', h.SELECTION: ['70001']},
                allocation_loader=lambda *args: dict(master=master(), sales=totals(),
                                                       orders=totals(), contracts={}))
        parent.assert_called_once()
        self.assertEqual(result['meta']['result_status'], 'no_data')
        self.assertNotIn('df', result)

    def test_allocation_edit_full_and_selected_guards(self):
        from app.ui.hospital_order_calculation_display import apply_allocation_edits

        def position(keys, product, group):
            return next(i for i, key in enumerate(keys)
                        if key is not None and key[-2:] == (product, group))

        original = parents()
        full = self.run_allocation()
        full.attrs[h.REFERENCE_ATTR] = original.to_dict('records')
        full.attrs[h.ORIGINAL_ATTR] = full.to_dict('records')
        shown, keys = build_display(original, full, with_mapping=True)
        changed = apply_allocation_edits(full, keys, {
            position(keys, 'P1', '70001'): {'실제 발주수량': 120}}, selected=False)
        p1 = changed.loc[changed['제품코드'].eq('P1')]
        self.assertEqual(p1['실제 발주수량'].sum(), D(540))
        self.assertEqual(p1.loc[p1['배분 재고적용처코드'].eq('50001'), '실제 발주수량'].iloc[0], D(370))
        self.assertEqual(changed.attrs[h.ORIGINAL_ATTR], full.attrs[h.ORIGINAL_ATTR])
        self.assertEqual(full.loc[full['제품코드'].eq('P1'), '실제 발주수량'].sum(), D(540))
        with self.assertRaisesRegex(ValueError, '참고행'):
            apply_allocation_edits(full, keys, {0: {'실제 발주수량': 1}}, selected=False)
        with self.assertRaisesRegex(ValueError, '잔여처 수량이 부족'):
            apply_allocation_edits(full, keys, {
                position(keys, 'P1', '70001'): {'실제 발주수량': 500}}, selected=False)
        with self.assertRaisesRegex(ValueError, '자동 조정'):
            apply_allocation_edits(full, keys, {
                position(keys, 'P1', '50001'): {'실제 발주수량': 300}}, selected=False)
        historical = self.run_allocation(orders=totals([('P1', '80001', 5)]))
        historical.attrs[h.REFERENCE_ATTR] = original.to_dict('records')
        _, history_keys = build_display(original, historical, with_mapping=True)
        with self.assertRaisesRegex(ValueError, '기발주 전용'):
            apply_allocation_edits(historical, history_keys, {
                position(history_keys, 'P1', '80001'): {'실제 발주수량': 1}}, selected=False)
        smaller = apply_allocation_edits(full, keys, {
            position(keys, 'P2', '70001'): {'실제 발주수량': 290}}, selected=False)
        p2 = smaller.loc[smaller['제품코드'].eq('P2')]
        self.assertEqual(p2['실제 발주수량'].sum(), D(460))
        self.assertEqual(p2.loc[p2['배분 재고적용처코드'].eq('50001'), '실제 발주수량'].iloc[0], D(10))

        scoped = self.run_allocation(selected=('70001',))
        refs = original.to_dict('records')
        for row in refs:
            row['미배분수량'] = scoped.attrs[h.UNALLOCATED_ATTR][row['제품코드']]
        scoped.attrs[h.REFERENCE_ATTR] = refs
        scoped.attrs[h.ORIGINAL_ATTR] = scoped.to_dict('records')
        _, scoped_keys = build_display(pd.DataFrame(refs), scoped, with_mapping=True)
        with self.assertRaisesRegex(ValueError, '필요수량'):
            apply_allocation_edits(scoped, scoped_keys, {
                position(scoped_keys, 'P1', '70001'): {'실제 발주수량': 120}}, selected=True)
        abundant = sources()
        abundant['sales'] = totals([('P1', '70001', 600)])
        abundant['orders'] = totals()
        one = h.allocate(original.iloc[:1], company=3, selected=('70001',), **abundant)
        one.attrs[h.REFERENCE_ATTR] = original.iloc[:1].to_dict('records')
        _, one_keys = build_display(original.iloc[:1], one, with_mapping=True)
        with self.assertRaisesRegex(ValueError, '부모 발주수량'):
            apply_allocation_edits(one, one_keys, {position(one_keys, 'P1', '70001'):
                                                   {'실제 발주수량': 550}}, selected=True)
        edited = apply_allocation_edits(scoped, scoped_keys, {
            position(scoped_keys, 'P1', '70001'): {'실제 발주수량': 80}}, selected=True)
        self.assertEqual(edited.attrs[h.UNALLOCATED_ATTR]['P1'], D(460))
        self.assertEqual(edited.attrs[h.REFERENCE_ATTR][0]['미배분수량'], D(460))
        self.assertEqual(scoped.attrs[h.UNALLOCATED_ATTR]['P1'], D(440))
        self.assertEqual(edited.attrs[h.ORIGINAL_ATTR], scoped.attrs[h.ORIGINAL_ATTR])
        self.assertEqual(set(edited['배분정책']), {h.SCOPED_POLICY})
        self.assertIs(apply_allocation_edits(edited, scoped_keys, {
            position(scoped_keys, 'P1', '70001'): {'실제 발주수량': 80}}, selected=True), edited)
        from app.ui import chat_middleware as chat
        with patch.object(h, 'get_current_company_id', return_value=3), \
             patch('app.services.snapshot_product_information_service.project_product_information_frame_for_viewer',
                   side_effect=lambda df: df.copy()):
            csv, excel = chat._make_table_downloads(edited)
        self.assertEqual(pd.read_csv(csv)['실제 발주수량'].sum(), 580)
        self.assertEqual(pd.read_excel(excel, sheet_name='실제배분')['실제 발주수량'].sum(), 580)

    def test_editor_callback_updates_current_table_and_download_without_recalculation(self):
        from unittest.mock import MagicMock
        from app.ui import hospital_order_calculation_display as display
        from app.ui import chat_middleware as chat
        import streamlit as st
        original = parents()
        full = self.run_allocation()
        full.attrs[h.REFERENCE_ATTR] = original.to_dict('records')
        full.attrs[h.ORIGINAL_ATTR] = full.to_dict('records')
        _, keys = build_display(original, full, with_mapping=True)
        position = next(i for i, key in enumerate(keys)
                        if key is not None and key[-2:] == ('P1', '70001'))
        state = {'__sims_current_table_source_key': 'fixture',
                 'sims_export_tables': {'fixture': full},
                 '__sims_export_tables_by_key': {'fixture': full},
                 '__sims_table_render_cache': {'fixture:old': object()},
                 '__sims_download_ready::fixture-uid': True,
                 '__sims_download_bytes::fixture-uid': {'csv_bytes': b'old'}}
        meta = {'table_key': 'fixture', 'allocation_quantity': '2600', 'allocation_selected': False}
        item = {'id': 'message', 'room_id': 'room', 'params': {}, 'meta': meta}
        with patch.object(st, 'session_state', state), \
             patch.object(st, 'data_editor') as editor, \
             patch.object(st, 'caption'), patch.object(st, 'error'), \
             patch('app.db.mssql_client.get_current_company_id', return_value=3), \
             patch.object(h, 'get_current_company_id', return_value=3), \
             patch('app.services.snapshot_product_information_service.project_product_information_frame_for_viewer',
                   side_effect=lambda df: df.copy()), \
             patch.object(chat, '_build_sims_context_from_result') as context, \
             patch.object(h.base, 'get_order_calculation_result', side_effect=AssertionError('no parent recalc')), \
             patch.object(h, 'load_allocation_sources', side_effect=AssertionError('no source query')):
            self.assertTrue(display.render_hospital_order_table(item, meta, download_uid='fixture-uid'))
            self.assertEqual(editor.call_count, 1)
            widget_key = editor.call_args.kwargs['key']
            self.assertIn('실제 발주수량', editor.call_args.args[0].data.columns)
            state[widget_key] = {'edited_rows': {position: {'실제 발주수량': 120}}}
            editor.call_args.kwargs['on_change']()
            context.assert_called_once()
            self.assertEqual(meta.get('editor_widget_revision', 0), 0)
            changed = state['sims_export_tables']['fixture']
            self.assertIs(changed, state['__sims_export_tables_by_key']['fixture'])
            self.assertEqual(changed.loc[changed['제품코드'].eq('P1'), '실제 발주수량'].sum(), D(540))
            self.assertEqual(changed.attrs[h.ORIGINAL_ATTR], full.attrs[h.ORIGINAL_ATTR])
            self.assertFalse(state['__sims_table_render_cache'])
            self.assertNotIn('__sims_download_ready::fixture-uid', state)
            self.assertNotIn('__sims_download_bytes::fixture-uid', state)
            state['__sims_download_ready::fixture-uid'] = True
            editor.call_args.kwargs['on_change']()
            self.assertIs(state['sims_export_tables']['fixture'], changed)
            self.assertIn('__sims_download_ready::fixture-uid', state)
            self.assertEqual(meta['quantity_edit_revision'], 1)
            csv, excel = chat._make_table_downloads(changed)
        self.assertEqual(pd.read_csv(csv)['실제 발주수량'].sum(), 2600)
        self.assertEqual(pd.read_excel(excel, sheet_name='실제배분')['실제 발주수량'].sum(), 2600)
        self.assertEqual(pd.read_excel(excel, sheet_name='참고표_합계제외')['배분 재고적용처'].notna().sum(), len(changed))

    def test_editor_callback_rejects_parent_row_and_company_change(self):
        from app.ui import hospital_order_calculation_display as display
        import streamlit as st
        original = parents()
        full = self.run_allocation()
        full.attrs[h.REFERENCE_ATTR] = original.to_dict('records')
        state = {'__sims_current_table_source_key': 'fixture', 'sims_export_tables': {'fixture': full}}
        meta = {'table_key': 'fixture', 'allocation_selected': False}
        item = {'id': 'message', 'room_id': 'room', 'meta': meta}
        with patch.object(st, 'session_state', state), patch.object(st, 'data_editor') as editor, \
             patch.object(st, 'caption'), patch.object(st, 'error'), \
             patch('app.db.mssql_client.get_current_company_id', return_value=3) as company, \
             patch('app.services.snapshot_product_information_service.project_product_information_frame_for_viewer',
                   side_effect=lambda df: df.copy()):
            display.render_hospital_order_table(item, meta)
            widget = editor.call_args.kwargs['key']
            state[widget] = {'edited_rows': {0: {'실제 발주수량': 1}}}
            editor.call_args.kwargs['on_change']()
            self.assertIn('참고행', state['hospital_edit_error::fixture'])
            self.assertEqual(meta['editor_widget_revision'], 1)
            self.assertIs(state['sims_export_tables']['fixture'], full)
            company.return_value = 4
            editor.call_args.kwargs['on_change']()
            self.assertIn('회사 또는 현재표', state['hospital_edit_error::fixture'])

    def test_residual_requires_code_not_name(self):
        frame = master().loc[master().code.ne('50001')]
        with self.assertRaises(h.MasterError) as failure:
            self.run_allocation(master=frame)
        self.assertEqual(failure.exception.reason, 'residual_missing')
        frame = pd.concat([master(), pd.DataFrame([
            ('99999', '재고적용거래처', '', '')], columns=h.MASTER_COLUMNS)])
        self.assertEqual(h.resolve_groups(frame)[1], '50001')

    def test_priority_contract_before_cheaper_fallback_and_tie(self):
        p = parents().iloc[:1].copy(); p['실제 발주수량'] = D(100)
        data = sources(); data['sales'] = totals([('P1', '70001', 100), ('P1', '70002', 100)])
        data['orders'] = totals(); data['contracts'] = {('P1', '80002'): D(200)}
        result = h.allocate(p, company=3, **data)
        self.assertEqual(result.iloc[0]['배분 재고적용처코드'], '70001')
        self.assertEqual(result.iloc[0]['발주단가'], D(200))
        data['contracts'][('P1', '80001')] = D(150)
        self.assertEqual(h.allocate(p, company=3, **data).iloc[0]['배분 재고적용처코드'], '70002')
        data['contracts'][('P1', '80001')] = D(200)
        self.assertEqual(h.allocate(p, company=3, **data).iloc[0]['배분 재고적용처코드'], '70001')

    def test_named_stock_apply_groups_follow_all_ordinary_groups(self):
        extra = pd.DataFrame([
            ('00400', 'special member A', '70003', '80003'),
            ('00401', 'special member B', '70004', '80004'),
            ('70003', '재고적용 A', '', ''), ('70004', 'B재고적용', '', ''),
            ('80003', 'special cost A', '', ''), ('80004', 'special cost B', '', ''),
        ], columns=h.MASTER_COLUMNS)
        frame = pd.concat([master(), extra], ignore_index=True)
        p = parents().iloc[:1].copy()
        p['실제 발주수량'] = D(450)
        data = dict(master=frame, sales=totals([
            ('P1', '70001', 100), ('P1', '70002', 100),
            ('P1', '70003', 100), ('P1', '70004', 100)]), orders=totals(),
            contracts={('P1', '80002'): D(200), ('P1', '80001'): D(100),
                       ('P1', '80003'): D(1), ('P1', '80004'): D(1)})
        result = h.allocate(p, company=3, **data)
        self.assertEqual(result['배분 재고적용처코드'].tolist(),
                         ['70002', '70001', '70003', '70004', '50001'])
        self.assertEqual(result['실제 발주수량'].tolist(), list(map(D, (100, 100, 100, 100, 50))))
        self.assertEqual(result['실제 발주수량'].sum(), D(450))
        # A cheaper special contract never outranks an ordinary group without a contract.
        data['contracts'].pop(('P1', '80002'))
        result = h.allocate(p, company=3, **data)
        self.assertEqual(result['배분 재고적용처코드'].tolist()[:3], ['70002', '70001', '70003'])
        selected = h.allocate(p, company=3, **data,
                              selected=('70001', '70002', '70003', '70004', '50001'))
        self.assertNotIn('50001', selected['배분 재고적용처코드'].tolist())
        self.assertEqual(selected['실제 발주수량'].sum(), D(400))
        self.assertEqual(sum(selected.attrs[h.UNALLOCATED_ATTR].values()), D(50))
        p['실제 발주수량'] = D(150)
        scarce = h.allocate(p, company=3, **data)
        self.assertEqual(scarce['배분 재고적용처코드'].tolist(), ['70002', '70001'])
        self.assertEqual(scarce['실제 발주수량'].tolist(), [D(100), D(50)])

    def test_negative_need_preserved_without_negative_allocation(self):
        result = self.run_allocation(orders=totals([('P1', '70001', 1000)]))
        self.assertFalse((result['실제 발주수량'] < 0).any())
        evidence = result.loc[result['제품코드'].eq('P1') & result['배분 재고적용처코드'].eq('70001')].iloc[0]
        self.assertEqual(evidence['현재발주수량'], D(-800))
        self.assertEqual(evidence['실제 발주수량'], D(0))
        self.assertEqual(result['실제 발주수량'].sum(), D(2600))

    def test_zero_none_and_invalid_numbers(self):
        zero = parents(); zero['실제 발주수량'] = D(0)
        result = self.run_allocation(parents=zero)
        self.assertEqual(len(result), 3)
        self.assertEqual(result['실제 발주수량'].sum(), 0)
        for invalid in (None, D('NaN'), D('Infinity'), D('-1'), D('1.5')):
            p = parents(); p.loc[0, '실제 발주수량'] = invalid
            with self.assertRaises(h.AllocationError):
                self.run_allocation(parents=p)

    def test_mapping_duplicates_company_period_and_missing_aggregates(self):
        broken = parents(); broken.loc[0, '회사'] = 4
        with self.assertRaises(h.AllocationError): self.run_allocation(parents=broken)
        broken = parents(); broken.loc[0, '발주일자'] = '2026-10-01'
        with self.assertRaises(h.AllocationError): self.run_allocation(parents=broken)
        with self.assertRaises(h.AllocationError): self.run_allocation(parents=pd.concat([parents(), parents()]))
        with self.assertRaises(h.AllocationError): self.run_allocation(sales=None)
        for sales in (totals([('P1', '', 5)]), totals([('P1', 'missing', 5)]),
                      totals([('P1', '70001', 5), ('P1', '70001', 5)])):
            with self.assertRaises(h.AllocationError): self.run_allocation(sales=sales)

    def test_missing_price_preserved_as_unknown(self):
        p = parents(); p['발주단가'] = None
        result = self.run_allocation(parents=p, contracts={})
        self.assertTrue(result['발주공급가액'].isna().all())
        self.assertTrue(result['단가판정'].eq('미확정').all())

    def test_display_blank_rules_and_export_sum(self):
        original = parents(); result = self.run_allocation(parents=original)
        saved = result.copy(deep=True)
        display = build_display(original, result)
        self.assertEqual(len(display), 11)
        self.assertEqual(display['실제 발주수량'].sum(), D(5200))
        self.assertEqual(result['실제 발주수량'].sum(), D(2600))
        children = display.loc[display['제품코드'].isna()]
        self.assertTrue(children[display.columns[:display.columns.get_loc('실제 발주수량')]].isna().all().all())
        self.assertTrue(children.loc[:, '입고예정수량':].isna().all().all())
        self.assertTrue(display.columns.is_unique)
        self.assertIn('원계산 재고적용처', display)
        pd.testing.assert_frame_equal(saved, result)
        buffer = BytesIO()
        result.to_excel(buffer, index=False)
        buffer.seek(0)
        exported = pd.read_excel(buffer)
        self.assertEqual(exported['실제 발주수량'].sum(), 2600)
        self.assertEqual(len(build_display(original, result, max_rows=4)), 4)

    def test_sql_dates_roles_complete_orders_and_binds(self):
        params = {'stock_cd_list': ['00001'], '_order_saved_stock_cd_list': ['00001'],
                  'io_gu_list': ['501', '502', '601']}
        queries = h.build_aggregate_queries(params, ['P1'], date(2026, 11, 16), 100)
        sales, orders = queries
        self.assertEqual(sales[1][:2], ['20261001', '20261101'])
        self.assertIn('V.Rd03_Ven_Cd = O.Rd12_Ven_Cd', sales[0])
        self.assertNotIn('O.Rd12_Stock_Apply_Cd', sales[0])
        self.assertNotIn('601', sales[1])
        self.assertIn("D.Rd18_Or_Di IN ('1','2','3')", orders[0])
        self.assertNotIn('Rd18_In_Quantity', orders[0])
        self.assertIn('SUM(D.Rd18_Quantity)', orders[0])
        self.assertIn('D.Rd18_Stock_Apply_Cd', orders[0])
        self.assertIn('20261101', orders[1]); self.assertIn('20261116', orders[1])
        self.assertNotIn('__SAVED_STOCK_CODES__', orders[0])
        for sql, values in queries:
            self.assertEqual(sql.count('?'), len(values))
        many = h.build_aggregate_queries(params, [f'P{i}' for i in range(2100)], date(2026, 1, 2), 100)
        self.assertLess(len(many[0][1]), 20)
        self.assertEqual(many[0][1][0], '20251201')

    def test_sqlite_sales_multiple_customers_and_normal_codes(self):
        params = {'stock_cd_list': ['00001'], 'io_gu_list': ['501', '601']}
        sql, values = h.build_aggregate_queries(params, ['P1'], date(2026, 11, 16), 100)[0]
        sql = sql.replace('TOP 101 ', '').replace('dbo.', '').replace('COUNT_BIG', 'COUNT')
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.executescript('''CREATE TABLE Rddbc030(Rd03_Ven_Cd TEXT PRIMARY KEY, Rd03_Stock_Apply_Cd TEXT);
                CREATE TABLE Rddbc120(Rd12_Physic_Cd TEXT, Rd12_Ven_Cd TEXT, Rd12_Quantity INTEGER,
                Rd12_Oquantity INTEGER, Rd12_Out_YyMmDd TEXT, Rd12_Io_Gu_Gcode TEXT, Rd12_Io_Gu TEXT,
                Rd12_Stock_Cd_Gcode TEXT, Rd12_Stock_Cd TEXT);''')
            conn.executemany('INSERT INTO Rddbc030 VALUES (?,?)', [('C1', 'G1'), ('C2', 'G1')])
            conn.executemany('INSERT INTO Rddbc120 VALUES (?,?,?,?,?,?,?,?,?)', [
                ('P1', 'C1', 20, 2, '20261001', '0012', '501', '0018', '00001'),
                ('P1', 'C2', 30, 3, '20261031', '0012', '501', '0018', '00001'),
                ('P1', 'C1', -4, 0, '20261031', '0012', '501', '0018', '00001'),
                ('P1', 'C1', -10, 0, '20261031', '0012', '601', '0018', '00001'),
                ('P1', 'C1', 99, 0, '20261101', '0012', '501', '0018', '00001')])
            row = conn.execute(sql, values).fetchone()
            self.assertEqual(row, ('P1', 'G1', 46, 5, 3, 0))

    def test_source_loader_contract_reuse_limits_and_unknown_prices(self):
        s = sources()
        params = {'company_id': 3, 'stock_cd_list': ['00001'], 'io_gu_list': ['501']}
        with patch.object(h, 'get_current_company_id', return_value=3), \
             patch.object(h, 'load_master', return_value=master()), \
             patch.object(h, 'query_to_df', side_effect=[s['sales'], s['orders']]), \
             patch('app.services.rddbc070_service.get_rddbc070_current_df', return_value=pd.DataFrame()) as price:
            loaded = h.load_allocation_sources(params, parents(), date(2026, 11, 16), [])
        self.assertEqual(price.call_count, 2)
        self.assertEqual(loaded['contracts'], {})
        for call in price.call_args_list:
            self.assertEqual(call.args[0]['as_of'], '20261116')
            self.assertTrue(call.args[0]['_order_contract_projection_only'])
        with self.assertRaises(h.AllocationError): self.run_allocation(limit=2)

    def test_existing_parent_engine_called_once_and_unchanged(self):
        from tools.check_order_calculation_contract import fixture
        params, base_sources = fixture()
        params.update(_display_context='panel', company_id=7)
        with patch.object(h, 'get_current_company_id', return_value=7), \
             patch.object(h, 'load_master', return_value=master()), \
             patch.object(h.base, 'get_current_company_id', return_value=7), \
             patch.object(h.base, 'load_application_names', return_value=pd.DataFrame([{'code': '50001', 'name': 'fixture default'}])):
            before = h.base.get_order_calculation_result(params, source_loader=lambda q: base_sources)
            calls = []
            def parent_load(q): calls.append(q.copy()); return base_sources
            def allocation_load(q, p, reference, selected):
                self.assertNotIn(h.SELECTION, q)
                return dict(master=master(), sales=totals([
                    (p.iloc[0]['제품코드'], '70001', 1)]), orders=totals(), contracts={})
            actual = h.get_hospital_order_calculation_result({**params, h.SELECTION: ['70001']},
                source_loader=parent_load, allocation_loader=allocation_load)
            after = h.base.get_order_calculation_result(params, source_loader=lambda q: base_sources)
        self.assertEqual(len(calls), 1)
        self.assertEqual(actual['meta']['result_status'], 'success')
        self.assertLessEqual(actual['df']['실제 발주수량'].sum(), before['df']['실제 발주수량'].sum())
        self.assertFalse(actual['meta']['order_calculation_editable'])
        self.assertEqual(actual['meta']['order_header_metrics'], before['meta']['order_header_metrics'])
        self.assertEqual(actual['meta']['order_display_conditions']['stock_application'], 'group A')
        self.assertIn('재고적용처 group A', actual['meta']['summary_md'])
        self.assertGreaterEqual(actual['meta']['elapsed_ms'], before['meta']['elapsed_ms'])
        self.assertNotIn('_hospital_reference_records', actual['meta'])
        self.assertEqual(len(actual['df'].attrs[h.REFERENCE_ATTR]), 1)
        pd.testing.assert_frame_equal(before['df'], after['df'])

    def test_hospital_header_reuses_order_cards_with_actual_allocation_scope(self):
        from app.ui import chat_middleware as chat
        item = {'action': h.ACTION, 'params': {'company_id': 12}}
        meta = {'hospital_order_calculation': True, 'parent_product_count': 1405,
                'row_count_total': 1953, 'row_count': 300, 'table_key': 'fixture',
                'query_summary': '입력 발주일 2026-11-15 | 배분 재고적용처 전체',
                'order_header_metrics': {'order_date': '2026-11-15',
                    'effective_business_date': '2026-11-16', 'query_mode': '발주해당자료만',
                    'safety_days': 3, 'target_days': 15, 'closing_day': 25},
                'order_display_conditions': {'stock_location': '00001',
                    'cost_application': '50002', 'stock_application': '전체'},
                'elapsed_ms': 13500}
        view = chat._build_sims_result_header_view(item, meta)
        self.assertEqual(view['line1'], view['line2'])
        self.assertEqual([card[0] for card in view['order_condition_cards']],
                         ['발주일', '유효 발주일', '조회구분', '안전재고', '적정재고', '결제일'])
        self.assertEqual([card[0] for card in view['order_cards']],
                         ['발주대상', '재고위치', '단가적용처', '재고적용처', '처리시간'])
        self.assertEqual([card[1] for card in view['order_cards']],
                         [1405, '00001', '50002', '전체', 13.5])
        self.assertIn('배분 재고적용처 전체', '\n'.join(view['details']))
        with patch.object(chat.st, 'caption'), patch.object(chat.st, 'container') as container, \
             patch.object(chat.st, 'expander'), patch.object(chat.st, 'markdown'), \
             patch.object(chat, '_chat_metric_card') as card:
            chat._render_sims_result_header_view(view)
        self.assertEqual(card.call_count, 11)
        selected = {**meta, 'order_display_conditions': {**meta['order_display_conditions'],
                    'stock_application': '50592', 'stock_location': '2곳 선택',
                    'stock_location_codes': '00001, 00002'}}
        selected_view = chat._build_sims_result_header_view(item, selected)
        self.assertEqual(selected_view['order_cards'][3][1], '50592')
        self.assertEqual(selected_view['order_cards'][1][1], '2곳 선택')
        self.assertIn('적용 재고위치코드: 00001, 00002', '\n'.join(selected_view['details']))
        with patch.object(chat.st, 'markdown') as markdown:
            chat._chat_metric_card('단가적용처', '50002', text_only=True)
            self.assertIn('>50002</span>', markdown.call_args.args[0])
            self.assertNotIn('50,002', markdown.call_args.args[0])
            chat._chat_metric_card('재고적용처', '<unsafe>', text_only=True)
            self.assertIn('&lt;unsafe&gt;', markdown.call_args.args[0])

    def test_registry_permissions_routing_and_nlq_zero_db(self):
        from app.sims.meta.erp_table_feature_registry import registry_completeness_errors, menu_actions, get_action_spec
        from app.services.ssai_permission_policy import get_required_permission
        from app.services.erp_table_nlq import resolve_registered_erp_table_nlq, is_order_calculation_request
        self.assertEqual(registry_completeness_errors(), [])
        self.assertEqual(menu_actions(business_group='재고관리', target=h.ACTION), (h.ACTION,))
        self.assertEqual(get_required_permission(action=h.ACTION), 'IO_READ')
        self.assertNotEqual(get_action_spec(h.ACTION)[1].payload_key, get_action_spec('발주 계산')[1].payload_key)
        for text in ('병원발주계산', '병원 발주 계산 재고적용처 70001'):
            self.assertFalse(is_order_calculation_request(text))
            resolved = resolve_registered_erp_table_nlq(text)
            self.assertEqual(resolved['action'], h.ACTION)
            with patch('app.ui.ssai_login.has_permission', return_value=False), \
                 patch.object(h.base, 'get_order_calculation_result', side_effect=AssertionError('must not execute')):
                result = h.get_hospital_order_calculation_result(resolved['params'])
            self.assertTrue(result['meta']['service_call_skipped'])
            self.assertEqual(result['meta']['result_status'], 'permission_denied')
        self.assertTrue(is_order_calculation_request('발주 계산'))

    def test_production_nlq_dispatch_permission_denied(self):
        import logging
        from app.sims.nlq.nlq_router import _try_handle_io_nlq
        sent = []
        with patch('app.ui.ssai_login.has_permission', return_value=False), \
             patch.object(h.base, 'get_order_calculation_result', side_effect=AssertionError('ERP forbidden')), \
             patch('app.ui.chat_middleware.push_sims_result_to_chat', side_effect=lambda p, a: sent.append(p) or p['meta']):
            handled = _try_handle_io_nlq('병원발주계산 재고적용처 70001', room={}, session_state={},
                make_ts=lambda: 'fixture', next_seq=lambda: 1, logger=logging.getLogger('fixture'))
        self.assertTrue(handled)
        self.assertEqual(sent[0]['action'], h.ACTION)
        self.assertTrue(sent[0]['meta']['service_call_skipped'])
        self.assertEqual(sent[0]['meta']['result_status'], 'permission_denied')

    def test_panel_multiselect_submit_and_company_reset(self):
        from streamlit.testing.v1 import AppTest
        requests = []
        def service(params, **kwargs):
            requests.append(dict(params))
            return {'action': h.ACTION, 'final': True, 'data': 'fixture'}
        script = ('from app.sims.views.order_calculation_view import view_hospital_order_calculation\n'
                  "view_hospital_order_calculation({'order_date': '2026-11-16'})")
        with patch('app.db.mssql_client.get_current_company_id', return_value=3) as company, \
             patch.object(h, 'group_options', return_value={'70001': 'group A', '50001': 'residual'}), \
             patch.object(h, 'get_hospital_order_calculation_result', side_effect=service), \
             patch('app.sims.views.order_calculation_view.render_product_master_filters', return_value={}):
            app = AppTest.from_string(script).run()
            self.assertFalse(app.exception)
            self.assertEqual(app.button[0].label, h.ACTION)
            app.multiselect[0].select('70001')
            app.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0][h.SELECTION], ['70001'])
            self.assertEqual(requests[0]['stock_apply_cd'], '50001')
            self.assertEqual(requests[0]['_display_context'], 'panel')
            self.assertEqual(len(app.dataframe), 0)
            app.session_state['__io_hospital_order_calculation_last_payload'] = {'company': 3}
            company.return_value = 4
            app.run()
            self.assertFalse(app.exception)
            self.assertEqual(app.multiselect[0].value, [])
            self.assertNotIn('__io_hospital_order_calculation_last_payload', app.session_state)

    def test_parent_input_matrix_before_any_calculation(self):
        default_code = h.base.apply_application_defaults({})['stock_apply_cd']
        cases = [
            ('', 'group A', False, 0),
            ('70001', 'group A', True, 1),
            ('70001', '', True, 1),
            ('', '', True, 1),
            ('70001', 'other', False, 1),
            ('missing', '', False, 1),
        ]
        def lookup(codes):
            names = {'70001': 'group A', default_code: 'fixture default'}
            return pd.DataFrame([{'code': c, 'name': names[c]} for c in codes if c in names], columns=['code','name'])
        for code, name, valid, lookup_count in cases:
            with self.subTest(code=code, name=name), \
                 patch.object(h, 'get_current_company_id', return_value=3), \
                 patch.object(h, 'load_master', return_value=master()), \
                 patch.object(h.base, 'load_application_names', side_effect=lookup) as identity, \
                 patch.object(h.base, 'get_order_calculation_result', return_value={'meta': {'result_status': 'input_required'}}) as parent:
                source = unittest.mock.Mock(side_effect=AssertionError('parent source forbidden'))
                allocation = unittest.mock.Mock(side_effect=AssertionError('allocation forbidden'))
                params = {'company_id':3,'_display_context':'panel','stock_apply_cd':code,
                          'stock_apply_nm':name,h.SELECTION:['70001']}
                original = params.copy()
                result = h.get_hospital_order_calculation_result(params,source_loader=source,allocation_loader=allocation)
                self.assertEqual(parent.call_count, int(valid))
                self.assertEqual(identity.call_count, lookup_count)
                source.assert_not_called(); allocation.assert_not_called()
                self.assertEqual(params, original)
                if valid:
                    submitted = parent.call_args.args[0]
                    self.assertEqual(submitted['stock_apply_cd'], code or default_code)
                    self.assertEqual(submitted['stock_apply_nm'], lookup([code or default_code]).iloc[0]['name'])
                    self.assertNotIn(h.SELECTION, submitted)
                else:
                    self.assertTrue(result['meta']['service_call_skipped'])
                    self.assertEqual(result['params'][h.SELECTION], ['70001'])

    def test_parent_identity_fail_closed_and_company_boundary(self):
        valid = pd.DataFrame([{'code':'70001','name':'group A'}])
        for identities in (pd.DataFrame(),pd.concat([valid,valid]),
                           pd.DataFrame([{'code':'other','name':'group A'}]),
                           pd.DataFrame([{'code':'70001','name':''}])):
            with patch.object(h,'get_current_company_id',return_value=3), \
                 patch.object(h.base,'load_application_names',return_value=identities):
                with self.assertRaises(h.AllocationError):
                    h.validate_parent_stock_application({'company_id':3,'stock_apply_cd':'70001'})
        with patch.object(h,'get_current_company_id',return_value=4), \
             patch.object(h.base,'load_application_names') as lookup, \
             patch.object(h.base,'get_order_calculation_result') as parent:
            with self.assertRaises(h.AllocationError):
                h.get_hospital_order_calculation_result({'company_id':3,'_display_context':'panel','stock_apply_cd':'70001'})
            lookup.assert_not_called(); parent.assert_not_called()
        with patch.object(h,'get_current_company_id',side_effect=[3,4]), \
             patch.object(h.base,'load_application_names',return_value=valid):
            with self.assertRaises(h.AllocationError):
                h.validate_parent_stock_application({'company_id':3,'stock_apply_cd':'70001'})

    def test_existing_name_only_guard_preserved(self):
        from tools.check_order_calculation_contract import fixture
        params, data = fixture()
        normal = h.base.assemble_result(params, data)
        params.update(stock_apply_cd='',stock_apply_nm='fixture')
        unresolved = h.base.assemble_result(params, data)
        self.assertGreater(normal.iloc[0]['실제 발주수량'], 0)
        self.assertGreater(unresolved.iloc[0]['계산 발주수량'], 0)
        self.assertIsNone(unresolved.iloc[0]['추천 발주수량'])
        self.assertIsNone(unresolved.iloc[0]['실제 발주수량'])
        needed = h.base.apply_application_defaults({**params,'query_mode':'발주해당자료만'})
        self.assertTrue(h.base.assemble_result(needed,data).empty)

    def test_exact_application_lookup_contract(self):
        with patch.object(h.base,'query_to_df',return_value=pd.DataFrame()) as query:
            h.base.load_application_names(['70001'])
            sql, values = query.call_args.args
            self.assertIn('Rd03_Ven_Cd IN (?)',sql)
            self.assertNotIn('LIKE',sql)
            self.assertEqual(values,('70001',))
            query.reset_mock()
            self.assertTrue(h.base.load_application_names([]).empty)
            query.assert_not_called()

    def test_panel_keeps_submitted_pair_and_allocation_independent(self):
        from streamlit.testing.v1 import AppTest
        requests = []
        prefix = '__io_hospital_order_calculation'
        def service(params, **kwargs):
            requests.append(dict(params))
            return {'action':h.ACTION,'final':True,'data':'fixture'}
        script = ('from app.sims.views.order_calculation_view import view_hospital_order_calculation\n'
                  "view_hospital_order_calculation({'order_date':'2026-11-16'})")
        with patch('app.db.mssql_client.get_current_company_id',return_value=3), \
             patch.object(h,'group_options',return_value={'70001':'group A','70002':'group B'}), \
             patch.object(h,'get_hospital_order_calculation_result',side_effect=service), \
             patch('app.sims.views.order_calculation_view.render_product_master_filters',return_value={}):
            app = AppTest.from_string(script).run()
            app.session_state[prefix+'_last_source'] = {'company_id':3,
                'source_params':{'stock_apply_cd':'50001','stock_apply_nm':'old name'}}
            app.text_input(key=prefix+'_stock_apply_cd').set_value('70001')
            app.text_input(key=prefix+'_stock_apply_nm').set_value('group A')
            app.multiselect[0].set_value(['70001','70002'])
            app.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertEqual(requests[-1]['stock_apply_nm'],'group A')
            self.assertEqual(requests[-1][h.SELECTION],['70001','70002'])
            app.text_input(key=prefix+'_stock_apply_cd').set_value('')
            app.button[0].click().run()
            self.assertEqual(requests[-1]['stock_apply_cd'],'')
            self.assertEqual(requests[-1]['stock_apply_nm'],'group A')
            self.assertEqual(requests[-1][h.SELECTION],['70001','70002'])
            app.text_input(key=prefix+'_stock_apply_nm').set_value('')
            app.multiselect[0].set_value([])
            app.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertEqual(requests[-1]['stock_apply_cd'],'')
            self.assertEqual(requests[-1]['stock_apply_nm'],'')

    def test_panel_clears_foreign_defaults_on_company_change(self):
        from streamlit.testing.v1 import AppTest
        prefix = '__io_hospital_order_calculation'
        script = ('from app.sims.views.order_calculation_view import view_hospital_order_calculation\n'
                  "view_hospital_order_calculation({'company_id':3,'order_date':'2026-11-16',"
                  "'stock_apply_cd':'70001','stock_apply_nm':'old company',"
                  "'allocation_stock_apply_codes':['70001']})")
        with patch('app.db.mssql_client.get_current_company_id',return_value=4), \
             patch.object(h,'group_options',return_value={'50001':'new residual'}), \
             patch('app.sims.views.order_calculation_view.render_product_master_filters',return_value={}):
            app = AppTest.from_string(script).run()
            self.assertFalse(app.exception)
            self.assertEqual(app.text_input(key=prefix+'_stock_apply_nm').value,'')
            self.assertEqual(app.text_input(key=prefix+'_stock_apply_cd').value,
                             h.base.apply_application_defaults({})['stock_apply_cd'])
            self.assertEqual(app.multiselect[0].value,[])

    def test_panel_invalid_input_never_calls_parent_or_allocation(self):
        from streamlit.testing.v1 import AppTest
        prefix = '__io_hospital_order_calculation'
        results = []
        real_service = h.get_hospital_order_calculation_result
        allocation = unittest.mock.Mock(side_effect=AssertionError('allocation forbidden'))
        def checked(params, **kwargs):
            result = real_service(params, allocation_loader=allocation, **kwargs)
            results.append(result)
            return result
        script = ('from app.sims.views.order_calculation_view import view_hospital_order_calculation\n'
                  "view_hospital_order_calculation({'order_date':'2026-11-16'})")
        with patch('app.db.mssql_client.get_current_company_id',return_value=3), \
             patch.object(h,'get_current_company_id',return_value=3), \
             patch.object(h,'group_options',return_value={'70001':'group A'}), \
             patch.object(h,'get_hospital_order_calculation_result',side_effect=checked), \
             patch.object(h.base,'load_application_names',return_value=pd.DataFrame([{'code':'70001','name':'group A'}])) as identity, \
             patch.object(h.base,'get_order_calculation_result',side_effect=AssertionError('parent forbidden')) as parent, \
             patch('app.sims.views.order_calculation_view.load_sources',side_effect=AssertionError('source forbidden')) as source, \
             patch('app.sims.views.order_calculation_view.render_product_master_filters',return_value={}):
            app = AppTest.from_string(script).run()
            app.text_input(key=prefix+'_stock_apply_cd').set_value('')
            app.text_input(key=prefix+'_stock_apply_nm').set_value('group A')
            app.button[0].click().run()
            self.assertFalse(app.exception)
            identity.assert_not_called()
            self.assertIn('코드가 없습니다',results[-1]['message'])
            app.text_input(key=prefix+'_stock_apply_cd').set_value('70001')
            app.text_input(key=prefix+'_stock_apply_nm').set_value('wrong')
            app.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertIn('일치하지 않습니다',results[-1]['message'])
            self.assertEqual(identity.call_count,1)
            parent.assert_not_called(); source.assert_not_called(); allocation.assert_not_called()

    def test_readonly_chat_renderer_uses_reference_and_canonical_frames(self):
        from streamlit.testing.v1 import AppTest
        script = '''
import streamlit as st
from tools.check_hospital_order_calculation import parents, sources
from app.services.hospital_order_calculation_service import allocate, REFERENCE_ATTR
from app.ui.hospital_order_calculation_display import render_hospital_order_table
original = parents()
full = allocate(original, company=3, **sources())
full.attrs[REFERENCE_ATTR] = original.to_dict('records')
st.session_state['sims_export_tables'] = {'fixture': full}
render_hospital_order_table({}, {'table_key': 'fixture', 'allocation_quantity': '2600'})
'''
        with patch('app.db.mssql_client.get_current_company_id', return_value=3), \
             patch('app.services.snapshot_product_information_service.project_product_information_frame_for_viewer', side_effect=lambda df: df.copy()):
            app = AppTest.from_string(script).run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.dataframe), 1)
        self.assertEqual(len(app.dataframe[0].value), 11)
        self.assertEqual(app.session_state['sims_export_tables']['fixture']['실제 발주수량'].sum(), D(2600))

    def test_current_chat_renderer_uses_editor_without_db(self):
        from streamlit.testing.v1 import AppTest
        script = '''
import streamlit as st
from tools.check_hospital_order_calculation import parents, sources
from app.services.hospital_order_calculation_service import allocate, REFERENCE_ATTR
from app.ui.hospital_order_calculation_display import render_hospital_order_table
if 'sims_export_tables' not in st.session_state:
    original = parents()
    full = allocate(original, company=3, **sources())
    full.attrs[REFERENCE_ATTR] = original.to_dict('records')
    st.session_state['sims_export_tables'] = {'fixture': full}
st.session_state['__sims_current_table_source_key'] = 'fixture'
render_hospital_order_table({'id': 'message', 'room_id': 'room'},
                            {'table_key': 'fixture', 'allocation_quantity': '2600'})
'''
        with patch('app.db.mssql_client.get_current_company_id', return_value=3), \
             patch('app.services.snapshot_product_information_service.project_product_information_frame_for_viewer',
                   side_effect=lambda df: df.copy()), \
             patch.object(h.base, 'get_order_calculation_result', side_effect=AssertionError('no recalc')), \
             patch.object(h, 'load_allocation_sources', side_effect=AssertionError('no source query')):
            app = AppTest.from_string(script).run(timeout=30)
        self.assertFalse(app.exception)
        self.assertEqual(len(app.dataframe), 1)
        self.assertEqual(len(app.dataframe[0].value), 11)

    def test_hospital_page_widget_reuses_full_source_without_db_or_recalculation(self):
        from streamlit.testing.v1 import AppTest
        script = '''
import pandas as pd
import streamlit as st
from tools.check_hospital_order_calculation import large_display_fixture
from app.services.hospital_order_calculation_service import REFERENCE_ATTR
from app.ui.hospital_order_calculation_display import render_hospital_order_table
if 'sims_export_tables' not in st.session_state:
    original, full = large_display_fixture()
    extra = full.iloc[:155].copy()
    extra['배분 재고적용처코드'] = '70003'
    full = pd.concat([full, extra], ignore_index=True)
    full.attrs[REFERENCE_ATTR] = original.to_dict('records')
    st.session_state['sims_export_tables'] = {'fixture': full}
render_hospital_order_table({'id': st.session_state.get('message_id', 'message-1'),
                             'room_id': st.session_state.get('room_id', 'room-a')},
                            {'table_key': 'fixture', 'allocation_quantity': '194400'})
'''
        with patch('app.db.mssql_client.get_current_company_id', return_value=3) as current_company, \
             patch('app.services.snapshot_product_information_service.project_product_information_frame_for_viewer',
                   side_effect=lambda df: df.copy()), \
             patch.object(h.base, 'get_order_calculation_result', side_effect=AssertionError('no parent recalc')), \
             patch.object(h, 'load_allocation_sources', side_effect=AssertionError('no source query')):
            app = AppTest.from_string(script).run(timeout=30)
            self.assertFalse(app.exception)
            self.assertEqual(len(app.selectbox), 1)
            self.assertEqual(len(app.button), 0)
            self.assertEqual(len(app.session_state['sims_export_tables']['fixture']), 1944)
            app.selectbox[0].set_value(7).run(timeout=30)
            self.assertFalse(app.exception)
            self.assertEqual(app.selectbox[0].value, 7)
            self.assertEqual(app.dataframe[0].value['배분 재고적용처'].fillna('').astype(str).str.strip().ne('').sum(), 144)
            app.selectbox[0].set_value(6).run(timeout=30)
            self.assertFalse(app.exception)
            self.assertEqual(app.selectbox[0].value, 6)
            app.selectbox[0].set_value(7).run(timeout=30)
            self.assertFalse(app.exception)
            self.assertEqual(app.selectbox[0].value, 7)
            app.session_state['room_id'] = 'room-b'
            app.run(timeout=30)
            self.assertFalse(app.exception)
            self.assertEqual(app.selectbox[0].value, 1)
            self.assertEqual(len(app.session_state['sims_export_tables']['fixture']), 1944)
            app.selectbox[0].set_value(7).run(timeout=30)
            app.session_state['message_id'] = 'message-2'
            app.run(timeout=30)
            self.assertFalse(app.exception)
            self.assertEqual(app.selectbox[0].value, 1)
            current_company.return_value = 4
            app.run(timeout=30)
            self.assertFalse(app.exception)
            self.assertEqual(len(app.dataframe), 0)

    def test_contract_authority_price_map_and_company_switch(self):
        s = sources()
        params = {'company_id': 3, 'stock_cd_list': ['00001'], 'io_gu_list': ['501']}
        def prices(query):
            return pd.DataFrame([{'단가적용거래처': query['ven_cd'], '제품코드': 'P1',
                '실입고단가': D(25), '장부입고단가': D(30), 'otc 입고단가': D(35)}])
        with patch.object(h, 'get_current_company_id', return_value=3), \
             patch.object(h, 'load_master', return_value=master()), \
             patch.object(h, 'query_to_df', side_effect=[s['sales'], s['orders']]), \
             patch('app.services.rddbc070_service.get_rddbc070_current_df', side_effect=prices):
            loaded = h.load_allocation_sources(params, parents(), date(2026, 11, 16), [])
        self.assertEqual(loaded['contracts'][('P1', '80002')], D(25))
        self.assertEqual(loaded['contracts'][('P1', '80001')], D(25))
        with patch.object(h, 'get_current_company_id', side_effect=[3, 4]), \
             patch.object(h, 'load_master', return_value=master()), \
             patch.object(h, 'query_to_df', side_effect=AssertionError('company changed')):
            with self.assertRaises(h.AllocationError):
                h.load_allocation_sources(params, parents(), date(2026, 11, 16), [])

    def test_chat_stash_preserves_reference_outside_history_and_totals(self):
        import streamlit as st
        from app.ui import chat_middleware as chat
        original = parents()
        frame = self.run_allocation(parents=original)
        frame.attrs[h.REFERENCE_ATTR] = original.to_dict('records')
        payload = {'action': h.ACTION, 'title': h.ACTION, 'df': frame, 'df_display': frame.copy(),
                   'params': {'company_id': 3, '_display_context': 'panel'}, 'final': True,
                   'meta': {'hospital_order_calculation': True, 'company_id': 3,
                            'row_count_total': len(frame), 'result_status': 'success', 'allocation_quantity': '2600'}}
        state = {'__sims_push_count': 0}
        with patch.object(st, 'session_state', state), \
             patch.object(chat, 'wire_chat_context'), patch.object(chat, 'drain_inbox_to_chat'), \
             patch.object(chat, '_append_finalized_nlq_case'), \
             patch.object(chat, 'maybe_create_structured_presentation', return_value={'status': 'skipped'}), \
             patch.object(chat, '_chat_current_company_sig', return_value=('3', '')):
            chat.push_sims_result_to_chat(payload, h.ACTION)
        self.assertEqual(len(state['__chat_inbox']), 1)
        item = state['__chat_inbox'][0]
        key = item['meta']['table_key']
        full = state['sims_export_tables'][key]
        self.assertEqual(full['실제 발주수량'].sum(), D(2600))
        self.assertEqual(len(full.attrs[h.REFERENCE_ATTR]), 3)
        self.assertNotIn(h.REFERENCE_ATTR, item['meta'])
        self.assertEqual(state['__sims_current_table_source_key'], key)

    def test_excel_reference_sheet_no_double_sum_and_legacy_unchanged(self):
        from app.ui.chat_middleware import _make_table_downloads
        frame = self.run_allocation()
        frame.attrs[h.REFERENCE_ATTR] = parents().to_dict('records')
        with patch.object(h, 'get_current_company_id', return_value=3), \
             patch('app.services.snapshot_product_information_service.project_product_information_frame_for_viewer', side_effect=lambda df: df.copy()):
            csv, excel = _make_table_downloads(frame)
        sheets = pd.read_excel(excel, sheet_name=None)
        self.assertEqual(list(sheets), ['실제배분', '참고표_합계제외'])
        for sheet in sheets.values():
            self.assertEqual(pd.to_numeric(sheet['실제 발주수량'], errors='coerce').sum(), 2600)
        reference = sheets['참고표_합계제외']
        self.assertEqual(reference['실제 발주수량'].astype(str).str.startswith('참고:').sum(), 3)
        self.assertEqual(len(pd.read_csv(csv)), 8)
        _, legacy = _make_table_downloads(parents())
        self.assertEqual(list(pd.read_excel(legacy, sheet_name=None)), ['SIMS'])

    def test_export_company_and_policy_boundary(self):
        import streamlit as st
        frame = self.run_allocation()
        with patch.object(st, 'session_state', {'sims_export_tables': {'hospital': frame}}), \
             patch.object(h, 'get_current_company_id', return_value=3):
            exported = h.get_hospital_order_calculation_export_df({'table_key': 'hospital'})
            self.assertEqual(exported['실제 발주수량'].sum(), D(2600))
            exported.loc[0, '실제 발주수량'] = -1
            self.assertEqual(frame.loc[0, '실제 발주수량'], D(100))
            with self.assertRaises(h.AllocationError):
                h.get_hospital_order_calculation_export_df({'table_key': 'hospital', 'company_id': 4})


class MasterPreflightTests(unittest.TestCase):
    def test_source_contract_reasons_are_distinct_and_fail_closed(self):
        original = master()
        products = {'P1'}
        cases = [
            (totals([('P2', '70001', 1)]), 'product_out_of_scope'),
            (totals([('P1', '', 1)]), 'stock_apply_blank'),
            (totals([('P1', '99999', 1)]), 'stock_identity_missing'),
            (totals([('P1', '80001', 1)]), 'stock_reference_missing'),
            (totals([('P1', '70001', 1), ('P1', ' 70001 ', 2)]), 'normalized_key_duplicate'),
        ]
        for frame, reason in cases:
            with self.subTest(reason=reason), self.assertRaises(h.SourceContractError) as failure:
                h._totals(frame, products, original, source='sales')
            self.assertEqual((failure.exception.reason, failure.exception.source), (reason, 'sales'))

    def test_historical_order_identity_is_valid_without_current_membership(self):
        for company in (12, 13, 40):
            with self.subTest(company=company):
                actual = h._totals(totals([('P1', '80001', 5)]), {'P1'}, master(),
                                   source='orders', company=company)
                self.assertEqual(actual, {('P1', '80001'): D(5)})
        with self.assertLogs('ssai', level='WARNING') as captured:
            with self.assertRaises(h.SourceContractError) as failure:
                h._totals(totals([('P1', '99999', 5)]), {'P1'}, master(),
                          source='orders', company=12)
        self.assertEqual(failure.exception.reason, 'stock_identity_missing')
        self.assertIn('missing_identity_count=1', captured.output[0])
        self.assertIn('unreferenced_group_count=0', captured.output[0])

    def test_sales_missing_customer_and_reverse_reference_still_fail(self):
        frame = totals([('P1', '70001', 5)])
        frame['missing_customer_rows'] = 1
        with self.assertRaises(h.SourceContractError) as failure:
            h._totals(frame, {'P1'}, master(), source='sales')
        self.assertEqual(failure.exception.reason, 'sales_customer_missing')
        frame['stock_apply'] = ''
        with self.assertRaises(h.SourceContractError) as failure:
            h._totals(frame, {'P1'}, master(), source='sales')
        self.assertEqual(failure.exception.reason, 'sales_customer_missing')
        with self.assertRaises(h.SourceContractError) as failure:
            h._totals(totals([('P1', '80001', 5)]), {'P1'}, master(), source='sales')
        self.assertEqual(failure.exception.reason, 'stock_reference_missing')

    def test_historical_orders_remain_separate_and_cannot_receive_new_allocation(self):
        data = sources()
        data['orders'] = pd.concat([data['orders'], totals([
            ('P1', '80001', 200), ('P1', '50001', 50)])], ignore_index=True)
        original_orders = data['orders'].copy(deep=True)
        result = h.allocate(parents(), company=3, **data)
        historical = result.loc[result['배분 재고적용처코드'].eq('80001')].iloc[0]
        self.assertEqual((historical['이번달발주수량'], historical['현재발주수량'],
                          historical['실제 발주수량']), (D(200), D(-200), D(0)))
        self.assertEqual(historical['단가출처'], '과거 기발주 참고')
        self.assertIsNone(historical['발주단가'])
        self.assertFalse(historical['선택외 잔여배정'])
        self.assertEqual(result.loc[result['제품코드'].eq('P1') &
                                    result['배분 재고적용처코드'].eq('50001'),
                                    '이번달발주수량'].iloc[0], D(50))
        self.assertEqual(result.groupby('제품코드')['실제 발주수량'].sum().to_dict(),
                         parents().set_index('제품코드')['실제 발주수량'].to_dict())
        shown = build_display(parents(), result)
        self.assertTrue(((shown['배분 재고적용처'] == 'cost A') &
                         (shown['이번달발주수량'] == D(200))).any())
        excel = BytesIO()
        result.to_excel(excel, index=False)
        excel.seek(0)
        exported = pd.read_excel(excel)
        self.assertEqual(exported['실제 발주수량'].sum(), 2600)
        self.assertEqual(exported.loc[exported['배분 재고적용처코드'].eq(80001),
                                      '이번달발주수량'].iloc[0], 200)
        pd.testing.assert_frame_equal(data['orders'], original_orders)
        with self.assertRaises(h.AllocationError):
            h.allocate(parents(), company=3, **data, selected=('80001',))
        data['orders'] = totals([('P1', '80001', -1)])
        with self.assertRaises(h.SourceContractError) as failure:
            h.allocate(parents(), company=3, **data)
        self.assertEqual(failure.exception.reason, 'historical_order_negative')

    def test_historical_orders_do_not_trigger_contract_lookup(self):
        data = sources()
        data['orders'] = totals([('P1', '80001', 200)])
        params = {'company_id': 3, 'stock_cd_list': ['00001'], 'io_gu_list': ['501']}
        with patch.object(h, 'get_current_company_id', return_value=3), \
             patch.object(h, 'query_to_df', side_effect=[data['sales'], data['orders']]), \
             patch('app.services.rddbc070_service.get_rddbc070_current_df', return_value=pd.DataFrame()) as price:
            loaded = h.load_allocation_sources(params, parents(), date(2026, 11, 16), [], master=master())
            historical_contract_calls = price.call_args_list[:]
        with patch.object(h, 'get_current_company_id', return_value=3), \
             patch.object(h, 'query_to_df', side_effect=[data['sales'], totals()]), \
             patch('app.services.rddbc070_service.get_rddbc070_current_df', return_value=pd.DataFrame()) as price:
            h.load_allocation_sources(params, parents(), date(2026, 11, 16), [], master=master())
        self.assertEqual(loaded['audit']['historical_order_groups'], 1)
        self.assertEqual(loaded['audit']['historical_order_quantity'], '200')
        self.assertEqual(historical_contract_calls, price.call_args_list)

    def test_referenced_order_only_group_does_not_require_new_price(self):
        frame = pd.concat([master(), pd.DataFrame([
            ('00400', 'order-only member', '70003', ''),
            ('70003', 'order-only group', '', ''),
        ], columns=h.MASTER_COLUMNS)], ignore_index=True)
        data = sources()
        data['master'] = frame
        data['orders'] = pd.concat([data['orders'], totals([('P1', '70003', 5)])], ignore_index=True)
        result = h.allocate(parents(), company=3, **data)
        past = result.loc[result['배분 재고적용처코드'].eq('70003')].iloc[0]
        self.assertEqual((past['이번달발주수량'], past['실제 발주수량']), (D(5), D(0)))
        self.assertEqual(result['실제 발주수량'].sum(), D(2600))
        with self.assertRaises(h.MasterError) as failure:
            h.allocate(parents(), company=3, **data, selected=('70003',))
        self.assertEqual(failure.exception.reason, 'first_cost_identity_missing')

    def test_company12_scale_historical_orders_stay_in_original_groups(self):
        frame = master()
        codes = ('H1', 'H2', 'H3', 'H4')
        frame = pd.concat([frame, pd.DataFrame([
            (code, f'historical {code}', '50001', '80001') for code in codes
        ], columns=h.MASTER_COLUMNS)], ignore_index=True)
        original = pd.concat([parents().iloc[[0]]] * 8, ignore_index=True)
        original['제품코드'] = [f'fixture-{i}' for i in range(8)]
        original['실제 발주수량'] = D(100)
        pairs = [(0, 'H1'), (0, 'H2'), (0, 'H3'), (1, 'H1'), (1, 'H2'), (1, 'H4'),
                 (2, 'H1'), (2, 'H3'), (3, 'H1'), (3, 'H4'), (4, 'H1'), (4, 'H2'),
                 (5, 'H1'), (6, 'H3'), (7, 'H1')]
        order_rows = [(f'fixture-{product}', group, 1200 if index == 0 else 100)
                      for index, (product, group) in enumerate(pairs)]
        order_rows += [(f'fixture-{product}', '50001', 442) for product in range(8)]
        result = h.allocate(original, company=3, master=frame, sales=totals(),
                            orders=totals(order_rows), contracts={})
        past = result.loc[result['배분 재고적용처코드'].isin(codes)]
        residual = result.loc[result['배분 재고적용처코드'].eq('50001')]
        self.assertEqual((len(past), past['이번달발주수량'].sum(),
                          past['실제 발주수량'].sum()), (15, D(2600), D(0)))
        self.assertEqual((len(residual), residual['이번달발주수량'].sum(),
                          residual['실제 발주수량'].sum()), (8, D(3536), D(800)))
        self.assertEqual(result.groupby('제품코드')['실제 발주수량'].sum().to_dict(),
                         original.set_index('제품코드')['실제 발주수량'].to_dict())

    def test_source_contract_failure_reports_reason_without_partial_result(self):
        from unittest.mock import Mock
        invalid = sources()
        invalid['sales'] = totals([('P1', '', 10)])
        allocation = Mock(return_value=invalid)
        parent = {'df': parents(), 'meta': {'result_status': 'success',
                  'order_header_metrics': {'effective_business_date': '2026-11-16'},
                  'query_summary': 'fixture'}}
        with patch.object(h, 'get_current_company_id', return_value=3), \
             patch.object(h, 'load_master', return_value=master()), \
             patch.object(h, 'validate_parent_stock_application', side_effect=lambda q: q), \
             patch.object(h.base, 'get_order_calculation_result', return_value=parent) as calculate:
            result = h.get_hospital_order_calculation_result(
                {'company_id': 3, '_display_context': 'panel'}, allocation_loader=allocation)
        calculate.assert_called_once()
        allocation.assert_called_once()
        self.assertEqual(result['meta']['result_status'], 'input_required')
        self.assertEqual(result['meta']['source_contract_reason'], 'stock_apply_blank')
        self.assertEqual(result['meta']['source_contract_source'], 'sales')
        self.assertEqual(result['meta']['allocation_stage'], 'allocation')
        self.assertEqual(result['meta']['row_count_total'], 0)

    def check_preflight(self, frame, *, company=7, context='chat', reason=None, selected=()):
        from unittest.mock import Mock
        original = parents()
        original['회사'] = company
        payload = {'df': original, 'meta': {'result_status': 'success',
            'order_header_metrics': {'effective_business_date': '2026-11-16'}, 'query_summary': 'fixture'}}
        allocation = Mock(return_value={**sources(), 'master': frame})
        with patch.object(h, 'get_current_company_id', return_value=company), \
             patch('app.ui.ssai_login.has_permission', return_value=True), \
             patch.object(h, 'load_master', return_value=frame) as lookup, \
             patch.object(h, 'validate_parent_stock_application', side_effect=lambda q: q), \
             patch('app.services.order_stock_location_scope.prepare_order_stock_scope',
                   side_effect=lambda q, **kw: (q, '', [])), \
             patch.object(h.base, 'get_order_calculation_result', return_value=payload) as parent, \
             patch.object(h, 'allocate', wraps=h.allocate) as allocate:
            result = h.get_hospital_order_calculation_result(
                {'company_id': company, '_display_context': context, h.SELECTION: list(selected)},
                allocation_loader=allocation)
        lookup.assert_called_once_with(company, timeout_seconds=15)
        expected = 0 if reason else 1
        self.assertEqual((parent.call_count, allocation.call_count, allocate.call_count), (expected,)*3)
        if reason:
            self.assertEqual(result['meta']['master_reason'], reason)
            self.assertTrue(result['meta']['service_call_skipped'])
        else:
            self.assertEqual(result['meta']['result_status'], 'success')
            self.assertEqual(result['df']['실제 발주수량'].sum() +
                             sum(result['df'].attrs[h.UNALLOCATED_ATTR].values(), D(0)),
                             original['실제 발주수량'].sum())
        return result

    def test_company4_normal_master_unchanged(self):
        self.check_preflight(master(), company=4)

    def test_synthetic_orphan_text_reference(self):
        frame = master()
        frame.loc[len(frame)] = ['09999', 'synthetic orphan', '재고', '80001']
        for context in ('panel','chat'):
            self.check_preflight(frame, context=context)
        with patch.object(h, 'load_master', return_value=frame):
            options = h.group_options(7)
        self.assertNotIn('재고', options)
        self.assertIn('70001', options)
        self.check_preflight(frame, context='panel', selected=('재고',), reason='stock_identity_missing')

    def test_two_unused_orphans_do_not_block_default_but_source_does(self):
        frame = master()
        frame.loc[len(frame)] = ['09998', 'unused A', '0', '80001']
        frame.loc[len(frame)] = ['09999', 'unused B', '세림', '80001']
        self.check_preflight(frame, company=12)
        ordinary = h.allocate(parents(), company=3, **sources())
        actual = h.allocate(parents(), company=3, master=frame,
                            sales=sources()['sales'], orders=sources()['orders'],
                            contracts=sources()['contracts'])
        pd.testing.assert_frame_equal(actual, ordinary)
        for bad in (totals([('P1', '0', 5)]), totals([('P1', '세림', 0)])):
            with self.subTest(bad=bad.iloc[0]['stock_apply']):
                with self.assertRaises(h.AllocationError):
                    h.allocate(parents(), company=3, master=frame,
                               sales=bad, orders=totals(), contracts={})
                with self.assertRaises(h.AllocationError):
                    h.allocate(parents(), company=3, master=frame,
                               sales=totals(), orders=bad, contracts={})
                with self.assertRaises(h.AllocationError):
                    h.allocate(parents(), company=3, master=frame,
                               sales=bad, orders=totals(), contracts={}, selected=('70001',))
        historical = h.allocate(parents(), company=3, master=frame,
                                sales=totals([('P1', '70001', 1)]), orders=totals([('P1', '80001', 5)]),
                                contracts={}, selected=('70001',))
        self.assertEqual(historical.loc[historical['배분 재고적용처코드'].eq('80001'),
                                        '이번달발주수량'].iloc[0], D(5))

    def test_source_query_reused_and_orphan_checked_before_contract(self):
        frame = master()
        frame.loc[len(frame)] = ['09999', 'unused', '재고', '80001']
        params = {'company_id': 3, 'stock_cd_list': ['00001'], 'io_gu_list': ['501']}
        for sale, order, expected in ((totals(), totals(), True),
                                      (totals([('P1', '재고', 5)]), totals(), False),
                                      (totals(), totals([('P1', '재고', 5)]), False)):
            with self.subTest(sale=len(sale), order=len(order)), \
                 patch.object(h, 'get_current_company_id', return_value=3), \
                 patch.object(h, 'query_to_df', side_effect=[sale, order]) as query, \
                 patch('app.services.rddbc070_service.get_rddbc070_current_df', return_value=pd.DataFrame()) as price:
                if expected:
                    loaded = h.load_allocation_sources(params, parents(), date(2026, 11, 16), [], master=frame)
                    self.assertEqual(loaded['audit']['unused_orphan_codes'], 1)
                else:
                    with self.assertRaises(h.AllocationError):
                        h.load_allocation_sources(params, parents(), date(2026, 11, 16), [], master=frame)
                self.assertEqual(query.call_count, 2)
                if not expected:
                    price.assert_not_called()

    def test_stock_word_in_customer_name_is_not_a_code(self):
        frame = master()
        frame.loc[len(frame)] = ['09999', '재고 관련 거래처', '50001', '80001']
        self.assertNotIn('재고', set(frame.stock_apply))
        self.assertIn('50001', h._master_options(frame))
        self.check_preflight(frame, company=7)

    def test_swapped_code_and_name_columns_are_not_accepted(self):
        frame = master().rename(columns={'code': 'name', 'name': 'code'})
        self.check_preflight(frame, reason='residual_missing')

    def test_master_sql_maps_code_and_name_separately(self):
        with patch.object(h, 'get_current_company_id', return_value=7), \
             patch.object(h, 'query_to_df', return_value=master()) as query:
            loaded = h.load_master(7, timeout_seconds=15)
        sql = query.call_args.args[0]
        self.assertIn('Rd03_Ven_Cd) AS code', sql)
        self.assertIn('Rd03_Ven_Nm) AS name', sql)
        self.assertIn('Rd03_Stock_Apply_Cd) AS stock_apply', sql)
        self.assertIn('Rd03_Cost_Apply_Cd) AS cost_apply', sql)
        self.assertEqual(loaded.loc[loaded.code.eq('50001'), 'name'].iloc[0], '재고적용거래처')

    def test_residual_code_not_inferred_from_name(self):
        frame = master().replace({'50001': '77777'})
        with self.assertRaises(h.MasterError) as failure:
            h.resolve_groups(frame)
        self.assertEqual(failure.exception.reason, 'residual_missing')

    def test_residual_name_is_display_only(self):
        frame = master()
        frame.loc[frame.code.eq('50001'),'name'] = '일반 재고'
        result = self.check_preflight(frame)
        self.assertEqual(result['df'].loc[result['df']['배분 재고적용처코드'].eq('50001'),
                                                '배분 재고적용처'].unique().tolist(), ['일반 재고'])

    def test_company_name_variants_keep_source_name(self):
        for variant, company in [('재고적용거래처#', 8), ('재고적용', 13)]:
            with self.subTest(company=company):
                frame = master()
                frame.loc[frame.code.eq('50001'), 'name'] = variant
                result = self.check_preflight(frame, company=company)
                self.assertEqual(result['df'].loc[result['df']['배분 재고적용처코드'].eq('50001'),
                                                  '배분 재고적용처'].unique().tolist(), [variant])

    def test_residual_duplicate_name_does_not_change_identity(self):
        frame = master()
        frame.loc[len(frame)] = ['99999','재고적용거래처','','']
        self.check_preflight(frame)

    def test_residual_duplicate_code_is_rejected(self):
        frame = pd.concat([master(), master().loc[master().code.eq('50001')]])
        self.check_preflight(frame, reason='master_identity_duplicate')

    def test_duplicate_code(self):
        self.check_preflight(pd.concat([master(),master().iloc[[0]]]), reason='master_identity_duplicate')

    def test_empty_master(self):
        self.check_preflight(master().iloc[:0], reason='master_identity_missing')

    def test_first_invalid_cost_no_next_customer_fallback(self):
        frame = master()
        frame.loc[frame.code.eq('00100'),'cost_apply'] = ''
        self.check_preflight(frame, selected=('70001',), reason='first_cost_identity_missing')
        from unittest.mock import Mock
        parent_rows = parents()
        parent_rows['회사'] = 7
        payload = {'df': parent_rows, 'meta': {'result_status': 'success',
            'order_header_metrics': {'effective_business_date': '2026-11-16'}, 'query_summary': 'fixture'}}
        allocation = Mock(return_value={**sources(), 'master': frame})
        with patch.object(h, 'get_current_company_id', return_value=7), \
             patch.object(h, 'load_master', return_value=frame), \
             patch.object(h, 'validate_parent_stock_application', side_effect=lambda q: q), \
             patch.object(h.base, 'get_order_calculation_result', return_value=payload) as parent:
            result = h.get_hospital_order_calculation_result(
                {'company_id': 7, '_display_context': 'panel'}, allocation_loader=allocation)
        parent.assert_called_once()
        allocation.assert_called_once()
        self.assertEqual(result['meta']['result_status'], 'input_required')
        self.assertIn('첫 거래처', result['message'])

    def test_residual_members_missing(self):
        frame = master()
        frame.loc[frame.stock_apply.eq('50001'),'stock_apply'] = '70001'
        self.check_preflight(frame, reason='residual_members_missing')

    def test_selected_and_parent_independent(self):
        result = self.check_preflight(master(), company=4, selected=('70001',))
        self.assertEqual(set(result['df']['배분 재고적용처코드']), {'70001'})

    def test_query_failure_and_timeout_safe_diagnostics(self):
        for exc, reason in [(RuntimeError('secret connection detail'), 'master_query_failed'),
                            (TimeoutError('secret timeout'), 'master_query_timeout'),
                            (RuntimeError('42000 secret (229)'), 'master_permission_denied'),
                            (RuntimeError('HYT00 secret driver detail'), 'master_query_timeout')]:
            with patch.object(h, 'get_current_company_id', return_value=7), \
                 patch.object(h, 'query_to_df', side_effect=exc) as query:
                with self.assertRaises(h.MasterError) as failure:
                    h.load_master(7, timeout_seconds=15)
                self.assertEqual(failure.exception.reason, reason)
                self.assertTrue(failure.exception.query_failed)
                self.assertNotIn('secret', str(failure.exception))
                query.assert_called_once()

    def test_query_error_never_reported_as_absent(self):
        for reason in ('master_query_timeout', 'master_query_failed'):
            with patch.object(h, 'get_current_company_id', return_value=7), \
                 patch.object(h, 'validate_parent_stock_application', side_effect=lambda q: q), \
                 patch.object(h, 'load_master', side_effect=h.MasterError(reason, '조회 실패', query_failed=True)), \
                 patch.object(h.base, 'get_order_calculation_result') as parent:
                result = h.get_hospital_order_calculation_result({'company_id':7,'_display_context':'panel'})
            self.assertEqual(result['meta']['result_status'], 'query_error')
            self.assertEqual(result['meta']['master_reason'], reason)
            parent.assert_not_called()

    def test_master_cap_does_not_return_partial(self):
        from types import SimpleNamespace
        with patch.object(h, 'get_current_company_id', return_value=7), \
             patch.object(h, 'registered_query_limits', return_value=SimpleNamespace(source_top=1)), \
             patch.object(h, 'query_to_df', return_value=master()) as query:
            with self.assertRaises(h.MasterError) as failure:
                h.load_master(7)
        self.assertEqual(failure.exception.reason, 'master_limit_exceeded')
        self.assertEqual(query.call_count, 2)
        self.assertIn('SELECT TOP 2', query.call_args_list[0].args[0])
        self.assertIn('SELECT TOP 2', query.call_args_list[1].args[0])

    def test_compact_master_preserves_allocation_and_first_customer(self):
        from types import SimpleNamespace
        full = pd.concat([master(), pd.DataFrame([
            (f'{i:05d}', 'fixture extra', '70001', '80001') for i in range(1000, 1200)
        ], columns=h.MASTER_COLUMNS)], ignore_index=True)
        full.loc[full.code.eq('50001'), 'name'] = '재고적용거래처#'
        # The SQL projection includes first members, referenced identities,
        # cost identities and the approved residual code.
        projected = full.loc[full.code.isin(
            ('00100', '00200', '00300', '70001', '70002', '50001', '80001', '80002'))].copy()
        projected['master_rows'] = len(full)
        projected['unique_codes'] = full.code.nunique()
        projected['blank_codes'] = 0
        projected['reference_codes'] = full.stock_apply.loc[full.stock_apply.ne('')].nunique()
        with patch.object(h, 'get_current_company_id', return_value=3), \
             patch.object(h, 'registered_query_limits', return_value=SimpleNamespace(source_top=10)), \
             patch.object(h, 'query_to_df', side_effect=[full.head(11), projected]) as query:
            compact = h.load_master(3)
        self.assertEqual(query.call_count, 2)
        sql = query.call_args_list[1].args[0]
        self.assertIn('ROW_NUMBER() OVER (PARTITION BY stock_apply ORDER BY code)', sql)
        self.assertIn('COUNT_BIG(DISTINCT code)', sql)
        self.assertIn("WHERE code = '50001'", sql)
        self.assertNotIn("name = N'재고적용거래처'", sql)
        self.assertEqual(h.resolve_groups(compact), h.resolve_groups(full))
        actual = h.allocate(parents(), company=3, master=compact,
                            sales=sources()['sales'], orders=sources()['orders'],
                            contracts=sources()['contracts'])
        expected = h.allocate(parents(), company=3, master=full,
                              sales=sources()['sales'], orders=sources()['orders'],
                              contracts=sources()['contracts'])
        pd.testing.assert_frame_equal(actual, expected)

    def test_compact_master_metadata_fails_closed(self):
        from types import SimpleNamespace
        compact = master().copy()
        compact['master_rows'] = 100
        compact['unique_codes'] = 100
        compact['blank_codes'] = 0
        compact['reference_codes'] = 3
        capped = pd.concat([master(), pd.DataFrame([
            ('01000', 'fixture', '70001', '80001'),
            ('01001', 'fixture', '70001', '80001')
        ], columns=h.MASTER_COLUMNS)], ignore_index=True)
        cases = [('duplicate', {'unique_codes': 99}, 'master_identity_duplicate'),
                 ('blank', {'blank_codes': 1}, 'master_identity_duplicate'),
                 ('missing_group', {'reference_codes': 4}, 'master_compact_invalid'),
                 ('missing_metric', {'master_rows': None}, 'master_compact_invalid')]
        for label, changes, reason in cases:
            with self.subTest(label=label):
                partial = compact.assign(**changes)
                with patch.object(h, 'get_current_company_id', return_value=3), \
                     patch.object(h, 'registered_query_limits', return_value=SimpleNamespace(source_top=10)), \
                     patch.object(h, 'query_to_df', side_effect=[capped, partial]):
                    with self.assertRaises(h.MasterError) as failure:
                        h.load_master(3)
                self.assertEqual(failure.exception.reason, reason)

    def test_compact_master_query_failure_is_not_empty_master(self):
        from types import SimpleNamespace
        with patch.object(h, 'get_current_company_id', return_value=3), \
             patch.object(h, 'registered_query_limits', return_value=SimpleNamespace(source_top=1)), \
             patch.object(h, 'query_to_df', side_effect=[master().head(2), RuntimeError('secret')]) as query:
            with self.assertRaises(h.MasterError) as failure:
                h.load_master(3)
        self.assertEqual(query.call_count, 2)
        self.assertEqual(failure.exception.reason, 'master_query_failed')
        self.assertTrue(failure.exception.query_failed)
        self.assertNotIn('secret', str(failure.exception))

    def test_parent_stock_transport_error_never_runs_allocation(self):
        from unittest.mock import Mock
        allocation = Mock()
        with patch.object(h, 'get_current_company_id', return_value=12), \
             patch.object(h, 'validate_parent_stock_application', side_effect=lambda q: q), \
             patch.object(h, 'load_master', return_value=master()), \
             patch.object(h.base, 'get_order_calculation_result',
                          side_effect=RuntimeError('08S01 SQLGetData')) as parent:
            with self.assertRaisesRegex(RuntimeError, '08S01'):
                h.get_hospital_order_calculation_result(
                    {'company_id': 12, '_display_context': 'panel'}, allocation_loader=allocation)
        parent.assert_called_once()
        allocation.assert_not_called()

    def test_company_switch_during_master_read(self):
        with patch.object(h, 'get_current_company_id', side_effect=[7,4]), \
             patch.object(h, 'query_to_df', return_value=master()):
            with self.assertRaises(h.AllocationError):
                h.load_master(7)

    def test_panel_lists_valid_groups_without_unused_orphan(self):
        from streamlit.testing.v1 import AppTest
        frame = master()
        frame.loc[len(frame)] = ['09999','fixture','재고','80001']
        script = ('from app.sims.views.order_calculation_view import view_hospital_order_calculation\n'
                  'view_hospital_order_calculation()')
        with patch('app.db.mssql_client.get_current_company_id', return_value=7), \
             patch.object(h, 'load_master', return_value=frame), \
             patch('app.sims.views.product_master_filters.read_df', return_value=pd.DataFrame()), \
             patch.object(h.base, 'get_order_calculation_result') as parent:
            app = AppTest.from_string(script).run()
        self.assertFalse(app.exception)
        self.assertFalse(app.error)
        self.assertNotIn('재고', app.multiselect[0].options)
        parent.assert_not_called()

    def test_default_allocation_reuses_preflight_master(self):
        # Exercise the real loader (empty sources) rather than injecting an allocation result.
        original = parents()
        original['회사'] = 7
        frame = master()
        frame.loc[len(frame)] = ['09999', 'unused', '재고', '80001']
        payload = {'df': original, 'meta': {'result_status': 'success',
            'order_header_metrics': {'effective_business_date': '2026-11-16'},'query_summary':'fixture'}}
        with patch.object(h, 'get_current_company_id', return_value=7), \
             patch.object(h, 'load_master', return_value=frame) as lookup, \
             patch.object(h, 'validate_parent_stock_application', side_effect=lambda q: q), \
             patch.object(h.base, 'get_order_calculation_result', return_value=payload) as parent, \
             patch.object(h, 'query_to_df', side_effect=[totals(),totals()]) as aggregate, \
             patch('app.services.rddbc070_service.get_rddbc070_current_df',return_value=pd.DataFrame()):
            # Mock parent does not invoke capture, so supply the scope at its normal capture boundary.
            def parent_result(q, *, source_loader):
                source_loader(q)
                return payload
            parent.side_effect = parent_result
            result = h.get_hospital_order_calculation_result(
                {'company_id':7,'_display_context':'panel','stock_cd_list':['00001'],'io_gu_list':['501']},
                source_loader=lambda q: {'source_params':q})
        lookup.assert_called_once_with(7, timeout_seconds=15)
        parent.assert_called_once()
        self.assertEqual(aggregate.call_count, 2)
        self.assertEqual(result['meta']['allocation_audit']['unused_orphan_codes'], 1)
        self.assertEqual(result['df']['실제 발주수량'].sum(), original['실제 발주수량'].sum())


if __name__ == '__main__':
    # Block real SQL even when a newly introduced path forgets to use a fixture.
    with patch('app.db.mssql_client.read_df', side_effect=AssertionError('ERP access forbidden in offline gate')):
        unittest.main(verbosity=2)
