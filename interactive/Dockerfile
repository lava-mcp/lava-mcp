# Container image for lava-mcp interactive board sessions.
#
# A LAVA docker test action runs this image with the target board's USB/serial
# mapped in. The `lava-gateway-connect` script (run by the ssh-gateway.yaml test
# definition) starts sshd and opens a reverse-SSH tunnel out to the lava-mcp
# gateway, so the gateway can run commands against the board (qdl/fastboot/adb/sh).
#
# Build & publish (override LAVA_MCP_INTERACTIVE_IMAGE if you host it elsewhere):
#   docker build -t ghcr.io/mattface/lava-mcp/interactive:latest interactive/
#   docker push  ghcr.io/mattface/lava-mcp/interactive:latest
FROM debian:trixie-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-server \
        autossh \
        android-tools-adb \
        android-tools-fastboot \
        qdl \
        usbutils \
        picocom \
        ca-certificates \
        tar \
        iproute2 \
    && rm -rf /var/lib/apt/lists/*

COPY lava-gateway-connect /usr/local/bin/lava-gateway-connect
RUN chmod +x /usr/local/bin/lava-gateway-connect
