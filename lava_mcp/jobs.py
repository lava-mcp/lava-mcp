"""Build the LAVA job that opens an interactive board session.

The job grabs a board of the requested type and, in a docker test action, runs the
interactive container image. That container dials back to this MCP server's SSH
gateway (`ssh -R`) using the per-session key, so the gateway can run commands in it.

The container image + test definition live in this repo under ``interactive/``
(image published to GHCR, test def fetched from this repo by the lab worker); their
parameter contract is the keys set in ``parameters`` below. Image / definition repo /
path are configurable so this stays decoupled from a specific deployment.
"""

from __future__ import annotations

import base64
from typing import Any

import yaml

from .config import Config
from .gateway import BoardSession


def build_interactive_job(
    config: Config,
    session: BoardSession,
    device_type: str,
    tags: list[str] | None = None,
    image: str | None = None,
    timeout_minutes: int = 60,
    console_session: BoardSession | None = None,
) -> str:
    """Return the YAML job definition for an interactive session on ``device_type``.

    When ``console_session`` is given, the job also runs the ser2net-proxy as a Test
    Services container so the board's serial console is reachable (via attach_console)
    alongside the board session. The ser2net port is per-board and only known once LAVA
    schedules the job, so it is NOT baked in here — the board container reports its
    LAVA_CONNECTION_COMMAND at runtime and the gateway pushes the endpoint to the proxy
    (see gateway.set_console_target). The proxy is writable from the start (no boot to
    gate on).
    """
    gateway_host = config.gateway_advertise_host or config.host

    parameters = {
        # GATEWAY_HOST is only the ssh user@host label; the container tunnels its
        # ssh -R over GATEWAY_WS_URL (wss://, 443, via Caddy) using websocat.
        "GATEWAY_HOST": gateway_host,
        "GATEWAY_WS_URL": config.gateway_ws_url,
        "SESSION_ID": session.session_id,
        "REVERSE_PORT": str(session.reverse_port),
        "SESSION_PRIVATE_KEY": session.private_key,
        "SESSION_PUBLIC_KEY": session.public_key,
    }

    # Pin the job to devices tagged for remote access, so LAVA will only ever
    # schedule an interactive session on a device an admin has opted in.
    job_tags = list(tags or [])
    if config.remote_access_tag and config.remote_access_tag not in job_tags:
        job_tags.append(config.remote_access_tag)

    board_action = {
        "test": {
            "timeout": {"minutes": timeout_minutes},
            "docker": {"image": image or config.interactive_image},
            # permit the session to run the device's LAVA commands (power_on/off,
            # hard_reset, recovery_*, user_commands) on the worker via the DEVICECMD
            # relay — e.g. power-cycling the DUT for flashing/EDL.
            "device_commands": True,
            "definitions": [
                {
                    "repository": config.interactive_repo,
                    "from": "git",
                    "path": config.interactive_path,
                    "name": "interactive-ssh-gateway",
                    "parameters": parameters,
                }
            ],
        }
    }
    actions: list[dict[str, Any]] = [board_action]

    job: dict[str, Any] = {
        "device_type": device_type,
        "job_name": f"lava-mcp interactive {session.session_id}",
        "visibility": "personal",
        "timeouts": {
            "job": {"minutes": timeout_minutes},
            "action": {"minutes": timeout_minutes},
            "connection": {"minutes": 5},
        },
        "priority": "medium",
    }

    if console_session is not None and config.gateway_ws_url:
        # LAVA forbids the reserved 'common' namespace (the default for an unnamespaced
        # action) alongside any named namespace. The console proxy uses 'console', so
        # the board action must be named too — otherwise the job fails validation with
        # "'common' is a reserved namespace that should not be present with other
        # namespaces". (A plain board session keeps the implicit 'common'.)
        board_action["test"]["namespace"] = "board"
        # start the console proxy first, so it is watching from the start of the job
        actions.insert(
            0,
            {
                "test": {
                    "namespace": "console",
                    "timeout": {"minutes": timeout_minutes},
                    "services": [
                        {
                            "name": "ser2net-proxy",
                            "from": "git",
                            "repository": config.interactive_repo,
                            "path": "interactive/ser2net-proxy/docker-compose.yml",
                        }
                    ],
                }
            },
        )
        key_b64 = base64.b64encode(console_session.private_key.encode()).decode()
        # LAVA writes this top-level environment into the proxy's compose .env. The
        # ser2net host/port are deliberately absent — they are per-board and unknown at
        # submit time, so the gateway pushes them to the proxy at runtime. The proxy
        # uses these console-session values to dial out and expose the console.
        job["environment"] = {
            "SER2NET_NETWORK": "lava-dispatcher_default",
            "CONSOLE_READY_SENTINEL": "",  # writable from start (no boot to gate on)
            "GATEWAY_HOST": gateway_host,
            "GATEWAY_WS_URL": config.gateway_ws_url,
            "SESSION_ID": console_session.session_id,
            "REVERSE_PORT": str(console_session.reverse_port),
            "SESSION_PRIVATE_KEY_B64": key_b64,
        }

    job["actions"] = actions
    if job_tags:
        job["tags"] = job_tags
    return yaml.safe_dump(job, sort_keys=False)
