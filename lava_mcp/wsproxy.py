"""The lava-mcp gateway ProxyCommand helper(s) — the stdio<->wss relay ssh uses.

Clients and in-job containers tunnel ssh to the board-session gateway over ``wss://``
(443, via Caddy). The relay for that leg is a dependency-light Python ``ProxyCommand``
needing only Python 3.8+ and the ``websockets`` package (Debian/Ubuntu:
``python3-websockets``; otherwise ``pip install websockets``) — packaged and maintained,
unlike websocat which it replaces.

Two forms, both built from the one relay core (``_RELAY_SRC``):

- ``PROXY_SCRIPT`` — a generic relay (URL on argv). The server serves it at
  ``<mcp>/gateway-proxy`` and the interactive / ser2net-proxy images bake it in as
  ``lava-ws-proxy`` (keyed by ``tests/`` to stay byte-identical to the committed copies).
- ``build_connect_script(...)`` — a per-session, self-contained connect script generated
  on the fly by the attach tools. It EMBEDS the session's ephemeral ssh key, so a client
  just saves it and runs ``python3 <file>`` to get the shell/console: run normally it
  writes the key to a temp file and execs ssh; re-invoked with ``--proxy`` (as its own
  ProxyCommand) it runs the relay. Because it carries a private key it is returned over
  the authenticated MCP channel, never served unauthenticated.
"""

from __future__ import annotations

import base64

# The relay core, shared verbatim by both forms. ``_run_relay(url)`` connects to the
# WebSocket and pipes this process's stdin/stdout to it as binary frames, exiting hard
# when either side ends (os.write to fd 1 is unbuffered, so received data is already
# out; a hard exit avoids wedging on a stdin read stuck in a worker thread).
_RELAY_SRC = r'''
def _run_relay(url):
    import asyncio
    import os
    import sys
    import warnings

    warnings.filterwarnings("ignore")  # keep ssh ProxyCommand stderr clean
    try:
        import websockets
    except ImportError:
        sys.stderr.write(
            "lava-ws-proxy: needs the 'websockets' package "
            "(apt install python3-websockets, or pip install websockets)\n"
        )
        sys.exit(1)

    async def _relay():
        try:
            ws = await websockets.connect(url, max_size=None, ping_interval=None)
        except Exception as exc:
            sys.stderr.write("lava-ws-proxy: cannot connect to %s: %s\n" % (url, exc))
            sys.exit(1)

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
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        os._exit(0)

    asyncio.run(_relay())
'''


PROXY_SCRIPT = (
    "#!/usr/bin/env python3\n"
    '"""lava-mcp gateway proxy: relay ssh stdio over a wss:// WebSocket.\n'
    "\n"
    "Dependency-light stand-in for `websocat -b <url>` as an ssh ProxyCommand:\n"
    '    ssh -o ProxyCommand="python3 lava-ws-proxy wss://HOST/mcp/gateway-ssh" ...\n'
    "\n"
    "Any extra args ssh appends (e.g. %h %p) are ignored; the first ws:// or wss://\n"
    "argument is used. Requires Python 3.8+ and the `websockets` package\n"
    "(apt install python3-websockets, or pip install websockets).\n"
    '"""\n'
    "import sys\n"
    + _RELAY_SRC
    + '''

def main():
    args = [a for a in sys.argv[1:] if a]
    url = next((a for a in args
                if a.startswith("ws://") or a.startswith("wss://")), None)
    if not url:
        sys.stderr.write("usage: lava-ws-proxy wss://HOST/mcp/gateway-ssh\\n")
        return 2
    try:
        _run_relay(url)
        return 0
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
'''
)


def build_connect_script(
    *,
    kind: str,
    ws_url: str,
    private_key: str,
    session_id: str,
    reverse_port: int,
    container_user: str | None = None,
    gateway_host: str | None = None,
) -> str:
    """Generate a self-contained, per-session connect script that embeds the ssh key.

    ``kind`` is ``"shell"`` (ProxyJump into the board container's sshd — needs
    ``container_user``) or ``"console"`` (``ssh -W`` to the reverse port — needs
    ``gateway_host``). Run ``python3 <file>`` and it opens the connection; it re-invokes
    itself as the ProxyCommand (``--proxy``) to run the relay. The embedded key makes the
    file secret, so it is only ever returned over the authenticated MCP channel.
    """
    key_b64 = base64.b64encode(private_key.encode()).decode()
    head = (
        "#!/usr/bin/env python3\n"
        '"""lava-mcp one-shot connect for session %s (%s).\n'
        "\n"
        "THIS FILE EMBEDS AN EPHEMERAL SSH PRIVATE KEY — treat it as a secret and\n"
        "delete it when done. Run it to open the %s:\n"
        "    python3 <thisfile>\n"
        "Needs python3, the `websockets` package (apt install python3-websockets, or\n"
        "pip install websockets) and the `ssh` client.\n"
        '"""\n' % (session_id, kind, "board shell" if kind == "shell" else "serial console")
    )
    consts = (
        f"WS_URL = {ws_url!r}\n"
        f"SESSION = {session_id!r}\n"
        f"REVERSE_PORT = {int(reverse_port)}\n"
        f"CONTAINER_USER = {container_user!r}\n"
        f"GATEWAY_HOST = {gateway_host!r}\n"
        f'PRIVATE_KEY_B64 = "{key_b64}"\n'
    )
    # proxy mode first: when ssh invokes this file as its ProxyCommand, run the relay
    # against the embedded URL and nothing else.
    proxy_mode = (
        "import sys\n"
        'if "--proxy" in sys.argv:\n'
        "    _run_relay(WS_URL)\n"
        "    sys.exit(0)\n"
    )
    if kind == "shell":
        launcher = r'''
import base64
import os
import tempfile

keyfd, keypath = tempfile.mkstemp(prefix="lava-key-")
os.write(keyfd, base64.b64decode(PRIVATE_KEY_B64))
os.close(keyfd)
os.chmod(keypath, 0o600)
self = os.path.abspath(sys.argv[0])
conf = (
    "Host gw\n"
    "    User %s\n" % SESSION +
    "    IdentityFile %s\n" % keypath +
    "    ProxyCommand python3 %s --proxy\n" % self +
    "    StrictHostKeyChecking no\n"
    "    UserKnownHostsFile /dev/null\n"
    "Host board\n"
    "    HostName 127.0.0.1\n"
    "    Port %d\n" % REVERSE_PORT +
    "    User %s\n" % CONTAINER_USER +
    "    IdentityFile %s\n" % keypath +
    "    ProxyJump gw\n"
    "    StrictHostKeyChecking no\n"
    "    UserKnownHostsFile /dev/null\n"
)
conffd, confpath = tempfile.mkstemp(prefix="lava-conf-")
os.write(conffd, conf.encode())
os.close(conffd)
os.execvp("ssh", ["ssh", "-F", confpath, "board"])
'''
    else:
        launcher = r'''
import base64
import os
import tempfile

keyfd, keypath = tempfile.mkstemp(prefix="lava-key-")
os.write(keyfd, base64.b64decode(PRIVATE_KEY_B64))
os.close(keyfd)
os.chmod(keypath, 0o600)
self = os.path.abspath(sys.argv[0])
os.execvp("ssh", [
    "ssh", "-i", keypath,
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ProxyCommand=python3 %s --proxy" % self,
    "-W", "127.0.0.1:%d" % REVERSE_PORT,
    "%s@%s" % (SESSION, GATEWAY_HOST),
])
'''
    return head + consts + _RELAY_SRC + proxy_mode + launcher
