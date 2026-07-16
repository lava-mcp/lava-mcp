from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml

from lava_mcp.config import Config
from lava_mcp.gateway import (
    SessionManager,
    _GatewaySSHServer,
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
    assert job["tags"] == ["wifi"]
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
