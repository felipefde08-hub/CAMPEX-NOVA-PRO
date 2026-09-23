from __future__ import annotations

from campex_node.storage.local_store import LocalStore


def test_local_store_persists_meta_and_outbox(tmp_path):
    store = LocalStore(tmp_path / "node.sqlite3")
    store.initialize()

    store.set_meta("node_id", "node_test")
    event_id = store.enqueue_event("heartbeat", {"status": "online"})

    assert store.get_meta("node_id") == "node_test"
    assert event_id.startswith("evt_")
    assert store.outbound_queue_size() == 1


def test_local_store_tracks_outbound_retry_and_synced_state(tmp_path):
    store = LocalStore(tmp_path / "node.sqlite3")
    store.initialize()
    item_id = store.enqueue_event("event", {"event_id": "event_1"}, event_id="event_1")

    pending = store.pending_outbound()
    assert pending[0]["id"] == item_id
    assert pending[0]["payload"]["event_id"] == "event_1"

    store.mark_outbound_failed(item_id, "temporary offline", retry_seconds=60)
    assert store.pending_outbound() == []
    assert store.outbound_queue_size() == 1

    store.mark_outbound_synced(item_id)
    assert store.outbound_queue_size() == 0
