"""External API administration, explicitly permissioned per selected company."""
from __future__ import annotations

import os
from datetime import datetime, time, timedelta, timezone

import streamlit as st

from app.services import external_api_management as management
from app.services.kpis_mcp_contract import FIRST_PRIORITY, SERVICES, Request
from app.services.kpis_rest_client import INPUTS, KpisClient

KST = timezone(timedelta(hours=9))


def clear_changed_scope(state, scope):
    if state.get("__external_api_scope") != scope or scope is None:
        for key in list(state):
            if str(key).startswith("__external_api_"):
                del state[key]
        if scope is not None:
            state["__external_api_scope"] = scope


def render_external_api_admin():
    st.subheader("외부 API 관리")
    try:
        actor = management.load_current_api_session()
        clear_changed_scope(st.session_state, (actor.user_id, actor.company_id, actor.epoch) if actor else None)
        if actor is None:
            st.error("현재 회사의 유효한 사용자 연결이 필요합니다.")
            return
        management.ApiStore.authorize(actor, actor.company_id, management.VIEW)
        store = management.configured_store()
        rows = store.view(actor, actor.company_id)
    except Exception:
        clear_changed_scope(st.session_state, None)
        st.error("외부 API 전용 권한 또는 승인된 암호화 저장소 설정을 확인해 주세요.")
        return
    scope = (actor.user_id, actor.company_id, actor.epoch)
    st.caption(f"회사 {actor.company_id}")
    st.dataframe(rows, hide_index=True, width="stretch")
    service = st.selectbox("서비스", FIRST_PRIORITY, format_func=lambda c: f"{c} {SERVICES[c].name}")
    mode = st.radio("환경", ("TEST", "PRODUCTION"), horizontal=True)
    key = f"__external_api_{actor.user_id}_{actor.company_id}_{actor.epoch}_{service}_{mode}"
    end_date = st.date_input("승인 만료일(KST)", value=datetime.now(KST).date(), key=key + "_expiry")
    expires = datetime.combine(end_date, time(23, 59, 59), KST)

    def fresh_actor():
        current = management.load_current_api_session()
        if current is None or (current.user_id, current.company_id, current.epoch) != scope:
            raise management.ApiManagementError("changed_session")
        return current

    def act(fn):
        try:
            current = fresh_actor()
            fn(current)
            st.success("처리 완료")
        except Exception:
            st.error("처리하지 못했습니다. 권한·회사·만료일·입력·저장소 상태를 확인해 주세요.")

    if management.MANAGE in actor.permissions:
        with st.form(key + "_profile", clear_on_submit=True):
            applicant = st.text_input("API 신청 사업자번호 / 요양기관기호", key=key + "_applicant")
            evidence = st.text_input("이용 승인 근거 식별자", key=key + "_evidence")
            enabled = st.checkbox("서비스 활성화", value=False, key=key + "_enabled")
            if st.form_submit_button("프로필 저장"):
                act(lambda a: store.configure(a, a.company_id, service, mode, applicant=applicant,
                                             enabled=enabled, expires=expires, evidence=evidence))
    if management.SECRET in actor.permissions:
        choices = ("기존 ERP 참조", "별도 암호화 저장") if service in management.ERP_KEY_CODES and mode == "PRODUCTION" else ("별도 암호화 저장",)
        source = st.radio("인증정보 저장 방식", choices, key=key + "_source", horizontal=True)
        if source == "기존 ERP 참조":
            if st.button("기존 ERP API KEY 참조 설정", key=key + "_erp"):
                act(lambda a: store.set_erp_reference(a, a.company_id, service, mode))
        else:
            with st.form(key + "_credentials", clear_on_submit=True):
                if service == "MA116":
                    credentials = {"client_id": st.text_input("JWT Client ID", type="password", key=key + "_id"),
                                   "client_secret": st.text_input("JWT Client Secret", type="password", key=key + "_secret")}
                else:
                    credentials = {"apiKey": st.text_input("API KEY", type="password", key=key + "_secret")}
                if st.form_submit_button("인증정보 등록·교체"):
                    act(lambda a: store.set_credentials(a, a.company_id, service, mode, credentials))
        if st.button("SIMS 인증정보 폐기·참조 해제", key=key + "_revoke"):
            act(lambda a: store.set_credentials(a, a.company_id, service, mode, {}))
    if management.GRANT in actor.permissions:
        try:
            current = fresh_actor()
            st.dataframe(store.grants_view(current, actor.company_id), hide_index=True, width="stretch")
        except Exception:
            st.error("서비스 사용 권한 목록을 조회하지 못했습니다.")
        with st.form(key + "_grant", clear_on_submit=True):
            uid = st.text_input("사용자 ID", key=key + "_uid")
            allow = st.checkbox("서비스 사용 허용", key=key + "_allow")
            if st.form_submit_button("사용 권한 저장"):
                act(lambda a: store.grant(a, a.company_id, service, mode, uid, expires if allow else None))
    if management.AUDIT in actor.permissions:
        st.subheader("API 호출 및 변경 이력")
        try:
            current = fresh_actor()
            st.dataframe(store.history(current, actor.company_id), hide_index=True, width="stretch")
        except Exception:
            st.error("이력 조회 권한 또는 저장소 상태를 확인해 주세요.")
    # Explicit server-side query only. No LLM-selected URL, company or credentials.
    if "MCP_EXTERNAL_RESOURCE_READ" in actor.permissions:
        with st.form(key + "_query", clear_on_submit=True):
            query = {name: st.text_input(name, max_chars=size, key=key + "_q_" + name)
                     for name, size in INPUTS[service].items()}
            confirmed = st.checkbox("선택 회사·환경의 외부 API 조회 실행", key=key + "_confirm")
            if st.form_submit_button("API 조회", disabled=not os.environ.get("SSAI_KPIS_LIVE_ENABLED") == "1"):
                try:
                    current = fresh_actor()
                except management.ApiManagementError:
                    st.error("회사 또는 사용자 상태가 변경되었습니다.")
                    return
                client = KpisClient(store, management.load_current_api_session,
                                    enabled=os.environ.get("SSAI_KPIS_LIVE_ENABLED") == "1",
                                    test_http_approved=os.environ.get("SSAI_KPIS_TEST_HTTP_APPROVED") == "1",
                                    erp_key_loader=management.load_erp_api_key
                                    if os.environ.get("SSAI_KPIS_ERP_KEY_READ_APPROVED") == "1" else None)
                result = client.call(Request(service, mode, current.company_id, current.user_id, current.epoch),
                                     {k: v for k, v in query.items() if v}, explicit=confirmed)
                if result["status"] == "SUCCESS":
                    st.caption(f"KPIS / {mode} / {result['retrieved_at']}")
                    st.dataframe(result["rows"], hide_index=True, width="stretch")
                else:
                    st.error("API 조회 실패: " + result["reason_code"])
