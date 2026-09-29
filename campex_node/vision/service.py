from __future__ import annotations

import dataclasses
import importlib.metadata
import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable

from backend.config import ROOT_DIR, Settings, get_settings
from backend.database.db import connect, initialize_database

from campex_node.core.config import NodeSettings


logger = logging.getLogger("campex.node.vision")

ZONE_TYPES = {"monitored", "restricted"}
DEFAULT_POLL_SECONDS = 1.0
# Sent ahead of everything else in the outbox, including telemetry events (0).
DETECTION_EVENT_PRIORITY = -1

# Tracks which (event, status) pairs already went to the outbox. The events
# table's updated_at mixes isoformat (on create) and CURRENT_TIMESTAMP (on
# update), so it cannot be used as an ordered cursor.
FORWARDED_EVENTS_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS node_forwarded_events (
        event_id TEXT NOT NULL,
        status TEXT NOT NULL,
        forwarded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (event_id, status)
    )
    """,
)


def resolve_model_path(model: str) -> Path:
    """Absolute model path; relative names resolve against the project root.

    An absolute path also stops ultralytics from trying to download a model by
    name when the file is missing, which would fail offline.
    """
    path = Path(model).expanduser()
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path.resolve()


def build_vision_settings(node_settings: NodeSettings) -> Settings:
    """Backend settings for the Node's own vision database.

    VISION_* values still come from the environment/.env; only the database
    moves next to the Node's data so it never touches the cloud database, and
    the model is pinned to an absolute path so the start directory is irrelevant.
    """
    base = get_settings()
    database_path = (Path(node_settings.data_dir) / "vision.sqlite3").resolve()
    return dataclasses.replace(
        base,
        database_url=f"sqlite:///{database_path.as_posix()}",
        runtime="local",
        vision_model=str(resolve_model_path(base.vision_model)),
    )


def _default_engine_factory(settings: Settings, camera_manager: Any) -> Any:
    from backend.vision.engine import VisionEngine

    return VisionEngine(settings, camera_manager)


class NodeVisionService:
    """Runs backend VisionEngine sessions on the Node's camera frame buffers.

    Construction is cheap (no threads, no model): the local app builds throwaway
    lifecycles while pairing. The engine only exists between start() and stop().
    """

    def __init__(
        self,
        settings: NodeSettings,
        camera_manager: Any = None,
        *,
        store: Any = None,
        engine_factory: Callable[[Settings, Any], Any] | None = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self.settings = settings
        self.camera_manager = camera_manager
        self.store = store
        self.vision_settings = build_vision_settings(settings)
        self._engine_factory = engine_factory or _default_engine_factory
        self._poll_seconds = poll_seconds
        self.engine: Any | None = None
        self.error: str | None = None
        self._sessions: set[str] = set()
        self._detector_reported = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def database_path(self) -> Path:
        return self.vision_settings.sqlite_path

    @property
    def active_camera_ids(self) -> set[str]:
        with self._lock:
            return set(self._sessions)

    def initialize(self) -> None:
        initialize_database(self.vision_settings)
        with connect(self.database_path) as connection:
            for statement in FORWARDED_EVENTS_SCHEMA:
                connection.execute(statement)
            connection.commit()

    def start(self) -> None:
        if self.engine is not None:
            return
        if not self.vision_settings.vision_enabled:
            logger.info("Vision disabled by VISION_ENABLED; Node runs without detection")
            return
        if self.camera_manager is None:
            raise RuntimeError("NodeVisionService.start requires a camera manager.")
        _warn_on_opencv_conflict()
        model_path = Path(self.vision_settings.vision_model)
        if self.vision_settings.vision_detector == "yolo" and not model_path.is_file():
            logger.warning(
                "YOLO model file not found at %s; detector will fall back", model_path
            )
        logger.info(
            "Starting Node vision",
            extra={
                "detector": self.vision_settings.vision_detector,
                "model": str(model_path),
                "database": str(self.database_path),
            },
        )
        self._stop.clear()
        self.engine = self._engine_factory(self.vision_settings, self.camera_manager)
        self.reconcile()
        self._thread = threading.Thread(
            target=self._run,
            name="campex-node-vision",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None
        engine, self.engine = self.engine, None
        with self._lock:
            self._sessions.clear()
        if engine is not None:
            engine.shutdown()
            try:
                self.forward_events()
            except Exception:
                logger.exception("Final vision event forward failed")

    def forward_events(self) -> int:
        """Queue every new (event, status) pair from the vision DB for the cloud.

        Outbox ids are "{event_id}:{status}" so an event's OPEN and CLOSED
        versions are separate items; the cloud upserts them by event_id.
        Returns the number of pairs queued.
        """
        if self.store is None:
            return 0
        with connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT e.* FROM events e
                LEFT JOIN node_forwarded_events f
                  ON f.event_id = e.id AND f.status = e.status
                WHERE f.event_id IS NULL
                ORDER BY e.created_at, e.id
                """
            ).fetchall()
            for row in rows:
                # Enqueue before marking: a crash in between only re-queues an
                # item the outbox already deduplicates by id.
                self.store.enqueue_event(
                    "event",
                    _event_payload(row),
                    event_id=f"{row['id']}:{row['status']}",
                    priority=DETECTION_EVENT_PRIORITY,
                )
                connection.execute(
                    "INSERT OR IGNORE INTO node_forwarded_events (event_id, status) VALUES (?, ?)",
                    (row["id"], row["status"]),
                )
            connection.commit()
        if rows:
            logger.info("Queued %s vision event update(s) for the cloud", len(rows))
        return len(rows)

    def desired_camera_ids(self) -> set[str]:
        """Enabled cameras, minus those the cloud explicitly marked vision off."""
        return {
            camera.id
            for camera in self.camera_manager.configs()
            if camera.enabled and camera.vision_enabled is not False
        }

    def reconcile(self) -> tuple[set[str], set[str]]:
        """Start/stop sessions so they follow the camera manager's configs."""
        engine = self.engine
        if engine is None:
            return set(), set()
        desired = self.desired_camera_ids()
        with self._lock:
            current = set(self._sessions)
        started = desired - current
        stopped = current - desired
        for camera_id in sorted(stopped):
            engine.stop_session(camera_id)
            with self._lock:
                self._sessions.discard(camera_id)
            logger.info("Vision session stopped", extra={"camera_id": camera_id})
        for camera_id in sorted(started):
            status = engine.start_session(camera_id)
            with self._lock:
                self._sessions.add(camera_id)
            logger.info(
                "Vision session started",
                extra={"camera_id": camera_id, "status": status.get("status")},
            )
        return started, stopped

    def status(self) -> list[dict]:
        engine = self.engine
        if engine is None:
            return []
        return [engine.status(camera_id) for camera_id in sorted(self.active_camera_ids)]

    def _run(self) -> None:
        while not self._stop.wait(self._poll_seconds):
            try:
                self.reconcile()
                self._report_detector_once()
                self.forward_events()
                self.error = None
            except Exception as exc:
                self.error = str(exc)
                logger.exception("Node vision loop failed")

    def _report_detector_once(self) -> None:
        """Log which detector actually runs, once it has finished loading."""
        if self._detector_reported:
            return
        for status in self.status():
            detector = (status.get("components") or {}).get("detector") or {}
            if detector.get("state") in (None, "LOADING"):
                continue
            self._detector_reported = True
            if status.get("detector_fallback"):
                logger.warning(
                    "\n%s\n[CAMPEX][NODE] !!! VISAO DO NODE EM FALLBACK: %s !!!\n"
                    "[CAMPEX][NODE] Motivo: %s\n%s",
                    "=" * 72,
                    detector.get("name"),
                    status.get("detector_fallback_reason"),
                    "=" * 72,
                )
            else:
                logger.info(
                    "Node vision detector active: %s (%s)",
                    detector.get("name"),
                    detector.get("model"),
                )
            return

    def apply_zones(self, zones: list[dict[str, Any]]) -> int:
        """Replace the local zones with the cloud's list, keeping cloud IDs.

        Invalid zones are skipped and logged so one bad polygon does not drop
        every other zone. Returns the number of zones stored.
        """
        rows = []
        for zone in zones:
            row = _zone_row(zone)
            if row is None:
                logger.warning(
                    "Skipping invalid zone from cloud config",
                    extra={"zone_id": zone.get("id") if isinstance(zone, dict) else None},
                )
                continue
            rows.append(row)
        with connect(self.database_path) as connection:
            connection.execute("DELETE FROM zones")
            connection.executemany(
                """
                INSERT INTO zones (
                    id, camera_id, name, type, enabled, points, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), COALESCE(?, CURRENT_TIMESTAMP))
                """,
                rows,
            )
            connection.commit()
        logger.info("Zone sync applied %s zone(s)", len(rows))
        return len(rows)


def _event_payload(row) -> dict[str, Any]:
    """Shape a vision DB event row as backend/api/node_sync.NodeEventPayload."""
    try:
        metadata = json.loads(row["metadata"]) if row["metadata"] else {}
    except (TypeError, ValueError):
        metadata = {}
    return {
        "event_id": row["id"],
        "camera_id": row["camera_id"],
        "event_type": row["type"],
        "severity": row["severity"],
        "status": row["status"],
        "timestamp": row["started_at"],
        "ended_at": row["ended_at"],
        "duration": row["duration"],
        "confidence": row["confidence"],
        "zone_id": row["zone_id"],
        "track_id": row["track_id"],
        "metadata": metadata if isinstance(metadata, dict) else {},
    }


def _warn_on_opencv_conflict() -> None:
    """opencv-python and opencv-python-headless both ship the cv2 module."""
    installed = set()
    for name in ("opencv-python", "opencv-python-headless"):
        try:
            importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            continue
        installed.add(name)
    if len(installed) == 2:
        logger.warning(
            "Both opencv-python and opencv-python-headless are installed; they "
            "overwrite each other's cv2. See campex_node/requirements.txt."
        )


def _zone_row(zone: Any) -> tuple | None:
    if not isinstance(zone, dict):
        return None
    zone_id = zone.get("id")
    camera_id = zone.get("camera_id")
    zone_type = zone.get("type")
    points = zone.get("points")
    if not zone_id or not camera_id or zone_type not in ZONE_TYPES:
        return None
    if not isinstance(points, list) or len(points) < 3:
        return None
    normalized: list[list[float]] = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return None
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            return None
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            return None
        normalized.append([x, y])
    created_at = zone.get("created_at") or zone.get("updated_at")
    updated_at = zone.get("updated_at") or created_at
    return (
        str(zone_id),
        str(camera_id),
        str(zone.get("name") or zone_id),
        zone_type,
        int(bool(zone.get("enabled", True))),
        json.dumps(normalized),
        created_at,
        updated_at,
    )
