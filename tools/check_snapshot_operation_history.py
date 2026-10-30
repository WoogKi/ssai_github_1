"""DB-free contracts for append-only Snapshot operation history."""

from __future__ import annotations

import ast
import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import snapshot_operation_history as history
from app.services.ssai_analytics_snapshot_migration import MIGRATIONS


def check_event_contract() -> None:
    event = history.OperationEvent(
        3, "cleanup", "cleanup_committed", "succeeded",
        run_id="0e5f8d65-e47e-51c8-8fe7-b79a9cd233e2",
        origin="backfill", operation_date=date(2026, 10, 26),
        deleted_manifest_ids=(16, 17, 19, 20), deleted_rows=158553,
        reserved_delta_kib=42624,
        source_report="snapshot_cleanup_3_40_status.md",
    )
    values = history._validated_values(event, "a" * 64)
    assert len(values) == len(history._COLUMNS)
    assert values[history._COLUMNS.index("deleted_rows")] == 158553
    assert values[history._COLUMNS.index("started_at")] is None
    assert values[history._COLUMNS.index("source_report")] == "snapshot_cleanup_3_40_status.md"
    cursor = MagicMock()
    cursor.rowcount = 1
    assert history.insert_event_on_cursor(cursor, event, target_hash="a" * 64)
    sql = cursor.execute.call_args.args[0]
    assert "WITH (UPDLOCK,HOLDLOCK)" in sql
    assert "WHERE event_key=?" in sql
    assert "DELETE" not in sql
    try:
        history._validated_values(history.OperationEvent(3, "cleanup", "done", "succeeded",
                                                         details={"password": "secret"}), "a" * 64)
    except ValueError:
        pass
    else:
        raise AssertionError("sensitive detail key was accepted")


def check_record_transaction() -> None:
    conn = MagicMock()
    conn.cursor.return_value.execute.return_value.fetchval.return_value = 11
    event = history.OperationEvent(40, "snapshot_generate", "draft_committed", "succeeded")
    with patch.object(history, "analytics_target_hash", return_value="a" * 64), \
         patch.object(history, "connect_company_analytics_db", return_value=conn), \
         patch.object(history, "insert_event_on_cursor", return_value=True):
        assert history.record_event(event, require_store=True)
    conn.commit.assert_called_once()
    conn.close.assert_called_once()


def check_schema_and_ui() -> None:
    assert MIGRATIONS[-1].migration_id == "011_snapshot_operation_event"
    sql = MIGRATIONS[-1].sql
    assert "CREATE TABLE snapshot.operation_event" in sql
    assert "UQ_snapshot_operation_event_key" in sql
    assert "GRANT SELECT" in sql and "GRANT SELECT, INSERT" in sql
    root = Path(__file__).resolve().parents[1]
    admin = (root / "app/ui/ssai_admin.py").read_text(encoding="utf-8")
    assert "집계 작업 이력" in admin and "Saved IO 3등급 비교" not in admin
    assert "saved_io_grade_preview" not in admin
    for module in ("app/ui/snapshot_operation_history.py", "app/services/snapshot_operation_history.py"):
        ast.parse((root / module).read_text(encoding="utf-8"))
    assert not (root / "app/ui/ssai_shadow_grade_preview.py").exists()


def check_cleanup_atomicity() -> None:
    from tools import cleanup_snapshot_history as cleanup

    class Cursor:
        rowcount = 0
        sql = ""

        def execute(self, sql: str, *_args: object) -> "Cursor":
            self.sql = sql
            self.rowcount = 1 if sql.startswith("DELETE FROM snapshot.manifest") else 0
            return self

        def fetchone(self) -> tuple[object, ...]:
            if self.sql.startswith("SELECT company_id"):
                return ("3", "superseded", "approved", "a" * 64, 1,
                        "frequency", "202610", "b" * 64, "2.1", "algorithm", "c" * 64)
            return (0,)

        def fetchval(self) -> int:
            return 0

    cursor = Cursor()
    conn = MagicMock()
    conn.cursor.return_value = cursor
    with patch.object(cleanup, "connect_company_analytics_db", return_value=conn), \
         patch.object(cleanup, "analytics_target_hash", return_value="b" * 64), \
         patch.object(cleanup, "insert_event_on_cursor", return_value=True) as insert:
        result = cleanup.cleanup_one(company_id=3, kind="snapshot", manifest_id=16,
                                     expected_checksum="a" * 64, expected_rows=1, apply=True,
                                     expected_generation=1)
    assert result["applied"] and result["deleted_rows"] == 1
    insert.assert_called_once()
    conn.commit.assert_called_once()

    conn = MagicMock()
    conn.cursor.return_value = Cursor()
    with patch.object(cleanup, "connect_company_analytics_db", return_value=conn), \
         patch.object(cleanup, "analytics_target_hash", return_value="b" * 64), \
         patch.object(cleanup, "insert_event_on_cursor", side_effect=RuntimeError("audit unavailable")):
        try:
            cleanup.cleanup_one(company_id=3, kind="snapshot", manifest_id=16,
                                expected_checksum="a" * 64, expected_rows=1, apply=True,
                                expected_generation=1)
        except RuntimeError:
            pass
        else:
            raise AssertionError("cleanup committed without an audit event")
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


def check_published_cleanup_guard() -> None:
    from tools import cleanup_snapshot_history as cleanup
    from types import SimpleNamespace
    from app.services import dashboard_inventory_frequency_snapshot_service as snapshots

    current = ("frequency", "202610", "b" * 64, "2.1", "algorithm", "c" * 64)
    old = (*current[:5], "d" * 64)
    scope = SimpleNamespace(stock_codes=("10001",), product_group_codes=(), product_di_codes=(),
                            product_class_codes=(), io_gu_codes=("001",), stock_mode="real")
    key = SimpleNamespace(snapshot_type=current[0], evaluation_month=current[1],
                          scope_fingerprint=current[2], schema_version=current[3],
                          algorithm_version=current[4], profile_fingerprint=current[5])

    class PublishedCursor:
        def execute(self, _sql: str, *_args: object) -> "PublishedCursor":
            return self

        def fetchall(self) -> list[tuple[str, ...]]:
            return [current]

    with patch.object(snapshots, "resolve_dashboard_profile_stock_scope", return_value=scope), \
         patch.object(snapshots, "build_frequency_snapshot_plan", return_value=object()), \
         patch.object(snapshots, "frequency_snapshot_read_keys", return_value=(key,)):
        assert cleanup._published_snapshot_is_obsolete(PublishedCursor(), 3, old)
        assert not cleanup._published_snapshot_is_obsolete(PublishedCursor(), 3, current)

    class Cursor:
        def execute(self, sql: str, *_args: object) -> "Cursor":
            self.sql = sql
            return self

        def fetchval(self) -> int:
            return 0

        def fetchone(self) -> tuple[object, ...]:
            return ("3", "published", "approved", "a" * 64, 2,
                    "frequency", "202610", "b" * 64, "2.1", "algorithm", "c" * 64)

    conn = MagicMock()
    conn.cursor.return_value = Cursor()
    with patch.object(cleanup, "connect_company_analytics_db", return_value=conn), \
         patch.object(cleanup, "_published_snapshot_is_obsolete", return_value=False):
        try:
            cleanup.cleanup_one(company_id=3, kind="snapshot", manifest_id=16,
                                expected_checksum="a" * 64, expected_rows=1, apply=True,
                                expected_generation=2, allow_published_history=True)
        except ValueError as exc:
            assert "published" in str(exc)
        else:
            raise AssertionError("operating published snapshot could be deleted")
    conn.commit.assert_not_called()
    with patch.object(cleanup, "connect_company_analytics_db", return_value=conn), \
         patch.object(cleanup, "_published_snapshot_is_obsolete", return_value=True):
        result = cleanup.cleanup_one(company_id=3, kind="snapshot", manifest_id=16,
                                     expected_checksum="a" * 64, expected_rows=1,
                                     apply=False, expected_generation=2,
                                     allow_published_history=True)
    assert result["status"] == "published" and not result["applied"]
    conn.commit.assert_not_called()


def check_multi_server_contract() -> None:
    from app.services import snapshot_operation_history_admin as admin

    actor = MagicMock(user_type="SSART_ADMIN", user_grade="SUPER")
    registered = [
        {"company_id": 3, "company_name": "과거 시험", "db_server": "server-a", "db_port": None, "is_active": True},
        {"company_id": 6, "company_name": "회사 6", "db_server": "server-a", "db_port": None, "is_active": False},
        {"company_id": 40, "company_name": "신규 회사", "db_server": "server-b", "db_port": None, "is_active": True},
        {"company_id": 41, "company_name": "상태 미확인", "db_server": "server-b", "db_port": None, "is_active": None},
    ]
    with patch.object(admin, "list_companies", return_value=registered):
        catalog = admin.build_history_catalog(actor)
    assert len(catalog.servers) == 2
    assert catalog.company_servers[3] == catalog.company_servers[6]
    assert catalog.company_servers[3] != catalog.company_servers[40]
    assert catalog.company_names[3] == "과거 시험"
    assert catalog.company_statuses == {3: "활성", 6: "비활성", 40: "활성", 41: "확인 불가"}
    assert next(server for server in catalog.servers if 6 in server.company_ids).anchor_company_id == 3
    conn = MagicMock()
    conn.cursor.return_value.execute.return_value.fetchval.return_value = 11
    keys = tuple(server.key for server in catalog.servers)
    snapshot_rows = [{"company_id": "3", "manifest_id": 24, "generation_no": 1},
                     {"company_id": "40", "manifest_id": 2, "generation_no": 1}]
    def rows(_cursor: object, sql: str, *_args: object) -> list[dict[str, object]]:
        if "FROM snapshot.operation_event" in sql:
            return []
        if "FROM snapshot.manifest" in sql:
            return [row for row in snapshot_rows if str(row["company_id"]) in _args]
        return []
    with patch.object(admin, "connect_company_analytics_db", return_value=conn) as connect, \
         patch.object(admin, "_rows", side_effect=rows):
        result = admin.query_admin_history(actor, catalog, server_keys=keys,
                                           company_ids=None, from_date=date(2026, 10, 25),
                                           to_date=date(2026, 10, 26))
    assert connect.call_count == 2
    assert {int(row["company_id"]): row["manifest_id"] for row in result["snapshots"]} == {3: 24, 40: 2}
    assert admin.to_kst(datetime(2026, 10, 25, 15, 0, 0)).startswith("2026-10-26 00:00")
    assert admin.kst_day_bounds(date(2026, 10, 26), date(2026, 10, 26))[0] == datetime(2026, 10, 25, 15)
    assert admin._registration_status("0") == "비활성"
    assert admin._registration_status(None) == "확인 불가"
    try:
        admin.query_admin_history(MagicMock(user_type="USER", user_grade="NORMAL"), catalog,
                                  server_keys=keys, company_ids=None,
                                  from_date=date(2026, 10, 26), to_date=date(2026, 10, 26))
    except PermissionError:
        pass
    else:
        raise AssertionError("non-admin reached Analytics history")
    try:
        admin.query_admin_history(actor, catalog, server_keys=(keys[0], keys[0]),
                                  company_ids=None, from_date=date(2026, 10, 26),
                                  to_date=date(2026, 10, 26))
    except ValueError:
        pass
    else:
        raise AssertionError("duplicate server query was accepted")
    missing_conn = MagicMock()
    missing_conn.cursor.return_value.execute.return_value.fetchval.return_value = None
    with patch.object(admin, "connect_company_analytics_db", return_value=missing_conn), \
         patch.object(admin, "_rows") as rows:
        missing = admin.query_admin_history(actor, catalog, server_keys=(keys[0],),
                                            company_ids=(3,), from_date=date(2026, 10, 26),
                                            to_date=date(2026, 10, 26))
    assert missing["servers"][0]["status"] == "migration_011_missing"
    rows.assert_not_called()
    with patch.object(admin, "connect_company_analytics_db", return_value=conn) as connect, \
         patch.object(admin, "_rows", return_value=[]):
        admin.query_admin_history(actor, catalog, server_keys=(catalog.company_servers[6],),
                                  company_ids=(6,), from_date=date(2026, 10, 26),
                                  to_date=date(2026, 10, 26))
    assert connect.call_args.args[0] == 3, "inactive company was used as connection anchor"


def check_migration_ledger_contract() -> None:
    from types import SimpleNamespace
    from app.services import snapshot_operation_history_admin as admin

    actor = SimpleNamespace(user_type="SSART_ADMIN", user_grade="SUPER")
    registered = [
        {"company_id": 3, "company_name": "시험", "db_server": "server-a",
         "db_port": None, "is_active": True},
        {"company_id": 6, "company_name": "공유", "db_server": "server-a",
         "db_port": None, "is_active": False},
        {"company_id": 40, "company_name": "별도", "db_server": "server-b",
         "db_port": None, "is_active": True},
    ]
    with patch.object(admin, "list_companies", return_value=registered):
        catalog = admin.build_history_catalog(actor)
    server = next(value for value in catalog.servers if 3 in value.company_ids)
    applied_at = datetime(2026, 10, 30, 15)
    stored = [{"migration_id": MIGRATIONS[-2].migration_id,
               "migration_checksum": MIGRATIONS[-2].checksum,
               "applied_at": applied_at, "applied_by": "operator"},
              {"migration_id": MIGRATIONS[-1].migration_id,
               "migration_checksum": "0" * 64,
               "applied_at": applied_at, "applied_by": "operator"}]
    rows = admin._migration_status_rows(server, (3, 6), stored)
    assert len(rows) == len(MIGRATIONS)
    assert rows[-2]["status"] == "applied" and rows[-1]["status"] == "checksum_mismatch"
    assert rows[0]["status"] == "not_recorded"
    assert rows[-2]["shared_company_ids"] == (3, 6)
    assert admin.to_kst(rows[-2]["applied_at"]).startswith("2026-10-31 00:00")
    assert all(row["status"] == "history_unavailable"
               for row in admin._migration_status_rows(server, (3,), None))

    conn = MagicMock()
    conn.cursor.return_value.execute.return_value.fetchval.side_effect = [1, None]
    with patch.object(admin, "connect_company_analytics_db", return_value=conn) as connect, \
         patch.object(admin, "_rows", return_value=stored) as query:
        result = admin.query_admin_history(actor, catalog, server_keys=(server.key,),
                                           company_ids=(3, 6), from_date=date(2026, 10, 30),
                                           to_date=date(2026, 10, 31))
    assert connect.call_count == 1 and query.call_count == 1
    assert len(result["migrations"]) == len(MIGRATIONS)
    assert result["servers"][0]["status"] == "migration_011_missing"
    assert result["migrations"][-1]["status"] == "checksum_mismatch"

    conn = MagicMock()
    conn.cursor.return_value.execute.return_value.fetchval.side_effect = [None, None]
    with patch.object(admin, "connect_company_analytics_db", return_value=conn), \
         patch.object(admin, "_rows") as query:
        missing = admin.query_admin_history(actor, catalog, server_keys=(server.key,),
                                            company_ids=(3,), from_date=date(2026, 10, 30),
                                            to_date=date(2026, 10, 31))
    query.assert_not_called()
    assert all(row["status"] == "history_unavailable" for row in missing["migrations"])

    with patch.object(admin, "connect_company_analytics_db", side_effect=ConnectionError("offline")):
        failed = admin.query_admin_history(actor, catalog, server_keys=(server.key,),
                                           company_ids=(3,), from_date=date(2026, 10, 30),
                                           to_date=date(2026, 10, 31))
    assert failed["servers"][0]["status"] == "connection_or_query_failed"
    assert failed["migrations"] == []
    try:
        admin.query_admin_history(SimpleNamespace(user_type="USER", user_grade="NORMAL"), catalog,
                                  server_keys=(server.key,), company_ids=(3,),
                                  from_date=date(2026, 10, 30), to_date=date(2026, 10, 31))
    except PermissionError:
        pass
    else:
        raise AssertionError("unauthorized Migration history query was allowed")


def check_admin_render() -> None:
    from app.ui import snapshot_operation_history as view
    from app.services.snapshot_operation_history_admin import HistoryCatalog, HistoryServer

    assert view._detail_value(date(2026, 10, 26)) == "2026-10-26"
    event = {key: None for key in (
        "event_id", "company_id", "analytics_target_hash", "run_id", "operation_kind",
        "stage", "result_status", "origin", "event_at", "operation_date",
        "started_at", "finished_at", "evaluation_month", "basis_month",
        "manifest_id", "generation_no", "profile_fingerprint", "product_count",
        "fact_count", "source_row_count", "exact_status", "whole_checksum",
        "source_ms", "calculation_ms", "storage_ms", "total_ms", "approved_by",
        "approval_status", "deleted_manifest_ids", "deleted_rows",
        "reserved_delta_kib", "error_code", "source_report", "details_json",
    )}
    event.update(event_id=1, company_id="3", operation_kind="cleanup",
                 stage="cleanup_committed", result_status="succeeded",
                 origin="backfill", deleted_rows=158553, deleted_manifest_ids="[16,17,19,20]",
                 manifest_id=24, generation_no=1, server_label="ERP 서버 1")
    report = dict(view._history_report(event, "회사3"))
    assert report["삭제 Snapshot Manifest"] == "16, 17, 19, 20"
    assert report["보존 Snapshot"] == "24/1"
    assert "succeeded" not in " ".join(report.values())
    catalog = HistoryCatalog((HistoryServer("a", "ERP 서버 1", 3, (3, 40)),),
                             {3: "회사3", 40: "회사40"}, {3: "a", 40: "a"}, ())
    mock_st = MagicMock()
    mock_st.columns.side_effect = lambda n: [MagicMock() for _ in range(n if isinstance(n, int) else len(n))]
    mock_st.selectbox.side_effect = lambda label, options, **_kw: event if label == "상세 작업" else options[0]
    mock_st.date_input.side_effect = lambda _label, value: value
    mock_st.text_input.return_value = ""
    mock_st.form.return_value.__enter__.return_value = mock_st
    mock_st.form_submit_button.return_value = True
    result = {"events": [event], "snapshots": [
        {"company_id": "3", "manifest_id": 24, "generation_no": 1},
        {"company_id": "40", "manifest_id": 2, "generation_no": 1}],
        "monthly": [], "servers": [{"server": "ERP 서버 1", "status": "ready",
                                     "elapsed_ms": 10, "event_count": 1}],
        "migrations": [{"server_label": "ERP 서버 1", "selected_company_ids": (3,),
                         "shared_company_ids": (3, 40), "migration_id": MIGRATIONS[-1].migration_id,
                         "status": "applied", "applied_at": datetime(2026, 10, 30, 15),
                         "applied_by": "operator", "stored_checksum": MIGRATIONS[-1].checksum,
                         "code_checksum": MIGRATIONS[-1].checksum}],
        "company_names": catalog.company_names}
    with patch.object(view, "st", mock_st), \
         patch.object(view, "require_history_admin"), \
         patch.object(view, "get_current_user", return_value=object()), \
         patch.object(view, "get_selected_company", return_value={"company_id": 3}), \
         patch.object(view, "build_history_catalog", return_value=catalog), \
         patch.object(view, "query_admin_history", return_value=result) as query:
        view.render_snapshot_operation_history_page()
    assert query.call_count == 1
    assert query.call_args.kwargs["server_keys"] == ("a",)
    assert query.call_args.kwargs["company_ids"] is None
    frames = [call.args[0] for call in mock_st.dataframe.call_args_list]
    assert any("24/1" in frame.to_string() and "2/1" in frame.to_string() for frame in frames)
    migration_frames = [frame for frame in frames if "Migration ID" in frame.columns]
    assert len(migration_frames) == 1
    assert migration_frames[0]["적용 상태"].iloc[0] == "적용·코드 일치"
    assert migration_frames[0]["서버 공유 회사"].iloc[0] == "3, 40"
    assert migration_frames[0]["적용 일시 (KST)"].iloc[0].startswith("2026-10-31 00:00")
    mock_st.json.assert_called_once()
    denied_st = MagicMock()
    with patch.object(view, "st", denied_st), patch.object(view, "get_current_user", return_value=None), \
         patch.object(view, "query_admin_history") as query:
        view.render_snapshot_operation_history_page()
    denied_st.error.assert_called_once()
    query.assert_not_called()
    no_company_st = MagicMock()
    no_company_st.columns.side_effect = mock_st.columns.side_effect
    no_company_st.form.return_value.__enter__.return_value = no_company_st
    no_company_st.form_submit_button.return_value = True
    no_company_st.selectbox.side_effect = lambda label, options, **_kw: (
        "현재 서버" if label == "ERP 서버" else options[0])
    no_company_st.date_input.side_effect = lambda _label, value: value
    no_company_st.text_input.return_value = ""
    with patch.object(view, "st", no_company_st), patch.object(view, "require_history_admin"), \
         patch.object(view, "get_current_user", return_value=object()), \
         patch.object(view, "get_selected_company", return_value=None), \
         patch.object(view, "build_history_catalog", return_value=catalog), \
         patch.object(view, "query_admin_history") as query:
        view.render_snapshot_operation_history_page()
    query.assert_not_called()
    no_company_st.warning.assert_called()
    mock_st.selectbox.side_effect = lambda label, options, **_kw: (
        "다중" if label == "회사 범위" else event if label == "상세 작업" else options[0])
    mock_st.multiselect.return_value = [3, 40]
    with patch.object(view, "st", mock_st), patch.object(view, "require_history_admin"), \
         patch.object(view, "get_current_user", return_value=object()), \
         patch.object(view, "get_selected_company", return_value={"company_id": 3}), \
         patch.object(view, "build_history_catalog", return_value=catalog), \
         patch.object(view, "query_admin_history", return_value=result) as query:
        view.render_snapshot_operation_history_page()
    assert query.call_args.kwargs["company_ids"] == (3, 40)


def check_detail_rerun_contract() -> None:
    from types import SimpleNamespace
    from app.ui import snapshot_operation_history as view
    from app.services.snapshot_operation_history_admin import HistoryCatalog, HistoryServer

    actor = SimpleNamespace(user_id=1, login_id="admin", user_type="SSART_ADMIN",
                            user_grade="SUPER", approval_status="APPROVED", is_active=True)
    catalog = HistoryCatalog((HistoryServer("a", "ERP 서버 1", 3, (3, 40)),),
                             {3: "회사3", 40: "회사40"}, {3: "a", 40: "a"}, ())
    events = [dict(event_id=i, company_id="3" if i == 1 else "40", operation_kind="cleanup",
                   stage="cleanup_committed", result_status="succeeded", origin="backfill",
                   event_at=datetime(2026, 10, 26, 9, i), manifest_id=24 if i == 1 else 2,
                   generation_no=1, deleted_manifest_ids="[16,17]" if i == 1 else "[1]",
                   details_json=("{}" if i == 1 else
                                 '{"deleted_snapshot_ids":[1],"deleted_monthly_ids":[1,2,3,4,5,6,7]}'),
                   deleted_rows=10, total_ms=None, fact_count=None,
                   product_count=None, server_label="ERP 서버 1") for i in (1, 2)]
    result = {"events": events,
              "snapshots": [{"company_id": "3", "manifest_id": 24, "generation_no": 1},
                            {"company_id": "40", "manifest_id": 2, "generation_no": 1}],
              "monthly": [], "servers": [{"server": "ERP 서버 1", "status": "ready",
                                          "elapsed_ms": 10, "event_count": 2},
                                         {"server": "ERP 서버 2", "status": "migration_011_missing",
                                          "elapsed_ms": 10, "event_count": 0},
                                         {"server": "ERP 서버 3", "status": "connection_or_query_failed",
                                          "error_type": "PermissionError", "elapsed_ms": 10,
                                          "event_count": 0}],
              "company_names": catalog.company_names}
    mock_st = MagicMock()
    mock_st.session_state = {}
    mock_st.columns.side_effect = lambda n: [MagicMock() for _ in range(n if isinstance(n, int) else len(n))]
    mock_st.form.return_value.__enter__.return_value = mock_st
    mock_st.date_input.side_effect = lambda _label, value: value
    mock_st.text_input.return_value = ""
    selected_event = [events[0]]
    mock_st.selectbox.side_effect = lambda label, options, **_kw: (
        selected_event[0] if label == "상세 작업" else options[0])
    mock_st.form_submit_button.side_effect = [True, False, False, False, True, False, True, False]
    current_company = [{"company_id": 3}]
    with patch.object(view, "st", mock_st), \
         patch.object(view, "get_current_user", return_value=actor) as user, \
         patch.object(view, "get_selected_company", side_effect=lambda: current_company[0]), \
         patch.object(view, "build_history_catalog", return_value=catalog) as catalog_read, \
         patch.object(view, "query_admin_history", return_value=result) as query:
        view.render_snapshot_operation_history_page()
        assert any("조회 완료 1개 서버 / Migration 미적용 1개 / 활성 연결 기준 없음 0개 / 연결·권한 오류 1개"
                   in call.args[0] for call in mock_st.warning.call_args_list)
        assert any(call.args[0] == "서버별 상세 상태" for call in mock_st.expander.call_args_list)
        frames = [call.args[0] for call in mock_st.dataframe.call_args_list]
        cleanup_frames = [frame for frame in frames if "삭제 Snapshot Manifest" in frame.columns]
        assert len(cleanup_frames) == 1
        cleanup_frame = cleanup_frames[0]
        assert cleanup_frame["삭제 Snapshot Manifest"].tolist() == ["16, 17", "1"]
        assert cleanup_frame["삭제 월별 Manifest"].tolist() == ["없음", "1, 2, 3, 4, 5, 6, 7"]
        assert cleanup_frame["현재 보존 Snapshot"].tolist() == ["24/1", "2/1"]
        selected_event[0] = events[1]
        view.render_snapshot_operation_history_page()
        selected_event[0] = events[0]
        view.render_snapshot_operation_history_page()
        assert query.call_count == 1, "detail selection queried Analytics again"
        assert mock_st.json.call_count == 3, "detail or results vanished on rerun"
        current_company[0] = {"company_id": 40}
        view.render_snapshot_operation_history_page()
        assert mock_st.json.call_count == 3, "old company history remained visible"
        view.render_snapshot_operation_history_page()
        assert query.call_count == 2, "new search did not refresh results"
        user.return_value = SimpleNamespace(user_id=2, login_id="other", user_type="SSART_ADMIN",
                                            user_grade="SUPER", approval_status="APPROVED", is_active=True)
        view.render_snapshot_operation_history_page()
        assert mock_st.json.call_count == 4, "prior user's history remained visible"
        assert query.call_count == 2
        view.render_snapshot_operation_history_page()
        assert query.call_count == 3
        catalog_read.return_value = HistoryCatalog(
            (HistoryServer("b", "ERP 서버 2", 40, (3, 40)),), catalog.company_names,
            {3: "b", 40: "b"}, ())
        view.render_snapshot_operation_history_page()
        assert mock_st.json.call_count == 5, "prior server history remained visible"
        user.return_value = SimpleNamespace(user_id=3, login_id="reader", user_type="SSART_USER",
                                            user_grade="NORMAL")
        view.render_snapshot_operation_history_page()
        assert view._RESULT_KEY not in mock_st.session_state
        assert query.call_count == 3


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-company", type=int, choices=(3, 40))
    parser.add_argument("--live-admin", action="store_true")
    parser.add_argument("--live-all-admin", action="store_true")
    args = parser.parse_args()
    if not (args.live_company or args.live_admin or args.live_all_admin):
        connection_guard = patch("pyodbc.connect", side_effect=AssertionError("DB access in DB-free gate"))
        connection_guard.start()
    else:
        connection_guard = None
    try:
        check_event_contract()
        check_record_transaction()
        check_schema_and_ui()
        check_cleanup_atomicity()
        check_published_cleanup_guard()
        check_multi_server_contract()
        check_migration_ledger_contract()
        check_admin_render()
        check_detail_rerun_contract()
    finally:
        if connection_guard is not None:
            connection_guard.stop()
    print("snapshot_operation_history: 9/9 PASS")
    if args.live_all_admin:
        from types import SimpleNamespace
        from app.services import snapshot_operation_history_admin as admin

        actor = SimpleNamespace(user_type="SSART_ADMIN", user_grade="SUPER")
        catalog = admin.build_history_catalog(actor)
        result = admin.query_admin_history(
            actor, catalog, server_keys=tuple(server.key for server in catalog.servers),
            company_ids=None, from_date=date(2026, 10, 25), to_date=date(2026, 10, 26),
            kind="cleanup", per_server_limit=100,
        )
        counts = {status: sum(row["status"] == status for row in result["servers"])
                  for status in ("ready", "migration_011_missing", "no_active_anchor",
                                 "connection_or_query_failed")}
        cleanup_counts = {company: sum(int(row["company_id"]) == company
                                       and row["operation_kind"] == "cleanup"
                                       for row in result["events"])
                          for company in (3, 40)}
        snapshots = {int(row["company_id"]): (int(row["manifest_id"]), int(row["generation_no"]))
                     for row in result["snapshots"]}
        print(f"snapshot_operation_history all-server read: servers={len(result['servers'])} "
              f"statuses={counts} cleanup_counts={cleanup_counts} snapshot3={snapshots.get(3)} "
              f"snapshot40={snapshots.get(40)}")
        assert cleanup_counts[3] == 1 and cleanup_counts[40] == 1
        assert snapshots.get(3) == (24, 1) and snapshots.get(40) == (2, 1)
    if args.live_admin:
        from types import SimpleNamespace
        from app.services import snapshot_operation_history_admin as admin

        actor = SimpleNamespace(user_type="SSART_ADMIN", user_grade="SUPER")
        catalog = admin.build_history_catalog(actor)
        keys = tuple(dict.fromkeys(catalog.company_servers[company] for company in (3, 40)))
        result = admin.query_admin_history(actor, catalog, server_keys=keys,
                                           company_ids=(3, 40), from_date=date.today() - timedelta(days=2),
                                           to_date=date.today())
        assert all(item["status"] == "ready" for item in result["servers"]), [
            (item["status"], item.get("error_type")) for item in result["servers"]]
        assert {int(row["company_id"]): (int(row["manifest_id"]), int(row["generation_no"]))
                for row in result["snapshots"]} == {3: (24, 1), 40: (2, 1)}
        assert {int(row["company_id"]) for row in result["events"]} <= {3, 40}
        print(f"snapshot_operation_history live admin: servers={len(keys)} events={len(result['events'])} "
              "snapshot3=24/1 snapshot40=2/1 PASS")
    if args.live_company:
        from app.services.sql_server_snapshot_repository import SqlServerSnapshotRepository
        from app.services.ssai_analytics_target_resolver import connect_company_analytics_db
        from app.services.ssai_snapshot_repository import SnapshotKey

        company = args.live_company
        summary = history.read_history_summary(company, company)
        events = history.query_events(company, company_id=company,
                                      from_date=date.today() - timedelta(days=2),
                                      to_date=date.today(), kind="cleanup",
                                      status="succeeded", limit=50)
        assert summary["snapshot"][0] == (24 if company == 3 else 2)
        assert summary["monthly"][3] is not None
        assert len([row for row in events if row["origin"] == "backfill"]) == 1
        assert company in history.list_history_company_ids(company)
        assert not history.query_events(company, company_id=company,
                                        from_date=date(2020, 1, 1), to_date=date(2020, 1, 31),
                                        kind="cleanup", limit=50)
        assert not history.query_events(company, company_id=company,
                                        from_date=date.today() - timedelta(days=2),
                                        to_date=date.today(), kind="inspect", limit=50)
        assert not history.query_events(company, company_id=company,
                                        from_date=date.today() - timedelta(days=2),
                                        to_date=date.today(), kind="cleanup",
                                        evaluation_month="202611", limit=50)
        connection = connect_company_analytics_db(company, "reader", timeout=30)
        try:
            manifest = connection.cursor().execute(
                "SELECT snapshot_type,evaluation_month,scope_fingerprint,schema_version,"
                "algorithm_version,profile_fingerprint,checksum,manifest_id,generation_no "
                "FROM snapshot.manifest WHERE company_id=? AND status='published' "
                "AND approval_status='approved' ORDER BY published_at DESC,manifest_id DESC",
                str(company),
            ).fetchone()
        finally:
            connection.close()
        assert manifest is not None
        key = SnapshotKey(str(company), *(str(item or "") for item in manifest[:6]))
        repository = SqlServerSnapshotRepository(
            reader_connection_factory=lambda: connect_company_analytics_db(company, "reader", timeout=30),
            writer_connection_factory=lambda: (_ for _ in ()).throw(AssertionError("reader gate cannot write")),
        )
        read = repository.read(key)
        assert read.status == "ready" and read.manifest_id == int(manifest[7])
        assert read.generation_no == int(manifest[8]) and read.checksum.lower() == str(manifest[6]).lower()
        print(f"snapshot_operation_history live company={company}: summary/filter/backfill/reader PASS")


if __name__ == "__main__":
    main()
