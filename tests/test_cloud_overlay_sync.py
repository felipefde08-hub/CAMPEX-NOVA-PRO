"""The Cloud draws a report's boxes only on the frame they were detected on."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np
from fastapi.testclient import TestClient

from backend.main import app

from tests.test_cloud_cameras import CAMERA, _configure, _login, _pair_node, _report

PERSON = {"track_id": 1, "class_name": "person", "confidence": 0.9, "bounding_box": [4, 4, 30, 40]}
OTHER = {"track_id": 2, "class_name": "person", "confidence": 0.8, "bounding_box": [40, 4, 60, 40]}
BASE_TIME = datetime.now(timezone.utc)


def _jpeg(value: int) -> str:
    """A 64x48 frame whose brightness tells frames apart."""
    ok, encoded = cv2.imencode(".jpg", np.full((48, 64, 3), value, dtype=np.uint8))
    assert ok
    return base64.b64encode(encoded.tobytes()).decode("ascii")


def _ref(frame_id: int, session: str = "s1", seconds: float | None = None) -> dict:
    at = BASE_TIME + timedelta(seconds=frame_id / 10 if seconds is None else seconds)
    return {"session_id": session, "frame_id": frame_id, "frame_at": at.isoformat(), "width": 64, "height": 48}


def _brightness(jpeg: bytes) -> int:
    return int(cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE).mean())


class _Camera:
    def __init__(self, client, user, node_headers, camera_id):
        self.client, self.user, self.node, self.id = client, user, node_headers, camera_id

    def report(self, **fields):
        return _report(self.client, self.node, self.id, vision={"status": "RUNNING"}, **fields)

    def frame(self, frame_id: int, value: int, objects=(), session: str = "s1", seconds=None, paired=True):
        ref = _ref(frame_id, session, seconds)
        fields = {"frame_jpeg_base64": _jpeg(value), "width": 64, "height": 48, "frame_ref": ref, "objects": list(objects)}
        if paired:
            fields["vision_frame"] = ref
        return self.report(**fields)

    def snapshot(self, overlay: bool) -> bytes:
        query = "?overlay=true" if overlay else ""
        response = self.client.get(f"/api/v1/cameras/{self.id}/snapshot{query}", headers=self.user)
        assert response.headers["x-campex-frame"] == "live"
        return response.content

    def has_boxes(self) -> bool:
        return self.snapshot(overlay=True) != self.snapshot(overlay=False)

    def objects(self):
        return self.client.get(f"/api/v1/cameras/{self.id}/vision/objects", headers=self.user).json()


def _watched_camera(client) -> _Camera:
    user = _login(client, "ana@empresa.com")
    _node_id, node_headers = _pair_node(client, user, "node_public_doca")
    camera_id = client.post("/api/v1/cameras", headers=user, json={**CAMERA, "vision_enabled": True}).json()["id"]
    client.get(f"/api/v1/cameras/{camera_id}/snapshot", headers=user)  # a viewer is waiting
    return _Camera(client, user, node_headers, camera_id)


def test_boxes_are_drawn_only_with_the_objects_of_the_stored_frame(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    with TestClient(app) as client:
        camera = _watched_camera(client)
        camera.frame(10, value=90, objects=[PERSON])
        with_person = camera.snapshot(overlay=True)
        assert camera.has_boxes()

        # A newer analysis reported without a frame updates the live objects
        # but must not be drawn over the older stored image.
        camera.report(objects=[OTHER], vision_frame=_ref(11))

        assert camera.objects() == [OTHER]
        assert camera.snapshot(overlay=True) == with_person


def test_frames_arriving_late_or_twice_do_not_replace_newer_frames(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    with TestClient(app) as client:
        camera = _watched_camera(client)
        camera.frame(10, value=90, objects=[PERSON])
        camera.frame(9, value=30, objects=[OTHER])  # delayed: older than what is stored
        assert abs(_brightness(camera.snapshot(overlay=False)) - 90) <= 2
        camera.frame(10, value=200, objects=[OTHER])  # duplicate of frame 10
        assert abs(_brightness(camera.snapshot(overlay=False)) - 90) <= 2
        assert camera.objects() == [OTHER]  # same analysis id: objects may be refreshed

        camera.frame(11, value=150, objects=[PERSON])
        assert abs(_brightness(camera.snapshot(overlay=False)) - 150) <= 2


def test_objects_of_an_older_analysis_do_not_replace_newer_objects(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    with TestClient(app) as client:
        camera = _watched_camera(client)
        camera.report(objects=[PERSON], vision_frame=_ref(10))
        camera.report(objects=[OTHER], vision_frame=_ref(9))

        assert camera.objects() == [PERSON]


def test_a_frame_without_its_own_objects_is_shown_without_boxes(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    with TestClient(app) as client:
        camera = _watched_camera(client)
        # Objects of analysis 7 arrive with the live camera frame 8.
        camera.frame(8, value=90, objects=[PERSON], paired=False)
        camera.report(objects=[PERSON], vision_frame=_ref(7))

        assert camera.objects() == [PERSON]
        assert not camera.has_boxes()


def test_older_nodes_without_frame_identity_get_no_boxes(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    with TestClient(app) as client:
        camera = _watched_camera(client)
        camera.report(frame_jpeg_base64=_jpeg(90), width=64, height=48, objects=[PERSON])

        assert camera.objects() == [PERSON]  # operational data still flows
        assert not camera.has_boxes()  # but they may belong to another frame


def test_a_restarted_camera_session_replaces_the_old_frames(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    with TestClient(app) as client:
        camera = _watched_camera(client)
        camera.frame(500, value=90, objects=[PERSON], session="before", seconds=5)

        # A late message from an earlier session is still older than what is stored...
        camera.frame(800, value=30, objects=[OTHER], session="earlier", seconds=1)
        assert abs(_brightness(camera.snapshot(overlay=False)) - 90) <= 2

        # ...while the restarted camera restarts its frame ids at 1.
        camera.frame(1, value=150, objects=[OTHER], session="after", seconds=6)
        assert abs(_brightness(camera.snapshot(overlay=False)) - 150) <= 2
        assert camera.has_boxes()
