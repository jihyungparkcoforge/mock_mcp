"""Pick the MCP transport: Streamable HTTP when MCP_SERVER_URL is set, stdio otherwise."""

from __future__ import annotations

import os
import sys
from typing import Any
from urllib.parse import urlparse

from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


def server_url() -> str | None:
    return os.environ.get("MCP_SERVER_URL") or None


def http_settings(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"MCP_SERVER_URL must look like http://host:port/path, got {url!r}")
    return {
        "host": parsed.hostname,
        "port": parsed.port or (443 if parsed.scheme == "https" else 80),
        "streamable_http_path": parsed.path or "/mcp",
    }


def open_transport():
    """Async context manager yielding (read, write) for ClientSession."""
    url = server_url()
    if url:
        return streamable_http_client(url)
    params = StdioServerParameters(command=sys.executable, args=["-m", "equipment_mcp.server"])
    return stdio_client(params)
