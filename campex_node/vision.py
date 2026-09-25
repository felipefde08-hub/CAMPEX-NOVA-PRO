from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import cv2

from campex_node.cameras.manager import CameraManager
from campex_node.core.config import NodeSettings


logger = logging.getLogger("campex.node.vision")


@dataclass(frozen=True)
class EdgeDetection:
    class_name: str
    confidence: float
    x1: float
    y1: float
    x2: float
    y2: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "class_name": self.class_name,
            "confidence": self.confidence,
            "bounding_box": {
                "x1": self.x1,
                "y1": self.y1,
                "x2": self.x2,
                "y2": self.y2,
            },
        }


class EdgeVisionService:
    """Lightweight offline vision loop for CAMPEX Node.

    This first local detector intentionally uses OpenCV HOG so the Windows
    package can run without model downloads, API calls, CUDA, Torch or YOLO.
    """

    def __init__(self, settings: NodeSettings, camera_manager: CameraManager) -> None:
        self.settings = settings
        self.camera_manager = camera_manager
        self._hog = cv2.HOGDescriptor()
        self._hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run,
            name="campex-node-edge-vision",
            daemon=True,
        )
        self._state: dict[str, dict[str, Any]] = {}

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3)

    def status(self, camera_id: str) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state.get(camera_id) or {})
        camera = self._camera(camera_id)
        if camera is None:
            return {
                "camera_id": camera_id,
                "status": "NOT_FOUND",
                "enabled": False,
                "detector": "opencv-hog",
                "objects": 0,
                "error": "Camera not found.",
            }
        return {
            "camera_id": camera_id,
            "status": state.get("status") or ("STARTING" if camera.vision_enabled else "STOPPED"),
            "enabled": camera.vision_enabled,
            "detector": "opencv-hog",
            "device": "CPU",
            "objects": len(state.get("detections") or []),
            "last_processed_at": state.get("last_processed_at"),
            "inference_ms": state.get("inference_ms"),
            "frames_processed": state.get("frames_processed", 0),
            "error": state.get("error"),
        }

    def objects(self, camera_id: str) -> list[dict[str, Any]]:
        with self._lock:
            detections = list((self._state.get(camera_id) or {}).get("detections") or [])
        return [item.as_dict() for item in detections]

    def render_overlay(self, camera_id: str, frame):
        with self._lock:
            detections = list((self._state.get(camera_id) or {}).get("detections") or [])
        for detection in detections:
            x1, y1, x2, y2 = map(int, (detection.x1, detection.y1, detection.x2, detection.y2))
            cv2.rectangle(frame, (x1, y1), (x2, y2), (125, 179, 255), 2)
            label = f"{detection.class_name} {detection.confidence:.2f}"
            cv2.putText(
                frame,
                label,
                (x1, max(18, y1 - 7)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (235, 245, 255),
                2,
                cv2.LINE_AA,
            )
        return frame

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._process_enabled_cameras()
            except Exception:
                logger.exception("Edge vision loop failed")
            self._stop.wait(self.settings.vision_interval_seconds)

    def _process_enabled_cameras(self) -> None:
        for camera in self.camera_manager.configs():
            if not camera.enabled or not camera.vision_enabled:
                self._set_state(camera.id, status="STOPPED", detections=[])
                continue
            frame, frame_at = self.camera_manager.latest_frame(camera.id)
            if frame is None:
                self._set_state(camera.id, status="WAITING_FRAME", detections=[])
                continue
            started = time.perf_counter()
            try:
                detections = self._detect_people(frame)
                inference_ms = (time.perf_counter() - started) * 1000
                self._set_state(
                    camera.id,
                    status="RUNNING",
                    detections=detections,
                    error=None,
                    inference_ms=inference_ms,
                    last_frame_at=frame_at.isoformat() if frame_at else None,
                )
            except Exception as exc:
                self._set_state(camera.id, status="ERROR", detections=[], error=str(exc))

    def _detect_people(self, frame) -> list[EdgeDetection]:
        boxes, weights = self._hog.detectMultiScale(
            frame,
            winStride=(8, 8),
            padding=(8, 8),
            scale=1.05,
        )
        detections: list[EdgeDetection] = []
        for index, (x, y, width, height) in enumerate(boxes):
            raw_score = float(weights[index]) if index < len(weights) else 1.0
            confidence = max(0.0, min(1.0, raw_score if raw_score <= 1.0 else raw_score / 3.0))
            if confidence < self.settings.vision_confidence:
                continue
            detections.append(
                EdgeDetection(
                    class_name="person",
                    confidence=round(confidence, 6),
                    x1=float(x),
                    y1=float(y),
                    x2=float(x + width),
                    y2=float(y + height),
                )
            )
        return detections

    def _set_state(self, camera_id: str, **updates: Any) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            previous = dict(self._state.get(camera_id) or {})
            frames_processed = int(previous.get("frames_processed") or 0)
            if updates.get("status") == "RUNNING":
                frames_processed += 1
            previous.update(updates)
            previous["last_processed_at"] = now if updates.get("status") == "RUNNING" else previous.get("last_processed_at")
            previous["frames_processed"] = frames_processed
            self._state[camera_id] = previous

    def _camera(self, camera_id: str):
        return next((camera for camera in self.camera_manager.configs() if camera.id == camera_id), None)
