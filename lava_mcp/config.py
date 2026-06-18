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
    """Connection + behaviour settings for the LAVA REST client."""

    url: str
    token: str | None = None
    api_version: str = "v0.3"
    read_only: bool = False
    timeout: float = 30.0

    @classmethod
    def from_env(cls) -> "Config":
        url = os.environ.get("LAVA_URL")
        if not url:
            raise ValueError("LAVA_URL is required (e.g. https://lava.example.com)")
        return cls(
            url=url,
            token=os.environ.get("LAVA_TOKEN"),
            api_version=os.environ.get("LAVA_API_VERSION", "v0.3"),
            read_only=_env_bool("LAVA_MCP_READ_ONLY"),
            timeout=float(os.environ.get("LAVA_MCP_TIMEOUT", "30")),
        )
