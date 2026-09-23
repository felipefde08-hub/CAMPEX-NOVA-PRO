from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from backend.cameras.health import CameraStatus


@dataclass(frozen=True)
class CameraRuntimeState:
    id: str
    name: str
    status: CameraStatus
    last_frame_at: datetime | None
    last_connected_at: datetime | None
    reconnect_attempts: int
    frames_received: int
    consecutive_failures: int
    last_error: str | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status.value,
            "last_frame_at": self.last_frame_at.isoformat() if self.last_frame_at else None,
            "last_connected_at": (
                self.last_connected_at.isoformat() if self.last_connected_at else None
            ),
            "reconnect_attempts": self.reconnect_attempts,
            "frames_received": self.frames_received,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
        }
