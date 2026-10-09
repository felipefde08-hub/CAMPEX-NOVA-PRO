from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections import deque
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

import cv2
import numpy as np

from backend.events.engine import EventEngine
from backend.events.models import Event, normalize_event_metadata
from backend.vision.evidence import EvidenceRecorder
from backend.vision.models import BoundingBox, Detection, TrackedObject
from backend.vision.tracker import ByteTrackTracker
from backend.zones.engine import SpatialEngine
from backend.zones.models import Zone, ZonePoint
from campex_node.factory import EVENT_ZONE_TYPES, ZONE_TYPES, normalize_zone_settings
from campex_node.storage.sqlite import add_column


logger = logging.getLogger("campex.node.events")

# Clip frames are downscaled to this width and kept as JPEG in the pre-roll
# buffer so a few seconds of 1080p per camera stay a few MB.
CLIP_MAX_WIDTH = 960
CLIP_PRE_ROLL_SECONDS = 5.0
CLIP_POST_ROLL_SECONDS = 3.0
CLIP_MAX_SECONDS = 120.0
# Vision can analyse faster than this, but every clip frame is VP8-encoded
# once per open clip (~30 ms each on a mid-range CPU): clips keep the rate
# they had when vision ran at 2-3 FPS and their cost no longer grows with it.
CLIP_MAX_FPS = 3.0
ZONE_CACHE_SECONDS = 2.0

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS zones (
        id TEXT PRIMARY KEY,
        camera_id TEXT NOT NULL,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        points TEXT NOT NULL,
        settings TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        camera_id TEXT NOT NULL,
        zone_id TEXT,
        track_id INTEGER,
        severity TEXT NOT NULL,
        status TEXT NOT NULL,
        confidence REAL,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        duration REAL,
        metadata TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_started_at ON events (started_at DESC)",
)


class NodeEventStore:
    """Zones and events in the Node's own SQLite file.

    Implements the subset of the backend ``EventRepository`` that
    ``EventEngine`` calls, so the engine runs unchanged on the Node.
    """

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.zones_version = 0

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            for statement in SCHEMA:
                connection.execute(statement)
            add_column(connection, "zones", "settings", "TEXT NOT NULL DEFAULT '{}'")
            # Open events from a previous run lost their tracking state; they
            # can never get an exit, so stop presenting them as ongoing.
            rows = connection.execute("SELECT id, metadata FROM events WHERE status = 'OPEN'").fetchall()
            for row in rows:
                metadata = _loads(row["metadata"])
                metadata.update(knowledge_state="UNKNOWN", uncertainty_reason="node_restarted")
                connection.execute(
                    "UPDATE events SET status = 'CLOSED', metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(metadata), _utc_now(), row["id"]),
                )
            connection.commit()

    # Zones

    def list_zones(self, camera_id: str | None = None) -> list[Zone]:
        with closing(self._connect()) as connection:
            if camera_id:
                rows = connection.execute(
                    "SELECT * FROM zones WHERE camera_id = ? ORDER BY created_at ASC", (camera_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM zones ORDER BY created_at ASC").fetchall()
        return [_row_to_zone(row) for row in rows]

    def zone_settings(self, camera_id: str | None = None) -> dict[str, dict[str, Any]]:
        """zone_id -> attributes for the zone's type (cost, operator rules...)."""
        with closing(self._connect()) as connection:
            if camera_id:
                rows = connection.execute(
                    "SELECT id, type, settings FROM zones WHERE camera_id = ?", (camera_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT id, type, settings FROM zones").fetchall()
        return {row["id"]: normalize_zone_settings(row["type"], _loads(row["settings"])) for row in rows}

    def zone_dict(self, zone: Zone) -> dict[str, Any]:
        settings = self.zone_settings(zone.camera_id).get(zone.id) or normalize_zone_settings(zone.type, None)
        return {**zone.as_dict(), "settings": settings}

    def get_zone(self, zone_id: str) -> Zone | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM zones WHERE id = ?", (zone_id,)).fetchone()
        return _row_to_zone(row) if row else None

    def create_zone(
        self,
        *,
        camera_id: str,
        name: str,
        zone_type: str,
        points: list[list[float]],
        enabled: bool = True,
        settings: dict[str, Any] | None = None,
    ) -> Zone:
        zone_id = f"zone_{uuid4().hex[:12]}"
        now = _utc_now()
        zone_type = _zone_type(zone_type)
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO zones (id, camera_id, name, type, enabled, points, settings, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    zone_id, camera_id, name, zone_type, int(enabled), _points_json(points, zone_type),
                    json.dumps(normalize_zone_settings(zone_type, settings)), now, now,
                ),
            )
            connection.commit()
        self.zones_version += 1
        return self.get_zone(zone_id)  # type: ignore[return-value]

    def update_zone(self, zone_id: str, updates: dict[str, Any]) -> Zone | None:
        current = self.get_zone(zone_id)
        if current is None:
            return None
        zone_type = _zone_type(updates.get("type") or current.type)
        columns = {
            "name": lambda value: value,
            "type": _zone_type,
            "enabled": lambda value: int(bool(value)),
            "points": lambda value: _points_json(value, zone_type),
        }
        fields = [(column, convert(updates[column])) for column, convert in columns.items() if column in updates]
        if "points" not in updates and zone_type != current.type:
            fields.append(("points", _points_json([point.as_list() for point in current.points], zone_type)))
        if "settings" in updates or zone_type != current.type:
            # A new type starts from its own defaults.
            base = self.zone_settings(current.camera_id).get(zone_id) if zone_type == current.type else None
            settings = normalize_zone_settings(zone_type, updates.get("settings"), base)
            fields.append(("settings", json.dumps(settings)))
        if fields:
            assignments = ", ".join(f"{column} = ?" for column, _ in fields)
            with closing(self._connect()) as connection:
                connection.execute(
                    f"UPDATE zones SET {assignments}, updated_at = ? WHERE id = ?",
                    (*(value for _, value in fields), _utc_now(), zone_id),
                )
                connection.commit()
            self.zones_version += 1
        return self.get_zone(zone_id)

    def delete_zone(self, zone_id: str) -> bool:
        with closing(self._connect()) as connection:
            cursor = connection.execute("DELETE FROM zones WHERE id = ?", (zone_id,))
            connection.commit()
        self.zones_version += 1
        return cursor.rowcount > 0

    # Events (EventRepository interface used by EventEngine)

    def create(
        self,
        *,
        event_type: str,
        camera_id: str,
        zone_id: str | None = None,
        track_id: int | None = None,
        severity: str = "info",
        status: str = "OPEN",
        confidence: float | None = None,
        metadata: dict | None = None,
        started_at: str | None = None,
        **_ignored: Any,
    ) -> Event:
        event_id = f"evt_{uuid4().hex[:12]}"
        now = _utc_now()
        started = started_at or now
        normalized = normalize_event_metadata(
            event_type=event_type,
            camera_id=camera_id,
            zone_id=zone_id,
            track_id=track_id,
            started_at=started,
            ended_at=None,
            duration=None,
            metadata=metadata,
        )
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO events (
                    id, type, camera_id, zone_id, track_id, severity, status, confidence,
                    started_at, ended_at, duration, metadata, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)
                """,
                (
                    event_id, event_type, camera_id, zone_id, track_id, severity, status,
                    confidence, started, json.dumps(normalized), now, now,
                ),
            )
            connection.commit()
        return self.get(event_id)  # type: ignore[return-value]

    def get(self, event_id: str) -> Event | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return _row_to_event(row) if row else None

    def update_status(
        self,
        event_id: str,
        status: str,
        ended_at: str | None = None,
        duration: float | None = None,
        metadata_update: dict | None = None,
    ) -> Event | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if row is None:
                return None
            next_ended_at = ended_at or row["ended_at"]
            next_duration = duration if duration is not None else row["duration"]
            metadata = _loads(row["metadata"])
            metadata.update(metadata_update or {})
            metadata = normalize_event_metadata(
                event_type=row["type"],
                camera_id=row["camera_id"],
                zone_id=row["zone_id"],
                track_id=row["track_id"],
                started_at=row["started_at"],
                ended_at=next_ended_at,
                duration=next_duration,
                metadata=metadata,
            )
            connection.execute(
                """
                UPDATE events
                SET status = ?, ended_at = ?, duration = ?, metadata = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, next_ended_at, next_duration, json.dumps(metadata), _utc_now(), event_id),
            )
            connection.commit()
        return self.get(event_id)

    def close(self, event_id: str, ended_at: str | None = None, duration: float | None = None) -> Event | None:
        return self.update_status(event_id, "CLOSED", ended_at, duration)

    def list_events(
        self,
        *,
        status: str | None = None,
        camera_id: str | None = None,
        event_type: str | None = None,
        limit: int = 200,
    ) -> list[Event]:
        conditions: list[str] = []
        params: list[Any] = []
        for column, value in (("status", status), ("camera_id", camera_id), ("type", event_type)):
            if value:
                conditions.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT * FROM events {where} ORDER BY started_at DESC LIMIT ?",
                (*params, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def events_between(self, start: datetime, end: datetime, event_type: str | None = None) -> list[Event]:
        """Events that started in [start, end), oldest first."""
        query = "SELECT * FROM events WHERE started_at >= ? AND started_at < ?"
        params: list[Any] = [start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()]
        if event_type:
            query += " AND type = ?"
            params.append(event_type)
        with closing(self._connect()) as connection:
            rows = connection.execute(query + " ORDER BY started_at ASC", params).fetchall()
        return [_row_to_event(row) for row in rows]

    def delete_event(self, event_id: str) -> bool:
        with closing(self._connect()) as connection:
            cursor = connection.execute("DELETE FROM events WHERE id = ?", (event_id,))
            connection.commit()
        return cursor.rowcount > 0

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection


@dataclass
class _OpenClip:
    event_id: str
    path: Path
    writer: Any
    size: tuple[int, int]
    started_at: float
    stop_at: float | None = None


@dataclass
class _CameraBuffer:
    frames: deque = field(default_factory=deque)  # (monotonic, jpeg bytes)
    last_taken: float | None = None


class ClipRecorder:
    """Writes one WebM clip per event: pre-roll, the event, then post-roll.

    Frames arrive at the vision loop's real rate (the configured interval plus
    inference time, shared between cameras), so each clip is encoded at the
    rate measured over its pre-roll and plays back in real time.
    """

    def __init__(self, evidence_dir: Path, fps: float) -> None:
        self.evidence_dir = evidence_dir
        self.fps = max(1.0, min(CLIP_MAX_FPS, fps))
        self._buffers: dict[str, _CameraBuffer] = {}
        self._clips: dict[str, dict[str, _OpenClip]] = {}

    def push(self, camera_id: str, frame: Any, now: float) -> list[tuple[str, Path]]:
        """Feed a frame; returns (event_id, path) for clips that just finished.

        Frames arriving faster than CLIP_MAX_FPS are not recorded; clips that
        are due to finish still finish on them.
        """
        buffer = self._buffers.setdefault(camera_id, _CameraBuffer())
        take = buffer.last_taken is None or now - buffer.last_taken >= 1.0 / CLIP_MAX_FPS
        small = None
        if take:
            buffer.last_taken = now
            small = _downscale(frame)
            ok, encoded = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                buffer.frames.append((now, encoded.tobytes()))
        while buffer.frames and now - buffer.frames[0][0] > CLIP_PRE_ROLL_SECONDS:
            buffer.frames.popleft()
        finished = []
        for clip in list(self._clips.get(camera_id, {}).values()):
            if small is not None:
                _write(clip, small)
            expired = now - clip.started_at >= CLIP_MAX_SECONDS
            if expired or (clip.stop_at is not None and now >= clip.stop_at):
                finished.append(self._finish(camera_id, clip.event_id))
        return finished

    def start(self, camera_id: str, event_id: str) -> None:
        buffer = self._buffers.get(camera_id)
        if buffer is None or not buffer.frames:
            return
        first = cv2.imdecode(_as_array(buffer.frames[0][1]), cv2.IMREAD_COLOR)
        height, width = first.shape[:2]
        event_dir = self.evidence_dir / event_id
        event_dir.mkdir(parents=True, exist_ok=True)
        path, writer = _open_writer(event_dir, _measured_fps(buffer, self.fps), (width, height))
        if writer is None:
            logger.warning("No video codec available for event clip", extra={"event_id": event_id})
            return
        clip = _OpenClip(event_id, path, writer, (width, height), started_at=buffer.frames[-1][0])
        for _, data in buffer.frames:
            _write(clip, cv2.imdecode(_as_array(data), cv2.IMREAD_COLOR))
        self._clips.setdefault(camera_id, {})[event_id] = clip

    def stop(self, camera_id: str, event_id: str, now: float) -> None:
        clip = self._clips.get(camera_id, {}).get(event_id)
        if clip is not None and clip.stop_at is None:
            clip.stop_at = now + CLIP_POST_ROLL_SECONDS

    def finish_camera(self, camera_id: str) -> list[tuple[str, Path]]:
        """The camera stopped delivering frames: close its clips as they are."""
        self._buffers.pop(camera_id, None)
        return [self._finish(camera_id, event_id) for event_id in list(self._clips.get(camera_id, {}))]

    def _finish(self, camera_id: str, event_id: str) -> tuple[str, Path]:
        clip = self._clips[camera_id].pop(event_id)
        clip.writer.release()
        return event_id, clip.path


class NodeEventPipeline:
    """Detections → tracks → zone observations → events with evidence.

    Runs inside the Node's vision loop, entirely offline.
    """

    def __init__(self, store: NodeEventStore, data_dir: Path, fps: float) -> None:
        self.store = store
        self.evidence_dir = data_dir / "evidence"
        self.engine = EventEngine(store)  # type: ignore[arg-type]
        self.spatial = SpatialEngine()
        self.evidence = EvidenceRecorder(storage_dir=self.evidence_dir)
        self.clips = ClipRecorder(self.evidence_dir, fps)
        self._trackers: dict[str, ByteTrackTracker] = {}
        self._active: set[str] = set()
        self._zones: dict[str, list[Zone]] = {}
        self._zones_loaded: tuple[float, int] | None = None
        self._lock = threading.Lock()

    def process(
        self,
        camera_id: str,
        frame: Any,
        detections: Iterable[Any],
        *,
        now: float,
        observed_at: datetime | None = None,
        tracked: bool = False,
    ) -> list[Event]:
        """``tracked``: detections already carry the vision tracker's IDs."""
        with self._lock:
            zones = self._camera_zones(camera_id, now)
            if not zones:
                self._forget(camera_id, "zone_removed")
                return []
            timestamp = observed_at or datetime.now(timezone.utc)
            self._active.add(camera_id)
            if tracked:
                objects = [
                    _to_tracked(item, camera_id, timestamp)
                    for item in detections
                    if getattr(item, "track_id", None) is not None
                ]
            else:
                tracker = self._trackers.setdefault(camera_id, ByteTrackTracker())
                objects = tracker.update(camera_id, [_to_detection(item) for item in detections], timestamp)
            self.spatial.update_zones(camera_id, zones)
            height, width = frame.shape[:2]
            observations, _presence = self.spatial.evaluate(camera_id, objects, width, height)
            changed = self.engine.process(camera_id, observations, zones)
            for event in changed:
                if event.ended_at is None:
                    self._record_start(event, frame, objects, timestamp)
                else:
                    self.clips.stop(camera_id, event.id, now)
            for event_id, path in self.clips.push(camera_id, frame, now):
                self.engine.attach_evidence(event_id, {"clip_path": str(path)})
            return changed

    def camera_unavailable(self, camera_id: str, reason: str) -> None:
        with self._lock:
            self._forget(camera_id, reason)

    def _forget(self, camera_id: str, reason: str) -> None:
        if camera_id in self._active:
            self._active.discard(camera_id)
            self._trackers.pop(camera_id, None)
            self.spatial.reset(camera_id)
            self.engine.mark_camera_unknown(camera_id, reason)
        for event_id, path in self.clips.finish_camera(camera_id):
            self.engine.attach_evidence(event_id, {"clip_path": str(path)})

    def _record_start(self, event: Event, frame: Any, tracked: list[TrackedObject], timestamp: datetime) -> None:
        try:
            paths = self.evidence.record(event=event, frame=frame, objects=tracked, frame_at=timestamp)
            self.engine.attach_evidence(event.id, paths)
        except Exception:
            logger.exception("Failed to save event snapshot", extra={"event_id": event.id})
        self.clips.start(event.camera_id, event.id)

    def _camera_zones(self, camera_id: str, now: float) -> list[Zone]:
        loaded = self._zones_loaded
        if loaded is None or now - loaded[0] >= ZONE_CACHE_SECONDS or loaded[1] != self.store.zones_version:
            zones: dict[str, list[Zone]] = {}
            for zone in self.store.list_zones():
                if zone.enabled and zone.type in EVENT_ZONE_TYPES:
                    zones.setdefault(zone.camera_id, []).append(zone)
            self._zones = zones
            self._zones_loaded = (now, self.store.zones_version)
        return self._zones.get(camera_id, [])


def _to_tracked(item: Any, camera_id: str, timestamp: datetime) -> TrackedObject:
    return TrackedObject(
        track_id=int(item.track_id),
        camera_id=camera_id,
        class_name=item.class_name,
        confidence=float(item.confidence),
        bounding_box=BoundingBox(float(item.x1), float(item.y1), float(item.x2), float(item.y2)),
        timestamp=timestamp,
    )


def _to_detection(item: Any) -> Detection:
    return Detection(
        class_name=item.class_name,
        confidence=float(item.confidence),
        bounding_box=BoundingBox(float(item.x1), float(item.y1), float(item.x2), float(item.y2)),
    )


def _measured_fps(buffer: _CameraBuffer, fallback: float) -> float:
    if len(buffer.frames) < 2:
        return fallback
    span = buffer.frames[-1][0] - buffer.frames[0][0]
    if span <= 0:
        return fallback
    return max(0.5, min(30.0, (len(buffer.frames) - 1) / span))


def _open_writer(event_dir: Path, fps: float, size: tuple[int, int]) -> tuple[Path, Any]:
    # VP8/WebM plays in every browser; mp4v is the fallback for OpenCV builds
    # without libvpx (it downloads but does not play inline in Chrome).
    for name, codec in (("clip.webm", "VP80"), ("clip.mp4", "mp4v")):
        path = event_dir / name
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*codec), fps, size)
        if writer.isOpened():
            return path, writer
        writer.release()
    return event_dir / "clip.webm", None


def _write(clip: _OpenClip, frame: Any) -> None:
    if (frame.shape[1], frame.shape[0]) != clip.size:
        frame = cv2.resize(frame, clip.size)
    clip.writer.write(frame)


def _downscale(frame: Any) -> Any:
    height, width = frame.shape[:2]
    if width <= CLIP_MAX_WIDTH:
        return frame
    scale = CLIP_MAX_WIDTH / width
    return cv2.resize(frame, (CLIP_MAX_WIDTH, int(height * scale)), interpolation=cv2.INTER_AREA)


def _as_array(data: bytes):
    return np.frombuffer(data, dtype=np.uint8)


def _zone_type(value: str) -> str:
    if value not in ZONE_TYPES:
        raise ValueError(f"Zone type must be one of {', '.join(ZONE_TYPES)}.")
    return value


def _points_json(points: list[list[float]], zone_type: str = "monitored") -> str:
    if zone_type == "line":
        if len(points) != 2:
            raise ValueError("A counting line must have exactly 2 points.")
    elif len(points) < 3:
        raise ValueError("Zone polygon must have at least 3 points.")
    normalized = []
    for point in points:
        if len(point) != 2:
            raise ValueError("Each point must have 2 coordinates.")
        x, y = float(point[0]), float(point[1])
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ValueError("Zone coordinates must be normalized (0.0-1.0).")
        normalized.append([round(x, 6), round(y, 6)])
    return json.dumps(normalized)


def _row_to_zone(row) -> Zone:
    return Zone(
        id=row["id"],
        camera_id=row["camera_id"],
        name=row["name"],
        type=row["type"],
        enabled=bool(row["enabled"]),
        points=[ZonePoint(x=float(x), y=float(y)) for x, y in json.loads(row["points"])],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_event(row) -> Event:
    return Event(
        id=row["id"],
        type=row["type"],
        camera_id=row["camera_id"],
        zone_id=row["zone_id"],
        track_id=row["track_id"],
        severity=row["severity"],
        status=row["status"],
        confidence=row["confidence"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        duration=row["duration"],
        metadata=_loads(row["metadata"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _loads(value: str | None) -> dict[str, Any]:
    try:
        data = json.loads(value) if value else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
