"""Offline product-information SIMS manager and export contract."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import snapshot_product_information_service as service
from app.services.product_master_filter_contract import build_product_master_enrichment_sql
from app.ui.chat_middleware import _make_table_downloads
from app.ui.current_table_followups.generic import _find_common_group_column
from tools.check_product_information_visibility_contract import _scope, _projection, MANAGEMENT


def main() -> int:
    plain_joins, _ = build_product_master_enrichment_sql(product_alias="P", include_audit_price=False)
    assert "POV.Rd03_Ven_Cd" not in plain_joins
    manager_joins, expressions = build_product_master_enrichment_sql(
        product_alias="P", include_audit_price=False, include_vendor_managers=True,
    )
    assert "P.Rd04_Orven_Cd = POV.Rd03_Ven_Cd" in manager_joins
    assert "PV.Rd03_Sales_Man = PMS.Rd06_User_Cd" in manager_joins
    assert "POV.Rd03_Sales_Man = POS.Rd06_User_Cd" in manager_joins
    assert expressions["maker_manager_nm"] == "PMS.Rd06_User_Nm"
    assert expressions["order_vendor_manager_nm"] == "POS.Rd06_User_Nm"
    assert "Rd03_Take_Nm" not in manager_joins

    queries = []
    rows = pd.DataFrame([
        {"제품코드": "00001", "제품명": "one", "제약사": "same", "규격": "10T",
         "제약사 담당자": "김", "발주처 담당자": "김"},
        {"제품코드": "00002", "제품명": "two", "제약사": "different", "규격": "20T",
         "제약사 담당자": "이", "발주처 담당자": "박"},
        {"제품코드": "00003", "제품명": "three", "제약사": "missing", "규격": "30T",
         "제약사 담당자": "", "발주처 담당자": ""},
    ])
    driver = SimpleNamespace(timeout=0)
    connection = SimpleNamespace(connection=SimpleNamespace(driver_connection=driver))

    @contextmanager
    def fake_connection():
        yield connection

    def fake_read_sql(sql, *, con, params):
        queries.append((sql, params))
        assert con is connection and driver.timeout == 30
        return rows.copy()

    with (
        patch("app.db.mssql_client.get_conn", fake_connection),
        patch("pandas.read_sql", fake_read_sql),
    ):
        master = service.load_product_information_master({})
    assert driver.timeout == 0 and len(queries) == 1
    sql, params = queries[0]
    assert not params and "LEFT JOIN dbo.Rddbc030 AS POV" in sql
    assert "PMS.Rd06_User_Nm" in sql and "POS.Rd06_User_Nm" in sql
    assert "Rd03_Take_Nm" not in sql
    assert master["제품코드"].is_unique

    with patch.object(service, "get_current_company_id", return_value=7):
        result = service.get_snapshot_product_information_result(
            {"company_id": 7}, profile_resolver=_scope, projection_reader=_projection,
            master_loader=lambda _params: master, viewer_user=MANAGEMENT,
        )
    assert result["meta"]["result_status"] == "success"
    assert result["meta"]["source_call_count"] == 1
    assert result["meta"]["snapshot_read_call_count"] == 1
    assert len(result["df"]) == len(result["df_display"]) == 3
    assert result["df"]["제품코드"].is_unique
    columns = list(result["df"].columns)
    grade = columns.index("품목기여등급")
    assert columns[grade + 1:grade + 3] == ["제약사 담당자", "발주처 담당자"]
    assert list(result["df_display"].columns) == columns == result["columns"]
    assert result["df"]["제약사 담당자"].tolist() == ["김", "이", ""]
    assert result["df"]["발주처 담당자"].tolist() == ["김", "박", ""]
    assert _find_common_group_column(result["df"], "현재표 제약사 담당자별 집계", "제품정보 조회") == "제약사 담당자"
    assert _find_common_group_column(result["df"], "현재표 발주처 담당자별 집계", "제품정보 조회") == "발주처 담당자"
    csv_buf, xlsx_buf = _make_table_downloads(result["df"])
    assert list(pd.read_csv(csv_buf, nrows=0).columns) == columns
    assert list(pd.read_excel(xlsx_buf, nrows=0).columns) == columns
    print("PASS product-information manager joins, one ERP source, row grain, display/CSV/Excel order")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
