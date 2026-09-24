from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from campex_node.cameras.manager import CameraManager
from campex_node.cloud.client import CloudClient
from campex_node.core.config import NodeSettings
from campex_node.storage.local_store import LocalStore


logger = logging.getLogger("campex.node.heartbeat")


class HeartbeatService:
    def __init__(
        self,
        *,
        settings: NodeSettings,
        node_id: str,
        cloud_client: CloudClient,
        camera_manager: CameraManager,
        store: LocalStore,
    ) -> None:
        self.settings = settings
        self.node_id = node_id
        self.cloud_client = cloud_client
        self.camera_manager = camera_manager
        self.store = store
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="campex-node-heartbeat",
            daemon=True,
        )

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3)

    def send_once(self) -> dict:
        payload = self.build_payload()
        result = self.cloud_client.send_heartbeat(payload)
        now = datetime.now(timezone.utc).isoformat()
        self.store.set_meta("last_heartbeat_at", now)
        if result.ok:
            self.store.set_meta("last_cloud_ok_at", now)
            logger.info("Heartbeat sent", extra={"status_code": result.status_code})
        else:
            self.store.set_meta("last_cloud_error", result.error or str(result.status_code))
            self.store.enqueue_event("heartbeat", payload)
            logger.warning("Heartbeat not delivered: %s", result.error or result.status_code)
        return payload

    def build_payload(self) -> dict:
        summary = self.camera_manager.summary()
        return {
            "node_id": self.node_id,
            "status": "online",
            "version": self.settings.version,
            "cameras_total": summary["cameras_total"],
            "cameras_online": summary["cameras_online"],
            "queue_size": self.store.outbound_queue_size(),
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            self.send_once()
            self._stop.wait(self.settings.heartbeat_interval_seconds)
