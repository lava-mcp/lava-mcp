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

## Run (stdio)

```sh
export LAVA_URL=https://lava.example.com
export LAVA_TOKEN=<your-api-token>      # optional for public endpoints
lava-mcp                                 # or: lava-mcp --read-only
```

Configure your MCP client to launch `lava-mcp` over stdio. Example
(`claude_desktop_config.json` / Claude Code `mcp` config):

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

## Configuration

| Env var | CLI flag | Meaning |
|---|---|---|
| `LAVA_URL` | `--url` | LAVA base URL (required) |
| `LAVA_TOKEN` | `--token` | API token (`Authorization: Token …`) |
| `LAVA_API_VERSION` | `--api-version` | REST version, default `v0.3` |
| `LAVA_MCP_READ_ONLY` | `--read-only` | Hide write tools (submit/cancel/resubmit/priority) |

## Tools (v1)

Read/observe: `whoami`, `version`, `list_devices`, `get_device`,
`get_device_dictionary`, `list_device_types`, `list_workers`, `list_jobs`,
`get_job`, `get_job_definition`, `get_job_logs`, `get_job_results`, `get_queue`,
`get_running`, `get_lab_health`, `validate_job`.

Write (omitted with `--read-only`): `submit_job`, `cancel_job`, `resubmit_job`,
`set_job_priority`.

## Test

```sh
pytest
```

## Roadmap

- Interactive **board sessions**: `lava-mcp` runs as a hosted gateway; a LAVA job
  starts a device-attached container that dials out (`ssh -R`) to the gateway, so an
  agent/human gets shell + qdl/fastboot access to a real board. See the project plan.
