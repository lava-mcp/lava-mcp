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
        curl \
        tar \
        iproute2 \
    && rm -rf /var/lib/apt/lists/*

# websocat: ssh ProxyCommand that tunnels the reverse-SSH dial-out over wss://
# (443, via Caddy). Debian has no package, so fetch the static musl build at
# image-build time (the lab blocks runtime egress, not the build). Bump
# WEBSOCAT_VERSION if the pinned release moves.
ARG WEBSOCAT_VERSION=1.13.0
ARG TARGETARCH=amd64
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) wsarch=x86_64 ;; \
        arm64) wsarch=aarch64 ;; \
        *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/websocat \
        "https://github.com/vi/websocat/releases/download/v${WEBSOCAT_VERSION}/websocat.${wsarch}-unknown-linux-musl"; \
    chmod +x /usr/local/bin/websocat; \
    websocat --version

COPY lava-gateway-connect /usr/local/bin/lava-gateway-connect
RUN chmod +x /usr/local/bin/lava-gateway-connect
