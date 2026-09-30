"""Offline routing and production-dispatch parity checks; no ERP access."""
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import logging
import sys
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.erp_table_nlq import resolve_registered_erp_table_nlq, is_order_calculation_request
from app.services.io_nlq import resolve_io_nlq
from app.sims.nlq.nlq_router import _try_handle_io_nlq, resolve_new_sims_nlq_candidate
from app.services.order_calculation_service import (
    build_manual_validation_evidence,
    filter_base_by_order_scope,
    get_order_calculation_result,
    order_calculation_performance_summary,
    order_scope_row_matches,
    write_manual_validation_evidence,
)
from tools.check_order_calculation_contract import fixture


def staff_fixture():
    params, sources = fixture()
    product_codes = ["00001", "00002", "00003", "00004"]
    base = pd.concat([sources["base"].iloc[[0]]] * len(product_codes), ignore_index=True)
    base["제품코드"] = product_codes
    demand = pd.concat([sources["demand"].iloc[[0]]] * len(product_codes), ignore_index=True)
    demand["제품코드"] = product_codes
    suppliers = pd.concat([sources["suppliers"].iloc[[0]]] * len(product_codes), ignore_index=True)
    suppliers["product_code"] = product_codes
    suppliers["recent_inbound_vendor_code"] = ["V001", "V002", "V003", "V004"]
    suppliers["recent_inbound_vendor_name"] = ["신민우 발주처", "윤정아 발주처", "담당자없는 발주처", "사용자미매칭 발주처"]
    suppliers["recent_inbound_vendor_staff_code"] = ["U001", "U002", "", "U004"]
    suppliers["recent_inbound_vendor_staff_name"] = ["신민우", "윤정아", "", ""]
    suppliers["manufacturer_vendor_code"] = ["M001", "M002", "M003", "M004"]
    suppliers["manufacturer_vendor_name"] = ["제약사1", "제약사2", "제약사3", "제약사4"]
    suppliers["manufacturer_staff_code"] = ["P001", "P002", "P003", "P004"]
    suppliers["manufacturer_staff_name"] = ["이기재", "김제약", "박제약", "이정민"]
    sources.update({"base": base, "demand": demand, "suppliers": suppliers})
    return params, sources


def _legacy_order_scope_matches(params, supplier):
    """The pre-early-scope assemble predicate, kept only as a regression oracle."""
    order_code = str(supplier.get("recent_inbound_vendor_staff_code") or "").strip()
    order_name = str(supplier.get("recent_inbound_vendor_staff_name") or "").strip()
    pharma_code = str(supplier.get("manufacturer_staff_code") or "").strip()
    pharma_name = str(supplier.get("manufacturer_staff_name") or "").strip()
    vendor_code = str(supplier.get("recent_inbound_vendor_code") or "").strip()
    vendor_name = str(supplier.get("recent_inbound_vendor_name") or "").strip()
    order_filter = str(params.get("order_staff_nm") or "").strip()
    pharma_filter = str(params.get("pharma_staff_nm") or "").strip()
    return not (
        (order_filter and order_filter != order_code and order_filter not in order_name)
        or (pharma_filter and pharma_filter != pharma_code and pharma_filter not in pharma_name)
        or (params.get("order_vendor_cd") and params["order_vendor_cd"] != vendor_code)
        or (params.get("order_vendor_nm") and params["order_vendor_nm"] not in vendor_name)
    )


def _assert_early_scope_exact_set():
    _, sources = staff_fixture()
    base = sources["base"].copy()
    base["재고적용처코드"] = "50001"
    base["단가적용처코드"] = "50002"
    suppliers = sources["suppliers"]
    supplier_index = {
        str(row["product_code"]).strip(): row
        for row in suppliers.to_dict("records")
    }
    cases = (
        {"order_staff_nm": "신"},
        {"order_staff_nm": "U002"},
        {"pharma_staff_nm": "김"},
        {"pharma_staff_nm": "P001"},
        {"order_staff_nm": "없는이름"},
        {"order_staff_nm": "신", "pharma_staff_nm": "이"},
        {"order_vendor_cd": "V001"},
        {"order_vendor_nm": "윤정아"},
        {},
    )
    for params in cases:
        expected = [
            str(row["제품코드"]).strip()
            for row in base.to_dict("records")
            if _legacy_order_scope_matches(params, supplier_index[str(row["제품코드"]).strip()])
        ]
        actual = filter_base_by_order_scope(base, suppliers, params)
        assert actual["제품코드"].astype(str).tolist() == expected, params
        assert [
            order_scope_row_matches(params, supplier_index[code]) for code in expected
        ] == [True] * len(expected), params
        if expected:
            assert actual["재고적용처코드"].eq("50001").all()
            assert actual["단가적용처코드"].eq("50002").all()
    print("PASS early-scope exact product-set and application-authority preservation; ERP calls 0")


def _assert_snapshot_error_precedes_early_scope():
    sources = {
        "snapshot": {
            "meta": {"snapshot_status": "query_error", "snapshot_reason": "fixture_snapshot_failure"},
            "message": "fixture snapshot failure",
        }
    }
    with TemporaryDirectory() as artifact_dir, \
         patch('app.services.order_calculation_service.get_current_company_id', return_value=7):
        result = get_order_calculation_result(
            {"company_id": 7, "order_staff_nm": "김", "_original_question": "제약담당자 김 발주계산",
             "_order_calculation_diagnostic_dir": artifact_dir},
            source_loader=lambda _: sources,
        )
        artifact_path = Path(result["meta"]["manual_validation_artifact_path"])
        assert artifact_path.is_file()
    evidence = result["meta"]["manual_validation_evidence"]
    assert result["meta"]["snapshot_status"] == "query_error"
    assert result["meta"]["snapshot_reason"] == "fixture_snapshot_failure"
    assert evidence["error"] == {
        "error_stage": "snapshot_validation", "exception_class": "fixture_snapshot_failure",
        "exception_message": "fixture snapshot failure",
    }
    assert evidence["source_call_count"] == 0
    print("PASS snapshot query_error evidence exits before early-scope source handling; ERP calls 0")


def _assert_manual_validation_evidence():
    frame = pd.DataFrame([
        {"제품코드": "00002", "발주담당자": "김", "제약담당자": "이", "발주처코드": "V002",
         "단가적용처": "단가처", "재고적용처": "재고처", "적용 필요예정수량": 4.0, "재고수량": 1,
         "입고예정수량": 2, "추천 발주수량": 3, "발주단가": 12.50, "발주금액(부가세포함)": 37.5},
        {"제품코드": "00001", "발주담당자": "김", "제약담당자": "박", "발주처코드": "V001",
         "단가적용처": "단가처", "재고적용처": "재고처", "적용 필요예정수량": 5, "재고수량": 2,
         "입고예정수량": 0, "추천 발주수량": 3, "발주단가": 10, "발주금액(부가세포함)": 30},
    ])
    common = {
        "params": {"company_id": 7, "order_date": "2026-09-23", "order_staff_nm": "김",
                   "_original_question": "발주담당자 김 발주계산"},
        "sources": {"base": frame, "diagnostic_candidate_product_codes": ["00003", "00001", "00002"],
                    "diagnostic_scoped_product_codes": ["00002", "00001"],
                    "diagnostic_stage_evidence": {"early_scope": {"elapsed_ms": 1, "rows": 2, "source_call_count": 0},
                                                  "forecast_stock": {"elapsed_ms": 2, "rows": 2, "source_call_count": 1}}},
        "measurement": {"queries": [{"sql": "fixture"}]}, "elapsed_ms": 12.3, "result_status": "success",
    }
    evidence = build_manual_validation_evidence(result_frame=frame, **common)
    reordered = build_manual_validation_evidence(result_frame=frame.iloc[::-1].reset_index(drop=True), **common)
    assert evidence["canonical_result"]["sha256"] == reordered["canonical_result"]["sha256"]
    assert evidence["product_sets"]["candidate_product_codes"]["codes"] == ["00001", "00002", "00003"]
    assert evidence["product_sets"]["scoped_product_codes"]["codes"] == ["00001", "00002"]
    assert evidence["product_sets"]["final_result_product_codes"]["codes"] == ["00001", "00002"]
    summary = order_calculation_performance_summary({
        "snapshot_master": 10, "representative_vendor": 20, "forecast_stock": 30,
        "current_customer_source": 40, "current_customer_mapping": 2,
        "order_history_3m_source": 35, "order_history_1y_source": 15, "pending_four_business_days": 60,
        "prices_code_names": 70, "contract_prices": 80, "post_source_total": 90,
    })
    assert summary == {
        "snapshot_ms": 10.0, "representative_vendor_ms": 20.0, "forecast_stock_ms": 30.0,
        "current_customer_ms": 42.0, "history_ms": 50.0, "pending_ms": 60.0,
        "r230_ms": 70.0, "contract_ms": 80.0, "post_source_assembly_ms": 90.0,
    }
    with TemporaryDirectory() as artifact_dir:
        path = Path(write_manual_validation_evidence({"_order_calculation_diagnostic_dir": artifact_dir}, evidence))
        assert path.is_file()
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["canonical_result"]["sha256"] == evidence["canonical_result"]["sha256"]
        assert saved["question"] == "발주담당자 김 발주계산"
    print("PASS canonical/raw-product-set/error diagnostic artifact is deterministic; ERP calls 0")


def _assert_r230_scoped_source_call_contract():
    from app.services import rddbc230_service as r230

    source = pd.DataFrame(
        [{"제품코드": "00001", "재고위치": "00001", "실입고단가": 100}]
    )
    with patch.object(r230, "execute_bound_select", return_value=source) as select:
        result = r230.get_rddbc230_result(
            {"order_product_code_list": ["00002", "00001", "00001"]}
        )
    assert select.call_count == 1
    sql, values = select.call_args.args
    assert "S.Rd23_Physic_Cd IN (?, ?, ?)" in sql
    assert tuple(values[:3]) == ("00002", "00001", "00001")
    assert result["meta"]["source_call_count"] == 1
    print("PASS R230 scoped final-price source has one physical service query; ERP calls 0")


def run():
    _assert_early_scope_exact_set()
    _assert_snapshot_error_precedes_early_scope()
    _assert_manual_validation_evidence()
    _assert_r230_scoped_source_call_contract()
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
        ('제약사 피엠지 발주수량 조회조건 전체 뽑아줘', 3, 15, 25),
        ('발주담당자 홍길동 발주 계산', 3, 15, 25),
        ('발주담당자 신민우 발주계산', 3, 15, 25),
        ('발주담당자 신민우 발주 계산 적정재고 30일로 해줘', 3, 30, 25),
        ('발주담당자 신민우 발주계산 안전재고 5일 적정재고 15일 결제일 31일', 5, 15, 31),
        ('제약담당자 이기재 발주계산', 3, 15, 25),
    ]
    for text, safety, target, closing in calculations:
        result = resolve_registered_erp_table_nlq(text, today=date(2026, 9, 14))
        p = result['params']
        assert result['action'] == '발주 계산'
        assert (p['safety_days'], p['target_days'], p['closing_day']) == (safety, target, closing)
        assert resolve_io_nlq(text)['action'] == '발주 계산'
        assert resolve_new_sims_nlq_candidate(text)['action'] == '발주 계산'
        if text.endswith('해줘'):
            from app.sims.nlq.nlq_router import _append_lookup_verb_for_io, _normalize_io_action_spacing
            routed = resolve_io_nlq(_append_lookup_verb_for_io(_normalize_io_action_spacing(text)))
            assert routed['params']['order_staff_nm'] == '신민우'
            assert routed['params']['target_days'] == 30
        if '12345' in text:
            assert p['cost_apply_cd'] == '12345' and p['stock_apply_cd'] == '23456'
            assert p['query_mode'] == '발주해당자료만' and p['only_needed']
        elif '피엠지' in text:
            assert p['maker_nm'] == '피엠지'
            assert p['query_mode'] == '전체' and not p['only_needed']
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
        elif '전체' not in text and '발주해당자료만' not in text:
            assert p['query_mode'] == '발주해당자료만' and p['only_needed']
        if '발주담당자' in text:
            expected_staff = next(name for name in ('홍길동', '신민우') if name in text)
            assert p['order_staff_nm'] == expected_staff
        if '제약담당자' in text:
            assert p['pharma_staff_nm'] == '이기재'
        cases.append({'text': text, **result})
    assert not is_order_calculation_request('제품 조회조건 전체')
    # Dispatch through the real router and service, using the same captured authority fixture.
    for text in ('제약사 환인 조회구분 전체 발주 계산',
                 '제약사 환인 안전재고 3일 적정재고 15일 결제일 25일 조회구분 전체 발주 계산',
                 '제약사 한림 안전재고 5일 적정재고 20일 마감일자 25일 조회구분 전체 발주 계산',
                 '제품코드 64063 조회구분 전체 발주 수량 계산'):
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
        assert requests[0]["_original_question"] == text
        with patch('app.services.order_calculation_service.get_current_company_id', return_value=7):
            panel = get_order_calculation_result(requests[0], source_loader=lambda p: sources)
        pd.testing.assert_frame_equal(sent[0]['df'], panel['df'])
        assert sent[0]['meta']['source_call_count'] == 0

    staff_cases = {
        '발주담당자 신민우 조회구분 전체 발주계산': ['신민우'],
        '발주담당자 신 조회구분 전체 발주계산': ['신민우'],
        '발주담당자 윤 조회구분 전체 발주계산': ['윤정아'],
        '발주담당자 없는이름 발주계산': [],
    }
    for text, expected_names in staff_cases.items():
        _, sources = staff_fixture()
        sent, requests = [], []
        def staff_service(q):
            requests.append(q)
            return get_order_calculation_result(q, source_loader=lambda p: sources)
        with patch('app.services.order_calculation_service.get_current_company_id', return_value=7), \
             patch('app.services.order_calculation_service.get_order_calculation_result', side_effect=staff_service), \
             patch('app.ui.chat_middleware.push_sims_result_to_chat', side_effect=lambda p, a: sent.append(p) or p['meta']):
            assert _try_handle_io_nlq(text, room={}, session_state={}, make_ts=lambda: 'fixture',
                next_seq=lambda: 1, logger=logging.getLogger('fixture'))
        assert len(requests) == len(sent) == 1
        assert requests[0]['order_staff_nm'] in text
        assert requests[0]["_original_question"] == text
        if expected_names:
            assert sorted(sent[0]['df']['발주담당자'].drop_duplicates().tolist()) == expected_names
        else:
            assert sent[0].get('records') == [] and sent[0]['meta']['row_count_total'] == 0
        assert sent[0]['meta']['source_call_count'] == 0

    pharma_cases = {
        '제약담당자 이 조회구분 전체 발주계산': ['이기재', '이정민'],
        '제약담당자 이기재 조회구분 전체 발주계산': ['이기재'],
        '제약담당자 없는이름 조회구분 전체 발주계산': [],
    }
    for text, expected_names in pharma_cases.items():
        _, sources = staff_fixture()
        sent, requests = [], []
        def pharma_service(q):
            requests.append(q)
            return get_order_calculation_result(q, source_loader=lambda p: sources)
        with patch('app.services.order_calculation_service.get_current_company_id', return_value=7), \
             patch('app.services.order_calculation_service.get_order_calculation_result', side_effect=pharma_service), \
             patch('app.ui.chat_middleware.push_sims_result_to_chat', side_effect=lambda p, a: sent.append(p) or p['meta']):
            assert _try_handle_io_nlq(text, room={}, session_state={}, make_ts=lambda: 'fixture',
                next_seq=lambda: 1, logger=logging.getLogger('fixture'))
        assert requests[0]['pharma_staff_nm'] in text
        assert requests[0]["_original_question"] == text
        if expected_names:
            assert sorted(sent[0]['df']['제약담당자'].drop_duplicates().tolist()) == expected_names
        else:
            assert sent[0].get('records') == [] and sent[0]['meta']['row_count_total'] == 0
        assert sent[0]['meta']['source_call_count'] == 0

    ambiguous = resolve_registered_erp_table_nlq('담당자 신 발주계산')['params']
    assert ambiguous.get('_ambiguous_staff_role') is True
    sent = []
    with patch('app.services.order_calculation_service.get_current_company_id', return_value=7), \
         patch('app.ui.chat_middleware.push_sims_result_to_chat', side_effect=lambda p, a: sent.append(p) or p['meta']):
        assert _try_handle_io_nlq('담당자 신 발주계산', room={}, session_state={}, make_ts=lambda: 'fixture',
            next_seq=lambda: 1, logger=logging.getLogger('fixture'))
    assert sent[0]['meta']['result_status'] == 'input_required'
    assert sent[0]['meta']['source_call_count'] == 0

    for text in ('출고현황 영업사원 김', '출고현황 영업담당자 김'):
        assert resolve_io_nlq(text)['params']['sales_man_nm'] == '김'

    _, sources = staff_fixture()
    with patch('app.services.order_calculation_service.get_current_company_id', return_value=7):
        all_rows = get_order_calculation_result(
            {"company_id": 7, "query_mode": "전체", "only_needed": False},
            source_loader=lambda p: sources,
        )
    assert len(all_rows['df']) == 4
    assert all_rows['df']['발주담당자'].eq('').sum() == 2
    assert all_rows['meta']['order_staff_resolution'] == {
        'missing_order_vendor': 0, 'missing_sales_man': 1, 'unmatched_user': 1,
    }
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
    print('PASS order/pharma staff roles, ambiguous generic staff, salesperson aliases; ERP calls 0')
    print('PASS manufacturer partial/ambiguous 4/4; no_data conditions 1/1')
    print('PASS vendor/manufacturer collision 1/1')


if __name__ == '__main__':
    run()
