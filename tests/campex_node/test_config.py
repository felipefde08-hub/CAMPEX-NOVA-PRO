from __future__ import annotations

import json

from campex_node.core.config import NodeSettings


def test_node_settings_loads_cameras_from_json(monkeypatch, tmp_path):
    monkeypatch.setenv("CAMPEX_NODE_DATA_DIR", str(tmp_path / "node"))
    monkeypatch.setenv(
        "CAMPEX_NODE_CAMERAS_JSON",
        json.dumps(
            [
                {
                    "id": "cam_1",
                    "name": "Camera 1",
                    "rtsp_url": "rtsp://user:pass@192.168.1.10/stream",
                }
            ]
        ),
    )

    settings = NodeSettings.from_env()

    assert settings.database_path == (tmp_path / "node" / "node.sqlite3").resolve()
    assert len(settings.cameras) == 1
    assert settings.cameras[0].id == "cam_1"
    assert settings.cameras[0].rtsp_url.startswith("rtsp://")
