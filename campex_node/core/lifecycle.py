from __future__ import annotations

import logging
import signal
import threading
import uuid

from campex_node.cameras.manager import CameraManager
from campex_node.cloud.client import CloudClient
from campex_node.cloud.config_sync import ConfigSyncService
from campex_node.cloud.heartbeat import HeartbeatService
from campex_node.cloud.sync import SyncService
from campex_node.core.config import NodeSettings
from campex_node.storage.local_store import LocalStore


logger = logging.getLogger("campex.node.lifecycle")


class NodeLifecycle:
    def __init__(
        self,
        *,
        settings: NodeSettings,
        store: LocalStore,
        cloud_client: CloudClient,
        camera_manager: CameraManager,
    ) -> None:
        self.settings = settings
        self.store = store
        self.cloud_client = cloud_client
        self.camera_manager = camera_manager
        self.node_id = ""
        self.heartbeat: HeartbeatService | None = None
        self.config_sync: ConfigSyncService | None = None
        self.sync: SyncService | None = None
        self._running = False

    def initialize(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.store.initialize()
        self.node_id = self._get_or_create_node_id()
        cloud_status = self.cloud_client.check_connection()
        if cloud_status.ok:
            logger.info("CAMPEX Cloud connection verified")
        else:
            logger.warning("CAMPEX Cloud unavailable or not configured: %s", cloud_status.error or cloud_status.status_code)
        logger.info(
            "Loaded %s camera(s) for CAMPEX Node %s",
            len(self.settings.cameras),
            self.node_id,
        )

    def start(self) -> None:
        if self._running:
            return
        self.config_sync = ConfigSyncService(
            settings=self.settings,
            cloud_client=self.cloud_client,
            camera_manager=self.camera_manager,
        )
        if self.cloud_client.is_configured():
            self.config_sync.sync_once()
        self.camera_manager.start()
        if self.cloud_client.is_configured():
            self.config_sync.start()
        self.sync = SyncService(
            settings=self.settings,
            cloud_client=self.cloud_client,
            store=self.store,
        )
        if self.cloud_client.is_configured():
            self.sync.start()
        self.heartbeat = HeartbeatService(
            settings=self.settings,
            node_id=self.node_id,
            cloud_client=self.cloud_client,
            camera_manager=self.camera_manager,
            store=self.store,
        )
        self.heartbeat.start()
        self._running = True
        logger.info("CAMPEX Node started")

    def run_forever(self) -> None:
        stop_requested = threading.Event()

        def request_stop(signum, frame) -> None:
            stop_requested.set()

        previous_handlers = []
        for signal_name in ("SIGINT", "SIGTERM"):
            signum = getattr(signal, signal_name, None)
            if signum is not None:
                previous_handlers.append((signum, signal.signal(signum, request_stop)))
        try:
            while not stop_requested.wait(1):
                pass
        finally:
            for signum, previous_handler in previous_handlers:
                signal.signal(signum, previous_handler)
            self.stop()

    def stop(self) -> None:
        if self.config_sync is not None:
            self.config_sync.stop()
        if self.sync is not None:
            self.sync.stop()
        if self.heartbeat is not None:
            self.heartbeat.stop()
        self.camera_manager.stop()
        self._running = False
        logger.info("CAMPEX Node stopped")

    def run_once(self) -> dict:
        if self.cloud_client.is_configured():
            ConfigSyncService(
                settings=self.settings,
                cloud_client=self.cloud_client,
                camera_manager=self.camera_manager,
            ).sync_once()
        self.camera_manager.start()
        try:
            heartbeat = HeartbeatService(
                settings=self.settings,
                node_id=self.node_id,
                cloud_client=self.cloud_client,
                camera_manager=self.camera_manager,
                store=self.store,
            )
            return heartbeat.send_once()
        finally:
            self.camera_manager.stop()

    def _get_or_create_node_id(self) -> str:
        if self.settings.node_id:
            self.store.set_meta("node_id", self.settings.node_id)
            return self.settings.node_id
        existing = self.store.get_meta("node_id")
        if existing:
            return existing
        node_id = f"node_{uuid.uuid4().hex}"
        self.store.set_meta("node_id", node_id)
        try:
            self.settings.node_id_file.parent.mkdir(parents=True, exist_ok=True)
            self.settings.node_id_file.write_text(node_id, encoding="utf-8")
        except OSError:
            logger.debug("Could not write node_id file", exc_info=True)
        return node_id
