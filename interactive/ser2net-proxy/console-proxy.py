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

Dependency-free (stdlib asyncio). Configured via environment (LAVA writes the job's
environment into the compose .env):

  SER2NET_HOST / SER2NET_PORT   console endpoint (optional; else delivered via SETPORT)
  CONSOLE_LISTEN_PORT           port watchers connect to (default 2323)
  CONSOLE_READY_SENTINEL        string that unlocks writes (must match the job's echo)
  CONSOLE_INPUT_CHAR_DELAY      per-character gap (s) when writing user input to the
                                board, so a slow UART doesn't drop chars (default 0.05)
"""

from __future__ import annotations

import asyncio
import os
import sys

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


async def handle_watcher(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    peer = writer.get_extra_info("peername")
    head = await _read_control_prefix(reader)
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
