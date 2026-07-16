"""Configuration for the lava-mcp server."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str) -> tuple[str, ...]:
    """Parse a comma/space-separated env var into a tuple (empty if unset)."""
    value = os.environ.get(name)
    if not value:
        return ()
    return tuple(item for item in value.replace(",", " ").split() if item)


@dataclass
class Config:
    """Connection + behaviour settings for the LAVA REST client and server.

    ``url`` normally pins the server to the LAVA instance it fronts (set
    ``LAVA_URL`` per deployment); it is then authoritative and connecting clients
    only send their own ``X-Lava-Token`` to act as their own LAVA user. Left
    empty, the server is fully multi-tenant and clients supply the target via an
    ``X-Lava-Url`` header. ``token`` is a fallback used for local stdio.
    """

    url: str = ""
    token: str | None = None
    api_version: str = "v0.3"
    read_only: bool = False
    timeout: float = 30.0
    # serving (hostable mode)
    transport: str = "stdio"  # "stdio" | "streamable-http"
    host: str = "127.0.0.1"
    port: int = 8000
    # HTTP transport: plain-JSON responses are the most proxy- and client-friendly
    # for a hosted service. Keep sessions stateful so the gateway lifespan (the SSH
    # listener) starts once, not per request.
    json_response: bool = True
    stateless_http: bool = False
    # interactive SSH gateway (only used in hosted mode)
    gateway_enabled: bool = False
    gateway_bind: str = "0.0.0.0"
    gateway_port: int = 2222
    # host/port the in-job container should dial back to (advertised in jobs)
    gateway_advertise_host: str | None = None
    gateway_advertise_port: int | None = None
    # optional gateway access control (both empty = open):
    #  - allow_ips: source IPs/CIDRs permitted to connect to the SSH gateway
    #  - allow_users: LAVA usernames (via whoami) permitted to open board sessions
    gateway_allow_ips: tuple[str, ...] = ()
    gateway_allow_users: tuple[str, ...] = ()
    # LAVA device tag a device must carry to host interactive/remote-access jobs.
    # open_board_session requires it and pins the job to it. Empty disables the gate.
    remote_access_tag: str = "allow-remote-access"
    # how long an ephemeral human access key (from attach_*) stays valid, in seconds
    gateway_human_key_ttl: float = 3600.0
    # interactive session assets (container image + test definition location).
    # Override via LAVA_MCP_INTERACTIVE_* if you host the image/repo elsewhere.
    interactive_image: str = "ghcr.io/mattface/lava-mcp/interactive:latest"
    interactive_repo: str = "https://github.com/mattface/lava-mcp.git"
    interactive_path: str = "interactive/ssh-gateway.yaml"

    @classmethod
    def from_env(cls) -> "Config":
        # url/token are optional: in hosted mode clients supply them via headers.
        url = os.environ.get("LAVA_URL", "")
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
            json_response=_env_bool("LAVA_MCP_JSON_RESPONSE", True),
            stateless_http=_env_bool("LAVA_MCP_STATELESS", False),
            gateway_enabled=_env_bool("LAVA_MCP_GATEWAY_ENABLED"),
            gateway_bind=os.environ.get("LAVA_MCP_GATEWAY_BIND", "0.0.0.0"),
            gateway_port=gw_port,
            gateway_advertise_host=os.environ.get("LAVA_MCP_GATEWAY_ADVERTISE_HOST"),
            gateway_advertise_port=int(adv_port) if adv_port else None,
            gateway_allow_ips=_env_list("LAVA_MCP_GATEWAY_ALLOW_IPS"),
            gateway_allow_users=_env_list("LAVA_MCP_GATEWAY_ALLOW_USERS"),
            remote_access_tag=os.environ.get(
                "LAVA_MCP_REMOTE_ACCESS_TAG", "allow-remote-access"
            ),
            gateway_human_key_ttl=float(
                os.environ.get("LAVA_MCP_GATEWAY_HUMAN_KEY_TTL", "3600")
            ),
            interactive_image=os.environ.get(
                "LAVA_MCP_INTERACTIVE_IMAGE",
                "ghcr.io/mattface/lava-mcp/interactive:latest",
            ),
            interactive_repo=os.environ.get(
                "LAVA_MCP_INTERACTIVE_REPO",
                "https://github.com/mattface/lava-mcp.git",
            ),
            interactive_path=os.environ.get(
                "LAVA_MCP_INTERACTIVE_PATH", "interactive/ssh-gateway.yaml"
            ),
        )
