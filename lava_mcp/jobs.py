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
) -> str:
    """Return the YAML job definition for an interactive session on ``device_type``."""
    gateway_host = config.gateway_advertise_host or config.host
    gateway_port = config.gateway_advertise_port or config.gateway_port

    parameters = {
        "GATEWAY_HOST": gateway_host,
        "GATEWAY_PORT": str(gateway_port),
        # When set, the container tunnels its ssh -R over this wss:// URL (443, via
        # Caddy) instead of dialling GATEWAY_PORT directly.
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
        "actions": [
            {
                "test": {
                    "timeout": {"minutes": timeout_minutes},
                    "docker": {"image": image or config.interactive_image},
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
        ],
    }
    if job_tags:
        job["tags"] = job_tags
    return yaml.safe_dump(job, sort_keys=False)
