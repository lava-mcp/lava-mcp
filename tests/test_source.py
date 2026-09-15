from __future__ import annotations

from pathlib import Path

from lava_mcp.source import LavaSourceMirror, _safe_join


def _mirror(tmp_path, ready=True, ref="abc123") -> LavaSourceMirror:
    m = LavaSourceMirror(
        repo="https://gitlab.com/lava/lava.git",
        explicit_ref="",
        clone_dir=str(tmp_path),
        version_fn=lambda: "",
        ref_from_version=lambda v: "",
    )
    m._ready = ready
    m._ref = ref
    return m


def test_safe_join_confines_to_base(tmp_path) -> None:
    base = tmp_path
    assert _safe_join(base, "doc/content/x.md") == (base / "doc/content/x.md").resolve()
    assert _safe_join(base, "/doc/x.md") == (base / "doc/x.md").resolve()  # leading /
    # traversal / absolute escape -> None
    assert _safe_join(base, "../../etc/passwd") is None
    assert _safe_join(base, "a/../../b") is None
    assert _safe_join(base, "") is None


def test_read_and_list_come_from_the_checkout(tmp_path) -> None:
    tr = tmp_path / "doc/content/technical-references"
    tr.mkdir(parents=True)
    (tr / "architecture.md").write_text("# Architecture\n")
    (tr / "results.md").write_text("# Results\n")
    (tr / "job-definition").mkdir()
    (tr / "job-definition" / "index.md").write_text("# Jobs\n")
    (tmp_path / ".git").mkdir()  # must be excluded from listings
    (tmp_path / ".git" / "config").write_text("x")
    m = _mirror(tmp_path, ref="deadbeef")

    got = m.read("doc/content/technical-references/architecture.md")
    assert got["text"] == "# Architecture\n" and got["ref"] == "deadbeef"

    listed = m.list("doc/content/technical-references/")
    assert listed["ref"] == "deadbeef"
    assert listed["files"] == [
        "doc/content/technical-references/architecture.md",
        "doc/content/technical-references/job-definition/index.md",
        "doc/content/technical-references/results.md",
    ]  # recursive, sorted, .git excluded


def test_read_errors_are_clear(tmp_path) -> None:
    m = _mirror(tmp_path)
    assert "invalid path" in m.read("../secret")["error"]
    assert "not found" in m.read("doc/content/missing.md")["error"]


def test_reads_blocked_until_ready(tmp_path) -> None:
    m = _mirror(tmp_path, ready=False)
    assert "not mirrored yet" in m.read("doc/content/x.md")["error"]
    assert "not mirrored yet" in m.list("doc/content/")["error"]


def test_resolve_ref_prefers_explicit_then_version(tmp_path) -> None:
    explicit = LavaSourceMirror(
        repo="r",
        explicit_ref="2026.07",
        clone_dir=str(tmp_path),
        version_fn=lambda: "ignored",
        ref_from_version=lambda v: "derived",
    )
    assert explicit._resolve_ref() == "2026.07"
    derived = LavaSourceMirror(
        repo="r",
        explicit_ref="",
        clone_dir=str(tmp_path),
        version_fn=lambda: "2026.07-42-gdeadbeef",
        ref_from_version=lambda v: "deadbeef" if v else "",
    )
    assert derived._resolve_ref() == "deadbeef"
    # no version available -> empty ref -> sync would not open the gate
    blank = LavaSourceMirror(
        repo="r",
        explicit_ref="",
        clone_dir=str(tmp_path),
        version_fn=lambda: "",
        ref_from_version=lambda v: "",
    )
    assert blank._resolve_ref() == ""
