"""Temporary, capability-guarded artifact store served over the MCP app.

An agent uploads a build product (kernel, rootfs, DTB, script, ...) with an HTTP
``PUT`` and LAVA — or a job running on the board / in a device-connected container —
fetches it back with a ``GET``, over the same 443/Caddy path as the SSH gateway. The
bytes never travel through an MCP tool call (they would blow the model context); the
tool only mints an upload ticket.

Security model (see the design notes): there is no way at the HTTP layer to prove a
fetcher is the LAVA dispatcher rather than the DUT, so this is a *capability* store,
not a dispatcher-authenticated one:

* an unguessable ``artifact_id`` in the URL path + a bearer token (compared in
  constant time against a stored hash) guard every artifact;
* retention is TTL-bounded (<= a few hours) so a leaked token dies quickly;
* uploads are refused when they would exceed the per-artifact cap or push the disk
  below a free-space floor.

For the LAVA deploy-download path the token is additionally kept out of the job
entirely: it is registered as a LAVA per-user *remote artifact token* and the deploy
block references the token NAME, which LAVA substitutes for the secret at download
time. That wiring lives in the server tools; this module only owns the bytes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

logger = logging.getLogger("lava_mcp")

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str) -> str:
    """Reduce an agent-supplied name to a safe basename for the URL + disposition.

    Strips any directory part and anything but ``[A-Za-z0-9._-]`` so it is inert in a
    URL path and a ``Content-Disposition`` header, and can never traverse the store.
    """
    base = os.path.basename((name or "").strip()).lstrip(".")
    cleaned = _SAFE_FILENAME.sub("_", base).strip("_")
    return cleaned or "artifact"


@dataclass
class Artifact:
    """Metadata for one stored (or awaited) artifact; persisted as a JSON sidecar."""

    artifact_id: str
    filename: str
    owner: str
    token_hash: str
    size_declared: int
    created: float
    expires: float
    state: str = "await_upload"  # "await_upload" -> "stored"
    size_actual: int | None = None
    bind_job_id: int | None = None
    lava_token_name: str | None = None

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires

    def public_view(self) -> dict[str, Any]:
        """Owner-facing view (never includes the token or its hash)."""
        return {
            "artifact_id": self.artifact_id,
            "filename": self.filename,
            "state": self.state,
            "size_declared": self.size_declared,
            "size_actual": self.size_actual,
            "created": self.created,
            "expires": self.expires,
            "bind_job_id": self.bind_job_id,
        }


class ArtifactError(RuntimeError):
    """Raised for admission failures (too large, disk full, bad state)."""


class ArtifactStore:
    """On-disk store of temporary artifacts with capability-token access.

    Layout under ``root``: ``<id>.json`` (metadata) beside ``<id>.blob`` (bytes),
    with ``<id>.part`` used transiently during an upload. Metadata is also cached in
    memory and reloaded from disk on startup so the store survives a restart.
    """

    def __init__(
        self,
        root: str | os.PathLike[str] | None,
        *,
        base_url: str,
        ttl_default: float,
        ttl_max: float,
        max_bytes: int,
        min_free_fraction: float,
    ) -> None:
        # A stable default (not a fresh mkdtemp) so the store survives a restart and
        # does not orphan a new temp dir each boot; override with LAVA_MCP_ARTIFACT_DIR.
        self.root = (
            Path(root) if root else Path(tempfile.gettempdir()) / "lava-mcp-artifacts"
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url.rstrip("/")
        self.ttl_default = ttl_default
        self.ttl_max = ttl_max
        self.max_bytes = max_bytes
        self.min_free_fraction = min_free_fraction
        self._lock = threading.Lock()
        self._artifacts: dict[str, Artifact] = {}
        # (owner, lava_token_name) awaiting deletion — the background reaper cannot act
        # as a LAVA user, so removals are flushed when that owner next calls a tool.
        self._pending_token_deletions: set[tuple[str, str]] = set()
        self._load()

    # -- persistence -------------------------------------------------------
    def _meta_path(self, artifact_id: str) -> Path:
        return self.root / f"{artifact_id}.json"

    def _blob_path(self, artifact_id: str) -> Path:
        return self.root / f"{artifact_id}.blob"

    def _part_path(self, artifact_id: str) -> Path:
        return self.root / f"{artifact_id}.part"

    def _load(self) -> None:
        for meta in self.root.glob("*.json"):
            try:
                data = json.loads(meta.read_text())
                art = Artifact(**data)
            except (ValueError, TypeError):
                logger.warning("artifact: ignoring unreadable metadata %s", meta.name)
                continue
            self._artifacts[art.artifact_id] = art

    def _persist(self, art: Artifact) -> None:
        self._meta_path(art.artifact_id).write_text(json.dumps(asdict(art)))

    def _remove_files(self, artifact_id: str) -> None:
        for path in (
            self._meta_path(artifact_id),
            self._blob_path(artifact_id),
            self._part_path(artifact_id),
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    # -- disk accounting ---------------------------------------------------
    def _free_after(self, extra_bytes: int) -> tuple[int, int]:
        """(free-after-extra, total) bytes on the store's volume."""
        usage = shutil.disk_usage(self.root)
        return usage.free - max(extra_bytes, 0), usage.total

    def check_admission(self, size_bytes: int) -> None:
        """Raise ``ArtifactError`` if an upload of ``size_bytes`` is not allowed."""
        if size_bytes < 0:
            raise ArtifactError("size must be non-negative")
        if size_bytes > self.max_bytes:
            raise ArtifactError(
                f"artifact too large: {size_bytes} bytes exceeds the "
                f"{self.max_bytes}-byte per-artifact cap"
            )
        free_after, total = self._free_after(size_bytes)
        if total and free_after < self.min_free_fraction * total:
            raise ArtifactError(
                "insufficient disk: this upload would leave less than "
                f"{int(self.min_free_fraction * 100)}% free on the artifact volume"
            )

    # -- lifecycle ---------------------------------------------------------
    def create(
        self,
        filename: str,
        size_bytes: int,
        owner: str,
        *,
        ttl_seconds: float | None = None,
        bind_job_id: int | None = None,
    ) -> tuple[Artifact, str]:
        """Reserve an artifact slot; returns (metadata, plaintext bearer token).

        Admission (size cap + disk floor) is checked up front against the declared
        ``size_bytes`` so the agent is not told to start a doomed multi-GB upload.
        """
        self.reap()
        self.check_admission(size_bytes)
        ttl = self.ttl_default if ttl_seconds is None else ttl_seconds
        ttl = max(1.0, min(ttl, self.ttl_max))
        artifact_id = secrets.token_urlsafe(16)
        token = secrets.token_urlsafe(32)
        now = time.time()
        art = Artifact(
            artifact_id=artifact_id,
            filename=safe_filename(filename),
            owner=owner,
            token_hash=_hash_token(token),
            size_declared=size_bytes,
            created=now,
            expires=now + ttl,
            bind_job_id=bind_job_id,
        )
        with self._lock:
            self._artifacts[artifact_id] = art
            self._persist(art)
        return art, token

    def get(self, artifact_id: str) -> Artifact | None:
        art = self._artifacts.get(artifact_id)
        if art is None:
            return None
        if art.expired:
            self._reap_one(art)
            return None
        return art

    def verify_token(self, art: Artifact, presented: str | None) -> bool:
        if not presented:
            return False
        return hmac.compare_digest(art.token_hash, _hash_token(presented))

    def blob_path(self, art: Artifact) -> Path:
        return self._blob_path(art.artifact_id)

    def get_url(self, art: Artifact) -> str:
        return f"{self.base_url}/{art.artifact_id}/{quote(art.filename)}"

    async def write_stream(
        self, art: Artifact, chunks: AsyncIterator[bytes], content_length: int | None
    ) -> Artifact:
        """Stream an upload to disk, enforcing the size cap and disk floor.

        Writes to ``<id>.part`` then atomically renames to ``<id>.blob``; on any
        breach the partial file is discarded and the artifact left awaiting upload.
        """
        if art.state != "await_upload":
            raise ArtifactError("artifact already uploaded")
        if content_length is not None:
            self.check_admission(content_length)
        part = self._part_path(art.artifact_id)
        written = 0
        checkpoint = 0
        try:
            with open(part, "wb") as fh:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > self.max_bytes:
                        raise ArtifactError("upload exceeds the per-artifact cap")
                    # re-check free space periodically for streams with no/dishonest
                    # Content-Length, so a runaway upload cannot fill the disk.
                    if written - checkpoint >= 64 * 1024 * 1024:
                        checkpoint = written
                        free_after, total = self._free_after(0)
                        if total and free_after < self.min_free_fraction * total:
                            raise ArtifactError("disk floor reached during upload")
                    fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(part, self._blob_path(art.artifact_id))
        except BaseException:
            try:
                part.unlink()
            except FileNotFoundError:
                pass
            raise
        with self._lock:
            art.state = "stored"
            art.size_actual = written
            self._persist(art)
        return art

    def store_bytes(self, art: Artifact, data: bytes) -> Artifact:
        """Synchronous helper (tests / tiny inline uploads): store ``data`` at once."""
        if art.state != "await_upload":
            raise ArtifactError("artifact already uploaded")
        self.check_admission(len(data))
        self._blob_path(art.artifact_id).write_bytes(data)
        with self._lock:
            art.state = "stored"
            art.size_actual = len(data)
            self._persist(art)
        return art

    def delete(self, artifact_id: str, owner: str | None = None) -> Artifact | None:
        """Remove an artifact (and queue its LAVA token for deletion). Returns the
        removed metadata, or None if it was absent / owned by someone else."""
        with self._lock:
            art = self._artifacts.get(artifact_id)
            if art is None:
                return None
            if owner is not None and art.owner != owner:
                return None
            self._artifacts.pop(artifact_id, None)
            self._remove_files(artifact_id)
            if art.lava_token_name:
                self._pending_token_deletions.add((art.owner, art.lava_token_name))
        return art

    def list_for(self, owner: str) -> list[Artifact]:
        self.reap()
        return [a for a in self._artifacts.values() if a.owner == owner]

    def set_lava_token_name(self, artifact_id: str, name: str) -> None:
        with self._lock:
            art = self._artifacts.get(artifact_id)
            if art is not None:
                art.lava_token_name = name
                self._persist(art)

    # -- retention ---------------------------------------------------------
    def _reap_one(self, art: Artifact) -> None:
        with self._lock:
            self._artifacts.pop(art.artifact_id, None)
            self._remove_files(art.artifact_id)
            if art.lava_token_name:
                self._pending_token_deletions.add((art.owner, art.lava_token_name))

    def reap(self) -> list[Artifact]:
        """Delete expired artifacts; returns those removed (LAVA tokens are queued)."""
        expired = [a for a in list(self._artifacts.values()) if a.expired]
        for art in expired:
            self._reap_one(art)
        return expired

    def take_pending_token_deletions(self, owner: str) -> list[str]:
        """Pop and return LAVA token names awaiting deletion for ``owner`` (the caller
        deletes them via that user's client, since the reaper has no LAVA identity)."""
        with self._lock:
            taken = [n for (o, n) in self._pending_token_deletions if o == owner]
            self._pending_token_deletions -= {(owner, n) for n in taken}
        return taken


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
