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
cp .env.example .env      # set LAVA_URL/LAVA_TOKEN/LAVA_MCP_DOMAIN/LAVA_MCP_GATEWAY_HOST
docker compose up -d
```

- Agents connect to `https://$LAVA_MCP_DOMAIN/mcp` (streamable-HTTP transport).
- In-job containers dial the SSH gateway at `$LAVA_MCP_GATEWAY_HOST:2222`.

Or run the HTTP transport directly:

```sh
lava-mcp --transport streamable-http --host 0.0.0.0 --port 8000 --gateway
```

## Interactive board sessions (gateway)

In hosted mode with `--gateway` (or `LAVA_MCP_GATEWAY_ENABLED=true`), the server runs
an in-process SSH rendezvous. `open_board_session` submits a LAVA job that runs a
device-attached container; the container dials **out** (`ssh -R`) to the gateway, so
no inbound access to the worker is needed.

```mermaid
sequenceDiagram
    actor Agent as Agent (MCP client)
    participant MCP as lava-mcp + SSH gateway
    participant LAVA as LAVA scheduler
    participant Board as Board container<br/>(on lab worker)

    Agent->>MCP: open_board_session(device_type)
    Note over MCP: mint per-session ed25519 keypair,<br/>allocate reverse port, create session
    MCP->>LAVA: submit_job (interactive job,<br/>key + gateway host/port + reverse port)
    LAVA->>Board: schedule on a worker with the board,<br/>run device-attached container
    Board->>MCP: ssh -R reverse_port:localhost:22<br/>(auth with session key)
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

Two optional allowlists gate the gateway (both default to open):

- `LAVA_MCP_GATEWAY_ALLOW_IPS` — comma/space-separated IPs or CIDRs permitted to
  connect to the SSH gateway on `:2222`. Connections from anywhere else are dropped
  before authentication. Set this to your lab worker network so only in-job containers
  can dial in.
- `LAVA_MCP_GATEWAY_ALLOW_USERS` — comma/space-separated LAVA usernames permitted to
  use the board-session tools. The server discovers the caller's identity from their
  token via the `whoami` API and rejects anyone off the list.

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

### Interactive SSH shell for humans (planned)

> **Not yet implemented** — this is the design for a real interactive shell over the
> same gateway. The tool and flags below don't exist yet; tracked on the
> [roadmap](#roadmap).

The plan is for the gateway to double as an **SSH bastion** so a person gets a live PTY
on the board without an agent and without ever seeing the container's private key. It
reuses the one listener already published on `:2222`, telling the two callers apart by
key and channel:

- the **container** authenticates with its per-session key and may only request the
  reverse forward (as it does today);
- a **human** authenticates with a short-lived, per-session key and may only open a
  shell — which the gateway bridges into the board over the existing reverse tunnel,
  doing the inner hop with the container key itself.

A person would open a session (`open_board_session`), then call a new
`attach_session(session_id)` tool. That mints an ephemeral keypair, authorizes it for
the session, and returns the private key plus a ready-to-run command over the
already-authenticated MCP/HTTPS channel — the container key never leaves the server.
One line then drops you into the board shell:

```sh
ssh -i lava-session-<id> -p 2222 <session_id>@<gateway-host>
```

```mermaid
sequenceDiagram
    actor Human
    participant MCP as lava-mcp + SSH gateway<br/>(bastion)
    participant Board as Board container<br/>(reverse tunnel already up)

    Note over MCP,Board: session already open — container dialed back,<br/>tunnel live at 127.0.0.1:reverse_port

    Human->>MCP: attach_session(session_id)
    Note over MCP: mint ephemeral human keypair,<br/>authorize it for this session
    MCP-->>Human: private key + ready-to-run ssh command

    Human->>MCP: ssh -p 2222 session_id@gateway<br/>(human key, shell channel)
    Note over MCP: authenticate human key,<br/>allow shell channel only
    MCP->>Board: open inner SSH over tunnel<br/>(container key), request PTY + shell
    Board-->>MCP: PTY
    MCP-->>Human: bridged PTY

    loop live interactive shell
        Human->>Board: keystrokes (via gateway)
        Board-->>Human: terminal output (via gateway)
    end

    Human->>MCP: close_board_session(session_id)
    Note over MCP: revoke human key
    MCP->>Board: cancel job (releases the board)
```

Human keys are per-session and revoked on `close_board_session`; the whole path sits
behind a `LAVA_MCP_GATEWAY_HUMAN_ENABLED` flag.

### Direct serial console via ser2net (planned)

> **Not yet implemented** — design only. The tool, signal, and flag below don't exist
> yet; tracked on the [roadmap](#roadmap).

Where the SSH shell above gives you the board's *userspace* (it needs a booted,
networked board), a **serial console** is the board's actual UART — it shows boot,
kernel and panic logs, works with no DUT networking, and hands you the login prompt
itself. Many LAVA labs already front the board's UART with
[ser2net](https://github.com/cminyard/ser2net) — the device dictionary's
`connection_command` is typically `telnet <ser2net-host> <port>`, and LAVA drives boot
over that same console. This path lets a human attach to it.

The catch is not to fight LAVA for the console while it is driving boot. So the
interactive test definition boots the board and, on reaching a known shell prompt,
emits a LAVA signal (a `console-ready` lava-test-case). lava-mcp waits for that flag
before exposing the console:

1. Human opens a session; lava-mcp submits a boot-to-shell job that emits
   `console-ready`.
2. lava-mcp watches the job's signals/results and exposes nothing during boot.
3. Once flagged, a new `get_serial_console(session_id)` tool returns a connect command.
   Remote humans are bridged through the existing gateway (the ser2net TCP port is
   reverse-forwarded out, the same `ssh -R` mechanism); in-lab humans get the
   `telnet <host> <port>` directly.
4. Human attaches to the **live raw UART** — kernel logs, login prompt, the lot.
5. `close_board_session` ends the job; LAVA reclaims the console and releases the board.

```mermaid
sequenceDiagram
    actor Human
    participant MCP as lava-mcp + gateway
    participant LAVA as LAVA (dispatcher<br/>drives the console)
    participant Ser2net as ser2net → board UART

    Human->>MCP: open_board_session(device_type)
    MCP->>LAVA: submit boot-to-shell job<br/>(emit console-ready on prompt)
    LAVA->>Ser2net: drive boot over the serial console
    Note over LAVA,Ser2net: watch for the shell prompt
    LAVA-->>MCP: signal "booted to shell" (console-ready)

    Human->>MCP: get_serial_console(session_id)
    Note over MCP: bridge ser2net's TCP port out<br/>via the gateway (ssh -R)
    MCP-->>Human: connect command (telnet/nc through gateway)

    Human->>Ser2net: attach to the raw UART (via gateway)
    loop live serial console
        Human->>Ser2net: keystrokes
        Ser2net-->>Human: boot/kernel logs + shell output
    end

    Human->>MCP: close_board_session(session_id)
    MCP->>LAVA: cancel job — LAVA reclaims console, releases board
```

**Console handoff** is the one wrinkle: either ser2net is configured for multiple
connections (the human shares the console LAVA already holds), or the job idles after
boot — reservation held, console released — and the human takes it over. Gated behind a
`LAVA_MCP_SERIAL_CONSOLE_ENABLED` flag.

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
| `LAVA_MCP_GATEWAY_PORT` | `--gateway-port` | SSH gateway port (default 2222) |
| `LAVA_MCP_GATEWAY_ADVERTISE_HOST` | `--gateway-advertise-host` | Host containers dial back to |
| `LAVA_MCP_GATEWAY_ALLOW_IPS` | `--gateway-allow-ip` | Source IPs/CIDRs allowed to reach the SSH gateway (empty = all) |
| `LAVA_MCP_GATEWAY_ALLOW_USERS` | `--gateway-allow-user` | LAVA usernames allowed to open board sessions (empty = all) |

## Tools (v1)

Read/observe: `whoami`, `version`, `list_devices`, `get_device`,
`get_device_dictionary`, `list_device_types`, `list_workers`, `list_jobs`,
`get_job`, `get_job_definition`, `get_job_logs`, `get_job_results`, `get_queue`,
`get_running`, `get_lab_health`, `validate_job`.

Write (omitted with `--read-only`): `submit_job`, `cancel_job`, `resubmit_job`.

Interactive board sessions (hosted gateway mode): `open_board_session`,
`run_in_session`, `close_board_session`, `list_board_sessions`.

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
