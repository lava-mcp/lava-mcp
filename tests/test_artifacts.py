from __future__ import annotations

import asyncio
import time

import pytest

from lava_mcp.artifacts import ArtifactError, ArtifactStore, safe_filename


def make_store(tmp_path, **kw) -> ArtifactStore:
    opts = dict(
        base_url="https://h/mcp/artifacts",
        ttl_default=6 * 3600.0,
        ttl_max=6 * 3600.0,
        max_bytes=6 * 1024**3,
        min_free_fraction=0.10,
    )
    opts.update(kw)
    return ArtifactStore(str(tmp_path), **opts)


async def _drain(store, art, chunks, length=None):
    async def gen():
        for c in chunks:
            yield c

    return await store.write_stream(art, gen(), length)


def test_safe_filename_strips_paths_and_unsafe_chars() -> None:
    assert safe_filename("kernel.img") == "kernel.img"
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("/abs/path/boot.img.gz") == "boot.img.gz"
    assert safe_filename("weird name!@#.bin") == "weird_name_.bin"
    assert safe_filename("") == "artifact"
    assert safe_filename("...") == "artifact"


def test_create_then_stream_and_read_roundtrip(tmp_path) -> None:
    store = make_store(tmp_path)
    art, token = store.create("boot.img.gz", 5, "alice")
    assert art.state == "await_upload"
    # canonical URL carries id + real filename
    assert (
        store.get_url(art) == f"https://h/mcp/artifacts/{art.artifact_id}/boot.img.gz"
    )
    # token is verifiable, a wrong one is not
    assert store.verify_token(art, token)
    assert not store.verify_token(art, "nope")
    assert not store.verify_token(art, None)

    asyncio.run(_drain(store, art, [b"hel", b"lo"], length=5))
    assert art.state == "stored"
    assert art.size_actual == 5
    assert store.blob_path(art).read_bytes() == b"hello"
    # metadata survives a reload from disk
    reloaded = make_store(tmp_path)
    got = reloaded.get(art.artifact_id)
    assert got is not None and got.size_actual == 5


def test_admission_rejects_oversize_and_disk_floor(tmp_path) -> None:
    store = make_store(tmp_path, max_bytes=10)
    with pytest.raises(ArtifactError):
        store.create("big.bin", 20, "alice")  # exceeds per-artifact cap
    floor = make_store(tmp_path, min_free_fraction=1.0)
    with pytest.raises(ArtifactError):
        floor.create("x.bin", 1, "alice")  # can never leave 100% free


def test_stream_over_cap_aborts_and_keeps_await_state(tmp_path) -> None:
    store = make_store(tmp_path, max_bytes=4)
    art, _ = store.create("f.bin", 0, "alice")  # declared 0 passes admission
    with pytest.raises(ArtifactError):
        asyncio.run(_drain(store, art, [b"toolong"], length=None))
    assert art.state == "await_upload"
    assert not store.blob_path(art).exists()
    assert not store._part_path(art.artifact_id).exists()


def test_expiry_reaps_and_get_returns_none(tmp_path) -> None:
    store = make_store(tmp_path)
    art, _ = store.create("f.bin", 3, "alice", ttl_seconds=1)
    art.expires = time.time() - 1  # force expiry
    assert store.get(art.artifact_id) is None
    assert store.get(art.artifact_id) is None  # already gone


def test_ttl_is_capped_at_max(tmp_path) -> None:
    store = make_store(tmp_path, ttl_max=100.0)
    art, _ = store.create("f.bin", 1, "alice", ttl_seconds=9999)
    assert art.expires - art.created == pytest.approx(100.0, abs=1.0)


def test_delete_is_owner_scoped_and_queues_token(tmp_path) -> None:
    store = make_store(tmp_path)
    art, _ = store.create("f.bin", 1, "alice")
    store.set_lava_token_name(art.artifact_id, "lava-mcp-artifact-x")
    # a different owner cannot delete it
    assert store.delete(art.artifact_id, owner="bob") is None
    removed = store.delete(art.artifact_id, owner="alice")
    assert removed is not None
    # the LAVA token is queued for the owner to flush
    assert store.take_pending_token_deletions("alice") == ["lava-mcp-artifact-x"]
    assert store.take_pending_token_deletions("alice") == []


def test_list_for_is_owner_scoped(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create("a.bin", 1, "alice")
    store.create("b.bin", 1, "bob")
    assert [a.filename for a in store.list_for("alice")] == ["a.bin"]
