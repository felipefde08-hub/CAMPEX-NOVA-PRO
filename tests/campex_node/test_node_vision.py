from __future__ import annotations

import logging
import time

import numpy as np

from backend.cameras.base import FrameResult
from backend.cameras.health import CameraHealth, CameraStatus
from backend.config import ROOT_DIR

from campex_node.cameras.manager import CameraManager
from campex_node.cloud.client import CloudClient
from campex_node.cloud.config_sync import ConfigSyncService
from campex_node.core.config import NodeCameraConfig, NodeSettings
from campex_node.core.lifecycle import NodeLifecycle
from campex_node.storage.local_store import LocalStore
from campex_node.vision.service import NodeVisionService
import campex_node.workers.camera_worker as camera_worker_module


class FrameSource:
    def __init__(self, camera_id: str) -> None:
        self._health = CameraHealth(camera_id=camera_id, status=CameraStatus.OFFLINE)

    def connect(self):
        self._health.status = CameraStatus.ONLINE
        return True

    def read(self):
        self._health.frames_received += 1
        return FrameResult(success=True, frame=np.zeros((240, 320, 3), dtype=np.uint8))

    def reconnect(self):
        return self.connect()

    def close(self):
        self._health.status = CameraStatus.OFFLINE

    def health(self):
        return self._health


def _settings(tmp_path, cameras=()) -> NodeSettings:
    return NodeSettings(
        environment="test",
        version="0.1.0",
        log_level="INFO",
        data_dir=tmp_path,
        database_path=tmp_path / "node.sqlite3",
        node_id_file=tmp_path / "node_id",
        cloud_url=None,
        cloud_token=None,
        camera_reconnect_seconds=0.01,
        cameras=tuple(cameras),
    )


def _camera(camera_id: str, **overrides) -> NodeCameraConfig:
    values = {"id": camera_id, "name": camera_id, "rtsp_url": f"rtsp://192.168.1.20/{camera_id}"}
    values.update(overrides)
    return NodeCameraConfig(**values)


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class FakeEngine:
    def __init__(self, settings, camera_manager) -> None:
        self.settings = settings
        self.camera_manager = camera_manager
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.shut_down = False
        self.status_payload: dict = {}

    def start_session(self, camera_id):
        self.started.append(camera_id)
        return {"camera_id": camera_id, "status": "STARTING"}

    def stop_session(self, camera_id):
        self.stopped.append(camera_id)
        return {"camera_id": camera_id, "status": "STOPPED"}

    def status(self, camera_id):
        return dict(self.status_payload, camera_id=camera_id)

    def shutdown(self):
        self.shut_down = True


class FakeCameraManager:
    def __init__(self, cameras) -> None:
        self.cameras = list(cameras)

    def configs(self):
        return list(self.cameras)


def test_node_camera_manager_exposes_backend_frame_interface(monkeypatch, tmp_path):
    monkeypatch.setattr(
        camera_worker_module,
        "create_rtsp_source",
        lambda camera, settings=None: FrameSource(camera.id),
    )
    manager = CameraManager(_settings(tmp_path, [_camera("cam_1")]))
    manager.start()
    try:
        assert _wait_for(lambda: manager.frame_stats("cam_1").frames_received > 0)
        snapshot = manager.latest_frame_snapshot("cam_1")
        assert manager.is_running("cam_1") is True
        assert snapshot.frame is not None
        assert snapshot.frame_at is not None
        assert snapshot.frame_id > 0
        assert manager.camera_health("cam_1").status == CameraStatus.ONLINE
    finally:
        manager.stop()

    assert manager.is_running("missing") is False
    assert manager.latest_frame_snapshot("missing").frame is None
    assert manager.camera_health("missing").status == CameraStatus.OFFLINE
    assert manager.frame_stats("missing").frames_received == 0


def test_real_vision_engine_processes_node_frames(monkeypatch, tmp_path):
    monkeypatch.setenv("VISION_ENABLED", "true")
    monkeypatch.setenv("VISION_DETECTOR", "hog")
    monkeypatch.setattr(
        camera_worker_module,
        "create_rtsp_source",
        lambda camera, settings=None: FrameSource(camera.id),
    )
    manager = CameraManager(_settings(tmp_path, [_camera("cam_1")]))
    vision = NodeVisionService(_settings(tmp_path), manager, poll_seconds=0.1)
    vision.initialize()
    manager.start()
    vision.start()
    try:
        assert vision.active_camera_ids == {"cam_1"}

        def processed() -> bool:
            status = vision.engine.status("cam_1")
            metrics = status.get("metrics") or {}
            return status["status"] == "RUNNING" and metrics.get("frames_processed", 0) > 0

        assert _wait_for(processed), vision.engine.status("cam_1")
    finally:
        vision.stop()
        manager.stop()
    assert vision.engine is None


def test_vision_follows_camera_configs_and_vision_enabled(tmp_path):
    cameras = FakeCameraManager(
        [
            _camera("cam_default"),
            _camera("cam_vision_on", vision_enabled=True),
            _camera("cam_vision_off", vision_enabled=False),
            _camera("cam_disabled", enabled=False),
        ]
    )
    vision = NodeVisionService(_settings(tmp_path), cameras, engine_factory=FakeEngine)
    vision.start()
    try:
        engine = vision.engine
        assert sorted(engine.started) == ["cam_default", "cam_vision_on"]

        cameras.cameras = [_camera("cam_vision_on", vision_enabled=True), _camera("cam_new")]
        started, stopped = vision.reconcile()

        assert started == {"cam_new"}
        assert stopped == {"cam_default"}
        assert engine.stopped == ["cam_default"]
        assert vision.active_camera_ids == {"cam_vision_on", "cam_new"}
    finally:
        vision.stop()
    assert engine.shut_down is True
    assert vision.active_camera_ids == set()


def test_vision_does_not_start_when_disabled_by_env(monkeypatch, tmp_path):
    monkeypatch.setenv("VISION_ENABLED", "false")
    vision = NodeVisionService(
        _settings(tmp_path), FakeCameraManager([_camera("cam_1")]), engine_factory=FakeEngine
    )

    vision.start()

    assert vision.engine is None
    vision.stop()


def test_vision_model_path_is_absolute_regardless_of_start_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VISION_MODEL", "yolo11n.pt")
    relative = NodeVisionService(_settings(tmp_path)).vision_settings.vision_model
    monkeypatch.setenv("VISION_MODEL", str(tmp_path / "custom.pt"))
    absolute = NodeVisionService(_settings(tmp_path)).vision_settings.vision_model

    assert relative == str((ROOT_DIR / "yolo11n.pt").resolve())
    assert absolute == str((tmp_path / "custom.pt").resolve())


def test_node_logs_visible_warning_when_detector_falls_back(caplog, tmp_path):
    vision = NodeVisionService(
        _settings(tmp_path), FakeCameraManager([_camera("cam_1")]), engine_factory=FakeEngine
    )
    vision.start()
    try:
        vision.engine.status_payload = {
            "detector_fallback": True,
            "detector_fallback_reason": "YOLO unavailable: missing file",
            "components": {"detector": {"state": "DEGRADED", "name": "OpenCV HOG Person Detector"}},
        }
        with caplog.at_level(logging.WARNING, logger="campex.node.vision"):
            vision._report_detector_once()
            vision._report_detector_once()
    finally:
        vision.stop()

    warnings = [record for record in caplog.records if "FALLBACK" in record.getMessage()]
    assert len(warnings) == 1
    assert "YOLO unavailable: missing file" in warnings[0].getMessage()


def test_camera_config_vision_enabled_is_optional():
    base = {"id": "cam_1", "name": "Doca", "rtsp_url": "rtsp://192.168.1.20/s"}

    assert NodeCameraConfig.from_mapping(base).vision_enabled is None
    assert NodeCameraConfig.from_mapping(base | {"vision_enabled": False}).vision_enabled is False
    assert NodeCameraConfig.from_mapping(base | {"vision_enabled": 1}).vision_enabled is True


class RecordingVision:
    def __init__(self, fail_start: bool = False) -> None:
        self.calls: list[str] = []
        self.fail_start = fail_start

    def initialize(self):
        self.calls.append("initialize")

    def start(self):
        self.calls.append("start")
        if self.fail_start:
            raise RuntimeError("model exploded")

    def stop(self):
        self.calls.append("stop")

    def apply_zones(self, zones):
        self.calls.append("apply_zones")


def _lifecycle(tmp_path, vision) -> NodeLifecycle:
    settings = _settings(tmp_path)
    return NodeLifecycle(
        settings=settings,
        store=LocalStore(settings.database_path),
        cloud_client=CloudClient(settings),
        camera_manager=CameraManager(settings),
        vision=vision,
    )


def test_lifecycle_initializes_starts_wires_zones_and_stops_vision(tmp_path):
    vision = RecordingVision()
    lifecycle = _lifecycle(tmp_path, vision)

    lifecycle.initialize()
    lifecycle.start()
    try:
        assert isinstance(lifecycle.config_sync, ConfigSyncService)
        assert lifecycle.config_sync.on_zones == vision.apply_zones
    finally:
        lifecycle.stop()

    assert vision.calls == ["initialize", "start", "stop"]


def test_lifecycle_keeps_running_when_vision_fails_to_start(tmp_path):
    vision = RecordingVision(fail_start=True)
    lifecycle = _lifecycle(tmp_path, vision)

    lifecycle.initialize()
    lifecycle.start()
    try:
        assert lifecycle.heartbeat is not None
        assert lifecycle.sync is not None
    finally:
        lifecycle.stop()
