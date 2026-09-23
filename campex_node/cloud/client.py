from __future__ import annotations

import json
import logging
import platform
import socket
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from backend.cameras.security import sanitize_error_message

from campex_node.core.config import NodeSettings


logger = logging.getLogger("campex.node.cloud")


@dataclass(frozen=True)
class CloudResult:
    ok: bool
    status_code: int | None = None
    error: str | None = None
    data: Any | None = None


class CloudClient:
    def __init__(self, settings: NodeSettings) -> None:
        self.settings = settings

    def is_configured(self) -> bool:
        return bool(self.settings.cloud_url)

    def check_connection(self) -> CloudResult:
        if not self.settings.cloud_url:
            return CloudResult(ok=False, error="CAMPEX_NODE_CLOUD_URL is not configured.")
        return self._send("GET", "/health")

    def send_heartbeat(self, payload: dict[str, Any]) -> CloudResult:
        if not self.settings.cloud_url:
            logger.info("Cloud URL not configured; heartbeat kept local.")
            return CloudResult(ok=False, error="CAMPEX_NODE_CLOUD_URL is not configured.")
        node_id = payload.get("node_id") or self.settings.node_id
        if not node_id:
            return CloudResult(ok=False, error="Node is not paired.")
        heartbeat_payload = dict(payload)
        heartbeat_payload.pop("node_id", None)
        heartbeat_payload.setdefault("platform", platform.system().lower())
        heartbeat_payload.setdefault("hostname", socket.gethostname())
        heartbeat_payload.setdefault("vision_status", "idle")
        heartbeat_payload.setdefault("queue_size", 0)
        return self._send("POST", f"/nodes/{node_id}/heartbeat", payload=heartbeat_payload)

    def fetch_config(self) -> CloudResult:
        if not self.settings.cloud_url:
            return CloudResult(ok=False, error="CAMPEX_NODE_CLOUD_URL is not configured.")
        if not self.settings.node_id:
            return CloudResult(ok=False, error="Node is not paired.")
        return self._send("GET", f"/nodes/{self.settings.node_id}/config")

    def claim_pairing_code(self, *, code: str, node_name: str, version: str) -> CloudResult:
        if not self.settings.cloud_url:
            return CloudResult(ok=False, error="CAMPEX_NODE_CLOUD_URL is not configured.")
        return self._send(
            "POST",
            "/nodes/pair/claim",
            payload={
                "code": code,
                "node_name": node_name,
                "platform": platform.system().lower(),
                "hostname": socket.gethostname(),
                "version": version,
            },
            include_node_token=False,
        )

    def send_events(self, events: list[dict[str, Any]]) -> CloudResult:
        if not self.settings.cloud_url:
            return CloudResult(ok=False, error="CAMPEX_NODE_CLOUD_URL is not configured.")
        return self._send("POST", "/node-sync/events", payload=events)

    def send_metrics(self, metrics: list[dict[str, Any]]) -> CloudResult:
        if not self.settings.cloud_url:
            return CloudResult(ok=False, error="CAMPEX_NODE_CLOUD_URL is not configured.")
        return self._send("POST", "/node-sync/metrics", payload=metrics)

    def send_batch(self, *, events: list[dict[str, Any]], metrics: list[dict[str, Any]]) -> CloudResult:
        if not self.settings.cloud_url:
            return CloudResult(ok=False, error="CAMPEX_NODE_CLOUD_URL is not configured.")
        return self._send("POST", "/node-sync/batch", payload={"events": events, "metrics": metrics})

    def _send(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        include_node_token: bool = True,
    ) -> CloudResult:
        assert self.settings.cloud_url is not None
        url = f"{self.settings.cloud_url}{path}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if include_node_token and self.settings.cloud_token:
            headers["Authorization"] = f"Bearer {self.settings.cloud_token}"
        if self.settings.cloud_api_token:
            headers["X-CAMPEX-Token"] = self.settings.cloud_api_token
        if self.settings.organization_id:
            headers["X-CAMPEX-Organization-Id"] = self.settings.organization_id
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.settings.cloud_timeout_seconds) as response:
                response_body = response.read()
                data = json.loads(response_body.decode("utf-8")) if response_body else None
                return CloudResult(
                    ok=200 <= response.status < 300,
                    status_code=response.status,
                    data=data,
                )
        except HTTPError as exc:
            return CloudResult(
                ok=False,
                status_code=exc.code,
                error=sanitize_error_message(str(exc)),
            )
        except URLError as exc:
            return CloudResult(ok=False, error=sanitize_error_message(str(exc.reason)))
        except OSError as exc:
            return CloudResult(ok=False, error=sanitize_error_message(str(exc)))
