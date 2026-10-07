from __future__ import annotations

from campex_node import vision as vision_module
from campex_node.core.config import NodeCameraConfig
from campex_node.vision import EdgeVisionService

from tests.campex_node.test_live_relay import FakeCameraManager, _settings


def test_status_exposes_cloud_shaped_metrics_for_the_panel(tmp_path, monkeypatch):
    camera = NodeCameraConfig(id="cam-1", name="Canal6", rtsp_url="rtsp://camera/1", vision_enabled=True)
    manager = FakeCameraManager([camera])
    service = EdgeVisionService(_settings(tmp_path), manager)
    service._set_state("cam-1", status="RUNNING", detections=[], mapping_status="STOPPED")

    clock = iter([100.0, 102.0])
    monkeypatch.setattr(vision_module.time, "monotonic", lambda: next(clock))
    first = service.status("cam-1")
    manager.states = lambda: [
        state.__class__(**{**state.__dict__, "frames_received": 75})
        for state in FakeCameraManager([camera]).states()
    ]
    second = service.status("cam-1")

    assert first["metrics"]["frames_received"] == 25
    assert first["metrics"]["frames_processed"] == 1
    assert first["metrics"]["camera_fps"] == 0
    assert second["metrics"]["frames_received"] == 75
    assert second["metrics"]["camera_fps"] == 25.0
    assert second["components"]["mapping"] == {"state": "STOPPED", "poses": 0, "error": None}
    # Flat fields stay for the cloud relay, which reads them from vision_json.
    assert second["vision_fps"] == second["metrics"]["vision_fps"]
    assert second["mapping_status"] == "STOPPED"
