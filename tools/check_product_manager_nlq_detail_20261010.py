"""Offline staff-filter and empty-detail provenance regression."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.io_nlq import resolve_io_nlq
from app.services.product_master_filter_contract import extract_product_manager_filters
from app.services import snapshot_product_information_service as info
from app.sims.nlq import nlq_goods
from app.sims.views import goods
from app.ui.chat_middleware import _normalize_result_for_chat


def main() -> int:
    cases = (
        ("제품정보 조회 발주담당자 가상하나", "order_vendor_manager_nm", "가상하나"),
        ("제품정보 조회 발주처담당자 가상하나", "order_vendor_manager_nm", "가상하나"),
        ("제품정보 조회 발주처 담당자 가상하나", "order_vendor_manager_nm", "가상하나"),
        ("제품정보 조회 제약담당자 가상두나", "maker_manager_nm", "가상두나"),
        ("제품정보 조회 제약사담당자 가상두나", "maker_manager_nm", "가상두나"),
        ("제품정보 조회 제약사 가상제약", "maker_nm", "가상제약"),
        ("제품정보 조회 발주처 가상제약", "order_nm", "가상제약"),
    )
    for question, key, value in cases:
        result = resolve_io_nlq(question)
        assert result["action"] == "제품정보 조회", question
        params = result["params"]
        assert params[key] == value, (question, params)
        assert not any(params.get(other) for other in (
            "maker_nm", "order_nm", "maker_manager_nm", "order_vendor_manager_nm",
        ) if other != key), (question, params)

    missing_name = resolve_io_nlq("제품정보 조회 발주담당자")["params"]
    assert missing_name["_product_manager_condition_invalid"] == "1"
    with patch.object(info, "_company_id", return_value=4):
        blocked = info.get_snapshot_product_information_result(
            missing_name,
            profile_resolver=MagicMock(side_effect=AssertionError("unexpected Snapshot read")),
            master_loader=MagicMock(side_effect=AssertionError("unexpected ERP read")),
        )
    assert blocked["meta"]["result_status"] == "input_required"
    assert blocked["meta"]["source_call_count"] == 0
    assert blocked["meta"]["snapshot_read_call_count"] == 0

    driver = SimpleNamespace(timeout=0)
    conn = SimpleNamespace(connection=SimpleNamespace(driver_connection=driver))
    captures = []

    @contextmanager
    def fake_conn():
        yield conn

    def fake_read_sql(sql, *, con, params):
        assert con is conn and driver.timeout == 30
        captures.append((sql, tuple(params)))
        return pd.DataFrame(columns=["제품코드", "제품명"])

    with patch("app.db.mssql_client.get_conn", fake_conn), patch("pandas.read_sql", fake_read_sql):
        for question, key, value in cases:
            info.load_product_information_master(resolve_io_nlq(question)["params"])
            sql, binds = captures[-1]
            expression = {
                "order_vendor_manager_nm": "POS.Rd06_User_Nm",
                "maker_manager_nm": "PMS.Rd06_User_Nm",
                "maker_nm": "PV.Rd03_Ven_Nm",
                "order_nm": "POV.Rd03_Ven_Nm",
            }[key]
            assert f"{expression} LIKE ?" in sql, question
            assert binds.count(f"%{value}%") == 1, (question, binds)
    assert len(captures) == len(cases) and driver.timeout == 0

    filters, residual = extract_product_manager_filters("제품코드 조회 발주처담당자 가상하나")
    assert filters == {"order_vendor_manager_nm": "가상하나"}
    assert residual == "제품코드 조회"
    called = []
    frame = pd.DataFrame({"제품코드": ["00001"], "발주처 담당자": ["가상하나"]})

    def fake_search(**kwargs):
        called.append(kwargs)
        return frame.copy()

    with (
        patch.object(nlq_goods, "search_goods_full", side_effect=fake_search),
        patch.object(nlq_goods, "apply_labels", side_effect=lambda df, _table: df),
        patch.object(nlq_goods, "_build_goods_display_df", side_effect=lambda df, **_kwargs: df),
        patch.object(nlq_goods, "_push_goods_result", return_value=True),
    ):
        for question in (
            "제품코드 조회 발주담당자 가상하나",
            "제품코드 조회 발주처담당자 가상하나",
            "제품코드 조회 발주처 담당자 가상하나",
            "제품코드 조회 제약담당자 가상두나",
            "제품코드 조회 제약사담당자 가상두나",
        ):
            assert nlq_goods.try_handle_goods_nlq(
                question, room={}, session_state={},
                make_ts=lambda: "", next_seq=lambda: 1, logger=MagicMock(),
            )
    assert len(called) == 5
    for args in called[:3]:
        assert args["order_vendor_manager_nm_kw"] == "가상하나"
        assert not args["ven_nm_kw"] and not args["keyword"]
    for args in called[3:]:
        assert args["maker_manager_nm_kw"] == "가상두나"
        assert not args["ven_nm_kw"] and not args["keyword"]

    fake_st = MagicMock()
    fake_st.form.return_value.__enter__.return_value = None
    fake_st.text_input.return_value = "NOT_REGISTERED"
    fake_st.form_submit_button.return_value = True
    with patch.object(goods, "st", fake_st), patch.object(goods, "get_goods_detail_full", return_value=pd.DataFrame()):
        detail = goods.view_goods_detail()
    normalized = _normalize_result_for_chat(detail)
    assert detail["meta"]["result_status"] == "no_data"
    assert normalized["type"] != "table" and "df" not in normalized
    assert "table_key" not in normalized.get("meta", {})
    print("PASS 7 product-information conditions, bound SQL, product-master staff route, empty-detail provenance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
