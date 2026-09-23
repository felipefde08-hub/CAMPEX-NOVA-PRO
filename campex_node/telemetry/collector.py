from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from backend.cameras.health import CameraStatus
from backend.cameras.security import sanitize_error_message

from campex_node.cameras.manager import CameraManager
from campex_node.core.config import NodeSettings
from campex_node.storage.local_store import LocalStore


logger = logging.getLogger("campex.node.telemetry")


class TelemetryCollector:
    """Collects camera health snapshots and queues them for Cloud sync.

    The collector does not process images. It only converts runtime camera state
    into operational metrics and status-change events so CAMPEX Cloud can show
    reliable node/camera health before the AI pipeline exists.
    """

    def __init__(
        self,
        *,
        settings: NodeSettings,
        node_id: str,
        camera_manager: CameraManager,
        store: LocalStore,
    ) -> None:
        self.settings = settings
        self.node_id = node_id
        self.camera_manager = camera_manager
        self.store = store
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="campex-node-telemetry",
            daemon=True,
        )

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3)

    def collect_once(self) -> dict[str, int]:
        captured_at = datetime.now(timezone.utc).isoformat()
        metrics = events = 0
        for state in self.camera_manager.states():
            for payload in self._metric_payloads(state.as_dict(), captured_at):
                self.store.enqueue_event("metric", payload, event_id=payload["metric_id"])
                metrics += 1
            status_event = self._status_event_payload(state.as_dict(), captured_at)
            if status_event:
                self.store.enqueue_event("event", status_event, event_id=status_event["event_id"])
                events += 1
        if metrics or events:
            logger.debug("Telemetry queued", extra={"metrics": metrics, "events": events})
        return {"metrics": metrics, "events": events}

    def _metric_payloads(self, state: dict[str, Any], captured_at: str) -> list[dict[str, Any]]:
        camera_id = str(state["id"])
        status = str(state.get("status") or CameraStatus.OFFLINE.value)
        online = 1.0 if status == CameraStatus.ONLINE.value else 0.0
        payload = {
            "status": status,
            "camera_name": state.get("name"),
            "last_frame_at": state.get("last_frame_at"),
            "last_connected_at": state.get("last_connected_at"),
            "last_error": sanitize_error_message(state.get("last_error")),
        }
        values = {
            "camera_online": online,
            "camera_frames_received": float(state.get("frames_received") or 0),
            "camera_reconnect_attempts": float(state.get("reconnect_attempts") or 0),
            "camera_consecutive_failures": float(state.get("consecutive_failures") or 0),
        }
        return [
            {
                "metric_id": self._item_id("metric", camera_id, metric_type, captured_at),
                "metric_type": metric_type,
                "camera_id": camera_id,
                "captured_at": captured_at,
                "value": value,
                "payload": payload,
            }
            for metric_type, value in values.items()
        ]

    def _status_event_payload(self, state: dict[str, Any], captured_at: str) -> dict[str, Any] | None:
        camera_id = str(state["id"])
        status = str(state.get("status") or CameraStatus.OFFLINE.value)
        meta_key = f"camera_status:{camera_id}"
        previous = self.store.get_meta(meta_key)
        if previous == status:
            return None
        self.store.set_meta(meta_key, status)
        severity = "info" if status == CameraStatus.ONLINE.value else "attention"
        return {
            "event_id": self._item_id("event", camera_id, f"status_{status}", captured_at),
            "camera_id": camera_id,
            "event_type": "camera_status_changed",
            "severity": severity,
            "status": "CLOSED",
            "timestamp": captured_at,
            "metadata": {
                "previous_status": previous,
                "current_status": status,
                "camera_name": state.get("name"),
                "last_error": sanitize_error_message(state.get("last_error")),
            },
        }

    def _item_id(self, kind: str, camera_id: str, name: str, captured_at: str) -> str:
        safe_camera = _safe_id(camera_id)
        safe_name = _safe_id(name)
        safe_time = _safe_id(captured_at)
        return f"{kind}_{self.node_id}_{safe_camera}_{safe_name}_{safe_time}"

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.collect_once()
            except Exception:
                logger.exception("Telemetry collection failed")
            self._stop.wait(self.settings.telemetry_interval_seconds)


def _safe_id(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value)[:120]
