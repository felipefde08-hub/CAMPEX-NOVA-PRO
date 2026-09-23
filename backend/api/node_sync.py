from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from backend.cloud.nodes import NodeIdentity, get_node_identity
from backend.config import get_settings
from backend.database.db import connect


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


@router.post("/events")
def sync_events(
    events: list[NodeEventPayload],
    identity: NodeIdentity = Depends(get_node_identity),
) -> dict:
    accepted = 0
    duplicates = 0
    settings = get_settings()
    with connect(settings.sqlite_path) as connection:
        for event in events:
            inserted = _insert_event(connection, identity, event)
            accepted += int(inserted)
            duplicates += int(not inserted)
        connection.commit()
    return {"ok": True, "accepted": accepted, "duplicates": duplicates}


@router.post("/metrics")
def sync_metrics(
    metrics: list[NodeMetricPayload],
    identity: NodeIdentity = Depends(get_node_identity),
) -> dict:
    accepted = 0
    duplicates = 0
    settings = get_settings()
    with connect(settings.sqlite_path) as connection:
        for metric in metrics:
            inserted = _insert_metric(connection, identity, metric)
            accepted += int(inserted)
            duplicates += int(not inserted)
        connection.commit()
    return {"ok": True, "accepted": accepted, "duplicates": duplicates}


@router.post("/batch")
def sync_batch(
    batch: NodeSyncBatch,
    identity: NodeIdentity = Depends(get_node_identity),
) -> dict:
    settings = get_settings()
    accepted_events = duplicate_events = accepted_metrics = duplicate_metrics = 0
    with connect(settings.sqlite_path) as connection:
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
        INSERT OR IGNORE INTO events (
            id, type, camera_id, severity, status, confidence,
            started_at, ended_at, duration, metadata, organization_id, node_id,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
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
        INSERT OR IGNORE INTO node_metrics (
            id, organization_id, node_id, camera_id, metric_type,
            value, payload_json, captured_at, received_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        INSERT OR IGNORE INTO node_sync_items (id, organization_id, node_id, type)
        VALUES (?, ?, ?, ?)
        """,
        (item_id, identity.organization_id, identity.node_id, item_type),
    )
