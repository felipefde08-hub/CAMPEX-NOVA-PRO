from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from campex_node.core.config import NodeCameraConfig
from campex_node.local_app import create_app
from campex_node.recording import Segment


SQUARE = [[0.1, 0.1], [0.5, 0.1], [0.5, 0.9], [0.1, 0.9]]


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("CAMPEX_NODE_DATA_DIR", str(tmp_path / "node"))
    monkeypatch.setenv("CAMPEX_NODE_RECORDING_ENABLED", "false")
    app = create_app()
    camera = NodeCameraConfig(id="cam-1", name="Prensas", rtsp_url="rtsp://camera/1")
    return app, camera


def test_shifts_and_factory_settings(monkeypatch, tmp_path):
    app, _camera = _client(monkeypatch, tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/factory/settings").json() == {"timezone": "America/Sao_Paulo", "currency": "BRL"}
        assert client.patch("/api/factory/settings", json={"timezone": "Mars/Base"}).status_code == 400

        created = client.post(
            "/api/factory/shifts",
            json={"name": "Noite", "start": "22:00", "end": "06:00", "days": [0, 1, 2, 3, 4],
                  "breaks": [{"name": "Jantar", "start": "02:00", "end": "02:30"}]},
        )
        assert created.status_code == 201
        shift = created.json()
        assert shift["breaks"] == [{"name": "Jantar", "start": "02:00", "end": "02:30"}]
        assert client.post("/api/factory/shifts", json={"name": "X", "start": "25:00", "end": "06:00"}).status_code == 400

        updated = client.patch(f"/api/factory/shifts/{shift['id']}", json={"end": "05:30"}).json()
        assert (updated["start"], updated["end"]) == ("22:00", "05:30")
        assert [item["name"] for item in client.get("/api/factory/shifts").json()] == ["Noite"]
        assert client.delete(f"/api/factory/shifts/{shift['id']}").status_code == 204
        assert client.get("/api/factory/shifts").json() == []


def test_machine_zones_carry_settings_and_take_external_counts(monkeypatch, tmp_path):
    app, camera = _client(monkeypatch, tmp_path)
    with TestClient(app) as client:
        monkeypatch.setattr(app.state.runtime.lifecycle.camera_manager, "configs", lambda: [camera])
        machine = client.post(
            "/api/zones",
            json={"camera_id": "cam-1", "name": "Prensa 3", "type": "machine", "points": SQUARE,
                  "settings": {"cost_per_hour": 450, "requires_operator": True, "unknown": 1}},
        )
        assert machine.status_code == 201
        settings = machine.json()["settings"]
        assert settings["cost_per_hour"] == 450.0 and settings["requires_operator"] is True
        assert "unknown" not in settings and settings["stop_after_seconds"] == 30.0

        line = {"camera_id": "cam-1", "name": "Portão", "type": "line", "points": [[0.5, 0.0], [0.5, 1.0]]}
        assert client.post("/api/zones", json=line).status_code == 201
        assert client.post("/api/zones", json={**line, "points": SQUARE}).status_code == 400
        assert client.post("/api/zones", json={**line, "type": "station"}).status_code == 400

        zone_id = machine.json()["id"]
        patched = client.patch(f"/api/zones/{zone_id}", json={"settings": {"line": "Linha 2"}}).json()
        assert patched["settings"]["line"] == "Linha 2" and patched["settings"]["cost_per_hour"] == 450.0

        at = datetime.now(timezone.utc) - timedelta(minutes=5)
        assert client.post("/api/production/counts", json={"zone_id": zone_id, "count": 40, "at": at.isoformat()}).status_code == 201
        assert client.post("/api/production/counts", json={"zone_id": "nope", "count": 1}).status_code == 404

        report = client.get("/api/analytics/machines?period=today").json()
        [press] = report["machines"]
        assert press["name"] == "Prensa 3" and press["cycles"] == 40
        assert client.get("/api/analytics/machines?period=forever").status_code == 400

        for path in (
            "/api/analytics/stops?period=week&min_minutes=15",
            f"/api/analytics/machines/{zone_id}/speed?period=today",
            "/api/analytics/hourly?period=today",
            "/api/analytics/shifts?period=month",
            "/api/analytics/occupancy?period=today",
            "/api/analytics/first-arrival",
            "/api/analytics/breaks?date=2026-10-07",
            "/api/analytics/after-hours?period=yesterday",
            "/api/analytics/docks?period=today",
            "/api/analytics/lines?period=today",
            "/api/analytics/summary?period=week",
            "/api/factory/live",
        ):
            assert client.get(path).status_code == 200, path


def test_recording_lookup_serves_the_segment_with_seek_offset(monkeypatch, tmp_path):
    app, _camera = _client(monkeypatch, tmp_path)
    with TestClient(app) as client:
        lifecycle = app.state.runtime.lifecycle
        started = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
        path = lifecycle.settings.recordings_path / "cam-1" / "2026-10-07" / "120000_rec_a.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"0123456789")
        lifecycle.recording_store.add(
            Segment("rec_a", "cam-1", path, started, started + timedelta(minutes=5), 300.0, 10, "h264", 1920, 1080, "COMPLETE")
        )

        found = client.get("/api/cameras/cam-1/recording", params={"at": "2026-10-07T12:02:30+00:00"}).json()
        assert found["offset_seconds"] == 150.0
        assert found["url"] == "/api/recordings/rec_a/file#t=150.0"
        assert client.get("/api/cameras/cam-1/recording", params={"at": "2026-10-07T13:00:00+00:00"}).status_code == 404

        listed = client.get("/api/recordings", params={"camera_id": "cam-1", "start": "2026-10-07T00:00:00+00:00",
                                                      "end": "2026-10-08T00:00:00+00:00"}).json()
        assert [item["id"] for item in listed] == ["rec_a"]
        # The player seeks with range requests.
        partial = client.get("/api/recordings/rec_a/file", headers={"Range": "bytes=2-5"})
        assert partial.status_code == 206 and partial.content == b"2345"
        assert partial.headers["content-type"] == "video/mp4"
