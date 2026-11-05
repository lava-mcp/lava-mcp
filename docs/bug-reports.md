# Bug-report inbox

A small, text-only inbox so agents (and humans) can report problems with lava-mcp or
LAVA behaviour, which an operator reviews periodically — and, because the operator can
reply and the filer can read that reply back, a report is a short conversation rather
than a write-only drop box. It's enabled by default (`LAVA_MCP_BUG_REPORTS_ENABLED=1`)
and needs no hosted mode.

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

## Reading your own reports (filer)

`my_bug_reports()` — open to any token holder — returns the reports **you** filed
(matched on your LAVA username), newest first, each with its `status`
(`open` / `answered` / `resolved`) and the operator's `response` once one is recorded.
An agent that filed a bug in an earlier session calls this to see whether it was
answered or resolved. It only ever returns the caller's own reports.

## Reviewing (operator)

- `list_bug_reports(include_resolved=False)` — every open report with reporter, time,
  summary/details, category, job_id, status and any response; set `include_resolved`
  to also list the archive.
- `respond_bug_report(bug_id, response)` — reply to a report and mark it `answered`;
  it stays in the inbox and the filer reads the reply via `my_bug_reports`.
- `resolve_bug_report(bug_id, response="")` — mark a report dealt with. It is **moved
  to the archive** (status `resolved`), not deleted, so the filer can still read the
  outcome; this also frees an intake slot. Pass `response` to reply and resolve in one
  step.
- `purge_bug_report(bug_id)` — delete a report for good, open or archived (no copy
  kept). Use it to clear the archive or drop an unparseable/spam report.

These four **require a LAVA admin (superuser) account**. Reviewing the inbox means
reading other users' reports, so the caller must authenticate as a LAVA admin — and that
is checked against LAVA itself, not self-asserted. LAVA exposes no self-service admin
flag (`system/whoami/` returns only the username), so the server detects it by probing a
superuser-only REST endpoint (`GET users/`): 200 means admin, 403 means not. The check
fails closed — a bad token or an unreachable LAVA denies review rather than granting it.

`LAVA_MCP_BUG_ADMIN_USERS` optionally narrows this further: when set, a reviewer must be
a LAVA admin **and** named in it (a comma/space-separated username list, independent of
the interactive-use allowlist); empty means any LAVA admin may review. Filing
(`report_bug`) and reading your own (`my_bug_reports`) are always open regardless.

Reports are stored as one small JSON file each under `LAVA_MCP_BUG_REPORT_DIR` (resolved
ones under `archive/`), so an admin can also read them straight off disk. Filing,
replying, resolving and purging are logged under the `lava_mcp` logger.

A report file that can't be parsed is **kept and shown** (marked `malformed`) rather
than silently dropped, so an operator always sees that something was filed and can
`purge` it.

## Size bounds (so it never eats disk)

Every field is text, cleaned of control characters and byte-capped. Three limits, all
configurable (see [configuration.md](configuration.md)):

- **per report** — `LAVA_MCP_BUG_REPORT_MAX_BYTES` (default 8 KB; summary + details +
  category). Over this, the report is rejected (`too_large`) — summarise instead.
- **whole inbox on disk** — `LAVA_MCP_BUG_REPORT_TOTAL_MAX_BYTES` (default 4 MB),
  counting **open and archived** reports together.
- **number of open reports** — `LAVA_MCP_BUG_REPORT_MAX_COUNT` (default 1000).

When the inbox hits its disk or open-count cap, new reports are rejected (`full`) rather
than silently dropped or evicting existing ones — the signal to an operator to review
and clear it. Resolving a report frees an open-count slot (it moves to the archive) but
not disk; `purge` frees both. The archive is itself bounded to `MAX_COUNT` resolved
reports and, unlike the open inbox, evicts its **oldest** when it would overflow — it is
a best-effort convenience for the filer, never a guarantee, so an old resolved report
may age out before the filer reads it.
