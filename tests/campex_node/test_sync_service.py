from __future__ import annotations

from campex_node.cloud.client import CloudResult
from campex_node.cloud.sync import SyncService
from campex_node.core.config import NodeSettings
from campex_node.storage.local_store import LocalStore


class RecordingCloud:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.events = []
        self.metrics = []
        self.heartbeats = []

    def send_events(self, events):
        self.events.extend(events)
        return CloudResult(ok=self.ok, error=None if self.ok else "offline")

    def send_metrics(self, metrics):
        self.metrics.extend(metrics)
        return CloudResult(ok=self.ok, error=None if self.ok else "offline")

    def send_heartbeat(self, payload):
        self.heartbeats.append(payload)
        return CloudResult(ok=self.ok, error=None if self.ok else "offline")


def test_sync_service_sends_pending_items_and_marks_synced(tmp_path):
    settings = _settings(tmp_path)
    store = LocalStore(settings.database_path)
    store.initialize()
    store.enqueue_event("event", {"event_id": "event_1"}, event_id="event_1")
    store.enqueue_event("metric", {"metric_id": "metric_1"}, event_id="metric_1")
    store.enqueue_event("heartbeat", {"node_id": "node_1"}, event_id="heartbeat_1")
    cloud = RecordingCloud(ok=True)

    result = SyncService(settings=settings, cloud_client=cloud, store=store).sync_once()

    assert result["sent"] == 3
    assert result["pending"] == 0
    assert cloud.events == [{"event_id": "event_1"}]
    assert cloud.metrics == [{"metric_id": "metric_1"}]
    assert cloud.heartbeats == [{"node_id": "node_1"}]


def test_sync_service_keeps_failed_items_pending_with_backoff(tmp_path):
    settings = _settings(tmp_path)
    store = LocalStore(settings.database_path)
    store.initialize()
    store.enqueue_event("event", {"event_id": "event_1"}, event_id="event_1")

    result = SyncService(settings=settings, cloud_client=RecordingCloud(ok=False), store=store).sync_once()

    assert result["failed"] == 1
    assert result["pending"] == 1
    assert store.pending_outbound() == []


def _settings(tmp_path):
    return NodeSettings(
        environment="test",
        version="0.1.0",
        log_level="INFO",
        data_dir=tmp_path,
        database_path=tmp_path / "node.sqlite3",
        node_id_file=tmp_path / "node_id",
    )
