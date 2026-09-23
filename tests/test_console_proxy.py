"""Tests for the ser2net-proxy's TAC bridge (interactive/ser2net-proxy/console-proxy.py).

The proxy is a standalone script baked into its own image, so it is loaded by path
with TAC_API_URL pointed at a local stub of the pytactl REST service.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from lava_mcp.gateway import (
    CONSOLE_SETPORT_PREFIX,
    CONSOLE_TAC_PREFIX,
    generate_keypair,
    tac_control_token,
)

PROXY = Path(__file__).parent.parent / "interactive/ser2net-proxy/console-proxy.py"
# the console session key the proxy is started with (it also dials out with it)
SESSION_KEY, _ = generate_keypair()


class _TacStub(BaseHTTPRequestHandler):
    """Stand-in for the lab's pytactl REST service: records requests, echoes paths."""

    seen: list[tuple[str, str]] = []

    def _reply(self) -> None:
        self.seen.append((self.command, self.path))
        code = 500 if "broken" in self.path else 200
        body = f'{{"path": "{self.path}"}}'.encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_PUT = _reply

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture()
def tac_stub() -> Iterator[str]:
    _TacStub.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TacStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()


def _load_proxy(
    monkeypatch: pytest.MonkeyPatch, tac_url: str, with_key: bool = True
) -> ModuleType:
    monkeypatch.setenv("TAC_API_URL", tac_url)
    monkeypatch.setenv("TAC_TIMEOUT", "5")
    if with_key:
        key_b64 = base64.b64encode(SESSION_KEY.encode()).decode()
        monkeypatch.setenv("SESSION_PRIVATE_KEY_B64", key_b64)
    else:
        monkeypatch.delenv("SESSION_PRIVATE_KEY_B64", raising=False)
    spec = importlib.util.spec_from_file_location("console_proxy_under_test", PROXY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _control_exchange(proxy: ModuleType, line: bytes) -> bytes:
    """Send one control line to the proxy over a real socket; return its reply."""
    server = await asyncio.start_server(proxy.handle_watcher, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(line)
        await writer.drain()
        reply = await asyncio.wait_for(reader.read(), timeout=10)
        writer.close()
        return reply


def test_control_protocol_matches_the_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy = _load_proxy(monkeypatch, "http://tac-api:80")
    assert proxy.TAC_PREFIX == CONSOLE_TAC_PREFIX
    assert proxy.SETPORT_PREFIX == CONSOLE_SETPORT_PREFIX
    # the prefix sniffer reads len(SETPORT_PREFIX) bytes, so it must be the longest
    assert len(proxy.SETPORT_PREFIX) >= len(proxy.TAC_PREFIX)
    # the proxy derives the same token from its SESSION_PRIVATE_KEY_B64
    assert proxy.TAC_TOKEN == tac_control_token(SESSION_KEY.encode())


def test_tac_request_forwards_only_tac_routes(
    monkeypatch: pytest.MonkeyPatch, tac_stub: str
) -> None:
    proxy = _load_proxy(monkeypatch, tac_stub)
    assert proxy.tac_request("GET", "/S1/quick")[0] == 200
    assert proxy.tac_request("GET", "/S1")[0] == 200
    assert proxy.tac_request("PUT", "/S1/quick/powerOff")[0] == 200
    status, body = proxy.tac_request("PUT", "/S1/command/kpd_pwr?value=1")
    assert status == 200 and b"kpd_pwr?value=1" in body
    assert proxy.tac_request("PUT", "/S1/pin/D3?value=0")[0] == 200
    # the TAC service's own error status is passed through
    assert proxy.tac_request("GET", "/broken/quick")[0] == 500
    refused = [
        ("DELETE", "/S1/quick/powerOff"),
        ("POST", "/S1/quick/powerOff"),
        ("PUT", "/S1/quick"),  # no method named
        ("PUT", "/S1/command/kpd_pwr"),  # no value
        ("PUT", "/S1/command/kpd_pwr?value=2"),
        ("GET", "/S1/../admin"),
        ("GET", "/.."),
        ("GET", "/"),
        ("GET", "/S1/quick/powerOff?x=1"),
        ("PUT", "http://elsewhere/S1/quick/powerOn"),
    ]
    for method, path in refused:
        status, body = proxy.tac_request(method, path)
        assert status == 0 and b"refused" in body, (method, path)
    assert _TacStub.seen == [
        ("GET", "/S1/quick"),
        ("GET", "/S1"),
        ("PUT", "/S1/quick/powerOff"),
        ("PUT", "/S1/command/kpd_pwr?value=1"),
        ("PUT", "/S1/pin/D3?value=0"),
        ("GET", "/broken/quick"),
    ]


def test_tac_request_disabled_or_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    disabled = _load_proxy(monkeypatch, "")
    assert disabled.tac_request("GET", "/S1/quick") == (
        0,
        b"TAC bridge disabled (TAC_API_URL is empty)",
    )
    # nothing listens on port 9 (discard) on loopback in the test environment
    unreachable = _load_proxy(monkeypatch, "http://127.0.0.1:9")
    status, body = unreachable.tac_request("GET", "/S1/quick")
    assert status == 0 and b"unreachable" in body


def test_watcher_connection_answers_tac_control_line(
    monkeypatch: pytest.MonkeyPatch, tac_stub: str
) -> None:
    """End to end over a real socket, as the gateway does through the tunnel: a TAC
    control line gets "<status>\\n<body>" back and is never treated as a watcher."""
    proxy = _load_proxy(monkeypatch, tac_stub)
    token = tac_control_token(SESSION_KEY.encode())
    line = CONSOLE_TAC_PREFIX + f"{token} PUT /S1/command/kpd_pwr?value=0\n".encode()
    reply = asyncio.run(_control_exchange(proxy, line))
    status, _, body = reply.partition(b"\n")
    assert status == b"200"
    assert b"kpd_pwr?value=0" in body
    assert not proxy.watchers


def test_tac_control_line_requires_the_session_token(
    monkeypatch: pytest.MonkeyPatch, tac_stub: str
) -> None:
    """A human on the relay (attach_console) has no session key, so cannot drive the
    TAC — e.g. aim a request at another board on the lab's TAC service."""
    proxy = _load_proxy(monkeypatch, tac_stub)
    other, _ = generate_keypair()
    for line in (
        CONSOLE_TAC_PREFIX + b"PUT /OTHER/quick/powerOff\n",  # no token
        CONSOLE_TAC_PREFIX
        + f"{tac_control_token(other.encode())} PUT /OTHER/quick/powerOff\n".encode(),
    ):
        reply = asyncio.run(_control_exchange(proxy, line))
        assert reply.startswith(b"0\n"), reply
    assert _TacStub.seen == []
    # a relay-only proxy (no session key) refuses every TAC line
    keyless = _load_proxy(monkeypatch, tac_stub, with_key=False)
    token = tac_control_token(SESSION_KEY.encode())
    line = CONSOLE_TAC_PREFIX + f"{token} GET /S1/quick\n".encode()
    assert asyncio.run(_control_exchange(keyless, line)) == (
        b"0\nTAC control line not authorised"
    )
    assert _TacStub.seen == []
