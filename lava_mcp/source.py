"""A local mirror of the deployed LAVA's source, kept in sync in the background.

The MCP server can start before LAVA is reachable: a daemon thread periodically reads
the deployed version from the LAVA API (unless a ref is pinned), then clones/updates a
local checkout of the source repo at that ref. Only once a checkout succeeds does the
``ready`` gate open — so read_lava_docs never serves a guessed ref, and a wrong/missing
version just leaves docs unavailable until the next successful sync.

Reading is then plain filesystem access to the checkout (docs are Markdown under
``doc/content/``; any source file is readable too), so there is no per-file network
fetch and no directory traversal over a git-host API.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger("lava_mcp")


def _safe_join(base: Path, relpath: str) -> Path | None:
    """Resolve ``relpath`` under ``base``; None if it escapes ``base`` or is absolute."""
    rel = (relpath or "").strip().lstrip("/").split("#", 1)[0]
    if not rel or "\0" in rel:
        return None
    target = (base / rel).resolve()
    base_r = base.resolve()
    if target != base_r and base_r not in target.parents:
        return None
    return target


class LavaSourceMirror:
    """Keeps a local git checkout of the LAVA source at the deployed ref, refreshed on
    a timer. Thread-safe; reads block only during the brief checkout swap."""

    def __init__(
        self,
        *,
        repo: str,
        explicit_ref: str,
        clone_dir: str | None,
        version_fn: Callable[[], str],
        ref_from_version: Callable[[object], str],
        poll_interval: float = 300.0,
        git_timeout: float = 300.0,
    ) -> None:
        self.repo = repo
        self.explicit_ref = explicit_ref
        self.dir = (
            Path(clone_dir)
            if clone_dir
            else Path(tempfile.gettempdir()) / "lava-mcp-source"
        )
        self._version_fn = version_fn
        self._ref_from_version = ref_from_version
        self.poll_interval = poll_interval
        self.git_timeout = git_timeout
        self._lock = threading.Lock()
        self._ready = False
        self._ref: str | None = None
        self._last_error = ""
        self._started = False

    # -- lifecycle ---------------------------------------------------------
    def ensure_started(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._run, name="lava-mcp-source", daemon=True).start()

    def _run(self) -> None:
        while True:
            try:
                self.sync_once()
            except Exception as exc:  # noqa: BLE001 - background loop must not die
                self._last_error = str(exc)
                logger.warning("lava source mirror: sync failed: %s", exc)
            time.sleep(self.poll_interval)

    def _resolve_ref(self) -> str:
        if self.explicit_ref:
            return self.explicit_ref
        return self._ref_from_version(self._version_fn())

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            check=True,
            capture_output=True,
            timeout=self.git_timeout,
        )

    def sync_once(self) -> bool:
        """Resolve the ref and clone/update the checkout to it. Returns True on success
        (gate open), False if the ref could not be resolved yet."""
        ref = self._resolve_ref()
        if not ref:
            return False
        d = str(self.dir)
        if not (self.dir / ".git").exists():
            self.dir.parent.mkdir(parents=True, exist_ok=True)
            # partial clone: all commits/trees, blobs fetched lazily on checkout — small,
            # and lets us check out any tag or (short) commit sha.
            self._git("clone", "--filter=blob:none", "--no-checkout", self.repo, d)
        self._git("-C", d, "fetch", "--filter=blob:none", "--tags", "--force", "origin")
        with self._lock:
            self._git(
                "-C",
                d,
                "-c",
                "advice.detachedHead=false",
                "checkout",
                "--force",
                "--detach",
                ref,
            )
            self._ref = ref
            self._ready = True
            self._last_error = ""
        return True

    # -- state -------------------------------------------------------------
    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def ref(self) -> str | None:
        return self._ref

    def _not_ready(self) -> dict:
        detail = f" (last error: {self._last_error})" if self._last_error else ""
        return {
            "error": "the LAVA source is not mirrored yet — the server is starting up "
            "or the LAVA API is unreachable, so the deployed version/ref is unknown. "
            "Retry shortly" + detail
        }

    # -- reads (plain filesystem on the checkout) --------------------------
    def read(self, relpath: str) -> dict:
        if not self._ready:
            return self._not_ready()
        target = _safe_join(self.dir, relpath)
        if target is None:
            return {"error": f"invalid path: {relpath!r}"}
        with self._lock:
            if not target.is_file():
                return {"error": f"not found at ref {self._ref}: {relpath}"}
            try:
                text = target.read_text(errors="replace")
            except OSError as exc:
                return {"error": f"read failed: {exc}"}
            return {"path": relpath, "ref": self._ref, "text": text}

    def list(self, relpath: str) -> dict:
        if not self._ready:
            return self._not_ready()
        base = _safe_join(self.dir, relpath)
        if base is None or not base.is_dir():
            return {"error": f"not a directory at ref {self._ref}: {relpath}"}
        with self._lock:
            files = sorted(
                str(p.relative_to(self.dir))
                for p in base.rglob("*")
                if p.is_file() and ".git/" not in f"{p.relative_to(self.dir)}/"
            )
        return {"dir": relpath.rstrip("/"), "ref": self._ref, "files": files}
