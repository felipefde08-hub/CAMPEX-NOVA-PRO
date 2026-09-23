from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from backend.config import Settings
from backend.database.db import connect, initialize_database
from backend.main import app


def test_node_pairing_auth_config_and_heartbeat(monkeypatch, tmp_path):
    _settings(monkeypatch, tmp_path)
    headers = {"X-CAMPEX-Token": "cloud-secret"}

    with TestClient(app) as client:
        pair = client.post("/api/v1/nodes/pair/request", headers=headers, json={})
        assert pair.status_code == 201
        code = pair.json()["code"]
        assert code.startswith("CXP-")

        claim = client.post(
            "/api/v1/nodes/pair/claim",
            json={
                "code": code,
                "node_name": "RBA-NODE-01",
                "platform": "windows",
                "hostname": "RBA-VISION-01",
                "version": "0.1.0",
            },
        )
        assert claim.status_code == 200
        claimed = claim.json()
        assert claimed["node_id"].startswith("node_")
        assert claimed["node_token"]

        listed_text = client.get("/api/v1/nodes", headers=headers).text
        assert claimed["node_token"] not in listed_text

        camera = client.post(
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
        assert camera.status_code == 201
        assert camera.json()["source_uri"] == "rtsp://***:***@192.168.1.10/stream"

        node_headers = {"Authorization": f"Bearer {claimed['node_token']}"}
        config = client.get(
            f"/api/v1/nodes/{claimed['node_id']}/config",
            headers=node_headers,
        )
        heartbeat = client.post(
            f"/api/v1/nodes/{claimed['node_id']}/heartbeat",
            headers=node_headers,
            json={
                "status": "online",
                "version": "0.1.0",
                "platform": "windows",
                "hostname": "RBA-VISION-01",
                "cameras_total": 1,
                "cameras_online": 0,
                "vision_status": "running",
                "queue_size": 2,
            },
        )
        nodes = client.get("/api/v1/nodes", headers=headers)

    assert config.status_code == 200
    cameras = config.json()["cameras"]
    assert len(cameras) == 1
    assert cameras[0]["source_uri"] == "rtsp://user:pass@192.168.1.10/stream"
    assert heartbeat.status_code == 200
    listed = nodes.json()
    assert listed[0]["id"] == claimed["node_id"]
    assert listed[0]["cameras_total"] == 1
    assert listed[0]["queue_size"] == 2


def test_node_auth_rejects_invalid_and_revoked_tokens(monkeypatch, tmp_path):
    _settings(monkeypatch, tmp_path)
    headers = {"X-CAMPEX-Token": "cloud-secret"}

    with TestClient(app) as client:
        code = client.post("/api/v1/nodes/pair/request", headers=headers, json={}).json()["code"]
        claim = client.post(
            "/api/v1/nodes/pair/claim",
            json={"code": code, "node_name": "Node inválido"},
        ).json()

        invalid = client.get(
            f"/api/v1/nodes/{claim['node_id']}/config",
            headers={"Authorization": "Bearer invalid-token"},
        )
        revoked = client.delete(f"/api/v1/nodes/{claim['node_id']}", headers=headers)
        after_revoke = client.get(
            f"/api/v1/nodes/{claim['node_id']}/config",
            headers={"Authorization": f"Bearer {claim['node_token']}"},
        )

    assert invalid.status_code == 401
    assert revoked.status_code == 204
    assert after_revoke.status_code == 401


def test_pairing_code_expires(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    expired_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    with connect(settings.sqlite_path) as connection:
        connection.execute(
            """
            INSERT INTO node_pairing_codes (code, organization_id, expires_at, created_at)
            VALUES ('CXP-OLD1-OLD2', 'default', ?, ?)
            """,
            (expired_at.isoformat(), expired_at.isoformat()),
        )
        connection.commit()

    with TestClient(app) as client:
        claim = client.post(
            "/api/v1/nodes/pair/claim",
            json={"code": "CXP-OLD1-OLD2", "node_name": "Node expirado"},
        )

    assert claim.status_code == 400


def test_node_list_marks_stale_heartbeat_offline(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    stale = datetime.now(timezone.utc) - timedelta(minutes=10)
    with connect(settings.sqlite_path) as connection:
        connection.execute(
            """
            INSERT INTO campex_nodes (
                id, organization_id, name, token_hash, status, version,
                cameras_total, cameras_online, last_seen_at, created_at, updated_at
            )
            VALUES (
                'node_stale', 'default', 'Node parado', 'hash', 'online', '0.1.0',
                2, 2, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """,
            (stale.isoformat(),),
        )
        connection.commit()

    with TestClient(app) as client:
        nodes = client.get(
            "/api/v1/nodes",
            headers={"X-CAMPEX-Token": "cloud-secret"},
        )

    assert nodes.status_code == 200
    assert nodes.json()[0]["status"] == "offline"


def _settings(monkeypatch, tmp_path) -> Settings:
    database_path = tmp_path / "node-api.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("CAMPEX_API_TOKEN", "cloud-secret")
    monkeypatch.setenv("CAMPEX_DEFAULT_ORGANIZATION_ID", "default")
    monkeypatch.setenv("CAMPEX_ALLOWED_ORGANIZATION_IDS", "default")
    monkeypatch.delenv("CAMPEX_ORGANIZATION_TOKENS", raising=False)
    settings = Settings.from_env()
    initialize_database(settings)
    return settings
