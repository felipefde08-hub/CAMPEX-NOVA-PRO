from __future__ import annotations

import base64
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np

from backend.cameras.health import CameraStatus
from campex_node.cameras.camera import CameraRuntimeState
from campex_node.cloud import live_relay
from campex_node.cloud.client import CloudResult
from campex_node.cloud.config_sync import ConfigSyncService
from campex_node.cloud.live_relay import LiveRelayService
from campex_node.core.config import NodeCameraConfig, NodeSettings


def _settings(tmp_path):
    return NodeSettings(
        environment="test",
        version="0.1.0",
        log_level="INFO",
        data_dir=tmp_path,
        database_path=tmp_path / "node.sqlite3",
        node_id_file=tmp_path / "node_id",
        cloud_url="https://cloud.example/api/v1",
        cloud_token="node-token",
    )


class FakeCameraManager:
    def __init__(self, cameras):
        self.cameras = list(cameras)
        self.frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        self.frame_at = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

    def configs(self):
        return list(self.cameras)

    def apply_configs(self, cameras):
        self.cameras = list(cameras)

    def states(self):
        return [
            CameraRuntimeState(
                id=camera.id,
                name=camera.name,
                status=CameraStatus.ONLINE,
                last_frame_at=self.frame_at,
                last_connected_at=self.frame_at,
                reconnect_attempts=0,
                frames_received=25,
                consecutive_failures=0,
            )
            for camera in self.cameras
        ]

    def latest_frame(self, camera_id):
        return self.frame, self.frame_at


class FakeCloud:
    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads = []

    def send_live_state(self, payload):
        self.payloads.append(payload)
        return CloudResult(ok=True, status_code=200, data=self.responses.pop(0))


class FakeVision:
    def status(self, camera_id):
        return {"status": "RUNNING", "mapping_status": "STOPPED"}

    def cloud_objects(self, camera_id):
        return [{"track_id": 1, "class_name": "person", "confidence": 0.9, "bounding_box": [1, 2, 3, 4]}]

    def cloud_poses(self, camera_id):
        return []


CLOUD_CAMERA = NodeCameraConfig(id="cam_cloud", name="Doca", rtsp_url="rtsp://10.0.0.5/stream")
LOCAL_CAMERA = NodeCameraConfig(id="local_abc", name="Local", rtsp_url="rtsp://10.0.0.6/stream")


def test_uploads_frames_only_while_the_cloud_reports_a_viewer(tmp_path):
    manager = FakeCameraManager([CLOUD_CAMERA, LOCAL_CAMERA])
    cloud = FakeCloud(
        [
            {"next_upload_seconds": 1.0, "cameras": {"cam_cloud": {"enabled": True, "live": True}}},
            {"next_upload_seconds": 10.0, "cameras": {"cam_cloud": {"enabled": True, "live": False}}},
            {"next_upload_seconds": 10.0, "cameras": {}},
        ]
    )
    relay = LiveRelayService(settings=_settings(tmp_path), cloud_client=cloud, camera_manager=manager, vision=FakeVision())

    assert relay.relay_once() == 1.0
    first = cloud.payloads[0]["cameras"]
    assert [report["camera_id"] for report in first] == ["cam_cloud"]
    assert "frame_jpeg_base64" not in first[0]
    assert first[0]["status"] == "ONLINE"
    assert first[0]["objects"][0]["class_name"] == "person"

    assert relay.relay_once() == 10.0
    live_report = cloud.payloads[1]["cameras"][0]
    assert base64.b64decode(live_report["frame_jpeg_base64"]).startswith(b"\xff\xd8")
    # Detections are in source pixels, so the source size is reported.
    assert (live_report["width"], live_report["height"]) == (1920, 1080)

    relay.relay_once()
    assert "frame_jpeg_base64" not in cloud.payloads[2]["cameras"][0]


def test_dashboard_toggle_triggers_an_immediate_config_sync(tmp_path):
    manager = FakeCameraManager([CLOUD_CAMERA])
    cloud = FakeCloud(
        [{"next_upload_seconds": 10.0, "cameras": {"cam_cloud": {"enabled": True, "vision_enabled": True}}}]
    )
    config_sync = SimpleNamespace(calls=0)
    config_sync.sync_once = lambda: setattr(config_sync, "calls", config_sync.calls + 1)
    relay = LiveRelayService(
        settings=_settings(tmp_path), cloud_client=cloud, camera_manager=manager, config_sync=config_sync
    )

    relay.relay_once()

    assert config_sync.calls == 1


def test_connection_tests_requested_by_the_cloud_run_on_the_node(tmp_path, monkeypatch):
    calls = []

    def fake_test(source_uri, *, settings=None, timeout_seconds=12.0):
        calls.append(source_uri)
        return {"ok": True, "success": True, "status": "ONLINE"}

    monkeypatch.setattr(live_relay, "test_rtsp_connection", fake_test)
    manager = FakeCameraManager([])
    cloud = FakeCloud(
        [
            {"next_upload_seconds": 10.0, "cameras": {}, "test_jobs": [{"id": "ctj_1", "source_uri": "rtsp://10.0.0.9/live"}]},
            {"next_upload_seconds": 10.0, "cameras": {}},
        ]
    )
    relay = LiveRelayService(settings=_settings(tmp_path), cloud_client=cloud, camera_manager=manager)

    relay.relay_once()
    for _ in range(200):
        if relay._test_results:
            break
        relay._wake.wait(0.01)
    relay.relay_once()

    assert calls == ["rtsp://10.0.0.9/live"]
    assert cloud.payloads[1]["test_results"] == [
        {"job_id": "ctj_1", "result": {"ok": True, "success": True, "status": "ONLINE"}}
    ]


def test_config_sync_applies_vision_and_mapping_from_the_cloud(tmp_path):
    manager = FakeCameraManager([])
    client = SimpleNamespace(
        fetch_config=lambda: CloudResult(
            ok=True,
            data={
                "cameras": [
                    {
                        "id": "cam_cloud",
                        "name": "Doca",
                        "source_uri": "rtsp://10.0.0.5/stream",
                        "enabled": True,
                        "vision_enabled": True,
                        "mapping_enabled": True,
                    }
                ]
            },
        )
    )
    sync = ConfigSyncService(settings=_settings(tmp_path), cloud_client=client, camera_manager=manager)

    sync.sync_once()

    assert manager.cameras[0].vision_enabled is True
    assert manager.cameras[0].mapping_enabled is True
