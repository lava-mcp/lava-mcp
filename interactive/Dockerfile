# Container image for lava-mcp interactive board sessions.
#
# A LAVA docker test action runs this image with the target board's USB/serial
# mapped in. The `lava-gateway-connect` script (run by the ssh-gateway.yaml test
# definition) starts sshd and opens a reverse-SSH tunnel out to the lava-mcp
# gateway, so the gateway can run commands against the board (qdl/fastboot/adb/sh).
#
# Build & publish (override LAVA_MCP_INTERACTIVE_IMAGE if you host it elsewhere):
#   docker build -t ghcr.io/lava-mcp/lava-mcp/interactive:latest interactive/
#   docker push  ghcr.io/lava-mcp/lava-mcp/interactive:latest
#
# Debian base on purpose: a session can `apt-get install` or build any extra
# tooling it needs at runtime (e.g. a newer/custom flashing tool from source).
# The pre-installed set below just covers the common cases.
FROM debian:trixie-slim

# python3 + python3-websockets back the ssh ProxyCommand (lava-ws-proxy) that tunnels
# the reverse-SSH dial-out over wss:// (443, via Caddy). Both are packaged in Debian —
# no fetching an unpackaged binary at build time (this replaces websocat).
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
        git \
        python3 \
        python3-websockets \
        tar \
        iproute2 \
    && rm -rf /var/lib/apt/lists/*

# Session foreground supervisor + the device-command/signal relay clients, plus the
# WebSocket ProxyCommand (lava-ws-proxy). The supervisor manages the reverse tunnel and
# lets agent ssh sessions run LAVA device commands (power_on/off, hard_reset, ...) and
# emit signals via the dispatcher.
COPY lava-gateway-connect lava-session-supervisor lava-device-command lava-signal \
     lava-power-on lava-power-off lava-hard-reset lava-ws-proxy /usr/local/bin/
RUN chmod +x /usr/local/bin/lava-gateway-connect \
        /usr/local/bin/lava-session-supervisor \
        /usr/local/bin/lava-device-command \
        /usr/local/bin/lava-signal \
        /usr/local/bin/lava-power-on \
        /usr/local/bin/lava-power-off \
        /usr/local/bin/lava-hard-reset \
        /usr/local/bin/lava-ws-proxy
