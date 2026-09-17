# LAVA docs & source mirror (`read_lava_docs`)

Agents usually can't fetch LAVA's rendered documentation directly (most instances sit
behind bot-protection such as Anubis), and the docs don't always cover exact behaviour.
So the server can offer LAVA's documentation **and** its source code itself, read from a
local git mirror of the exact deployed build — the code the agent is actually driving.

## Enabling it

Set `LAVA_SOURCE_REPO` to the git repo the deployed LAVA was built from. `LAVA_SOURCE_REF`
is optional:

- unset → the server derives the ref from the LAVA API's reported version. A plain
  release like `2026.07` is used as a tag; a git-describe build like
  `2026.07-<count>-<sha>` resolves to the exact commit `<sha>`.
- set → pins the ref explicitly.

Optional: `LAVA_SOURCE_DIR` (checkout location, default a temp subdir) and
`LAVA_SOURCE_POLL_INTERVAL` (how often, in seconds, the poller re-reads the version and
updates the checkout; default 300). See [configuration.md](configuration.md).

The container image ships `git` for this.

## How the mirror works

`read_lava_docs` is only offered when `LAVA_SOURCE_REPO` is set — with no source
configured there is nothing to read, and the server doesn't tell agents to read docs.

A background daemon thread ([`lava_mcp/source.py`](../lava_mcp/source.py)):

1. resolves the ref (explicit, or derived from the live LAVA API version);
2. clones the repo as a partial checkout (`git clone --filter=blob:none --no-checkout`,
   blobs fetched lazily) and checks out that ref;
3. opens the **ready gate** only after a successful checkout.

Consequences of this design:

- The MCP server can start **before** LAVA is reachable. At startup `ensure_started()`
  just spawns the thread and returns — no git or network on the request path. If the
  version can't be read yet, the ref won't resolve and the mirror simply retries next
  poll; the server is unaffected.
- Docs and source are served **only** once the mirror has checked out. Until then
  `read_lava_docs` returns a "not mirrored yet — retry shortly" error and serves
  nothing — never a guessed ref.
- The ref is re-checked periodically, so the mirror follows the deployed version.

### Logging

The mirror logs (`logging` name `lava_mcp`, level INFO) when it starts a clone, when the
ready gate opens (with repo, resolved ref, and dir), when the ref later changes, and when
the version/ref can't be resolved yet. Sync failures log at WARNING.

## Reading

Reads are plain filesystem access to the checkout, with path-traversal protection, so
there's no per-file network fetch or git-host API traversal.

`read_lava_docs(path)` where `path` is repo-relative:

- a file path → `{path, ref, text}`;
- a directory path ending in `/` → `{dir, ref, files}` (recursive listing, `.git`
  excluded);
- on error → `{error}`.

LAVA's docs are Markdown under `doc/content/`; the technical reference is the directory
`doc/content/technical-references/`. The default path is
`doc/content/technical-references/architecture.md`.

```
read_lava_docs('doc/content/technical-references/')          # list the whole section
read_lava_docs('doc/content/technical-references/results.md') # read one page
read_lava_docs('lava_dispatcher/actions/deploy/')            # read the deployed source
```

## Read selectively

The technical reference is large — dozens of pages, one per boot method, one per deploy
method, and one per service. The server's instructions deliberately tell agents to read
it **selectively**, not wholesale: read the few core pages that explain the model
(architecture, `job-definition/job.md`, `results.md`) if unfamiliar with LAVA, then list
the section and read only the page(s) for the deploy/boot method(s) a given job actually
uses. Reading the whole tree would burn a large amount of context for little benefit.
Source is consulted on demand, only when the docs don't settle a question — so the
answer matches exactly what is running.
