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

The LAVA target + token are resolved **per request**:

- **Hosted (HTTP) mode** — each connecting client sends its own
  `X-Lava-Url` and `X-Lava-Token` headers, so every agent/human acts as their own
  LAVA user. The server stores no LAVA credentials.
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

For a hosted server, point Claude Code at the HTTP endpoint and pass your LAVA
credentials as headers:

```sh
claude mcp add --transport http lava https://mcp.example.com/mcp \
  --header "X-Lava-Url: https://lava.example.com" \
  --header "X-Lava-Token: <your-api-token>"
```

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
no inbound access to the worker is needed. Then:

- `run_in_session(session_id, command)` runs a command on the board's container
  (e.g. `qdl`, `fastboot`, `adb`, shell).
- `close_board_session(session_id)` cancels the job and frees the board.

The container image + test definition live in this repo under `interactive/`
(published to `ghcr.io/mattface/lava-mcp/interactive` and fetched from this repo by
the lab worker); the parameter contract is in `lava_mcp/jobs.py`.

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
- Human shell proxy + interactive PTY through the gateway.
