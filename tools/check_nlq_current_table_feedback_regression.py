"""Regression checks promoted from the 2026-09-17 NLQ feedback review."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.erp_table_nlq import resolve_registered_erp_table_nlq
from app.sims.nlq.nlq_router import _io_payload_user_message
from app.sims.views.users import _build_user_list_view
from app.ui.current_table_followups.action_dispatcher import (
    handle_current_table_followup_by_action,
    parse_current_table_rank_request,
)
from app.ui.chat_middleware import (
    _build_sims_result_header_view,
    _normalize_result_for_chat,
    _user_facing_chat_message,
    _user_facing_internal_object_message,
)


LOG = logging.getLogger("nlq-current-table-feedback-regression")


def _find_col(
    df: pd.DataFrame,
    *,
    exact: tuple[str, ...] = (),
    include_any: tuple[str, ...] = (),
    exclude_any: tuple[str, ...] = (),
) -> str | None:
    columns = [str(column) for column in df.columns]
    for candidate in exact:
        if candidate in columns:
            return candidate
    for column in columns:
        if include_any and not any(token in column for token in include_any):
            continue
        if any(token in column for token in exclude_any):
            continue
        return column
    return None


def _dispatch(df: pd.DataFrame, query: str, source_action: str) -> dict[str, Any]:
    kind, payload = _dispatch_any(df, query, source_action)
    if kind != "table":
        raise AssertionError(f"query={query!r}, pushed_kind={kind!r}, payload={payload!r}")
    return payload


def _dispatch_any(df: pd.DataFrame, query: str, source_action: str) -> tuple[str, dict[str, Any]]:
    pushed: list[tuple[str, dict[str, Any]]] = []

    handled = handle_current_table_followup_by_action(
        df=df.copy(deep=True),
        query=query,
        top_n=20,
        table_key="feedback-regression-fixture",
        source_action=source_action,
        helpers={
            "find_col": _find_col,
            "to_num": lambda series: pd.to_numeric(series, errors="coerce").fillna(0),
            "push_table": lambda **kwargs: (pushed.append(("table", kwargs)) or True),
            "push_notice": lambda **kwargs: (pushed.append(("notice", kwargs)) or True),
        },
        log=LOG,
        source_meta={"result_status": "success"},
    )
    if not handled or len(pushed) != 1:
        raise AssertionError(f"query={query!r}, handled={handled!r}, pushed={pushed!r}")
    return pushed[0]


def _result_frame(payload: dict[str, Any]) -> pd.DataFrame:
    frame = payload.get("df")
    if not isinstance(frame, pd.DataFrame):
        raise AssertionError(f"table payload has no DataFrame: {payload!r}")
    return frame


def _assert_rank_contract() -> None:
    inventory = pd.DataFrame(
        {
            "제품코드": [f"P{index:03d}" for index in range(1, 16)],
            "제품명": [f"제품{index:02d}" for index in range(1, 16)],
            "이월수량": [-2, -1, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
            "추세판정": ["감소"] * 12 + ["증가"] * 3,
        }
    )
    high_queries = (
        "현재표 이월수량 top 10 보여줘",
        "현재표 이월수량 많은 10개 보여줘",
        "현재표 이월수량 많은순 10개 보여줘",
    )
    low_queries = (
        "현재표 이월수량 low 10 보여줘",
        "현재표 이월수량 적은 10개 보여줘",
    )
    for query in high_queries:
        out = _result_frame(_dispatch(inventory, query, "제품재고현황 조회"))
        values = pd.to_numeric(out["이월수량"], errors="raise").tolist()
        assert len(out) == 10 and values == sorted(values, reverse=True), (query, values)
    for query in low_queries:
        out = _result_frame(_dispatch(inventory, query, "제품재고현황 조회"))
        values = pd.to_numeric(out["이월수량"], errors="raise").tolist()
        assert len(out) == 10 and values == sorted(values), (query, values)

    most = _result_frame(
        _dispatch(inventory, "현재표 이월수량 제일 많은 자료", "제품재고현황 조회")
    )
    assert len(most) == 1 and int(most.iloc[0]["이월수량"]) == 11

    below = _result_frame(
        _dispatch(inventory, "현재표 이월수량 0 이하 10개 보여줘", "제품재고현황 조회")
    )
    above = _result_frame(
        _dispatch(inventory, "현재표 이월수량 0 이상 10개 보여줘", "제품재고현황 조회")
    )
    assert len(below) == 4 and (below["이월수량"] <= 0).all()
    assert len(above) == 10 and (above["이월수량"] >= 0).all()

    price = pd.DataFrame(
        {"입고번호": list(range(1, 16)), "확정단가": list(range(1500, 0, -100))}
    )
    low_price = _result_frame(_dispatch(price, "현재표 확정단가 low 10", "입고명세 조회"))
    price_values = pd.to_numeric(low_price["확정단가"], errors="raise").tolist()
    assert len(low_price) == 10 and price_values == sorted(price_values)

    short_trend = _result_frame(
        _dispatch(inventory, "현재표 추세판정 감소", "제품재고현황 조회")
    )
    verb_trend = _result_frame(
        _dispatch(inventory, "현재표 추세판정 감소 보여줘", "제품재고현황 조회")
    )
    assert len(short_trend) == len(verb_trend) == 12

    expected_ranks = {
        "top 10": ("desc", 10),
        "high 7": ("desc", 7),
        "큰 3개": ("desc", 3),
        "제일 많은": ("desc", 1),
        "low 10": ("asc", 10),
        "작은 4개": ("asc", 4),
        "제일 적은": ("asc", 1),
    }
    for phrase, expected in expected_ranks.items():
        assert parse_current_table_rank_request(phrase) == expected, phrase


def _assert_user_group_and_projection_contract() -> None:
    users = pd.DataFrame(
        {
            "사용자명": ["사용자A", "사용자B", "사용자C"],
            "부서명": ["영업", "영업", "관리"],
            "직책": ["사원", "과장", "사원"],
            "등록자": ["관리자", "관리자", "운영자"],
            "수정자": ["운영자", "관리자", "운영자"],
        }
    )
    grouped = _result_frame(_dispatch(users, "현재표 등록자별 분석", "사용자목록 + 부서명"))
    assert "등록자" in grouped.columns and set(grouped["등록자"]) == {"관리자", "운영자"}
    assert int(grouped["행수"].sum()) == len(users)

    raw = pd.DataFrame(
        {
            "Rd06_User_Cd": ["00001"],
            "Rd06_User_ID": ["user01"],
            "Rd06_User_Nm": ["사용자A"],
            "Rd06_Department_Nm": ["영업"],
            "Rd06_Add_Cd": ["00099"],
            "add_user_nm": ["관리자"],
            "Rd06_SMS_ID": ["internal-sms"],
            "Rd06_POL_ID": ["internal-pol"],
        }
    )
    projected = _build_user_list_view(raw)
    assert len(projected) == len(raw)
    assert "사용자코드" in projected.columns and projected.iloc[0]["사용자코드"] == "00001"
    assert not any(str(column).startswith("Rd06_") for column in projected.columns)
    assert "Rd06_SMS_ID" not in projected.columns and "Rd06_POL_ID" not in projected.columns


def _assert_unlabeled_current_table_and_grain_contract() -> None:
    products = pd.DataFrame(
        {
            "제품코드": ["00001", "00002", "00003"],
            "제품명": ["로수탄젯10", "로수탄젯20", "다른제품"],
            "제조사명": ["유한양행", "유한양행", "다른제약"],
            "손익등급": ["A", "B", "A"],
        }
    )
    unlabeled = _result_frame(
        _dispatch(products, "현재표 로수탄젯 상세히 보여줘", "제품정보 조회")
    )
    explicit = _result_frame(
        _dispatch(products, "현재표 제품명 로수탄젯 상세히 보여줘", "제품정보 조회")
    )
    assert len(unlabeled) == len(explicit) == 2
    assert unlabeled["제품코드"].tolist() == explicit["제품코드"].tolist()

    ambiguous_df = products.copy()
    ambiguous_df.loc[0, "제조사명"] = "로수탄젯제약"
    kind, notice = _dispatch_any(
        ambiguous_df,
        "현재표 로수탄젯 상세히 보여줘",
        "제품정보 조회",
    )
    assert kind == "notice"
    assert (notice.get("extra_meta") or {}).get("result_status") == "candidate_required"

    kind, notice = _dispatch_any(
        products,
        "현재표 없는제품 상세히 보여줘",
        "제품정보 조회",
    )
    assert kind == "notice"
    assert (notice.get("extra_meta") or {}).get("result_status") == "no_data"

    product_group = _result_frame(
        _dispatch(products, "현재표 손익등급별 집계", "제품정보 조회")
    )
    assert "제품수" in product_group.columns and "행수" not in product_group.columns, list(product_group.columns)

    transactions = pd.concat([products, products.iloc[[0]]], ignore_index=True)
    transaction_group = _result_frame(
        _dispatch(transactions, "현재표 손익등급별 집계", "입고명세 조회")
    )
    assert "제품수" in transaction_group.columns and "행수" in transaction_group.columns


def _assert_contract_price_entity_contract() -> None:
    numeric = resolve_registered_erp_table_nlq("50002 계약단가 조회") or {}
    numeric_params = dict(numeric.get("params") or {})
    assert numeric_params.get("_registered_unlabeled_entity") == "50002"
    assert not numeric_params.get("ven_cd")

    labeled_numeric = resolve_registered_erp_table_nlq("단가적용처코드 50002 계약단가 조회") or {}
    assert (labeled_numeric.get("params") or {}).get("ven_cd") == "50002"

    semantic_only = resolve_registered_erp_table_nlq("전문약 계약단가 조회") or {}
    assert (semantic_only.get("params") or {}).get("product_di_semantic_group") == "insurance"
    assert not (semantic_only.get("params") or {}).get("ven_nm")

    missing_vendor_value = resolve_registered_erp_table_nlq("단가적용거래처 전문약 계약단가 조회") or {}
    missing_params = dict(missing_vendor_value.get("params") or {})
    assert missing_params.get("product_di_semantic_group") == "insurance"
    assert not missing_params.get("ven_nm")

    vendor_and_semantic = resolve_registered_erp_table_nlq("단가적용거래처 보덕 전문약 계약단가 조회") or {}
    vendor_params = dict(vendor_and_semantic.get("params") or {})
    assert vendor_params.get("ven_nm") == "보덕"
    assert vendor_params.get("product_di_semantic_group") == "insurance"

    maker_and_semantic = resolve_registered_erp_table_nlq("제조사 한미 전문약 계약단가 조회") or {}
    maker_params = dict(maker_and_semantic.get("params") or {})
    assert maker_params.get("maker_nm") == "한미"
    assert maker_params.get("product_di_semantic_group") == "insurance"

    product_and_semantic = resolve_registered_erp_table_nlq("제품명 이가탄 전문약 계약단가 조회") or {}
    product_params = dict(product_and_semantic.get("params") or {})
    assert product_params.get("physic_nm") == "이가탄"
    assert product_params.get("product_di_semantic_group") == "insurance"

    parsed = resolve_registered_erp_table_nlq("태응약품 계약단가 조회") or {}
    params = dict(parsed.get("params") or {})
    assert parsed.get("action") == "최종 계약단가 조회"
    assert params.get("_registered_unlabeled_entity") == "태응약품"

    from app.services import rddbc070_service
    from app.sims.nlq import nlq_router

    service_params: list[dict[str, Any]] = []
    pushed: list[dict[str, Any]] = []

    def _contract_result(params=None, **kwargs):
        final_params = dict(params or kwargs.get("params") or {})
        service_params.append(final_params)
        phrase = str(final_params.get("_r070_unlabeled_name") or "")
        assert phrase == "태응약품"
        frame = pd.DataFrame([
            {"단가적용거래처": "54165", "제품코드": "00001", "계약단가": 1000},
            {"단가적용거래처": "53904", "제품코드": "00002", "계약단가": 2000},
            {"단가적용거래처": "20325", "제품코드": "00003", "계약단가": 3000},
        ])
        return {
            "final": True,
            "type": "table",
            "title": "최종 계약단가 조회",
            "action": "최종 계약단가 조회",
            "params": final_params,
            "df": frame,
            "df_display": frame.copy(),
            "meta": {
                "row_count": 3,
                "row_count_total": 3,
                "result_status": "success",
                "source_call_count": 1,
                "physical_source_call_count": 1,
                "entity_resolution_status": "resolved",
                "resolved_kind": "cost_apply",
                "authority_matches": [{"match_type": "cost_apply", "match_count": 3}],
            },
        }

    with (
        patch(
            "app.services.io_nlq._lookup_unlabeled_io_entity_candidates",
            side_effect=AssertionError("R070 must not run the common entity resolver"),
        ),
        patch.object(rddbc070_service, "get_rddbc070_current_result", side_effect=_contract_result),
        patch(
            "app.ui.chat_middleware.push_sims_result_to_chat",
            side_effect=lambda payload, *_args, **_kwargs: (pushed.append(payload) or payload.get("meta")),
        ),
    ):
        handled = nlq_router._try_handle_io_nlq(
            "태응약품 계약단가 조회",
            room={"messages": []},
            session_state={},
            make_ts=lambda: "2026-09-17 00:00:00",
            next_seq=lambda: 1,
            logger=LOG,
        )
    assert handled and len(service_params) == 1 and len(pushed) == 1
    assert service_params[0].get("_r070_unlabeled_name") == "태응약품"
    assert (pushed[0].get("meta") or {}).get("result_status") == "success"
    assert (pushed[0].get("meta") or {}).get("physical_source_call_count") == 1
    assert len(pushed[0].get("df")) == 3

    ambiguous_code_frame = pd.DataFrame([
        {
            "__candidate_count": 2,
            "__cost_apply_match_count": 1,
            "__manufacturer_match_count": 0,
            "__product_match_count": 1,
        }
    ])
    with patch.object(
        rddbc070_service,
        "execute_bound_select",
        return_value=ambiguous_code_frame,
    ) as execute_once:
        ambiguous_code = rddbc070_service.get_rddbc070_current_result(
            {"_r070_unlabeled_name": "50002", "top": 100}
        )
    assert execute_once.call_count == 1
    ambiguous_meta = dict(ambiguous_code.get("meta") or {})
    assert ambiguous_meta.get("result_status") == "candidate_required"
    assert ambiguous_meta.get("physical_source_call_count") == 1
    assert "단가적용처코드" in str(ambiguous_code.get("message") or "")


def _assert_user_facing_metadata_boundary() -> None:
    internal_payload = {
        "action": "최종 계약단가 조회",
        "params": {"_r070_unlabeled_name": "태응약품"},
        "message": "조회 후보가 여러 개입니다.",
        "meta": {
            "result_status": "candidate_required",
            "source_call_count": 1,
            "physical_source_call_count": 1,
            "trace_request_id": "private-trace-id",
            "private_routing_metadata": {"route": "r070"},
        },
    }
    safe_message = _user_facing_internal_object_message(internal_payload)
    assert safe_message == "조회 후보가 여러 개입니다."
    for private_value in (
        "source_call_count",
        "physical_source_call_count",
        "private-trace-id",
        "_r070_unlabeled_name",
    ):
        assert private_value not in safe_message

    serialized_payload = dict(internal_payload)
    serialized_payload["message"] = '{"source_call_count": 1, "params": {"name": "태응약품"}}'
    serialized_message = _user_facing_internal_object_message(serialized_payload)
    assert serialized_message == "조회 후보가 여러 개입니다. 조회 조건을 더 구체적으로 입력해 주세요."

    no_data_payload = {
        "type": "text",
        "message": {"params": {"ven_nm": "전문약"}},
        "data": {
            "table": "Rddbc070",
            "params": {"ven_nm": "전문약"},
            "meta": {
                "result_status": "no_data",
                "source_call_count": 1,
                "physical_source_call_count": 1,
                "nlq_trace_request_id": "private-trace-id",
            },
        },
        "meta": {"result_status": "no_data"},
    }
    no_data_message = _user_facing_chat_message(
        no_data_payload,
        no_data_payload["meta"],
        no_data_payload["data"],
    )
    assert no_data_message == "해당 조회조건의 자료가 없습니다."
    assert "params" not in no_data_message and "private-trace-id" not in no_data_message

    for restore_meta in (
        {"result_status": "no_data", "history_restore": True},
        {"result_status": "no_data", "current_table_restore": True},
    ):
        restored_message = _user_facing_chat_message(
            {**no_data_payload, "meta": restore_meta},
            restore_meta,
            no_data_payload["data"],
        )
        assert restored_message == "해당 조회조건의 자료가 없습니다."
        assert "source_call_count" not in restored_message

    router_message = _io_payload_user_message(
        no_data_payload,
        fallback="해당 조회조건의 자료가 없습니다.",
    )
    assert router_message == "해당 조회조건의 자료가 없습니다."

    assert _user_facing_internal_object_message({"chart": {"kind": "bar"}}) is None

    candidate_frame = pd.DataFrame([{"제품코드": "00001", "제품명": "이가탄"}])
    normalized = _normalize_result_for_chat(
        {
            "action": "최종 계약단가 조회",
            "df": candidate_frame,
            "df_display": candidate_frame.copy(),
            "meta": {
                "candidate_table": True,
                "source_call_count": 1,
                "physical_source_call_count": 1,
            },
        }
    )
    assert normalized.get("type") == "table"
    assert isinstance(normalized.get("data"), pd.DataFrame)

    header = _build_sims_result_header_view(
        normalized,
        normalized.get("meta") or {},
        normalized.get("data"),
        title="최종 계약단가 조회",
    )
    details = "\n".join(header.get("details") or [])
    for private_key in (
        "source_call_count",
        "physical_source_call_count",
        "trace_request_id",
        "private_routing_metadata",
        "params",
    ):
        assert private_key not in details


def main() -> int:
    _assert_rank_contract()
    _assert_user_group_and_projection_contract()
    _assert_unlabeled_current_table_and_grain_contract()
    _assert_contract_price_entity_contract()
    _assert_user_facing_metadata_boundary()
    print("[OK] nlq current-table feedback regression")
    print("focused_queries=20 rank_aliases=7")
    print("contracts=ranking/filter/unlabeled-search/product-grain/user-group/user-export/r070-entity/metadata-boundary")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
