from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import cv2
import psutil

from campex_node.cameras.manager import CameraManager
from campex_node.core.config import ROOT_DIR, NodeSettings
from campex_node.events import NodeEventPipeline
from campex_node.tracking import EdgeTracker


logger = logging.getLogger("campex.node.vision")

COCO_KEYPOINTS = (
    "nose", "left_eye", "right_eye", "left_ear", "right_ear",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle",
)
# COCO classes the detector keeps: people plus the vehicles that matter at
# gates and docks.
DETECTED_CLASSES = {0: "person", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
VEHICLE_CLASSES = frozenset({"car", "motorcycle", "bus", "truck"})
# A model that failed to load (e.g. no internet to fetch the pose weights) is
# retried after this delay instead of on every frame.
MODEL_RETRY_SECONDS = 60.0
# An analysed frame older than this is not shown as the synchronized overlay:
# the vision loop stalled, so the image would look live while it is not.
OVERLAY_RESULT_MAX_AGE_SECONDS = 5.0
# With no camera ready the scheduler waits this long for a new frame instead
# of spinning (Windows rounds short waits up to its ~15 ms timer tick).
IDLE_WAIT_SECONDS = 0.01
RESOURCE_SAMPLE_SECONDS = 2.0
# How long stop() waits for queued event work to finish before giving up.
POST_QUEUE_STOP_SECONDS = 10.0
_STOP_TASK = object()


@dataclass(frozen=True)
class EdgeDetection:
    class_name: str
    confidence: float
    x1: float
    y1: float
    x2: float
    y2: float
    track_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "class_name": self.class_name,
            "confidence": self.confidence,
            "bounding_box": {
                "x1": self.x1,
                "y1": self.y1,
                "x2": self.x2,
                "y2": self.y2,
            },
        }

    def as_cloud_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
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


@dataclass(frozen=True)
class VisionResult:
    """One analysed frame and everything found on it, published as a unit.

    Boxes are in ``frame`` pixels and belong to ``frame`` only: draw them on
    ``frame.copy()``, never on a newer camera frame. ``frame`` is read-only.
    ``frame_at`` is when the Node received the frame, not the camera's own
    capture time (RTSP/FFmpeg buffering in between is not measured).
    """

    camera_id: str
    session_id: str
    frame_id: int | None
    frame_at: datetime | None
    processed_at: datetime
    frame_width: int
    frame_height: int
    detections: tuple[EdgeDetection, ...]
    poses: tuple[EdgePose, ...]
    frame: Any = field(repr=False, compare=False)

    @property
    def key(self) -> tuple[str, int | None, str | None]:
        return (self.session_id, self.frame_id, self.frame_at.isoformat() if self.frame_at else None)

    def frame_ref(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "frame_id": self.frame_id,
            "frame_at": self.frame_at.isoformat() if self.frame_at else None,
            "processed_at": self.processed_at.isoformat(),
            "width": self.frame_width,
            "height": self.frame_height,
        }


class FrameObserver(Protocol):
    """Consumes each analysed frame (occupancy, counting lines...)."""

    def observe(self, camera_id: str, frame: Any, detections: list[EdgeDetection], observed_at: datetime) -> None: ...

    def camera_unavailable(self, camera_id: str, reason: str) -> None: ...


def _age_ms(at: datetime | None) -> float | None:
    return round((datetime.now(timezone.utc) - at).total_seconds() * 1000, 1) if at else None


@dataclass
class _CameraSchedule:
    # Detector seconds this camera has used: the next analysis goes to the
    # ready camera that used least, so an expensive camera cannot starve others.
    virtual_time: float
    next_due: float = 0.0
    last_frame: tuple[str, int] | None = None
    frames_skipped: int = 0


class _PostProcessor:
    """Runs the event rules and frame observers off the inference thread.

    One worker and one FIFO queue, so per camera the analysed frames and the
    "camera unavailable" notices keep their order. The queue is bounded: when
    it is full the inference thread waits (back-pressure) instead of dropping
    work, so no event is lost. Before start() and after stop() tasks run
    inline on the caller's thread.
    """

    def __init__(self, maxsize: int) -> None:
        self._queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._accepting = False
        self.max_depth = 0
        self.full_waits = 0
        self.failed = 0

    def start(self) -> None:
        with self._lock:
            if self._accepting:
                return
            self._accepting = True
            self._thread = threading.Thread(target=self._work, name="campex-node-vision-post", daemon=True)
            self._thread.start()

    def submit(self, task) -> float:
        """Queues the task; returns the seconds spent waiting for room."""
        with self._lock:
            if not self._accepting:
                self._execute(task)
                return 0.0
            try:
                self._queue.put_nowait(task)
                waited = 0.0
            except queue.Full:
                self.full_waits += 1
                started = time.perf_counter()
                self._queue.put(task)  # back-pressure: wait for the worker
                waited = time.perf_counter() - started
            self.max_depth = max(self.max_depth, self._queue.qsize())
            return waited

    def depth(self) -> int:
        return self._queue.qsize()

    def stop(self, timeout: float) -> bool:
        """Finishes the queued work, then stops. False if it did not finish in time."""
        with self._lock:
            if not self._accepting:
                return True
            self._accepting = False
            self._queue.put(_STOP_TASK)
            thread = self._thread
        thread.join(timeout)
        if thread.is_alive():
            logger.warning("Vision post-processing did not finish: %d tasks pending", self.depth())
            return False
        return True

    def _work(self) -> None:
        while True:
            task = self._queue.get()
            if task is _STOP_TASK:
                return
            self._execute(task)

    def _execute(self, task) -> None:
        try:
            task()
        except Exception:
            self.failed += 1
            logger.exception("Vision post-processing task failed")


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

    People and vehicles are detected with the bundled Ultralytics YOLO model,
    falling back to OpenCV HOG (people only) when YOLO cannot load. Body
    mapping uses YOLO Pose.
    """

    def __init__(
        self,
        settings: NodeSettings,
        camera_manager: CameraManager,
        events: NodeEventPipeline | None = None,
        observers: list[FrameObserver] | None = None,
    ) -> None:
        self.settings = settings
        self.camera_manager = camera_manager
        self.events = events
        self.observers = list(observers or [])
        self._trackers: dict[str, EdgeTracker] = {}
        # camera_id -> identity of the last analysed frame: (session_id,
        # frame_id) when the camera manager provides it, else its frame_at
        # (two frames read within one clock tick can share a frame_at).
        self._last_frame_at: dict[str, Any] = {}
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
        # camera_id -> last analysed frame with its detections. Replaced as a
        # whole under the lock, so readers never mix two analyses.
        self._results: dict[str, VisionResult] = {}
        # camera_id -> (sampled_at, frames_received, fps); status() is polled by
        # several callers, so the FPS is only resampled once per window.
        self._capture_fps: dict[str, tuple[float, int, float | None]] = {}
        self._schedules: dict[str, _CameraSchedule] = {}
        self._post = _PostProcessor(settings.vision_post_queue_size)
        self._process = psutil.Process()
        self._resources: dict[str, float | None] = {"cpu_usage_percent": None, "memory_usage_mb": None}
        self._resources_sampled_at = 0.0

    def start(self) -> None:
        if not self._thread.is_alive():
            self._post.start()
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3)
        # Events already queued are still written before the Node exits.
        self._post.stop(POST_QUEUE_STOP_SECONDS)

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
        result = self.latest_result(camera_id)
        return {
            # Identity of the frame objects()/cloud_objects() were detected on.
            "frame": result.frame_ref() if result else None,
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
            # Times start when the Node received the frame (frame_at), not
            # when the camera captured it: RTSP/FFmpeg buffering is not known.
            "metrics": {
                "camera_fps": camera_fps or 0,
                "vision_fps": state.get("vision_fps", 0),
                "inference_ms": state.get("inference_ms"),
                "frames_processed": state.get("frames_processed", 0),
                "frames_received": frames_received,
                "objects_detected": len(state.get("detections") or []),
                "capture_fps": camera_fps or 0,
                "inference_fps": state.get("vision_fps", 0),
                # Detector (+ pose) time of the last analysis.
                "inference_latency_ms": state.get("inference_latency_ms"),
                # How old the frame already was when its inference started.
                "frame_age_ms": state.get("frame_age_ms"),
                # Frame received -> result published, for the last analysis.
                "known_latency_ms": state.get("known_latency_ms"),
                # How old the overlay image would be if shown now.
                "overlay_age_ms": _age_ms(result.frame_at) if result else None,
                # Camera frames replaced before the detector reached them.
                "frames_skipped": state.get("frames_skipped", 0),
                "queue_depth": self._post.depth(),
                "queue_full_waits": self._post.full_waits,
                # Whole Node process, not this camera alone.
                "cpu_usage_percent": self._resources["cpu_usage_percent"],
                "memory_usage_mb": self._resources["memory_usage_mb"],
                "time_basis": "node_receipt",
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

    def latest_result(self, camera_id: str) -> VisionResult | None:
        with self._lock:
            return self._results.get(camera_id)

    def objects(self, camera_id: str) -> list[dict[str, Any]]:
        result = self.latest_result(camera_id)
        if result is None:
            return []
        ref = {"frame_id": result.frame_id, "frame_at": result.frame_ref()["frame_at"]}
        return [{**item.as_dict(), **ref} for item in result.detections]

    def cloud_objects(self, camera_id: str) -> list[dict[str, Any]]:
        result = self.latest_result(camera_id)
        return [item.as_cloud_dict() for item in result.detections] if result else []

    def people_boxes(self, camera_id: str) -> list[tuple[float, float, float, float]]:
        # The boxes come from the last analysed frame, which can be some
        # hundred ms older than the frame the caller samples: see
        # people_observation() for the timestamp a caller needs to account for it.
        return self.people_observation(camera_id)[1]

    def people_observation(
        self, camera_id: str
    ) -> tuple[datetime | None, list[tuple[float, float, float, float]]]:
        """People boxes with the frame_at of the frame they were detected on."""
        result = self.latest_result(camera_id)
        if result is None:
            return None, []
        boxes = [(item.x1, item.y1, item.x2, item.y2) for item in result.detections if item.class_name == "person"]
        return result.frame_at, boxes

    def cloud_poses(self, camera_id: str) -> list[dict[str, Any]]:
        result = self.latest_result(camera_id)
        return [item.as_cloud_dict(index + 1) for index, item in enumerate(result.poses)] if result else []

    def overlay_result(self, camera_id: str) -> VisionResult | None:
        """The result to show as the synchronized overlay, if it is still recent."""
        result = self.latest_result(camera_id)
        if result is None:
            return None
        age = (datetime.now(timezone.utc) - result.processed_at).total_seconds()
        return result if age <= OVERLAY_RESULT_MAX_AGE_SECONDS else None

    @staticmethod
    def render_result(result: VisionResult):
        """The analysed frame with its own boxes drawn on a copy."""
        frame = result.frame.copy()
        for detection in result.detections:
            x1, y1, x2, y2 = map(int, (detection.x1, detection.y1, detection.x2, detection.y2))
            cv2.rectangle(frame, (x1, y1), (x2, y2), (125, 179, 255), 2)
            prefix = f"#{detection.track_id} " if detection.track_id is not None else ""
            label = f"{prefix}{detection.class_name} {detection.confidence:.2f}"
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
                busy = self._run_once()
            except Exception:
                logger.exception("Edge vision loop failed")
                self._stop.wait(1.0)
                continue
            self._sample_resources()
            self._stop.wait(self._pause_after(busy))

    def _pause_after(self, busy: float | None) -> float:
        if busy is None:
            return IDLE_WAIT_SECONDS  # nothing ready: wait for a new frame
        # Rest in proportion to the work just done so the inference thread
        # stays under vision_max_busy_ratio of the CPU time.
        ratio = self.settings.vision_max_busy_ratio
        return busy * (1.0 - ratio) / ratio

    def _run_once(self) -> float | None:
        """Analyses one camera; None when no camera has a new frame due.

        Among the cameras with a new frame and whose minimum interval has
        passed, the one that used least detector time goes first. Only the
        newest frame of a camera is ever analysed: older ones are skipped.
        """
        now = time.monotonic()
        cameras = self.camera_manager.configs()
        self._forget_removed_cameras({camera.id for camera in cameras})
        ready = [camera for camera in cameras if self._check_camera(camera, now)]
        if not ready:
            return None
        camera = min(ready, key=lambda item: self._schedules[item.id].virtual_time)
        schedule = self._schedules[camera.id]
        schedule.next_due = now + self.settings.vision_interval_seconds
        busy = self._analyse(camera)
        schedule.virtual_time += max(busy, 1e-3)
        return busy

    def _process_enabled_cameras(self) -> None:
        """One pass over every camera, ignoring the per-camera interval."""
        for camera in self.camera_manager.configs():
            if self._check_camera(camera, time.monotonic(), force=True):
                self._analyse(camera)

    def _check_camera(self, camera, now: float, force: bool = False) -> bool:
        """Whether the camera has a new, live frame to analyse now."""
        if not camera.enabled or not (camera.vision_enabled or camera.mapping_enabled):
            self._mark_unavailable(
                camera.id, "STOPPED", "vision_stopped", mapping_status="STOPPED", mapping_error=None
            )
            return False
        schedule = self._schedule(camera.id)
        if not force and now < schedule.next_due:
            return False
        identity = self._frame_identity(camera.id)
        if identity is None:
            self._mark_unavailable(camera.id, "WAITING_FRAME", "camera_observation_unavailable")
            return False
        frame_key, frame_at = identity
        if frame_key is not None and self._last_frame_at.get(camera.id) == frame_key:
            return False  # the camera has not delivered a new frame yet
        age = (datetime.now(timezone.utc) - frame_at).total_seconds() if frame_at else 0.0
        if age > self.settings.vision_stale_frame_seconds:
            # The stream froze without disconnecting: the last image is
            # not what the camera sees now.
            self._mark_unavailable(
                camera.id, "STALE_FRAME", "camera_frame_stale", error="Imagem da câmera congelada."
            )
            return False
        return True

    def _schedule(self, camera_id: str) -> _CameraSchedule:
        schedule = self._schedules.get(camera_id)
        if schedule is None:
            # A new camera starts level with the others instead of owing or
            # being owed all the detector time used before it appeared.
            start = min((item.virtual_time for item in self._schedules.values()), default=0.0)
            schedule = self._schedules[camera_id] = _CameraSchedule(virtual_time=start)
        return schedule

    def _forget_removed_cameras(self, camera_ids: set[str]) -> None:
        for camera_id in set(self._schedules) - camera_ids:
            del self._schedules[camera_id]

    def _mark_unavailable(self, camera_id: str, status: str, reason: str, **state: Any) -> None:
        """Nothing to analyse: clears the boxes once, not on every scheduler pass."""
        with self._lock:
            current = (self._state.get(camera_id) or {}).get("status")
            has_result = camera_id in self._results
        if current == status and not has_result:
            return
        self._set_state(camera_id, status=status, detections=[], poses=[], **state)
        self._drop_result(camera_id)
        self._events_unavailable(camera_id, reason)

    def _frame_identity(self, camera_id: str) -> tuple[Any, datetime | None] | None:
        """(frame key, frame_at) of the latest frame, copying it only if unavoidable."""
        latest_identity = getattr(self.camera_manager, "latest_identity", None)
        if latest_identity is not None:
            identity = latest_identity(camera_id)
            if identity is None or not identity[1]:
                return None
            session_id, frame_id, frame_at = identity
            return (session_id, frame_id), frame_at
        frame, frame_at, session_id, frame_id = self._latest_frame(camera_id)
        if frame is None:
            return None
        return ((session_id, frame_id) if frame_id is not None else frame_at), frame_at

    def _analyse(self, camera) -> float:
        """Runs the models on the camera's newest frame.

        Returns the seconds of work, not counting time spent waiting for the
        post-processing queue: that wait is not CPU this camera used.
        """
        started = time.perf_counter()
        waited = 0.0
        frame, frame_at, session_id, frame_id = self._latest_frame(camera.id)
        if frame is None:
            self._mark_unavailable(camera.id, "WAITING_FRAME", "camera_observation_unavailable")
            return time.perf_counter() - started
        frame_key = (session_id, frame_id) if frame_id is not None else frame_at
        if frame_key is not None:
            self._last_frame_at[camera.id] = frame_key
        schedule = self._schedule(camera.id)
        if frame_id is not None:
            last = schedule.last_frame
            if last is not None and last[0] == session_id and frame_id > last[1] + 1:
                schedule.frames_skipped += frame_id - last[1] - 1
            schedule.last_frame = (session_id, frame_id)
        frame_age_ms = (
            round((datetime.now(timezone.utc) - frame_at).total_seconds() * 1000, 1) if frame_at else None
        )
        updates: dict[str, Any] = {
            "last_frame_at": frame_at.isoformat() if frame_at else None,
            "frame_age_ms": frame_age_ms,
            "frames_skipped": schedule.frames_skipped,
        }
        detections: list[EdgeDetection] = []
        vision_ok = False
        detect_ms = pose_ms = 0.0
        if camera.vision_enabled:
            model_started = time.perf_counter()
            try:
                detections, detector = self._detect(frame)
                detect_ms = (time.perf_counter() - model_started) * 1000
                tracker = self._trackers.setdefault(camera.id, EdgeTracker())
                detections = tracker.update(detections, frame_at or datetime.now(timezone.utc))
                updates.update(status="RUNNING", detections=detections, detector=detector, error=None)
                vision_ok = True
            except Exception as exc:
                updates.update(status="ERROR", detections=[], error=str(exc))
                self._events_unavailable(camera.id, "detector_error")
        else:
            updates.update(status="STOPPED", detections=[])
            self._events_unavailable(camera.id, "vision_stopped")
        if camera.mapping_enabled:
            pose_started = time.perf_counter()
            updates.update(self._estimate_poses(frame))
            pose_ms = (time.perf_counter() - pose_started) * 1000
        else:
            updates.update(poses=[], mapping_status="STOPPED", mapping_error=None)
        if vision_ok or updates.get("mapping_status") == "ACTIVE":
            result = self._publish_result(
                camera.id, frame, frame_at, session_id, frame_id,
                detections if vision_ok else [], updates.get("poses") or [],
            )
            if frame_at is not None:
                updates["known_latency_ms"] = round((result.processed_at - frame_at).total_seconds() * 1000, 1)
        else:
            self._drop_result(camera.id)
        if vision_ok:
            # Event rules and observers run on the post-processing worker, so
            # a slow disk write does not delay the next camera's inference.
            camera_id = camera.id
            waited = self._post.submit(lambda: self._observe_frame(camera_id, frame, detections, frame_at))
        updates["inference_latency_ms"] = round(detect_ms + pose_ms, 1)
        updates["inference_ms"] = round((time.perf_counter() - started - waited) * 1000, 1)
        self._set_state(camera.id, **updates)
        return time.perf_counter() - started - waited

    def _latest_frame(self, camera_id: str) -> tuple[Any | None, datetime | None, str, int | None]:
        latest_snapshot = getattr(self.camera_manager, "latest_snapshot", None)
        if latest_snapshot is None:  # a camera manager without frame identity
            frame, frame_at = self.camera_manager.latest_frame(camera_id)
            return frame, frame_at, "", None
        snapshot = latest_snapshot(camera_id)
        if snapshot is None:
            return None, None, "", None
        return snapshot.frame, snapshot.frame_at, snapshot.session_id, snapshot.frame_id

    def _publish_result(
        self,
        camera_id: str,
        frame,
        frame_at: datetime | None,
        session_id: str,
        frame_id: int | None,
        detections: list[EdgeDetection],
        poses: list[EdgePose],
    ) -> VisionResult:
        # The frame is this loop's own copy (LatestFrameBuffer copies on read).
        # Freezing it after the models ran lets readers share it without
        # copying, and makes any in-place drawing fail loudly.
        frame.flags.writeable = False
        height, width = frame.shape[:2]
        result = VisionResult(
            camera_id=camera_id,
            session_id=session_id,
            frame_id=frame_id,
            frame_at=frame_at,
            processed_at=datetime.now(timezone.utc),
            frame_width=int(width),
            frame_height=int(height),
            detections=tuple(detections),
            poses=tuple(poses),
            frame=frame,
        )
        with self._lock:
            self._results[camera_id] = result
        return result

    def _drop_result(self, camera_id: str) -> None:
        """Nothing was analysed: old boxes must not be shown as current."""
        with self._lock:
            self._results.pop(camera_id, None)

    def _observe_frame(self, camera_id: str, frame, detections: list[EdgeDetection], frame_at) -> None:
        self._process_events(camera_id, frame, detections, frame_at)
        self._notify_observers(camera_id, frame, detections, frame_at)

    def _process_events(self, camera_id: str, frame, detections: list[EdgeDetection], frame_at) -> None:
        if self.events is None:
            return
        try:
            self.events.process(camera_id, frame, detections, now=time.monotonic(), observed_at=frame_at, tracked=True)
        except Exception:
            logger.exception("Event pipeline failed for camera %s", camera_id)

    def _notify_observers(self, camera_id: str, frame, detections: list[EdgeDetection], frame_at) -> None:
        observed_at = frame_at or datetime.now(timezone.utc)
        for observer in self.observers:
            try:
                observer.observe(camera_id, frame, detections, observed_at)
            except Exception:
                logger.exception("Frame observer %s failed for camera %s", type(observer).__name__, camera_id)

    def _events_unavailable(self, camera_id: str, reason: str) -> None:
        self._trackers.pop(camera_id, None)
        self._last_frame_at.pop(camera_id, None)
        # Queued behind this camera's pending frames, so observers see them in order.
        self._post.submit(lambda: self._release_camera(camera_id, reason))

    def _release_camera(self, camera_id: str, reason: str) -> None:
        for observer in self.observers:
            try:
                observer.camera_unavailable(camera_id, reason)
            except Exception:
                logger.exception("Frame observer %s failed to release camera %s", type(observer).__name__, camera_id)
        if self.events is None:
            return
        try:
            self.events.camera_unavailable(camera_id, reason)
        except Exception:
            logger.exception("Event pipeline failed to release camera %s", camera_id)

    def _sample_resources(self) -> None:
        """Process-wide CPU and memory, resampled at most every few seconds."""
        now = time.monotonic()
        if now - self._resources_sampled_at < RESOURCE_SAMPLE_SECONDS:
            return
        try:
            cpu = self._process.cpu_percent(None)  # since the previous sample
            rss = self._process.memory_info().rss
        except (psutil.Error, OSError):
            return
        first = self._resources_sampled_at == 0.0
        self._resources_sampled_at = now
        self._resources = {
            "cpu_usage_percent": None if first else round(cpu / (psutil.cpu_count() or 1), 1),
            "memory_usage_mb": round(rss / 2**20, 1),
        }

    def _detect(self, frame) -> tuple[list[EdgeDetection], str]:
        model = self._detector.get()
        if model is None:
            return self._detect_people_hog(frame), "opencv-hog"
        results = model.predict(
            frame,
            conf=self.settings.vision_confidence,
            classes=list(DETECTED_CLASSES),
            imgsz=self.settings.vision_input_size,
            verbose=False,
        )
        detections: list[EdgeDetection] = []
        for result in results:
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            classes = boxes.cls.tolist() if getattr(boxes, "cls", None) is not None else [0] * len(boxes.conf)
            for xyxy, confidence, class_id in zip(boxes.xyxy.tolist(), boxes.conf.tolist(), classes):
                class_name = DETECTED_CLASSES.get(int(class_id))
                if class_name is None:
                    continue
                x1, y1, x2, y2 = (float(value) for value in xyxy[:4])
                detections.append(EdgeDetection(class_name, round(float(confidence), 4), x1, y1, x2, y2))
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
