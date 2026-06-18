"""Build the MCP server and register LAVA tools.

The LAVA target + token are resolved per request from the connecting client's
``X-Lava-Url`` / ``X-Lava-Token`` headers (falling back to the server's env config
for local stdio use), so each agent/human acts as their own LAVA user.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import LavaClient, client_from
from .config import Config
from .gateway import Gateway, GatewayError
from .jobs import build_interactive_job


def build_server(config: Config) -> FastMCP:
    """Create a FastMCP server exposing LAVA operations as tools.

    Read/observe tools are always registered. Write tools are registered unless
    ``read_only``. Interactive board-session tools are registered when the SSH
    gateway is enabled (hosted mode).
    """
    gateway = Gateway(config) if config.gateway_enabled else None

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        if gateway is not None:
            await gateway.start()
        try:
            yield {}
        finally:
            if gateway is not None:
                await gateway.stop()

    mcp = FastMCP(
        "lava",
        host=config.host,
        port=config.port,
        json_response=config.json_response,
        stateless_http=config.stateless_http,
        lifespan=lifespan,
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

        @mcp.tool()
        def set_job_priority(job_id: int, priority: int) -> Any:
            """Set a job's queue priority (0-100, higher runs sooner)."""
            return client().set_job_priority(job_id, priority)

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
            session = gateway.manager.create(device_type=device_type)
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
            connected = False
            try:
                await gateway.wait_connected(session.session_id, timeout=wait_seconds)
                connected = True
            except (asyncio.TimeoutError, GatewayError):
                pass
            view = session.public_view()
            view["connected"] = connected
            return view

        @mcp.tool()
        async def run_in_session(
            session_id: str, command: str, timeout: int = 120
        ) -> Any:
            """Run a shell command inside an open board session, returning output."""
            return await gateway.run(session_id, command, timeout=timeout)

        @mcp.tool()
        async def close_board_session(session_id: str) -> Any:
            """Close a board session and cancel its LAVA job (releases the board)."""
            session = gateway.manager.remove(session_id)
            if session is None:
                return {"closed": False, "reason": "unknown session"}
            cancel = client().cancel_job(session.job_id) if session.job_id else None
            session.status = "closed"
            return {"closed": True, "job_id": session.job_id, "cancel": cancel}

        @mcp.tool()
        def list_board_sessions() -> Any:
            """List the currently-open interactive board sessions."""
            return [s.public_view() for s in gateway.manager.list()]

    return mcp
