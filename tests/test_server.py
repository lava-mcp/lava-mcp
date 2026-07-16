from __future__ import annotations

import asyncio

import pytest

from lava_mcp.config import Config
from lava_mcp.server import (
    _enforce_user_allowlist,
    _lava_username,
    _require_remote_access_device,
    build_server,
)


class _FakeDevicesClient:
    """Stub LavaClient exposing just list_devices for the tag-gate tests."""

    def __init__(self, count: int) -> None:
        self._count = count
        self.calls: list[dict] = []

    def list_devices(self, limit: int = 50, **filters: object) -> dict:
        self.calls.append({"limit": limit, **filters})
        results = [{"hostname": f"d{i}"} for i in range(self._count)]
        return {"count": self._count, "results": results}


def tool_names(read_only: bool) -> set[str]:
    server = build_server(Config(url="https://lava.example.com", read_only=read_only))
    tools = asyncio.run(server.list_tools())
    return {t.name for t in tools}


def test_read_tools_always_present() -> None:
    names = tool_names(read_only=False)
    assert {"list_devices", "get_job", "get_queue", "get_job_results"} <= names


def test_write_tools_present_when_not_read_only() -> None:
    names = tool_names(read_only=False)
    assert {"submit_job", "cancel_job", "resubmit_job"} <= names


def test_write_tools_absent_in_read_only() -> None:
    names = tool_names(read_only=True)
    assert "submit_job" not in names
    assert "cancel_job" not in names
    # validate_job is non-mutating, so it stays available
    assert "validate_job" in names


def test_lava_username_extraction() -> None:
    assert _lava_username({"user": "alice"}) == "alice"
    assert _lava_username({"username": "bob"}) == "bob"
    assert _lava_username("carol") == "carol"
    assert _lava_username({}) is None
    assert _lava_username(None) is None


def test_require_remote_access_device_passes_when_tagged_device_exists() -> None:
    client = _FakeDevicesClient(count=2)
    _require_remote_access_device(client, "qcs6490", "allow-remote-access")
    # the gate queries by device_type and tag name
    assert client.calls[0]["device_type"] == "qcs6490"
    assert client.calls[0]["tags__name"] == "allow-remote-access"


def test_require_remote_access_device_raises_when_none_tagged() -> None:
    client = _FakeDevicesClient(count=0)
    with pytest.raises(PermissionError, match="allow-remote-access"):
        _require_remote_access_device(client, "qcs6490", "allow-remote-access")


def test_require_remote_access_device_noop_when_gate_disabled() -> None:
    client = _FakeDevicesClient(count=0)
    _require_remote_access_device(client, "qcs6490", "")  # empty tag disables the gate
    assert client.calls == []


def test_enforce_user_allowlist() -> None:
    # empty allowlist is open: any user (or none) is fine
    _enforce_user_allowlist("alice", ())
    _enforce_user_allowlist(None, ())
    # configured allowlist admits members and rejects everyone else
    _enforce_user_allowlist("alice", ("alice", "bob"))
    with pytest.raises(PermissionError):
        _enforce_user_allowlist("mallory", ("alice", "bob"))
    with pytest.raises(PermissionError):
        _enforce_user_allowlist(None, ("alice",))
