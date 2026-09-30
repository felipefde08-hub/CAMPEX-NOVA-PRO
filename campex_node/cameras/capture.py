from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

from backend.cameras.security import sanitize_error_message
from backend.cameras.base import CameraConfig, CameraSource
from backend.cameras.opencv_source import RTSPSource

from campex_node.core.config import NodeCameraConfig, NodeSettings


def create_rtsp_source(camera: NodeCameraConfig, settings: NodeSettings | None = None) -> CameraSource:
    timeouts = {}
    if settings is not None:
        timeouts = {
            "open_timeout_ms": settings.camera_open_timeout_ms,
            "read_timeout_ms": settings.camera_read_timeout_ms,
        }
    return RTSPSource(
        CameraConfig(
            id=camera.id,
            source_type="rtsp",
            source_uri=camera.rtsp_url,
        ),
        **timeouts,
    )


def test_rtsp_connection(
    rtsp_url: str,
    *,
    settings: NodeSettings | None = None,
    timeout_seconds: float = 12.0,
) -> dict:
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="campex-node-camera-test")
    future = executor.submit(_test_rtsp_connection_sync, rtsp_url, settings)
    try:
        return future.result(timeout=timeout_seconds)
    except FutureTimeoutError:
        future.cancel()
        return {
            "ok": False,
            "success": False,
            "status": "OFFLINE",
            "latency_ms": int(timeout_seconds * 1000),
            "resolution": {"width": None, "height": None},
            "frames_received": 0,
            "error": (
                "Timeout ao abrir o RTSP no CAMPEX Node local. Verifique IP, porta, "
                "usuario/senha, canal/subtipo e se este computador acessa a rede da camera."
            ),
        }
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _test_rtsp_connection_sync(rtsp_url: str, settings: NodeSettings | None = None) -> dict:
    camera = NodeCameraConfig(
        id="test_connection",
        name="Teste RTSP",
        rtsp_url=rtsp_url,
        enabled=False,
    )
    source = create_rtsp_source(camera, settings)
    started_at = time.monotonic()
    try:
        connected = source.connect()
        health = source.health()
        return {
            "ok": bool(connected),
            "success": bool(connected),
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
            "success": False,
            "status": "OFFLINE",
            "latency_ms": int((time.monotonic() - started_at) * 1000),
            "resolution": {"width": None, "height": None},
            "frames_received": 0,
            "error": sanitize_error_message(str(exc)),
        }
    finally:
        source.close()
