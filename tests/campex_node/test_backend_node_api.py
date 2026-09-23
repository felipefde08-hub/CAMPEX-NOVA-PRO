from __future__ import annotations

from fastapi.testclient import TestClient

from backend.config import Settings
from backend.database.db import initialize_database
from backend.main import app


def test_node_config_returns_enabled_rtsp_sources_for_node(monkeypatch, tmp_path):
    database_path = tmp_path / "node-api.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("CAMPEX_API_TOKEN", "cloud-secret")
    monkeypatch.setenv("CAMPEX_DEFAULT_ORGANIZATION_ID", "default")
    monkeypatch.setenv("CAMPEX_ALLOWED_ORGANIZATION_IDS", "default")
    monkeypatch.delenv("CAMPEX_ORGANIZATION_TOKENS", raising=False)
    initialize_database(Settings.from_env())

    headers = {"X-CAMPEX-Token": "cloud-secret"}
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/cameras",
            headers=headers,
            json={
                "name": "Camera RTSP",
                "source_type": "rtsp",
                "source_uri": "rtsp://user:pass@192.168.1.10/stream",
                "enabled": True,
                "vision_enabled": False,
            },
        )
        assert response.status_code == 201
        assert response.json()["source_uri"] == "rtsp://***:***@192.168.1.10/stream"

        config = client.get("/api/v1/node/config", headers=headers)
        heartbeat = client.post(
            "/api/v1/node/heartbeat",
            headers=headers,
            json={
                "node_id": "node_test",
                "status": "online",
                "version": "0.1.0",
                "cameras_total": 1,
                "cameras_online": 0,
            },
        )

    assert config.status_code == 200
    cameras = config.json()["cameras"]
    assert len(cameras) == 1
    assert cameras[0]["source_uri"] == "rtsp://user:pass@192.168.1.10/stream"
    assert heartbeat.status_code == 200
    assert heartbeat.json()["ok"] is True
