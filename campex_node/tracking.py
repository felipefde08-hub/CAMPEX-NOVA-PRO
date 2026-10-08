from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from math import hypot
from typing import Any, Iterable


logger = logging.getLogger("campex.node.tracking")


# The Node samples ~2-3 frames per second, so a walking person moves most of
# a body width between frames: plain IoU matching loses them. Tracks are
# matched on IoU against a velocity-predicted box, with a center-distance
# fallback scaled by the box size.
MIN_SIMILARITY = 0.1
MAX_CENTER_DISTANCE = 1.0  # in box diagonals
MAX_AGE_SECONDS = 3.0
CONFIRM_HITS = 2
VELOCITY_SMOOTHING = 0.5


@dataclass
class _Track:
    track_id: int
    class_name: str
    box: tuple[float, float, float, float]
    last_seen: datetime
    hits: int = 1
    velocity: tuple[float, float] = (0.0, 0.0)  # px/s of the box center

    def predicted(self, at: datetime) -> tuple[float, float, float, float]:
        dt = max(0.0, (at - self.last_seen).total_seconds())
        dx, dy = self.velocity[0] * dt, self.velocity[1] * dt
        x1, y1, x2, y2 = self.box
        return (x1 + dx, y1 + dy, x2 + dx, y2 + dy)


class EdgeTracker:
    """Gives detections stable IDs across the Node's low-rate frames.

    Returns every detection; ``track_id`` is set once a track has been seen
    on ``CONFIRM_HITS`` frames, so one-frame false positives never get an ID.
    """

    def __init__(self, max_age_seconds: float = MAX_AGE_SECONDS) -> None:
        self.max_age_seconds = max_age_seconds
        self._tracks: dict[int, _Track] = {}
        self._next_id = 1

    def update(self, detections: Iterable[Any], timestamp: datetime) -> list[Any]:
        detections = list(detections)
        self._expire(timestamp)
        pairs: list[tuple[float, int, int]] = []
        for det_index, detection in enumerate(detections):
            det_box = _box(detection)
            for track in self._tracks.values():
                if track.class_name != detection.class_name:
                    continue
                score = _similarity(track.predicted(timestamp), det_box)
                if score >= MIN_SIMILARITY:
                    pairs.append((score, det_index, track.track_id))
        pairs.sort(key=lambda item: item[0], reverse=True)

        assigned: dict[int, int] = {}
        used_tracks: set[int] = set()
        for _score, det_index, track_id in pairs:
            if det_index in assigned or track_id in used_tracks:
                continue
            assigned[det_index] = track_id
            used_tracks.add(track_id)

        results = []
        for det_index, detection in enumerate(detections):
            box = _box(detection)
            track_id = assigned.get(det_index)
            if track_id is None:
                track = _Track(self._next_id, detection.class_name, box, timestamp)
                self._tracks[track.track_id] = track
                self._next_id += 1
            else:
                track = self._tracks[track_id]
                _advance(track, box, timestamp)
            confirmed = track.hits >= CONFIRM_HITS
            results.append(replace(detection, track_id=track.track_id if confirmed else None))
        return results

    def reset(self) -> None:
        self._tracks.clear()

    def _expire(self, timestamp: datetime) -> None:
        for track_id, track in list(self._tracks.items()):
            if (timestamp - track.last_seen).total_seconds() > self.max_age_seconds:
                del self._tracks[track_id]


def _advance(track: _Track, box: tuple[float, float, float, float], timestamp: datetime) -> None:
    dt = (timestamp - track.last_seen).total_seconds()
    if dt > 0:
        old_cx, old_cy = _center(track.box)
        new_cx, new_cy = _center(box)
        vx, vy = (new_cx - old_cx) / dt, (new_cy - old_cy) / dt
        track.velocity = (
            track.velocity[0] * (1 - VELOCITY_SMOOTHING) + vx * VELOCITY_SMOOTHING,
            track.velocity[1] * (1 - VELOCITY_SMOOTHING) + vy * VELOCITY_SMOOTHING,
        )
    track.box = box
    track.last_seen = timestamp
    track.hits += 1


def _similarity(predicted: tuple[float, float, float, float], box: tuple[float, float, float, float]) -> float:
    iou = _iou(predicted, box)
    if iou > 0:
        return iou
    diagonal = max(1.0, hypot(predicted[2] - predicted[0], predicted[3] - predicted[1]))
    (pcx, pcy), (cx, cy) = _center(predicted), _center(box)
    distance = hypot(cx - pcx, cy - pcy) / diagonal
    if distance >= MAX_CENTER_DISTANCE:
        return 0.0
    # Never outranks a real overlap of the same quality.
    return 0.5 * (1.0 - distance / MAX_CENTER_DISTANCE)


def _box(detection: Any) -> tuple[float, float, float, float]:
    return (float(detection.x1), float(detection.y1), float(detection.x2), float(detection.y2))


def _center(box: tuple[float, float, float, float]) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


def _iou(first: tuple[float, float, float, float], second: tuple[float, float, float, float]) -> float:
    x1, y1 = max(first[0], second[0]), max(first[1], second[1])
    x2, y2 = min(first[2], second[2]), min(first[3], second[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if intersection == 0:
        return 0.0
    union = _area(first) + _area(second) - intersection
    return intersection / union if union else 0.0


def _area(box: tuple[float, float, float, float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


# ByteTrack (Roboflow's ``trackers`` package, Apache-2.0). It predicts with a
# Kalman filter driven by each frame's timestamp, so the Node's low, uneven
# frame rate does not break it. Detections below the activation threshold
# never start a track; they only keep an existing one alive (ByteTrack's
# "second association").
BYTETRACK_ACTIVATION = 0.5
BYTETRACK_HIGH_CONFIDENCE = 0.5
BYTETRACK_MIN_IOU = 0.1


class ByteTrackEdgeTracker:
    """Same contract as ``EdgeTracker``, backed by ByteTrack.

    One ByteTrack per class keeps a person and a vehicle from swapping IDs;
    the Node hands out its own IDs so they stay unique across classes.
    """

    def __init__(self, max_age_seconds: float = MAX_AGE_SECONDS) -> None:
        import trackers  # optional dependency: raises ImportError when missing

        self._factory = lambda: trackers.ByteTrackTracker(
            # Counted in 30 FPS frames; with timestamps it becomes seconds * 30.
            lost_track_buffer=max(1, round(max_age_seconds * 30)),
            frame_rate=30.0,
            track_activation_threshold=BYTETRACK_ACTIVATION,
            minimum_consecutive_frames=CONFIRM_HITS,
            minimum_iou_threshold=BYTETRACK_MIN_IOU,
            high_conf_det_threshold=BYTETRACK_HIGH_CONFIDENCE,
        )
        self._trackers: dict[str, Any] = {}
        self._ids: dict[tuple[str, int], int] = {}
        self._next_id = 1

    def update(self, detections: Iterable[Any], timestamp: datetime) -> list[Any]:
        import numpy as np
        import supervision as sv

        detections = list(detections)
        results: list[Any] = [replace(detection, track_id=None) for detection in detections]
        by_class: dict[str, list[int]] = {}
        for index, detection in enumerate(detections):
            by_class.setdefault(detection.class_name, []).append(index)
        seconds = timestamp.timestamp()
        # Classes absent from this frame still age their tracks.
        for class_name in self._trackers.keys() - by_class.keys():
            self._trackers[class_name].update(sv.Detections.empty(), timestamp=seconds)
        for class_name, indexes in by_class.items():
            tracker = self._trackers.get(class_name)
            if tracker is None:
                tracker = self._trackers[class_name] = self._factory()
            batch = sv.Detections(
                xyxy=np.array([_box(detections[i]) for i in indexes], dtype=np.float32),
                confidence=np.array([float(detections[i].confidence) for i in indexes], dtype=np.float32),
                class_id=np.zeros(len(indexes), dtype=int),
                data={"index": np.array(indexes, dtype=int)},
            )
            tracked = tracker.update(batch, timestamp=seconds)
            if tracked.tracker_id is None:
                continue
            # ByteTrack reorders its output; "index" points back to the input.
            for index, raw_id in zip(tracked.data["index"], tracked.tracker_id):
                if raw_id >= 0:
                    results[int(index)] = replace(detections[int(index)], track_id=self._node_id(class_name, int(raw_id)))
        return results

    def reset(self) -> None:
        self._trackers.clear()
        self._ids.clear()

    def _node_id(self, class_name: str, raw_id: int) -> int:
        key = (class_name, raw_id)
        if key not in self._ids:
            self._ids[key] = self._next_id
            self._next_id += 1
        return self._ids[key]


def create_tracker(kind: str) -> Any:
    """``EdgeTracker`` or ``ByteTrackEdgeTracker``; ByteTrack falls back to
    the Node's own tracker when its package is not installed."""
    if kind == "bytetrack":
        try:
            return ByteTrackEdgeTracker()
        except ImportError:
            _warn_bytetrack_missing()
    return EdgeTracker()


_bytetrack_warned = False


def _warn_bytetrack_missing() -> None:
    global _bytetrack_warned
    if not _bytetrack_warned:
        _bytetrack_warned = True
        logger.warning(
            "CAMPEX_NODE_VISION_TRACKER=bytetrack but the 'trackers' package is missing; using the edge tracker"
        )
