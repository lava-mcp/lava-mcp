# LAVA jobs & inventory (the standard workflow)

This is the core of the server and what you should reach for first: a thin proxy over
LAVA's REST API for querying the board farm and running ordinary test jobs. Every tool
here grants exactly what your own LAVA token grants.

## The standard deploy/boot/test job

The standard way to use LAVA is a **non-interactive job**: a YAML definition with an
`actions:` list that DEPLOYS an image to the board (fetch/flash), BOOTS it, then runs
one or more TEST definitions that record pass/fail results. You submit it, it queues
for a free board of the requested `device_type`, and you read its logs and results once
it runs. The great majority of LAVA use is exactly this shape — the
[interactive sessions](board-sessions.md) and [serial console](serial-console.md) are
smaller, special-case features.

### Building a job

Deploy/boot parameters — flash method, storage/media, partitioning/rawprogram and
artifact auth — are image- and device-specific and easy to get wrong. The easiest path
is to adapt a previous **successful** job whose deploy `url` matches the artifacts you
want:

- `find_boot_template(artifact_url, device_type)` (or `list_jobs` + `get_job_definition`)
  returns a candidate. Keep its deploy+boot actions, swap in your URL, and **keep its
  artifact authentication** (`Authorization`/token headers). Don't base a job on an
  unrelated job such as a health-check.
- If you craft a job yourself, first study several recent jobs on that device_type:
  their definitions (`get_job_definition`) and their `metadata` (`get_job`) — submitters
  often record the build/source/artifact context there.
- If the server offers [`read_lava_docs`](lava-source-docs.md), read the action
  reference for the deploy/boot methods you use.
- Always `validate_job` before submitting.

### Lifecycle

`validate_job` (check without submitting) → `submit_job` (returns the job id) → poll
`get_job` for state and health, and read `get_job_logs` / `get_job_results`;
`cancel_job` stops a queued or running job. A finished job's health is **Complete** (all
passed) or **Incomplete** (something failed).

## Jobs are independent — no continuity

The lab is shared and jobs are independent. Do **not** assume continuity across jobs:

- The scheduler picks a free board per job, so the board you land on is not yours to
  keep. Jobs are **not** guaranteed to run concurrently, one-after-another, or on the
  same board.
- You cannot carry device state (power/boot/bootloader state, flashed contents, uptime,
  peripherals) between jobs. If several steps depend on a board's state, they **must** be
  in a single job.
- Artifacts a job downloads (deploy URLs and test-definition repos) are fetched into
  that job's own workspace and deleted when it ends — they are **not** shared with other
  jobs. Every job that needs a file must fetch it itself.
- Within the **same** job, a file an earlier action downloads stays in that job's
  workspace and is available to later actions (including a docker test container running
  later) — so download once up front, then use it.

### Reusing another job's artifact

When LAVA downloaded a file with an auth header, the URL alone will not work for you:
your job must fetch it the same way, through a LAVA deploy `download` action carrying
the same header, and if the header names a LAVA token, that token must be registered
under **your own** user (LAVA only substitutes the submitter's own tokens). This holds
even when the lab cache (kisscache) already has the artifact — a cache hit still needs
the auth header, and only LAVA can substitute the token. If `$HTTP_CACHE` is set (a URL
template containing `%s`), fetch through it to speed up repeat/large downloads.

### Server-side file transforms

LAVA can modify a downloaded file server-side, so you can transform files you cannot
even fetch locally: add `postprocess: {docker: {image: ..., steps: [...]}}` to a
`deploy: to: downloads` action. LAVA runs those shell steps in that image with the
downloaded files at `/lava-downloads` (the working directory) — decompress, patch,
repack, sign, inject a config — and the modified files are used by later actions.
`$HTTP_CACHE` is exported into the postprocess env too.

## Searching jobs by metadata

`list_jobs(metadata={...})` filters on the job's `metadata` dict (what submitters record
about a build/source). Each `{key: value}` becomes a LAVA `metadata__<key>` query. The
key may be nested and/or carry a Django lookup with `__`:

```
metadata={"build__id": "1234"}
metadata={"branch__startswith": "release/"}
metadata={"source__icontains": "linux"}
```

Default lookup is exact. This is the way to **find** jobs by what they built/tested
(e.g. a boot template for a specific branch or build) rather than scanning definitions.
Read a job's `metadata` (`get_job`) when studying it — it often explains what the job
actually built/tested better than the definition alone.

## Tools

Read/observe: `whoami`, `version`, `list_devices`, `get_device`,
`get_device_dictionary`, `get_qdl_info`, `list_device_types`, `list_workers`,
`list_jobs`, `get_job`, `get_job_definition`, `get_job_logs`, `get_job_results`,
`get_queue`, `get_running`, `get_lab_health`, `find_boot_template`, `validate_job`.

Write (omitted with `--read-only`): `submit_job`, `cancel_job`, `resubmit_job`.
