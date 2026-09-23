from __future__ import annotations

from datetime import datetime, timezone

from backend.cameras.health import CameraStatus

from campex_node.cameras.camera import CameraRuntimeState
from campex_node.core.config import NodeSettings
from campex_node.storage.local_store import LocalStore
from campex_node.telemetry.collector import TelemetryCollector


class StaticCameraManager:
    def __init__(self, states):
        self._states = states

    def states(self):
        return self._states


def test_telemetry_collector_queues_metrics_and_status_event(tmp_path):
    settings = _settings(tmp_path)
    store = LocalStore(settings.database_path)
    store.initialize()
    manager = StaticCameraManager([
        CameraRuntimeState(
            id="cam_1",
            name="Entrada",
            status=CameraStatus.OFFLINE,
            last_frame_at=None,
            last_connected_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
            reconnect_attempts=2,
            frames_received=10,
            consecutive_failures=1,
            last_error="rtsp://user:pass@192.168.1.10/stream failed",
        )
    ])

    result = TelemetryCollector(
        settings=settings,
        node_id="node_test",
        camera_manager=manager,
        store=store,
    ).collect_once()

    pending = store.pending_outbound(limit=10)
    assert result == {"metrics": 4, "events": 1}
    assert store.outbound_queue_size() == 5
    assert {item["type"] for item in pending} == {"metric", "event"}
    assert all("user:pass" not in str(item["payload"]) for item in pending)


def test_telemetry_collector_only_emits_status_event_on_change(tmp_path):
    settings = _settings(tmp_path)
    store = LocalStore(settings.database_path)
    store.initialize()
    state = CameraRuntimeState(
        id="cam_1",
        name="Entrada",
        status=CameraStatus.ONLINE,
        last_frame_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
        last_connected_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
        reconnect_attempts=0,
        frames_received=100,
        consecutive_failures=0,
    )
    collector = TelemetryCollector(
        settings=settings,
        node_id="node_test",
        camera_manager=StaticCameraManager([state]),
        store=store,
    )

    first = collector.collect_once()
    second = collector.collect_once()

    assert first["events"] == 1
    assert second["events"] == 0
    assert second["metrics"] == 4


def _settings(tmp_path):
    return NodeSettings(
        environment="test",
        version="0.1.0",
        log_level="INFO",
        data_dir=tmp_path,
        database_path=tmp_path / "node.sqlite3",
        node_id_file=tmp_path / "node_id",
        telemetry_interval_seconds=1,
    )
