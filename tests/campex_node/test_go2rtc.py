from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import replace

import pytest
import yaml
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from campex_node.cameras.manager import CameraManager
from campex_node.core.config import NodeCameraConfig
from campex_node.local_app import create_app
from campex_node.streaming.go2rtc import Go2rtcRelay, find_binary, render_config, stream_name

from tests.campex_node.test_camera_worker import make_settings


CAMERA = NodeCameraConfig(id="local_cam 1", name="Prensa", rtsp_url="rtsp://admin:s3nha@192.168.1.10/live")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _relay_settings(tmp_path, **overrides):
    api_port, rtsp_port = _free_port(), _free_port()
    return replace(
        make_settings(tmp_path),
        go2rtc_enabled=True,
        go2rtc_api_port=api_port,
        go2rtc_rtsp_port=rtsp_port,
        **overrides,
    )


def test_stream_name_is_url_safe_and_changes_with_the_camera_url():
    name = stream_name(CAMERA)
    assert name.startswith("local_cam_1_")
    assert stream_name(CAMERA) == name
    assert stream_name(replace(CAMERA, rtsp_url="rtsp://admin:s3nha@192.168.1.10/other")) != name
    assert stream_name(replace(CAMERA, name="Renamed")) == name


def test_config_keeps_go2rtc_local_password_protected_and_without_command_modules():
    config = yaml.safe_load(
        render_config(api_port=8788, rtsp_port=8789, password="p@ss", streams={"cam_a": "rtsp://u:p@10.0.0.2/x?a=1&b=2"})
    )
    assert config["api"]["listen"] == "127.0.0.1:8788"
    assert config["rtsp"]["listen"] == "127.0.0.1:8789"
    assert config["api"]["local_auth"] is True and config["api"]["password"] == "p@ss"
    assert set(config["api"]["allow_paths"]) == {"/api", "/api/streams", "/api/ws"}
    assert not {"exec", "echo", "expr", "ffmpeg", "webrtc"} & set(config["app"]["modules"])
    assert config["streams"] == {"cam_a": "rtsp://u:p@10.0.0.2/x?a=1&b=2"}


def test_without_go2rtc_cameras_keep_their_own_url(tmp_path):
    disabled = Go2rtcRelay(make_settings(tmp_path))
    disabled.start([CAMERA])
    assert disabled.status == "DISABLED"
    assert disabled.source_url(CAMERA) == CAMERA.rtsp_url
    assert disabled.summary()["live_mode"] == "mjpeg"

    missing = Go2rtcRelay(_relay_settings(tmp_path), binary=None)
    missing.binary = None
    missing.start([CAMERA])
    assert missing.status == "MISSING"
    assert missing.source_url(CAMERA) == CAMERA.rtsp_url
    assert missing.live_upstream(CAMERA) is None


class RecordingRelay:
    def __init__(self):
        self.synced = []

    def start(self, cameras):
        self.synced.append([camera.id for camera in cameras])

    def sync(self, cameras):
        self.synced.append([camera.id for camera in cameras])

    def stop(self):
        pass

    def source_url(self, camera):
        return f"rtsp://127.0.0.1:8789/{camera.id}"


def test_camera_manager_syncs_the_relay_before_workers_open_its_restream(tmp_path, monkeypatch):
    from campex_node.cameras import manager as manager_module
    from tests.campex_node.test_camera_worker import CountingWorker

    CountingWorker.instances = []
    monkeypatch.setattr(manager_module, "CameraWorker", CountingWorker)
    relay = RecordingRelay()
    manager = CameraManager(replace(make_settings(tmp_path), cameras=(CAMERA,)), relay=relay)
    manager.start()
    assert CountingWorker.instances[0].source_url == "rtsp://127.0.0.1:8789/local_cam 1"

    second = NodeCameraConfig(id="cam_2", name="Torno", rtsp_url="rtsp://10.0.0.3/s")
    manager.apply_configs([CAMERA, second])
    assert relay.synced == [["local_cam 1"], ["local_cam 1", "cam_2"]]
    assert manager.source_url(second) == "rtsp://127.0.0.1:8789/cam_2"


@pytest.fixture
def go2rtc_binary():
    binary = find_binary()
    if binary is None:
        pytest.skip("go2rtc binary not installed (python scripts/fetch_go2rtc.py)")
    return binary


def _streams(relay):
    return relay._request("GET", None) or {}


def test_go2rtc_runs_locked_down_follows_the_cameras_and_restarts(tmp_path, go2rtc_binary):
    settings = _relay_settings(tmp_path)
    relay = Go2rtcRelay(settings, binary=go2rtc_binary)
    relay.start([CAMERA])
    try:
        assert relay.status == "RUNNING", relay.error
        assert relay.source_url(CAMERA) == f"rtsp://127.0.0.1:{settings.go2rtc_rtsp_port}/{stream_name(CAMERA)}"
        assert set(_streams(relay)) == {stream_name(CAMERA)}

        # Even from this computer, go2rtc wants the Node's password.
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(f"http://127.0.0.1:{settings.go2rtc_api_port}/api/streams", timeout=3)
        assert refused.value.code == 401

        moved = replace(CAMERA, rtsp_url="rtsp://admin:s3nha@192.168.1.11/live")
        relay.sync([moved])
        assert set(_streams(relay)) == {stream_name(moved)}

        killed = relay._process
        killed.kill()
        killed.wait(timeout=5)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (relay.status == "RUNNING" and relay._process not in (None, killed)):
            time.sleep(0.2)
        assert relay.status == "RUNNING"
        assert set(_streams(relay)) == {stream_name(moved)}
    finally:
        process = relay._process
        relay.stop()
    assert process.poll() is not None
    assert relay.status == "STOPPED"


@contextmanager
def _node(monkeypatch, tmp_path):
    monkeypatch.setenv("CAMPEX_NODE_DATA_DIR", str(tmp_path / "node"))
    monkeypatch.setenv("CAMPEX_NODE_RECORDING_ENABLED", "false")
    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1:8787", client=("127.0.0.1", 50000)) as local, TestClient(
        app, base_url="http://192.168.0.20:8787", client=("192.168.0.31", 50001)
    ) as network:
        yield local, network


def _close_code(client, path, **kwargs) -> int:
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(path, **kwargs) as websocket:
            websocket.receive_bytes()
    return closed.value.code


def test_live_websocket_checks_origin_and_login(monkeypatch, tmp_path):
    with _node(monkeypatch, tmp_path) as (local, network):
        # The test client ignores base_url for WebSockets: give the full URL.
        path = "/api/cameras/local_cam/live"
        on_node, on_network = f"ws://127.0.0.1:8787{path}", f"ws://192.168.0.20:8787{path}"
        # Another site open in a browser on this computer cannot read the video.
        assert _close_code(local, on_node, headers={"origin": "http://evil.example"}) == 1008
        # The factory network needs a login.
        assert _close_code(network, on_network, headers={"origin": "http://192.168.0.20:8787"}) == 1008
        # Allowed, but without go2rtc there is no live stream: the panel uses snapshots.
        assert _close_code(local, on_node, headers={"origin": "http://127.0.0.1:8787"}) == 1013
        assert local.get("/api/status").json()["streaming"]["live_mode"] == "mjpeg"
