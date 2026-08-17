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
from urllib.parse import urlparse

import yaml

logger = logging.getLogger("lava_mcp")

from mcp.server.fastmcp import FastMCP

from .artifacts import ArtifactError, ArtifactStore
from .client import LavaClient, LavaError, client_from, ser2net_endpoint
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

The lab is shared and jobs are independent — do NOT assume continuity across jobs. The
scheduler picks a free board per job, so the board you land on is not yours to keep:
the job that ran on it before (or runs next) is very likely someone else's, and a board
you just used is not necessarily the one you get next time. Nothing persists on a board
between jobs — a flashed image, files, or leftover state from one job must not be relied
on by another; re-establish what you need within your own job. Likewise, artifacts a job
downloads (deploy URLs — including from the artifact store — and test-definition repos)
are fetched into that job's own workspace on the worker and deleted when it ends; they
are NOT shared with or reused by other jobs, so every job that needs a file must fetch
it itself.

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

   open_board_session(console=true) ALSO gives you the board's serial console beside
   the shell. Serial-console access — in ANY job, not just this one — is bridged by a
   ser2net-proxy Test Services container: the container LAVA runs your test/session in
   generally can't reach the lab's ser2net endpoint, so a sibling Test Services
   container on the dispatcher network relays the UART out (it is the same proxy
   open_console_session adds to a deploy+boot job). That is why console access needs a
   device that allows Test Services. For console=true the server adds and wires the
   proxy automatically; you just call attach_console(console_session_id). Only ser2net
   (telnet) consoles can be proxied.

   The container is Debian and runs as root, so you can apt-get or build any tooling
   at runtime. E.g. build qdl from source and detect the attached board (in EDL mode
   it enumerates as vendor HS-USB QDLoader 05c6:9008):
     apt-get update && apt-get install -y git build-essential pkg-config \\
       libusb-1.0-0-dev libxml2-dev
     git clone https://github.com/linux-msm/qdl && make -C qdl
     lsusb | grep -i '05c6:9008'   # board present in EDL mode; qdl can now flash it

   Power/recovery control. A board session runs in a container next to the board and
   has no direct power over it — the LAVA_*_COMMAND values in its env call
   dispatcher-host tools that aren't in the container. Instead it asks the dispatcher
   to run the device's LAVA command on the worker: call
   run_device_command(session_id, name), or inside a shell run `lava-device-command
   <name>` (aliases: lava-power-on, lava-power-off, lava-hard-reset). Names: power_on,
   power_off, hard_reset, recovery_mode, recovery_exit, pre_power_command,
   pre_os_command, and any device user_commands (e.g. USB-port toggles). This is how
   you power-cycle a board into EDL for flashing, or recover one that has wedged. It
   returns the command's exit status (0 = ran). Two things: (a) a power cycle makes
   the DUT re-enumerate over USB, so wait for its lsusb entry / device nodes to
   reappear before adb/fastboot/qdl rather than assuming they're there; (b) it needs a
   LAVA instance that supports the device-command relay — a non-zero/unavailable
   result means the instance lacks it. (A session can likewise record LAVA results
   with `lava-signal 'TESTCASE TEST_CASE_ID=x RESULT=pass'`.)

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
   find_boot_template(artifact_url, device_type) — it searches this instance's recent
   successful jobs and returns the best URL-matched ones with their full definition
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

Serving your own files to LAVA: when you have a build product (kernel, rootfs, DTB,
script) you want a job to deploy/flash or a booted device to fetch, but no URL to host
it at, use create_artifact_upload -> HTTP PUT -> reference it from your job. The
temporary token it returns is registered as a LAVA *remote artifact token*, so a
deploy action can carry the token NAME (LAVA swaps in the secret at download, keeping
it out of the job) — the tool hands back a deploy_block and a full example snippet. It
also returns a fetch_command (plain curl) you can run from a test action ON a booted
device with working networking to land the file on the device itself. Artifacts
auto-expire within hours; set the job's `visibility: personal` when it references one.
(Only offered in hosted mode.)

Handing out an SSH key (attach_shell/attach_console): the returned private_key must
be saved to a file with `chmod 600` — ssh refuses a key file with looser permissions.
"""


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


def _require_test_services_device_type(
    client: LavaClient, device_type: str, remote_access_tag: str = ""
) -> None:
    """Pre-flight for a device_type console: at least ONE schedulable board must allow
    Test Services.

    ``allow_test_services`` is per-board (like the ser2net console port), NOT uniform
    across a device-type, so we must not refuse just because the first-listed board
    lacks it — some boards of the type may enable it while others don't. We scan the
    candidate boards (those carrying the remote-access tag the job pins to, since LAVA
    only schedules the session onto one of those) and pass as soon as one enables it;
    we refuse only when we could read at least one candidate and none did. The board
    LAVA finally assigns is resolved at runtime. Best-effort: skips silently if the
    inventory can't be read (or no candidate is readable)."""
    filters = {"tags__name": remote_access_tag} if remote_access_tag else {}
    try:
        page = client.list_devices(device_type=device_type, limit=50, **filters)
    except Exception:  # noqa: BLE001 - pre-flight is best-effort
        return
    checked = False
    for row in page.get("results", []) or []:
        host = row.get("hostname")
        if not host:
            continue
        try:
            allowed = client.allows_test_services(host)
        except Exception:  # noqa: BLE001 - skip a device we can't read
            continue
        checked = True
        if allowed:
            return  # at least one candidate board enables Test Services
    if checked:
        raise PermissionError(
            f"no {device_type!r} device (tagged for remote access) enables "
            "'allow_test_services', which the serial console needs. The board-session "
            "container cannot reach the lab's ser2net endpoint directly, so the console "
            "is only available via the Test Services proxy (console=true) — there is no "
            "manual workaround from inside the container. Open the board session "
            "without console=true for host-side access, or ask a lab admin to enable "
            "'allow_test_services' on a board of this type."
        )


def _discover_console_target(client: LavaClient, job_id: int | str | None) -> dict:
    """Resolve the serial console for the board a job actually landed on.

    The ser2net port is per-board and only known once LAVA schedules the job onto a
    specific device, so it cannot be discovered up-front from a representative device.
    This reads the job's ``actual_device`` (set at scheduling) and inspects that board's
    connection command. Works for both a board session (Mode 1) and a standalone console
    job (Mode 2) — both have a job on a real board. Returns a status dict:

      {"status": "pending"}                     job not scheduled / device unknown yet
      {"status": "unsupported", "command": ...} console command is not a ser2net telnet
                                                we can proxy (another lab's tooling)
      {"status": "ok", "host": ..., "port": ...} proxyable ser2net endpoint
    """
    if job_id is None:
        return {"status": "pending"}
    try:
        job = client.get_job(job_id)
        host = job.get("actual_device") if isinstance(job, dict) else None
        if not host:
            return {"status": "pending"}
        cmd = client.console_connection_command(host)
    except Exception:  # noqa: BLE001 - discovery is best-effort
        return {"status": "pending"}
    endpoint = ser2net_endpoint(cmd or "")
    if endpoint is None:
        return {"status": "unsupported", "command": cmd, "hostname": host}
    return {"status": "ok", "host": endpoint[0], "port": endpoint[1], "hostname": host}


def _unproxyable_console_note(res: dict) -> str:
    """Agent-facing explanation when a board's console cannot be proxied."""
    cmd = res.get("command")
    how = f"via {cmd!r}" if cmd else "in a way lava-mcp did not recognise"
    return (
        f"serial console unavailable: this lab reaches board {res.get('hostname')}'s "
        f"console {how}, but lava-mcp can only proxy ser2net (telnet) consoles. Use a "
        "board session (open_board_session without console) for host-side access."
    )


async def _ensure_console_target(
    client: LavaClient, gateway: Any, session: Any, job_id: int | str | None
) -> dict:
    """Resolve a console session's ser2net endpoint (from the assigned board) and push
    it to the proxy over the reverse tunnel. Idempotent — safe to call repeatedly as a
    job schedules; re-pushes an already-known target. Returns the status dict from
    _discover_console_target (or an "ok" dict for an already-known target)."""
    if session.console_target is not None:
        host, port = session.console_target
        await asyncio.to_thread(
            gateway.set_console_target, session.session_id, host, port
        )
        return {"status": "ok", "host": host, "port": port}
    res = await asyncio.to_thread(_discover_console_target, client, job_id)
    if res.get("status") == "ok":
        session.console_target = (res["host"], res["port"])
        await asyncio.to_thread(
            gateway.set_console_target, session.session_id, res["host"], res["port"]
        )
    return res


async def _wire_console(
    client: LavaClient,
    gateway: Any,
    session: Any,
    console_session: Any,
    board_connected: bool,
    wait_seconds: int,
) -> str:
    """Wire a board session's paired console proxy to the assigned board's ser2net
    endpoint. Returns a note for the caller/agent."""
    if not board_connected:
        return (
            "board session has not connected yet; the serial console will be wired "
            "when you call attach_console(console_session_id)"
        )
    # wait for the proxy to dial in, then push the endpoint the board actually got
    await gateway.wait_connected(
        console_session.session_id, timeout=min(wait_seconds, 60)
    )
    res = await _ensure_console_target(client, gateway, console_session, session.job_id)
    if res.get("status") == "unsupported":
        return _unproxyable_console_note(res)
    if res.get("status") != "ok":
        return (
            "could not resolve the board's console endpoint yet (job may still be "
            "scheduling) — retry with attach_console(console_session_id)"
        )
    return (
        "call attach_console(console_session_id) for the live serial console "
        f"(endpoint {res['host']}:{res['port']})"
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


def _artifact_base_url(config: Config, streamable_path: str) -> str:
    """External ``.../artifacts`` base for artifact URLs handed to jobs.

    Uses ``artifact_base_url`` when set, else derives it from the gateway WebSocket
    URL (same host/scheme, same ``/mcp`` prefix Caddy already routes here). Empty when
    neither is configured — the store cannot advertise a fetch URL, so it stays off.
    """
    if config.artifact_base_url:
        return config.artifact_base_url.rstrip("/")
    if not config.gateway_ws_url:
        return ""
    parsed = urlparse(config.gateway_ws_url)
    scheme = "https" if parsed.scheme in ("wss", "https") else "http"
    return f"{scheme}://{parsed.netloc}{streamable_path.rstrip('/')}/artifacts"


def _presented_token(request: Any) -> str | None:
    """Extract the bearer token from an artifact request's Authorization header.

    LAVA's remote-artifact-token substitution replaces the header VALUE with the raw
    secret, so the value is usually the token itself; humans/containers may prefix it
    with ``Bearer``/``Token``. Accept both.
    """
    value = request.headers.get("authorization")
    if not value:
        return None
    parts = value.split(None, 1)
    if len(parts) == 2 and parts[0].lower() in ("bearer", "token"):
        return parts[1]
    return value


_TERMINAL_JOB_STATES = {"Finished", "Canceling", "Canceled"}


def _register_artifact_routes(
    mcp: FastMCP, config: Config, artifacts: ArtifactStore
) -> None:
    """Mount the artifact PUT (upload) / GET (fetch) routes on the MCP app.

    Siblings of the gateway WebSocket route under ``/mcp/artifacts`` — Caddy already
    routes ``/mcp*`` here, so uploads/downloads ride the same 443 path the dispatcher,
    a device-connected container, or the DUT can reach. Access is by capability id +
    bearer token; a bound job that has finished 410s and drops the artifact.
    """
    from starlette.responses import FileResponse, JSONResponse
    from starlette.routing import Route

    def _server_client() -> LavaClient | None:
        # For best-effort job-binding checks only; works when the server is pinned to
        # a LAVA instance with a token. Multi-tenant (no server creds) skips the check.
        try:
            return client_from(config, None)
        except LavaError:
            return None

    def _authorized(request: Any) -> Any:
        artifact_id = request.path_params["artifact_id"]
        art = artifacts.get(artifact_id)
        if art is None:
            return None, JSONResponse({"error": "not found"}, status_code=404)
        if not artifacts.verify_token(art, _presented_token(request)):
            return None, JSONResponse({"error": "unauthorized"}, status_code=401)
        return art, None

    async def _put(request: Any) -> Any:
        art, err = _authorized(request)
        if err is not None:
            return err
        if art.state != "await_upload":
            return JSONResponse({"error": "already uploaded"}, status_code=409)
        length = request.headers.get("content-length")
        try:
            await artifacts.write_stream(
                art, request.stream(), int(length) if length is not None else None
            )
        except ArtifactError as exc:
            return JSONResponse({"error": str(exc)}, status_code=413)
        return JSONResponse(
            {"stored": True, "artifact_id": art.artifact_id, "size": art.size_actual}
        )

    async def _get(request: Any) -> Any:
        art, err = _authorized(request)
        if err is not None:
            return err
        if art.state != "stored":
            return JSONResponse({"error": "not uploaded yet"}, status_code=409)
        if art.bind_job_id is not None:
            sc = _server_client()
            if sc is not None:
                try:
                    state = (sc.get_job(art.bind_job_id) or {}).get("state")
                except LavaError:
                    state = None
                if state in _TERMINAL_JOB_STATES:
                    artifacts.delete(art.artifact_id)
                    return JSONResponse(
                        {"error": "bound job finished"}, status_code=410
                    )
        return FileResponse(
            artifacts.blob_path(art),
            filename=art.filename,
            media_type="application/octet-stream",
        )

    async def _endpoint(request: Any) -> Any:
        return await (_put(request) if request.method == "PUT" else _get(request))

    base = mcp.settings.streamable_http_path.rstrip("/") + "/artifacts"
    for path in (
        f"{base}/{{artifact_id}}",
        f"{base}/{{artifact_id}}/{{filename:path}}",
    ):
        mcp._custom_starlette_routes.append(
            Route(path, _endpoint, methods=["GET", "PUT"])
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
        instructions=_SERVER_INSTRUCTIONS,
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

    artifacts: ArtifactStore | None = None
    if config.artifacts_enabled:
        base_url = _artifact_base_url(config, mcp.settings.streamable_http_path)
        if not base_url:
            logger.warning(
                "artifacts enabled but no base URL resolvable; set "
                "LAVA_MCP_ARTIFACT_BASE_URL or LAVA_MCP_GATEWAY_WS_URL — store disabled"
            )
        else:
            artifacts = ArtifactStore(
                config.artifact_dir or None,
                base_url=base_url,
                ttl_default=config.artifact_ttl_default,
                ttl_max=config.artifact_ttl_max,
                max_bytes=config.artifact_max_bytes,
                min_free_fraction=config.artifact_min_free_fraction,
            )
            _register_artifact_routes(mcp, config, artifacts)
            mcp._lava_artifacts = artifacts  # type: ignore[attr-defined]

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
        limit: int = 25,
        top: int = 3,
    ) -> Any:
        """Find the best previous job to use as a deploy+boot template for an image.

        Searches this LAVA instance's recent successful (Complete) jobs, extracts each
        job's deploy URL(s), and ranks them by similarity to ``artifact_url`` (same
        artifact filename dominates, then shared path segments, then same host).
        Returns the top matches with their full job ``definition`` so you can adapt it
        directly: swap in your artifact_url, KEEP that job's artifact authentication
        (Authorization headers/credentials), and add the console proxy. Pass
        ``device_type`` to narrow the search. Only ``limit`` recent jobs are scanned
        (reported as ``jobs_scanned``).
        """
        cl = client()
        filters: dict[str, Any] = {"health": "Complete", "ordering": "-id"}
        if device_type:
            filters["device_type"] = device_type
        page = cl.list_jobs(limit=limit, **filters)
        scored: list[dict[str, Any]] = []
        scanned = 0
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
                    "job_id": jid,
                    "device_type": job.get("requested_device_type"),
                    "deploy_url": best_url,
                    "score": best,
                    "definition": defn,
                }
            )
        scored.sort(key=lambda m: m["score"], reverse=True)
        return {
            "artifact_url": artifact_url,
            "jobs_scanned": scanned,
            "matches": scored[:top],
            "note": (
                "Use the top match's `definition` as your deploy+boot base: swap in "
                "artifact_url, KEEP its Authorization/artifact auth, then add the "
                "console proxy (see open_console_session). If matches is empty or "
                "scores are low, raise limit or drop device_type."
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
            console: bool = False,
        ) -> Any:
            """Open a shell in a container running *next to* the board (not on it).

            Way 1 of 2 (see also open_console_session for the board's own serial
            console). Submits a LAVA job (as your LAVA user) that runs a
            device-attached container on the worker, with the board's USB/serial
            exposed — for flashing, fastboot/adb, qdl and bring-up. The container
            dials back to this gateway over SSH; waits up to wait_seconds for it to
            connect, then the session is usable via run_in_session / attach_shell.
            Only devices tagged for remote access can host one.

            Set console=true to ALSO get the board's serial console alongside the
            session (for bring-up: flash over USB while watching the UART). The console
            does NOT come from inside this container — your session container can't reach
            the lab's ser2net endpoint. As for ANY job that wants the serial console
            bridged out (e.g. open_console_session's deploy+boot job), the same
            ser2net-proxy Test Services container runs beside the job on the dispatcher
            network and relays the board's UART out over the gateway; that is why console
            access needs a device that allows Test Services (check_serial_console_support).
            For console=true the server adds and wires that proxy for you automatically.
            The result includes a console_session_id — call
            attach_console(console_session_id) for the live console. Closing the board
            session closes the console too. Only ser2net (telnet) consoles are proxyable;
            for any other lab console tooling the console_note says it is unavailable.
            """
            user = require_user(config.http_allow_users)
            if not config.gateway_ws_url:
                return {"error": _WS_NOT_CONFIGURED}
            _require_remote_access_device(
                client(), device_type, config.remote_access_tag
            )
            await asyncio.to_thread(gateway.ensure_started)
            console_session = None
            if console:
                _require_test_services_device_type(
                    client(), device_type, config.remote_access_tag
                )
                console_session = gateway.manager.create(
                    device_type=device_type, kind="console", owner=user
                )
            session = gateway.manager.create(device_type=device_type, owner=user)
            if console_session is not None:
                session.console_session_id = console_session.session_id
            job_yaml = build_interactive_job(
                config,
                session,
                device_type=device_type,
                tags=tags,
                image=image,
                timeout_minutes=timeout_minutes,
                console_session=console_session,
            )
            result = client().submit_job(job_yaml)
            job_ids = result.get("job_ids") if isinstance(result, dict) else None
            session.job_id = job_ids[0] if job_ids else None
            if console_session is not None:
                console_session.job_id = session.job_id
            connected = await gateway.wait_connected(
                session.session_id, timeout=wait_seconds
            )
            view = session.public_view()
            view["connected"] = connected
            if console_session is not None:
                view["console_session_id"] = console_session.session_id
                view["console_note"] = await _wire_console(
                    client(), gateway, session, console_session, connected, wait_seconds
                )
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
        async def run_device_command(session_id: str, name: str) -> Any:
            """Run a LAVA device command on the worker for a board session.

            Lets you drive the DUT's power/recovery from outside the board — e.g.
            power-cycle it into EDL for flashing — without shelling in. ``name`` is a
            device command: power_on, power_off, hard_reset, recovery_mode,
            recovery_exit, pre_power_command, pre_os_command, or one of the device's
            user_commands (e.g. USB-port toggles). Runs `lava-device-command` in the
            container, which relays the request to the dispatcher (the session's job is
            submitted with device_commands enabled). Returns the command's exit status
            (ok = ran successfully). Requires a LAVA instance with DEVICECMD support.
            """
            user = require_user(config.http_allow_users)
            session = gateway.manager.get(session_id)
            if session is None:
                return {"error": f"unknown session {session_id}"}
            _require_owner(session, user)
            if session.kind != "container":
                return {"error": f"session {session_id} is not a container session"}
            if not name or any(c.isspace() for c in name) or ">" in name:
                return {"error": "invalid device command name (no whitespace or '>')"}
            await asyncio.to_thread(gateway.ensure_started)
            # give the relay's own ack wait (~90s) room to return before gateway.run
            res = await gateway.run(
                session_id, f"lava-device-command {name}", timeout=150
            )
            rc = res.get("exit_status")
            return {
                "name": name,
                "exit_status": rc,
                "ok": rc == 0,
                "stdout": res.get("stdout"),
                "stderr": res.get("stderr"),
            }

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
            # a paired console session shares the same job; drop it too so it doesn't
            # linger (the job cancel below tears its proxy down)
            if session.console_session_id:
                console = gateway.manager.remove(session.console_session_id)
                if console is not None:
                    console.revoke_human_keys()
                    console.status = "closed"
            cancel = client().cancel_job(session.job_id) if session.job_id else None
            session.status = "closed"
            return {
                "closed": True,
                "job_id": session.job_id,
                "cancel": cancel,
                "console_session_closed": session.console_session_id,
            }

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
            find_boot_template(artifact_url, device_type) to search this instance for
            the best URL match. Do NOT use an unrelated job such as a health-check.
            Keep that job's deploy+boot actions —
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
                        "`environment:`, plus: "
                        "SER2NET_NETWORK (docker network ser2net is on, usually "
                        "'lava-dispatcher_default'); "
                        "CONSOLE_READY_SENTINEL (must match the echo in step 3, e.g. "
                        "LAVA_MCP_CONSOLE_WRITABLE). "
                        "Do NOT set SER2NET_HOST/SER2NET_PORT: the console port is "
                        "per-board and unknown until LAVA schedules the job, so the "
                        "server discovers the assigned board's endpoint and pushes it "
                        "to the proxy for you (see step 'then')."
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
                        "Submit the job, then poll "
                        "check_console_ready(job_id, session_id=session_id) until it "
                        "returns ready:true (do NOT scrape logs yourself). Passing "
                        "session_id lets the server wire the console proxy to the board "
                        "LAVA assigned; if that board's console is not a proxyable "
                        "ser2net telnet the reply's console_note says so (console "
                        "unavailable — use a board session instead). Once ready, call "
                        "attach_console(session_id) for a writable console."
                    ),
                },
            }

        @mcp.tool()
        async def check_console_ready(
            job_id: int,
            sentinel: str = "LAVA_MCP_CONSOLE_WRITABLE",
            session_id: str | None = None,
        ) -> Any:
            """Has a console job reached console-ready (writable) state yet?

            Poll THIS instead of reading job logs yourself. It scans job_id's logs for
            the CONSOLE_READY_SENTINEL your deploy+boot job echoes once the board boots
            to a shell — the moment the ser2net-proxy flips the console from read-only
            to writable. Returns {ready, job_state, job_health}: when ready is true,
            attach_console gives a writable console; if job_state is Finished/Canceling
            the board never signalled, so stop polling. Pass sentinel if your job set a
            custom CONSOLE_READY_SENTINEL.

            Pass session_id (from open_console_session) so the server can wire the
            console proxy to the board LAVA actually assigned: the ser2net port is
            per-board and unknown until the job is scheduled, so the server reads the
            job's assigned device and pushes the endpoint to the proxy. If that board's
            console is not a proxyable ser2net telnet, the reply includes a
            console_note explaining the console is unavailable.
            """
            logs = client().get_job_logs(job_id)
            ready = console_ready_in_logs(logs, sentinel)
            job = client().get_job(job_id)
            state = job.get("state") if isinstance(job, dict) else None
            health = job.get("health") if isinstance(job, dict) else None
            result = {
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
            if session_id:
                session = gateway.manager.get(session_id)
                if session is not None and session.kind == "console":
                    await asyncio.to_thread(gateway.ensure_started)
                    if session.job_id is None:
                        session.job_id = job_id
                    res = await _ensure_console_target(
                        client(), gateway, session, job_id
                    )
                    if res.get("status") == "unsupported":
                        result["console_note"] = _unproxyable_console_note(res)
                        result["console_proxyable"] = False
                    elif res.get("status") == "ok":
                        result["console_note"] = (
                            f"console proxy wired to {res['host']}:{res['port']}"
                        )
                        result["console_proxyable"] = True
            return result

        @mcp.tool()
        async def attach_console(session_id: str, job_id: int | None = None) -> Any:
            """Get a command to attach to the board's serial console (UART).

            The interactive form of a console session (Way 2): the board's own console,
            not a container shell (use attach_shell for that). Mints a short-lived key
            authorised for this session and returns an ``ssh -W`` command that tunnels
            to the console through the gateway. The board/proxy key is never disclosed.
            The console is read-only until the job emits console-ready.

            Pass job_id for a standalone console session (open_console_session) if you
            have not already wired it via check_console_ready(session_id=...): the server
            resolves the board LAVA assigned and pushes its ser2net endpoint to the
            proxy. If that board's console is not a proxyable ser2net telnet, the reply's
            ``console_note`` says the console is unavailable (and no ssh command is
            returned).
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
            # ensure the proxy knows which ser2net endpoint to relay (idempotent); for a
            # board-session console this was wired at open time, so console_target is
            # already set and this just re-pushes it.
            if session.job_id is None and job_id is not None:
                session.job_id = job_id
            res = await _ensure_console_target(
                client(), gateway, session, session.job_id
            )
            if res.get("status") == "unsupported":
                return {
                    "session_id": session_id,
                    "console_available": False,
                    "console_note": _unproxyable_console_note(res),
                }
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

    if artifacts is not None:

        def _flush_pending_tokens(c: LavaClient, user: str) -> None:
            """Delete this user's LAVA remote-artifact tokens whose artifact is gone.

            The background reaper cannot act as a LAVA user, so it queues removals; we
            flush the current caller's queue opportunistically on every artifact tool
            call. Best-effort — a failed delete is retried on the next call.
            """
            for name in artifacts.take_pending_token_deletions(user):
                try:
                    c.delete_remote_artifact_token(name)
                except LavaError:
                    pass

        if not config.read_only:

            @mcp.tool()
            def create_artifact_upload(
                filename: str,
                size_bytes: int,
                ttl_seconds: int | None = None,
                bind_job_id: int | None = None,
            ) -> Any:
                """Reserve a temporary upload slot for a build artifact LAVA can fetch.

                For when you have a file (kernel, rootfs, DTB, script, ...) you want a
                LAVA job to deploy/flash, or a board session / booted device to pull, but
                nowhere to host it. The bytes do NOT go through this tool — it only mints
                the slot; you then upload with the returned `put_command` (an HTTP PUT),
                which keeps multi-GB files out of the model context. Pass `size_bytes`
                (the file's real size) so the store can pre-check its per-artifact cap
                and disk floor before you start pushing.

                The artifact is fetchable at `get_url` for up to a few hours (`ttl_max`),
                guarded by a temporary bearer token returned as `token`. That same token
                is ALSO registered with LAVA as a per-user *remote artifact token* named
                `lava-mcp-artifact-<id>`: when a deploy/test action's header value is a
                token NAME you own, LAVA swaps in the secret at download time, so the
                value never appears in the stored job definition or logs. The token (and
                the artifact) are deleted on expiry, on delete_artifact, or when a bound
                job finishes.

                Three ways to consume it:
                - LAVA deploy download (the dispatcher fetches + flashes it): paste
                  `deploy_block` under the image/url in your deploy action — it uses the
                  token NAME, keeping the secret out of the job (see `example_job_snippet`
                  for exactly where it goes). ALSO set the job's top-level
                  `visibility: personal` (see `visibility_note`).
                - onto a BOOTED device that has working networking: run `fetch_command`
                  (plain curl, token inline) from a test action that executes on the DUT
                  — that lands the file on the device's own filesystem (e.g. push a test
                  binary, config, or firmware to a running board). See `on_device_note`.
                - inside a device-connected container (board session): run the same
                  `fetch_command` in the container.
                The inline-token cases expose the token to that context by necessity, so
                rely on the short TTL.

                Optionally pass `bind_job_id`: once that job finishes the artifact stops
                serving. Delete early with delete_artifact; it auto-expires regardless.
                """
                user = require_user(config.http_allow_users)
                c = client()
                _flush_pending_tokens(c, user)
                try:
                    art, token = artifacts.create(
                        filename,
                        size_bytes,
                        user,
                        ttl_seconds=ttl_seconds,
                        bind_job_id=bind_job_id,
                    )
                except ArtifactError as exc:
                    return {"error": str(exc)}
                # register the secret as a LAVA named token so the deploy block can
                # reference the NAME (LAVA substitutes the value at download time).
                token_name = f"lava-mcp-artifact-{art.artifact_id}"
                lava_token_registered = False
                try:
                    c.add_remote_artifact_token(token_name, token)
                    artifacts.set_lava_token_name(art.artifact_id, token_name)
                    lava_token_registered = True
                except LavaError as exc:
                    logger.warning("artifact: LAVA token registration failed: %s", exc)
                url = artifacts.get_url(art)
                header_value = token_name if lava_token_registered else token
                deploy_block = (
                    f"url: {url}\n" f"headers:\n" f"  Authorization: {header_value}"
                )
                # a full deploy action showing exactly where deploy_block sits, so the
                # agent does not have to guess the nesting (indent under images.<label>).
                example_job_snippet = (
                    "- deploy:\n"
                    "    to: <your deploy method, from the template job>\n"
                    "    images:\n"
                    f"      {art.filename.split('.')[0] or 'image'}:\n"
                    f"        url: {url}\n"
                    "        headers:\n"
                    f"          Authorization: {header_value}\n"
                    "# ...keep the template's other deploy/boot params unchanged"
                )
                return {
                    "artifact_id": art.artifact_id,
                    "get_url": url,
                    "expires": art.expires,
                    "token": token,
                    "remote_artifact_token": {
                        "name": token_name if lava_token_registered else None,
                        "registered": lava_token_registered,
                        "how": (
                            "This name is a LAVA remote artifact token holding the "
                            "secret. Put the NAME as a header value in a deploy/test "
                            "url action and LAVA substitutes the real token when the "
                            "dispatcher downloads — the secret never enters the job."
                        ),
                    },
                    "put_command": (
                        f"curl -fsS -T <local-file> "
                        f'-H "Authorization: {token}" {url}'
                    ),
                    "fetch_command": (
                        f'curl -fsSL -H "Authorization: {token}" '
                        f"-o {art.filename} {url}"
                    ),
                    "deploy_block": deploy_block,
                    "example_job_snippet": example_job_snippet,
                    "deploy_note": (
                        "Paste deploy_block under the image/url you want LAVA to "
                        "download (see example_job_snippet for placement). It "
                        "references the token by NAME, so the secret stays out of the "
                        "job."
                        if lava_token_registered
                        else "NOTE: could not register a LAVA named token, so "
                        "deploy_block carries the raw token inline — set the job "
                        "visibility to personal to limit exposure."
                    ),
                    "on_device_note": (
                        "To land this file on a BOOTED device that has working "
                        "networking, run fetch_command from a test action that executes "
                        "on the DUT (e.g. an inline test running `curl`/`wget`). The "
                        "device must be able to reach this server over HTTPS; the file "
                        "is written to the device's own filesystem. Needs curl or wget "
                        "on the target."
                    ),
                    "visibility_note": (
                        "Set `visibility: personal` at the top of any job that "
                        "references this artifact, so its URL is not publicly readable."
                    ),
                    "lava_token_registered": lava_token_registered,
                }

            @mcp.tool()
            def delete_artifact(artifact_id: str) -> Any:
                """Delete an uploaded artifact now and revoke its LAVA token."""
                user = require_user(config.http_allow_users)
                art = artifacts.delete(artifact_id, owner=user)
                _flush_pending_tokens(client(), user)
                if art is None:
                    return {"deleted": False, "reason": "unknown artifact or not yours"}
                return {"deleted": True, "artifact_id": artifact_id}

        @mcp.tool()
        def list_artifacts() -> Any:
            """List your temporary artifacts (id, filename, size, state, expiry)."""
            user = require_user(config.http_allow_users)
            _flush_pending_tokens(client(), user)
            return {"artifacts": [a.public_view() for a in artifacts.list_for(user)]}

    return mcp
