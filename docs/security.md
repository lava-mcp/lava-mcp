# Security model — interactive gateway

The gateway deliberately builds tunnels **out of** an isolated lab network so agents and
humans can reach boards. That makes it a bridge into the lab, so its controls matter.
This documents the trust boundaries, what is enforced, and what an operator must do.

## Topology / trust boundary

- **lava-master** runs lava-mcp (HTTP `/mcp` behind Caddy on 443) and the SSH gateway on
  **2222**. The lab can reach the master; the master **cannot** reach into the lab.
- **Lab** runs the workers, boards, and the ser2net console server. In-job containers
  dial **out** to the gateway (`ssh -R`); nothing inbound to the lab is required.
- The tunnel is the only path from master-side to a lab service, so every control below
  exists to ensure only an authorised party can traverse it, and only to the one service
  a session exposes.

## Roles and what each may do

Authentication is **public-key only** (no passwords). Username = session id; the key
decides the role:

| Role | Key | May | May not |
|---|---|---|---|
| **agent** (in-job container / console proxy) | the session key, baked into the job | reverse-forward **its own** `reverse_port`, **loopback bind only** | open a shell, open `-L`/direct-tcpip, forward any other port/host |
| **human** | a short-lived key from `attach_*` | `-W`/direct-tcpip to **its own** `127.0.0.1:reverse_port` only | reverse-forward, open a shell, reach any other host/port |

The gateway itself offers **no shell/exec/sftp and no UNIX-socket forwarding** to anyone
(`session_requested`, `unix_*` all denied). It is a pure rendezvous.

The interactive **container shell** (`attach_shell`) uses this same human `-W`/direct-
tcpip: lava-mcp authorises the human's ephemeral key inside the board container and the
human `ssh -J`'s through the gateway into the **container's own sshd** for the PTY. The
gateway never bridges a shell itself; the container is ephemeral (destroyed with the
job). The **serial console** (`attach_console`) is the same forward to the console relay.

## Enforced controls (and where)

- **Loopback-only reverse tunnel** — `_GatewaySSHServer.server_requested` refuses any
  non-loopback bind. This is the critical one: asyncssh binds the reverse listener to the
  client-requested host, and an empty/`0.0.0.0` request would expose the tunnelled lab
  service on the master with **no SSH auth and no IP allowlist**. Loopback bind means
  `reverse_port` is reachable only from the master, i.e. only via an authenticated human
  `-W`. (The connect scripts also request `127.0.0.1:` explicitly.)
- **Source-IP allowlist** — `LAVA_MCP_GATEWAY_ALLOW_IPS` drops connections before auth,
  for agents and humans alike. Empty = open (a startup warning is logged); set it to the
  lab egress plus any human/VPN ranges.
- **Per-session key auth** — only the session's agent key or a live human key for that
  session authenticates; unknown keys are rejected.
- **Ephemeral, expiring human keys** — `attach_*` mints a per-session key valid for
  `LAVA_MCP_GATEWAY_HUMAN_KEY_TTL` seconds (default 3600); expired keys are refused, and
  `close_*` revokes all of a session's keys. The board/console key is never disclosed to
  humans.
- **Per-user session ownership** — a session records the LAVA user who opened it
  (discovered via `whoami`); only the owner may `run`/`attach`/`close` it or see it in
  `list`. Prevents one allowlisted user pivoting into another's board/console.
- **Device gates** — interactive sessions only run on devices tagged
  `allow-remote-access`; the serial console additionally requires `allow_test_services`
  in the device dict.

These are covered by unit tests plus a live-asyncssh integration test
(`tests/test_gateway.py`) that confirms the agent key can reverse-forward its loopback
port, cannot get a shell on the gateway, and that a stranger key is refused.

## Operator responsibilities

- **Set `LAVA_MCP_GATEWAY_ALLOW_IPS`** to the lab egress IP (and human/VPN ranges). This
  is defence-in-depth on top of key auth; do not leave it open in production.
- **Open only port 2222 inbound** on the master, restricted to those source ranges. 443
  is already open for LAVA. Never expose the ephemeral `reverse_port` range — it is
  loopback-only by design and must stay that way.
- **Treat the session (agent) key as a job secret.** It is embedded in the LAVA job; it
  only permits a loopback reverse-forward of one port, but rotate/close sessions promptly.

## Residual risks (accepted / by design)

- A LAVA user who can submit jobs can already run containers on a worker (Test Services)
  and reach lab hosts from them; the gateway extends that reach to an authenticated,
  allowlisted human. This is the intended capability, bounded by the controls above.
- `run_in_session` runs arbitrary commands in the board container by design — that is the
  interactive-session feature, scoped to the session owner.
