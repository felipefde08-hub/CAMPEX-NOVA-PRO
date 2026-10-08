from __future__ import annotations

import logging
import shutil
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from backend.cameras.security import sanitize_error_message
from campex_node.cameras.manager import CameraManager
from campex_node.core.config import NodeCameraConfig, NodeSettings
from campex_node.storage.sqlite import connect, iso, parse_iso, utc_now


logger = logging.getLogger("campex.node.recording")

# Browsers play H.264 (and HEVC where the OS has a decoder) from MP4. Other
# codecs are kept in Matroska: downloadable, not playable inline.
MP4_CODECS = frozenset({"h264", "hevc"})
# Fragmented MP4: a segment is playable while it is still being written and
# survives a crash, unlike a regular MP4 whose index is written on close.
MP4_OPTIONS = {"movflags": "frag_keyframe+empty_moov+default_base_moof"}
PROGRESS_SECONDS = 15.0
SYNC_SECONDS = 5.0
RETENTION_SECONDS = 60.0
DISK_RETRY_SECONDS = 60.0
GB = 1024**3

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS recordings (
        id TEXT PRIMARY KEY,
        camera_id TEXT NOT NULL,
        path TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT NOT NULL,
        duration REAL NOT NULL DEFAULT 0,
        size_bytes INTEGER NOT NULL DEFAULT 0,
        codec TEXT,
        width INTEGER,
        height INTEGER,
        status TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_recordings_camera_time ON recordings (camera_id, started_at)",
)


@dataclass(frozen=True)
class Segment:
    id: str
    camera_id: str
    path: Path
    started_at: datetime
    ended_at: datetime
    duration: float
    size_bytes: int
    codec: str | None
    width: int | None
    height: int | None
    status: str  # RECORDING | COMPLETE

    @property
    def media_type(self) -> str:
        return "video/mp4" if self.path.suffix == ".mp4" else "video/x-matroska"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "camera_id": self.camera_id,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat(),
            "duration": round(self.duration, 2),
            "size_bytes": self.size_bytes,
            "codec": self.codec,
            "width": self.width,
            "height": self.height,
            "status": self.status,
            "media_type": self.media_type,
        }


class RecordingStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(connect(self.database_path)) as connection:
            for statement in SCHEMA:
                connection.execute(statement)
            # Segments left open by a crash keep what they recorded so far.
            connection.execute("UPDATE recordings SET status = 'COMPLETE' WHERE status = 'RECORDING'")
            connection.commit()

    def add(self, segment: Segment) -> None:
        with closing(connect(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO recordings (id, camera_id, path, started_at, ended_at, duration, size_bytes, codec, width, height, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    segment.id, segment.camera_id, str(segment.path), iso(segment.started_at), iso(segment.ended_at),
                    segment.duration, segment.size_bytes, segment.codec, segment.width, segment.height, segment.status,
                ),
            )
            connection.commit()

    def progress(self, segment_id: str, duration: float, size_bytes: int, *, complete: bool = False) -> None:
        with closing(connect(self.database_path)) as connection:
            row = connection.execute("SELECT started_at FROM recordings WHERE id = ?", (segment_id,)).fetchone()
            if row is None:
                return
            ended_at = parse_iso(row["started_at"]) + timedelta(seconds=duration)  # type: ignore[operator]
            connection.execute(
                "UPDATE recordings SET ended_at = ?, duration = ?, size_bytes = ?, status = ? WHERE id = ?",
                (iso(ended_at), duration, size_bytes, "COMPLETE" if complete else "RECORDING", segment_id),
            )
            connection.commit()

    def get(self, segment_id: str) -> Segment | None:
        with closing(connect(self.database_path)) as connection:
            row = connection.execute("SELECT * FROM recordings WHERE id = ?", (segment_id,)).fetchone()
        return _row_to_segment(row) if row else None

    def list(self, camera_id: str | None, start: datetime, end: datetime) -> list[Segment]:
        """Segments overlapping [start, end], oldest first."""
        query = "SELECT * FROM recordings WHERE ended_at > ? AND started_at < ?"
        params: list[Any] = [iso(start), iso(end)]
        if camera_id:
            query += " AND camera_id = ?"
            params.append(camera_id)
        with closing(connect(self.database_path)) as connection:
            rows = connection.execute(query + " ORDER BY started_at ASC", params).fetchall()
        return [_row_to_segment(row) for row in rows]

    def at(self, camera_id: str, at: datetime) -> tuple[Segment, float] | None:
        """The segment covering ``at`` and the offset into it, in seconds."""
        with closing(connect(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT * FROM recordings WHERE camera_id = ? AND started_at <= ? AND ended_at > ?
                ORDER BY started_at DESC LIMIT 1
                """,
                (camera_id, iso(at), iso(at)),
            ).fetchone()
        if row is None:
            return None
        segment = _row_to_segment(row)
        return segment, max(0.0, (at - segment.started_at).total_seconds())

    def oldest_complete(self, limit: int = 50) -> list[Segment]:
        with closing(connect(self.database_path)) as connection:
            rows = connection.execute(
                "SELECT * FROM recordings WHERE status = 'COMPLETE' ORDER BY started_at ASC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_segment(row) for row in rows]

    def total_bytes(self) -> int:
        with closing(connect(self.database_path)) as connection:
            row = connection.execute("SELECT COALESCE(SUM(size_bytes), 0) AS total FROM recordings").fetchone()
        return int(row["total"])

    def delete(self, segment: Segment) -> None:
        try:
            segment.path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not delete recording %s", segment.path)
            return
        with closing(connect(self.database_path)) as connection:
            connection.execute("DELETE FROM recordings WHERE id = ?", (segment.id,))
            connection.commit()

    def summary(self) -> dict[str, Any]:
        with closing(connect(self.database_path)) as connection:
            rows = connection.execute(
                """
                SELECT camera_id, MIN(started_at) AS first, MAX(ended_at) AS last,
                       COUNT(*) AS segments, SUM(size_bytes) AS bytes
                FROM recordings GROUP BY camera_id
                """
            ).fetchall()
        return {
            row["camera_id"]: {
                "first": row["first"],
                "last": row["last"],
                "segments": row["segments"],
                "size_bytes": int(row["bytes"] or 0),
            }
            for row in rows
        }


class CameraRecorder:
    """Remuxes one camera's stream into fixed-length segments.

    The stream is copied packet by packet (no decoding), so recording costs
    almost no CPU and keeps the camera's full quality. It opens its own RTSP
    session, separate from the one the vision worker decodes; with go2rtc
    both read the same local restream.
    """

    def __init__(
        self,
        camera: NodeCameraConfig,
        settings: NodeSettings,
        store: RecordingStore,
        *,
        clock: Callable[[], datetime] = utc_now,
        source_url: str | None = None,
    ) -> None:
        self.camera = camera
        self.source_url = source_url
        self.settings = settings
        self.store = store
        self.clock = clock
        self.status = "STARTING"
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"campex-node-recorder-{camera.id}", daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._disk_full():
                # Retention frees space from old segments; until then nothing
                # is written, so a full disk never stops the computer.
                self.status, self.error = "DISK_FULL", "Pouco espaço livre no disco da gravação."
                self._stop.wait(DISK_RETRY_SECONDS)
                continue
            try:
                self.record_session()
            except Exception as exc:
                self.status = "ERROR"
                self.error = sanitize_error_message(str(exc))
                logger.warning("[camera:%s] recording failed: %s", self.camera.id, self.error)
            self._stop.wait(self.settings.camera_reconnect_seconds)

    def record_session(self) -> None:
        """Records until the stream ends, fails or the recorder is stopped."""
        import av

        source = av.open(
            self.source_url or self.camera.rtsp_url,
            options={"rtsp_transport": "tcp"},
            timeout=(self.settings.camera_open_timeout_ms / 1000, self.settings.camera_read_timeout_ms / 1000),
        )
        writer: _SegmentWriter | None = None
        try:
            stream = source.streams.video[0]
            codec = stream.codec_context.name
            anchor = self.clock()
            self.status, self.error = "RECORDING", None
            for packet in source.demux(stream):
                if self._stop.is_set():
                    break
                if packet.dts is None or packet.pts is None:
                    continue
                if writer is None:
                    if not packet.is_keyframe:
                        continue
                    writer = self._open_segment(stream, codec, anchor)
                elif packet.is_keyframe and writer.elapsed(packet) >= self.settings.recording_segment_seconds:
                    anchor = writer.started_at + timedelta(seconds=writer.duration)
                    self._close_segment(writer)
                    writer = None
                    if self._disk_full():
                        break
                    writer = self._open_segment(stream, codec, anchor)
                writer.write(packet)
                if writer.should_report():
                    self.store.progress(writer.segment_id, writer.duration, writer.size())
        finally:
            if writer is not None:
                self._close_segment(writer)
            source.close()
            if self.status == "RECORDING":
                self.status = "STOPPED"

    def _disk_full(self) -> bool:
        free = _free_bytes(self.settings.recordings_path)
        return free is not None and free < self.settings.recording_min_free_gb * GB

    def _open_segment(self, stream, codec: str, started_at: datetime) -> "_SegmentWriter":
        extension = ".mp4" if codec in MP4_CODECS else ".mkv"
        segment_id = f"rec_{uuid4().hex[:12]}"
        folder = self.settings.recordings_path / self.camera.id / started_at.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{started_at.strftime('%H%M%S')}_{segment_id}{extension}"
        writer = _SegmentWriter(segment_id, path, stream, started_at)
        self.store.add(
            Segment(
                id=segment_id,
                camera_id=self.camera.id,
                path=path,
                started_at=started_at,
                ended_at=started_at,
                duration=0.0,
                size_bytes=0,
                codec=codec,
                width=stream.codec_context.width or None,
                height=stream.codec_context.height or None,
                status="RECORDING",
            )
        )
        return writer

    def _close_segment(self, writer: "_SegmentWriter") -> None:
        writer.close()
        self.store.progress(writer.segment_id, writer.duration, writer.size(), complete=True)


class _SegmentWriter:
    def __init__(self, segment_id: str, path: Path, stream, started_at: datetime) -> None:
        import av

        self.segment_id = segment_id
        self.path = path
        self.started_at = started_at
        options = MP4_OPTIONS if path.suffix == ".mp4" else {}
        self._container = av.open(str(path), mode="w", options=options)
        self._stream = self._container.add_stream_from_template(stream)
        self._time_base = stream.time_base
        # Timestamps are shifted by the first DTS so they stay positive; the
        # first keyframe's PTS (later than its DTS with B-frames) is where
        # playback starts, so time is measured from it.
        self._first_dts: int | None = None
        self._start_pts = 0
        self._last_dts: int | None = None
        self._end_pts = 0
        self._last_report = 0.0
        self.duration = 0.0

    def elapsed(self, packet) -> float:
        if self._first_dts is None:
            return 0.0
        return float((packet.pts - self._first_dts - self._start_pts) * self._time_base)

    def write(self, packet) -> None:
        if self._first_dts is None:
            self._first_dts = packet.dts
            self._start_pts = packet.pts - packet.dts
        dts = packet.dts - self._first_dts
        if self._last_dts is not None and dts <= self._last_dts:
            return  # out-of-order packet from the camera; the muxer rejects it
        packet.pts = max(0, packet.pts - self._first_dts)
        packet.dts = dts
        self._last_dts = dts
        self._end_pts = max(self._end_pts, packet.pts + (packet.duration or 0))
        packet.stream = self._stream
        self._container.mux(packet)
        self.duration = float((self._end_pts - self._start_pts) * self._time_base)

    def should_report(self) -> bool:
        if self.duration - self._last_report >= PROGRESS_SECONDS:
            self._last_report = self.duration
            return True
        return False

    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def close(self) -> None:
        try:
            self._container.close()
        except Exception:
            logger.warning("Recording segment %s closed with an error", self.path, exc_info=True)


class RecordingService:
    """Keeps one recorder per enabled camera and enforces retention."""

    def __init__(self, settings: NodeSettings, camera_manager: CameraManager, store: RecordingStore) -> None:
        self.settings = settings
        self.camera_manager = camera_manager
        self.store = store
        self._recorders: dict[str, CameraRecorder] = {}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="campex-node-recording", daemon=True)

    def start(self) -> None:
        if self.settings.recording_enabled and not self._thread.is_alive():
            self.settings.recordings_path.mkdir(parents=True, exist_ok=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        with self._lock:
            recorders = list(self._recorders.values())
            self._recorders.clear()
        for recorder in recorders:
            recorder.stop()

    def status(self) -> dict[str, Any]:
        with self._lock:
            recorders = dict(self._recorders)
        stored = self.store.summary()
        return {
            "enabled": self.settings.recording_enabled,
            "path": str(self.settings.recordings_path),
            "disk": _disk(self.settings.recordings_path),
            "total_bytes": sum(item["size_bytes"] for item in stored.values()),
            "retention_days": self.settings.recording_retention_days,
            "min_free_gb": self.settings.recording_min_free_gb,
            "max_gb": self.settings.recording_max_gb,
            "cameras": {
                camera_id: {
                    "status": recorders[camera_id].status if camera_id in recorders else "STOPPED",
                    "error": recorders[camera_id].error if camera_id in recorders else None,
                    **stored.get(camera_id, {}),
                }
                for camera_id in set(recorders) | set(stored)
            },
        }

    def enforce_retention(self) -> int:
        """Deletes the oldest complete segments; returns how many."""
        deleted = 0
        cutoff = utc_now() - timedelta(days=self.settings.recording_retention_days)
        max_bytes = self.settings.recording_max_gb * GB
        min_free = self.settings.recording_min_free_gb * GB
        total = self.store.total_bytes()
        while True:
            candidates = self.store.oldest_complete()
            if not candidates:
                return deleted
            for segment in candidates:
                free = _free_bytes(self.settings.recordings_path)
                too_old = segment.ended_at < cutoff
                over_cap = max_bytes > 0 and total > max_bytes
                low_disk = free is not None and free < min_free
                if not (too_old or over_cap or low_disk):
                    return deleted
                self.store.delete(segment)
                total -= segment.size_bytes
                deleted += 1

    def _run(self) -> None:
        next_retention = 0.0
        while not self._stop.is_set():
            try:
                self._sync_recorders()
                if time.monotonic() >= next_retention:
                    next_retention = time.monotonic() + RETENTION_SECONDS
                    removed = self.enforce_retention()
                    if removed:
                        logger.info("Recording retention removed %s segment(s)", removed)
            except Exception:
                logger.exception("Recording service loop failed")
            self._stop.wait(SYNC_SECONDS)

    def _sync_recorders(self) -> None:
        wanted = {camera.id: camera for camera in self.camera_manager.configs() if camera.enabled and camera.rtsp_url}
        with self._lock:
            for camera_id in list(self._recorders):
                recorder = self._recorders[camera_id]
                camera = wanted.get(camera_id)
                if camera is None or camera.rtsp_url != recorder.camera.rtsp_url:
                    recorder.stop(timeout=0.5)
                    del self._recorders[camera_id]
            for camera_id, camera in wanted.items():
                if camera_id not in self._recorders:
                    recorder = CameraRecorder(
                        camera, self.settings, self.store, source_url=self.camera_manager.source_url(camera)
                    )
                    self._recorders[camera_id] = recorder
                    recorder.start()


def _free_bytes(path: Path) -> int | None:
    # The folder may not exist yet: measure the disk it will be created on.
    while not path.exists() and path.parent != path:
        path = path.parent
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _disk(path: Path) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {"total_gb": None, "free_gb": None}
    return {"total_gb": round(usage.total / GB, 1), "free_gb": round(usage.free / GB, 1)}


def _row_to_segment(row) -> Segment:
    return Segment(
        id=row["id"],
        camera_id=row["camera_id"],
        path=Path(row["path"]),
        started_at=parse_iso(row["started_at"]),  # type: ignore[arg-type]
        ended_at=parse_iso(row["ended_at"]),  # type: ignore[arg-type]
        duration=float(row["duration"]),
        size_bytes=int(row["size_bytes"]),
        codec=row["codec"],
        width=row["width"],
        height=row["height"],
        status=row["status"],
    )
