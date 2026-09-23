from __future__ import annotations

from dataclasses import replace

from campex_node.local_app import LocalNodeRuntime
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
        "packaging/linux/campex-node.service",
        "packaging/linux/install_systemd.sh",
        "packaging/windows/install_task.ps1",
    ]
    forbidden = ["CAMPEX_NODE_TOKEN=", "CAMPEX_API_TOKEN=", "CAMPEXTOKEN=", "rtsp://"]
    for path in paths:
        text = open(path, encoding="utf-8").read()
        for item in forbidden:
            assert item not in text
