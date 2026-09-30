from __future__ import annotations

import threading

from backend.cameras.frame_buffer import LatestFrameSnapshot, LatestFrameStats
from backend.cameras.health import CameraHealth, CameraStatus

from campex_node.cameras.camera import CameraRuntimeState
from campex_node.core.config import NodeCameraConfig, NodeSettings
from campex_node.workers.camera_worker import CameraWorker


class CameraManager:
    def __init__(self, settings: NodeSettings) -> None:
        self.settings = settings
        self._cameras: dict[str, NodeCameraConfig] = {
            camera.id: camera for camera in settings.cameras
        }
        self._workers: dict[str, CameraWorker] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        for camera in self._cameras.values():
            if camera.enabled:
                self.start_camera(camera.id)

    def apply_configs(self, cameras: list[NodeCameraConfig]) -> None:
        desired = {camera.id: camera for camera in cameras}
        with self._lock:
            existing_ids = set(self._cameras)
        for camera_id in existing_ids - set(desired):
            self.stop_camera(camera_id)
        with self._lock:
            current = dict(self._cameras)
        for camera_id, camera in desired.items():
            previous = current.get(camera_id)
            # Name or vision toggles must not drop the live RTSP session.
            source_changed = previous is None or previous.rtsp_url != camera.rtsp_url
            with self._lock:
                self._cameras[camera_id] = camera
                worker = self._workers.get(camera_id)
            if not camera.enabled:
                self.stop_camera(camera_id)
            elif source_changed:
                self.stop_camera(camera_id)
                self.start_camera(camera_id)
            elif worker is None or not worker.is_alive():
                self.start_camera(camera_id)
            else:
                worker.camera = camera
        with self._lock:
            self._cameras = desired

    def start_camera(self, camera_id: str) -> None:
        with self._lock:
            camera = self._cameras[camera_id]
            existing = self._workers.get(camera_id)
            if existing is not None and existing.is_alive():
                return
            worker = CameraWorker(camera, self.settings)
            self._workers[camera_id] = worker
            worker.start()

    def stop_camera(self, camera_id: str) -> None:
        with self._lock:
            worker = self._workers.pop(camera_id, None)
        if worker is not None:
            worker.stop()

    def stop(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        # Signal every worker first so slow RTSP shutdowns overlap.
        for worker in workers:
            worker.request_stop()
        for worker in workers:
            worker.join()

    def configs(self) -> list[NodeCameraConfig]:
        with self._lock:
            return list(self._cameras.values())

    def states(self) -> list[CameraRuntimeState]:
        with self._lock:
            workers = dict(self._workers)
            cameras = dict(self._cameras)
        states: list[CameraRuntimeState] = []
        for camera_id, camera in cameras.items():
            worker = workers.get(camera_id)
            if worker is None:
                states.append(
                    CameraRuntimeState(
                        id=camera.id,
                        name=camera.name,
                        status=CameraStatus.OFFLINE,
                        last_frame_at=None,
                        last_connected_at=None,
                        reconnect_attempts=0,
                        frames_received=0,
                        consecutive_failures=0,
                    )
                )
            else:
                states.append(worker.state())
        return states

    def latest_frame(self, camera_id: str):
        with self._lock:
            worker = self._workers.get(camera_id)
        if worker is None:
            return None, None
        return worker.latest_frame()

    # The four methods below match backend.cameras.manager.CameraManager so
    # backend.vision.engine.VisionEngine can consume the Node's frame buffers.

    def is_running(self, camera_id: str) -> bool:
        with self._lock:
            worker = self._workers.get(camera_id)
        return worker is not None and worker.is_alive()

    def latest_frame_snapshot(self, camera_id: str) -> LatestFrameSnapshot:
        with self._lock:
            worker = self._workers.get(camera_id)
        if worker is None:
            return LatestFrameSnapshot(
                frame=None,
                frame_at=None,
                frame_id=0,
                frames_received=0,
                frames_replaced=0,
            )
        return worker.latest_snapshot()

    def camera_health(self, camera_id: str) -> CameraHealth:
        with self._lock:
            worker = self._workers.get(camera_id)
        if worker is None:
            return CameraHealth(camera_id=camera_id, status=CameraStatus.OFFLINE)
        return worker.source.health()

    def frame_stats(self, camera_id: str) -> LatestFrameStats:
        with self._lock:
            worker = self._workers.get(camera_id)
        if worker is None:
            return LatestFrameStats(frames_received=0, frames_replaced=0)
        return worker.frame_stats()

    def summary(self) -> dict:
        states = self.states()
        return {
            "cameras_total": len(states),
            "cameras_online": sum(1 for state in states if state.status == CameraStatus.ONLINE),
            "cameras": [state.as_dict() for state in states],
        }
