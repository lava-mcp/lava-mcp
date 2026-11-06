from __future__ import annotations

import pathlib
import subprocess
import sys
import threading

import pytest

from lava_mcp.server import gateway_proxy_url, ws_proxy_hint
from lava_mcp.wsproxy import PROXY_SCRIPT, build_connect_script

_REPO = pathlib.Path(__file__).resolve().parents[1]


def test_proxy_script_is_valid_python() -> None:
    compile(PROXY_SCRIPT, "lava-ws-proxy", "exec")
    # it names its one dependency so a client knows what to install
    assert "websockets" in PROXY_SCRIPT
    assert PROXY_SCRIPT.startswith("#!/usr/bin/env python3")


def test_baked_image_copies_match_proxy_script() -> None:
    # the interactive and ser2net-proxy images bake lava-ws-proxy; it must stay
    # byte-identical to the canonical PROXY_SCRIPT the server serves.
    for rel in ("interactive/lava-ws-proxy",
                "interactive/ser2net-proxy/lava-ws-proxy"):
        assert (_REPO / rel).read_text() == PROXY_SCRIPT, rel


def test_no_websocat_left_in_image_build_inputs() -> None:
    # the cutover stopped fetching/installing websocat and stopped invoking it; the
    # connect scripts now use lava-ws-proxy (a passing mention in a Dockerfile comment
    # that it "replaces websocat" is fine — we only reject real usage).
    for rel in ("interactive/Dockerfile", "interactive/ser2net-proxy/Dockerfile"):
        text = (_REPO / rel).read_text()
        assert "WEBSOCAT_VERSION" not in text, rel  # no pinned-binary fetch
        assert "releases/download" not in text, rel  # no GitHub binary download
        assert "lava-ws-proxy" in text, rel  # bakes the python proxy instead
    for rel in ("interactive/lava-gateway-connect",
                "interactive/ser2net-proxy/console-connect.sh"):
        text = (_REPO / rel).read_text()
        assert "websocat" not in text, rel
        assert "ProxyCommand=lava-ws-proxy" in text, rel


def test_build_connect_script_embeds_key_and_both_modes() -> None:
    key = "-----BEGIN OPENSSH PRIVATE KEY-----\nSECRET\n-----END OPENSSH PRIVATE KEY-----\n"
    shell = build_connect_script(
        kind="shell", ws_url="wss://h/mcp/gateway-ssh", private_key=key,
        session_id="s1", reverse_port=22001, container_user="root",
    )
    console = build_connect_script(
        kind="console", ws_url="wss://h/mcp/gateway-ssh", private_key=key,
        session_id="c1", reverse_port=22002, gateway_host="gw.example",
    )
    import base64

    for src in (shell, console):
        compile(src, "connect", "exec")
        assert '"--proxy" in sys.argv' in src  # self-invoked ProxyCommand mode
        assert "os.execvp(\"ssh\"" in src  # launches ssh
        # the raw key is NOT in cleartext; it's base64-embedded and decoded at runtime
        assert "SECRET" not in src
        assert base64.b64encode(key.encode()).decode() in src
    assert "ProxyJump gw" in shell  # shell hops gateway -> board
    # console is a single ssh -W hop; session@host is assembled at runtime from the
    # embedded constants
    assert "-W" in console
    assert "SESSION = 'c1'" in console and "GATEWAY_HOST = 'gw.example'" in console


def test_gateway_proxy_url_derivation() -> None:
    assert (
        gateway_proxy_url("wss://h.example/mcp/gateway-ssh")
        == "https://h.example/mcp/gateway-proxy"
    )
    assert (
        gateway_proxy_url("ws://h.example/mcp/gateway-ssh")
        == "http://h.example/mcp/gateway-proxy"
    )
    assert gateway_proxy_url("") == ""


def test_ws_proxy_hint_names_url_and_dependency() -> None:
    hint = ws_proxy_hint("wss://h.example/mcp/gateway-ssh")
    assert hint["url"] == "https://h.example/mcp/gateway-proxy"
    assert "websockets" in hint["requires"]
    assert "python3-websockets" in hint["requires"]


def test_gateway_proxy_route_served_when_gateway_enabled() -> None:
    from lava_mcp.config import Config
    from lava_mcp.server import build_server

    mcp = build_server(Config(url="https://x", gateway_enabled=True))
    paths = [getattr(r, "path", "") for r in mcp._custom_starlette_routes]
    assert any(p.endswith("/gateway-proxy") for p in paths)
    # and not served when the gateway is off
    off = build_server(Config(url="https://x", gateway_enabled=False))
    off_paths = [getattr(r, "path", "") for r in off._custom_starlette_routes]
    assert not any(p.endswith("/gateway-proxy") for p in off_paths)


def test_proxy_relays_stdio_over_websocket(tmp_path) -> None:
    """End-to-end: the script pipes stdin->ws and ws->stdout against an echo server."""
    serve = pytest.importorskip("websockets.sync.server").serve

    def echo(ws) -> None:
        for message in ws:
            ws.send(message)

    server = serve(echo, "127.0.0.1", 0)
    port = server.socket.getsockname()[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    script = tmp_path / "lava-ws-proxy"
    script.write_text(PROXY_SCRIPT)

    proc = subprocess.Popen(
        [sys.executable, str(script), f"ws://127.0.0.1:{port}"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(b"hello over ws\n")
        proc.stdin.flush()
        # read the echoed bytes back off stdout, with a watchdog so a hang fails fast
        out: list[bytes] = []
        reader = threading.Thread(target=lambda: out.append(proc.stdout.read(14)))
        reader.start()
        reader.join(timeout=10)
        assert not reader.is_alive(), "proxy did not relay within 10s"
        assert out == [b"hello over ws\n"]
    finally:
        proc.kill()
        proc.wait(timeout=5)
        server.shutdown()
