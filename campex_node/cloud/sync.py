from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from backend.cameras.security import sanitize_error_message

from campex_node.cloud.client import CloudClient
from campex_node.cloud.outbox import CloudOutbox
from campex_node.core.config import NodeSettings
from campex_node.storage.local_store import LocalStore


logger = logging.getLogger("campex.node.sync")

# Outbox item type -> field of the Cloud's batch. Heartbeats go on their own.
BATCH_FIELDS = {
    "event": "events",
    "metric": "metrics",
    "interval": "intervals",
    "count": "counts",
    "factory": "factory",
}
BATCH_LIMIT = 200


class SyncService:
    def __init__(
        self,
        *,
        settings: NodeSettings,
        cloud_client: CloudClient,
        store: LocalStore,
        outbox: CloudOutbox | None = None,
    ) -> None:
        self.settings = settings
        self.cloud_client = cloud_client
        self.store = store
        self.outbox = outbox
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

    def sync_once(self, limit: int = BATCH_LIMIT) -> dict:
        items = self.store.pending_outbound(limit=limit)
        sent = failed = 0
        batch = [item for item in items if item["type"] in BATCH_FIELDS]
        if batch:
            if self._send_batch(batch):
                sent += len(batch)
            else:
                failed += len(batch)
        for item in items:
            if item["type"] in BATCH_FIELDS:
                continue
            if self._send_item(item):
                self._delivered(item)
                sent += 1
            else:
                self._failed(item)
                failed += 1
        return {"sent": sent, "failed": failed, "pending": self.store.outbound_queue_size()}

    _last_error: str | None = None

    def _send_batch(self, items: list[dict]) -> bool:
        payload: dict[str, list] = {field: [] for field in BATCH_FIELDS.values()}
        for item in items:
            payload[BATCH_FIELDS[item["type"]]].append(item["payload"])
        result = self.cloud_client.send_factory_batch(payload)
        if not result.ok:
            self._last_error = sanitize_error_message(result.error or str(result.status_code))
            logger.warning("Sync batch of %s item(s) failed: %s", len(items), self._last_error)
            for item in items:
                self._failed(item)
            return False
        self._last_error = None
        for item in items:
            self._delivered(item)
        return True

    def _delivered(self, item: dict) -> None:
        # A newer state queued while this one was in flight stays pending.
        self.store.mark_outbound_synced(item["id"], item.get("version"))
        self.store.set_meta("last_sync_at", datetime.now(timezone.utc).isoformat())

    def _failed(self, item: dict) -> None:
        attempts = item["attempts"] + 1
        self.store.mark_outbound_failed(
            item["id"],
            self._last_error or "Sync failed.",
            retry_seconds=min(300, 2 ** min(attempts, 8)),
        )

    def _send_item(self, item: dict) -> bool:
        payload_type = item["type"]
        payload = item["payload"]
        if payload_type == "heartbeat":
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
                try:
                    if self.outbox is not None:
                        self.outbox.refresh_factory()
                    self.sync_once()
                except Exception:
                    logger.exception("Outbound sync failed")
            self._stop.wait(self.settings.sync_interval_seconds)
