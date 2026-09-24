from __future__ import annotations

import time

from backend.cameras.security import sanitize_error_message
from backend.cameras.base import CameraConfig, CameraSource
from backend.cameras.factory import create_camera_source

from campex_node.core.config import NodeCameraConfig


def create_rtsp_source(camera: NodeCameraConfig) -> CameraSource:
    return create_camera_source(
        CameraConfig(
            id=camera.id,
            source_type="rtsp",
            source_uri=camera.rtsp_url,
        )
    )


def test_rtsp_connection(rtsp_url: str) -> dict:
    camera = NodeCameraConfig(
        id="test_connection",
        name="Teste RTSP",
        rtsp_url=rtsp_url,
        enabled=False,
    )
    source = create_rtsp_source(camera)
    started_at = time.monotonic()
    try:
        connected = source.connect()
        health = source.health()
        return {
            "ok": bool(connected),
            "status": health.status.value,
            "latency_ms": int((time.monotonic() - started_at) * 1000),
            "resolution": {
                "width": health.resolution.width if health.resolution else None,
                "height": health.resolution.height if health.resolution else None,
            },
            "frames_received": health.frames_received,
            "error": sanitize_error_message(health.last_error),
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "OFFLINE",
            "latency_ms": int((time.monotonic() - started_at) * 1000),
            "resolution": {"width": None, "height": None},
            "frames_received": 0,
            "error": sanitize_error_message(str(exc)),
        }
    finally:
        source.close()
