from __future__ import annotations

import asyncio
import json
import os
import platform
import sys
import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import time

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import HTTPConnection
from pydantic import BaseModel, Field

from backend.cameras.security import sanitize_error_message
from backend.config import ROOT_DIR as BACKEND_ROOT_DIR
from backend.middleware.cors import LocalNetworkCORSMiddleware

from backend.auth.service import AuthError
from campex_node.analytics import resolve_period
from campex_node.auth import SESSION_TTL, NodeUser
from campex_node.backup import backup_database, backup_filename, restore_database, validate_backup
from campex_node.core.config import NodeCameraConfig, NodeSettings
from campex_node.node_ui import NODE_HTML


class CloudConnectionPayload(BaseModel):
    cloud_url: str = Field(min_length=1, max_length=500)
    pairing_code: str = Field(min_length=1, max_length=40)
    node_name: str = Field(default="CAMPEX Node", max_length=120)


class PairingStartPayload(BaseModel):
    cloud_url: str | None = Field(default=None, max_length=500)
    node_name: str = Field(default="CAMPEX Node", max_length=120)


class LocalCameraPayload(BaseModel):
    id: str | None = Field(default=None, max_length=120)
    name: str = Field(default="", max_length=120)
    rtsp_url: str | None = Field(default=None, max_length=1000)
    source_uri: str | None = Field(default=None, max_length=1000)
    source_type: str = Field(default="rtsp", max_length=40)
    enabled: bool = True
    area_id: str | None = None
    vision_enabled: bool | None = None


class CameraTestPayload(BaseModel):
    rtsp_url: str | None = Field(default=None, max_length=1000)
    source_uri: str | None = Field(default=None, max_length=1000)


class PairingLookupPayload(BaseModel):
    code: str = Field(min_length=1, max_length=40)


class PairingAuthorizePayload(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    node_name: str | None = Field(default=None, max_length=120)


ZONE_TYPE_PATTERN = "^(monitored|restricted|machine|station|dock|area|line)$"


class ZoneCreatePayload(BaseModel):
    camera_id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    type: str = Field(pattern=ZONE_TYPE_PATTERN)
    enabled: bool = True
    # A counting line has 2 points; every other zone is a polygon.
    points: list[list[float]] = Field(min_length=2)
    settings: dict[str, Any] | None = None


class ZoneUpdatePayload(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    type: str | None = Field(default=None, pattern=ZONE_TYPE_PATTERN)
    enabled: bool | None = None
    points: list[list[float]] | None = None
    settings: dict[str, Any] | None = None


class RegisterPayload(BaseModel):
    name: str
    email: str
    password: str
    role: str | None = None


class LoginPayload(BaseModel):
    email: str
    password: str


class UserUpdatePayload(BaseModel):
    role: str | None = Field(default=None, pattern="^(admin|operator)$")
    password: str | None = None


class PasswordChangePayload(BaseModel):
    current_password: str
    new_password: str


class FactorySettingsPayload(BaseModel):
    timezone: str | None = Field(default=None, max_length=64)
    currency: str | None = Field(default=None, max_length=3)


class ShiftBreakPayload(BaseModel):
    name: str = Field(default="Intervalo", max_length=60)
    start: str
    end: str


class ShiftPayload(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    start: str | None = None
    end: str | None = None
    days: list[int] | None = None
    breaks: list[ShiftBreakPayload] | None = None
    enabled: bool | None = None


class ProductionCountPayload(BaseModel):
    """A count from a PLC, sensor or operator, added to a machine or line."""

    zone_id: str = Field(min_length=1, max_length=120)
    count: int = Field(ge=1, le=100000)
    at: datetime | None = None


class EventUpdatePayload(BaseModel):
    status: str = Field(pattern="^(OPEN|REVIEWED|CLOSED)$")


EVIDENCE_VARIANTS = {
    "overlay": ("overlay_path", "image/jpeg"),
    "snapshot": ("snapshot_path", "image/jpeg"),
    "metadata": ("evidence_metadata_path", "application/json"),
    "clip": ("clip_path", None),
}


class CloudManagedCameraError(ValueError):
    """The camera belongs to the CAMPEX Cloud and is changed from its dashboard."""

    def __init__(self) -> None:
        super().__init__(
            "Esta câmera é gerenciada pela CAMPEX Cloud. Altere-a pelo painel da Cloud; "
            "o Node aplica a mudança em alguns segundos."
        )


class LocalNodeRuntime:
    def __init__(self) -> None:
        self.lifecycle = self._build_from_persisted_connection()
        self.lifecycle.initialize()
        self.lifecycle.start()

    def restore_database(self, backup: Path) -> Path:
        """Replaces the database with a validated backup and restarts the Node.

        The current database is kept next to the backups first, so a wrong
        restore can be undone.
        """
        validate_backup(backup)
        settings = self.lifecycle.settings
        safety = backup_database(
            settings.database_path, settings.backups_path / f"antes-da-restauracao-{backup_filename()}"
        )
        self.lifecycle.stop()
        try:
            restore_database(backup, settings.database_path)
        finally:
            backup.unlink(missing_ok=True)
            self.lifecycle = self._build_from_persisted_connection()
            self.lifecycle.initialize()
            self.lifecycle.start()
        return safety

    def reconfigure(self, payload: CloudConnectionPayload) -> dict | None:
        from campex_node.main import build_lifecycle

        current_status = self.status()
        if current_status.get("paired"):
            if self.lifecycle.config_sync is not None:
                self.lifecycle.config_sync.sync_once()
            return {"already_paired": True}
        self.lifecycle.stop()
        settings = replace(
            NodeSettings.from_env(),
            cloud_url=_normalize_cloud_api_url(payload.cloud_url),
        )
        os.environ["CAMPEX_NODE_CLOUD_URL"] = settings.cloud_url or ""
        claim_lifecycle = build_lifecycle(settings)
        claim_lifecycle.store.initialize()
        result = claim_lifecycle.cloud_client.claim_pairing_code(
            code=payload.pairing_code,
            node_name=payload.node_name.strip() or "CAMPEX Node",
            version=settings.version,
        )
        if not result.ok or not isinstance(result.data, dict):
            self.lifecycle = self._build_from_persisted_connection()
            self.lifecycle.initialize()
            self.lifecycle.start()
            raise ValueError(result.error or "Não foi possível parear este Node.")
        data = result.data
        paired_settings = replace(
            settings,
            node_id=data["node_id"],
            cloud_token=data["node_token"],
            organization_id=data["organization_id"],
        )
        os.environ["CAMPEX_NODE_ID"] = paired_settings.node_id or ""
        os.environ["CAMPEX_NODE_TOKEN"] = paired_settings.cloud_token or ""
        os.environ["CAMPEX_NODE_ORGANIZATION_ID"] = paired_settings.organization_id or ""
        self.lifecycle = build_lifecycle(paired_settings)
        self.lifecycle.initialize()
        self.lifecycle.store.set_meta("cloud_url", paired_settings.cloud_url or "")
        self.lifecycle.store.set_meta("node_id", paired_settings.node_id or "")
        self.lifecycle.store.set_meta("node_token", paired_settings.cloud_token or "")
        self.lifecycle.store.set_meta("organization_id", paired_settings.organization_id or "")
        self.lifecycle.start()
        return None

    def add_camera(self, payload: LocalCameraPayload) -> dict:
        rtsp_url = (payload.rtsp_url or payload.source_uri or "").strip()
        if not payload.name.strip():
            raise ValueError("Informe o nome da câmera.")
        if not rtsp_url:
            raise ValueError("Informe a URL RTSP da câmera.")
        camera_id = _camera_id(payload.id)
        if self._is_cloud_camera(camera_id):
            raise CloudManagedCameraError()
        camera = NodeCameraConfig(
            id=camera_id,
            name=payload.name.strip(),
            rtsp_url=rtsp_url,
            enabled=payload.enabled,
            vision_enabled=bool(payload.vision_enabled),
        )
        self.lifecycle.store.save_local_camera(camera)
        self._upsert_camera_config(camera)
        return self._camera_response(camera)

    def update_camera(self, camera_id: str, payload: LocalCameraPayload) -> dict:
        existing = {camera.id: camera for camera in self.lifecycle.camera_manager.configs()}
        current = existing.get(camera_id)
        if current is None:
            raise ValueError("Camera not found.")
        if self._is_cloud_camera(camera_id):
            raise CloudManagedCameraError()
        rtsp_url = (payload.rtsp_url or payload.source_uri or current.rtsp_url).strip()
        camera = NodeCameraConfig(
            id=camera_id,
            name=payload.name.strip() if payload.name else current.name,
            rtsp_url=rtsp_url,
            enabled=payload.enabled,
            vision_enabled=current.vision_enabled if payload.vision_enabled is None else bool(payload.vision_enabled),
        )
        self.lifecycle.store.save_local_camera(camera)
        self._upsert_camera_config(camera)
        return self._camera_response(camera)

    def delete_camera(self, camera_id: str) -> None:
        if self._is_cloud_camera(camera_id):
            raise CloudManagedCameraError()
        self.lifecycle.store.delete_local_camera(camera_id)
        cameras = [camera for camera in self.lifecycle.camera_manager.configs() if camera.id != camera_id]
        self._apply_camera_configs(cameras)

    def _is_cloud_camera(self, camera_id: str) -> bool:
        return any(camera.id == camera_id for camera in self.lifecycle.store.get_cached_cloud_cameras())

    def _upsert_camera_config(self, camera: NodeCameraConfig) -> None:
        cameras = {item.id: item for item in self.lifecycle.camera_manager.configs()}
        cameras[camera.id] = camera
        self._apply_camera_configs(list(cameras.values()))

    def _apply_camera_configs(self, cameras: list[NodeCameraConfig]) -> None:
        # Only the affected camera workers restart; a full lifecycle restart
        # would drop every live stream and could leave RTSP sessions open.
        settings = replace(self.lifecycle.settings, cameras=tuple(cameras))
        self.lifecycle.settings = settings
        if self.lifecycle.config_sync is not None:
            self.lifecycle.config_sync.settings = settings
        self.lifecycle.camera_manager.apply_configs(cameras)

    def test_camera(self, payload: CameraTestPayload) -> dict:
        from campex_node.cameras.capture import test_rtsp_connection

        rtsp_url = (payload.rtsp_url or payload.source_uri or "").strip()
        if not rtsp_url:
            return {"ok": False, "success": False, "status": "OFFLINE", "error": "RTSP URL is required."}
        result = test_rtsp_connection(rtsp_url, settings=self.lifecycle.settings)
        return result

    def cameras(self) -> list[dict]:
        states = {state["id"]: state for state in self.lifecycle.camera_manager.summary()["cameras"]}
        return [
            self._camera_response(camera, states.get(camera.id))
            for camera in self.lifecycle.camera_manager.configs()
        ]

    def camera_health(self, camera_id: str) -> dict:
        for camera in self.cameras():
            if camera["id"] == camera_id:
                return camera["health"]
        raise ValueError("Camera not found.")

    def status(self) -> dict:
        settings = self.lifecycle.settings
        summary = self.lifecycle.camera_manager.summary()
        meta = self.lifecycle.store.meta_dict()
        uptime_seconds = 0
        if self.lifecycle.started_at is not None:
            uptime_seconds = int((datetime.now(timezone.utc) - self.lifecycle.started_at).total_seconds())
        resources = _system_resources()
        return {
            "node_id": self.lifecycle.node_id,
            "version": settings.version,
            "update": self.lifecycle.updates.status.as_dict() if self.lifecycle.updates else None,
            "cloud_url": settings.cloud_url,
            "organization_id": settings.organization_id,
            "paired": bool(settings.cloud_token and settings.node_id),
            "cloud_configured": bool(settings.cloud_url),
            "queue_size": self.lifecycle.store.outbound_queue_size(),
            "queue": self.lifecycle.store.outbound_summary(),
            "last_sync_at": meta.get("last_sync_at"),
            "last_heartbeat_at": meta.get("last_heartbeat_at"),
            "last_cloud_ok_at": meta.get("last_cloud_ok_at"),
            "last_cloud_error": sanitize_error_message(meta.get("last_cloud_error")),
            "uptime_seconds": uptime_seconds,
            "data_dir": str(settings.data_dir),
            "logs_dir": str(settings.data_dir / "logs"),
            "cpu_percent": resources["cpu_percent"],
            "ram_percent": resources["ram_percent"],
            "ram_used_mb": resources["ram_used_mb"],
            "cameras_total": summary["cameras_total"],
            "cameras_online": summary["cameras_online"],
            "cameras": summary["cameras"],
            "streaming": self.streaming_status(),
        }

    def streaming_status(self) -> dict:
        relay = self.lifecycle.camera_manager.relay
        if relay is None:
            return {"enabled": False, "status": "DISABLED", "error": None, "version": None, "live_mode": "mjpeg"}
        return relay.summary()

    def node_summary(self) -> dict:
        data = self.status()
        hostname = platform.node()
        return {
            "id": data["node_id"],
            "node_id": data["node_id"],
            "name": self.lifecycle.store.get_meta("node_name") or hostname or "CAMPEX Node",
            "hostname": hostname,
            "platform": platform.system().lower(),
            "version": self.lifecycle.settings.version,
            "status": "online",
            "paired": data["paired"],
            "cloud_configured": data["cloud_configured"],
            "last_seen_at": data["last_heartbeat_at"] or datetime.now(timezone.utc).isoformat(),
            "cameras_total": data["cameras_total"],
            "cameras_online": data["cameras_online"],
            "queue_size": data["queue_size"],
        }

    def node_telemetry(self) -> dict:
        data = self.status()
        cameras = [
            {
                "camera_id": camera["id"],
                "name": camera["name"],
                "online": camera["status"] == "ONLINE",
                "status": camera["status"],
                "frames_received": camera.get("frames_received", 0),
                "reconnect_attempts": camera.get("reconnect_attempts", 0),
                "consecutive_failures": camera.get("consecutive_failures", 0),
                "last_frame_at": camera.get("last_frame_at"),
            }
            for camera in data["cameras"]
        ]
        latest_metric_at = data["last_heartbeat_at"] or data["last_sync_at"]
        metrics = []
        if latest_metric_at:
            metrics.append(
                {
                    "created_at": latest_metric_at,
                    "cpu_percent": data["cpu_percent"],
                    "ram_percent": data["ram_percent"],
                    "queue_size": data["queue_size"],
                }
            )
        return {
            "node_id": data["node_id"],
            "summary": {
                "latest_metric_at": latest_metric_at,
                "metrics_count": len(metrics),
                "events_count": data["queue_size"],
            },
            "cameras": cameras,
            "events": [],
            "metrics": metrics,
        }

    def _camera_response(self, camera: NodeCameraConfig, state: dict | None = None) -> dict:
        if state is None:
            state = next(
                (item for item in self.lifecycle.camera_manager.summary()["cameras"] if item["id"] == camera.id),
                None,
            )
        status = (state or {}).get("status") or ("CONNECTING" if camera.enabled else "OFFLINE")
        health = {
            "status": status,
            "last_successful_frame": (state or {}).get("last_frame_at"),
            "last_connected_at": (state or {}).get("last_connected_at"),
            "last_error": sanitize_error_message((state or {}).get("last_error")),
            "reconnect_attempts": (state or {}).get("reconnect_attempts", 0),
            "frames_received": (state or {}).get("frames_received", 0),
            "consecutive_failures": (state or {}).get("consecutive_failures", 0),
            "resolution": None,
            "approximate_fps": None,
        }
        return {
            "ok": True,
            "id": camera.id,
            "name": camera.name,
            "source_type": "rtsp",
            "source_uri": camera.rtsp_url,
            "rtsp_url": camera.rtsp_url,
            "enabled": camera.enabled,
            "vision_enabled": camera.vision_enabled,
            "edge_vision": self.lifecycle.vision.status(camera.id) if self.lifecycle.vision else None,
            "status": status,
            "health": health,
        }

    def set_camera_vision(self, camera_id: str, enabled: bool) -> dict:
        cameras = {camera.id: camera for camera in self.lifecycle.camera_manager.configs()}
        current = cameras.get(camera_id)
        if current is None:
            raise ValueError("Camera not found.")
        if self._is_cloud_camera(camera_id):
            raise CloudManagedCameraError()
        updated = NodeCameraConfig(
            id=current.id,
            name=current.name,
            rtsp_url=current.rtsp_url,
            enabled=current.enabled,
            vision_enabled=enabled,
        )
        self.lifecycle.store.save_local_camera(updated)
        self._upsert_camera_config(updated)
        if self.lifecycle.vision is None:
            return {"camera_id": camera_id, "status": "DISABLED", "enabled": enabled}
        return self.lifecycle.vision.status(camera_id)

    def vision_status(self, camera_id: str) -> dict:
        if self.lifecycle.vision is None:
            return {
                "camera_id": camera_id,
                "status": "DISABLED",
                "enabled": False,
                "error": "Edge vision service is not running.",
            }
        return self.lifecycle.vision.status(camera_id)

    def vision_objects(self, camera_id: str) -> list[dict]:
        if self.lifecycle.vision is None:
            return []
        return self.lifecycle.vision.objects(camera_id)

    def sync_now(self) -> dict:
        if self.lifecycle.config_sync is None:
            return {"ok": False, "error": "Config sync service is not running."}
        cameras = self.lifecycle.config_sync.sync_once()
        if self.lifecycle.sync is not None:
            self.lifecycle.sync.sync_once()
        return {"ok": True, "cameras_loaded": len(cameras), **self.status()}

    def start_pairing(self, payload: PairingStartPayload) -> dict:
        from campex_node.main import build_lifecycle

        cloud_url = _normalize_cloud_api_url(payload.cloud_url)
        if not cloud_url:
            node_public_id = self.lifecycle.node_id
            pairing_code = _pairing_code()
            session_id = f"local-{uuid.uuid4().hex}"
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
            self.lifecycle.store.set_meta("pairing_session_id", session_id)
            self.lifecycle.store.set_meta("pairing_node_public_id", node_public_id)
            self.lifecycle.store.set_meta("pairing_code", pairing_code)
            self.lifecycle.store.set_meta("pairing_expires_at", expires_at.isoformat())
            self.lifecycle.store.set_meta("node_name", payload.node_name.strip() or "CAMPEX Node")
            self.lifecycle.store.set_meta("cloud_url", "")
            return {
                "ok": True,
                "mode": "local",
                "status": "pending",
                "session_id": session_id,
                "node_public_id": node_public_id,
                "pairing_code": pairing_code,
                "code": pairing_code,
                "expires_at": expires_at.isoformat(),
                "message": "Codigo local gerado. Sem CAMPEX Cloud configurada, ele fica aguardando autorizacao futura.",
                **self.status(),
            }

        self.lifecycle.store.set_meta("cloud_url", cloud_url)
        settings = replace(
            NodeSettings.from_env(),
            cloud_url=cloud_url,
        )
        client_lifecycle = build_lifecycle(settings)
        client_lifecycle.store.initialize()
        node_public_id = self.lifecycle.node_id
        result = client_lifecycle.cloud_client.start_pairing_session(
            node_public_id=node_public_id,
            node_name=payload.node_name.strip() or "CAMPEX Node",
            version=settings.version,
        )
        if not result.ok or not isinstance(result.data, dict):
            raise ValueError(result.error or "Nao foi possivel iniciar pareamento.")
        self.lifecycle.store.set_meta("pairing_session_id", result.data["session_id"])
        self.lifecycle.store.set_meta("pairing_node_public_id", node_public_id)
        os.environ["CAMPEX_NODE_CLOUD_URL"] = settings.cloud_url or ""
        return {"ok": True, **result.data, **self.status()}

    def pairing_status(self) -> dict:
        from campex_node.main import build_lifecycle

        session_id = self.lifecycle.store.get_meta("pairing_session_id")
        node_public_id = self.lifecycle.store.get_meta("pairing_node_public_id") or self.lifecycle.node_id
        if not session_id:
            return {"ok": False, "status": "missing"}
        if session_id.startswith("local-") or not (self.lifecycle.settings.cloud_url or self.lifecycle.store.get_meta("cloud_url")):
            expires_at = self.lifecycle.store.get_meta("pairing_expires_at")
            expired = False
            if expires_at:
                try:
                    expired = datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc)
                except ValueError:
                    expired = False
            return {
                "ok": True,
                "mode": "local",
                "status": "expired" if expired else "pending",
                "session_id": session_id,
                "node_public_id": node_public_id,
                "pairing_code": self.lifecycle.store.get_meta("pairing_code"),
                "code": self.lifecycle.store.get_meta("pairing_code"),
                "expires_at": expires_at,
                **self.status(),
            }
        settings = replace(
            NodeSettings.from_env(),
            cloud_url=self.lifecycle.settings.cloud_url or self.lifecycle.store.get_meta("cloud_url"),
        )
        client_lifecycle = build_lifecycle(settings)
        client_lifecycle.store.initialize()
        result = client_lifecycle.cloud_client.check_pairing_session(
            session_id=session_id,
            node_public_id=node_public_id,
        )
        if not result.ok or not isinstance(result.data, dict):
            return {"ok": False, "status": "pending", "error": result.error}
        data = result.data
        if data.get("status") == "authorized":
            self.lifecycle.stop()
            paired_settings = replace(
                settings,
                node_id=data["node_id"],
                cloud_token=data["node_token"],
                organization_id=data["organization_id"],
            )
            self.lifecycle = build_lifecycle(paired_settings)
            self.lifecycle.initialize()
            self.lifecycle.store.set_meta("cloud_url", paired_settings.cloud_url or "")
            self.lifecycle.store.set_meta("node_id", paired_settings.node_id or "")
            self.lifecycle.store.set_meta("node_token", paired_settings.cloud_token or "")
            self.lifecycle.store.set_meta("organization_id", paired_settings.organization_id or "")
            self.lifecycle.start()
        return {"ok": True, **data, **self.status()}

    def lookup_local_pairing_code(self, code: str) -> dict:
        stored_code = (self.lifecycle.store.get_meta("pairing_code") or "").strip().upper()
        requested_code = code.strip().upper()
        expires_at = self.lifecycle.store.get_meta("pairing_expires_at")
        if not stored_code or requested_code != stored_code:
            raise ValueError("Codigo de pareamento nao encontrado.")
        if expires_at:
            try:
                expired = datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc)
            except ValueError:
                expired = False
            if expired:
                raise ValueError("Codigo de pareamento expirado.")
        return {
            "ok": True,
            "code": stored_code,
            "node_id": self.lifecycle.node_id,
            "node_public_id": self.lifecycle.node_id,
            "node_name": self.lifecycle.store.get_meta("node_name") or "CAMPEX Node",
            "hostname": platform.node(),
            "platform": platform.system().lower(),
            "version": self.lifecycle.settings.version,
            "expires_at": expires_at,
        }

    def authorize_local_pairing_code(self, payload: PairingAuthorizePayload) -> dict:
        found = self.lookup_local_pairing_code(payload.code)
        if payload.node_name:
            self.lifecycle.store.set_meta("node_name", payload.node_name.strip())
        return {
            "ok": True,
            "mode": "local",
            "id": self.lifecycle.node_id,
            "node_id": self.lifecycle.node_id,
            "name": self.lifecycle.store.get_meta("node_name") or found["node_name"],
            "status": "online",
            "message": "Node local autorizado para teste sem CAMPEX Cloud.",
        }

    def diagnostics(self) -> dict:
        settings = self.lifecycle.settings
        return {
            "ok": True,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "node_id": self.lifecycle.node_id,
            "version": settings.version,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "data_dir": str(settings.data_dir),
            "database_path": str(settings.database_path),
            "database_exists": settings.database_path.exists(),
            "cloud_configured": bool(settings.cloud_url),
            "paired": bool(settings.node_id and settings.cloud_token),
            "queue_size": self.lifecycle.store.outbound_queue_size(),
            "queue": self.lifecycle.store.outbound_summary(),
            "last_sync_at": self.lifecycle.store.get_meta("last_sync_at"),
            "last_heartbeat_at": self.lifecycle.store.get_meta("last_heartbeat_at"),
            "last_cloud_ok_at": self.lifecycle.store.get_meta("last_cloud_ok_at"),
            "last_cloud_error": sanitize_error_message(self.lifecycle.store.get_meta("last_cloud_error")),
            "resources": _system_resources(),
            "services": {
                "camera_manager": True,
                "config_sync": self.lifecycle.config_sync is not None,
                "sync": self.lifecycle.sync is not None,
                "telemetry": self.lifecycle.telemetry is not None,
                "heartbeat": self.lifecycle.heartbeat is not None,
                "edge_vision": self.lifecycle.vision is not None,
            },
            "cameras": self.lifecycle.camera_manager.summary(),
        }

    def _build_from_persisted_connection(self):
        from campex_node.main import build_lifecycle

        lifecycle = build_lifecycle()
        lifecycle.settings.data_dir.mkdir(parents=True, exist_ok=True)
        lifecycle.store.initialize()
        meta = lifecycle.store.meta_dict()
        camera_configs = {
            camera.id: camera for camera in lifecycle.store.get_cached_cloud_cameras()
        }
        for camera in lifecycle.store.get_local_cameras():
            # Cameras that came from the Cloud keep the Cloud settings.
            camera_configs.setdefault(camera.id, camera)
        cached_cameras = tuple(camera_configs.values())
        if not meta.get("cloud_url") and not lifecycle.settings.cloud_url:
            if cached_cameras:
                settings = replace(lifecycle.settings, cameras=cached_cameras)
                return build_lifecycle(settings)
            return lifecycle
        settings = replace(
            lifecycle.settings,
            cloud_url=_normalize_cloud_api_url(lifecycle.settings.cloud_url or meta.get("cloud_url")),
            node_id=lifecycle.settings.node_id or meta.get("node_id"),
            cloud_token=lifecycle.settings.cloud_token or meta.get("node_token"),
            organization_id=lifecycle.settings.organization_id or meta.get("organization_id"),
            cameras=cached_cameras,
        )
        os.environ["CAMPEX_NODE_CLOUD_URL"] = settings.cloud_url or ""
        os.environ["CAMPEX_NODE_ID"] = settings.node_id or ""
        os.environ["CAMPEX_NODE_TOKEN"] = settings.cloud_token or ""
        os.environ["CAMPEX_NODE_ORGANIZATION_ID"] = settings.organization_id or ""
        return build_lifecycle(settings)


def create_app() -> FastAPI:
    runtime = LocalNodeRuntime()
    app = FastAPI(title="CAMPEX Node Local", version="0.2.0")
    allowed_frontend_origins = _frontend_origins()
    app.add_middleware(
        LocalNetworkCORSMiddleware,
        local_runtime=True,
        allow_origins=allowed_frontend_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def local_cors_fallback(request, call_next):
        origin = request.headers.get("origin", "")
        if request.method == "OPTIONS" and _is_allowed_frontend_origin(origin):
            response = Response(status_code=204)
        else:
            response = await call_next(request)
        if _is_allowed_frontend_origin(origin):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Methods"] = "GET,POST,PATCH,DELETE,OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = request.headers.get(
                "access-control-request-headers",
                "authorization,content-type,accept,x-campex-token",
            )
            if request.headers.get("access-control-request-private-network") == "true":
                response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        # The panel is reachable from the factory network: every API call
        # needs a signed-in user, except from this computer itself (the Node
        # window and local tools) and the few calls that come before login.
        path = request.url.path
        if (
            path.startswith("/api/")
            and request.method != "OPTIONS"
            and path not in PUBLIC_API_PATHS
            and not _is_local_request(request)
            and _session_user(runtime, request) is None
        ):
            return JSONResponse({"detail": "Faça login para continuar."}, status_code=401)
        return await call_next(request)

    app.state.runtime = runtime
    frontend_dir = Path(__file__).resolve().parents[1] / "frontend"
    assets_dir = frontend_dir / "assets"
    static_dirs = {
        "/assets": assets_dir,
        "/css": frontend_dir / "css",
        "/js": frontend_dir / "js",
        "/vendor": frontend_dir / "vendor",
    }
    for route, directory in static_dirs.items():
        if directory.exists():
            app.mount(route, StaticFiles(directory=str(directory)), name=route.strip("/"))

    @app.on_event("shutdown")
    def shutdown() -> None:
        runtime.lifecycle.stop()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return NODE_HTML

    @app.get("/app", response_class=HTMLResponse)
    def campex_app() -> str:
        index_path = frontend_dir / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=404, detail="CAMPEX frontend not found.")
        return index_path.read_text(encoding="utf-8")

    @app.get("/config.js")
    def frontend_config() -> Response:
        return Response(
            content=(
                # Same origin: the Node may not be on its default port.
                'window.CAMPEX_API_BASE_URL = `${window.location.origin}/api`;\n'
                # Served by the Node: sign in against it, also from the network.
                "window.CAMPEX_NODE_PANEL = true;\n"
                f'window.CAMPEX_NODE_DOWNLOAD_URL = "{_node_download_url()}";\n'
            ),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/media-worker.js")
    def media_worker() -> Response:
        worker_path = frontend_dir / "media-worker.js"
        if not worker_path.exists():
            raise HTTPException(status_code=404, detail="CAMPEX media worker not found.")
        return Response(
            content=worker_path.read_text(encoding="utf-8"),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/status")
    def status(request: Request) -> dict:
        return {**runtime.status(), "panel_urls": _panel_urls(request)}

    # Accounts: the same contract as the CAMPEX Cloud /auth API.

    @app.get("/api/auth/setup")
    def auth_setup(request: Request) -> dict:
        return {
            "needs_setup": runtime.lifecycle.auth_store.needs_setup(),
            "local": _is_local_request(request),
        }

    @app.post("/api/auth/register", status_code=201)
    def auth_register(payload: RegisterPayload, request: Request, response: Response) -> dict:
        store = runtime.lifecycle.auth_store
        try:
            if store.needs_setup():
                # The first account is the administrator, created at the Node's
                # own computer so nobody on the network can claim it.
                if not _is_local_request(request):
                    raise AuthError("Crie a primeira conta no computador onde o CAMPEX Node está instalado.", 403)
                user = store.create_user(name=payload.name, email=payload.email, password=payload.password, role="admin")
                session = store.create_session(user, request.headers.get("user-agent"))
                _set_session_cookie(request, response, session.token)
                return session.as_dict()
            _require_admin(runtime, request)
            user = store.create_user(
                name=payload.name, email=payload.email, password=payload.password, role=payload.role or "operator"
            )
        except AuthError as error:
            raise HTTPException(status_code=error.status_code, detail=error.message) from error
        return {"user": user.as_dict()}

    @app.post("/api/auth/login")
    def auth_login(payload: LoginPayload, request: Request, response: Response) -> dict:
        try:
            session = runtime.lifecycle.auth_store.login(
                email=payload.email, password=payload.password, user_agent=request.headers.get("user-agent")
            )
        except AuthError as error:
            raise HTTPException(status_code=error.status_code, detail=error.message) from error
        _set_session_cookie(request, response, session.token)
        return session.as_dict()

    @app.get("/api/auth/me")
    def auth_me(request: Request) -> dict:
        user = _session_user(runtime, request)
        if user is None:
            raise HTTPException(status_code=401, detail="Sessão expirada. Entre novamente.")
        return {"user": user.as_dict()}

    @app.post("/api/auth/logout", status_code=204, response_class=Response, response_model=None)
    def auth_logout(request: Request) -> Response:
        runtime.lifecycle.auth_store.logout(_session_token(request))
        response = Response(status_code=204)
        response.delete_cookie(_cookie_name(request), path="/")
        return response

    @app.get("/api/auth/users")
    def auth_users() -> list[dict]:
        return [user.as_dict() for user in runtime.lifecycle.auth_store.list_users()]

    @app.patch("/api/auth/users/{user_id}")
    def auth_update_user(user_id: str, payload: UserUpdatePayload, request: Request) -> dict:
        _require_admin(runtime, request)
        try:
            user = runtime.lifecycle.auth_store.update_user(user_id, role=payload.role, password=payload.password)
        except AuthError as error:
            raise HTTPException(status_code=error.status_code, detail=error.message) from error
        return {"user": user.as_dict()}

    @app.delete("/api/auth/users/{user_id}", status_code=204, response_class=Response, response_model=None)
    def auth_delete_user(user_id: str, request: Request) -> Response:
        admin = _require_admin(runtime, request)
        if admin is not None and admin.id == user_id:
            raise HTTPException(status_code=400, detail="Você não pode excluir a própria conta.")
        try:
            runtime.lifecycle.auth_store.delete_user(user_id)
        except AuthError as error:
            raise HTTPException(status_code=error.status_code, detail=error.message) from error
        return Response(status_code=204)

    @app.post("/api/auth/password", status_code=204, response_class=Response, response_model=None)
    def auth_change_password(payload: PasswordChangePayload, request: Request) -> Response:
        user = _session_user(runtime, request)
        if user is None:
            raise HTTPException(status_code=401, detail="Sessão expirada. Entre novamente.")
        try:
            runtime.lifecycle.auth_store.change_password(user, payload.current_password, payload.new_password)
        except AuthError as error:
            raise HTTPException(status_code=error.status_code, detail=error.message) from error
        response = Response(status_code=204)
        response.delete_cookie(_cookie_name(request), path="/")
        return response

    # Backups of the Node database (zones, shifts, events, history, users).

    @app.get("/api/backup/status")
    def backup_status() -> dict:
        return runtime.lifecycle.backups.status()

    @app.get("/api/backup")
    def download_backup(request: Request):
        _require_admin(runtime, request)
        settings = runtime.lifecycle.settings
        name = backup_filename()
        path = backup_database(settings.database_path, settings.backups_path / "downloads" / name)
        return FileResponse(path, media_type="application/vnd.sqlite3", filename=name)

    @app.post("/api/backup/restore")
    async def restore_backup(request: Request) -> dict:
        _require_admin(runtime, request)
        settings = runtime.lifecycle.settings
        upload = settings.backups_path / "uploads" / f"restore-{uuid.uuid4().hex}.sqlite3"
        upload.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with upload.open("wb") as handle:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_BACKUP_BYTES:
                    handle.close()
                    upload.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail="Arquivo de backup grande demais.")
                handle.write(chunk)
        try:
            safety = runtime.restore_database(upload)
        except ValueError as exc:
            upload.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "previous_database": safety.name}

    @app.get("/api/health")
    def health() -> dict:
        data = runtime.status()
        return {
            "ok": True,
            "service": "CAMPEX Node",
            "version": runtime.lifecycle.settings.version,
            "node_id": data["node_id"],
            "status": "online",
            "paired": data["paired"],
            "cameras_total": data["cameras_total"],
            "cameras_online": data["cameras_online"],
            "queue_size": data["queue_size"],
        }

    @app.post("/api/connect")
    def connect(payload: CloudConnectionPayload) -> dict:
        try:
            result = runtime.reconfigure(payload)
            if result and result.get("already_paired"):
                return {
                    "ok": True,
                    "message": "Este Node já está pareado. Use Sincronizar agora para carregar câmeras.",
                    **runtime.status(),
                }
            return {"ok": True, **runtime.status()}
        except ValueError as exc:
            return {"ok": False, "error": str(exc), **runtime.status()}

    @app.post("/api/pairing/start")
    def start_pairing(payload: PairingStartPayload) -> dict:
        try:
            return runtime.start_pairing(payload)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), **runtime.status()}

    @app.get("/api/pairing/status")
    def pairing_status() -> dict:
        return runtime.pairing_status()

    @app.get("/api/nodes")
    def list_nodes() -> list[dict]:
        return [runtime.node_summary()]

    @app.get("/api/nodes/{node_id}/telemetry")
    def node_telemetry(node_id: str) -> dict:
        if node_id != runtime.lifecycle.node_id:
            raise HTTPException(status_code=404, detail="Node not found.")
        return runtime.node_telemetry()

    @app.post("/api/nodes/pair/request")
    def request_node_pairing_code() -> dict:
        try:
            result = runtime.start_pairing(PairingStartPayload(cloud_url=None, node_name="CAMPEX Node"))
            return {
                "ok": True,
                "code": result["pairing_code"],
                "pairing_code": result["pairing_code"],
                "expires_at": result["expires_at"],
                "node_id": result["node_id"],
                "node_public_id": result["node_public_id"],
                "mode": result.get("mode", "local"),
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/nodes/pairing/lookup")
    def lookup_node_pairing_code(payload: PairingLookupPayload) -> dict:
        try:
            return runtime.lookup_local_pairing_code(payload.code)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/nodes/pairing/authorize")
    def authorize_node_pairing_code(payload: PairingAuthorizePayload) -> dict:
        try:
            return runtime.authorize_local_pairing_code(payload)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


    @app.get("/api/cameras/{camera_id}/snapshot")
    def camera_snapshot(camera_id: str, overlay: bool = Query(False)):
        frame, _frame_at = runtime.lifecycle.camera_manager.latest_frame(camera_id)
        if frame is None:
            encoded = _encode_jpeg(_blank_frame("Aguardando imagem da camera"), quality=72)
            return Response(
                content=encoded,
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store", "X-CAMPEX-Frame": "waiting"},
            )
        if overlay and runtime.lifecycle.vision is not None:
            frame = runtime.lifecycle.vision.render_overlay(camera_id, frame.copy())
        encoded = _encode_jpeg(frame, quality=72)
        return Response(
            content=encoded,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store", "X-CAMPEX-Frame": "live"},
        )

    @app.get("/api/cameras/{camera_id}/stream")
    def camera_stream(camera_id: str, overlay: bool = Query(False)):
        return StreamingResponse(
            _mjpeg_frames(runtime, camera_id, overlay=overlay),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store"},
        )

    @app.websocket("/api/cameras/{camera_id}/live")
    async def camera_live(websocket: WebSocket, camera_id: str) -> None:
        # The HTTP login middleware does not see WebSockets: check access here.
        if not _websocket_allowed(runtime, websocket):
            await websocket.close(code=1008)
            return
        manager = runtime.lifecycle.camera_manager
        camera = next((item for item in manager.configs() if item.id == camera_id), None)
        upstream = manager.relay.live_upstream(camera) if camera is not None and manager.relay is not None else None
        if upstream is None:
            await websocket.close(code=1013, reason="live_unavailable")
            return
        await websocket.accept()
        await _proxy_live(websocket, *upstream)

    @app.post("/api/cameras/{camera_id}/vision/start")
    def start_camera_vision(camera_id: str) -> dict:
        try:
            return runtime.set_camera_vision(camera_id, True)
        except CloudManagedCameraError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/cameras/{camera_id}/vision/stop")
    def stop_camera_vision(camera_id: str) -> dict:
        try:
            return runtime.set_camera_vision(camera_id, False)
        except CloudManagedCameraError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/cameras/{camera_id}/vision/restart")
    def restart_camera_vision(camera_id: str) -> dict:
        try:
            runtime.set_camera_vision(camera_id, False)
            return runtime.set_camera_vision(camera_id, True)
        except CloudManagedCameraError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/cameras/{camera_id}/vision/status")
    def camera_vision_status(camera_id: str) -> dict:
        return runtime.vision_status(camera_id)

    @app.get("/api/cameras/{camera_id}/vision/objects")
    def camera_vision_objects(camera_id: str) -> list[dict]:
        return runtime.vision_objects(camera_id)

    @app.post("/api/sync")
    def sync() -> dict:
        return runtime.sync_now()

    @app.post("/api/cameras/test")
    def test_camera(payload: CameraTestPayload) -> dict:
        return runtime.test_camera(payload)

    @app.post("/api/cameras/test-source")
    def test_camera_source(payload: CameraTestPayload) -> dict:
        return runtime.test_camera(payload)

    @app.get("/api/cameras")
    def list_cameras() -> list[dict]:
        return runtime.cameras()

    @app.post("/api/cameras")
    def add_camera(payload: LocalCameraPayload):
        try:
            return runtime.add_camera(payload)
        except ValueError as exc:
            # 400 para o painel web não tratar a recusa como sucesso; "error"
            # continua no corpo para a tela local do Node.
            status_code = 409 if isinstance(exc, CloudManagedCameraError) else 400
            return JSONResponse(
                status_code=status_code,
                content={"ok": False, "error": str(exc), "detail": str(exc)},
            )

    @app.patch("/api/cameras/{camera_id}")
    def update_camera(camera_id: str, payload: LocalCameraPayload) -> dict:
        try:
            return runtime.update_camera(camera_id, payload)
        except CloudManagedCameraError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/cameras/{camera_id}", status_code=204, response_class=Response, response_model=None)
    def delete_camera(camera_id: str) -> Response:
        try:
            runtime.delete_camera(camera_id)
        except CloudManagedCameraError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Response(status_code=204)

    @app.get("/api/cameras/{camera_id}/health")
    def camera_health(camera_id: str) -> dict:
        try:
            return runtime.camera_health(camera_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/zones")
    def list_zones(camera_id: str | None = None) -> list[dict]:
        store = runtime.lifecycle.events_store
        settings = store.zone_settings(camera_id)
        return [{**zone.as_dict(), "settings": settings.get(zone.id, {})} for zone in store.list_zones(camera_id)]

    @app.post("/api/zones", status_code=201)
    def create_zone(payload: ZoneCreatePayload) -> dict:
        if not any(camera.id == payload.camera_id for camera in runtime.lifecycle.camera_manager.configs()):
            raise HTTPException(status_code=404, detail="Camera not found.")
        try:
            zone = runtime.lifecycle.events_store.create_zone(
                camera_id=payload.camera_id,
                name=payload.name,
                zone_type=payload.type,
                points=payload.points,
                enabled=payload.enabled,
                settings=payload.settings,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return runtime.lifecycle.events_store.zone_dict(zone)

    @app.patch("/api/zones/{zone_id}")
    def update_zone(zone_id: str, payload: ZoneUpdatePayload) -> dict:
        try:
            zone = runtime.lifecycle.events_store.update_zone(zone_id, payload.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if zone is None:
            raise HTTPException(status_code=404, detail="Zone not found.")
        return runtime.lifecycle.events_store.zone_dict(zone)

    @app.delete("/api/zones/{zone_id}", status_code=204, response_class=Response, response_model=None)
    def delete_zone(zone_id: str) -> Response:
        if not runtime.lifecycle.events_store.delete_zone(zone_id):
            raise HTTPException(status_code=404, detail="Zone not found.")
        return Response(status_code=204)

    @app.get("/api/events")
    def list_events(
        status: str | None = None,
        camera_id: str | None = None,
        event_type: str | None = None,
        limit: int = Query(200, ge=1, le=1000),
    ) -> list[dict]:
        events = runtime.lifecycle.events_store.list_events(
            status=status, camera_id=camera_id, event_type=event_type, limit=limit
        )
        return [event.as_dict() for event in events]

    @app.get("/api/events/{event_id}")
    def get_event(event_id: str) -> dict:
        event = runtime.lifecycle.events_store.get(event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        return event.as_dict()

    @app.patch("/api/events/{event_id}")
    def update_event(event_id: str, payload: EventUpdatePayload) -> dict:
        event = runtime.lifecycle.events_store.update_status(event_id, payload.status)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        return event.as_dict()

    @app.delete("/api/events/{event_id}", status_code=204, response_class=Response, response_model=None)
    def delete_event(event_id: str) -> Response:
        if not runtime.lifecycle.events_store.delete_event(event_id):
            raise HTTPException(status_code=404, detail="Event not found.")
        return Response(status_code=204)

    @app.get("/api/events/{event_id}/evidence")
    def event_evidence(event_id: str, variant: str = Query("overlay", pattern="^(overlay|snapshot|metadata|clip)$")):
        event = runtime.lifecycle.events_store.get(event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        key, media_type = EVIDENCE_VARIANTS[variant]
        stored = event.metadata.get(key)
        if not stored:
            raise HTTPException(status_code=404, detail="Evidence not found.")
        file_path = _evidence_file(runtime.lifecycle.settings.data_dir, stored)
        if file_path is None:
            raise HTTPException(status_code=404, detail="Evidence file not found.")
        if media_type is None:
            media_type = "video/webm" if file_path.suffix == ".webm" else "video/mp4"
        return FileResponse(file_path, media_type=media_type)

    @app.get("/api/diagnostics")
    def diagnostics() -> dict:
        return runtime.diagnostics()

    # Factory configuration

    @app.get("/api/factory/settings")
    def factory_settings() -> dict:
        return runtime.lifecycle.factory_store.settings()

    @app.patch("/api/factory/settings")
    def update_factory_settings(payload: FactorySettingsPayload) -> dict:
        try:
            return runtime.lifecycle.factory_store.update_settings(payload.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/factory/shifts")
    def list_shifts() -> list[dict]:
        return [shift.as_dict() for shift in runtime.lifecycle.factory_store.list_shifts()]

    @app.post("/api/factory/shifts", status_code=201)
    def create_shift(payload: ShiftPayload) -> dict:
        try:
            return runtime.lifecycle.factory_store.create_shift(payload.model_dump(exclude_none=True)).as_dict()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.patch("/api/factory/shifts/{shift_id}")
    def update_shift(shift_id: str, payload: ShiftPayload) -> dict:
        try:
            shift = runtime.lifecycle.factory_store.update_shift(shift_id, payload.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if shift is None:
            raise HTTPException(status_code=404, detail="Shift not found.")
        return shift.as_dict()

    @app.delete("/api/factory/shifts/{shift_id}", status_code=204, response_class=Response, response_model=None)
    def delete_shift(shift_id: str) -> Response:
        if not runtime.lifecycle.factory_store.delete_shift(shift_id):
            raise HTTPException(status_code=404, detail="Shift not found.")
        return Response(status_code=204)

    @app.get("/api/factory/live")
    def factory_live(camera_id: str | None = None) -> dict:
        """What the monitors see right now: machine states and zone occupancy."""
        lifecycle = runtime.lifecycle
        zones = {zone.id: zone for zone in lifecycle.events_store.list_zones(camera_id)}
        now = datetime.now(timezone.utc)
        shift = lifecycle.factory_store.shift_at(now)

        def named(items: list[dict]) -> list[dict]:
            return [{**item, "name": zones[item["zone_id"]].name} for item in items if item["zone_id"] in zones]

        # Every machine is listed: one the monitor has no frames for is UNKNOWN.
        sampled = {item["zone_id"]: item for item in lifecycle.machines.snapshot(camera_id)} if lifecycle.machines else {}
        machines = [
            {
                **sampled.get(zone.id, {"zone_id": zone.id, "camera_id": zone.camera_id, "state": "UNKNOWN", "since": None}),
                "name": zone.name,
            }
            for zone in zones.values()
            if zone.type == "machine" and zone.enabled
        ]
        return {
            "at": now.isoformat(),
            "shift": shift.as_dict() if shift else None,
            "working_time": lifecycle.factory_store.is_working_time(now),
            "machines": machines,
            "occupancy": named(lifecycle.occupancy.snapshot(camera_id)) if lifecycle.occupancy else [],
        }

    @app.post("/api/production/counts", status_code=201)
    def add_production_count(payload: ProductionCountPayload) -> dict:
        zone = runtime.lifecycle.events_store.get_zone(payload.zone_id)
        if zone is None or zone.type not in ("machine", "line"):
            raise HTTPException(status_code=404, detail="Machine or counting line not found.")
        at = payload.at or datetime.now(timezone.utc)
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        runtime.lifecycle.activity_store.add_count(zone.id, at, "external", payload.count)
        return {"ok": True, "zone_id": zone.id, "count": payload.count, "at": at.isoformat()}

    # Analytics: every endpoint takes period=today|yesterday|week|last_week|month|last_month
    # or an explicit start/end (ISO 8601).

    def _range(period: str | None, start: str | None, end: str | None) -> tuple[datetime, datetime]:
        try:
            return resolve_period(runtime.lifecycle.factory_store, period=period, start=start, end=end)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _day(value: str | None) -> date:
        if not value:
            return runtime.lifecycle.factory_store.local_date(datetime.now(timezone.utc))
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD.") from exc

    @app.get("/api/analytics/machines")
    def analytics_machines(period: str | None = None, start: str | None = None, end: str | None = None) -> dict:
        return runtime.lifecycle.analytics.machines(*_range(period, start, end))

    @app.get("/api/analytics/stops")
    def analytics_stops(
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        zone_id: str | None = None,
        min_minutes: float = Query(0, ge=0),
    ) -> list[dict]:
        return runtime.lifecycle.analytics.stops(*_range(period, start, end), min_seconds=min_minutes * 60, zone_id=zone_id)

    @app.get("/api/analytics/machines/{zone_id}/speed")
    def analytics_speed(zone_id: str, period: str | None = None, start: str | None = None, end: str | None = None) -> dict:
        return runtime.lifecycle.analytics.speed(zone_id, *_range(period, start, end))

    @app.get("/api/analytics/hourly")
    def analytics_hourly(
        period: str | None = None, start: str | None = None, end: str | None = None, zone_id: str | None = None
    ) -> dict:
        return runtime.lifecycle.analytics.hourly(*_range(period, start, end), zone_id=zone_id)

    @app.get("/api/analytics/shifts")
    def analytics_shifts(period: str | None = None, start: str | None = None, end: str | None = None) -> dict:
        return runtime.lifecycle.analytics.shifts(*_range(period, start, end))

    @app.get("/api/analytics/occupancy")
    def analytics_occupancy(
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        zone_id: str | None = None,
        min_vacant_minutes: float = Query(1, ge=0),
    ) -> list[dict]:
        return runtime.lifecycle.analytics.occupancy(
            *_range(period, start, end), zone_id=zone_id, min_vacant_seconds=min_vacant_minutes * 60
        )

    @app.get("/api/analytics/first-arrival")
    def analytics_first_arrival(date: str | None = None, zone_id: list[str] | None = Query(None)) -> dict:
        return runtime.lifecycle.analytics.first_arrival(_day(date), zone_id)

    @app.get("/api/analytics/breaks")
    def analytics_breaks(date: str | None = None) -> list[dict]:
        return runtime.lifecycle.analytics.breaks(_day(date))

    @app.get("/api/analytics/after-hours")
    def analytics_after_hours(period: str | None = None, start: str | None = None, end: str | None = None) -> dict:
        return runtime.lifecycle.analytics.after_hours(*_range(period, start, end))

    @app.get("/api/analytics/docks")
    def analytics_docks(period: str | None = None, start: str | None = None, end: str | None = None) -> dict:
        return runtime.lifecycle.analytics.docks(*_range(period, start, end))

    @app.get("/api/analytics/lines")
    def analytics_lines(period: str | None = None, start: str | None = None, end: str | None = None) -> list[dict]:
        return runtime.lifecycle.analytics.lines(*_range(period, start, end))

    @app.get("/api/analytics/summary")
    def analytics_summary(period: str | None = None, start: str | None = None, end: str | None = None) -> dict:
        return runtime.lifecycle.analytics.summary(*_range(period, start, end))

    # Recordings

    @app.get("/api/recordings")
    def list_recordings(
        camera_id: str | None = None, period: str | None = None, start: str | None = None, end: str | None = None
    ) -> list[dict]:
        begin, finish = _range(period, start, end)
        return [segment.as_dict() for segment in runtime.lifecycle.recording_store.list(camera_id, begin, finish)]

    @app.get("/api/recordings/status")
    def recordings_status() -> dict:
        if runtime.lifecycle.recording is None:
            return {"enabled": False, "cameras": {}}
        return runtime.lifecycle.recording.status()

    @app.get("/api/recordings/{segment_id}/file")
    def recording_file(segment_id: str):
        segment = runtime.lifecycle.recording_store.get(segment_id)
        root = runtime.lifecycle.settings.recordings_path.resolve()
        if segment is None or root not in segment.path.resolve().parents or not segment.path.is_file():
            raise HTTPException(status_code=404, detail="Recording not found.")
        return FileResponse(segment.path, media_type=segment.media_type)

    @app.get("/api/cameras/{camera_id}/recording")
    def camera_recording_at(camera_id: str, at: datetime) -> dict:
        """The segment that holds the camera's video at a moment, and where in it to seek."""
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        found = runtime.lifecycle.recording_store.at(camera_id, at)
        if found is None:
            raise HTTPException(status_code=404, detail="No recording at that time.")
        segment, offset = found
        return {
            "segment": segment.as_dict(),
            "offset_seconds": round(offset, 2),
            "url": f"/api/recordings/{segment.id}/file#t={offset:.1f}",
        }

    return app


# Cookies are shared by every port of a host: the port in the name keeps two
# Nodes (or a Node and a dev server) on one computer from mixing sessions.
SESSION_COOKIE_PREFIX = "campex_session_"
SESSION_HEADER = "x-campex-session"
PUBLIC_API_PATHS = frozenset(
    {"/api/health", "/api/status", "/api/auth/setup", "/api/auth/login", "/api/auth/register", "/api/auth/logout"}
)
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
LIVE_MESSAGE_TYPES = frozenset({"mse", "mp4"})
# One fMP4 fragment; a 4K keyframe stays well below this.
LIVE_MAX_MESSAGE_BYTES = 16 * 1024**2
MAX_BACKUP_BYTES = 4 * 1024**3


def _is_local_request(request: HTTPConnection) -> bool:
    """A request from the Node's own computer, addressed to it by a loopback name.

    Checking the Host header too keeps a web page that rebinds its domain to
    127.0.0.1 from passing as local.
    """
    client = request.client.host if request.client else ""
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    return client in LOOPBACK_HOSTS and host in LOOPBACK_HOSTS


def _session_token(request: HTTPConnection) -> str | None:
    # The panel sends the header; images and videos carry the cookie.
    return request.headers.get(SESSION_HEADER) or request.cookies.get(_cookie_name(request)) or None


def _session_user(runtime: "LocalNodeRuntime", request: HTTPConnection) -> NodeUser | None:
    return runtime.lifecycle.auth_store.authenticate(_session_token(request))


def _require_admin(runtime: "LocalNodeRuntime", request: Request) -> NodeUser | None:
    """The signed-in administrator; None when the call comes from the Node's computer."""
    user = _session_user(runtime, request)
    if user is not None and user.is_admin:
        return user
    if user is None and _is_local_request(request):
        return None
    raise HTTPException(status_code=403, detail="Apenas administradores podem fazer isso.")


def _cookie_name(request: HTTPConnection) -> str:
    return f"{SESSION_COOKIE_PREFIX}{request.url.port or 80}"


def _set_session_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        _cookie_name(request),
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="strict",
        path="/",
    )


def _panel_urls(request: Request) -> list[str]:
    """Addresses where other computers on the network open this panel."""
    import socket

    port = request.url.port or 80
    addresses: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            # No packet is sent: this only asks which interface routes outwards.
            probe.connect(("10.255.255.255", 1))
            addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass
    return [f"http://{address}:{port}/app" for address in sorted(addresses) if not address.startswith("127.")]


def _evidence_file(data_dir: Path, stored: str) -> Path | None:
    raw = Path(stored)
    file_path = (raw if raw.is_absolute() else BACKEND_ROOT_DIR / raw).resolve()
    evidence_root = (data_dir / "evidence").resolve()
    if evidence_root not in file_path.parents or not file_path.is_file():
        return None
    return file_path


def _websocket_allowed(runtime: "LocalNodeRuntime", websocket: WebSocket) -> bool:
    # Browsers apply no CORS to WebSockets: without the origin check any web
    # page open on a viewer's computer could read the camera's video.
    origin = websocket.headers.get("origin")
    if origin:
        same_origin = urlsplit(origin).netloc.lower() == (websocket.headers.get("host") or "").lower()
        if not same_origin and not _is_allowed_frontend_origin(origin):
            return False
    return _is_local_request(websocket) or _session_user(runtime, websocket) is not None


async def _proxy_live(websocket: WebSocket, url: str, headers: dict[str, str]) -> None:
    """Relays go2rtc's MSE/MP4 WebSocket, which listens only on this computer."""
    from websockets.asyncio.client import connect
    from websockets.exceptions import WebSocketException

    async def to_browser(upstream) -> None:
        async for message in upstream:
            if isinstance(message, bytes):
                await websocket.send_bytes(message)
            else:
                await websocket.send_text(message)

    async def to_go2rtc(upstream) -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            try:
                kind = json.loads(message.get("text") or "").get("type")
            except (ValueError, AttributeError):
                continue
            # Only playback requests: go2rtc's other messages (WebRTC
            # signalling, HLS) stay out of reach.
            if kind in LIVE_MESSAGE_TYPES:
                await upstream.send(message["text"])

    try:
        async with connect(url, additional_headers=headers, max_size=LIVE_MAX_MESSAGE_BYTES, open_timeout=5) as upstream:
            tasks = [asyncio.create_task(to_browser(upstream)), asyncio.create_task(to_go2rtc(upstream))]
            _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    except (OSError, WebSocketException, asyncio.TimeoutError):
        pass
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass  # already closed by the browser


def _mjpeg_frames(runtime: LocalNodeRuntime, camera_id: str, *, overlay: bool = False):
    last_payload: bytes | None = None
    while True:
        frame, _frame_at = runtime.lifecycle.camera_manager.latest_frame(camera_id)
        if frame is not None:
            if overlay and runtime.lifecycle.vision is not None:
                frame = runtime.lifecycle.vision.render_overlay(camera_id, frame.copy())
            last_payload = _encode_jpeg(frame, quality=68)
        payload = last_payload or _encode_jpeg(_blank_frame("Aguardando imagem da camera"), quality=68)
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Cache-Control: no-store\r\n\r\n"
            + payload
            + b"\r\n"
        )
        time.sleep(0.15)


def _encode_jpeg(frame, *, quality: int) -> bytes:
    import cv2

    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise HTTPException(status_code=500, detail="Nao foi possivel codificar o frame.")
    return encoded.tobytes()


def _blank_frame(message: str):
    import cv2
    import numpy as np

    frame = np.zeros((480, 854, 3), dtype=np.uint8)
    cv2.putText(
        frame,
        message,
        (32, 240),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (220, 220, 220),
        2,
        cv2.LINE_AA,
    )
    return frame


def _camera_id(value: str | None) -> str:
    raw_value = (value or "").strip()
    if raw_value:
        safe_value = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in raw_value)
        return safe_value[:120]
    return f"local_{uuid.uuid4().hex[:12]}"


def _pairing_code() -> str:
    raw_value = uuid.uuid4().hex[:8].upper()
    return f"CXP-{raw_value[:4]}-{raw_value[4:]}"


def _frontend_origins() -> list[str]:
    configured = [
        item.strip()
        for item in os.getenv("CAMPEX_FRONTEND_ORIGINS", "").split(",")
        if item.strip()
    ]
    defaults = [
        "https://campexfront.vercel.app",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:5174",
        "http://localhost:5174",
        "http://127.0.0.1:5500",
        "http://localhost:5500",
    ]
    return list(dict.fromkeys(configured + defaults))


def _normalize_cloud_api_url(value: str | None) -> str:
    raw_value = (value or "").strip().rstrip("/")
    if not raw_value:
        return ""
    if raw_value.endswith("/api"):
        return f"{raw_value}/v1"
    if raw_value.endswith("/api/v1"):
        return raw_value
    return f"{raw_value}/api/v1"


def _node_download_url() -> str:
    return os.getenv(
        "CAMPEX_NODE_DOWNLOAD_URL",
        "https://github.com/felipefde08-hub/CAMPEX-NOVA-PRO/releases/latest/download/CampexNode-windows.zip",
    )


def _is_allowed_frontend_origin(origin: str) -> bool:
    return origin in _frontend_origins()


def _system_resources() -> dict:
    try:
        import psutil

        memory = psutil.virtual_memory()
        return {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "ram_percent": memory.percent,
            "ram_used_mb": int(memory.used / (1024 * 1024)),
        }
    except Exception:
        return {"cpu_percent": None, "ram_percent": None, "ram_used_mb": None}
