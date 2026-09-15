# Temporary artifact store

When you have a build product (kernel, rootfs, DTB, script, ...) you want a job to
deploy/flash or a booted device to fetch, but nowhere to host it, upload it here. LAVA —
or a job running on the board / in a device-connected container — fetches it back over
the same 443/Caddy path as the SSH gateway.

Enable it in hosted mode with `LAVA_MCP_ARTIFACTS_ENABLED=1`; the routes mount under
`/mcp/artifacts` (Caddy already routes `/mcp*`). See [configuration.md](configuration.md)
for the retention/size knobs and [deployment.md](deployment.md) for hosting.

## How it works

The bytes never travel through an MCP tool call (that would blow the model context) —
the tool only mints an **upload ticket**, and you push the file with an HTTP `PUT`.

- `create_artifact_upload(filename, size_bytes, ttl_seconds?, bind_job_id?)` reserves a
  slot and returns a `put_command`, a `get_url` (with the real filename on the end for
  readability), a bearer `token`, ready-made `deploy_block` / `example_job_snippet` /
  `fetch_command`, and expiry. Pass the real `size_bytes` so the store can pre-check its
  per-artifact cap and disk floor before you start pushing a multi-GB file.
- Upload with the `put_command` (an HTTP `PUT` carrying the bearer token).
- `list_artifacts()` shows your artifacts (id, filename, size, state, expiry).
- `delete_artifact(artifact_id)` removes it early and revokes its LAVA token.

## Security & retention model

There is no way at the HTTP layer to prove a fetcher is the LAVA dispatcher rather than
the DUT, so this is a **capability store**, not a dispatcher-authenticated one:

- an unguessable `artifact_id` in the URL path + a bearer token (compared in constant
  time against a stored SHA-256 hash) guard every artifact;
- retention is TTL-bounded (default and max ~6h) so a leaked token dies quickly;
- uploads are refused when they would exceed the per-artifact cap (default 6 GB) or push
  the disk below the free-space floor (default 10% free);
- an optional `bind_job_id` makes the artifact stop serving (HTTP 410) once that job
  finishes;
- metadata persists as a JSON sidecar beside the blob, so the store survives a restart;
  a background reaper deletes expired artifacts.

## Three ways to consume an artifact

### 1. LAVA deploy download (dispatcher fetches + flashes)

The upload token is **also** registered with LAVA as a per-user *remote artifact token*
named `lava-mcp-artifact-<id>`. When a deploy/test action's header value is a token NAME
you own, LAVA swaps in the secret at download time, so the secret never appears in the
stored job definition or logs. Paste the returned `deploy_block` under the image/url in
your deploy action (see `example_job_snippet` for placement):

```yaml
- deploy:
    to: <your deploy method, from the template job>
    images:
      image:
        url: https://<domain>/mcp/artifacts/<id>/<filename>
        headers:
          Authorization: lava-mcp-artifact-<id>   # token NAME; LAVA substitutes the secret
```

Also set the job's top-level `visibility: personal` so its URL is not publicly readable.
(If LAVA token registration fails, `deploy_block` carries the raw token inline instead —
the tool says so — so `visibility: personal` matters even more.)

### 2. Onto a booted device with networking

Run the returned `fetch_command` (plain `curl`, token inline) from a test action that
executes **on the DUT** — that lands the file on the device's own filesystem (e.g. push
a test binary, config, or firmware to a running board). The device must reach this
server over HTTPS and have `curl`/`wget`.

### 3. Inside a device-connected container (board session)

Run the same `fetch_command` in the container — or, better, pass the artifact to
`open_board_session(downloads=[{"url", "headers"}])` / `open_console_session(downloads=...)`
so LAVA fetches it (substituting the token) and mounts it at `/lava-downloads`. The
container itself cannot substitute a token.

The inline-token cases (2 and 3, and the `fetch_command`) expose the token to that
context by necessity — rely on the short TTL.

## Reusing a token you already hold

`list_remote_artifact_tokens()` returns the **names** of your LAVA remote artifact
tokens (secret values are never shown), so you can reference an existing one by name in a
download/deploy `headers:` value. LAVA substitutes the secret when it downloads, so the
value never enters the job.

## Tools

`create_artifact_upload`, `list_artifacts`, `delete_artifact` — offered only when the
store is enabled; `create_artifact_upload` and `delete_artifact` are also hidden with
`--read-only`. `list_remote_artifact_tokens` is always available (it only needs your
LAVA token).
