from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np

from campex_node.events import NodeEventPipeline, NodeEventStore
from campex_node.vision import EdgeDetection


DOOR = [[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]]


def _store(tmp_path) -> NodeEventStore:
    store = NodeEventStore(tmp_path / "node.sqlite3")
    store.initialize()
    return store


def _person(x1: float) -> EdgeDetection:
    return EdgeDetection("person", 0.9, x1, 100.0, x1 + 80.0, 300.0)


def _run(pipeline: NodeEventPipeline, steps: list[list[EdgeDetection]]) -> None:
    started = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc)
    for index, detections in enumerate(steps):
        frame = np.full((360, 640, 3), index * 3 % 255, dtype=np.uint8)
        pipeline.process(
            "cam-6",
            frame,
            detections,
            now=100.0 + index,
            observed_at=started + timedelta(seconds=index),
        )


def test_person_crossing_a_zone_becomes_a_closed_event_with_clip(tmp_path):
    store = _store(tmp_path)
    store.create_zone(camera_id="cam-6", name="Porta", zone_type="monitored", points=DOOR)
    pipeline = NodeEventPipeline(store, tmp_path, fps=1.0)

    inside = [[_person(60.0)] for _ in range(6)]
    outside = [[_person(500.0)] for _ in range(8)]
    _run(pipeline, inside + outside)

    events = store.list_events()
    assert len(events) == 1
    event = events[0]
    assert event.type == "PERSON_MONITORED_ZONE"
    assert event.status == "CLOSED"
    assert event.started_at.startswith("2026-10-06T22:00:01")
    assert event.ended_at is not None
    # Tracked from the 2nd frame (t=1) until last seen inside (t=5).
    assert event.duration == 4.0
    clip = Path(event.metadata["clip_path"])
    assert clip.is_file() and clip.stat().st_size > 0
    capture = cv2.VideoCapture(str(clip))
    frames = 0
    while capture.read()[0]:
        frames += 1
    assert frames >= 6
    assert Path(event.metadata["overlay_path"]).name == "overlay.jpg"


def test_event_stays_open_while_person_is_in_the_zone(tmp_path):
    store = _store(tmp_path)
    store.create_zone(camera_id="cam-6", name="Porta", zone_type="monitored", points=DOOR)
    pipeline = NodeEventPipeline(store, tmp_path, fps=1.0)

    _run(pipeline, [[_person(60.0)] for _ in range(4)])

    [event] = store.list_events()
    assert event.status == "OPEN"
    assert event.ended_at is None
    assert "clip_path" not in event.metadata


def test_losing_the_camera_finishes_the_clip_without_inventing_an_exit(tmp_path):
    store = _store(tmp_path)
    store.create_zone(camera_id="cam-6", name="Porta", zone_type="monitored", points=DOOR)
    pipeline = NodeEventPipeline(store, tmp_path, fps=1.0)
    _run(pipeline, [[_person(60.0)] for _ in range(4)])

    pipeline.camera_unavailable("cam-6", "camera_observation_unavailable")

    [event] = store.list_events()
    assert event.ended_at is None
    assert event.metadata["knowledge_state"] == "UNKNOWN"
    assert Path(event.metadata["clip_path"]).is_file()


def test_cameras_without_zones_produce_no_events(tmp_path):
    store = _store(tmp_path)
    pipeline = NodeEventPipeline(store, tmp_path, fps=1.0)

    _run(pipeline, [[_person(60.0)] for _ in range(4)])

    assert store.list_events() == []


def test_open_events_from_a_previous_run_are_closed_on_start(tmp_path):
    store = _store(tmp_path)
    created = store.create(event_type="PERSON_MONITORED_ZONE", camera_id="cam-6")

    NodeEventStore(tmp_path / "node.sqlite3").initialize()

    event = store.get(created.id)
    assert event.status == "CLOSED"
    assert event.metadata["uncertainty_reason"] == "node_restarted"
