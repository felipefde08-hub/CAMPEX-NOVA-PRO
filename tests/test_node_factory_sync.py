"""Factory data from a CAMPEX Node to the Cloud: sync, alerts and reports.

The Node side is real (stores, outbox, SyncService); its Cloud client posts
through the Cloud's FastAPI app. Telegram is the only fake.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.database.db import connect, initialize_database
from backend.factory.reports import factory_analytics, factory_nodes
from backend.integrations.telegram import TelegramClient
from backend.main import app
from backend.notifications.models import ALERT_TYPES, NotificationPreference
from backend.notifications.service import _report_due
from campex_node.activity import MACHINE, ActivityStore
from campex_node.analytics import FactoryAnalytics
from campex_node.cloud.client import CloudResult
from campex_node.cloud.outbox import CloudOutbox
from campex_node.cloud.sync import SyncService
from campex_node.core.config import NodeCameraConfig, NodeSettings
from campex_node.events import NodeEventStore
from campex_node.factory import FactoryStore
from campex_node.storage.local_store import LocalStore

from tests.test_cloud_cameras import _configure, _login, _pair_node


SQUARE = [[0.1, 0.1], [0.5, 0.1], [0.5, 0.9], [0.1, 0.9]]
CAMERA = NodeCameraConfig(id="local_cam1", name="Galpão 1", rtsp_url="rtsp://10.0.0.2/s")


class ApiCloud:
    """The Node's CloudClient, posting through the Cloud's test client."""

    def __init__(self, client: TestClient, headers: dict) -> None:
        self.client = client
        self.headers = headers

    def send_factory_batch(self, payload):
        response = self.client.post("/api/v1/node-sync/v2/batch", headers=self.headers, json=payload)
        return CloudResult(ok=response.status_code == 200, status_code=response.status_code, data=response.json())


class Node:
    def __init__(self, tmp_path, cloud: ApiCloud) -> None:
        database = tmp_path / "node.sqlite3"
        self.store = LocalStore(database)
        self.events = NodeEventStore(database)
        self.activity = ActivityStore(database)
        self.factory = FactoryStore(database)
        for item in (self.store, self.events, self.activity, self.factory):
            item.initialize()
        self.outbox = CloudOutbox(self.store, self.events, self.activity, self.factory, lambda: [CAMERA])
        self.outbox.attach()
        settings = NodeSettings(
            environment="test", version="0.1.0", log_level="INFO", data_dir=tmp_path,
            database_path=database, node_id_file=tmp_path / "node_id",
        )
        self.sync = SyncService(settings=settings, cloud_client=cloud, store=self.store, outbox=self.outbox)

    def flush(self) -> None:
        self.activity.flush()
        self.outbox.refresh_factory(force=True)
        while self.store.outbound_queue_size():
            result = self.sync.sync_once()
            assert result["failed"] == 0, self.store.pending_outbound()


@pytest.fixture
def cloud(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(TelegramClient, "send_message", lambda self, chat_id, message: sent.append((chat_id, message)) or {"message_id": len(sent)})
    with TestClient(app) as client:
        user = _login(client, "gestor@fabrica.com")
        organization_id = client.get("/api/v1/auth/me", headers=user).json()["user"]["organization_id"]
        preferences = client.get("/api/v1/notifications/preferences", headers=user).json()
        client.put(
            "/api/v1/notifications/preferences",
            headers=user,
            json={**preferences, "telegram_enabled": True, "telegram_chat_id": "555"},
        )
        node_id, node_headers = _pair_node(client, user, "node-public-1")
        node = Node(tmp_path / "node", ApiCloud(client, node_headers))
        yield client, user, organization_id, node_id, node_headers, node, sent


def _machine(node: Node, *, cost_per_hour: float = 120.0):
    zone = node.events.create_zone(
        camera_id=CAMERA.id, name="Injetora 3", zone_type="machine", points=SQUARE,
        settings={"line": "Linha A", "cost_per_hour": cost_per_hour},
    )
    node.factory.create_shift({"name": "Geral", "start": "00:00", "end": "23:59", "days": [0, 1, 2, 3, 4, 5, 6], "breaks": []})
    return zone


def test_machine_stop_reaches_the_cloud_alerts_once_and_matches_the_node_report(cloud):
    client, user, organization_id, node_id, _headers, node, sent = cloud
    zone = _machine(node)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    running = node.activity.open(kind=MACHINE, zone_id=zone.id, camera_id=CAMERA.id, state="RUNNING", started_at=now - timedelta(hours=2))
    node.activity.close(running, now - timedelta(minutes=30))
    stopped = node.activity.open(kind=MACHINE, zone_id=zone.id, camera_id=CAMERA.id, state="STOPPED", started_at=now - timedelta(minutes=30))
    for _ in range(12):
        node.activity.add_count(zone.id, now - timedelta(hours=1), "cycles")
    event = node.events.create(
        event_type="MACHINE_STOPPED", camera_id=CAMERA.id, zone_id=zone.id, severity="attention",
        started_at=(now - timedelta(minutes=2)).isoformat(),
        metadata={"zone_name": "Injetora 3", "zone_type": "machine", "line": "Linha A", "snapshot_path": "C:/node/frame.jpg"},
    )
    node.flush()

    # One alert, with the machine, its line and the camera name.
    assert len(sent) == 1
    chat_id, message = sent[0]
    assert chat_id == "555"
    assert "🔴 Máquina parada: Injetora 3 · Linha A" in message
    assert "Câmera: Galpão 1" in message

    # The machine runs again: the event closes in the Cloud, no second alert.
    node.activity.close(stopped, now)
    node.events.update_status(event.id, "CLOSED", ended_at=now.isoformat(), duration=120.0)
    node.flush()
    assert len(sent) == 1
    with connect(Settings.from_env().database_target) as connection:
        row = connection.execute("SELECT status, duration, zone_id, metadata FROM events WHERE id = ?", (event.id,)).fetchone()
    assert (row["status"], row["duration"], row["zone_id"]) == ("CLOSED", 120.0, zone.id)
    assert "snapshot_path" not in row["metadata"]  # Node disk paths stay on the Node

    # The Cloud runs the Node's analytics on the synced data: same numbers.
    start, end = now - timedelta(hours=3), now
    local = FactoryAnalytics(node.events, node.activity, node.factory).machines(start, end, now)
    (synced,) = factory_nodes(Settings.from_env(), organization_id)
    remote = factory_analytics(Settings.from_env(), organization_id, synced).machines(start, end, now)
    assert remote == local
    assert remote["machines"][0]["stopped_seconds"] == 30 * 60
    assert remote["machines"][0]["lost_cost"] == 60.0
    assert remote["machines"][0]["cycles"] == 12

    # "Send report" now uses the factory data.
    response = client.post("/api/v1/notifications/send-report", headers=user)
    assert response.status_code == 200, response.text
    report = sent[-1][1]
    assert "CAMPEX - Relatório da fábrica" in report
    assert "Injetora 3" in report


def test_old_events_feed_reports_without_alerting(cloud):
    *_rest, node, sent = cloud
    zone = _machine(node)
    node.events.create(
        event_type="MACHINE_STOPPED", camera_id=CAMERA.id, zone_id=zone.id, severity="attention",
        started_at=(datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
        metadata={"zone_name": "Injetora 3"},
    )
    node.events.create(event_type="DOCK_VISIT", camera_id=CAMERA.id, zone_id=zone.id, metadata={"zone_name": "Doca"})
    node.flush()
    assert sent == []


def test_a_node_cannot_overwrite_another_nodes_data(cloud):
    client, user, _org, _node_id, headers, node, _sent = cloud
    zone = _machine(node)
    event = node.events.create(event_type="STATION_VACANT", camera_id=CAMERA.id, zone_id=zone.id, metadata={"zone_name": "Posto 1"})
    node.flush()

    _other_id, other_headers = _pair_node(client, user, "node-public-2")
    forged = {"event_id": event.id, "camera_id": CAMERA.id, "event_type": "STATION_VACANT", "status": "CLOSED", "timestamp": event.started_at}
    response = client.post("/api/v1/node-sync/v2/batch", headers=other_headers, json={"events": [forged, {"event_id": ""}]})
    assert response.status_code == 200
    assert response.json()["events"] == {"accepted": 0, "rejected": 2}  # foreign id + invalid item
    with connect(Settings.from_env().database_target) as connection:
        assert connection.execute("SELECT status FROM events WHERE id = ?", (event.id,)).fetchone()["status"] == "OPEN"


def test_new_alert_types_start_enabled_but_a_choice_is_kept(cloud):
    client, user, _org, *_rest = cloud
    with connect(Settings.from_env().database_target) as connection:
        # A preference saved before factory alerts existed.
        connection.execute(
            "UPDATE notification_preferences SET alert_types = ?, known_alert_types = ?",
            ('["camera_offline"]', '["camera_offline","camera_online","crowding_ended","crowding_started","equipment_state_changed","equipment_stop_started","long_presence","zone_activity_resumed","zone_idle"]'),
        )
        connection.commit()
    types = set(client.get("/api/v1/notifications/preferences", headers=user).json()["alert_types"])
    assert {"camera_offline", "missing_operator", "station_vacant", "restricted_zone", "after_hours_presence"} <= types
    assert "equipment_stop_started" not in types  # it existed and was not chosen

    preferences = client.get("/api/v1/notifications/preferences", headers=user).json()
    chosen = sorted(types - {"station_vacant"})
    client.put("/api/v1/notifications/preferences", headers=user, json={**preferences, "alert_types": chosen})
    assert "station_vacant" not in client.get("/api/v1/notifications/preferences", headers=user).json()["alert_types"]


def test_scheduled_report_runs_from_cron_once_per_day(cloud):
    client, user, *_rest, node, sent = cloud
    _machine(node)
    node.flush()
    preferences = client.get("/api/v1/notifications/preferences", headers=user).json()
    client.put(
        "/api/v1/notifications/preferences",
        headers=user,
        json={**preferences, "reports_enabled": True, "report_frequency": "DAILY", "report_time": "00:00"},
    )
    assert client.get("/api/v1/notifications/cron/reports").status_code == 401
    cron = {"Authorization": "Bearer cron-secret"}
    assert client.get("/api/v1/notifications/cron/reports", headers=cron).status_code == 200
    assert client.get("/api/v1/notifications/cron/reports", headers=cron).status_code == 200
    reports = [message for _chat, message in sent if "Relatório da fábrica" in message]
    assert len(reports) == 1


def _pref(**values) -> NotificationPreference:
    return replace(
        NotificationPreference(id="p", organization_id="org", reports_enabled=True, timezone="America/Sao_Paulo"), **values
    )


def test_report_is_due_from_its_local_time_on_its_day():
    # 2026-10-09 is a Friday; 21:30 UTC is 18:30 in São Paulo.
    friday_evening = datetime(2026, 10, 9, 21, 30, tzinfo=timezone.utc)
    friday_morning = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    assert _report_due(_pref(report_time="18:00"), friday_evening) == ("scheduled-report:DAILY:2026-10-09", "today")
    assert _report_due(_pref(report_time="18:00"), friday_morning) is None
    assert _report_due(_pref(report_frequency="WEEKLY", report_weekday=4), friday_evening)[1] == "week"
    assert _report_due(_pref(report_frequency="WEEKLY", report_weekday=0), friday_evening) is None
    month_end = datetime(2026, 10, 31, 22, 0, tzinfo=timezone.utc)
    assert _report_due(_pref(report_frequency="MONTHLY", report_month_day=0), month_end)[1] == "month"
    assert _report_due(_pref(report_frequency="MONTHLY", report_month_day=0), friday_evening) is None
    assert _report_due(_pref(reports_enabled=False), friday_evening) is None


def test_all_factory_alert_types_are_offered():
    from backend.notifications.models import NODE_EVENT_ALERTS

    assert set(NODE_EVENT_ALERTS.values()) <= ALERT_TYPES
