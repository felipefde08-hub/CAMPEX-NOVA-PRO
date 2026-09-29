from __future__ import annotations

from fastapi.testclient import TestClient

from backend.api.node_sync import NodeEventPayload
from backend.config import Settings
from backend.database.db import connect, initialize_database
from backend.events.repository import EventRepository
from backend.main import app

from campex_node.core.config import NodeSettings
from campex_node.storage.local_store import LocalStore
from campex_node.vision.service import DETECTION_EVENT_PRIORITY, NodeVisionService


def _node_settings(tmp_path) -> NodeSettings:
    return NodeSettings(
        environment="test",
        version="0.1.0",
        log_level="INFO",
        data_dir=tmp_path / "node",
        database_path=tmp_path / "node" / "node.sqlite3",
        node_id_file=tmp_path / "node" / "node_id",
    )


def _service(tmp_path, **kwargs) -> tuple[NodeVisionService, LocalStore, EventRepository]:
    settings = _node_settings(tmp_path)
    store = LocalStore(settings.database_path)
    store.initialize()
    service = NodeVisionService(settings, store=store, **kwargs)
    service.initialize()
    return service, store, EventRepository(service.vision_settings)


def _open_event(events: EventRepository):
    return events.create(
        event_type="PERSON_RESTRICTED_ZONE",
        camera_id="cam_1",
        zone_id="zone_a",
        track_id=7,
        severity="critical",
        confidence=0.88,
        metadata={"zone_name": "Doca"},
        started_at="2026-09-29T18:00:00+00:00",
    )


def _outbox(store: LocalStore) -> list[dict]:
    return store.pending_outbound(limit=100)


def test_forward_queues_open_then_closed_with_original_event_id(tmp_path):
    service, store, events = _service(tmp_path)
    event = _open_event(events)

    assert service.forward_events() == 1
    assert service.forward_events() == 0

    events.close(event.id, ended_at="2026-09-29T18:00:12+00:00", duration=12.0)
    assert service.forward_events() == 1

    items = _outbox(store)
    assert [item["id"] for item in items] == [f"{event.id}:OPEN", f"{event.id}:CLOSED"]
    opened, closed = (item["payload"] for item in items)
    assert opened["event_id"] == closed["event_id"] == event.id
    assert opened["status"] == "OPEN"
    assert opened["timestamp"] == "2026-09-29T18:00:00+00:00"
    assert opened["zone_id"] == "zone_a"
    assert opened["track_id"] == 7
    assert opened["metadata"]["zone_name"] == "Doca"
    assert closed["status"] == "CLOSED"
    assert closed["ended_at"] == "2026-09-29T18:00:12+00:00"
    assert closed["duration"] == 12.0


def test_forward_sends_close_even_when_updated_at_formats_differ(tmp_path):
    # create() writes an isoformat updated_at ("...T18:00:00..."); close() writes
    # CURRENT_TIMESTAMP ("... 18:00:05"), which sorts earlier as text.
    service, store, events = _service(tmp_path)
    event = _open_event(events)
    service.forward_events()
    events.close(event.id, ended_at="2026-09-29T18:00:05+00:00", duration=5.0)
    with connect(service.database_path) as connection:
        created, updated = connection.execute(
            "SELECT created_at, updated_at FROM events WHERE id = ?", (event.id,)
        ).fetchone()
    assert updated < created

    service.forward_events()

    assert f"{event.id}:CLOSED" in {item["id"] for item in _outbox(store)}


def test_event_opened_and_closed_between_polls_is_sent_once_as_closed(tmp_path):
    service, store, events = _service(tmp_path)
    event = _open_event(events)
    events.close(event.id, ended_at="2026-09-29T18:00:01+00:00", duration=1.0)

    service.forward_events()

    items = _outbox(store)
    assert [item["id"] for item in items] == [f"{event.id}:CLOSED"]
    assert items[0]["payload"]["timestamp"] == "2026-09-29T18:00:00+00:00"


def test_detection_events_jump_ahead_of_telemetry_events_and_metrics(tmp_path):
    service, store, events = _service(tmp_path)
    store.enqueue_event("event", {"event_id": "status_1"}, event_id="status_1")
    store.enqueue_event("metric", {"metric_id": "metric_1"}, event_id="metric_1")
    store.enqueue_event("heartbeat", {"node_id": "n"}, event_id="heartbeat_1")
    event = _open_event(events)

    service.forward_events()

    assert DETECTION_EVENT_PRIORITY == -1
    order = [item["id"] for item in _outbox(store)]
    assert order == [f"{event.id}:OPEN", "status_1", "heartbeat_1", "metric_1"]


def test_forward_is_noop_without_store(tmp_path):
    service = NodeVisionService(_node_settings(tmp_path))
    service.initialize()
    _open_event(EventRepository(service.vision_settings))

    assert service.forward_events() == 0


class _IdleEngine:
    def __init__(self, settings, camera_manager) -> None:
        pass

    def shutdown(self) -> None:
        pass


class _NoCameras:
    def configs(self):
        return []


def test_stop_flushes_events_created_since_last_poll(tmp_path):
    service, store, events = _service(tmp_path, engine_factory=_IdleEngine, poll_seconds=60)
    service.camera_manager = _NoCameras()
    service.start()
    event = _open_event(events)

    service.stop()

    assert [item["id"] for item in _outbox(store)] == [f"{event.id}:OPEN"]


def test_forwarded_payloads_are_accepted_by_cloud_and_end_up_with_duration(monkeypatch, tmp_path):
    cloud_db = tmp_path / "cloud.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{cloud_db}")
    monkeypatch.setenv("CAMPEX_API_TOKEN", "cloud-secret")
    monkeypatch.delenv("CAMPEXTOKEN", raising=False)
    monkeypatch.setenv("CAMPEX_DEFAULT_ORGANIZATION_ID", "default")
    monkeypatch.setenv("CAMPEX_ALLOWED_ORGANIZATION_IDS", "default")
    monkeypatch.delenv("CAMPEX_ORGANIZATION_TOKENS", raising=False)
    cloud_settings = Settings.from_env()
    initialize_database(cloud_settings)

    service, store, events = _service(tmp_path)
    event = _open_event(events)
    service.forward_events()
    events.close(event.id, ended_at="2026-09-29T18:00:12+00:00", duration=12.0)
    service.forward_events()

    with TestClient(app) as client:
        code = client.post(
            "/api/v1/nodes/pair/request", headers={"X-CAMPEX-Token": "cloud-secret"}, json={}
        ).json()["code"]
        token = client.post(
            "/api/v1/nodes/pair/claim", json={"code": code, "node_name": "Node E2E"}
        ).json()["node_token"]
        for item in _outbox(store):
            NodeEventPayload(**item["payload"])  # same contract the endpoint enforces
            response = client.post(
                "/api/v1/node-sync/events",
                headers={"Authorization": f"Bearer {token}"},
                json=[item["payload"]],
            )
            assert response.status_code == 200, response.text

    with connect(cloud_settings.sqlite_path) as connection:
        row = connection.execute("SELECT * FROM events WHERE id = ?", (event.id,)).fetchone()
    assert row["status"] == "CLOSED"
    assert row["started_at"] == "2026-09-29T18:00:00+00:00"
    assert row["ended_at"] == "2026-09-29T18:00:12+00:00"
    assert row["duration"] == 12.0
    assert row["zone_id"] == "zone_a"
    assert row["track_id"] == 7
