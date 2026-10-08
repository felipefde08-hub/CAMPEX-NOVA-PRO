from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import pytest

from campex_node.tracking import ByteTrackEdgeTracker, EdgeTracker, create_tracker
from campex_node.vision import EdgeDetection


START = datetime(2026, 10, 8, 8, 0, tzinfo=timezone.utc)
STEP = timedelta(seconds=0.35)  # the Node's default vision interval


def _person(x: float, confidence: float = 0.9, class_name: str = "person") -> EdgeDetection:
    return EdgeDetection(class_name=class_name, confidence=confidence, x1=x, y1=100, x2=x + 60, y2=260)


@pytest.fixture(params=["edge", "bytetrack"])
def tracker(request):
    if request.param == "bytetrack":
        pytest.importorskip("trackers")
    return create_tracker(request.param)


def test_a_walking_person_keeps_one_id_after_confirmation(tracker):
    ids = []
    for frame in range(8):
        # ~1 m/s across the image: most of a body width per sampled frame.
        (result,) = tracker.update([_person(100 + 40 * frame)], START + frame * STEP)
        ids.append(result.track_id)
    assert ids[0] is None  # one-frame false positives never get an ID
    assert ids[1] is not None and len(set(ids[1:])) == 1


def test_every_detection_comes_back_in_order(tracker):
    frame = [_person(100), _person(600), _person(900, class_name="truck")]
    tracker.update(frame, START)
    result = tracker.update(frame, START + STEP)
    assert [item.x1 for item in result] == [100, 600, 900]
    assert [item.class_name for item in result] == ["person", "person", "truck"]
    assert len({item.track_id for item in result}) == 3  # unique across classes


def test_a_track_expires_after_its_max_age(tracker):
    tracker.update([_person(100)], START)
    (first,) = tracker.update([_person(100)], START + STEP)
    (later,) = tracker.update([_person(100)], START + timedelta(seconds=10))
    assert first.track_id is not None and later.track_id != first.track_id


def test_bytetrack_needs_confidence_to_start_a_track():
    pytest.importorskip("trackers")
    tracker = ByteTrackEdgeTracker()
    results = [tracker.update([_person(100, confidence=0.4)], START + i * STEP)[0] for i in range(4)]
    assert all(item.track_id is None for item in results)


def test_bytetrack_falls_back_to_the_edge_tracker_when_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "trackers", None)  # import raises ImportError
    assert isinstance(create_tracker("bytetrack"), EdgeTracker)
