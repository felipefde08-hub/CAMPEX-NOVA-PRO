from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]


def _load_dotenv() -> None:
    env_path = ROOT_DIR / ".env"
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        item = line.strip()
        if not item or item.startswith("#") or "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


_load_dotenv()


def _env_float(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value == "":
        return default
    return float(raw_value)


def _env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value == "":
        return default
    return int(raw_value)


def _env_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value == "":
        return default
    return raw_value.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class NodeCameraConfig:
    id: str
    name: str
    rtsp_url: str
    enabled: bool = True

    @classmethod
    def from_mapping(cls, item: dict[str, Any]) -> "NodeCameraConfig":
        camera_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or camera_id).strip()
        rtsp_url = str(
            item.get("rtsp_url") or item.get("source_uri") or item.get("url") or ""
        ).strip()
        if not camera_id:
            raise ValueError("Camera config must include id.")
        if not name:
            raise ValueError(f"Camera {camera_id} must include name.")
        if not rtsp_url:
            raise ValueError(f"Camera {camera_id} must include rtsp_url.")
        return cls(
            id=camera_id,
            name=name,
            rtsp_url=rtsp_url,
            enabled=bool(item.get("enabled", True)),
        )


@dataclass(frozen=True)
class NodeSettings:
    environment: str
    version: str
    log_level: str
    data_dir: Path
    database_path: Path
    node_id_file: Path
    cloud_url: str | None = None
    cloud_token: str | None = None
    cloud_api_token: str | None = None
    organization_id: str | None = None
    cloud_timeout_seconds: float = 10.0
    config_sync_interval_seconds: float = 15.0
    heartbeat_interval_seconds: float = 30.0
    camera_reconnect_seconds: float = 5.0
    camera_read_failure_limit: int = 3
    camera_open_timeout_ms: int = 3000
    camera_read_timeout_ms: int = 3000
    cameras: tuple[NodeCameraConfig, ...] = field(default_factory=tuple)

    @classmethod
    def from_env(cls) -> "NodeSettings":
        data_dir = Path(os.getenv("CAMPEX_NODE_DATA_DIR", "./storage/campex_node")).resolve()
        database_path = Path(
            os.getenv("CAMPEX_NODE_DATABASE", str(data_dir / "node.sqlite3"))
        ).resolve()
        node_id_file = Path(
            os.getenv("CAMPEX_NODE_ID_FILE", str(data_dir / "node_id"))
        ).resolve()
        return cls(
            environment=os.getenv("CAMPEX_NODE_ENV", os.getenv("CAMPEX_ENV", "development")),
            version=os.getenv("CAMPEX_NODE_VERSION", "0.1.0"),
            log_level=os.getenv("CAMPEX_NODE_LOG_LEVEL", os.getenv("CAMPEX_LOG_LEVEL", "INFO")),
            data_dir=data_dir,
            database_path=database_path,
            node_id_file=node_id_file,
            cloud_url=(os.getenv("CAMPEX_NODE_CLOUD_URL") or "").rstrip("/") or None,
            cloud_token=os.getenv("CAMPEX_NODE_TOKEN") or None,
            cloud_api_token=(
                os.getenv("CAMPEX_NODE_CLOUD_API_TOKEN")
                or os.getenv("CAMPEXTOKEN")
                or os.getenv("CAMPEX_API_TOKEN")
                or None
            ),
            organization_id=os.getenv("CAMPEX_NODE_ORGANIZATION_ID") or None,
            cloud_timeout_seconds=_env_float("CAMPEX_NODE_CLOUD_TIMEOUT_SECONDS", 10.0),
            config_sync_interval_seconds=_env_float("CAMPEX_NODE_CONFIG_SYNC_SECONDS", 15.0),
            heartbeat_interval_seconds=_env_float("CAMPEX_NODE_HEARTBEAT_SECONDS", 30.0),
            camera_reconnect_seconds=_env_float("CAMPEX_NODE_CAMERA_RECONNECT_SECONDS", 5.0),
            camera_read_failure_limit=_env_int("CAMPEX_NODE_CAMERA_READ_FAILURE_LIMIT", 3),
            camera_open_timeout_ms=_env_int("CAMPEX_NODE_CAMERA_OPEN_TIMEOUT_MS", 3000),
            camera_read_timeout_ms=_env_int("CAMPEX_NODE_CAMERA_READ_TIMEOUT_MS", 3000),
            cameras=tuple(_load_camera_configs()),
        )

    def __post_init__(self) -> None:
        if self.heartbeat_interval_seconds <= 0:
            raise ValueError("CAMPEX_NODE_HEARTBEAT_SECONDS must be greater than zero.")
        if self.config_sync_interval_seconds <= 0:
            raise ValueError("CAMPEX_NODE_CONFIG_SYNC_SECONDS must be greater than zero.")
        if self.camera_reconnect_seconds < 0:
            raise ValueError("CAMPEX_NODE_CAMERA_RECONNECT_SECONDS must be non-negative.")
        if self.camera_read_failure_limit < 1:
            raise ValueError("CAMPEX_NODE_CAMERA_READ_FAILURE_LIMIT must be at least 1.")
        if self.camera_open_timeout_ms <= 0 or self.camera_read_timeout_ms <= 0:
            raise ValueError("Camera timeout values must be greater than zero.")


def _load_camera_configs() -> list[NodeCameraConfig]:
    raw_json = os.getenv("CAMPEX_NODE_CAMERAS_JSON")
    if raw_json:
        payload = json.loads(raw_json)
        if not isinstance(payload, list):
            raise ValueError("CAMPEX_NODE_CAMERAS_JSON must be a JSON array.")
        return [NodeCameraConfig.from_mapping(item) for item in payload]

    file_path = os.getenv("CAMPEX_NODE_CAMERAS_FILE")
    if file_path:
        payload = json.loads(Path(file_path).read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("CAMPEX_NODE_CAMERAS_FILE must contain a JSON array.")
        return [NodeCameraConfig.from_mapping(item) for item in payload]

    return []
