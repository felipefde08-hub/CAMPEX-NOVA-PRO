from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from campex_node import __version__


ROOT_DIR = Path(__file__).resolve().parents[2]
# Each release is published with a signed manifest; GitHub redirects
# "latest" to the newest non-prerelease release.
DEFAULT_UPDATE_MANIFEST_URL = (
    "https://github.com/felipefde08-hub/CAMPEX-NOVA-PRO/releases/latest/download/campex-node-manifest.json"
)


def _is_desktop_runtime() -> bool:
    return os.getenv("CAMPEX_DESKTOP_MODE", "").lower() in {"1", "true", "yes", "on"} or bool(
        getattr(sys, "frozen", False)
    )


def _default_data_dir() -> Path:
    configured = os.getenv("CAMPEX_NODE_DATA_DIR") or os.getenv("CAMPEX_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if _is_desktop_runtime():
        if os.name == "nt":
            base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            return (Path(base) / "CAMPEX" / "node").resolve()
        return (Path.home() / ".local" / "share" / "campex" / "node").resolve()
    return (ROOT_DIR / "storage" / "campex_node").resolve()


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
    vision_enabled: bool = False
    mapping_enabled: bool = False

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
            vision_enabled=bool(item.get("vision_enabled", False)),
            mapping_enabled=bool(item.get("mapping_enabled", False)),
        )


@dataclass(frozen=True)
class NodeSettings:
    environment: str
    version: str
    log_level: str
    data_dir: Path
    database_path: Path
    node_id_file: Path
    node_id: str | None = None
    cloud_url: str | None = None
    cloud_token: str | None = None
    cloud_api_token: str | None = None
    organization_id: str | None = None
    cloud_timeout_seconds: float = 10.0
    config_sync_interval_seconds: float = 15.0
    sync_interval_seconds: float = 10.0
    telemetry_interval_seconds: float = 60.0
    heartbeat_interval_seconds: float = 30.0
    camera_reconnect_seconds: float = 5.0
    camera_read_failure_limit: int = 3
    camera_open_timeout_ms: int = 10000
    camera_read_timeout_ms: int = 6000
    # Minimum time between two analyses of the same camera: at most
    # 1/interval inferences per second per camera (0.2 s -> 5 FPS).
    vision_interval_seconds: float = 0.2
    # Share of wall time the inference thread may keep the CPU busy; after
    # each inference it rests long enough to stay under it, so capture,
    # recording and the panel keep CPU even when cameras outnumber the CPU.
    vision_max_busy_ratio: float = 0.75
    # Analysed frames waiting for the event rules and observers. When full the
    # inference waits (back-pressure): events are never dropped.
    vision_post_queue_size: int = 8
    vision_confidence: float = 0.35
    # Ultralytics weights: the detector ships with the package; the pose model
    # is downloaded into <data_dir>/models on first use when it is missing.
    vision_model: str = "yolo11n.pt"
    pose_model: str = "yolo11n-pose.pt"
    vision_input_size: int = 640
    # A frame older than this is a frozen stream, not the current scene.
    vision_stale_frame_seconds: float = 10.0
    # "edge": the Node's own tracker; "bytetrack": Roboflow's ByteTrack.
    vision_tracker: str = "edge"
    # go2rtc keeps one connection per camera and restreams it locally to the
    # vision worker, the recorder and the live view.
    go2rtc_enabled: bool = False
    go2rtc_path: str | None = None  # default: bundled binary, then PATH
    go2rtc_api_port: int = 8788
    go2rtc_rtsp_port: int = 8789
    # Continuous recording: the camera stream is remuxed into segments.
    recording_enabled: bool = True
    recording_dir: Path | None = None  # default: <data_dir>/recordings
    recording_segment_seconds: float = 300.0
    recording_retention_days: float = 30.0
    recording_min_free_gb: float = 10.0
    recording_max_gb: float = 0.0  # 0 = limited only by free space and age
    # Clipes e imagens de eventos; os eventos em si não são apagados.
    evidence_retention_days: float = 90.0  # 0 = guardar para sempre
    machine_monitor_interval_seconds: float = 0.2
    # Daily copies of the Node database; default: <data_dir>/backups.
    backup_dir: Path | None = None
    backup_keep: int = 14
    live_frame_max_width: int = 960
    live_frame_jpeg_quality: int = 70
    outbound_max_pending: int = 5000
    auto_update_enabled: bool = True
    update_manifest_url: str = DEFAULT_UPDATE_MANIFEST_URL
    update_check_interval_seconds: float = 6 * 60 * 60
    cameras: tuple[NodeCameraConfig, ...] = field(default_factory=tuple)

    @property
    def recordings_path(self) -> Path:
        return self.recording_dir or self.data_dir / "recordings"

    @property
    def backups_path(self) -> Path:
        return self.backup_dir or self.data_dir / "backups"

    @classmethod
    def from_env(cls) -> "NodeSettings":
        data_dir = _default_data_dir()
        database_path = Path(
            os.getenv("CAMPEX_NODE_DATABASE", str(data_dir / "node.sqlite3"))
        ).resolve()
        node_id_file = Path(
            os.getenv("CAMPEX_NODE_ID_FILE", str(data_dir / "node_id"))
        ).resolve()
        return cls(
            environment=os.getenv("CAMPEX_NODE_ENV", os.getenv("CAMPEX_ENV", "development")),
            version=__version__,
            log_level=os.getenv("CAMPEX_NODE_LOG_LEVEL", os.getenv("CAMPEX_LOG_LEVEL", "INFO")),
            data_dir=data_dir,
            database_path=database_path,
            node_id_file=node_id_file,
            node_id=os.getenv("CAMPEX_NODE_ID") or None,
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
            sync_interval_seconds=_env_float("CAMPEX_NODE_SYNC_SECONDS", 10.0),
            telemetry_interval_seconds=_env_float("CAMPEX_NODE_TELEMETRY_SECONDS", 60.0),
            heartbeat_interval_seconds=_env_float("CAMPEX_NODE_HEARTBEAT_SECONDS", 30.0),
            camera_reconnect_seconds=_env_float("CAMPEX_NODE_CAMERA_RECONNECT_SECONDS", 5.0),
            camera_read_failure_limit=_env_int("CAMPEX_NODE_CAMERA_READ_FAILURE_LIMIT", 3),
            camera_open_timeout_ms=_env_int("CAMPEX_NODE_CAMERA_OPEN_TIMEOUT_MS", 10000),
            camera_read_timeout_ms=_env_int("CAMPEX_NODE_CAMERA_READ_TIMEOUT_MS", 6000),
            vision_interval_seconds=_env_float("CAMPEX_NODE_VISION_INTERVAL_SECONDS", 0.2),
            vision_max_busy_ratio=_env_float("CAMPEX_NODE_VISION_MAX_BUSY_RATIO", 0.75),
            vision_post_queue_size=_env_int("CAMPEX_NODE_VISION_POST_QUEUE_SIZE", 8),
            vision_confidence=_env_float("CAMPEX_NODE_VISION_CONFIDENCE", 0.35),
            vision_model=os.getenv("CAMPEX_NODE_VISION_MODEL", "yolo11n.pt"),
            pose_model=os.getenv("CAMPEX_NODE_POSE_MODEL", "yolo11n-pose.pt"),
            vision_input_size=_env_int("CAMPEX_NODE_VISION_INPUT_SIZE", 640),
            vision_stale_frame_seconds=_env_float("CAMPEX_NODE_VISION_STALE_FRAME_SECONDS", 10.0),
            vision_tracker=os.getenv("CAMPEX_NODE_VISION_TRACKER", "edge").strip().lower() or "edge",
            go2rtc_enabled=_env_bool("CAMPEX_NODE_GO2RTC", False),
            go2rtc_path=os.getenv("CAMPEX_NODE_GO2RTC_PATH") or None,
            go2rtc_api_port=_env_int("CAMPEX_NODE_GO2RTC_API_PORT", 8788),
            go2rtc_rtsp_port=_env_int("CAMPEX_NODE_GO2RTC_RTSP_PORT", 8789),
            recording_enabled=_env_bool("CAMPEX_NODE_RECORDING_ENABLED", True),
            recording_dir=(
                Path(os.environ["CAMPEX_NODE_RECORDING_DIR"]).expanduser().resolve()
                if os.getenv("CAMPEX_NODE_RECORDING_DIR")
                else None
            ),
            recording_segment_seconds=_env_float("CAMPEX_NODE_RECORDING_SEGMENT_SECONDS", 300.0),
            recording_retention_days=_env_float("CAMPEX_NODE_RECORDING_RETENTION_DAYS", 30.0),
            recording_min_free_gb=_env_float("CAMPEX_NODE_RECORDING_MIN_FREE_GB", 10.0),
            recording_max_gb=_env_float("CAMPEX_NODE_RECORDING_MAX_GB", 0.0),
            evidence_retention_days=_env_float("CAMPEX_NODE_EVIDENCE_RETENTION_DAYS", 90.0),
            machine_monitor_interval_seconds=_env_float("CAMPEX_NODE_MACHINE_MONITOR_SECONDS", 0.2),
            backup_dir=(
                Path(os.environ["CAMPEX_NODE_BACKUP_DIR"]).expanduser().resolve()
                if os.getenv("CAMPEX_NODE_BACKUP_DIR")
                else None
            ),
            backup_keep=_env_int("CAMPEX_NODE_BACKUP_KEEP", 14),
            live_frame_max_width=_env_int("CAMPEX_NODE_LIVE_FRAME_MAX_WIDTH", 960),
            live_frame_jpeg_quality=_env_int("CAMPEX_NODE_LIVE_FRAME_JPEG_QUALITY", 70),
            outbound_max_pending=_env_int("CAMPEX_NODE_QUEUE_MAX_ITEMS", 5000),
            auto_update_enabled=_env_bool("CAMPEX_NODE_AUTO_UPDATE", True),
            update_manifest_url=os.getenv("CAMPEX_NODE_UPDATE_MANIFEST_URL", DEFAULT_UPDATE_MANIFEST_URL).strip(),
            update_check_interval_seconds=_env_float("CAMPEX_NODE_UPDATE_CHECK_HOURS", 6.0) * 60 * 60,
            cameras=tuple(_load_camera_configs()),
        )

    def __post_init__(self) -> None:
        if self.heartbeat_interval_seconds <= 0:
            raise ValueError("CAMPEX_NODE_HEARTBEAT_SECONDS must be greater than zero.")
        if self.config_sync_interval_seconds <= 0:
            raise ValueError("CAMPEX_NODE_CONFIG_SYNC_SECONDS must be greater than zero.")
        if self.sync_interval_seconds <= 0:
            raise ValueError("CAMPEX_NODE_SYNC_SECONDS must be greater than zero.")
        if self.telemetry_interval_seconds <= 0:
            raise ValueError("CAMPEX_NODE_TELEMETRY_SECONDS must be greater than zero.")
        if self.camera_reconnect_seconds < 0:
            raise ValueError("CAMPEX_NODE_CAMERA_RECONNECT_SECONDS must be non-negative.")
        if self.camera_read_failure_limit < 1:
            raise ValueError("CAMPEX_NODE_CAMERA_READ_FAILURE_LIMIT must be at least 1.")
        if self.camera_open_timeout_ms <= 0 or self.camera_read_timeout_ms <= 0:
            raise ValueError("Camera timeout values must be greater than zero.")
        if self.vision_interval_seconds <= 0:
            raise ValueError("CAMPEX_NODE_VISION_INTERVAL_SECONDS must be greater than zero.")
        if not 0.05 <= self.vision_max_busy_ratio <= 1.0:
            raise ValueError("CAMPEX_NODE_VISION_MAX_BUSY_RATIO must be between 0.05 and 1.")
        if self.vision_post_queue_size < 1:
            raise ValueError("CAMPEX_NODE_VISION_POST_QUEUE_SIZE must be at least 1.")
        if not 0.0 <= self.vision_confidence <= 1.0:
            raise ValueError("CAMPEX_NODE_VISION_CONFIDENCE must be between 0 and 1.")
        if self.evidence_retention_days < 0:
            raise ValueError("CAMPEX_NODE_EVIDENCE_RETENTION_DAYS must be zero (keep forever) or positive.")
        if self.vision_stale_frame_seconds <= 0:
            raise ValueError("CAMPEX_NODE_VISION_STALE_FRAME_SECONDS must be greater than zero.")
        if self.vision_tracker not in {"edge", "bytetrack"}:
            raise ValueError("CAMPEX_NODE_VISION_TRACKER must be 'edge' or 'bytetrack'.")
        for port in (self.go2rtc_api_port, self.go2rtc_rtsp_port):
            if not 1 <= port <= 65535:
                raise ValueError("go2rtc ports must be between 1 and 65535.")
        if self.go2rtc_api_port == self.go2rtc_rtsp_port:
            raise ValueError("CAMPEX_NODE_GO2RTC_API_PORT and CAMPEX_NODE_GO2RTC_RTSP_PORT must differ.")
        if self.recording_segment_seconds < 10:
            raise ValueError("CAMPEX_NODE_RECORDING_SEGMENT_SECONDS must be at least 10.")
        if self.recording_retention_days <= 0 or self.recording_min_free_gb < 0 or self.recording_max_gb < 0:
            raise ValueError("Recording retention limits must be positive.")
        if self.machine_monitor_interval_seconds <= 0:
            raise ValueError("CAMPEX_NODE_MACHINE_MONITOR_SECONDS must be greater than zero.")
        if self.outbound_max_pending < 1:
            raise ValueError("CAMPEX_NODE_QUEUE_MAX_ITEMS must be at least 1.")
        if self.update_check_interval_seconds < 60:
            raise ValueError("CAMPEX_NODE_UPDATE_CHECK_HOURS must be at least one minute.")


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
