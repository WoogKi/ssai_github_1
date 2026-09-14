"""Sequential company-7 NLQ release smoke; SELECT only, no retry."""
import argparse
import json
import logging
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db.mssql_client import set_current_company_id, read_only_request
from app.services.order_calculation_service import get_order_calculation_result
from app.sims.nlq.nlq_router import _try_handle_io_nlq

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', required=True)
    out = Path(parser.parse_args().output_dir)
    set_current_company_id(7)
    reports = []
    cases = (
        ('환인 조회구분 해당 발주 계산', '환인', (3, 15, 25)),
        ('한림 안전재고 5일 적정재고 20일 마감일자 25일 조회구분 해당 발주 계산', '한림', (5, 20, 25)),
        ('환인 발주 보여줘', '환인', None),
        ('제품코드 64063 발주 수량 계산', None, (3, 15, 25)),
    )
    for text, maker, conditions in cases:
        sent = []
        def guarded_service(q):
            if maker:
                assert q.get('maker_nm') == maker, q
                assert q.get('only_needed') is True, q
            else:
                assert q.get('physic_cd') == '64063', q
            assert tuple(q[k] for k in ('safety_days', 'target_days', 'closing_day')) == conditions
            assert q.get('cost_apply_cd') == '50002' and q.get('stock_apply_cd') == '50001'
            return get_order_calculation_result(q)
        started = time.perf_counter()
        try:
            with read_only_request(timeout_seconds=120) as measurement, \
                 patch('app.services.order_calculation_service.get_order_calculation_result', side_effect=guarded_service), \
                 patch('app.ui.chat_middleware.push_sims_result_to_chat', side_effect=lambda p, a: sent.append(p) or p.get('meta', {})):
                handled = _try_handle_io_nlq(text, room={}, session_state={}, make_ts=lambda: 'release-smoke',
                    next_seq=lambda: 1, logger=logging.getLogger('ssai'))
            assert handled and len(sent) == 1
            p = sent[0]
            q, meta, frame = p.get('params', {}), p.get('meta', {}), p.get('df')
            assert p['action'] == ('발주 계산' if conditions else '발주조회')
            assert meta.get('entity_resolution_status') not in ('not_found', 'candidate_required', 'resolution_unavailable')
            assert frame is not None and len(frame) > 0
            if maker:
                assert q.get('maker_nm') == maker
            representatives = []
            if conditions:
                assert frame['추천 발주수량'].equals(frame['실제 발주수량'])
                for code, expected in (('64063', 10), ('23976', 100), ('83315', 7)):
                    matched = frame.loc[frame['제품코드'].eq(code)]
                    if not matched.empty:
                        row = matched.iloc[0]
                        assert row['추천 발주수량'] == row['실제 발주수량'] == expected
                        representatives.append({k: row[k] for k in ('제품코드', '계산 발주수량',
                            '추천 발주수량', '실제 발주수량', '발주단가', '발주금액(부가세포함)')})
            report = {'text': text, 'status': 'PASS', 'action': p['action'], 'params': q,
                'rows': len(frame), 'elapsed_seconds': time.perf_counter() - started,
                'service_elapsed_ms': meta.get('elapsed_ms'), 'source_call_count': meta.get('source_call_count'),
                'outer_request_query_count': len(measurement['queries']),
                'snapshot_status': meta.get('snapshot_status'), 'representatives': representatives}
        except Exception as exc:
            report = {'text': text, 'status': 'FAIL', 'error_type': type(exc).__name__,
                'error': str(exc), 'elapsed_seconds': time.perf_counter() - started}
            reports.append(report)
            (out / 'order_release_smoke.json').write_text(json.dumps(reports, ensure_ascii=False, default=str, indent=2), encoding='utf-8')
            raise
        reports.append(report)
        (out / 'order_release_smoke.json').write_text(json.dumps(reports, ensure_ascii=False, default=str, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, default=str), flush=True)

if __name__ == '__main__':
    main()
