from __future__ import annotations

from dataclasses import replace

from campex_node.cameras.manager import CameraManager
from campex_node.cloud.client import CloudResult
from campex_node.cloud.heartbeat import HeartbeatService
from campex_node.core.config import NodeSettings
from campex_node.storage.local_store import LocalStore


class OfflineCloud:
    def send_heartbeat(self, payload):
        return CloudResult(ok=False, error="offline")


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


def test_heartbeat_payload_is_queued_when_cloud_is_unavailable(tmp_path):
    settings = make_settings(tmp_path)
    store = LocalStore(settings.database_path)
    store.initialize()
    service = HeartbeatService(
        settings=settings,
        node_id="node_test",
        cloud_client=OfflineCloud(),
        camera_manager=CameraManager(settings),
        store=store,
    )

    payload = service.send_once()

    assert payload == {
        "node_id": "node_test",
        "status": "online",
        "version": "0.1.0",
        "cameras_total": 0,
        "cameras_online": 0,
    }
