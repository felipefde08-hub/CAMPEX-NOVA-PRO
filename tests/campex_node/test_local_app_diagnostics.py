from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from campex_node.local_app import LocalNodeRuntime, create_app
from campex_node.main import build_lifecycle


def test_local_node_diagnostics_do_not_expose_tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("CAMPEX_NODE_DATA_DIR", str(tmp_path / "node"))
    monkeypatch.setenv("CAMPEX_NODE_TOKEN", "secret-node-token")
    monkeypatch.setenv("CAMPEX_NODE_ID", "node_test")
    monkeypatch.setenv("CAMPEX_NODE_CLOUD_URL", "https://example.invalid/api/v1")

    runtime = LocalNodeRuntime.__new__(LocalNodeRuntime)
    settings = build_lifecycle().settings
    settings = replace(
        settings,
        data_dir=tmp_path / "node",
        database_path=tmp_path / "node" / "node.sqlite3",
        node_id_file=tmp_path / "node" / "node_id",
        cloud_token="secret-node-token",
        node_id="node_test",
        cloud_url="https://example.invalid/api/v1",
    )
    runtime.lifecycle = build_lifecycle(settings)
    runtime.lifecycle.initialize()

    diagnostics = runtime.diagnostics()
    text = str(diagnostics)

    assert diagnostics["paired"] is True
    assert diagnostics["cloud_configured"] is True
    assert "secret-node-token" not in text
    assert "cloud_token" not in text
    assert "node_token" not in text


def test_service_packaging_files_do_not_contain_secrets():
    paths = [
        "packaging/macos/install_launch_agent.sh",
        "packaging/macos/status_launch_agent.sh",
        "packaging/macos/build.sh",
        "packaging/linux/campex-node.service",
        "packaging/linux/install_systemd.sh",
        "packaging/windows/install_task.ps1",
    ]
    forbidden = ["CAMPEX_NODE_TOKEN=", "CAMPEX_API_TOKEN=", "CAMPEXTOKEN=", "rtsp://"]
    for path in paths:
        text = open(path, encoding="utf-8").read()
        for item in forbidden:
            assert item not in text


def test_local_node_allows_hosted_frontend_private_network_preflight(monkeypatch, tmp_path):
    monkeypatch.setenv("CAMPEX_NODE_DATA_DIR", str(tmp_path / "node"))
    app = create_app()

    with TestClient(app) as client:
        for path in (
            "/api/operations/summary",
            "/api/cameras",
            "/api/zones",
            "/api/health",
        ):
            response = client.options(
                path,
                headers={
                    "Origin": "https://campexfront.vercel.app",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "x-campex-token",
                    "Access-Control-Request-Private-Network": "true",
                },
            )

            assert response.status_code in {200, 204}
            assert response.headers["access-control-allow-origin"] == "https://campexfront.vercel.app"
            assert response.headers["access-control-allow-private-network"] == "true"


def test_local_node_snapshot_waits_with_jpeg_when_frame_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("CAMPEX_NODE_DATA_DIR", str(tmp_path / "node"))
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/cameras/local_missing/snapshot")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.headers["x-campex-frame"] == "waiting"
    assert response.content.startswith(b"\xff\xd8")


def test_node_panel_does_not_override_cloud_cameras(tmp_path):
    from types import SimpleNamespace

    from campex_node.core.config import NodeCameraConfig
    from campex_node.local_app import CloudManagedCameraError
    from campex_node.storage.local_store import LocalStore

    store = LocalStore(tmp_path / "node.sqlite3")
    store.initialize()
    store.set_meta(
        "cloud_cameras_json",
        '[{"id":"cam_cloud","name":"Doca","rtsp_url":"rtsp://10.0.0.5/stream"}]',
    )
    camera = NodeCameraConfig(id="cam_cloud", name="Doca", rtsp_url="rtsp://10.0.0.5/stream")
    runtime = LocalNodeRuntime.__new__(LocalNodeRuntime)
    runtime.lifecycle = SimpleNamespace(store=store, camera_manager=SimpleNamespace(configs=lambda: [camera]))

    for change in (
        lambda: runtime.set_camera_vision("cam_cloud", True),
        lambda: runtime.delete_camera("cam_cloud"),
    ):
        try:
            change()
        except CloudManagedCameraError:
            pass
        else:
            raise AssertionError("Cloud camera was changed from the Node panel")

    assert store.get_local_cameras() == []
