"""Camera runtime for the CAMPEX Cloud, where cameras are captured by CAMPEX Nodes.

The serverless backend cannot open RTSP streams on the customer's network.
Each Node reports camera status, Vision results and (while someone is
watching) the latest JPEG frame; this module stores those reports and answers
the dashboard from them. Vision and mapping toggles are stored on the camera
and travel back to the Node in the report response.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import cv2
import numpy as np

from backend.cameras.models import Camera
from backend.cameras.security import sanitize_error_message
from backend.config import Settings
from backend.database.db import connect
from backend.vision.models import BoundingBox, PoseEstimate, PoseKeypoint, TrackedObject
from backend.vision.overlay import OverlayRenderer


NODE_CAMERA_SOURCE_TYPES = ("rtsp", "ip_camera")
# A camera whose Node has not reported for this long is shown as offline.
REPORT_STALE_SECONDS = 45
# Frames are uploaded every second while a dashboard asked for one recently.
VIEWER_ACTIVE_SECONDS = 20
LIVE_UPLOAD_SECONDS = 1.0
IDLE_UPLOAD_SECONDS = 10.0
TEST_JOB_TTL_SECONDS = 90
MAX_FRAME_BYTES = 1_500_000
_NEVER_REPORTED = "1970-01-01T00:00:00+00:00"

_overlay_renderer = OverlayRenderer()


class CloudCameraError(Exception):
    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class CameraReport:
    camera_id: str
    status: str
    last_error: str | None
    frames_received: int
    approximate_fps: float | None
    width: int | None
    height: int | None
    last_frame_at: str | None
    vision: dict[str, Any]
    objects: list[dict[str, Any]]
    poses: list[dict[str, Any]]
    frame_jpeg: bytes | None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _seconds_since(value: str | None) -> float | None:
    parsed = _parse_time(value)
    return None if parsed is None else (_utc_now() - parsed).total_seconds()


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _loads(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def decode_frame(frame_base64: str | None) -> bytes | None:
    if not frame_base64:
        return None
    try:
        frame = base64.b64decode(frame_base64, validate=True)
    except (ValueError, TypeError):
        return None
    if not frame.startswith(b"\xff\xd8") or len(frame) > MAX_FRAME_BYTES:
        return None
    return frame


class CloudCameraRuntime:
    def __init__(self, settings: Settings) -> None:
        self.database_target = settings.database_target

    # ------------------------------------------------------------------
    # Node side
    # ------------------------------------------------------------------

    def node_cameras(self, organization_id: str, node_id: str) -> list[dict[str, Any]]:
        with connect(self.database_target) as connection:
            rows = connection.execute(
                """
                SELECT id, name, source_type, source_uri, enabled, vision_enabled,
                       mapping_enabled, updated_at
                FROM cameras
                WHERE organization_id = ?
                  AND source_type IN ('rtsp', 'ip_camera')
                  AND (node_id IS NULL OR node_id = ?)
                ORDER BY created_at ASC
                """,
                (organization_id, node_id),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "source_type": row["source_type"],
                "source_uri": row["source_uri"],
                "enabled": bool(row["enabled"]),
                "vision_enabled": bool(row["vision_enabled"]),
                "mapping_enabled": bool(row["mapping_enabled"]),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def record_reports(
        self,
        organization_id: str,
        node_id: str,
        reports: list[CameraReport],
        test_results: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        cameras = {camera["id"]: camera for camera in self.node_cameras(organization_id, node_id)}
        now = _utc_now().isoformat()
        with connect(self.database_target) as connection:
            for report in reports:
                if report.camera_id not in cameras:
                    continue
                self._store_report(connection, organization_id, node_id, report, now)
            for job_id, result in test_results or []:
                connection.execute(
                    """
                    UPDATE camera_test_jobs
                    SET status = 'done', result_json = ?, completed_at = ?
                    WHERE id = ? AND organization_id = ? AND node_id = ?
                    """,
                    (_json(result), now, job_id, organization_id, node_id),
                )
            viewers = {
                row["camera_id"]: row["viewer_seen_at"]
                for row in connection.execute(
                    "SELECT camera_id, viewer_seen_at FROM camera_live_state WHERE organization_id = ?",
                    (organization_id,),
                ).fetchall()
            }
            test_jobs = self._claim_test_jobs(connection, organization_id, node_id, now)

        camera_flags = {}
        any_live = False
        for camera_id, camera in cameras.items():
            idle_for = _seconds_since(viewers.get(camera_id))
            live = idle_for is not None and idle_for <= VIEWER_ACTIVE_SECONDS
            any_live = any_live or live
            camera_flags[camera_id] = {
                "enabled": camera["enabled"],
                "vision_enabled": camera["vision_enabled"],
                "mapping_enabled": camera["mapping_enabled"],
                "live": live,
            }
        return {
            "next_upload_seconds": LIVE_UPLOAD_SECONDS if any_live else IDLE_UPLOAD_SECONDS,
            "cameras": camera_flags,
            "test_jobs": test_jobs,
        }

    def _store_report(self, connection, organization_id: str, node_id: str, report: CameraReport, now: str) -> None:
        status = report.status if report.status in {"CONNECTING", "ONLINE", "DEGRADED", "OFFLINE"} else "OFFLINE"
        connection.execute(
            """
            INSERT INTO camera_live_state (
                camera_id, organization_id, node_id, status, last_error, frames_received,
                approximate_fps, width, height, last_frame_at, reported_at,
                vision_json, objects_json, poses_json, frame_jpeg, frame_captured_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(camera_id) DO UPDATE SET
                node_id = excluded.node_id,
                status = excluded.status,
                last_error = excluded.last_error,
                frames_received = excluded.frames_received,
                approximate_fps = excluded.approximate_fps,
                width = COALESCE(excluded.width, camera_live_state.width),
                height = COALESCE(excluded.height, camera_live_state.height),
                last_frame_at = excluded.last_frame_at,
                reported_at = excluded.reported_at,
                vision_json = excluded.vision_json,
                objects_json = excluded.objects_json,
                poses_json = excluded.poses_json,
                frame_jpeg = COALESCE(excluded.frame_jpeg, camera_live_state.frame_jpeg),
                frame_captured_at = COALESCE(excluded.frame_captured_at, camera_live_state.frame_captured_at)
            """,
            (
                report.camera_id,
                organization_id,
                node_id,
                status,
                sanitize_error_message(report.last_error),
                max(0, int(report.frames_received or 0)),
                report.approximate_fps,
                report.width,
                report.height,
                report.last_frame_at,
                now,
                _json(report.vision or {}),
                _json(report.objects or []),
                _json(report.poses or []),
                report.frame_jpeg,
                now if report.frame_jpeg else None,
            ),
        )
        connection.execute(
            """
            UPDATE cameras
            SET status = ?, connection_state = ?, last_frame_at = COALESCE(?, last_frame_at)
            WHERE id = ? AND organization_id = ?
            """,
            (status, status, report.last_frame_at, report.camera_id, organization_id),
        )

    def _claim_test_jobs(self, connection, organization_id: str, node_id: str, now: str) -> list[dict[str, str]]:
        rows = connection.execute(
            """
            SELECT id, source_uri, expires_at FROM camera_test_jobs
            WHERE organization_id = ? AND node_id = ? AND status = 'pending'
            ORDER BY created_at ASC
            """,
            (organization_id, node_id),
        ).fetchall()
        jobs = []
        for row in rows:
            expired = (_seconds_since(row["expires_at"]) or 0) > 0
            connection.execute(
                "UPDATE camera_test_jobs SET status = ? WHERE id = ?",
                ("expired" if expired else "running", row["id"]),
            )
            if not expired:
                jobs.append({"id": row["id"], "source_uri": row["source_uri"]})
        return jobs

    # ------------------------------------------------------------------
    # Dashboard side
    # ------------------------------------------------------------------

    def _live_state(self, camera_id: str, organization_id: str) -> dict[str, Any] | None:
        with connect(self.database_target) as connection:
            row = connection.execute(
                """
                SELECT camera_id, node_id, status, last_error, frames_received, approximate_fps,
                       width, height, last_frame_at, reported_at, vision_json, objects_json,
                       poses_json, frame_captured_at
                FROM camera_live_state
                WHERE camera_id = ? AND organization_id = ?
                """,
                (camera_id, organization_id),
            ).fetchone()
        if row is None or row["reported_at"] == _NEVER_REPORTED:
            return None
        return dict(row)

    def _camera_flags(self, camera_id: str, organization_id: str) -> dict[str, bool]:
        with connect(self.database_target) as connection:
            row = connection.execute(
                "SELECT vision_enabled, mapping_enabled FROM cameras WHERE id = ? AND organization_id = ?",
                (camera_id, organization_id),
            ).fetchone()
        if row is None:
            return {"vision_enabled": False, "mapping_enabled": False}
        return {"vision_enabled": bool(row["vision_enabled"]), "mapping_enabled": bool(row["mapping_enabled"])}

    def _is_fresh(self, state: dict[str, Any] | None) -> bool:
        if state is None:
            return False
        age = _seconds_since(state.get("reported_at"))
        return age is not None and age <= REPORT_STALE_SECONDS

    def health(self, camera: Camera, organization_id: str) -> dict[str, Any]:
        state = self._live_state(camera.id, organization_id)
        fresh = self._is_fresh(state)
        if state is None:
            status, error = "OFFLINE", self._waiting_message(organization_id)
        elif not fresh:
            status, error = "OFFLINE", "CAMPEX Node sem comunicação com a nuvem."
        else:
            status, error = state["status"], state.get("last_error")
        resolution = None
        if state and state.get("width") and state.get("height"):
            resolution = {"width": state["width"], "height": state["height"]}
        return {
            "camera_id": camera.id,
            "status": status,
            "connection_state": status,
            "last_frame_at": state.get("last_frame_at") if state else None,
            "last_successful_frame": state.get("last_frame_at") if state else None,
            "last_connected_at": None,
            "last_disconnected_at": None,
            "last_error": error,
            "resolution": resolution,
            "approximate_fps": state.get("approximate_fps") if fresh else None,
            "frames_received": state.get("frames_received", 0) if state else 0,
            "reconnect_attempts": 0,
            "consecutive_failures": 0,
            "runtime": "campex_node",
            "node_id": state.get("node_id") if state else None,
            "reported_at": state.get("reported_at") if state else None,
        }

    def _waiting_message(self, organization_id: str) -> str:
        with connect(self.database_target) as connection:
            node = connection.execute(
                "SELECT 1 FROM campex_nodes WHERE organization_id = ? AND revoked_at IS NULL",
                (organization_id,),
            ).fetchone()
        if node is None:
            return "Nenhum CAMPEX Node conectado. Instale e pareie o Node no computador da rede das câmeras."
        return (
            "Aguardando o CAMPEX Node enviar o status desta câmera. "
            "Se demorar mais de 1 minuto, confirme que o Node está aberto e atualizado."
        )

    def vision_status(self, camera: Camera, organization_id: str) -> dict[str, Any]:
        flags = self._camera_flags(camera.id, organization_id)
        state = self._live_state(camera.id, organization_id)
        fresh = self._is_fresh(state)
        vision = _loads(state.get("vision_json"), {}) if fresh else {}
        objects = _loads(state.get("objects_json"), []) if fresh else []
        poses = _loads(state.get("poses_json"), []) if fresh else []

        reported = str(vision.get("status") or "").upper()
        if not flags["vision_enabled"]:
            status = "STOPPED"
        elif not fresh:
            status = "STARTING"
        elif reported in {"RUNNING", "ERROR", "WAITING_FRAME"}:
            status = "STARTING" if reported == "WAITING_FRAME" else reported
        else:
            status = "STARTING"

        mapping_reported = str(vision.get("mapping_status") or "").upper()
        if not flags["mapping_enabled"]:
            mapping_state = "STOPPED"
        elif mapping_reported == "ACTIVE" and fresh:
            mapping_state = "ACTIVE"
        else:
            mapping_state = "LOADING"
        mapping_error = vision.get("mapping_error") if flags["mapping_enabled"] and fresh else None
        if mapping_error:
            mapping_state = "ERROR"

        error = vision.get("error") if status == "ERROR" else None
        if flags["vision_enabled"] and not fresh:
            error = "Aguardando o CAMPEX Node aplicar a configuração."
        return {
            "camera_id": camera.id,
            "status": status,
            "error": error,
            "detector": vision.get("detector"),
            "device": vision.get("device"),
            "runtime": "campex_node",
            "metrics": {
                "camera_fps": state.get("approximate_fps") if fresh else 0,
                "vision_fps": vision.get("vision_fps", 0),
                "inference_ms": vision.get("inference_ms"),
                "frames_processed": vision.get("frames_processed", 0),
                "frames_received": state.get("frames_received", 0) if fresh else 0,
                "objects_detected": len(objects),
            },
            "components": {
                "mapping": {
                    "state": mapping_state,
                    "poses": len(poses),
                    "error": mapping_error,
                }
            },
        }

    def objects(self, camera_id: str, organization_id: str) -> list[dict[str, Any]]:
        state = self._live_state(camera_id, organization_id)
        return _loads(state.get("objects_json"), []) if self._is_fresh(state) else []

    def poses(self, camera_id: str, organization_id: str) -> list[dict[str, Any]]:
        state = self._live_state(camera_id, organization_id)
        return _loads(state.get("poses_json"), []) if self._is_fresh(state) else []

    def snapshot(self, camera_id: str, organization_id: str, *, overlay: bool) -> bytes | None:
        """Latest frame as JPEG, and marks the camera as being watched."""
        now = _utc_now().isoformat()
        with connect(self.database_target) as connection:
            # The placeholder row (never reported) lets the Node see that a
            # viewer is waiting before it has uploaded anything.
            connection.execute(
                """
                INSERT INTO camera_live_state (camera_id, organization_id, node_id, reported_at, viewer_seen_at)
                VALUES (?, ?, '', ?, ?)
                ON CONFLICT(camera_id) DO UPDATE SET viewer_seen_at = excluded.viewer_seen_at
                """,
                (camera_id, organization_id, _NEVER_REPORTED, now),
            )
            row = connection.execute(
                """
                SELECT frame_jpeg, frame_captured_at, reported_at, objects_json, poses_json, width, height
                FROM camera_live_state
                WHERE camera_id = ? AND organization_id = ?
                """,
                (camera_id, organization_id),
            ).fetchone()
        if row is None or row["frame_jpeg"] is None or row["reported_at"] == _NEVER_REPORTED:
            return None
        # A Node that stopped reporting would otherwise leave a frozen image
        # on screen that looks live.
        if not self._is_fresh({"reported_at": row["reported_at"]}):
            return None
        frame_bytes = bytes(row["frame_jpeg"])
        if not overlay:
            return frame_bytes
        objects = _loads(row["objects_json"], [])
        poses = _loads(row["poses_json"], [])
        if not objects and not poses:
            return frame_bytes
        source_size = (row["width"], row["height"]) if row["width"] and row["height"] else None
        return _render_overlay(frame_bytes, camera_id, objects, poses, source_size) or frame_bytes

    def set_flags(
        self,
        camera_id: str,
        organization_id: str,
        *,
        vision_enabled: bool | None = None,
        mapping_enabled: bool | None = None,
    ) -> None:
        assignments, params = [], []
        if vision_enabled is not None:
            assignments.append("vision_enabled = ?")
            params.append(int(vision_enabled))
        if mapping_enabled is not None:
            assignments.append("mapping_enabled = ?")
            params.append(int(mapping_enabled))
        if not assignments:
            return
        with connect(self.database_target) as connection:
            connection.execute(
                f"UPDATE cameras SET {', '.join(assignments)}, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND organization_id = ?",
                (*params, camera_id, organization_id),
            )

    def forget(self, camera_id: str, organization_id: str) -> None:
        with connect(self.database_target) as connection:
            connection.execute(
                "DELETE FROM camera_live_state WHERE camera_id = ? AND organization_id = ?",
                (camera_id, organization_id),
            )

    def assign_default_node(self, camera_id: str, organization_id: str) -> None:
        """Pins a new camera to the organization's Node when there is exactly one."""
        with connect(self.database_target) as connection:
            nodes = connection.execute(
                "SELECT id FROM campex_nodes WHERE organization_id = ? AND revoked_at IS NULL",
                (organization_id,),
            ).fetchall()
            if len(nodes) == 1:
                connection.execute(
                    "UPDATE cameras SET node_id = ? WHERE id = ? AND organization_id = ? AND node_id IS NULL",
                    (nodes[0]["id"], camera_id, organization_id),
                )

    def create_test_job(self, organization_id: str, source_uri: str) -> dict[str, Any]:
        with connect(self.database_target) as connection:
            node = connection.execute(
                """
                SELECT id, last_seen_at FROM campex_nodes
                WHERE organization_id = ? AND revoked_at IS NULL
                ORDER BY last_seen_at DESC
                LIMIT 1
                """,
                (organization_id,),
            ).fetchone()
            if node is None:
                raise CloudCameraError(
                    "Nenhum CAMPEX Node conectado. Instale e pareie o Node no computador da rede das câmeras."
                )
            seen_for = _seconds_since(node["last_seen_at"])
            if seen_for is None or seen_for > 120:
                raise CloudCameraError("O CAMPEX Node está offline. Abra o Node no computador da rede das câmeras.")
            job_id = f"ctj_{uuid4().hex}"
            expires_at = (_utc_now() + timedelta(seconds=TEST_JOB_TTL_SECONDS)).isoformat()
            connection.execute(
                """
                INSERT INTO camera_test_jobs (id, organization_id, node_id, source_uri, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, organization_id, node["id"], source_uri, expires_at),
            )
        return {"job_id": job_id, "status": "pending", "pending": True}

    def get_test_job(self, organization_id: str, job_id: str) -> dict[str, Any] | None:
        with connect(self.database_target) as connection:
            row = connection.execute(
                """
                SELECT id, status, result_json, expires_at
                FROM camera_test_jobs
                WHERE id = ? AND organization_id = ?
                """,
                (job_id, organization_id),
            ).fetchone()
            if row is None:
                return None
            # The RTSP credentials are only needed until the Node picks the job up.
            if row["status"] == "done":
                connection.execute("UPDATE camera_test_jobs SET source_uri = '' WHERE id = ?", (job_id,))
        if row["status"] == "done":
            result = _loads(row["result_json"], {})
            return {**result, "job_id": job_id, "pending": False}
        expired = row["status"] == "expired" or (_seconds_since(row["expires_at"]) or 0) > 0
        if expired:
            return {
                "job_id": job_id,
                "pending": False,
                "ok": False,
                "success": False,
                "status": "OFFLINE",
                "error": "O CAMPEX Node não respondeu ao teste. Verifique se ele está aberto e conectado.",
            }
        return {"job_id": job_id, "pending": True, "status": row["status"]}


def _render_overlay(
    frame_bytes: bytes,
    camera_id: str,
    objects: list[dict],
    poses: list[dict],
    source_size: tuple[int, int] | None = None,
) -> bytes | None:
    try:
        frame = cv2.imdecode(np.frombuffer(frame_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return None
        # Detections are in source-camera pixels; the relayed frame may be downscaled.
        height, width = frame.shape[:2]
        sx = width / source_size[0] if source_size else 1.0
        sy = height / source_size[1] if source_size else 1.0

        def box(values) -> BoundingBox:
            x1, y1, x2, y2 = (float(value) for value in values[:4])
            return BoundingBox(x1 * sx, y1 * sy, x2 * sx, y2 * sy)

        now = _utc_now()
        tracked = [
            TrackedObject(
                track_id=int(item.get("track_id") or index + 1),
                camera_id=camera_id,
                class_name=str(item.get("class_name") or "person"),
                confidence=float(item.get("confidence") or 0),
                bounding_box=box(item["bounding_box"]),
                timestamp=now,
            )
            for index, item in enumerate(objects)
            if len(item.get("bounding_box") or []) >= 4
        ]
        estimates = [
            PoseEstimate(
                pose_id=int(item.get("pose_id") or index + 1),
                camera_id=camera_id,
                confidence=float(item.get("confidence") or 0),
                bounding_box=box(item["bounding_box"]),
                keypoints=[
                    PoseKeypoint(
                        name=str(point.get("name") or ""),
                        x=float(point.get("x") or 0) * sx,
                        y=float(point.get("y") or 0) * sy,
                        confidence=float(point.get("confidence") or 0),
                    )
                    for point in item.get("keypoints") or []
                ],
                timestamp=now,
            )
            for index, item in enumerate(poses)
            if len(item.get("bounding_box") or []) >= 4
        ]
        rendered = _overlay_renderer.render(frame, tracked, estimates)
        ok, encoded = cv2.imencode(".jpg", rendered, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        return encoded.tobytes() if ok else None
    except Exception:
        return None
