# Deployment & credentials

How to install, authenticate, and run `lava-mcp` — locally over stdio or as a hosted
HTTPS service. For the full list of environment variables and CLI flags see
[configuration.md](configuration.md).

## Install

```sh
pip install -e .[dev]
```

## Credentials

The LAVA **target** (`LAVA_URL`) is normally pinned to the instance a deployment
fronts; the **token** is resolved per request. The server stores no per-user token —
each client acts as its own LAVA user, so every general LAVA tool grants exactly what
that user's own token grants.

- **Hosted (HTTP), pinned** — set `LAVA_URL` on the server to the LAVA instance it
  serves. Each connecting client then sends only its own `X-Lava-Token` header; no
  `X-Lava-Url` is needed.
- **Hosted (HTTP), multi-tenant** — leave `LAVA_URL` unset; each client sends both
  `X-Lava-Url` and `X-Lava-Token`, so one server can front many LAVA instances.
- **Local (stdio) mode** — falls back to `LAVA_URL` / `LAVA_TOKEN` in the environment
  (single user).

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

For a hosted server pinned to a LAVA instance, point Claude Code at the HTTP endpoint
and pass only your token:

```sh
claude mcp add --transport http lava https://mcp.example.com/mcp \
  --header "X-Lava-Token: <your-api-token>"
```

If the server is multi-tenant (no `LAVA_URL` set), also pass
`--header "X-Lava-Url: https://lava.example.com"` to choose the instance.

## Run as a hosted service (HTTPS via Caddy)

The [interactive board sessions](board-sessions.md), [serial console](serial-console.md)
and [artifact store](artifact-store.md) all require the server to be reachable by lab
workers (and, for the console/shell, by humans), so those features only work in hosted
mode. `docker compose` brings up the MCP server behind Caddy (automatic HTTPS) and
exposes the SSH board-session gateway:

```sh
cp .env.example .env      # set LAVA_URL, LAVA_MCP_DOMAIN, LAVA_MCP_GATEWAY_HOST, ...
docker compose up -d
```

- Agents connect to `https://$LAVA_MCP_DOMAIN/mcp` (streamable-HTTP transport).
- In-job containers and humans reach the SSH gateway over the WebSocket transport at
  `wss://$LAVA_MCP_DOMAIN/mcp/gateway-ssh` — a WebSocket route on the MCP app itself
  (same port as `/mcp`, so Caddy's existing `/mcp` route serves it). Set
  `LAVA_MCP_GATEWAY_WS_URL` to that URL; clients use `websocat`.
- The [artifact store](artifact-store.md) routes ride the same `/mcp*` path under
  `/mcp/artifacts` (uploads/downloads over 443).

Or run the HTTP transport directly:

```sh
lava-mcp --transport streamable-http --host 0.0.0.0 --port 8000 --gateway
```

## Container image

The `Dockerfile` builds the hostable server. It installs `git` and `ca-certificates`
because the [LAVA source/docs mirror](lava-source-docs.md) clones the deployed LAVA
source with `git`.
