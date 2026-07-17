# ser2net console proxy image for the lava-mcp serial-console feature (Mode 2).
#
# Runs as a LAVA Test Services container on the worker: it relays the board's ser2net
# console (read-only until console-ready) and, when a gateway session is configured,
# dials OUT (ssh -R) to the lava-mcp gateway so the console surfaces on the master.
# ssh/autossh are baked in so nothing is installed at runtime (lab networks may block
# egress). Published to ghcr.io/<owner>/lava-mcp/ser2net-proxy.
FROM python:3.12-alpine

RUN apk add --no-cache openssh-client autossh curl

# websocat: ssh ProxyCommand that tunnels the console dial-out over wss:// (443,
# via Caddy). Fetch the static musl build at build time (lab blocks runtime egress,
# not the build). The static binary runs on alpine as-is. Bump WEBSOCAT_VERSION if
# the pinned release moves.
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

COPY console-proxy.py /console-proxy.py
COPY console-connect.sh /console-connect.sh
RUN chmod +x /console-connect.sh

ENTRYPOINT ["/console-connect.sh"]
