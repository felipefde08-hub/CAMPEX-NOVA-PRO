from __future__ import annotations

import platform as platform_module
import socket
from datetime import datetime, timezone
import json

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from backend.cameras.repository import CameraRepository
from backend.cloud.nodes import NodeIdentity, NodeRepository, get_node_identity
from backend.config import get_settings
from backend.database.db import connect
from backend.security import OrganizationScope, get_organization_scope


router = APIRouter(prefix="/api/v1/nodes", tags=["nodes"])
legacy_router = APIRouter(prefix="/api/v1/node", tags=["node-legacy"])


class PairRequest(BaseModel):
    requested_by: str | None = Field(default=None, max_length=120)


class PairClaim(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    node_name: str = Field(default="", max_length=120)
    platform: str = Field(default="", max_length=80)
    hostname: str = Field(default="", max_length=120)
    version: str = Field(default="0.1.0", max_length=40)


class NodePairingStart(BaseModel):
    node_public_id: str = Field(min_length=1, max_length=120)
    node_name: str = Field(default="CAMPEX Node", max_length=120)
    hostname: str | None = Field(default=None, max_length=120)
    platform: str | None = Field(default=None, max_length=80)
    architecture: str | None = Field(default=None, max_length=80)
    version: str = Field(default="0.1.0", max_length=40)


class NodePairingStatusQuery(BaseModel):
    session_id: str = Field(min_length=1, max_length=120)
    node_public_id: str = Field(min_length=1, max_length=120)


class NodePairingAuthorize(BaseModel):
    code: str = Field(min_length=1, max_length=20)
    node_name: str | None = Field(default=None, max_length=120)


class NodeHeartbeat(BaseModel):
    status: str = Field(default="online", max_length=40)
    version: str = Field(default="0.1.0", max_length=40)
    platform: str | None = Field(default=None, max_length=80)
    hostname: str | None = Field(default=None, max_length=120)
    cameras_total: int = Field(default=0, ge=0)
    cameras_online: int = Field(default=0, ge=0)
    vision_status: str | None = Field(default="idle", max_length=80)
    queue_size: int = Field(default=0, ge=0)
    timestamp: str | None = None


class NodeUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


def get_node_repository() -> NodeRepository:
    return NodeRepository(get_settings())


def get_camera_repository() -> CameraRepository:
    return CameraRepository(get_settings())


@router.post("/pair/request", status_code=status.HTTP_201_CREATED)
def request_pairing_code(
    payload: PairRequest,
    scope: OrganizationScope = Depends(get_organization_scope),
    repository: NodeRepository = Depends(get_node_repository),
) -> dict:
    pairing = repository.create_pairing_code(
        organization_id=scope.organization_id,
        requested_by=payload.requested_by,
    )
    return {
        "code": pairing.code,
        "organization_id": pairing.organization_id,
        "expires_at": pairing.expires_at,
    }


@router.post("/pair/claim")
def claim_pairing_code(
    payload: PairClaim,
    repository: NodeRepository = Depends(get_node_repository),
) -> dict:
    claimed = repository.claim_pairing_code(
        code=payload.code,
        node_name=payload.node_name.strip() or socket.gethostname() or "CAMPEX Node",
        platform=payload.platform.strip() or platform_module.system().lower(),
        hostname=payload.hostname.strip() or socket.gethostname(),
        version=payload.version,
    )
    if claimed is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Código de pareamento inválido ou expirado.",
        )
    return {
        "node_id": claimed.node_id,
        "organization_id": claimed.organization_id,
        "node_token": claimed.node_token,
        "name": claimed.name,
    }


@router.post("/pairing/start", status_code=status.HTTP_201_CREATED)
def start_node_initiated_pairing(
    payload: NodePairingStart,
    repository: NodeRepository = Depends(get_node_repository),
) -> dict:
    session = repository.start_pairing_session(
        node_public_id=payload.node_public_id,
        node_name=payload.node_name.strip() or "CAMPEX Node",
        hostname=payload.hostname,
        platform=payload.platform,
        architecture=payload.architecture,
        version=payload.version,
    )
    return {
        "session_id": session.id,
        "pairing_code": f"{session.pairing_code[:3]} {session.pairing_code[3:]}",
        "expires_at": session.expires_at,
        "status": session.status,
    }


@router.post("/pairing/status")
def node_pairing_status(
    payload: NodePairingStatusQuery,
    repository: NodeRepository = Depends(get_node_repository),
) -> dict:
    claimed = repository.consume_authorized_pairing_session(
        payload.session_id,
        payload.node_public_id,
    )
    if claimed is not None:
        return {
            "status": "authorized",
            "node_id": claimed.node_id,
            "organization_id": claimed.organization_id,
            "node_token": claimed.node_token,
            "name": claimed.name,
        }
    session = repository.get_pairing_session_status(payload.session_id, payload.node_public_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Pairing session not found.")
    return {
        "status": session["status"],
        "expires_at": session["expires_at"],
    }


@router.post("/pairing/lookup")
def lookup_node_pairing_session(
    payload: NodePairingAuthorize,
    scope: OrganizationScope = Depends(get_organization_scope),
    repository: NodeRepository = Depends(get_node_repository),
) -> dict:
    session = repository.find_pending_pairing_session_by_code(payload.code)
    if session is None:
        raise HTTPException(status_code=404, detail="Código inválido ou expirado.")
    return {
        "status": "pending",
        "node_name": session["node_name"],
        "hostname": session["hostname"],
        "platform": session["platform"],
        "architecture": session["architecture"],
        "version": session["version"],
        "expires_at": session["expires_at"],
        "organization_id": scope.organization_id,
    }


@router.post("/pairing/authorize")
def authorize_node_pairing_session(
    payload: NodePairingAuthorize,
    scope: OrganizationScope = Depends(get_organization_scope),
    repository: NodeRepository = Depends(get_node_repository),
) -> dict:
    claimed = repository.authorize_pairing_session(
        code=payload.code,
        organization_id=scope.organization_id,
        node_name=payload.node_name,
    )
    if claimed is None:
        raise HTTPException(status_code=404, detail="Código inválido, expirado ou já utilizado.")
    return {
        "ok": True,
        "node_id": claimed.node_id,
        "organization_id": claimed.organization_id,
        "name": claimed.name,
    }


@router.get("")
def list_nodes(
    scope: OrganizationScope = Depends(get_organization_scope),
    repository: NodeRepository = Depends(get_node_repository),
) -> list[dict]:
    return [_serialize_node(node) for node in repository.list_nodes(scope.organization_id)]


@router.get("/{node_id}")
def get_node(
    node_id: str,
    scope: OrganizationScope = Depends(get_organization_scope),
    repository: NodeRepository = Depends(get_node_repository),
) -> dict:
    node = repository.get_node(node_id, scope.organization_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found.")
    return _serialize_node(node)


@router.get("/{node_id}/telemetry")
def get_node_telemetry(
    node_id: str,
    scope: OrganizationScope = Depends(get_organization_scope),
    repository: NodeRepository = Depends(get_node_repository),
) -> dict:
    node = repository.get_node(node_id, scope.organization_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found.")
    settings = get_settings()
    with connect(settings.sqlite_path) as connection:
        metric_rows = connection.execute(
            """
            SELECT * FROM node_metrics
            WHERE organization_id = ? AND node_id = ?
            ORDER BY captured_at DESC
            LIMIT 200
            """,
            (scope.organization_id, node_id),
        ).fetchall()
        event_rows = connection.execute(
            """
            SELECT id, type, camera_id, severity, status, started_at, metadata
            FROM events
            WHERE organization_id = ? AND node_id = ?
            ORDER BY started_at DESC
            LIMIT 50
            """,
            (scope.organization_id, node_id),
        ).fetchall()
    metrics = [_serialize_metric(row) for row in metric_rows]
    events = [_serialize_node_event(row) for row in event_rows]
    return {
        "node": _serialize_node(node),
        "summary": _telemetry_summary(metrics, events),
        "cameras": _camera_telemetry(metrics, events),
        "metrics": metrics[:50],
        "events": events,
    }


@router.patch("/{node_id}")
def rename_node(
    node_id: str,
    payload: NodeUpdate,
    scope: OrganizationScope = Depends(get_organization_scope),
    repository: NodeRepository = Depends(get_node_repository),
) -> dict:
    node = repository.rename_node(node_id, scope.organization_id, payload.name)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found.")
    return _serialize_node(node)


@router.delete(
    "/{node_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
def revoke_node(
    node_id: str,
    scope: OrganizationScope = Depends(get_organization_scope),
    repository: NodeRepository = Depends(get_node_repository),
) -> Response:
    if not repository.revoke_node(node_id, scope.organization_id):
        raise HTTPException(status_code=404, detail="Node not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{node_id}/heartbeat")
def node_heartbeat(
    node_id: str,
    payload: NodeHeartbeat,
    identity: NodeIdentity = Depends(get_node_identity),
    repository: NodeRepository = Depends(get_node_repository),
) -> dict:
    if node_id != identity.node_id:
        raise HTTPException(status_code=403, detail="Node token does not match requested node.")
    repository.update_heartbeat(
        node_id=identity.node_id,
        status=payload.status,
        version=payload.version,
        platform=payload.platform,
        hostname=payload.hostname,
        cameras_total=payload.cameras_total,
        cameras_online=payload.cameras_online,
        vision_status=payload.vision_status,
        queue_size=payload.queue_size,
    )
    return {
        "ok": True,
        "node_id": identity.node_id,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/{node_id}/config")
def node_config(
    node_id: str,
    identity: NodeIdentity = Depends(get_node_identity),
    repository: CameraRepository = Depends(get_camera_repository),
) -> dict:
    if node_id != identity.node_id:
        raise HTTPException(status_code=403, detail="Node token does not match requested node.")
    cameras = [
        {
            "id": camera.id,
            "name": camera.name,
            "source_type": camera.source_type,
            "source_uri": camera.source_uri,
            "enabled": camera.enabled,
            "updated_at": camera.updated_at,
        }
        for camera in repository.list(identity.organization_id)
        if camera.source_type in {"rtsp", "ip_camera"} and camera.enabled
    ]
    return {
        "organization_id": identity.organization_id,
        "node_id": identity.node_id,
        "version": get_settings().version,
        "cameras": cameras,
    }


@legacy_router.get("/config")
def legacy_node_config(
    identity: NodeIdentity = Depends(get_node_identity),
    repository: CameraRepository = Depends(get_camera_repository),
) -> dict:
    return node_config(identity.node_id, identity, repository)


@legacy_router.post("/heartbeat")
def legacy_node_heartbeat(
    payload: NodeHeartbeat,
    identity: NodeIdentity = Depends(get_node_identity),
    repository: NodeRepository = Depends(get_node_repository),
) -> dict:
    return node_heartbeat(identity.node_id, payload, identity, repository)


def _serialize_node(node: dict) -> dict:
    return {
        "id": node["id"],
        "organization_id": node["organization_id"],
        "name": node["name"],
        "status": node["status"],
        "version": node["version"],
        "platform": node.get("platform"),
        "hostname": node.get("hostname"),
        "cameras_total": node.get("cameras_total", 0),
        "cameras_online": node.get("cameras_online", 0),
        "vision_status": node.get("vision_status"),
        "queue_size": node.get("queue_size", 0),
        "last_seen_at": node.get("last_seen_at"),
        "revoked_at": node.get("revoked_at"),
        "created_at": node.get("created_at"),
        "updated_at": node.get("updated_at"),
    }


def _serialize_metric(row) -> dict:
    payload = _loads_json(row["payload_json"])
    return {
        "id": row["id"],
        "node_id": row["node_id"],
        "camera_id": row["camera_id"],
        "metric_type": row["metric_type"],
        "value": row["value"],
        "payload": payload,
        "captured_at": row["captured_at"],
        "received_at": row["received_at"],
    }


def _serialize_node_event(row) -> dict:
    return {
        "id": row["id"],
        "type": row["type"],
        "camera_id": row["camera_id"],
        "severity": row["severity"],
        "status": row["status"],
        "started_at": row["started_at"],
        "metadata": _loads_json(row["metadata"]),
    }


def _telemetry_summary(metrics: list[dict], events: list[dict]) -> dict:
    latest_by_type: dict[str, dict] = {}
    for metric in metrics:
        key = metric["metric_type"]
        if key not in latest_by_type:
            latest_by_type[key] = metric
    cameras = _camera_telemetry(metrics, events)
    return {
        "metrics_count": len(metrics),
        "events_count": len(events),
        "cameras_reported": len(cameras),
        "cameras_online": sum(1 for camera in cameras if camera.get("online")),
        "latest_metric_at": metrics[0]["captured_at"] if metrics else None,
        "latest_event_at": events[0]["started_at"] if events else None,
        "latest": latest_by_type,
    }


def _camera_telemetry(metrics: list[dict], events: list[dict]) -> list[dict]:
    cameras: dict[str, dict] = {}
    for metric in metrics:
        camera_id = metric.get("camera_id") or "unknown"
        camera = cameras.setdefault(camera_id, {"camera_id": camera_id, "metrics": {}, "events": []})
        metric_type = metric["metric_type"]
        if metric_type not in camera["metrics"]:
            camera["metrics"][metric_type] = metric
        payload = metric.get("payload") or {}
        if payload.get("camera_name") and not camera.get("name"):
            camera["name"] = payload.get("camera_name")
        if payload.get("status") and not camera.get("status"):
            camera["status"] = payload.get("status")
    for event in events:
        camera_id = event.get("camera_id") or "unknown"
        camera = cameras.setdefault(camera_id, {"camera_id": camera_id, "metrics": {}, "events": []})
        if len(camera["events"]) < 5:
            camera["events"].append(event)
        metadata = event.get("metadata") or {}
        if metadata.get("camera_name") and not camera.get("name"):
            camera["name"] = metadata.get("camera_name")
        if metadata.get("current_status") and not camera.get("status"):
            camera["status"] = metadata.get("current_status")
    for camera in cameras.values():
        online_metric = camera["metrics"].get("camera_online")
        camera["online"] = bool(online_metric and online_metric.get("value") == 1)
        camera["frames_received"] = _metric_value(camera, "camera_frames_received")
        camera["reconnect_attempts"] = _metric_value(camera, "camera_reconnect_attempts")
        camera["consecutive_failures"] = _metric_value(camera, "camera_consecutive_failures")
        camera["last_metric_at"] = max((m["captured_at"] for m in camera["metrics"].values()), default=None)
    return sorted(cameras.values(), key=lambda item: item.get("name") or item["camera_id"])


def _metric_value(camera: dict, metric_type: str) -> float | None:
    metric = camera["metrics"].get(metric_type)
    return metric.get("value") if metric else None


def _loads_json(value) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}
