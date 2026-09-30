import cv2
import numpy as np

from backend.cameras.base import CameraConfig
from backend.cameras.factory import create_camera_source
from backend.cameras.health import CameraStatus
from backend.cameras.opencv_source import RTSPSource, VideoFileSource, WebcamSource
from backend.cameras.security import sanitize_source_uri


def create_fixture_video(path):
    writer = cv2.VideoWriter(
        str(path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        5,
        (64, 48),
    )
    for index in range(3):
        frame = np.full((48, 64, 3), index * 40, dtype=np.uint8)
        writer.write(frame)
    writer.release()


def test_source_factory_selects_implementation():
    assert isinstance(
        create_camera_source(CameraConfig("cam_1", "webcam", "0")), WebcamSource
    )
    assert isinstance(
        create_camera_source(CameraConfig("cam_1", "video_file", "test.mp4")),
        VideoFileSource,
    )
    assert isinstance(
        create_camera_source(CameraConfig("cam_1", "rtsp", "rtsp://host/stream")),
        RTSPSource,
    )


def test_video_file_source_reads_valid_frame(tmp_path):
    video_path = tmp_path / "fixture.mp4"
    create_fixture_video(video_path)
    source = VideoFileSource(CameraConfig("cam_file", "video_file", str(video_path)))

    try:
        assert source.connect() is True
        result = source.read()
        health_snapshot = source.health().as_dict()
    finally:
        source.close()

    assert result.success is True
    assert health_snapshot["status"] == CameraStatus.ONLINE.value
    assert health_snapshot["resolution"] is not None
    assert health_snapshot["resolution"]["width"] == 64
    assert health_snapshot["frames_received"] >= 1


def test_invalid_video_file_reports_offline(tmp_path):
    source = VideoFileSource(
        CameraConfig("cam_missing", "video_file", str(tmp_path / "missing.mp4"))
    )

    try:
        assert source.connect() is False
        health = source.health()
    finally:
        source.close()

    assert health.status == CameraStatus.OFFLINE
    assert health.last_error


def test_rtsp_uri_sanitization_redacts_credentials():
    assert (
        sanitize_source_uri("rtsp://example-user:example-pass@192.168.1.50:554/stream")
        == "rtsp://***:***@192.168.1.50:554/stream"
    )
    assert sanitize_source_uri("0") == "0"


def test_rtsp_source_passes_timeouts_when_opening(monkeypatch):
    opened = []

    class ClosedCapture:
        def isOpened(self):
            return False

        def set(self, *args):
            return True

        def release(self):
            pass

    def fake_capture(*args):
        opened.append(args)
        return ClosedCapture()

    monkeypatch.setattr(cv2, "VideoCapture", fake_capture)
    monkeypatch.setattr("backend.cameras.opencv_source._can_open_tcp", lambda host, port: True)
    source = RTSPSource(
        CameraConfig("cam_rtsp", "rtsp", "rtsp://host/stream"),
        open_timeout_ms=1234,
        read_timeout_ms=4321,
    )

    assert source.connect() is False
    target, backend, params = opened[0]
    assert (target, backend) == ("rtsp://host/stream", cv2.CAP_FFMPEG)
    assert params == [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 1234, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 4321]


def test_connect_validation_frame_is_returned_by_next_read(tmp_path):
    video_path = tmp_path / "fixture.mp4"
    create_fixture_video(video_path)
    source = VideoFileSource(CameraConfig("cam_file", "video_file", str(video_path)))

    try:
        assert source.connect() is True
        first = source.read()
        second = source.read()
    finally:
        source.close()

    assert first.success is True
    assert int(first.frame.mean()) == 0
    assert second.success is True
    assert int(second.frame.mean()) > 0
