from __future__ import annotations

import logging
import json
import threading
from typing import Any, Callable

from backend.cameras.security import sanitize_error_message

from campex_node.cameras.manager import CameraManager
from campex_node.cloud.client import CloudClient
from campex_node.core.config import NodeCameraConfig, NodeSettings
from campex_node.storage.local_store import LocalStore


logger = logging.getLogger("campex.node.config_sync")


class ConfigSyncService:
    def __init__(
        self,
        *,
        settings: NodeSettings,
        cloud_client: CloudClient,
        camera_manager: CameraManager,
        store: LocalStore | None = None,
        on_zones: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> None:
        self.settings = settings
        self.cloud_client = cloud_client
        self.camera_manager = camera_manager
        self.store = store
        self.on_zones = on_zones
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="campex-node-config-sync",
            daemon=True,
        )
        self.last_error: str | None = None
        self.last_count = 0

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3)

    def sync_once(self) -> list[NodeCameraConfig]:
        result = self.cloud_client.fetch_config()
        if not result.ok:
            self.last_error = sanitize_error_message(result.error or str(result.status_code))
            logger.warning("Config sync failed: %s", self.last_error)
            return []
        payload = result.data or {}
        remote_cameras = [
            NodeCameraConfig.from_mapping(
                {
                    "id": item["id"],
                    "name": item["name"],
                    "rtsp_url": item["source_uri"],
                    "enabled": item.get("enabled", True),
                    "vision_enabled": item.get("vision_enabled"),
                }
            )
            for item in payload.get("cameras", [])
            if item.get("source_uri")
        ]
        if self.store is not None:
            self.store.set_meta(
                "cloud_cameras_json",
                json.dumps(
                    [
                        {
                            "id": camera.id,
                            "name": camera.name,
                            "rtsp_url": camera.rtsp_url,
                            "enabled": camera.enabled,
                            "vision_enabled": camera.vision_enabled,
                        }
                        for camera in remote_cameras
                    ],
                    separators=(",", ":"),
                ),
            )
        cameras_by_id = {camera.id: camera for camera in self.settings.cameras}
        cameras_by_id.update({camera.id: camera for camera in remote_cameras})
        cameras = list(cameras_by_id.values())
        self.camera_manager.apply_configs(cameras)
        # A cloud without the "zones" key predates zone sync; keep local zones.
        if self.on_zones is not None and "zones" in payload:
            try:
                self.on_zones(list(payload.get("zones") or []))
            except Exception as exc:
                self.last_error = sanitize_error_message(f"Zone sync failed: {exc}")
                logger.exception("Zone sync failed")
                return cameras
        self.last_error = None
        self.last_count = len(cameras)
        logger.info("Config sync applied %s camera(s)", len(cameras))
        return cameras

    def _run(self) -> None:
        while not self._stop.is_set():
            self.sync_once()
            self._stop.wait(self.settings.config_sync_interval_seconds)
