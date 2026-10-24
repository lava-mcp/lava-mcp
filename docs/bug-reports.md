# Bug-report inbox

A small, text-only inbox so agents (and humans) can report problems with lava-mcp or
LAVA behaviour, which an operator reviews periodically. It's enabled by default
(`LAVA_MCP_BUG_REPORTS_ENABLED=1`) and needs no hosted mode.

## What it's for

A defect, a wrong or confusing tool result, a gap in the server instructions, or LAVA
behaviour that seems broken — anything a maintainer should see. It is **not** a place to
stash logs, task output, or large context: reports are deliberately tiny.

## Reporting

`report_bug(summary, details="", category="", job_id=None)`

- `summary` (required) — one line.
- `details` — optional repro / expected vs actual.
- `category` — optional short tag (e.g. `docs`, `tool-error`, `lava`).
- `job_id` — optional related LAVA job.

The **reporter is recorded automatically** from the caller's LAVA token (`whoami`) — it
is never supplied by the agent, so every report is attributable to a real LAVA user.
Returns the `bug_id` and the recorded `reporter`, or an `error` with a `reason`
(`empty` / `too_large` / `full`).

## Reviewing (operator)

- `list_bug_reports()` — every open report with reporter, time, summary/details,
  category and job_id.
- `resolve_bug_report(bug_id)` — removes a reviewed report, **freeing its space**.

Both are gated by the same user allowlist as the interactive *use* tools
(`LAVA_MCP_HTTP_ALLOW_USERS`); open to all when that is unset. Reports are stored as one
small JSON file each under `LAVA_MCP_BUG_REPORT_DIR`, so you can also read them straight
off disk. Filing and resolving are logged under the `lava_mcp` logger.

## Size bounds (so it never eats disk)

Every field is text, cleaned of control characters and byte-capped. Three limits, all
configurable (see [configuration.md](configuration.md)):

- **per report** — `LAVA_MCP_BUG_REPORT_MAX_BYTES` (default 8 KB; summary + details +
  category). Over this, the report is rejected (`too_large`) — summarise instead.
- **whole inbox on disk** — `LAVA_MCP_BUG_REPORT_TOTAL_MAX_BYTES` (default 4 MB).
- **number of reports** — `LAVA_MCP_BUG_REPORT_MAX_COUNT` (default 1000).

When the inbox hits its disk or count cap, new reports are rejected (`full`) rather than
silently dropped or evicting existing ones — the signal to an operator to review and
clear it. Resolving reports frees space and count.
