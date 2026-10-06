from __future__ import annotations

import logging
import json
import threading

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
    ) -> None:
        self.settings = settings
        self.cloud_client = cloud_client
        self.camera_manager = camera_manager
        self.store = store
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="campex-node-config-sync",
            daemon=True,
        )
        self.last_error: str | None = None
        self.last_count = 0
        # Every id the Cloud has sent; such a camera only runs while the Cloud
        # still lists it, so a camera deleted there stops here too.
        self._cloud_ids: set[str] = (
            {camera.id for camera in store.get_cached_cloud_cameras()} if store is not None else set()
        )

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
                    "vision_enabled": item.get("vision_enabled", False),
                    "mapping_enabled": item.get("mapping_enabled", False),
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
                            "mapping_enabled": camera.mapping_enabled,
                        }
                        for camera in remote_cameras
                    ],
                    separators=(",", ":"),
                ),
            )
        self._cloud_ids.update(camera.id for camera in remote_cameras)
        cameras_by_id = {
            camera.id: camera for camera in self.settings.cameras if camera.id not in self._cloud_ids
        }
        cameras_by_id.update({camera.id: camera for camera in remote_cameras})
        if self.store is not None:
            # The Cloud copy of its cameras always wins: a copy saved on this
            # Node would keep an old RTSP URL or toggle forever.
            for camera in self.store.get_local_cameras():
                if camera.id in self._cloud_ids:
                    self.store.delete_local_camera(camera.id)
                else:
                    cameras_by_id[camera.id] = camera
        cameras = list(cameras_by_id.values())
        self.camera_manager.apply_configs(cameras)
        self.last_error = None
        self.last_count = len(cameras)
        logger.info("Config sync applied %s camera(s)", len(cameras))
        return cameras

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync_once()
            except Exception:
                logger.exception("Config sync loop failed")
            self._stop.wait(self.settings.config_sync_interval_seconds)
