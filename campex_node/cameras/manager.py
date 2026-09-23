from __future__ import annotations

import threading

from backend.cameras.health import CameraStatus

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
            changed = previous != camera
            with self._lock:
                self._cameras[camera_id] = camera
            if not camera.enabled:
                self.stop_camera(camera_id)
            elif changed:
                self.stop_camera(camera_id)
                self.start_camera(camera_id)
            elif camera_id not in self._workers:
                self.start_camera(camera_id)
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
            camera_ids = list(self._workers)
        for camera_id in camera_ids:
            self.stop_camera(camera_id)

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

    def summary(self) -> dict:
        states = self.states()
        return {
            "cameras_total": len(states),
            "cameras_online": sum(1 for state in states if state.status == CameraStatus.ONLINE),
            "cameras": [state.as_dict() for state in states],
        }
