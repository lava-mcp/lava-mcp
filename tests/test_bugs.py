from __future__ import annotations

import pytest

from lava_mcp.bugs import BugReportError, BugReportStore, clean_text


def make_store(tmp_path, **kw) -> BugReportStore:
    opts = dict(max_report_bytes=8 * 1024, total_max_bytes=4 * 1024**2, max_count=1000)
    opts.update(kw)
    return BugReportStore(str(tmp_path), **opts)


def test_clean_text_strips_control_and_caps_bytes() -> None:
    assert clean_text("  hi\x00there \x07 ", 100) == "hithere"
    assert clean_text("keep\ttab\nnl", 100) == "keep\ttab\nnl"
    assert clean_text(None, 100) == ""
    assert clean_text(1234, 100) == "1234"  # coerced to text
    assert clean_text("a" * 50, 10) == "a" * 10  # byte-capped


def test_create_records_reporter_and_fields(tmp_path) -> None:
    store = make_store(tmp_path)
    r = store.create(
        reporter="alice",
        summary="validate_job rejects a valid qemu job",
        details="steps...",
        category="tool-error",
        job_id=42,
    )
    assert r.reporter == "alice"
    assert r.summary.startswith("validate_job")
    assert r.category == "tool-error"
    assert r.job_id == 42
    # survives a reload from disk
    reloaded = make_store(tmp_path)
    got = reloaded.get(r.bug_id)
    assert got is not None and got.reporter == "alice" and got.job_id == 42


def test_summary_required_and_reporter_defaults(tmp_path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(BugReportError) as exc:
        store.create(reporter="", summary="   ")
    assert exc.value.reason == "empty"
    # a missing reporter falls back to a placeholder, never blank
    r = store.create(reporter="", summary="something")
    assert r.reporter == "unknown"


def test_per_report_size_cap(tmp_path) -> None:
    store = make_store(tmp_path, max_report_bytes=64)
    with pytest.raises(BugReportError) as exc:
        store.create(reporter="a", summary="x", details="y" * 200)
    assert exc.value.reason == "too_large"


def test_inbox_count_and_disk_caps(tmp_path) -> None:
    count = make_store(tmp_path, max_count=2)
    count.create(reporter="a", summary="one")
    count.create(reporter="a", summary="two")
    with pytest.raises(BugReportError) as exc:
        count.create(reporter="a", summary="three")
    assert exc.value.reason == "full"

    disk = make_store(tmp_path / "d", total_max_bytes=1)  # any report trips the cap
    disk.create(reporter="a", summary="first fills it")
    with pytest.raises(BugReportError) as exc2:
        disk.create(reporter="a", summary="second")
    assert exc2.value.reason == "full"


def test_resolve_removes_and_frees(tmp_path) -> None:
    store = make_store(tmp_path)
    r = store.create(reporter="a", summary="bug")
    assert store.resolve(r.bug_id) is not None
    assert store.get(r.bug_id) is None
    assert store.resolve(r.bug_id) is None  # already gone
    assert not (tmp_path / f"{r.bug_id}.json").exists()


def test_job_id_coercion(tmp_path) -> None:
    store = make_store(tmp_path)
    assert store.create(reporter="a", summary="s", job_id="77").job_id == 77
    assert store.create(reporter="a", summary="s", job_id=None).job_id is None
    assert store.create(reporter="a", summary="s", job_id="nope").job_id is None
