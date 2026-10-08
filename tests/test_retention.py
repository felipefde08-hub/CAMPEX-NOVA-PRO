from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from backend.database.db import connect, initialize_database
from backend.maintenance import retention
from backend.maintenance.retention import delete_evidence_files, run_retention, run_retention_if_due
from tests.helpers import make_settings


NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def _settings(tmp_path):
    settings = replace(make_settings(tmp_path / "campex.sqlite3"), retention_evidence_days=0)
    initialize_database(settings)
    return settings


def _insert_metric(connection, metric_id: str, captured_at: datetime) -> None:
    connection.execute(
        """
        INSERT INTO node_metrics (id, organization_id, node_id, camera_id, metric_type, value, captured_at)
        VALUES (?, 'default', 'node_1', 'cam_1', 'camera_health', 1, ?)
        """,
        (metric_id, captured_at.isoformat()),
    )
    # received_at usa o formato de CURRENT_TIMESTAMP ("YYYY-MM-DD HH:MM:SS").
    connection.execute(
        "INSERT INTO node_sync_items (id, organization_id, node_id, type, received_at) VALUES (?, 'default', 'node_1', 'metric', ?)",
        (metric_id, captured_at.strftime("%Y-%m-%d %H:%M:%S")),
    )


def test_retention_removes_only_rows_older_than_window(tmp_path):
    settings = _settings(tmp_path)
    with connect(settings.database_target) as connection:
        _insert_metric(connection, "old", NOW - timedelta(days=10))
        _insert_metric(connection, "recent", NOW - timedelta(days=2))
        connection.commit()

    result = run_retention(settings, now=NOW)

    assert result["deleted_rows"]["node_metrics"] == 1
    assert result["deleted_rows"]["node_sync_items"] == 1
    with connect(settings.database_target) as connection:
        remaining = [row["id"] for row in connection.execute("SELECT id FROM node_metrics")]
        remaining_sync = [row["id"] for row in connection.execute("SELECT id FROM node_sync_items")]
    assert remaining == ["recent"]
    assert remaining_sync == ["recent"]


def test_retention_zero_days_keeps_everything(tmp_path):
    settings = replace(_settings(tmp_path), retention_metrics_days=0)
    with connect(settings.database_target) as connection:
        _insert_metric(connection, "very_old", NOW - timedelta(days=400))
        connection.commit()

    result = run_retention(settings, now=NOW)

    assert "node_metrics" not in result["deleted_rows"]


def test_retention_runs_at_most_once_per_interval(tmp_path):
    settings = _settings(tmp_path)

    first = run_retention_if_due(settings, now=NOW)
    too_soon = run_retention_if_due(settings, now=NOW + timedelta(hours=1))
    later = run_retention_if_due(settings, now=NOW + timedelta(hours=settings.retention_interval_hours, minutes=1))

    assert first is not None
    assert too_soon is None
    assert later is not None


def test_retention_if_due_never_raises(tmp_path, monkeypatch):
    settings = _settings(tmp_path)

    def broken(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(retention, "run_retention", broken)

    assert run_retention_if_due(settings, now=NOW) is None


def test_delete_evidence_files_respects_cutoff(tmp_path):
    root = tmp_path / "evidence"
    old_dir = root / "evt_old"
    new_dir = root / "evt_new"
    old_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)
    old_file = old_dir / "clean.jpg"
    new_file = new_dir / "clean.jpg"
    old_file.write_bytes(b"x" * 10)
    new_file.write_bytes(b"y" * 10)
    old_time = (NOW - timedelta(days=100)).timestamp()
    os.utime(old_file, (old_time, old_time))

    result = delete_evidence_files(root, older_than=NOW - timedelta(days=90))

    assert result == {"deleted_files": 1, "deleted_bytes": 10}
    assert not old_dir.exists()
    assert new_file.exists()
