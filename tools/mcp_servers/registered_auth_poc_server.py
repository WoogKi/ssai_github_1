"""Registered STDIO MCP PoC. Synthetic data only; no SIMS session issuer is wired."""

from __future__ import annotations

import hmac
import json
import os
import threading
import time
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

try:
    from .native_auth_poc_server import (
        ACTIVE_CONTEXT, ALLOWED_USERS, PERMISSION, SERVICE, FixtureVerifier,
    )
except ImportError:
    from native_auth_poc_server import (  # type: ignore[no-redef]
        ACTIVE_CONTEXT, ALLOWED_USERS, PERMISSION, SERVICE, FixtureVerifier,
    )


PLUGIN_ID = "mcp/sims-synthetic-auth"
EXECUTIONS = {"notice_read": 0, "admin_read": 0}
ATTEMPTS = {"notice_read": 0, "admin_read": 0}
MAX_USED_NONCES = 4096


def _secret_from_file() -> bytes:
    raw = os.environ.get("SIMS_MCP_POC_SECRET_FILE", "")
    if not raw:
        raise RuntimeError("synthetic MCP secret file is not configured")
    path = Path(raw)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("synthetic MCP secret file is missing or linked")
    with path.open("rb") as handle:
        secret = handle.read(33)
    if len(secret) != 32:
        raise RuntimeError("synthetic MCP secret must be 32 random bytes")
    return secret


def make_server(secret: bytes) -> MCPServer:
    if len(secret) != 32:
        raise ValueError("synthetic MCP secret must be 32 bytes")
    verifier = FixtureVerifier(secret)
    used_nonces: dict[str, int] = {}
    nonce_lock = threading.Lock()
    server = MCPServer("SIMS Registered Synthetic Auth PoC")

    async def guarded(tool: str, company_id: int, service: str, capability: str) -> str:
        ATTEMPTS[tool] += 1
        access = await verifier.verify_token(capability) if capability else None
        claims = access.claims if access else None
        now = time.time()
        if not isinstance(claims, dict):
            raise ToolError("permission_denied")
        nonce = claims.get("nonce")
        if (not isinstance(nonce, str) or not 16 <= len(nonce) <= 128
                or claims.get("aud") != PLUGIN_ID
                or claims.get("exp", 0) > now + 120
                or claims.get("company_id") != company_id
                or (company_id, claims.get("user_id")) not in ALLOWED_USERS
                or ACTIVE_CONTEXT.get(claims.get("user_id"))
                != (company_id, claims.get("session_epoch"))
                or claims.get("service") != service or service != SERVICE
                or claims.get("permission") != PERMISSION
                or tool not in claims.get("allowed_tools", [])):
            raise ToolError("permission_denied")
        # Keep replay protection bounded without evicting any still-valid nonce.
        with nonce_lock:
            now = time.time()
            for key in [key for key, expiry in used_nonces.items() if expiry <= now]:
                del used_nonces[key]
            if (nonce in used_nonces or len(used_nonces) >= MAX_USED_NONCES
                    or claims["exp"] <= now):
                raise ToolError("permission_denied")
            used_nonces[nonce] = claims["exp"]
        EXECUTIONS[tool] += 1
        proof = hmac.digest(secret, f"{nonce}:{company_id}:{service}:{tool}".encode(), "sha256").hex()
        return json.dumps({"company_id": company_id, "service": service,
                           "source": "synthetic-fixture", "receipt": proof}, sort_keys=True)

    @server.tool(name="notice_read", description="Read a synthetic notice with a one-use SIMS test capability.")
    async def notice_read(company_id: int, service: str, capability: str) -> str:
        return await guarded("notice_read", company_id, service, capability)

    @server.tool(name="admin_read", description="Synthetic denied-tool fixture.")
    async def admin_read(company_id: int, service: str, capability: str) -> str:
        return await guarded("admin_read", company_id, service, capability)

    return server


if __name__ == "__main__":
    make_server(_secret_from_file()).run("stdio")
