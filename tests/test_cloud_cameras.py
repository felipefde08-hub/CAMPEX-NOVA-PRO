"""Cloud camera flow: dashboard -> Cloud -> CAMPEX Node -> Cloud -> dashboard."""

from __future__ import annotations

import base64

import cv2
import numpy as np
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.database.db import initialize_database
from backend.main import app


CAMERA = {
    "name": "Doca 1",
    "source_type": "rtsp",
    "source_uri": "rtsp://admin:segredo@192.168.1.10:554/stream",
    "enabled": True,
    "vision_enabled": False,
}


def _configure(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CAMPEX_RUNTIME", "serverless")
    monkeypatch.setenv("CAMPEX_REQUIRE_LOGIN", "true")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cloud.sqlite3'}")
    monkeypatch.delenv("CAMPEX_API_TOKEN", raising=False)
    monkeypatch.delenv("CAMPEXTOKEN", raising=False)
    initialize_database(Settings.from_env())


def _jpeg(width: int = 64, height: int = 48) -> str:
    frame = np.full((height, width, 3), 90, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", frame)
    assert ok
    return base64.b64encode(encoded.tobytes()).decode("ascii")


def _login(client, email: str) -> dict:
    token = client.post(
        "/api/v1/auth/register",
        json={"name": "Operador", "email": email, "password": "senha-forte-123"},
    ).json()["token"]
    return {"X-CAMPEX-Session": token}


def _pair_node(client, user: dict, public_id: str) -> tuple[str, dict]:
    session = client.post(
        "/api/v1/nodes/pairing/start",
        json={"node_public_id": public_id, "node_name": "Node Doca"},
    ).json()
    authorized = client.post(
        "/api/v1/nodes/pairing/authorize",
        headers=user,
        json={"code": session["pairing_code"], "node_name": "Node Doca"},
    )
    assert authorized.status_code == 200
    status = client.post(
        "/api/v1/nodes/pairing/status",
        json={"session_id": session["session_id"], "node_public_id": public_id},
    ).json()
    node_headers = {"Authorization": f"Bearer {status['node_token']}"}
    heartbeat = client.post(f"/api/v1/nodes/{status['node_id']}/heartbeat", headers=node_headers, json={})
    assert heartbeat.status_code == 200
    return status["node_id"], node_headers


def _report(client, node_headers: dict, camera_id: str, **extra) -> dict:
    report = {"camera_id": camera_id, "status": "ONLINE", "frames_received": 10, **extra}
    response = client.post("/api/v1/node-sync/cameras/live", headers=node_headers, json={"cameras": [report]})
    assert response.status_code == 200
    return response.json()


def test_node_camera_live_view_vision_and_mapping(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)

    with TestClient(app) as client:
        user = _login(client, "ana@empresa.com")
        node_id, node_headers = _pair_node(client, user, "node_public_doca")

        camera = client.post("/api/v1/cameras", headers=user, json=CAMERA)
        assert camera.status_code == 201
        camera_id = camera.json()["id"]

        # Before the Node reports anything the dashboard explains what it waits for.
        health = client.get(f"/api/v1/cameras/{camera_id}/health", headers=user).json()
        assert health["status"] == "OFFLINE"
        assert "Aguardando o CAMPEX Node" in health["last_error"]
        info = client.get(f"/api/v1/cameras/{camera_id}/stream/info", headers=user).json()
        assert info["mode"] == "node_relay"

        # The Node receives the camera with the real RTSP URL and toggles.
        config = client.get(f"/api/v1/nodes/{node_id}/config", headers=node_headers).json()
        assert config["cameras"][0]["source_uri"] == CAMERA["source_uri"]
        assert config["cameras"][0]["vision_enabled"] is False

        # Opening the live view marks the camera as watched, so the Node uploads frames every second.
        waiting = client.get(f"/api/v1/cameras/{camera_id}/snapshot", headers=user)
        assert waiting.headers["x-campex-frame"] == "waiting"
        idle = _report(client, node_headers, camera_id)
        assert idle["cameras"][camera_id]["live"] is True
        assert idle["next_upload_seconds"] == 1.0

        _report(client, node_headers, camera_id, width=64, height=48, frame_jpeg_base64=_jpeg())
        snapshot = client.get(f"/api/v1/cameras/{camera_id}/snapshot", headers=user)
        assert snapshot.headers["x-campex-frame"] == "live"
        assert snapshot.content.startswith(b"\xff\xd8")
        health = client.get(f"/api/v1/cameras/{camera_id}/health", headers=user).json()
        assert health["status"] == "ONLINE"
        assert health["resolution"] == {"width": 64, "height": 48}
        listed = client.get("/api/v1/cameras", headers=user).json()
        assert listed[0]["status"] == "ONLINE"

        # Vision: the dashboard toggle reaches the Node, the Node reports detections.
        started = client.post(f"/api/v1/cameras/{camera_id}/vision/start", headers=user).json()
        assert started["status"] == "STARTING"
        flags = _report(client, node_headers, camera_id)
        assert flags["cameras"][camera_id]["vision_enabled"] is True
        person = {"track_id": 1, "class_name": "person", "confidence": 0.9, "bounding_box": [4, 4, 30, 40]}
        _report(
            client,
            node_headers,
            camera_id,
            vision={"status": "RUNNING", "detector": "yolo", "vision_fps": 2.5, "frames_processed": 7},
            objects=[person],
        )
        status = client.get(f"/api/v1/cameras/{camera_id}/vision/status", headers=user).json()
        assert status["status"] == "RUNNING"
        # Reports without a frame keep the source size used to scale the overlay.
        health = client.get(f"/api/v1/cameras/{camera_id}/health", headers=user).json()
        assert health["resolution"] == {"width": 64, "height": 48}
        assert status["metrics"]["objects_detected"] == 1
        assert client.get(f"/api/v1/cameras/{camera_id}/vision/objects", headers=user).json() == [person]
        overlay = client.get(f"/api/v1/cameras/{camera_id}/snapshot?overlay=true", headers=user)
        assert overlay.content.startswith(b"\xff\xd8")

        # Mapping: requested in the dashboard, confirmed by the Node with poses.
        mapping = client.post(f"/api/v1/cameras/{camera_id}/mapping/start", headers=user).json()
        assert mapping["components"]["mapping"]["state"] == "LOADING"
        pose = {
            "pose_id": 1,
            "confidence": 0.8,
            "bounding_box": [4, 4, 30, 40],
            "keypoints": [{"name": "nose", "x": 10, "y": 8, "confidence": 0.9}],
        }
        _report(
            client,
            node_headers,
            camera_id,
            vision={"status": "RUNNING", "mapping_status": "ACTIVE"},
            objects=[person],
            poses=[pose],
        )
        status = client.get(f"/api/v1/cameras/{camera_id}/vision/status", headers=user).json()
        assert status["components"]["mapping"] == {"state": "ACTIVE", "poses": 1, "error": None}
        assert client.get(f"/api/v1/cameras/{camera_id}/mapping/poses", headers=user).json() == [pose]

        stopped = client.post(f"/api/v1/cameras/{camera_id}/vision/stop", headers=user).json()
        assert stopped["status"] == "STOPPED"
        assert _report(client, node_headers, camera_id)["cameras"][camera_id]["vision_enabled"] is False


def test_connection_test_runs_on_the_node(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)

    with TestClient(app) as client:
        user = _login(client, "ana@empresa.com")
        _, node_headers = _pair_node(client, user, "node_public_teste")

        job = client.post(
            "/api/v1/cameras/test-source",
            headers=user,
            json={"source_type": "rtsp", "source_uri": CAMERA["source_uri"]},
        ).json()
        assert job["pending"] is True
        assert client.get(f"/api/v1/cameras/test-source/{job['job_id']}", headers=user).json()["pending"] is True

        relayed = client.post("/api/v1/node-sync/cameras/live", headers=node_headers, json={"cameras": []}).json()
        assert relayed["test_jobs"] == [{"id": job["job_id"], "source_uri": CAMERA["source_uri"]}]

        result = {"ok": True, "success": True, "status": "ONLINE", "resolution": {"width": 1920, "height": 1080}}
        client.post(
            "/api/v1/node-sync/cameras/live",
            headers=node_headers,
            json={"cameras": [], "test_results": [{"job_id": job["job_id"], "result": result}]},
        )
        done = client.get(f"/api/v1/cameras/test-source/{job['job_id']}", headers=user).json()
        assert done["pending"] is False
        assert done["success"] is True
        assert done["resolution"] == {"width": 1920, "height": 1080}


def test_connection_test_without_node_explains_what_is_missing(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)

    with TestClient(app) as client:
        user = _login(client, "ana@empresa.com")
        result = client.post(
            "/api/v1/cameras/test-source",
            headers=user,
            json={"source_type": "rtsp", "source_uri": CAMERA["source_uri"]},
        ).json()

        assert result["success"] is False
        assert "Nenhum CAMPEX Node conectado" in result["error"]


def test_node_cannot_report_cameras_of_another_organization(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)

    with TestClient(app) as client:
        ana = _login(client, "ana@empresa.com")
        carlos = _login(client, "carlos@outra.com")
        _pair_node(client, ana, "node_public_ana")
        _, carlos_node = _pair_node(client, carlos, "node_public_carlos")
        camera_id = client.post("/api/v1/cameras", headers=ana, json=CAMERA).json()["id"]

        response = _report(client, carlos_node, camera_id, frame_jpeg_base64=_jpeg())

        assert camera_id not in response["cameras"]
        health = client.get(f"/api/v1/cameras/{camera_id}/health", headers=ana).json()
        assert health["status"] == "OFFLINE"
