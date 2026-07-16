#!/usr/bin/env python3
"""ser2net console relay with a read-only-until-ready gate.

Runs as a LAVA Test Services container on the worker, started at the *beginning* of
the job. It connects to the board's ser2net console and relays it to connected
watchers so a user can watch deploy/boot from the start. User input is DROPPED until
the console-ready sentinel is seen in the console stream (emitted by the job's
console-ready test once the board has booted to a shell) — so the relay is strictly
read-only while LAVA drives the boot, and only becomes interactive afterwards.

Dependency-free (stdlib asyncio). Configured via environment (LAVA writes the job's
environment into the compose .env):

  SER2NET_HOST / SER2NET_PORT   console endpoint (default ser2net:7095)
  CONSOLE_LISTEN_PORT           port watchers connect to (default 2323)
  CONSOLE_READY_SENTINEL        string that unlocks writes (must match the job's echo)
"""
from __future__ import annotations

import asyncio
import os
import sys

SER2NET_HOST = os.environ.get("SER2NET_HOST", "ser2net")
SER2NET_PORT = int(os.environ.get("SER2NET_PORT", "7095"))
LISTEN_PORT = int(os.environ.get("CONSOLE_LISTEN_PORT", "2323"))
SENTINEL = os.environ.get("CONSOLE_READY_SENTINEL", "LAVA_MCP_CONSOLE_WRITABLE").encode()

console: dict = {"writer": None, "writable": False}
watchers: set[asyncio.StreamWriter] = set()


def log(msg: str) -> None:
    print(f"ser2net-proxy: {msg}", flush=True)


async def console_reader() -> None:
    """Hold a connection to ser2net, relay + log the console, watch for the sentinel."""
    backoff = 1
    tail = b""
    while True:
        try:
            log(f"connecting to console {SER2NET_HOST}:{SER2NET_PORT}")
            reader, writer = await asyncio.open_connection(SER2NET_HOST, SER2NET_PORT)
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


async def handle_watcher(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    log(f"watcher connected from {peer} (writes {'enabled' if console['writable'] else 'disabled'})")
    watchers.add(writer)
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            cw = console["writer"]
            if console["writable"] and cw is not None:
                cw.write(data)
                await cw.drain()
            # else: silently drop input while read-only
    except Exception:
        pass
    finally:
        watchers.discard(writer)
        writer.close()
        log(f"watcher {peer} disconnected")


async def main() -> None:
    server = await asyncio.start_server(handle_watcher, "0.0.0.0", LISTEN_PORT)
    log(f"listening for console watchers on :{LISTEN_PORT}")
    await asyncio.gather(console_reader(), server.serve_forever())


if __name__ == "__main__":
    asyncio.run(main())
