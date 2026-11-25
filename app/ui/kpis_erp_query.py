"""Explicit KPIS ERP-reference form; results use the existing chat table path."""
from __future__ import annotations

from functools import partial
from datetime import datetime, timedelta
from calendar import monthrange
import logging
import re
from hashlib import sha256

import pandas as pd
import streamlit as st

from app.services.external_api_management import ApiManagementError, future, load_current_api_session
from app.services.kpis_erp_direct import DIRECT_SERVICES, ErpDirectAccess, configured_direct_client
from app.services.kpis_mcp_contract import Request, SERVICES
from app.services.kpis_rest_client import INPUTS, OUTPUTS, validate_query, response_row_limit
from app.services.business_calendar_service import kst_today, recent_business_days
from app.ui.external_api_admin import clear_changed_scope
from app.ui.kpis_result_display import attachment_label, notice_plain_text

LABELS = {"srchStaDd": "조회 시작일", "srchEndDd": "조회 종료일",
          "msupStdCd": "의약품 표준코드(13자리)", "msupSplyFrmCd": "거래처 구분", "tkrHbin": "거래처 사업자번호",
          "tkrpNm": "거래처명", "ykiho": "요양기관기호", "splyTpCd": "공급 구분", "splyDd": "공급일",
          "rcvYr": "보고 연도", "rcvQt": "보고 분기", "owcpOtcTpCd": "자사/타사 구분",
          "rpayCmmYr": "회수명령년", "rpayCmmStaDd": "회수명령 시작일", "rpayCmmEndDd": "회수명령 종료일"}
KPIS_ACTIONS = {
    "의약품센터 공지사항 조회 (MA117)": "MA117",
    "표준코드 이력조회 (MA101)": "MA101",
    "KPIS 거래처 조회 (MA102)": "MA102",
    "분기별 적기보고율 조회 (MA116)": "MA116",
    "공급내역 집계 조회 (MA110)": "MA110",
    "회수의약품 정보조회 (MA109)": "MA109",
}
DATE_FIELDS = frozenset({"srchStaDd", "srchEndDd", "splyDd", "rpayCmmStaDd", "rpayCmmEndDd"})
CODE_OPTIONS = {
    "msupSplyFrmCd": {"5": "요양기관 (5)", "6": "의약품공급업체 (6)",
                      "8": "안전상비의약품 (8)", "9": "특수의료시설 (9)"},
    "splyTpCd": {None: "출고 + 정정", "1": "출고 (1)", "2": "반품 (2)",
                 "3": "폐기 (3)", "4": "정정 (4)"},
}
NOTICE_COLUMNS = {"brdOrd": "공지번호", "brdTtl": "제목", "brdDtlTxt": "내용",
                  "wrtpNm": "작성자", "wrtYmd": "작성일", "fileYn": "첨부여부"}
SUPPLY_COLUMNS = {"msupSplyAmt": "공급금액", "msupSplyQty": "공급수량", "rcvCnt": "접수건수"}
VENDOR_COLUMNS = {"bscqRegYn": "거래처등록여부", "bscqNm": "거래처명칭", "cloDd": "폐기일자"}
RECALL_COLUMNS = {"msupStdCd": "표준코드", "msupMnfNo": "제조번호", "mfdsItmCrtrCd": "품목기준코드",
                  "rpayCmmDt": "회수명령일자", "regDt": "등록일자", "mnfDt": "제조일자",
                  "rpayRspnpBizrno": "회수의무자 사업자번호", "rpayRspnpFirmNm": "회수의무자명",
                  "riskGrdDivCdNm": "위험등급분류코드명", "rpayCmmEndDd": "회수명령종료일"}
STANDARD_COLUMNS = {
    "stdCd": "표준코드", "korMgdsNm": "표준코드명칭", "adptFrDt": "적용개시일자", "adptToDt": "적용종료일자",
    "tranTkovrAdptFrDt": "양도개시일자", "tranTkovrAdptToDt": "양도종료일자", "packMgdsTotQty": "포장내제품총수량",
    "upCost": "상한가", "payType": "급여/비급여 구분", "sftMsup": "안전상비 의약품여부", "spcGnlCd": "일반/전문구분",
    "wlprLprcRadt": "퇴장방지/저가/방사선여부", "itemStdCd": "품목기준코드", "cnclDt": "식약처취소일자",
    "snoOmssYnCd": "일련번호제외 여부코드", "snoOmssRsCd": "일련번호제외 사유코드",
    "msupDctOftTpCd": "의약품판독장비구분코드", "crtrPrdCd3": "기준제품코드3",
}
CALL_ERRORS = {
    "ma102_counterparty_required": "거래처 사업자번호는 필수입니다. 거래처명만으로 조회할 수 없습니다.",
    "invalid_counterparty_number": "거래처 사업자번호는 숫자 10자리 또는 000-00-00000 형식으로 입력해 주세요.",
    "invalid_institution_code": "요양기관기호는 숫자 8자리로 입력하거나 비워 두세요.",
    "ma109_rows_missing": "MA109 응답에 회수의약품 목록 필드가 없습니다. 응답 계약 확인이 필요합니다.",
    "ma109_rows_null": "MA109 회수의약품 목록이 NULL입니다. 정상 0건으로 처리하지 않았습니다.",
    "ma109_rows_type": "MA109 회수의약품 목록이 공식 배열 형식과 다릅니다.",
    "ma109_rows_limit": f"MA109 결과가 {response_row_limit('MA109'):,}행 상한을 초과했습니다. 기간을 좁혀 조회해 주세요. 자료를 자르거나 자동 분할하지 않았습니다.",
    "erp_key_missing_or_duplicate": "선택 회사의 ERP API KEY가 미등록이거나 중복 등록되어 있습니다.",
    "erp_key_invalid": "선택 회사의 ERP API KEY 형식이 유효하지 않습니다.",
    "erp_key_unavailable": "선택 회사의 ERP API KEY를 읽지 못했습니다. ERP 연결 상태를 확인해 주세요.",
    "authorization_changed": "사용자·회사·인증 상태가 변경되어 조회를 중단했습니다.",
    "stale_or_mismatched_session": "현재 로그인·회사 세션을 다시 확인해 주세요.",
    "erp_identity_unavailable": "현재 회사의 로그인 자사정보를 확인해 주세요.",
    "erp_identity_not_verified": "현재 회사의 로그인 자사정보가 유효하지 않습니다.",
    "audit_unavailable": "감사 기록을 저장할 수 없어 조회를 중단했습니다.",
    "timeout": "KPIS 응답 제한시간을 초과했습니다. 자동 재시도하지 않습니다.",
    "http_error": "KPIS 서버가 HTTP 오류를 반환했습니다.",
    "http_auth_error": "KPIS 서버가 인증 또는 접근을 거부했습니다. 서비스 이용 자격과 ERP KEY 등록을 확인해 주세요.",
    "connection_error": "KPIS 서버에 연결하지 못했습니다. 네트워크·인증서 상태를 확인해 주세요.",
    "network_error": "KPIS 통신 중 연결이 끊기거나 송수신에 실패했습니다.",
    "protocol_error": "KPIS HTTP 통신 규약 오류로 조회를 중단했습니다.",
    "transport_error": "KPIS 요청 전송에 실패했습니다. 통신 상태를 확인해 주세요.",
    "response_decoding_error": "KPIS 응답 전송 인코딩을 해석하지 못했습니다.",
    "response_encoding_error": "KPIS 응답의 문자 인코딩을 해석하지 못했습니다.",
    "response_json_invalid": "KPIS 응답이 올바른 JSON 형식이 아닙니다.",
    "response_type": "KPIS 응답의 콘텐츠 유형이 JSON이 아닙니다.",
    "response_schema": "KPIS 응답의 최상위 구조가 공식 객체 형식과 다릅니다.",
    "response_row": "KPIS 응답에 올바르지 않은 목록 행이 있습니다.",
    "response_field": "KPIS 응답 필드의 자료형 또는 값이 허용 형식과 다릅니다.",
    "response_field_limit": "KPIS 응답 필드가 허용 길이를 초과했습니다.",
    "compressed_response_not_supported": "요청한 비압축 형식과 다른 압축 응답을 받아 중단했습니다.",
    "elapsed_limit": "KPIS 응답 수신의 전체 제한시간을 초과했습니다.",
    "provider_rejected": "KPIS가 조회 요청을 거부했습니다. 조회조건과 서비스 이용 자격을 확인해 주세요.",
    "response_rows": "KPIS 응답 목록 형식 또는 허용 행수를 확인할 수 없습니다.",
    "ma110_rows_missing": "MA110 응답에 공급내역 목록 필드가 없습니다. 응답 계약 확인이 필요합니다.",
    "ma110_rows_null": "MA110 공급내역 목록이 NULL입니다. 0건으로 처리하지 않았습니다.",
    "ma110_rows_type": "MA110 공급내역 목록이 공식 배열 형식과 다릅니다.",
    "ma110_rows_limit": "MA110 공급내역이 1,000행 상한을 초과했습니다. 결과를 잘라 표시하지 않았습니다.",
    "response_limit": "KPIS 응답 크기가 허용 범위를 초과했습니다.",
    "credential_in_response": "응답에 인증정보가 포함될 가능성이 있어 결과 표시를 차단했습니다.",
    "internal_or_transport_error": "KPIS 연결 또는 응답 처리에 실패했습니다.",
}


def call_error_message(reason):
    return CALL_ERRORS.get(reason, "KPIS 조회를 완료하지 못했습니다. 감사 기록의 결과 코드를 확인해 주세요.")


def _display_query_value(name, value):
    if name in CODE_OPTIONS:
        return CODE_OPTIONS[name].get(value, value)
    if name in DATE_FIELDS:
        try:
            return datetime.strptime(value, "%Y%m%d").date().isoformat()
        except (TypeError, ValueError):
            pass
    return value


def publish_result(result, query, expected, *, load_session=None, push=None, summary_note=""):
    current = (load_session or load_current_api_session)()
    if (current is None or (current.company_id, current.user_id, current.epoch) != expected
            or not future(current.expires_at)
            or result.get("status") != "SUCCESS" or result.get("company_id") != current.company_id
            or result.get("service") not in DIRECT_SERVICES):
        return False
    service = result["service"]
    try:
        ErpDirectAccess().execution_profile(current, Request(service, "PRODUCTION", *expected))
    except ApiManagementError:
        return False
    labels = {"MA117": NOTICE_COLUMNS, "MA101": STANDARD_COLUMNS, "MA109": RECALL_COLUMNS,
              "MA110": SUPPLY_COLUMNS, "MA102": VENDOR_COLUMNS}.get(service, {})
    columns = list(labels) if labels else sorted(OUTPUTS[service][1])
    frame = pd.DataFrame(result["rows"], columns=columns)
    if service == "MA117":
        frame["brdDtlTxt"] = frame["brdDtlTxt"].map(notice_plain_text)
        frame["fileYn"] = frame["fileYn"].map(attachment_label)
    if labels:
        frame = frame.rename(columns=labels)
    if service in {"MA101", "MA109"} and "erp_mapping" in result:
        from app.ui.kpis_result_display import with_product_mapping
        frame = with_product_mapping(frame, result["erp_mapping"], result.get("erp_mapping_status", "ready"))
    title = "KPIS " + SERVICES[service].name
    if service == "MA101":
        title = "KPIS 표준코드 이력조회 (MA101)"
    elif service == "MA102":
        title = "KPIS 거래처 조회 (MA102)"
    elif service == "MA109":
        title = "KPIS 회수의약품 정보조회 (MA109)"
    payload = dict(type="table", action=title, title=title, df=frame,
                   params=dict(query), meta={"_ssai_company_id": current.company_id,
                       "source": "KPIS", "service": service, "result_status": "success",
                       "query_summary": ("전체 변경 이력 / " if service == "MA101" else "") + " / ".join(f"{LABELS.get(k, k)}: {_display_query_value(k, v)}" for k, v in query.items()) + (" / " + summary_note if summary_note else ""),
                       "physical_call_count": result["physical_call_count"],
                       "kpis_history_scope": "전체 변경 이력" if service == "MA101" else "",
                       "retrieved_at": result["retrieved_at"]})
    if push is None:
        from app.ui.chat_middleware import wssz
        push = wssz
    return push(payload, action=title) is not None


def _fresh_actor(expected, service="MA101"):
    actor = load_current_api_session()
    if actor is None or (actor.company_id, actor.user_id, actor.epoch) != expected:
        raise ApiManagementError("authorization_changed")
    ErpDirectAccess().execution_profile(actor, Request(service, "PRODUCTION", *expected))
    return actor


def _product_candidates(scope, key):
    from app.services.rddbc040_service import search_standard_code_candidates
    from app.services.io_nlq import _is_product_code_token

    mode = st.radio("검색 방식", ("표준코드 직접 입력", "ERP 제품 검색"), key=key + "_mode")
    if mode == "표준코드 직접 입력":
        return None
    term = st.text_input("제품명 또는 ERP 제품코드", key=key + "_term", max_chars=100).strip()
    signature = (mode, term)
    saved_key = key + "_candidates"
    if st.session_state.get(key + "_search_sig") != signature:
        st.session_state.pop(saved_key, None)
        st.session_state.pop(key + "_products", None)
        st.session_state.pop(key + "_codes", None)
        st.session_state.pop(key + "_selected_products", None)
        for old_key in list(st.session_state):
            if old_key.startswith(key + "_codes_"):
                st.session_state.pop(old_key, None)
        st.session_state[key + "_search_sig"] = signature
    if st.button("제품 검색", key=key + "_search", disabled=not term):
        st.session_state.pop(saved_key, None)
        try:
            actor = _fresh_actor(scope)
            frame = search_standard_code_candidates(actor.company_id,
                **({"physic_cd": term} if _is_product_code_token(term) else {"keyword": term}))
            _fresh_actor(scope)
            st.session_state[saved_key] = frame
            st.session_state[key + "_search_sig"] = signature
        except Exception as exc:
            reason = str(exc) if isinstance(exc, (ValueError, ApiManagementError)) else "product_search_failed"
            messages = {"invalid_product_search": "제품명 또는 ERP 제품코드를 확인해 주세요."}
            st.error(messages.get(reason, "제품 검색 실패 또는 회사·사용자 상태 변경. 조회를 중단했습니다."))
    frame = st.session_state.get(saved_key)
    if not isinstance(frame, pd.DataFrame):
        return []
    if frame.empty:
        st.info("검색 결과가 없습니다.")
        return []
    if frame.attrs.get("kpis_candidates_partial") is True:
        st.warning("검색 결과 중 제품코드·표준코드 순 앞의 200개 매핑만 표시합니다. 전체 결과가 아니므로 필요하면 검색조건을 좁혀 주세요.")
    names = {row["제품코드"]: " / ".join(str(row.get(c) or "") for c in ("제품명", "규격", "제약사명"))
             for row in frame.to_dict("records")}
    products = st.multiselect("제품 선택", list(names), format_func=lambda p: f"{p} / {names[p]}", key=key + "_products")
    matches = frame[frame["제품코드"].isin(products)]["표준코드"]
    codes = sorted({v for v in matches if isinstance(v, str) and len(v) == 13 and v.isascii() and v.isdigit()})
    if products and not codes:
        st.warning("선택한 제품에 유효한 13자리 표준코드가 없습니다.")
    elif any(not isinstance(v, str) or v not in codes for v in matches):
        st.warning("일부 매핑에 표준코드가 없거나 형식이 잘못되었습니다.")
    selection_key = key + "_codes_" + sha256(repr((products, codes)).encode("utf-8")).hexdigest()[:16]
    selected = st.multiselect("표준코드 선택", codes, default=codes, key=selection_key)
    if len(selected) > 20:
        st.warning("연결된 표준코드를 모두 선택했습니다. 한 번에 최대 20개까지 조회할 수 있으므로 일부 코드를 제외해 주세요.")
        return []
    if any(code not in codes for code in selected):
        st.error("현재 검색 결과의 표준코드를 다시 선택해 주세요.")
        return []
    return selected


def _clear_vendor_selection(key, number_key, *, edited=False):
    state = st.session_state
    if edited and key + "_autofill" in state and state.get(number_key) == state[key + "_autofill"]:
        return
    if not edited and key + "_autofill" in state and state.get(number_key) == state[key + "_autofill"]:
        state[number_key] = ""
        state[key + "_number_revision"] = state.get(key + "_number_revision", 0) + 1
    for suffix in ("_selected", "_applied", "_autofill"):
        state.pop(key + suffix, None)


def _vendor_number_edited(key, number_key, widget_key):
    state = st.session_state
    current = number_key + "_input_" + str(state.get(key + "_number_revision", 0))
    if widget_key == current:
        state[number_key] = state.get(widget_key, "")
        _clear_vendor_selection(key, number_key, edited=True)


def _vendor_event(scope, key, service, number_key, event):
    from app.services.rddbc030_service import search_vendor_candidates
    state = st.session_state
    if not isinstance(event, dict) or type(event.get("id")) is not int:
        return False
    if event["id"] <= state.get(key + "_ack", 0):
        return False
    state[key + "_ack"] = event["id"]
    term = event.get("term")
    if not isinstance(term, str) or len(term) > 100:
        return True
    term = term.strip()
    try:
        actor = _fresh_actor(scope, service)
        if event.get("kind") == "invalidate":
            state[key + "_dirty"] = True
            _clear_vendor_selection(key, number_key)
        elif event.get("kind") == "search":
            state[key + "_dirty"] = False
            if state.get(key + "_signature") == (term,):
                return True
            state.pop(key + "_rows", None)
            state.pop(key + "_error", None)
            _clear_vendor_selection(key, number_key)
            state[key + "_signature"] = (term,)
            state[key + "_term"] = term
            if term:
                # Preserve the existing five-character alphanumeric ERP code contract.
                condition = {"ven_cd": term} if re.fullmatch(r"[0-9A-Za-z]{5}", term) else {"ven_nm_kw": term}
                frame = search_vendor_candidates(actor.company_id, scope="", **condition)
                _fresh_actor(scope, service)
                state[key + "_rows"] = frame
        elif event.get("kind") == "select" and not state.get(key + "_dirty") and term == state.get(key + "_term"):
            frame = state.get(key + "_rows")
            index = event.get("index")
            if isinstance(frame, pd.DataFrame) and type(index) is int and 0 <= index < len(frame):
                number = str(frame.fillna("").iloc[index].get("사업자번호") or "").strip()
                state[key + "_selected"] = index
                state[key + "_applied"] = index
                state[key + "_autofill"] = number
                state[number_key] = number
                state[key + "_number_revision"] = state.get(key + "_number_revision", 0) + 1
    except Exception:
        state.pop(key + "_rows", None)
        _clear_vendor_selection(key, number_key)
        state[key + "_error"] = "거래처 검색 실패 또는 회사·사용자 상태 변경. 조회를 중단했습니다."
        logging.getLogger("ssai.kpis").warning("[kpis.vendor_search] status=unavailable")
    return True


def _vendor_candidate(scope, key, service):
    from app.ui.kpis_vendor_combobox import render_vendor_combobox

    number_key = key + service + "tkrHbin"
    key += "_vendor"
    dirty = bool(st.session_state.get(key + "_dirty"))
    frame = None if dirty else st.session_state.get(key + "_rows")
    message = ""
    if isinstance(frame, pd.DataFrame):
        message = "검색 결과가 없습니다." if frame.empty else f"{len(frame)}개 후보"
    if isinstance(frame, pd.DataFrame) and frame.attrs.get("kpis_candidates_partial") is True:
        message = "거래처코드 순 앞의 200개 검색 결과입니다. 전체 결과가 아니므로 검색조건을 좁혀 주세요."
    error = st.session_state.get(key + "_error")
    if error:
        st.error(error)
    rows = frame.fillna("").to_dict("records") if isinstance(frame, pd.DataFrame) else []
    labels = [" / ".join(str(row.get(c) or "") for c in ("ERP 거래처코드", "ERP 거래처명", "사업자번호")) for row in rows]
    selected = st.session_state.get(key + "_selected")
    render_vendor_combobox(key + "_combo", {
        "term": st.session_state.get(key + "_term", ""), "options": labels,
        "selected": selected, "selectedLabel": labels[selected] if selected in range(len(labels)) else "",
        "ack": st.session_state.get(key + "_ack", 0), "message": message, "dirty": dirty,
    }, on_event=lambda event: _vendor_event(scope, key, service, number_key, event))
    if selected is not None and selected in range(len(rows)):
        try:
            _fresh_actor(scope, service)
        except ApiManagementError:
            st.session_state.pop(key + "_rows", None)
            st.error("사용자·회사·인증 상태가 변경되어 거래처 선택을 사용할 수 없습니다.")
            return "", True
    # A new programmatic value gets a new widget revision, rejecting late old inputs.
    number_widget_key = number_key + "_input_" + str(st.session_state.get(key + "_number_revision", 0))
    number = st.text_input(LABELS["tkrHbin"], max_chars=12, key=number_widget_key,
        value=st.session_state.get(number_key, ""),
        on_change=_vendor_number_edited, args=(key, number_key, number_widget_key))
    missing_link = selected is not None and not number.strip()
    if missing_link:
        st.warning("선택한 ERP 거래처에 사업자번호가 없습니다.")
    return number, missing_link or dirty


def _call_selected(service, queries, scope, *, check_scope=None, on_result=None):
    # 입력을 먼저 전부 확인하고, 실패 시 나머지 요청 및 부분 결과 게시를 중단한다.
    if not 1 <= len(queries) <= 20:
        raise ApiManagementError("invalid_query_count")
    checked = [validate_query(service, q) for q in queries]
    results = []
    for query in checked:
        _fresh_actor(scope, service)
        if check_scope is not None:
            check_scope()
        result = configured_direct_client().call(Request(service, "PRODUCTION", *scope), query, explicit=True)
        if on_result is not None:
            on_result(query, result)
        if check_scope is not None:
            check_scope()
        if result["status"] != "SUCCESS":
            return result
        results.append(result)
    combined = dict(results[-1])
    combined["rows"] = [row for result in results for row in result["rows"]]
    combined["physical_call_count"] = sum(result["physical_call_count"] for result in results)
    if service in {"MA101", "MA109"}:
        from app.services.rddbc040_service import lookup_standard_code_products
        code_field = "stdCd" if service == "MA101" else "msupStdCd"
        codes = {row.get(code_field) for row in combined["rows"]}
        valid = {v for v in codes if isinstance(v, str) and len(v) == 13 and v.isascii() and v.isdigit()}
        try:
            combined["erp_mapping"] = lookup_standard_code_products(scope[0], valid,
                check_scope=lambda: _fresh_actor(scope, service))
            combined["erp_mapping_status"] = "ready"
        except ApiManagementError:
            raise
        except Exception:
            # The API result remains complete; never label a failed lookup as unregistered.
            combined["erp_mapping"] = pd.DataFrame()
            combined["erp_mapping_status"] = "unavailable"
            logging.getLogger("ssai.kpis").warning("[kpis.erp_mapping] status=unavailable")
        _fresh_actor(scope, service)
        if check_scope is not None:
            check_scope()
    return combined


def _initial_dates(service, today):
    if service == "MA117":
        return {"srchStaDd": today - timedelta(days=7), "srchEndDd": today}
    if service == "MA109":
        year, month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        return {"rpayCmmStaDd": today.replace(year=year, month=month, day=min(today.day, monthrange(year, month)[1])),
                "rpayCmmEndDd": today}
    if service == "MA110":
        result = recent_business_days(base_date=today, count=1, include_base=False)
        return {"splyDd": datetime.strptime(result.dates[0], "%Y%m%d").date()
                if result.status == "ready" and result.dates else None,
                "_calendar_unavailable": result.status != "ready" or not result.dates}
    return {}


def render_kpis_erp_query(service="MA117"):
    idle = {"type": "form_only", "final": False}
    if service not in KPIS_ACTIONS.values():
        st.error("지원하지 않는 KPIS 작업입니다.")
        return idle
    st.subheader(next(name for name, code in KPIS_ACTIONS.items() if code == service))
    actor = load_current_api_session()
    scope = (actor.company_id, actor.user_id, actor.epoch) if actor else None
    clear_changed_scope(st.session_state, scope)
    if actor is None:
        st.error("활성 로그인 사용자와 접근 가능한 활성 회사를 확인해 주세요.")
        return idle
    try:
        ErpDirectAccess().execution_profile(actor, Request("MA117", "PRODUCTION", *scope))
    except ApiManagementError:
        st.error("현재 회사의 로그인 자사정보를 확인해 주세요. 회사 재선택 또는 재로그인이 필요할 수 있습니다.")
        return idle
    pending = service == "MA116"
    st.caption(f"회사 {actor.company_id} / " + ("JWT / 준비 중" if pending else "ERP 참조 / 운영"))
    key = f"__external_api_direct_{actor.company_id}_{actor.user_id}_{actor.epoch}_{service}"
    if pending:
        st.info("준비 중: MA116 JWT 실연동 확인이 필요합니다.")
    codes = _product_candidates(scope, key) if service == "MA101" else None
    vendor_number, vendor_blocked = _vendor_candidate(scope, key, service) if service in {"MA102", "MA110"} else (None, False)
    if service == "MA101":
        st.caption("전체 변경 이력 / 현재 적용정보 판정 기준 미확정")
    recall_mode = st.radio("조회 기준", ("회수명령년", "회수명령 기간"), index=1, key=key + "_recall_mode") if service == "MA109" else None
    defaults_key = key + "_date_defaults"
    if defaults_key not in st.session_state:
        st.session_state[defaults_key] = _initial_dates(service, kst_today())
    defaults = st.session_state[defaults_key]
    if service == "MA110" and defaults.get("_calendar_unavailable") and defaults.get("splyDd") is None:
        st.info("공통 영업일 달력이 준비되지 않아 공급일을 직접 선택해야 합니다.")
    with st.form(key + "_" + service):
        query = {}
        visible_inputs = dict(INPUTS[service])
        if service == "MA109":
            visible_inputs = ({"rpayCmmYr": 4} if recall_mode == "회수명령년"
                              else {"rpayCmmStaDd": 8, "rpayCmmEndDd": 8})
        date_fields = [name for name in visible_inputs if name in DATE_FIELDS]
        if date_fields:
            from app.sims.views.rddbc_io_shared import _render_date_input_with_week
            for column, name in zip(st.columns(len(date_fields)), date_fields):
                with column:
                    date_key = key + service + name + "_date"
                    value = _render_date_input_with_week(date_key, LABELS[name], st.session_state.get(date_key, defaults.get(name)))
                    defaults[name] = value
                    query[name] = value.strftime("%Y%m%d") if value is not None else ""
        text_fields = [(name, size) for name, size in visible_inputs.items()
                       if name not in DATE_FIELDS and (service != "MA101" or codes is None)
                       and not (name == "tkrHbin" and vendor_number is not None)]
        for offset in range(0, len(text_fields), 2):
            group = text_fields[offset:offset + 2]
            for column, (name, size) in zip(st.columns(len(group)), group):
                with column:
                    if name in CODE_OPTIONS:
                        options = CODE_OPTIONS[name]
                        choice_key = key + service + name + "_choice"
                        old = st.session_state.get(key + service + name)
                        default = list(options).index(old) if old in options else 0
                        query[name] = st.selectbox(LABELS[name], list(options), index=default,
                                                  format_func=options.__getitem__, key=choice_key)
                        continue
                    help_text = None
                    if service == "MA102":
                        help_text = {"tkrHbin": "필수: 공급받는 거래처의 사업자번호 (하이픈 입력 가능)",
                                     "tkrpNm": "공급받는 거래처명. 제품명 LIKE 검색과는 다른 API 조회조건입니다.",
                                     "ykiho": "공급받는 거래처의 요양기관기호 숫자 8자리 (선택)"}.get(name)
                    query[name] = st.text_input(LABELS[name], max_chars=12 if name == "tkrHbin" else size,
                                               key=key + service + name, help=help_text)
        submitted = st.form_submit_button("KPIS 조회", disabled=pending or vendor_blocked)
    if submitted:
        if pending:
            st.error("MA116은 JWT 실연동 준비 중입니다.")
            return idle
        query = {k: v for k, v in query.items() if v or (service == "MA110" and k == "splyTpCd" and v is None)}
        try:
            if vendor_number is not None:
                if vendor_blocked:
                    st.error("조회 거래처의 사업자번호를 확인해 주세요.")
                    return idle
                _fresh_actor(scope, service)
                if vendor_number.strip():
                    query["tkrHbin"] = vendor_number.strip()
            queries = [{"msupStdCd": c} for c in codes] if codes is not None else [query]
            if not queries:
                st.error("조회할 표준코드를 선택해 주세요.")
                return idle
            queries = [validate_query(service, q) for q in queries]
            with st.spinner("KPIS 조회 중"):
                result = _call_selected(service, queries, scope)
        except ApiManagementError as exc:
            messages = {"invalid_date_range": "조회 시작일은 종료일보다 늦을 수 없습니다.",
                        "invalid_date": "조회 날짜를 확인해 주세요.", "missing_query": "필수 조회조건을 입력해 주세요.",
                        "invalid_recall_year": "회수명령년 4자리를 입력해 주세요.",
                        "invalid_standard_code": "의약품 표준코드 13자리를 입력해 주세요."}
            st.error(messages.get(str(exc), CALL_ERRORS.get(str(exc), "조회조건 또는 현재 인증 상태를 확인해 주세요.")))
            return idle
        query = queries[0] if len(queries) == 1 else {"msupStdCd": ", ".join(q["msupStdCd"] for q in queries)}
        if result["status"] != "SUCCESS":
            st.error(call_error_message(result["reason_code"]))
        elif publish_result(result, query, scope):
            st.rerun()
        else:
            st.error("현재 회사·사용자 상태 또는 채팅 전달 상태를 확인해 주세요.")
    return idle


KPIS_VIEWS = {name: partial(render_kpis_erp_query, service=code) for name, code in KPIS_ACTIONS.items()}
KPIS_VIEWS["표준코드마스터 조회 (MA101)"] = partial(render_kpis_erp_query, service="MA101")
KPIS_VIEWS["거래처 정보 조회 (MA102)"] = partial(render_kpis_erp_query, service="MA102")
