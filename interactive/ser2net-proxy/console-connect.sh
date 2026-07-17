#!/bin/sh
# Entry point for the ser2net-proxy Test Services container.
#
#   1. start the gated console relay (console-proxy.py) on CONSOLE_LISTEN_PORT;
#   2. if a gateway session is configured, dial OUT (ssh -R) to the lava-mcp gateway so
#      the console surfaces on a master-local port; otherwise run relay-only (watch via
#      docker logs).
#
# Config comes from the job/device environment (compose .env). The session private key
# arrives base64-encoded (SESSION_PRIVATE_KEY_B64) because a .env cannot hold the
# multi-line PEM. Values come from lava-mcp's open_console_session tool.
set -eu

CONSOLE_LISTEN_PORT="${CONSOLE_LISTEN_PORT:-2323}"
export CONSOLE_LISTEN_PORT

# gated console relay in the background (listens on CONSOLE_LISTEN_PORT)
python3 /console-proxy.py &

if [ -z "${GATEWAY_HOST:-}" ] || [ -z "${SESSION_ID:-}" ] \
   || [ -z "${REVERSE_PORT:-}" ] || [ -z "${SESSION_PRIVATE_KEY_B64:-}" ]; then
  echo "ser2net-proxy: no gateway session configured — relay only (no dial-out)"
  wait
fi

mkdir -p /root/.ssh && chmod 700 /root/.ssh
printf '%s' "$SESSION_PRIVATE_KEY_B64" | base64 -d > /root/.ssh/session_key
chmod 600 /root/.ssh/session_key

echo "ser2net-proxy: dialing gateway ${GATEWAY_HOST}:${GATEWAY_PORT:-2222} as" \
     "${SESSION_ID} (reverse ${REVERSE_PORT} -> localhost:${CONSOLE_LISTEN_PORT})"

# autossh keeps the reverse tunnel up; -R exposes the relay on the gateway's REVERSE_PORT.
# AUTOSSH_GATETIME=0 keeps it reconnecting if ssh exits quickly; a bounded outer loop
# retries a few times if autossh itself exits. The console relay above keeps running
# regardless, so local `docker logs` watching still works even if the dial-out is down.
export AUTOSSH_GATETIME=0
attempt=0
max_attempts="${GATEWAY_CONNECT_ATTEMPTS:-10}"
while [ "$attempt" -lt "$max_attempts" ]; do
  attempt=$((attempt + 1))
  echo "ser2net-proxy: gateway connect attempt ${attempt}/${max_attempts}"
  autossh -M 0 -N \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -o ConnectTimeout=15 \
    -o ExitOnForwardFailure=yes \
    -i /root/.ssh/session_key \
    -R "127.0.0.1:${REVERSE_PORT}:localhost:${CONSOLE_LISTEN_PORT}" \
    -p "${GATEWAY_PORT:-2222}" \
    "${SESSION_ID}@${GATEWAY_HOST}"
  echo "ser2net-proxy: gateway connection exited (attempt ${attempt}), retrying in 5s"
  sleep 5
done
echo "ser2net-proxy: giving up after ${max_attempts} gateway connect attempts" >&2
# keep the relay-only container alive so the console can still be watched via docker logs
wait
