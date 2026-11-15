"""Condition-only Panel; results and quantity editor belong to Chat."""

from datetime import date
import streamlit as st

from app.services.business_calendar_service import kst_today
from app.services.order_calculation_service import ACTION, get_order_calculation_result, apply_application_defaults, load_sources
from app.sims.views.product_master_filters import render_product_master_filters
from app.sims.views.rddbc_io_order_views import _application_filters, _order_vendor_filters
from app.sims.views.rddbc_io_shared import _trigger_panel_run


def view_order_calculation(params=None, *, hospital=False):
    defaults = apply_application_defaults(params or {})
    prefix = "__io_hospital_order_calculation" if hospital else "__order_calculation"
    action = "병원발주계산" if hospital else ACTION
    from app.db.mssql_client import get_current_company_id
    company = get_current_company_id()
    allocation_options = {}
    if hospital:
        from app.services.hospital_order_calculation_service import group_options, SELECTION, AllocationError
        owner_key = prefix + '_company'
        previous_company = st.session_state.get(owner_key)
        foreign_defaults = defaults.get('company_id') is not None and str(defaults['company_id']) != str(company)
        if previous_company != company or foreign_defaults:
            if previous_company is not None or foreign_defaults:
                for field in (SELECTION, 'cost_apply_cd', 'cost_apply_nm', 'stock_apply_cd', 'stock_apply_nm'):
                    defaults.pop(field, None)
                defaults = apply_application_defaults(defaults)
            for key in list(st.session_state):
                if str(key).startswith(prefix):
                    st.session_state.pop(key, None)
            st.session_state[owner_key] = company
        defaults['company_id'] = company
        try:
            allocation_options = group_options(company, timeout_seconds=15)
        except AllocationError as exc:
            st.error(str(exc))
            return {"action": action, "title": action, "final": False, "data": str(exc)}
        except Exception:
            st.error("현재 회사의 배분 재고적용처 목록을 확인하지 못했습니다. 조회하지 않았습니다.")
            return {"action": action, "title": action, "final": False, "data": "재고적용처 목록 확인 필요"}
    reuse_key = prefix + "_last_source"
    previous = st.session_state.get(reuse_key)
    if previous and previous['company_id'] != company:
        st.session_state.pop(reuse_key, None)
        for field in ('cost_apply_cd', 'cost_apply_nm', 'stock_apply_cd', 'stock_apply_nm'):
            st.session_state.pop(prefix + '_' + field, None)
        previous = None
    if previous and not hospital:
        for field in ('cost_apply', 'stock_apply'):
            authority = previous['source_params']
            if defaults.get(field + '_cd') == authority.get(field + '_cd'):
                name = authority.get(field + '_nm')
                if name:
                    defaults[field + '_nm'] = name
                    st.session_state[prefix + '_' + field + '_nm'] = name
    with st.form(prefix + "_form", enter_to_submit=False):
        if hospital:
            st.caption("부모 발주 계산 적용처")
        c1, c2, a1, a2, a3, a4, c3, c4, c5 = st.columns([1.4, 1.3, 1, 1.4, 1, 1.4, 1, 1, 1])
        with c1:
            reference = st.date_input("발주일자", value=date.fromisoformat(defaults["order_date"]) if defaults.get("order_date") else kst_today())
        with c2:
            modes = ["전체", "발주해당자료만", "확인 필요"]
            saved_mode = defaults.get("query_mode") or ("발주해당자료만" if defaults.get("only_needed") else "전체")
            mode = st.selectbox("조회구분", modes, index=modes.index(saved_mode) if saved_mode in modes else 0)
        application = _application_filters(prefix, defaults, columns=(a1, a2, a3, a4), include_stock=False)
        if hospital:
            selected_allocations = st.multiselect(
                "병원별 배분 재고적용처", options=list(allocation_options),
                default=list(defaults.get(SELECTION) or []), key=prefix + '_allocations',
                format_func=lambda code: f"{allocation_options[code]} ({code})",
                help="미선택은 전체. 잔여수량은 선택 범위와 무관하게 기본 재고적용거래처에 배정합니다.")
        with c3:
            safety = st.number_input("안전재고일수(영업일)", min_value=0, value=int(defaults.get("safety_days", 3)))
        with c4:
            target = st.number_input("적정재고일수(영업일)", min_value=0, value=int(defaults.get("target_days", 15)))
        with c5:
            closing = st.number_input("결제일자/마감일자", min_value=0, max_value=31, value=int(defaults.get("closing_day", 25)))
        c1, c2, c3, c4, c5 = st.columns(5)
        with c1:
            staff = st.text_input("발주담당자", value=str(defaults.get("order_staff_nm") or ""))
        with c2:
            vendor_cd = st.text_input("발주처코드", value=str(defaults.get("order_vendor_cd") or ""))
        with c3:
            vendor_nm = st.text_input("발주처명", value=str(defaults.get("order_vendor_nm") or ""))
        with c4:
            maker_code = st.text_input("제약사코드", value=str(defaults.get("maker_cd") or ""))
        with c5:
            maker_nm = st.text_input("제약사명 포함", value=str(defaults.get("maker_nm") or ""))
        vendor = {"order_vendor_cd": vendor_cd.strip(), "order_vendor_nm": vendor_nm.strip()}
        product = render_product_master_filters(prefix=prefix, ns="calculation", defaults=defaults,
            only_use_default=False, include_price_filters=False, include_audit_filters=False, include_only_use=False, compact=True)
        product["maker_nm"] = maker_nm.strip()
        with st.expander("재고위치", expanded=False):
            s1, s2 = st.columns(2)
            with s1:
                application['stock_cd'] = st.text_input("재고위치코드", value=str(defaults.get("stock_cd") or ""), key=f"{prefix}_stock_cd").strip()
            with s2:
                application['stock_nm'] = st.text_input("재고위치명", value=str(defaults.get("stock_nm") or ""), key=f"{prefix}_stock_nm").strip()
        submitted = st.form_submit_button(action, type="primary", width="stretch", on_click=_trigger_panel_run)
    request = {**defaults, **vendor, **product, **application, "order_date": reference.isoformat(),
                "safety_days": int(safety), "target_days": int(target), "closing_day": int(closing),
                "only_needed": mode == "발주해당자료만", "order_staff_nm": staff.strip(),
                "maker_cd": maker_code.strip(), "query_mode": mode, "_display_context": "panel",
                "_original_question": "발주 계산 화면 실행"}
    if hospital:
        request.update({SELECTION: selected_allocations, '_original_question': '병원발주계산 화면 실행'})
        request.pop('_hospital_panel_required', None)
    if submitted:
        try:
            def source_loader(query):
                identity = {k: v for k, v in query.items() if not k.startswith('_')}
                for field in ('cost_apply', 'stock_apply'):
                    if identity.get(field + '_cd'):
                        identity.pop(field + '_nm', None)
                if previous and previous['identity'] == identity and previous['mode'] != mode:
                    query.update({k: v for k, v in previous['source_params'].items() if k.endswith('_apply_nm')})
                    return previous['sources']
                sources = load_sources(query)
                if sources.get('snapshot', {}).get('meta', {}).get('snapshot_status') == 'ready':
                    st.session_state[reuse_key] = {'company_id': company, 'identity': identity,
                        'mode': mode, 'source_params': dict(query), 'sources': sources}
                return sources
            if hospital:
                from app.services.hospital_order_calculation_service import get_hospital_order_calculation_result
                result = get_hospital_order_calculation_result(request, source_loader=source_loader)
            else:
                result = get_order_calculation_result(request, source_loader=source_loader)
            if st.session_state.get(reuse_key):
                st.session_state[reuse_key]['mode'] = mode
            st.session_state["__sims_panel_active"] = True
            st.session_state.pop("__sims_close_after_push", None)
            return result
        except ValueError as exc:
            st.error(str(exc))
        except Exception as exc:
            import logging
            if hospital:
                logging.getLogger("ssai").error("[hospital_order_calculation.query] failed error_type=%s", type(exc).__name__)
            else:
                logging.getLogger("ssai").exception("[order_calculation.query] query failed")
            st.error(f"{action} 원천을 확인하지 못했습니다. 재시도하지 않았습니다.")
    return {"title": action, "action": action, "params": request, "final": False,
            "data": f"조회조건을 입력해 {action}을 실행하세요."}


def view_hospital_order_calculation(params=None):
    return view_order_calculation(params, hospital=True)
