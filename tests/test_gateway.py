from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import asyncssh
import pytest
import yaml

from lava_mcp.config import Config
from lava_mcp.gateway import (
    Gateway,
    SessionManager,
    _GatewaySSHServer,
    free_port,
    generate_keypair,
    ip_allowed,
    parse_networks,
)
from lava_mcp.jobs import build_interactive_job
from lava_mcp.server import build_server


class _FakeConn:
    """Minimal stand-in for an asyncssh connection for the IP-allowlist tests."""

    def __init__(self, peer: tuple[str, int] | None) -> None:
        self._peer = peer
        self.closed = False

    def get_extra_info(self, key: str) -> Any:
        return self._peer if key == "peername" else None

    def close(self) -> None:
        self.closed = True


def test_generate_keypair() -> None:
    private, public = generate_keypair()
    assert "PRIVATE KEY" in private
    assert public.startswith("ssh-ed25519 ")


def test_session_manager_create_get_remove() -> None:
    mgr = SessionManager()
    s = mgr.create(device_type="qcs6490")
    assert s.session_id.startswith("s-")
    assert s.reverse_port > 0
    assert s.public_key.startswith("ssh-ed25519 ")
    assert mgr.get(s.session_id) is s
    assert "private_key" not in s.public_view()  # never leak the key
    assert mgr.remove(s.session_id) is s
    assert mgr.get(s.session_id) is None


def test_build_interactive_job_carries_session_params() -> None:
    cfg = Config(
        url="https://lava.example.com",
        gateway_port=2222,
        gateway_advertise_host="gw.example.com",
    )
    session = SessionManager().create(device_type="qcs6490")
    job = yaml.safe_load(
        build_interactive_job(cfg, session, device_type="qcs6490", tags=["wifi"])
    )
    assert job["device_type"] == "qcs6490"
    # user tags are preserved and the remote-access tag is pinned on
    assert job["tags"] == ["wifi", "allow-remote-access"]
    test_action = job["actions"][0]["test"]
    assert test_action["docker"]["image"] == cfg.interactive_image
    definition = test_action["definitions"][0]
    assert definition["repository"] == cfg.interactive_repo
    assert definition["path"] == cfg.interactive_path
    params = definition["parameters"]
    assert params["SESSION_ID"] == session.session_id
    assert params["REVERSE_PORT"] == str(session.reverse_port)
    assert params["GATEWAY_HOST"] == "gw.example.com"
    assert params["GATEWAY_PORT"] == "2222"
    assert params["SESSION_PUBLIC_KEY"] == session.public_key


def test_build_interactive_job_pins_remote_access_tag_without_user_tags() -> None:
    cfg = Config(url="https://lava.example.com")
    session = SessionManager().create(device_type="qcs6490")
    job = yaml.safe_load(build_interactive_job(cfg, session, device_type="qcs6490"))
    assert job["tags"] == ["allow-remote-access"]


def test_build_interactive_job_tag_gate_can_be_disabled() -> None:
    cfg = Config(url="https://lava.example.com", remote_access_tag="")
    session = SessionManager().create(device_type="qcs6490")
    job = yaml.safe_load(build_interactive_job(cfg, session, device_type="qcs6490"))
    assert "tags" not in job


def test_interactive_assets_match_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    testdef = yaml.safe_load((root / "interactive" / "ssh-gateway.yaml").read_text())
    assert testdef["metadata"]["format"] == "Lava-Test Test Definition 1.0"
    steps = testdef["run"]["steps"]
    # the script runs last; LAVA writes params as non-exported shell vars, so they
    # must be exported first for the child script to inherit them
    assert steps[-1] == "lava-gateway-connect"
    export_step = next(s for s in steps if s.startswith("export "))
    exported = set(export_step.removeprefix("export ").split())
    assert set(testdef["params"]) <= exported
    # the script the test definition runs exists in the image build context
    assert (root / "interactive" / "lava-gateway-connect").exists()
    # the default test-definition path points at the file we ship
    assert Config(url="https://x").interactive_path == "interactive/ssh-gateway.yaml"


def _tool_names(cfg: Config) -> set[str]:
    server = build_server(cfg)
    return {t.name for t in asyncio.run(server.list_tools())}


def test_board_tools_registered_when_gateway_enabled() -> None:
    names = _tool_names(Config(url="https://x", gateway_enabled=True))
    assert {
        "open_board_session",
        "run_in_session",
        "close_board_session",
        "list_board_sessions",
        "attach_shell",
    } <= names


def test_board_tools_absent_without_gateway() -> None:
    names = _tool_names(Config(url="https://x"))
    assert "open_board_session" not in names


def test_ip_allowed_empty_allowlist_allows_all() -> None:
    assert ip_allowed("10.9.8.7", parse_networks(())) is True


def test_ip_allowed_matches_cidr_and_bare_ip() -> None:
    nets = parse_networks(("10.0.0.0/24", "192.168.1.5"))
    assert ip_allowed("10.0.0.9", nets) is True
    assert ip_allowed("192.168.1.5", nets) is True
    assert ip_allowed("192.168.1.6", nets) is False
    assert ip_allowed("172.16.0.1", nets) is False


def test_ip_allowed_normalises_ipv4_mapped_v6() -> None:
    assert ip_allowed("::ffff:10.0.0.9", parse_networks(("10.0.0.0/24",))) is True


def test_ip_allowed_rejects_unparseable_address() -> None:
    assert ip_allowed("not-an-ip", parse_networks(("10.0.0.0/8",))) is False


def test_gateway_ssh_server_rejects_unlisted_ip() -> None:
    srv = _GatewaySSHServer(SessionManager(), parse_networks(("10.0.0.0/24",)))
    conn = _FakeConn(("192.168.1.1", 40000))
    srv.connection_made(conn)
    assert conn.closed is True
    assert srv._allowed is False
    # even if auth is still attempted on the closing connection, it is denied
    assert srv.validate_public_key("s-whatever", object()) is False


def test_gateway_ssh_server_allows_listed_ip() -> None:
    srv = _GatewaySSHServer(SessionManager(), parse_networks(("10.0.0.0/24",)))
    conn = _FakeConn(("10.0.0.5", 40000))
    srv.connection_made(conn)
    assert conn.closed is False
    assert srv._allowed is True


def _connected_server(mgr: SessionManager) -> _GatewaySSHServer:
    srv = _GatewaySSHServer(mgr)
    srv.connection_made(_FakeConn(("10.0.0.1", 1)))
    return srv


def test_session_authorize_and_revoke_human_keys() -> None:
    s = SessionManager().create(kind="console")
    _, pub = generate_keypair()
    s.authorize_human(pub, ttl=3600)
    s.authorize_human(pub, ttl=3600)  # idempotent (refreshes expiry)
    assert s.active_human_keys() == [pub.strip()]
    assert s.public_view()["kind"] == "console"
    s.revoke_human_keys()
    assert s.active_human_keys() == []


def test_expired_human_key_is_rejected() -> None:
    mgr = SessionManager()
    s = mgr.create(kind="console")
    _, pub = generate_keypair()
    s.authorize_human(pub, ttl=-1)  # already expired
    assert s.active_human_keys() == []
    srv = _connected_server(mgr)
    assert not srv.validate_public_key(s.session_id, asyncssh.import_public_key(pub))


def test_gateway_ssh_server_agent_vs_human_roles() -> None:
    mgr = SessionManager()
    s = mgr.create(kind="console")

    agent = _connected_server(mgr)
    assert agent.validate_public_key(
        s.session_id, asyncssh.import_public_key(s.public_key)
    )
    assert agent._role == "agent"

    _, human_pub = generate_keypair()
    s.authorize_human(human_pub, ttl=3600)
    human = _connected_server(mgr)
    assert human.validate_public_key(
        s.session_id, asyncssh.import_public_key(human_pub)
    )
    assert human._role == "human"

    _, stranger_pub = generate_keypair()
    stranger = _connected_server(mgr)
    assert not stranger.validate_public_key(
        s.session_id, asyncssh.import_public_key(stranger_pub)
    )


def test_server_requested_binds_loopback_only() -> None:
    # SECURITY: the reverse tunnel must never bind to a public interface.
    mgr = SessionManager()
    s = mgr.create(kind="console")
    for host, ok in [
        ("127.0.0.1", True),
        ("localhost", True),
        ("::1", True),
        ("", False),
        ("0.0.0.0", False),
        ("10.0.0.1", False),
        ("::", False),
    ]:
        agent = _connected_server(mgr)
        agent.validate_public_key(
            s.session_id, asyncssh.import_public_key(s.public_key)
        )
        assert agent.server_requested(host, s.reverse_port) is ok, host


def test_gateway_role_gating_of_forwards() -> None:
    mgr = SessionManager()
    s = mgr.create(kind="console")

    # agent: may reverse-forward its own port, may not open a -W forward
    agent = _connected_server(mgr)
    agent.validate_public_key(s.session_id, asyncssh.import_public_key(s.public_key))
    assert agent.server_requested("127.0.0.1", s.reverse_port) is True
    assert agent.connection_requested("127.0.0.1", s.reverse_port, "x", 0) is False
    # agent must not reverse-forward some other port
    agent2 = _connected_server(mgr)
    agent2.validate_public_key(s.session_id, asyncssh.import_public_key(s.public_key))
    assert agent2.server_requested("127.0.0.1", s.reverse_port + 1) is False

    # human: may -W to the session port only, may not reverse-forward
    _, human_pub = generate_keypair()
    s.authorize_human(human_pub, ttl=3600)
    human = _connected_server(mgr)
    human.validate_public_key(s.session_id, asyncssh.import_public_key(human_pub))
    assert human.connection_requested("127.0.0.1", s.reverse_port, "x", 0) is True
    assert human.connection_requested("127.0.0.1", s.reverse_port + 1, "x", 0) is False
    assert human.connection_requested("10.0.0.9", s.reverse_port, "x", 0) is False
    assert human.server_requested("127.0.0.1", s.reverse_port) is False


def test_gateway_denies_shell_and_unix_channels() -> None:
    # SECURITY: the gateway offers no shell/exec/sftp or unix-socket forwarding.
    srv = _GatewaySSHServer(SessionManager())
    assert srv.session_requested() is False
    assert srv.unix_server_requested("/tmp/x") is False
    assert srv.unix_connection_requested("/tmp/x", "h", 0) is False


def test_gateway_attach_human_authorises_key() -> None:
    gw = Gateway(Config(url="https://x", gateway_advertise_host="gw.example.com"))
    s = gw.manager.create(kind="console")
    info = gw.attach_human(s.session_id)
    assert "PRIVATE KEY" in info["private_key"]
    assert info["gateway_host"] == "gw.example.com"
    assert info["reverse_port"] == s.reverse_port
    assert info["expires_in"] == 3600
    assert len(s.active_human_keys()) == 1


def test_console_tools_registered_when_gateway_enabled() -> None:
    names = _tool_names(Config(url="https://x", gateway_enabled=True))
    assert {
        "open_console_session",
        "attach_console",
        "close_console_session",
        "check_serial_console_support",
    } <= names


def test_gateway_integration_security_posture() -> None:
    """Drive a live gateway with a real asyncssh client to verify the SSH posture:
    the agent key authenticates and may reverse-forward its loopback port, but cannot
    open a shell on the gateway; a stranger key cannot authenticate at all."""
    port = free_port()
    gw = Gateway(
        Config(
            url="https://x",
            gateway_enabled=True,
            gateway_bind="127.0.0.1",
            gateway_port=port,
        )
    )
    session = gw.manager.create(kind="console")
    gw.ensure_started()

    async def scenario() -> None:
        agent_key = asyncssh.import_private_key(session.private_key)
        async with asyncssh.connect(
            "127.0.0.1",
            port,
            username=session.session_id,
            client_keys=[agent_key],
            known_hosts=None,
        ) as conn:
            # loopback reverse-forward on the allocated port is accepted
            listener = await conn.forward_remote_port(
                "127.0.0.1", session.reverse_port, "127.0.0.1", 2323
            )
            assert listener.get_port() == session.reverse_port
            listener.close()
            # no shell/exec is offered on the gateway itself
            with pytest.raises(asyncssh.Error):
                await conn.run("id", check=False)

        # a key not authorised for the session cannot authenticate
        stranger_priv, _ = generate_keypair()
        with pytest.raises(asyncssh.Error):
            async with asyncssh.connect(
                "127.0.0.1",
                port,
                username=session.session_id,
                client_keys=[asyncssh.import_private_key(stranger_priv)],
                known_hosts=None,
            ):
                pass

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(gw.stop())
