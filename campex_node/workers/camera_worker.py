from __future__ import annotations

import logging
import threading
from datetime import datetime

from backend.cameras.base import CameraSource
from backend.cameras.frame_buffer import LatestFrameBuffer
from backend.cameras.health import CameraStatus, utc_now
from backend.cameras.security import sanitize_error_message

from campex_node.cameras.camera import CameraRuntimeState
from campex_node.cameras.capture import create_rtsp_source
from campex_node.core.config import NodeCameraConfig, NodeSettings


logger = logging.getLogger("campex.node.camera_worker")


class CameraWorker:
    def __init__(
        self,
        camera: NodeCameraConfig,
        settings: NodeSettings,
        source: CameraSource | None = None,
    ) -> None:
        self.camera = camera
        self.settings = settings
        self.source = source or create_rtsp_source(camera)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"campex-node-camera-{camera.id}",
            daemon=True,
        )
        self._frame_buffer = LatestFrameBuffer()
        self._last_frame_at: datetime | None = None

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.source.close()
        if self._thread.is_alive():
            self._thread.join(timeout=3)
        if self._thread.is_alive():
            logger.warning("Camera worker did not stop before timeout", extra={"camera_id": self.camera.id})

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def state(self) -> CameraRuntimeState:
        health = self.source.health()
        return CameraRuntimeState(
            id=self.camera.id,
            name=self.camera.name,
            status=health.status,
            last_frame_at=health.last_successful_frame or self._last_frame_at,
            last_connected_at=health.last_connected_at,
            reconnect_attempts=health.reconnect_attempts,
            frames_received=health.frames_received,
            consecutive_failures=health.consecutive_failures,
            last_error=sanitize_error_message(health.last_error),
        )

    def latest_frame(self):
        return self._frame_buffer.latest()

    def _run(self) -> None:
        consecutive_failures = 0
        while not self._stop.is_set():
            try:
                if self.source.health().status == CameraStatus.OFFLINE:
                    if not self.source.connect():
                        self._stop.wait(self.settings.camera_reconnect_seconds)
                        continue

                result = self.source.read()
                if result.success:
                    frame_at = utc_now()
                    self._last_frame_at = frame_at
                    self._frame_buffer.put(result.frame, frame_at)
                    consecutive_failures = 0
                    self._stop.wait(0.02)
                    continue

                consecutive_failures += 1
                health = self.source.health()
                health.consecutive_failures = consecutive_failures
                if consecutive_failures >= self.settings.camera_read_failure_limit:
                    health.status = CameraStatus.DEGRADED
                    health.connection_state = CameraStatus.DEGRADED.value
                    logger.warning(
                        "[camera:%s] frame_read_failed: %s",
                        self.camera.id,
                        sanitize_error_message(result.error),
                        extra={"camera_id": self.camera.id},
                    )
                    try:
                        self.source.reconnect()
                    except Exception as exc:
                        health.status = CameraStatus.OFFLINE
                        health.connection_state = CameraStatus.OFFLINE.value
                        health.last_error = sanitize_error_message(str(exc))
                        logger.exception("Camera reconnect failed", extra={"camera_id": self.camera.id})
                    consecutive_failures = 0
                    self._stop.wait(self.settings.camera_reconnect_seconds)
                else:
                    self._stop.wait(0.2)
            except Exception as exc:
                health = self.source.health()
                health.status = CameraStatus.OFFLINE
                health.connection_state = CameraStatus.OFFLINE.value
                health.last_error = sanitize_error_message(str(exc))
                logger.exception("Camera worker loop failed", extra={"camera_id": self.camera.id})
                self._stop.wait(self.settings.camera_reconnect_seconds)
        self.source.close()
