"""ERP panel adapters: condition widgets submit to the registered detail service."""
import streamlit as st

from app.db.labels_map import LABELS
from app.sims.views.master_advanced_filters import render_master_audit_filter
from app.services import rddbc310_service, rddbc320_service, rddbc330_service
from app.services.erp_table_query_service import calendar_month_period, query_scope
from app.sims.views.rddbc_io_shared import (
    _apply_vendor_pick, _needs_vendor_pick, _render_io_date_range,
    _render_vendor_candidate_row, _trigger_panel_run,
)


def _vendor_candidates(text, scope):
    if not text.strip():
        return []
    from app.services.rddbc030_service import search_vendor_candidates
    query_scope("IO_READ", expected=scope)
    try:
        frame = search_vendor_candidates(scope["company_id"], ven_nm_kw=text.strip())
    except Exception:
        raise RuntimeError("거래처 후보를 조회할 수 없습니다. 입력과 연결 상태를 확인해 주세요.") from None
    query_scope("IO_READ", expected=scope)
    code, name = "ERP 거래처코드", "ERP 거래처명"
    return [(str(c).strip(), str(n).strip()) for c, n in frame[[code, name]].itertuples(index=False, name=None)]


def _view(spec, run):
    title = spec["title"]
    pending = {"title": title, "action": title, "final": False, "data": "조회조건을 입력해 주세요."}
    try:
        scope = query_scope("IO_READ", fresh=False)
    except PermissionError as exc:
        st.warning(str(exc))
        return pending
    prefix = "__io_detail_" + "_".join(map(str, (spec["table"], *scope.values())))
    if st.session_state.get(prefix + "_vendor_candidates"):
        try:
            query_scope("IO_READ", expected=scope)
        except PermissionError as exc:
            st.warning(str(exc))
            return pending
    labels = LABELS[spec["table"]]
    start, end = calendar_month_period()
    filters = {}
    table = spec["table"]

    def input_row(*keys):
        for col, key in zip(st.columns(len(keys)), keys):
            filters[key] = col.text_input(spec["filter_labels"][key], key=prefix+"_"+key)

    with st.form(prefix, clear_on_submit=False, enter_to_submit=False):
        role_col, *date_cols = st.columns(3)
        role = role_col.selectbox("조회 기준일", spec["dates"],
                                  format_func=lambda d: labels[spec["date_columns"][d]], key=prefix+"_role")
        period = _render_io_date_range(prefix, default_from=start, default_to=end, columns=date_cols)
        cd, nm, find_vendor = _render_vendor_candidate_row(prefix)
        direction = "all"
        if table == "rddbc310":
            family, code, staff = st.columns(3)
            options = {"all": "전체", "receipt": "입금", "payment": "출금"}
            direction = family.selectbox("입금/출금 구분", list(options), format_func=options.get, key=prefix+"_family")
            filters["flow_code"] = code.text_input(spec["filter_labels"]["flow_code"], key=prefix+"_group_code")
            filters["vendor_staff_name"] = staff.text_input(spec["filter_labels"]["vendor_staff_name"], key=prefix+"_staff")
        else:
            input_row("note_no", "note_seq", "note_type_name" if table == "rddbc320" else "collateral_kind_name")
        with st.expander("추가 조회조건"):
            if table == "rddbc310":
                input_row("bank_name", "product_group_name", "sales_user_name")
                input_row("reference_vendor_name", "transfer_vendor_name")
                input_row("note_no", "note_seq")
                input_row("slip_no", "slip_seq", "permit_no", "permit_seq")
                input_row("flow_name", "remark")
            elif table == "rddbc320":
                input_row("bank_name", "note_class_name", "flow_name")
                input_row("slip_no", "slip_seq")
                input_row("issuer", "deleted", "remark")
            else:
                input_row("bank_name", "note_type_name", "offer_kind")
                input_row("issuer", "deleted", "remark")
            input_row("amount_from", "amount_to")
            filters.update(render_master_audit_filter(prefix=prefix, ns="audit", embedded=True,
                                                      add_user_label="등록자명", mod_user_label="수정자명"))
        submit = st.form_submit_button("조회", type="primary", on_click=_trigger_panel_run)
    try:
        if find_vendor:
            found = _vendor_candidates(nm, scope)
            st.session_state[prefix+"_vendor_candidates"] = found
            st.session_state[prefix+"_vendor_lookup_name"] = nm.strip()
            st.session_state[prefix+"_vendor_msg"] = f"거래처 후보 {len(found)}건"
            st.rerun()
        if not submit:
            return pending
        vendor = {"ven_cd": cd.strip(), "ven_nm": nm.strip()}
        if _needs_vendor_pick(prefix, vendor):
            raise ValueError("거래처 후보를 선택해 주세요.")
        vendor = _apply_vendor_pick(prefix, vendor)
        if vendor["ven_cd"]:
            filters["vendor_cd"] = vendor["ven_cd"]
        elif vendor["ven_nm"]:
            filters["vendor_name"] = vendor["ven_nm"]
        return run({"date_from": period["date_from"], "date_to": period["date_to"], "date_role": role,
                    "direction": direction, "filters": filters, "_display_context": "panel"}, expected_scope=scope)
    except (ValueError, PermissionError, RuntimeError) as exc:
        st.warning(str(exc))
        return pending


def view_rddbc310(params=None):
    return _view(rddbc310_service.SPEC, rddbc310_service.get_rddbc310_result)


def view_rddbc320(params=None):
    return _view(rddbc320_service.SPEC, rddbc320_service.get_rddbc320_result)


def view_rddbc330(params=None):
    return _view(rddbc330_service.SPEC, rddbc330_service.get_rddbc330_result)
