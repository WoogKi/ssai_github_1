from __future__ import annotations

import copy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot import (  # noqa: E402
    SnapshotContractError,
    build_relational_frequency_projection,
    build_relational_frequency_snapshot_from_aggregates,
    relational_row_checksum,
    validate_relational_frequency_projection,
    validate_relational_frequency_snapshot,
)
from app.services.sql_server_snapshot_repository import _contract_checksum_values  # noqa: E402
from app.services.ssai_auth_service import CompanyDbConfig  # noqa: E402
from app.services import ssai_analytics_target_resolver as resolver  # noqa: E402


def _diagnostics() -> dict[str, int]:
    return {
        "diagnostic_contract_version": 2,
        "source_row_count": 2,
        "normal_positive_accepted_row_count": 2,
        "normal_positive_duplicate_row_count": 0,
        "normal_positive_conflicting_row_count": 0,
        "normal_positive_missing_key_row_count": 0,
        "normal_positive_nonintegral_row_count": 0,
        "normal_nonpositive_row_count": 0,
        "return_positive_row_count": 0,
        "return_nonpositive_row_count": 0,
        "other_tcode_row_count": 0,
        "normal_positive_row_count": 2,
        "distinct_normal_event_count": 2,
        "conflicting_event_count": 0,
    }


def _snapshot():
    return build_relational_frequency_snapshot_from_aggregates(
        company_id=6,
        evaluation_month="202608",
        stock_codes=["00001", "00008", "00013"],
        product_codes=["A1", "B1", "X1"],
        monthly_rows=[
            {"month": "202605", "product_code": "A1", "stock_code": "00001", "occurrence_count": 4, "outbound_quantity": 10, "outbound_day_count": 2},
            {"month": "202606", "product_code": "B1", "stock_code": "00008", "occurrence_count": 1, "outbound_quantity": 1, "outbound_day_count": 1},
        ],
        source_diagnostics=_diagnostics(),
    )


def _must_fail(action, label: str) -> None:
    try:
        action()
    except SnapshotContractError:
        return
    raise AssertionError(f"{label} did not fail closed")


class _Cursor:
    def __init__(self, row):
        self._row = row

    def execute(self, _sql):
        return self

    def fetchone(self):
        return self._row

    def close(self):
        return None


class _Connection:
    def __init__(self, row):
        self._row = row
        self.timeout = 0
        self.closed = False

    def cursor(self):
        return _Cursor(self._row)

    def close(self):
        self.closed = True


def main() -> int:
    snapshot = _snapshot()
    validate_relational_frequency_snapshot(snapshot)
    assert snapshot.checksum == _snapshot().checksum
    rows, headers = build_relational_frequency_projection(snapshot)
    validate_relational_frequency_projection(rows=rows, headers=headers, require_complete=True)

    contract_columns = ("returns_are_netted", "include_rd04_del_flag_e")
    writer_values = (1, 0)
    reader_values = (True, False)
    expected_contract_checksum = relational_row_checksum(
        "frequency_source_contract", contract_columns, writer_values
    )
    assert relational_row_checksum(
        "frequency_source_contract", contract_columns, _contract_checksum_values(reader_values)
    ) == expected_contract_checksum

    corrupted = copy.deepcopy(rows)
    corrupted[0]["occurrence_count_3m"] += 1
    _must_fail(lambda: validate_relational_frequency_projection(rows=corrupted, headers=headers, require_complete=True), "row corruption")
    _must_fail(lambda: validate_relational_frequency_projection(rows=rows[:-1], headers=headers, require_complete=True), "partial projection")
    bad_headers = copy.deepcopy(headers)
    bad_headers[0]["expected_product_count"] += 1
    _must_fail(lambda: validate_relational_frequency_projection(rows=rows, headers=bad_headers, require_complete=True), "header corruption")

    original_cfg = resolver.get_company_db_config
    original_connect = resolver.pyodbc.connect
    try:
        resolver.get_company_db_config = lambda company_id: CompanyDbConfig(int(company_id), "C6", "fixture", "ERP", "ASP145-SVR", None, "erp", "erp_user", "erp_password", "ODBC Driver 18 for SQL Server")
        assert resolver.resolve_analytics_target(6, "reader").target_id == "company-db-server:asp145-svr"
        assert resolver.resolve_analytics_target(66, "writer").target_id == "company-db-server:asp145-svr"
        good_connection = _Connection(("SSAI_ANALYTICS", "ASP145-SVR", "ASP145-SVR"))
        resolver.pyodbc.connect = lambda *_args, **_kwargs: good_connection
        assert resolver.connect_company_analytics_db(6, "reader") is good_connection
        assert good_connection.timeout == 10
        bad_connection = _Connection(("OTHER_DATABASE", "ASP145-SVR", "ASP145-SVR"))
        resolver.pyodbc.connect = lambda *_args, **_kwargs: bad_connection
        try:
            resolver.connect_company_analytics_db(6, "reader")
        except resolver.AnalyticsTargetResolutionError:
            assert bad_connection.closed
        else:
            raise AssertionError("different analytics database must fail closed")
        resolver.get_company_db_config = lambda company_id: CompanyDbConfig(int(company_id), "C6", "fixture", "ERP", "", None, "erp", "erp_user", "erp_password", "ODBC Driver 18 for SQL Server")
        try:
            resolver.resolve_analytics_target(6, "reader")
        except resolver.AnalyticsTargetResolutionError:
            pass
        else:
            raise AssertionError("missing target must fail closed")
    finally:
        resolver.get_company_db_config = original_cfg
        resolver.pyodbc.connect = original_connect
    print("PASS snapshot relational authority offline gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
