from __future__ import annotations

import pytest
import responses

from lava_mcp.client import LavaClient, LavaError
from lava_mcp.config import Config

BASE = "https://lava.example.com/api/v0.3/"


@pytest.fixture
def client() -> LavaClient:
    return LavaClient(Config(url="https://lava.example.com", token="secret"))


@responses.activate
def test_whoami_sets_token_header(client: LavaClient) -> None:
    responses.get(BASE + "system/whoami/", json={"user": "matt"})
    assert client.whoami() == {"user": "matt"}
    assert responses.calls[0].request.headers["Authorization"] == "Token secret"


@responses.activate
def test_version(client: LavaClient) -> None:
    responses.get(BASE + "system/version/", json={"version": "2026.05"})
    assert client.version()["version"] == "2026.05"


@responses.activate
def test_list_devices_pagination(client: LavaClient) -> None:
    responses.get(
        BASE + "devices/",
        json={
            "count": 2,
            "next": None,
            "results": [{"hostname": "a"}, {"hostname": "b"}],
        },
    )
    out = client.list_devices(limit=50, health="Good")
    assert out["count"] == 2
    assert [d["hostname"] for d in out["results"]] == ["a", "b"]
    # filters + limit are passed as query params, None values dropped
    assert responses.calls[0].request.params == {"limit": "50", "health": "Good"}


@responses.activate
def test_get_job_definition_uses_original(client: LavaClient) -> None:
    responses.get(
        BASE + "jobs/42/",
        json={"id": 42, "original_definition": "job_name: x", "definition": None},
    )
    assert client.get_job_definition(42) == "job_name: x"


@responses.activate
def test_get_job_logs_returns_text(client: LavaClient) -> None:
    responses.get(BASE + "jobs/42/logs/", body="- {lvl: info, msg: hello}\n")
    assert "hello" in client.get_job_logs(42, start=0, end=10)


@responses.activate
def test_submit_job(client: LavaClient) -> None:
    responses.post(BASE + "jobs/", json={"message": "ok", "job_ids": [7]}, status=201)
    assert client.submit_job("job_name: x")["job_ids"] == [7]
    assert responses.calls[0].request.body is not None


@responses.activate
def test_set_job_priority(client: LavaClient) -> None:
    responses.post(BASE + "jobs/7/priority/", json={"id": 7, "priority": 90})
    assert client.set_job_priority(7, 90)["priority"] == 90


@responses.activate
def test_http_error_raises_lavaerror(client: LavaClient) -> None:
    responses.get(BASE + "jobs/999/", json={"detail": "Not found"}, status=404)
    with pytest.raises(LavaError) as exc:
        client.get_job(999)
    assert "404" in str(exc.value)


@responses.activate
def test_anonymous_dashboard_no_auth_header() -> None:
    client = LavaClient(Config(url="https://lava.example.com"))  # no token
    responses.get(BASE + "dashboard/queue/", json={"count": 0, "results": []})
    client.dashboard_queue()
    assert "Authorization" not in responses.calls[0].request.headers


@responses.activate
def test_get_qdl_info_parses_rendered_dictionary(client: LavaClient) -> None:
    rendered = """
actions:
  deploy:
    methods:
      qdl: {parameters: {flash_cmds_order: [boot]}}
      fastboot: {}
  boot:
    methods:
      qdl: {}
      fastboot: {}
"""
    responses.get(BASE + "devices/rb3g2-01/dictionary/", body=rendered)
    info = client.get_qdl_info("rb3g2-01")
    assert info["supports_qdl"] is True
    assert info["deploy_methods"] == ["fastboot", "qdl"]
    assert responses.calls[0].request.params == {"render": "true"}
