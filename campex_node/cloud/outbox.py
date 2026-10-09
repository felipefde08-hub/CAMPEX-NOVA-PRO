"""Queues the factory's data for CAMPEX Cloud.

The monitors write events, state intervals (machine running/stopped, zone
occupied/vacant) and per-minute counters to the Node's database; this module
puts the latest state of each into the outbox, and ``SyncService`` delivers
them in batches. With the zones, shifts and camera names (the "factory"
snapshot) the Cloud can build the same reports and alerts as the Node.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Callable

from backend.events.models import Event

from campex_node.activity import ActivityStore, Interval
from campex_node.events import NodeEventStore
from campex_node.factory import FactoryStore
from campex_node.storage.local_store import LocalStore


logger = logging.getLogger("campex.node.outbox")

FACTORY_CHECK_SECONDS = 60.0
# Paths on the Node's disk mean nothing to the Cloud (and name the user's
# folders); the event metadata repeats them under facts.evidence.
LOCAL_METADATA_KEYS = frozenset({"snapshot_path", "overlay_path", "clip_path", "metadata_path", "paths"})


class CloudOutbox:
    def __init__(
        self,
        store: LocalStore,
        events: NodeEventStore,
        activity: ActivityStore,
        factory: FactoryStore,
        cameras: Callable[[], list[Any]] = list,
    ) -> None:
        self.store = store
        self.events = events
        self.activity = activity
        self.factory = factory
        self.cameras = cameras
        self._factory_digest: str | None = None
        self._factory_checked = 0.0

    def attach(self) -> None:
        self.events.listeners.append(self.on_event)
        self.activity.interval_listeners.append(self.on_interval)
        self.activity.count_listeners.append(self.on_counts)

    def detach(self) -> None:
        for listeners, callback in (
            (self.events.listeners, self.on_event),
            (self.activity.interval_listeners, self.on_interval),
            (self.activity.count_listeners, self.on_counts),
        ):
            if callback in listeners:
                listeners.remove(callback)

    def on_event(self, event: Event) -> None:
        self.store.enqueue_state("event", f"event:{event.id}", event_payload(event))

    def on_interval(self, interval: Interval) -> None:
        self.store.enqueue_state("interval", f"interval:{interval.id}", interval_payload(interval))

    def on_counts(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            self.store.enqueue_state("count", f"count:{row['zone_id']}:{row['minute']}", row)

    def refresh_factory(self, *, force: bool = False) -> bool:
        """Queues the zones, shifts and cameras when they changed."""
        now = time.monotonic()
        if not force and now - self._factory_checked < FACTORY_CHECK_SECONDS:
            return False
        self._factory_checked = now
        snapshot = factory_snapshot(self.events, self.factory, self.cameras())
        digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode("utf-8")).hexdigest()
        if digest == self._factory_digest:
            return False
        self.store.enqueue_state("factory", "factory", snapshot)
        self._factory_digest = digest
        return True


def event_payload(event: Event) -> dict[str, Any]:
    return {
        "event_id": event.id,
        "camera_id": event.camera_id,
        "zone_id": event.zone_id,
        "event_type": event.type,
        "severity": event.severity,
        "status": event.status,
        "timestamp": event.started_at,
        "ended_at": event.ended_at,
        "duration": event.duration,
        "confidence": event.confidence,
        "metadata": _without_local_paths(event.metadata or {}),
    }


def _without_local_paths(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_local_paths(item) for key, item in value.items() if key not in LOCAL_METADATA_KEYS}
    if isinstance(value, list):
        return [_without_local_paths(item) for item in value]
    return value


def interval_payload(interval: Interval) -> dict[str, Any]:
    last_seen = interval.last_seen_at or interval.started_at
    return {
        "interval_id": interval.id,
        "kind": interval.kind,
        "zone_id": interval.zone_id,
        "camera_id": interval.camera_id,
        "state": interval.state,
        "started_at": interval.started_at.isoformat(),
        "ended_at": interval.ended_at.isoformat() if interval.ended_at else None,
        "last_seen_at": last_seen.isoformat(),
        "peak": interval.peak,
        "metadata": interval.metadata,
    }


def factory_snapshot(events: NodeEventStore, factory: FactoryStore, cameras: list[Any]) -> dict[str, Any]:
    settings = events.zone_settings()
    return {
        "settings": factory.settings(),
        "shifts": [shift.as_dict() for shift in factory.list_shifts()],
        "zones": [{**zone.as_dict(), "settings": settings.get(zone.id, {})} for zone in events.list_zones()],
        "cameras": [{"id": camera.id, "name": camera.name} for camera in cameras],
    }
