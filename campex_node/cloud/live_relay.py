from __future__ import annotations

import base64
import logging
import threading
import time
from typing import Any

import cv2

from campex_node.cameras.capture import test_rtsp_connection
from campex_node.cameras.manager import CameraManager
from campex_node.cloud.client import CloudClient
from campex_node.core.config import NodeSettings


logger = logging.getLogger("campex.node.live_relay")

# Cameras created in this Node's own panel are not known to the Cloud.
LOCAL_CAMERA_PREFIX = "local_"
MIN_INTERVAL_SECONDS = 0.5
MAX_INTERVAL_SECONDS = 30.0
FAILURE_RETRY_SECONDS = 10.0


class LiveRelayService:
    """Sends camera status, Vision results and live frames to the CAMPEX Cloud.

    Frames are only uploaded for cameras the Cloud reports as being watched;
    the Cloud answer also carries the dashboard toggles (Vision, mapping) and
    connection tests to run on this Node's network.
    """

    def __init__(
        self,
        *,
        settings: NodeSettings,
        cloud_client: CloudClient,
        camera_manager: CameraManager,
        vision=None,
        config_sync=None,
    ) -> None:
        self.settings = settings
        self.cloud_client = cloud_client
        self.camera_manager = camera_manager
        self.vision = vision
        self.config_sync = config_sync
        self._stop = threading.Event()
        # Set to upload right away (e.g. a connection test just finished).
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, name="campex-node-live-relay", daemon=True)
        self._lock = threading.Lock()
        self._live_cameras: set[str] = set()
        self._last_uploaded_frame: dict[str, Any] = {}
        self._fps_samples: dict[str, tuple[float, int]] = {}
        self._test_results: list[dict[str, Any]] = []
        self._running_tests: set[str] = set()
        self.last_error: str | None = None

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                interval = self.relay_once()
            except Exception:
                logger.exception("Live relay loop failed")
                interval = FAILURE_RETRY_SECONDS
            self._wake.wait(interval)
            self._wake.clear()

    def relay_once(self) -> float:
        with self._lock:
            test_results, self._test_results = self._test_results, []
        payload = {"cameras": self._camera_reports(), "test_results": test_results}
        result = self.cloud_client.send_live_state(payload)
        if not result.ok:
            self.last_error = result.error or str(result.status_code)
            with self._lock:
                # Keep test results for the next attempt.
                self._test_results = test_results + self._test_results
            logger.warning("Live relay not delivered: %s", self.last_error)
            return FAILURE_RETRY_SECONDS
        self.last_error = None
        data = result.data or {}
        self._apply_cloud_flags(data.get("cameras") or {})
        for job in data.get("test_jobs") or []:
            self._start_test(job)
        try:
            interval = float(data.get("next_upload_seconds") or MAX_INTERVAL_SECONDS)
        except (TypeError, ValueError):
            interval = MAX_INTERVAL_SECONDS
        return max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, interval))

    def _camera_reports(self) -> list[dict[str, Any]]:
        states = {state.id: state for state in self.camera_manager.states()}
        with self._lock:
            live = set(self._live_cameras)
        reports = []
        for camera in self.camera_manager.configs():
            if camera.id.startswith(LOCAL_CAMERA_PREFIX):
                continue
            state = states.get(camera.id)
            report: dict[str, Any] = {
                "camera_id": camera.id,
                "status": state.status.value if state else "OFFLINE",
                "last_error": state.last_error if state else None,
                "frames_received": state.frames_received if state else 0,
                "approximate_fps": self._fps(camera.id, state.frames_received if state else 0),
                "last_frame_at": state.last_frame_at.isoformat() if state and state.last_frame_at else None,
            }
            if self.vision is not None:
                report["vision"] = self.vision.status(camera.id)
                report["objects"] = self.vision.cloud_objects(camera.id)
                report["poses"] = self.vision.cloud_poses(camera.id)
            if camera.id in live:
                report.update(self._encoded_frame(camera.id))
            reports.append(report)
        return reports

    def _fps(self, camera_id: str, frames_received: int) -> float | None:
        now = time.monotonic()
        previous = self._fps_samples.get(camera_id)
        self._fps_samples[camera_id] = (now, frames_received)
        if previous is None or now <= previous[0] or frames_received < previous[1]:
            return None
        return round((frames_received - previous[1]) / (now - previous[0]), 1)

    def _encoded_frame(self, camera_id: str) -> dict[str, Any]:
        frame, frame_at = self.camera_manager.latest_frame(camera_id)
        if frame is None or self._last_uploaded_frame.get(camera_id) == frame_at:
            return {}
        height, width = frame.shape[:2]
        if width > self.settings.live_frame_max_width:
            scale = self.settings.live_frame_max_width / width
            frame = cv2.resize(frame, (self.settings.live_frame_max_width, max(1, int(height * scale))), interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.settings.live_frame_jpeg_quality])
        if not ok:
            return {}
        self._last_uploaded_frame[camera_id] = frame_at
        # Boxes and keypoints are in source-frame pixels; report the source size.
        return {
            "width": width,
            "height": height,
            "frame_jpeg_base64": base64.b64encode(encoded.tobytes()).decode("ascii"),
        }

    def _apply_cloud_flags(self, cameras: dict[str, dict[str, Any]]) -> None:
        with self._lock:
            self._live_cameras = {camera_id for camera_id, flags in cameras.items() if flags.get("live")}
        current = {camera.id: camera for camera in self.camera_manager.configs()}
        changed = any(
            camera_id not in current
            or current[camera_id].vision_enabled != bool(flags.get("vision_enabled"))
            or current[camera_id].mapping_enabled != bool(flags.get("mapping_enabled"))
            for camera_id, flags in cameras.items()
            if flags.get("enabled", True)
        )
        if changed and self.config_sync is not None:
            # Apply dashboard toggles now instead of waiting for the next config poll.
            try:
                self.config_sync.sync_once()
            except Exception:
                logger.exception("Config sync after live relay failed")

    def _start_test(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("id") or "")
        source_uri = str(job.get("source_uri") or "")
        if not job_id or job_id in self._running_tests:
            return
        self._running_tests.add(job_id)
        threading.Thread(
            target=self._run_test,
            args=(job_id, source_uri),
            name=f"campex-node-camera-test-{job_id[-8:]}",
            daemon=True,
        ).start()

    def _run_test(self, job_id: str, source_uri: str) -> None:
        try:
            if not source_uri.lower().startswith("rtsp://"):
                result = {"ok": False, "success": False, "status": "OFFLINE", "error": "Somente URLs rtsp:// podem ser testadas pelo Node."}
            else:
                result = test_rtsp_connection(source_uri, settings=self.settings)
        except Exception as exc:
            result = {"ok": False, "success": False, "status": "OFFLINE", "error": str(exc)}
        finally:
            self._running_tests.discard(job_id)
        with self._lock:
            self._test_results.append({"job_id": job_id, "result": result})
        self._wake.set()
