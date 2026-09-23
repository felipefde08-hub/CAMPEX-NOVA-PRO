from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NormalizedRect:
    x: float
    y: float
    width: float
    height: float

    def as_dict(self) -> dict[str, float]:
        return {
            "x": round(self.x, 6),
            "y": round(self.y, 6),
            "width": round(self.width, 6),
            "height": round(self.height, 6),
        }


@dataclass(frozen=True)
class CameraROI:
    id: str
    organization_id: str
    camera_id: str
    name: str
    type: str
    shape: str
    coordinates: dict[str, Any]
    description: str | None
    enabled: bool
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "organization_id": self.organization_id,
            "camera_id": self.camera_id,
            "name": self.name,
            "type": self.type,
            "shape": self.shape,
            "coordinates": self.coordinates,
            "description": self.description,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class CameraMonitor:
    id: str
    organization_id: str
    camera_id: str
    roi_id: str | None
    type: str
    name: str
    configuration: dict[str, Any]
    enabled: bool
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "organization_id": self.organization_id,
            "camera_id": self.camera_id,
            "roi_id": self.roi_id,
            "type": self.type,
            "name": self.name,
            "configuration": self.configuration,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class MonitorState:
    id: str
    organization_id: str
    monitor_id: str
    name: str
    operational_meaning: str
    color: str
    hsv_target: dict[str, float]
    tolerance: dict[str, float]
    is_stop_state: bool
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "organization_id": self.organization_id,
            "monitor_id": self.monitor_id,
            "name": self.name,
            "operational_meaning": self.operational_meaning,
            "color": self.color,
            "hsv_target": self.hsv_target,
            "tolerance": self.tolerance,
            "is_stop_state": self.is_stop_state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

