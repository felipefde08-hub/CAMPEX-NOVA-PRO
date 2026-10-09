from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field

from backend.cameras.cloud_runtime import CameraReport, CloudCameraRuntime, decode_frame
from backend.cloud.nodes import NodeIdentity, get_node_identity
from backend.config import get_settings
from backend.database.db import connect
from backend.maintenance.retention import run_retention_if_due


router = APIRouter(prefix="/api/v1/node-sync", tags=["node-sync"])


class NodeEventPayload(BaseModel):
    event_id: str = Field(min_length=1, max_length=160)
    camera_id: str = Field(min_length=1, max_length=120)
    event_type: str = Field(min_length=1, max_length=120)
    severity: Literal["info", "attention", "critical"] = "info"
    status: Literal["OPEN", "REVIEWED", "CLOSED"] = "OPEN"
    timestamp: str
    ended_at: str | None = None
    duration: float | None = None
    confidence: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class NodeMetricPayload(BaseModel):
    metric_id: str = Field(min_length=1, max_length=160)
    metric_type: str = Field(min_length=1, max_length=120)
    captured_at: str
    camera_id: str | None = Field(default=None, max_length=120)
    value: float | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class NodeSyncBatch(BaseModel):
    events: list[NodeEventPayload] = Field(default_factory=list)
    metrics: list[NodeMetricPayload] = Field(default_factory=list)


class CameraLiveReport(BaseModel):
    camera_id: str = Field(min_length=1, max_length=120)
    status: str = Field(default="OFFLINE", max_length=20)
    last_error: str | None = Field(default=None, max_length=500)
    frames_received: int = Field(default=0, ge=0)
    approximate_fps: float | None = None
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    last_frame_at: str | None = Field(default=None, max_length=40)
    vision: dict[str, Any] = Field(default_factory=dict)
    objects: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    poses: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    # Identity ({session_id, frame_id, frame_at, ...}) of the frame the objects
    # were detected on, and of the uploaded frame. Older Nodes send neither.
    vision_frame: dict[str, Any] | None = None
    frame_ref: dict[str, Any] | None = None
    frame_jpeg_base64: str | None = Field(default=None, max_length=2_100_000)


class CameraTestResult(BaseModel):
    job_id: str = Field(min_length=1, max_length=80)
    result: dict[str, Any] = Field(default_factory=dict)


class NodeLiveBatch(BaseModel):
    cameras: list[CameraLiveReport] = Field(default_factory=list, max_length=64)
    test_results: list[CameraTestResult] = Field(default_factory=list, max_length=16)


@router.post("/cameras/live")
def sync_camera_live_state(
    batch: NodeLiveBatch,
    identity: NodeIdentity = Depends(get_node_identity),
) -> dict:
    """Camera status, Vision results and frames from the Node.

    The response carries the camera toggles set in the dashboard and pending
    connection tests, so the Node reacts within one upload interval.
    """
    reports = [
        CameraReport(
            camera_id=item.camera_id,
            status=item.status.upper(),
            last_error=item.last_error,
            frames_received=item.frames_received,
            approximate_fps=item.approximate_fps,
            width=item.width,
            height=item.height,
            last_frame_at=item.last_frame_at,
            vision=item.vision,
            objects=item.objects,
            poses=item.poses,
            frame_jpeg=decode_frame(item.frame_jpeg_base64),
            vision_frame=item.vision_frame,
            frame_ref=item.frame_ref,
        )
        for item in batch.cameras
    ]
    return CloudCameraRuntime(get_settings()).record_reports(
        identity.organization_id,
        identity.node_id,
        reports,
        test_results=[(item.job_id, item.result) for item in batch.test_results],
    )


@router.post("/events")
def sync_events(
    events: list[NodeEventPayload],
    background_tasks: BackgroundTasks,
    identity: NodeIdentity = Depends(get_node_identity),
) -> dict:
    accepted = 0
    duplicates = 0
    settings = get_settings()
    # Limpeza automática em segundo plano (no máximo a cada N horas).
    background_tasks.add_task(run_retention_if_due, settings)
    with connect(settings.database_target) as connection:
        for event in events:
            inserted = _insert_event(connection, identity, event)
            accepted += int(inserted)
            duplicates += int(not inserted)
        connection.commit()
    return {"ok": True, "accepted": accepted, "duplicates": duplicates}


@router.post("/metrics")
def sync_metrics(
    metrics: list[NodeMetricPayload],
    background_tasks: BackgroundTasks,
    identity: NodeIdentity = Depends(get_node_identity),
) -> dict:
    accepted = 0
    duplicates = 0
    settings = get_settings()
    # Limpeza automática em segundo plano (no máximo a cada N horas).
    background_tasks.add_task(run_retention_if_due, settings)
    with connect(settings.database_target) as connection:
        for metric in metrics:
            inserted = _insert_metric(connection, identity, metric)
            accepted += int(inserted)
            duplicates += int(not inserted)
        connection.commit()
    return {"ok": True, "accepted": accepted, "duplicates": duplicates}


@router.post("/batch")
def sync_batch(
    batch: NodeSyncBatch,
    background_tasks: BackgroundTasks,
    identity: NodeIdentity = Depends(get_node_identity),
) -> dict:
    settings = get_settings()
    # Limpeza automática em segundo plano (no máximo a cada N horas).
    background_tasks.add_task(run_retention_if_due, settings)
    accepted_events = duplicate_events = accepted_metrics = duplicate_metrics = 0
    with connect(settings.database_target) as connection:
        for event in batch.events:
            inserted = _insert_event(connection, identity, event)
            accepted_events += int(inserted)
            duplicate_events += int(not inserted)
        for metric in batch.metrics:
            inserted = _insert_metric(connection, identity, metric)
            accepted_metrics += int(inserted)
            duplicate_metrics += int(not inserted)
        connection.commit()
    return {
        "ok": True,
        "events": {"accepted": accepted_events, "duplicates": duplicate_events},
        "metrics": {"accepted": accepted_metrics, "duplicates": duplicate_metrics},
    }


def _insert_event(connection, identity: NodeIdentity, event: NodeEventPayload) -> bool:
    cursor = connection.execute(
        """
        INSERT INTO events (
            id, type, camera_id, severity, status, confidence,
            started_at, ended_at, duration, metadata, organization_id, node_id,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT DO NOTHING
        """,
        (
            event.event_id,
            event.event_type,
            event.camera_id,
            event.severity,
            event.status,
            event.confidence,
            event.timestamp,
            event.ended_at,
            event.duration,
            json.dumps(event.metadata, separators=(",", ":")),
            identity.organization_id,
            identity.node_id,
        ),
    )
    if cursor.rowcount:
        _record_sync_item(connection, identity, event.event_id, "event")
    return cursor.rowcount > 0


def _insert_metric(connection, identity: NodeIdentity, metric: NodeMetricPayload) -> bool:
    cursor = connection.execute(
        """
        INSERT INTO node_metrics (
            id, organization_id, node_id, camera_id, metric_type,
            value, payload_json, captured_at, received_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        (
            metric.metric_id,
            identity.organization_id,
            identity.node_id,
            metric.camera_id,
            metric.metric_type,
            metric.value,
            json.dumps(metric.payload, separators=(",", ":")),
            metric.captured_at,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    if cursor.rowcount:
        _record_sync_item(connection, identity, metric.metric_id, "metric")
    return cursor.rowcount > 0


def _record_sync_item(connection, identity: NodeIdentity, item_id: str, item_type: str) -> None:
    connection.execute(
        """
        INSERT INTO node_sync_items (id, organization_id, node_id, type)
        VALUES (?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        (item_id, identity.organization_id, identity.node_id, item_type),
    )
