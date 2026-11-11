"""Isolated, synthetic HTTP MCP authorization PoC; never connects to ERP/KPIS."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from typing import Any

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError


PERMISSION = "MCP_EXTERNAL_RESOURCE_READ"
SERVICE = "synthetic-notice"
ALLOWED_USERS = {(2, "user-2"), (3, "user-2"), (3, "user-3")}
ACTIVE_CONTEXT = {"user-2": (2, 1), "user-3": (3, 1)}
EXECUTIONS = {"notice_read": 0, "admin_read": 0}
ATTEMPTS = {"notice_read": 0, "admin_read": 0}
HTTP_STATS = {"requests": 0, "http_4xx": 0, "http_401": 0}
AUTH_STATS = {"checked": 0, "accepted": 0}
_USED_TOKENS: dict[str, int] = {}
_TOKEN_LOCK = threading.Lock()


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def fixture_token(secret: bytes, claims: dict[str, Any]) -> str:
    """Test issuer only. Production identity must come from SIMS authentication."""
    payload = _encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    signature = _encode(hmac.digest(secret, payload.encode("ascii"), "sha256"))
    return payload + "." + signature


class FixtureVerifier:
    def __init__(self, secret: bytes):
        self.secret = secret

    async def verify_token(self, token: str) -> AccessToken | None:
        AUTH_STATS["checked"] += 1
        if not isinstance(token, str) or not 1 <= len(token) <= 8192:
            return None
        try:
            payload, signature = token.split(".", 1)
            expected = _encode(hmac.digest(self.secret, payload.encode("ascii"), "sha256"))
            if not hmac.compare_digest(signature, expected):
                return None
            claims = json.loads(_decode(payload))
            if (not isinstance(claims, dict) or type(claims.get("exp")) is not int
                    or type(claims.get("company_id")) is not int or claims["company_id"] <= 0
                    or type(claims.get("session_epoch")) is not int or claims["session_epoch"] < 1
                    or not isinstance(claims.get("user_id"), str) or not claims["user_id"]
                    or not isinstance(claims.get("service"), str)
                    or not isinstance(claims.get("permission"), str)
                    or not isinstance(claims.get("allowed_tools"), list)
                    or any(not isinstance(tool, str) or not tool for tool in claims["allowed_tools"])):
                return None
            if claims["exp"] <= time.time() or claims["exp"] > time.time() + 120:
                return None
            access = AccessToken(
                token=token,
                client_id="sims-poc",
                scopes=[PERMISSION],
                expires_at=claims["exp"],
                subject=str(claims.get("user_id", "")),
                claims=claims,
            )
            AUTH_STATS["accepted"] += 1
            return access
        except (ValueError, TypeError, UnicodeError, KeyError):
            return None


def _authorized(company_id: int, service: str, tool: str) -> bool:
    access = get_access_token()
    claims = access.claims if access else None
    if not isinstance(claims, dict):
        return False
    allowed = (
        claims.get("company_id") == company_id
        and (company_id, claims.get("user_id")) in ALLOWED_USERS
        and ACTIVE_CONTEXT.get(claims.get("user_id")) == (company_id, claims.get("session_epoch"))
        and claims.get("service") == service == SERVICE
        and claims.get("permission") == PERMISSION
        and tool in claims.get("allowed_tools", [])
    )
    if not allowed or type(claims.get("exp")) is not int or claims["exp"] <= time.time():
        return False
    # Discovery may reuse the bearer, but an authorized tool body may run only once.
    digest = hashlib.sha256(access.token.encode("utf-8")).hexdigest()
    with _TOKEN_LOCK:
        now = time.time()
        for key in [key for key, expiry in _USED_TOKENS.items() if expiry <= now]:
            del _USED_TOKENS[key]
        if digest in _USED_TOKENS or len(_USED_TOKENS) >= 4096 or claims["exp"] <= now:
            return False
        _USED_TOKENS[digest] = claims["exp"]
    return True


def notice_read(company_id: int, service: str) -> str:
    ATTEMPTS["notice_read"] += 1
    if not _authorized(company_id, service, "notice_read"):
        raise ToolError("permission_denied")
    EXECUTIONS["notice_read"] += 1
    return json.dumps({"company_id": company_id, "source": "synthetic-fixture"})


def admin_read(company_id: int, service: str) -> str:
    ATTEMPTS["admin_read"] += 1
    if not _authorized(company_id, service, "admin_read"):
        raise ToolError("permission_denied")
    EXECUTIONS["admin_read"] += 1
    return "synthetic-fixture"


def make_app(secret: bytes, port: int):
    server = MCPServer(
        "SIMS Native Auth Isolation PoC",
        token_verifier=FixtureVerifier(secret),
        auth=AuthSettings(
            issuer_url="http://127.0.0.1:1",
            resource_server_url=f"http://127.0.0.1:{port}/mcp",
        ),
    )
    server.tool(name="notice_read", description="Synthetic notice with server-side authorization.")(notice_read)
    server.tool(name="admin_read", description="Synthetic second tool for deny testing.")(admin_read)
    app = server.streamable_http_app(stateless_http=True, json_response=True, host="127.0.0.1")

    async def counted_app(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        HTTP_STATS["requests"] += 1

        async def counted_send(message):
            if message["type"] == "http.response.start" and 400 <= message["status"] < 500:
                HTTP_STATS["http_4xx"] += 1
                if message["status"] == 401:
                    HTTP_STATS["http_401"] += 1
            await send(message)

        return await app(scope, receive, counted_send)

    return counted_app
