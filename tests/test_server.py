from __future__ import annotations

import asyncio

from lava_mcp.client import LavaClient
from lava_mcp.config import Config
from lava_mcp.server import build_server


def tool_names(read_only: bool) -> set[str]:
    client = LavaClient(Config(url="https://lava.example.com", read_only=read_only))
    server = build_server(client)
    tools = asyncio.run(server.list_tools())
    return {t.name for t in tools}


def test_read_tools_always_present() -> None:
    names = tool_names(read_only=False)
    assert {"list_devices", "get_job", "get_queue", "get_job_results"} <= names


def test_write_tools_present_when_not_read_only() -> None:
    names = tool_names(read_only=False)
    assert {"submit_job", "cancel_job", "resubmit_job", "set_job_priority"} <= names


def test_write_tools_absent_in_read_only() -> None:
    names = tool_names(read_only=True)
    assert "submit_job" not in names
    assert "cancel_job" not in names
    # validate_job is non-mutating, so it stays available
    assert "validate_job" in names
