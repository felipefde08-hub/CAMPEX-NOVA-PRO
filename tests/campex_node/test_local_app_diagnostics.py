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
