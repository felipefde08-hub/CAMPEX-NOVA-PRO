from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from backend.zones.models import Zone
from campex_node.activity import MACHINE, PERSON_OCCUPANCY, VEHICLE_OCCUPANCY, ActivityStore
from campex_node.events import NodeEventStore
from campex_node.factory import FactoryStore
from campex_node.storage.sqlite import iso


logger = logging.getLogger("campex.node.monitors")

VEHICLE_CLASSES = frozenset({"car", "motorcycle", "bus", "truck"})
OCCUPIED, VACANT = "OCCUPIED", "VACANT"
RUNNING, STOPPED, UNKNOWN = "RUNNING", "STOPPED", "UNKNOWN"

# Who is counted in each zone type.
ZONE_SUBJECTS = {
    "monitored": ("person",),
    "restricted": ("person",),
    "machine": ("person",),
    "station": ("person",),
    "dock": ("vehicle",),
    "area": ("person", "vehicle"),
    "occupancy": ("person",),
}
SUBJECT_KIND = {"person": PERSON_OCCUPANCY, "vehicle": VEHICLE_OCCUPANCY}
# Detections drop out for a frame or two; a zone only turns vacant after
# nobody has been seen in it for this long (vehicles park still and get missed
# less, but leave slower).
VACANT_AFTER_SECONDS = {"person": 10.0, "vehicle": 30.0}
CONFIRM_FRAMES = 2
# Open intervals record how far they are confirmed, so a Node restart ends
# them there instead of stretching them over the downtime.
TOUCH_SECONDS = 30.0
ZONE_CACHE_SECONDS = 2.0
LINE_TRACK_SECONDS = 5.0
# Occupancy limits: the count must stay at or under the limit this long
# before the limit is considered respected again (detections flicker).
LIMIT_CLEAR_SECONDS = 10.0
# A person's stay ends when their track is not seen for this long.
DWELL_GONE_SECONDS = 10.0

# Machine motion analysis
ROI_WIDTH = 160
PIXEL_DELTA = 25
RUN_WINDOW_SECONDS = 5.0
RUN_MIN_MOTION_SAMPLES = 2
PERSON_BOX_PADDING = 0.15
# People boxes come from the last analysed frame, which is older than the
# frame sampled here. Within this age each box is widened by how far a person
# can have walked meanwhile; beyond it the boxes say nothing reliable about
# where people are now.
PERSON_OBSERVATION_MAX_AGE_SECONDS = 2.0
PERSON_MAX_SPEED_BOX_WIDTHS = 3.0  # a brisk walk (~1.5 m/s), in body widths per second
# Below this share of the machine area left unmasked the sample cannot tell
# whether the machine moved.
MIN_VISIBLE_FRACTION = 0.25

# Machine signal lights
LIGHT_MIN_VALUE = 200  # HSV brightness of a lit pixel
LIGHT_MIN_SATURATION = 80  # a colored light, as opposed to white glare
LIGHT_WHITE_MAX_SATURATION = 60
# Hue ranges (OpenCV, 0-179) of each light color; red wraps around 0.
LIGHT_HUES = {"red": ((0, 10), (160, 179)), "yellow": ((15, 35),), "green": ((40, 85),), "blue": ((95, 130),)}
# A light counts as lit if it was lit this recently, so a blinking light
# stays "on" between flashes.
LIGHT_HOLD_SECONDS = 2.0
# Below this share of the light visible (a person in front of it), the
# sample says nothing.
LIGHT_MIN_VISIBLE = 0.5


class ZoneCache:
    """Enabled zones per camera with their settings, reloaded when they change."""

    def __init__(self, store: NodeEventStore) -> None:
        self.store = store
        self._loaded: tuple[float, int] | None = None
        self._zones: dict[str, list[tuple[Zone, dict[str, Any]]]] = {}
        self._lock = threading.Lock()

    def camera_zones(self, camera_id: str) -> list[tuple[Zone, dict[str, Any]]]:
        now = time.monotonic()
        with self._lock:
            loaded = self._loaded
            if loaded is None or now - loaded[0] >= ZONE_CACHE_SECONDS or loaded[1] != self.store.zones_version:
                settings = self.store.zone_settings()
                zones: dict[str, list[tuple[Zone, dict[str, Any]]]] = {}
                for zone in self.store.list_zones():
                    if zone.enabled:
                        zones.setdefault(zone.camera_id, []).append((zone, settings.get(zone.id, {})))
                self._zones = zones
                self._loaded = (now, self.store.zones_version)
            return list(self._zones.get(camera_id, []))


class FactoryEvents:
    """Opens and closes the factory monitors' events, with a snapshot."""

    def __init__(self, store: NodeEventStore, evidence_dir: Path) -> None:
        self.store = store
        self.evidence_dir = evidence_dir

    def open(
        self,
        *,
        event_type: str,
        zone: Zone,
        severity: str,
        started_at: datetime,
        frame: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        event = self.store.create(
            event_type=event_type,
            camera_id=zone.camera_id,
            zone_id=zone.id,
            severity=severity,
            started_at=iso(started_at),
            metadata={"zone_name": zone.name, "zone_type": zone.type, "source": "factory_monitor", **(metadata or {})},
        )
        if frame is not None:
            try:
                folder = self.evidence_dir / event.id
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / "frame.jpg"
                if cv2.imwrite(str(path), frame):
                    self.store.update_status(
                        event.id, "OPEN", metadata_update={"snapshot_path": str(path), "overlay_path": str(path)}
                    )
            except Exception:
                logger.exception("Failed to save snapshot for event %s", event.id)
        return event.id

    def close(self, event_id: str, ended_at: datetime, metadata: dict[str, Any] | None = None) -> None:
        event = self.store.get(event_id)
        if event is None or event.ended_at is not None:
            return
        started = datetime.fromisoformat(event.started_at)
        duration = max(0.0, (ended_at - started).total_seconds())
        self.store.update_status(
            event_id, "CLOSED", ended_at=iso(max(ended_at, started)), duration=round(duration, 1), metadata_update=metadata
        )


# Occupancy


@dataclass
class _Presence:
    zone_id: str
    camera_id: str
    subject: str
    state: str | None = None
    state_since: datetime | None = None
    interval_id: str | None = None
    observing_since: datetime | None = None
    streak: int = 0
    streak_start: datetime | None = None
    last_seen: datetime | None = None
    last_frame: datetime | None = None
    last_touch: datetime | None = None
    peak: int = 0
    count: int = 0
    event_id: str | None = None
    # Occupancy limit
    over_since: datetime | None = None
    under_since: datetime | None = None
    limit_peak: int = 0
    limit_event_id: str | None = None


@dataclass
class _Dwell:
    zone_id: str
    camera_id: str
    track_id: int
    first_seen: datetime
    last_seen: datetime
    event_id: str | None = None


@dataclass
class _LinePoint:
    point: tuple[float, float]
    side: float
    seen_at: datetime


class OccupancyMonitor:
    """Turns each analysed frame into occupied/vacant intervals per zone.

    It counts people (and vehicles, for docks and areas) by where their feet
    are, not by track ID, so a lost or switched track does not open a false
    gap. It also counts crossings of counting lines and raises:

    - STATION_VACANT: a station empty for longer than its alert time, during
      working hours (any time when no shifts are configured);
    - AFTER_HOURS_PRESENCE: someone or a vehicle in an area outside the shifts;
    - DOCK_VISIT: a vehicle parked at a dock for at least ``min_visit_seconds``;
    - OCCUPANCY_LIMIT: more people in an occupancy zone than ``max_people``
      for ``over_limit_seconds``;
    - LONG_PRESENCE: one person (one track) in an occupancy zone for longer
      than ``max_dwell_seconds``. A track that switches restarts the count.
    """

    def __init__(
        self,
        events_store: NodeEventStore,
        activity: ActivityStore,
        factory: FactoryStore,
        evidence_dir: Path,
        zones: ZoneCache | None = None,
    ) -> None:
        self.activity = activity
        self.factory = factory
        self.events = FactoryEvents(events_store, evidence_dir)
        self.zones = zones or ZoneCache(events_store)
        self._presence: dict[tuple[str, str], _Presence] = {}
        self._lines: dict[tuple[str, int], _LinePoint] = {}
        self._dwell: dict[tuple[str, int], _Dwell] = {}
        self._lock = threading.Lock()

    def presence(self, zone_id: str, subject: str = "person") -> tuple[str | None, datetime | None]:
        with self._lock:
            state = self._presence.get((zone_id, subject))
            return (state.state, state.state_since) if state else (None, None)

    def snapshot(self, camera_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = [item for item in self._presence.values() if camera_id is None or item.camera_id == camera_id]
            return [
                {
                    "zone_id": item.zone_id,
                    "camera_id": item.camera_id,
                    "subject": item.subject,
                    "state": item.state,
                    "since": iso(item.state_since),
                    "count": item.count,
                }
                for item in items
            ]

    def observe(self, camera_id: str, frame: Any, detections: list[Any], observed_at: datetime) -> None:
        height, width = frame.shape[:2]
        feet: dict[str, list[tuple[float, float]]] = {"person": [], "vehicle": []}
        for detection in detections:
            subject = _subject(detection.class_name)
            if subject:
                feet[subject].append(_foot(detection, width, height))
        zones = self.zones.camera_zones(camera_id)
        with self._lock:
            for zone, settings in zones:
                if zone.type == "line":
                    self._count_line(zone, settings, detections, width, height, observed_at)
                    continue
                margin = float(settings.get("operator_margin", 0.0)) if zone.type == "machine" else 0.0
                points = [(point.x, point.y) for point in zone.points]
                for subject in ZONE_SUBJECTS.get(zone.type, ()):
                    count = sum(1 for foot in feet[subject] if _inside(foot, points, margin))
                    self._update(zone, settings, subject, count, observed_at, frame)
                if zone.type == "occupancy" and float(settings.get("max_dwell_seconds") or 0) > 0:
                    self._track_dwell(zone, settings, detections, points, width, height, observed_at, frame)
            self._forget_removed(camera_id, {zone.id for zone, _ in zones})
            self._prune_lines(observed_at)
            self._prune_dwell(camera_id, observed_at)

    def camera_unavailable(self, camera_id: str, reason: str) -> None:
        with self._lock:
            for key, item in list(self._presence.items()):
                if item.camera_id != camera_id:
                    continue
                self._release(item, reason)
                del self._presence[key]
            for key, dwell in list(self._dwell.items()):
                if dwell.camera_id == camera_id:
                    self._end_dwell(dwell, {"knowledge_state": "UNKNOWN", "uncertainty_reason": reason})
                    del self._dwell[key]

    def _update(self, zone: Zone, settings: dict[str, Any], subject: str, count: int, at: datetime, frame: Any) -> None:
        item = self._presence.setdefault((zone.id, subject), _Presence(zone.id, zone.camera_id, subject))
        item.last_frame = at
        item.count = count
        if item.observing_since is None:
            item.observing_since = at
        vacant_after = VACANT_AFTER_SECONDS[subject]
        if count > 0:
            item.last_seen = at
            if item.streak == 0:
                item.streak_start = at
            item.streak += 1
            item.peak = max(item.peak, count)
            if item.state != OCCUPIED and item.streak >= CONFIRM_FRAMES:
                self._transition(item, zone, settings, OCCUPIED, item.streak_start or at, frame)
        else:
            item.streak = 0
            if item.state == OCCUPIED and item.last_seen and (at - item.last_seen).total_seconds() >= vacant_after:
                self._transition(item, zone, settings, VACANT, item.last_seen, frame)
            elif item.state is None and (at - item.observing_since).total_seconds() >= vacant_after:
                self._transition(item, zone, settings, VACANT, item.observing_since, frame)
        if item.interval_id and (item.last_touch is None or (at - item.last_touch).total_seconds() >= TOUCH_SECONDS):
            self.activity.touch(item.interval_id, at, item.peak)
            item.last_touch = at
        self._check_alerts(item, zone, settings, at, frame)
        if zone.type == "occupancy":
            self._check_limit(item, zone, settings, at, frame)

    def _transition(self, item: _Presence, zone: Zone, settings: dict[str, Any], state: str, at: datetime, frame: Any) -> None:
        if item.interval_id:
            self.activity.close(item.interval_id, at, item.peak)
        item.peak = item.count if state == OCCUPIED else 0
        item.interval_id = self.activity.open(
            kind=SUBJECT_KIND[item.subject],
            zone_id=zone.id,
            camera_id=zone.camera_id,
            state=state,
            started_at=at,
            peak=item.peak,
        )
        item.state, item.state_since, item.last_touch = state, at, at
        if state == OCCUPIED:
            if zone.type == "station" and item.event_id:
                self.events.close(item.event_id, at)
                item.event_id = None
            if zone.type == "area" and settings.get("after_hours_alert") and self.factory.is_working_time(at) is False:
                item.event_id = self.events.open(
                    event_type="AFTER_HOURS_PRESENCE",
                    zone=zone,
                    severity="critical",
                    started_at=at,
                    frame=frame,
                    metadata={"subject": item.subject},
                )
        elif item.event_id and zone.type in ("area", "dock"):
            self.events.close(item.event_id, at, {"peak": item.peak})
            item.event_id = None

    def _check_alerts(self, item: _Presence, zone: Zone, settings: dict[str, Any], at: datetime, frame: Any) -> None:
        if item.event_id or item.state_since is None:
            return
        elapsed = (at - item.state_since).total_seconds()
        if zone.type == "station" and item.state == VACANT:
            if elapsed < float(settings["vacant_alert_seconds"]) or self.factory.is_working_time(at) is False:
                return
            # Only the part of the vacancy inside the current shift counts.
            window = self.factory.shift_at(at)
            started = max(item.state_since, window.start) if window else item.state_since
            if (at - started).total_seconds() < float(settings["vacant_alert_seconds"]):
                return
            item.event_id = self.events.open(
                event_type="STATION_VACANT", zone=zone, severity="attention", started_at=started, frame=frame,
                metadata={"line": settings.get("line") or None},
            )
        elif zone.type == "dock" and item.state == OCCUPIED and elapsed >= float(settings["min_visit_seconds"]):
            item.event_id = self.events.open(
                event_type="DOCK_VISIT", zone=zone, severity="info", started_at=item.state_since, frame=frame,
                metadata={"subject": "vehicle"},
            )

    def _check_limit(self, item: _Presence, zone: Zone, settings: dict[str, Any], at: datetime, frame: Any) -> None:
        limit = float(settings.get("max_people") or 0)
        if limit <= 0:
            return
        if item.count > limit:
            item.under_since = None
            item.over_since = item.over_since or at
            item.limit_peak = max(item.limit_peak, item.count)
            if item.limit_event_id is None and (at - item.over_since).total_seconds() >= float(settings["over_limit_seconds"]):
                item.limit_event_id = self.events.open(
                    event_type="OCCUPANCY_LIMIT", zone=zone, severity="attention", started_at=item.over_since, frame=frame,
                    metadata={"max_people": int(limit), "people": item.count},
                )
            return
        item.under_since = item.under_since or at
        if (at - item.under_since).total_seconds() < LIMIT_CLEAR_SECONDS:
            return
        if item.limit_event_id:
            self.events.close(item.limit_event_id, item.under_since, {"peak": item.limit_peak, "max_people": int(limit)})
            item.limit_event_id = None
        item.over_since, item.limit_peak = None, 0

    def _track_dwell(
        self,
        zone: Zone,
        settings: dict[str, Any],
        detections: list[Any],
        points: list[tuple[float, float]],
        width: int,
        height: int,
        at: datetime,
        frame: Any,
    ) -> None:
        max_dwell = float(settings["max_dwell_seconds"])
        for detection in detections:
            track_id = getattr(detection, "track_id", None)
            if track_id is None or _subject(detection.class_name) != "person":
                continue
            if not _inside(_foot(detection, width, height), points):
                continue
            key = (zone.id, int(track_id))
            dwell = self._dwell.get(key)
            if dwell is None:
                dwell = self._dwell[key] = _Dwell(zone.id, zone.camera_id, int(track_id), at, at)
            dwell.last_seen = at
            if dwell.event_id is None and (at - dwell.first_seen).total_seconds() >= max_dwell:
                dwell.event_id = self.events.open(
                    event_type="LONG_PRESENCE", zone=zone, severity="attention", started_at=dwell.first_seen, frame=frame,
                    metadata={"track_id": dwell.track_id, "max_dwell_seconds": max_dwell},
                )

    def _prune_dwell(self, camera_id: str, at: datetime) -> None:
        for key, dwell in list(self._dwell.items()):
            if dwell.camera_id == camera_id and (at - dwell.last_seen).total_seconds() > DWELL_GONE_SECONDS:
                self._end_dwell(dwell)
                del self._dwell[key]

    def _end_dwell(self, dwell: _Dwell, metadata: dict[str, Any] | None = None) -> None:
        if dwell.event_id:
            seconds = round((dwell.last_seen - dwell.first_seen).total_seconds(), 1)
            self.events.close(dwell.event_id, dwell.last_seen, {"dwell_seconds": seconds, **(metadata or {})})

    def _release(self, item: _Presence, reason: str) -> None:
        ended = item.last_frame or item.state_since
        if item.interval_id and ended:
            self.activity.close(item.interval_id, ended, item.peak)
        for event_id in (item.event_id, item.limit_event_id):
            if event_id and ended:
                self.events.close(event_id, ended, {"knowledge_state": "UNKNOWN", "uncertainty_reason": reason})

    def _forget_removed(self, camera_id: str, zone_ids: set[str]) -> None:
        for key, item in list(self._presence.items()):
            if item.camera_id == camera_id and item.zone_id not in zone_ids:
                self._release(item, "zone_removed")
                del self._presence[key]
        for key, dwell in list(self._dwell.items()):
            if dwell.camera_id == camera_id and dwell.zone_id not in zone_ids:
                self._end_dwell(dwell, {"knowledge_state": "UNKNOWN", "uncertainty_reason": "zone_removed"})
                del self._dwell[key]

    def _count_line(
        self, zone: Zone, settings: dict[str, Any], detections: list[Any], width: int, height: int, at: datetime
    ) -> None:
        start, end = (zone.points[0].x, zone.points[0].y), (zone.points[1].x, zone.points[1].y)
        wanted = settings.get("subject", "person")
        for detection in detections:
            track_id = getattr(detection, "track_id", None)
            subject = _subject(detection.class_name)
            if track_id is None or subject is None or (wanted != "any" and subject != wanted):
                continue
            point = _foot(detection, width, height)
            side = _cross(start, end, point)
            key = (zone.id, int(track_id))
            previous = self._lines.get(key)
            if (
                previous is not None
                and previous.side * side < 0
                and _segments_intersect(previous.point, point, start, end)
            ):
                # Forward: from the right-hand side of A->B, as seen on
                # screen, to its left (y grows downwards in the frame).
                self.activity.add_count(zone.id, at, "forward" if previous.side > 0 else "backward")
            keep_side = side if side != 0 else (previous.side if previous else 0.0)
            self._lines[key] = _LinePoint(point, keep_side, at)

    def _prune_lines(self, at: datetime) -> None:
        for key, item in list(self._lines.items()):
            if (at - item.seen_at).total_seconds() > LINE_TRACK_SECONDS:
                del self._lines[key]


# Machines


@dataclass
class _Machine:
    zone_id: str
    camera_id: str
    state: str | None = None
    state_since: datetime | None = None
    interval_id: str | None = None
    monitoring_since: datetime | None = None
    last_sample: datetime | None = None
    last_motion: datetime | None = None
    last_touch: datetime | None = None
    was_moving: bool = False
    activity: float = 0.0
    motions: deque = field(default_factory=deque)
    prev_gray: np.ndarray | None = None
    geometry_key: tuple | None = None
    roi: tuple[int, int, int, int] = (0, 0, 0, 0)
    roi_size: tuple[int, int] = (0, 0)
    mask: np.ndarray | None = None
    stop_event_id: str | None = None
    operator_event_id: str | None = None
    # Last sample that could not tell whether the machine moved (people
    # boxes too old, or the machine hidden behind people).
    last_unclear: datetime | None = None
    observation_age: float | None = None
    conclusive: bool = True
    # Signal light machines
    light_level: float | None = None
    last_lit: datetime | None = None


class MachineMonitor:
    """Decides whether each machine is running from movement in its area,
    or from a signal light (``detection: light``).

    Every sample compares the machine's area with the previous frame (people
    detected by the vision loop are masked out, so an operator walking past
    does not count as the machine moving):

    - RUNNING after movement on ``RUN_MIN_MOTION_SAMPLES`` samples within
      ``RUN_WINDOW_SECONDS``;
    - STOPPED after ``stop_after_seconds`` without movement, starting at the
      last movement seen;
    - UNKNOWN while the camera is offline or frozen (never counted as downtime).

    ``people(camera_id)`` returns ``(frame_at, boxes)``: the people boxes and
    when their frame was received. They are older than the sampled frame, so
    they are widened by how far a person can have walked since; samples they
    cannot clear up are inconclusive and count neither as movement nor as
    stillness. A provider returning a plain list of boxes is taken as current.

    A signal light gives the same running signal as movement: lit (or unlit,
    for a light that means "stopped") within ``LIGHT_HOLD_SECONDS``. The
    lit share of the light's area is compared with the levels calibrated on
    the camera, so a dim LED and a bright tower light both work.

    Each burst of movement after a still sample counts as one cycle (a press
    stroke, a robot pick). It raises MACHINE_STOPPED for every stop and, for
    machines that require an operator, MISSING_OPERATOR when the machine runs
    with nobody near it.
    """

    def __init__(
        self,
        *,
        events_store: NodeEventStore,
        activity: ActivityStore,
        occupancy: OccupancyMonitor | None,
        evidence_dir: Path,
        camera_manager: Any = None,
        people: Callable[[str], Any] | None = None,
        interval_seconds: float = 0.2,
        stale_frame_seconds: float = 10.0,
        zones: ZoneCache | None = None,
    ) -> None:
        self.activity = activity
        self.occupancy = occupancy
        self.events = FactoryEvents(events_store, evidence_dir)
        self.zones = zones or ZoneCache(events_store)
        self.camera_manager = camera_manager
        self.people = people or (lambda camera_id: [])
        self.interval_seconds = interval_seconds
        self.stale_frame_seconds = stale_frame_seconds
        self._machines: dict[str, _Machine] = {}
        self._last_frame_at: dict[str, datetime] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="campex-node-machines", daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3)

    def snapshot(self, camera_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "zone_id": item.zone_id,
                    "camera_id": item.camera_id,
                    "state": item.state or UNKNOWN,
                    "since": iso(item.state_since),
                    "activity": round(item.activity, 4),
                    "light_level": round(item.light_level, 4) if item.light_level is not None else None,
                    "last_motion_at": iso(item.last_motion),
                    # Whether the last sample could tell movement from people.
                    "conclusive": item.conclusive,
                    "people_observation_age_ms": (
                        round(item.observation_age * 1000) if item.observation_age is not None else None
                    ),
                    "stop_event_id": item.stop_event_id,
                    "operator_event_id": item.operator_event_id,
                }
                for item in self._machines.values()
                if camera_id is None or item.camera_id == camera_id
            ]

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._sample_cameras()
            except Exception:
                logger.exception("Machine monitor loop failed")
            self._stop.wait(self.interval_seconds)

    def light_level(self, zone_id: str) -> float | None:
        """The lit share of a signal light's area in the latest sample."""
        with self._lock:
            machine = self._machines.get(zone_id)
            return machine.light_level if machine else None

    def _sample_cameras(self) -> None:
        for camera in self.camera_manager.configs():
            machines = [item for item in self.zones.camera_zones(camera.id) if item[0].type == "machine"]
            if not machines and not any(item.camera_id == camera.id for item in self._machines.values()):
                continue
            frame, frame_at = self.camera_manager.latest_frame(camera.id) if camera.enabled else (None, None)
            now = datetime.now(timezone.utc)
            if frame is None or frame_at is None or (now - frame_at).total_seconds() > self.stale_frame_seconds:
                self.camera_unavailable(camera.id, "camera_observation_unavailable")
                continue
            if self._last_frame_at.get(camera.id) == frame_at:
                continue
            self._last_frame_at[camera.id] = frame_at
            self.sample(camera.id, frame, frame_at)

    def sample(self, camera_id: str, frame: Any, at: datetime) -> None:
        machines = [item for item in self.zones.camera_zones(camera_id) if item[0].type == "machine"]
        observed = self.people(camera_id)
        observed_at, people = observed if isinstance(observed, tuple) else (None, observed)
        # Seconds between the frame the boxes came from and this frame.
        age = max(0.0, (at - observed_at).total_seconds()) if observed_at is not None else 0.0
        with self._lock:
            for zone, settings in machines:
                machine = self._machines.setdefault(zone.id, _Machine(zone.id, camera_id))
                self._sample_machine(machine, zone, settings, frame, people, at, age)
            wanted = {zone.id for zone, _ in machines}
            for zone_id, machine in list(self._machines.items()):
                if machine.camera_id == camera_id and zone_id not in wanted:
                    self._go_unknown(machine, machine.last_sample or at, "zone_removed")
                    del self._machines[zone_id]

    def camera_unavailable(self, camera_id: str, reason: str) -> None:
        with self._lock:
            self._last_frame_at.pop(camera_id, None)
            for machine in self._machines.values():
                if machine.camera_id == camera_id and machine.state != UNKNOWN:
                    self._go_unknown(machine, machine.last_sample or datetime.now(timezone.utc), reason)

    def _sample_machine(
        self,
        machine: _Machine,
        zone: Zone,
        settings: dict[str, Any],
        frame: Any,
        people: list[tuple[float, float, float, float]],
        at: datetime,
        people_age: float = 0.0,
    ) -> None:
        height, width = frame.shape[:2]
        key = (width, height, tuple((point.x, point.y) for point in zone.points))
        if machine.geometry_key != key:
            self._prepare_geometry(machine, zone, width, height)
            machine.geometry_key = key
        x1, y1, x2, y2 = machine.roi
        if x2 - x1 < 2 or y2 - y1 < 2:
            return
        crop = cv2.resize(frame[y1:y2, x1:x2], machine.roi_size, interpolation=cv2.INTER_AREA)
        machine.last_sample = at
        light = settings.get("detection") == "light"
        signal = self._light_signal if light else self._motion_signal
        moving = signal(machine, settings, crop, people, at, people_age)
        if moving is None:
            return

        if moving:
            machine.motions.append(at)
            machine.last_motion = at
        while machine.motions and (at - machine.motions[0]).total_seconds() > RUN_WINDOW_SECONDS:
            machine.motions.popleft()

        stop_after = float(settings["stop_after_seconds"])
        # A stop is only concluded after stop_after seconds of samples that
        # could see the machine; it still starts at the last movement.
        unclear = machine.last_unclear
        if machine.state != RUNNING and len(machine.motions) >= RUN_MIN_MOTION_SAMPLES:
            self._transition(machine, zone, settings, RUNNING, machine.motions[0], frame)
        elif machine.state == RUNNING and machine.last_motion and _quiet_for(at, machine.last_motion, unclear) >= stop_after:
            self._transition(machine, zone, settings, STOPPED, machine.last_motion, frame)
        elif machine.state is None and not machine.motions:
            quiet_since = machine.last_motion or machine.monitoring_since
            if _quiet_for(at, quiet_since, unclear) >= stop_after:
                self._transition(machine, zone, settings, STOPPED, quiet_since, frame)

        if moving and not machine.was_moving and machine.state == RUNNING and not light:
            self.activity.add_count(zone.id, at, "cycles")
        machine.was_moving = moving

        self._check_operator(machine, zone, settings, at, frame)
        if machine.interval_id and (machine.last_touch is None or (at - machine.last_touch).total_seconds() >= TOUCH_SECONDS):
            self.activity.touch(machine.interval_id, at)
            machine.last_touch = at

    def _motion_signal(
        self,
        machine: _Machine,
        settings: dict[str, Any],
        crop: Any,
        people: list[tuple[float, float, float, float]],
        at: datetime,
        people_age: float = 0.0,
    ) -> bool | None:
        """Movement in the area since the previous sample; None on the first one.

        An inconclusive sample (see ``_note_observation``) reports no movement.
        """
        gray = cv2.GaussianBlur(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), (5, 5), 0)
        previous, machine.prev_gray = machine.prev_gray, gray
        if previous is None or machine.monitoring_since is None:
            self._start_monitoring(machine, at)
            return None
        valid = self._visible(machine, people, people_age)
        changed = cv2.absdiff(gray, previous) > PIXEL_DELTA
        machine.activity = float(changed[valid].mean()) if valid.any() else 0.0
        moving = machine.activity >= float(settings["motion_threshold"])
        visible = valid.sum() / max(1, machine.mask.sum())
        # Movement next to boxes too old to mask cannot be told from people;
        # a machine mostly hidden behind people cannot be seen to stand still.
        stale = people_age > PERSON_OBSERVATION_MAX_AGE_SECONDS and bool(people)
        conclusive = not ((moving and stale) or visible < MIN_VISIBLE_FRACTION)
        self._note_observation(machine, people, people_age, conclusive, at)
        return moving and conclusive

    def _light_signal(
        self,
        machine: _Machine,
        settings: dict[str, Any],
        crop: Any,
        people: list[tuple[float, float, float, float]],
        at: datetime,
        people_age: float = 0.0,
    ) -> bool | None:
        """Whether the light says the machine runs; None while it is hidden.

        Boxes too old to place people hide the light as well: anyone could be
        standing in front of it now.
        """
        if machine.monitoring_since is None:
            self._start_monitoring(machine, at)
        valid = self._visible(machine, people, people_age)
        stale = people_age > PERSON_OBSERVATION_MAX_AGE_SECONDS and bool(people)
        hidden = stale or valid.sum() < machine.mask.sum() * LIGHT_MIN_VISIBLE
        self._note_observation(machine, people, people_age, not hidden, at)
        if hidden:
            return None
        level = float(_lit_pixels(crop, settings["light_color"])[valid].mean())
        machine.light_level = level
        on, off = float(settings["light_on_level"]), float(settings["light_off_level"])
        threshold = (on + off) / 2 if on > off else float(settings["light_threshold"])
        if level >= threshold:
            machine.last_lit = at
        lit = machine.last_lit is not None and (at - machine.last_lit).total_seconds() <= LIGHT_HOLD_SECONDS
        return lit if settings["light_means"] == "running" else not lit

    def _start_monitoring(self, machine: _Machine, at: datetime) -> None:
        machine.monitoring_since = at
        if machine.state == UNKNOWN:
            machine.state = None  # decide afresh once frames are back

    def _note_observation(
        self, machine: _Machine, people: list, people_age: float, conclusive: bool, at: datetime
    ) -> None:
        machine.conclusive = conclusive
        machine.observation_age = people_age if people else None
        if not conclusive:
            machine.last_unclear = at  # restarts the count towards a stop

    def _visible(
        self, machine: _Machine, people: list[tuple[float, float, float, float]], people_age: float = 0.0
    ) -> np.ndarray:
        """The machine's area minus the people in front of it.

        Each box is widened by how far its person can have walked since it was
        detected. Boxes older than PERSON_OBSERVATION_MAX_AGE_SECONDS are not
        used: the caller treats the sample as inconclusive instead.
        """
        x1, y1, x2, y2 = machine.roi
        valid = machine.mask.copy()
        scale_x, scale_y = machine.roi_size[0] / (x2 - x1), machine.roi_size[1] / (y2 - y1)
        recent = people_age <= PERSON_OBSERVATION_MAX_AGE_SECONDS
        for px1, py1, px2, py2 in people if recent else []:
            drift = (px2 - px1) * PERSON_MAX_SPEED_BOX_WIDTHS * people_age
            pad_x = (px2 - px1) * PERSON_BOX_PADDING + drift
            pad_y = (py2 - py1) * PERSON_BOX_PADDING + drift
            left = int(max(0, (px1 - pad_x - x1) * scale_x))
            top = int(max(0, (py1 - pad_y - y1) * scale_y))
            right = int(min(machine.roi_size[0], (px2 + pad_x - x1) * scale_x))
            bottom = int(min(machine.roi_size[1], (py2 + pad_y - y1) * scale_y))
            if right > left and bottom > top:
                valid[top:bottom, left:right] = False
        return valid

    def _prepare_geometry(self, machine: _Machine, zone: Zone, width: int, height: int) -> None:
        xs = [point.x * width for point in zone.points]
        ys = [point.y * height for point in zone.points]
        x1, y1 = int(max(0, min(xs))), int(max(0, min(ys)))
        x2, y2 = int(min(width, max(xs))), int(min(height, max(ys)))
        machine.roi = (x1, y1, x2, y2)
        roi_width = max(1, x2 - x1)
        scale = min(1.0, ROI_WIDTH / roi_width)
        size = (max(2, int(roi_width * scale)), max(2, int(max(1, y2 - y1) * scale)))
        machine.roi_size = size
        polygon = np.array(
            [[(x - x1) * size[0] / roi_width, (y - y1) * size[1] / max(1, y2 - y1)] for x, y in zip(xs, ys)],
            dtype=np.int32,
        )
        mask = np.zeros((size[1], size[0]), dtype=np.uint8)
        cv2.fillPoly(mask, [polygon], 1)
        machine.mask = mask.astype(bool)
        machine.prev_gray = None

    def _transition(self, machine: _Machine, zone: Zone, settings: dict[str, Any], state: str, at: datetime, frame: Any) -> None:
        if machine.state_since and at < machine.state_since:
            at = machine.state_since
        if machine.interval_id:
            self.activity.close(machine.interval_id, at)
        machine.interval_id = self.activity.open(
            kind=MACHINE, zone_id=zone.id, camera_id=zone.camera_id, state=state, started_at=at,
            metadata={"line": settings.get("line") or None},
        )
        machine.state, machine.state_since, machine.last_touch = state, at, at
        if state == RUNNING and machine.stop_event_id:
            self.events.close(machine.stop_event_id, at)
            machine.stop_event_id = None
        elif state == STOPPED and machine.stop_event_id is None:
            machine.stop_event_id = self.events.open(
                event_type="MACHINE_STOPPED",
                zone=zone,
                severity="attention",
                started_at=at,
                frame=frame,
                metadata={"line": settings.get("line") or None, "cost_per_hour": settings.get("cost_per_hour")},
            )
        if state != RUNNING and machine.operator_event_id:
            self.events.close(machine.operator_event_id, at)
            machine.operator_event_id = None

    def _check_operator(self, machine: _Machine, zone: Zone, settings: dict[str, Any], at: datetime, frame: Any) -> None:
        if not settings.get("requires_operator") or self.occupancy is None or machine.state != RUNNING:
            return
        presence, since = self.occupancy.presence(zone.id, "person")
        if presence == OCCUPIED and machine.operator_event_id:
            self.events.close(machine.operator_event_id, since or at)
            machine.operator_event_id = None
        elif presence == VACANT and since and machine.operator_event_id is None and machine.state_since:
            absent_since = max(since, machine.state_since)
            if (at - absent_since).total_seconds() >= float(settings["operator_absent_seconds"]):
                machine.operator_event_id = self.events.open(
                    event_type="MISSING_OPERATOR", zone=zone, severity="attention", started_at=absent_since, frame=frame,
                    metadata={"line": settings.get("line") or None},
                )

    def _go_unknown(self, machine: _Machine, at: datetime, reason: str) -> None:
        if machine.interval_id:
            self.activity.close(machine.interval_id, at)
            machine.interval_id = None
        for attribute in ("stop_event_id", "operator_event_id"):
            event_id = getattr(machine, attribute)
            if event_id:
                self.events.close(event_id, at, {"knowledge_state": "UNKNOWN", "uncertainty_reason": reason})
                setattr(machine, attribute, None)
        machine.state, machine.state_since = UNKNOWN, at
        # Nothing seen during the outage counts: decide afresh once it ends.
        machine.prev_gray, machine.monitoring_since, machine.last_motion = None, None, None
        machine.last_lit, machine.light_level = None, None
        machine.motions.clear()
        machine.was_moving = False


def _lit_pixels(crop: Any, color: str) -> np.ndarray:
    """Pixels of a BGR crop that look like a lit light of ``color``."""
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hue, saturation, value = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    lit = value >= LIGHT_MIN_VALUE
    if color == "white":
        return lit & (saturation <= LIGHT_WHITE_MAX_SATURATION)
    if color in LIGHT_HUES:
        in_hue = np.zeros(hue.shape, dtype=bool)
        for low, high in LIGHT_HUES[color]:
            in_hue |= (hue >= low) & (hue <= high)
        return lit & in_hue & (saturation >= LIGHT_MIN_SATURATION)
    return lit


# Geometry (normalized frame coordinates)


def _quiet_for(at: datetime, quiet_since: datetime, unclear: datetime | None) -> float:
    """Seconds of conclusive stillness: an inconclusive sample restarts the count."""
    since = max(quiet_since, unclear) if unclear is not None else quiet_since
    return (at - since).total_seconds()


def _subject(class_name: str) -> str | None:
    if class_name == "person":
        return "person"
    if class_name in VEHICLE_CLASSES:
        return "vehicle"
    return None


def _foot(detection: Any, width: int, height: int) -> tuple[float, float]:
    return ((detection.x1 + detection.x2) / 2 / max(1, width), detection.y2 / max(1, height))


def _inside(point: tuple[float, float], polygon: list[tuple[float, float]], margin: float = 0.0) -> bool:
    if _point_in_polygon(point, polygon):
        return True
    if margin <= 0:
        return False
    return min(_segment_distance(point, polygon[i], polygon[(i + 1) % len(polygon)]) for i in range(len(polygon))) <= margin


def _point_in_polygon(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _segment_distance(point: tuple[float, float], start: tuple[float, float], end: tuple[float, float]) -> float:
    (px, py), (ax, ay), (bx, by) = point, start, end
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
    cx, cy = ax + t * dx, ay + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5


def _cross(start: tuple[float, float], end: tuple[float, float], point: tuple[float, float]) -> float:
    return (end[0] - start[0]) * (point[1] - start[1]) - (end[1] - start[1]) * (point[0] - start[0])


def _segments_intersect(p1, p2, q1, q2) -> bool:
    d1, d2 = _cross(q1, q2, p1), _cross(q1, q2, p2)
    d3, d4 = _cross(p1, p2, q1), _cross(p1, p2, q2)
    return d1 * d2 < 0 and d3 * d4 < 0
