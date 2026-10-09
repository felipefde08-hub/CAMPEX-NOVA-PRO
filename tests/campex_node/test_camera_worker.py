from __future__ import annotations

import time

from backend.cameras.base import FrameResult
from backend.cameras.health import CameraHealth, CameraStatus

from campex_node.core.config import NodeCameraConfig, NodeSettings
from campex_node.workers.camera_worker import CameraWorker


class FailingSource:
    def __init__(self):
        self.closed = False
        self.reconnects = 0
        self._health = CameraHealth(camera_id="cam_fail", status=CameraStatus.OFFLINE)

    def connect(self):
        self._health.status = CameraStatus.ONLINE
        self._health.connection_state = CameraStatus.ONLINE.value
        return True

    def read(self):
        return FrameResult(success=False, error="rtsp://user:pass@192.168.1.10/stream failed")

    def reconnect(self):
        self.reconnects += 1
        self._health.reconnect_attempts += 1
        self._health.status = CameraStatus.OFFLINE
        return False

    def close(self):
        self.closed = True
        self._health.status = CameraStatus.OFFLINE

    def health(self):
        return self._health


def make_settings(tmp_path):
    return NodeSettings(
        environment="test",
        version="0.1.0",
        log_level="INFO",
        data_dir=tmp_path,
        database_path=tmp_path / "node.sqlite3",
        node_id_file=tmp_path / "node_id",
        cloud_url=None,
        cloud_token=None,
        cloud_timeout_seconds=1,
        heartbeat_interval_seconds=1,
        camera_reconnect_seconds=0.01,
        camera_read_failure_limit=1,
        camera_open_timeout_ms=100,
        camera_read_timeout_ms=100,
        cameras=(),
    )


def test_camera_worker_reconnects_without_exposing_rtsp_credentials(tmp_path):
    source = FailingSource()
    worker = CameraWorker(
        NodeCameraConfig(
            id="cam_fail",
            name="Camera fail",
            rtsp_url="rtsp://user:pass@192.168.1.10/stream",
        ),
        make_settings(tmp_path),
        source=source,
    )

    worker.start()
    deadline = time.monotonic() + 1
    while source.reconnects == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    worker.stop()

    assert source.closed is True
    assert source.reconnects >= 1
    assert worker.state().last_error in {None, "rtsp://***:***@192.168.1.10/stream failed"}


class CountingWorker:
    instances = []

    def __init__(self, camera, settings, source_url=None):
        self.camera = camera
        self.source_url = source_url
        self.stopped = False
        CountingWorker.instances.append(self)

    def start(self):
        pass

    def is_alive(self):
        return not self.stopped

    def request_stop(self):
        self.stopped = True

    def join(self):
        pass

    def stop(self):
        self.stopped = True


def test_camera_manager_keeps_stream_when_only_vision_changes(tmp_path, monkeypatch):
    from dataclasses import replace

    from campex_node.cameras import manager as manager_module

    CountingWorker.instances = []
    monkeypatch.setattr(manager_module, "CameraWorker", CountingWorker)
    camera = NodeCameraConfig(id="cam_1", name="Cam", rtsp_url="rtsp://host/a")
    manager = manager_module.CameraManager(replace(make_settings(tmp_path), cameras=(camera,)))
    manager.start()

    manager.apply_configs([replace(camera, vision_enabled=True, name="Cam renamed")])
    assert len(CountingWorker.instances) == 1
    assert CountingWorker.instances[0].camera.name == "Cam renamed"

    manager.apply_configs([replace(camera, rtsp_url="rtsp://host/b")])
    assert len(CountingWorker.instances) == 2
    assert CountingWorker.instances[0].stopped is True
