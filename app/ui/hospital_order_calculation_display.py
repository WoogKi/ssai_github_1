"""Reference rows are presentation only; exports/current-table contain allocations."""

import logging
import time
from decimal import Decimal

import pandas as pd

from app.services import hospital_order_calculation_service as service
from app.services.order_calculation_service import ROW_KEY
from app.services.order_calculation_contract import amounts
from app.ui.order_calculation_editor import _PAGE_SIZE, _order_table_display_config, _select_order_page, _style_order_table


def build_display(parents: pd.DataFrame, allocations: pd.DataFrame, *, max_rows=300,
                  with_mapping=False, include_review=False, include_unallocated=False):
    # Group operations propagate attrs. The full parent reference must not be
    # deep-copied once per product; this local, read-only view contains values only.
    allocations = pd.DataFrame(allocations, copy=False)
    extra = ["배분 재고적용처", "지난달매출수량", "이번달발주수량", "배분 단가", "배분 단가출처"]
    columns = list(parents.rename(columns={"재고적용처": "원계산 재고적용처"}).columns)
    position = columns.index("실제 발주수량") + 1
    columns[position:position] = extra
    if len(columns) != len(set(columns)):
        raise service.AllocationError("병원 표의 표시 컬럼 키가 중복되었습니다.")
    children = {key: part for key, part in allocations.groupby(list(ROW_KEY), sort=False, dropna=False)}
    rows = []
    row_keys = []
    for parent in parents.to_dict('records'):
        key = tuple(parent[k] for k in ROW_KEY)
        if key not in children:
            if include_unallocated or (include_review and parent.get('계산상태') == '수요/재고 사용자확인'):
                rows.append({**parent, '원계산 재고적용처': parent.get('재고적용처')})
                row_keys.append(None)
            continue
        row = {**parent, '원계산 재고적용처': parent.get('재고적용처')}
        rows.append(row)
        row_keys.append(None)
        if len(rows) >= max_rows:
            break
        for _, child in children[key].iterrows():
            rows.append({'실제 발주수량': child['실제 발주수량'],
                         '배분 재고적용처': child['배분 재고적용처'],
                         '지난달매출수량': child['지난달매출수량'],
                         '이번달발주수량': child['이번달발주수량'],
                         '배분 단가': child['발주단가'], '배분 단가출처': child['단가출처']})
            row_keys.append(tuple(child[column] for column in service.ALLOCATION_KEY))
            if len(rows) >= max_rows:
                break
        if len(rows) >= max_rows:
            break
    shown = pd.DataFrame(rows).reindex(columns=columns)
    return (shown, row_keys) if with_mapping else shown


def apply_allocation_edits(full: pd.DataFrame, row_keys: list, changes: dict, *, selected: bool) -> pd.DataFrame:
    if not changes:
        return full
    work = pd.DataFrame(full, copy=False)
    if work.duplicated(list(service.ALLOCATION_KEY)).any():
        raise ValueError('병원 배분행 식별키가 중복되었습니다.')
    parents = {service._text(row['제품코드']): service._quantity(row['실제 발주수량'], nonnegative=True)
               for row in full.attrs.get(service.REFERENCE_ATTR, [])
               if row['실제 발주수량'] is not None}
    if not parents:
        raise ValueError('부모 발주수량 원본을 확인할 수 없습니다.')
    pending = []
    for position, change in changes.items():
        if set(change) != {'실제 발주수량'} or not 0 <= int(position) < len(row_keys):
            raise ValueError('실제 배분수량만 수정할 수 있습니다.')
        key = row_keys[int(position)]
        if key is None:
            raise ValueError('제품 부모 참고행은 수정할 수 없습니다.')
        mask = pd.Series(True, index=work.index)
        for column, value in zip(service.ALLOCATION_KEY, key):
            mask &= work[column].eq(value)
        if mask.sum() != 1:
            raise ValueError('병원 배분행을 원본에서 유일하게 찾지 못했습니다.')
        row = work.loc[mask].iloc[0]
        product = service._text(row['제품코드'])
        code = service._text(row['배분 재고적용처코드'])
        if row['단가출처'] == '과거 기발주 참고' or row['실제 발주수량'] == 0 and row['현재발주수량'] < 0:
            raise ValueError('기발주 전용 참고행은 수정할 수 없습니다.')
        if not selected and code == service.RESIDUAL_STOCK_APPLY_CODE:
            raise ValueError('기본 잔여처 수량은 다른 병원 수량의 변경에 따라 자동 조정됩니다.')
        value = service._quantity(change['실제 발주수량'], nonnegative=True)
        if selected and value > max(Decimal(0), service._quantity(row['현재발주수량'])):
            raise ValueError('선택 그룹의 추가 발주 필요수량을 초과할 수 없습니다.')
        if value == service._quantity(row['실제 발주수량'], nonnegative=True):
            continue
        pending.append((mask, value, row['발주단가'], product))
    if not pending:
        return full
    result = pd.DataFrame(work, copy=True)
    original_attrs = full.attrs.copy()
    original_attrs[service.UNALLOCATED_ATTR] = dict(full.attrs.get(service.UNALLOCATED_ATTR, {}))
    original_attrs[service.REFERENCE_ATTR] = [dict(row) for row in full.attrs[service.REFERENCE_ATTR]]
    touched = set()
    for mask, value, price, product in pending:
        result.loc[mask, '실제 발주수량'] = value
        for column, amount in amounts(value, service.base.decimal_or_none(price)).items():
            result.loc[mask, column] = amount
        touched.add(product)
    for product in touched:
        mask = result['제품코드'].map(service._text).eq(product)
        allocated = sum((service._quantity(value, nonnegative=True)
                         for value in result.loc[mask, '실제 발주수량']), Decimal(0))
        if selected:
            if allocated > parents[product]:
                raise ValueError('선택 그룹 배분 합계가 부모 발주수량을 초과합니다.')
            original_attrs.setdefault(service.UNALLOCATED_ATTR, {})[product] = parents[product] - allocated
            for row in original_attrs[service.REFERENCE_ATTR]:
                if service._text(row['제품코드']) == product:
                    row['미배분수량'] = parents[product] - allocated
        else:
            residual_mask = mask & result['배분 재고적용처코드'].eq(service.RESIDUAL_STOCK_APPLY_CODE)
            old_residual = (service._quantity(result.loc[residual_mask, '실제 발주수량'].iloc[0], nonnegative=True)
                            if residual_mask.any() else Decimal(0))
            new_residual = old_residual + parents[product] - allocated
            if new_residual < 0:
                raise ValueError('기본 잔여처 수량이 부족하여 다른 병원 수량을 임의 변경하지 않았습니다.')
            if not residual_mask.any() and new_residual > 0:
                template = original_attrs.get(service.RESIDUAL_TEMPLATES_ATTR, {}).get(product)
                if template is None:
                    raise ValueError('기본 잔여처의 원계산 단가를 확인할 수 없습니다.')
                result = pd.concat([result, pd.DataFrame([{**template, '실제 발주수량': new_residual}])], ignore_index=True)
                residual_mask = result['제품코드'].map(service._text).eq(product) & result['배분 재고적용처코드'].eq(service.RESIDUAL_STOCK_APPLY_CODE)
            if residual_mask.any():
                result.loc[residual_mask, '실제 발주수량'] = new_residual
                result.loc[residual_mask, '잔여배정수량'] = new_residual
                price = service.base.decimal_or_none(result.loc[residual_mask, '발주단가'].iloc[0])
                for column, amount in amounts(new_residual, price).items():
                    result.loc[residual_mask, column] = amount
    if result.equals(work):
        return full
    result.attrs = original_attrs
    return result


def render_hospital_order_table(item: dict, meta: dict, *, download_uid: str = '') -> bool:
    import streamlit as st
    from app.db.mssql_client import get_current_company_id
    from app.services.snapshot_product_information_service import project_product_information_frame_for_viewer

    started = time.perf_counter()
    log = logging.getLogger('ssai')
    log.info('[hospital.table] start')
    key = str(meta.get('table_key') or item.get('table_key') or '')
    ss = st.session_state
    full = ss.get('sims_export_tables', {}).get(key)
    parents = pd.DataFrame(full.attrs.get(service.REFERENCE_ATTR) or []) if isinstance(full, pd.DataFrame) else pd.DataFrame()
    full = pd.DataFrame(full, copy=False) if isinstance(full, pd.DataFrame) else full
    company = get_current_company_id()
    if (company is None or not isinstance(full, pd.DataFrame) or parents.empty or
            '회사' not in full or '회사' not in parents or
            not full['회사'].eq(company).all() or not parents['회사'].eq(company).all()):
        st.warning('현재 회사의 병원발주계산 표시 원본이 없습니다. 다시 조회하세요.')
        return True
    parents = project_product_information_frame_for_viewer(parents)
    room = str(meta.get('room_id') or item.get('room_id') or '')
    message = str(item.get('id') or meta.get('message_id') or '')
    page_key = f'hospital_page::{company}::{room}::{key}::{message}'
    page, page_count = _select_order_page(len(full), page_key)
    visible = full.iloc[(page - 1) * _PAGE_SIZE:page * _PAGE_SIZE]
    source, row_keys = build_display(parents, visible, max_rows=len(parents) + len(visible), with_mapping=True)
    is_current = ss.get('__sims_current_table_source_key') == key
    view, config, height = _order_table_display_config(source, editing=is_current)
    config['배분 재고적용처']['label'] = '재고적용처'
    config['원계산 재고적용처']['label'] = '재고적용처(원계산)'
    prepared = time.perf_counter()
    log.info('[hospital.table] prepared allocation_rows=%s page=%s page_count=%s page_allocations=%s display_rows=%s seconds=%.3f',
             len(full), page, page_count, len(visible), len(view), prepared - started)
    if is_current:
        view['실제 발주수량'] = pd.array([
            None if value is None or pd.isna(value) else int(value)
            for value in source['실제 발주수량']], dtype='Int64')
        actual_config = config.get('실제 발주수량') or {}
        config['실제 발주수량'] = st.column_config.NumberColumn(
            '발주수량', min_value=0, step=1, format='%,.0f',
            width=actual_config.get('width'), alignment='right',
            help='병원별 수량을 수정하면 부모 발주수량과 필요수량을 검증합니다.')
        widget_key = (f'hospital_actual::{company}::{room}::{key}::{message}::{page}::'
                      f"{meta.get('editor_widget_revision', 0)}")

        def commit():
            try:
                if ss.get('__sims_current_table_source_key') != key or get_current_company_id() != company:
                    raise ValueError('회사 또는 현재표가 변경되어 수정을 적용하지 않았습니다.')
                current = ss.get('sims_export_tables', {}).get(key)
                if not isinstance(current, pd.DataFrame) or not current['회사'].eq(company).all():
                    raise ValueError('현재 회사의 병원 배분 원본을 확인할 수 없습니다.')
                updated = apply_allocation_edits(
                    current, row_keys, ss.get(widget_key, {}).get('edited_rows', {}),
                    selected=bool(meta.get('allocation_selected')))
            except (ValueError, service.AllocationError) as exc:
                ss[f'hospital_edit_error::{key}'] = str(exc)
                meta['editor_widget_revision'] = int(meta.get('editor_widget_revision', 0)) + 1
                item['meta'] = meta
                return
            ss.pop(f'hospital_edit_error::{key}', None)
            if updated is current:
                return
            plain = pd.DataFrame(updated, copy=False)
            display = plain.head(_PAGE_SIZE).copy()
            for store in ('sims_export_tables', '__sims_export_tables_by_key'):
                ss.setdefault(store, {})[key] = updated
            ss.setdefault('sims_tables', {})[key] = display
            item.update(df=updated, df_display=display, records=display.to_dict('records'))
            total = sum(plain['실제 발주수량'], Decimal(0))
            unallocated = sum(updated.attrs.get(service.UNALLOCATED_ATTR, {}).values(), Decimal(0))
            meta['allocation_quantity'] = str(total)
            meta['unallocated_quantity'] = str(unallocated)
            meta['row_count_total'] = len(updated)
            meta['download_row_count'] = len(updated)
            meta['quantity_edit_revision'] = int(meta.get('quantity_edit_revision', 0)) + 1
            if len(updated) != len(current):
                meta['editor_widget_revision'] = int(meta.get('editor_widget_revision', 0)) + 1
            scope = (meta.get('order_display_conditions') or {}).get('stock_application', '전체')
            product_label = '전월 매출 제품' if meta.get('allocation_selected') else '배분 대상 제품'
            meta['summary_md'] = (f'병원발주계산: 재고적용처 {scope} / '
                                  f'{product_label} {meta.get("parent_product_count", 0):,}개 / '
                                  f'실제 배분 {len(updated):,}행 / 전체 배분수량 {total:,} / '
                                  f'미배분수량 {unallocated:,}. ERP에 등록되지 않았습니다.')
            meta['llm_summary_md'] = meta['summary_md']
            item.update(meta=meta, data=meta['summary_md'], message=meta['summary_md'])
            from app.ui.chat_middleware import _build_sims_context_from_result
            _build_sims_context_from_result({**item, 'df': plain}, service.ACTION,
                                            dict(item.get('params') or {}), plain)
            for cache_key in list(ss.get('__sims_table_render_cache', {})):
                if str(cache_key).startswith(key + ':'):
                    ss['__sims_table_render_cache'].pop(cache_key, None)
            for cache_key in (f'__sims_download_ready::{download_uid}',
                              f'__sims_download_bytes::{download_uid}'):
                ss.pop(cache_key, None)
            log.info('[hospital.edit] company_id=%s table_key=%s revision=%s',
                     company, key, meta['quantity_edit_revision'])

        st.data_editor(_style_order_table(view, editing=True), key=widget_key,
                       on_change=commit, hide_index=True, placeholder=' ',
                       disabled=[column for column in view if column != '실제 발주수량'],
                       column_config=config, num_rows='fixed', height=height, width='stretch')
        error = ss.get(f'hospital_edit_error::{key}')
        if error:
            st.error(error)
    else:
        st.dataframe(_style_order_table(view, editing=False), hide_index=True, width='stretch',
                     height=height, column_config=config)
    log.info('[hospital.table] submitted seconds=%.3f total_seconds=%.3f',
             time.perf_counter() - prepared, time.perf_counter() - started)
    st.caption(f"{page} / {page_count}페이지 · 실제 배분 합계 {meta.get('allocation_quantity', '')} / "
               f"미배분 {meta.get('unallocated_quantity', '0')} / "
               f"전체 배분 {len(full):,}행 / 현재 배분 {len(visible):,}행 / "
               f"참고 포함 화면 {len(view):,}행. Excel 실제배분·CSV·현재표는 전체 배분행 기준.")
    review = parents.loc[parents['계산상태'].eq('수요/재고 사용자확인')] if '계산상태' in parents else parents.iloc[:0]
    if not review.empty:
        with st.expander(f"사용자 확인 제품 {len(review):,}개 · 신규 배분 없음"):
            columns = [column for column in ('제품코드', '제품명', '계산상태', '조달주의',
                                             '계산 발주수량', '추천 발주수량', '실제 발주수량') if column in review]
            st.dataframe(review.loc[:, columns], hide_index=True, width='stretch')
    no_order = parents.loc[parents['실제 발주수량'].eq(0) &
                           ~parents['계산상태'].eq('수요/재고 사용자확인')] if '계산상태' in parents else parents.iloc[:0]
    if not no_order.empty:
        with st.expander(f"발주 불필요 제품 {len(no_order):,}개 · 신규 배분 없음"):
            columns = [column for column in ('제품코드', '제품명', '계산상태',
                                             '계산 발주수량', '추천 발주수량', '실제 발주수량') if column in no_order]
            st.dataframe(no_order.loc[:, columns].head(_PAGE_SIZE), hide_index=True, width='stretch')
            if len(no_order) > _PAGE_SIZE:
                st.caption('전체 참고자료는 Excel 참고표_합계제외 시트에서 확인할 수 있습니다.')
    return True


def write_hospital_excel_if_any(writer, frame, *, sanitize, format_sheet) -> bool:
    if not isinstance(frame, pd.DataFrame) or service.REFERENCE_ATTR not in frame.attrs:
        return False
    from app.services.snapshot_product_information_service import project_product_information_frame_for_viewer

    company = service._company(service.get_current_company_id())
    parents = pd.DataFrame(frame.attrs[service.REFERENCE_ATTR])
    frame = pd.DataFrame(frame, copy=False)
    if (parents.empty or not parents['회사'].eq(company).all() or
            not frame['회사'].eq(company).all() or
            frame['배분정책'].nunique() != 1 or
            not frame['배분정책'].isin((service.POLICY, service.SCOPED_POLICY)).all()):
        raise service.AllocationError('병원발주계산 Excel 회사/정책을 확인해야 합니다.')
    parents = project_product_information_frame_for_viewer(parents)
    reference = build_display(parents, frame, max_rows=len(parents) + len(frame),
                              include_review=True, include_unallocated=True)
    parent_rows = reference['제품코드'].notna()
    # Reference totals must not double SUM(actual quantity) in the sample layout.
    reference.loc[parent_rows, '실제 발주수량'] = reference.loc[parent_rows, '실제 발주수량'].map(
        lambda value: '참고: 사용자 확인' if value is None or pd.isna(value) else f'참고: {value:,}')
    for name, source in (('실제배분', frame), ('참고표_합계제외', reference)):
        exported = sanitize(source)
        exported.to_excel(writer, index=False, sheet_name=name)
        format_sheet(writer, exported, name)
    return True
