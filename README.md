# lava-mcp

An [MCP](https://modelcontextprotocol.io) server that exposes a
[LAVA](https://www.lavasoftware.org/) instance to agents — letting them query the
board farm, submit and manage test jobs, and (in a later phase) open interactive
sessions to a board.

It is a thin client over LAVA's REST API (v0.2/v0.3); point it at any LAVA instance.

## Install

```sh
pip install -e .[dev]
```

## Credentials

The LAVA **target** (`LAVA_URL`) is normally pinned to the instance a deployment
fronts; the **token** is resolved per request:

- **Hosted (HTTP), pinned** — set `LAVA_URL` on the server to the LAVA instance it
  serves. Each connecting client then sends only its own `X-Lava-Token` and acts as
  its own LAVA user; no `X-Lava-Url` is needed. The server stores no per-user token.
- **Hosted (HTTP), multi-tenant** — leave `LAVA_URL` unset; each client sends both
  `X-Lava-Url` and `X-Lava-Token`, so one server can front many LAVA instances.
- **Local (stdio) mode** — falls back to `LAVA_URL` / `LAVA_TOKEN` in the
  environment (single user).

## Run (stdio, local)

```sh
export LAVA_URL=https://lava.example.com
export LAVA_TOKEN=<your-api-token>
lava-mcp                                  # or: lava-mcp --read-only
```

Launch over stdio from your MCP client (`claude_desktop_config.json` / Claude Code):

```json
{
  "mcpServers": {
    "lava": {
      "command": "lava-mcp",
      "env": { "LAVA_URL": "https://lava.example.com", "LAVA_TOKEN": "..." }
    }
  }
}
```

For a hosted server pinned to a LAVA instance, point Claude Code at the HTTP
endpoint and pass only your token:

```sh
claude mcp add --transport http lava https://mcp.example.com/mcp \
  --header "X-Lava-Token: <your-api-token>"
```

If the server is multi-tenant (no `LAVA_URL` set), also pass
`--header "X-Lava-Url: https://lava.example.com"` to choose the instance.

## Run as a hosted service (HTTPS via Caddy)

For interactive board sessions the server must be reachable by lab workers, so run
it hosted. `docker compose` brings up the MCP server behind Caddy (automatic HTTPS)
and exposes the SSH board-session gateway:

```sh
cp .env.example .env      # set LAVA_URL/LAVA_TOKEN/LAVA_MCP_DOMAIN/LAVA_MCP_GATEWAY_WS_URL
docker compose up -d
```

- Agents connect to `https://$LAVA_MCP_DOMAIN/mcp` (streamable-HTTP transport).
- In-job containers and humans reach the SSH gateway over the WebSocket transport at
  `wss://$LAVA_MCP_DOMAIN/mcp/gateway-ssh` — a WebSocket route on the MCP app itself
  (same port as `/mcp`, so Caddy's existing `/mcp` route serves it). Set
  `LAVA_MCP_GATEWAY_WS_URL` to that URL; clients use `websocat`.

Or run the HTTP transport directly:

```sh
lava-mcp --transport streamable-http --host 0.0.0.0 --port 8000 --gateway
```

## Interactive board sessions (gateway)

In hosted mode with `--gateway` (or `LAVA_MCP_GATEWAY_ENABLED=true`), the server runs
an in-process SSH rendezvous fronted by a WebSocket bridge. `open_board_session`
submits a LAVA job that runs a device-attached container; the container dials **out**
(`ssh -R`, tunnelled over `wss://.../mcp/gateway-ssh` via `websocat`) to the gateway, so
no inbound access to the worker is needed. The asyncssh listener is loopback-only and
reachable exclusively through the bridge; there is no direct SSH port.

```mermaid
sequenceDiagram
    actor Agent as Agent (MCP client)
    participant MCP as lava-mcp + SSH gateway
    participant LAVA as LAVA scheduler
    participant Board as Board container<br/>(on lab worker)

    Agent->>MCP: open_board_session(device_type)
    Note over MCP: mint per-session ed25519 keypair,<br/>allocate reverse port, create session
    MCP->>LAVA: submit_job (interactive job,<br/>key + gateway ws url + reverse port)
    LAVA->>Board: schedule on a worker with the board,<br/>run device-attached container
    Board->>MCP: ssh -R reverse_port:localhost:22<br/>(over wss via websocat, auth with session key)
    Note over MCP: validate key, accept reverse forward,<br/>mark session "connected"
    MCP-->>Agent: session connected

    Agent->>MCP: run_in_session(session_id, command)
    MCP->>Board: ssh back through tunnel<br/>(127.0.0.1:reverse_port), run command
    Board-->>MCP: exit status + stdout/stderr
    MCP-->>Agent: command output

    Agent->>MCP: close_board_session(session_id)
    MCP->>LAVA: cancel job (releases the board)
```

Then:

- `run_in_session(session_id, command)` runs a command on the board's container
  (e.g. `qdl`, `fastboot`, `adb`, shell).
- `close_board_session(session_id)` cancels the job and frees the board.

The container image + test definition live in this repo under `interactive/`
(published to `ghcr.io/mattface/lava-mcp/interactive` and fetched from this repo by
the lab worker); the parameter contract is in `lava_mcp/jobs.py`.

Optional allowlists gate the interactive features (all default to open). The general
LAVA-proxy tools are **never** gated here — they are equivalent to using your own LAVA
token, so `/mcp` is usable by any token holder.

- `LAVA_MCP_GATEWAY_ALLOW_IPS` — comma/space-separated IPs or CIDRs permitted to reach
  the gateway. It is enforced at the WebSocket bridge against Caddy's forwarded client
  IP and applies to **every** connection — in-job containers and humans alike — dropped
  before authentication. Set it to your lab worker network plus any human/VPN source
  ranges.
- `LAVA_MCP_HTTP_ALLOW_USERS` — LAVA users (via `whoami`) allowed the interactive *use*
  tools: `open_board_session`, `run_in_session`, `close_*`, `list_*`,
  `open_console_session`, `check_serial_console_support`.
- `LAVA_MCP_SSH_ALLOW_USERS` — users allowed the *attach* tools that hand out gateway/SSH
  keys: `attach_shell`, `attach_console`.

Interactive sessions are also gated **per device**: they only run on devices an admin
has opted in by tagging with `allow-remote-access` (override the tag name with
`LAVA_MCP_REMOTE_ACCESS_TAG`; set it empty to disable the gate). `open_board_session`
checks up front that the device-type has at least one such device and fails with an
actionable message if not, and every interactive job is pinned to the tag so LAVA only
schedules it on a permitted device.

The gateway tunnels into an isolated lab network, so its trust model matters — see
[docs/security.md](docs/security.md) for the roles, enforced controls (loopback-only
reverse tunnels, per-session keys, ephemeral human keys, session ownership), and the
operator responsibilities (set `LAVA_MCP_GATEWAY_ALLOW_IPS`; Caddy's `/mcp` route already fronts the `/mcp/gateway-ssh` WebSocket).

### For humans (without an agent)

The gateway has no dedicated human client — but the board-session tools are just MCP
calls, so a person can drive the exact same open → run → close flow by hand. Point any
generic MCP client at the hosted endpoint with your token; no LLM is involved.

The quickest is the [MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```sh
npx @modelcontextprotocol/inspector
# In the UI: Transport = Streamable HTTP
#            URL       = https://<LAVA_MCP_DOMAIN>/mcp
#            Header    = X-Lava-Token: <your-api-token>
# (add X-Lava-Url too if the server is multi-tenant)
```

Then invoke the same tools the agent would:

1. `open_board_session` with `device_type` (e.g. `qcs6490-rb3gen2-core-kit`) — reserves
   a board, submits the LAVA job, and waits for the container to dial back. The result
   includes the `session_id` and `connected: true`.
2. `run_in_session` with that `session_id` and a `command` (`qdl`, `fastboot`, `adb`,
   any shell) — returns the exit status, stdout and stderr.
3. `close_board_session` with the `session_id` — cancels the job and frees the board.

This is command-at-a-time execution over the gateway, not a live PTY.

### Interactive SSH shell for humans (`attach_shell`)

For a live PTY in the board's **container** — not command-at-a-time — call
`attach_shell(session_id)`. This is the interactive form of a board session: a shell
*next to* the board (not on it), for controlling how the board is driven from the host
— trying different flashing software/versions, custom fastboot/qdl/adb sequences, or
deeper USB debugging of a board that won't boot. (For the board's own console, use
`attach_console`.) It mints a short-lived keypair, authorises it **both** at the gateway
(for the tunnel)
and inside the board container (appended to its `authorized_keys` over the existing
session), and returns a private key plus a ready-to-use `ssh_config`. The config's jump
host tunnels to the gateway over `wss://.../mcp/gateway-ssh` (via `websocat`), then
`ProxyJump`s into the container's own sshd — the gateway forwards but offers no shell of
its own, and the container's key is never disclosed. Requires `websocat` on your PATH:

```sh
# save private_key to lava-shell-<id>.key (chmod 600) and ssh_config to
# lava-shell-<id>.conf, then:
ssh -F lava-shell-<id>.conf board-<id>
```

```mermaid
sequenceDiagram
    actor Human
    participant MCP as lava-mcp + SSH gateway
    participant Board as Board container<br/>(reverse tunnel up, runs sshd)

    Human->>MCP: attach_shell(session_id)
    Note over MCP: mint ephemeral human key; authorise it<br/>at the gateway AND in the container
    MCP->>Board: append human key to authorized_keys<br/>(over the session)
    MCP-->>Human: private key + ssh_config (websocat + ProxyJump)

    Human->>MCP: ssh -F conf (wss via websocat, human key)
    Note over MCP: human role — allow direct-tcpip to<br/>127.0.0.1:reverse_port only
    MCP->>Board: tunnel to the container sshd
    Human->>Board: authenticate as root (human key) → PTY
    loop live interactive shell
        Human->>Board: keystrokes (via gateway tunnel)
        Board-->>Human: terminal output (via gateway tunnel)
    end

    Human->>MCP: close_board_session(session_id)
    Note over MCP: revoke human key; cancel job (container destroyed)
```

Human keys expire (`LAVA_MCP_GATEWAY_HUMAN_KEY_TTL`, default 1h) and are revoked on
`close_board_session`. Your source IP must be inside `LAVA_MCP_GATEWAY_ALLOW_IPS` if set.
See [docs/security.md](docs/security.md) for the full model.

### Direct serial console via ser2net (`open_console_session` / `attach_console`)

Where `attach_shell` gives you the board's *userspace* (it needs a booted, networked
board), a **serial console** is the board's actual UART — boot/kernel/panic logs, works
with no DUT networking, and the login prompt itself. Many LAVA labs front the UART with
[ser2net](https://github.com/cminyard/ser2net) (the device dict's `connection_command`
is `telnet <ser2net-host> <port>`), and LAVA drives boot over that same console. This is
**Mode 2** — used with a LAVA job that deploys and boots an image and runs its test *on
the board* (no device-attached container), so the in-lab foothold comes from a **LAVA
Test Services** container (`interactive/ser2net-proxy/`) instead. It needs
`allow_test_services: true` in the device dict (check with `check_serial_console_support`).

**Reach for the console** to interact with the booted board directly — drive tests and
run commands live at the console *without writing a LAVA test definition*, watch the
boot, or work with the bootloader/login prompt — whereas a board session (`attach_shell`)
is for host-side work *next to* the board (flashing, fastboot/adb/qdl, USB debugging).

**Don't hand-author the deploy+boot job** — getting boot right per device is hard. Start
from a job that already boots this device and adapt it: `get_job_definition` of a recent
successful job for the device, or its **health-check job** (`get_device` →
`last_health_report_job` → `get_job_definition`). Keep its deploy+boot actions, then add
the console proxy on top. A ready template ships at
[`interactive/ser2net-proxy/test-job-qcs615.yaml`](interactive/ser2net-proxy/test-job-qcs615.yaml).

Flow:

1. `open_console_session()` mints a session and returns a `job_environment` block. Add it
   to your deploy-and-boot job's top-level `environment:`, include the
   `interactive/ser2net-proxy` **services** block, and set the `SER2NET_*` vars for your
   lab. Submit the job.
2. The proxy starts at the beginning of the job, relays the console **read-only** while
   LAVA drives the boot, and dials **out** (`ssh -R`) to the gateway. When your
   console-ready test echoes the sentinel (`LAVA_MCP_CONSOLE_WRITABLE`), the proxy
   enables writes.
3. `attach_console(session_id)` returns an `ssh -W` command that tunnels to the gateway
   over `wss://.../mcp/gateway-ssh` (via `websocat`; wrap with `socat` for a raw PTY) — you
   get the live UART, bridged through the gateway on a loopback-only port.
4. `close_console_session(session_id)` revokes access; ending the job tears down the proxy.

```mermaid
sequenceDiagram
    actor Human
    participant MCP as lava-mcp + gateway
    participant Proxy as ser2net-proxy<br/>(Test Services, in lab)
    participant Ser2net as ser2net → board UART

    Human->>MCP: open_console_session() → job_environment
    Note over Human: embed in a deploy+boot job<br/>with the services block, submit
    Proxy->>Ser2net: connect to the console (read-only)
    Proxy->>MCP: dial out ssh -R over wss (websocat, loopback reverse port)
    Note over Proxy: console-ready sentinel → enable writes
    Human->>MCP: attach_console(session_id) → ssh -W command
    Human->>MCP: ssh -W (wss via websocat, human key)
    MCP->>Proxy: tunnel to the relay
    loop live serial console
        Human->>Ser2net: keystrokes (via gateway → proxy)
        Ser2net-->>Human: boot/kernel logs + shell output
    end
    Human->>MCP: close_console_session(session_id)
```

**Console handoff** wrinkle: ser2net must allow the proxy's concurrent connection (or the
job idles after boot so LAVA releases the console). Confirmed working on staging. A ready
test job is in `interactive/ser2net-proxy/test-job-qcs615.yaml`.

## Configuration

| Env var | CLI flag | Meaning |
|---|---|---|
| `LAVA_URL` | `--url` | LAVA base URL (stdio fallback; HTTP clients send `X-Lava-Url`) |
| `LAVA_TOKEN` | `--token` | API token (stdio fallback; HTTP clients send `X-Lava-Token`) |
| `LAVA_API_VERSION` | `--api-version` | REST version, default `v0.3` |
| `LAVA_MCP_READ_ONLY` | `--read-only` | Hide write tools (submit/cancel/resubmit) |
| `LAVA_MCP_TRANSPORT` | `--transport` | `stdio` (default) or `streamable-http` |
| `LAVA_MCP_HOST` / `LAVA_MCP_PORT` | `--host` / `--port` | HTTP bind (hosted mode) |
| `LAVA_MCP_GATEWAY_ENABLED` | `--gateway` | Enable interactive SSH board-session gateway |
| `LAVA_MCP_GATEWAY_PORT` | `--gateway-port` | Internal loopback asyncssh port the WS bridge relays to (default 2222) |
| `LAVA_MCP_GATEWAY_ADVERTISE_HOST` | `--gateway-advertise-host` | Host containers dial back to |
| `LAVA_MCP_GATEWAY_WS_URL` | `--gateway-ws-url` | Advertised `wss://host/mcp/gateway-ssh` URL; the SSH gateway is served as a WebSocket route on the MCP app (same port as `/mcp`). Required for interactive sessions; clients need `websocat` |
| `LAVA_MCP_GATEWAY_ALLOW_IPS` | `--gateway-allow-ip` | Source IPs/CIDRs allowed to reach the SSH gateway (empty = all) |
| `LAVA_MCP_HTTP_ALLOW_USERS` | `--http-allow-user` | LAVA users allowed the interactive 'use' tools (empty = all) |
| `LAVA_MCP_SSH_ALLOW_USERS` | `--ssh-allow-user` | LAVA users allowed the 'attach' (SSH/console) tools (empty = all) |
| `LAVA_MCP_REMOTE_ACCESS_TAG` | `--remote-access-tag` | Device tag required to host remote-access sessions (empty = no gate) |
| `LAVA_MCP_GATEWAY_HUMAN_KEY_TTL` | — | Lifetime (seconds) of an ephemeral human access key from `attach_*` (default 3600) |

## Tools (v1)

Read/observe: `whoami`, `version`, `list_devices`, `get_device`,
`get_device_dictionary`, `list_device_types`, `list_workers`, `list_jobs`,
`get_job`, `get_job_definition`, `get_job_logs`, `get_job_results`, `get_queue`,
`get_running`, `get_lab_health`, `validate_job`.

Write (omitted with `--read-only`): `submit_job`, `cancel_job`, `resubmit_job`.

Interactive board sessions (hosted gateway mode): `open_board_session`,
`run_in_session`, `attach_shell`, `close_board_session`, `list_board_sessions`.

Serial console (hosted gateway mode): `check_serial_console_support`,
`open_console_session`, `attach_console`, `close_console_session`.

## Test

```sh
pytest
```

## Roadmap

- The interactive **board sessions** gateway is implemented here, along with the
  container image + test definition the in-job container runs (`interactive/`,
  published to `ghcr.io/mattface/lava-mcp/interactive`).
- Human shell proxy + interactive PTY through the gateway (design above).
- Direct serial console for humans via ser2net, gated on a `console-ready` job signal
  (design above).
