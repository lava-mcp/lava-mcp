# ser2net console proxy image for the lava-mcp serial-console feature (Mode 2).
#
# Runs as a LAVA Test Services container on the worker: it relays the board's ser2net
# console (read-only until console-ready) and, when a gateway session is configured,
# dials OUT (ssh -R) to the lava-mcp gateway so the console surfaces on the master.
# ssh/autossh are baked in so nothing is installed at runtime (lab networks may block
# egress). Published to ghcr.io/<owner>/lava-mcp/ser2net-proxy.
FROM python:3.12-alpine

RUN apk add --no-cache openssh-client autossh

# websockets backs the ssh ProxyCommand (lava-ws-proxy) that tunnels the console
# dial-out over wss:// (443, via Caddy). Installed with the image's own pip at build
# time (lab blocks runtime egress, not the build) — this replaces websocat.
RUN pip install --no-cache-dir websockets

COPY console-proxy.py /console-proxy.py
COPY console-connect.sh /console-connect.sh
COPY lava-ws-proxy /usr/local/bin/lava-ws-proxy
RUN chmod +x /console-connect.sh /usr/local/bin/lava-ws-proxy

ENTRYPOINT ["/console-connect.sh"]
