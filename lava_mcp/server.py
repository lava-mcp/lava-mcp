"""Build the MCP server and register LAVA tools.

The LAVA target is normally pinned server-side (``LAVA_URL``) to the instance the
deployment fronts; connecting clients then send only their own ``X-Lava-Token`` to
act as their own LAVA user. Left unpinned, the server is multi-tenant and clients
also supply the target via ``X-Lava-Url``. Both fall back to the server's env
config for local stdio use.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

logger = logging.getLogger("lava_mcp")

from mcp.server.fastmcp import FastMCP

from .client import LavaClient, client_from
from .config import Config
from .gateway import Gateway
from .jobs import build_interactive_job


def _lava_username(whoami: Any) -> str | None:
    """Pull the LAVA username out of a ``system/whoami/`` response."""
    if isinstance(whoami, dict):
        for key in ("user", "username"):
            value = whoami.get(key)
            if value:
                return str(value)
        return None
    if isinstance(whoami, str):
        return whoami.strip() or None
    return None


def _enforce_user_allowlist(username: str | None, allow: tuple[str, ...]) -> None:
    """Raise ``PermissionError`` if an allowlist is set and ``username`` is off it."""
    if allow and (username is None or username not in allow):
        raise PermissionError(
            f"LAVA user {username!r} is not permitted to use interactive board "
            "sessions on this server"
        )


def _require_remote_access_device(
    client: LavaClient, device_type: str, tag: str
) -> None:
    """Ensure at least one device of ``device_type`` carries the remote-access tag.

    Interactive sessions may only run on devices an admin has opted in by tagging.
    Fail fast with an actionable message rather than submitting a job that would
    queue forever against a device-type with no permitted device.
    """
    if not tag:
        return
    result = client.list_devices(device_type=device_type, limit=1, **{"tags__name": tag})
    count = result.get("count")
    if count is None:
        count = len(result.get("results") or [])
    if not count:
        raise PermissionError(
            f"Remote access is not enabled for device-type {device_type!r}: no device "
            f"carries the {tag!r} tag. Ask a lab admin to tag a device of this type "
            "for remote access, or choose a different device-type."
        )


def _require_test_services_device(client: LavaClient, hostname: str) -> None:
    """Ensure ``hostname`` opts into LAVA Test Services, needed for the serial console.

    The console proxy runs as a Test Services container on the worker, which LAVA only
    permits on devices whose dictionary sets ``allow_test_services: true``. Fail with an
    actionable message rather than submitting a job LAVA would reject at validation.
    """
    if not client.allows_test_services(hostname):
        raise PermissionError(
            f"Serial console needs 'allow_test_services' enabled in the device "
            f"dictionary for {hostname!r}, but it is not set — a console proxy cannot "
            "be started on this device. Ask a lab admin to enable it."
        )


def _require_owner(session: Any, username: str) -> None:
    """Raise ``PermissionError`` unless ``username`` owns ``session``.

    Sessions grant access to lab hardware, so only the LAVA user who opened one may
    operate on it — otherwise any allowlisted user could pivot into another user's
    board or console.
    """
    owner = getattr(session, "owner", None)
    if owner is not None and owner != username:
        raise PermissionError(
            f"session {session.session_id} belongs to another user"
        )


def build_server(config: Config) -> FastMCP:
    """Create a FastMCP server exposing LAVA operations as tools.

    Read/observe tools are always registered. Write tools are registered unless
    ``read_only``. Interactive board-session tools are registered when the SSH
    gateway is enabled (hosted mode).
    """
    gateway = Gateway(config) if config.gateway_enabled else None

    if gateway is not None and not config.gateway_allow_ips:
        # The gateway still requires a valid per-session key, but with no source-IP
        # allowlist anyone on the network may attempt to connect. Strongly recommend
        # restricting it to the lab (and any human/VPN ranges).
        logger.warning(
            "gateway enabled with no LAVA_MCP_GATEWAY_ALLOW_IPS: the SSH gateway "
            "accepts connections from any source IP. Set an allowlist for the lab "
            "(and human/VPN) networks."
        )

    # NOTE: the gateway is a process-lifetime singleton running in its own thread.
    # It is deliberately NOT started/stopped via the FastMCP lifespan: in stateful
    # streamable-HTTP the lifespan tears down per session, which would stop the
    # gateway's event loop while its listening socket stays open (handshakes then
    # hang). The gateway tools call ensure_started(); the daemon thread exits with
    # the process.
    mcp = FastMCP(
        "lava",
        host=config.host,
        port=config.port,
        json_response=config.json_response,
        stateless_http=config.stateless_http,
    )

    def client() -> LavaClient:
        """Resolve the LAVA client for the current request (per-client creds)."""
        request = None
        try:
            request = mcp.get_context().request_context.request
        except (LookupError, AttributeError, ValueError):
            request = None
        headers = request.headers if request is not None else None
        return client_from(config, headers)

    def require_user(allow: tuple[str, ...]) -> str:
        """Resolve the caller's LAVA user (via whoami) and enforce ``allow``.

        Discovers the username with the caller's own token and raises
        ``PermissionError`` when ``allow`` is set and excludes them. Returns the
        resolved username (empty string if none reported). General LAVA-proxy tools
        do not call this — they are open to any token holder.
        """
        username = _lava_username(client().whoami())
        _enforce_user_allowlist(username, allow)
        return username or ""

    # -- system / identity -------------------------------------------------
    @mcp.tool()
    def whoami() -> Any:
        """Return the LAVA user your token authenticates as."""
        return client().whoami()

    @mcp.tool()
    def version() -> Any:
        """Return the version of the connected LAVA server."""
        return client().version()

    # -- inventory ---------------------------------------------------------
    @mcp.tool()
    def list_devices(
        device_type: str | None = None,
        health: str | None = None,
        state: str | None = None,
        limit: int = 50,
    ) -> Any:
        """List devices, optionally filtered by device_type, health or state.

        Returns {count, results}. health is e.g. Good/Bad/Maintenance/Unknown;
        state is Idle/Reserved/Running.
        """
        return client().list_devices(
            limit=limit, device_type=device_type, health=health, state=state
        )

    @mcp.tool()
    def get_device(hostname: str) -> Any:
        """Get the full record for one device by hostname."""
        return client().get_device(hostname)

    @mcp.tool()
    def get_device_dictionary(hostname: str) -> str:
        """Get a device's rendered configuration dictionary (Jinja2/YAML text)."""
        return client().get_device_dictionary(hostname)

    @mcp.tool()
    def get_qdl_info(hostname: str) -> Any:
        """Summarise a device's QDL/flash capability (qdl/fastboot deploy + boot params).

        Useful before flashing a vendor board: reports whether the device supports
        qdl, the qdl deploy/boot method parameters, and all available deploy/boot
        methods, derived from the device's rendered configuration.
        """
        return client().get_qdl_info(hostname)

    @mcp.tool()
    def list_device_types(limit: int = 100) -> Any:
        """List the device types known to this LAVA instance."""
        return client().list_device_types(limit=limit)

    @mcp.tool()
    def list_workers() -> Any:
        """List the dispatcher workers and their health/state."""
        return client().list_workers()

    # -- jobs --------------------------------------------------------------
    @mcp.tool()
    def list_jobs(
        state: str | None = None,
        health: str | None = None,
        submitter: str | None = None,
        device_type: str | None = None,
        limit: int = 25,
    ) -> Any:
        """List test jobs, newest first, with optional filters.

        state is e.g. Submitted/Scheduling/Scheduled/Running/Canceling/Finished;
        health is Unknown/Complete/Incomplete/Canceled.
        """
        return client().list_jobs(
            limit=limit,
            state=state,
            health=health,
            submitter=submitter,
            requested_device_type=device_type,
        )

    @mcp.tool()
    def get_job(job_id: int) -> Any:
        """Get the full record (state, health, device, times) for one job."""
        return client().get_job(job_id)

    @mcp.tool()
    def get_job_definition(job_id: int) -> str:
        """Get the original submitted YAML job definition for a job."""
        return client().get_job_definition(job_id)

    @mcp.tool()
    def get_job_logs(
        job_id: int, start: int | None = None, end: int | None = None
    ) -> str:
        """Get a job's logs (YAML). Optionally limit to the [start, end) line range."""
        return client().get_job_logs(job_id, start=start, end=end)

    @mcp.tool()
    def get_job_results(job_id: int, limit: int = 200) -> Any:
        """Get a job's test-case results (pass/fail per case)."""
        return client().get_job_results(job_id, limit=limit)

    # -- dashboards (v0.3) -------------------------------------------------
    @mcp.tool()
    def get_queue() -> Any:
        """Get the queue of submitted jobs waiting for a device."""
        return client().dashboard_queue()

    @mcp.tool()
    def get_running() -> Any:
        """Get per-device-type running/reserved counts."""
        return client().dashboard_running()

    @mcp.tool()
    def get_lab_health() -> Any:
        """Get per-device health across the lab."""
        return client().dashboard_lab_health()

    # -- validate (no mutation, always available) --------------------------
    @mcp.tool()
    def validate_job(definition: str) -> Any:
        """Validate a YAML job definition without submitting it."""
        return client().validate_job(definition)

    # -- resources (read-only data the client can fetch by URI) ------------
    @mcp.resource("lava://devices")
    def devices_resource() -> Any:
        """The current device inventory."""
        return client().list_devices(limit=500)

    @mcp.resource("lava://job/{job_id}/definition")
    def job_definition_resource(job_id: str) -> str:
        """The submitted YAML definition for a job."""
        return client().get_job_definition(job_id)

    @mcp.resource("lava://job/{job_id}/log")
    def job_log_resource(job_id: str) -> str:
        """The logs for a job (YAML)."""
        return client().get_job_logs(job_id)

    if not config.read_only:

        @mcp.tool()
        def submit_job(definition: str) -> Any:
            """Submit a YAML job definition. Returns the new job id(s)."""
            return client().submit_job(definition)

        @mcp.tool()
        def cancel_job(job_id: int) -> Any:
            """Request cancellation of a running or queued job."""
            return client().cancel_job(job_id)

        @mcp.tool()
        def resubmit_job(job_id: int) -> Any:
            """Resubmit a finished job with the same definition."""
            return client().resubmit_job(job_id)

    # -- interactive board sessions (hosted gateway mode) ------------------
    if gateway is not None and not config.read_only:

        @mcp.tool()
        async def open_board_session(
            device_type: str,
            tags: list[str] | None = None,
            image: str | None = None,
            wait_seconds: int = 120,
            timeout_minutes: int = 60,
        ) -> Any:
            """Reserve a board and open an interactive container session on it.

            Submits a LAVA job (as your LAVA user) that runs a device-attached
            container which dials back to this gateway over SSH. Waits up to
            wait_seconds for the container to connect, then the session is usable
            via run_in_session.
            """
            user = require_user(config.http_allow_users)
            _require_remote_access_device(
                client(), device_type, config.remote_access_tag
            )
            await asyncio.to_thread(gateway.ensure_started)
            session = gateway.manager.create(device_type=device_type, owner=user)
            job_yaml = build_interactive_job(
                config,
                session,
                device_type=device_type,
                tags=tags,
                image=image,
                timeout_minutes=timeout_minutes,
            )
            result = client().submit_job(job_yaml)
            job_ids = result.get("job_ids") if isinstance(result, dict) else None
            session.job_id = job_ids[0] if job_ids else None
            connected = await gateway.wait_connected(
                session.session_id, timeout=wait_seconds
            )
            view = session.public_view()
            view["connected"] = connected
            return view

        @mcp.tool()
        async def run_in_session(
            session_id: str, command: str, timeout: int = 120
        ) -> Any:
            """Run a shell command inside an open board session, returning output."""
            user = require_user(config.http_allow_users)
            session = gateway.manager.get(session_id)
            if session is None:
                return {"error": f"unknown session {session_id}"}
            _require_owner(session, user)
            if session.kind != "container":
                return {"error": f"session {session_id} is not a container session"}
            await asyncio.to_thread(gateway.ensure_started)
            return await gateway.run(session_id, command, timeout=timeout)

        @mcp.tool()
        async def close_board_session(session_id: str) -> Any:
            """Close a board session and cancel its LAVA job (releases the board)."""
            user = require_user(config.http_allow_users)
            session = gateway.manager.get(session_id)
            if session is None:
                return {"closed": False, "reason": "unknown session"}
            _require_owner(session, user)
            await asyncio.to_thread(gateway.ensure_started)
            gateway.manager.remove(session_id)
            session.revoke_human_keys()
            cancel = client().cancel_job(session.job_id) if session.job_id else None
            session.status = "closed"
            return {"closed": True, "job_id": session.job_id, "cancel": cancel}

        @mcp.tool()
        async def list_board_sessions() -> Any:
            """List the interactive board sessions you own."""
            user = require_user(config.http_allow_users)
            await asyncio.to_thread(gateway.ensure_started)
            return [
                s.public_view()
                for s in gateway.manager.list()
                if s.owner in (None, user)
            ]

        @mcp.tool()
        async def attach_shell(session_id: str) -> Any:
            """Get an ssh command for an interactive shell in a board container.

            Mints a short-lived key, authorises it both at the gateway and inside the
            board container, and returns an ``ssh`` command that jumps through the
            gateway into the container's shell. The container's own key is never
            disclosed; the gateway itself offers no shell.
            """
            user = require_user(config.ssh_allow_users)
            await asyncio.to_thread(gateway.ensure_started)
            session = gateway.manager.get(session_id)
            if session is None:
                return {"error": f"unknown session {session_id}"}
            _require_owner(session, user)
            if session.kind != "container":
                return {
                    "error": f"session {session_id} is not a container session; "
                    "use attach_console for console sessions"
                }
            if session.status != "connected":
                return {"error": f"session {session_id} is not connected yet"}
            info = gateway.attach_human(session_id)
            # authorise the human key inside the board container so it can log in over
            # the tunnel (the container is ephemeral — destroyed when the job ends).
            pub = info["public_key"].replace("'", "")
            push = await gateway.run(
                session_id,
                "mkdir -p /root/.ssh && chmod 700 /root/.ssh && "
                f"printf '%s\\n' '{pub}' >> /root/.ssh/authorized_keys",
            )
            if push.get("exit_status") not in (0, None):
                return {"error": "failed to authorise key in container", "detail": push}
            key_file = f"lava-shell-{session_id}.key"
            ssh = (
                f"ssh -i {key_file} -o StrictHostKeyChecking=no "
                f"-o UserKnownHostsFile=/dev/null "
                f"-o ProxyJump={session_id}@{info['gateway_host']}:{info['gateway_port']} "
                f"-p {info['reverse_port']} {session.container_user}@127.0.0.1"
            )
            return {
                "session_id": session_id,
                "private_key": info["private_key"],
                "ssh_command": ssh,
                "expires_in": info["expires_in"],
                "note": (
                    f"save private_key to {key_file} (chmod 600), then run ssh_command "
                    "for a shell. Your source IP must be inside "
                    "LAVA_MCP_GATEWAY_ALLOW_IPS if set."
                ),
            }

        # -- serial console (Mode 2: ser2net proxy via LAVA Test Services) ----
        @mcp.tool()
        def check_serial_console_support(hostname: str) -> Any:
            """Check whether a device permits the serial-console proxy.

            The proxy runs as a LAVA Test Services container, which LAVA only allows on
            devices whose dictionary sets ``allow_test_services: true``.
            """
            require_user(config.http_allow_users)
            allowed = client().allows_test_services(hostname)
            return {
                "hostname": hostname,
                "allow_test_services": allowed,
                "ok": allowed,
                "message": (
                    "ready"
                    if allowed
                    else f"{hostname} does not set allow_test_services; a lab admin "
                    "must enable it before the serial-console proxy can run."
                ),
            }

        @mcp.tool()
        async def open_console_session(device_type: str | None = None) -> Any:
            """Reserve a serial-console session to embed in your own LAVA job (Mode 2).

            Mints a session the ser2net-proxy Test Services container dials back to over
            the gateway. Returns values to add to your job's top-level ``environment:``
            (LAVA writes them into the proxy's compose .env) so the proxy can dial out.
            Add the ``interactive/ser2net-proxy`` services block to the same job and set
            the ``SER2NET_*`` vars for your device. Once the job boots and the proxy
            connects, call ``attach_console(session_id)`` for a connect command.
            """
            user = require_user(config.http_allow_users)
            await asyncio.to_thread(gateway.ensure_started)
            session = gateway.manager.create(
                device_type=device_type, kind="console", owner=user
            )
            advertise_host = config.gateway_advertise_host or config.host
            advertise_port = config.gateway_advertise_port or config.gateway_port
            # a compose .env cannot hold the multi-line PEM, so base64 it (single line);
            # the proxy's connect script decodes it.
            key_b64 = base64.b64encode(session.private_key.encode()).decode()
            return {
                "session_id": session.session_id,
                "reverse_port": session.reverse_port,
                "job_environment": {
                    "GATEWAY_HOST": advertise_host,
                    "GATEWAY_PORT": str(advertise_port),
                    "SESSION_ID": session.session_id,
                    "REVERSE_PORT": str(session.reverse_port),
                    "SESSION_PRIVATE_KEY_B64": key_b64,
                },
            }

        @mcp.tool()
        async def attach_console(session_id: str) -> Any:
            """Get a command to attach to an open serial-console session as a human.

            Mints a short-lived key authorised for this session and returns an
            ``ssh -W`` command that tunnels to the board console through the gateway.
            The board/proxy key is never disclosed. The console is read-only until the
            job emits console-ready.
            """
            user = require_user(config.ssh_allow_users)
            await asyncio.to_thread(gateway.ensure_started)
            session = gateway.manager.get(session_id)
            if session is None:
                return {"error": f"unknown session {session_id}"}
            _require_owner(session, user)
            if session.kind != "console":
                return {"error": f"session {session_id} is not a console session"}
            info = gateway.attach_human(session_id)
            key_file = f"lava-console-{session_id}.key"
            ssh = (
                f"ssh -i {key_file} -p {info['gateway_port']} "
                f"-W 127.0.0.1:{info['reverse_port']} {session_id}@{info['gateway_host']}"
            )
            return {
                "session_id": session_id,
                "private_key": info["private_key"],
                "ssh_W_command": ssh,
                "raw_console": (
                    f"# save private_key to {key_file} (chmod 600), then for a raw "
                    f"console:\nsocat -,raw,echo=0,escape=0x1d 'EXEC:{ssh},pty'"
                ),
                "note": "Your source IP must be inside LAVA_MCP_GATEWAY_ALLOW_IPS if set.",
            }

        @mcp.tool()
        async def close_console_session(session_id: str) -> Any:
            """Close a serial-console session and revoke its human keys."""
            user = require_user(config.http_allow_users)
            session = gateway.manager.get(session_id)
            if session is None:
                return {"closed": False, "reason": "unknown session"}
            _require_owner(session, user)
            gateway.manager.remove(session_id)
            session.revoke_human_keys()
            session.status = "closed"
            return {"closed": True, "session_id": session_id}

    return mcp
