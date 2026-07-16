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
import ipaddress
import socket
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import asyncssh

from .config import Config

_Network = ipaddress.IPv4Network | ipaddress.IPv6Network


class GatewayError(RuntimeError):
    """Raised for interactive-session/gateway failures."""


def parse_networks(entries: Iterable[str]) -> list[_Network]:
    """Parse IP/CIDR allowlist entries into networks (a bare IP becomes a /32 or /128)."""
    return [ipaddress.ip_network(e, strict=False) for e in entries if e]


def ip_allowed(ip: str, networks: list[_Network]) -> bool:
    """True if ``ip`` falls in any allowlisted network. Empty allowlist = allow all."""
    if not networks:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    # a v4 client on a dual-stack listener shows up as ::ffff:a.b.c.d
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return any(addr in net for net in networks)


def generate_keypair() -> tuple[str, str]:
    """Return (private_key_openssh, public_key_openssh) for a fresh ed25519 key."""
    key = asyncssh.generate_private_key("ssh-ed25519")
    private = key.export_private_key().decode()
    public = key.export_public_key().decode().strip()
    return private, public


def _key_matches(key: asyncssh.SSHKey, authorized_pub: str) -> bool:
    """True if ``key`` equals the public key encoded in ``authorized_pub``."""
    try:
        authorized = asyncssh.import_public_key(authorized_pub)
    except (asyncssh.KeyImportError, ValueError):
        return False
    return key == authorized


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
    # "container" (Mode 1: reverse_port -> board container sshd) or
    # "console" (Mode 2: reverse_port -> ser2net console relay in the job container)
    kind: str = "container"
    container_user: str = "root"
    device_type: str | None = None
    job_id: int | None = None
    # LAVA username that opened the session; only the owner may operate on it
    owner: str | None = None
    status: str = "pending"  # pending -> connected -> closed
    created: float = field(default_factory=time.time)
    # short-lived public keys authorised for human access, mapped to expiry (epoch s)
    human_keys: dict[str, float] = field(default_factory=dict)
    # set (from the gateway loop) when the container's reverse tunnel registers
    _connected: threading.Event = field(default_factory=threading.Event, repr=False)

    def authorize_human(self, public_key: str, ttl: float) -> float:
        """Authorise an ephemeral human public key for ``ttl`` seconds; return expiry."""
        pub = public_key.strip()
        expires = time.time() + ttl
        if pub:
            self.human_keys[pub] = expires
        return expires

    def active_human_keys(self) -> list[str]:
        """Authorised human public keys that have not expired."""
        now = time.time()
        return [pub for pub, exp in self.human_keys.items() if exp > now]

    def revoke_human_keys(self) -> None:
        self.human_keys.clear()

    def public_view(self) -> dict[str, Any]:
        """Session info safe to return to a client (no private key)."""
        return {
            "session_id": self.session_id,
            "kind": self.kind,
            "owner": self.owner,
            "job_id": self.job_id,
            "device_type": self.device_type,
            "status": self.status,
            "reverse_port": self.reverse_port,
        }


class SessionManager:
    """In-memory registry of board sessions, keyed by session id."""

    def __init__(self) -> None:
        self.sessions: dict[str, BoardSession] = {}

    def create(
        self,
        device_type: str | None = None,
        kind: str = "container",
        owner: str | None = None,
    ) -> BoardSession:
        private, public = generate_keypair()
        session = BoardSession(
            session_id="s-" + uuid.uuid4().hex[:12],
            private_key=private,
            public_key=public,
            reverse_port=free_port(),
            kind=kind,
            device_type=device_type,
            owner=owner,
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

    def __init__(
        self, manager: SessionManager, allow_networks: list[_Network] | None = None
    ) -> None:
        self._manager = manager
        self._username: str | None = None
        self._allow_networks = allow_networks or []
        self._allowed = True
        self._session: BoardSession | None = None
        self._role: str | None = None  # "agent" (dial-out) | "human" (watcher)

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        peer = conn.get_extra_info("peername")
        ip = peer[0] if peer else ""
        self._allowed = ip_allowed(ip, self._allow_networks)
        if not self._allowed:
            # drop connections from outside the gateway IP allowlist before auth
            conn.close()

    def begin_auth(self, username: str) -> bool:
        self._username = username
        return True  # require key auth below

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        if not self._allowed:
            return False
        session = self._manager.get(username)
        if session is None:
            return False
        self._session = session
        # the dial-out agent (board container / console proxy) holds the session key
        if _key_matches(key, session.public_key):
            self._role = "agent"
            return True
        # a human attached to the session holds a short-lived, non-expired key
        if any(_key_matches(key, pub) for pub in session.active_human_keys()):
            self._role = "human"
            return True
        return False

    def server_requested(self, listen_host: str, listen_port: int) -> bool:
        # The dial-out agent requests `ssh -R <reverse_port>:localhost:<svc>`. Only the
        # agent role may reverse-forward, and only the port we pre-allocated.
        if not self._allowed or self._role != "agent" or self._session is None:
            return False
        if listen_port != self._session.reverse_port:
            return False
        # SECURITY (critical): only ever bind the reverse tunnel to loopback. asyncssh
        # binds to the client-requested host, and an empty/0.0.0.0 request would expose
        # the tunnelled lab service on the master with NO SSH auth and NO IP allowlist.
        # Reachable only from the master itself -> only via an authenticated human -W.
        if listen_host not in ("127.0.0.1", "localhost", "::1"):
            return False
        self._session.status = "connected"
        self._session._connected.set()
        return True

    def connection_requested(
        self, dest_host: str, dest_port: int, orig_host: str, orig_port: int
    ) -> bool:
        # A human forwards into the session with `ssh -W 127.0.0.1:<reverse_port>`.
        # Only the human role may do this, and only to their own session's loopback
        # port; the traffic then rides the agent's reverse tunnel to the board.
        if not self._allowed or self._role != "human" or self._session is None:
            return False
        return dest_host in ("127.0.0.1", "localhost", "::1") and (
            dest_port == self._session.reverse_port
        )

    # SECURITY: the gateway is a pure rendezvous. It offers no shell/exec/sftp of its
    # own and no UNIX-socket forwarding, for any role. (asyncssh defaults deny these;
    # we override explicitly so the posture is not an accident of the base class.)
    def session_requested(self) -> bool:
        return False

    def unix_server_requested(self, listen_path: str) -> bool:
        return False

    def unix_connection_requested(
        self, dest_path: str, orig_host: str, orig_port: int
    ) -> bool:
        return False


class Gateway:
    """Hosts the SSH rendezvous server (own thread/loop) and runs commands over
    session tunnels."""

    def __init__(self, config: Config, manager: SessionManager | None = None) -> None:
        self.config = config
        self.manager = manager or SessionManager()
        self._allow_networks = parse_networks(config.gateway_allow_ips)
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
            lambda: _GatewaySSHServer(self.manager, self._allow_networks),
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

    def attach_human(self, session_id: str) -> dict[str, Any]:
        """Mint an ephemeral keypair and authorise it for human access to a session.

        The key is authorised for ``gateway_human_key_ttl`` seconds only. Returns the
        private key plus the coordinates a human needs to connect (via ``ssh -W``
        through the gateway). The board/console key is never disclosed.
        """
        session = self.manager.get(session_id)
        if session is None:
            raise GatewayError(f"unknown session {session_id}")
        private, public = generate_keypair()
        expires = session.authorize_human(public, ttl=self.config.gateway_human_key_ttl)
        advertise_host = self.config.gateway_advertise_host or self.config.host
        advertise_port = self.config.gateway_advertise_port or self.config.gateway_port
        return {
            "session_id": session_id,
            "private_key": private,
            "public_key": public,
            "gateway_host": advertise_host,
            "gateway_port": advertise_port,
            "reverse_port": session.reverse_port,
            "kind": session.kind,
            "expires_in": int(self.config.gateway_human_key_ttl),
            "expires_at": expires,
        }

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
