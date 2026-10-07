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

# Machine motion analysis
ROI_WIDTH = 160
PIXEL_DELTA = 25
RUN_WINDOW_SECONDS = 5.0
RUN_MIN_MOTION_SAMPLES = 2
PERSON_BOX_PADDING = 0.15


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
    - DOCK_VISIT: a vehicle parked at a dock for at least ``min_visit_seconds``.
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
            self._forget_removed(camera_id, {zone.id for zone, _ in zones})
            self._prune_lines(observed_at)

    def camera_unavailable(self, camera_id: str, reason: str) -> None:
        with self._lock:
            for key, item in list(self._presence.items()):
                if item.camera_id != camera_id:
                    continue
                self._release(item, reason)
                del self._presence[key]

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

    def _release(self, item: _Presence, reason: str) -> None:
        ended = item.last_frame or item.state_since
        if item.interval_id and ended:
            self.activity.close(item.interval_id, ended, item.peak)
        if item.event_id and ended:
            self.events.close(item.event_id, ended, {"knowledge_state": "UNKNOWN", "uncertainty_reason": reason})

    def _forget_removed(self, camera_id: str, zone_ids: set[str]) -> None:
        for key, item in list(self._presence.items()):
            if item.camera_id == camera_id and item.zone_id not in zone_ids:
                self._release(item, "zone_removed")
                del self._presence[key]

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


class MachineMonitor:
    """Decides whether each machine is running from movement in its area.

    Every sample compares the machine's area with the previous frame (people
    detected by the vision loop are masked out, so an operator walking past
    does not count as the machine moving):

    - RUNNING after movement on ``RUN_MIN_MOTION_SAMPLES`` samples within
      ``RUN_WINDOW_SECONDS``;
    - STOPPED after ``stop_after_seconds`` without movement, starting at the
      last movement seen;
    - UNKNOWN while the camera is offline or frozen (never counted as downtime).

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
        people: Callable[[str], list[tuple[float, float, float, float]]] | None = None,
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
                    "last_motion_at": iso(item.last_motion),
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
        people = self.people(camera_id)
        with self._lock:
            for zone, settings in machines:
                machine = self._machines.setdefault(zone.id, _Machine(zone.id, camera_id))
                self._sample_machine(machine, zone, settings, frame, people, at)
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
    ) -> None:
        height, width = frame.shape[:2]
        key = (width, height, tuple((point.x, point.y) for point in zone.points))
        if machine.geometry_key != key:
            self._prepare_geometry(machine, zone, width, height)
            machine.geometry_key = key
        x1, y1, x2, y2 = machine.roi
        if x2 - x1 < 2 or y2 - y1 < 2:
            return
        gray = cv2.cvtColor(cv2.resize(frame[y1:y2, x1:x2], machine.roi_size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        previous, machine.prev_gray = machine.prev_gray, gray
        machine.last_sample = at
        if previous is None or machine.monitoring_since is None:
            machine.monitoring_since = at
            if machine.state == UNKNOWN:
                machine.state = None  # decide afresh once frames are back
            return

        valid = machine.mask.copy()
        scale_x, scale_y = machine.roi_size[0] / (x2 - x1), machine.roi_size[1] / (y2 - y1)
        for px1, py1, px2, py2 in people:
            pad_x, pad_y = (px2 - px1) * PERSON_BOX_PADDING, (py2 - py1) * PERSON_BOX_PADDING
            left = int(max(0, (px1 - pad_x - x1) * scale_x))
            top = int(max(0, (py1 - pad_y - y1) * scale_y))
            right = int(min(machine.roi_size[0], (px2 + pad_x - x1) * scale_x))
            bottom = int(min(machine.roi_size[1], (py2 + pad_y - y1) * scale_y))
            if right > left and bottom > top:
                valid[top:bottom, left:right] = False
        changed = cv2.absdiff(gray, previous) > PIXEL_DELTA
        machine.activity = float(changed[valid].mean()) if valid.any() else 0.0
        moving = machine.activity >= float(settings["motion_threshold"])

        if moving:
            machine.motions.append(at)
            machine.last_motion = at
        while machine.motions and (at - machine.motions[0]).total_seconds() > RUN_WINDOW_SECONDS:
            machine.motions.popleft()

        stop_after = float(settings["stop_after_seconds"])
        if machine.state != RUNNING and len(machine.motions) >= RUN_MIN_MOTION_SAMPLES:
            self._transition(machine, zone, settings, RUNNING, machine.motions[0], frame)
        elif machine.state == RUNNING and machine.last_motion and (at - machine.last_motion).total_seconds() >= stop_after:
            self._transition(machine, zone, settings, STOPPED, machine.last_motion, frame)
        elif machine.state is None and not machine.motions:
            quiet_since = machine.last_motion or machine.monitoring_since
            if (at - quiet_since).total_seconds() >= stop_after:
                self._transition(machine, zone, settings, STOPPED, quiet_since, frame)

        if moving and not machine.was_moving and machine.state == RUNNING:
            self.activity.add_count(zone.id, at, "cycles")
        machine.was_moving = moving

        self._check_operator(machine, zone, settings, at, frame)
        if machine.interval_id and (machine.last_touch is None or (at - machine.last_touch).total_seconds() >= TOUCH_SECONDS):
            self.activity.touch(machine.interval_id, at)
            machine.last_touch = at

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
        machine.motions.clear()
        machine.was_moving = False


# Geometry (normalized frame coordinates)


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
