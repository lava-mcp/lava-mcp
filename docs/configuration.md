# Configuration reference

Every setting is read from an environment variable (and, where shown, a CLI flag).
Values come from [`lava_mcp/config.py`](../lava_mcp/config.py), which is authoritative.
See [`.env.example`](../.env.example) for a commented starting point.

## Connection & core

| Env var | CLI flag | Default | Meaning |
|---|---|---|---|
| `LAVA_URL` | `--url` | (empty) | LAVA base URL. Pins the server to one instance; empty = multi-tenant (clients send `X-Lava-Url`). stdio fallback. |
| `LAVA_TOKEN` | `--token` | (none) | API token; stdio fallback (HTTP clients send `X-Lava-Token`). |
| `LAVA_API_VERSION` | `--api-version` | `v0.3` | REST version. |
| `LAVA_MCP_READ_ONLY` | `--read-only` | `false` | Hide write tools (submit/cancel/resubmit, artifact create/delete). |
| `LAVA_MCP_TIMEOUT` | — | `30` | LAVA HTTP client timeout (seconds). |

## Serving (hosted mode)

| Env var | CLI flag | Default | Meaning |
|---|---|---|---|
| `LAVA_MCP_TRANSPORT` | `--transport` | `stdio` | `stdio` or `streamable-http`. |
| `LAVA_MCP_HOST` / `LAVA_MCP_PORT` | `--host` / `--port` | `127.0.0.1` / `8000` | HTTP bind. |
| `LAVA_MCP_JSON_RESPONSE` | — | `true` | Plain-JSON HTTP responses (proxy/client friendly). |
| `LAVA_MCP_STATELESS` | — | `false` | Keep sessions stateful so the gateway lifespan starts once. |

## Interactive SSH gateway

Powers [board sessions](board-sessions.md) and the [serial console](serial-console.md).

| Env var | CLI flag | Default | Meaning |
|---|---|---|---|
| `LAVA_MCP_GATEWAY_ENABLED` | `--gateway` | `false` | Enable the interactive SSH gateway. |
| `LAVA_MCP_GATEWAY_PORT` | `--gateway-port` | `2222` | Internal loopback asyncssh port the WS bridge relays to. |
| `LAVA_MCP_GATEWAY_ADVERTISE_HOST` | `--gateway-advertise-host` | (host) | Host label containers dial back to. |
| `LAVA_MCP_GATEWAY_ADVERTISE_PORT` | — | (none) | Advertised port, if different. |
| `LAVA_MCP_GATEWAY_WS_URL` | `--gateway-ws-url` | (empty) | Advertised `wss://host/mcp/gateway-ssh` URL (served on the MCP app, same port as `/mcp`). **Required** for interactive sessions; clients need `websocat`. |
| `LAVA_MCP_GATEWAY_HUMAN_KEY_TTL` | — | `3600` | Lifetime (seconds) of an ephemeral human key from `attach_*`. |

## Access control (all default to open)

The general LAVA-proxy tools are **never** gated — they equal using your own LAVA token.
These gate only the interactive features.

| Env var | CLI flag | Meaning |
|---|---|---|
| `LAVA_MCP_GATEWAY_ALLOW_IPS` | `--gateway-allow-ip` | Source IPs/CIDRs allowed to reach the gateway (checked at the WS bridge against Caddy's forwarded client IP; empty = all). |
| `LAVA_MCP_HTTP_ALLOW_USERS` | `--http-allow-user` | LAVA users allowed the interactive *use* tools (open/run/close/list session, open console, support check). |
| `LAVA_MCP_SSH_ALLOW_USERS` | `--ssh-allow-user` | LAVA users allowed the *attach* tools that hand out SSH/console keys. |
| `LAVA_MCP_REMOTE_ACCESS_TAG` | `--remote-access-tag` | Device tag a device must carry to host remote-access sessions (default `allow-remote-access`; empty = no per-device gate). |

## Interactive session assets

Where lab workers pull the container image and fetch the test definition. Override only
if you host them elsewhere.

| Env var | Default |
|---|---|
| `LAVA_MCP_INTERACTIVE_IMAGE` | `ghcr.io/lava-mcp/lava-mcp/interactive:latest` |
| `LAVA_MCP_INTERACTIVE_REPO` | `https://github.com/lava-mcp/lava-mcp.git` |
| `LAVA_MCP_INTERACTIVE_PATH` | `interactive/ssh-gateway.yaml` |

## Artifact store

See [artifact-store.md](artifact-store.md).

| Env var | Default | Meaning |
|---|---|---|
| `LAVA_MCP_ARTIFACTS_ENABLED` | `0` | Enable the temporary artifact store. |
| `LAVA_MCP_ARTIFACT_DIR` | (temp subdir) | On-disk store location (stable default survives restart). |
| `LAVA_MCP_ARTIFACT_BASE_URL` | (from gateway WS URL) | External base URL for `get_url` (derived unless set). |
| `LAVA_MCP_ARTIFACT_TTL_DEFAULT` | `21600` (6h) | Default artifact TTL (seconds). |
| `LAVA_MCP_ARTIFACT_TTL_MAX` | `21600` (6h) | Maximum artifact TTL. |
| `LAVA_MCP_ARTIFACT_MAX_BYTES` | `6442450944` (6 GB) | Per-artifact size cap. |
| `LAVA_MCP_ARTIFACT_MIN_FREE_FRACTION` | `0.10` | Reject uploads that would cross this free-disk line. |

## LAVA docs & source mirror

See [lava-source-docs.md](lava-source-docs.md).

| Env var | Default | Meaning |
|---|---|---|
| `LAVA_SOURCE_REPO` | (empty) | Git repo the deployed LAVA was built from. Enables `read_lava_docs`. |
| `LAVA_SOURCE_REF` | (derived) | Ref to check out; empty = derive from the LAVA API version. |
| `LAVA_SOURCE_DIR` | (temp subdir) | Local checkout dir for the mirror. |
| `LAVA_SOURCE_POLL_INTERVAL` | `300` | Seconds between version re-reads / checkout updates. |
