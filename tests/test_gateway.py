from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from lava_mcp.config import Config
from lava_mcp.gateway import SessionManager, generate_keypair
from lava_mcp.jobs import build_interactive_job
from lava_mcp.server import build_server


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
    assert testdef["run"]["steps"] == ["lava-gateway-connect"]
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
