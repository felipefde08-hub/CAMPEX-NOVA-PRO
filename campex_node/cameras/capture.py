from __future__ import annotations

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
