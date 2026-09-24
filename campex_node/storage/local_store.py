from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from campex_node.core.config import NodeCameraConfig


SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS node_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS outbound_events (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT,
        last_error TEXT,
        synced_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_outbound_events_status_created
    ON outbound_events(status, created_at)
    """,
)


class LocalStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            for statement in SCHEMA:
                connection.execute(statement)
            self._migrate_outbound_events(connection)
            connection.commit()

    def get_meta(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM node_meta WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO node_meta (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, now),
            )
            connection.commit()

    def meta_dict(self) -> dict[str, str]:
        with self._connect() as connection:
            rows = connection.execute("SELECT key, value FROM node_meta").fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def get_local_cameras(self) -> list[NodeCameraConfig]:
        return self._get_camera_meta("local_cameras_json")

    def get_cached_cloud_cameras(self) -> list[NodeCameraConfig]:
        return self._get_camera_meta("cloud_cameras_json")

    def _get_camera_meta(self, key: str) -> list[NodeCameraConfig]:
        raw_value = self.get_meta(key)
        if not raw_value:
            return []
        try:
            payload = json.loads(raw_value)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        cameras: list[NodeCameraConfig] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                cameras.append(NodeCameraConfig.from_mapping(item))
            except ValueError:
                continue
        return cameras

    def save_local_camera(self, camera: NodeCameraConfig) -> None:
        cameras = {item.id: item for item in self.get_local_cameras()}
        cameras[camera.id] = camera
        self._save_local_cameras(cameras.values())

    def delete_local_camera(self, camera_id: str) -> None:
        cameras = {item.id: item for item in self.get_local_cameras()}
        cameras.pop(camera_id, None)
        self._save_local_cameras(cameras.values())

    def _save_local_cameras(self, cameras: Any) -> None:
        camera_items = cameras.values() if hasattr(cameras, "values") else cameras
        payload = [
            {
                "id": item.id,
                "name": item.name,
                "rtsp_url": item.rtsp_url,
                "enabled": item.enabled,
            }
            for item in sorted(camera_items, key=lambda item: item.name.lower())
        ]
        self.set_meta("local_cameras_json", json.dumps(payload, separators=(",", ":")))

    def outbound_summary(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS total
                FROM outbound_events
                GROUP BY status
                """
            ).fetchall()
        summary = {"pending": 0, "synced": 0}
        for row in rows:
            summary[str(row["status"])] = int(row["total"])
        return summary

    def enqueue_event(self, event_type: str, payload: dict[str, Any], event_id: str | None = None) -> str:
        event_id = event_id or str(payload.get("event_id") or payload.get("metric_id") or f"evt_{uuid.uuid4().hex}")
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO outbound_events (
                    id, type, payload_json, status, attempts, next_attempt_at, created_at, updated_at
                )
                VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    event_id,
                    event_type,
                    json.dumps(payload, separators=(",", ":")),
                    now,
                    now,
                    now,
                ),
            )
            connection.commit()
        return event_id

    def pending_outbound(self, limit: int = 50) -> list[dict[str, Any]]:
        now = _utc_now()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM outbound_events
                WHERE status = 'pending'
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
        return [_row_to_outbound_item(row) for row in rows]

    def mark_outbound_synced(self, item_id: str) -> None:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE outbound_events
                SET status = 'synced', synced_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, item_id),
            )
            connection.commit()

    def mark_outbound_failed(self, item_id: str, error: str, *, retry_seconds: float) -> None:
        now_dt = datetime.now(timezone.utc)
        next_attempt = datetime.fromtimestamp(
            now_dt.timestamp() + max(0.0, retry_seconds),
            tz=timezone.utc,
        ).isoformat()
        now = now_dt.isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE outbound_events
                SET attempts = attempts + 1,
                    last_error = ?,
                    next_attempt_at = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (error[:1000], next_attempt, now, item_id),
            )
            connection.commit()

    def outbound_queue_size(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM outbound_events WHERE status = 'pending'"
            ).fetchone()
        return int(row["total"] if row else 0)

    def _migrate_outbound_events(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(outbound_events)").fetchall()
        }
        migrations = {
            "next_attempt_at": "ALTER TABLE outbound_events ADD COLUMN next_attempt_at TEXT",
            "last_error": "ALTER TABLE outbound_events ADD COLUMN last_error TEXT",
            "synced_at": "ALTER TABLE outbound_events ADD COLUMN synced_at TEXT",
        }
        for column, statement in migrations.items():
            if column not in columns:
                connection.execute(statement)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_outbound_item(row) -> dict[str, Any]:
    payload = json.loads(row["payload_json"])
    return {
        "id": row["id"],
        "type": row["type"],
        "payload": payload,
        "attempts": int(row["attempts"] or 0),
        "last_error": row["last_error"],
    }
