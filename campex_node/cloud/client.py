from __future__ import annotations

import json
import logging
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
        return self._send("POST", "/node/heartbeat", payload=payload)

    def _send(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> CloudResult:
        assert self.settings.cloud_url is not None
        url = f"{self.settings.cloud_url}{path}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.settings.cloud_token:
            headers["Authorization"] = f"Bearer {self.settings.cloud_token}"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.settings.cloud_timeout_seconds) as response:
                return CloudResult(ok=200 <= response.status < 300, status_code=response.status)
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
