"""Command-line entry point for the lava-mcp server."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Literal, cast

from .config import Config
from .server import build_server


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lava-mcp",
        description="MCP server exposing a LAVA instance to agents.",
    )
    parser.add_argument(
        "--url", help="LAVA base URL (default: $LAVA_URL)", default=None
    )
    parser.add_argument(
        "--token", help="LAVA API token (default: $LAVA_TOKEN)", default=None
    )
    parser.add_argument(
        "--api-version", default=None, help="REST API version (default: v0.3)"
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Disable write tools (submit/cancel/resubmit).",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default=None,
        help="MCP transport (default: stdio, or $LAVA_MCP_TRANSPORT).",
    )
    parser.add_argument("--host", default=None, help="HTTP bind host (hosted mode).")
    parser.add_argument(
        "--port", type=int, default=None, help="HTTP bind port (hosted mode)."
    )
    parser.add_argument(
        "--gateway",
        action="store_true",
        help="Enable the interactive SSH board-session gateway (hosted mode).",
    )
    parser.add_argument(
        "--gateway-port", type=int, default=None, help="SSH gateway listen port."
    )
    parser.add_argument(
        "--gateway-advertise-host",
        default=None,
        help="Host the in-job container should dial back to (default: --host).",
    )
    parser.add_argument(
        "--gateway-allow-ip",
        action="append",
        default=None,
        metavar="IP/CIDR",
        help="Restrict gateway SSH connections to this IP/CIDR (repeatable).",
    )
    parser.add_argument(
        "--http-allow-user",
        action="append",
        default=None,
        metavar="USERNAME",
        help="Restrict the interactive 'use' tools to this LAVA user (repeatable).",
    )
    parser.add_argument(
        "--ssh-allow-user",
        action="append",
        default=None,
        metavar="USERNAME",
        help="Restrict the 'attach' (SSH/console) tools to this LAVA user (repeatable).",
    )
    parser.add_argument(
        "--remote-access-tag",
        default=None,
        metavar="TAG",
        help="Device tag required to host remote-access sessions "
        "(default: allow-remote-access; empty string disables the gate).",
    )
    args = parser.parse_args(argv)

    config = Config.from_env()
    if args.url:
        config.url = args.url
    if args.token:
        config.token = args.token
    if args.api_version:
        config.api_version = args.api_version
    if args.read_only:
        config.read_only = True
    if args.transport:
        config.transport = args.transport
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port
    if args.gateway:
        config.gateway_enabled = True
    if args.gateway_port:
        config.gateway_port = args.gateway_port
    if args.gateway_advertise_host:
        config.gateway_advertise_host = args.gateway_advertise_host
    if args.gateway_allow_ip:
        config.gateway_allow_ips = tuple(args.gateway_allow_ip)
    if args.http_allow_user:
        config.http_allow_users = tuple(args.http_allow_user)
    if args.ssh_allow_user:
        config.ssh_allow_users = tuple(args.ssh_allow_user)
    if args.remote_access_tag is not None:
        config.remote_access_tag = args.remote_access_tag

    if config.transport not in ("stdio", "streamable-http"):
        parser.error(f"unsupported transport: {config.transport}")
    if config.transport == "stdio" and not config.url:
        parser.error(
            "stdio mode needs --url or $LAVA_URL "
            "(HTTP mode takes per-client X-Lava-Url/X-Lava-Token headers)"
        )

    server = build_server(config)
    server.run(transport=cast(Literal["stdio", "streamable-http"], config.transport))
    return 0


if __name__ == "__main__":
    sys.exit(main())
