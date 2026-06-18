# Hostable lava-mcp service (streamable-HTTP transport).
# Build:  docker build -t lava-mcp .
# Run:    docker run -p 8000:8000 \
#           -e LAVA_URL=https://lava.example.com -e LAVA_TOKEN=... \
#           -e LAVA_MCP_TRANSPORT=streamable-http -e LAVA_MCP_HOST=0.0.0.0 \
#           lava-mcp
FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY lava_mcp ./lava_mcp
RUN pip install --no-cache-dir .

ENV LAVA_MCP_TRANSPORT=streamable-http \
    LAVA_MCP_HOST=0.0.0.0 \
    LAVA_MCP_PORT=8000
EXPOSE 8000

ENTRYPOINT ["lava-mcp"]
