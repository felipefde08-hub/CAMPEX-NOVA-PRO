from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from backend.cameras.repository import CameraRepository
from backend.config import get_settings
from backend.monitoring.repository import MonitoringRepository
from backend.monitoring.roi import normalize_rect
from backend.monitoring.service import MonitoringService
from backend.security import OrganizationScope, get_organization_scope


router = APIRouter(prefix="/api/v1", tags=["monitoring"])


class ROICreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    type: str = Field(default="area", min_length=1, max_length=80)
    shape: Literal["rect"] = "rect"
    coordinates: dict[str, float]
    description: str | None = Field(default=None, max_length=400)
    enabled: bool = True

    @field_validator("coordinates")
    @classmethod
    def validate_coordinates(cls, value):
        rect = normalize_rect(
            x=value.get("x", 0),
            y=value.get("y", 0),
            width=value.get("width", 0),
            height=value.get("height", 0),
        )
        return rect.as_dict()


class MonitorCreate(BaseModel):
    camera_id: str = Field(min_length=1, max_length=80)
    roi_id: str | None = Field(default=None, max_length=80)
    type: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    configuration: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    states: list[dict[str, Any]] = Field(default_factory=list)


class DraftPayload(BaseModel):
    key: str = Field(min_length=1, max_length=120)
    data: dict[str, Any]


def _repo() -> MonitoringRepository:
    return MonitoringRepository(get_settings())


def _camera_repo() -> CameraRepository:
    return CameraRepository(get_settings())


def _service(request: Request) -> MonitoringService:
    return MonitoringService(request.app.state.settings, request.app.state.camera_manager)


@router.get("/camera-templates")
def camera_templates() -> list[dict[str, Any]]:
    return [
        {"id": "machine", "name": "Monitorar máquina", "recommended_monitors": ["equipment_state", "visual_indicator", "motion", "dwell"]},
        {"id": "entry", "name": "Monitorar entrada", "recommended_monitors": ["presence", "entry_exit", "counting"]},
        {"id": "people_flow", "name": "Fluxo de pessoas", "recommended_monitors": ["counting", "entry_exit", "dwell"]},
        {"id": "stock", "name": "Monitorar estoque", "recommended_monitors": ["object", "absence_detected"]},
        {"id": "dock_vehicle", "name": "Doca e veículos", "recommended_monitors": ["vehicle", "presence", "dwell"]},
        {"id": "custom", "name": "Configuração personalizada", "recommended_monitors": []},
    ]


@router.get("/cameras/{camera_id}/rois")
def list_camera_rois(
    camera_id: str,
    scope: OrganizationScope = Depends(get_organization_scope),
    repo: MonitoringRepository = Depends(_repo),
) -> list[dict[str, Any]]:
    return [roi.as_dict() for roi in repo.list_rois(scope.organization_id, camera_id)]


@router.post("/cameras/{camera_id}/rois", status_code=status.HTTP_201_CREATED)
def create_camera_roi(
    camera_id: str,
    payload: ROICreate,
    scope: OrganizationScope = Depends(get_organization_scope),
    repo: MonitoringRepository = Depends(_repo),
    cameras: CameraRepository = Depends(_camera_repo),
) -> dict[str, Any]:
    if cameras.get(camera_id, scope.organization_id) is None:
        raise HTTPException(status_code=404, detail="Camera not found.")
    roi = repo.create_roi(
        organization_id=scope.organization_id,
        camera_id=camera_id,
        name=payload.name,
        roi_type=payload.type,
        shape=payload.shape,
        coordinates=payload.coordinates,
        description=payload.description,
        enabled=payload.enabled,
    )
    return roi.as_dict()


@router.get("/monitors")
def list_monitors(
    camera_id: str | None = None,
    scope: OrganizationScope = Depends(get_organization_scope),
    repo: MonitoringRepository = Depends(_repo),
) -> list[dict[str, Any]]:
    return [monitor.as_dict() for monitor in repo.list_monitors(scope.organization_id, camera_id)]


@router.post("/monitors", status_code=status.HTTP_201_CREATED)
def create_monitor(
    payload: MonitorCreate,
    scope: OrganizationScope = Depends(get_organization_scope),
    repo: MonitoringRepository = Depends(_repo),
    cameras: CameraRepository = Depends(_camera_repo),
) -> dict[str, Any]:
    if cameras.get(payload.camera_id, scope.organization_id) is None:
        raise HTTPException(status_code=404, detail="Camera not found.")
    if payload.roi_id and repo.get_roi(payload.roi_id, scope.organization_id) is None:
        raise HTTPException(status_code=404, detail="ROI not found.")
    monitor = repo.create_monitor(
        organization_id=scope.organization_id,
        camera_id=payload.camera_id,
        roi_id=payload.roi_id,
        monitor_type=payload.type,
        name=payload.name,
        configuration=payload.configuration,
        enabled=payload.enabled,
    )
    states = repo.replace_states(
        organization_id=scope.organization_id,
        monitor_id=monitor.id,
        states=payload.states,
    ) if payload.states else []
    return {**monitor.as_dict(), "states": [state.as_dict() for state in states]}


@router.get("/monitors/{monitor_id}/states")
def list_monitor_states(
    monitor_id: str,
    scope: OrganizationScope = Depends(get_organization_scope),
    repo: MonitoringRepository = Depends(_repo),
) -> list[dict[str, Any]]:
    if repo.get_monitor(monitor_id, scope.organization_id) is None:
        raise HTTPException(status_code=404, detail="Monitor not found.")
    return [state.as_dict() for state in repo.list_states(scope.organization_id, monitor_id)]


@router.get("/monitors/{monitor_id}/status")
def monitor_status(
    monitor_id: str,
    request: Request,
    scope: OrganizationScope = Depends(get_organization_scope),
) -> dict[str, Any]:
    try:
        return _service(request).validate_visual_indicator(
            organization_id=scope.organization_id,
            monitor_id=monitor_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

