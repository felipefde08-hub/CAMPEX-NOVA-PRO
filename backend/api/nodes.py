from __future__ import annotations

import platform as platform_module
import socket
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.cameras.repository import CameraRepository
from backend.cloud.nodes import NodeIdentity, NodeRepository, get_node_identity
from backend.config import get_settings
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


@router.delete("/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_node(
    node_id: str,
    scope: OrganizationScope = Depends(get_organization_scope),
    repository: NodeRepository = Depends(get_node_repository),
) -> None:
    if not repository.revoke_node(node_id, scope.organization_id):
        raise HTTPException(status_code=404, detail="Node not found.")


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
