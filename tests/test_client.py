from __future__ import annotations

import pytest
import responses

from lava_mcp.client import (
    LavaClient,
    LavaError,
    client_from,
    device_dict_allows_test_services,
)
from lava_mcp.config import Config


def test_device_dict_allows_test_services() -> None:
    assert device_dict_allows_test_services(
        "parameters:\n  allow_test_services: True\n"
    )
    # lowercase yaml bool is fine (both parse to Python True)
    assert device_dict_allows_test_services(
        "parameters:\n  allow_test_services: true\n"
    )
    # explicitly disabled, absent, or no parameters block → not allowed
    assert not device_dict_allows_test_services(
        "parameters:\n  allow_test_services: false\n"
    )
    assert not device_dict_allows_test_services("parameters:\n  interfaces: {}\n")
    assert not device_dict_allows_test_services("actions: {}\n")
    assert not device_dict_allows_test_services("")


def test_client_from_pins_configured_url_but_uses_client_token() -> None:
    # A deployment pinned to a LAVA instance (LAVA_URL set) ignores any client
    # X-Lava-Url, but still authenticates as the client via X-Lava-Token.
    cfg = Config(url="https://pinned.example.com", token="deftok")
    c = client_from(
        cfg, {"x-lava-url": "https://elsewhere.example.com", "x-lava-token": "htok"}
    )
    assert c.base.startswith("https://pinned.example.com/api/")
    assert c.session.headers["Authorization"] == "Token htok"


def test_client_from_uses_header_url_when_not_pinned() -> None:
    # No LAVA_URL configured (fully multi-tenant): the client supplies the target.
    c = client_from(
        Config(url=""),
        {"x-lava-url": "https://chosen.example.com", "x-lava-token": "htok"},
    )
    assert c.base.startswith("https://chosen.example.com/api/")
    assert c.session.headers["Authorization"] == "Token htok"


def test_client_from_falls_back_to_config() -> None:
    cfg = Config(url="https://default.example.com", token="deftok")
    c = client_from(cfg, None)
    assert c.base.startswith("https://default.example.com/api/")


def test_client_from_requires_a_url() -> None:
    with pytest.raises(LavaError):
        client_from(Config(url=""), {})


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
def test_cancel_job_uses_get(client: LavaClient) -> None:
    # LAVA's cancel action is a GET; a POST returns HTTP 405.
    responses.get(BASE + "jobs/7/cancel/", json={"message": "Job cancel signal sent."})
    assert "cancel" in client.cancel_job(7)["message"].lower()
    assert responses.calls[0].request.method == "GET"


@responses.activate
def test_resubmit_job_uses_post(client: LavaClient) -> None:
    responses.post(BASE + "jobs/7/resubmit/", json={"message": "ok", "job_ids": [8]})
    assert client.resubmit_job(7)["job_ids"] == [8]
    assert responses.calls[0].request.method == "POST"


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
def test_allows_test_services_reads_device_dictionary(client: LavaClient) -> None:
    responses.get(
        BASE + "devices/rb3g2-01/dictionary/",
        body="parameters:\n  allow_test_services: True\n",
    )
    assert client.allows_test_services("rb3g2-01") is True
    assert responses.calls[0].request.params == {"render": "true"}


@responses.activate
def test_allows_test_services_false_when_unset(client: LavaClient) -> None:
    responses.get(
        BASE + "devices/rb3g2-02/dictionary/",
        body="parameters:\n  interfaces: {}\n",
    )
    assert client.allows_test_services("rb3g2-02") is False


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
