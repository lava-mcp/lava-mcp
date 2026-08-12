from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from lava_mcp.config import Config
from lava_mcp.server import build_server


@pytest.fixture
def app_and_store(tmp_path):
    cfg = Config(
        url="https://lava.example.com",
        token="t",
        artifacts_enabled=True,
        artifact_base_url="https://h/mcp/artifacts",
        artifact_dir=str(tmp_path),
    )
    mcp = build_server(cfg)
    return TestClient(mcp.streamable_http_app()), mcp._lava_artifacts


def test_put_then_get_roundtrip_over_http(app_and_store) -> None:
    client, store = app_and_store
    art, token = store.create("kernel.img", 3, "alice")
    url = f"/mcp/artifacts/{art.artifact_id}/kernel.img"

    # upload streams to the store
    r = client.put(url, content=b"abc", headers={"Authorization": token})
    assert r.status_code == 200 and r.json()["stored"] is True
    assert store.get(art.artifact_id).state == "stored"

    # download returns the bytes with the real filename in the disposition
    r = client.get(url, headers={"Authorization": token})
    assert r.status_code == 200 and r.content == b"abc"
    assert "kernel.img" in r.headers.get("content-disposition", "")

    # the bare-id URL (no filename) works too
    r = client.get(
        f"/mcp/artifacts/{art.artifact_id}", headers={"Authorization": token}
    )
    assert r.status_code == 200 and r.content == b"abc"


def test_auth_is_enforced(app_and_store) -> None:
    client, store = app_and_store
    art, token = store.create("f.bin", 3, "alice")
    url = f"/mcp/artifacts/{art.artifact_id}/f.bin"
    store.store_bytes(art, b"abc")

    assert client.get(url).status_code == 401  # no token
    assert client.get(url, headers={"Authorization": "wrong"}).status_code == 401
    # a Bearer prefix is accepted
    assert (
        client.get(url, headers={"Authorization": f"Bearer {token}"}).status_code == 200
    )
    # unknown id -> 404
    assert (
        client.get(
            "/mcp/artifacts/nope/f.bin", headers={"Authorization": token}
        ).status_code
        == 404
    )


def test_get_before_upload_is_409_and_double_put_is_409(app_and_store) -> None:
    client, store = app_and_store
    art, token = store.create("f.bin", 3, "alice")
    url = f"/mcp/artifacts/{art.artifact_id}/f.bin"
    hdr = {"Authorization": token}

    assert client.get(url, headers=hdr).status_code == 409  # not uploaded yet
    assert client.put(url, content=b"abc", headers=hdr).status_code == 200
    assert client.put(url, content=b"xyz", headers=hdr).status_code == 409  # again
