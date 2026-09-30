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


class SlowReadSource:
    """Records whether close() ever ran while a read() was in progress."""

    def __init__(self, read_seconds: float):
        import threading

        self.read_seconds = read_seconds
        self._reading = threading.Event()
        self.closed = False
        self.closed_during_read = False
        self._health = CameraHealth(camera_id="cam_slow", status=CameraStatus.ONLINE)

    def connect(self):
        return True

    def read(self):
        self._reading.set()
        time.sleep(self.read_seconds)
        self._reading.clear()
        return FrameResult(success=True, frame=b"frame")

    def reconnect(self):
        return True

    def close(self):
        if self._reading.is_set():
            self.closed_during_read = True
        self.closed = True

    def health(self):
        return self._health

    def wait_until_reading(self):
        assert self._reading.wait(2)


def test_camera_worker_stop_waits_for_read_before_closing_camera(tmp_path):
    source = SlowReadSource(read_seconds=0.3)
    worker = CameraWorker(
        NodeCameraConfig(id="cam_slow", name="Slow", rtsp_url="rtsp://192.168.1.10/s"),
        make_settings(tmp_path),
        source=source,
    )
    worker.start()
    source.wait_until_reading()

    worker.stop()

    assert worker.is_alive() is False
    assert source.closed is True
    assert source.closed_during_read is False


def test_camera_worker_stop_timeout_leaves_close_to_the_worker_thread(monkeypatch, tmp_path):
    source = SlowReadSource(read_seconds=0.5)
    worker = CameraWorker(
        NodeCameraConfig(id="cam_slow", name="Slow", rtsp_url="rtsp://192.168.1.10/s"),
        make_settings(tmp_path),
        source=source,
    )
    monkeypatch.setattr(worker, "_stop_timeout_seconds", lambda: 0.05)
    worker.start()
    source.wait_until_reading()

    worker.stop()

    assert worker.is_alive() is True
    assert source.closed is False
    worker._thread.join(timeout=2)
    assert source.closed is True
    assert source.closed_during_read is False


class CountingWorker:
    instances = []

    def __init__(self, camera, settings):
        self.camera = camera
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
