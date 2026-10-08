"""Factory reports in CAMPEX Cloud, from the data CAMPEX Nodes sync.

The Node's ``FactoryAnalytics`` runs unchanged on stores that read the
Cloud's tables, so the shift, break and cost rules live in one place. Each
Node is a plant with its own shifts and time zone: reports are per Node.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from backend.config import Settings
from backend.database.db import connect
from backend.events.models import Event
from backend.zones.models import Zone, ZonePoint
from campex_node.activity import COUNT_FIELDS, Interval
from campex_node.analytics import FactoryAnalytics, resolve_period
from campex_node.factory import SnapshotFactory
from campex_node.storage.sqlite import parse_iso, utc_now


# An open interval the Node stopped confirming (offline, unplugged) ends
# where it was last confirmed instead of growing until now.
STALE_INTERVAL = timedelta(minutes=5)


@dataclass(frozen=True)
class FactoryNode:
    node_id: str
    name: str
    snapshot: dict[str, Any]
    updated_at: str | None


def factory_nodes(settings: Settings, organization_id: str) -> list[FactoryNode]:
    """The organization's Nodes that synced a factory snapshot."""
    with connect(settings.database_target) as connection:
        rows = connection.execute(
            """
            SELECT s.node_id, s.payload, s.updated_at, n.name
            FROM node_factory_snapshots s
            LEFT JOIN campex_nodes n ON n.id = s.node_id
            WHERE s.organization_id = ?
            ORDER BY s.node_id
            """,
            (organization_id,),
        ).fetchall()
    nodes = []
    for row in rows:
        try:
            snapshot = json.loads(row["payload"])
        except ValueError:
            continue
        nodes.append(FactoryNode(row["node_id"], row["name"] or row["node_id"], snapshot, row["updated_at"]))
    return nodes


def factory_analytics(settings: Settings, organization_id: str, node: FactoryNode) -> FactoryAnalytics:
    return FactoryAnalytics(
        CloudEvents(settings, organization_id, node.node_id, node.snapshot),
        CloudActivity(settings, organization_id, node.node_id),
        SnapshotFactory(node.snapshot),
    )


def factory_summaries(
    settings: Settings, organization_id: str, *, period: str = "today", now: datetime | None = None
) -> list[dict[str, Any]]:
    """``FactoryAnalytics.summary`` of each Node for a named period."""
    now = now or utc_now()
    summaries = []
    for node in factory_nodes(settings, organization_id):
        analytics = factory_analytics(settings, organization_id, node)
        start, end = resolve_period(analytics.factory, period=period, now=now)
        summaries.append(
            {
                "node_id": node.node_id,
                "node_name": node.name,
                "period": period,
                "timezone": analytics.factory.settings()["timezone"],
                "summary": analytics.summary(start, end, now),
            }
        )
    return summaries


class CloudEvents:
    """The part of ``NodeEventStore`` the analytics read, from the Cloud."""

    def __init__(self, settings: Settings, organization_id: str, node_id: str, snapshot: dict[str, Any]) -> None:
        self.settings = settings
        self.organization_id = organization_id
        self.node_id = node_id
        self._zones = [_zone(item) for item in snapshot.get("zones") or []]
        self._settings = {str(item["id"]): item.get("settings") or {} for item in snapshot.get("zones") or []}

    def list_zones(self, camera_id: str | None = None) -> list[Zone]:
        return [zone for zone in self._zones if camera_id is None or zone.camera_id == camera_id]

    def zone_settings(self, camera_id: str | None = None) -> dict[str, dict[str, Any]]:
        return {zone.id: dict(self._settings.get(zone.id, {})) for zone in self.list_zones(camera_id)}

    def events_between(self, start: datetime, end: datetime, event_type: str | None = None) -> list[Event]:
        query = (
            "SELECT * FROM events WHERE organization_id = ? AND node_id = ? AND started_at >= ? AND started_at < ?"
        )
        params: list[Any] = [self.organization_id, self.node_id, _utc_iso(start), _utc_iso(end)]
        if event_type:
            query += " AND type = ?"
            params.append(event_type)
        with connect(self.settings.database_target) as connection:
            rows = connection.execute(query + " ORDER BY started_at ASC", params).fetchall()
        return [_event(row) for row in rows]


class CloudActivity:
    """The part of ``ActivityStore`` the analytics read, from the Cloud."""

    def __init__(self, settings: Settings, organization_id: str, node_id: str) -> None:
        self.settings = settings
        self.organization_id = organization_id
        self.node_id = node_id

    def intervals(
        self,
        *,
        kind: str | None = None,
        zone_ids: Iterable[str] | None = None,
        state: str | None = None,
        start: datetime,
        end: datetime,
    ) -> list[Interval]:
        query = (
            "SELECT * FROM node_activity_intervals WHERE organization_id = ? AND node_id = ?"
            " AND started_at < ? AND (ended_at IS NULL OR ended_at > ?)"
        )
        params: list[Any] = [self.organization_id, self.node_id, _utc_iso(end), _utc_iso(start)]
        if kind:
            query += " AND kind = ?"
            params.append(kind)
        if state:
            query += " AND state = ?"
            params.append(state)
        if zone_ids is not None:
            ids = list(zone_ids)
            if not ids:
                return []
            query += f" AND zone_id IN ({','.join('?' for _ in ids)})"
            params.extend(ids)
        with connect(self.settings.database_target) as connection:
            rows = connection.execute(query + " ORDER BY started_at ASC", params).fetchall()
        now = utc_now()
        intervals = [_interval(row, now) for row in rows]
        return [item for item in intervals if item.ended_at is None or item.ended_at > start]

    def counts(self, zone_ids: Iterable[str], start: datetime, end: datetime) -> list[dict[str, Any]]:
        ids = list(zone_ids)
        if not ids:
            return []
        with connect(self.settings.database_target) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM node_zone_counts
                WHERE organization_id = ? AND node_id = ? AND zone_id IN ({','.join('?' for _ in ids)})
                  AND minute_at >= ? AND minute_at <= ?
                ORDER BY minute_at ASC
                """,
                (self.organization_id, self.node_id, *ids, _utc_iso(start)[:16], _utc_iso(end)[:16]),
            ).fetchall()
        result = []
        for row in rows:
            minute = parse_iso(row["minute_at"] + ":00+00:00")
            if minute is None or minute >= end:
                continue
            result.append({"zone_id": row["zone_id"], "minute": minute, **{field: row[field] for field in COUNT_FIELDS}})
        return result


def _utc_iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat()


def _zone(item: dict[str, Any]) -> Zone:
    return Zone(
        id=str(item["id"]),
        camera_id=str(item.get("camera_id") or ""),
        name=str(item.get("name") or ""),
        type=str(item.get("type") or "monitored"),
        enabled=bool(item.get("enabled", True)),
        points=[ZonePoint(x=float(x), y=float(y)) for x, y in item.get("points") or []],
        created_at=str(item.get("created_at") or ""),
        updated_at=str(item.get("updated_at") or ""),
    )


def _event(row) -> Event:
    try:
        metadata = json.loads(row["metadata"] or "{}")
    except ValueError:
        metadata = {}
    return Event(
        id=row["id"],
        type=row["type"],
        camera_id=row["camera_id"],
        zone_id=row["zone_id"],
        track_id=row["track_id"],
        severity=row["severity"],
        status=row["status"],
        confidence=row["confidence"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        duration=row["duration"],
        metadata=metadata,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _interval(row, now: datetime) -> Interval:
    started = parse_iso(row["started_at"])
    ended = parse_iso(row["ended_at"])
    last_seen = parse_iso(row["last_seen_at"]) or started
    if ended is None and last_seen is not None and now - last_seen > STALE_INTERVAL:
        ended = max(last_seen, started)  # type: ignore[type-var]
    try:
        metadata = json.loads(row["metadata"] or "{}")
    except ValueError:
        metadata = {}
    return Interval(
        id=row["id"],
        kind=row["kind"],
        zone_id=row["zone_id"],
        camera_id=row["camera_id"],
        state=row["state"],
        started_at=started,  # type: ignore[arg-type]
        ended_at=ended,
        peak=int(row["peak"] or 0),
        metadata=metadata,
        last_seen_at=last_seen,
    )
