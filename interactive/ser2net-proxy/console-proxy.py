#!/usr/bin/env python3
"""ser2net console relay with a read-only-until-ready gate.

Runs as a LAVA Test Services container on the worker, started at the *beginning* of
the job. It connects to the board's ser2net console and relays it to connected
watchers so a user can watch deploy/boot from the start. User input is DROPPED until
the console-ready sentinel is seen in the console stream (emitted by the job's
console-ready test once the board has booted to a shell) — so the relay is strictly
read-only while LAVA drives the boot, and only becomes interactive afterwards.

The ser2net endpoint can arrive two ways:

  * SER2NET_HOST/SER2NET_PORT in the environment — used when the submitter already
    knows the port (e.g. a standalone console job that pins the port in its job def).
  * a SETPORT control line pushed in at runtime over a watcher connection — used by a
    board session, where the port is per-board and only known once LAVA schedules the
    job. The proxy waits (read-only, no console yet) until the endpoint is delivered.

The proxy also bridges the lab's TAC REST API (the pytactl service that drives the
board's debug board / Alpaca: power, pins such as the power key, boot modes). Like
ser2net it lives on the dispatcher network, out of reach of the MCP server, so the
gateway sends a TAC control line over the same reverse tunnel and the proxy performs
that one HTTP request and writes the response back. Only TAC routes are accepted.

Dependency-free (stdlib asyncio). Configured via environment (LAVA writes the job's
environment into the compose .env):

  SER2NET_HOST / SER2NET_PORT   console endpoint (optional; else delivered via SETPORT)
  CONSOLE_LISTEN_PORT           port watchers connect to (default 2323)
  CONSOLE_READY_SENTINEL        string that unlocks writes (must match the job's echo)
  CONSOLE_INPUT_CHAR_DELAY      per-character gap (s) when writing user input to the
                                board, so a slow UART doesn't drop chars (default 0.05)
  TAC_API_URL                   base URL of the lab's TAC REST service (default
                                http://tac-api:80; empty disables TAC requests)
  SESSION_PRIVATE_KEY_B64       the session key (also used to dial out); TAC control
                                lines must carry a token derived from it
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import re
import sys
import urllib.error
import urllib.request

LISTEN_PORT = int(os.environ.get("CONSOLE_LISTEN_PORT", "2323"))
SENTINEL = os.environ.get(
    "CONSOLE_READY_SENTINEL", "LAVA_MCP_CONSOLE_WRITABLE"
).encode()
# Pace user input to the board one byte at a time with this gap (seconds). A slow
# UART/getty drops characters if fed too fast (e.g. a pasted command), so trickle
# them. 0 disables pacing. Applies only to watcher->board writes, not the console
# output relayed back.
INPUT_CHAR_DELAY = float(os.environ.get("CONSOLE_INPUT_CHAR_DELAY", "0.05"))

# Control line the gateway pushes over a watcher connection to set the endpoint at
# runtime (must match lava_mcp.gateway.CONSOLE_SETPORT_PREFIX). Followed by
# "<host> <port>\n".
SETPORT_PREFIX = b"\x00LAVA-MCP-SETPORT "

# Control line the gateway pushes to make one TAC REST request (must match
# lava_mcp.gateway.CONSOLE_TAC_PREFIX). Followed by "<token> <METHOD> <path>\n" (see
# tac_control_token); the proxy answers "<status>\n<body>" and closes. Status 0 means
# the proxy refused or could not make the request (the body says why).
TAC_PREFIX = b"\x00LAVA-MCP-TAC "
TAC_API_URL = os.environ.get("TAC_API_URL", "http://tac-api:80").rstrip("/")
TAC_TIMEOUT = float(os.environ.get("TAC_TIMEOUT", "30"))
# The pytactl REST routes a session may use: read the board, run a quick method
# (powerOn, bootToEDL, ...) or set a pin by command name or pin id. Nothing else on
# the TAC service is reachable through the proxy.
TAC_GET_RE = re.compile(r"^/[A-Za-z0-9_-]+(/(quick|command|pin)(/[A-Za-z0-9_]+)?)?$")
TAC_PUT_RE = re.compile(
    r"^/[A-Za-z0-9_-]+(/quick/[A-Za-z0-9_]+|/(command|pin)/[A-Za-z0-9_]+\?value=[01])$"
)


def tac_control_token(private_key_pem: bytes) -> str:
    """Token a TAC control line must carry (must match lava_mcp.gateway).

    Derived from the session's private key, which the proxy holds (to dial out) and
    the gateway minted, but a human given attach_console access to the relay never
    sees. So only the gateway can drive the TAC: it resolves the job's own board's
    serial server-side, and a human on the relay cannot aim requests at another board.
    """
    return hashlib.sha256(b"lava-mcp-tac:" + private_key_pem).hexdigest()


# no session key (relay-only, no gateway) -> no token -> TAC requests are refused
_key_b64 = os.environ.get("SESSION_PRIVATE_KEY_B64", "")
TAC_TOKEN = tac_control_token(base64.b64decode(_key_b64)) if _key_b64 else None

# When the sentinel is empty there is no boot to gate on (e.g. a board session, where
# nothing drives the console), so the console is writable from the start.
console: dict = {"writer": None, "writable": not SENTINEL, "host": None, "port": None}
# set once an endpoint is known (from env or a SETPORT push); console_reader waits on it
endpoint_ready = asyncio.Event()
watchers: set[asyncio.StreamWriter] = set()


def log(msg: str) -> None:
    print(f"ser2net-proxy: {msg}", flush=True)


def set_endpoint(host: str, port: str) -> None:
    """Record the ser2net endpoint and wake console_reader (idempotent)."""
    try:
        port_int = int(port)
    except (TypeError, ValueError):
        log(f"ignoring invalid console endpoint {host!r}:{port!r}")
        return
    if (host, port_int) == (console["host"], console["port"]):
        return
    console["host"], console["port"] = host, port_int
    log(f"console endpoint set to {host}:{port_int}")
    endpoint_ready.set()


async def console_reader() -> None:
    """Hold a connection to ser2net, relay + log the console, watch for the sentinel."""
    backoff = 1
    tail = b""
    await endpoint_ready.wait()
    while True:
        host, port = console["host"], console["port"]
        try:
            log(f"connecting to console {host}:{port}")
            reader, writer = await asyncio.open_connection(host, port)
            console["writer"] = writer
            backoff = 1
            log("console connected (read-only until console-ready sentinel)")
            while True:
                data = await reader.read(4096)
                if not data:
                    log("console closed by ser2net")
                    break
                # 'watch': surface the console in this container's docker logs
                sys.stdout.buffer.write(data)
                sys.stdout.flush()
                # fan out to connected watchers
                for w in list(watchers):
                    try:
                        w.write(data)
                    except Exception:
                        watchers.discard(w)
                # unlock writes once the board signals it has booted to a shell
                if not console["writable"]:
                    tail = (tail + data)[-4096:]
                    if SENTINEL in tail:
                        console["writable"] = True
                        log("console-ready sentinel seen — user writes ENABLED")
        except Exception as exc:  # keep trying; never crash the container
            log(f"console connection error: {exc}")
        finally:
            console["writer"] = None
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 15)


async def _read_control_prefix(reader: asyncio.StreamReader) -> bytes:
    """Read up to len(SETPORT_PREFIX) bytes with a short deadline.

    SETPORT_PREFIX is the longest control prefix, so this is enough to recognise any
    control line (a TAC line's head then also carries the start of its request).
    A control connection sends the prefix immediately; a console watcher usually sends
    nothing (it only reads) or a few keystrokes. We accumulate until we have enough to
    compare, or a brief timeout elapses, so a fragmented control write is not
    misread. The bytes are returned so a watcher's early input is not lost.
    """
    head = b""
    try:
        while len(head) < len(SETPORT_PREFIX):
            chunk = await asyncio.wait_for(
                reader.read(len(SETPORT_PREFIX) - len(head)), timeout=0.3
            )
            if not chunk:
                break
            head += chunk
    except asyncio.TimeoutError:
        pass
    return head


def tac_request(method: str, path: str) -> tuple[int, bytes]:
    """Perform one TAC REST request (blocking); return (status, body).

    Status 0 means the request was refused or could not be made; the body explains.
    Only the pytactl routes in TAC_GET_RE / TAC_PUT_RE are forwarded.
    """
    if not TAC_API_URL:
        return 0, b"TAC bridge disabled (TAC_API_URL is empty)"
    allowed = {"GET": TAC_GET_RE, "PUT": TAC_PUT_RE}.get(method)
    if allowed is None or not allowed.match(path):
        return 0, f"refused TAC request {method} {path!r}".encode()
    req = urllib.request.Request(TAC_API_URL + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TAC_TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        return 0, f"TAC service unreachable at {TAC_API_URL}: {exc}".encode()


async def _handle_tac(line: bytes, writer: asyncio.StreamWriter) -> None:
    """Answer a TAC control line ("<token> <METHOD> <path>") with
    "<status>\\n<body>"."""
    parts = line.decode(errors="replace").split()
    if len(parts) != 3:
        status, body = 0, b"malformed TAC control line"
    elif TAC_TOKEN is None or not hmac.compare_digest(parts[0], TAC_TOKEN):
        status, body = 0, b"TAC control line not authorised"
        log("refused an unauthorised TAC control line")
    else:
        status, body = await asyncio.to_thread(tac_request, parts[1], parts[2])
        log(f"TAC {parts[1]} {parts[2]} -> {status}")
    writer.write(f"{status}\n".encode() + body)
    await writer.drain()


async def handle_watcher(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    peer = writer.get_extra_info("peername")
    head = await _read_control_prefix(reader)
    if head.startswith(TAC_PREFIX):
        # a TAC REST request relayed from the gateway, not a watcher
        line = head[len(TAC_PREFIX) :]
        if b"\n" not in line:
            line += await reader.readline()
        try:
            await _handle_tac(line.split(b"\n", 1)[0], writer)
        finally:
            writer.close()
        return
    if head == SETPORT_PREFIX:
        # runtime endpoint delivery, not a real watcher: read "<host> <port>\n"
        rest = (await reader.readline()).decode(errors="replace").split()
        if len(rest) >= 2:
            set_endpoint(rest[0], rest[1])
        else:
            log(f"malformed SETPORT control line: {rest!r}")
        writer.close()
        return
    log(
        f"watcher connected from {peer} "
        f"(writes {'enabled' if console['writable'] else 'disabled'})"
    )
    watchers.add(writer)
    try:
        if head:
            await _to_board(head)  # early input read while sniffing for control
        while True:
            data = await reader.read(4096)
            if not data:
                break
            await _to_board(data)
    except Exception:
        pass
    finally:
        watchers.discard(writer)
        writer.close()
        log(f"watcher {peer} disconnected")


async def _to_board(data: bytes) -> None:
    """Trickle watcher input to the board so a slow UART/getty doesn't drop chars."""
    cw = console["writer"]
    if not console["writable"] or cw is None:
        return  # silently drop input while read-only or before the console is up
    for i in range(len(data)):
        cw.write(data[i : i + 1])
        await cw.drain()
        if INPUT_CHAR_DELAY:
            await asyncio.sleep(INPUT_CHAR_DELAY)


async def main() -> None:
    host = os.environ.get("SER2NET_HOST")
    port = os.environ.get("SER2NET_PORT")
    if host and port:
        set_endpoint(host, port)  # eager: submitter pinned the endpoint
    else:
        log("no ser2net endpoint yet — waiting for a SETPORT control push")
    server = await asyncio.start_server(handle_watcher, "0.0.0.0", LISTEN_PORT)
    log(f"listening for console watchers on :{LISTEN_PORT}")
    await asyncio.gather(console_reader(), server.serve_forever())


if __name__ == "__main__":
    asyncio.run(main())
