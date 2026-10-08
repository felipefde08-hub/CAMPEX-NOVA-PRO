from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field, ValidationError

from backend.cameras.cloud_runtime import CameraReport, CloudCameraRuntime, decode_frame
from backend.cloud.nodes import NodeIdentity, get_node_identity
from backend.config import get_settings
from backend.database.db import connect
from backend.maintenance.retention import run_retention_if_due
from backend.notifications.node_alerts import alertable, send_node_event_alerts


logger = logging.getLogger("campex.node_sync")


router = APIRouter(prefix="/api/v1/node-sync", tags=["node-sync"])


class NodeEventPayload(BaseModel):
    event_id: str = Field(min_length=1, max_length=160)
    camera_id: str = Field(min_length=1, max_length=120)
    zone_id: str | None = Field(default=None, max_length=120)
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


class NodeIntervalPayload(BaseModel):
    interval_id: str = Field(min_length=1, max_length=160)
    kind: str = Field(min_length=1, max_length=40)
    zone_id: str = Field(min_length=1, max_length=120)
    camera_id: str = Field(min_length=1, max_length=120)
    state: str = Field(min_length=1, max_length=20)
    started_at: str = Field(min_length=1, max_length=40)
    ended_at: str | None = Field(default=None, max_length=40)
    last_seen_at: str = Field(min_length=1, max_length=40)
    peak: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class NodeCountPayload(BaseModel):
    zone_id: str = Field(min_length=1, max_length=120)
    minute: str = Field(min_length=16, max_length=16)  # YYYY-MM-DDTHH:MM, UTC
    cycles: int = Field(default=0, ge=0)
    forward: int = Field(default=0, ge=0)
    backward: int = Field(default=0, ge=0)
    external: int = Field(default=0, ge=0)


class NodeFactorySnapshot(BaseModel):
    settings: dict[str, Any] = Field(default_factory=dict)
    shifts: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    zones: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    cameras: list[dict[str, Any]] = Field(default_factory=list, max_length=200)


class NodeFactoryBatch(BaseModel):
    """Everything the Node's outbox delivers in one request.

    Items stay raw here and are validated one by one, so a single bad item
    is dropped instead of blocking the Node's queue forever.
    """

    events: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    metrics: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    intervals: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    counts: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    factory: list[dict[str, Any]] = Field(default_factory=list, max_length=5)


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


@router.post("/v2/batch")
def sync_factory_batch(
    batch: NodeFactoryBatch,
    background_tasks: BackgroundTasks,
    identity: NodeIdentity = Depends(get_node_identity),
) -> dict:
    """Events (inserted, then updated as they close), metrics, machine and
    zone intervals, per-minute counters and the factory snapshot."""
    settings = get_settings()
    background_tasks.add_task(run_retention_if_due, settings)
    totals = {kind: {"accepted": 0, "rejected": 0} for kind in ("events", "metrics", "intervals", "counts", "factory")}
    alerts: list[dict[str, Any]] = []

    def parse(model, raw: dict[str, Any], kind: str):
        try:
            return model.model_validate(raw)
        except ValidationError:
            totals[kind]["rejected"] += 1
            return None

    with connect(settings.database_target) as connection:
        for raw in batch.factory[-1:]:  # only the latest snapshot matters
            snapshot = parse(NodeFactorySnapshot, raw, "factory")
            if snapshot is not None:
                _save_factory_snapshot(connection, identity, snapshot)
                totals["factory"]["accepted"] += 1
        for raw in batch.events:
            event = parse(NodeEventPayload, raw, "events")
            if event is None:
                continue
            outcome = _upsert_event(connection, identity, event)
            if outcome is None:
                totals["events"]["rejected"] += 1
                continue
            totals["events"]["accepted"] += 1
            if outcome == "created" and alertable(event.model_dump()):
                alerts.append(event.model_dump())
        for raw in batch.metrics:
            metric = parse(NodeMetricPayload, raw, "metrics")
            if metric is not None:
                _insert_metric(connection, identity, metric)
                totals["metrics"]["accepted"] += 1
        for raw in batch.intervals:
            interval = parse(NodeIntervalPayload, raw, "intervals")
            if interval is None:
                continue
            if _upsert_interval(connection, identity, interval):
                totals["intervals"]["accepted"] += 1
            else:
                totals["intervals"]["rejected"] += 1
        for raw in batch.counts:
            count = parse(NodeCountPayload, raw, "counts")
            if count is not None:
                _upsert_count(connection, identity, count)
                totals["counts"]["accepted"] += 1
        camera_names = _camera_names(connection, identity) if alerts else {}
        connection.commit()
    if any(item["rejected"] for item in totals.values()):
        logger.warning("Node %s batch had rejected items: %s", identity.node_id, totals)
    if alerts:
        background_tasks.add_task(send_node_event_alerts, settings, identity.organization_id, alerts, camera_names)
    return {"ok": True, **totals}


def _upsert_event(connection, identity: NodeIdentity, event: NodeEventPayload) -> str | None:
    """'created', 'updated', or None when the id belongs to another Node."""
    row = connection.execute(
        "SELECT organization_id, node_id FROM events WHERE id = ?", (event.event_id,)
    ).fetchone()
    metadata = json.dumps(event.metadata, separators=(",", ":"))
    if row is None:
        connection.execute(
            """
            INSERT INTO events (
                id, type, camera_id, zone_id, severity, status, confidence,
                started_at, ended_at, duration, metadata, organization_id, node_id,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT DO NOTHING
            """,
            (
                event.event_id, event.event_type, event.camera_id, event.zone_id, event.severity,
                event.status, event.confidence, event.timestamp, event.ended_at, event.duration,
                metadata, identity.organization_id, identity.node_id,
            ),
        )
        _record_sync_item(connection, identity, event.event_id, "event")
        return "created"
    if row["organization_id"] != identity.organization_id or row["node_id"] != identity.node_id:
        return None
    # A review made in the dashboard is kept when the Node closes the event.
    connection.execute(
        """
        UPDATE events
        SET status = CASE WHEN status = 'REVIEWED' THEN status ELSE ? END,
            severity = ?, ended_at = ?, duration = ?, metadata = ?, zone_id = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (event.status, event.severity, event.ended_at, event.duration, metadata, event.zone_id, event.event_id),
    )
    return "updated"


def _upsert_interval(connection, identity: NodeIdentity, interval: NodeIntervalPayload) -> bool:
    row = connection.execute(
        "SELECT organization_id, node_id FROM node_activity_intervals WHERE id = ?", (interval.interval_id,)
    ).fetchone()
    if row is not None and (row["organization_id"] != identity.organization_id or row["node_id"] != identity.node_id):
        return False
    values = (
        interval.kind, interval.zone_id, interval.camera_id, interval.state, interval.started_at,
        interval.ended_at, interval.last_seen_at, interval.peak, json.dumps(interval.metadata, separators=(",", ":")),
    )
    if row is None:
        connection.execute(
            """
            INSERT INTO node_activity_intervals (
                kind, zone_id, camera_id, state, started_at, ended_at, last_seen_at, peak, metadata,
                id, organization_id, node_id, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT DO NOTHING
            """,
            (*values, interval.interval_id, identity.organization_id, identity.node_id),
        )
    else:
        connection.execute(
            """
            UPDATE node_activity_intervals
            SET kind = ?, zone_id = ?, camera_id = ?, state = ?, started_at = ?, ended_at = ?,
                last_seen_at = ?, peak = ?, metadata = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (*values, interval.interval_id),
        )
    return True


def _upsert_count(connection, identity: NodeIdentity, count: NodeCountPayload) -> None:
    # The Node sends each minute's running totals: the latest one wins.
    connection.execute(
        """
        INSERT INTO node_zone_counts (
            organization_id, node_id, zone_id, minute_at, cycles, forward, backward, external
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (node_id, zone_id, minute_at) DO UPDATE SET
            cycles = excluded.cycles,
            forward = excluded.forward,
            backward = excluded.backward,
            external = excluded.external
        """,
        (
            identity.organization_id, identity.node_id, count.zone_id, count.minute,
            count.cycles, count.forward, count.backward, count.external,
        ),
    )


def _save_factory_snapshot(connection, identity: NodeIdentity, snapshot: NodeFactorySnapshot) -> None:
    connection.execute(
        """
        INSERT INTO node_factory_snapshots (node_id, organization_id, payload, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT (node_id) DO UPDATE SET
            organization_id = excluded.organization_id,
            payload = excluded.payload,
            updated_at = CURRENT_TIMESTAMP
        """,
        (identity.node_id, identity.organization_id, json.dumps(snapshot.model_dump(), separators=(",", ":"))),
    )


def _camera_names(connection, identity: NodeIdentity) -> dict[str, str]:
    row = connection.execute(
        "SELECT payload FROM node_factory_snapshots WHERE node_id = ? AND organization_id = ?",
        (identity.node_id, identity.organization_id),
    ).fetchone()
    if row is None:
        return {}
    try:
        cameras = json.loads(row["payload"]).get("cameras") or []
    except (ValueError, AttributeError):
        return {}
    return {str(item.get("id")): str(item.get("name")) for item in cameras if item.get("id") and item.get("name")}


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
