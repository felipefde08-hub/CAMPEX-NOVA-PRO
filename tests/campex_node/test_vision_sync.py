"""Boxes are only ever shown on the frame they were detected on.

The detector is replaced by an oracle that finds the white "person" rectangle
in the very frame it receives, so a box and the rectangle in its frame must
match exactly; any mix of two frames shows up as a mismatch.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from backend.cameras.frame_buffer import LatestFrameBuffer
from campex_node import local_app
from campex_node.core.config import NodeCameraConfig
from campex_node.vision import EdgeDetection, EdgeVisionService

from tests.campex_node.test_live_relay import FakeCameraManager, _settings


def scene(x: int, camera: int = 0) -> np.ndarray:
    """A black frame with one white person; its height tells the camera apart."""
    frame = np.zeros((120, 240, 3), dtype=np.uint8)
    frame[20 : 60 + 10 * camera, x : x + 20] = 255
    return frame


def blob_box(frame) -> tuple[float, float, float, float] | None:
    mask = np.all(frame == 255, axis=2)
    if not mask.any():
        return None
    ys, xs = np.nonzero(mask)
    return float(xs.min()), float(ys.min()), float(xs.max() + 1), float(ys.max() + 1)


def box_of(detection) -> tuple[float, float, float, float]:
    return detection.x1, detection.y1, detection.x2, detection.y2


class BufferedCameras:
    """Camera manager backed by the real LatestFrameBuffer, one per camera."""

    def __init__(self, cameras):
        self.cameras = list(cameras)
        self.buffers = {camera.id: LatestFrameBuffer() for camera in cameras}

    def configs(self):
        return list(self.cameras)

    def states(self):
        return []

    def put(self, camera_id: str, frame) -> None:
        self.buffers[camera_id].put(frame, datetime.now(timezone.utc))

    def latest_frame(self, camera_id):
        return self.buffers[camera_id].latest()

    def latest_snapshot(self, camera_id):
        return self.buffers[camera_id].snapshot()

    def restart(self, camera_id: str) -> None:
        # CameraManager starts a new CameraWorker, which owns a new buffer.
        self.buffers[camera_id] = LatestFrameBuffer()


class OracleVision(EdgeVisionService):
    def __init__(self, *args, latency: float = 0.0, during_inference=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.latency = latency
        self.during_inference = during_inference

    def _detect(self, frame):
        box = blob_box(frame)
        if self.during_inference is not None:
            self.during_inference()  # the camera keeps delivering frames meanwhile
        if self.latency:
            time.sleep(self.latency)
        return ([EdgeDetection("person", 0.9, *box)] if box else []), "oracle"


def camera(camera_id: str = "cam", **flags) -> NodeCameraConfig:
    return NodeCameraConfig(id=camera_id, name=camera_id, rtsp_url="rtsp://camera/1", vision_enabled=True, **flags)


def assert_synchronized(result) -> None:
    assert result is not None
    assert len(result.detections) == 1
    assert box_of(result.detections[0]) == blob_box(result.frame)


def test_standing_person_box_stays_on_the_person(tmp_path):
    cams = BufferedCameras([camera()])
    vision = OracleVision(_settings(tmp_path), cams)
    for _ in range(3):
        cams.put("cam", scene(50))
        vision._process_enabled_cameras()

    result = vision.latest_result("cam")
    assert_synchronized(result)
    assert box_of(result.detections[0]) == blob_box(cams.latest_frame("cam")[0])


def test_walking_person_is_drawn_on_the_frame_it_was_detected_on(tmp_path):
    cams = BufferedCameras([camera()])
    vision = OracleVision(_settings(tmp_path), cams, during_inference=lambda: cams.put("cam", scene(110)))
    cams.put("cam", scene(50))

    vision._process_enabled_cameras()

    result = vision.latest_result("cam")
    assert_synchronized(result)
    # The camera moved on: drawing these boxes on the newest frame (the old
    # behaviour) would put them 60 px behind the person.
    assert blob_box(cams.latest_frame("cam")[0])[0] - result.detections[0].x1 == 60
    rendered = vision.render_result(result)
    assert rendered is not result.frame and result.frame.flags.writeable is False
    assert box_of(result.detections[0]) == blob_box(result.frame)  # drawing did not touch the analysed frame


@pytest.mark.parametrize("latency", [0.05, 0.15, 0.40])
def test_slow_inference_never_mixes_frames(tmp_path, latency):
    cams = BufferedCameras([camera()])
    settings = replace(_settings(tmp_path), vision_interval_seconds=0.01)
    vision = OracleVision(settings, cams, latency=latency)
    stop = threading.Event()

    def camera_feed():
        x = 0
        while not stop.is_set():
            cams.put("cam", scene(x))
            x = (x + 4) % 200
            time.sleep(1 / 30)

    feeder = threading.Thread(target=camera_feed, daemon=True)
    feeder.start()
    vision.start()
    seen, stale_against_live = set(), 0
    deadline = time.monotonic() + 3 * latency + 0.8
    try:
        while time.monotonic() < deadline:
            result = vision.overlay_result("cam")
            if result is not None and result.detections:
                assert_synchronized(result)
                seen.add(result.key)
                live = blob_box(cams.latest_frame("cam")[0])
                stale_against_live += box_of(result.detections[0]) != live
            time.sleep(0.005)
    finally:
        stop.set()
        vision.stop()
        feeder.join()

    assert len(seen) >= 2
    # Sanity check of the test itself: the live frame really is ahead of the
    # analysed one, so a mix of the two would have been caught above.
    assert stale_against_live > 0


def test_four_cameras_keep_their_own_frames_and_ids(tmp_path):
    configs = [camera(f"cam{index}") for index in range(4)]
    cams = BufferedCameras(configs)
    vision = OracleVision(_settings(tmp_path), cams)
    for step in range(3):
        for index in range(4):
            cams.put(f"cam{index}", scene(20 + 40 * index + step, camera=index))
        vision._process_enabled_cameras()

    results = [vision.latest_result(f"cam{index}") for index in range(4)]
    for index, result in enumerate(results):
        assert result.camera_id == f"cam{index}"
        assert_synchronized(result)
        assert result.detections[0].y2 == 60 + 10 * index  # the person of this camera
        assert result.frame_id == 3
    assert len({result.session_id for result in results}) == 4


def _runtime(vision, cams):
    return SimpleNamespace(lifecycle=SimpleNamespace(vision=vision, camera_manager=cams))


def _first_part(stream) -> tuple[dict[str, str], bytes]:
    part = next(stream)
    head, _, body = part.partition(b"\r\n\r\n")
    headers = dict(line.split(": ", 1) for line in head.decode("ascii").split("\r\n")[1:])
    return headers, body[:-2]


def _decoded_blob(jpeg: bytes):
    image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    mask = image.min(axis=2) > 200
    ys, xs = np.nonzero(mask)
    return int(round(xs.min())), int(round(xs.max()))


def test_stream_without_overlay_keeps_showing_the_live_frame(tmp_path):
    cams = BufferedCameras([camera()])
    vision = OracleVision(_settings(tmp_path), cams, during_inference=lambda: cams.put("cam", scene(150)))
    cams.put("cam", scene(30))
    vision._process_enabled_cameras()

    headers, jpeg = _first_part(local_app._mjpeg_frames(_runtime(vision, cams), "cam", overlay=False))

    assert headers["X-CAMPEX-Overlay"] == "none"
    left, _ = _decoded_blob(jpeg)
    assert abs(left - 150) <= 2  # the newest frame, not the analysed one at x=30


def test_stream_with_overlay_sends_the_analysed_frame_with_its_boxes(tmp_path):
    cams = BufferedCameras([camera()])
    vision = OracleVision(_settings(tmp_path), cams, during_inference=lambda: cams.put("cam", scene(150)))
    cams.put("cam", scene(30))
    vision._process_enabled_cameras()
    result = vision.latest_result("cam")

    headers, jpeg = _first_part(local_app._mjpeg_frames(_runtime(vision, cams), "cam", overlay=True))

    assert headers["X-CAMPEX-Overlay"] == "synced"
    assert headers["X-CAMPEX-Frame-Id"] == f"{result.session_id}:{result.frame_id}"
    assert jpeg == local_app._encode_jpeg(vision.render_result(result), quality=68)


def test_overlay_before_the_first_analysis_shows_the_live_frame_without_boxes(tmp_path):
    cams = BufferedCameras([camera()])
    vision = OracleVision(_settings(tmp_path), cams)
    cams.put("cam", scene(30))

    headers, jpeg = _first_part(local_app._mjpeg_frames(_runtime(vision, cams), "cam", overlay=True))

    assert headers["X-CAMPEX-Overlay"] == "unavailable"
    assert "X-CAMPEX-Frame-Id" not in headers
    assert jpeg == local_app._encode_jpeg(scene(30), quality=68)


def test_stopping_vision_drops_the_last_boxes(tmp_path):
    cams = BufferedCameras([camera()])
    vision = OracleVision(_settings(tmp_path), cams)
    cams.put("cam", scene(30))
    vision._process_enabled_cameras()
    assert vision.latest_result("cam") is not None

    cams.cameras = [replace(cams.cameras[0], vision_enabled=False)]
    vision._process_enabled_cameras()

    assert vision.latest_result("cam") is None
    assert vision.objects("cam") == [] and vision.cloud_objects("cam") == [] and vision.people_boxes("cam") == []
    assert vision.status("cam")["frame"] is None


@pytest.mark.parametrize("failure", ["no_frame", "frozen_frame", "detector_error"])
def test_no_fresh_analysis_means_no_boxes(tmp_path, failure):
    cams = BufferedCameras([camera()])
    vision = OracleVision(_settings(tmp_path), cams)
    cams.put("cam", scene(30))
    vision._process_enabled_cameras()
    assert vision.latest_result("cam") is not None

    if failure == "no_frame":
        cams.restart("cam")  # camera reconnecting: buffer empty
    elif failure == "frozen_frame":
        cams.buffers["cam"].put(scene(30), datetime.now(timezone.utc) - timedelta(minutes=1))
    else:
        cams.put("cam", scene(40))
        vision._detect = lambda frame: (_ for _ in ()).throw(RuntimeError("model crashed"))
    vision._process_enabled_cameras()

    assert vision.latest_result("cam") is None
    assert vision.overlay_result("cam") is None


def test_a_stalled_vision_loop_stops_feeding_the_overlay(tmp_path):
    cams = BufferedCameras([camera()])
    vision = OracleVision(_settings(tmp_path), cams)
    cams.put("cam", scene(30))
    vision._process_enabled_cameras()
    stale = replace(vision.latest_result("cam"), processed_at=datetime.now(timezone.utc) - timedelta(seconds=30))
    vision._results["cam"] = stale

    assert vision.overlay_result("cam") is None
    headers, _ = _first_part(local_app._mjpeg_frames(_runtime(vision, cams), "cam", overlay=True))
    assert headers["X-CAMPEX-Overlay"] == "unavailable"


def test_existing_consumers_keep_their_fields(tmp_path):
    cams = BufferedCameras([camera()])
    vision = OracleVision(_settings(tmp_path), cams)
    for _ in range(2):  # the second hit confirms the track
        cams.put("cam", scene(30))
        vision._process_enabled_cameras()
    result = vision.latest_result("cam")

    local = vision.objects("cam")[0]
    assert {"track_id", "class_name", "confidence", "bounding_box"} <= set(local)
    assert local["bounding_box"] == {"x1": 30.0, "y1": 20.0, "x2": 50.0, "y2": 60.0}
    assert (local["frame_id"], local["frame_at"]) == (result.frame_id, result.frame_at.isoformat())
    assert vision.cloud_objects("cam") == [
        {"track_id": 1, "class_name": "person", "confidence": 0.9, "bounding_box": [30.0, 20.0, 50.0, 60.0]}
    ]
    assert vision.people_boxes("cam") == [(30.0, 20.0, 50.0, 60.0)]
    assert vision.people_observation("cam") == (result.frame_at, [(30.0, 20.0, 50.0, 60.0)])
    status = vision.status("cam")
    assert status["status"] == "RUNNING" and status["objects"] == 1
    assert status["frame"]["frame_id"] == result.frame_id


def test_camera_manager_without_frame_identity_still_works(tmp_path):
    manager = FakeCameraManager([camera("cam-1")])
    manager.frame = scene(30)
    manager.frame_at = datetime.now(timezone.utc)
    vision = OracleVision(_settings(tmp_path), manager)

    vision._process_enabled_cameras()

    result = vision.latest_result("cam-1")
    assert_synchronized(result)
    assert (result.session_id, result.frame_id) == ("", None)
    assert result.frame_at == manager.frame_at


def test_readers_never_see_a_frame_with_another_frames_boxes(tmp_path):
    cams = BufferedCameras([camera()])
    vision = OracleVision(_settings(tmp_path), cams)
    stop = threading.Event()
    failures: list[str] = []
    reads = [0]

    def reader():
        while not stop.is_set():
            result = vision.latest_result("cam")
            if result is not None and result.detections:
                reads[0] += 1
                if box_of(result.detections[0]) != blob_box(result.frame):
                    failures.append(f"result {result.key}")
            # Frame n shows the person at x = n, so objects() must report a
            # box at x1 == frame_id: boxes and frame id come from one result.
            for item in vision.objects("cam"):
                if item["bounding_box"]["x1"] != item["frame_id"]:
                    failures.append(f"objects of frame {item['frame_id']}")

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
    for thread in readers:
        thread.start()
    try:
        for frame_id in range(1, 200):
            cams.put("cam", scene(frame_id))
            vision._process_enabled_cameras()
    finally:
        stop.set()
        for thread in readers:
            thread.join()

    assert failures == []
    assert reads[0] > 0


def test_camera_restart_starts_a_new_frame_session(tmp_path):
    cams = BufferedCameras([camera()])
    vision = OracleVision(_settings(tmp_path), cams)
    for _ in range(3):
        cams.put("cam", scene(30))
        vision._process_enabled_cameras()
    before = vision.latest_result("cam")

    cams.restart("cam")
    cams.put("cam", scene(30))
    vision._process_enabled_cameras()
    after = vision.latest_result("cam")

    assert (before.frame_id, after.frame_id) == (3, 1)
    assert before.session_id != after.session_id
    assert before.key != after.key


def test_frame_buffer_sessions_never_share_ids():
    first, second = LatestFrameBuffer(), LatestFrameBuffer()
    now = datetime.now(timezone.utc)
    first.put(scene(1), now)
    second.put(scene(1), now)

    a, b = first.snapshot(), second.snapshot()
    assert a.frame_id == b.frame_id == 1
    assert a.session_id != b.session_id


def _live_relay(tmp_path, cams, vision):
    from campex_node.cloud.live_relay import LiveRelayService

    from tests.campex_node.test_live_relay import FakeCloud

    cloud = FakeCloud([{"next_upload_seconds": 1.0, "cameras": {"cam": {"enabled": True, "live": True}}}] * 4)
    relay = LiveRelayService(settings=_settings(tmp_path), cloud_client=cloud, camera_manager=cams, vision=vision)
    relay.relay_once()  # learns that the camera is being watched
    return relay, cloud


def test_relay_uploads_the_analysed_frame_with_the_identity_of_its_objects(tmp_path):
    import base64

    cams = BufferedCameras([camera()])
    vision = OracleVision(_settings(tmp_path), cams, during_inference=lambda: cams.put("cam", scene(150)))
    cams.put("cam", scene(30))
    vision._process_enabled_cameras()
    result = vision.latest_result("cam")
    relay, cloud = _live_relay(tmp_path, cams, vision)

    relay.relay_once()

    report = cloud.payloads[-1]["cameras"][0]
    assert report["vision_frame"]["frame_id"] == result.frame_id
    assert report["frame_ref"]["source"] == "vision"
    assert {key: report["frame_ref"][key] for key in ("session_id", "frame_id", "frame_at")} == {
        key: report["vision_frame"][key] for key in ("session_id", "frame_id", "frame_at")
    }
    assert report["objects"][0]["bounding_box"] == [30.0, 20.0, 50.0, 60.0]
    image = cv2.imdecode(np.frombuffer(base64.b64decode(report["frame_jpeg_base64"]), np.uint8), cv2.IMREAD_COLOR)
    left, _ = _decoded_blob(cv2.imencode(".jpg", image)[1].tobytes())
    assert abs(left - 30) <= 2  # the analysed frame, not the newer camera frame at x=150

    relay.relay_once()  # nothing new was analysed: the same frame is not uploaded again
    assert "frame_jpeg_base64" not in cloud.payloads[-1]["cameras"][0]


def test_relay_without_vision_uploads_the_live_frame_without_object_identity(tmp_path):
    cams = BufferedCameras([replace(camera(), vision_enabled=False)])
    vision = OracleVision(_settings(tmp_path), cams)
    cams.put("cam", scene(30))
    vision._process_enabled_cameras()
    relay, cloud = _live_relay(tmp_path, cams, vision)

    relay.relay_once()

    report = cloud.payloads[-1]["cameras"][0]
    assert report["objects"] == [] and "vision_frame" not in report
    assert report["frame_ref"]["source"] == "camera"
    assert report["frame_ref"]["frame_id"] == 1
