# -*- coding: utf-8 -*-
"""Process-local lifecycle guard for expensive Dashboard requests."""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
import uuid
from typing import Any, MutableMapping


DASHBOARD_SESSION_TOKEN_KEY = "__dashboard_session_token"
DASHBOARD_ACTION_KEY = "SIMS_DASHBOARD"


class DashboardRequestAlreadyRunning(RuntimeError):
    """Raised before source execution when an equivalent request is active."""


class DashboardRequestStale(RuntimeError):
    """Raised when a request no longer owns the current session generation."""


@dataclass(frozen=True)
class DashboardRequestLease:
    session_token: str
    user_id: str
    company_id: str
    room_id: str
    request_token: str
    generation: int
    started_at: float

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.session_token, self.user_id, self.company_id, DASHBOARD_ACTION_KEY)

    def receipt(self) -> dict[str, Any]:
        return {
            "session_token": self.session_token,
            "user_id": self.user_id,
            "company_id": self.company_id,
            "room_id": self.room_id,
            "request_token": self.request_token,
            "generation": self.generation,
        }


class DashboardRequestCoordinator:
    """Serialize one Dashboard per session/user/company and track stale epochs."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: dict[tuple[str, str, str, str], DashboardRequestLease] = {}
        self._generations: dict[str, int] = {}

    def acquire(
        self,
        *,
        session_token: str,
        user_id: Any,
        company_id: Any,
        room_id: Any,
    ) -> DashboardRequestLease:
        session_value = str(session_token or "").strip()
        if not session_value:
            raise ValueError("dashboard session token is required")
        user_value = str(user_id or "").strip()
        company_value = str(company_id or "").strip()
        key = (session_value, user_value, company_value, DASHBOARD_ACTION_KEY)
        with self._lock:
            if key in self._active:
                raise DashboardRequestAlreadyRunning("dashboard request is already running")
            generation = int(self._generations.get(session_value) or 0) + 1
            self._generations[session_value] = generation
            lease = DashboardRequestLease(
                session_token=session_value,
                user_id=user_value,
                company_id=company_value,
                room_id=str(room_id or "").strip(),
                request_token=uuid.uuid4().hex,
                generation=generation,
                started_at=time.monotonic(),
            )
            self._active[key] = lease
            return lease

    def checkpoint(self, lease: DashboardRequestLease) -> None:
        with self._lock:
            current_generation = int(self._generations.get(lease.session_token) or 0)
            active = self._active.get(lease.key)
            if (
                current_generation != lease.generation
                or active is None
                or active.request_token != lease.request_token
            ):
                raise DashboardRequestStale("dashboard request is stale")

    def publish_allowed(
        self,
        receipt: Any,
        *,
        user_id: Any,
        company_id: Any,
        room_id: Any,
    ) -> bool:
        source = receipt if isinstance(receipt, dict) else {}
        session_token = str(source.get("session_token") or "").strip()
        if not session_token:
            return False
        with self._lock:
            if int(self._generations.get(session_token) or 0) != int(source.get("generation") or -1):
                return False
        return (
            str(source.get("user_id") or "").strip() == str(user_id or "").strip()
            and str(source.get("company_id") or "").strip() == str(company_id or "").strip()
            and str(source.get("room_id") or "").strip() == str(room_id or "").strip()
        )

    def release(self, lease: DashboardRequestLease) -> bool:
        with self._lock:
            active = self._active.get(lease.key)
            if active is None or active.request_token != lease.request_token:
                return False
            self._active.pop(lease.key, None)
            return True

    def invalidate_session(self, session_token: str) -> int:
        session_value = str(session_token or "").strip()
        if not session_value:
            return 0
        with self._lock:
            self._generations[session_value] = int(self._generations.get(session_value) or 0) + 1
            return sum(1 for key in self._active if key[0] == session_value)

    def reset_for_tests(self) -> None:
        with self._lock:
            self._active.clear()
            self._generations.clear()


dashboard_request_coordinator = DashboardRequestCoordinator()


def ensure_dashboard_session_token(session_state: MutableMapping[str, Any]) -> str:
    token = str(session_state.get(DASHBOARD_SESSION_TOKEN_KEY) or "").strip()
    if not token:
        token = uuid.uuid4().hex
        session_state[DASHBOARD_SESSION_TOKEN_KEY] = token
    return token


def invalidate_dashboard_session_state(session_state: MutableMapping[str, Any]) -> int:
    token = str(session_state.get(DASHBOARD_SESSION_TOKEN_KEY) or "").strip()
    return dashboard_request_coordinator.invalidate_session(token)
