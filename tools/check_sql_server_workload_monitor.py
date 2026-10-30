"""DB-free gate for bounded DB3 DMV sampling and extraction performance output."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import date
from pathlib import Path
from threading import Event
from time import process_time
from types import SimpleNamespace
from unittest.mock import patch

from app.services.sql_server_workload_monitor import (
    MEMORY_SQL, REQUEST_SQL, TEMPDB_SQL, SqlServerWorkloadMonitor, worker_process_metrics,
)
from tools.extract_ml_bulk_db3 import ProgressReporter, main as extract_main, run as extract_run


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.description = []
        self.row = None

    def execute(self, sql: str, *params: object) -> FakeCursor:
        self.connection.calls.append((sql, params))
        if sql == REQUEST_SQL:
            if params != (73,):
                raise AssertionError("request scope changed")
            self.connection.request_calls += 1
            values = (10 * self.connection.request_calls, 100 * self.connection.request_calls,
                      5 * self.connection.request_calls, 3, 30, 2, 20, 4, "LCK_M_S", 81)
            names = ("session_cpu_ms", "session_logical_reads", "session_reads", "request_cpu_ms",
                     "request_logical_reads", "request_reads", "request_elapsed_ms", "request_wait_ms",
                     "wait_type", "blocking_session_id")
        elif sql == TEMPDB_SQL:
            if params != (73,):
                raise AssertionError("tempdb scope changed")
            if self.connection.deny_tempdb:
                raise PermissionError("VIEW SERVER PERFORMANCE STATE denied")
            names, values = ("completed_task_tempdb_net_kb",), (32,)
        elif sql == MEMORY_SQL:
            if params:
                raise AssertionError("memory DMV takes no parameters")
            names = ("sql_process_memory_kb", "server_total_memory_kb", "server_available_memory_kb")
            values = (4096, 8192, 2048)
        else:
            raise AssertionError("unexpected SQL")
        self.description = [(name,) for name in names]
        self.row = values
        return self

    def fetchone(self) -> tuple[object, ...] | None:
        return self.row

    def close(self) -> None:
        pass


class FakeConnection:
    def __init__(self, *, deny_tempdb: bool = False) -> None:
        self.deny_tempdb = deny_tempdb
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.request_calls = 0
        self.timeout = None
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True


class WorkloadMonitorGate(unittest.TestCase):
    def test_bounded_session_samples_and_shutdown(self) -> None:
        connection = FakeConnection()
        samples = []
        monitor = SqlServerWorkloadMonitor(lambda: connection, session_id=73, interval_s=.01,
                                           max_samples=2, on_sample=samples.append)
        monitor.start()
        monitor.thread.join(timeout=1)
        monitor.stop()
        result = monitor.summary()
        self.assertFalse(monitor.thread.is_alive())
        self.assertTrue(connection.closed)
        self.assertEqual(connection.timeout, 5)
        self.assertEqual((result["sample_count"], result["monitor_select_attempts"]), (2, 6))
        self.assertEqual((result["session_cpu_ms_sample_delta"],
                          result["session_logical_reads_sample_delta"],
                          result["session_reads_sample_delta"]), (10, 100, 5))
        self.assertTrue(result["blocking_observed"])
        self.assertEqual(result["max_completed_task_tempdb_net_kb"], 32)
        self.assertEqual(result["last_server_memory_kb"]["sql_process_memory_kb"], 4096)
        self.assertEqual(len(samples), 2)
        self.assertEqual(set(sql for sql, _ in connection.calls), {REQUEST_SQL, TEMPDB_SQL, MEMORY_SQL})
        self.assertEqual(result["unavailable"]["server_cpu"], "no_supported_low_cost_metric")

    def test_permission_failure_not_retried(self) -> None:
        connection = FakeConnection(deny_tempdb=True)
        monitor = SqlServerWorkloadMonitor(lambda: connection, session_id=73, interval_s=.01, max_samples=3)
        monitor.start()
        monitor.thread.join(timeout=1)
        monitor.stop()
        result = monitor.summary()
        self.assertEqual(result["sample_count"], 3)
        self.assertEqual(result["monitor_select_attempts"], 7)
        self.assertEqual(sum(sql == TEMPDB_SQL for sql, _ in connection.calls), 1)
        self.assertEqual(result["unavailable"]["tempdb"], "PermissionError")
        self.assertNotIn("VIEW SERVER", json.dumps(result))

    def test_connection_failure_and_select_only(self) -> None:
        attempts = []
        def fail_connect() -> FakeConnection:
            attempts.append(1)
            raise ConnectionError("private server name")
        monitor = SqlServerWorkloadMonitor(fail_connect, session_id=73, interval_s=.01, max_samples=3)
        monitor.start()
        monitor.stop()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(monitor.summary()["unavailable"]["connection_or_output"], "ConnectionError")
        self.assertEqual(monitor.summary()["monitor_select_attempts"], 0)
        with self.assertRaisesRegex(ValueError, "SELECT-only"):
            monitor._query(FakeConnection(), "DELETE FROM dbo.Rddbc120")

    def test_stop_during_wait_and_process_metrics(self) -> None:
        connection = FakeConnection()
        seen = Event()
        monitor = SqlServerWorkloadMonitor(lambda: connection, session_id=73, interval_s=60,
                                           on_sample=lambda _: seen.set())
        monitor.start()
        self.assertTrue(seen.wait(1))
        monitor.stop()
        self.assertEqual(monitor.summary()["sample_count"], 1)
        self.assertTrue(connection.closed)
        worker = worker_process_metrics(process_time())
        self.assertEqual(worker["scope"], "LOCAL_WORKER_PROCESS")
        self.assertGreaterEqual(worker["cpu_time_ms"], 0)
        self.assertIsNotNone(worker["working_set_bytes"])

    def test_perf_summary_stderr_and_approval_gate(self) -> None:
        stream = io.StringIO()
        with ProgressReporter(stream=stream) as reporter:
            reporter.update("source_select", {})
            reporter.update("source_sql_elapsed", {"elapsed_ms": 25})
            reporter.update("batch_complete", {"rows": 7, "elapsed_ms": 100})
            reporter.update("monitor_setup_select", {})
            reporter.update("db_monitor_sample", {
                "sample_count": 1, "request_active": True, "blocking": False,
                "session_cpu_ms": 10, "session_logical_reads": 100, "session_reads": 5,
                "request_elapsed_ms": 20, "request_wait_ms": 4,
                "tempdb_net_kb": 32, "server_available_memory_kb": 2048,
                "unavailable": {"server_cpu": "no_supported_low_cost_metric"},
            })
            reporter.update("monitor_summary", {"monitor_select_attempts": 3, "blocking_observed": False})
        self.assertIn("DB_MONITOR", stream.getvalue())
        self.assertIn("logical_reads=100", stream.getvalue())
        line = next(line for line in stream.getvalue().splitlines() if "PERF_SUMMARY" in line)
        summary = json.loads(line.split("PERF_SUMMARY ", 1)[1])
        self.assertEqual((summary["source_select_attempts"], summary["monitor_select_attempts"],
                          summary["total_db_select_attempts"]), (1, 4, 5))
        self.assertEqual((summary["new_batch_avg_ms"], summary["new_batch_max_ms"]), (100, 100))
        stderr = io.StringIO()
        with (patch("sys.argv", ["extract_ml_bulk_db3.py", "--live-read-only", "--company-id", "3",
                                "--monitor-db3"]),
              patch("tools.extract_ml_bulk_db3.run", side_effect=AssertionError("must not connect")),
              redirect_stderr(stderr)):
            with self.assertRaises(SystemExit):
                extract_main()
        self.assertIn("approval reference", stderr.getvalue())

    def test_monitor_setup_failure_is_explicit_without_retry(self) -> None:
        class SourceConnection:
            timeout = None
            def __enter__(self) -> SourceConnection:
                return self
            def __exit__(self, *_: object) -> None:
                pass
            def cursor(self) -> FakeCursor:
                raise PermissionError("private DB address")

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with (patch("tools.extract_ml_bulk_db3.ml_storage_root", return_value=Path(tmp)),
                  patch("tools.extract_ml_bulk_db3.load_dashboard_profile_checked",
                        return_value=SimpleNamespace(status="ready", profile={"io_gu_list": ["501"]})),
                  patch("tools.extract_ml_bulk_db3.get_company_db_config",
                        return_value=SimpleNamespace(company_id=3)),
                  patch("tools.extract_ml_bulk_db3.build_company_conn_str", return_value="fake"),
                  patch("tools.extract_ml_bulk_db3.pyodbc.connect", return_value=SourceConnection()) as connect,
                  patch("tools.extract_ml_bulk_db3.Db3Source.product_rows", return_value=iter(()))):
                with ProgressReporter(stream=output) as reporter:
                    result = extract_run(
                        dataset_version="db3-bulk-monitorfixture", date_from=date(2018, 1, 1),
                        date_to=date(2018, 1, 31), group_size=1, product_limit=None,
                        include_dataset_version=None, resume=False, max_batches=None,
                        inventory_only=True, monitor_db3=True,
                        monitor_approval_reference="fixture-approval", progress=reporter.update)
        self.assertEqual(result["erp_select_count"], 0)
        self.assertEqual(connect.call_count, 1)
        summary = json.loads(next(line.split("PERF_SUMMARY ", 1)[1]
                                  for line in output.getvalue().splitlines() if "PERF_SUMMARY" in line))
        self.assertEqual(summary["db3_monitor"]["state"], "UNAVAILABLE")
        self.assertEqual(summary["db3_monitor"]["unavailable"]["setup"], "PermissionError")
        self.assertEqual(summary["total_db_select_attempts"], 1)
        self.assertNotIn("private DB address", output.getvalue())


if __name__ == "__main__":
    unittest.main()
