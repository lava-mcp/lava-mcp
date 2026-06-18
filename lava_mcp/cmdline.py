"""Command-line entry point for the lava-mcp server."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .client import LavaClient
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
        help="Disable write tools (submit/cancel/resubmit/priority).",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="MCP transport (default: stdio).",
    )
    args = parser.parse_args(argv)

    try:
        config = Config.from_env()
    except ValueError:
        if not args.url:
            parser.error("LAVA URL required: pass --url or set $LAVA_URL")
        config = Config(url=args.url)

    if args.url:
        config.url = args.url
    if args.token:
        config.token = args.token
    if args.api_version:
        config.api_version = args.api_version
    if args.read_only:
        config.read_only = True

    client = LavaClient(config)
    server = build_server(client)
    server.run(transport=args.transport)
    return 0


if __name__ == "__main__":
    sys.exit(main())
