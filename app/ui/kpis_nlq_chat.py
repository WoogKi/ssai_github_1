"""Single-submit KPIS chat adapter; manual form execution/publication is reused."""
from __future__ import annotations

import logging
import re

import streamlit as st

from app.services.external_api_management import ApiManagementError
from app.services.chat_company_scope_policy import room_matches_company
from app.services.kpis_nlq import MAX_PRODUCT_LIMIT, is_explicit_kpis, parse_request
from app.services.kpis_rest_client import validate_query
from app.ui import kpis_erp_query as ui


def _product_queries(plan, scope, check_scope):
    from app.services.rddbc040_service import search_standard_code_candidates
    from app.services.io_nlq import _is_product_code_token

    check_scope()
    term = plan.product_term
    candidates = search_standard_code_candidates(
        scope[0], **({"physic_cd": term} if _is_product_code_token(term) else {"keyword": term}))
    check_scope()
    if candidates.empty:
        return [], "현재 회사의 ERP 제품 검색 결과가 없습니다."
    # Keep the service's ordered TOP(201)/first-200 contract; never invent a ranking.
    all_products = list(dict.fromkeys(candidates["제품코드"].astype(str)))
    products = all_products[:plan.product_limit]
    partial = bool(candidates.attrs.get("kpis_candidates_partial"))
    codes, details = [], []
    cell = lambda value: "" if ui.pd.isna(value) else str(value).strip()
    for product in products:
        rows = candidates[candidates["제품코드"].astype(str) == product]
        first = rows.iloc[0]
        label = " / ".join(cell(first.get(key)) for key in ("제품코드", "제품명", "규격", "제약사명"))
        standards = list(dict.fromkeys(cell(value) for value in rows["표준코드"]))
        if partial and product == str(candidates.iloc[-1]["제품코드"]):
            excluded = "검색 상한 경계의 제품이므로 전체 연결 확인 필요"
        elif len(standards) != 1:
            excluded = "제품당 표준코드 1개 계약과 다른 복수 등록"
        elif not standards[0]:
            excluded = "표준코드 미등록"
        elif not re.fullmatch(r"[0-9]{13}", standards[0]):
            excluded = "표준코드 13자리 형식 오류"
        else:
            excluded = ""
        if excluded:
            details.append(f"{label}: 제외({excluded})")
        else:
            code = standards[0]
            details.append(f"{label}: {code}" + (" (중복 코드 1회 조회)" if code in codes else ""))
            if code not in codes:
                codes.append(code)
    summary = (f"ERP 검색 후보 {len(all_products)}개 제품({len(candidates)}개 매핑) 중 "
               f"기존 정렬(제품코드·표준코드 순)의 앞 {len(products)}개 제품 선택 "
               f"(요청 상위 {plan.product_limit}개), 조회 대상 {len(codes)}개 고유 표준코드. ")
    if partial:
        summary += "ERP 검색은 앞 200개 매핑의 일부 결과이며 전체 검색 건수는 미확정입니다. "
    summary += " / ".join(details)
    return [{"msupStdCd": code} for code in codes], summary


def _batch_summary(queries, completed):
    parts = []
    for query in queries:
        code = query["msupStdCd"]
        result = completed.get(code)
        if result is None:
            status = "미실행 또는 완료 미확인"
        elif result[0] == "SUCCESS":
            status = f"성공 {result[1]}건"
        else:
            status = "실패: " + ui.call_error_message(result[2])
        parts.append(f"{code}: {status}")
    return " / ".join(parts)


def try_handle_kpis_chat(text, room, *, event_id, make_message, materialize, save, is_current):
    if not is_explicit_kpis(text):
        return False
    state = st.session_state
    room_id = str(room.get("id") or "")
    if not event_id or not room_id or str(state.get("current_room") or "") != room_id or not is_current():
        return True
    event_key = "__external_api_nlq_consumed"
    if state.get(event_key) == event_id:
        return True
    state[event_key] = event_id
    state["__queue_ai"] = False
    state.pop("__deferred_current_table_followup", None)
    state.pop("__sims_flash", None)
    state["__did_user_input"] = True
    user = make_message("user", text)
    room.setdefault("messages", []).append(user)
    room.setdefault("sims_messages", []).append(dict(user))
    materialize(room)
    save()
    scope = None

    def room_current():
        return is_current() and str(state.get("current_room") or "") == room_id and (
            scope is None or room_matches_company(room, scope[0]))

    def notice(message):
        if scope is not None:
            current = ui.load_current_api_session()
            if current is None or (current.company_id, current.user_id, current.epoch) != scope:
                return
        if room_current():
            room.setdefault("messages", []).append(make_message("assistant", message))
            materialize(room)
            save()

    reason = "internal_error"
    service = ""
    queries, completed = [], {}
    summary = ""

    def check_scope():
        ui._fresh_actor(scope, service)
        if not room_current():
            raise ApiManagementError("authorization_changed")

    def record_result(query, result):
        completed[query["msupStdCd"]] = (
            result["status"], len(result.get("rows", [])), result.get("reason_code", "internal_error"))

    def incomplete(message):
        if queries:
            try:
                check_scope()
            except ApiManagementError:
                return
            message += " 전체 조회 미완료: 부분 결과표는 게시하지 않았습니다. " + _batch_summary(queries, completed)
        notice(message)

    try:
        plan = parse_request(text, today=ui.kst_today())
        service, reason = plan.service, plan.reason
        if plan.status == "clarify":
            notice(plan.message)
            return True
        actor = ui.load_current_api_session()
        if actor is None:
            raise ApiManagementError("stale_or_mismatched_session")
        scope = (actor.company_id, actor.user_id, actor.epoch)
        if not room_current() or (room.get("user_id") is not None and str(room["user_id"]) != str(actor.user_id)):
            raise ApiManagementError("authorization_changed")
        ui._fresh_actor(scope, service)
        query = dict(plan.query)
        if plan.status == "products":
            queries, summary = _product_queries(plan, scope, check_scope)
            if not queries:
                notice(summary + " 조회 가능한 표준코드가 없어 API를 호출하지 않았습니다.")
                reason = "no_product_codes"
                return True
            if len(queries) > MAX_PRODUCT_LIMIT:
                raise ApiManagementError("invalid_query")
            for item in queries:
                validate_query(service, item)
            query = queries[0]
        if plan.status == "defaults":
            defaults = ui._initial_dates(service, ui.kst_today())
            query.update({k: v.strftime("%Y%m%d") for k, v in defaults.items()
                          if not k.startswith("_") and v is not None})
        query = validate_query(service, query)
        if not room_current():
            raise ApiManagementError("authorization_changed")
        if queries:
            result = ui._call_selected(service, queries, scope, check_scope=check_scope, on_result=record_result)
            if result["status"] == "SUCCESS":
                summary += " / " + _batch_summary(queries, completed)
        else:
            result = ui._call_selected(service, [query], scope)
        ui._fresh_actor(scope, service)
        if not room_current():
            raise ApiManagementError("authorization_changed")
        reason = result.get("reason_code", "completed")
        if result["status"] != "SUCCESS":
            incomplete(summary + " / " + ui.call_error_message(reason) if queries else ui.call_error_message(reason))
        elif ui.publish_result(result, query, scope, **({"summary_note": summary} if queries else {})):
            materialize(room)
            save()
            reason = "published"
        else:
            reason = "publication_denied"
            notice("현재 회사·사용자 상태 또는 채팅 전달 상태를 확인해 주세요. 자동 재조회하지 않습니다.")
    except ApiManagementError as exc:
        reason = str(exc)
        incomplete({"missing_query": "필수 조회조건을 확정하지 못했습니다. 공통 영업일 달력이 준비되지 않았다면 공급일을 직접 지정해 주세요. API를 호출하지 않았습니다.",
                "invalid_date_range": "시작일이 종료일보다 늦습니다. API를 호출하지 않았습니다."}.get(reason, ui.call_error_message(reason)))
    except Exception:
        reason = "internal_error"
        incomplete("KPIS 요청 처리 중 오류가 발생했습니다. 자동 재시도나 ERP NLQ 우회 실행은 하지 않습니다.")
    finally:
        # Never log the original question, identifiers, exceptions, or response body.
        allowed_reasons = set(ui.CALL_ERRORS) | {
            "unsupported_or_ambiguous", "input_limit", "unresolved_condition", "standard_selection_required",
            "invalid_date", "missing_date_endpoint", "missing_explicit_date", "invalid_date_range",
            "missing_query", "invalid_query", "invalid_supply_form", "invalid_supply_type", "invalid_standard_code",
            "invalid_recall_year", "jwt_pending", "ready", "existing_default", "completed", "published",
            "publication_denied", "internal_error", "no_product_codes", "product_search", "product_limit_exceeded"}
        logging.getLogger("ssai.kpis").info("[kpis.nlq] service=%s reason=%s", service or "none",
                                          reason if reason in allowed_reasons else "request_denied")
    return True
