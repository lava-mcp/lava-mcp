"""Configuration for the lava-mcp server."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    """Connection + behaviour settings for the LAVA REST client and server."""

    url: str
    token: str | None = None
    api_version: str = "v0.3"
    read_only: bool = False
    timeout: float = 30.0
    # serving (hostable mode)
    transport: str = "stdio"  # "stdio" | "streamable-http"
    host: str = "127.0.0.1"
    port: int = 8000
    # interactive SSH gateway (only used in hosted mode)
    gateway_enabled: bool = False
    gateway_bind: str = "0.0.0.0"
    gateway_port: int = 2222
    # host/port the in-job container should dial back to (advertised in jobs)
    gateway_advertise_host: str | None = None
    gateway_advertise_port: int | None = None
    # interactive session assets (container image + test definition location).
    # These MUST be set to wherever you host the lava-mcp image/repo.
    interactive_image: str = "registry.example.com/lava-mcp-interactive:latest"
    interactive_repo: str = "https://git.example.com/lava-mcp.git"
    interactive_path: str = "interactive/ssh-gateway.yaml"

    @classmethod
    def from_env(cls) -> "Config":
        url = os.environ.get("LAVA_URL")
        if not url:
            raise ValueError("LAVA_URL is required (e.g. https://lava.example.com)")
        gw_port = int(os.environ.get("LAVA_MCP_GATEWAY_PORT", "2222"))
        adv_port = os.environ.get("LAVA_MCP_GATEWAY_ADVERTISE_PORT")
        return cls(
            url=url,
            token=os.environ.get("LAVA_TOKEN"),
            api_version=os.environ.get("LAVA_API_VERSION", "v0.3"),
            read_only=_env_bool("LAVA_MCP_READ_ONLY"),
            timeout=float(os.environ.get("LAVA_MCP_TIMEOUT", "30")),
            transport=os.environ.get("LAVA_MCP_TRANSPORT", "stdio"),
            host=os.environ.get("LAVA_MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("LAVA_MCP_PORT", "8000")),
            gateway_enabled=_env_bool("LAVA_MCP_GATEWAY_ENABLED"),
            gateway_bind=os.environ.get("LAVA_MCP_GATEWAY_BIND", "0.0.0.0"),
            gateway_port=gw_port,
            gateway_advertise_host=os.environ.get("LAVA_MCP_GATEWAY_ADVERTISE_HOST"),
            gateway_advertise_port=int(adv_port) if adv_port else None,
            interactive_image=os.environ.get(
                "LAVA_MCP_INTERACTIVE_IMAGE",
                "registry.example.com/lava-mcp-interactive:latest",
            ),
            interactive_repo=os.environ.get(
                "LAVA_MCP_INTERACTIVE_REPO",
                "https://git.example.com/lava-mcp.git",
            ),
            interactive_path=os.environ.get(
                "LAVA_MCP_INTERACTIVE_PATH", "interactive/ssh-gateway.yaml"
            ),
        )
