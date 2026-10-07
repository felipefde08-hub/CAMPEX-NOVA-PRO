from __future__ import annotations

import os
import platform
import sys
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import time

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.cameras.security import sanitize_error_message
from backend.config import ROOT_DIR as BACKEND_ROOT_DIR
from backend.middleware.cors import LocalNetworkCORSMiddleware

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


class ZoneCreatePayload(BaseModel):
    camera_id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    type: str = Field(pattern="^(monitored|restricted)$")
    enabled: bool = True
    points: list[list[float]] = Field(min_length=3)


class ZoneUpdatePayload(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    type: str | None = Field(default=None, pattern="^(monitored|restricted)$")
    enabled: bool | None = None
    points: list[list[float]] | None = None


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
            raise ValueError("Camera name is required.")
        if not rtsp_url:
            raise ValueError("RTSP URL is required.")
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
        }

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
    def status() -> dict:
        return runtime.status()

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
    def add_camera(payload: LocalCameraPayload) -> dict:
        try:
            return runtime.add_camera(payload)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), **runtime.status()}

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
        return [zone.as_dict() for zone in runtime.lifecycle.events_store.list_zones(camera_id)]

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
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return zone.as_dict()

    @app.patch("/api/zones/{zone_id}")
    def update_zone(zone_id: str, payload: ZoneUpdatePayload) -> dict:
        try:
            zone = runtime.lifecycle.events_store.update_zone(zone_id, payload.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if zone is None:
            raise HTTPException(status_code=404, detail="Zone not found.")
        return zone.as_dict()

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

    return app


def _evidence_file(data_dir: Path, stored: str) -> Path | None:
    raw = Path(stored)
    file_path = (raw if raw.is_absolute() else BACKEND_ROOT_DIR / raw).resolve()
    evidence_root = (data_dir / "evidence").resolve()
    if evidence_root not in file_path.parents or not file_path.is_file():
        return None
    return file_path


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
