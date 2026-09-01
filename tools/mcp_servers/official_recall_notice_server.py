"""Controlled local MCP server exposing one static, read-only test resource."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from mcp.server import MCPServer


SERVER = MCPServer("SIMS Official Recall Notice PoC")


@SERVER.resource(
    "local-mcp://official-recall-notice/{query}",
    name="official-recall-notice",
    title="Official recall notice test resource",
    description="Static local fixture for the SIMS MCP transport PoC.",
    mime_type="application/json",
)
def official_recall_notice(query: str) -> str:
    delay_text = os.environ.get("SIMS_MCP_TEST_DELAY_S", "").strip()
    if delay_text:
        time.sleep(max(0.0, min(float(delay_text), 30.0)))
    if os.environ.get("SIMS_MCP_TEST_INVALID_RESPONSE") == "1":
        return json.dumps({"resource_id": "official-recall-notice"})
    return json.dumps(
        {
            "resource_id": "official-recall-notice",
            "title": "Local official recall notice fixture",
            "source_uri": "local-mcp://official-recall-notice/static-fixture",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "text": "Local MCP test fixture only. No external recall service was contacted.",
        },
        ensure_ascii=False,
    )


if __name__ == "__main__":
    SERVER.run("stdio")
