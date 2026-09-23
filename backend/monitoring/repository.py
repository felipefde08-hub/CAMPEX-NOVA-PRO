from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from backend.config import Settings
from backend.database.db import connect
from backend.monitoring.models import CameraMonitor, CameraROI, MonitorState


def _json(value: Any) -> str:
    return json.dumps(value or {}, separators=(",", ":"))


def _loads(value: str | None, fallback):
    if not value:
        return fallback
    parsed = json.loads(value)
    return parsed if parsed is not None else fallback


class MonitoringRepository:
    def __init__(self, settings: Settings) -> None:
        self.database_path = settings.sqlite_path

    def list_rois(self, organization_id: str, camera_id: str | None = None) -> list[CameraROI]:
        params: list[Any] = [organization_id]
        where = "organization_id = ?"
        if camera_id:
            where += " AND camera_id = ?"
            params.append(camera_id)
        with connect(self.database_path) as connection:
            rows = connection.execute(
                f"SELECT * FROM camera_rois WHERE {where} ORDER BY created_at ASC",
                params,
            ).fetchall()
        return [_row_to_roi(row) for row in rows]

    def get_roi(self, roi_id: str, organization_id: str) -> CameraROI | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM camera_rois WHERE id = ? AND organization_id = ?",
                (roi_id, organization_id),
            ).fetchone()
        return _row_to_roi(row) if row else None

    def create_roi(
        self,
        *,
        organization_id: str,
        camera_id: str,
        name: str,
        roi_type: str,
        shape: str,
        coordinates: dict[str, Any],
        description: str | None = None,
        enabled: bool = True,
    ) -> CameraROI:
        roi_id = f"roi_{uuid4().hex[:12]}"
        with connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO camera_rois (
                    id, organization_id, camera_id, name, type, shape,
                    coordinates, description, enabled
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    roi_id,
                    organization_id,
                    camera_id,
                    name,
                    roi_type,
                    shape,
                    _json(coordinates),
                    description,
                    int(enabled),
                ),
            )
            connection.commit()
        roi = self.get_roi(roi_id, organization_id)
        if roi is None:
            raise RuntimeError("ROI was not persisted.")
        return roi

    def list_monitors(self, organization_id: str, camera_id: str | None = None) -> list[CameraMonitor]:
        params: list[Any] = [organization_id]
        where = "organization_id = ?"
        if camera_id:
            where += " AND camera_id = ?"
            params.append(camera_id)
        with connect(self.database_path) as connection:
            rows = connection.execute(
                f"SELECT * FROM camera_monitors WHERE {where} ORDER BY created_at ASC",
                params,
            ).fetchall()
        return [_row_to_monitor(row) for row in rows]

    def get_monitor(self, monitor_id: str, organization_id: str) -> CameraMonitor | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM camera_monitors WHERE id = ? AND organization_id = ?",
                (monitor_id, organization_id),
            ).fetchone()
        return _row_to_monitor(row) if row else None

    def create_monitor(
        self,
        *,
        organization_id: str,
        camera_id: str,
        roi_id: str | None,
        monitor_type: str,
        name: str,
        configuration: dict[str, Any],
        enabled: bool = True,
    ) -> CameraMonitor:
        monitor_id = f"mon_{uuid4().hex[:12]}"
        with connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO camera_monitors (
                    id, organization_id, camera_id, roi_id, type, name,
                    configuration, enabled
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    monitor_id,
                    organization_id,
                    camera_id,
                    roi_id,
                    monitor_type,
                    name,
                    _json(configuration),
                    int(enabled),
                ),
            )
            connection.commit()
        monitor = self.get_monitor(monitor_id, organization_id)
        if monitor is None:
            raise RuntimeError("Monitor was not persisted.")
        return monitor

    def replace_states(
        self,
        *,
        organization_id: str,
        monitor_id: str,
        states: list[dict[str, Any]],
    ) -> list[MonitorState]:
        with connect(self.database_path) as connection:
            connection.execute(
                "DELETE FROM monitor_states WHERE organization_id = ? AND monitor_id = ?",
                (organization_id, monitor_id),
            )
            for state in states:
                connection.execute(
                    """
                    INSERT INTO monitor_states (
                        id, organization_id, monitor_id, name, operational_meaning,
                        color, hsv_target, tolerance, is_stop_state
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        state.get("id") or f"st_{uuid4().hex[:12]}",
                        organization_id,
                        monitor_id,
                        state["name"],
                        state.get("operational_meaning") or state["name"],
                        state.get("color") or "",
                        _json(state.get("hsv_target")),
                        _json(state.get("tolerance") or {"h": 12, "s": 80, "v": 80}),
                        int(bool(state.get("is_stop_state"))),
                    ),
                )
            connection.commit()
        return self.list_states(organization_id, monitor_id)

    def list_states(self, organization_id: str, monitor_id: str) -> list[MonitorState]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM monitor_states
                WHERE organization_id = ? AND monitor_id = ?
                ORDER BY created_at ASC
                """,
                (organization_id, monitor_id),
            ).fetchall()
        return [_row_to_state(row) for row in rows]

    def record_transition(
        self,
        *,
        organization_id: str,
        camera_id: str,
        monitor_id: str,
        roi_id: str | None,
        previous_state_id: str | None,
        current_state_id: str | None,
        confidence: float,
    ) -> dict[str, Any]:
        transition_id = f"tr_{uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        with connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO state_transitions (
                    id, organization_id, camera_id, monitor_id, roi_id,
                    previous_state_id, current_state_id, confidence, occurred_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transition_id,
                    organization_id,
                    camera_id,
                    monitor_id,
                    roi_id,
                    previous_state_id,
                    current_state_id,
                    confidence,
                    now,
                ),
            )
            connection.commit()
        return {
            "id": transition_id,
            "organization_id": organization_id,
            "camera_id": camera_id,
            "monitor_id": monitor_id,
            "roi_id": roi_id,
            "previous_state_id": previous_state_id,
            "current_state_id": current_state_id,
            "confidence": confidence,
            "occurred_at": now,
        }


def _row_to_roi(row) -> CameraROI:
    return CameraROI(
        id=row["id"],
        organization_id=row["organization_id"],
        camera_id=row["camera_id"],
        name=row["name"],
        type=row["type"],
        shape=row["shape"],
        coordinates=_loads(row["coordinates"], {}),
        description=row["description"],
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_monitor(row) -> CameraMonitor:
    return CameraMonitor(
        id=row["id"],
        organization_id=row["organization_id"],
        camera_id=row["camera_id"],
        roi_id=row["roi_id"],
        type=row["type"],
        name=row["name"],
        configuration=_loads(row["configuration"], {}),
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_state(row) -> MonitorState:
    return MonitorState(
        id=row["id"],
        organization_id=row["organization_id"],
        monitor_id=row["monitor_id"],
        name=row["name"],
        operational_meaning=row["operational_meaning"],
        color=row["color"],
        hsv_target=_loads(row["hsv_target"], {}),
        tolerance=_loads(row["tolerance"], {}),
        is_stop_state=bool(row["is_stop_state"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )

