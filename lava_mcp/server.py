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
from dataclasses import replace
from typing import Any
from urllib.parse import urlparse

import yaml

logger = logging.getLogger("lava_mcp")

from mcp.server.fastmcp import FastMCP

from .client import LavaClient, client_from
from .config import Config
from .gateway import Gateway
from .jobs import build_interactive_job

# The interactive gateway is WebSocket-only: the dial-out containers and human
# clients reach it exclusively over wss://.../gateway-ssh (via websocat). Without an
# advertised URL there is no way to connect, so the tools that hand out connect
# details refuse rather than emit something unusable.
_WS_NOT_CONFIGURED = (
    "interactive gateway WebSocket URL is not configured; set "
    "LAVA_MCP_GATEWAY_WS_URL (e.g. wss://host/gateway-ssh)"
)

# Surfaced to MCP clients (via the server's initialize response) so an agent
# understands the two distinct ways to reach a board and when to use each.
_SERVER_INSTRUCTIONS = """\
LAVA (Linaro Automated Validation Architecture) is a system for automated testing on
real hardware: it schedules jobs onto physical devices ("boards") in a lab, deploys
or flashes an OS image, boots it, and runs tests — all described by a YAML job
definition. Boards are grouped by device-type; a job queues until a matching board is
free. Results and logs are retrievable per job.

This server proxies one LAVA instance: query devices/jobs, submit and manage test
jobs, and open interactive sessions to a board. General LAVA tools grant exactly what
your own LAVA token grants.

There are TWO different ways to get an interactive shell/console, for different jobs:

1. Board session — a shell in a container running *next to* the board (on the
   worker), NOT a shell on the board itself. Use it for host-side work against the
   device: flashing, fastboot/adb, qdl, and bring-up. It needs the board's USB
   exposed to the container. Reach for it when you need to control *how* the board is
   driven from the host rather than the fixed deploy LAVA would run — e.g. trying
   different flashing software or versions, custom fastboot/qdl/adb sequences, or
   deeper hands-on debugging over USB (a board that won't boot, recovery mode).
   Tools: open_board_session -> run_in_session (run one command) or attach_shell
   (interactive ssh). Only devices tagged for remote access can host one.

   The container is Debian and runs as root, so you can apt-get or build any tooling
   at runtime. E.g. build qdl from source and detect the attached board (in EDL mode
   it enumerates as vendor HS-USB QDLoader 05c6:9008):
     apt-get update && apt-get install -y git build-essential pkg-config \\
       libusb-1.0-0-dev libxml2-dev
     git clone https://github.com/linux-msm/qdl && make -C qdl
     lsusb | grep -i '05c6:9008'   # board present in EDL mode; qdl can now flash it

2. Serial console — the board's *own* serial console (UART): boot/kernel logs, the
   login prompt, a shell on the booted board. Use it when you need what's actually on
   the board, or console access with no DUT networking. Reach for it to interact with
   the booted board directly — drive tests and run commands live at the console
   WITHOUT writing a LAVA test definition, watch the boot, or work with the
   bootloader/login prompt. Unlike a board session, this path uses LAVA to DEPLOY and
   BOOT an image first; the server then adds a test action that bridges the console
   out. Tools: check_serial_console_support -> open_console_session -> attach_console.

   Writing a correct deploy+boot LAVA job from scratch is hard. Do NOT hand-author
   the boot flow — adapt an existing job. ALWAYS base it on a previous successful job
   whose deploy `url` closely matches the artifacts you want to boot: deploy+boot
   parameters (flash method, rawprogram/patch, storage, auth headers) are
   image-specific, so ONLY a job that flashed a similar URL is a safe template. Call
   find_boot_template(artifact_url, device_type) — it searches this instance and the
   template masters and returns the best URL-matched jobs with their full definition
   (or do it by hand with list_jobs + get_job_definition). Do NOT use an unrelated job
   (e.g. a health-check, or a job for a different image) as the template — it will have
   incompatible deploy settings. Keep the matching job's deploy+boot actions — swap in
   your URL but KEEP its artifact authentication (HTTP headers such as Authorization,
   and any token/credentials) so the fetch succeeds — and add the console proxy on
   top. You do NOT need an example anywhere:
   open_console_session returns (in its `add_to_job` field) the exact `services` test
   action to paste in as the first action, plus the `environment:` values to set.
   After submitting, poll check_console_ready(job_id) until ready:true (instead of
   reading logs), then call attach_console.

Handing out an SSH key (attach_shell/attach_console): the returned private_key must
be saved to a file with `chmod 600` — ssh refuses a key file with looser permissions.
"""


def _build_instructions(config: Config) -> str:
    """Server instructions, extended with the configured boot-template masters."""
    ins = _SERVER_INSTRUCTIONS
    if config.template_job_masters:
        masters = ", ".join(config.template_job_masters)
        ins += (
            "\n\nBoot-template masters: the best deploy-URL-matching jobs usually live "
            f"on these other LAVA masters, not this instance — search them too: "
            f"{masters}. They typically hold production jobs with correct deploy URLs "
            "and artifact authentication; prefer a URL-matched template from there."
        )
    return ins


def build_shell_ssh_config(
    session_id: str,
    key_file: str,
    ws_url: str,
    reverse_port: int,
    container_user: str,
) -> str:
    """ssh config for a container shell over the WebSocket transport.

    The jump host tunnels to the gateway over wss:// via websocat; ProxyJump then
    reaches the board container's sshd on its loopback reverse port. ``ssh -F <conf>
    board-<id>`` gives the shell.
    """
    return (
        f"Host gw-{session_id}\n"
        f"    User {session_id}\n"
        f"    IdentityFile {key_file}\n"
        f"    ProxyCommand websocat -b {ws_url}\n"
        f"    StrictHostKeyChecking no\n"
        f"    UserKnownHostsFile /dev/null\n"
        f"Host board-{session_id}\n"
        f"    HostName 127.0.0.1\n"
        f"    Port {reverse_port}\n"
        f"    User {container_user}\n"
        f"    IdentityFile {key_file}\n"
        f"    ProxyJump gw-{session_id}\n"
        f"    StrictHostKeyChecking no\n"
        f"    UserKnownHostsFile /dev/null\n"
    )


def build_console_ssh_command(
    session_id: str,
    key_file: str,
    ws_url: str,
    reverse_port: int,
    gateway_host: str,
) -> str:
    """``ssh -W`` command that tunnels to a console session over the WebSocket
    transport (websocat ProxyCommand to the gateway, then -W to the reverse port)."""
    return (
        f"ssh -i {key_file} -o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null "
        f"-o 'ProxyCommand=websocat -b {ws_url}' "
        f"-W 127.0.0.1:{reverse_port} {session_id}@{gateway_host}"
    )


def build_console_services_action(
    interactive_repo: str, timeout_minutes: int = 70
) -> str:
    """The LAVA ``services`` test action that runs the ser2net-proxy console bridge.

    Returned by open_console_session so an agent can paste it straight into its
    deploy+boot job (as the first action) — no need to hunt for an example in the
    lava-mcp repo. The proxy image/scripts are fetched from ``interactive_repo``.
    """
    return (
        "- test:\n"
        "    namespace: console\n"
        "    timeout:\n"
        f"      minutes: {timeout_minutes}\n"
        "    services:\n"
        "    - name: ser2net-proxy\n"
        "      from: git\n"
        f"      repository: {interactive_repo}\n"
        "      path: interactive/ser2net-proxy/docker-compose.yml\n"
    )


def build_console_ready_action(
    sentinel: str = "LAVA_MCP_CONSOLE_WRITABLE",
    timeout_minutes: int = 60,
    namespace: str = "boot",
) -> str:
    """LAVA test action that signals console-ready then hands the console to the user.

    Add as the LAST action, after deploy+boot brings the board to a shell. It echoes
    ``sentinel`` to the console (which unlocks the ser2net-proxy from read-only) and
    then execs an interactive shell that blocks — holding the job open so the user can
    work. LAVA tolerates a silent console (a test-shell expect timeout just loops), so
    NO keepalive/tick output is needed; the action ``timeout`` (set it to your job
    length) bounds the hold. The user ends the session by exiting the shell (Ctrl-D).
    ``namespace``/``connection-namespace`` MUST match your boot action's namespace so
    the step runs over the booted serial console.
    """
    return (
        "- test:\n"
        f"    namespace: {namespace}\n"
        f"    connection-namespace: {namespace}\n"
        "    timeout:\n"
        f"      minutes: {timeout_minutes}\n"
        "    definitions:\n"
        "    - from: inline\n"
        "      name: console-ready\n"
        "      path: inline/console-ready.yaml\n"
        "      repository:\n"
        "        metadata:\n"
        "          format: Lava-Test Test Definition 1.0\n"
        "          name: console-ready\n"
        "          description: signal console-ready, then hand the console to the user\n"
        "        run:\n"
        "          steps:\n"
        "          - lava-test-case console-ready --result pass\n"
        f"          - 'echo \"{sentinel}\"'\n"
        "          - 'exec \"$(command -v bash || command -v sh)\" -i'\n"
    )


def _collect_urls(node: Any, out: list[str]) -> None:
    if isinstance(node, str):
        if node.startswith("http://") or node.startswith("https://"):
            out.append(node)
    elif isinstance(node, dict):
        for v in node.values():
            _collect_urls(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_urls(v, out)


def deploy_urls_from_definition(definition_text: str) -> list[str]:
    """Every http(s) artifact URL under the deploy actions of a job definition."""
    try:
        job = yaml.safe_load(definition_text)
    except yaml.YAMLError:
        return []
    if not isinstance(job, dict):
        return []
    urls: list[str] = []
    for action in job.get("actions", []) or []:
        if isinstance(action, dict) and "deploy" in action:
            _collect_urls(action["deploy"], urls)
    return urls


def url_match_score(target: str, candidate: str) -> int:
    """Similarity of two artifact URLs: same filename dominates, then shared path
    segments, then same host. Build-number path segments differ between builds, so a
    good template scores high on the (build-independent) filename and device segments.
    """
    t, c = urlparse(target), urlparse(candidate)
    tseg = [s for s in t.path.split("/") if s]
    cseg = [s for s in c.path.split("/") if s]
    score = 0
    if t.netloc and t.netloc == c.netloc:
        score += 2
    if tseg and cseg and tseg[-1] == cseg[-1]:  # same artifact filename
        score += 5
    score += len(set(tseg) & set(cseg))  # shared path segments (device, image family)
    return score


def console_ready_in_logs(logs_text: str, sentinel: str) -> bool:
    """True once the board has echoed the console-ready ``sentinel`` in the job log.

    The proxy flips the console from read-only to writable when it sees the sentinel
    on the console; the same string lands in the job log as board output. Ignore the
    ``CONSOLE_READY_SENTINEL=<sentinel>`` env declaration echoed at job start, which
    is present from the very beginning and does not mean the board is up.
    """
    if not sentinel:
        return False
    for line in logs_text.splitlines():
        if sentinel in line and "CONSOLE_READY_SENTINEL" not in line:
            return True
    return False


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
    result = client.list_devices(
        device_type=device_type, limit=1, **{"tags__name": tag}
    )
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
        raise PermissionError(f"session {session.session_id} belongs to another user")


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
        instructions=_build_instructions(config),
        host=config.host,
        port=config.port,
        json_response=config.json_response,
        stateless_http=config.stateless_http,
    )

    if gateway is not None:
        # Serve the gateway's SSH-over-WebSocket bridge as a route on this same app
        # (one port), at a sub-path of the MCP endpoint: <streamable_http_path>/
        # gateway-ssh, i.e. /mcp/gateway-ssh. Caddy already routes /mcp* here and
        # bypasses anubis, so the dial-out/consumer SSH streams ride wss:// on 443.
        from starlette.routing import WebSocketRoute

        async def _gateway_ws_endpoint(websocket: Any) -> None:
            await asyncio.to_thread(gateway.ensure_started)
            await gateway.bridge_websocket(websocket)

        ws_path = mcp.settings.streamable_http_path.rstrip("/") + "/gateway-ssh"
        # FastMCP folds _custom_starlette_routes into the Starlette app it builds for
        # the streamable-HTTP transport; a WebSocketRoute rides along fine (the list is
        # typed for HTTP Routes, but Starlette's router accepts WebSocketRoute too).
        mcp._custom_starlette_routes.append(
            WebSocketRoute(ws_path, _gateway_ws_endpoint)  # type: ignore[arg-type]
        )
        # exposed for tests/introspection; the tools capture `gateway` via closure
        mcp._lava_gateway = gateway  # type: ignore[attr-defined]

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
    def find_boot_template(
        artifact_url: str,
        device_type: str | None = None,
        per_master: int = 20,
        top: int = 3,
    ) -> Any:
        """Find the best previous job to use as a deploy+boot template for an image.

        Searches THIS instance (with your token) and the configured template masters
        (LAVA_MCP_TEMPLATE_JOB_MASTERS, public jobs only — no auth) for recent
        successful (Complete) jobs, extracts each job's deploy URL(s), and ranks them
        by similarity to ``artifact_url`` (same artifact filename dominates, then
        shared path segments, then same host). Returns the top matches with their full
        job ``definition`` so you can adapt it directly: swap in your artifact_url,
        KEEP that job's artifact authentication (Authorization headers/credentials),
        and add the console proxy. Pass ``device_type`` to narrow the search. Only
        ``per_master`` recent jobs per master are scanned (reported in ``searched``).
        """
        searches: list[tuple[LavaClient, str, str]] = [(client(), config.url, "token")]
        seen = {config.url.rstrip("/")} if config.url else set()
        for master in config.template_job_masters:
            if master.rstrip("/") in seen:
                continue
            seen.add(master.rstrip("/"))
            searches.append(
                (LavaClient(replace(config, url=master, token=None)), master, "public")
            )

        scored: list[dict[str, Any]] = []
        searched: list[dict[str, Any]] = []
        for cl, master, auth in searches:
            scanned = 0
            try:
                filters: dict[str, Any] = {"health": "Complete", "ordering": "-id"}
                if device_type:
                    filters["device_type"] = device_type
                page = cl.list_jobs(limit=per_master, **filters)
                for row in page.get("results", []) or []:
                    jid = row.get("id")
                    if jid is None:
                        continue
                    try:
                        job = cl.get_job(jid)
                    except Exception:  # noqa: BLE001 - skip an unreadable job
                        continue
                    scanned += 1
                    defn = job.get("original_definition") or job.get("definition") or ""
                    urls = deploy_urls_from_definition(defn)
                    if not urls:
                        continue
                    best_url = max(urls, key=lambda u: url_match_score(artifact_url, u))
                    best = url_match_score(artifact_url, best_url)
                    if best <= 0:
                        continue
                    scored.append(
                        {
                            "master": master,
                            "auth": auth,
                            "job_id": jid,
                            "device_type": job.get("requested_device_type"),
                            "deploy_url": best_url,
                            "score": best,
                            "definition": defn,
                        }
                    )
                searched.append(
                    {"master": master, "auth": auth, "jobs_scanned": scanned}
                )
            except (
                Exception
            ) as exc:  # noqa: BLE001 - one master failing must not kill all
                searched.append(
                    {
                        "master": master,
                        "auth": auth,
                        "jobs_scanned": scanned,
                        "error": str(exc),
                    }
                )
        scored.sort(key=lambda m: m["score"], reverse=True)
        return {
            "artifact_url": artifact_url,
            "searched": searched,
            "matches": scored[:top],
            "note": (
                "Use the top match's `definition` as your deploy+boot base: swap in "
                "artifact_url, KEEP its Authorization/artifact auth, then add the "
                "console proxy (see open_console_session). If matches is empty or "
                "scores are low, widen per_master or drop device_type."
            ),
        }

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
            """Open a shell in a container running *next to* the board (not on it).

            Way 1 of 2 (see also open_console_session for the board's own serial
            console). Submits a LAVA job (as your LAVA user) that runs a
            device-attached container on the worker, with the board's USB/serial
            exposed — for flashing, fastboot/adb, qdl and bring-up. The container
            dials back to this gateway over SSH; waits up to wait_seconds for it to
            connect, then the session is usable via run_in_session / attach_shell.
            Only devices tagged for remote access can host one.
            """
            user = require_user(config.http_allow_users)
            if not config.gateway_ws_url:
                return {"error": _WS_NOT_CONFIGURED}
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
            """Run a shell command in the board session's container (next to the
            board), returning output. The command runs in the device-attached
            container, not on the board itself."""
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
            """Get an ssh command for an interactive shell in the board's container.

            The interactive form of a board session (Way 1): a shell in the container
            running *next to* the board, not on the board itself (use attach_console
            for the board's serial console). Mints a short-lived key, authorises it
            both at the gateway and inside the container, and returns an ``ssh``
            command that jumps through the gateway into the container's shell. The
            container's own key is never disclosed; the gateway itself offers no shell.
            """
            user = require_user(config.ssh_allow_users)
            if not config.gateway_ws_url:
                return {"error": _WS_NOT_CONFIGURED}
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
            conf_file = f"lava-shell-{session_id}.conf"
            config_text = build_shell_ssh_config(
                session_id,
                key_file,
                info["gateway_ws_url"],
                info["reverse_port"],
                session.container_user,
            )
            return {
                "session_id": session_id,
                "private_key": info["private_key"],
                "expires_in": info["expires_in"],
                "ssh_config": config_text,
                "ssh_command": f"ssh -F {conf_file} board-{session_id}",
                "note": (
                    f"Save private_key to {key_file} and `chmod 600 {key_file}` — ssh "
                    "REFUSES a key file with looser permissions — then save ssh_config "
                    f"to {conf_file} and run ssh_command for a shell. Requires "
                    "`websocat` on your PATH. Your source IP must be inside "
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
            """Reserve access to the board's own serial console (UART), for a LAVA job.

            Way 2 of 2 (see also open_board_session for a shell in a container beside
            the board). This reaches the board's actual console — boot/kernel logs, the
            login prompt, a shell on the booted board — and works with no DUT
            networking. Unlike a board session, this path relies on a LAVA job that
            DEPLOYS and BOOTS an image; this call only reserves the console bridge.

            You supply the deploy+boot job. Do NOT hand-author the boot flow — adapt an
            existing job. ALWAYS base it on a previous successful job whose deploy `url`
            closely matches the artifacts you want to boot: deploy+boot params (flash
            method, rawprogram/patch, storage, auth headers) are image-specific, so only
            a job that flashed a similar URL is a safe template. Call
            find_boot_template(artifact_url, device_type) to search this instance and
            the template masters for the best URL match. Do NOT use an unrelated job
            such as a health-check. Keep that job's deploy+boot actions —
            swap in your URL but KEEP its artifact authentication (HTTP headers such as
            Authorization, and any credentials) so the fetch succeeds — and add the
            console proxy on top.

            You do NOT need to find an example in any repo: this call returns, in
            ``add_to_job``, the exact ``services`` test action to paste in and the full
            list of ``environment:`` values to set. Once the job boots and the proxy
            connects, call ``attach_console(session_id)``. Requires the device dict to
            allow Test Services (check_serial_console_support).
            """
            user = require_user(config.http_allow_users)
            if not config.gateway_ws_url:
                return {"error": _WS_NOT_CONFIGURED}
            await asyncio.to_thread(gateway.ensure_started)
            session = gateway.manager.create(
                device_type=device_type, kind="console", owner=user
            )
            advertise_host = config.gateway_advertise_host or config.host
            # a compose .env cannot hold the multi-line PEM, so base64 it (single line);
            # the proxy's connect script decodes it.
            key_b64 = base64.b64encode(session.private_key.encode()).decode()
            job_environment = {
                # GATEWAY_HOST is the ssh user@host label; the console dial-out
                # tunnels over GATEWAY_WS_URL (wss://, 443) via websocat.
                "GATEWAY_HOST": advertise_host,
                "GATEWAY_WS_URL": config.gateway_ws_url,
                "SESSION_ID": session.session_id,
                "REVERSE_PORT": str(session.reverse_port),
                "SESSION_PRIVATE_KEY_B64": key_b64,
            }
            return {
                "session_id": session.session_id,
                "reverse_port": session.reverse_port,
                "job_environment": job_environment,
                "add_to_job": {
                    "note": (
                        "Everything to add to your deploy+boot LAVA job — no repo "
                        "lookup or example needed. Two actions + the environment."
                    ),
                    "step_1_services_action": build_console_services_action(
                        config.interactive_repo
                    ),
                    "step_1_note": (
                        "Add this as the FIRST action (before deploy/boot) so the proxy "
                        "watches the console from the start of the job."
                    ),
                    "step_2_environment": (
                        "Put job_environment (above) into the job's top-level "
                        "`environment:`, plus these for your board: "
                        "SER2NET_HOST (ser2net hostname, usually 'ser2net'); "
                        "SER2NET_PORT (the board's ser2net port — read it from the "
                        "device's connection_command, e.g. 'telnet ser2net 7095' -> "
                        "7095, via get_device/get_device_dictionary); "
                        "SER2NET_NETWORK (docker network ser2net is on, usually "
                        "'lava-dispatcher_default'); "
                        "CONSOLE_READY_SENTINEL (must match the echo in step 3, e.g. "
                        "LAVA_MCP_CONSOLE_WRITABLE)."
                    ),
                    "step_3_console_ready_action": build_console_ready_action(),
                    "step_3_note": (
                        "Add this as the LAST action (after deploy+boot reaches a "
                        "shell). It echoes CONSOLE_READY_SENTINEL to unlock the "
                        "read-only console, then hands you an interactive shell that "
                        "holds the job open. Set its `timeout.minutes` to your job "
                        "length and its `namespace`/`connection-namespace` to match "
                        "your boot action. LAVA tolerates a silent console, so no "
                        "keepalive is needed; end the session by exiting the shell."
                    ),
                    "then": (
                        "Submit the job, then poll check_console_ready(job_id) until "
                        "it returns ready:true (do NOT scrape logs yourself). Once "
                        "ready, call attach_console(session_id) for a writable console."
                    ),
                },
            }

        @mcp.tool()
        def check_console_ready(
            job_id: int, sentinel: str = "LAVA_MCP_CONSOLE_WRITABLE"
        ) -> Any:
            """Has a console job reached console-ready (writable) state yet?

            Poll THIS instead of reading job logs yourself. It scans job_id's logs for
            the CONSOLE_READY_SENTINEL your deploy+boot job echoes once the board boots
            to a shell — the moment the ser2net-proxy flips the console from read-only
            to writable. Returns {ready, job_state, job_health}: when ready is true,
            attach_console gives a writable console; if job_state is Finished/Canceling
            the board never signalled, so stop polling. Pass sentinel if your job set a
            custom CONSOLE_READY_SENTINEL.
            """
            logs = client().get_job_logs(job_id)
            ready = console_ready_in_logs(logs, sentinel)
            job = client().get_job(job_id)
            state = job.get("state") if isinstance(job, dict) else None
            health = job.get("health") if isinstance(job, dict) else None
            return {
                "job_id": job_id,
                "ready": ready,
                "sentinel": sentinel,
                "job_state": state,
                "job_health": health,
                "note": (
                    "console is writable — call attach_console for a live console"
                    if ready
                    else "not writable yet; poll again. Stop if job_state is "
                    "Finished/Canceling (the board never echoed the sentinel)."
                ),
            }

        @mcp.tool()
        async def attach_console(session_id: str) -> Any:
            """Get a command to attach to the board's serial console (UART).

            The interactive form of a console session (Way 2): the board's own console,
            not a container shell (use attach_shell for that). Mints a short-lived key
            authorised for this session and returns an ``ssh -W`` command that tunnels
            to the console through the gateway. The board/proxy key is never disclosed.
            The console is read-only until the job emits console-ready.
            """
            user = require_user(config.ssh_allow_users)
            if not config.gateway_ws_url:
                return {"error": _WS_NOT_CONFIGURED}
            await asyncio.to_thread(gateway.ensure_started)
            session = gateway.manager.get(session_id)
            if session is None:
                return {"error": f"unknown session {session_id}"}
            _require_owner(session, user)
            if session.kind != "console":
                return {"error": f"session {session_id} is not a console session"}
            info = gateway.attach_human(session_id)
            key_file = f"lava-console-{session_id}.key"
            ssh = build_console_ssh_command(
                session_id,
                key_file,
                info["gateway_ws_url"],
                info["reverse_port"],
                info["gateway_host"],
            )
            note = (
                f"Save private_key to {key_file} and `chmod 600 {key_file}` — ssh "
                "REFUSES a key file with looser permissions. Requires `websocat` on "
                "your PATH. Your source IP must be inside LAVA_MCP_GATEWAY_ALLOW_IPS "
                "if set."
            )
            return {
                "session_id": session_id,
                "private_key": info["private_key"],
                "ssh_W_command": ssh,
                "raw_console": (
                    f"# save private_key to {key_file} (chmod 600), then for a raw "
                    f"console:\nsocat -,raw,echo=0,escape=0x1d 'EXEC:{ssh},pty'"
                ),
                "note": note,
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
