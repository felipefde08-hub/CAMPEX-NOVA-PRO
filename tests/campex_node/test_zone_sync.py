from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.zones.repository import ZoneRepository

from campex_node.cloud.config_sync import ConfigSyncService
from campex_node.core.config import NodeSettings
from campex_node.vision.service import NodeVisionService


FULL_FRAME = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]


@dataclass
class FakeResult:
    ok: bool
    data: dict | None = None
    error: str | None = None
    status_code: int | None = 200


class FakeCloudClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def fetch_config(self) -> FakeResult:
        return FakeResult(ok=True, data=self.payload)


class FakeCameraManager:
    def __init__(self) -> None:
        self.applied: list = []

    def apply_configs(self, cameras) -> None:
        self.applied = list(cameras)


def _settings(tmp_path) -> NodeSettings:
    return NodeSettings(
        environment="test",
        version="0.1.0",
        log_level="INFO",
        data_dir=tmp_path,
        database_path=tmp_path / "node.sqlite3",
        node_id_file=tmp_path / "node_id",
    )


def _zone(zone_id: str, camera_id: str = "cam_1", **overrides) -> dict:
    zone = {
        "id": zone_id,
        "camera_id": camera_id,
        "name": f"Zona {zone_id}",
        "type": "restricted",
        "enabled": True,
        "points": FULL_FRAME,
        "created_at": "2026-09-29T18:00:00+00:00",
        "updated_at": "2026-09-29T18:00:00+00:00",
    }
    zone.update(overrides)
    return zone


def _config_sync(tmp_path, payload: dict, on_zones) -> ConfigSyncService:
    return ConfigSyncService(
        settings=_settings(tmp_path),
        cloud_client=FakeCloudClient(payload),
        camera_manager=FakeCameraManager(),
        on_zones=on_zones,
    )


def test_config_sync_passes_cloud_zones_to_callback(tmp_path):
    received: list = []
    payload = {
        "cameras": [{"id": "cam_1", "name": "Doca", "source_uri": "rtsp://192.168.1.20/s"}],
        "zones": [_zone("zone_a")],
    }

    _config_sync(tmp_path, payload, received.append).sync_once()

    assert received == [[_zone("zone_a")]]


def test_config_sync_skips_callback_when_cloud_sends_no_zones_key(tmp_path):
    received: list = []

    _config_sync(tmp_path, {"cameras": []}, received.append).sync_once()

    assert received == []


def test_config_sync_reports_zone_callback_failure(tmp_path):
    def fail(zones):
        raise RuntimeError("disk full")

    service = _config_sync(tmp_path, {"cameras": [], "zones": []}, fail)
    service.sync_once()

    assert "Zone sync failed" in service.last_error


def test_vision_service_database_lives_in_node_data_dir(tmp_path):
    service = NodeVisionService(_settings(tmp_path))

    assert service.database_path == (tmp_path / "vision.sqlite3").resolve()
    assert service.vision_settings.runtime == "local"


def test_apply_zones_keeps_cloud_ids_and_is_readable_by_zone_repository(tmp_path):
    service = NodeVisionService(_settings(tmp_path))
    service.initialize()

    stored = service.apply_zones([_zone("zone_a"), _zone("zone_b", camera_id="cam_2", type="monitored")])

    assert stored == 2
    repository = ZoneRepository(service.vision_settings)
    zones = repository.list("cam_1")
    assert [zone.id for zone in zones] == ["zone_a"]
    assert zones[0].type == "restricted"
    assert [point.as_list() for point in zones[0].points] == FULL_FRAME
    assert [zone.id for zone in repository.list("cam_2")] == ["zone_b"]


def test_apply_zones_replaces_previous_zones(tmp_path):
    service = NodeVisionService(_settings(tmp_path))
    service.initialize()
    service.apply_zones([_zone("zone_old")])

    service.apply_zones([_zone("zone_new", enabled=False)])

    zones = ZoneRepository(service.vision_settings).list("cam_1")
    assert [zone.id for zone in zones] == ["zone_new"]
    assert zones[0].enabled is False


def test_apply_zones_skips_invalid_zones_without_dropping_valid_ones(tmp_path):
    service = NodeVisionService(_settings(tmp_path))
    service.initialize()

    stored = service.apply_zones(
        [
            _zone("zone_ok"),
            _zone("bad_type", type="forbidden"),
            _zone("bad_points", points=[[0.0, 0.0], [1.0, 1.0]]),
            _zone("out_of_frame", points=[[0.0, 0.0], [2.0, 0.0], [1.0, 1.0]]),
            _zone("no_dates", created_at=None, updated_at=None),
            "not-a-zone",
        ]
    )

    assert stored == 2
    ids = {zone.id for zone in ZoneRepository(service.vision_settings).list("cam_1")}
    assert ids == {"zone_ok", "no_dates"}
