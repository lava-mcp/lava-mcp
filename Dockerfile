# Hostable lava-mcp service (streamable-HTTP transport).
# Build:  docker build -t lava-mcp .
# Run:    docker run -p 8000:8000 \
#           -e LAVA_URL=https://lava.example.com -e LAVA_TOKEN=... \
#           -e LAVA_MCP_TRANSPORT=streamable-http -e LAVA_MCP_HOST=0.0.0.0 \
#           lava-mcp
FROM python:3.13-slim

# git + ca-certificates: read_lava_docs mirrors the deployed LAVA source with git.
# tini: a proper init as PID 1 to reap zombies. git spawns helper processes
# (git-remote-https, pack/index) that can be orphaned onto PID 1; without an init
# reaping them, they accumulate as `git <defunct>` on every mirror poll.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY lava_mcp ./lava_mcp
RUN pip install --no-cache-dir .

ENV LAVA_MCP_TRANSPORT=streamable-http \
    LAVA_MCP_HOST=0.0.0.0 \
    LAVA_MCP_PORT=8000
EXPOSE 8000

# tini as PID 1 forwards signals and reaps orphaned git helper processes.
ENTRYPOINT ["tini", "--", "lava-mcp"]
