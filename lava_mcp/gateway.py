"""Interactive board-session gateway.

When ``lava-mcp`` runs as a hosted service it can act as an SSH rendezvous point:
a LAVA job starts a device-attached container that dials OUT to this gateway with
``ssh -R`` (so no inbound access to the lab worker is needed). The gateway then
runs commands inside that container over the reverse tunnel.

One reverse tunnel = one session = one job. A per-session ed25519 keypair is minted
here and handed to the job; the same key authenticates the container's outbound
tunnel *and* the gateway's commands back into the container (symmetric trust).

The SSH server runs in its own dedicated thread + event loop, independent of the
MCP/uvicorn request lifecycle (an asyncssh listener created inside a request task
gets torn down when that task ends). Cross-loop calls go through
``run_coroutine_threadsafe``; the per-session "connected" signal is a
``threading.Event`` so it is safe to set/await across loops.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import asyncssh

from .config import Config


class GatewayError(RuntimeError):
    """Raised for interactive-session/gateway failures."""


def generate_keypair() -> tuple[str, str]:
    """Return (private_key_openssh, public_key_openssh) for a fresh ed25519 key."""
    key = asyncssh.generate_private_key("ssh-ed25519")
    private = key.export_private_key().decode()
    public = key.export_public_key().decode().strip()
    return private, public


def free_port() -> int:
    """Pick a currently-free TCP port to assign to a session's reverse tunnel."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return int(port)


@dataclass
class BoardSession:
    session_id: str
    private_key: str
    public_key: str
    reverse_port: int
    container_user: str = "root"
    device_type: str | None = None
    job_id: int | None = None
    status: str = "pending"  # pending -> connected -> closed
    created: float = field(default_factory=time.time)
    # set (from the gateway loop) when the container's reverse tunnel registers
    _connected: threading.Event = field(default_factory=threading.Event, repr=False)

    def public_view(self) -> dict[str, Any]:
        """Session info safe to return to a client (no private key)."""
        return {
            "session_id": self.session_id,
            "job_id": self.job_id,
            "device_type": self.device_type,
            "status": self.status,
            "reverse_port": self.reverse_port,
        }


class SessionManager:
    """In-memory registry of board sessions, keyed by session id."""

    def __init__(self) -> None:
        self.sessions: dict[str, BoardSession] = {}

    def create(self, device_type: str | None = None) -> BoardSession:
        private, public = generate_keypair()
        session = BoardSession(
            session_id="s-" + uuid.uuid4().hex[:12],
            private_key=private,
            public_key=public,
            reverse_port=free_port(),
            device_type=device_type,
        )
        self.sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> BoardSession | None:
        return self.sessions.get(session_id)

    def remove(self, session_id: str) -> BoardSession | None:
        return self.sessions.pop(session_id, None)

    def list(self) -> list[BoardSession]:
        return list(self.sessions.values())


class _GatewaySSHServer(asyncssh.SSHServer):
    """Per-connection SSH server: authenticates a session key and accepts its
    reverse port-forward request."""

    def __init__(self, manager: SessionManager) -> None:
        self._manager = manager
        self._username: str | None = None

    def begin_auth(self, username: str) -> bool:
        self._username = username
        return True  # require key auth below

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        session = self._manager.get(username)
        if session is None:
            return False
        try:
            authorized = asyncssh.import_public_key(session.public_key)
        except (asyncssh.KeyImportError, ValueError):
            return False
        return key == authorized

    def server_requested(self, listen_host: str, listen_port: int) -> bool:
        # Container requests `ssh -R <reverse_port>:localhost:22`. Only accept the
        # port we pre-allocated for this authenticated session.
        session = self._manager.get(self._username or "")
        if session is None or listen_port != session.reverse_port:
            return False
        session.status = "connected"
        session._connected.set()
        return True


class Gateway:
    """Hosts the SSH rendezvous server (own thread/loop) and runs commands over
    session tunnels."""

    def __init__(self, config: Config, manager: SessionManager | None = None) -> None:
        self.config = config
        self.manager = manager or SessionManager()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: asyncssh.SSHAcceptor | None = None
        self._lock = threading.Lock()

    # -- lifecycle (own thread + loop) -------------------------------------
    def ensure_started(self) -> None:
        """Start the SSH listener in a dedicated background loop (idempotent)."""
        with self._lock:
            if self._loop is not None:
                return
            loop = asyncio.new_event_loop()
            ready = threading.Event()
            error: list[BaseException] = []

            def run() -> None:
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(self._start_server())
                except BaseException as exc:  # surface bind errors to the caller
                    error.append(exc)
                    ready.set()
                    return
                ready.set()
                loop.run_forever()

            thread = threading.Thread(target=run, name="lava-mcp-gateway", daemon=True)
            thread.start()
            if not ready.wait(timeout=15):
                raise GatewayError("gateway SSH server did not start in time")
            if error:
                raise GatewayError(f"gateway SSH server failed to start: {error[0]}")
            self._loop = loop
            self._thread = thread

    async def _start_server(self) -> None:
        host_key = asyncssh.generate_private_key("ssh-ed25519")
        self._server = await asyncssh.create_server(
            lambda: _GatewaySSHServer(self.manager),
            self.config.gateway_bind,
            self.config.gateway_port,
            server_host_keys=[host_key],
        )

    async def stop(self) -> None:
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        self._loop = None

    # -- operations (submitted to the gateway loop) ------------------------
    def _submit(self, coro: Any) -> Any:
        if self._loop is None:
            raise GatewayError("gateway is not started")
        return asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, self._loop))

    async def wait_connected(self, session_id: str, timeout: float) -> bool:
        session = self.manager.get(session_id)
        if session is None:
            raise GatewayError(f"unknown session {session_id}")
        return await asyncio.get_event_loop().run_in_executor(
            None, session._connected.wait, timeout
        )

    async def run(
        self, session_id: str, command: str, timeout: float = 120
    ) -> dict[str, Any]:
        return await self._submit(self._run(session_id, command, timeout))

    async def _run(
        self, session_id: str, command: str, timeout: float
    ) -> dict[str, Any]:
        session = self.manager.get(session_id)
        if session is None:
            raise GatewayError(f"unknown session {session_id}")
        if session.status != "connected":
            raise GatewayError(f"session {session_id} is not connected yet")
        try:
            async with asyncssh.connect(
                "127.0.0.1",
                session.reverse_port,
                username=session.container_user,
                client_keys=[asyncssh.import_private_key(session.private_key)],
                known_hosts=None,
            ) as conn:
                result = await conn.run(command, timeout=timeout)
        except (asyncssh.Error, OSError, asyncio.TimeoutError) as exc:
            raise GatewayError(f"command failed in session {session_id}: {exc}")
        return {
            "exit_status": result.exit_status,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
