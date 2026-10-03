"""Offline gate for Dashboard single-flight, stale publish, and query timeout."""

from __future__ import annotations

from pathlib import Path
import sys
import threading


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import mssql_client as db
from app.services.dashboard_request_coordinator import (
    DASHBOARD_SESSION_TOKEN_KEY,
    DashboardRequestAlreadyRunning,
    DashboardRequestCoordinator,
    DashboardRequestStale,
    dashboard_request_coordinator,
    ensure_dashboard_session_token,
)
from app.sims.views import dashboard_lite as dashboard_view


def _single_flight_gate() -> None:
    coordinator = DashboardRequestCoordinator()
    first = coordinator.acquire(
        session_token="session-a", user_id="user", company_id="7", room_id="room-a",
    )
    try:
        coordinator.acquire(
            session_token="session-a", user_id="user", company_id="7", room_id="room-b",
        )
    except DashboardRequestAlreadyRunning:
        pass
    else:
        raise AssertionError("duplicate Dashboard request was not blocked")
    coordinator.checkpoint(first)
    assert coordinator.release(first) is True

    session_a = coordinator.acquire(
        session_token="session-a", user_id="user", company_id="7", room_id="room-a",
    )
    session_b = coordinator.acquire(
        session_token="session-b", user_id="user", company_id="7", room_id="room-a",
    )
    coordinator.checkpoint(session_a)
    coordinator.checkpoint(session_b)
    assert coordinator.release(session_a) is True
    assert coordinator.release(session_b) is True


def _stale_and_owner_gate(reason: str) -> None:
    coordinator = DashboardRequestCoordinator()
    started = threading.Event()
    release = threading.Event()
    source_calls: list[str] = []
    published: list[str] = []
    stale: list[bool] = []

    def _old_request() -> None:
        lease = coordinator.acquire(
            session_token="session", user_id="user", company_id="7", room_id="room-old",
        )
        try:
            coordinator.checkpoint(lease)
            source_calls.append("inbound")
            started.set()
            assert release.wait(5)
            coordinator.checkpoint(lease)
            source_calls.append("sales")
            published.append("cache")
        except DashboardRequestStale:
            stale.append(True)
        finally:
            coordinator.release(lease)

    thread = threading.Thread(target=_old_request, name=f"dashboard-{reason}")
    thread.start()
    assert started.wait(5)
    assert coordinator.invalidate_session("session") == 1
    try:
        coordinator.acquire(
            session_token="session", user_id="user", company_id="7", room_id="room-new",
        )
    except DashboardRequestAlreadyRunning:
        pass
    else:
        raise AssertionError("stale SQL owner no longer blocks the same Dashboard key")

    company3_lease = coordinator.acquire(
        session_token="session", user_id="user", company_id="3", room_id="room-new",
    )
    coordinator.checkpoint(company3_lease)
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert stale == [True]
    assert source_calls == ["inbound"]
    assert published == []
    coordinator.checkpoint(company3_lease)
    assert coordinator.release(company3_lease) is True, "old owner removed the other-company registry"
    company7_lease = coordinator.acquire(
        session_token="session", user_id="user", company_id="7", room_id="room-after-release",
    )
    assert coordinator.release(company7_lease) is True


class _FakeDriver:
    def __init__(self) -> None:
        self.timeout = 9


class _FakeDbapiProxy:
    def __init__(self, driver: _FakeDriver) -> None:
        self.driver_connection = driver


class _FakeConnection:
    def __init__(self, driver: _FakeDriver) -> None:
        self.connection = _FakeDbapiProxy(driver)
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeEngine:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    def connect(self) -> _FakeConnection:
        return self.connection


def _timeout_restore_gate() -> None:
    original_get_engine = db._get_engine
    try:
        driver = _FakeDriver()
        connection = _FakeConnection(driver)
        db._get_engine = lambda: _FakeEngine(connection)
        with db.read_only_request(timeout_seconds=120):
            with db.get_conn():
                assert driver.timeout == 120
        assert driver.timeout == 9 and connection.closed

        driver = _FakeDriver()
        connection = _FakeConnection(driver)
        db._get_engine = lambda: _FakeEngine(connection)
        try:
            with db.read_only_request(timeout_seconds=120):
                with db.get_conn():
                    assert driver.timeout == 120
                    raise RuntimeError("fixture")
        except RuntimeError:
            pass
        assert driver.timeout == 9 and connection.closed
    finally:
        db._get_engine = original_get_engine


def _common_boundary_gate() -> None:
    original_facts = dashboard_view.build_dashboard_lite_facts
    dashboard_request_coordinator.reset_for_tests()
    calls = {"count": 0}
    checkpoints: list[str] = []

    def _facts(_params, *, request_checkpoint=None, **_kwargs):
        calls["count"] += 1
        state = db._READ_ONLY_REQUEST.get()
        assert state is not None and state["timeout_seconds"] == 120
        for phase in ("before_inbound", "after_inbound", "before_sales", "after_sales", "before_stock", "after_stock"):
            request_checkpoint(phase)
            checkpoints.append(phase)
        return {
            "kind": "SIMS_DASHBOARD_FACTS_V01",
            "source_call_count": 3,
            "filters": {},
            "today_actions": [],
            "inventory": {"readiness_rows": [{"product_code": "fixture"}]},
        }

    try:
        dashboard_view.build_dashboard_lite_facts = _facts
        session_state: dict = {}
        payload, cache = dashboard_view.build_dashboard_lite_result_payload(
            {"company_id": "7"},
            room_id="room-a",
            company_id="7",
            action="SIMS 일일점검",
            session_state=session_state,
        )
        assert calls["count"] == 1
        assert len(checkpoints) == 6
        assert payload["meta"]["source_call_count"] == 3
        assert cache["facts"]["source_call_count"] == 3
        assert isinstance(payload["meta"].get("_dashboard_request_guard"), dict)
        assert DASHBOARD_SESSION_TOKEN_KEY in session_state
        assert dashboard_view.dashboard_request_publish_allowed(cache, current_room_id="room-a") is True
        assert dashboard_view.dashboard_request_publish_allowed(cache, current_room_id="room-b") is False
        dashboard_request_coordinator.invalidate_session(session_state[DASHBOARD_SESSION_TOKEN_KEY])
        assert dashboard_view.dashboard_request_publish_allowed(cache, current_room_id="room-a") is False

        retry_calls = {"count": 0}

        def _timeout(_params, **_kwargs):
            retry_calls["count"] += 1
            raise TimeoutError("fixture query timeout")

        dashboard_view.build_dashboard_lite_facts = _timeout
        try:
            dashboard_view.build_dashboard_lite_result_payload(
                {"company_id": "7"},
                room_id="room-a",
                company_id="7",
                session_state=session_state,
            )
        except TimeoutError as exc:
            assert dashboard_view.is_dashboard_query_timeout(exc)
        else:
            raise AssertionError("timeout exception was swallowed")
        assert retry_calls["count"] == 1
    finally:
        dashboard_view.build_dashboard_lite_facts = original_facts
        dashboard_request_coordinator.reset_for_tests()


def _session_token_survival_gate() -> None:
    state = {DASHBOARD_SESSION_TOKEN_KEY: "stable-session", "__dashboard_lite_result": {"facts": {}}}
    dashboard_view.clear_dashboard_lite_session_state(state)
    assert state[DASHBOARD_SESSION_TOKEN_KEY] == "stable-session"
    assert ensure_dashboard_session_token(state) == "stable-session"


def main() -> int:
    _single_flight_gate()
    _stale_and_owner_gate("company-switch")
    _stale_and_owner_gate("logout")
    _timeout_restore_gate()
    _common_boundary_gate()
    _session_token_survival_gate()
    print(
        "PASS dashboard request guard: single_flight=1 different_session=parallel "
        "company_stale=blocked same_key_until_release=blocked cross_company=allowed "
        "logout_stale=blocked owner_cleanup=safe timeout=120 restored retry=0 "
        "source_call_count=3"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
