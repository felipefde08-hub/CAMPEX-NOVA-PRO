from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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

    def enqueue_event(self, event_type: str, payload: dict[str, Any]) -> str:
        event_id = f"evt_{uuid.uuid4().hex}"
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO outbound_events (
                    id, type, payload_json, status, attempts, created_at, updated_at
                )
                VALUES (?, ?, ?, 'pending', 0, ?, ?)
                """,
                (event_id, event_type, json.dumps(payload, separators=(",", ":")), now, now),
            )
            connection.commit()
        return event_id

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
