from __future__ import annotations

import subprocess
import sys
import threading

import pytest

from lava_mcp.server import gateway_proxy_url, ws_proxy_hint
from lava_mcp.wsproxy import PROXY_SCRIPT


def test_proxy_script_is_valid_python() -> None:
    compile(PROXY_SCRIPT, "lava-ws-proxy", "exec")
    # it names its one dependency so a client knows what to install
    assert "websockets" in PROXY_SCRIPT
    assert PROXY_SCRIPT.startswith("#!/usr/bin/env python3")


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
