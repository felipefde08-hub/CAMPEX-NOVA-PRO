from __future__ import annotations

import sqlite3

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


def test_pending_outbound_sends_events_before_heartbeats_and_metrics(tmp_path):
    store = LocalStore(tmp_path / "node.sqlite3")
    store.initialize()
    store.enqueue_event("metric", {"metric_id": "metric_1"}, event_id="metric_1")
    store.enqueue_event("heartbeat", {"node_id": "node_1"}, event_id="heartbeat_1")
    store.enqueue_event("metric", {"metric_id": "metric_2"}, event_id="metric_2")
    store.enqueue_event("event", {"event_id": "event_1"}, event_id="event_1")

    order = [item["id"] for item in store.pending_outbound()]

    assert order == ["event_1", "heartbeat_1", "metric_1", "metric_2"]


def test_pending_outbound_respects_explicit_priority(tmp_path):
    store = LocalStore(tmp_path / "node.sqlite3")
    store.initialize()
    store.enqueue_event("event", {"event_id": "event_1"}, event_id="event_1")
    store.enqueue_event("metric", {"metric_id": "urgent"}, event_id="urgent", priority=-1)

    assert store.pending_outbound()[0]["id"] == "urgent"


def test_initialize_migrates_legacy_outbox_and_backfills_priority(tmp_path):
    database_path = tmp_path / "node.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE outbound_events (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        for item_id, item_type, created_at in (
            ("metric_old", "metric", "2026-01-01T00:00:00+00:00"),
            ("heartbeat_old", "heartbeat", "2026-01-01T00:00:01+00:00"),
            ("event_new", "event", "2026-01-01T00:00:02+00:00"),
        ):
            connection.execute(
                "INSERT INTO outbound_events (id, type, payload_json, created_at, updated_at) "
                "VALUES (?, ?, '{}', ?, ?)",
                (item_id, item_type, created_at, created_at),
            )

    store = LocalStore(database_path)
    store.initialize()
    store.initialize()  # migration must be idempotent

    assert [item["id"] for item in store.pending_outbound()] == [
        "event_new",
        "heartbeat_old",
        "metric_old",
    ]
    with sqlite3.connect(database_path) as connection:
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(outbound_events)")}
    assert "idx_outbound_events_status_priority_created" in indexes
