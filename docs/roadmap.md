# Implementation plan: unbuilt interactive features

The README's *Interactive board sessions* section carries the user-facing **design**
for the pieces below. This doc is the **engineering plan** for building them — files,
functions, config, tests, and the open questions to resolve first.

Status legend: 🟢 shipped · 🟡 planned (design agreed) · 🔵 proposed (not yet agreed).

Shipped foundation (for reference): the SSH rendezvous gateway, `open_board_session` /
`run_in_session` / `close_board_session` / `list_board_sessions`, the gateway
access-control allowlists (`LAVA_MCP_GATEWAY_ALLOW_IPS`, `LAVA_MCP_GATEWAY_ALLOW_USERS`),
and the per-device `allow-remote-access` **tag** gate (`LAVA_MCP_REMOTE_ACCESS_TAG`):
`open_board_session` fails fast if no device of the type carries the tag, and every
interactive job is pinned to it so LAVA only schedules on a permitted device.

---

## A. Human interactive SSH shell (gateway as bastion) 🟡

**Goal.** `ssh -p 2222 <session_id>@<gateway>` drops a human into a live PTY on the
board's container, without the human ever holding the container's private key.

**Current gap.** `_GatewaySSHServer` only handles the container's reverse-forward
(`server_requested`) and authenticates one key (the container's session key). There is
no session/shell channel handling, no human keys, and no `attach_session` tool.

**Design (bastion proxy).**
1. `BoardSession` gains `human_keys: dict[str, str]` (key id → public key) and an
   optional `human_key_expires: dict[str, float]` for TTLs.
2. New tool `attach_session(session_id)` (gated by the user allowlist **and** a new
   `gateway_human_enabled` flag): mint an ephemeral ed25519 keypair, authorize its
   public key on the session, return `{private_key, ssh_command, expires_at}` over the
   already-authenticated MCP/HTTPS channel.
3. `_GatewaySSHServer.validate_public_key` accepts **either** the container's session
   key or an authorized human key, recording which authenticated (`self._role`).
4. Capability split by role:
   - container key → may `server_requested` (reverse forward), may **not** open a shell;
   - human key → may open a session/shell channel, may **not** reverse-forward.
5. Shell handler (the hard part): on a human PTY/shell request, open an inner
   `asyncssh.connect("127.0.0.1", reverse_port, ...)` with the container private key
   (reuse `Gateway._run`'s connect logic), request a PTY + shell inner-side, and pump
   stdin/stdout/stderr **and** window-size changes bidirectionally between the human
   channel and the inner shell. Prefer asyncssh `process_factory` + `create_process` on
   the inner connection with manual duplex copy.

**Files.** `gateway.py` (session fields, role tracking, shell bridge), `server.py`
(`attach_session`, revoke on close), `config.py` + `cmdline.py` (`gateway_human_enabled`
→ `LAVA_MCP_GATEWAY_HUMAN_ENABLED` / `--gateway-human`), README config table, tests.

**Lifecycle.** `close_board_session` clears the session's human keys; expired human keys
are rejected at auth.

**✅ Decided — IP allowlist applies to humans too (security).**
`LAVA_MCP_GATEWAY_ALLOW_IPS` drops *all* non-listed connections at `connection_made`,
before auth — containers **and** humans. Human SSH access is therefore *defence in
depth*: a valid ephemeral per-session key **and** a source IP inside the allowlist.
The connection-level check stays where it is (do **not** narrow it to the
reverse-forward role). Operators who want remote humans must add those source ranges
(e.g. the VPN/office CIDR) to `LAVA_MCP_GATEWAY_ALLOW_IPS`; an empty allowlist stays
fully open. No code change from today's behaviour — this is a locked constraint for
feature A.

**Tests.** human key authenticates while container key is refused a shell (and vice
versa); `attach_session` adds/returns a key and `close_board_session` revokes it; TTL
expiry rejects.

**Effort.** Medium–high — the asyncssh PTY bridge is the main risk.

---

## B. Direct serial console (LAVA-deploy-and-boot jobs) 🟡

**Two interactive modes.** These are different LAVA job styles and must not be conflated:
- **Mode 1 (bring-up / flashing) — built.** A device-attached *docker test action*
  container dials out to the gateway; used for `qdl`/`fastboot`/`adb` when the board may
  have no OS. Covered by `open_board_session` + the SSH gateway.
- **Mode 2 (deploy + boot, test-on-board) — this feature.** LAVA deploys and boots an
  image and the test action runs *on the board itself* — there is **no** device-attached
  container. The serial console lets a human watch/interact with the booted system's UART.

**Goal.** After the job flags it has booted to a shell, a human reaches the board's raw
serial console (boot/kernel/panic logs, login prompt) — no DUT networking required.

**Topology constraint (decisive).** The MCP server runs on the **lava-master**, which is
**not** on the workers' lab network: lab → master is allowed, master → lab is not. So the
master can never open `telnet ser2net …` into the lab. Anything touching ser2net must
originate **inside the lab and dial out**. In Mode 2 there is no per-job container to
piggyback on — so the lab-side foothold comes from LAVA's **Test Services** feature.

**LAVA Test Services** (`services:` under a `test` action; impl
`lava_dispatcher/actions/test/service.py`, schema `lava_common/schemas/test/service.py`):
a docker-compose project LAVA runs **on the worker** (in the lab), up for the job's
lifetime (torn down at job end or via the `stop_test_services` command). Networking is
whatever the compose file declares (`network_mode: host` → reaches lab hosts incl.
ser2net; outbound works for `ssh -R`); device/job `environment` is written to the
compose `.env`. **Gated per device by `allow_test_services: true` in the device dict.**

**Design.**
1. **Services proxy container.** The Mode 2 job includes a `services:` block running our
   ser2net-proxy image. It dials out to the gateway with a per-session key (reusing the
   Mode 1 connect-script + session model), so it registers as a session and the gateway
   can exec back into it.
2. **Boot-ready signal.** The test definition boots the board and, on reaching a known
   shell prompt, emits `lava-test-case console-ready --result pass`; lava-mcp detects it
   by polling `get_job_results()`.
3. **Endpoint discovery + on-demand forward.** lava-mcp resolves the reserved device's
   console from the LAVA API on the master (`get_job(job_id).actual_device` →
   `get_device_dictionary(hostname, render=True)` → parse `connection_command`
   `telnet <host> <port>`), then instructs the services container (over the gateway exec
   channel) to `ssh -R <console_port>:<ser2net-host>:<ser2net-port>` — the TCP connect to
   ser2net happens **from the container inside the lab**. Doing it on demand avoids
   needing the reserved device at job-submit time.
4. **New tool** `get_serial_console(session_id)` (gated by the user allowlist **and** a
   `serial_console_enabled` flag): verify `console-ready`, ensure the forward is up,
   return a connect command via the gateway-local `<console_port>`.
5. **Console contention.** LAVA holds the console for the whole job; a second connection
   needs ser2net multi-connection, and interactive *writing* realistically only after the
   automated actions finish and the board idles (the `console-ready` gate).

**Device gate + reasonable failure — primitive built.** The `allow_test_services`
check is implemented and tested ahead of the tool: `client.allows_test_services(host)`
(parses `parameters.allow_test_services` from the rendered device dict, mirroring LAVA's
strict `is True`) and `server._require_test_services_device(client, host)`, which raises
an actionable `PermissionError` (*"Serial console needs 'allow_test_services' enabled in
the device dictionary for `<host>` … Ask a lab admin to enable it."*). Wiring: once
`get_serial_console` resolves the reserved device from the job, call this gate **before**
submitting the `services` block, so we never submit a job LAVA would reject at
validation. This is a **device-dict** gate, separate from Mode-1's `allow-remote-access`
**tag** gate (enforced in `open_board_session`, see §A/README); a Mode 2 device generally
needs both.

**Cheap first cut — read-only console, no lab path.** In Mode 2 LAVA already streams the
console to the master as job logs. A `watch_console(job_id)` tool that tails
`jobs/<id>/logs/` gives live boot/kernel output to read (no typing) with zero new infra —
ship this before the interactive bridge.

**Files.** `client.py` (parse `connection_command`, device-dict `allow_test_services`
check, log-tail helper), `server.py` (`get_serial_console`, `watch_console`), `jobs.py`
(+ a Mode 2 job builder with the `services:` block and `console-ready`), a new
`interactive/ser2net-proxy/` image + compose, `config.py`/`cmdline.py`
(`serial_console_enabled` → `LAVA_MCP_SERIAL_CONSOLE_ENABLED`), README, tests.

**Note.** The session must track the **reserved device hostname**, not just `device_type`
— looked up from the job once scheduled.

**Open items to confirm on staging before building the interactive tier:**
- target devices have (or can get) `allow_test_services: true`;
- the services container can reach the console host (ser2net vs raw `/dev/tty*` on the
  worker) and dial out to the gateway;
- ser2net (or LAVA console sharing) permits the concurrent connection.

**Tests.** `connection_command` → `(host, port)` parsing; `allow_test_services` gate +
its failure message; `console-ready` detection from a results payload; Mode 2 job builder
emits the `services:` block; log-tail helper.

**Effort.** Read-only tier: low. Interactive tier: high — services image + on-demand
forward + contention.

---

## C. Per-user session ownership 🔵

**Goal.** A session belongs to the LAVA user who opened it; only that user (or a
configured admin) can `run_in_session` / `close_board_session` / see it. Natural
hardening now that the username allowlist lands the caller's identity server-side.

**Current gap.** `SessionManager` has no ownership; any allowlisted user can operate on
any `session_id`.

**Design.**
- `BoardSession.owner: str | None`, set from `require_session_user()` at open time.
- `run_in_session` / `close_board_session`: reject unless caller is the owner (or in a
  configured `gateway_admin_users` set).
- `list_board_sessions`: return only the caller's sessions (admins see all).

**Files.** `gateway.py` (owner field), `server.py` (ownership checks), `config.py` +
`cmdline.py` (optional `gateway_admin_users`), tests.

**Effort.** Low–medium. Recommend building this alongside A, since human `attach_session`
should be owner-scoped too.

---

## D. Deployment hygiene (not a code feature) 🔵

Pin the factory compose to an immutable image (`ghcr.io/mattface/lava-mcp@sha256:…` or a
`type=sha` tag) instead of `:latest`, so a staging deploy is reproducible and does not
silently move when the image is rebuilt.

---

## Suggested sequencing

1. **C (session ownership)** — small, security-relevant, unblocks safe multi-user use of
   what already ships.
2. **A (human SSH shell)** — resolve the IP-allowlist open question first; build owner-
   scoped from the start.
3. **B (serial console)** — most moving parts (LAVA signal + ser2net + bridge); do last.

Cross-cutting: A and B both add a hosted-mode feature flag and a user-allowlist-gated
tool, so factor a shared "gated interactive tool" helper when building the second.
