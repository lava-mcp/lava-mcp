from __future__ import annotations

import asyncio

import pytest

from lava_mcp.config import Config
from lava_mcp.server import (
    _WS_NOT_CONFIGURED,
    _enforce_user_allowlist,
    _lava_username,
    _require_owner,
    _require_remote_access_device,
    _require_test_services_device,
    build_console_ready_action,
    build_console_services_action,
    build_console_ssh_command,
    build_server,
    build_shell_ssh_config,
    console_ready_in_logs,
)


class _OwnedSession:
    def __init__(self, owner: str | None) -> None:
        self.owner = owner
        self.session_id = "s-1"


class _FakeDevicesClient:
    """Stub LavaClient exposing just list_devices for the tag-gate tests."""

    def __init__(self, count: int) -> None:
        self._count = count
        self.calls: list[dict] = []

    def list_devices(self, limit: int = 50, **filters: object) -> dict:
        self.calls.append({"limit": limit, **filters})
        results = [{"hostname": f"d{i}"} for i in range(self._count)]
        return {"count": self._count, "results": results}


class _FakeServicesClient:
    """Stub LavaClient exposing allows_test_services for the console-gate tests."""

    def __init__(self, allowed: bool) -> None:
        self._allowed = allowed

    def allows_test_services(self, hostname: str) -> bool:
        return self._allowed


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


def test_require_test_services_device_passes_when_allowed() -> None:
    _require_test_services_device(_FakeServicesClient(allowed=True), "rb3g2-01")


def test_require_test_services_device_raises_when_disabled() -> None:
    with pytest.raises(PermissionError, match="allow_test_services"):
        _require_test_services_device(_FakeServicesClient(allowed=False), "rb3g2-01")


def test_config_reads_split_user_allowlists(monkeypatch) -> None:
    monkeypatch.setenv("LAVA_MCP_HTTP_ALLOW_USERS", "alice, bob")
    monkeypatch.setenv("LAVA_MCP_SSH_ALLOW_USERS", "alice")
    cfg = Config.from_env()
    assert cfg.http_allow_users == ("alice", "bob")
    assert cfg.ssh_allow_users == ("alice",)


def test_require_owner_enforces_session_ownership() -> None:
    _require_owner(_OwnedSession("alice"), "alice")  # owner ok
    _require_owner(_OwnedSession(None), "alice")  # unowned (legacy) ok
    with pytest.raises(PermissionError, match="another user"):
        _require_owner(_OwnedSession("bob"), "alice")


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


def test_build_shell_ssh_config_tunnels_over_websocat() -> None:
    ws = "wss://lava.example.com/gateway-ssh"
    conf = build_shell_ssh_config("s-abc", "k.key", ws, 45678, "root")
    # the jump host reaches the gateway only over the WebSocket transport
    assert f"ProxyCommand websocat -b {ws}" in conf
    # ProxyJump then rides the reverse tunnel to the board container's sshd
    assert "ProxyJump gw-s-abc" in conf
    assert "Port 45678" in conf
    assert "User root" in conf
    # no direct-dial SSH port anywhere (WebSocket-only)
    assert "-p " not in conf
    assert "Port 22\n" not in conf


def test_build_console_ssh_command_tunnels_over_websocat() -> None:
    ws = "wss://lava.example.com/gateway-ssh"
    cmd = build_console_ssh_command("s-xyz", "k.key", ws, 33333, "gw.example.com")
    assert f"ProxyCommand=websocat -b {ws}" in cmd
    assert "-W 127.0.0.1:33333" in cmd
    assert "s-xyz@gw.example.com" in cmd
    # WebSocket-only: no `-p <port>` direct gateway dial
    assert "-p " not in cmd


def test_ws_not_configured_message_names_the_env_var() -> None:
    assert "LAVA_MCP_GATEWAY_WS_URL" in _WS_NOT_CONFIGURED


def test_build_console_ready_action_is_valid_and_interactive() -> None:
    import yaml

    block = build_console_ready_action(sentinel="MY_SENTINEL", timeout_minutes=70)
    parsed = yaml.safe_load(block)
    assert isinstance(parsed, list) and len(parsed) == 1
    action = parsed[0]["test"]
    assert action["timeout"]["minutes"] == 70
    steps = action["definitions"][0]["repository"]["run"]["steps"]
    # echoes the (matching) sentinel to unlock the proxy
    assert any("MY_SENTINEL" in s for s in steps)
    # holds with an interactive shell (no tick/keepalive loop)
    assert any("exec" in s and "-i" in s for s in steps)
    assert not any("sleep" in s for s in steps)


def test_console_ready_in_logs_ignores_env_declaration() -> None:
    sentinel = "LAVA_MCP_CONSOLE_WRITABLE"
    # the env var is echoed at job start — not readiness
    env_only = '- {"lvl":"debug","msg":"- CONSOLE_READY_SENTINEL=' + sentinel + '"}\n'
    assert console_ready_in_logs(env_only, sentinel) is False
    # board echoing it as console output = ready
    booted = env_only + '- {"lvl":"target","msg":"' + sentinel + '"}\n'
    assert console_ready_in_logs(booted, sentinel) is True
    # empty / absent
    assert console_ready_in_logs("", sentinel) is False
    assert console_ready_in_logs("no marker here", sentinel) is False


def test_build_console_services_action_is_pasteable_and_uses_configured_repo() -> None:
    import yaml

    repo = "https://github.com/example/lava-mcp.git"
    block = build_console_services_action(repo)
    # valid YAML the agent can paste straight into a job's actions list
    parsed = yaml.safe_load(block)
    assert isinstance(parsed, list) and len(parsed) == 1
    svc = parsed[0]["test"]["services"][0]
    assert svc["name"] == "ser2net-proxy"
    assert svc["repository"] == repo
    assert svc["path"] == "interactive/ser2net-proxy/docker-compose.yml"


def test_server_instructions_describe_both_ways_to_reach_a_board() -> None:
    """The server's instructions (surfaced to MCP clients) must explain both the
    container-beside-the-board and the serial-console ways, and steer agents to base
    the console deploy/boot on an existing job rather than authoring one."""
    server = build_server(Config(url="https://x", gateway_enabled=True))
    ins = server.instructions or ""
    # defines what LAVA is for clients unfamiliar with it
    assert "Linaro Automated Validation Architecture" in ins
    # both ways, and the on-board vs next-to-board distinction
    assert "open_board_session" in ins and "open_console_session" in ins
    assert "next to" in ins and "serial console" in ins.lower()
    # console deploy/boot must be seeded from a previous job whose deploy url matches
    # the target artifacts, keeping its auth headers
    assert "get_job_definition" in ins
    assert "deploy `url`" in ins
    assert "Authorization" in ins
    # board-session container is Debian; includes a build-a-tool example
    assert "Debian" in ins and "linux-msm/qdl" in ins


def test_template_job_masters_surface_in_instructions() -> None:
    ins = (
        build_server(
            Config(url="https://x", template_job_masters=("https://lava.example.com",))
        ).instructions
        or ""
    )
    assert "Boot-template masters" in ins and "lava.example.com" in ins
    # absent when not configured
    ins2 = build_server(Config(url="https://x")).instructions or ""
    assert "Boot-template masters" not in ins2


def test_config_reads_template_job_masters(monkeypatch) -> None:
    monkeypatch.setenv(
        "LAVA_MCP_TEMPLATE_JOB_MASTERS", "https://lava.infra.foundries.io https://other"
    )
    cfg = Config.from_env()
    assert cfg.template_job_masters == (
        "https://lava.infra.foundries.io",
        "https://other",
    )
