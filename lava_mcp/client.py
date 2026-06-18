"""Thin synchronous client for the LAVA REST API (v0.2/v0.3)."""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import requests
import yaml

from .config import Config


class LavaError(RuntimeError):
    """Raised when a LAVA REST call fails."""


class LavaClient:
    """Wraps the LAVA REST API with the operations lava-mcp exposes.

    Authentication uses ``Authorization: Token <secret>`` when a token is set;
    a few endpoints (version, dashboards) work anonymously.
    """

    def __init__(self, config: Config, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()
        # base ends with a slash so urljoin appends relative paths correctly
        self.base = f"{config.url.rstrip('/')}/api/{config.api_version}/"
        if config.token:
            self.session.headers["Authorization"] = f"Token {config.token}"

    # -- low level ---------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> requests.Response:
        url = urljoin(self.base, path.lstrip("/"))
        try:
            resp = self.session.request(
                method,
                url,
                params=_clean(params),
                json=json,
                timeout=self.config.timeout,
            )
        except requests.RequestException as exc:
            raise LavaError(f"request to {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise LavaError(
                f"{method} {url} -> HTTP {resp.status_code}: {resp.text[:500]}"
            )
        return resp

    def _get_json(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params=params).json()

    def _list(self, path: str, limit: int, **params: Any) -> dict[str, Any]:
        """Return a single page: {count, results} (LimitOffsetPagination)."""
        data = self._get_json(path, limit=limit, **params)
        if isinstance(data, dict) and "results" in data:
            return {"count": data.get("count"), "results": data["results"]}
        return {"count": len(data), "results": data}

    # -- system ------------------------------------------------------------
    def whoami(self) -> Any:
        return self._get_json("system/whoami/")

    def version(self) -> Any:
        return self._get_json("system/version/")

    # -- inventory ---------------------------------------------------------
    def list_devices(self, limit: int = 50, **filters: Any) -> dict[str, Any]:
        return self._list("devices/", limit, **filters)

    def get_device(self, hostname: str) -> Any:
        return self._get_json(f"devices/{hostname}/")

    def get_device_dictionary(self, hostname: str, render: bool = False) -> str:
        params = {"render": "true"} if render else None
        return self._request(
            "GET", f"devices/{hostname}/dictionary/", params=params
        ).text

    def get_qdl_info(self, hostname: str) -> dict[str, Any]:
        """Summarise a device's QDL/flash capability from its rendered config."""
        data = yaml.safe_load(self.get_device_dictionary(hostname, render=True)) or {}
        actions = data.get("actions", {}) if isinstance(data, dict) else {}
        deploy = actions.get("deploy", {}).get("methods", {}) or {}
        boot = actions.get("boot", {}).get("methods", {}) or {}
        return {
            "hostname": hostname,
            "supports_qdl": "qdl" in deploy or "qdl" in boot,
            "qdl_deploy": deploy.get("qdl"),
            "qdl_boot": boot.get("qdl"),
            "deploy_methods": sorted(deploy.keys()),
            "boot_methods": sorted(boot.keys()),
        }

    def list_device_types(self, limit: int = 100, **filters: Any) -> dict[str, Any]:
        return self._list("devicetypes/", limit, **filters)

    def list_workers(self, limit: int = 100) -> dict[str, Any]:
        return self._list("workers/", limit)

    # -- jobs --------------------------------------------------------------
    def list_jobs(self, limit: int = 25, **filters: Any) -> dict[str, Any]:
        return self._list("jobs/", limit, **filters)

    def get_job(self, job_id: int | str) -> Any:
        return self._get_json(f"jobs/{job_id}/")

    def get_job_definition(self, job_id: int | str) -> str:
        job = self.get_job(job_id)
        return job.get("original_definition") or job.get("definition") or ""

    def get_job_logs(
        self, job_id: int | str, start: int | None = None, end: int | None = None
    ) -> str:
        return self._request(
            "GET", f"jobs/{job_id}/logs/", params={"start": start, "end": end}
        ).text

    def get_job_results(self, job_id: int | str, limit: int = 200) -> dict[str, Any]:
        return self._list(f"jobs/{job_id}/tests/", limit)

    # -- dashboards (v0.3) -------------------------------------------------
    def dashboard_queue(self) -> Any:
        return self._get_json("dashboard/queue/")

    def dashboard_running(self) -> Any:
        return self._get_json("dashboard/running/")

    def dashboard_lab_health(self) -> Any:
        return self._get_json("dashboard/lab-health/")

    # -- writes ------------------------------------------------------------
    def validate_job(self, definition: str) -> Any:
        return self._request(
            "POST", "jobs/validate/", json={"definition": definition}
        ).json()

    def submit_job(self, definition: str) -> Any:
        return self._request("POST", "jobs/", json={"definition": definition}).json()

    def cancel_job(self, job_id: int | str) -> Any:
        return _maybe_json(self._request("POST", f"jobs/{job_id}/cancel/"))

    def resubmit_job(self, job_id: int | str) -> Any:
        return _maybe_json(self._request("POST", f"jobs/{job_id}/resubmit/"))

    def set_job_priority(self, job_id: int | str, priority: int) -> Any:
        return self._request(
            "POST", f"jobs/{job_id}/priority/", json={"priority": priority}
        ).json()


def _clean(params: dict[str, Any] | None) -> dict[str, Any] | None:
    if not params:
        return None
    return {k: v for k, v in params.items() if v is not None}


def _maybe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {"status": resp.status_code, "text": resp.text[:200]}
