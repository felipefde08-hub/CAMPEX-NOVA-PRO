from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from backend.cameras.manager import CameraManager
from backend.cameras.repository import CameraRepository
from backend.config import Settings
from backend.events.repository import EventRepository
from backend.monitoring.models import NormalizedRect
from backend.monitoring.repository import MonitoringRepository
from backend.monitoring.roi import crop_normalized_rect
from backend.monitoring.visual_indicator import DebouncedStateMachine, classify_hsv, dominant_hsv
from backend.notifications.service import NotificationService


class MonitoringService:
    def __init__(
        self,
        settings: Settings,
        camera_manager: CameraManager,
        *,
        repository: MonitoringRepository | None = None,
        camera_repository: CameraRepository | None = None,
        event_repository: EventRepository | None = None,
    ) -> None:
        self.settings = settings
        self.camera_manager = camera_manager
        self.repository = repository or MonitoringRepository(settings)
        self.cameras = camera_repository or CameraRepository(settings)
        self.events = event_repository or EventRepository(settings)
        self._machines: dict[str, DebouncedStateMachine] = {}

    def validate_visual_indicator(
        self,
        *,
        organization_id: str,
        monitor_id: str,
    ) -> dict[str, Any]:
        monitor = self.repository.get_monitor(monitor_id, organization_id)
        if monitor is None:
            raise ValueError("Monitor not found.")
        if monitor.type != "visual_indicator":
            raise ValueError("Validation is available for visual_indicator monitors.")
        roi = self.repository.get_roi(monitor.roi_id or "", organization_id)
        if roi is None:
            raise ValueError("ROI not found.")
        states = [state.as_dict() for state in self.repository.list_states(organization_id, monitor.id)]
        if not states:
            raise ValueError("At least one monitor state is required.")

        frame, frame_at = self.camera_manager.latest_frame(monitor.camera_id)
        if frame is None:
            return {
                "camera_id": monitor.camera_id,
                "monitor_id": monitor.id,
                "roi_id": roi.id,
                "available": False,
                "message": "No frame is available yet.",
            }

        coords = roi.coordinates
        rect = NormalizedRect(
            x=float(coords["x"]),
            y=float(coords["y"]),
            width=float(coords["width"]),
            height=float(coords["height"]),
        )
        roi_frame = crop_normalized_rect(frame, rect)
        hsv = dominant_hsv(roi_frame)
        classification = classify_hsv(hsv, states)
        debounce_seconds = float(monitor.configuration.get("debounce_seconds", 2.0))
        machine = self._machines.setdefault(monitor.id, DebouncedStateMachine(debounce_seconds))
        debounce = machine.observe(classification.state_id)
        transition = None
        event = None
        if debounce.get("changed"):
            transition = self.repository.record_transition(
                organization_id=organization_id,
                camera_id=monitor.camera_id,
                monitor_id=monitor.id,
                roi_id=roi.id,
                previous_state_id=debounce.get("previous_state_id"),
                current_state_id=debounce.get("current_state_id"),
                confidence=classification.confidence,
            )
            event = self._create_state_event(
                organization_id=organization_id,
                monitor=monitor.as_dict(),
                roi=roi.as_dict(),
                transition=transition,
                states=states,
            )

        current_state = _state_by_id(states, classification.state_id)
        return {
            "camera_id": monitor.camera_id,
            "monitor_id": monitor.id,
            "roi_id": roi.id,
            "available": True,
            "frame_at": frame_at.isoformat() if frame_at else None,
            "detected_hsv": hsv,
            "detected_state": current_state,
            "confidence": classification.confidence,
            "stable": bool(debounce.get("stable")),
            "debounce": debounce,
            "transition": transition,
            "event": event.as_dict() if event else None,
        }

    def _create_state_event(
        self,
        *,
        organization_id: str,
        monitor: dict[str, Any],
        roi: dict[str, Any],
        transition: dict[str, Any],
        states: list[dict[str, Any]],
    ):
        current = _state_by_id(states, transition.get("current_state_id"))
        previous = _state_by_id(states, transition.get("previous_state_id"))
        is_stop = bool((current or {}).get("is_stop_state"))
        event_type = "equipment_stop_started" if is_stop else "equipment_state_changed"
        now = datetime.now(timezone.utc).isoformat()
        event = self.events.create(
            event_type=event_type,
            camera_id=monitor["camera_id"],
            zone_id=roi["id"],
            severity="attention" if is_stop else "info",
            confidence=transition.get("confidence"),
            metadata={
                "monitor_id": monitor["id"],
                "monitor_name": monitor["name"],
                "roi_id": roi["id"],
                "zone_name": roi["name"],
                "zone_type": roi["type"],
                "activity_type": event_type,
                "activity_label": (current or {}).get("operational_meaning") or event_type,
                "previous_state": previous,
                "current_state": current,
                "transition_id": transition["id"],
                "knowledge_state": "OBSERVED",
            },
            started_at=transition.get("occurred_at") or now,
            organization_id=organization_id,
        )
        self._send_alert_safely(organization_id, event.as_dict())
        return event

    def _send_alert_safely(self, organization_id: str, event: dict[str, Any]) -> None:
        try:
            NotificationService(self.settings).send_alert(organization_id, event)
        except Exception:
            return


def _state_by_id(states: list[dict[str, Any]], state_id: str | None) -> dict[str, Any] | None:
    if not state_id:
        return None
    for state in states:
        if state.get("id") == state_id:
            return state
    return None
