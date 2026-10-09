from __future__ import annotations

from campex_node.cloud.client import CloudResult
from campex_node.cloud.sync import SyncService
from campex_node.core.config import NodeSettings
from campex_node.storage.local_store import LocalStore


class RecordingCloud:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.batches = []
        self.heartbeats = []
        self.before_reply = None

    def send_factory_batch(self, payload):
        self.batches.append(payload)
        if self.before_reply:
            self.before_reply()
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
    # Everything but heartbeats travels in one request.
    assert len(cloud.batches) == 1
    assert cloud.batches[0]["events"] == [{"event_id": "event_1"}]
    assert cloud.batches[0]["metrics"] == [{"metric_id": "metric_1"}]
    assert cloud.heartbeats == [{"node_id": "node_1"}]


def test_a_changed_state_is_sent_again_after_it_was_synced(tmp_path):
    settings = _settings(tmp_path)
    store = LocalStore(settings.database_path)
    store.initialize()
    cloud = RecordingCloud(ok=True)
    service = SyncService(settings=settings, cloud_client=cloud, store=store)

    store.enqueue_state("event", "event:evt_1", {"event_id": "evt_1", "status": "OPEN"})
    service.sync_once()
    store.enqueue_state("event", "event:evt_1", {"event_id": "evt_1", "status": "CLOSED"})
    store.enqueue_state("event", "event:evt_1", {"event_id": "evt_1", "status": "CLOSED", "duration": 42})
    service.sync_once()

    assert [batch["events"] for batch in cloud.batches] == [
        [{"event_id": "evt_1", "status": "OPEN"}],
        [{"event_id": "evt_1", "status": "CLOSED", "duration": 42}],  # only the latest state
    ]
    assert store.outbound_queue_size() == 0


def test_a_state_queued_while_sending_is_not_lost(tmp_path):
    settings = _settings(tmp_path)
    store = LocalStore(settings.database_path)
    store.initialize()
    cloud = RecordingCloud(ok=True)
    service = SyncService(settings=settings, cloud_client=cloud, store=store)
    store.enqueue_state("interval", "interval:act_1", {"interval_id": "act_1", "ended_at": None})
    # The machine stops while the first state is in flight.
    cloud.before_reply = lambda: store.enqueue_state(
        "interval", "interval:act_1", {"interval_id": "act_1", "ended_at": "2026-10-08T10:00:00+00:00"}
    )
    service.sync_once()
    cloud.before_reply = None
    assert store.outbound_queue_size() == 1
    service.sync_once()
    assert cloud.batches[-1]["intervals"] == [{"interval_id": "act_1", "ended_at": "2026-10-08T10:00:00+00:00"}]
    assert store.outbound_queue_size() == 0


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
