from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.api import operations
from backend.config import Settings
from backend.database.db import connect, initialize_database

ORG = "org-test"


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _setup(monkeypatch, tmp_path) -> Settings:
    database_path = tmp_path / "summary.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    settings = Settings.from_env()
    initialize_database(settings)
    monkeypatch.setattr(operations, "_settings", lambda: settings)
    return settings


def _insert_camera(connection, camera_id, *, status, enabled=1, vision_enabled=1, last_frame_at=None, created_at=None):
    connection.execute(
        """
        INSERT INTO cameras (id, organization_id, name, source_type, source_uri, enabled, vision_enabled,
                             status, last_frame_at, created_at)
        VALUES (?, ?, ?, 'rtsp', 'rtsp://user:secret@10.0.0.1/stream', ?, ?, ?, ?, ?)
        """,
        (camera_id, ORG, camera_id, enabled, vision_enabled, status, last_frame_at, created_at or "2026-01-01 00:00:00"),
    )


def _insert_event(connection, event_id, *, severity, status, started_at):
    connection.execute(
        """
        INSERT INTO events (id, organization_id, type, camera_id, severity, status, started_at)
        VALUES (?, ?, 'PERSON_RESTRICTED_ZONE', 'cam-online', ?, ?, ?)
        """,
        (event_id, ORG, severity, status, started_at),
    )


def test_summary_separates_camera_states_and_vision(monkeypatch, tmp_path):
    settings = _setup(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    with connect(settings.sqlite_path) as connection:
        _insert_camera(connection, "cam-online", status="ONLINE")
        _insert_camera(connection, "cam-vision-off", status="ONLINE", vision_enabled=0)
        _insert_camera(connection, "cam-offline", status="OFFLINE")
        _insert_camera(connection, "cam-reconnecting", status="CONNECTING", last_frame_at=_iso(now - timedelta(seconds=10)))
        _insert_camera(connection, "cam-stuck", status="CONNECTING", last_frame_at=_iso(now - timedelta(minutes=10)))
        _insert_camera(connection, "cam-disabled", status="OFFLINE", enabled=0)
        connection.commit()

    payload = operations._operations_summary_payload(None, ORG)

    states = {camera["id"]: camera["image_state"] for camera in payload["cameras"]}
    assert states == {
        "cam-online": "online",
        "cam-vision-off": "online",
        "cam-offline": "no_image",
        "cam-reconnecting": "connecting",
        "cam-stuck": "no_image",
        "cam-disabled": "disabled",
    }
    monitoring = {camera["id"] for camera in payload["cameras"] if camera["monitoring"]}
    assert monitoring == {"cam-online"}
    kpis = payload["kpis"]
    assert kpis["cameras_monitoring"] == 1
    assert kpis["cameras_vision_off"] == 1
    assert kpis["cameras_no_image"] == 2
    assert kpis["cameras_connecting"] == 1
    assert kpis["cameras_disabled"] == 1
    assert all("source_uri" not in camera for camera in payload["cameras"])


def test_summary_counts_only_open_critical_and_lists_open_events(monkeypatch, tmp_path):
    settings = _setup(monkeypatch, tmp_path)
    base = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    with connect(settings.sqlite_path) as connection:
        _insert_camera(connection, "cam-online", status="ONLINE")
        _insert_event(connection, "closed-critical", severity="critical", status="CLOSED", started_at=_iso(base))
        _insert_event(connection, "open-info-old", severity="info", status="OPEN", started_at=_iso(base + timedelta(minutes=1)))
        _insert_event(connection, "open-critical", severity="critical", status="OPEN", started_at=_iso(base + timedelta(minutes=5)))
        _insert_event(connection, "open-attention", severity="attention", status="OPEN", started_at=_iso(base + timedelta(minutes=2)))
        _insert_event(connection, "reviewed", severity="critical", status="REVIEWED", started_at=_iso(base + timedelta(minutes=3)))
        connection.commit()

    payload = operations._operations_summary_payload(None, ORG)

    assert payload["kpis"]["events_open"] == 3
    assert payload["kpis"]["events_open_critical"] == 1
    assert [event["id"] for event in payload["open_events"]] == ["open-critical", "open-attention", "open-info-old"]
