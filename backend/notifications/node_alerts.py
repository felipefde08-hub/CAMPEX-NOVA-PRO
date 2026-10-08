"""Alerts for the factory events a CAMPEX Node syncs."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.config import Settings
from backend.notifications.models import NODE_EVENT_ALERTS
from backend.notifications.service import NotificationService


logger = logging.getLogger("campex.notifications.node")

# A Node back online after hours delivers old events: they go to the reports,
# not to the phone.
ALERT_MAX_AGE = timedelta(minutes=30)


def alertable(event: dict[str, Any], now: datetime | None = None) -> bool:
    if event.get("event_type") not in NODE_EVENT_ALERTS or event.get("status") != "OPEN":
        return False
    try:
        started = datetime.fromisoformat(str(event.get("timestamp")))
    except ValueError:
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - started <= ALERT_MAX_AGE


def send_node_event_alerts(
    settings: Settings,
    organization_id: str,
    events: list[dict[str, Any]],
    camera_names: dict[str, str],
    service: NotificationService | None = None,
) -> None:
    service = service or NotificationService(settings)
    for event in events:
        metadata = event.get("metadata") or {}
        try:
            service.send_alert(
                organization_id,
                {
                    "id": event["event_id"],
                    "event_type": NODE_EVENT_ALERTS[event["event_type"]],
                    "source": "campex_node",
                    "camera_id": event.get("camera_id"),
                    "camera_name": camera_names.get(str(event.get("camera_id"))),
                    "zone_name": metadata.get("zone_name"),
                    "line": metadata.get("line"),
                    "started_at": event.get("timestamp"),
                },
            )
        except Exception:
            logger.exception("Alert for Node event %s failed", event.get("event_id"))
