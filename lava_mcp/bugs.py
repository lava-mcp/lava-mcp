"""A tiny, size-bounded, text-only bug-report inbox with a feedback loop.

Agents file short bug reports about lava-mcp / LAVA behaviour with the ``report_bug``
tool; an operator reviews them (``list_bug_reports``), can reply to the filer
(``respond``), and clears them (``resolve`` → archived, or ``purge`` → gone). The filer
reads their own reports and any operator reply with ``mine`` — so a report is a
conversation, not a write-only drop box. The store is strictly capped (a per-report
byte limit, a whole-inbox byte limit covering open *and* archived reports, and a maximum
number of open reports) so it can never consume meaningful disk. There are no
attachments or binary payloads: every field is text.

Each report records the ``reporter`` — the LAVA username resolved from the caller's own
token, not something the agent supplies — so a report is always attributable, and the
filer (matched by that same username) can later read the operator's reply.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import secrets
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("lava_mcp")

# strip ASCII control characters except tab/newline/carriage-return
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# report lifecycle
STATUS_OPEN = "open"
STATUS_ANSWERED = "answered"  # an operator has replied; still in the open inbox
STATUS_RESOLVED = "resolved"  # dealt with and moved to the archive (kept for the filer)


def clean_text(value: Any, limit_bytes: int) -> str:
    """Coerce ``value`` to stripped, control-free text, truncated to ``limit_bytes``."""
    if value is None:
        return ""
    s = value if isinstance(value, str) else str(value)
    s = _CONTROL.sub("", s).strip()
    encoded = s.encode("utf-8", "replace")
    if len(encoded) > limit_bytes:
        s = encoded[:limit_bytes].decode("utf-8", "ignore")
    return s


@dataclass
class BugReport:
    """One filed report; persisted as a small JSON file.

    Beyond the filing fields it carries a small lifecycle: ``status`` moves
    open → answered → resolved, and ``response`` / ``responder`` / ``responded`` hold
    the operator's reply so the filer can read it back via :meth:`BugReportStore.mine`.
    """

    bug_id: str
    created: float
    reporter: str  # LAVA username resolved from the caller's token (not agent-supplied)
    summary: str = ""
    details: str = ""
    category: str = ""
    job_id: int | None = None
    status: str = STATUS_OPEN
    response: str = ""  # the operator's reply to the filer, if any
    responder: str = ""  # LAVA username of the operator who replied/resolved
    responded: float | None = None  # when the reply was recorded
    resolved_at: float | None = None  # when it was archived
    # not persisted: set only for a report file that could not be parsed, so an
    # operator sees it (and can purge it) rather than it vanishing silently.
    malformed: bool = field(default=False, compare=False)

    _FIELD_NAMES = None  # populated lazily below

    def public_view(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("malformed", None)
        if self.malformed:
            data["malformed"] = True
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BugReport":
        """Build from stored JSON, ignoring unknown keys (forward/backward compatible)."""
        if cls._FIELD_NAMES is None:
            cls._FIELD_NAMES = {f.name for f in dataclasses.fields(cls)}
        known = {k: v for k, v in data.items() if k in cls._FIELD_NAMES}
        return cls(**known)


class BugReportError(RuntimeError):
    """Raised when a report is rejected (empty, too large, or the inbox is full)."""

    def __init__(self, message: str, *, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


class BugReportStore:
    """On-disk inbox of text-only bug reports, strictly size- and count-bounded.

    Open reports live as ``<bug_id>.json`` under the root; resolved reports move to an
    ``archive/`` subdirectory and are kept (bounded) so the filer can still read the
    outcome. The disk-byte cap covers both; the count cap applies to open reports only,
    so resolving a report always re-opens intake even when the inbox was full.
    """

    def __init__(
        self,
        root: str | None,
        *,
        max_report_bytes: int,
        total_max_bytes: int,
        max_count: int,
    ) -> None:
        self.root = (
            Path(root)
            if root
            else Path(tempfile.gettempdir()) / "lava-mcp-bug-reports"
        )
        self.archive_root = self.root / "archive"
        self.root.mkdir(parents=True, exist_ok=True)
        self.archive_root.mkdir(parents=True, exist_ok=True)
        self.max_report_bytes = max_report_bytes
        self.total_max_bytes = total_max_bytes
        self.max_count = max_count
        self._lock = threading.Lock()
        self._reports: dict[str, BugReport] = {}
        self._archived: dict[str, BugReport] = {}
        self._load()

    def _path(self, bug_id: str, *, archived: bool = False) -> Path:
        base = self.archive_root if archived else self.root
        return base / f"{bug_id}.json"

    def _load_dir(
        self, base: Path, status: str, into: dict[str, BugReport]
    ) -> None:
        for meta in base.glob("*.json"):
            text = ""
            try:
                text = meta.read_text()
                report = BugReport.from_dict(json.loads(text))
            except (ValueError, TypeError):
                # Keep it visible (and purgeable) rather than dropping a report
                # someone took the trouble to file — see listindex's reports.parse.
                logger.warning("bug report: unparseable file %s (kept as malformed)",
                               meta.name)
                try:
                    created = meta.stat().st_mtime
                except OSError:
                    created = time.time()
                report = BugReport(
                    bug_id=meta.stem,
                    created=created,
                    reporter="unknown",
                    summary="(unparseable report — read the file on disk)",
                    status=status,
                    malformed=True,
                )
            into[report.bug_id] = report

    def _load(self) -> None:
        self._load_dir(self.root, STATUS_OPEN, self._reports)
        self._load_dir(self.archive_root, STATUS_RESOLVED, self._archived)

    def _persist(self, report: BugReport, *, archived: bool = False) -> None:
        payload = asdict(report)
        payload.pop("malformed", None)
        self._path(report.bug_id, archived=archived).write_text(json.dumps(payload))

    def _current_bytes(self) -> int:
        total = 0
        for base in (self.root, self.archive_root):
            for p in base.glob("*.json"):
                try:
                    total += p.stat().st_size
                except FileNotFoundError:
                    pass
        return total

    def create(
        self,
        *,
        reporter: str,
        summary: str,
        details: str = "",
        category: str = "",
        job_id: int | None = None,
    ) -> BugReport:
        """Store a report. Raises ``BugReportError`` if empty, too large, or inbox full.

        All text is cleaned and byte-capped; the per-report payload (summary + details +
        category) must fit ``max_report_bytes``. ``reporter`` is the LAVA user the
        caller's token authenticates as — the operator needs to know who asked, and the
        filer is matched on it to read any reply back.
        """
        summary = clean_text(summary, self.max_report_bytes)
        if not summary:
            raise BugReportError("summary is required (text)", reason="empty")
        details = clean_text(details, self.max_report_bytes)
        category = clean_text(category, 100)
        reporter = clean_text(reporter, 100) or "unknown"
        payload_bytes = len((summary + details + category).encode("utf-8"))
        if payload_bytes > self.max_report_bytes:
            raise BugReportError(
                f"bug report too large: {payload_bytes} bytes exceeds the "
                f"{self.max_report_bytes}-byte limit — keep it short and text-only",
                reason="too_large",
            )
        jid: int | None = None
        if isinstance(job_id, bool):
            jid = None
        elif isinstance(job_id, int):
            jid = job_id
        elif isinstance(job_id, str) and job_id.strip().isdigit():
            jid = int(job_id.strip())
        with self._lock:
            if len(self._reports) >= self.max_count:
                raise BugReportError(
                    "bug inbox is full (max report count reached) — an operator must "
                    "review and clear it before new reports are accepted",
                    reason="full",
                )
            if self._current_bytes() >= self.total_max_bytes:
                raise BugReportError(
                    "bug inbox is full (disk cap reached) — an operator must review and "
                    "clear it before new reports are accepted",
                    reason="full",
                )
            bug_id = secrets.token_urlsafe(8)
            report = BugReport(
                bug_id=bug_id,
                created=time.time(),
                reporter=reporter,
                summary=summary,
                details=details,
                category=category,
                job_id=jid,
            )
            self._reports[bug_id] = report
            self._persist(report)
        logger.info(
            "bug report: filed id=%s reporter=%s category=%s job_id=%s summary=%r",
            bug_id,
            reporter,
            category or "-",
            jid,
            summary[:120],
        )
        return report

    def list(self, *, include_resolved: bool = False) -> list[BugReport]:
        """Open reports (oldest first); with ``include_resolved`` the archive follows."""
        with self._lock:
            out = sorted(self._reports.values(), key=lambda r: r.created)
            if include_resolved:
                out += sorted(self._archived.values(), key=lambda r: r.created)
            return out

    def get(self, bug_id: str) -> BugReport | None:
        return self._reports.get(bug_id) or self._archived.get(bug_id)

    def mine(self, reporter: str) -> list[BugReport]:
        """Every report (open and archived) filed by ``reporter``, newest first.

        This is how a filer reads an operator's reply: match is on the LAVA username
        recorded at filing time, so it only ever returns the caller's own reports.
        """
        reporter = clean_text(reporter, 100)
        if not reporter:
            return []
        with self._lock:
            mine = [
                r
                for r in (*self._reports.values(), *self._archived.values())
                if r.reporter == reporter
            ]
        return sorted(mine, key=lambda r: r.created, reverse=True)

    def respond(
        self, bug_id: str, response: str, *, responder: str = ""
    ) -> BugReport | None:
        """Record an operator reply on an OPEN report, leaving it in the inbox.

        The filer sees it next time they call :meth:`mine`. Returns the updated report,
        or None if there is no open report at ``bug_id`` (resolve an archived one is not
        reopened here). Raises ``BugReportError`` if the reply text is empty.
        """
        response = clean_text(response, self.max_report_bytes)
        if not response:
            raise BugReportError("response text is required", reason="empty")
        responder = clean_text(responder, 100)
        with self._lock:
            report = self._reports.get(bug_id)
            if report is None:
                return None
            report.response = response
            report.responder = responder
            report.responded = time.time()
            report.status = STATUS_ANSWERED
            self._persist(report)
        logger.info(
            "bug report: answered id=%s by=%s", bug_id, responder or "-"
        )
        return report

    def resolve(
        self, bug_id: str, *, response: str | None = None, responder: str = ""
    ) -> BugReport | None:
        """Resolve a report: archive it (kept for the filer), freeing an intake slot.

        Optionally attach a final ``response`` at the same time. The report is moved to
        the archive with status ``resolved`` rather than deleted, so the filer can still
        read the outcome via :meth:`mine`; use :meth:`purge` to delete for good. Returns
        the resolved report, or None if ``bug_id`` is not an open report.
        """
        responder = clean_text(responder, 100)
        with self._lock:
            report = self._reports.pop(bug_id, None)
            if report is None:
                return None
            if response is not None:
                cleaned = clean_text(response, self.max_report_bytes)
                if cleaned:
                    report.response = cleaned
                    report.responded = time.time()
            if responder:
                report.responder = responder
            report.status = STATUS_RESOLVED
            report.resolved_at = time.time()
            self._archived[bug_id] = report
            self._persist(report, archived=True)
            try:
                self._path(bug_id).unlink()
            except FileNotFoundError:
                pass
            self._enforce_archive_cap_locked()
        logger.info(
            "bug report: resolved+archived id=%s by=%s", bug_id, responder or "-"
        )
        return report

    def _enforce_archive_cap_locked(self) -> None:
        """Keep the archive bounded: drop the oldest resolved reports past ``max_count``.

        The archive is a best-effort convenience for the filer, not a guarantee, so
        unlike the open inbox it evicts its oldest rather than refusing — the open
        inbox never silently drops a report, but an old resolved one may age out.
        """
        if len(self._archived) <= self.max_count:
            return
        ordered = sorted(
            self._archived.values(), key=lambda r: (r.resolved_at or r.created)
        )
        for report in ordered[: len(self._archived) - self.max_count]:
            self._archived.pop(report.bug_id, None)
            try:
                self._path(report.bug_id, archived=True).unlink()
            except FileNotFoundError:
                pass
            logger.info("bug report: archive full, evicted id=%s", report.bug_id)

    def purge(self, bug_id: str) -> BugReport | None:
        """Delete a report for good, open or archived, freeing its space. None if absent.

        Not undoable — a report is the one thing here nothing can recreate — so it is
        deliberately one id at a time.
        """
        with self._lock:
            report = self._reports.pop(bug_id, None)
            archived = report is None
            if report is None:
                report = self._archived.pop(bug_id, None)
            if report is None:
                return None
            try:
                self._path(bug_id, archived=archived).unlink()
            except FileNotFoundError:
                pass
        logger.info("bug report: purged id=%s", bug_id)
        return report
