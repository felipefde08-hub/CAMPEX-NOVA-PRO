from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import av
import numpy as np

from campex_node.core.config import NodeCameraConfig, NodeSettings
from campex_node.recording import CameraRecorder, RecordingService, RecordingStore


STARTED = datetime(2026, 10, 7, 11, 0, tzinfo=timezone.utc)


def _source_video(path: Path, seconds: int, fps: int = 10) -> Path:
    container = av.open(str(path), mode="w")
    stream = container.add_stream("libx264", rate=fps)
    stream.width, stream.height, stream.pix_fmt = 160, 120, "yuv420p"
    # A keyframe every second so segments can be cut on time.
    stream.codec_context.gop_size = fps
    for index in range(seconds * fps):
        image = np.full((120, 160, 3), (index * 5) % 255, dtype=np.uint8)
        for packet in stream.encode(av.VideoFrame.from_ndarray(image, format="rgb24")):
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    return path


def _settings(tmp_path: Path, **overrides) -> NodeSettings:
    base = NodeSettings(
        environment="test",
        version="test",
        log_level="INFO",
        data_dir=tmp_path,
        database_path=tmp_path / "node.sqlite3",
        node_id_file=tmp_path / "node_id",
        recording_segment_seconds=10.0,
    )
    return replace(base, **overrides)


def _store(settings: NodeSettings) -> RecordingStore:
    store = RecordingStore(settings.database_path)
    store.initialize()
    return store


def _record(tmp_path: Path, seconds: int = 25):
    settings = _settings(tmp_path)
    store = _store(settings)
    source = _source_video(tmp_path / "source.mp4", seconds)
    camera = NodeCameraConfig(id="cam-1", name="Prensa", rtsp_url=str(source))
    CameraRecorder(camera, settings, store, clock=lambda: STARTED).record_session()
    return settings, store


def test_stream_is_split_into_playable_segments_on_keyframes(tmp_path):
    _settings_, store = _record(tmp_path)

    segments = store.list("cam-1", STARTED - timedelta(hours=1), STARTED + timedelta(hours=1))
    assert [round(item.duration) for item in segments] == [10, 10, 5]
    assert all(item.status == "COMPLETE" and item.codec == "h264" for item in segments)
    # Each segment starts where the previous one ended.
    assert segments[1].started_at == segments[0].ended_at
    for segment in segments:
        assert segment.path.suffix == ".mp4" and segment.size_bytes > 0
        with av.open(str(segment.path)) as playback:
            frames = sum(1 for _ in playback.decode(video=0))
        assert frames == round(segment.duration * 10)


def test_lookup_returns_the_segment_and_offset_for_a_moment(tmp_path):
    _settings_, store = _record(tmp_path)

    segment, offset = store.at("cam-1", STARTED + timedelta(seconds=13))
    assert segment.started_at == STARTED + timedelta(seconds=10)
    assert round(offset, 1) == 3.0
    assert store.at("cam-1", STARTED + timedelta(minutes=5)) is None


def test_retention_deletes_oldest_segments_past_the_age_limit(tmp_path):
    settings, store = _record(tmp_path)
    service = RecordingService(replace(settings, recording_retention_days=1.0), camera_manager=None, store=store)
    old = store.list("cam-1", STARTED - timedelta(hours=1), STARTED + timedelta(hours=1))
    # Age the first segment past the limit.
    first = old[0]
    store.delete(first)
    store.add(replace(first, started_at=STARTED - timedelta(days=3), ended_at=STARTED - timedelta(days=3) + timedelta(seconds=10)))
    first.path.write_bytes(b"x")

    assert service.enforce_retention() == 1
    remaining = store.list("cam-1", STARTED - timedelta(days=5), STARTED + timedelta(hours=1))
    assert [item.id for item in remaining] == [item.id for item in old[1:]]
    assert not first.path.exists()


def test_retention_frees_space_when_the_disk_is_low(tmp_path):
    settings, store = _record(tmp_path)
    service = RecordingService(
        replace(settings, recording_min_free_gb=1_000_000.0), camera_manager=None, store=store
    )
    assert service.enforce_retention() == 3
    assert store.total_bytes() == 0


def test_recorder_does_not_write_when_the_disk_is_low(tmp_path, monkeypatch):
    settings = _settings(tmp_path, recording_min_free_gb=1_000_000.0, camera_reconnect_seconds=0.0)
    store = _store(settings)
    camera = NodeCameraConfig(id="cam-1", name="Prensa", rtsp_url=str(tmp_path / "missing.mp4"))
    recorder = CameraRecorder(camera, settings, store)
    sessions = []
    monkeypatch.setattr(recorder, "record_session", lambda: sessions.append(1))
    recorder.start()
    import time

    time.sleep(0.3)
    recorder.stop()
    assert recorder.status == "DISK_FULL"
    assert sessions == []
