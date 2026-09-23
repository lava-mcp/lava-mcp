# lava-mcp

An [MCP](https://modelcontextprotocol.io) server that exposes a
[LAVA](https://www.lavasoftware.org/) instance to agents (and humans) — letting them
query the board farm, submit and manage test jobs, open interactive sessions to a board,
reach a board's serial console, serve build artifacts to jobs, and read the deployed
LAVA's own docs and source.

It is a thin client over LAVA's REST API (v0.2/v0.3); point it at any LAVA instance.

## Quick start

```sh
pip install -e .[dev]

export LAVA_URL=https://lava.example.com
export LAVA_TOKEN=<your-api-token>
lava-mcp                                  # stdio, for a local MCP client
```

For a hosted HTTPS deployment (required by the interactive, console and artifact
features) and per-request token handling, see **[docs/deployment.md](docs/deployment.md)**.

## Features

Each feature is documented on its own page:

- **[LAVA jobs & inventory](docs/lava-jobs.md)** — the core proxy: query devices/jobs
  and run the standard deploy/boot/test job. Includes building jobs from templates,
  the "jobs are independent" rules, and searching jobs by metadata. *Start here.*
- **[Interactive board sessions](docs/board-sessions.md)** — a shell in a container
  *next to* the board (flashing, `fastboot`/`adb`/`qdl`, bring-up) over an SSH gateway;
  `run_in_session`, `attach_shell`, and the device-command power/recovery relay.
- **[Serial console](docs/serial-console.md)** — the board's own UART via a ser2net
  proxy: boot/kernel logs and a live console, with or without a deploy+boot job.
- **[Debug-board (TAC) control](docs/tac-control.md)** — drive the board's debug board
  through the lab's pytactl REST service: quick methods (`powerOn`, `bootToEDL`, ...)
  and individual pins, e.g. holding the power key to reset a frozen board.
- **[Artifact store](docs/artifact-store.md)** — upload a build product the server hosts
  temporarily; LAVA (or a booted device / container) fetches it back, with the secret
  kept out of the job via LAVA remote-artifact tokens.
- **[LAVA docs & source mirror](docs/lava-source-docs.md)** — `read_lava_docs` serves
  the deployed LAVA's documentation and source from a local git mirror at the exact
  deployed ref.

Supporting docs:

- **[Deployment & credentials](docs/deployment.md)** — install, token handling, stdio vs
  hosted, docker compose + Caddy.
- **[Configuration reference](docs/configuration.md)** — every environment variable and
  CLI flag.
- **[Security model](docs/security.md)** — gateway trust model, enforced controls, and
  operator responsibilities.
- **[Roadmap](docs/roadmap.md)** — design notes and future work.

## Tools

Read/observe: `whoami`, `version`, `list_devices`, `get_device`,
`get_device_dictionary`, `get_qdl_info`, `list_device_types`, `list_workers`,
`list_jobs`, `get_job`, `get_job_definition`, `get_job_logs`, `get_job_results`,
`get_queue`, `get_running`, `get_lab_health`, `find_boot_template`, `validate_job`,
`list_remote_artifact_tokens`.

Write (omitted with `--read-only`): `submit_job`, `cancel_job`, `resubmit_job`.

Interactive board sessions (hosted gateway mode): `open_board_session`, `run_in_session`,
`attach_shell`, `run_device_command`, `close_board_session`, `list_board_sessions`.

Serial console (hosted gateway mode): `check_serial_console_support`,
`open_console_session`, `check_console_ready`, `attach_console`, `close_console_session`.

Debug-board control (hosted gateway mode, via the console proxy): `tac_info`,
`tac_command`.

Artifact store (hosted mode, when enabled): `create_artifact_upload`, `list_artifacts`,
`delete_artifact`.

LAVA docs & source (when `LAVA_SOURCE_REPO` is set): `read_lava_docs`.

## Test

```sh
pytest
```

## License

MIT — see [LICENSE](LICENSE).
