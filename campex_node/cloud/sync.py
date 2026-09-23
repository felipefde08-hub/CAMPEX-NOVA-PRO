from __future__ import annotations

import logging
import threading

from backend.cameras.security import sanitize_error_message

from campex_node.cloud.client import CloudClient
from campex_node.core.config import NodeSettings
from campex_node.storage.local_store import LocalStore


logger = logging.getLogger("campex.node.sync")


class SyncService:
    def __init__(
        self,
        *,
        settings: NodeSettings,
        cloud_client: CloudClient,
        store: LocalStore,
    ) -> None:
        self.settings = settings
        self.cloud_client = cloud_client
        self.store = store
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="campex-node-sync",
            daemon=True,
        )

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3)

    def sync_once(self, limit: int = 50) -> dict:
        items = self.store.pending_outbound(limit=limit)
        sent = failed = 0
        for item in items:
            result = self._send_item(item)
            if result:
                self.store.mark_outbound_synced(item["id"])
                sent += 1
            else:
                attempts = item["attempts"] + 1
                retry_seconds = min(300, 2 ** min(attempts, 8))
                self.store.mark_outbound_failed(
                    item["id"],
                    self._last_error or "Sync failed.",
                    retry_seconds=retry_seconds,
                )
                failed += 1
        return {"sent": sent, "failed": failed, "pending": self.store.outbound_queue_size()}

    _last_error: str | None = None

    def _send_item(self, item: dict) -> bool:
        payload_type = item["type"]
        payload = item["payload"]
        if payload_type == "event":
            result = self.cloud_client.send_events([payload])
        elif payload_type == "metric":
            result = self.cloud_client.send_metrics([payload])
        elif payload_type == "heartbeat":
            result = self.cloud_client.send_heartbeat(payload)
        else:
            self._last_error = sanitize_error_message(f"Unknown outbound type: {payload_type}")
            logger.warning("Sync item failed: %s", self._last_error)
            return False
        if result.ok:
            self._last_error = None
            return True
        self._last_error = sanitize_error_message(result.error or str(result.status_code))
        logger.warning("Sync item failed: %s", self._last_error)
        return False

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.cloud_client.is_configured() and self.settings.cloud_token:
                self.sync_once()
            self._stop.wait(self.settings.sync_interval_seconds)
