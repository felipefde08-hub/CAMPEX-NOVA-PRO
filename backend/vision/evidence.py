from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2

from backend.config import ROOT_DIR, get_data_dir
from backend.events.models import Event
from backend.vision.models import TrackedObject
from backend.vision.overlay import OverlayRenderer


class EvidenceRecorder:
    def __init__(self, storage_dir: Path | None = None) -> None:
        self.storage_dir = storage_dir or get_data_dir() / "evidence"
        self._overlay = OverlayRenderer()

    def record(
        self,
        *,
        event: Event,
        frame: Any,
        objects: list[TrackedObject],
        frame_at: datetime,
    ) -> dict:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        event_dir = self.storage_dir / event.id
        event_dir.mkdir(parents=True, exist_ok=True)

        clean_path = event_dir / "frame.jpg"
        overlay_path = event_dir / "overlay.jpg"
        metadata_path = event_dir / "metadata.json"

        cv2.imwrite(str(clean_path), frame)
        overlay = self._overlay.render(frame, objects)
        cv2.imwrite(str(overlay_path), overlay)

        payload = {
            "event_id": event.id,
            "event_type": event.type,
            "camera_id": event.camera_id,
            "zone_id": event.zone_id,
            "track_id": event.track_id,
            "severity": event.severity,
            "frame_at": frame_at.isoformat(),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "objects": [obj.as_dict() for obj in objects],
        }
        metadata_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        return {
            "evidence_dir": _metadata_path(event_dir),
            "snapshot_path": _metadata_path(clean_path),
            "overlay_path": _metadata_path(overlay_path),
            "evidence_metadata_path": _metadata_path(metadata_path),
        }


def _metadata_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT_DIR))
    except ValueError:
        return str(path)
