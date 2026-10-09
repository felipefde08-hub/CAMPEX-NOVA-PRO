"""Vision scheduler: newest frames first, fair per camera, events kept in order.

Synthetic detectors only: these tests check the plumbing (timing, fairness,
queues, synchronization), not the accuracy of any model.
"""

from __future__ import annotations

import random
import threading
import time
import tracemalloc
from dataclasses import replace

from campex_node.vision import EdgeDetection

from tests.campex_node.test_live_relay import _settings
from tests.campex_node.test_vision_sync import (
    BufferedCameras,
    OracleVision,
    assert_synchronized,
    blob_box,
    box_of,
    camera,
    scene,
)


class IdentityCameras(BufferedCameras):
    """Adds the copy-free identity peek the real CameraManager offers."""

    def latest_identity(self, camera_id):
        return self.buffers[camera_id].identity()


class Feeder:
    """Delivers frames to every camera at ``fps``, the person walking 4 px per frame."""

    def __init__(self, cams, camera_ids, fps: float = 30.0, first_index: int = 0) -> None:
        self.cams, self.camera_ids, self.fps = cams, list(camera_ids), fps
        self.first_index = first_index
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        x = 0
        while not self.stop.is_set():
            for index, camera_id in enumerate(self.camera_ids):
                self.cams.put(camera_id, scene(x, camera=(self.first_index + index) % 4))
            x = (x + 4) % 200
            self.stop.wait(1 / self.fps)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join()


class CountingVision(OracleVision):
    """Oracle detector that records which camera each analysis served."""

    def __init__(self, *args, latency_by_camera=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.latency_by_camera = latency_by_camera or {}
        self.calls: list[tuple[float, int]] = []

    def _detect(self, frame):
        camera_index = int(blob_box(frame)[3]) // 10 - 6 if blob_box(frame) else -1
        self.calls.append((time.monotonic(), camera_index))
        latency = self.latency_by_camera.get(camera_index, self.latency)
        if callable(latency):
            latency = latency()
        box = blob_box(frame)
        if latency:
            time.sleep(latency)
        return ([EdgeDetection("person", 0.9, *box)] if box else []), "oracle"

    def rate(self, camera_index: int, since: float) -> float:
        calls = [at for at, index in self.calls if index == camera_index and at >= since]
        return len(calls) / max(1e-6, time.monotonic() - since)


def run_for(vision, seconds: float, check=None) -> None:
    vision.start()
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if check is not None:
                check()
            time.sleep(0.01)
    finally:
        vision.stop()


def test_one_camera_reaches_the_configured_rate_with_synchronized_boxes(tmp_path):
    cams = IdentityCameras([camera()])
    vision = CountingVision(_settings(tmp_path), cams, latency=0.02)

    def check():
        result = vision.overlay_result("cam")
        if result is not None and result.detections:
            assert_synchronized(result)

    with Feeder(cams, ["cam"]):
        time.sleep(0.1)
        started = time.monotonic()
        run_for(vision, 2.0, check)

    rate = vision.rate(0, started)
    assert 3.0 <= rate <= 5.5  # vision_interval_seconds = 0.2 caps it at 5 FPS
    starts = [at for at, _ in vision.calls]
    # Never faster than the interval (time.monotonic ticks ~15 ms on Windows).
    assert min(b - a for a, b in zip(starts, starts[1:])) >= 0.17


def test_four_cameras_share_the_detector_evenly(tmp_path):
    cams = IdentityCameras([camera(f"cam{index}") for index in range(4)])
    vision = CountingVision(_settings(tmp_path), cams, latency=0.03)

    with Feeder(cams, [f"cam{index}" for index in range(4)]):
        time.sleep(0.1)
        started = time.monotonic()
        run_for(vision, 2.5)

    rates = [vision.rate(index, started) for index in range(4)]
    assert min(rates) >= 3.0, rates
    assert min(rates) >= 0.75 * max(rates), rates
    for index in range(4):
        result = vision.latest_result(f"cam{index}")
        assert result.camera_id == f"cam{index}" and result.detections[0].y2 == 60 + 10 * index


def test_an_expensive_camera_does_not_hold_back_the_others(tmp_path):
    cams = IdentityCameras([camera(f"cam{index}") for index in range(3)])
    settings = replace(_settings(tmp_path), vision_max_busy_ratio=1.0)
    vision = CountingVision(settings, cams, latency=0.02, latency_by_camera={0: 0.25})

    with Feeder(cams, [f"cam{index}" for index in range(3)]):
        time.sleep(0.1)
        started = time.monotonic()
        run_for(vision, 3.0)

    slow, fast = vision.rate(0, started), [vision.rate(index, started) for index in (1, 2)]
    # The old loop ran every camera in turn: all three at ~1.5 FPS here.
    assert min(fast) >= 2.5, fast
    assert slow >= 0.8, slow  # still served, not starved


def test_a_frozen_camera_is_not_analysed_again_and_does_not_block_others(tmp_path):
    cams = IdentityCameras([camera("frozen"), camera("live")])
    vision = CountingVision(_settings(tmp_path), cams, latency=0.01)
    cams.put("frozen", scene(30, camera=0))  # one frame, then nothing new

    with Feeder(cams, ["live"], first_index=1):
        time.sleep(0.1)
        cams.put("live", scene(30, camera=1))
        started = time.monotonic()
        run_for(vision, 1.5)

    frozen_calls = [index for _, index in vision.calls if index == 0]
    assert len(frozen_calls) == 1
    assert vision.rate(1, started) >= 3.0


def test_only_the_newest_frame_is_analysed_and_skipped_frames_are_counted(tmp_path):
    cams = IdentityCameras([camera()])
    settings = replace(_settings(tmp_path), vision_interval_seconds=0.001)
    vision = CountingVision(settings, cams)
    cams.put("cam", scene(10))
    vision._run_once()
    for x in (20, 30, 40, 50, 60):  # five frames arrive during the next inference
        cams.put("cam", scene(x))
    time.sleep(0.03)  # past the 1 ms interval even with a ~15 ms clock tick
    vision._run_once()

    result = vision.latest_result("cam")
    assert result.frame_id == 6 and box_of(result.detections[0])[0] == 60
    assert vision.status("cam")["metrics"]["frames_skipped"] == 4
    assert vision._run_once() is None  # nothing new: no second analysis of frame 6


def test_a_camera_restart_does_not_count_frames_of_the_old_session_as_skipped(tmp_path):
    cams = IdentityCameras([camera()])
    vision = CountingVision(_settings(tmp_path), cams)
    for x in (10, 20, 30):
        cams.put("cam", scene(x))
        vision._process_enabled_cameras()
    cams.restart("cam")
    cams.put("cam", scene(40))
    vision._process_enabled_cameras()

    assert vision.latest_result("cam").frame_id == 1
    assert vision.status("cam")["metrics"]["frames_skipped"] == 0


def test_without_yolo_the_hog_fallback_runs_at_the_scheduled_rate(tmp_path):
    from campex_node.vision import EdgeVisionService

    cams = IdentityCameras([camera()])
    vision = EdgeVisionService(_settings(tmp_path), cams)
    vision._detector.get = lambda: None  # YOLO weights could not load
    hog_calls = []
    original = vision._detect_people_hog
    vision._detect_people_hog = lambda frame: hog_calls.append(1) or original(frame)

    with Feeder(cams, ["cam"]):
        time.sleep(0.1)
        run_for(vision, 1.0)

    status = vision.status("cam")
    assert status["detector"] == "opencv-hog" and status["error"] is None
    assert 2 <= len(hog_calls) <= 6  # paced by the scheduler, no busy loop


def test_variable_inference_latency_keeps_boxes_on_their_frames(tmp_path):
    cams = IdentityCameras([camera(f"cam{index}") for index in range(2)])
    rng = random.Random(3)
    vision = CountingVision(_settings(tmp_path), cams, latency=lambda: rng.uniform(0.0, 0.12))
    published = set()

    def check():
        for index in range(2):
            result = vision.overlay_result(f"cam{index}")
            if result is not None and result.detections:
                assert_synchronized(result)
                published.add(result.key)

    with Feeder(cams, ["cam0", "cam1"]):
        time.sleep(0.1)
        run_for(vision, 2.0, check)

    assert len(published) >= 6


class SlowObserver:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.seen: list[tuple[str, str, int | None]] = []

    def observe(self, camera_id, frame, detections, observed_at):
        time.sleep(self.delay)
        self.seen.append(("frame", camera_id, int(detections[0].x1) if detections else None))

    def camera_unavailable(self, camera_id, reason):
        self.seen.append(("unavailable", camera_id, None))


def test_stop_finishes_queued_event_work_and_ends_both_workers(tmp_path):
    cams = IdentityCameras([camera()])
    observer = SlowObserver(delay=0.05)
    vision = CountingVision(_settings(tmp_path), cams, observers=[observer], latency=0.005)
    submitted = []
    original_submit = vision._post.submit
    vision._post.submit = lambda task: submitted.append(1) or original_submit(task)

    with Feeder(cams, ["cam"]):
        time.sleep(0.1)
        run_for(vision, 1.0)

    assert not vision._thread.is_alive()
    assert not vision._post._thread.is_alive()
    assert len(observer.seen) == len(submitted) > 0  # nothing queued was lost on stop


def test_overloaded_event_work_slows_inference_instead_of_growing_or_dropping(tmp_path):
    cams = IdentityCameras([camera()])
    settings = replace(_settings(tmp_path), vision_post_queue_size=2, vision_interval_seconds=0.01)
    observer = SlowObserver(delay=0.1)  # events 10x slower than inference
    vision = CountingVision(settings, cams, observers=[observer], latency=0.005)
    analysed = []
    original_observe = vision._observe_frame
    vision._observe_frame = lambda *args: analysed.append(1) or original_observe(*args)

    with Feeder(cams, ["cam"]):
        time.sleep(0.1)
        run_for(vision, 1.5)

    assert vision._post.max_depth <= 2
    assert vision._post.full_waits > 0
    assert len([item for item in observer.seen if item[0] == "frame"]) == len(analysed)
    assert len(vision.calls) <= 25  # held to the event rate (~10/s), not 100/s


def test_events_see_every_analysed_frame_in_order(tmp_path):
    cams = IdentityCameras([camera()])
    observer = SlowObserver(delay=0.01)
    vision = CountingVision(_settings(tmp_path), cams, observers=[observer], latency=0.01)
    stop = threading.Event()

    def feed():
        x = 0
        while not stop.is_set():
            cams.put("cam", scene(x))
            x += 1
            time.sleep(1 / 30)

    feeder = threading.Thread(target=feed, daemon=True)
    feeder.start()
    vision.start()
    time.sleep(1.0)
    cams.cameras = [replace(cams.cameras[0], vision_enabled=False)]  # vision switched off
    time.sleep(0.3)
    stop.set()
    feeder.join()
    vision.stop()

    frames = [x for kind, _, x in observer.seen if kind == "frame"]
    assert frames == sorted(frames) and len(frames) >= 3
    assert observer.seen[-1] == ("unavailable", "cam", None)  # after the last frame


def test_long_run_memory_stays_bounded(tmp_path):
    cams = IdentityCameras([camera(f"cam{index}") for index in range(4)])
    vision = CountingVision(_settings(tmp_path), cams, latency=0.01)

    with Feeder(cams, [f"cam{index}" for index in range(4)]):
        vision.start()
        try:
            time.sleep(1.0)
            tracemalloc.start()
            baseline = tracemalloc.get_traced_memory()[0]
            time.sleep(3.0)
            grown = tracemalloc.get_traced_memory()[0] - baseline
            tracemalloc.stop()
        finally:
            vision.stop()

    assert grown < 8 * 2**20, grown
    assert len(vision._results) <= 4 and len(vision._schedules) == 4
    assert vision._post.max_depth <= vision.settings.vision_post_queue_size
    assert all(len(tracker._tracks) <= 3 for tracker in vision._trackers.values())


def test_status_reports_the_new_timing_metrics(tmp_path):
    cams = IdentityCameras([camera()])
    vision = CountingVision(_settings(tmp_path), cams, latency=0.02)
    cams.put("cam", scene(10))
    vision._run_once()

    metrics = vision.status("cam")["metrics"]
    assert metrics["inference_latency_ms"] >= 20
    assert metrics["frame_age_ms"] is not None and metrics["known_latency_ms"] >= metrics["inference_latency_ms"]
    assert metrics["overlay_age_ms"] >= metrics["known_latency_ms"]
    assert metrics["queue_depth"] == 0 and metrics["frames_skipped"] == 0
    assert metrics["time_basis"] == "node_receipt"
    # Fields read by the panels and the Cloud are unchanged.
    assert {"camera_fps", "vision_fps", "inference_ms", "frames_processed", "frames_received", "objects_detected"} <= set(metrics)
