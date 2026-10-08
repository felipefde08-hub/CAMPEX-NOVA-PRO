from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import uuid4

from campex_node.storage.sqlite import connect, iso, loads, parse_iso, utc_now


logger = logging.getLogger("campex.node.activity")


# Interval kinds
MACHINE = "machine"  # RUNNING | STOPPED | UNKNOWN
PERSON_OCCUPANCY = "occupancy_person"  # OCCUPIED | VACANT
VEHICLE_OCCUPANCY = "occupancy_vehicle"  # OCCUPIED | VACANT

# Per-minute counters
COUNT_FIELDS = ("cycles", "forward", "backward", "external")
# A press can stroke every second: counts are kept in memory and written in
# one transaction at most this often (and before every read).
COUNT_FLUSH_SECONDS = 5.0

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS activity_intervals (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        zone_id TEXT NOT NULL,
        camera_id TEXT NOT NULL,
        state TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        last_seen_at TEXT NOT NULL,
        peak INTEGER NOT NULL DEFAULT 0,
        metadata TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_activity_zone_time ON activity_intervals (zone_id, kind, started_at)",
    """
    CREATE TABLE IF NOT EXISTS zone_counts (
        zone_id TEXT NOT NULL,
        minute TEXT NOT NULL,
        cycles INTEGER NOT NULL DEFAULT 0,
        forward INTEGER NOT NULL DEFAULT 0,
        backward INTEGER NOT NULL DEFAULT 0,
        external INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (zone_id, minute)
    )
    """,
)


@dataclass(frozen=True)
class Interval:
    id: str
    kind: str
    zone_id: str
    camera_id: str
    state: str
    started_at: datetime
    ended_at: datetime | None
    peak: int
    metadata: dict[str, Any]
    # How far an open interval is confirmed (the Node touches it periodically).
    last_seen_at: datetime | None = None

    def end_or(self, now: datetime) -> datetime:
        return self.ended_at or now

    def clipped_seconds(self, start: datetime, end: datetime, now: datetime | None = None) -> float:
        """Seconds of this interval inside [start, end]."""
        stop = min(end, self.end_or(now or utc_now()))
        return max(0.0, (stop - max(start, self.started_at)).total_seconds())

    def as_dict(self, now: datetime | None = None) -> dict[str, Any]:
        ended = self.ended_at
        duration = (self.end_or(now or utc_now()) - self.started_at).total_seconds()
        return {
            "id": self.id,
            "kind": self.kind,
            "zone_id": self.zone_id,
            "camera_id": self.camera_id,
            "state": self.state,
            "started_at": self.started_at.isoformat(),
            "ended_at": ended.isoformat() if ended else None,
            "ongoing": ended is None,
            "duration_seconds": round(max(0.0, duration), 1),
            "peak": self.peak,
            "metadata": self.metadata,
        }


class ActivityStore:
    """State intervals and per-minute counters produced by the factory monitors."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._pending: dict[tuple[str, str, str], int] = defaultdict(int)
        self._pending_lock = threading.Lock()
        self._last_flush = time.monotonic()
        # Called with each interval opened, confirmed or closed, and with the
        # counter rows each flush changed (the Cloud outbox).
        self.interval_listeners: list[Callable[[Interval], None]] = []
        self.count_listeners: list[Callable[[list[dict[str, Any]]], None]] = []

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(connect(self.database_path)) as connection:
            for statement in SCHEMA:
                connection.execute(statement)
            # The Node stopped while these were open: they end where the Node
            # last confirmed them, and the gap stays unaccounted for.
            connection.execute(
                "UPDATE activity_intervals SET ended_at = last_seen_at WHERE ended_at IS NULL"
            )
            connection.commit()

    # Intervals

    def open(
        self,
        *,
        kind: str,
        zone_id: str,
        camera_id: str,
        state: str,
        started_at: datetime,
        peak: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        interval_id = f"act_{uuid4().hex[:12]}"
        with closing(connect(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO activity_intervals (id, kind, zone_id, camera_id, state, started_at, ended_at, last_seen_at, peak, metadata)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (interval_id, kind, zone_id, camera_id, state, iso(started_at), iso(started_at), peak, json.dumps(metadata or {})),
            )
            connection.commit()
        self._notify_interval(interval_id)
        return interval_id

    def touch(self, interval_id: str, seen_at: datetime, peak: int | None = None) -> None:
        with closing(connect(self.database_path)) as connection:
            if peak is None:
                connection.execute(
                    "UPDATE activity_intervals SET last_seen_at = ? WHERE id = ?", (iso(seen_at), interval_id)
                )
            else:
                connection.execute(
                    "UPDATE activity_intervals SET last_seen_at = ?, peak = MAX(peak, ?) WHERE id = ?",
                    (iso(seen_at), peak, interval_id),
                )
            connection.commit()
        self._notify_interval(interval_id)

    def close(self, interval_id: str, ended_at: datetime, peak: int | None = None) -> None:
        with closing(connect(self.database_path)) as connection:
            connection.execute(
                """
                UPDATE activity_intervals
                SET ended_at = MAX(started_at, ?), last_seen_at = MAX(started_at, ?), peak = MAX(peak, ?)
                WHERE id = ?
                """,
                (iso(ended_at), iso(ended_at), peak or 0, interval_id),
            )
            connection.commit()
        self._notify_interval(interval_id)

    def get(self, interval_id: str) -> Interval | None:
        with closing(connect(self.database_path)) as connection:
            row = connection.execute("SELECT * FROM activity_intervals WHERE id = ?", (interval_id,)).fetchone()
        return _row_to_interval(row) if row else None

    def _notify_interval(self, interval_id: str) -> None:
        if not self.interval_listeners:
            return
        interval = self.get(interval_id)
        if interval is None:
            return
        for listener in self.interval_listeners:
            try:
                listener(interval)
            except Exception:
                logger.exception("Interval listener failed for %s", interval_id)

    def intervals(
        self,
        *,
        kind: str | None = None,
        zone_ids: Iterable[str] | None = None,
        state: str | None = None,
        start: datetime,
        end: datetime,
    ) -> list[Interval]:
        """Intervals overlapping [start, end], oldest first."""
        query = "SELECT * FROM activity_intervals WHERE started_at < ? AND (ended_at IS NULL OR ended_at > ?)"
        params: list[Any] = [iso(end), iso(start)]
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
        with closing(connect(self.database_path)) as connection:
            rows = connection.execute(query + " ORDER BY started_at ASC", params).fetchall()
        return [_row_to_interval(row) for row in rows]

    # Counters

    def add_count(self, zone_id: str, at: datetime, field: str, amount: int = 1) -> None:
        if field not in COUNT_FIELDS:
            raise ValueError(f"Unknown counter {field}.")
        with self._pending_lock:
            self._pending[(zone_id, minute_key(at), field)] += amount
            due = time.monotonic() - self._last_flush >= COUNT_FLUSH_SECONDS
        if due:
            self.flush()

    def flush(self) -> None:
        with self._pending_lock:
            pending, self._pending = self._pending, defaultdict(int)
            self._last_flush = time.monotonic()
        if not pending:
            return
        with closing(connect(self.database_path)) as connection:
            for (zone_id, minute, field), amount in pending.items():
                connection.execute(
                    f"""
                    INSERT INTO zone_counts (zone_id, minute, {field}) VALUES (?, ?, ?)
                    ON CONFLICT(zone_id, minute) DO UPDATE SET {field} = {field} + excluded.{field}
                    """,
                    (zone_id, minute, amount),
                )
            connection.commit()
            if not self.count_listeners:
                return
            keys = sorted({(zone_id, minute) for zone_id, minute, _field in pending})
            rows = [
                connection.execute("SELECT * FROM zone_counts WHERE zone_id = ? AND minute = ?", key).fetchone()
                for key in keys
            ]
        changed = [{"zone_id": row["zone_id"], "minute": row["minute"], **{f: row[f] for f in COUNT_FIELDS}} for row in rows if row]
        for listener in self.count_listeners:
            try:
                listener(changed)
            except Exception:
                logger.exception("Count listener failed")

    def counts(self, zone_ids: Iterable[str], start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Per-minute rows in [start, end): zone_id, minute (datetime) and the counters."""
        ids = list(zone_ids)
        if not ids:
            return []
        self.flush()
        with closing(connect(self.database_path)) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM zone_counts
                WHERE zone_id IN ({','.join('?' for _ in ids)}) AND minute >= ? AND minute <= ?
                ORDER BY minute ASC
                """,
                (*ids, minute_key(start), minute_key(end)),
            ).fetchall()
        result = []
        for row in rows:
            minute = parse_iso(row["minute"] + ":00+00:00")
            if minute is None or minute >= end:
                continue
            result.append({"zone_id": row["zone_id"], "minute": minute, **{field: row[field] for field in COUNT_FIELDS}})
        return result


def minute_key(at: datetime) -> str:
    return iso(at)[:16]  # type: ignore[index]


def _row_to_interval(row) -> Interval:
    return Interval(
        id=row["id"],
        kind=row["kind"],
        zone_id=row["zone_id"],
        camera_id=row["camera_id"],
        state=row["state"],
        started_at=parse_iso(row["started_at"]),  # type: ignore[arg-type]
        ended_at=parse_iso(row["ended_at"]),
        peak=row["peak"],
        metadata=loads(row["metadata"]),
        last_seen_at=parse_iso(row["last_seen_at"]),
    )
