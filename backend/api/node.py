from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from backend.cameras.repository import CameraRepository
from backend.config import get_settings
from backend.database.db import connect
from backend.security import OrganizationScope, get_organization_scope


router = APIRouter(prefix="/api/v1/node", tags=["node"])


class NodeHeartbeat(BaseModel):
    node_id: str = Field(min_length=1, max_length=120)
    status: str = Field(default="online", max_length=40)
    version: str = Field(default="0.1.0", max_length=40)
    cameras_total: int = Field(default=0, ge=0)
    cameras_online: int = Field(default=0, ge=0)


def get_repository() -> CameraRepository:
    return CameraRepository(get_settings())


@router.get("/config")
def node_config(
    scope: OrganizationScope = Depends(get_organization_scope),
    repository: CameraRepository = Depends(get_repository),
) -> dict:
    cameras = [
        {
            "id": camera.id,
            "name": camera.name,
            "source_type": camera.source_type,
            "source_uri": camera.source_uri,
            "enabled": camera.enabled,
            "updated_at": camera.updated_at,
        }
        for camera in repository.list(scope.organization_id)
        if camera.source_type in {"rtsp", "ip_camera"} and camera.enabled
    ]
    return {
        "organization_id": scope.organization_id,
        "version": get_settings().version,
        "cameras": cameras,
    }


@router.post("/heartbeat")
def node_heartbeat(
    payload: NodeHeartbeat,
    scope: OrganizationScope = Depends(get_organization_scope),
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    settings = get_settings()
    with connect(settings.sqlite_path) as connection:
        connection.execute(
            """
            INSERT INTO campex_nodes (
                id, organization_id, name, status, version,
                cameras_total, cameras_online, last_seen_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                organization_id = excluded.organization_id,
                status = excluded.status,
                version = excluded.version,
                cameras_total = excluded.cameras_total,
                cameras_online = excluded.cameras_online,
                last_seen_at = excluded.last_seen_at,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                payload.node_id,
                scope.organization_id,
                payload.node_id,
                payload.status,
                payload.version,
                payload.cameras_total,
                payload.cameras_online,
                now,
            ),
        )
        connection.commit()
    return {"ok": True, "node_id": payload.node_id, "received_at": now}
