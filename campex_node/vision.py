from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2

from campex_node.cameras.manager import CameraManager
from campex_node.core.config import ROOT_DIR, NodeSettings
from campex_node.events import NodeEventPipeline


logger = logging.getLogger("campex.node.vision")

COCO_KEYPOINTS = (
    "nose", "left_eye", "right_eye", "left_ear", "right_ear",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle",
)
# A model that failed to load (e.g. no internet to fetch the pose weights) is
# retried after this delay instead of on every frame.
MODEL_RETRY_SECONDS = 60.0


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

    def as_cloud_dict(self, track_id: int) -> dict[str, Any]:
        return {
            "track_id": track_id,
            "class_name": self.class_name,
            "confidence": self.confidence,
            "bounding_box": [self.x1, self.y1, self.x2, self.y2],
        }


@dataclass(frozen=True)
class EdgePose:
    confidence: float
    box: tuple[float, float, float, float]
    keypoints: tuple[tuple[str, float, float, float], ...]

    def as_cloud_dict(self, pose_id: int) -> dict[str, Any]:
        return {
            "pose_id": pose_id,
            "confidence": self.confidence,
            "bounding_box": list(self.box),
            "keypoints": [
                {"name": name, "x": x, "y": y, "confidence": confidence}
                for name, x, y, confidence in self.keypoints
            ],
        }


def resolve_model_path(name: str, data_dir: Path) -> str:
    """Finds bundled weights; otherwise points Ultralytics at a writable path.

    Ultralytics downloads its official weights to the given path when the
    file is missing, so the fallback lives in the Node's data directory.
    """
    candidate = Path(name)
    if candidate.is_absolute():
        return str(candidate)
    search: list[Path] = []
    if getattr(sys, "frozen", False):
        search.append(Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)))
        search.append(Path(sys.executable).parent)
    search.extend([ROOT_DIR, data_dir / "models"])
    for base in search:
        if (base / name).is_file():
            return str(base / name)
    target = data_dir / "models" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    return str(target)


class _LazyModel:
    """Loads an Ultralytics model on first use, with a cooldown after failures."""

    def __init__(self, path_factory) -> None:
        self._path_factory = path_factory
        self._model: Any | None = None
        self._failed_at: float | None = None
        self.error: str | None = None

    def get(self):
        if self._model is not None:
            return self._model
        if self._failed_at is not None and time.monotonic() - self._failed_at < MODEL_RETRY_SECONDS:
            return None
        try:
            from ultralytics import YOLO

            self._model = YOLO(self._path_factory())
            self.error = None
            self._failed_at = None
        except Exception as exc:
            self._failed_at = time.monotonic()
            self.error = str(exc)
            logger.warning("Vision model unavailable: %s", exc)
        return self._model


class EdgeVisionService:
    """Offline vision loop for CAMPEX Node.

    Person detection uses the bundled Ultralytics YOLO model and falls back to
    OpenCV HOG when YOLO cannot load. Body mapping uses YOLO Pose.
    """

    def __init__(
        self,
        settings: NodeSettings,
        camera_manager: CameraManager,
        events: NodeEventPipeline | None = None,
    ) -> None:
        self.settings = settings
        self.camera_manager = camera_manager
        self.events = events
        self._hog = cv2.HOGDescriptor()
        self._hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        self._detector = _LazyModel(lambda: resolve_model_path(settings.vision_model, settings.data_dir))
        self._pose_model = _LazyModel(lambda: resolve_model_path(settings.pose_model, settings.data_dir))
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run,
            name="campex-node-edge-vision",
            daemon=True,
        )
        self._state: dict[str, dict[str, Any]] = {}
        # camera_id -> (sampled_at, frames_received, fps); status() is polled by
        # several callers, so the FPS is only resampled once per window.
        self._capture_fps: dict[str, tuple[float, int, float | None]] = {}

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
                "detector": state.get("detector") or "opencv-hog",
                "objects": 0,
                "error": "Camera not found.",
            }
        frames_received, camera_fps = self._capture_metrics(camera_id)
        mapping_status = state.get("mapping_status") or "STOPPED"
        poses = len(state.get("poses") or [])
        return {
            "camera_id": camera_id,
            "status": state.get("status") or ("STARTING" if camera.vision_enabled else "STOPPED"),
            "enabled": camera.vision_enabled,
            "detector": state.get("detector") or "opencv-hog",
            "device": "CPU",
            "objects": len(state.get("detections") or []),
            "last_processed_at": state.get("last_processed_at"),
            "inference_ms": state.get("inference_ms"),
            "frames_processed": state.get("frames_processed", 0),
            "vision_fps": state.get("vision_fps", 0),
            "mapping_status": mapping_status,
            "mapping_error": state.get("mapping_error"),
            "poses": poses,
            "error": state.get("error"),
            # Same shape the cloud API serves, so the panel reads one format.
            "metrics": {
                "camera_fps": camera_fps or 0,
                "vision_fps": state.get("vision_fps", 0),
                "inference_ms": state.get("inference_ms"),
                "frames_processed": state.get("frames_processed", 0),
                "frames_received": frames_received,
                "objects_detected": len(state.get("detections") or []),
            },
            "components": {
                "mapping": {
                    "state": mapping_status,
                    "poses": poses,
                    "error": state.get("mapping_error"),
                }
            },
        }

    def _capture_metrics(self, camera_id: str) -> tuple[int, float | None]:
        runtime = next((item for item in self.camera_manager.states() if item.id == camera_id), None)
        frames_received = runtime.frames_received if runtime else 0
        now = time.monotonic()
        with self._lock:
            previous = self._capture_fps.get(camera_id)
            if previous is None or frames_received < previous[1]:
                self._capture_fps[camera_id] = (now, frames_received, None)
                return frames_received, None
            sampled_at, sampled_frames, fps = previous
            if now - sampled_at >= 1.0:
                fps = round((frames_received - sampled_frames) / (now - sampled_at), 1)
                self._capture_fps[camera_id] = (now, frames_received, fps)
        return frames_received, fps

    def objects(self, camera_id: str) -> list[dict[str, Any]]:
        with self._lock:
            detections = list((self._state.get(camera_id) or {}).get("detections") or [])
        return [item.as_dict() for item in detections]

    def cloud_objects(self, camera_id: str) -> list[dict[str, Any]]:
        with self._lock:
            detections = list((self._state.get(camera_id) or {}).get("detections") or [])
        return [item.as_cloud_dict(index + 1) for index, item in enumerate(detections)]

    def cloud_poses(self, camera_id: str) -> list[dict[str, Any]]:
        with self._lock:
            poses = list((self._state.get(camera_id) or {}).get("poses") or [])
        return [item.as_cloud_dict(index + 1) for index, item in enumerate(poses)]

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
            if not camera.enabled or not (camera.vision_enabled or camera.mapping_enabled):
                self._set_state(camera.id, status="STOPPED", detections=[], poses=[], mapping_status="STOPPED", mapping_error=None)
                self._events_unavailable(camera.id, "vision_stopped")
                continue
            frame, frame_at = self.camera_manager.latest_frame(camera.id)
            if frame is None:
                self._set_state(camera.id, status="WAITING_FRAME", detections=[], poses=[])
                self._events_unavailable(camera.id, "camera_observation_unavailable")
                continue
            updates: dict[str, Any] = {"last_frame_at": frame_at.isoformat() if frame_at else None}
            started = time.perf_counter()
            if camera.vision_enabled:
                try:
                    detections, detector = self._detect_people(frame)
                    updates.update(status="RUNNING", detections=detections, detector=detector, error=None)
                except Exception as exc:
                    updates.update(status="ERROR", detections=[], error=str(exc))
                    self._events_unavailable(camera.id, "detector_error")
                else:
                    self._process_events(camera.id, frame, detections, frame_at)
            else:
                updates.update(status="STOPPED", detections=[])
                self._events_unavailable(camera.id, "vision_stopped")
            if camera.mapping_enabled:
                updates.update(self._estimate_poses(frame))
            else:
                updates.update(poses=[], mapping_status="STOPPED", mapping_error=None)
            updates["inference_ms"] = round((time.perf_counter() - started) * 1000, 1)
            self._set_state(camera.id, **updates)

    def _process_events(self, camera_id: str, frame, detections: list[EdgeDetection], frame_at) -> None:
        if self.events is None:
            return
        try:
            self.events.process(camera_id, frame, detections, now=time.monotonic(), observed_at=frame_at)
        except Exception:
            logger.exception("Event pipeline failed for camera %s", camera_id)

    def _events_unavailable(self, camera_id: str, reason: str) -> None:
        if self.events is None:
            return
        try:
            self.events.camera_unavailable(camera_id, reason)
        except Exception:
            logger.exception("Event pipeline failed to release camera %s", camera_id)

    def _detect_people(self, frame) -> tuple[list[EdgeDetection], str]:
        model = self._detector.get()
        if model is None:
            return self._detect_people_hog(frame), "opencv-hog"
        results = model.predict(
            frame,
            conf=self.settings.vision_confidence,
            classes=[0],
            imgsz=self.settings.vision_input_size,
            verbose=False,
        )
        detections: list[EdgeDetection] = []
        for result in results:
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            for xyxy, confidence in zip(boxes.xyxy.tolist(), boxes.conf.tolist()):
                x1, y1, x2, y2 = (float(value) for value in xyxy[:4])
                detections.append(EdgeDetection("person", round(float(confidence), 4), x1, y1, x2, y2))
        return detections, "yolo"

    def _estimate_poses(self, frame) -> dict[str, Any]:
        model = self._pose_model.get()
        if model is None:
            return {
                "poses": [],
                "mapping_status": "ERROR",
                "mapping_error": f"Modelo de mapeamento indisponível: {self._pose_model.error or 'carregando'}",
            }
        try:
            results = model.predict(
                frame,
                conf=self.settings.vision_confidence,
                imgsz=self.settings.vision_input_size,
                verbose=False,
            )
        except Exception as exc:
            return {"poses": [], "mapping_status": "ERROR", "mapping_error": str(exc)}
        poses: list[EdgePose] = []
        for result in results:
            keypoints = getattr(result, "keypoints", None)
            boxes = getattr(result, "boxes", None)
            if keypoints is None or boxes is None:
                continue
            points_xy = keypoints.xy.tolist()
            points_conf = keypoints.conf.tolist() if keypoints.conf is not None else None
            for index, (xyxy, confidence) in enumerate(zip(boxes.xyxy.tolist(), boxes.conf.tolist())):
                named = []
                for point_index, (x, y) in enumerate(points_xy[index] if index < len(points_xy) else []):
                    if point_index >= len(COCO_KEYPOINTS):
                        break
                    point_conf = points_conf[index][point_index] if points_conf else 1.0
                    named.append((COCO_KEYPOINTS[point_index], float(x), float(y), round(float(point_conf), 4)))
                poses.append(
                    EdgePose(
                        confidence=round(float(confidence), 4),
                        box=tuple(float(value) for value in xyxy[:4]),
                        keypoints=tuple(named),
                    )
                )
        return {"poses": poses, "mapping_status": "ACTIVE", "mapping_error": None}

    def _detect_people_hog(self, frame) -> list[EdgeDetection]:
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
        now = datetime.now(timezone.utc)
        with self._lock:
            previous = dict(self._state.get(camera_id) or {})
            frames_processed = int(previous.get("frames_processed") or 0)
            running = updates.get("status") == "RUNNING" or updates.get("mapping_status") == "ACTIVE"
            if running:
                frames_processed += 1
                last = previous.get("_last_processed_monotonic")
                current = time.monotonic()
                if last is not None and current > last:
                    instant_fps = 1.0 / (current - last)
                    previous_fps = float(previous.get("vision_fps") or instant_fps)
                    previous["vision_fps"] = round(previous_fps * 0.7 + instant_fps * 0.3, 2)
                previous["_last_processed_monotonic"] = current
            else:
                previous["vision_fps"] = 0
                previous["_last_processed_monotonic"] = None
            previous.update(updates)
            previous["last_processed_at"] = now.isoformat() if running else previous.get("last_processed_at")
            previous["frames_processed"] = frames_processed
            self._state[camera_id] = previous

    def _camera(self, camera_id: str):
        return next((camera for camera in self.camera_manager.configs() if camera.id == camera_id), None)
