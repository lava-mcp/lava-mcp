# Implementation plan: unbuilt interactive features

The README's *Interactive board sessions* section carries the user-facing **design**
for the pieces below. This doc is the **engineering plan** for building them — files,
functions, config, tests, and the open questions to resolve first.

Status legend: 🟢 shipped · 🟡 planned (design agreed) · 🔵 proposed (not yet agreed).

Shipped foundation (for reference): the SSH rendezvous gateway, `open_board_session` /
`run_in_session` / `close_board_session` / `list_board_sessions`, and the gateway
access-control allowlists (`LAVA_MCP_GATEWAY_ALLOW_IPS`,
`LAVA_MCP_GATEWAY_ALLOW_USERS`).

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

## B. Direct serial console via ser2net 🟡

**Goal.** After the LAVA job flags it has booted to a shell, a human attaches to the
board's raw UART (fronted by ser2net) — boot/kernel/panic logs and the login prompt,
no DUT networking required.

**Current gap.** Nothing exists: no boot-ready signal, no ser2net endpoint discovery,
no console tool or bridge.

**Design.**
1. **Boot-ready signal.** The interactive test definition boots the board and, on
   reaching a known shell prompt, emits `lava-test-case console-ready --result pass`.
   lava-mcp detects it by polling `get_job_results()` for that test case (reuses
   existing client code; no signal-stream plumbing needed).
2. **Endpoint discovery.** Resolve the reserved device's console from LAVA itself:
   `get_job(job_id)` → `actual_device` (hostname), then
   `get_device_dictionary(hostname, render=True)` and parse `connection_command`
   (typically `telnet <host> <port>`) into `(host, port)`.
3. **New tool** `get_serial_console(session_id)` (gated by the user allowlist **and** a
   `serial_console_enabled` flag): verify `console-ready`, resolve the ser2net endpoint,
   then return a connect command. In-lab callers get `telnet <host> <port>` directly;
   remote callers get a gateway-side TCP proxy (`telnet <gateway-host> <proxy-port>`)
   that forwards to the ser2net endpoint.
4. **Console contention** (deployment concern to document): either ser2net is configured
   for multiple connections (human shares LAVA's console), or the interactive test def
   idles after signaling ready — reservation held, console released — so the human takes
   it over. `close_board_session` cancels the job and reclaims it.

**Files.** new `serial console` helpers in `client.py` (parse `connection_command`) and
`gateway.py` (TCP proxy listener), `server.py` (`get_serial_console`), `jobs.py` /
`interactive/ssh-gateway.yaml` (emit `console-ready`, optional idle-hold), `config.py` +
`cmdline.py` (`serial_console_enabled` → `LAVA_MCP_SERIAL_CONSOLE_ENABLED`), README, tests.

**Note.** The session must track the **reserved device hostname**, not just
`device_type` — looked up from the job once scheduled.

**Tests.** `connection_command` → `(host, port)` parsing; `console-ready` detection from
a results payload; tool gated by flag + allowlist; TCP proxy forwards bytes.

**Effort.** Medium–high — endpoint discovery + TCP bridge + contention handling.

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
