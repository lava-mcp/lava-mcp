"""The lava-mcp gateway ProxyCommand helper, and the text the server serves it as.

Clients tunnel ssh to the board-session gateway over ``wss://`` (443, via Caddy). The
long-standing client for that leg is ``websocat``, but websocat is an unpackaged static
binary (not in Debian) that each client has to fetch. This module ships a
dependency-light alternative: a stdio<->WebSocket relay usable as an ssh
``ProxyCommand``, needing only Python 3.8+ and the ``websockets`` package — both
actively maintained and, unlike websocat, packaged (Debian/Ubuntu: ``python3-websockets``;
otherwise ``pip install websockets``).

``PROXY_SCRIPT`` is the single source of truth. The server serves it verbatim at
``<mcp>/gateway-proxy`` (see ``server._register_gateway_proxy_route``) so a client can
fetch it with ``curl`` and point ssh at it; the same text is what we would bake into an
image to replace websocat. It is kept as a string (not a separate executable file) so
the installed server can always serve it without packaging a script outside the package.
"""

from __future__ import annotations

PROXY_SCRIPT = r'''#!/usr/bin/env python3
"""lava-mcp gateway proxy: relay ssh stdio over a wss:// WebSocket.

A dependency-light stand-in for `websocat -b <url>` as an ssh ProxyCommand. Point ssh at
it with the gateway's wss:// URL:

    ssh -o ProxyCommand="python3 /path/to/lava-ws-proxy wss://HOST/mcp/gateway-ssh" \
        -o StrictHostKeyChecking=no user@placeholder

It connects to the WebSocket and pipes this process's stdin/stdout to it as binary
frames. Any extra ProxyCommand args (e.g. %h %p that ssh may append) are ignored; the
first ws:// or wss:// argument is used.

Requires Python 3.8+ and the `websockets` package:
    Debian/Ubuntu:  apt install python3-websockets
    anywhere:       pip install websockets
"""
import asyncio
import os
import sys
import warnings

# Keep ssh's ProxyCommand stderr clean (e.g. websockets' legacy-API deprecation notice).
warnings.filterwarnings("ignore")

try:
    import websockets
except ImportError:
    sys.stderr.write(
        "lava-ws-proxy: the 'websockets' package is required "
        "(apt install python3-websockets, or pip install websockets)\n"
    )
    sys.exit(1)


async def _relay(url):
    try:
        ws = await websockets.connect(url, max_size=None, ping_interval=None)
    except Exception as exc:  # surface any connect failure to ssh as a non-zero exit
        sys.stderr.write("lava-ws-proxy: cannot connect to %s: %s\n" % (url, exc))
        return 1

    async def ws_to_stdout():
        try:
            async for msg in ws:
                if isinstance(msg, str):
                    msg = msg.encode()
                data = memoryview(msg)
                while data:
                    data = data[os.write(1, data):]
        except Exception:
            pass

    async def stdin_to_ws():
        loop = asyncio.get_event_loop()
        try:
            while True:
                chunk = await loop.run_in_executor(None, os.read, 0, 65536)
                if not chunk:
                    break
                await ws.send(chunk)
        except Exception:
            pass
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    tasks = [asyncio.ensure_future(ws_to_stdout()),
             asyncio.ensure_future(stdin_to_ws())]
    # Whichever side ends first (remote close, or local EOF) ends the tunnel. os.write to
    # fd 1 is unbuffered, so everything received is already on stdout; exit hard so a
    # stdin read still blocked in a worker thread can't wedge interpreter shutdown.
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    os._exit(0)


def main():
    args = [a for a in sys.argv[1:] if a]
    url = next((a for a in args
                if a.startswith("ws://") or a.startswith("wss://")), None)
    if not url:
        sys.stderr.write("usage: lava-ws-proxy wss://HOST/mcp/gateway-ssh\n")
        return 2
    try:
        return asyncio.run(_relay(url))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
'''
