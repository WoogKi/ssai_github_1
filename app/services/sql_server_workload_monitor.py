"""Bounded, read-only SQL Server session sampling for opt-in workload diagnostics."""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from threading import Event, Lock, Thread
from time import monotonic, process_time
from typing import Any, Callable


REQUEST_SQL = """
SELECT s.cpu_time AS session_cpu_ms, s.logical_reads AS session_logical_reads,
       s.reads AS session_reads, r.cpu_time AS request_cpu_ms,
       r.logical_reads AS request_logical_reads, r.reads AS request_reads,
       r.total_elapsed_time AS request_elapsed_ms, r.wait_time AS request_wait_ms,
       r.wait_type, r.blocking_session_id
FROM sys.dm_exec_sessions AS s
LEFT JOIN sys.dm_exec_requests AS r ON r.session_id = s.session_id
WHERE s.session_id = ?
"""
TEMPDB_SQL = """
SELECT (user_objects_alloc_page_count - user_objects_dealloc_page_count
      + internal_objects_alloc_page_count - internal_objects_dealloc_page_count) * 8
      AS completed_task_tempdb_net_kb
FROM sys.dm_db_session_space_usage WHERE session_id = ?
"""
MEMORY_SQL = """
SELECT p.physical_memory_in_use_kb AS sql_process_memory_kb,
       m.total_physical_memory_kb AS server_total_memory_kb,
       m.available_physical_memory_kb AS server_available_memory_kb
FROM sys.dm_os_process_memory AS p CROSS JOIN sys.dm_os_sys_memory AS m
"""


def _unavailable_reason(exc: BaseException) -> str:
    state = str(exc.args[0]) if exc.args else ""
    return f"{type(exc).__name__}:{state}" if len(state) == 5 and state.isalnum() else type(exc).__name__


class SqlServerWorkloadMonitor:
    """Sample one known ERP session; never scan other sessions or retain raw rows."""

    def __init__(self, connection_factory: Callable[[], Any], *, session_id: int,
                 interval_s: float = 60.0, max_samples: int = 120,
                 on_sample: Callable[[dict[str, Any]], None] | None = None) -> None:
        if session_id <= 0 or interval_s <= 0 or not 1 <= max_samples <= 720:
            raise ValueError("invalid bounded SQL monitor scope")
        self.connection_factory = connection_factory
        self.session_id = session_id
        self.interval_s = interval_s
        self.max_samples = max_samples
        self.on_sample = on_sample
        self.stop_event = Event()
        self.lock = Lock()
        self.thread = Thread(target=self._run, daemon=True)
        self.sample_count = 0
        self.select_count = 0
        self.unavailable: dict[str, str] = {"server_cpu": "no_supported_low_cost_metric"}
        self.first_session: dict[str, int] | None = None
        self.last_session: dict[str, int] | None = None
        self.max_request_elapsed_ms: int | None = None
        self.max_request_wait_ms: int | None = None
        self.blocking_observed = False
        self.max_tempdb_net_kb: int | None = None
        self.last_server_memory: dict[str, int] | None = None

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join()

    def _query(self, connection: Any, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        if not sql.lstrip().upper().startswith("SELECT"):
            raise ValueError("SQL monitor is SELECT-only")
        with self.lock:
            self.select_count += 1
        cursor = connection.cursor()
        try:
            cursor.execute(sql, *params)
            row = cursor.fetchone()
            if row is None:
                return None
            return dict(zip((column[0] for column in cursor.description), row))
        finally:
            cursor.close()

    def _run(self) -> None:
        connection = None
        try:
            connection = self.connection_factory()
            connection.timeout = 5
            while not self.stop_event.is_set() and self.sample_count < self.max_samples:
                sample: dict[str, Any] = {"scope": "DB3_ERP_SESSION", "session_id": self.session_id}
                for name, sql, params in (
                    ("request", REQUEST_SQL, (self.session_id,)),
                    ("tempdb", TEMPDB_SQL, (self.session_id,)),
                    ("server_memory", MEMORY_SQL, ()),
                ):
                    with self.lock:
                        disabled = name in self.unavailable
                    if disabled:
                        continue
                    try:
                        value = self._query(connection, sql, params)
                        if value is None:
                            with self.lock:
                                self.unavailable[name] = "target_session_or_metric_not_visible"
                        else:
                            sample[name] = value
                    except Exception as exc:
                        with self.lock:
                            self.unavailable[name] = _unavailable_reason(exc)
                with self.lock:
                    self.sample_count += 1
                    request = sample.get("request")
                    if request:
                        counters = {key: int(request[key]) for key in (
                            "session_cpu_ms", "session_logical_reads", "session_reads")}
                        self.first_session = self.first_session or counters
                        self.last_session = counters
                        for field, attribute in (("request_elapsed_ms", "max_request_elapsed_ms"),
                                                 ("request_wait_ms", "max_request_wait_ms")):
                            if request[field] is not None:
                                prior = getattr(self, attribute)
                                setattr(self, attribute, max(prior or 0, int(request[field])))
                        self.blocking_observed |= int(request["blocking_session_id"] or 0) != 0
                    tempdb = sample.get("tempdb")
                    if tempdb and tempdb["completed_task_tempdb_net_kb"] is not None:
                        value = int(tempdb["completed_task_tempdb_net_kb"])
                        self.max_tempdb_net_kb = max(self.max_tempdb_net_kb or value, value)
                    memory = sample.get("server_memory")
                    if memory:
                        self.last_server_memory = {key: int(value) for key, value in memory.items()}
                    unavailable = dict(self.unavailable)
                if self.on_sample is not None:
                    request = sample.get("request") or {}
                    tempdb = sample.get("tempdb") or {}
                    memory = sample.get("server_memory") or {}
                    self.on_sample({"scope": "DB3_ERP_SESSION", "sample_count": self.sample_count,
                                    "request_active": request.get("request_elapsed_ms") is not None,
                                    "session_cpu_ms": request.get("session_cpu_ms"),
                                    "session_logical_reads": request.get("session_logical_reads"),
                                    "session_reads": request.get("session_reads"),
                                    "request_elapsed_ms": request.get("request_elapsed_ms"),
                                    "request_wait_ms": request.get("request_wait_ms"),
                                    "tempdb_net_kb": tempdb.get("completed_task_tempdb_net_kb"),
                                    "server_available_memory_kb": memory.get("server_available_memory_kb"),
                                    "blocking": self.blocking_observed, "unavailable": unavailable})
                if all(name in unavailable for name in ("request", "tempdb", "server_memory")):
                    break
                if self.stop_event.wait(self.interval_s):
                    break
        except Exception as exc:
            with self.lock:
                self.unavailable["connection_or_output"] = _unavailable_reason(exc)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception as exc:
                    with self.lock:
                        self.unavailable["close"] = _unavailable_reason(exc)

    def summary(self) -> dict[str, Any]:
        with self.lock:
            first, last = self.first_session, self.last_session
            metric_failures = set(self.unavailable) - {"server_cpu"}
            state = ("UNAVAILABLE" if self.sample_count == 0
                     or {"request", "tempdb", "server_memory"}.issubset(metric_failures)
                     else "PARTIAL" if metric_failures else "SAMPLED")
            return {
                "scope": "DB3_ERP_SESSION", "state": state, "session_id": self.session_id,
                "sample_interval_s": self.interval_s, "sample_count": self.sample_count,
                "sample_cap_reached": self.sample_count >= self.max_samples,
                "monitor_select_attempts": self.select_count,
                "session_cpu_ms_sample_delta": last["session_cpu_ms"] - first["session_cpu_ms"] if first and last else None,
                "session_logical_reads_sample_delta": last["session_logical_reads"] - first["session_logical_reads"] if first and last else None,
                "session_reads_sample_delta": last["session_reads"] - first["session_reads"] if first and last else None,
                "max_sampled_request_elapsed_ms": self.max_request_elapsed_ms,
                "max_sampled_request_wait_ms": self.max_request_wait_ms,
                "blocking_observed": self.blocking_observed,
                "max_completed_task_tempdb_net_kb": self.max_tempdb_net_kb,
                "last_server_memory_kb": dict(self.last_server_memory) if self.last_server_memory else None,
                "unavailable": dict(self.unavailable),
                "coverage": "bounded_samples_not_exact_query_totals",
            }


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in (
            "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
            "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
            "PagefileUsage", "PeakPagefileUsage")]


def worker_process_metrics(started_cpu_s: float) -> dict[str, Any]:
    result: dict[str, Any] = {"scope": "LOCAL_WORKER_PROCESS", "pid": os.getpid(),
                              "cpu_time_ms": round((process_time() - started_cpu_s) * 1000),
                              "working_set_bytes": None, "peak_working_set_bytes": None}
    if sys.platform != "win32":
        result["memory_unavailable"] = "windows_process_api_required"
        return result
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessMemoryCounters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise OSError(ctypes.get_last_error())
        result["working_set_bytes"] = counters.WorkingSetSize
        result["peak_working_set_bytes"] = counters.PeakWorkingSetSize
    except (OSError, AttributeError) as exc:
        result["memory_unavailable"] = type(exc).__name__
    return result
