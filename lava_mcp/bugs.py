"""A tiny, size-bounded, text-only bug-report inbox.

Agents file short bug reports about lava-mcp / LAVA behaviour with the ``report_bug``
tool; an operator reviews them periodically (``list_bug_reports``) and clears them
(``resolve_bug_report``). The store is strictly capped — a per-report byte limit, a
whole-inbox byte limit, and a maximum number of reports — so it can never consume
meaningful disk. There are no attachments or binary payloads: every field is text.

Each report records the ``reporter`` — the LAVA username resolved from the caller's own
token, not something the agent supplies — so a report is always attributable.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("lava_mcp")

# strip ASCII control characters except tab/newline/carriage-return
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


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
    """One filed report; persisted as a small JSON file."""

    bug_id: str
    created: float
    reporter: str  # LAVA username resolved from the caller's token (not agent-supplied)
    summary: str
    details: str = ""
    category: str = ""
    job_id: int | None = None

    def public_view(self) -> dict[str, Any]:
        return asdict(self)


class BugReportError(RuntimeError):
    """Raised when a report is rejected (empty, too large, or the inbox is full)."""

    def __init__(self, message: str, *, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


class BugReportStore:
    """On-disk inbox of text-only bug reports, strictly size- and count-bounded."""

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
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_report_bytes = max_report_bytes
        self.total_max_bytes = total_max_bytes
        self.max_count = max_count
        self._lock = threading.Lock()
        self._reports: dict[str, BugReport] = {}
        self._load()

    def _path(self, bug_id: str) -> Path:
        return self.root / f"{bug_id}.json"

    def _load(self) -> None:
        for meta in self.root.glob("*.json"):
            try:
                report = BugReport(**json.loads(meta.read_text()))
            except (ValueError, TypeError):
                logger.warning("bug report: ignoring unreadable %s", meta.name)
                continue
            self._reports[report.bug_id] = report

    def _persist(self, report: BugReport) -> None:
        self._path(report.bug_id).write_text(json.dumps(asdict(report)))

    def _current_bytes(self) -> int:
        total = 0
        for p in self.root.glob("*.json"):
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
        caller's token authenticates as — the operator needs to know who asked.
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

    def list(self) -> list[BugReport]:
        with self._lock:
            return sorted(self._reports.values(), key=lambda r: r.created)

    def get(self, bug_id: str) -> BugReport | None:
        return self._reports.get(bug_id)

    def resolve(self, bug_id: str) -> BugReport | None:
        """Remove a reviewed report, freeing its space. None if it was unknown."""
        with self._lock:
            report = self._reports.pop(bug_id, None)
            if report is None:
                return None
            try:
                self._path(bug_id).unlink()
            except FileNotFoundError:
                pass
        logger.info("bug report: resolved+removed id=%s", bug_id)
        return report
